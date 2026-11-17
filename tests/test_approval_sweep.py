# -*- coding: utf-8 -*-
"""审查单的**过期**这件事，以及它凭什么不会把会话挡死。

## 为什么单独立一个文件

`R-6a4ef5eb`：建于 21:27:31、等待上限 300 秒、应在 21:32:31 过期；控制面在
21:30:10 重启，两小时后它**仍是 `pending`**。

单子的生命周期是**进程推着走的**——`approval.wait` 轮询到 `expiresAt` 才把它
收成 `expired`。等待它的进程先死，就再也没人推进它。而入口闸的判据是"这张会话
上有没有 `pending` 单子"，于是那张会话被**永久**挡住：挡死的程度与当初那个
HTTP 400 一模一样，只是文案更好看。

所以这里有两条对称的用例，缺任何一条都不行：

* **僵尸单不许挡**（`test_a_corpse_does_not_block_the_turn`）——进程死过之后，
  人会以为"重启一下就好了"，那是真的，但没人会往那儿想；
* **活单照样挡**（`test_a_live_ticket_still_blocks`）——修"不该挡的挡了"时
  最容易顺手把"该挡的"也放过去，而那条路的代价是**配对又被写坏**。

另外 `expired()` 抽出来是有理由的：判据原本在 `wait` 里内联，闸门另写一份的话
它们迟早分叉，而分叉的表现正是"单子明明过期了、闸门却说它在等人"。
"""
import os
import sys
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import approval, server, toolpair                        # noqa: E402
from tests import support                                             # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


def corpse(store, session_id="", tool="run_command", seconds_ago=60.0,
           timeout_s=300):
    """造一张**已经过了等待上限**却还挂着 `pending` 的单子（进程死掉的样子）。

    直接改 `expires_at` 而不是 `time.sleep(timeout)`：用例要断的是"过期怎么处理"，
    不是"睡够没有"。用 sleep 还会让它变成一条慢用例，而慢用例迟早被跳过。
    """
    row = store.record_approval(session_id=session_id, tool=tool, tier="always",
                                headline="要跑一条命令", timeout_s=timeout_s)
    store.conn.execute("UPDATE approvals SET expires_at=? WHERE id=?",
                       (time.time() - seconds_ago, row["id"]))
    store.conn.commit()
    return store.get_approval(row["id"])


class ExpiredRuleTestCase(unittest.TestCase):
    """`approval.expired`：判据本身。"""

    def test_past_its_limit_is_expired(self):
        self.assertTrue(approval.expired({"expiresAt": time.time() - 1}))

    def test_within_its_limit_is_not_expired(self):
        self.assertFalse(approval.expired({"expiresAt": time.time() + 300}))

    def test_no_limit_is_never_expired(self):
        """没写到期时间 = 不按过期处理。

        反过来（缺字段就当过期）会把一张**正常**的单子当场收掉，而症状是
        "审查从来没人来得及批"——看起来像人手慢。
        """
        self.assertFalse(approval.expired({"expiresAt": 0}))
        self.assertFalse(approval.expired({}))
        self.assertFalse(approval.expired(None))

    def test_the_wait_loop_uses_the_same_rule(self):
        """`wait` 与闸门必须**同一份判据**。

        这条用一个真实的过期单子看 `wait` 的结局：它必须立刻收成 `expired`，
        而且用的是"审查超时"那句话——**不是**清理僵尸单用的那句话。两者分开写
        是因为原因不同：一个是"人一直没应答"，另一个是"应答的人和单子一起没了"。
        """
        directory, store = support.temporary_store("approval_wait.db")
        try:
            plane = types.SimpleNamespace(store=store)
            row = corpse(store, seconds_ago=5.0)
            status, settled = approval.wait(plane, row["id"])
            self.assertEqual("expired", status)
            self.assertIn("审查超时", settled["note"])
            self.assertNotIn(approval.STALE_NOTE, settled["note"])
        finally:
            store.close()
            directory.cleanup()


class SweepTestCase(unittest.TestCase):
    """`approval.sweep`：清掉僵尸单，并交出还活着的那些。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("approval_sweep.db")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_a_stale_ticket_is_settled_and_not_handed_back(self):
        row = corpse(self.store)
        live = approval.sweep(self.store)
        self.assertEqual([], live, "过期的单子不许被当成「还在等」")
        self.assertEqual("expired", self.store.get_approval(row["id"])["status"])

    def test_the_stale_note_says_what_really_happened(self):
        row = corpse(self.store)
        approval.sweep(self.store)
        note = self.store.get_approval(row["id"])["note"]
        self.assertEqual(approval.STALE_NOTE, note)
        self.assertIn("重启", note, "要说清是进程死过，而不是人没应答")

    def test_a_live_ticket_is_handed_back_untouched(self):
        row = self.store.record_approval(session_id="", tool="run_command",
                                        timeout_s=300)
        live = approval.sweep(self.store)
        self.assertEqual([row["id"]], [r["id"] for r in live])
        self.assertEqual("pending", self.store.get_approval(row["id"])["status"])

    def test_it_can_be_scoped_to_one_session(self):
        """闸门按会话清，别把别的会话还在等的单子替人决定掉。"""
        mine = corpse(self.store, session_id="C-a")
        theirs = corpse(self.store, session_id="C-b")
        live = approval.sweep(self.store, session_id="C-a")
        self.assertEqual([], live)
        self.assertEqual("expired", self.store.get_approval(mine["id"])["status"])
        self.assertEqual("pending", self.store.get_approval(theirs["id"])["status"],
                         "没点到名的会话不许被动")

    def test_a_settled_ticket_is_left_alone(self):
        row = self.store.record_approval(session_id="", tool="run_command",
                                        timeout_s=300)
        approval.decide(types.SimpleNamespace(store=self.store), row["id"],
                        approve=False, decided_by="admin")
        approval.sweep(self.store)
        settled = self.store.get_approval(row["id"])
        self.assertEqual("denied", settled["status"], "已经裁决过的单子不许被改写成过期")
        self.assertEqual("admin", settled["decidedBy"])

    def test_sweeping_twice_changes_nothing(self):
        row = corpse(self.store)
        approval.sweep(self.store)
        first = self.store.get_approval(row["id"])
        approval.sweep(self.store)
        self.assertEqual(first, self.store.get_approval(row["id"]))


class BootSweepTestCase(unittest.TestCase):
    """`ControlPlane.__init__` 起来就清：它覆盖的正是"进程死过"那个场景。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("approval_boot.db")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_booting_settles_what_the_dead_process_left_behind(self):
        row = corpse(self.store)
        self.assertEqual("pending", self.store.get_approval(row["id"])["status"],
                         "前提：库里确实留着一张僵尸单")
        server.ControlPlane(self.store, WEB_DIR, auth_required=False)
        self.assertEqual("expired", self.store.get_approval(row["id"])["status"],
                         "新进程起来第一件事就是把它结掉")

    def test_booting_does_not_touch_a_live_ticket(self):
        row = self.store.record_approval(session_id="", tool="run_command",
                                        timeout_s=300)
        server.ControlPlane(self.store, WEB_DIR, auth_required=False)
        self.assertEqual("pending", self.store.get_approval(row["id"])["status"],
                         "重启不许把还在等的单子判掉——那等于替人拒绝")


class GateTestCase(unittest.TestCase):
    """入口闸的两侧：僵尸单不许挡死，活单照样挡。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("approval_gate.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)
        self.session = self.store.create_chat_session("审查", "", "", "", "", "")
        self.sid = self.session["id"]
        # `_chat_turn_setup` 要走到底需要一个能解析的模型；夹具里给它一个。
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
                                       "arguments": {"command": "curl -s /"}}]})

    def _ctx(self):
        return {"body": {"message": "继续", "sessionId": self.sid},
                "params": {}, "user": None}

    def test_a_corpse_does_not_block_the_turn(self):
        """**这条是整件事的目的。** 进程死过之后，那一轮不该继续拦人。"""
        self._open_pair()
        row = corpse(self.store, session_id=self.sid)
        self.assertEqual(["call_00_x2I7"],
                         toolpair.open_calls(self.store.chat_messages(self.sid)),
                         "前提：末尾确实欠着结果")
        setup = self.plane._chat_turn_setup(self._ctx())
        self.assertTrue(setup["sessionId"])
        self.assertEqual("expired", self.store.get_approval(row["id"])["status"],
                         "放行的同时要把库里那张僵尸单收掉，两处状态不许分叉")

    def test_a_live_ticket_still_blocks(self):
        """修"不该挡的挡了"时最容易顺手把"该挡的"也放过去。

        "活的"现在指**有人在等它**，而不是"库里有一张 pending"—— 两者的差别
        正好是重启，见下面那一条。
        """
        self._open_pair()
        row = self.store.record_approval(session_id=self.sid, tool="run_command",
                                        timeout_s=300)
        with approval.waiting(row["id"]):
            with self.assertRaises(server.ApiError) as caught:
                self.plane._chat_turn_setup(self._ctx())
        self.assertEqual(409, caught.exception.status)
        self.assertIn(row["id"], caught.exception.message)

    def test_a_ticket_nobody_waits_for_does_not_block(self):
        """进程死过之后那张单子没有执行者了 —— 不该继续挡人。

        这是原来只能靠"启动清僵尸单"去缩短窗口的那件事：等待者随进程一起没了，
        而单子要等到 `expires_at` 才被收掉。拿"有 pending"当判据，就是为一张
        永远不会执行的单子把会话挡到超时为止 —— 线上那次两小时就是这么来的。
        """
        self._open_pair()
        self.store.record_approval(session_id=self.sid, tool="run_command",
                                   timeout_s=300)
        setup = self.plane._chat_turn_setup(self._ctx())
        self.assertTrue(setup["sessionId"], "没有等待者的单子不该挡下一轮")

    def test_the_message_says_where_the_card_actually_is(self):
        """文案要把人指到一个**真实存在**的位置。

        它错过两次：先是让人去「审查」里批（界面里没有那个页签），后来改成
        "卡片长在这一轮的实时气泡里，刷新即作废" —— 前半句对，后半句错。人会
        照它去等超时，或者以为刷新就能绕过去。卡片现在画在对话记录里（与那一轮
        的消息同一列），刷新会自己回来（端到端见 `tests/test_review_survives_reload.py`）。
        """
        self._open_pair()
        row = self.store.record_approval(session_id=self.sid, tool="run_command",
                                         timeout_s=300)
        with approval.waiting(row["id"]):
            with self.assertRaises(server.ApiError) as caught:
                self.plane._chat_turn_setup(self._ctx())
        message = caught.exception.message
        self.assertIn("对话记录里", message, "要说清卡片在哪")
        self.assertIn("刷新", message)
        self.assertNotIn("会让它作废", message,
                         "刷新不再作废它 —— 这么说会让人白白去等超时")
        self.assertIn("停止", message)
        self.assertNotIn("到「审查」里", message,
                         "那个页签不存在，别再把人指过去")

    def test_a_refused_turn_writes_nothing(self):
        self._open_pair()
        row = self.store.record_approval(session_id=self.sid, tool="run_command",
                                         timeout_s=300)
        with approval.waiting(row["id"]):
            before = [m["seq"] for m in self.store.chat_messages(self.sid)]
            with self.assertRaises(server.ApiError):
                self.plane._chat_turn_setup(self._ctx())
        after = [m["seq"] for m in self.store.chat_messages(self.sid)]
        self.assertEqual(before, after,
                         "被拒的那一轮不许往库里加任何一条消息——事故就是这么攒出来的")


class ReviewListTestCase(unittest.TestCase):
    """审查单列表怎么呈现"过了上限却还没收尾"的那一类。

    这不是新造的场景，而是**重启之后必然出现**的那一类：进程推着单子走，进程一死
    就再没有人会发现它该过期了。入口闸那边已经在放行（`GateTestCase`），但列表
    这一侧曾经照旧把它报成"等待批准"，而控制台在那张卡片下面印着「到时间会自动
    收掉」—— 等待者一死，那句话就不成立了，而使用者会一直等一个不动的倒计时。
    """

    def setUp(self):
        self.dir, self.store = support.temporary_store("approval_list.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)
        self.sid = self.store.create_chat_session("审查", "", "", "", "", "")["id"]

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _list(self, **query):
        query.setdefault("sessionId", self.sid)
        return self.plane.list_reviews({"query": query, "params": {}, "body": {},
                                        "user": None})[1]

    def test_a_corpse_is_not_offered_as_waiting(self):
        row = corpse(self.store, session_id=self.sid)
        body = self._list()
        self.assertEqual([row["id"]], [r["id"] for r in body["reviews"]],
                         "前提：它还在库里、还是这张会话的单子")
        card = body["reviews"][0]
        self.assertEqual("expired", card["status"])
        self.assertEqual("已作废", card["statusLabel"])
        self.assertFalse(card["pending"], "不许再把它报成等人回答")
        self.assertEqual(0, card["secondsLeft"], "倒计时已经到 0 了")
        self.assertEqual(0, body["pendingCount"],
                         "角标要与卡片同源：页面上没有待办，角标就不该写 1")

    def test_a_live_ticket_is_still_offered(self):
        """修"不该报成待办的报了"时，最容易顺手把该报的也按掉。"""
        row = self.store.record_approval(session_id=self.sid, tool="run_command",
                                        timeout_s=300)
        with approval.waiting(row["id"]):
            body = self._list()
        self.assertEqual("pending", body["reviews"][0]["status"])
        self.assertTrue(body["reviews"][0]["awaiting"], "有人在等它")
        self.assertGreater(body["reviews"][0]["secondsLeft"], 0)
        self.assertEqual(1, body["pendingCount"])

    def test_the_row_is_not_written_by_the_read(self):
        """读接口不写库：呈现改了，库里那一行留给下一次 `sweep()`。

        这条守的是分工本身。如果哪天有人图省事让列表顺手把单子收掉，"读一次就
        改一次库"会让别处的重放、对账、两个标签页同时刷新都变成隐患。
        """
        row = corpse(self.store, session_id=self.sid)
        self._list()
        self.assertEqual("pending", self.store.get_approval(row["id"])["status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
