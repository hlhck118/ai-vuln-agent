# -*- coding: utf-8 -*-
"""DeepSeek 官方接口定义的合规性。

这一组的核心手法是：**假服务端按官方规则把违规请求拒掉**。

官方定义里有一条硬规则——带 `tools` 的请求，此前每一轮的 `reasoning_content`
都必须原样回灌，否则 400。这种规则光靠"读代码看有没有这个字段"是钉不住的：
字段可能出现又消失（历史重建、流式累积、跨轮次各自一处），而每一处单看都对。
所以 `DeepSeekEndpoint` 在这里充当契约的执行者：它检查工具请求里每条
assistant 历史是否带 `reasoning_content`，缺了就按官方的样子回 400。

于是"平台合规"这件事变成一个可执行的断言：只要有一条路径漏了，那个用例就会
拿到 400 并失败，而不是等到真实调用时在第 2 轮才炸。

另外两类被钉住的东西：

* **官方取值与默认值**。base_url 不带 `/v1`、模型名是当前官方那两个、
  `thinking` / `reasoning_effort` 的语义（空串 = 不下发 = 用官方默认）、
  thinking 模式下 temperature 不生效、`stream_options` 只能与 stream 同用。
* **老配置的升级**。库里已经存着 `openai_compat` + `/v1` + `deepseek-chat` 的
  配置，建列不会修正它们；升级必须是无歧义的、可重复的、且留下痕迹。
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

from pentestd import providers                                    # noqa: E402
from pentestd.store import Store                                  # noqa: E402
from tests import support                                         # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


# ------------------------------------------------------------ 假 DeepSeek 端点

class DeepSeekEndpoint:
    """按 **官方定义** 应答的假端点：不合规的请求会被它拒掉。

    它做三件真实端点会做的事：

    1. 校验"带 tools 就必须回灌 reasoning_content"（缺了就 400）；
    2. 每次回复都带 `reasoning_content`（思考模式默认开启）；
    3. `stream: true` 时按 SSE 回，最后一片带 usage。
    """

    def __init__(self, script=None, enforce=True, stream_safe=True):
        # script: 依次返回的响应体；用完就重复最后一个
        self.script = list(script or [])
        self.enforce = enforce
        self.stream_safe = stream_safe
        self.requests = []
        self.violations = []
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
                record = {"path": self.path,
                          "headers": {k.lower(): v for k, v in self.headers.items()},
                          "body": body}
                outer.requests.append(record)
                problem = outer.contract_violation(body)
                if problem:
                    outer.violations.append(problem)
                    outer._send_json(self, 400, {
                        "error": {"message": problem,
                                  "type": "invalid_request_error",
                                  "code": "invalid_request_error"}})
                    return
                index = min(len(outer.requests) - 1, max(len(outer.script) - 1, 0))
                reply = outer.script[index] if outer.script else outer.default_reply()
                if body.get("stream"):
                    outer._send_sse(self, reply)
                else:
                    outer._send_json(self, 200, reply)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    # ---- 官方契约（这就是被钉住的那条规则）

    @staticmethod
    def contract_violation(body):
        """带 tools 的请求里，每条 assistant 历史都必须带 reasoning_content。"""
        if not body.get("tools"):
            return ""
        for message in body.get("messages") or []:
            if message.get("role") != "assistant":
                continue
            if not message.get("reasoning_content"):
                return ("Missing reasoning_content in assistant message; when the "
                        "request carries tools, reasoning_content of all previous "
                        "turns must be passed back (400)")
        return ""

    def default_reply(self):
        return {"id": "cmpl-1", "object": "chat.completion",
                "model": "deepseek-flash",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": "好的。",
                                         "reasoning_content": "思考：先看看情况。"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20,
                          "total_tokens": 30}}

    # ---- 传输

    @staticmethod
    def _send_json(handler, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    @staticmethod
    def _send_sse(handler, reply):
        message = ((reply.get("choices") or [{}])[0].get("message") or {})
        finish = (reply.get("choices") or [{}])[0].get("finish_reason") or "stop"
        frames = []
        for piece in ("思考", "：先看看情况。"):
            frames.append({"choices": [{"index": 0, "delta": {
                "reasoning_content": piece}}]})
        for piece in ("好", "的。"):
            frames.append({"choices": [{"index": 0, "delta": {"content": piece}}]})
        frames.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        # 官方 stream_options 语义：只有最后一片带真实 usage
        frames.append({"choices": [], "usage": {"prompt_tokens": 10,
                                                "completion_tokens": 20,
                                                "total_tokens": 30}})
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        for frame in frames:
            handler.wfile.write(("data: %s\n\n" % json.dumps(
                frame, ensure_ascii=False)).encode("utf-8"))
        handler.wfile.write(b"data: [DONE]\n\n")
        # 不声明 Content-Length，靠连接关闭收尾
        handler.close_connection = True

    @property
    def base_url(self):
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d" % (host, port)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def last(self):
        return self.requests[-1] if self.requests else None

    def assistant_messages(self, index=-1):
        body = self.requests[index]["body"] if self.requests else {}
        return [m for m in body.get("messages") or []
                if m.get("role") == "assistant"]


def tool_then_answer(endpoint_name="scan_tasks"):
    """第一轮工具调用（带 reasoning），第二轮给结论。"""
    return [
        {"id": "cmpl-a", "object": "chat.completion", "model": "deepseek-flash",
         "choices": [{"index": 0, "finish_reason": "tool_calls",
                      "message": {"role": "assistant", "content": "",
                                  "reasoning_content": "思考-1：先查现状再决定。",
                                  "tool_calls": [{"id": "call_1", "type": "function",
                                                  "function": {
                                                      "name": endpoint_name,
                                                      "arguments": json.dumps(
                                                          {"path": "/srv/app"})}}]}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}},
        {"id": "cmpl-b", "object": "chat.completion", "model": "deepseek-flash",
         "choices": [{"index": 0, "finish_reason": "stop",
                      "message": {"role": "assistant", "content": "结论：已看完。",
                                  "reasoning_content": "思考-2：任务已列全，可以收尾。"}}],
         "usage": {"prompt_tokens": 30, "completion_tokens": 40, "total_tokens": 70}},
    ]


def deepseek_profile(**overrides):
    profile = {"provider": "deepseek", "baseUrl": "https://api.deepseek.com",
               "model": "deepseek-flash", "maxTokens": 4096, "temperature": 0.2}
    profile.update(overrides)
    return profile


TOOLS = [{"name": "scan_tasks", "description": "列出任务",
          "parameters": {"type": "object", "properties": {}}}]
HISTORY = [
    {"role": "user", "text": "先看看"},
    {"role": "assistant", "text": "我去列任务",
     "reasoning": "思考-1：先查现状",
     "toolCalls": [{"id": "call_1", "name": "scan_tasks",
                    "arguments": {"path": "/srv/app"}}]},
    {"role": "tool", "toolCallId": "call_1", "toolName": "scan_tasks",
     "text": "共 3 个任务"},
]


def body_of(profile, tools=TOOLS, stream=False, messages=None, system="SYSTEM"):
    request = providers.build_chat_request(
        profile, "sk-test", list(HISTORY if messages is None else messages),
        tools, system, stream=stream)
    return request.full_url, json.loads(request.data.decode("utf-8"))


# ------------------------------------------------------------ 协议与预设

class ProtocolCatalogTestCase(unittest.TestCase):
    """DeepSeek 必须是**自己的协议**，不能继续挂在 openai_compat 下。"""

    def test_deepseek_is_its_own_protocol(self):
        self.assertIn("deepseek", providers.PROVIDERS)
        self.assertEqual(providers.PROVIDER_LABELS["deepseek"], "DeepSeek 官方")

    def test_preset_points_at_the_official_endpoint(self):
        preset = providers.get_preset("deepseek")
        self.assertIsNotNone(preset)
        self.assertEqual(preset["provider"], "deepseek")
        # 官方 base_url 不带 /v1。留着也不会立刻报错（网关的鉴权闸门在路由之前，
        # 401 无法区分路径是否存在），但"按官方定义"就是这一条。
        self.assertEqual(preset["baseUrl"], "https://api.deepseek.com")
        self.assertNotIn("/v1", preset["baseUrl"])
        self.assertEqual(preset["models"], ["deepseek-flash", "deepseek-v4-pro"])

    def test_legacy_model_names_are_gone_from_the_catalog(self):
        """`deepseek-chat` / `deepseek-reasoner` 已不在官方模型表里。"""
        models = providers.get_preset("deepseek")["models"]
        for retired in ("deepseek-chat", "deepseek-reasoner"):
            self.assertNotIn(retired, models)
        # 但它们仍能被自愈到现名，老配置不会因为改名而彻底失效
        self.assertEqual(providers.DEEPSEEK_RETIRED_MODELS["deepseek-chat"],
                         "deepseek-flash")

    def test_effort_aliases_follow_the_official_table(self):
        table = {"minimal": "low", "low": "low", "medium": "high", "high": "high",
                 "xhigh": "high", "max": "max", "ultra": "max"}
        for raw, expected in table.items():
            self.assertEqual(providers.normalize_effort(raw), expected, raw)
        self.assertEqual(providers.normalize_effort(""), "")
        self.assertEqual(providers.normalize_effort("bogus"), "")


class ThinkingConfigTestCase(unittest.TestCase):
    """`thinking` 与 `reasoning_effort` 的语义：空串 = 不下发 = 用官方默认。"""

    def test_unset_means_do_not_send_anything(self):
        self.assertEqual(providers.thinking_config({}), {})
        # 官方默认是"开启"，所以不显式下发时"开着"仍然是 True
        self.assertTrue(providers.thinking_enabled({}))

    def test_explicit_toggle_and_effort(self):
        self.assertEqual(providers.thinking_config({"thinking": "disabled"}),
                         {"thinking": {"type": "disabled"}})
        self.assertEqual(
            providers.thinking_config({"thinking": "enabled",
                                       "reasoningEffort": "low"}),
            {"thinking": {"type": "enabled"}, "reasoning_effort": "low"})

    def test_effort_none_disables_thinking(self):
        """官方说 `none` 就是关掉思考，两个参数一起下发不留歧义。"""
        config = providers.thinking_config({"reasoningEffort": "none"})
        self.assertEqual(config, {"thinking": {"type": "disabled"}})
        self.assertFalse(providers.thinking_enabled({"reasoningEffort": "none"}))

    def test_unknown_effort_is_rejected_not_ignored(self):
        """写错的值必须报错，不能静默当默认——否则使用者以为设置生效了。"""
        with self.assertRaises(ValueError):
            providers.thinking_config({"reasoningEffort": "bogus"})


# ------------------------------------------------------------ 请求形状

class ReasoningRoundTripTestCase(unittest.TestCase):
    """`reasoning_content` 回灌：判据是"这一轮有没有 tools"，不是调用方的心情。"""

    def test_reasoning_is_kept_when_tools_are_present(self):
        _url, body = body_of(deepseek_profile())
        assistants = [m for m in body["messages"] if m["role"] == "assistant"]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0]["reasoning_content"], "思考-1：先查现状")
        # 正文与工具调用必须原样保留，多一个字段不能挤掉它们
        self.assertEqual(assistants[0]["content"], "我去列任务")
        self.assertTrue(assistants[0]["tool_calls"])

    def test_reasoning_is_dropped_without_tools(self):
        """不带 tools 时官方会忽略它——不必白付这份 token。"""
        _url, body = body_of(deepseek_profile(), tools=None)
        assistants = [m for m in body["messages"] if m["role"] == "assistant"]
        self.assertNotIn("reasoning_content", assistants[0])

    def test_reasoning_survives_every_assistant_turn(self):
        """官方要求"此前**每一轮**都要回灌"，只带最后一轮是不合规的。"""
        messages = [
            {"role": "user", "text": "第一问"},
            {"role": "assistant", "text": "第一答", "reasoning": "思考-A"},
            {"role": "user", "text": "第二问"},
            {"role": "assistant", "text": "第二答", "reasoning": "思考-B",
             "toolCalls": [{"id": "c1", "name": "scan_tasks", "arguments": {}}]},
            {"role": "tool", "toolCallId": "c1", "toolName": "scan_tasks", "text": "ok"},
        ]
        _url, body = body_of(deepseek_profile(), messages=messages)
        kept = [m.get("reasoning_content") for m in body["messages"]
                if m["role"] == "assistant"]
        self.assertEqual(kept, ["思考-A", "思考-B"])

    def test_empty_reasoning_adds_no_field(self):
        messages = [{"role": "assistant", "text": "无思考", "reasoning": "",
                     "toolCalls": [{"id": "c1", "name": "scan_tasks",
                                    "arguments": {}}]}]
        _url, body = body_of(deepseek_profile(), messages=messages)
        self.assertNotIn("reasoning_content", body["messages"][0])

    def test_openai_compat_never_receives_deepseek_private_fields(self):
        """私有字段不能泄漏给通用端点——那会把本来能用的配置弄坏。"""
        profile = {"provider": "openai_compat", "baseUrl": "https://api.openai.com/v1",
                   "model": "gpt-4o-mini", "maxTokens": 2048, "temperature": 0.2,
                   "userId": "u-1", "thinking": "enabled", "reasoningEffort": "low"}
        _url, body = body_of(profile, stream=True)
        self.assertNotIn("reasoning_content", json.dumps(body))
        for private in ("thinking", "reasoning_effort", "stream_options", "user_id"):
            self.assertNotIn(private, body)
        # 通用端点这边 temperature 照常下发
        self.assertEqual(body["temperature"], 0.2)


class ParameterComplianceTestCase(unittest.TestCase):
    """官方对参数有效性的规定，逐条对齐。"""

    def test_thinking_mode_omits_temperature(self):
        """官方：思考模式下 temperature 不生效（静默忽略），所以不下发。"""
        _url, thinking_body = body_of(deepseek_profile())
        self.assertNotIn("temperature", thinking_body)
        _url, off_body = body_of(deepseek_profile(thinking="disabled"))
        self.assertEqual(off_body["temperature"], 0.2)
        self.assertEqual(off_body["thinking"], {"type": "disabled"})

    def test_stream_options_travels_with_stream(self):
        """官方：stream_options 必须与 stream:true 同用，单独发会 400。"""
        _url, body = body_of(deepseek_profile(), stream=True)
        self.assertTrue(body["stream"])
        self.assertEqual(body["stream_options"], {"include_usage": True})
        _url, plain = body_of(deepseek_profile(), stream=False)
        self.assertNotIn("stream_options", plain)

    def test_unset_max_tokens_uses_the_official_default(self):
        """不下发 max_tokens，让官方默认生效（非思考 8K / 思考 64K）。"""
        _url, body = body_of(deepseek_profile(maxTokens=0))
        self.assertNotIn("max_tokens", body)
        _url, body = body_of(deepseek_profile(maxTokens=1234))
        self.assertEqual(body["max_tokens"], 1234)

    def test_user_id_is_sanitized_to_the_official_charset(self):
        self.assertEqual(providers.sanitize_user_id("u/1 a:b@c#"), "u-1-a-b-c-")
        self.assertEqual(len(providers.sanitize_user_id("x" * 600)), 512)
        _url, body = body_of(deepseek_profile(userId="u/1 a:b"))
        self.assertEqual(body["user_id"], "u-1-a-b")
        _url, body = body_of(deepseek_profile())
        self.assertNotIn("user_id", body)

    def test_endpoint_is_the_official_path(self):
        url, _body = body_of(deepseek_profile())
        self.assertEqual(url, "https://api.deepseek.com/chat/completions")

    def test_tool_choice_is_never_required_in_thinking_mode(self):
        """官方：思考模式下 required 与具名 tool_choice 会 400。"""
        _url, body = body_of(deepseek_profile())
        self.assertEqual(body["tool_choice"], "auto")


# ------------------------------------------------------------ 契约（端到端）

class OfficialContractTestCase(unittest.TestCase):
    """假端点按官方规则拒人，平台必须一路都对。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("ds.db")
        self.httpd, self.client = support.start(self.store, WEB_DIR)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.dir)

    def _profile(self, endpoint, **overrides):
        body = {"name": "DeepSeek 官方", "provider": "deepseek",
                "baseUrl": endpoint.base_url, "model": "deepseek-flash",
                "apiKeySource": "inline", "apiKey": "sk-test",
                "maxTokens": 4096, "temperature": 0.2}
        body.update(overrides)
        status, payload = self.client.post("/api/v1/models", body)
        self.assertEqual(status, 201, payload)
        self.client.post("/api/v1/models/%s/activate" % payload["profile"]["id"], {})
        return payload["profile"]

    def test_connectivity_test_passes_and_warns_about_small_budget(self):
        endpoint = DeepSeekEndpoint()
        try:
            profile = self._profile(endpoint, maxTokens=2048)
            status, result = self.client.post(
                "/api/v1/models/%s/test" % profile["id"], {})
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"], result.get("message"))
            self.assertIn("reasoning_effort", result.get("warning", ""),
                          "思考模式开着却没配 effort，应当被提示")
            # 提示要落库：刷新页面之后再回来看设置页时仍然要看得到
            _status, listing = self.client.get("/api/v1/models")
            stored = [item for item in listing["profiles"]
                      if item["id"] == profile["id"]][0]
            self.assertIn("warning", stored["lastTest"] or {})
        finally:
            endpoint.stop()

    def test_tool_loop_satisfies_the_reasoning_contract(self):
        """工具循环的第二轮必须带上第一轮的 reasoning，否则端点回 400。"""
        endpoint = DeepSeekEndpoint(script=tool_then_answer())
        try:
            self._profile(endpoint)
            status, payload = self.client.post("/api/v1/chat",
                                               {"message": "看看有什么任务"})
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload.get("ok"), payload.get("error"))
            self.assertEqual(endpoint.violations, [],
                             "带 tools 的请求里漏了 reasoning_content：%s"
                             % endpoint.violations)
            self.assertGreaterEqual(len(endpoint.requests), 2,
                                    "这一轮应当至少发生两次模型调用")
            # 第二轮那一次请求里，第一轮的 assistant 必须带着它的思考过程
            second = [m for m in endpoint.requests[-1]["body"]["messages"]
                      if m["role"] == "assistant"]
            self.assertTrue(second, "第二轮请求里应当有 assistant 历史")
            self.assertEqual(second[0].get("reasoning_content"),
                             "思考-1：先查现状再决定。")
        finally:
            endpoint.stop()

    def test_reasoning_is_persisted_so_later_turns_still_comply(self):
        """跨用户轮次也要合规：历史重建不能把 reasoning 丢掉。"""
        endpoint = DeepSeekEndpoint(script=tool_then_answer() + tool_then_answer())
        try:
            self._profile(endpoint)
            _status, first = self.client.post("/api/v1/chat", {"message": "第一问"})
            status, second = self.client.post(
                "/api/v1/chat",
                {"message": "第二问", "sessionId": first["sessionId"]})
            self.assertEqual(status, 200, second)
            self.assertTrue(second.get("ok"), second.get("error"))
            self.assertEqual(endpoint.violations, [],
                             "第二轮用户消息之后的请求丢了历史 reasoning：%s"
                             % endpoint.violations)
            # 落库的 assistant 消息里必须留得住思考过程
            _status, detail = self.client.get(
                "/api/v1/chat/sessions/%s" % first["sessionId"])
            stored = [m for m in detail["messages"] if m["role"] == "assistant"]
            self.assertTrue(any(m.get("reasoning") for m in stored),
                            "思考过程没有落库，下一轮就无法回灌")
        finally:
            endpoint.stop()

    def test_streamed_reasoning_is_separated_from_the_answer(self):
        """流式路径：思考增量单独成事件，正文不受污染，最终一起落库。"""
        endpoint = DeepSeekEndpoint()
        try:
            self._profile(endpoint)
            events = self._read_sse({"message": "在吗"})
            names = [name for name, _data in events]
            self.assertIn("reasoning", names,
                          "流式没有把思考过程作为独立事件交出来：%s" % names)
            self.assertIn("done", names)
            self.assertEqual(endpoint.violations, [])

            thinking = "".join(data.get("text") or ""
                               for name, data in events if name == "reasoning")
            self.assertIn("先看看情况", thinking)

            # 关键：思考内容不能混进正文。混进去就没法折叠、也没法单独回灌。
            answers = [data.get("text") or ""
                       for name, data in events if name == "delta"]
            self.assertNotIn("先看看情况", "".join(answers))

            # 落库的那条 assistant 消息必须两样都有——下一轮要靠 reasoning 回灌
            persisted = [data.get("message") or {}
                         for name, data in events if name == "persist"]
            assistant = [m for m in persisted if m.get("role") == "assistant"]
            self.assertTrue(assistant, "这一轮没有落库任何 assistant 消息")
            self.assertIn("先看看情况", assistant[0].get("reasoning") or "")
            self.assertIn("好", assistant[0].get("text") or "")
        finally:
            endpoint.stop()

    def _read_sse(self, body):
        """读一次流式对话，返回 [(事件名, 数据)]。

        这里必须绕开 `support.Client` 的 JSON 解析：`/chat/stream` 是 SSE，
        用 `client.post` 读会在 `json.loads` 那一步炸掉——而那跟被测行为无关。
        """
        request = urllib.request.Request(
            self.client.base + "/api/v1/chat/stream",
            data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream",
                     "X-CSRF-Token": self.client.csrf})
        events = []
        with self.client.opener.open(request, timeout=30) as response:
            event, data = "", []
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    if data:
                        events.append((event or "message",
                                       json.loads("\n".join(data))))
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
        return events


# ------------------------------------------------------------ 落库与迁移

class ProfileStorageTestCase(unittest.TestCase):
    """思考模式两列：能存、能读、非法值被拒。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("dsstore.db")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _save(self, **overrides):
        args = dict(profile_id="", name="DS", provider="deepseek",
                    base_url="https://api.deepseek.com", model="deepseek-flash",
                    api_key_source="none", api_key_ref="", api_key_secret="",
                    temperature=0.2, max_tokens=4096, timeout_s=90, note="")
        args.update(overrides)
        return self.store.save_profile(**args)

    def test_thinking_fields_round_trip(self):
        saved = self._save(thinking="enabled", reasoning_effort="low")
        self.assertEqual(saved["thinking"], "enabled")
        self.assertEqual(saved["reasoningEffort"], "low")
        again = self.store.get_profile(saved["id"])
        self.assertEqual(again["thinking"], "enabled")
        self.assertEqual(again["reasoningEffort"], "low")

    def test_effort_is_normalized_on_write(self):
        saved = self._save(reasoning_effort="minimal")
        self.assertEqual(saved["reasoningEffort"], "low")

    def test_illegal_values_are_rejected(self):
        from pentestd.models import ValidationError
        with self.assertRaises(ValidationError):
            self._save(reasoning_effort="bogus")
        with self.assertRaises(ValidationError):
            self._save(thinking="maybe")

    def test_blank_stays_blank(self):
        saved = self._save()
        self.assertEqual(saved["thinking"], "")
        self.assertEqual(saved["reasoningEffort"], "")


class LegacyUpgradeTestCase(unittest.TestCase):
    """老配置自愈：provider / base_url / 已下线的模型名。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("dslegacy.db")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _seed(self, provider, base_url, model, note="", name="老配置"):
        saved = self.store.save_profile(
            "", name, provider, base_url, model, "none", "", "",
            0.7, 2048, 90, note,
            thinking="", reasoning_effort="")
        return saved["id"]

    def _reopen(self):
        """模拟"重启后再次打开同一个库"——升级必须可重复。"""
        path = self.store.path
        self.store.close()
        self.store = Store(path)
        return self.store

    def test_legacy_deepseek_profile_is_upgraded(self):
        profile_id = self._seed("openai_compat", "https://api.deepseek.com/v1",
                               "deepseek-chat")
        # 未升级时它会被当成通用兼容端点——那正是工具循环第二轮 400 的成因
        self.assertEqual(self.store.get_profile(profile_id)["provider"],
                         "openai_compat")
        self._reopen()
        upgraded = self.store.get_profile(profile_id)
        self.assertEqual(upgraded["provider"], "deepseek")
        self.assertEqual(upgraded["baseUrl"], "https://api.deepseek.com")
        self.assertEqual(upgraded["model"], "deepseek-flash")
        # 改了什么必须留下痕迹：悄悄换掉模型名，使用者下次会找不到线索
        self.assertIn("[已迁移]", upgraded["note"])
        self.assertIn("deepseek-chat", upgraded["note"])

    def test_upgrade_is_idempotent(self):
        profile_id = self._seed("openai_compat", "https://api.deepseek.com/v1",
                               "deepseek-reasoner")
        self._reopen()
        first = self.store.get_profile(profile_id)["note"]
        self._reopen()
        second = self.store.get_profile(profile_id)["note"]
        self.assertEqual(first, second, "重复打开库不能反复追加迁移痕迹")
        self.assertEqual(second.count("[已迁移]"), 1)

    def test_beta_endpoint_is_left_alone(self):
        """`/beta` 是官方 strict 工具模式的入口，改写它会静默关掉一个配置。"""
        profile_id = self._seed("openai_compat", "https://api.deepseek.com/beta",
                               "deepseek-flash")
        self._reopen()
        upgraded = self.store.get_profile(profile_id)
        self.assertEqual(upgraded["provider"], "deepseek")
        self.assertTrue(upgraded["baseUrl"].endswith("/beta"),
                        upgraded["baseUrl"])
        self.assertNotIn("去掉 /v1", upgraded["note"])

    def test_other_providers_are_not_touched(self):
        openai_id = self._seed("openai_compat", "https://api.openai.com/v1",
                               "gpt-4o-mini", note="别动我")
        ollama_id = self._seed("ollama", "http://127.0.0.1:11434", "qwen2.5:14b")
        self._reopen()
        self.assertEqual(self.store.get_profile(openai_id)["baseUrl"],
                         "https://api.openai.com/v1")
        self.assertEqual(self.store.get_profile(openai_id)["note"], "别动我")
        self.assertEqual(self.store.get_profile(ollama_id)["provider"], "ollama")

    def test_existing_note_is_appended_not_lost(self):
        profile_id = self._seed("openai_compat", "https://api.deepseek.com/v1",
                               "deepseek-chat", note="生产环境专用")
        self._reopen()
        note = self.store.get_profile(profile_id)["note"]
        self.assertIn("[已迁移]", note)
        self.assertIn("生产环境专用", note)


# ------------------------------------------------------------ 诊断

class DiagnosticsTestCase(unittest.TestCase):
    """"能连上但会咬人"的那些组合要被说出来，而不是等出事。"""

    def test_abnormal_finish_reasons_are_explained(self):
        self.assertEqual(providers.finish_note("stop"), "")
        self.assertEqual(providers.finish_note("tool_calls"), "")
        self.assertIn("资源", providers.finish_note("insufficient_system_resource"))
        self.assertIn("中断", providers.finish_note("aborted"))
        # length 是"话被截断"，在思考模式里尤其常见（推理也吃 completion 预算）
        self.assertIn("截断", providers.finish_note("length"))

    def test_small_budget_in_thinking_mode_is_warned(self):
        warning = providers.deepseek_config_warning(
            {"provider": "deepseek", "maxTokens": 2048,
             "reasoningEffort": "high"})
        self.assertIn("max_tokens", warning)
        self.assertIn("length", warning)

    def test_no_warning_when_thinking_is_off(self):
        warning = providers.deepseek_config_warning(
            {"provider": "deepseek", "maxTokens": 512, "thinking": "disabled"})
        self.assertEqual(warning, "")

    def test_no_warning_for_other_providers(self):
        warning = providers.deepseek_config_warning(
            {"provider": "openai_compat", "maxTokens": 128})
        self.assertEqual(warning, "")

    def test_thinking_ping_gets_a_budget_that_can_actually_answer(self):
        """8 个 token 在思考模式下产不出 content，会让测试假失败。"""
        request = providers.build_request(
            {"provider": "deepseek", "baseUrl": "https://api.deepseek.com",
             "model": "deepseek-flash", "maxTokens": 8}, "sk-test")
        body = json.loads(request.data.decode("utf-8"))
        self.assertGreaterEqual(body["max_tokens"],
                               providers.DEEPSEEK_PING_MIN_TOKENS)
        # 关掉思考时不该被抬预算
        request = providers.build_request(
            {"provider": "deepseek", "baseUrl": "https://api.deepseek.com",
             "model": "deepseek-flash", "maxTokens": 8, "thinking": "disabled"},
            "sk-test")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["max_tokens"], 8)


class FailureShapeTestCase(unittest.TestCase):
    """失败的返回值必须与成功同一组键——否则调用方按统一形状取值会炸。"""

    def test_chat_once_failure_has_the_same_keys_as_success(self):
        result = providers.chat_once(
            {"provider": "deepseek", "baseUrl": "http://127.0.0.1:1",
             "model": "deepseek-flash", "timeoutS": 2}, "sk-test", [])
        self.assertFalse(result["ok"])
        for key in ("text", "toolCalls", "reasoning", "finishReason", "latencyMs"):
            self.assertIn(key, result, "失败结果缺字段 %s" % key)

    def test_bad_config_failure_also_keeps_the_shape(self):
        result = providers.chat_once(
            {"provider": "deepseek", "baseUrl": "", "model": "deepseek-flash"},
            "sk-test", [])
        self.assertFalse(result["ok"])
        for key in ("text", "toolCalls", "reasoning", "finishReason"):
            self.assertIn(key, result)


if __name__ == "__main__":
    unittest.main()
