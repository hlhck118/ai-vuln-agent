# -*- coding: utf-8 -*-
"""渗透任务：一行一个目标，状态由作业派生。

「任务」在本地就是 engagements 那一行（不为它另开一张表），状态是**算出来的**：
暂停优先，其次执行中，其次排队中，有过作业就是已完成，什么都没有就是未开始。
这一组用例钉住这条派生规则、单条与批量共用一条路径、以及归档默认不可见。
"""
import unittest

from tests import support
from pentestd import auth
from pentestd.store import RUNNING, DONE


def _engagement(store, target="10.10.10.110"):
    return store.create_engagement(target, "拿下控制台", "本人授权")["id"]


class TasksTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("tasks.db")
        self.httpd, self.client = support.start(self.store)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def _mark(self, job_id, status):
        """把作业直接推到某个状态。

        这一页只关心「任务怎么读作业」，不关心作业是怎么跑到那个状态的；走一遍
        派发要起执行器、等心跳，测的就不再是列表了。
        """
        self.store.conn.execute("UPDATE jobs SET status=? WHERE id=?",
                                (status, job_id))
        self.store.conn.commit()

    def _operator_client(self):
        self.store.create_user("operator", auth.hash_password("Operator-Pass1!"),
                              role="operator")
        base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        client = support.Client(base)
        status, payload = client.login("operator", "Operator-Pass1!")
        self.assertEqual(200, status, payload)
        return client

    def test_requires_session(self):
        directory, store = support.temporary_store("tasks-anon.db")
        eid = _engagement(store)
        httpd, client = support.start(store, admin=False)
        try:
            status, payload = client.get("/api/v1/tasks")
            self.assertEqual(401, status, payload)
            status, payload = client.get("/api/v1/tasks/%s" % eid)
            self.assertEqual(401, status, payload)
        finally:
            support.stop(httpd, store, directory)

    def test_empty_list_keeps_every_counter(self):
        status, payload = self.client.get("/api/v1/tasks")
        self.assertEqual(200, status, payload)
        self.assertEqual([], payload["tasks"])
        self.assertEqual(0, payload["total"])
        # 计数条上的六个数字一律给齐：少一个，那一格就变成空白而不是 0。
        for key in ("all", "running", "queued", "paused", "done", "idle",
                    "archived"):
            self.assertIn(key, payload["counts"])
        self.assertIn("idle", payload["statusLabels"])

    def test_status_is_derived_from_jobs(self):
        eid = _engagement(self.store)
        status, payload = self.client.get("/api/v1/tasks/%s" % eid)
        self.assertEqual(200, status, payload)
        self.assertEqual("idle", payload["status"])
        self.assertEqual(0, payload["jobs"]["total"])
        self.assertEqual("10.10.10.110", payload["name"])

        job = self.store.create_job("scan", "10.10.10.110", engagement_id=eid)
        status, payload = self.client.get("/api/v1/tasks/%s" % eid)
        self.assertEqual("queued", payload["status"])
        self.assertEqual(1, payload["jobs"]["pending"])

        self._mark(job["id"], RUNNING)
        status, payload = self.client.get("/api/v1/tasks/%s" % eid)
        self.assertEqual("running", payload["status"])
        self.assertEqual(1, payload["inFlight"])

        self._mark(job["id"], DONE)
        status, payload = self.client.get("/api/v1/tasks/%s" % eid)
        self.assertEqual("done", payload["status"])

    def test_pause_wins_over_running(self):
        eid = _engagement(self.store)
        job = self.store.create_job("scan", "10.10.10.110", engagement_id=eid)
        self._mark(job["id"], RUNNING)
        status, payload = self.client.post("/api/v1/tasks/%s/pause" % eid)
        self.assertEqual(200, status, payload)
        self.assertEqual("pause", payload["action"])
        self.assertEqual(1, payload["updated"])
        status, payload = self.client.get("/api/v1/tasks/%s" % eid)
        # 暂停只挡新作业，已经派出去的还在跑——所以状态说「已暂停」，但
        # jobs.running 仍然是 1，两个读数不矛盾。
        self.assertEqual("paused", payload["status"])
        self.assertTrue(payload["paused"])
        self.assertEqual(1, payload["jobs"]["running"])

    def test_resume_clears_the_flag(self):
        eid = _engagement(self.store)
        self.client.post("/api/v1/tasks/%s/pause" % eid)
        status, payload = self.client.post("/api/v1/tasks/%s/resume" % eid)
        self.assertEqual(200, status, payload)
        self.assertEqual("resume", payload["action"])
        status, payload = self.client.get("/api/v1/tasks/%s" % eid)
        self.assertEqual("idle", payload["status"])
        self.assertFalse(payload["paused"])

    def test_single_and_bulk_share_one_path(self):
        eid = _engagement(self.store)
        status, payload = self.client.post("/api/v1/tasks/pause",
                                           {"taskIds": [eid]})
        self.assertEqual(200, status, payload)
        self.assertEqual(1, payload["updated"])
        self.assertEqual([], payload["missing"])
        status, payload = self.client.post("/api/v1/tasks/resume",
                                           {"taskIds": [eid, "E-nope"]})
        self.assertEqual(200, status, payload)
        self.assertEqual(1, payload["updated"])
        self.assertEqual(["E-nope"], payload["missing"])

    def test_archive_hides_from_default_list(self):
        eid = _engagement(self.store)
        status, payload = self.client.post("/api/v1/tasks/archive",
                                           {"taskIds": [eid]})
        self.assertEqual(200, status, payload)
        status, payload = self.client.get("/api/v1/tasks")
        self.assertEqual([], payload["tasks"])
        self.assertEqual(1, payload["counts"]["archived"])
        self.assertEqual(0, payload["counts"]["all"])
        status, payload = self.client.get("/api/v1/tasks?archived=1")
        self.assertEqual(1, len(payload["tasks"]))
        self.assertTrue(payload["tasks"][0]["archived"])
        status, payload = self.client.post("/api/v1/tasks/unarchive",
                                           {"taskIds": [eid]})
        self.assertEqual(200, status, payload)
        status, payload = self.client.get("/api/v1/tasks")
        self.assertEqual(1, len(payload["tasks"]))

    def test_filters_do_not_move_the_counters(self):
        first = _engagement(self.store, "10.10.10.110")
        _engagement(self.store, "example.com")
        self.store.create_job("scan", "example.com", engagement_id=first)
        status, payload = self.client.get("/api/v1/tasks?status=idle")
        self.assertEqual(200, status, payload)
        self.assertEqual(1, len(payload["tasks"]))
        self.assertEqual("example.com", payload["tasks"][0]["target"])
        # 计数按全部行算：点了「未开始」之后，仍要知道还有几个在排队。
        self.assertEqual(2, payload["counts"]["all"])
        self.assertEqual(1, payload["counts"]["queued"])

        status, payload = self.client.get("/api/v1/tasks?q=example")
        self.assertEqual(1, len(payload["tasks"]))
        status, payload = self.client.get("/api/v1/tasks?status=running")
        self.assertEqual([], payload["tasks"])
        status, payload = self.client.get("/api/v1/tasks?status=nope")
        self.assertEqual(400, status, payload)

    def test_update_task_only_touches_whitelisted_fields(self):
        eid = _engagement(self.store)
        status, payload = self.client.post(
            "/api/v1/tasks/%s" % eid,
            {"pinned": True, "remark": "先看这个", "nonsense": 1})
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["pinned"])
        self.assertEqual("先看这个", payload["remark"])
        self.assertNotIn("nonsense", payload)
        self.assertNotIn("nonsense", self.store.get_task(eid))
        status, payload = self.client.post(
            "/api/v1/tasks/%s" % eid, {"noResultMinSeverity": "nope"})
        self.assertEqual(400, status, payload)

    def test_pinned_sorts_first(self):
        first = _engagement(self.store, "10.10.10.110")
        _engagement(self.store, "example.com")
        self.client.post("/api/v1/tasks/%s" % first, {"pinned": True})
        status, payload = self.client.get("/api/v1/tasks")
        self.assertEqual(first, payload["tasks"][0]["id"])

    def test_unknown_task_is_reported_per_id(self):
        status, payload = self.client.get("/api/v1/tasks/E-nope")
        self.assertEqual(404, status, payload)
        # 批量路径不整体失败，而是逐条报「这个没改成」：一个 id 拼错时静默少改
        # 一行，比整批 404 更难发现。
        status, payload = self.client.post("/api/v1/tasks/E-nope/pause")
        self.assertEqual(200, status, payload)
        self.assertEqual(0, payload["updated"])
        self.assertEqual(["E-nope"], payload["missing"])

    def test_operator_reads_but_cannot_pause(self):
        eid = _engagement(self.store)
        client = self._operator_client()
        status, payload = client.get("/api/v1/tasks")
        self.assertEqual(200, status, payload)
        self.assertEqual(1, len(payload["tasks"]))
        status, payload = client.post("/api/v1/tasks/%s/pause" % eid)
        self.assertEqual(403, status, payload)
        status, payload = client.post("/api/v1/tasks/archive",
                                      {"taskIds": [eid]})
        self.assertEqual(403, status, payload)
