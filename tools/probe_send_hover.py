# -*- coding: utf-8 -*-
"""t91：悬停那一档的**改前 / 改后**（发送圆钮）。

为什么需要专门的脚本：`render_ui_fixture.py` 出的图没有指针，`:hover` 永远不匹配，
**静态截图里看不见这条 bug**——而它恰恰是"鼠标一放上去就发生"的那种。

做法：把样式表里每一处 `:hover` 改写成 `.__h`（一个类）。CSS 里类选择器与伪类的
**特异性完全相同**（都是 (0,1,0)），而 `.btn.__h` / `.chat-send.__h` 的先后与胜负
关系与真实悬停**逐条一致**。所以这不是"模拟外观"，是把指针状态换成页面状态。
唯一手写的是"改前"那一档：旧样式表里胜出的是 `.btn:hover`，这里给它一个 `pinned`
类让它照样胜出（声明本身逐字照抄）。

产出：
  .scratch/t91/old-glass.png / old-midnight.png   改前（悬停时圆钮被换成中性面）
  .scratch/t91/new-glass.png / new-midnight.png   改后（同色相挪一档 + 三层白光）
  .scratch/t91/hover.png                          2×2 拼图（dev 尺度 4x）

用法：python tools/probe_send_hover.py
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import render_ui_fixture as fixture     # noqa: E402

SEND = ('<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>')
STOP = '<rect x="6.5" y="6.5" width="11" height="11" rx="2.5"/>'

MARK = "/* ---------- 悬停：**不许被换成一个中性面**（t91） ----------"
LAST = ".chat-send.is-stop:not(:disabled):hover"


def styled():
    with open("web/style.css", encoding="utf-8") as handle:
        return handle.read()


def old_css(css):
    """把 t91 新加的那一段切掉，得到改前的样式表。"""
    start = css.index(MARK)
    end = css.index("\n}\n", css.index(LAST)) + 3
    return css[:start] + css[end:]


def as_class(css):
    """:hover → .__h：类与伪类同特异性，级联结果逐条等价。"""
    return css.replace(":hover", ".__h")


def icon(kind):
    if kind == "stop":
        return ('<svg viewBox="0 0 24 24" fill="currentColor">%s</svg>' % STOP)
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">'
            '%s</svg>' % SEND)


def cell(caption, kind="send", hover=False, pinned=False):
    classes = ["btn", "chat-send"]
    if kind == "stop":
        classes.append("is-stop")
    if hover:
        classes.append("__h")
    if pinned:
        classes.append("pinned")
    return ('<div class="cell"><button class="%s" type="button">%s</button>'
            '<div class="cap">%s</div></div>'
            % (" ".join(classes), icon(kind), caption))


PAGE = """<!DOCTYPE html><html lang="zh-CN" data-theme="%(theme)s"><head>
<meta charset="utf-8"><style>%(css)s</style>
<style>
  body{margin:0;padding:26px 30px 14px;background:var(--bg);}
  .row{display:flex;gap:34px;align-items:flex-start;}
  .cell{display:flex;flex-direction:column;align-items:center;gap:10px;}
  .cap{font:12px/1.4 var(--sans);color:var(--text-dim);white-space:nowrap;}
  /* 只给"改前"那一格用：`.btn:hover` 当年胜出的那两条声明，逐字照抄。 */
  .pinned.btn.__h{background:var(--panel-3);border-color:var(--accent-dim);}
</style></head><body><div class="row">%(cells)s</div></body></html>"""


def render(name, css, cards, theme):
    page = PAGE % {"theme": theme, "css": css, "cells": "".join(cards)}
    path = ".scratch/t91/%s.html" % name
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(page)
    browser = fixture._browser()
    if not browser:
        raise SystemExit("找不到 Edge / Chrome")
    profile = tempfile.mkdtemp(prefix="t91-")
    png = os.path.abspath(".scratch/t91/%s.png" % name)
    subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-first-run",
                    "--hide-scrollbars", "--user-data-dir=" + profile,
                    "--force-device-scale-factor=4",
                    "--window-size=460,150",
                    "--virtual-time-budget=3000",
                    "--screenshot=" + png,
                    "file:///" + os.path.abspath(path).replace("\\", "/")],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace", timeout=180, check=False)
    return png


def montage(paths):
    from PIL import Image
    tiles = [Image.open(p) for p in paths]
    # 每张图裁掉右侧/下方多出来的空白（窗口比内容宽），只留内容
    tiles = [t.crop(t.getbbox() or (0, 0, t.width, t.height)) for t in tiles]
    pad, gap = 18, 14
    width = max(t.width for t in tiles) * 2 + gap + pad * 2
    height = max(t.height for t in tiles) * 2 + gap + pad * 2
    sheet = Image.new("RGB", (width, height), (24, 24, 28))
    for index, tile in enumerate(tiles):
        x = pad + (index % 2) * (tiles[0].width + gap)
        y = pad + (index // 2) * (max(t.height for t in tiles) + gap)
        sheet.paste(tile, (x, y))
    out = ".scratch/t91/hover.png"
    sheet.save(out)
    return out


if __name__ == "__main__":
    css = styled()
    new, old = css, old_css(css)
    produced = []
    for theme, label in (("glass", "液态玻璃"), ("midnight", "午夜")):
        produced.append(render(
            "old-" + theme, as_class(old),
            [cell("%s · 正常" % label),
             cell("%s · 旧悬停" % label, hover=True, pinned=True),
             cell("%s · 停止·旧悬停" % label, "stop", hover=True, pinned=True)],
            theme))
        produced.append(render(
            "new-" + theme, as_class(new),
            [cell("%s · 正常" % label),
             cell("%s · 新悬停" % label, hover=True),
             cell("%s · 停止·新悬停" % label, "stop", hover=True)],
            theme))
    print("pieces:", *[os.path.basename(p) for p in produced])
    print("sheet:", montage([".scratch/t91/old-glass.png", ".scratch/t91/new-glass.png",
                             ".scratch/t91/old-midnight.png",
                             ".scratch/t91/new-midnight.png"]))
