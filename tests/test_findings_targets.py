# -*- coding: utf-8 -*-
"""漏洞页按目标分组：左栏选范围，右栏要么是那份台账，要么是全部目标的看板。

## 守的是什么

使用者说的是"同时多个会话对不同目标渗透时，分组显示"——真实场景里一次作业就是
几个目标同时在打。此前漏洞页只有一个范围：`state.eid`。于是「全部目标」这件事在
界面上根本不存在，读的人只能一个个点过去，还得自己在脑子里合并。

这一页现在有两条渲染路径，它们的**判据不同**，写错一条就会静默串味：

* 单目标：`state.findings` / `state.findingGroups` 来自 `/engagements/{eid}/findings`；
* 全部目标：`state.findingsByTarget` 来自 `/findings`，每个目标一段，段内再分
  已证实 / 未证实。

最容易犯的两个错都用断言钉住：

1. **切页签仍然强制重拉**（`loadTabData` 里两条分支都必须带 `true`）——使用者报过
   "漏洞、资产不能自动加载，需要刷新"；
2. **跨目标视图要能自己更新**。`findings` 指纹只数当前目标，新漏洞落在别的目标上
   时它不动；那种情况下只有节点指纹会动，所以「全部目标」必须额外盯 `nodes`。
   漏掉这一条的症状是：另一台机器打出了洞，看板上什么都不变。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

WEB_DIR = os.path.join(ROOT, "web")


def _read(name: str) -> str:
    with open(os.path.join(WEB_DIR, name), encoding="utf-8") as handle:
        return handle.read()


class FindingsTargetRailTestCase(unittest.TestCase):
    """左栏是静态骨架 + 一次 `/findings` 聚合快照。"""

    @classmethod
    def setUpClass(cls):
        cls.html = _read("index.html")
        cls.app = _read("app.js")
        cls.css = _read("style.css")
        cls.split = _read("split.js")
        cls.server = _read(os.path.join(ROOT, "pentestd", "server.py"))

    def body(self, marker: str) -> str:
        """切出一个函数体：从 marker 到下一个顶层 `function`（跳过注释里的空行）。"""
        start = self.app.index(marker)
        stop = self.app.find("\nfunction ", start + 1)
        return self.app[start:] if stop < 0 else self.app[start:stop]

    def test_the_left_rail_is_a_static_pane_of_its_own(self):
        self.assertIn('class="split-pane findings-side" id="findings-side"', self.html)
        self.assertIn('id="findings-targets"', self.html)
        self.assertIn('class="split-handle" data-split="findings"', self.html,
                      "手柄的 class 必须写在 data-split 之前（契约测试按这个顺序抓）")
        self.assertIn('class="split-pane findings-main"', self.html)
        self.assertLess(self.html.index("findings-side"),
                        self.html.index("findings-main"),
                        "左栏必须排在正文之前")

    def test_the_pane_has_its_own_drag_token_with_a_default(self):
        self.assertIn("id: 'findings'", self.split)
        self.assertIn("token: '--split-findings'", self.split)
        self.assertIn("--split-findings: 300px;", self.css)
        # split.js 的 def 与 :root 的默认值必须是同一个数，否则第一次拖拽会跳。
        self.assertIn("def: 300 }", self.split)
        self.assertIn("flex: 0 0 var(--split-findings, 300px);", self.css)

    def test_the_all_targets_view_has_its_own_render_path(self):
        paint = self.body("function paintFindings(")
        self.assertIn("if (state.findingsScope === '') { paintGroupedFindings(wrap); return; }",
                      paint, "「全部目标」落回了单目标的台账渲染")
        grouped = self.body("function paintGroupedFindings(")
        self.assertIn("bucket.target", grouped, "分组段头没写目标名")
        self.assertIn("bucket.assetLabels", grouped,
                      "跨目标卡片没做资产名翻译：会退化成 asset-xxxx")
        self.assertIn("paintFindingSections(", grouped,
                      "分组段内没有再分已证实 / 未证实")

    def test_the_rail_row_calls_select_engagement_for_a_real_target(self):
        pick = self.body("async function selectFindingsScope(")
        self.assertIn("await selectEngagement(next);", pick,
                      "选具体目标没走 selectEngagement：漏洞 / 资产 / 报告会各看各的")
        self.assertIn("await loadAllFindings(false);", pick,
                      "回到「全部目标」时没取聚合快照")
        row = self.body("function targetRow(")
        self.assertIn("dataset.targetScope = scope", row)
        self.assertIn("aria-current", row, "选中行没有给读屏器留标记")

    def test_the_aggregate_route_exists_on_the_server(self):
        self.assertIn('r.add("GET", "/api/v1/findings"', self.server)
        self.assertIn("def get_all_findings(", self.server)

    def test_a_tab_switch_still_reloads_every_scope_and_the_assets(self):
        tab = self.body("function loadTabData(")
        self.assertIn("loadAssets(true);", tab)
        self.assertIn("if (state.findingsScope === '') loadAllFindings(true); else loadFindings(true);",
                      tab, "切回漏洞页签时信任了缓存")

    def test_the_all_targets_view_also_follows_the_node_stamp(self):
        pulse = self.body("function pumpPulse(")
        self.assertIn("state.findingsScope === '' && changed('nodes')", pulse,
                      "新漏洞落在别的目标上时，看板不会动")
        loader = self.body("function loadAllFindings(")
        self.assertIn("state.allFindingsInflight", loader,
                      "并发请求没有去重：心跳与切页签会各发一次")

    def test_the_chip_counts_read_the_same_scope_as_the_cards(self):
        source = self.body("function severitySource(")
        self.assertIn("state.findingsScope === ''", source)
        self.assertIn("state.findingsByTarget", source,
                      "芯片数的是当前目标的记录，卡片却是全部目标的")

    def test_an_empty_engagement_list_still_draws_the_rail(self):
        empty = self.body("function renderNoEngagement(")
        self.assertIn("state.findingsScope = '';", empty)
        self.assertIn("renderFindingsTargets();", empty,
                      "删光目标后左栏停在旧行上")


if __name__ == "__main__":
    unittest.main(verbosity=2)
