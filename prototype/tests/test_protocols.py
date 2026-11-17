# -*- coding: utf-8 -*-
"""模型协议适配测试（引擎侧）。

平台侧的「测试连接」用的是平台自己的适配层，而**扫描时走的是引擎这一份**。
两份都必须被验证，否则会出现"设置页测试通过、扫描却连不上"的错位。

这里起一个假服务端，分别模拟 OpenAI 兼容 / Anthropic / Ollama 三种协议，
断言引擎发出去的请求路径、鉴权头与消息体形状正确，并验证返回值解析。
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from vulnagent.config import ModelConfig                        # noqa: E402
from vulnagent.llm import (AnthropicChat, LLMUnavailable, OllamaChat,   # noqa: E402
                           OpenAICompatChat, build_chat)


class FakeEndpoint:
    def __init__(self, reply: dict, status: int = 200, body_text: str = ""):
        self.reply = reply
        self.status = status
        self.body_text = body_text
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                outer.requests.append({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(raw) if raw else {}})
                payload = (outer.body_text or json.dumps(outer.reply)).encode("utf-8")
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d" % (host, port)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def last(self):
        return self.requests[-1]


class OpenAIShapeTestCase(unittest.TestCase):
    def test_request_and_reply(self):
        fake = FakeEndpoint({"choices": [{"message": {"content": "应答内容"}}]})
        try:
            chat = OpenAICompatChat(fake.url, "gpt-4o-mini", "UNUSED_ENV",
                                    api_key="sk-direct-key")
            text = chat.chat("系统提示", "用户提问", 0.3, 128, 10)
            self.assertEqual(text, "应答内容")
            sent = fake.last()
            self.assertEqual(sent["path"], "/chat/completions")
            self.assertEqual(sent["headers"]["authorization"], "Bearer sk-direct-key")
            self.assertEqual(sent["body"]["model"], "gpt-4o-mini")
            self.assertEqual(sent["body"]["temperature"], 0.3)
            self.assertEqual(sent["body"]["max_tokens"], 128)
            roles = [m["role"] for m in sent["body"]["messages"]]
            self.assertEqual(roles, ["system", "user"])
        finally:
            fake.stop()

    def test_direct_key_takes_precedence_over_env(self):
        fake = FakeEndpoint({"choices": [{"message": {"content": "ok"}}]})
        os.environ["VULNAGENT_TEST_KEY"] = "from-env"
        try:
            chat = OpenAICompatChat(fake.url, "m", "VULNAGENT_TEST_KEY",
                                    api_key="from-argument")
            chat.chat("s", "u", 0.1, 8, 10)
            self.assertEqual(fake.last()["headers"]["authorization"],
                             "Bearer from-argument")
            # 不给直接密钥时回退环境变量
            chat2 = OpenAICompatChat(fake.url, "m", "VULNAGENT_TEST_KEY")
            chat2.chat("s", "u", 0.1, 8, 10)
            self.assertEqual(fake.last()["headers"]["authorization"],
                             "Bearer from-env")
        finally:
            os.environ.pop("VULNAGENT_TEST_KEY", None)
            fake.stop()

    def test_http_error_becomes_llm_unavailable(self):
        fake = FakeEndpoint({"error": {"message": "invalid api key"}}, status=401)
        try:
            chat = OpenAICompatChat(fake.url, "m", "UNUSED")
            with self.assertRaises(LLMUnavailable):
                chat.chat("s", "u", 0.1, 8, 10)
        finally:
            fake.stop()

    def test_malformed_reply_becomes_llm_unavailable(self):
        fake = FakeEndpoint({"unexpected": True})
        try:
            chat = OpenAICompatChat(fake.url, "m", "UNUSED")
            with self.assertRaises(LLMUnavailable):
                chat.chat("s", "u", 0.1, 8, 10)
        finally:
            fake.stop()


class AnthropicShapeTestCase(unittest.TestCase):
    def test_request_and_reply(self):
        fake = FakeEndpoint({"content": [{"type": "text", "text": "克劳德应答"}]})
        try:
            chat = AnthropicChat(fake.url, "claude-sonnet-4-5", "UNUSED_ENV",
                                 api_key="sk-ant-key")
            text = chat.chat("系统提示", "用户提问", 0.2, 256, 10)
            self.assertEqual(text, "克劳德应答")
            sent = fake.last()
            self.assertEqual(sent["path"], "/messages")
            self.assertEqual(sent["headers"]["x-api-key"], "sk-ant-key")
            self.assertNotIn("authorization", sent["headers"])
            self.assertEqual(sent["headers"]["anthropic-version"],
                             AnthropicChat.ANTHROPIC_VERSION)
            self.assertEqual(sent["body"]["system"], "系统提示",
                             "系统提示词是顶层字段，不是 messages 里的一条")
            self.assertEqual([m["role"] for m in sent["body"]["messages"]], ["user"])
            self.assertEqual(sent["body"]["max_tokens"], 256)
        finally:
            fake.stop()

    def test_multiple_content_blocks_are_joined(self):
        fake = FakeEndpoint({"content": [{"type": "text", "text": "第一段"},
                                         {"type": "text", "text": "第二段"}]})
        try:
            chat = AnthropicChat(fake.url, "m", "UNUSED")
            self.assertEqual(chat.chat("s", "u", 0.1, 8, 10), "第一段第二段")
        finally:
            fake.stop()


class OllamaShapeTestCase(unittest.TestCase):
    def test_request_and_reply(self):
        fake = FakeEndpoint({"message": {"role": "assistant", "content": "本地应答"}})
        try:
            chat = OllamaChat(fake.url, "qwen2.5:14b", "UNUSED_ENV")
            text = chat.chat("系统提示", "用户提问", 0.5, 64, 10)
            self.assertEqual(text, "本地应答")
            sent = fake.last()
            self.assertEqual(sent["path"], "/api/chat")
            self.assertNotIn("authorization", sent["headers"],
                             "本地 Ollama 默认不需要密钥")
            self.assertFalse(sent["body"]["stream"], "必须关掉流式，否则响应无法整体解析")
            self.assertEqual(sent["body"]["options"]["num_predict"], 64)
            self.assertEqual([m["role"] for m in sent["body"]["messages"]],
                             ["system", "user"])
        finally:
            fake.stop()


class BuildChatTestCase(unittest.TestCase):
    def test_provider_selects_client(self):
        cases = {"openai_compat": OpenAICompatChat,
                 "anthropic": AnthropicChat,
                 "ollama": OllamaChat}
        for provider, expected in cases.items():
            cfg = ModelConfig(provider=provider, base_url="http://127.0.0.1:1/v1",
                              model="m", api_key="k")
            self.assertIsInstance(build_chat(cfg), expected,
                                  "provider=%s 应造出 %s" % (provider, expected.__name__))

    def test_direct_key_propagates(self):
        cfg = ModelConfig(provider="openai_compat", base_url="http://127.0.0.1:1/v1",
                          model="m", api_key="sk-from-config")
        self.assertEqual(build_chat(cfg).resolve_key(), "sk-from-config")

    def test_unknown_provider_fails_loudly(self):
        """未知协议要直接报错，不能悄悄退化成 OpenAI 兼容——退化的症状是 404，
        比一句"不支持的协议"难排查得多。"""
        cfg = ModelConfig(provider="gemini", base_url="http://x", model="m")
        with self.assertRaises(ValueError) as ctx:
            build_chat(cfg)
        self.assertIn("不支持的模型协议", str(ctx.exception))

    def test_heuristic_yields_null_chat(self):
        cfg = ModelConfig(provider="heuristic")
        self.assertFalse(build_chat(cfg).available())


class SandboxOutputEncodingTestCase(unittest.TestCase):
    """沙箱输出解码：子进程被强制 utf-8，父进程就必须按 utf-8 解。

    踩过的坑：`subprocess.run(text=True)` 会走**系统区域编码**（中文 Windows 上是 gbk），
    而子进程被设成 PYTHONIOENCODING=utf-8，于是读取线程在后台直接抛
    UnicodeDecodeError——报错出现在一个没人看的线程里，表现为"验证结果偶尔不对"。
    """

    def setUp(self):
        from vulnagent.config import SandboxConfig
        self.config = SandboxConfig()
        self.config.timeout_s = 20

    def _run(self, source: str):
        from vulnagent.tools import SandboxRunner
        runner = SandboxRunner(self.config, allow_fallback=True)
        import tempfile
        target = tempfile.mkdtemp(prefix="enc-target-")
        return runner.run_poc(source, target_root=target)

    def test_utf8_output_is_captured(self):
        result = self._run(
            "import sys\n"
            "sys.stdout.write('中文标记 VULNAGENT_MARK\\n')\n")
        self.assertIn("VULNAGENT_MARK", result.stdout or "")
        self.assertIn("中文标记", result.stdout or "")

    def test_non_utf8_bytes_do_not_crash_the_reader(self):
        """非 UTF-8 字节应被替换而不是让读取线程崩掉。"""
        result = self._run(
            "import sys\n"
            "sys.stdout.buffer.write(b'VULNAGENT_MARK ' + b'\\xce\\xd2\\xca\\xc7' + b'\\n')\n"
            "sys.stdout.flush()\n")
        self.assertIsInstance(result.stdout, str)
        self.assertIn("VULNAGENT_MARK", result.stdout,
                      "即使有非法字节，标记也该被读出来")

    def test_large_output_is_truncated_not_lost(self):
        result = self._run(
            "import sys\n"
            "for _ in range(2000):\n"
            "    sys.stdout.write('M' * 200 + '\\n')\n")
        self.assertLessEqual(len(result.stdout or ""),
                             self.config.max_output_bytes + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
