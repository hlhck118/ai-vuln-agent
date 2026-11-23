# -*- coding: utf-8 -*-
"""点选 vs 拖拽的回归探针：图里点不同节点，「节点详情」必须跟着变。

## 为什么需要它

这个缺陷（鼠标抖 1 像素就被判成拖拽，抬手点选被吞掉，症状是"切换节点内容不变"）
**单元测试抓不到**：判定逻辑藏在 `pointerdown` / `pointermove` / `pointerup` 三个
浏览器事件处理函数里，要跑出真实行为就得有真实指针事件序列和真实布局——而
`toUserSpace` 依赖 `getBoundingClientRect()`，在无头 DOM 里宽度恒为 0，判定的
分子分母一起消失，怎么写都是"通过"。

所以这里走**夹具 + 无头浏览器**的路：生成一份内联了真实接口响应的自包含 HTML，
注入一段探针脚本用真实 `PointerEvent` 走一遍点选，把结果写进 `<html>` 属性，
再用 `--dump-dom` 读回来。检查的量是"选中态有没有换到另一个节点"，不是"没抛异常"。

## 判定标准

| 手势 | 期望 | 为什么 |
|---|---|---|
| 完全静止的点选 | 换 | 基线：不能连这个都坏 |
| 抖 1 / 2 / 3 像素 | 换 | **真实鼠标按下到抬起一定在抖**，这就是本用例的主体 |
| 拖 30 像素 | **不换** | 拖拽平移必须保持可用；改宽阈值改到这里就说明修过头了 |

## 用法

    python tools/probe_graph_click.py [输出目录]

需要本机有 Edge 或 Chrome（无浏览器时打印 SKIP 并以 0 退出，不误报失败）。
本工具**不进单元测试套件**：`python -m unittest discover -s tests` 必须保持零外部
依赖，而这里要起浏览器。它属于 `tools/` 下的验收工具，和 `render_layout_png.py`
同一类。
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
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import render_ui_fixture                                        # noqa: E402

from _browser import BROWSERS    # noqa: E402

# 探针：不改应用代码，只把 svg.setPointerCapture 换成空函数——合成 PointerEvent
# 没有活跃指针，真实调用会抛 NotFoundError，那是测试手段的限制，不是被测行为。
PROBE = r"""
<script>
(function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var detail = function () {
    var n = document.getElementById('node-detail-body');
    return (n ? n.textContent : '').replace(/\s+/g, ' ').trim();
  };
  // 画布上没有"某张卡"这个元素：选中态读 state，位置由 PentestUI.graphNodePoints()
  // 给（换算与 toUserSpace 互逆，只此一份）。
  var selected = function () {
    var ui = window.PentestUI;
    return (ui && ui.state && ui.state.selectedNodeId) || '';
  };
  var points = function () {
    var ui = window.PentestUI;
    return (ui && ui.graphNodePoints && ui.graphNodePoints()) || [];
  };

  function gesture(surface, point, dx) {
    var base = { bubbles: true, cancelable: true, button: 0, pointerId: 1,
                 pointerType: 'mouse', clientX: point.x, clientY: point.y };
    var moved = {}; Object.keys(base).forEach(function (k) { moved[k] = base[k]; });
    moved.clientX = point.x + dx;
    // 合成 PointerEvent 没有活跃指针，真实捕获会抛 NotFoundError——那是手段的限制。
    surface.dispatchEvent(new PointerEvent('pointerdown', base));
    if (dx) surface.dispatchEvent(new PointerEvent('pointermove', moved));
    surface.dispatchEvent(new PointerEvent('pointerup', moved));
  }

  async function run() {
    var out = {};
    try {
      var svg = document.getElementById('graph-surface');   // 绘制面是 canvas
      // 夹具里会有多个 engagement（每次下发任务建一个），而下拉框默认停在
      // `list[0]`——它未必是最有内容的那个：夹具跑的三次任务里有一次没执行过，
      // 那个 engagement 只有一个 goal 节点。所以先扫一遍，挑节点最多的那个。
      // 顶栏那个 `#engagement-select` 已经撤掉（选择搬进了「会话」侧边栏），现在
      // 取「最多节点」的方式是切到**全部目标**：那正是合并视图，节点最全。
      // 原来那段扫下拉框的代码留着只会抛 `Cannot read properties of null`，
      // 于是探针永远判 PROBE_THREW——一条永远红的探针等于没有探针。
      var scopeAll = document.getElementById('graph-scope-all');
      if (scopeAll) { scopeAll.click(); await sleep(600); }
      out.scope = scopeAll ? 'all' : 'single';
      var list = points();                              // [{id, x, y}]，可见卡的中心
      out.engagements = list.length;
      var ids = list.map(function (p) { return p.id; });
      out.nodeCount = ids.length;
      if (ids.length < 2) { out.verdict = 'NOT_ENOUGH_NODES'; return out; }

      var vbW = Number(svg.dataset.width || 0);
      var rect = svg.getBoundingClientRect();
      out.viewBox = { w: vbW, rectW: Math.round(rect.width) };
      out.userUnitsPerScreenPx = rect.width ? Number((vbW / rect.width).toFixed(2)) : null;
      out.slopPx = 4;

      var origCapture = svg.setPointerCapture;
      svg.setPointerCapture = function () {};
      var cases = [];
      [[0, true], [1, true], [2, true], [3, true], [30, false]].forEach(function (pair) {
        var dx = pair[0], expectSwap = pair[1];
        gesture(svg, list[0], 0);             // 先把选中态停在第一个节点
        var before = detail(), beforeSel = selected();
        gesture(svg, list[1], dx);
        var after = detail(), afterSel = selected();
        cases.push({
          jitterPx: dx,
          expectSwap: expectSwap,
          swapped: before !== after,
          selectionMoved: beforeSel !== afterSel,
          ok: (before !== after) === expectSwap
        });
      });
      svg.setPointerCapture = origCapture;
      out.cases = cases;
      out.verdict = cases.every(function (c) { return c.ok; }) ? 'PASS' : 'FAIL';
    } catch (e) {
      out.verdict = 'PROBE_THREW';
      out.error = String((e && e.stack) || e);
    }
    return out;
  }

  window.addEventListener('load', function () {
    setTimeout(function () {
      run().then(function (out) {
        document.documentElement.setAttribute(
          'data-click-probe',
          btoa(unescape(encodeURIComponent(JSON.stringify(out)))));
      });
    }, 900);
  });
})();
</script>
"""


def _browser():
    for path in BROWSERS:
        if path and os.path.exists(path):
            return path
    return ""


def _dump(browser, path, profile):
    """把页面跑成 DOM 文本。profile 用临时目录，且**失败要重试一次**。

    这台机器上同时挂着几十个 msedge 进程时，新起的无头实例偶尔会直接以 0 退出、
    一个字节都不吐（不是崩溃，也不是超时）。探针那次 `--dump-dom` 因此拿到空串，
    于是报「页面没跑到探针」——而同一份文件手工再 dump 一次就正常。所以这里不把
    空输出当成结论：换一个全新的 profile 再跑一遍，两次都空才认。
    """
    # 必须是绝对路径：headless 的 file:// 不认相对路径（会 ERR_FILE_NOT_FOUND），
    # 而 out_dir 来自 argv，可能是相对 cwd 的。
    url = "file:///" + os.path.abspath(path).replace("\\", "/")
    err = ""
    for attempt in range(2):
        cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
               "--user-data-dir=" + (profile if attempt == 0 else profile + "-retry"),
               "--virtual-time-budget=20000", "--dump-dom", url]
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300)
        if done.stdout:
            return done.stdout
        err = (done.stderr or "").strip()[-400:]
    if err:
        print("（两次 dump 都是空的；浏览器 stderr 末尾：%s）" % err)
    return ""


def main() -> int:
    browser = _browser()
    if not browser:
        print("SKIP：本机没找到 Edge / Chrome，跳过图点选探针。")
        print("      （这不是失败：探针要真实浏览器，而单元测试套件保持零外部依赖。）")
        return 0

    out_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="click-probe-")
    os.makedirs(out_dir, exist_ok=True)
    fixture = os.path.join(out_dir, "graph.html")
    data = render_ui_fixture.collect()
    render_ui_fixture.build(fixture, data, tab="graph")
    with open(fixture, "r", encoding="utf-8") as handle:
        html = handle.read()
    probed = os.path.join(out_dir, "graph-probe.html")
    with open(probed, "w", encoding="utf-8") as handle:
        handle.write(html.replace("</body>", PROBE + "</body>", 1))

    dom = _dump(browser, probed, os.path.join(out_dir, "browser-profile"))
    match = re.search(r'data-click-probe="([^"]+)"', dom)
    if not match:
        errors = re.search(r'data-fixture-errors="([^"]*)"', dom)
        print("FAIL：页面没跑到探针。夹具错误：%s" % (errors.group(1) if errors else "(无)"))
        return 2
    payload = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
    print("夹具：%s" % fixture)
    print("engagement %s 个（取节点最多的 %s）｜ 图内节点 %s 个 ｜ viewBox 宽 %s ｜ "
          "显示宽 %s px ｜ 1 屏幕像素 = %s 用户单位"
          % (payload.get("engagements"), payload.get("richestOption"),
             payload.get("nodeCount"), (payload.get("viewBox") or {}).get("w"),
             (payload.get("viewBox") or {}).get("rectW"),
             payload.get("userUnitsPerScreenPx")))
    for item in payload.get("cases") or []:
        print("  %-8s 期望%s换 ｜ 实际%s换 ｜ 选中态%s ｜ %s"
              % ("拖 %dpx" % item["jitterPx"] if item["jitterPx"] else "静止点选",
                 "  " if item["expectSwap"] else "不", "  " if item["swapped"] else "不",
                 "已变" if item["selectionMoved"] else "未变",
                 "OK" if item["ok"] else "不符"))
    print("结论：%s" % payload.get("verdict"))
    if payload.get("error"):
        print(payload["error"])
    return 0 if payload.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
