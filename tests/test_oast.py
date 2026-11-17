# -*- coding: utf-8 -*-
"""带外回连（oast）的验证。

这里**没有一处 mock socket**，因为本模块要证明的事情只有真包能证明：

* "端口 0 会被换成真实端口"——只有真的 bind 过才知道内核给了哪个端口；
* "目标回连时我们收到了"——只有真的发一个 HTTP 请求、真的收一个 UDP DNS
  查询，才知道监听器解不解得开目标会发来的那种包；
* "DNS 应答里真的有一条 A 记录"——本测试自己按线格式把应答拆开读，
  不断言"没报错"。收到一个空应答和收到一个正确应答，在不解析的时候长得
  一模一样。

三类用例的分工：

* `RegistryTestCase` 对着注册表本体，钉住溢出、过期、并发这些**与网络无关**
  的语义（并发那一条仍然用真连接，因为它要验的是锁，不是模型）；
* `ListenerHttpTestCase` / `ListenerDnsTestCase` 对着真实监听器，钉住"目标侧
  发什么、我们记成什么"；
* `ListenerLifecycleTestCase` 钉住反复启停——测试套件里监听器会被起停几百次，
  泄漏一个线程就会让后面某条用例随机失败。
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

from pentestd import oast                                              # noqa: E402

TOKEN_RE = r"^[0-9a-f]{32}$"


# ---------------------------------------------------------------- 测试夹具

def pack_name(name: str) -> bytes:
    """域名 → DNS 线格式（测试自己拼包，不用被测代码的任何函数）。"""
    payload = b""
    for label in name.split("."):
        payload += bytes([len(label)]) + label.encode("ascii")
    return payload + b"\x00"


def build_query(name: str, qtype: int = 1, query_id: int = 0x1234) -> bytes:
    header = struct.pack(">HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    return header + pack_name(name) + struct.pack(">HH", qtype, 1)


def send_http(port: int, raw: bytes, read_reply: bool = False,
              timeout: float = 5.0):
    """真发一个原始 HTTP 请求。返回响应字节（`read_reply` 为真时）。"""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(raw)
        if not read_reply:
            return b""
        sock.settimeout(timeout)
        chunks = []
        while True:
            try:
                data = sock.recv(4096)
            except (socket.timeout, ConnectionResetError):
                break
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)


def http_get(port: int, path: str, token: str = "", method: str = "GET",
             headers=None, body: bytes = b"", read_reply: bool = True,
             timeout: float = 5.0) -> bytes:
    if token:
        headers = dict(headers or {})
        headers.setdefault("X-Oast-Token", token)
    head = ["%s %s HTTP/1.1" % (method, path), "Host: 127.0.0.1",
            "Connection: close"]
    for key, value in (headers or {}).items():
        head.append("%s: %s" % (key, value))
    if body:
        head.append("Content-Length: %d" % len(body))
    raw = ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body
    return send_http(port, raw, read_reply=read_reply, timeout=timeout)


def response_body(raw: bytes) -> bytes:
    """从原始响应里取正文，用于确认回包本身是完整的（不是半截就断了）。"""
    head, _, body = raw.partition(b"\r\n\r\n")
    return body


def udp_exchange(port: int, payload: bytes, timeout: float = 3.0) -> bytes:
    """向监听器发一个 UDP 包并等应答。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(payload, ("127.0.0.1", port))
        data, _peer = sock.recvfrom(4096)
        return data


def dns_query(port: int, name: str, qtype: int = 1) -> bytes:
    return udp_exchange(port, build_query(name, qtype))


# ---------------------------------------------------------------- 注册表

class RegistryTestCase(unittest.TestCase):
    """注册表：与网络无关的语义，以及它的线程安全。"""

    def test_tokens_are_32_hex_and_unique(self):
        """token 是 32 位十六进制，且**不可预测**（不是计数器、不随 label 变）。

        可预测的 token 让人能拿别人的 token 回连，伪造一次没发生过的利用——
        这是本模块唯一一个"猜中就等于伪造证据"的地方。
        """
        import re
        tokens = [oast.new_token("ssrf") for _ in range(64)]
        for token in tokens:
            self.assertRegex(token, TOKEN_RE,
                             "token 必须是 32 位十六进制，否则目标侧的 URL 会带上"
                             "奇怪的字符，而记录里的 token 再也不是原值")
        self.assertEqual(len(set(tokens)), len(tokens), "token 必须全局唯一")

        labelled = [oast.new_token("rce") for _ in range(64)]
        self.assertEqual(len(set(labelled) & set(tokens)), 0,
                         "不同 label 之间不该有交集；token 与 label 无关")
        self.assertEqual(len(set(labelled)), len(labelled))
        # label 只是备注：它不该出现在 token 里，否则同用途的 token 会有共同前缀。
        self.assertNotIn("rce", "".join(labelled))

    def test_token_url_prefers_public_host(self):
        """`0.0.0.0` 绝不能出现在发给目标的 URL 里。

        绑 0.0.0.0 才能收到任意网卡的包，而 0.0.0.0 发给目标是一条无效指令
        （目标会去连它自己）。这两个值混成一个，症状是"收到了回连，可是回连
        来自目标自己"——极难归因。
        """
        endpoint = {"httpHost": "0.0.0.0", "httpPort": 8080,
                    "publicHost": "10.1.2.3", "domain": "oast.lab"}
        self.assertEqual(oast.token_url(endpoint, "abc"),
                         "http://10.1.2.3:8080/oast/abc")
        fallback = {"httpHost": "127.0.0.1", "httpPort": 9000}
        self.assertEqual(oast.token_url(fallback, "abc"),
                         "http://127.0.0.1:9000/oast/abc",
                         "没有 publicHost 时退回 httpHost")
        self.assertEqual(oast.token_host(endpoint, "abc"), "abc.oast.lab")
        self.assertEqual(oast.token_host({}, "abc"), "abc.oast.local",
                         "domain 缺省是 oast.local")

    def test_wildcard_binding_address_is_not_a_target_address(self):
        """`token_url()` 的兜底是文档化的；**用不用它**要判得出来。

        `0.0.0.0` / `::` / `*` 只在 `bind()` 里有意义。把它发给目标，目标只会
        去连自己（或直接失败），两种结果都没有回连到这里——而结果里会如实写下
        "窗口内未观测到回连"，读起来像一句关于**目标**的结论。
        """
        for host in ("0.0.0.0", "::", "[::]", "*", " 0.0.0.0 ", "0.0.0.0:8080"):
            self.assertTrue(oast.is_wildcard_host(host), repr(host))
        for host in ("127.0.0.1", "10.1.2.3", "oast.lab", "", None):
            self.assertFalse(oast.is_wildcard_host(host), repr(host))

    def test_target_url_usable_only_rejects_wildcards(self):
        """只拦通配这一种。内网 IP、域名到不到得了是拓扑问题，这里不猜。

        猜"不可达"会让一次同网段目标的 SSRF 验证被无故跳过；猜"可达"会让一次
        必然收不到回连的验证等满窗口。两种都不该由这个函数决定。
        """
        self.assertFalse(oast.target_url_is_usable({"httpHost": "0.0.0.0",
                                                    "httpPort": 8080}))
        self.assertTrue(oast.target_url_is_usable({"httpHost": "0.0.0.0",
                                                   "httpPort": 8080,
                                                   "publicHost": "10.1.2.3"}),
                        "给了 publicHost 就与绑定地址无关了")
        self.assertTrue(oast.target_url_is_usable({"httpHost": "127.0.0.1",
                                                   "httpPort": 8080}))
        self.assertTrue(oast.target_url_is_usable({}),
                        "什么都没有时 token_url() 退回 127.0.0.1，那是真实地址")

    def test_unregistered_hit_is_recorded_and_labelled(self):
        """未注册的 token 也要记：目标会截断、改写、小写化它。

        这类命中在两个方向上都只是信息、不是噪声——它既证明"目标确实回连了"，
        又说明"某个 token 被改写过"。丢掉它就等于在最该报"连通"的时候报"没有回连"。
        """
        registry = oast.Registry()
        registry.register("aa" * 16, "ssrf")
        hit = registry.record("deadbeef" * 4, "http", "10.0.0.9",
                              raw="GET /oast/deadbeef", path="/oast/deadbeef")
        self.assertEqual(hit["label"], "unregistered",
                         "未知 token 的命中必须标 unregistered——空 label 在界面上"
                         "和'没填备注'分不出来，而这两件事的处置完全不同")
        self.assertTrue(hit["id"])
        self.assertGreaterEqual(hit["elapsedMs"], 0.0)
        self.assertLess(hit["elapsedMs"], 5000.0, "刚登记就回连，不该是几千毫秒")
        self.assertEqual(len(registry.tokens()), 2, "未注册的 token 也要进目录")

        known = registry.record("aa" * 16, "http", "10.0.0.9")
        self.assertEqual(known["label"], "ssrf", "已登记 token 的命中带自己的 label")

    def test_max_hits_drops_the_oldest(self):
        """满了丢最旧的，不拒新的。目标可能为一个 URL 疯狂重连。

        真正要看的是"最新那条"（以及它和上一条的时间差），而疯狂重连的攻击者
        恰恰最想让最新那条被拒收。
        """
        registry = oast.Registry(max_hits=5)
        for index in range(12):
            registry.record("aa", "http", "10.0.0.9",
                            path="/oast/aa/%d" % index)
        hits = registry.hits("aa")
        self.assertEqual(len(hits), 5, "命中数必须钉在上限上，不能越滚越大")
        paths = [hit["path"] for hit in hits]
        self.assertEqual(paths, ["/oast/aa/%d" % index for index in range(7, 12)],
                         "留下的必须是**最新**的 5 条，且顺序不串")
        self.assertEqual(registry.tokens()[0]["hits"], 5,
                         "token 目录里的命中数要跟着裁掉，否则界面上的计数在说谎")

    def test_purge_removes_only_expired_hits(self):
        """purge 按 ttl 清命中并返回条数；新命中不受影响。

        返回条数不是装饰：工具层靠它判断"证据是不是已经被清掉了"，返回 0 与
        返回 3 的下一步动作完全不同。
        """
        registry = oast.Registry(ttl_s=60)
        now = time.time()
        registry.register("old", "ssrf")
        registry.register("new", "rce")
        stale = registry.record("old", "http", "10.0.0.9")
        registry.record("new", "http", "10.0.0.9")
        registry._hits["old"][0]["receivedAt"] = now - 120     # 让它变老
        self.assertTrue(stale["id"])

        removed = registry.purge(now=now)
        self.assertEqual(removed, 1, "只该清掉那一条过期的")
        self.assertEqual(registry.hits("old"), [], "过期的命中必须消失")
        self.assertEqual(len(registry.hits("new")), 1, "没过期的必须留下")
        self.assertEqual(registry.tokens()[0]["hits"], 0,
                         "过期后被清空的 token 命中数要归零")
        self.assertIn("new", [entry["token"] for entry in registry.tokens()],
                      "token 登记本身不清：清掉会让'没人回连'退化成'没登记过'")

    def test_wait_returns_none_on_timeout_without_spinning(self):
        """超时返回 None，且不会永久阻塞。

        `wait()` 是对话工具在等它。超时不返回，一整轮对话就挂在一个永远不来的
        回连上；返回得比 timeout 慢很多，说明有人在忙等（轮询间隔写错了）。
        """
        registry = oast.Registry()
        registry.register("aa", "ssrf")
        started = time.time()
        self.assertIsNone(registry.wait("aa", 0.3))
        elapsed = time.time() - started
        self.assertGreaterEqual(elapsed, 0.29, "没等够 timeout 就返回了")
        self.assertLess(elapsed, 2.5, "超时后拖这么久说明不是轮询而是忙等")

        registry.record("aa", "http", "10.0.0.9")
        found = registry.wait("aa", 1.0)
        self.assertIsNotNone(found, "命中已经在了，wait 必须立刻拿到")
        self.assertEqual(found["token"], "aa")

    def test_hits_view_is_a_copy(self):
        """`hits()` 交出去的是拷贝：命中还在被 HTTP 线程往同一个字典里追加。

        交原件的话，序列化的同时另一次回连落进来，症状是偶发的
        "dictionary changed size during iteration"——一个只在生产流量下出现的错误。
        """
        registry = oast.Registry()
        registry.register("aa", "ssrf")
        registry.record("aa", "http", "10.0.0.9", path="/oast/aa")
        hit = registry.hits("aa")[0]
        hit["path"] = "被改过的"
        hit["headers"]["x-evil"] = "1"
        again = registry.hits("aa")[0]
        self.assertEqual(again["path"], "/oast/aa", "调用方改返回值不能改到内部记录")
        self.assertNotIn("x-evil", again["headers"], "headers 也要深拷一层")


# ---------------------------------------------------------------- HTTP 面

class ListenerHttpTestCase(unittest.TestCase):
    """HTTP 面：目标真发请求，我们真记下来。"""

    def setUp(self):
        self.registry = oast.Registry()
        self.listener = oast.Listener(self.registry)
        self.endpoint = self.listener.start()
        self.addCleanup(self.listener.stop)
        self.port = self.endpoint["httpPort"]

    def test_endpoint_ports_are_real(self):
        """端口 0 必须被换成内核给的真实端口。

        写 0 会让集成去连端口 0——一个必然失败、错误信息却完全看不出根因的地址。
        """
        self.assertNotEqual(self.endpoint["httpPort"], 0)
        self.assertNotEqual(self.endpoint["dnsPort"], 0)
        self.assertGreater(self.endpoint["httpPort"], 0)
        self.assertGreater(self.endpoint["dnsPort"], 0)
        self.assertEqual(self.endpoint["domain"], "oast.local")
        self.assertTrue(self.endpoint["baseUrl"].startswith("http://127.0.0.1:"))
        # endpoint 里报出来的端口必须真的能连上，否则它只是一串好看的数字。
        with socket.create_connection(("127.0.0.1", self.endpoint["httpPort"]),
                                      timeout=3):
            pass

    def test_http_callback_is_recorded_with_full_request(self):
        token = self.registry.register(oast.new_token("ssrf"), "ssrf")
        raw = http_get(self.port, "/oast/%s/inner/path?x=1" % token, token=token,
                       headers={"User-Agent": "oast-test/1.0"})
        self.assertTrue(raw.startswith(b"HTTP/1.1 "),
                        "监听器必须回一个真的 HTTP 响应，否则目标会当成失败重试")

        hit = self.registry.wait(token, 5.0)
        self.assertIsNotNone(hit, "真发了回连，wait 却拿不到命中")
        self.assertEqual(hit["protocol"], "http")
        self.assertEqual(hit["token"], token)
        self.assertEqual(hit["method"], "GET")
        self.assertEqual(hit["path"], "/oast/%s/inner/path?x=1" % token,
                         "整个请求路径都要留下：后缀说明目标把它当 URL 用过了")
        self.assertEqual(hit["sourceIp"], "127.0.0.1",
                         "来源地址不能是空的，否则证据指不出是谁回连的")
        self.assertEqual(hit["label"], "ssrf")
        self.assertEqual(hit["headers"].get("x-oast-token"), token,
                         "请求头要记下来（名字统一小写）")

    def test_head_callback_is_recorded(self):
        """HEAD 也要记：有的 SSRF gadget 只发 HEAD，漏掉它就漏掉一整类回连。"""
        token = self.registry.register(oast.new_token("head"), "rce")
        http_get(self.port, "/oast/%s" % token, token=token, method="HEAD")
        hit = self.registry.wait(token, 5.0)
        self.assertIsNotNone(hit, "HEAD 回连必须被记录")
        self.assertEqual(hit["method"], "HEAD")

    def test_empty_token_callback_is_recorded(self):
        """/oast/ 后面什么都没有也要记，token 记成空串。

        "收到一个不带 token 的回连"本身就是信息（目标把 URL 截断了、或者只取了
        域名部分），而这条信息只在"记了空 token"的时候才存在。
        """
        raw = http_get(self.port, "/oast/")
        self.assertIn(b"404", raw.split(b"\r\n", 1)[0],
                      "没有 token 的请求不该被当成有效回连")
        dead = self.registry.hits("")
        self.assertEqual(len(dead), 1,
                         "不带 token 的回连必须留下一条记录（token 为空串）")
        self.assertEqual(dead[0]["token"], "")
        self.assertEqual(dead[0]["protocol"], "http")
        self.assertEqual(dead[0]["path"], "/oast/")
        self.assertEqual(dead[0]["sourceIp"], "127.0.0.1")

    def test_outside_paths_are_recorded_too(self):
        """任何其它路径也记录：状态码 404，但证据不丢。"""
        http_get(self.port, "/favicon.ico")
        hits = self.registry.hits()
        self.assertEqual(len(hits), 1, "非 /oast/ 路径的回连同样是证据")
        self.assertEqual(hits[0]["token"], "")
        self.assertEqual(hits[0]["path"], "/favicon.ico")

    def test_token_with_body_is_recorded(self):
        """POST 的正文要取到：外带型漏洞经常把数据塞在 body 里。"""
        token = self.registry.register(oast.new_token("xxe"), "xxe")
        http_get(self.port, "/oast/%s" % token, token=token, method="POST",
                 headers={"Content-Type": "application/xml"},
                 body=b"<root>secret</root>")
        hit = self.registry.wait(token, 5.0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["method"], "POST")
        self.assertIn("secret", hit["body"], "正文没读到，等于丢掉了外带的数据")

    def test_callback_carries_elapsed_time(self):
        """`elapsedMs` 从登记那刻起算，是"发出后多久回连"的唯一时间证据。

        从收包起算的话它永远是 0——而 0 恰恰是这条证据要回答的那个问题。
        """
        token = self.registry.register(oast.new_token("timing"), "ssrf")
        time.sleep(0.15)
        http_get(self.port, "/oast/%s" % token)
        hit = self.registry.wait(token, 5.0)
        self.assertIsNotNone(hit)
        self.assertGreater(hit["elapsedMs"], 100.0,
                           "等了 150ms 才回连，elapsedMs 却几乎是 0——说明它从收包"
                           "起算，而不是从 register() 起算")
        self.assertLess(hit["elapsedMs"], 5000.0, "这个延迟不合理")

    def test_unregistered_http_token_is_marked(self):
        """目标改写过的 token 也要在真连接里被标成 unregistered。"""
        unknown = "f" * 32
        self.assertNotIn(unknown, [entry["token"] for entry in
                                   self.registry.tokens()])
        http_get(self.port, "/oast/%s" % unknown)
        hit = self.registry.wait(unknown, 5.0)
        self.assertIsNotNone(hit, "未注册 token 的回连必须被记下来")
        self.assertEqual(hit["label"], "unregistered")
        self.assertEqual(hit["token"], unknown)


# ---------------------------------------------------------------- DNS 面

class ListenerDnsTestCase(unittest.TestCase):
    """DNS 面：真发 UDP 查询，并且**真解析应答**。"""

    def setUp(self):
        self.registry = oast.Registry()
        self.listener = oast.Listener(self.registry)
        self.endpoint = self.listener.start()
        self.addCleanup(self.listener.stop)
        self.port = self.endpoint["dnsPort"]

    def test_a_query_is_recorded_and_answered_with_an_a_record(self):
        """收到查询要记，还要回一个**真的** A 记录让目标不重试。

        断言必须落到应答字节上：只断言"sendto 没报错"，一个空应答、一个
        头都不完整的应答、一个答错地址的应答全都算通过。
        """
        token = self.registry.register(oast.new_token("dns"), "rce")
        name = "%s.%s" % (token, self.endpoint["domain"])
        reply = dns_query(self.port, name)

        self.assertGreaterEqual(len(reply), 12, "应答连 DNS 头都不够长")
        parsed = oast.parse_response(reply)
        self.assertIsNotNone(parsed, "应答解析不出来，说明它不是合法 DNS 包")
        self.assertEqual(parsed["id"], 0x1234, "应答的 ID 必须回抄请求的 ID")
        self.assertTrue(parsed["flags"] & 0x8000, "QR 位没置，这不是一个应答")
        self.assertEqual(parsed["flags"] & 0x000F, 0, "rcode 必须是 NOERROR")
        self.assertEqual(parsed["qname"], name)
        self.assertEqual(parsed["answerCount"], 1, "必须带一条答案记录")
        self.assertEqual(parsed["addresses"], ["127.0.0.1"],
                         "答案里必须是可用的地址，否则目标会继续重试别的名字")

        hit = self.registry.wait(token, 5.0)
        self.assertIsNotNone(hit, "真发了 DNS 查询，wait 却拿不到命中")
        self.assertEqual(hit["protocol"], "dns")
        self.assertEqual(hit["token"], token)
        self.assertEqual(hit["body"], name, "body 放 QNAME，原始域名要留全")
        self.assertEqual(hit["method"], "", "DNS 没有方法，别填一个假值")
        self.assertEqual(hit["sourceIp"], "127.0.0.1")

    def test_non_a_query_gets_an_empty_noerror(self):
        """非 A 查询回空的 NOERROR：目标不会重试，我们也少一条 fuzz 分支。"""
        token = self.registry.register(oast.new_token("aaaa"), "ssrf")
        name = "%s.%s" % (token, self.endpoint["domain"])
        reply = dns_query(self.port, name, qtype=28)             # AAAA
        parsed = oast.parse_response(reply)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["answerCount"], 0, "非 A 查询不该带 A 记录")
        self.assertEqual(parsed["flags"] & 0x000F, 0, "仍然是 NOERROR")

    def test_malformed_packets_do_not_kill_the_listener(self):
        """5 个垃圾包之后，合法查询照样被服务。

        这是本模块最容易写崩的地方：畸形包能让解析线程抛异常或永远卡在一个
        自指压缩指针里，而崩掉的线程是**唯一**接收回连的那一个，症状是
        "监听器还在，却再也收不到任何东西"——那时人已经在怀疑目标侧了。
        """
        good = self.registry.register(oast.new_token("alive"), "ssrf")
        good_name = "%s.%s" % (good, self.endpoint["domain"])

        header = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        junk = [
            b"",                                       # 空数据报
            b"\x00" * 11,                              # 连头都不够
            header + b"\xc0\x0c" + b"\x00\x01\x00\x01",   # 压缩指针指向自己
            header + b"\xfa" + b"a" * 20,                 # 标签长度声明 250
            header + b"\x3f" * 240 + b"\x00",             # 总长远超 255
        ]
        for payload in junk:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.4)
                sock.sendto(payload, ("127.0.0.1", self.port))
                try:
                    sock.recvfrom(512)                 # 畸形包不该有应答
                except socket.timeout:
                    pass

        reply = dns_query(self.port, good_name)
        parsed = oast.parse_response(reply)
        self.assertIsNotNone(parsed, "垃圾包之后监听器不再应答，说明收包线程死了")
        self.assertEqual(parsed["addresses"], ["127.0.0.1"])
        self.assertIsNotNone(self.registry.wait(good, 5.0),
                             "垃圾包之后合法查询没被记录")
        self.assertEqual(len(self.registry.hits()), 1,
                         "畸形包不该产出命中记录：只该有那一次合法查询")
        self.assertEqual(self.registry.hits()[0]["body"], good_name)

    def test_malformed_payload_returns_none_instead_of_raising(self):
        """畸形包在纯函数层面是 None，不是异常。

        这一层单独钉住，是因为它是崩溃面的入口：`Listener._dns_reply` 里那层
        `except Exception` 是兜底，不该被当成"畸形包已被处理"的依据。
        """
        self.assertIsNone(oast.parse_query(b""))
        self.assertIsNone(oast.parse_query(b"\x00" * 5))
        self.assertIsNone(oast.parse_query(b"\x00" * 12))          # qdcount = 0
        self.assertIsNone(oast.parse_query(b"\x00" * 11))
        loop = (b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
                + b"\xc0\x0c" + b"\x00\x01\x00\x01")
        self.assertIsNone(oast.parse_query(loop), "自指压缩指针必须被拒，否则死循环")
        backward = (b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
                    + b"\xc0\x02" + b"\x00\x01\x00\x01")
        self.assertIsNone(oast.parse_query(backward), "向后指的压缩指针同样要拒")


# ---------------------------------------------------------------- 并发生命周期

class ListenerConcurrencyTestCase(unittest.TestCase):
    """并发：目标的多个请求一个都不能丢。"""

    def test_twenty_concurrent_callbacks_are_all_recorded(self):
        """20 个线程同时回连同一个 token，命中数必须正好 20。

        少一条 = 锁没护住某段临界区（丢证据）；多一条 = 记录被写了两次
        （伪造证据）。两种错在人工点几下时都看不见。

        ## 为什么要读回包（这条用例最初就是这么假的失败）

        最初这里是"发完就走"（不读响应）。它在本机约三分之一的运行里报"只记到
        16/18 条"，而**客户端一次都没失败过**——看上去像记录被丢了。逐步打点后
        真相在客户端一侧：套接字在 `with` 退出时被立刻关闭，而 Windows 上"发送后
        立即关闭"可能把接收缓冲区里**还没被 accept 线程读走**的那批字节一起丢掉，
        于是 handler 读到 EOF、`parse_request` 失败，这一次连接连记录都进不去。

        真实目标不会这么干：它发出回连是为了等一个响应，连接在收到答复之前一直
        开着（本模块的 handler 也正是"读完、记录、再回包"）。让夹具按目标的样子
        做——发出请求、读到响应为止——这个竞态就没有了。也就是说读回包不是为了让
        测试"变绿"，而是让夹具不再比目标更苛刻；同时它顺带断言了监听器在 20 路
        并发下每一条都回得出响应。
        """
        registry = oast.Registry()
        listener = oast.Listener(registry)
        endpoint = listener.start()
        self.addCleanup(listener.stop)
        token = registry.register(oast.new_token("burst"), "ssrf")
        port = endpoint["httpPort"]

        workers = 20
        barrier = threading.Barrier(workers)
        failures = []
        replies = []

        def knock(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                raw = http_get(port, "/oast/%s/%d" % (token, index), token=token,
                               timeout=10)
                replies.append(raw)
            except Exception as exc:                   # noqa: BLE001
                failures.append("%r" % (exc,))

        threads = [threading.Thread(target=knock, args=(index,), daemon=True)
                   for index in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(failures, [], "并发回连里有连接失败：%s" % failures)
        self.assertEqual(len(replies), workers, "有线程没走完")
        for raw in replies:
            self.assertTrue(raw.startswith(b"HTTP/1."),
                            "并发下有的回连没拿到响应：%r" % (raw[:80],))
        hits = registry.hits(token)
        self.assertEqual(len(hits), workers,
                         "命中数不是 %d：少一条说明记录丢了，多一条说明记重了"
                         % workers)
        paths = sorted(hit["path"] for hit in hits)
        self.assertEqual(paths, sorted("/oast/%s/%d" % (token, index)
                                       for index in range(workers)),
                         "20 条命中的路径必须一一对应，不能有串台或覆盖")
        self.assertEqual(registry.tokens()[0]["hits"], workers,
                         "目录里的命中数要与实际记录一致")


class ListenerLifecycleTestCase(unittest.TestCase):
    """启停：测试套件会反复起停几百次，泄漏一个线程就够让后面随机失败。"""

    def test_stop_closes_ports_and_start_again_works(self):
        """stop 之后端口真的关了，且能再次 start（端口重新分配）。"""
        registry = oast.Registry()
        listener = oast.Listener(registry)
        self.addCleanup(listener.stop)

        first = listener.start()
        first_port = first["httpPort"]
        token = registry.register(oast.new_token("first"), "ssrf")
        http_get(first_port, "/oast/%s" % token)
        self.assertIsNotNone(registry.wait(token, 5.0), "首次回连没记上")

        listener.stop()
        self.assertEqual(listener.endpoint, {},
                         "stop 之后不该再报出一个还能连的端口")
        # 本机的实测行为要先说清，否则这段断言看起来像在测别的东西：
        # 在 Windows 上，连一个**从来没人绑定过**的 TCP 端口也是超时而不是
        # 立即 ECONNREFUSED（SYN 被静默丢弃，实测 2 秒不返回）。所以"连一下
        # 看拒不拒"在这里根本证明不了端口关没关——它和"端口还开着但没人 accept"
        # 的观察结果一模一样。
        #
        # 于是改用两条本机真的分得开的证据：
        # ① UDP 层：未监听的 UDP 端口会回 ICMP 端口不可达，在 Python 里表现为
        #    下一次 recvfrom 抛 ConnectionResetError（对照一个从未绑定过的端口，
        #    行为一致）。DNS 面因此是"真的关了"的直接证据。
        # ② 绑定层：监听套接字真释放的话，同一个端口立刻可以再次 bind。
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        probe.settimeout(1.0)
        probe.sendto(build_query("probe.oast.local"), ("127.0.0.1", first["dnsPort"]))
        with self.assertRaises(OSError) as caught:
            probe.recvfrom(512)
        probe.close()
        self.assertIsInstance(
            caught.exception, (ConnectionResetError, ConnectionRefusedError),
            "DNS 端口在 stop 之后还在应答（拿到的是 %r）：UDP 套接字没关，"
            "监听线程还活着" % (caught.exception,))

        rebind = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            rebind.bind(("127.0.0.1", first_port))
        finally:
            rebind.close()

        second = listener.start()
        self.assertNotEqual(second["httpPort"], 0)
        token2 = registry.register(oast.new_token("second"), "rce")
        http_get(second["httpPort"], "/oast/%s" % token2)
        hit = registry.wait(token2, 5.0)
        self.assertIsNotNone(hit, "重启之后收不到回连了")
        self.assertEqual(hit["token"], token2, "重启后不能把命中记到旧 token 上")
        self.assertEqual(len(registry.hits(token)), 1,
                         "重启不该影响已经记下的旧证据")

    def test_start_is_idempotent_and_does_not_leak_threads(self):
        """重复 start 不额外起监听器；反复启停后线程数回到基线。"""
        base = threading.active_count()
        listener = oast.Listener(oast.Registry())
        for _ in range(4):
            endpoint = listener.start()
            self.assertEqual(listener.start()["httpPort"], endpoint["httpPort"],
                             "已启动时再 start 必须回到同一个监听器，而不是新起一个")
            listener.stop()
        listener.stop()                                  # 再停一次也不能炸
        deadline = time.time() + 5
        while threading.active_count() > base and time.time() < deadline:
            time.sleep(0.05)
        # 留 1 的余量：`stop()` 已经把监听线程 join 掉了，但如果刚好有一个 DNS
        # 回包线程还在收尾，严格相等会因为一个与本用例无关的时序失败——那种
        # 失败一旦出现，最可能的处理是被顺手放宽或删掉，而不是被查清。
        self.assertLessEqual(threading.active_count(), base + 1,
                             "反复启停泄漏了线程：基线 %d，现在 %d（每次 start 都会"
                             "新起 HTTP 与 DNS 两个线程，泄漏会随用例数累积）"
                             % (base, threading.active_count()))


if __name__ == "__main__":
    unittest.main()
