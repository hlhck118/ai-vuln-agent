# -*- coding: utf-8 -*-
"""测试用的 MCP 服务（stdio 传输）。**不是测试用例**，是一个真的能被拉起来的服务。

## 为什么是一个真进程，而不是一个替身

`mcp._StdioSession` 里最容易写错的三处，替身对象一个都测不出来：

* **换行分隔的 JSON 帧** —— 一次写一行、一次读一行，多写一个换行就再也对不上；
* **stderr 必须被读掉** —— 不读的话，这个服务往 stderr 写满管道缓冲区就会卡死，
  而症状是"工具超时"，看起来像服务慢；
* **进程死掉要能被发现** —— 读队列要带小超时地取，否则进程没了只能干等满超时。

所以这个文件是一个**真的会读 stdin、写 stdout、往 stderr 写日志**的程序，由
`subprocess` 拉起来。它与被测代码之间只有协议，没有共享内存——这正是要测的东西。

## 它提供的工具

| 工具 | 用途 |
|---|---|
| `echo` | 回显一个参数，用来验证参数确实传到了服务那边 |
| `read_file` / `read.file` | 两个**会被 slug 撞成同一个名字**的工具名 |
| `fail` | `isError: true`，验证失败结果不被当成成功 |
| `big` | 返回超过上限的文本，验证截断 |
| `noise` | 先吐一行非 JSON，再吐响应，验证噪声被跳过 |
| `stderr` | 先往 stderr 写，再回结果，验证 stderr 被读掉 |
| `poll` | 先发一个**反向请求**（`sampling/createMessage`），验证客户端会回错误 |
| `slow` | 睡很久，验证超时 |
| `die` | 直接退出，验证"进程没了"被报成 closed 而不是干等超时 |
| `blocks` | 返回非文本内容块与 structuredContent |
"""
import json
import os
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "回显 text 参数",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                        "required": ["text"]},
    },
    {
        "name": "read_file",
        "description": "读一个文件（示意）",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "read.file",
        "description": "与 read_file 会撞成同一个工具名",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
    {"name": "fail", "description": "总是失败", "inputSchema": {"type": "object"}},
    {"name": "big", "description": "返回一大段文本", "inputSchema": {"type": "object"}},
    {"name": "noise", "description": "先吐一行噪声", "inputSchema": {"type": "object"}},
    {"name": "stderr", "description": "先写 stderr", "inputSchema": {"type": "object"}},
    {"name": "poll", "description": "反向请求一次", "inputSchema": {"type": "object"}},
    {"name": "slow", "description": "睡很久", "inputSchema": {"type": "object"}},
    {"name": "die", "description": "直接退出", "inputSchema": {"type": "object"}},
    {"name": "blocks", "description": "非文本内容块", "inputSchema": {"type": "object"}},
]


def _send(payload):
    """写一行 JSON-RPC。

    **显式用 UTF-8 字节写**，不用 `sys.stdout.write`：在 Windows 上后者的编码是
    控制台的代码页（cp950/936/1252…），于是 `ensure_ascii=False` 的中文会被编成
    Big5 或 GBK 字节 —— 而 MCP 的 stdio 传输规定是 UTF-8，客户端按 UTF-8 解出来
    就是乱码（真实服务踩这个坑的表现是"工具说明是问号"）。
    """
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:                          # pragma: no cover - buf 一直在
        sys.stdout.write(data.decode("utf-8"))
        sys.stdout.flush()
        return
    stream.write(data)
    stream.flush()


def _text(message):
    return {"content": [{"type": "text", "text": message}]}


def _call(name, args):
    if name == "echo":
        return _text("echo:%s" % args.get("text"))
    if name in ("read_file", "read.file"):
        # 回显**原始**工具名，调用方据此判断到底调的是哪一个
        return _text("%s -> /%s" % (name, args.get("path") or ""))
    if name == "fail":
        return {"content": [{"type": "text", "text": "这个工具故意失败"}],
                "isError": True}
    if name == "big":
        return _text("B" * 40000)
    if name == "noise":
        sys.stdout.write("这一行不是 JSON，客户端应当跳过\n")
        sys.stdout.flush()
        return _text("噪声之后的正常结果")
    if name == "stderr":
        sys.stderr.write("这是 stderr 上的一段日志\n")
        sys.stderr.flush()
        return _text("stderr 已写过")
    if name == "poll":
        _send({"jsonrpc": "2.0", "id": 9001, "method": "sampling/createMessage",
               "params": {"messages": []}})
        # 等客户端回一条错误（或超时）；把结果写进返回文本，测试据此断言
        line = _readline()
        return _text("反向请求的答复：%s" % (line.strip() or "（没有答复）"))
    if name == "slow":
        time.sleep(float(args.get("seconds") or 30))
        return _text("醒了")
    if name == "die":
        sys.exit(3)
    if name == "blocks":
        return {"content": [{"type": "text", "text": "文本部分"},
                            {"type": "image", "mimeType": "image/png"}],
                "structuredContent": {"ok": True, "n": 7}}
    return {"content": [{"type": "text", "text": "未知工具 %s" % name}],
            "isError": True}


def _readline():
    """读一行，**显式按 UTF-8 解码**。

    与 `_send` 对称：在 Windows 上 `sys.stdin` 的编码是控制台代码页，于是客户端
    按 UTF-8 发过来的中文会被解成 `浣犲ソ` 那一类乱码，而这个服务再把乱码回显回去
    —— 症状是"参数传过去就坏了"，看起来像客户端编码错了。
    """
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:                          # pragma: no cover - 交互式输入
        return sys.stdin.readline()
    raw = stream.readline()
    if not raw:
        return ""
    return raw.decode("utf-8", "replace")


def main():
    if "--selftest" in sys.argv:
        # 不连协议，只回答"这个脚本能跑起来吗"，给排查用。
        sys.stdout.write("ok %d tools\n" % len(TOOLS))
        return 0
    while True:
        raw = _readline()
        if not raw:
            break
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method = message.get("method") or ""
        rid = message.get("id")
        if method == "initialize":
            if rid is not None:
                _send({"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fixture", "version": "0.1"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            if rid is not None:
                _send({"jsonrpc": "2.0", "id": rid,
                       "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = message.get("params") or {}
            outcome = _call(str(params.get("name") or ""),
                            params.get("arguments") or {})
            if rid is not None:
                _send({"jsonrpc": "2.0", "id": rid, "result": outcome})
        elif rid is not None:
            _send({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32601, "message": "没有这个方法：%s" % method}})
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
