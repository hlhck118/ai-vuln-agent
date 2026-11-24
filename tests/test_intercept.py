# -*- coding: utf-8 -*-
"""工具入参拦截：规则表、保存校验、试匹配，以及任务归档。

这一组测的是**判据**本身（哪条规则先命中、坏规则怎么处理、开关关掉之后是不是
真的不拦），而不是某条内置正则拦不拦得住某条命令 —— 那是规则内容的事，改一版
规则就要改一次测试。这里只固定「拦截机制的语义」。
"""
import unittest

from tests import support
from pentestd import intercept


class InterceptCatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("intercept.db")
        self.httpd, self.client = support.start(self.store)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def test_catalog_requires_session(self):
        directory, store = support.temporary_store("intercept-anon.db")
        httpd, client = support.start(store, admin=False)
        try:
            status, _payload = client.get("/api/v1/intercept")
            self.assertEqual(status, 401)
        finally:
            support.stop(httpd, store, directory)

    def test_catalog_ships_builtin_rules_and_enums(self):
        status, payload = self.client.get("/api/v1/intercept")
        self.assertEqual(status, 200)
        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["admin"])
        # 内置规则必须一次播种到位：少一条，界面就会出现一个谁也改不回来的空表。
        self.assertEqual(payload["counts"]["total"], len(intercept.DEFAULT_RULES))
        self.assertEqual(payload["counts"]["builtin"], len(intercept.DEFAULT_RULES))
        ids = [rule["id"] for rule in payload["rules"]]
        self.assertEqual(len(ids), len(set(ids)))
        # 排序由服务端定（priority 降序），前端不得重排。
        priorities = [rule["priority"] for rule in payload["rules"]]
        self.assertEqual(priorities, sorted(priorities, reverse=True))
        meta = payload["meta"]
        self.assertEqual(meta["targets"], ["tool_input"])
        self.assertEqual(meta["actions"], ["deny"])
        self.assertEqual(meta["settingsKey"], "intercept.enabled")

    def test_rule_fields_are_camel_case(self):
        status, payload = self.client.get("/api/v1/intercept")
        self.assertEqual(status, 200)
        rule = payload["rules"][0]
        for key in ("id", "name", "enabled", "priority", "matchTarget",
                    "matchType", "pattern", "action", "message",
                    "timeoutEnabled", "timeoutSeconds", "timeoutAction",
                    "builtin", "createdBy", "createdAt", "updatedAt"):
            self.assertIn(key, rule)


class InterceptRuleTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("intercept-rules.db")
        self.httpd, self.client = support.start(self.store)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def _create(self, **overrides):
        body = {"name": "测试规则", "pattern": "rm\\s+-rf\\s+/",
                "priority": 40, "enabled": True, "matchTarget": "tool_input",
                "matchType": "regex", "action": "deny", "message": "别删根"}
        body.update(overrides)
        return self.client.post("/api/v1/intercept/rules", body)

    def test_create_rule_gets_generated_id(self):
        status, payload = self._create()
        self.assertEqual(status, 200)
        self.assertTrue(payload["created"])
        rule = payload["rule"]
        self.assertTrue(rule["id"].startswith("ir-"))
        self.assertEqual(rule["name"], "测试规则")
        self.assertFalse(rule["builtin"])
        # 读回来必须是同一条：保存只回显入参就测不出落库丢字段。
        status, catalog = self.client.get("/api/v1/intercept")
        stored = [item for item in catalog["rules"] if item["id"] == rule["id"]]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["pattern"], "rm\\s+-rf\\s+/")

    def test_bad_regex_is_rejected(self):
        status, _payload = self._create(pattern="rm -rf (")
        self.assertEqual(status, 400)

    def test_priority_and_timeout_are_bounded(self):
        status, _payload = self._create(priority=1000)
        self.assertEqual(status, 400)
        status, _payload = self._create(timeoutEnabled=True, timeoutSeconds=0)
        self.assertEqual(status, 400)

    def test_literal_match_type_escapes_pattern(self):
        """literal 规则里那个 `.` 必须当字面量，不能当「任意字符」。

        这里**绕开**接口的 test 端点、直接喂 match_rules 单条规则：内置的
        「rm -rf」那条优先级更高，走端点会先命中它，于是这条断言永远为真、
        永远测不出 literal 的转义有没有做对。
        """
        status, payload = self._create(name="字面量", matchType="literal",
                                       pattern="rm -rf /tmp/a.b")
        self.assertEqual(status, 200)
        rule = payload["rule"]
        self.assertEqual(rule["matchType"], "literal")
        self.assertIsNone(intercept.match_rules([rule], ["rm -rf /tmp/axb"]))
        hit = intercept.match_rules([rule], ["rm -rf /tmp/a.b"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["id"], rule["id"])

    def test_update_keeps_builtin_flag(self):
        status, catalog = self.client.get("/api/v1/intercept")
        builtin = catalog["rules"][0]
        self.assertTrue(builtin["builtin"])
        status, payload = self.client.post("/api/v1/intercept/rules", {
            "id": builtin["id"], "name": builtin["name"] + "改",
            "pattern": builtin["pattern"], "priority": builtin["priority"],
            "enabled": False, "matchTarget": builtin["matchTarget"],
            "matchType": builtin["matchType"], "action": builtin["action"],
            "message": builtin["message"]})
        self.assertEqual(status, 200)
        self.assertFalse(payload["created"])
        self.assertTrue(payload["rule"]["builtin"])
        self.assertFalse(payload["rule"]["enabled"])

    def test_unknown_rule_update_is_404(self):
        status, _payload = self.client.post("/api/v1/intercept/rules", {
            "id": "ir-nope", "name": "x", "pattern": "x"})
        self.assertEqual(status, 404)

    def test_delete_rule(self):
        status, payload = self._create()
        rule_id = payload["rule"]["id"]
        status, deleted = self.client.post(
            "/api/v1/intercept/rules/%s/delete" % rule_id, {})
        self.assertEqual(status, 200)
        self.assertEqual(deleted["deleted"], rule_id)
        status, _payload = self.client.post(
            "/api/v1/intercept/rules/%s/delete" % rule_id, {})
        self.assertEqual(status, 404)

    def test_reset_only_restores_builtin(self):
        status, payload = self._create(name="自建")
        own_id = payload["rule"]["id"]
        status, result = self.client.post("/api/v1/intercept/reset", {})
        self.assertEqual(status, 200)
        self.assertEqual(result["restored"], len(intercept.DEFAULT_RULES))
        status, catalog = self.client.get("/api/v1/intercept")
        ids = [rule["id"] for rule in catalog["rules"]]
        # 自建的一条不能因为「恢复默认」而消失。
        self.assertIn(own_id, ids)


class InterceptMatchTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("intercept-match.db")
        self.httpd, self.client = support.start(self.store)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def test_test_endpoint_accepts_text_and_arguments(self):
        status, payload = self.client.post("/api/v1/intercept/test",
                                          {"tool": "shell",
                                           "text": "rm -rf /"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["matched"])
        self.assertIn("拦截规则", payload["message"])
        status, payload = self.client.post("/api/v1/intercept/test",
                                          {"tool": "shell",
                                           "arguments": {"command": "rm -rf /"}})
        self.assertEqual(status, 200)
        self.assertTrue(payload["matched"])
        status, payload = self.client.post("/api/v1/intercept/test",
                                          {"text": "ls -la /tmp"})
        self.assertFalse(payload["matched"])
        self.assertIsNone(payload["rule"])

    def test_match_message_hides_pattern(self):
        status, payload = self.client.post("/api/v1/intercept/test",
                                          {"tool": "shell",
                                           "text": "rm -rf /"})
        self.assertEqual(status, 200)
        # 原始 pattern 不给出去：模型只会照着它变形绕过。
        self.assertNotIn(payload["rule"]["pattern"], payload["message"])

    def _plane(self):
        class Plane(object):
            pass
        plane = Plane()
        plane.store = self.store
        return plane

    def test_disabled_switch_stops_matching(self):
        status, _payload = self.client.post("/api/v1/intercept/settings",
                                           {"enabled": False})
        self.assertEqual(status, 200)
        # 开关是字符串，bool("0") 会是 True —— 这条断言就是防那个坑。
        self.assertFalse(intercept.enabled(self.store))
        blocked, rule = intercept.guard(self._plane(), "shell",
                                        {"command": "rm -rf /"})
        self.assertEqual((blocked, rule), ("", None))
        status, _payload = self.client.post("/api/v1/intercept/settings",
                                           {"enabled": True})
        self.assertTrue(intercept.enabled(self.store))
        self.assertTrue(intercept.guard(self._plane(), "shell",
                                        {"command": "rm -rf /"})[1])

    def test_guard_passes_when_rule_table_broken(self):
        # 规则坏了必须放行：这一层是防手滑，不是安全边界。
        self.store.conn.execute(
            "UPDATE intercept_rules SET pattern=? WHERE builtin=1",
            ("rm -rf (",))
        self.store.conn.commit()
        blocked, rule = intercept.guard(self._plane(), "shell",
                                        {"command": "rm -rf /"})
        self.assertEqual((blocked, rule), ("", None))


class JobArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("job-archive.db")
        self.httpd, self.client = support.start(self.store)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def test_archive_hides_from_default_list(self):
        job = self.store.create_job("toolkit", "10.0.0.9")
        status, payload = self.client.post(
            "/api/v1/jobs/%s/archive" % job["id"], {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["job"]["archived"])
        status, listing = self.client.get("/api/v1/jobs")
        self.assertEqual(status, 200)
        self.assertNotIn(job["id"], [item["id"] for item in listing["jobs"]])
        status, listing = self.client.get("/api/v1/jobs?archived=1")
        self.assertIn(job["id"], [item["id"] for item in listing["jobs"]])
        status, listing = self.client.get("/api/v1/jobs?archived=all")
        self.assertIn(job["id"], [item["id"] for item in listing["jobs"]])
        status, payload = self.client.post(
            "/api/v1/jobs/%s/unarchive" % job["id"], {})
        self.assertEqual(status, 200)
        self.assertFalse(payload["job"]["archived"])
        status, listing = self.client.get("/api/v1/jobs")
        self.assertIn(job["id"], [item["id"] for item in listing["jobs"]])

    def test_archive_missing_job_is_404(self):
        status, _payload = self.client.post("/api/v1/jobs/job-nope/archive", {})
        self.assertEqual(status, 404)

    def test_batch_archive_defaults_to_finished_jobs(self):
        running = self.store.create_job("toolkit", "10.0.0.10")
        done = self.store.create_job("toolkit", "10.0.0.11")
        self.store.finish_job(done["id"], "done", {"written": {}})
        status, payload = self.client.post("/api/v1/jobs/archive", {})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(payload["archived"], 1)
        # 还在跑的任务不能被顺手藏起来：它在列表里消失但仍在占执行器。
        self.assertFalse(self.store.get_job(running["id"]).get("archived"))
        self.assertTrue(self.store.get_job(done["id"])["archived"])


class InterceptHookWiringTests(unittest.TestCase):
    """钩子必须真的在 check_tool 里，且排在归属判据之后。

    这不是风格检查：钩子排在归属判据之前，一个目标是别人的调用会先报拦截，
    干扰排障时对原因的判断；钩子干脆不在 check_tool 里，则所有工具调用都不设防，
    而接口层的测试全都会通过 —— 那种漏法只能靠源码断言发现。
    """

    def test_hook_present_and_ordered(self):
        import io
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "pentestd", "chat.py")
        with io.open(path, encoding="utf-8") as handle:
            source = handle.read()
        hook = source.index("intercept_module.guard(plane, name, args,")
        owner = source.index("_terminal_owner_problem(plane, name, args,")
        self.assertGreater(hook, owner)


if __name__ == "__main__":
    unittest.main()
