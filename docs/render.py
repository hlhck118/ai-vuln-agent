# -*- coding: utf-8 -*-
"""系统设计说明书 docx 渲染器：复用模板的节设置、页脚与版式规范。"""
import copy
import os
import re
import sys

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Emu, Pt, RGBColor

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, 'base.docx')

BODY_FONT = '仿宋_GB2312'
H1_FONT = '黑体'
H2_FONT = '楷体_GB2312'
H3_FONT = '仿宋_GB2312'
TITLE_FONT = '方正小标宋简体'
BODY_SIZE = 16.0        # 三号
TABLE_SIZE = 10.5       # 五号
LINE_TWIPS = 560        # 固定行距 28pt


def set_run_font(run, ea_font, size_pt, bold=False, ascii_font=None):
    ascii_font = ascii_font or ea_font
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.get_or_add_rFonts()
    rFonts.set(qn('w:ascii'), ascii_font)
    rFonts.set(qn('w:hAnsi'), ascii_font)
    rFonts.set(qn('w:eastAsia'), ea_font)
    rFonts.set(qn('w:cs'), ascii_font)
    rFonts.set(qn('w:hint'), 'eastAsia')
    run.font.size = Pt(size_pt)
    run.font.bold = bold


def set_para(p, line_twips=None, align=None, first_line_chars=0,
             space_before=0, space_after=0, keep_with_next=False):
    pPr = p._element.get_or_add_pPr()
    if line_twips:
        spacing = pPr.find(qn('w:spacing'))
        if spacing is None:
            spacing = OxmlElement('w:spacing')
            pPr.append(spacing)
        spacing.set(qn('w:line'), str(line_twips))
        spacing.set(qn('w:lineRule'), 'exact')
        spacing.set(qn('w:before'), str(space_before))
        spacing.set(qn('w:after'), str(space_after))
    if first_line_chars:
        ind = pPr.find(qn('w:ind'))
        if ind is None:
            ind = OxmlElement('w:ind')
            pPr.append(ind)
        ind.set(qn('w:firstLineChars'), str(first_line_chars))
        ind.set(qn('w:firstLine'), str(int(BODY_SIZE * 20 * first_line_chars / 100)))
    if align is not None:
        p.alignment = align
    if keep_with_next:
        kwn = OxmlElement('w:keepNext')
        pPr.append(kwn)
    return p


def zh_typo(s):
    """中文排版规范化：直引号转中文引号、半角标点转全角、中英文之间去空格。"""
    if not s:
        return s
    if '"' in s:
        if s.count('"') % 2:
            print('[warn] 引号不配对: %s' % s[:50])
        out, opening = [], True
        for ch in s:
            if ch == '"':
                out.append('“' if opening else '”')
                opening = not opening
            else:
                out.append(ch)
        s = ''.join(out)
    cjk = '\u4e00-\u9fff\u3001\u3002\u300a\u300b\u201c\u201d\uff0c\uff1b\uff1a\uff01\uff1f\u2014'
    word = 'A-Za-z0-9%\u00d7'
    s = re.sub(r'(?<=[%s\u201c\u201d]),' % cjk, '，', s)
    s = re.sub(r'(?<=[%s\u201c\u201d]);' % cjk, '；', s)
    s = re.sub(r'(?<=[%s\u201c\u201d]):' % cjk, '：', s)
    s = re.sub(r'(?<=[%s\u201c\u201d])!' % cjk, '！', s)
    s = re.sub(r'(?<=[%s\u201c\u201d])\?' % cjk, '？', s)
    s = re.sub(r'(?<=[%s])[ \t]+(?=[%s])' % (cjk, word), '', s)
    s = re.sub(r'(?<=[%s])[ \t]+(?=[%s])' % (word, cjk), '', s)
    s = re.sub(r'(?<=\u00d7)[ \t]+(?=[0-9A-Za-z])', '', s)
    s = re.sub(r'(?<=[0-9A-Za-z])[ \t]+(?=\u00d7)', '', s)
    return s


def add_markdown_para(doc, text, first_line_chars=200):
    """正文段落支持 **加粗** 内联标记（其余按普通正文排版）。"""
    p = doc.add_paragraph()
    set_para(p, line_twips=LINE_TWIPS, align=WD_ALIGN_PARAGRAPH.JUSTIFY,
             first_line_chars=first_line_chars)
    for index, chunk in enumerate(text.split('**')):
        if not chunk:
            continue
        run = p.add_run(zh_typo(chunk))
        set_run_font(run, BODY_FONT, BODY_SIZE, bold=(index % 2 == 1))
    return p


def add_para(doc, text, ea_font=BODY_FONT, size=BODY_SIZE, bold=False,
             align=None, first_line_chars=0, line_twips=LINE_TWIPS,
             ascii_font=None, before=0, after=0, keep_with_next=False,
             typo=False):
    p = doc.add_paragraph()
    set_para(p, line_twips=line_twips, align=align,
             first_line_chars=first_line_chars, space_before=before,
             space_after=after, keep_with_next=keep_with_next)
    if text:
        if typo:
            text = zh_typo(text)
        run = p.add_run(text)
        set_run_font(run, ea_font, size, bold=bold, ascii_font=ascii_font)
    return p


def add_rich_para(doc, segments, first_line_chars=0, align=None,
                  line_twips=LINE_TWIPS, size=BODY_SIZE, before=0, after=0):
    """segments: [(text, bold), ...] 全部使用正文字体。"""
    p = doc.add_paragraph()
    set_para(p, line_twips=line_twips, align=align,
             first_line_chars=first_line_chars, space_before=before,
             space_after=after)
    for text, bold in segments:
        run = p.add_run(text)
        set_run_font(run, BODY_FONT, size, bold=bold)
    return p


def set_cell_borders(tbl):
    tblPr = tbl._element.tblPr
    borders = OxmlElement('w:tblBorders')
    for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        el = OxmlElement('w:' + edge)
        el.set(qn('w:val'), 'single')
        el.set(qn('w:sz'), '6')
        el.set(qn('w:space'), '0')
        el.set(qn('w:color'), '808080')
        borders.append(el)
    tblPr.append(borders)


def shade_cell(cell, fill):
    tcPr = cell._element.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), fill)
    tcPr.append(shd)


def set_repeat_header(row):
    trPr = row._element.get_or_add_trPr()
    el = OxmlElement('w:tblHeader')
    el.set(qn('w:val'), 'true')
    trPr.append(el)


def set_table_grid(tbl, widths_twips):
    tblPr = tbl._element.tblPr
    tblW = tblPr.find(qn('w:tblW'))
    if tblW is None:
        tblW = OxmlElement('w:tblW')
        tblPr.append(tblW)
    tblW.set(qn('w:type'), 'dxa')
    tblW.set(qn('w:w'), str(sum(widths_twips)))
    grid = tbl._element.find(qn('w:tblGrid'))
    for gc, w in zip(grid.findall(qn('w:gridCol')), widths_twips):
        gc.set(qn('w:w'), str(w))
    for row in tbl.rows:
        for cell, w in zip(row.cells, widths_twips):
            cell.width = Emu(int(w * 635))  # 1 twip = 635 EMU


def add_table(doc, header, rows, widths=None, size=TABLE_SIZE,
              header_fill='DCE6F1', align_center_cols=None):
    ncols = len(header)
    tbl = doc.add_table(rows=1 + len(rows), cols=ncols)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl.autofit = False
    set_cell_borders(tbl)
    align_center_cols = align_center_cols or []
    usable_twips = 11906 - 1797 - 1106
    if not widths:
        widths = [1] * ncols
    total = float(sum(widths))
    widths_twips = [int(usable_twips * w / total) for w in widths]
    widths_twips[-1] = usable_twips - sum(widths_twips[:-1])

    def fill_row(cells, values, bold, is_header):
        for idx, (cell, value) in enumerate(zip(cells, values)):
            cell.vertical_alignment = 1  # center
            p = cell.paragraphs[0]
            center = is_header or idx in align_center_cols
            set_para(p, line_twips=int(size * 20 * 1.35),
                     align=WD_ALIGN_PARAGRAPH.CENTER if center else WD_ALIGN_PARAGRAPH.LEFT)
            for chunk_index, chunk in enumerate(zh_typo(str(value)).split('**')):
                if not chunk:
                    continue
                run = p.add_run(chunk)
                set_run_font(run, H1_FONT if is_header else BODY_FONT, size,
                             bold=bold or chunk_index % 2 == 1)
            if is_header:
                shade_cell(cell, header_fill)

    fill_row(tbl.rows[0].cells, header, True, True)
    set_repeat_header(tbl.rows[0])
    for r_i, row in enumerate(rows, start=1):
        fill_row(tbl.rows[r_i].cells, row, False, False)

    set_table_grid(tbl, widths_twips)
    return tbl


def build(blocks, out_path):
    doc = Document(TEMPLATE)
    body = doc.element.body
    sectPr = body.find(qn('w:sectPr'))
    for child in list(body):
        if child is not sectPr:
            body.remove(child)

    # 统一 Normal 样式为公文正文（标题/表格均显式设置字体，不依赖继承）
    normal = doc.styles['Normal']
    normal.font.size = Pt(BODY_SIZE)
    normal.font.name = BODY_FONT
    rpr = normal.element.get_or_add_rPr()
    rf = rpr.get_or_add_rFonts()
    rf.set(qn('w:ascii'), BODY_FONT)
    rf.set(qn('w:hAnsi'), BODY_FONT)
    rf.set(qn('w:eastAsia'), BODY_FONT)
    rf.set(qn('w:cs'), BODY_FONT)
    normal.paragraph_format.line_spacing = Pt(28)
    normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)

    # 页脚页码保持小五宋体，避免随 Normal 放大
    for section in doc.sections:
        for p in section.footer.paragraphs:
            for run in p.runs:
                set_run_font(run, '宋体', 10.5)

    for block in blocks:
        kind = block[0]
        if kind == 'title':
            add_para(doc, block[1], TITLE_FONT, 22.0, align=WD_ALIGN_PARAGRAPH.CENTER,
                     line_twips=560)
        elif kind == 'subtitle':
            add_para(doc, block[1], BODY_FONT, 16.0, align=WD_ALIGN_PARAGRAPH.CENTER,
                     line_twips=560)
        elif kind == 'blank':
            add_para(doc, '', BODY_FONT, 16.0, line_twips=560)
        elif kind == 'h1':
            add_para(doc, block[1], H1_FONT, BODY_SIZE, line_twips=LINE_TWIPS,
                     before=240, after=120, keep_with_next=True)
        elif kind == 'h2':
            add_para(doc, block[1], H2_FONT, BODY_SIZE, bold=True,
                     line_twips=LINE_TWIPS, before=120, after=60, keep_with_next=True)
        elif kind == 'h3':
            add_para(doc, block[1], H3_FONT, BODY_SIZE, bold=True,
                     line_twips=LINE_TWIPS, first_line_chars=200,
                     before=60, keep_with_next=True)
        elif kind == 'p':
            if '**' in block[1]:
                add_markdown_para(doc, block[1])
            else:
                add_para(doc, block[1], BODY_FONT, BODY_SIZE,
                         first_line_chars=200, align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                         typo=True)
        elif kind == 'rich':
            add_rich_para(doc, block[1], first_line_chars=200,
                          align=WD_ALIGN_PARAGRAPH.JUSTIFY)
        elif kind == 'caption':
            add_para(doc, block[1], H1_FONT, 12.0, align=WD_ALIGN_PARAGRAPH.CENTER,
                     line_twips=360, before=120, after=60, keep_with_next=True)
        elif kind == 'table':
            add_table(doc, block[1], block[2],
                      widths=block[3] if len(block) > 3 else None)
            add_para(doc, '', BODY_FONT, 6.0, line_twips=120)
        elif kind == 'note':
            add_para(doc, block[1], H3_FONT, 12.0, line_twips=360,
                     first_line_chars=200, align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                     typo=True)
        elif kind == 'ref':
            add_para(doc, block[1], BODY_FONT, 10.5, line_twips=300,
                     align=WD_ALIGN_PARAGRAPH.LEFT, ascii_font='Times New Roman',
                     before=0, after=40, typo=True)
        elif kind == 'code':
            add_para(doc, block[1], BODY_FONT, 10.5, line_twips=260,
                     align=WD_ALIGN_PARAGRAPH.LEFT, ascii_font='Consolas',
                     before=0, after=60)
        elif kind == 'pagebreak':
            doc.add_page_break()
        else:
            raise ValueError('unknown block: %r' % (kind,))

    doc.save(out_path)
    print('saved:', out_path)


if __name__ == '__main__':
    import d1, d2, d3, d4, d5
    blocks = d1.BLOCKS + d2.BLOCKS + d3.BLOCKS + d4.BLOCKS + d5.BLOCKS
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'design.docx')
    build(blocks, out)
