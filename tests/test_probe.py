# -*- coding: utf-8 -*-
"""网络探测（probe）的验证。

三条线各测一层：

* **作用域门禁**是纯函数，逐条钉住边界语义（默认最小、fail-closed、域名只解析
  一次、规模上限）；
* **探测引擎**对着真实监听套接字跑，不用 mock 顶替 socket——mock 只能证明
  "我调用了 connect"，证明不了"拿到 HTTP 状态行"、"认出未授权 Redis"；
* **端到端**起控制面与执行节点，断言副作用：任务真的进了队列、链路真的写进了
  资产与事实、越界目标真的被 403 挡住。
"""
import importlib.util
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agent, authz, chat, cli, engine, pki, probe, scope, server  # noqa: E402
from pentestd.server import ControlPlane                          # noqa: E402
from pentestd.store import Store                                  # noqa: E402
from tests import support                                         # noqa: E402
from tests.test_chat import ScriptedModel, text_reply, tool_reply  # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")

# 本文件里有两条断言盯的是"**认识但未授权**的侵入类型要报 403，而不是 400"——
# 那说的是**闸照判**时的行为，所以这一模块内把 `authz.FORCE_OPEN` 关掉；跑完还原。
#
# 这两条**不是**靠 `assertRaises(authz.AuthzError)` 写的，而是走 HTTP 断言状态码，
# 所以按异常去找的排查扫不到它们——它们的失败形态是 `201 != 403`，看起来像"建任务
# 的路由坏了"，不像"闸被绕过了"。把这条留在这里，好让下一次改这道闸的人知道
# **按错误类型找是找不全的**。
_OPEN = authz.FORCE_OPEN


def setUpModule():
    authz.FORCE_OPEN = False


def tearDownModule():
    authz.FORCE_OPEN = _OPEN


# ---------------------------------------------------------------- 测试夹具

class FakeHttp:
    """最小 HTTP 服务：按路径返回预设响应，用来测指纹与未授权判定。"""

    def __init__(self, routes=None, server_header="FakeServer/1.0"):
        self.routes = routes or {
            "/": (200, "text/html; charset=utf-8",
                  b"<html><head><title>Probe Fixture</title></head>"
                  b"<body>ok</body></html>")}
        self.server_header = server_header
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_GET(self):
                status, ctype, body = outer.routes.get(
                    self.path.split("?")[0], (404, "text/plain", b"not found"))
                if not isinstance(body, bytes):
                    body = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Server", outer.server_header)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        # **端口不能落在 `probe.SERVICE_NAMES` 里。** 临时端口由系统分配，撞上
        # 2049（nfs）、6379（redis）这类条目时，`probe_endpoint` 会把服务名保持成
        # 端口表里的那个（只有 `unknown` 才让指纹覆盖），于是"非标准端口上的 HTTP
        # 也要认出来"这条断言会随机变红——实测撞过一次 2049。丢的不是判定逻辑：
        # 端口表只管展示用的命名，HTTP 指纹才是证据。
        self.httpd = None
        for _ in range(8):
            candidate = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            if probe.service_name(candidate.server_address[1]) == "unknown":
                self.httpd = candidate
                break
            candidate.server_close()
        if self.httpd is None:
            raise AssertionError("临时端口全部落在端口表里，夹具没法用")
        self.httpd.daemon_threads = True
        self.httpd.handle_error = lambda *args: None
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class TlsFixture:
    """带自签证书的 HTTPS 服务：验证证书探测确实能读到"未通过校验"。"""

    def __init__(self, cert: str, key: str):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b"<html><title>tls fixture</title></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.handle_error = lambda *args: None
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeRedis:
    """只说 Redis 协议的最小服务：PING → +PONG，INFO → 版本行。"""

    def __init__(self, require_auth: bool = False):
        self.require_auth = require_auth
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn) -> None:
        with conn:
            while not self._stop:
                try:
                    data = conn.recv(1024)
                except OSError:
                    return
                if not data:
                    return
                text = data.upper()
                try:
                    if self.require_auth:
                        conn.sendall(b"-NOAUTH Authentication required.\r\n")
                    elif b"PING" in text:
                        conn.sendall(b"+PONG\r\n")
                    elif b"INFO" in text:
                        conn.sendall(b"# Server\r\nredis_version:7.2.4\r\n")
                    else:
                        conn.sendall(b"-ERR unknown command\r\n")
                except OSError:
                    return

    def stop(self) -> None:
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


LOCAL_SCOPE = ["127.0.0.0/8", "::1/128"]
FAST_LIMITS = {"connectTimeoutS": 1.0, "bannerTimeoutS": 0.5,
               "totalTimeoutS": 30, "concurrency": 8}


# ---------------------------------------------------------------- 作用域门禁

class ScopeGuardTestCase(unittest.TestCase):
    """门禁是纯函数，边界语义必须逐条钉住：这些规则一旦松开就是"能扫任何地址"。"""

    def test_default_scope_covers_loopback_and_private(self):
        for address in ("127.0.0.1", "10.1.2.3", "192.168.31.7", "172.16.5.4", "::1"):
            self.assertTrue(scope.in_scope(address, scope.DEFAULT_SCOPE), address)

    def test_default_scope_rejects_public_addresses(self):
        for address in ("8.8.8.8", "1.1.1.1", "203.0.113.9"):
            self.assertFalse(scope.in_scope(address, scope.DEFAULT_SCOPE), address)

    def test_empty_scope_fails_closed(self):
        """没配置 ≠ 什么都能扫。这条是整道门禁的底线。"""
        with self.assertRaises(scope.ScopeError) as caught:
            scope.plan("127.0.0.1", "80", [])
        self.assertIn("fail-closed", str(caught.exception))

    def test_any_scope_allows_public_address(self):
        granted = scope.normalize_scope(["any"])
        plan = scope.plan("8.8.8.8", "80", granted)
        self.assertEqual(plan["hosts"], ["8.8.8.8"])

    def test_scope_keywords_pick_a_named_policy(self):
        self.assertEqual(set(scope.normalize_scope(["any"])), set(scope.ANY_SCOPE))
        self.assertEqual(scope.normalize_scope(["default"]), scope.DEFAULT_SCOPE)
        self.assertEqual(scope.normalize_scope(["none"]), [])

    def test_none_beats_every_other_item(self):
        """`none` 压过其它项：关掉探测不该被一个多余的网段稀释成"只关了一部分"。"""
        self.assertEqual(scope.normalize_scope(["any", "none"]), [])

    def test_scope_text_names_both_extremes(self):
        self.assertIn("不限制", scope.scope_text(scope.ANY_SCOPE))
        self.assertIn("拒绝一切", scope.scope_text([]))

    def test_out_of_scope_target_is_rejected_with_reason(self):
        with self.assertRaises(scope.ScopeError) as caught:
            scope.plan("8.8.8.8", "80", scope.DEFAULT_SCOPE)
        message = str(caught.exception)
        self.assertIn("8.8.8.8", message)
        self.assertIn("--probe-scope", message)

    def test_one_address_out_of_scope_rejects_whole_target(self):
        """整体放行或整体拒绝：部分放行会让人以为"任务跑完了"。"""
        with self.assertRaises(scope.ScopeError):
            scope.plan("192.168.0.0/22", "80", ["192.168.0.0/24"])

    def test_scope_item_must_be_a_network_not_a_domain(self):
        with self.assertRaises(scope.ScopeError) as caught:
            scope.normalize_scope(["example.com"])
        self.assertIn("网段", str(caught.exception))

    def test_hostname_is_resolved_then_checked_as_address(self):
        plan = scope.plan("localhost", "80", LOCAL_SCOPE)
        self.assertEqual(plan["kind"], "host")
        self.assertTrue(plan["resolved"]["localhost"])
        for address in plan["resolved"]["localhost"]:
            self.assertTrue(scope.in_scope(address, LOCAL_SCOPE))

    def test_cidr_expansion_and_network_limits(self):
        plan = scope.plan("192.168.1.0/30", "80-82", scope.DEFAULT_SCOPE)
        self.assertEqual(plan["hosts"], ["192.168.1.1", "192.168.1.2"])
        self.assertEqual(plan["ports"], [80, 81, 82])
        with self.assertRaises(scope.ScopeError):
            scope.plan("10.0.0.0/8", "80", scope.DEFAULT_SCOPE)      # 1600 万个地址

    def test_inline_port_in_target_is_merged(self):
        plan = scope.plan("127.0.0.1:8443", "", LOCAL_SCOPE)
        self.assertEqual(plan["hostSpec"], "127.0.0.1")
        self.assertEqual(plan["ports"], [8443])

    def test_port_expression_validation(self):
        self.assertEqual(scope.parse_ports("443,80,80"), [80, 443])
        self.assertEqual(scope.parse_ports("8100-8098"), [8098, 8099, 8100])
        with self.assertRaises(scope.ScopeError):
            scope.parse_ports("0")
        with self.assertRaises(scope.ScopeError):
            scope.parse_ports("first")
        with self.assertRaises(scope.ScopeError):
            scope.parse_ports("1-999999")

    def test_ipv6_literal_needs_brackets(self):
        host, ports = scope.split_host_ports("[::1]:8080")
        self.assertEqual(host, "::1")
        self.assertEqual(ports, "8080")


# ---------------------------------------------------------------- 探测原语

class PortScanTestCase(unittest.TestCase):

    def test_open_and_closed_are_distinguished(self):
        fixture = FakeHttp()
        self.addCleanup(fixture.stop)
        opened = probe.scan_port("127.0.0.1", fixture.port, 1.0, 0.5)
        self.assertEqual(opened["state"], "open")
        self.assertGreaterEqual(opened["latencyMs"], 0)

        # 一个几乎不可能被占用的高位端口：要么收到 RST（closed），要么被丢弃
        # （filtered）。两者都必须与 open 区分开——把"没开"和"主机不在"混为一谈，
        # 就丢掉了"这个网段到底有没有活主机"这个结论。
        dead = probe.scan_port("127.0.0.1", 9, 0.6, 0.3)
        self.assertIn(dead["state"], ("closed", "filtered"))

    def test_http_fingerprint_reads_status_server_and_title(self):
        fixture = FakeHttp()
        self.addCleanup(fixture.stop)
        info = probe.http_fingerprint("127.0.0.1", fixture.port, 2.0)
        self.assertTrue(info["ok"])
        self.assertEqual(info["status"], 200)
        self.assertEqual(info["server"], "FakeServer/1.0")
        self.assertEqual(info["title"], "Probe Fixture")

    def test_http_fingerprint_on_non_http_service_is_not_ok(self):
        redis = FakeRedis()
        self.addCleanup(redis.stop)
        info = probe.http_fingerprint("127.0.0.1", redis.port, 2.0)
        self.assertFalse(info["ok"])

    def test_redis_without_auth_is_detected(self):
        redis = FakeRedis()
        self.addCleanup(redis.stop)
        info = probe.redis_probe("127.0.0.1", redis.port, 1.5)
        self.assertTrue(info["checked"])
        self.assertTrue(info["unauthorized"])
        self.assertEqual(info.get("version"), "7.2.4")

    def test_redis_with_auth_is_not_flagged(self):
        redis = FakeRedis(require_auth=True)
        self.addCleanup(redis.stop)
        info = probe.redis_probe("127.0.0.1", redis.port, 1.5)
        self.assertTrue(info["checked"])
        self.assertFalse(info["unauthorized"])

    def test_docker_api_is_detected_by_version_json(self):
        fixture = FakeHttp({"/version": (200, "application/json",
                                         b'{"ApiVersion":"1.41","Version":"24.0.7"}')})
        self.addCleanup(fixture.stop)
        info = probe.docker_probe("127.0.0.1", fixture.port, 2.0)
        self.assertTrue(info["exposed"])

    def test_docker_api_without_json_is_not_flagged(self):
        fixture = FakeHttp({"/version": (200, "text/html", b"<html>nope</html>")})
        self.addCleanup(fixture.stop)
        info = probe.docker_probe("127.0.0.1", fixture.port, 2.0)
        self.assertTrue(info["checked"])
        self.assertFalse(bool(info.get("exposed")))

    def test_binary_banner_is_sanitised(self):
        text = probe._clean_text(b"\x00\x01SSH-2.0-OpenSSH_9.0\r\n\xff")
        self.assertEqual(text, "SSH-2.0-OpenSSH_9.0")


# ---------------------------------------------------------------- 端到端探测

class ProbeRunTestCase(unittest.TestCase):

    def test_run_reports_alive_host_open_port_and_fingerprint(self):
        fixture = FakeHttp()
        self.addCleanup(fixture.stop)
        result = probe.run("127.0.0.1", str(fixture.port), scopes=LOCAL_SCOPE,
                           limits=FAST_LIMITS)
        self.assertEqual(result["summary"]["alive"], 1)
        self.assertEqual(result["summary"]["openPorts"], 1)
        host = result["hosts"][0]
        self.assertEqual(host["state"], "up")
        entry = host["openPorts"][0]
        self.assertEqual(entry["port"], fixture.port)
        # 非标准端口上的 HTTP 服务也要被认出来：这是"跑在 8080 之外的 web 服务"
        # 唯一能被发现的路径。
        self.assertEqual(entry["service"], "http")
        self.assertEqual((entry.get("http") or {}).get("title"), "Probe Fixture")

    def test_run_marks_unreachable_host_without_inventing_findings(self):
        result = probe.run("127.0.0.1", "9", scopes=LOCAL_SCOPE, limits=FAST_LIMITS)
        self.assertEqual(result["summary"]["openPorts"], 0)
        self.assertEqual(result["findings"], [])

    def test_run_refuses_out_of_scope_target_before_any_packet(self):
        with self.assertRaises(scope.ScopeError):
            probe.run("8.8.8.8", "80", scopes=scope.DEFAULT_SCOPE)
        with self.assertRaises(scope.ScopeError):
            probe.run("127.0.0.1", "80", scopes=[])

    def test_findings_come_only_from_decidable_evidence(self):
        """端口开放不写成漏洞，只有可复现的弱配置才写。"""
        hosts = [{
            "ip": "10.0.0.9",
            "openPorts": [
                {"port": 80, "service": "http", "http": {"status": 200}},
                {"port": 6379, "service": "redis",
                 "auth": {"checked": True, "unauthorized": True, "reply": "+PONG"}},
                {"port": 2375, "service": "docker",
                 "auth": {"checked": True, "exposed": True, "status": 200}},
                {"port": 443, "service": "https",
                 "tls": {"handshake": True, "version": "TLSv1.2",
                         "certVerified": False, "verifyError": "self-signed certificate",
                         "certSha256": "ab" * 32}},
                {"port": 8443, "service": "https-alt",
                 "tls": {"handshake": True, "version": "TLSv1.2",
                         "certVerified": True, "legacyAccepted": True}},
            ],
        }]
        findings = probe.build_findings(hosts)
        titles = sorted(item["title"] for item in findings)
        self.assertEqual(len(findings), 4)
        self.assertIn("Redis 未授权访问", titles)
        self.assertIn("Docker 守护进程 API 未授权访问", titles)
        self.assertIn("TLS 证书未通过校验", titles)
        self.assertIn("TLS 接受已废弃协议版本", titles)
        for item in findings:
            self.assertTrue(item["steps"], "每条交付项都必须有可复现步骤")
        severities = {item["title"]: item["severity"] for item in findings}
        self.assertEqual(severities["Docker 守护进程 API 未授权访问"], "critical")
        self.assertEqual(severities["Redis 未授权访问"], "high")


# ---------------------------------------------------------------- 全端口与超时标定

class PortExpressionTestCase(unittest.TestCase):
    """端口表达式：`all` 是唯一能一次铺开全端口的写法。"""

    def test_all_expands_to_the_whole_range(self):
        ports = scope.parse_ports("all")
        self.assertEqual(len(ports), 65535)
        self.assertEqual((ports[0], ports[-1]), (1, 65535))

    def test_all_and_the_explicit_range_are_equivalent(self):
        """关键字只是让意图可读，规则上不享有特权。"""
        self.assertEqual(scope.parse_ports("all"), scope.parse_ports("1-65535"))
        # 关键字与数字混写仍是全端口：多写一个 80 不改变意图
        self.assertEqual(len(scope.parse_ports("all,80")), 65535)

    def test_a_range_wider_than_the_port_space_is_refused(self):
        """超出端口空间的范围要在展开前被拒，而不是先建一个百万级集合。"""
        with self.assertRaises(scope.ScopeError):
            scope.parse_ports("1-999999")

    def test_a_mid_sized_range_is_expressed_directly(self):
        """4096–65535 之间的规模必须能直接写出来（旧规则下无处表达）。"""
        self.assertEqual(len(scope.parse_ports("1-10000")), 10000)

    def test_compressed_expression_round_trips(self):
        """压缩与解析是一对，任何一侧漂移都会让节点扫的端口不等于控制面判定的。"""
        for text in ("all", "common", "80,443,8000-8100", "1-10000", "80"):
            ports = scope.parse_ports(text)
            self.assertEqual(scope.parse_ports(scope.compress_ports(ports)),
                             ports)

    def test_full_port_set_is_delivered_as_a_short_expression(self):
        """全端口下发的是 8 个字符，不是六万多个数字。"""
        self.assertEqual(scope.compress_ports(scope.parse_ports("all")),
                         "1-65535")

    def test_common_reaches_management_ports_the_default_set_misses(self):
        ports = scope.parse_ports("common")
        self.assertGreater(len(ports), len(scope.DEFAULT_PORTS))
        for port in (2375, 5985, 6443, 9092, 11211):
            self.assertIn(port, ports)

    def test_full_scan_budget_blocks_a_network_times_every_port(self):
        """`/24` × 全端口 = 1660 万次连接，必须在展开阶段被拒。"""
        with self.assertRaises(scope.ScopeError) as caught:
            scope.plan("10.0.0.0/24", "all", scope.ANY_SCOPE)
        self.assertIn("超过单次预算", str(caught.exception))

    def test_full_scan_budget_admits_a_single_host(self):
        spec = scope.plan("10.0.0.5", "all", scope.ANY_SCOPE)
        self.assertEqual(spec["probeCount"], 65535)


class ConnectTimeoutCalibrationTestCase(unittest.TestCase):
    """超时按目标实测标定——写死一个值必然在另一类网络里判错端口状态。"""

    def test_calibration_follows_the_target_not_a_constant(self):
        timeout, message = probe.calibrate_connect_timeout("127.0.0.1", 4.0)
        # 本机对未监听端口的拒绝要迟延约 2s；标定值必须跟上，否则超时定小了
        # 会把每一个关闭端口都记成 filtered。
        self.assertGreater(timeout, float(probe.LIMITS["connectTimeoutS"]))
        self.assertIn("实测标定", message)

    def test_calibration_never_exceeds_the_ceiling(self):
        timeout, _ = probe.calibrate_connect_timeout("127.0.0.1", 1.2)
        self.assertLessEqual(timeout, 1.2)

    def test_closed_ports_are_marked_closed_after_calibration(self):
        """扫一批必然关闭的端口：它们必须被记成 closed，而不是一片 filtered。

        这是本模块最容易出错的一处：`closedCount` 若恒为 0，"主机活着"就只剩
        "有开放端口"这一条依据，而 filtered 与 closed 的区分正是为了回答
        "这个网段里到底有没有活主机"。
        """
        result = probe.run("127.0.0.1", "60100-60110", scopes=LOCAL_SCOPE,
                           limits=dict(FAST_LIMITS))
        host = result["hosts"][0]
        self.assertEqual(host["closedCount"], 11)
        self.assertEqual(host["filteredCount"], 0)
        self.assertEqual(host["state"], "up")

    def test_limits_snapshot_records_the_values_actually_used(self):
        """报告里的 limits 必须与本次扫描真实用的参数一致。"""
        result = probe.run("127.0.0.1", "60150-60155", scopes=LOCAL_SCOPE,
                           limits=dict(FAST_LIMITS))
        self.assertGreater(result["limits"]["connectTimeoutS"],
                           FAST_LIMITS["connectTimeoutS"])


class WideScanTestCase(unittest.TestCase):
    """大规模端口扫描：一次任务跑完，不靠分片拼接。"""

    def test_wide_scan_raises_concurrency_and_finishes(self):
        result = probe.run("127.0.0.1", "40000-47999", scopes=LOCAL_SCOPE,
                           limits=dict(FAST_LIMITS, calibrate=False))
        self.assertEqual(result["degraded"], [])
        self.assertEqual(result["limits"]["concurrency"],
                         probe.LIMITS["fullPortsConcurrency"])
        scanned = result["hosts"][0]
        self.assertEqual(scanned["closedCount"] + scanned["filteredCount"]
                         + len(scanned["openPorts"]), 8000)

    def test_narrow_scan_keeps_the_modest_concurrency(self):
        result = probe.run("127.0.0.1", "60180-60184", scopes=LOCAL_SCOPE,
                           limits=dict(FAST_LIMITS, calibrate=False))
        self.assertEqual(result["limits"]["concurrency"],
                         FAST_LIMITS["concurrency"])


class TlsProbeFailureTestCase(unittest.TestCase):
    """不依赖 openssl 的那条路径：明文服务上的 TLS 握手必须判为失败并留下原因。

    读到证书、判定是否可信需要一张真实证书，本机没有 openssl 时只能跳过；但
    "握手失败"这条路径在任何环境都能验证，而它恰恰是误报的入口——把明文服务的
    乱码当成 TLS 响应，就会给一堆 HTTP 端口报出"证书问题"。
    """

    def test_plain_http_service_has_no_tls_handshake(self):
        fixture = FakeHttp()
        self.addCleanup(fixture.stop)
        info = probe.tls_probe("127.0.0.1", fixture.port, 1.5, "127.0.0.1")
        self.assertFalse(info["handshake"])
        self.assertTrue(info.get("error"), "握手失败必须留下原因")


@unittest.skipUnless(pki.openssl_available(), "本机没有 openssl，跳过 TLS 探测")
class TlsProbeTestCase(unittest.TestCase):

    def test_self_signed_certificate_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            pki.init_ca(tmp, "probe-ca")
            issued = pki.issue(tmp, "probe-server", san="127.0.0.1")
            fixture = TlsFixture(issued["cert"], issued["key"])
            self.addCleanup(fixture.stop)
            info = probe.tls_probe("127.0.0.1", fixture.port, 3.0, "127.0.0.1")
            self.assertTrue(info["handshake"], info.get("error"))
            self.assertTrue(info["version"].startswith("TLSv"))
            self.assertFalse(info["certVerified"])
            self.assertTrue(info.get("verifyError"), "必须留下无法校验的原因")
            self.assertEqual(len(info["certSha256"]), 64)
            # 探针自身不出假阳性：现代 OpenSSL 默认不接受 TLS 1.0/1.1
            self.assertFalse(info["legacyAccepted"])


# ---------------------------------------------------------------- 链路翻译

class ProbeChainTestCase(unittest.TestCase):

    def setUp(self):
        self.directory, self.store = support.temporary_store("probe-chain.db")
        self.addCleanup(self.store.close)
        self.addCleanup(self.directory.cleanup)
        self.engagement = self.store.create_engagement("10.0.0.0/24", "暴露面测绘", "AUTH-1")

    def sample(self):
        return {
            "run_id": "probe-test", "scope": ["10.0.0.0/8"], "limits": {},
            "elapsed_ms": 1234, "degraded": ["总时长达到上限，2 个端口探测未完成"],
            "target": {"name": "10.0.0.0/24", "kind": "network", "rootType": "ip",
                       "root": "10.0.0.0/24", "hosts": 2, "ports": 2,
                       "portsScanned": [80, 6379], "resolved": {}},
            "hosts": [
                {"ip": "10.0.0.5", "hostname": "", "state": "up", "closedCount": 1,
                 "filteredCount": 0,
                 "openPorts": [{"port": 80, "state": "open", "service": "http",
                                "banner": "", "latencyMs": 3,
                                "http": {"status": 200, "server": "nginx",
                                         "title": "内网门户"}}]},
                {"ip": "10.0.0.6", "hostname": "", "state": "up", "closedCount": 0,
                 "filteredCount": 1,
                 "openPorts": [{"port": 6379, "state": "open", "service": "redis",
                                "banner": "", "latencyMs": 2,
                                "auth": {"checked": True, "unauthorized": True,
                                         "reply": "+PONG", "version": "7.2.4"}}]},
            ],
            "findings": probe.build_findings([
                {"ip": "10.0.0.5", "openPorts": []},
                {"ip": "10.0.0.6", "openPorts": [
                    {"port": 6379, "service": "redis",
                     "auth": {"checked": True, "unauthorized": True, "reply": "+PONG"}}]},
            ]),
            "summary": {"hosts": 2, "alive": 2, "openPorts": 2, "findings": 1},
        }

    def test_specs_are_ordered_and_link_types_are_correct(self):
        specs = engine.build_probe_chain_specs(self.sample())
        kinds = [spec["kind"] for spec in specs]
        self.assertEqual(kinds[0], "asset")                       # 根资产先写
        self.assertIn("intent", kinds)
        self.assertIn("finding", kinds)
        self.assertLess(kinds.index("intent"), kinds.index("fact"))

    def test_write_chain_creates_hierarchy_and_edges(self):
        result = self.sample()
        written = engine.write_chain(self.store, self.engagement["id"],
                                     engine.build_probe_chain_specs(result))
        self.assertEqual(written["asset"], 5)         # 根 + 2 主机 + 2 服务
        self.assertEqual(written["finding"], 1)

        view = self.store.view(self.engagement["id"], 200)
        # view() 返回的是摊平后的节点（payload 字段已提到顶层）
        values = sorted((node.get("value"), node.get("type"))
                        for node in view["nodes"]["assets"])
        self.assertIn(("10.0.0.5", "ip"), values)
        self.assertIn(("6379/tcp redis", "service"), values)
        parent_edges = [edge for edge in view["edges"] if edge["kind"] == "parent"]
        self.assertEqual(len(parent_edges), 4)       # 2 主机挂根 + 2 服务挂主机
        findings = self.store.findings(self.engagement["id"])
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0]["affectedAssetId"], "交付项必须指回具体服务资产")

    def test_fact_surfaces_degraded_notes(self):
        specs = engine.build_probe_chain_specs(self.sample())
        details = " ".join(spec["payload"].get("detail", "")
                           for spec in specs if spec["kind"] == "fact")
        self.assertIn("探测受限", details)


# ---------------------------------------------------------------- 任务与接口

class ProbeJobTestCase(unittest.TestCase):

    def setUp(self):
        self.directory, self.store = support.temporary_store("probe-job.db")
        self.httpd, self.client = support.start(self.store)
        self.addCleanup(support.stop, self.httpd, self.store, self.directory)

    def test_full_port_job_payload_is_a_compact_expression(self):
        """全端口任务的载荷是 "1-65535"，而不是六万多个数字。

        载荷要经 HTTP 下发、存进任务记录、再被节点解析一遍。展开成列表会让一条
        任务记录涨到四十万字符，而端口信息本身只有 8 个字符。`portCount` 随行
        下发，节点解析后比对，用来发现两侧解析版本漂移。
        """
        status, payload = self.client.post(
            "/api/v1/jobs",
            {"kind": "probe", "target": "10.0.0.5", "ports": "all"})
        self.assertEqual(status, 201)
        self.assertEqual(payload["ports"], 65535)
        stored = self.store.jobs()[0]["payload"]
        self.assertEqual(stored["ports"], "1-65535")
        self.assertEqual(stored["portCount"], 65535)
        self.assertLess(len(str(stored)), 20000, "载荷不该随端口数线性膨胀")

    def test_inline_port_survives_into_the_payload(self):
        """`host:80` 的内联端口必须随载荷下发，否则节点会退回默认端口集合。"""
        status, _ = self.client.post("/api/v1/jobs",
                                     {"kind": "probe", "target": "10.0.0.5:8080"})
        self.assertEqual(status, 201)
        stored = self.store.jobs()[0]["payload"]
        self.assertEqual(stored["hostSpec"], "10.0.0.5")
        self.assertEqual(stored["ports"], "8080")

    def test_probe_job_rejected_outside_scope(self):
        status, payload = self.client.post("/api/v1/jobs",
                                           {"kind": "probe", "target": "8.8.8.8"})
        self.assertEqual(status, 403)
        self.assertIn("作用域", payload["error"])
        self.assertEqual(self.store.jobs(), [], "被拒绝的任务不能留在队列里")

    def test_probe_job_unknown_kind_is_rejected(self):
        # 用**真正不认识**的类型。这里原来是 `exploit`，而它后来成了合法类型
        # （`JOB_KINDS`），于是请求走到了侵入式准入门、返回 403 而不是 400。
        # 换一个不存在的名字，这条用例才还在测它本来要测的东西：
        # "类型不认识" 与 "类型认识但没授权" 是两件必须分得开的事。
        status, payload = self.client.post("/api/v1/jobs",
                                          {"kind": "teleport", "target": "127.0.0.1"})
        self.assertEqual(status, 400)
        self.assertIn("任务类型", payload["error"])

    def test_known_but_unauthorized_intrusive_kind_is_403_not_400(self):
        """合法但未授权的侵入类型必须是 403，不能报"类型不支持"。

        两者的处置完全不同：400 会让人去改任务类型，而真正要做的是去部署侧开
        `--allow-intrusive`。报错报错方向，是最费时间的一种错。
        """
        status, payload = self.client.post("/api/v1/jobs",
                                          {"kind": "exploit", "target": "127.0.0.1"})
        self.assertEqual(status, 403)
        self.assertIn("侵入", payload["error"])
        self.assertEqual(self.store.jobs(), [], "被拒绝的任务不能留在队列里")

    def test_probe_job_carries_scope_snapshot(self):
        status, payload = self.client.post(
            "/api/v1/jobs", {"kind": "probe", "target": "127.0.0.1", "ports": "80,443"})
        self.assertEqual(status, 201)
        self.assertEqual(payload["kindLabel"], "网络探测")
        self.assertEqual(payload["hosts"], 1)
        self.assertEqual(payload["ports"], 2)
        job = self.store.get_job(payload["jobId"])
        self.assertEqual(job["kind"], "probe")
        self.assertEqual(job["payload"]["scope"], scope.DEFAULT_SCOPE)
        self.assertEqual(job["payload"]["hostSpec"], "127.0.0.1")

    def test_probe_job_runs_end_to_end_through_local_executor(self):
        fixture = FakeHttp()
        self.addCleanup(fixture.stop)
        status, payload = self.client.post(
            "/api/v1/jobs", {"kind": "probe", "target": "127.0.0.1",
                             "ports": str(fixture.port)})
        self.assertEqual(status, 201)
        executor = agent.LocalExecutor(self.store, name="probe-local", verbose=False)
        self.assertTrue(executor.run_once())

        job = self.store.get_job(payload["jobId"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["summary"]["openPorts"], 1)
        self.assertEqual(job["summary"]["kind"], "probe")

        meta = self.store.get_engagement_meta(payload["engagementId"])
        # **根资产与"存活主机"在这台机器上是同一条。** 目标是单个 IP 时，
        # `build_chain_specs` 先写一条根资产（type=ip，value=目标），再为存活主机
        # 写一条（type=ip，value 还是那个 IP，parent 指向根）——两条 (type, value)
        # 完全相同。`store.find_asset` 按身份去重之后它们合成一条，所以这里数出来的
        # 资产是 2（1 个 ip + 1 个 service），不是 3。
        #
        # 断言的重点因此不是"总数不少于 3"，而是**那一条 ip 只有一份**：报告里
        # 63 → 67 → 68 那种单调增长就是从这两条开始的。
        self.assertEqual(2, meta["counts"]["assets"])
        ips = [n for n in self.store.list_nodes(payload["engagementId"], "asset")
               if n["payload"].get("type") == "ip"]
        self.assertEqual(1, len(ips), "同一个 IP 登记了 %d 条" % len(ips))
        self.assertGreaterEqual(meta["counts"]["facts"], 3)
        self.assertTrue(self.store.report_markdown(payload["engagementId"]))

    def test_probe_does_not_require_a_model_profile(self):
        """探测不使用大模型，因此未配置模型时照样能建任务、照样能跑完。"""
        self.assertIsNone(self.store.active_profile())
        status, payload = self.client.post(
            "/api/v1/jobs", {"kind": "probe", "target": "127.0.0.1", "ports": "80"})
        self.assertEqual(status, 201)
        self.assertEqual(payload["model"], "确定性探测")

    def test_scope_endpoint_exposes_limits(self):
        status, payload = self.client.get("/api/v1/scope")
        self.assertEqual(status, 200)
        self.assertEqual(payload["scope"], scope.DEFAULT_SCOPE)
        self.assertFalse(payload["unrestricted"])
        # 任务类型目录来自 `server.JOB_KINDS`，四种都在。原来只断言 scan/probe——
        # 那是侵入类型（exploit / creds）加进来之前的契约。
        kinds = [item["kind"] for item in payload["kinds"]]
        self.assertEqual(kinds, list(server.JOB_KINDS))
        self.assertIn("probe", kinds)


class ClaimCapabilityTestCase(unittest.TestCase):
    """节点只领自己声明支持的任务类型。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("probe-claim.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)

    def test_scan_only_node_never_claims_probe_job(self):
        probe_job = self.store.create_job("probe", "127.0.0.1", "")
        scan_job = self.store.create_job("scan", "/srv/app", "")
        scan_only = self.store.register_executor("legacy-scan", ["scan"])
        claimed = self.store.claim_job(scan_only["executorId"])
        self.assertEqual(claimed["id"], scan_job["id"])

        probe_node = self.store.register_executor("probe-node", ["scan", "probe"])
        claimed_next = self.store.claim_job(probe_node["executorId"])
        self.assertEqual(claimed_next["id"], probe_job["id"])

    def test_node_without_declared_capabilities_still_gets_work(self):
        job = self.store.create_job("scan", "/srv/app", "")
        node = self.store.register_executor("bare", [])
        claimed = self.store.claim_job(node["executorId"])
        self.assertEqual(claimed["id"], job["id"])


# ---------------------------------------------------------------- 对话工具

class ProbeChatToolTestCase(unittest.TestCase):
    """对话下发探测：断言的是副作用——真实的 job 记录与真实的拒绝。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("probe-chat.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.plane = ControlPlane(self.store, WEB_DIR)
        self.models = []
        self.addCleanup(self._stop_models)

    def _stop_models(self):
        for model in self.models:
            model.stop()

    def profile(self, model):
        return {"provider": "openai_compat", "baseUrl": model.base_url, "model": "m",
                "temperature": 0.2, "maxTokens": 1024, "timeoutS": 30}

    def respond(self, first):
        state = {"n": 0}

        def responder(record):
            state["n"] += 1
            if state["n"] == 1:
                return first
            return text_reply("openai_compat", "已下发，稍后用 list_jobs 查看。")

        return responder

    def test_start_probe_creates_a_real_job(self):
        model = ScriptedModel(self.respond(
            tool_reply("openai_compat", "start_probe",
                       {"target": "127.0.0.1", "ports": "80", "objective": "本机自检"})))
        self.models.append(model)
        result = chat.run_turn(self.plane, self.profile(model), "k",
                               [{"role": "user", "text": "探测一下 127.0.0.1 的 80 端口"}])
        self.assertTrue(result["ok"], result.get("message"))
        trace = result["trace"]
        self.assertEqual(trace[0]["tool"], "start_probe")
        self.assertTrue(trace[0]["ok"], trace[0]["summary"])
        jobs = self.store.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["kind"], "probe")
        self.assertEqual(jobs[0]["target"], "127.0.0.1")

    def test_out_of_scope_probe_is_refused_and_reported_to_the_model(self):
        model = ScriptedModel(self.respond(
            tool_reply("openai_compat", "start_probe", {"target": "8.8.8.8", "ports": "80"})))
        self.models.append(model)
        result = chat.run_turn(self.plane, self.profile(model), "k",
                               [{"role": "user", "text": "扫一下 8.8.8.8"}])
        trace = result["trace"][0]
        self.assertFalse(trace["ok"])
        self.assertIn("作用域", trace["summary"])
        self.assertEqual(self.store.jobs(), [])

    def test_system_prompt_carries_the_scope(self):
        """作用域写进提示词，模型一开始就知道边界，不必靠试错发现。"""
        prompt = chat.build_system_prompt(self.plane.probe_scope)
        self.assertIn("127.0.0.0/8", prompt)
        self.assertIn("start_probe", prompt)

    def test_tool_catalog_advertises_probe(self):
        catalog = {item["name"]: item for item in chat.tool_catalog()}
        self.assertIn("start_probe", catalog)
        self.assertIn("target", catalog["start_probe"]["parameters"])
        self.assertIn("ports", catalog["start_probe"]["parameters"])


class NetScanChatToolTestCase(unittest.TestCase):
    """本机扫描工具：断言的同样是副作用——真实的监听端口被扫到、越界在发包前被拒。

    为什么值得单独测：这条通道的存在理由就是"没有 Npcap 也能扫"，而 Npcap 的缺失
    是**机器属性**，测试机上装没装都不该决定这条通道是否可用。所以这里不用 mock
    顶替 socket，而是真绑一个监听端口让扫描去扫——mock 只能证明"我调用了 connect"，
    证明不了"那个端口被判成 open"。
    """

    def setUp(self):
        self.directory, self.store = support.temporary_store("netscan-chat.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.plane = ControlPlane(self.store, WEB_DIR)
        # 真实的监听套接字：绑定端口 0 让内核挑一个空闲端口，避免和本机服务撞车。
        self.sink = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sink.bind(("127.0.0.1", 0))
        self.sink.listen(8)
        self.addCleanup(self.sink.close)
        self.port = self.sink.getsockname()[1]

    def call(self, **args):
        return chat.HANDLERS["netscan"](self.plane, args, "", {})

    def test_catalog_advertises_the_local_scan(self):
        catalog = {item["name"]: item for item in chat.tool_catalog()}
        self.assertIn("netscan", catalog)
        # 目录里 parameters 是参数名列表（界面只需要知道"能填什么"）。
        self.assertEqual(sorted(catalog["netscan"]["parameters"]),
                         ["banner", "mode", "ports", "target", "timeout"])
        self.assertEqual(catalog["netscan"]["level"], "probe")

    def test_finds_a_really_listening_port(self):
        result = self.call(target="127.0.0.1", ports=str(self.port), timeout=0.5)
        hosts = [row for row in result["data"]["hosts"] if row["alive"]]
        self.assertTrue(hosts, "回环地址必须被判定为存活")
        open_ports = [item["port"] for item in hosts[0]["ports"]
                      if item["state"] == "open"]
        self.assertEqual(open_ports, [self.port])
        self.assertEqual(result["data"]["summary"]["openPorts"], 1)

    def test_discover_mode_does_not_scan_ports(self):
        result = self.call(target="127.0.0.1", mode="discover")
        host = result["data"]["hosts"][0]
        self.assertTrue(host["alive"])
        self.assertEqual(host["ports"], [])

    def test_out_of_scope_target_is_refused_before_any_packet(self):
        with self.assertRaises(ValueError) as caught:
            self.call(target="8.8.8.8", ports="80")
        self.assertIn("作用域", str(caught.exception))
        self.assertEqual(self.store.jobs(), [])

    def test_read_only_policy_does_not_see_it(self):
        readonly = [item["name"] for item in chat.tool_catalog("readonly")]
        self.assertNotIn("netscan", readonly)
        probe_names = [item["name"] for item in chat.tool_catalog("probe")]
        self.assertIn("netscan", probe_names)


# ---------------------------------------------------------------- 部署默认值

def _deploy_parser():
    """加载 deploy/start.py 的命令行契约。

    deploy/ 不是包（没有 __init__.py），所以按路径加载——测到的必须是部署入口
    真正在用的那个 parser。在测试里另抄一份默认值等于没测：默认值改了，抄来的
    那份照样是绿的。
    """
    path = os.path.join(ROOT, "deploy", "start.py")
    spec = importlib.util.spec_from_file_location("deploy_start_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_parser()


class DeploymentScopeTestCase(unittest.TestCase):
    """部署策略（默认不设限）与库层机制（默认收窄）的分界，必须有人守着。

    只测"any 能用"证明不了默认值是什么；只测"默认值是什么"证明不了这条路真的通。
    所以两边都测：默认值、可收窄、可关闭，以及不设限时公网目标确实进得了队列。
    """

    def _store(self):
        directory, store = support.temporary_store("probe-deploy.db")
        self.addCleanup(store.close)
        self.addCleanup(directory.cleanup)
        return store

    def _served(self, **kwargs):
        directory, store = support.temporary_store("probe-deploy-api.db")
        httpd, client = support.start(store, **kwargs)
        self.addCleanup(support.stop, httpd, store, directory)
        return store, client

    def test_deploy_entry_defaults_to_unrestricted(self):
        args = _deploy_parser().parse_args([])
        self.assertEqual(set(scope.parse_scope_spec(args.probe_scope)),
                         set(scope.ANY_SCOPE))

    def test_deploy_entry_can_narrow_and_disable(self):
        parser = _deploy_parser()
        narrow = scope.parse_scope_spec(
            parser.parse_args(["--probe-scope", "203.0.113.0/24"]).probe_scope)
        self.assertEqual(narrow, ["203.0.113.0/24"])
        off = scope.parse_scope_spec(
            parser.parse_args(["--probe-scope", "none"]).probe_scope)
        self.assertEqual(off, [])

    def test_cli_serve_defaults_to_unrestricted(self):
        args = cli.build_parser().parse_args(["serve"])
        self.assertEqual(set(scope.parse_scope_spec(args.probe_scope)),
                         set(scope.ANY_SCOPE))

    def test_blank_spec_means_default_not_disabled(self):
        """空参数 ≠ 关闭探测：后者必须有人写下 `none`。"""
        for blank in ("", "   ", None):
            self.assertEqual(set(scope.parse_scope_spec(blank)),
                             set(scope.ANY_SCOPE))

    def test_library_default_stays_narrow(self):
        """库层默认值不动：新写的代码与测试不该悄悄继承一个全开的作用域。"""
        plane = ControlPlane(self._store(), WEB_DIR)
        self.assertEqual(plane.probe_scope, scope.DEFAULT_SCOPE)

    def test_unrestricted_plane_lets_a_public_target_into_the_queue(self):
        store, client = self._served(probe_scope=scope.ANY_SCOPE)
        status, payload = client.post("/api/v1/jobs",
                                      {"kind": "probe", "target": "203.0.113.9",
                                       "ports": "80"})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["hosts"], 1)
        job = store.get_job(payload["jobId"])
        self.assertEqual(job["payload"]["scope"], scope.ANY_SCOPE)

    def test_none_scope_refuses_every_target(self):
        store, client = self._served(probe_scope=scope.parse_scope_spec("none"))
        status, payload = client.post("/api/v1/jobs",
                                      {"kind": "probe", "target": "127.0.0.1",
                                       "ports": "80"})
        self.assertEqual(status, 403)
        self.assertIn("fail-closed", payload["error"])
        self.assertEqual(store.jobs(), [])


if __name__ == "__main__":
    unittest.main()
