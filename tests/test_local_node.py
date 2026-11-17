# -*- coding: utf-8 -*-
"""本机执行节点（单机形态）的验证。

这一组钉住的是一类**"一切看起来正常，却没有任何东西在干活"**的故障：控制面起来了、
界面打得开、任务也下发成功，队列里的任务却永远停在 pending。根因不在任务，而在
"执行面到底有没有进程在跑"，所以断言必须落在可观测的副作用上：

* 节点在**空转**时也报活——否则注册时写下的 `status='online'` 再没人续上，90 秒后
  就被判为离线；界面上一个明明正在干活的节点显示成掉线，比根本没有节点更值得怀疑；
* 重启后复用同一条节点记录——否则 `executors` 表随重启增长，堆出一串**真的曾经
  在线过**的"离线节点"，看的人分不清"以前跑过"和"现在掉线了"；
* 上次被中断的任务被显式关闭——否则一条"执行中"看起来像进度问题，实际永远不会动，
  反而把真正排队中的任务衬得像"还没轮到"；
* 端到端：任务真的从队列里被领走并跑完，这是"任务无法执行"的直接反证。
"""
import importlib.util
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agent                                      # noqa: E402
from pentestd.models import now                                 # noqa: E402
from tests import support                                       # noqa: E402
from tests.test_probe import FakeHttp                           # noqa: E402

# 与 Store.list_executors 的默认阈值保持一致：超时无心跳即判离线。
OFFLINE_AFTER = 90.0


def load_start_module():
    """按路径加载 deploy/start.py（deploy 目录不是包，不能直接 import）。"""
    path = os.path.join(ROOT, "deploy", "start.py")
    spec = importlib.util.spec_from_file_location("deploy_start", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalNodeTestCase(unittest.TestCase):

    def setUp(self):
        self.directory, self.store = support.temporary_store("local-node.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)

    # ------------------------------------------------------------ 辅助

    def online(self, executor_id: str) -> bool:
        for item in self.store.list_executors(offline_after=OFFLINE_AFTER):
            if item["id"] == executor_id:
                return item["online"]
        return False

    def set_last_seen(self, executor_id: str, value: float) -> None:
        """直接改表：必须借用 Store 自己的锁。

        公开方法都被 `serialized` 包着（见 `store._serialize_public_methods`），
        而这里绕过方法直接写。不加锁就会和后台心跳线程的事务交错，症状是随机的
        `OperationalError: not an error`——看着像产品缺陷，实际是测试自己制造的
        事务竞争。`_lock` 是可重入的，借用它是安全的。
        """
        with self.store._lock:
            self.store.conn.execute("UPDATE executors SET last_seen=? WHERE id=?",
                                    (value, executor_id))
            self.store.conn.commit()

    def make_stale(self, executor_id: str) -> None:
        """把 last_seen 回拨，模拟"注册之后再没有过心跳"。"""
        self.set_last_seen(executor_id, now() - OFFLINE_AFTER - 10)

    def wait_for_job(self, job_id: str, timeout: float = 15.0):
        deadline = time.time() + timeout
        job = self.store.get_job(job_id)
        while time.time() < deadline:
            job = self.store.get_job(job_id)
            if job["status"] in ("done", "failed"):
                return job
            time.sleep(0.1)
        self.fail("任务在 %.0fs 内没有走到终态：%s"
                  % (timeout, json.dumps(job, ensure_ascii=False)))

    # ------------------------------------------------------------ 在线状态

    def test_heartbeat_brings_a_stale_node_back_online(self):
        executor = agent.LocalExecutor(self.store, name="local", verbose=False)
        self.make_stale(executor.executor_id)
        self.assertFalse(self.online(executor.executor_id), "回拨后应判为离线")

        self.assertTrue(executor.heartbeat(0))
        self.assertTrue(self.online(executor.executor_id), "心跳之后必须重新算在线")

    def test_idle_loop_keeps_reporting_heartbeat(self):
        """空转循环必须报活：否则界面上活着的节点会显示成离线。

        不断言"回拨后立刻是离线"——循环线程随时可能心跳，那是个竞态，测出来的是
        调度运气而不是行为。改为断言最终结果：last_seen 被刷新、状态回到在线。
        去掉心跳这条就会失败，因为 last_seen 会永远停在回拨值上。
        """
        executor, thread = agent.start_local_executor(
            self.store, work_root=self.directory.name, interval=0.1, verbose=False)
        try:
            stale = now() - OFFLINE_AFTER - 10
            self.set_last_seen(executor.executor_id, stale)

            deadline = time.time() + 5.0
            while time.time() < deadline and not self.online(executor.executor_id):
                time.sleep(0.05)
            self.assertTrue(self.online(executor.executor_id),
                            "没有任务可领时也必须持续报活")

            row = self.store.conn.execute(
                "SELECT last_seen FROM executors WHERE id=?",
                (executor.executor_id,)).fetchone()
            self.assertGreater(row["last_seen"], stale, "last_seen 必须被空转循环刷新")
        finally:
            executor.shutdown()
            thread.join(timeout=5.0)

    # ------------------------------------------------------------ 节点身份

    def test_restart_reuses_one_node_record(self):
        """重启不该在 executors 表里堆积僵尸节点。"""
        first = agent.LocalExecutor(self.store, name="local", verbose=False)
        second = agent.LocalExecutor(self.store, name="local", verbose=False)
        self.assertEqual(first.executor_id, second.executor_id)
        self.assertEqual(len(self.store.list_executors()), 1)

    # ------------------------------------------------------------ 中断回收

    def test_orphan_running_job_is_closed_on_restart(self):
        executor = agent.LocalExecutor(self.store, name="local", verbose=False)
        eid = self.store.create_engagement("127.0.0.1", "中断回收", "测试授权")["id"]
        job = self.store.create_job("probe", "127.0.0.1", eid)
        claimed = self.store.claim_job(executor.executor_id)
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["status"], "running")

        # 进程被强杀：任务停在 running，此后没有人会去写它的终态
        agent.LocalExecutor(self.store, name="local", verbose=False)

        recovered = self.store.get_job(job["id"])
        self.assertEqual(recovered["status"], "failed")
        self.assertIn("中断", recovered["error"])

    def test_recovery_leaves_pending_jobs_alone(self):
        executor = agent.LocalExecutor(self.store, name="local", verbose=False)
        eid = self.store.create_engagement("127.0.0.1", "中断回收", "测试授权")["id"]
        first = self.store.create_job("probe", "127.0.0.1", eid)
        second = self.store.create_job("probe", "127.0.0.1", eid)
        self.store.claim_job(executor.executor_id)          # 领走最早那条，留下后一条

        agent.LocalExecutor(self.store, name="local", verbose=False)

        self.assertEqual(self.store.get_job(first["id"])["status"], "failed")
        self.assertEqual(self.store.get_job(second["id"])["status"], "pending",
                         "排队中的任务不该被回收逻辑误伤")

    # ------------------------------------------------------------ 端到端

    def test_local_node_executes_a_queued_job(self):
        """队列真的会动：对"任务无法执行"最直接的反证。"""
        fixture = FakeHttp()
        self.addCleanup(fixture.stop)
        httpd, client = support.start(self.store)
        self.addCleanup(httpd.server_close)
        status, payload = client.post(
            "/api/v1/jobs", {"kind": "probe", "target": "127.0.0.1",
                             "ports": str(fixture.port)})
        self.assertEqual(status, 201)
        self.assertEqual(self.store.get_job(payload["jobId"])["status"], "pending")

        executor, thread = agent.start_local_executor(
            self.store, work_root=self.directory.name, interval=0.2, verbose=False)
        try:
            job = self.wait_for_job(payload["jobId"])
        finally:
            executor.shutdown()
            thread.join(timeout=5.0)
            httpd.shutdown()

        self.assertEqual(job["status"], "done")
        self.assertEqual(job["summary"]["kind"], "probe")
        self.assertEqual(job["summary"]["openPorts"], 1)
        meta = self.store.get_engagement_meta(payload["engagementId"])
        # **根资产与"存活主机"在这台机器上是同一条。** 目标是单个 IP 时，
        # `build_probe_chain_specs` 先写一条根资产（type=ip，value=目标），再为存活
        # 主机写一条（type=ip，value 还是那个 IP，parent 指向根）——两条
        # (type, value) 完全相同，`store.find_asset` 按身份去重之后合成一条。
        # 所以这里是 2（1 个 ip + 1 个 service），不是 3。
        #
        # 断言的重点因此不是"总数不少于 3"，而是**那一条 ip 只有一份**。
        # 与 `test_probe.ProbeJobTestCase` 里那一条同源，两处必须一致——
        # 那边早先按"根 + 主机 + 服务 = 3"写，在去重生效之后就成了一条永远红的用例，
        # 而它红得没有道理：产品行为是对的。
        self.assertEqual(2, meta["counts"]["assets"])
        ips = [n for n in self.store.list_nodes(payload["engagementId"], "asset")
               if n["payload"].get("type") == "ip"]
        self.assertEqual(1, len(ips), "同一个 IP 登记了 %d 条" % len(ips))

    def test_shutdown_stops_the_loop(self):
        executor, thread = agent.start_local_executor(
            self.store, work_root=self.directory.name, interval=0.1, verbose=False)
        self.assertTrue(thread.is_alive(), "启动函数必须真的把节点跑起来")
        executor.shutdown()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())


class DeploymentEntryPointTestCase(unittest.TestCase):
    """两个部署入口的默认行为必须一致。

    只在其中一个入口接上本机节点，"任务永远 pending"这个坑就还留在另一个入口上。
    """

    def test_start_script_defaults_to_local_executor(self):
        args = load_start_module().build_parser().parse_args([])
        self.assertTrue(args.local_executor, "单机部署默认应带本机执行节点")

    def test_start_script_can_disable_local_executor(self):
        args = load_start_module().build_parser().parse_args(["--no-local-executor"])
        self.assertFalse(args.local_executor)

    def test_serve_command_uses_the_same_default(self):
        from pentestd import cli
        args = cli.build_parser().parse_args(["serve"])
        self.assertFalse(args.no_local_executor,
                         "`pentestd serve` 默认也要带本机执行节点")


if __name__ == "__main__":
    unittest.main(verbosity=2)
