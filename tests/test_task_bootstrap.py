# -*- coding: utf-8 -*-
"""任务从哪来：一条对话的第一句话。

这一组钉的是「渗透任务由会话自动建立」这条约定本身：说一句话之后任务就在了、名字
是他说的那句话、目标是从话里认出来的地址；第二句话不会另立一个任务，也不改动已有
的归属与名字。
"""
import datetime
import unittest

from pentestd import tasks
from pentestd.store import DEFAULT_SESSION_LABEL
from tests import support


def _session(store, title="新对话", user="admin"):
    return store.create_chat_session(title, user, "")


class TargetTests(unittest.TestCase):
    def test_target_shapes(self):
        cases = (
            ("帮我渗透 http://10.10.10.110:8080/login 拿下后台",
             "http://10.10.10.110:8080/login"),
            ("扫一下 10.10.10.0/24 这段", "10.10.10.0/24"),
            ("目标是 example.com:8443 那个面板", "example.com:8443"),
            ("打一下 10.10.10.110。", "10.10.10.110"),
            ("帮我看看后台有没有弱口令", ""),
        )
        for text, expected in cases:
            self.assertEqual(expected, tasks.target_of(text), text)

    def test_name_drops_the_politeness(self):
        self.assertEqual("看看后台", tasks.name_of("麻烦你帮我看看后台"))
        self.assertEqual("把后台拿下", tasks.name_of("帮我把后台拿下"))
        self.assertEqual("", tasks.name_of("   "))
        long_name = tasks.name_of("打" * 100)
        self.assertEqual(tasks.NAME_LIMIT, len(long_name))

    def test_name_uses_the_first_non_empty_line(self):
        self.assertEqual("先看首页",
                         tasks.name_of("\n\n先看首页\n然后再扫端口"))


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("task-bootstrap.db")

    def tearDown(self):
        self.store.close()

    def test_first_message_creates_the_task(self):
        session = _session(self.store)
        text = "帮我渗透 http://10.10.10.110:8080/login 拿下后台"
        self.store.append_chat_message(session["id"], "user", {"text": text})
        eid = tasks.bootstrap(self.store, session["id"], text, "admin")
        self.assertTrue(eid.startswith("E-"), eid)
        meta = self.store.get_chat_session(session["id"])
        self.assertEqual(eid, meta["engagementId"])
        task = self.store.get_task(eid)
        self.assertEqual("http://10.10.10.110:8080/login", task["target"])
        self.assertEqual(text, task["objective"])
        self.assertEqual(text, task["description"])
        self.assertTrue(task["name"].startswith("渗透"), task["name"])
        self.assertIn("admin", task["authorization"])
        # 任务名下就是这条会话——「谁在打它」有据可查。
        self.assertEqual([session["id"]],
                         [item["id"] for item in task["sessions"]])
        self.assertEqual(1, task["sessions"][0]["messageCount"])

    def test_no_target_keeps_the_placeholder_and_still_names_the_task(self):
        session = _session(self.store)
        eid = tasks.bootstrap(self.store, session["id"], "帮我把后台拿下", "admin")
        task = self.store.get_task(eid)
        self.assertEqual(DEFAULT_SESSION_LABEL, task["target"])
        self.assertEqual("把后台拿下", task["name"])
        # 任务名与「被测目标」是两个字段：不知道打谁时不能把一句话塞进 target，
        # 报告第一行（被测的是什么）会跟着变成一句口号。
        self.assertNotEqual(task["name"], task["target"])

    def test_the_second_message_does_not_start_another_task(self):
        session = _session(self.store)
        first = tasks.bootstrap(self.store, session["id"],
                                "打一下 10.10.10.110", "admin")
        second = tasks.bootstrap(self.store, session["id"],
                                 "换个地方：看看 10.10.10.250 的 8080", "admin")
        self.assertEqual(first, second)
        task = self.store.get_task(first)
        self.assertEqual("10.10.10.110", task["target"])
        self.assertEqual(1, len(self.store.list_tasks()["tasks"]))

    def test_merges_into_an_existing_task_and_keeps_its_name(self):
        existing = self.store.create_engagement("10.10.10.110", "之前那一次", "AUTH-1")
        self.store.update_task(existing["id"], {"name": "既有任务"})
        session = _session(self.store)
        eid = tasks.bootstrap(self.store, session["id"],
                              "再摸一下 10.10.10.110 的 22 端口", "admin")
        self.assertEqual(existing["id"], eid)
        task = self.store.get_task(eid)
        self.assertEqual("既有任务", task["name"])
        self.assertEqual("AUTH-1", task["authorization"])

    def test_deleted_task_is_rebuilt_on_the_next_message(self):
        session = _session(self.store)
        first = tasks.bootstrap(self.store, session["id"], "打 10.10.10.110", "admin")
        self.store.delete_engagement(first)
        second = tasks.bootstrap(self.store, session["id"], "打 10.10.10.250", "admin")
        self.assertNotEqual(first, second)
        self.assertEqual(second,
                         self.store.get_chat_session(session["id"])["engagementId"])

    def test_unknown_session_is_ignored(self):
        self.assertEqual("", tasks.bootstrap(self.store, "C-nope", "打 1.2.3.4"))
        self.assertEqual("", tasks.bootstrap(self.store, "", "打 1.2.3.4"))


if __name__ == "__main__":
    unittest.main()
