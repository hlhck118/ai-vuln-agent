# -*- coding: utf-8 -*-
"""一轮对话的后台泵（`pentestd/turnpump.py`）与它带来的三个接口。

这条功能修的是"刷新页面 = 这一轮被砍断"。所以测试的重心不是"事件到没到"，
而是三件在界面上看不出来的事：

1. **没有观察者时它照样跑完、照样落库。** 断开的是看的人，不是跑的人；
2. **回来之后能整轮重放**，而已经落库的那几条不会在屏幕上出现两遍；
3. **停止是一次显式请求**，不是"连接断了"。

第 3 条特别容易写反：旧实现在断开时就停，于是"刷新"与"停止"共用了一条路。
这里钉住的是两条路必须分开——否则改完看着一切正常，等到有人刷新时才发现
停止按钮其实从来没生效过（或者反过来，刷新真的把活儿杀了）。
"""
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import turnpump                                       # noqa: E402
from tests import support                                           # noqa: E402
from tests.test_chat import has_tool_result                         # noqa: E402
from tests.test_chat_stream import (                                # noqa: E402
    WEB_DIR, ScriptedStreamModel, openai_text_stream, openai_tool_stream)

MODEL_BODY = {"name": "假模型", "provider": "openai_compat",
              "baseUrl": "", "model": "fake-1", "apiKeySource": "none",
              "temperature": 0.2, "maxTokens": 512, "timeoutS": 20}


def read_sse(response):
    """把一条 SSE 响应读成 `(事件名, data)`。"""
    event, data = "", []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line == "":
            if data:
                yield event or "message", json.loads("\n".join(data))
            event, data = "", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)


# ---------------------------------------------------------------- 泵本身

class PumpCoreTestCase(unittest.TestCase):
    """不经过 HTTP：泵的缓冲、取消与回收。"""

    def drain(self, run, since=0, timeout=10.0):
        seen, cursor = [], since
        deadline = time.time() + timeout
        while time.time() < deadline:
            items, gap, last, finished = run.read_from(cursor, 0.2)
            for seq, name, data in items:
                seen.append((seq, name, data))
                cursor = seq
            if finished and cursor >= last:
                return seen, cursor
        self.fail("这一轮在 %.1fs 内没有收尾" % timeout)

    def test_events_are_buffered_and_a_finished_run_stays_readable(self):
        """跑完之后缓冲区还要在：刚断开、还没重连上的客户端靠它补尾巴。"""
        pumps = turnpump.TurnPumps()
        run = pumps.start("s1", lambda one: iter([
            ("start", {"sessionId": "s1"}),
            ("delta", {"text": "你"}),
            ("done", {"ok": True})]))
        seen, cursor = self.drain(run)
        self.assertEqual([name for _seq, name, _data in seen],
                         ["start", "delta", "done"])
        self.assertTrue(run.finished)
        self.assertIs(pumps.get("s1"), run, "跑完的一轮不该立刻消失")
        # 整轮重放：换一个从没看过的人来读，拿到的应当是同一串。
        again, _ = self.drain(run, since=0, timeout=2.0)
        self.assertEqual([name for _seq, name, _data in again],
                         ["start", "delta", "done"])

    def test_a_second_start_on_the_same_session_is_refused(self):
        """两道请求同时进来时，第二道必须被挡在**落用户消息之前**。"""
        pumps = turnpump.TurnPumps()
        gate = threading.Event()
        run = pumps.start("s1", lambda one: iter([("start", {}), ("done", {})])
                          if False else self._blocking(gate))
        with self.assertRaises(turnpump.TurnBusy):
            pumps.start("s1", lambda one: iter([("done", {})]))
        gate.set()
        self.assertTrue(run.wait(5.0))

    @staticmethod
    def _blocking(gate):
        gate.wait(5.0)
        yield "start", {}
        yield "done", {}

    def test_cancel_is_the_only_way_to_stop_a_run(self):
        """没有观察者不是停止的理由；`cancel()` 才是。"""
        pumps = turnpump.TurnPumps()
        seen = []

        def events(run):
            yield "start", {}
            run.cancel_requested.wait(5.0)
            seen.append("cancelled")
            yield "done", {"interrupted": True, "stopReason": "interrupted"}

        run = pumps.start("s1", events)
        deadline = time.time() + 5
        while time.time() < deadline and run.first_seq < 1:
            time.sleep(0.01)
        self.assertTrue(pumps.cancel("s1"))
        self.assertFalse(pumps.cancel("s1"), "重复点停止不该再说一次“才挂上”")
        items, _cursor = self.drain(run)
        self.assertIn("cancelled", seen)
        self.assertEqual(items[-1][1], "done")
        self.assertTrue(items[-1][2].get("interrupted"))

    def test_a_trimmed_buffer_reports_the_gap_instead_of_pretending(self):
        """丢了开头必须说，否则重放方会把“开头没了”当成“这一轮就这么短”。"""
        run = turnpump.TurnRun("s1")
        for index in range(4):
            run._push("delta", {"index": index})
        del run._events[:2]          # 模拟 MAX_EVENTS 把最老的挤掉
        items, gap, last, finished = run.read_from(0, 0.1)
        self.assertTrue(gap, "开头已经被挤掉了却没报 gap")
        self.assertEqual(last, 4)
        self.assertFalse(finished)

    def test_a_finished_run_is_pruned_after_the_keep_window(self):
        old = turnpump.KEEP_SECONDS
        # 用负数而不是 0：`now - finished_at > KEEP_SECONDS` 里两边可能落在同一个
        # 时钟刻度上（浮点差恰好 0.0），而 0 > 0 是假——那会让这条测试偶发通过。
        turnpump.KEEP_SECONDS = -1.0
        self.addCleanup(setattr, turnpump, "KEEP_SECONDS", old)
        pumps = turnpump.TurnPumps()
        first = pumps.start("old", lambda one: iter([("done", {})]))
        self.assertTrue(first.wait(5.0))
        # 下一次查询/登记顺手回收：留一整个 KEEP_SECONDS 是为了补尾巴，不是为了留住历史。
        pumps.start("new", lambda one: iter([("done", {})]))
        self.assertIsNone(pumps.get("old"))


class InboxTestCase(unittest.TestCase):
    """插话收件箱：一轮跑着时补的话怎么进去、怎么被取走、什么时候接不住。"""

    def test_interject_goes_to_the_inbox_and_is_taken_exactly_once(self):
        """取走是**取空**：同一条话不能被两个步各接一次。"""
        run = turnpump.TurnRun("s1")
        self.assertTrue(run.interject("先别扫 10.0.0.0/24"))
        self.assertTrue(run.interject("改看 /etc/hosts"))
        self.assertEqual(run.info()["inbox"], 2, "界面靠这个数显示“已送出、等下一步”")
        self.assertEqual(run.take_inbox(), ["先别扫 10.0.0.0/24", "改看 /etc/hosts"])
        self.assertEqual(run.info()["inbox"], 0)
        self.assertEqual(run.take_inbox(), [])

    def test_an_empty_interject_is_refused(self):
        run = turnpump.TurnRun("s1")
        self.assertFalse(run.interject("   "))
        self.assertEqual(run.take_inbox(), [])

    def test_interject_is_refused_after_the_run_has_finished(self):
        run = turnpump.TurnRun("s1")
        run._finish()
        self.assertFalse(run.interject("再说一句"))
        self.assertEqual(run.take_inbox(), [])

    def test_interject_is_refused_after_stop_is_requested(self):
        """停止在途时收件箱永远不会被取走——收下它等于骗人说“已经送到了”。"""
        run = turnpump.TurnRun("s1")
        run.cancel()
        self.assertFalse(run.interject("停下来之前再补一句"))
        self.assertEqual(run.take_inbox(), [])

    def test_on_finish_runs_after_the_turn_is_finished_and_can_start_the_next(self):
        """自动续跑就挂在这个钩子上：回调里必须能登记下一轮（此时旧的已不活跃）。"""
        pumps = turnpump.TurnPumps()
        seen = []

        def finished(one):
            seen.append((one.session_id, one.finished, one.active))
            pumps.start(one.session_id,
                        lambda second: iter([("done", {"second": True})]))

        first = pumps.start("s1", lambda one: iter([("done", {"first": True})]),
                            on_finish=finished)
        self.assertTrue(first.wait(5.0))
        deadline = time.time() + 5
        second = None
        while time.time() < deadline:
            second = pumps.get("s1")
            if second is not None and second is not first:
                break
            time.sleep(0.01)
        self.assertIsNotNone(second, "收尾钩子没有把下一轮开起来")
        self.assertIsNot(second, first)
        self.assertTrue(second.wait(5.0))
        self.assertEqual(seen, [("s1", True, False)],
                         "钩子要在 _finish() 之后调用（finished=True, active=False）")
        items, _cursor = PumpCoreTestCase().drain(second)
        self.assertEqual(items[-1][1], "done")
        self.assertTrue(items[-1][2].get("second"))


# ------------------------------------------------------------ 三个新接口

class PumpEndpointTestCase(unittest.TestCase):
    """真起控制面：断开、重放、停止、409。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("turnpump-http.db")
        self.models = []
        self.httpd = None

    def tearDown(self):
        for model in self.models:
            model.stop()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.store.close()
        self.dir.cleanup()

    # ---- 脚手架

    def start(self, responder):
        model = ScriptedStreamModel(responder)
        self.models.append(model)
        self.httpd, self.client = support.start(self.store, WEB_DIR)
        body = dict(MODEL_BODY, baseUrl=model.base_url + "/v1")
        status, _payload = self.client.post("/api/v1/models", body)
        if status != 201:
            raise RuntimeError("模型登记失败：%s" % status)
        return model

    def open_stream(self, body=None, path="/api/v1/chat/stream", method="POST"):
        data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
        request = urllib.request.Request(
            self.client.base + path, data=data, method=method,
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream",
                     "X-CSRF-Token": self.client.csrf})
        return self.client.opener.open(request, timeout=30)

    def live(self, session_id):
        status, payload = self.client.get(
            "/api/v1/chat/live?sessionId=" + urllib.parse.quote(session_id))
        self.assertEqual(status, 200, payload)
        return payload

    def wait_active(self, session_id, want=True, timeout=20.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.live(session_id)
            if bool(last.get("active")) is want:
                return last
            time.sleep(0.05)
        self.fail("等了 %.1fs，active 仍然是 %r：%r"
                  % (timeout, last and last.get("active"), last))

    def detail(self, session_id):
        status, payload = self.client.get(
            "/api/v1/chat/sessions/" + urllib.parse.quote(session_id))
        self.assertEqual(status, 200, payload)
        return payload

    @staticmethod
    def session_of(events):
        for name, data in events:
            if name == "start":
                return data["sessionId"]
        raise AssertionError("这一轮没有 start 事件：%r" % (events[:3],))

    def collect(self, response):
        with response:
            return list(read_sse(response))

    # ---- 收尾之后

    def test_a_finished_turn_can_be_replayed_and_then_says_inactive(self):
        """刷新回来时：先问在不在跑（不在），再从缓冲区整轮重放。"""
        def responder(record):
            if has_tool_result(record):
                return openai_text_stream("节点已列出。")
            return openai_tool_stream("list_executors", {})

        self.start(responder)
        events = self.collect(self.open_stream({"message": "有哪些节点"}))
        session_id = self.session_of(events)
        self.assertEqual(events[-1][0], "done")
        self.assertFalse(self.live(session_id)["active"])

        replay = self.collect(self.open_stream(method="GET",
                                               path="/api/v1/chat/stream?sessionId=%s&since=0"
                                               % urllib.parse.quote(session_id)))
        self.assertEqual(self.session_of(replay), session_id)
        self.assertIn("done", [name for name, _data in replay])
        self.assertIn("tool", [name for name, _data in replay])
        # 落库的那些序号要一起报出来：接回来的界面靠它把重放与库里那几条去重。
        self.assertTrue(self.live(session_id)["run"]["persistedSeqs"])

        stored = [one for one in self.detail(session_id)["messages"]
                  if one.get("role") == "assistant" and one.get("text")]
        self.assertTrue(any("节点已列出" in (one.get("text") or "") for one in stored),
                        "这一轮的结论没有落库：%r" % (stored,))

    # ---- 正在跑

    def test_a_second_send_while_running_is_refused_without_storing_it(self):
        gate = threading.Event()

        def responder(record):
            gate.wait(10.0)
            return openai_text_stream("好")

        self.start(responder)
        box = {}

        def run():
            try:
                box["events"] = self.collect(self.open_stream({"message": "第一句"}))
            except Exception as exc:        # noqa: BLE001 —— 由断言去看
                box["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        # 会话行由 `_chat_turn_setup` 在建轮之前就写好，所以只要等它出现，
        # 后面那句“第二次发送被拒”就一定是“会话已在跑”造成的。
        deadline = time.time() + 15
        while time.time() < deadline and not self.store.chat_sessions():
            time.sleep(0.05)
        sessions = self.store.chat_sessions()
        self.assertTrue(sessions, "这一轮没有建出会话")
        session_id = sessions[0]["id"]
        self.wait_active(session_id, True)
        status, payload = self.client.post("/api/v1/chat/stream",
                                           {"message": "第二句", "sessionId": session_id})
        self.assertEqual(status, 409, payload)
        # **被拒的那一句不该留下痕迹**：检查在 `_chat_turn_setup` 之前。
        users = [one for one in self.detail(session_id)["messages"]
                 if one.get("role") == "user"]
        self.assertEqual([one.get("text") for one in users], ["第一句"])

        gate.set()
        worker.join(20)
        self.assertNotIn("error", box, box.get("error"))
        self.assertEqual(box["events"][-1][0], "done")

    def test_a_disconnect_does_not_stop_the_turn(self):
        """**这条是本次改动的核心。** 关掉连接之后，这一轮照样跑完、照样落库。"""
        gate = threading.Event()
        closed = threading.Event()
        seen = []

        def responder(record):
            if has_tool_result(record):
                gate.wait(20.0)
                return openai_text_stream("断线之后也算完了。")
            return openai_tool_stream("list_executors", {})

        self.start(responder)

        def run():
            response = self.open_stream({"message": "断开试试"})
            try:
                for name, data in read_sse(response):
                    seen.append(name)
                    if name == "tool":
                        # 工具已经执行、assistant 消息已经落库；此时把连接关掉。
                        response.close()
                        closed.set()
                        return
            finally:
                try:
                    response.close()
                except Exception:      # noqa: BLE001
                    pass

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(closed.wait(20.0), "没等到工具事件，无法制造“断开”")
        session_id = self.store.chat_sessions()[0]["id"]
        # 连接没了，这一轮仍在跑（模型正卡在 gate 上）。
        self.wait_active(session_id, True)
        gate.set()
        self.wait_active(session_id, False, timeout=30.0)

        stored = self.detail(session_id)["messages"]
        texts = [one.get("text") or "" for one in stored if one.get("role") == "assistant"]
        self.assertTrue(any("断线之后也算完了" in text for text in texts),
                        "断开之后这一轮的结论没有落库：%r" % (texts,))
        self.assertEqual(sum(1 for text in texts if "断线之后也算完了" in text), 1,
                         "同一条结论落了两遍：%r" % (texts,))

    def test_stop_ends_the_turn_before_the_next_model_call(self):
        """停止是显式请求：它在下一次模型调用之前生效，并如实标成中断。"""
        gate = threading.Event()
        calls = []

        def responder(record):
            calls.append(record)
            if len(calls) == 1:
                return openai_tool_stream("list_executors", {})
            if len(calls) == 2:
                gate.wait(20.0)
                return openai_tool_stream("list_executors", {})
            return openai_text_stream("不该走到这里。")

        model = self.start(responder)
        box = {}

        def run():
            try:
                box["events"] = self.collect(self.open_stream({"message": "停一下"}))
            except Exception as exc:        # noqa: BLE001
                box["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        deadline = time.time() + 20
        while time.time() < deadline and len(model.requests) < 2:
            time.sleep(0.05)
        self.assertEqual(len(model.requests), 2, "第二次模型调用没有发出来")
        session_id = self.store.chat_sessions()[0]["id"]
        self.wait_active(session_id, True)
        status, payload = self.client.post("/api/v1/chat/stop",
                                           {"sessionId": session_id})
        self.assertEqual(status, 200, payload)
        gate.set()
        worker.join(30)
        self.assertNotIn("error", box, box.get("error"))
        done = [data for name, data in box["events"] if name == "done"][-1]
        self.assertTrue(done.get("interrupted"), done)
        self.assertEqual(done.get("stopReason"), "interrupted")

    def test_live_is_false_for_a_session_that_never_existed(self):
        self.start(lambda record: openai_text_stream("ok"))
        status, payload = self.client.get("/api/v1/chat/live?sessionId=nope")
        self.assertEqual(status, 404, payload)

    # ---- 插话与排队（t86）

    @staticmethod
    def user_texts(record):
        body = record.get("body") or {}
        return " ".join(str(one.get("content") or one.get("text") or "")
                        for one in body.get("messages") or [])

    def test_interjecting_delivers_into_the_running_turn(self):
        """插话必须落进**正在跑的那一轮**，而不是排到下一轮。"""
        gate = threading.Event()

        def responder(record):
            gate.wait(20.0)
            return openai_text_stream("看到了。")

        self.start(responder)
        box = {}

        def run():
            try:
                box["events"] = self.collect(self.open_stream({"message": "先别动"}))
            except Exception as exc:        # noqa: BLE001 —— 由断言去看
                box["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        deadline = time.time() + 15
        while time.time() < deadline and not self.store.chat_sessions():
            time.sleep(0.05)
        self.assertTrue(self.store.chat_sessions(), "这一轮没有建出会话")
        session_id = self.store.chat_sessions()[0]["id"]
        self.wait_active(session_id, True)

        status, payload = self.client.post("/api/v1/chat/interject",
                                           {"sessionId": session_id,
                                            "message": "补一句：先看 /etc/hosts"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload.get("ok"), payload)
        self.assertEqual(payload.get("mode"), "interject")
        gate.set()
        worker.join(30)
        self.assertNotIn("error", box, box.get("error"))
        self.wait_active(session_id, False, timeout=30.0)

        users = [one for one in self.detail(session_id)["messages"]
                 if one.get("role") == "user"]
        injected = [one for one in users if one.get("injected")]
        self.assertEqual(len(injected), 1, users)
        self.assertIn("先看 /etc/hosts", injected[0].get("text") or "")
        # 它没有被当成"下一轮"：队列里是空的。
        status, payload = self.client.get(
            "/api/v1/chat/queue?sessionId=" + urllib.parse.quote(session_id))
        self.assertEqual(payload.get("items"), [], payload)

    def test_queuing_while_idle_starts_the_turn_right_away(self):
        """排的时候刚好没轮在跑：不能再等一个永远不会来的收尾事件。"""
        self.start(lambda record: openai_text_stream("在。"))
        events = self.collect(self.open_stream({"message": "第一句"}))
        session_id = self.session_of(events)
        self.wait_active(session_id, False, timeout=30.0)

        status, payload = self.client.post("/api/v1/chat/queue",
                                           {"sessionId": session_id,
                                            "message": "排队的一句"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload.get("started"), payload)
        self.wait_active(session_id, False, timeout=30.0)
        users = [one.get("text") for one in self.detail(session_id)["messages"]
                 if one.get("role") == "user"]
        self.assertIn("排队的一句", users)
        status, payload = self.client.get(
            "/api/v1/chat/queue?sessionId=" + urllib.parse.quote(session_id))
        self.assertEqual(payload.get("items"), [],
                         "开跑的那一条还留在队列里，界面上会画两遍")

    def test_stop_does_not_start_the_queued_turn_by_itself(self):
        """点「停止」不等于「上一任务完成」：排着的那条要等一句明确的话。"""
        gate = threading.Event()

        def responder(record):
            if "排队一号" in self.user_texts(record):
                gate.wait(20.0)
            return openai_text_stream("好。")

        self.start(responder)
        events = self.collect(self.open_stream({"message": "开个头"}))
        session_id = self.session_of(events)
        self.wait_active(session_id, False, timeout=30.0)

        status, payload = self.client.post("/api/v1/chat/queue",
                                           {"sessionId": session_id,
                                            "message": "排队一号"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload.get("started"), payload)
        self.wait_active(session_id, True)

        status, payload = self.client.post("/api/v1/chat/queue",
                                           {"sessionId": session_id,
                                            "message": "排队二号"})
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload.get("started"), "跑着的时候该排着，而不是又开一轮")

        status, payload = self.client.post("/api/v1/chat/stop",
                                           {"sessionId": session_id})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload.get("queued"), 1, payload)
        gate.set()
        self.wait_active(session_id, False, timeout=30.0)
        time.sleep(0.5)        # 给错误的"自动开下一条"留一点发生的机会

        users = [one.get("text") for one in self.detail(session_id)["messages"]
                 if one.get("role") == "user"]
        self.assertIn("排队一号", users)
        self.assertNotIn("排队二号", users, "点了停止却又自动开了下一条")
        status, payload = self.client.get(
            "/api/v1/chat/queue?sessionId=" + urllib.parse.quote(session_id))
        self.assertEqual([one["text"] for one in payload.get("items") or []],
                         ["排队二号"], "被停下的那条把后面排着的那条也带走了")

