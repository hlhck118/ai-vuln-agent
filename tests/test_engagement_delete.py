# -*- coding: utf-8 -*-
"""目标的删除：删掉的是**证据**，不是一条调度记录。

与删除任务的分工是本模块的中心：

  * 单条任务任何登录用户都能删，删掉只丢调度记录（`delete_job` 的注释写着这一条）；
  * 删目标**带走链路节点、边、漏洞与资产**，所以它要管理员，而且返回逐表计数。

四条必须钉住的边界：

  1. **挂靠表要逐张删。** 漏掉 `baselines` 的症状最隐蔽：目标没了、它那次的基线还在，
     于是下次对同一路径扫描会被判成增量，结果是一堆"没变化"，而没人会去怀疑一个
     已经删掉的目标；
  2. **计数里带 0 也要返回。** "那张表本来就没有行"与"我没删那张表"必须能分开——
     后者是删漏，而删漏会在下一次列表刷新时冒出来；
  3. **`chat_sessions` 不删，只解绑。** 那些会话是使用者的工作记录（他问过什么、
     模型答了什么），不是这个目标的证据。删目标顺手删掉对话，等于让"我删了一个扫错
     的地址"变成"我丢了一段工作过程"；
  4. **权限是管理员。** 这个动作不可撤销，而单条任务的删除不是它的先例。
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

ATTACHED = ("nodes", "edges", "counters", "jobs", "baselines", "footholds")


class EngagementDeleteStoreTestCase(unittest.TestCase):
    """store 层：逐表清干净、计数如实、缺失的目标要有明确的错。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(os.path.join(self.tmp.name, "del.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _make(self, target="10.0.0.5"):
        meta = self.store.create_engagement(target=target, objective="冒烟",
                                            authorization="AUTH-DEL-1")
        return meta["id"]

    def test_every_attached_table_is_cleared_and_counted(self):
        eid = self._make()
        self.store.create_job("probe", "10.0.0.5", engagement_id=eid)
        counts = self.store.delete_engagement(eid)
        for table in ATTACHED:
            self.assertIn(table, counts,
                          "计数里少了 %s：删漏一张表在界面上看不出来" % table)
        self.assertEqual(1, counts["engagements"])
        self.assertGreaterEqual(counts["nodes"], 1, "goal 节点应当一起走")
        self.assertEqual(1, counts["jobs"], "挂在这个目标上的任务应当一起走")

    def test_a_table_with_no_rows_still_reports_zero(self):
        """0 也要在计数里。少了它，"没有行"与"没删那张表"就分不开了。"""
        eid = self._make()
        counts = self.store.delete_engagement(eid)
        self.assertEqual(0, counts["edges"])
        self.assertEqual(set(ATTACHED) | {"jobEvents", "engagements",
                                          "chatSessionsUnbound"}, set(counts))

    def test_the_execution_log_goes_with_the_job(self):
        """日志按 `job_id` 挂，不在挂靠表清单里——但它也不能留。

        `job_events` 是全库唯一一张行数由"跑了多久"决定的表：一次扫描几万行。
        它没有 `engagement_id` 列，所以「逐张挂靠表删」那个循环看不见它；漏了
        它的症状不是报错，是删掉一批目标之后库还是那么大，而且那些行再也查不到
        出处。计到 `jobEvents` 而不是并在 `jobs` 里，是为了让调用方能分开说
        "删了 3 个任务"与"删了 20000 行日志"。
        """
        eid = self._make()
        job = self.store.create_job("probe", "10.0.0.5", engagement_id=eid)
        self.assertEqual(2, self.store.append_job_events(job["id"], ["一", "二"]))
        counts = self.store.delete_engagement(eid)
        self.assertEqual(2, counts["jobEvents"])
        left = self.store.conn.execute(
            "SELECT COUNT(*) FROM job_events WHERE job_id=?", (job["id"],)).fetchone()[0]
        self.assertEqual(0, left, "任务行走了，日志行还在——它就成了无主的表尾")

    def test_the_engagement_disappears_from_the_list(self):
        eid = self._make()
        self.assertIn(eid, [item["id"] for item in self.store.list_engagements()])
        self.store.delete_engagement(eid)
        self.assertNotIn(eid, [item["id"] for item in self.store.list_engagements()])

    def test_the_baseline_goes_with_it(self):
        """基线与目标是挂靠关系（没有真外键），漏删的症状延迟出现且看不出来。"""
        eid = self._make(target="/srv/app")
        self.store.set_baseline("/srv/app", {"files": {}}, engagement_id=eid)
        self.assertIsNotNone(self.store.get_baseline("/srv/app"))
        self.store.delete_engagement(eid)
        self.assertIsNone(self.store.get_baseline("/srv/app"),
                          "基线还在的话，下次扫描会被判成增量")

    def test_chat_sessions_are_unbound_not_deleted(self):
        eid = self._make()
        session = self.store.create_chat_session(title="问了点什么", user_id="U-x",
                                                 engagement_id=eid)
        counts = self.store.delete_engagement(eid)
        self.assertEqual(1, counts["chatSessionsUnbound"])
        survived = self.store.get_chat_session(session["id"])
        self.assertIsNotNone(survived, "对话是工作记录，不该跟着证据一起消失")
        self.assertEqual("", survived["engagementId"] or "")

    def test_deleting_a_missing_engagement_raises(self):
        with self.assertRaises(KeyError):
            self.store.delete_engagement("E-nope")


class EngagementDeleteApiTestCase(unittest.TestCase):
    """接口层：管理员才删得动，响应要说得出删了什么。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(os.path.join(self.tmp.name, "api.db"))
        self.store.create_user("root", hash_password("Passw0rd-Root1!"), "admin")
        self.store.create_user("ops", hash_password("Passw0rd-Ops1!"), "operator")
        self.store.create_engagement(target="10.0.0.5", objective="x",
                                     authorization="AUTH-DEL-1")
        self.plane = ControlPlane(self.store, WEB_DIR, auth_required=True)
        self._users = {"admin": self.store.get_user_by_name("root"),
                       "operator": self.store.get_user_by_name("ops")}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _ctx(self, eid, role="admin"):
        return {"query": {}, "body": {}, "headers": {},
                "params": {"eid": eid}, "user": self._users[role]}

    def test_only_admin_can_delete(self):
        eid = self.store.list_engagements()[0]["id"]
        with self.assertRaises(ApiError) as ctx:
            self.plane.delete_engagement(self._ctx(eid, role="operator"))
        self.assertEqual(403, ctx.exception.status)
        self.assertIn(eid, [item["id"] for item in self.store.list_engagements()],
                      "被拒之后目标必须还在")

    def test_the_response_says_what_was_taken(self):
        eid = self.store.list_engagements()[0]["id"]
        status, payload = self.plane.delete_engagement(self._ctx(eid))
        self.assertEqual(200, status)
        self.assertEqual(eid, payload["deleted"])
        self.assertGreaterEqual(payload["counts"]["engagements"], 1)

    def test_deleting_twice_is_a_404_not_a_500(self):
        eid = self.store.list_engagements()[0]["id"]
        self.plane.delete_engagement(self._ctx(eid))
        with self.assertRaises(ApiError) as ctx:
            self.plane.delete_engagement(self._ctx(eid))
        self.assertEqual(404, ctx.exception.status)


if __name__ == "__main__":
    unittest.main()
