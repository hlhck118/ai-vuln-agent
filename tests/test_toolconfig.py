# -*- coding: utf-8 -*-
"""适配器表外置：文件不在、写错、写对了，三种情况下平台分别怎么表现。

这张表决定了**哪些二进制会被执行**，所以它的失败方式值得单独钉住。要钉的是
"失败时是不是看得见"，而不是"解析器对不对"：

  * 文件不在 → 内置表继续工作，并且界面上一行字说"还没有配置文件、点这儿生成"；
  * 文件写错 → **整份**退回内置表，错误原文（第几项、哪个键、为什么）送到面板；
  * 文件写对 → 生效，包括新增的工具出现在检测里、删掉的从清单里消失。

最后一条最容易漏：`detect()` 有缓存，配置变了却不丢缓存，症状是"我从配置里删了
它，界面上它还在，只是装不上也删不掉"。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import toolconfig, toolinstall, toolkit          # noqa: E402
from pentestd.auth import hash_password                        # noqa: E402
from pentestd.server import ApiError, ControlPlane             # noqa: E402
from pentestd.store import Store                               # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class _ConfigCase(unittest.TestCase):
    """所有用例共用的地基：一份临时配置位置，测完把全局表还原。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "tools.json")
        self._env = mock.patch.dict(os.environ, {toolconfig.ENV_CONFIG: self.path})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        # 全局表与缓存都是模块级的：不复位的话，后面跑的用例会继承这里的配置。
        toolkit.reload()
        self.tmp.cleanup()

    def write_raw(self, payload):
        text = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False)
        handle = open(self.path, "w", encoding="utf-8", newline="\n")
        handle.write(text)
        handle.close()
        return self.path

    def default_rows(self):
        return [dict(item) for item in toolkit.DEFAULT_ADAPTERS]

    def custom_row(self, **extra):
        row = {"id": "internal-cli", "label": "内部客户端",
               "binaries": ["internal-cli"], "class": "toolkit",
               "arguments": ["-q"], "why": "团队内部编译的客户端，PATH 上有就算装了"}
        row.update(extra)
        return row


class LoadTestCase(_ConfigCase):
    def test_a_missing_file_falls_back_to_the_builtin_table(self):
        rows, meta = toolconfig.load(toolkit.DEFAULT_ADAPTERS, self.path)
        self.assertEqual(meta["source"], "builtin")
        self.assertFalse(meta["exists"])
        self.assertEqual(meta["count"], len(toolkit.DEFAULT_ADAPTERS))
        # **没有错误**。文件不存在是初始状态，不是故障——把它写成错误，会让每个
        # 新装的人打开面板就看到一条红字。
        self.assertEqual(meta["error"], "")
        self.assertEqual([row["id"] for row in rows],
                         [row["id"] for row in toolkit.DEFAULT_ADAPTERS])

    def test_the_generated_file_round_trips(self):
        target = toolconfig.write(toolkit.DEFAULT_ADAPTERS, self.path)
        self.assertTrue(os.path.isfile(target))
        payload = json.loads(read_text(target))
        # 生成的文件要能被人直接看懂：说明在前，表在后。
        self.assertIn("_readme", payload)
        self.assertTrue(any("重新检测" in line for line in payload["_readme"]))
        rows, meta = toolconfig.load(toolkit.DEFAULT_ADAPTERS, self.path)
        self.assertEqual(meta["source"], "file")
        self.assertEqual(meta["error"], "")
        self.assertEqual([row["id"] for row in rows],
                         [row["id"] for row in toolkit.DEFAULT_ADAPTERS])
        # JSON 里是列表，表里是元组：`build_command` 用 `set(...)` 包了一层，
        # 两种都能跑，但保持与内置表同一形状，免得下游某处按元组做断言。
        curl = [row for row in rows if row["id"] == "curl"][0]
        self.assertIsInstance(curl["dataFlags"], tuple)
        self.assertIn("-w", curl["dataFlags"])

    def test_ensure_never_overwrites_an_existing_file(self):
        self.write_raw({"adapters": [self.custom_row()]})
        info = toolconfig.ensure(toolkit.DEFAULT_ADAPTERS, self.path)
        self.assertFalse(info["created"])
        self.assertEqual(len(json.loads(read_text(self.path))["adapters"]), 1)

    def test_a_broken_file_is_reported_not_swallowed(self):
        self.write_raw("{不是 JSON")
        rows, meta = toolconfig.load(toolkit.DEFAULT_ADAPTERS, self.path)
        self.assertEqual(meta["source"], "builtin")
        self.assertTrue(meta["exists"])
        self.assertIn("JSON", meta["error"])
        self.assertEqual(len(rows), len(toolkit.DEFAULT_ADAPTERS))

    def test_an_empty_file_says_so_instead_of_loading_zero_tools(self):
        self.write_raw("   \n")
        _rows, meta = toolconfig.load(toolkit.DEFAULT_ADAPTERS, self.path)
        self.assertIn("空", meta["error"])
        self.assertEqual(meta["source"], "builtin")

    def test_a_newer_schema_is_refused(self):
        self.write_raw({"_schema": toolconfig.SCHEMA + 1,
                        "adapters": [self.custom_row()]})
        _rows, meta = toolconfig.load(toolkit.DEFAULT_ADAPTERS, self.path)
        self.assertIn("版本", meta["error"])
        self.assertEqual(meta["source"], "builtin")


class ValidationTestCase(_ConfigCase):
    """六种"写错"，每一种都必须**整份**拒绝并说清是哪一项、为什么。"""

    def _error(self, entries):
        self.write_raw({"adapters": entries})
        _rows, meta = toolconfig.load(toolkit.DEFAULT_ADAPTERS, self.path)
        self.assertEqual(meta["source"], "builtin")
        return meta["error"]

    def test_an_unknown_key_is_refused(self):
        # `dataflags`（小写 f）是多打一个字母的典型。它若被当成未知键**收下**，
        # 行为是"这个开关没有声明为数据档"——curl -w 的格式串会被字符集拒掉，
        # 而错误指向的是参数，不是配置。
        text = self._error([self.custom_row(dataflags=["-q"])])
        self.assertIn("未知键", text)
        self.assertIn("dataflags", text)

    def test_a_class_outside_the_authorised_set_is_refused(self):
        text = self._error([self.custom_row(**{"class": "internal"})])
        self.assertIn("class", text)
        # 可选值要列出来，否则读的人还得去翻 authz.py。
        self.assertIn("toolkit", text)

    def test_a_duplicate_id_is_refused(self):
        text = self._error([self.custom_row(), self.custom_row(label="另一个")])
        self.assertIn("重复", text)

    def test_a_flag_declared_outside_arguments_is_refused(self):
        text = self._error([self.custom_row(arguments=["-q"], dataFlags=["-o"])])
        self.assertIn("-o", text)
        self.assertIn("arguments", text)

    def test_a_missing_required_key_is_refused(self):
        row = self.custom_row()
        row.pop("binaries")
        self.assertIn("binaries", self._error([row]))

    def test_an_empty_table_is_refused(self):
        # 一张空表 = "这台机器不允许执行任何外部程序"。它可能是故意的，但更可能是
        # 编辑到一半保存了，所以拒绝并要求删掉文件来表达"我就是要空"。
        self.assertIn("非空", self._error([]))

    def test_a_bad_identifier_is_refused(self):
        # id 会进 data-* 属性、日志与命令行摘要，空格与引号在某一层一定会出问题。
        self.assertIn("id", self._error([self.custom_row(id="Internal CLI")]))

    def test_a_broken_regex_is_refused(self):
        # 正则写错的后果是那条降级警告**永远不出现**，而它的缺席读起来像"一切正常"。
        row = self.custom_row(degradations=[["[unclosed", "说明"]])
        self.assertIn("degradations", self._error([row]))

    def test_one_bad_entry_rejects_the_whole_file(self):
        """不保留"能用的那些"：少一项而没人说，比整份退回并说明更坏。"""
        text = self._error([self.default_rows()[0], self.custom_row(degradations=[["(", "x"]])])
        self.assertIn("第 2 项", text)


class ReloadTestCase(_ConfigCase):
    """改完配置怎么生效：`reload()` 重绑全局表并清掉检测缓存。"""

    def test_a_tool_added_in_the_file_shows_up_in_detection(self):
        rows = self.default_rows()
        rows.append(self.custom_row())
        toolconfig.write(rows, self.path)
        meta = toolkit.reload()
        self.assertEqual(meta["source"], "file")
        self.assertIn("internal-cli", toolkit.ADAPTER_INDEX)
        ids = [item["id"] for item in toolkit.detect(refresh=True)]
        self.assertIn("internal-cli", ids)
        # 配置新增的工具没有下载配方，因此**只能**是手工装——这是刻意的边界，
        # 不是漏做的功能（配方进配置等于把配置变成代码执行面）。
        self.assertNotIn("internal-cli",
                         [row.get("adapter") for row in toolinstall.status()])

    def test_a_tool_removed_in_the_file_disappears_from_the_cache_too(self):
        toolkit.reload()
        before = len(toolkit.detect(refresh=True))
        rows = [row for row in self.default_rows() if row["id"] != "nmap"]
        toolconfig.write(rows, self.path)
        toolkit.reload()
        ids = [item["id"] for item in toolkit.detect()]   # **不 refresh**：缓存必须已被丢弃
        self.assertNotIn("nmap", ids)
        self.assertEqual(len(ids), before - 1)
        self.assertNotIn("nmap", toolkit.ADAPTER_INDEX)

    def test_a_broken_file_keeps_the_platform_working(self):
        self.write_raw({"adapters": [self.custom_row(**{"class": "nope"})]})
        meta = toolkit.reload()
        self.assertEqual(meta["source"], "builtin")
        self.assertTrue(meta["error"])
        # 回到内置那张表：导航、安装、对话里的工具调用都还能跑。
        self.assertEqual(len(toolkit.ADAPTERS), len(toolkit.DEFAULT_ADAPTERS))

    def test_config_status_never_returns_the_live_dict(self):
        # 调用方改坏一次返回值，下一次请求读到的就是被改坏的那份。
        status = toolkit.config_status()
        status["source"] = "tampered"
        self.assertNotEqual(toolkit.config_status()["source"], "tampered")


class ApiTestCase(_ConfigCase):
    """接口面：清单里带上来源、打开配置只给管理员、以及"重新检测"顺带重载。"""

    def setUp(self):
        _ConfigCase.setUp(self)
        self.store = Store(os.path.join(self.tmp.name, "tools.db"))
        self.store.create_user("root", hash_password("Passw0rd-Root1!"), "admin")
        self.store.create_user("ops", hash_password("Passw0rd-Ops1!"), "operator")
        self.plane = ControlPlane(self.store, WEB_DIR, auth_required=True)
        # 全局表是模块级的，导入时就按默认路径读过一次。这里重载，让 `config`
        # 这一块报的是本次用例的临时文件——否则测的是一台机器上的历史残留。
        toolkit.reload()
        # 真实的 sqlite3.Row：`_chat_user` 读 `row["id"]`，dict 替身会让
        # "登录之后 500"这种缺陷在单测里永远看不见。
        self._users = {"admin": self.store.get_user_by_name("root"),
                       "operator": self.store.get_user_by_name("ops")}

    def tearDown(self):
        self.store.close()
        _ConfigCase.tearDown(self)

    def _ctx(self, role="admin", body=None):
        return {"query": {}, "body": body or {}, "headers": {},
                "user": self._users[role]}

    def test_the_list_says_where_the_table_came_from(self):
        with mock.patch.object(toolkit, "detect", return_value=[]), \
                mock.patch.object(toolinstall, "status", return_value=[]):
            status, payload = self.plane.tools(self._ctx())
        self.assertEqual(status, 200)
        cfg = payload["config"]
        self.assertEqual(os.path.abspath(cfg["path"]), os.path.abspath(self.path))
        self.assertEqual(cfg["source"], "builtin")
        self.assertFalse(cfg["exists"])
        self.assertEqual(cfg["error"], "")

    def test_opening_the_config_creates_it_and_reports_where_it_is(self):
        with mock.patch.object(toolconfig, "open_in_system",
                               return_value={"opened": False,
                                             "error": "没有桌面会话"}) as opener:
            status, payload = self.plane.tools_config_open(self._ctx())
        self.assertEqual(status, 200)
        self.assertTrue(payload["created"])
        self.assertTrue(os.path.isfile(self.path))
        self.assertFalse(payload["opened"])
        # **打不开也要 200 并把原因带回来**：文件已经在那儿了，这时把它当错误抛出去，
        # 连"文件已经建好、位置在哪"都会一起被吞掉。
        self.assertIn("没有桌面会话", payload["error"])
        self.assertEqual(opener.call_count, 1)

    def test_opening_again_keeps_what_the_user_wrote(self):
        self.write_raw({"adapters": [self.custom_row()]})
        text = read_text(self.path)
        with mock.patch.object(toolconfig, "open_in_system",
                               return_value={"opened": True, "error": ""}):
            _status, payload = self.plane.tools_config_open(self._ctx())
        self.assertFalse(payload["created"])
        self.assertEqual(read_text(self.path), text)

    def test_opening_the_config_needs_an_admin(self):
        with mock.patch.object(toolconfig, "open_in_system",
                               return_value={"opened": True, "error": ""}) as opener:
            with self.assertRaises(ApiError) as caught:
                self.plane.tools_config_open(self._ctx("operator"))
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("管理员", caught.exception.message)
        # 被拒的请求不许有副作用：连文件都不该被创建。
        self.assertEqual(opener.call_count, 0)
        self.assertFalse(os.path.exists(self.path))

    def test_recheck_reloads_the_adapter_table(self):
        """「重新检测」是"改了配置怎么生效"的唯一答案，所以它必须重载。"""
        rows = [row for row in self.default_rows() if row["id"] != "nmap"]
        toolconfig.write(rows, self.path)
        with mock.patch.object(toolkit, "detect", return_value=[]), \
                mock.patch.object(toolinstall, "status", return_value=[]):
            status, payload = self.plane.tools_check(self._ctx())
        self.assertEqual(status, 200)
        self.assertNotIn("nmap", toolkit.ADAPTER_INDEX)
        self.assertEqual(payload["config"]["source"], "file")
        self.assertEqual(payload["config"]["count"], len(rows))


class ContractTestCase(unittest.TestCase):
    """静态契约：三处接线各自只有一处写法，写错时症状都很隐蔽。"""

    def setUp(self):
        self.html = read_text(os.path.join(WEB_DIR, "index.html"))
        self.app = read_text(os.path.join(WEB_DIR, "app.js"))
        self.server = read_text(os.path.join(ROOT, "pentestd", "server.py"))

    def test_the_button_and_its_handler_exist(self):
        self.assertIn('id="tools-config"', self.html)
        self.assertIn("openToolConfig", self.app)
        # 按钮在「重新检测」**左边**：改配置是先决动作，检测是验证动作。
        self.assertLess(self.html.index('id="tools-config"'),
                        self.html.index('id="tools-check"'))

    def test_the_route_is_registered(self):
        self.assertIn('r.add("POST", "/api/v1/tools/config/open"', self.server)

    def test_the_config_line_is_rendered_before_the_list(self):
        """来源一行必须画在清单**之前**：配置没被采用时，它是唯一的红字。"""
        body = self.app[self.app.index("function renderTools(data)"):]
        body = body[:body.index("\nfunction ")]
        self.assertIn("toolConfigLine(info.config)", body)
        self.assertLess(body.index("toolConfigLine(info.config)"),
                        body.index("byClass"))
