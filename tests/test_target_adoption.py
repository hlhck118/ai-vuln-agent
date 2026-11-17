# -*- coding: utf-8 -*-
"""目标名要与实际扫描目标对应：会话只能写占位名，真实地址由第一条任务回填。

## 守的是什么

会话下单时自动建的目标只能叫「渗透目标」（`ensure_session_engagement` 传的是
标签，不是地址），而 `engagements.target` 恰好就是链路图那个目标节点的标签、
漏洞页左栏那一行、报告第一行的出处。于是 38 个任务打在 `10.10.10.250` 上，
整份交付物的抬头却写着「渗透目标」——读的人没法确认这叠证据测的是哪台机器。

回填发生在 `create_job`：那是第一个知道"这台机器到底叫什么"的时刻。三条边界：

* 只替换**占位名**（`渗透目标` / 旧的 `对话作业` / 空串）——使用者亲手起的
  「内部办公网」一个字符都不能被任务地址盖掉；
* 任务自己的 target 也是占位名时不回填（那等于拿占位名换占位名）；
* 历史库里的旧目标在下次启动时自愈（`_heal_goal_targets`），因为它已经错过
  了 `create_job` 那一刻。

## 为什么把资产名也带进聚合接口

「全部目标」是跨目标看板，而页面手上只有**当前目标**的资产树。聚合响应不
把 asset id 翻成地址，卡片上「影响资产」就会退化成 `asset-xxxx`——那一条字段
等于没写。
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import models                       # noqa: E402
from pentestd import store as store_module        # noqa: E402
from pentestd.store import Store                  # noqa: E402


class TargetAdoptionTestCase(unittest.TestCase):
    """会话建的目标拿到第一条真实任务时，把地址写到两处。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = os.path.join(self.directory.name, "targets.db")
        self.store = Store(self.path)

    def tearDown(self):
        if self.store is not None:
            self.store.close()
        self.directory.cleanup()

    def test_the_first_real_job_target_replaces_the_placeholder(self):
        eid = self.store.ensure_session_engagement("C-target-1")
        self.assertEqual(store_module.DEFAULT_SESSION_LABEL,
                         self.store.get_engagement_meta(eid)["target"])
        self.store.create_job("probe", "192.0.2.10:443", engagement_id=eid)
        self.assertEqual("192.0.2.10:443",
                         self.store.get_engagement_meta(eid)["target"],
                         "目标还叫「渗透目标」：链路图与报告抬头会指错机器")
        self.assertEqual("192.0.2.10:443", self.store.view(eid)["goal"]["target"],
                         "goal 节点没跟着改：链路图上的目标标签仍是占位名")

    def test_an_explicit_name_is_never_overwritten(self):
        eid = self.store.create_engagement("内部办公网", "夹具")["id"]
        self.store.create_job("probe", "192.0.2.11", engagement_id=eid)
        self.assertEqual("内部办公网", self.store.get_engagement_meta(eid)["target"],
                         "使用者起的名字被任务地址盖掉了")
        self.assertEqual("内部办公网", self.store.view(eid)["goal"]["target"])

    def test_a_placeholder_job_target_never_wins(self):
        eid = self.store.create_engagement("对话作业", "夹具")["id"]
        self.store.create_job("probe", "对话作业", engagement_id=eid)
        self.assertEqual("对话作业", self.store.get_engagement_meta(eid)["target"])
        self.store.create_job("probe", "", engagement_id=eid)
        self.assertEqual("对话作业", self.store.get_engagement_meta(eid)["target"],
                         "空目标不该把已有名字抹成空串")

    def test_a_second_job_keeps_the_first_real_target(self):
        eid = self.store.ensure_session_engagement("C-target-2")
        self.store.create_job("probe", "10.0.0.5", engagement_id=eid)
        self.store.create_job("scan", "10.0.0.5:8443", engagement_id=eid)
        self.assertEqual("10.0.0.5", self.store.get_engagement_meta(eid)["target"],
                         "后来的端口名把主机名换掉了：回填只该在占位名上发生")

    def test_startup_heals_a_placeholder_left_by_an_older_build(self):
        eid = self.store.ensure_session_engagement("C-target-3")
        self.store.create_job("probe", "10.0.0.9", engagement_id=eid)
        # 模拟旧版本留下的两行：都还停在占位名上（旧版本没有回填这一步）。
        self.store.conn.execute(
            "UPDATE engagements SET target=? WHERE id=?",
            (store_module.DEFAULT_SESSION_LABEL, eid))
        row = self.store.conn.execute(
            "SELECT payload FROM nodes WHERE id='goal-1' AND engagement_id=?",
            (eid,)).fetchone()
        payload = json.loads(row["payload"])
        payload["target"] = store_module.DEFAULT_SESSION_LABEL
        self.store.conn.execute(
            "UPDATE nodes SET payload=? WHERE id='goal-1' AND engagement_id=?",
            (json.dumps(payload, ensure_ascii=False), eid))
        self.store.conn.commit()

        self.store.close()
        self.store = Store(self.path)          # 重开 = 一次启动自愈
        self.assertEqual("10.0.0.9",
                         self.store.get_engagement_meta(eid)["target"],
                         "重启没有把旧库里的占位名补成真实地址")
        self.assertEqual("10.0.0.9", self.store.view(eid)["goal"]["target"])

    def test_all_findings_groups_by_target_and_carries_asset_labels(self):
        first = self.store.create_engagement("10.0.0.1", "甲")["id"]
        second = self.store.create_engagement("10.0.0.2", "乙")["id"]
        intent = self.store.add_node(first, "intent",
                                     {"title": "TLS 复核", "goalId": "goal-1"})
        asset = self.store.add_node(first, "asset",
                                    {"type": "service", "value": "10.0.0.1:443"})
        self.store.add_node(first, "finding", {
            "intentId": intent["id"], "title": "TLS 证书未通过校验",
            "severity": "low", "affectedAssetId": asset["id"],
            "description": "证书链未通过系统 CA 校验",
            "reproducibleSteps": ["openssl s_client -connect 10.0.0.1:443"]})

        buckets = self.store.all_findings()
        got = {item["engagementId"]: item for item in buckets}
        self.assertIn(first, got)
        self.assertNotIn(second, got, "没有漏洞的目标也进了聚合：左栏会多出空分组")
        self.assertEqual(1, got[first]["counts"]["total"])
        self.assertEqual(0, got[first]["counts"]["verified"] + 0,
                         "未验证的漏洞不该被算成已证实")
        self.assertEqual("10.0.0.1:443", got[first]["assetLabels"][asset["id"]],
                         "聚合接口没带资产名：跨目标卡片的「影响资产」会退化")


if __name__ == "__main__":
    unittest.main(verbosity=2)
