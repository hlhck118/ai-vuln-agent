# -*- coding: utf-8 -*-
"""`pentestd.console` 与两个入口脚本的契约。

**这组用例钉的是一个具体的、可复现的崩溃**：中文 Windows 控制台是 cp936，
而 `deploy/start.py` 与 `pentestd/cli.py` 的横幅里有 `⚠`。打不出来时 Python 抛
`UnicodeEncodeError`，而它抛在 `main()` 里——**进程在开始监听之前就退出了**。
使用者的观察是"加了 `--no-auth` 或 `--host 0.0.0.0` 就起不来"。

所以这里有三层，缺一层就会留下一个看起来通过、实际不设防的口子：

    1. **机制**：`make_safe` 在一个 GBK 流上确实把 `⚠` 变成 `?` 而不是抛异常。
       反面也要断言——不做这件事时**必须**抛，否则"修好了"和"这个字符本来就
       没事"两种情况分不开。
    2. **接线**：两个入口的 `main()` 真的调了它。机制对而没人调，等于没修。
       这条用 AST 判，不用字符串搜索——`start.py` 里那句解释性注释也含
       `make_safe`，`assertIn` 会被自己的注释骗过去。
    3. **端到端**：用 `PYTHONIOENCODING=gbk` 起一个真的 `--no-auth` 实例，
       断言它**真的起来了**。前两层都过了而这一层挂了，说明问题不在我改的地方。
"""
import ast
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import console                                            # noqa: E402


class MakeSafeTestCase(unittest.TestCase):
    """机制本身：打不出的字符退化成 `?`，而不是把异常抛到输出路径上。"""

    @staticmethod
    def _gbk_stream():
        raw = io.BytesIO()
        return raw, io.TextIOWrapper(raw, encoding="gbk")

    def test_a_gbk_stream_really_does_reject_the_warning_sign(self):
        """反面：不调 `make_safe` 时它**必须**抛——否则下一条断言说明不了任何事。

        没有这一条，"修好了"与"这个字符在 GBK 下本来就没事"在测试里长得一模一样。
        """
        _, stream = self._gbk_stream()
        with self.assertRaises(UnicodeEncodeError):
            stream.write("⚠ 警告\n")

    def test_make_safe_downgrades_instead_of_raising(self):
        """`make_safe` 之后同一个字符变成 `?`，后面的中文照旧打得出来。"""
        raw, stream = self._gbk_stream()
        console.make_safe([stream])
        stream.write("⚠ 已关闭认证\n")
        stream.flush()
        text = raw.getvalue().decode("gbk")
        self.assertTrue(text.startswith("? 已关闭认证"), text)
        self.assertNotIn("⚠", text)

    def test_it_is_idempotent(self):
        """重复调用没有额外效果——入口脚本可能走两条路径都调到它。"""
        raw, stream = self._gbk_stream()
        console.make_safe([stream])
        console.make_safe([stream])
        stream.write("✓ 可用\n")
        stream.flush()
        self.assertTrue(raw.getvalue().decode("gbk").startswith("? 可用"))

    def test_a_stream_without_reconfigure_is_left_alone(self):
        """被包装过的输出对象没有 `reconfigure`——**不抛**比改造它重要。

        本模块存在的意义就是"不要在输出的路上抛"，所以它自己更不能抛。
        """

        class Bare(object):
            def write(self, text):
                return len(text)

        console.make_safe([Bare()])          # 不抛即通过
        console.make_safe([None])            # 连 None 也不行

    def test_the_default_is_the_process_streams(self):
        """不传参时改的是进程自己的 stdout/stderr——入口脚本用的就是这个形态。"""
        before_out = sys.stdout.errors
        before_err = sys.stderr.errors
        console.make_safe()
        self.assertEqual(sys.stdout.errors, "replace")
        self.assertEqual(sys.stderr.errors, "replace")
        # 还原，免得影响同一进程里其它用例的输出
        sys.stdout.reconfigure(errors=before_out)
        sys.stderr.reconfigure(errors=before_err)

    @staticmethod
    def _redirected():
        """忠实模拟"重定向到文件"：`TextIOWrapper` 套在**带缓冲**的二进制层上。

        只用 `TextIOWrapper(BytesIO())` 复现不出来——那个组合是直通的，块缓冲
        发生在 `io.BufferedWriter` 那一层，也就是 `open(path, "w")` 给出的东西。
        """
        raw = io.BytesIO()
        return raw, io.TextIOWrapper(io.BufferedWriter(raw, buffer_size=8192),
                                     encoding="utf-8")

    def test_a_redirected_stream_is_line_buffered_so_the_banner_is_visible(self):
        """重定向到文件时，裸 `print()` 也要立刻落盘。

        这不是"好看"的问题。`deploy/start.py` 的横幅是裸 `print()`，重定向之后
        默认块缓冲，于是那张写着**本次生效的探测范围、侵入授权、模型、数据库
        路径**的表会一直躺在缓冲区里，直到进程退出或缓冲写满。使用者去日志里找
        "这次到底放开了什么"，看到的是一片空白——而横幅是部署上唯一会说出这些
        的地方，别处都只是"取默认"。

        现场证据：同一次启动、同一个 stdout，本机节点那条 `print(..., flush=True)`
        的行**在**日志里，横幅**不在**，差别只有 flush。
        """
        raw, stream = self._redirected()
        print("横幅第一行", file=stream)               # 刻意不 flush
        self.assertEqual(raw.getvalue(), b"",
                         "基线不成立：这个替身本来就是直通的，那条断言证明不了任何事")

        raw, stream = self._redirected()
        console.make_safe([stream])
        print("横幅第一行", file=stream)               # 同样不 flush
        # 只比内容，不比行尾：这个替身和真正的 `sys.stdout` 一样会做行尾转换
        # （`\n` → `os.linesep`），那是另一件事，不该混进这条契约里。
        self.assertEqual(raw.getvalue().decode("utf-8").strip(), "横幅第一行")

    def test_a_stream_that_only_accepts_errors_still_gets_the_character_fix(self):
        """只认 `errors` 的输出对象：`line_buffering` 落不了地，字符修复仍要做到。

        两个目标是分开的，一个失败不该带走另一个。真实世界里"只认 errors"的
        对象就在进程边界上（各种日志包装器），而它恰恰是最需要字符修复的地方。
        """

        class ErrorsOnly(object):
            def __init__(self):
                self.calls = []

            def reconfigure(self, errors=None):
                if errors is None:
                    self.calls.append("flush-only")
                    return
                self.calls.append(errors)

        stream = ErrorsOnly()
        console.make_safe([stream])                     # 不抛即通过
        self.assertIn("replace", stream.calls,
                      "line_buffering 不被接受时，errors 那次调用被一起丢了")


class EntryPointsCallItTestCase(unittest.TestCase):
    """接线：两个入口的 `main()` 都真的调了它。

    机制对而没人调，等于没修——而"没人调"在功能测试里是完全看不出来的：
    全量用例都不会去跑 `--no-auth` 的启动横幅。
    """

    ENTRY_POINTS = (
        os.path.join(ROOT, "deploy", "start.py"),
        os.path.join(ROOT, "pentestd", "cli.py"),
    )

    @staticmethod
    def _read(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    @staticmethod
    def _main_calls_make_safe(path):
        tree = ast.parse(EntryPointsCallItTestCase._read(path), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                            and sub.func.attr == "make_safe":
                        return True
        return False

    def test_both_entry_points_call_it_from_main(self):
        """按 AST 判，不按字符串——注释里也会出现 `make_safe` 这个名字。

        连"它必须是 main() 的第一件事"也一起钉住：调用点排在 `print()` 之后的话，
        那个 `print` 就已经先抛了，改与不改没差别。
        """
        for path in self.ENTRY_POINTS:
            with self.subTest(entry=os.path.relpath(path, ROOT)):
                self.assertTrue(self._main_calls_make_safe(path),
                                "%s 的 main() 没有调 console.make_safe()——"
                                "cp936 控制台下它会因为一个 ⚠ 起不来" % path)
                tree = ast.parse(self._read(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == "main":
                        body = [n for n in node.body
                                if not (isinstance(n, ast.Expr)
                                        and isinstance(n.value, ast.Constant))]
                        first_print = next(
                            (i for i, n in enumerate(body)
                             if isinstance(n, ast.Expr)
                             and isinstance(n.value, ast.Call)
                             and getattr(n.value.func, "id", "") == "print"), None)
                        call_index = next(
                            (i for i, n in enumerate(body)
                             if isinstance(n, ast.Expr)
                             and isinstance(n.value, ast.Call)
                             and isinstance(n.value.func, ast.Attribute)
                             and n.value.func.attr == "make_safe"), None)
                        if first_print is not None and call_index is not None:
                            self.assertLess(call_index, first_print,
                                            "make_safe() 排在了第一个 print() 后面，"
                                            "那时横幅已经先抛了")


class ServeSurvivesAGbkConsoleTestCase(unittest.TestCase):
    """端到端：`PYTHONIOENCODING=gbk` 下 `--no-auth` 必须真的起得来。

    这**正是**当初坏掉的那条路径（`deploy/start.py` 横幅里的 `⚠`）。逐字复现：
    输出重定向到文件（不是管道——管道会在子进程写满缓冲时把它卡住）、
    编码强制成 gbk、`--no-auth`、临时库、随机端口。
    """

    def test_no_auth_starts_under_a_gbk_stdout(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        tmp = tempfile.mkdtemp(prefix="console-gbk-")
        out_path = os.path.join(tmp, "out.txt")
        err_path = os.path.join(tmp, "err.txt")
        env = dict(os.environ, PYTHONIOENCODING="gbk", PYTHONUNBUFFERED="1")
        env.pop("PYTHONUTF8", None)
        proc = None
        try:
            with open(out_path, "wb") as out, open(err_path, "wb") as err:
                proc = subprocess.Popen(
                    [sys.executable, os.path.join(ROOT, "deploy", "start.py"),
                     "--no-browser", "--no-auth", "--port", str(port),
                     "--db", os.path.join(tmp, "t.db")],
                    cwd=ROOT, stdout=out, stderr=err, env=env)
            base = "http://127.0.0.1:%d" % port
            up = False
            for _ in range(120):
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
                try:
                    with urllib.request.urlopen(base + "/api/v1/health", timeout=3) as r:
                        if r.status == 200:
                            up = True
                            break
                except Exception:
                    pass
            with open(err_path, "rb") as fh:
                stderr_text = fh.read().decode("utf-8", "replace")
            self.assertNotIn("UnicodeEncodeError", stderr_text, stderr_text[-800:])
            self.assertTrue(up, "GBK 控制台下 --no-auth 没能起来。exit=%s\nstderr=%s"
                            % (proc.poll(), stderr_text[-800:]))
            with urllib.request.urlopen(base + "/api/v1/health", timeout=5) as r:
                self.assertEqual(json.loads(r.read().decode())["status"], "ok")
            # `?` 出现在横幅里 = 降级生效了，而不是那个字符恰好被绕开了。
            # 要 `PYTHONUNBUFFERED`：重定向到文件时 stdout 是块缓冲，横幅会一直
            # 躺在缓冲区里直到进程退出——不加它，这条断言会去断言一个空文件。
            with open(out_path, "rb") as fh:
                banner = fh.read().decode("gbk", "replace")
            self.assertIn("? 已监听所有网卡或关闭认证", banner)
            self.assertNotIn("⚠", banner)
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class RealDeployDisablesTheDshHostTestCase(unittest.TestCase):
    """真部署调用**不再需要**关掉任何 DSH 宿主 —— 因为平台不再拉起它。

    这一段历史值得留着，它是这个类存在的原因：`deploy/start.py` 曾经默认把自己的
    DSH 网页版宿主一起拉起来（那是**部署**时的正确行为），而测试里"正确行为"会变成
    **每跑一次就多一个活着的 `dsh web` 进程**：用例结束时平台被 kill，而宿主是孙
    进程、收不到那个信号，于是它继续初始化一个临时 profile 并留在机器上。

    真机实测（2026-10-16）：一次全量跑完，`pentest-web --from-default-profile web`
    的宿主从 8792 一路排到 8799，跨了三次全量；它们不报错、不监听、只是活着。

    那一半被整体删掉之后（界面进了插件，宿主是使用者自己的 DSH），**没有可关的
    东西了**，所以这条守卫改成钉"没有可关的东西"这个事实本身：真部署调用不带那个
    已删除的实参，而且 `pentestd/` 里不再有那两个模块。
    """

    def test_no_deploy_path_starts_a_dsh_host(self):
        """真部署调用**不再**需要关掉什么 —— 因为平台不再拉起 DSH 宿主。

        这条守卫的前身钉的是"每条真部署调用都要带 `--no-dsh-web`"。平台把那一半
        整体删掉之后，那条断言只会让所有的真部署调用传一个不存在的参数（启动即
        `unrecognized arguments`）——**能力没了、守卫还在守旧事实**，是这一轮里
        最容易留下的那种债。所以它改成钉两件新事实：

        1. 整个 `tests/` 里没有任何真部署调用还在传那个已删除的实参；
        2. 平台代码里不再有 `dsh_web` / `dsh_proxy` 这两个模块（符号也不该留）。

        仍然扫整个 `tests/` 目录：泄漏只要有一处漏网就会发生。
        """
        here = os.path.dirname(os.path.abspath(__file__))
        names = sorted(name for name in os.listdir(here)
                       if name.startswith("test_") and name.endswith(".py"))
        self.assertIn("test_console.py", names)
        launches = 0
        for name in names:
            path = os.path.join(here, name)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                callee = getattr(node.func, "attr", "") or getattr(node.func, "id", "")
                if callee not in ("Popen", "run", "call", "check_output", "check_call"):
                    continue
                if not node.args:
                    continue
                argv = node.args[0]
                if not isinstance(argv, (ast.List, ast.Tuple)):
                    continue
                # 逐字取 argv 里的**所有**字符串常量，含嵌在 `os.path.join(...)` 里的
                # 那一份路径——只取列表直接元素的话，"start.py" 正好在 join 里面，
                # 于是这条守卫会**空转**。
                values = [item.value for item in ast.walk(argv)
                          if isinstance(item, ast.Constant)
                          and isinstance(item.value, str)]
                if not any("start.py" in value for value in values):
                    continue
                launches += 1
                self.assertNotIn("--no-dsh-web", values,
                                 "%s 里还在传已删除的 --no-dsh-web：平台已经不起 DSH "
                                 "宿主了，传它会让启动直接失败" % name)
        self.assertGreaterEqual(
            launches, 1, "整个 tests/ 里应当至少有一条真部署调用，否则这条守卫是空转的")
        root = os.path.dirname(here)
        for gone in ("dsh_web.py", "dsh_proxy.py"):
            self.assertFalse(
                os.path.isfile(os.path.join(root, "pentestd", gone)),
                "平台不再内嵌/拉起 DSH 页面，%s 不该回来" % gone)


if __name__ == "__main__":
    unittest.main()
