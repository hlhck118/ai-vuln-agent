# -*- coding: utf-8 -*-
"""团队插件到底有没有接进**这条对话**：装、没装、装了没生效。

## 这组用例存在的理由

对话改由项目内的 DSH 运行时执行之后，「在 DSH 里装个插件」第一次真的能影响
对话。于是出现两种都会骗人的状态：

* **装了但没进 bundles**：`dsh plugin` 的安装残留，磁盘上有包、模型看不到工具；
* **装在平台自己的插件容器里**：那边的宿主是真的有，这条对话是真的没有。

两者在界面上长得一模一样（"插件已安装"），区别只在模型手里有没有
`agent_teams_*`。所以这里的判据全部钉在 `active`（装 + 进 bundles）上，并且
要求**文案把"为什么没有"说出来**——理由说不出来，用户就只能靠重启 DSH 试运气。

所有 manifest 都造在 `tempfile` 里；不读使用者机器上的 DSH。
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agents, chat, dsh_bridge, dsh_runtime, dshplugins  # noqa: E402
from tests import support                                           # noqa: E402

TEAMS = dshplugins.TEAMS_PLUGIN
PATCH = """\
# 该插件的宿主挂载
- insert:
    - id: agent-teams
      name: '@nanmicoder/dsh-agent-teams'
      config:
        stateDir: .agent-teams
        memberProvider: spawn
"""


def make_plugin(profile_path, name, version="0.1.25", patch=PATCH, extra=None):
    """在 profile 的 node_modules 里造一个插件包。"""
    folder = os.path.join(profile_path, "node_modules", *name.split("/"))
    os.makedirs(folder, exist_ok=True)
    meta = {"name": name, "version": version, "description": "团队"}
    meta.update(extra or {})
    with open(os.path.join(folder, "package.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle)
    if patch is not None:
        with open(os.path.join(folder, "cordis.patch.yml"), "w", encoding="utf-8") as f:
            f.write(patch)
    return folder


def make_profile(home, profile="v2sdk", *, dep=True, bundled=True, patch=PATCH):
    """造一个 profile：`package.json` 的依赖 + bundles + 磁盘上的包。"""
    path = dshplugins.profile_dir(home, profile)
    os.makedirs(path, exist_ok=True)
    manifest = {"name": profile, "dependencies": {}, "dsh": {"profile": {"bundles": []}}}
    if dep:
        manifest["dependencies"][TEAMS] = "^0.1.25"
        make_plugin(path, TEAMS, patch=patch)
    if bundled:
        manifest["dsh"]["profile"]["bundles"] = ["@deepseek-ai/dsh-base", TEAMS]
    with open(os.path.join(path, "package.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    return path


class ConversationProfileTestCase(unittest.TestCase):
    """对话用哪个 profile、那个 profile 里装了什么。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = self.dir.name

    def tearDown(self):
        self.dir.cleanup()

    def test_a_bundled_team_plugin_counts_as_active(self):
        make_profile(self.home)
        state = dshplugins.conversation_teams(self.home, "v2sdk")
        self.assertTrue(state["active"])
        self.assertEqual(state["version"], "0.1.25")
        self.assertEqual(state["mounts"], ["agent-teams"])
        self.assertEqual(state["stateDir"], dshplugins.TEAMS_STATE_DIR)

    def test_installed_but_not_bundled_is_not_active(self):
        """`dsh plugin add` 失败在中途会留下包——让模型调不存在的工具比没装更糟。"""
        make_profile(self.home, bundled=False)
        state = dshplugins.conversation_teams(self.home, "v2sdk")
        self.assertTrue(state["installed"])
        self.assertFalse(state["bundled"])
        self.assertFalse(state["active"])

    def test_a_missing_profile_is_empty_not_a_fault(self):
        """对话还没有自己的 profile（全新安装）不是错误，只是"还空着"。"""
        data = dshplugins.conversation_plugins(self.home, "v2sdk")
        self.assertFalse(data["available"])
        self.assertEqual(data["plugins"], [])
        state = dshplugins.conversation_teams(self.home, "v2sdk")
        self.assertFalse(state["installed"])
        self.assertFalse(state["active"])

    def test_a_dependency_without_a_directory_is_not_installed(self):
        path = make_profile(self.home, dep=False)
        manifest = {"name": "v2sdk", "dependencies": {TEAMS: "^0.1.25"},
                    "dsh": {"profile": {"bundles": [TEAMS]}}}
        with open(os.path.join(path, "package.json"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        data = dshplugins.conversation_plugins(self.home, "v2sdk")
        self.assertEqual([item["missing"] for item in data["plugins"]], [True])
        self.assertFalse(dshplugins.conversation_teams(self.home, "v2sdk")["active"])

    def test_the_conversation_state_reports_the_plugin_by_name(self):
        make_profile(self.home)
        state = dsh_runtime._conversation_state(self.home, "v2sdk")
        self.assertTrue(state["available"])
        self.assertEqual(state["plugins"], [TEAMS])
        self.assertTrue(state["agentTeams"]["active"])
        self.assertEqual(state["mounts"], ["agent-teams"])


class VerdictTestCase(unittest.TestCase):
    """界面上的那句话：生效就说生效，不生效必须说清为什么。"""

    def conversation(self, active=True, profile="v2sdk"):
        return {"profile": profile, "mounts": ["agent-teams"], "active": active,
                "dir": "x"}

    def plugin(self):
        return {"name": TEAMS, "mounts": ["agent-teams"], "presets": [], "skills": []}

    def test_a_live_mount_is_reported_as_reaching_the_conversation(self):
        outcome = dshplugins.contribution(self.plugin(), self.conversation(True))
        self.assertEqual(outcome["kind"], "runtime")
        self.assertIn("已随对话 profile 加载", outcome["summary"])
        self.assertIn("agent_teams_", outcome["detail"])

    def test_a_mount_on_another_profile_says_so_and_gives_the_command(self):
        outcome = dshplugins.contribution(self.plugin(), self.conversation(False))
        self.assertEqual(outcome["kind"], "host")
        self.assertIn("不作用于对话", outcome["summary"])
        self.assertIn("dsh plugin --profile v2sdk add", outcome["detail"])

    def test_an_active_profile_that_does_not_mount_this_row_is_still_host(self):
        """`active` 只看团队插件；别的包挂在别处时不能跟着一起说"生效"。"""
        conversation = self.conversation(True)
        conversation["mounts"] = ["some-other-row"]
        outcome = dshplugins.contribution(self.plugin(), conversation)
        self.assertEqual(outcome["kind"], "host")


class PatchRowTestCase(unittest.TestCase):
    """状态目录落点：写在补丁里，而不是让插件按 cwd 落到项目根。"""

    def text(self, **kwargs):
        return dsh_bridge.patch_text(
            sys.executable, os.path.join(ROOT, "tools", "dsh_bridge.py"),
            os.path.join(ROOT, "data", "dsh-home"), ROOT, "full",
            session_id="s1", console_url="http://127.0.0.1:8787", **kwargs)

    def test_the_row_names_the_state_dir_and_the_member_provider(self):
        text = self.text(teams_state_dir=dshplugins.TEAMS_STATE_DIR)
        self.assertIn("- id: agent-teams", text)
        self.assertIn("stateDir: '%s'" % dshplugins.TEAMS_STATE_DIR, text)
        # 补丁按行**整块**替换 config，所以两个键都得写：只写 stateDir 会把
        # `memberProvider` 抹掉，成员就换了一套提供者（真机上是静默行为变化）。
        self.assertIn("memberProvider: spawn", text)

    def test_without_the_plugin_there_is_no_row(self):
        text = self.text(teams_state_dir="")
        self.assertNotIn("agent-teams", text)

    def test_the_row_only_exists_when_the_conversation_really_loads_it(self):
        directory, store = support.temporary_store("teams.db")
        try:
            home = os.path.join(directory.name, "home")
            make_profile(home, bundled=False)          # 装了，没进 bundles
            store.set_setting(dsh_runtime.SETTING_PROFILE, "v2sdk")
            outcome = dsh_bridge.render(store, home, ROOT, session_id="s1")
            self.assertFalse(outcome.get("teams"))
            with open(dsh_bridge.runtime_patch_path(home, "s1"), encoding="utf-8") as f:
                self.assertNotIn("agent-teams", f.read())

            make_profile(home, bundled=True)           # 真加载
            outcome = dsh_bridge.render(store, home, ROOT, session_id="s1")
            self.assertTrue(outcome.get("teams"))
            with open(dsh_bridge.runtime_patch_path(home, "s1"), encoding="utf-8") as f:
                self.assertIn("stateDir: '%s'" % dshplugins.TEAMS_STATE_DIR, f.read())
        finally:
            store.close()
            directory.cleanup()


class SeedTestCase(unittest.TestCase):
    """送进会话的第一条消息：能力说明必须与本轮实际有什么对齐。"""

    def test_the_teams_note_is_appended_only_when_the_plugin_is_live(self):
        plain = chat.conversation_seed("口径", "看一眼", False)
        full = chat.conversation_seed("口径", "看一眼", True)
        self.assertNotIn("AgentTeams", plain)
        self.assertIn(agents.DSH_TEAMS_NOTE.strip(), full)
        # 顺序与"不覆盖使用者的话"这两件事都要成立：最后一段永远是用户输入原文。
        self.assertTrue(full.endswith("看一眼"))

    def test_the_note_tells_the_model_the_tools_and_the_approval_rule(self):
        text = agents.DSH_TEAMS_NOTE
        for name in ("agent_teams_create", "agent_teams_approve",
                     "agent_teams_send_message", "agent_teams_update_task"):
            self.assertIn(name, text)
        self.assertIn("不要自己替使用者批准", text)
        self.assertIn(dshplugins.TEAMS_STATE_DIR, text)
        # 平台自己的同名能力在 SDK 轮里不存在 —— 不说这句，模型会去调 team_*。
        self.assertIn("team_*", text)


class InstallerTestCase(unittest.TestCase):
    """`tools/install_conversation_plugin.py`：装进对话 profile 的那条路。

    这里只验**判据与命令拼装**（三处都对得上才算可用、跨路径搬运要先清依赖），
    真正的 `dsh plugin` 子进程不在单测里跑——它要联网拿包，而且会把结果写进一份
    真实的 home。安装路径本身由端到端探针验（`--check` 在真机 home 上返回 0）。
    """

    def setUp(self):
        import importlib.util
        path = os.path.join(ROOT, "tools", "install_conversation_plugin.py")
        spec = importlib.util.spec_from_file_location("install_conversation_plugin", path)
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        self.tmp = tempfile.mkdtemp(prefix="conv-install-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _profile(self, name="v2sdk", deps=None, bundles=None):
        folder = os.path.join(self.tmp, "home", "profiles", name)
        os.makedirs(folder, exist_ok=True)
        manifest = {"dependencies": deps or {},
                    "dsh": {"profile": {"bundles": bundles or []}}}
        with open(os.path.join(folder, "package.json"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        return folder

    def test_a_specs_version_is_not_part_of_the_package_name(self):
        self.assertEqual(self.tool.package_name("@nanmicoder/dsh-agent-teams@0.1.25"),
                         "@nanmicoder/dsh-agent-teams")
        self.assertEqual(self.tool.package_name(TEAMS), TEAMS)

    def test_usable_needs_all_three_places_to_agree(self):
        """依赖、bundles、补丁文件——少一处就是“装上了但没生效”，最不难查的那种。"""
        folder = self._profile(deps={TEAMS: "^0.1.25"}, bundles=[TEAMS])
        info = self.tool.status(os.path.join(self.tmp, "home"), "v2sdk", TEAMS)
        self.assertTrue(info["declared"] and info["bundled"])
        self.assertFalse(info["installed"], "没有包目录时不能算装好")
        self.assertFalse(self.tool.usable(info))
        package = os.path.join(folder, "node_modules", *TEAMS.split("/"))
        os.makedirs(package)
        with open(os.path.join(package, "cordis.patch.yml"), "w", encoding="utf-8") as handle:
            handle.write(PATCH)
        info = self.tool.status(os.path.join(self.tmp, "home"), "v2sdk", TEAMS)
        self.assertTrue(self.tool.usable(info))

    def test_a_profile_moved_from_another_path_is_reported_not_installed_over(self):
        """搬过来的 profile：pnpm 记着旧绝对路径，继续装会失败 —— 要先说清楚。"""
        folder = self._profile()
        os.makedirs(os.path.join(folder, "node_modules"))
        stale = os.path.join("D:" + os.sep, "elsewhere", "node_modules", ".pnpm")
        with open(os.path.join(folder, "node_modules", ".modules.yaml"),
                  "w", encoding="utf-8") as handle:
            handle.write('{\n  "virtualStoreDir": %s\n}\n' % json.dumps(stale))
        self.assertTrue(self.tool.moved_profile(folder))
        expected = os.path.join(folder, "node_modules", ".pnpm")
        with open(os.path.join(folder, "node_modules", ".modules.yaml"),
                  "w", encoding="utf-8") as handle:
            handle.write('{\n  "virtualStoreDir": %s\n}\n' % json.dumps(expected))
        self.assertEqual(self.tool.moved_profile(folder), "")

    def test_a_store_is_only_reused_when_it_still_exists(self):
        folder = self._profile()
        os.makedirs(os.path.join(folder, "node_modules"), exist_ok=True)
        self.assertEqual(self.tool.recorded_store(folder), "")
        with open(os.path.join(folder, "node_modules", ".modules.yaml"),
                  "w", encoding="utf-8") as handle:
            handle.write('{\n  "storeDir": %s\n}\n' % json.dumps(os.path.join(self.tmp, "gone")))
        self.assertEqual(self.tool.recorded_store(folder), "",
                         "记着的 store 已经不在了：不能拿它去装，交给 pnpm 自己选")


if __name__ == "__main__":
    unittest.main()
