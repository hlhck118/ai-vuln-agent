# -*- coding: utf-8 -*-
"""工具调用配对——写坏的入口、修回来的规则、"修了要说"。

线上真事：会话 `C-63f5af86` 从某一轮起**每一轮**都回 HTTP 400，两条报错交替出现：

    Messages with role 'tool' must be a response to a preceding message with 'tool_calls'
    An assistant message with 'tool_calls' must be followed by tool messages
    responding to each 'tool_call_id'. (insufficient tool messages following tool_calls)

成因是库里的历史断了配对：`run_command` 阻塞着等人工审批的时候使用者又发了几轮，
新一轮把那半截配对带进了请求；300 秒后真正的工具结果落地时前面已经隔着六条消息。
**坏处在于它留在库里**：此后每一轮重建历史都带着它，每一轮都被拒，而报错看起来像
模型或网关的问题。

三条防线，本文各钉一组：

1. **修**（`toolpair.repair`）——真事故序列修完必须是合法请求；
2. **挡**（`server._refuse_while_awaiting_review`）——被拒的那一轮**一条消息都不写**
   （事故里每次重试都往库里克两条，正是它把配对彻底写断的）；
3. **说**（SSE 的 `notice`）——模型照着一两条**合成的**"没有结果"继续干活时，
   使用者必须知道，否则他会以为那个工具真的跑过。
"""
import dis
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import approval, providers, server, toolpair              # noqa: E402
from tests import support                                             # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


def violations(messages):
    """OpenAI 兼容协议那两条规则的可执行翻译。返回违规列表（空 = 合法）。

    这是**测试自己的**判据：产品代码不按"规则"走，它按"补齐欠账"走（见
    `toolpair.repair`）。两者独立写出来才有意义——直接复用产品代码里的判断，
    等于用实现证明实现。
    """
    bad = []
    open_ids = []
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            if open_ids:
                bad.append(("assistant 打断未回答的 tool_calls", index, list(open_ids)))
            open_ids = [c.get("id") or providers.call_id(i)
                        for i, c in enumerate(message.get("toolCalls") or [])]
        elif role == "tool":
            if not open_ids:
                bad.append(("orphan tool（前面没有紧邻的 tool_calls）", index,
                            message.get("toolCallId")))
            elif (message.get("toolCallId") or "") in open_ids:
                open_ids.remove(message["toolCallId"])
            else:
                bad.append(("tool 的 id 对不上欠账", index,
                            (message.get("toolCallId"), list(open_ids))))
        else:
            if open_ids:
                bad.append(("非 tool 消息打断未回答的 tool_calls", index, list(open_ids)))
            open_ids = []
    if open_ids:
        bad.append(("结尾仍然欠着结果", len(messages), list(open_ids)))
    return bad


def accident():
    """会话 `C-63f5af86` 上 #117..#125 的真实形状，逐字照抄。"""
    return [
        {"role": "assistant", "text": "",
         "toolCalls": [{"id": "call_00_x2I7", "name": "run_command",
                        "arguments": {"command": "curl -s http://10.10.10.250/"}}]},
        {"role": "user", "text": "工具列表"},
        {"role": "assistant", "text": ""},          # 这一轮的请求被拒，落库成空消息
        {"role": "user", "text": "工具列表"},
        {"role": "assistant", "text": ""},
        {"role": "user", "text": "工具列表"},
        {"role": "assistant", "text": ""},
        {"role": "tool", "toolCallId": "call_00_x2I7", "toolName": "run_command",
         "text": json.dumps({"ok": False, "reviewId": "R-268d0f29",
                             "summary": "审查单已过期（300 秒内没有人应答），"
                                        "命令没有执行"},
                            ensure_ascii=False)},
        {"role": "assistant", "text": "审核单过期了。",
         "toolCalls": [{"id": "call_00_Y8Vy", "name": "run_tool", "arguments": {}}]},
        {"role": "tool", "toolCallId": "call_00_Y8Vy", "toolName": "run_tool",
         "text": "{\"ok\": true}"},
    ]


# ------------------------------------------------------- 判据：读字节码，不读源文件

def _code_objects(fn):
    """函数对象里的全部代码对象（含嵌套的 def / 生成器 / lambda）。"""
    pending = [fn.__code__]
    seen = []
    while pending:
        code = pending.pop()
        seen.append(code)
        for item in code.co_consts:
            if isinstance(item, type(code)):
                pending.append(item)
    return seen


def attribute_reads(fn, names):
    """函数体里对 names 中任一属性的**读取次数**。

    为什么不用 `inspect.getsource`：它按函数对象携带的**行号**去磁盘上重新读文件，
    全量套件里前面任何一个用例在同一个进程里改过/写过源文件、或让 linecache 记住
    了旧内容，这里就会读到别人的正文——`Ran 2914 tests` 那一次的两条失败正是这么
    来的（单跑必过、全套必挂）。字节码在 import 时刻就固定了，不会随磁盘变化。
    """
    wanted = set(names)
    total = 0
    for code in _code_objects(fn):
        for instruction in dis.get_instructions(code):
            if instruction.opname in ("LOAD_ATTR", "LOAD_METHOD", "LOAD_GLOBAL"):
                if instruction.argval in wanted:
                    total += 1
    return total


def string_literals(fn):
    """函数体里出现过的**字符串常量**（含字典字面量的键元组），展平成一个列表。

    只做"数一数"，不判位置：这条判据问的是"两条返回都写了没有"，位置由下面的
    返回值断言各不相同地保证。
    """
    found = []

    def collect(value):
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, tuple):
            for item in value:
                collect(item)

    for code in _code_objects(fn):
        for item in code.co_consts:
            if not isinstance(item, type(code)):
                collect(item)
    return found


# ---------------------------------------------------------------- 一、修

class RepairRulesTestCase(unittest.TestCase):

    def test_the_accident_sequence_is_broken_to_begin_with(self):
        """先把"它确实是坏的"钉住。不钉的话，下面那条"修好了"可能本来就成立。"""
        bad = violations(accident())
        self.assertEqual(2, len(bad), bad)
        self.assertIn("打断", bad[0][0])
        self.assertIn("orphan", bad[1][0])

    def test_the_accident_sequence_becomes_a_legal_request(self):
        fixed, notes = toolpair.repair(accident())
        self.assertEqual([], violations(fixed))
        self.assertEqual(["missing", "orphan"], [n["kind"] for n in notes])

    def test_the_synthetic_result_sits_directly_after_its_call(self):
        """补的那条必须**紧挨**在调用后面。

        补到别处（比如追加到末尾）满足不了协议：规则要的是相邻。
        """
        fixed, _notes = toolpair.repair(accident())
        self.assertEqual("assistant", fixed[0]["role"])
        self.assertTrue(fixed[0]["toolCalls"])
        self.assertEqual("tool", fixed[1]["role"])
        self.assertEqual("call_00_x2I7", fixed[1]["toolCallId"])
        self.assertEqual("run_command", fixed[1]["toolName"],
                         "合成结果要说清是哪个工具，否则模型不知道该重发什么")

    def test_the_late_real_result_is_kept_as_a_plain_message(self):
        """配对断在另一边时，那条真实结果**不能丢**。

        真事里它就是"审查单已过期"——这一轮唯一的证据。丢了模型只会看到一条
        合成的"没有结果"，然后很可能又发一次同样的 300 秒审批。
        """
        fixed, _notes = toolpair.repair(accident())
        text = json.dumps(fixed, ensure_ascii=False)
        self.assertIn("R-268d0f29", text, "真实结果的内容必须还在")
        kept = [m for m in fixed if m.get("role") == "user"
                and "R-268d0f29" in (m.get("text") or "")]
        self.assertEqual(1, len(kept), "降级成一条 user 消息，而且只降一条")
        self.assertIn("对应的那次调用已经不在上下文里", kept[0]["text"],
                      "要说清这不是使用者说的话")

    def test_a_valid_history_is_untouched(self):
        messages = [{"role": "user", "text": "你好"},
                    {"role": "assistant", "text": "在"},
                    {"role": "user", "text": "再看一眼"}]
        fixed, notes = toolpair.repair(messages)
        self.assertEqual([], notes)
        self.assertEqual(messages, fixed)
        self.assertTrue(all(a is b for a, b in zip(messages, fixed)),
                        "没改动时连消息对象都不该换")
        self.assertEqual([], violations(fixed))

    def test_repair_is_idempotent(self):
        once, _ = toolpair.repair(accident())
        twice, notes = toolpair.repair(once)
        self.assertEqual(once, twice, "修两次必须与修一次一样")
        self.assertEqual([], notes, "第二次不该再报「改了什么」")

    def test_only_the_unanswered_call_is_filled(self):
        messages = [
            {"role": "assistant", "text": "",
             "toolCalls": [{"id": "a", "name": "x", "arguments": {}},
                           {"id": "b", "name": "y", "arguments": {}}]},
            {"role": "tool", "toolCallId": "a", "toolName": "x", "text": "真结果"},
        ]
        fixed, notes = toolpair.repair(messages)
        self.assertEqual(["b"], [n["toolCallId"] for n in notes])
        self.assertEqual("真结果", fixed[1]["text"], "已经答了的那条不许被顶掉")
        self.assertEqual([], violations(fixed))

    def test_the_synthetic_result_is_a_tool_shaped_json(self):
        """合成结果与真实工具结果**同形状**。

        一句自由文本会被模型当成"系统提示"而不是工具返回——本平台的工具结果
        一律是 `{"ok": ..., "summary": ..., "data": ...}`。
        """
        _fixed, notes = toolpair.repair(accident())
        missing = [n for n in notes if n["kind"] == "missing"]
        self.assertEqual(["call_00_x2I7"], [n["toolCallId"] for n in missing])
        payload = json.loads(toolpair.MISSING_RESULT)
        self.assertIs(False, payload["ok"])
        self.assertIn("不要假定它执行过", payload["summary"])
        self.assertIn("重新调用", payload["summary"])
        self.assertTrue(payload["data"]["missing"])

    def test_a_call_without_an_id_pairs_with_what_the_provider_renders(self):
        """没有 id 的调用（Ollama 常见）补出来的 id 必须与服务端渲染的一致。

        不一致的话，我们合成的结果自己又成了孤儿——把一个 400 换成另一个 400。
        """
        messages = [
            {"role": "assistant", "text": "",
             "toolCalls": [{"name": "run_tool", "arguments": {}}]},
        ]
        fixed, _notes = toolpair.repair(messages)
        rendered = providers.to_provider_messages("openai_compat", "", fixed)
        self.assertEqual(providers.call_id(0), rendered[0]["tool_calls"][0]["id"])
        self.assertEqual(rendered[0]["tool_calls"][0]["id"],
                         rendered[1]["tool_call_id"])

    def test_a_lone_open_call_absorbs_an_id_less_result(self):
        """结果没带 id、而这一组只欠一条 → 就认它是那一条，并把 id 补上。

        生产中确实会出现（`toolCallId` 写成空串），留空的话渲染出去的
        `tool_call_id` 是空串，等于又造一个孤儿。
        """
        messages = [
            {"role": "assistant", "text": "",
             "toolCalls": [{"id": "only", "name": "x", "arguments": {}}]},
            {"role": "tool", "toolCallId": "", "toolName": "x", "text": "真结果"},
        ]
        fixed, notes = toolpair.repair(messages)
        self.assertEqual([], notes, "认得出就不要报「改了什么」")
        self.assertEqual("only", fixed[1]["toolCallId"])
        self.assertEqual("真结果", fixed[1]["text"])
        self.assertEqual([], violations(fixed))

    def test_two_open_calls_do_not_absorb_an_id_less_result(self):
        """欠着两条却来了一条没 id 的结果：**不猜**。

        猜错的代价是模型照着 A 的结果继续处理 B 的事——而它读起来完全正常。
        宁可降级成一条普通消息（内容还在），也不做这个判断。
        """
        messages = [
            {"role": "assistant", "text": "",
             "toolCalls": [{"id": "a", "name": "x", "arguments": {}},
                           {"id": "b", "name": "y", "arguments": {}}]},
            {"role": "tool", "toolCallId": "", "toolName": "x", "text": "真结果"},
        ]
        fixed, notes = toolpair.repair(messages)
        self.assertIn("orphan", [n["kind"] for n in notes])
        self.assertIn("真结果", json.dumps(fixed, ensure_ascii=False))
        self.assertEqual([], violations(fixed))

    def test_switching_to_another_assistant_closes_the_previous_group(self):
        """连续两条带调用的 assistant，前一组必须先结清。"""
        messages = [
            {"role": "assistant", "text": "",
             "toolCalls": [{"id": "a", "name": "x", "arguments": {}}]},
            {"role": "assistant", "text": "换个活干",
             "toolCalls": [{"id": "b", "name": "y", "arguments": {}}]},
            {"role": "tool", "toolCallId": "b", "toolName": "y", "text": "{}"},
        ]
        fixed, notes = toolpair.repair(messages)
        self.assertEqual(["a"], [n["toolCallId"] for n in notes])
        self.assertEqual([], violations(fixed))

    def test_open_calls_only_reports_a_trailing_break(self):
        """`open_calls` 的语义就是"末尾还欠着"——别指望它报中途的断裂。

        入口闸要的正是"上一轮走完了没有"，那是末尾问题；中途的断裂是 `repair`
        的事。两者混起来会让闸门按一份"有历史遗留问题"的老库把会话永久挡住。
        """
        self.assertEqual(["call_00_x2I7"], toolpair.open_calls(accident()[:1]))
        self.assertEqual([], toolpair.open_calls(accident()),
                         "末尾那一组是答过的，所以不算欠账")
        raw = accident()
        self.assertEqual(["call_00_Y8Vy"], toolpair.open_calls(raw[:-1]),
                         "砍掉最后一条结果，末尾那一组就真的欠着了")

    def test_describe_is_empty_when_nothing_changed(self):
        self.assertEqual("", toolpair.describe([]))

    def test_describe_names_both_kinds(self):
        _fixed, notes = toolpair.repair(accident())
        text = toolpair.describe(notes)
        self.assertIn("还没有结果", text)
        self.assertIn("配不上调用", text)
        self.assertIn("内容没丢", text, "说清没有丢东西，否则使用者会去重新取证")


# ---------------------------------------------------------------- 二、挡

class TurnGuardTestCase(unittest.TestCase):
    """入口闸。

    事故真正的伤害不是那几次 400，是**它们留下的消息**：每一次重试都往库里加一条
    user + 一条失败的 assistant，六条消息把配对彻底隔断。所以这条闸门放在任何写
    之前，被拒的那一轮不许留痕。
    """

    def setUp(self):
        self.dir, self.store = support.temporary_store("toolpair.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)
        self.session = self.store.create_chat_session("配对", "", "", "", "", "")
        self.sid = self.session["id"]

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _open_pair(self):
        """把会话写成"上一轮发起了 run_command、结果还没落库"。"""
        self.store.append_chat_message(self.sid, "user", {"text": "看一眼首页"})
        self.store.append_chat_message(self.sid, "assistant", {
            "text": "", "toolCalls": [{"id": "call_00_x2I7", "name": "run_command",
                                       "arguments": {"command": "curl -s /"}}]})

    def _ctx(self):
        return {"body": {"message": "工具列表", "sessionId": self.sid},
                "params": {}, "user": None}

    def test_a_pending_review_with_an_open_pair_refuses_the_turn(self):
        self._open_pair()
        row = self.store.record_approval(session_id=self.sid, tool="run_command",
                                         tier="always", headline="要跑一条命令",
                                         timeout_s=300)
        # 闸门的判据是"**有人在等**"（`approval.awaited`），不是"库里有一张
        # `pending`"。两者的差别正好是重启：进程一死等待者就没了，而单子会留到
        # 超时——那时还挡人就是在为一张永远不会执行的单子把会话关起来。所以要验
        # "该挡的挡住了"，就得真摆一个等待者。
        with approval.waiting(row["id"]):
            with self.assertRaises(server.ApiError) as caught:
                self.plane._chat_turn_setup(self._ctx())
        self.assertEqual(409, caught.exception.status)
        message = caught.exception.message
        self.assertIn(row["id"], message, "要把单子编号说出来，使用者才知道去批哪一个")
        self.assertIn("run_command", message)
        self.assertIn("停止", message, "要给出路：按「停止」也是一种结束方式")

    def test_a_refused_turn_writes_nothing(self):
        self._open_pair()
        row = self.store.record_approval(session_id=self.sid, tool="run_command",
                                         timeout_s=300)
        with approval.waiting(row["id"]):
            before = [m["seq"] for m in self.store.chat_messages(self.sid)]
            with self.assertRaises(server.ApiError) as caught:
                self.plane._chat_turn_setup(self._ctx())
        # **必须是闸门那个 409**，不能是别的错误码。少了这一句，闸门哪天不再挡了
        # 它也照样是绿的（`assertRaises` 会被"缺少模型"之类的 400 满足），而它要守
        # 的正是"闸门挡在写之前"这件事。
        self.assertEqual(409, caught.exception.status)
        after = [m["seq"] for m in self.store.chat_messages(self.sid)]
        self.assertEqual(before, after,
                         "被拒的那一轮不许往库里加任何一条消息——事故就是这么攒出来的")

    def test_settling_the_review_releases_the_gate(self):
        self._open_pair()
        row = self.store.record_approval(session_id=self.sid, tool="run_command",
                                        timeout_s=300)
        self.store.settle_approval(row["id"], "denied",
                                   note="夹具：人拒了")
        raw = self.store.chat_messages(self.sid)
        self.assertIsNone(self.plane._refuse_while_awaiting_review(self.sid, raw),
                          "单子结清了就该放行")

    def test_an_open_pair_without_a_review_is_never_refused(self):
        """**永不锁死**：末尾欠着结果、但没有审查单时不许挡。

        那种状态是一次死掉的轮次留下的。挡了它，会话就再也发不出消息——比原来
        那个 400 更难查（400 至少每次都说同一句话）。放行时 `toolpair.repair`
        会补一条"还没有结果"，历史照样合法。
        """
        self._open_pair()
        raw = self.store.chat_messages(self.sid)
        self.assertEqual(["call_00_x2I7"], toolpair.open_calls(raw), "前提：确实欠着")
        self.assertIsNone(self.plane._refuse_while_awaiting_review(self.sid, raw))

    def test_a_pending_review_without_an_open_pair_is_not_refused(self):
        """单子还挂着，但这一轮的调用早就落库结清了 → 没有东西需要保护。"""
        self._open_pair()
        self.store.append_chat_message(self.sid, "tool", {
            "toolCallId": "call_00_x2I7", "toolName": "run_command",
            "text": "{\"ok\": false}"})
        self.store.record_approval(session_id=self.sid, tool="run_command",
                                   timeout_s=300)
        raw = self.store.chat_messages(self.sid)
        self.assertEqual([], toolpair.open_calls(raw))
        self.assertIsNone(self.plane._refuse_while_awaiting_review(self.sid, raw))

    def test_the_guard_runs_before_the_history_is_even_read_twice(self):
        """闸门用的历史与真正发出去的历史必须是**同一份**。

        读两次的话，两次之间可能有一条工具结果落库——闸门据以判断的那份与
        实际发出去的那份就成两份了，而"配对断没断"恰好是这次判断的全部依据。

        判据读的是**字节码**（`attribute_reads`），不是 `inspect.getsource`：
        后者按 import 时刻的行号去磁盘重读文件，全量套件里会被前面用例写过的东西
        带歪——`Ran 2914 tests` 那次的两条失败就是它（单跑必过、全套报 1 != 0）。
        """
        reads = attribute_reads(server.ControlPlane._chat_turn_setup,
                                {"chat_messages", "chat_messages_tail"})
        # 读法可能是 `chat_messages` 或 `chat_messages_tail`，数的是**读的次数**。
        self.assertEqual(1, reads,
                         "会话历史只许读一次，并且复用给入口闸与历史重建")


# ---------------------------------------------------------------- 三、说

class HistoryAndNoticeTestCase(unittest.TestCase):
    """整条链路：库里的坏历史 → 重建 → 修 → 合法请求 + 一句提示。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("toolpair2.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)
        self.session = self.store.create_chat_session("配对二", "", "", "", "", "")
        self.sid = self.session["id"]
        # 一个能解析出来的模型配置：`_chat_turn_setup` 要它，但**不会**调用它。
        self.store.save_profile("m-fake", "夹具模型", "openai_compat",
                                "http://127.0.0.1:9/v1", "fake-1", "none", "",
                                None, 0.2, 512, 20, "")
        self.store.activate_profile("m-fake")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _write(self, messages):
        for message in messages:
            self.store.append_chat_message(self.sid, message["role"], message)

    def _ctx(self):
        return {"body": {"message": "继续", "sessionId": self.sid},
                "params": {}, "user": None}

    def _preamble(self):
        """断掉的配对**落在窗口中间**才是有代表性的形状。

        真会话里它前面有 116 条消息；只有 10 条的夹具会让那一对正好落在窗口开头，
        而窗口开头本来就会被 `_history_for_model` 裁掉（那条路是**故意**的：宁可
        少一段历史，也不留残缺）。两种形状都合法，但要测的显然是中间那一种。
        """
        return [{"role": "user", "text": "先看一眼首页"},
                {"role": "assistant", "text": "好"}]

    def test_a_pair_broken_at_the_window_head_is_simply_dropped(self):
        """极端形状：断在窗口开头 → 那次调用被裁掉，结果降级成普通消息。

        合法，而且**不假装**补齐过一次调用——那条调用在我们的视野之外。
        """
        self._write(accident())
        history, notes = toolpair.repair(
            self.plane._history_for_model(self.store.chat_messages(self.sid)))
        self.assertEqual([], violations(history))
        self.assertEqual(["orphan"], [n["kind"] for n in notes])
        self.assertIn("R-268d0f29", json.dumps(history, ensure_ascii=False))

    def test_the_broken_session_now_builds_a_legal_request(self):
        """这条是整件事的目的：坏掉的会话**不用人工改库**就能继续。"""
        self._write(self._preamble() + accident())
        setup = self.plane._chat_turn_setup(self._ctx())
        self.assertEqual([], violations(setup["history"]))
        self.assertTrue(setup["pairNotes"], "修过就要报出「改了什么」")

    def test_the_request_carries_the_missing_result_note(self):
        """合成结果要真的进了请求——不是只在 notes 里。"""
        self._write(self._preamble() + accident())
        setup = self.plane._chat_turn_setup(self._ctx())
        text = json.dumps(setup["history"], ensure_ascii=False)
        self.assertIn("不要假定它执行过", text)
        self.assertIn("R-268d0f29", text, "真实结果也不能因为修复被丢掉")

    def test_a_clean_session_builds_a_clean_request(self):
        self._write([{"role": "user", "text": "你好"},
                     {"role": "assistant", "text": "在"}])
        setup = self.plane._chat_turn_setup(self._ctx())
        self.assertEqual([], setup["pairNotes"])
        self.assertEqual([], violations(setup["history"]))

    def test_the_stream_says_the_context_was_repaired(self):
        """SSE 的 `notice`。

        不说的话，模型会照着一两条**合成的**"没有结果"继续干活，而使用者以为
        那是真的工具返回——结论看起来有依据，依据却是平台编的。
        """
        self._write(self._preamble() + accident())
        response = self.plane.send_chat_stream(self._ctx())
        events = response.events
        try:
            first = next(events)
            second = next(events)
        finally:
            events.close()
        self.assertEqual("start", first[0])
        self.assertEqual("notice", second[0])
        self.assertEqual("toolpair", second[1]["kind"])
        self.assertIn("还没有结果", second[1]["message"])

    def test_a_clean_stream_says_nothing_extra(self):
        self._write([{"role": "user", "text": "你好"}])
        response = self.plane.send_chat_stream(self._ctx())
        events = response.events
        try:
            first = next(events)
            second = next(events)
        finally:
            events.close()
        self.assertEqual("start", first[0])
        self.assertNotEqual("notice", second[0],
                            "没修过就别说话——一句每次都在的提示等于没有提示")

    def test_the_whole_endpoint_response_reports_the_note(self):
        """`/chat` 那条整包入口也要带 `pairNote`（它是脚本与不吃流式的客户端在走）。"""
        literals = string_literals(server.ControlPlane.send_chat)
        # 两处都写成字典字面量的键，所以常量表里是键元组（`("ok", "sessionId", ...,
        # "pairNote", ...)`）。数键元组里出现几次，正好是"几条返回带了它"。
        reported = sum(1 for item in literals if item == "pairNote")
        self.assertEqual(2, reported, "成功与失败两条返回都要带上它")


if __name__ == "__main__":
    unittest.main(verbosity=2)
