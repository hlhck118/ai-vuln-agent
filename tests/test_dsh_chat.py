# -*- coding: utf-8 -*-
"""对话循环接上 DSH 运行时通道之后的行为（**完全离线**）。

这里只用替身通道，不发任何模型请求。要钉住的是四件事，它们的共同点是"界面看不出来"：

1. **开关是关的就必须走原生循环**——接入一条新通道最怕的副作用是它悄悄接管了一切；
2. **切过去之后，平台那一套事件契约必须照旧**（`delta`/`reasoning`/`tool`/`persist`/`done`）
   ——`run_turn` 与 SSE 都读同一份，少一种事件的表现是"某个入口上什么都不显示"；
3. **失败与中断不能被说成"答完了"**：DSH 报 aborted 时平台必须报 error，
   客户端断开时必须带上 `interrupted`；
4. **系统提示只种一次**：每轮重发会让平台口径在 DSH 那边越堆越多，还会与它自己的
   人设打架；而一次都不种，模型就不知道本平台的动作边界。
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import chat, dsh_runtime, server, workspace          # noqa: E402
from tests import support                                          # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")

PROFILE = {"name": "假模型", "provider": "deepseek", "model": "deepseek-chat",
           "apiKey": "k", "contextWindow": 128000}
BRIDGE = os.path.join("x", "patches", "pentest-bridge.patch.yml")


class _FakeChannel:
    """替身通道：喂**既定事件**、记下平台发出去的话。"""

    def __init__(self, events=(), summary=None, bridge=BRIDGE):
        self.events = list(events)
        self.summary = dict(summary if summary is not None else {
            "text": "就绪", "reasoning": "", "usage": {"totalTokens": 7},
            "turn": {"reason": "completed"}, "ok": True, "tools": ["a"],
            "config": {}, "unknown": {}, "expected": {}, "events": []})
        self.bridge_patch = bridge
        self.prompts = []
        self.sessions = []
        self.opened = 0
        self.open_kwargs = []

    def open(self, **kwargs):
        self.opened += 1
        self.open_kwargs.append(dict(kwargs))
        return {"serverInfo": {"name": "fake-runtime"}}

    def session_for(self, chat_session_id):
        if chat_session_id in self.sessions:
            return {"id": "session-fake", "fresh": False, "turn": len(self.prompts)}
        self.sessions.append(chat_session_id)
        return {"id": "session-fake", "fresh": True, "turn": 0}

    def prompt(self, session_id, text):
        self.prompts.append((session_id, text))
        return {"messageId": "m1"}

    def iter_turn(self, **kwargs):
        for event in self.events:
            yield dict(event)
        yield dict(self.summary, type="summary")


def _delta(text):
    return {"type": "delta", "text": text}


def _tool_row(name="mcp__platform__list_engagements", text="ok"):
    return {"type": "tool", "phase": "result", "name": name, "ok": True,
            "okKnown": True, "text": text}


class DshChannelTurnTests(unittest.TestCase):
    def setUp(self):
        self.dir, self.store = support.temporary_store("dsh-channel.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    # ------------------------------------------------------------ 工具
    def _drain(self, messages, channel=None, enabled=None, session_id="s-1",
               **kwargs):
        """跑一轮并把事件收齐。

        `enabled` 为 None 时跟随"给没给替身通道"；传 True 但 `channel=None`
        用来测"通道打开了、但起不来"那条路（此时由调用方自己 patch `channel`）。
        """
        if enabled is None:
            enabled = channel is not None
        patches = [mock.patch.object(dsh_runtime, "close_channels", return_value=0)]
        if channel is not None:
            patches.append(mock.patch.object(dsh_runtime, "channel",
                                             return_value=channel))
        self.plane.store.set_setting(dsh_runtime.SETTING_RUNTIME,
                                     "sdk" if enabled else "off")
        for patcher in patches:
            patcher.start()
        try:
            return list(chat.run_turn_stream(
                self.plane, PROFILE, "k", messages,
                session={"sessionId": session_id, "userId": "u"},
                policy_id="full", role="admin", **kwargs))
        finally:
            for patcher in reversed(patches):
                patcher.stop()

    def _builtin_model(self):
        """原生循环的模型调用替身：一段文本、没有工具调用。

        `_call_model` 的契约是生成器 + `return (payload, error)`（它 docstring 里
        写明了为什么用 `return` 而不是再 yield 一个结果事件）。
        """
        def fake(profile, api_key, working, tools, prompt, insecure, stream):
            yield {"type": "delta", "text": "原生回答"}
            return ({"text": "原生回答", "toolCalls": [], "reasoning": "",
                     "usage": {"prompt_tokens": 3}, "finishReason": "stop"}, None)
        return fake

    # ------------------------------------------------------------ 用例
    def test_the_switch_being_off_keeps_the_native_loop(self):
        """接上一条新通道最怕的副作用是它悄悄接管一切。"""
        with mock.patch.object(chat, "_call_model", self._builtin_model()):
            events = self._drain([{"role": "user", "text": "在吗"}])
        kinds = [e["type"] for e in events]
        self.assertEqual("builtin", events[0]["runtime"])
        self.assertIn("persist", kinds)
        self.assertIn("done", kinds)
        self.assertNotIn("notice", kinds)      # 没有那条"本轮由 DSH 执行"的说明
        self.assertEqual("原生回答", events[-1]["text"])

    def test_enabling_the_channel_routes_the_turn_to_dsh(self):
        chan = _FakeChannel(events=[_delta("用"), _delta("户"),
                                    {"type": "reasoning", "text": "想"},
                                    _tool_row()])
        bomb = mock.Mock(side_effect=AssertionError("原生循环不该被调用"))
        with mock.patch.object(chat, "_call_model", bomb):
            events = self._drain([{"role": "user", "text": "查一下资产"}], channel=chan)
        self.assertEqual(1, chan.opened)
        self.assertEqual([], bomb.call_args_list)
        self.assertEqual("dsh", events[0]["runtime"])
        kinds = [e["type"] for e in events]
        for expected in ("delta", "reasoning", "tool", "persist", "done"):
            self.assertIn(expected, kinds)
        notice = [e for e in events if e["type"] == "notice"
                  and e.get("kind") == "runtime"]
        self.assertEqual(1, len(notice))
        self.assertIn("不生效", notice[0]["message"])
        persisted = [e["message"] for e in events if e["type"] == "persist"][0]
        self.assertEqual("用户", persisted["text"])
        self.assertEqual("想", persisted["reasoning"])
        self.assertEqual(1, len(persisted["trace"]))
        self.assertEqual("mcp__platform__list_engagements",
                         persisted["trace"][0]["tool"])
        done = events[-1]
        self.assertEqual("completed", done["finishStatus"])
        self.assertEqual({"totalTokens": 7}, done["usage"])
        # DSH 不报它的循环步数：如实报 0，而不是编一个数
        self.assertEqual(0, done["steps"])

    def test_the_runtime_starts_in_this_sessions_workspace(self):
        """DSH 自带 shell 的落点跟着进程 cwd 走，而它必须是本会话的工作区。

        回归的是项目根目录里反复冒出来的一次性脚本（`probe_*.py`、
        `iksys*.py`）：它们不是 `run_command` 写的（那条路早就按会话分目录了），
        而是模型用 DSH 自带 shell 写的，落点正是 `open(cwd=...)` 这一处。
        """
        chan = _FakeChannel(events=[_delta("好")])
        self._drain([{"role": "user", "text": "查一下"}], channel=chan,
                    session_id="s-1")
        self._drain([{"role": "user", "text": "再查一下"}], channel=chan,
                    session_id="s-2")
        self.assertEqual(2, len(chan.open_kwargs))
        first, second = [kw.get("cwd") or "" for kw in chan.open_kwargs]
        self.assertTrue(first and second,
                        "open() 没拿到 cwd：%r" % (chan.open_kwargs,))
        self.assertTrue(os.path.isdir(first), "工作区没被建出来：%r" % (first,))
        want = workspace.session_dir(self.plane, "s-1")
        self.assertEqual(os.path.normcase(want), os.path.normcase(first))
        self.assertEqual(
            os.path.normcase(workspace.root(self.plane)),
            os.path.normcase(os.path.dirname(os.path.dirname(first))),
            "运行时的 cwd 不在工作区根里：%r" % (first,))
        self.assertNotEqual(os.path.normcase(first), os.path.normcase(second),
                            "两个会话共用了同一个 cwd")

    def test_the_platform_prompt_is_seeded_exactly_once(self):
        chan = _FakeChannel(events=[_delta("好")])
        self._drain([{"role": "user", "text": "第一问"}], channel=chan)
        self._drain([{"role": "user", "text": "第一问"},
                     {"role": "assistant", "text": "好"},
                     {"role": "user", "text": "第二问"}], channel=chan)
        first, second = chan.prompts[0][1], chan.prompts[1][1]
        self.assertIn("工作口径", first)
        self.assertIn("第一问", first)
        self.assertEqual("第二问", second)

    def test_only_the_last_user_message_is_sent(self):
        """DSH 自己保存上下文：整段历史重放会让同一条对话在两边各存一份。"""
        chan = _FakeChannel(events=[_delta("好")])
        self._drain([{"role": "user", "text": "旧问题"},
                     {"role": "assistant", "text": "旧回答"},
                     {"role": "user", "text": "新问题"}], channel=chan)
        self.assertIn("新问题", chan.prompts[0][1])
        self.assertNotIn("旧问题", chan.prompts[0][1])

    def test_a_missing_runtime_is_a_clean_error(self):
        with mock.patch.object(dsh_runtime, "channel",
                               side_effect=dsh_runtime.RuntimeUnavailable("补丁不在")):
            events = self._drain([{"role": "user", "text": "在吗"}], enabled=True)
        kinds = [e["type"] for e in events]
        self.assertIn("error", kinds)
        self.assertNotIn("done", kinds)
        self.assertNotIn("persist", kinds)
        self.assertIn("补丁不在", events[-1]["error"])

    def test_a_failed_turn_is_not_reported_as_done(self):
        chan = _FakeChannel(events=[_delta("半句")],
                            summary={"text": "半句", "reasoning": "", "usage": {},
                                     "turn": {"reason": "aborted"}, "ok": False,
                                     "tools": [], "config": {}, "unknown": {},
                                     "expected": {}, "events": []})
        events = self._drain([{"role": "user", "text": "在吗"}], channel=chan)
        kinds = [e["type"] for e in events]
        self.assertIn("error", kinds)
        self.assertNotIn("done", kinds)
        self.assertIn("aborted", events[-1]["error"])
        self.assertEqual("半句", events[-1]["text"])   # 已经流出去的部分要带上

    def test_interrupt_stops_forwarding_but_keeps_the_conclusion(self):
        closed = {"now": False}
        chan = _FakeChannel(events=[_delta("一"), _delta("二")])

        def is_closed():
            return closed["now"]

        original = chan.iter_turn

        def iter_turn(**kwargs):
            first = True
            for event in original(**kwargs):
                if not first:
                    closed["now"] = True     # 第二条之后，客户端"断开"
                first = False
                yield event

        chan.iter_turn = iter_turn
        events = self._drain([{"role": "user", "text": "长回答"}], channel=chan,
                             is_closed=is_closed)
        kinds = [e["type"] for e in events]
        self.assertIn("notice", kinds)
        self.assertIn("done", kinds)
        done = events[-1]
        self.assertTrue(done["interrupted"])
        self.assertEqual("interrupted", done["stopReason"])
        # 结论要落库：客户端走了不代表这一轮白跑
        self.assertTrue([e for e in events if e["type"] == "persist"])

    def test_unknown_events_are_surfaced(self):
        chan = _FakeChannel(events=[_delta("好")],
                            summary={"text": "好", "reasoning": "", "usage": {},
                                     "turn": {"reason": "completed"}, "ok": True,
                                     "tools": [], "config": {},
                                     "unknown": {"brand/new": 2}, "expected": {},
                                     "events": []})
        events = self._drain([{"role": "user", "text": "在吗"}], channel=chan)
        notice = [e for e in events if e.get("kind") == "unknown-events"]
        self.assertEqual(1, len(notice))
        self.assertIn("brand/new", notice[0]["message"])

    def test_a_turn_without_a_bridge_says_so(self):
        """运行时补丁没写成时要说出来 —— 不然"模型看不到平台工具"会变成一个
        没有解释的空对话。"""
        chan = _FakeChannel(events=[_delta("好")], bridge="")
        events = self._drain([{"role": "user", "text": "在吗"}], channel=chan)
        notice = [e for e in events if e.get("kind") == "tools-patch"]
        self.assertEqual(1, len(notice))
        self.assertIn("平台工具没挂上", notice[0]["message"])

    def test_no_user_message_is_a_clean_error(self):
        events = self._drain([{"role": "assistant", "text": "我在"}],
                             channel=_FakeChannel())
        self.assertEqual("error", events[-1]["type"])
        self.assertIn("没有可发送的用户消息", events[-1]["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
