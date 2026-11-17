# -*- coding: utf-8 -*-
"""把一次真实的派活按**到达时刻**打出来。

单元测试证明不了两件事，而这个脚本就是为了它们存在的：

1. **子智能体真的在并发。** 用例读的是自己进程里的状态，串行执行也会全绿；
   这里三个子智能体各自 sleep，看的是它们的 `start`/`done` 在时间轴上是否重叠。
2. **工具调用真的落到了控制面。** 打桩 `execute_tool` 的用例照样全绿；这里用
   真实 HTTP 模型端点 + 真实 `ControlPlane` + 真实 SQLite，最后数一遍库里的任务。

用法：
    python tools/subagent_demo.py

不需要任何 API key：模型端点是本脚本自己起的，按角色脚本化回话。
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import chat, server                                    # noqa: E402
from pentestd.store import Store                                     # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")

# 每个子智能体第一次调用时先睡一下。目的不是"模拟慢"，
# 而是让并发在时间轴上**看得见**：串行的话三个 start 会各隔 0.35 秒。
THINK = 0.35
ROLE_RE = __import__("re").compile(r"## 你的角色\s*\n\s*(\S+)")


def role_of(body) -> str:
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            found = ROLE_RE.search(content)
            if found:
                return found.group(1)
    return ""


def say(text):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


def call_tool(name, arguments, call_id):
    return {"choices": [{"message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name,
                                     "arguments": json.dumps(arguments,
                                                             ensure_ascii=False)}}]},
        "finish_reason": "tool_calls"}]}


class Script:
    """按角色脚本化。父轮靠"系统提示里没有 `## 你的角色`"认出来。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.seen = {}

    def bump(self, key) -> int:
        with self.lock:
            self.seen[key] = self.seen.get(key, 0) + 1
            return self.seen[key]

    def __call__(self, body):
        role = role_of(body)
        if not role:
            if self.bump("parent") == 1:
                return call_tool("delegate", {"children": [
                    {"role": "recon",
                     "objective": "探测 10.10.10.250 的开放端口与服务",
                     "tools": ["start_probe", "get_assets"]},
                    {"role": "web",
                     "objective": "核对已有资产里有没有 Web 服务",
                     "tools": ["list_executors", "get_assets"]},
                    {"role": "creds",
                     "objective": "评估 10.10.10.250 的口令强度"}]}, "call-parent")
            return say("已派出 3 个子智能体并汇总完毕。")
        time.sleep(THINK)
        step = self.bump(role)
        if role == "recon":
            if step == 1:
                return call_tool("start_probe",
                                 {"target": "10.10.10.250", "objective": "看存活"},
                                 "call-recon-1")
            return say("结论：10.10.10.250 有 3 个端口开放\n"
                       "证据：22/tcp open ssh；80/tcp open http；443/tcp open https\n"
                       "建议：对 443 做证书与认证配置判定")
        if role == "web":
            if step == 1:
                return call_tool("list_executors", {}, "call-web-1")
            return say("结论：控制面有 1 个执行节点在线\n"
                       "证据：exec-local（本机）\n建议：无")
        # creds：让它的模型直接失败，用来看"失败原因有没有被吞掉"
        return {"error": {"message": "上游模型端点返回 502"}}


def serve(handler_script):
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
            payload = json.dumps(handler_script(body) or {}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address[:2]
    return httpd, "http://%s:%d/v1" % (host, port)


def main() -> int:
    directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    store = Store(os.path.join(directory.name, "demo.db"))
    plane = server.ControlPlane(store, WEB_DIR, auth_required=False)
    httpd, base_url = serve(Script())
    profile = {"provider": "openai_compat", "baseUrl": base_url,
               "model": "fake-1", "temperature": 0.0, "maxTokens": 512,
               "timeoutS": 30}
    origin = time.time()
    print("时间轴（秒）  事件")
    print("-" * 78)
    result = None
    persisted = []
    try:
        for event in chat.run_turn_stream(
                plane, profile, "demo-key",
                [{"role": "user", "text": "派几个人并行看看 10.10.10.250"}],
                policy_id="probe", max_steps=8, stream=False):
            stamp = time.time() - origin
            kind = event.get("type")
            if kind == "subagent":
                phase = event.get("phase")
                who = "%d/%d %s" % (event.get("index"), event.get("total"),
                                    event.get("role"))
                if phase == "start":
                    tail = "policy=%s tools=%d" % (event.get("policy"),
                                                   event.get("toolCount"))
                elif phase == "step":
                    tail = "第 %s 次模型调用" % event.get("step")
                elif phase == "tool":
                    tail = "%s %s %s in %dms" % (
                        "OK " if event.get("ok") else "ERR",
                        event.get("name"), event.get("brief"),
                        event.get("durationMs") or 0)
                else:
                    tail = "%s steps=%s %sms  %s%s" % (
                        "完成" if event.get("ok") else "失败",
                        event.get("steps"), event.get("durationMs"),
                        event.get("summary") or "",
                        ("  ← " + event["error"]) if event.get("error") else "")
                print("%7.3f       └ 子智能体 %-14s %-8s %s"
                      % (stamp, who, phase, tail))
            elif kind == "tool":
                print("%7.3f       %s%s in %dms  %s"
                      % (stamp, "OK " if event.get("ok") else "ERR",
                         event.get("name"), event.get("durationMs") or 0,
                         event.get("brief") or ""))
            elif kind == "turn":
                print("%7.3f   turn   agent=%s policy=%s maxSteps=%s tools=%d"
                      % (stamp, event.get("agentLabel"), event.get("policyId"),
                         event.get("maxSteps"), event.get("tools") or 0))
            elif kind == "step":
                print("%7.3f   step   第 %s 次模型调用（父轮）"
                      % (stamp, event.get("step")))
            elif kind == "notice":
                print("%7.3f   notice %s" % (stamp, event.get("message")))
            elif kind == "persist":
                persisted.append(event["message"])
            elif kind == "done":
                result = event
                print("%7.3f   done   steps=%s  %s"
                      % (stamp, event.get("steps"), event.get("text")))
            elif kind == "error":
                print("%7.3f   ERROR  %s" % (stamp, event.get("error")))
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("-" * 78)
    if not result:
        print("这一轮没有结束事件。")
        return 1
    entries = [entry for entry in result.get("trace") or []
               if entry.get("tool") == "delegate"]
    if entries:
        payload = json.loads(entries[0]["data"])
        print("派活汇总：spawned=%s succeeded=%s failed=%s 并发=%s 预算=%s"
              % (payload["spawned"], payload["succeeded"], payload["failed"],
                 payload["concurrency"], payload["budget"]))
        for child in payload["children"]:
            mark = "✓" if child["ok"] else "✗"
            print("  %s %-6s policy=%-6s steps=%s %5dms  %s"
                  % (mark, child["role"], child["policy"], child["steps"],
                     child["durationMs"],
                     child["summary"] if child["ok"] else child["error"]))
            for call in child["toolCalls"]:
                print("      · %s %s" % (call["name"],
                                         "ok" if call["ok"] else "失败"))
    jobs = store.jobs()
    print("库里的任务：%d 个" % len(jobs))
    for job in jobs:
        print("  · %s kind=%s target=%s status=%s"
              % (job.get("id"), job.get("kind"), job.get("target"),
                 job.get("status")))
    print("父轮步数：%s（maxSteps 8：delegate 自身 1 + 子智能体扣掉的预算）"
          % result.get("steps"))
    carried = [message for message in persisted
               if message.get("role") == "assistant" and message.get("subagents")]
    if carried:
        rows = carried[-1]["subagents"]
        print("落库：最后那条 assistant 消息带了 %d 行子智能体记录"
              "（刷新后重画用的就是它，不是靠流里的临时状态）" % len(rows))
        for row in rows:
            print("  #%s/%s %-6s state=%-6s steps=%s  %s"
                  % (row["index"], row["total"], row["role"], row["state"],
                     row["steps"], row["summary"] or row["error"] or ""))
    else:
        print("落库：**没有**子智能体记录挂到消息上（刷新后这一块会消失）")
    store.close()
    directory.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
