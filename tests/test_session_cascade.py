# -*- coding: utf-8 -*-
"""删掉一条对话，它**独占**的目标与任务跟着走。

对话是目标的生命周期入口：`ensure_session_engagement` 在第一次产出型调用时把会话绑
到一个目标上，此后这一轮的任务、链路、漏洞、资产都落在那个目标里。所以"删掉这段工作
记录"与"删掉它留下的东西"是同一件事的两个部分——只删对话，侧边栏会留下一个 0 条对话
的目标，任务页会留下一批再也查不到出处的执行记录。

本模块钉住四条边界，每一条都对应一种"删错"：

  1. **独占才连带。** 目标可能被多条对话共用（新建对话会跟着当时选中的目标走）。还有
     别人绑着时删掉目标 = 顺手删掉别人的证据；
  2. **删证据仍是管理员动作。** 不能从"删对话"这里绕到 `/engagements/{id}/delete`
     那道门的里面去；
  3. **响应对得上账。** 走没走目标、为什么没走，必须写在响应里——只回 `{"ok": true}`
     的话，使用者会以为目标也走了，而它在侧边栏里还在；
  4. **运行时映射跟着走。** `dsh_session:<会话>` 是设置表里的一条键，删对话不删它，库
     里就会堆着一串指向已不存在会话的映射。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd.auth import hash_password             # noqa: E402
from pentestd.server import ApiError, ControlPlane  # noqa: E402
from pentestd.store import Store                    # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


class SessionCascadeBase(unittest.TestCase):
    """建一个绑在目标上的会话，外加一条任务与它们的日志。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(os.path.join(self.tmp.name, "cascade.db"))
        self.store.create_user("root", hash_password("Passw0rd-Root1!"), "admin")
        self.store.create_user("ops", hash_password("Passw0rd-Ops1!"), "operator")
        self.admins = {"admin": self.store.get_user_by_name("root"),
                       "operator": self.store.get_user_by_name("ops")}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _bound(self, target="10.0.0.5", user="admin", with_job=True):
        """目标 + 一条绑在它上面的对话（+ 一条带日志的任务）。"""
        meta = self.store.create_engagement(target=target, objective="冒烟",
                                            authorization="AUTH-CASCADE-1")
        eid = meta["id"]
        session = self.store.create_chat_session(
            title="扫一下", user_id=self.admins[user]["id"], engagement_id=eid)
        job = None
        if with_job:
            job = self.store.create_job("probe", target, engagement_id=eid)
            self.store.append_job_events(job["id"], ["起", "止"])
        return eid, session["id"], job

    def _ctx(self, session_id, role="admin", body=None):
        return {"query": {}, "body": dict(body or {}), "headers": {},
                "params": {"session_id": session_id},
                "user": self.admins[role]}

    def _plane(self, auth_required=True):
        return ControlPlane(self.store, WEB_DIR, auth_required=auth_required)


class ExclusiveSessionTestCase(SessionCascadeBase):
    """独占一个目标的对话：删对话 = 删目标。"""

    def test_the_goal_and_its_jobs_go_with_the_chat(self):
        eid, sid, job = self._bound()
        status, payload = self._plane().delete_chat_session(self._ctx(sid))
        self.assertEqual(200, status)
        self.assertEqual(sid, payload["deleted"])
        self.assertEqual(eid, payload["goal"]["deleted"])
        self.assertEqual(1, payload["goal"]["counts"]["engagements"])
        self.assertEqual([], [item["id"] for item in self.store.list_engagements()],
                         "目标还在的话，侧边栏会留下一个 0 条对话的空目标")
        self.assertIsNone(self.store.get_job(job["id"]), "任务行应当跟着走")
        left = self.store.conn.execute(
            "SELECT COUNT(*) FROM job_events WHERE job_id=?", (job["id"],)).fetchone()[0]
        self.assertEqual(0, left, "执行日志也应当跟着走")
        self.assertIsNone(self.store.get_chat_session(sid))

    def test_the_goal_is_gone_before_the_next_list_refresh(self):
        """删完立刻查库，不等任何缓存或列表重画。"""
        eid, sid, _job = self._bound()
        self._plane().delete_chat_session(self._ctx(sid))
        self.assertNotIn(eid, [item["id"] for item in self.store.list_engagements()])


class SharedGoalTestCase(SessionCascadeBase):
    """还有别的对话绑着同一个目标：只删对话。"""

    def test_the_goal_stays_and_the_report_says_why(self):
        eid, sid, _job = self._bound()
        other = self.store.create_chat_session(title="另一条", user_id="",
                                               engagement_id=eid)
        status, payload = self._plane().delete_chat_session(self._ctx(sid))
        self.assertEqual(200, status)
        self.assertNotIn("deleted", payload["goal"])
        self.assertEqual(eid, payload["goal"]["kept"])
        self.assertEqual("shared", payload["goal"]["reason"])
        self.assertEqual(1, payload["goal"]["sessions"])
        self.assertIn(eid, [item["id"] for item in self.store.list_engagements()],
                      "共用目标被顺手删掉 = 删掉别人的证据")
        self.assertIsNotNone(self.store.get_chat_session(other["id"]))
        self.assertIsNone(self.store.get_chat_session(sid))

    def test_a_session_still_there_is_counted_after_the_delete(self):
        """剩下几条也要算得出来——"共用"是个数量，不是一个是非。"""
        eid, sid, _job = self._bound()
        for index in range(2):
            self.store.create_chat_session(title="同伴%d" % index, user_id="",
                                           engagement_id=eid)
        _status, payload = self._plane().delete_chat_session(self._ctx(sid))
        self.assertEqual(2, payload["goal"]["sessions"])


class PermissionTestCase(SessionCascadeBase):
    """删证据是管理员动作，不能从"删对话"这里绕进去。"""

    def test_an_operator_only_loses_the_chat(self):
        eid, sid, _job = self._bound(user="operator")
        status, payload = self._plane().delete_chat_session(
            self._ctx(sid, role="operator"))
        self.assertEqual(200, status)
        self.assertEqual("admin", payload["goal"]["reason"])
        self.assertEqual(eid, payload["goal"]["kept"])
        self.assertIn(eid, [item["id"] for item in self.store.list_engagements()],
                      "非管理员删对话不该带走目标")

    def test_with_login_disabled_everyone_counts_as_admin(self):
        """开发模式没有角色可用，此时"能不能删证据"由 `/engagements/delete` 那道门
        之外的部署条件决定（没开登录 = 本机单人使用），这里不该额外拦。"""
        eid, sid, _job = self._bound()
        ctx = {"query": {}, "body": {}, "headers": {},
               "params": {"session_id": sid}, "user": None}
        _status, payload = self._plane(auth_required=False).delete_chat_session(ctx)
        self.assertEqual(eid, payload["goal"]["deleted"])


class ExplicitKeepTestCase(SessionCascadeBase):
    """`withEngagement: false` 是"只删对话"的显式出口。"""

    def test_the_goal_can_be_kept_on_request(self):
        eid, sid, _job = self._bound()
        status, payload = self._plane().delete_chat_session(
            self._ctx(sid, body={"withEngagement": False}))
        self.assertEqual(200, status)
        self.assertEqual("kept", payload["goal"]["reason"])
        self.assertEqual(eid, payload["goal"]["kept"])
        self.assertIn(eid, [item["id"] for item in self.store.list_engagements()])

    def test_an_unbound_chat_says_so(self):
        session = self.store.create_chat_session(title="随便问问", user_id="")
        _status, payload = self._plane().delete_chat_session(self._ctx(session["id"]))
        self.assertEqual("unbound", payload["goal"]["reason"])
        self.assertEqual("", payload["goal"]["kept"])
        self.assertIsNone(self.store.get_chat_session(session["id"]))

    def test_a_dangling_binding_only_deletes_the_chat(self):
        """绑定指向一个已经不在的目标（历史遗留）→ 删对话，不报错。"""
        session = self.store.create_chat_session(
            title="旧对话", user_id="", engagement_id="E-gone")
        status, payload = self._plane().delete_chat_session(self._ctx(session["id"]))
        self.assertEqual(200, status)
        self.assertEqual("missing", payload["goal"]["reason"])
        self.assertIsNone(self.store.get_chat_session(session["id"]))

    def test_deleting_twice_is_a_404(self):
        _eid, sid, _job = self._bound()
        plane = self._plane()
        plane.delete_chat_session(self._ctx(sid))
        with self.assertRaises(ApiError) as ctx:
            plane.delete_chat_session(self._ctx(sid))
        self.assertEqual(404, ctx.exception.status)


class RuntimeMappingTestCase(SessionCascadeBase):
    """`dsh_session:<会话>` 跟着走；别处的键不许被顺手清掉。"""

    def test_the_dsh_session_mapping_is_dropped(self):
        _eid, sid, _job = self._bound()
        self.store.set_setting("dsh_session:" + sid, "session-abc")
        self._plane().delete_chat_session(self._ctx(sid))
        self.assertEqual("", self.store.get_setting("dsh_session:" + sid, ""),
                         "留着它，设置表里会堆一串指向已删除对话的键")

    def test_other_settings_are_untouched(self):
        _eid, sid, _job = self._bound()
        self.store.set_setting("dsh_profile", "v2sdk")
        self.store.set_setting("dsh_session:other", "keep-me")
        self._plane().delete_chat_session(self._ctx(sid))
        self.assertEqual("v2sdk", self.store.get_setting("dsh_profile", ""))
        self.assertEqual("keep-me", self.store.get_setting("dsh_session:other", ""))

    def test_delete_setting_reports_whether_it_was_there(self):
        self.store.set_setting("temp.key", "1")
        self.assertTrue(self.store.delete_setting("temp.key"))
        self.assertFalse(self.store.delete_setting("temp.key"),
                         "删一条本来就没有的键要返回 False，而不是装作删掉了")


if __name__ == "__main__":
    unittest.main()
