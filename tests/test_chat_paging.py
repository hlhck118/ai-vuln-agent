# -*- coding: utf-8 -*-
"""长输出读不全：分页、原始输出落库、正文可读。

报上来的现象是"报告读到 fact-33 就断了、nmap 的输出读不到"，而它其实是
**四个独立的缺陷叠在一起**，每一个单独看都像"这一处本来就该这样"：

1. `agent.LocalExecutor.run_once` 与 `server.job_result` 各写一份五键字面量，
   两份都没有 `toolOutput`——`execute_toolkit` 特意保留到 64 KB 的原始输出在
   最后一步被丢掉，而日志、状态页、报告全都正常，只有证据本身不见了；
2. `get_report` 把正文 `_clip(markdown, 6000)` 切一次；
3. 回灌给模型时又被回灌上限连着 JSON 信封切一次，**切完是非法 JSON**；
4. 没有任何工具能读单个任务的原始输出、或单个节点的正文（`get_chain_overview`
   只给最近 8 条的标题）。

所以这里断言的都是**端到端行为**：建一个真库、跑真执行器、按页真读，然后问
"每一个 fact 都读得到吗"。只断言"返回里有 text 字段"毫无用处——那正是缺陷 ②③
看起来的样子：它确实返回了正文，只是少了一多半。
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agent as agent_module                       # noqa: E402
from pentestd import chat, hubcatalog, policy, server            # noqa: E402
from pentestd import store as store_module                       # noqa: E402
from tests import support                                        # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


def build(store, target: str = "10.10.10.250") -> str:
    """一个目标 + 一个意图 + 若干事实。返回 engagement id。"""
    return store.create_engagement(target, "验证长输出", "工单 #1")["id"]


def fill(store, eid: str, count: int, detail_chars: int = 110) -> list:
    """写 `count` 条事实，正文够长以便把报告撑到多页。返回它们的 id。"""
    intent = store.add_node(eid, "intent",
                            {"title": "外部工具调度", "detail": "nmap -sV 目标",
                             "goalId": "goal-1"})
    made = []
    for index in range(count):
        node = store.add_node(eid, "fact",
                              {"intentId": intent["id"], "kind": "info",
                               "target": "nmap",
                               "detail": "f%03d %s" % (index, "x" * detail_chars)})
        made.append(node["id"])
    return made


def page_all(plane, eid: str, tool: str = "get_report", extra=None) -> str:
    """按 nextOffset 一直翻到底，把每一页拼起来。**只翻到 hasMore 为假为止。**"""
    args = dict(extra or {})
    args["engagementId"] = eid
    chunks, guard = [], 0
    while True:
        guard += 1
        if guard > 200:
            raise AssertionError("翻页没有终点：nextOffset 没有前进")
        outcome = chat.execute_tool(plane, tool, dict(args), eid)
        data = outcome["data"]
        chunks.append(data["text"])
        if not data["hasMore"]:
            return "".join(chunks)
        args["offset"] = data["nextOffset"]


class PageEnvelopeTestCase(unittest.TestCase):
    """分页信封本身：`_page_text` 的契约。"""

    def test_paging_visits_every_character_exactly_once(self):
        """把所有页拼起来必须**逐字节等于原文**：不重不漏。"""
        text = "".join("第%d行\n" % i for i in range(5000))
        parts, offset = [], 0
        while True:
            page = chat._page_text(text, offset, 1234)
            parts.append(page["text"])
            self.assertLessEqual(page["count"], 1234)
            if not page["hasMore"]:
                break
            self.assertEqual(page["nextOffset"], offset + page["count"])
            offset = page["nextOffset"]
        self.assertEqual("".join(parts), text)

    def test_offset_past_the_end_is_an_empty_page_with_the_right_total(self):
        """越界只表示"后面没有了"，不是失败——报成失败会让模型重试同一页。"""
        page = chat._page_text("0123456789", 999, 10)
        self.assertEqual(page["text"], "")
        self.assertEqual(page["count"], 0)
        self.assertEqual(page["total"], 10)
        self.assertFalse(page["hasMore"])
        self.assertEqual(page["nextOffset"], 10)   # 停在末尾，不越过

    def test_page_size_and_offset_survive_model_written_junk(self):
        """`limit`/`offset` 由模型手写：非整数、零、负数一律退回默认，不抛。"""
        for bad in (0, -5, "abc", None, True, [], {}):
            page = chat._page_text("x" * 100, 0, bad)
            self.assertGreaterEqual(page["limit"], 1, "limit=%r" % (bad,))
            self.assertLessEqual(page["limit"], chat.TEXT_PAGE_MAX)
        for bad in (-1, "x", None, True):
            self.assertEqual(chat._page_text("x" * 100, bad, 10)["offset"], 0)

    def test_a_big_page_is_clamped_and_says_so(self):
        """要一个超大页只拿到上限，而 `limit` 字段如实写出**实际**给了多少。"""
        page = chat._page_text("x" * 100000, 0, 999999)
        self.assertEqual(page["limit"], chat.TEXT_PAGE_MAX)
        self.assertEqual(page["count"], chat.TEXT_PAGE_MAX)
        self.assertTrue(page["hasMore"])

    def test_the_envelope_uses_the_same_keys_as_the_hub_catalog(self):
        """键名与 `hubcatalog.search` 同一套：模型学一次够用。

        各写一套的结果是它每换一个工具都要重新猜哪个键表示"还有更多"，
        而猜错的表现是**静默少读一段**——读的人看到的仍然是一份通顺的报告。
        """
        mine = set(chat._page_text("abc", 0, 10))
        theirs = set(hubcatalog.search([], "", "", False, 10, 0)) - {"items"}
        self.assertTrue(theirs <= mine,
                        "分页信封少了目录检索那一套里的键：%s" % (theirs - mine))
        # 多出来的三个都是**有意的**：`text` 是正文本身（那一套是 `items`），
        # `nextOffset` 省掉调用方每次翻页算一次加法（算错就静默跳过一段），
        # `truncated` 是 `hasMore` 的别名——界面与既有用例读的是那个名字。
        self.assertEqual(mine - theirs, {"text", "nextOffset", "truncated"},
                         "信封比那一套多出来的键只能是这三个")


class MessageLayerTestCase(unittest.TestCase):
    """回灌给模型的那条消息：**永远可以被解析**。"""

    def test_a_full_page_survives_the_worst_case_json_escaping(self):
        """一页正文序列化后必须装得进回灌上限。**这条是本文件的核心不变量。**

        这是算出来的关系，不是抄来的数：最坏放大 `_ESCAPE_WORST_CASE` 倍，
        再加信封余量。它一旦破掉，症状不是"少看到一点"，而是回灌给模型的
        JSON 被从中间切开——模型拿到的不是"内容被截断了"，是一段解析不了的
        东西，表现是"这个工具坏了"。

        所以这里用一个**全是换行**的页做最坏输入：`\\n` 转义后每个字符占两位。
        """
        worst = "\n" * chat.TEXT_PAGE_MAX
        text = chat._dump_fitting(
            {"ok": True, "summary": "s" * 200,
             "data": {"text": worst, "hint": "h" * 200, "engagementId": "E-12345678",
                      "target": "10.0.0.1", "jobId": "job-12345678"}},
            chat.TOOL_RESULT_LIMIT)
        self.assertLessEqual(len(text), chat.TOOL_RESULT_LIMIT)
        parsed = json.loads(text)                       # 必须能被解析
        self.assertEqual(len(parsed["data"]["text"]), chat.TEXT_PAGE_MAX)

    def test_an_oversize_result_is_parseable_and_says_what_to_do(self):
        """装不下时丢掉正文、保住"发生了什么"，并给出下一步——**不是半截 JSON**。"""
        text = chat._dump_fitting({"ok": True, "summary": "s",
                                   "data": {"text": "\n" * 100000}}, 500)
        self.assertLessEqual(len(text), 500)
        parsed = json.loads(text)
        self.assertTrue(parsed["truncated"])
        self.assertIn("get_report", parsed["hint"])

    def test_the_message_layer_is_the_only_clip_left(self):
        """`_clip`（给人看的轨迹）与 `_dump_fitting`（给模型看的消息）分工不同。

        前者切一半再加一句"已截断"没问题——那本来就是一段给人读的文本；
        后者必须能被解析。这条用例把两者的差异钉住，免得以后有人"顺手统一"。
        """
        self.assertTrue(chat._dump("x" * 100, 10)
                        .endswith("已截断，完整内容请在界面上查看）"))
        with self.assertRaises(ValueError):
            json.loads(chat._dump({"text": "x" * 100}, 10))
        self.assertIsInstance(json.loads(chat._dump_fitting({"text": "x" * 100}, 10)),
                              dict)


class ReportPagingTestCase(unittest.TestCase):
    """报告：**这一轮报上来的那个现象**。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("longout.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR,
                                         require_executor_auth=False)
        self.eid = build(self.store)
        self.facts = fill(self.store, self.eid, 70)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_a_report_longer_than_one_page_is_readable_to_the_end(self):
        """逐页读完，报告里的**每一条**事实都要出现。

        这就是缺陷 ②③ 的回归用例：此前正文被切两次，读到的不到一半，而
        返回里没有任何一处说"后面还有"。断言"返回了 text"完全测不出它。
        """
        first = chat.execute_tool(self.plane, "get_report",
                                  {"engagementId": self.eid}, self.eid)["data"]
        self.assertEqual(first["offset"], 0)
        self.assertTrue(first["hasMore"], "报告应当超过一页，否则这条用例测不到东西")
        self.assertLess(first["total"], 10 ** 6)

        whole = page_all(self.plane, self.eid)
        self.assertEqual(len(whole), first["total"], "拼起来的长度必须等于 total")
        missing = [fid for fid in self.facts if "`%s`" % fid not in whole]
        self.assertEqual(missing, [], "这些事实的正文一页页读也读不到：%s" % missing[:5])

    def test_the_first_page_alone_is_not_the_whole_report(self):
        """第一页**不等于**全文——否则上面那条用例会因为"一次就读全"而失去意义。"""
        whole = page_all(self.plane, self.eid)
        first = chat._page_text(whole, 0, chat.TEXT_PAGE)
        self.assertTrue(first["hasMore"])
        self.assertNotEqual(first["text"], whole)

    def test_the_report_is_not_windowed_but_the_interface_one_is(self):
        """模型读到的是**完整文档**，界面那个仍然是窗口——两者都如实标注。

        窗口是给渲染用的（图上铺不开）。把它套在"把报告读完"这件事上，
        少掉的那一百条在文档里只剩一行"另有 N 条未在其中展示"，而读的人
        拿不到它们。所以两边**故意不同**，这条用例把两者同时钉住。

        窗口只管**链路一节**：漏洞与资产是交付物，两边都必须是完整集合
        （见 `tests/test_report_window.py`）。
        """
        many = build(self.store, "10.0.0.9")
        fill(self.store, many, store_module.DEFAULT_WINDOW + 20, detail_chars=20)

        model_side = page_all(self.plane, many)
        ui_side = self.store.report_markdown(many, store_module.DEFAULT_WINDOW)
        self.assertGreater(len(model_side), len(ui_side))
        self.assertIn("未在其中展示", ui_side)
        self.assertNotIn("未在其中展示", model_side)
        self.assertIn("「四、漏洞清单」与「五、资产」是完整的", ui_side)

    def test_summary_states_the_total_and_where_the_page_sits(self):
        """摘要必须说出"共多少、这次给了哪一段、还有多少"。

        模型是靠这句话决定要不要接着读的；只说"报告有 9405 字符"而把正文
        悄悄截断，它会把"没读到"当成"报告里只有这些"。
        """
        outcome = chat.execute_tool(self.plane, "get_report",
                                    {"engagementId": self.eid}, self.eid)
        self.assertIn("共", outcome["summary"])
        self.assertIn("接着读", outcome["summary"])

    def test_a_job_report_of_another_engagement_is_refused(self):
        """一对不成立的 (目标, 任务) 要报错，不能照着空集合渲染出**看似正常**的报告。"""
        other = build(self.store, "10.0.0.7")
        job = self.store.create_job("toolkit", "10.0.0.7", other)
        self.store.finish_job(job["id"], "done", {"summary": {}}, output="x")
        outcome = chat.execute_tool(self.plane, "get_report",
                                    {"engagementId": self.eid, "jobId": job["id"]},
                                    self.eid)
        self.assertFalse(outcome["ok"])
        self.assertIn("不属于目标", outcome["summary"])

    def test_a_job_report_can_be_read_by_the_same_tool(self):
        """`jobId` 换的是整篇文档（任务报告），不是"汇总报告过滤一下"。"""
        job = self.store.create_job("toolkit", "10.10.10.250", self.eid)
        self.store.finish_job(job["id"], "done", {"summary": {"adapter": "nmap"}},
                              output="x")
        outcome = chat.execute_tool(self.plane, "get_report",
                                    {"engagementId": self.eid, "jobId": job["id"]},
                                    self.eid)
        self.assertTrue(outcome["ok"])
        self.assertIn(job["id"], outcome["data"]["text"])
        self.assertNotEqual(outcome["data"]["text"],
                            page_all(self.plane, self.eid))


class JobOutputTestCase(unittest.TestCase):
    """原始输出：**缺陷 ①，本轮的根因**。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("jobout.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR,
                                         require_executor_auth=False)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_the_local_executor_stores_the_raw_output(self):
        """本机执行器必须把 `toolOutput` 交给落库，而不是在收尾时丢掉。

        这是本轮的根因：`run_once` 曾经用一个五键字面量构造 `result`，
        `execute_toolkit` 特意保留到 64 KB 的原始输出就在这一步消失——而
        日志、状态页、报告全都正常，只有证据本身不见了。
        """
        fake = {"status": "done", "summary": {"adapter": "nmap", "exitCode": 0},
                "nodes": [], "toolOutput": "[nmap] 端口 445 开放\n" * 40}
        executor = agent_module.LocalExecutor(self.store, name="ut-local",
                                              verbose=False)
        job = self.store.create_job("toolkit", "10.10.10.250")
        with mock.patch.object(agent_module, "execute_job", return_value=fake):
            self.assertTrue(executor.run_once())

        stored = self.store.get_job(job["id"])
        self.assertEqual(stored["toolOutput"], fake["toolOutput"])
        self.assertEqual(stored["summary"]["adapter"], "nmap")

    def test_the_remote_result_path_stores_the_raw_output(self):
        """远程节点回传同样的东西，走的必须是**同一个**落库函数。

        两处各写一份是这次分叉的来源：一个版本记得带、另一个不记得，而两边
        都不报错。
        """
        store = self.store
        eid = build(store, "10.0.0.5")
        job = store.create_job("toolkit", "10.0.0.5", eid)
        store.claim_job(store.ensure_executor("ut-node", ["toolkit"]))
        raw = "SMB 会话建立成功\n" * 30
        ctx = {"params": {"job_id": job["id"]},
               "body": {"status": "done", "summary": {"adapter": "smbclient"},
                        "nodes": [], "toolOutput": raw},
               "query": {}, "headers": {}, "path": "/", "clientAddress": ""}
        status, payload = self.plane.job_result(ctx)
        self.assertEqual(status, 200)
        self.assertEqual(store.get_job(job["id"])["toolOutput"], raw)

    def test_only_one_place_decides_what_goes_into_the_result(self):
        """两个调用点合用一个 `stored_job_result`，且它**不含** `toolOutput`。

        原始输出落的是自己的列（它可能几万字节），摘要里留一个指针就够。
        把这条钉住，是因为"顺手加一行"看起来无害，而后果是每次任务列表都
        拖着几万字节走。
        """
        stored = agent_module.stored_job_result({"summary": {"a": 1}}, {}, "")
        self.assertEqual(sorted(stored), ["agents", "chainError", "error",
                                          "incremental", "summary", "written"])
        self.assertNotIn("toolOutput", stored)

    def test_the_job_list_carries_the_size_but_not_the_body(self):
        """列表说得出"这条有 1 235 字节输出"，但不把那 1 235 字节拖过来。"""
        eid = build(self.store, "10.0.0.6")
        job = self.store.create_job("toolkit", "10.0.0.6", eid)
        self.store.finish_job(job["id"], "done", {"summary": {"adapter": "nmap"}},
                              output="o" * 1235)
        listed = [row for row in self.store.jobs() if row["id"] == job["id"]][0]
        self.assertEqual(listed["toolOutput"], "")
        self.assertEqual(listed["toolOutputBytes"], 1235)
        self.assertEqual(len(self.store.get_job(job["id"])["toolOutput"]), 1235)

    def test_the_list_tool_points_at_the_job_instead_of_dumping_raw_json(self):
        """`list_jobs` 的摘要必须是**已解析的摘要**，不是 `result` 那一列的原始 JSON。

        此前它读的是 `job["result"]`——那是字符串，`isinstance(..., dict)` 恒为假，
        于是走 else 分支把整段 JSON 当摘要发了出去。任务少时只是"摘要有点长"；
        等原始输出落库，它会变成每条几十 KB，直接把整条工具结果撑到装不下，
        而那时的表现是**回灌给模型的 JSON 被切开**。
        """
        eid = build(self.store, "10.0.0.8")
        job = self.store.create_job("toolkit", "10.0.0.8", eid)
        self.store.finish_job(job["id"], "done", {"summary": {"adapter": "nmap"}},
                              output="o" * 5000)
        outcome = chat.execute_tool(self.plane, "list_jobs", {}, eid)
        row = [item for item in outcome["data"]["jobs"] if item["id"] == job["id"]][0]
        self.assertIsInstance(row["summary"], dict)
        self.assertEqual(row["summary"]["adapter"], "nmap")
        self.assertEqual(row["toolOutputBytes"], 5000)
        self.assertIn(job["id"], outcome["data"]["hint"])
        self.assertNotIn("o" * 5000, json.dumps(outcome["data"], ensure_ascii=False))

    def test_get_job_pages_the_output_and_reports_the_job_identity(self):
        """`get_job` 是那条链路的最后一环：事实 → jobId → 原始输出。"""
        eid = build(self.store, "10.0.0.9")
        job = self.store.create_job("toolkit", "10.0.0.9", eid)
        raw = "".join("line-%04d 端口 445 开放\n" % i for i in range(400))
        self.store.finish_job(job["id"], "done", {"summary": {"adapter": "smbclient"}},
                              output=raw)

        parts, offset, guard = [], 0, 0
        while True:
            guard += 1
            self.assertLess(guard, 100)
            data = chat.execute_tool(self.plane, "get_job",
                                     {"jobId": job["id"], "offset": offset},
                                     eid)["data"]
            self.assertEqual(data["jobId"], job["id"])
            self.assertEqual(data["engagementId"], eid)
            self.assertEqual(data["kindLabel"], chat.JOB_KIND_LABEL["toolkit"])
            parts.append(data["text"])
            if not data["hasMore"]:
                break
            offset = data["nextOffset"]
        self.assertEqual("".join(parts), raw)

    def test_get_job_says_so_when_there_is_no_output(self):
        """没有输出要说成"没有"，而不是让空串看起来像"读到了空白"。"""
        eid = build(self.store, "10.0.0.10")
        job = self.store.create_job("scan", "/srv/app", eid)
        self.store.finish_job(job["id"], "done", {"summary": {}})
        outcome = chat.execute_tool(self.plane, "get_job", {"jobId": job["id"]}, eid)
        self.assertIn("没有原始输出", outcome["summary"])
        self.assertEqual(outcome["data"]["total"], 0)

    def test_a_missing_job_id_is_refused_with_a_next_step(self):
        """空 `jobId` 要给出"去哪儿拿"，而不是一句"参数错误"。"""
        outcome = chat.execute_tool(self.plane, "get_job", {}, "")
        self.assertFalse(outcome["ok"])
        self.assertIn("list_jobs", outcome["summary"])


class ChainNodesTestCase(unittest.TestCase):
    """节点正文：**缺陷 ④**。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("nodes.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR,
                                         require_executor_auth=False)
        self.eid = build(self.store)
        self.facts = fill(self.store, self.eid, 12)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_every_node_body_is_readable(self):
        """`fact-N` 的正文要读得到——这就是"fact-70 我读不到它的正文"那条。"""
        data = chat.execute_tool(self.plane, "get_chain_nodes",
                                 {"engagementId": self.eid, "kind": "fact",
                                  "limit": 200}, self.eid)["data"]
        self.assertEqual(data["total"], len(self.facts))
        self.assertFalse(data["hasMore"])
        by_id = {node["id"]: node for node in data["nodes"]}
        for node_id in self.facts:
            self.assertIn(node_id, by_id)
            self.assertIn("f%03d" % self.facts.index(node_id),
                          by_id[node_id].get("detail") or "")

    def test_a_single_node_is_read_by_id(self):
        """给 `nodeId` 只回那一条——这是最常用的一种读法（图上点了一条记录）。"""
        wanted = self.facts[7]
        data = chat.execute_tool(self.plane, "get_chain_nodes",
                                 {"engagementId": self.eid, "nodeId": wanted},
                                 self.eid)["data"]
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["nodes"][0]["id"], wanted)

    def test_a_prefixed_node_id_is_accepted(self):
        """合并视图里的 `E-xxxx/fact-3` 与报告里的 `fact-3` 指的是同一条。

        只认一种，另一种的报错会看起来像"这条记录不存在"——而它明明在图上。
        """
        data = chat.execute_tool(self.plane, "get_chain_nodes",
                                 {"engagementId": self.eid,
                                  "nodeId": "%s/%s" % (self.eid, self.facts[3])},
                                 self.eid)["data"]
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["nodes"][0]["id"], self.facts[3])

    def test_the_page_unit_is_the_node_not_the_character(self):
        """`offset` 是第几个节点。跨节点拼字符没有意义——读的人要的是"第 N 条"。"""
        first = chat.execute_tool(self.plane, "get_chain_nodes",
                                  {"engagementId": self.eid, "kind": "fact",
                                   "limit": 5}, self.eid)["data"]
        second = chat.execute_tool(self.plane, "get_chain_nodes",
                                   {"engagementId": self.eid, "kind": "fact",
                                    "limit": 5, "offset": 5}, self.eid)["data"]
        self.assertEqual(len(first["nodes"]), 5)
        self.assertEqual(first["nextOffset"], 5)
        self.assertTrue(first["hasMore"])
        self.assertEqual([n["id"] for n in first["nodes"]],
                         self.facts[0:5])
        self.assertEqual([n["id"] for n in second["nodes"]], self.facts[5:10])

    def test_the_overview_says_where_the_bodies_are(self):
        """概览只给标题，所以它必须写出正文去哪儿读。

        不写的话，"概览里没有"会被读成"链路里没有"——而事实是同一批数据，
        只是换了一个工具。
        """
        data = chat.execute_tool(self.plane, "get_chain_overview",
                                 {"engagementId": self.eid}, self.eid)["data"]
        self.assertIn("get_chain_nodes", data["hint"])
        self.assertNotIn("detail", data["recentNodes"][0])


class RegistrationTestCase(unittest.TestCase):
    """新工具接得上：分级、注册、描述里写了怎么分页。"""

    def test_the_new_readers_are_readonly(self):
        """三条都是读已经落库的证据，不碰目标、不改变控制面状态。"""
        for name in ("get_job", "get_chain_nodes"):
            self.assertEqual(policy.TOOL_LEVEL[name], "readonly")
            self.assertIn(name, chat.HANDLERS)

    def test_every_paged_tool_documents_its_paging(self):
        """描述里必须出现 `offset` 与 `hasMore`。

        没有这两句，模型只会读第一页，然后把"还没读到"当成"只有这些"——
        这正是这一轮故障在**使用者**那一侧的样子。
        """
        for spec in chat.TOOLS:
            if spec["name"] not in ("get_report", "get_job"):
                continue
            parameters = set((spec["parameters"] or {}).get("properties") or {})
            self.assertIn("offset", parameters, spec["name"])
            self.assertIn("limit", parameters, spec["name"])
            self.assertIn("hasMore", spec["description"], spec["name"])

    def test_get_chain_nodes_offers_the_id_and_the_kind(self):
        spec = [item for item in chat.TOOLS if item["name"] == "get_chain_nodes"][0]
        parameters = set((spec["parameters"] or {}).get("properties") or {})
        self.assertTrue({"nodeId", "kind", "offset", "limit"} <= parameters)

    def test_the_tool_descriptions_do_not_promise_a_clipped_report(self):
        """`get_report` 的描述不能再写着"会被截断"——那是旧行为，现在是分页。"""
        spec = [item for item in chat.TOOLS if item["name"] == "get_report"][0]
        self.assertNotIn("会被截断", spec["description"])


if __name__ == "__main__":
    unittest.main()
