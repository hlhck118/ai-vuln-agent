# -*- coding: utf-8 -*-
"""审查档位 `auto`：**单子照建、平台自裁、不留人等**。

## 为什么这一档必须单独测

`off` 与 `auto` 在"命令会不会被执行"上看起来一样，但它们在审计里是相反的两件事：

* `off` —— 根本不建单。事后翻 `/reviews` 什么都查不到，"这一次是谁批的"没有答案。
* `auto` —— 每次都建单、写明命令与理由，批准人记成 `auto`。查得到，且看得出
  "不是人批的"。

所以这一档的用例重点不在"放行了没有"，而在**留痕有没有留对**。少这一条，实现
最省事的写法恰好就是把 `auto` 折成 `off`，而那会让"自动批准"变成"没有批准"。

## 两条路径都要测

原生轮（`chat._review_only`，生成器，界面要看见两帧）与 DSH 桥
（`chat.review_platform_call`，同步函数）是同一个判定、两种等待形状。只测一条的话，
另一条会安静地退回"继续等人"，症状是无人值守时那一轮永远不动——而那正是这一档
唯一的用途。

`_review_only` 在 `ask` 档下会**真的等待**（默认 300 秒），所以这里不跑该分支：
用 `harness.needs_review` 断言"ask 仍然要人批"即可，需要验证"不阻塞"的只有 auto。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import approval, chat, harness                        # noqa: E402
from tests import support                                          # noqa: E402


class _Plane(object):
    """`approval.submit`/`auto_approve` 只用到 `plane.store`。

    真起一个控制面来测"自动批准"会把"存储写对没有"和"HTTP 路由对不对"混在一起；
    路由另有下面的服务端用例。这里给最小的平面对象，读起来也更清楚它依赖什么。
    """

    def __init__(self, store):
        self.store = store


def _drain(generator):
    """把生成器跑到结束，返回 `(yield 出来的事件列表, 返回值)`。"""
    events = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stop:
            return events, stop.value


class AutoModeCatalogTestCase(unittest.TestCase):
    """档位常量与判定：`auto` 的审查面与 `strict` 相同，等待面与 `off` 相同。"""

    def test_auto_is_a_first_class_mode(self):
        self.assertIn("auto", harness.MODES)
        self.assertEqual(harness.resolve_mode("auto"), "auto")
        # 默认档位不能因为加了第四档就漂走。
        self.assertEqual(harness.DEFAULT_MODE, "ask")

    def test_auto_approved_matches_only_auto(self):
        self.assertTrue(harness.auto_approved("auto"))
        for other in ("", "off", "ask", "strict", "nope"):
            self.assertFalse(harness.auto_approved(other), other)
        # 大小写与空白是容错的（`resolve_mode` 本来就 `strip().lower()`）：
        # 设置接口已经先归一过，这里再拒一次只会让"手改数据库"变成没人认识的档位。
        self.assertTrue(harness.auto_approved("AUTO "))


    def test_auto_reviews_the_same_actions_as_strict(self):
        for name in ("run_command",) + tuple(harness.REVIEW_STRICT):
            self.assertTrue(harness.needs_review(name, "auto"), name)
        self.assertEqual(harness.reviewed_tools("auto"),
                         harness.reviewed_tools("strict"))

    def test_ask_still_needs_a_human_for_commands(self):
        # 加 auto 不许把 ask 顺手放宽：这三条是 ask 的语义本身。
        self.assertTrue(harness.needs_review("run_command", "ask"))
        self.assertFalse(harness.needs_review("run_tool", "ask"))
        self.assertFalse(harness.needs_review("run_command", "off"))

    def test_the_catalog_offers_auto_with_an_honest_summary(self):
        modes = {item["id"]: item for item in
                 harness.catalog("auto")["review"]["modes"]}
        self.assertIn("auto", modes)
        self.assertIn("自动批准", modes["auto"]["label"])
        # 摘要必须点明"留痕"——这是它与 off 唯一的分界，界面上就靠这句话区分。
        self.assertIn("审计", modes["auto"]["summary"])

    def test_the_system_prompt_tells_the_agent_nobody_will_answer(self):
        block = harness.prompt_block("auto")
        self.assertIn("auto", block)
        self.assertIn("没有人", block)
        self.assertIn("批准", block)
        # 其余三档的措辞不许被这段改写带走。
        self.assertNotIn("自动批准", harness.prompt_block("ask"))


class AutoApproveRecordTestCase(unittest.TestCase):
    """`approval.auto_approve`：落库为 `approved`，批准人是 `auto`，且留一句话。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("review-auto.db")
        self.plane = _Plane(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def _submit(self, tool="run_command"):
        return approval.submit(
            self.plane, tool=tool, tier="always", headline="要跑一条命令",
            reasons=["测试理由"], facts={"command": "id"}, arguments={"command": "id"},
            session_id="", engagement_id="", requested_by="")

    def test_auto_approve_settles_as_approved_by_the_platform(self):
        row = self._submit()
        settled = approval.auto_approve(self.plane, row)
        self.assertEqual(settled["status"], "approved")
        self.assertFalse(settled["pending"])
        self.assertEqual(settled["decidedBy"], harness.AUTO_APPROVER)
        self.assertIn("自动批准", settled["note"])
        stored = self.store.get_approval(row["id"])
        self.assertEqual(stored["status"], "approved")
        self.assertEqual(stored["decidedBy"], "auto")

    def test_the_ticket_is_still_there_for_the_audit(self):
        row = self._submit()
        approval.auto_approve(self.plane, row)
        listed = self.store.list_approvals(session_id="")
        self.assertEqual([item["id"] for item in listed], [row["id"]])
        self.assertEqual(listed[0]["tool"], "run_command")

    def test_a_vanished_ticket_is_not_reported_as_approved(self):
        """单子凭空没了（被清理、被删库）时不许假装批过。"""
        settled = approval.auto_approve(self.plane, {"id": "R-does-not-exist"})
        self.assertEqual(settled["status"], "expired")
        self.assertNotEqual(settled["status"], "approved")


class PlatformCallTestCase(unittest.TestCase):
    """DSH 桥那条同步路径：auto 直接放行，且放行的单子写着批准人。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("review-call.db")
        self.plane = _Plane(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_auto_allows_a_command_without_waiting(self):
        result = chat.review_platform_call(
            self.plane, "run_command", {"command": "id"}, review_mode="auto")
        self.assertTrue(result["required"])
        self.assertTrue(result["allowed"])
        self.assertEqual(result["review"]["status"], "approved")
        self.assertEqual(result["review"]["decidedBy"], "auto")
        # 放行时不给 outcome：调用方自己去执行，那才是结果。
        self.assertIsNone(result["outcome"])

    def test_auto_does_not_record_the_expired_excuse(self):
        """不许复用 `expired` 那条"无人裁决即放行"——审计结论正好相反。"""
        result = chat.review_platform_call(
            self.plane, "run_command", {"command": "id"}, review_mode="auto")
        self.assertNotEqual(result["review"]["status"], "expired")
        self.assertNotIn("无人裁决", result["review"]["note"])

    def test_tools_that_need_no_review_stay_free(self):
        # 用一张两边都不认识、且不在 REVIEW_ALWAYS/REVIEW_STRICT 里的卡：
        # auto 抬高的是"要人点头"的范围（同 strict），不是"所有调用都要建单"。
        result = chat.review_platform_call(
            self.plane, "read_workspace_file", {}, review_mode="auto")
        self.assertFalse(result["required"])
        self.assertTrue(result["allowed"])
        self.assertIsNone(result["review"])


class ReviewOnlyGeneratorTestCase(unittest.TestCase):
    """原生轮那条生成器路径：两帧事件，中间不等人。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("review-only.db")
        self.plane = _Plane(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_the_frames_are_emitted_and_the_call_is_approved(self):
        events, value = _drain(chat._review_only(
            self.plane, "run_command", {"command": "id"}, "", {}, "auto"))
        self.assertEqual([item["phase"] for item in events],
                         ["pending", "settled"])
        self.assertEqual([item["type"] for item in events], ["review", "review"])
        # pending 帧必须写明"没有人在等"：界面据此不画批准按钮，而不是画一个
        # 点了也没用的按钮。
        self.assertFalse(events[0]["review"]["awaiting"])
        self.assertEqual(events[1]["review"]["status"], "approved")
        approved, settled = value
        self.assertTrue(approved)
        self.assertEqual(settled["decidedBy"], "auto")


class ServerRouteTestCase(unittest.TestCase):
    """接口：`GET /harness` 里有这一档，`POST /harness/review` 能切、能拒。"""

    @classmethod
    def setUpClass(cls):
        cls.directory, cls.store = support.temporary_store("review-route.db")
        cls.httpd, cls.client = support.start(cls.store)

    @classmethod
    def tearDownClass(cls):
        support.stop(cls.httpd, cls.store, cls.directory)

    def test_the_catalog_lists_auto(self):
        status, payload = self.client.get("/api/v1/harness")
        self.assertEqual(status, 200)
        ids = [item["id"] for item in payload["review"]["modes"]]
        self.assertEqual(ids, ["off", "ask", "strict", "auto"])
        self.assertIn("auto", ids)

    def test_auto_can_be_selected_and_reported_back(self):
        status, payload = self.client.post("/api/v1/harness/review",
                                           {"mode": "auto"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["review"]["mode"], "auto")
        # 读回路：下一次 GET 必须仍然说 auto（写进设置才算生效）。
        status, again = self.client.get("/api/v1/harness")
        self.assertEqual(again["review"]["mode"], "auto")
        # 收尾切回默认档，别把状态漏给同库的其它用例。
        self.client.post("/api/v1/harness/review", {"mode": "ask"})

    def test_an_unknown_mode_is_rejected(self):
        status, payload = self.client.post("/api/v1/harness/review",
                                           {"mode": "yolo"})
        self.assertEqual(status, 400)
        self.assertIn("未知", payload["error"])

    def test_a_non_admin_cannot_change_it(self):
        status, _ = self.client.post("/api/v1/users", {
            "username": "operator-review", "password": "Operator-Pass-1!",
            "role": "operator"})
        self.assertIn(status, (200, 201))
        base = self.client.base
        from tests.support import Client
        operator = Client(base)
        status, _ = operator.login("operator-review", "Operator-Pass-1!")
        self.assertEqual(status, 200)
        status, payload = operator.post("/api/v1/harness/review", {"mode": "auto"})
        self.assertEqual(status, 403)
        self.assertIn("管理员", payload["error"])


if __name__ == "__main__":
    unittest.main()
