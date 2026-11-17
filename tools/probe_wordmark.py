# -*- coding: utf-8 -*-
"""字标度量探针 —— 在真浏览器里量出两行字标的**几何**，而不是靠看。

**为什么需要它。** "英文和中文对齐"这句话没有数字就没有判据：两行字看起来歪，
可能是因为左边缘差了 3px，可能是行末那个字距空档没算进去，也可能是两行的**宽度**
差着一大截。三种原因的修法不同，而截图**看不出来是哪一种** —— 差 3px 和差 8px 在
18px 的字上看都像"有点歪"。

它量的是**每个字符的矩形**（`Range.getClientRects()`），不是元素的盒子：

* `.brand-cn` 是 `display: block`，盒子宽度**等于容器宽度**，与文字多长无关 ——
  按盒子量会得到"两行一样宽"这种**恒真**的结论，而真正的错就藏在那句恒真里；
* 行末的 `letter-spacing` 会让盒子比最后一个字形宽一格。

所以这里逐字符取矩形：首字符的左边缘就是**画出来的左边缘**，末字符的右边缘减掉
一个行末字距才是**画出来的右边缘**。

它还回答一个问题：**字距调到多少，两行才会一样宽。**

    视觉宽度 V = N + (n - 1) · L        N = 自然宽度（字距为 0），n = 字符数

`letter-spacing` 置 0 量一遍拿到 N 与 n，再按实测的 L 反推 `V_en == V_cn` 的解，
px 与 em 两种都给（em 那份可以直接抄进 CSS，它随字号缩放）。

**只管几何，不管颜色。** 渐变 / 描边 / 光晕是主题相关的，几何不是 —— 换主题不改
字号、不改对齐。所以默认只量一个主题。

用法：
  python tools/probe_wordmark.py                 # 左侧栏那处（18px）
  python tools/probe_wordmark.py --anon          # 登录门那处（24px）
  python tools/probe_wordmark.py --width 820     # 窄窗（这一档只留中文）
  python tools/probe_wordmark.py --json          # 只打 JSON（给测试用）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB = os.path.join(ROOT, "web")
sys.path.insert(0, HERE)

from probe_boot_chain import (STUB, SUFFIX_STUB, _attr,  # noqa: E402
                              _browser, _unescape)

# 未登录时的载荷：`authenticated: false` + `needsSetup: false` → auth.js 显示**登录**
# 那一屏（不是"创建管理员"），字标就挂在它上面。用探针的默认载荷是看不到登录门的
# ——那一份是 `authenticated: true`，`#auth-gate` 整个 hidden。
ANON_STUB = dict(STUB)
ANON_STUB["/api/v1/auth/state"] = dict(STUB["/api/v1/auth/state"],
                                      authenticated=False, csrfToken="")
ANON_STUB["/api/v1/auth/me"] = {"authenticated": False, "csrfToken": ""}

# 候选字面：报告里逐个报"装没装"，用来回答"这台机器上命中的到底是哪一个黑体"。
CANDIDATES = ["Source Han Sans SC", "Noto Sans SC", "Microsoft YaHei UI",
              "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
              "Noto Sans CJK SC", "SimHei"]

MEASURE_SCRIPT = r"""<script>
var __CANDIDATES__ = __CANDIDATE_LIST__;
// fetch 桩：与 `probe_boot_chain.py` 同一个形状。**少了这一段，两次运行（登录 /
// 未登录）会量到同一块** —— 所有接口都打空，`auth.js` 停在登录门上，
// 于是 `--anon` 与默认载荷的差别在报告里完全看不出来。
var __STUB__ = __STUB_JSON__;
var __SUFFIX__ = __SUFFIX_JSON__;
function __payload__(path) {
  if (Object.prototype.hasOwnProperty.call(__STUB__, path)) return __STUB__[path];
  var keys = Object.keys(__SUFFIX__);
  for (var i = 0; i < keys.length; i++) {
    if (path.slice(-keys[i].length) === keys[i]) return __SUFFIX__[keys[i]];
  }
  return {};
}
window.fetch = function (input, init) {
  var url = String(input), at = url.indexOf('/api/v1');
  var raw = (at >= 0 ? url.slice(at) : url), path = raw.split('?')[0];
  return Promise.resolve(new Response(JSON.stringify(__payload__(path)), {
    status: 200, headers: { 'Content-Type': 'application/json' } }));
};
function __charRect__(node, i) {
  var r = document.createRange();
  r.setStart(node, i); r.setEnd(node, i + 1);
  var rects = r.getClientRects();
  return rects.length ? rects[0] : null;
}
function __bounds__(text) {
  var a = 0, b = text.length - 1;
  while (a < text.length && /\s/.test(text[a])) a++;
  while (b >= 0 && /\s/.test(text[b])) b--;
  return [a, b];
}
// `forceSpacing` 传 0 量"自然宽度"，传 null 量"现在"。同一个函数量两遍 ——
// 两条代码路径量出来的差会被误读成字距的功。
function __measureLine__(el, forceSpacing) {
  if (!el) return { error: '节点不在' };
  var node = el.firstChild;
  if (!node || node.nodeType !== 3) return { error: '不是纯文本节点' };
  var cs = getComputedStyle(el);
  var before = el.style.letterSpacing;
  var L = (forceSpacing === null) ? (parseFloat(cs.letterSpacing) || 0)
                                  : forceSpacing;
  if (forceSpacing !== null) el.style.letterSpacing = forceSpacing + 'px';
  var bound = __bounds__(node.nodeValue);
  var first = __charRect__(node, bound[0]), last = __charRect__(node, bound[1]);
  var out = null;
  if (first && last) {
    var n = bound[1] - bound[0] + 1;
    var right = last.right - L;         // 减掉行末那一个空档
    out = {
      text: node.nodeValue.slice(bound[0], bound[1] + 1),
      chars: n,
      left: +first.left.toFixed(2),
      right: +right.toFixed(2),
      visual: +(right - first.left).toFixed(2),      // 画出来的宽度
      box: +(right - first.left + n * L).toFixed(2),  // 含行末空档
      top: +first.top.toFixed(2),
      bottom: +last.bottom.toFixed(2),
      fontSize: parseFloat(cs.fontSize),
      letterSpacing: +L.toFixed(3),
      fontWeight: cs.fontWeight,
      fontFamily: cs.fontFamily
    };
  }
  if (forceSpacing !== null) el.style.letterSpacing = before;
  return out || { error: '取不到字符矩形' };
}
function __probe__() {
  var report = { spots: {}, fonts: {} };
  report.viewport = { width: window.innerWidth, height: window.innerHeight,
                      dpr: window.devicePixelRatio || 1 };
  __CANDIDATES__.forEach(function (name) {
    try { report.fonts[name] = document.fonts.check('700 24px "' + name + '"'); }
    catch (e) { report.fonts[name] = null; }
  });
  [['auth', '.auth-brand .brand-lockup'], ['rail', '.rail-brand .brand-lockup']]
    .forEach(function (pair) {
      var lockup = document.querySelector(pair[1]);
      if (!lockup || !lockup.offsetParent) {
        report.spots[pair[0]] = { present: false };
        return;
      }
      var cnEl = lockup.querySelector('.brand-cn');
      var enEl = lockup.querySelector('.brand-en');
      // 隐藏的（display:none）节点量出来全是 0。分开报，否则"窄窗下英文被藏起来"
      // 会被读成"英文宽度是 0 —— 差得很远"。
      var hidden = !enEl || getComputedStyle(enEl).display === 'none';
      var spot = {
        present: true, englishHidden: hidden,
        size: parseFloat(getComputedStyle(cnEl).fontSize),
        lockupWidth: +lockup.getBoundingClientRect().width.toFixed(2),
        align: getComputedStyle(lockup).alignItems,
        textAlign: getComputedStyle(lockup).textAlign,
        cnNatural: __measureLine__(cnEl, 0),
        cn: __measureLine__(cnEl, null)
      };
      var cnNow = spot.cn, cnNatural = spot.cnNatural;
      if (!hidden) {
        spot.enNatural = __measureLine__(enEl, 0);
        spot.en = __measureLine__(enEl, null);
      }
      if (!hidden && !spot.en.error && !cnNow.error) {
        var enNow = spot.en, enNatural = spot.enNatural;
        spot.leftDelta = +(enNow.left - cnNow.left).toFixed(2);
        spot.rightDelta = +(enNow.right - cnNow.right).toFixed(2);
        spot.widthDelta = +(enNow.visual - cnNow.visual).toFixed(2);
        if (enNatural && !enNatural.error && enNatural.chars > 1) {
          var want = cnNow.visual;
          var L = (want - enNatural.visual) / (enNatural.chars - 1);
          spot.neededEnSpacing = { px: +L.toFixed(3),
                                   em: +(L / enNow.fontSize).toFixed(4),
                                   targetVisual: +want.toFixed(2) };
        }
      }
      report.spots[pair[0]] = spot;
    });
  document.documentElement.setAttribute('data-probe-wordmark',
                                        JSON.stringify(report));
}
document.addEventListener('DOMContentLoaded', function () {
  var run = function () {
    try { __probe__(); } catch (e) {
      document.documentElement.setAttribute(
        'data-probe-wordmark', JSON.stringify({ fatal: String(e && e.stack || e) }));
    }
  };
  // **反复量，取最后一次。** 量一次是不行的，而且失败得很安静：`auth.js` 要先拿
  // `/auth/state` 才能决定显示登录门还是控制台，那是一个异步往返。量早了，两次
  // 不同的运行（登录 / 未登录）会量到**同一块**，于是"rail 不在这一屏上"这种结论
  // 看起来完全正常 —— 而那正是入口那一刻的真实状态，不是我要量的事。
  // 每次覆盖同一个属性，`--dump-dom` 拿到的是最后一次（也就是稳定之后那一次）。
  var tick = function () { run(); };
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(function () { tick(); setInterval(tick, 150); });
  } else { tick(); setInterval(tick, 150); }
});
</script>
"""


def _web_url() -> str:
    return "file:///" + WEB.replace("\\", "/")


def build_harness(anon: bool) -> str:
    with open(os.path.join(WEB, "index.html"), "r", encoding="utf-8") as handle:
        html = handle.read()
    stub = ANON_STUB if anon else STUB
    script = (MEASURE_SCRIPT
              .replace("__CANDIDATE_LIST__",
                       json.dumps(CANDIDATES, ensure_ascii=False))
              .replace("__STUB_JSON__", json.dumps(stub, ensure_ascii=False))
              .replace("__SUFFIX_JSON__", json.dumps(SUFFIX_STUB,
                                                     ensure_ascii=False)))
    # `<base>` 让真实的 `<script src="app.js">` 解析到 web/ 下；fetch 桩必须**先于**
    # 应用脚本装好，所以这段脚本排在第一个 `<script>` 前面。
    html = html.replace('<meta charset="utf-8">',
                        '<meta charset="utf-8">\n<base href="%s/">' % _web_url(), 1)
    return html.replace('<script>', script + '<script>', 1)


def dump_with_size(browser: str, page: str, budget_ms: int, width: int,
                   height: int) -> str:
    """与 `probe_boot_chain.dump_dom` 同一套，只多一个窗口尺寸。

    **尺寸必须显式给。** 无头窗口默认 800×600，而 800 正落在"横排、只留中文"那条
    媒体查询里 —— 不问一句就量，量到的是**另一档布局**，而它照样打出一份看起来
    完全正常的报告。
    """
    profile = tempfile.mkdtemp(prefix="wordmark-profile-")
    handle, out = tempfile.mkstemp(prefix="wordmark-dom-", suffix=".html")
    os.close(handle)
    try:
        args = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
                "--window-size=%d,%d" % (width, height),
                "--user-data-dir=" + profile,
                "--virtual-time-budget=%d" % int(budget_ms), "--dump-dom", page]
        with open(out, "w", encoding="utf-8", errors="replace") as sink:
            subprocess.run(args, stdout=sink, stderr=subprocess.DEVNULL,
                           timeout=180, check=False)
        with open(out, "r", encoding="utf-8", errors="replace") as source:
            return source.read()
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        try:
            os.remove(out)
        except OSError:
            pass


def probe(anon: bool = False, width: int = 1440, height: int = 1000,
          budget_ms: int = 12000, keep: str = "") -> dict:
    browser = _browser()
    if not browser:
        return {"browser": "", "skipped": True}
    work = tempfile.mkdtemp(prefix="wordmark-") if not keep else keep
    page = os.path.join(work, "wordmark.html")
    with open(page, "w", encoding="utf-8") as handle:
        handle.write(build_harness(anon))
    dom = dump_with_size(browser, "file:///" + page.replace("\\", "/"),
                         budget_ms, width, height)
    raw = _unescape(_attr(dom, "data-probe-wordmark"))
    try:
        report = json.loads(raw) if raw else {}
    except ValueError:
        report = {"parse_error": raw[:300]}
    report["browser"] = browser
    report["mode"] = "auth" if anon else "rail"
    if keep:
        report["page"] = page
    return report


def _line(label, value):
    if not value or value.get("error"):
        print("    %-6s %s" % (label, (value or {}).get("error", "（没有）")))
        return
    print("    %-6s 左 %8.2f  右 %8.2f  宽 %7.2f  %4.1fpx  字距 %6.3f  "
          "字重 %-3s  %d 字符"
          % (label, value["left"], value["right"], value["visual"],
             value["fontSize"], value["letterSpacing"], value["fontWeight"],
             value["chars"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="在真浏览器里量字标两行的几何")
    parser.add_argument("--anon", action="store_true",
                        help="未登录载荷（量登录门那处，24px）")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--budget", type=int, default=12000)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--keep", default="")
    args = parser.parse_args()
    report = probe(args.anon, args.width, args.height, args.budget, args.keep)
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
        return 0
    if report.get("skipped"):
        print("SKIP：本机没找到 Edge / Chrome。")
        return 0
    if report.get("fatal") or report.get("parse_error"):
        print("探针自己坏了：%s"
              % (report.get("fatal") or report.get("parse_error")))
        return 1
    print("模式 %s    视口 %s×%s" % (report["mode"],
                                    report.get("viewport", {}).get("width"),
                                    report.get("viewport", {}).get("height")))
    installed = [n for n, ok in (report.get("fonts") or {}).items() if ok]
    print("装了的字面：%s" % ("、".join(installed) or "（一个都没认出）"))
    for name in ("auth", "rail"):
        spot = (report.get("spots") or {}).get(name)
        if not spot or not spot.get("present"):
            print("\n[%s] 不在这一屏上" % name)
            continue
        print("\n[%s]  字标宽 %.2f  align %s  text-align %s  英文%s"
              % (name, spot["lockupWidth"], spot["align"], spot["textAlign"],
                 "已隐藏" if spot["englishHidden"] else "在"))
        _line("自然", spot.get("cnNatural"))
        _line("中文", spot.get("cn"))
        if spot.get("englishHidden"):
            print("    （这一档只留中文，没有第二行可比）")
            continue
        _line("自然", spot.get("enNatural"))
        _line("英文", spot.get("en"))
        print("    左边缘差 %+.2fpx   右边缘差 %+.2fpx   宽度差 %+.2fpx"
              % (spot["leftDelta"], spot["rightDelta"], spot["widthDelta"]))
        need = spot.get("neededEnSpacing")
        if need:
            print("    要两行一样宽：英文 letter-spacing = %.3fpx = %.4fem"
                  "（目标 %.2fpx）" % (need["px"], need["em"],
                                       need["targetVisual"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
