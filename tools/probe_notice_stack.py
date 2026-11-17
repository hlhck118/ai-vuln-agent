# -*- coding: utf-8 -*-
"""通知浮层的落点探针：错误条与截断提示**必须贴在输入卡片上沿的正上方**。

## 为什么需要它

这一次改动全是「位置」，而源码文本对位置几乎什么也证明不了：

* `position: fixed` 写对了，也可能落在屏幕外面（`--notice-bottom` 算错一行就出界）；
* 锚点公式写对了，也可能量错了被参照的那个盒子（卡片的上沿取决于输入框行数、
  目录是否展开、有没有选项行——写死一个 px 就会随内容漂移）；
* 「浮在输入卡片上方」这句话在卡片变高时还有第二种解释：卡片长高、上沿上移，
  浮层得跟着新上沿走，不能停在旧高度上。盖住卡片也算"在卡片上方"，但用起来
  就是错的那种。

`tests/test_notice_stack.py` 守的是结构（谁在谁里面、变量接没接上）；这里量的是
**真实浏览器里的矩形**。两者互补：前者挡住"改回去"，后者挡住"看起来对但贴错地方"。

第 6 条是这条探针存在的最硬理由：`.chat-auto-note` 是 `.chat-field` 的**前面一格兄弟**
（t94 之后中间还隔着排队块与选项面板），盒子顺序一直是对的（源码里它就在卡片前面，
静态断言永远绿），可卡片用负外边距把自己抬进了它的盒子——**整块盖住、连暖色底都
看不见**。这种错只有矩形和命中测试能发现。

## 判定标准

| # | 状态 | 期望 |
|---|---|---|
| 0 | 初态（没有通知） | 浮层是 `fixed`、已贴在卡片上方、没有可见子节点 |
| 1 | `showError()` | 错误条出现，且**底边贴住输入卡片上沿**（0px，不叠、不悬空） |
| 2 | 同上 | 浮层在视口里水平居中、宽度不超过 560px |
| 3 | 截断提示也显示 | 两条在同一个浮层里，都看得见 |
| 4 | 同上 | 两条上下排列，间隙等于 flex gap（8px 左右），不叠在一起 |
| 5 | 把卡片撑高 120px（t94 之后坞不再长在卡片里，所以自己撑） | 浮层跟着新上沿走，不留旧高度 |
| 6 | 自动继续那一行（`#chat-auto-note`）出现 | 它落在卡片**上方**（16px 的缝：`--chat-overlap` + 4 + compose 的 12），且 `elementFromPoint()` 命中的是它自己而不是 `chat-input` |
| 6b | 排队块也在（`#chat-queue` 可见） | 它只让**一格**：行到排队 8–24px、排队到卡片 8–24px——不能两边都让 146px（中间会留一个大洞，`.chat-compose` 跟着长高） |
| 7 | 换到没有输入卡片的页签 | 退回页面顶部，且**仍然看得见**（全局通知不能因为换页签消失） |

## 用法

    python tools/probe_notice_stack.py [输出目录]

需要本机有 Edge 或 Chrome（没有浏览器时打印 SKIP 并以 0 退出，不误报失败）。本工具
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

# 探针只读矩形、只调界面自己的入口（`PentestUI.showError`），不改应用代码。
# 两条通知都走它们真正的显示路径（`classList` / `showError`），而不是直接摆位置——
# 那样量到的就不是"通知出现时"的形态了。
PROBE = r"""
<script>
(function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  function byId(id) { return document.getElementById(id); }
  function sel(s) { return document.querySelector(s); }
  function box(node) {
    var r = node.getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, left: r.left, width: r.width, height: r.height };
  }
  function visible(node) { return !!(node && node.getClientRects().length > 0); }
  function stackEl() { return byId('notice-stack'); }
  /* 「卡片上沿」= `.chat-field` 的上沿，与 `syncNoticeAnchor()` **同一个边缘**。
     不能用 `.chat-compose`：卡片是叠进记录区的（`margin-top: calc(-1 *
     var(--chat-overlap))`），compose 的盒子比画出来的圆角卡片低一整个
     `--chat-overlap`（实测 740 对 626）；更要紧的是 compose 里还装着卡片上方会
     临时长出来的几行（排队中的消息、自动继续那一行），一按"最高可见边缘"取，
     量到的就成了那几行的顶边——排队 1 条时 compose.top 348 而卡片 626，差 278px。
     探针量错边缘的症状很隐蔽：应用是对的，报告却是红的（或反过来，两边一起歪，
     看起来一直绿）。卡片不可见时退回 compose，兜底态总得有个参照。 */
  function edgeTop() {
    var card = sel('.chat-field'), compose = sel('.chat-compose');
    var n = (card && card.getClientRects().length) ? card : compose;
    return n && n.getClientRects().length ? n.getBoundingClientRect().top : null;
  }
  function snap() {
    var S = stackEl(), C = sel('.chat-compose');
    if (!S) return { missing: true };
    var cs = getComputedStyle(S);
    var shown = [].slice.call(S.children)
      .filter(function (n) { return !n.classList.contains('hidden'); })
      .map(function (n) { return n.id; });
    return {
      anchor: S.getAttribute('data-anchor'),
      position: cs.position,
      zIndex: cs.zIndex,
      stack: box(S),
      compose: C ? box(C) : null,
      gap: box(S).bottom !== null && edgeTop() !== null ? Math.round(edgeTop() - box(S).bottom) : null,
      bottomVar: (cs.getPropertyValue('--notice-bottom') || '').trim(),
      shown: shown,
      vw: window.innerWidth,
      vh: window.innerHeight
    };
  }

  async function run() {
    var out = { cases: [], notes: [] };
    var add = function (name, ok, extra) {
      var item = { name: name, ok: !!ok };
      if (extra) { for (var k in extra) { item[k] = extra[k]; } }
      out.cases.push(item);
      return item;
    };
    try {
      // 启动链要先把状态拉回来、把卡片建出来（`init()` 里才挂锚点）。
      for (var i = 0; i < 200; i++) {
        if (sel('.chat-compose') && stackEl() && stackEl().getAttribute('data-anchor') === 'composer') break;
        await sleep(50);
      }
      if (!stackEl()) { out.verdict = 'NO_STACK'; return out; }

      var base = snap();
      add('0 初态：浮层已贴卡片上方、无可见通知',
        base.anchor === 'composer' && base.position === 'fixed' && base.shown.length === 0 && base.compose,
        { anchor: base.anchor, position: base.position, shown: base.shown.join(',') || '(空)' });

      // 1 + 2：错误条。走界面自己的入口，15 秒自动收起不影响这里的量测。
      window.PentestUI.showError('探针：这条要浮在输入卡片上面');
      await sleep(80);
      var a = snap();
      /* 空隙是 `syncNoticeAnchor()` 里那个 `gap = 8`：8px 是"贴着的一层皮"，
         也是"浮层要属于这张卡片"的可读距离。范围给 6–10px 是为了容纳取整。 */
      add('1 错误条贴在输入卡片上沿（留白 = gap 8px）',
        a.shown.indexOf('error-bar') >= 0 && a.gap !== null && a.gap >= 6 && a.gap <= 10,
        { gap: a.gap, bottomVar: a.bottomVar });
      add('2 浮层水平居中、宽度 ≤ 560px',
        Math.abs((a.stack.left + a.stack.width / 2) - a.vw / 2) <= 2 && a.stack.width <= 560.5,
        { width: Math.round(a.stack.width), left: Math.round(a.stack.left), vw: a.vw });

      // 3 + 4：截断提示也在栈里；两条上下排列、不重叠。
      byId('truncate-banner').classList.remove('hidden');
      await sleep(60);
      var b = snap();
      add('3 截断提示与错误条同栈、同时可见',
        b.shown.indexOf('truncate-banner') >= 0 && b.shown.indexOf('error-bar') >= 0,
        { shown: b.shown.join(',') });
      var bar = box(byId('error-bar')), banner = box(byId('truncate-banner'));
      var between = Math.round(banner.top - bar.bottom);
      add('4 两条上下排列、间隙 = flex gap（8px 左右）',
        between >= 4 && between <= 12, { between: between });
      byId('truncate-banner').classList.add('hidden');

      // 5：坞展开时，最高边缘是坞的上沿。
      var tools = byId('chat-tools'), quick = byId('chat-quick');
      if (tools) {
        tools.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
        for (var j = 0; j < 100; j++) { if (visible(quick)) break; await sleep(50); }
      }
      if (visible(quick)) {
        await sleep(150);
        // 这条守的是"浮层贴的是**当场的**卡片上沿"：卡片一变高，底边就得跟着走。
        // 原来靠展开坞把卡片顶高——**t94 之后坞不在卡片里了**，展开它卡片一点不
        // 变（实测 436 → 436），那条断言就只是在测"夹具把坞打开了"。
        // 所以这里自己把卡片撑高：守的是同一件事，而且不再依赖坞怎么摆。
        var cardEl = sel('.chat-field');
        var baseCard = cardEl ? Math.round(box(cardEl).height) : 0;
        var savedMin = cardEl ? cardEl.style.minHeight : '';
        if (cardEl) cardEl.style.minHeight = (baseCard + 120) + 'px';
        await sleep(200);
        var d = snap();
        var grownCard = cardEl ? Math.round(box(cardEl).height) : 0;
        var gapToCard = edgeTop() !== null ? Math.round(edgeTop() - d.stack.bottom) : null;
        add('5 卡片变高后仍贴它的上沿（不留旧高度）',
          gapToCard !== null && gapToCard >= 6 && gapToCard <= 10 && grownCard > baseCard,
          { gapToCard: gapToCard, cardHeight: grownCard, baseHeight: baseCard });
        if (cardEl) cardEl.style.minHeight = savedMin;
        tools.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
        await sleep(150);
      } else {
        out.notes.push('坞没有展开（夹具的接口没回内容），跳过用例 5');
      }

      // 6：自动继续那一行要在卡片**上方**、且没被卡片盖住。
      // `showAutoNote()`（web/chat.js:669）只做两件事——写 `textContent`、去掉
      // `hidden`；它没有导出入口，所以这里照抄那两步。摆位完全由样式表决定，
      // 而摆位正是要量的东西：卡片用负外边距（`--chat-overlap`）抬进上一行的盒子，
      // 上一行不补偿就会被整块吃掉，`elementFromPoint()` 会返回 `chat-input`。
      // 排队那几行会把它顶上去，而"紧贴卡片"这一种排布才是要守的（两条都补偿了
      // 同一段）；先收起来量，量完还原，别把夹具状态带歪。
      var note = byId('chat-auto-note'), noteText = byId('chat-auto-text');
      var queueEl = byId('chat-queue'), savedQueueHidden = queueEl ? queueEl.hidden : null;
      if (note && noteText) {
        var savedText = noteText.textContent;
        if (queueEl) queueEl.hidden = true;
        noteText.textContent = '自动继续暂停：这个会话还有一轮在跑，等它结束再试。';
        note.hidden = false;
        await sleep(150);
        var nBox = box(note), cardBox = box(sel('.chat-field'));
        var noteGap = Math.round(cardBox.top - nBox.bottom);
        var hit = document.elementFromPoint(nBox.left + nBox.width / 2,
                                            (nBox.top + nBox.bottom) / 2);
        var hitName = hit ? (hit.id || hit.className || hit.tagName) : '(空)';
        add('6 自动继续那一行在卡片上方、没被卡片盖住',
          noteGap >= 8 && noteGap <= 24 && String(hitName).indexOf('chat-auto') === 0,
          { gap: noteGap, hit: hitName });
        // 6b：排队块**在场**时，这一行不该再让 `--chat-overlap`（让的是排队块）。
        // 多让 146px 的症状很显眼：两行之间一块空白，`.chat-compose` 跟着长高一截。
        if (savedQueueHidden === false && queueEl) {
          note.hidden = true;
          queueEl.hidden = false;
          await sleep(150);
          var noteOn = byId('chat-auto-note');
          noteOn.hidden = false;
          await sleep(150);
          var n2 = box(noteOn), q2 = box(queueEl), c2 = box(sel('.chat-field'));
          var gapNoteQueue = Math.round(q2.top - n2.bottom);
          var gapQueueCard = Math.round(c2.top - q2.bottom);
          add('6b 排队块在场时只让一格（两行之间不留一个大洞）',
            gapNoteQueue >= 6 && gapNoteQueue <= 24 && gapQueueCard >= 6 && gapQueueCard <= 24,
            { 行到排队: gapNoteQueue, 排队到卡片: gapQueueCard });
          noteOn.hidden = true;
        }
        note.hidden = true;
        noteText.textContent = savedText;
        if (queueEl) queueEl.hidden = savedQueueHidden;
        await sleep(60);
      } else {
        out.notes.push('夹具里没有 #chat-auto-note，跳过用例 6');
      }

      // 7：没有输入卡片的页签 —— 退回顶部，但不能消失。
      var target = sel('.tab[data-tab="settings"]') || sel('.tab[data-tab="report"]');
      if (target) {
        target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
        await sleep(120);
        // 虚时间把 15 秒的自动收起也走完了：再报一次，验证的是"换页签/置顶"这条
        // 路径本身，而不是 15 秒之前那一次。
        if (window.PentestUI) window.PentestUI.showError('探针：换页签后仍应看得见');
        await sleep(80);
        var t = snap();
        add('7 没有输入卡片的页签：退回顶部且仍可见',
          t.anchor === 'top' && t.stack.top >= 0 && t.stack.top <= 32
            && t.shown.indexOf('error-bar') >= 0,
          { anchor: t.anchor, top: Math.round(t.stack.top) });
      } else {
        out.notes.push('夹具里没有设置/报告页签按钮，跳过用例 7');
      }

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
          'data-notice-probe', btoa(unescape(encodeURIComponent(JSON.stringify(out)))));
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
    # `--window-size` 不能省：默认无头窗口只有 800x600 上下，夹具的输入卡片会被
    # 挤到视口下沿以外（实测卡片顶边 606 ＞ 视口高 488），那时再量"离卡片多远"
    # 量的是屏幕外面——不是布局错，是窗口太矮。用接近真实桌面的大小。
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
           "--window-size=1280,900",
           "--user-data-dir=" + profile,
           "--virtual-time-budget=30000", "--dump-dom",
           "file:///" + path.replace("\\", "/")]
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)
    return done.stdout or ""


def main() -> int:
    browser = _browser()
    if not browser:
        print("SKIP：本机没找到 Edge / Chrome，跳过通知浮层探针。")
        print("      （这不是失败：探针要真实浏览器，而单元测试套件保持零外部依赖。）")
        return 0

    # 必须绝对路径：`--dump-dom` 拿到的 `file:///` + 相对路径会被解析成盘符根下的
    # 同名文件（`F:\.scratch\...`），页面根本打不开，探针当然跑不到。
    out_dir = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                              else tempfile.mkdtemp(prefix="notice-probe-"))
    os.makedirs(out_dir, exist_ok=True)
    fixture = os.path.join(out_dir, "chat-notice.html")
    data = render_ui_fixture.collect()
    render_ui_fixture.build(fixture, data, tab="chat")
    with open(fixture, "r", encoding="utf-8") as handle:
        html = handle.read()
    probed = os.path.join(out_dir, "chat-notice-probe.html")
    with open(probed, "w", encoding="utf-8") as handle:
        handle.write(html.replace("</body>", PROBE + "</body>", 1))

    dom = _dump(browser, probed, os.path.join(out_dir, "browser-profile"))
    match = re.search(r'data-notice-probe="([^"]+)"', dom)
    if not match:
        errors = re.search(r'data-fixture-errors="([^"]*)"', dom)
        print("FAIL：页面没跑到探针。夹具错误：%s" % (errors.group(1) if errors else "(无)"))
        return 2
    payload = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
    print("夹具：%s" % fixture)
    if payload.get("verdict") == "NO_STACK":
        print("FAIL：页面上没有 #notice-stack。")
        return 1
    print("%-46s %s" % ("用例", "结果"))
    for item in payload.get("cases") or []:
        extra = "  " + " ".join("%s=%s" % (k, v) for k, v in item.items()
                                if k not in ("name", "ok"))
        print("  %-44s %s%s" % (item.get("name"), "OK" if item.get("ok") else "**不符**", extra))
    print("结论：%s" % payload.get("verdict"))
    if payload.get("error"):
        print(payload["error"])
    for note in payload.get("notes") or []:
        print("note: %s" % note)
    return 0 if payload.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
