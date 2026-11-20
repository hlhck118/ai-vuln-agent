# -*- coding: utf-8 -*-
"""在上传版 16 页 deck 上补充：开发背景、真实运行截图（3 页）、经济效益测算。
沿用原 deck 的版式语言：底色 #F7F8FA、主色 #3E5C76、强调 #C0564A、字体 Noto Sans SC。
用法：python -X utf8 docs/deck_augment.py --in IN.pptx --out OUT.pptx
"""
import argparse
import os

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR, MSO_AUTO_SIZE
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls, qn

# 截图目录按脚本自己的位置算：写成 `F:/Desktop/...` 时，换台机器、换个克隆路径
# 就只会在画到第 17 页时抛 FileNotFoundError，而前面 16 页看着都是好的。
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deck_assets") + "/"

FONT = "Noto Sans SC"
BG = RGBColor(0xF7, 0xF8, 0xFA)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
CELL = RGBColor(0xF9, 0xFA, 0xFB)
INK = RGBColor(0x1F, 0x2A, 0x37)
MAIN = RGBColor(0x3E, 0x5C, 0x76)
ACC = RGBColor(0xC0, 0x56, 0x4A)
SUB = RGBColor(0x5B, 0x6B, 0x7C)
LIGHT = RGBColor(0x8F, 0xA3, 0xB8)
PALE = RGBColor(0xC9, 0xD4, 0xE0)
SOFT = RGBColor(0xEC, 0xF1, 0xF5)


def style_run(r, size, bold=False, color=INK, font=FONT):
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.color.rgb = color
    r.font.name = font
    rPr = r._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):
        if rPr.find(qn(tag)) is None:
            rPr.append(parse_xml('<%s %s typeface="%s"/>' % (tag, nsdecls("a"), font)))


def textbox(slide, x, y, w, h, anchor=MSO_ANCHOR.TOP):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    return tf


def para(tf, text, size=14, bold=False, color=INK, first=False,
         space_before=0, line=1.30, align=PP_ALIGN.LEFT):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.alignment = align
    p.line_spacing = line
    if space_before:
        p.space_before = Pt(space_before)
    if text:
        style_run(p.add_run(), size, bold, color)
        p.runs[-1].text = text
    return p


def rich(tf, parts, size=14, first=False, space_before=0, line=1.35, align=PP_ALIGN.LEFT):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.alignment = align
    p.line_spacing = line
    if space_before:
        p.space_before = Pt(space_before)
    for text, bold, color in parts:
        r = p.add_run()
        r.text = text
        style_run(r, size, bold, color)
    return p


def set_alpha(shape, pct):
    spPr = shape._element.spPr
    clr = spPr.find(qn("a:solidFill")).find(qn("a:srgbClr"))
    clr.append(parse_xml('<a:alpha %s val="%d"/>' % (nsdecls("a"), int(pct * 1000))))


def rect(slide, x, y, w, h, color, alpha=None, shape=MSO_SHAPE.RECTANGLE):
    sp = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    sp.fill.solid()
    sp.fill.fore_color.rgb = color
    sp.line.fill.background()
    sp.shadow.inherit = False
    if alpha is not None:
        set_alpha(sp, alpha)
    sp.text_frame.word_wrap = True
    return sp


def card(slide, x, y, w, h, fill=WHITE, radius=5000, shadow=True):
    sp = rect(slide, x, y, w, h, fill, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
    sp.adjustments[0] = radius / 100000.0
    if shadow:
        spPr = sp._element.spPr
        old = spPr.find(qn("a:effectLst"))
        if old is not None:
            spPr.remove(old)
        spPr.append(parse_xml(
            '<a:effectLst %s><a:outerShdw blurRad="25400" dist="12700" dir="5400000" algn="ctr">'
            '<a:srgbClr val="000000"><a:alpha val="5000"/></a:srgbClr></a:outerShdw></a:effectLst>'
            % nsdecls("a")))
    return sp


def chrome(slide, section, title, sub=None):
    rect(slide, 0, 0, 17.78, 10.0, BG)
    rect(slide, 0.28, 2.78, 0.04, 1.11, MAIN, alpha=80)
    rect(slide, 0.19, 4.11, 0.22, 0.22, ACC, alpha=90)
    rect(slide, 17.47, 2.50, 0.03, 1.67, LIGHT, alpha=60)
    tf = textbox(slide, 0.56, 9.53, 1.58, 0.25)
    para(tf, "成果发布汇报", 12, True, MAIN, first=True, line=1.25)
    tf = textbox(slide, 1.72, 9.53, 2.79, 0.25)
    para(tf, "ACHIEVEMENT RELEASE", 12, False, LIGHT, first=True, line=1.25)
    rect(slide, 0.56, 0.33, 0.06, 0.44, MAIN)
    tf = textbox(slide, 0.83, 0.18, 14.6, 0.75)
    para(tf, title, 36, True, INK, first=True, line=1.18)
    if section:
        tf = textbox(slide, 15.74, 0.41, 1.90, 0.29)
        para(tf, section, 14, False, LIGHT, first=True, line=1.25)
        rect(slide, 17.11, 0.50, 0.11, 0.11, ACC)
    if sub:
        tf = textbox(slide, 0.86, 1.02, 16.0, 0.34)
        para(tf, sub, 13, False, SUB, first=True, line=1.25)


def new_slide(prs):
    return prs.slides.add_slide(prs.slide_layouts[0])


def notes(slide, text):
    slide.notes_slide.notes_text_frame.text = text


def plain_cell(cell, fill_hex=None):
    tcPr = cell._tc.get_or_add_tcPr()
    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
        el = tcPr.find(qn(tag))
        if el is not None:
            tcPr.remove(el)
    for idx, tag in enumerate(("a:lnL", "a:lnR", "a:lnT", "a:lnB")):
        tcPr.insert(idx, parse_xml('<%s %s><a:noFill/></%s>' % (tag, nsdecls("a"), tag)))
    tcPr.set("marL", "114300")
    tcPr.set("marR", "114300")
    tcPr.set("marT", "63500")
    tcPr.set("marB", "63500")
    tcPr.set("anchor", "ctr")
    if fill_hex is not None:
        cell.fill.solid()
        cell.fill.fore_color.rgb = fill_hex


def build_table(slide, x, y, w, col_widths, row_h, data, header=True,
                body_size=10.5):
    rows, cols = len(data), len(data[0])
    gf = slide.shapes.add_table(rows, cols, Inches(x), Inches(y), Inches(w), Inches(row_h * rows))
    tbl = gf.table
    tblPr = gf._element.graphic.graphicData.tbl.find(qn("a:tblPr"))
    if tblPr is not None:
        sid = tblPr.find(qn("a:tableStyleId"))
        if sid is not None:
            tblPr.remove(sid)
        tblPr.set("firstRow", "0")
        tblPr.set("bandRow", "0")
    for i, cw in enumerate(col_widths):
        tbl.columns[i].width = Inches(cw)
    for i in range(rows):
        tbl.rows[i].height = Inches(row_h)
    for ri, row in enumerate(data):
        for ci, (text, bold, color) in enumerate(row):
            cell = tbl.rows[ri].cells[ci]
            if header and ri == 0:
                plain_cell(cell, MAIN)
            else:
                plain_cell(cell, CELL if (header and ri % 2 == 0) else (WHITE if header else (WHITE if ri % 2 == 1 else CELL)))
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.LEFT
            p.line_spacing = 1.15
            style_run(p.add_run(), body_size, bold, color)
            p.runs[-1].text = text
    return gf


def picture(slide, path, x, y, w, h):
    """按原始像素比例等比塞进 (x, y, w, h) 这个框并水平居中，避免拉伸变形。"""
    from PIL import Image
    with Image.open(path) as im:
        iw, ih = im.size
    scale = min(w / iw, h / ih)
    pw, ph = iw * scale, ih * scale
    return slide.shapes.add_picture(path, Inches(x + (w - pw) / 2.0), Inches(y + (h - ph) / 2.0),
                                    Inches(pw), Inches(ph))


# ---------------------------------------------------------------- 页 A 开发背景
def slide_background(prs):
    s = new_slide(prs)
    chrome(s, "SECTION 02", "三、开发背景：从能跑的原型到可复算的系统",
           "时间线取自版本库提交记录；工程规模取自当前代码库实测，口径可在仓库内复核")
    cards = [
        ("11-17 · 起点", "先证明能做",
         "把「大模型能不能挖漏洞」做成能跑完的闭环原型：12 个模块 3982 行，三个样例目标各配修复版做差分对照。"),
        ("11-18 · 工程化", "再证明好用",
         "原型只回答「可行吗」。要交付就得有会话、有权限、有留痕：控制面 57 个模块，工作台 12 个前端模块。"),
        ("11-19 · 可复算", "把判定交出去",
         "结论从「模型说了算」改成「证据说了算」：写入即连边，报告由库内记录合成，同一输入重跑结论一致。"),
        ("11-20 · 可交付", "三种部署形态",
         "离线单机、旁路部署、私有云三形态；48 项工具能力、15 个视图，交付即能用，不依赖任何外部云服务。"),
    ]
    for i, (tag, head, body) in enumerate(cards):
        x = 0.86 + i * 4.08
        card(s, x, 1.55, 3.96, 2.52, radius=7692)
        tf = textbox(s, x + 0.24, 1.75, 3.48, 0.28)
        para(tf, tag, 11, True, ACC if i == 0 else MAIN, first=True, line=1.2)
        tf = textbox(s, x + 0.24, 2.06, 3.48, 0.40)
        para(tf, head, 16, True, INK, first=True, line=1.2)
        tf = textbox(s, x + 0.24, 2.56, 3.48, 1.35)
        para(tf, body, 11.5, False, SUB, first=True, line=1.45)

    card(s, 0.56, 4.28, 8.20, 2.42, radius=5714)
    tf = textbox(s, 0.86, 4.48, 7.6, 0.34)
    para(tf, "技术约束先于功能清单", 15, True, MAIN, first=True, line=1.2)
    tf = textbox(s, 0.86, 4.92, 7.62, 1.62)
    for i, (a, b) in enumerate([
        ("无外部服务依赖", "存储、图谱、沙箱全部自研，断网也能跑完整闭环"),
        ("数据不出内网", "模型可替换、可本地部署，密钥与日志留在本机"),
        ("结论必须可复核", "每条结论绑定证据与复现步骤，导出即报告"),
    ]):
        rich(tf, [("%d  " % (i + 1), True, ACC), (a, True, INK), ("  " + b, False, SUB)],
             12, first=(i == 0), space_before=0 if i == 0 else 8, line=1.3)

    card(s, 9.11, 4.28, 8.11, 2.42, radius=5714)
    tf = textbox(s, 9.41, 4.48, 7.5, 0.34)
    para(tf, "工程规模（当前代码库实测）", 15, True, MAIN, first=True, line=1.2)
    stats = [("57 个", "后端模块"), ("52084 行", "后端代码"), ("16810 行", "前端代码"), ("3142 项", "自动化用例")]
    for i, (v, k) in enumerate(stats):
        x = 9.41 + (i % 2) * 3.86
        y = 4.96 + (i // 2) * 0.84
        tf = textbox(s, x, y, 1.85, 0.42)
        para(tf, v, 24, True, MAIN, first=True, line=1.1)
        tf = textbox(s, x, y + 0.42, 3.4, 0.26)
        para(tf, k, 11, False, SUB, first=True, line=1.2)

    rect(s, 0.56, 6.92, 16.67, 1.68, MAIN, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
    tf = textbox(s, 1.00, 7.12, 15.6, 0.28)
    para(tf, "为什么先做工程、再谈模型", 12.5, True, PALE, first=True, line=1.2)
    tf = textbox(s, 1.00, 7.44, 15.4, 0.42)
    para(tf, "模型能力会换代，判定标准不能跟着换代", 20, True, WHITE, first=True, line=1.2)
    tf = textbox(s, 1.00, 7.94, 15.6, 0.34)
    para(tf, "把「是否成立」交给可执行验证，把「怎么找」交给模型；模型升级时，历史结论不被推翻。",
         12.5, False, SOFT, first=True, line=1.2)
    notes(s, "这一页回答「怎么来的」。时间线的四段对应版本库的四个阶段：先有能跑完的原型，"
             "再做工程化（会话、权限、留痕），最后把判定标准从模型改成交付可复算。"
             "右下四个数字是当前代码库实测，审计时可以现场复核；"
             "底下一句话是本项目的取舍：判定标准不跟模型换代。约 40 秒。")
    return s


# ---------------------------------------------------------------- 页 B/C/D 真实运行
def slide_shots(prs, title, sub, left, right, note1, note2, bottom, speaker=""):
    s = new_slide(prs)
    chrome(s, "SECTION 05", title, sub)
    for x, (cap, path) in ((0.56, left), (9.11, right)):
        tf = textbox(s, x, 1.50, 8.10, 0.30)
        para(tf, cap, 12.5, True, MAIN, first=True, line=1.2)
        picture(s, path, x, 1.86, 8.10, 5.02)
    for x, note in ((0.56, note1), (9.11, note2)):
        tf = textbox(s, x, 7.02, 8.10, 0.88)
        para(tf, note, 11.5, False, SUB, first=True, line=1.42)
    rect(s, 0.56, 8.06, 0.04, 1.04, ACC, alpha=90)
    tf = textbox(s, 0.78, 8.06, 16.4, 1.04)
    para(tf, bottom, 12.5, False, SUB, first=True, line=1.45)
    if speaker:
        notes(s, speaker)
    return s


def slide_shots_1(prs):
    A = ASSETS
    return slide_shots(
        prs,
        "真实运行数据（一）：一句话下发任务，过程与耗时可见",
        "截图取自本机运行实例，非设计稿：目标 2 个 · 节点 433 · 边 425 · 任务 17 条",
        ("控制台：对话即任务", A + "crops/c-chat.png"),
        ("探索链路：全局证据图", A + "crops/c-graph.png"),
        "Agent 在本轮逐步调用 list_toolkit / run_command / run_tool，每一步的耗时串在对话里；右侧任务面板同时给出 17 条任务的全部状态。",
        "节点 433、边 425，按目标（蓝）、意图（紫）、事实（绿）、漏洞（橙）、资产（青）五类着色；点任一节点可看它的全部原始字段。",
        "一条自然语言指令进去，出来的是「谁在什么时候、用什么工具、对哪个地址、得到了什么」——过程本身即为记录，不需要事后补材料。",
        speaker="这是本机实例的实拍，不是设计稿。左图是一次对话的实时过程：模型每一步调了什么工具、花了多久，"
                "都串在对话里；右图是同一个库里的全局证据图。两边的数字对得上，因为它们是同一份数据。约 30 秒。",
    )


def slide_shots_2(prs):
    A = ASSETS
    return slide_shots(
        prs,
        "真实运行数据（二）：每条结论都带证据与复现步骤",
        "同一个库里的 34 条结论，全部标注「已证实」或「待确认」，不存在只在报告里出现、库里查不到的结论",
        ("漏洞：34 条，按严重度分组", A + "crops/c-find.png"),
        ("报告：25762 字符 / 426 行", A + "crops/c-report.png"),
        "严重 1 · 高危 3 · 中危 12 · 低危 10 · 信息 3；每条附依据原文与 3 步复现步骤，例如 Redis 未授权一条给出 RESP 命令与预期回显。",
        "按任务归档：目标与授权、执行记录（任务号 / 类型 / 耗时 / 写入节点）、结论汇总；点一次执行就只看那一次的结论。",
        "报告不是二次写作的产物，而是库内记录的投影：摘掉任何一条结论，对应的执行记录与证据也会一起消失——这是「可复核」的最低要求。",
        speaker="这页是「结论怎么站得住」。左边 34 条结论，全部标注已证实或待确认；"
                "右边报告里每条都带依据原文和复现步骤。请注意报告的统计口径：意图、事实、漏洞、资产分别计数，"
                "不存在只出现在报告里、库里查不到的结论。约 30 秒。",
    )


def slide_shots_3(prs):
    A = ASSETS
    return slide_shots(
        prs,
        "真实运行数据（三）：资产测绘与任务执行全部留痕",
        "资产 51 项、任务 17 条 / 37 次执行；失败同样落库并写明原因，不产生「看起来成功」的记录",
        ("资产：按目标独立成树", A + "crops/c-assets.png"),
        ("任务：逐次执行可展开", A + "crops/c-jobs.png"),
        "端口、服务、指纹逐层展开，每个目标一个独立根；资产与漏洞、意图之间由边连接，图上点得回去。",
        "每条记录 adapter、exitCode、耗时与写入节点数，例如 nmap · exitCode 0 · 链路节点 2；失败的执行同样保留原始输出。",
        "结论能一路回溯到产生它的那次执行。审计要的不是结论好不好看，而是能不能把结论重新走一遍。",
        speaker="最后一页实拍讲留痕。左边资产按目标独立成树，右边每次执行都能展开到 adapter、退出码、耗时、写入节点数。"
                "失败的执行同样落库并写明原因，不产生看起来成功的记录。约 30 秒。",
    )


# ---------------------------------------------------------------- 页 E 经济效益
def slide_economy(prs):
    s = new_slide(prs)
    chrome(s, "SECTION 06", "六、经济效益：一次性投入、年度节省与回收周期",
           "人力单价取行业顾问 3000–6000 元/人天区间；节省额与回收期由该区间与年度人天差直接折算")
    kpis = [
        ("280 人天", "年度人力差（400 → 120）", ACC),
        ("84–168 万元", "按 3000–6000 元/人天折算的年节省", MAIN),
        ("1.4–8.6 个月", "一次性投入 20–60 万时的静态回收期", MAIN),
        ("70% 以上", "每千行算力成本降幅（设计锚点）", MAIN),
    ]
    for i, (v, k, color) in enumerate(kpis):
        x = 0.56 + i * 4.12
        card(s, x, 1.50, 3.96, 1.28, radius=10000)
        tf = textbox(s, x + 0.26, 1.66, 3.5, 0.46)
        para(tf, v, 26, True, color, first=True, line=1.1)
        tf = textbox(s, x + 0.26, 2.22, 3.46, 0.42)
        para(tf, k, 11, False, SUB, first=True, line=1.28)

    tf = textbox(s, 0.56, 3.00, 8.2, 0.34)
    para(tf, "成本结构：四个成本项各自的去向", 15, True, MAIN, first=True, line=1.2)
    data = [
        [("成本项", True, WHITE), ("人工方式", True, WHITE), ("本系统", True, WHITE), ("效益", True, WHITE)],
        [("推理成本", True, INK), ("分析师时薪 × 数小时", False, SUB), ("模型 token（秒级）", True, MAIN), ("降 70% 以上", True, ACC)],
        [("验证成本", True, INK), ("手工搭建复现环境", False, SUB), ("沙箱自动差分", True, MAIN), ("每人次→每任务", True, ACC)],
        [("重复成本", True, INK), ("每次变更全量重看", False, SUB), ("增量只审影响面", True, MAIN), ("随变更量增长", True, ACC)],
        [("误报成本", True, INK), ("每条误报一次复现", False, SUB), ("差分裁决前置拦截", True, MAIN), ("显著下降", True, ACC)],
    ]
    build_table(s, 0.56, 3.40, 8.20, [1.9, 2.5, 2.2, 1.6], 0.532, data)

    card(s, 9.11, 3.00, 8.11, 3.40, radius=5714)
    tf = textbox(s, 9.41, 3.20, 7.5, 0.34)
    para(tf, "回收期敏感性（月）", 15, True, MAIN, first=True, line=1.2)
    tf = textbox(s, 9.41, 3.54, 7.5, 0.28)
    para(tf, "静态回收期 = 一次性投入 ÷（年节省 ÷ 12）", 10.5, False, SUB, first=True, line=1.2)
    rec = [
        [("一次性投入", True, WHITE), ("年省 84 万", True, WHITE), ("年省 168 万", True, WHITE)],
        [("20 万", True, INK), ("2.9", False, SUB), ("1.4", False, SUB)],
        [("40 万", True, INK), ("5.7", False, SUB), ("2.9", False, SUB)],
        [("60 万", True, INK), ("8.6", False, SUB), ("4.3", False, SUB)],
    ]
    build_table(s, 9.41, 3.92, 7.5, [2.5, 2.5, 2.5], 0.475, rec, body_size=11)
    tf = textbox(s, 9.41, 5.98, 7.5, 0.28)
    para(tf, "未计资金时间价值；投入规模按部署形态核定，非报价", 10.5, False, LIGHT, first=True, line=1.2)

    card(s, 0.56, 6.52, 16.67, 2.28, fill=CELL, radius=5714, shadow=False)
    tf = textbox(s, 0.86, 6.72, 15.8, 0.32)
    para(tf, "测算口径与边界（不把设计目标当成实测结果）", 14, True, MAIN, first=True, line=1.2)
    tf = textbox(s, 0.86, 7.14, 15.9, 1.5)
    for i, t in enumerate([
        "人天单价取行业顾问 3000–6000 元/人天，未含间接管理成本与业务停机损失；年度 400 → 120 人天取自五类场景加权测算。",
        "一次性投入（模型接入、节点接入、规则与技能手册调优）不计入单次审计成本，避免把建设成本摊进每一次扫描。",
        "每千行 70% 为设计锚点与估算；容器隔离成本因本机无 Docker 尚未实测，规模化部署后需重算。",
    ]):
        rich(tf, [("%d  " % (i + 1), True, ACC), (t, False, SUB)],
             11.5, first=(i == 0), space_before=0 if i == 0 else 6, line=1.42)
    notes(s, "经济账只有三个数：一年少花多少人天、折成多少钱、多久收回投入。"
             "280 人天来自五类场景加权；84 到 168 万是把人天乘上行业顾问单价区间；"
             "回收期是一笔一次性投入除以月节省，做了敏感性表，没做折现。"
             "每千行降七成是设计锚点，不是实测，所以我把它单独标出来。约 40 秒。")
    return s


SECTION_PLAN = {
    2: "SECTION 01", 3: "SECTION 01",                    # 一、形势
    4: "SECTION 02",                                     # 二、问题分析
    5: "SECTION 03", 6: "SECTION 03", 7: "SECTION 03",   # 三、开发背景与总体方案
    8: "SECTION 04", 9: "SECTION 04", 10: "SECTION 04",  # 四、技术创新
    11: "SECTION 04", 12: "SECTION 04", 13: "SECTION 04", 14: "SECTION 04",
    15: "SECTION 05", 16: "SECTION 05", 17: "SECTION 05", 18: "SECTION 05",  # 实测
    19: "SECTION 06", 20: "SECTION 06",                  # 经济效益
}


def relabel_sections(prs):
    for idx, label in SECTION_PLAN.items():
        if idx > len(prs.slides):
            continue
        for sh in prs.slides[idx - 1].shapes:
            if not sh.has_text_frame:
                continue
            t = sh.text_frame.text.strip()
            if t.startswith("SECTION ") and len(t) <= 12:
                p = sh.text_frame.paragraphs[0]
                if p.runs:
                    p.runs[0].text = label
                    for extra in p.runs[1:]:
                        extra.text = ""
                break


def reorder(prs, order):
    lst = prs.slides._sldIdLst
    ids = list(lst)
    for el in ids:
        lst.remove(el)
    for i in order:
        lst.append(ids[i])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    a = ap.parse_args()
    prs = Presentation(a.src)
    n0 = len(prs.slides)
    slide_background(prs)
    slide_shots_1(prs)
    slide_shots_2(prs)
    slide_shots_3(prs)
    slide_economy(prs)
    # 原 0..15；新增 16 开发背景 / 17-19 截图 / 20 经济
    order = [0, 1, 2, 3, 16, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 17, 18, 19, 14, 20, 15]
    assert len(order) == n0 + 5, (len(order), n0)
    reorder(prs, order)
    relabel_sections(prs)
    # 末页页码
    last = prs.slides[-1]
    for sh in last.shapes:
        if sh.has_text_frame and sh.text_frame.text.strip() == "16 / 16":
            p = sh.text_frame.paragraphs[0]
            p.runs[0].text = "21 / 21"
    prs.save(a.dst)
    print("slides:", len(prs.slides), "->", a.dst)


if __name__ == "__main__":
    main()
