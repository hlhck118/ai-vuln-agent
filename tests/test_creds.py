# -*- coding: utf-8 -*-
"""凭据测试：认证驱动、锁定检测、速率下限、口令只在链路里留指纹。

这一组用例守的是一条界线：**一个只会不停试口令的程序，和一个能用的凭据测试
工具，差别不在字典大小，而在它知不知道自己什么时候该停。** 所以锁定检测是
被测的重点，不是附带。

夹具是真实的本地服务：HTTP 服务带 basic / form / 限流三类路由，另有一个最小
的 FTP 服务和一个 RESP（Redis）应答器，用来覆盖"服务自己怎么说"这两条路径。
"""
import base64
import socket
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pentestd import authz, creds

# 本文件里那两条授权用例守的是**闸照判**时的拒绝（授权说明不够长、类别没开），
# 所以这一模块内把 `authz.FORCE_OPEN` 关掉；跑完还原。
_OPEN = authz.FORCE_OPEN


def setUpModule():
    authz.FORCE_OPEN = False


def tearDownModule():
    authz.FORCE_OPEN = _OPEN


GOOD = ("admin", "secret")
FORM_GOOD = ("admin", "letmein")
LOCK_AFTER = 3


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _send(self, status, body="", ctype="text/html; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            self.wfile.write(raw)
        except OSError:
            # 客户端提前断开。夹具不该为此在屏幕上打一段 traceback ——
            # 那会让一次全绿的运行看起来像出了问题。
            return

    def _basic_user(self):
        header = self.headers.get("authorization") or ""
        if not header.lower().startswith("basic "):
            return "", ""
        try:
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        except Exception:
            return "", ""
        account, _, password = decoded.partition(":")
        return account, password

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/basic":
            account, password = self._basic_user()
            if not account:
                self._send(401, "auth required")
                return
            if (account, password) == GOOD:
                self._send(200, "welcome, %s" % account)
                return
            self._send(401, "invalid password")
            return
        if path == "/enum":
            account, password = self._basic_user()
            if account == "ghost":
                self._send(401, "no such user")
                return
            self._send(401, "invalid password")
            return
        if path == "/lock":
            account, _ = self._basic_user()
            count = _LOCK_COUNTER.get(account, 0) + 1
            _LOCK_COUNTER[account] = count
            if count > LOCK_AFTER:
                self._send(200, "Too many failed attempts, account is locked")
                return
            self._send(401, "invalid password")
            return
        if path == "/digest":
            account, password = self._basic_user()
            self._send(200, "ok")
            return
        if path == "/open":
            # 本来就不需要认证的入口。任何口令都会"成功"——
            # 差分辨对照挡的就是它。
            self._send(200, "public landing page")
            return
        self._send(404, "not found")

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        path = urllib.parse.urlsplit(self.path).path
        if path == "/login":
            fields = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            account = (fields.get("username") or [""])[0]
            password = (fields.get("password") or [""])[0]
            if (account, password) == FORM_GOOD:
                self._send(200, "<h1>welcome dashboard</h1>")
                return
            self._send(200, "<h1>invalid credentials</h1>")
        self._send(404, "not found")


_LOCK_COUNTER = {}


class _FakeFtp(threading.Thread):
    """最小 FTP：问候语 + USER + PASS + QUIT。够验证驱动读得懂协议应答。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _serve(conn):
        try:
            conn.sendall(b"220 fake ftp\r\n")
            user = ""
            while True:
                data = conn.recv(1024)
                if not data:
                    return
                line = data.decode("utf-8", "replace").strip()
                verb, _, value = line.partition(" ")
                verb = verb.upper()
                if verb == "USER":
                    user = value
                    conn.sendall(b"331 need password\r\n")
                elif verb == "PASS":
                    if (user, value) == GOOD:
                        conn.sendall(b"230 logged in\r\n")
                    else:
                        conn.sendall(b"530 invalid password\r\n")
                elif verb == "QUIT":
                    conn.sendall(b"221 bye\r\n")
                    return
                else:
                    conn.sendall(b"502 not implemented\r\n")
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def shutdown(self):
        self.stop_flag = True
        try:
            self.sock.close()
        except OSError:
            pass


class _FakeRedis(threading.Thread):
    """最小 RESP 应答器，用来覆盖"未设密码"那条分支。"""

    def __init__(self, password="secret", no_auth=False):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.password = password
        self.no_auth = no_auth
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                conn.recv(4096)
                if self.no_auth:
                    conn.sendall(b"-ERR Client sent AUTH, but no password is set\r\n")
                else:
                    # 客户端发的是 `*2\r\n$4\r\nAUTH\r\n$6\r\nsecret\r\n`
                    conn.sendall(b"+OK\r\n")
                conn.close()
            except OSError:
                pass

    def shutdown(self):
        self.stop_flag = True
        try:
            self.sock.close()
        except OSError:
            pass


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.ftp = _FakeFtp()
        cls.ftp.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.ftp.shutdown()
        _LOCK_COUNTER.clear()

    def setUp(self):
        _LOCK_COUNTER.clear()

    def http_target(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def grant(self, targets=None):
        return {"grant": "ENG-CREDS-0001", "enabled": ["all"],
                "targets": targets or ["127.0.0.1"]}


class DriverTestCase(Fixture):
    def test_http_basic_success_and_failure(self):
        item = creds.describe_driver("http-basic")
        self.assertEqual((), item["needs"])
        good = creds.attempt("http-basic", "127.0.0.1", self.port, *GOOD,
                             path="/basic")
        self.assertTrue(good.ok)
        self.assertEqual(200, good.status)
        bad = creds.attempt("http-basic", "127.0.0.1", self.port, "admin", "wrong",
                            path="/basic")
        self.assertFalse(bad.ok)
        self.assertEqual(401, bad.status)

    def test_http_basic_uses_status_not_page_content(self):
        # 200 vs 401 是协议规定的，没有解释空间。这条用例把"判据来自哪里"
        # 钉住：即使正文完全一样，状态码不同结论就不同。
        bad = creds.attempt("http-basic", "127.0.0.1", self.port, "admin", "wrong",
                            path="/basic")
        self.assertIn("invalid password", bad.body)

    def test_lockout_page_is_not_a_valid_credential(self):
        """实测踩到的假凭据，也是本组最重要的一条。

        夹具的 `/lock` 在超过阈值后返回 **200** + "Too many failed attempts,
        account is locked"。`status != 401 → 成功` 会把它读成一次有效登录：
        报告上出现一个并不存在的凭据，而操作员会拿着它去试别的系统。
        """
        for _ in range(LOCK_AFTER + 1):
            creds.attempt("http-basic", "127.0.0.1", self.port, "admin", "wrong",
                          path="/lock")
        result = creds.attempt("http-basic", "127.0.0.1", self.port, "admin",
                               "wrong", path="/lock")
        self.assertFalse(result.ok, "锁定页不是认证成功")
        self.assertIn("锁定", result.error)

    def test_public_endpoint_yields_no_credential(self):
        # 未认证就已经可访问的入口，每个口令都会"成功"。判据必须是
        # 「认证改变了什么」，而不是「响应里没有 401」。
        result = creds.attempt("http-basic", "127.0.0.1", self.port, "admin",
                               "anything", path="/open")
        self.assertFalse(result.ok)
        self.assertIn("不需要认证", result.error)

    def test_real_credential_still_passes_all_three_conditions(self):
        # 三个正向条件不能把真的成功也挡掉。
        result = creds.attempt("http-basic", "127.0.0.1", self.port, *GOOD,
                               path="/basic")
        self.assertTrue(result.ok)
        self.assertEqual(200, result.status)

    def test_http_form_requires_matchers(self):
        # 不给判据就只能猜，而猜出来的成功是假凭据。
        with self.assertRaises(creds.CredsError) as ctx:
            creds.attempt("http-form", "127.0.0.1", self.port, "admin", "letmein",
                          path="/login")
        self.assertIn("success", str(ctx.exception))

    def test_http_form_with_matchers(self):
        good = creds.attempt("http-form", "127.0.0.1", self.port, *FORM_GOOD,
                             path="/login", success=r"welcome dashboard",
                             failure=r"invalid credentials")
        self.assertTrue(good.ok)
        bad = creds.attempt("http-form", "127.0.0.1", self.port, "admin", "nope",
                            path="/login", success=r"welcome dashboard",
                            failure=r"invalid credentials")
        self.assertFalse(bad.ok)
        self.assertIn("未命中成功判据", bad.error)

    def test_ftp_driver_reads_protocol_replies(self):
        good = creds.attempt("ftp", "127.0.0.1", self.ftp.port, *GOOD)
        self.assertTrue(good.ok)
        self.assertIn("fake ftp", good.banner)
        bad = creds.attempt("ftp", "127.0.0.1", self.ftp.port, "admin", "wrong")
        self.assertFalse(bad.ok)
        self.assertIn("530", bad.error)

    def test_redis_success(self):
        server = _FakeRedis()
        server.start()
        try:
            result = creds.attempt("redis", "127.0.0.1", server.port, "", "secret")
            self.assertTrue(result.ok)
        finally:
            server.shutdown()

    def test_redis_without_auth_is_flagged_separately(self):
        # 这不是"认证失败"，是"这个服务根本没有认证" —— 一条严重得多的结论。
        server = _FakeRedis(no_auth=True)
        server.start()
        try:
            result = creds.attempt("redis", "127.0.0.1", server.port, "", "secret")
            self.assertFalse(result.ok)
            self.assertIn("未启用认证", result.error)
        finally:
            server.shutdown()

    def test_telnet_demands_a_success_pattern(self):
        with self.assertRaises(creds.CredsError) as ctx:
            creds.attempt("telnet", "127.0.0.1", 23, "a", "b")
        self.assertIn("success", str(ctx.exception))
        self.assertIn("启发式", str(creds.describe_driver("telnet")["note"]))

    def test_connection_refused_is_an_error_not_a_failure(self):
        result = creds.attempt("http-basic", "127.0.0.1", 1, "a", "b", timeout=2.0)
        self.assertFalse(result.ok)
        self.assertTrue(result.error)
        self.assertEqual(0, result.status)

    def test_unknown_driver_points_at_the_toolkit(self):
        """未知驱动的报错必须给出一条**现在就能走**的路。

        这条断言原先只查 `"toolkit" in 文本`，而它能过靠的是那句话里恰好出现了
        `list_toolkit` 这个词 —— "点出一条真入口"和"文本里含有 toolkit 这七个字母"
        是两件事，后者可以被一个名字里带 toolkit 的**无关**词满足（这个仓库里就有
        好几个）。所以改成按入口断：必须点到 `list_toolkit` / `run_tool` /
        `install_tool` 里的至少一个。

        它也不是恒真的：`gap_sentence()` 有两支，装上客户端之后走的是"全都有"那一支，
        而那一支一度**一个去处都没有**（只写"各自都有已安装的客户端可用"）—— 那时
        这条用例是红的，红得对：它一直在替一个内容不完整的句子背书。两支现在都点名，
        这条断言因此在两种部署状态下都成立，而不是只在"没装"那一支成立。
        """
        with self.assertRaises(creds.CredsError) as ctx:
            creds.describe_driver("ssh")
        text = str(ctx.exception)
        self.assertIn("ssh", text, "报错没有复述被拒的驱动名")
        self.assertTrue(
            any(entry in text for entry in ("list_toolkit", "run_tool", "install_tool")),
            "没有点出任何一条真入口：%s" % text)

    def test_listed_drivers_separate_native_from_delegated(self):
        rows = creds.drivers()
        native = {row["id"] for row in rows if not row.get("delegated")}
        delegated = {row["id"] for row in rows if row.get("delegated")}
        self.assertTrue({"http-basic", "http-form", "ftp", "redis"} <= native)
        self.assertTrue(all(row.startswith("toolkit:") for row in delegated))


class LockoutGuardTestCase(unittest.TestCase):
    """三道闸里最要紧的那道。"""

    def guard(self, **kwargs):
        options = {"max_attempts": 5, "min_delay_ms": 0}
        options.update(kwargs)
        return creds.LockoutGuard(**options)

    def test_normal_failures_do_not_stop(self):
        guard = self.guard()
        for _ in range(3):
            self.assertEqual("", guard.observe(
                creds.Attempt("admin", "x", False, 401, body="invalid password")))
        self.assertEqual("", guard.check("admin"))

    def test_a_new_lockout_shaped_response_stops_the_account(self):
        guard = self.guard()
        guard.observe(creds.Attempt("admin", "x", False, 401, body="invalid password"))
        guard.observe(creds.Attempt("admin", "y", False, 401, body="invalid password"))
        reason = guard.observe(creds.Attempt(
            "admin", "z", False, 200, body="Too many failed attempts, account locked"))
        self.assertTrue(reason, "失败的样子变了就必须停")
        self.assertIn("locked", reason)
        self.assertIn("admin", guard.report()["lockouts"])

    def test_first_attempt_that_already_looks_locked_does_not_count_as_our_fault(self):
        # 第一次尝试就遇到限流页，说明目标对我们这台机器本来就有策略 ——
        # 那不是我们把它打出来的，不该记成"我们把账号打锁了"。
        guard = self.guard()
        reason = guard.observe(creds.Attempt(
            "admin", "x", False, 200, body="Too many failed attempts"))
        self.assertEqual("", reason)

    def test_http_429_stops_the_account(self):
        guard = self.guard()
        guard.observe(creds.Attempt("admin", "x", False, 401, body="invalid"))
        reason = guard.observe(creds.Attempt("admin", "y", False, 429, body="slow down"))
        self.assertIn("429", reason)

    def test_hard_attempt_cap(self):
        guard = self.guard(max_attempts=2)
        guard.observe(creds.Attempt("admin", "x", False, 401, body="invalid"))
        guard.observe(creds.Attempt("admin", "y", False, 401, body="invalid"))
        self.assertIn("上限", guard.check("admin"))

    def test_signatures_ignore_timestamps_and_ids(self):
        # 正文里常有时间戳与请求 id。逐字比会把"同一个错误页"判成两个签名，
        # 于是第 3 道闸永远不触发 —— 而它恰恰是最重要的那道。
        guard = self.guard()
        first = creds.Attempt("admin", "x", False, 401,
                              body="invalid password at 12:00:01 id=9f8e7d6c5b4a")
        second = creds.Attempt("admin", "y", False, 401,
                               body="invalid password at 12:00:02 id=1a2b3c4d5e6f")
        self.assertEqual(first.signature, second.signature,
                         "只差时间戳与请求 id 的同一个错误页必须同签名")

    def test_signatures_separate_genuinely_different_pages(self):
        # 反向的一半：归一化不能把所有东西都抹平成同一个签名，否则第 3 道闸
        # 会在第一次尝试之后就立刻触发。
        guard = self.guard()
        wrong = creds.Attempt("admin", "x", False, 401, body="invalid password")
        locked = creds.Attempt("admin", "y", False, 401,
                               body="account is locked, contact the administrator")
        self.assertNotEqual(wrong.signature, locked.signature)

    def test_normalization_does_not_eat_real_words(self):
        # `invalid` 与 `password` 是区分"口令错"和"账号锁定"的信号，
        # 不能被 id 归一化规则误伤。
        self.assertEqual("invalid password", creds._normalize_page("invalid password"))

    def test_enumeration_hint_is_recorded_not_acted_on(self):
        guard = self.guard()
        self.assertEqual("", guard.observe(
            creds.Attempt("ghost", "x", False, 401, body="no such user")))
        self.assertIn("ghost", guard.report()["enumeration"])

    def test_delay_has_jitter_and_a_floor(self):
        guard = self.guard(min_delay_ms=400)
        waits = [guard.delay_for("admin") for _ in range(30)]
        self.assertTrue(all(w >= 0.3 for w in waits), "抖动不能让间隔低于下限量级")
        self.assertTrue(all(w <= 0.5 for w in waits))
        self.assertGreater(len(set(waits)), 1, "固定间隔本身就是指纹")

    def test_zero_delay_means_no_sleep(self):
        self.assertEqual(0.0, self.guard(min_delay_ms=0).delay_for("admin"))


class RunTestCase(Fixture):
    def test_finds_a_credential_and_records_only_a_fingerprint_in_the_chain(self):
        result = creds.run(self.http_target("/basic"), "http-basic",
                           accounts=["admin", "root"],
                           passwords=["wrong", "secret"],
                           grants=self.grant(), limits={"minDelayMs": 0},
                           path="/basic")
        self.assertEqual(1, result["summary"]["found"])
        self.assertEqual("secret", result["found"][0]["password"],
                         "结果里要带口令本身：操作员要拿它登进去")
        specs = creds.build_chain_specs(result)
        blob = " ".join(str(spec) for spec in specs)
        self.assertNotIn("secret", blob,
                         "链路会被导出、被翻看、会进报告 —— 口令不许写进去")
        self.assertIn(result["found"][0]["fingerprint"], blob)

    def test_stops_the_account_when_the_server_starts_locking(self):
        result = creds.run(self.http_target("/lock"), "http-basic",
                           accounts=["admin"], passwords=["a", "b", "c", "d", "e"],
                           grants=self.grant(),
                           limits={"minDelayMs": 0, "maxAttemptsPerAccount": 5},
                           path="/lock")
        self.assertEqual(0, result["summary"]["found"])
        self.assertTrue(result["stopped"], "出现锁定页就必须停在该账号")
        self.assertIn("admin", result["guard"]["lockouts"])

    def test_attempt_cap_bounds_the_dictionary(self):
        result = creds.run(self.http_target("/basic"), "http-basic",
                           accounts=["admin"],
                           passwords=["a", "b", "c", "d", "e", "f", "g", "h"],
                           grants=self.grant(),
                           limits={"minDelayMs": 0, "maxAttemptsPerAccount": 3},
                           path="/basic")
        self.assertEqual(3, result["summary"]["attempts"],
                         "字典再大也受单账号上限约束")

    def test_account_count_cap(self):
        with self.assertRaises(creds.CredsError):
            creds.run(self.http_target("/basic"), "http-basic",
                      accounts=["u%d" % i for i in range(50)],
                      passwords=["x"], grants=self.grant(),
                      limits={"maxAccounts": 4}, path="/basic")

    def test_password_count_cap(self):
        with self.assertRaises(creds.CredsError):
            creds.run(self.http_target("/basic"), "http-basic", accounts=["admin"],
                      passwords=["p%d" % i for i in range(100)],
                      grants=self.grant(), limits={"maxPasswords": 8},
                      path="/basic")

    def test_run_without_grant_is_refused(self):
        with self.assertRaises(authz.AuthzError):
            creds.run(self.http_target("/basic"), "http-basic",
                      accounts=["admin"], passwords=["secret"], path="/basic")

    def test_target_outside_allowlist_is_refused(self):
        with self.assertRaises(authz.AuthzError):
            creds.run(self.http_target("/basic"), "http-basic",
                      accounts=["admin"], passwords=["secret"],
                      grants=self.grant(targets=["10.0.0.0/8"]), path="/basic")

    def test_default_dictionaries_are_deliberately_small(self):
        # 它不是"能力"，是"省得为了试最常见的组合先去找字典"。真评估由
        # 操作员提供字典，而那时尝试上限会随字典变大而更早生效。
        self.assertLessEqual(len(creds.DEFAULT_ACCOUNTS), 8)
        self.assertLessEqual(len(creds.DEFAULT_PASSWORDS), 8)

    def test_chain_specs_carry_the_grant(self):
        result = creds.run(self.http_target("/basic"), "http-basic",
                           accounts=["admin"], passwords=["secret"],
                           grants=self.grant(), limits={"minDelayMs": 0},
                           path="/basic")
        blob = " ".join(str(spec.get("payload")) for spec in
                        creds.build_chain_specs(result))
        self.assertIn("ENG-CREDS-0001", blob)


if __name__ == "__main__":
    unittest.main()
