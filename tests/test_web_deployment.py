# -*- coding: utf-8 -*-
"""Web 端部署的测试。

部署形态是"一个 Python 进程 + 浏览器"，所以这里验证两件事：

  * 控制台接口（部署自检 / 节点接入 / 吊销 / 任务与基线）返回的形状正确；
  * `deploy/start.py` 真能起来并把控制台暴露在指定端口上——
    部署脚本如果只是"看起来能跑"，那和没有部署脚本区别不大。
"""
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
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

from pentestd import engine                                     # noqa: E402
from pentestd.store import Store                                # noqa: E402
from tests import support                                       # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")
DEPLOY_DIR = os.path.join(ROOT, "deploy")
PYTHON = sys.executable
# 一键启动脚本是 POSIX sh（Windows 上由 Git 自带的 sh 或 MSYS 提供）。没有 sh 的
# 机器只跳过执行那一条：静态契约与解释器无关，照旧全跑。
SHELL = shutil.which("sh") or shutil.which("bash") or ""
NO_SHELL = "这台机器上没有 sh，跳过 start.sh 的执行用例"
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
    """别人已经起着一个，就别再起第二个——以及 `start.sh` 的手感契约。

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

    def _launch(self, tmp, port, log_path, use_launcher=False, extra=()):
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        handle = open(log_path, "w", encoding="utf-8")
        if use_launcher:
            # 走**真的**启动器：一键启动那条路上，壳与 Python 一起才是交付物。
            # cwd 故意给一个不相干的目录——从桌面快捷方式拉起时 cwd 是随机的，
            # 脚本里那句 `cd "$here"` 就是为那一下存在的。
            argv = [SHELL, os.path.join(ROOT, "start.sh"),
                    "--no-browser", "--port", str(port),
                    "--db", os.path.join(tmp, "start.db")] + list(extra)
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

        `start.sh` 用的是 `exec`，pid 就是服务本身；但 `--local-executor` 会让
        控制面再拉一个节点线程/进程，只 terminate 父进程仍可能留下占着日志文件的
        残余——于是临时目录
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
    @unittest.skipUnless(SHELL, NO_SHELL)
    def test_the_lan_launcher_binds_all_interfaces(self):
        """`start.sh --host 0.0.0.0` 真的把地址送进了 start.py。

        只测默认参数存在是不够的：壳里少一个 `"$@"`、或者它被放在了别处，
        文件都长得一样好看，而手机那边还是一片连不上。这里看的是**启动横幅**
        ——它只在 `--host 0.0.0.0` 时才会多出「局域网」那一行。
        """
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "lan.log")
            process, handle = self._launch(tmp, port, log_path, use_launcher=True,
                                           extra=("--host", "0.0.0.0"))
            try:
                served = self._await_port(log_path)
                self.assertIsNotNone(served, "start.sh 未能在 40 秒内报出监听端口")
                text = self._read(log_path)
                self.assertIn("局域网", text,
                              "横幅没报手机能用的地址：那等于又要使用者自己去 ipconfig")
                self.assertIn(":%d/" % served, text,
                              "横幅里的端口不是真正监听的那个（顺延之后就说错了）")
                client = support.Client("http://127.0.0.1:%d" % served)
                self.assertEqual(client.get("/api/v1/auth/state")[0], 200)
            finally:
                self._finish(process, handle)

    @unittest.skipUnless(SHELL, NO_SHELL)
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
                # 那条提示在正常启动时也会打，拿它当标记会永远为真，
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

    @unittest.skipUnless(SHELL, NO_SHELL)
    def test_the_launcher_starts_a_working_console(self):
        """反过来也要测：`start.sh` 在端口空闲时**真的**起得来。

        只测"拒绝"会得到一个"永远拒绝"的通过版本——那种脚本两条用例里只过
        一条，而使用者那天敲的是没被测到的那一条。
        """
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "launcher.log")
            process, handle = self._launch(tmp, port, log_path, use_launcher=True)
            try:
                served = self._await_port(log_path)
                self.assertIsNotNone(served, "start.sh 未能在 40 秒内报出监听端口")
                self.assertEqual(served, port, "端口空闲时应沿用指定端口")
                client = support.Client("http://127.0.0.1:%d" % served)
                self.assertEqual(client.get("/api/v1/auth/state")[0], 200)
                self.assertEqual(client.bootstrap()[0], 201)
                self.assertTrue(client.get("/api/v1/setup")[1]["ok"])
                self.assertIn("（Web 端部署）", self._read(log_path),
                              "启动横幅不见了：壳与 deploy/start.py 之间断了"
                              "（横幅一直打在 start.py 里，与壳无关）")
            finally:
                self._finish(process, handle)


def _deploy_module():
    """把 deploy/start.py 当模块取出来。

    启动器那一层（菜单 / 三档 / 隧道）已经撤掉，`start.sh` 只负责找到解释器
    再把参数原样交出去；「说了什么」这类断言仍然必须看得到模块里的字符串。
    """
    path = os.path.join(DEPLOY_DIR, "start.py")
    spec = importlib.util.spec_from_file_location("deploy_start_module_copy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class StartScriptTestCase(unittest.TestCase):
    """`start.sh` 的静态契约：这条路上只有一层壳，壳写错就是起不来。"""

    @classmethod
    def setUpClass(cls):
        cls.path = os.path.join(ROOT, "start.sh")
        with open(cls.path, "rb") as handle:
            cls.raw = handle.read()
        cls.text = cls.raw.decode("utf-8")

    def test_the_launcher_exists_at_the_project_root(self):
        self.assertTrue(os.path.isfile(self.path), "一键启动脚本不见了")

    def test_line_endings_are_lf(self):
        """CRLF 会让 sh 在 shebang 那一行就报 bad interpreter。

        那种失败的外观是"脚本明明存在、敲完却什么也没发生"，而文件里每一行都
        写得好好的——排查的人会去读逻辑，而问题是行尾。Windows 上把 .sh 当文本
        文件存成 CRLF 是最常见的一种。
        """
        self.assertNotIn("\r", self.text, "start.sh 必须是 LF 行尾")

    def test_no_bom(self):
        """带 BOM 时 `#!/bin/sh` 变成 `<BOM>#!/bin/sh`。

        内核不认这个解释器路径，报 `bad interpreter`，而那一行看起来完全正常。
        """
        self.assertFalse(self.raw.startswith(b"\xef\xbb\xbf"), "start.sh 带了 BOM")

    def test_it_does_not_depend_on_the_callers_directory(self):
        """从别的目录调用、或经桌面快捷方式拉起时 cwd 是随机的。

        `--db` 与 Web 资源的默认路径都按仓库根算：少这一次 cd，就会在随机目录
        里建一个空库，然后报"未找到 Web 界面资源"。
        """
        self.assertIn('dirname -- "$0"', self.text)
        self.assertIn('cd "$here"', self.text)

    def test_it_hands_the_whole_job_to_python(self):
        """壳只做三件事：站到仓库根、找解释器、把参数原样交下去。"""
        self.assertIn("deploy/start.py", self.text)
        self.assertIn('"$@"', self.text, "参数没有原样交下去：--host / --port 会被吞掉")
        self.assertNotIn("--launcher", self.text,
                         "--launcher 那一层已经撤掉了，壳不该再传它")

    def test_it_does_not_pin_an_absolute_python_path(self):
        """写死一个 `/usr/bin/python3` 或 `C:\\...\\python.exe` 换台机器就找不到。"""
        self.assertNotIn("C:\\", self.text)
        for candidate in ("python3", "python"):
            self.assertIn(candidate, self.text)

    def test_it_passes_the_exit_code_through(self):
        """0 / 1 / 2 是三种不同结局（2 = 已经有一个实例在跑）。

        用 `exec` 而不是"先跑再 exit $?"：pid 不换，Ctrl+C 才打得到控制面，
        退出码也天然是它的。
        """
        self.assertIn("exec ", self.text, "没有 exec：Ctrl+C 打不到控制面")

    def test_the_child_always_writes_utf8(self):
        """Python 的输出编码不能由终端决定。

        横幅里有 ⚠ 这类字符：strict 会让进程在 console.make_safe() 跑到之前就崩
        （踩过：加了 --host 0.0.0.0 就起不来）。replace 让它退化成问号而不是异常。
        """
        match = re.search(r"PYTHONIOENCODING=(\S+)", self.text)
        self.assertIsNotNone(match, "没有设置 PYTHONIOENCODING")
        self.assertIn("utf-8", match.group(1).lower(), "日志里会变成终端编码")
        self.assertIn("replace", match.group(1), "strict 会先于 make_safe 崩掉")
        self.assertIn("export PYTHONIOENCODING", self.text,
                      "只赋值不 export：子进程看不到")

    def test_it_says_something_when_python_is_missing(self):
        """找不到解释器时不能一声不响地退出 0。"""
        self.assertIn("was not found", self.text)
        self.assertIn("python.org", self.text)
        self.assertIn("exit 1", self.text)

    @unittest.skipUnless(SHELL, NO_SHELL)
    def test_the_script_runs_through_the_shell(self):
        """`--help` 能打出来，说明 shebang 之后的每一行都被 sh 读懂了。

        静态检查看不出"for 少一个 done"这类写法，而那种脚本看起来与正常脚本
        一模一样。cwd 故意给系统临时目录：这一条同时验证它自己不依赖调用目录。
        """
        completed = subprocess.run([SHELL, self.path, "--help"],
                                   cwd=tempfile.gettempdir(),
                                   capture_output=True, timeout=120,
                                   encoding="utf-8", errors="replace")
        self.assertEqual(completed.returncode, 0, completed.stderr or "")
        self.assertIn("--host", completed.stdout, "参数没有交到 start.py")


class LauncherDocumentationTestCase(unittest.TestCase):
    """启动路径只有一个，文档里那几处必须跟着它走。"""

    def _text(self, name):
        return _read_text(os.path.join(ROOT, name))

    def test_the_guide_names_the_start_script(self):
        guide = self._text("使用手册.md")
        self.assertIn("start.sh", guide, "指南里没写仓库根的启动脚本")
        self.assertIn("--host 0.0.0.0", guide, "没写怎么让同一内网打开")
        self.assertNotIn("start.cmd", guide, "指南还在讲已经删掉的 .cmd")
        self.assertNotIn("选择启动方式", guide, "菜单已经撤掉了")

    def test_the_readme_and_the_overview_agree(self):
        for name in ("README.md", "项目说明.md"):
            text = self._text(name)
            self.assertIn("start.sh", text, "%s 里没有新的启动路径" % name)
            self.assertNotIn("start.cmd", text, "%s 还在讲已经删掉的 .cmd" % name)


class TunnelDocumentationTestCase(unittest.TestCase):
    """对外那条路只有一条，文档必须说的是那条。

    实测（本机出口 211.20.18.216，CGNAT 下没有公网入口）：Cloudflare 快速隧道
    `--protocol http2` 能注册成功，但分配到的 `*.trycloudflare.com` 地址全程 530
    （Argo 1033 / Unable to resolve）；`quic` 永远 "failed to dial to edge"；
    `--ha-connections 4` 直接 `Post https://api.trycloudflare.com/tunnel: EOF`。
    Tailscale 的 `machine/register` 十秒超时、三个 DERP 探测全 000。
    serveo.net 会话 200–330s 就断、中段大量 502。留下的是 localhost.run。

    文档漂移的代价在这条路上特别大：照旧写 cloudflared，读者会拿到一个永远 530
    的地址，然后以为是自己的防火墙没开。所以这里钉三件事——手册要给的是那条能用的、
    要写明为什么另一条不通、给出的命令必须真的存在。
    """

    def _text(self, name):
        return _read_text(os.path.join(ROOT, name))

    def test_the_guide_points_at_the_working_tunnel(self):
        guide = self._text("使用手册.md")
        self.assertIn("keep_tunnel.py", guide, "指南没给那条实测可用的隧道")
        self.assertIn("localhost.run", guide, "指南没写默认 provider")
        self.assertIn("data/tunnel-url.txt", guide, "指南没写地址落在哪")
        self.assertIn("530", guide, "指南没写 Cloudflare 快速隧道在这条链路上不通")
        # 「用 cloudflared」当唯一答案是旧写法：它现在只能作为"别照这个试"的注脚出现。
        self.assertNotIn("要从外网（手机流量、异地电脑）打开：用 Cloudflare", guide,
                         "指南仍把 Cloudflare 快速隧道当成对外那条路")

    def test_the_overview_agrees(self):
        overview = self._text("项目说明.md")
        self.assertIn("keep_tunnel.py", overview, "项目说明没跟着换隧道方式")

    def test_the_tunnel_script_is_real(self):
        path = os.path.join(ROOT, "tools", "keep_tunnel.py")
        self.assertTrue(os.path.isfile(path), "缺少 tools/keep_tunnel.py")
        completed = subprocess.run([PYTHON, "-m", "py_compile", path],
                                   capture_output=True, timeout=60,
                                   encoding="utf-8", errors="replace")
        self.assertEqual(completed.returncode, 0,
                         "keep_tunnel.py 语法错误：%s" % (completed.stderr or ""))
        source = _read_text(path)
        # 两个 provider 的地址形状不同，抓错一个就等于写了一个永远打不开的地址。
        # 正则里那段是转义过的字面量（`app\.lhr\.life`），照着源码写的形状核。
        for token in ("app\\.lhr\\.life", "serveousercontent\\.com",
                      "ExitOnForwardFailure=yes", "os.replace"):
            self.assertIn(token, source, "keep_tunnel.py 里没有 %s" % token)


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
