# -*- coding: utf-8 -*-
"""生成 8 分钟成果发布 PPT（.pptx）—— 面向领导，全部用大白话。

版式取自项目自己的视觉令牌（视觉设计系统.md 的深色面板 + Apple HIG 的层级），
用 python-pptx 直接画，不维护 .potx 模板。

写法约定：**页面上只放领导和评委能一眼看懂的话**，所有技术名词、文件名、
协议细节都放进演讲者备注（讲的时候说，不摆在投影上）。

用法：
    python -X utf8 docs/deck_release.py --out 成果发布-8分钟.pptx
"""
import argparse
import os
import sys

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

W, H = 13.333, 7.5

BG = RGBColor(0x0B, 0x0B, 0x0D)
SURF1 = RGBColor(0x16, 0x16, 0x1A)
SURF2 = RGBColor(0x1E, 0x1E, 0x23)
LINE = RGBColor(0x34, 0x34, 0x3B)
TEXT = RGBColor(0xF5, 0xF5, 0xF7)
DIM = RGBColor(0xAD, 0xAD, 0xB8)
MUTED = RGBColor(0x9A, 0x9A, 0xA6)
ACCENT = RGBColor(0x0A, 0x84, 0xFF)
ACCENT_T = RGBColor(0x40, 0x9C, 0xFF)
OK = RGBColor(0x30, 0xD1, 0x58)
WARN = RGBColor(0xFF, 0xD6, 0x0A)
DANGER = RGBColor(0xFF, 0x45, 0x3A)
CYAN = RGBColor(0x40, 0xC8, 0xE0)
VIOLET = RGBColor(0x5E, 0x5C, 0xE6)

FONT = "微软雅黑"


def set_font(run, size, bold=False, color=TEXT, name=FONT):
    font = run.font
    font.size = Pt(size)
    font.bold = bold
    font.color.rgb = color
    font.name = name
    rPr = run._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):
        node = rPr.find(qn(tag))
        if node is None:
            node = rPr.makeelement(qn(tag), {})
            rPr.append(node)
        node.set("typeface", name)


def textbox(slide, x, y, w, h):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    return tf


def para(tf, text, size=16, bold=False, color=TEXT, first=False, space_before=6,
         line=1.32, align=PP_ALIGN.LEFT):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.alignment = align
    p.space_before = Pt(space_before)
    p.line_spacing = line
    run = p.add_run()
    run.text = text
    set_font(run, size, bold, color)
    return p


def rich(tf, parts, size=16, first=False, space_before=6, line=1.32):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.space_before = Pt(space_before)
    p.line_spacing = line
    for text, bold, color in parts:
        run = p.add_run()
        run.text = text
        set_font(run, size, bold, color)
    return p


def blank(prs):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = BG
    return slide


def card(slide, x, y, w, h, fill=SURF1, edge=None, radius=0.06):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.adjustments[0] = radius
    shape.shadow.inherit = False
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    if edge is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = edge
        shape.line.width = Pt(1)
    return shape


def bar(slide, x, y, w, h, color):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                   Inches(x), Inches(y), Inches(w), Inches(h))
    shape.adjustments[0] = 0.5
    shape.shadow.inherit = False
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


def head(slide, kicker, title, sub=None):
    bar(slide, 0.85, 0.60, 0.10, 0.56, ACCENT)
    tf = textbox(slide, 1.10, 0.52, 11.3, 1.0)
    para(tf, kicker, 12.5, True, ACCENT_T, first=True, space_before=0)
    para(tf, title, 27, True, TEXT, space_before=4, line=1.2)
    if sub:
        tf2 = textbox(slide, 1.10, 1.52, 11.3, 0.46)
        para(tf2, sub, 13.5, False, DIM, first=True, space_before=0)


def footer(slide, idx, total, note=""):
    tf = textbox(slide, 0.85, 6.95, 10.4, 0.32)
    para(tf, note, 10, False, MUTED, first=True, space_before=0)
    tf2 = textbox(slide, 11.7, 6.95, 0.95, 0.32)
    para(tf2, "%02d / %02d" % (idx, total), 10, False, MUTED, first=True,
         space_before=0, align=PP_ALIGN.RIGHT)


def notes(slide, text):
    slide.notes_slide.notes_text_frame.text = text


def kpi(slide, x, y, w, value, label, color=ACCENT, h=1.20):
    card(slide, x, y, w, h, SURF1)
    tf = textbox(slide, x + 0.26, y + 0.18, w - 0.44, h - 0.26)
    para(tf, value, 24, True, color, first=True, space_before=0)
    para(tf, label, 11.5, False, DIM, space_before=3)


def bullet(slide, x, y, w, items, size=15, gap=0.86, mark_color=ACCENT):
    for i, (title, body) in enumerate(items):
        yy = y + i * gap
        bar(slide, x, yy + 0.12, 0.09, 0.09, mark_color)
        tf = textbox(slide, x + 0.27, yy - 0.02, w, gap)
        if body:
            rich(tf, [(title, True, TEXT), ("　" + body, False, DIM)],
                 size=size, first=True, space_before=0)
        else:
            para(tf, title, size, True, TEXT, first=True, space_before=0)


def build(path):
    prs = Presentation()
    prs.slide_width = Inches(W)
    prs.slide_height = Inches(H)
    total = 12

    # ── 01 封面 ──────────────────────────────────────────────
    s = blank(prs)
    glow = s.shapes.add_shape(MSO_SHAPE.OVAL, Inches(8.4), Inches(-1.6),
                              Inches(7.2), Inches(7.2))
    glow.shadow.inherit = False
    glow.line.fill.background()
    glow.fill.solid()
    glow.fill.fore_color.rgb = RGBColor(0x10, 0x14, 0x1C)

    bar(s, 1.0, 1.80, 0.12, 0.76, ACCENT)
    tf = textbox(s, 1.30, 1.66, 9.8, 2.6)
    para(tf, "成果发布 · 8 分钟", 14, True, ACCENT_T, first=True, space_before=0)
    para(tf, "AI 智能体渗透测试工作台", 44, True, TEXT, space_before=10)
    para(tf, "让「找漏洞」这件事，有据可查、说得清楚", 20, False, DIM,
         space_before=12)

    card(s, 1.0, 4.62, 6.4, 0.60, SURF1)
    tf = textbox(s, 1.24, 4.74, 6.0, 0.4)
    para(tf, "一台电脑就能装 · 不用连外网 · 每一步都留记录", 13, False, MUTED,
         first=True, space_before=0)

    stats = [("48", "项能力"), ("7", "类任务"), ("15", "个界面"),
             ("3142", "项自检全过")]
    for i, (v, l) in enumerate(stats):
        x = 1.0 + i * 1.55
        tf = textbox(s, x, 5.60, 1.45, 0.9)
        para(tf, v, 24, True, ACCENT_T, first=True, space_before=0)
        para(tf, l, 11.5, False, MUTED, space_before=2)
    notes(s, "40 秒。开场只说一句话：我们把'靠老师傅翻代码找漏洞'这件事，"
             "做成了一套看得见、查得到、能交代的流程。"
             "四个数字先立住：48 项能力、7 类任务、15 个界面、3142 项自检全过。")
    footer(s, 1, total, "")

    # ── 02 一句话讲清 ────────────────────────────────────────
    s = blank(prs)
    head(s, "01 这是什么", "一句话：把老师傅脑子里的判断，变成一套看得见的流程",
         "电脑上开个网页就能用，不用装数据库、不用装框架、不用连外网")
    kpi(s, 0.85, 2.14, 2.55, "48 项", "能做的事，说一句就能调", ACCENT)
    kpi(s, 3.55, 2.14, 2.55, "7 类", "任务类型，从查代码到查网络", CYAN)
    kpi(s, 6.25, 2.14, 2.55, "15 个", "界面，从下命令到出报告", VIOLET)
    kpi(s, 8.95, 2.14, 3.35, "0", "需要另装的外部组件", OK)
    tf = textbox(s, 0.98, 3.46, 11.3, 0.4)
    para(tf, "查代码 · 查网络 · 验证能不能真打进去 · 测口令 · 投递载荷 · 绕出去 · 调外部工具",
         11, False, MUTED, first=True, space_before=0)
    bullet(s, 0.85, 4.06, 11.4, [
        ("大脑在服务器，手脚在目标旁边",
         "网页只是屏幕，判断都在服务器；真正去扫的那部分，装在能连到目标的那台机器上"),
        ("一份规则，两头都认",
         "能扫哪儿、能做什么，只写了一遍；服务器和目标机不会各说各话"),
        ("结果只存一份",
         "所有结论落在一个文件里，随时能备份、能整包导出来给人看"),
    ], size=14, gap=0.88)
    notes(s, "45 秒。这一页就讲一个比喻：大脑在服务器，手脚在目标旁边。"
             "为什么手脚要分开装？因为有些目标在别人内网里，只有那台机器够得着。")
    footer(s, 2, total, "")

    # ── 03 以前为什么贵 ──────────────────────────────────────
    s = blank(prs)
    head(s, "02 为什么要做", "过去查漏洞，贵在「来回跑四趟」")
    steps = [("① 先读懂", "翻代码、翻文档、翻测试环境"),
             ("② 再往下追", "一个入口牵出十几个文件"),
             ("③ 搭环境真打一次", "造请求、看反应、复现"),
             ("④ 再核实一遍", "证明「这不是误报」")]
    for i, (t, b) in enumerate(steps):
        x = 0.85 + i * 3.02
        card(s, x, 1.86, 2.78, 1.44, SURF1)
        tf = textbox(s, x + 0.26, 2.06, 2.32, 1.1)
        para(tf, t, 15, True, TEXT, first=True, space_before=0)
        para(tf, b, 11.5, False, DIM, space_before=7, line=1.3)

    card(s, 0.85, 3.70, 5.82, 2.42, SURF1, edge=RGBColor(0x3A, 0x1B, 0x1B))
    tf = textbox(s, 1.12, 3.94, 5.3, 2.0)
    para(tf, "靠人做", 13, True, DANGER, first=True, space_before=0)
    para(tf, "以小时、以天算，而且估不准", 16, False, TEXT, space_before=10)
    para(tf, "结论写在报告里，依据留在脑子里", 13, False, DIM, space_before=8)
    para(tf, "换个人接手，等于从零再来一遍", 13, False, DIM, space_before=5)

    card(s, 6.88, 3.70, 5.6, 2.42, SURF1, edge=RGBColor(0x3A, 0x1B, 0x1B))
    tf = textbox(s, 7.15, 3.94, 5.1, 2.0)
    para(tf, "靠传统扫描器", 13, True, DANGER, first=True, space_before=0)
    para(tf, "报得多，准得少", 16, False, TEXT, space_before=10)
    para(tf, "项目一大，每次改动都得全量重扫", 13, False, DIM, space_before=8)
    para(tf, "最后要么扫不起，要么扫得少", 13, False, DIM, space_before=5)
    notes(s, "45 秒。四个断点要用'人'的话说：读代码、往下追、真打一次、再核实。"
             "右边这条是给传统扫描器留的位置：不是它没用，是它只能告诉你'看着像'。")
    footer(s, 3, total, "")

    # ── 04 现在怎么做 ────────────────────────────────────────
    s = blank(prs)
    head(s, "03 怎么做的", "一条流水线：下命令 → 派活 → 就地干 → 带证据回来")
    card(s, 4.55, 1.92, 4.25, 0.74, SURF2)
    tf = textbox(s, 4.75, 2.08, 3.9, 0.48)
    para(tf, "使用者 · 打开网页下命令", 14, True, TEXT, first=True, space_before=0,
         align=PP_ALIGN.CENTER)

    card(s, 4.55, 2.94, 4.25, 1.20, SURF2, edge=ACCENT)
    tf = textbox(s, 4.78, 3.10, 3.8, 0.95)
    para(tf, "指挥中心 · 服务器", 14, True, ACCENT_T, first=True, space_before=0,
         align=PP_ALIGN.CENTER)
    para(tf, "排任务 · 发指令 · 存结果", 11, False, DIM, space_before=4,
         align=PP_ALIGN.CENTER)
    para(tf, "三道锁都在这一层", 11, False, DIM, space_before=2,
         align=PP_ALIGN.CENTER)

    for i, (t, b) in enumerate([("内网", "别人机房里的那台"),
                                ("隔离区", "能出去的跳板"),
                                ("构建机", "改一次就扫一次")]):
        x = 1.15 + i * 3.40
        card(s, x, 4.68, 3.20, 1.28, SURF1)
        tf = textbox(s, x + 0.24, 4.86, 2.75, 1.0)
        para(tf, "手脚 · " + t, 14, True, TEXT, first=True, space_before=0,
             align=PP_ALIGN.CENTER)
        para(tf, b, 11, False, DIM, space_before=5, align=PP_ALIGN.CENTER)

    bar(s, 6.65, 2.66, 0.06, 0.26, LINE)
    bar(s, 6.65, 4.14, 0.06, 0.52, LINE)
    tf = textbox(s, 9.02, 2.02, 4.0, 0.6)
    para(tf, "只在内网里说话，不必连外网", 11.5, False, MUTED, first=True,
         space_before=0)
    tf = textbox(s, 9.02, 3.10, 4.0, 1.4)
    para(tf, "谁在哪、有没有在线、能不能干活", 11.5, False, MUTED, first=True,
         space_before=0, line=1.35)
    para(tf, "都是它自己报上来的", 11.5, False, MUTED, space_before=6)
    notes(s, "40 秒。这张图要讲的只有一句：装的位置由'谁够得着目标'决定。"
             "所以在自己机房、在客户内网、在流水线上，用的是同一套东西。")
    footer(s, 4, total, "")

    # ── 05 创新一 证据链 ─────────────────────────────────────
    s = blank(prs)
    head(s, "04 创新一", "每个结论都带一条证据链，不是一句空口断言",
         "从「想查什么」到「查到什么」到「因此是什么」——一路都挂着依据")
    chain = [("要查的目标", VIOLET), ("想查什么", RGBColor(0xBF, 0x5A, 0xF2)),
             ("查到的事实", CYAN), ("结论", RGBColor(0xFF, 0x9F, 0x0A)),
             ("影响的东西", RGBColor(0x64, 0xD2, 0xFF))]
    for i, (name, color) in enumerate(chain):
        x = 0.85 + i * 2.42
        card(s, x, 2.02, 1.85, 1.00, SURF2, edge=color)
        tf = textbox(s, x + 0.10, 2.28, 1.65, 0.5)
        para(tf, name, 14, True, TEXT, first=True, space_before=0,
             align=PP_ALIGN.CENTER)
        if i < 4:
            bar(s, x + 1.90, 2.47, 0.45, 0.06, LINE)

    bullet(s, 0.85, 3.32, 11.4, [
        ("不合格的结论进不去",
         "说不清依据、给不出复现步骤，系统当场拒收，而不是存下来等出报告时才发现"),
        ("结论一生成就挂在依据上",
         "不用事后补文档；过半年再翻，还看得出当时凭什么下这个判断"),
        ("查的时候能筛，底账不动",
         "界面上怎么过滤都不改数据，整条链路随时能导出、能离线看"),
        ("一个项目一份账",
         "不会出现「你这边验过、我这边还没验」的分裂"),
    ], size=13.5, gap=0.86)
    notes(s, "45 秒。用办案打比方：不是抓到人就算，得有证据链。"
             "这一页的重点是'当场拒收'——不合格的结论根本进不了库，事后不用再清理。")
    footer(s, 5, total, "")

    # ── 06 创新二 敢说不是 ───────────────────────────────────
    s = blank(prs)
    head(s, "05 创新二", "敢说「不是漏洞」，也敢说「我不知道」",
         "拿不准的交给人，不硬给一个结论——这是最难、也最值钱的一条")
    card(s, 0.85, 1.98, 3.55, 1.66, SURF1)
    tf = textbox(s, 1.10, 2.18, 3.05, 1.3)
    para(tf, "有问题的版本", 12.5, True, WARN, first=True, space_before=0)
    para(tf, "真打进去了", 19, True, TEXT, space_before=8)
    para(tf, "在隔离环境里真的打一次，有反应", 11, False, DIM, space_before=6)

    card(s, 4.62, 1.98, 3.55, 1.66, SURF1)
    tf = textbox(s, 4.87, 2.18, 3.05, 1.3)
    para(tf, "修好的版本", 12.5, True, OK, first=True, space_before=0)
    para(tf, "打不进去", 19, True, TEXT, space_before=8)
    para(tf, "同一套打法，打到修好的版本上", 11, False, DIM, space_before=6)

    card(s, 8.39, 1.98, 4.09, 1.66, SURF2, edge=OK)
    tf = textbox(s, 8.64, 2.18, 3.6, 1.3)
    para(tf, "两边对上，才算成立", 12.5, True, OK, first=True, space_before=0)
    para(tf, "修好的版本：0 条误报", 17, True, TEXT, space_before=8)
    para(tf, "不会把「看着像」报成漏洞", 11, False, DIM, space_before=6)

    bullet(s, 0.85, 4.02, 11.4, [
        ("打不动 ≠ 漏洞不存在",
         "环境不对、权限不够、目标报错——一律标「待人工确认」，不当误报，也不当成没问题"),
        ("不认编出来的行号",
         "模型说在第几行，对不上就丢掉，不许它「大概在第 40 行左右」"),
        ("没依据的猜测不立项",
         "拿不出依据的猜测，在生成阶段就被删掉，不会进到后面的流程"),
    ], size=13.5, gap=0.86)
    notes(s, "50 秒。这一页是全场最能打的：我们不是'报得多'，是'报得准、说得清'。"
             "两句要念出来：不确定不等于不存在；宁可交给人工复核，也不把没查清的事说成已排除。"
             "修好的版本扫出 0 条，是当场跑出来的，不是承诺。")
    footer(s, 6, total, "")

    # ── 07 创新三 三道锁 ─────────────────────────────────────
    s = blank(prs)
    head(s, "06 创新三", "三道锁同时开才行，而且只能收紧、不能放松",
         "写在提示里的话能糊弄模型，但糊弄不了这三道锁")
    gates = [
        ("第一道 · 能扫哪儿", "只放行写明的网段；写「随便扫」也要当场过一遍", ACCENT),
        ("第二道 · 谁能用哪些能力",
         "四档：只读 · 探测 · 审计 · 完全；只能在会话里往回收", CYAN),
        ("第三道 · 危险动作逐项批",
         "利用验证 / 口令测试 / 投递载荷 / 横向 / 外部工具，一项一项开", WARN),
    ]
    for i, (t, b, c) in enumerate(gates):
        y = 1.94 + i * 1.32
        card(s, 0.85, y, 5.55, 1.18, SURF1, edge=RGBColor(0x24, 0x24, 0x2A))
        bar(s, 0.85, y + 0.18, 0.08, 0.82, c)
        tf = textbox(s, 1.12, y + 0.16, 5.05, 0.95)
        para(tf, t, 14.5, True, TEXT, first=True, space_before=0)
        para(tf, b, 11, False, DIM, space_before=6, line=1.3)

    tf = textbox(s, 6.72, 1.92, 5.8, 0.4)
    para(tf, "为什么这三把锁拆不掉", 13, True, ACCENT_T, first=True, space_before=0)
    rows = [
        ("给清单 ≠ 给许可", "告诉它有哪些工具，不等于允许它动手；真动手之前再判一次"),
        ("对不上就起不来", "谁的权限漏写一项，程序直接报错，不允许「悄悄地放宽」"),
        ("两套身份不互通", "网页登录的账号拿不到干活的权限，反过来也一样"),
        ("工具返回的话也不可信", "它可能被返回内容带偏，所以每次动手前都重新判一次"),
    ]
    for i, (t, b) in enumerate(rows):
        yy = 2.42 + i * 1.06
        bar(s, 6.72, yy + 0.09, 0.08, 0.08, MUTED)
        tf2 = textbox(s, 6.98, yy - 0.04, 5.45, 1.0)
        para(tf2, t, 13, True, TEXT, first=True, space_before=0)
        para(tf2, b, 11, False, DIM, space_before=3, line=1.28)
    notes(s, "50 秒。这是评委最爱问的一页：这种工具自己会不会失控。"
             "一句话回答：能顺手放宽权限的开关，本身就是个后门，所以这种开关我们不放。"
             "门锁在系统里，不在提示词里。")
    footer(s, 7, total, "")

    # ── 08 创新四 对话 ───────────────────────────────────────
    s = blank(prs)
    head(s, "07 创新四", "像跟同事说话一样用它，但每一步都记账",
         "说人话下命令，动作还是走原来那套流程——没有一步能绕开")
    cards = [
        ("说人话就行", "查哪儿、要什么、出报告，一句话；"
                  "不用记命令、不用点十几层菜单", 0.85, 1.96),
        ("断线不丢活", "一步干完才记账，不会留下"
                  "「任务下了、没人对结果负责」的空账", 3.72, 1.96),
        ("几条线同时跑", "切走不打断，切回来接着看；"
                   "几个人同时用也不串台", 6.59, 1.96),
        ("一句话派多路", "一句话里提到两个目标，"
                   "自动分头去查，各自回来汇报", 9.46, 1.96),
    ]
    for t, b, x, y in cards:
        card(s, x, y, 2.74, 1.72, SURF1)
        tf = textbox(s, x + 0.24, y + 0.20, 2.30, 1.4)
        para(tf, t, 14, True, ACCENT_T, first=True, space_before=0)
        para(tf, b, 11, False, DIM, space_before=7, line=1.32)

    card(s, 0.85, 3.94, 11.35, 1.30, SURF2, edge=ACCENT)
    tf = textbox(s, 1.12, 4.14, 10.9, 1.0)
    para(tf, "口径可以按人来定，不是一个全局开关", 13, True, ACCENT_T, first=True,
         space_before=0)
    para(tf, "同一个人可以一边做合规评估、一边做对抗演练，两边口径不同、互不影响；"
             "做成一个总开关，切换就是打断别人正在做的事", 12, False, DIM,
         space_before=7, line=1.3)

    tf = textbox(s, 0.85, 5.46, 11.3, 1.1)
    para(tf, "最要紧的一条：它只会「决定做什么」，不会「自己做」——"
             "每一次动手，都还是走原来那套流程，该过的锁一道不少，该留的记录一条不缺",
         13.5, True, TEXT, first=True, space_before=0, line=1.35)
    notes(s, "50 秒。这一页回答'AI 会不会乱来'。答案是：它只负责决定该做什么，"
             "动手执行走的还是老流程。所以能力变强了，门禁一点没松。")
    footer(s, 8, total, "")

    # ── 09 三个限制变优势 ────────────────────────────────────
    s = blank(prs)
    head(s, "08 三个取舍", "三个「自己给自己加的限制」，反而成了优势")
    items = [
        ("不装任何外部组件", "只靠系统自带的东西",
         "内网断网也能跑；不用等审批装东西；没有「上游一升级、我们全崩」的风险", OK),
        ("一个程序加一个网页", "不用配数据库、不用起容器",
         "装完就能用；一台机器就是一个完整系统，出了问题只查一处", ACCENT),
        ("平时完全不连外网", "需要更强能力时才接上外部智能体",
         "默认不外联；要升级能力是一次显式动作，不是默认行为", CYAN),
    ]
    for i, (t, sub, body, c) in enumerate(items):
        y = 1.86 + i * 1.56
        card(s, 0.85, y, 11.63, 1.36, SURF1)
        bar(s, 0.85, y + 0.20, 0.09, 0.96, c)
        tf = textbox(s, 1.15, y + 0.18, 4.2, 1.05)
        para(tf, t, 16.5, True, TEXT, first=True, space_before=0)
        para(tf, sub, 11, False, MUTED, space_before=6)
        tf2 = textbox(s, 5.50, y + 0.30, 6.75, 0.95)
        para(tf2, body, 12, False, DIM, first=True, space_before=0, line=1.35)
    notes(s, "40 秒。这三条不是省事，是取舍。零依赖换来的是内网能跑、信创能过；"
             "单进程换来的是装得上；默认不外联换来的是能交代。")
    footer(s, 9, total, "")

    # ── 10 效益 ─────────────────────────────────────────────
    s = blank(prs)
    head(s, "09 效益", "四个维度：更快、更准、更省、更说得清",
         "能落到数字的给数字，落不到的就说清楚边界")
    dims = [
        ("更快", "过去几小时到几天，现在几十秒到几分钟",
         "三个样例项目当场跑：单文件约 1 秒；改动一处只查受影响的那几块", ACCENT),
        ("更准", "修好的版本实测 0 条误报",
         "报出来的每一条都能复现；拿不准的明确标「待人工确认」", OK),
        ("更省", "每千行代码的算力开销比人工低七成以上",
         "这是设计目标：改动多少查多少，不是每次全量重来", WARN),
        ("更说得清", "谁授权、扫了哪、凭什么下结论，都能翻出来",
         "每一步留痕；没有出厂口令；对外的模型默认关闭", CYAN),
    ]
    for i, (t, a, b, c) in enumerate(dims):
        x = 0.85 + (i % 2) * 6.02
        y = 1.92 + (i // 2) * 2.02
        card(s, x, y, 5.72, 1.78, SURF1)
        bar(s, x, y + 0.18, 0.09, 1.42, c)
        tf = textbox(s, x + 0.30, y + 0.16, 5.2, 1.5)
        para(tf, t, 16, True, c, first=True, space_before=0)
        para(tf, a, 13, True, TEXT, space_before=7, line=1.3)
        para(tf, b, 10.5, False, DIM, space_before=6, line=1.3)
    notes(s, "50 秒。「更省」那一条要主动说清是设计目标、还没在真实大项目上量过 —— "
             "把实测和估算分开讲，比含糊过去更可信。")
    footer(s, 10, total, "")

    # ── 11 凭什么信 ─────────────────────────────────────────
    s = blank(prs)
    head(s, "10 凭什么信", "每一条结论背后，都有可以自己复算的证据", "")
    kpi(s, 0.85, 1.90, 2.75, "3142", "项自检全部通过", OK, h=1.16)
    kpi(s, 3.72, 1.90, 2.75, "113", "组自检，覆盖各个角落", ACCENT, h=1.16)
    kpi(s, 6.59, 1.90, 2.75, "5.2 万行", "后端代码", CYAN, h=1.16)
    kpi(s, 9.46, 1.90, 3.02, "0", "需要另装的外部组件", VIOLET, h=1.16)

    bullet(s, 0.85, 3.36, 11.4, [
        ("秒级不是估出来的", "三个样例项目当场跑：单文件约 1 秒，跨三个文件也是秒级"),
        ("界面上的每个数字都能复算", "颜色、字号、对比度都有专门的小工具算给你看，不是拍脑袋"),
        ("一条命令起全套", "脚本只做三件事：切目录、挑 Python、把活交出去；外网入口一条隧道命令"),
        ("连「门锁」本身都有自检", "权限、范围、会话隔离都有用例守着，改坏了直接报错"),
    ], size=13.5, gap=0.88)
    notes(s, "40 秒。这一页只做一件事：把「我们说的」换成「你可以自己去看的」。"
             "3142 项自检是我们跑的，跑全了一千六百多秒，一条没错。")
    footer(s, 11, total, "")

    # ── 12 小结 ─────────────────────────────────────────────
    s = blank(prs)
    glow = s.shapes.add_shape(MSO_SHAPE.OVAL, Inches(-2.2), Inches(4.4),
                              Inches(7.0), Inches(7.0))
    glow.shadow.inherit = False
    glow.line.fill.background()
    glow.fill.solid()
    glow.fill.fore_color.rgb = RGBColor(0x10, 0x14, 0x1C)

    bar(s, 1.0, 1.16, 0.12, 0.72, ACCENT)
    tf = textbox(s, 1.30, 1.10, 10.8, 1.6)
    para(tf, "10 小结", 13, True, ACCENT_T, first=True, space_before=0)
    para(tf, "三句话讲完", 30, True, TEXT, space_before=8)

    pts = [
        ("有据可查", "每个结论都能翻到依据，换谁来复核都认"),
        ("越改越省", "只查改动影响到的地方，不是每次从头再来"),
        ("装得上、跑得动", "不挑环境，内网断网也能用，出事只查一处"),
    ]
    for i, (t, b) in enumerate(pts):
        y = 3.10 + i * 0.98
        card(s, 1.0, y, 9.6, 0.82, SURF1)
        tf = textbox(s, 1.28, y + 0.15, 9.1, 0.66)
        rich(tf, [(t, True, ACCENT_T), ("　" + b, False, DIM)], size=13.5,
             first=True, space_before=0)

    tf = textbox(s, 1.0, 6.28, 10.8, 0.5)
    para(tf, "值不值得投入，最终取决于是否认可：把「找漏洞」从一门手艺，变成一项工程",
         13, False, MUTED, first=True, space_before=0)
    notes(s, "40 秒。收尾就三句，不要再展开。最后一句留白，把时间交给提问。")
    footer(s, 12, total, "")

    prs.save(path)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="build the 8-minute release deck")
    parser.add_argument("--out", default="成果发布-8分钟.pptx")
    args = parser.parse_args(argv)
    out = os.path.abspath(args.out)
    build(out)
    print("wrote %s (%d bytes)" % (out, os.path.getsize(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
