# -*- coding: utf-8 -*-
"""漏洞去重：分组判据、聚合字段，以及"一条都不能丢"。

要解决的那件事是真实的。目标 `E-4919b27c` 上四条记录里有三条逐字相同：

    finding-1  low  TLS 证书未通过校验  asset-13  ...:443    self-signed certificate
    finding-2  low  TLS 证书未通过校验  asset-28  ...:8443   unable to get local issuer
    finding-3  low  TLS 证书未通过校验  asset-47  ...:18888  self-signed in certificate chain

三条都对——同一个问题落在三个端口上的三次观测，各有自己的证书链错误与复现步骤。
但界面上那是三张标题、等级、配色完全一样的卡片，而真正该先看的那个 critical
被压到了第四张。所以这一层的目标是**一张卡片说清"这是一处问题、有三处证据"**，
而不是删掉两条。

三组断言：

1. **判据**（`models.finding_group_key` / `group_findings`）；
2. **聚合**（`count` / `assetIds` / 首见时间 / 组的先后顺序）；
3. **不丢**（接口返回的 `groups` 必须恰好覆盖 `findings` 每一条一次）。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import models, server                                     # noqa: E402
from tests import support                                              # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


def finding(ident, title="TLS 证书未通过校验", severity="low", asset="", at=0.0,
            intent="intent-2", description="", steps=("openssl s_client",)):
    return {"id": ident, "kind": "finding", "title": title, "severity": severity,
            "affectedAssetId": asset, "createdAt": at, "intentId": intent,
            "description": description, "reproducibleSteps": list(steps)}


def accident():
    """真实那四条，按 `store.findings` 的排序（等级升、同档时间新→旧）摆好。"""
    return [
        finding("finding-4", title="凭证有效：admin", severity="critical", at=600.0,
                intent="intent-63", description="以该账号登录成功"),
        finding("finding-3", at=500.0, asset="asset-47",
                description="self-signed certificate in certificate chain"),
        finding("finding-2", at=499.0, asset="asset-28",
                description="unable to get local issuer certificate"),
        finding("finding-1", at=498.0, asset="asset-13",
                description="self-signed certificate"),
    ]


class GroupingRulesTestCase(unittest.TestCase):

    def test_the_three_identical_titles_become_one_group(self):
        groups = models.group_findings(accident())
        self.assertEqual(2, len(groups), "四处记录、两处问题")
        self.assertEqual("凭证有效：admin", groups[0]["title"])
        self.assertEqual("TLS 证书未通过校验", groups[1]["title"])
        self.assertEqual(3, groups[1]["count"])

    def test_every_record_lands_in_exactly_one_group(self):
        """**一条都不能丢、也不能重复**。这是这一层唯一的硬约束。

        分组是派生视图，而派生视图最典型的坏法就是"悄悄少了几条"——界面上看不出来
        （卡片长得一样），只有人去核对记录数才发现。
        """
        items = accident()
        groups = models.group_findings(items)
        ids = [ident for group in groups for ident in group["ids"]]
        self.assertEqual(sorted(item["id"] for item in items), sorted(ids))

    def test_group_order_follows_the_record_order(self):
        """组的先后 = 组里最新那条的先后（输入已按等级、再按时间排过）。

        这条保证"最严重的排在最前面"这件事**在分组之后依然成立**——否则界面会把
        一个 critical 压到三张 low 卡片下面。
        """
        groups = models.group_findings(accident())
        self.assertEqual("critical", groups[0]["severity"])
        self.assertEqual(["finding-4"] , groups[0]["ids"])

    def test_the_assets_and_the_time_range_are_aggregated(self):
        group = models.group_findings(accident())[1]
        self.assertEqual(["asset-47", "asset-28", "asset-13"], group["assetIds"],
                         "按记录顺序去重，不排序——排序会让界面上的次序与记录对不上")
        self.assertEqual(500.0, group["createdAt"], "最新一条的时间")
        self.assertEqual(498.0, group["firstAt"], "最早一条的时间（首见）")
        self.assertEqual(["intent-2"], group["intentIds"])

    def test_the_same_title_with_a_different_severity_stays_separate(self):
        """等级一起进判据。

        同标题不同等级时合并，等于由平台替使用者决定"这到底算 low 还是 high"——
        而那个决定直接改变它排在哪一条前面。宁可多一张卡片（卡片上的「N 处」会解释
        它为什么在那里），也不替人做这个判断。
        """
        groups = models.group_findings([
            finding("a", severity="low", at=2.0),
            finding("b", severity="high", at=1.0),
        ])
        self.assertEqual(2, len(groups))

    def test_a_blank_title_never_merges_two_records(self):
        """空标题各成一组。

        `validate_finding` 要求标题非空，所以这在正常数据里不会出现；但真出现时按
        空串分组会把毫不相干的记录塌成一条——那是**静默的数据丢失**，而且是分组
        这一层唯一能造成的不可逆后果。
        """
        groups = models.group_findings([
            finding("a", title="", at=2.0),
            finding("b", title="", at=1.0),
        ])
        self.assertEqual(2, len(groups))
        self.assertEqual({"a", "b"}, {group["ids"][0] for group in groups})

    def test_whitespace_and_case_are_normalised_but_punctuation_is_not(self):
        """只压空白与大小写，不动标点。

        "TLS 证书未通过校验" 与 "tls  证书未通过校验" 是同一句话的两种写法；
        而 "…校验" 与 "…校验！" 是两句话——合并后者的风险比省一张卡片大得多。
        """
        same = models.group_findings([
            finding("a", title="TLS 证书未通过校验", at=2.0),
            finding("b", title="tls  证书未通过校验 ", at=1.0),
        ])
        self.assertEqual(1, len(same), "空白与大小写应该归一")
        other = models.group_findings([
            finding("a", title="TLS 证书未通过校验", at=2.0),
            finding("b", title="TLS 证书未通过校验！", at=1.0),
        ])
        self.assertEqual(2, len(other), "标点不同就是两句话")

    def test_the_group_keeps_the_first_records_own_title(self):
        """组标题取组里第一条的**原文**，不做归一化回写。

        归一化只用来判等；把归一化后的串当标题显示，等于把使用者写下的那句话改掉
        （大小写、空格全变），而它是要出现在报告里的。
        """
        group = models.group_findings([
            finding("a", title="TLS  证书未通过校验", at=2.0),
            finding("b", title="tls 证书未通过校验", at=1.0),
        ])[0]
        self.assertEqual("TLS  证书未通过校验", group["title"])

    def test_one_record_makes_a_group_of_one(self):
        group = models.group_findings([finding("a")])[0]
        self.assertEqual(1, group["count"])
        self.assertEqual(["a"], group["ids"])

    def test_an_empty_list_makes_no_groups(self):
        self.assertEqual([], models.group_findings([]))


class FindingsEndpointTestCase(unittest.TestCase):
    """接口契约：`groups` 与 `findings` 由**同一次查询**派生。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("groups.db")
        self.httpd, self.client = support.start(self.store, support.WEB_DIR)
        self.eid = self.store.create_engagement("10.10.10.250", "夹具")["id"]
        # 走**真实的写入路径**（`add_node` 会校验 intent 存在、步骤非空），而不是直接
        # 往表里塞行：那样写出来的夹具能通过，产品却不能——夹具就没有意义了。
        intent = self.store.add_node(self.eid, "intent",
                                     {"title": "TLS 复核", "goalId": "goal-1"})
        assets = [self.store.add_node(self.eid, "asset",
                                      {"type": "service",
                                       "value": "10.10.10.250:" + port})["id"]
                  for port in ("443", "8443", "18888")]
        self.store.add_node(self.eid, "finding", {
            "intentId": intent["id"], "title": "凭证有效：admin",
            "severity": "critical", "description": "以该账号在 86 端口登录成功",
            "reproducibleSteps": ["以 admin 登录"]})
        for asset, port in zip(assets, ("443", "8443", "18888")):
            self.store.add_node(self.eid, "finding", {
                "intentId": intent["id"], "title": "TLS 证书未通过校验",
                "severity": "low", "affectedAssetId": asset,
                "description": "端口 %s 的证书链未通过系统 CA 校验" % port,
                "reproducibleSteps": ["openssl s_client -connect 10.10.10.250:" + port]})

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def test_the_endpoint_returns_both_shapes(self):
        status, payload = self.client.get("/api/v1/engagements/%s/findings" % self.eid)
        self.assertEqual(200, status)
        self.assertEqual(4, len(payload["findings"]), "记录一条都不能少")
        self.assertEqual(2, len(payload["groups"]))

    def test_the_groups_cover_every_record_exactly_once(self):
        _status, payload = self.client.get(
            "/api/v1/engagements/%s/findings" % self.eid)
        covered = [ident for group in payload["groups"] for ident in group["ids"]]
        self.assertEqual(sorted(item["id"] for item in payload["findings"]),
                         sorted(covered),
                         "groups 与 findings 必须一一对应——两处不一致时界面看起来"
                         "完全正常，只有人去数记录才会发现")

    def test_the_model_still_sees_every_record(self):
        """给模型的那条路（`get_findings` 工具）走的是 `store.findings`，不经过分组。

        分组是**界面**的呈现问题：模型需要的是全部记录（它要判断这是三处独立问题
        还是一处），删给它看反而会让它少写两条复现步骤。
        """
        self.assertEqual(4, len(self.store.findings(self.eid)))


class FindingsWiringContractTestCase(unittest.TestCase):
    """前端接线：按分组渲染，且**退得回去**。"""

    def setUp(self):
        with open(os.path.join(WEB_DIR, "app.js"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_the_client_keeps_both_fields(self):
        self.assertIn("state.findings = asArray(data.findings)", self.app)
        self.assertIn("state.findingGroups = asArray(data.groups)", self.app)

    def test_the_group_card_is_used_and_the_single_card_is_kept(self):
        # 两个调用点都多了一个 `labelOf`：`「全部目标」`是跨目标的看板，手上只有
        # **当前目标**的资产树，不带翻译函数就会把资产名退化成 `asset-xxxx`。
        # 单目标视图传的是 undefined，走默认的 `assetLabel`——行为不变。
        self.assertIn("findingGroupCard(group, byId, labelOf)", self.app)
        self.assertIn("findingCard(instances[0], labelOf)", self.app,
                      "单条记录要照旧渲染——绝大多数问题只有一处证据")

    def test_a_missing_groups_field_falls_back_to_the_flat_list(self):
        """服务端升级而页面还开着时，旧 JS 拿到的响应里没有 `groups`。

        那种情况下必须退回**平铺**渲染：显示"没有匹配的漏洞"与"这个目标真的没有
        漏洞"长得一模一样，而后者是完全错误的一句话。
        """
        self.assertIn("if (!state.findingGroups.length)", self.app)

    def test_the_severity_chips_count_cards_not_records(self):
        """芯片的数要预告"你将看到几张卡片"。

        数记录条数的话，芯片说「低危 3」而点进去只有一张卡片；记录条数并没有丢——
        卡片上写着「3 处」。
        """
        self.assertIn("function severitySource()", self.app)
        self.assertIn("severitySource().filter", self.app)

    def test_the_instance_keeps_its_own_description_and_steps(self):
        """组内每一条记录都保留自己的描述与步骤——那是它能被复现的依据。

        三条 TLS 记录的 openssl 报错各不相同，合并成一句就等于把依据换成了一句
        概括，而概括恰恰是这一层不该做的事。
        """
        body = self.app[self.app.find("function findingInstanceBlock("):]
        body = body[:body.find("\n}")]
        self.assertIn("finding.description", body)
        self.assertIn("stepsBlock(finding)", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
