# -*- coding: utf-8 -*-
"""流量 / 请求重发 / 工作空间 / 访问日志：四个新页签背后的接口。

## 为什么单独一份

这四条路径是"参考 artex 补全功能"时新加的第一批接口，它们的价值不在"能返回
数据"，而在四处**只有当接口存在时才会暴露的判断**：

* **作用域门禁在这条路径上只有一处。** 重发不发任务，因此不经过
  `create_job` 的那道闸；如果这里不判，整个平台就多出一条"想去哪就去哪"的
  发包路径，而它与其它路径的区别在界面上完全看不出来（都是填个地址点发送）。
  用例把"打到作用域外 → 403 且一条记录都没写"钉成断言：拒绝要发生在连接之前，
  否则"被拦了"与"发了但失败"在记录里长得一样。
* **发失败的那条同样要留记录。** `error` 非空表示"我发了但没拿到响应"，
  与"我没发过"是两件不同的事，而事后追查全靠这个字段分辨。这条一旦退化，
  表现是记录里只剩成功的请求，于是"我明明试过"变成无从查证。
* **流量记录里有别人发出去过的 Cookie 与令牌。** 列表与详情都按 `userId`
  收口，管理员的越权视角是**显式点名**（`?userId=`）而不是默认。
* **访问日志是"谁登录了、从哪来"。** 它只给管理员，并且只给尾部——整份取回
  没人在界面上看，而一次请求就要搬几百 KB JSON。

四条都不是"多写一个断言更保险"，而是各自对应一种**看起来能用、其实在漏**的
形态，所以各占一条用例。
"""
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import chat, httpreplay                            # noqa: E402
from pentestd.server import ControlPlane                         # noqa: E402
from tests import support                                        # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")

OPERATOR = ("ut-traffic-operator", "Operator-Pass1!")
# 203.0.113.0/24 是文档保留段（TEST-NET-3）：它不会被路由出去，因此把"作用域外"
# 当断言是安全的——即便门禁哪天失效，失败的也是一次连不上，而不是真的打到别人的机器上。
OUTSIDE = "http://203.0.113.5/nothing-here"
INSIDE = "http://127.0.0.1:%d/hello?x=1"


class _Echo(BaseHTTPRequestHandler):
    """最小的本地回声：把方法与正文回给调用方，便于核对原文有没有被改写。"""

    protocol_version = "HTTP/1.1"

    def _reply(self, code: int, body: bytes, extra=None):
        self.send_response(code)
        for name, value in (extra or []):
            self.send_header(name, value)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                        # noqa: N802
        self._reply(200, ("GET %s" % self.path).encode("utf-8"),
                    extra=[("X-Echo", "one")])

    def do_POST(self):                                       # noqa: N802
        size = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(size) if size else b""
        self._reply(201, b"POST " + raw, extra=[("X-Echo", "one")])

    def log_message(self, *args):                            # noqa: D401
        """安静：测试输出里不需要这个夹具自己的访问日志。"""


class TrafficApiTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fixture = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
        cls.fixture_thread = threading.Thread(target=cls.fixture.serve_forever,
                                              daemon=True)
        cls.fixture_thread.start()
        cls.fixture_port = cls.fixture.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.fixture.shutdown()
        cls.fixture.server_close()

    def setUp(self):
        self.directory, self.store = support.temporary_store("traffic-api.db")
        # addCleanup 后进先出：先停服务、再关连接、最后删临时目录。
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.httpd, self.admin = support.start(self.store)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = self.admin.base
        self.admin_id = self.store.get_user_by_name(support.ADMIN_USER)["id"]
        status, payload = self.admin.post("/api/v1/users", {
            "username": OPERATOR[0], "password": OPERATOR[1],
            "role": "operator"})
        self.assertEqual(status, 201, payload)
        self.operator_id = payload["user"]["id"]
        self.operator = support.Client(self.base)
        status, _ = self.operator.login(*OPERATOR)
        self.assertEqual(status, 200)

    # ------------------------------------------------------------ 辅助
    def inside(self):
        return INSIDE % self.fixture_port

    def records(self, path="/api/v1/traffic"):
        status, payload = self.admin.get(path)
        self.assertEqual(status, 200, payload)
        return payload

    # ------------------------------------------------- 重发：门禁与记账
    def test_replaying_a_request_keeps_the_original_bytes(self):
        """发出去的那条要被原样记下来，而列表默认不搬正文。

        列表一屏几十条，每条塞几十 KB 正文会把响应撑到几 MB，而界面上列表
        并不显示正文。所以"列表摘掉、详情给全"是刻意的分工，不是漏字段。
        """
        status, record = self.admin.post("/api/v1/traffic/replay", {
            "method": "POST", "url": self.inside(), "body": "参数=值",
            "headers": {"X-Probe": "yes"}})
        self.assertEqual(status, 200, record)
        self.assertEqual(record["status"], 201)
        self.assertEqual(record["responseBody"], "POST 参数=值")
        self.assertTrue(record["id"].startswith("X-"), record["id"])

        listed = self.records()["records"]
        self.assertEqual(len(listed), 1, listed)
        self.assertEqual(listed[0]["id"], record["id"])
        self.assertEqual(listed[0]["requestBody"], "")
        self.assertEqual(listed[0]["responseBody"], "")
        self.assertEqual(listed[0]["requestHeaders"], {"X-Probe": "yes"})

        status, detail = self.admin.get("/api/v1/traffic/%s" % record["id"])
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["requestBody"], "参数=值")
        self.assertEqual(detail["responseBody"], "POST 参数=值")
        self.assertIn(["X-Echo", "one"], detail["responseHeaders"])

    def test_an_address_outside_the_scope_is_refused_before_connecting(self):
        """作用域外 → 403，并且一条记录都不写。

        这是这条路径上唯一的一道闸（重发不发任务，因此不经过 `create_job`）。
        断言"库里没有记录"是在钉"拒绝发生在连接之前"：如果先连再判，记录里
        会多出一条 `error` 非空的条目，而那种形态与"目标不通"完全一样。
        """
        status, payload = self.admin.post("/api/v1/traffic/replay",
                                          {"url": OUTSIDE})
        self.assertEqual(status, 403, payload)
        self.assertIn("作用域", payload["error"])
        self.assertEqual(self.store.list_traffic(), [])

    def test_a_failed_send_is_still_recorded(self):
        """连不上也要留一条：`error` 非空才是"我发过"的证据。"""
        status, record = self.admin.post("/api/v1/traffic/replay", {
            "url": "http://127.0.0.1:1/", "timeoutS": 3})
        self.assertEqual(status, 200, record)
        self.assertEqual(record["status"], 0)
        self.assertTrue(record["error"], record)
        listed = self.records()["records"]
        self.assertEqual(len(listed), 1, listed)
        self.assertTrue(listed[0]["error"])

    def test_a_method_it_cannot_send_is_refused_by_name(self):
        status, payload = self.admin.post("/api/v1/traffic/replay", {
            "method": "CONNECT", "url": self.inside()})
        self.assertEqual(status, 400, payload)
        self.assertIn("不支持的方法", payload["error"])

    def test_headers_the_platform_computes_are_refused(self):
        """`Content-Length` 之类由平台算：静默改写会让人以为自己填的生效了。"""
        status, payload = self.admin.post("/api/v1/traffic/replay", {
            "url": self.inside(), "headers": {"Content-Length": "5"}})
        self.assertEqual(status, 400, payload)
        self.assertIn("不要手填", payload["error"])

    def test_a_body_on_get_is_refused(self):
        status, payload = self.admin.post("/api/v1/traffic/replay", {
            "method": "GET", "url": self.inside(), "body": "x"})
        self.assertEqual(status, 400, payload)
        self.assertIn("不该带正文", payload["error"])

    # ------------------------------------------------------------ 归属
    def test_the_listing_is_scoped_to_the_caller(self):
        """普通用户只看自己的；管理员的越权视角要显式点名。"""
        mine = self.store.record_traffic(user_id=self.admin_id, method="GET",
                                         url="http://127.0.0.1/admin")
        theirs = self.store.record_traffic(user_id=self.operator_id,
                                           method="POST",
                                           url="http://127.0.0.1/operator")

        payload = self.records()
        self.assertEqual(payload["scope"], "all")
        self.assertEqual(sorted(item["id"] for item in payload["records"]),
                         sorted([mine, theirs]))

        named = self.records("/api/v1/traffic?userId=%s" % self.operator_id)
        self.assertEqual(named["scope"], "own")
        self.assertEqual([item["id"] for item in named["records"]], [theirs])

        status, own = self.operator.get("/api/v1/traffic")
        self.assertEqual(status, 200, own)
        self.assertEqual(own["scope"], "own")
        self.assertEqual([item["id"] for item in own["records"]], [theirs])

    def test_the_listing_can_be_narrowed_to_one_session(self):
        """「流量」与「请求重发」右栏列的是**这条对话**里发出去的东西。

        不收口的话，上一个会话、或者同一个人的另一个标签页里跑过的扫描，会一起
        堆在这里——而列表上没有任何东西说明它们不是这一条对话的。前端为此每次
        都带 `sessionId`（panels.js 的 `trafficQuery`），这一条钉的是后端真的
        按它收口。
        """
        one = self.store.record_traffic(session_id="C-one", user_id=self.admin_id,
                                         method="GET", url="http://127.0.0.1/one")
        two = self.store.record_traffic(session_id="C-two", user_id=self.admin_id,
                                         method="GET", url="http://127.0.0.1/two")
        narrow = self.records("/api/v1/traffic?sessionId=C-one")
        self.assertEqual([item["id"] for item in narrow["records"]], [one])
        # 不带这一格就是**不收口**：两条都在。宁可多列几条，也别把这一页显示成空。
        every = self.records()["records"]
        self.assertEqual(sorted(item["id"] for item in every), sorted([one, two]))

    def test_a_replayed_request_is_filed_under_the_session_it_came_from(self):
        """重发页发出去的那一条要落进**当前这条对话**。

        漏传 `sessionId` 的症状最难看出来：请求发成功了、响应也画出来了，只是
        它落在"没有会话"里——于是「流量」页与右边那一栏立刻把刚发的那条过滤掉，
        看起来像"发了但没记下来"。
        """
        status, record = self.admin.post("/api/v1/traffic/replay", {
            "method": "GET", "url": self.inside(), "sessionId": "C-here"})
        self.assertEqual(status, 200, record)
        here = self.records("/api/v1/traffic?sessionId=C-here")["records"]
        self.assertEqual([item["id"] for item in here], [record["id"]])
        self.assertEqual(
            self.records("/api/v1/traffic?sessionId=C-elsewhere")["records"], [])

    def test_reading_someone_elses_record_is_refused(self):
        """记录里躺着 Cookie 与令牌，跨用户可读等于把它们摊开。"""
        mine = self.store.record_traffic(user_id=self.admin_id, method="GET",
                                         url="http://127.0.0.1/admin",
                                         request_headers={"Cookie": "s=1"},
                                         response_body="秘密")
        status, payload = self.operator.get("/api/v1/traffic/%s" % mine)
        self.assertEqual(status, 403, payload)
        self.assertIn("其他用户", payload["error"])
        self.assertNotIn("秘密", str(payload))

    def test_a_missing_record_is_404_not_an_empty_object(self):
        status, payload = self.admin.get("/api/v1/traffic/X-0000000000")
        self.assertEqual(status, 404, payload)

    def test_clearing_is_an_admin_action(self):
        self.store.record_traffic(user_id=self.operator_id, method="GET",
                                  url="http://127.0.0.1/x")
        status, payload = self.operator.post("/api/v1/traffic/clear")
        self.assertEqual(status, 403, payload)
        self.assertEqual(len(self.store.list_traffic()), 1)

        status, payload = self.admin.post("/api/v1/traffic/clear")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["removed"], 1)
        self.assertEqual(self.store.list_traffic(), [])

    # ------------------------------------------------------- 工作空间
    def test_the_workspace_page_reads_without_creating_anything(self):
        """打开这一页不该在盘上凭空多出三个目录。"""
        status, payload = self.admin.get("/api/v1/workspace")
        self.assertEqual(status, 200, payload)
        for key in ("root", "sessions", "cache", "exists", "sessionCount"):
            self.assertIn(key, payload)
        self.assertFalse(os.path.exists(payload["sessions"]),
                         "读一次就让 sessions 目录存在了")
        self.assertFalse(os.path.exists(payload["cache"]),
                         "读一次就让 cache 目录存在了")

    # ------------------------------------------------------- 访问日志
    def test_the_log_is_admin_only(self):
        status, payload = self.operator.get("/api/v1/logs")
        self.assertEqual(status, 403, payload)

    def test_the_log_gives_the_tail_and_drops_the_half_line(self):
        """截断时首行必须是完整记录——半行看起来最像一条真记录，而前半截没了。"""
        path = os.path.join(os.path.dirname(self.store.path), "access.log")
        # `newline="\n"`：Windows 的文本模式会把每一行写成 `\r\n`，于是
        # 读回来的行尾带一个 `\r`——那是测试夹具的写法问题，不是接口的问题。
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for index in range(20000):
                handle.write("L%05d %s" % (index, "x" * 48) + "\n")
        self.assertGreater(os.path.getsize(path), ControlPlane.MAX_LOG_BYTES)

        status, payload = self.admin.get("/api/v1/logs?limit=5")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["limit"], 5)
        self.assertEqual(len(payload["lines"]), 5)
        for line in payload["lines"]:
            self.assertRegex(line, r"^L\d{5} x+$")

    def test_the_log_limit_is_clamped_instead_of_rejected(self):
        path = os.path.join(os.path.dirname(self.store.path), "access.log")
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("LAST-LINE\n")
        status, payload = self.admin.get("/api/v1/logs?limit=0")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["limit"], 1)
        status, payload = self.admin.get("/api/v1/logs?limit=99999")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["limit"], 2000)
        self.assertIn("LAST-LINE", payload["lines"])


class ReplayUrlTests(unittest.TestCase):
    """`httpreplay` 本层的地址与头部解析（不经 HTTP，跑得快）。"""

    def test_a_url_without_a_scheme_is_refused_instead_of_guessed(self):
        # 猜出来的 scheme 决定了往 443 发还是往 80 发 TLS，猜错的表现是
        # "目标不响应"，而使用者会去查目标。
        with self.assertRaises(httpreplay.ReplayError) as caught:
            httpreplay.normalize_url("example.com/a")
        self.assertIn("scheme", str(caught.exception))

    def test_only_http_and_https_are_accepted(self):
        with self.assertRaises(httpreplay.ReplayError) as caught:
            httpreplay.normalize_url("file:///etc/passwd")
        self.assertIn("只支持", str(caught.exception))

    def test_the_default_port_follows_the_scheme(self):
        self.assertEqual(httpreplay.normalize_url("http://h/a")["port"], 80)
        self.assertEqual(httpreplay.normalize_url("https://h/a")["port"], 443)
        self.assertEqual(httpreplay.normalize_url("http://h:8080/a")["port"],
                         8080)

    def test_a_header_with_a_newline_is_refused(self):
        with self.assertRaises(httpreplay.ReplayError) as caught:
            httpreplay._check_headers({"X": "a\r\nInjected: yes"})
        self.assertIn("换行", str(caught.exception))

    def test_the_timeout_is_clamped_between_two_bounds(self):
        self.assertEqual(httpreplay._timeout_of(0),
                         httpreplay.DEFAULT_TIMEOUT_S)
        self.assertEqual(httpreplay._timeout_of(9999),
                         httpreplay.MAX_TIMEOUT_S)
        self.assertEqual(httpreplay._timeout_of("abc"),
                         httpreplay.DEFAULT_TIMEOUT_S)
        self.assertEqual(httpreplay._timeout_of(3), 3.0)


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------ 对话侧的流量生产者

class ChatHttpRequestToolTestCase(unittest.TestCase):
    """对话里发出去的那条请求，要真的落进「流量」页。

    这条用例守的是一件产品事实，不是一段代码：流量页此前**只有一个写入点**——
    人在「请求重发」里按下的那一条。模型在对话里用工具打出去的请求（`netscan` 的
    banner 探测就是）走裸 socket，一条都不进那张表。于是「流量跟随对话实时显示」
    这件事在页面上表现为「永远只有我手工发的那几条」。

    要验证它，必须断言**两侧**：工具返回里带着 `trafficId`（说明它走了记账那条路），
    以及库里真的多了一行、且归属落在这个会话上（否则页面上按 `sessionId` 收口时
    会被过滤掉，看起来和「没记下来」一模一样）。
    """

    @classmethod
    def setUpClass(cls):
        cls.fixture = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
        cls.fixture_thread = threading.Thread(target=cls.fixture.serve_forever,
                                              daemon=True)
        cls.fixture_thread.start()
        cls.fixture_port = cls.fixture.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.fixture.shutdown()
        cls.fixture.server_close()

    def setUp(self):
        self.directory, self.store = support.temporary_store("traffic-chat.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        # `probe_scope` 用库层默认（回环 + 私有网段）：这条用例要打的正是回环。
        self.plane = ControlPlane(self.store, WEB_DIR)

    def call(self, session=None, **args):
        return chat.HANDLERS["http_request"](self.plane, args, "", session or {})

    def inside(self):
        return INSIDE % self.fixture_port

    def test_the_catalog_advertises_it_as_a_probe_tool(self):
        catalog = {item["name"]: item for item in chat.tool_catalog()}
        self.assertIn("http_request", catalog)
        self.assertEqual(catalog["http_request"]["level"], "probe")
        self.assertEqual(sorted(catalog["http_request"]["parameters"]),
                         ["body", "headers", "method", "timeout", "url"])

    def test_a_request_sent_in_chat_lands_in_the_traffic_table(self):
        """工具返回的 `trafficId` 必须能在库里查到，且归属这条会话。"""
        session = {"sessionId": "C-here", "userId": "u-1"}
        result = self.call(session=session, url=self.inside(),
                           headers={"X-Probe": "yes"})
        self.assertTrue(result["data"]["trafficId"].startswith("X-"),
                        result["data"]["trafficId"])
        rows = self.store.list_traffic(session_id="C-here")
        self.assertEqual([row["id"] for row in rows], [result["data"]["trafficId"]])
        self.assertEqual(rows[0]["method"], "GET")
        self.assertEqual(rows[0]["status"], 200)
        self.assertEqual(rows[0]["userId"], "u-1")
        # 列表默认摘正文（`with_body=False`），原文要从详情那条取。
        detail = self.store.get_traffic(rows[0]["id"])
        self.assertIn("GET /hello", detail["responseBody"])

    def test_the_same_request_is_invisible_to_another_session(self):
        """收口是「这一条对话」：别的会话不该看到它，否则页面又变成大杂烩。"""
        self.call(session={"sessionId": "C-here"}, url=self.inside())
        self.assertEqual(self.store.list_traffic(session_id="C-elsewhere"), [])

    def test_a_failed_send_is_still_recorded(self):
        """连不上也要留一条：`error` 非空才是「我发过」的证据。"""
        result = self.call(session={"sessionId": "C-here"},
                           url="http://127.0.0.1:1/", timeout=3)
        self.assertEqual(result["data"]["status"], 0)
        self.assertTrue(result["data"]["error"])
        rows = self.store.list_traffic(session_id="C-here")
        self.assertEqual(len(rows), 1, rows)
        self.assertTrue(rows[0]["error"])

    def test_an_address_outside_the_scope_is_refused_before_connecting(self):
        """门禁在 `httpreplay` 那一层，和「请求重发」共用；越界要报成 ValueError。"""
        with self.assertRaises(ValueError) as caught:
            self.call(session={"sessionId": "C-here"}, url=OUTSIDE)
        self.assertIn("作用域", str(caught.exception))
        self.assertEqual(self.store.list_traffic(), [])

    def test_no_session_still_records_under_the_empty_session(self):
        """没有会话（CLI / 脚本化调用）不该炸，只是这一行落进「没有会话」里。"""
        result = self.call(url=self.inside())
        self.assertTrue(result["data"]["trafficId"])
        self.assertEqual(len(self.store.list_traffic()), 1)
