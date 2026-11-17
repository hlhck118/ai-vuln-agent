# -*- coding: utf-8 -*-
"""对话工具层缺的那两道闸：终端归属、清空任务队列的角色。

## 为什么单开一份

这两处都不在 HTTP 路由上，而在 **对话工具** 里：模型拿到工具名与参数就能调，
中间的判据只有 `chat.check_tool` 一处。它们此前都只判了"这类动作允不允许"
（权限档），没判"这一条是不是你的"和"这个人是不是管理员"：

1. `terminal_read` / `terminal_kill` 按 id 走，只查记录在不在、进程是不是本
   控制面起的。同一个部署里 A 拿到 B 的 `T-xxxxxxxx` 就能读走那条命令的完整
   输出（落盘文件 + 落库前 24000 字符，里面直接有目标口令与中间产物），也能
   停掉 B 的后台扫描；`list_terminals` 又能按任意 `engagementId` 列出别人的
   id，所以"猜不到"这一层也不成立。
2. `clear_finished_jobs` 在 `policy.TOOL_LEVEL` 里是 `full`，而 `full` 是
   **默认档**；同名的 HTTP 端点 `POST /jobs/clear` 一直要求管理员。于是默认
   配置下任何 operator 都能让模型一行清掉全部署已结束的任务与执行日志 —— 与
   `server.clear_jobs` 的 docstring 直接冲突。

七条不变量，一条用例一条：别人的终端读不得、停不得；自己的照旧能读；管理员
不受这条限制；列表按人收窄；命令行（没有登录用户）不被误伤；清空任务队列
在 `full` + operator 下被拒、在 admin 下放行。
"""
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import chat                                    # noqa: E402
from pentestd import policy                                  # noqa: E402
from tests import support                                    # noqa: E402

OPERATOR = {"userId": "u-operator", "sessionId": "s-operator", "role": "operator"}
ADMIN = {"userId": "u-admin", "sessionId": "s-admin", "role": "admin"}
CLI = {"userId": "", "sessionId": "", "role": "admin", "client": "cli"}


class ToolOwnershipTestCase(unittest.TestCase):

    def setUp(self):
        self.dir, self.store = support.temporary_store("tool-own.db")
        self.plane = types.SimpleNamespace(store=self.store)
        # 归属判据比的是记录里的 `user_id`，所以夹具必须**有主**：无主的记录
        # 是命令行起的，任何会话都不是它的主人。
        self.admin_terminal = "T-aaaa1111"
        self.operator_terminal = "T-bbbb2222"
        self.store.record_terminal(
            terminal_id=self.admin_terminal, command="cat /etc/shadow",
            engagement_id="e-1", session_id="s-admin", user_id="u-admin",
            shell="bash", cwd="/srv", background=True, status="running",
            output="root:$6$secret", output_path="")
        self.store.record_terminal(
            terminal_id=self.operator_terminal, command="id",
            engagement_id="e-1", session_id="s-operator", user_id="u-operator",
            shell="bash", cwd="/srv", background=False, status="exited",
            exit_code=0, output="uid=0(root)", output_path="")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def check(self, name, args=None, session=None, role=None,
              policy_id="full"):
        who = OPERATOR if session is None else session
        denied, _args = chat.check_tool(
            self.plane, name, args or {}, "", policy_id,
            role if role is not None else who.get("role", ""), True, who)
        return denied

    # ------------------------------------------------- 读别人的终端 → 拒绝
    def test_reading_someone_elses_terminal_is_refused(self):
        """别人的终端读不得，且拒绝要说清是归属问题（不是"没有这条记录"）。

        修复前这条返回 `None`（放行），handler 随后把 `output` 整段交出去。
        """
        denied = self.check("terminal_read",
                            {"terminalId": self.admin_terminal})
        self.assertIsNotNone(denied)
        self.assertTrue(denied["data"]["denied"])
        self.assertTrue(denied["data"]["owner"])
        self.assertIn("其他用户", denied["summary"])
        self.assertNotIn("root:$6$secret", str(denied))

    def test_killing_someone_elses_terminal_is_refused(self):
        """停别人的后台终端同样拒绝，而且被拒之后那条记录**一点没动**。"""
        denied = self.check("terminal_kill",
                            {"terminalId": self.admin_terminal})
        self.assertIsNotNone(denied)
        self.assertIn("其他用户", denied["summary"])
        row = self.store.get_terminal(self.admin_terminal)
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["output"], "root:$6$secret")

    # ------------------------------------------------- 自己 / 管理员不受影响
    def test_your_own_terminal_still_works(self):
        """收紧不能把正常用法一起收掉：自己的终端照旧能读能停。"""
        self.assertIsNone(self.check("terminal_read",
                                     {"terminalId": self.operator_terminal}))

    def test_an_admin_is_not_limited_by_ownership(self):
        """管理员放行，与 `server._require_chat_access` 同一条口子。"""
        self.assertIsNone(self.check("terminal_read",
                                     {"terminalId": self.operator_terminal},
                                     session=ADMIN))

    def test_a_command_line_session_is_not_mistaken_for_someone_else(self):
        """命令行没有登录用户（`toolclient` 的 `session.userId` 是空串）。

        判据取"两侧都非空且不等"才拒：这里要是按"没有 userId 就拒"来写，
        CLI 那一路会全废。
        """
        self.assertIsNone(self.check("terminal_read",
                                     {"terminalId": self.admin_terminal},
                                     session=CLI))

    # ------------------------------------------------- 列表按人收窄
    def test_the_listing_only_shows_your_own(self):
        """列表是 id 的唯一天然来源，所以它也得按人收口（管理员看全部）。"""
        mine = self.store.list_terminals(user_id="u-operator")
        self.assertEqual([row["id"] for row in mine], [self.operator_terminal])
        self.assertEqual(len(self.store.list_terminals(user_id="u-admin")), 1)
        self.assertEqual(len(self.store.list_terminals()), 2)   # 不传就不过滤

        outcome = chat._tool_list_terminals(self.plane, {}, "", OPERATOR)
        ids = [item["terminalId"] for item in outcome["data"]["terminals"]]
        self.assertEqual(ids, [self.operator_terminal])
        outcome = chat._tool_list_terminals(self.plane, {}, "", ADMIN)
        self.assertEqual(len(outcome["data"]["terminals"]), 2)

    def test_the_listing_cannot_be_aimed_at_another_engagement(self):
        """显式传别人的 `engagementId` 也只拿到自己的 —— 归属优先于筛选条件。"""
        outcome = chat._tool_list_terminals(
            self.plane, {"engagementId": "e-1"}, "", OPERATOR)
        self.assertEqual([item["terminalId"]
                          for item in outcome["data"]["terminals"]],
                         [self.operator_terminal])


class ClearFinishedJobsGateTestCase(unittest.TestCase):
    """清空任务队列：与 `POST /jobs/clear` 同一把判据（管理员）。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("clear-gate.db")
        self.plane = types.SimpleNamespace(store=self.store)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_the_policy_puts_it_in_the_admin_set(self):
        self.assertIn("clear_finished_jobs", policy.ADMIN_TOOLS)
        self.assertTrue(policy.permits("full", "clear_finished_jobs", "admin"))
        self.assertFalse(policy.permits("full", "clear_finished_jobs",
                                        "operator"))
        self.assertIn("管理员",
                      policy.deny_reason("full", "clear_finished_jobs",
                                         "operator"))

    def test_the_model_does_not_even_see_it(self):
        """看不见比看得见但被拒更省：被拒的调用要占一次工具步数与一段上下文。"""
        offered = {tool["name"] for tool in chat.tool_catalog("full", "operator")}
        self.assertNotIn("clear_finished_jobs", offered)
        offered = {tool["name"] for tool in chat.tool_catalog("full", "admin")}
        self.assertIn("clear_finished_jobs", offered)

    def test_an_operator_gets_a_refusal_and_nothing_is_deleted(self):
        """修复前这里放行，一行就清掉全部署已结束的任务与执行日志。"""
        job = self.store.create_job("scan", "/srv/app", "e-1")
        self.store.finish_job(job["id"], status="done", result={})
        denied, _args = chat.check_tool(
            self.plane, "clear_finished_jobs", {}, "", "full", "operator", True,
            {"userId": "u-operator", "sessionId": "s-operator",
             "role": "operator"})
        self.assertIsNotNone(denied)
        self.assertIn("管理员", denied["summary"])
        self.assertEqual(len(self.store.jobs("done")), 1)

    def test_an_admin_can_still_clear(self):
        denied, _args = chat.check_tool(
            self.plane, "clear_finished_jobs", {}, "", "full", "admin", True,
            {"userId": "u-admin", "sessionId": "s-admin", "role": "admin"})
        self.assertIsNone(denied)
