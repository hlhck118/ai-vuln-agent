# -*- coding: utf-8 -*-
"""外部工具调度：参数白名单、如实报缺、以及"外部工具也得过授权闸"。

最后一条是这一组里最重要的：把工作交给一个外部程序，不改变它在授权上的性质。
`hydra` 和本平台自己的凭据测试在准入上没有区别。
"""
import unittest

from pentestd import authz, toolkit

# 本文件最后那一组守的是"外部工具也得过授权闸"，测的是**闸照判**时的拒绝；
# 所以这一模块内把 `authz.FORCE_OPEN` 关掉；跑完还原。
_OPEN = authz.FORCE_OPEN


def setUpModule():
    authz.FORCE_OPEN = False


def tearDownModule():
    authz.FORCE_OPEN = _OPEN


class ArgumentTestCase(unittest.TestCase):
    """Windows 上很多工具是 .cmd 包装，参数最终会进 cmd.exe —— 白名单是唯一的闸。"""

    def test_shell_metacharacters_are_refused(self):
        for bad in ("10.0.0.5 && calc.exe", "a|b", "x%PATH%", "a^b",
                    "a>b", 'a"b', "a<b", "10.0.0.5;id", "$(id)", "a b"):
            with self.assertRaises(toolkit.ToolkitError,
                                   msg="%r 必须被拒绝" % bad):
                toolkit.validate_argument(bad)

    def test_real_targets_are_accepted(self):
        for good in ("10.0.0.5", "10.0.0.0/24", "host.example", "127.0.0.1:8080",
                     "/admin/login", "user@example", "::1", "-sT"):
            self.assertEqual(good, toolkit.validate_argument(good))

    def test_refusal_explains_the_cmd_exe_reason(self):
        with self.assertRaises(toolkit.ToolkitError) as ctx:
            toolkit.validate_argument("a|b")
        self.assertIn("cmd.exe", str(ctx.exception))


class AdapterTableTestCase(unittest.TestCase):
    def test_every_adapter_explains_why_not_to_reimplement(self):
        # 缺了这句话，使用者会以为"没装工具"只是可选功能的缺失，而不是
        # "重造这个协议会得到更坏的结果"。
        for item in toolkit.ADAPTERS:
            self.assertTrue(item.get("why"), "%s 缺 why" % item["id"])
            self.assertTrue(item.get("purpose"), "%s 缺 purpose" % item["id"])
            self.assertTrue(item.get("binaries"), "%s 缺 binaries" % item["id"])

    def test_every_adapter_declares_an_intrusive_class(self):
        # 没归类的适配器会在 authz.gate 里被拒（未知类别抛错），但那时
        # 报的是"未知类别"而不是"配置缺失"。建表阶段就该发现。
        for item in toolkit.ADAPTERS:
            self.assertIn(item["class"], {c["id"] for c in authz.CLASSES},
                          "%s 的类别 %r 不在侵入类别表里" % (item["id"], item["class"]))

    def test_adapter_ids_are_unique(self):
        ids = [item["id"] for item in toolkit.ADAPTERS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_protocol_clients_are_covered(self):
        # 那五件"不能做"里，凡是需要真实协议客户端的，这里必须有落点。
        ids = {item["id"] for item in toolkit.ADAPTERS}
        for needed in ("ssh", "smbclient", "ncrack", "hydra", "msfconsole"):
            self.assertIn(needed, ids)


class DetectTestCase(unittest.TestCase):
    def test_detect_returns_every_adapter(self):
        rows = toolkit.detect()
        self.assertEqual(len(toolkit.ADAPTERS), len(rows))
        for row in rows:
            self.assertIn("installed", row)
            self.assertIn("path", row)

    def test_caching_and_refresh(self):
        first = toolkit.detect()
        second = toolkit.detect()
        self.assertEqual([r["id"] for r in first], [r["id"] for r in second])
        self.assertEqual(len(first), len(toolkit.detect(refresh=True)))

    def test_installed_list_is_sorted_ids(self):
        found = toolkit.installed()
        self.assertEqual(sorted(found), found)

    def test_capabilities_group_by_class(self):
        caps = toolkit.capabilities()
        for key in caps:
            self.assertIn(key, {c["id"] for c in authz.CLASSES})
            self.assertEqual(sorted(caps[key]), caps[key])


class RequireTestCase(unittest.TestCase):
    def test_unknown_adapter_raises_with_options(self):
        with self.assertRaises(toolkit.ToolkitError) as ctx:
            toolkit.require("nmap-but-typo")
        self.assertIn("nmap", str(ctx.exception))

    def test_missing_adapter_reports_three_things(self):
        # 装了什么、怎么装、为什么不自己实现 —— 三句缺一不可。
        missing = [item for item in toolkit.detect() if not item["installed"]]
        if not missing:
            self.skipTest("本机装齐了所有适配器")
        target = missing[0]["id"]
        with self.assertRaises(toolkit.ToolkitError) as ctx:
            toolkit.require(target)
        message = str(ctx.exception)
        self.assertIn("本机没有找到", message)
        self.assertIn("为什么不自己实现", message)
        self.assertIn("安装", message)

    def test_missing_report_lists_both_sides(self):
        text = toolkit.missing_report()
        self.assertIn("已安装", text)
        self.assertIn("完整", text,
                      "必须说清缺工具不等于缺功能，否则使用者会以为整个平台不可用")


class CommandTestCase(unittest.TestCase):
    def test_build_command_returns_a_list_never_a_string(self):
        # 返回字符串就迟早有人 shell=True，那时参数白名单会被绕过一层。
        for row in toolkit.detect():
            if not row["installed"]:
                continue
            argv = toolkit.build_command(row["id"], ["-h"])
            self.assertIsInstance(argv, list, row["id"])
            self.assertEqual(row["path"], argv[0])
            self.assertEqual(["-h"], argv[1:])
            return
        self.skipTest("本机一个适配器都没装")

    def test_args_are_validated_before_execution(self):
        installed = toolkit.installed()
        if not installed:
            self.skipTest("本机一个适配器都没装")
        with self.assertRaises(toolkit.ToolkitError):
            toolkit.build_command(installed[0], ["a; b"])


class GateTestCase(unittest.TestCase):
    """外部工具不是绕过授权闸的理由。"""

    def test_run_is_refused_without_a_grant(self):
        # 顺序上授权判定在找工具之前：没被授权的调用不该因为收到错误信息
        # 而顺带知道这台机器上装了哪些工具。
        with self.assertRaises(authz.AuthzError):
            toolkit.run("nmap", ["-sT", "10.0.0.5"], host="10.0.0.5")

    def test_run_is_refused_when_the_class_is_not_enabled(self):
        with self.assertRaises(authz.AuthzError) as ctx:
            toolkit.run("nmap", ["-sT", "10.0.0.5"],
                        grants={"grant": "ENG-0001", "enabled": ["exploit"],
                                "targets": ["10.0.0.5"]}, host="10.0.0.5")
        self.assertIn("toolkit", str(ctx.exception))

    def test_run_is_refused_for_a_target_outside_the_allowlist(self):
        with self.assertRaises(authz.AuthzError):
            toolkit.run("nmap", ["-sT", "10.0.0.5"],
                        grants={"grant": "ENG-0001", "enabled": ["all"],
                                "targets": ["192.168.0.0/16"]}, host="10.0.0.5")

    def test_a_missing_tool_reports_honestly_after_the_gate(self):
        # 过了授权闸之后才报"没装"，而且要说清怎么装。
        missing = [item for item in toolkit.detect() if not item["installed"]]
        if not missing:
            self.skipTest("本机装齐了所有适配器")
        target = missing[0]["id"]
        with self.assertRaises(toolkit.ToolkitError) as ctx:
            toolkit.run(target, ["-h"], host="10.0.0.5",
                        grants={"grant": "ENG-0001", "enabled": ["all"],
                                "targets": ["10.0.0.5"]})
        self.assertIn("安装", str(ctx.exception))

    def test_a_run_records_the_grant_and_the_effective_limits(self):
        installed = toolkit.installed()
        if not installed:
            self.skipTest("本机一个适配器都没装")
        row = [item for item in toolkit.detect() if item["id"] == installed[0]][0]
        result = toolkit.run(row["id"], ["--version"], host="10.0.0.5",
                             grants={"grant": "ENG-2026-0007",
                                     "enabled": ["all"],
                                     "targets": ["10.0.0.5"]})
        self.assertEqual("ENG-2026-0007", result["grant"]["grant"])
        self.assertIn("timeoutS", result["limits"])
        self.assertIn("maxOutputBytes", result["limits"])

    def test_output_is_redacted_before_it_reaches_a_chain_node(self):
        installed = toolkit.installed()
        if not installed:
            self.skipTest("本机一个适配器都没装")
        row = [item for item in toolkit.detect() if item["id"] == installed[0]][0]
        result = toolkit.run(row["id"], ["--version"], host="10.0.0.5",
                             secrets=["python", "Python"],
                             grants={"grant": "ENG-0001", "enabled": ["all"],
                                     "targets": ["10.0.0.5"]})
        self.assertNotIn("ython", result["output"],
                         "命令输出里可能带凭据，返回前必须打码")

    def test_timeout_and_output_cap_come_from_the_limits(self):
        installed = toolkit.installed()
        if not installed:
            self.skipTest("本机一个适配器都没装")
        row = [item for item in toolkit.detect() if item["id"] == installed[0]][0]
        result = toolkit.run(row["id"], ["--version"], host="10.0.0.5",
                             timeout=99999.0, max_output=10 ** 9,
                             grants={"grant": "ENG-0001", "enabled": ["all"],
                                     "targets": ["10.0.0.5"]})
        # 上限按**这个适配器自己的类别**取，取不到再要平台兜底。
        #
        # 这里原先写死 `authz.LIMITS["toolkit"]`，而它只在"installed[0] 恰好是
        # toolkit 类"时成立——那是按字母序撞上的。装一个 chisel（`pivot` 类）
        # 之后这条用例就红了，而产品行为是对的：`pivot` 那一类根本没声明
        # `maxRuntimeS`，上限来自 `toolkit.MAX_RUNTIME_S` 那个兜底。
        declared = authz.LIMITS.get(row["class"], {})
        runtime = declared.get("maxRuntimeS") or toolkit.MAX_RUNTIME_S
        output = declared.get("maxOutputBytes") or toolkit.MAX_OUTPUT_BYTES
        self.assertLessEqual(result["limits"]["timeoutS"], runtime)
        self.assertLessEqual(result["limits"]["maxOutputBytes"], output)

    def test_a_class_without_declared_limits_still_gets_the_platform_cap(self):
        """**没有声明上限的类别也必须被兜住。**

        `authz.LIMITS` 是按侵入类别写的，`pivot`/`creds`/`exploit` 三类里都没有
        `maxRuntimeS`，而十二个适配器里有八个属于这三类。原先的写法
        `limits.get("maxRuntimeS", timeout)` 默认取调用方给的值，等于"没声明
        就是没有上限"——`run_tool(..., timeoutS=99999)` 真的会跑 27 小时。
        """
        rows = [item for item in toolkit.detect() if item["installed"]]
        classes = sorted({row["class"] for row in rows})
        if not any("maxRuntimeS" not in authz.LIMITS.get(name, {})
                   for name in classes):
            self.skipTest("本机装的适配器所属类别都声明了运行上限")
        row = [item for item in rows
               if "maxRuntimeS" not in authz.LIMITS.get(item["class"], {})][0]
        result = toolkit.run(row["id"], ["--version"], host="10.0.0.5",
                             timeout=99999.0,
                             grants={"grant": "ENG-0001", "enabled": ["all"],
                                     "targets": ["10.0.0.5"]})
        self.assertLessEqual(result["limits"]["timeoutS"], toolkit.MAX_RUNTIME_S)
        self.assertEqual("platform", result["limits"]["cappedBy"])


if __name__ == "__main__":
    unittest.main()
