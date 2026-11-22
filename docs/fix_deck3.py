# -*- coding: utf-8 -*-
# 第三轮：第 15 页右下块换内容。
# 原来那块「沙箱执行加固（docker run）」列的是 prototype/vulnagent/tools.py 里
# SandboxRunner._docker_cmd() 拼出的七个参数 —— 逻辑是真的，但开发机只有 WSL 没有
# Docker，容器后端从未在本机跑过，汇报时被问一句「这是实测还是设计」就站不住。
# 换成 scope.py 的三条不变量：本机每天在跑、每条都有单测钉住、控制面与执行节点共用
# 同一份实现，而且它正好回答本页的核心命题 —— 合规要素为什么是「原生成立」而不是
# 事后补的报告章节：因为「能扫什么」不是一句自我声明，是一条可执行的判定。
# 左半是三条法规红线（编号 1/2/3 的红色圆点），右半是三条技术不变量（①②③），
# 左右对仗，不再是「左讲法规、右讲参数」。
#
# 幂等：本脚本可在同一份文件上重复执行。旧胶囊按 id 删；本脚本自己新建的
# 三个文本框与出处行按**首字/前缀**删 —— 它们的 id 是每跑一次就变的，按 id
# 记不住，而按完整文案匹配又会在改了措辞之后失效。
import sys
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
SRC, DST = sys.argv[1], sys.argv[2]
APPLY = '--apply' in sys.argv
prs = Presentation(SRC)
S = prs.slides[14]
log = []
pre = prs.slides[14].slides if False else None

def by_id(side, sid):
    for sh in side.shapes:
        if sh.shape_id == sid:
            return sh
    return None

def drop_where(side, pred):
    n = 0
    for sh in list(side.shapes):
        if sh.has_text_frame and pred(sh.text_frame.text.strip()):
            if APPLY:
                sh._element.getparent().remove(sh._element)
            n += 1
    return n

# 0. 幂等：先删掉上一轮跑出来的三行不变量与出处行。
n_old = drop_where(S, lambda t: t[:1] in ('①', '②', '③')
                   or t.startswith('实现：pentestd/scope.py'))
if n_old:
    log.append('  p15 删除上一轮生成的不变量行/出处行 %d 个（幂等）' % n_old)

# 1. 删掉参数胶囊（1031-1038）与旧口径行（1066/1067）。按 id 删，不用文案匹配 ——
#    这一整块都要换掉，留着任何一枚胶囊都会读成「旧内容没删干净」。
#    1066 是首版口径行的 id，1067 是加了行距调整后的 id，两个都认。
DROP = [1031, 1032, 1033, 1034, 1035, 1036, 1037, 1038, 1066, 1067]
for sid in DROP:
    sh = by_id(S, sid)
    if sh is not None:
        if APPLY:
            sh._element.getparent().remove(sh._element)
        log.append('  p15 删除 id=%d' % sid)

# 2. 标题换口径；底注换掉 —— 原来那句「禁用网络 · 限制资源 · 禁止提权 · …」是描述
#    docker flag 的，随胶囊一起作废。新底注讲 scope.py 里唯一容易被忽略的一条：
#    any 归一化成 0.0.0.0/0 + ::/0 后走的仍是正常判定路径，但 MAX_HOSTS / MAX_PROBES
#    照旧生效 —— 放开范围不等于放开预算。
TITLE_NEW = '授权边界：三条不变量'
NOTE_NEW = '放开范围不等于放开预算：即便写成 any，单次目标仍受 MAX_HOSTS / MAX_PROBES 约束'
if APPLY:
    by_id(S, 1029).text_frame.paragraphs[0].runs[0].text = TITLE_NEW
    by_id(S, 1040).text_frame.paragraphs[0].runs[0].text = NOTE_NEW
log.append('  p15 标题 -> 「%s」' % TITLE_NEW)
log.append('  p15 底注 -> 范围与预算分离')

# 3. 出处写进标题行右侧，不占深色块内的竖向空间。板块内的三行本来就要拉开行距，
#    再塞一行 8.5pt 的出处会把第三行挤到分隔线上。右对齐到 x=17.22，与上方三张
#    能力卡的「验证与演示 / 常态化运营 / 研发流程前置」同一条右边界。
if APPLY:
    sb = S.shapes.add_textbox(Inches(12.60), Inches(3.66), Inches(4.62), Inches(0.25))
    tf = sb.text_frame
    tf.word_wrap = False
    tf.auto_size = None
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.RIGHT
    r = p.add_run()
    r.text = '实现：pentestd/scope.py · 三条不变量均有单测钉住'
    r.font.size = Pt(9)
    r.font.bold = False
    r.font.color.rgb = RGBColor(0x8F, 0xA3, 0xB8)
    r.font.name = 'Noto Sans SC'
log.append('  p15 出处移到标题行右侧（右沿 17.22）')

# 4. 三条不变量。每行一个文本框两段：13pt 白色粗体标题 + 10.5pt 灰字说明。
#    行距 0.62（top 4.30 / 4.92 / 5.54），第三段说明下沿约 6.04，距分隔线 6.25
#    留 0.21 —— 上一版行距 0.60 且块内还压了一行出处，第三行与出处只差 0.10，
#    读起来是粘在一起的两段。
ROWS = [
    ('①', '未配置即拒绝', '空作用域拒绝一切目标 ——「没配置」不等于「什么都能扫」'),
    ('②', '域名只解析一次', '校验过的地址与实际发包的地址永远是同一批，堵住 DNS rebinding'),
    ('③', '判定是整体的', '一条目标里只要有一个地址越界，整个任务就不下发，不做部分放行'),
]
for i, (mark, head, body) in enumerate(ROWS):
    if not APPLY:
        continue
    top = 4.30 + i * 0.62
    tb = S.shapes.add_textbox(Inches(9.08), Inches(top), Inches(8.00), Inches(0.54))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.auto_size = None
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    p1 = tf.paragraphs[0]
    p1.alignment = PP_ALIGN.LEFT
    r0 = p1.add_run()
    r0.text = mark + ' '
    r0.font.size = Pt(12)
    r0.font.bold = True
    r0.font.color.rgb = RGBColor(0xC0, 0x56, 0x4A)
    r0.font.name = 'Noto Sans SC'
    r1 = p1.add_run()
    r1.text = head
    r1.font.size = Pt(13)
    r1.font.bold = True
    r1.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    r1.font.name = 'Noto Sans SC'
    p2 = tf.add_paragraph()
    p2.alignment = PP_ALIGN.LEFT
    p2.space_before = Pt(2)
    r2 = p2.add_run()
    r2.text = body
    r2.font.size = Pt(10.5)
    r2.font.bold = False
    r2.font.color.rgb = RGBColor(0x8F, 0xA3, 0xB8)
    r2.font.name = 'Noto Sans SC'
log.append('  p15 三条不变量三行（top 4.30 / 4.92 / 5.54）')

print('--- %d ops ---' % len(log))
print(chr(10).join(log))
if APPLY:
    prs.save(DST)
    print('SAVED ' + DST)
