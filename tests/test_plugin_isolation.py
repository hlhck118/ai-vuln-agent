# -*- coding: utf-8 -*-
"""插件隔离：装/卸载只作用于项目自己的 DSH，**绝不读写电脑上那一份**。

## 为什么这一组必须单独存在

隔离做错的**表现是"什么都正常"**：装成功了、插件列表出现了、Agent 多了一条——
只有副作用发生在别人的 `~/.dsh` 里。没有断言去盯那个目录，这件事就永远没人发现，
直到使用者某天发现自己 DSH 的 `package.json` 被改过。

所以这一组不看"接口返回 200"，只钉三件在界面上看不见的事：

1. 子进程里的 `DSH_HOME` 到底指向哪儿（`run_cli`）；
2. **没有** home 时是不是真的什么都没做（fail-closed，而不是退回本机）；
3. 项目 profile 的 pnpm 配置有没有补齐那几个运维知识——不补的表现是
   "插件市场突然全坏，过一天自己好了"。

## 「只读」也删掉了：现在连读都不读

上一版这里还留着一份"只读的本机插件清单"（`machine_plugins()`），理由是
"隔离之后使用者不知道电脑上装了什么"。而它的实际行为是：读哪一份 home 取决于
平台**被谁启动**（`$DSH_HOME` 优先），于是同一个函数在两种启动方式下给出不同结果，
且这件事在界面上看不出来。整块删掉之后，这一组多了一类断言：**环境变量与那份
home 都不能影响结果**（见 `NoMachineReadTestCase`）。

## 本文件绝不向真实 `~/.dsh` 写入，也绝不去读它

`platform_home` 在每个用例里都被换成临时目录，`sandbox()` 另外造一个**诱饵**
本机 home（里面放着它自己的 profile 与插件），任何"读错了家"的实现都会把诱饵里
那个哨兵名字带进返回值里——那一处断言比"文件没被改"更早发现读错。
"""
import json
import os
import shutil
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import dsh_runtime, dshplugins, hub, server  # noqa: E402
from tests import support                                  # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


class _Sandbox(object):
    """把两个 home 都按到临时目录下，并还原进程级状态。

    做成 mixin 而不是 setUp 里抄一遍：只漏掉一处（比如忘了还原 platform home），
    失败会出现在**别的**文件里，而那里没人会怀疑到这里。
    """

    def sandbox(self):
        self.dir, self.store = support.temporary_store("iso.db")
        self.root = self.dir.name
        self.machine = os.path.join(self.root, "machine-dsh")
        self.platform = os.path.join(self.root, "platform-dsh")
        os.makedirs(os.path.join(self.machine, "profiles"), exist_ok=True)
        self._saved_platform = dshplugins.platform_home()
        self._saved_env = {key: os.environ.get(key)
                           for key in ("DSH_HOME", "DSH_PROFILE")}
        # 诱饵：一个"电脑上那份 DSH"，带一个只可能从它那里读到的哨兵。
        # 任何读错家的实现都会把这个哨兵带进返回值。
        self.sentinel = "kettle-of-the-machine-home"
        self.write_profile(
            self.machine, "desktop",
            {self.sentinel: "github:owner/%s#deadbeef" % self.sentinel},
            package_meta={self.sentinel: {
                "name": self.sentinel, "version": "9.9.9",
                "description": "渗透红队演练（这是诱饵，只有读错家才会看到它）"}})
        # 环境变量也指向诱饵：老实现会据此把诱饵当成"本机 home"。
        os.environ["DSH_HOME"] = self.machine
        os.environ["DSH_PROFILE"] = "desktop"
        dshplugins.set_platform_home(self.platform)
        dshplugins.invalidate()

    def unsandbox(self):
        dshplugins.set_platform_home(self._saved_platform)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        dshplugins.invalidate()
        self.store.close()
        self.dir.cleanup()

    def write_profile(self, home, name, dependencies, bundles=(),
                      package_meta=None):
        """在某个 home 下造一个 profile：清单 + 每个依赖的 package.json。"""
        path = os.path.join(home, "profiles", name)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "package.json"), "w", encoding="utf-8") as fh:
            json.dump({"name": "dsh-profile-%s" % name, "private": True,
                       "dependencies": dependencies,
                       "dsh": {"profile": {"bundles": list(bundles)}}}, fh)
        for pkg, meta in (package_meta or {}).items():
            target = os.path.join(path, "node_modules", *pkg.split("/"))
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, "package.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(meta, fh)
        return path


# ---------------------------------------------------------------- CLI 通道

class RunCliIsolationTestCase(_Sandbox, unittest.TestCase):
    """`run_cli` 是唯一启动 DSH 子进程的地方，所以隔离必须在这里被看见。"""

    def setUp(self):
        self.sandbox()
        self._saved_run = hub.subprocess.run
        self._saved_cli = hub.cli_path
        self.seen = []
        hub.cli_path = lambda: "X:\\fake\\dsh.cmd"

        def fake_run(command, **kwargs):
            self.seen.append((list(command), dict(kwargs.get("env") or {})))

            class _Done(object):
                returncode = 0
                stdout = "ok"
                stderr = ""
            return _Done()

        hub.subprocess.run = fake_run
        # 父进程环境里放一份"电脑上那份 DSH"的会话身份，用来验证泄漏清理。
        self._leaked = {"DSH_PROFILE": "desktop",
                        "DSH_PROFILE_DIR": os.path.join(self.machine, "profiles",
                                                        "desktop"),
                        "DSH_SESSION_ID": "session-leak",
                        "DSH_SHELL": "1", "DSH_WEB_URL": "http://127.0.0.1:19387"}
        self._saved_env = {name: os.environ.get(name) for name in self._leaked}
        os.environ.update(self._leaked)

    def tearDown(self):
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        hub.subprocess.run = self._saved_run
        hub.cli_path = self._saved_cli
        self.unsandbox()

    def test_the_child_process_gets_dsh_home_pointing_at_the_project(self):
        """这一行就是隔离本身：`DSH_HOME` 是 DSH 那套东西的唯一根。"""
        hub.run_cli(["plugin", "--profile", dshplugins.PLATFORM_PROFILE,
                     "add", "is-odd"], dsh_home=self.platform)
        self.assertEqual(len(self.seen), 1)
        command, env = self.seen[0]
        self.assertEqual(env.get("DSH_HOME"), self.platform)
        # 参数一个都不能少：`--profile` 决定落在哪个 profile 上，两者合起来才是隔离。
        # 命令前缀由启动器种类决定（`.js` 先给 node、`.cmd` 走 COMSPEC——
        # 这里打的是 `.cmd`），所以断言的是**结尾那一段参数**，不是 `command[1:]`。
        expected = ["plugin", "--profile", dshplugins.PLATFORM_PROFILE, "add", "is-odd"]
        self.assertEqual(command[-len(expected):], expected)
        self.assertGreaterEqual(len(command), len(expected) + 1, "启动器前缀不见了")

    def test_the_parent_harness_identity_is_not_handed_to_the_child(self):
        """删掉那串 `DSH_*` 会话变量，同时**不能**改到父进程自己。

        `dict(os.environ)` 之后再 pop 是刻意的：若直接 `os.environ.pop()`，
        平台会把自己进程里的 DSH 身份抹掉——那会让宿主 DSH 在后续调用里失常，
        而症状出现在平台之外，几乎不可能归因到这里。
        """
        hub.run_cli(["plugin", "--profile", "p", "add", "x"], dsh_home=self.platform)
        _command, env = self.seen[0]
        for name in hub._LEAKED_ENV:
            self.assertNotIn(name, env, "%s 泄漏进了子进程" % name)
            self.assertEqual(os.environ.get(name), self._leaked[name],
                             "%s 被从父进程里删掉了" % name)

    def test_the_runtime_strips_the_same_leaked_env_as_the_cli(self):
        """两处必须**同源**，而且真的把那一组剥掉。

        这条用例的由来：两处各写了一份清单，而 `dsh_runtime` 的注释还写着
        "hub.py 里也剥了同一组"——实际少了 `DSH_SHELL` 与 `DSH_WEB_URL`。
        缺口只在**运行时**那条路上出现：插件那条路的断言照常通过，所以两个清单
        不一致这件事，本来没有任何东西会报。

        三个断言各挡一种错法，一个都不能省：

        * `assertIs` 挡"两处不再是同一份"（相等只说明今天恰好一样，明天各改一处
          不会被发现）；
        * `assertEqual(set(...))` 挡"整体缺项或多出东西"；
        * 下面那份 `leaked` 是**写在用例里**的清单，**不抄产品**——如果这里改成
          遍历 `dsh_runtime.LEAKED_ENV`，那么清单少一个时用例会跟着少检查一个，
          永远绿。这一条是被负例检查逼出来的：第一版正是那么写的，负例两个全绿。
        """
        self.assertIs(hub._LEAKED_ENV, dsh_runtime.LEAKED_ENV)
        leaked = ("DSH_PROFILE", "DSH_PROFILE_DIR", "DSH_SESSION_ID",
                  "DSH_SHELL", "DSH_WEB_URL")           # 用例自己的判据
        self.assertEqual(set(leaked), set(dsh_runtime.LEAKED_ENV),
                         "清单缺项或多出了东西")
        for name in leaked:
            self.assertIn(name, os.environ, "沙箱没设 %s，这条断言会空转" % name)
        env = dsh_runtime.DshRuntime(home=self.platform, root=ROOT,
                                     dsh_path="X:\\fake\\dsh.cmd",
                                     patch="X:\\fake\\patch.yml").env()
        for name in leaked:
            self.assertNotIn(name, env, "%s 泄漏进了运行时子进程" % name)
        self.assertEqual(env.get("DSH_HOME"), self.platform,
                         "运行时的家必须是项目自己那一份")

    def test_an_empty_home_refuses_to_start_the_process(self):
        """最内层守卫：没有 home 就不启动子进程，而不是继承父进程的 `DSH_HOME`。

        继承的后果是"安装落到了电脑上那份 DSH 里"，而父进程的 `DSH_HOME` 恰好
        正是那个值——所以这里不断言返回值，断言**进程根本没被启动**。
        """
        result = hub.run_cli(["plugin", "--profile", "p", "add", "x"], dsh_home="")
        self.assertFalse(result["ok"])
        self.assertIn("隔离", result["reason"])
        self.assertEqual(self.seen, [], "未指定 home 时不该启动 DSH CLI")

    def test_proxy_rides_along_as_environment(self):
        hub.run_cli(["plugin", "--profile", "p", "add", "x"], dsh_home=self.platform,
                    proxy="http://127.0.0.1:7897")
        _command, env = self.seen[0]
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertEqual(env.get(name), "http://127.0.0.1:7897")


class WritePathRefusalTestCase(_Sandbox, unittest.TestCase):
    """写路径的两道守卫，以及"拒绝时真的什么都没做"。"""

    def setUp(self):
        self.sandbox()
        self._saved_run = hub.subprocess.run
        self.started = []
        hub.subprocess.run = lambda *a, **k: self.started.append(a) or None
        self._saved_cli = hub.cli_path
        hub.cli_path = lambda: "X:\\fake\\dsh.cmd"

    def tearDown(self):
        hub.subprocess.run = self._saved_run
        hub.cli_path = self._saved_cli
        self.unsandbox()

    def test_install_without_a_home_is_refused_and_runs_nothing(self):
        result = hub.install(self.root, "is-odd", dshplugins.PLATFORM_PROFILE)
        self.assertFalse(result["ok"])
        self.assertIn("不允许动电脑上那份 DSH", result["reason"])
        self.assertEqual(self.started, [])

    def test_remove_without_a_home_is_refused_and_runs_nothing(self):
        result = hub.remove(self.root, "is-odd", dshplugins.PLATFORM_PROFILE)
        self.assertFalse(result["ok"])
        self.assertEqual(self.started, [])

    def test_a_missing_profile_name_is_refused_rather_than_guessed(self):
        """profile 缺失时不能"猜一个默认值"：`dsh plugin` 的 profile 是必填项，
        猜错会落到另一个 profile 上，而那是我们要避免的事的另一种形式。"""
        result = hub.install(self.root, "is-odd", "", dsh_home=self.platform)
        self.assertFalse(result["ok"])
        self.assertIn("profile", result["reason"])
        self.assertEqual(self.started, [])

    def test_a_bad_target_is_still_refused_before_the_home_check(self):
        """目标串白名单是挡 cmd.exe 注入的那道闸，校验顺序不能被这次改动打乱。"""
        result = hub.install(self.root, "is-odd&calc", dshplugins.PLATFORM_PROFILE,
                             dsh_home=self.platform)
        self.assertFalse(result["ok"])
        self.assertIn("不允许的字符", result["reason"])
        self.assertEqual(self.started, [])


class ProfileWorkspaceTestCase(_Sandbox, unittest.TestCase):
    """项目 profile 的 pnpm 配置。这里补的是**运维知识**，不是格式偏好。"""

    def setUp(self):
        self.sandbox()
        self.path = os.path.join(self.platform, "profiles", "p")
        os.makedirs(self.path, exist_ok=True)
        # DSH 的 `initProfile()` 写出来就是这三行，没有别的。
        self._write("pnpm-workspace.yaml",
                    "packages:\n  - .\n\nnodeLinker: hoisted\n"
                    "autoInstallPeers: false\n")

    def _write(self, name, text):
        with open(os.path.join(self.path, name), "w", encoding="utf-8",
                  newline="") as fh:
            fh.write(text)

    def _read(self, name):
        with open(os.path.join(self.path, name), "r", encoding="utf-8",
                  newline="") as fh:
            return fh.read()

    def tearDown(self):
        self.unsandbox()

    def test_the_release_age_gate_is_dropped(self):
        """pnpm 11 的发布年龄闸门会让"刚发布的插件"把整个市场挡成不可用，
        连卸载都一起失败。新 profile 是空的，会原样继承这个坑。"""
        hub.sync_profile_settings(self.path)
        text = self._read("pnpm-workspace.yaml")
        self.assertIn("minimumReleaseAge: 0", text)
        self.assertEqual(text.count("minimumReleaseAge"), 1)

    def test_the_rewrite_is_lf_only_and_keeps_line_endings_consistent(self):
        """DSH 用 `writeFileSync` 写的是 LF。这里若翻成 CRLF，同一个文件就变成
        混行尾——YAML 照样解析，但任何按行比对的测试都会莫名其妙地失败。"""
        hub.sync_profile_settings(self.path)
        with open(os.path.join(self.path, "pnpm-workspace.yaml"), "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"\r\n", raw)
        self.assertIn("minimumReleaseAge: 0".encode("utf-8"), raw)

    def test_existing_settings_and_comments_survive(self):
        """**只追加，不重写**。全局那份 `pnpm-workspace.yaml` 里带着大段手写注释，
        那是踩出来的运维知识；重写会把它和 `allowBuilds`、`patchedDependencies`
        一起丢掉。"""
        original = ("packages:\n  - .\n\nnodeLinker: hoisted\nautoInstallPeers: false\n"
                    "storeDir: C:\\somewhere\n"
                    "# 这段注释是运维知识，不能被覆盖\n"
                    "allowBuilds:\n  'dsh-x@1.0.0': true\n")
        self._write("pnpm-workspace.yaml", original)
        hub.sync_profile_settings(self.path)
        text = self._read("pnpm-workspace.yaml")
        for fragment in ("packages:\n  - .\n", "storeDir: C:\\somewhere",
                         "# 这段注释是运维知识，不能被覆盖",
                         "'dsh-x@1.0.0': true", "autoInstallPeers: false"):
            self.assertIn(fragment, text)
        self.assertIn("minimumReleaseAge: 0", text)

    def test_it_is_idempotent(self):
        hub.sync_profile_settings(self.path)
        once = self._read("pnpm-workspace.yaml")
        hub.sync_profile_settings(self.path)
        self.assertEqual(self._read("pnpm-workspace.yaml"), once)

    def test_it_does_not_pin_a_store_directory(self):
        """刻意**不**补 `storeDir` / `virtualStoreDir`：它们由 pnpm 按项目路径推导，
        推导自洽就没问题。硬写一个从别处抄来的 store，反而会因"路径拼写与
        `.modules.yaml` 记录不一致"触发 ERR_PNPM_UNEXPECTED_STORE。"""
        hub.sync_profile_settings(self.path)
        text = self._read("pnpm-workspace.yaml")
        self.assertNotIn("storeDir", text)
        self.assertNotIn("virtualStoreDir", text)

    def test_the_registry_lands_in_npmrc_only_when_absent(self):
        hub.sync_profile_settings(self.path, "https://registry.npmmirror.com")
        self.assertEqual(self._read(".npmrc"), "registry=https://registry.npmmirror.com\n")
        # 已经写过的 registry 不被覆盖：使用者自己改过就该以他的为准。
        self._write(".npmrc", "registry=https://my.internal/\n")
        hub.sync_profile_settings(self.path, "https://registry.npmmirror.com")
        self.assertEqual(self._read(".npmrc"), "registry=https://my.internal/\n")

    def test_an_absent_directory_is_not_an_error(self):
        """profile 还没建出来时同步配置应当无声跳过——它不是错误路径，
        而是"第一次安装"之前的正常状态。"""
        hub.sync_profile_settings(os.path.join(self.root, "nope"))
        hub.sync_profile_settings("")


# ---------------------------------------------------------------- 快照隔离

class SnapshotIsolationTestCase(_Sandbox, unittest.TestCase):

    def setUp(self):
        self.sandbox()

    def tearDown(self):
        self.unsandbox()

    def test_the_default_snapshot_is_the_project_not_the_machine(self):
        """本机 profile 里放一个插件，平台快照必须**看不到**它。"""
        self.write_profile(self.machine, "desktop",
                           {"dsh-purge": "github:YuJunZhiXue/dsh-purge#abc"},
                           package_meta={"dsh-purge": {
                               "name": "dsh-purge", "version": "9.9.9",
                               "description": "红队插件"}})
        snap = dshplugins.snapshot()
        self.assertTrue(snap["isolated"])
        self.assertEqual(snap["home"], self.platform)
        self.assertNotEqual(snap["home"], self.machine)
        self.assertEqual([p["name"] for p in snap["plugins"]], [])

    def test_a_declared_but_missing_home_does_not_fall_back_to_the_machine(self):
        """**最关键的一条**：项目 home 还不存在时，绝不能退回本机。

        "目录不存在"恰恰是第一次安装之前的状态，也正是最需要隔离的时刻。
        退回本机的后果是第一次安装就落到别人的 `~/.dsh` 上。
        """
        self.write_profile(self.machine, "desktop", {"@howmp/dsh-pentest": "1.0.0"},
                           package_meta={"@howmp/dsh-pentest": {
                               "name": "@howmp/dsh-pentest", "version": "1.0.0"}})
        snap = dshplugins.snapshot()
        self.assertFalse(snap["available"])
        self.assertTrue(snap["isolated"])
        self.assertEqual(snap["home"], self.platform)
        self.assertEqual(snap["plugins"], [])
        # **关键**："还一个都没装"是正常状态，不能进 `errors`。
        # 进了就会在界面上重复报同一件事（前端自己也会说一句），更要紧的是让人
        # 分不清"集成坏了"和"还空着"——而这两件事的下一步动作完全不同。
        self.assertEqual(snap["errors"], [],
                         "空目录是正常状态，不该当成错误报出来")
        # 但路径要在：使用者需要知道去哪儿装、去哪儿看。
        self.assertEqual(snap["profileDir"],
                         os.path.join(self.platform, "profiles",
                                      dshplugins.PLATFORM_PROFILE))

    def test_unconfigured_scans_nothing_instead_of_the_machine_home(self):
        """未声明平台 home 时**什么都不扫**，并把理由说清。

        从前的行为是"退回本机 home 并标记 `isolated: False`"——那让"平台是独立的"
        这句话在最需要它的时候（还没配置、或控制面还没起来）不成立，而症状是设置页
        列出了别人装的插件，看着像功能。现在唯一的例外是**调用方显式传了 home**。
        """
        dshplugins.set_platform_home("")
        snap = dshplugins.snapshot()
        self.assertFalse(snap["isolated"])
        self.assertEqual(snap["home"], "", "没有声明就不该有任何扫描对象")
        self.assertFalse(snap["available"])
        self.assertEqual(snap["plugins"], [])
        self.assertEqual(snap["profiles"], [])
        self.assertTrue(any("不退回电脑上那份" in e for e in snap["errors"]),
                        snap["errors"])
        # 哨兵必须不出现——这一条才是"真的没读"
        self.assertNotIn(self.sentinel, json.dumps(snap, ensure_ascii=False))

    def test_the_profile_name_is_pinned_so_a_second_profile_cannot_hijack_it(self):
        """平台 home 里只认自己那一个 profile 名。

        `active_profile()` 的兜底是"最近改动过的那个"，一旦这个 home 里多出一个
        profile，它就会把目录指到别处——而那表现为"插件突然全不见了"。
        """
        self.write_profile(self.platform, "something-else", {},
                           package_meta={})
        # 让别的那个看起来"更新"，以便把启发式钓出来
        stamp = time.time() + 10
        os.utime(os.path.join(self.platform, "profiles", "something-else",
                              "package.json"), (stamp, stamp))
        snap = dshplugins.snapshot()
        self.assertEqual(snap["profile"], dshplugins.PLATFORM_PROFILE)
        # 诱饵确实在磁盘上（否则这条用例什么都没验），但被无视了：
        # `profiles` 是**实际存在**的清单，而 `profile` 是平台认定的那一个。
        self.assertEqual(snap["profiles"], ["something-else"])
        self.assertEqual(snap["profileDir"],
                         os.path.join(self.platform, "profiles",
                                      dshplugins.PLATFORM_PROFILE))
        self.assertFalse(snap["available"], "我们的 profile 还没建，不该报可用")

    def test_platform_profile_dir_is_reported_before_it_exists(self):
        self.assertEqual(dshplugins.platform_profile_dir(),
                         os.path.join(self.platform, "profiles",
                                      dshplugins.PLATFORM_PROFILE))


class NoMachineReadTestCase(_Sandbox, unittest.TestCase):
    """平台**读都不读**电脑上那份 DSH —— 这一类断言盯的就是"读"。

    为什么不能只验"文件没被写"：读错家这件事不影响任何文件的 mtime，也不产生
    任何错误码。它的唯一症状是**返回值里多出了本不该有的东西**，所以在诱饵 home
    里放一个哨兵，然后用四个互不相同的角度去问同一件事：它会不会漏出来。

    1. 有平台 home 时，快照与接口载荷里都没有哨兵；
    2. `$DSH_HOME` / `$DSH_PROFILE` 指向诱饵，也改不了结果（这是老实现的实际入口）；
    3. 那几个"本机"函数**已经不存在了**（防止有人照着老代码加回来）；
    4. 诱饵那棵树的内容与时间戳一个字节没动。
    """

    def setUp(self):
        self.sandbox()

    def tearDown(self):
        self.unsandbox()

    def test_the_machine_home_exposes_no_way_in(self):
        """第 3 条：接口面。老实现靠 `machine_home()` / `machine_plugins()` 进来，
        它们必须不在 —— 名字还在就一定会有人再调一次。"""
        for name in ("machine_home", "machine_plugins", "machine_profile_dir",
                     "_machine_inventory", "_MACHINE_CACHE"):
            self.assertFalse(hasattr(dshplugins, name),
                             "`%s` 又回来了：它读的是另一个软件的 home" % name)

    def test_the_snapshot_never_contains_the_machine_sentinel(self):
        """第 1 条。哨兵在诱饵的依赖名、版本号与描述里各出现一次。"""
        snap = dshplugins.snapshot()
        blob = json.dumps(snap, ensure_ascii=False)
        self.assertNotIn(self.sentinel, blob)
        self.assertEqual(snap["home"], self.platform)
        self.assertTrue(snap["isolated"])

    def test_the_environment_cannot_point_it_at_the_machine(self):
        """第 2 条：`$DSH_HOME` 指向诱饵时，平台仍然只看自己那一份。

        这是老实现真正的入口：平台完全可能被 DSH 自己拉起来（那时 `DSH_HOME` 正
        指着使用者的家目录），于是"读了什么"取决于启动方式。沙箱里 `DSH_HOME`
        就是诱饵，而结果必须与它无关。
        """
        self.assertEqual(os.environ.get("DSH_HOME"), self.machine,
                         "沙箱没生效，这条用例什么都没验")
        snap = dshplugins.snapshot()
        self.assertEqual(snap["home"], self.platform)
        self.assertNotIn(self.machine, json.dumps(snap, ensure_ascii=False))

    def test_scanning_does_not_disturb_the_machine_tree(self):
        """第 4 条：内容与时间戳。读不改 mtime，但**写**会——这一条盯的是写。"""
        def tree():
            rows = {}
            for base, _dirs, files in os.walk(self.machine):
                for name in files:
                    path = os.path.join(base, name)
                    with open(path, "rb") as fh:
                        rows[os.path.relpath(path, self.machine)] = (
                            fh.read(), os.path.getmtime(path))
            return rows

        before = tree()
        dshplugins.snapshot(refresh=True)
        # 走一遍接口那条路：载荷里曾经专门有一项是为"迁移"准备的本机清单
        plane = server.ControlPlane(self.store, WEB_DIR)
        public = plane._dsh_public(dshplugins.snapshot(refresh=True))
        self.assertEqual(tree(), before, "诱饵那棵树被改过")
        self.assertNotIn("machinePlugins", public)
        self.assertNotIn("machineHome", public)
        self.assertNotIn(self.sentinel, json.dumps(public, ensure_ascii=False))


# ---------------------------------------------------------------- 队列路径

class QueueRefusalTestCase(_Sandbox, unittest.TestCase):
    """队列路径上的 fail-closed，**不替换 `hub.install`**。

    这一条刻意只在最深处打桩（真正启动进程的 `run_cli`）。把 `hub.install` 换掉会
    顺手把守卫本身也换掉，于是测到的只是"假函数被调用了"，而真正要回答的问题——
    "没有 home 时会不会落到电脑上那份 DSH"——根本没被验证。这个深度差一寸，
    整个用例就没有意义了。
    """

    def setUp(self):
        self.sandbox()
        self._saved_run_cli = hub.run_cli
        self._saved_ensure = hub.ensure_profile
        self.launched = []
        hub.run_cli = lambda args, **kwargs: (
            self.launched.append((list(args), dict(kwargs))) or
            {"ok": True, "command": "", "exitCode": 0, "output": "模拟输出",
             "reason": "", "ms": 0})
        hub.ensure_profile = lambda dsh_home, profile, proxy="", timeout=0: {
            "ok": True, "created": False, "dir": "", "reason": ""}

    def tearDown(self):
        hub.run_cli = self._saved_run_cli
        hub.ensure_profile = self._saved_ensure
        self.unsandbox()

    def _drain(self, timeout=6.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            ops = self.store.plugin_ops(5)
            if ops and all(item["status"] != "running" for item in ops):
                return ops
            time.sleep(0.05)
        return self.store.plugin_ops(5)

    def test_the_queue_refuses_and_launches_nothing_without_a_home(self):
        runner = hub.OpRunner(self.store, self.root)          # 故意不给 home
        runner.submit("install", "is-odd", dshplugins.PLATFORM_PROFILE, "u1")
        ops = self._drain()
        self.assertEqual(ops[0]["status"], "failed")
        self.assertIn("不允许动电脑上那份 DSH", ops[0]["reason"])
        self.assertEqual(self.launched, [], "没有 home 时进程根本不该被启动")

    def test_the_queue_launches_with_the_platform_home(self):
        """对照组：给了 home 就正常执行，而且带的是**平台那个** home。"""
        runner = hub.OpRunner(self.store, self.root,
                              dsh_home_provider=dshplugins.platform_home)
        runner.submit("install", "is-odd", dshplugins.PLATFORM_PROFILE, "u1")
        ops = self._drain()
        self.assertEqual(ops[0]["status"], "done", ops[0]["reason"])
        self.assertEqual(len(self.launched), 1)
        _args, kwargs = self.launched[0]
        self.assertEqual(kwargs.get("dsh_home"), dshplugins.platform_home())


# ---------------------------------------------------------------- 服务端接线

class PluginIsolationApiTestCase(_Sandbox, unittest.TestCase):

    def setUp(self):
        self.sandbox()
        self._saved_install = hub.install
        self._saved_ensure = hub.ensure_profile
        self.calls = []
        hub.install = lambda data_dir, target, profile="", proxy="", timeout=0, \
            dsh_home="": (self.calls.append((target, profile, dsh_home)) or
                          {"ok": True, "action": "install", "target": target,
                           "exitCode": 0, "output": "模拟", "reason": ""})
        hub.ensure_profile = lambda dsh_home, profile, proxy="", timeout=0: {
            "ok": True, "created": False, "dir": "", "reason": ""}
        self.httpd, self.client = support.start(self.store, WEB_DIR)
        # `support.start` 会按真实启动路径重设平台 home（`<data_dir>/dsh-home`），
        # 所以要在起服务**之后**再读一次：断言必须对着控制面真正在用的那个目录，
        # 而不是我们自己造的那个。读错了值，这一组用例会全都"通过"而什么都没验。
        self.platform = dshplugins.platform_home()

    def tearDown(self):
        hub.install = self._saved_install
        hub.ensure_profile = self._saved_ensure
        self.httpd.shutdown()
        self.httpd.server_close()
        self.unsandbox()

    def _drain(self, timeout=6.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            ops = self.store.plugin_ops(5)
            if ops and all(item["status"] != "running" for item in ops):
                return ops
            time.sleep(0.05)
        return self.store.plugin_ops(5)

    def test_the_plane_puts_its_dsh_home_under_the_data_directory(self):
        """插件是运行期状态，就放进运行期数据目录：交付包不带 `data/`，
        所以插件不会跟着仓库跑，也不会有人把别人的插件目录提交上来。"""
        plane = server.ControlPlane(self.store, WEB_DIR)
        self.assertEqual(plane.dsh_home,
                         os.path.join(plane.data_dir,
                                      dshplugins.PLATFORM_HOME_DIR))
        self.assertEqual(dshplugins.platform_home(), plane.dsh_home)

    def test_the_cli_is_only_ever_pointed_at_the_project_home(self):
        """走一遍真实入队路径，断言真的执行的安装拿到的是平台 home。"""
        status, payload = self.client.post(
            "/api/v1/hub/install", {"target": "is-odd"})
        self.assertEqual(status, 202, payload)
        self._drain()
        self.assertEqual(len(self.calls), 1)
        target, profile, home = self.calls[0]
        self.assertEqual(target, "is-odd")
        self.assertEqual(profile, dshplugins.PLATFORM_PROFILE)
        self.assertEqual(home, dshplugins.platform_home())
        self.assertTrue(home.startswith(self.root), home)

    def test_a_client_supplied_foreign_profile_is_refused_without_any_side_effect(self):
        """**这道门不能重新打开**：隔离之前 `profile` 由请求体决定，那时它指的就是
        电脑上那份 DSH 的 profile。现在允许改它，等于把刚关上的门再开一次。"""
        status, payload = self.client.post("/api/v1/hub/install",
                                          {"target": "is-odd", "profile": "desktop"})
        self.assertEqual(status, 400)
        self.assertIn("只允许装进项目内的 profile", payload["error"])
        self.assertEqual(self.calls, [], "被拒的请求不该真的去装")
        self.assertEqual(self.store.plugin_ops(5), [], "被拒的请求不该留下记录")

    def test_removal_is_pinned_to_the_same_profile(self):
        status, _payload = self.client.post("/api/v1/hub/remove",
                                           {"name": "dsh-purge", "profile": "web"})
        self.assertEqual(status, 400)
        self.assertEqual(self.store.plugin_ops(5), [])

    def test_adopt_is_gone_with_a_reason_not_a_404(self):
        """`/hub/adopt` 整条撤掉了（它的 spec 只能从本机依赖记录里来）。

        端点**留着并回 410**，而不是让路由消失：老脚本打到这里时，
        "这个功能被有意撤掉了、请改用插件市场"与"404 说明我拼错了路径"
        是两件不同的事，而 404 会把第二种解释摆在前面。
        """
        dshplugins.invalidate()
        status, payload = self.client.post("/api/v1/hub/adopt", {"name": "dsh-purge"})
        self.assertEqual(status, 410, payload)
        self.assertIn("不再读取电脑上那份 DSH", payload["error"])
        self.assertEqual(self.store.plugin_ops(5), [], "被拒的请求不该留下记录")
        self.assertEqual(self.calls, [], "被拒的请求不该真的去装")

    def test_adopt_is_not_reachable_without_a_session(self):
        anon = support.Client(self.client.base)
        status, _payload = anon.post("/api/v1/hub/adopt", {"name": "dsh-purge"})
        self.assertEqual(status, 401)

    def test_clearing_ops_keeps_the_running_one_and_says_so(self):
        """默认只清已终结的，而且**把"留了几条"报出来**。

        `running` 那一条是"这台机器上此刻正在跑一次 pnpm"的**唯一痕迹**——
        `record_plugin_op` 的整个理由就是**先记后做**。清掉它，一次跑到一半的安装
        就变成查无此事：`finish_plugin_op` 回来时 UPDATE 不到任何行，`rowcount == 0`，
        而那个返回值只被日志用到，界面上一片安静。

        `kept` 也必须报：只说"已清空"，用户会以为那条还在跑的也一并没了。
        """
        done = self.store.record_plugin_op("install", "is-odd", "p", "u1")
        self.store.finish_plugin_op(done, True, "ok")
        bad = self.store.record_plugin_op("install", "nope", "p", "u1")
        self.store.finish_plugin_op(bad, False, "", "装不上", 1)
        live = self.store.record_plugin_op("remove", "is-odd", "p", "u1")

        status, payload = self.client.post("/api/v1/hub/ops/clear", {})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["deleted"], 2)
        self.assertEqual(payload["kept"], 1)
        self.assertEqual([op["id"] for op in self.store.plugin_ops(10)], [live],
                         "执行中那一条被清掉了，而它还在跑")

    def test_all_true_is_the_only_way_to_clear_a_running_record(self):
        """要连执行中一起清，必须自己写出来——默认不能替人做这个决定。"""
        live = self.store.record_plugin_op("install", "is-odd", "p", "u1")
        status, payload = self.client.post("/api/v1/hub/ops/clear", {"all": True})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["deleted"], 1)
        self.assertEqual(self.store.plugin_ops(10), [],
                         "all=true 时 %s 也该被清掉" % live)

    def test_clearing_with_nothing_to_clear_is_not_an_error(self):
        """空表上点"清空"是很正常的一次点击，不该回一个错误。"""
        status, payload = self.client.post("/api/v1/hub/ops/clear", {})
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload["deleted"], payload["kept"]), (0, 0))

    def test_status_hands_the_user_the_platform_paths_and_nothing_else(self):
        """隔离不该只是一句承诺：路径要摊开，使用者才能自己看出"装到哪儿去了"。

        同时钉住**反面**：这份响应里不再出现电脑上那份 DSH 的任何路径。它曾经有
        （`machineHome` / `machineProfileDir`），而那两行正是"只读也要读"的入口。
        """
        status, payload = self.client.get("/api/v1/dsh")
        self.assertEqual(status, 200)
        iso = payload["isolation"]
        self.assertEqual(iso["mode"], "project")
        self.assertEqual(iso["home"], dshplugins.platform_home())
        self.assertEqual(iso["profile"], dshplugins.PLATFORM_PROFILE)
        self.assertEqual(iso["profileDir"], dshplugins.platform_profile_dir())
        self.assertIn("不读、不写、不看", iso["note"])
        self.assertNotIn("machineHome", iso)
        self.assertNotIn("machineProfileDir", iso)
        self.assertTrue(payload["dsh"]["isolated"])
        self.assertEqual(payload["dsh"]["platformHome"], dshplugins.platform_home())
        self.assertNotIn(self.machine, json.dumps(payload, ensure_ascii=False))

    def test_the_public_dsh_payload_carries_no_machine_inventory(self):
        """载荷里不再有"电脑上装了哪些插件"这一项。

        它曾经是界面上"一键迁移"的依据；那个入口撤掉之后，留着这份清单等于留着
        一条没人看、但仍在读别人家目录的代码路径。
        """
        dshplugins.invalidate()
        _status, payload = self.client.get("/api/v1/dsh")
        self.assertNotIn("machinePlugins", payload["dsh"])
        self.assertNotIn(self.sentinel, json.dumps(payload, ensure_ascii=False))


# ---------------------------------------------------------------- 写路径门禁

class NonAdminPluginWriteTestCase(unittest.TestCase):
    """非管理员点不动任何一条插件写路径。

    这里的权限模型与 Agent 预设**不同**，理由要说清：预设是共享目录，改错了别人
    下次对话跟着变；而插件安装是**在本机跑包管理器**——它能执行代码。所以这一组
    不能只验"看不看得见按钮"，要验服务端在鉴权层就挡住，且**不留下任何记录**。
    """

    @classmethod
    def setUpClass(cls):
        cls.dir, cls.store = support.temporary_store("iso-auth.db")
        cls._saved_platform = dshplugins.platform_home()
        cls.httpd, cls.admin = support.start(cls.store, WEB_DIR)
        status, payload = cls.admin.post("/api/v1/users", {
            "username": "viewer2", "password": "Viewer-Pass-456",
            "role": "operator"})
        if status != 201:
            raise RuntimeError("建测试用户失败：%s %s" % (status, payload))
        base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        cls.viewer = support.Client(base)
        if cls.viewer.login("viewer2", "Viewer-Pass-456")[0] != 200:
            raise RuntimeError("测试用户登录失败")

    @classmethod
    def tearDownClass(cls):
        support.stop(cls.httpd, cls.store, cls.dir)
        # `support.start` 把平台 home 指向了这个用例的临时目录，托底还原：
        # 它是进程级状态，留着会让后面所有用例都在用错的 home。
        dshplugins.set_platform_home(cls._saved_platform)

    def test_every_plugin_write_path_is_admin_only(self):
        cases = [("/api/v1/hub/install", {"target": "is-odd"}),
                 ("/api/v1/hub/remove", {"name": "is-odd"}),
                 ("/api/v1/hub/adopt", {"name": "dsh-purge"}),
                 # 清空记录也是写路径，而且**跨用户**：安装队列是全部署共享的，
                 # 清掉就等于把别人的历史一起删了。它不是"顺手加的一个按钮"。
                 ("/api/v1/hub/ops/clear", {})]
        for path, body in cases:
            status, _payload = self.viewer.post(path, body)
            self.assertEqual(status, 403, path)
        self.assertEqual(self.store.plugin_ops(10), [],
                         "被拒的请求不得留下任何安装记录")

    def test_an_operator_can_still_read_the_status(self):
        """收窄的是写路径，不是整个插件页——只读信息本来就不该要管理员。"""
        status, payload = self.viewer.get("/api/v1/dsh")
        self.assertEqual(status, 200)
        self.assertIn("isolation", payload)
        status, _payload = self.viewer.get("/api/v1/hub/installed")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()