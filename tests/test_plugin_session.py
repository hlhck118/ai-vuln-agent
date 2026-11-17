# -*- coding: utf-8 -*-
"""插件安装 → 应用会话：目录失效、Agent 归属、技能跟不跟得上。

这一组用例来自一个真实缺口：插件在插件市场里**装好了**，DSH 状态里也写着"已接入"，
但会话入口的 Agent 下拉框里没有它带来的预设——因为：

1. `skills.py` 有自己的 60 秒目录缓存，`hub.invalidate_snapshot()` 只清了
   `dshplugins` 的缓存，技能列表于是比 Agent 列表晚一步更新；
2. 技能缓存键当时用的是 `id(snapshot)`。快照被失效后下一次重建的是一个**同尺寸**
   的 dict，CPython 很可能把刚释放的那块内存再分配出去——地址复用，于是命中
   上一份目录，表现为"装好了，列表里还是旧的"，要等满 TTL 才自愈；
3. 界面上没有任何地方能回答"这个插件该用到哪个会话里"，用户只能自己去下拉框里
   按插件起的名字找。

前两条在这里被钉死；第三条由 `_dsh_public` 的 `agents` 字段保证——本文件只验证
后端把"插件 → Agent"的对应关系给全了，界面上那个按钮才可能选对。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agents, dshplugins, hub, skills          # noqa: E402
from pentestd.server import ControlPlane                      # noqa: E402
from pentestd.store import Store                              # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


def _skill(path, name, plugin, integrated=True):
    return {"path": path, "name": name, "plugin": plugin,
            "integrated": integrated, "reason": "测试夹具"}


def _snapshot(paths, plugin="dsh-purge"):
    """最小但**形状正确**的快照：只需要 `skills` 与 `presets` 两个键。"""
    return {"available": True, "home": "/tmp/home", "profile": "desktop",
            "profiles": ["desktop"], "errors": [],
            "plugins": [{"name": plugin, "version": "1.0.0", "bundled": True,
                         "missing": False, "presets": [], "skills": []}],
            "skills": [_skill(path, os.path.basename(path), plugin) for path in paths],
            "presets": []}


def _preset(name, preset_id, persona, integrated, order=0, plugin="dsh-purge"):
    """一条预设。`reason` 是必须有的：`dshplugins.snapshot()` 给每条都写了理由，
    `_dsh_public` 直接读它——夹具少了这个键，测的就不是真实形状了。"""
    return {"id": preset_id, "presetId": "%s:%s" % (plugin, preset_id), "name": name,
            "description": "", "persona": persona, "plugin": plugin,
            "pluginVersion": "1.1.71", "integrated": integrated, "order": order,
            "reason": "测试夹具：用户指定集成的插件" if integrated else "测试夹具：不相关"}


class CatalogInvalidationTestCase(unittest.TestCase):
    """安装成功后，凡是依赖插件快照的目录都要一起失效。"""

    def setUp(self):
        skills.invalidate()
        dshplugins.invalidate()
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name

    def tearDown(self):
        skills.invalidate()
        dshplugins.invalidate()
        self.tmp.cleanup()

    def test_invalidate_snapshot_clears_the_skill_catalog_too(self):
        """只清 dshplugins 的缓存是不够的——技能目录是**另一份**缓存。

        这条断言在修复前必失败：`hub.invalidate_snapshot()` 只调了
        `dshplugins.invalidate()`，`skills._CACHE` 原封不动，于是"插件装完，
        Agent 列表更新了、技能列表还是旧的"能稳定复现，且没人能一眼看出哪边是对的。
        """
        skills.catalog(_snapshot(["/p/a.md"]), self.data_dir)
        dshplugins.snapshot()                        # 让 dshplugins 的缓存也进一层
        self.assertTrue(skills._CACHE, "前提：技能目录已经进过缓存")
        hub.invalidate_snapshot()
        self.assertEqual(skills._CACHE, {},
                         "安装后技能目录缓存必须一起失效，否则技能列表会落后 60 秒")
        self.assertEqual(dshplugins._CACHE, {})

    def test_skill_cache_key_does_not_depend_on_object_identity(self):
        """缓存键按内容算，不按对象地址算。

        用 `id(snapshot)` 做键时，快照失效后重建的同尺寸 dict 很可能拿到**刚释放的
        那个地址**，于是这里命中上一份目录——一个只在特定分配顺序下出现、看起来像
        "随机失灵"的缺陷。内容变了键就该跟着变，与对象身份无关。
        """
        first = _snapshot(["/p/a.md"])
        second = _snapshot(["/p/a.md"])              # 内容相同，是另一个对象
        self.assertIsNot(first, second)
        self.assertEqual(skills._cache_key(first, self.data_dir),
                         skills._cache_key(second, self.data_dir),
                         "内容相同的两份快照必须算出同一个键")
        third = _snapshot(["/p/a.md", "/p/b.md"])
        self.assertNotEqual(skills._cache_key(first, self.data_dir),
                            skills._cache_key(third, self.data_dir),
                            "内容不同的快照必须算出不同的键")
        self.assertNotEqual(skills._cache_key(first, self.data_dir),
                            skills._cache_key(first, self.data_dir + "x"),
                            "不同数据目录不能共用一条缓存")

    def test_non_integrated_skills_do_not_change_the_key(self):
        """被判定为不相关的技能不进目录，也就不该改动缓存键。

        否则每多一个无关插件（`dsh-routing-suite` 这类）都会让目录缓存失效重算，
        而重算结果一模一样——白读 20 多份 markdown。
        """
        base = _snapshot(["/p/a.md"])
        noisy = _snapshot(["/p/a.md"])
        noisy["skills"].append(_skill("/p/z.md", "z.md", "dsh-routing-suite",
                                      integrated=False))
        self.assertEqual(skills._cache_key(base, self.data_dir),
                         skills._cache_key(noisy, self.data_dir))

    def test_invalidate_lets_a_new_local_skill_through(self):
        """用户往 `data/skills/` 丢一份手册，失效之后目录里就该有它。

        这条同时也锁住"缓存确实存在"这个前提：不失效直接再查一次是拿不到新文件的
        （60 秒 TTL 内命中缓存），所以如果哪天缓存被去掉，这个用例的前半段会失败，
        提醒改的人重新考虑"-失效"这层还该不该留。
        """
        skill_dir = os.path.join(self.data_dir, skills.LOCAL_DIR_NAME)
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, "alpha.md"), "w", encoding="utf-8") as handle:
            handle.write("# alpha\n")
        names = lambda: [item["name"] for item in
                         skills.catalog(None, self.data_dir)]
        self.assertIn("alpha", names())
        with open(os.path.join(skill_dir, "beta.md"), "w", encoding="utf-8") as handle:
            handle.write("# beta\n")
        self.assertNotIn("beta", names(), "前提：TTL 内应当命中缓存（否则这条用例没意义）")
        skills.invalidate()
        self.assertIn("beta", names(), "失效之后必须看得见新增的本地技能")


class PluginAgentContributionTestCase(unittest.TestCase):
    """`_dsh_public` 要说清"哪个插件带来了哪几个 Agent"。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        self.plane = ControlPlane(self.store, WEB_DIR, auth_required=False)
        skills.invalidate()

    def tearDown(self):
        skills.invalidate()
        self.store.close()
        self.tmp.cleanup()

    def test_each_plugin_reports_its_own_agent_ids(self):
        snapshot = _snapshot([], plugin="dsh-purge")
        snapshot["presets"] = [
            _preset("红队演练", "redteam", "以红队视角推进。", True, order=10),
            _preset("编码助手", "coding", "写业务代码。", False, order=0),
        ]
        public = self.plane._dsh_public(snapshot)
        row = public["plugins"][0]
        self.assertEqual(row["name"], "dsh-purge")
        self.assertEqual([item["id"] for item in row["agents"]], ["dsh-purge"],
                         "只应报告**被采纳**为 Agent 的预设")
        self.assertIn("dsh-purge", row["agents"][0]["label"])
        # 未采纳的那条必须仍然带着理由出现在 ignored 里，否则"装了插件却没多出
        # 那个 Agent"就无从解释
        self.assertEqual([item["name"] for item in public["ignored"]], ["编码助手"])
        self.assertTrue(public["ignored"][0]["reason"])

    def test_agent_ids_match_the_session_options_catalog(self):
        """界面按钮上的 id 必须就是会话入口那个下拉框认的 id。

        两处各算一遍 `_dsh_id`（它带去重后缀）迟早对不上，而对不上的表现是
        "点了『应用到会话』，会话却换成了另一个 Agent"——所以这里直接拿
        `session_options` 的目录来比对，而不是再算一遍期望值。
        """
        snapshot = _snapshot([], plugin="dsh-purge")
        snapshot["presets"] = [
            _preset("红队演练", "redteam", "以红队视角推进。", True, order=10),
        ]
        catalog_ids = {item["id"] for item in agents.catalog("", snapshot)}
        public_ids = {item["id"] for item in self.plane._dsh_public(snapshot)["plugins"][0]["agents"]}
        self.assertTrue(public_ids, "该预设应当贡献一个 Agent")
        self.assertTrue(public_ids <= catalog_ids,
                        "按钮给的 id 必须都在会话 Agent 目录里；差集：%s"
                        % (public_ids - catalog_ids))

    def test_plugin_without_presets_reports_no_agents(self):
        public = self.plane._dsh_public(_snapshot([], plugin="dsh-routing-suite"))
        self.assertEqual(public["plugins"][0]["agents"], [],
                         "不贡献预设的插件必须给出空列表，界面据此不画按钮")


class AgentDelegationNoticeTestCase(unittest.TestCase):
    """「派活」这件事要让**使用者**看得见，不能只写在提示词里。

    真实缺口：`agents.MISSING_TEMPLATE` 把"本平台的派活叫 `delegate`、一次最多 6 个
    兄弟子智能体、同时最多 3 个在飞、不走 DSH 那条原生路径"写进了系统提示，但那句话
    只约束**模型**。用户在会话入口的下拉框里选了插件预设「红队演练模式（dsh-purge）」
    ——它的 README 与 `agents/` 角色稿里写着"**必须用原生 `subagent` 工具派活**"
    ——而界面上原本一个字都不说。于是他只能猜助手这一次到底怎么派的活，
    或者更糟，信了助手自己编的那句"已派出 N 个子智能体并发推进中"。

    这一组钉住把它送到界面那条链路的两个判据，两处都是刻意的：

    * 只读**插件人设**（`persona`），不读拼好的 `prompt`。适配段由 `agents` 自己
      生成，`TOOL_MAP` 与 `MISSING_TEMPLATE` 里本来就写着 `subagent`（作为**要被
      改写成 `delegate` 的外来名**）——拿它当判据，每个插件预设都会命中，
      "这一档到哪为止"就成了一句对谁都说的话，等于没说。
    * 只能给**出现证据**，不能给**要求证据**。真实人设同一条句子里既有"必须用
      `subagent`"也有"不要改用 `spawn_teammate`"；字符串层面两者没有区别，
      逐词断言必然把被**禁止**的那个也说成被要求的。

    另外两条（`test_adapter_text_states_delegate...` 与 `..._gives_platform_wording`）
    是同一件事的另一个方向：本平台**现在有**派活能力，所以适配段必须正面写出
    `delegate` 的名字、硬上限与措辞。少写任何一项，模型就会照 DSH 的形态说话——
    那是这个适配层最容易失效的方式，也是这两条守卫真正拦的东西。
    """

    # 两段都照抄本机 `dshplugins.snapshot()` 读出来的**真实**人设原文，不是编的：
    # @howmp/dsh-pentest 用英文写 `subagents`（复数），dsh-purge 用中文写"派活"
    # 并显式禁掉 `spawn_teammate`。判据必须在两种写法上都成立。
    PURGE_PERSONA = (
        "你只做四件事：**① 计划智能体任务 ② 派活 ③ 分析子智能体回报 ④ 向用户汇报"
        "并决定下一个任务**。你自己**不动手**：不扫描、不爆破、不利用。"
        "派活前后的 `redteam_preflight` / `redteam_agent_slot` / `redteam_session_info`。"
        "**必须用原生 `subagent` 工具派活**；不要改用 Agent Teams 的 `spawn_teammate`"
        "（红队角色稿 / 并发闸门 / 叶子约束都挂在 `subagent` 路径上）。")
    PENTEST_PERSONA = (
        "Reconnaissance and execution are delegated to subagents, which submit "
        "confirmed results directly to their intent.")

    def test_persona_written_for_subagents_is_flagged(self):
        """两种真实写法都要命中：中文"派活"与英文 `subagents`（复数）。"""
        for name, persona in (("dsh-purge", self.PURGE_PERSONA),
                              ("@howmp/dsh-pentest", self.PENTEST_PERSONA)):
            result = agents.delegation({"persona": persona})
            self.assertTrue(result["asks"], "%s 的人设是为派活写的，必须命中" % name)
            self.assertIn("subagent", result["matched"])

    def test_builtin_and_custom_presets_are_never_flagged(self):
        """本平台自己写的口径不该让人期待 DSH 那套派活——误报会把这条提示变成噪音。"""
        for item in agents.builtin():
            self.assertFalse(agents.delegation(item)["asks"],
                             "内置档 %s 的人设不是为派活写的" % item["id"])
        custom = agents.custom_agents([{
            "id": "custom-lateral", "label": "内网横向专项", "summary": "", "note": "",
            "prompt": "先收集，再核对链路，然后决定下一步打哪个资产。",
            "steps": 8, "skills": []}])
        self.assertEqual(len(custom), 1)
        self.assertFalse(agents.delegation(custom[0])["asks"])

    def test_adapter_text_alone_does_not_flag(self):
        """插件预设的**适配段**里就写着 `subagent`——判据读了它，每个插件预设都命中。

        这条在修复前必失败，也是本组最有价值的一条：把 `delegation()` 改成读
        `prompt`（一个看起来很自然、甚至更"全"的改动），它会立刻红。
        """
        snapshot = _snapshot([], plugin="dsh-purge")
        snapshot["presets"] = [_preset("红队演练模式", "redteam", "", True)]
        items = agents.dsh_agents(snapshot)
        self.assertEqual(len(items), 1)
        self.assertIn("subagent", items[0]["prompt"],
                      "前提：适配段里确实写着 subagent——TOOL_MAP 与 MISSING_TEMPLATE "
                      "都把它作为**要被改写成 delegate 的外来名**保留着")
        self.assertEqual(items[0]["persona"], "")
        self.assertFalse(agents.delegation(items[0])["asks"],
                         "判据必须只看人设；读适配段等于对每个插件预设都说同样的话")

    def _delegation_section(self, prompt):
        """适配段里"派活"那一节：从 `### 派活` 到 `### 本平台没有的能力` 之前。

        断言**只在这一段里**做，不去全文搜关键词。全文里 `TOOL_MAP` 的表格本来就写着
        `delegate` 与那两个数字，全文搜等于让"整节被删掉"这种回归照样通过——
        我实测过：把 `MISSING_TEMPLATE` 里 `delegate` 全部替换掉，全文搜仍然全绿。
        这个切片就是那条实测逼出来的。

        两个标题都必须存在：`### 派活` 缺席意味着派活能力**又被写回"没有的能力"里**
        （那正是本轮要改掉的旧文案），所以这里先断言它存在，再切片。
        """
        for heading in ("### 派活", "### 本平台没有的能力"):
            self.assertIn(heading, prompt, "适配段缺少 %s 一节" % heading)
        return prompt[prompt.index("### 派活"):prompt.index("### 本平台没有的能力")]

    def test_adapter_text_states_delegate_with_its_hard_limits(self):
        """适配段必须正面写出 `delegate`：工具名、上限 6、并发 3、不递归、计入父轮。

        平台还没有派活能力时，这条守卫守的是"必须在提示词里说清本平台没有"；能力有了
        之后，守的东西变成**本平台怎么派活**必须在提示词里。少写任何一项，模型就会照
        DSH 的形态说话：承诺第 7 个角色、承诺"都在同时跑"、承诺一个下一轮才到的异步
        回报——三件本平台都做不到的事。
        """
        snapshot = _snapshot([], plugin="dsh-purge")
        snapshot["presets"] = [_preset("红队演练模式", "redteam", self.PURGE_PERSONA, True)]
        prompt = agents.dsh_agents(snapshot)[0]["prompt"]
        section = self._delegation_section(prompt)
        for needle, why in (
                ("delegate", "派活工具的名字"),
                ("最多 6 个", "一次最多 6 个兄弟子智能体"),
                ("同时最多 3 个在飞", "同时最多 3 个在飞"),
                ("children[].role", "角色写在哪里"),
                ("不能再派活", "子智能体不能再次派活（不递归）"),
                ("步数预算", "子智能体的工具调用计入父轮预算"),
                ("同步返回", "返回是同步的，不是等下一轮收回报")):
            self.assertIn(needle, section, "适配段的派活一节少写了：%s" % why)
        # 顺序也是判据：先"有"后"没有"。反过来写，第一印象还是"派活不可用"。
        self.assertIn("### 本平台没有的能力", prompt)
        self.assertLess(prompt.index("### 派活"), prompt.index("### 本平台没有的能力"),
                        "`delegate` 那一节必须排在「本平台没有的能力」之前")
        # 概览列表里也得有它，否则模型只看一张"插件工具 → 本平台对应"的表格，
        # 容易把它当成"边缘能力"而不是这一个会话里可以真的调用的东西
        self.assertIn("delegate", prompt[prompt.index("### 本平台的实际能力"):])

    def test_adapter_text_gives_platform_wording_and_buries_the_dsh_one(self):
        """措辞要给本平台的，DSH 的说法只允许出现在"不要"句里。

        模型复述"已派出 N 个子智能体并发推进中"是这类适配最典型的失效：那句话在 DSH
        里是对的，在这里描述了一件没发生的事。所以适配段既得给一句能照抄的话
        （"派出了 3 个子智能体，已全部返回"），也得把 DSH 那句钉在禁止的位置上——
        只写"要有"不写"不要有"，模型两种都照着说的概率并不低。
        """
        snapshot = _snapshot([], plugin="dsh-purge")
        snapshot["presets"] = [_preset("红队演练模式", "redteam", self.PURGE_PERSONA, True)]
        prompt = agents.dsh_agents(snapshot)[0]["prompt"]
        section = self._delegation_section(prompt)
        self.assertIn("已全部返回", section,
                      "必须给出本平台自己的措辞，否则模型只能自己编一句")
        self.assertIn("不要把 `delegate` 说成", section,
                      "必须明确禁止把 delegate 说成 DSH 的原生 subagent")
        at = section.index("并发推进中")
        bullet = section.rfind("\n*", 0, at)
        self.assertIn("不要", section[bullet:at],
                      "DSH 的措辞只能出现在'不要说…'里，不能是一句可以照抄的话")

    def test_only_evidence_is_reported_never_a_requirement(self):
        """字段形态只有 evidence：`matched` 是"人设里出现了这些词"。

        `spawn_teammate` 在真实人设里是被**明令禁止**的那个（"不要改用 Agent Teams
        的 `spawn_teammate`"）。它出现在 `matched` 里是对的——人设确实提到了它——
        但这说明这个字段绝不能改名叫 `required` 之类：那必然对禁用的工具也说谎。
        """
        result = agents.delegation({"persona": self.PURGE_PERSONA})
        self.assertEqual(sorted(result), ["asks", "matched"],
                         "只允许 evidence 两个键；任何 required/needed 之类的键都会说谎")
        self.assertIn("spawn_teammate", result["matched"])
        self.assertIn("redteam_agent_slot", result["matched"])

    def test_catalog_entry_carries_the_flag_without_the_persona(self):
        """端到端：界面拿到的目录条目必须带上判定，且**不能**带上人设正文。

        人设可能有五千字（本机 dsh-purge 是 5017 字），`public()` 刻意不下发它——
        判定因此必须在**服务端**算好，而不是把正文丢给浏览器让它自己找关键词。
        """
        snapshot = _snapshot([], plugin="dsh-purge")
        snapshot["presets"] = [_preset("红队演练模式", "redteam", self.PURGE_PERSONA, True)]
        entries = agents.catalog("", snapshot, [])
        entry = [item for item in entries if item["id"] == "dsh-purge"][0]
        self.assertTrue(entry["delegation"]["asks"])
        self.assertIn("subagent", entry["delegation"]["matched"])
        self.assertNotIn("persona", entry, "人设正文不下发浏览器")
        self.assertEqual(entry["personaChars"], len(self.PURGE_PERSONA),
                         "只给字数——它足以说明'这人设很长'，又说不出内容")

    def test_unknown_or_empty_agent_falls_back_to_no_notice(self):
        """拿不到条目时按"这份口径不是为派活写的"处理：方向保守，不凭空吓人。"""
        self.assertEqual(agents.delegation({}), {"asks": False, "matched": []})
        self.assertEqual(agents.delegation({"persona": None}), {"asks": False, "matched": []})


if __name__ == "__main__":
    unittest.main()
