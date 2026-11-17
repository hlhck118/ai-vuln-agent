# -*- coding: utf-8 -*-
"""模型端点的地址回退：坏地址排在最前面时，请求必须仍然成功。

## 针对的故障

同一个主机名解析出多个地址，其中一个是坏的（云厂商下线节点、中间设备重置
TLS）。标准库的地址回退**只覆盖 TCP**：`socket.create_connection` 在
`sock.connect()` 成功的那一刻就返回了，而 TLS 握手在它的上一层。所以"TCP 通、
TLS 被断"这条路径不会换地址重试，整条请求失败，报的是

    [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol

而它是**间歇性**的——命中好地址就一切正常。这类故障最容易被归结成"网络不稳"，
因为两次失败长得一模一样，而"再点一次测试按钮就好了"看起来像是运气。

## 夹具为什么长这样

本机没有 openssl，起不了"真签证书"的服务，所以证书是**事先生成、把 PEM 固化
在本文件里**的自签证书：只签给 `localhost` 与 `api.deepseek.com`，有效期到
2056 年，**只在这个文件里被读取、只在回环地址上监听**。它没有任何保密价值；
写死在这里是为了让用例不需要外部工具就能在任何机器上跑起来（本项目的
`test_production.py` / `test_probe.py` 在缺 openssl 时是跳过整组用例的，这条路
能让同一件事**不用跳过**）。

坏地址那一端用真实 socket 造：accept 之后**先把客户端发来的字节读掉再 close**。
不读就关，Windows 会发 RST，客户端看到的是 `ConnectionAbortedError`；先读再关
才会发干净的 FIN，客户端才看到"握手期间 EOF"——也就是那句真实报错。
`FixtureFidelityTestCase` 专门钉住这一点：夹具造错了，后面所有用例就只是在
测一个别的问题，而且会一路绿。
"""
import json
import os
import shutil
import socket
import ssl
import tempfile
import threading
import unittest
import urllib.request

import http.server

from pentestd import providers, streaming

# 借一个真实域名当被测主机名：这不是为了连它，解析在用例里被替换成回环地址，
# 只是让"同一个域名两个地址"这件事读起来就是它本来的样子。
HOST = "api.deepseek.com"

_TEST_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIDLDCCAhSgAwIBAgIQa0EVsv/SMLtEZCiXC8u1qjANBgkqhkiG9w0BAQsFADAU
MRIwEAYDVQQDDAlsb2NhbGhvc3QwIBcNMjYxMDE0MDY0ODQ3WhgPMjA1NjEwMTQw
NjU4NDdaMBQxEjAQBgNVBAMMCWxvY2FsaG9zdDCCASIwDQYJKoZIhvcNAQEBBQAD
ggEPADCCAQoCggEBAJ5qbfQH3LmZS5Qnfi6Y0l6dNt4clSKF3GvsbhN5HRTYFIBY
nFlqEmMZNNmOhX8V/27HGG6LyMy8MXHnl6A8OuJ2AtP6ux2PRf39XUSWeJhFe1KR
PGY5/p7+SBb+NBxPy2B04iZ0n7KMi22fZhHQKf2dkW1qIlN3XUf5jWS/9JnkzrSI
uF2Ymx7O2pGonPwZKwQT329a9PMiiMPUf9w/GShWfoT24sYzWgfgxTHQVf7eh+2z
Xe4czZbUEdxcuhES6kYtIEKRndLpnSfvx2Q+XBpXVAufu3Pj5W/aDk3VaxjjgWSo
jwD++1iKwRxoeZYNUp+UZVLb7Jhq/CiqjlZRRY0CAwEAAaN4MHYwDgYDVR0PAQH/
BAQDAgWgMB0GA1UdJQQWMBQGCCsGAQUFBwMCBggrBgEFBQcDATAmBgNVHREEHzAd
gglsb2NhbGhvc3SCEGFwaS5kZWVwc2Vlay5jb20wHQYDVR0OBBYEFB2k17hO6aeR
XALO1goUvKAMjONjMA0GCSqGSIb3DQEBCwUAA4IBAQBPFhe4W3Q+p523WsFvfCIw
fz7tvL7f4hQks1xYWusDm4DY6gTe3Cl8QshNlDOD3eZZAsm3WWfpNxoRx7l7W8Lk
29Om+jPERTun/345hflLMVuu6cpxcz885blLgBfGdzx7MVNoexYNy+4Z0luV2nFw
5h5AIZ8kQpIFmxXSYVnpFpqKfVxAsH5T6f0EuWirKjhIbzOgw8H8cPQl+vAtIBBL
UfDlcJkdeZcbroYFricOJCQ0NokZrVmMcHGQ1egXDcbsupeZK4WZrmeLXYw8qY1f
Dv1J8kc5LQSnzMaabCSyahYcWAXZkXobW+fnoU6dSVT7dNdb5PwGr3lBEY6YtnUN
-----END CERTIFICATE-----
"""

_TEST_KEY_PEM = """-----BEGIN RSA PRIVATE KEY-----
MIIEpQIBAAKCAQEAnmpt9AfcuZlLlCd+LpjSXp023hyVIoXca+xuE3kdFNgUgFic
WWoSYxk02Y6FfxX/bscYbovIzLwxceeXoDw64nYC0/q7HY9F/f1dRJZ4mEV7UpE8
Zjn+nv5IFv40HE/LYHTiJnSfsoyLbZ9mEdAp/Z2RbWoiU3ddR/mNZL/0meTOtIi4
XZibHs7akaic/BkrBBPfb1r08yKIw9R/3D8ZKFZ+hPbixjNaB+DFMdBV/t6H7bNd
7hzNltQR3Fy6ERLqRi0gQpGd0umdJ+/HZD5cGldUC5+7c+Plb9oOTdVrGOOBZKiP
AP77WIrBHGh5lg1Sn5RlUtvsmGr8KKqOVlFFjQIDAQABAoIBAQCMfWS/280jiFDf
tlqQZpWCtTh4qK6WLFL8H3TLrkhdOPiNjogyQADIhA07t8qql06t5sHDIBLXhzru
gm4U+/VY60G8zwrsvyhahT0W279dg5lH82e2XU5zoJ08TvrV9Oovw9jgM+OBDGWG
tatmts7WjcvwVgTVMvG2KBn+NMJvS3fYbMZeom94eVBgKqTlHmlCI2Zw//EM6Gtj
VplnmmDMSQBPz4IgzwX4SIZtjefnUGT3JPxzkWE1EQlVgA0hC0gJRU4VNV9YqoyS
1oQ6hdWt/BTusy0ycP2oe/DlFf9swz8v+26WXyto9NmND72yCJKz7h0DJCCZqCWv
eqM4qKsdAoGBAMnJhBfMqeUQ+nHbWPyRWiHOQyH6zhCHJM/KwhiWIHMPZS0S16RL
udjSCleP0K+tZNH3o/lKfKnQNY36A1wJZevWVBZzy+VxK8I6l8bZlXatwpBc3NQJ
mbyGh6Vttl92xCPHExnZL3OSqneKo81D6DETRoRheTQAVyYk8I+oMHHnAoGBAMj5
66dtPQiKGQduEQ8Xrs/ppwjjAwGD0WSv/+M5FULGbFuu07x3YKVn7vMwWX0GLWrb
G8s6kDIxzv6w5HrD5DkOK3gLO3qzFhiRXu4l2M3efnkZt8Yj3ReyNXhBmHuHZrdR
l8Weo58v8Tdo3IVOTrUPeWD5jjjaSMF8gPchH8ZrAoGBAIV63/4+TERSrtiSGJM6
Mz0ZjB72IUmdnCQ4I5zP8jcqf+sJA8ySqyVaC61QXONY99BM2BMXys8qA8MfFwnS
zbDr3kPb5YypCCE7URsg41XWK1YDl7BLiX7EHwa3RGRx09022tPGbzUsV8cr1p75
xKZx8buwHtTfRMbeuKLOxv0RAoGARD3AS6hm+fhSSViRSEulWwmeYwEaKzCEMCkm
vRPL6VGQx4zUboEmeuOQmljHehFX6JmflWE8Rx7sh0SvQBQ18T2jeu5tcttOXGc4
EaseM8EhtqMJ6KiS0RH83+l+CivOr95vp3WXX10bDdJ4VndheE9ZypjSNvG/lJte
+wrvTCMCgYEAlpyeKUM2B1fhAmuhLBKd8jpFoPCOFkr8L1EasY2eVuX1f4Ccrv7a
ehwaNH4pw59JNoV9/so0O2a9Qoo3EEDMUe/QBkG6ckH/vNVzOIqeYt8xQjvFM/Yv
WhghPG73y3Qb+sxBQBntfli7IGChA/5zXYqSz1zGy3Gj5lZUq6pRkjU=
-----END RSA PRIVATE KEY-----
"""

DEEPSEEK_REPLY = {"choices": [{"index": 0, "finish_reason": "stop",
                                "message": {"role": "assistant",
                                            "content": "pong"}}]}
OLLAMA_REPLY = {"model": "local", "done": True,
                "message": {"role": "assistant", "content": "pong"}}
DEEPSEEK_SSE = (b'data: {"choices":[{"index":0,"delta":{"content":"po"}}]}\n\n'
                b'data: {"choices":[{"index":0,"delta":{"content":"ng"},'
                b'"finish_reason":"stop"}]}\n\n'
                b'data: [DONE]\n\n')

_CERT_DIR = ""


def setUpModule():
    """把夹具证书落盘一次。

    另把 `getproxies` 打成空：本文件断言的是"逐地址尝试"，而 `ProxyHandler`
    一旦被环境变量或注册表里的代理配置激活，请求会整个改道走代理，那时连的
    根本不是我起的端点——用例会以一种与主题无关的方式失败或通过。
    """
    global _CERT_DIR
    _CERT_DIR = tempfile.mkdtemp(prefix="dsh-provider-cert-")
    for name, text in (("cert.pem", _TEST_CERT_PEM), ("key.pem", _TEST_KEY_PEM)):
        with open(os.path.join(_CERT_DIR, name), "w", encoding="ascii") as handle:
            handle.write(text)
    unittest.addModuleCleanup(_restore_proxies, urllib.request.getproxies)
    urllib.request.getproxies = lambda: {}


def tearDownModule():
    if _CERT_DIR:
        shutil.rmtree(_CERT_DIR, ignore_errors=True)


def _restore_proxies(original):
    urllib.request.getproxies = original


class DrainingListener:
    """一个**明文**监听器：accept 之后把客户端发来的字节读掉，再优雅关闭。

    它就是"坏地址"的那一端，模拟的是 TCP 层通、TLS 层被断。读掉再关这一步
    不能省，理由见模块文档——不读就关在 Windows 上会变成 RST，造出来的是另一种
    错误，而那种错误**本来就**会被 `create_connection` 的回退兜住，用例会假绿。
    """

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.seen = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            self.seen += 1
            try:
                conn.settimeout(1.0)
                try:
                    conn.recv(4096)
                except OSError:
                    pass
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        self.sock.close()
        self._thread.join(timeout=2)


class DeadPort:
    """一个刚被释放的端口：连它得到的是"连接被拒绝"。

    "TCP 层就失败"的那种坏地址只能这样造——真起一个拒绝连接的服务需要
    管理员权限，而绑一个空闲端口再释放，在回环上得到的是同样的结果。
    """

    def __init__(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()


class Endpoint:
    """一个真的 TLS（或明文）端点，返回预设的响应体。"""

    def __init__(self, body, content_type="application/json", tls=True):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.content_type = content_type
        self.requests = 0
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(os.path.join(_CERT_DIR, "cert.pem"),
                                    os.path.join(_CERT_DIR, "key.pem"))
            self.httpd.socket = context.wrap_socket(self.httpd.socket,
                                                    server_side=True)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def _handler(self):
        endpoint = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                endpoint.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", endpoint.content_type)
                self.send_header("Content-Length", str(len(endpoint.body)))
                self.end_headers()
                self.wfile.write(endpoint.body)

            def log_message(self, *args):
                pass

        return Handler

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=3)


class ResolveStub:
    """把某个主机名解析成指定的地址表；其它主机名原样走真实解析。

    只拦这一个名字：`socket.getaddrinfo` 是进程级的，全拦会把
    `ThreadingHTTPServer` 自己那条路径也改掉。
    """

    def __init__(self, host, addresses):
        self.host = host
        self.addresses = [tuple(a) for a in addresses]

    def __enter__(self):
        self._real = socket.getaddrinfo

        def fake(host, port, *args, **kwargs):
            if host == self.host:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                         (ip, p)) for ip, p in self.addresses]
            return self._real(host, port, *args, **kwargs)

        socket.getaddrinfo = fake
        return self

    def __exit__(self, *exc):
        socket.getaddrinfo = self._real
        return False


class FixtureFidelityTestCase(unittest.TestCase):
    """夹具必须先造出**真实的那句报错**，否则后面的用例全在测别的东西。"""

    def setUp(self):
        providers.reset_address_health()

    def test_the_plaintext_listener_reproduces_the_reported_error(self):
        """坏地址造出来的必须是 UNEXPECTED_EOF_WHILE_READING。

        这条不通过就意味着夹具退化了（比如某天把"先读再关"删掉），而那时
        `test_tls_failure_on_the_first_address_still_reaches_the_second` 会
        因为一个完全不同的原因继续变绿。
        """
        bad = DrainingListener()
        self.addCleanup(bad.stop)
        with ResolveStub(HOST, [("127.0.0.1", bad.port)]):
            with self.assertRaises(providers.ConnectAttemptsError) as caught:
                providers.connect_any(HOST, bad.port, 5.0,
                                      ssl.create_default_context())
        self.assertEqual(caught.exception.attempts[0][1],
                         "UNEXPECTED_EOF_WHILE_READING")
        self.assertEqual(bad.seen, 1)


class AddressFallbackTestCase(unittest.TestCase):

    def setUp(self):
        providers.reset_address_health()

    def _context(self):
        # 自签证书：关掉校验，但**仍然是一个真实的 SSLContext**，握手是真的。
        return ssl._create_unverified_context()

    def test_tls_failure_on_the_first_address_still_reaches_the_second(self):
        """核心回归：第一个地址 TCP 通、TLS 被断时，必须继续试下一个地址。

        修之前这里会抛 SSLEOFError —— 因为 `socket.create_connection` 已经把
        坏地址的 socket 返回出去了，TLS 失败发生在它的上一层，没人会换地址。
        """
        bad = DrainingListener()
        self.addCleanup(bad.stop)
        good = Endpoint(DEEPSEEK_REPLY)
        self.addCleanup(good.stop)
        with ResolveStub(HOST, [("127.0.0.1", bad.port),
                                ("127.0.0.1", good.port)]):
            sock = providers.connect_any(HOST, good.port, 5.0, self._context())
        self.addCleanup(sock.close)
        self.assertGreaterEqual(bad.seen, 1,
                                "坏地址必须真的被连过，否则这个用例什么也没证明")
        self.assertEqual(sock.getpeername(), ("127.0.0.1", good.port),
                         "回退之后必须连在第二个地址上")

    def test_a_failed_address_is_tried_last_on_the_next_attempt(self):
        """第一次失败之后，坏地址该排到最后——它不该是每次请求都要付的代价。

        观察的是**副作用**而不是内部排序结果：第二次调用如果没有再碰那个坏
        地址，它记到的连接数就不会变。
        """
        bad = DrainingListener()
        self.addCleanup(bad.stop)
        good = Endpoint(DEEPSEEK_REPLY)
        self.addCleanup(good.stop)
        with ResolveStub(HOST, [("127.0.0.1", bad.port),
                                ("127.0.0.1", good.port)]):
            providers.connect_any(HOST, good.port, 5.0, self._context()).close()
            self.assertGreaterEqual(bad.seen, 1)
            seen_after_first = bad.seen
            providers.connect_any(HOST, good.port, 5.0, self._context()).close()
        self.assertEqual(bad.seen, seen_after_first,
                         "已经失败的地址在冷却期内不该再被尝试")

    def test_every_address_is_reported_when_they_all_fail(self):
        """全部失败时报的是**每个**地址各自的原因，不是一个笼统的失败。

        只报最后一个的话，"两个地址都失败"与"只有一个地址"长得一模一样，
        而这两者的下一步动作完全不同：前者查链路，后者查 DNS。
        """
        first = DrainingListener()
        self.addCleanup(first.stop)
        second = DrainingListener()
        self.addCleanup(second.stop)
        with ResolveStub(HOST, [("127.0.0.1", first.port),
                                ("127.0.0.1", second.port)]):
            with self.assertRaises(providers.ConnectAttemptsError) as caught:
                providers.connect_any(HOST, first.port, 5.0,
                                      ssl.create_default_context())
        error = caught.exception
        self.assertEqual(len(error.attempts), 2)
        self.assertIn("已尝试 2 个地址", str(error))
        self.assertIn("UNEXPECTED_EOF_WHILE_READING", str(error))
        self.assertGreaterEqual(first.seen, 1)
        self.assertGreaterEqual(second.seen, 1,
                                "第一个失败之后必须接着试第二个")

    def test_a_cooling_address_is_still_attempted_when_it_is_the_only_one(self):
        """冷却只是顺序，不是黑名单。

        把已知坏的地址删掉，就等于把某次故障时的判断变成一张永久黑名单；而
        DNS 记录会变、节点会被修好——那时表现是"明明恢复了却连不上"，且没有
        任何地方会说出原因。
        """
        bad = DrainingListener()
        self.addCleanup(bad.stop)
        with ResolveStub(HOST, [("127.0.0.1", bad.port)]):
            for _ in range(2):
                with self.assertRaises(providers.ConnectAttemptsError):
                    providers.connect_any(HOST, bad.port, 5.0,
                                          ssl.create_default_context())
        self.assertEqual(bad.seen, 2, "它在冷却中，但仍然必须被尝试")

    def test_a_resolvable_host_with_one_dead_address_still_succeeds(self):
        """TCP 层就被拒的坏地址：回退同样要生效。"""
        dead = DeadPort()
        good = Endpoint(DEEPSEEK_REPLY)
        self.addCleanup(good.stop)
        with ResolveStub(HOST, [("127.0.0.1", dead.port),
                                ("127.0.0.1", good.port)]):
            sock = providers.connect_any(HOST, good.port, 5.0, self._context())
        self.addCleanup(sock.close)
        self.assertEqual(sock.getpeername(), ("127.0.0.1", good.port))

    def test_health_is_recorded_per_address_not_per_host(self):
        """健康记录按 `IP:端口` 记，不按 IP。

        同一台机器上"443 坏了"不等于"8443 也坏"。按 IP 记会让两个端点的状态
        互相覆盖，而症状是"一个地址明明好好的，却被另一个地址的失败带下去了"。
        """
        dead = DeadPort()
        good = Endpoint(DEEPSEEK_REPLY)
        self.addCleanup(good.stop)
        with ResolveStub(HOST, [("127.0.0.1", dead.port),
                                ("127.0.0.1", good.port)]):
            providers.connect_any(HOST, good.port, 5.0, self._context()).close()
        table = providers.address_health()[HOST]
        self.assertEqual(table["127.0.0.1:%d" % dead.port]["fail"], 1.0)
        self.assertEqual(table["127.0.0.1:%d" % good.port]["ok"], 1.0)
        self.assertEqual(table["127.0.0.1:%d" % good.port]["until"], 0.0,
                         "成功的地址不该带着冷却时间")


class AddressBudgetTestCase(unittest.TestCase):
    """单个地址的连接预算：只在**有备选**时才缩短。"""

    def test_single_address_keeps_the_callers_timeout(self):
        """没有备选可换时，缩短预算只会把"慢但能成"改成"失败"。"""
        self.assertEqual(providers._address_budget(90.0, 1), 90.0)
        self.assertEqual(providers._address_budget(2.5, 1), 2.5)

    def test_multiple_addresses_share_a_shorter_budget(self):
        self.assertEqual(providers._address_budget(90.0, 2),
                         providers.DEFAULT_CONNECT_BUDGET_S)

    def test_a_budget_never_exceeds_what_the_caller_gave(self):
        """调用方给的超时比默认预算还短时，以调用方为准。"""
        self.assertEqual(providers._address_budget(3.0, 4), 3.0)


class TestConnectionEndToEndTestCase(unittest.TestCase):
    """端到端：走完整的 opener，而不是直接调 `connect_any`。"""

    def setUp(self):
        providers.reset_address_health()

    def test_the_test_button_survives_a_blocked_first_address(self):
        bad = DrainingListener()
        self.addCleanup(bad.stop)
        good = Endpoint(DEEPSEEK_REPLY)
        self.addCleanup(good.stop)
        profile = {"provider": "deepseek", "model": "deepseek-flash",
                   "baseUrl": "https://api.deepseek.com:%d" % good.port,
                   "timeoutS": 10, "maxTokens": 64, "thinking": "disabled"}
        with ResolveStub(HOST, [("127.0.0.1", bad.port),
                                ("127.0.0.1", good.port)]):
            result = providers.test_connection(profile, "sk-test", insecure=True)
        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(result["sample"], "pong")
        self.assertGreaterEqual(bad.seen, 1)
        self.assertEqual(good.requests, 1)

    def test_plain_http_uses_the_same_fallback(self):
        """明文那条路也走同一套：否则"https 会换地址、http 不会"会在某次
        调试里被当成协议问题。"""
        dead = DeadPort()
        good = Endpoint(OLLAMA_REPLY, tls=False)
        self.addCleanup(good.stop)
        profile = {"provider": "ollama", "model": "local",
                   "baseUrl": "http://api.deepseek.com:%d" % good.port,
                   "timeoutS": 10}
        with ResolveStub(HOST, [("127.0.0.1", dead.port),
                                ("127.0.0.1", good.port)]):
            result = providers.test_connection(profile, "")
        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(result["sample"], "pong")

    def test_streaming_uses_the_same_fallback(self):
        """流式那条路必须也在回退范围内。

        它走的是 `streaming.iter_chat`，不是 `providers.chat_once`——两条路各写
        一遍 opener 的话，就会变成"整包请求能自愈、开流式就报 UNEXPECTED_EOF"。
        """
        bad = DrainingListener()
        self.addCleanup(bad.stop)
        good = Endpoint(DEEPSEEK_SSE, content_type="text/event-stream")
        self.addCleanup(good.stop)
        profile = {"provider": "deepseek", "model": "deepseek-flash",
                   "baseUrl": "https://api.deepseek.com:%d" % good.port,
                   "timeoutS": 10, "maxTokens": 64, "thinking": "disabled"}
        with ResolveStub(HOST, [("127.0.0.1", bad.port),
                                ("127.0.0.1", good.port)]):
            result = streaming.chat_stream(profile, "sk-test",
                                           [{"role": "user", "content": "ping"}],
                                           insecure=True)
        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(result["text"], "pong")
        self.assertGreaterEqual(bad.seen, 1)


class FailureMessageTestCase(unittest.TestCase):
    """报错文案：要能指出是哪个地址、以及失败发生在哪一层。"""

    def setUp(self):
        providers.reset_address_health()

    def test_a_real_tls_failure_is_reported_as_a_reason_code(self):
        """真实链路里取到的是 `UNEXPECTED_EOF_WHILE_READING`，而不是那句带着
        `(_ssl.c:1000)` 的完整 `str()`。

        那段 C 源码位置与使用者要做的事无关，而且随 Python 版本变——留着它，
        同一条故障在不同版本上的报错文本就不一样，也就没法拿去搜。
        """
        bad = DrainingListener()
        self.addCleanup(bad.stop)
        with ResolveStub(HOST, [("127.0.0.1", bad.port)]):
            with self.assertRaises(providers.ConnectAttemptsError) as caught:
                providers.connect_any(HOST, bad.port, 5.0,
                                      ssl.create_default_context())
        reason = caught.exception.attempts[0][1]
        self.assertEqual(reason, "UNEXPECTED_EOF_WHILE_READING")
        self.assertNotIn("_ssl.c", reason)

    def test_a_reason_code_is_recovered_from_the_message(self):
        """取不到 `.reason` 时必须从消息里剥出短标识。

        `SSLError.reason` 是 C 层在**抛出时**填进去的，手工构造的、或经别的库
        转手过的 SSLError 上它是空的。那一路若直接 `str()`，留给使用者的是整句
        `[SSL: XXX] ... (_ssl.c:1000)`——本文件不许它漏出来。
        """
        error = ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"
                                " (_ssl.c:1000)")
        self.assertEqual(providers._reason_text(error), "WRONG_VERSION_NUMBER")

    def test_a_timeout_is_not_reported_as_a_reset(self):
        self.assertEqual(providers._reason_text(TimeoutError("timed out")),
                         "连接超时")

    def test_the_message_names_the_endpoint_and_every_address(self):
        bad = DrainingListener()
        self.addCleanup(bad.stop)
        with ResolveStub(HOST, [("127.0.0.1", bad.port)]):
            with self.assertRaises(providers.ConnectAttemptsError) as caught:
                providers.connect_any(HOST, bad.port, 5.0,
                                      ssl.create_default_context())
        text = str(caught.exception)
        self.assertIn(HOST, text)
        self.assertIn("127.0.0.1", text)


if __name__ == "__main__":
    unittest.main()
