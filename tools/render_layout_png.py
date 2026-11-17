# -*- coding: utf-8 -*-
"""把服务端布局渲染成 PNG 预览（开发/评审用）。

为什么要这个脚本：布局是纯函数输出，单元测试能验证"分层正确、不重叠、边不悬空"，
但**看上去是否合理**仍需人眼确认。把同一份布局画成位图，就不必为了看一眼而
起浏览器、截图、再贴回来。

用法：
    python tools/render_layout_png.py <db> <engagement-id> <out.png>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFont          # noqa: E402

from pentestd import graph                           # noqa: E402
from pentestd.store import Store                     # noqa: E402

# 字体位置从系统环境里取，不写盘符：Windows 装在任何盘都能找到，Linux/macOS 走
# 常见路径。`LAYOUT_FONT` 可以显式指定一份（找不到时自动往下走）。
FONT_FILES = ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc",
              "NotoSansCJK-Regular.ttc")
POSIX_FONTS = ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
               "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
               "/System/Library/Fonts/PingFang.ttc")


def font_candidates():
    """候选字体文件的绝对路径：目录一律来自环境变量，不写死盘符。"""
    out = [os.environ.get("LAYOUT_FONT") or ""]
    dirs = []
    windows = os.environ.get("WINDIR") or ""
    if windows:
        dirs.append(os.path.join(windows, "Fonts"))
    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    for base in dirs:
        for name in FONT_FILES:
            out.append(os.path.join(base, name))
    out.extend(POSIX_FONTS)
    return [path for path in out if path]


FONT_CANDIDATES = font_candidates()
BACKDROP = (18, 22, 31)
HEADER = (200, 214, 232)


def _font(size: int):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render(db_path: str, engagement_id: str, out_path: str) -> str:
    store = Store(db_path)
    try:
        view = store.view(engagement_id)
        nodes = ([view["goal"]] if view["goal"] else [])
        for key in ("intents", "facts", "findings", "assets"):
            nodes.extend(view["nodes"].get(key, []))
        layout = graph.build_layout(nodes, view["edges"])
    finally:
        store.close()

    title = _font(18)
    label_font = _font(13)
    small_font = _font(10)
    image = Image.new("RGB", (layout["width"], layout["height"] + 46), BACKDROP)
    draw = ImageDraw.Draw(image)
    draw.text((40, 14), "渗透测试探索链路预览", font=title, fill=HEADER)

    offset_y = 46
    positions = {n["id"]: n for n in layout["nodes"]}
    for edge in layout["edges"]:
        source, target = positions.get(edge["source"]), positions.get(edge["target"])
        if not source or not target:
            continue
        start = (source["x"] + source["w"], source["y"] + offset_y + source["h"] / 2)
        end = (target["x"], target["y"] + offset_y + target["h"] / 2)
        if end[0] < start[0]:
            start = (source["x"] + source["w"] / 2, source["y"] + offset_y + source["h"])
            end = (target["x"] + target["w"] / 2, target["y"] + offset_y + target["h"])
        mid_x = (start[0] + end[0]) / 2
        draw.line([start, (mid_x, start[1]), (mid_x, end[1]), end],
                  fill=(107, 124, 147), width=2)
        box = draw.textbbox((0, 0), edge.get("label", ""), font=small_font)
        draw.text((mid_x - (box[2] - box[0]) / 2, (start[1] + end[1]) / 2 - 14 + offset_y),
                  edge.get("label", ""), font=small_font, fill=(143, 163, 191))

    for node in layout["nodes"]:
        fill, stroke = graph.KIND_COLORS.get(node["kind"], ("#1b2230", "#5b6b82"))
        if node["kind"] == "finding" and node.get("severity"):
            stroke = graph.SEVERITY_COLORS.get(node["severity"], stroke)
        box = [node["x"], node["y"] + offset_y,
               node["x"] + node["w"], node["y"] + offset_y + node["h"]]
        draw.rounded_rectangle(box, radius=8, fill=fill, outline=stroke, width=2)
        draw.text((box[0] + 12, box[1] + 8), (node.get("label") or "")[:22],
                  font=label_font, fill=(230, 237, 247))
        subtitle = (node.get("subtitle") or "")[:32]
        if subtitle:
            draw.text((box[0] + 12, box[1] + 30), subtitle,
                      font=small_font, fill=(159, 176, 200))
        draw.text((box[2] - 60, box[1] + 6), node["id"],
                  font=small_font, fill=(124, 139, 161))

    image.save(out_path)
    return "%s  (%dx%d, %d 节点 / %d 边)" % (out_path, image.width, image.height,
                                             len(layout["nodes"]), len(layout["edges"]))


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    print(render(sys.argv[1], sys.argv[2], sys.argv[3]))
