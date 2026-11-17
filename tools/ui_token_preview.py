# -*- coding: utf-8 -*-
"""把候选视觉系统注入真实夹具，出「改前 / 改后」对照图。

为什么单独一个脚本而不是给 `render_ui_fixture.py` 加参数：对照图需要**同一份 DOM
渲染两次**（只差一层 `<style>`），这样两张图的差异可以逐像素归因到样式表，而不是
"两次采集的数据不同"。所以这里的做法是——用夹具生成一份 HTML，原样截一张；
再把候选 CSS 注入它的 `</head>` 之前，截第二张。夹具自身的选项一行都不用改。

用法：
    python tools/ui_token_preview.py --out DIR [--tabs chat,setup,findings]
                                     [--css tools/ui-preview/apple-tokens.css]

产物：DIR/before-<tab>.png、DIR/after-<tab>.png（以及对应的 .html）。
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import render_ui_fixture as fixture          # noqa: E402

DEFAULT_TABS = ["chat", "graph", "findings", "assets", "report",
                "setup", "settings", "guide"]

INJECT = ("\n<!-- ui_token_preview: 候选视觉系统（工具注入，不进产品代码） -->\n"
          "<style>\n%s\n</style>\n")


def build_fixture(tab: str, path: str) -> str:
    """按夹具的既有入口生成一份 HTML（不截图）。"""
    cmd = [sys.executable, os.path.join(HERE, "render_ui_fixture.py"), path,
           "--tab", tab]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            cwd=ROOT, timeout=600)
    if result.returncode != 0 or not os.path.exists(path):
        raise SystemExit("夹具生成失败（%s）：\n%s\n%s"
                         % (tab, result.stdout, result.stderr))
    return path


def inject(src: str, dst: str, css: str) -> str:
    """把候选 CSS 插在 `</head>` 之前——它在夹具内联的 style.css **之后**，
    因此同优先级下后者胜；这正是「覆盖层」该有的位置关系。

    刻意不合并进原来那个 `<style>`：合并会让「哪些声明是候选的」变得不可读，
    而这张对照图的意义就在于差异可归因。
    """
    with open(src, "r", encoding="utf-8") as handle:
        html = handle.read()
    if "</head>" not in html:
        raise SystemExit("%s 里找不到 </head>，注入点失效" % src)
    with open(dst, "w", encoding="utf-8") as handle:
        handle.write(html.replace("</head>", INJECT % css + "</head>", 1))
    return dst


def main() -> int:
    parser = argparse.ArgumentParser(description="候选视觉系统对照图")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--tabs", default=",".join(DEFAULT_TABS),
                        help="要出图的页签，逗号分隔（默认全部八个）")
    parser.add_argument("--css", default=os.path.join("tools", "ui-preview",
                                                      "apple-tokens.css"),
                        help="候选样式表（相对仓库根）")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=1000)
    args = parser.parse_args()

    css_path = args.css if os.path.isabs(args.css) else os.path.join(ROOT, args.css)
    with open(css_path, "r", encoding="utf-8") as handle:
        css = handle.read()
    print("候选样式表 %s（%d 字节）" % (css_path, len(css)))

    os.makedirs(args.out, exist_ok=True)
    made = []
    for tab in [t.strip() for t in args.tabs.split(",") if t.strip()]:
        before_html = os.path.join(args.out, "before-%s.html" % tab)
        before_png = os.path.join(args.out, "before-%s.png" % tab)
        build_fixture(tab, before_html)
        shot = fixture.screenshot(before_html, before_png, args.width, args.height)
        if not shot:
            print("SKIP：本机没找到 Edge / Chrome，无法出图。")
            return 1

        after_html = os.path.join(args.out, "after-%s.html" % tab)
        inject(before_html, after_html, css)
        after_png = os.path.join(args.out, "after-%s.png" % tab)
        shot = fixture.screenshot(after_html, after_png, args.width, args.height)
        if not shot:
            print("SKIP：第二张图没出来。")
            return 1
        made.append(tab)
        print("  %-9s before=%s  after=%s" % (tab, os.path.basename(before_png),
                                              os.path.basename(after_png)))
    print("共 %d 个页签的对照图已写入 %s" % (len(made), os.path.abspath(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
