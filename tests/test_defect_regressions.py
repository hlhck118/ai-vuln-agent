# -*- coding: utf-8 -*-
"""第 47 轮报上来的六个平台缺陷里，**在磁盘上仍然成立**的那几个的守卫。

| 编号 | 缺陷 | 守它的用例 |
|---|---|---|
| ① | `start_exploit` 省略 `templates` 直接报错，与工具描述「省略则全部运行」相反 | `ExploitTemplateDefaultTestCase` |
| ② | 长输出读不全（报告 cut 在 fact-33） | **已在 `test_chat_paging.py`** |
| ③ | `list_jobs` 升序取前 N，最新的落在截断区 | `JobListWindowTestCase` |
| ④ | 中文 Windows 上 nmap 的本地化输出触发 `UnicodeEncodeError('gbk')`，任务误判 failed | `ChildOutputTestCase` |
| ⑤ | 派活覆写目标的 `target` / `objective` | **已在 `test_engagement_merge.py`** |
| ⑥ | 缺 Npcap 时 `smb-enum-*` 静默无输出，退出码却是 0 | `ToolDegradationTestCase` |

**② 与 ⑤ 为什么不在这里再写一遍。** 它们的守卫长在各自的主场（分页在
`test_chat_paging.py`，目标身份在 `test_engagement_merge.py`）。同一件事写两份
断言，改一处就要改两处，而漏改的那一份会在重构后继续绿着——那比没有更坏。

这里的测试全部**离线**：不解压、不下载、不连网、不依赖本机装了哪个工具。
"""
import os
import re
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agent as agent_module                      # noqa: E402
from pentestd import chat                                       # noqa: E402
from pentestd import exploit                                    # noqa: E402
from pentestd import toolkit                                    # noqa: E402


# ============================================================ ① 省略 = 全部

class ExploitTemplateDefaultTestCase(unittest.TestCase):
    """① 省略 `templates` = 跑全部，与工具描述里那句逐字一致。"""

    def test_omitting_templates_runs_every_builtin_template(self):
        chosen = exploit.choose_templates(None)
        self.assertEqual(len(exploit.builtin_templates()), len(chosen))
        self.assertTrue(chosen)

    def test_an_empty_list_also_means_all(self):
        """模型发一个空数组与完全不发这个键，对使用者是同一件事。

        只把 `None` 当"全部"而让 `[]` 报错，等于同一个陷阱换个形状再来一次。
        """
        self.assertEqual(len(exploit.choose_templates(None)),
                         len(exploit.choose_templates([])))

    def test_naming_templates_still_selects_exactly_those(self):
        chosen = exploit.choose_templates(["forced-browsing", "auth-path-bypass"])
        self.assertEqual(["forced-browsing", "auth-path-bypass"],
                         [t["id"] for t in chosen])

    def test_the_choice_never_hands_out_the_module_registry(self):
        """两次取到的必须是**不同的对象**。

        `Runner` 会读到模板；别名一旦被就地改写，改动会留在整个进程里，而症状
        是"同一个模板第二次跑就不一样了"——那种缺陷查起来毫无抓手。
        """
        self.assertIsNot(exploit.choose_templates(None)[0],
                         exploit.choose_templates(None)[0])
        self.assertIsNot(exploit.choose_templates(["forced-browsing"])[0],
                         exploit.choose_templates(["forced-browsing"])[0])

    def test_an_unknown_template_name_still_fails_loudly(self):
        """放宽缺省值不等于放宽校验：点错名字仍然要当场报错。"""
        with self.assertRaises((exploit.ExploitError, KeyError)):
            exploit.choose_templates(["no-such-template"])


# ============================================================ ③ 列表窗口从最新数

class _FakeStore(object):
    def __init__(self, jobs):
        self._jobs = jobs

    def jobs(self, status=None):
        rows = self._jobs
        if status:
            rows = [j for j in rows if j["status"] == status]
        return list(rows)


class _FakePlane(object):
    def __init__(self, jobs):
        self.store = _FakeStore(jobs)


def _jobs(count, status="done"):
    """`store.jobs()` 的形状：**升序**（最早的在最前面）。"""
    return [{"id": "job-%02d" % i, "status": status, "kind": "probe",
             "target": "10.0.0.%d" % i, "engagementId": "E-1",
             "summary": {}, "error": "", "chainError": "",
             "toolOutputBytes": 0, "createdAt": 1000 + i}
            for i in range(count)]


class JobListWindowTestCase(unittest.TestCase):
    """③ 窗口从**最新**那条往回数，不是从头往后切。

    `store.jobs()` 是 `ORDER BY created_at` 升序（报告里读起来是时间顺序，那个
    顺序不能动）。所以"取前 N 条"与"给最近 N 条"是两件事，而此前写的是前者：
    任务多于上限时被切掉的**恰好是最新的那几条**，也就是"查进展"要的那几条。
    """

    def test_the_newest_jobs_are_the_ones_that_survive(self):
        total = chat.LIST_LIMIT + 5
        out = chat._tool_list_jobs(_FakePlane(_jobs(total)), {}, "", None)
        ids = [job["id"] for job in out["data"]["jobs"]]
        self.assertEqual(chat.LIST_LIMIT, len(ids))
        self.assertIn("job-%02d" % (total - 1), ids, "最新的任务反而被切掉了")
        self.assertNotIn("job-00", ids, "该切掉的是最早的那几条")
        self.assertEqual(total, out["data"]["total"])
        self.assertTrue(out["data"]["truncated"])
        self.assertTrue(out["data"]["hasMore"])
        self.assertEqual(chat.LIST_LIMIT, out["data"]["nextOffset"])

    def test_under_the_limit_nothing_is_marked_as_cut(self):
        out = chat._tool_list_jobs(_FakePlane(_jobs(3)), {}, "", None)
        self.assertEqual(3, out["data"]["count"])
        self.assertFalse(out["data"]["truncated"])
        self.assertFalse(out["data"]["hasMore"])
        self.assertIsNone(out["data"]["nextOffset"])

    def test_paging_backwards_reaches_the_oldest_and_then_stops(self):
        """一路往回翻必须**每条都碰到过、且不重复**。"""
        total = chat.LIST_LIMIT + 5
        seen, offset, guard = [], 0, 0
        while True:
            guard += 1
            self.assertLess(guard, 20, "翻页没有终点")
            data = chat._tool_list_jobs(_FakePlane(_jobs(total)),
                                        {"offset": offset}, "", None)["data"]
            seen = [job["id"] for job in data["jobs"]] + seen
            if not data["hasMore"]:
                break
            offset = data["nextOffset"]
        self.assertEqual(["job-%02d" % i for i in range(total)], seen)

    def test_a_size_that_is_not_a_number_falls_back_instead_of_failing(self):
        out = chat._tool_list_jobs(_FakePlane(_jobs(3)),
                                   {"limit": "大一点", "offset": "从后面数"},
                                   "", None)
        self.assertEqual(3, out["data"]["count"])
        self.assertEqual(0, out["data"]["offset"])


# ============================================================ ④ 输出按自己的编码解

class ChildOutputTestCase(unittest.TestCase):
    """④ 子进程输出按**它自己的编码**解，而不是一律 UTF-8。"""

    # 实测取自本机 nmap（中文 Windows）：它把时区名按控制台代码页打了出去。
    NMAP_TAIL = (b"Starting Nmap 7.92 ( https://nmap.org ) at 2026-10-15 01:35 "
                 b"\xcc\xa8\xb1\xb1\xb1\xea\xd7\xbc\xca\xb1\xbc\xe4\r\n")

    def test_this_sample_really_is_invalid_utf8(self):
        """前提断言：这段字节**必须**是解不通 UTF-8 的。

        否则下面那条会在解 UTF-8 成功的情况下也绿着——而它要证明的正是"解不通
        的时候会退回代码页"。
        """
        with self.assertRaises(UnicodeDecodeError):
            self.NMAP_TAIL.decode("utf-8")

    def test_console_codepage_output_is_decoded_not_mangled(self):
        text = toolkit._decode_child_output(self.NMAP_TAIL)
        self.assertIn("台北标准时间", text)
        self.assertNotIn("\ufffd", text)
        text.encode("gbk")          # 编不进 GBK 就会在这里炸——那正是误判 failed 的原因

    def test_utf8_output_is_left_alone(self):
        self.assertEqual("台北标准时间",
                         toolkit._decode_child_output("台北标准时间".encode("utf-8")))

    def test_ascii_output_is_unaffected(self):
        self.assertEqual("Nmap done\n", toolkit._decode_child_output(b"Nmap done\n"))

    def test_a_failing_progress_callback_does_not_fail_the_call(self):
        """进度回调抛异常**不算调用失败**。

        实测的链条：输出里有一个字符打不进控制台代码页 → 回调里的
        `print(..., flush=True)` 抛 `UnicodeEncodeError` → 异常穿过 `run()` 被
        上层当成"工具调用失败" → 一次退出码 0 的成功扫描被记成 failed，而输出
        反而因为 `%r` 进了 error 字段。失败的明明是打印。
        """
        def boom(_message):
            raise UnicodeEncodeError("gbk", "\ufffd", 0, 1,
                                     "illegal multibyte sequence")

        with self.assertRaises(UnicodeEncodeError):
            boom("x")                   # 前提：这个回调确实会抛

        notes = []
        toolkit._notify(boom, "x", notes)
        self.assertEqual(1, len(notes))
        self.assertIn("进度", notes[0])
        self.assertIn("UnicodeEncodeError", notes[0], "要说清是哪种异常")

    def test_a_missing_callback_is_fine(self):
        notes = []
        toolkit._notify(None, "x", notes)
        self.assertEqual([], notes)


# ============================================================ ⑥ 退出码 0 也可能降级

class ToolDegradationTestCase(unittest.TestCase):
    """⑥ 适配器要能从输出里认出"这次调用被降级了"。"""

    NPCAP_WARNING = ("WARNING: Could not import all necessary Npcap functions. "
                     "You may need to upgrade to the latest version from "
                     "https://npcap.org. Resorting to connect() mode -- "
                     "Nmap may not function completely")

    def _nmap(self):
        return [a for a in toolkit.ADAPTERS if a["id"] == "nmap"][0]

    def test_the_npcap_warning_is_recognised(self):
        notes = toolkit._degradations(self._nmap(), self.NPCAP_WARNING)
        self.assertEqual(1, len(notes))
        self.assertIn("Npcap", notes[0])
        # 点名哪些脚本会静默失效——不说的话，"脚本那一节是空的"会被读成
        # "脚本没发现问题"，而那是 blind 的那一半。
        self.assertIn("smb-enum", notes[0])

    def test_clean_output_produces_nothing(self):
        self.assertEqual([], toolkit._degradations(self._nmap(), "Nmap done\n"))

    def test_every_declared_marker_is_a_usable_regex_with_a_message(self):
        """声明表是数据，写错一条不该在运行时才发现——更不该静默跳过。"""
        for adapter in toolkit.ADAPTERS:
            for pattern, message in adapter.get("degradations") or ():
                re.compile(pattern)
                self.assertTrue(message, "%s 的降级标记没有说明" % adapter["id"])

    def _run_with(self, **overrides):
        fake = {"adapter": "nmap", "label": "nmap", "class": "toolkit", "argv": [],
                "exitCode": 0, "timedOut": False, "truncated": False,
                "output": "", "bytes": 0, "elapsedMs": 1, "degradations": []}
        fake.update(overrides)
        job = {"id": "job-x", "kind": "toolkit", "target": "10.0.0.5",
               "payload": {"adapter": "nmap", "grant": {}}}
        with mock.patch.object(agent_module.toolkit, "run", return_value=fake):
            return agent_module.execute_toolkit(job, "", lambda _m: None)

    def test_tool_degradations_reach_the_job_summary(self):
        result = self._run_with(degradations=["本机没有 Npcap，nmap 已退回 connect() 模式"])
        self.assertIn("Npcap", " ".join(result["summary"]["degraded"]))

    def test_truncated_output_is_still_reported(self):
        """改动不该把原有的那条降级说明挤掉。"""
        result = self._run_with(truncated=True, bytes=999)
        degraded = result["summary"]["degraded"]
        self.assertTrue(any("截断" in item for item in degraded), degraded)

    def test_a_clean_run_reports_nothing(self):
        self.assertEqual([], self._run_with()["summary"]["degraded"])


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
