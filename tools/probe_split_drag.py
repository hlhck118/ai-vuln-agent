# -*- coding: utf-8 -*-
"""分栏拖拽探针：在真浏览器里合成一次拖动，读回宽度是否真的变了。

为什么需要它：`tests/test_ui_contract.py` 是静态检查，它能证明"宽度写在自定义
属性上、窄屏会隐藏手柄、媒体查询没有重新钉死"——但**证明不了拖得动**。方向算反、
上下限夹错、事件名写成 mousemove、漏掉 CLICK_SLOP 的判断，静态断言全都是绿的，
而这几种错的表现是"这条线拖不动 / 拖反 / 能拖到负数"，只有真的按下去才知道。

做法：复用 tools/render_ui_fixture.py 生成的真实夹具页（真 index.html + 真
style.css + 真 split.js），往页尾注入一段脚本，用合成 PointerEvent 走一遍
pointerdown → pointermove → pointerup，把结果串写进 <title>，
再用 msedge --headless --dump-dom 读回来。

两处**必须**这样写的地方：
  * 每次拖动之间排一个 400ms 的 setTimeout。split.js 用时间戳做双击复位判定
    （320ms），连着发三次 pointerdown 会被判成"双击"，测出来的就不是拖拽。
    配合 --virtual-time-budget，这些定时器在虚拟时间里立刻到期，实机仍是零耗时。
  * 最小宽度从 web/split.js 里**读出来**，不在本文件里抄一份。抄一份的下场是
    改了下限之后探针仍然按旧值断言，然后红在一个正确实现上。

用法：
    python tools/probe_split_drag.py <out_dir> [--splits chat,graph,assets]
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import render_ui_fixture as fixture            # noqa: E402

MARKER = "SPLITPROBE:"
SEED_DELTA = 37          # 恢复档的播种宽度 = 默认值 + 这个数，必须与默认不同

PANE_SELECTOR = {"chat": ".chat-side", "graph": ".side-panel", "assets": ".tree-pane"}


def split_config() -> dict:
    """从 split.js 读回每条分栏的 {min, def}。

    **不在本文件里再写一份**：那是同一份事实的两个副本，改一处就会漂移，
    而漂移的表现是"探针红在一个正确的实现上"。
    """
    with open(os.path.join(ROOT, "web", "split.js"), "r", encoding="utf-8") as handle:
        source = handle.read()
    config = {}
    for match in re.finditer(
            r"\{\s*id:\s*'([\w-]+)',[^}]*?min:\s*(\d+),\s*ratio:\s*([\d.]+),\s*def:\s*(\d+)",
            source):
        config[match.group(1)] = {"min": int(match.group(2)),
                                  "ratio": float(match.group(3)),
                                  "def": int(match.group(4))}
    return config


DRAG_PROBE = """
<script>
(function () {
  var ID = %(split)s, result = { split: ID, phase: 'drag' }, paneSel = %(pane)s;
  function pev(type, x, y) {
    return new PointerEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y,
      button: 0, buttons: 1, pointerId: 1, pointerType: 'mouse' });
  }
  function rectOf(sel) { var n = document.querySelector(sel); return n ? n.getBoundingClientRect() : null; }
  function widthOf(sel) { var r = rectOf(sel); return r ? Math.round(r.width) : -1; }
  function handle() { return document.querySelector('.split-handle[data-split="' + ID + '"]'); }
  var sign = 1;    // 栏在手柄左侧为 +1，右侧为 -1；第一步量出来
  function drag(dx, done) {
    var h = handle();
    if (!h) { done(); return; }
    var hr = h.getBoundingClientRect();
    var x = Math.round(hr.left + hr.width / 2), y = Math.round(hr.top + hr.height / 2);
    h.dispatchEvent(pev('pointerdown', x, y));
    // 分两步移动：真实指针不会一次跳到位，而跨过 CLICK_SLOP 的那一步
    // 正是"按下 vs 拖动"的判定点，一次跳到位会让这段逻辑少走一半。
    document.dispatchEvent(pev('pointermove', Math.round(x + dx / 2), y));
    document.dispatchEvent(pev('pointermove', Math.round(x + dx), y));
    document.dispatchEvent(pev('pointerup', Math.round(x + dx), y));
    done();
  }
  var steps = [
    function (next) {
      var h = handle();
      result.found = !!h;
      if (!h) { next(); return; }
      var hr = h.getBoundingClientRect(), pr = rectOf(paneSel);
      result.handleWidth = Math.round(hr.width * 100) / 100;
      result.display = window.getComputedStyle(h).display;
      result.ariaRole = h.getAttribute('role');
      result.ariaOrientation = h.getAttribute('aria-orientation');
      result.tabindex = h.getAttribute('tabindex');
      result.paneWidth0 = Math.round(pr.width);
      result.paneLeftOfHandle = pr.left <= hr.left;
      sign = result.paneLeftOfHandle ? 1 : -1;
      result.boxWidth = Math.round(h.parentElement.getBoundingClientRect().width * 100) / 100;
      // 两栏之间不得有缝、也不得叠出第二条线：手柄靠近栏的那条边就应当是栏的边
      result.edgeDelta = Math.round(Math.abs(
        (pr.left <= hr.left ? pr.right - hr.left : pr.left - hr.right)) * 100) / 100;
      result.token0 = window.getComputedStyle(document.documentElement)
        .getPropertyValue('--split-' + ID).trim();
      next();
    },
    function (next) { drag(60, next); },
    function (next) { result.paneWidth1 = widthOf(paneSel); next(); },
    function (next) { drag(-120, next); },
    function (next) { result.paneWidth2 = widthOf(paneSel); next(); },
    // 上下限各拖到边界之外。**方向必须乘 sign**：手柄往右移是让左侧的栏变宽、
    // 右侧的栏变窄——只朝一个方向拖，对右栏来说测到的一直是上限，下限永远碰不到。
    function (next) { drag(-4000 * sign, next); },
    function (next) { result.paneWidthMin = widthOf(paneSel); next(); },
    function (next) { drag(4000 * sign, next); },
    function (next) { result.paneWidthMax = widthOf(paneSel); next(); },
    // 手抖：位移 2px，小于 CLICK_SLOP（4），宽度不得变
    function (next) { drag(2, next); },
    function (next) {
      result.paneWidthSlop = widthOf(paneSel);
      result.tokenMax = window.getComputedStyle(document.documentElement)
        .getPropertyValue('--split-' + ID).trim();
      try { result.stored = window.localStorage.getItem('pentest.split.v1') || ''; result.storage = 'ok'; }
      catch (err) { result.stored = ''; result.storage = 'unavailable'; }
      next();
    }
  ];
  (function pump() {
    if (!steps.length) { document.title = %(marker)s + JSON.stringify(result); return; }
    var step = steps.shift();
    setTimeout(function () { step(pump); }, 400);
  })();
})();
</script>
"""

# 恢复档：在 <head> 里先播种一份偏好，再看打开时宽度是不是它。
# 播种必须插在 head —— 那里在 DOMContentLoaded 之前执行，正是 split.js 的
# applyAll 读存储的时刻。插到页尾就变成"先按默认值渲染、再跳一下"，
# 反而测不出真实表现。
RESTORE_PROBE = """
<script>
(function () {
  var ID = %(split)s, result = { split: ID, phase: 'restore' }, paneSel = %(pane)s;
  setTimeout(function () {
    var n = document.querySelector(paneSel);
    result.paneWidth = n ? Math.round(n.getBoundingClientRect().width) : -1;
    result.token = window.getComputedStyle(document.documentElement)
      .getPropertyValue('--split-' + ID).trim();
    document.title = %(marker)s + JSON.stringify(result);
  }, 400);
})();
</script>
"""


def dump_dom(html_path: str, wait_ms: int = 14000) -> str:
    """跑一次无头浏览器，返回序列化后的 DOM。"""
    browser = fixture._browser()
    if not browser:
        return ""
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
           "--no-default-browser-check", "--hide-scrollbars",
           "--window-size=1440,1000",
           "--virtual-time-budget=%d" % int(wait_ms),
           "--dump-dom",
           "file:///" + os.path.abspath(html_path).replace("\\", "/")]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    return proc.stdout or ""


def run(split_id: str, out_dir: str, seed_px=None) -> dict:
    """载入一次夹具页并跑探针。seed_px 非空时先播种一份宽度偏好。"""
    suffix = "restore" if seed_px else "drag"
    html_path = os.path.join(out_dir, "%s-%s.html" % (split_id, suffix))
    # 每条分栏各自出一份夹具：夹具按 --tab 决定哪个面板是激活的，
    # 而**非激活面板量出来的宽度是 0**（祖先 display:none），量不到。
    fixture.build(html_path, fixture.collect(), tab=split_id)
    with open(html_path, "r", encoding="utf-8") as handle:
        html = handle.read()

    subs = {"split": json.dumps(split_id), "pane": json.dumps(PANE_SELECTOR[split_id]),
            "marker": json.dumps(MARKER)}
    if seed_px:
        seed = ("<script>try{window.localStorage.setItem('pentest.split.v1', "
                "JSON.stringify({%s: %d}));}catch(e){}</script>\n"
                % (json.dumps(split_id), int(seed_px)))
        html = html.replace("<head>", "<head>\n" + seed, 1)
        html = html.replace("</body>", (RESTORE_PROBE % subs) + "\n</body>")
    else:
        html = html.replace("</body>", (DRAG_PROBE % subs) + "\n</body>")

    with open(html_path, "w", encoding="utf-8") as handle:
        handle.write(html)

    dom = dump_dom(html_path)
    found = re.search(re.escape(MARKER) + r"(\{.*?\})</title>", dom, re.S)
    if not found:
        return {"split": split_id, "phase": suffix,
                "error": "探针没有回报（页面没跑起来？）", "domBytes": len(dom)}
    return json.loads(found.group(1))


def main() -> int:
    parser = argparse.ArgumentParser(description="分栏拖拽探针（真浏览器）")
    parser.add_argument("out_dir", help="夹具 HTML 的落盘目录")
    parser.add_argument("--splits", default="chat,graph,assets")
    args = parser.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    config = split_config()
    if not config:
        print("从 web/split.js 里读不到 SPLITTERS —— 配置的写法变了？")
        return 2

    results, failed = [], []
    for split_id in [s.strip() for s in args.splits.split(",") if s.strip()]:
        if split_id not in config:
            failed.append("%s: split.js 里没有这一条" % split_id)
            continue
        item = run(split_id, args.out_dir)
        results.append(item)
        if item.get("error"):
            failed.append("%s: %s" % (split_id, item["error"]))
            continue
        if not item.get("found"):
            failed.append("%s: 页面上找不到拖拽条" % split_id)
            continue

        cfg = config[split_id]
        if item.get("display") == "none":
            failed.append("%s: 夹具视口 1440 不在窄屏档，手柄不该被隐藏" % split_id)
        if item.get("paneWidth0") != cfg["def"]:
            failed.append("%s: 初始宽度应为默认 %d，实得 %s"
                          % (split_id, cfg["def"], item.get("paneWidth0")))
        if item.get("handleWidth") != 1:
            failed.append("%s: 分栏线应当是 1px 的 hairline，实得 %s"
                          % (split_id, item.get("handleWidth")))
        if item.get("edgeDelta") != 0:
            failed.append("%s: 手柄与栏之间不该有缝或叠线，实测偏差 %s"
                          % (split_id, item.get("edgeDelta")))
        if item.get("ariaRole") != "separator" or item.get("tabindex") != "0":
            failed.append("%s: 手柄的无障碍属性不全" % split_id)

        # 方向由"栏在手柄左边还是右边"决定，与 split.js 的 signOf 同一个判据
        sign = 1 if item.get("paneLeftOfHandle") else -1
        want1 = item["paneWidth0"] + sign * 60
        if item.get("paneWidth1") != want1:
            failed.append("%s: 拖 +60 后应为 %d，实得 %s"
                          % (split_id, want1, item.get("paneWidth1")))
        want2 = max(cfg["min"], want1 + sign * -120)
        if item.get("paneWidth2") != want2:
            failed.append("%s: 再拖 -120 后应为 %d，实得 %s"
                          % (split_id, want2, item.get("paneWidth2")))
        if item.get("paneWidthMin") != cfg["min"]:
            failed.append("%s: 拖过下限后应停在下限 %d，实得 %s"
                          % (split_id, cfg["min"], item.get("paneWidthMin")))
        want_max = max(cfg["min"], round(item.get("boxWidth", 0) * cfg["ratio"]))
        # 容差 1px：JS 的 Math.round 是四舍五入、Python 的 round 是银行家舍入，
        # 而容器宽度本身是个小数。差 1px 不说明夹取错了。
        if abs(item.get("paneWidthMax", -1) - want_max) > 1:
            failed.append("%s: 拖过上限后应停在容器 %.0f × %.2f ≈ %d，实得 %s"
                          % (split_id, item.get("boxWidth", 0), cfg["ratio"],
                             want_max, item.get("paneWidthMax")))
        if item.get("paneWidthSlop") != item.get("paneWidthMax"):
            failed.append("%s: 位移 2px（小于 CLICK_SLOP）不该改变宽度" % split_id)
        if not item.get("stored"):
            failed.append("%s: 拖动之后没有写进存储（刷新就丢）" % split_id)

        # 恢复档：换一份偏好重新打开，宽度应当就是它
        seed = cfg["def"] + SEED_DELTA
        restored = run(split_id, args.out_dir, seed_px=seed)
        results.append(restored)
        if restored.get("error"):
            failed.append("%s(恢复): %s" % (split_id, restored["error"]))
        elif restored.get("paneWidth") != seed:
            failed.append("%s(恢复): 播种 %d，打开后是 %s"
                          % (split_id, seed, restored.get("paneWidth")))

    print(json.dumps(results, ensure_ascii=False, indent=2))
    if failed:
        print("\n失败：")
        for line in failed:
            print("  - " + line)
        return 1
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
