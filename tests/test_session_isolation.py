# -*- coding: utf-8 -*-
"""会话之间不许串味：一轮的归属、切换时的接回、切走后仍在跑。

使用者报的三件事（原话「每个会话框不要冲突，切换会话不影响另外一个会话的后台进行，
切换会话显示当前会话内容」）：

  1. 切到别的对话去看记录，原来那一轮**仍在跑**，切回来能接回它；
  2. 两条会话的实时气泡 / 审查卡 / 落库消息**不串味**；
  3. 切回来看到的是目标会话**自己的**内容（记录、配置、队列、审查单）。

它们其实是同一条缺陷的三个面：`view.live` / `view.abort` / `view.sending` 曾经是
**单例**——不管屏上是谁，所有流都往同一份 `live` 里写。于是后台会话的 `delta` 会
淌进前台的气泡；后台那轮的 `start` 帧会改写 `view.sessionId`（界面被拽走）；后台
那轮收尾时 `finish()` 拿的是"此刻在看的那条"的 id，把别人的视图重画一遍。

现在的结构：`view.runs` 以 `sessionId` 为键持有每一轮（`{ sessionId, live,
controller }`），而 `view.live` / `view.abort` / `view.sending` 只是"**屏上那一轮**"
的投影（`adoptRun` 切回来时挂上、`retractRun` 收尾时摘掉）。凡是会动 `view.*` 的
事件都先问 `onScreenRun(run)`。

与 `test_ui_contract.py` 同一套写法：切源码文本做断言，**不执行**那份 JS。每条断言
都在改动前的代码上会红（下面逐条注明了它原来长什么样）。
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
    """去掉 `//` 与 `/* */` 注释：注释里正会提到这些名字。"""
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


class _Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.raw = _read("web", "chat.js")
        cls.js = _strip_comments(cls.raw)

    def test_starting_a_run_does_not_drop_the_previous_one(self):
        """同一个键上再开一轮，先把旧的摘掉再登记，不许直接覆盖。

        新会话的第一轮在 `start` 帧到达之前键是空串；使用者在那几十毫秒里连点两下
        「新对话」，直接覆盖会把上一轮从表里挤丢——它的流还在跑，却再也切不回去。
        """
        body = _fn_body(self.js, "startRun")
        self.assertIn("retractRun(view.runs[key]);", body,
                      "开新一轮时直接覆盖了同一个键上的旧项：那一轮会从表里丢掉")
        self.assertLess(body.index("retractRun(view.runs[key]);"),
                        body.index("view.runs[key] = run;"),
                        "摘旧项要写在登记新项之前，否则摘的就是自己")


class ApplyEventBelongsToItsRunTestCase(_Base):
    """事件写进**它自己那一轮**；界面的改动才过 `onScreenRun`。"""

    def test_the_event_writer_takes_the_run(self):
        """`applyEvent(frame, run)`，而且 `live` 取自 `run`。

        改动前是 `applyEvent(frame)` + `const live = view.live`：一条后台会话的流也
        往"屏上那一份"里写，于是它的字出现在别人的气泡里。
        """
        self.assertIn("function applyEvent(frame, run)", self.js,
                      "事件写入没有带上它自己那一轮")
        body = _fn_body(self.js, "applyEvent")
        self.assertIn("const live = run ? run.live : view.live;", body,
                      "`live` 不是取自这一轮：两条会话会写进同一份实时视图")

    def test_the_start_frame_does_not_steal_the_session(self):
        """`start` 帧**不许**无条件改写 `view.sessionId`。

        改动前是 `view.sessionId = data.sessionId || view.sessionId;`：后台那一轮
        收到 `start` 就把界面拽到它身上——正是"切换会话显示当前会话内容"的反面。
        """
        body = _fn_body(self.js, "applyEvent")
        self.assertNotIn("view.sessionId = data.sessionId", body,
                      "start 帧又无条件改写会话指针了：后台那一轮会把界面拽走")
        self.assertIn("if (shown) view.sessionId = actual;", body,
                      "会话指针要跟着这一轮换（新会话的 id 在这里才产生），但只在它正挂在屏上时")
        self.assertIn("renameRun(run, actual)", body,
                      "表里的键没跟着换：新会话切走再切回就找不到这一轮")

    def test_the_review_card_and_the_persisted_line_stay_with_their_session(self):
        """审查卡与落库的插话只进**它自己那个会话**的视图。

        两者都是"挂在会话上的东西"：卡片属于发起它的那一轮（`public()` 里带
        `sessionId`），插话属于那条会话的记录。切走了还往屏上塞，就是串味。
        """
        body = _fn_body(self.js, "applyEvent")
        self.assertIn("applyReview(data.review || {})", body, "审查卡那条分支不见了")
        self.assertIn("if (shown || !(data.review && data.review.sessionId))", body,
                      "审查卡没有跟着会话走：切到别的对话之后会冒出别人的单子")
        self.assertIn("if (shown) insertPersisted(data.message, data.seq);", body,
                      "落库的插话没有跟着会话走：会插进别人的记录里")

    def test_only_the_screen_run_repaints(self):
        """后台那一轮不驱动重绘（`renderLive` 画的是 `view.live`，它没变）。"""
        body = _fn_body(self.js, "applyEvent")
        self.assertIn("if (shown) scheduleLiveRender();", body,
                      "后台会话每来一帧都重绘一次前台窗口：白跑，还会抢滚动位置")


class FinishClosesItsOwnRunTestCase(_Base):
    """收尾收的是**哪一轮**，不是"此刻在看哪一条对话"。"""

    def test_finish_takes_the_run_it_is_closing(self):
        """`finish(run)`，会话 id 从 `run` 上取。

        改动前是 `async function finish() { const sessionId = view.sessionId;`：
        切到别的对话去看记录之后，后台那一轮跑完时按"屏上那条"收尾——把别人的视图
        重画一遍，还把正看着的那一轮气泡抹掉。
        """
        self.assertIn("async function finish(run)", self.js,
                      "收尾没有带上它要收的那一轮")
        body = _fn_body(self.js, "finish")
        self.assertIn("const sessionId = (run && run.sessionId) || view.sessionId;", body,
                      "会话 id 仍取自 `view.sessionId`：后台那一轮会收掉别人的视图")
        self.assertIn("const onScreen = Boolean(mine && view.live === mine);", body,
                      "没有判这一轮此刻挂没挂在屏上")

    def test_closing_a_background_run_leaves_the_window_alone(self):
        """摘表与清投影必须分开：只有挂在屏上的那一轮才动界面。

        两处 `if (onScreen)` 缺一不可——第一处清 `view.live` / `view.abort` /
        `sending`，第二处才 `openSession` 重画。少了判据，后台那一轮收尾会把使用者
        正在看的窗口按它的结果重画一遍。
        """
        body = _fn_body(self.js, "finish")
        self.assertIn("retractRun(run);", body, "收尾时没把它从表里摘掉")
        self.assertEqual(2, body.count("if (onScreen) {"),
                         "`onScreen` 的两处判据不全：一处管清投影，一处管重画")
        self.assertIn("if (sessionId) await openSession(sessionId);", body,
                      "收尾后没按服务端落库的那份重画（挂屏上时）")

    def test_the_goal_switch_and_the_auto_continue_counter_stay_local(self):
        """待办的切换与自动继续的计数都只属于屏上那一轮。

        后台那一轮收尾时去补一次「换目标」的切换，会把使用者正看的窗口换走；
        它去清自动继续的计数，则是替别人做了决定。
        """
        body = _fn_body(self.js, "finish")
        self.assertIn("const pending = onScreen ? view.pendingGoalSwitch : '';", body,
                      "待办没有跟着挂在屏上这个判据走：后台那一轮会把人换走")
        self.assertIn("if (onScreen && !auto.failed)", body,
                      "自动继续的计数被后台那一轮清掉了")


class SwitchingSessionsTestCase(_Base):
    """切走 / 切回：摘投影、挂投影，谁也不掐谁。"""

    def test_opening_a_session_restores_its_run(self):
        """`openSession` 先把那一轮挂回屏上，再走后面的加载。

        改动前没有任何"接回"这一步：`view.live` 在发送时才建，切回来就是一个空
        窗口，等服务端把已落库的那几条给你——正在写的那半句不在里面。
        """
        body = _fn_body(self.js, "openSession")
        self.assertIn("adoptRun(sessionId);", body,
                      "切回会话时没把它那一轮挂回屏上")

    def test_a_new_draft_parks_the_running_turn_instead_of_killing_it(self):
        """「新对话」只摘投影，**不 abort**。

        改动前 `newSession` 根本没碰 `view.live` / `view.abort`：于是那颗刹车还指着
        上一个会话的流（在新会话里点停止会打断别人），而气泡会被画进新会话的窗口。
        """
        body = _fn_body(self.js, "newSession")
        self.assertIn("view.live = null;", body, "新对话没把实时气泡摘下来")
        self.assertIn("view.abort = null;", body, "新对话没把控制器摘下来")
        self.assertNotIn(".abort(", body,
                      "新对话把上一轮掐了：切走不该影响后台进行的那一轮")

    def test_attach_skips_a_session_that_already_has_a_run(self):
        """同一个会话已经有流在跑，就不再 attach 第二条。

        改动前判的是 `view.sending`（全局忙碌）：切到别的对话时它可能是真的，于是
        切回来接不上；也可能是假的，于是同一个会话挂了两条流、两遍字。
        """
        body = _fn_body(self.js, "attachTurn")
        self.assertIn("runOf(sessionId)", body, "没有按会话查是不是已经有一轮在跑")
        self.assertIn("const turn = startRun(sessionId);", body,
                      "接回来的那一轮没有登记进表：再切走一次就丢了")
        self.assertIn("await finish(turn);", body, "收尾没带上这一轮")


class StreamCarriesItsOwnRunTestCase(_Base):
    """流与看门狗都认**自己那一条**，不认"屏上那一条"。"""

    def test_the_watchdog_aborts_its_own_controller(self):
        """空闲看门狗掐的是它自己的 `controller`，不是 `view.abort`。

        改动前是 `if (view.abort) view.abort.abort('idle')`：切到别的对话之后
        `view.abort` 指向别人——后台那一条超时就会把使用者**正看着**的那一轮掐死，
        而真正卡住的那一条还在原地转。
        """
        body = _fn_body(self.js, "idleWatchdog")
        self.assertIn("function idleWatchdog(controller)", self.js,
                      "看门狗没拿它自己那一条流的控制器")
        self.assertIn("if (controller) controller.abort('idle');", body,
                      "看门狗掐的不是自己这一条：会掐到别人身上")
        self.assertNotIn("view.abort.abort('idle')", body,
                      "看门狗又按 `view.abort` 掐了：那是屏上那一轮，不是这一条")

    def test_the_reader_writes_into_the_run_it_was_given(self):
        """`consumeStream(response, controller, run)`：读完看的是 `run.live`。

        改动前是 `consumeStream(response, signal)` + 尾部 `const live = view.live;`：
        收尾判定（"没有收到结束帧"、"没有产出正文"）会拿屏上那一轮去判，于是后台
        那一轮跑完可能被判成"没产出"，而前台这一轮被冤枉。
        """
        self.assertIn("async function consumeStream(response, controller, run)", self.js,
                      "读流没有带上它自己那一轮")
        body = _fn_body(self.js, "consumeStream")
        self.assertIn("const live = run ? run.live : view.live;", body,
                      "读流的尾部判定仍取自 `view.live`")
        self.assertIn("const watchdog = idleWatchdog(controller);", body,
                      "看门狗拿的不是这一条流的控制器")
        self.assertIn("applyEvent(frame, run)", body,
                      "事件没有带上这一轮")

    def test_auto_continue_only_fires_for_the_screen_run(self):
        """「继续」只替**屏上那一轮**发：它用的是 `view.sessionId`。

        改动前两处 `scheduleAutoContinue(live.error)` 无条件执行：后台会话失败一次，
        前台会话里就会冒出一句"继续"——发给另一个会话。
        """
        body = _fn_body(self.js, "consumeStream")
        self.assertEqual(2, body.count("if (onScreenRun(run)) scheduleAutoContinue(live.error);"),
                         "两处续跑判据不全：没收到结束帧 / 没有产出正文，都要判")
        send = _fn_body(self.js, "sendMessage")
        self.assertIn("if (onScreenRun(run)) scheduleAutoContinue(describeError(error));", send,
                      "发送失败那条路仍无条件续跑：切走之后会往别人的会话里发「继续」")


class SigningOutStopsEveryRunTestCase(_Base):
    """退出时每一条流都要断——它们都在往这个已经没有会话的页面里写。"""

    def test_signing_out_aborts_the_whole_table(self):
        """`onSignedOut` 遍历 `view.runs` 逐个 abort 并清表。

        改动前只 abort 了 `view.abort` 这一个：后台那几条继续读，读到的字写进一个
        已经签出的页面（`auto` 甚至可能对着它发"继续"）。
        """
        body = _fn_body(self.js, "onSignedOut")
        self.assertIn("Object.keys(view.runs || {}).forEach", body,
                      "退出时只断了一条流")
        self.assertIn("one.controller.abort('signout')", body,
                      "退出时没有把每一条流都断掉")
        self.assertIn("view.runs = {};", body, "退出后没清表：残留的项会被切回来命中")




