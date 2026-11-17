# -*- coding: utf-8 -*-
"""审计记录与日志的删除 / 导出：四条路由背后的判断。

## 为什么单独一份

「审批记录」和「日志」两页原先只有"看"：看得到，擦不掉，也拿不走。补上删与
导出的时候，有四件**看起来能用、其实在漏**的事必须钉住，一件一条用例：

1. **导出不是列表的翻页。** 列表夹在 100 条以内是为了"一屏看得完"；导出照那个
   上限走，得到的是一份看着完整、其实只有最近一百条的文件——这种缺口事后翻的
   时候看不出来。所以导出要真的比列表多。
2. **清空的范围与列表一样窄。** 列表按归属收口，清空若按全库走，普通用户一次
   点击就删掉了别人的账；而界面上他看到的是自己那几条。
3. **还有人在等的 pending 单子不删。** 删掉之后，等它的那一轮查不到记录会一直
   轮询到上限：批也批不了、清也清不掉，整轮对话卡在那里。
4. **日志导出是整份，不是尾部。** 页面只读尾部 512 KB 是给眼睛省流量的；导出
   照尾部走，交出去的文件会缺掉最早那一段，而拿到文件的人无从知道缺了。

导出与清空都不该在没登录的情况下动到别人的东西，所以每条都同时问一遍
管理员与操作员两种身份。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import approval                                  # noqa: E402
from tests import support                                      # noqa: E402

OPERATOR = ("ut-audit-operator", "Operator-Pass1!")


class AuditCleanupTests(unittest.TestCase):
    """一套夹具：两个用户、两条对话、几沓审查单。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("audit-clean.db")
        self.httpd, self.admin = support.start(self.store, support.WEB_DIR)
        self.base = self.admin.base
        # 归属判据比的是会话的 user_id，所以夹具必须建有主的会话：
        # 不挂会话的单子（session_id 空串）不在任何人的名下，
        # 用那种单子测"只清自己的"测不出东西。
        self.admin_id = self.store.get_user_by_name(support.ADMIN_USER)["id"]
        status, payload = self.admin.post("/api/v1/users", {
            "username": OPERATOR[0], "password": OPERATOR[1],
            "role": "operator"})
        self.assertEqual(status, 201, payload)
        self.operator_id = payload["user"]["id"]
        self.operator = support.Client(self.base)
        status, _ = self.operator.login(*OPERATOR)
        self.assertEqual(status, 200)
        self.admin_session = self.store.create_chat_session(
            "管理员那条", self.admin_id)["id"]
        self.operator_session = self.store.create_chat_session(
            "操作员那条", self.operator_id)["id"]

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    # ------------------------------------------------------------ 辅助
    def review_of(self, session_id, headline="要执行一条命令"):
        return self.store.record_approval(
            session_id=session_id, tool="run_command", tier="shell",
            headline=headline, arguments={"command": "id"}, timeout_s=300)

    def log_path(self, suffix=""):
        return os.path.join(os.path.dirname(self.store.path), "access.log" + suffix)

    def write_log(self, suffix, lines):
        # newline="\n"：Windows 的文本模式会把每行写成 \r\n，读回来的行尾多一个
        # 回车，那是夹具的写法问题，不是接口的问题。
        with open(self.log_path(suffix), "w", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(line + "\n")

    # ------------------------------------------- 导出：比列表多，且同一道闸
    def test_the_export_is_not_the_listing_paged(self):
        """导出要突破列表那道 100 条的上限。

        列表夹住是为了"一屏看得完"，导出是"把这段时间的账一次取全"。照列表的
        上限导，交出去的文件看着完整、其实只有最近一百条——那种缺口事后翻的
        时候看不出来，而它正好落在"要核对更早的记录"这个场景上。
        """
        for index in range(120):
            self.review_of(self.admin_session, "第%d条" % index)
        status, listed = self.admin.get("/api/v1/reviews?limit=100")
        self.assertEqual(status, 200, listed)
        self.assertLessEqual(len(listed["reviews"]), 100,
                             "列表这边不该超过上限")

        status, payload = self.admin.get("/api/v1/reviews/export")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["total"], 120)
        self.assertEqual(len(payload["reviews"]), 120)
        # 导出的文件要能自己说清什么时候导的：一份没有时间戳的账，
        # 与后来另一次导出的文件混在一起就分不出先后。
        self.assertTrue(payload["exportedAt"])

    def test_the_export_keeps_the_same_ownership_gate(self):
        """导出与列表走同一个归属判据。

        这是最容易漏的一处：列表上看得见的判定补上了，导出照着 store 直接
        全量捞——于是"看不见"与"拿不到"分离，而拿到的是一份完整文件。
        """
        mine = self.review_of(self.admin_session)
        theirs = self.review_of(self.operator_session)

        status, payload = self.operator.get("/api/v1/reviews/export")
        self.assertEqual(status, 200, payload)
        ids = [item["id"] for item in payload["reviews"]]
        self.assertIn(theirs["id"], ids, "自己的单子要导得出来")
        self.assertNotIn(mine["id"], ids, "别人的单子不该出现在导出文件里")

        status, payload = self.operator.get(
            "/api/v1/reviews/export?sessionId=%s" % self.admin_session)
        self.assertEqual(status, 403, payload)
        self.assertIn("其他用户", payload["error"])

        # 管理员保持全库视图：这是显式身份带来的，不是默认值。
        status, payload = self.admin.get("/api/v1/reviews/export")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["total"], 2)

    # ------------------------------------------- 清空：范围与列表一样窄
    def test_clearing_a_non_admin_only_takes_their_own(self):
        """普通用户清空只清自己名下会话的单子。

        列表按归属收口，清空若走全库，一次点击就删掉别人的账——而屏幕上
        他看到的正好是自己那几条，"确定清空这 N 条"读起来毫无异样。
        """
        mine = self.review_of(self.admin_session)
        theirs = self.review_of(self.operator_session)

        status, payload = self.operator.post("/api/v1/reviews/clear", {})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["deleted"], 1)
        self.assertTrue(payload["ok"])
        self.assertIsNone(self.store.get_approval(theirs["id"]), "自己的没删掉")
        self.assertIsNotNone(self.store.get_approval(mine["id"]),
                             "别人的被清掉了")

        status, payload = self.admin.post("/api/v1/reviews/clear", {})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["deleted"], 1)
        self.assertIsNone(self.store.get_approval(mine["id"]))
        self.assertEqual(self.store.list_approvals(), [])

    def test_a_review_someone_is_waiting_on_is_not_cleared(self):
        """还有人在等的 pending 单子不删，删它单条也回 409。

        删掉之后，等它的那一轮查不到记录会一直轮到上限，症状是
        "批也批不了、清也清不掉，整轮对话卡在那里"——比多留一张卡片
        糟得多。它会自己超时收掉，所以"这次没删掉"不是漏洞。
        """
        waited = self.review_of(self.operator_session, "正在等的那条")
        idle = self.review_of(self.operator_session, "没人等的那条")

        with approval.waiting(waited["id"]):
            status, payload = self.operator.post("/api/v1/reviews/clear", {})
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["awaiting"], 1, "要说清几条没删")
            self.assertIsNotNone(self.store.get_approval(waited["id"]),
                                 "还在等的单子被删了")
            self.assertIsNone(self.store.get_approval(idle["id"]),
                              "没人等的那条也该删掉")

            status, payload = self.operator.post(
                "/api/v1/reviews/delete", {"reviewId": waited["id"]})
            self.assertEqual(status, 409, payload)
            self.assertIn("还在等", payload["error"])

    # ------------------------------------------------------------ 删除单条
    def test_deleting_one_names_what_was_deleted(self):
        """删单条：回的是那条记录本身，不是一句"已删除"。

        "删的是已批准的那条命令"与"删的是一条从没人看过的超时单"，事后分不出来
        就不是审计。所以接口把删掉的原样回给界面。
        """
        row = self.review_of(self.operator_session, "待删那条")
        status, payload = self.operator.post("/api/v1/reviews/delete",
                                             {"reviewId": row["id"]})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["deleted"], 1)
        self.assertEqual(payload["review"]["id"], row["id"])
        self.assertEqual(payload["review"]["headline"], "待删那条")
        self.assertIsNone(self.store.get_approval(row["id"]))

        status, payload = self.operator.post("/api/v1/reviews/delete",
                                             {"reviewId": row["id"]})
        self.assertEqual(status, 404, payload)

    def test_deleting_someone_elses_review_is_refused(self):
        row = self.review_of(self.admin_session)
        status, payload = self.operator.post("/api/v1/reviews/delete",
                                             {"reviewId": row["id"]})
        self.assertEqual(status, 403, payload)
        self.assertIn("其他用户", payload["error"])
        self.assertIsNotNone(self.store.get_approval(row["id"]),
                             "被拒的请求不能留下副作用")

    # ------------------------------------------------------------ 日志
    def test_the_log_export_is_the_whole_file(self):
        """导出的是整份（含滚动出来的 .1），不是这一页尾部那一段。

        页面只读尾部 512 KB 是给眼睛省流量的。照尾部导出，交出去的文件会缺掉
        最早那一段，而拿到文件的人看不出缺了——他手里没有任何东西提示总行数。
        """
        self.write_log(".1", ["OLD-%03d" % index for index in range(3)])
        self.write_log("", ["NEW-%03d" % index for index in range(2)])

        status, payload = self.admin.get("/api/v1/logs?limit=2")
        self.assertEqual(status, 200, payload)
        self.assertLessEqual(len(payload["lines"]), 2)

        status, payload = self.admin.get("/api/v1/logs/export")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["total"], 5, "已滚动的那份也要在")
        self.assertIn("OLD-000", payload["lines"])
        self.assertIn("NEW-001", payload["lines"])
        self.assertGreater(payload["sizeBytes"], 0)

    def test_the_log_export_and_clear_are_admin_only(self):
        self.write_log("", ["LINE-1"])
        status, payload = self.operator.get("/api/v1/logs/export")
        self.assertEqual(status, 403, payload)
        status, payload = self.operator.post("/api/v1/logs/clear", {})
        self.assertEqual(status, 403, payload)
        self.assertTrue(os.path.exists(self.log_path()),
                        "被拒的清空不能删文件")

    def test_clearing_the_log_takes_the_rolled_file_and_leaves_a_note(self):
        """清空连同 .1 一起删，并在原处留一行说明。

        一份"干净得像没跑过"的日志比脏的更容易误导人，而"谁在什么时候清过
        日志"正是审计要回答的问题之一。所以清完文件还在，只是只剩那一行。
        """
        self.write_log(".1", ["ROLLED"])
        self.write_log("", ["CURRENT"])

        status, payload = self.admin.post("/api/v1/logs/clear", {})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertGreater(payload["removed"], 0)
        self.assertFalse(os.path.exists(self.log_path(".1")),
                         "滚动出来的那份没删")
        self.assertTrue(os.path.exists(self.log_path()),
                        "清完要留一行说明")
        with open(self.log_path(), "r", encoding="utf-8") as handle:
            left = handle.read()
        self.assertIn("清空", left)
        self.assertNotIn("CURRENT", left, "旧内容还在")


if __name__ == "__main__":
    unittest.main()
