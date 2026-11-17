# -*- coding: utf-8 -*-
"""刷新页面之后，审查单还在不在。

## 这件事原来是怎么坏的

卡片只长在那一轮的**实时气泡**里（`view.live`），而 `view.live` 是纯客户端状态。
刷新一次它就没了 —— 但**单子还在库里 `pending`**，等它的那个轮次也还在服务端
跑着（`server._stream` 写不出去也会把生成器抽完）。于是使用者进了死角：

  * 卡片没了，批不了；
  * 想发下一轮，被入口闸以 409 挡住；
  * 而那条 409 当时写的是"刷新页面会让它作废" —— **不对**，而且**从来不对**。

真正会作废它的那条路（`approval.wait` 收一个 `is_closed` 探针、写失败就作废），
在等待期间**根本不会触发**：生成器停在 `wait` 里，一个字节都不产出，所以没有写、
也就没有失败可看。于是同一个"刷新"动作，"模型正在吐字时刷新"会作废单子、
"卡在审查单上刷新"不会 —— 两次结果不同，而使用者做的是同一件事。

## 现在的不变量

  **一张审查单可裁决，当且仅当有一个轮次正在等它**（`approval.awaited`）。

它带来三件事：

  1. 连接断没断不再影响单子（`wait` 不再收那个探针）；
  2. 界面拿 `awaiting` 决定给不给按钮 —— 没有等待者就不给，而不是给一个
     "点了批准却什么都不发生"的按钮；
  3. 入口闸的判据从"库里有一张 pending"换成"有人在等"。差别正好是重启：
     进程一死等待者就没了，那时还为一张永远不会执行的单子把会话挡到超时，
     正是线上 `R-6a4ef5eb` 那次两小时的由来。

这一组用例全部走**真 HTTP**：丢掉连接这件事只有真的丢一次才算验过。
"""
import inspect
import json
import os
import sys
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import approval, server                                 # noqa: E402
from tests import support                                             # noqa: E402
from tests.test_chat import has_tool_result                           # noqa: E402
from tests.test_chat_stream import (ScriptedStreamModel,              # noqa: E402
                                    openai_text_stream,
                                    openai_tool_stream)

WEB_DIR = os.path.join(ROOT, "web")

# 命令本身无害：只有**被批准**的那几条用例才会真的执行它。
PROBE_COMMAND = "echo review-reload-probe"


def wait_until(predicate, timeout=10.0, interval=0.05):
    """轮询到为真，返回最后取到的值。用于等"服务端那个线程往前走了一步"。"""
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return value


# ---------------------------------------------------------------- 真 HTTP 流

class ReloadStreamTestCase(unittest.TestCase):
    """读到 `pending` 帧就把连接丢掉（= 刷新页面），然后从别处裁决。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("review-reload.db")
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

    def ensure_server(self):
        """起服务并登录（一个用例里只起一次）。不跑流的那几条也要它。"""
        if self.httpd is None:
            self.httpd, self.client = support.start(self.store, WEB_DIR)
        return self.client

    def start(self, responder):
        model = ScriptedStreamModel(responder)
        self.models.append(model)
        self.ensure_server()
        status, payload = self.client.post("/api/v1/models", {
            "name": "假模型", "provider": "openai_compat",
            "baseUrl": model.base_url + "/v1", "model": "fake-1",
            "apiKeySource": "none", "temperature": 0.2, "maxTokens": 512,
            "timeoutS": 20})
        if status != 201:
            raise RuntimeError("模型登记失败：%s %s" % (status, payload))
        return model

    def open_stream(self, body):
        request = urllib.request.Request(
            self.client.base + "/api/v1/chat/stream",
            data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream",
                     "X-CSRF-Token": self.client.csrf})
        return self.client.opener.open(request, timeout=30)

    @staticmethod
    def read_events(response):
        """与 `test_chat_stream` 同一套 SSE 解析（分帧规则只该有一处）。"""
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

    @staticmethod
    def command_responder(command):
        def responder(record):
            if has_tool_result(record):
                return openai_text_stream("命令处理完了。")
            return openai_tool_stream("run_command", {"command": command})
        return responder

    def drop_after_pending(self, command=PROBE_COMMAND):
        """跑一轮，读到审查单的 `pending` 帧就**把连接丢掉**，返回那张单子。

        丢连接 = 刷新页面 / 关标签页。此刻服务端的生成器正停在 `wait` 里等裁决，
        一个字节都不往外写 —— 所以"写失败"那条发现断开的路径不会触发，
        这正是为什么要用真连接来验：换个假的探针就测不到这一点。
        """
        self.start(self.command_responder(command))
        response = self.open_stream({"message": "跑一条命令"})
        review = {}
        try:
            for name, data in self.read_events(response):
                if name == "review" and data.get("phase") == "pending":
                    review = data.get("review") or {}
                    break
        finally:
            response.close()                    # ← 就是这一句：连接没了
        self.assertTrue(review.get("id"), "没有拿到审查单")
        return review

    # ---- 断开之后，单子还在

    def test_the_dropped_connection_does_not_settle_the_review(self):
        review = self.drop_after_pending()
        self.assertTrue(review["awaiting"],
                        "pending 帧必须说清有人在等它 —— 界面靠这个决定画不画按钮")
        self.assertEqual("pending",
                         self.store.get_approval(review["id"])["status"],
                         "连接丢了，单子不该跟着作废")
        self.assertTrue(approval.awaited(review["id"]),
                        "等待者还在服务端跑着，这件事必须能被问出来")

    def test_the_command_has_not_run_while_the_review_waits(self):
        """前提：审查是执行**之前**的一步。它没跑了才开始问人。"""
        self.drop_after_pending()
        self.assertEqual([], self.store.list_terminals())

    # ---- 从另一条连接裁决，它真的落地

    def test_a_decision_from_another_connection_runs_the_command(self):
        """**这一条是整件事的目的。** 刷新之后从重新载入的页面点批准，
        命令必须真的执行 —— 等它的那个轮次还在。"""
        review = self.drop_after_pending()
        status, payload = self.client.post(
            "/api/v1/reviews/%s" % review["id"], {"approve": True})
        self.assertEqual(200, status, payload)
        self.assertEqual("approved", payload["review"]["status"])
        self.assertTrue(
            wait_until(lambda: self.store.list_terminals()),
            "批准之后命令必须真的跑起来 —— 这就是「刷新没弄丢它」的证据")
        self.assertTrue(wait_until(lambda: not approval.awaited(review["id"])),
                        "裁决落地之后，等待者应当退出登记")

    def test_a_decision_from_another_connection_can_also_refuse(self):
        review = self.drop_after_pending()
        status, _payload = self.client.post(
            "/api/v1/reviews/%s" % review["id"], {"approve": False})
        self.assertEqual(200, status)
        self.assertEqual("denied",
                         self.store.get_approval(review["id"])["status"])
        self.assertEqual([], self.store.list_terminals(), "拒绝之后不许执行")

    # ---- 界面要的那两个事实

    def test_the_list_endpoint_marks_who_has_a_waiter(self):
        review = self.drop_after_pending()
        status, payload = self.client.get("/api/v1/reviews?status=pending")
        self.assertEqual(200, status)
        rows = {row["id"]: row for row in payload["reviews"]}
        self.assertIn(review["id"], rows)
        self.assertTrue(rows[review["id"]]["awaiting"])

    def test_a_review_with_nobody_waiting_is_marked_so(self):
        self.ensure_server()
        row = self.store.record_approval(tool="run_command", timeout_s=60)
        status, payload = self.client.get("/api/v1/reviews?status=pending")
        self.assertEqual(200, status)
        rows = {item["id"]: item for item in payload["reviews"]}
        self.assertIn(row["id"], rows)
        self.assertFalse(rows[row["id"]]["awaiting"],
                         "没有等待者的单子必须如实标出来 —— 界面据此不画按钮")

    def test_deciding_a_review_with_no_waiter_is_refused(self):
        """**不许把 `approved` 记下来。**

        记下来最坏：库里写着"已批准"，而那条命令永远不会执行 —— 事后翻审计会
        看到一次"批过的执行"，实际什么都没发生，与使用者以为的正好相反。
        界面已经不画按钮了，这一条守的是"渲染与点击之间等待者没了"那个竞态。
        """
        row = self.store.record_approval(tool="run_command", timeout_s=60)
        self.ensure_server()
        status, payload = self.client.post(
            "/api/v1/reviews/%s" % row["id"], {"approve": True})
        self.assertEqual(409, status, payload)
        self.assertEqual("pending",
                         self.store.get_approval(row["id"])["status"],
                         "被拒的裁决不许改库里的状态")


# ------------------------------------------------------- 断连不再是一条出口

class NoConnectionTieTestCase(unittest.TestCase):

    def test_the_wait_cannot_see_the_connection(self):
        """这条守的是一个**已删除**的参数。

        原来 `wait(..., is_closed=...)` 会在"写失败"时把单子收成 `expired`。
        而等待期间一个字节都不写，所以那个探针在最常见的场景（人刷新了页面）
        根本不会触发 —— 同一个动作两种结果。删掉之后期限统一由 `expires_at`
        兜底；有人把它加回来，这里会红。
        """
        self.assertNotIn("is_closed", inspect.signature(approval.wait).parameters,
                         "审查单不该再由「连接还在不在」来裁决")


# -------------------------------------------------------------- 入口闸判据

class GateUsesAwaitingTestCase(unittest.TestCase):
    """闸门的判据是"有人在等"，不是"库里有一张 pending"。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("review-gate.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)
        session = self.store.create_chat_session("审查", "", "", "", "", "")
        self.sid = session["id"]
        self.store.save_profile("m-fake", "夹具", "openai_compat",
                                "http://127.0.0.1:9/v1", "fake-1", "none", "",
                                None, 0.2, 512, 20, "")
        self.store.activate_profile("m-fake")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _open_pair(self):
        """把会话写成"上一轮发起了 run_command、结果还没落库"。"""
        self.store.append_chat_message(self.sid, "user", {"text": "看一眼首页"})
        self.store.append_chat_message(self.sid, "assistant", {
            "text": "", "toolCalls": [{"id": "call_00_x2I7", "name": "run_command",
                                       "arguments": {"command": PROBE_COMMAND}}]})

    def _ctx(self):
        return {"body": {"message": "继续", "sessionId": self.sid},
                "params": {}, "user": None}

    def _ticket(self, timeout_s=300):
        return self.store.record_approval(session_id=self.sid, tool="run_command",
                                          timeout_s=timeout_s)

    def test_a_waiter_blocks_the_turn(self):
        self._open_pair()
        row = self._ticket()
        with approval.waiting(row["id"]):
            with self.assertRaises(server.ApiError) as caught:
                self.plane._chat_turn_setup(self._ctx())
        self.assertEqual(409, caught.exception.status)
        self.assertIn(row["id"], caught.exception.message)

    def test_a_review_without_a_waiter_does_not_block(self):
        """重启之后那张单子没有执行者了 —— 不该继续挡人。

        这正是原来只能靠"启动清僵尸单"去缩短窗口的那件事：进程一死等待者就没了，
        而单子要等到 `expires_at` 才被收掉，中间那段时间谁也不该被挡。
        """
        self._open_pair()
        self._ticket()
        setup = self.plane._chat_turn_setup(self._ctx())
        self.assertTrue(setup["sessionId"], "没有等待者的单子不该挡下一轮")

    def test_the_message_says_where_the_card_really_is(self):
        """老文案说"刷新页面会让它作废"，而那是错的 —— 说了会让人白白去等超时。

        卡片现在画在**对话记录里**（与这一轮的消息同一列），刷新重新载入会话时
        它自己回来，所以文案必须这么说。
        """
        self._open_pair()
        row = self._ticket()
        with approval.waiting(row["id"]):
            with self.assertRaises(server.ApiError) as caught:
                self.plane._chat_turn_setup(self._ctx())
        message = caught.exception.message
        self.assertIn("对话记录里", message, "要说清卡片在哪")
        self.assertIn("刷新", message)
        self.assertNotIn("会让它作废", message, "刷新不再作废它")
        self.assertIn("停止", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
