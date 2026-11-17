# -*- coding: utf-8 -*-
"""出口路由（pivot）的验证。

三条线各测一层，与 `test_probe.py` 同一个路子：

* **白名单**是纯函数，逐条钉住边界语义（空 = 拒绝、域名先解析再判、解析失败
  fail-closed、每个解析结果都要命中）；
* **代理与隧道**对着真实监听套接字跑，不用 mock 顶替 socket——mock 只能证明
  "我发了这段字节"，证明不了"echo 服务真的收到了"、"半关闭之后另一半真的还在
  流动"、"统计出来的字节数真是转发出去的字节数"；
* **协议细节**（认证子协商、垃圾字节、应答码）全部手工拼真实字节走一遍，
  因为这些正是 mock 最容易盖住的问题所在。

半关闭那条用例是本文件的重点：它是唯一一条"看起来通过、实际丢了后半段"的
缺陷，用 mock 永远测不出来。
"""
import os
import socket
import struct
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import pivot                                        # noqa: E402


# ---------------------------------------------------------------- 测试夹具

class EchoService:
    """最小 TCP echo 服务：收到多少原样回多少。

    刻意用**真实**监听套接字而不是 mock：转发链路上任何一处"看起来转发了"的
    错误，只有在另一头真的收到字节时才会暴露。
    """

    def __init__(self, reply=None):
        self.reply = reply                     # 可选：忽略输入，只回固定内容
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._threads = []
        self.accepted = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._serve, name="echo-serve", daemon=True).start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            with self._lock:
                self.accepted += 1
            thread = threading.Thread(target=self._handle, args=(conn,),
                                      name="echo-conn", daemon=True)
            self._threads.append(thread)
            thread.start()

    def _handle(self, conn):
        try:
            if self.reply is not None:
                # 固定回复模式：读完客户端发来的第一段就回固定内容，然后关写。
                # 用于"服务端先说话"的场景。
                conn.recv(65536)
                conn.sendall(self.reply)
                conn.shutdown(socket.SHUT_WR)
                return
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                conn.sendall(chunk)
            # 客户端关了写，echo 也关写：让对端读到 EOF。这一步必须做——
            # 否则半关闭用例会停在"等一个永远不来的 EOF"，而那是测试自身的缺陷。
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


class ProxyCase(unittest.TestCase):
    """起了 echo 服务与代理的公共基类，负责收摊（端口与线程都不许残留）。"""

    proxy_allow = ["127.0.0.0/8", "::1/128"]

    def setUp(self):
        self.echo = EchoService()
        self.proxies = []

    def tearDown(self):
        for proxy in self.proxies:
            proxy.stop()
        self.echo.stop()

    def new_proxy(self, allow=None, **kwargs):
        proxy = pivot.Socks5Server(
            allow=self.proxy_allow if allow is None else allow, **kwargs)
        self.proxies.append(proxy)
        proxy.start()
        return proxy

    def assert_refused(self, code, host="127.0.0.1", port=None):
        """手工握手到"代理该拒绝"这一步。

        不用 `socks5_connect`：那个函数把失败包装成 `PivotError`，而这里要断言的
        正是**线路上真实出现的应答码**。错误信息与真实字节都要看到，才谈得上
        "测了协议"。
        """
        conn = socket.create_connection(("127.0.0.1", self.proxy_port), 5)
        conn.settimeout(5)
        try:
            conn.sendall(b"\x05\x01\x00")
            method = self._read(conn, 2)
            self.assertEqual(method, b"\x05\x00",
                             "无认证模式下代理应选 0x00，实际 %r" % (method,))
            request = (b"\x05\x01\x00" + self.atyp(host)
                       + struct.pack("!H", self.echo.port if port is None else port))
            conn.sendall(request)
            reply = self._read(conn, 4)
            self.assertEqual(reply[0], 0x05, "应答版本号应为 5，实际 %r" % (reply[0],))
            self.assertEqual(reply[1], code,
                             "应答码应为 0x%02x，实际 0x%02x" % (code, reply[1]))
            return reply
        finally:
            conn.close()

    def atyp(self, host):
        if host.replace(".", "").isdigit():
            return b"\x01" + socket.inet_aton(host)
        if ":" in host:
            return b"\x04" + socket.inet_pton(socket.AF_INET6, host)
        raw = host.encode("idna")
        return b"\x03" + bytes([len(raw)]) + raw

    @staticmethod
    def _read(conn, count):
        data = b""
        while len(data) < count:
            chunk = conn.recv(count - len(data))
            if not chunk:
                raise AssertionError("连接在读满 %d 字节前被关闭（已读 %r）"
                                     % (count, data))
            data += chunk
        return data

    @property
    def proxy_port(self):
        return self._proxy.port


# ---------------------------------------------------------------- 端口与生命周期

class LifecycleTest(ProxyCase):
    """`start()` 的端口必须是系统分配的真实端口；`stop()` 之后必须真的关掉。"""

    def test_start_returns_real_port_and_stop_closes_it(self):
        proxy = self.new_proxy()
        self._proxy = proxy
        endpoint = proxy.endpoint
        port = endpoint["port"]
        self.assertNotEqual(port, 0, "start() 必须回真实端口，不能是请求值 0")
        self.assertEqual(port, proxy.port)
        # 端口真的在听：建得上连接。
        probe = socket.create_connection(("127.0.0.1", port), 5)
        probe.close()

        proxy.stop()
        deadline = time.time() + 3
        refused = False
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), 1).close()
            except OSError:
                refused = True
                break
            time.sleep(0.05)
        self.assertTrue(refused, "stop() 之后端口 %d 仍可连接" % port)

    def test_restart_same_instance(self):
        """同一实例反复启停：每次都真的能干活，收尾干净。"""
        proxy = self.new_proxy()
        self._proxy = proxy
        for round_index in range(3):
            port = proxy.endpoint["port"]
            self.assertNotEqual(port, 0, "start() 必须回真实端口")
            sock = pivot.socks5_connect("127.0.0.1", port, "127.0.0.1",
                                        self.echo.port)
            payload = ("往返-%d" % round_index).encode("utf-8")
            sock.sendall(payload)
            self.assertEqual(sock.recv(64), payload,
                             "第 %d 轮 start/stop 后数据没能往返" % (round_index + 1))
            sock.close()
            proxy.stop()
            self.assertTrue(proxy.wait_idle(3.0),
                            "stop() 后仍有一条在途连接没结束")
            # 停掉之后这个端口必须连不上（端口复用由内核决定，因此这里断言的是
            # "监听套接字真的关掉了"，而不是"端口号一定被换了"）。
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    socket.create_connection(("127.0.0.1", port), 1).close()
                except OSError:
                    break
                time.sleep(0.05)
            else:
                self.fail("第 %d 轮 stop() 之后端口 %d 仍可连接"
                          % (round_index + 1, port))
            proxy.start()
        self.assertTrue(proxy.wait_idle(3.0), "收尾后还有残留线程")


# ---------------------------------------------------------------- 数据面

class ProxyDataTest(ProxyCase):
    def test_round_trip_through_proxy(self):
        """经代理访问真实 echo：发什么必须收到什么。"""
        proxy = self.new_proxy()
        self._proxy = proxy
        sock = pivot.socks5_connect("127.0.0.1", proxy.port, "127.0.0.1",
                                    self.echo.port)
        try:
            payload = b"hello-pivot-" + os.urandom(16)
            sock.sendall(payload)
            self.assertEqual(sock.recv(len(payload)), payload)
        finally:
            sock.close()

    def test_ipv6_target(self):
        """`ATYP=0x04` 目标必须真的能连上，而不是"地址类型不支持"。"""
        try:
            server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("::1", 0))
        except OSError as exc:
            self.skipTest("本机不支持 IPv6 回环：%s" % exc)
        server.listen(4)
        port = server.getsockname()[1]
        stop = threading.Event()

        def serve():
            while not stop.is_set():
                try:
                    conn, _addr = server.accept()
                except OSError:
                    return
                try:
                    data = conn.recv(64)
                    conn.sendall(b"v6:" + data)
                except OSError:
                    pass
                finally:
                    conn.close()

        threading.Thread(target=serve, name="echo-v6", daemon=True).start()
        proxy = self.new_proxy()
        self._proxy = proxy
        try:
            sock = pivot.socks5_connect("127.0.0.1", proxy.port, "::1", port)
            sock.sendall(b"ping6")
            self.assertEqual(sock.recv(64), b"v6:ping6",
                             "IPv6 目标经代理没有拿到回包")
            sock.close()
        finally:
            stop.set()
            server.close()

    def test_tunnel_forwards_and_closes(self):
        tunnel = pivot.Tunnel("127.0.0.1", 0, "127.0.0.1", self.echo.port,
                              allow=["127.0.0.1/32"])
        tunnel.start()
        try:
            endpoint = tunnel.endpoint
            self.assertNotEqual(endpoint["listenPort"], 0,
                                "隧道 start() 必须回真实监听端口")
            sock = socket.create_connection(("127.0.0.1", endpoint["listenPort"]), 5)
            sock.settimeout(5)
            sock.sendall(b"through-tunnel")
            self.assertEqual(sock.recv(64), b"through-tunnel")
            sock.close()
            port = endpoint["listenPort"]
        finally:
            tunnel.stop()
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), 1).close()
            except OSError:
                break
            time.sleep(0.05)
        else:
            self.fail("隧道 stop() 之后端口 %d 仍可连接" % port)

    def test_tunnel_refuses_out_of_allow(self):
        """固定目标也要过白名单：放行别的网段，本机目标就不许立隧道。

        门在 `start()` 而不是构造函数：与 `Socks5Server` 一致的做法是"登记一个
        出口"不产生副作用，只有真正把它立起来那一刻才判能不能立。
        """
        tunnel = pivot.Tunnel("127.0.0.1", 0, "127.0.0.1", self.echo.port,
                              allow=["10.0.0.0/8"])
        with self.assertRaises(pivot.PivotError) as ctx:
            tunnel.start()
        self.assertIn("白名单", str(ctx.exception))
        self.assertEqual(tunnel.endpoint["listenPort"], 0,
                         "启动被拒时不该留下一个监听端口")

    def test_tunnel_refuses_empty_allow(self):
        """空白名单 = fail-closed：隧道根本不该立起来，而不是立起来等连接被拒。"""
        tunnel = pivot.Tunnel("127.0.0.1", 0, "127.0.0.1", self.echo.port, allow=[])
        with self.assertRaises(pivot.PivotError) as ctx:
            tunnel.start()
        self.assertIn("白名单为空", str(ctx.exception))
        self.assertEqual(tunnel.endpoint["listenPort"], 0,
                         "启动被拒时不该留下一个监听端口")


# ---------------------------------------------------------------- 白名单

class AllowListTest(ProxyCase):
    def test_empty_allow_denies_and_counts(self):
        """空白名单 = 拒绝一切，且每一条被拒的连接都计进 denied。"""
        proxy = self.new_proxy(allow=[])
        self._proxy = proxy
        self.assertEqual(proxy.endpoint["allow"], [], "空白名单不该被填成默认值")
        self.assert_refused(pivot.REP_NOT_ALLOWED)
        with self.assertRaises(pivot.PivotError) as ctx:
            pivot.socks5_connect("127.0.0.1", proxy.port, "127.0.0.1", self.echo.port)
        message = str(ctx.exception)
        self.assertIn("白名单", message, "拒绝原因里必须点明白名单：%s" % message)
        self.assertIn("0x02", message, "拒绝原因里应带应答码：%s" % message)
        self.assertTrue(proxy.wait_idle(3.0))
        self.assertEqual(proxy.stats["denied"], 2,
                         "两次被拒就该计两次（手工握手一次 + 客户端一次），"
                         "实际 %r" % (proxy.stats,))
        self.assertEqual(proxy.stats["connections"], 0,
                         "被拒的连接不该计进 connections")

    def test_local_allow_permits_and_other_netblock_denies(self):
        permitted = self.new_proxy(allow=["127.0.0.1/32"])
        self._proxy = permitted
        sock = pivot.socks5_connect("127.0.0.1", permitted.port, "127.0.0.1",
                                    self.echo.port)
        sock.sendall(b"allowed")
        self.assertEqual(sock.recv(64), b"allowed")
        sock.close()

        blocked = self.new_proxy(allow=["10.0.0.0/8"])
        self._proxy = blocked
        self.assert_refused(pivot.REP_NOT_ALLOWED)
        self.assertEqual(blocked.stats["denied"], 1)

    def test_domain_cannot_bypass_allowlist(self):
        """域名绕过：放行 10.0.0.0/8，用 ATYP=domain 请求 127.0.0.1 必须被拒。

        这是整个模块里最容易漏的一条。只比字符串的白名单会被 `localhost`
        绕开——白名单说着"只能碰 10 网段"，实际却放客户端碰了本机。所以域名
        必须**在代理侧解析后**再判，判的必须是解析出来的地址。
        """
        proxy = self.new_proxy(allow=["10.0.0.0/8"])
        self._proxy = proxy
        reply = self.assert_refused(pivot.REP_NOT_ALLOWED, host="localhost")
        self.assertEqual(reply[1], pivot.REP_NOT_ALLOWED)
        with self.assertRaises(pivot.PivotError) as ctx:
            pivot.socks5_connect("127.0.0.1", proxy.port, "localhost",
                                 self.echo.port)
        self.assertIn("白名单", str(ctx.exception))
        self.assertEqual(proxy.stats["denied"], 2,
                         "两条被拒的域名请求都该计数，实际 %r" % (proxy.stats,))
        self.assertEqual(proxy.stats["connections"], 0,
                         "域名绕过的请求绝不能被算作成功连接")

    def test_domain_allowed_when_resolved_address_is_in_allow(self):
        """反向也要成立：解析结果真在名单内时，域名必须能过。

        注意 `localhost` 在双栈机器上会同时解析出 `127.0.0.1` 与 `::1`，而
        "每个解析结果都要命中"这条规则要求两者都被放行——这正是它该有的行为，
        所以这里的白名单必须同时含 v4 与 v6 回环。
        """
        proxy = self.new_proxy(allow=["127.0.0.0/8", "::1/128"])
        self._proxy = proxy
        sock = pivot.socks5_connect("127.0.0.1", proxy.port, "localhost",
                                    self.echo.port)
        sock.sendall(b"by-name")
        self.assertEqual(sock.recv(64), b"by-name")
        sock.close()

    def test_domain_denied_when_one_resolved_address_is_outside(self):
        """解析出多个地址、只有一个越界 → 整体拒绝。

        这是"只要求第一个地址命中"会留下的洞：连出去的是哪一个由解析顺序决定，
        不由我们决定，所以判定必须取"全部命中"。
        """
        allowed, _reason = pivot._resolve_addresses("localhost")
        if not allowed or len(allowed) < 2:
            self.skipTest("本机 localhost 只解析出 %r，无法构造多地址场景" % (allowed,))
        v4 = [item for item in allowed if ":" not in item]
        v6 = [item for item in allowed if ":" in item]
        if not v4 or not v6:
            self.skipTest("本机 localhost 未同时解析出 v4 与 v6：%r" % (allowed,))
        proxy = self.new_proxy(allow=[v4[0] + "/32"])
        self._proxy = proxy
        self.assert_refused(pivot.REP_NOT_ALLOWED, host="localhost")
        self.assertEqual(proxy.stats["denied"], 1,
                         "有一条解析结果越界时，整条请求都应被拒")

    def test_parse_allow_normalizes(self):
        self.assertEqual(pivot.parse_allow(["10.0.0.0/8"]), ["10.0.0.0/8"])
        self.assertEqual(pivot.parse_allow(["127.0.0.1"]), ["127.0.0.1/32"])
        self.assertEqual(pivot.parse_allow(["::1"]), ["::1/128"])
        self.assertEqual(pivot.parse_allow([]), [])
        self.assertEqual(pivot.parse_allow(None), [])
        self.assertEqual(pivot.parse_allow(["", "  "]), [], "空项不该造出放行段")
        self.assertEqual(pivot.parse_allow(["10.0.0.0/8", "10.0.0.0/8"]),
                         ["10.0.0.0/8"], "重复项应去掉")
        self.assertEqual(pivot.parse_allow(["any"]),
                         ["0.0.0.0/0", "::/0"], "any 应展开成显式的全覆盖集合")
        with self.assertRaises(pivot.PivotError) as ctx:
            pivot.parse_allow(["example.com"])
        message = str(ctx.exception)
        self.assertIn("合法", message, "单地址项的报错要说明是什么不合法：%s" % message)
        with self.assertRaises(pivot.PivotError):
            pivot.parse_allow(["10.0.0.0/33"])

    def test_in_allow_fails_closed(self):
        self.assertFalse(pivot.in_allow("127.0.0.1", []), "空名单必须拒绝")
        self.assertFalse(pivot.in_allow("127.0.0.1", ["10.0.0.0/8"]))
        self.assertTrue(pivot.in_allow("127.0.0.1", ["127.0.0.0/8"]))
        self.assertTrue(pivot.in_allow("::1", ["::1/128"]))
        self.assertFalse(pivot.in_allow("::1", ["127.0.0.0/8"]),
                         "v4 网段不该匹配 v6 地址")
        self.assertFalse(pivot.in_allow("no-such-host.invalid", ["0.0.0.0/0"]),
                         "域名解析失败必须 fail-closed，哪怕名单是全开")
        self.assertFalse(pivot.in_allow("", ["0.0.0.0/0"]))


# ---------------------------------------------------------------- 认证

class AuthTest(ProxyCase):
    def test_correct_credentials_succeed(self):
        proxy = self.new_proxy(username="operator", password="s3cret-pass")
        self._proxy = proxy
        self.assertTrue(proxy.endpoint["auth"])
        sock = pivot.socks5_connect("127.0.0.1", proxy.port, "127.0.0.1",
                                    self.echo.port, username="operator",
                                    password="s3cret-pass")
        sock.sendall(b"auth-ok")
        self.assertEqual(sock.recv(64), b"auth-ok")
        sock.close()
        self.assertTrue(proxy.wait_idle(3.0))
        self.assertEqual(proxy.stats["connections"], 1)

    def test_wrong_password_rejected(self):
        proxy = self.new_proxy(username="operator", password="s3cret-pass")
        self._proxy = proxy
        with self.assertRaises(pivot.PivotError) as ctx:
            pivot.socks5_connect("127.0.0.1", proxy.port, "127.0.0.1",
                                 self.echo.port, username="operator",
                                 password="wrong-pass")
        self.assertIn("认证失败", str(ctx.exception),
                      "错误信息要指出是认证这一步：%s" % ctx.exception)
        self.assertEqual(proxy.stats["connections"], 0)
        self.assertEqual(proxy.stats["denied"], 0,
                         "认证失败不是白名单拒绝，不该混进 denied")

    def test_client_without_auth_rejected_with_ff(self):
        """服务端要认证、客户端只报 0x00 → 必须回 0xFF 拒绝。"""
        proxy = self.new_proxy(username="operator", password="s3cret-pass")
        self._proxy = proxy
        conn = socket.create_connection(("127.0.0.1", proxy.port), 5)
        conn.settimeout(5)
        try:
            conn.sendall(b"\x05\x01\x00")
            reply = self._read(conn, 2)
            self.assertEqual(reply, b"\x05\xff",
                             "应回 0x05 0xFF，实际 %r" % (reply,))
        finally:
            conn.close()
        self.assertEqual(proxy.stats["connections"], 0)

    def test_no_auth_mode_accepts_00(self):
        """无认证模式必须接受 0x00，且不能要求客户端发子协商。

        有些客户端（含 curl）在服务端只回 0x00 时不再发子协商包，所以这条
        用例发完 0x00 就直接发包，模拟那种客户端。
        """
        proxy = self.new_proxy()
        self._proxy = proxy
        conn = socket.create_connection(("127.0.0.1", proxy.port), 5)
        conn.settimeout(5)
        try:
            conn.sendall(b"\x05\x01\x02")
            self.assertEqual(self._read(conn, 2), b"\x05\x00",
                             "无认证模式即使客户端只报 0x02 也应回 0x00")
            conn.sendall(b"\x05\x01\x00\x01"
                         + socket.inet_aton("127.0.0.1")
                         + struct.pack("!H", self.echo.port))
            reply = self._read(conn, 10)
            self.assertEqual(reply[1], 0x00,
                             "应答码应为成功 0x00，实际 0x%02x" % reply[1])
            conn.sendall(b"no-subnegotiation")
            self.assertEqual(conn.recv(64), b"no-subnegotiation")
        finally:
            conn.close()


# ---------------------------------------------------------------- 转发语义

class RelaySemanticsTest(ProxyCase):
    def test_half_close_still_delivers_reply(self):
        """半关闭：客户端 `shutdown(SHUT_WR)` 之后，仍要收到服务端发来的字节。

        这是本文件最重要的一条。把两个方向合成一个循环、一端 EOF 就整体关掉的
        实现会在这里失败——客户端发完就关写、然后等回答（`nc -N` 的用法），
        若不转发半关闭，它永远等不到那条回答。用 mock 不可能测出来。
        """
        proxy = self.new_proxy()
        self._proxy = proxy
        sock = pivot.socks5_connect("127.0.0.1", proxy.port, "127.0.0.1",
                                    self.echo.port)
        sock.settimeout(5)
        try:
            sock.sendall(b"half-close-payload")
            sock.shutdown(socket.SHUT_WR)          # 我不再发了，但还要收
            received = b""
            while len(received) < len(b"half-close-payload"):
                chunk = sock.recv(64)
                if not chunk:
                    break
                received += chunk
            self.assertEqual(received, b"half-close-payload",
                             "半关闭之后没能收全回包：%r" % (received,))
            self.assertEqual(sock.recv(64), b"",
                             "echo 已关写，客户端应读到 EOF")
        finally:
            sock.close()

    def test_server_speaking_first_reaches_client(self):
        """服务端先说话：客户端发一句触发后，必须能收到服务端发来的 banner。

        走的是"单向上行 + 单向下行 + 服务端先 EOF"这条路径：半关闭处理错了的话，
        服务端关写之后的 banner 会丢在缓冲区里谁也读不到。
        """
        service = EchoService(reply=b"banner-from-service\n")
        self.addCleanup(service.stop)
        proxy = self.new_proxy()
        self._proxy = proxy
        sock = pivot.socks5_connect("127.0.0.1", proxy.port, "127.0.0.1",
                                    service.port)
        sock.settimeout(5)
        try:
            sock.sendall(b"trigger\n")
            data = sock.recv(64)
            self.assertEqual(data, b"banner-from-service\n",
                             "服务端主动发来的 banner 没送到客户端：%r" % (data,))
            self.assertEqual(sock.recv(64), b"",
                             "服务端已关写，客户端随后应读到 EOF")
        finally:
            sock.close()

    def test_concurrent_clients(self):
        """8 个客户端同时经代理往返：全部成功，connections 计数正确。"""
        proxy = self.new_proxy(max_conns=32)
        self._proxy = proxy
        results = {}
        barrier = threading.Barrier(8)

        def worker(index):
            payload = ("client-%d-" % index).encode("utf-8") + os.urandom(8)
            try:
                barrier.wait(timeout=10)
                sock = pivot.socks5_connect("127.0.0.1", proxy.port, "127.0.0.1",
                                            self.echo.port)
                try:
                    sock.settimeout(10)
                    sock.sendall(payload)
                    got = b""
                    while len(got) < len(payload):
                        chunk = sock.recv(1024)
                        if not chunk:
                            break
                        got += chunk
                    results[index] = got
                finally:
                    sock.close()
            except Exception as exc:                # noqa: BLE001 - 要把失败带回主线程
                results[index] = "错误：%r" % (exc,)

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        for index, got in results.items():
            self.assertEqual(len(got), len(("client-%d-" % index).encode("utf-8")) + 8,
                             "第 %d 个客户端的回包长度不对：%r" % (index, got))
        self.assertTrue(proxy.wait_idle(10.0), "并发转发没有收尾")
        self.assertEqual(proxy.stats["connections"], 8,
                         "应记 8 条成功连接，实际 %r" % (proxy.stats,))
        self.assertEqual(proxy.stats["active"], 0)

    def test_byte_accounting_is_exact(self):
        """字节统计必须等于真实转发的字节数，不能只断言"大于 0"。"""
        proxy = self.new_proxy()
        self._proxy = proxy
        up_payload = b"U" * 4096
        down_payload = b"D" * 8192
        sock = pivot.socks5_connect("127.0.0.1", proxy.port, "127.0.0.1",
                                    self.echo.port)
        sock.settimeout(10)
        try:
            sock.sendall(up_payload)
            got = b""
            while len(got) < len(up_payload):
                got += sock.recv(65536)
            self.assertEqual(len(got), len(up_payload))
            # echo 原样返回，所以上行下行都是 4096。再灌一段更大的下行，
            # 让两个方向的计数**不同**，避免把"两边都加了 1"当成算对了。
            sock.sendall(down_payload)
            got = b""
            while len(got) < len(down_payload):
                got += sock.recv(65536)
            self.assertEqual(len(got), len(down_payload))
        finally:
            sock.close()
        self.assertTrue(proxy.wait_idle(10.0), "转发没有收尾，计数读出来会是错的")
        stats = proxy.stats
        self.assertEqual(stats["bytesUp"], 4096 + 8192,
                         "上行字节数应精确等于发送量，实际 %r" % (stats,))
        self.assertEqual(stats["bytesDown"], 4096 + 8192,
                         "下行字节数应精确等于回包量，实际 %r" % (stats,))
        self.assertEqual(stats["connections"], 1)

    def test_max_conns_refuses_and_counts(self):
        """超过并发上限的连接被拒绝并计数——而不是排队等到超时。"""
        proxy = self.new_proxy(max_conns=1)
        self._proxy = proxy
        holder = pivot.socks5_connect("127.0.0.1", proxy.port, "127.0.0.1",
                                      self.echo.port)
        try:
            # 等在途连接真的进到 active 里，否则第二次握手会撞上"还没占用"。
            deadline = time.time() + 5
            while time.time() < deadline and proxy.stats["active"] < 1:
                time.sleep(0.02)
            self.assertEqual(proxy.stats["active"], 1)
            extra = socket.create_connection(("127.0.0.1", proxy.port), 5)
            extra.settimeout(5)
            try:
                with self.assertRaises((ConnectionError, OSError)) as ctx:
                    extra.sendall(b"\x05\x01\x00")
                    extra.recv(2)
                self.assertEqual(proxy.stats["denied"], 1,
                                 "超并发的拒绝应计进 denied，实际 %r" % (proxy.stats,))
            finally:
                extra.close()
        finally:
            holder.close()

    def test_garbage_bytes_do_not_kill_service(self):
        """收到非 SOCKS5 字节：连接被关掉，但服务继续接受新连接。"""
        proxy = self.new_proxy()
        self._proxy = proxy
        junk = socket.create_connection(("127.0.0.1", proxy.port), 5)
        junk.settimeout(5)
        try:
            junk.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
            try:
                reply = junk.recv(16)
            except OSError:
                reply = b""            # 被复位也算"关掉"
            self.assertEqual(reply, b"",
                             "对垃圾字节不应回任何数据（回了就能被指纹化）：%r"
                             % (reply,))
        finally:
            junk.close()
        self.assertIn(proxy.stats["connections"], (0,),
                      "垃圾连接不该被算成成功连接")
        # 服务还活着。
        sock = pivot.socks5_connect("127.0.0.1", proxy.port, "127.0.0.1",
                                    self.echo.port)
        sock.sendall(b"still-alive")
        self.assertEqual(sock.recv(64), b"still-alive")
        sock.close()


# ---------------------------------------------------------------- 客户端失败路径

class ClientFailureTest(unittest.TestCase):
    def test_connect_failure_reports_step(self):
        """代理端口没人听：错误信息必须说明是"连不上代理"这一步。"""
        # 先占一个端口再关掉，拿到一个几乎肯定没人听的端口号。
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(pivot.PivotError) as ctx:
            pivot.socks5_connect("127.0.0.1", port, "127.0.0.1", 80, timeout=2)
        self.assertIn("连接代理", str(ctx.exception),
                      "错误信息应指出是连接代理这一步：%s" % ctx.exception)

    def test_reply_code_maps_to_hint(self):
        """应答码要翻译成人话：0x05 → 目标拒绝连接，而不是"未知错误"。"""
        self.assertIn("白名单", pivot._reply_text(pivot.REP_NOT_ALLOWED))
        self.assertIn("拒绝连接", pivot._reply_text(pivot.REP_CONN_REFUSED))

    def test_bad_target_rejected_before_network(self):
        with self.assertRaises(pivot.PivotError):
            pivot.socks5_connect("127.0.0.1", 1080, "", 80)
        with self.assertRaises(pivot.PivotError):
            pivot.socks5_connect("127.0.0.1", 1080, "127.0.0.1", 0)
        with self.assertRaises(pivot.PivotError):
            pivot.socks5_connect("127.0.0.1", 1080, "127.0.0.1", 70000)


# ---------------------------------------------------------------- 立足点

class FootholdTest(unittest.TestCase):
    def test_build_accepts_known_kinds(self):
        for kind in pivot.FOOTHOLD_KINDS:
            foothold = pivot.build_foothold(kind, "10.0.0.5", 1080)
            self.assertEqual(foothold.kind, kind)
            self.assertTrue(foothold.id.startswith(kind + "-"),
                            "id 前缀应带种类：%s" % foothold.id)

    def test_build_rejects_unknown_kind(self):
        for bad in ("socks", "SOCK5", "tunel", "", "rce"):
            with self.assertRaises(pivot.PivotError) as ctx:
                pivot.build_foothold(bad, "10.0.0.5", 1080)
            self.assertIn("种类非法", str(ctx.exception))
            self.assertIn(bad or "''", str(ctx.exception))

    def test_round_trip(self):
        foothold = pivot.Foothold(
            id="fs-1", kind="ssh", host="10.0.0.5", port=22, note="跳板机",
            scope=["10.1.0.0/16", "192.168.8.0/24"], tags=["dmz", "linux"],
            capability="ssh", created_at=1700000000.0)
        data = foothold.to_dict()
        self.assertEqual(data["kind"], "ssh")
        self.assertEqual(data["scope"], ["10.1.0.0/16", "192.168.8.0/24"])
        self.assertEqual(data["createdAt"], 1700000000.0)
        clone = pivot.Foothold.from_dict(data)
        self.assertEqual(clone.to_dict(), data, "to_dict/from_dict 必须往返一致")

    def test_round_trip_with_defaults(self):
        data = {"id": "fs-2", "kind": "webshell", "host": "10.0.0.9"}
        clone = pivot.Foothold.from_dict(data)
        self.assertEqual(clone.port, 0)
        self.assertEqual(clone.scope, [])
        self.assertEqual(clone.tags, [])
        self.assertEqual(clone.capability, pivot.DEFAULT_CAPABILITY)
        self.assertEqual(clone.to_dict()["id"], "fs-2")

    def test_invalid_construction(self):
        with self.assertRaises(pivot.PivotError):
            pivot.Foothold(id="x", kind="socks", host="10.0.0.5")
        with self.assertRaises(pivot.PivotError):
            pivot.Foothold(id="x", kind="ssh", host="")
        with self.assertRaises(pivot.PivotError):
            pivot.Foothold(id="x", kind="ssh", host="10.0.0.5", port=99999)
        with self.assertRaises(pivot.PivotError):
            pivot.Foothold(id="", kind="ssh", host="10.0.0.5")
        with self.assertRaises(pivot.PivotError):
            pivot.Foothold.from_dict({"id": "x", "kind": "bogus", "host": "h"})

    def test_every_kind_and_status_has_a_chinese_word(self):
        """报告里那一行要有中文说法，而且表外的值原样回。

        「本次任务登记的立足点」原先印的是 `[socks5] 10.0.0.5:1080 — open`，与
        同一份文档里的「严重程度：高危」是两种语言。词表放在 `pivot` 里而不是报告
        自己写一份：立足点的种类本来就由这个模块定义，两处各列一遍等于给同一件事
        留两个可以漂的答案。

        表外的值**原样回**：把 `SOCKS5` 改写回 `socks5` 会让那个值再也 grep 不到原文，
        而认不出的种类正是最需要照原样看的东西。
        """
        self.assertEqual(set(pivot.FOOTHOLD_KIND_LABELS), set(pivot.FOOTHOLD_KINDS),
                         "有一类立足点没有中文名——报告里会显示成裸 id")
        for kind in pivot.FOOTHOLD_KINDS:
            word = pivot.foothold_kind_label(kind)
            self.assertTrue(word and word != kind,
                            "%s 的中文名还是它自己" % kind)
        self.assertEqual(len(set(pivot.FOOTHOLD_KIND_LABELS.values())),
                         len(pivot.FOOTHOLD_KINDS), "两种立足点共用一个中文名")
        self.assertEqual(pivot.foothold_kind_label("shell"), "shell")
        self.assertEqual(pivot.foothold_kind_label(""), "")
        self.assertEqual(pivot.foothold_kind_label(None), "")
        self.assertEqual(pivot.foothold_kind_label(" SSH "), "SSH 跳板")
        self.assertEqual(pivot.foothold_status_label("open"), "存活")
        self.assertEqual(pivot.foothold_status_label("closed"), "已关闭")
        self.assertEqual(pivot.foothold_status_label("gone"), "gone")

    def test_scope_keeps_bad_entries_instead_of_raising(self):
        """登记信息是地图不是门：一条写坏的网段不该让整条记录读不出来。"""
        foothold = pivot.build_foothold("agent", "10.0.0.5",
                                        scope=["10.0.0.0/8", "not-a-cidr", ""])
        self.assertEqual(foothold.scope, ["10.0.0.0/8", "not-a-cidr"])
        self.assertIn("10.0.0.0/8", foothold.summary())


if __name__ == "__main__":
    unittest.main()
