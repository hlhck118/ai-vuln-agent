# -*- coding: utf-8 -*-
"""候选视觉系统的对比度自检：从 token 源文件里读颜色，按 WCAG 2.1 算实测值。

存在的理由：配色是唯一一种"改了以后测试全绿、只有肉眼（或者说只有算术）能发现
变糟"的属性。`test_ui_contract.py` 管得住 id、外部资源、overflow，管不住
"--muted 在一半底色上不达 AA" 这类事——而那一类缺陷已经真实发生过一次
（见 视觉评审.md P1-1）。

用法：
    python tools/ui_token_contrast.py            # 用 tools/ui-preview/apple-tokens.css
    python tools/ui_token_contrast.py --css X.css
    python tools/ui_token_contrast.py --fail-under 4.5   # 真值低于门槛时返回码 1
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CSS = os.path.join(ROOT, "tools", "ui-preview", "apple-tokens.css")

# 文字要在哪些底上被读：这六档就是界面里所有的"底"。
SURFACES = ["bg", "surface-tray", "surface-1", "surface-2", "surface-3", "surface-4"]
# 要在哪些底上被读的文字 token、门槛（正文 4.5），以及**契约允许的最亮底色**。
# 最后一列不是"豁免"，而是一条成文规则：surface-3/4 是交互态（悬停/按下），
# 落在那些底上的文字本来就不该用强调色，而该用 --label* 或 --accent-text。
# 三个数值分别是 SURFACES 的下标：5=到 surface-4（全部），3=到 surface-2。
TEXT = [("label", 4.5, 5), ("label-2", 4.5, 5), ("label-3", 4.5, 4),
        ("accent", 4.5, 3), ("accent-text", 4.5, 4),
        ("ok", 4.5, 5), ("warn", 4.5, 5), ("danger", 4.5, 2),
        ("sev-critical", 4.5, 2), ("sev-high", 4.5, 5), ("sev-medium", 4.5, 5),
        ("sev-low", 4.5, 5), ("sev-info", 4.5, 4)]
# 亮底实心块上的墨字：门槛同为 4.5（都是小字），但它是"反向"的一对。
INK_ON = ["ok", "warn", "danger", "accent"]


def read_tokens(path: str) -> dict:
    css = open(path, encoding="utf-8").read()
    # 只取 :root 段，避免把规则体里的 rgba 当成 token
    root = re.search(r":root\s*\{(.*?)\n\}", css, re.S)
    if not root:
        raise SystemExit("%s 里找不到 :root 段" % path)
    tokens = {}
    for name, value in re.findall(r"--([\w-]+)\s*:\s*([^;]+);", root.group(1)):
        tokens[name] = value.strip()
    return tokens


def parse_color(value: str, tokens: dict, depth: int = 0):
    """支持 #rgb / #rrggbb / rgba(r,g,b,a) / var(--x)。返回 (r, g, b, a) 0–255 + 0–1。"""
    value = value.strip()
    if value.startswith("var(") and depth < 4:
        name = re.match(r"var\(--([\w-]+)", value).group(1)
        if name not in tokens:
            return None
        return parse_color(tokens[name], tokens, depth + 1)
    value = value.strip()
    if value.startswith("#"):
        body = value[1:]
        if len(body) == 3:
            body = "".join(c * 2 for c in body)
        if len(body) != 6:
            return None
        return (int(body[0:2], 16), int(body[2:4], 16), int(body[4:6], 16), 1.0)
    found = re.match(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+))?\)",
                     value)
    if found:
        return (float(found.group(1)), float(found.group(2)), float(found.group(3)),
                float(found.group(4)) if found.group(4) is not None else 1.0)
    return None


def flatten(fg, bg):
    """a<1 的前景先与底合成——半透明 hairline 与 tinted 底都必须这样算，
    直接拿 alpha 值当颜色算出来的对比度是假的（会虚高）。"""
    if fg[3] >= 1:
        return fg[:3]
    a = fg[3]
    return tuple(fg[i] * a + bg[i] * (1 - a) for i in range(3))


def luminance(rgb):
    parts = []
    for channel in rgb:
        c = channel / 255
        parts.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


def ratio(fg_rgb, bg_rgb):
    a, b = luminance(fg_rgb), luminance(bg_rgb)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def main() -> int:
    parser = argparse.ArgumentParser(description="token 对比度自检")
    parser.add_argument("--css", default=DEFAULT_CSS)
    parser.add_argument("--fail-under", type=float, default=0.0,
                        help="真值低于该门槛即返回码 1（默认只报告不拦）")
    args = parser.parse_args()

    tokens = read_tokens(args.css)
    surfaces = {}
    for name in SURFACES:
        color = parse_color(tokens.get(name, ""), tokens)
        if color:
            surfaces[name] = color
    print("token 源：%s（%d 个 token，%d 档底色）"
          % (os.path.relpath(args.css, ROOT), len(tokens), len(surfaces)))
    print("契约：文字只落在「允许的最亮底」及更暗的底上；超出契约的格子标 x，不计入最低值。")
    print()
    labels = list(surfaces)
    print("%-14s %s" % ("文字 token", "  ".join("%-9s" % s[:9] for s in labels)))
    worst = ("", 99.0)
    for name, floor, max_index in TEXT:
        color = parse_color(tokens.get(name, ""), tokens)
        if not color:
            continue
        cells = []
        for index, (surface, bg) in enumerate(surfaces.items()):
            value = ratio(flatten(color, bg), bg[:3])
            if index > max_index:
                cells.append("%-9s" % ("%.2f x" % value))
                continue
            mark = "" if value >= floor else "!"
            cells.append("%-9s" % ("%.2f%s" % (value, mark)))
            if value < worst[1]:
                worst = ("%s on %s" % (name, surface), value)
        print("%-14s %s" % (name, "  ".join(cells)))
    print()
    print("亮底实心块上的墨字（--ink #0B0B0D）：")
    ink = parse_color(tokens.get("ink", "#000000"), tokens)
    for name in INK_ON:
        color = parse_color(tokens.get(name, ""), tokens)
        if not color:
            continue
        value = ratio(ink[:3], color[:3])
        print("  %-10s %.2f %s" % (name, value, "" if value >= 4.5 else "  ← 低于 4.5"))
    print()
    print("全表最低值：%s = %.2f" % (worst[0], worst[1]))
    if args.fail_under and worst[1] < args.fail_under:
        print("FAIL：最低值 %.2f 低于门槛 %.2f" % (worst[1], args.fail_under))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
