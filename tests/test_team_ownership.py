# -*- coding: utf-8 -*-
"""智能体团队的归属：任务板按**会话**收口，不是按"知不知道 id"。

这一份单开的原因与 `test_review_ownership.py` 同一条：两处缺陷（第十处 `plan_jobs`
的 `insecure`、第十一处 `dsh/approval`）都在"拿得到 id 就能动"这一形态上，而
它们各自是不同模块的**同一行守则**漏写。团队的这一处漏得比它们更宽 —— 四个
写入口全部不核归属，其中 `team_done` 能把别人正在跑的任务标成失败。

## 这里的判据是"团队属于建它的那条会话"

`teams` 表有 `session_id`（`store.py` 的建表语句里就有），`create_team` 一直
在写它，但**读的那一侧没有一处用过**。于是下面这条链路是通的：

    A 会话 team_status → 拿到 T-xxxxxxxx（它就在返回体的 team.id 里）
    B 会话 team_status {"teamId": "T-xxxxxxxx"} → 读到 A 那批任务的 summary/evidence
    B 会话 team_add_task {"teamId": 同上}       → 往 A 的板上加活
    B 会话 team_done {"taskId": <A 的任务>}     → 把 A 的任务标成失败

四个口子共用一个 `taskId` / `teamId` 参数，而这条参数由模型提供 —— 模型能把
工具结果里的任何字符串填回来。所以"猜不到"从来不是一道闸：那张 id 就摆在 A 自己
的对话记录里，而对话记录是可以被复制、被分享、被贴进另一轮对话的。

## 三条判据都钉住，包括"故意不挡"的那两条

管理员放行、两侧任一为空放行（命令行没有登录用户 / 早期记录没有 session_id）。
后两条是**故意的**，写成断言是为了让下一次改动必须显式地推翻它们 —— 只钉"会拒"
的测试，会让人把"判不出来也拒"当成顺手更安全的方向，而那会把"这条记录是旧的"
表现成"你没有权限"。
"""
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests import support                                             # noqa: E402
from pentestd import team                                             # noqa: E402

ALICE = {"sessionId": "S-alice", "userId": "u-alice", "role": "operator"}
BOB = {"sessionId": "S-bob", "userId": "u-bob", "role": "operator"}
ADMIN = {"sessionId": "S-admin", "userId": "u-admin", "role": "admin"}
CLI = {"sessionId": "", "userId": "", "role": "admin", "client": "cli"}


class TeamOwnershipTestCase(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("team-own.db")
        self.plane = types.SimpleNamespace(store=self.store)
        self.team = self.store.create_team(session_id="S-alice",
                                           engagement_id="e-1",
                                           name="Alice 的队", goal="扫 A 段")
        self.store.add_team_member(self.team["id"], "侦察", role="侦察员")
        self.first = self.store.add_team_task(self.team["id"], "扫 10.0.0.1",
                                              description="nmap", assignee="侦察")

    def tearDown(self):
        # 这一份不起 HTTP 服务（判据在 `team.py`，不在路由上），所以不走
        # `support.stop` —— 它第一句就是 `httpd.shutdown()`。
        self.store.close()
        self.directory.cleanup()

    # -------------------------------------------------------------- 读
    def test_another_session_cannot_read_the_board(self):
        with self.assertRaises(team.TeamError) as caught:
            team.status(self.plane, {"teamId": self.team["id"]}, BOB)
        self.assertIn("其他会话", str(caught.exception))

    def test_another_session_cannot_add_a_task(self):
        with self.assertRaises(team.TeamError):
            team.add_task(self.plane, {"teamId": self.team["id"],
                                       "subject": "Bob 插进来的活"}, BOB)
        self.assertEqual(len(self.store.list_team_tasks(self.team["id"])), 1)

    def test_another_session_cannot_add_a_member(self):
        with self.assertRaises(team.TeamError):
            team.add_member(self.plane, {"teamId": self.team["id"],
                                         "name": "Bob 的探子"}, BOB)
        self.assertEqual(len(self.store.list_team_members(self.team["id"])), 1)

    # -------------------------------------------------------------- 写
    def test_another_session_cannot_change_a_task(self):
        with self.assertRaises(team.TeamError):
            team.update(self.plane, {"taskId": self.first["id"],
                                     "subject": "改掉"}, BOB)
        fresh = self.store.get_team_task(self.first["id"])
        self.assertEqual(fresh["subject"], "扫 10.0.0.1")

    def test_another_session_cannot_mark_a_task_done(self):
        """这条比读更重：被标成失败的任务**永远不会就绪**，下游会一直等。"""
        with self.assertRaises(team.TeamError):
            team.done(self.plane, {"taskId": self.first["id"], "ok": False,
                                   "error": "Bob 说没做完"}, BOB)
        fresh = self.store.get_team_task(self.first["id"])
        self.assertEqual(fresh["status"], "pending")
        self.assertEqual(fresh["error"], "")

    # -------------------------------------------------------------- 故意不挡
    def test_the_owner_can_still_work_on_its_own_board(self):
        seen = team.status(self.plane, {"teamId": self.team["id"]}, ALICE)
        self.assertIn("Alice 的队", seen["summary"])
        # 成功分支的返回体没有 `ok` 字段（只有失败分支带 `ok: False`），
        # 所以断言看库里那一行 —— 那也正是"标成完成"唯一的判据。
        team.done(self.plane, {"taskId": self.first["id"], "ok": True,
                               "summary": "扫完了"}, ALICE)
        self.assertEqual(self.store.get_team_task(self.first["id"])["status"],
                         "done")

    def test_an_admin_is_not_limited_by_ownership(self):
        """排障要看别人的任务板。管理员是归属判据的唯一例外，与
        `server._require_chat_access` 那两处同一条。"""
        seen = team.status(self.plane, {"teamId": self.team["id"]}, ADMIN)
        self.assertIn("Alice 的队", seen["summary"])

    def test_a_session_without_an_owner_is_not_mistaken_for_someone_else(self):
        """命令行没有登录会话（`sessionId` 为空串），早期记录也没有 `session_id`。

        两侧任一为空时放行：判不出来就挡人，会把"这条记录是旧的"表现成
        "你没有权限"，而使用者会去找权限，找不到。
        """
        legacy = self.store.create_team(session_id="", engagement_id="",
                                        name="旧队", goal="")
        self.store.add_team_task(legacy["id"], "旧任务")
        anonymous = {"sessionId": "", "userId": ""}
        seen = team.status(self.plane, {"teamId": legacy["id"]}, anonymous)
        self.assertIn("旧队", seen["summary"])
        # 有归属的团队，对一个**没有**会话身份的调用方同样放行（同一条判据）。
        seen = team.status(self.plane, {"teamId": self.team["id"]}, anonymous)
        self.assertIn("Alice 的队", seen["summary"])

    # -------------------------------------------------------------- 未被削弱
    def test_a_missing_team_is_still_a_404_shaped_error(self):
        with self.assertRaises(team.TeamError) as caught:
            team.status(self.plane, {"teamId": "T-00000000"}, BOB)
        self.assertIn("没有编号为", str(caught.exception))

    def test_the_default_is_still_this_sessions_own_team(self):
        """不给 `teamId` 时走 `find_team(sessionId)`，那条路本来就没问题 —— 钉住
        它，是为了让"归属闸"不要被误写成"必须显式给 teamId"。"""
        seen = team.status(self.plane, {}, ALICE)
        self.assertIn("Alice 的队", seen["summary"])
        with self.assertRaises(team.TeamError) as caught:
            team.status(self.plane, {}, BOB)
        self.assertIn("还没有建过团队", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
