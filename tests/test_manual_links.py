# -*- coding: utf-8 -*-
"""人手画的边（`link`）：存储、HTTP、分层三处各守一条不变量。

这条通路与「记录推出来的边」是两件事，所以分开守：`models.edges_for` 从字段推出
spawns / yields / derived_from / proves / covers / parent，人一次都不写；而画布上的
连线是分析员自己看出来的「这两件事我放在一起看」，只能由人写。混起来的后果不是报错，
是图上分不出哪条线是证据、哪条线是意见。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import support                                                   # noqa: E402
from pentestd import graph                                       # noqa: E402
from pentestd.models import ValidationError                      # noqa: E402
from pentestd.store import Store                                 # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


class ManualEdgeStoreTestCase(unittest.TestCase):
    """存储层：写一条手工边、重复写、四种拒绝、删。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(os.path.join(self.tmp.name, "links.db"))
        self.eid = self.store.create_engagement(
            "links-target", "连线目标", "测试授权 L-1")["id"]
        self.store.add_node(self.eid, "intent", {"title": "A", "goalId": "goal-1"})
        self.store.add_node(self.eid, "intent", {"title": "B", "goalId": "goal-1"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_a_manual_edge_is_written_and_shows_up_in_the_view(self):
        edge = self.store.add_edge(self.eid, "intent-1", "intent-2")
        self.assertEqual(edge["kind"], "link")
        self.assertEqual(edge["sourceId"], "intent-1")
        self.assertEqual(edge["targetId"], "intent-2")
        self.assertNotIn("_reused", edge, "第一次画不是复用")
        kinds = {(e["kind"], e["sourceId"], e["targetId"])
                 for e in self.store.list_edges(self.eid)}
        self.assertIn(("link", "intent-1", "intent-2"), kinds)
        # 节点表没被动过：连线不产记录。
        self.assertEqual(2 + 1, len(self.store.list_nodes(self.eid)),
                         "连线不该顺手插一条节点（多出来的是 goal-1）")

    def test_drawing_the_same_line_twice_reuses_it(self):
        first = self.store.add_edge(self.eid, "intent-1", "intent-2")
        second = self.store.add_edge(self.eid, "intent-1", "intent-2")
        self.assertTrue(second.get("_reused"), "重复画同一条线应当回既有那条")
        self.assertEqual(first["id"], second["id"], "两次画出了两条不同的边")
        links = [e for e in self.store.list_edges(self.eid) if e["kind"] == "link"]
        self.assertEqual(1, len(links), "重复画在库里留下了两行")

    def test_a_card_cannot_be_linked_to_itself(self):
        with self.assertRaises(ValidationError) as caught:
            self.store.add_edge(self.eid, "intent-1", "intent-1")
        self.assertIn("不能连自己", str(caught.exception))

    def test_a_missing_endpoint_is_rejected_by_name(self):
        with self.assertRaises(ValidationError) as caught:
            self.store.add_edge(self.eid, "intent-1", "intent-99")
        self.assertIn("intent-99", str(caught.exception),
                      "拒绝时不说清是哪个端点不在，人只能干瞪眼")

    def test_derived_kinds_cannot_be_written_by_hand(self):
        """`spawns` 说的是「记录这么说的」，人手补一条就成了假证据。"""
        with self.assertRaises(ValidationError) as caught:
            self.store.add_edge(self.eid, "intent-1", "intent-2", kind="spawns")
        self.assertIn("不能手写", str(caught.exception))

    def test_both_endpoints_are_required(self):
        for body in (("", "intent-2"), ("intent-1", "")):
            with self.assertRaises(ValidationError) as caught:
                self.store.add_edge(self.eid, body[0], body[1])
            self.assertIn("两个端点", str(caught.exception))

    def test_a_derived_edge_cannot_be_deleted_from_here(self):
        """自动边是记录的影子：删线不删记录，读的人就不知道它曾经在。"""
        auto = [e for e in self.store.list_edges(self.eid)
                if e["kind"] != "link"][0]
        with self.assertRaises(ValidationError) as caught:
            self.store.delete_edge(self.eid, auto["id"])
        self.assertIn("改它要改记录本身", str(caught.exception))

    def test_a_manual_edge_can_be_deleted_and_a_second_try_says_missing(self):
        edge = self.store.add_edge(self.eid, "intent-1", "intent-2")
        removed = self.store.delete_edge(self.eid, edge["id"])
        self.assertEqual(edge["id"], removed["id"])
        self.assertEqual([], [e for e in self.store.list_edges(self.eid)
                              if e["id"] == edge["id"]])
        with self.assertRaises(KeyError):
            self.store.delete_edge(self.eid, edge["id"])

    def test_a_link_does_not_move_any_card_to_another_column(self):
        """分层看的是推进关系。一条人际连线把卡挤到别的列，图就成了意见的产物。"""
        before = {n["id"]: n["x"] for n in graph.build_layout(
            self.store.list_nodes(self.eid), self.store.list_edges(self.eid))["nodes"]}
        self.store.add_edge(self.eid, "intent-2", "intent-1")
        after = {n["id"]: n["x"] for n in graph.build_layout(
            self.store.list_nodes(self.eid), self.store.list_edges(self.eid))["nodes"]}
        self.assertEqual(before, after, "手工连线改了列位：它被当成推进关系算进分层了")


class ManualEdgeHttpTestCase(unittest.TestCase):
    """HTTP 层：真实起服务、真实发请求（会经过鉴权与 CSRF）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.store = Store(os.path.join(cls.tmp.name, "links-api.db"))
        cls.httpd, cls.client = support.start(cls.store, WEB_DIR)

    @classmethod
    def tearDownClass(cls):
        support.stop(cls.httpd, cls.store, cls.tmp)

    def _engagement(self):
        status, created = self.client.post("/api/v1/engagements",
                                           {"target": "links-api"})
        self.assertEqual(status, 201, created)
        eid = created["id"]
        for title in ("A", "B"):
            status, _ = self.client.post(
                "/api/v1/engagements/%s/nodes" % eid,
                {"kind": "intent", "title": title, "goalId": "goal-1"})
            self.assertEqual(status, 201)
        return eid

    def test_post_creates_then_reuses(self):
        eid = self._engagement()
        status, payload = self.client.post(
            "/api/v1/engagements/%s/edges" % eid,
            {"sourceId": "intent-1", "targetId": "intent-2"})
        self.assertEqual(status, 201, payload)
        self.assertEqual("link", payload["edge"]["kind"])
        status, again = self.client.post(
            "/api/v1/engagements/%s/edges" % eid,
            {"sourceId": "intent-1", "targetId": "intent-2"})
        self.assertEqual(200, status, "重复画应当是 200 + 既有那条，不是又建一条")
        self.assertTrue(again["edge"]["_reused"])
        self.assertEqual(payload["edge"]["id"], again["edge"]["id"])

    def test_short_body_names_are_accepted(self):
        """`source`/`target` 也认：面板另一处用的是短名，两条路不该分叉。"""
        eid = self._engagement()
        status, payload = self.client.post(
            "/api/v1/engagements/%s/edges" % eid,
            {"source": "intent-1", "target": "intent-2"})
        self.assertEqual(201, status, payload)
        self.assertEqual("intent-2", payload["edge"]["targetId"])

    def test_bad_requests_are_400_with_a_reason(self):
        eid = self._engagement()
        for body, want in (
                ({"sourceId": "intent-1", "targetId": "intent-1"}, "不能连自己"),
                ({"sourceId": "intent-1", "targetId": "intent-99"}, "端点不存在"),
                ({"sourceId": "intent-1"}, "两个端点"),
                ({"sourceId": "intent-1", "targetId": "intent-2", "kind": "proves"},
                 "不能手写")):
            status, payload = self.client.post(
                "/api/v1/engagements/%s/edges" % eid, body)
            self.assertEqual(400, status, "%r 应当被拒绝" % (body,))
            self.assertIn(want, payload["error"], "拒绝理由没说到点上：%r" % (payload,))

    def test_delete_round_trips_and_disappears(self):
        eid = self._engagement()
        _, created = self.client.post("/api/v1/engagements/%s/edges" % eid,
                                      {"sourceId": "intent-1", "targetId": "intent-2"})
        edge_id = created["edge"]["id"]
        status, payload = self.client.post(
            "/api/v1/engagements/%s/edges/%s/delete" % (eid, edge_id), {})
        self.assertEqual(200, status, payload)
        self.assertEqual(edge_id, payload["deleted"]["id"])
        _, view = self.client.get("/api/v1/engagements/%s" % eid)
        self.assertEqual([], [e for e in view["edges"] if e["id"] == edge_id],
                         "删掉的线还留在视图里")
        status, _ = self.client.post(
            "/api/v1/engagements/%s/edges/%s/delete" % (eid, edge_id), {})
        self.assertEqual(404, status, "删第二次应当说「不存在」")

    def test_the_written_line_reaches_the_layout(self):
        """写进库还不够：图里得真有这条边，而且带中文名。"""
        eid = self._engagement()
        self.client.post("/api/v1/engagements/%s/edges" % eid,
                         {"sourceId": "intent-1", "targetId": "intent-2"})
        _, view = self.client.get("/api/v1/engagements/%s" % eid)
        drawn = [e for e in view["layout"]["edges"] if e["kind"] == "link"]
        self.assertEqual(1, len(drawn), "连线没进布局：画布上画不出它")
        self.assertEqual("连线", drawn[0]["label"],
                         "布局里的边没带中文名：画布会把 link 原样印出来")


if __name__ == "__main__":
    unittest.main()
