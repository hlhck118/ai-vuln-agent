# -*- coding: utf-8 -*-
"""任务列表的删除与清空。

两颗按钮看着只是界面便利，实际要钉住三件容易被忽略的事：

* **删除的是调度记录，不是证据。** 链路节点、边、基线都按 engagement 组织，
  删掉"谁在什么时候跑过它"不该顺手把跑出来的结论也删掉。这里的用例把
  "任务没了、链路节点还在"写成断言——否则下次有人重构存储时，一句
  `ON DELETE CASCADE` 就能把证据链悄悄清空，而且不会有任何测试响；
* **删除执行中的任务是有代价的**，代价必须被说出来。删除不中止已经跑起来的
  那次执行，只是它的结果无处可去（控制面回传时按 404 拒绝）。响应里的
  `discarded` 与界面上的措辞都建立在这条事实上；
* **清空的默认范围是"已结束"**。把 pending 一起清掉等于悄悄取消别人排队中的
  任务，所以那必须是显式点名 `all` 的选择，而不是默认值。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agent, engine                                 # noqa: E402
from tests import support                                          # noqa: E402

WEB = os.path.join(ROOT, "web")


class JobDeleteApiTestCase(unittest.TestCase):

    def setUp(self):
        self.directory, self.store = support.temporary_store("job-admin.db")
        # addCleanup 是后进先出，因此"最后注册的最先执行"：停机顺序必须是
        # 先停服务、再关连接、最后删临时目录。反过来的话，工作线程会用到
        # 已经关闭的 SQLite 连接。
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.httpd, self.client = support.start(self.store)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    # ------------------------------------------------------------ 辅助

    def new_job(self, kind: str = "probe", target: str = "127.0.0.1"):
        status, payload = self.client.post(
            "/api/v1/jobs", {"kind": kind, "target": target})
        self.assertEqual(status, 201, payload)
        return payload["jobId"], payload["engagementId"]

    def make_running(self, job_id: str) -> None:
        """借存储层把任务推进到 running。

        必须**建一条就立刻领一条**：`claim_job` 取的是队列里最早的那条 pending，
        攒着一起领会领错对象，测出来的是别的东西。
        """
        claimed = self.store.claim_job("exec-test")
        self.assertIsNotNone(claimed, "队列里没有可领取的任务")
        self.assertEqual(claimed["id"], job_id)
        self.assertEqual(claimed["status"], "running")

    def ids(self):
        return [job["id"] for job in self.store.jobs()]

    # ------------------------------------------------------------ 单条删除

    def test_delete_removes_the_job(self):
        job_id, _ = self.new_job()
        status, payload = self.client.post("/api/v1/jobs/%s/delete" % job_id, {})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["discarded"], "待执行的任务没在跑，不该报结果被丢弃")
        self.assertIsNone(self.store.get_job(job_id))
        self.assertEqual(self.ids(), [])

    def test_delete_missing_job_is_404(self):
        status, payload = self.client.post("/api/v1/jobs/job-nope/delete", {})
        self.assertEqual(status, 404)
        self.assertIn("job-nope", payload.get("error", ""))

    def test_delete_running_job_is_allowed_and_reported(self):
        """执行中的任务可以删（最该删的恰恰是卡住的僵尸任务），但必须说明代价。"""
        job_id, _ = self.new_job()
        self.make_running(job_id)
        status, payload = self.client.post("/api/v1/jobs/%s/delete" % job_id, {})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "running")
        self.assertTrue(payload["discarded"], "删除执行中的任务必须如实报告结果会被丢弃")
        self.assertIsNone(self.store.get_job(job_id))

    def test_delete_keeps_engagement_evidence(self):
        """核心语义：删任务不动证据。"""
        job_id, eid = self.new_job()
        written = engine.write_chain(self.store, eid, [
            {"kind": "intent", "localRef": "intent:probe",
             "payload": {"title": "探测 127.0.0.1", "detail": "这次任务产出的意图",
                         "goalId": engine.GOAL_ID}},
            {"kind": "fact",
             "payload": {"intentId": {"$ref": "intent:probe"}, "kind": "info",
                         "detail": "主机存活", "confidence": 0.9}},
        ])
        self.assertEqual(written["intent"], 1)
        self.assertEqual(written["fact"], 1)

        self.assertEqual(
            self.client.post("/api/v1/jobs/%s/delete" % job_id, {})[0], 200)

        self.assertIsNone(self.store.get_job(job_id))
        counts = self.store.get_engagement_meta(eid)["counts"]
        self.assertEqual(counts["intents"], 1, "任务删了，链路证据不该跟着消失")
        self.assertEqual(counts["facts"], 1)
        # 两条边都该还在：目标 → 意图（spawns）、意图 → 事实（yields）
        self.assertEqual(counts["edges"], 2)

    # ------------------------------------------------------------ 批量清空

    def prepare_four_jobs(self):
        """一条执行中 + 一条已完成 + 一条失败 + 一条待执行。"""
        running_id, _ = self.new_job()
        self.make_running(running_id)
        done_id, _ = self.new_job()
        self.make_running(done_id)
        self.store.finish_job(done_id, "done", {"summary": {"kind": "probe"}})
        failed_id, _ = self.new_job()
        self.make_running(failed_id)
        self.store.finish_job(failed_id, "failed", {"error": "boom"})
        queued_id, _ = self.new_job()
        return running_id, done_id, failed_id, queued_id

    def test_clear_removes_only_finished_jobs(self):
        running_id, _, _, queued_id = self.prepare_four_jobs()
        status, payload = self.client.post("/api/v1/jobs/clear", {})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["deleted"], 2)
        self.assertEqual(payload["kept"], 2)
        self.assertEqual(sorted(self.ids()), sorted([running_id, queued_id]))

    def test_clear_reports_what_it_kept(self):
        """只说"已清空"，用户会以为队列空了，而真正还排着的任务还在。"""
        self.prepare_four_jobs()
        self.assertEqual(
            self.client.post("/api/v1/jobs/clear", {"all": True})[0], 200)
        self.assertEqual(self.store.jobs(), [])

    def test_clear_all_needs_to_be_asked_for(self):
        """默认不动 pending 与 running。

        连点两次默认清空（第二次已无终态任务可清）之后，排队中和执行中的那两条
        仍须原样在——"清空"不该因为被点过一次就悄悄扩大到它们身上。
        """
        running_id, _, _, queued_id = self.prepare_four_jobs()
        self.client.post("/api/v1/jobs/clear", {})
        status, second = self.client.post("/api/v1/jobs/clear", {})
        self.assertEqual(status, 200, second)
        self.assertEqual(second["deleted"], 0)
        self.assertEqual(second["kept"], 2)
        self.assertEqual(sorted(self.ids()), sorted([running_id, queued_id]))

    # ------------------------------------------------------------ 权限

    def test_operator_can_delete_one_but_not_clear_all(self):
        """单条删除与创建对称（能下发就能撤回）；批量清空是跨用户的动作。"""
        job_id, _ = self.new_job()
        self.assertEqual(self.client.post("/api/v1/users", {
            "username": "op-clear", "password": "Operator-Pass1!",
            "role": "operator"})[0], 201)
        self.client.logout()
        self.assertEqual(self.client.login("op-clear", "Operator-Pass1!")[0], 200)

        self.assertEqual(
            self.client.post("/api/v1/jobs/%s/delete" % job_id, {})[0], 200)

        self.new_job()
        status, _ = self.client.post("/api/v1/jobs/clear", {})
        self.assertEqual(status, 403, "清空会删掉别人的历史，只有管理员能做")
        self.assertEqual(len(self.ids()), 1, "被拒之后队列必须原样不动")

    def test_anonymous_cannot_delete(self):
        job_id, _ = self.new_job()
        self.client.logout()
        self.assertEqual(
            self.client.post("/api/v1/jobs/%s/delete" % job_id, {})[0], 401)
        self.assertIsNotNone(self.store.get_job(job_id))

    def test_delete_requires_csrf(self):
        job_id, _ = self.new_job()
        status, _ = self.client.request("POST", "/api/v1/jobs/%s/delete" % job_id,
                                        {}, with_csrf=False)
        self.assertEqual(status, 403)
        self.assertIsNotNone(self.store.get_job(job_id))


class DeletedJobResultTestCase(unittest.TestCase):
    """执行节点：任务在执行期间被删除，结果不该写进链路。

    远程节点的这条路径由控制面挡住（`job_result` 查不到任务就返回 404），
    但本机节点直接持有 Store、没有那次 HTTP 往返——不在这里拦，就是
    "任务删掉了，证据却照写进来"。
    """

    def setUp(self):
        self.directory, self.store = support.temporary_store("job-delete-node.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)

    def stub_execution(self, delete_during_run: bool):
        """替换 `execute_job`：产出**合法可写入**的一个意图节点。

        用合法数据是关键——伪造一个写不进去的节点，"没写进链路"就成了必然结果，
        测出来的是别的东西（这也是下面那条反证用例存在的理由）。
        """
        original = agent.execute_job

        def hijack(job, workdir, on_progress=None):
            if delete_during_run:                 # 执行期间用户在列表上点了「删除」
                self.store.delete_job(job["id"])
            return {"status": "done", "summary": {"findings": 1},
                    "nodes": [{"kind": "intent",
                               "payload": {"title": "本次执行的产出",
                                           "detail": "应当被丢弃",
                                           "goalId": engine.GOAL_ID}}],
                    "agents": []}

        agent.execute_job = hijack
        self.addCleanup(setattr, agent, "execute_job", original)

    def run_one(self, delete_during_run: bool):
        eid = self.store.create_engagement("127.0.0.1", "删除保护", "测试授权")["id"]
        job = self.store.create_job("scan", "127.0.0.1", eid)
        executor = agent.LocalExecutor(self.store, name="local",
                                       work_root=self.directory.name, verbose=False)
        self.stub_execution(delete_during_run)
        self.assertTrue(executor.run_once(), "run_once 应报告这次确实干了活")
        return job["id"], eid

    def test_result_of_a_deleted_job_is_discarded(self):
        job_id, eid = self.run_one(delete_during_run=True)
        self.assertIsNone(self.store.get_job(job_id))
        counts = self.store.get_engagement_meta(eid)["counts"]
        self.assertEqual(counts["intents"], 0,
                         "任务已被删除，这次执行的结果不该写进链路")

    def test_result_is_written_when_the_job_still_exists(self):
        """反证：同样的伪造结果，任务还在时必须真的写进链路。

        没有这一条，上面那条用例可能因为"伪造的节点数据本来就写不进去"而通过。
        """
        job_id, eid = self.run_one(delete_during_run=False)
        self.assertIsNotNone(self.store.get_job(job_id))
        counts = self.store.get_engagement_meta(eid)["counts"]
        self.assertEqual(counts["intents"], 1)


class JobDeleteUiTestCase(unittest.TestCase):
    """界面上必须真的存在这两颗按钮，并且接到对应的接口上。"""

    def setUp(self):
        with open(os.path.join(WEB, "app.js"), encoding="utf-8") as handle:
            self.js = handle.read()
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as handle:
            self.html = handle.read()

    def test_clear_button_exists_in_the_markup(self):
        self.assertIn('id="jobs-clear"', self.html)
        self.assertIn('id="jobs-message"', self.html)

    def test_row_delete_button_changes_status_aware_wording(self):
        self.assertIn("data-job-id", self.js)
        # 执行中的任务要额外说明代价，否则"删了"会被当成"停了"
        self.assertIn("本次执行仍会跑完，但结果会被丢弃", self.js)

    def test_buttons_call_the_router_paths(self):
        self.assertIn("jobPath(id, '/delete')", self.js)
        self.assertIn("'/jobs/clear'", self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
