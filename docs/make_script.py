# -*- coding: utf-8 -*-
"""生成《成果发布-8分钟》配套讲解演示稿 docx。"""
import os, sys
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

EA = "微软雅黑"
EA_BODY = "宋体"
INK = (0x1F, 0x2A, 0x37)
DIM = (0x5A, 0x6B, 0x7C)
ACC = (0xB0, 0x4A, 0x3C)


def font(run, size, bold=False, color=INK, ea=EA, ascii_name="Segoe UI"):
    run.font.name = ascii_name
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = RGBColor(*color)
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:eastAsia"), ea)
    rFonts.set(qn("w:ascii"), ascii_name)
    rFonts.set(qn("w:hAnsi"), ascii_name)


def para(doc, text="", size=10.5, bold=False, color=INK, ea=EA_BODY,
         space_before=0, space_after=4, line=1.5, align=None, indent=0.0):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(space_before)
    pf.space_after = Pt(space_after)
    pf.line_spacing = line
    if indent:
        pf.first_line_indent = Cm(indent)
    if align == "center":
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif align == "right":
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    if text:
        r = p.add_run(text)
        font(r, size, bold, color, ea)
    return p


def h1(doc, text):
    para(doc, text, size=15, bold=True, color=INK, ea=EA,
         space_before=18, space_after=8, line=1.2)
    rule(doc)


def h2(doc, text, tag=None):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = 12
    pf.space_after = 3
    pf.line_spacing = 1.2
    if tag:
        r = p.add_run(tag + "　")
        font(r, 10, True, ACC, EA)
    r = p.add_run(text)
    font(r, 12, True, INK, EA)


def rule(doc):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(8)
    pPr = p._element.get_or_add_pPr()
    bd = OxmlElement("w:pBdr")
    bot = OxmlElement("w:bottom")
    bot.set(qn("w:val"), "single")
    bot.set(qn("w:sz"), "6")
    bot.set(qn("w:color"), "C8D0D8")
    bd.append(bot)
    pPr.append(bd)


def table(doc, head_cells, rows, widths):
    t = doc.add_table(rows=1, cols=len(head_cells))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.LEFT
    t.autofit = False
    hdr = t.rows[0].cells
    for i, c in enumerate(head_cells):
        hdr[i].text = ""
        r = hdr[i].paragraphs[0].add_run(c)
        font(r, 9, True, (0xFF, 0xFF, 0xFF), EA)
        hdr[i].paragraphs[0].paragraph_format.space_after = Pt(1)
        sh = OxmlElement("w:shd")
        sh.set(qn("w:fill"), "3E5C76")
        hdr[i]._tc.get_or_add_tcPr().append(sh)
    for row in rows:
        cells = t.add_row().cells
        for i, c in enumerate(row):
            cells[i].text = ""
            p = cells[i].paragraphs[0]
            p.paragraph_format.space_after = Pt(1)
            p.paragraph_format.line_spacing = 1.15
            r = p.add_run(str(c))
            font(r, 9, False, INK, EA_BODY)
    for row in t.rows:
        for i, w in enumerate(widths):
            row.cells[i].width = Cm(w)
    return t
