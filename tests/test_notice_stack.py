# -*- coding: utf-8 -*-
"""全局通知的落点：错误条与截断提示必须**浮在输入卡片上方**。

报上来的原话是「所有的弹窗提示和自动继续的那个提示，显示在输入框上面」。自动继续
那条（`#chat-auto-note`）看起来已经在卡片上方——它长在 `.chat-compose` 里，是卡片
自己的上一行——**但那只是盒子顺序**，画出来是另一回事：`.chat-field` 有
`margin-top: calc(-1 * var(--chat-overlap))`，会把自己整张抬进上一位兄弟的盒子，
而那一行当时没有像 `.chat-queue` 那样补偿这段，于是被卡片**整块盖住**。实测
（1440×900、`#chat-queue` 隐藏、强显一行文案）：note 竖直范围 684–752、卡片
626–768，落在 note 中心的 `elementFromPoint()` 返回 `chat-input`，截图里
`--tint-warn` 的暖色 rgb(253,243,221) 一个像素都没有——不是"偏下"，是看不见。
`test_the_pause_note_compensates_the_card_overlap` 守的就是这段补偿。另外两条不在
卡片上方：

  * `#error-bar` 挂在 `<body>` 的最顶上，是一条**全页宽**的横幅。它离输入框隔着
    整个记录区，而且出现时会把整页往下顶一行；
  * `#truncate-banner` 是 `.workspace` 的第一个子节点，占的是工作区的一行：
    出现时页头整体下移。更要紧的是它跟着页签走——换到「漏洞」「报告」就看不见了，
    而那两页读的恰好是**同一份被截断的图**。

现在两条都进 `#notice-stack`，和那张卡片对齐：位置由 `syncNoticeAnchor()` 按输入
卡片的实测上沿写进 `--notice-bottom`。为什么不能只用 CSS：「浮在卡片上方」要求
知道卡片上沿离视口底多远，而那个高度由输入框行数、目录是否展开、有没有选项行共同
决定，没有任何一个 CSS 变量能表达它。

## 为什么单独一个文件

与 `test_ui_contract.py` 是「专题与总目」的关系（同 `test_side_panel.py`、
`test_dock_dismiss.py`）：那一个守整页的结构契约，这里只守这一条——它被写歪时
**不会让任何功能测试变红**，只会让通知慢慢飘回它原来的两个角落。

## 写法

切源码文本做断言，不执行那份 JS（这个仓库没有 node/jsdom 那一层）。所以这里证明的
是「结构是这么摆的」；「真的贴在卡片上沿」要靠浏览器里看——两者互补，缺一半都行。
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def _strip_comments(text):
    """去掉 `/* */` 与 `//` 注释，只留会被执行的代码。

    **必须做这一步**：这些位置旁边正好写着注释（`index.html` 的注释里就写着
    `#truncate-banner`、`--notice-bottom`、`.chat-compose`），查注释会把
    「写清了为什么这么做」报成「这么做了」——守卫红在正确代码上，比漏报更糟。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _fn_body(js, name):
    """切出一个**顶层**函数的函数体（到下一行顶格的 `}` 为止）。找不到返回空串。

    不借用 `_fn_body` 那套按四空格缩进的写法：这里的函数都在顶格，按缩进切会把
    整份文件当成一个函数体——断言照样通过，却什么都没守住。
    """
    start = js.find("\nfunction %s(" % name)
    if start < 0:
        return ""
    end = js.find("\n}\n", start)
    return _strip_comments(js[start:end if end >= 0 else len(js)])


class NoticeStackContractTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _read("web", "index.html")
        cls.js = _read("web", "app.js")
        cls.css = re.sub(r"/\*.*?\*/", "", _read("web", "style.css"), flags=re.S)

    def _rule(self, selector):
        for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", self.css):
            if " ".join(sel.split()) == selector:
                return " ".join(body.split())
        return ""

    def _decl(self, rule, prop):
        match = re.search(r"(?:^|;)\s*%s:\s*([^;]+)" % re.escape(prop), rule)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _shorthand(value):
        """按**括号外**的空白切简写值（`calc(... + ...)` 里有空格，不能用 split()）。"""
        parts, depth, token = [], 0, ""
        for ch in value:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch.isspace() and depth == 0:
                if token:
                    parts.append(token)
                    token = ""
                continue
            token += ch
        if token:
            parts.append(token)
        return parts

    def _box_bottom(self, rule, prop="margin"):
        """取一个盒子的**下**边距：既认 `margin-bottom`，也认 `margin` 简写。

        `.chat-queue` / `.chat-auto-note` 都写成 `margin: 0 0 <值>` 这种三值简写
        （上 / 左右 / 下），直接读 `margin-bottom` 会读到空串——那条守卫就会在
        「写对了」的代码上变红，比漏报更糟。"""
        direct = self._decl(rule, prop + "-bottom")
        if direct:
            return direct
        parts = self._shorthand(self._decl(rule, prop))
        if len(parts) == 1:
            return parts[0]
        if len(parts) == 2:
            return parts[0]
        if len(parts) == 3:
            return parts[2]
        if len(parts) == 4:
            return parts[2]
        return ""

    _COMPOSE_BLOCKS = ("chat-auto-note", "chat-queue", "chat-choice",
                       "chat-quick", "chat-field")

    def _compose_chain(self):
        """按 index.html 的兄弟顺序，取 `#chat-auto-note` 与卡片之间的那几块。

        不读 DOM（这个仓库没有 jsdom 那一层），所以用标记在源码里的先后位置定序
        —— 这几块都是 `.chat-compose` 的一级子节点，位置就代表顺序。顺序反了
        `:has(+ …)` 会匹配不上，而 CSS 一条错都不报，只是那一行又看不见了。
        """
        order = []
        for name in self._COMPOSE_BLOCKS:
            marker = 'class="chat-field"' if name == "chat-field" else 'id="%s"' % name
            pos = self.html.find(marker)
            self.assertNotEqual(pos, -1, "index.html 里找不到 %s" % marker)
            order.append((pos, name))
        names = [name for _, name in sorted(order)]
        start, stop = names.index("chat-auto-note"), names.index("chat-field")
        self.assertLess(start, stop,
                        "`#chat-auto-note` 不在卡片前面了：那一行会掉到输入框下面")
        chain = names[start + 1:stop]
        self.assertTrue(chain, "自动继续那一行已经紧挨着卡片：`:has()` 判据无从谈起，"
                               "基础规则本身就必须补偿")
        for name in chain:
            self.assertIn("display: none", self._rule(".%s[hidden]" % name),
                          "`.%s[hidden]` 没有关掉显示：链子里那一格永远算“在场”，"
                          "补偿分支永远不生效" % name)
        return chain

    def test_the_two_notices_live_in_one_floating_box(self):
        """两条通知必须在同一个盒子里，而且各自只有一份。"""
        stack = self.html.find('id="notice-stack"')
        self.assertGreaterEqual(stack, 0, "没有 #notice-stack：两条通知又各飞各的角落了")
        for ident, where in (("error-bar", "body 顶上"),
                             ("truncate-banner", "工作区顶上")):
            self.assertEqual(self.html.count('id="%s"' % ident), 1,
                             "#%s 出现了不止一次或一次都没有" % ident)
            self.assertGreater(self.html.find('id="%s"' % ident), stack,
                               "#%s 不在通知栈里，它会回到%s去" % (ident, where))

    def test_the_banner_no_longer_takes_a_row_in_the_workspace(self):
        """留在工作区里的那一份会挤掉页头，而且换页签就看不见。"""
        start = self.html.find('<div class="workspace">')
        end = self.html.find('<main class="panels">')
        self.assertGreaterEqual(start, 0, "找不到工作区的开头")
        self.assertGreater(end, start, "找不到工作区里的面板容器")
        self.assertNotIn("truncate-banner", self.html[start:end],
                         "截断提示又回到工作区里了：它会挤掉一行，且只在链路页可见")

    def test_the_stack_floats_and_takes_its_bottom_from_the_anchor(self):
        """浮层 + 底部由锚点给出 + 压层低于模态遮罩。"""
        rule = self._rule(".notice-stack")
        self.assertIn("position: fixed", rule,
                      "通知栈不是浮层：它一出现就会把下面的布局顶开一行")
        self.assertIn("var(--notice-bottom", rule,
                      "bottom 没接锚点：浮层会停在某个写死的位置，不再跟着卡片走")
        z = self._decl(rule, "z-index")
        self.assertTrue(z.isdigit(), "通知栈没有自己的压层，会被记录区或卡片盖住")
        self.assertLess(int(z), 40, "通知栈压过了模态遮罩（40）：弹窗打开时通知会浮在它上面")
        self.assertIn("pointer-events: none", rule,
                      "浮层的空白处会拦住点击：卡片上方那一片会点不动")

    def test_the_top_fallback_exists(self):
        """没有输入卡片的页签（未登录、或被隐藏）要退回顶部，而不是消失。"""
        rule = self._rule('.notice-stack[data-anchor="top"]')
        self.assertIn("top:", rule, "没有退回顶部的规则：卡片一隐藏，通知就留在原地或看不见")
        self.assertIn("bottom: auto", rule,
                      "退顶时没有撤掉 bottom：浮层会被同时按顶部和底部定位，高度被拉满")

    def test_the_anchor_is_measured_from_the_composer(self):
        """位置只能是量出来的：拿卡片上沿换算出距视口底的距离，写回 CSS 变量。"""
        body = _fn_body(self.js, "syncNoticeAnchor")
        self.assertIn(".chat-compose", body, "锚点没有读输入卡片")
        self.assertIn("getBoundingClientRect", body,
                      "没有量卡片上沿：位置只能猜（写死一个值就会随输入框行数漂移）")
        self.assertIn("--notice-bottom", body, "量到了却没写回 CSS 变量")
        self.assertIn("dataset.anchor", body,
                      "没有切回顶部的分支（`dataset.anchor = 'top'`）：卡片不在时通知会停在上一次的位置")
        self.assertIn("getClientRects", body,
                      "用高度判断可见性会误判 0 高卡片 / display:none 的页签")

    def test_the_anchor_is_recomputed_at_the_three_moments_it_moves(self):
        """卡片换位置只有三种来源：换页签、窗口改大小、卡片自己变高。"""
        self.assertIn("syncNoticeAnchor()", _fn_body(self.js, "switchTab"),
                      "换页签不重算：通知会停在上一个页签的位置")
        wire = _fn_body(self.js, "wireNoticeAnchor")
        self.assertIn("'resize'", wire, "窗口改大小不重算")
        self.assertIn("ResizeObserver", wire,
                      "卡片自己长高/变矮（目录展开、选项行出现）不重算")
        self.assertIn("wireNoticeAnchor()", _fn_body(self.js, "init"),
                      "启动时没有把浮层放到位：第一次报错会停在 body 顶上")

    def test_the_float_hugs_the_card_not_the_rows_above_it(self):
        """参照只留卡片本身：用"最高可见边缘"贴，浮层会被上面几行顶出几百像素。

        用户原话「准确说弹窗应该是浮层贴」。实测过按最高边缘贴的结果：排队 1 条
        离卡片 47px、4 条 121px、自动继续那行 254px、坞展开 82px。
        """
        body = _fn_body(self.js, "syncNoticeAnchor")
        self.assertIn(".chat-field", body,
                      "锚点没有读画出来的那张卡片：`.chat-compose` 是布局盒，比卡片低一整段")
        for banned in ("chat-quick", "chat-queue", "chat-auto-note"):
            self.assertNotIn(banned, body,
                             "锚点又去看 `%s` 了：浮层会被卡片上方那一行顶上去，不再贴着输入框" % banned)
        match = re.search(r"const gap = (\d+);", body)
        self.assertIsNotNone(match, "找不到浮层与卡片之间的空隙（gap）")
        self.assertLessEqual(int(match.group(1)), 12,
                             "空隙超过 12px：这条提示属于卡片，离开一块就不像贴着它了")
        self.assertGreaterEqual(int(match.group(1)), 0, "空隙不能为负（浮层会压进卡片里）")

    def test_the_float_is_repositioned_when_it_is_shown(self):
        """显示的那一帧就要在对的位置：观察器那条路要等下一帧。"""
        self.assertIn("syncNoticeAnchor()", _fn_body(self.js, "showError"),
                      "显示错误条时不重算：无头/后台标签下 rAF 会被饿死，浮层停在旧位置")
        # chat.js 整个文件包在一个 IIFE 里，函数是缩进的，_fn_body 只认顶格函数，
        # 所以这里按"名字 + 紧跟的一段"截取（同样先剥注释，免得读到自己的说明）。
        chat = _strip_comments(_read("web", "chat.js"))
        at = chat.find("function showAutoNote(")
        self.assertGreater(at, 0, "chat.js 里找不到 showAutoNote")
        self.assertIn("syncNoticeAnchor()", chat[at:at + 400],
                      "自动继续那一行出现/收起时不重算：它会改卡片上沿")


    def test_the_pause_note_compensates_the_card_overlap(self):
        """自动继续那一行必须让出卡片叠进来的那段，**但只在它是卡片邻居的时候**。

        `.chat-field` 用 `margin-top: calc(-1 * var(--chat-overlap))` 把自己抬进
        上一位兄弟的盒子，抬多少就吃掉多少——所以要让的只有"最后一个兄弟"。
        `.chat-auto-note` 后面永远跟着 `#chat-queue`，它默认不是邻居，只留普通间距；
        排队块不在（`hidden` 是它唯一的开关）时才是，这时才补 `--chat-overlap`。
        两个方向都得守：少了补偿会被卡片整块吃掉（实测 note 684–752、卡片 626–768，
        `elementFromPoint()` 命中 `chat-input`，暖色底零像素）；补偿没收住则两行之间
        凭空多出一块空白，`.chat-compose` 长高一整段（实测 270 → 662，中间 158px 的洞）。
        """
        note = self._rule(".chat-auto-note")
        self.assertTrue(note, "找不到 .chat-auto-note 的规则")
        self.assertNotIn("var(--chat-overlap)", self._box_bottom(note),
                         "基础规则里补偿了 `--chat-overlap`：排队块在场时这一行并不是"
                         "卡片的邻居，会让两行之间凭空多出一块空白")
        # `:has(+ …)` 才是"我是邻居"的判据：紧跟着的那几格都是隐藏的。
        # 链子**从 index.html 推出来**，不写死字符串：t94 把选项面板搬到队列与卡片
        # 之间，选择器就从 `:has(+ .chat-queue[hidden])` 长成
        # `:has(+ .chat-queue[hidden] + .chat-choice[hidden])`。写死的话，布局一搬家
        # 守卫就红得莫名其妙；真正该守的是"这条链子把卡片与自己之间的每一格都盖住了"。
        chain = self._compose_chain()
        expected = ".chat-auto-note:has(+ %s)" % " + ".join(".%s[hidden]" % n for n in chain)
        adj = self._rule(expected)
        self.assertTrue(adj, "没有 `%s` 那条：这一行永远不会补偿，会被卡片整块盖住"
                             "（盒子顺序一直是对的，静态断言看不出来。推导自 index.html："
                             "从它到卡片之间现在是 %s）"
                             % (expected, " / ".join(chain)))
        queue = self._box_bottom(self._rule(".chat-queue"))
        self.assertTrue(queue, "找不到 .chat-queue 的下边距")
        self.assertEqual(" ".join(self._box_bottom(adj).split()), " ".join(queue.split()),
                         "两条卡片上方提示的补偿算式不一样了：同一个 `--chat-overlap` "
                         "只能有一种算法，否则其中一条迟早会被卡片压住")
        self.assertIn("var(--chat-overlap)", self._box_bottom(adj),
                      "邻居分支里没有补偿：卡片会把它整块吃掉")
        self.assertIn("display: none", self._rule(".chat-auto-note[hidden]"),
                      "`[hidden]` 时没有关掉显示：空着的提示条会留在输入框上面")


if __name__ == "__main__":
    unittest.main()
