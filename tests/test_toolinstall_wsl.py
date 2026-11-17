# -*- coding: utf-8 -*-
"""wsl 渠道：配方形状、argv 构造、探测脚本、以及几条错误路径。

**不联网、不真的跑 WSL。** 所有 `wsl_run` 都被换掉——被测对象是"我们交给 WSL
的东西长什么样"，而不是"WSL 能不能跑"。真跑的那部分在
`tools/verify_wsl_channel.py`（那需要一台有 WSL 的机器）。
"""
import os
import re
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import toolinstall as T     # noqa: E402
from pentestd import toolkit              # noqa: E402

WSL_IDS = ["ncrack", "hydra", "smbclient", "netexec", "evil-winrm", "msfconsole"]


class RecipeShapeTestCase(unittest.TestCase):
    """六条配方必须是 wsl 渠道，而且每条都能独立回答"怎么装"。"""

    def test_the_six_are_wsl_channel_now(self):
        for adapter_id in WSL_IDS:
            spec = T.RECIPES.get(adapter_id)
            self.assertIsNotNone(spec, adapter_id)
            self.assertEqual(spec.get("channel"), T.WSL_CHANNEL, adapter_id)

    def test_every_wsl_recipe_names_a_method_package_and_binary(self):
        for adapter_id in WSL_IDS:
            spec = T.RECIPES[adapter_id]
            self.assertIn(spec.get("method"), T.WSL_METHODS, adapter_id)
            self.assertTrue(str(spec.get("package") or "").strip(), adapter_id)
            self.assertTrue(str(spec.get("binary") or "").strip(), adapter_id)

    def test_netexec_and_evil_winrm_are_not_apt(self):
        """这两个**不是**发行版仓库里的包。写成 apt 会失败，而失败信息是
        "找不到这个包"——那读起来像包名打错，而不是渠道选错。"""
        self.assertEqual(T.RECIPES["netexec"]["method"], "pipx")
        self.assertEqual(T.RECIPES["evil-winrm"]["method"], "gem")

    def test_only_metasploit_carries_a_third_party_repository(self):
        with_repo = [k for k, v in T.RECIPES.items()
                     if isinstance(v.get("repository"), dict) and v["repository"]]
        self.assertEqual(with_repo, ["msfconsole"])
        repo = T.RECIPES["msfconsole"]["repository"]
        for field in ("key", "keyring", "line", "file"):
            self.assertTrue(str(repo.get(field) or "").strip(), field)

    def test_netexec_declares_its_build_requirements(self):
        """它要 Rust 参与构建。少了这一条，pipx 会在编译 Rust 扩展时失败，
        而日志指向的是编译器，不是"缺一条依赖声明"。"""
        requires = T.RECIPES["netexec"].get("requires") or ()
        self.assertIn("rustc", requires)
        self.assertIn("cargo", requires)

    def test_evil_winrm_declares_libreadline(self):
        """`readline-ext` 编不过时的日志是一串 mkmf 输出，指向编译，而不是
        "缺一个发行版包"。"""
        requires = T.RECIPES["evil-winrm"].get("requires") or ()
        self.assertIn("libreadline-dev", requires)

    def test_ssh_is_still_manual(self):
        """它不该被顺手改成 wsl：Windows 自带 OpenSSH 客户端，`_which` 找得到，
        而"已经能用"的东西不该多一条安装渠道。"""
        self.assertEqual(T.RECIPES["ssh"]["channel"], "manual")


class MarkerTestCase(unittest.TestCase):
    """`wsl://` 标记的形状与解析。"""

    def test_split_roundtrip(self):
        # 配方里写的是**命令名**，所以第二段就是命令名。
        self.assertEqual(T.wsl_split("wsl://pentest-linux/hydra"),
                         ("pentest-linux", "hydra"))
        # 配方若写绝对路径，拼接时多出来的那个斜杠正好把它带回来。
        self.assertEqual(T.wsl_split("wsl://pentest-linux//usr/bin/hydra"),
                         ("pentest-linux", "/usr/bin/hydra"))

    def test_resolve_is_empty_when_not_in_inventory(self):
        with mock.patch.object(T, "wsl_inventory", lambda *a, **k: {}):
            self.assertEqual(T.resolve("hydra"), "")

    def test_resolve_is_a_marker_when_present(self):
        with mock.patch.object(T, "wsl_inventory",
                               lambda *a, **k: {"hydra": "/usr/bin/hydra"}):
            self.assertTrue(T.resolve("hydra").startswith("wsl://"))

    def test_command_prefix_is_a_wsl_invocation(self):
        with mock.patch.object(T, "wsl_inventory",
                               lambda *a, **k: {"hydra": "/usr/bin/hydra"}), \
             mock.patch.object(T, "wsl_exe",
                               lambda: r"C:\Windows\System32\wsl.exe"):
            argv = T.command_prefix("hydra")
        # 最后一段是**命令名**，不是探测到的绝对路径：让 WSL 侧的 execvp 去
        # 查 PATH，配方就不必知道工具装在哪个目录。
        self.assertEqual(argv[-1], "hydra")
        self.assertEqual(argv[argv.index("--") - 1], "root")
        self.assertIn("-d", argv)

    def test_marker_uses_the_recipe_binary_not_the_probed_path(self):
        """探测到的路径只用来回答"在不在"，不进 marker。进了的话，同一份配方
        在不同发行版上会生成不同的 marker，而"上次装的是什么"就不可比了。"""
        with mock.patch.object(T, "wsl_inventory",
                               lambda *a, **k: {"hydra": "/weird/place/hydra"}):
            self.assertEqual(T.resolve("hydra"),
                             "wsl://%s/hydra" % T.wsl_distro())


class ProbeScriptTestCase(unittest.TestCase):
    """探测脚本的形状。**这一条是为了一个具体的、极隐蔽的缺陷。**"""

    def _capture(self):
        seen = []

        def fake(argv, **kwargs):
            seen.append(list(argv))
            return {"ok": True, "code": 0, "stdout": "hydra=/usr/bin/hydra\n",
                    "stderr": "", "raw": b"", "output": ""}
        return seen, fake

    def test_probe_uses_inline_substitution_and_no_bare_variable(self):
        """脚本里不许出现 `$name` 形式的变量，必须有 `$(`。

        实测：脚本经 `["sh", "-c", text]` 交给 wsl.exe 时，`$c` / `$x` 这类
        变量在到达 Linux 之前就变成了空串（输出恒为 `[]`），而
        `$(command -v x)` 原样到达并被正确执行。**症状是探测"跑得很成功"，
        只是每个工具都报没装**——与这个函数要回答的问题正好相反。所以它不能
        靠"跑通了"来验证，必须直接看交给 wsl 的那段文本。
        """
        seen, fake = self._capture()
        with mock.patch.object(T, "wsl_run", fake), \
             mock.patch.object(T, "wsl_listed", lambda *a, **k: True):
            T.wsl_forget()
            T.wsl_inventory(refresh=True)
        self.assertTrue(seen, "没有调用 wsl_run，探测根本没发生")
        script = seen[0][-1]
        bare = re.findall(r"\$\{?[A-Za-z_][A-Za-z0-9_]*", script)
        self.assertEqual(bare, [], "探测脚本里有裸变量：%s" % bare)
        self.assertIn("$(", script)
        for adapter_id in WSL_IDS:
            binary = T.RECIPES[adapter_id]["binary"]
            self.assertIn(binary, script)

    def test_probe_is_one_call_for_all_tools(self):
        """**一次**问完。逐个问就是十几次 wsl 往返，全部落在打开状态页上。"""
        seen, fake = self._capture()
        with mock.patch.object(T, "wsl_run", fake), \
             mock.patch.object(T, "wsl_listed", lambda *a, **k: True):
            T.wsl_forget()
            T.wsl_inventory(refresh=True)
        self.assertEqual(len(seen), 1, "探测发了 %d 次 wsl 调用" % len(seen))

    def test_inventory_is_cached_until_forgotten(self):
        seen, fake = self._capture()
        with mock.patch.object(T, "wsl_run", fake), \
             mock.patch.object(T, "wsl_listed", lambda *a, **k: True):
            T.wsl_forget()
            first = T.wsl_inventory(refresh=True)
            second = T.wsl_inventory()
            T.wsl_inventory()
        self.assertEqual(len(seen), 1)
        self.assertEqual(first, second)
        T.wsl_forget()

    def test_no_distro_means_no_probe_and_no_error(self):
        """发行版不在时**不发指令**，也不抛——`detect()` 会为每个适配器走到
        这里，而"WSL 还没装"不该让状态页整个起不来。"""
        with mock.patch.object(T, "wsl_listed", lambda *a, **k: False), \
             mock.patch.object(T, "wsl_run",
                               side_effect=AssertionError("不该被调用")):
            T.wsl_forget()
            self.assertEqual(T.wsl_inventory(refresh=True), {})


class InstallPathTestCase(unittest.TestCase):
    """几条失败路径，每一条的下一步都不同。"""

    def test_without_a_distro_it_points_at_the_setup_script(self):
        with mock.patch.object(T, "wsl_exe", lambda: r"C:\wsl.exe"), \
             mock.patch.object(T, "wsl_listed", lambda *a, **k: False):
            with self.assertRaises(T.InstallError) as caught:
                T._install_wsl("hydra", T.RECIPES["hydra"], T.tools_root(), 10.0)
        message = str(caught.exception)
        self.assertIn("wsl_setup.py", message)
        self.assertIn(T.wsl_distro(), message)

    def test_without_wsl_exe_it_says_how_to_enable_it(self):
        with mock.patch.object(T, "wsl_exe", lambda: ""):
            with self.assertRaises(T.InstallError) as caught:
                T._install_wsl("hydra", T.RECIPES["hydra"], T.tools_root(), 10.0)
        self.assertIn("wsl --install", str(caught.exception))

    def test_a_package_name_with_whitespace_is_refused(self):
        spec = dict(T.RECIPES["hydra"], package="hydra --evil")
        with mock.patch.object(T, "wsl_exe", lambda: r"C:\wsl.exe"), \
             mock.patch.object(T, "wsl_listed", lambda *a, **k: True):
            with self.assertRaises(T.InstallError) as caught:
                T._install_wsl("hydra", spec, T.tools_root(), 10.0)
        self.assertIn("空白", str(caught.exception))

    def test_pipx_runs_with_an_explicit_bin_dir(self):
        """pipx 默认装到 `~/.local/bin`，而那个目录不在 PATH 上。不指到
        `/usr/local/bin`，"装成功了"与"没装"在界面上一模一样。"""
        calls = []

        def fake(argv, **kwargs):
            calls.append((list(argv), dict(kwargs.get("env") or {})))
            return {"ok": True, "code": 0, "stdout": "", "stderr": "",
                    "raw": b"", "output": ""}

        with mock.patch.object(T, "wsl_run", fake), \
             mock.patch.object(T, "wsl_listed", lambda *a, **k: True), \
             mock.patch.object(T, "wsl_inventory",
                               lambda *a, **k: {"nxc": "/usr/local/bin/nxc"}), \
             mock.patch.object(T, "_wsl_version", lambda *a, **k: "1.0"), \
             mock.patch.object(T, "_load_registry",
                               lambda root: {"schema": T.REGISTRY_SCHEMA,
                                             "tools": {}}), \
             mock.patch.object(T, "_save_registry", lambda root, doc: None):
            T._install_wsl("netexec", T.RECIPES["netexec"], T.tools_root(), 10.0)
        pipx = [env for argv, env in calls if argv[:1] == ["pipx"]]
        self.assertEqual(len(pipx), 1, "pipx 没有被调用一次")
        self.assertEqual(pipx[0].get("PIPX_BIN_DIR"), "/usr/local/bin")

    def test_a_binary_that_never_appears_is_an_error_not_a_success(self):
        """装完之后 `command -v` 仍然找不到它——这**必须报错**。报成成功的
        话，下一步是使用者拿着一个"已安装"的徽章去用，而工具根本不存在。"""
        with mock.patch.object(T, "wsl_run",
                               lambda *a, **k: {"ok": True, "code": 0,
                                                "stdout": "", "stderr": "",
                                                "raw": b"", "output": ""}), \
             mock.patch.object(T, "wsl_listed", lambda *a, **k: True), \
             mock.patch.object(T, "wsl_inventory", lambda *a, **k: {}), \
             mock.patch.object(T, "_load_registry",
                               lambda root: {"schema": T.REGISTRY_SCHEMA,
                                             "tools": {}}):
            with self.assertRaises(T.InstallError) as caught:
                T._install_wsl("hydra", T.RECIPES["hydra"], T.tools_root(), 10.0)
        self.assertIn("command -v", str(caught.exception))


class RemoveTestCase(unittest.TestCase):

    def test_remove_of_a_gem_tool_does_not_claim_it_removed_anything(self):
        """`gem uninstall` 会连带删掉可能被别的 gem 依赖的库，所以不做——
        但也不能报成"卸掉了"。"""
        with mock.patch.object(T, "wsl_run",
                               side_effect=AssertionError("不该被调用")), \
             mock.patch.object(T, "wsl_listed", lambda *a, **k: True), \
             mock.patch.object(T, "_load_registry",
                               lambda root: {"schema": T.REGISTRY_SCHEMA,
                                             "tools": {}}), \
             mock.patch.object(T, "_save_registry", lambda root, doc: None):
            result = T.remove("evil-winrm")
        self.assertFalse(result["removed"])
        self.assertIn("gem uninstall", result["note"])

    def test_remove_never_unregisters_the_distro(self):
        """删发行版是另一个动作。这里只卸包。"""
        calls = []

        def fake(argv, **kwargs):
            calls.append(list(argv))
            return {"ok": True, "code": 0, "stdout": "", "stderr": "",
                    "raw": b"", "output": ""}

        with mock.patch.object(T, "wsl_run", fake), \
             mock.patch.object(T, "wsl_listed", lambda *a, **k: True), \
             mock.patch.object(T, "_load_registry",
                               lambda root: {"schema": T.REGISTRY_SCHEMA,
                                             "tools": {}}), \
             mock.patch.object(T, "_save_registry", lambda root, doc: None):
            result = T.remove("hydra")
        self.assertTrue(result["removed"])
        flat = " ".join(" ".join(item) for item in calls)
        self.assertNotIn("--unregister", flat)
        self.assertIn("remove", flat)


class LocateTestCase(unittest.TestCase):
    """`toolkit._locate` 对 wsl 工具的来源标注。"""

    def test_source_says_wsl_not_project(self):
        adapter = toolkit.ADAPTER_INDEX["hydra"]
        with mock.patch.object(toolkit.toolinstall, "command_prefix",
                               lambda *a, **k: ["wsl.exe", "-d", "d", "-u",
                                                "root", "--", "hydra"]), \
             mock.patch.object(toolkit.toolinstall, "resolve",
                               lambda *a, **k: "wsl://d/hydra"):
            located = toolkit._locate(adapter)
        self.assertEqual(located["source"], "wsl")
        self.assertTrue(located["path"].startswith("wsl://"))

    def test_a_project_tool_is_still_labelled_project(self):
        adapter = toolkit.ADAPTER_INDEX["chisel"]
        with mock.patch.object(toolkit.toolinstall, "command_prefix",
                               lambda *a, **k: [r"C:\t\chisel.exe"]), \
             mock.patch.object(toolkit.toolinstall, "resolve",
                               lambda *a, **k: r"C:\t\chisel.exe"):
            located = toolkit._locate(adapter)
        self.assertEqual(located["source"], "project")

    def test_a_wsl_marker_is_not_mistaken_for_a_wrapper(self):
        """`wsl://…/hydra` 不以 `.cmd`/`.bat` 结尾，所以不会被当成包装脚本。
        被当成了的话数据档会整个回落到严格档，而症状是"装完工具之后那条以前
        能跑的命令跑不了了"。"""
        marker = "wsl://pentest-linux/usr/bin/hydra"
        self.assertFalse(marker.lower().endswith(toolkit._WRAPPER_SUFFIXES))


class SetupScriptTestCase(unittest.TestCase):

    def test_the_setup_script_exists_and_compiles(self):
        """`_install_wsl` 的错误信息把使用者指到这个文件。它必须真的在，
        而且真的能跑（编译得过）。"""
        path = os.path.join(ROOT, "tools", "wsl_setup.py")
        self.assertTrue(os.path.isfile(path), path)
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        compile(text, path, "exec")
        self.assertIn("--status", text)
        self.assertIn("--force", text)

    def test_the_setup_script_verifies_the_download(self):
        """下载要在 80% 处被服务器切断是实测过的，而那时的文件"看起来差不多
        大"。所以它必须比对官方摘要，而不是比对大小。"""
        path = os.path.join(ROOT, "tools", "wsl_setup.py")
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("SHA256SUMS", text)
        self.assertIn("_sha256", text)
        self.assertIn("摘要不一致", text)


if __name__ == "__main__":
    unittest.main()
