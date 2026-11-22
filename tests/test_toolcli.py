# -*- coding: utf-8 -*-
"""命令行调用工具（`pentestd tool`）的用例。

这个模块的每一条都对应一种**具体的错法**，而不是"把函数都跑一遍"：

* 默认档取错 → 省略 `--policy` 等于把上限开满（而参数看起来只是"没给"）；
* 默认作用域取错 → 启动控制面时限制了网段，命令行这条却哪儿都能扫；
* 闸门被复制 → 界面上拒了、命令行过了；
* 被拒绝的调用产生副作用 → 一次被拒的投递仍然是一次真实的网络动作；
* 未知参数被静默接受 → 使用者以为参数生效了；
* JSON 里的 exitCode 与进程退出码不一致 → CI 判定与日志对不上。
"""
import io
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import authz, chat, cli, policy, scope, toolclient   # noqa: E402
from tests import support                                          # noqa: E402

# 本文件里那几条守的是"闸门被复制 → 界面上拒了、命令行过了"与"最高档位仍然要部署
# 侧放行"，测的是**闸照判**时的拒绝；所以这一模块内把 `authz.FORCE_OPEN` 关掉；
# 跑完还原。
_OPEN = authz.FORCE_OPEN


def setUpModule():
    authz.FORCE_OPEN = False


def tearDownModule():
    authz.FORCE_OPEN = _OPEN


def _run(argv, db):
    """按真实命令行的样子跑一遍，捕获 stdout/stderr 与退出码。"""
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = cli.main(list(argv) + ["--db", db])
    finally:
        sys.stdout, sys.stderr = saved
    return code, out.getvalue(), err.getvalue()


def _sample(key, spec):
    """按声明类型造一个合法的命令行取值。"""
    kind = str(spec.get("type") or "string")
    if kind == "integer":
        return "1"
    if kind == "number":
        return "1.5"
    if kind == "boolean":
        return "true"
    if kind == "array":
        return '["a"]'
    if kind == "object":
        return '{"a":1}'
    return "X%s" % key


class DefaultsAreTheNarrowest(unittest.TestCase):
    """默认方向只能是"更窄"：取反了不会有任何报错，只会静默放行。"""

    def test_empty_policy_resolves_to_narrowest_not_default(self):
        resolved = toolclient.resolve_policy("")
        narrowest = min(policy.POLICIES, key=lambda row: row["level"])["id"]
        self.assertEqual(resolved, narrowest)
        # 真正要守的是下面这一条：`policy.resolve("")` 的语义是"回退到默认档"，
        # 而默认档是 full —— 那是**界面**会话入口的默认，因为那边"选档位"是一次
        # 有人按下的动作。命令行若把空串直接交给闸门，省略 --policy 就等于把上限
        # 开满，而参数看起来只是"没给"。
        self.assertEqual(policy.resolve("")["id"], policy.DEFAULT_POLICY)
        self.assertNotEqual(resolved, policy.resolve("")["id"])
        self.assertLess(policy.POLICY_INDEX[resolved]["level"],
                        policy.POLICY_INDEX[policy.DEFAULT_POLICY]["level"])

    def test_empty_scope_falls_back_to_library_baseline_not_unrestricted(self):
        resolved, text = toolclient.resolve_scope("")
        self.assertEqual(sorted(resolved), sorted(scope.DEFAULT_SCOPE))
        self.assertIn("未指定", text)
        # `parse_scope_spec("")` 给的是 ANY_SCOPE —— 那是**部署入口**的默认
        # （`DEFAULT_SCOPE_SPEC = "any"`，一个有人签过字的显式决定）。命令行沿用
        # 它就意味着"启动控制面时限制了网段、这条却哪儿都能扫"，而两者在界面上
        # 看不出区别。
        self.assertEqual(sorted(scope.parse_scope_spec("")), sorted(scope.ANY_SCOPE))
        self.assertNotEqual(sorted(resolved), sorted(scope.ANY_SCOPE))

    def test_explicit_unrestricted_still_works(self):
        resolved, text = toolclient.resolve_scope("any")
        self.assertEqual(sorted(resolved), sorted(scope.ANY_SCOPE))
        self.assertIn("any", text)

    def test_unknown_policy_is_a_loud_error_not_a_silent_fallback(self):
        with self.assertRaises(toolclient.UsageError):
            toolclient.resolve_policy("root")

    def test_blank_whitelist_means_unrestricted_but_none_fails_closed(self):
        """留空与 `none` 字面上都是"没有目标"，意思却相反——两端必须各钉一条。

        这条断言的前一版写反了：它认为"放行了类别却不给目标"会让每次调用都被
        fail-closed 拒掉，于是要求命令行直接拦下。查 `authz.target_mode` 才知道
        空清单是 **any**（不限制目标，只受 scope 约束），`gate` 只对显式的 `none`
        拒绝。按旧判据拦，会把一个合法且常见的配置判成错误；而真正该拦的那一种
        （开了却一律拒绝）反而漏了过去——正是 `target_mode` 文档里点名要分清的
        "安静的严重错误"。
        """
        classes, targets, note = toolclient.resolve_intrusive("exploit", "")
        self.assertEqual(("exploit",), classes)
        self.assertEqual((), targets, "留空 = 不限制目标，不是一个收窄清单")
        self.assertIn("--allow-intrusive", note)
        with self.assertRaises(toolclient.UsageError):
            toolclient.resolve_intrusive("exploit", "none")
        self.assertEqual(((), (), "全部关闭（默认）"),
                         toolclient.resolve_intrusive("", ""))
        enabled, targets, text = toolclient.resolve_intrusive("exploit", "10.0.0.5")
        self.assertEqual(("exploit",), enabled)
        self.assertEqual(("10.0.0.5",), targets)
        self.assertIn("--allow-intrusive", text)


class GateIsNotCopied(unittest.TestCase):
    """命令行必须过**同一道**闸，而不是自己判一遍。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("toolcli.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.db = self.store.path

    def test_a_denied_call_leaves_no_side_effect(self):
        code, _out, err = _run(
            ["tool", "call", "start_probe", "--arg", "target=127.0.0.1"], self.db)
        self.assertEqual(code, toolclient.EXIT_DENIED)
        self.assertIn("--policy", err)
        # 关键的一条：**没有任务**。被拒绝的调用若已经把任务建出来，这次拒绝就
        # 只是"少打印了一行"——而它的表现是"什么都没发生"，看不出来。
        self.assertEqual([], self.store.jobs())

    def test_the_same_call_passes_at_a_higher_level(self):
        code, out, _err = _run(
            ["tool", "call", "start_probe", "--arg", "target=127.0.0.1",
             "--policy", "probe"], self.db)
        self.assertEqual(code, toolclient.EXIT_OK, out)
        self.assertEqual(1, len(self.store.jobs()))

    def test_scope_narrowing_applies_on_the_cli_side_too(self):
        # 8.8.8.8 是公网。默认作用域是库基线（回环 + 私有网段），所以这条必须被拒；
        # 若这里过了，说明命令行用的是部署入口的 "any" 默认。
        code, out, _err = _run(
            ["tool", "call", "start_probe", "--arg", "target=8.8.8.8",
             "--policy", "probe"], self.db)
        self.assertEqual(code, toolclient.EXIT_FAILED)
        self.assertIn("作用域", out)
        self.assertEqual([], self.store.jobs())

    def test_cli_is_not_a_shortcut_around_the_intrusive_gate(self):
        """最高档位 + 目标在白名单里，仍然要部署侧的 `--allow-intrusive`。

        这是本模块最要紧的一条：`--policy full` 若顺带放行了侵入式动作，那
        `--policy` 就成了"解锁"而不是"收窄"，而设这三个档位的全部意义恰恰是
        它们互相不能解锁。
        """
        base = ["tool", "call", "start_exploit", "--arg", "target=127.0.0.1",
                "--arg", "authorization=GRANT-TEST-0001", "--policy", "full"]
        code, out, _err = _run(base, self.db)
        self.assertEqual(code, toolclient.EXIT_FAILED)
        self.assertIn("准入判定", out)
        self.assertIn("--allow-intrusive", out)
        self.assertEqual([], self.store.jobs())

        code, out, _err = _run(
            base + ["--allow-intrusive", "exploit",
                    "--intrusive-targets", "127.0.0.1"], self.db)
        self.assertEqual(code, toolclient.EXIT_OK, out)
        self.assertEqual(1, len(self.store.jobs()))

    def test_dry_run_changes_nothing(self):
        code, out, _err = _run(
            ["tool", "call", "start_probe", "--arg", "target=127.0.0.1",
             "--policy", "probe", "--dry-run"], self.db)
        self.assertEqual(code, toolclient.EXIT_OK, out)
        self.assertIn("--dry-run", out)
        self.assertEqual([], self.store.jobs())

    def test_permission_has_exactly_one_owner(self):
        """断言的是**关系**而不是名单：CLI 放行的工具恰好是 policy 说允许的那批。

        在 CLI 里另写一份清单，症状是"新加的工具命令行看不到"，或者更糟——
        "界面上已收回的工具命令行还在"。
        """
        allowed = set(policy.allowed_tools("readonly", "", True))
        cli_allowed = {tool["name"] for tool in chat.TOOLS
                       if policy.permits(toolclient.resolve_policy(""),
                                         tool["name"], "", True)}
        self.assertEqual(allowed, cli_allowed)
        self.assertIn("start_probe", set(chat.HANDLERS) - allowed)


class ArgumentsAreChecked(unittest.TestCase):
    """`additionalProperties: False` 只是**声明**——`normalize_arguments` 不校验。"""

    def test_unknown_key_is_refused_not_ignored(self):
        with self.assertRaises(toolclient.UsageError) as caught:
            toolclient.parse_arguments("get_findings", "", ["targte=1"])
        self.assertIn("targte", str(caught.exception))
        self.assertIn("engagementId", str(caught.exception))

    def test_values_are_coerced_by_declared_type(self):
        args = toolclient.parse_arguments("get_plugin_ops", "", ["limit=20"])
        self.assertEqual(20, args["limit"])
        self.assertIsInstance(args["limit"], int)
        # ports 是**端口表达式**（"22,80"），必须保持字符串。猜成列表会让 probe
        # 拿到另一种形状，而它不会报错，只会扫错端口。
        probe = toolclient.parse_arguments("start_probe", "",
                                          ["target=10.0.0.5", "ports=22,80"])
        self.assertEqual("22,80", probe["ports"])
        self.assertIsInstance(probe["ports"], str)
        scan = toolclient.parse_arguments("start_scan", "", ["target=/srv",
                                                             "incremental=true"])
        self.assertIs(scan["incremental"], True)
        # 外部工具的参数是**数组**（原样交给 nmap 的 argv），不是逗号串：
        # 这里靠的是工具自己的声明，命令行不猜。
        run = toolclient.parse_arguments(
            "run_tool", "", ["adapter=nmap", "target=127.0.0.1",
                             'toolArgs=["-sV"]', "timeoutS=30.5"])
        self.assertEqual(["-sV"], run["toolArgs"])
        self.assertAlmostEqual(30.5, run["timeoutS"])

    def test_a_parameter_the_tool_does_not_declare_is_refused(self):
        """schema 是唯一事实来源：工具没声明的键不能"顺手收下"。

        `load_skill` 只声明了 `name` 与 `file`（`file` 是给目录型手册读附属文件用的）。
        命令行若照收 `limit`，工具会按自己的
        行为跑完，而使用者以为受限了——这类"看起来成功了"的错必须在执行前拦住。

        **这里原本用的是 `get_findings` 没有 `limit`。** 那已经不成立了：报告
        DSH-PT-2026-1015 的 D17 要求把各读取口的分页语义统一，`get_findings`
        因此补上了 `offset`/`limit`。换成 `load_skill` 是为了守住同一条不变量，
        而不是把断言改成"顺着代码走"——`load_skill` 的 schema 只有 `name` 与
        `file`，`limit` 不在里面，这一点由下面的 `assertNotIn` 独立核对。
        """
        with self.assertRaises(toolclient.UsageError) as caught:
            toolclient.parse_arguments("load_skill", "", ["limit=1"])
        self.assertIn("limit", str(caught.exception))
        self.assertIn("name", str(caught.exception))
        declared = (chat.TOOL_INDEX["load_skill"]["parameters"]
                    .get("properties") or {})
        self.assertNotIn("limit", declared)

    def test_missing_required_argument(self):
        with self.assertRaises(toolclient.UsageError) as caught:
            toolclient.parse_arguments("start_probe", "", ["ports=22"])
        self.assertIn("target", str(caught.exception))

    def test_json_and_arg_are_mutually_exclusive(self):
        with self.assertRaises(toolclient.UsageError):
            toolclient.parse_arguments("get_findings", "{}", ["limit=1"])

    def test_json_must_be_a_valid_object(self):
        with self.assertRaises(toolclient.UsageError):
            toolclient.parse_arguments("get_findings", "[1,2]", [])
        with self.assertRaises(toolclient.UsageError):
            toolclient.parse_arguments("get_findings", "{oops", [])

    def test_unknown_key_in_json_is_refused_too(self):
        with self.assertRaises(toolclient.UsageError):
            toolclient.parse_arguments("get_findings", '{"nope":1}', [])

    def test_every_declared_parameter_of_every_tool_is_reachable(self):
        """反向守卫：校验太严会把**合法**参数挡在门外。

        遍历的是 `chat.TOOLS` 自己的声明，所以"某个工具的 schema 改了、校验器
        没跟上"会当场红——而那种红看起来像"这个工具命令行用不了"。
        """
        for tool in chat.TOOLS:
            properties = (tool.get("parameters") or {}).get("properties") or {}
            if not properties:
                continue
            pairs = ["%s=%s" % (key, _sample(key, spec))
                     for key, spec in properties.items()]
            try:
                args = toolclient.parse_arguments(tool["name"], "", pairs)
            except toolclient.UsageError as exc:
                self.fail("工具 %s 的声明参数被校验器挡了：%s" % (tool["name"], exc))
            self.assertEqual(sorted(properties), sorted(args))


class OutputContract(unittest.TestCase):
    """命令行的输出是**接口**：脚本按它做判断。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("toolcli.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.db = self.store.path

    def test_json_mode_keeps_stdout_parseable(self):
        code, out, err = _run(
            ["tool", "call", "list_engagements", "--output", "json"], self.db)
        self.assertEqual(code, toolclient.EXIT_OK)
        payload = json.loads(out)          # 混进任何一行非 JSON 都会在这里炸
        self.assertTrue(payload["ok"])
        self.assertEqual(0, payload["exitCode"])
        self.assertEqual("readonly", payload["bounds"]["policy"]["id"])
        # 边界说明只在 stderr —— 少打这一处的后果是 jq/grep 拿到意外内容
        self.assertIn("边界", err)
        self.assertNotIn("边界", out)

    def test_json_exit_code_matches_the_process_exit_code(self):
        """两者不一致时没有任何地方会报错，只有"CI 判定和日志对不上"。"""
        cases = (
            (["tool", "call", "start_probe", "--arg", "target=127.0.0.1"],
             toolclient.EXIT_DENIED),
            (["tool", "call", "nosuchtool"], toolclient.EXIT_USAGE),
            (["tool", "call", "start_probe", "--arg", "target=8.8.8.8",
              "--policy", "probe"], toolclient.EXIT_FAILED),
        )
        for argv, expected in cases:
            code, out, _err = _run(list(argv) + ["--output", "json"], self.db)
            self.assertEqual(expected, code, argv)
            self.assertEqual(code, json.loads(out)["exitCode"], argv)

    def test_quiet_silences_the_bounds_line(self):
        _code, out, err = _run(
            ["tool", "call", "list_engagements", "--quiet"], self.db)
        self.assertEqual("", err)
        self.assertIn("list_engagements", out)

    def test_unknown_tool_is_a_usage_error_not_a_permission_denial(self):
        """两类拒绝要分开：脚本对它们的下一步动作不同（改命令 vs 改档位）。"""
        code, out, _err = _run(["tool", "call", "nosuchtool"], self.db)
        self.assertEqual(code, toolclient.EXIT_USAGE)
        self.assertIn("nosuchtool", out)
        # 分类取的是结构化的 data.denied，不是文案
        denied, _args = chat.check_tool(None, "nosuchtool", {}, "", "", "", True, {})
        self.assertIsNone((denied.get("data") or {}).get("denied"))

    def test_usage_error_stops_before_any_action(self):
        code, _out, _err = _run(
            ["tool", "call", "start_probe", "--arg", "targte=127.0.0.1"], self.db)
        self.assertEqual(code, toolclient.EXIT_USAGE)
        self.assertEqual([], self.store.jobs())

    def test_missing_tool_name_is_a_usage_error(self):
        for action in ("show", "call"):
            code, out, _err = _run(["tool", action], self.db)
            self.assertEqual(code, toolclient.EXIT_USAGE, action)
            self.assertIn("工具名", out)


class Rendering(unittest.TestCase):
    def test_list_groups_by_level_and_shows_the_gap(self):
        lines = toolclient.render_list("", "")
        text = "\n".join(lines)
        self.assertIn("只读观察", text)
        self.assertIn("✗", text)                  # 不可用的工具不隐藏
        self.assertIn("← 需要 --policy", text)
        self.assertIn("需管理员", text)            # 管理员要求看得见
        # 每个工具恰好出现一次：重复意味着两个分组都认领了它。
        # 比对**整词**而不是子串——`record_finding` 是 `record_finding_retest_result`
        # 的前缀，用 `in line` 数会把后者算到前者头上，工具一多就一直红着。
        names = [tool["name"] for tool in chat.TOOLS]
        for name in names:
            hits = sum(1 for line in lines
                       if re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(name), line))
            self.assertEqual(1, hits, "%s 出现次数不对" % name)

    def test_show_lists_types_and_an_example(self):
        lines = toolclient.render_show("start_probe", "", "")
        text = "\n".join(lines)
        self.assertIn("target", text)
        self.assertIn("string", text)
        self.assertIn("--policy probe", text)
        self.assertIn("pentestd tool call start_probe", text)

    def test_show_unknown_tool(self):
        with self.assertRaises(toolclient.UsageError):
            toolclient.render_show("nosuchtool", "", "")

    def test_bounds_line_says_which_copy_is_in_use(self):
        info = toolclient.bounds("", "", "回环与私有网段", "全部关闭（默认）")
        line = toolclient.bounds_line(info)
        self.assertIn("只读观察", line)
        self.assertIn("全部关闭", line)
        # "未指定"必须写在明处：它显示的是**本次调用自己**用的那一份，而运行中的
        # 控制面可能不同（作用域与授权在库层面不持久化）。
        self.assertIn("未指定", info["policy"]["note"])
        self.assertIn("不持久化", info["note"])


if __name__ == "__main__":
    unittest.main()
