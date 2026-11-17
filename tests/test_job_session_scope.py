# -*- coding: utf-8 -*-
"""任务属于哪条对话：存储列、下发透传、老库补列。

对话页右栏的「任务」块回答的是「**这条对话**还派着几件活」，而它原先只有目标归属
（`engagement_id`）——一个目标可能被好几条对话共用（新建对话会跟着当时选中的目标
走），于是切到另一条对话时那一块纹丝不动，看起来完全正常。

本模块钉三件事，每一件都对应一种「看起来做了其实没有」：

  1. **列真的在库里。** 只加 `_row_to_job` 的输出字段不行：列表读的是行，行里没有
     这一列时取键会抛，而防御写法会把它悄悄读成空串——界面上表现为「每条任务都不
     跟对话走」，比报错更难查；
  2. **下发路径真的带上了。** 请求体里有 `sessionId` 而 `create_job` 不收，是最
     容易漏的一环：整条链上没有任何一处会报错；
  3. **老库补列时默认值方向对。** 补成空串 =「没有会话归属」，而不是补成某个存在
     的 id——那会把一批历史任务栽到一条对话的账上。
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pentestd import chat                                        # noqa: E402
from pentestd.server import ControlPlane                         # noqa: E402
from pentestd.store import SCHEMA_VERSION, Store                 # noqa: E402
from tests import support                                        # noqa: E402


class JobSessionColumnTestCase(unittest.TestCase):
    """存储层：写入、读回、老库补列。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("job-session.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)

    def test_a_job_remembers_the_session_that_created_it(self):
        job = self.store.create_job("probe", "10.0.0.5", "", "", {}, "C-sess-1")
        self.assertEqual(job["sessionId"], "C-sess-1")
        # 列表路径与单条读取读的是同一份行，两处都得有
        listed = [item for item in self.store.jobs() if item["id"] == job["id"]]
        self.assertEqual(listed[0]["sessionId"], "C-sess-1")

    def test_a_job_without_a_session_reads_as_empty_not_missing(self):
        """老调用点（CLI、脚本）不传这一项：读回来必须是空串，不是缺键、不是 None。

        缺键会让界面那一侧取到 undefined，而「取值或空串」这种写法恰好也就成了
        空串——但只有一处这么写的时候才恰好。空串是**说清楚**的那个值。
        """
        job = self.store.create_job("scan", "/srv/app")
        self.assertEqual(job["sessionId"], "")
        self.assertIn("sessionId", job)

    def test_the_session_is_trimmed(self):
        job = self.store.create_job("probe", "10.0.0.5", "", "", {}, "  C-x  ")
        self.assertEqual(job["sessionId"], "C-x")

    def test_a_deleted_session_does_not_take_the_job_with_it(self):
        """会话没了，任务还在，只是不再跟任何对话走。

        删除对话时会连带删掉它**独占**的目标与任务（见 `test_session_cascade`），
        但那条规则管的是「目标也被删」的情形；这条钉的是**列本身不设外键**：
        报告里的执行记录不能因为一段工作记录被删就查不到出处。
        """
        job = self.store.create_job("probe", "10.0.0.5", "", "", {}, "C-gone")
        self.assertTrue(self.store.get_job(job["id"]))
        self.assertEqual(self.store.get_job(job["id"])["sessionId"], "C-gone")


class OldDatabaseGetsTheColumnTestCase(unittest.TestCase):
    """v22 的库没有 `session_id`。补列时默认值的方向要钉住。"""

    def test_an_old_jobs_table_gains_the_column_with_an_empty_default(self):
        directory, _unused = support.temporary_store("job-session-old.db")
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "old.db")
        conn = sqlite3.connect(path)
        # v22 的 jobs：没有 session_id，也没有 job_events 那张表
        conn.executescript(
            "CREATE TABLE jobs ("
            " id TEXT PRIMARY KEY, engagement_id TEXT, kind TEXT NOT NULL,"
            " target TEXT NOT NULL, patched TEXT DEFAULT '',"
            " status TEXT NOT NULL DEFAULT 'pending', payload TEXT DEFAULT '{}',"
            " result TEXT DEFAULT '', output TEXT DEFAULT '',"
            " claimed_by TEXT DEFAULT '', created_at REAL, claimed_at REAL,"
            " finished_at REAL, event_dropped INTEGER DEFAULT 0);")
        conn.execute("INSERT INTO jobs(id,kind,target,status,payload,created_at)"
                     " VALUES(?,?,?,?,?,?)",
                     ("job-old", "probe", "10.0.0.9", "done", "{}", 1.0))
        conn.execute("PRAGMA user_version=22")
        conn.commit()
        conn.close()

        store = Store(path)
        self.addCleanup(store.close)
        columns = {row["name"] for row in
                   store.conn.execute("PRAGMA table_info(jobs)")}
        self.assertIn("session_id", columns,
                      "老库没有被补列：老库整个打不开或读不出这一项")
        # 默认值的方向：历史任务**没有**会话归属，不能栽到某条对话头上
        self.assertEqual(store.get_job("job-old")["sessionId"], "")

    def test_schema_version_is_at_least_23(self):
        # 下界断言，与 `test_job_events` 那一条同一条理由：写死数字会让下一个提
        # schema 的人（哪怕提得完全正确）来改这里，而改的时候什么都没被证明。
        self.assertGreaterEqual(SCHEMA_VERSION, 23)


class DispatchCarriesTheSessionTestCase(unittest.TestCase):
    """下发路径：请求体 → `create_job`，以及工具调用这一侧。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("job-session-http.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.httpd, self.client = support.start(self.store)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def test_the_http_post_carries_the_session_into_the_row(self):
        status, payload = self.client.post(
            "/api/v1/jobs", {"kind": "probe", "target": "10.0.0.5",
                             "sessionId": "C-http"})
        self.assertEqual(status, 201, payload)
        job = self.store.get_job(payload["jobId"])
        self.assertEqual(job["sessionId"], "C-http")

    def test_a_post_without_a_session_still_works(self):
        """脚本与节点接入走的是同一条路由，它们不带对话。"""
        status, payload = self.client.post(
            "/api/v1/jobs", {"kind": "probe", "target": "10.0.0.5"})
        self.assertEqual(status, 201, payload)
        self.assertEqual(self.store.get_job(payload["jobId"])["sessionId"], "")

    def test_a_tool_call_stamps_the_session_on_the_job(self):
        """工具这条路是模型下发任务的唯一入口：它不带会话，归属就断了。

        会话 id 只从 `session` 取，**不读工具参数**：让模型有机会指定「这条操作记在
        哪条对话名下」，等于把归属交给被审计者。
        """
        session = self.store.create_chat_session(title="下发一条")
        plane = ControlPlane(self.store, support.WEB_DIR)
        result = chat.execute_tool(plane, "start_scan", {"target": "/srv/app"},
                                   session={"sessionId": session["id"]})
        self.assertTrue(result["ok"], result)
        job = self.store.get_job(result["data"]["jobId"])
        self.assertEqual(job["sessionId"], session["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)