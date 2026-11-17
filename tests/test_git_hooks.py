# -*- coding: utf-8 -*-
"""`tools/install_git_hooks.py`：自动推送钩子的**形状**与安装/检查/拆除。

钩子本身不进版本库（`.git/hooks/` 不是仓库内容），所以它的内容必须靠这个脚本带着走。
这个用例守两件事：

1. **两条硬性质写在钩子正文里，而且能静态认出来**——不 `set -e`（提交不被推送失败挡）、
   后台推（`&` 出现在 `exit 0` 之前）、`GIT_TERMINAL_PROMPT=0` 写在命令替换**内部**
   （写在外面到不了 git 里，会把一次提交拖到 timeout 才回来）。
2. **安装/检查/拆除三条路都真的动文件**，并且检查发现不一致时返回非零。

为什么这两条要钉：钩子是"提交之后顺手做一件事"里最容易变成负担的一类——写成前台
push，一次提交就被网络拖住；写成 `VAR=x out=$(cmd)`，`VAR` 是空的，人还是得等。
两种都不报错，只是慢，所以只能靠静态断言守着。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import install_git_hooks                                  # noqa: E402


class HookBodyTests(unittest.TestCase):
    """钩子正文的硬性质（纯字符串，不需要 git）。"""

    def setUp(self):
        self.body = install_git_hooks.HOOK

    def test_it_never_blocks_the_commit(self):
        """没有 `set -e`，而且**最后一句**是无条件 `exit 0`。"""
        self.assertNotIn("set -e\n", self.body)
        self.assertIn("set +e", self.body)
        tail = [line for line in self.body.strip().splitlines() if line.strip()][-1]
        self.assertEqual(tail.strip(), "exit 0")

    def test_it_pushes_in_the_background(self):
        """推送必须在子 shell 里 `&` 出去，钩子自己立刻返回。"""
        self.assertIn(") </dev/null >/dev/null 2>&1 &", self.body)
        self.assertLess(self.body.index(") </dev/null"), self.body.rindex("exit 0"))

    def test_the_prompt_flag_sits_inside_the_substitution(self):
        """`out=$(GIT_TERMINAL_PROMPT=0 …)`，不是 `GIT_TERMINAL_PROMPT=0 out=$(…)`。"""
        self.assertIn("GIT_TERMINAL_PROMPT=0 timeout 300", self.body)
        self.assertNotIn("GIT_TERMINAL_PROMPT=0 out=$(", self.body)

    def test_it_has_a_deadline_and_a_log(self):
        """慢网络也要有尽头；结果要留痕，否则"推没推上去"无从查证。"""
        self.assertIn("timeout 300", self.body)
        self.assertIn('.git/push.log', self.body)
        self.assertIn("tail -n 200", self.body)

    def test_it_clears_an_inherited_repository_pointer(self):
        """在钩子里跑 git 会继承 GIT_DIR；不清掉，push 可能落在别处。"""
        self.assertIn("unset GIT_DIR GIT_WORK_TREE", self.body)


@unittest.skipUnless(shutil.which("git"), "这台机器上没有 git")
class InstallTests(unittest.TestCase):
    """真的在临时仓库里装一次、检查一次、拆一次。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hooks-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        subprocess.run(["git", "init", "-q", self.tmp], check=True, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.hook = os.path.join(self.tmp, ".git", "hooks", install_git_hooks.HOOK_NAME)
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self.cwd)

    def test_check_reports_missing_then_clean(self):
        self.assertEqual(install_git_hooks.main(["--check"]), 2)
        self.assertEqual(install_git_hooks.main([]), 0)
        self.assertTrue(os.path.isfile(self.hook))
        self.assertEqual(install_git_hooks.main(["--check"]), 0)

    def test_install_is_idempotent(self):
        install_git_hooks.main([])
        first = os.stat(self.hook).st_mtime_ns
        self.assertEqual(install_git_hooks.main([]), 0)
        self.assertEqual(os.stat(self.hook).st_mtime_ns, first)

    def test_a_hand_edited_hook_is_backed_up_before_being_replaced(self):
        """装的时候若已有别的内容，先把旧那份备份出来——那里可能有别人加的步骤。"""
        os.makedirs(os.path.dirname(self.hook), exist_ok=True)
        with open(self.hook, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("#!/bin/sh\necho custom\n")
        self.assertEqual(install_git_hooks.main(["--check"]), 2)
        self.assertEqual(install_git_hooks.main([]), 0)
        backups = [name for name in os.listdir(os.path.dirname(self.hook))
                   if name.startswith(install_git_hooks.HOOK_NAME + ".bak-")]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(os.path.dirname(self.hook), backups[0]),
                  encoding="utf-8") as fh:
            self.assertIn("echo custom", fh.read())

    def test_remove_takes_it_away(self):
        install_git_hooks.main([])
        self.assertEqual(install_git_hooks.main(["--remove"]), 0)
        self.assertFalse(os.path.exists(self.hook))
        self.assertEqual(install_git_hooks.main(["--remove"]), 0)

    def test_outside_a_repository_it_says_so(self):
        plain = tempfile.mkdtemp(prefix="plain-")
        self.addCleanup(shutil.rmtree, plain, True)
        os.chdir(plain)
        self.assertEqual(install_git_hooks.main([]), 1)


if __name__ == "__main__":
    unittest.main()