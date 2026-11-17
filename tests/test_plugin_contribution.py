# -*- coding: utf-8 -*-
"""插件对本平台对话的作用：有 / 没有 / 为什么没有。

## 为什么这一组必须单独存在

"插件市场里装的插件在对话里不起作用"这件事，**代码上永远是对的**：能起作用的
（`dsh-purge` 的 23 份手册、`@howmp/dsh-pentest` 的预设）都是磁盘上的静态产物，
读到了就是读到了；起不了作用的（`graph-memory`、`@nanmicoder/dsh-agent-teams`、
`@anionex/dsh-turn-rewind`）把能力注册在 **DSH 宿主**里，而本平台的对话从来不启动
那个宿主。坏掉的从来不是功能，是**界面没说这句话**——使用者的结论只能是"装了个
没用的东西"，或者更糟：去看那句"或在 DSH 侧还没重启加载"，然后一遍遍重启。

所以这里钉三件事：

1. 宿主声明（`cordis.patch.yml` 的 `insert:`）能被**只读地**读出来，而且不会被
   里面的 `config:` 块污染；
2. 判定（`contribution`）对四类插件给出的结论是**可断言**的，尤其是 host 那一类
   必须说清"不作用于对话"以及理由；
3. 这句理由能一路走到接口（`_dsh_public`）与界面（`settings.js`），并且**那句话**
   （"或在 DSH 侧还没重启加载"）不许再出现——它是假的，而且方向明确地把人带歪。

## 绝不去读使用者机器上的东西

所有 manifest 都造在 `tempfile` 目录里，`machine_home` 与 `platform_home` 都指向
临时目录（理由同 `test_plugin_isolation.py`：`set_platform_home` 是进程级状态，
不还原的话失败会出现在别的文件里）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import chat, dshplugins, harness, server        # noqa: E402
from tests import support                                      # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


def _strip_js_comments(text):
    """去掉 JS 注释，**但按字符扫，认得字符串**。

    这一层每个用例都必须先过：本仓库里"断言命中了自己的注释"已经发生过七次，
    而这一轮我自己那段解释里就引用了要禁掉的那句话。

    第一版写成"找 `/*` 就删到 `*/`"，于是它在我**自己刚加的那段文案**上就崩了 ——
    那句话里写着 `preset[s]/*/preset.yml`，其中的 `/*` 被当成了块注释的开头，
    一直吃到文件后面某个 `*/`，把几千行（含要断言的内容）一起删掉了。症状是
    "界面里少了一句话"，而真相是**量具**把中间那段剪掉了。所以这里必须字符串感知：
    引号内的 `/*`、`//` 都是普通字符，转义也要认。
    """
    out, index = [], 0
    length, quote, block, line = len(text), None, False, False
    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""
        if block:
            if char == "*" and nxt == "/":
                block = False
                index += 2
                continue
            index += 1
            continue
        if line:
            if char == "\n":
                line = False
                out.append(char)
            index += 1
            continue
        if quote:
            if char == "\\":
                out.append(text[index:index + 2])
                index += 2
                continue
            if char == quote:
                quote = None
            out.append(char)
            index += 1
            continue
        if char in "\"'`":
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "/" and nxt == "*":
            block = True
            index += 2
            continue
        if char == "/" and nxt == "/":
            line = True
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _read(relative):
    with open(os.path.join(ROOT, relative), "r", encoding="utf-8",
              errors="replace") as handle:
        return handle.read()


class _Sandbox(unittest.TestCase):
    """临时 home + 临时数据目录，tearDown 原样还原进程级状态。"""

    # 诱饵：只出现在"电脑上那份 DSH"的依赖清单里。任何读错家的实现都会把它带出来。
    MACHINE_SENTINEL = "sentinel-from-the-machine-home"

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="t66-")
        # `self.machine` 只作为**诱饵**存在（放一份带哨兵的 profile），平台不该看它。
        self.machine = os.path.join(self.root, "machine-dsh")
        self.platform = os.path.join(self.root, "platform-dsh")
        os.makedirs(os.path.join(self.machine, "profiles", "desktop"),
                    exist_ok=True)
        with open(os.path.join(self.machine, "profiles", "desktop",
                               "package.json"), "w", encoding="utf-8") as fh:
            json.dump({"name": "dsh-profile-desktop", "private": True,
                       "dependencies": {self.MACHINE_SENTINEL: "1.0.0"}}, fh)
        self._saved_platform = dshplugins.platform_home()
        self._saved_env = {key: os.environ.get(key)
                           for key in ("DSH_HOME", "DSH_PROFILE")}
        os.environ["DSH_HOME"] = self.machine
        os.environ["DSH_PROFILE"] = "desktop"
        dshplugins.set_platform_home(self.platform)
        dshplugins.invalidate()

    def tearDown(self):
        dshplugins.set_platform_home(self._saved_platform)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        dshplugins.invalidate()
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------------------------------------------------------- 造夹具

    def write_plugin(self, name, meta=None, patch="", presets=None, skills=None):
        """在项目 profile 里造一个插件包目录，返回包路径。

        `patch` 写进 `cordis.patch.yml`（宿主声明），`presets` 是
        `{预设目录名: preset.yml 正文}`，`skills` 是 `{文件名: 正文}`。
        """
        base = os.path.join(self.platform, "profiles", dshplugins.PLATFORM_PROFILE)
        path = os.path.join(base, "node_modules", *name.split("/"))
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "package.json"), "w", encoding="utf-8") as fh:
            json.dump(meta if meta is not None else {"name": name, "version": "1.0.0"},
                      fh)
        if patch:
            with open(os.path.join(path, "cordis.patch.yml"), "w",
                      encoding="utf-8") as fh:
                fh.write(patch)
        for folder, body in (presets or {}).items():
            target = os.path.join(path, "presets", folder)
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, "preset.yml"), "w", encoding="utf-8") as fh:
                fh.write(body)
        for filename, body in (skills or {}).items():
            target = os.path.join(path, "skills")
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, filename), "w", encoding="utf-8") as fh:
                fh.write(body)
        return path

    def write_profile(self, dependencies):
        base = os.path.join(self.platform, "profiles", dshplugins.PLATFORM_PROFILE)
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "package.json"), "w", encoding="utf-8") as fh:
            json.dump({"name": "dsh-profile-%s" % dshplugins.PLATFORM_PROFILE,
                       "private": True, "dependencies": dict(dependencies),
                       "dsh": {"profile": {"bundles": sorted(dependencies)}}}, fh)
        return base


# ---------------------------------------------------------------- 宿主声明

class HostMountsTestCase(_Sandbox):
    """`cordis.patch.yml` 里那一段 `insert:` 是插件"要做别的事情"的唯一静态痕迹。"""

    def test_an_insert_block_yields_its_mount_ids(self):
        path = self.write_plugin("@nanmicoder/dsh-agent-teams", patch="""\
# dsh-agent-teams bundle patch: mounts the agent-teams plugin into the host
# composition of a dsh profile. The plugin registers its `agent_teams_*` tools
# into the shared `tools` registry.
- insert:
    - id: agent-teams
      # Node-resolvable package name
      name: '@nanmicoder/dsh-agent-teams'
      config:
        stateDir: .agent-teams
        memberProvider: spawn
""")
        result = dshplugins.host_mounts(path)
        self.assertEqual(["agent-teams"], result["mounts"])
        self.assertTrue(result["file"].endswith("cordis.patch.yml"))

    def test_a_config_block_does_not_leak_into_the_mount_list(self):
        """`config:` 里既有 `key: value` 也有块状列表，两者都不是挂载。

        这一条的失败方式很难看：把 config 里任何一条读成挂载，界面上就会说
        "该插件挂载了 `recallEnabled`"——一个看起来像事实、其实毫无意义的 id。
        """
        path = self.write_plugin("graph-memory", patch="""\
- insert:
    - id: graph-memory
      name: 'graph-memory/dsh'
      config:
        recallEnabled: true
        recallMaxNodes: 6
        messageRetention:
          keep: all
          recentTurns: 0
        embedding:
          apiKeyEnv: !!js "process.env.X ? 'Y' : undefined"
        labels:
          - alpha
          - beta
""")
        self.assertEqual(["graph-memory"], dshplugins.host_mounts(path)["mounts"])

    def test_a_second_insert_block_is_read_too(self):
        path = self.write_plugin("multi", patch="""\
- insert:
    - id: first
- insert:
    - id: second
      name: 'multi/second'
""")
        self.assertEqual(["first", "second"], dshplugins.host_mounts(path)["mounts"])

    def test_a_sibling_list_item_ends_the_insert_scope(self):
        """与 `insert:` 同级的下一项之后，再深一层的 `- id:` 就不属于这次挂载了。"""
        path = self.write_plugin("stop", patch="""\
- insert:
    - id: inside
- something-else:
    - id: outside
""")
        self.assertEqual(["inside"], dshplugins.host_mounts(path)["mounts"])

    def test_a_package_without_a_patch_is_empty_not_an_error(self):
        path = self.write_plugin("plain")
        self.assertEqual({"file": "", "mounts": []}, dshplugins.host_mounts(path))


# ---------------------------------------------------------------- 判定

class ContributionTestCase(_Sandbox):
    """四类插件各自的结论。**每一句都要能被读的人直接拿来用**。"""

    def _plugin(self, name, **kwargs):
        self.write_plugin(name, **kwargs)
        self.write_profile({name: "1.0.0"})
        dshplugins.invalidate()
        snapshot = dshplugins.snapshot(refresh=True)
        return snapshot["plugins"][0]

    def test_a_plugin_with_a_preset_is_an_agent_source(self):
        plugin = self._plugin("with-preset", presets={"redteam": "name: 红队模式\n"},
                              skills={"manual.md": "name: manual\n渗透\n"})
        verdict = dshplugins.contribution(plugin)
        self.assertEqual("agent", verdict["kind"])
        self.assertIn("作用于对话", verdict["summary"])
        self.assertIn("1 个 Agent 预设", verdict["summary"])
        self.assertIn("1 份技能手册", verdict["summary"])

    def test_skills_only_is_a_skills_source(self):
        plugin = self._plugin("skills-only", skills={"a.md": "name: a\n渗透\n"})
        verdict = dshplugins.contribution(plugin)
        self.assertEqual("skills", verdict["kind"])
        self.assertIn("作用于对话", verdict["summary"])

    def test_a_host_only_plugin_says_why_it_does_nothing(self):
        """**这一条就是这一轮的根因**：装了、装成功了、对话里什么都没变。"""
        plugin = self._plugin("@nanmicoder/dsh-agent-teams", patch="""\
- insert:
    - id: agent-teams
      name: '@nanmicoder/dsh-agent-teams'
""")
        verdict = dshplugins.contribution(plugin)
        self.assertEqual("host", verdict["kind"])
        self.assertIn("不作用于对话", verdict["summary"])
        self.assertIn("「agent-teams」", verdict["detail"])
        self.assertIn("不会生效", verdict["detail"])
        # 那句把人带歪的话不许回来：重启 DSH 不会改变任何东西。
        self.assertNotIn("重启加载", verdict["detail"])

    def test_an_empty_plugin_says_there_is_nothing_to_read(self):
        plugin = self._plugin("nothing")
        verdict = dshplugins.contribution(plugin)
        self.assertEqual("empty", verdict["kind"])
        self.assertIn("没有可读内容", verdict["summary"])

    def test_a_client_side_part_is_flagged_even_when_the_plugin_contributes(self):
        """带客户端界面的插件，界面部分同样不生效——这一句要写在能起作用的那一类里。

        不写的话，使用者会拿着"预设能用了"去推断"那张图也应该能用了"，而后者
        只在 DSH 宿主里画得出来。
        """
        plugin = self._plugin("@howmp/dsh-pentest",
                              meta={"name": "@howmp/dsh-pentest", "version": "1.0.0",
                                    "dsh": {"client": {"platform": "web"}}},
                              presets={"pentest": "name: 渗透模式\n"})
        verdict = dshplugins.contribution(plugin)
        self.assertEqual("agent", verdict["kind"])
        self.assertIn("客户端界面", verdict["detail"])
        self.assertTrue(plugin["hasClient"])

    def test_scan_profile_exposes_the_mounts(self):
        plugin = self._plugin("mounted", patch="- insert:\n    - id: mounted\n")
        self.assertEqual(["mounted"], plugin["mounts"])
        self.assertTrue(plugin["bundled"])

    def test_the_verdict_text_is_plain(self):
        """判定行在界面上是 `textContent`，**原样**显示——所以理由里不许出现记号。

        第一版写的是 `AI 对话**不是** DSH 会话`，出图之后才看见星号被读出来；
        反引号那一批（`list_skills` / `cordis.patch.yml`）同样。与 `harness.plain`
        那条注释记的是同一个坑，而它是"截图里看出来的，不是想出来的"——所以这一条
        不是风格偏好，是显示层的约束。
        """
        cases = [
            {"name": "a", "mounts": [], "hasClient": False,
             "presets": [{}], "skills": [{}]},
            {"name": "b", "mounts": ["graph-memory"], "hasClient": False,
             "presets": [], "skills": []},
            {"name": "c", "mounts": ["agent-teams", "x"], "hasClient": True,
             "presets": [], "skills": []},
            {"name": "d", "mounts": [], "hasClient": False,
             "presets": [], "skills": []},
        ]
        for case in cases:
            verdict = dshplugins.contribution(case)
            for field in ("summary", "detail"):
                for mark in ("**", "`"):
                    self.assertNotIn(mark, verdict[field],
                                     "判定行会原样显示，%s 里带着 %r"
                                     % (field, mark))


# ---------------------------------------------------------------- 技能来源

class SkillSourcesTestCase(unittest.TestCase):
    """`integrated` 挂在预设与技能上，**不在插件那一层**。

    这一条以前从 `snap["plugins"]` 上读 `integrated`，而 `scan_profile` 从不产出那个
    键 —— 于是 `plugins` 恒为空。它的输出是模型回答"技能从哪儿来"的唯一依据，
    而当时的样子是 `pluginCount: 23` 配 `plugins: []`（自相矛盾，且没有人会去查）。
    """

    class _Plane(object):
        data_dir = ""

    def _snapshot(self):
        return {
            "plugins": [{"name": "dsh-purge"}, {"name": "graph-memory"}],
            "presets": [{"name": "红队模式", "plugin": "dsh-purge", "integrated": True},
                        {"name": "闲置预设", "plugin": "graph-memory",
                         "integrated": False}],
            "skills": [{"name": "recon", "plugin": "dsh-purge", "integrated": True,
                        "path": "x.md"},
                       {"name": "看图", "plugin": "@liustack/modlens",
                        "integrated": False, "path": "y.md"}],
        }

    def test_the_contributing_plugins_are_named(self):
        original = dshplugins.snapshot
        dshplugins.snapshot = lambda *a, **k: self._snapshot()
        try:
            data = chat.skill_sources(self._Plane())
        finally:
            dshplugins.snapshot = original
        self.assertIn("dsh-purge", data["plugins"])
        # 只挂载宿主、什么都没贡献的那个不许混进来：它是一句谎话的最小形态。
        self.assertNotIn("graph-memory", data["plugins"])
        self.assertNotIn("@liustack/modlens", data["plugins"])

    def test_each_plugin_is_named_once(self):
        original = dshplugins.snapshot
        snap = self._snapshot()
        snap["skills"].append({"name": "b", "plugin": "dsh-purge",
                               "integrated": True, "path": "z.md"})
        dshplugins.snapshot = lambda *a, **k: snap
        try:
            data = chat.skill_sources(self._Plane())
        finally:
            dshplugins.snapshot = original
        self.assertEqual(1, data["plugins"].count("dsh-purge"))


# ---------------------------------------------------------------- 能力对端

class LandingTestCase(unittest.TestCase):
    """挂载 id → 本平台能力。**显式表**，不猜。"""

    def test_a_mapped_capability_names_the_native_tools(self):
        rows = harness.landing_for(["agent-teams"])
        self.assertEqual(1, len(rows))
        self.assertEqual("team", rows[0]["capability"])
        self.assertEqual("智能体团队", rows[0]["label"])
        self.assertIn("team_create", rows[0]["tools"])

    def test_unmapped_mounts_have_no_landing(self):
        """没有对端就是没有。给一个"大概相当于"的答案比不说更糟。"""
        self.assertEqual([], harness.landing_for(["graph-memory", "turn-rewind"]))
        self.assertEqual([], harness.landing_for([]))

    def test_every_mapped_capability_exists(self):
        for row in harness.PLUGIN_MOUNTS:
            self.assertIn(row["capability"], harness.CAPABILITY_INDEX,
                          "PLUGIN_MOUNTS 指向了一个不存在的能力 id")
        ids = [row["mount"] for row in harness.PLUGIN_MOUNTS]
        self.assertEqual(len(ids), len(set(ids)), "同一个挂载 id 登记了两次")


# ---------------------------------------------------------------- 接口与界面

class DshPublicTestCase(_Sandbox):
    """接口要把判定与对端一起交出去：前端不算，免得两处各说一套。"""

    def setUp(self):
        super(DshPublicTestCase, self).setUp()
        self.dir, self.store = support.temporary_store("t66.db")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()
        super(DshPublicTestCase, self).tearDown()

    def test_the_api_exposes_the_verdict_and_the_landing(self):
        base = os.path.join(ROOT, "web")
        self.write_plugin("@nanmicoder/dsh-agent-teams",
                          patch="- insert:\n    - id: agent-teams\n")
        self.write_plugin("dsh-purge",
                          skills={"recon.md": "name: recon\n渗透\n"})
        self.write_profile({"dsh-purge": "1.0.0",
                            "@nanmicoder/dsh-agent-teams": "1.0.0"})
        plane = server.ControlPlane(self.store, base, auth_required=False)
        # **顺序要紧**：`ControlPlane.__init__` 会把平台 home 指到它自己的 data_dir
        # （真实部署里那就是对的），所以沙箱要在它之后再钉回来一次。不钉的话这里读的
        # 是另一个 home——夹具写的插件一个都看不见，而失败长得像"接口没透出判定"。
        dshplugins.set_platform_home(self.platform)
        dshplugins.invalidate()
        public = plane._dsh_public(dshplugins.snapshot(refresh=True))
        rows = {item["name"]: item for item in public["plugins"]}
        self.assertEqual("host", rows["@nanmicoder/dsh-agent-teams"]["contribution"]["kind"])
        self.assertEqual(["agent-teams"],
                         rows["@nanmicoder/dsh-agent-teams"]["mounts"])
        self.assertEqual("智能体团队",
                         rows["@nanmicoder/dsh-agent-teams"]["landing"][0]["label"])
        self.assertEqual("skills", rows["dsh-purge"]["contribution"]["kind"])
        self.assertEqual([], rows["dsh-purge"]["landing"])


class PluginMarketUiTestCase(unittest.TestCase):
    """界面这一侧：**装完必须说话，而且不许说那句假话**。"""

    def setUp(self):
        self.js = _strip_js_comments(_read("web/settings.js"))
        self.css = _read("web/style.css")
        self.guide = _strip_js_comments(_read("web/guide.js"))

    def test_the_false_lead_is_gone(self):
        """"或在 DSH 侧还没重启加载"是**假的**，而且方向明确地把人带歪。

        重启 DSH 不会让一个宿主插件在本平台的对话里生效 —— 它要的东西从来不在
        这条路上。断言前先剥注释（本仓库里这条规矩踩过七次）。
        """
        self.assertNotIn("重启加载", self.js)

    def test_the_reason_comes_from_the_backend(self):
        self.assertIn("item.contribution", self.js,
                      "判定要显示后端算好的那一条，前端不重算")
        self.assertIn("function noAgentReason", self.js)
        self.assertIn("noAgentReason(finished)", self.js)   # 真的被调用，不是死代码

    def test_the_summary_separates_the_three_kinds(self):
        for token in ("contribKind", "只在 DSH 宿主里生效", "没有可读内容"):
            self.assertIn(token, self.js)

    def test_the_verdict_line_has_its_own_row(self):
        self.assertIn("hub-verdict", self.js)
        self.assertIn(".hub-verdict", self.css)

    def test_the_guide_states_the_boundary(self):
        """手册要把边界说到 2026-10-20 之后的样子：对话**已经**是 DSH 运行时。

        旧文案写的是“本工作台的对话不是 DSH 会话、也不启动 DSH 宿主”，它当时是
        真的；对话改由项目内 DSH 执行之后这句话就反了——照它去判断，
        `dsh-agent-teams` 会被误判成“白装”。但也不能矫枉过正：挂在平台自己插件
        容器里的包（`graph-memory`）仍然不作用于对话，所以两句都要在。
        """
        self.assertIn("cordis.patch.yml", self.guide)
        self.assertIn("由项目内自带的 DSH 运行时执行", self.guide)
        self.assertIn("装进对话那个 profile", self.guide)
        self.assertIn("agent_teams_", self.guide)
        self.assertIn("不会改变对话行为", self.guide)
        # “对话不是 DSH 会话”已经被推翻，留着它就是把新的真话盖掉。
        self.assertNotIn("对话不是 DSH 会话", self.guide)


if __name__ == "__main__":
    unittest.main()
