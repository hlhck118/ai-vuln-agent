# -*- coding: utf-8 -*-
"""`pentestd/context.py` + 区块索引的用例。

这里守的不是"能压缩"（那是 `compaction.py` 的事），而是**压缩之后还能查回来**：

* 范围必须是 `seq` 而不是消息下标——下标每一轮都会变，用它当范围的表现是
  "读回来的是别人的历史，而它看起来完全正常"；
* 检索只出摘要片段、还原才出原文——两者混在一起，一次检索就把压缩白做了；
* 区块与压缩点**成对**：有一条摘要消息就必须有一个区块，否则"这 10 条去哪了"
  在 `context_status` 里永远得不到回答。

`WiringTestCase` 用真的 `ControlPlane` + 临时库走工具层，其余全部纯函数。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import chat, context, policy, server                 # noqa: E402
from tests import support                                          # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")

SESSION = "C-abc12345"


def _messages(*pairs):
    """`(seq, role, text)` → 中立消息列表。"""
    return [{"seq": seq, "role": role, "text": text} for seq, role, text in pairs]


class RangeTestCase(unittest.TestCase):
    """`range_of` 与区块 id：范围与去重的两个依据。"""

    def test_range_uses_seq_not_index(self):
        folded = _messages((17, "user", "扫 10.0.0.0/24"),
                           (18, "assistant", "收到"),
                           (23, "tool", "22/tcp open ssh"))
        first, last, known = context.range_of(folded)
        self.assertEqual((first, last, known), (17, 23, True))

    def test_unstored_messages_make_the_range_unknown(self):
        """没有 seq 的消息（合成的工具结果、测试桩）不能参与定范围。

        它为 0 时如果仍被当成"范围是 0"，还原会读回第一条消息——而调用方
        完全看不出来自己读错了。所以这里必须显式返回 known=False。
        """
        folded = [{"role": "user", "text": "没有 seq"}]
        self.assertEqual(context.range_of(folded), (0, 0, False))
        self.assertEqual(context.range_of([]), (0, 0, False))

    def test_one_missing_seq_does_not_poison_the_rest(self):
        """"一半有 seq"是真实情况：同一条历史里混着合成消息。

        取有的那些仍然正确——被压掉的那段**至少**覆盖到这些 seq，
        还原时读到的是超集而不是错的段。
        """
        folded = _messages((40, "user", "a")) + [{"role": "tool", "text": "合成"}]
        self.assertEqual(context.range_of(folded), (40, 40, True))

    def test_block_id_is_stable_for_the_same_range(self):
        one = context.block_id(SESSION, 17, 23, 100.5)
        two = context.block_id(SESSION, 17, 23, 100.5)
        self.assertEqual(one, two)
        self.assertTrue(one.startswith("B-"))

    def test_block_id_differs_across_sessions_and_ranges(self):
        base = context.block_id(SESSION, 17, 23, 100.5)
        self.assertNotEqual(base, context.block_id("C-other", 17, 23, 100.5))
        self.assertNotEqual(base, context.block_id(SESSION, 17, 24, 100.5))


class BlockTestCase(unittest.TestCase):
    """`make_block` / `public`：落库形状与对外形状。"""

    def setUp(self):
        self.folded = _messages((5, "user", "目标 10.10.10.250"),
                                (6, "tool", "6379/tcp open redis"))
        self.summary = "目标：10.10.10.250\n已确认 redis 6379 未授权访问。"
        self.block = context.make_block(SESSION, self.folded, self.summary,
                                        measured=52000, window=131072,
                                        basis="measured", stamp=1.5, model="m-1")

    def test_block_carries_the_range_and_the_counts(self):
        self.assertEqual((self.block["firstSeq"], self.block["lastSeq"]), (5, 6))
        self.assertTrue(self.block["rangeKnown"])
        self.assertEqual(self.block["folded"], 2)
        self.assertEqual(self.block["foldedChars"],
                         len("目标 10.10.10.250") + len("6379/tcp open redis"))
        self.assertEqual(self.block["summaryChars"], len(self.summary))
        self.assertEqual(self.block["promptTokens"], 52000)

    def test_topic_is_the_first_line_not_a_blind_cut(self):
        """摘要把最重要的"目标与范围"写在第一行，标题就该取那一行。

        按字数硬切会把「目标：10.10.10.250，已开放 22/80/6379」切成
        「目标：10.10.10.250，已开」。

        注意这里**不是**在断言"标题里有冒号"——那是对摘要写法的假设；
        真正的不变量是"标题不含换行、且等于第一行去掉列表前缀"。
        """
        topic = context.topic_of("- 目标：10.10.10.250，已开放 22/80/6379\n- 结论：…")
        self.assertEqual(topic, "目标：10.10.10.250，已开放 22/80/6379")
        self.assertNotIn("\n", topic)

    def test_long_topic_is_truncated_with_an_ellipsis(self):
        topic = context.topic_of("x" * 200)
        self.assertTrue(topic.endswith("…"))
        self.assertLessEqual(len(topic), context.TOPIC_CHARS + 1)

    def test_empty_summary_still_yields_a_topic(self):
        self.assertEqual(context.topic_of(""), "(空摘要)")
        self.assertEqual(context.topic_of("   \n  "), "(空摘要)")

    def test_public_hides_the_summary_by_default(self):
        """检索结果默认不带摘要正文——否则一次检索就把压缩白做了。"""
        item = context.public(self.block)
        self.assertNotIn("summary", item)
        self.assertEqual(item["blockId"], self.block["id"])
        self.assertEqual(item["topic"], context.topic_of(self.summary))

    def test_public_can_carry_the_summary_when_asked(self):
        self.assertEqual(context.public(self.block, with_summary=True)["summary"],
                         self.summary)


class SearchTestCase(unittest.TestCase):
    """`search`：命中、排序、空查询。"""

    def setUp(self):
        self.blocks = [
            context.make_block(SESSION, _messages((1, "user", "a")),
                               "目标：10.0.0.0/24\n扫到 22 与 80 开放",
                               stamp=10.0),
            context.make_block(SESSION, _messages((9, "user", "b")),
                               "目标：192.168.1.5\nredis 6379 未授权，可写入",
                               stamp=20.0),
            context.make_block(SESSION, _messages((17, "user", "c")),
                               "结论：那条 6379 的写入路径没有回显",
                               stamp=30.0),
        ]

    def test_exact_phrase_outranks_scattered_words(self):
        """整句命中（第 9 块）必须压过只分别提过这两个词的（第 17 块）。

        两块都命中，但只有一块原样写过这句话。排序反了的表现是：
        模型先被送去读一段无关的历史，然后才找到真正的那一段。
        """
        hits = context.search(self.blocks, "redis 6379 未授权")
        self.assertTrue(hits)
        self.assertIn("6379 未授权", hits[0]["summary"])
        self.assertGreater(hits[0]["score"], 0)

    def test_hits_carry_a_snippet_not_the_whole_summary(self):
        hits = context.search(self.blocks, "10.0.0.0/24")
        self.assertEqual(len(hits), 1)
        self.assertIn("10.0.0.0/24", hits[0]["snippet"])
        self.assertLessEqual(len(hits[0]["snippet"]),
                             context.SNIPPET_PAD * 2 + 40)

    def test_empty_query_lists_the_newest_blocks_first(self):
        hits = context.search(self.blocks, "")
        self.assertEqual(len(hits), 3)
        self.assertEqual(hits[0]["createdAt"], 30.0)
        self.assertEqual(hits[-1]["createdAt"], 10.0)

    def test_limit_bounds_the_result(self):
        self.assertEqual(len(context.search(self.blocks, "", limit=2)), 2)
        self.assertEqual(len(context.search(self.blocks, "", limit=0)), 1)

    def test_no_hit_is_an_empty_list_not_an_error(self):
        self.assertEqual(context.search(self.blocks, "完全没提过的词"), [])
        self.assertEqual(context.search([], "任意"), [])

    def test_short_terms_are_ignored_to_avoid_noise(self):
        """单字查询不参与打分：中文里"的""了"会让每一次检索都命中每一个区块。

        关键不只是"词元表为空"，而是**它不会退化成列出全部区块**——
        那个退化看起来像查到了结果，实际一条都没匹配上。
        """
        self.assertEqual(context._terms("了"), [])
        self.assertEqual(context._terms("redis a"), ["redis a", "redis"])
        self.assertEqual(context.search(self.blocks, "了"), [])


class RenderTestCase(unittest.TestCase):
    """`render`：还原文的分页。"""

    def setUp(self):
        self.messages = _messages((1, "user", "扫 10.0.0.0/24"),
                                  (2, "assistant", "开始"),
                                  (3, "tool", "22/tcp open ssh\n" + "x" * 500))

    def test_render_labels_roles_and_tools(self):
        body = context.render(self.messages)["text"]
        self.assertIn("【使用者】 扫 10.0.0.0/24", body)
        self.assertIn("【助手】 开始", body)
        self.assertIn("【工具结果", body)

    def test_tool_name_goes_into_the_label(self):
        messages = [{"seq": 4, "role": "tool", "toolName": "run_command",
                     "text": "id\nuid=0"}]
        self.assertIn("run_command", context.render(messages)["text"])

    def test_paging_by_characters_not_by_message_count(self):
        """一条可以几万字符：按条数分页会让"读一页"的大小随机。"""
        first = context.render(self.messages, 0, 100)
        self.assertTrue(first["hasMore"])
        self.assertEqual(len(first["text"]), 100)
        self.assertEqual(first["nextOffset"], 100)
        second = context.render(self.messages, first["nextOffset"], 100)
        self.assertEqual(second["offset"], 100)
        self.assertNotEqual(first["text"], second["text"])

    def test_last_page_has_no_next_offset(self):
        page = context.render(self.messages, 0, context.PAGE_MAX)
        self.assertFalse(page["hasMore"])
        self.assertIsNone(page["nextOffset"])
        self.assertEqual(page["total"], len(page["text"]))

    def test_limit_is_capped_and_negative_offset_is_clamped(self):
        page = context.render(self.messages, -5, 10 ** 8)
        self.assertEqual(page["offset"], 0)
        self.assertEqual(page["limit"], context.PAGE_MAX)

    def test_tool_call_arguments_survive_into_the_text(self):
        """还原文的用途是复核"当时到底发了什么"，丢掉参数就没法复核。"""
        messages = [{"seq": 5, "role": "assistant", "text": "",
                     "toolCalls": [{"name": "start_probe",
                                    "arguments": {"target": "10.0.0.0/24"}}]}]
        body = context.render(messages)["text"]
        self.assertIn("start_probe", body)
        self.assertIn("10.0.0.0/24", body)


class StatusTestCase(unittest.TestCase):
    """`status` / `describe`：使用者问"我的历史还在不在"时的答案。"""

    def test_empty_ledger_says_nothing_was_compacted(self):
        data = context.status([], window=131072, measured=40000,
                              basis="measured")
        self.assertEqual(data["blocks"], 0)
        self.assertEqual(data["foldedMessages"], 0)
        self.assertIn("还没有压缩过", context.describe(data))

    def test_ledger_aggregates_blocks(self):
        blocks = [context.make_block(SESSION, _messages((1, "a", "b"),
                                                        (2, "c", "d")), "s1",
                                     stamp=1.0),
                  context.make_block(SESSION, _messages((3, "e", "f")), "s2",
                                     stamp=2.0)]
        data = context.status(blocks, window=200000, measured=100000)
        self.assertEqual(data["blocks"], 2)
        self.assertEqual(data["foldedMessages"], 3)
        self.assertEqual(data["rangesKnown"], 2)
        self.assertEqual(data["ratio"], 0.5)

    def test_ratio_is_zero_without_a_window(self):
        """没声明窗口时不能算出 0.0 以外的数，也不能抛零除。"""
        data = context.status([], window=0, measured=1000)
        self.assertEqual(data["ratio"], 0.0)

    def test_describe_reports_both_sides(self):
        """只说"压缩了 3 次"答不了"还有多少没压"。"""
        blocks = [context.make_block(SESSION, _messages((1, "a", "b")), "s",
                                     stamp=1.0)]
        text = context.describe(context.status(blocks, window=1000, measured=800))
        self.assertIn("覆盖 1 条", text)
        self.assertIn("80%", text)


class WiringTestCase(unittest.TestCase):
    """工具层：注册、分级、落库、检索、还原、拒绝。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("context.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)
        self.session = {"sessionId": SESSION, "contextWindow": 131072}
        self.store.create_chat_session(title="上下文", user_id="")
        # `create_chat_session` 的签名在各版本间变过，拿真实 id 覆盖一下
        sessions = self.store.chat_sessions("")
        if sessions:
            self.session["sessionId"] = sessions[0]["id"]
        else:
            self.session["sessionId"] = SESSION

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _append(self, role, payload):
        return self.store.append_chat_message(self.session["sessionId"], role,
                                              payload)

    def _seed_block(self):
        first = self._append("user", {"text": "扫 10.10.10.250"})
        second = self._append("tool", {"text": "6379/tcp open redis",
                                       "toolName": "run_command"})
        block = context.make_block(
            self.session["sessionId"],
            [{"seq": first["seq"]}, {"seq": second["seq"]}],
            "目标：10.10.10.250\nredis 6379 未授权", stamp=1.0)
        self.store.record_context_block(block)
        return block, first, second

    def test_three_tools_are_registered_and_read_only(self):
        names = {tool["name"] for tool in chat.tool_catalog()}
        for name in ("context_search", "context_decompress", "context_status"):
            self.assertIn(name, names)
            self.assertEqual(policy.TOOL_LEVEL.get(name), "readonly")

    def test_read_only_policy_sees_all_three(self):
        tools = policy.filter_tools(chat.tool_catalog(), "readonly")
        names = {tool["name"] for tool in tools}
        for name in ("context_search", "context_decompress", "context_status"):
            self.assertIn(name, names)

    def test_search_reads_blocks_from_the_store(self):
        block, _, _ = self._seed_block()
        outcome = chat.execute_tool(self.plane, "context_search",
                                    {"query": "redis"}, "", session=self.session)
        self.assertTrue(outcome["ok"], outcome["summary"])
        hits = outcome["data"]["hits"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["blockId"], block["id"])
        self.assertNotIn("text", hits[0])          # 检索不给原文

    def test_search_finds_nothing_when_history_was_never_compacted(self):
        outcome = chat.execute_tool(self.plane, "context_search", {}, "",
                                    session=self.session)
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["data"]["hits"], [])
        self.assertIn("还没有可检索", outcome["summary"])

    def test_decompress_returns_the_original_text(self):
        block, first, second = self._seed_block()
        outcome = chat.execute_tool(self.plane, "context_decompress",
                                    {"blockId": block["id"]}, "", session=self.session)
        self.assertTrue(outcome["ok"], outcome["summary"])
        self.assertIn("扫 10.10.10.250", outcome["data"]["text"])
        self.assertIn("6379/tcp open redis", outcome["data"]["text"])
        self.assertEqual((outcome["data"]["firstSeq"],
                          outcome["data"]["lastSeq"]),
                         (first["seq"], second["seq"]))
        self.assertFalse(outcome["data"]["hasMore"])

    def test_decompress_refuses_an_unknown_block(self):
        outcome = chat.execute_tool(self.plane, "context_decompress",
                                    {"blockId": "B-deadbeef"}, "", session=self.session)
        self.assertFalse(outcome["ok"])
        self.assertIn("context_status", outcome["summary"])

    def test_decompress_refuses_an_unreliable_range(self):
        """范围不可信的区块**必须拒绝**：按它读会读到别的历史。"""
        block = context.make_block(self.session["sessionId"],
                                   [{"role": "tool", "text": "合成结果"}],
                                   "摘要", stamp=2.0)
        self.assertFalse(block["rangeKnown"])
        self.store.record_context_block(block)
        outcome = chat.execute_tool(self.plane, "context_decompress",
                                    {"blockId": block["id"]}, "", session=self.session)
        self.assertFalse(outcome["ok"])
        self.assertIn("范围不可信", outcome["summary"])

    def test_status_reports_the_ledger(self):
        self._seed_block()
        outcome = chat.execute_tool(self.plane, "context_status", {}, "",
                                    session=self.session)
        self.assertTrue(outcome["ok"], outcome["summary"])
        self.assertEqual(outcome["data"]["blocks"], 1)
        self.assertEqual(outcome["data"]["foldedMessages"], 2)
        self.assertEqual(outcome["data"]["window"], 131072)

    def test_no_session_is_refused_rather_than_returning_empty(self):
        """"检索到 0 条"和"没有会话可查"的下一步完全不同，不能混成一个。"""
        outcome = chat.execute_tool(self.plane, "context_search", {}, "", {})
        self.assertFalse(outcome["ok"])
        self.assertIn("没有 id", outcome["summary"])

    def test_block_round_trips_through_the_store(self):
        block, _, _ = self._seed_block()
        rows = self.store.context_blocks(self.session["sessionId"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], block["id"])
        self.assertEqual(rows[0]["summary"], block["summary"])
        self.assertTrue(rows[0]["rangeKnown"])

    def test_recording_the_same_block_twice_does_not_double_the_ledger(self):
        """落库失败后重试不该让"覆盖了多少条"凭空翻倍。"""
        block, _, _ = self._seed_block()
        self.store.record_context_block(block)
        rows = self.store.context_blocks(self.session["sessionId"])
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
