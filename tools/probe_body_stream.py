# -*- coding: utf-8 -*-
"""正文「流式」探针：回答是**原地长**出来的，还是每帧整篇重画一遍。

## 它回答的问题

思考块修好之后，同一个气泡里还剩一件容易被忽略的事：正文（`.chat-msg-body`）。
旧写法每帧 `refill(node.__body, liveBody())`——整篇回答（每一段、每个代码块、每张
表格）都重新解析、重新建节点。长回答里这等于每 16ms 抖一次整页正文：选中的文字、
刚滚到的位置都可能被换掉，而真正在变的只有最后那一段。

## 怎么判

不「看一眼截图」，而是让真浏览器按真实节拍回放一段**带空行的正文流**，页面里装一个
100ms 采样器，记录：

- `body.firstElementChild` 的节点身份（`__pid`）：整块重画时每帧都换，增量时一直是同一个；
- 正文里元素级的 `added` / `removed` 次数：整块重画两样都大，增量时只有新段落被加进去；
- 正文字符数：必须单调上升（没在长说明流没跑，那这条探针什么都没证明）。

两个场景，同一份夹具：`before` 在**临时副本**上把 `paintLiveBody(node)` 换回
`refill(node.__body, liveBody())`（仓库里的 `web/chat.js` 一次都不动），`after`
用仓库现状。

| 量 | 整篇重画（旧） | 原地增量（新） |
|---|---|---|
| 首个段落的身份数 | 42（几乎每帧一个） | 7（开头占位切换 + 工具边界） |
| 首段变化的采样点 | 41 / 42 | 6 / 42 |
| 元素级 added / removed | 3018 / 2941 | 87 / 10 |

判定要求：旧场景首段变化 ≥ 采样数一半、删除 ≥100；新场景首段变化 ≤8、删除 ≤20、
元素写入比旧场景少一个数量级。**工具边界**是正文里真正该重画的时刻：那一段文字刚
定型，结尾的"正在…"要换回来。除那一刻之外每帧都只追加。
对照组没造对时两边一样，那时探针什么都没证明。

## 用法

    python tools/probe_body_stream.py [--seconds 5]

输出 `.scratch/reason/body-before.json` / `body-after.json` 与每个场景两张截图。
无浏览器时打印 SKIP 并以 0 退出（与其它探针一致）。
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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import render_ui_fixture                                        # noqa: E402
from probe_reasoning_stream import (CDP, _browser, _page_ws,    # noqa: E402
                                    _wait_endpoint)

OUT_DIR = os.path.join(ROOT, ".scratch", "reason")

SAMPLER = r"""
(function () {
  window.__trace = [];
  window.__adds = 0;
  window.__rems = 0;
  window.__kids = 0;
  function body() {
    return document.querySelector('.chat-live .chat-msg-body');
  }
  function inside(node) {
    var b = body();
    if (!b || !node) return false;
    if (node === b) return true;
    if (node.nodeType === 1) return b.contains(node);
    return !!(node.parentNode && b.contains(node.parentNode));
  }
  document.addEventListener('DOMContentLoaded', function () {
    new MutationObserver(function (list) {
      for (var i = 0; i < list.length; i++) {
        if (!inside(list[i].target)) continue;
        var j, added = list[i].addedNodes, removed = list[i].removedNodes;
        for (j = 0; j < added.length; j++) if (added[j].nodeType === 1) window.__adds++;
        for (j = 0; j < removed.length; j++) if (removed[j].nodeType === 1) window.__rems++;
      }
    }).observe(document.documentElement, { childList: true, subtree: true });
    function sample() {
      var b = body();
      if (b) {
        var first = b.firstElementChild;
        if (first && !first.__pid) first.__pid = 'k' + (++window.__kids);
        window.__trace.push({
          t: Math.round(performance.now()),
          ch: (b.textContent || '').length,
          pid: first ? first.__pid : '',
          adds: window.__adds,
          rems: window.__rems,
          kids: window.__kids,
          blocks: b.querySelectorAll('p').length
        });
      }
      setTimeout(sample, 100);
    }
    setTimeout(sample, 100);
  });
})();
"""

# 对照组：把"逐帧只重画尾巴"换回"每帧整块重填正文"。
LEGACY_SWAP = [
    ("    paintLiveBody(node);", "    refill(node.__body, liveBody());"),
]


def content_frames(frames, count=180):
    """在 `done` 之前铺一段带空行的正文流（每帧一段，段落之间空行）。"""
    out = list(frames)
    at = len(out)
    for index in range(len(out) - 1, -1, -1):
        if out[index] and out[index][0] == "done":
            at = index
            break
    injected = []
    for i in range(count):
        text = "第 %d 段：先确认目标可达性，再看端口暴露面与版本指纹。\n\n" % (i + 1)
        injected.append(["delta", {"text": text}])
    return out[:at] + injected + out[at:]


def legacy_webdir():
    src = render_ui_fixture.WEB_DIR
    dst = tempfile.mkdtemp(prefix="body-legacy-web-")
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


def trace(html_path, label, seconds=5.0, shots=(1.6, 3.4)):
    browser = _browser()
    if not browser:
        return None, "SKIP：本机没找到 Edge / Chrome", []
    profile = tempfile.mkdtemp(prefix="body-stream-profile-")
    port = 9500 + (os.getpid() % 400)
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
                png = os.path.join(OUT_DIR, "body-%s-%d.png" % (label, index + 1))
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


def summarise(samples):
    rows = [s for s in samples if s.get("ch")]
    if not rows:
        return {"ok": False, "note": "(正文一直是空的：流没跑起来)", "samples": len(samples)}
    chars = [row["ch"] for row in rows]
    grew = sum(1 for a, b in zip(chars, chars[1:]) if b > a)
    return {
        "ok": True,
        "samples": len(rows),
        "span_ms": rows[-1]["t"] - rows[0]["t"],
        "first_child_ids": rows[-1]["kids"],
        "added": rows[-1]["adds"],
        "removed": rows[-1]["rems"],
        "blocks_first": rows[0]["blocks"],
        "blocks_last": rows[-1]["blocks"],
        "chars_first": chars[0],
        "chars_last": chars[-1],
        "grew_samples": grew,
        "pid_changes": sum(1 for a, b in zip(rows, rows[1:]) if a["pid"] != b["pid"]),
        "pids": [row["pid"] for row in rows],
        "trace": rows,
    }


def _frames():
    cache = os.path.join(OUT_DIR, "data.json")
    if os.path.exists(cache):
        with io.open(cache, encoding="utf-8") as handle:
            data = json.load(handle)
    else:
        print("采集夹具数据（真跑一轮扫描，几分钟）...", flush=True)
        data = render_ui_fixture.collect()
        with io.open(cache, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
    frames = content_frames(data.get("__stream__") or [], count=180)
    data["__stream__"] = frames
    return data, frames


def main(argv):
    seconds = 5.0
    for index, arg in enumerate(argv):
        if arg == "--seconds" and index + 1 < len(argv):
            seconds = float(argv[index + 1])
    os.makedirs(OUT_DIR, exist_ok=True)
    if not _browser():
        print("SKIP：本机没找到 Edge / Chrome，跳过正文流式探针。")
        return 0

    data, frames = _frames()
    print("夹具：%d 帧（正文帧铺在 done 之前，每帧间隔 45ms）" % len(frames))
    stop = render_ui_fixture._stop_before_done(frames)

    legacy_dir = legacy_webdir()
    original = render_ui_fixture.WEB_DIR
    results = {}
    try:
        for label, webdir in (("before", legacy_dir), ("after", None)):
            render_ui_fixture.WEB_DIR = webdir or original
            html_path = os.path.join(OUT_DIR, "body-%s.html" % label)
            render_ui_fixture.build(html_path, data, tab="chat", stream=True,
                                    stream_stop=stop)
            samples, errors, files = trace(html_path, label, seconds=seconds)
            if samples is None:
                print("%s：%s" % (label, errors))
                return 1
            summary = summarise(samples)
            summary["errors"] = errors or ""
            summary["shots"] = files
            results[label] = summary
            with io.open(os.path.join(OUT_DIR, "body-%s.json" % label),
                         "w", encoding="utf-8") as handle:
                json.dump(summary, handle, ensure_ascii=False, indent=2)
    finally:
        render_ui_fixture.WEB_DIR = original
        shutil.rmtree(legacy_dir, ignore_errors=True)

    played = frames[:stop] if stop else frames
    tools = sum(1 for frame in played if frame and frame[0] == "tool")
    for label in results:
        results[label]["tool_boundaries"] = tools
    print("场景       首个段落身份   元素 added/removed   正文块      字数")
    for label in ("before", "after"):
        row = results[label]
        if not row.get("ok"):
            print("  %-8s %s" % (label, row.get("note")))
            continue
        print("  %-8s %-13d %-19s %-10s %d 字→%d 字"
              % (label, row["first_child_ids"],
                 "%d / %d" % (row["added"], row["removed"]),
                 "%d→%d" % (row["blocks_first"], row["blocks_last"]),
                 row["chars_first"], row["chars_last"]))

    before, after = results["before"], results["after"]
    problems = []
    if not before.get("ok") or not after.get("ok"):
        problems.append("有一个场景没有正文")
    else:
        # 旧写法**每个采样点**都是新节点（pid_changes ≈ 采样数）。新写法只在开头那串
        # 占位切换（"正在推理…" ↔ "正在推理与调用工具…"）与每个工具边界整块重画一次；
        # 正文一旦开写，后面几十个采样点都是同一个段落节点。
        if before["pid_changes"] < max(10, before["samples"] // 2):
            problems.append("对照组没造对：旧写法只有 %d 次首段变化（应几乎每帧一次）"
                            % before["pid_changes"])
        if before["removed"] < 100:
            problems.append("对照组没造对：旧写法只删了 %d 次（应反复删整篇）"
                            % before["removed"])
        if after["pid_changes"] > 8:
            problems.append("新写法首段变了 %d 次（上限 8：开头占位切换 + 工具边界）"
                            % after["pid_changes"])
        if after["pid_changes"] * 4 > before["pid_changes"]:
            problems.append("新写法的首段变化没有降到旧写法的四分之一：%d vs %d"
                            % (after["pid_changes"], before["pid_changes"]))
        if after["removed"] > 20:
            problems.append("新写法删了 %d 个正文节点（上限 20：只在工具边界清一次）"
                            % after["removed"])
        if after["added"] * 20 > before["added"]:
            problems.append("新写法的元素写入没有数量级下降：%d vs %d"
                            % (after["added"], before["added"]))
        if after["chars_last"] <= after["chars_first"]:
            problems.append("正文没在长：%d → %d" % (after["chars_first"], after["chars_last"]))
    if problems:
        print("结论：**不符**")
        for item in problems:
            print("  - " + item)
        return 1
    print("结论：PASS（旧：每帧换首个段落、反复删整篇；新：只在工具边界重画一次，其余帧只追加）")
    for label in ("before", "after"):
        for shot in results[label].get("shots") or []:
            print("截图：%s" % shot)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
