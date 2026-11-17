# -*- coding: utf-8 -*-
"""节点名的路径安全：`pki` 的三个子命令都自己拼文件名。

这一份守的是一件很小但在这一层很重的事：`issue` / `revoke` / `describe` 都用
`os.path.join(directory, "%s.crt" % name)` 拼路径，而 `name` 来自命令行。
`os.path.join` 不会挡住 `..` —— 实测 `--name ../pentest` 拼出来的是**目录外面**
那个文件，`revoke` 会把它连同 `.key` / `.csr` / `.ext` 一并删掉。

这里不测"报错信息好不好看"，只测三条不变量：

  1. 带路径分隔符、`..`、`ca.key`/`ca.crt` 一律拒；
  2. 正常名字照旧能用（收紧不能把正常用法一起收掉）；
  3. 拒的位置在**拼接之前** —— 用"目录外那个文件还在不在"来验，而不是只看异常。
"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import pki                                             # noqa: E402


class NodeNameTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="dsh-pki-name-")

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_a_plain_name_is_accepted(self):
        for name in ("node-a", "control", "node_1", "a.b"):
            self.assertEqual(pki.node_name(name), name)

    def test_a_name_with_a_path_separator_is_refused(self):
        for name in ("../pentest", "a/b", "a\\b", "..", ".", "C:x"):
            with self.assertRaises(pki.PkiError):
                pki.node_name(name)

    def test_the_ca_files_cannot_be_named(self):
        for name in ("ca.key", "ca.crt"):
            with self.assertRaises(pki.PkiError) as caught:
                pki.node_name(name)
            self.assertIn("CA", str(caught.exception))

    def test_an_empty_name_is_refused(self):
        with self.assertRaises(pki.PkiError):
            pki.node_name("   ")

    def test_revoke_cannot_reach_outside_the_directory(self):
        """判据必须在**拼接之前**：外面那个文件还在，才是真的没被碰过。"""
        outside = os.path.join(self.directory, "pentest.key")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("别删我")
        inner = os.path.join(self.directory, "pki")
        os.makedirs(inner, exist_ok=True)
        with self.assertRaises(pki.PkiError):
            pki.revoke(inner, "../pentest")
        self.assertTrue(os.path.exists(outside))

    def test_describe_cannot_reach_outside_the_directory(self):
        with self.assertRaises(pki.PkiError):
            pki.describe(self.directory, "../nothing")


if __name__ == "__main__":
    unittest.main()
