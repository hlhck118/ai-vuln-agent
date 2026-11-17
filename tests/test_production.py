# -*- coding: utf-8 -*-
"""生产化能力测试：鉴权、mTLS、schema 迁移、增量任务、容器入口。

这组用例对应"从能跑到能上生产"的那一段：

  * 节点鉴权——令牌缺失、错误、跨节点冒用都必须被拒绝，且库里不存明文；
  * mTLS——真签证书、真起 TLS 服务、真做双向认证（本机有 openssl 时执行）；
  * schema 迁移——v1 库能自动升到 v2，历史数据不丢；
  * 增量任务——基线存在控制面，第二个节点也能增量，变更文件仍被检出；
  * 容器入口——用容器里同一条命令在本机起节点，跑通完整任务链路。
"""
import json
import os
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agent as agent_module                       # noqa: E402
from pentestd import engine, pki                                 # noqa: E402
from pentestd.server import ControlPlane, serve_background       # noqa: E402
from pentestd.store import SCHEMA_VERSION, Store, hash_token     # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")
DEPLOY_DIR = os.path.join(ROOT, "deploy")
PYTHON = sys.executable


def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SchemaMigrationTestCase(unittest.TestCase):
    def test_v1_database_migrates_and_hashes_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            # 造一个 v1 库：executors 存明文 token
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE executors (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL,
                    capabilities TEXT DEFAULT '[]', status TEXT DEFAULT 'online',
                    load INTEGER DEFAULT 0, registered_at REAL, last_seen REAL);
            """)
            conn.execute("INSERT INTO executors(id,name,token,capabilities,status,"
                         "load,registered_at,last_seen) VALUES(?,?,?,?,?,?,?,?)",
                         ("exec-old", "legacy", "plain-token-abc", "[]", "online",
                          0, 1.0, 1.0))
            conn.execute("PRAGMA user_version=1")
            conn.commit()
            conn.close()

            store = Store(path)                       # 打开即迁移
            try:
                row = store.conn.execute(
                    "SELECT token_hash FROM executors WHERE id='exec-old'").fetchone()
                self.assertIsNotNone(row, "历史节点记录必须保留")
                self.assertEqual(row["token_hash"], hash_token("plain-token-abc"),
                                 "明文令牌应被就地哈希化")
                self.assertTrue(store.verify_executor_token("exec-old",
                                                            "plain-token-abc"))
                version = store.conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, SCHEMA_VERSION)
            finally:
                store.close()

    def test_newer_schema_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "future.db")
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA user_version=999")
            conn.commit()
            conn.close()
            with self.assertRaises(RuntimeError) as ctx:
                Store(path)
            self.assertIn("高于本程序支持", str(ctx.exception))


class AuthTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(os.path.join(cls.tmp.name, "auth.db"))
        cls.httpd, _ = serve_background(cls.store, WEB_DIR, "127.0.0.1", 0,
                                        enroll_token="enroll-secret")
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.close()
        cls.tmp.cleanup()

    def _post(self, path, payload, headers=None):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"),
            headers=headers or {"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_register_requires_enroll_token(self):
        status, payload = self._post("/api/v1/executors/register", {"name": "x"})
        self.assertEqual(status, 403)
        self.assertIn("X-Enroll-Token", payload["error"])

    def test_register_with_wrong_enroll_token(self):
        status, _ = self._post("/api/v1/executors/register", {"name": "x"},
                               headers={"Content-Type": "application/json",
                                        "X-Enroll-Token": "wrong"})
        self.assertEqual(status, 403)

    def test_register_with_correct_enroll_token(self):
        status, payload = self._post(
            "/api/v1/executors/register", {"name": "x"},
            headers={"Content-Type": "application/json",
                     "X-Enroll-Token": "enroll-secret"})
        self.assertEqual(status, 201)
        self.assertIn("token", payload)
        self.assertIn("仅此一次", payload.get("note", ""))

    def test_full_node_lifecycle_with_token(self):
        _, info = self._post("/api/v1/executors/register", {"name": "life"},
                             headers={"Content-Type": "application/json",
                                      "X-Enroll-Token": "enroll-secret"})
        token = info["token"]
        status, _ = self._post("/api/v1/executors/%s/heartbeat" % info["executorId"],
                               {"load": 0},
                               headers={"Content-Type": "application/json",
                                        "Authorization": "Bearer %s" % token})
        self.assertEqual(status, 200)

    def test_health_reports_auth_configuration(self):
        with urllib.request.urlopen(self.base + "/api/v1/health", timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(payload["auth"]["executorToken"])
        self.assertTrue(payload["auth"]["enrollToken"])
        self.assertFalse(payload["auth"]["tls"])


class ControlPlaneAuthOffTestCase(unittest.TestCase):
    """开发模式：显式关闭鉴权后接口应可用（并有明确记录）。"""

    def test_disabled_auth_allows_anonymous_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "dev.db"))
            httpd, _ = serve_background(store, WEB_DIR, "127.0.0.1", 0,
                                        require_executor_auth=False)
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            try:
                plane_store = store
                info = plane_store.register_executor("dev-node", ["scan"])
                request = urllib.request.Request(
                    base + "/api/v1/executors/%s/heartbeat" % info["executorId"],
                    data=b'{"load":0}', method="POST",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    self.assertEqual(response.status, 200)
            finally:
                httpd.shutdown()
                httpd.server_close()
                store.close()


@unittest.skipUnless(pki.openssl_available(), "本机没有 openssl，跳过证书签发与 mTLS")
class PkiAndMtlsTestCase(unittest.TestCase):
    def test_issue_list_revoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = pki.init_ca(tmp, "unit-test-ca")
            self.assertTrue(os.path.exists(info["caCert"]))
            with self.assertRaises(pki.PkiError):
                pki.init_ca(tmp, "again")            # 不覆盖已有 CA

            issued = pki.issue(tmp, "node-x", san="127.0.0.1,localhost")
            self.assertTrue(os.path.exists(issued["cert"]))
            self.assertIn("node-x", pki.list_issued(tmp))
            ok, detail = pki.describe(tmp, "node-x")
            self.assertTrue(ok)
            self.assertIn("CN", detail)
            with self.assertRaises(pki.PkiError):
                pki.issue(tmp, "node-x")             # 不重复签发

            removed = pki.revoke(tmp, "node-x")
            self.assertGreaterEqual(len(removed), 2)
            self.assertNotIn("node-x", pki.list_issued(tmp))

    def test_mtls_requires_client_certificate(self):
        """真起一个要求客户端证书的服务：带证书能连，不带证书连不上。"""
        with tempfile.TemporaryDirectory() as tmp:
            pki.init_ca(tmp, "mtls-ca")
            server_cert = pki.issue(tmp, "server", san="127.0.0.1,localhost")
            client_cert = pki.issue(tmp, "client-a", san="127.0.0.1")

            store = Store(os.path.join(tmp, "tls.db"))
            port = _free_port()
            httpd, _ = serve_background(
                store, WEB_DIR, "127.0.0.1", port,
                tls_cert=server_cert["cert"], tls_key=server_cert["key"],
                tls_ca=os.path.join(tmp, "ca.crt"), require_client_cert=True)
            url = "https://127.0.0.1:%d/api/v1/health" % port
            try:
                # ① 带客户端证书：应当成功
                context = ssl.create_default_context(
                    cafile=os.path.join(tmp, "ca.crt"))
                context.load_cert_chain(client_cert["cert"], client_cert["key"])
                with urllib.request.urlopen(url, context=context, timeout=10) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(payload["status"], "ok")
                self.assertTrue(payload["auth"]["clientCert"])

                # ② 不带客户端证书：服务端在握手阶段就应拒绝
                plain = ssl.create_default_context(cafile=os.path.join(tmp, "ca.crt"))
                with self.assertRaises((ssl.SSLError, urllib.error.URLError, OSError)):
                    urllib.request.urlopen(url, context=plain, timeout=10)
            finally:
                httpd.shutdown()
                httpd.server_close()
                store.close()

    def test_node_client_uses_certificate(self):
        """执行节点客户端带证书时能连上要求 mTLS 的控制面。"""
        with tempfile.TemporaryDirectory() as tmp:
            pki.init_ca(tmp, "node-ca")
            server_cert = pki.issue(tmp, "server2", san="127.0.0.1,localhost")
            client_cert = pki.issue(tmp, "worker", san="127.0.0.1")
            store = Store(os.path.join(tmp, "tls2.db"))
            port = _free_port()
            httpd, _ = serve_background(
                store, WEB_DIR, "127.0.0.1", port,
                tls_cert=server_cert["cert"], tls_key=server_cert["key"],
                tls_ca=os.path.join(tmp, "ca.crt"), require_client_cert=True)
            try:
                client = agent_module.ControlClient(
                    "https://127.0.0.1:%d" % port,
                    ca_cert=os.path.join(tmp, "ca.crt"),
                    client_cert=client_cert["cert"], client_key=client_cert["key"])
                status, payload = client.health()
                self.assertEqual(status, 200)
                self.assertEqual(payload["status"], "ok")
            finally:
                httpd.shutdown()
                httpd.server_close()
                store.close()


@unittest.skipUnless(engine.locate_engine(), "未找到漏洞发掘引擎（vulnagent）")
class IncrementalJobTestCase(unittest.TestCase):
    """增量任务：基线存控制面，因此换节点也能增量。"""

    VULNERABLE = ("import subprocess\n\n\n"
                  "def run(text):\n"
                  "    return subprocess.run('echo ' + text, shell=True,\n"
                  "                          capture_output=True, text=True).stdout\n")
    SAFE = ("import subprocess\n\n\n"
            "def run(text):\n"
            "    return subprocess.run(['echo', text], shell=False,\n"
            "                          capture_output=True, text=True).stdout\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.project = os.path.join(self.root, "app")
        self.patched = os.path.join(self.root, "app_patched")
        os.makedirs(self.project)
        os.makedirs(self.patched)
        self._write("alpha.py", self.VULNERABLE)
        self._write("beta.py", "def helper():\n    return 1\n")
        self._write("alpha.py", self.SAFE, root=self.patched)
        self._write("beta.py", "def helper():\n    return 1\n", root=self.patched)
        self.store = Store(os.path.join(self.root, "inc.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _write(self, name, source, root=None):
        with open(os.path.join(root or self.project, name), "w",
                  encoding="utf-8") as handle:
            handle.write(source)

    def _run_job(self, incremental=True, executor_name="node"):
        eid = self.store.create_engagement(self.project, "增量测试", "测试授权")["id"]
        job = self.store.create_job("scan", self.project, eid, self.patched,
                                    {"incremental": incremental})
        executor = agent_module.LocalExecutor(self.store, name=executor_name,
                                              work_root=self.root, verbose=False)
        executor.run_once()
        return self.store.get_job(job["id"]), eid

    def test_first_run_builds_baseline_then_second_run_skips(self):
        first, _ = self._run_job()
        self.assertEqual(first["status"], "done")
        baseline = self.store.get_baseline(self.project)
        self.assertIsNotNone(baseline, "首次增量扫描后应保存基线")
        self.assertGreaterEqual(len(baseline["files"]), 2)
        first_summary = json.loads(first["result"])["summary"]
        self.assertGreaterEqual(first_summary["findings"], 1,
                                "首次全量扫描应检出缺陷")

        second, _ = self._run_job(executor_name="node-2")
        summary = json.loads(second["result"])["summary"]
        self.assertTrue(summary["incremental"])
        self.assertEqual(summary["changedFiles"], 0, "无变更时变更文件数应为 0")
        self.assertEqual(summary["findings"], 0)

    def test_changed_file_is_audited_by_a_different_node(self):
        self._run_job(executor_name="node-1")
        # 改动 alpha.py（内容变化即可触发增量识别）
        self._write("alpha.py", self.VULNERABLE + "# touched by developer\n")
        job, eid = self._run_job(executor_name="node-2")
        summary = json.loads(job["result"])["summary"]
        self.assertEqual(summary["changedFiles"], 1)
        self.assertGreaterEqual(summary["findings"], 1, "变更文件中的缺陷必须被检出")
        counts = self.store.get_engagement_meta(eid)["counts"]
        self.assertGreaterEqual(counts["findings"], 1)
        self.assertGreater(counts["intents"], 0, "链路里应记录本次增量意图")

    def test_baseline_is_stored_on_control_plane(self):
        self._run_job()
        summary = self.store.baseline_summary()
        self.assertTrue(any(item["target"] == self.project for item in summary))
        self.assertGreaterEqual(summary[0]["files"], 2)




if __name__ == "__main__":
    unittest.main(verbosity=2)
