# -*- coding: utf-8 -*-
"""平台工具在 DSH 里的唯一入口（`pentestd/dsh_bridge.py`）：运行时补丁、暴露档位、
逐动作放行的写入。

这一份测试钉的是"**只有一条路**"这件事：

* 平台工具只写进**运行时专用补丁**（平台自己那份 DSH home 的
  `platform-tools.patch.yml`），由「DSH 运行时」通道在拉起时加载；
* 插件包里那份 `cordis.patch.yml` 只带界面，**不**再顺手把工具挂进使用者的 DSH ——
  装插件不该改变使用者对话里能看到的工具；
* 默认纯读；写入要**点名放行一个动作**，执行时按那个动作自己的档位判。
"""
import os
import shutil
import tempfile
import unittest

import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import dsh_bridge as br                             # noqa: E402
from pentestd import dsh_runtime as rt                           # noqa: E402

# 实测：readonly 52 / probe 88 / audit 94 / full 100（full 按操作员计，管理员 107）
# ARTEX 那 59 项工具并进 chat.TOOLS 时一次性改了这组数：只读 +29、探测 +23、审计 +3、
# 完全作业 +4（其中后四条进 ADMIN_TOOLS，管理员差额从 3 变 7）。并进的是同一张
# `policy.TOOL_LEVEL` 表，所以四档都是累加口径，四个数必须一次改完。
# 加 `http_request`（探测档）：probe 35→36，而 audit / full 是累加口径，跟着 38→39、
# 44→45，管理员侧 47→48。与上面几次一样，这几行必须一次改完。
# 加 `agent_loop`、`netscan`（都是探测档）时各漏改过一次：代码与 guide.js 都加了，只有这里还写着旧数。
# 加 `context_search` / `context_decompress` / `context_status`（都是只读档）时又漏了第三次——
# 这一次是**这一组数本身**没有跟着变：它们漂了整整三项而没有任何地方会红，因为
# 旧断言只钉"这一档等于读数"，读数写错了它自己也对得上。所以下面还有一条独立性检查。
#
# 清空任务队列那次是**反向**的：`clear_finished_jobs` 进了 `policy.ADMIN_TOOLS`（清空任务
# 队列是跨用户的动作，与 `POST /jobs/clear` 同一条判据），于是操作员的 full 档从 45 掉到
# 44，而管理员那一侧不动。数字变小同样要改这一行 —— 否则漂移只是换了个方向。
EXPECTED_COUNTS = {"readonly": 52, "probe": 88, "audit": 94, "full": 100}

# 管理员那一档多出来的三个正是 `policy.ADMIN_TOOLS`：单钉操作员那个数会漏掉"某一项被
# 悄悄挪到管理员侧"这种改动（操作员少了 1、管理员没变，两边都不红）。
EXPECTED_ADMIN_FULL = 107


class _Store:
    """只要设置读写就够（`tier_of` / `write` / `status` 都只用这两个）。"""

    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_setting(self, key, default=""):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value


class TierTests(unittest.TestCase):
    def test_tiers_are_the_platform_policy_ids(self):
        self.assertEqual(br.tiers(), ["readonly", "probe", "audit", "full"])

    def test_exposure_counts_and_monotonicity(self):
        counts = {}
        for tier in br.tiers():
            names = {t["name"] for t in br.exposed_tools(tier)}
            counts[tier] = len(names)
            self.assertTrue(names, tier)
        self.assertEqual(counts, EXPECTED_COUNTS)
        admin_full = len({t["name"] for t in br.exposed_tools("full",
                                                              role="admin")})
        self.assertEqual(admin_full, EXPECTED_ADMIN_FULL)
        order = br.tiers()
        for narrow, wide in zip(order, order[1:]):
            narrow_names = {t["name"] for t in br.exposed_tools(narrow)}
            wide_names = {t["name"] for t in br.exposed_tools(wide)}
            self.assertTrue(narrow_names <= wide_names,
                            "%s 不是 %s 的子集" % (narrow, wide))

    def test_admin_only_tools_need_the_admin_role(self):
        """`install_plugin` 是部署级动作（会改使用者本机的 DSH），所以它是**双闸**：
        普通角色即使在 full 档也拿不到，只有管理员拿得到。
        """
        ordinary = {t["name"] for t in br.exposed_tools("full")}
        admin = {t["name"] for t in br.exposed_tools("full", role="admin")}
        self.assertNotIn("install_plugin", ordinary)
        self.assertIn("install_plugin", admin)
        ro = {t["name"] for t in br.exposed_tools("readonly")}
        for name in ("start_scan", "start_exploit", "run_command"):
            self.assertNotIn(name, ro)

    def test_default_and_invalid_settings_fall_back_to_narrowest(self):
        self.assertEqual(br.tier_of(_Store()), "readonly")
        self.assertEqual(br.tier_of(_Store({rt.SETTING_BRIDGE_TIER: "没这个档"})), "readonly")
        self.assertEqual(br.tier_of(_Store({rt.SETTING_BRIDGE_TIER: "audit"})), "audit")


class WriteActionTests(unittest.TestCase):
    """写入动作：默认没有，放行一个就只多那一个。"""

    def test_default_is_no_write_action_at_all(self):
        """默认纯读。这条是整套设计的地基：一旦默认带写入，"模型能不能动我的
        目标"就成了一个没有人做过的决定。"""
        self.assertEqual(br.write_actions_of(_Store()), [])
        names = {t["name"] for t in br.exposed_tools("readonly")}
        self.assertNotIn("start_probe", names)

    def test_only_whitelisted_actions_can_be_granted(self):
        """名字写错**不能**落库：存一个永远不生效的名字，症状只能是"点了没反应"。"""
        store = _Store()
        got = br.set_write_actions(store, ["rm_rf", "start_probe", "start_probe"])
        self.assertFalse(got["ok"])
        self.assertEqual(got["actions"], ["start_probe"])
        self.assertEqual(len(got["rejected"]), 1)
        self.assertIn("rm_rf", got["rejected"][0]["reason"])
        self.assertEqual(store.get_setting(br.SETTING_WRITE), "start_probe")

    def test_a_granted_action_shows_up_with_a_write_marker(self):
        names = {t["name"]: t for t in br.exposed_tools("readonly", writes=["start_probe"])}
        self.assertIn("start_probe", names)
        self.assertTrue(names["start_probe"].get("write"))
        self.assertIn("写入动作", names["start_probe"]["description"])
        # 只多那一个：其它写入面一个都不该跟着出现
        for other in ("start_scan", "run_command", "start_exploit", "install_plugin"):
            self.assertNotIn(other, names)
        self.assertEqual(len(names), EXPECTED_COUNTS["readonly"] + 1)

    def test_execution_uses_the_actions_own_tier(self):
        """放行一个动作 ≠ 把整档放宽。

        `start_probe` 在自己的 `probe` 档执行（作用域与授权照旧），而其它工具仍按
        通道的档位判 —— 否则"只读通道 + 一个写入动作"就名不副实了。
        """
        self.assertEqual(br.execution_policy("readonly", "start_probe"), "probe")
        self.assertEqual(br.execution_policy("readonly", "list_jobs"), "readonly")
        # 当前档更高时**不要降**：降档会让一个本该允许的动作莫名其妙被拒
        self.assertEqual(br.execution_policy("full", "start_probe"), "full")

    def test_revoke_is_an_explicit_write_of_an_empty_list(self):
        """收回写成空串而不是删键：删键会让"曾经放行过"在库里消失，而撤回是一次
        可审计的动作。"""
        store = _Store({br.SETTING_WRITE: "start_probe"})
        got = br.set_write_actions(store, [])
        self.assertTrue(got["ok"])
        self.assertEqual(got["actions"], [])
        self.assertEqual(store.get_setting(br.SETTING_WRITE, "缺键"), "")

    def test_a_broken_store_value_reads_as_no_grant(self):
        """库里被写坏（库被外部改过/旧版本残留）时读成"没放行" —— 更窄的那一侧。"""
        self.assertEqual(br.write_actions_of(_Store({br.SETTING_WRITE: "start_probe,, x"})),
                         ["start_probe"])
        self.assertEqual(br.write_actions_of(_Store({br.SETTING_WRITE: "none"})), [])


class PatchTextTests(unittest.TestCase):
    def text(self, **kw):
        args = dict(python=r"C:\Python312\python.exe",
                    script=r"F:\proj\tools\platform_mcp_server.py",
                    home=r"F:\proj\data\dsh-home", root=r"F:\proj", tier="readonly")
        args.update(kw)
        return br.patch_text(**args)

    def test_it_carries_only_the_tool_insert(self):
        """这份补丁只做一件事：把平台工具挂进**运行时**。界面那一格属于插件包，
        出现在这里就说明两份文件又混在一起了。"""
        body = self.text()
        self.assertIn("id: %s" % br.BRIDGE_INSERT_ID, body)
        self.assertIn("name: '@deepseek-ai/dsh-mcp-client'", body)
        self.assertNotIn(br.UI_INSERT_ID, body)
        self.assertNotIn("dsh-pentest-console", body)
        for needle in ("serverName: %s" % br.SERVER_NAME, "transport: stdio",
                       "toolCallTimeoutMs: %d" % br.TOOL_CALL_TIMEOUT_MS):
            self.assertIn(needle, body, needle)
        self.assertIn("# **请勿手工编辑**", body)   # 偶然打开的人要知道这是生成的

    def test_windows_paths_are_single_quoted_and_not_escaped(self):
        """单引号标量里的反斜杠是字面量 —— 所以 Windows 路径不该被转义成 `\\\\`。"""
        body = self.text()
        self.assertIn("command: 'C:\\Python312\\python.exe'", body)
        self.assertNotIn("\\\\", body)

    def test_single_quotes_in_values_are_doubled(self):
        body = self.text(root="F:\\we'ird\\proj")
        self.assertIn("PENTEST_HOME: 'F:\\we''ird\\proj'", body)

    def test_env_carries_home_db_and_tier(self):
        body = self.text(tier="probe")
        self.assertIn("PENTEST_HOME: 'F:\\proj'", body)
        self.assertIn("PENTEST_DB: 'F:\\proj\\data\\pentest.db'", body)
        self.assertIn("PENTEST_TIER: 'probe'", body)
        self.assertNotIn("PENTEST_SESSION", body)
        self.assertNotIn("PENTEST_WRITE", body)   # 没放行就不写这个键

    def test_session_pin_is_optional(self):
        self.assertIn("PENTEST_SESSION: 'session-1'", self.text(session_id="session-1"))

    def test_granted_actions_are_written_into_env_and_header(self):
        body = self.text(writes=["start_probe"])
        self.assertIn("PENTEST_WRITE: 'start_probe'", body)
        self.assertIn("# 写入动作：start_probe", body)


class FileActionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dsh-bridge-")
        self.home = os.path.join(self.dir, "dsh-home")
        self.root = os.path.join(self.dir, "proj")
        os.makedirs(self.home, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_runtime_patch_lives_in_the_platform_home(self):
        """判据不是"某个路径下有文件"，而是"它在平台自己的 home 里"：只有这样
        使用者装插件时才不会被顺手挂上平台工具。"""
        got = br.write(_Store(), self.home, self.root, tier="readonly", python="py.exe")
        self.assertTrue(got["ok"])
        self.assertEqual(got["path"], br.runtime_patch_path(self.home))
        self.assertEqual(os.path.dirname(got["path"]), self.home)
        self.assertTrue(os.path.isfile(got["path"]))
        # 插件包里那份不该被这次写入碰到
        self.assertFalse(os.path.exists(br.patch_path(self.root)))

    def test_write_is_idempotent_and_remembers_the_tier(self):
        store = _Store()
        first = br.write(store, self.home, self.root, tier="probe", python="py.exe")
        self.assertTrue(first["ok"])
        self.assertTrue(first["changed"])
        self.assertEqual(first["tier"], "probe")
        self.assertEqual(store.get_setting(rt.SETTING_BRIDGE_TIER), "probe")
        second = br.write(store, self.home, self.root, python="py.exe")
        self.assertTrue(second["ok"])
        self.assertFalse(second["changed"])       # 内容没变就不碰盘
        self.assertEqual(second["tier"], "probe")

    def test_write_refuses_when_the_home_is_missing(self):
        """home 不在时不替它造：那是"部署"该做的事，不是补丁该做的事。"""
        missing = os.path.join(self.dir, "no-home")
        got = br.write(_Store(), missing, self.root, tier="readonly", python="py.exe")
        self.assertFalse(got["ok"])
        self.assertIn("DSH home", got["error"])

    def test_unknown_tier_is_refused_and_writes_nothing(self):
        store = _Store()
        got = br.write(store, self.home, self.root, tier="godmode", python="py.exe")
        self.assertFalse(got["ok"])
        self.assertIn("档位", got["error"])
        self.assertFalse(os.path.exists(br.runtime_patch_path(self.home)))
        self.assertEqual(store.values, {})

    def test_remove_deletes_the_runtime_patch(self):
        store = _Store()
        br.write(store, self.home, self.root, tier="readonly", python="py.exe")
        path = br.runtime_patch_path(self.home)
        self.assertTrue(os.path.exists(path))
        got = br.remove(store, self.home, self.root)
        self.assertTrue(got["ok"])
        self.assertTrue(got["removed"])
        self.assertFalse(os.path.exists(path))

    def test_status_is_safe_when_nothing_exists(self):
        """补丁不在/home 不存在时 `status` 也不该抛 —— 诊断要能跑。"""
        store = _Store()
        got = br.status(store, os.path.join(self.dir, "absent"), self.root)
        self.assertFalse(got["patchPresent"])
        self.assertFalse(got["ownsPatch"])
        self.assertFalse(got["bridgeMounted"])
        self.assertEqual(got["tier"], "readonly")
        self.assertEqual(got["tierOptions"], br.tiers())
        self.assertEqual(got["writes"], [])
        self.assertEqual(got["writeActions"], list(br.WRITE_ACTIONS))
        self.assertEqual(got["exposedCount"], EXPECTED_COUNTS["readonly"])
        self.assertEqual(len(got["exposed"]), got["exposedCount"])
        self.assertFalse(got["enabled"])          # 运行时通道默认关

    def test_status_recognises_our_own_patch(self):
        store = _Store()
        br.write(store, self.home, self.root, tier="audit", python="py.exe")
        got = br.status(store, self.home, self.root)
        self.assertTrue(got["patchPresent"])
        self.assertTrue(got["ownsPatch"])
        self.assertTrue(got["bridgeMounted"])
        self.assertEqual(got["patchPath"], br.runtime_patch_path(self.home))
        self.assertEqual(got["tier"], "audit")
        self.assertEqual(got["exposedCount"], EXPECTED_COUNTS["audit"])

    def test_status_reports_the_grant_and_its_execution_tier(self):
        """"放行了哪个动作、它按哪一档执行"必须是可读的 —— 否则"放行了一个动作"
        与"把整档放宽"在屏幕上长得一样。"""
        store = _Store()
        br.set_write_actions(store, ["start_probe"])
        br.write(store, self.home, self.root, tier="readonly", python="py.exe")
        got = br.status(store, self.home, self.root)
        self.assertEqual(got["writes"], ["start_probe"])
        self.assertEqual(got["execution"], {"start_probe": "probe"})
        self.assertEqual(got["exposedCount"], EXPECTED_COUNTS["readonly"] + 1)

    def test_status_reports_our_own_script_presence(self):
        got = br.status(_Store(), self.home, self.root)
        self.assertFalse(got["scriptPresent"])     # 临时项目里没有 tools/
        self.assertTrue(got["scriptPath"].endswith(os.path.join("tools", "platform_mcp_server.py")))

    def test_enabled_follows_the_runtime_setting(self):
        self.assertTrue(br.status(_Store({rt.SETTING_RUNTIME: "sdk"}), self.home,
                                  self.root)["enabled"])
        self.assertFalse(br.status(_Store({rt.SETTING_RUNTIME: "原生"}), self.home,
                                   self.root)["enabled"])


class ConsoleAndSessionTests(unittest.TestCase):
    """桥要能**回来问**平台，还得知道问的是**哪条对话**。

    这两样（控制面地址、会话 id）都随补丁的 env 走。它们缺了不会报错——桥会退回
    "问不到人 → 放行"，而放行同样是这条通道的合法结局，所以缺了之后**看起来一切
    正常**。这正是必须用测试把它们钉住的原因：症状是"审查再也没出现过卡片"。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dsh-console-")
        self.home = os.path.join(self.dir, "dsh-home")
        self.root = os.path.join(self.dir, "proj")
        os.makedirs(self.home, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_console_url_defaults_to_the_loopback_address(self):
        body = br.patch_text(python="py.exe", script="s.py", home=self.home,
                             root=self.root, tier="readonly")
        self.assertIn("PENTEST_CONSOLE: '%s'" % br.DEFAULT_CONSOLE, body)

    def test_console_url_comes_from_the_setting_when_present(self):
        store = _Store({br.SETTING_CONSOLE: "http://127.0.0.1:9999"})
        got = br.write(store, self.home, self.root, tier="readonly", python="py.exe")
        with open(got["path"], "r", encoding="utf-8") as handle:
            self.assertIn("PENTEST_CONSOLE: 'http://127.0.0.1:9999'", handle.read())

    def test_two_sessions_do_not_share_one_patch_file(self):
        """共用一份的后果是"后写的赢"：A 会话的审查单会出现在 B 会话的卡片区，
        点下去执行的是别人的命令。所以路径必须能区分会话。"""
        one = br.runtime_patch_path(self.home, "sess-1")
        two = br.runtime_patch_path(self.home, "sess-2")
        self.assertNotEqual(one, two)
        self.assertTrue(one.endswith(".patch.yml"))

    def test_session_ids_are_sanitised_for_the_filesystem(self):
        weird = br.runtime_patch_path(self.home, "a/b:c\\d")
        self.assertEqual(os.path.dirname(weird), self.home)   # 不许逃出 home
        self.assertNotIn("/", os.path.basename(weird))
        self.assertNotIn("\\", os.path.basename(weird))

    def test_a_session_patch_pins_the_session_in_env(self):
        store = _Store()
        got = br.write(store, self.home, self.root, tier="readonly",
                       python="py.exe", session_id="sess-1")
        self.assertTrue(got["path"].endswith("sess-1.patch.yml"))
        with open(got["path"], "r", encoding="utf-8") as handle:
            self.assertIn("PENTEST_SESSION: 'sess-1'", handle.read())
        # 共用那一份不该被这次按会话的写入碰过
        self.assertFalse(os.path.exists(br.runtime_patch_path(self.home)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
