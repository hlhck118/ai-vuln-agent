# -*- coding: utf-8 -*-
"""编排面：目标 / 意图 / 约束 / 状态 / 暂停。

这一层钉的是**人能介入的那一半**：链路不只是"看得见"，还要能加目标、开意图、
改状态、立约束、暂停。每条用例都走真实 HTTP（socket → `_dispatch` → 处理函数），
因为权限（admin）、路径参数与请求体形状正是这条路径自己的职责。
"""
import unittest

from tests import support
from pentestd import auth


def _engagement(store, target="10.10.10.110"):
    return store.create_engagement(target, "拿下控制台", "本人授权")["id"]


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("orchestration.db")
        self.eid = _engagement(self.store)
        self.httpd, self.client = support.start(self.store)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def _url(self, suffix):
        return "/api/v1/engagements/%s/%s" % (self.eid, suffix)

    def _operator_client(self):
        self.store.create_user("operator", auth.hash_password("Operator-Pass1!"),
                               role="operator")
        base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        client = support.Client(base)
        status, payload = client.login("operator", "Operator-Pass1!")
        self.assertEqual(200, status, payload)
        return client

    def test_orchestration_requires_session(self):
        directory, store = support.temporary_store("orchestration-anon.db")
        eid = _engagement(store)
        httpd, client = support.start(store, admin=False)
        try:
            status, payload = client.get("/api/v1/engagements/%s/orchestration" % eid)
            self.assertEqual(401, status, payload)
        finally:
            support.stop(httpd, store, directory)

    def test_default_shape(self):
        status, payload = self.client.get(self._url("orchestration"))
        self.assertEqual(200, status, payload)
        self.assertEqual(1, len(payload["goals"]))
        self.assertEqual("goal-1", payload["goals"][0]["id"])
        self.assertEqual("active", payload["goals"][0]["state"])
        self.assertFalse(payload["paused"])
        self.assertEqual([], payload["constraints"])
        self.assertEqual(0, payload["counts"]["intents"])
        # 状态下拉框的取值也由后端给，前端不自己列一遍。
        self.assertIn("blocked", payload["intentStates"])
        self.assertIn("met", payload["goalStates"])

    def test_add_goal_appends_a_second_goal(self):
        status, payload = self.client.post(self._url("goals"),
                                           {"text": "拿到数据库", "objective": "拖库前先验证"})
        self.assertEqual(201, status, payload)
        self.assertEqual("goal-2", payload["goal"]["id"])
        status, view = self.client.get(self._url("orchestration"))
        self.assertEqual(["goal-1", "goal-2"], [g["id"] for g in view["goals"]])
        self.assertEqual(2, view["counts"]["goals"])

    def test_empty_goal_is_rejected(self):
        status, payload = self.client.post(self._url("goals"), {"text": "   "})
        self.assertEqual(400, status, payload)

    def test_add_intent_and_change_state(self):
        status, payload = self.client.post(self._url("intents"),
                                           {"title": "枚举 /api 路由", "goalId": "goal-1"})
        self.assertEqual(201, status, payload)
        intent_id = payload["intent"]["id"]
        self.assertEqual("active", payload["intent"]["state"])

        status, payload = self.client.post(
            "/api/v1/engagements/%s/nodes/%s/state" % (self.eid, intent_id),
            {"state": "blocked"})
        self.assertEqual(200, status, payload)
        self.assertEqual("blocked", payload["node"]["state"])

        status, view = self.client.get(self._url("orchestration"))
        self.assertEqual(1, view["counts"]["intentsBlocked"])

    def test_state_must_fit_the_kind(self):
        status, payload = self.client.post(
            "/api/v1/engagements/%s/nodes/goal-2/state" % self.eid, {"state": "blocked"})
        self.assertEqual(404, status, payload)      # goal-2 还不存在
        status, payload = self.client.post(
            "/api/v1/engagements/%s/nodes/goal-1/state" % self.eid, {"state": "blocked"})
        self.assertEqual(400, status, payload)
        self.assertIn("state", payload["error"])

    def test_intent_requires_an_anchor(self):
        status, payload = self.client.post(self._url("intents"), {"title": "无锚点"})
        self.assertEqual(400, status, payload)

    def test_constraints_are_deduplicated_and_removable(self):
        status, payload = self.client.post(self._url("constraints"),
                                           {"kind": "deny", "text": "禁止 brute force"})
        self.assertEqual(201, status, payload)
        first = payload["constraint"]["id"]
        self.assertEqual("禁止", payload["constraint"]["kindLabel"])

        status, again = self.client.post(self._url("constraints"),
                                         {"kind": "deny", "text": "  禁止 brute force "})
        self.assertEqual(201, status, again)
        self.assertEqual(first, again["constraint"]["id"])

        status, payload = self.client.post(self._url("constraints"),
                                           {"kind": "allow", "text": "只测当前端口"})
        self.assertEqual(201, status, payload)
        status, view = self.client.get(self._url("orchestration"))
        self.assertEqual(2, view["counts"]["constraints"])
        self.assertEqual(1, view["counts"]["allow"])
        self.assertEqual(1, view["counts"]["deny"])

        status, payload = self.client.post(
            "/api/v1/engagements/%s/constraints/%s/delete" % (self.eid, first), {})
        self.assertEqual(200, status, payload)
        status, view = self.client.get(self._url("orchestration"))
        self.assertEqual(1, view["counts"]["constraints"])

    def test_bad_constraint_kind_is_rejected(self):
        status, payload = self.client.post(self._url("constraints"),
                                           {"kind": "maybe", "text": "x"})
        self.assertEqual(400, status, payload)

    def test_pause_and_resume(self):
        status, payload = self.client.post(self._url("pause"), {})
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["paused"])
        status, view = self.client.get(self._url("orchestration"))
        self.assertTrue(view["paused"])
        status, payload = self.client.post(self._url("resume"), {})
        self.assertEqual(200, status, payload)
        self.assertFalse(payload["paused"])

    def test_delete_intent_removes_its_edges(self):
        status, payload = self.client.post(self._url("intents"),
                                           {"title": "探一下", "goalId": "goal-1"})
        self.assertEqual(201, status, payload)
        intent_id = payload["intent"]["id"]
        status, payload = self.client.post(
            "/api/v1/engagements/%s/nodes/%s/delete" % (self.eid, intent_id), {})
        self.assertEqual(200, status, payload)
        self.assertGreaterEqual(payload["deleted"]["edges"], 1)
        status, view = self.client.get(self._url("orchestration"))
        self.assertEqual(0, view["counts"]["intents"])

    def test_goal_one_cannot_be_deleted(self):
        status, payload = self.client.post(
            "/api/v1/engagements/%s/nodes/goal-1/delete" % self.eid, {})
        self.assertEqual(400, status, payload)
        self.assertIn("goal-1", payload["error"])

    def test_writes_need_admin(self):
        client = self._operator_client()
        for suffix, body in (("goals", {"text": "x"}), ("intents", {"title": "x"}),
                             ("constraints", {"kind": "deny", "text": "x"}),
                             ("pause", {})):
            status, payload = client.post(self._url(suffix), body)
            self.assertEqual(403, status, "%s 应要求管理员：%s" % (suffix, payload))
        # 读仍然可以：只读用户也要看得见编排现状。
        status, payload = client.get(self._url("orchestration"))
        self.assertEqual(200, status, payload)

    def test_unknown_engagement_is_404(self):
        status, payload = self.client.get("/api/v1/engagements/E-nope/orchestration")
        self.assertEqual(404, status, payload)


if __name__ == "__main__":
    unittest.main()