# -*- coding: utf-8 -*-
import sys
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from PIL import ImageFont
E = 914400.0
F_ = ImageFont.truetype('C:/Windows/Fonts/NotoSansSC-VF.ttf', 100)
def w_in(t, pt, bold=False):
    return F_.getlength(t) * pt / 100.0 / 72.0 * (1.02 if bold else 1.0)
INK   = RGBColor(0x1F,0x2A,0x37)
SUB   = RGBColor(0x5B,0x6B,0x7C)
MAIN  = RGBColor(0x3E,0x5C,0x76)
ACC   = RGBColor(0xC0,0x56,0x4A)
LIGHT = RGBColor(0x8F,0xA3,0xB8)
PALE  = RGBColor(0xC9,0xD4,0xE0)
SOFT  = RGBColor(0xEC,0xF1,0xF5)
WHITE = RGBColor(0xFF,0xFF,0xFF)
SRC, DST = sys.argv[1], sys.argv[2]
APPLY = '--apply' in sys.argv
prs = Presentation(SRC)
log = []
kept = []

def tb(s, x, y, w, h, anchor=MSO_ANCHOR.TOP):
    b = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = b.text_frame
    tf.word_wrap = True
    tf.auto_size = None
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    return b

def put(tf, text, size, bold=False, color=INK, first=False, align=PP_ALIGN.LEFT):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.alignment = align
    r = p.add_run()
    r.text = text
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.color.rgb = color
    r.font.name = 'Noto Sans SC'
    return p

def box(s, x, y, w, h, fill, line=None):
    shp = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shp.fill.solid()
    shp.fill.fore_color.rgb = fill
    if line is None:
        shp.line.fill.background()
    else:
        shp.line.color.rgb = line
        shp.line.width = Pt(0.75)
    shp.shadow.inherit = False
    try:
        shp.adjustments[0] = 0.10
    except Exception:
        pass
    return shp

def arrow(s, x, y, w, h, fill, kind):
    shp = s.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    shp.fill.solid()
    shp.fill.fore_color.rgb = fill
    shp.line.fill.background()
    shp.shadow.inherit = False
    try:
        shp.adjustments[0] = 0.45
        shp.adjustments[1] = 0.50
    except Exception:
        pass
    return shp

# A. 第 11 页：矢量重画双向闭环
for sh in list(prs.slides[10].shapes):
    if sh.shape_id == 664:
        log.append('p11 删除位图 664（中文渲染为方框）')
        if APPLY:
            sh._element.getparent().remove(sh._element)
if APPLY:
    s = prs.slides[10]
    gapL, gapR = 5.99, 11.79          # 左卡右沿 5.89 ~ 右卡左沿 11.89 之间的空隙
    gw = gapR - gapL
    arrow(s, gapL, 2.62, gw, 0.30, ACC, MSO_SHAPE.RIGHT_ARROW)
    arrow(s, gapL, 5.06, gw, 0.30, MAIN, MSO_SHAPE.LEFT_ARROW)
    t = tb(s, gapL, 2.26, gw, 0.30)
    put(t.text_frame, '抽象假设 → 可执行任务', 12, True, ACC, first=True, align=PP_ALIGN.CENTER)
    t = tb(s, gapL, 3.06, gw, 0.30)
    put(t.text_frame, '定向模糊测试 · 符号执行 · 定向污点', 10.5, False, SUB, first=True, align=PP_ALIGN.CENTER)
    t = tb(s, gapL, 5.44, gw, 0.30)
    put(t.text_frame, '执行事实 → 回灌下一轮', 12, True, MAIN, first=True, align=PP_ALIGN.CENTER)
    t = tb(s, gapL, 5.80, gw, 0.30)
    put(t.text_frame, '覆盖率增量 · 崩溃样本 · 路径结果', 10.5, False, SUB, first=True, align=PP_ALIGN.CENTER)
    mid = box(s, gapL + 1.30, 3.52, gw - 2.60, 1.02, WHITE, PALE)
    mid.text_frame.word_wrap = True
    mid.text_frame.margin_left = mid.text_frame.margin_right = 0
    mid.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    put(mid.text_frame, '双向反馈闭环', 11.5, True, INK, first=True, align=PP_ALIGN.CENTER)
    put(mid.text_frame, '假设 → 任务 → 事实 → 新假设', 10, False, LIGHT, align=PP_ALIGN.CENTER)
    log.append('p11 已用矢量重画：双向大箭头 + 中央闭环卡（替代缺字体的位图）')
# B. 第 12 页：红底红字改白
for sh in prs.slides[11].shapes:
    if sh.shape_id in (741, 746, 789) and sh.has_text_frame:
        for pa in sh.text_frame.paragraphs:
            for r in pa.runs:
                r.font.color.rgb = WHITE
        log.append('p12 id=%d 改白：%s' % (sh.shape_id, sh.text_frame.text[:18]))

# C. 第 13 页：删离画布竖条
for sh in list(prs.slides[12].shapes):
    if sh.shape_id == 836:
        log.append('p13 删除越界竖条 836')
        if APPLY:
            sh._element.getparent().remove(sh._element)

# D. 全局收边
RIGHT = 17.20
TITLE_RIGHT = 15.50
def profile(sh):
    widest = 0.0
    top_pt = 0.0
    top_bold = False
    for pa in sh.text_frame.paragraphs:
        t = ''.join(r.text for r in pa.runs)
        if not t.strip():
            continue
        sz = max([r.font.size.pt for r in pa.runs if r.font.size] or [14])
        bd = any(r.font.bold for r in pa.runs)
        widest = max(widest, w_in(t, sz, bd))
        if sz > top_pt:
            top_pt, top_bold = sz, bd
    return widest, top_pt, top_bold
for si, s in enumerate(prs.slides, 1):
    for sh in s.shapes:
        if not sh.has_text_frame:
            continue
        txt = sh.text_frame.text.strip()
        if not txt or txt.startswith('SECTION'):
            continue
        Lw = (sh.left or 0) / E
        W = (sh.width or 0) / E
        if Lw + W <= RIGHT:
            continue
        need, pt, bold = profile(sh)
        is_title = pt >= 30 and (sh.top or 0) / E < 1.05
        limit = TITLE_RIGHT if is_title else RIGHT
        room = limit - Lw
        if room < 0.60:
            kept.append('  p%-2d id=%-5d 右侧无空间，跳过' % (si, sh.shape_id))
            continue
        neww = min(W, room)
        if neww + 1e-9 < need:
            scale = max(0.86, neww / need)
            newpt = round(pt * scale * 2) / 2.0
            if need * scale > neww:
                kept.append('  p%-2d id=%-5d %.1fpt 收窄会换行(need %.2f > room %.2f)，保持原宽' % (si, sh.shape_id, pt, need, neww))
                continue
            for pa in sh.text_frame.paragraphs:
                for r in pa.runs:
                    if r.font.size:
                        r.font.size = Pt(newpt)
            log.append('  p%-2d id=%-5d 宽 %.2f→%.2f + 字号 %.1f→%.1f' % (si, sh.shape_id, W, neww, pt, newpt))
        else:
            log.append('  p%-2d id=%-5d 宽 %.2f→%.2f (need %.2f)' % (si, sh.shape_id, W, neww, need))
        if APPLY:
            sh.width = Inches(neww)

# E. 第 8 页 pill：统一 12pt，保证单行不折
#    原来 13pt 在 PowerPoint 实测渲染下比 PIL 宽约 6~8%，orchestrator.py / incremental.py / agents.py /
#    analysis.py / resolver.py / memory.py 六个胶囊都被折成两行（半个 "py" 掉到下一行）。
#    PIL 按 12pt + 15% 余量复算，十个胶囊全部放得下，故整排统一 12pt（只缩这一层，不动其余文字）。
PILL_PT = 12.0
for sh in prs.slides[7].shapes:
    if sh.has_text_frame and 'FREEFORM' in str(sh.shape_type or ''):
        t = sh.text_frame.text.strip()
        if not t:
            continue
        fs = [r.font.size.pt for pa in sh.text_frame.paragraphs for r in pa.runs if r.font.size]
        if not fs:
            continue
        pt = max(fs)
        avail = (sh.width or 0) / E - 0.16
        newpt = PILL_PT
        while newpt > 8.0 and w_in(t, newpt) * 1.15 > avail:
            newpt -= 0.5
        if newpt != pt:
            for pa in sh.text_frame.paragraphs:
                for r in pa.runs:
                    if r.font.size:
                        r.font.size = Pt(newpt)
            log.append('  p8 pill %-16s %.1fpt→%.1fpt' % (t, pt, newpt))
# F. 第 4 页中列（02 工具现状局限）三块指标堆叠越出卡片下沿
#    卡片 id=139 覆盖 y 1.25~7.08；三块灰底原为 2.38/4.30/6.22 各高 1.71，
#    末块下沿跑到 7.93，压到 7.36 起的深色结论带。改为每块 1.44 高、间距 0.16，
#    整体 2.40→7.04，全部回到卡片内；块内三行同步上移，行距不变。
#    顺带把四列的文本框右沿从 13.08 收到卡片内 11.21，去掉看不见的探出部分。
P4_COL = 3
P4_Y = {
    144: 2.40, 145: 2.60, 146: 2.86, 147: 3.42,
    148: 4.00, 149: 4.20, 150: 4.46, 151: 5.02,
    152: 5.60, 153: 5.80, 154: 6.06, 155: 6.62,
}
P4_BLOCK = {144: 1.44, 148: 1.44, 152: 1.44}
for sh in prs.slides[P4_COL].shapes:
    if sh.shape_id in P4_Y:
        old = (sh.top or 0) / E
        if APPLY:
            sh.top = Inches(P4_Y[sh.shape_id])
        log.append('  p4 id=%-5d y %.2f→%.2f' % (sh.shape_id, old, P4_Y[sh.shape_id]))
    if sh.shape_id in P4_BLOCK:
        if APPLY:
            sh.height = Inches(P4_BLOCK[sh.shape_id])
    if sh.shape_id in (145, 146, 147, 149, 150, 151, 153, 154, 155):
        if APPLY:
            sh.width = Inches(4.42)
# G. 第 20 页右侧「各场景效率提升幅度」：原为一张 5 类目条形图，画布只给到 1.51 英寸高，
#    PowerPoint 自动跳标，五个类目只剩三条读数（60/80/90 之外的两条根本不出来）。
#    这里把 chart 换成五行手绘条：标签 + 条 + 数值，行高 0.32 固定，五个数全部可读；
#    数据与左侧表格「降幅」列完全一致，条形只承担「一眼看相对大小」，不做第二套口径。
P20_ROWS = [('全量审计', 80), ('应急确认', 90), ('增量审计', 90), ('信息收集', 60), ('版本差分', 90)]
for sh in list(prs.slides[19].shapes):
    if sh.shape_id == 1214 and sh.has_chart:
        if APPLY:
            sp = sh._element
            sp.getparent().remove(sp)
        log.append('  p20 删除 chart id=1214（5 类目被跳标）')
if APPLY:
    for i, (name, val) in enumerate(P20_ROWS):
        ry = 4.18 + i * 0.34
        t1 = tb(prs.slides[19], 11.96, ry - 0.01, 1.22, 0.26, MSO_ANCHOR.MIDDLE)
        put(t1.text_frame, name, 10.5, False, SUB, True)
        bar_bg = prs.slides[19].shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(13.22), Inches(ry + 0.06), Inches(3.18), Inches(0.13))
        bar_bg.adjustments[0] = 0.45
        bar_bg.fill.solid()
        bar_bg.fill.fore_color.rgb = SOFT
        bar_bg.line.fill.background()
        bar_bg.shadow.inherit = False
        bw = 3.18 * val / 100.0
        bar = prs.slides[19].shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(13.22), Inches(ry + 0.06), Inches(bw), Inches(0.13))
        bar.adjustments[0] = 0.45
        bar.fill.solid()
        bar.fill.fore_color.rgb = MAIN
        bar.line.fill.background()
        bar.shadow.inherit = False
        t2 = tb(prs.slides[19], 16.48, ry - 0.01, 0.62, 0.26, MSO_ANCHOR.MIDDLE)
        put(t2.text_frame, '%d%%' % val, 10.5, True, ACC, True, PP_ALIGN.RIGHT)
    log.append('  p20 重绘 5 行条形')
print('--- %d ops ---' % len(log))
print(chr(10).join(log))
if kept:
    print('--- kept ---')
    print(chr(10).join(kept))
if APPLY:
    prs.save(DST)
    print('SAVED ' + DST)