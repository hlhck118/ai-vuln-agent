# -*- coding: utf-8 -*-
"""DSH 面板插件（`dsh-plugins/dsh-pentest-console`）的契约用例。

这一份**不跑浏览器**：它守的是"插件与平台之间的那几条约定"，因为那些约定的
失败形态全都是**静默**的——

* 包名与 `__ModuleLoader__.load({id})` 不一致：模块加载器会报"loaded without
  registering <id>"，而那一行在浏览器控制台里，服务端日志上什么都没有；
* 侧栏那一格与主面板用了两个不同的 id：DSH 在"点开一个不存在的主面板"时**抛错**
  并保留当前选中态，看起来像"点了没反应"；
* 面板请求的接口在平台侧被改名：面板显示的是"连不上平台"，而平台一切正常；
* 令牌提示里写的那条命令不存在：使用者照着敲，得到"未知子命令"。

所以每一条都同时钉住**两侧**：插件的清单与注册，以及平台的路由与 CLI。
"""
import contextlib
import io
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from pentestd import cli                                              # noqa: E402
from pentestd.server import ControlPlane                              # noqa: E402
from tests import support                                            # noqa: E402
import install_console_plugin                                        # noqa: E402

PLUGIN_DIR = os.path.join(ROOT, "dsh-plugins", "dsh-pentest-console")
PACKAGE_NAME = "dsh-pentest-console"


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def code_only(source: str) -> str:
    """去掉注释之后的代码。

    这一份里好几条断言是"某段文字**不许**出现"（不嵌 iframe、不拼 HTML、不写死
    颜色），而注释里正需要**写出**那些词才能说明为什么不许——所以这些断言只能看
    代码。留着 `/* … */` 会让它们变成"注释写得越清楚越容易红"。
    """
    without_block = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", " ", without_block, flags=re.MULTILINE)


class PluginPackageTestCase(unittest.TestCase):
    """清单与文件：DSH 装载一个包时真正会读的那几处。"""

    @classmethod
    def setUpClass(cls):
        cls.dir = PLUGIN_DIR
        cls.manifest = json.loads(read(os.path.join(cls.dir, "package.json")))
        cls.client = read(os.path.join(cls.dir, "client.js"))
        cls.patch = read(os.path.join(cls.dir, "cordis.patch.yml"))
        cls.host = read(os.path.join(cls.dir, "index.js"))

    def test_directory_name_matches_package_name(self):
        self.assertEqual(os.path.basename(self.dir), PACKAGE_NAME)
        self.assertEqual(self.manifest["name"], PACKAGE_NAME)

    def test_manifest_declares_bundle_and_client(self):
        dsh = self.manifest.get("dsh") or {}
        self.assertEqual(dsh.get("bundle", {}).get("patch"),
                         "./cordis.patch.yml")
        client = dsh.get("client") or {}
        self.assertEqual(client.get("platform"), "web")
        self.assertTrue(client.get("inject"),
                        "inject 为空时浏览器半拿不到它依赖的客户端模块")

    def test_exports_include_client_and_package(self):
        exports = self.manifest.get("exports") or {}
        self.assertIn(".", exports)
        self.assertIn("./client", exports,
                      "DSH 按 exports['./client'] 找浏览器半")

    def test_patch_inserts_exactly_this_package(self):
        names = re.findall(r"^\s*name:\s*['\"]?([^'\"\s]+)['\"]?\s*$",
                           self.patch, re.MULTILINE)
        self.assertIn(PACKAGE_NAME, names)

    def test_loader_id_equals_package_name(self):
        """模块加载器按 id 认包：两处不一致时页面会静默地少一个插件。"""
        found = re.search(r"__ModuleLoader__\.load\(\{\s*id:\s*['\"]([^'\"]+)['\"]",
                          self.client)
        self.assertIsNotNone(found, "client.js 不是模块加载器要的形状")
        self.assertEqual(found.group(1), PACKAGE_NAME)

    def test_sidebar_entry_and_main_panel_share_one_id(self):
        """两处 id 必须相同——DSH 用侧栏那一格的 id 去 main 里找面板。"""
        ids = set(re.findall(r"\bid:\s*PANEL_ID\b", self.client))
        self.assertTrue(ids, "侧栏那一格没有用 PANEL_ID 注册")
        self.assertIn("key: PANEL_ID", self.client,
                      "主面板没有按同一个 id 注册")
        self.assertEqual(self.client.count("const PANEL_ID = "), 1,
                         "PANEL_ID 应当只有一处定义")

    def test_registers_both_slots(self):
        for slot in ("'main'", "'sidebar.panellist'"):
            self.assertIn(slot, self.client, "没有注册 %s" % slot)

    def test_does_not_import_host_ui_packages(self):
        """不 import 宿主的 UI 组件包：那会让面板与 DSH 内部组件版本绑死。"""
        self.assertNotIn("@deepseek-ai/dsh-client-ui-primitives", self.client)
        for match in re.findall(r"require\(\s*['\"]([^'\"]+)['\"]", self.client):
            self.assertIn(match, ("react", "react/jsx-runtime", "react-dom"),
                          "浏览器半 import 了白名单之外的模块：%s" % match)

    def test_locale_files_carry_display_text(self):
        for name in ("zh", "en"):
            payload = json.loads(read(os.path.join(self.dir, "locale",
                                                   "%s.json" % name)))
            self.assertTrue(payload.get("title"))
            self.assertTrue(payload.get("description"))

    def test_icon_declared_and_present(self):
        icon = self.manifest.get("icon")
        self.assertEqual(icon, "./icon.svg")
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "icon.svg")))

    def test_host_half_is_a_plugin(self):
        self.assertIn("export function apply", self.host)

    def test_token_hint_names_a_real_command(self):
        """提示里那条命令必须真的存在（它就在 cli.py 的子命令表里）。"""
        self.assertIn("pentestd console-token", self.client)
        self.assertIn("console-token", cli.build_parser()._subparsers
                      ._group_actions[0].choices)

    # ---- 这一版新增的界面面（目标 / 报告 / 节点 / 连接设置）与它的四条硬约束

    def test_settings_section_is_registered_with_its_own_id(self):
        """连接设置同时是 DSH 设置页里的一节：那一节的 id 与侧栏/主面板的 id 不同，
        两处混用会让设置页去 main 里找一个不存在的主面板（DSH 会抛错）。"""
        self.assertIn("ctx.slots.inject('settings.section'", self.client)
        self.assertIn("id: SETTINGS_ID", self.client)
        self.assertEqual(self.client.count("const SETTINGS_ID = "), 1)
        self.assertNotEqual(
            re.search(r"const PANEL_ID = '([^']+)'", self.client).group(1),
            re.search(r"const SETTINGS_ID = '([^']+)'", self.client).group(1))

    def test_every_tab_has_a_body(self):
        """页签与渲染分支必须一一对应：漏一个的表现是那一页永远空白。"""
        tabs = set(re.findall(r"\[\s*'([a-z]+)',\s*t\('tab", self.client))
        self.assertTrue(tabs, "没找到页签表")
        handled = set(re.findall(r"tab === '([a-z]+)'", self.client))
        for key in sorted(tabs - {'overview'}):
            self.assertIn(key, handled, "页签 %s 没有任何渲染分支" % key)

    def test_every_used_locale_key_is_defined_in_both_languages(self):
        """`t('x')` 用了但字典里没有 → 界面上直接显示 `x`；只有中文没有英文 →
        英文界面显示中文键名。两种都是"看着像功能没做完"，不是报错。"""
        block = self.client.split("const DICT = {", 1)[1].split("const FALLBACK", 1)[0]
        zh_part, rest = block.split("zh: {", 1)[1].split("en: {", 1)
        en_part = rest.split("\n      },", 1)[0]
        pattern = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*'")
        zh = set(pattern.findall(zh_part))
        en = set(pattern.findall(en_part))
        # `ctx.get('timer')` 这种调用里也有 `t('…')` 三个字符，所以必须要求
        # `t(` 前面不是标识符或点号——否则这条断言会把服务名当成文案键。
        used = set(re.findall(r"(?<![A-Za-z0-9_$.])t\(\s*'([A-Za-z_][A-Za-z0-9_]*)'",
                              code_only(self.client)))
        self.assertTrue(used, "没找到任何 t('…') 调用")
        self.assertEqual(sorted(zh - en), [], "这些键只有中文，英文界面会显示键名")
        self.assertEqual(sorted(en - zh), [], "这些键只有英文")
        self.assertEqual(sorted(used - zh), [], "这些键被用了但字典里没有")

    def test_report_is_rendered_as_text_never_as_markup(self):
        """报告正文里有**第三方可控**的字符串（目标地址、端口横幅、证据原文）。
        唯一安全的前提是"全部走 React 文本节点"——一旦出现 raw HTML 注入，
        报告就成了一个注入口。"""
        code = code_only(self.client)
        self.assertNotIn("dangerouslySetInnerHTML", code)
        self.assertNotIn("innerHTML", code)
        self.assertIn("mdNodes(", code, "报告渲染函数不见了")

    def test_no_raw_colors_in_the_client_half(self):
        """颜色只能来自主题令牌：硬编码色的失败形态是"黑夜模式下某一页刺眼"，
        而它只在某一个主题下出现。"""
        hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", code_only(self.client))
        self.assertEqual(hexes, [], "浏览器半出现硬编码颜色：%s" % hexes)

    def test_asset_type_comes_from_the_platform_not_a_second_table(self):
        """资产类型印平台贴的 `typeLabel`，插件这一侧不自己维护对照表。

        这一页原先直接印 `item.row.type`，于是同一份数据在网页资产树上写「服务」、
        在这里写 `service`，而链路图导出的 SVG 上又是第三种写法。词表收口到
        `pentestd/models.ASSET_TYPE_LABELS` 之后，读到的每个出口都是同一个词；
        `type` 仍然照原样回作为兜底（离线缓存里的旧数据没有 `typeLabel`）。
        """
        code = code_only(self.client)
        self.assertIn("item.row.typeLabel || item.row.type", code,
                      "资产类型没读平台贴的 typeLabel")
        self.assertNotIn("chip(item.row.type ||", code,
                         "资产类型又回到直接印机器词了")

    def test_all_injected_selectors_are_namespaced(self):
        """注入的样式进的是**宿主的**文档：裸标签选择器会改到 DSH 自己的界面。"""
        block = self.client.split("const CSS = [", 1)[1].split("].join('')", 1)[0]
        selectors = re.findall(r"'\.([A-Za-z0-9_-]+)\{", block)
        self.assertTrue(selectors, "没找到样式选择器")
        for name in selectors:
            self.assertTrue(name.startswith("pp-"),
                            "选择器没有 pp- 前缀：.%s" % name)

    def test_storage_key_is_still_v1(self):
        """沿用 v1 那个键：使用者上一版已经粘过一次令牌，换键等于让他再找一次。"""
        self.assertIn("'dsh-pentest-console/v1'", self.client)

    def test_does_not_embed_the_platform_ui(self):
        """三条被否掉的做法各留一条反向断言：iframe 平台页面、解析平台 HTML、
        把 DSH 自己再嵌一层。任何一条回来都会让"界面只有一套"重新变成两套。"""
        code = code_only(self.client)
        self.assertNotIn("<iframe", code)
        self.assertNotIn("iframe", code)
        self.assertNotIn("/dsh/", code)

    def test_endpoints_declared_in_the_marker_block(self):
        """端点表是故意写成注释的：它随代码一起被审，且能被用例逐条核对。"""
        block = self.client.split("本插件请求的平台端点", 1)[1].split("*/", 1)[0]
        paths = re.findall(r"^\s*//\s*(/api/v1/\S+)\s*$", block, re.MULTILINE)
        self.assertGreaterEqual(len(paths), 5, "端点表少了几条")
        self.assertIn("/api/v1/panel", paths)


class PluginInstallerTestCase(unittest.TestCase):
    """安装脚本：它会**改另一份 DSH 的数据目录**，所以每一步都要留痕、可撤销。

    这一组不跑 pnpm（`--no-install`），跑的是"清单那一步"与"报告那一段"——
    而那两步正是"装到别人那份 DSH 里"时唯一会被读到的部分。
    """

    def setUp(self):
        self.directory, _store = support.temporary_store("installer.db")
        self.db = os.path.join(self.directory.name, "pentest.db")
        self.home = os.path.join(self.directory.name, "somebody-else-home")
        self.profile = os.path.join(self.home, "profiles", "pentest-web")
        os.makedirs(self.profile)
        self.manifest = os.path.join(self.profile, "package.json")
        with open(self.manifest, "w", encoding="utf-8") as handle:
            json.dump({"name": "dsh-profile-pentest-web", "private": True,
                       "dependencies": {"some-other-plugin": "^1.0.0"},
                       "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}}},
                      handle)

    def tearDown(self):
        self.directory.cleanup()

    def test_backup_is_byte_identical_and_named_with_a_stamp(self):
        backup = install_console_plugin.backup_manifest(self.manifest)
        self.assertTrue(backup, "没有留下备份")
        self.assertIn(".bak-", os.path.basename(backup))
        self.assertEqual(read(backup), read(self.manifest))

    def test_backup_of_a_missing_manifest_is_empty_not_an_error(self):
        self.assertEqual(install_console_plugin.backup_manifest(
            os.path.join(self.profile, "nope.json")), "")

    def test_home_label_distinguishes_your_dsh_from_the_platform_one(self):
        own = install_console_plugin.home_for(self.db)
        self.assertIn("平台自带", install_console_plugin.home_label(own, own))
        self.assertIn("电脑上", install_console_plugin.home_label(self.home, own))

    def test_no_install_still_writes_the_bundle_entry_and_keeps_the_rest(self):
        """`--no-install` 只跳过 pnpm：bundles 那一处仍然要写，且**不许动**
        同一个文件里别人的依赖。"""
        from pentestd import dsh_runtime
        if not dsh_runtime.locate_dsh():
            self.skipTest("本机没有 DSH 运行时，装不了")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = install_console_plugin.main([
                "--db", self.db, "--home", self.home,
                "--profile", "pentest-web", "--no-install"])
        text = buffer.getvalue()
        manifest = json.loads(read(self.manifest))
        bundles = manifest["dsh"]["profile"]["bundles"]
        self.assertIn(PACKAGE_NAME, bundles)
        self.assertIn("@deepseek-ai/dsh-base", bundles, "原有条目被覆盖了")
        self.assertEqual(manifest["dependencies"],
                         {"some-other-plugin": "^1.0.0"}, "动了别人的依赖")
        # 没有 node_modules 时"三处一致"必须是**否**，而且要说清怎么撤销。
        self.assertEqual(code, 7)
        self.assertIn("三处一致  : **否**", text)
        self.assertIn("remove %s" % PACKAGE_NAME, text)
        self.assertIn("package.json.bak-", text)

    def test_copy_mode_installs_without_pnpm_and_lands_three_places(self):
        """`--copy` 那条路：拷贝 + 两处清单，全都由脚本自己做。

        这是"在一份**正被 DSH 打开着**的 profile 上装插件"时该走的路——`dsh plugin add`
        会为这件事启动一个完整的桌面宿主（实测它会一直活着、占住 stdout、拿着
        `package.json.lock`），而一个 `file:` 依赖真正需要的只是"把包放进 node_modules"。
        """
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = install_console_plugin.main([
                "--db", self.db, "--home", self.home,
                "--profile", "pentest-web", "--copy"])
        text = buffer.getvalue()
        target = os.path.join(self.profile, "node_modules", PACKAGE_NAME)
        self.assertTrue(os.path.isfile(os.path.join(target, "client.js")))
        self.assertTrue(os.path.isfile(os.path.join(target, "cordis.patch.yml")))
        manifest = json.loads(read(self.manifest))
        self.assertIn(PACKAGE_NAME,
                      manifest["dsh"]["profile"]["bundles"])
        self.assertIn(PACKAGE_NAME, manifest["dependencies"])
        self.assertIn("file:", manifest["dependencies"][PACKAGE_NAME])
        self.assertEqual(manifest["dependencies"].get("some-other-plugin"),
                         "^1.0.0")
        self.assertEqual(code, 0, text)
        self.assertIn("三处一致  : 是", text)

    def test_copy_mode_is_idempotent(self):
        """装第二次不许把 node_modules 里的东西搞成"两份叠在一起"。"""
        for _round in range(2):
            with contextlib.redirect_stdout(io.StringIO()):
                code = install_console_plugin.main([
                    "--db", self.db, "--home", self.home,
                    "--profile", "pentest-web", "--copy"])
            self.assertEqual(code, 0)
        target = os.path.join(self.profile, "node_modules", PACKAGE_NAME)
        self.assertEqual(sorted(os.listdir(target)),
                         sorted(os.listdir(os.path.join(
                             ROOT, "dsh-plugins", PACKAGE_NAME))))

    def test_missing_package_dir_is_refused_before_touching_anything(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = install_console_plugin.main([
                "--db", self.db, "--home", self.home,
                "--package-dir", os.path.join(self.directory.name, "not-a-plugin"),
                "--no-install"])
        self.assertEqual(code, 2)
        self.assertNotIn(PACKAGE_NAME, read(self.manifest))


class PluginPlatformContractTestCase(unittest.TestCase):
    """插件请求的东西，在平台这一侧真的存在。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("contract.db")
        self.plane = ControlPlane(self.store, os.path.join(ROOT, "web"))

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_panel_endpoint_exists_with_the_shape_the_panel_reads(self):
        paths = [pattern.pattern for _method, pattern, _h, _auth
                 in self.plane.router.routes]
        self.assertTrue(any(path.endswith(r"/api/v1/panel$") for path in paths),
                        "面板请求的 /api/v1/panel 不在路由表里")

    def test_every_endpoint_the_panel_calls_exists_on_the_platform(self):
        """跨文件核对：客户端那张端点表逐条拿去路由表里找。

        端点改名在两侧各自的用例里都看不出来——插件那份只读自己的字符串，
        平台那份只读自己的路由表；只有把两张表对起来才发现对不上。
        """
        client = read(os.path.join(PLUGIN_DIR, "client.js"))
        block = client.split("本插件请求的平台端点", 1)[1].split("*/", 1)[0]
        paths = re.findall(r"^\s*//\s*(/api/v1/\S+)\s*$", block, re.MULTILINE)
        patterns = [pattern.pattern for _method, pattern, _h, _auth
                    in self.plane.router.routes]
        for path in paths:
            # `{eid}` 这类占位在路由表里是命名组，转成正则再比。
            wanted = re.sub(r"\{[^}]+\}", r"[^/]+", path)
            hit = any(re.fullmatch(pattern, path.replace("{eid}", "E-1"))
                      or pattern.endswith(wanted + "$")
                      for pattern in patterns)
            self.assertTrue(hit, "插件请求的 %s 在平台路由表里不存在" % path)

    def test_client_fetches_the_same_path(self):
        client = read(os.path.join(PLUGIN_DIR, "client.js"))
        self.assertIn("'/api/v1/panel'", client,
                      "面板请求的路径与平台路由必须逐字一致")

    def test_panel_requires_login_like_every_other_console_route(self):
        for method, pattern, _handler, auth in self.plane.router.routes:
            if pattern.pattern.endswith(r"/api/v1/panel$"):
                self.assertEqual(auth, "session",
                                 "面板接口默认（最严）就该要凭据")
                self.assertEqual(method, "GET")

    def test_loopback_origin_is_the_only_cross_origin_allowed(self):
        from pentestd.server import CORS_ORIGIN_RE
        self.assertTrue(CORS_ORIGIN_RE.match("http://127.0.0.1:8790"))
        self.assertTrue(CORS_ORIGIN_RE.match("http://localhost:8790"))
        self.assertFalse(CORS_ORIGIN_RE.match("https://evil.example"))
        self.assertFalse(CORS_ORIGIN_RE.match("http://127.0.0.1.evil.example"))


if __name__ == "__main__":
    unittest.main()
