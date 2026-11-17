# -*- coding: utf-8 -*-
"""会话工作区：默认 cwd 必须落在 `workspace/sessions/<会话>`，绝不在项目根目录。

为什么单独一个测试文件：这条规则的全部价值在于**模型写文件时真的会走到那个目录**。
只测 `workspace.ensure()` 返回什么字符串，等于测了一个没人用的函数——曾经的真实
形态是 `_cwd_of()` 返回 `os.getcwd()`，而 `os.getcwd()` 恰好是项目根。
"""
import os
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import chat, terminal, workspace          # noqa: E402
from tests import support                              # noqa: E402


class WorkspacePathTestCase(unittest.TestCase):
    """根目录怎么定：跟着数据库走，但部署形态（库在 `data/` 下）提到项目根。"""

    def test_root_is_raised_from_data_dir(self):
        base = tempfile.mkdtemp(prefix="ws-")
        try:
            fake = types.SimpleNamespace(path=os.path.join(base, "data", "pentest.db"))
            self.assertEqual(os.path.normcase(workspace.root(fake)),
                             os.path.normcase(os.path.join(base, workspace.DIRNAME)))
        finally:
            os.rmdir(base)

    def test_root_sits_beside_a_custom_database(self):
        base = tempfile.mkdtemp(prefix="ws-")
        try:
            fake = types.SimpleNamespace(path=os.path.join(base, "db", "x.db"))
            self.assertEqual(os.path.normcase(workspace.root(fake)),
                             os.path.normcase(os.path.join(base, "db", workspace.DIRNAME)))
        finally:
            os.rmdir(base)

    def test_environment_override_wins(self):
        base = tempfile.mkdtemp(prefix="ws-env-")
        old = os.environ.get(workspace.ENV_DIR)
        os.environ[workspace.ENV_DIR] = base
        try:
            self.assertEqual(os.path.normcase(workspace.root(None)),
                             os.path.normcase(base))
        finally:
            if old is None:
                os.environ.pop(workspace.ENV_DIR, None)
            else:
                os.environ[workspace.ENV_DIR] = old
            os.rmdir(base)

    def test_no_database_never_writes_into_the_repository(self):
        """没有数据库（单元测试、纯工具调用）时落临时目录。"""
        path = workspace.root(None)
        self.assertNotEqual(os.path.normcase(os.path.abspath(path))[:len(ROOT)],
                            os.path.normcase(ROOT))
        self.assertIn("pentest-platform", path)

    def test_session_id_cannot_escape_the_workspace(self):
        for hostile in ("../../data", "..", "C:/Windows", "a/b\\c", "   ", None):
            label = workspace.safe_id(hostile)
            self.assertNotIn("..", label)
            self.assertNotIn("/", label)
            self.assertNotIn("\\", label)
            target = workspace.session_dir(None, hostile)
            inside = os.path.normcase(os.path.abspath(target)).startswith(
                os.path.normcase(os.path.abspath(workspace.root(None))))
            self.assertTrue(inside, "会话 id %r 逃出了工作区：%s" % (hostile, target))

    def test_two_sessions_get_two_directories(self):
        base = tempfile.mkdtemp(prefix="ws-")
        old = os.environ.get(workspace.ENV_DIR)
        os.environ[workspace.ENV_DIR] = base
        try:
            plain = types.SimpleNamespace(path="")
            first = workspace.ensure(plain, "C-aaaa1111")
            second = workspace.ensure(plain, "C-bbbb2222")
            self.assertTrue(os.path.isdir(first) and os.path.isdir(second))
            self.assertNotEqual(os.path.normcase(first), os.path.normcase(second))
            summary = workspace.info(plain)
            self.assertGreaterEqual(summary["sessionCount"], 2)
        finally:
            if old is None:
                os.environ.pop(workspace.ENV_DIR, None)
            else:
                os.environ[workspace.ENV_DIR] = old
            import shutil
            shutil.rmtree(base, ignore_errors=True)


class CommandCwdTestCase(unittest.TestCase):
    """`run_command` 省略 cwd 时，命令真的在会话工作区里执行。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("ws-run.db")
        self.plane = types.SimpleNamespace(store=self.store)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _write_marker(self, name="mark.txt"):
        if os.name == "nt":
            return "cmd", "echo one> %s" % name
        return "sh", "echo one > %s" % name

    def test_default_cwd_is_the_session_workspace(self):
        shell, command = self._write_marker()
        outcome = terminal.run(self.plane, {"command": command, "shell": shell},
                               session_id="C-ws000001")
        cwd = outcome["data"]["cwd"]
        self.assertEqual(os.path.normcase(cwd),
                         os.path.normcase(workspace.session_dir(self.plane,
                                                                "C-ws000001")))
        self.assertTrue(os.path.isfile(os.path.join(cwd, "mark.txt")),
                        "文件没有落在会话工作区：%s" % cwd)
        # 反过来钉住：项目根目录里不许出现这个文件（旧实现就落在那儿）。
        self.assertFalse(os.path.exists(os.path.join(ROOT, "mark.txt")))

    def test_explicit_cwd_still_wins(self):
        target = tempfile.mkdtemp(prefix="ws-explicit-")
        try:
            outcome = terminal.run(self.plane, {"command": "echo hi", "cwd": target},
                                   session_id="C-ws000002")
            self.assertEqual(os.path.normcase(outcome["data"]["cwd"]),
                             os.path.normcase(target))
        finally:
            import shutil
            shutil.rmtree(target, ignore_errors=True)

    def test_missing_cwd_is_an_error_not_a_silent_creation(self):
        """拼错路径要报错。静默建目录会让命令跑在别处而输出看起来正常。"""
        missing = os.path.join(tempfile.gettempdir(), "ws-does-not-exist-xyz")
        with self.assertRaises(terminal.TerminalError):
            terminal.run(self.plane, {"command": "echo hi", "cwd": missing},
                         session_id="C-ws000003")
        self.assertFalse(os.path.isdir(missing))

    def test_background_command_gets_the_same_default(self):
        outcome = terminal.start_background(self.plane, {"command": "echo bg"},
                                            session_id="C-ws000004")
        self.assertEqual(os.path.normcase(outcome["data"]["cwd"]),
                         os.path.normcase(workspace.session_dir(self.plane,
                                                                "C-ws000004")))
        deadline = time.time() + 10
        while time.time() < deadline:
            row = self.store.get_terminal(outcome["data"]["terminalId"])
            if row and row["status"] != "running":
                break
            time.sleep(0.05)


class ToolDescriptionTestCase(unittest.TestCase):
    """工具描述是模型唯一能读到这条规则的地方，措辞要跟着实现一起变。"""

    def test_run_command_says_where_files_go(self):
        # `tool_catalog()` 只给参数名（界面要的那一层），完整 schema 在 `chat.TOOLS`。
        spec = next(tool for tool in chat.TOOLS if tool["name"] == "run_command")
        text = str(spec["parameters"]["properties"]["cwd"].get("description") or "")
        self.assertIn("workspace/sessions", text)
        self.assertIn("根目录", text)
        self.assertIn("workspace/cache", text)


class EngineMemoryTestCase(unittest.TestCase):
    """发掘引擎的记忆库不许按"进程当前目录"落。

    引擎自己的默认值是相对路径 `memory/cases.db`，控制面从项目根启动——忘了传
    `memory_path` 的调用点就会在根目录写出一个 `memory/`。这是实测复现过的
    （全量测试跑完根目录又长回来一次），所以钉住"平台侧接管默认值"。
    """

    def test_run_scan_defaults_the_memory_into_the_workspace_cache(self):
        import inspect
        from pentestd import engine, workspace

        source = inspect.getsource(engine.run_scan)
        self.assertIn("if not memory_path", source,
                      "run_scan 没有接管缺省的记忆库路径")
        self.assertIn("workspace.cache()", source,
                      "缺省记忆库必须落在工作区缓存里")
        self.assertIn("engine-cases.db", source)

        cache = os.path.join(workspace.root(), "cache")
        self.assertTrue(os.path.normcase(cache).endswith(
            os.path.normcase(os.path.join("workspace", "cache"))), cache)


if __name__ == "__main__":
    unittest.main()
