# -*- coding: utf-8 -*-
"""控制台令牌、跨源读、面板聚合。

三件事在一起测，是因为它们在**产品上是同一条通道**：DSH 网页端（另一个源）
拿一个机器凭据去读一份聚合数据。分开测会各自变绿，而合起来的失败形态恰好是
最容易被忽略的那一类——"每一块都对，但它们拼不起来"（例如：令牌能过鉴权，
可是预检被 401 挡住；或者面板数据对，但响应没带 CORS 头）。

因此这一份用例里既有单点（令牌文件、常数时间比对），也有**端到端**：
带 `Origin` + `Authorization` 的请求走真实的 HTTP 处理函数。
"""
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import auth                                          # noqa: E402
from tests import support                                          # noqa: E402


class ConsoleTokenFileTestCase(unittest.TestCase):
    """令牌文件本身：创建、幂等、轮换、常数时间比对。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("token.db")
        self.path = auth.console_token_path(self.directory.name)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_missing_file_means_no_token(self):
        """没配过 = 空串，而不是"自动生成一个"。"""
        self.assertEqual(auth.read_console_token(self.path), "")
        self.assertFalse(auth.verify_console_token(self.path, "ptc_whatever"))

    def test_ensure_creates_once_and_keeps_it(self):
        token, created = auth.ensure_console_token(self.path)
        self.assertTrue(created)
        self.assertTrue(token.startswith(auth.CONSOLE_TOKEN_PREFIX))
        again, created_again = auth.ensure_console_token(self.path)
        self.assertFalse(created_again)
        self.assertEqual(token, again,
                         "第二次调用必须是同一个令牌：启动时顺手换一个，"
                         "后果是'重启一次，面板全 401'")

    def test_rotate_replaces_and_invalidates(self):
        token, _ = auth.ensure_console_token(self.path)
        rotated = auth.rotate_console_token(self.path)
        self.assertNotEqual(token, rotated)
        self.assertFalse(auth.verify_console_token(self.path, token))
        self.assertTrue(auth.verify_console_token(self.path, rotated))

    def test_verify_is_exact_not_prefix(self):
        token, _ = auth.ensure_console_token(self.path)
        self.assertFalse(auth.verify_console_token(self.path, token[:10]))
        self.assertFalse(auth.verify_console_token(self.path, token + "x"))
        self.assertFalse(auth.verify_console_token(self.path, ""))

    def test_write_is_atomic_leaves_no_temp(self):
        auth.ensure_console_token(self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".tmp"),
                         "临时文件留在原地，会让'半个令牌'看起来像配置")

    def test_token_is_not_the_session_format(self):
        """前缀不同：现场看到一串东西时，能一眼分出是哪种凭据。"""
        self.assertNotEqual(auth.new_console_token()[:4],
                            auth.new_session_token()[:4])


class ConsoleTokenHttpTestCase(unittest.TestCase):
    """走真实 HTTP：令牌当凭据用、CORS 头、预检、面板聚合。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("panel.db")
        self.httpd, self.client = support.start(self.store)
        self.base = self.client.base
        # 管理员建一个令牌（走真实接口，不直接写文件）
        status, payload = self.client.get("/api/v1/console-token")
        self.assertEqual(status, 200, payload)
        self.token = payload["token"]
        self.assertTrue(self.token)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    # ------------------------------------------------------------ 工具

    def raw(self, method: str, path: str, headers=None, body=None):
        """**不用** support.Client：它带 Cookie，而这里要验的正是"不带 Cookie"。"""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data,
                                         method=method,
                                         headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                return response.status, payload, dict(response.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                payload = {"error": raw.decode("utf-8", "replace")}
            return exc.code, payload, dict(exc.headers)

    def bearer(self, extra=None):
        head = {"Authorization": "Bearer " + self.token,
                "Content-Type": "application/json"}
        head.update(extra or {})
        return head

    # ------------------------------------------------------------ 令牌

    def test_bearer_reads_without_cookie(self):
        status, payload, _ = self.raw("GET", "/api/v1/jobs", self.bearer())
        self.assertEqual(status, 200, payload)

    def test_wrong_token_is_401(self):
        status, payload, _ = self.raw("GET", "/api/v1/jobs",
                                      {"Authorization": "Bearer ptc_nope"})
        self.assertEqual(status, 401, payload)

    def test_no_token_and_no_cookie_is_401(self):
        status, _, _ = self.raw("GET", "/api/v1/jobs")
        self.assertEqual(status, 401)

    def test_token_writes_without_csrf(self):
        """写操作不需要 CSRF：浏览器不会自动带 Authorization 头。

        这一条是**行为契约**，不是"顺手放宽"：如果哪天它开始要求 CSRF，
        每一个机器调用方都会突然 403，而错误信息会指向一个它们拿不到的东西。
        """
        status, payload, _ = self.raw(
            "POST", "/api/v1/engagements", self.bearer(),
            {"target": "10.0.0.5", "objective": "面板写入用例"})
        self.assertEqual(status, 201, payload)

    def test_cookie_without_csrf_still_403(self):
        """放开的是令牌那一条路，**不是**把 CSRF 整个拿掉。"""
        status, payload = self.client.request("POST", "/api/v1/engagements",
                                              {"target": "10.0.0.6"}, with_csrf=False)
        self.assertEqual(status, 403, payload)

    def test_auth_me_answers_token_validity(self):
        status, payload, _ = self.raw("GET", "/api/v1/auth/me", self.bearer())
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload.get("viaConsoleToken"))
        self.assertEqual(payload["csrfToken"], "",
                         "令牌没有 CSRF 那半个；编一个出来会让界面显示一个不存在的值")

    def test_token_endpoint_refuses_to_be_swapped_by_token(self):
        """令牌不能换令牌：那等于一张可自我复制的永久通行证。"""
        status, payload, _ = self.raw("POST", "/api/v1/console-token",
                                      self.bearer(), {})
        self.assertEqual(status, 403, payload)

    def test_token_without_admin_says_so(self):
        """令牌对、但没有管理员时，报的必须是**缺管理员**，不是"令牌无效"。

        这一条来自真机：平台上还没有账号（首次打开控制台才建）时，令牌是有效的，
        而使用者刚把它复制过来——如果回"令牌无效"，他会去轮换、再粘、再失败，
        而真正要做的是另一件事。
        """
        # 把用户表清空：走 store 自己的删除能力，不绕过它写 SQL。
        for row in self.store.list_users():
            self.store.delete_user(row["id"])
        status, payload, _ = self.raw("GET", "/api/v1/jobs", self.bearer())
        self.assertEqual(status, 401, payload)
        self.assertIn("管理员", payload.get("error", ""))
        self.assertNotIn("令牌无效", payload.get("error", ""))

    def test_panel_allows_token(self):
        status, payload, _ = self.raw("GET", "/api/v1/panel", self.bearer())
        self.assertEqual(status, 200, payload)
        for key in ("platform", "counts", "engagements", "assets", "findings",
                    "jobs"):
            self.assertIn(key, payload)

    # ------------------------------------------------------------ CORS

    def test_cors_echoes_loopback_origin(self):
        status, _, headers = self.raw(
            "GET", "/api/v1/panel", self.bearer({"Origin": "http://127.0.0.1:8790"}))
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"),
                         "http://127.0.0.1:8790")
        self.assertIn("Origin", headers.get("Vary", ""))
        self.assertNotIn("Access-Control-Allow-Credentials", headers,
                         "一旦允许凭据，被放行的来源就能带着用户 Cookie 读数据；"
                         "跨源那一侧只该用自己头里的令牌")

    def test_cors_rejects_foreign_origin(self):
        status, _, headers = self.raw(
            "GET", "/api/v1/panel", self.bearer({"Origin": "https://evil.example"}))
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_cors_accepts_localhost_and_ipv6_loopback(self):
        for origin in ("http://localhost:8790", "http://[::1]:8790",
                       "http://127.0.0.1"):
            status, _, headers = self.raw(
                "GET", "/api/v1/health", {"Origin": origin})
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Access-Control-Allow-Origin"), origin,
                             "回环来源应当被接受：%s" % origin)

    def test_preflight_needs_no_credential(self):
        """预检**不带**凭据也要过 —— 否则浏览器连真正的请求都不会发。"""
        status, _, headers = self.raw(
            "OPTIONS", "/api/v1/panel",
            {"Origin": "http://127.0.0.1:8790",
             "Access-Control-Request-Method": "GET",
             "Access-Control-Request-Headers": "authorization"})
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"),
                         "http://127.0.0.1:8790")
        self.assertIn("Authorization", headers.get("Access-Control-Allow-Headers", ""))

    def test_extra_origin_from_env_is_honoured_exactly(self):
        """把服务放到局域网上之后，面板的来源不再是回环（见 `extra_cors_origins`）。

        这一条同时钉住两件事：清单里的来源被放行，而**它之外的**仍然被拒
        （包括"前缀相同但端口不同"这种最像的冒充写法）。
        """
        allowed = "http://10.10.10.200:8790"
        with mock.patch.dict(os.environ, {"PENTESTD_CORS_ORIGINS": allowed}):
            for origin, expected in ((allowed, allowed),
                                     ("http://10.10.10.200:8791", None),
                                     ("https://10.10.10.200:8790", None),
                                     ("http://evil.example", None)):
                status, _, headers = self.raw(
                    "GET", "/api/v1/panel", self.bearer({"Origin": origin}))
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Access-Control-Allow-Origin"),
                                 expected, "来源 %s 的放行判断错了" % origin)

    def test_without_the_env_var_loopback_is_still_the_only_one(self):
        """不配就不放——**默认收紧**，加这一项不是把门开着。"""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PENTESTD_CORS_ORIGINS", None)
            status, _, headers = self.raw(
                "GET", "/api/v1/panel",
                self.bearer({"Origin": "http://10.10.10.200:8790"}))
            self.assertEqual(status, 200)
            self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_preflight_from_forbidden_origin_is_refused(self):
        status, _, headers = self.raw(
            "OPTIONS", "/api/v1/health",
            {"Origin": "https://evil.example",
             "Access-Control-Request-Method": "GET"})
        self.assertEqual(status, 403)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    # ------------------------------------------------------------ 面板

    def test_panel_is_self_consistent(self):
        """计数与列表来自同一次查询：面板上不可能出现"18 个资产、列出 17 条"。"""
        engagement = self.store.create_engagement("10.0.0.9", "面板一致性", "")
        eid = engagement["id"]
        for index in range(3):
            self.store.add_node(eid, "asset",
                                {"type": "ip", "value": "10.0.0.%d" % index})
        status, payload, _ = self.raw("GET", "/api/v1/panel", self.bearer())
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["counts"]["assets"], len(payload["assets"]))
        self.assertEqual(payload["counts"]["findings"], len(payload["findings"]))
        self.assertEqual(payload["counts"]["jobs"], len(payload["jobs"]))

    def test_panel_unknown_engagement_is_404(self):
        status, payload, _ = self.raw("GET", "/api/v1/panel?engagement=nope",
                                      self.bearer())
        self.assertEqual(status, 404, payload)

    def test_panel_works_with_empty_platform(self):
        """空库不是错误：新装的平台打开面板应当看到"什么都没有"，而不是 500。"""
        status, payload, _ = self.raw("GET", "/api/v1/panel", self.bearer())
        self.assertEqual(status, 200, payload)
        self.assertIsNone(payload["engagement"])
        self.assertEqual(payload["counts"]["assets"], 0)


if __name__ == "__main__":
    unittest.main()
