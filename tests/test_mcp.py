# -*- coding: utf-8 -*-
"""MCP 客户端：协议、命名、闸门、审查、声明、接口。

## 为什么用一个**真的**子进程，而不是替身对象

`mcp._StdioSession` 里最容易写错的三处，替身一个都测不出来（见
`tests/mcp_fixture_server.py` 的模块文档）：换行分隔的 JSON 帧、stderr 必须被读掉、
进程死掉要能立刻发现。这三处的失效形态都是"看起来正常，直到某个具体条件出现"——
而它们全都只在真的管道上才出现。所以这个文件里的 stdio 用例跑的是
`fixture --selftest` 之外的真协议交互。

HTTP 那一半用进程内的 `ThreadingHTTPServer`：它是真的走 TCP，而且能按需切换
"JSON 响应"与"SSE 响应"—— 这两种形状规范都允许，只认一种会在另一种上得到一句
误导性很强的 `响应不是合法 JSON`。

## 这个文件里最该保住的三条

1. **闸门只有一处**：`chat.check_tool` 的那一支 MCP 分支调 `mcp.gate`。下面既有
   "档位不够被拒"的正面用例，也有"把 gate 放宽之后必须红"的变异守卫。
2. **名字是发现时确定的**：`read_file` 与 `read.file` 会撞成同一个 slug，撞名之后
   必须各自唯一、并且**调回各自那个原始工具**——这是"倒推名字"必然做错的地方。
3. **保存是整条覆盖**：漏一个字段就是一次静默清空（`env` 曾经会这样丢）。所以有一条
   用例逐项比对"管理员拿到的字段"与"validate 产出的字段"。
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import chat, harness, mcp, policy, subagent         # noqa: E402
from pentestd.server import ControlPlane                          # noqa: E402
from pentestd.store import Store                                  # noqa: E402
from tests import support                                         # noqa: E402

FIXTURE = os.path.join(HERE, "mcp_fixture_server.py")


def stdio_fields(**overrides):
    data = {"id": "fx", "label": "测试服务", "transport": "stdio",
            "command": sys.executable, "args": ["-u", FIXTURE],
            "level": "full", "review": True, "timeoutS": 20}
    data.update(overrides)
    return mcp.validate(data)


class Base(unittest.TestCase):
    """一个控制面 + 一个已登记的 stdio 服务。每个用例自己收进程。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("mcp.db")
        self.plane = ControlPlane(self.store, support.WEB_DIR,
                                  auth_required=False)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.addCleanup(mcp.close_all)
        mcp.close_all()

    def register(self, **overrides):
        server = self.store.save_mcp_server(stdio_fields(**overrides))
        mcp.invalidate(server["id"])
        return server

    def probe(self, server_id="fx"):
        return mcp.probe(self.plane, server_id)

    def call(self, name, args=None, policy_id="full"):
        denied, clean = chat.check_tool(self.plane, name, args or {}, "",
                                       policy_id, "", False, {})
        if denied is not None:
            return denied
        return chat.invoke_tool(self.plane, name, clean, "")


# ------------------------------------------------------------------ 协议

class StdioProtocolTestCase(Base):
    """stdio：握手、清单、调用、噪声、stderr、进程死、超时、反向请求。"""

    def test_handshake_reports_protocol_and_server_identity(self):
        self.register()
        result = self.probe()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["serverName"], "fixture")
        self.assertEqual(result["serverVersion"], "0.1")
        # 服务回自己的版本，**采纳它**而不是坚持我们发出去的那个。
        self.assertEqual(result["protocol"], "2024-11-05")

    def test_tools_are_discovered_with_schemas_and_readonly_hints(self):
        self.register()
        result = self.probe()
        by_name = {tool["name"]: tool for tool in result["tools"]}
        self.assertIn("echo", by_name)
        self.assertEqual(by_name["echo"]["schema"]["properties"]["text"]["type"],
                         "string")
        self.assertTrue(by_name["read_file"]["readOnly"],
                        "annotations.readOnlyHint 没有被读进来")

    def test_arguments_and_results_round_trip_as_utf8(self):
        """参数的中文必须在服务那边**完好无损**地出现，回来的也必须是原样。

        这一条测的是 `_write` 用纯 ASCII 出站那个决定：出站用 `\\uXXXX` 转义，
        于是对端 stdin 是什么代码页都不影响；如果用 UTF-8 字节直发，一个 Code Page
        为 cp950 的服务会把「你好」读成「浣犲ソ」，再原样回显回来。
        """
        self.register()
        self.probe()
        outcome = self.call("mcp__fx__echo", {"text": "你好，世界"})
        self.assertTrue(outcome["ok"], outcome["summary"])
        self.assertEqual(outcome["data"]["text"], "echo:你好，世界")

    def test_non_json_lines_on_stdout_are_skipped(self):
        """服务在响应前吐一行非 JSON —— 必须跳过，而不是当成协议错误。"""
        self.register()
        self.probe()
        outcome = self.call("mcp__fx__noise")
        self.assertTrue(outcome["ok"], outcome["summary"])
        self.assertEqual(outcome["data"]["text"], "噪声之后的正常结果")

    def test_stderr_is_drained_so_the_child_cannot_block(self):
        """服务往 stderr 写日志不能把它自己堵死。

        不读 stderr 的话，管道缓冲区写满之后子进程会**卡在 write 上**，而表现是
        "这个工具超时" —— 看起来像服务慢，实际是它被自己的日志堵住了。这一条与
        `toolkit._stream_child` 里那处是同一个坑。
        """
        self.register()
        self.probe()
        outcome = self.call("mcp__fx__stderr")
        self.assertTrue(outcome["ok"], outcome["summary"])
        self.assertEqual(outcome["data"]["text"], "stderr 已写过")

    def test_a_dead_child_is_reported_with_its_exit_code(self):
        """进程退出必须是"closed + 退出码"，不是干等满超时。

        等满超时会把一次立刻可判定的失败变成几十秒的等待，而且报出来的原因是错的
        （"超时"而不是"它退出了，原因是这个"）。
        """
        self.register()
        self.probe()
        outcome = self.call("mcp__fx__die")
        self.assertFalse(outcome["ok"])
        self.assertIn("退出码 3", outcome["summary"])

    def test_a_reverse_request_is_answered_so_the_server_does_not_hang(self):
        """服务端反向调用（这里发的是 sampling/createMessage）必须被回一条错误。

        不回的话，一个等着这个响应的服务会挂在那里，而表现是"这个工具超时"。
        回什么内容 fixture 会原样带回来，所以这里能断言它**真的收到了**。
        """
        self.register()
        self.probe()
        outcome = self.call("mcp__fx__poll")
        self.assertTrue(outcome["ok"], outcome["summary"])
        self.assertIn("-32601", outcome["data"]["text"])
        self.assertIn("sampling/createMessage", outcome["data"]["text"])

    def test_a_timeout_is_reported_as_a_timeout_and_costs_one_budget(self):
        """超时要报成 timeout，而且**只花一次预算**（不重试）。

        重试一个超时的 `tools/call` 等于把那个动作做第二遍 —— 服务可能已经动手了。
        """
        self.register(timeoutS=2)
        self.probe()
        started = time.time()
        outcome = self.call("mcp__fx__slow", {"seconds": 30})
        elapsed = time.time() - started
        self.assertFalse(outcome["ok"])
        self.assertIn("超过 2 秒", outcome["summary"])
        self.assertLess(elapsed, 6, "超时之后又重试了一次（用时翻倍）")

    def test_a_too_large_result_is_truncated_and_says_so(self):
        self.register()
        self.probe()
        outcome = self.call("mcp__fx__big")
        self.assertEqual(len(outcome["data"]["text"]), mcp.MAX_RESULT_TEXT)
        self.assertTrue(outcome["data"]["truncated"])
        self.assertIn("已截断", outcome["summary"])

    def test_is_error_from_the_server_is_not_reported_as_success(self):
        """服务报告 isError 时，回灌给模型的那一条必须**不是**成功。

        混起来的话，模型会把"这次调用失败了"读成"目标没有问题"。
        """
        self.register()
        self.probe()
        outcome = self.call("mcp__fx__fail")
        self.assertTrue(outcome["ok"], "工具本身执行完了")   # 走到服务了
        self.assertTrue(outcome["data"]["isError"], "服务说的失败没有被带出来")
        self.assertIn("服务报告这次调用失败", outcome["summary"])

    def test_non_text_blocks_and_structured_content_survive(self):
        self.register()
        self.probe()
        outcome = self.call("mcp__fx__blocks")
        self.assertEqual(outcome["data"]["structuredContent"], {"ok": True, "n": 7})
        self.assertEqual(outcome["data"]["blocks"], [{"type": "image"}])

    def test_a_dead_session_is_replaced_on_the_next_call(self):
        """一个坏掉的会话不能留在缓存里：下一次调用必须能重新连上。"""
        self.register()
        self.probe()
        self.call("mcp__fx__die")
        outcome = self.call("mcp__fx__echo", {"text": "复活"})
        self.assertTrue(outcome["ok"], outcome["summary"])
        self.assertEqual(outcome["data"]["text"], "echo:复活")

    def test_missing_command_is_a_spawn_error_naming_the_command(self):
        """命令不存在时要说清**是哪个命令**。只说"连不上"使用者无从下手。"""
        self.register(id="gone", command="definitely-not-a-real-program-xyz")
        result = mcp.probe(self.plane, "gone")
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "spawn")
        self.assertIn("definitely-not-a-real-program-xyz", result["error"])


class HttpProtocolTestCase(Base):
    """HTTP：JSON 与 SSE 两种响应、会话 id、三类失败。"""

    @classmethod
    def setUpClass(cls):
        cls.state = {"sse": False, "session": [], "calls": []}
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _HttpFixture)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def http_fields(self, **overrides):
        data = {"id": "web", "label": "HTTP 服务", "transport": "http",
                "url": "http://127.0.0.1:%d/mcp" % self.port,
                "level": "full", "timeoutS": 10}
        data.update(overrides)
        return mcp.validate(data)

    def register_http(self, **overrides):
        server = self.store.save_mcp_server(self.http_fields(**overrides))
        mcp.invalidate(server["id"])
        return server

    def _round_trip(self, sse):
        self.state["sse"] = sse
        self.state["session"] = []
        self.register_http()
        result = self.probe("web")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual([tool["name"] for tool in result["tools"]], ["fetch.url"])
        outcome = self.call("mcp__web__fetch_url", {"url": "https://x.test"})
        self.assertTrue(outcome["ok"], outcome["summary"])
        self.assertEqual(outcome["data"]["text"], "取到了 https://x.test")

    def test_json_responses_are_understood(self):
        self._round_trip(False)

    def test_sse_responses_are_understood(self):
        self._round_trip(True)

    def test_the_session_id_from_a_response_header_is_echoed_back(self):
        """Streamable HTTP 的会话靠响应头带回来，后续请求要带上它。"""
        self.state["sse"] = False
        self.state["session"] = []
        self.register_http()
        self.probe("web")
        self.call("mcp__web__fetch_url", {"url": "https://y.test"})
        # 第一条是 initialize（那时还没有会话 id），之后都必须带上
        self.assertIn("sess-abc", self.state["session"])
        self.assertEqual(self.state["session"][-1], "sess-abc")

    def test_the_three_failure_shapes_are_distinguishable(self):
        """连不上 / 404 / 响应不是 JSON —— 三种都要给出**不同**的说明。

        它们给不出同一句话：一种是"地址不对或服务没起"，一种是"这个路径没有服务"，
        一种是"对面不是 MCP 服务"。混成一句"连接失败"就要人去逐个排查。
        """
        cases = [("refused", "http://127.0.0.1:9/mcp", "transport",
                  "连接服务失败"),
                 ("notfound", "http://127.0.0.1:%d/nope" % self.port, "protocol",
                  "HTTP 404"),
                 ("garbage", "http://127.0.0.1:%d/garbage" % self.port,
                  "protocol", "不是合法 JSON")]
        seen = []
        for server_id, url, stage, needle in cases:
            self.register_http(id=server_id, url=url)
            result = mcp.probe(self.plane, server_id)
            self.assertFalse(result["ok"], "%s 竟然成功了" % server_id)
            self.assertEqual(result["stage"], stage)
            self.assertIn(needle, result["error"])
            seen.append(result["error"])
        self.assertEqual(len(set(seen)), 3, "三种失败说出了同一句话")

    def test_a_failed_probe_is_written_back_so_the_ui_can_show_it(self):
        self.register_http(id="bad", url="http://127.0.0.1:9/mcp")
        mcp.probe(self.plane, "bad")
        row = self.store.get_mcp_server("bad")
        self.assertEqual(row["status"], "error")
        self.assertTrue(row["last_error"])


class _HttpFixture(BaseHTTPRequestHandler):
    """进程内的 HTTP MCP 服务。**按路径分流**，所以 404 与坏 JSON 都是真的。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _send(self, status, body, kind="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(
            body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Mcp-Session-Id", "sess-abc")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path == "/garbage":
            return self._send(200, b"this is not json at all")
        if self.path != "/mcp":
            return self._send(404, {"error": "no such endpoint"})
        length = int(self.headers.get("Content-Length") or 0)
        message = json.loads(self.rfile.read(length).decode("utf-8"))
        HttpProtocolTestCase.state["session"].append(
            self.headers.get("Mcp-Session-Id") or "")
        method = message.get("method") or ""
        rid = message.get("id")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {},
                      "serverInfo": {"name": "http-fixture", "version": "9.9"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "fetch.url", "description": "取一个网址",
                                 "inputSchema": {"type": "object", "properties": {
                                     "url": {"type": "string"}}}}]}
        elif method == "tools/call":
            params = message.get("params") or {}
            HttpProtocolTestCase.state["calls"].append(params)
            result = {"content": [{"type": "text", "text": "取到了 %s" % (
                params.get("arguments") or {}).get("url")}]}
        else:
            result = {"ok": True}
        payload = {"jsonrpc": "2.0", "id": rid, "result": result}
        if HttpProtocolTestCase.state["sse"]:
            text = "event: message\ndata: %s\n\n" % json.dumps(
                payload, ensure_ascii=False)
            return self._send(200, text.encode("utf-8"), "text/event-stream")
        return self._send(200, payload)


# ------------------------------------------------------------------ 命名

class NameTestCase(unittest.TestCase):
    """工具名的形状、撞名、长度、解析。**不碰存储也不起进程。**"""

    def test_the_shape_is_prefix_id_separator_tool(self):
        self.assertEqual(mcp.exposed_name("fs", "read_file"),
                         "mcp__fs__read_file")
        self.assertEqual(mcp.parse_name("mcp__fs__read_file"),
                         ("fs", "read_file"))

    def test_illegal_characters_are_replaced_with_underscore(self):
        self.assertEqual(mcp.exposed_name("fs", "read.file"), "mcp__fs__read_file")
        self.assertEqual(mcp.exposed_name("fs", "read/file"), "mcp__fs__read_file")
        self.assertEqual(mcp.exposed_name("fs", "read file"), "mcp__fs__read_file")

    def test_colliding_names_get_distinct_suffixes_and_stay_callable(self):
        """`read_file` 与 `read.file` 会撞成同一个 slug —— **必须各自唯一**。

        更重要的另一半：撞名之后要能**调回各自那个原始工具**。"把 exposed 倒推成
        原始名"在这里必然推错其中一个，所以映射是发现时确定的（见 `normalize_tools`）。
        """
        tools = mcp.normalize_tools("fs", [
            {"name": "read_file"}, {"name": "read.file"}])
        exposed = [tool["exposed"] for tool in tools]
        self.assertEqual(exposed, ["mcp__fs__read_file", "mcp__fs__read_file_2"])
        self.assertEqual(len(set(exposed)), 2)

    def test_a_long_name_is_trimmed_to_the_function_name_limit(self):
        raw = "very." * 40 + "long"
        name = mcp.exposed_name("fs", raw)
        self.assertLessEqual(len(name), mcp.MAX_TOOL_NAME)
        # 裁过之后仍然唯一：两个只在前缀之后才不同的长名字不能撞成同一个
        other = mcp.exposed_name("fs", raw + "x")
        self.assertNotEqual(name, other)

    def test_a_server_id_may_not_contain_the_separator(self):
        """id 里带 `__` 就切不开工具名了 —— 写路径必须当场拒绝。"""
        with self.assertRaises(Exception) as caught:
            mcp.validate({"id": "a__b", "label": "x", "command": "y"})
        self.assertIn("分隔符", str(caught.exception))

    def test_owns_only_matches_the_prefix(self):
        self.assertTrue(mcp.owns("mcp__fs__x"))
        self.assertFalse(mcp.owns("mcp"))
        self.assertFalse(mcp.owns("run_tool"))
        self.assertFalse(mcp.owns(""))


class LocateTestCase(Base):
    """名字 → 服务的解析：三种找不到的原因必须各不相同。"""

    def setUp(self):
        super().setUp()
        self.register()
        self.probe()

    def test_unknown_server_names_the_ones_that_exist(self):
        _server, _tool, problem = mcp.locate(self.plane, "mcp__nope__x", "full")
        self.assertIn("没有登记过", problem)
        self.assertIn("fx", problem, "要告诉它已登记的是哪些，否则只能瞎猜")

    def test_unknown_tool_lists_what_the_server_actually_declares(self):
        _server, _tool, problem = mcp.locate(self.plane, "mcp__fx__nope", "full")
        self.assertIn("没有名为 nope 的工具", problem)
        self.assertIn("echo", problem)

    def test_a_server_with_no_cached_tools_says_to_probe_it(self):
        """缓存是空的与"服务真的没声明工具"是两件事，说给模型的下一步也不同。"""
        self.register(id="fresh", tools=None)
        _server, _tool, problem = mcp.locate(self.plane, "mcp__fresh__x", "full")
        self.assertIn("测试连接", problem)

    def test_a_disabled_server_says_so_and_where_to_turn_it_back_on(self):
        self.store.save_mcp_server(dict(stdio_fields(), enabled=False))
        mcp.invalidate("fx")
        _server, _tool, problem = mcp.locate(self.plane, "mcp__fx__echo", "full")
        self.assertIn("已被停用", problem)

    def test_a_malformed_name_says_what_the_shape_should_be(self):
        _server, _tool, problem = mcp.locate(self.plane, "mcp__fx__", "full")
        self.assertIn("格式不对", problem)


# ------------------------------------------------------------------ 闸门

class GateTestCase(Base):
    """闸门：`chat.check_tool` 那一支 + `mcp.gate`。"""

    def setUp(self):
        super().setUp()
        self.register(level="audit")
        self.probe()

    def test_a_low_policy_is_refused_with_the_level_it_needs(self):
        denied, _args = chat.check_tool(self.plane, "mcp__fx__echo", {}, "", "probe",
                                        "", False, {})
        self.assertIsNotNone(denied)
        self.assertIn("权限拒绝", denied["summary"])
        self.assertIn("源码审计", denied["summary"], "要说清需要哪一档")
        self.assertTrue(denied["data"]["denied"])
        self.assertTrue(denied["data"]["mcp"])

    def test_a_high_enough_policy_passes(self):
        denied, args = chat.check_tool(self.plane, "mcp__fx__echo", {"text": "x"},
                                       "", "audit", "", False, {})
        self.assertIsNone(denied)
        self.assertEqual(args, {"text": "x"})

    def test_the_gate_is_the_only_one_and_it_is_not_the_static_table(self):
        """MCP 工具**不在**那两张静态表里，而启动断言仍然成立。

        它们是运行时发现的，进不了 `HANDLERS` / `TOOL_LEVEL`；如果哪天有人"顺手"
        把它们塞进 `TOOL_LEVEL`，`_tool_level_guard` 会因为"分级里有多余条目"而炸。
        这一条同时钉住两件事：它们不在表里，以及静态表仍然一一对应。
        """
        self.assertNotIn("mcp__fx__echo", chat.HANDLERS)
        self.assertNotIn("mcp__fx__echo", policy.TOOL_LEVEL)
        self.assertNotIn("mcp__fx__echo", policy.allowed_tools("full"))
        chat._tool_level_guard()            # 不抛就是过

    def test_an_unknown_mcp_name_is_not_reported_as_a_nonexistent_tool(self):
        """`mcp__a__b` 而服务不存在时，不能说"不存在这个工具"。

        那句话会让模型以为这一类工具根本不存在，于是不再尝试——而真实原因是
        "那个服务没登记"或"档位不够"，两者的下一步动作完全不同。
        """
        denied, _args = chat.check_tool(self.plane, "mcp__ghost__x", {}, "", "full",
                                        "", False, {})
        self.assertIsNotNone(denied)
        self.assertNotIn("不存在名为", denied["summary"])
        self.assertIn("没有登记过", denied["summary"])

    def test_gate_widening_guard_has_teeth(self):
        """变异守卫：把 `mcp.gate` 改成恒放行，上面的档位用例必须红。

        没有这一条，"档位真的在拦"这件事只由上面那条正面用例保证——而它也可能
        因为别的原因通过（比如工具压根没被找到）。
        """
        original = mcp.gate
        try:
            mcp.gate = lambda *a, **k: ("", {})
            denied, _args = chat.check_tool(self.plane, "mcp__fx__echo", {}, "",
                                            "probe", "", False, {})
            self.assertIsNone(denied, "放宽之后仍然被拒——说明上面那条测的不是档位")
        finally:
            mcp.gate = original
        denied, _args = chat.check_tool(self.plane, "mcp__fx__echo", {}, "", "probe",
                                        "", False, {})
        self.assertIsNotNone(denied)

    def test_a_call_records_that_the_server_was_used(self):
        """真的调过一次之后 `used_at` 要有值：它回答"这个服务还在用吗"。"""
        self.assertIsNone(self.store.get_mcp_server("fx")["used_at"])
        self.call("mcp__fx__echo", {"text": "x"}, policy_id="audit")
        self.assertIsNotNone(self.store.get_mcp_server("fx")["used_at"])

    def test_a_denied_call_does_not_touch_the_server(self):
        """被闸门拒的那一次**不算用过**：它没有连服务。"""
        self.call("mcp__fx__echo", {"text": "x"}, policy_id="probe")
        self.assertIsNone(self.store.get_mcp_server("fx")["used_at"])


# ------------------------------------------------------------------ 审查

class ReviewTestCase(Base):
    """审查：`chat._review_required` 是唯一的合成点，`mcp.classify` 自己写卡。"""

    def setUp(self):
        super().setUp()
        self.register()
        self.probe()

    def test_required_when_the_server_asks_for_it(self):
        self.assertTrue(chat._review_required(self.plane, "mcp__fx__echo", "ask"))

    def test_not_required_when_the_server_opted_out(self):
        self.store.save_mcp_server(dict(stdio_fields(), review=False))
        mcp.invalidate("fx")
        self.assertFalse(chat._review_required(self.plane, "mcp__fx__echo", "ask"))

    def test_off_disables_it_even_when_the_server_asks(self):
        """`chat_review=off` 必须能真的关掉它，否则那一档就是一句空话。

        这一条与 `harness.tier_of` 的"档位先于表判定"是同一条纪律 —— 两半各自
        在自己的分支里读 mode，少任何一边右边那一档就漏。
        """
        self.assertFalse(chat._review_required(self.plane, "mcp__fx__echo", "off"))
        self.assertTrue(harness.needs_review("run_command", "ask"),
                        "对照：普通档位下 run_command 仍然要审")
        self.assertFalse(harness.needs_review("run_command", "off"))

    def test_platform_tools_are_unaffected(self):
        self.assertEqual(chat._review_required(self.plane, "run_command", "ask"),
                         harness.needs_review("run_command", "ask"))
        self.assertEqual(chat._review_required(self.plane, "start_probe", "ask"),
                         harness.needs_review("start_probe", "ask"))

    def test_the_card_does_not_claim_it_is_an_intrusive_action(self):
        """审查卡**不许**说"它会改动目标的状态"。

        那是 `harness.classify` 兜底分支照侵入式动作写的措辞。MCP 工具跑在控制面
        这台机器上、碰的是本机文件与网络，与目标无关——读单子的人据此判断"要不要批"
        就会判断错，而这张单子是他唯一的依据。
        """
        card = mcp.classify(self.plane, "mcp__fx__echo", {"text": "x"}, "ask")
        self.assertTrue(card["required"])
        self.assertEqual(card["tier"], "always")
        text = " ".join(card["reasons"]) + card["headline"]
        self.assertNotIn("侵入式", text)
        self.assertNotIn("改动目标的状态", text)
        self.assertIn("控制面", text)
        self.assertIn("测试服务", card["headline"])
        self.assertIn("echo", card["headline"])
        self.assertEqual(card["facts"]["service"], "fx")
        self.assertEqual(card["facts"]["tool"], "echo")

    def test_the_card_says_the_result_is_not_evidence(self):
        card = mcp.classify(self.plane, "mcp__fx__echo", {}, "ask")
        self.assertIn("不产出目标证据", " ".join(card["reasons"]))

    def test_the_card_text_carries_no_markdown_marks(self):
        """卡片是 `textContent`，星号与反引号会被原样读到。

        这一张卡原先是少数几处**自己带记号**的：`这是**外部 MCP 服务**提供的
        工具`、`它**不产出目标证据**`、`**现在要不要做这一条**`。`harness.plain`
        那条注释记的是同一个坑（"截图里看出来的，不是想出来的"），写入口
        `approval.submit` 也有一道闸；但源头这一份本来就不该带——两道都钉住，
        是因为改回带记号的那种写法在页面上完全看不出来。
        """
        card = mcp.classify(self.plane, "mcp__fx__echo", {"text": "x"}, "ask")
        text = " ".join(card["reasons"]) + card["headline"]
        for mark in ("**", "`"):
            self.assertNotIn(mark, text, "审查卡上出现了 %s 记号" % mark)

    def test_harness_classify_is_not_consulted_for_mcp_tools(self):
        """`harness.classify` 对 MCP 工具返回 required=False —— 合成在 chat 那一句。"""
        self.assertFalse(harness.classify("mcp__fx__echo", {}, "ask")["required"])


# ------------------------------------------------------------------ 声明与提示词

class DeclarationTestCase(Base):
    def test_only_the_policy_that_reaches_the_server_gets_its_tools(self):
        self.register(level="audit")
        self.probe()
        self.assertTrue(mcp.declarations(self.plane, "audit", "", False))
        self.assertEqual(mcp.declarations(self.plane, "probe", "", False), [])

    def test_a_disabled_server_contributes_nothing(self):
        self.register()
        self.probe()
        self.assertTrue(mcp.declarations(self.plane, "full", "", False))
        self.store.save_mcp_server(dict(stdio_fields(), enabled=False))
        mcp.invalidate("fx")
        self.assertEqual(mcp.declarations(self.plane, "full", "", False), [])

    def test_the_declaration_carries_the_label_the_ui_shows(self):
        """轨迹上显示的是"文件系统 · read_file"，不是 `mcp__fs__read_file`。"""
        self.register()
        self.probe()
        item = mcp.declarations(self.plane, "full", "", False)[0]
        self.assertTrue(item["name"].startswith("mcp__fx__"))
        self.assertEqual(item["label"], "测试服务 · echo")
        self.assertIn("外部 MCP 服务", item["description"])

    def test_declarations_are_capped(self):
        """上限必须真的生效：它是**上下文成本**的闸。"""
        tools = [{"name": "t%d" % index} for index in range(mcp.MAX_TOOLS_PER_SERVER)]
        self.store.save_mcp_server(stdio_fields())
        self.store.set_mcp_probe("fx", status="ready",
                                 tools=mcp.normalize_tools("fx", tools))
        got = mcp.declarations(self.plane, "full", "", False)
        self.assertEqual(len(got), mcp.MAX_DECLARATIONS)

    def test_declarations_for_takes_exactly_the_named_ones(self):
        """子智能体点名要的那几个：**不受 MAX_DECLARATIONS 约束**。

        受它约束的话，第 49 个工具会被点名收下却拿不到声明——模型看得到名字、
        发得出来、平台也认，只是不知道该带什么参数。
        """
        tools = [{"name": "t%d" % index} for index in range(mcp.MAX_TOOLS_PER_SERVER)]
        self.store.save_mcp_server(stdio_fields())
        self.store.set_mcp_probe("fx", status="ready",
                                 tools=mcp.normalize_tools("fx", tools))
        wanted = ["mcp__fx__t%d" % index for index in (0, 47, 60, 127)]
        got = mcp.declarations_for(self.plane, wanted, "full", "", False)
        self.assertEqual(sorted(item["name"] for item in got), sorted(wanted))

    def test_the_prompt_block_names_the_shape_and_the_limits(self):
        self.register()
        self.probe()
        text = mcp.prompt_block(self.plane, "full", "", False)
        self.assertIn("mcp__<服务id>__<工具名>", text)
        self.assertIn("不可信输入", text)
        self.assertIn("不产出目标证据", text)
        self.assertIn("控制面这台机器", text)
        self.assertIn("测试服务", text)

    def test_the_prompt_block_names_the_servers_you_cannot_reach(self):
        """档位不够的服务要点名，否则"我加了服务，模型却说没有"看起来像坏了。"""
        self.register(level="full")
        self.probe()
        text = mcp.prompt_block(self.plane, "readonly", "", False)
        self.assertIn("看不到", text)
        self.assertIn("fx", text)

    def test_the_prompt_block_without_a_plane_makes_no_claim_about_the_deployment(self):
        """`plane=None` 是"没绑定控制面"，**不能**说"本部署没有登记任何服务"。

        那是一句它没有依据的话，而它会出现在模块级的 SYSTEM_PROMPT 常量里。
        """
        text = mcp.prompt_block(None)
        self.assertNotIn("没有登记任何", text)
        self.assertIn("mcp__<服务id>__<工具名>", text)

    def test_the_catalog_lists_what_the_model_can_see(self):
        self.register()
        self.probe()
        data = mcp.catalog(self.plane, "full", "", False, admin=False)
        self.assertEqual(len(data["servers"]), 1)
        self.assertGreaterEqual(data["declared"], 1)
        self.assertIn("mcp__fx__echo", data["tools"])

    def test_a_non_admin_cannot_see_the_transport_details(self):
        """非管理员看不到命令 / 地址 / 环境变量：那些字段里可能有凭据。"""
        self.register(env={"SECRET_TOKEN": "hunter2"}, transport="stdio")
        self.probe()
        mine = mcp.catalog(self.plane, "full", "", False, admin=True)["servers"][0]
        theirs = mcp.catalog(self.plane, "full", "", False, admin=False)["servers"][0]
        self.assertIn("command", mine)
        self.assertIn("SECRET_TOKEN", mine["envKeys"])
        for key in ("command", "args", "env", "envKeys", "url", "headers",
                    "headerKeys", "cwd", "insecure"):
            self.assertNotIn(key, theirs, "%s 不该给非管理员" % key)
        self.assertNotIn("hunter2", json.dumps(theirs, ensure_ascii=False))


class SaveRoundTripTestCase(Base):
    """保存是**整条覆盖**，所以管理员拿到的字段必须够回填一整条。"""

    def test_the_admin_payload_covers_every_field_validate_produces(self):
        """逐项比对：`public_server(admin)` 的键 ⊇ `validate()` 的产出。

        漏一个键的后果是**静默清空**：界面把这条服务原样发回去保存（改个名字、
        点一下停用），服务端把没带的字段当成"没填"而写成空 —— `env` 曾经就会
        这样丢，而界面上没有任何提示。
        """
        fields = mcp.validate({
            "id": "fs", "label": "文件系统", "transport": "stdio",
            "command": "npx", "args": ["-y", "srv"], "env": {"A": "1"},
            "cwd": "/tmp", "level": "audit", "review": False, "timeoutS": 30,
            "enabled": False, "note": "备注", "insecure": True,
        })
        server = self.store.save_mcp_server(fields)
        public = mcp.public_server(self.plane, server, "full", True)
        # 可回填 = validate 认识的**输入键**（id 由路径给，created_by 由服务端填）
        editable = {"id", "label", "transport", "level", "review", "timeoutS",
                    "enabled", "note", "insecure", "command", "args", "env",
                    "cwd", "url", "headers"}
        missing = sorted(editable - set(public))
        self.assertEqual([], missing, "这些字段没有回给管理员，保存时会丢：%s" % missing)
        self.assertEqual(public["env"], {"A": "1"})
        self.assertEqual(public["args"], ["-y", "srv"])
        self.assertFalse(public["review"])

    def test_saving_does_not_clear_the_discovery_cache(self):
        """改一个名称不该把上一次的探测结果清掉。

        清掉的话，界面上"改个错别字"会让工具清单变空，而使用者会以为改坏了。
        """
        self.register()
        self.probe()
        before = len(self.store.get_mcp_server("fx")["tools"])
        self.assertGreater(before, 0)
        self.store.save_mcp_server(dict(stdio_fields(), label="改了名字"))
        row = self.store.get_mcp_server("fx")
        self.assertEqual(row["label"], "改了名字")
        self.assertEqual(len(row["tools"]), before)

    def test_env_and_headers_are_stored_as_json(self):
        self.store.save_mcp_server(mcp.validate({
            "id": "w", "label": "w", "transport": "http", "url": "http://h/mcp",
            "headers": {"Authorization": "Bearer x"}, "level": "full"}))
        row = self.store.get_mcp_server("w")
        self.assertEqual(row["headers"], {"Authorization": "Bearer x"})
        self.assertEqual(row["args"], [])

    def test_a_corrupt_json_column_does_not_break_the_read_path(self):
        """被手工改坏的 JSON 列不该让整个设置页打不开（读路径永不抛）。"""
        self.store.save_mcp_server(stdio_fields())
        self.store.conn.execute("UPDATE mcp_servers SET args='{not json' WHERE id='fx'")
        self.store.conn.commit()
        row = self.store.get_mcp_server("fx")
        self.assertEqual(row["args"], [])

    def test_a_probe_result_is_written_back_on_success_and_failure(self):
        self.register()
        self.probe()
        ok = self.store.get_mcp_server("fx")
        self.assertEqual(ok["status"], "ready")
        self.assertEqual(ok["last_error"], "")
        self.assertTrue(ok["probed_at"])
        mcp.invalidate("fx")
        mcp.probe(self.plane, "fx")           # 再探一次仍然成功
        self.assertEqual(self.store.get_mcp_server("fx")["status"], "ready")


# ------------------------------------------------------------------ 接口

class RouteTestCase(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("mcpapi.db")
        self.httpd, self.client = support.start(self.store)
        self.addCleanup(mcp.close_all)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(support.stop, self.httpd, self.store, None)
        mcp.close_all()

    def payload(self, **overrides):
        data = {"id": "fx", "label": "测试服务", "transport": "stdio",
                "command": sys.executable, "args": ["-u", FIXTURE],
                "level": "full", "review": True, "timeoutS": 20}
        data.update(overrides)
        return data

    def test_the_catalog_needs_a_session(self):
        self.client.logout()
        status, _payload = self.client.get("/api/v1/mcp")
        self.assertEqual(status, 401)

    def test_saving_probes_once_and_returns_the_tools(self):
        status, body = self.client.post("/api/v1/mcp", self.payload())
        self.assertEqual(status, 200, body)
        self.assertTrue(body["probe"]["ok"], body["probe"].get("error"))
        names = [tool["name"] for tool in body["probe"]["tools"]]
        self.assertIn("echo", names)
        self.assertEqual(body["server"]["id"], "fx")

    def test_the_catalog_reports_the_declared_tools(self):
        self.client.post("/api/v1/mcp", self.payload())
        status, body = self.client.get("/api/v1/mcp")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["servers"]), 1)
        self.assertIn("mcp__fx__echo", body["tools"])
        self.assertIn("mcp__<服务id>__<工具名>", body["promptBlock"])
        self.assertIn(body["reviewMode"], harness.MODES)

    def test_only_an_admin_can_save_test_or_call(self):
        """那三件事会写配置或**拉起外部进程**，所以只给管理员。"""
        self.client.post("/api/v1/users", {"username": "op", "password": "Operator-Pass1!",
                                           "role": "operator"})
        self.client.logout()
        status, _payload = self.client.login("op", "Operator-Pass1!")
        self.assertEqual(status, 200)
        for path, body in (("/api/v1/mcp", self.payload()),
                           ("/api/v1/mcp/fx/test", {}),
                           ("/api/v1/mcp/fx/delete", {}),
                           ("/api/v1/mcp/fx/call", {"name": "mcp__fx__echo"})):
            code, payload = self.client.post(path, body)
            self.assertEqual(code, 403, "%s 竟然让非管理员过了" % path)
            self.assertIn("管理员", payload.get("error", ""))

    def test_a_write_needs_the_csrf_header(self):
        status, _payload = self.client.post(
            "/api/v1/mcp", self.payload(), headers={"X-CSRF-Token": ""})
        self.assertIn(status, (400, 403))

    def test_test_and_delete_and_call_round_trip(self):
        self.client.post("/api/v1/mcp", self.payload())
        status, body = self.client.post("/api/v1/mcp/fx/test", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["probe"]["ok"])
        status, body = self.client.post("/api/v1/mcp/fx/call",
                                        {"name": "mcp__fx__echo",
                                         "arguments": {"text": "接口"}})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["data"]["text"], "echo:接口")
        status, body = self.client.post("/api/v1/mcp/fx/delete", {})
        self.assertEqual(status, 200)
        self.assertEqual(0, self.store.count_mcp_servers())

    def test_calling_a_disabled_server_is_refused_and_creates_no_process(self):
        self.client.post("/api/v1/mcp", self.payload(enabled=False))
        status, body = self.client.post("/api/v1/mcp/fx/call",
                                        {"name": "mcp__fx__echo",
                                         "arguments": {"text": "x"}})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn("已被停用", body["error"])

    def test_bad_arguments_are_refused_before_anything_is_started(self):
        self.client.post("/api/v1/mcp", self.payload())
        status, payload = self.client.post("/api/v1/mcp/fx/call", {"name": ""})
        self.assertEqual(status, 400)
        self.assertIn("name", payload.get("error", ""))
        status, payload = self.client.post("/api/v1/mcp/fx/call",
                                          {"name": "mcp__fx__echo",
                                           "arguments": "not an object"})
        self.assertEqual(status, 400)

    def test_a_bad_configuration_is_refused_with_a_reason(self):
        status, payload = self.client.post("/api/v1/mcp", self.payload(id="Bad Id"))
        self.assertEqual(status, 400)
        self.assertIn("服务 id", payload.get("error", ""))

    def test_the_server_count_is_capped(self):
        self.client.post("/api/v1/mcp", self.payload())
        for index in range(mcp.MAX_SERVERS):
            self.store.save_mcp_server(stdio_fields(id="s%d" % index))
        status, payload = self.client.post("/api/v1/mcp", self.payload(id="one-more"))
        self.assertEqual(status, 400)
        self.assertIn("上限", payload.get("error", ""))

    def test_chat_tools_lists_mcp_tools_and_matches_the_prompt(self):
        """「可用能力」里列出的 MCP 工具必须与 `/mcp` 报出的**一致**。

        两处各算一遍会出现"界面写着 3 个、实际只发 1 个"，而那恰好是使用者最想
        确认"我接的服务到底生效没有"的地方。这里比的是两个接口的输出 —— 它们
        读的是同一份缓存，所以一旦有人在前端另算一遍，这一条就会红。
        """
        self.client.post("/api/v1/mcp", self.payload())
        status, body = self.client.get("/api/v1/chat/tools")
        self.assertEqual(status, 200)
        mcp_tools = [tool for tool in body["tools"] if tool.get("source") == "mcp"]
        self.assertTrue(mcp_tools, "工具目录里没有 MCP 工具")
        self.assertTrue(all(tool["name"].startswith("mcp__fx__")
                            for tool in mcp_tools))
        catalog = self.client.get("/api/v1/mcp")[1]
        self.assertEqual(sorted(tool["name"] for tool in mcp_tools),
                         sorted(catalog["tools"]))
        self.assertIn("mcp__<服务id>__<工具名>", body["systemPrompt"])
        self.assertTrue(body["mcp"])


# ------------------------------------------------------------------ 子智能体

class ChildToolTestCase(Base):
    """子智能体：外部工具必须**逐个点名**，而且不能比父会话更宽。"""

    def setUp(self):
        super().setUp()
        self.register()
        self.probe()

    def test_mcp_tools_are_absent_when_nothing_is_named(self):
        """不点名时孩子拿不到外部工具 —— 与利用类工具同一条规矩。"""
        spec = subagent.Spec("r", "x")
        names, _dropped = subagent.resolve_tools(spec, "full", "", False,
                                                plane=self.plane)
        self.assertFalse([name for name in names if mcp.owns(name)])

    def test_naming_an_mcp_tool_gives_it_and_its_schema(self):
        spec = subagent.Spec("r", "x", wanted=["mcp__fx__echo"])
        names, dropped = subagent.resolve_tools(spec, "full", "", False,
                                                plane=self.plane)
        self.assertIn("mcp__fx__echo", names)
        self.assertEqual([], dropped)
        schemas = mcp.declarations_for(self.plane, names, "full", "", False)
        self.assertEqual([item["name"] for item in schemas], ["mcp__fx__echo"])

    def test_naming_an_mcp_tool_above_the_policy_is_dropped_with_the_right_reason(self):
        self.store.save_mcp_server(dict(stdio_fields(), level="audit"))
        mcp.invalidate("fx")
        spec = subagent.Spec("r", "x", wanted=["mcp__fx__echo"])
        names, dropped = subagent.resolve_tools(spec, "probe", "", False,
                                                plane=self.plane)
        self.assertNotIn("mcp__fx__echo", names)
        self.assertEqual(len(dropped), 1)
        self.assertIn("权限档", dropped[0]["reason"])
        self.assertNotIn("不存在这个工具", dropped[0]["reason"])

    def test_an_unknown_mcp_name_is_not_called_a_missing_tool(self):
        spec = subagent.Spec("r", "x", wanted=["mcp__ghost__x"])
        _names, dropped = subagent.resolve_tools(spec, "full", "", False,
                                                plane=self.plane)
        self.assertEqual(len(dropped), 1)
        self.assertIn("没有登记过", dropped[0]["reason"])

    def test_without_a_plane_a_named_mcp_tool_is_accepted_then_gated_at_call_time(self):
        """没有 plane 时先收下 —— 真正的拒绝在 `chat.check_tool`。

        这一条钉住的是"候选集只是提前拦"，而不是"唯一的一道闸"。少了它，直接调用
        `resolve_tools` 的调用方（命令行、测试）会拿到一个与真实行为不一致的回答。
        """
        spec = subagent.Spec("r", "x", wanted=["mcp__fx__echo"])
        names, _dropped = subagent.resolve_tools(spec, "full", "", False)
        self.assertIn("mcp__fx__echo", names)


if __name__ == "__main__":
    unittest.main()
