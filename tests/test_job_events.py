# -*- coding: utf-8 -*-
"""任务执行过程的**实时**可见性：存储、流式输出、写入口、读接口。

这个能力的每一层都有一个"看起来实现了、其实没有"的失败方式，而它们的症状
都是同一个：**界面上什么也不动**。所以下面的用例都盯着机制，不盯返回值：

* 存储层 —— 游标取时间戳会让流式读取每隔一批**重复一段**；
* 输出层 —— `subprocess.run` 也能"把输出给回调"，只是要等进程退出；
* 写入口 —— 不合批会打爆控制面，只合批会让安静的任务从界面上消失；
* 读接口 —— 终态一到就收尾，最后几行（通常是"失败：…"）永远发不出去。
"""
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pentestd import agent, store as store_module, toolkit          # noqa: E402
from pentestd.server import ControlPlane                            # noqa: E402
from tests import support                                           # noqa: E402

WEB_DIR = support.WEB_DIR
PY = sys.executable


class StoreEventTestCase(unittest.TestCase):
    """`job_events` 的存储语义。不起服务，只碰存储。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("job-events.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)

    def _job(self, job_id="job-e1", status="running"):
        self.store.conn.execute(
            "INSERT INTO jobs(id,kind,target,status,payload,created_at) "
            "VALUES(?,?,?,?,?,?)", (job_id, "toolkit", "127.0.0.1", status,
                                    "{}", 1.0))
        self.store.conn.commit()
        return job_id

    def test_schema_version_covers_the_events_table(self):
        # 下界断言，与 `test_footholds` 那一批同一条理由：精确数字归各自新表的
        # 用例管，写死 17 会让下一个提 schema 的人（哪怕提得完全正确）来改这里，
        # 而改的时候什么都没被证明。
        self.assertGreaterEqual(store_module.SCHEMA_VERSION, 17)
        self.assertTrue(self.store._table_exists("job_events"))

    def test_multiline_input_is_split_and_blank_lines_dropped(self):
        job_id = self._job()
        written = self.store.append_job_events(
            job_id, "[scan]\n\n  [scan] 开放 22/tcp  \n\n[scan] 完成")
        self.assertEqual(written, 3)
        got = self.store.job_events(job_id)
        # 行首的空白**保留**，只去行尾。nmap 那一类输出靠缩进表达层级，
        # 抹掉行首会把一棵树压平成一串看不出关系的话——而"看不出关系"
        # 恰恰是这份日志存在的意义所在。
        self.assertEqual([e["message"] for e in got["events"]],
                         ["[scan]", "  [scan] 开放 22/tcp", "[scan] 完成"])

    def test_a_giant_single_line_is_capped_and_says_so(self):
        job_id = self._job()
        self.store.append_job_events(job_id, "X" * 9000)
        message = self.store.job_events(job_id)["events"][0]["message"]
        self.assertLess(len(message), 9000)
        self.assertIn("已截断", message)

    def test_the_cursor_does_not_repeat_a_batch(self):
        """游标必须是自增主键。

        用 `ts` 当游标时，同一批写入共用同一个时刻，"上一批的最后一行"与
        "这一批的第一行"就分不开——读取端会**重复**一段。而重复的行看起来
        像这条任务真的重跑了一遍，是最容易被误读成严重故障的一种假象。
        """
        job_id = self._job()
        self.store.append_job_events(job_id, "one")
        self.store.append_job_events(job_id, "two")     # 第二批：同一秒内
        first = self.store.job_events(job_id, limit=1)
        self.assertEqual(len(first["events"]), 1)
        self.assertTrue(first["hasMore"])
        rest = self.store.job_events(job_id, after=first["lastId"])
        self.assertEqual([e["message"] for e in rest["events"]], ["two"])
        self.assertFalse(rest["hasMore"])

    def test_ring_limit_drops_the_oldest_and_counts_them(self):
        job_id = self._job()
        over = store_module.JOB_EVENT_LIMIT + 7
        self.store.append_job_events(job_id, "\n".join("line-%d" % i
                                                       for i in range(over)))
        got = self.store.job_events(job_id, limit=store_module.JOB_EVENT_PAGE)
        self.assertEqual(got["dropped"], 7)
        # 留下的必须是**最新的**那些：日志唯一要回答的问题是"现在跑到哪了"。
        tail = self.store.job_events(job_id, after=over)
        self.assertEqual(tail["events"], [])
        self.assertEqual(self.store.get_job(job_id)["eventDropped"], 7)

    def test_deleting_a_job_takes_its_events_with_it(self):
        job_id = self._job()
        self.store.append_job_events(job_id, "a\nb")
        self.store.delete_job(job_id)
        left = self.store.conn.execute(
            "SELECT COUNT(*) FROM job_events").fetchone()[0]
        self.assertEqual(left, 0,
                         "任务删了、日志留着：这张表只增不减，而列表上看不出来")

    def test_clearing_jobs_takes_the_events_too(self):
        for index in range(2):
            job_id = self._job("job-c%d" % index, status="done")
            self.store.append_job_events(job_id, "a\nb")
        self.store.clear_jobs(["done"])
        left = self.store.conn.execute(
            "SELECT COUNT(*) FROM job_events").fetchone()[0]
        self.assertEqual(left, 0)

    def test_a_deleted_job_accepts_no_more_events(self):
        """任务在执行期间被删掉：之后送来的日志必须被拒。

        不拒的话它们会落成**无人认领的行**——界面上没有任何路径读得到，
        而库文件会一直变大。这正是本层要挡的那一类"悄悄增长"。
        """
        job_id = self._job()
        self.store.append_job_events(job_id, "before")
        self.store.delete_job(job_id)
        self.assertEqual(self.store.append_job_events(job_id, "after"), 0)
        left = self.store.conn.execute(
            "SELECT COUNT(*) FROM job_events").fetchone()[0]
        self.assertEqual(left, 0)


class StreamChildTestCase(unittest.TestCase):
    """`toolkit._stream_child`：子进程输出必须**边到边**送出去。"""

    @staticmethod
    def _argv(body):
        return [PY, "-c", body]

    def test_lines_arrive_before_the_process_exits(self):
        """核心断言是**时间**，不是内容。

        只断言"回调收到全文"的话，`subprocess.run` 那种跑完才给的回调同样通过
        ——而它恰恰是这个用例要挡住的实现。
        """
        seen = []
        body = ("import time\n"
                "for i in range(3):\n"
                "    print('chunk-%d' % i, flush=True)\n"
                "    time.sleep(0.4)\n")
        started = time.time()
        result = toolkit._stream_child(self._argv(body), None, 30.0, 1 << 20,
                                       lambda text: seen.append(
                                           (round(time.time() - started, 2), text.strip())),
                                       [], None)
        finished = time.time() - started
        self.assertEqual(len(seen), 3, seen)
        self.assertIn("chunk-0", seen[0][1])
        self.assertIn("chunk-2", seen[-1][1])
        self.assertLess(seen[0][0], finished - 0.5,
                        "第一行是等进程跑完才给的：那就不是实时输出")
        self.assertFalse(result["timed_out"])

    def test_timeout_fires_while_the_child_is_silent(self):
        """读取必须由独立线程做。

        直接在主循环里 `read1`，子进程安静多久就阻塞多久——**超时永远不会触发**。
        这条用"一个 30 秒的沉默子进程"来测，因为它正是那种实现挂住的地方。
        """
        started = time.time()
        result = toolkit._stream_child(self._argv("import time; time.sleep(30)"),
                                       None, 1.2, 1 << 20, None, [], None)
        spent = time.time() - started
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["code"], -1)
        self.assertLess(spent, 6.0, "超时没有被执行：读取把主循环阻塞住了")

    def test_capped_output_still_drains_the_pipe(self):
        """超上限之后必须继续读。

        停下不读，子进程写满管道缓冲区就卡住，最后**表现为超时**——一次正常的
        截断会被报成一次超时，而这两件事的处置完全不同。
        """
        body = "import sys; sys.stdout.write('x' * 400000)"
        result = toolkit._stream_child(self._argv(body), None, 20.0, 4096,
                                       None, [], None)
        self.assertEqual(len(result["raw"]), 4096)
        self.assertEqual(result["total"], 400000)
        self.assertFalse(result["timed_out"])

    def test_streamed_text_is_already_masked(self):
        """流出去的每一段都必须已打码。

        只在最终结果里打码是不够的：日志是**实时**看的，泄露在第一次发送时
        就已经发生了。
        """
        seen = []
        body = ("import time\n"
                "print('token=SUPERSECRETVALUE here', flush=True)\n"
                "time.sleep(0.2)\n")
        toolkit._stream_child(self._argv(body), None, 20.0, 1 << 20,
                              seen.append, [], ["SUPERSECRETVALUE"])
        joined = "".join(seen)
        self.assertNotIn("SUPERSECRETVALUE", joined)
        self.assertIn("***", joined)

    def test_a_tail_without_a_newline_is_not_lost(self):
        seen = []
        toolkit._stream_child(self._argv(
            "import sys; sys.stdout.write('tail-no-newline')"), None, 20.0,
            1 << 20, seen.append, [], None)
        self.assertIn("tail-no-newline", "".join(seen))

    def test_raw_is_the_full_output_prefix(self):
        """`raw` 是"完整输出的前 max_output 字节"，`total` 是完整长度。

        落进结果正文的字符串由 `_decode_child_output(raw)` 得到——同一份输入、
        同一段代码，所以改成流式之后 `result["output"]` 逐字节未变。这条钉的是
        那个不变式，不是解码器的行为（混合编码的整段解码另有其用例）。
        """
        body = "import sys; sys.stdout.buffer.write(b'alpha\\n' * 10)"
        result = toolkit._stream_child(self._argv(body), None, 20.0, 1 << 20,
                                       None, [], None)
        self.assertEqual(result["raw"], b"alpha\n" * 10)
        self.assertEqual(result["total"], 60)
        self.assertEqual(toolkit._decode_child_output(result["raw"]),
                         "alpha\n" * 10)

    def test_the_cap_does_not_change_what_the_body_decodes_to(self):
        """截断时 `raw` 是前缀，`total` 说得出真实体积。"""
        body = "import sys; sys.stdout.write('ab' * 5000)"
        result = toolkit._stream_child(self._argv(body), None, 20.0, 100,
                                       None, [], None)
        self.assertEqual(result["raw"], b"ab" * 50)
        self.assertEqual(result["total"], 10000)


class JobProgressTestCase(unittest.TestCase):
    """`agent.JobProgress`：合批、止损、以及"写不进去不算失败"。"""

    def test_the_first_line_goes_out_immediately(self):
        batches = []
        progress = agent.JobProgress("job-x", lambda jid, lines: batches.append(list(lines)))
        progress("第一条")
        self.assertEqual(batches, [["第一条"]],
                         "首行被合批压住了：安静下来的任务会从界面上消失")

    def test_a_burst_is_batched_into_fewer_writes(self):
        batches = []
        progress = agent.JobProgress("job-x", lambda jid, lines: batches.append(list(lines)),
                                     interval=30.0)
        progress("第一条")
        for index in range(50):
            progress("第 %d 条" % index)
        self.assertEqual(len(batches), 1, "首行之外的突发没有被合批")
        progress.close()
        self.assertEqual(len(batches), 2, "close 必须把尾巴刷出去")

    def test_a_failing_write_raises_nothing_and_says_so_once(self):
        notices = []

        def boom(job_id, lines):
            raise RuntimeError("控制面挂了")

        progress = agent.JobProgress("job-x", boom, echo=notices.append)
        for index in range(5):
            progress("第 %d 条" % index)          # 不许抛
        progress.close()
        failures = [item for item in notices if "写不进控制面" in item]
        self.assertEqual(len(failures), 1,
                         "失败一次之后还在重试：会把一次抖动拖成连接风暴")
        self.assertTrue(progress.broken)

    def test_job_gone_stops_quietly(self):
        """任务被删除是**已知的正常情况**，不该打出一条像网络故障的提示。

        两者的区别不是措辞问题：混在一起会让人去排查网络，而真相只是自己
        刚按了删除。
        """
        notices = []

        def gone(job_id, lines):
            raise agent.JobGone()

        progress = agent.JobProgress("job-x", gone, echo=notices.append)
        progress("第一条")
        progress("第二条")
        self.assertTrue(progress.broken)
        # 控制台**照旧**拿到每一句：它是节点自己的日志，与"入库成不成功"无关。
        self.assertEqual(notices, ["第一条", "第二条"])
        # 但不许出现任何像故障的提示。任务被删除是已知的正常情况。
        self.assertFalse([item for item in notices if "写不进控制面" in item],
                         "删除任务之后，节点日志里出现了像故障的提示")


class JobEventApiTestCase(unittest.TestCase):
    """两个读接口 + 节点写接口，走**真实 HTTP**。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("job-events-api.db")
        self.addCleanup(self.directory.cleanup)
        self.httpd, self.client = support.start(self.store, WEB_DIR)
        self.addCleanup(support.stop, self.httpd, self.store,
                        self.directory)
        registered = self.store.register_executor("node-a", ["toolkit"])
        self.executor_id = registered["executorId"]
        self.token = registered["token"]

    def _claimed_job(self, kind="toolkit", target="127.0.0.1"):
        # 用 toolkit + curl：它正是**输出最多**的那一类任务，而这个能力的
        # 全部意义就在那些任务上。
        status, payload = self.client.post(
            "/api/v1/jobs", {"kind": kind, "target": target, "adapter": "curl"})
        self.assertEqual(status, 201, payload)
        job_id = payload["jobId"]
        self.store.conn.execute(
            "UPDATE jobs SET status='running', claimed_by=? WHERE id=?",
            (self.executor_id, job_id))
        self.store.conn.commit()
        return job_id

    def _node_post(self, path, body):
        return self.client.post(path, body,
                                headers={"Authorization": "Bearer %s" % self.token})

    def test_a_node_pushes_events_and_the_console_reads_them(self):
        job_id = self._claimed_job()
        status, payload = self._node_post(
            "/api/v1/jobs/%s/events" % job_id,
            {"lines": ["[toolkit] 启动 nmap", "[toolkit] 22/tcp open"]})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["written"], 2)
        status, page = self.client.get("/api/v1/jobs/%s/events" % job_id)
        self.assertEqual(status, 200)
        self.assertEqual([e["message"] for e in page["events"]],
                         ["[toolkit] 启动 nmap", "[toolkit] 22/tcp open"])
        self.assertEqual(page["status"], "running")
        self.assertGreater(page["lastId"], 0)

    def test_a_foreign_node_cannot_write_into_someone_elses_job(self):
        """日志直接改变"这次任务做到了哪一步"的判断。

        只按"节点令牌有效"放行的话，任何一台节点都能往别人的任务里注入看起来
        完全正常的日志。
        """
        job_id = self._claimed_job()
        other = self.store.register_executor("node-b", ["toolkit"])
        status, payload = self.client.post(
            "/api/v1/jobs/%s/events" % job_id, {"lines": ["伪造的一行"]},
            headers={"Authorization": "Bearer %s" % other["token"]})
        self.assertEqual(status, 401, payload)
        self.assertEqual(self.store.job_events(job_id)["events"], [])

    def test_events_require_a_node_token(self):
        job_id = self._claimed_job()
        status, _ = self.client.post("/api/v1/jobs/%s/events" % job_id,
                                     {"lines": ["x"]})
        self.assertEqual(status, 401)

    def test_a_deleted_job_refuses_events_with_404(self):
        """404 而不是 200。给 200 会让节点一直送到任务跑完。"""
        job_id = self._claimed_job()
        self.store.delete_job(job_id)
        status, _ = self._node_post("/api/v1/jobs/%s/events" % job_id,
                                    {"lines": ["x"]})
        self.assertEqual(status, 404)

    def test_events_and_stream_are_404_for_an_unknown_job(self):
        status, _ = self.client.get("/api/v1/jobs/job-nope/events")
        self.assertEqual(status, 404)
        status, _ = self.client.get("/api/v1/jobs/job-nope/stream")
        self.assertEqual(status, 404)

    def test_events_endpoint_requires_a_session(self):
        job_id = self._claimed_job()
        # 先登出、再看结果。反过来（先清掉 csrf 再登出）是不行的：登出本身是
        # 写操作、也要 CSRF 头，那样它会被 403 挡下，而会话仍然有效——
        # 于是这条用例会因为"没真的登出"而错误地失败。
        status, _ = self.client.logout()
        self.assertEqual(status, 200)
        status, _ = self.client.get("/api/v1/jobs/%s/events" % job_id)
        self.assertEqual(status, 401)


class JobStreamTestCase(unittest.TestCase):
    """SSE 跟随：真实 socket，读到连接关闭为止。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("job-stream.db")
        self.addCleanup(self.directory.cleanup)
        self.httpd, self.client = support.start(self.store, WEB_DIR)
        self.addCleanup(support.stop, self.httpd, self.store,
                        self.directory)
        registered = self.store.register_executor("node-s", ["toolkit"])
        self.executor_id = registered["executorId"]
        self.token = registered["token"]

    def _claimed_job(self):
        status, payload = self.client.post(
            "/api/v1/jobs", {"kind": "toolkit", "target": "127.0.0.1",
                             "adapter": "curl"})
        self.assertEqual(status, 201, payload)
        job_id = payload["jobId"]
        self.store.conn.execute(
            "UPDATE jobs SET status='running', claimed_by=? WHERE id=?",
            (self.executor_id, job_id))
        self.store.conn.commit()
        return job_id

    def _stream(self, path, timeout=30):
        """读到服务端关闭为止。终态的任务会自己收尾，所以这里不会挂住。"""
        request = urllib.request.Request(self.client.base + path, method="GET")
        with self.client.opener.open(request, timeout=timeout) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream",
                          response.headers.get("Content-Type", ""))
            body = response.read().decode("utf-8", "replace")
        return self._frames(body)

    @staticmethod
    def _frames(body):
        out = []
        for block in body.split("\n\n"):
            block = block.strip()
            if not block or block.startswith(":"):
                continue
            event, data = "", ""
            for line in block.splitlines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = line[6:]
            out.append((event, json.loads(data) if data else {}))
        return out

    def test_the_stream_delivers_logs_and_then_closes_on_terminal(self):
        """终态后必须把剩下的日志吐干净**再**关。

        一看到终态就 return，最后几行（通常正是"失败：…"）永远发不出去——
        而那几行恰恰是打开这个面板要找的东西。
        """
        job_id = self._claimed_job()
        self.store.append_job_events(job_id, "第一行")
        self.store.finish_job(job_id, "failed", {"error": "外部工具调用失败：boom"})
        self.store.append_job_events(job_id, "失败：boom")

        frames = self._stream("/api/v1/jobs/%s/stream" % job_id)
        logs = [data["message"] for event, data in frames if event == "log"]
        ends = [data for event, data in frames if event == "end"]
        self.assertEqual(logs, ["第一行", "失败：boom"], frames)
        self.assertEqual(len(ends), 1, frames)
        self.assertEqual(ends[0]["status"], "failed")

    def test_the_stream_reports_dropped_lines(self):
        """滚掉了开头就必须说出来。

        不说的话，读者会把"日志从中间开始"当成"任务从中间才动"。
        """
        job_id = self._claimed_job()
        self.store.append_job_events(
            job_id, "\n".join("line-%d" % i
                              for i in range(store_module.JOB_EVENT_LIMIT + 3)))
        self.store.finish_job(job_id, "done", {})
        frames = self._stream("/api/v1/jobs/%s/stream" % job_id)
        dropped = [data["dropped"] for event, data in frames if event == "dropped"]
        self.assertTrue(dropped, "滚掉了行数却没有任何提示")
        self.assertEqual(dropped[0], 3)

    def test_a_running_job_emits_a_heartbeat_not_an_end(self):
        """安静的任务必须靠心跳维持连接，而不是让客户端以为它结束了。

        没有注释帧/心跳，中间设备会在空闲超时后掐断连接，而客户端看到的
        只是"日志停在某一行，之后再不动了"。
        """
        job_id = self._claimed_job()
        self.store.append_job_events(job_id, "只有这一行")
        # 用一个极短的跟随时长，让服务端主动收尾，从而结束读取。
        original = sys.modules["pentestd.server"].JOB_STREAM_MAX_SECONDS
        sys.modules["pentestd.server"].JOB_STREAM_MAX_SECONDS = 1.0
        try:
            frames = self._stream("/api/v1/jobs/%s/stream" % job_id)
        finally:
            sys.modules["pentestd.server"].JOB_STREAM_MAX_SECONDS = original
        kinds = [event for event, _ in frames]
        self.assertIn("log", kinds)
        self.assertIn("timeout", kinds, frames)
        self.assertNotIn("end", kinds,
                         "任务还在跑，流却报了终态")


if __name__ == "__main__":
    unittest.main()
