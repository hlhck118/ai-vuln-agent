# -*- coding: utf-8 -*-
"""对话式操作（AI 对话页签）的后端验证。

这里的关键是**用一个真正会说话的假模型服务端**来驱动整条链路：模型返回工具调用
→ 控制面执行工具 → 结果回灌 → 模型给出结论。只测"接口返回 200"是不够的——
对话功能最容易出的故障恰恰是"看起来答了，其实什么都没做"：
模型说要扫描，服务端却把它当成一句普通文本回显给用户，任务根本没建。

因此每条用例都断言**副作用**：任务真的进了队列、选项真的被序列化进工具结果、
越权真的被挡住、步数上限真的不会变成死循环。
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agents, chat, providers, server                  # noqa: E402
from pentestd.store import Store                                   # noqa: E402
from tests import support                                          # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


# ---------------------------------------------------------------- 假模型服务端

def text_reply(protocol: str, text: str) -> dict:
    if protocol == "anthropic":
        return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
    if protocol == "ollama":
        return {"message": {"role": "assistant", "content": text}, "done": True}
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


def tool_reply(protocol: str, name: str, arguments: dict, call_id: str = "call-1") -> dict:
    """按协议把一次工具调用包成响应体（OpenAI 用 JSON 字符串，其余用对象）。"""
    if protocol == "anthropic":
        return {"content": [{"type": "tool_use", "id": call_id, "name": name,
                             "input": arguments}], "stop_reason": "tool_use"}
    if protocol == "ollama":
        return {"message": {"role": "assistant", "content": "",
                            "tool_calls": [{"function": {"name": name,
                                                         "arguments": arguments}}]},
                "done": True}
    return {"choices": [{"message": {"role": "assistant", "content": "",
                                     "tool_calls": [{"id": call_id, "type": "function",
                                                     "function": {
                                                         "name": name,
                                                         "arguments": json.dumps(
                                                             arguments,
                                                             ensure_ascii=False)}}]},
                         "finish_reason": "tool_calls"}]}


class ScriptedModel:
    """脚本化模型服务端：`responder(request) -> dict` 决定每一次请求回什么。"""

    def __init__(self, responder, protocol: str = "openai_compat"):
        self.protocol = protocol
        self.responder = responder
        self.requests = []
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
                reply = outer.responder(record)
                payload = json.dumps(reply or {}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d" % (host, port)

    def last_messages(self):
        return (self.requests[-1]["body"].get("messages") or []) if self.requests else []

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def has_tool_result(record) -> bool:
    """这一轮请求里是否已经带回了工具结果（OpenAI/Anthropic/Ollama 三种形状）。"""
    for message in record["body"].get("messages") or []:
        if message.get("role") == "tool":
            return True
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return True
    return False


# ---------------------------------------------------------------- 协议形状

class ChatProtocolAdapterTestCase(unittest.TestCase):
    """工具声明与消息回灌在三家协议里的形状各不相同，逐条钉住。"""

    def profile(self, provider):
        return {"provider": provider, "baseUrl": "http://127.0.0.1:1/v1",
                "model": "m", "temperature": 0.2, "maxTokens": 1024, "timeoutS": 30}

    def body(self, provider, messages):
        request = providers.build_chat_request(
            self.profile(provider), "k", messages, chat.TOOLS, chat.SYSTEM_PROMPT)
        return json.loads(request.data.decode("utf-8"))

    def test_openai_tools_and_tool_result_shape(self):
        body = self.body("openai_compat", [
            {"role": "assistant", "text": "", "toolCalls": [
                {"id": "call-9", "name": "list_jobs", "arguments": {"status": "done"}}]},
            {"role": "tool", "toolCallId": "call-9", "toolName": "list_jobs",
             "text": "{\"ok\":true}"},
        ])
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["tool_choice"], "auto")
        declaration = body["tools"][0]
        self.assertEqual(declaration["type"], "function")
        self.assertIn("parameters", declaration["function"])
        # 工具调用的 arguments 必须是 **JSON 字符串**，给对象会被 OpenAI 兼容端点拒
        self.assertIsInstance(
            body["messages"][1]["tool_calls"][0]["function"]["arguments"], str)
        self.assertEqual(body["messages"][2]["tool_call_id"], "call-9")

    def test_anthropic_uses_input_schema_and_tool_result_block(self):
        body = self.body("anthropic", [
            {"role": "assistant", "text": "", "toolCalls": [
                {"id": "tu-1", "name": "list_jobs", "arguments": {}}]},
            {"role": "tool", "toolCallId": "tu-1", "toolName": "list_jobs", "text": "{}"},
        ])
        self.assertNotIn("messages", [m for m in body["messages"]
                                      if m.get("role") == "system"])
        self.assertIn("system", body)
        self.assertIn("input_schema", body["tools"][0])
        self.assertNotIn("type", body["tools"][0])
        self.assertEqual([m["role"] for m in body["messages"]], ["assistant", "user"])
        self.assertEqual(body["messages"][0]["content"][0]["type"], "tool_use")
        self.assertEqual(body["messages"][1]["content"][0]["type"], "tool_result")
        self.assertEqual(body["messages"][1]["role"], "user")

    def test_ollama_takes_object_arguments_and_tool_role(self):
        body = self.body("ollama", [
            {"role": "assistant", "text": "", "toolCalls": [
                {"id": "1", "name": "list_jobs", "arguments": {"status": "done"}}]},
            {"role": "tool", "toolName": "list_jobs", "text": "{}"},
        ])
        self.assertIsInstance(
            body["messages"][1]["tool_calls"][0]["function"]["arguments"], dict)
        self.assertEqual(body["messages"][2]["role"], "tool")
        self.assertIn("options", body)

    def test_parse_reply_normalizes_all_three(self):
        parsed = providers.parse_chat_reply("openai_compat", tool_reply(
            "openai_compat", "start_scan", {"target": "/srv/app"}, "call-7"))
        self.assertEqual(parsed["toolCalls"][0]["arguments"]["target"], "/srv/app")
        parsed = providers.parse_chat_reply("anthropic", tool_reply(
            "anthropic", "start_scan", {"target": "/srv/app"}))
        self.assertEqual(parsed["toolCalls"][0]["name"], "start_scan")
        parsed = providers.parse_chat_reply("ollama", tool_reply(
            "ollama", "start_scan", {"target": "/srv/app"}))
        self.assertEqual(parsed["toolCalls"][0]["arguments"]["target"], "/srv/app")

    def test_broken_arguments_are_kept_for_the_model_to_fix(self):
        """截断的 JSON 不能让服务端崩，也不该被猜成一个默认值。"""
        self.assertEqual(providers.normalize_arguments('{"target": "/sr'),
                         {"_raw": '{"target": "/sr'})
        self.assertEqual(providers.normalize_arguments('{"a": 1}'), {"a": 1})
        self.assertEqual(providers.normalize_arguments(None), {})


# ---------------------------------------------------------------- 工具执行

class ToolExecutionTestCase(unittest.TestCase):
    def setUp(self):
        self.dir, self.store = support.temporary_store("tools.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_catalog_covers_scan_and_analysis(self):
        names = {tool["name"] for tool in chat.tool_catalog()}
        for required in ("start_scan", "list_jobs", "get_findings", "get_assets",
                         "get_report", "get_chain_overview", "list_executors"):
            self.assertIn(required, names)
        for tool in chat.tool_catalog():
            self.assertTrue(tool["label"], "工具缺少中文标签：%s" % tool["name"])
            self.assertTrue(tool["description"])

    def test_start_scan_really_creates_a_job(self):
        outcome = chat.execute_tool(self.plane, "start_scan",
                                    {"target": "/srv/app",
                                     "objective": "审计认证逻辑"}, "")
        self.assertTrue(outcome["ok"], outcome["summary"])
        jobs = self.store.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["target"], "/srv/app")
        self.assertIn("jobId", outcome["data"])
        # 目标说明必须真的落进 engagement，而不是只留在对话里
        meta = self.store.get_engagement_meta(jobs[0]["engagementId"])
        self.assertEqual(meta["objective"], "审计认证逻辑")

    def test_start_scan_without_target_asks_instead_of_guessing(self):
        outcome = chat.execute_tool(self.plane, "start_scan", {}, "")
        self.assertFalse(outcome["ok"])
        self.assertIn("target", outcome["summary"])
        self.assertEqual(self.store.jobs(), [])

    def test_unknown_tool_lists_the_real_ones(self):
        outcome = chat.execute_tool(self.plane, "run_shell", {"cmd": "id"}, "")
        self.assertFalse(outcome["ok"])
        self.assertIn("start_scan", outcome["summary"])
        self.assertEqual(self.store.jobs(), [])

    def test_engagement_defaults_to_the_most_recent(self):
        first = chat.execute_tool(self.plane, "start_scan", {"target": "/srv/a"}, "")
        second = chat.execute_tool(self.plane, "start_scan", {"target": "/srv/b"}, "")
        outcome = chat.execute_tool(self.plane, "get_chain_overview", {}, "")
        self.assertTrue(outcome["ok"], outcome["summary"])
        self.assertEqual(outcome["data"]["engagementId"], second["data"]["engagementId"])
        self.assertNotEqual(first["data"]["engagementId"],
                            second["data"]["engagementId"])

    def test_tools_without_engagement_report_the_empty_state(self):
        outcome = chat.execute_tool(self.plane, "get_findings", {}, "")
        self.assertFalse(outcome["ok"])
        self.assertIn("start_scan", outcome["summary"])

    def test_executor_query_explains_the_pending_trap(self):
        outcome = chat.execute_tool(self.plane, "list_executors", {}, "")
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["data"]["executors"], [])
        self.assertIn("pending", outcome["data"]["hint"])


# ---------------------------------------------------------------- 对话循环

class ChatLoopTestCase(unittest.TestCase):
    """用脚本化模型驱动 run_turn：模型说要调工具，任务就必须真的建起来。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("loop.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)
        self.models = []

    def tearDown(self):
        for model in self.models:
            model.stop()
        self.store.close()
        self.dir.cleanup()

    def scripted(self, responder, protocol="openai_compat"):
        model = ScriptedModel(responder, protocol)
        self.models.append(model)
        return model

    def profile_for(self, model):
        return {"provider": model.protocol, "baseUrl": model.base_url + "/v1"
                if model.protocol != "ollama" else model.base_url,
                "model": "fake-1", "temperature": 0.2, "maxTokens": 512,
                "timeoutS": 20}

    def test_tool_call_then_final_answer(self):
        def responder(record):
            if has_tool_result(record):
                return text_reply("openai_compat", "扫描已下发，请稍后用任务列表查询。")
            return tool_reply("openai_compat", "start_scan",
                              {"target": "/srv/target", "objective": "审计上传逻辑"},
                              "call-1")

        model = self.scripted(responder)
        result = chat.run_turn(self.plane, self.profile_for(model), "k",
                               [{"role": "user", "text": "扫描 /srv/target"}])
        self.assertTrue(result["ok"], result.get("message"))
        self.assertIn("已下发", result["text"])
        self.assertEqual(len(result["trace"]), 1)
        self.assertEqual(result["trace"][0]["tool"], "start_scan")
        self.assertTrue(result["trace"][0]["ok"])
        self.assertEqual(len(self.store.jobs()), 1)
        # 回灌的顺序：assistant(工具调用) → tool(结果) → assistant(结论)
        roles = [message["role"] for message in result["appended"]]
        self.assertEqual(roles, ["assistant", "tool", "assistant"])

    def test_model_sees_the_tool_result(self):
        def responder(record):
            if has_tool_result(record):
                return text_reply("openai_compat", "ok")
            return tool_reply("openai_compat", "list_executors", {}, "call-1")

        model = self.scripted(responder)
        chat.run_turn(self.plane, self.profile_for(model), "k",
                      [{"role": "user", "text": "有可用节点吗"}])
        second = model.requests[-1]["body"]["messages"]
        tool_message = [m for m in second if m.get("role") == "tool"][-1]
        payload = json.loads(tool_message["content"])
        self.assertTrue(payload["ok"])
        self.assertIn("executors", payload["data"])

    def test_step_limit_ends_with_a_conclusion(self):
        """反复调用同一工具不会变成死循环：到上限后要求它收尾。"""
        def responder(record):
            messages = record["body"]["messages"]
            if not has_tool_result(record):
                return tool_reply("openai_compat", "list_executors", {}, "call-1")
            if len(record["body"].get("tools") or []) == 0:      # 收尾那一轮没有工具声明
                return text_reply("openai_compat", "没有在线节点，无法推进。")
            return tool_reply("openai_compat", "list_executors", {}, "call-2")

        model = self.scripted(responder)
        result = chat.run_turn(self.plane, self.profile_for(model), "k",
                               [{"role": "user", "text": "扫吧"}], max_steps=2)
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertIn("无法推进", result["text"])
        self.assertEqual(len(result["trace"]), 2)

    def test_model_failure_is_reported_not_swallowed(self):
        def responder(record):
            return {"error": {"message": "quota exceeded"}}

        model = self.scripted(responder)
        result = chat.run_turn(self.plane, self.profile_for(model), "k",
                               [{"role": "user", "text": "扫吧"}])
        self.assertFalse(result["ok"])
        self.assertIn("quota", result["message"])
        self.assertEqual(self.store.jobs(), [])

    def test_unreachable_model_is_actionable(self):
        profile = {"provider": "openai_compat", "baseUrl": "http://127.0.0.1:1/v1",
                   "model": "m", "temperature": 0.2, "maxTokens": 256, "timeoutS": 3}
        result = chat.run_turn(self.plane, profile, "k",
                               [{"role": "user", "text": "你好"}])
        self.assertFalse(result["ok"])
        self.assertIn("无法连接", result["message"])


# ---------------------------------------------------------------- HTTP 端到端

class ChatApiTestCase(unittest.TestCase):
    def setUp(self):
        self.dir, self.store = support.temporary_store("chat.db")

        def responder(record):
            if has_tool_result(record):
                return text_reply("openai_compat", "任务已下发，稍后用任务列表查询进展。")
            return tool_reply("openai_compat", "start_scan",
                              {"target": "/srv/app", "objective": "审计入口"}, "call-1")

        self.model = ScriptedModel(responder)
        self.httpd, self.client = support.start(self.store, WEB_DIR)
        status, _payload = self.client.post("/api/v1/models", {
            "name": "假模型", "provider": "openai_compat",
            "baseUrl": self.model.base_url + "/v1", "model": "fake-1",
            "apiKeySource": "none", "temperature": 0.2, "maxTokens": 512,
            "timeoutS": 20})
        self.assertEqual(status, 201)

    def tearDown(self):
        self.model.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.dir.cleanup()

    def test_tools_endpoint_is_available_to_the_page(self):
        status, payload = self.client.get("/api/v1/chat/tools")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(payload["tools"]), 8)
        self.assertGreater(payload["maxSteps"], 0)
        self.assertIn("工具", payload["systemPrompt"])

    def test_chat_requires_login(self):
        anon = support.Client(self.client.base)
        status, _payload = anon.get("/api/v1/chat/sessions")
        self.assertEqual(status, 401)
        status, _payload = anon.post("/api/v1/chat", {"message": "hi"})
        self.assertEqual(status, 401)

    def test_chat_requires_csrf_header(self):
        status, payload = self.client.post("/api/v1/chat", {"message": "扫 /srv/app"},
                                           headers={"X-CSRF-Token": "wrong"})
        self.assertEqual(status, 403)
        self.assertIn("CSRF", payload["error"])

    def test_conversation_creates_a_job_and_persists_history(self):
        status, payload = self.client.post("/api/v1/chat",
                                           {"message": "帮我扫描 /srv/app"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload.get("error"))
        self.assertIn("已下发", payload["text"])
        self.assertEqual(payload["trace"][0]["tool"], "start_scan")
        self.assertEqual(payload["model"]["name"], "假模型")
        session_id = payload["sessionId"]

        jobs = self.store.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["target"], "/srv/app")

        status, listing = self.client.get("/api/v1/chat/sessions")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in listing["sessions"]], [session_id])
        self.assertEqual(listing["sessions"][0]["title"], "帮我扫描 /srv/app")

        status, detail = self.client.get("/api/v1/chat/sessions/%s" % session_id)
        self.assertEqual(status, 200)
        roles = [message["role"] for message in detail["messages"]]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])
        self.assertEqual(detail["messages"][3]["text"], payload["text"])
        self.assertEqual(detail["messages"][3]["trace"][0]["tool"], "start_scan")

    def test_second_turn_keeps_the_history(self):
        _status, first = self.client.post("/api/v1/chat", {"message": "扫 /srv/app"})
        status, second = self.client.post("/api/v1/chat",
                                          {"message": "现在什么状态",
                                           "sessionId": first["sessionId"]})
        self.assertEqual(status, 200, second)
        self.assertTrue(second["ok"], second.get("error"))
        # 第二轮请求里必须带上第一轮的历史，否则模型失忆
        sent = self.model.requests[-1]["body"]["messages"]
        self.assertTrue(any("帮我" in json.dumps(m, ensure_ascii=False)
                            or "扫 /srv/app" in json.dumps(m, ensure_ascii=False)
                            for m in sent))
        self.assertEqual(first["sessionId"], second["sessionId"])

    def test_missing_message_is_rejected(self):
        status, payload = self.client.post("/api/v1/chat", {"message": "   "})
        self.assertEqual(status, 400)
        self.assertIn("message", payload["error"])

    def test_unknown_session_is_404(self):
        status, _payload = self.client.post("/api/v1/chat", {"message": "hi",
                                                             "sessionId": "C-nope"})
        self.assertEqual(status, 404)

    def test_rename_and_delete_session(self):
        _status, payload = self.client.post("/api/v1/chat", {"message": "扫 /srv/app"})
        session_id = payload["sessionId"]
        status, renamed = self.client.post(
            "/api/v1/chat/sessions/%s/rename" % session_id, {"title": "上传逻辑审计"})
        self.assertEqual(status, 200)
        self.assertEqual(renamed["session"]["title"], "上传逻辑审计")
        status, _payload = self.client.post(
            "/api/v1/chat/sessions/%s/delete" % session_id, {})
        self.assertEqual(status, 200)
        self.assertIsNone(self.store.get_chat_session(session_id))
        self.assertEqual(self.store.chat_messages(session_id), [])

    def test_operator_cannot_read_someone_elses_conversation(self):
        _status, payload = self.client.post("/api/v1/chat", {"message": "扫 /srv/app"})
        session_id = payload["sessionId"]
        status, _payload = self.client.post("/api/v1/users", {
            "username": "operator-a", "password": "Operator-Pass1!",
            "role": "operator"})
        self.assertEqual(status, 201)
        other = support.Client(self.client.base)
        other.login("operator-a", "Operator-Pass1!")
        status, _payload = other.get("/api/v1/chat/sessions/%s" % session_id)
        self.assertEqual(status, 403)
        status, listing = other.get("/api/v1/chat/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(listing["sessions"], [])
        self.assertEqual(listing["scope"], "own")

    def test_chat_without_active_model_points_at_settings(self):
        # 直接改表借用 Store 的锁：公开方法都被 serialized 包着，绕过它们裸写
        # 会和并发线程的事务交错（见 test_local_node 里同样的处理）。
        with self.store._lock:
            self.store.conn.execute("UPDATE model_profiles SET is_active=0")
            self.store.conn.commit()
        status, payload = self.client.post("/api/v1/chat", {"message": "扫 /srv/app"})
        self.assertEqual(status, 400)
        self.assertIn("设置", payload["error"])

    def test_broken_model_key_blocks_the_conversation(self):
        with self.store._lock:
            self.store.conn.execute("UPDATE model_profiles SET api_key_source='env',"
                                    " api_key_ref='CHAT_TEST_MISSING_ENV'")
            self.store.conn.commit()
        status, payload = self.client.post("/api/v1/chat", {"message": "扫 /srv/app"})
        self.assertEqual(status, 400)
        self.assertIn("CHAT_TEST_MISSING_ENV", payload["error"])
        self.assertEqual(self.store.jobs(), [])


# ---------------------------------------------------------------- 历史配对

class ChatHistoryWindowTestCase(unittest.TestCase):
    """长对话：接口只送**最近** 400 条，并且要说明更早还有多少条。

    老写法是 `ORDER BY seq LIMIT 400`——从第一条起取 400 条。对话长过 400 条之后，
    最新的那几条永远不在返回值里，而页面收尾时正是按这份列表重画：刚发出去的那句
    话当场消失。这里把"必须给尾巴"钉成契约。
    """

    def setUp(self):
        self.dir, self.store = support.temporary_store("chat-tail.db")
        self.httpd, self.client = support.start(self.store, WEB_DIR)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.dir)

    def _long_session(self, count):
        session = self.store.create_chat_session(title="长对话")
        for index in range(count):
            self.store.append_chat_message(session["id"], "user",
                                           {"text": "第 %d 条" % index})
        return session["id"]

    def test_the_page_gets_the_newest_messages_and_knows_how_many_it_missed(self):
        session_id = self._long_session(405)
        status, payload = self.client.get("/api/v1/chat/sessions/%s" % session_id)
        self.assertEqual(200, status, payload)
        self.assertEqual(400, len(payload["messages"]))
        self.assertEqual(6, payload["messages"][0]["seq"])
        self.assertEqual(405, payload["messages"][-1]["seq"], "最新的那条必须还在")
        self.assertEqual("第 404 条", payload["messages"][-1]["text"])
        self.assertEqual(5, payload["earlierCount"])

    def test_a_short_session_is_complete_and_says_so(self):
        session_id = self._long_session(1)
        status, payload = self.client.get("/api/v1/chat/sessions/%s" % session_id)
        self.assertEqual(200, status, payload)
        self.assertEqual(1, len(payload["messages"]))
        self.assertEqual(0, payload["earlierCount"])

    def test_the_tail_helper_returns_them_in_ascending_order(self):
        session_id = self._long_session(7)
        rows = self.store.chat_messages_tail(session_id, 3)
        self.assertEqual([5, 6, 7], [row["seq"] for row in rows])

    def test_an_earlier_page_is_fetched_by_cursor(self):
        session_id = self._long_session(405)
        status, payload = self.client.get(
            "/api/v1/chat/sessions/%s?before=6" % session_id)
        self.assertEqual(200, status, payload)
        self.assertEqual([1, 2, 3, 4, 5],
                         [m["seq"] for m in payload["messages"]])
        self.assertEqual(0, payload["earlierCount"],
                         "已经到第一条了，上面不能再有")
        self.assertEqual(400, payload["pageSize"])

    def test_an_earlier_page_is_bounded_and_reports_the_rest(self):
        session_id = self._long_session(900)
        status, payload = self.client.get(
            "/api/v1/chat/sessions/%s?before=501" % session_id)
        self.assertEqual(200, status, payload)
        self.assertEqual(400, len(payload["messages"]))
        self.assertEqual(101, payload["messages"][0]["seq"])
        self.assertEqual(500, payload["messages"][-1]["seq"],
                         "游标那一页必须紧贴 ?before= 之前，不能从头上重来")
        self.assertEqual(100, payload["earlierCount"])

    def test_a_bad_cursor_falls_back_to_the_tail(self):
        session_id = self._long_session(405)
        status, payload = self.client.get(
            "/api/v1/chat/sessions/%s?before=abc" % session_id)
        self.assertEqual(200, status, payload)
        self.assertEqual(405, payload["messages"][-1]["seq"])


class HistoryPairingTestCase(unittest.TestCase):
    """截断历史不能把"assistant 工具调用 / tool 结果"这对拆开。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("hist.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_cut_inside_a_tool_pair_drops_both_sides(self):
        messages = []
        for index in range(6):
            messages.append({"role": "user", "text": "问题 %d" % index})
            messages.append({"role": "assistant", "text": "答 %d" % index})
        messages.append({"role": "assistant", "text": "",
                         "toolCalls": [{"id": "c1", "name": "list_jobs",
                                        "arguments": {}}]})
        messages.append({"role": "tool", "toolCallId": "c1", "toolName": "list_jobs",
                         "text": "{}"})
        window = self.plane._history_for_model(messages, limit=2)
        self.assertTrue(window)
        self.assertNotEqual(window[0].get("role"), "tool")
        self.assertFalse(window[0].get("toolCalls"))
        for message in window:
            if message.get("role") == "tool":
                self.assertTrue(any(other.get("toolCalls")
                                    for other in window[:window.index(message)]))

    def test_complete_pairs_survive_truncation(self):
        messages = [{"role": "user", "text": "扫一下"},
                    {"role": "assistant", "text": "",
                     "toolCalls": [{"id": "c1", "name": "start_scan",
                                    "arguments": {"target": "/srv/a"}}]},
                    {"role": "tool", "toolCallId": "c1", "toolName": "start_scan",
                     "text": "{\"ok\":true}"},
                    {"role": "assistant", "text": "已下发"}]
        window = self.plane._history_for_model(messages, limit=10)
        self.assertEqual([m["role"] for m in window],
                         ["user", "assistant", "tool", "assistant"])
        self.assertEqual(window[2]["toolCallId"], "c1")


class StepCeilingTestCase(unittest.TestCase):
    """`chat.steps_at_ceiling`：这一轮的预算是不是"只是平台上限"。

    它是气泡上「最多 N 步」要不要显示的唯一判据，所以边界值单独钉一遍。最要紧的是
    `test_the_module_fallback_is_not_the_ceiling`：本模块自己也有一个 `MAX_STEPS = 6`
    （预设没写步数时的兜底值），判据要是拿它去比，**每一轮都会被判成顶格**，于是所有
    真有限额的会话都不再显示那个会中途把作业切停的数——界面上看起来一切正常，只是
    少了一句话。
    """

    def test_the_platform_ceiling_counts_as_the_ceiling(self):
        self.assertIs(chat.steps_at_ceiling(agents.MAX_STEPS), True)

    def test_the_module_fallback_is_not_the_ceiling(self):
        self.assertLess(chat.MAX_STEPS, agents.MAX_STEPS, "两个常量撞在一起了")
        self.assertIs(chat.steps_at_ceiling(chat.MAX_STEPS), False)

    def test_one_step_below_the_ceiling_is_still_a_real_budget(self):
        """写成 `>` 就会漏掉"正好顶格"，写成 `!=` 则会把更高的值判成真预算。"""
        self.assertIs(chat.steps_at_ceiling(agents.MAX_STEPS - 1), False)

    def test_ordinary_budgets_are_real(self):
        for steps in (1, 6, 10, 12, 40, 500):
            self.assertIs(chat.steps_at_ceiling(steps), False, steps)

    def test_junk_is_never_reported_as_a_ceiling(self):
        """缺字段 / 空串 / 非数字一律按"有上限"处理。

        反过来的方向（把垃圾当顶格）会让上限整个消失，而"查不出上限"与"没有上限"
        在界面上长得一模一样。
        """
        for value in (None, "", "很多", [], {}, 3.5):
            self.assertIs(chat.steps_at_ceiling(value), False, repr(value))


if __name__ == "__main__":
    unittest.main(verbosity=2)
