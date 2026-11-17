# -*- coding: utf-8 -*-
"""输入辅助坞「点别处收起」：**三条收起路径必须落到同一个状态上**。

报上来的原话是「可用能力和技能手册点击打开后，点击其他位置自动关闭」。这两格是
对话页输入框上方那个坞（`#chat-quick`），入口是输入框里那两颗按钮
（`#chat-tools` 可用能力 / `#chat-skills` 技能手册）。改版前只有一条收起路径：
**再点一次同一颗按钮**（`setDock` 里的 toggle）。于是展开之后想收起来，必须回到
那颗按钮上再点一次——而使用者的第一反应是点别处，点完没反应，看起来像「关不掉」。

## 这一版加了什么

- 一个**文档级 `click` 监听**（冒泡阶段）：点坞外任意位置收起；
- 它的键盘等价物：Esc；
- `insideDock()` 划出「坞内」的边界：内容区 + 那两颗入口按钮。

## 为什么单独一个文件

与 `test_ui_contract.py` 的关系是「专题与总目」（同 `test_side_panel.py`）：那一个
守整页的结构契约，这里只守这一条容易被写歪、写歪了**不会让任何功能测试变红**的
性质。

## 最容易写歪的三处，就是下面这组用例里的重点

1. **拿 `setDock` 去收起**。它那一行是「点当前这一格就收起」的开关，要求知道用户点
   的是哪一格；而「点别处」这条路上没有那一格。用 `setDock('tools')` 去收，用户正
   看着技能手册时会被**换成能力目录**——两格内容看着都正常，表现为「点哪儿都不对」。
2. **忘了把两颗按钮算作坞内**。点另一颗按钮会先被文档级监听收起、再被它自己的 toggle
   展开；两次都发生了，但顺序反了就成了「点一下没反应」（看上去一直开着）。
3. **收起时顺手动了输入框**。收起是副作用最小的一次状态变化：已经打好的那行字一个字
   都不该动，也不该抢焦点——它是一个「关掉辅助」的动作，不是一次输入。

## 写法

与 `test_side_panel.py` 同一套：切源码文本做断言，**不执行**那份 JS（这个仓库没有
node/jsdom 那一层）。所以这里只能证明「代码是这么写的」；**「点下去真的收起来了」由
`tools/probe_dock_dismiss.py` 在真实浏览器里验**（夹具 + 无头 Edge），两者互补。
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
    """去掉 `//` 与 `/* */` 注释，只留下会被执行的代码。

    **必须做这一步**：本文件查的是「某个东西有没有出现」，而这几段代码旁边的注释里
    正好会提到它（`closeDock` 的注释里写着 `setDock`、`insideDock` 的注释里写着
    `.dock-tab`）。查注释会把「写清了为什么不能这么做」报成「这么做了」。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _fn_body(text, name):
    """按缩进切出一个函数的函数体（含首行）。找不到返回空串。"""
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


def _listener(text, needle):
    """取**这一个**监听器的注册语句与它的函数体，到它自己那个 `});` 为止。

    比「从 needle 起固定截 N 个字符」强：那种截法会溢出到**下一个**监听器里，于是
    「查 click 监听器」的断言实际上也在管 Esc 那条（看起来更严，其实是碰巧）。
    两个监听器的函数体里都不含 `});`，所以第一个就是终结符。
    """
    at = text.find(needle)
    if at < 0:
        return ""
    rest = text[at:]
    end = rest.find("});")
    return rest if end < 0 else rest[:end + 3]


class DockDismissTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chat = _read("web", "chat.js")
        cls.code = _strip_comments(cls.chat)
        cls.html = _read("web", "index.html")
        cls.css = _strip_comments(_read("web", "style.css"))
        cls.close = _fn_body(cls.chat, "closeDock")
        cls.inside = _fn_body(cls.chat, "insideDock")
        cls.toggle = _fn_body(cls.chat, "setDock")
        cls.bind = _fn_body(cls.chat, "cacheAndBind")

    # ---------------------------------------------------------------- 1 ----

    def test_close_is_a_state_not_a_toggle(self):
        """收起只有一个去向（`none`），且**不复用** `setDock` 的开关语义。"""
        self.assertTrue(self.close, "找不到 closeDock：点别处没有收起路径")
        self.assertIn("view.dock = 'none'", self.close, "收起没有把模式写回 none")
        self.assertIn("renderDock()", self.close,
                      "收起必须走 renderDock：它是唯一把 data-dock 写回 DOM 的地方，"
                      "自己另写一遍属性就会有两条可以互相矛盾的路")
        self.assertNotIn("setDock", self.close,
                         "收起走了 setDock：那是「点当前这一格就收起」的开关，拿另一格"
                         "的名字去收，会在用户正看着技能手册时换成能力目录")

    def test_the_toggle_path_still_works(self):
        """再点一次同一颗按钮收起——这条**不能**被新路径挤掉。"""
        self.assertIn("view.dock === mode ? 'none' : mode", self.toggle,
                      "点当前这一格必须回到收起态，否则展开之后收不回去")

    # ---------------------------------------------------------------- 2 ----

    def test_the_outside_click_listener_is_registered_exactly_once(self):
        """文档级监听只许有一处，且在 `cacheAndBind` 里（那里有 `view.bound` 闸门）。

        散在模块顶层或第二次绑定时，同一次点击会走两遍收起——今天看不出问题
        （第二遍是空操作），等哪天收起里加了动画或埋点，症状是「点一下收两次」。
        """
        found = re.findall(r"document\.addEventListener\('click'", self.code)
        self.assertEqual(len(found), 1,
                         "文档级 click 监听的注册有 %d 处（期望 1）：重复绑定会让一次"
                         "点击跑多遍收起" % len(found))
        self.assertIn("document.addEventListener('click'", self.bind,
                      "监听没有挂在 cacheAndBind 里——只有那里有 view.bound 这个"
                      "只跑一次的闸门")
        self.assertIn("view.bound", self.code, "cacheAndBind 的一次性闸门不见了")

    def test_the_click_listener_is_guarded_by_containment(self):
        """同一次点击里，按钮自己的 handler 先跑，这一条只判「是不是坞外」。"""
        window = _listener(self.code, "document.addEventListener('click'")
        self.assertIn("view.dock === 'none'", window,
                      "收起态没有被区分出来：这一页每一次点击都会走到这里")
        self.assertIn("insideDock(event.target)", window,
                      "没有判断点击落点就收起：点坞里的卡片会连坞一起收掉，而点卡片的"
                      "本意是「再挑一张」")
        self.assertIn("closeDock()", window, "守卫齐了但没有真的收起")

    # ---------------------------------------------------------------- 3 ----

    def test_the_two_entry_buttons_count_as_inside(self):
        """`insideDock` 的边界：内容区 + 两颗入口按钮。

        按钮那一半最关键：漏了它，点另一颗按钮会**收起后立刻展开**（document 上的
        监听在按钮的 handler 之后跑），看结果是「点一下没反应」。所以这里同时钉住
        判据用的类名，以及 index.html 里那两颗按钮**真的**带着这个类名。
        """
        self.assertTrue(self.inside, "找不到 insideDock")
        self.assertIn("dom.quick.contains(target)", self.inside, "内容区没有算作坞内")
        self.assertIn("'.dock-tab'", self.inside,
                      "入口按钮没有算作坞内：点另一颗会先收起再展开，表现为没反应")
        tabs = re.findall(r'class="dock-tab"', self.html)
        self.assertEqual(len(tabs), 2,
                         'index.html 里 class="dock-tab" 出现 %d 次（期望 2）：类名改了'
                         "而 JS 的判据没跟着改，点入口就会「没反应」" % len(tabs))
        self.assertIn(".dock-tab", self.css,
                      "样式表里没有 .dock-tab——判据用的类名必须是页面真在用的那个")
        for anchor in ("chat-tools", "chat-skills"):
            self.assertIn('id="%s" class="dock-tab"' % anchor, self.html,
                          "%s 不再是 .dock-tab 了" % anchor)

    def test_unknown_targets_are_treated_as_outside(self):
        """判不出落点时倒向「坞外」。

        两种错法不对称：误判成坞外最多多收一次（再点一下就开），误判成坞内是
        **永远收不起来**——那正是本次要修的那个症状。
        """
        # 断言**返回值**，不是"提到了 closest"：写 `return true` 同样能过前者，
        # 而那正是这条要挡住的写法（变异 M12 抓的就是这一步）。
        self.assertIn("!target.closest) return false;", self.inside,
                      "目标没有 closest（文本节点 / 很老的浏览器）时要按坞外处理，"
                      "否则这一条会变成「收不起来」的唯一原因")

    # ---------------------------------------------------------------- 4 ----

    def test_dismiss_does_not_touch_the_draft_or_the_focus(self):
        """收起不是一次输入：输入框的字与焦点都不该被碰。"""
        for forbidden in ("dom.input", ".value", "focus(", "preventDefault",
                          "showError"):
            self.assertNotIn(forbidden, self.close,
                             "closeDock 里出现了 %s：收起只该改 view.dock 与重画坞"
                             % forbidden)
        window = _listener(self.code, "document.addEventListener('click'")
        self.assertNotIn("preventDefault", window,
                         "文档级监听里 preventDefault：会吃掉这一页其他控件的默认行为"
                         "（点按钮、拖分栏都从这一次点击开始）")
        self.assertNotIn("hideError", window,
                         "文档级监听里 hideError：点任意位置都会抹掉错误提示，而那句"
                         "话可能正是使用者刚要读的")

    # ---------------------------------------------------------------- 5 ----

    def test_escape_is_the_keyboard_equivalent(self):
        """Esc 收坞。与 `auth.js` 的密码弹窗同一语义，且**只管坞**。"""
        # 文档级 keydown 不止一处（「加载更早」的 Alt+↑ / PageUp 入口也是），所以不数
        # 注册总数，只数**碰到坞**的那一处：坞的 Esc 是唯一收坞的键盘入口。
        blocks = re.findall(r"document\.addEventListener\('keydown'.*?\n    \}\);",
                            self.code, re.S)
        dock = [block for block in blocks if 'closeDock()' in block]
        self.assertEqual(len(dock), 1,
                         "收坞的文档级 keydown 不是恰好一处（%d）" % len(dock))
        self.assertIn("event.key !== 'Escape'", dock[0],
                      "收坞的 keydown 应当只认 Esc")
        window = _listener(self.code, "document.addEventListener('keydown'")
        self.assertIn("'Escape'", window, "没有判 Escape 键")
        self.assertIn("closeDock()", window, "判了键但没收起")
        self.assertNotIn("dom.input", window,
                         "Escape 顺带动了输入框：它只该收回坞，不该清掉已经打好的字")

    # ---------------------------------------------------------------- 6 ----

    def test_closing_when_already_closed_is_a_no_op(self):
        """已经收起时直接返回，**在写状态之前**。

        这条监听挂在这一页每一次点击上，而它按的正是输入框旁边那条热路径。每次都
        重画一遍坞（清空 + 重建二十多张卡片）看不出错，但它是白做的工。
        """
        early = self.close.find("return")
        write = self.close.find("view.dock = 'none'")
        self.assertGreater(early, -1, "没有提前返回")
        self.assertLess(early, write, "提前返回写在状态写入之后：那等于没有提前返回")
        self.assertIn("view.dock === 'none'", self.close[:early],
                      "提前返回的条件不是「已收起」")

    # ---------------------------------------------------------------- 7 ----

    def test_the_signout_reset_agrees_with_the_dismiss_path(self):
        """退出登录那条路也把坞复位，且同样不走 toggle。

        留着坞展开着换人，下一个人看到的是一格**按上一个人的权限档过滤过的**目录
        （`view.tools` / `view.skillList` 已被清空，于是是一格空的坞）。
        """
        body = _fn_body(self.chat, "onSignedOut")
        self.assertIn("view.dock = 'none'", body, "退出登录没有复位坞的模式")
        self.assertNotIn("setDock", body,
                         "退出登录用了 toggle：那会把「已经收起」翻成展开")


if __name__ == "__main__":
    unittest.main()
