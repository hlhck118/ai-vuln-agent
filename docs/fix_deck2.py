# -*- coding: utf-8 -*-
# 第二轮版面修补：只处理 p11 / p8 / p16 三处「文字或卡片越出容器」的实测缺陷。
# 与 fix_deck.py 分开成两个脚本，是为了不重跑那边的 A 段（第 11 页整块删除重画），
# 那个操作不幂等，在同一份文件上跑第二遍会叠出第二套图形。
import sys
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
E = 914400.0
SRC, DST = sys.argv[1], sys.argv[2]
APPLY = '--apply' in sys.argv
prs = Presentation(SRC)
log = []

def by_id(slide, sid):
    for sh in slide.shapes:
        if sh.shape_id == sid:
            return sh
    return None

def move(pi, sid, top):
    sh = by_id(prs.slides[pi], sid)
    if sh is None:
        log.append('  !! 找不到 id=%d' % sid)
        return
    if APPLY:
        sh.top = Inches(top)

def resize(pi, sid, h):
    sh = by_id(prs.slides[pi], sid)
    if sh is None:
        log.append('  !! 找不到 id=%d' % sid)
        return
    if APPLY:
        sh.height = Inches(h)

def widen(pi, sid, w):
    sh = by_id(prs.slides[pi], sid)
    if sh is None:
        log.append('  !! 找不到 id=%d' % sid)
        return
    if APPLY:
        sh.width = Inches(w)

# H. 第 11 页：左右两块灰底只有 3.89 高（下沿 5.92），内部第三块白卡下沿 6.08，
#    「定向污点查询 / 污点传播类缺陷」「路径与查询结果 / 可行不可行分支」两行白底的
#    下半截露在灰底之外，读起来像被灰卡切掉了。灰底补到 4.05，正好与白卡下沿齐。
resize(10, 652, 4.05)
resize(10, 665, 4.05)
log.append('  p11 灰底 652/665 高 3.89 -> 4.05（下沿 5.92 -> 6.08，与内部白卡齐）')

# I1. 第 8 页右下「技术栈实现约束」：四行标签行距 0.49，第四行「模型接入」下沿 8.24
#     已越过白卡下沿 8.06，被下方「设计目标」卡（上沿 8.33）压住半行。
#     白卡补到 8.20，四行行距收到 0.44，末行下沿 8.05，留 0.15 的余量。
resize(7, 410, 2.64)
P8_ROWS = {413: 6.40, 414: 6.42, 415: 6.84, 416: 6.86,
           417: 7.28, 418: 7.30, 419: 7.72, 420: 7.74}
for sid, top in P8_ROWS.items():
    move(7, sid, top)
log.append('  p8 技术栈卡 410 高 2.50 -> 2.64；四行行距 0.49 -> 0.44')

# I2. 第 8 页「设计目标」正文：need 4.69 > 框宽 4.61，PowerPoint 折成两行，末字「性」孤行。
#     框宽补到 4.87（与标题 422 同宽，右沿 17.20 仍在卡内）。
widen(7, 423, 4.87)
log.append('  p8 设计目标正文 423 宽 4.61 -> 4.87（need 4.69，一行容纳）')

# J. 第 16 页三张样例目标卡：漏洞标签第三行下沿 7.34/7.35，越过卡片下沿 7.15 共 0.19，
#    而卡下紧接「增量扫描」卡（上沿 7.36），不能再往下长。卡补到 7.28；三行行距由
#    0.35 收到 0.32，「检出漏洞」整行上移 0.04 让出第一行标签的净空。
#    第二张卡只有两行标签，一并按同一行距排，三张卡等高、行基线一致。
for sid in (1094, 1116, 1136):
    resize(15, sid, 3.74)
P16_TOP = {
    1107: 5.74, 1108: 5.60, 1109: 5.74,
    1129: 5.74, 1130: 5.60, 1131: 5.74,
    1149: 5.74, 1150: 5.60, 1151: 5.74,
    1110: 6.28, 1111: 6.27, 1112: 6.60, 1113: 6.60, 1114: 6.92, 1115: 6.91,
    1132: 6.28, 1133: 6.27, 1134: 6.60, 1135: 6.60,
    1152: 6.28, 1153: 6.27, 1154: 6.60, 1155: 6.60, 1156: 6.92, 1157: 6.91,
}
for sid, top in P16_TOP.items():
    move(15, sid, top)
log.append('  p16 三张样例卡 1094/1116/1136 高 3.61 -> 3.74；标签三行 6.28/6.60/6.92')


# K. 第 15 页「沙箱执行加固（docker run）」：深色块里那七个参数是 prototype/vulnagent/tools.py
#    的 SandboxRunner._docker_cmd() 原样拼出来的（config.py:49 docker_image = python:3.12-slim），
#    但开发机没有 Docker（仅有 WSL），容器后端从未在本机跑过，默认仍是 subprocess 后端。
#    汇报时被问「这是实测还是设计」，页面必须自己先说清；写在参数块里，比放备注页有用。
P15_NOTE = '参数由 SandboxRunner._docker_cmd() 生成；本机未装 Docker，容器后端待部署环境启用，现默认 subprocess'
for sh in list(prs.slides[14].shapes):
    if sh.has_text_frame and sh.text_frame.text.strip() == P15_NOTE:
        if APPLY:
            sh._element.getparent().remove(sh._element)
        log.append('  p15 删除旧口径行（幂等）')
if APPLY:
    n = prs.slides[14].shapes.add_textbox(Inches(9.08), Inches(5.66), Inches(8.12), Inches(0.20))
    tf = n.text_frame
    tf.word_wrap = True
    tf.auto_size = None
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    r = p.add_run()
    r.text = P15_NOTE
    r.font.size = Pt(8.5)
    r.font.bold = False
    r.font.color.rgb = RGBColor(0x8F, 0xA3, 0xB8)
    r.font.name = 'Noto Sans SC'
log.append('  p15 深色块补一行口径（参数出处 + 本机未装 Docker）')

print('--- %d ops ---' % len(log))
print(chr(10).join(log))
if APPLY:
    prs.save(DST)
    print('SAVED ' + DST)