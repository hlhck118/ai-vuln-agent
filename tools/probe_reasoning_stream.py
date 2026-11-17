# -*- coding: utf-8 -*-
"""思考块「流式」探针：思考文字是**原地长出来**的，还是每帧被整块新画一遍。

## 它回答的问题

上一轮修的是「点开后被下一帧折回去」。修完后的症状变成另一句：「能打开了，但是
无法显示思考时的流式输出，一直在刷新」。

两种可能，源码文本分不开：

| 可能 | 判定方式 |
|---|---|
| 节点每帧被销毁重建（读起来像在刷新） | `.chat-reasoning` / `.chat-reasoning-text` 的**节点身份**变不变 |
| 文字确实在原地增长 | `<pre>` 的字符数是否单调上升、是否一直贴着底 |

所以探针不「看一眼截图」，而是让真浏览器跑一段**按真实节拍回放**的思考流
（夹具里 600 帧 reasoning 铺在 `done` 之前，每帧间隔 45ms），在页面里装一个
100ms 采样器，记录：节点身份（挂上去的 `__pid`）、字符数、`scrollTop/maxScroll`、
以及累计造了多少个思考块/正文块。

## 两个场景，同一份夹具

`before` 把 `renderLive` 里那段换回**重建版**（在临时副本上改，绝不碰仓库里的
`web/chat.js`），`after` 用仓库现状。两次跑同一段流、同一个采样器，数字并排打出来：

| 量 | 重建版（旧） | 原地长（新） |
|---|---|---|
| 思考块身份数 | 每帧一个（几十个） | 1 |

## 用法

    python tools/probe_reasoning_stream.py [--seconds 6]

输出落在 `.scratch/reason/stream-before.json` / `stream-after.json`，以及每个场景
两张相隔约 1.8 秒的截图（用来看「同一块地方的字变多了」）。
"""
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import render_ui_fixture                                        # noqa: E402
from probe_reasoning import BROWSERS, reasoning_frames           # noqa: E402

OUT_DIR = os.path.join(ROOT, ".scratch", "reason")

# 每 100ms 采一次样。采样器必须在流开始之前就位（`addScriptToEvaluateOnNewDocument`），
# 否则第一帧的节点身份就丢了。
SAMPLER = r"""
(function () {
  window.__trace = [];
  window.__added = 0;
  window.__removed = 0;
  window.__boxes = 0;
  window.__pres = 0;
  function has(node, cls) {
    return !!(node && node.nodeType === 1 && node.classList && node.classList.contains(cls));
  }
  document.addEventListener('DOMContentLoaded', function () {
    new MutationObserver(function (list) {
      for (var i = 0; i < list.length; i++) {
        var added = list[i].addedNodes, removed = list[i].removedNodes, j;
        for (j = 0; j < added.length; j++) if (has(added[j], 'chat-reasoning')) window.__added++;
        for (j = 0; j < removed.length; j++) if (has(removed[j], 'chat-reasoning')) window.__removed++;
      }
    }).observe(document.documentElement, { childList: true, subtree: true });
    var opened = false;
    setInterval(function () {
      var box = document.querySelector('.chat-reasoning');
      if (!box || opened) return;
      var summary = box.querySelector('summary');
      if (summary) { summary.click(); opened = true; }
    }, 50);
    function sample() {
      var box = document.querySelector('.chat-reasoning');
      var pre = box ? box.querySelector('.chat-reasoning-text') : null;
      if (box && !box.__pid) box.__pid = 'n' + (++window.__boxes);
      if (pre && !pre.__pid) pre.__pid = 't' + (++window.__pres);
      var size = box ? box.querySelector('.chat-reasoning-size') : null;
      var sel = window.getSelection();
      // 「用户往上滚」：内容一够长就把这一块滚到顶，之后只看它会不会被拽回去。
      // 用脚本设 scrollTop 而不是派发 wheel——无头浏览器不保证合成滚轮生效，而
      // 被测的是"下一帧会不会覆盖用户的位置"，不是输入事件本身。
      if (pre && !window.__scrolledAt && pre.scrollHeight - pre.clientHeight > 80) {
        window.__scrolledAt = performance.now();
        pre.scrollTop = 0;
      }
      window.__trace.push({
        t: Math.round(performance.now()),
        ch: pre ? (pre.textContent || '').length : -1,
        n: document.querySelectorAll('.chat-reasoning').length,
        pid: box ? box.__pid : '',
        tpid: pre ? pre.__pid : '',
        open: box ? !!box.open : null,
        st: pre ? Math.round(pre.scrollTop) : 0,
        mx: pre ? Math.max(0, pre.scrollHeight - pre.clientHeight) : 0,
        chh: pre ? pre.clientHeight : 0,
        sz: size ? size.textContent : '',
        add: window.__added,
        rem: window.__removed,
        boxes: window.__boxes,
        pres: window.__pres,
        selInPre: !!(sel && sel.anchorNode && pre && pre.contains(sel.anchorNode)),
        scrolled: window.__scrolledAt || 0
      });
      setTimeout(sample, 100);
    }
    setTimeout(sample, 100);
  });
})();
"""

# 旧实现的等价物：**每帧重造**——不复用上一块，并且先把旧的从文档里摘掉。
# 只改这一处就够：`renderLive` 的槽位仍会把它 append 回去，但节点身份每帧都换。
LEGACY_SWAP = [
    ("    let box = node.__reasoning || null;",
     "    let box = null;\n    if (node.__reasoning) node.__reasoning.remove();"),
]


def _browser():
    for path in BROWSERS:
        if path and os.path.exists(path):
            return path
    return ""


class CDP(object):
    """够用就行的 DevTools 客户端：一次请求一次应答，没有事件订阅。"""

    def __init__(self, ws_url):
        import websocket
        self.ws = websocket.create_connection(ws_url, timeout=30,
                                              suppress_origin=True)
        self.seq = 0

    def call(self, method, **params):
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        while True:
            raw = self.ws.recv()
            if not raw:
                raise RuntimeError("浏览器连接断了")
            message = json.loads(raw)
            if message.get("id") == self.seq:
                if "error" in message:
                    raise RuntimeError("%s: %s" % (method, message["error"]))
                return message.get("result") or {}

    def eval(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            raise RuntimeError(str(result["exceptionDetails"])[:400])
        return (result.get("result") or {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _wait_endpoint(port, seconds=30.0):
    deadline = time.time() + seconds
    url = "http://127.0.0.1:%d/json/version" % port
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            time.sleep(0.15)
    return None


def _page_ws(port):
    with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=5) as resp:
        targets = json.loads(resp.read().decode("utf-8"))
    for item in targets:
        if item.get("type") == "page":
            return item["webSocketDebuggerUrl"]
    return ""


def trace(html_path, label, seconds=6.0, shots=(2.2, 4.0)):
    """跑一段真实节拍的流，返回采样列表、夹具错误与截图路径。"""
    browser = _browser()
    if not browser:
        return None, "SKIP：本机没找到 Edge / Chrome", []
    profile = tempfile.mkdtemp(prefix="reason-stream-profile-")
    port = 9300 + (os.getpid() % 400)
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
           "--no-default-browser-check", "--hide-scrollbars",
           "--user-data-dir=" + profile, "--window-size=1440,1000",
           "--remote-debugging-port=%d" % port, "--remote-allow-origins=*",
           "about:blank"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    files = []
    try:
        if not _wait_endpoint(port):
            return None, "FAIL：浏览器没起来", []
        ws_url = _page_ws(port)
        if not ws_url:
            return None, "FAIL：没有 page target", []
        cdp = CDP(ws_url)
        try:
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            cdp.call("Page.addScriptToEvaluateOnNewDocument", source=SAMPLER)
            url = "file:///" + os.path.abspath(html_path).replace("\\", "/")
            cdp.call("Page.navigate", url=url)
            start = time.time()
            for index, at in enumerate(shots):
                wait = at - (time.time() - start)
                if wait > 0:
                    time.sleep(wait)
                png = os.path.join(OUT_DIR, "stream-%s-%d.png" % (label, index + 1))
                data = cdp.call("Page.captureScreenshot", format="png").get("data")
                with open(png, "wb") as handle:
                    handle.write(base64.b64decode(data))
                files.append(png)
            wait = seconds - (time.time() - start)
            if wait > 0:
                time.sleep(wait)
            samples = cdp.eval("JSON.stringify(window.__trace)")
            errors = cdp.eval("document.documentElement.getAttribute('data-fixture-errors')")
        finally:
            cdp.close()
        return json.loads(samples or "[]"), errors, files
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def summarise(samples, note):
    rows = [s for s in samples if s.get("n")]
    if not rows:
        return {"ok": False, "note": note or "(没有思考块)", "samples": len(samples)}
    chars = [row["ch"] for row in rows]
    grew = sum(1 for a, b in zip(chars, chars[1:]) if b > a)
    shrank = sum(1 for a, b in zip(chars, chars[1:]) if b < a)
    flat = sum(1 for a, b in zip(chars, chars[1:]) if b == a)
    pinned = [row for row in rows if row["mx"] > 0]
    at_bottom = sum(1 for row in pinned if row["st"] >= row["mx"] - 2)
    scrolled = [row for row in rows if row.get("scrolled") and row["mx"] > 0]
    kept = sum(1 for row in scrolled if row["st"] < 24)
    return {
        "ok": True, "note": note or "",
        "samples": len(rows), "span_ms": rows[-1]["t"] - rows[0]["t"],
        "boxes_created": rows[-1]["boxes"], "pres_created": rows[-1]["pres"],
        "box_reinserts": rows[-1]["add"] - rows[-1]["boxes"],
        "open_ratio": round(sum(1 for row in rows if row["open"]) / float(len(rows)), 2),
        "chars_first": chars[0], "chars_last": chars[-1],
        "grew": grew, "flat": flat, "shrank": shrank,
        "scrollable_samples": len(pinned),
        "at_bottom_ratio": (round(at_bottom / float(len(pinned)), 2) if pinned else None),
        "scrolled_samples": len(scrolled),
        "scroll_kept_ratio": (round(kept / float(len(scrolled)), 2) if scrolled else None),
        "first_open_ms": next((row["t"] for row in rows if row["open"]), None),
    }


def _frames():
    cache = os.path.join(OUT_DIR, "data.json")
    if os.path.exists(cache):
        with open(cache, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    else:
        print("采集夹具数据（真跑一轮扫描，几分钟）...", flush=True)
        data = render_ui_fixture.collect()
        with open(cache, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
    frames = reasoning_frames(data.get("__stream__") or [], count=600)
    data["__stream__"] = frames
    return data, frames


def build(path, data, frames):
    stop = render_ui_fixture._stop_before_done(frames)
    render_ui_fixture.build(path, data, tab="chat", stream=True, stream_stop=stop)
    return path


def legacy_webdir():
    """造一份「重建版」的 web/ 临时副本。

    只在副本上改：仓库里的 `web/chat.js` 是**被测对象**，探针不能为了造对照组去动它
    （否则一次失败就会把修复留在半路上）。
    """
    src = render_ui_fixture.WEB_DIR
    dst = tempfile.mkdtemp(prefix="reason-legacy-web-")
    for name in sorted(os.listdir(src)):
        item = os.path.join(src, name)
        if os.path.isfile(item):
            shutil.copy2(item, os.path.join(dst, name))
    path = os.path.join(dst, "chat.js")
    text = io.open(path, encoding="utf-8", newline="").read()
    for needle, replacement in LEGACY_SWAP:
        if "\r\n" in text:
            needle = needle.replace("\n", "\r\n")
            replacement = replacement.replace("\n", "\r\n")
        if text.count(needle) != 1:
            raise SystemExit("FAIL：对照组锚点不唯一：%r" % needle)
        text = text.replace(needle, replacement, 1)
    io.open(path, "w", encoding="utf-8", newline="").write(text)
    return dst


def main(argv):
    seconds = 6.0
    for index, arg in enumerate(argv):
        if arg == "--seconds" and index + 1 < len(argv):
            seconds = float(argv[index + 1])
    os.makedirs(OUT_DIR, exist_ok=True)
    if not _browser():
        print("SKIP：本机没找到 Edge / Chrome，跳过思考块流式探针。")
        return 0

    data, frames = _frames()
    print("夹具：%d 帧（思考帧铺在 done 之前，每帧间隔 45ms）" % len(frames))

    # 顺序很要紧：先在临时副本上用**重建版**建夹具，再恢复 WEB_DIR 建新版。
    # 仓库里的 web/chat.js 一次都不动——它是被测对象，探针不给自己造对照组时改它。
    legacy_dir = legacy_webdir()
    results = []
    for label, webdir in (("before", legacy_dir), ("after", None)):
        if webdir:
            original = render_ui_fixture.WEB_DIR
            render_ui_fixture.WEB_DIR = webdir
            html = build(os.path.join(OUT_DIR, "stream-%s.html" % label), data, frames)
            render_ui_fixture.WEB_DIR = original
        else:
            html = build(os.path.join(OUT_DIR, "stream-%s.html" % label), data, frames)
        samples, note, shots = trace(html, label, seconds=seconds)
        if samples is None:
            print("%s: %s" % (label, note))
            return 0 if note.startswith("SKIP") else 2
        result = summarise(samples, note)
        result["label"] = label
        with open(os.path.join(OUT_DIR, "stream-%s.json" % label), "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=1)
        with open(os.path.join(OUT_DIR, "stream-%s.trace.json" % label), "w", encoding="utf-8") as handle:
            json.dump(samples, handle, ensure_ascii=False)
        result["shots"] = shots
        results.append(result)
        print("%s：采样 %d 次 / %d ms" % (label, result.get("samples", 0), result.get("span_ms", 0)))
        if not result.get("ok"):
            print("  FAIL：%s" % result.get("note"))
            continue
        print("  思考块新造 %d 个、正文块新造 %d 个（重复插入 %d 次）→ %s"
              % (result["boxes_created"], result["pres_created"], result["box_reinserts"],
                 "每帧重建" if result["boxes_created"] > 1 else "同一块原地长"))
        print("  字符 %d → %d（涨 %d / 平 %d / 掉 %d），展开 %.2f，贴底 %s"
              % (result["chars_first"], result["chars_last"], result["grew"],
                 result["flat"], result["shrank"], result["open_ratio"],
                 result["at_bottom_ratio"]))
        print("  往上滚之后仍停在原处：%s（采样 %d 次）"
              % (result["scroll_kept_ratio"], result["scrolled_samples"]))
        for shot in result["shots"]:
            print("  截图：%s" % shot)

    rows = [item for item in results if item.get("ok")]
    if len(rows) == 2:
        print("\n%-8s %-10s %-10s %-12s %-8s %-8s"
              % ("场景", "思考块个数", "正文块个数", "字符", "展开", "贴底"))
        for item in rows:
            print("%-8s %-12s %-12s %-14s %-8s %-8s"
                  % (item["label"], item["boxes_created"], item["pres_created"],
                     "%d→%d" % (item["chars_first"], item["chars_last"]),
                     item["open_ratio"], item["at_bottom_ratio"]))
    # 三个条件缺一不可：旧场景真的每帧重造；新场景只造一个；并且**用户往上滚之后**
    # 新场景留在原处、旧场景被拽回底部（后者不成立就说明这个场景没测到滚动）。
    verdict = (len(rows) == 2
               and rows[0]["boxes_created"] > 1
               and rows[1]["boxes_created"] == 1
               and rows[1].get("scroll_kept_ratio") is not None
               and rows[1]["scroll_kept_ratio"] >= 0.9
               and rows[0].get("scroll_kept_ratio") is not None
               and rows[0]["scroll_kept_ratio"] <= 0.2)
    print("结论：%s" % ("PASS" if verdict else "**不符**"))
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
