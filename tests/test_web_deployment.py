# -*- coding: utf-8 -*-
"""Web 端部署的测试。

部署形态是"一个 Python 进程 + 浏览器"，所以这里验证两件事：

  * 控制台接口（部署自检 / 节点接入 / 吊销 / 任务与基线）返回的形状正确；
  * `deploy/start.py` 真能起来并把控制台暴露在指定端口上——
    部署脚本如果只是"看起来能跑"，那和没有部署脚本区别不大。
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import engine                                     # noqa: E402
from pentestd.store import Store                                # noqa: E402
from tests import support                                       # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")
DEPLOY_DIR = os.path.join(ROOT, "deploy")
PYTHON = sys.executable
ENROLL_SECRET = "console-secret"
ENROLL_HEADERS = {"Content-Type": "application/json",
                  "X-Enroll-Token": ENROLL_SECRET}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class WebDeploymentConsoleTestCase(unittest.TestCase):
    """「部署」页签背后的接口。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(os.path.join(cls.tmp.name, "console.db"))
        cls.httpd, cls.client = support.start(cls.store, WEB_DIR,
                                              enroll_token="console-secret")
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.close()
        cls.tmp.cleanup()

    def _get(self, path):
        return self.client.get(path)

    def _post(self, path, payload, headers=None):
        return self.client.post(path, payload, headers)

    # ------------------------------------------------------------ 部署自检
    def test_setup_returns_full_deployment_picture(self):
        status, payload = self._get("/api/v1/setup")
        self.assertEqual(status, 200)
        for key in ("ok", "version", "engine", "web", "database", "listen",
                    "urls", "auth", "counts", "checks", "examples"):
            self.assertIn(key, payload, "部署自检缺少字段：%s" % key)
        self.assertTrue(payload["web"]["ready"])
        self.assertGreater(payload["database"]["schemaVersion"], 0)
        self.assertIn("/api/v1", payload["urls"]["api"])

    def test_checks_cover_the_important_risks(self):
        _, payload = self._get("/api/v1/setup")
        by_id = {check["id"]: check for check in payload["checks"]}
        for key in ("engine", "web", "enroll", "executorAuth", "tls",
                    "uiAuth", "database"):
            self.assertIn(key, by_id, "自检缺少检查项：%s" % key)
            self.assertIn(by_id[key]["level"], ("ok", "warn", "fail"))
            self.assertTrue(by_id[key]["title"])
        # 本次启动配了注册凭据、开了节点鉴权、没开 TLS
        self.assertEqual(by_id["enroll"]["level"], "ok")
        self.assertEqual(by_id["executorAuth"]["level"], "ok")
        self.assertEqual(by_id["tls"]["level"], "warn")
        # 本测试已创建管理员且开启了登录认证 → 该项应为 ok
        self.assertEqual(by_id["uiAuth"]["level"], "ok")
        self.assertIn("model", by_id, "自检应报告当前推理来源")

    def test_checks_warn_when_console_login_disabled(self):
        """关掉登录认证时必须报警告，而不是显示一切正常。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "noauth.db"))
            httpd, _client = support.start(store, WEB_DIR, admin=False,
                                           auth_required=False)
            try:
                request = urllib.request.Request(
                    "http://127.0.0.1:%d/api/v1/setup" % httpd.server_address[1])
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                by_id = {c["id"]: c for c in payload["checks"]}
                self.assertEqual(by_id["uiAuth"]["level"], "warn")
                self.assertIn("no-auth", by_id["uiAuth"]["title"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                store.close()

    def test_counts_and_examples(self):
        _, payload = self._get("/api/v1/setup")
        self.assertGreaterEqual(payload["counts"]["executors"], 0)
        self.assertIn("pendingJobs", payload["counts"])
        if engine.locate_engine():
            self.assertTrue(payload["examples"], "有引擎时应给出可一键填入的示例目标")
            for example in payload["examples"]:
                self.assertTrue(os.path.isdir(example["target"]))

    # ------------------------------------------------------------ 节点接入
    def test_enroll_requires_registration_credential(self):
        """配了注册凭据时，控制台建身份也必须带凭据。

        否则配 --enroll-token 就是自欺欺人：节点上门注册被挡住，
        但从控制台接口照样能凭空造出可领任务的身份。
        """
        status, payload = self._post("/api/v1/executors/enroll", {"name": "sneaky"})
        self.assertEqual(status, 403, "未带注册凭据不应能创建节点身份")
        self.assertIn("X-Enroll-Token", payload["error"])
        status, _ = self._post("/api/v1/executors/enroll", {"name": "sneaky"},
                               headers={"Content-Type": "application/json",
                                        "X-Enroll-Token": "wrong"})
        self.assertEqual(status, 403)

    def test_enroll_creates_identity_and_usable_commands(self):
        status, payload = self._post("/api/v1/executors/enroll", {"name": "console-node"},
                                     headers=ENROLL_HEADERS)
        self.assertEqual(status, 201)
        self.assertIn("executorId", payload)
        self.assertIn("token", payload)
        self.assertIn("只显示这一次", payload["warning"])
        for shell in ("powershell", "bash", "systemd"):
            self.assertIn(shell, payload["commands"])
            command = payload["commands"][shell]
            self.assertIn(payload["token"], command, "%s 命令里应含节点令牌" % shell)
            self.assertIn("--server", command)
            self.assertIn("--executor-id %s" % payload["executorId"], command,
                          "%s 命令必须带节点 id，否则节点会去走需要注册凭据的注册流程"
                          % shell)
        self.assertIn("WantedBy=multi-user.target", payload["commands"]["systemd"])

    def test_enrolled_node_can_be_used_immediately(self):
        """控制台建的身份必须能直接用来领任务，否则接入命令是假的。"""
        _, payload = self._post("/api/v1/executors/enroll", {"name": "instant"},
                                     headers=ENROLL_HEADERS)
        executor_id, token = payload["executorId"], payload["token"]
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer %s" % token}
        request = urllib.request.Request(
            self.base + "/api/v1/executors/%s/heartbeat" % executor_id,
            data=b'{"load":0}', method="POST", headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
        pull = urllib.request.Request(
            self.base + "/api/v1/jobs/pull?executorId=%s" % executor_id,
            headers=headers)
        with urllib.request.urlopen(pull, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}") \
                if response.status == 200 else {}
            # 队列空是 204，有任务则 200 且带 job —— 两种都说明节点能立刻干活
            self.assertIn(response.status, (200, 204))
            if response.status == 200:
                self.assertIn("job", payload)

    def test_enroll_rejects_empty_or_long_name(self):
        status, payload = self._post("/api/v1/executors/enroll", {"name": "  "}, headers=ENROLL_HEADERS)
        self.assertEqual(status, 400)
        self.assertIn("name", payload["error"])
        status, _ = self._post("/api/v1/executors/enroll", {"name": "x" * 65},
                                     headers=ENROLL_HEADERS)
        self.assertEqual(status, 400)

    def test_revoke_removes_identity_and_invalidates_token(self):
        _, payload = self._post("/api/v1/executors/enroll", {"name": "to-revoke"},
                                     headers=ENROLL_HEADERS)
        executor_id, token = payload["executorId"], payload["token"]
        status, result = self._post("/api/v1/executors/%s/revoke" % executor_id, {})
        self.assertEqual(status, 200)
        self.assertTrue(result["revoked"])
        # 令牌随即失效
        request = urllib.request.Request(
            self.base + "/api/v1/executors/%s/heartbeat" % executor_id,
            data=b'{"load":0}', method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer %s" % token})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 401)
        # 重复吊销 → 404
        status, _ = self._post("/api/v1/executors/%s/revoke" % executor_id, {})
        self.assertEqual(status, 404)

    def test_executor_list_exposes_online_flag(self):
        self._post("/api/v1/executors/enroll", {"name": "listed"},
                                     headers=ENROLL_HEADERS)
        _, payload = self._get("/api/v1/executors")
        entry = [e for e in payload["executors"] if e["name"] == "listed"][0]
        self.assertIn("online", entry)
        self.assertIn("registeredAt", entry)
        self.assertIn("lastSeen", entry)

    # ------------------------------------------------------------ 任务与基线
    def test_create_job_from_console_warns_about_unseen_path(self):
        status, payload = self._post("/api/v1/jobs",
                                     {"target": "/definitely/not/here",
                                      "incremental": True,
                                      "objective": "控制台下发"})
        self.assertEqual(status, 201)
        self.assertTrue(payload["incremental"])
        self.assertIn("warning", payload)
        self.assertIn("执行节点", payload["warning"])

    def test_job_detail_and_list_shape(self):
        _, created = self._post("/api/v1/jobs", {"target": os.path.join(ROOT, "web")})
        job_id = created["jobId"]
        status, detail = self._get("/api/v1/jobs/%s" % job_id)
        self.assertEqual(status, 200)
        for key in ("id", "status", "target", "engagementId", "incremental",
                    "summary", "error", "elapsedMs", "createdAt"):
            self.assertIn(key, detail["job"], "任务详情缺少字段：%s" % key)
        _, listing = self._get("/api/v1/jobs")
        self.assertIn(job_id, [j["id"] for j in listing["jobs"]])
        _, filtered = self._get("/api/v1/jobs?status=pending")
        self.assertTrue(all(j["status"] == "pending" for j in filtered["jobs"]))

    def test_unknown_job_returns_404(self):
        status, _payload = self.client.get("/api/v1/jobs/job-nope")
        self.assertEqual(status, 404)

    def test_job_pull_not_shadowed_by_job_detail_route(self):
        """路由顺序：/jobs/pull 必须先于 /jobs/{id} 命中。"""
        _, payload = self._post("/api/v1/executors/enroll", {"name": "route-check"},
                                     headers=ENROLL_HEADERS)
        request = urllib.request.Request(
            self.base + "/api/v1/jobs/pull?executorId=%s" % payload["executorId"],
            headers={"Authorization": "Bearer %s" % payload["token"]})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
                self.assertIn(response.status, (200, 204))
                if response.status == 200:
                    # 走到 /jobs/{id} 的话返回的是 {"job": ...} 但语义不同：
                    # 这里必须带 job.id，且不可能是 "任务不存在"
                    self.assertIn("job", json.loads(body))
        except urllib.error.HTTPError as exc:
            self.fail("领任务接口被 /jobs/{id} 抢了路由：HTTP %d" % exc.code)

    def test_baselines_endpoint(self):
        status, payload = self._get("/api/v1/baselines")
        self.assertEqual(status, 200)
        self.assertIn("baselines", payload)
        self.store.set_baseline("/tmp/demo-target", {"a.py": "hash"}, "E-test")
        _, payload = self._get("/api/v1/baselines")
        entry = [b for b in payload["baselines"] if b["target"] == "/tmp/demo-target"][0]
        self.assertEqual(entry["files"], 1)
        self.assertIn("updatedAt", entry)


class StartScriptTestCase(unittest.TestCase):
    """`deploy/start.py` 是 Web 端部署的入口，必须真能起服务。"""

    def _launch(self, tmp, port, log_path):
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"          # 重定向到文件时也要立刻落盘
        handle = open(log_path, "w", encoding="utf-8")
        process = subprocess.Popen(
            [PYTHON, os.path.join(DEPLOY_DIR, "start.py"),
             "--no-browser", "--port", str(port),
             # **不要自启「DSH 控制台」的后端**：这一条会把启动脚本真的拉起来，
             # 而那个后端是一个真的 DSH 子进程 —— 单测里不该有这样的东西
             # （它会往临时目录里写 dsh-home，并在清理时把目录锁住）。
                          "--db", os.path.join(tmp, "start.db")],
            cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT)
        return process, handle

    def _await_port(self, log_path, timeout=30):
        """从 start.py 的输出里读出它最终监听的端口。

        不靠扫描端口段来猜：扫描会撞上同一台机器上别的服务，把别人的端口
        当成自己的。让启动脚本自己说用了哪个端口，才是确定性的做法。
        """
        pattern = re.compile(r"http://127\.0\.0\.1:(\d+)/")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                    match = pattern.search(handle.read())
                if match:
                    return int(match.group(1))
            time.sleep(0.3)
        return None

    def test_start_script_serves_the_console(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "start.log")
            process, handle = self._launch(tmp, port, log_path)
            try:
                served = self._await_port(log_path)
                self.assertIsNotNone(served, "start.py 未能在 30 秒内报出监听端口")
                self.assertEqual(served, port, "端口空闲时应沿用指定端口")
                client = support.Client("http://127.0.0.1:%d" % served)
                status, state = client.get("/api/v1/auth/state")
                self.assertEqual(status, 200)
                self.assertTrue(state["needsSetup"], "全新实例应要求创建管理员")
                self.assertEqual(client.bootstrap()[0], 201)
                _status, payload = client.get("/api/v1/setup")
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["listen"]["port"], port)
                # 静态界面也必须能打开
                with urllib.request.urlopen("http://127.0.0.1:%d/" % served,
                                            timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("<html", response.read().decode("utf-8").lower())
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                handle.close()

    def test_start_script_avoids_busy_port(self):
        """端口被占用时应自动换一个，而不是直接崩掉。"""
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            busy = blocker.getsockname()[1]
            with tempfile.TemporaryDirectory() as tmp:
                log_path = os.path.join(tmp, "busy.log")
                process, handle = self._launch(tmp, busy, log_path)
                try:
                    served = self._await_port(log_path)
                    self.assertIsNotNone(served, "start.py 未能在 30 秒内报出监听端口")
                    self.assertNotEqual(served, busy, "占用中的端口不该被选中")
                    with open(log_path, "r", encoding="utf-8",
                              errors="replace") as reader:
                        self.assertIn("被占用，改用", reader.read(),
                                      "应当明确提示改用了哪个端口")
                    client = support.Client("http://127.0.0.1:%d" % served)
                    self.assertEqual(client.get("/api/v1/auth/state")[0], 200)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    handle.close()


class StartupGuardTestCase(unittest.TestCase):
    """别人已经起着一个，就别再起第二个——以及 `start.cmd` 的手感契约。

    **这一组存在的理由**：`pick_port` 在端口被占时会顺延到下一个。对"别的程序
    占了 8787"那是对的；对"我们自己已经跑着一个"则是错的，而且错得很安静：

      * 第二个实例会打开**同一个数据库**；
      * 本机执行节点按名字复用同一条 `executors` 记录（`store.ensure_executor`），
        所以两个实例的节点 id 相同；
      * 而节点启动时会回收"自己名下"的在途任务（`store.fail_orphan_jobs`）。

    合起来就是：双击一下启动脚本，正在跑的那次作业变成失败，而两次启动之间
    没有任何一处报错。所以这里照着那条伤害写——不是断言"返回码是 2"就算完，
    而是断言**那条执行中的任务还在执行**。
    """

    @classmethod
    def setUpClass(cls):
        path = os.path.join(DEPLOY_DIR, "start.py")
        spec = importlib.util.spec_from_file_location("deploy_start_under_test", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def _launch(self, tmp, port, log_path, use_launcher=False, launcher=None):
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        handle = open(log_path, "w", encoding="utf-8")
        if launcher or use_launcher:
            # 走**真的**启动器：一键启动那条路上，cmd 与 Python 一起才是交付物。
            # cwd 故意给一个不相干的目录——双击时的 cwd 是随机的，
            # `cd /d "%~dp0"` 就是为那一下存在的。
            name = launcher or "start.cmd"
            argv = ["cmd", "/c", "call", os.path.join(ROOT, name),
                    "--no-browser", "--port", str(port),
                    "--db", os.path.join(tmp, "start.db")]
        else:
            argv = [PYTHON, os.path.join(DEPLOY_DIR, "start.py"),
                    "--no-browser", "--port", str(port),
                    "--db", os.path.join(tmp, "start.db")]
        process = subprocess.Popen(argv, cwd=tmp, env=env, stdout=handle,
                                   stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL)
        return process, handle

    def _await_port(self, log_path, timeout=40):
        pattern = re.compile(r"http://127\.0\.0\.1:(\d+)/")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                    match = pattern.search(handle.read())
                if match:
                    return int(match.group(1))
            time.sleep(0.3)
        return None

    @staticmethod
    def _read(path):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def _finish(self, process, handle):
        """收摊时**连子进程一起**收掉。

        `start.cmd` 起的是 `cmd.exe`，真正的服务是它的子进程 `python.exe`。
        只 terminate 那一个 cmd，python 会活下来继续占着日志文件——于是临时目录
        删不掉，报的还是 `NotADirectoryError`（一个把所有注意力引向错误方向的
        名字）。Windows 上用 `taskkill /T` 按进程树收。
        """
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        else:
            process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        handle.close()
        time.sleep(0.4)          # 让被杀的那一端真的松手，否则临时目录仍删不掉

    # ------------------------------------------------------------ 分类
    def test_probe_instance_says_free_when_nothing_listens(self):
        self.assertEqual(self.module.probe_instance("127.0.0.1", _free_port()), "free")

    def test_probe_instance_says_other_for_a_stranger_on_the_port(self):
        """别人的服务占着端口：照旧顺延端口，这是原有行为。

        方向必须保守——**只在确证是自己时才拒绝**。判错的代价不对称：多起一个
        无害的实例，比拒绝启动一个本该起来的服务轻得多。
        """
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            # 超时给短一点：这个替身接了连接但永远不回话，返回"other"靠的是超时。
            self.assertEqual(
                self.module.probe_instance("127.0.0.1", blocker.getsockname()[1],
                                           timeout=0.4),
                "other")

    def test_probe_instance_says_ours_for_a_real_instance(self):
        directory, store = support.temporary_store("probe-ours.db")
        httpd, _client = support.start(store, WEB_DIR, admin=False)
        port = httpd.server_address[1]
        try:
            self.assertEqual(self.module.probe_instance("127.0.0.1", port), "ours")
        finally:
            support.stop(httpd, store, directory)

    def test_probe_instance_does_not_go_through_a_proxy(self):
        """探测必须绕开系统代理。

        `urllib` 默认读 `http_proxy`，而回环地址走代理会要么连不上、要么问到
        一个不相干的服务器——两种都会让"是不是自己"判错，而判错的后果是
        **对着一个已经在跑的实例又起一个**。
        """
        source = None
        with open(os.path.join(DEPLOY_DIR, "start.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("ProxyHandler({})", source,
                      "探测没绕代理：配了 http_proxy 的机器上会判错")

    def test_lan_addresses_are_candidates_a_phone_could_open(self):
        """横幅里那一列地址只该是本机可访问的 IPv4。

        回环与 169.254 排除掉：前者手机根本到不了，后者是没拿到 DHCP 时的
        自配置地址。取不到地址时返回空列表——只负责展示，不能让启动失败。
        """
        for addr in self.module.lan_addresses():
            self.assertRegex(addr, r"^\d+\.\d+\.\d+\.\d+$", "不是 IPv4：%r" % addr)
            self.assertFalse(addr.startswith("127."), "回环地址手机打不开：%r" % addr)
            self.assertFalse(addr.startswith("169.254."), "自配置地址不可用：%r" % addr)

    def test_the_all_interfaces_banner_names_a_lan_address(self):
        """`--host 0.0.0.0` 时横幅必须把「局域网」那一行拿出来。

        只打 `127.0.0.1` 就是上一版手机连不上的直接原因：使用者看着那行
        去输地址，输对了也到不了。
        """
        source = self._read(os.path.join(DEPLOY_DIR, "start.py"))
        self.assertIn("局域网", source, "横幅里没有给手机用的地址行")
        self.assertIn("lan_addresses()", source)

    # ------------------------------------------------------------ 真的再起一次
    @unittest.skipUnless(os.name == "nt", "start-lan.cmd 是 Windows 启动器")
    def test_the_lan_launcher_binds_all_interfaces(self):
        """`start-lan.cmd` 真的把 `--host 0.0.0.0` 送进了 start.py。

        只测默认参数存在是不够的：壳里少一个 `%PENTEST_DEFAULT_ARGS%`、
        或者它被放在了 `%*` 后面，文件都长得一样好看，而手机那边还是一片
        连不上。这里看的是**启动横幅**——它只在 `--host 0.0.0.0` 时才会
        多出「局域网」那一行。
        """
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "lan.log")
            process, handle = self._launch(tmp, port, log_path, launcher="start-lan.cmd")
            try:
                served = self._await_port(log_path)
                self.assertIsNotNone(served, "start-lan.cmd 未能在 40 秒内报出监听端口")
                text = self._read(log_path)
                self.assertIn("局域网", text,
                              "横幅没报手机能用的地址：那等于又要使用者自己去 ipconfig")
                self.assertIn(":%d/" % served, text,
                              "横幅里的端口不是真正监听的那个（顺延之后就说错了）")
                client = support.Client("http://127.0.0.1:%d" % served)
                self.assertEqual(client.get("/api/v1/auth/state")[0], 200)
            finally:
                self._finish(process, handle)

    @unittest.skipUnless(os.name == "nt", "start.cmd 是 Windows 启动器")
    def test_the_second_start_leaves_running_jobs_alone(self):
        """**这条是整组的重点**：双击第二次，不能把第一次正在跑的任务打断。"""
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "start.db")
            log_path = os.path.join(tmp, "first.log")
            process, handle = self._launch(tmp, port, log_path)
            try:
                served = self._await_port(log_path)
                self.assertIsNotNone(served, "第一个实例没能起来，后面的断言无从谈起")
                client = support.Client("http://127.0.0.1:%d" % served)
                self.assertEqual(client.get("/api/v1/health")[0], 200)

                # 造一条"执行中"的任务，挂在第一个实例的本机节点名下。
                # 直接用 sqlite3 而不经 Store：`Store.__init__` 会跑建表与自愈，
                # 而这里的库正被另一个进程用着——测试不该为了造一条数据去动 schema。
                conn = sqlite3.connect(db_path, timeout=15)
                try:
                    executor_id = conn.execute(
                        "SELECT id FROM executors ORDER BY registered_at LIMIT 1"
                    ).fetchone()[0]
                    conn.execute(
                        "INSERT INTO jobs(id,kind,target,status,payload,claimed_by,"
                        "created_at,claimed_at) VALUES(?,?,?,?,?,?,?,?)",
                        ("job-selftest", "probe", "10.0.0.9", "running", "{}",
                         executor_id, time.time(), time.time()))
                    conn.commit()
                finally:
                    conn.close()

                # 第二次双击
                second_log = os.path.join(tmp, "second.log")
                second, second_handle = self._launch(tmp, served, second_log,
                                                     use_launcher=True)
                try:
                    code = second.wait(timeout=90)
                finally:
                    second_handle.close()

                self.assertEqual(code, self.module.ALREADY_RUNNING,
                                 "第二次启动应当明确拒绝，而不是顺延端口再起一个")
                text = self._read(second_log)
                self.assertIn("已经有一个本平台实例在跑", text)
                self.assertIn("http://127.0.0.1:%d/" % served, text,
                              "拒绝时必须把现有实例的地址给出来")
                # 这一屏是"改了服务端代码之后"的第一现场。**原来说不出下一步**：
                # 它只说"已经有一个在跑"，于是使用者的动作是再双击一次——那又看到
                # 同一屏。实测发生过（改完 `pentestd/agents.py` 又双击启动器，服务端
                # 仍是 65 分钟前那一份，保存照旧报改掉之前的那句错）。所以这里钉住
                # "怎么换成新起的那一份"，而不只是"能不能再起一个"。
                self.assertIn("netstat -ano", text,
                              "拒绝时必须给出停掉那个实例的办法（先查 PID）")
                self.assertIn("taskkill /PID", text,
                              "结束必须按 PID 给：`taskkill /IM python.exe /F` 会连"
                              "同机正在跑测试的 python 一起杀掉")
                # 用横幅的抬头当标记。**别挑 `按 Ctrl+C 停止` 那类句子**：
                # start.cmd 自己也会提示停止方式，拿它当标记会永远为真，
                # 于是这条断言静默地什么都不测。
                self.assertNotIn("（Web 端部署）", text,
                                 "不该出现启动横幅——那说明它真的起来了")

                # **真正的断言**：那条任务还在跑
                conn = sqlite3.connect(db_path, timeout=15)
                try:
                    status = conn.execute(
                        "SELECT status FROM jobs WHERE id='job-selftest'"
                    ).fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(status, "running",
                                 "第二个实例把它名下「执行中」的任务标成失败了——"
                                 "这正是 probe_instance 要挡的那条伤害")
                self.assertEqual(client.get("/api/v1/health")[0], 200,
                                 "第一个实例不该受影响")
            finally:
                self._finish(process, handle)

    @staticmethod
    def _read_stats(path):
        conn = sqlite3.connect(path, timeout=15)
        try:
            return conn.execute("SELECT COUNT(*) FROM executors").fetchone()[0]
        finally:
            conn.close()

    @unittest.skipUnless(os.name == "nt", "start.cmd 是 Windows 启动器")
    def test_the_launcher_starts_a_working_console(self):
        """反过来也要测：`start.cmd` 在端口空闲时**真的**起得来。

        只测"拒绝"会得到一个"永远拒绝"的通过版本——那种脚本两条用例里只过
        一条，而使用者那天双击的是没被测到的那一条。
        """
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "launcher.log")
            process, handle = self._launch(tmp, port, log_path, use_launcher=True)
            try:
                served = self._await_port(log_path)
                self.assertIsNotNone(served, "start.cmd 未能在 40 秒内报出监听端口")
                self.assertEqual(served, port, "端口空闲时应沿用指定端口")
                client = support.Client("http://127.0.0.1:%d" % served)
                self.assertEqual(client.get("/api/v1/auth/state")[0], 200)
                self.assertEqual(client.bootstrap()[0], 201)
                self.assertTrue(client.get("/api/v1/setup")[1]["ok"])
                self.assertIn("启动方式 局域网", self._read(log_path),
                              "启动横幅不见了（现在由 deploy/start.py 打）；"
                              "本机档取消后默认就是局域网")
            finally:
                self._finish(process, handle)


def _deploy_module():
    """把 deploy/start.py 当模块取出来。

    启动器的文案、默认参数、菜单与隧道都已搬进 Python（批处理只剩 ASCII），
    所以「说了什么」这类断言必须看得到模块里的字符串，而不是 grep .cmd。
    """
    path = os.path.join(DEPLOY_DIR, "start.py")
    spec = importlib.util.spec_from_file_location("deploy_start_launcher_copy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class LauncherScriptTestCase(unittest.TestCase):
    """`start.cmd` 的静态契约：这几条每一条都对应一种"能跑但会咬人"。"""

    @classmethod
    def setUpClass(cls):
        cls.path = os.path.join(ROOT, "start.cmd")
        with open(cls.path, "rb") as handle:
            cls.raw = handle.read()
        # ASCII 是这个文件最硬的约束（见 test_the_launcher_source_is_pure_ascii），
        # 解码也按它来：中文带回来的第一天，这里就先炸，而不是等到某份日志里。
        cls.text = cls.raw.decode("ascii")

    def test_the_launcher_exists_at_the_project_root(self):
        self.assertTrue(os.path.isfile(self.path), "一键启动脚本不见了")

    def test_line_endings_are_crlf(self):
        """cmd 的 `goto` / `call :label` 在纯 LF 的批处理上会跳错标签。

        那种失败的外观是"脚本里那段代码没执行"，而文件里它明明在那——排查的
        人会去读逻辑，而问题是行尾。
        """
        self.assertNotIn("\n", self.text.replace("\r\n", ""),
                         "start.cmd 必须是 CRLF 行尾")
        self.assertNotIn("\r", self.text.replace("\r\n", ""))

    def test_no_bom(self):
        """带 BOM 的话第一行变成 `<BOM>@echo off`，cmd 不认识它。

        症状是**每一行命令都被回显**——看起来像少了 `@echo off`，而那一行明明
        写在那里。
        """
        self.assertFalse(self.raw.startswith(b"\xef\xbb\xbf"), "start.cmd 带了 BOM")

    def test_it_does_not_depend_on_the_callers_directory(self):
        """双击时的工作目录是随机的，`%~dp0` 是唯一可靠的起点。"""
        self.assertIn('cd /d "%~dp0"', self.text)

    def test_the_launcher_source_is_pure_ascii(self):
        """批处理里的中文会在日志里把行读串——这是实测出来的，不是洁癖。

        现象：stdout 被重定向到文件时，cmd 会在某一行之后读错自己的文件偏移，
        把后面的行从半个汉字处切开。屏幕（日志）上是一行
        `'用' is not recognized as an internal or external command`，而紧挨着
        的两行提示凭空消失；同一个文件在控制台里直接跑却完全正常，于是很容易
        被当成“偶发”。纯 ASCII 的批处理不受影响。

        所以规则是：**这个文件只写 ASCII，中文提示全部放在 deploy/start.py
        的启动器段里**（Python 在控制台上走 WriteConsoleW，重定向时按
        PYTHONIOENCODING 输出，两边都不乱码）。
        """
        try:
            self.raw.decode("ascii")
        except UnicodeDecodeError as exc:
            self.fail("start.cmd 里出现了非 ASCII 字节（偏移 %d：%s）。\n"
                      "中文提示请放进 deploy/start.py 的启动器段，"
                      "否则 `> log.txt` 时提示会被切掉。" % (exc.start, exc.reason))

    def test_it_hands_the_whole_job_to_python(self):
        """三种启动方式（含菜单）都只在 Python 里实现，批处理只传话。"""
        self.assertIn('deploy\\start.py" --launcher %*', self.text,
                      "没有 --launcher，菜单与模式默认值就无处安放")
        self.assertNotIn("PENTEST_DEFAULT_ARGS", self.text,
                         "默认参数已搬进 deploy/start.py 的 launcher_defaults()")

    def test_it_keeps_the_window_open_before_exiting(self):
        """双击起来的窗口在进程退出那一刻就关了，出错信息只闪一下。"""
        self.assertIn("\npause", self.text)

    def test_it_does_not_pin_an_absolute_python_path(self):
        """写死一个 `C:\\...\\python.exe` 在别的机器上必然找不到。"""
        self.assertNotIn("C:\\", self.text)
        for candidate in ("py", "python"):
            self.assertIn(candidate, self.text)

    def test_it_passes_the_exit_code_through(self):
        """0 / 1 / 2 是三种不同结局，批处理不许把它们归一。

        「已经有实例在跑」那一屏现在由 deploy/start.py 打印（退出码 2 的含义
        也在那边的模块文档里），批处理只负责原样交出去——归成 1 的话，调用方
        会去查一个根本没坏的参数。
        """
        self.assertIn('set "CODE=%ERRORLEVEL%"', self.text)
        self.assertIn("exit /b %CODE%", self.text, "退出码没有传出去")
        self.assertIn('start.py" --launcher %*', self.text)

    def test_the_child_always_writes_utf8(self):
        """Python 的输出编码不能由控制台代码页决定。

        批处理不再 `chcp 65001`：Python 在 Windows 控制台上走 WriteConsoleW
        （与代码页无关），只有重定向时才按 PYTHONIOENCODING 出字节。`replace`
        缺了会是 strict：横幅里一个 ⚠ 就能让进程在 console.make_safe() 跑到
        之前崩掉（踩过：加了 --host 0.0.0.0 就起不来）。
        """
        match = re.search(r'set "PYTHONIOENCODING=([^"]+)"', self.text)
        self.assertIsNotNone(match, "没有设置 PYTHONIOENCODING")
        self.assertIn("utf-8", match.group(1).lower(), "日志里会变成代码页编码")
        self.assertIn("replace", match.group(1),
                      "strict 会在 make_safe 跑到之前先把一个 ⚠ 变成崩溃")


class LanLauncherScriptTestCase(unittest.TestCase):
    """`start-lan.cmd` 的静态契约：它是一层壳，壳漏了比没有更糟。"""

    @classmethod
    def setUpClass(cls):
        cls.path = os.path.join(ROOT, "start-lan.cmd")
        cls.exists = os.path.isfile(cls.path)
        cls.raw = b""
        if cls.exists:
            with open(cls.path, "rb") as handle:
                cls.raw = handle.read()
        cls.text = cls.raw.decode("utf-8", "replace")

    def test_it_exists_next_to_start_cmd(self):
        self.assertTrue(self.exists, "局域网启动器不见了（手机就又连不上了）")

    def test_line_endings_are_crlf_and_no_bom(self):
        """与 start.cmd 同一条理由：LF 批处理会让 `call :label` 跳错。"""
        self.assertNotIn("\n", self.text.replace("\r\n", ""), "必须是 CRLF")
        self.assertFalse(self.raw.startswith(b"\xef\xbb\xbf"), "带 BOM 会被当成普通字符")

    def test_it_delegates_with_the_mode_word(self):
        """壳里不允许出现第二份「找 Python」或默认参数。

        抄一份会有两个后果：改一处忘一处；以及两份各自漂移之后「双击
        start.cmd 能起、双击这个起不来」。模式词把差异缩成一个参数，
        局域网还漏掉 `lan` 的话，双击它就静默退回本机——手机连不上，
        而屏幕上什么异常都没有。
        """
        self.assertIn('call "%~dp0start.cmd" lan %*', self.text)
        self.assertNotIn("%PY%", self.text, "找 Python 的逻辑应该只在 start.cmd 里")
        self.assertNotIn("PENTEST_DEFAULT_ARGS", self.text,
                         "默认参数已经搬进 deploy/start.py 的 launcher_defaults()")

    def test_the_shim_is_pure_ascii_too(self):
        self.raw.decode("ascii")

    def test_the_lan_mode_turns_on_all_interfaces(self):
        """壳漏了一个词、或 Python 那边默认参数改了，都会在这里看到。"""
        self.assertEqual(_deploy_module().launcher_defaults("lan"), ["--host", "0.0.0.0"])

    def test_it_says_what_listening_on_every_interface_costs(self):
        """暴露面写在自己文件里：这一屏是使用者唯一会读到的风险说明。"""
        text = "".join(_deploy_module()._lan_risk_lines())
        self.assertIn("所有网卡", text)
        self.assertIn("内网", text)
        self.assertIn("公用 Wi-Fi", text, "没说不适合什么场合")
        self.assertIn("127.0.0.1", text, "要解释清楚控制台那行为什么手机用不了")
        self.assertIn("--host 127.0.0.1", text, "要给一条收回去的办法")


class TunnelLauncherScriptTestCase(unittest.TestCase):
    """`start-tunnel.cmd` 的静态契约：它把控制台临时开到公网，写错一句就是敞门。"""

    @classmethod
    def setUpClass(cls):
        cls.path = os.path.join(ROOT, "start-tunnel.cmd")
        cls.exists = os.path.isfile(cls.path)
        cls.raw = b""
        if cls.exists:
            with open(cls.path, "rb") as handle:
                cls.raw = handle.read()
        cls.text = cls.raw.decode("utf-8", "replace")

    def test_it_exists_at_the_project_root(self):
        self.assertTrue(self.exists, "外网入口的启动器不见了")

    def test_line_endings_are_crlf_and_no_bom(self):
        self.assertNotIn("\n", self.text.replace("\r\n", ""), "必须是 CRLF")
        self.assertFalse(self.raw.startswith(b"\xef\xbb\xbf"),
                         "与 start.cmd/start-lan.cmd 一致：无 BOM")

    def test_it_delegates_with_the_mode_word(self):
        """壳里不能留下第二份健康检查或 `--host`：两份会各说各话。"""
        self.assertIn('call "%~dp0start.cmd" tunnel %*', self.text)
        self.assertIn("exit /b %errorlevel%", self.text, "退出码要传回给调用方")
        self.assertNotIn("%PY%", self.text)
        self.assertNotIn("--host", self.text)

    def test_the_shim_is_pure_ascii_too(self):
        self.raw.decode("ascii")

    def test_the_tunnel_targets_loopback_only(self):
        """隧道连 127.0.0.1：外网入口与局域网入口是两条路。"""
        module = _deploy_module()
        self.assertEqual(module.tunnel_url(8787), "http://127.0.0.1:8787")
        _read_text(os.path.join(DEPLOY_DIR, "start.py"))  # 文件在
        self.assertIn('"--url"', open(os.path.join(DEPLOY_DIR, "start.py"),
                                       encoding="utf-8").read())

    def test_the_port_defaults_to_the_console_port(self):
        module = _deploy_module()
        self.assertEqual(module.DEFAULT_PORT, 8787)
        self.assertEqual(module._port_from_args([]), module.DEFAULT_PORT)
        self.assertEqual(module._port_from_args(["--port", "9000"]), 9000)
        self.assertEqual(module._port_from_args(["--port=9001"]), 9001)

    def test_it_waits_for_a_live_console_before_opening_the_tunnel(self):
        """先探健康检查再起隧道：否则用户拿到的是一个 502 地址，还得自己猜。"""
        source = _read_text(os.path.join(DEPLOY_DIR, "start.py"))
        self.assertIn("def wait_for_console", source)
        self.assertIn("health_is_ours(", source)
        self.assertIn("wait_for_console(port)", source)

    def test_it_says_what_the_public_url_costs(self):
        source = _read_text(os.path.join(DEPLOY_DIR, "start.py"))
        for needle in ("每次启动都不同", "口令", "明文", "反向代理"):
            self.assertIn(needle, source, "隧道风险说明少了：%s" % needle)

    def test_it_stops_when_the_window_closes(self):
        source = _read_text(os.path.join(DEPLOY_DIR, "start.py"))
        self.assertIn("Ctrl+C", source)
        self.assertIn("公网入口", source)

    def test_it_points_at_the_official_client_when_missing(self):
        source = _read_text(os.path.join(DEPLOY_DIR, "start.py"))
        self.assertIn("cloudflared.exe", source)
        self.assertIn("github.com/cloudflare/cloudflared/releases", source)


class LauncherMenuTestCase(unittest.TestCase):
    """菜单三档，以及「2、3 需要控制台时按 1 的方式把它拉起来」。

    这些是**启动器的选择语义**，不是界面文案：写错一处，使用者要么按一个
    不存在的档位，要么又回到「先开一个窗口起服务、再开另一个建隧道」。
    """

    @classmethod
    def setUpClass(cls):
        cls.module = _deploy_module()

    def _launch(self, argv, pick=None):
        """跑一遍 run_launcher，收回 stdout 与交给 main 的 argv。

        main() 会真的去起控制台，所以换成一个只记参数、立刻抛出的桩：
        要测的是「启动器把哪句话、哪个默认参数交下去」，不是控制台起没起来。
        """
        module = self.module
        said = {}

        class Stop(Exception):
            pass

        def fake_main(argv):
            said["argv"] = list(argv)
            raise Stop()

        original = module.main
        module.main = fake_main
        out = io.StringIO()
        patch = mock.patch("builtins.input", return_value=pick) \
            if pick is not None else None
        try:
            if patch is not None:
                patch.start()
            with contextlib.redirect_stdout(out):
                try:
                    code = module.run_launcher(list(argv))
                except Stop:
                    code = None
        finally:
            module.main = original
            if patch is not None:
                patch.stop()
        return code, out.getvalue(), said.get("argv")

    def test_the_menu_offers_three_modes_and_the_first_is_local(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.module._launcher_menu()
        menu = out.getvalue()
        self.assertIn("1  本机", menu)
        self.assertIn("2  局域网", menu)
        self.assertIn("3  外网隧道", menu)
        self.assertNotIn("已取消", menu, "1 本机回来了，菜单里不该再写已取消")
        self.assertIn("回车 = 2", menu, "默认档要写在菜单上")

    def test_choice_one_starts_a_loopback_console(self):
        """按 1 = 只听 127.0.0.1。这一档存在的意义就是「别人连不上」。"""
        code, text, argv = self._launch([], pick="1")
        self.assertIsNone(code, "选了 1 就该把参数交给 main")
        self.assertEqual(argv, ["--host", "127.0.0.1"])
        self.assertIn("本机模式", text)

    def test_local_mode_keeps_its_host_unless_told_otherwise(self):
        code, text, argv = self._launch(["local", "--port", "9000"])
        self.assertIsNone(code)
        self.assertEqual(argv, ["--host", "127.0.0.1", "--port", "9000"],
                         "默认参数要垫在前面，使用者写的才盖得掉")
        self.assertIn("127.0.0.1", text)

    def test_the_three_words_and_their_numbers_agree(self):
        module = self.module
        with mock.patch("builtins.input", side_effect=["1", "2", "3", "", "7"]):
            picks = [module._ask_launcher_mode() for _ in range(5)]
        self.assertEqual(picks, ["local", "lan", "tunnel", "lan", "lan"],
                         "回车与看不懂的输入都落到局域网（默认档）")
        self.assertEqual(module.LAUNCHER_MODES, ("local", "lan", "tunnel"))

    def test_an_unknown_mode_word_fails_instead_of_widening_the_door(self):
        """写错一个字母必须失败，并把三个真正的词给出来。

        静默落到局域网 = 把门开大却不说（`local` 时代那种意外）；静默什么
        都不起 = 按了回车之后又一次惊喜。
        """
        code, text, argv = self._launch(["locl"])
        self.assertEqual(code, 1)
        self.assertIsNone(argv, "不认识的模式词不该交到 main")
        self.assertIn("不认识的启动方式", text)
        for word in ("local", "lan", "tunnel"):
            self.assertIn(word, text)

    def test_no_mode_word_now_means_lan_and_says_so(self):
        """不带模式词的老用法（start.cmd --port 9000）落到局域网，必须说出来。"""
        code, text, argv = self._launch(["--port", "9000"])
        self.assertIsNone(code)
        self.assertEqual(argv, ["--host", "0.0.0.0", "--port", "9000"])
        self.assertIn("没写启动方式", text)

    def test_lan_mode_warns_when_the_live_console_could_be_loopback_only(self):
        """端口上已有一份控制台时，选 2 的人会以为手机能连。

        那一份可能是「1 本机」起的；不把这两件事连起来，使用者看到的只是
        「选了 2，手机还是连不上」。
        """
        module = self.module
        original = module.probe_instance
        module.probe_instance = lambda host, port: "ours"
        try:
            code, text, argv = self._launch(["lan", "--port", "9000"])
        finally:
            module.probe_instance = original
        self.assertIsNone(code)
        self.assertEqual(argv, ["--host", "0.0.0.0", "--port", "9000"])
        self.assertIn("已经有一份控制台在跑", text)
        self.assertIn("Ctrl+C", text, "要给出换成本次监听的办法")

    def test_the_tunnel_mode_starts_a_loopback_console(self):
        """选 3 需要一个窗口：没在跑就先按「1 本机」把控制台拉起来。

        隧道在本地连回来，没必要顺手把网卡也打开（那是 2 的事）。
        """
        argv = self.module.console_command(["--no-browser", "--db", "x.db"], 9000)
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], os.path.abspath(os.path.join(DEPLOY_DIR, "start.py")))
        self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")
        # 使用者的参数紧跟其后（覆盖机制），端口补在最后
        self.assertEqual(argv.index("--no-browser"), argv.index("--host") + 2)
        self.assertEqual(argv[-2:], ["--port", "9000"])

    def test_the_tunnel_mode_respects_an_explicit_wider_host(self):
        """`start.cmd tunnel --host 0.0.0.0` 仍然收得回来：argparse 取最后一个。"""
        argv = self.module.console_command(["--host", "0.0.0.0"], 8787)
        self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")
        self.assertEqual(argv[argv.index("--host", argv.index("--host") + 1) + 1],
                         "0.0.0.0")

    def test_a_missing_console_is_not_an_error_any_more(self):
        """过去这一档只建隧道，控制台没起来就报错退出。

        现在那条分支必须先尝试拉起控制台；错误文案里也不该再教人「先选 1 或 2」。
        """
        source = _read_text(os.path.join(DEPLOY_DIR, "start.py"))
        self.assertIn("def start_console(", source)
        self.assertIn('start_console(rest, port, LAUNCHER_HOSTS["tunnel"])', source)
        self.assertNotIn("start.cmd 选 1 或 2", source,
                         "那句提示现在会让人绕回两个窗口的老路")


class TunnelDocumentationTestCase(unittest.TestCase):
    """使用指南是使用者唯一会读到的那一页，双击路径不能只在代码里存在。"""

    def test_the_guide_names_the_double_click_path(self):
        with open(os.path.join(ROOT, "使用手册.md"), encoding="utf-8") as handle:
            guide = handle.read()
        for needle in ("start-tunnel.cmd", "trycloudflare.com",
                       "start.cmd lan", "start.cmd tunnel"):
            self.assertIn(needle, guide, "指南里没写：%s" % needle)
        self.assertIn("选择启动方式", guide, "没说双击 start.cmd 会先出菜单")


class DeploymentAssetsTestCase(unittest.TestCase):
    """部署产物的静态检查：不引 Docker，但有部署脚本与 systemd 单元。"""

    def test_no_docker_artifacts_left(self):
        leftovers = [name for name in os.listdir(DEPLOY_DIR)
                     if "docker" in name.lower() or name.startswith("Dockerfile")]
        self.assertEqual(leftovers, [], "已改为 Web 端部署，不应残留 Docker 产物：%s"
                                        % leftovers)

    def test_deploy_assets_exist_and_compile(self):
        for name in ("start.py",):
            path = os.path.join(DEPLOY_DIR, name)
            self.assertTrue(os.path.isfile(path), "缺少部署入口：%s" % name)
            completed = subprocess.run([PYTHON, "-m", "py_compile", path],
                                       capture_output=True, timeout=60,
                                       encoding="utf-8", errors="replace")
            self.assertEqual(completed.returncode, 0,
                             "%s 语法错误：%s" % (name, completed.stderr or ""))

    def test_systemd_unit_template_is_valid_enough(self):
        path = os.path.join(DEPLOY_DIR, "pentest-node.service")
        with open(path, "r", encoding="utf-8") as handle:
            unit = handle.read()
        for section in ("[Unit]", "[Service]", "[Install]"):
            self.assertIn(section, unit)
        self.assertIn("ExecStart=", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        # 单元文件里会放节点令牌，模板必须提醒收紧权限
        self.assertIn("chmod 600", unit)

    def test_start_refuses_when_web_assets_missing(self):
        """Web 端部署的前提是静态资源在位；缺失时要明确报错而不是起一个空壳。"""
        completed = subprocess.run(
            [PYTHON, "-c",
             "import sys; sys.path.insert(0, r'%s');"
             "from pentestd.server import ControlPlane;"
             "from pentestd.store import Store;"
             "import tempfile, os;"
             "s = Store(os.path.join(tempfile.mkdtemp(), 'x.db'));"
             "p = ControlPlane(s, os.path.join(r'%s', 'no-such-dir'));"
             "print(any(c['id'] == 'web' and c['level'] == 'fail'"
             " for c in p._checks(None)))" % (ROOT, ROOT)],
            capture_output=True, timeout=60, encoding="utf-8", errors="replace")
        self.assertEqual(completed.returncode, 0, completed.stderr or "")
        self.assertIn("True", completed.stdout,
                      "Web 资源缺失时自检必须报 fail")


if __name__ == "__main__":
    unittest.main(verbosity=2)
