# -*- coding: utf-8 -*-
"""实测：把一次流式对话的**真实 SSE 字节**按到达时刻打印出来。

这条脚本存在的意义是证明"真的在流"。单元测试做不到这一点——它读的是同一个进程里
的响应对象，服务端就算把整轮跑完再一次性写出，测试照样全绿。只有在真实 socket 上
量"每一帧什么时候到"，才能把"边算边发"和"攒完再发"区分开：

    攒完再发 → 所有帧的时间戳挤在同几毫秒里，且都等于整轮耗时；
    边算边发 → 首帧在几十毫秒内出现，后续帧分散在整轮时间轴上。

用法：
    python tools/stream_demo.py            # 默认用假模型
"""
import json
import os
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

from pentestd.server import serve_background                        # noqa: E402
from pentestd.store import Store                                    # noqa: E402

ADMIN = "demo-admin"
PASSWORD = "Demo-Admin-Pass1!"


def sse(payload) -> str:
    return "data: %s\n\n" % json.dumps(payload, ensure_ascii=False)


class FakeModel:
    """会流式回话的假模型：第一轮要求调用工具，第二轮给出结论。"""

    def __init__(self, chunks: int = 12, gap: float = 0.12):
        self.chunks, self.gap = chunks, gap
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                body = json.loads(raw or "{}")
                outer.requests.append(body)
                has_tool = any(message.get("role") == "tool"
                               for message in body.get("messages") or [])
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                for frame in outer.script(has_tool):
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(outer.gap)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self):
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d" % (host, port)

    def script(self, has_tool):
        if not has_tool:
            raw = json.dumps({"target": "/srv/demo", "objective": "审计上传逻辑"},
                             ensure_ascii=False)
            step = max(1, len(raw) // 4)
            frames = [sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call-1", "type": "function",
                 "function": {"name": "start_scan", "arguments": ""}}]},
                "finish_reason": None}]})]
            for index in range(0, len(raw), step):
                frames.append(sse({"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": raw[index:index + step]}}]},
                    "finish_reason": None}]}))
            frames.append(sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}))
            frames.append("data: [DONE]\n\n")
            return frames
        text = ("已完成。`start_scan` 已把 /srv/demo 加入源码审计队列，"
                "审计目标是上传逻辑；结果可在「任务列表」与「漏洞」页签查看。")
        frames = []
        size = max(1, len(text) // self.chunks)
        for index in range(0, len(text), size):
            frames.append(sse({"choices": [
                {"delta": {"content": text[index:index + size]}, "finish_reason": None}]}))
        frames.append(sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
        frames.append("data: [DONE]\n\n")
        return frames


def main():
    model = FakeModel()
    import tempfile
    directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    store = Store(os.path.join(directory.name, "demo.db"))
    httpd, _thread = serve_background(store, os.path.join(ROOT, "web"),
                                      "127.0.0.1", 0)
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def post(path, body, headers=None, opener_=None):
        client = opener_ or opener
        head = {"Content-Type": "application/json"}
        head.update(headers or {})
        request = urllib.request.Request(base + path,
                                         data=json.dumps(body).encode("utf-8"),
                                         method="POST", headers=head)
        with client.open(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")

    _status, boot = post("/api/v1/auth/bootstrap",
                         {"username": ADMIN, "password": PASSWORD})
    csrf = boot.get("csrfToken", "")
    status, profile = post("/api/v1/models", {
        "name": "流式演示模型", "provider": "openai_compat",
        "baseUrl": model.base_url + "/v1", "model": "fake-1",
        "apiKeySource": "none", "temperature": 0.2, "maxTokens": 512,
        "timeoutS": 30}, {"X-CSRF-Token": csrf})
    print("模型登记 → HTTP %s" % status)

    request = urllib.request.Request(
        base + "/api/v1/chat/stream",
        data=json.dumps({"message": "扫描 /srv/demo，目标是审计上传逻辑"}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-CSRF-Token": csrf})
    started = time.time()
    print("\n%-9s %-13s %s" % ("到达(s)", "事件", "内容"))
    print("-" * 78)
    text = ""
    event, data = "", []
    with opener.open(request, timeout=60) as response:
        print("HTTP %s  Content-Type: %s" % (response.status,
                                             response.headers.get("Content-Type")))
        for raw in response:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line == "":
                if data:
                    payload = json.loads("\n".join(data))
                    stamp = time.time() - started
                    show = json.dumps(payload, ensure_ascii=False)
                    if event == "delta":
                        text += payload.get("text") or ""
                    print("%-9.3f %-13s %s" % (stamp, event, show[:96]))
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

    print("-" * 78)
    print("整轮耗时 %.3fs；模型请求 %d 次；任务 %d 个"
          % (time.time() - started, len(model.requests), len(store.jobs())))
    session = store.chat_sessions("")
    stored = store.chat_messages(session[0]["id"])
    print("落库消息：%s" % [message["role"] for message in stored])
    print("结论带轨迹：%s（%d 次调用）"
          % (bool(stored[-1].get("trace")), len(stored[-1].get("trace") or [])))
    httpd.shutdown()
    httpd.server_close()
    store.close()
    directory.cleanup()


if __name__ == "__main__":
    main()
