# -*- coding: utf-8 -*-
"""会话里"排着的任务"（`pentestd/turnqueue.py`）。

这条功能来自一句最普通的需求：一轮跑着的时候，人还想补一句话。补的那句话
不能被丢掉，也不能立刻打断正在跑的这一轮——它得排在那儿，等这一轮结束。

所以这里钉的不是列表读写，而是四件在界面上看不出来的事：

1. **顺序就是用户看到的顺序。** 队列是 FIFO，界面上「第 N 条」就是按这个顺序
   数的；顺序错了，用户点「现在发送」发出去的不是他以为的那一句。
2. **条目记得是谁排的。** 自动开跑那一刻没有任何 HTTP 请求在场，`_chat_user()`
   拿不到人；身份只能记在条目上，否则"以谁的名义写进历史"就成了猜测。
3. **空话和超长在入口就被拒绝，不做静默截断。** 截断会让人以为整句都排上了。
4. **取走与放回不丢条目。** `pop` + 开跑失败 `push_front` 是自动续跑的唯一
   一条路；这两步之间丢一次，用户排的那句话就凭空消失了。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import turnqueue                                       # noqa: E402


class QueueOrderTestCase(unittest.TestCase):
    """先进先出、身份、上限。"""

    def setUp(self):
        self.queues = turnqueue.ChatQueues()

    def test_items_come_back_in_the_order_they_were_queued(self):
        self.queues.add("s1", "第一条")
        self.queues.add("s1", "第二条")
        self.queues.add("s1", "第三条")
        self.assertEqual([item["text"] for item in self.queues.list("s1")],
                         ["第一条", "第二条", "第三条"])
        self.assertEqual(self.queues.head("s1")["text"], "第一条")
        self.assertEqual(self.queues.size("s1"), 3)
        self.assertEqual(self.queues.count(), 3)

    def test_a_queued_item_remembers_who_queued_it(self):
        item = self.queues.add("s1", "  顺手看一眼 /etc/hosts  ",
                               user_id="u-7", role="admin")
        self.assertEqual(item["text"], "顺手看一眼 /etc/hosts", "首尾空白该去掉")
        stored = self.queues.list("s1")[0]
        self.assertEqual(stored["userId"], "u-7")
        self.assertEqual(stored["role"], "admin")
        self.assertTrue(stored["id"])
        self.assertEqual(stored["error"], "")

    def test_two_sessions_do_not_see_each_others_queue(self):
        self.queues.add("s1", "给一的")
        self.queues.add("s2", "给二的")
        self.assertEqual([item["text"] for item in self.queues.list("s1")], ["给一的"])
        self.assertEqual([item["text"] for item in self.queues.list("s2")], ["给二的"])
        self.assertEqual(self.queues.size("s3"), 0)
        self.assertIsNone(self.queues.head("s3"))
        self.assertFalse(self.queues.has("s3"))

    def test_an_empty_item_is_refused_not_queued_as_blank(self):
        with self.assertRaises(ValueError):
            self.queues.add("s1", "   ")
        self.assertFalse(self.queues.has("s1"))

    def test_an_oversized_item_is_refused_instead_of_truncated(self):
        """截成 4000 字会让人以为整段都排上了——拒绝才说得清。"""
        with self.assertRaises(ValueError):
            self.queues.add("s1", "字" * (turnqueue.MAX_TEXT + 1))
        item = self.queues.add("s1", "字" * turnqueue.MAX_TEXT)
        self.assertEqual(len(item["text"]), turnqueue.MAX_TEXT)

    def test_the_queue_is_capped_and_says_which_session_is_full(self):
        for index in range(turnqueue.MAX_ITEMS):
            self.queues.add("s1", "第 %d 条" % index)
        with self.assertRaises(turnqueue.QueueFull):
            self.queues.add("s1", "多出来的这一条")
        self.assertEqual(self.queues.size("s1"), turnqueue.MAX_ITEMS)
        # 另一个会话不跟着满：上限是每个会话各自的一摞。
        self.queues.add("s2", "别的会话还能排")

    def test_a_returned_item_is_a_copy(self):
        """调用方拿着条目随便改，不该改到队列里那一条。"""
        item = self.queues.add("s1", "原文")
        item["text"] = "被改了"
        self.assertEqual(self.queues.list("s1")[0]["text"], "原文")


class QueueEditTestCase(unittest.TestCase):
    """改、删、取走、放回。"""

    def setUp(self):
        self.queues = turnqueue.ChatQueues()
        self.first = self.queues.add("s1", "第一条")
        self.second = self.queues.add("s1", "第二条")

    def test_update_keeps_the_position_and_clears_a_previous_error(self):
        self.queues.set_error("s1", self.first["id"], "审查单还没结")
        self.assertEqual(self.queues.list("s1")[0]["error"], "审查单还没结")
        changed = self.queues.update("s1", self.first["id"], "改过的第一条")
        self.assertEqual(changed["text"], "改过的第一条")
        self.assertEqual(changed["error"], "", "改过之后旧的失败原因该清掉")
        self.assertEqual(self.queues.list("s1")[0]["id"], self.first["id"],
                         "改文字不该把这一条挪到队尾")
        self.assertEqual(self.queues.head("s1")["text"], "改过的第一条")

    def test_update_refuses_blank_text_and_reports_unknown_ids(self):
        with self.assertRaises(ValueError):
            self.queues.update("s1", self.first["id"], "  ")
        self.assertIsNone(self.queues.update("s1", "不存在", "随便"))
        self.assertEqual(self.queues.size("s1"), 2, "拒绝的改动不该动队列")

    def test_remove_returns_what_was_removed_and_is_idempotent(self):
        gone = self.queues.remove("s1", self.second["id"])
        self.assertEqual(gone["text"], "第二条")
        self.assertIsNone(self.queues.remove("s1", self.second["id"]),
                          "撤一条不存在的不是错，页面双击删除也不该报 500")
        self.assertEqual([item["text"] for item in self.queues.list("s1")],
                         ["第一条"])

    def test_pop_takes_the_head_and_push_front_puts_it_back_in_order(self):
        """自动续跑就靠这两步：取队首 → 开跑失败 → 放回队首。"""
        taken = self.queues.pop("s1")
        self.assertEqual(taken["text"], "第一条")
        self.assertEqual(self.queues.head("s1")["text"], "第二条")
        self.queues.push_front("s1", dict(taken, error="开跑失败：审查单没结"))
        self.assertEqual([item["text"] for item in self.queues.list("s1")],
                         ["第一条", "第二条"], "放回队首而不是队尾")
        self.assertEqual(self.queues.list("s1")[0]["error"], "开跑失败：审查单没结")

    def test_pop_from_an_empty_queue_is_none(self):
        self.queues.drop("s1")
        self.assertIsNone(self.queues.pop("s1"))
        self.queues.push_front("s1", None)     # 不该抛
        self.assertEqual(self.queues.size("s1"), 0)

    def test_drop_only_clears_that_session(self):
        self.queues.add("s2", "另一条")
        self.queues.drop("s1")
        self.assertFalse(self.queues.has("s1"))
        self.assertEqual(self.queues.size("s2"), 1)


if __name__ == "__main__":
    unittest.main()
