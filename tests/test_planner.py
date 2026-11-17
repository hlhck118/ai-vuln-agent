# -*- coding: utf-8 -*-
"""任务规划：把一句意图拆成**待审的提案**。

这一层最容易在两种方向上走歪，用例因此分两组：

* **别让它变成"模型直接下发"**。规划只读状态、不建任务、不建 engagement；
  下发仍走 `create_job`，一条不差地过同一道门禁。`test_plan_never_creates_*`
  与 `test_commit_...` 两组断言守的就是这条线——少了它，"模型判定该打谁"
  和"模型真的打了谁"之间就没有任何东西挡着。
* **别让它悄悄丢掉东西**。越界的、目标不在原话里的条目都要**原样列出来**并附上
  原因。静默丢弃会让"我明明说了要扫 X，计划里却没有 X"变成一个无解的疑问，
  而这恰好是使用者最需要答案的时刻。

还有一条不那么显眼但同样重要的：模型失败时**不降级**。降级产出与模型产出形态
相似、来源不同，悄悄换掉会让人以为那几条是模型拆的。
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import planner, scope                                # noqa: E402
from tests import support                                          # noqa: E402

WEB = os.path.join(ROOT, "web")
MODEL = {"name": "测试模型", "provider": "openai", "baseUrl": "http://127.0.0.1:1",
         "model": "test-model", "apiKey": "sk-test", "temperature": 0.2,
         "maxTokens": 512, "timeoutS": 5}


class FakePlane:
    """只带规划需要的那几样：作用域，外加可选的 store 与 agent_mode。

    刻意做成最小对象而不是真控制面：规划层**不该**依赖比这更多的东西。一旦它
    开始需要别的，这些用例会立刻失败，而不是让新依赖悄悄长出来。
    """

    def __init__(self, scopes=None, store=None):
        self.probe_scope = list(scope.ANY_SCOPE if scopes is None else scopes)
        self.store = store

    def agent_mode(self):
        return None


def reply(text: str) -> dict:
    return {"ok": True, "text": text, "toolCalls": [], "message": "",
            "latencyMs": 1}


def model_plan(payload: dict) -> dict:
    """把一段（可能是 Python dict 的）模型输出包成 chat_once 的返回值。"""
    text = payload if isinstance(payload, str) else json.dumps(payload,
                                                              ensure_ascii=False)
    return {"ok": True, "text": text, "toolCalls": [], "message": "",
            "latencyMs": 1}


def one_task(**overrides) -> dict:
    task = {"kind": "probe", "target": "10.1.2.0/24", "ports": "common",
            "objective": "内网暴露面测绘", "reason": "用户要求了解该网段",
            "evidence": "扫一下 10.1.2.0/24"}
    task.update(overrides)
    return task


# ---------------------------------------------------------------- 解析与反幻觉

class ProposalParsingTestCase(unittest.TestCase):
    """解析层：JSON 定位、字段白名单，以及"目标必须来自原话"。"""

    def test_json_slice_ignores_fence_and_small_talk(self):
        raw = '好的，以下是计划：\n```json\n{"tasks": [{"kind": "probe"}]}\n```\n以上。'
        self.assertEqual(planner._json_slice(raw),
                         '{"tasks": [{"kind": "probe"}]}')

    def test_json_slice_handles_braces_inside_strings(self):
        """字符串里的花括号不能参与配对——否则截出来的片段根本没法解析。"""
        raw = '{"reason": "目标里有 { 这种字符", "tasks": []}'
        self.assertEqual(planner._json_slice(raw), raw)

    def test_json_slice_gives_up_on_truncated_output(self):
        """找不到配对就返回空串。规划结果直接决定下发什么，"猜它的意思"是危险的。"""
        self.assertEqual(planner._json_slice('{"tasks": [{"kind"'), "")

    def test_target_absent_from_intent_is_rejected_not_dropped(self):
        """最危险的失败模式：模型顺手补全出一个地址，而它恰好落在作用域内。"""
        intent = "扫一下 10.1.2.0/24 这个网段"
        rejected = []
        task = planner._normalize_task(
            one_task(target="192.168.9.9", evidence="192.168.9.9"), 1, intent,
            rejected)
        self.assertIsNone(task)
        self.assertEqual(len(rejected), 1)
        self.assertIn("192.168.9.9", rejected[0]["reason"])
        # 拒绝理由要能回答"我该怎么做"，而不是只说一句"不行"。
        self.assertIn("写进意图", rejected[0]["reason"])

    def test_target_verbatim_in_intent_passes(self):
        rejected = []
        task = planner._normalize_task(one_task(), 1, "扫一下 10.1.2.0/24", rejected)
        self.assertIsNotNone(task)
        self.assertEqual(rejected, [])
        self.assertEqual(task["target"], "10.1.2.0/24")

    def test_target_matches_after_whitespace_normalisation(self):
        """用户把地址写得很松（带空格）时不该误杀：归一化后仍算原文片段。"""
        rejected = []
        task = planner._normalize_task(one_task(target="10.1.2.0/24"), 1,
                                       "扫一下 10.1.2.0 / 24 网段", rejected)
        self.assertIsNotNone(task)

    def test_normalised_target_is_rejected_and_explained(self):
        """误杀是有意的：规范化过的目标不再逐字可查，而它可见、可解释、改一下就能过。"""
        rejected = []
        task = planner._normalize_task(
            one_task(target="192.168.1.0/24"),
            1, "扫一下 192.168.1.5 整个 C 段", rejected)
        self.assertIsNone(task)
        self.assertIn("192.168.1.0/24", rejected[0]["reason"])

    def test_unknown_kind_is_rejected(self):
        rejected = []
        task = planner._normalize_task(one_task(kind="exploit"), 1,
                                       "扫一下 10.1.2.0/24", rejected)
        self.assertIsNone(task)
        self.assertIn("scan", rejected[0]["reason"])

    def test_missing_target_is_rejected(self):
        rejected = []
        task = planner._normalize_task({"kind": "probe", "target": "  "}, 1,
                                       "扫一下 10.1.2.0/24", rejected)
        self.assertIsNone(task)
        self.assertEqual(rejected[0]["reason"], "缺少 target")

    def test_ungrounded_evidence_is_cleared_but_task_survives(self):
        """依据只用于展示。它不成立就清空——绝不让界面引用一句用户没说过的话。"""
        rejected = []
        task = planner._normalize_task(
            one_task(evidence="用户要求全端口扫描"), 1, "扫一下 10.1.2.0/24", rejected)
        self.assertIsNotNone(task)
        self.assertEqual(task["evidence"], "")
        self.assertEqual(rejected, [])

    def test_scan_only_keeps_patched_and_incremental(self):
        """probe 条目上的 patched / incremental 是无意义的，直接丢掉而不是留在记录里。"""
        rejected = []
        task = planner._normalize_task(
            one_task(kind="probe", patched="/tmp/fix", incremental=True), 1,
            "扫一下 10.1.2.0/24", rejected)
        self.assertEqual(task["patched"], "")
        self.assertFalse(task["incremental"])


# ---------------------------------------------------------------- 门禁复核

class GateTestCase(unittest.TestCase):
    """复核层：判定权威是 `scope.plan`，且被拒的条目**不被丢弃**。"""

    def test_out_of_scope_task_is_listed_and_blocked(self):
        plane = FakePlane(scopes=["10.0.0.0/8"])
        rejected = []
        task = planner._normalize_task(one_task(target="8.8.8.8"), 1,
                                       "看一下 8.8.8.8", rejected)
        passed, blocked = planner.review(plane, [task], True)
        self.assertEqual(passed, [])
        self.assertEqual(len(blocked), 1)
        gate = blocked[0]["gate"]
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["code"], "out_of_scope")
        self.assertIn("8.8.8.8", gate["message"])

    def test_in_scope_probe_reports_the_same_numbers_as_the_gate(self):
        plane = FakePlane(scopes=["10.0.0.0/8"])
        rejected = []
        task = planner._normalize_task(one_task(target="10.1.2.0/24", ports="80,443"),
                                       1, "扫一下 10.1.2.0/24 的 80,443", rejected)
        passed, blocked = planner.review(plane, [task], True)
        self.assertEqual(blocked, [])
        detail = passed[0]["gate"]["detail"]
        self.assertEqual(detail["hosts"], 254)
        self.assertEqual(detail["ports"], 2)
        self.assertEqual(detail["probes"], 508)
        # 与 create_job 用的是同一个压缩函数：提案里的端口表达式必须和实际下发的一致。
        self.assertEqual(detail["portsSpec"], "80,443")

    def test_scope_disabled_blocks_every_probe(self):
        plane = FakePlane(scopes=[])
        rejected = []
        task = planner._normalize_task(one_task(), 1, "扫一下 10.1.2.0/24", rejected)
        _passed, blocked = planner.review(plane, [task], True)
        self.assertEqual(blocked[0]["gate"]["code"], "scope_disabled")

    def test_over_budget_is_classified(self):
        """连接预算挡的是"地址数 × 端口数"，而不是地址数本身。

        `/16` 会先被主机数上限（256）拦下，测到的是另一条规则。这里用 `/29`：
        6 个地址 × 65535 个端口 = 39 万次连接，正好越过 26 万的预算。
        """
        plane = FakePlane(scopes=["10.0.0.0/8"])
        rejected = []
        task = planner._normalize_task(one_task(target="10.0.0.0/29", ports="all"),
                                       1, "扫 10.0.0.0/29 全端口", rejected)
        _passed, blocked = planner.review(plane, [task], True)
        self.assertEqual(blocked[0]["gate"]["code"], "over_budget")

    def test_scan_needs_a_model(self):
        """与 create_job 的闸门对齐：源码审计配不上模型就不下发。"""
        plane = FakePlane()
        rejected = []
        task = planner._normalize_task(one_task(kind="scan", target="/srv/app"),
                                       1, "审计 /srv/app", rejected)
        _passed, blocked = planner.review(plane, [task], False)
        self.assertEqual(blocked[0]["gate"]["code"], "needs_model")
        self.assertIn("网络探测不需要模型", blocked[0]["gate"]["message"])

    def test_scan_path_shape_is_checked(self):
        plane = FakePlane()
        rejected = []
        task = planner._normalize_task(one_task(kind="scan", target="srvapp"),
                                       1, "审计 srvapp", rejected)
        _passed, blocked = planner.review(plane, [task], True)
        self.assertEqual(blocked[0]["gate"]["code"], "bad_target")


# ---------------------------------------------------------------- 降级拆分

class FallbackTestCase(unittest.TestCase):
    """没有模型时的确定性拆分。规则要直白到可以口算。"""

    def plan(self, intent: str, scopes=None):
        return planner.plan(FakePlane(scopes=scopes), None, "", intent)

    def test_address_gets_a_probe_and_a_path_gets_a_scan(self):
        result = self.plan("先看 /srv/app 的登录逻辑，再看 10.1.2.0/24 这个网段")
        kinds = sorted(task["kind"] for task in result["tasks"])
        self.assertEqual(kinds, ["probe", "scan"])
        self.assertEqual(result["source"], "fallback")

    def test_cidr_suffix_is_not_mistaken_for_a_path(self):
        """`192.168.1.0/24` 尾部的 `/24` 很容易被读成一条路径——先把地址整段剔除再找路径。"""
        result = self.plan("扫一下 192.168.1.0/24")
        self.assertEqual([task["kind"] for task in result["tasks"]], ["probe"])
        self.assertEqual([task["target"] for task in result["tasks"]],
                         ["192.168.1.0/24"])

    def test_all_port_hint_reaches_the_proposal(self):
        result = self.plan("对 10.1.2.5 做全端口扫描")
        self.assertEqual(result["tasks"][0]["ports"], "all")

    def test_windows_path_is_recognised(self):
        result = self.plan(r"审计 D:\code\service 的权限校验")
        self.assertEqual(result["tasks"][0]["kind"], "scan")
        self.assertEqual(result["tasks"][0]["target"], r"D:\code\service")

    def test_no_target_asks_for_more_instead_of_inventing_one(self):
        result = self.plan("帮我看看有没有问题")
        self.assertEqual(result["tasks"], [])
        self.assertTrue(result["needMore"])
        self.assertIn("192.168.1.0/24", result["needMore"])

    def test_fallback_is_labelled_and_explained(self):
        result = self.plan("扫一下 10.1.2.0/24")
        self.assertEqual(result["source"], "fallback")
        self.assertTrue(any("未启用模型" in text for text in result["warnings"]))

    def test_intent_is_required_and_bounded(self):
        with self.assertRaises(ValueError):
            self.plan("   ")
        with self.assertRaises(ValueError):
            self.plan("扫 " + "x" * (planner.INTENT_LIMIT + 1))


# ---------------------------------------------------------------- 模型路径

class ModelProposalTestCase(unittest.TestCase):

    def test_model_output_drives_the_proposal(self):
        payload = {"summary": "测绘内网办公段", "needMore": "",
                   "tasks": [one_task()]}
        with mock.patch.object(planner, "chat_once",
                               return_value=model_plan(payload)) as called:
            result = planner.plan(FakePlane(scopes=["10.0.0.0/8"]), MODEL, "sk",
                                  "扫一下 10.1.2.0/24")
        self.assertTrue(called.called)
        self.assertEqual(result["source"], "model")
        self.assertEqual(result["summary"], "测绘内网办公段")
        self.assertEqual(len(result["tasks"]), 1)
        self.assertTrue(result["tasks"][0]["gate"]["ok"])
        self.assertEqual(result["model"]["name"], "测试模型")

    def test_model_failure_does_not_silently_fall_back(self):
        """失败就是失败。降级产出和模型产出形态相似、来源不同，混起来没人分得清。"""
        failure = {"ok": False, "message": "连接被拒绝", "text": "",
                   "toolCalls": [], "latencyMs": 1}
        with mock.patch.object(planner, "chat_once", return_value=failure):
            result = planner.plan(FakePlane(), MODEL, "sk", "扫一下 10.1.2.0/24")
        self.assertFalse(result["ok"])
        self.assertEqual(result["source"], "model")
        self.assertEqual(result["tasks"], [])
        self.assertIn("连接被拒绝", result["message"])

    def test_non_json_reply_is_an_error_not_a_guess(self):
        with mock.patch.object(planner, "chat_once",
                               return_value=reply("我觉得应该扫 10.1.2.0/24")):
            result = planner.plan(FakePlane(), MODEL, "sk", "扫一下 10.1.2.0/24")
        self.assertFalse(result["ok"])
        self.assertIn("JSON", result["message"])

    def test_raw_output_is_kept_for_audit(self):
        payload = {"summary": "s", "tasks": [one_task()]}
        with mock.patch.object(planner, "chat_once",
                               return_value=model_plan(payload)):
            result = planner.plan(FakePlane(scopes=["10.0.0.0/8"]), MODEL, "sk",
                                  "扫一下 10.1.2.0/24")
        # 只存解析后的条目，就答不了"模型当时到底说了什么"。
        self.assertIn("10.1.2.0/24", result["rawOutput"])

    def test_proposals_beyond_the_limit_are_truncated_and_reported(self):
        addresses = ["10.0.0.%d" % index for index in range(1, 16)]
        intent = "扫一下 " + " ".join(addresses)
        payload = {"summary": "s",
                   "tasks": [one_task(target=address, ports="common",
                                      evidence=address) for address in addresses]}
        with mock.patch.object(planner, "chat_once",
                               return_value=model_plan(payload)):
            result = planner.plan(FakePlane(scopes=["10.0.0.0/8"]), MODEL, "sk",
                                  intent)
        self.assertEqual(len(result["tasks"]), planner.MAX_PLAN_TASKS)
        self.assertTrue(any("截断" in text for text in result["warnings"]))

    def test_rejected_and_blocked_are_both_reported(self):
        payload = {"summary": "s", "tasks": [
            one_task(),
            one_task(target="172.16.5.5", evidence="172.16.5.5"),   # 不在原话里
        ]}
        with mock.patch.object(planner, "chat_once",
                               return_value=model_plan(payload)):
            result = planner.plan(FakePlane(scopes=["10.0.0.0/8"]), MODEL, "sk",
                                  "扫一下 10.1.2.0/24")
        self.assertEqual(result["counts"]["rejected"], 1)
        self.assertEqual(result["counts"]["tasks"], 1)
        self.assertTrue(result["rejected"])


# ---------------------------------------------------------------- 不变量

class NoSideEffectTestCase(unittest.TestCase):
    """**规划不产生任何动作。** 这是这一层存在的全部理由。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("planner-side.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)

    def test_plan_never_creates_jobs_or_engagements(self):
        plane = FakePlane(scopes=["10.0.0.0/8"], store=self.store)
        before = (len(self.store.jobs()), len(self.store.list_engagements()))
        result = planner.plan(plane, None, "", "扫一下 10.1.2.0/24，再审计 /srv/app")
        self.assertTrue(result["ok"])
        self.assertTrue(result["tasks"])
        after = (len(self.store.jobs()), len(self.store.list_engagements()))
        self.assertEqual(before, after,
                         "规划提案不得建任务或 engagement——下发只有 create_job 一条路径")


# ---------------------------------------------------------------- API

class PlannerApiTestCase(unittest.TestCase):

    def setUp(self):
        self.directory, self.store = support.temporary_store("planner-api.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.httpd, self.client = support.start(self.store, WEB)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def plan(self, intent):
        status, payload = self.client.post("/api/v1/jobs/plan", {"intent": intent})
        self.assertEqual(status, 200, payload)
        return payload

    # -------------------------------------------------------- 规划接口

    def test_plan_endpoint_returns_a_saved_proposal(self):
        payload = self.plan("扫一下 10.1.2.0/24")
        self.assertTrue(payload["ok"], payload)
        plan = payload["plan"]
        self.assertTrue(plan["id"].startswith("plan-"))
        self.assertEqual(plan["source"], "fallback")
        self.assertEqual(len(plan["tasks"]), 1)
        self.assertTrue(plan["tasks"][0]["gate"]["ok"])
        self.assertIsNotNone(self.store.get_plan(plan["id"]), "提案必须落库")

    def test_plan_requires_an_intent(self):
        status, payload = self.client.post("/api/v1/jobs/plan", {})
        self.assertEqual(status, 400)
        self.assertIn("意图", payload.get("error", ""))

    def test_plan_without_a_target_asks_for_more(self):
        payload = self.plan("帮我看看有没有问题")
        self.assertEqual(payload["plan"]["tasks"], [])
        self.assertTrue(payload["plan"]["needMore"])

    def test_scan_proposal_is_blocked_without_a_model(self):
        payload = self.plan("审计一下 /srv/app 的登录逻辑")
        plan = payload["plan"]
        self.assertEqual(len(plan["tasks"]), 1)
        task = plan["tasks"][0]
        self.assertEqual(task["kind"], "scan")
        self.assertFalse(task["gate"]["ok"])
        self.assertEqual(task["gate"]["code"], "needs_model")
        # 被挡下的条目照样列出来——它是"我提的那条为什么不在计划里"的答案。
        self.assertEqual(plan["counts"]["ready"], 0)
        self.assertEqual(plan["counts"]["tasks"], 1)

    def test_plan_does_not_touch_the_queue(self):
        self.plan("扫一下 10.1.2.0/24")
        self.assertEqual(self.store.jobs(), [])

    # -------------------------------------------------------- 提案列表

    def test_plan_list_hides_the_raw_model_output(self):
        self.plan("扫一下 10.1.2.0/24")
        status, payload = self.client.get("/api/v1/plans")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["plans"])
        self.assertNotIn("rawOutput", payload["plans"][0])

    def test_plan_detail_keeps_the_raw_model_output(self):
        created = self.plan("扫一下 10.1.2.0/24")["plan"]
        status, payload = self.client.get("/api/v1/plans/%s" % created["id"])
        self.assertEqual(status, 200, payload)
        self.assertIn("rawOutput", payload["plan"])

    def test_unknown_plan_detail_is_404(self):
        status, payload = self.client.get("/api/v1/plans/plan-nope")
        self.assertEqual(status, 404)
        self.assertIn("plan-nope", payload.get("error", ""))

    # -------------------------------------------------------- 采纳与关联

    def test_commit_links_the_job_back_to_the_proposal(self):
        plan = self.plan("扫一下 10.1.2.0/24")["plan"]
        task = plan["tasks"][0]
        status, payload = self.client.post("/api/v1/jobs", {
            "kind": task["kind"], "target": task["target"], "ports": task["ports"],
            "planId": plan["id"], "planReason": task["reason"],
            "planEvidence": task["evidence"]})
        self.assertEqual(status, 201, payload)
        job = self.store.get_job(payload["jobId"])
        self.assertEqual(job["payload"]["planId"], plan["id"])
        self.assertEqual(job["payload"]["planEvidence"], task["evidence"])
        # 提案 → 动作的那条连线，事后据此回答"这批任务哪来的"。
        self.assertEqual(self.store.get_plan(plan["id"])["jobIds"], [payload["jobId"]])

    def test_commit_still_goes_through_the_scope_gate(self):
        """规划**不**放宽门禁：越界目标在采纳这一步一样被拒。"""
        plan = self.plan("扫一下 10.1.2.0/24")["plan"]
        status, payload = self.client.post("/api/v1/jobs", {
            "kind": "probe", "target": "8.8.8.8", "planId": plan["id"]})
        self.assertEqual(status, 403, payload)
        self.assertEqual(self.store.jobs(), [])

    def test_unknown_plan_id_is_rejected_before_anything_is_queued(self):
        """悬空的 planId 比没有来源更糟：它看起来可追溯，实际追溯不到。"""
        status, payload = self.client.post("/api/v1/jobs", {
            "kind": "probe", "target": "127.0.0.1", "planId": "plan-nope"})
        self.assertEqual(status, 400, payload)
        self.assertIn("plan-nope", payload.get("error", ""))
        self.assertEqual(self.store.jobs(), [])

    def test_jobs_without_a_plan_id_are_unaffected(self):
        status, payload = self.client.post(
            "/api/v1/jobs", {"kind": "probe", "target": "127.0.0.1"})
        self.assertEqual(status, 201, payload)
        job = self.store.get_job(payload["jobId"])
        self.assertNotIn("planId", job["payload"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
