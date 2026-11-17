# -*- coding: utf-8 -*-
"""坞的收起路径探针：点坞**以外**的任何位置，要把「可用能力 / 技能手册」收起来。

## 为什么需要它

这条行为由三样东西共同决定，而它们**都只在浏览器里成立**：

1. 一个挂在 `document` 上的 `click` 监听（冒泡阶段）；
2. 它的判据 `insideDock(event.target)`——依赖真实的 DOM 包含关系；
3. 收起的看得见的那一面：`#chat-quick` 上的 `data-dock="none"` 与样式表那条
   `display: none`。

源码文本只能证明「代码是这么写的」；**「点下去真的收起来了」**、以及更麻烦的那半边
「点坞里的卡片**没有**被收掉」都只有真跑一遍才知道。后者尤其要紧：写宽了就成了
「点一下卡片坞就没了，想挑第二张得重新展开」，而它不会让任何一条功能测试变红。

## 判定标准

| # | 动作 | 期望 |
|---|---|---|
| 0 | 夹具自带的展开（`--dock tools`） | 开着、有卡片 |
| 1 | 点坞里的卡片 | **仍开着**，且输入框被填 |
| 2 | 点 `.chat-log`（坞外） | 收起 |
| 3 | 点 `#chat-input`（坞外） | 收起 |
| 4 | 点 `.chat-compose`（坞所在卡片的父层，算坞外） | 收起 |
| 5 | 开着 tools 时点 `#chat-skills` | **仍开着**，且切到 skills |
| 6 | 再点一次 `#chat-skills`（当前格） | 收起（旧的 toggle 没被挤掉） |
| 7 | 开着时按 Esc | 收起 |
| 8 | 点 `#chat-model-select`（坞外控件） | 收起 |
| 9 | 输入框里有草稿时点坞外 | 收起，且**草稿一个字不变** |
| 10 | 已收起时再点坞外 | 仍是收起（幂等，不报错） |

## 用法

    python tools/probe_dock_dismiss.py [输出目录]

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

# 探针：只发事件、读属性，不动应用代码。事件用 `MouseEvent` / `KeyboardEvent`
# 走**真实冒泡路径**——这正是被测的那条链（按钮自己的 handler 先跑，document 上的
# 监听后跑）；直接调 `closeDock()` 等于把两件事都跳过去了。
PROBE = r"""
<script>
(function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  function snap() {
    var n = document.getElementById('chat-quick');
    var input = document.getElementById('chat-input');
    var tools = document.getElementById('chat-tools');
    var skills = document.getElementById('chat-skills');
    return {
      mode: n ? n.getAttribute('data-dock') : '(no #chat-quick)',
      shown: n ? (getComputedStyle(n).display !== 'none') : false,
      chips: n ? n.querySelectorAll('.dock-chip').length : 0,
      tools: tools ? tools.getAttribute('aria-pressed') : null,
      skills: skills ? skills.getAttribute('aria-pressed') : null,
      draft: input ? input.value : null
    };
  }
  function click(node) {
    if (!node) return false;
    node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
    return true;
  }
  function esc() {
    var target = document.activeElement || document.body;
    target.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
  }
  var byId = function (id) { return document.getElementById(id); };
  var sel = function (s) { return document.querySelector(s); };

  async function run() {
    var out = { cases: [], notes: [] };
    var step = function (name, action, check, note) {
      action();
      var after = snap();
      var ok = false;
      try { ok = !!check(after); } catch (e) { out.notes.push(name + ': ' + e); }
      out.cases.push({ name: name, mode: after.mode, shown: after.shown,
                       chips: after.chips, tools: after.tools, skills: after.skills,
                       draft: after.draft, note: note || '', ok: ok });
      return after;
    };

    try {
      // 0. 夹具自己展开（--dock tools）。它点完按钮后要等接口回来才建卡片，所以轮询。
      for (var i = 0; i < 120; i++) {
        var s = snap();
        if (s.mode === 'tools' && s.chips > 0) break;
        await sleep(100);
      }
      out.base = snap();
      out.cases.push({ name: '0 夹具展开后的初态', mode: out.base.mode,
                       shown: out.base.shown, chips: out.base.chips,
                       tools: out.base.tools, skills: out.base.skills,
                       draft: out.base.draft, note: 'data-dock-fill 见顶层字段',
                       ok: out.base.mode === 'tools' && out.base.shown && out.base.chips > 0 });
      if (!out.cases[0].ok) { out.verdict = 'NOT_OPEN'; return out; }

      // 1. 点坞里的卡片：坞必须**留在原地**，输入框要真的被填。
      step('1 点卡片（坞内）', function () {
          click(sel('#chat-quick .dock-chip'));
        },
        function (a) { return a.mode === 'tools' && a.shown && a.draft && a.draft.length; },
        '坞内 → 不收；同时验证"点一下填进输入框"这条链还在');

      // 2. 坞外：日志区。
      step('2 点 .chat-log（坞外）', function () { click(sel('#chat-log')); },
        function (a) { return a.mode === 'none' && !a.shown; });

      // 3. 坞外：输入框本身。**这一条最常被漏**——它是展开坞的原因，也是收起它之后
      //    视线的落点。
      step('3 打开后点 #chat-input（坞外）', function () {
          click(byId('chat-tools'));
        }, function (a) { return a.mode === 'tools'; }, '先确认能再打开');
      step('3b 点 #chat-input', function () { click(byId('chat-input')); },
        function (a) { return a.mode === 'none' && !a.shown; });

      // 4. 坞外：坞的**父层**。包含关系判反（比如用 `container.contains(target)` 写成
      //    `target.contains(container)`）时，这一格恰好是分界处，所以单独走一遍。
      step('4 打开后点 .chat-compose（坞所在卡片的父层）', function () {
          click(byId('chat-tools'));
        }, function (a) { return a.mode === 'tools'; });
      step('4b 点 .chat-compose 本身', function () { click(sel('.chat-compose')); },
        function (a) { return a.mode === 'none'; });

      // 5. 点**另一颗**入口：切过去，不是关掉。落点判漏了按钮就会出现"收起后立刻展开"
      //    ——表现是点一下没反应，而 DOM 上确实发生了两次变化，光看状态看不出哪次对。
      step('5 开着 tools 时点 #chat-skills', function () {
          click(byId('chat-tools'));
        }, function (a) { return a.mode === 'tools'; });
      step('5b 点 #chat-skills', function () { click(byId('chat-skills')); },
        function (a) { return a.mode === 'skills' && a.shown && a.skills === 'true'
                              && a.tools === 'false'; },
        '切格：仍开着，且选中态换到 skills 那一颗');

      // 6. 再点一次当前格 = 收起。旧的 toggle 不能被新监听挤掉。
      step('6 再点一次 #chat-skills（当前格）', function () { click(byId('chat-skills')); },
        function (a) { return a.mode === 'none' && a.tools === 'false' && a.skills === 'false'; });

      // 7. Esc。
      step('7 打开后按 Esc', function () { click(byId('chat-tools')); },
        function (a) { return a.mode === 'tools'; });
      step('7b 按 Esc', function () { esc(); },
        function (a) { return a.mode === 'none'; });

      // 8. 坞外的**控件**（下拉框）。它自己有 change 行为，点它不该被 preventDefault
      //    之类的写法吃掉。
      step('8 打开后点坞外的 #chat-model-select', function () {
          click(byId('chat-tools'));
        }, function (a) { return a.mode === 'tools'; });
      step('8b 点 #chat-model-select', function () { click(byId('chat-model-select')); },
        function (a) { return a.mode === 'none'; });

      // 9. 草稿保护：收起只管坞，不许碰输入框里那行字。
      step('9 草稿保留', function () {
          byId('chat-input').value = 'KEEP-ME: 扫一下 /srv/app';
          click(byId('chat-tools'));
        },
        function (a) { return a.mode === 'tools' && a.draft === 'KEEP-ME: 扫一下 /srv/app'; },
        '先把输入框写成一句话再展开');
      step('9b 点坞外收起后草稿仍在', function () { click(sel('#chat-log')); },
        function (a) { return a.mode === 'none' && a.draft === 'KEEP-ME: 扫一下 /srv/app'; },
        '收起不是一次输入');

      // 10. 已经收起时点坞外：幂等，不该抛错、也不该把坞重新画出来。
      step('10 已收起时再点坞外', function () { click(sel('#chat-log')); },
        function (a) { return a.mode === 'none' && a.chips === 0; });

      out.fill = document.documentElement.getAttribute('data-dock-fill');
      out.fill2 = document.documentElement.getAttribute('data-dock-fill2');
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
          'data-dock-probe',
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
           "--user-data-dir=" + profile,
           "--virtual-time-budget=30000", "--dump-dom",
           "file:///" + path.replace("\\", "/")]
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)
    return done.stdout or ""


def main() -> int:
    browser = _browser()
    if not browser:
        print("SKIP：本机没找到 Edge / Chrome，跳过坞收起探针。")
        print("      （这不是失败：探针要真实浏览器，而单元测试套件保持零外部依赖。）")
        return 0

    out_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="dock-probe-")
    # 相对路径会让 `file:///` 少掉盘符，浏览器静默交出空 DOM，看起来像"探针没跑到"。
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    fixture = os.path.join(out_dir, "chat-dock.html")
    data = render_ui_fixture.collect()
    render_ui_fixture.build(fixture, data, tab="chat", dock="tools")
    with open(fixture, "r", encoding="utf-8") as handle:
        html = handle.read()
    probed = os.path.join(out_dir, "chat-dock-probe.html")
    with open(probed, "w", encoding="utf-8") as handle:
        handle.write(html.replace("</body>", PROBE + "</body>", 1))

    dom = _dump(browser, probed, os.path.join(out_dir, "browser-profile"))
    match = re.search(r'data-dock-probe="([^"]+)"', dom)
    if not match:
        errors = re.search(r'data-fixture-errors="([^"]*)"', dom)
        print("FAIL：页面没跑到探针。夹具错误：%s" % (errors.group(1) if errors else "(无)"))
        return 2
    payload = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
    print("夹具：%s" % fixture)
    print("夹具自带的展开与点卡片记录：data-dock-fill=%s ｜ data-dock-fill2=%s"
          % (payload.get("fill"), payload.get("fill2")))
    print("%-34s %-6s %-6s %s" % ("用例", "模式", "可见", "结果"))
    for item in payload.get("cases") or []:
        print("  %-32s %-6s %-6s %s%s"
              % (item.get("name"), item.get("mode"),
                 "是" if item.get("shown") else "否",
                 "OK" if item.get("ok") else "**不符**",
                 ("  " + item["note"]) if item.get("note") else ""))
    print("结论：%s" % payload.get("verdict"))
    if payload.get("error"):
        print(payload["error"])
    for note in payload.get("notes") or []:
        print("note: %s" % note)
    return 0 if payload.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
