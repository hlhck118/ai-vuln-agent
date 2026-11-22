# -*- coding: utf-8 -*-
"""报告窗口：只许截链路，不许截交付物。

## 守的是什么

`windowed_view` 按"最近 N 条记录"取（`DEFAULT_WINDOW`）。拿它给「三、探索链路」一节
封顶是对的：意图与事实会堆到几百条，全铺进报告没人读。

但漏洞与资产**不是"最近发生的事"**——它们是这份报告的交付物，而它们几乎总落在窗口
边界之前：补扫时先写事实与资产，漏洞在最后才落；或者本次只复验，漏洞是更早那次写的。
于是窗口把它们**整批**切掉。报告照常生成、格式完整、照常印一句「未记录到漏洞。」。

那不是空白，是一句假话。一个有 56 条漏洞的目标，交出去的文档说它没有漏洞；而读者
没有任何线索去怀疑它——文档里没有"另有 N 条未展示"这类提示，因为被切掉的正是
生成那句话所依据的统计本身。

所以这条边界必须钉住：**窗口只约束链路一节的篇幅，漏洞清单与资产永远取完整集合。**

## 三种"看起来都没问题"的失败形态

  * 漏洞整批消失，正文写「未记录到漏洞。」——像"扫了没发现"，不像"报告漏了"；
  * 资产整批消失，资产树是空的——像"这次没有资产"，不像"被截断了"；
  * 链路被截后本节一条意图都没有，正文写「本次未记录到意图。」——可本次**有**意图，
    只是它们不在窗口里。截断与"确实没有"必须分开说。
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import engine                               # noqa: E402
from pentestd import store as store_module                # noqa: E402
from pentestd.store import Store                          # noqa: E402

WINDOW = store_module.DEFAULT_WINDOW
HOST = "10.10.10.250"


class ReportWindowTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(os.path.join(self.tmp.name, "window.db"))
        self.eid = self.store.create_engagement(HOST)["id"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _intent(self, title="网络暴露面测绘"):
        return self.store.add_node(self.eid, "intent",
                                   {"title": title, "goalId": "goal-1"})

    def _facts(self, intent_id, count, host=HOST):
        """补扫的事实：数量足以把更早写下的东西挤出窗口。"""
        for i in range(count):
            self.store.add_node(self.eid, "fact", {
                "intentId": intent_id, "kind": "port",
                "detail": "%s 端口 %d 开放" % (host, i), "confidence": 0.9})

    def _assets(self, count):
        """晚写的一批资产：数量足以把更早的意图与事实挤出窗口。"""
        for i in range(count):
            self.store.add_node(self.eid, "asset", {
                "type": "ip", "value": "172.16.%d.%d" % (i // 250, i % 250)})

    def _finding(self, intent_id, title="配置接口未鉴权"):
        return self.store.add_node(self.eid, "finding", {
            "intentId": intent_id, "title": title, "severity": "high",
            "reproducibleSteps": ["curl -s http://%s/api/config" % HOST],
            "affectedAssetId": None})

    def test_a_finding_written_before_the_late_facts_is_still_in_the_report(self):
        """漏洞写在补扫事实之前——它必须还在报告里。

        这正是现场那个形态：56 条漏洞，报告写「未记录到漏洞。」。
        """
        intent = self._intent()
        finding = self._finding(intent["id"])
        self._facts(intent["id"], WINDOW + 40)

        md = self.store.report_markdown(self.eid, WINDOW)
        self.assertNotIn("未记录到漏洞。", md,
                         "窗口把漏洞整批切掉了：报告在说「没有漏洞」，"
                         "而这台目标上有 %s" % finding["id"])
        self.assertIn("配置接口未鉴权", md)
        self.assertIn("漏洞 1", md, "抬头统计也必须数到它")

    def test_an_asset_written_before_the_late_facts_is_still_in_the_report(self):
        """资产同理：空资产树会被读成「这次没有资产」。"""
        intent = self._intent()
        asset = self.store.add_node(self.eid, "asset",
                                    {"type": "ip", "value": "172.16.99.1"})
        self._facts(intent["id"], WINDOW + 40)

        md = self.store.report_markdown(self.eid, WINDOW)
        self.assertIn("172.16.99.1", md,
                      "窗口把资产切掉了：资产树会是空的，而目标上有 %s"
                      % asset["id"])

    def test_the_window_note_says_the_deliverable_is_complete(self):
        """截断说明必须交代清"哪一节被截了、哪一节是完整的"。

        只写"另有 N 条未展示"，读者无从知道漏洞清单是否也少了东西。
        """
        intent = self._intent()
        self._facts(intent["id"], WINDOW + 10)

        md = self.store.report_markdown(self.eid, WINDOW)
        self.assertIn("未在其中展示", md)
        self.assertIn("「四、漏洞清单」与「五、资产」是完整的", md)

    def test_a_truncated_chain_does_not_read_as_having_no_intent(self):
        """「本节里没有意图」不等于「本次没有意图」。

        `windowed_view` 会把被引用记录所需的锚点一并保留，所以"窗口里只剩事实"时
        意图通常还在。真正截空的一种情形是晚写的一批资产把整段链路挤出窗口——此时
        链路一节是空的，而**这次有意图**。若照旧写「本次未记录到意图。」，读者会
        以为这次执行根本没规划。
        """
        intent = self._intent()
        self.store.add_node(self.eid, "fact",
                            {"intentId": intent["id"], "kind": "port",
                             "detail": "%s 22 开放" % HOST, "confidence": 0.9})
        self._assets(WINDOW + 10)

        md = self.store.report_markdown(self.eid, WINDOW)
        self.assertNotIn("本次未记录到意图。", md,
                         "链路被截空时说成「本次没有意图」，把截断说成了事实")
        self.assertIn("其中没有意图节点", md)

    def test_a_task_report_keeps_its_findings_outside_the_window(self):
        """作业报告：本次写的漏洞，不能被**别的任务**写下的事实挤出窗口。

        窗口曾经是全局的：别的任务补了 200 条事实，这一篇作业报告就只剩空链路段，
        漏洞清单一并消失。
        """
        job = self.store.create_job("scan", HOST, self.eid, "", {})
        engine.write_chain(self.store, self.eid, [
            {"kind": "intent", "localRef": "i1",
             "payload": {"title": "配置判定", "goalId": "goal-1"}},
            {"kind": "finding",
             "payload": {"intentId": {"$ref": "i1"}, "title": "任务内发现的漏洞",
                         "severity": "high",
                         "reproducibleSteps": ["打开 http://%s/" % HOST]}},
        ], job_id=job["id"])

        other = self._intent("别的任务")
        self._facts(other["id"], WINDOW + 20)

        md = self.store.report_markdown(self.eid, WINDOW, job["id"])
        self.assertIn("任务内发现的漏洞", md,
                      "作业报告漏掉了本次自己写下的漏洞")
        self.assertNotIn("未记录到漏洞。", md)


if __name__ == "__main__":
    unittest.main()
