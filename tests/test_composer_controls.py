# -*- coding: utf-8 -*-
"""输入区三件新东西的静态契约（t80）。

这一版把三样东西搬进了对话框：

  1. **权限档 chip**：一个 28px 的无框小钮（盾牌 + 当前档名 + 箭头），弹出的是
     `menuitemradio` 菜单；切到「完全作业」前必须多问一句（勾选后才可启用）。
     原生 `<select id="chat-policy">` 留着当唯一真相源，菜单只改它的值再派发
     `change` —— 落库那段路径因此一条都没变。
  2. **审批卡进对话记录**：`renderReviews()` 把卡片 append 到 `#chat-log`，
     输入框上方那一格（`#chat-review-slot`）已经删掉。
  3. **失败自动继续**：一轮非人为失败后等几秒、自动发一句带幂等提示的「继续」，
     连续失败按 3→20→40→80→120 秒退避，最多 5 次；永久性错误直接跳过并说明；
     人一动（发送 / 停止 / 关开关 / 切会话）就撤销。

真实浏览器里这三样的行为由 `tools/probe_boot_chain.py` 的 `button` / `review`
两场跑；这里守的是源码里的结构，防止有人把它们改回去或改成两处真相。
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def strip_comments(text: str) -> str:
    """去掉块注释与整行 `//` 注释（字符串里的 `//` 不动，只去整行）。"""
    without_block = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line for line in without_block.splitlines()
                     if not line.strip().startswith("//"))


def slice_function(js: str, name: str) -> str:
    """取一个函数体（从 `function <name>(` 到下一个两空格缩进的 `}`）。"""
    return js.split("function %s(" % name)[1].split("\n  }")[0]


class ComposerControlsTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "web", "index.html"), "r", encoding="utf-8") as handle:
            cls.html = handle.read()
        with open(os.path.join(ROOT, "web", "chat.js"), "r", encoding="utf-8") as handle:
            cls.raw_js = handle.read()
        cls.js = strip_comments(cls.raw_js)

    def _actions_row(self):
        """动作行那一段 HTML —— 从开标签切到它自己的收口。

        原先这里是一句 `self.html[at:at + 2400]`：写死的字数窗口。t100 在动作行里
        加了模型 chip 的来龙去脉（一段必须留着的注释），`#chat-policy-menu` 就被挤出
        窗口、判据变红，而标记本身是对的。窗口改成跟着结构走：动作行的最后一个子元素
        是发送键，它收口之后紧跟动作行的 `</div>`。
        """
        at = self.html.index('<div class="chat-actions">')
        send = self.html.index('id="chat-send"', at)
        shut = self.html.index("</button>", send)
        end = self.html.index(chr(10) + "            </div>", shut)
        return self.html[at:end]

    @staticmethod
    def _style_css():
        """整份 style.css 的文本（用到时现读：这份文件不是每个用例都要）。"""
        with open(os.path.join(ROOT, "web", "style.css"), encoding="utf-8") as handle:
            return handle.read()

    # ---- ① 权限档 chip ------------------------------------------------

    def test_the_permission_chip_lives_in_the_composer_row(self):
        row = self._actions_row()
        for node in ('id="chat-policy"', 'id="chat-policy-btn"',
                     'id="chat-policy-name"', 'id="chat-policy-menu"'):
            self.assertIn(node, row, "%s 要在 .chat-actions 里，与发送同排" % node)
        self.assertLess(row.index('id="chat-policy"'), row.index('id="chat-policy-btn"'),
                        "原生 select 必须先出现：它是唯一真相源，菜单只改它")
        self.assertIn('aria-haspopup="menu"', row)

    def test_the_chip_names_the_effective_preset_and_marks_the_default(self):
        body = slice_function(self.js, "syncPolicyChip")
        self.assertIn("defaults", body, "没选的时候要回落到会话默认档")
        self.assertIn("（默认）", body, "回落到默认时要标出来，否则像是我选的")

        menu = slice_function(self.js, "renderPolicyMenu")
        self.assertIn("menuitemradio", menu)
        self.assertIn("aria-checked", menu)
        self.assertIn("chat-policy-item-default", menu, "默认档那一行要有个默认标记")

    def test_the_chip_goes_through_the_old_select(self):
        """菜单不能自己写 `view.config`：改值 + 派发 change，落库走原路。"""
        apply_body = slice_function(self.js, "applyPolicy")
        self.assertIn("dom.policy.value = id", apply_body)
        self.assertIn("dispatchEvent(new Event('change'", apply_body)

    # ---- ② 模型 chip（t100）--------------------------------------------
    #
    # 用户这一轮的两件事：「去除蓝底」「选择的模型后面增加√」。两件都做在**页内**
    # 菜单上：原生 `<select>` 的弹出层画不出行内的勾（`<option>` 放不进 DOM、伪元素
    # 也不渲染），而它的行底色由 UA 拿我们的 `option` 底去配它自己的高亮——实测
    # 一行是我们的 `--bg`、一行是 UA 的灰，那正是用户说的「蓝底」。

    def test_the_model_chip_lives_in_the_composer_row(self):
        row = self._actions_row()
        for node in ('id="chat-model-select"', 'id="chat-model-btn"',
                     'id="chat-model-name"', 'id="chat-model-menu"'):
            self.assertIn(node, row, "%s 要在 .chat-actions 里，与权限、发送同排" % node)
        self.assertLess(row.index('id="chat-model-select"'), row.index('id="chat-model-btn"'),
                        "原生 select 必须先出现：它仍是唯一真相源，菜单只改它")
        self.assertIn('aria-haspopup="menu"', row)
        self.assertIn('aria-expanded="false"', row,
                      "收起态必须显式写 false：读屏靠它播报「已展开 / 已收起」")

    def test_the_native_select_stays_the_only_source_of_truth(self):
        """原生 select 是**隐形**的，但它必须在页面上。

        改成 `display: none` 也能"看起来对"——`fillSelect` 照样能写它、`change`
        照样能派发，页面上一点异常都没有。坏在别处：某些浏览器把 `display: none`
        的表单控件从无障碍树里摘掉，而 `aria-label="模型选择"` 正是它被念出来的
        唯一途径；更要紧的是"看不见的控件才是真相源"这件事一旦没人钉，下一个人
        很容易把落库也挪到菜单里，那时"界面换了、发出去的还是旧的"就回来了。
        """
        row = self._actions_row()
        select = row[row.index('id="chat-model-select"'):]
        select = select[:select.index(">")]
        self.assertIn('aria-label=', select,
                      "隐形控件必须自带名字：它是纯图标/无字控件的唯一可读名")
        self.assertIn('class="chat-model-native"', select)
        css = self._style_css()
        native = css[css.index(".chat-model-native {"):]
        native = native[:native.index("}")]
        self.assertIn("position: absolute", native)
        self.assertIn("opacity: 0", native, "隐形但仍在文档里（不是 display: none）")

    def test_the_model_menu_checks_the_chosen_row(self):
        menu = slice_function(self.js, "renderModelMenu")
        self.assertIn("menuitemradio", menu, "一组单选：读屏会念「已选中 / 未选中」")
        self.assertIn("aria-checked", menu)
        self.assertIn("chat-model-item-check", menu)
        self.assertIn("'✓'", menu, "用户要的是那个勾本身，不是一个换了颜色的底")
        # 勾**只**在菜单里打：收起态那一行只有一个值，没有"别的"可比。
        chip = slice_function(self.js, "syncModelChip")
        self.assertNotIn("✓", chip,
                         "chip 上不许出现勾：那是「从这些里挑了这一个」的记号")

    def test_the_model_menu_paints_no_blue_row(self):
        css = self._style_css()
        hover = css[css.index(".chat-model-item:hover"):]
        hover = hover[:hover.index("}")]
        self.assertIn("var(--panel-3)", hover,
                      "悬停行只许用中性面：用户说的「蓝底」就是行底色跟着重点色走")
        for bad in ("--accent", "--accent-dim", "#ccdbf3"):
            self.assertNotIn(bad, hover,
                             "行底色里出现了 %s：那是蓝底，正是这一轮要消掉的东西" % bad)

    def test_full_access_asks_before_it_applies(self):
        choose = slice_function(self.js, "choosePolicy")
        self.assertIn("'full'", choose)
        self.assertIn("renderPolicyConfirm", choose)

        confirm = slice_function(self.js, "renderPolicyConfirm")
        self.assertIn("不会", confirm, "必须说明它不放宽门禁与审查闸")
        self.assertIn("checkbox", confirm)
        self.assertIn("confirm.disabled = !tick.checked", confirm,
                      "没勾风险确认之前，启用钮要按不动")

    def test_the_outside_click_uses_the_dispatch_path(self):
        """点菜单项会同步 `clear(menu)`：事件冒到 document 时按钮已不在文档里，
        `target.closest('.chat-policy')` 会是 null，于是"选一档"被当成"点了外面"，
        确认框当场关掉（切「完全作业」时表现为点了没反应）。路径在派发那一刻就固定，
        所以判内外必须用 `composedPath()`。"""
        bind = slice_function(self.js, "cacheAndBind")
        self.assertIn("composedPath", bind)
        self.assertIn("chat-policy", bind)

    # ---- ② 审批卡进对话记录 -------------------------------------------

    def test_the_review_card_is_drawn_into_the_transcript(self):
        self.assertNotIn('id="chat-review-slot"', self.html,
                         "输入框上方那一格已经撤了；再加回去会出现两份卡片")
        body = slice_function(self.js, "renderReviews")
        self.assertIn("dom.log", body)
        self.assertIn("log.append(box)", body)
        self.assertNotIn("dom.reviewSlot", self.js)

    # ---- ③ 失败自动继续 -----------------------------------------------

    def test_the_backoff_starts_small_caps_at_five_and_is_slow_enough(self):
        self.assertIn("const AUTO_MAX = 5", self.js)
        block = self.js.split("const AUTO_DELAYS")[1].split("]")[0]
        numbers = [int(n) for n in re.findall(r"\d+", block)]
        self.assertEqual(numbers[0], 3000, "第一次失败先等 3 秒：抖动常常自己就好了")
        self.assertEqual(numbers, sorted(numbers), "退避只能变长")
        self.assertGreaterEqual(numbers[-1], 60000,
                                "最后一档要足够长（≥60 秒），别把一次故障拖成一串请求")

    def test_permanent_failures_are_skipped_with_a_reason(self):
        body = slice_function(self.js, "scheduleAutoContinue")
        self.assertIn("AUTO_PERMANENT.test(why)", body)
        self.assertIn("自动继续跳过", body,
                      "跳过必须说一句为什么；静默跳过和没反应长得一样")

    def test_the_continue_text_tells_it_not_to_replay_side_effects(self):
        body = slice_function(self.js, "fireAutoContinue")
        self.assertIn("继续", body)
        self.assertIn("不要重复已经生效的操作", body,
                      "失败可能发生在工具已经执行之后：无脑重放会把副作用做两遍")

    def test_auto_continue_starts_from_failure_and_stops_on_human_action(self):
        send_body = slice_function(self.js, "sendMessage")
        self.assertIn("scheduleAutoContinue", send_body,
                      "请求本身失败（断网、超时）时要开始自动继续")
        self.assertIn("auto.failed = true", send_body)

        event = slice_function(self.js, "applyEvent")
        self.assertIn("scheduleAutoContinue", event,
                      "服务端报 error 帧（流还活着）时同样要开始")

        for name in ("send", "stopTurn", "newSession", "onSignedOut"):
            self.assertIn("cancelAutoContinue", slice_function(self.js, name),
                          "%s 之后不该还留着待发的续跑" % name)

    def test_a_stream_that_ends_without_done_counts_as_interrupted(self):
        """EOF 不是"跑完了"：服务端重启、代理掐断都会让流干净地关掉。

        不认这一种，界面上就是"停在半句、按钮变回发送、什么都没发生"——而插件
        （auto-continue）把 interrupted 与 error 同等对待，这里必须一样。"""
        body = slice_function(self.js, "consumeStream")
        self.assertIn("!live.done", body)
        self.assertIn("连接在没有收到结束帧的情况下断了", body)
        self.assertIn("scheduleAutoContinue", body)
        self.assertIn("signal.aborted", body, "人点了停止不是故障，不能续跑")

    def test_a_turn_with_no_visible_output_counts_as_failed(self):
        body = slice_function(self.js, "visibleOutput")
        self.assertIn("live.text", body)
        # 思考、工具、子智能体**都不算**产出：库里既有 "reasoning 3919、正文空、
        # trace 0" 的消息，也有一串工具跑完却一个字没写的（思考吃光 max_tokens 时
        # 就是这样）。把它们算成产出，这类回合永远不会续。
        for field in ("live.reasoning", "live.tools", "live.subagents"):
            self.assertNotIn(field, body)
        consume = slice_function(self.js, "consumeStream")
        self.assertIn("visibleOutput(live)", consume)
        self.assertIn("没有产出正文", consume)

    def test_an_empty_turn_note_carries_the_reason_when_there_is_one(self):
        """空正文那句话要说清"为什么空"。

        "预算被思考吃光"和"模型只想不说"是两件事：前者把 max_tokens 调大就好，
        后者只能再问一次。同一句话盖掉，读者不知道该动哪里。
        """
        body = slice_function(self.js, "emptyTurnNote")
        self.assertIn("message.finishNote", body)
        self.assertIn("这一轮没有产出正文", body)
        self.assertIn("模型只做了思考或工具调用", body)
        self.assertIn("emptyTurnNote(message)", slice_function(self.js, "messageNode"))
        self.assertIn("emptyTurnNote(live)", slice_function(self.js, "liveBody"))

    def test_a_blocked_continue_says_why_instead_of_vanishing(self):
        body = slice_function(self.js, "fireAutoContinue")
        self.assertIn("view.sending", body)
        self.assertIn("showAutoNote", body,
                      "丢下一次续跑必须说出来，否则看起来就是没生效")
        self.assertIn("canSend()", body)
        self.assertIn("没有可用的模型", body)

    def test_the_auto_switch_and_note_sit_around_the_input(self):
        self.assertIn('id="chat-auto-note"', self.html)
        self.assertLess(self.html.index('id="chat-auto-note"'),
                        self.html.index('<div class="chat-field">'),
                        "倒计时行在卡片**上方**，不能占输入框里的位置")
        # t82：开关从 `.chat-actions`（模型/权限/发送那一行）搬到**左边那一组**，
        # 紧挨着两颗目录入口。原因：动作行自己的宽度约 465px，卡片内容宽在 1440 下
        # 只有 390px，整行溢出卡片（发送键落在卡片外）；这颗开关独占 106px。
        tabs = self.html.index('class="chat-field-tabs"')
        toggle = self.html.index('id="chat-auto"')
        actions = self.html.index('<div class="chat-actions">')
        self.assertLess(tabs, toggle, "自动继续在左边那一组：目录入口之后")
        self.assertLess(toggle, actions,
                        "它在卡片里、动作行之前——动作行只剩模型 / 权限 / 发送")
        self.assertIn('aria-pressed="true"', self.html[toggle:toggle + 300], "默认开")
        self.assertNotIn('id="chat-auto"', self._actions_row(),
                         "自动继续不许回到动作行：那一行放不下（465px > 390px）")
        self.assertIn('id="chat-auto-cancel"', self.html)

    def test_the_toggle_does_not_persist_a_second_truth(self):
        """三项选择只有一份真相（服务端）。自动继续是界面偏好，刷新回默认开。"""
        self.assertNotIn("localStorage", self.raw_js)
        self.assertRegex(self.js, r"const auto = \{\s*on: true",
                         "默认必须是开的；写成读某个存储值就把第二份真相请回来了")

    # ---- ④ 发送键图标 -------------------------------------------------

    def test_the_send_button_swaps_inline_svg_icons(self):
        self.assertIn("ICON_SEND", self.js)
        self.assertIn("ICON_STOP", self.js)
        self.assertIn("dom.send.innerHTML", self.js,
                      "图标在 JS 里换：HTML 里空着，避免两种身份各写一份")
        send = slice_function(self.js, "setSending")
        self.assertIn("ICON_STOP", send)
        self.assertIn("ICON_SEND", send)

    # ---- ⑤ 中断文案 ---------------------------------------------------

    def test_an_aborted_fetch_is_not_reported_as_undefined(self):
        """`abort('user')` 之后，fetch 的拒绝值**就是那个字符串**。

        它没有 `name`、没有 `message`，所以 `'…：' + error.message` 会打出
        「对话请求失败：undefined」——这个错误在真浏览器里复现过（探针：发一轮、
        再点停止 → 错误条正是那句话）。这条守两件事：判据看**信号自己的 reason**，
        文案一律经 `describeError` 收口。
        """
        self.assertIn("signal.reason", slice_function(self.js, "interruptionOf"),
                      "判据必须是信号里的 reason：fetch 把 reason 原样当拒绝值")
        for name in ("sendMessage", "attachTurn"):
            body = slice_function(self.js, name)
            self.assertIn("interruptionOf(error, controller.signal)", body,
                          "%s 必须先判中断，再把它当成失败" % name)
            self.assertNotIn("error.message", body,
                             "%s 不许直接拼 error.message：中断时它会是 undefined" % name)
            self.assertIn("describeError(error)", body)

    def test_the_three_interruptions_have_their_own_meanings(self):
        body = slice_function(self.js, "interruptionOf")
        for word in ("'user'", "'idle'", "'signout'"):
            self.assertIn("reason === " + word, body)
        send = slice_function(self.js, "sendMessage")
        self.assertIn("interrupted === 'user'", send, "人点的停止：一个字都不说")
        self.assertIn("interrupted === 'idle'", send, "看门狗掐的：要说明还在服务端跑")
        self.assertIn("if (interrupted) return;", send)
        # 看门狗掐断**不自动续跑**：那一轮很可能还在服务端跑，再发一句只会撞 409；
        # 正确的恢复动作是接回去（刷新走的就是那条路）。
        self.assertLess(send.index("if (interrupted) return;"),
                        send.index("scheduleAutoContinue"),
                        "中断必须在续跑之前返回")

    def test_attaching_to_a_live_turn_has_the_same_treatment(self):
        attach = slice_function(self.js, "attachTurn")
        self.assertIn("interruptionOf(error, controller.signal)", attach)
        for word in ("'user'", "'idle'"):
            self.assertIn("interrupted === " + word, attach)
        self.assertIn("describeError(error)", attach)
        self.assertNotIn("没接上正在跑的那一轮：' + error.message", self.raw_js,
                         "这句曾经真的打出过「：undefined」")

    def test_the_watchdog_does_not_re_attach_into_the_same_stall(self):
        """看门狗掐断后 `finish()` 会再问一次“在跑吗”。

        不拦它就会立刻接上、再等三分钟、再掐——一个没有出口的圈。这条守
        “跳过下一次 attach” 的标记，以及它在 attachTurn 里确实被消费掉。
        真实的 3 分钟静默无法在静态契约里跑，行为由 `gen_abort_probe.py` 的
        attach 场（改成 idle 计时器）覆盖。
        """
        send = slice_function(self.js, "sendMessage")
        idle = send.index("interrupted === 'idle'")
        self.assertIn("view.attachPaused = true", send[idle:idle + 400],
                      "看门狗那一支必须立起这个标记，否则绕圈")
        attach = slice_function(self.js, "attachTurn")
        self.assertIn("view.attachPaused", attach)
        self.assertLess(attach.index("view.attachPaused"),
                        attach.index("apiGet"),
                        "标记要在问“在跑吗”之前就短路，不然已经接上了")

    def test_manual_refresh_retries_the_attach(self):
        """「刷新会话」是人明确说“再试一次”，不能也被那枚标记挡掉。

        挡掉的话，界面提示「刷新页面可以再接上」、而按下按钮什么也没发生——
        比不给提示更糟。整页刷新会重载脚本、标记自然不在了；按钮这条路要自己清。
        """
        at = self.js.index("dom.refresh.addEventListener")
        window = self.js[at:at + 500]
        self.assertIn("view.attachPaused = false", window,
                      "刷新按钮必须先清标记，再 openSession")

    def test_manual_refresh_is_the_retry_path(self):
        """「刷新会话」是那句话里给使用者的出口：它必须清掉一次性标记。

        不清的话，提示「刷新页面可以再接上」在**页内**刷新时就变成假的：按钮
        点下去，`openSession → attachTurn` 还是被自己立的那块牌子挡住。
        """
        at = self.js.index("dom.refresh.addEventListener")
        window = self.js[at:at + 420]
        self.assertIn("view.attachPaused = false", window,
                      "手动刷新要撤掉看门狗留下的标记，否则这一次刷新什么也不做")


class ReasoningBlockTestCase(unittest.TestCase):
    """思考块能不能「随时」点开（t85）、而且开着的文字是原地长的（t86）。

    `renderLive()` 每收到一帧就 `clear` 再整块重建「正在生成」的气泡，而
    `<details>` 的展开状态住在 DOM 上 —— 重建一次就没了。于是流还在往外冒的
    时候点开思考块，下一帧它就自己折回去，使用者看到的是「点了没反应」。

    修法是：状态存 `view.live.reasoningOpen`，重建前再从旧 DOM 读一次（点击后
    `toggle` 事件是异步派发的，只信 view 会丢一帧），重建后写回，并把 toggle
    接回 view。收尾换成落库消息时同理接力一次，否则答案出现的瞬间又折回去。

    静态契约守不住「真的点得开」——那一条由 `tools/probe_reasoning.py` 在真浏览器
    里跑（夹具里注入 600 帧思考，点开、等 900ms、再读 `open`），并且反向对照
    （把 `box.open = !!open` 换成 `box.open = false`）必须得到 `open=false`。
    t86 是同一个块的第二个坑：能点开之后，它仍然是每帧整块重画的，于是文字被整段
    替换而不是增长——读起来像一直在刷新（真浏览器 4.4 秒里造 45 个块）。修法是把
    这一块交给 `syncReasoning()` 原地追加。

    t87 是第三个坑：只把思考块"留下来"还不够。旧实现每帧仍要 `clear(node)` 重画整个
    气泡，于是那块每 16ms 被摘下再插回一次——滚轮落下的那一格常常正好打在"这一瞬间
    不在文档里"的元素上，拖滚动条更会在半路被换掉，表现是「能打开，但滚不动」。
    现在气泡改成**三个固定槽位**（head / reasoning / body）：槽位只建一次，每帧只
    重填 head 与 body 的内容，思考块连父子关系都不变。

    t88 是第四个坑，藏在同一个气泡的正文里：正文每帧也被整块重填，长回答等于
    每 16ms 重建整篇（真浏览器 42 个采样点里 42 个不同的首段、3018 次元素写入）。
    现在 `paintLiveBody()` 把"空行之后不会再变"的块定格，只重画还在长的尾巴——同一
    段流里首段只换 7 次（开头的占位切换与工具边界），元素写入降到 87。

    t86 由 `tools/probe_reasoning_stream.py` 在真浏览器里量（before/after 两个场景
    并排：45 个节点 vs 1 个），t88 由 `tools/probe_body_stream.py` 量（42 vs 7 个
    首段）。这里守的是：这几处结构别被人改回去。
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "web", "chat.js"), "r", encoding="utf-8") as handle:
            cls.js = strip_comments(handle.read())

    def test_the_block_takes_its_open_state_from_the_caller(self):
        """`<details>` 自己不记状态：第一次造它时由调用方写回。

        没有 `box.open = !!open` 这一行，传进来的状态就只是个被忽略的形参，
        而 `openImmediately` 之后的第一次重画就会折回去。
        """
        body = slice_function(self.js, "reasoningBlock")
        self.assertIn("box.open = !!open", body,
                      "思考块必须按调用方给的状态落 open，否则流式期间点不开")

    def test_the_block_is_created_once_and_never_detached(self):
        """思考块只造一次，之后每帧只在原地追加（t86/t87）。

        不能每帧摘下来再插回去：滚轮落下的那一格正好打在"这一瞬间不在文档里"的
        元素上，拖滚动条会在半路被换掉——「能打开，但滚不动」就是这么多出来的。
        展开状态仍由调用方在**造节点那一刻**写进去（`view.live.reasoningOpen`）。
        """
        body = slice_function(self.js, "paintLiveReasoning")
        self.assertIn("reasoningBlock(text, !view.live.done, view.live.reasoningOpen)", body,
                      "第一次造块要把展开状态传进 reasoningBlock")
        self.assertIn("node.insertBefore(box, node.__body)", body,
                      "位置固定夹在 head 与 body 之间，只插一次")
        self.assertEqual(body.count("box.remove()"), 1,
                         "摘除只允许出现在「这一轮没有思考了」那个分支里")
        self.assertIn("node.__reasoning = box", body, "槽位记在气泡上，下一帧复用")

    def test_the_block_grows_in_place(self):
        """流式期间那一块**只追加**，不重建（t86）。

        真浏览器采样：旧写法 4.4 秒里造了 45 个思考块、45 个 `<pre>`（每个采样点
        一个新节点）；新写法各 1 个。没有这条，文字就是每帧被整段替换——读起来
        像一直在刷新，正在看的那一行、框内滚动、选中的文字都留不住。
        """
        body = slice_function(self.js, "syncReasoning")
        self.assertIn("pre.append(document.createTextNode(text.slice(shown.length)))", body,
                      "前缀没变时只能追加增量，不能整段重写 textContent")
        self.assertIn("text.indexOf(shown) === 0", body,
                      "要先判前缀；直接 append 会把重写的文本拼在后面")
        self.assertIn("size.textContent !== label", body,
                      "字数标签只在真变化时改，否则每帧都是一次无谓的写入")
        live = slice_function(self.js, "paintLiveReasoning")
        self.assertIn("syncReasoning(box, text, !view.live.done)", live,
                      "已有旧块时必须走增量，而不是重新 reasoningBlock")
        self.assertEqual(live.count("addEventListener('toggle'"), 1,
                         "toggle 只在造节点时挂一次；每帧重挂会一直往后积")

    def test_the_toggle_writes_back_into_the_live_turn(self):
        """用户点开/收起之后，状态要写回这一轮。"""
        body = slice_function(self.js, "paintLiveReasoning")
        at = body.index("addEventListener('toggle'")
        self.assertIn("view.live.reasoningOpen = box.open", body[at:at + 200],
                      "toggle 必须写回 view.live.reasoningOpen")

    def test_the_bubble_keeps_fixed_slots(self):
        """气泡的三个槽位建一次：head / reasoning / body（t87）。

        滚动能动的前提是**这一块一直是同一个节点**。旧实现每帧 `clear(node)` 之后
        整块重画，思考块即使被复用也要每 16ms 摘下再插回——滚动条拖动因此中断。
        """
        body = slice_function(self.js, "renderLive")
        self.assertIn("if (!node.__live)", body, "槽位只初始化一次")
        self.assertIn("refill(node.__head, liveHead())", body)
        self.assertIn("paintLiveBody(node)", body)
        self.assertNotIn("refill(node.__body", body,
                         "正文不能再整块重填：那是每帧重建整篇回答（t88）")
        self.assertIn("paintLiveReasoning(node)", body)
        self.assertIn("while (node.__body.nextSibling)", body,
                      "尾巴重建必须排在 body 之后，不能把思考块也一起删掉")
        self.assertEqual(body.count("clear(node)"), 1,
                         "clear 只允许出现在槽位初始化那一次")

    def test_the_body_repaints_only_its_tail(self):
        """正文也只重画尾巴（t88）。

        思考块留下来之后，正文仍是每帧 `refill(node.__body, liveBody())`——整篇回答
        （每一段、每个代码块、每张表格）重新解析重建。长回答里这等于每 16ms 抖一次
        整页正文：选中的文字、刚滚到的位置都可能被换掉，而真正在变的只有最后一段。
        现在只有"空行之后"那一段逐帧重画，之前定型的块画一次就不动了。
        """
        live = slice_function(self.js, "renderLive")
        self.assertIn("paintLiveBody(node)", live, "正文要走增量重画")
        self.assertNotIn("refill(node.__body", live,
                         "正文不能再整块重填（t88）")
        body = slice_function(self.js, "paintLiveBody")
        self.assertIn("stablePrefix(text)", body, "切点必须从 stablePrefix 来")
        self.assertIn("bodyMode(live, parts)", body, "先判这一次该不该走增量")
        self.assertIn("state.prefix.forEach((item) => item.remove())", body,
                      "正文被改写时要把定型的块一起收回")
        self.assertIn("state.tail.forEach((item) => item.remove())", body)
        self.assertEqual(body.count("refill(body, liveBody())"), 1,
                         "整块重画只留给占位/错误那几种一次性状态")
        self.assertIn("const anchor = state.tail.length ? state.tail[0] : null;", body,
                      "新定型的块插在尾巴之前，不能插到最前面")
        cut = slice_function(self.js, "stablePrefix")
        self.assertIn("fence = !fence", cut, "围栏里的空行不是切点")
        self.assertIn("cut = offset", cut, "切点落在空行之后")

    def test_the_answer_keeps_the_block_open(self):
        """一轮结束时，落库消息要接住用户的展开状态。

        不接力的话：思考块开着、答案一出现，`finish()` 把实时气泡换成落库消息，
        块就折回去——恰好是「想看结论怎么来的」那一刻最需要的。
        """
        finish = slice_function(self.js, "finish")
        self.assertIn("carryReasoningOpen = true", finish)
        self.assertLess(finish.index("carryReasoningOpen = true"),
                        finish.index("view.live = null"),
                        "要在把这一轮清掉之前判断，否则状态已经丢了")
        self.assertIn("carryReasoningOpen: false", self.js,
                      "这个标记得先在 view 上声明，不然第一轮是 undefined")
        log = slice_function(self.js, "renderLog")
        # 标记必须在**早退之前**消费掉：空态与"仍在生成"两个分支都不渲染完整记录，
        # 留着它会在切会话之后把别人的思考块打开。
        self.assertIn("const carryReasoning = view.carryReasoningOpen", log)
        self.assertIn("view.carryReasoningOpen = false", log)
        self.assertLess(log.index("const carryReasoning"),
                        log.index("if (!view.messages.length)"),
                        "标记要在空态早退之前消费，否则会漏到下一次重画")
        # 两重限定：最后一条消息得带思考（不然打开的是更早那一轮），并且只开最后一块。
        self.assertIn("lastMessage.reasoning", log)
        self.assertIn("blocks[blocks.length - 1].open = true", log)

    def test_the_stored_message_stays_collapsed_by_default(self):
        """落库消息仍然默认折叠：思考是草稿，不该把正文挤下去。"""
        self.assertIn("reasoningBlock(message.reasoning, false)", self.js,
                      "落库消息只画一次，展开状态由 DOM 自己管，默认折叠")


if __name__ == "__main__":
    unittest.main()
