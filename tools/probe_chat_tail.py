# -*- coding: utf-8 -*-
"""对话末尾落点的探针：最后一样东西**必须停在输入卡片上沿之上 `--chat-fade` 处**。

## 为什么需要它

`.chat-log` 的底部内边距原本是算出来的：`calc(var(--chat-overlap) + var(--chat-fade))`，
依据是「叠进深度 = 卡片高度」那条不变式。这条不变式在**卡片上方长出别的行**时就
不成立了——`.chat-compose` 自己的 12px 上内边距、16px flex `gap`，以及排队行 /
告警行各自的 `margin|padding-bottom: var(--chat-overlap)`，都会把卡片推到离记录区
盒底更远（或更近）的地方去。结果是末尾空出 40px，用户看到的是「弹窗离输入框太远」。

源码文本对位置什么也证明不了：算式写着 `overlap + fade`，量出来却是 188 与 148。
这里量的是**真实浏览器里的两个矩形**，因此和数据无关、和消息多少无关；任何一处把
「量」改回「算」都会当场红。

## 判定标准

| # | 状态 | 期望 |
|---|---|---|
| 0 | 正文结尾（滚到底） | 末条离卡片上沿 = `--chat-fade`（12px±1.5） |
| 0b | 同上 | `padding-bottom` 是 12px 量级（JS 写的），不是 CSS 兜底的 188px 量级 |
| 1 | 末尾挂一张「已批准」审查卡 | 卡底离卡片上沿 = 12（`:last-child` 去掉那 12px 下边距） |
| 2 | 审查卡后面还有一条消息 | 卡**不再**贴死：卡底与下一条之间留 ~12；末条仍是 12 |
| 3 | 排队长出来（卡片被推到记录区下面） | 末尾只留渐隐那一段，不留一整屏空白 |
| 4 | 排队收起 | 回到 12 |
| 5 | 模型告警行可见 | 同 3 |
| 6 | 告警收起 | 回到 12 |
| 7 | 选项面板出现（挤在卡片上方的一行） | 仍为 12，且面板不压住卡片上沿 |

## 用法

    python tools/probe_chat_tail.py [输出目录]

需要本机有 Edge 或 Chrome（没有浏览器时打印 SKIP 并以 0 退出，不误报失败）。本工具
**不进单元测试套件**：`python -m unittest discover -s tests` 必须保持零外部依赖。
结构契约由 `tests/test_ui_contract.py::ChatLogTailContractTestCase` 守。

## 夹具与真页面的差别（别把这里的绝对值当产品形态）

`tools/render_ui_fixture.py` 造出来的夹具里，坞（`#chat-quick`）与目标行是**卡片上方**
的一整块，于是卡片会被顶到记录区盒子下面去（`overlap` 为负）；真页面里它们展开时
卡片是叠进记录区的（`overlap` 为正）。所以这里的判定是**规则式**的：卡片叠着就
量"离卡片上沿"，被顶到下面就量"离记录区盒底"，两边都必须是 `--chat-fade`。产品里
那组绝对值（40 → 12、52 → 12、90 → 12）是真页面量出来的，见
`tests/test_ui_contract.py::ChatLogTailContractTestCase` 的类注释。
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
# 消息是探针自己造的：这里要的是「记录区有内容、能滚」，不是真实那一轮的文本。
# 造 30 条是为了让记录区**溢出**——记录区不滚的时候它的盒底由内容决定，
# 那时「末条离卡片多远」量的是别的东西。
PROBE = r"""
<script>
(function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  function byId(id) { return document.getElementById(id); }
  function sel(s) { return document.querySelector(s); }
  function box(node) {
    var r = node.getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, height: r.height, left: r.left, width: r.width };
  }
  function cs(node, prop) { return getComputedStyle(node).getPropertyValue(prop).trim(); }
  function msg(cls, text) {
    var node = document.createElement('div');
    node.className = cls;
    var body = document.createElement('div');
    body.className = 'chat-msg-body';
    body.textContent = text;
    node.appendChild(body);
    return node;
  }
  /* 审查卡按 `web/chat.js` 真正生成的那串 class 造：量的是**产品里的那一张**，
     不是一个看起来像的盒子（`.chat-log .chat-review` 的边距就是量它的）。 */
  function review() {
    var wrap = document.createElement('div');
    wrap.className = 'chat-review';
    var card = document.createElement('div');
    card.className = 'chat-review-card chat-review-done';
    card.innerHTML = '<div class="chat-review-head">'
      + '<span class="chat-review-title">已批准</span>'
      + '<span class="chat-review-tool">run_command</span></div>'
      + '<p class="chat-review-headline">在控制面执行命令：扫描 10.0.0.0/24 的 22 端口</p>'
      + '<ul class="chat-review-reasons">'
      + '<li>这是任意命令执行：这台机器上的文件、进程、网络它都碰得到。</li>'
      + '<li>命令里出现改写型动作（set-content）：它会改动本机的文件或系统状态。</li>'
      + '</ul>'
      + '<p class="chat-review-note">auto: 自动批准并执行（审查档位 auto）</p>';
    wrap.appendChild(card);
    return wrap;
  }
  function snap() {
    var log = byId('chat-log'), card = sel('.chat-field');
    var last = log.lastElementChild;
    var reviewBox = sel('.chat-review');
    var lr = box(log), cr = box(card);
    return {
      logBottom: Math.round(lr.bottom),
      cardTop: Math.round(cr.top),
      cardHeight: Math.round(cr.height),
      /* 叠进深度：正的 = 记录区的盒子伸到卡片上沿下面多少；负的 = 卡片整张
         落到记录区下面去了（排队行 / 告警行可见时就是这种形态）。 */
      overlap: Math.round(lr.bottom - cr.top),
      /* JS 写进去的那个值。CSS 兜底是 188px 量级，写进来的是 12px 量级——
         两者差一个数量级，所以"跑没跑"一眼可辨。 */
      padBottom: cs(log, 'padding-bottom'),
      gapLast: last ? Math.round(cr.top - box(last).bottom) : null,
      gapLogBottom: last ? Math.round(lr.bottom - box(last).bottom) : null,
      lastClass: last ? String(last.className) : '',
      /* 末条离审查卡底多远：队/告警行可见时卡片的 `gapLast` 里含着它们，
         "两格之间该留多少"看的是这一个。 */
      gapCard: reviewBox ? Math.round(cr.top - box(reviewBox).bottom) : null,
      /* 卡底离记录区**盒底**：卡片叠进记录区时它等于 gapCard，卡片被推到下面时
         它是"卡底到记录区尽头"的那一段（末条该离盒底多远就看它）。 */
      gapCardLog: reviewBox ? Math.round(lr.bottom - box(reviewBox).bottom) : null,
      /* 审查卡底下一条（如果有）与卡底之间的间距：不是末格时那 12px 要留着。 */
      gapCardNext: (function () {
        if (!reviewBox) return null;
        var next = reviewBox.nextElementSibling;
        return next ? Math.round(box(next).top - box(reviewBox).bottom) : null;
      })(),
      vw: window.innerWidth, vh: window.innerHeight
    };
  }
  /* JS 走 `scheduleNoticeAnchor()` 那条路（rAF + 32ms 兜底），两帧就够；
     给 350ms 是为了无头环境下 rAF 被饿死时那条定时器兜底也能轮到。 */
  function settle() { return sleep(350); }

  async function run() {
    var out = { cases: [], notes: [] };
    function add(name, ok, extra) {
      var item = { name: name, ok: !!ok };
      if (extra) { for (var k in extra) { item[k] = extra[k]; } }
      out.cases.push(item);
      return item;
    }
    function near(value, want) { return value !== null && Math.abs(value - want) <= 1.5; }
    /* 末尾该停在哪，取决于卡片此刻是不是"叠"在记录区上：
         * 叠着（`overlap > 0`）—— 停在其上沿之上 `--chat-fade` 处，这就是用户看到的
           "输入框上方那道缝"；
         * 没叠（`overlap <= 0`）—— 卡片被坞 / 排队行 / 告警行顶到记录区下面去了，
           这时该恒定的是"离记录区盒底 = fade"，再按叠进深度留白就是白留一整屏。
       两种情况都不能空出一大段——所以判定既看值也看当前形态。 */
    function tailOk(s, fade) {
      return s.overlap > 0 ? near(s.gapLast, fade) : near(s.gapLogBottom, fade);
    }
    function reviewOk(s, fade) {
      return near(s.overlap > 0 ? s.gapCard : s.gapCardLog, fade);
    }
    try {
      for (var i = 0; i < 200; i++) {
        if (sel('.chat-compose') && byId('chat-log') && sel('.chat-field')) break;
        await sleep(50);
      }
      var log = byId('chat-log'), card = sel('.chat-field'), main = sel('.chat-main');
      if (!log || !card || !main) { out.verdict = 'NO_CHAT'; return out; }
      var fade = parseFloat(cs(main, '--chat-fade')) || 12;

      // 造内容：30 轮对话，滚到底。
      log.innerHTML = '';
      for (var n = 1; n <= 30; n++) {
        log.appendChild(msg('chat-msg chat-msg-user', '第 ' + n + ' 轮：扫一下 10.0.0.0/24 的暴露面。'));
        log.appendChild(msg('chat-msg chat-msg-ai', '第 ' + n + ' 轮回复：先做端口与指纹，再按需升级。'));
      }
      await settle();
      log.scrollTop = log.scrollHeight;
      await settle();

      var base = snap();
      add('0 正文结尾：末尾停在可贴的那条边之上 --chat-fade', tailOk(base, fade),
        { gap: base.gapLast, gapLogBottom: base.gapLogBottom, want: fade,
          padBottom: base.padBottom, overlap: base.overlap });
      add('0b 内边距是 JS 量出来的（不是 CSS 兜底那条）', parseFloat(base.padBottom) < 100,
        { padBottom: base.padBottom });

      // 1：末尾挂一张「已批准」审查卡。
      log.appendChild(review());
      await settle();
      log.scrollTop = log.scrollHeight;
      await settle();
      var one = snap();
      add('1 审查卡结尾：卡底离可贴的那条边 = 12（`:last-child` 去掉下边距）',
        reviewOk(one, fade),
        { gapCard: one.gapCard, gapCardLog: one.gapCardLog, want: fade,
          lastClass: one.lastClass });

      // 2：卡后面再有一条消息 —— 那 12px 是两格间距，这里要保留。
      log.appendChild(msg('chat-msg chat-msg-ai', '（这条在审查卡之后）'));
      await settle();
      log.scrollTop = log.scrollHeight;
      await settle();
      var two = snap();
      /* 实测 24：审查卡自己的下边距 12 + `.chat-log` 的 flex `gap` 12
         （flex 容器里相邻外边距不合并，所以是 24 而不是 12）。这里只要它**没被
         抹成 0**——`:last-child` 那条规则不该影响到非末格。 */
      add('2 审查卡不是末格：与下一条之间仍留间距（不该被抹成 0）',
        two.gapCardNext !== null && two.gapCardNext >= 16 && two.gapCardNext <= 28,
        { gapCardNext: two.gapCardNext });
      add('2b 末条仍然停在可贴的那条边之上 --chat-fade', tailOk(two, fade),
        { gap: two.gapLast, gapLogBottom: two.gapLogBottom });
      log.lastElementChild.remove();
      await settle();

      // 3 / 4：排队行。它自己的 `margin-bottom: var(--chat-overlap)` 会把卡片推下去。
      var queue = byId('chat-queue');
      if (queue) {
        queue.hidden = false;
        queue.innerHTML = '<div class="chat-queue-head">'
          + '<span class="chat-queue-title">排队中</span></div>'
          + '<ul class="chat-queue-list"><li>再扫一遍 22 端口</li></ul>';
        await settle();
        log.scrollTop = log.scrollHeight;
        await settle();
        var q = snap();
        add('3 排队行可见：末尾只留渐隐，不再留一整屏空白',
          q.overlap <= 0 && near(q.gapLogBottom, fade),
          { overlap: q.overlap, gapLogBottom: q.gapLogBottom, padBottom: q.padBottom });
        queue.hidden = true;
        queue.innerHTML = '';
        await settle();
        log.scrollTop = log.scrollHeight;
        await settle();
        var qr = snap();
        add('4 排队收起：回到 12', near(qr.gapCard, fade),
          { gapCard: qr.gapCard, overlap: qr.overlap });
      } else {
        out.notes.push('夹具里没有 #chat-queue，跳过用例 3/4');
      }

      // 5 / 6：模型告警行。机制同上，但走 `.chat-notice:not(:empty)` 的 padding。
      var notice = byId('chat-model');
      if (notice) {
        notice.textContent = '未启用模型：先去设置里选一个';
        await settle();
        log.scrollTop = log.scrollHeight;
        await settle();
        var w = snap();
        add('5 告警行可见：末尾只留渐隐',
          w.overlap <= 0 && near(w.gapLogBottom, fade),
          { overlap: w.overlap, gapLogBottom: w.gapLogBottom, padBottom: w.padBottom });
        notice.textContent = '';
        await settle();
        log.scrollTop = log.scrollHeight;
        await settle();
        var wr = snap();
        add('6 告警收起：回到 12', near(wr.gapCard, fade),
          { gapCard: wr.gapCard, overlap: wr.overlap });
      } else {
        out.notes.push('夹具里没有 #chat-model，跳过用例 5/6');
      }

      // 7：选项面板出现 —— 它是 `.chat-compose` 里排在卡片**上面**的一行（不是把卡片
      //    顶高的那种写法：`--chat-choice-h` 早已没有消费者），于是卡片被往下推、
      //    叠进深度跟着变。写死一个数就会在这里红。
      var choice = byId('chat-choice');
      if (choice) {
        choice.hidden = false;
        choice.innerHTML = '<div class="chat-choice-head"><span class="chat-choice-ask">选一件或几件继续：</span>' + '<span class="chat-choice-count">一件都没选</span></div>'
          + '<ul class="chat-choice-list"><li class="chat-choice-item">只扫常见端口</li>'
          + '<li class="chat-choice-item">扫全端口</li></ul>';
        for (var s2 = 0; s2 < 2; s2++) { await settle(); }
        if (typeof log.scrollTop === 'number') { log.scrollTop = log.scrollHeight; }
        await settle();
        var g = snap();
        var panel = box(choice);
        /* 面板在卡片上方那一格：它的底沿不该越过卡片上沿，末尾也不该因此跑远。 */
        add('7 选项面板出现：末尾仍是 --chat-fade，且面板不压住卡片上沿',
          reviewOk(g, fade) && (!panel || panel.bottom <= g.cardTop + 1),
          { gapCard: g.gapCard, gapCardLog: g.gapCardLog, cardHeight: g.cardHeight,
            panelBottom: panel ? panel.bottom : null, cardTop: g.cardTop });
      } else {
        out.notes.push('夹具里没有 #chat-choice，跳过用例 7');
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
          'data-tail-probe', btoa(unescape(encodeURIComponent(JSON.stringify(out)))));
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
    # `--window-size` 不能省：默认无头窗口只有 800x600 上下，卡片会被挤到视口外，
    # 那时量到的"离卡片多远"是屏幕外面的距离。
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
        print("SKIP：本机没找到 Edge / Chrome，跳过对话末尾探针。")
        print("      （这不是失败：探针要真实浏览器，而单元测试套件保持零外部依赖。）")
        return 0

    # 必须绝对路径：`--dump-dom` 拿到的 `file:///` + 相对路径会被解析成盘符根下的
    # 同名文件，页面根本打不开。
    out_dir = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                              else tempfile.mkdtemp(prefix="chat-tail-probe-"))
    os.makedirs(out_dir, exist_ok=True)
    fixture = os.path.join(out_dir, "chat-tail.html")
    render_ui_fixture.build(fixture, render_ui_fixture.collect(), tab="chat")
    with open(fixture, "r", encoding="utf-8") as handle:
        html = handle.read()
    probed = os.path.join(out_dir, "chat-tail-probe.html")
    with open(probed, "w", encoding="utf-8") as handle:
        handle.write(html.replace("</body>", PROBE + "</body>", 1))

    dom = _dump(browser, probed, os.path.join(out_dir, "browser-profile"))
    match = re.search(r'data-tail-probe="([^"]+)"', dom)
    if not match:
        errors = re.search(r'data-fixture-errors="([^"]*)"', dom)
        print("FAIL：页面没跑到探针。夹具错误：%s" % (errors.group(1) if errors else "(无)"))
        return 2
    payload = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
    print("夹具：%s" % fixture)
    if payload.get("verdict") == "NO_CHAT":
        print("FAIL：页面上没有 #chat-log / .chat-field。")
        return 1
    print("%-50s %s" % ("用例", "结果"))
    for item in payload.get("cases") or []:
        extra = "  " + " ".join("%s=%s" % (k, v) for k, v in item.items()
                                if k not in ("name", "ok"))
        print("  %-48s %s%s" % (item.get("name"), "OK" if item.get("ok") else "**不符**", extra))
    print("结论：%s" % payload.get("verdict"))
    if payload.get("error"):
        print(payload["error"])
    for note in payload.get("notes") or []:
        print("note: %s" % note)
    return 0 if payload.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
