# -*- coding: utf-8 -*-
"""探索链路：意图推导、排产口径与派活（explore 任务）。

这里固定的是**判据**：什么状态该派哪一步、派过之后静默期怎么算、在途上限怎么卡。
具体某一轮派了几条不是契约 —— 换一套排产策略不该让这组测试变红。
"""
import unittest

from tests import support
from pentestd import auth, exploration, models


def _engagement(store, target="10.10.10.110"):
    return store.create_engagement(target, "拿下控制台", "本人授权")["id"]


def _intent(store, engagement_id, title="侦察端口"):
    return store.add_node(engagement_id, "intent",
                          {"title": title, "goalId": "goal-1"})


def _finding(store, engagement_id, intent_id, title="SQLi", verified=False):
    payload = {"intentId": intent_id, "title": title, "severity": "high",
               "reproducibleSteps": ["curl -s http://10.10.10.110/?id=1%27"]}
    if verified:
        payload["verification"] = {"verdict": models.VERDICT_VERIFIED,
                                   "method": "手工复现"}
    return store.add_node(engagement_id, "finding", payload)


class ExplorationStateTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("exploration.db")
        self.httpd, self.client = support.start(self.store)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def test_requires_session(self):
        directory, store = support.temporary_store("exploration-anon.db")
        httpd, client = support.start(store, admin=False)
        try:
            status, _payload = client.get("/api/v1/exploration")
            self.assertEqual(status, 401)
        finally:
            support.stop(httpd, store, directory)

    def test_ships_settings_enum_and_ranges(self):
        status, payload = self.client.get("/api/v1/exploration")
        self.assertEqual(status, 200)
        # 默认必须是关的：一上来就自动往队列塞活，是行为改变而不是默认值。
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["settings"]["enabled"], False)
        self.assertEqual(payload["settings"]["intervalSeconds"],
                         exploration.DEFAULTS["intervalSeconds"])
        self.assertEqual(payload["settings"]["maxPending"],
                         exploration.DEFAULTS["maxPending"])
        self.assertEqual(payload["jobKind"], "explore")
        self.assertEqual(sorted(payload["kinds"]),
                         sorted(exploration.INTENT_LABELS))
        # 上下界要跟后端校验一致：界面上照着 ranges 渲染输入框。
        self.assertEqual(payload["ranges"]["intervalSeconds"], [60, 86400])
        self.assertEqual(payload["ranges"]["maxPending"], [1, 20])
        self.assertEqual(payload["engagements"], [])

    def test_reports_counts_and_in_flight(self):
        engagement_id = _engagement(self.store)
        intent = _intent(self.store, engagement_id)
        _finding(self.store, engagement_id, intent["id"])
        exploration.tick(self.store, force=True)

        status, payload = self.client.get("/api/v1/exploration")
        self.assertEqual(status, 200)
        row = payload["engagements"][0]
        self.assertEqual(row["engagementId"], engagement_id)
        self.assertEqual(row["counts"]["intents"], 1)
        self.assertEqual(row["counts"]["findings"], 1)
        # 派出去的那条还没被认领，所以在途是 1；这个数字是界面上"有没有在干活"的唯一依据。
        self.assertEqual(row["inFlight"], 1)
        self.assertEqual(len(row["inFlightIds"]), 1)
        self.assertTrue(row["intents"])


class ExplorationSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("exploration-set.db")
        self.httpd, self.client = support.start(self.store)
        self.store.create_user("operator", auth.hash_password("Operator-Pass1!"),
                               role="operator")

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def _operator(self):
        client = support.Client(self.client.base)
        status, _payload = client.login("operator", "Operator-Pass1!")
        self.assertEqual(status, 200)
        return client

    def test_operator_cannot_save(self):
        client = self._operator()
        status, payload = client.post("/api/v1/exploration/settings",
                                      {"enabled": True})
        self.assertEqual(status, 403)
        self.assertFalse(exploration.settings(self.store)["enabled"])

    def test_out_of_range_is_rejected(self):
        status, _payload = self.client.post("/api/v1/exploration/settings",
                                            {"intervalSeconds": 0})
        self.assertEqual(status, 400)
        status, _payload = self.client.post("/api/v1/exploration/settings",
                                            {"maxPending": 99})
        self.assertEqual(status, 400)
        # 被拒的请求不能留下半份配置。
        config = exploration.settings(self.store)
        self.assertEqual(config["intervalSeconds"],
                         exploration.DEFAULTS["intervalSeconds"])
        self.assertEqual(config["maxPending"],
                         exploration.DEFAULTS["maxPending"])

    def test_enabled_flag_persists(self):
        status, payload = self.client.post("/api/v1/exploration/settings",
                                           {"settings": {"enabled": True,
                                                         "intervalSeconds": 120,
                                                         "maxPending": 2}})
        self.assertEqual(status, 200)
        self.assertTrue(payload["enabled"])
        status, current = self.client.get("/api/v1/exploration")
        self.assertEqual(status, 200)
        self.assertTrue(current["settings"]["enabled"])
        self.assertEqual(current["settings"]["intervalSeconds"], 120)
        self.assertEqual(current["settings"]["maxPending"], 2)

    def test_flat_body_is_accepted(self):
        status, _payload = self.client.post("/api/v1/exploration/settings",
                                            {"intervalSeconds": 300})
        self.assertEqual(status, 200)
        self.assertEqual(exploration.settings(self.store)["intervalSeconds"], 300)


class ExplorationIntentTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("exploration-intent.db")
        self.engagement_id = _engagement(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def _keys(self):
        return [item["key"] for item in
                exploration.intents(self.store, self.engagement_id)]

    def test_empty_target_asks_plan_and_recon(self):
        keys = self._keys()
        # 刚立的目标既没有意图，也没有任何事实，两件事都要人做。
        self.assertIn("plan:goal-1", keys)
        self.assertIn("recon:goal-1", keys)

    def test_intent_without_output_asks_progress(self):
        intent = _intent(self.store, self.engagement_id)
        self.assertIn("progress:%s" % intent["id"], self._keys())

    def test_unverified_finding_asks_verify(self):
        intent = _intent(self.store, self.engagement_id)
        finding = _finding(self.store, self.engagement_id, intent["id"])
        items = exploration.intents(self.store, self.engagement_id)
        self.assertEqual(items[0]["key"], "verify:%s" % finding["id"])
        self.assertEqual(items[0]["kind"], "verify")
        self.assertTrue(items[0]["target"])

    def test_verified_finding_drops_verify(self):
        intent = _intent(self.store, self.engagement_id)
        _finding(self.store, self.engagement_id, intent["id"], verified=True)
        keys = self._keys()
        self.assertFalse([key for key in keys if key.startswith("verify:")])

    def test_vuln_fact_asks_confirm(self):
        intent = _intent(self.store, self.engagement_id)
        # 写入口收的是 `kind`（结构类型已由 add_node 固定为 fact），落库后才叫 `factKind`。
        fact = self.store.add_node(self.engagement_id, "fact",
                                   {"intentId": intent["id"], "kind": "vuln",
                                    "detail": "5.1.2 版本存在注入点"})
        keys = self._keys()
        self.assertIn("confirm:%s" % fact["id"], keys)


class ExplorationTickTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("exploration-tick.db")
        self.engagement_id = _engagement(self.store)
        self.intent = _intent(self.store, self.engagement_id)
        _finding(self.store, self.engagement_id, self.intent["id"])

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_off_by_default(self):
        result = exploration.tick(self.store)
        self.assertTrue(result["ok"])
        self.assertFalse(result["enabled"])
        self.assertEqual(result["created"], [])
        self.assertEqual(self.store.jobs(), [])

    def test_force_creates_explore_job(self):
        result = exploration.tick(self.store, force=True)
        self.assertEqual(len(result["created"]), 1)
        entry = result["created"][0]
        self.assertEqual(entry["engagementId"], self.engagement_id)
        job = self.store.get_job(entry["jobId"])
        self.assertEqual(job["kind"], "explore")
        self.assertEqual(job["engagementId"], self.engagement_id)
        self.assertEqual(job["payload"]["intentKey"], entry["intentKey"])
        self.assertTrue(job["payload"]["auto"])

    def test_quiet_window_skips_repeat(self):
        exploration.tick(self.store, force=True)
        again = exploration.tick(self.store, force=True)
        # 派过的那一条在静默期内不再派：否则每一轮都往队列塞同样的活。
        self.assertEqual(again["created"], [])
        self.assertTrue(again["skipped"])

    def test_pending_cap_skips(self):
        exploration.save_settings(self.store, {"maxPending": 1})
        self.store.create_job("explore", "10.10.10.110", self.engagement_id)
        result = exploration.tick(self.store, force=True)
        self.assertEqual(result["created"], [])
        self.assertTrue(result["skipped"])


if __name__ == "__main__":
    unittest.main()