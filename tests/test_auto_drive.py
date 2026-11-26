# -*- coding: utf-8 -*-
"""会话的「自动执行」开关：一轮收尾之后平台自己接着干。

## 守的是什么

开关落库（chat_sessions.auto_drive），因此它必须**经接口往返**都能读回原值：
界面上的那颗按钮、下一轮真正跑不跑，读的是同一个地方。第二份真相（浏览器存储、
内存里的默认值）会让"我明明关了它还在跑"变成一件查不出来的事。

真正容易写错的是**什么时候不该接着开**。判据每一条都对应一种"接着开就是错的"，
而错的样子都很安静：多跑一轮只是多烧一次模型，界面上看起来像是模型自己在干活。
所以这里逐条钉住：开关关着、这一轮出错、模型自己说做完了、连开轮数到上限。

## 为什么不用真模型

_begin_turn 换成假的：这一段要验的是**判据**，不是"一轮能不能跑完"。
真起一轮会把网络、超时、线程调度全拉进来，而它们与这里断言的四条判据无关。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import chat, loop, server, turnpump               # noqa: E402
from pentestd.store import Store                               # noqa: E402
from tests import support                                      # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


def _run(session_id="", outcome=None):
    """一个"刚收尾"的轮次：_auto_continue 只看它记下的结局。"""
    run = turnpump.TurnRun(session_id)
    if outcome:
        run.note_outcome(outcome[0], outcome[1])
    return run


def _done(text="这一轮做完了，链路已更新。", summary=""):
    return ("done", {"ok": True, "text": text, "finishSummary": summary,
                     "steps": 3, "truncated": False, "interrupted": False,
                     "stopReason": "", "finishStatus": "", "trace": []})


class AutoContinueTests(unittest.TestCase):
    """判据：什么时候自己开下一轮，什么时候不。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "auto.db"))
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)
        self.session = self.store.create_chat_session("自动执行用例", "",
                                                      auto_drive=True)
        self.sid = self.session["id"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _continue(self, outcome=None, started=True):
        run = _run(self.sid, outcome)
        effect = (None if started else server.ApiError(409, "已经有一轮在跑"))
        with mock.patch.object(self.plane, "_begin_turn", side_effect=effect):
            return self.plane._auto_continue(run, self.sid)

    def test_a_clean_turn_starts_the_next_one_and_says_so(self):
        started = []
        run = _run(self.sid, _done())
        with mock.patch.object(
                self.plane, "_begin_turn",
                side_effect=lambda sid, text, uid, role: started.append((sid, text))):
            self.assertTrue(self.plane._auto_continue(run, self.sid))
        self.assertEqual(1, len(started))
        self.assertEqual(self.sid, started[0][0])
        # 那句话要落成一条普通的用户消息，所以它必须自己说清"接着干、不换方向"，
        # 而不是"继续"两个字——刷新页面之后人只看到这一句。
        text = self.plane.AUTO_CONTINUE_TEXT
        self.assertEqual(text, started[0][1])
        self.assertIn("继续", text)
        self.assertIn("目标不变", text)

    def test_the_switch_off_means_no_next_turn(self):
        self.store.set_chat_session_config(self.sid, auto_drive=False)
        self.assertFalse(self._continue(_done()))
        self.assertEqual(0, int(self.plane.auto_turns.get(self.sid) or 0))

    def test_a_turn_that_ended_in_error_does_not_restart_itself(self):
        """出错不是"做完了"。接着开会把同一个故障原地重放一遍，而失败恢复
        归前端那个退避（3 到 120 秒、封顶 5 次）。"""
        error = ("error", {"error": "模型连接断了", "steps": 2, "trace": []})
        self.assertFalse(self._continue(error))

    def test_a_finished_goal_does_not_keep_the_loop_alive(self):
        """模型自己说做完了就别再开——而"说做完了"的判据与 loop 是同一份。

        两处各判一套的话，会出现"编排认为收尾了、自动执行还在开"这种互相打脸的
        状态，且它只表现在账单上。
        """
        text = "目标已全部达成。" + loop.DONE_MARKERS[0]
        run = _run(self.sid, _done(text=text))
        with mock.patch.object(self.plane, "_begin_turn") as begin:
            self.assertFalse(self.plane._auto_continue(run, self.sid))
        begin.assert_not_called()
        # 计数要清掉：留着的话，人接着说话之前它一直是满的。
        self.assertNotIn(self.sid, self.plane.auto_turns)

    def test_the_streak_stops_at_the_limit(self):
        limit = self.plane.AUTO_TURN_LIMIT
        self.plane.auto_turns[self.sid] = limit
        self.assertFalse(self._continue(_done()))
        self.assertEqual(limit, self.plane.auto_turns[self.sid])

    def test_the_streak_advances_one_per_turn(self):
        self.plane.auto_turns[self.sid] = 2
        self.assertTrue(self._continue(_done()))
        self.assertEqual(3, self.plane.auto_turns[self.sid])

    def test_a_busy_session_is_not_counted_as_started(self):
        """撞上"已经有一轮在跑"时不能记一笔：记了的话，那个上限会被人根本
        没看见的失败悄悄吃掉几次。"""
        self.assertFalse(self._continue(_done(), started=False))
        self.assertNotIn(self.sid, self.plane.auto_turns)

    def test_a_message_from_a_person_resets_the_streak(self):
        """人一说话，那条"连开了几轮"从头算。

        不清的话，人中途接手两次之后，之后每一次自动执行都会被那个早就该作废的
        上限立刻掐掉，而界面上看不出任何原因。
        """
        self.store.save_profile("m-fake", "夹具", "openai_compat",
                                "http://127.0.0.1:9/v1", "fake-1", "none", "",
                                None, 0.2, 512, 20, "")
        self.store.activate_profile("m-fake")
        self.plane.auto_turns[self.sid] = 5
        self.plane._chat_turn_setup(
            {"body": {"message": "我来接手", "sessionId": self.sid},
             "params": {}, "user": None})
        self.assertNotIn(self.sid, self.plane.auto_turns)

    def test_a_queued_message_wins_over_the_automatic_turn(self):
        """人排的条目优先：那是人明确要的那一句，而"接着干"什么时候都能开。"""
        run = _run(self.sid, _done())
        with mock.patch.object(self.plane, "_drain_queue", return_value=True), \
                mock.patch.object(self.plane, "_auto_continue") as auto:
            self.plane._turn_finished(run)
        auto.assert_not_called()

    def test_a_stopped_turn_neither_drains_nor_restarts(self):
        """点了停止就别再自己开：那颗按钮要看起来真的生效了。"""
        run = _run(self.sid, _done())
        run.cancel()
        with mock.patch.object(self.plane, "_drain_queue") as drain, \
                mock.patch.object(self.plane, "_auto_continue") as auto:
            self.plane._turn_finished(run)
        drain.assert_not_called()
        auto.assert_not_called()


class AutoDriveConfigApiTestCase(unittest.TestCase):
    """开关经 HTTP 往返：读回来的与写下去的必须是同一个值。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("autoapi.db")
        self.httpd, self.client = support.start(self.store, WEB_DIR)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.dir.cleanup()

    def _session(self):
        return self.store.create_chat_session("开关用例")["id"]

    def test_a_new_session_comes_with_the_switch_off(self):
        """默认关。默认开的话，每一台机器上的每一次对话都会自己一直跑下去。"""
        sid = self._session()
        self.assertIs(False, self.store.get_chat_session(sid)["autoDrive"])
        _status, detail = self.client.get("/api/v1/chat/sessions/%s" % sid)
        self.assertIs(False, detail["config"]["autoDrive"])

    def test_the_switch_survives_a_write_and_reads_back(self):
        sid = self._session()
        status, payload = self.client.post(
            "/api/v1/chat/sessions/%s/config" % sid, {"autoDrive": True})
        self.assertEqual(status, 200, payload)
        self.assertIs(True, payload["config"]["autoDrive"])
        self.assertIs(True, payload["session"]["autoDrive"])
        self.assertIs(True, self.store.get_chat_session(sid)["autoDrive"])
        # 三个出口读的是同一段解析（_session_config），因此不可能不一致。
        _status, detail = self.client.get("/api/v1/chat/sessions/%s" % sid)
        self.assertIs(True, detail["config"]["autoDrive"])

    def test_turning_it_off_is_not_the_same_as_leaving_it_alone(self):
        """关掉与"没提这一项"在库里是两个值，不能混。

        混了的话，界面在别的下拉上顺手提交一次就会把开关带回默认——而使用者
        刚关掉它。
        """
        sid = self._session()
        status, payload = self.client.post(
            "/api/v1/chat/sessions/%s/config" % sid, {"autoDrive": True})
        self.assertEqual(status, 200, payload)
        status, payload = self.client.post(
            "/api/v1/chat/sessions/%s/config" % sid, {"autoDrive": False})
        self.assertEqual(status, 200, payload)
        self.assertIs(False, payload["config"]["autoDrive"])
        # 只改别的一项时，开关保持原样（这里是"关"），不会被"没提"顶回默认。
        self.client.post("/api/v1/chat/sessions/%s/config" % sid,
                         {"autoDrive": True})
        status, payload = self.client.post(
            "/api/v1/chat/sessions/%s/config" % sid, {"policyId": "readonly"})
        self.assertEqual(status, 200, payload)
        self.assertIs(True, payload["config"]["autoDrive"])

    def test_the_switch_is_not_part_of_the_remembered_entry_choice(self):
        """它不是"上次选了什么"里的一项：那三项随人走，这个开关随会话走。

        跟着人走的话，一个人在 A 会话里打开它，B 会话会莫名其妙也开着。
        """
        sid = self._session()
        admin = ""
        _status, users = self.client.get("/api/v1/users")
        for item in users["users"]:
            if item["username"] == "admin":
                admin = item["id"]
        self.client.post("/api/v1/chat/sessions/%s/config" % sid,
                         {"autoDrive": True})
        self.assertEqual({}, self.store.chat_entry_choice(admin))


if __name__ == "__main__":
    unittest.main(verbosity=2)