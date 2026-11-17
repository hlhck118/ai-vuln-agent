# -*- coding: utf-8 -*-
"""两个引擎：部署页必须分开说。

这条用例守的是一个**语言**问题，不是排版问题：

| 名字 | 是什么 | 不分开写会怎样 |
|---|---|---|
| 推理引擎 | 项目自带的 DSH 运行时（`vendor/dsh`）：跑模型、跑工具循环 | 使用者以为平台的"引擎"就是扫描器，模型那半边在界面上不存在 |
| 发掘引擎 | `vulnagent`（`prototype/`）：真正执行扫描与验证用例 | 反过来，看到 DSH 版本号的人会以为扫描也换成了 DSH |

所以断言分两层：接口真的给了两块的读数；界面真的把它们画成两行。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.dirname(HERE)
sys.path.insert(0, PLATFORM)

from tests import support                                        # noqa: E402
from pentestd import dsh_runtime                                 # noqa: E402

WEB = os.path.join(PLATFORM, "web")


class TwoEnginesContract(unittest.TestCase):
    """接口层：`/api/v1/setup` 里两块都在，而且指向各自的东西。"""

    @classmethod
    def setUpClass(cls):
        cls.directory, cls.store = support.temporary_store("two-engines.db")
        cls.httpd, cls.client = support.start(cls.store, WEB)

    @classmethod
    def tearDownClass(cls):
        support.stop(cls.httpd, cls.store, cls.directory)

    def test_setup_reports_the_reasoning_engine_separately(self):
        status, payload = self.client.get("/api/v1/setup")
        self.assertEqual(200, status)
        dsh = payload.get("dsh") or {}
        engine = payload.get("engine") or {}
        for key in ("mode", "enabled", "profile", "available", "version",
                    "cli", "vendored"):
            self.assertIn(key, dsh, "部署页缺推理引擎字段：%s" % key)
        self.assertIn("available", engine, "部署页缺发掘引擎字段")
        # 两块必须指向**不同**的东西：一边是 DSH 可执行文件，一边是 vulnagent 目录。
        if engine.get("available") and dsh.get("cli"):
            self.assertNotEqual(os.path.normcase(engine.get("home") or ""),
                                os.path.normcase(dsh.get("cli") or ""),
                                "两个引擎指向了同一个路径，说明读数串了")

    @unittest.skipUnless(os.path.isfile(dsh_runtime.VENDOR_DSH_BIN),
                         "项目内 DSH 未安装（vendor/dsh 还没跑 install.cmd）")
    def test_the_reasoning_engine_is_the_vendored_one(self):
        """项目内那一份：路径在 vendor 下、版本可读、`vendored=true`。"""
        status, payload = self.client.get("/api/v1/setup")
        self.assertEqual(200, status)
        dsh = payload.get("dsh") or {}
        vendor = os.path.normcase(os.path.abspath(dsh_runtime.VENDOR_DSH_BIN))
        self.assertTrue(dsh.get("vendored"), "推理引擎不是项目内那一份：%r" % dsh)
        self.assertEqual(vendor, os.path.normcase(os.path.abspath(dsh.get("cli") or "")))
        self.assertTrue(dsh.get("version"), "读不出项目内 DSH 的版本")
        self.assertFalse(dsh.get("reason"), "项目内 DSH 应可用：%r" % dsh.get("reason"))

    def test_the_skill_catalog_is_not_a_drill(self):
        """技能目录不再走「演练台」那条路：没有授权门，也没有 /api/v1/drill。"""
        status, _payload = self.client.get("/api/v1/drill")
        self.assertEqual(404, status)
        status, payload = self.client.get("/api/v1/skills")
        self.assertEqual(200, status)
        self.assertIn("skills", payload)


class TwoEnginesWording(unittest.TestCase):
    """界面层：两条标签都在，而且不再有单独一行的「引擎」。"""

    def _source(self, name):
        with open(os.path.join(WEB, name), encoding="utf-8") as fh:
            return fh.read()

    def test_deployment_card_names_both(self):
        js = self._source("app.js")
        self.assertIn("'推理引擎'", js, "部署卡没把 DSH 那一侧写出来")
        self.assertIn("'发掘引擎'", js, "部署卡没把 vulnagent 那一侧写出来")
        self.assertNotIn("appendInfoRow(list, '引擎'", js,
                         "还把两个引擎合成一行：看的人会以为引擎就是扫描器")

    def test_skills_admin_lives_in_the_settings_group(self):
        """技能管理在设置里，界面上没有授权门、没有演练台卡片。"""
        html = self._source("index.html")
        self.assertIn('data-settings-group="skills"', html)
        self.assertNotIn("drill-gate", html, "授权门还留在页面上")
        self.assertIn('src="skills.js"', html)
        self.assertNotIn('src="drill.js"', html)

if __name__ == "__main__":
    unittest.main()
