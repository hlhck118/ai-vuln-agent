# -*- coding: utf-8 -*-
"""t91：把"悬停变白"从一句抱怨变成两个数。

量的是**计算样式**（`getComputedStyle`），不是看图猜色：圆钮的 `background-color`
在玻璃主题下是 `rgba(255,255,255,.92)`（半透明，得先合成到画布上），而墨色是
`--on-accent` = #fff——两者一合成就是"白底白箭头"。

对每一档算两件事：
  · 底与图标（`color`）的 WCAG 对比度——"图标还在不在";
  · 底与画布的对比度——"这颗按钮还是个按钮吗"。

用法：python tools/probe_send_hover_contrast.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import probe_ui_style                       # noqa: E402
import probe_send_hover as hover                                # noqa: E402  (复用 PAGE / cell / as_class)


def lin(channel):
    value = channel / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def parse(text):
    """'rgba(255, 255, 255, 0.92)' / 'rgb(27, 99, 214)' → (r, g, b, a)。

    ⚠️ `color-mix()` 的计算值被 Chrome 序列化成 `color(srgb 0.0988 0.3492 0.7493)`
    ——三分量是 0..1 的浮点，不是 0..255，要单独认。这一点不实测写不对：
    自洽的猜测是"和 rgb() 一样读"，于是 0.09 被当成 0.09 档亮度，量出来的对比度
    漂亮但全是假的。
    """
    if text.startswith("color("):
        inner = text[text.index("(") + 1:text.rindex(")")].strip()
        if inner.startswith("srgb"):
            inner = inner[4:]
        parts = [p.strip() for p in inner.replace("/", " ").split()]
        values = [float(p) * 255.0 for p in parts[:3]]
        alpha = float(parts[3]) if len(parts) > 3 else 1.0
        return tuple(values) + (alpha,)
    inner = text[text.index("(") + 1:text.rindex(")")]
    parts = [p.strip() for p in inner.split(",")]
    if len(parts) == 3:
        parts.append("1")
    return tuple(float(p) for p in parts)


def over(fg, bg):
    alpha = fg[3]
    return tuple(fg[i] * alpha + bg[i] * (1 - alpha) for i in range(3))


def ratio(one, two):
    def lum(rgb):
        return (0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2]))
    a, b = lum(one), lum(two)
    high, low = max(a, b), min(a, b)
    return (high + 0.05) / (low + 0.05)


SCRIPT = """
var out=[];
document.querySelectorAll('.cell').forEach(function(c){
  var b=c.querySelector('button'), s=getComputedStyle(b);
  out.push({cap:c.querySelector('.cap').textContent,
            bg:s.backgroundColor, fg:s.color, sh:s.boxShadow});
});
return {page:getComputedStyle(document.body).backgroundColor, cells:out};
"""


def run(css, cards, name, theme="glass"):
    page = hover.PAGE % {"theme": theme, "css": hover.as_class(css),
                         "cells": "".join(cards)}
    path = ".scratch/t91/m-%s.html" % name
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(page)
    return probe_ui_style.probe(path, SCRIPT)


def report(label, data):
    canvas = parse(data["page"])
    print("\n== %s ==" % label)
    for cell in data["cells"]:
        bg = over(parse(cell["bg"]), canvas[:3])
        fg = parse(cell["fg"])
        ink = ratio(fg, bg)
        against = ratio(bg, canvas[:3])
        print("  %-16s 底 %-22s 图标对比 %5.2f:1   底/画布 %5.2f:1   %s"
              % (cell["cap"], "rgb(%d,%d,%d)" % tuple(int(v) for v in bg),
                 ink, against, "有白光" if cell["sh"] != "none" else "-"))
    return data


if __name__ == "__main__":
    css = hover.styled()
    new, old = css, hover.old_css(css)
    fresh = [hover.cell("normal"), hover.cell("stop", "stop")]
    hot_old = [hover.cell("hover-OLD", hover=True, pinned=True),
               hover.cell("hover-OLD stop", "stop", hover=True, pinned=True)]
    hot_new = [hover.cell("hover-NEW", hover=True),
               hover.cell("hover-NEW stop", "stop", hover=True)]
    for theme, label in (("glass", "GLASS"), ("midnight", "MIDNIGHT")):
        report("%s / BEFORE" % label,
               run(old, fresh + hot_old, "old-" + theme, theme))
        report("%s / AFTER" % label,
               run(new, fresh + hot_new, "new-" + theme, theme))
