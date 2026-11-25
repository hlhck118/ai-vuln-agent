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

    def test_the_archived_bucket_has_a_word(self):
        """`tasks.archived` 这一档也要有中文说法。

        这一行原先在界面上印成裸 `archived`：`statusLabels`（后端 `TASK_STATE_LABELS`）
        只有五个正常状态，而计数里多一个归档档。界面按 `labels[key] || key` 取值，
        缺词就**安静地**退回机器词——整屏别处都是中文，只有这一行是英文，看起来
        像「另一种东西」，而它其实只是「已归档的任务」。
        """
        status, payload = self.client.get("/api/v1/dashboard")
        self.assertEqual(200, status, payload)
        self.assertIn("archived", payload["statusLabels"])
        self.assertEqual("已归档", payload["statusLabels"]["archived"])
        self.assertIn("archived", payload["tasks"])

    def test_the_asset_type_labels_ride_along(self):
        """`assetCounts` 的键是原始类型，词表必须跟着响应一起回来。

        键必须是机器值（`ip` / `subdomain` / `root-domain`），因为那是节点里存的
        东西；但给人看的那一列得是中文。词表只有后端 `models.ASSET_TYPE_LABELS`
        一份——界面自己再抄六项的话，平台改了词之后仪表盘与资产页会各说各话，
        而两边单看都说得通。
        """
        status, payload = self.client.get("/api/v1/dashboard")
        self.assertEqual(200, status, payload)
        self.assertIn("assetTypes", payload)
        self.assertIn("assetTypeLabels", payload)
        self.assertEqual(sorted(payload["assetTypes"]),
                         sorted(payload["assetTypeLabels"]),
                         "词表与类型清单不是同一套")
        # 与 `/tasks/{id}/assets` 是同一份；这里钉两个最容易被改错的。
        self.assertEqual("IP", payload["assetTypeLabels"]["ip"])
        self.assertEqual("根域", payload["assetTypeLabels"]["root-domain"])

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
