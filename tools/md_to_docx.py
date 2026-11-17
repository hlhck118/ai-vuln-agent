# -*- coding: utf-8 -*-
"""把项目里那几份 Markdown 说明转成 Word（.docx）。

为什么不用 pandoc：这台机器上没有 pandoc，而且两份文档的版式要求是"中文公文式"
（黑体章标题、楷体节标题、仿宋正文、Consolas 代码块），pandoc 的 reference-doc
也要先做一份；直接写 20 行样式比维护 reference-doc 更容易看清哪里改了。

支持的范围就是这两份文档实际用到的语法：标题（#..####）、段落、* / - 列表、
有序列表、表格、围栏代码、引用块、分隔线、行内 **粗体**、`代码`、*斜体*、
[链接](url)。遇到不认识的写法按普通段落输出，不静默丢字。

用法：
    python -X utf8 tools/md_to_docx.py --in 使用手册.md --out 使用手册.docx \
        --profile guide
    python -X utf8 tools/md_to_docx.py --in 设计说明书.md --out 设计说明书.docx \
        --profile design --cover

`--cover` 读文件开头的 `<!-- cover -->` 段（见 parse_cover），生成封面页后分页。
"""
import argparse
import os
import re
import sys

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

BODY_FONT = "仿宋_GB2312"
HEI = "黑体"
KAI = "楷体_GB2312"
CODE_FONT = "Consolas"
SONG = "宋体"

PROFILES = {
    # 设计说明书：沿用原先那份文档的版式（16pt 正文，三号仿宋）
    "design": {
        "body": 16, "h1": 16, "h2": 16, "h3": 16, "h4": 16,
        "code": 10.5, "table": 10.5, "quote": 15, "line": 28,
        "indent": 32, "h1_font": HEI, "h2_font": KAI, "h3_font": BODY_FONT,
    },
    # 使用说明：正文 12pt（小四），读起来不像公文，页数也可控
    "guide": {
        "body": 12, "h1": 18, "h2": 15, "h3": 12.5, "h4": 12,
        "code": 9.5, "table": 10, "quote": 11, "line": 22,
        "indent": 24, "h1_font": HEI, "h2_font": KAI, "h3_font": BODY_FONT,
    },
}


# ---------------------------------------------------------------- 文本与字体

def set_run_font(run, name, size=None, bold=None, italic=None, color=None):
    run.font.name = name
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attr), name)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    if color is not None:
        run.font.color.rgb = RGBColor.from_string(color)


def shade_run(run, fill="F2F2F2"):
    rpr = run._element.get_or_add_rPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    rpr.append(shd)


def shade_paragraph(paragraph, fill="F7F7F7"):
    ppr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    ppr.append(shd)


def border_left(paragraph, color="C9C9C9", size=18):
    ppr = paragraph._p.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    left = OxmlElement("w:left")
    left.set(qn("w:val"), "single")
    left.set(qn("w:sz"), str(size))
    left.set(qn("w:space"), "8")
    left.set(qn("w:color"), color)
    pbdr.append(left)
    ppr.append(pbdr)


def border_bottom(paragraph, color="BFBFBF", size=8):
    ppr = paragraph._p.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), color)
    pbdr.append(bottom)
    ppr.append(pbdr)


def keep_with_next(paragraph, on=True):
    ppr = paragraph._p.get_or_add_pPr()
    el = OxmlElement("w:keepNext")
    if not on:
        el.set(qn("w:val"), "0")
    ppr.append(el)


def add_page_number_footer(section):
    footer = section.footer
    paragraph = footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run()
    set_run_font(run, SONG, 9)
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_end)


# ---------------------------------------------------------------- 行内格式

INLINE = re.compile(
    r"(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*]+\*)|(\[[^\]]+\]\([^)]+\))"
)


def add_inline(paragraph, text, size, base_font, color=None, bold=False):
    """把一行 markdown 行内语法铺成 runs。"""
    pos = 0
    for match in INLINE.finditer(text):
        if match.start() > pos:
            run = paragraph.add_run(text[pos:match.start()])
            set_run_font(run, base_font, size, bold=bold, color=color)
        token = match.group(0)
        if token.startswith("`"):
            run = paragraph.add_run(token[1:-1])
            set_run_font(run, CODE_FONT, max(size - 0.5, 8), bold=bold, color="9C2A2A")
            shade_run(run)
        elif token.startswith("**"):
            run = paragraph.add_run(token[2:-2])
            set_run_font(run, base_font, size, bold=True, color=color)
        elif token.startswith("["):
            label, url = re.match(r"\[([^\]]+)\]\(([^)]+)\)", token).groups()
            run = paragraph.add_run(label)
            set_run_font(run, base_font, size, bold=bold, color="1F4E79")
            run = paragraph.add_run("（%s）" % url)
            set_run_font(run, base_font, max(size - 1, 8), color="7F7F7F")
        else:  # 斜体
            run = paragraph.add_run(token[1:-1])
            set_run_font(run, base_font, size, bold=bold, italic=True, color=color)
        pos = match.end()
    if pos < len(text):
        run = paragraph.add_run(text[pos:])
        set_run_font(run, base_font, size, bold=bold, color=color)


# ---------------------------------------------------------------- 块级解析

TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    # 只按未转义的竖线切
    cells, buf, esc = [], [], False
    for ch in line:
        if esc:
            buf.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == "|":
            cells.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    cells.append("".join(buf).strip())
    return cells


def is_table_start(lines, i):
    if i + 1 >= len(lines):
        return False
    if "|" not in lines[i]:
        return False
    return bool(TABLE_SEP.match(lines[i + 1])) and "|" in lines[i + 1]


def parse_cover(text):
    """文件顶部的 <!-- cover --> ... <!-- /cover --> 段：每行一个元素。

    行首标记决定版式：`# ` 主标题，`## ` 副标题，`@` 空行，其余为说明行。
    """
    match = re.search(r"<!--\s*cover\s*-->(.*?)<!--\s*/cover\s*-->", text, re.S)
    if not match:
        return None, text
    body = text[:match.start()] + text[match.end():]
    return [line.rstrip() for line in match.group(1).strip("\n").splitlines()], body


def build(lines, profile, cover, out_path):
    conf = PROFILES[profile]
    doc = Document()
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(3.0)
    section.right_margin = Cm(3.0)
    add_page_number_footer(section)

    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(conf["body"])
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)

    if cover:
        for line in cover:
            if not line.strip():
                continue
            if line.startswith("## "):
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.space_before = Pt(18)
                add_inline(p, line[3:], conf["h2"], KAI, color="333333")
            elif line.startswith("# "):
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.space_before = Pt(150)
                p.paragraph_format.space_after = Pt(36)
                add_inline(p, line[2:], 30, "方正小标宋简体", color="000000")
            elif line.startswith("@"):
                doc.add_paragraph()
            else:
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.space_before = Pt(6)
                add_inline(p, line, conf["h2"], BODY_FONT, color="333333")
        doc.add_page_break()

    i = 0
    ol_counter = 0
    ordered_active = False
    n = len(lines)
    while i < n:
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 围栏代码（允许前面有列表缩进）
        fence = re.match(r"^\s*```\s*([A-Za-z0-9_+-]*)\s*$", line)
        if fence:
            lang = fence.group(1)
            i += 1
            block = []
            while i < n and not re.match(r"^\s*```\s*$", lines[i]):
                block.append(lines[i].rstrip("\n"))
                i += 1
            i += 1
            p = doc.add_paragraph()
            pf = p.paragraph_format
            pf.space_before = Pt(4)
            pf.space_after = Pt(8)
            pf.left_indent = Cm(0.5)
            pf.first_line_indent = Pt(0)
            pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
            shade_paragraph(p, "F7F7F7")
            border_left(p)
            if not block:
                block = [""]
            for k, code_line in enumerate(block):
                if k:
                    p.add_run().add_break()
                run = p.add_run(code_line.replace("\t", "    "))
                set_run_font(run, CODE_FONT, conf["code"], color="1B1B1B")
            i += 1
            ordered_active = False
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            p = doc.add_paragraph()
            pf = p.paragraph_format
            pf.first_line_indent = Pt(0)
            pf.space_before = Pt(14 if level <= 2 else 10)
            pf.space_after = Pt(6 if level <= 2 else 4)
            pf.line_spacing = 1.4
            keep_with_next(p)
            if level == 1:
                size, font, color = conf["h1"] + 4, conf["h1_font"], "000000"
            elif level == 2:
                size, font, color = conf["h1"], conf["h1_font"], "000000"
            elif level == 3:
                size, font, color = conf["h2"], conf["h2_font"], "222222"
                p.runs and None
            else:
                size, font, color = conf["h3"], conf["h3_font"], "222222"
            add_inline(p, text, size, font, color=color,
                       bold=(level >= 3))
            i += 1
            ordered_active = False
            continue

        # 分隔线
        if re.match(r"^-{3,}\s*$", stripped):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(8)
            p.paragraph_format.first_line_indent = Pt(0)
            border_bottom(p)
            i += 1
            continue

        # 表格
        if is_table_start(lines, i):
            header = split_row(lines[i])
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            cols = max([len(header)] + [len(r) for r in rows])
            table = doc.add_table(rows=1, cols=cols)
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            table.autofit = True
            for c in range(cols):
                cell = table.rows[0].cells[c]
                cell.text = ""
                p = cell.paragraphs[0]
                p.paragraph_format.space_before = Pt(2)
                p.paragraph_format.space_after = Pt(2)
                add_inline(p, header[c] if c < len(header) else "",
                           conf["table"], SONG, bold=True)
                tcpr = cell._tc.get_or_add_tcPr()
                shd = OxmlElement("w:shd")
                shd.set(qn("w:val"), "clear")
                shd.set(qn("w:color"), "auto")
                shd.set(qn("w:fill"), "EDF1F7")
                tcpr.append(shd)
            for row in rows:
                cells = table.add_row().cells
                for c in range(cols):
                    p = cells[c].paragraphs[0]
                    p.paragraph_format.space_before = Pt(1)
                    p.paragraph_format.space_after = Pt(1)
                    add_inline(p, row[c] if c < len(row) else "",
                               conf["table"], SONG)
            spacer = doc.add_paragraph()
            spacer.paragraph_format.space_after = Pt(4)
            spacer.paragraph_format.first_line_indent = Pt(0)
            i += 1
            ordered_active = False
            continue

        # 引用块
        if stripped.startswith(">"):
            block = []
            while i < n and lines[i].strip().startswith(">"):
                block.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            while block and not block[-1].strip():
                block.pop()
            p = doc.add_paragraph()
            pf = p.paragraph_format
            pf.left_indent = Cm(0.75)
            pf.right_indent = Cm(0.4)
            pf.first_line_indent = Pt(0)
            pf.space_before = Pt(4)
            pf.space_after = Pt(8)
            pf.line_spacing = 1.35
            border_left(p, "B9C6DA")
            for k, qline in enumerate(block):
                if k:
                    p.add_run().add_break()
                add_inline(p, qline.strip(), conf["quote"], KAI, color="404040")
            i += 1
            continue

        # 列表
        bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        ordered = re.match(r"^(\s*)(\d+)[.)]\s+(.*)$", line)
        if bullet or ordered:
            if ordered and not ordered_active:
                ol_counter = 0
                ordered_active = True
            if bullet:
                ordered_active = False
            while True:
                if ordered:
                    ol_counter += 1
                    prefix = "%d. " % ol_counter
                    text = ordered.group(3)
                else:
                    prefix = "· "
                    text = bullet.group(2)
                # 续行：缩进且不是块级元素
                i += 1
                while i < n:
                    nxt = lines[i]
                    if not nxt.strip():
                        break
                    if re.match(r"^\s{2,}\S", nxt) and not re.match(
                            r"^\s*(-{3,}|\||>|```|#{1,4}\s|\s*[-*+]\s|\s*\d+[.)]\s)",
                            nxt):
                        text += nxt.strip()
                        i += 1
                    else:
                        break
                p = doc.add_paragraph()
                pf = p.paragraph_format
                pf.left_indent = Cm(0.75)
                pf.first_line_indent = Cm(-0.45)
                pf.space_before = Pt(1)
                pf.space_after = Pt(2)
                pf.line_spacing = 1.4
                run = p.add_run(prefix)
                set_run_font(run, BODY_FONT, conf["body"], bold=ordered is not None)
                add_inline(p, text, conf["body"], BODY_FONT)
                # 同一列表的下一条
                if i < n:
                    bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", lines[i])
                    ordered = re.match(r"^(\s*)(\d+)[.)]\s+(.*)$", lines[i])
                    if bullet or ordered:
                        continue
                break
            continue

        # 普通段落：连到下一个空行或块边界
        buf = [stripped]
        i += 1
        while i < n:
            nxt = lines[i]
            s = nxt.strip()
            if not s:
                break
            if (s.startswith(">") or s.startswith("|") or s.startswith("#")
                    or s.startswith("```") or re.match(r"^-{3,}$", s)
                    or re.match(r"^[-*+]\s+", s) or re.match(r"^\d+[.)]\s+", s)):
                break
            buf.append(s)
            i += 1
        text = "".join(buf)
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.first_line_indent = Pt(conf["indent"])
        pf.line_spacing = 1.5
        pf.space_before = Pt(2)
        pf.space_after = Pt(2)
        add_inline(p, text, conf["body"], BODY_FONT)
        ordered_active = False

    doc.save(out_path)
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    ap.add_argument("--profile", choices=sorted(PROFILES), default="guide")
    ap.add_argument("--cover", action="store_true")
    args = ap.parse_args(argv)

    with open(args.src, encoding="utf-8") as handle:
        text = handle.read()
    cover = None
    if args.cover:
        cover, text = parse_cover(text)
    build(text.splitlines(), args.profile, cover, args.dst)
    size = os.path.getsize(args.dst)
    print("wrote %s (%d bytes)" % (args.dst, size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
