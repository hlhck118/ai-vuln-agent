# -*- coding: utf-8 -*-
"""智能体的角色契约：路由各留记录、黑板记录带时间轴、无链也要留痕。

这三条都指向同一件事——"七个角色"必须可被外部核对。少任何一条，
工作台那边拿到的轨迹就会出现空洞，而空洞看起来和"这一环没跑"一样。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vulnagent.agents import (AgentContext, Blackboard,       # noqa: E402
                              ChainAgent)
from vulnagent.config import ModelConfig                      # noqa: E402
from vulnagent.llm import Router                              # noqa: E402


class RouterRoleTestCase(unittest.TestCase):
    """假设生成与反证质询必须各留一条路由记录。

    此前 `critic()` 直接 `return self.hypothesizer()`，于是 routes 里只有
    hypothesis 一条：文档宣称"按角色路由"，报告里却查不到反证阶段的推理实现，
    也就回答不了"这条假设是被谁用什么实现驳回的"。
    """

    def test_hypothesis_and_critic_each_record_a_route(self):
        router = Router(ModelConfig())
        router.hypothesizer()
        router.critic()
        self.assertEqual([r["role"] for r in router.routes],
                         ["hypothesis", "critic"])

    def test_both_roles_resolve_to_an_implementation(self):
        router = Router(ModelConfig())
        self.assertIsNotNone(router.hypothesizer().name)
        self.assertIsNotNone(router.critic().name)


class BlackboardTimelineTestCase(unittest.TestCase):

    def test_log_carries_timestamp_and_extras(self):
        bb = Blackboard(run_id="R-test", target_root=ROOT)
        bb.log("recon", "仓库测绘完成", files=3)
        self.assertEqual(len(bb.timeline), 1)
        entry = bb.timeline[0]
        self.assertEqual(entry["agent"], "recon")
        self.assertEqual(entry["files"], 3)
        self.assertIn("ts", entry)
        self.assertGreaterEqual(entry["ts"], 0)


class ChainAgentTimelineTestCase(unittest.TestCase):
    """没有交付项时也要留痕，否则这一环在轨迹里凭空消失。

    "利用链阶段没跑"和"跑了但无链可组"是两个不同的结论，轨迹必须能区分。
    """

    def test_logs_even_without_findings(self):
        bb = Blackboard(run_id="R-test", target_root=ROOT)
        ctx = AgentContext(config=None, reasoner=None, verifier=None,
                           tools=None, memory=None, budget=None)
        ChainAgent(ctx).run(bb)
        self.assertEqual(len(bb.timeline), 1)
        self.assertEqual(bb.timeline[0]["agent"], "chain")
        self.assertEqual(bb.timeline[0]["chains"], 0)


if __name__ == "__main__":
    unittest.main()
