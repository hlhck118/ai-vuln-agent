# -*- coding: utf-8 -*-
"""仪表盘：一次请求换整屏读数。

钉三件事：形状（前端按这些键画卡）、跨表聚合的一致性（同一屏上的数字来自同一
时刻）、以及空库时不塌（新装环境打开仪表盘不该是空白，更不该 500）。
"""
import unittest

from tests import support


def _engagement(store, target="10.10.10.110"):
    return store.create_engagement(target, "拿下控制台", "本人授权")["id"]


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("dashboard.db")
        self.httpd, self.client = support.start(self.store)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def test_requires_session(self):
        directory, store = support.temporary_store("dashboard-anon.db")
        httpd, client = support.start(store, admin=False)
        try:
            status, payload = client.get("/api/v1/dashboard")
            self.assertEqual(401, status, payload)
            status, payload = client.get("/api/v1/system/metrics")
            self.assertEqual(401, status, payload)
        finally:
            support.stop(httpd, store, directory)

    def test_empty_platform(self):
        status, payload = self.client.get("/api/v1/dashboard")
        self.assertEqual(200, status, payload)
        for key in ("activeTask", "topFindings", "recentJobs", "assetCounts",
                    "assets", "exploration", "findings", "traffic", "tasks",
                    "jobs", "executors", "executorsOnline", "nodes", "edges",
                    "generatedAt", "version", "engineMode", "llmConfigured",
                    "statusLabels"):
            self.assertIn(key, payload)
        self.assertIsNone(payload["activeTask"])
        self.assertEqual([], payload["topFindings"])
        self.assertEqual([], payload["recentJobs"])
        self.assertEqual(0, payload["assets"])
        self.assertEqual(0, payload["traffic"])
        self.assertEqual("idle", payload["engineMode"])
        self.assertEqual(0, payload["tasks"]["all"])
        # 六类记录一律给齐：缺一类，前端那一行就静默消失，而不是显示 0。
        for key in ("goal", "intent", "fact", "finding", "digest"):
            self.assertIn(key, payload["exploration"])
        self.assertIn("running", payload["statusLabels"])
        self.assertFalse(payload["llmConfigured"])

    def test_top_findings_keep_high_and_above(self):
        eid = _engagement(self.store)
        intent = self.store.add_node(
            eid, "intent", {"title": "打点", "goalId": "goal-1"})["id"]
        for severity, title in (("critical", "命令注入"), ("high", "SQL 注入"),
                                ("low", "目录列表"), ("info", "框架指纹")):
            # 节点的必填字段叫 title（name 是列表上显示的那个"名字"，finding 没有）。
            self.store.add_node(eid, "finding", {
                "intentId": intent, "title": title, "severity": severity,
                "summary": "摘要", "reproducibleSteps": ["curl -i http://x/"]})
        status, payload = self.client.get("/api/v1/dashboard")
        self.assertEqual(200, status, payload)
        names = [item["name"] for item in payload["topFindings"]]
        self.assertIn("命令注入", names)
        self.assertIn("SQL 注入", names)
        self.assertNotIn("目录列表", names)
        self.assertNotIn("框架指纹", names)
        for item in payload["topFindings"]:
            self.assertEqual(eid, item["engagementId"])
            self.assertEqual("10.10.10.110", item["target"])
            self.assertTrue(item["createdAt"])

    def test_recent_jobs_and_active_task(self):
        eid = _engagement(self.store)
        self.store.create_job("scan", "10.10.10.110", engagement_id=eid)
        status, payload = self.client.get("/api/v1/dashboard")
        self.assertEqual(200, status, payload)
        self.assertEqual(eid, payload["activeTask"]["id"])
        self.assertEqual("queued", payload["activeTask"]["status"])
        # 引擎模式跟着任务状态走：宁可说「排队中」，也不要上面说空闲、下面在跑。
        self.assertEqual("queued", payload["engineMode"])
        self.assertEqual(1, len(payload["recentJobs"]))
        job = payload["recentJobs"][0]
        self.assertEqual("scan", job["kind"])
        self.assertEqual("pending", job["status"])
        self.assertEqual(eid, job["engagementId"])

    def test_task_counts_match_the_task_page(self):
        _engagement(self.store, "10.10.10.110")
        _engagement(self.store, "example.com")
        status, tasks = self.client.get("/api/v1/tasks")
        self.assertEqual(200, status, tasks)
        status, payload = self.client.get("/api/v1/dashboard")
        # 两个页面必须数是同一批行：仪表盘自己再算一遍，就会出现「这里说 3 个
        # 任务、点进去只有 2 条」。
        self.assertEqual(tasks["counts"], payload["tasks"])

    def test_system_metrics_shape(self):
        status, payload = self.client.get("/api/v1/system/metrics")
        self.assertEqual(200, status, payload)
        for key in ("scope", "supported", "timestamp", "cpu", "memory",
                    "disk", "dataDisk", "loadAverage", "notes"):
            self.assertIn(key, payload)
        self.assertGreaterEqual(int(payload["cpu"]["cores"]), 1)
        # 磁盘靠 shutil，不依赖可选包；数字必须是数字，不能因为缺 psutil
        # 就退化成字符串（前端直接拿去做百分比）。
        for key in ("usedBytes", "totalBytes", "percent"):
            self.assertIn(key, payload["dataDisk"])
            self.assertIsInstance(payload["dataDisk"][key], (int, float))
        self.assertIsInstance(payload["notes"], list)
