# -*- coding: utf-8 -*-
"""探索链路：一张「按记录读」的图（节点元数据 / 摘要 / 覆盖边 / 窗口）。

这里固定的是**对外形状**与**写入约束**：节点带 payload 字符串与 state / origin /
priority / ts，边只有 src / dst / rel，摘要用 covers 声明它覆盖了谁。排产不在这一层
——谁往里写记录由任务与代理决定，这组测试不假定某一种排产策略。
"""
import json
import re
import unittest

from tests import support
from pentestd import models
from pentestd.store import merged_id


def _engagement(store, target="10.10.10.110"):
    return store.create_engagement(target, "拿下控制台", "本人授权")["id"]


def _intent(store, engagement_id, title="侦察端口"):
    return store.add_node(engagement_id, "intent",
                          {"title": title, "goalId": "goal-1"})


def _fact(store, engagement_id, intent_id, detail="80/tcp 开放"):
    return store.add_node(engagement_id, "fact",
                          {"intentId": intent_id, "kind": "port",
                           "target": "10.10.10.110", "detail": detail})


def _finding(store, engagement_id, intent_id, title="SQLi"):
    return store.add_node(engagement_id, "finding",
                          {"intentId": intent_id, "title": title,
                           "severity": "high",
                           "reproducibleSteps": ["curl -s 'http://10.10.10.110/?id=1%27'"]})


def _digest(store, engagement_id, facts=(), intents=(), body="80 端口开放，注入点已确认。"):
    return store.add_node(engagement_id, "digest",
                          {"body": body,
                           "memberIds": {"facts": list(facts),
                                         "intents": list(intents)}})


def _mid(engagement_id, node_id):
    """合并视图里的 id 带 engagement 前缀（见 `store.merged_id`）。"""
    return merged_id(engagement_id, node_id)


def _nodes_by_type(payload):
    return {(node.get("type"), node.get("id")): node for node in payload["nodes"]}


class ExplorationShapeTests(unittest.TestCase):
    """接口形状：与对照实现（ARTEX `/api/exploration/graph`）同构。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("exploration.db")
        self.httpd, self.client = support.start(self.store)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def test_requires_session(self):
        directory, store = support.temporary_store("exploration-anon.db")
        httpd, client = support.start(store, admin=False)
        try:
            status, payload = client.get("/api/v1/exploration")
            self.assertEqual(401, status, payload)
        finally:
            support.stop(httpd, store, directory)

    def test_empty_store_is_an_empty_graph(self):
        status, payload = self.client.get("/api/v1/exploration")
        self.assertEqual(200, status, payload)
        self.assertEqual([], payload["nodes"])
        self.assertEqual([], payload["edges"])
        self.assertEqual(0, payload["counts"]["nodes"])
        # 各组都在（哪怕是空列表）：调用方按 key 取，缺 key 就是一次 KeyError，
        # 而「这一类还没有记录」本来是该正常显示为空的状态。
        for group in models.GROUP_ORDER:
            self.assertIn(group, payload["counts"])
            self.assertEqual(0, payload["counts"][group])

    def test_payload_is_a_json_string_and_meta_is_flattened(self):
        eid = _engagement(self.store)
        intent = _intent(self.store, eid)
        fact = _fact(self.store, eid, intent["id"])

        status, payload = self.client.get("/api/v1/exploration?nodes=all")
        self.assertEqual(200, status, payload)
        by_key = _nodes_by_type(payload)
        node = by_key[("fact", _mid(eid, fact["id"]))]

        # payload 是**字符串**而不是对象：同一个字段在不同类型里含义不同，
        # 字符串保留"原样存进去的是什么"。
        self.assertIsInstance(node["payload"], str)
        decoded = json.loads(node["payload"])
        self.assertEqual("80/tcp 开放", decoded["detail"])
        self.assertEqual(_mid(eid, intent["id"]), decoded["intentId"])
        # 结构字段不进 payload —— 索引与内容分开。
        for key in ("id", "kind", "createdAt", "state", "origin", "priority"):
            self.assertNotIn(key, decoded)

        self.assertEqual("fact", node["type"])
        self.assertEqual("confirmed", node["state"])   # fact 的默认状态
        self.assertEqual("system", node["origin"])
        self.assertEqual(0, node["priority"])
        self.assertRegex(node["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

    def test_goal_is_a_node_too(self):
        eid = _engagement(self.store, target="10.10.10.110")
        status, payload = self.client.get("/api/v1/exploration")
        self.assertEqual(200, status, payload)
        kinds = [node["type"] for node in payload["nodes"]]
        self.assertEqual(["goal"], kinds)
        self.assertEqual(_mid(eid, "goal-1"), payload["nodes"][0]["id"])
        self.assertEqual(1, payload["counts"]["goals"])

    def test_edges_use_src_dst_rel(self):
        eid = _engagement(self.store)
        intent = _intent(self.store, eid)
        fact = _fact(self.store, eid, intent["id"])
        finding = _finding(self.store, eid, intent["id"])

        status, payload = self.client.get("/api/v1/exploration")
        self.assertEqual(200, status, payload)
        triples = {(edge["rel"], edge["src"], edge["dst"]) for edge in payload["edges"]}
        self.assertIn(("spawns", _mid(eid, "goal-1"), _mid(eid, intent["id"])),
                      triples)
        self.assertIn(("yields", _mid(eid, intent["id"]), _mid(eid, fact["id"])),
                      triples)
        self.assertIn(("proves", _mid(eid, intent["id"]), _mid(eid, finding["id"])),
                      triples)
        # 只有三个字段：画图要的坐标不该混进「按记录读」的接口里。
        for edge in payload["edges"]:
            self.assertEqual({"src", "dst", "rel"}, set(edge))

    def test_digest_covers_what_it_absorbed(self):
        eid = _engagement(self.store)
        intent = _intent(self.store, eid)
        fact = _fact(self.store, eid, intent["id"])
        digest = _digest(self.store, eid, facts=[fact["id"]], intents=[intent["id"]])

        status, payload = self.client.get("/api/v1/exploration")
        self.assertEqual(200, status, payload)
        covered = {(edge["src"], edge["dst"]) for edge in payload["edges"]
                   if edge["rel"] == "covers"}
        # covers 只由摘要发出，且指向「被覆盖的记录」。
        self.assertEqual({(_mid(eid, digest["id"]), _mid(eid, fact["id"])),
                          (_mid(eid, digest["id"]), _mid(eid, intent["id"]))},
                         covered)
        node = _nodes_by_type(payload)[("digest", _mid(eid, digest["id"]))]
        self.assertEqual("active", node["state"])
        decoded = json.loads(node["payload"])
        self.assertEqual(1, decoded["generation"])
        self.assertEqual(64, len(decoded["signature"]))

    def test_window_keeps_anchors_and_reports_drops(self):
        eid = _engagement(self.store)
        intent = _intent(self.store, eid)
        fact = _fact(self.store, eid, intent["id"])
        finding = _finding(self.store, eid, intent["id"])
        digest = _digest(self.store, eid, facts=[fact["id"]], intents=[intent["id"]])

        status, payload = self.client.get("/api/v1/exploration?nodes=1")
        self.assertEqual(200, status, payload)
        shown = {node["id"] for node in payload["nodes"]}
        # 窗口只留最后一条，而最后一条是摘要：它覆盖的事实与意图由它代表，不必
        # 再各占一格 —— 摘要存在的意义就是替掉那些细节。锚点保留（被指向的源头
        # 必须留下）不在这里重复验证，那是 `/api/v1/graph` 的既有契约。
        self.assertEqual({_mid(eid, "goal-1"), _mid(eid, digest["id"])}, shown)
        self.assertNotIn(_mid(eid, finding["id"]), shown)
        self.assertNotIn(_mid(eid, fact["id"]), shown)
        self.assertEqual(3, payload["truncated"]["dropped"])

    def test_bad_window_is_a_400_not_a_default(self):
        status, payload = self.client.get("/api/v1/exploration?nodes=lots")
        self.assertEqual(400, status, payload)
        self.assertIn("nodes", payload["error"])

    def test_scheduler_endpoints_are_gone(self):
        """链路是只读的：上一版的排产开关与「立刻排一轮」不该还挂在上面。"""
        status, _payload = self.client.post("/api/v1/exploration/settings",
                                            {"settings": {"enabled": True}})
        self.assertIn(status, (404, 405))
        status, _payload = self.client.post("/api/v1/exploration/tick", {"force": True})
        self.assertIn(status, (404, 405))


class NodeMetaTests(unittest.TestCase):
    """state / origin / priority 的写入约束：非法值一律拒绝，不静默兜底。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("meta.db")
        self.eid = _engagement(self.store)
        self.intent = _intent(self.store, self.eid)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_state_is_closed_per_kind(self):
        fact = self.store.add_node(self.eid, "fact",
                                   {"intentId": self.intent["id"], "detail": "80 开放",
                                    "state": "origin", "origin": "work#1",
                                    "priority": 7})
        self.assertEqual("origin", fact["state"])
        self.assertEqual("work#1", fact["origin"])
        self.assertEqual(7, fact["priority"])
        # goal 的状态只有 active / met，`done` 是意图的词。
        with self.assertRaises(models.ValidationError):
            self.store.add_node(self.eid, "intent",
                                {"title": "再看一眼", "goalId": "goal-1",
                                 "state": "met"})

    def test_origin_must_be_a_known_producer(self):
        with self.assertRaises(models.ValidationError):
            self.store.add_node(self.eid, "intent",
                                {"title": "探一下", "goalId": "goal-1",
                                 "origin": "someone"})
        # work#N 与 job-xxx 是合法的产物归属。
        node = self.store.add_node(self.eid, "intent",
                                   {"title": "探一下", "goalId": "goal-1",
                                    "origin": "job-abc123"})
        self.assertEqual("job-abc123", node["origin"])

    def test_priority_range(self):
        for bad in (-1, 10, "high"):
            with self.assertRaises(models.ValidationError):
                self.store.add_node(self.eid, "intent",
                                    {"title": "x", "goalId": "goal-1",
                                     "priority": bad})


class DigestValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("digest.db")
        self.eid = _engagement(self.store)
        self.intent = _intent(self.store, self.eid)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_signature_defaults_to_the_body_hash(self):
        node = _digest(self.store, self.eid, intents=[self.intent["id"]])
        expected = models.hashlib.sha256("80 端口开放，注入点已确认。".encode("utf-8")).hexdigest()
        self.assertEqual(expected, node["signature"])

    def test_signature_must_be_64_hex(self):
        with self.assertRaises(models.ValidationError):
            self.store.add_node(self.eid, "digest", {"body": "短正文", "signature": "abc"})

    def test_members_must_exist_and_be_the_right_kind(self):
        with self.assertRaises(models.ValidationError):
            _digest(self.store, self.eid, facts=["fact-404"])
        # 意图 id 塞进 facts 也是错的：类型对不上就等于指向了一条不存在的事实。
        with self.assertRaises(models.ValidationError):
            _digest(self.store, self.eid, facts=[self.intent["id"]])

    def test_generation_must_be_positive(self):
        with self.assertRaises(models.ValidationError):
            self.store.add_node(self.eid, "digest", {"body": "正文", "generation": 0})


class ViewGroupingTests(unittest.TestCase):
    """单目标视图与合并视图共用 `models.GROUP_KEYS`，摘要不能只在一边出现。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("grouping.db")
        self.eid = _engagement(self.store)
        self.intent = _intent(self.store, self.eid)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_single_view_has_a_digests_group(self):
        digest = _digest(self.store, self.eid, intents=[self.intent["id"]])
        view = self.store.view(self.eid, limit=None)
        self.assertEqual([digest["id"]],
                         [node["id"] for node in view["nodes"]["digests"]])

    def test_combined_view_has_a_digests_group(self):
        digest = _digest(self.store, self.eid, intents=[self.intent["id"]])
        view = self.store.combined_view(0)
        self.assertEqual([_mid(self.eid, digest["id"])],
                         [node["id"] for node in view["nodes"]["digests"]])
        node = view["nodes"]["digests"][0]
        self.assertEqual("active", node["state"])


if __name__ == "__main__":
    unittest.main()