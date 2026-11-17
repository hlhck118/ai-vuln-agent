# -*- coding: utf-8 -*-
"""AI 对话的流式通道（`POST /api/v1/chat/stream`）验证。

流式最容易出的缺陷不是"没流起来"，而是**流起来之后悄悄少做了事**：少落一条消息、
少判一次权限、少写一条轨迹。界面上一切正常（回答一个字一个字往外冒），只有事后翻
记录才发现工具调用没有留痕，或者只读会话竟然建出了任务。所以这里的重点全部压在
**副作用**上，"事件有没有按顺序到达"只是顺带。

分三层：

1. **分帧**（纯函数）——SSE 与 NDJSON 的解析规则；
2. **累积**（纯函数）——三家协议把碎成片段的工具调用拼回一个对象；
3. **端到端**——真起一个会流式回话的假模型，跑完整条链路，只断言副作用。
"""
import json
import os
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agents, chat, providers, server, streaming        # noqa: E402
from tests import support                                             # noqa: E402
from tests.test_chat import has_tool_result, text_reply               # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


# ------------------------------------------------------------ 假模型：会流式

def sse(payload) -> str:
    return "data: %s\n\n" % json.dumps(payload, ensure_ascii=False)


def openai_text_stream(text: str, size: int = 3):
    """把整句切成若干片发出去，模拟真实的逐字流。"""
    frames = []
    for index in range(0, len(text), size):
        frames.append(sse({"choices": [
            {"delta": {"content": text[index:index + size]}, "finish_reason": None}]}))
    frames.append(sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    frames.append("data: [DONE]\n\n")
    return frames


def openai_empty_stream(reason: str = "length"):
    """只吐思考、正文一片都没有就收尾。

    思考 token 也算 completion：`max_tokens` 小的时候预算在推理阶段就用光，上游
    给出的 `finish_reason` 正是 `length`，而 content 一个字都没有。线上就是这么
    产生"这一轮没有产出正文"的。
    """
    frames = [sse({"choices": [{"delta": {"reasoning_content": "先想了很多。"},
                                 "finish_reason": None}]})]
    frames.append(sse({"choices": [{"delta": {}, "finish_reason": reason}]}))
    frames.append("data: [DONE]\n\n")
    return frames


def openai_tool_stream(name: str, arguments: dict, call_id: str = "call-1",
                       pieces: int = 3):
    """工具调用：`id` 只在第一片、`name` 只出现一次、`arguments` 被切成几段。

    这正是真实现场的样子。把参数一次性整包下发，会让"分片累积"这条逻辑完全没被
    测到——而它恰恰是最容易写错的一段（少拼一片就得到一个截断的 JSON）。
    """
    raw = json.dumps(arguments, ensure_ascii=False)
    step = max(1, len(raw) // max(1, pieces))
    fragments = [raw[i:i + step] for i in range(0, len(raw), step)] or [""]
    frames = [sse({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": call_id, "type": "function",
         "function": {"name": name, "arguments": ""}}]}, "finish_reason": None}]})]
    for fragment in fragments:
        frames.append(sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": fragment}}]},
            "finish_reason": None}]}))
    frames.append(sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}))
    frames.append("data: [DONE]\n\n")
    return frames


class ScriptedStreamModel:
    """脚本模型服务端：`responder(record)` 决定每一次请求怎么回。

    返回值决定形态：

      * `list`         → 逐帧下发 SSE（流式）
      * `dict`         → 整包 JSON（非流式，用于验证回退）
      * `("reject", 原因)` → 400，用来假装"这个端点不吃 `stream: true`"
    """

    def __init__(self, responder):
        self.responder = responder
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _write_json(self, status: int, payload: dict):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                record = {"path": self.path, "body": json.loads(raw or "{}")}
                outer.requests.append(record)
                script = outer.responder(record)
                if isinstance(script, dict):
                    self._write_json(200, script)
                    return
                if isinstance(script, tuple):
                    self._write_json(400, {"error": {"message": script[1]}})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                for frame in script:
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d" % (host, port)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def profile_for(model) -> dict:
    return {"provider": "openai_compat", "baseUrl": model.base_url + "/v1",
            "model": "fake-1", "temperature": 0.2, "maxTokens": 512,
            "timeoutS": 20, "name": "假模型"}


# ------------------------------------------------------------------ 分帧

class SseFramingTestCase(unittest.TestCase):
    """SSE 解析：这些规则写错的症状是"偶发丢字"，而偶发是最难查的一种。"""

    def parse(self, text: str):
        return list(streaming.iter_sse(text.encode("utf-8").splitlines(True)))

    def test_blank_line_dispatches_one_event(self):
        self.assertEqual(self.parse("data: {\"a\":1}\n\n"),
                         [("message", '{"a":1}')])

    def test_multiline_data_is_joined_with_newline(self):
        """`data:` 可以多行；逐行分发会把一条 JSON 拆成几段各解析一次。"""
        self.assertEqual(self.parse("data: {\"a\":\ndata: 1}\n\n"),
                         [("message", '{"a":\n1}')])

    def test_event_name_is_preserved(self):
        """Anthropic 全靠 `event:` 区分 delta 类型，丢了它整条流都读不出来。"""
        self.assertEqual(self.parse("event: content_block_delta\ndata: {}\n\n"),
                         [("content_block_delta", "{}")])

    def test_comment_lines_are_ignored(self):
        """很多网关用 `: keepalive` 保活。当成数据会让整轮解析从这里开始错位。"""
        self.assertEqual(self.parse(": ping\n\ndata: x\n\n"), [("message", "x")])

    def test_two_events_in_one_chunk(self):
        self.assertEqual(self.parse("data: a\n\ndata: b\n\n"),
                         [("message", "a"), ("message", "b")])

    def test_crlf_and_missing_trailing_blank_line(self):
        self.assertEqual(self.parse("data: a\r\n\r\ndata: b"),
                         [("message", "a"), ("message", "b")])

    def test_ndjson_skips_a_torn_line(self):
        """连接被掐断时最后一行常常是半截 JSON，不能因此丢掉前面全部内容。"""
        rows = list(streaming.iter_ndjson(b'{"a":1}\n{"b":2\n{"c":3}\n'.splitlines(True)))
        self.assertEqual([row.get("a") or row.get("c") for row in rows], [1, 3])


# ---------------------------------------------------------------- 分片累积

class AccumulatorTestCase(unittest.TestCase):
    @staticmethod
    def feed(acc, frames):
        """把帧列表按 SSE 规则喂给累积器，返回累积出来的文本。"""
        text = ""
        for frame in frames:
            for name, payload in streaming.iter_sse(
                    frame.encode("utf-8").splitlines(True)):
                text += acc.feed(name, payload)
        return text

    def test_openai_reassembles_a_split_tool_call(self):
        """参数被切成三片，拼起来必须是一个完整可解析的对象。"""
        acc = streaming.OpenAIAccumulator()
        self.feed(acc, openai_tool_stream("start_scan",
                                          {"target": "/srv/app", "objective": "审计"}))
        calls = providers._normalize_calls(acc.calls())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "start_scan")
        self.assertEqual(calls[0]["arguments"]["target"], "/srv/app")
        self.assertEqual(calls[0]["id"], "call-1")

    def test_repeated_tool_name_is_not_concatenated(self):
        """有的网关每个分片都重发一次完整名字。

        无脑 `+=` 会拼出 `scan_tasksscan_tasks`，而症状是"模型调了一个不存在的
        工具"——离真正的原因很远，所以这条单独钉住。
        """
        acc = streaming.OpenAIAccumulator()
        for _ in range(3):
            acc.feed("message", json.dumps({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "list_jobs"}}]}}]}))
        calls = providers.normalize_calls(acc.calls())
        self.assertEqual(calls[0]["name"], "list_jobs")

    def test_multiple_tool_calls_keep_their_order(self):
        """一轮里同时调两个工具：下标不能串台，否则参数会被拼到另一个工具上。"""
        acc = streaming.OpenAIAccumulator()
        for index in (1, 0):
            acc.feed("message", json.dumps({"choices": [{"delta": {"tool_calls": [
                {"index": index, "id": "c%d" % index,
                 "function": {"name": "tool_%d" % index,
                              "arguments": json.dumps({"n": index})}}]}}]}))
        calls = providers._normalize_calls(acc.calls())
        self.assertEqual([call["name"] for call in calls],
                         ["tool_0", "tool_1"])
        self.assertEqual([call["arguments"]["n"] for call in calls], [0, 1])

    def test_anthropic_text_and_tool_blocks(self):
        acc = streaming.AnthropicAccumulator()
        acc.feed("content_block_delta", json.dumps(
            {"index": 0, "delta": {"type": "text_delta", "text": "先看看"}}))
        acc.feed("content_block_start", json.dumps(
            {"index": 1, "content_block": {"type": "tool_use", "id": "tu-1",
                                           "name": "list_jobs"}}))
        acc.feed("content_block_delta", json.dumps(
            {"index": 1, "delta": {"type": "input_json_delta",
                                   "partial_json": '{"status":'}}))
        acc.feed("content_block_delta", json.dumps(
            {"index": 1, "delta": {"type": "input_json_delta",
                                   "partial_json": '"done"}'}}))
        acc.feed("message_delta", json.dumps({"delta": {"stop_reason": "tool_use"}}))
        self.assertEqual(acc.text, "先看看")
        calls = providers._normalize_calls(acc.calls())
        self.assertEqual(calls[0]["name"], "list_jobs")
        self.assertEqual(calls[0]["arguments"], {"status": "done"})

    def test_ollama_reads_whole_tool_calls_from_ndjson(self):
        acc = streaming.OllamaAccumulator()
        acc.feed_json({"message": {"role": "assistant", "content": "",
                                   "tool_calls": [{"function": {
                                       "name": "list_jobs",
                                       "arguments": {"status": "done"}}}]},
                       "done": True, "done_reason": "stop"})
        calls = providers._normalize_calls(acc.calls())
        self.assertEqual(calls[0]["arguments"], {"status": "done"})
        self.assertEqual(acc.finish, "stop")

    def test_tool_call_without_arguments_yields_empty_object(self):
        """无参工具不该因为"没有参数分片"而变成一条无法调用的记录。"""
        acc = streaming.OpenAIAccumulator()
        acc.feed("message", json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "list_executors"}}]}}]}))
        calls = providers._normalize_calls(acc.calls())
        self.assertEqual(calls[0]["arguments"], {})


# --------------------------------------------------- 对话循环（直接驱动生成器）

class StreamTurnTestCase(unittest.TestCase):
    """直接跑 `run_turn_stream`：断连与落库这类行为在这里最容易被精确构造。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("stream-loop.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)
        self.models = []

    def tearDown(self):
        for model in self.models:
            model.stop()
        self.store.close()
        self.dir.cleanup()

    def scripted(self, responder):
        model = ScriptedStreamModel(responder)
        self.models.append(model)
        return model

    def drain(self, model, messages, profile=None, **kwargs):
        events = []
        for event in chat.run_turn_stream(self.plane, profile or profile_for(model),
                                          "k", messages, **kwargs):
            events.append(event)
        return events

    def test_deltas_arrive_before_the_conclusion(self):
        """流式的全部意义就在这个顺序上：先来片段，最后才是结论。"""
        model = self.scripted(lambda record: openai_text_stream("扫描已完成，未发现严重问题。"))
        events = self.drain(model, [{"role": "user", "text": "扫完了吗"}])
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds[0], "turn")
        self.assertLess(kinds.index("delta"), kinds.index("done"))
        self.assertGreater(kinds.count("delta"), 3, "文本没有被切开发送")
        self.assertEqual(events[-1]["text"], "扫描已完成，未发现严重问题。")

    def test_tool_call_streams_through_and_creates_the_job(self):
        """分片拼出来的工具调用必须真的能执行——拼错一个字这里就会失败。"""
        def responder(record):
            if has_tool_result(record):
                return openai_text_stream("已下发。")
            return openai_tool_stream("start_scan",
                                      {"target": "/srv/target",
                                       "objective": "审计上传逻辑"})

        model = self.scripted(responder)
        events = self.drain(model, [{"role": "user", "text": "扫描 /srv/target"}])
        executed = [event for event in events if event["type"] == "tool"]
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0]["name"], "start_scan")
        self.assertTrue(executed[0]["ok"], executed[0])
        jobs = self.store.jobs()
        self.assertEqual(len(jobs), 1, "工具执行了，任务却没建起来")
        self.assertEqual(jobs[0].get("target"), "/srv/target")

    def test_trace_is_attached_before_the_message_is_persisted(self):
        """轨迹要和消息**一起**落库。

        先落库、事后补字段会留下一个"消息在、轨迹还没写"的瞬间，而客户端刷新
        恰好最容易撞上：用户会看到一条没有轨迹的结论，以为这一轮什么工具都没调。
        """
        def responder(record):
            if has_tool_result(record):
                return openai_text_stream("好了")
            return openai_tool_stream("list_executors", {})

        model = self.scripted(responder)
        events = self.drain(model, [{"role": "user", "text": "有节点吗"}])
        last = [event for event in events if event["type"] == "persist"][-1]
        self.assertEqual(last["message"]["role"], "assistant")
        self.assertTrue(last["message"].get("trace"), "结论落库时没有带上调用轨迹")
        self.assertEqual(last["message"]["trace"][0]["tool"], "list_executors")

    def test_unparseable_stream_is_an_error_not_an_empty_answer(self):
        """一个合法分片都没解析出来时，**不能**当成"模型没话说"。

        这里连回退也会一起失败（对方根本不说 JSON），所以最终是一条 error。
        要守住的是：绝不能出现一条空文本的 `done`——那会让用户以为模型答了个空。
        """
        model = self.scripted(lambda record: ["not-an-sse-frame\n", "\n"])
        events = self.drain(model, [{"role": "user", "text": "在吗"}])
        self.assertNotIn("done", [event["type"] for event in events])
        self.assertEqual(events[-1]["type"], "error")
        # 回退前先说明"流式没起来"，用户才知道为什么两次请求都发生了
        notices = [event["message"] for event in events if event["type"] == "notice"]
        self.assertTrue(any("流式" in message for message in notices), notices)
        self.assertEqual(len(model.requests), 2, "应当先试流式、再退回整包")

    def test_endpoint_without_streaming_falls_back_to_whole_response(self):
        """网关不认 `stream: true` 时要能自动退回整包。

        没有这条回退，开流式对所有这类部署都等于把对话功能整个关掉——
        而它们的报错只是一句普通的 400，从错误本身看不出"是流式惹的祸"。
        """
        def responder(record):
            if record["body"].get("stream"):
                return ("reject", "stream is not supported by this endpoint")
            return text_reply("openai_compat", "整包回答")

        model = self.scripted(responder)
        events = self.drain(model, [{"role": "user", "text": "在吗"}])
        notices = [event for event in events if event["type"] == "notice"]
        self.assertTrue(notices, "回退没有任何提示，用户会以为自己看到的是流式")
        self.assertIn("流式", notices[0]["message"])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["text"], "整包回答")
        # 第一次带 stream、第二次不带：回退真的换了请求形态，而不是重试同一个
        self.assertTrue(model.requests[0]["body"].get("stream"))
        self.assertFalse(model.requests[-1]["body"].get("stream"))

    def test_partial_output_is_not_retried(self):
        """已经吐出半句话再回退，用户会看到两段拼接的答案——所以不许回退。"""
        def half_then_break(record):
            return openai_text_stream("开头是这样")

        model = self.scripted(half_then_break)
        # 让流在文本之后立刻断掉：模型只发两片就关闭连接
        events = []
        for event in chat.run_turn_stream(self.plane, profile_for(model), "k",
                                          [{"role": "user", "text": "讲一段"}],
                                          stream=True):
            events.append(event)
        self.assertNotIn("error", [event["type"] for event in events])
        self.assertEqual(len(model.requests), 1, "产出过内容就不该再发一次请求")

    def test_disconnect_stops_at_a_step_boundary(self):
        """客户端走了 → 停在步与步之间，而不是掐在一次工具调用中间。

        半途掐断会留下"任务建了、但没人对它的结论负责"的残缺记录；在步与步之间
        停下，落库的内容始终自洽。
        """
        def responder(record):
            if has_tool_result(record):
                return openai_text_stream("我应该不会再被调用")
            return openai_tool_stream("list_executors", {})

        model = self.scripted(responder)
        state = {"closed": False}
        events = []
        for event in chat.run_turn_stream(self.plane, profile_for(model), "k",
                                          [{"role": "user", "text": "查一下"}],
                                          is_closed=lambda: state["closed"]):
            events.append(event)
            if event["type"] == "tool":
                state["closed"] = True          # 工具刚跑完，客户端就断开了
        self.assertEqual(len(model.requests), 1,
                         "客户端已经断开，却又发了一次模型请求")
        self.assertTrue(events[-1].get("interrupted"))
        # 关键：断连之前真正执行过的调用必须有记录，否则任务的来由就丢了
        persisted = [event["message"] for event in events if event["type"] == "persist"]
        self.assertIn("tool", [message["role"] for message in persisted])
        marker = persisted[-1]
        self.assertTrue(marker.get("interrupted"))
        self.assertEqual(marker["trace"][0]["tool"], "list_executors")

    def test_disconnect_before_the_first_step_makes_no_model_call(self):
        model = self.scripted(lambda record: openai_text_stream("不该被调用"))
        events = list(chat.run_turn_stream(self.plane, profile_for(model), "k",
                                           [{"role": "user", "text": "在吗"}],
                                           is_closed=lambda: True))
        self.assertEqual(model.requests, [])
        self.assertTrue(events[-1].get("interrupted"))

    def test_an_injected_message_joins_the_turn_before_the_next_request(self):
        """插话要在**下一步的请求里**出现，而不是等这一轮结束再开一轮。

        它同时要立刻以 `persist` 落库：使用者点了「插话发送」，屏幕上得先看到那句
        话，再看到模型的下一步——否则按了按钮像是没反应。
        """
        def responder(record):
            if has_tool_result(record):
                return openai_text_stream("好了")
            return openai_tool_stream("list_executors", {})

        model = self.scripted(responder)
        pending = [["先别扫 /tmp，改看 /etc/hosts"]]

        def take():
            return pending.pop(0) if pending else []

        events = list(chat.run_turn_stream(self.plane, profile_for(model), "k",
                                           [{"role": "user", "text": "有节点吗"}],
                                           drain_inbox=take))
        injected = [event["message"] for event in events
                    if event["type"] == "persist"
                    and event["message"].get("injected")]
        self.assertEqual(len(injected), 1, "插话没有作为一条消息落库")
        self.assertEqual(injected[0]["role"], "user")
        self.assertIn("改看 /etc/hosts", injected[0]["text"])
        notices = [event for event in events if event["type"] == "notice"
                   and event.get("kind") == "interject"]
        self.assertTrue(notices, "接进来了却没说一声，用户不知道它生效了")
        self.assertIn("改看 /etc/hosts",
                      json.dumps(model.requests[-1]["body"], ensure_ascii=False),
                      "第二步请求里没有这句插话：模型根本没看见")
        self.assertEqual(events[-1]["text"], "好了")

    def test_an_injected_message_at_the_wrap_up_point_continues_the_turn(self):
        """模型已经准备收尾时才到的那句话，不能让这一轮就这么结束。"""
        calls = {"n": 0}

        def responder(record):
            calls["n"] += 1
            return openai_text_stream("第一版" if calls["n"] == 1 else "第二版")

        model = self.scripted(responder)
        # 第一步取空；"本可收尾"那一刻取到一条；再下一步又取空。
        pending = [[], ["再补一句：先别下结论"]]

        def take():
            return pending.pop(0) if pending else []

        events = list(chat.run_turn_stream(self.plane, profile_for(model), "k",
                                           [{"role": "user", "text": "给个结论"}],
                                           drain_inbox=take))
        self.assertEqual(len(model.requests), 2,
                         "收尾前补的那句没有让这一轮继续，它被留在了空处")
        self.assertEqual(events[-1]["text"], "第二版")
        injected = [event["message"] for event in events
                    if event["type"] == "persist"
                    and event["message"].get("injected")]
        self.assertEqual(len(injected), 1)
        self.assertIn("先别下结论", injected[0]["text"])
        notices = [event["message"] for event in events
                   if event["type"] == "notice"
                   and event.get("kind") == "interject"]
        self.assertTrue(any("接上" in message for message in notices), notices)


# ------------------------------------------------------- 端到端（真 HTTP 流）

    def test_an_empty_answer_says_why_it_was_empty(self):
        """正文为空时必须带上原因。

        `max_tokens` 被思考吃光时，这一轮落库就是一条空正文消息。界面上那句
        "模型只做了思考或工具调用"把"预算太小"说成了模型习惯——刷新之后更是一点
        线索都没有。原因要跟着消息一起落库，并随 `done` 回给界面（客户端拿它续跑）。
        """
        model = self.scripted(lambda record: openai_empty_stream("length"))
        events = self.drain(model, [{"role": "user", "text": "继续"}])
        last = [event for event in events if event["type"] == "persist"][-1]
        self.assertEqual(last["message"]["text"], "")
        self.assertEqual(last["message"]["finishNote"], "达到 maxTokens 上限，回答被截断")
        done = [event for event in events if event["type"] == "done"][-1]
        self.assertEqual(done["finishReason"], "length")
        self.assertEqual(done["finishNote"], "达到 maxTokens 上限，回答被截断")

    def test_a_normal_answer_carries_no_finish_note(self):
        """正常收尾不带"为什么空"：每条消息都多一个字段，读的人会以为出了事。"""
        model = self.scripted(lambda record: openai_text_stream("扫完了，没发现问题。"))
        events = self.drain(model, [{"role": "user", "text": "扫完了吗"}])
        last = [event for event in events if event["type"] == "persist"][-1]
        self.assertNotIn("finishNote", last["message"])
        done = [event for event in events if event["type"] == "done"][-1]
        self.assertEqual(done["finishNote"], "")
        # 正常收尾也要如实报出来（客户端靠它区分"答完了"与"被截断"）。
        self.assertEqual(done["finishReason"], "stop")

    def test_a_budget_starved_answer_is_retried_with_a_wider_budget(self):
        """预算被推理吃光时，平台放大一次预算再要结论，而不是把空话留给用户。

        只把原因写出来还不够：使用者按一次"继续"，平台仍拿同一份 2048 再想一遍，
        推理第一个把钱烧完、正文永远轮不上，界面上就是同一句话反复出现。所以这里
        要看到四件事——第二次请求的 `max_tokens` 更大、结论真的落库、实时气泡也拿到
        这段结论、界面上有一句说明。
        """
        def responder(record):
            if not record["body"].get("stream"):
                return text_reply("deepseek", "结论：面板未授权即可读到项目列表。")
            return openai_empty_stream("length")

        model = self.scripted(responder)
        profile = profile_for(model)
        profile.update({"provider": "deepseek", "thinking": "enabled",
                        "maxTokens": 2048})
        events = self.drain(model, [{"role": "user", "text": "继续"}],
                            profile=profile)
        last = [event for event in events if event["type"] == "persist"][-1]
        self.assertEqual(last["message"]["text"],
                         "结论：面板未授权即可读到项目列表。")
        self.assertNotIn("finishNote", last["message"])
        done = [event for event in events if event["type"] == "done"][-1]
        self.assertEqual(done["text"], "结论：面板未授权即可读到项目列表。")
        self.assertGreaterEqual(model.requests[-1]["body"]["max_tokens"], 8192)
        self.assertTrue([event for event in events
                         if event["type"] == "notice" and "8192" in event["message"]],
                        [event.get("message") for event in events])
        deltas = "".join(event["text"] for event in events
                         if event["type"] == "delta")
        self.assertIn("面板未授权即可读到项目列表", deltas)


class StreamEndpointTestCase(unittest.TestCase):
    """走真实 HTTP：SSE 的传输形态、鉴权，以及**流式通道上的权限闸门**。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("stream-http.db")
        self.models = []
        self.httpd = None

    def tearDown(self):
        for model in self.models:
            model.stop()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.store.close()
        self.dir.cleanup()

    def start(self, responder, **kwargs):
        model = ScriptedStreamModel(responder)
        self.models.append(model)
        self.httpd, self.client = support.start(self.store, WEB_DIR, **kwargs)
        status, _payload = self.client.post("/api/v1/models", {
            "name": "假模型", "provider": "openai_compat",
            "baseUrl": model.base_url + "/v1", "model": "fake-1",
            "apiKeySource": "none", "temperature": 0.2, "maxTokens": 512,
            "timeoutS": 20})
        if status != 201:
            raise RuntimeError("模型登记失败：%s" % status)
        return model

    def open_stream(self, body, csrf=True, path="/api/v1/chat/stream"):
        request = urllib.request.Request(
            self.client.base + path, data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream",
                     "X-CSRF-Token": self.client.csrf if csrf else ""})
        response = self.client.opener.open(request, timeout=30)
        return response, self.read_events(response)

    @staticmethod
    def read_events(response):
        event, data = "", []
        for raw in response:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line == "":
                if data:
                    yield event or "message", json.loads("\n".join(data))
                event, data = "", []
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)

    def collect(self, body, csrf=True, path="/api/v1/chat/stream"):
        response, events = self.open_stream(body, csrf=csrf, path=path)
        with response:
            return dict(response.headers), list(events)

    # ---- 传输形态

    def test_sse_headers_are_what_a_browser_needs_to_stream(self):
        """缺 Content-Type 浏览器不会边收边渲染；缺 Connection: close 会一直转圈等尾巴。"""
        self.start(lambda record: openai_text_stream("ok"))
        headers, _events = self.collect({"message": "在吗"})
        self.assertTrue(headers.get("Content-Type", "").startswith("text/event-stream"))
        self.assertIsNone(headers.get("Content-Length"),
                          "流式响应不该有 Content-Length：发送开始时它还无从确定")
        self.assertEqual(headers.get("Connection"), "close")

    def test_event_order_covers_the_whole_turn(self):
        def responder(record):
            if has_tool_result(record):
                return openai_text_stream("节点已列出。")
            return openai_tool_stream("list_executors", {})

        self.start(responder)
        _headers, events = self.collect({"message": "有哪些节点"})
        names = [name for name, _data in events]
        self.assertEqual(names[0], "start")
        for required in ("turn", "step", "delta", "tool", "persist", "done"):
            self.assertIn(required, names, "缺少事件：%s" % required)
        self.assertEqual(names[-1], "done")
        # 先看到工具跑完，再看到结论——顺序反了就说明是"攒完才发"
        self.assertLess(names.index("tool"), names.index("done"))

    def test_start_event_carries_the_session_so_the_ui_can_follow(self):
        """界面靠 `start` 里的会话 id 把这一轮挂到正确的对话上。"""
        self.start(lambda record: openai_text_stream("好"))
        _headers, events = self.collect({"message": "在吗"})
        first = dict(events[0][1])
        self.assertTrue(first.get("sessionId"))
        self.assertIn("config", first)
        self.assertIn("model", first)

    def test_messages_and_trace_land_in_the_store(self):
        """整轮跑完后，记录必须和整包入口跑出来的完全一样。"""
        def responder(record):
            if has_tool_result(record):
                return openai_text_stream("已下发扫描。")
            return openai_tool_stream("start_scan", {"target": "/srv/target"})

        self.start(responder)
        _headers, events = self.collect({"message": "扫描 /srv/target"})
        session_id = dict(events[0][1])["sessionId"]
        stored = self.store.chat_messages(session_id)
        self.assertEqual([message["role"] for message in stored],
                         ["user", "assistant", "tool", "assistant"])
        self.assertTrue(stored[-1].get("trace"), "结论里没有调用轨迹")
        self.assertEqual(stored[-1]["trace"][0]["tool"], "start_scan")
        self.assertEqual(len(self.store.jobs()), 1)

    # ---- 鉴权

    def test_streaming_requires_a_login_session(self):
        self.start(lambda record: openai_text_stream("ok"))
        anonymous = support.Client(self.client.base)
        status, payload = anonymous.post("/api/v1/chat/stream", {"message": "在吗"})
        self.assertEqual(status, 401, payload)

    def test_streaming_write_requires_the_csrf_header(self):
        """流式是写操作（它会建任务），漏掉 CSRF 检查等于开了一个静默旁路。"""
        self.start(lambda record: openai_text_stream("ok"))
        status, payload = self.client.request("POST", "/api/v1/chat/stream",
                                              {"message": "在吗"}, with_csrf=False)
        self.assertEqual(status, 403, payload)

    def test_missing_message_is_a_plain_json_error(self):
        """参数错误发生在事件流开始之前，所以还是普通的 400。"""
        self.start(lambda record: openai_text_stream("ok"))
        status, payload = self.client.post("/api/v1/chat/stream", {"message": "  "})
        self.assertEqual(status, 400)
        self.assertIn("message", payload["error"])

    # ---- 权限闸门（本模块的重点）

    def test_readonly_session_is_refused_on_the_streaming_path(self):
        """清单是提示，闸门在执行——流式这条新路也必须过同一道闸。

        这条用例针对的缺陷很具体：把权限判断只做在整包入口。漏一次检查的后果是
        **只读会话建出了任务**，而界面上看起来只是"模型调了个工具、失败了"。
        """
        def responder(record):
            if has_tool_result(record):
                return openai_text_stream("已尝试。")
            return openai_tool_stream("start_probe",
                                      {"target": "127.0.0.1", "ports": "80"})

        self.start(responder, probe_scope=["127.0.0.0/8"])
        _headers, events = self.collect({
            "message": "探测 127.0.0.1 的 80 端口", "policyId": "readonly"})
        executed = [data for name, data in events if name == "tool"]
        self.assertEqual(len(executed), 1)
        self.assertFalse(executed[0]["ok"], "只读会话竟然执行了探测")
        self.assertIn("权限拒绝", executed[0]["brief"])
        self.assertEqual(self.store.jobs(), [], "只读会话不得产生任务")

    def test_highest_level_still_obeys_the_scope_gate_when_streaming(self):
        def responder(record):
            if has_tool_result(record):
                return openai_text_stream("已尝试。")
            return openai_tool_stream("start_probe",
                                      {"target": "8.8.8.8", "ports": "80"})

        self.start(responder, probe_scope=["127.0.0.0/8"])
        _headers, events = self.collect({"message": "探测 8.8.8.8", "policyId": "full"})
        executed = [data for name, data in events if name == "tool"]
        self.assertFalse(executed[0]["ok"], "越界目标竟然通过了")
        self.assertIn("作用域", executed[0]["brief"])
        self.assertEqual(self.store.jobs(), [], "越界目标不得产生任务")

    def test_entry_choices_sent_with_the_message_are_persisted(self):
        """界面上的"改完直接发"只有一次往返；流式这条路上也必须落库。

        漏掉这一步的症状很隐蔽：这一轮确实按新档位跑了，但下一轮又回到旧设置，
        而用户会认为"切换有时候生效、有时候不生效"。
        """
        self.start(lambda record: openai_text_stream("好"))
        _headers, events = self.collect({"message": "在吗", "policyId": "readonly"})
        session_id = dict(events[0][1])["sessionId"]
        meta = self.store.get_chat_session(session_id)
        self.assertEqual(meta["policyId"], "readonly")

    def test_model_failure_is_reported_inside_the_stream(self):
        """模型挂了不能只体现在"流断了"——客户端要能拿到原因。"""
        self.start(lambda record: {"error": {"message": "quota exceeded"}})
        _headers, events = self.collect({"message": "在吗"})
        errors = [data for name, data in events if name == "error"]
        self.assertTrue(errors, "整轮失败却没有 error 事件")
        self.assertIn("quota", errors[0]["error"])


    # ---- 气泡上那个"最多几步"的语义

    def preset(self, handle, steps):
        """建一条步数预算为 `steps` 的 Agent 预设，返回它的 id。"""
        status, payload = self.client.post("/api/v1/agents/presets", {
            "label": "预算试档", "handle": handle,
            "prompt": "只回答「好」。", "summary": "试预算", "note": "",
            "steps": steps, "skills": []})
        self.assertEqual(status, 201, payload)
        return payload["preset"]["id"]

    def test_a_ceiling_budget_is_declared_as_a_ceiling_on_both_events(self):
        """使用者把预设调到顶格时，两个事件都要说"这只是平台上限"。

        界面据此不再显示「最多 99999 步」——那个数拦不住任何人，它只是把配置念一遍，
        而徽章前半句已经就是"本轮实际步数"。所以服务端必须**说出来**，不能让前端自己
        去比一个 99999：那个常数在 `agents` 里，抄一份到 JS 就会像刚才删掉的那个
        `AGENT_STEPS_MAX = 40` 一样，改了上限而界面不跟。

        两个事件都钉，是因为界面两处都读（`start` 先到、`turn` 随后覆盖）：只报一处
        会让这个字段有没有生效取决于两个事件的到达顺序，而那种缺陷在快机器上不复现。
        """
        self.start(lambda record: openai_text_stream("好"))
        preset_id = self.preset("steps-ceiling", agents.MAX_STEPS)
        _headers, events = self.collect({"message": "在吗", "agentId": preset_id})
        first = dict(events[0][1])
        turn = dict([data for name, data in events if name == "turn"][0])
        self.assertEqual(first["maxSteps"], agents.MAX_STEPS)
        self.assertIs(first["stepsAtCeiling"], True, "start 事件没有说这是顶格")
        self.assertEqual(turn["maxSteps"], agents.MAX_STEPS)
        self.assertIs(turn["stepsAtCeiling"], True, "turn 事件没有说这是顶格")

    def test_a_real_budget_is_not_declared_as_a_ceiling(self):
        """"顶格"不能永远为真，否则真限额的会话也不再显示它的上限。

        真预算会在一轮中途把作业切停，那是使用者唯一能提前看到的信号；这个数在顶格
        时会误导，在 6/12/40 上则是必须的。两侧都断言，才是在测"这个字段跟着预算走"，
        而不是在测"这个字段存在"。
        """
        self.start(lambda record: openai_text_stream("好"))
        preset_id = self.preset("steps-small", 6)
        _headers, events = self.collect({"message": "在吗", "agentId": preset_id})
        turn = dict([data for name, data in events if name == "turn"][0])
        self.assertEqual(turn["maxSteps"], 6)
        self.assertIs(turn["stepsAtCeiling"], False, "6 步的预算被说成了没有上限")


if __name__ == "__main__":
    unittest.main()
