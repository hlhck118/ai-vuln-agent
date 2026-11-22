# -*- coding: utf-8 -*-
"""在上传版 deck（docs/deck_augment.py 的产物，21 页）里补一页「AI 已经嵌进攻击的每一个环节」。

为什么不改 deck_augment.py：那条链的输入是 16 页上传版 pptx，它不在仓库里（--in 无从给），
改它等于让这一页只存在于「有原始输入的人」手里。这里换一种做法：直接对仓库里现有的 deck
做插入，输入输出都是仓库内文件，任何时候 `--in` 现成 deck 就能重跑，页数与页码也由脚本自己数。

版式全部复用 deck_augment 的助手与常量，不另起一套：底色 #F7F8FA、主色 #3E5C76、强调 #C0564A、
字体 Noto Sans SC、画布 17.78 × 10.0 英寸。

用法：python -X utf8 docs/deck_ai_attack.py --in IN.pptx --out OUT.pptx
"""
import argparse

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN

from deck_augment import (
    ACC, INK, LIGHT, MAIN, PALE, SOFT, SUB, WHITE,
    card, chrome, new_slide, notes, para, rect, rich, textbox,
)

# 六项用法照攻击流程排：从没摸到目标，到进了内网还得继续。
# 每条只写「做法」和「防守方能观察到的影响」——背景页要交代的是变化，不是攻击教程。
STAGES = [
    ("情报收集",
     "整理公开信息，归并出组织架构与人员名单；批量枚举子域、端口与对外暴露的服务，画出比资产台账更完整的攻击面。",
     "台账的维护速度跟不上暴露面的变化。"),
    ("社会工程",
     "按职务与业务场景撰写通顺邮件，语气、称呼、时机都对得上；语音合成与视频换脸冒充上级下达指令。",
     "「语言生硬」这类识别线索基本失效。"),
    ("漏洞发现",
     "辅助读代码与流量，快速定位输入点与调用链；对公开漏洞产出可运行的验证代码，省去自己搭环境的时间。",
     "从披露到可用利用代码的间隔被压缩。"),
    ("载荷构造",
     "改写与混淆，使每次样本的哈希与固定字符串都不同；按目标环境调整行为，避开常见沙箱与虚拟机的特征。",
     "基于固定特征的检测命中率下降。"),
    ("权限维持与横向",
     "解析主机配置与日志判断提权路径，把上一步的结果喂给下一步；失败就换一条路径，不需要人守着。",
     "从单个入口向内网推进不再需要人。"),
    ("运营与迭代",
     "批量管理多个目标，按失败原因自动调整策略；把一次成功的路径固化成模板，下次直接复用。",
     "同一个人能同时推进的目标数上升。"),
]
def slide_ai_attack(prs):
    s = new_slide(prs)
    chrome(s, "SECTION 01", "AI 已经嵌进攻击的每一个环节",
           "六项用法没有一项是新技术；改变的是每一项的完成速度与规模，以及这些环节能不能被自动串起来")

    # 三列两行：列宽按 16.66 的可用宽度均分，行高留 0.24 的呼吸位，
    # 六张卡等高，所以三个纵列里的「影响」天然落在同一条线上，不必逐个调 y。
    for i, (name, how, impact) in enumerate(STAGES):
        col, row = i % 3, i // 3
        x = 0.56 + col * 5.64
        y = 1.52 + row * 2.62
        card(s, x, y, 5.36, 2.30, radius=6667)
        # 编号做成实心徽章，与「人力·工具·制度」页那三块 01/02/03 同一套语言：
        # 观众不必逐个读标题就知道这是并行的六项，而不是六个递进步骤。
        # 底色取主色而不用强调色——本 deck 里红色留给要看的那个数字（28%、5 天）。
        rect(s, x + 0.24, y + 0.22, 0.46, 0.28, MAIN, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
        tf = textbox(s, x + 0.24, y + 0.22, 0.46, 0.28, anchor=MSO_ANCHOR.MIDDLE)
        para(tf, "%02d" % (i + 1), 10.5, True, WHITE, first=True, line=1.0,
             align=PP_ALIGN.CENTER)
        tf = textbox(s, x + 0.80, y + 0.20, 4.30, 0.32)
        para(tf, name, 13, True, INK, first=True, line=1.2)
        # 正文紧跟标题顶对齐，不居中：六格的说明长短不一，居中会让空白落在标题
        # 与正文之间，读起来像漏了一行；空白留在正文与影响条之间才是「换行」的暗示。
        tf = textbox(s, x + 0.26, y + 0.68, 4.84, 0.70)
        para(tf, how, 11.5, False, SUB, first=True, line=1.45)
        # 「影响」落在一条浅色底上，而不是悬在卡片下半部：语义上它回答的是
        # 「所以防守方会看到什么」，与上面那句「攻击怎么做」不同级；六张卡等高、
        # 正文行数却不一样，不垫底就会在卡里留下说不出理由的空白。
        rect(s, x + 0.18, y + 1.46, 5.00, 0.62, SOFT, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
        tf = textbox(s, x + 0.34, y + 1.54, 4.68, 0.46)
        rich(tf, [("影响　", True, LIGHT), (impact, False, INK)], 11, first=True, line=1.38)

    # 收尾条与「开发背景」页同一条样式：一是视觉上收口，二是把话说完整。
    rect(s, 0.56, 6.92, 16.67, 1.68, MAIN, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
    tf = textbox(s, 1.00, 7.12, 15.6, 0.28)
    para(tf, "这对防守意味着什么", 12.5, True, PALE, first=True, line=1.2)
    tf = textbox(s, 1.00, 7.44, 15.4, 0.42)
    para(tf, "缺的不是更快的工具，而是能被复核的结论", 20, True, WHITE, first=True, line=1.2)
    tf = textbox(s, 1.00, 7.94, 15.6, 0.34)
    para(tf, "工具决定扫得多快，证据决定结论能不能被采信——后者才是审计与处置真正卡住的地方。",
         12.5, False, SOFT, first=True, line=1.2)
    notes(s, "这一页交代背景。AI 在攻击侧没有发明什么新技术，它做的是两件事："
             "把每个环节的完成速度和规模抬上去，以及把这些环节串成能自己往下走的流水线。"
             "六格按攻击流程排，每格第二行写的是防守方能观察到的影响。"
             "所以结论落在中间那句话上：工具更快解决不了判断问题，这套系统的重心因此放在结论带证据、可复核。约 30 秒。")
    return s


def insert_at(prs, index):
    """把刚追加到末尾的那一页搬到 index 位（0 基）。"""
    lst = prs.slides._sldIdLst
    ids = list(lst)
    for el in ids:
        lst.remove(el)
    tail = ids.pop()
    ids.insert(index, tail)
    for el in ids:
        lst.append(el)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    a = ap.parse_args()
    prs = Presentation(a.src)
    n0 = len(prs.slides)
    slide_ai_attack(prs)
    # 紧跟「一、形势」的那一页之后：形势先讲威胁在涨，再讲涨在哪里。
    insert_at(prs, 2)

    total = len(prs.slides)
    assert total == n0 + 1, (total, n0)
    # deck 里只有末页带页码，其余页没有编号，所以只改这一处。
    last = prs.slides[-1]
    fixed = False
    for sh in last.shapes:
        if not sh.has_text_frame:
            continue
        t = sh.text_frame.text.strip()
        if "/" in t and len(t) <= 10 and t.split("/")[0].strip().isdigit():
            sh.text_frame.paragraphs[0].runs[0].text = "%d / %d" % (total, total)
            fixed = True
    assert fixed, "末页没找到形如 '21 / 21' 的页码，页码口径已变，先看清楚再改"
    prs.save(a.dst)
    print("slides:", n0, "->", total, "->", a.dst)


if __name__ == "__main__":
    main()