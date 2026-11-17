# -*- coding: utf-8 -*-
"""keep-alive 上的请求体没读干净 → 连接错位 → 「刷新就退出」。

这一组守的是一条**协议层**的不变量，而不是某个功能：HTTP/1.1 默认复用同一条
TCP 连接，连接上的字节是一条连续的流。"读走一条请求的 body"是**把读指针推到
下一条请求开头**的唯一动作；没读它，下一条请求就从 body 中间开始解析。

线上访问日志里的原始证据（本组用例把它做成了可重复的断言）：

    2026-10-15 02:27:20 ERROR code 501, message Unsupported method ('{}GET')
    （另有 8 条 Bad request syntax ('{}')，跨 10-13 与 10-15）

`{}GET` 这个"方法名"就是 `{}` 粘在下一个请求行前面拼出来的——它是连接错位的
签名，没有别的成因。

后果比那一条 400 大得多：**错位之后响应与请求对不上号**。浏览器按顺序把响应
配给请求，而错位会多出一条响应，把后面全部错开一格——于是
`GET /api/v1/auth/state` 可能收到别人的响应，或者一个 `Set-Cookie`
（退出登录那条带 `Max-Age=0`）挂到了别的请求上。表现出来正是"刷新一下就退出登录"。
"""
import contextlib
import http.client
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tests import support                                        # noqa: E402


class KeepAliveBodyTestCase(unittest.TestCase):
    """一条连接上连发多条请求：body 没读干净时**绝不能**留下错位的连接。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("keepalive.db")
        self.httpd, self.client = support.start(self.store)
        self.port = self.httpd.server_address[1]
        self.log_path = os.path.join(os.path.dirname(self.store.path), "access.log")

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    # ------------------------------------------------------------ 工具

    def _connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def _json(self, conn, method: str, path: str, body=None) -> tuple:
        payload = json.dumps(body if body is not None else {})
        conn.request(method, path, body=payload if method == "POST" else None,
                     headers={"Content-Type": "application/json",
                              "Accept": "application/json"})
        response = conn.getresponse()
        raw = response.read().decode("utf-8", "replace")
        return response.status, raw

    def _log(self) -> str:
        if not os.path.exists(self.log_path):
            return ""
        with open(self.log_path, encoding="utf-8", errors="replace") as handle:
            return handle.read()

    # ------------------------------------------------------------ 用例

    def test_a_404_post_does_not_desync_the_connection(self):
        """**本组的核心用例。** POST 到未知路由之后，同一条连接上的下一条请求要正常。

        修之前这条会红，而且红的样子与线上日志逐字一致：
        `501 Unsupported method ('{}GET')`——正常请求被答成了垃圾。
        """
        conn = self._connection()
        try:
            status, _body = self._json(conn, "POST", "/api/v1/definitely-not-a-route")
            self.assertEqual(404, status, "这条用例要的是路由没匹配上的那一支")
            status, raw = self._json(conn, "GET", "/api/v1/auth/state")
            self.assertEqual(200, status, "同一条连接上的下一条请求被答成了 %s：%s"
                                          % (status, raw[:120]))
            self.assertFalse(json.loads(raw)["needsSetup"], "响应体不是 auth/state 的")
        finally:
            conn.close()
        log = self._log()
        self.assertNotIn("Unsupported method", log, "连接错位又出现了")
        self.assertNotIn("Bad request syntax", log, "请求体没被读走")

    def test_the_broken_shape_is_what_the_live_log_records(self):
        """把线上那条日志的形状钉住：`{}GET` **只能**来自 `{}` 粘在 GET 前面。

        这条断言的价值在于"下一个看见它的人能立刻想起原因"——没有它，
        `Unsupported method ('{}GET')` 看起来像某个客户端的行为，而它是我们自己
        没读请求体造成的。
        """
        conn = self._connection()
        try:
            self._json(conn, "POST", "/api/v1/nope", body={})
            conn.request("GET", "/api/v1/health", headers={"Accept": "application/json"})
            response = conn.getresponse()
            self.assertEqual(200, response.status)
        finally:
            conn.close()
        self.assertNotIn("'{}GET'", self._log())

    def test_a_chunked_body_closes_the_instead_of_desyncing(self):
        """分块编码定位不了边界 → **关连接**，而不是硬着头皮复用。

        关连接的代价是多一次握手；留下一条错位的连接，坏的是后面每一条请求。
        """
        conn = self._connection()
        try:
            conn.putrequest("POST", "/api/v1/nope")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.putheader("Content-Type", "application/json")
            conn.endheaders()
            conn.send(b"2\r\n{}\r\n0\r\n\r\n")
            response = conn.getresponse()
            self.assertEqual(404, response.status)
            self.assertEqual("close", (response.getheader("Connection") or "").lower(),
                             "边界未知时必须如实告诉客户端别复用这条连接")
            response.read()
        finally:
            conn.close()
        self.assertIn("DESYNC", self._log())

    def test_an_oversize_body_is_refused_and_the_connection_is_closed(self):
        """`Content-Length` 超过上限时不读它 —— 那就**必须**关连接。"""
        conn = self._connection()
        try:
            conn.putrequest("POST", "/api/v1/auth/login")
            conn.putheader("Content-Length", str(9 * 1024 * 1024))
            conn.putheader("Content-Type", "application/json")
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(413, response.status)
            self.assertEqual("close", (response.getheader("Connection") or "").lower())
            response.read()
        finally:
            conn.close()
        self.assertIn("DESYNC", self._log())

    def test_a_get_carrying_a_body_keeps_the_connection_clean(self):
        """GET 带 body 是病态输入，但**不能因此把连接搞坏**。

        处理它是"替客户端把多余的两字节收掉"——收干净就照常复用，
        这一点与"收不干净就关掉"是同一条规则的两支。
        """
        conn = self._connection()
        try:
            conn.request("GET", "/api/v1/health", body="xx",
                         headers={"Content-Type": "application/json",
                                  "Accept": "application/json"})
            first = conn.getresponse()
            self.assertEqual(200, first.status)
            first.read()
            status, raw = self._json(conn, "GET", "/api/v1/health")
            self.assertEqual(200, status, raw[:120])
        finally:
            conn.close()
        self.assertNotIn("Bad request syntax", self._log())

    def test_a_normal_post_keeps_the_connection_alive(self):
        """正常请求**不该**被这一改动顺手关掉连接——那会把性能改坏而没人发现。"""
        conn = self._connection()
        try:
            status, _raw = self._json(conn, "POST", "/api/v1/auth/login",
                                      {"username": support.ADMIN_USER,
                                       "password": support.ADMIN_PASSWORD})
            self.assertEqual(200, status)
            self.assertNotEqual("close", ("" or "").lower())
            status, raw = self._json(conn, "GET", "/api/v1/health")
            self.assertEqual(200, status, raw[:120])
        finally:
            conn.close()
        self.assertNotIn("DESYNC", self._log())


class SessionDropIsLoggedTestCase(unittest.TestCase):
    """会话被作废时要留痕。**没有这条日志，"刷新就退出"无法归因。**"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("drop.db")
        self.httpd, self.client = support.start(self.store)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.log_path = os.path.join(os.path.dirname(self.store.path), "access.log")

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def _log(self) -> str:
        if not os.path.exists(self.log_path):
            return ""
        with open(self.log_path, encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def test_a_session_pointing_at_a_missing_user_is_logged_with_the_reason(self):
        """账号被删掉之后，旧 Cookie 一被使用就会作废——**必须写明是这一支**。

        三种作废原因（超时 / 账号没了 / 管理员操作）对应的下一步动作完全不同，
        只记"会话失效"等于没记。
        """
        status, payload = self.client.post("/api/v1/users",
                                           {"username": "temprole", "role": "operator",
                                            "password": "Temp-Pass-123!"})
        self.assertEqual(201, status, payload)
        victim_id = payload["user"]["id"]

        other = support.Client(self.base)
        status, payload = other.login("temprole", "Temp-Pass-123!")
        self.assertEqual(200, status, payload)
        self.assertTrue(other.get("/api/v1/auth/state")[1]["authenticated"])

        # **直接删 users 那一行，不走 `delete_user`。** `delete_user` 会顺手
        # 把该用户的会话一起清掉（`DELETE FROM sessions WHERE user_id=?`），
        # 于是根本走不到"会话还在、账号没了"这一支——而那一支恰恰是**数据库被
        # 替换/恢复**之后的样子：users 表换了一份，sessions 里留着指向旧 id 的行。
        # 这正是"刷新就退出"最需要被认出来的成因之一，所以单独钉住它。
        self.store.conn.execute("DELETE FROM users WHERE id=?", (victim_id,))
        self.store.conn.commit()
        self.assertIsNone(self.store.get_user(victim_id))
        rows = self.store.conn.execute("SELECT COUNT(*) FROM sessions"
                                       " WHERE user_id=?", (victim_id,)).fetchone()[0]
        self.assertEqual(1, rows, "这条用例的前提是会话行还在")

        status, payload = other.get("/api/v1/auth/state")
        self.assertEqual(200, status)
        self.assertFalse(payload["authenticated"], "账号没了，会话就该作废")

        log = self._log()
        self.assertIn("AUTH 会话作废", log)
        self.assertIn("账号不存在或已停用", log)

    def test_an_unknown_cookie_is_not_logged(self):
        """没带 Cookie / Cookie 查不到**不记**——那是每次未登录访问都会发生的事，
        记下来只会把真正的那几行淹掉。"""
        support.Client(self.base).get("/api/v1/auth/state")
        self.assertNotIn("AUTH 会话作废", self._log())

    def test_an_admin_action_that_kicks_everyone_is_logged_with_who_and_why(self):
        """"一个动作把某人所有登录端一起踢掉"必须留痕，且要写明原因的措辞。

        它和"这条会话自己到期"的下一步动作完全不同：前者要去查**谁**做了那个
        动作，后者是等一会儿或者找管理员。没有日志就只能靠猜——而这一条正是
        "刷新一下就退出登录"最需要被排除的可能之一。
        """
        status, payload = self.client.post("/api/v1/users",
                                           {"username": "kickme", "role": "operator",
                                            "password": "Kick-Me-12345!"})
        self.assertEqual(201, status, payload)
        victim_id = payload["user"]["id"]
        other = support.Client(self.base)
        self.assertEqual(200, other.login("kickme", "Kick-Me-12345!")[0])

        status, payload = self.client.post(
            "/api/v1/users/%s/password" % victim_id, {"password": "New-Pass-12345!"})
        self.assertEqual(200, status, payload)

        log = self._log()
        self.assertIn("该账号全部会话作废", log)
        self.assertIn("管理员重置了该账号的口令", log)
        self.assertIn(victim_id, log)
        self.assertFalse(other.get("/api/v1/auth/state")[1]["authenticated"])


class ConnectionNoiseTestCase(unittest.TestCase):
    """客户端断开**不该**打错误栈。

    默认的 `socketserver.handle_error` 是 `traceback.print_exc()`，而关标签页、
    页面跳转、健康检查超时都会走到那里。一屏常态的栈会做两件坏事：把真故障
    淹掉，以及让运维以为服务出了问题。所以断开类异常只写一行访问日志，
    **其它异常照旧打完整栈**——那一条才可能是真故障。
    """

    def setUp(self):
        self.directory, self.store = support.temporary_store("noise.db")
        self.httpd, _client = support.start(self.store)
        self.log_path = os.path.join(os.path.dirname(self.store.path), "access.log")

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def test_a_dropped_connection_writes_one_line_instead_of_a_traceback(self):
        try:
            raise ConnectionResetError("客户端走开了")
        except ConnectionResetError:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.httpd.handle_error(None, ("127.0.0.1", 5555))
        self.assertEqual("", stderr.getvalue(), "断开是常态，不该打栈")
        with open(self.log_path, encoding="utf-8", errors="replace") as handle:
            self.assertIn("连接已断开", handle.read())

    def test_a_real_error_still_prints_its_traceback(self):
        """反面：真故障不能被这一改动顺手吞掉。"""
        try:
            raise RuntimeError("这是真故障")
        except RuntimeError:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.httpd.handle_error(None, ("127.0.0.1", 5555))
        self.assertIn("这是真故障", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
