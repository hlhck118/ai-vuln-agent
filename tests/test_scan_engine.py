# -*- coding: utf-8 -*-
"""「扫描执行」口径：两条路都要出现在设置里，而且真的要分流。

这条用例守三件事，缺一件这个设置就是摆设：

1. **两条路都画出来**。只写"当前选中的那条"，使用者切过去之后才会发现缺件，
   而那时任务已经下发了——失败会看起来像偶发故障。
2. **写入口径要严格**。未知取值回 400，不回退默认：静默回退的症状是"点了选项
   但它没生效"，随后读到的报告来源与使用者以为的那条不一样。
3. **执行节点真的分流**。`scanEngine=dsh` 时只走 `scan_dsh`，**不许**静默回退到
   发掘引擎——口径是设置页对使用者的承诺，回退等于让报告说谎。

判据分层：设置读写（单元）、接口（HTTP）、分流（agent）、界面（源码契约）。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.dirname(HERE)
sys.path.insert(0, PLATFORM)

from tests import support                                        # noqa: E402
from pentestd import agent, dsh_runtime, scan_dsh, scan_engine    # noqa: E402

WEB = os.path.join(PLATFORM, "web")


class FakeStore:
    """只实现 `get_setting` / `set_setting` 的最小设置库。"""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.writes = []

    def get_setting(self, key, default=""):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.writes.append((key, value))
        self.values[key] = value


class ScanEngineSettingTest(unittest.TestCase):
    """设置读数：默认、宽容读、下发字段、缺件要给出下一步。"""

    def test_default_is_the_vulnagent_pipeline(self):
        store = FakeStore()
        self.assertEqual("vulnagent", scan_engine.mode(store))
        self.assertEqual("发掘引擎（vulnagent）", scan_engine.label("vulnagent"))

    def test_unknown_value_reads_as_default_but_is_not_written_back(self):
        """读侧宽容：旧库里的怪值不该让设置页打不开。"""
        store = FakeStore({"scan_engine": "something-else"})
        self.assertEqual("vulnagent", scan_engine.mode(store))
        self.assertEqual([], store.writes, "读接口不该顺手改库")

    def test_payload_carries_mode_home_and_profile(self):
        store = FakeStore({"scan_engine": "dsh",
                           "dsh_profile": "v2sdk"})
        payload = scan_engine.payload_for(store, home=r"F:\tmp\dsh-home")
        self.assertEqual("dsh", payload["scanEngine"])
        self.assertEqual(r"F:\tmp\dsh-home", payload["dshHome"])
        self.assertEqual("v2sdk", payload["dshProfile"])

    def test_payload_profile_falls_back_when_unset(self):
        payload = scan_engine.payload_for(FakeStore())
        self.assertEqual(dsh_runtime.DEFAULT_PROFILE, payload["dshProfile"])

    def test_both_paths_are_reported_with_their_own_availability(self):
        view = scan_engine.describe(FakeStore())
        self.assertEqual("vulnagent", view["mode"])
        self.assertEqual(["vulnagent", "dsh"],
                         [item["value"] for item in view["options"]])
        self.assertIn("available", view["vulnagent"])
        self.assertIn("available", view["dsh"])

    def test_missing_profile_says_what_to_run_next(self):
        """缺 profile 是这条路最常见的"起不来"，报错必须带下一步动作。"""
        with tempfile.TemporaryDirectory() as home:
            view = scan_engine.describe(FakeStore({"dsh_profile": "no-such"}), home)
        dsh = view["dsh"]
        self.assertFalse(dsh["available"])
        self.assertIn("no-such", dsh["reason"])
        self.assertIn("plugin --profile no-such", dsh["reason"])


class ScanEngineApiTest(unittest.TestCase):
    """接口层：读不设限、写要管理员、未知取值报错、任务载荷带口径。"""

    @classmethod
    def setUpClass(cls):
        cls.dir, cls.store = support.temporary_store("scan-engine.db")
        cls.httpd, cls.admin = support.start(cls.store, WEB)
        status, created = cls.admin.post("/api/v1/users", {
            "username": "scan-viewer", "password": "Viewer-Pass-123",
            "role": "operator"})
        if status != 201:
            raise RuntimeError("建测试用户失败：%s %s" % (status, created))
        base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        cls.viewer = support.Client(base)
        if cls.viewer.login("scan-viewer", "Viewer-Pass-123")[0] != 200:
            raise RuntimeError("测试用户登录失败")

    @classmethod
    def tearDownClass(cls):
        support.stop(cls.httpd, cls.store, cls.dir)

    def test_get_reports_two_choices_and_the_current_one(self):
        status, payload = self.admin.get("/api/v1/scan-engine")
        self.assertEqual(200, status)
        self.assertIn(payload["mode"], ("vulnagent", "dsh"))
        self.assertEqual(["vulnagent", "dsh"],
                         [item["value"] for item in payload["options"]])
        for key in ("available", "reason"):
            self.assertIn(key, payload["vulnagent"])
            self.assertIn(key, payload["dsh"])

    def test_operator_can_read_but_not_write(self):
        self.assertEqual(200, self.viewer.get("/api/v1/scan-engine")[0])
        status, _payload = self.viewer.post("/api/v1/scan-engine", {"mode": "dsh"})
        self.assertIn(status, (401, 403))

    def test_admin_choice_is_saved_and_read_back(self):
        status, payload = self.admin.post("/api/v1/scan-engine", {"mode": "dsh"})
        self.assertEqual(200, status)
        self.assertEqual("dsh", payload["mode"])
        self.assertEqual("dsh", scan_engine.mode(self.store))
        status, again = self.admin.get("/api/v1/scan-engine")
        self.assertEqual(200, status)
        self.assertEqual("dsh", again["mode"])

    def test_unknown_mode_is_rejected_and_nothing_changes(self):
        before = scan_engine.mode(self.store)
        status, payload = self.admin.post("/api/v1/scan-engine", {"mode": "auto"})
        self.assertEqual(400, status)
        self.assertIn("未知", payload.get("error", "") + str(payload))
        self.assertEqual(before, scan_engine.mode(self.store))

    def test_dsh_endpoint_carries_the_same_reading(self):
        """设置页只发 `/dsh` 一次请求就要能画完整块，两处不能各说一套。"""
        status, payload = self.admin.get("/api/v1/dsh")
        self.assertEqual(200, status)
        scan = payload.get("scanEngine") or {}
        self.assertEqual(scan_engine.mode(self.store), scan.get("mode"))
        self.assertEqual(["vulnagent", "dsh"],
                         [item["value"] for item in scan.get("options") or []])

    def test_job_creation_carries_the_chosen_engine(self):
        self.admin.post("/api/v1/scan-engine", {"mode": "dsh"})
        with tempfile.TemporaryDirectory() as target:
            status, created = self.admin.post("/api/v1/jobs", {"target": target})
        self.assertEqual(201, status)
        self.assertEqual("dsh", created.get("scanEngine"))
        self.assertEqual("推理引擎（DSH）", created.get("scanEngineLabel"))
        self.admin.post("/api/v1/scan-engine", {"mode": "vulnagent"})


class ScanEngineRoutingTest(unittest.TestCase):
    """执行节点：口径随任务下发，dsh 只走 DSH，绝不静默回退。"""

    def _job(self, mode, target):
        return {"id": "job-scan", "kind": "scan", "target": target,
                "patched": "", "engagementId": "",
                "payload": {"scanEngine": mode}}

    def test_dsh_mode_is_handed_to_the_dsh_runner(self):
        sentinel = {"status": "done", "summary": {}, "nodes": [],
                    "baseline": {}, "agents": []}
        with tempfile.TemporaryDirectory() as target:
            job = self._job("dsh", target)
            with mock.patch.object(scan_dsh, "execute_job",
                                   return_value=sentinel) as runner, \
                    mock.patch.object(agent.engine, "run_scan") as vulnagent:
                result = agent.execute_job(job, target)
        self.assertEqual(sentinel, result)
        runner.assert_called_once()
        self.assertFalse(vulnagent.called, "dsh 口径下不该调用发掘引擎")

    def test_dsh_failure_is_reported_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as target:
            job = self._job("dsh", target)
            with mock.patch.object(
                    scan_dsh, "execute_job",
                    side_effect=scan_dsh.ScanError("缺 profile")), \
                    mock.patch.object(agent.engine, "run_scan") as vulnagent:
                result = agent.execute_job(job, target)
        self.assertEqual("failed", result["status"])
        self.assertIn("缺 profile", result["error"])
        self.assertFalse(vulnagent.called, "失败时不许偷偷改用发掘引擎")

    def test_unknown_mode_never_runs_anything(self):
        with tempfile.TemporaryDirectory() as target:
            job = self._job("auto", target)
            with mock.patch.object(agent.engine, "run_scan") as vulnagent:
                result = agent.execute_job(job, target)
        self.assertEqual("failed", result["status"])
        self.assertIn("未知", result["error"])
        self.assertFalse(vulnagent.called)

    def test_default_mode_still_uses_the_vulnagent_pipeline(self):
        result_body = {"run_id": "r1", "target": {"name": "t"}, "slices": [],
                       "hypotheses": [], "findings": [], "chains": [],
                       "incremental": {}, "verifications": []}
        with tempfile.TemporaryDirectory() as target:
            job = self._job("", target)
            with mock.patch.object(agent.engine, "run_scan",
                                   return_value=result_body) as runner, \
                    mock.patch.object(agent.engine, "build_chain_specs",
                                      return_value=[]), \
                    mock.patch.object(agent.engine, "build_manifest",
                                      return_value={}), \
                    mock.patch.object(agent.engine, "build_agent_trace",
                                      return_value=[]):
                result = agent.execute_job(job, target)
        self.assertEqual("done", result["status"], result.get("error"))
        runner.assert_called_once()
        self.assertEqual("r1", result["summary"]["runId"])


class ScanEngineUiContractTest(unittest.TestCase):
    """界面层：卡片存在、两个选项都在、而且把证据差别写出来。"""

    def _source(self, name):
        with open(os.path.join(WEB, name), encoding="utf-8") as fh:
            return fh.read()

    def test_settings_page_has_the_scan_engine_card(self):
        html = self._source("index.html")
        self.assertIn('id="scan-engine-mode"', html)
        self.assertIn('id="scan-engine-save"', html)
        self.assertIn('value="vulnagent"', html)
        self.assertIn('value="dsh"', html)
        # 它有自己的分组与导航（设置页契约：一组一张卡、顺序与导航一致）
        self.assertIn('data-settings-group="scan"', html)
        self.assertIn('data-settings-group="scan">扫描执行</button>', html)

    def test_the_card_states_the_evidence_difference(self):
        """不给这句，使用者会以为两档只是换了个扫描器。"""
        html = self._source("index.html")
        self.assertIn("未证实（静态）", html)
        self.assertIn("不会偷偷回退", html)

    def test_settings_js_saves_and_renders_the_choice(self):
        js = self._source("settings.js")
        self.assertIn("renderScanEngine", js)
        self.assertIn("'/scan-engine'", js)
        self.assertIn("data.scanEngine", js)
        self.assertIn("saveScanEngine", js)

    def test_job_card_shows_which_engine_ran_the_scan(self):
        """两条路的结论可信度不同，任务卡上不能只写「扫描」不写谁跑的。"""
        js = self._source("app.js")
        self.assertIn("payload.scanEngine", js)
        self.assertIn("推理引擎（DSH）", js)
        self.assertIn("发掘引擎（vulnagent）", js)
        self.assertIn("未做差分验证", js)


if __name__ == "__main__":
    unittest.main()
