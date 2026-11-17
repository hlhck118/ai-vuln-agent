# -*- coding: utf-8 -*-
"""插件按 XDG 落盘时，必须落回项目自己的 home。

为什么这条值得单独一个用例：billion-context 只在 `DSH_HOME` 里是查不到它的状态的——
它按 XDG 规范写 `~/.local/share/billion-context`（实测跑了半天 318 MB），
配 `~/.config/billion-context`。于是"插件装在项目里"与"状态写在电脑上"可以同时成立，
而观察到的现象只有一处：项目目录删掉、磁盘上还留着几百兆，且两个实例共用同一个 index。

所以这里钉的是**可删除性**，不是某个实现细节。
"""
import os
import unittest

from pentestd import dsh_runtime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

XDG_FACETS = (("XDG_CONFIG_HOME", "config"),
              ("XDG_CACHE_HOME", "cache"),
              ("XDG_STATE_HOME", "state"),
              ("XDG_DATA_HOME", "share"))


class XdgRedirectTestCase(unittest.TestCase):
    def setUp(self):
        self.home = os.path.join(ROOT, "data", "dsh-home")

    def _env(self):
        return dsh_runtime.DshRuntime(home=self.home, root=ROOT,
                                      dsh_path="X:/fake/dsh.cmd",
                                      patch="X:/fake/patch.yml").env()

    def test_the_four_xdg_roots_stay_under_the_project_home(self):
        env = self._env()
        for name, leaf in XDG_FACETS:
            self.assertEqual(env.get(name),
                             os.path.join(self.home, "xdg", leaf),
                             "%s 没有指回项目 home" % name)

    def test_a_decoy_in_the_parent_environment_is_overridden(self):
        """反例：不重定向时这四项会从父进程继承（那正是状态跑到用户目录的方式）。"""
        decoy = os.path.join(ROOT, "decoy-xdg")
        old = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = decoy
        try:
            env = self._env()
        finally:
            if old is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = old
        self.assertNotEqual(env.get("XDG_DATA_HOME"), decoy,
                            "父进程的 XDG_DATA_HOME 泄漏进了运行时子进程")

    def test_the_roots_are_four_distinct_places(self):
        """四个互相不为前缀：合并成一处会让 cache 与 sessions 抢同一个清理边界。"""
        env = self._env()
        values = [env[name] for name, _ in XDG_FACETS]
        self.assertEqual(len(set(values)), len(values))
