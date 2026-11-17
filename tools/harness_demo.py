# -*- coding: utf-8 -*-
"""把「终端 + 授权审查」按**到达时刻**打出来，证明三件事。

单元测试证明不了这三件事，而这个脚本就是为了它们存在的：

1. **审查真的挂在执行之前。** 用例可以只查"审查单建了"，但那样即使命令早就跑完
   了也照样全绿。这里在审查单出现的那一刻去问文件系统：命令写的那个文件**在不在**。
2. **拒绝真的不执行。** 批准路径跑通不能证明拒绝路径没执行 —— 两条路径共用
   `_reviewed`，而"忘了看 status"这种错只在拒绝那一次才暴露。
3. **执行真的发生了。** 打桩 `run_command` 的用例全绿也说明不了这条命令跑过。

用法：
    python tools/harness_demo.py

不需要任何 API key：模型端点是本脚本自己起的，按步脚本化回话。
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

from pentestd import approval, chat, harness, server, terminal        # noqa: E402
from pentestd.store import Store                                   # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


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
    """第一次回话要跑命令，第二次给结论。"""

    def __init__(self, command):
        self.command = command
        self.turns = 0
        self.lock = threading.Lock()

    def __call__(self, body):
        with self.lock:
            self.turns += 1
            turn = self.turns
        if turn == 1:
            return call_tool("run_command", {"command": self.command,
                                             "timeoutS": 30}, "call-1")
        return say("命令已经跑过了，结论见工具结果。")


class Decider(threading.Thread):
    """扮演"看着界面点按钮的人"：审批单一出现就裁决。

    它读的是**库里的表**（`list_approvals`），不是内存里的钩子 —— 因为真实的
    批准就是这么进来的（一次 HTTP POST），而"队列里有单子"与"有人会点"是两件事，
    混起来的测试会掩盖"单子建了但界面看不到"。
    """

    def __init__(self, store, approve, note=""):
        super().__init__(daemon=True)
        self.store = store
        self.approve = approve
        self.note = note
        self.seen = []
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            for row in self.store.list_approvals(status="pending", limit=10):
                if row["id"] in self.seen:
                    continue
                self.seen.append(row["id"])
                time.sleep(0.15)                 # 让人看得出"挂起"是一段真实的时间
                self.store.decide_approval(row["id"], approve=self.approve,
                                           decided_by="demo-user", note=self.note)
            time.sleep(0.1)


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


def command_for(path, marker):
    """拼一条会在 `path` 写出 `marker` 的命令，跟随实际解释器。"""
    shell, _prefix = terminal.resolve_shell()
    if shell in ("pwsh", "powershell"):
        return "Set-Content -LiteralPath '%s' -Value '%s'" % (path, marker)
    return "printf '%%s' '%s' > '%s'" % (marker, path)


def run_case(name, *, approve, note="", mode="ask"):
    """跑一轮对话，返回 (结果字典, 时间轴行, 落地的文件内容或 None)。"""
    directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    store = Store(os.path.join(directory.name, "demo.db"))
    plane = server.ControlPlane(store, WEB_DIR, auth_required=False)
    store.set_setting(harness.SETTING_KEY, mode)
    target = os.path.join(directory.name, "landed.txt")
    script = Script(command_for(target, name))
    httpd, base_url = serve(script)
    decider = Decider(store, approve, note)
    profile = {"provider": "openai_compat", "baseUrl": base_url,
               "model": "fake-1", "temperature": 0.0, "maxTokens": 512,
               "timeoutS": 30}
    lines = []
    result = None
    at_pending = None
    origin = time.time()
    decider.start()
    try:
        for event in chat.run_turn_stream(
                plane, profile, "demo-key",
                [{"role": "user", "text": "在控制面跑一条命令"}],
                policy_id="full", max_steps=6, stream=False):
            stamp = time.time() - origin
            kind = event.get("type")
            if kind == "review":
                review = event["review"]
                if event.get("phase") == "pending":
                    # 就在这一刻问文件系统：命令**还没有**执行。
                    at_pending = os.path.exists(target)
                    lines.append("%7.3f   审查   挂起 %s  «%s»" % (
                        stamp, review["id"], review["headline"]))
                    for reason in review["reasons"][:3]:
                        lines.append("%7.3f           · %s" % (stamp, reason))
                else:
                    lines.append("%7.3f   审查   %s  %s" % (
                        stamp, review["statusLabel"], review["note"] or ""))
            elif kind == "tool":
                lines.append("%7.3f   工具   %s %s  %s" % (
                    stamp, "OK " if event.get("ok") else "ERR", event.get("name"),
                    (event.get("brief") or "")[:96]))
            elif kind == "step":
                lines.append("%7.3f   模型   第 %s 次调用" % (stamp, event.get("step")))
            elif kind == "done":
                result = event
                lines.append("%7.3f   结束   steps=%s  %s" % (
                    stamp, event.get("steps"), (event.get("text") or "")[:60]))
            elif kind == "error":
                lines.append("%7.3f   错误   %s" % (stamp, event.get("error")))
    finally:
        decider.stop.set()
        httpd.shutdown()
        httpd.server_close()
    landed = None
    if os.path.exists(target):
        with open(target, "r", encoding="utf-8") as handle:
            landed = handle.read().strip()
    terminals = store.list_terminals(limit=5)
    reviews = store.list_approvals(limit=5)
    store.close()
    directory.cleanup()
    return {"name": name, "approve": approve, "mode": mode,
            "pendingFileExisted": at_pending, "landed": landed,
            "result": result, "lines": lines,
            "reviewCount": len(reviews), "terminalCount": len(terminals),
            "terminalStatus": terminals[0]["status"] if terminals else "",
            "reviewStatus": reviews[0]["status"] if reviews else ""}


def main() -> int:
    cases = [
        run_case("批准后执行", approve=True, note="我确认这条命令"),
        run_case("拒绝后不执行", approve=False, note="这条命令我不批"),
        run_case("审查关闭时直接执行", approve=True, mode="off"),
    ]
    print("=" * 84)
    for case in cases:
        print("场景：%s（chat_review=%s，点的是「%s」）"
              % (case["name"], case["mode"], "批准" if case["approve"] else "拒绝"))
        print("-" * 84)
        for line in case["lines"]:
            print(line)
        print("-" * 84)
        print("  审查单 %d 张，最终状态 %s；终端记录 %d 条 %s"
              % (case["reviewCount"], case["reviewStatus"] or "（无）",
                 case["terminalCount"], case["terminalStatus"] or ""))
        if case["mode"] == "off":
            print("  挂起时文件是否存在：%s（这一档没有审查，所以是直接执行）"
                  % case["pendingFileExisted"])
        else:
            print("  挂起那一刻命令写的文件**在不在**：%s"
                  "  ← False 才是对的：审查真的挡在执行前面"
                  % case["pendingFileExisted"])
        print("  最终文件内容：%s" % (case["landed"] or "（不存在）"))
        verdict = {"批准后执行": "批准 → 文件应当存在",
                   "拒绝后不执行": "拒绝 → 文件**不**应当存在",
                   "审查关闭时直接执行": "关掉审查 → 文件存在且没有审查单"}[case["name"]]
        ok = (verdict.startswith("批准") and case["landed"])
        if case["name"] == "拒绝后不执行":
            ok = case["landed"] is None and case["reviewStatus"] == "denied"
        if case["name"] == "审查关闭时直接执行":
            ok = bool(case["landed"]) and case["reviewCount"] == 0
        print("  %s %s" % ("✓" if ok else "✗", verdict))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
