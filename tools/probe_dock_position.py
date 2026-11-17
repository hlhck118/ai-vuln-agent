# -*- coding: utf-8 -*-
"""坞的位置探针：点开「可用能力 / 技能手册」时，坞要**完整浮在输入卡片上沿之上**。

## 为什么需要它

这不是一条能靠源码字符串看住的性质。坞、记录区、输入卡片三家都在
`.chat-compose` 这棵子树里，卡片又用 `margin-top: calc(-1 * var(--chat-overlap))`
把自己叠进记录区，于是"谁画在谁上面"只由**真正算出来的矩形与层叠顺序**决定：

* 坞在文档流里时，卡片不透明的底会整块盖住它——看得见的只剩卡片上沿外一条缝；
* 而那一刻 DOM 完好、`data-dock="tools"`、宽高都正常，**任何属性断言都是绿的**。

t93 之前就是如此：真页面上坞 555.6..739.6、卡片 331.6..768，
`elementFromPoint` 在坞顶/中/底三点全部命中卡片里的 `textarea#chat-input`。
所以这探针量的是三件只有浏览器知道的事：

1. 坞的**三个采样点**各自落回坞里（不是落到卡片上）；
2. 坞底沿离卡片上沿正好一个 `--sp-3`（12px，浮层贴边）；
3. 打开坞**不移动卡片**（卡片是底锚定的，位置一变就说明坞又挤回文档流了）。

## 为什么先把卡片撑高

夹具的卡片只有一行高，撑不出"重叠"这个前提：坞高 min(184px,40vh)、
卡片矮于坞时两者本来就不撞。真页面上卡片常年 400px 以上（选项面板一开更高），
重叠才是常态。所以探针在量之前把 `.chat-field` 的最小高度顶到 420px——
这不是编造场景，而是把线上常态搬进夹具。

## 用法

    python tools/probe_dock_position.py [输出目录]

需要本机有 Edge 或 Chrome（无浏览器时打印 SKIP 并以 0 退出，不误报失败）。本工具
**不进单元测试套件**：`python -m unittest discover -s tests` 必须保持零外部依赖。
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

# 探针：只读几何、只发点击事件，不动应用代码。`elementFromPoint` 问的是浏览器
# 真正选中了谁——这是唯一能抓住"被盖住"的判据。
PROBE_HEAD = r"""
<style id="probe-tall">.chat-field { min-height: 420px; } .chat-compose { padding-top: 250px; }</style>
<script>
(function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  function box(el) {
    if (!el || !el.getClientRects().length) { return null; }
    var r = el.getBoundingClientRect();
    return { t: r.top, b: r.bottom, l: r.left, r: r.right,
             h: Math.round(r.height * 10) / 10, w: Math.round(r.width * 10) / 10 };
  }
  function id(el) {
    if (!el) { return '(null)'; }
    var name = el.id ? '#' + el.id : '';
    var cls = (el.className && typeof el.className === 'string')
      ? '.' + el.className.trim().split(/\s+/).join('.') : '';
    return (el.tagName || '?').toLowerCase() + name + cls;
  }
  function hit(x, y) {
    var el = document.elementFromPoint(x, y);
    return { at: x + ',' + y, el: id(el), insideDock: !!(el && el.closest && el.closest('#chat-quick')) };
  }
  function click(el) {
    if (!el) { return false; }
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
    return true;
  }
  function mode() {
    var q = document.getElementById('chat-quick');
    return q ? (q.getAttribute('data-dock') || '') : '';
  }
  async function run() {
    var out = {};
    try {
      out.cases = [];
      function record(name, ok, detail) {
        out.cases.push({ name: name, ok: !!ok, detail: detail });
      }
      var quick = document.getElementById('chat-quick');
      var field = document.querySelector('.chat-field');
      var log = document.getElementById('chat-log');
      if (!quick || !field) { throw new Error('夹具里找不到 #chat-quick / .chat-field'); }

      // 0. 收起：先记下卡片的基线矩形（后面要证明打开坞没有挪动它）。
      if (mode() !== 'none') { click(document.getElementById('chat-tools')); await sleep(300); }
      out.closed = { mode: mode(), field: box(field) };
      record('0 收起态', mode() === 'none', 'data-dock=' + mode());

      // 1. 打开目录坞。
      click(document.getElementById('chat-tools'));
      for (var i = 0; i < 60; i++) {
        if (mode() === 'tools' && quick.querySelector('.dock-chip')) { break; }
        await sleep(100);
      }
      await sleep(400);
      var qb = box(quick);
      out.open = { mode: mode(), quick: qb, field: box(field), log: box(log),
                   chips: quick.querySelectorAll('.dock-chip').length,
                   fieldClosed: out.closed.field };
      record('1 坞打开了且有卡片', mode() === 'tools' && out.open.chips > 0,
             'chips=' + out.open.chips);

      // 2. 坞的三个采样点必须落回坞里。顶 8px / 正中 / 底 8px。
      var hb = box(quick);
      out.hits = [];
      if (hb) {
        var mid = (hb.l + hb.r) / 2;
        out.hits = [hit(mid, hb.t + 8), hit(mid, Math.round((hb.t + hb.b) / 2)), hit(mid, hb.b - 8)];
      }
      record('2 坞顶/中/底三点都落在坞里',
             out.hits.length === 3 && out.hits.every(function (h) { return h.insideDock; }),
             out.hits.map(function (h) { return h.el; }).join(' | '));

      // 3. 坞底沿离卡片上沿一个 --sp-3（12px）。读真实计算值，不写死。
      var sp3 = 12;
      try {
        var raw = getComputedStyle(document.querySelector('.chat-main')).getPropertyValue('--sp-3');
        var n = parseFloat(raw);
        if (n > 0) { sp3 = n; }
      } catch (e) {}
      var gap = (hb && out.open.field) ? (out.open.field.t - hb.b) : null;
      out.gap = gap === null ? null : Math.round(gap * 10) / 10;
      record('3 坞底沿贴卡片上沿（差 ' + sp3 + 'px）',
             gap !== null && Math.abs(gap - sp3) <= 1, 'gap=' + out.gap + 'px');

      // 4. 打开坞不许移动卡片：卡片是底锚定的，挪了就说明坞又挤进文档流。
      var moved = (out.closed.field && out.open.field)
        ? Math.abs(out.closed.field.t - out.open.field.t) + Math.abs(out.closed.field.b - out.open.field.b)
        : null;
      out.moved = moved;
      record('4 打开坞时卡片不动', moved !== null && moved <= 1, '位移=' + moved + 'px');

      // 5. 坞与卡片同一列、左右对称。两种都算对：贴**外沿**（`left/right:
      //    calc(-1 * var(--sp-3))`，坞与卡片的外边框齐平）或贴**内沿**（`left/right: 0`，
      //    与卡片自己的内边距齐平）。钉死其中一种会在下一次调版式时误报。
      var flush = (hb && out.open.field)
        ? (Math.abs(hb.l - out.open.field.l) <= 1 && Math.abs(hb.r - out.open.field.r) <= 1)
        : false;
      var inset = (hb && out.open.field)
        ? (Math.abs(hb.l - (out.open.field.l + sp3)) <= 1
           && Math.abs(hb.r - (out.open.field.r - sp3)) <= 1)
        : false;
      out.width = (hb && out.open.field)
        ? { dock: hb.w, field: out.open.field.w, mode: flush ? '外沿' : (inset ? '内沿' : '不齐') }
        : null;
      record('5 坞与卡片同列且左右对称（贴外沿或贴内沿）', flush || inset,
             out.width ? 'dock=' + out.width.dock + ' 卡片=' + out.width.field + ' 对齐=' + out.width.mode : '');

      // 6. 记录区末尾要给坞让位：对话文字不能钻到坞底下。
      var pad = log ? parseFloat(getComputedStyle(log).paddingBottom) : NaN;
      var fade = 12;
      try {
        var fraw = getComputedStyle(document.querySelector(".chat-main"))
          .getPropertyValue("--chat-fade");
        var fn = parseFloat(fraw); if (fn > 0) { fade = fn; }
      } catch (e) {}
      // 与 web/app.js 的 syncChatTail() 同一算式：叠进深度夹 0 后再加渐隐。
      var want = (out.open.log && hb) ? (Math.max(0, out.open.log.b - hb.t) + fade) : null;
      out.pad = isNaN(pad) ? null : pad;
      out.padWant = want === null ? null : Math.round(want * 10) / 10;
      record('6 记录区末尾让到坞上方', want !== null && !isNaN(pad) && Math.abs(pad - want) <= 2,
             'padding-bottom=' + out.pad + ' 应≈' + out.padWant);

      out.verdict = out.cases.every(function (c) { return c.ok; }) ? 'PASS' : 'FAIL';
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
          'data-dock-geom',
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
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
           "--window-size=1452,1200",
           "--user-data-dir=" + profile,
           "--virtual-time-budget=30000", "--dump-dom",
           "file:///" + path.replace("\\", "/")]
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)
    return done.stdout or ""


def _r(value, digits=1):
    if value is None:
        return "(空)"
    return ("%." + str(digits) + "f") % value


def main() -> int:
    browser = _browser()
    if not browser:
        print("SKIP：本机没找到 Edge / Chrome，跳过坞位置探针。")
        print("      （这不是失败：探针要真实浏览器，而单元测试套件保持零外部依赖。）")
        return 0

    out_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="dock-geom-")
    # 相对路径会让 `file:///` 少掉盘符，浏览器静默交出空 DOM——先落成绝对路径。
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    fixture = os.path.join(out_dir, "chat-dock-geom.html")
    data = render_ui_fixture.collect()
    render_ui_fixture.build(fixture, data, tab="chat", dock="tools")
    with open(fixture, "r", encoding="utf-8") as handle:
        html = handle.read()
    probed = os.path.join(out_dir, "chat-dock-geom-probe.html")
    with open(probed, "w", encoding="utf-8") as handle:
        handle.write(html.replace("</body>", PROBE_HEAD + "</body>", 1))

    dom = _dump(browser, probed, os.path.join(out_dir, "browser-profile"))
    match = re.search(r'data-dock-geom="([^"]+)"', dom)
    if not match:
        errors = re.search(r'data-fixture-errors="([^"]*)"', dom)
        print("FAIL：页面没跑到探针。夹具错误：%s" % (errors.group(1) if errors else "(无)"))
        return 2
    payload = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
    print("夹具：%s" % fixture)
    closed = payload.get("closed") or {}
    opened = payload.get("open") or {}
    cb = closed.get("field") or {}
    ob = opened.get("field") or {}
    qb = opened.get("quick") or {}
    print("卡片（收起）：%s..%s  高 %s" % (_r(cb.get("t")), _r(cb.get("b")), _r(cb.get("h"))))
    print("卡片（展开）：%s..%s  高 %s" % (_r(ob.get("t")), _r(ob.get("b")), _r(ob.get("h"))))
    print("坞（展开）：%s..%s  宽 %s" % (_r(qb.get("t")), _r(qb.get("b")), _r(qb.get("w"))))
    print("坞底沿到卡片上沿：%s px ｜ 记录区 padding-bottom：%s px（应≈%s）"
          % (_r(payload.get("gap")), _r(payload.get("pad")), _r(payload.get("padWant"))))
    print("%-6s %-34s %s" % ("用例", "结果", "细节"))
    for item in payload.get("cases") or []:
        print("  %-4s %-32s %s" % ("OK" if item.get("ok") else "不符",
                                   item.get("name"), item.get("detail")))
    verdict = payload.get("verdict")
    if verdict != "PASS":
        print("")
        print("判据：坞必须是**浮层**——绝对定位贴住卡片上沿、盖在记录区之上，")
        print("      且不得把卡片挤动。详见 web/style.css 里 .chat-quick 的注释。")
        if payload.get("error"):
            print("探针异常：%s" % payload["error"])
    print("")
    print("结论：%s" % verdict)
    print("产物：%s" % probed)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
