# -*- coding: utf-8 -*-
"""对话里的「空气泡」：**任何一条已定稿的消息都不能渲染成没有内容的框**。

报上来的原话是「空的气泡」。它有三种成因，长在两条不同的渲染路径上：

1. **落库消息**（刷新/切会话走 `renderLog` → `messageNode`）。模型这一轮只做了
   思考或工具调用、一个正文字都没写时，落库的 `text` 是空串。改版前那一条会落到
   `else` 分支，画一句「（模型没有返回文本）」——**只说了"没东西"，没说"为什么"**，
   读者会以为是平台把回答弄丢了。
2. **中止标记**（`pentestd/chat.py` 的 `tail("", interrupted=True)`）。它写死了
   「已完成的工具调用见下方」，而中止那一轮**根本没有把轨迹落库**（轨迹只在
   `done` 事件里发一次）。于是这句话指向一片空白。
3. **实时气泡**（`renderLive` → `liveBody`）。这一轮结束时 `live.text`、
   `live.reasoning` 都空、又有工具调用时，改版前**一个子节点都不加**：框是空的；
   若连工具调用也没有，则继续显示「正在推理与调用工具…」——一句在「已完成」下面
   报出来的、不会再发生的事。

## 这一版的三条规矩

- 正文为空时，**两种路径说同一句话**（`这一轮没有产出正文…`）：刷新前后不该是
  两种说法，否则用户会以为是两次不同的事故；
- 「见下方」只在**真有轨迹**时说；
- 实时气泡在 `done` 之后**不许留空**，也不许再报"正在…"。

## 写法与边界

同 `test_dock_dismiss.py`：切源码文本做断言，**不执行**那份 JS（本仓库没有
node/jsdom 那一层）。所以这里只能证明"代码是这么写的"；真的画出来长什么样，
由 `tools/render_ui_fixture.py --tab chat --png` 在夹具里截图看，两者互补。
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: 两条路径必须逐字一致的那句话。**它就是契约本身**：分成两种说法，用户会把
#: "刷新后换了句话"当成两件事。
EMPTY_TEXT = "这一轮没有产出正文（模型只做了思考或工具调用）。"


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def _strip_comments(text):
    """去掉 `//` 与 `/* */` 注释，只留下会被执行的代码。

    本文件查的是"某句话有没有出现在某个分支里"，而注释里正好会**讨论**这些句子
    （比如 `messageNode` 的注释里写着「也不能再说『（模型没有返回文本）』」）。
    不剥注释，那条"旧文案必须消失"的断言会被自己的说明文字打红。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _fn_body(text, name):
    """按缩进切出一个函数的函数体（含首行），并剥掉注释。找不到返回空串。"""
    match = re.search(r"^(  (?:async )?function %s\b[^\n]*\n)" % re.escape(name),
                      text, re.M)
    if not match:
        return ""
    rest = text[match.end():]
    out = [match.group(1)]
    for line in rest.split("\n"):
        if line.strip() and not line.startswith("    "):
            break
        out.append(line)
    return _strip_comments("\n".join(out))


class EmptyBubbleTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chat = _read("web", "chat.js")
        cls.node = _fn_body(cls.chat, "messageNode")
        cls.log = _fn_body(cls.chat, "renderLog")
        cls.visible = _fn_body(cls.chat, "messageVisible")
        cls.live = _fn_body(cls.chat, "liveBody")
        cls.note_fn = _fn_body(cls.chat, "emptyTurnNote")

    # ----------------------------------------------------- 1. 落库消息 ----

    def test_a_stored_message_with_no_text_never_renders_empty(self):
        """`messageNode` 的每个分支都得往 body 里放东西。"""
        self.assertTrue(self.node, "找不到 messageNode：落库消息的渲染器改名字了")
        body = self.node.split("const body = el('div', 'chat-msg-body');")[1]
        # 6 = error / interrupted / mine / 选项有正文 / 选项无正文 / 纯正文 / 全空。
        # 其中"选项有正文 vs 无正文"共用一个 append 调用点（三元），所以 7 个分支
        # 对应 6 处调用。加 `interrupted` 分支时这里漏改过——它是**产品真加了一条路**，
        # 不是渲染退化，所以改断言而不是删分支。
        self.assertEqual(body.count("body.append("), 6,
                         "正文分支的个数变了：每加一条路都要保证它至少放一句话进去")
        # 那句话现在只有一处（`emptyTurnNote`）：两条路径都调它，"逐字一致"由它保证。
        self.assertIn(EMPTY_TEXT, self.note_fn,
                      "空正文的消息没有占位语——它就是一个空气泡")
        self.assertIn("emptyTurnNote(message)", body,
                      "落库路径没有走统一的那句话")

    def test_the_old_bare_placeholder_is_gone(self):
        """「（模型没有返回文本）」只说了"没东西"，读起来像平台丢了回答。"""
        self.assertNotIn("（模型没有返回文本）", self.node)

    # ----------------------------------------------------- 2. 中止标记 ----

    def test_interrupted_only_points_down_when_a_trace_exists(self):
        """没有轨迹时「见下方」指向一片空白。"""
        self.assertIn("hasTrace ? '已完成的工具调用见下方。' : ''", self.node,
                      "中止提示里的「见下方」必须跟着轨迹的有无走：中止那一轮"
                      "没有把轨迹落库，这句话会指向空白")
        self.assertIn("asArray(message.trace).length", self.node,
                      "hasTrace 没有从 message.trace 上来")

    def test_an_interrupted_carrier_is_not_skipped_by_the_log(self):
        """中止标记若带 toolCalls，会被"只看工具调用"的跳过规则吞掉。"""
        # 跳过规则与它的例外都收在 messageVisible() 里（renderLog 与实时重画共用
        # 同一套判据，分散成两处就会有一处忘掉例外从而把中止标记吞掉）。
        self.assertIn("!message.interrupted", self.visible,
                      "跳过规则没给中止标记留例外：那一轮连「发生了什么」都不剩")
        # 跳过规则本身还得在：普通载体（有工具调用、无正文）照旧不画，
        # 否则每一轮都会多出几个"这一轮没有产出正文"的空框。
        self.assertIn("asArray(message.toolCalls).length", self.visible,
                      "跳过规则被删了：携带工具调用的那条会重新变成一个空框")
        self.assertIn("if (!messageVisible(message)) return;", self.log,
                      "renderLog 必须走同一个判据：换成内联条件就会和实时路径分叉")

    # ----------------------------------------------------- 3. 实时气泡 ----

    def test_the_live_bubble_is_never_empty_after_it_finishes(self):
        """结束时正文空、思考空、有工具调用——改版前这里一个节点都不加。"""
        self.assertIn("live.done && !segments.length", self.live,
                      "实时气泡没有「跑完了但没正文」的分支：框会空着")
        self.assertIn(EMPTY_TEXT, self.note_fn,
                      "实时路径与落库路径说了两句话：刷新前后对不上")
        self.assertIn("emptyTurnNote(live)", self.live,
                      "实时路径没有走统一的那句话")

    def test_the_placeholder_carries_the_reason_when_the_server_knows_it(self):
        """`finishNote` 在时用原因，不再用那句"模型只做了思考"。

        这是两种不同的事故：输出预算被思考吃光（`length`）改配置就好，"只想不说"
        只能再问一次。用同一句话盖掉，用户不知道该动哪里——上一版就是这样。
        """
        self.assertIn("message.finishNote", self.note_fn)
        self.assertIn("emptyTurnNote(message)", self.node)
        self.assertIn("emptyTurnNote(live)", self.live)

    def test_finished_is_not_reported_as_still_thinking(self):
        """「已完成」下面还挂着「正在推理…」是假话，而且会让人一直等。"""
        self.assertIn("live.reasoning && !live.done", self.live,
                      "思考占位没有排除 done：跑完之后还在报「正在推理」")
        self.assertIn("!live.tools.length && !live.done", self.live,
                      "工具占位没有排除 done：跑完之后还在报「正在调用工具」")


if __name__ == "__main__":
    unittest.main()
