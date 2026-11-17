# -*- coding: utf-8 -*-
"""思考块探针：流还在往外冒的时候点开「思考过程」，它会不会被下一帧折回去。

## 为什么需要它

`renderLive()` 每收到一帧就 `clear` 再整块重建「正在生成」的气泡（不重建就得整片
重排，使用者往上翻的位置会被顶回去）。而 `<details>` 的展开状态住在 DOM 上 ——
重建一次就没了。症状是：思考还在涨的时候点开，下一帧（≤16ms）它自己折回去，
看起来像「点了没反应」。

源码文本只能证明「代码里存了状态」；「点下去真的还开着」只能让真浏览器跑一遍流，
在点击之后**再让它重建几十次**，然后问它 open 还是 closed。而「打开状态被抹掉」
这种失败不会让任何一条功能测试变红 —— 所以它需要自己的探针。

## 判定标准

| # | 场景 | 期望 |
|---|---|---|
| 1 | 真代码 + 点击 + 等 900ms（期间思考还在涨） | `open=true`，且字数标签变过 |
| 2 | 不许复用旧块 + 造块时忽略状态，再跑同一场 | `open=false` |

第 2 行不是可选的：没有它，探针可能只是「什么都没发生」而报 PASS。
第 1 行里「字数变过」同样是判据：流若没在动，点击之后什么都没有发生，
`open=true` 是**假通过**。两条都满足才算 PASS。

**对照要改两处**（`let reasoning = null;` 与 `reasoningBlock(..., false)`）。修复前
块是每帧重建的，只把状态写死成 `false` 就够了；修复后块不再重建，只改一处折不动
——这是"两件事都在"的结构，不是探针失灵。

## 两个夹具层面的造假，都写在这里，不藏在代码里

* **思考帧是注入的**：夹具的假模型（`render_ui_fixture._StreamingModel`）只发
  `tool_calls` 与 `content`，不发 `reasoning`。所以在抓到的帧序列里插 600 帧
  `reasoning`，铺在 `done` 之前 —— 插在开头的话，等探针认出思考块时（页面初始化
  要好几秒），那些帧早就放完了，点击之后一次重建都不会发生。
* **rAF 用 16ms 定时器顶替**：无头浏览器在 `--virtual-time-budget` 下不产
  BeginFrame，`requestAnimationFrame` 可能一次都不来（实测：整条流只重建了一两次）。
  被测的是 `renderLive` 的重建逻辑，不是调度器；换掉调度器之后节流节奏仍是 ~60fps。

## 用法

    python tools/probe_reasoning.py [输出目录] [--reuse]

`--reuse` 复用输出目录里的 `data.json`（上一次 `collect()` 的真实帧），省掉几分钟的
真扫描。需要本机有 Edge 或 Chrome（没有时打印 SKIP 并以 0 退出，不误报失败）。
本工具**不进单元测试套件**：`python -m unittest discover -s tests` 必须零外部依赖。
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import render_ui_fixture                                        # noqa: E402

# 与 probe_dock_dismiss / probe_ui_style 同一串候选：本机装哪个用哪个。
from _browser import BROWSERS    # noqa: E402

PROBE = r"""
(function () {
  // 无头虚拟时钟不产 BeginFrame，rAF 不可靠（见文件头）。换 16ms 定时器，
  // 节流节奏与线上一致；被测的是重建逻辑，不是调度器。
  window.requestAnimationFrame = function (cb) {
    return setTimeout(function () { cb(performance.now()); }, 16);
  };
  window.cancelAnimationFrame = function (id) { clearTimeout(id); };
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  async function run() {
    var out = { ok: false, error: null, waited: 0, before: null, after: null,
                openImmediately: null, nodes: 0, renders: 0, open: null,
                notes: null, live: 0, msgs: 0 };
    try {
      var i = 0;
      for (; i < 400 && !document.querySelector('.chat-reasoning'); i++) await sleep(50);
      out.waited = i * 50;
      out.notes = document.documentElement.getAttribute('data-fixture-errors');
      out.live = document.querySelectorAll('.chat-live').length;
      out.msgs = document.querySelectorAll('.chat-msg').length;
      var box = document.querySelector('.chat-reasoning');
      if (!box) { throw new Error('no .chat-reasoning（流里没有思考帧）'); }
      var size = box.querySelector('.chat-reasoning-size');
      out.before = size ? size.textContent : null;
      var summary = box.querySelector('summary');
      if (!summary) { throw new Error('思考块里没有 summary'); }
      summary.click();
      out.openImmediately = document.querySelector('.chat-reasoning').open;
      await sleep(900);
      var now = document.querySelector('.chat-reasoning');
      if (!now) { throw new Error('等待期间思考块整块消失了'); }
      out.nodes = document.querySelectorAll('.chat-reasoning').length;
      var sizeNow = now.querySelector('.chat-reasoning-size');
      out.after = sizeNow ? sizeNow.textContent : null;
      out.renders = (out.after !== out.before) ? 1 : 0;
      out.open = now.open;
      out.ok = now.open === true && out.renders === 1;
    } catch (e) {
      out.error = String(e);
    }
    document.documentElement.setAttribute(
      'data-reason-probe',
      btoa(unescape(encodeURIComponent(JSON.stringify(out)))));
  }
  run();
})();
"""

CLICK_LATER = (
    "<script>(function(){"
    # 截图那一版也要把 rAF 换成定时器：否则重建可能一次都不发生，图就证明不了
    # 「点开之后还开着」是修复的结果（反过来，有重建才可能把旧行为折回去）。
    "window.requestAnimationFrame=function(cb){return setTimeout(function(){cb(performance.now());},16);};"
    "window.cancelAnimationFrame=function(id){clearTimeout(id);};"
    # 等思考块出现再点：页面初始化要几秒，按固定时刻点常常点在空气上（第一版截图
    # 就是这么折着的）。
    "var tries=0;var iv=setInterval(function(){"
    "var b=document.querySelector('.chat-reasoning');"
    "if(b){var s=b.querySelector('summary');if(s)s.click();clearInterval(iv);return;}"
    "if(++tries>200)clearInterval(iv);},50);"
    "})();</script>"
)


def reasoning_frames(frames, count=600):
    """在 `done` 之前铺进 `count` 帧思考，让点击之后还有几十帧要来。"""
    out = list(frames)
    at = len(out)
    for index in range(len(out) - 1, -1, -1):
        if out[index] and out[index][0] == "done":
            at = index
            break
    injected = [["reasoning", {"text": "第 %d 段思考：先确认目标可达性，再看端口暴露面。"
                               % (i + 1)}] for i in range(count)]
    return out[:at] + injected + out[at:]


def _browser():
    for path in BROWSERS:
        if path and os.path.exists(path):
            return path
    return ""


def _dump(browser, html_path, wait_ms=120000):
    profile = tempfile.mkdtemp(prefix="reason-probe-profile-")
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
           "--user-data-dir=" + profile,
           "--window-size=1440,1000",
           "--virtual-time-budget=%d" % wait_ms,
           "--dump-dom", "file:///" + os.path.abspath(html_path).replace("\\", "/")]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=600, check=False)
    return result.stdout


def _probe(browser, html_path, out_path):
    dom = _dump(browser, html_path)
    match = re.search(r'data-reason-probe="([^"]+)"', dom)
    if not match:
        errors = re.search(r'data-fixture-errors="([^"]*)"', dom)
        return None, (errors.group(1) if errors else "(无)")
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(dom)
    return json.loads(base64.b64decode(match.group(1)).decode("utf-8")), ""


def main(argv):
    out_dir = ""
    reuse = "--reuse" in argv
    for arg in argv:
        if not arg.startswith("--"):
            out_dir = arg
    out_dir = os.path.abspath(out_dir or os.path.join(ROOT, ".scratch", "reason"))
    os.makedirs(out_dir, exist_ok=True)

    browser = _browser()
    if not browser:
        print("SKIP：本机没找到 Edge / Chrome，跳过思考块探针。")
        return 0

    cache = os.path.join(out_dir, "data.json")
    if reuse and os.path.exists(cache):
        with open(cache, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        print("夹具数据：复用 %s" % cache)
    else:
        print("采集夹具数据（真跑一轮扫描，几分钟）...", flush=True)
        data = render_ui_fixture.collect()
        with open(cache, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)

    frames = reasoning_frames(data.get("__stream__") or [])
    data["__stream__"] = frames
    stop = render_ui_fixture._stop_before_done(frames)
    print("帧：%d（思考帧铺在 done 之前，流停在落库之前）" % len(frames))

    html = os.path.join(out_dir, "reason.html")
    render_ui_fixture.build(html, data, tab="chat", stream=True, stream_stop=stop)
    with open(html, "r", encoding="utf-8") as handle:
        text = handle.read()
    probed = text.replace("</body>", "<script>" + PROBE + "</script></body>", 1)
    fixed_html = os.path.join(out_dir, "reason-probe.html")
    with open(fixed_html, "w", encoding="utf-8") as handle:
        handle.write(probed)

    fixed, note = _probe(browser, fixed_html, os.path.join(out_dir, "reason-probe.dom.html"))
    if fixed is None:
        print("FAIL：页面没跑到探针。夹具错误：%s" % note)
        return 2

    # 反向对照：**两处一起改**才等价于修复前的行为——
    #   ① 不许复用旧块（`let reasoning = null;` → 每帧 `clear` 之后重造）；
    #   ② 造块时忽略展开状态（`keepOpen` → `false`）。
    # 只改 ② 折不动了：块不再重建，`open` 自然留着。反过来只改 ① 也折不动——
    # 重建时会把状态写回去。这与"两件事都在"的直觉一致。
    controls = [
        ("    let box = node.__reasoning || null;",
         "    let box = null;\n    if (node.__reasoning) node.__reasoning.remove();"),
        ("reasoningBlock(text, !view.live.done, view.live.reasoningOpen)",
         "reasoningBlock(text, !view.live.done, false)"),
    ]
    broken = probed
    for needle, repl in controls:
        if broken.count(needle) != 1:
            print("FAIL：占位串出现 %d 次，无法构造反向对照：%s"
                  % (broken.count(needle), needle))
            return 2
        broken = broken.replace(needle, repl, 1)
    broken_html = os.path.join(out_dir, "reason-probe-old.html")
    with open(broken_html, "w", encoding="utf-8") as handle:
        handle.write(broken)
    reverted, note = _probe(browser, broken_html,
                            os.path.join(out_dir, "reason-probe-old.dom.html"))
    if reverted is None:
        print("FAIL：反向对照没跑到探针。夹具错误：%s" % note)
        return 2

    print("夹具：%s" % html)
    print("%-8s %-8s %-8s %-6s %-8s %s"
          % ("场景", "点击后", "900ms后", "重建", "字数", "结论"))
    for label, item in (("真代码", fixed), ("反向对照", reverted)):
        print("  %-8s %-10s %-10s %-6s %-10s %s"
              % (label,
                 "开" if item.get("openImmediately") else "合",
                 "开" if item.get("open") else "合",
                 "有" if item.get("renders") else "无",
                 "%s→%s" % (item.get("before"), item.get("after")),
                 "OK" if item.get("ok") else "--"))
        if item.get("error"):
            print("    error: %s" % item["error"])

    verdict = fixed.get("ok") is True and reverted.get("open") is False
    print("结论：%s" % ("PASS" if verdict else "**不符**"))

    # 给人看的一张图：流停在半途，思考块已经展开。
    shot = os.path.join(out_dir, "reason-open.html")
    # 截图版**不带探针**：探针自己也会点一次，两次点击正好把块又折回去（第一版
    # 截图就是这么折着的 —— 探针 PASS、图却是合的）。
    with open(shot, "w", encoding="utf-8") as handle:
        handle.write(text.replace("</body>", CLICK_LATER + "</body>", 1))
    png = render_ui_fixture.screenshot(shot, os.path.join(out_dir, "reason-open.png"))
    print("截图：%s" % (png or "SKIP"))
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
