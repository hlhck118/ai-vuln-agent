# -*- coding: utf-8 -*-
"""端到端与单元测试。

覆盖方案书中承诺的四类验证：
  1. 检出能力：注入的 6 类缺陷是否被全部发现；
  2. 误报控制：修复版是否零交付（修复版不应再报出可利用漏洞）；
  3. 差分验证：同一用例在两版上的行为差异是否被正确判定；
  4. 工程约束：预算上限、沙箱超时、记忆库复用、模型输出解析。

运行： python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from vulnagent.analysis import parse_target                    # noqa: E402
from vulnagent.config import AppConfig                          # noqa: E402
from vulnagent.domain import Hypothesis                         # noqa: E402
from vulnagent.integrations.dsh_pentest import (                # noqa: E402
    ASSET_TYPES, FACT_KINDS, SEVERITIES, export_from_run_dir, validate_replay)
from vulnagent.llm import RuleReasoner, extract_json            # noqa: E402
from vulnagent.memory import MemoryStore                        # noqa: E402
from vulnagent.orchestrator import Orchestrator                 # noqa: E402
from vulnagent.incremental import (build_manifest, diff_manifests,  # noqa: E402
                                   impacted_units)
from vulnagent.report import write_report                       # noqa: E402
from vulnagent.resolver import build_call_graph                  # noqa: E402
from vulnagent.tools import SandboxRunner, DifferentialVerifier  # noqa: E402


def _locate_pentest_plugin():
    """在 DSH profile 中定位已安装的 dsh-pentest 插件入口。"""
    home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    profiles = os.path.join(home, "profiles")
    if not os.path.isdir(profiles):
        return None
    for name in os.listdir(profiles):
        candidate = os.path.join(profiles, name, "node_modules", "@howmp",
                                 "dsh-pentest", "lib", "pentest.js")
        if os.path.exists(candidate):
            return candidate
    return None

VULN = os.path.join(ROOT, "demo", "vulnapp")
PATCHED = os.path.join(ROOT, "demo", "vulnapp_patched")
EXPECTED_CWES = {"CWE-89", "CWE-78", "CWE-22", "CWE-502", "CWE-327", "CWE-798"}


def make_config(tmpdir: str) -> AppConfig:
    cfg = AppConfig()
    cfg.model.provider = "heuristic"
    cfg.run.output_dir = tmpdir
    cfg.memory.db_path = os.path.join(tmpdir, "cases.db")
    cfg.tools.enabled = ["builtin_rules", "repo_map", "ast_graph",
                         "sandbox_exec", "poc_generator"]
    cfg.sandbox.timeout_s = 15
    return cfg


class TestRuleEngine(unittest.TestCase):
    """第 1 类：静态分析能力。"""

    def test_detects_all_planted_cwes(self):
        repo, graph, units, sinks, slices = parse_target(
            VULN, [".py"], [".git", "__pycache__"])
        found = {s.cwe for s in graph.sinks}
        self.assertTrue(EXPECTED_CWES.issubset(found),
                        "未能检出全部缺陷类型，实际：%s" % sorted(found))
        self.assertEqual(len(slices), 6, "切片数量应为 6（5 个函数 + 1 个模块级）")

    def test_patched_target_has_no_rule_hits(self):
        _, graph, _, _, slices = parse_target(PATCHED, [".py"], [".git", "__pycache__"])
        self.assertEqual(graph.sinks, [], "修复版不应再命中规则")
        self.assertEqual(slices, [], "修复版不应产生送审切片")

    def test_call_graph_and_reachability(self):
        _, graph, _, _, _ = parse_target(VULN, [".py"], [".git", "__pycache__"])
        self.assertGreaterEqual(graph.functions, 7)
        self.assertGreaterEqual(graph.calls, 5)
        self.assertTrue(graph.entry_points, "应识别出至少一个外部入口")
        self.assertTrue(graph.reachable_from_entry, "应计算出入口可达集合")


class TestRuleReasoner(unittest.TestCase):
    """第 4 类：无模型时的确定性推理。"""

    def test_propose_binds_evidence_and_preconditions(self):
        _, graph, _, _, slices = parse_target(VULN, [".py"], [".git", "__pycache__"])
        sl = [s for s in slices if s.symbol == "find_user"][0]
        from vulnagent.llm import HypothesisContext
        ctx = HypothesisContext(slice=sl, sinks=[s for s in graph.sinks
                                                 if s.symbol == "find_user"],
                                reachable=True)
        items = RuleReasoner().propose(ctx)
        self.assertTrue(items)
        self.assertEqual(items[0]["cwe"], "CWE-89")
        self.assertTrue(items[0]["preconditions"])
        self.assertGreater(items[0]["confidence"], 0.5)

    def test_challenge_drops_sanitized_path(self):
        _, graph, _, _, slices = parse_target(VULN, [".py"], [".git", "__pycache__"])
        sl = [s for s in slices if s.symbol == "read_doc"][0]
        from vulnagent.llm import HypothesisContext
        ctx = HypothesisContext(slice=sl, sinks=[], reachable=True,
                                sanitizers=["路径只取文件名"])
        out = RuleReasoner().challenge({"cwe": "CWE-22", "confidence": 0.7}, ctx)
        self.assertFalse(out["keep"], "存在消毒器时应推翻路径穿越假设")


class TestLLMParsing(unittest.TestCase):
    def test_extract_json_tolerates_fences(self):
        text = '说明文字\n```json\n[{"cwe":"CWE-89","line":12}]\n```\n结束'
        self.assertEqual(extract_json(text)[0]["cwe"], "CWE-89")

    def test_extract_json_object(self):
        self.assertTrue(extract_json('{"keep": false}')["keep"] is False)


class TestSandboxAndDifferential(unittest.TestCase):
    """第 3 类：沙箱执行与差分判定。"""

    def test_differential_verifies_sql_injection(self):
        cfg = AppConfig()
        cfg.sandbox.timeout_s = 15
        runner = SandboxRunner(cfg.sandbox)
        verifier = DifferentialVerifier(runner, None, PATCHED)
        hyp = Hypothesis(hyp_id="H-test", slice_id="S", cwe="CWE-89",
                         title="t", file=os.path.join(VULN, "userservice.py"),
                         line=39, sink_symbol="find_user")
        outcome = verifier.verify(hyp, VULN)
        self.assertEqual(outcome["verdict"], "verified")
        self.assertTrue(outcome["observed"]["effect_on_vulnerable"])
        self.assertFalse(outcome["observed"]["effect_on_patched"])

    def test_unknown_cwe_falls_back_to_static(self):
        cfg = AppConfig()
        runner = SandboxRunner(cfg.sandbox)
        verifier = DifferentialVerifier(runner, None, None)
        hyp = Hypothesis(hyp_id="H2", slice_id="S", cwe="CWE-999", title="t",
                         file=os.path.join(VULN, "userservice.py"), line=1,
                         sink_symbol="x")
        self.assertEqual(verifier.verify(hyp, VULN)["verdict"], "confirmed_static")

    def test_poc_execution_error_is_not_treated_as_false_positive(self):
        """用例因签名不匹配而执行失败时，必须转人工，不能判为误报。"""
        cfg = AppConfig()
        runner = SandboxRunner(cfg.sandbox)
        verifier = DifferentialVerifier(runner, None, None)
        # render_banner 需要一个字符串参数，这里故意指向无参函数 get_conn
        hyp = Hypothesis(hyp_id="H3", slice_id="S", cwe="CWE-78", title="t",
                         file=os.path.join(VULN, "userservice.py"), line=44,
                         sink_symbol="get_conn")
        outcome = verifier.verify(hyp, VULN)
        self.assertEqual(outcome["verdict"], "inconclusive",
                         "用例没能执行完时不得判定为误报")

    def test_sandbox_timeout_is_reported(self):
        cfg = AppConfig()
        cfg.sandbox.timeout_s = 2
        runner = SandboxRunner(cfg.sandbox)
        result = runner.run_poc("import time\ntime.sleep(30)\n", VULN)
        self.assertTrue(result.timed_out)
        self.assertIn("超时", result.error)


class TestMemory(unittest.TestCase):
    def test_record_and_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(os.path.join(tmp, "m.db"))
            store.start_run("R1", "demo", "hash")
            hyp = Hypothesis(hyp_id="H1", slice_id="S", cwe="CWE-89", title="SQL 注入",
                             file="a.py", line=10, sink_symbol="find_user",
                             confidence=0.8, rationale="字符串拼接构造查询")
            store.record_hypothesis("R1", hyp)
            store.record_verdict("H1", "verified", "dynamic", "差分成立", {})
            hits = store.search_similar("CWE-89", "SELECT name FROM users WHERE name = '%s'")
            self.assertTrue(hits)
            self.assertEqual(hits[0]["verdict"], "verified")
            self.assertGreaterEqual(store.stats()["verified"], 1)
            store.close()


class TestOrchestratorEndToEnd(unittest.TestCase):
    """第 1、2 类：全流程检出与误报控制。"""

    def test_full_scan_delivers_verified_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            orch = Orchestrator(cfg, patched_root=PATCHED,
                                memory_path=cfg.memory.db_path)
            try:
                result = orch.scan(VULN)
            finally:
                orch.close()
            cwes = {f.cwe for f in result.findings}
            self.assertTrue(EXPECTED_CWES.issubset(cwes),
                            "未能交付全部缺陷，实际：%s" % sorted(cwes))
            verified = [v for v in result.verifications if v.verdict == "verified"]
            self.assertEqual(len(verified), 5,
                             "应有 5 条通过差分验证（弱哈希自 v0.3 起可动态验证）")
            self.assertTrue(result.chains, "应产出攻击链")
            self.assertEqual(result.degraded, [], "离线确定性模式下不应有降级")
            self.assertEqual(len(result.hypotheses), 6)

    def test_patched_target_yields_zero_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            orch = Orchestrator(cfg, patched_root=None,
                                memory_path=cfg.memory.db_path)
            try:
                result = orch.scan(PATCHED)
            finally:
                orch.close()
            self.assertEqual(result.findings, [], "修复版不应产出任何交付项")

    def test_budget_stops_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            cfg.budget.max_rounds = 1
            orch = Orchestrator(cfg, patched_root=PATCHED,
                                memory_path=cfg.memory.db_path)
            try:
                result = orch.scan(VULN)
            finally:
                orch.close()
            self.assertLessEqual(len([h for h in result.hypotheses]), 3)
            self.assertTrue(result.budget_snapshot["stopped_reason"])


class TestCallGraphResolver(unittest.TestCase):
    """符号解析：跨文件可达性、同名方法消歧、未解析不猜。"""

    def setUp(self):
        self.webapp = os.path.join(ROOT, "demo", "webapp")

    def _graph(self, root):
        files, sources = [], {}
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    path = os.path.join(dirpath, fn)
                    files.append(path)
                    with open(path, encoding="utf-8") as handle:
                        sources[path] = handle.read()
        return build_call_graph(files, sources)

    def test_cross_file_reachability(self):
        graph = self._graph(self.webapp)
        reached = {k.replace(self.webapp + os.sep, "") for k in graph.reachable}
        # 入口在 controller.py，缺陷在 dao.py：跨文件可达性必须成立
        self.assertIn("controller.py::handle", reached)
        self.assertIn("service.py::load_profile", reached)
        self.assertIn("dao.py::find_user", reached)

    def test_same_name_method_disambiguation(self):
        graph = self._graph(self.webapp)
        callees = {}
        for src, dst in graph.edges:
            callees.setdefault(src.replace(self.webapp + os.sep, ""), set()).add(
                dst.replace(self.webapp + os.sep, ""))
        # 两个 DAO 都有 save：必须各自解析到自己的类，不能串味
        self.assertIn("dao.py::UserDao.save",
                      callees.get("service.py::ProfileService.persist", set()))
        self.assertNotIn("dao.py::OrderDao.save",
                         callees.get("service.py::ProfileService.persist", set()))
        self.assertIn("dao.py::OrderDao.save",
                      callees.get("service.py::ProfileService.archive", set()))
        self.assertNotIn("dao.py::UserDao.save",
                         callees.get("service.py::ProfileService.archive", set()))

    def test_key_format_is_consistent(self):
        """调用方与目标必须同为 路径::限定名，否则可达性会静默失效。"""
        graph = self._graph(self.webapp)
        for src, dst in graph.edges:
            self.assertIn("::", src)
            self.assertIn("::", dst)
            self.assertTrue(os.path.isabs(src.split("::")[0]),
                            "调用方键应以文件路径开头: %s" % src)
            self.assertTrue(os.path.isabs(dst.split("::")[0]),
                            "目标键应以文件路径开头: %s" % dst)

    def test_unknown_calls_are_not_guessed(self):
        """动态调用必须记为 unknown，不能按名字硬猜。"""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "alpha.py"), "w", encoding="utf-8") as handle:
                handle.write("def save(row):\n    return row\n")
            with open(os.path.join(tmp, "beta.py"), "w", encoding="utf-8") as handle:
                handle.write(
                    "import alpha\n\n"
                    "def save(row):\n    return row\n\n"
                    "def caller(name):\n"
                    "    fn = getattr(alpha, name)\n"
                    "    return fn()\n\n"
                    "def direct():\n"
                    "    return alpha.save(1)\n")
            graph = self._graph(tmp)
            targets = {s.raw: s for s in graph.sites}
            self.assertEqual(targets["fn"].kind, "unknown",
                             "通过变量调用的目标无法静态确定，必须记为 unknown")
            self.assertTrue(targets["alpha.save"].target.endswith("alpha.py::save"),
                            "跨模块调用必须解析到 alpha.py::save")


class TestSandboxBackend(unittest.TestCase):
    """容器沙箱：隔离参数、可用性探测与显式降级。"""

    def test_docker_command_carries_isolation_flags(self):
        cfg = AppConfig()
        cfg.sandbox.backend = "docker"
        runner = SandboxRunner(cfg.sandbox)
        joined = " ".join(runner._docker_cmd("/work"))
        for flag in ("--network none", "--read-only", "--pids-limit",
                     "--memory", "--security-opt no-new-privileges", "--cpus"):
            self.assertIn(flag, joined, "容器命令缺少隔离参数: %s" % flag)

    def test_backend_info_reports_effective_backend(self):
        cfg = AppConfig()
        cfg.sandbox.backend = "docker"
        info = SandboxRunner(cfg.sandbox).backend_info()
        self.assertIn(info["effective_backend"], ("docker", "subprocess"))
        self.assertEqual(info["isolated"], info["effective_backend"] == "docker")

    def test_unavailable_docker_degrades_explicitly(self):
        cfg = AppConfig()
        cfg.sandbox.backend = "docker"
        runner = SandboxRunner(cfg.sandbox)
        if runner.docker_available():
            self.skipTest("本机存在可用的容器运行时，跳过降级路径测试")
        result = runner.run_poc('import sys\nprint(\'VULNAGENT_RESULT {"ok": 1}\')\n', VULN)
        self.assertEqual(result.backend, "subprocess")
        self.assertTrue(runner.degraded, "容器不可用时必须留下降级记录")
        self.assertIn("未检测到可用的容器运行时", runner.degraded[0])

    def test_no_fallback_failure_never_becomes_false_negative(self):
        """严格模式下容器缺失时，绝不能把"没跑成"当成"没复现"。"""
        cfg = AppConfig()
        cfg.sandbox.backend = "docker"
        runner = SandboxRunner(cfg.sandbox, allow_fallback=False)
        if runner.docker_available():
            self.skipTest("本机存在可用的容器运行时，跳过失败路径测试")
        result = runner.run_poc("print('VULNAGENT_RESULT {}')\n", VULN)
        self.assertFalse(result.ok)
        self.assertIn("容器运行时", result.error)
        verifier = DifferentialVerifier(runner, None, PATCHED)
        hyp = Hypothesis(hyp_id="H-sbx", slice_id="S", cwe="CWE-89", title="t",
                         file=os.path.join(VULN, "userservice.py"),
                         line=39, sink_symbol="find_user")
        outcome = verifier.verify(hyp, VULN)
        self.assertEqual(outcome["verdict"], "inconclusive",
                         "沙箱不可用时必须转人工确认，而不是判为误报")


class TestMultiFileTarget(unittest.TestCase):
    """跨文件调用链的端到端检出与差分验证。"""

    def test_webapp_sql_injection_verified_across_files(self):
        webapp = os.path.join(ROOT, "demo", "webapp")
        patched = os.path.join(ROOT, "demo", "webapp_patched")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            orch = Orchestrator(cfg, patched_root=patched,
                                memory_path=cfg.memory.db_path)
            try:
                result = orch.scan(webapp)
            finally:
                orch.close()
            self.assertEqual(len(result.findings), 1)
            finding = result.findings[0]
            self.assertEqual(finding.cwe, "CWE-89")
            self.assertEqual(finding.verification.verdict, "verified")
            self.assertTrue(finding.file.endswith("dao.py"))
            self.assertGreaterEqual(result.graph.resolution_rate, 0.4)
            self.assertIn("dao.py", "".join(result.graph.reachable_from_entry))


WEBSVC = os.path.join(ROOT, "demo", "websvc")
WEBSVC_PATCHED = os.path.join(ROOT, "demo", "websvc_patched")


class TestWebsvcEndToEnd(unittest.TestCase):
    """v0.3 新增的四类 PoC 模板：XSS、代码注入、弱哈希、SSRF。"""

    def test_four_new_types_all_dynamically_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            orch = Orchestrator(cfg, patched_root=WEBSVC_PATCHED,
                                memory_path=cfg.memory.db_path)
            try:
                result = orch.scan(WEBSVC)
            finally:
                orch.close()
            verdicts = {f.cwe: f.verification.verdict for f in result.findings}
            self.assertEqual(set(verdicts), {"CWE-79", "CWE-95", "CWE-327", "CWE-918"})
            for cwe, verdict in verdicts.items():
                self.assertEqual(verdict, "verified",
                                 "%s 应通过差分验证，实际 %s" % (cwe, verdict))

    def test_patched_websvc_yields_zero_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            orch = Orchestrator(cfg, patched_root=None, memory_path=cfg.memory.db_path)
            try:
                result = orch.scan(WEBSVC_PATCHED)
            finally:
                orch.close()
            self.assertEqual(result.findings, [], "修复版不应产出任何交付项")

    def test_weak_hash_verification_is_objective(self):
        """弱哈希的判定应基于"常见口令摘要可被直接还原"，而非主观判断。"""
        cfg = AppConfig()
        runner = SandboxRunner(cfg.sandbox)
        verifier = DifferentialVerifier(runner, None, WEBSVC_PATCHED)
        hyp = Hypothesis(hyp_id="H-327", slice_id="S", cwe="CWE-327",
                         title="弱哈希", file=os.path.join(WEBSVC, "portal.py"),
                         line=29, sink_symbol="sign")
        outcome = verifier.verify(hyp, WEBSVC)
        self.assertEqual(outcome["verdict"], "verified")
        self.assertTrue(outcome["observed"]["effect_on_vulnerable"])


class TestIncrementalScan(unittest.TestCase):
    """增量扫描：影响面裁剪、无变更跳过、删除计入变更。"""

    ALPHA = "import beta\n\n\ndef handle():\n    return beta.work()\n"
    BETA = ("import subprocess\n\n\ndef work():\n"
            "    return helper()\n\n\ndef helper():\n"
            "    return subprocess.run('echo ' + 'x', shell=True)\n")
    GAMMA = "import hashlib\n\n\ndef unrelated(pw):\n    return hashlib.md5(pw.encode()).hexdigest()\n"
    DELTA = "import hashlib\n\n\ndef other(pw):\n    return hashlib.sha1(pw.encode()).hexdigest()\n"

    def _make_project(self, tmp):
        project = os.path.join(tmp, "proj")
        os.makedirs(project, exist_ok=True)
        for name, source in (("alpha.py", self.ALPHA), ("beta.py", self.BETA),
                             ("gamma.py", self.GAMMA), ("delta.py", self.DELTA)):
            with open(os.path.join(project, name), "w", encoding="utf-8") as handle:
                handle.write(source)
        return project

    def test_manifest_diff_detects_all_change_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            cfg = make_config(tmp)
            old = build_manifest(project, cfg.run.languages, cfg.run.exclude_dirs,
                                 cfg.run.max_file_bytes)
            with open(os.path.join(project, "gamma.py"), "a", encoding="utf-8") as handle:
                handle.write("\n# touched\n")
            os.remove(os.path.join(project, "delta.py"))
            with open(os.path.join(project, "epsilon.py"), "w", encoding="utf-8") as handle:
                handle.write("x = 1\n")
            new = build_manifest(project, cfg.run.languages, cfg.run.exclude_dirs,
                                 cfg.run.max_file_bytes)
            diff = diff_manifests(old, new)
            self.assertEqual(diff.modified, ["gamma.py"])
            self.assertEqual(diff.deleted, ["delta.py"])
            self.assertEqual(diff.added, ["epsilon.py"])

    def test_impact_analysis_limits_scope(self):
        """改动互不相关的模块时，只应审该模块自身。"""
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            cfg = make_config(tmp)
            repo, graph, units, sinks, slices = parse_target(
                project, cfg.run.languages, cfg.run.exclude_dirs)
            self.assertEqual(len(slices), 3, "beta/gamma/delta 各有一个切片")
            edges = [(e[0], e[1]) for e in graph.call_edges]
            impacted = impacted_units(["gamma.py"], project, edges, set(units))
            self.assertEqual(len(impacted), 1)
            selected = [s for s in slices
                        if "%s::%s" % (s.path, s.symbol) in impacted]
            self.assertEqual(len(selected), 1)
            self.assertTrue(selected[0].symbol.endswith("unrelated"))

    def test_no_change_skips_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            out = os.path.join(tmp, "out")
            cfg = make_config(tmp)
            cfg.run.output_dir = out
            baseline = os.path.join(out, "baseline.json")

            orch = Orchestrator(cfg, memory_path=cfg.memory.db_path)
            try:
                first = orch.scan(project, incremental=True, baseline_path=baseline)
            finally:
                orch.close()
            self.assertGreater(first.incremental["selected_slices"], 0,
                               "首次扫描无基线，应全量审计")

            orch = Orchestrator(cfg, memory_path=cfg.memory.db_path)
            try:
                second = orch.scan(project, incremental=True, baseline_path=baseline)
            finally:
                orch.close()
            self.assertEqual(second.incremental["diff"]["changed_count"], 0)
            self.assertEqual(second.incremental["selected_slices"], 0)
            self.assertEqual(second.hypotheses, [])

    def test_changed_file_is_audited_and_others_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            patched = os.path.join(tmp, "proj_patched")
            shutil.copytree(project, patched)
            out = os.path.join(tmp, "out")
            cfg = make_config(tmp)
            cfg.run.output_dir = out
            baseline = os.path.join(out, "baseline.json")

            orch = Orchestrator(cfg, memory_path=cfg.memory.db_path)
            try:
                orch.scan(project, incremental=True, baseline_path=baseline)
            finally:
                orch.close()

            # 把不相关的 delta.py 改成含命令注入的写法（保留参数并返回命令输出，
            # 与 PoC 模板的观测口径一致）
            injected = ("import subprocess\n\n\n"
                        "def other(text):\n"
                        "    return subprocess.run('echo ' + text, shell=True,\n"
                        "                          capture_output=True, text=True).stdout\n")
            with open(os.path.join(project, "delta.py"), "w", encoding="utf-8") as handle:
                handle.write(injected)
            with open(os.path.join(patched, "delta.py"), "w", encoding="utf-8") as handle:
                handle.write("import subprocess\n\n\n"
                             "def other(text):\n"
                             "    return subprocess.run(['echo', text], shell=False,\n"
                             "                          capture_output=True, text=True).stdout\n")

            orch = Orchestrator(cfg, patched_root=patched,
                                memory_path=cfg.memory.db_path)
            try:
                result = orch.scan(project, incremental=True, baseline_path=baseline)
            finally:
                orch.close()

            self.assertEqual(result.incremental["diff"]["changed_count"], 1)
            self.assertGreater(result.incremental["skipped_slices"], 0,
                               "未受影响的模块应被跳过")
            self.assertEqual(len(result.slices), 1, "只应审受影响的那个切片")
            self.assertTrue(result.findings, "变更文件中的缺陷必须被检出")
            for finding in result.findings:
                self.assertTrue(finding.file.endswith("delta.py"))
                self.assertEqual(finding.verification.verdict, "verified")


class TestDshPentestIntegration(unittest.TestCase):
    """与 @howmp/dsh-pentest 的集成契约：回放序列必须满足插件硬约束。"""

    def _run_scan(self, tmp):
        cfg = make_config(tmp)
        orch = Orchestrator(cfg, patched_root=PATCHED, memory_path=cfg.memory.db_path)
        try:
            result = orch.scan(VULN)
            write_report(result, cfg.run.output_dir)
        finally:
            orch.close()
        return cfg.run.output_dir

    def test_replay_built_from_run_dir_passes_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_scan(tmp)
            plan = export_from_run_dir(run_dir, authorization="测试授权")
            self.assertEqual(validate_replay(plan), [], "回放序列必须通过插件契约校验")
            self.assertEqual(plan["calls"][0]["tool"], "pentest_add_goal")
            self.assertEqual(plan["counts"]["findings"], 6)
            self.assertEqual(plan["counts"]["assets"], 1)

    def test_every_finding_has_reproducible_steps(self):
        """插件对 reproducibleSteps 有"至少一条"的硬约束，静态确认项也不例外。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_scan(tmp)
            plan = export_from_run_dir(run_dir)
            findings = [c for c in plan["calls"] if c["tool"] == "pentest_add_finding"]
            self.assertTrue(findings)
            for call in findings:
                self.assertGreaterEqual(len(call["args"]["reproducibleSteps"]), 1)
            static_calls = [c for c in findings
                            if "人工复核" in " ".join(c["args"]["reproducibleSteps"])]
            self.assertTrue(static_calls, "静态确认项应给出人工复核步骤")

    def test_ids_are_references_not_literals(self):
        """节点 id 由插件分配，回放序列只能用 $ref 占位，不能写死 id。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_scan(tmp)
            plan = export_from_run_dir(run_dir)
            blob = json.dumps(plan["calls"], ensure_ascii=False)
            for literal in ('"goal-1"', '"intent-1"', '"fact-1"', '"finding-1"'):
                self.assertNotIn(literal, blob, "不应出现写死的插件节点 id")
            refs = {c["ref"] for c in plan["calls"] if c.get("ref")}
            self.assertIn("goal", refs)
            self.assertIn("asset:root", refs)

    def test_attack_chain_uses_derived_from_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_scan(tmp)
            plan = export_from_run_dir(run_dir)
            derived = [c for c in plan["calls"] if c["tool"] == "pentest_add_intent"
                       and "derivedFromFactId" in c["args"]]
            self.assertTrue(derived, "攻击链应表示为 derived_from 意图")
            for call in derived:
                anchors = [k for k in ("goalId", "derivedFromFactId") if call["args"].get(k)]
                self.assertEqual(len(anchors), 1, "意图必须且只能有一个锚点")

    def test_validator_rejects_forward_reference(self):
        bad = {"calls": [
            {"tool": "pentest_add_goal", "ref": "goal", "args": {}},
            {"tool": "pentest_add_intent", "ref": "i1",
             "args": {"title": "t", "goalId": {"$ref": "goal"}}},
            {"tool": "pentest_add_finding", "ref": "f1",
             "args": {"intentId": {"$ref": "does-not-exist"}, "title": "t",
                      "severity": "high", "reproducibleSteps": ["s"]}},
        ]}
        problems = validate_replay(bad)
        self.assertTrue(any("未定义" in p for p in problems), problems)

    def test_validator_rejects_finding_without_steps(self):
        bad = {"calls": [
            {"tool": "pentest_add_goal", "ref": "goal", "args": {}},
            {"tool": "pentest_add_intent", "ref": "i1",
             "args": {"title": "t", "goalId": {"$ref": "goal"}}},
            {"tool": "pentest_add_finding", "ref": "f1",
             "args": {"intentId": {"$ref": "i1"}, "title": "t",
                      "severity": "high", "reproducibleSteps": []}},
        ]}
        problems = validate_replay(bad)
        self.assertTrue(any("reproducibleSteps" in p for p in problems), problems)

    def test_export_requires_findings_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                export_from_run_dir(tmp)

    def test_emitted_vocabulary_matches_plugin_enums(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_scan(tmp)
            plan = export_from_run_dir(run_dir)
            for call in plan["calls"]:
                if call["tool"] == "pentest_add_finding":
                    self.assertIn(call["args"]["severity"], SEVERITIES)
                if call["tool"] == "pentest_add_asset":
                    self.assertIn(call["args"]["type"], ASSET_TYPES)
                if call["tool"] == "pentest_add_fact":
                    self.assertIn(call["args"]["kind"], FACT_KINDS)

    def test_tool_and_parameter_names_match_installed_plugin(self):
        """对着已安装插件的源码校验工具名与参数名，防止插件升级后静默失配。"""
        plugin = _locate_pentest_plugin()
        if not plugin:
            self.skipTest("本机未安装 @howmp/dsh-pentest，跳过契约核对")
        with open(plugin, "r", encoding="utf-8", errors="ignore") as handle:
            source = handle.read()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_scan(tmp)
            plan = export_from_run_dir(run_dir)
        for call in plan["calls"]:
            self.assertIn('name: "%s"' % call["tool"], source,
                          "插件源码中不存在工具 %s" % call["tool"])
            for key in call["args"]:
                self.assertTrue(('%s: { type:' % key) in source
                                or ('%s: {' % key) in source,
                                "插件工具 %s 的参数里没有 %s" % (call["tool"], key))


if __name__ == "__main__":
    unittest.main(verbosity=2)
