# -*- coding: utf-8 -*-
"""外部工具的安装 / 删除 / 检测：谁能做、拒绝得清不清楚、做完有没有留痕。

这张卡是「部署」页签里**唯一一处会改这台机器**的地方（往项目目录下载可执行文件、
对 WSL 发行版执行 apt）。因此它要守住的边界和别处不同：

  * **只有管理员**能动它。看清单只要登录；装/删/重探都要管理员——它们改的是
    "这台机器上有什么"，不是某个作业；
  * **校验是硬的，而且当面说**：未知工具、已经装了、没装就删、要删 WSL 里那
    一份却没确认，四种都在请求里当场变成 4xx。放进队列再失败，人只能在历史
    记录里翻到一条"失败"，而那时他已经离开这一页了；
  * **入队即返回**，状态落 `tool_ops`。装一个 openssl 要下 40 MB，同步跑会被
    客户端的 300 秒超时掐成半个目录，而截断不会回滚；
  * **界面不许谎报能力**：系统自带的（OpenSSH）既不能装也不能删，那一行就不能
    出现删除按钮。这条在 `renderTools` 里，所以也在这里钉住。

本文件里所有涉及"这台机器装了什么"的断言都**替换了检测结果**：真跑 `detect()`
的测试会在装了一部分和一项没装的机器上给出不同答案，那种测试只能在写它的那台
机器上通过。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import authz, toolinstall, toolkit, toolrunner   # noqa: E402
from pentestd.auth import hash_password                        # noqa: E402
from pentestd.server import ApiError, ControlPlane             # noqa: E402
from pentestd.store import Store                               # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


def _row(adapter_id, installed=False, **extra):
    """一份 `toolkit.detect()` 的行，字段按真实形状给全。"""
    entry = toolkit.ADAPTER_INDEX[adapter_id]
    # 用 `.get()`：12 个适配器的字段并不齐（`ssh` 没有 dataFlags、`msfconsole`
    # 没有 arguments）。夹具替身要是比真数据更严格，测试测的就是替身。
    row = {"id": adapter_id, "label": entry["label"], "purpose": entry["purpose"],
           "class": entry["class"], "binaries": list(entry.get("binaries") or ()),
           "arguments": list(entry.get("arguments") or ()),
           "dataFlags": list(entry.get("dataFlags") or ()), "why": entry.get("why") or "",
           "install": entry.get("install") or "", "installed": installed,
           "source": "project" if installed else "", "path": "C:/t/x.exe" if installed else "",
           "execPath": "C:/t/x.exe" if installed else "", "installable": True,
           "command": "", "wrapper": ""}
    row.update(extra)
    return row


def _status(adapter_id, **extra):
    row = {"adapter": adapter_id, "channel": toolinstall.recipe(adapter_id).get("channel") or "",
           "installable": True, "installed": True, "path": "data/tools/%s" % adapter_id,
           "execPath": "data/tools/%s" % adapter_id, "relative": "", "version": "1.0",
           "installedAt": 1792600000.0, "size": 1048576, "note": "", "reason": ""}
    row.update(extra)
    return row


def _function_body(source, signature):
    """从 `signature` 起按括号配平取出整段函数。

    用"下一个 `}`"找结尾是错的：函数里的第一个右花括号属于内层 `if`，截出来的
    是一段被腰斩的代码，而 `assertNotIn` 会在那段残片里"通过"——一个永远绿的
    契约测试比没有更糟。
    """
    start = source.index(signature)
    index, depth = source.index("{", start), 0
    while index < len(source):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
        index += 1
    raise AssertionError("函数体没有配平：%s" % signature)


class ToolsAdminTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "tools.db"))
        self.store.create_user("root", hash_password("Passw0rd-Root1!"), "admin")
        self.store.create_user("ops", hash_password("Passw0rd-Ops1!"), "operator")
        self.plane = ControlPlane(self.store, WEB_DIR, auth_required=True)
        # 真实的 sqlite3.Row，不是 dict：`_chat_user` 读的是 `row["id"]`，
        # 用 dict 替身会让"登录之后 500"这种缺陷在单测里永远看不见。
        self._users = {"admin": self.store.get_user_by_name("root"),
                       "operator": self.store.get_user_by_name("ops")}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _ctx(self, role="admin", body=None, query=None):
        return {"query": query or {}, "body": body or {}, "headers": {},
                "user": self._users["admin" if role == "admin" else "operator"]}

    def _payload(self, rows, statuses):
        with mock.patch.object(toolkit, "detect", return_value=rows), \
                mock.patch.object(toolinstall, "status", return_value=statuses):
            status, payload = self.plane.tools(self._ctx())
        self.assertEqual(status, 200)
        return payload

    def _wait(self, op_id, seconds=20.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            for op in self.store.tool_ops(20):
                if op["id"] == op_id and op["status"] != "running":
                    return op
            time.sleep(0.02)
        self.fail("操作 %s 没有在 %.0f 秒内结束" % (op_id, seconds))

    # ------------------------------------------------------------ 权限

    def test_watching_is_allowed_but_changing_needs_an_admin(self):
        rows = [_row("curl")]
        with mock.patch.object(toolkit, "detect", return_value=rows), \
                mock.patch.object(toolinstall, "status", return_value=[]):
            self.plane.tools(self._ctx("operator"))
            for handler in (self.plane.tools_check, self.plane.tools_install,
                            self.plane.tools_remove):
                with self.assertRaises(ApiError) as caught:
                    handler(self._ctx("operator", {"adapter": "curl"}))
                self.assertEqual(caught.exception.status, 403)
                self.assertIn("管理员", caught.exception.message)

    # -------------------------------------------------- 清单：两份真相合起来

    def test_the_list_merges_detection_and_the_registry(self):
        """只读 `detect()` 就没有版本号，只读 `status()` 就会把系统里的 curl 报成缺件。"""
        rows = [_row("curl", installed=True), _row("nmap")]
        statuses = [_status("curl", version="8.23.0", size=7340032),
                    _status("ssh", channel="manual", installed=False, path="")]
        payload = self._payload(rows, statuses)
        by_id = {item["id"]: item for item in payload["adapters"]}
        self.assertEqual(len(by_id), 2)
        # 版本与占用只可能来自注册表；`installed` 只可能来自检测。
        self.assertEqual(by_id["curl"]["version"], "8.23.0")
        self.assertEqual(by_id["curl"]["size"], 7340032)
        self.assertTrue(by_id["curl"]["installed"])
        self.assertEqual(by_id["nmap"]["version"], "")       # 没有注册表行
        self.assertFalse(by_id["nmap"]["installed"])
        # 分类的中文说法来自 authz 那一处，不是前端自己写一份映射。
        self.assertEqual(by_id["curl"]["classLabel"],
                         authz.CLASS_INDEX["toolkit"]["label"])
        # 计数是"这一屏的事实"，不是猜的。
        self.assertEqual(payload["installed"], 1)
        self.assertEqual(payload["total"], 2)

    def test_a_manual_channel_is_marked_not_installable(self):
        """ssh 是 Windows 可选组件：我们既不装也不删，界面必须据此收掉按钮。"""
        rows = [_row("ssh", installed=True)]
        payload = self._payload(rows, [_status("ssh", channel="manual")])
        self.assertFalse(payload["adapters"][0]["installable"])
        self.assertEqual(payload["manual"], 1)
        self.assertTrue(payload["adapters"][0]["installed"])

    # ------------------------------------------------ 校验：当场拒绝并说清楚

    def test_an_unknown_adapter_is_refused_with_the_whole_menu(self):
        with self.assertRaises(ApiError) as caught:
            self.plane.tools_install(self._ctx(body={"adapter": "nope"}))
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("未知外部工具", caught.exception.message)
        self.assertIn("curl", caught.exception.message)      # 备选清单跟着一起给

    def test_a_missing_adapter_says_so_instead_of_queueing_nothing(self):
        with self.assertRaises(ApiError) as caught:
            self.plane.tools_install(self._ctx(body={}))
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("缺少 adapter", caught.exception.message)

    def test_an_installed_tool_is_not_installed_again(self):
        with mock.patch.object(toolrunner, "installed_at",
                               return_value="C:/t/curl.exe"):
            with self.assertRaises(ApiError) as caught:
                self.plane.tools_install(self._ctx(body={"adapter": "curl"}))
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("已经装好", caught.exception.message)

    def test_removing_what_is_not_installed_is_a_conflict(self):
        with mock.patch.object(toolrunner, "installed_at", return_value=""):
            with self.assertRaises(ApiError) as caught:
                self.plane.tools_remove(self._ctx(body={"adapter": "curl"}))
        self.assertEqual(caught.exception.status, 409)

    def test_a_manual_channel_explains_the_system_step(self):
        """「平台不支持」会让人放弃；真实答案是"这一步要在系统里做，命令如下"。"""
        with self.assertRaises(ApiError) as caught:
            self.plane.tools_install(self._ctx(body={"adapter": "ssh"}))
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("没有自动安装渠道", caught.exception.message)
        self.assertIn("OpenSSH", caught.exception.message)   # 配方里的手工装法
        self.assertNotIn("。。", caught.exception.message)   # 句号不许叠加

    def test_removing_a_wsl_copy_needs_a_second_confirmation(self):
        """apt remove 的影响会溢出发行版之外，所以只有这一类要确认。"""
        with mock.patch.object(toolrunner, "channel_of", return_value="wsl"),                 mock.patch.object(toolrunner, "installed_at", return_value="wsl://Ubuntu"):
            with self.assertRaises(ApiError) as caught:
                self.plane.tools_remove(self._ctx(body={"adapter": "hydra"}))
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("确认", caught.exception.message)
        self.assertIn("发行版", caught.exception.message)

    # ------------------------------------------ 队列：入队即返回、做完留痕

    def test_an_install_is_queued_and_its_outcome_is_recorded(self):
        outcome = {"bytes": 1234, "source": "https://example.test/x.zip", "channel": "zip",
                   "version": "9.9", "root": "data/tools/curl", "bin": "curl.exe",
                   "kind": "zip", "treeBytes": 4321, "treeFiles": 3, "pruned": {},
                   "elapsedMs": 12, "probe": {"ok": True, "output": "curl 9.9"}}
        with mock.patch.object(toolrunner, "installed_at", return_value=""),                 mock.patch.object(toolrunner, "channel_of", return_value="zip"),                 mock.patch.object(toolrunner, "run_locked", return_value=outcome) as run:
            status, payload = self.plane.tools_install(self._ctx(body={"adapter": "curl"}))
            # **202 而不是 200**：装 40 MB 不能放进请求里等。
            self.assertEqual(status, 202)
            self.assertEqual(payload["queuedAhead"], 0)
            self.assertIn("已排队", payload["note"])
            op = self._wait(payload["op"]["id"])
        self.assertEqual(op["status"], "done")
        self.assertEqual((op["action"], op["adapter"]), ("install", "curl"))
        self.assertEqual(op["bytes"], 1234)
        # 文案要能回答"装到哪、什么版本"：失败原因与成功内容都靠这一列。
        self.assertIn("渠道 zip", op["output"])
        self.assertIn("9.9", op["output"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args[0][:2], ("install", "curl"))

    def test_a_failed_install_records_its_reason_instead_of_vanishing(self):
        with mock.patch.object(toolrunner, "installed_at", return_value=""),                 mock.patch.object(toolrunner, "channel_of", return_value="zip"),                 mock.patch.object(toolrunner, "run_locked",
                                  side_effect=toolinstall.InstallError("下载超时（120 秒）")):
            _status_code, payload = self.plane.tools_install(
                self._ctx(body={"adapter": "curl"}))
            op = self._wait(payload["op"]["id"])
        self.assertEqual(op["status"], "failed")
        self.assertIn("下载超时", op["reason"])

    def test_a_dry_run_queues_nothing(self):
        before = len(self.store.tool_ops(50))
        status, payload = self.plane.tools_install(
            self._ctx(body={"adapter": "curl", "dryRun": True}))
        self.assertEqual(status, 200)
        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["channel"], "zip")
        self.assertEqual(len(self.store.tool_ops(50)), before)

    # ---------------------------------------------- 自检：缺件是警告，不是故障

    def test_the_setup_check_points_at_the_place_that_fixes_it(self):
        rows = [_row("curl"), _row("nmap")]
        with mock.patch.object(toolkit, "detect", return_value=rows):
            _status_code, payload = self.plane.setup(self._ctx())
        check = next(item for item in payload["checks"] if item["id"] == "tools")
        # 缺工具不影响"能不能扫"，只影响"能不能按名字拉起客户端"——报 fail 会让一个
        # 完全能用的部署看起来是坏的。
        self.assertEqual(check["level"], "warn")
        self.assertIn("工具管理", check["hint"])
        self.assertIn("2", check["title"])

        with mock.patch.object(toolkit, "detect",
                               return_value=[_row("curl", installed=True)]):
            _status_code, ok_payload = self.plane.setup(self._ctx())
        ok_check = next(item for item in ok_payload["checks"] if item["id"] == "tools")
        self.assertEqual(ok_check["level"], "ok")


class ToolsCardContractTestCase(unittest.TestCase):
    """界面这一侧的硬契约：位置、加载、轮询、委托、以及"不许谎报能力"。"""

    @classmethod
    def setUpClass(cls):
        def read(name):
            with open(os.path.join(WEB_DIR, name), "r", encoding="utf-8") as handle:
                return handle.read()
        cls.html = read("index.html")
        cls.js = read("app.js")
        cls.css = read("style.css")
        with open(os.path.join(ROOT, "pentestd", "chat.py"),
                  encoding="utf-8") as handle:
            cls.chat = handle.read()

    def test_the_card_sits_between_the_deployment_info_and_the_gate(self):
        order = [self.html.index('id="%s"' % name) for name in
                 ("setup-info", "tools-card", "authz-card")]
        self.assertEqual(order, sorted(order),
                         "外部工具卡必须排在一处：部署信息之后、侵入授权之前")

    def test_the_card_is_loaded_and_reset_with_the_rest_of_the_page(self):
        tab = _function_body(self.js, "function loadSetupTab(")
        self.assertIn("loadTools(force);", tab)
        loading = _function_body(self.js, "function renderSetupLoading(")
        self.assertIn("#tools-panel", loading)
        self.assertIn("$('#tools-check').addEventListener('click'", self.js)

    def test_leaving_the_tab_stops_the_polling(self):
        # 定时器不会因为页面被换走而自己消失，而它每 1.5 秒打一次服务端。
        self.assertIn("if (state.tab === 'setup' && name !== 'setup') stopToolsWatch();",
                      self.js)

    def test_the_row_buttons_are_delegated_on_a_static_container(self):
        self.assertIn("[data-tool-action]", self.js)
        self.assertIn("runToolAction(button.dataset.toolAction, button.dataset.toolAdapter,",
                      self.js)
        # 行是每 1.5 秒重画一次的：逐个 addEventListener 会随每次重画漏掉一批监听。
        row = _function_body(self.js, "function toolRow(")
        self.assertNotIn(".addEventListener(", row)

    def test_a_system_provided_tool_gets_no_delete_button(self):
        row = _function_body(self.js, "function toolRow(")
        # 分支顺序就是判定顺序：装着但渠道是 manual 的那一支必须在"装着"之前。
        self.assertLess(row.index("item.installed && item.channel === 'manual'"),
                        row.index("} else if (item.installed) {"))

    def test_the_endpoints_and_the_cadence_are_the_ones_the_server_serves(self):
        for needle in ("apiGet('/tools')", "apiGet('/tools/ops')",
                       "apiPost('/tools/check'", "apiPost(action === 'install' "
                       "? '/tools/install' : '/tools/remove'"):
            self.assertIn(needle, self.js)
        self.assertIn("const TOOLS_REFRESH_MS = 1500;", self.js)

    def test_the_groups_start_collapsed_and_survive_a_repaint(self):
        """默认收起，但**重画不许把用户展开的那一组收回去**。

        这张卡会自己重画（轮询 1.5 秒一次、装完之后重取整张）。要是重画时按"默认
        收起"重新建节点，用户点开的那一组会在 1.5 秒后自己关上——而它偏偏只在
        "有操作在跑"时才这样，看起来像界面在跳。
        """
        body = _function_body(self.js, "function renderTools(")
        self.assertIn("el('details', 'tool-group')", body)
        self.assertIn("box.open = state.toolOpen.has(label);", body)
        # 顺序是承重墙：先读 DOM 再 clear，读完再清就永远是"全部收起"。
        self.assertLess(body.index("rememberToolOpen(panel)"), body.index("clear(panel)"))
        # 读的是 DOM，不是自己维护的副本（副本要靠 toggle 才准，而 toggle 不冒泡）。
        self.assertIn("$$('details.tool-group[open]', panel)", self.js)

    def test_a_collapsed_group_head_says_what_is_missing(self):
        """收起时标题行就是那一组的全部可见信息，所以读数必须写在头上。"""
        head = _function_body(self.js, "function toolGroupHead(")
        self.assertIn("已装 ${ready}/${rows.length}", head)
        self.assertIn("tool-badge tool-missing", head)
        self.assertIn("tool-badge tool-busy", head)

    def test_the_recent_operations_start_collapsed_but_flag_a_failure(self):
        ops = _function_body(self.js, "function toolOpList(")
        self.assertIn("el('details', 'tool-ops')", ops)
        self.assertIn("box.open = state.toolOpsOpen;", ops)
        head = _function_body(self.js, "function toolOpsHead(")
        self.assertIn("tool-badge tool-failed", head)

    def test_the_native_marker_is_replaced_so_the_row_reads_as_clickable(self):
        # UA 的 marker 字形随平台变，而这一行是这张卡唯一的主入口。
        self.assertIn("::-webkit-details-marker", self.css)
        self.assertIn("content: '▸'", self.css)   # ▸ 收起
        self.assertIn("content: '▾'", self.css)   # ▾ 展开

    def test_the_rows_are_two_column_so_the_buttons_do_not_add_a_line_each(self):
        self.assertIn(".tool-body { flex: 1 1 auto;", self.css)
        self.assertIn(".tool-actions { flex: 0 0 auto;", self.css)

    def test_the_chat_tool_shares_the_same_serial_lock(self):
        """两条入口写同一份目录与注册表；共用一把锁才叫串行。"""
        self.assertEqual(self.chat.count("toolrunner_module.run_locked("), 2)
        # 全模块断言比"截取那个函数体"更硬也更省：`_tool_install_tool` 是唯一
        # 会装/删工具的地方，只要整个 chat.py 里再没有直连的调用，就是都走了锁。
        self.assertNotIn("toolinstall_module.install(", self.chat)
        self.assertNotIn("toolinstall_module.remove(", self.chat)
