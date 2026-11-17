# -*- coding: utf-8 -*-
"""并发回归测试。

这个文件存在的唯一原因是一个已经发生过的真实缺陷：`Store` 早期用**一条**
sqlite 连接（`check_same_thread=False`）被 `ThreadingHTTPServer` 的多个工作线程共享，
于是并发请求会交错彼此的事务，表现为随机 500：
`InterfaceError` / `cannot commit - no transaction is active` /
`SystemError: error return without exception set`，甚至出现"刚写入的配置查不到"的误报 404。

这类缺陷的特点是**单请求测试永远测不出来**，只有并发压力下才现形。
所以这里不测业务语义，只盯一件事：并发下不能有 5xx，也不能丢写。
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd.store import Store                                 # noqa: E402
from tests import support                                        # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")
CONCURRENCY = 16
ROUNDS = 10


class ConcurrentReadTestCase(unittest.TestCase):
    """并发读：这是真实前端启动时就会发生的场景（health + executors 同时发）。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("concurrent.db")
        self.httpd, self.client = support.start(self.store, WEB_DIR)
        for index in range(5):
            self.client.post("/api/v1/engagements", {"target": "并发目标%d" % index})
        self.client.post("/api/v1/executors/enroll", {"name": "并发节点"},
                         {"X-Enroll-Token": "unused"}) \
            if False else self.store.register_executor("并发节点", ["scan"])

    def tearDown(self):
        support.stop(self.httpd, self.store, self.dir)

    def _hammer(self, paths):
        """并发打一组接口，返回失败列表。"""
        failures = []
        lock = threading.Lock()

        def one(index):
            path = paths[index % len(paths)]
            status, payload = self.client.get(path)
            if status != 200:
                with lock:
                    failures.append((path, status, payload))

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            list(pool.map(one, range(CONCURRENCY * ROUNDS)))
        return failures

    def test_concurrent_reads_never_fail(self):
        paths = ["/api/v1/health", "/api/v1/executors", "/api/v1/engagements",
                 "/api/v1/setup", "/api/v1/jobs", "/api/v1/baselines",
                 "/api/v1/models", "/api/v1/users"]
        failures = self._hammer(paths)
        self.assertEqual(failures, [],
                         "并发读出现失败（很可能是连接被多线程共享）：%s"
                         % failures[:3])

    def test_health_and_executors_together(self):
        """前端启动时的组合请求：health 与 executors 并发。"""
        failures = []
        lock = threading.Lock()

        def one(index):
            path = "/api/v1/health" if index % 2 else "/api/v1/executors"
            status, payload = self.client.get(path)
            if status != 200:
                with lock:
                    failures.append((path, status, payload))

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            list(pool.map(one, range(CONCURRENCY * ROUNDS)))
        self.assertEqual(failures, [], "并发 health/executors 失败：%s" % failures[:3])

    def test_concurrent_writes_are_not_lost(self):
        """并发写：不能丢记录，也不能出现"刚写入就查不到"。"""
        created = []
        lock = threading.Lock()

        def one(index):
            status, payload = self.client.post(
                "/api/v1/engagements", {"target": "写入目标%d" % index})
            self.assertEqual(status, 201, payload)
            with lock:
                created.append(payload["id"])

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(one, range(24)))

        self.assertEqual(len(created), 24)
        self.assertEqual(len(set(created)), 24, "出现了重复 id")
        listed = {e["id"] for e in self.client.get("/api/v1/engagements")[1]
                  ["engagements"]}
        for eid in created:
            self.assertIn(eid, listed, "并发写入的记录丢失：%s" % eid)

    def test_concurrent_model_writes_and_reads(self):
        """并发改配置 + 读配置：这是前端「设置」页最密集的路径。"""
        profile_ids = []
        lock = threading.Lock()

        def create(index):
            status, payload = self.client.post("/api/v1/models", {
                "name": "并发配置%d" % index, "provider": "ollama",
                "baseUrl": "http://127.0.0.1:11434", "model": "m%d" % index,
                "apiKeySource": "none"})
            self.assertEqual(status, 201, payload)
            with lock:
                profile_ids.append(payload["profile"]["id"])

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(create, range(12)))
        self.assertEqual(len(profile_ids), 12)

        def read_or_activate(index):
            if index % 3 == 0:
                status, payload = self.client.post(
                    "/api/v1/models/%s/activate" % profile_ids[index % 12])
                self.assertEqual(status, 200, payload)
            else:
                status, _ = self.client.get("/api/v1/models")
                self.assertEqual(status, 200)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(read_or_activate, range(48)))

        profiles = self.client.get("/api/v1/models")[1]["profiles"]
        self.assertEqual(len(profiles), 12, "并发写入后配置数量不对")

    def test_node_heartbeat_and_pull_are_thread_safe(self):
        """节点侧的并发心跳与领任务（真实部署里多个节点会同时打）。"""
        tokens = []
        for index in range(6):
            info = self.store.register_executor("节点%d" % index, ["scan"])
            tokens.append(info)

        results = []
        lock = threading.Lock()

        def one(index):
            info = tokens[index % len(tokens)]
            if index % 2:
                status, _ = self.client.request(
                    "POST", "/api/v1/executors/%s/heartbeat" % info["executorId"],
                    {"load": 0}, {"Authorization": "Bearer %s" % info["token"]})
            else:
                status, _ = self.client.request(
                    "GET", "/api/v1/jobs/pull?executorId=%s" % info["executorId"],
                    headers={"Authorization": "Bearer %s" % info["token"]})
            if status not in (200, 204):
                with lock:
                    results.append((index, status))

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            list(pool.map(one, range(CONCURRENCY * ROUNDS)))
        self.assertEqual(results, [], "并发心跳/领任务失败：%s" % results[:3])


class StoreLockingTestCase(unittest.TestCase):
    """存储层加锁：公开方法都受保护，且不会重复加锁。"""

    def test_public_methods_are_serialized(self):
        locked = [name for name in dir(Store)
                  if getattr(getattr(Store, name), "_store_serialized", False)]
        self.assertGreaterEqual(len(locked), 40,
                                "公开方法应统一加锁，只有 %d 个" % len(locked))
        for name in ("add_node", "create_engagement", "claim_job",
                     "finish_job", "save_profile", "create_session"):
            self.assertIn(name, locked, "%s 未加锁" % name)

    def test_serialization_is_idempotent(self):
        """重复装配不应层层套壳（每次调用都会多一层锁开销）。"""
        from pentestd.store import _serialize_public_methods
        before = Store.create_engagement
        _serialize_public_methods(Store)
        self.assertIs(Store.create_engagement, before, "重复装配改变了方法对象")

    def test_nested_calls_do_not_deadlock(self):
        """可重入：方法内部调用另一个公开方法不能自锁死。"""
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            store = Store(os.path.join(directory.name, "t.db"))
            eid = store.create_engagement("目标")["id"]      # 内部调用 get_engagement_meta
            self.assertTrue(store.list_engagements())          # 内部调用 get_engagement_meta + windowed_view
            self.assertIn("goal-1", [n["id"] for n in store.list_nodes(eid, "goal")])
            store.close()
        finally:
            directory.cleanup()

    def test_schema_visible_from_every_thread(self):
        """多线程通过**公开方法**访问时，结构与数据都可见。

        注意这里刻意调 `store.stats()` 而不是 `store.conn.execute(...)`：
        直接用 `conn` 会绕过 Store 的加锁包装，四个线程就会在**同一条连接**上
        交错使用游标——先 `execute` 的线程可能在 `fetchone()` 之前被另一个线程
        重置游标，于是拿到 None。那正是本用例要防的模式，测试自己不该制造它。
        （这个写法曾经真的偶发 `TypeError: 'NoneType' object is not subscriptable`，
        一度被误当成产品缺陷去追。）
        """
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            store = Store(os.path.join(directory.name, "t.db"))
            store.create_engagement("目标")
            ok = []

            def one():
                ok.append(store.stats()["engagements"] >= 1)
                ok.append(len(store.list_engagements()) >= 1)

            threads = [threading.Thread(target=one) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(ok), 8)
            self.assertTrue(all(ok), "其它线程应通过公开方法看到已建好的数据")
            store.close()
        finally:
            directory.cleanup()

    def test_direct_conn_access_is_not_thread_safe_by_design(self):
        """把边界写清楚：`conn` 是受保护方法的实现细节，不是给并发用的入口。

        这条用例断言的是"我们**要求**调用方走方法"——如果哪天有人把并发代码
        直接写在 `conn` 上，就会退回到加锁之前那种随机故障。
        """
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            store = Store(os.path.join(directory.name, "t.db"))
            self.assertIs(store.conn, store.conn, "同一线程内应复用同一条连接")
            store.close()
            with self.assertRaises(Exception) as ctx:
                store.conn.execute("SELECT 1")
            self.assertIn("已关闭", str(ctx.exception),
                          "关闭后应给出可读错误，而不是 sqlite 的 ProgrammingError")
        finally:
            directory.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
