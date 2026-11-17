# -*- coding: utf-8 -*-
"""登录认证与模型设置测试。

两组都尽量"真做"，而不是只测参数校验：

* 认证：走真实的 Cookie 会话、CSRF、登录节流、口令升级；并检查口令与会话令牌
  **在库里都不是明文**（这类问题只有直接读库才能发现）。
* 模型：起一个**假模型服务端**分别模拟 OpenAI 兼容 / Anthropic / Ollama 三种协议，
  断言我们发出去的请求形状正确；再跑一次真实扫描，确认模型配置真的作用到了引擎上，
  而不只是"存进了数据库"。
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import auth, engine, providers                     # noqa: E402
from pentestd.store import Store                                 # noqa: E402
from tests import support                                        # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")
ENGINE_HOME = engine.locate_engine()


# ---------------------------------------------------------------- 假模型服务端

class FakeProvider:
    """按协议返回固定响应，并记录收到的请求，用于断言请求形状。"""

    def __init__(self, protocol: str):
        self.protocol = protocol
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
                outer.requests.append({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body})
                if outer.protocol == "anthropic":
                    reply = {"content": [{"type": "text", "text": "pong-anthropic"}]}
                elif outer.protocol == "ollama":
                    reply = {"message": {"role": "assistant",
                                         "content": "pong-ollama"}}
                else:
                    reply = {"choices": [{"message": {"role": "assistant",
                                                      "content": "pong-openai"}}]}
                payload = json.dumps(reply).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d" % (host, port)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def last(self):
        return self.requests[-1] if self.requests else None


# ---------------------------------------------------------------- 认证

class AuthenticationTestCase(unittest.TestCase):
    def setUp(self):
        self.dir, self.store = support.temporary_store("auth.db")
        self.httpd, self.client = support.start(self.store, WEB_DIR)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.dir.cleanup()

    def _anonymous(self):
        return support.Client(self.client.base)

    # ---------------------------------------------------------- 访问控制
    def test_console_requires_login(self):
        anon = self._anonymous()
        for path in ("/api/v1/engagements", "/api/v1/executors", "/api/v1/jobs",
                     "/api/v1/baselines", "/api/v1/setup", "/api/v1/users",
                     "/api/v1/models"):
            status, payload = anon.get(path)
            self.assertEqual(status, 401, "%s 未登录却可访问" % path)
            self.assertIn("error", payload)

    def test_auth_state_is_public_and_reports_setup(self):
        anon = self._anonymous()
        status, state = anon.get("/api/v1/auth/state")
        self.assertEqual(status, 200, "auth/state 必须公开，否则前端无法判断该显示什么")
        self.assertIn("needsSetup", state)
        self.assertIn("authenticated", state)
        self.assertFalse(state["authenticated"])

    def test_static_assets_are_reachable_without_login(self):
        """登录页本身必须能加载，否则死锁。"""
        anon = self._anonymous()
        for path in ("/", "/app.js", "/style.css"):
            with anon.opener.open(anon.base + path, timeout=10) as response:
                self.assertEqual(response.status, 200)

    def test_node_endpoints_reject_session_cookies(self):
        """节点通道只认节点令牌，不认控制台会话——两条信任链不能互相穿透。"""
        status, _ = self.client.get("/api/v1/jobs/pull?executorId=exec-whatever")
        self.assertEqual(status, 401)

    # ---------------------------------------------------------- 首次创建
    def test_bootstrap_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "fresh.db"))
            httpd, _ = support.start(store, WEB_DIR, admin=False)
            client = support.Client("http://127.0.0.1:%d" % httpd.server_address[1])
            try:
                self.assertTrue(client.get("/api/v1/auth/state")[1]["needsSetup"])
                self.assertEqual(
                    client.post("/api/v1/auth/bootstrap",
                                {"username": "admin", "password": "short"})[0], 400)
                self.assertEqual(
                    client.post("/api/v1/auth/bootstrap",
                                {"username": "admin",
                                 "password": "alllowercaseletters"})[0], 400)
                status, payload = client.bootstrap()
                self.assertEqual(status, 201)
                self.assertEqual(payload["user"]["role"], "admin")
                self.assertTrue(payload["csrfToken"])
                # 已有用户后不能再创建
                other = support.Client(client.base)
                status, _ = other.post("/api/v1/auth/bootstrap",
                                       {"username": "root",
                                        "password": "Another-Pass1!"})
                self.assertEqual(status, 409)
            finally:
                httpd.shutdown()
                httpd.server_close()
                store.close()

    def test_password_never_stored_in_plaintext(self):
        row = self.store.get_user_by_name(support.ADMIN_USER)
        stored = row["password_hash"]
        self.assertNotIn(support.ADMIN_PASSWORD, stored)
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        iterations = int(stored.split("$")[1])
        self.assertGreaterEqual(iterations, 200_000,
                                "迭代次数过低会让离线爆破变得便宜")

    def test_session_token_stored_as_digest_only(self):
        cookie = None
        for item in self.client.jar:
            if item.name == auth.COOKIE_NAME:
                cookie = item.value
        self.assertTrue(cookie, "登录后应下发会话 Cookie")
        rows = self.store.conn.execute(
            "SELECT token_hash FROM sessions").fetchall()
        stored = [r["token_hash"] for r in rows]
        self.assertNotIn(cookie, stored, "库里不得存明文会话令牌")
        self.assertIn(auth.token_digest(cookie), stored)

    # ---------------------------------------------------------- 登录
    def test_login_success_and_failure(self):
        anon = self._anonymous()
        status, payload = anon.login(password="Wrong-Passw0rd!")
        self.assertEqual(status, 401)
        self.assertIn("用户名或密码错误", payload["error"])
        self.assertEqual(anon.login()[0], 200)

    def test_login_is_throttled(self):
        anon = self._anonymous()
        for _ in range(auth.LOCK_THRESHOLD):
            anon.login(password="Wrong-Passw0rd!")
        status, payload = anon.login(password="Wrong-Passw0rd!")
        self.assertEqual(status, 423, "连续失败后应锁定")
        self.assertIn("秒后再试", payload["error"])
        # 锁定期间即使密码正确也不放行
        self.assertEqual(anon.login()[0], 423)

    def test_spoofed_forwarded_for_does_not_reset_the_throttle(self):
        """**换 `X-Forwarded-For` 不能换来新的尝试额度。**

        这一条钉的是一个真实存在过的漏洞：来源取自那个头，而那个头由请求方自己写，
        于是"每试一次换一个值"= 每一次都是全新的计数桶，用户级锁定一次都不触发
        （修复前实测连打二十次全部 401）。现在的默认口径是**只信 TCP 对端**，
        所以同一台机器无论怎么写这个头，都是同一个来源。
        """
        anon = self._anonymous()
        codes = [anon.login(password="Wrong-Passw0rd!")[0]
                 for _ in range(auth.LOCK_THRESHOLD)]
        self.assertNotIn(423, codes, "额度内的失败不该提前锁定")
        # 换一个来源继续试：不该被当成新额度
        status = anon.request("POST", "/api/v1/auth/login",
                              {"username": support.ADMIN_USER,
                               "password": "Wrong-Passw0rd!"},
                              {"X-Forwarded-For": "203.0.113.77"})[0]
        self.assertEqual(423, status, "换 X-Forwarded-For 不能解锁")

    def test_account_wide_threshold_catches_rotating_sources(self):
        """按账号合计的那道闸：换着来源试也走不脱。

        把来源那一半完全交给一个**每次都不同**的值（模拟换出口/伪造头），第一道闸
        永远不会满；只有账号级那一档还能数得下去。
        """
        anon = self._anonymous()
        # 直接往库里灌失败记录，模拟"来源各不相同"的失败集合——比发三十次真请求快，
        # 也把断言限定在判定逻辑上。每条的 client_ip 都不同。
        for index in range(auth.LOCK_THRESHOLD_USER):
            self.store.record_attempt(support.ADMIN_USER, "198.51.100.%d" % index, False)
        status, payload = anon.login(password="Wrong-Passw0rd!")
        self.assertEqual(423, status, "账号级阈值应当在来源轮换后仍然锁定")
        self.assertIn("秒后再试", payload["error"])

    def test_successful_login_clears_failures_from_every_source(self):
        """登录成功后，**所有来源**的旧失败记录都该清掉。

        不清的话，第二道闸会把上一次（别的来源上的）失败算进这一次，下一次无关的
        登录会撞上一个它没参与过的计数。
        """
        for index in range(3):
            self.store.record_attempt(support.ADMIN_USER, "198.51.100.%d" % index, False)
        self.assertEqual(200, self.client.login()[0])
        self.assertEqual([], self.store.recent_failures_for_user(
            support.ADMIN_USER, auth.LOCK_WINDOW_S))

    def test_logout_invalidates_session(self):
        self.assertEqual(self.client.get("/api/v1/engagements")[0], 200)
        self.assertEqual(self.client.logout()[0], 200)
        self.assertEqual(self.client.get("/api/v1/engagements")[0], 401)

    def test_csrf_required_for_writes(self):
        client = support.Client(self.client.base)
        client.login()
        # 故意不带上 CSRF 头
        status, payload = client.request("POST", "/api/v1/engagements",
                                         {"target": "x"}, with_csrf=False)
        self.assertEqual(status, 403)
        self.assertIn("CSRF", payload["error"])
        # 带上就正常
        self.assertEqual(client.post("/api/v1/engagements", {"target": "x"})[0], 201)

    def test_forged_csrf_rejected(self):
        client = support.Client(self.client.base)
        client.login()
        status, _ = client.post("/api/v1/engagements", {"target": "x"},
                                {"X-CSRF-Token": "forged-token"})
        self.assertEqual(status, 403)

    # ---------------------------------------------------------- 改密
    def test_change_password(self):
        status, _ = self.client.post("/api/v1/auth/password",
                                     {"currentPassword": "Wrong-Pass1!",
                                      "newPassword": "New-Admin-Pass2!"})
        self.assertEqual(status, 401)
        status, _ = self.client.post("/api/v1/auth/password",
                                     {"currentPassword": support.ADMIN_PASSWORD,
                                      "newPassword": "short"})
        self.assertEqual(status, 400)
        status, _ = self.client.post("/api/v1/auth/password",
                                     {"currentPassword": support.ADMIN_PASSWORD,
                                      "newPassword": "New-Admin-Pass2!"})
        self.assertEqual(status, 200)
        fresh = self._anonymous()
        self.assertEqual(fresh.login(password=support.ADMIN_PASSWORD)[0], 401)
        self.assertEqual(fresh.login(password="New-Admin-Pass2!")[0], 200)

    def test_legacy_hash_is_upgraded_on_login(self):
        """旧的低迭代口令在登录成功那一刻自动升级，不需要用户改密码。"""
        weak = auth.hash_password("Legacy-Pass1!", iterations=1000)
        self.store.create_user("legacy", weak, "operator")
        anon = self._anonymous()
        self.assertEqual(anon.login("legacy", "Legacy-Pass1!")[0], 200)
        stored = self.store.get_user_by_name("legacy")["password_hash"]
        self.assertGreaterEqual(int(stored.split("$")[1]), 200_000,
                                "登录后应把迭代次数升到当前标准")

    # ---------------------------------------------------------- 用户管理
    def test_user_management(self):
        status, payload = self.client.post("/api/v1/users",
                                           {"username": "ops", "role": "operator",
                                            "password": "Operator-Pass1!"})
        self.assertEqual(status, 201)
        user_id = payload["user"]["id"]
        self.assertNotIn("password", json.dumps(payload["user"]).lower())
        self.assertEqual(self.client.post(
            "/api/v1/users", {"username": "ops", "role": "operator",
                              "password": "Operator-Pass1!"})[0], 409)

        listed = self.client.get("/api/v1/users")[1]["users"]
        self.assertIn("ops", [u["username"] for u in listed])

        # 非管理员不能管理用户
        ops = support.Client(self.client.base)
        self.assertEqual(ops.login("ops", "Operator-Pass1!")[0], 200)
        self.assertEqual(ops.get("/api/v1/users")[0], 403)
        self.assertEqual(ops.post("/api/v1/users",
                                  {"username": "x", "role": "admin",
                                   "password": "Whatever-Pass1!"})[0], 403)

        self.assertEqual(self.client.post(
            "/api/v1/users/%s/disable" % user_id, {"disabled": True})[0], 200)
        self.assertEqual(ops.get("/api/v1/engagements")[0], 401,
                         "停用后其会话应立即失效")
        self.assertEqual(self.client.post("/api/v1/users/%s/delete" % user_id)[0], 200)

    def test_cannot_lock_out_the_last_admin(self):
        admin_id = self.store.get_user_by_name(support.ADMIN_USER)["id"]
        status, payload = self.client.post("/api/v1/users/%s/disable" % admin_id,
                                           {"disabled": True})
        self.assertEqual(status, 409)
        self.assertIn("当前登录", payload["error"])
        other_admin = self.client.post(
            "/api/v1/users", {"username": "admin2", "role": "admin",
                              "password": "Second-Admin-Pass1!"})
        self.assertEqual(other_admin[0], 201)
        # 有第二个管理员后，停用自己仍被拒（当前会话），但删除自己也被拒
        self.assertEqual(self.client.post(
            "/api/v1/users/%s/delete" % admin_id)[0], 409)

    def test_no_auth_escape_hatch(self):
        """显式关闭认证后接口可用，且界面不该再卡在登录门。

        这是被 UX 评审抓出来的不一致：`auth/state` 已回 `authRequired:false`，
        但如果 `authenticated` 仍是 false，前端就会一直显示登录页——
        用户看到一个既用不上也去不掉的登录门。
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "noauth.db"))
            httpd, _ = support.start(store, WEB_DIR, admin=False,
                                     auth_required=False)
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            try:
                anon = support.Client(base)
                self.assertEqual(anon.get("/api/v1/engagements")[0], 200)
                _status, setup = anon.get("/api/v1/setup")
                self.assertFalse(setup["auth"]["consoleLogin"])
                # 关键断言：应直接进入控制台，而不是停在登录门
                _status, state = anon.get("/api/v1/auth/state")
                self.assertFalse(state["authRequired"])
                self.assertTrue(state["authenticated"],
                                "关闭认证后应直接视为已认证，否则界面卡在登录门")
                self.assertFalse(state["needsSetup"])
                self.assertEqual(state["user"]["username"], "本地调试")
                # 前端 start() 在 state 未带 csrfToken 时会补一次 /auth/me；
                # 它也必须说"已认证"，否则控制台被 401 弹回登录门（实测现场：
                # state 200 / me 401，界面停在"登录状态已失效"）。
                _status, me = anon.get("/api/v1/auth/me")
                self.assertEqual(me["user"]["username"], "本地调试")
                self.assertTrue(me["authenticated"])
                self.assertFalse(me["authRequired"])
                # 没有账号体系，改密应被明确拒绝而不是 500
                status, payload = anon.post("/api/v1/auth/password",
                                            {"currentPassword": "x",
                                             "newPassword": "y"})
                self.assertEqual(status, 400)
                self.assertIn("调试模式", payload["error"])
            finally:
                support.stop(httpd, store)


# ---------------------------------------------------------------- 密钥保管

class SecretBoxTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.box = providers.SecretBox(os.path.join(self.dir.name, "secret.key"))

    def tearDown(self):
        self.dir.cleanup()

    def test_roundtrip(self):
        for plaintext in ("sk-abcdef1234567890", "短密钥", "a" * 500):
            self.assertEqual(self.box.decrypt(self.box.encrypt(plaintext)), plaintext)

    def test_same_plaintext_gives_different_ciphertext(self):
        first = self.box.encrypt("sk-same")
        second = self.box.encrypt("sk-same")
        self.assertNotEqual(first, second, "每次加密都必须换随机数")

    def test_tampered_ciphertext_is_rejected(self):
        token = self.box.encrypt("sk-secret-value")
        version, nonce, cipher, tag = token.split(":")
        flipped = "A" if cipher[0] != "A" else "B"
        broken = ":".join([version, nonce, flipped + cipher[1:], tag])
        with self.assertRaises(ValueError):
            self.box.decrypt(broken)

    def test_wrong_key_is_rejected(self):
        token = self.box.encrypt("sk-secret-value")
        other = providers.SecretBox(os.path.join(self.dir.name, "other.key"))
        with self.assertRaises(ValueError):
            other.decrypt(token)

    def test_key_file_is_created_once(self):
        self.box.encrypt("x")
        path = os.path.join(self.dir.name, "secret.key")
        with open(path, "rb") as handle:
            first = handle.read()
        again = providers.SecretBox(path)
        again.encrypt("y")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), first, "密钥文件不得被重写")

    def test_mask_hides_middle(self):
        masked = providers.mask_secret("sk-1234567890abcdef")
        self.assertNotIn("567890abc", masked)
        self.assertTrue(masked.startswith("sk-"))


# ---------------------------------------------------------------- 模型设置

class ModelSettingsTestCase(unittest.TestCase):
    def setUp(self):
        self.dir, self.store = support.temporary_store("models.db")
        self.httpd, self.client = support.start(self.store, WEB_DIR)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.dir.cleanup()

    def test_presets_cover_cloud_and_local(self):
        payload = self.client.get("/api/v1/models")[1]
        presets = {p["id"]: p for p in payload["presets"]}
        for expected in ("openai", "deepseek", "qwen", "zhipu", "moonshot",
                         "anthropic", "ollama", "vllm", "lmstudio", "llamacpp"):
            self.assertIn(expected, presets, "预设目录缺少 %s" % expected)
        for preset in payload["presets"]:
            self.assertIn(preset["provider"], providers.PROVIDERS)
            self.assertTrue(preset["scanCapable"],
                            "%s 声称可扫描，必须真的能用于扫描" % preset["id"])
        self.assertTrue(any(p["local"] for p in payload["presets"]),
                        "必须包含本地模型预设")
        self.assertEqual(len(payload["keySources"]), 4)

    def test_profile_crud_and_activation(self):
        status, payload = self.client.post("/api/v1/models", {
            "name": "本机 Ollama", "provider": "ollama",
            "baseUrl": "http://127.0.0.1:11434", "model": "qwen2.5:14b",
            "apiKeySource": "none"})
        self.assertEqual(status, 201)
        profile = payload["profile"]
        self.assertTrue(profile["active"], "第一条配置应自动启用")
        self.assertNotIn("apiKeySecret", profile)

        second = self.client.post("/api/v1/models", {
            "name": "OpenAI", "provider": "openai_compat",
            "baseUrl": "https://api.openai.com/v1", "model": "gpt-4o-mini",
            "apiKeySource": "env", "apiKeyRef": "OPENAI_API_KEY"})[1]["profile"]
        self.assertFalse(second["active"])
        self.assertEqual(self.client.post(
            "/api/v1/models/%s/activate" % second["id"])[1]["activeId"], second["id"])
        self.assertEqual(self.client.get("/api/v1/models")[1]["activeId"], second["id"])

        updated = self.client.post("/api/v1/models/%s" % second["id"], {
            "name": "OpenAI（改）", "provider": "openai_compat",
            "baseUrl": "https://api.openai.com/v1", "model": "gpt-4o",
            "apiKeySource": "env", "apiKeyRef": "OPENAI_API_KEY"})[1]["profile"]
        self.assertEqual(updated["name"], "OpenAI（改）")
        self.assertEqual(updated["model"], "gpt-4o")
        self.assertEqual(self.client.post(
            "/api/v1/models/%s/delete" % second["id"])[0], 200)

    def test_profile_validation(self):
        for body in ({"name": "", "provider": "ollama",
                      "baseUrl": "http://x", "model": "m"},
                     {"name": "n", "provider": "bogus",
                      "baseUrl": "http://x", "model": "m"},
                     {"name": "n", "provider": "ollama", "baseUrl": "", "model": "m"},
                     {"name": "n", "provider": "ollama", "baseUrl": "http://x",
                      "model": ""}):
            status, _ = self.client.post("/api/v1/models", body)
            self.assertEqual(status, 400, "非法配置应被拒绝：%s" % body)

    def test_inline_key_is_encrypted_and_never_returned(self):
        profile = self.client.post("/api/v1/models", {
            "name": "带密钥", "provider": "openai_compat",
            "baseUrl": "https://api.example.com/v1", "model": "m",
            "apiKeySource": "inline", "apiKey": "sk-super-secret-value"})[1]["profile"]
        self.assertTrue(profile["hasInlineKey"])
        self.assertNotIn("sk-super-secret-value", json.dumps(profile))
        raw = self.store.get_profile_raw(profile["id"])
        self.assertNotIn("sk-super-secret-value", raw["api_key_secret"],
                         "库里不得出现明文密钥")
        self.assertTrue(raw["api_key_secret"].startswith("v1:"),
                        "应存密文而不是明文")
        # 解析回来仍应得到原密钥（用控制面同一把密钥文件）
        plane_box = providers.SecretBox(os.path.join(self.dir.name, "secret.key"))
        value, note = providers.resolve_api_key(
            {"apiKeySource": "inline", "apiKeySecret": raw["api_key_secret"],
             "apiKeyRef": ""}, plane_box)
        self.assertEqual(value, "sk-super-secret-value")
        self.assertIn("加密", note)

    def test_resolve_key_sources(self):
        box = providers.SecretBox(os.path.join(self.dir.name, "k.key"))
        os.environ["PENTEST_TEST_KEY"] = "from-env"
        try:
            value, note = providers.resolve_api_key(
                {"apiKeySource": "env", "apiKeyRef": "PENTEST_TEST_KEY"}, box)
            self.assertEqual(value, "from-env")
            self.assertIn("PENTEST_TEST_KEY", note)
            with self.assertRaises(ValueError):
                providers.resolve_api_key(
                    {"apiKeySource": "env", "apiKeyRef": "NOT_SET_ANYWHERE"}, box)
        finally:
            os.environ.pop("PENTEST_TEST_KEY", None)

        key_file = os.path.join(self.dir.name, "api.key")
        with open(key_file, "w", encoding="utf-8") as handle:
            handle.write("from-file\n")
        value, _ = providers.resolve_api_key(
            {"apiKeySource": "file", "apiKeyRef": key_file}, box)
        self.assertEqual(value, "from-file")
        with self.assertRaises(ValueError):
            providers.resolve_api_key(
                {"apiKeySource": "file", "apiKeyRef": key_file + ".missing"}, box)

        value, _ = providers.resolve_api_key(
            {"apiKeySource": "none", "apiKeyRef": ""}, box)
        self.assertEqual(value, "")
        with self.assertRaises(ValueError):
            providers.resolve_api_key({"apiKeySource": "inline",
                                       "apiKeySecret": ""}, box)


class ProtocolAdapterTestCase(unittest.TestCase):
    """用假服务端验证三种协议的请求形状——这才是"兼容"的实证。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("proto.db")
        self.httpd, self.client = support.start(self.store, WEB_DIR)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.dir.cleanup()

    def _profile_for(self, fake, provider, model, key_source="none", key_ref="",
                     api_key=""):
        body = {"name": "%s 测试" % provider, "provider": provider,
                "baseUrl": fake.base_url, "model": model,
                "apiKeySource": key_source, "apiKeyRef": key_ref}
        if api_key:
            body["apiKey"] = api_key
        return self.client.post("/api/v1/models", body)[1]["profile"]

    def test_openai_compatible_request_shape(self):
        fake = FakeProvider("openai")
        try:
            profile = self._profile_for(fake, "openai_compat", "gpt-4o-mini",
                                        "inline", api_key="sk-test-openai")
            status, result = self.client.post(
                "/api/v1/models/%s/test" % profile["id"], {})
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"], result["message"])
            self.assertEqual(result["sample"], "pong-openai")
            sent = fake.last()
            self.assertEqual(sent["path"], "/chat/completions")
            self.assertEqual(sent["headers"].get("authorization"),
                             "Bearer sk-test-openai")
            self.assertEqual(sent["body"]["model"], "gpt-4o-mini")
            self.assertTrue(sent["body"]["messages"])
        finally:
            fake.stop()

    def test_anthropic_request_shape(self):
        fake = FakeProvider("anthropic")
        try:
            profile = self._profile_for(fake, "anthropic", "claude-sonnet-4-5",
                                        "inline", api_key="sk-ant-test")
            status, result = self.client.post(
                "/api/v1/models/%s/test" % profile["id"], {})
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"], result["message"])
            self.assertEqual(result["sample"], "pong-anthropic")
            sent = fake.last()
            self.assertEqual(sent["path"], "/messages")
            self.assertEqual(sent["headers"].get("x-api-key"), "sk-ant-test")
            self.assertNotIn("authorization", sent["headers"],
                             "Anthropic 不认 Bearer，别把 OpenAI 的头也发出去")
            self.assertIn("anthropic-version", sent["headers"])
            self.assertIn("max_tokens", sent["body"])
        finally:
            fake.stop()

    def test_ollama_request_shape(self):
        fake = FakeProvider("ollama")
        try:
            profile = self._profile_for(fake, "ollama", "qwen2.5:14b", "none")
            status, result = self.client.post(
                "/api/v1/models/%s/test" % profile["id"], {})
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"], result["message"])
            self.assertEqual(result["sample"], "pong-ollama")
            sent = fake.last()
            self.assertEqual(sent["path"], "/api/chat")
            self.assertNotIn("authorization", sent["headers"],
                             "本地 Ollama 不需要密钥")
            self.assertFalse(sent["body"].get("stream"), "必须显式关掉流式")
        finally:
            fake.stop()

    def test_unreachable_endpoint_gives_actionable_error(self):
        profile = self.client.post("/api/v1/models", {
            "name": "打不通", "provider": "openai_compat",
            "baseUrl": "http://127.0.0.1:1", "model": "m",
            "apiKeySource": "none", "timeoutS": 3})[1]["profile"]
        status, result = self.client.post("/api/v1/models/%s/test" % profile["id"],
                                          {})
        self.assertEqual(status, 200, "测试失败也应是 200 + ok=false")
        self.assertFalse(result["ok"])
        self.assertIn("无法连接", result["message"])

    def test_missing_env_key_gives_clear_message(self):
        profile = self.client.post("/api/v1/models", {
            "name": "缺密钥", "provider": "openai_compat",
            "baseUrl": "https://api.openai.com/v1", "model": "gpt-4o-mini",
            "apiKeySource": "env",
            "apiKeyRef": "PENTEST_ABSENT_KEY_FOR_TEST"})[1]["profile"]
        _status, result = self.client.post("/api/v1/models/%s/test" % profile["id"],
                                           {})
        self.assertFalse(result["ok"])
        self.assertIn("未设置", result["message"])

    def test_test_result_is_recorded_on_profile(self):
        fake = FakeProvider("openai")
        try:
            profile = self._profile_for(fake, "openai_compat", "m", "none")
            self.client.post("/api/v1/models/%s/test" % profile["id"], {})
            refreshed = [p for p in self.client.get("/api/v1/models")[1]["profiles"]
                         if p["id"] == profile["id"]][0]
            self.assertIsNotNone(refreshed["lastTest"])
            self.assertTrue(refreshed["lastTest"]["ok"])
            self.assertIn("at", refreshed["lastTest"])
        finally:
            fake.stop()

    def test_non_admin_cannot_manage_models(self):
        self.client.post("/api/v1/users", {"username": "ops", "role": "operator",
                                           "password": "Operator-Pass1!"})
        ops = support.Client(self.client.base)
        ops.login("ops", "Operator-Pass1!")
        self.assertEqual(ops.get("/api/v1/models")[0], 200, "只读可以")
        self.assertEqual(ops.post("/api/v1/models", {
            "name": "x", "provider": "ollama", "baseUrl": "http://x",
            "model": "m"})[0], 403, "改配置需要管理员")


@unittest.skipUnless(ENGINE_HOME, "未找到漏洞发掘引擎（vulnagent）")
class ModelUsedByScanTestCase(unittest.TestCase):
    """模型配置必须真的作用到扫描上，而不是只存进数据库。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("scan.db")
        self.httpd, self.client = support.start(self.store, WEB_DIR)
        self.fake = FakeProvider("openai")

    def tearDown(self):
        self.fake.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.dir.cleanup()

    def test_active_model_reaches_the_engine(self):
        demo = os.path.join(ENGINE_HOME, "demo", "vulnapp")
        patched = os.path.join(ENGINE_HOME, "demo", "vulnapp_patched")
        self.client.post("/api/v1/models", {
            "name": "假模型", "provider": "openai_compat",
            "baseUrl": self.fake.base_url, "model": "fake-model",
            "apiKeySource": "inline", "apiKey": "sk-fake-key",
            "temperature": 0.3, "maxTokens": 512, "timeoutS": 20})
        status, created = self.client.post("/api/v1/jobs", {
            "target": demo, "patched": patched, "objective": "模型接入验证"})
        self.assertEqual(status, 201)
        self.assertEqual(created["model"], "假模型")

        from pentestd import agent as agent_module
        executor = agent_module.LocalExecutor(self.store, work_root=self.dir.name,
                                              verbose=False)
        self.assertTrue(executor.run_once())
        job = self.store.get_job(created["jobId"])
        self.assertEqual(job["status"], "done")
        summary = job["summary"]
        self.assertEqual(summary["model"]["provider"], "openai_compat")
        self.assertEqual(summary["model"]["model"], "fake-model")

        # 关键：假模型服务端真的收到了引擎发来的推理请求
        self.assertTrue(self.fake.requests, "模型配置没有作用到引擎上")
        sent = self.fake.last()
        self.assertEqual(sent["path"], "/chat/completions")
        self.assertEqual(sent["body"]["model"], "fake-model")
        self.assertEqual(sent["headers"].get("authorization"), "Bearer sk-fake-key")

    def test_broken_model_config_blocks_job_creation(self):
        """配了模型却解析不出密钥时应当场拒绝，而不是静默退回启发式。"""
        demo = os.path.join(ENGINE_HOME, "demo", "vulnapp")
        self.client.post("/api/v1/models", {
            "name": "缺密钥", "provider": "openai_compat",
            "baseUrl": self.fake.base_url, "model": "m",
            "apiKeySource": "env",
            "apiKeyRef": "PENTEST_ABSENT_KEY_FOR_TEST"})
        status, payload = self.client.post("/api/v1/jobs", {"target": demo})
        self.assertEqual(status, 400)
        self.assertIn("无法使用", payload["error"])
        self.assertIn("设置", payload["error"])

    def test_chain_records_which_model_reasoned(self):
        demo = os.path.join(ENGINE_HOME, "demo", "vulnapp")
        patched = os.path.join(ENGINE_HOME, "demo", "vulnapp_patched")
        self.client.post("/api/v1/models", {
            "name": "假模型", "provider": "openai_compat",
            "baseUrl": self.fake.base_url, "model": "fake-model",
            "apiKeySource": "inline", "apiKey": "k", "timeoutS": 20})
        _status, created = self.client.post("/api/v1/jobs", {
            "target": demo, "patched": patched})
        from pentestd import agent as agent_module
        agent_module.LocalExecutor(self.store, work_root=self.dir.name,
                                   verbose=False).run_once()
        view = self.store.view(created["engagementId"])
        details = []
        for fact in view["nodes"]["facts"]:
            details.append(fact.get("detail")
                           or (fact.get("payload") or {}).get("detail", ""))
        self.assertTrue(any("fake-model" in detail and "假模型" in detail
                            for detail in details),
                        "链路里应同时留下配置名与模型名，便于审计与复现：%s"
                        % details[:3])

    def test_scan_without_active_model_falls_back_to_heuristic(self):
        demo = os.path.join(ENGINE_HOME, "demo", "vulnapp")
        patched = os.path.join(ENGINE_HOME, "demo", "vulnapp_patched")
        _status, created = self.client.post("/api/v1/jobs", {
            "target": demo, "patched": patched})
        from pentestd import agent as agent_module
        agent_module.LocalExecutor(self.store, work_root=self.dir.name,
                                   verbose=False).run_once()
        job = self.store.get_job(created["jobId"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["summary"]["model"]["provider"], "heuristic")
        self.assertGreaterEqual(job["summary"]["findings"], 1,
                                "没有模型时离线启发式也要能出结果")


if __name__ == "__main__":
    unittest.main(verbosity=2)
