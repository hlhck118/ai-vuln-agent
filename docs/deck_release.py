# -*- coding: utf-8 -*-
"""生成 8 分钟参赛汇报 PPT（.pptx）。

素材唯一来源：系统设计说明书（V1.0）与优秀 AI 应用项目设计大赛参赛方案书。
主线：**自动化漏洞发掘**——形势与困境、现有手段为何不够、七阶段闭环与五层架构、
七项创新、工程实测、效益与推广。措辞按正式汇报体，不写标语口号。

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

# 浅色阅读版：纸张白底 + 深墨字，正文对比度均在 7:1 以上，投影与打印同时可读
BG = RGBColor(0xFA, 0xFB, 0xFD)
SURF1 = RGBColor(0xFF, 0xFF, 0xFF)
SURF2 = RGBColor(0xF2, 0xF4, 0xF8)
EDGE = RGBColor(0xDD, 0xE2, 0xEA)
LINE = RGBColor(0xC9, 0xCF, 0xDA)
TEXT = RGBColor(0x16, 0x18, 0x1D)
DIM = RGBColor(0x45, 0x4A, 0x54)
MUTED = RGBColor(0x6B, 0x71, 0x7C)
ACCENT = RGBColor(0x0B, 0x5C, 0xC4)
ACCENT_T = RGBColor(0x0B, 0x5C, 0xC4)
OK = RGBColor(0x0F, 0x71, 0x39)
WARN = RGBColor(0x8A, 0x60, 0x00)
DANGER = RGBColor(0xB3, 0x24, 0x1A)
CYAN = RGBColor(0x0B, 0x62, 0x7A)
VIOLET = RGBColor(0x45, 0x3F, 0xAE)
AMBER = RGBColor(0x96, 0x52, 0x08)
PURPLE = RGBColor(0x63, 0x22, 0xC4)
BLUEL = RGBColor(0x0A, 0x55, 0x7C)

FONT = "等线"


def set_font(run, size, bold=False, color=TEXT, name=FONT):
    if size < 11:
        size = size + 1.0          # 小字统一放大一档，浅底细体更易读
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


def para(tf, text, size=16, bold=False, color=TEXT, first=False,
         space_before=6, line=1.30, align=PP_ALIGN.LEFT):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.alignment = align
    p.line_spacing = line
    p.space_before = Pt(0 if first else space_before)
    p.space_after = Pt(0)
    run = p.add_run()
    run.text = text
    set_font(run, size, bold, color)
    return p


def rich(tf, parts, size=15, first=False, space_before=6, line=1.28):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.line_spacing = line
    p.space_before = Pt(0 if first else space_before)
    p.space_after = Pt(0)
    for text, bold, color in parts:
        run = p.add_run()
        run.text = text
        set_font(run, size, bold, color)
    return p


def blank(prs):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0,
                                Inches(W), Inches(H))
    bg.line.fill.background()
    bg.shadow.inherit = False
    bg.fill.solid()
    bg.fill.fore_color.rgb = BG
    return slide


def card(slide, x, y, w, h, fill=SURF1, edge=EDGE, radius=0.06):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                   Inches(x), Inches(y), Inches(w), Inches(h))
    shape.adjustments[0] = radius
    shape.shadow.inherit = False
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    if edge is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = edge
        shape.line.width = Pt(0.9)
    return shape


def bar(slide, x, y, w, h, color):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                   Inches(x), Inches(y), Inches(w), Inches(h))
    shape.shadow.inherit = False
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


def head(slide, kicker, title, sub=None):
    bar(slide, 0.85, 0.58, 0.10, 0.58, ACCENT)
    tf = textbox(slide, 1.10, 0.50, 11.3, 1.05)
    para(tf, kicker, 12, True, ACCENT_T, first=True, space_before=0)
    para(tf, title, 25, True, TEXT, space_before=4, line=1.16)
    if sub:
        tf2 = textbox(slide, 1.10, 1.46, 11.3, 0.44)
        para(tf2, sub, 12.5, False, DIM, first=True, space_before=0)


def footer(slide, idx, total, note=""):
    tf = textbox(slide, 0.85, 6.96, 10.4, 0.30)
    para(tf, note, 9.5, False, MUTED, first=True, space_before=0)
    tf2 = textbox(slide, 11.7, 6.96, 0.95, 0.30)
    para(tf2, "%02d / %02d" % (idx, total), 9.5, False, MUTED, first=True,
         space_before=0, align=PP_ALIGN.RIGHT)


def notes(slide, text):
    slide.notes_slide.notes_text_frame.text = text


def stat(slide, x, y, w, value, label, color=ACCENT, h=1.16, vs=22):
    card(slide, x, y, w, h, SURF1)
    bar(slide, x, y + 0.18, 0.06, h - 0.36, color)
    tf = textbox(slide, x + 0.24, y + 0.16, w - 0.42, h - 0.24)
    para(tf, value, vs, True, color, first=True, space_before=0)
    para(tf, label, 10.5, False, DIM, space_before=3, line=1.2)


def bullet(slide, x, y, w, items, size=13, gap=0.72, mark_color=ACCENT):
    for i, (title, body) in enumerate(items):
        yy = y + i * gap
        bar(slide, x, yy + 0.10, 0.08, 0.08, mark_color)
        tf = textbox(slide, x + 0.25, yy - 0.02, w, gap)
        if body:
            rich(tf, [(title, True, TEXT), ("　" + body, False, DIM)],
                 size=size, first=True, space_before=0)
        else:
            para(tf, title, size, True, TEXT, first=True, space_before=0)


def build(path):
    prs = Presentation()
    prs.slide_width = Inches(W)
    prs.slide_height = Inches(H)
    total = 17

    # ── 01 封面 ──────────────────────────────────────────────
    s = blank(prs)
    bar(s, 1.0, 1.66, 0.12, 0.78, ACCENT)
    tf = textbox(s, 1.30, 1.50, 10.2, 2.4)
    para(tf, "优 秀 A I 应 用 项 目 设 计 大 赛 · 汇 报", 12.5, True, ACCENT_T,
         first=True, space_before=0)
    para(tf, "基于 AI 智能体的自动化漏洞发掘系统", 34, True, TEXT,
         space_before=12, line=1.16)
    para(tf, "技术创新研究 · 成果汇报", 18, False, DIM, space_before=12)

    card(s, 1.0, 4.40, 8.20, 0.62, SURF1)
    tf = textbox(s, 1.26, 4.54, 7.8, 0.36)
    para(tf, "规划—探索—验证—复盘四阶段智能体 · 证据链驱动 · 全离线私有化",
         12, False, MUTED, first=True, space_before=0)

    stats = [("七", "阶段端到端闭环"), ("五", "层体系架构"),
             ("七", "项技术创新"), ("100", "％结论可复现")]
    for i, (v, l) in enumerate(stats):
        x = 1.0 + i * 1.90
        tf = textbox(s, x, 5.42, 1.80, 0.9)
        para(tf, v, 22, True, ACCENT_T, first=True, space_before=0)
        para(tf, l, 10, False, MUTED, space_before=2)
    notes(s, "15 秒。开场说明汇报对象：一套把漏洞发掘从人工经验判断，"
             "改成四阶段智能体协同、每一步都有证据支撑的系统。"
             "四个数字分别是七阶段闭环、五层架构、七项创新、结论可复现率百分之百。")
    footer(s, 1, total, "")

    # ── 02 形势 ──────────────────────────────────────────────
    s = blank(prs)
    head(s, "一、形势", "漏洞披露量在涨，可修补的时间窗口在缩",
         "攻防两侧同时加速，防御侧仍以人工方式主导，这个速度差就是立项起点")
    nums = [("4.8 万", "2025 年全球新披露漏洞，较上年增约 20%", DANGER),
            ("28%", "其中完成完整分析与富化的比例", WARN),
            ("5 天", "披露到在野利用的平均间隔，2018 年为 63 天", AMBER),
            ("1%", "确认在野利用的比例，约 422 个", CYAN)]
    for i, (v, l, c) in enumerate(nums):
        x = 0.85 + i * 2.86
        card(s, x, 1.86, 2.72, 1.30, SURF1)
        bar(s, x, 2.04, 0.06, 0.94, c)
        tf = textbox(s, x + 0.24, 2.02, 2.34, 1.0)
        para(tf, v, 21, True, c, first=True, space_before=0)
        para(tf, l, 9.5, False, DIM, space_before=3, line=1.28)

    bullet(s, 0.85, 3.36, 11.4, [
        ("窗口以小时计", "约 28% 的漏洞在披露后 24 小时内即出现利用；"
                    "CVE-2025-55182 公开不到一个月，已累积 236 个有效公开利用"),
        ("告警与结论脱节", "披露量大、分析量小，未经富化的告警无法直接支撑修补决策"),
        ("防御侧靠人力", "ISC2 2024 统计全球从业约 550 万、缺口 480 万；"
                    "国内 2027 年预计缺口 327 万，2024 年底持证在岗约 32 万"),
    ], size=12.5, gap=0.78)

    card(s, 0.85, 5.72, 11.35, 0.94, SURF2, edge=ACCENT)
    tf = textbox(s, 1.12, 5.86, 10.9, 0.72)
    para(tf, "结论：漏洞发现必须从「想起来做一次」变成「跟着变更节奏反复做」，"
             "而人工方式最撑不住的正是重复", 12.5, True, ACCENT_T, first=True,
         space_before=0, line=1.32)
    notes(s, "30 秒。四个数字：披露量四点八万、完成分析只有两成八、"
             "披露到利用五天、真正被利用的百分之一。"
             "再叠加人力缺口——每年新增的漏洞远超分析人力的增长速度。")
    footer(s, 2, total, "数据来源：参赛方案书第一章（VulnCheck 2025、Google Cloud、ISC2 2024）")

        # ── 03 AI 在网络攻击中的用法 ─────────────────────────────
    s = blank(prs)
    head(s, "一、形势", "AI 已经嵌进攻击的每一个环节",
         "六项用法没有一项是新技术，改变的是速度、规模，以及这些环节能不能被自动串起来")
    stages = [
        ("情报收集", DANGER,
         "整理官网、招聘、代码仓库等公开信息，归并组织架构与人员清单；批量枚举子域与暴露面",
         "暴露面被梳理得更完整，过去长期没人看的资产也会被发现"),
        ("社会工程", WARN,
         "按职务与业务场景撰写邮件与消息，称呼准确、语言通顺；语音合成与视频换脸冒充上级",
         "「语言生硬、模板化」这类传统识别线索减弱"),
        ("漏洞发现", AMBER,
         "辅助阅读代码与流量定位可疑输入点；对公开漏洞快速产出可用的验证代码",
         "从漏洞公开到出现可用利用的间隔缩短，打补丁的窗口变窄"),
        ("载荷构造", CYAN,
         "改写与混淆恶意脚本，使每次生成的样本在文件哈希与固定字符串上都不相同",
         "哈希黑名单与固定特征匹配的效果下降"),
        ("权限维持与横向", VIOLET,
         "解析主机配置与日志判断哪条路径通向更高权限，把上一步的结果作为下一步输入",
         "人工介入点减少，一次入侵可以被推进得更深更快"),
        ("运营与迭代", BLUEL,
         "批量管理多个目标、汇总结果、按失败原因自动调整策略",
         "单人可同时推进的目标数量上升，投入不再与目标数线性相关"),
    ]
    # 六张卡等高、同列同基线：做法固定占两行高度，影响另起一框钉在同一 y 上，
    # 于是「影响」六个字在整页连成一条水平线，卡片底部余量也就不再随文字长短参差。
    for i, (name, color, how, impact) in enumerate(stages):
        x = 0.85 + (i % 3) * 3.86
        y = 1.80 + (i // 3) * 1.82
        card(s, x, y, 3.60, 1.70, SURF1, edge=EDGE)
        bar(s, x, y + 0.14, 3.60, 0.06, color)
        tf = textbox(s, x + 0.24, y + 0.30, 3.14, 0.32)
        para(tf, name, 13, True, color, first=True, space_before=0)
        tf2 = textbox(s, x + 0.24, y + 0.64, 3.12, 0.44)
        para(tf2, how, 9, False, DIM, first=True, space_before=0, line=1.24)
        tf3 = textbox(s, x + 0.24, y + 1.14, 3.12, 0.50)
        rich(tf3, [("影响　", True, color), (impact, False, MUTED)],
             size=9, first=True, space_before=0, line=1.24)

    # 结尾卡沿用第 2 页的基准（5.72 / 0.94），页脚之上留 0.30，与前一页同一条收尾线
    card(s, 0.85, 5.72, 11.35, 0.94, SURF2, edge=ACCENT)
    tf = textbox(s, 1.12, 5.92, 10.9, 0.60)
    para(tf, "结论：防守侧真正缺的不是更快的工具，而是能被复核的结论——"
             "工具再快，判断仍要落在证据上", 13, True, ACCENT_T, first=True,
         space_before=0, line=1.32)
    notes(s, "25 秒。六项用法都不是新技术，改变的是每一项的完成速度与规模，"
             "以及这些环节能不能被串成一条自动流水线。"
             "所以本系统的重心不在扫得更快，而在每条结论都带证据、可复核。")
    footer(s, 3, total, "内容为对公开报道与公开研究的归纳，定性陈述，不含本系统运行数据")

    # ── 04 三重困境 ──────────────────────────────────────────
    s = blank(prs)
    head(s, "二、问题分析", "人力、工具、制度三条线，各卡在一个环节",
         "把三类现实约束放在一起看，能看出同一个缺口")
    cols = [
        ("人力供给", DANGER,
         ["资深安全顾问 3000–6000 元／人天",
          "代码审计 1000–3000 元／千行",
          "经验留在个人身上，人员流动即失效"]),
        ("工具现状", WARN,
         ["单一 SAST 仅在 52% 的漏洞引入函数告警",
          "这些函数内逾 76% 的告警与漏洞无关",
          "漏检 22% 的漏洞引入提交"]),
        ("制度要求", CYAN,
         ["漏洞须在 2 日内报送工信部平台",
          "修补前不得公开、不得向境外提供",
          "关基运营者每年至少一次检测评估"]),
    ]
    for i, (name, color, rows) in enumerate(cols):
        x = 0.85 + i * 3.86
        card(s, x, 1.86, 3.60, 2.70, SURF1, edge=EDGE)
        bar(s, x, 2.02, 3.60, 0.06, color)
        tf = textbox(s, x + 0.24, 2.22, 3.14, 0.40)
        para(tf, name, 13.5, True, color, first=True, space_before=0)
        for j, row in enumerate(rows):
            bar(s, x + 0.26, 2.78 + j * 0.58, 0.07, 0.07, MUTED)
            tf2 = textbox(s, x + 0.46, 2.70 + j * 0.58, 2.94, 0.54)
            para(tf2, row, 10, False, DIM, first=True, space_before=0, line=1.28)

    tf = textbox(s, 0.85, 4.74, 11.4, 1.5)
    para(tf, "动态手段同样受限：Project Zero 用 AFL 对 SQLite 该缺陷持续跑 150 CPU 小时，仍未触发",
         12, False, TEXT, first=True, space_before=0, line=1.32)
    para(tf, "模糊测试的覆盖依赖种子与变异策略，遇到需要跨模块语义才能到达的逻辑缺陷，"
             "成功率迅速下降", 12, False, DIM, space_before=6, line=1.32)

    card(s, 0.85, 5.96, 11.35, 0.86, SURF2, edge=ACCENT)
    tf = textbox(s, 1.12, 6.10, 10.9, 0.64)
    para(tf, "共同缺口：发现靠人、判断靠经验、结论缺证据，三者都无法随变更节奏复制",
         12.5, True, ACCENT_T, first=True, space_before=0)
    notes(s, "30 秒。三条线：人力贵且留不住经验；工具告警多但准确率低；"
             "制度要求两日内报送、修补前不得公开。"
             "动态扫描也不是万能——AFL 跑一百五十个 CPU 小时都没触发那个缺陷。")
    footer(s, 4, total, "数据来源：参赛方案书第二章（ISSTA 2024 实证、Project Zero 实验）")

    # ── 05 现有 AI 的边界 ────────────────────────────────────
    s = blank(prs)
    head(s, "二、问题分析", "把提示词交给大模型，并不等于能挖漏洞",
         "公开基准的实测结果说明：通用智能体的自主完成率离工程可用还有距离")
    rows = [
        ("CVE-Bench", "13%", "最佳成绩，端到端复现真实 CVE", DANGER),
        ("CyberGym", "20%", "真实项目环境下的漏洞复现成功率", WARN),
        ("AutoPenBench", "21% / 64%", "完全自主 21%，人机协同 64%", AMBER),
        ("脆弱与已修复代码判别", "0.06–0.14", "大模型区分二者与随机的准确率区间", CYAN),
    ]
    for i, (name, val, desc, c) in enumerate(rows):
        y = 1.86 + i * 0.92
        card(s, 0.85, y, 11.35, 0.80, SURF1)
        bar(s, 0.85, y + 0.14, 0.06, 0.52, c)
        tf = textbox(s, 1.12, y + 0.22, 3.4, 0.40)
        para(tf, name, 12.5, True, TEXT, first=True, space_before=0)
        tf2 = textbox(s, 4.60, y + 0.16, 2.3, 0.50)
        para(tf2, val, 17, True, c, first=True, space_before=0)
        tf3 = textbox(s, 7.10, y + 0.24, 4.9, 0.40)
        para(tf3, desc, 11, False, DIM, first=True, space_before=0)

    tf = textbox(s, 0.85, 5.60, 11.4, 1.2)
    para(tf, "原因不在模型不够强，而在漏洞发掘本身是一个需要外部事实验证的任务："
             "模型给出的是判断，不是证据", 12.5, True, TEXT, first=True,
         space_before=0, line=1.34)
    para(tf, "因此技术空间不在「让模型更敢说」，而在用程序分析、沙箱执行与差分对照，"
             "结构性地补偿模型的不确定性", 12, False, DIM, space_before=8, line=1.34)
    notes(s, "35 秒。这四个数字是全篇的立论基础：通用智能体直接上，"
             "端到端复现也就一到两成；连区分脆弱代码和已修复代码都接近随机。"
             "所以我们的路线不是把模型换得更大，而是给它加外部事实约束。")
    footer(s, 5, total, "数据来源：参赛方案书 1.3 节（CVE-Bench、CyberGym、AutoPenBench 及判别实验）")

    # ── 06 七阶段闭环 ────────────────────────────────────────
    s = blank(prs)
    head(s, "三、总体方案", "七阶段端到端闭环，每阶段有确定产出",
         "前一阶段的产出是后一阶段的输入；任一阶段不成立，结论就不成立")
    steps = ["接入与基线", "侦察与建模", "规划与预算", "假设生成",
             "反证与筛选", "验证与定级", "报告与闭环"]
    for i, name in enumerate(steps):
        x = 0.85 + i * 1.60
        card(s, x, 1.86, 1.42, 1.00, SURF1,
             edge=ACCENT if i < 5 else OK)
        tf = textbox(s, x + 0.06, 1.96, 1.30, 0.82)
        para(tf, "%02d" % (i + 1), 10, True, ACCENT_T if i < 5 else OK,
             first=True, space_before=0, align=PP_ALIGN.CENTER)
        para(tf, name, 9.5, True, TEXT, space_before=4, line=1.2,
             align=PP_ALIGN.CENTER)

    three = [
        ("接入与建模", "代码仓库接入、指纹清单建基线；解析符号与调用关系，"
                  "形成入口、汇聚点与可达路径", CYAN),
        ("假设与反证", "每条假设必须挂载静态证据或图谱证据；先主动构造推翻条件，"
                  "带着主观确认往下跑是最常见的失效路径", AMBER),
        ("验证与闭环", "隔离环境执行验证用例，缺陷版与修复版两侧对照；"
                  "结论、证据、合规要素四份产物同源生成", VIOLET),
    ]
    for i, (t, b, c) in enumerate(three):
        x = 0.85 + i * 3.86
        card(s, x, 3.10, 3.60, 1.94, SURF1, edge=EDGE)
        bar(s, x, 3.10, 3.60, 0.05, c)
        tf = textbox(s, x + 0.26, 3.30, 3.14, 1.66)
        para(tf, t, 12.5, True, TEXT, first=True, space_before=0)
        para(tf, b, 10, False, DIM, space_before=7, line=1.36)

    tf = textbox(s, 0.85, 5.30, 11.4, 1.5)
    para(tf, "七个阶段都定义了失败出口：目录不可读判任务失败；语法错误文件跳过并记录；"
             "超出预算的切片记为未审计", 12, False, TEXT, first=True, space_before=0,
         line=1.34)
    para(tf, "模型不可用时降级为确定性推理；验证跑不起来时标注待人工确认——"
             "既不当成漏洞，也不当成不存在", 12, False, DIM, space_before=7, line=1.34)
    para(tf, "这一条决定系统能否交付：把不确定静默算成没有，比没有结论更危险", 12.5,
         True, TEXT, space_before=8, line=1.34)
    notes(s, "40 秒。七阶段是一条串行链，重点讲最后一句：每个阶段都有失败出口。"
             "不确定必须有地方去，不能悄悄消失。")
    footer(s, 6, total, "流水线实现：vulnagent（config · resolver · analysis · agents · orchestrator · tools · report）")

    # ── 07 五层架构 ─────────────────────────────────────────
    s = blank(prs)
    head(s, "三、总体方案", "五层体系架构，模块职责与失败处理一一对应",
         "分层的目的不是画图好看，而是让每层的降级行为可以被单独定义和测试")
    layers = [
        ("接入层", "config.py · cli.py", "配置装载、参数校验、扫描入口；目录不可读即任务失败", ACCENT),
        ("编排层", "orchestrator.py · incremental.py · llm.py", "预算控制、阶段调度、增量计划、模型路由与降级", CYAN),
        ("智能体层", "agents.py", "侦察、规划、审计、反证、验证、利用链、报告七个角色 + 共享黑板", VIOLET),
        ("工具层", "analysis.py · tools.py", "AST 分析、沙箱执行、差分验证；子进程超时 20 秒、输出截断 64KB", AMBER),
        ("知识层", "resolver.py · memory.py", "调用图索引、经验记忆库；符号解析带置信度，解析不到记 unknown", OK),
    ]
    for i, (name, mods, duty, c) in enumerate(layers):
        y = 1.86 + i * 0.78
        card(s, 0.85, y, 11.35, 0.68, SURF1)
        bar(s, 0.85, y, 0.06, 0.68, c)
        tf = textbox(s, 1.10, y + 0.20, 1.30, 0.36)
        para(tf, name, 12, True, c, first=True, space_before=0)
        tf2 = textbox(s, 2.50, y + 0.22, 3.60, 0.36)
        para(tf2, mods, 9.5, False, BLUEL, first=True, space_before=0)
        tf3 = textbox(s, 6.30, y + 0.22, 5.70, 0.36)
        para(tf3, duty, 9.5, False, DIM, first=True, space_before=0)

    card(s, 0.85, 5.86, 11.35, 0.94, SURF2, edge=ACCENT)
    tf = textbox(s, 1.12, 5.98, 10.9, 0.78)
    para(tf, "预算默认值：单任务最多 8 轮、60 次工具调用、40 万 token、100 个切片、"
             "每切片 3 条假设", 11.5, True, ACCENT_T, first=True, space_before=0,
         line=1.3)
    para(tf, "上下文预算按比例分配：代码切片约 50%、图谱 10%、静态证据 20%、"
             "历史案例 10%（最多 2 条）、输出约束 10%", 10.5, False, DIM,
         space_before=5, line=1.3)
    notes(s, "35 秒。五层从上到下：接入、编排、智能体、工具、知识。"
             "重点在每层都有明确的降级行为，以及预算和上下文的硬上限——"
             "成本可控是设计出来的，不是省出来的。")
    footer(s, 7, total, "技术栈：Python 3.10+，解析用标准库 ast，持久化用 sqlite3，模型接入不依赖第三方 SDK")

    # ── 08 创新一：多智能体 ─────────────────────────────────
    s = blank(prs)
    head(s, "四、技术创新（一）", "多智能体分工协作，共享黑板统一事实",
         "七个角色各自有输入、输出与预算，避免单一上下文里既要发现又要自证")
    roles = [("侦察", "入口与暴露面", ACCENT), ("规划", "切片与优先级", CYAN),
             ("审计", "提出假设", VIOLET), ("反证", "构造推翻条件", AMBER),
             ("验证", "沙箱执行", OK), ("利用链", "串联影响", PURPLE),
             ("报告", "证据与合规", BLUEL)]
    for i, (name, duty, c) in enumerate(roles):
        x = 0.85 + i * 1.62
        card(s, x, 1.86, 1.50, 0.94, SURF1, edge=c)
        tf = textbox(s, x + 0.08, 1.98, 1.34, 0.76)
        para(tf, name, 12, True, c, first=True, space_before=0,
             align=PP_ALIGN.CENTER)
        para(tf, duty, 8.5, False, DIM, space_before=4, line=1.2,
             align=PP_ALIGN.CENTER)

    mech = [
        ("共享黑板", "事实、假设、证据集中登记，任一角色可读，避免同一结论被重复推导三次", CYAN),
        ("预算约束", "轮次、工具调用、token 三重上限；触顶即安全早停并输出已有结论", WARN),
        ("失败升级", "角色连续失败则上调验证强度或转人工，而不是无限重试烧预算", VIOLET),
    ]
    for i, (t, b, c) in enumerate(mech):
        x = 0.85 + i * 3.86
        card(s, x, 3.02, 3.60, 1.62, SURF1, edge=EDGE)
        bar(s, x, 3.02, 3.60, 0.05, c)
        tf = textbox(s, x + 0.26, 3.22, 3.14, 1.34)
        para(tf, t, 12.5, True, TEXT, first=True, space_before=0)
        para(tf, b, 10, False, DIM, space_before=7, line=1.36)

    tf = textbox(s, 0.85, 4.94, 11.4, 1.6)
    para(tf, "职责分离带来的直接收益是减少自我确认：提出假设的角色不负责证明假设，"
             "负责验证的角色不参与构造假设", 12.5, True, TEXT, first=True,
         space_before=0, line=1.34)
    para(tf, "反证角色是这套分工里最关键的一环——它的任务是把假设推翻，"
             "而不是把它做实；能推翻的先推翻，剩下的才进入验证", 12, False, DIM,
         space_before=7, line=1.34)
    notes(s, "30 秒。七个角色不是七个提示词，是七个带独立输入输出和预算的执行单元。"
             "关键设计是反证角色——专门负责推翻，降低自我确认。")
    footer(s, 8, total, "实现：vulnagent/agents.py（角色定义、黑板、预算与升级策略）")

    # ── 09 创新二：语义图谱约束的混合检索 ───────────────────
    s = blank(prs)
    head(s, "四、技术创新（二）", "语义图谱约束的混合检索，先证明可达再谈漏洞",
         "向量、图谱、关键词三路召回后融合重排，按入口—传播链—汇聚点组装上下文")
    card(s, 0.85, 1.86, 6.20, 3.30, SURF1)
    chain = [("controller.handle", "入口：HTTP 处理", ACCENT),
             ("service.load_profile", "传播：业务层转发", CYAN),
             ("dao.find_user", "汇聚：数据访问", VIOLET),
             ("cur.execute", "缺陷点：SQL 拼接", DANGER)]
    for i, (code, note, color) in enumerate(chain):
        y = 2.06 + i * 0.76
        card(s, 1.12, y, 5.66, 0.60, SURF2, edge=color)
        tf = textbox(s, 1.34, y + 0.09, 5.2, 0.46)
        para(tf, code, 10.5, True, TEXT, first=True, space_before=0)
        para(tf, note, 9, False, DIM, space_before=1)
        if i < 3:
            bar(s, 3.90, y + 0.62, 0.05, 0.12, LINE)

    tf = textbox(s, 7.35, 1.86, 5.1, 0.36)
    para(tf, "样例目标 webapp（3 文件 / 97 行）解析统计", 11, True, ACCENT_T,
         first=True, space_before=0)
    grid = [("20", "调用点", TEXT), ("10", "内部解析", OK),
            ("1", "外部调用", CYAN), ("5", "无法静态确定", WARN)]
    for i, (v, l, c) in enumerate(grid):
        x = 7.35 + (i % 2) * 2.60
        y = 2.30 + (i // 2) * 1.04
        card(s, x, y, 2.45, 0.92, SURF1)
        tf2 = textbox(s, x + 0.22, y + 0.11, 2.1, 0.74)
        para(tf2, v, 17, True, c, first=True, space_before=0)
        para(tf2, l, 9.5, False, DIM, space_before=2)

    card(s, 7.35, 4.44, 5.13, 0.72, SURF2, edge=WARN)
    tf = textbox(s, 7.60, 4.56, 4.7, 0.54)
    para(tf, "解析率 50%；其余 5 处记 unknown 0.0，不按不可达处理", 10.5, True,
         TEXT, first=True, space_before=0, line=1.26)

    bullet(s, 0.85, 5.32, 11.4, [
        ("三路召回", "向量相似度解决语义相近、调用图解决结构可达、关键词解决标识符精确匹配，三路融合重排"),
        ("置信度分级", "模块级函数与类实例化 1.0、跨模块 import 0.9、self 属性 0.9、"
                  "基类回溯 0.7、标准库 external 0.6–0.7、无法确定 0.0"),
        ("漏报主战场", "缺陷在数据层、入口在控制层，只做单文件近邻匹配会把真实可达的注入判为不可达"),
    ], size=11.5, gap=0.54)
    notes(s, "40 秒。这一页讲两件事：一是检索不是向量相似度一条路，"
             "要有调用图约束；二是解析有边界，解析不出来要标 unknown，不能默认不可达。"
             "样例里二十个调用点，解析率五成，剩下五处明确标注。")
    footer(s, 9, total, "实现：vulnagent/resolver.py（作用域、导入、轻量类型推断、基类回溯）")

    # ── 10 创新三：双向反馈 ─────────────────────────────────
    s = blank(prs)
    head(s, "四、技术创新（三）", "大模型与程序分析双向反馈",
         "模型提假设、分析工具供事实、执行结果回灌下一轮，形成闭环而不是单向调用")
    card(s, 0.85, 1.86, 11.35, 1.56, SURF1, edge=CYAN)
    tf = textbox(s, 1.12, 2.00, 3.0, 0.34)
    para(tf, "① 模型 → 分析工具", 12, True, CYAN, first=True, space_before=0)
    tf = textbox(s, 1.12, 2.38, 10.85, 0.94)
    para(tf, "假设转成可执行的分析任务：为疑似解析漏洞生成定向模糊测试种子与 harness、"
             "为约束满足类缺陷生成符号执行条件、为污点传播类缺陷生成定向污点查询",
         11, False, DIM, first=True, space_before=0, line=1.36)

    card(s, 0.85, 3.54, 11.35, 1.56, SURF1, edge=OK)
    tf = textbox(s, 1.12, 3.68, 3.0, 0.34)
    para(tf, "② 分析工具 → 模型", 12, True, OK, first=True, space_before=0)
    tf = textbox(s, 1.12, 4.06, 10.85, 0.94)
    para(tf, "执行事实回灌：覆盖率增量、崩溃样本、符号执行的可行路径与不可行分支、"
             "污点查询结果，作为下一轮假设的证据与剪枝依据", 11, False, DIM,
         first=True, space_before=0, line=1.36)

    bullet(s, 0.85, 5.26, 11.4, [
        ("为什么必须双向", "单向调用下模型只能凭代码文本猜测；回灌后每一轮都在缩小搜索空间"),
        ("与纯 fuzz 的差别", "纯 fuzz 靠变异碰运气，这里由假设指定变异方向，种子与约束都带着目标"),
        ("可观测性", "每一轮的覆盖率增量与剪枝记录都进运行轨迹，可复盘为何没找到"),
    ], size=11.5, gap=0.54)
    notes(s, "35 秒。这一页回答「和大模型直接读代码有什么区别」："
             "模型不只是读，它把假设变成可执行的定向分析任务；工具的结果再回灌给模型。"
             "这样每一轮都在缩小搜索空间，而不是重复猜。")
    footer(s, 10, total, "对应参赛方案书创新点三；执行侧由 tools.py 的白名单工具承载")

    # ── 11 创新四：差分验证 ─────────────────────────────────
    s = blank(prs)
    head(s, "四、技术创新（四）", "沙箱差分验证为准绳，判定不由模型给出",
         "同一组用例分别跑缺陷版与修复版，两侧对照裁决；执行失败一律不下结论")
    chips = ["SQL 注入", "命令注入", "路径穿越", "反序列化",
             "弱哈希", "代码注入", "SSRF", "XSS"]
    tf = textbox(s, 0.85, 1.80, 4.0, 0.30)
    para(tf, "八类 PoC 模板", 10.5, True, ACCENT_T, first=True, space_before=0)
    for i, name in enumerate(chips):
        x = 0.85 + i * 1.40
        card(s, x, 2.12, 1.32, 0.54, SURF2, edge=EDGE)
        tf2 = textbox(s, x + 0.06, 2.26, 1.20, 0.28)
        para(tf2, name, 9.5, False, TEXT, first=True, space_before=0,
             align=PP_ALIGN.CENTER)

    rows = [("缺陷版复现成功，修复版未复现", "verified　已验证漏洞", OK),
            ("缺陷版执行正常但未复现", "refuted", DIM),
            ("两版都复现（疑似环境相关）", "inconclusive", WARN),
            ("缺基线，或修复版执行失败", "inconclusive", WARN),
            ("执行失败 / 超时 / 用例异常", "inconclusive　严禁判误报", DANGER),
            ("无法构造可执行用例", "confirmed_static　仅静态确认", CYAN)]
    for i, (cond, verdict, c) in enumerate(rows):
        y = 2.84 + i * 0.56
        card(s, 0.85, y, 11.35, 0.48, SURF1 if i % 2 == 0 else SURF2)
        tf = textbox(s, 1.10, y + 0.12, 6.4, 0.28)
        para(tf, cond, 10.5, False, TEXT, first=True, space_before=0)
        tf2 = textbox(s, 7.70, y + 0.12, 4.3, 0.28)
        para(tf2, verdict, 10.5, True, c, first=True, space_before=0)

    tf = textbox(s, 0.85, 6.32, 11.4, 0.5)
    para(tf, "口径只有一句：不确定不等于不存在。误报控制的实质不是调高阈值，"
             "而是让每条结论都带一组可复现的对照实验", 11.5, True, TEXT,
         first=True, space_before=0, line=1.3)
    notes(s, "40 秒。这是质量主张的核心。表格下半部分全部是 inconclusive，"
             "特别强调执行失败严禁判误报——这是同类工具最常见的失分点。")
    footer(s, 11, total, "沙箱参数：--network none --memory 512m --cpus 1 --pids-limit 128 --read-only no-new-privileges")

    # ── 12 创新五：幻觉抑制三道闸 ───────────────────────────
    s = blank(prs)
    head(s, "四、技术创新（五）", "幻觉抑制三道闸，结论必须过三道才允许输出",
         "证据绑定、模型交叉、可执行验证，三者取交集而不是任一通过")
    gates = [
        ("第一道　证据绑定", "每条结论必须挂到具体文件、行号与调用边；缺少证据的结论在生成阶段即被剔除", ACCENT),
        ("第二道　双模型交叉", "假设与判定分角色、分模型执行，判定用低温度；两个角色对同一结论不一致时降级为待确认", CYAN),
        ("第三道　可执行验证", "能用沙箱复现的必须复现；不能复现的只能标静态确认，不得升级为已验证", AMBER),
    ]
    for i, (t, b, c) in enumerate(gates):
        y = 1.86 + i * 1.24
        card(s, 0.85, y, 11.35, 1.10, SURF1, edge=EDGE)
        bar(s, 0.85, y, 0.06, 1.10, c)
        tf = textbox(s, 1.12, y + 0.16, 4.0, 0.34)
        para(tf, t, 12.5, True, TEXT, first=True, space_before=0)
        tf2 = textbox(s, 1.12, y + 0.54, 10.7, 0.50)
        para(tf2, b, 10.5, False, DIM, first=True, space_before=0, line=1.30)

    card(s, 0.85, 5.58, 11.35, 1.02, SURF2, edge=ACCENT)
    tf = textbox(s, 1.12, 5.72, 10.9, 0.80)
    para(tf, "设计取舍：宁可输出待人工确认，也不输出未经验证的肯定结论", 12.5, True,
         ACCENT_T, first=True, space_before=0)
    para(tf, "对参赛与对交付是同一个道理——一次把不确定说成确定，后面所有结论都要重新核", 10.5,
         False, DIM, space_before=5)
    notes(s, "30 秒。三道闸取交集，不是任意通过。第一道管有没有证据，"
             "第二道管角色有没有串通，第三道管能不能复现。")
    footer(s, 12, total, "对抗风险覆盖：提示词注入、模型编造证据、基准污染、沙箱逃逸、成本失控、模型供应链")

    # ── 13 创新六：经验记忆库 ───────────────────────────────
    s = blank(prs)
    head(s, "四、技术创新（六）", "经验记忆库与跨项目迁移",
         "把一次扫描的结论沉淀为可检索的经验，换一个项目还能用得上")
    tabs = [("runs", "扫描运行记录", ACCENT), ("hypotheses", "假设与证据", CYAN),
            ("verdicts", "判定与差分结果", OK), ("patterns", "缺陷模式", VIOLET)]
    for i, (name, note, c) in enumerate(tabs):
        x = 0.85 + i * 2.86
        card(s, x, 1.86, 2.72, 1.04, SURF1)
        bar(s, x, 2.02, 0.06, 0.72, c)
        tf = textbox(s, x + 0.24, 2.00, 2.34, 0.80)
        para(tf, name, 12, True, c, first=True, space_before=0)
        para(tf, note, 9.5, False, DIM, space_before=3)

    bullet(s, 0.85, 3.14, 11.4, [
        ("两段式检索", "先按 CWE 收敛候选，再用标识符词集的 Jaccard 相似度排序；"
                  "已验证过的模式额外加权，避免同类缺陷每次从零推导"),
        ("容量与保留", "evidence 字段 JSON 上限 4000 字符，避免单条记录撑爆库；"
                  "一般保留 12 个月，按需归档"),
        ("跨项目迁移", "经验以缺陷模式而非项目路径为键，因此换代码库仍可复用；"
                  "模式命中只作为先验，仍须本地验证"),
    ], size=12, gap=0.78)

    card(s, 0.85, 5.66, 11.35, 0.96, SURF2, edge=ACCENT)
    tf = textbox(s, 1.12, 5.78, 10.9, 0.76)
    para(tf, "这一项解决的是「经验留不住」：人员流动带走的是人，不是结论", 12.5, True,
         ACCENT_T, first=True, space_before=0)
    para(tf, "同时它也是成本下降的第二条来源——重复模式不再重复消耗推理预算", 10.5,
         False, DIM, space_before=5)
    notes(s, "25 秒。四张表、两段式检索、十二个月保留期。"
             "要点是经验以缺陷模式为键，所以能跨项目用。")
    footer(s, 13, total, "实现：vulnagent/memory.py（sqlite3 四表，按 cwe 与 hyp_id 建索引）")

    # ── 14 创新七：合规内生与私有化 ─────────────────────────
    s = blank(prs)
    head(s, "四、技术创新（七）", "合规内生的私有化架构",
         "合规要素不是报告里的附加章节，而是从数据流向与执行形态上就成立")
    forms = [
        ("单机原型", "subprocess 沙箱；heuristic 模式可完全离线\n适合验证与演示", ACCENT),
        ("内网平台", "推理服务器 + 容器集群；按等保三级部署\n适合常态化运营", CYAN),
        ("流水线嵌入", "只处理增量，接 CI 门禁\n适合研发流程内前置", VIOLET),
    ]
    for i, (t, b, c) in enumerate(forms):
        x = 0.85 + i * 3.86
        card(s, x, 1.86, 3.60, 1.50, SURF1, edge=c)
        tf = textbox(s, x + 0.24, 2.02, 3.14, 1.22)
        para(tf, t, 12.5, True, c, first=True, space_before=0)
        for j, ln in enumerate(b.split("\n")):
            para(tf, ln, 9.5, False, DIM, space_before=6 if j == 0 else 2, line=1.3)

    tf = textbox(s, 0.85, 3.56, 5.9, 0.32)
    para(tf, "三条合规红线", 11.5, True, ACCENT_T, first=True, space_before=0)
    lines = ["云端模型通道默认关闭，启用须显式配置",
             "危害国家安全或公共利益的情形 24 小时内报告",
             "漏洞信息 2 日内在工信部平台报送，须责任部门复核"]
    for i, ln in enumerate(lines):
        bar(s, 0.85, 3.94 + i * 0.42 + 0.09, 0.07, 0.07, AMBER)
        tf2 = textbox(s, 1.06, 3.94 + i * 0.42, 5.7, 0.38)
        para(tf2, ln, 10.5, False, DIM, first=True, space_before=0)

    card(s, 6.98, 3.54, 5.22, 1.94, SURF2)
    tf = textbox(s, 7.20, 3.66, 4.8, 0.30)
    para(tf, "沙箱执行加固（命令固化）", 10.5, True, OK, first=True, space_before=0)
    cmd = ("docker run --rm --network none \\\n"
           "  --memory 512m --cpus 1 --pids-limit 128 \\\n"
           "  --security-opt no-new-privileges --read-only \\\n"
           "  --tmpfs /tmp:rw,size=64m,noexec \\\n"
           "  -v <workdir>:/w:rw -w /w python:3.12-slim \\\n"
           "  python -I /w/poc_case.py /w/target")
    for j, ln in enumerate(cmd.split("\n")):
        tf2 = textbox(s, 7.20, 4.02 + j * 0.22, 4.8, 0.22)
        para(tf2, ln, 8.5, False, BLUEL, first=True, space_before=0, line=1.1)

    tf = textbox(s, 0.85, 5.66, 11.4, 0.9)
    para(tf, "接入侧：PBKDF2-HMAC-SHA256 26 万次迭代加随机盐，会话令牌只存摘要，"
             "不发默认口令；工具按四档权限过滤，执行前再判一次，分级缺失即拒绝启动",
         11, False, DIM, first=True, space_before=0, line=1.34)
    notes(s, "35 秒。合规内生有三层意思：部署形态可选、数据不出内网、"
             "报送与复核有固定动作。沙箱那六行参数是固化在代码里的，不是建议。")
    footer(s, 14, total, "对应参赛方案书创新点七与设计说明书第十二章（执行隔离、授权边界、审计追溯）")

    # ── 15 实测 ─────────────────────────────────────────────
    s = blank(prs)
    head(s, "五、验证情况", "三个样例目标端到端跑通，数字可复算",
         "自建样例注入已知缺陷，逐一核对是否检出、是否验证、修复后是否归零")
    stat(s, 0.85, 1.82, 2.72, "11 条", "三个目标合计检出漏洞", OK, h=1.10, vs=20)
    stat(s, 3.72, 1.82, 2.72, "4.1 s", "三个目标端到端总耗时", ACCENT, h=1.10, vs=20)
    stat(s, 6.59, 1.82, 2.72, "0 条", "修复版重新扫描的交付项", VIOLET, h=1.10, vs=20)
    stat(s, 9.46, 1.82, 3.02, "31 项", "分层自动化测试全部通过", CYAN, h=1.10, vs=20)

    heads = ["样例目标", "规模", "调用点 / 解析率", "检出结果"]
    colx = [0.85, 3.30, 5.40, 8.40]
    tf = textbox(s, 1.10, 3.10, 10.9, 0.26)
    para(tf, "　　　　　　　".join([]), 1, False, MUTED, first=True, space_before=0)
    for i, name in enumerate(heads):
        tfh = textbox(s, colx[i] + 0.24, 3.08, 3.0, 0.26)
        para(tfh, name, 9.5, True, MUTED, first=True, space_before=0)
    rows = [("vulnapp", "1 文件 92 行", "31 / 19%", "6 条：CWE-502 严重，78、798、89、22 高危，327 中危", OK),
            ("webapp", "3 文件 97 行", "20 / 50%", "1 条：CWE-89 dao.py:24（三层调用链可达）", CYAN),
            ("websvc", "1 文件 59 行", "16 / 25%", "4 条：CWE-95、918 高危，79、327 中危，全 verified", VIOLET)]
    for i, (a, b, c, d, col) in enumerate(rows):
        y = 3.38 + i * 0.56
        card(s, 0.85, y, 11.35, 0.48, SURF1 if i % 2 == 0 else SURF2)
        bar(s, 0.85, y, 0.05, 0.48, col)
        for j, val in enumerate((a, b, c)):
            tfx = textbox(s, colx[j] + 0.24, y + 0.12, 2.4, 0.28)
            para(tfx, val, 9.5, j == 0, TEXT if j == 0 else DIM, first=True,
                 space_before=0)
        tfd = textbox(s, colx[3] + 0.24, y + 0.12, 4.1, 0.28)
        para(tfd, d, 9, False, DIM, first=True, space_before=0)

    tf = textbox(s, 0.85, 5.12, 5.5, 0.28)
    para(tf, "增量扫描实测（同一目标连续三次）", 10.5, True, ACCENT_T, first=True,
         space_before=0)
    inc = [("① 首次建基线", "变更 3 文件 → 审计 0 切片 → 0 条", MUTED),
           ("② 重新引入缺陷", "变更 1 文件 → 审计 1 切片 → 1 条 CWE-89", OK),
           ("③ 无变更重跑", "跳过，直接给出已有结论", CYAN)]
    for i, (a, b, c) in enumerate(inc):
        tf2 = textbox(s, 0.85, 5.46 + i * 0.36, 6.0, 0.30)
        rich(tf2, [(a + "　", True, c), (b, False, DIM)], size=9.5, first=True,
             space_before=0)

    card(s, 7.10, 5.10, 5.10, 1.34, SURF2, edge=ACCENT)
    tf = textbox(s, 7.32, 5.24, 4.7, 1.10)
    para(tf, "缺陷注入与修复闭环", 10.5, True, ACCENT_T, first=True, space_before=0)
    para(tf, "三个样例目标注入六类典型缺陷全部检出；对修复版重新扫描为 0 条交付项，"
             "说明结论不是常驻噪声", 9.5, False, DIM, space_before=5, line=1.30)
    notes(s, "40 秒。三个样例目标、十一个漏洞、四点一秒。最值得讲的是最后一句："
             "把缺陷修掉再扫，交付项归零——这证明结论跟着代码走，不是常驻噪声。"
             "下面是增量三行，第三行无变更直接跳过。")
    footer(s, 15, total, "复算方式：python -X utf8 -m unittest discover -s tests -t .（31 项分层用例）")

    # ── 16 效益与推广 ───────────────────────────────────────
    s = blank(prs)
    head(s, "六、效益与推广", "五类场景的工时对比，实测与估算分开标注",
         "效率与质量已被实测证明；成本维度给出设计锚点与验证方法")
    heads = ["场景", "传统方式", "本系统", "变化"]
    colx = [0.85, 3.60, 7.20, 9.60]
    for i, name in enumerate(heads):
        tfh = textbox(s, colx[i] + 0.24, 1.86, 3.2, 0.26)
        para(tfh, name, 9.5, True, MUTED, first=True, space_before=0)
    rows = [("单系统全量审计（50 万行）", "4 人 × 15 天", "2 人 × 3 天", "人力降约 80%", OK),
            ("应急影响面确认", "3 人 × 2 天", "4 小时", "降约 90%", OK),
            ("2000 行增量审计", "1 人 × 1 天", "30 分钟 + 复核", "降约 90%", OK),
            ("渗透前期信息收集", "5 天", "1 + 1 天", "降约 60%", CYAN),
            ("补丁与版本差分", "2 天", "2 小时", "降约 90%", CYAN)]
    for i, (a, b, c, d, col) in enumerate(rows):
        y = 2.16 + i * 0.56
        card(s, 0.85, y, 11.35, 0.48, SURF1 if i % 2 == 0 else SURF2)
        bar(s, 0.85, y, 0.05, 0.48, col)
        tfa = textbox(s, colx[0] + 0.24, y + 0.12, 2.9, 0.28)
        para(tfa, a, 9.5, False, TEXT, first=True, space_before=0)
        for j, val in enumerate((b, c)):
            tfx = textbox(s, colx[j + 1] + 0.24, y + 0.12, 3.2, 0.28)
            para(tfx, val, 9.5, False, DIM, first=True, space_before=0)
        tfd = textbox(s, colx[3] + 0.24, y + 0.12, 2.6, 0.28)
        para(tfd, d, 9.5, True, col, first=True, space_before=0)

    tf = textbox(s, 0.85, 5.08, 6.0, 0.28)
    para(tf, "推广路径", 10.5, True, ACCENT_T, first=True, space_before=0)
    phases = [("1–6 月", "试点：单团队、单系统，跑通闭环"),
              ("7–18 月", "规模复制：接 CI、多节点、多项目"),
              ("19–36 月", "生态：模式库共享、行业适配")]
    for i, (t, b) in enumerate(phases):
        tf2 = textbox(s, 0.85, 5.40 + i * 0.34, 6.0, 0.30)
        rich(tf2, [(t + "　", True, ACCENT_T), (b, False, DIM)], size=9.5,
             first=True, space_before=0)

    card(s, 7.30, 5.06, 4.90, 1.28, SURF2, edge=WARN)
    tf = textbox(s, 7.52, 5.18, 4.5, 1.06)
    para(tf, "待规模化验证（报告中单列）", 10.5, True, WARN, first=True,
         space_before=0)
    para(tf, "真实大项目的每千行成本对比；多节点并发下的队列吞吐与基线并发覆盖",
         9.5, False, DIM, space_before=5, line=1.30)
    notes(s, "30 秒。五组数字来自方案书效益测算：全量审计降八成、应急确认降到四小时。"
             "成本那格是设计锚点，报告里单独标注了待规模化验证两项，不混在实测里。")
    footer(s, 16, total, "年度人力测算：400 人天 → 120 人天（按上述五类场景加权）")

    # ── 17 小结 ────────────────────────────────────────────
    s = blank(prs)
    bar(s, 1.0, 1.06, 0.12, 0.72, ACCENT)
    tf = textbox(s, 1.30, 1.00, 10.8, 1.4)
    para(tf, "七、小结", 12, True, ACCENT_T, first=True, space_before=0)
    para(tf, "把漏洞发掘从经验判断转为证据工程", 29, True, TEXT, space_before=8)

    pts = [
        ("路径成立", "四阶段智能体、七阶段闭环、五层架构已在原型上跑通，不是设计图"),
        ("结论可信", "判定由缺陷版与修复版差分给出；执行失败一律不下结论"),
        ("成本可控", "预算硬上限叠加增量与经验复用，成本随变更量而非代码总量增长"),
        ("落地可行", "三种部署形态，全离线私有化；合规要素内生于流程而非附加章节"),
    ]
    for i, (t, b) in enumerate(pts):
        y = 2.72 + i * 0.84
        card(s, 1.0, y, 10.6, 0.70, SURF1)
        tf2 = textbox(s, 1.28, y + 0.13, 10.1, 0.54)
        rich(tf2, [(t, True, ACCENT_T), ("　" + b, False, DIM)], size=11.5,
             first=True, space_before=0)

    tf = textbox(s, 1.0, 6.34, 10.8, 0.45)
    para(tf, "汇报完毕，请各位批评指正", 12, False, MUTED, first=True,
         space_before=0)
    notes(s, "20 秒。四句收尾：路径成立、结论可信、成本可控、落地可行。不展开，留提问时间。")
    footer(s, 17, total, "")

    prs.save(path)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="build the 8-minute report deck")
    parser.add_argument("--out", default="成果发布-8分钟.pptx")
    args = parser.parse_args(argv)
    out = os.path.abspath(args.out)
    build(out)
    print("wrote %s (%d bytes)" % (out, os.path.getsize(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
