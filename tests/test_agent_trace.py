# -*- coding: utf-8 -*-
"""智能体协作轨迹：七角色恒定齐备、阶段耗时归因、无动作角色兜底。

轨迹是"多 Agent 执行"唯一能被外部核对的产物，因此主力用例跑一次**真实扫描**
再断言结果，而不是把构造好的 timeline 喂给聚合函数——后者只能证明算术，
证明不了"引擎确实按七个角色往黑板写了记录"。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agent, engine                             # noqa: E402

ROLES = ["recon", "planner", "auditor", "critic",
         "verifier", "chain", "reporter"]


def demo_target():
    # 引擎现在在工作台目录之内（`<平台根>/prototype`）。用 `locate_engine()`
    # 而不是自己拼路径：引擎位置是 `engine.py` 的单一判定点，测试另拼一份
    # 就等于把"引擎在哪"这件事又抄了一遍，搬目录时必然有一处漏改。
    return os.path.join(engine.locate_engine(), "demo", "webapp")


class TraceShapeTestCase(unittest.TestCase):
    """纯聚合语义，不依赖引擎与网络。"""

    def test_seven_roles_present_even_without_timeline(self):
        trace = engine.build_agent_trace({"timeline": []})
        self.assertEqual([t["role"] for t in trace], ROLES)
        self.assertTrue(all(t["state"] == "idle" for t in trace))
        # 每个角色都要有可读的名字与职责，否则界面只能显示裸标识符
        self.assertTrue(all(t["label"] and t["duty"] for t in trace))

    def test_elapsed_ms_is_stage_delta(self):
        timeline = [
            {"agent": "recon", "message": "仓库测绘完成", "ts": 100},
            {"agent": "planner", "message": "生成审计计划", "ts": 250},
            {"agent": "auditor", "message": "切片审计完成", "ts": 900,
             "hypotheses": 1},
        ]
        trace = {t["role"]: t for t in engine.build_agent_trace({"timeline": timeline})}
        self.assertEqual(trace["recon"]["elapsedMs"], 100)
        self.assertEqual(trace["planner"]["elapsedMs"], 150)
        self.assertEqual(trace["auditor"]["elapsedMs"], 650)
        # 缺席的角色不推进游标，否则后面的阶段会把空白时间算到自己头上
        self.assertEqual(trace["critic"]["elapsedMs"], 0)

    def test_trace_survives_missing_timestamp(self):
        """早期引擎写的记录没有 ts：缺字段只能退化成 0，不能抛异常。"""
        trace = engine.build_agent_trace(
            {"timeline": [{"agent": "recon", "message": "仓库测绘完成"}]})
        self.assertEqual(trace[0]["state"], "done")
        self.assertEqual(trace[0]["elapsedMs"], 0)

    def test_repeated_actions_are_aggregated_into_one_row(self):
        timeline = [
            {"agent": "auditor", "message": "切片审计完成", "ts": 10, "hypotheses": 2},
            {"agent": "auditor", "message": "切片审计完成", "ts": 30, "hypotheses": 1},
            {"agent": "critic", "message": "假设质询完成", "ts": 40, "keep": True},
            {"agent": "critic", "message": "假设质询完成", "ts": 50, "keep": False},
        ]
        trace = {t["role"]: t for t in engine.build_agent_trace({"timeline": timeline})}
        self.assertEqual(trace["auditor"]["actions"], 2)
        self.assertIn("产出假设 3 条", trace["auditor"]["summary"])
        self.assertIn("保留 1 条、剔除 1 条", trace["critic"]["summary"])


class TraceIntegrationTestCase(unittest.TestCase):
    """跑一次真实扫描，断言轨迹确实来自引擎。"""

    @classmethod
    def setUpClass(cls):
        if not engine.engine_available():
            raise unittest.SkipTest("未找到发掘引擎（vulnagent）")
        cls.target = demo_target()
        if not os.path.isdir(cls.target):
            raise unittest.SkipTest("演示目标不存在：%s" % cls.target)
        with tempfile.TemporaryDirectory() as work:
            # 记忆库落临时目录：引擎默认值是相对路径，跑测试时不显式给就会
            # 在项目根目录写一个 `memory/`（曾经真的长回来过）。
            cls.result = engine.run_scan(
                cls.target, output_dir=os.path.join(work, "out"),
                memory_path=os.path.join(work, "cases.db"))
        cls.trace = engine.build_agent_trace(cls.result)

    def test_roles_in_collaboration_order(self):
        self.assertEqual([t["role"] for t in self.trace], ROLES)

    def test_first_and_last_stage_match_the_pipeline(self):
        active = [t for t in self.trace if t["state"] == "done"]
        self.assertGreaterEqual(len(active), 5, [t["role"] for t in active])
        # 顺序即协作结构：侦察必然最先，报告必然最后
        self.assertEqual(active[0]["role"], "recon")
        self.assertEqual(active[-1]["role"], "reporter")

    def test_stage_totals_stay_within_scan_time(self):
        total = sum(t["elapsedMs"] for t in self.trace)
        elapsed = (self.result.get("budget_snapshot") or {}).get("elapsed_ms", 0)
        self.assertGreater(total, 0)
        # 阶段耗时之和不可能超过整轮扫描；留 1s 余量给落盘与收尾
        self.assertLessEqual(total, elapsed + 1000)

    def test_reporter_row_summarizes_deliverables(self):
        self.assertIn("交付", self.trace[-1]["summary"])


class TraceDeliveryTestCase(unittest.TestCase):
    """轨迹要真的被回传，而不是算完就扔。"""

    def test_execute_job_returns_agent_trace(self):
        if not engine.engine_available():
            self.skipTest("未找到发掘引擎（vulnagent）")
        target = demo_target()
        if not os.path.isdir(target):
            self.skipTest("演示目标不存在：%s" % target)
        with tempfile.TemporaryDirectory() as work:
            os.makedirs(os.path.join(work, "out"), exist_ok=True)
            result = agent.execute_job(
                {"id": "job-test", "kind": "scan", "target": target,
                 "patched": "", "engagementId": "", "payload": {}}, work)
        self.assertEqual(result["status"], "done", result.get("error"))
        self.assertEqual([t["role"] for t in result["agents"]], ROLES)
        self.assertTrue(any(t["state"] == "done" for t in result["agents"]))


if __name__ == "__main__":
    unittest.main()
