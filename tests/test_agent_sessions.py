# -*- coding: utf-8 -*-
"""会话入口（Agent / 模型 / 权限档、技能手册、DSH 插件集成、插件市场）的契约。

本文件的重点不是"接口返回 200"，而是三件**做错了也不会有人发现**的事：

1. **权限档真的是闸门，不只是菜单**。把工具清单按档位过滤只是不给模型看；
   模型仍可能凭空发出一个工具名（它见过类似名字，或者被工具结果里的文字诱导），
   而工具结果按本平台的定义就是不可信输入。因此这里的主力用例是
   **绕过清单、直接调用 `execute_tool`**，断言它照样被拦。
2. **权限档不是解锁器**。它是收窄器，任何情况下都不能放宽探测作用域。
   最后一组用例把档位开到最高档，再用一个越界目标跑完整轮对话，
   断言任务没有落地——这是本模块存在的最重要的理由。
3. **插件贡献的 Agent 人设必须经过适配**。DSH 插件的人设里写满了本平台不存在的
   工具（`subagent`、`pentest_submit`、`redteam_*`）。原样塞进系统提示，模型会去
   调用不存在的工具，或者把"我已派了三个子智能体"当成事实说给用户听。
   这里钉住适配段**明确写出了哪些能力没有**。

DSH 相关的用例一律用**构造的快照**而不是本机真实安装：真实安装的内容会随用户
装了什么而变，把它写进断言等于让测试随环境漂移。真实发现另有一条用例，
但它只断言"能跑通、形状对"，不断言具体插件列表。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import (agent_modes, agents, chat, dshplugins, hub, policy,  # noqa: E402
                      scope, server, skills)
from pentestd.store import SCHEMA_VERSION                            # noqa: E402
from tests import support                                  # noqa: E402
from tests.test_chat import (ScriptedModel, has_tool_result, text_reply,  # noqa: E402
                             tool_reply)

WEB_DIR = os.path.join(ROOT, "web")


# ---------------------------------------------------------------- 构造快照

def fake_snapshot(integrated=True, persona="你是渗透测试指挥官。"):
    """一份形状与 `dshplugins.snapshot()` 一致的假快照。

    只放测试需要判断的字段。刻意**不**做成"真实快照的副本"：
    副本会随着上游插件改版而需要跟着改，最后没人改，测试就变成了摆设。
    """
    return {
        "available": True,
        "home": "/fake/dsh",
        "profile": "unit",
        "profileDir": "/fake/dsh/profiles/unit",
        "profiles": ["unit"],
        "plugins": [
            {"name": "@howmp/dsh-pentest", "version": "9.9.9", "bundled": True,
             "missing": False, "presets": 1, "skills": 0},
            {"name": "dsh-purge", "version": "1.2.3", "bundled": True,
             "missing": False, "presets": 1, "skills": 2},
        ],
        "presets": [
            {"id": "pentest", "name": "渗透模式", "description": "插件自带的渗透口径",
             "order": 0, "persona": persona, "path": "/fake/pentest",
             "composition": "/fake/pentest/agent.cordis.yml",
             "plugin": "@howmp/dsh-pentest", "pluginVersion": "9.9.9",
             "presetId": "@howmp/dsh-pentest:pentest",
             "integrated": integrated, "reason": "用户指定集成的插件"},
            {"id": "coding", "name": "编码模式", "description": "与本平台无关",
             "order": 0, "persona": "You are a coding agent.", "path": "/fake/coding",
             "composition": "", "plugin": "dsh-routing-suite", "pluginVersion": "0.1.2",
             "presetId": "dsh-routing-suite:coding",
             "integrated": False, "reason": "未发现安全作业相关内容"},
        ],
        "skills": [
            {"name": "recon-pipeline", "path": "/fake/skills/recon-pipeline.md",
             "plugin": "dsh-purge", "integrated": True,
             "reason": "用户指定集成的插件"},
            {"name": "modlens", "path": "/fake/skills/modlens.md",
             "plugin": "@liustack/modlens", "integrated": False,
             "reason": "未发现安全作业相关内容"},
        ],
        "errors": [],
        "scannedAt": 0.0,
    }


# ---------------------------------------------------------------- 权限档目录

class PolicyCatalogTestCase(unittest.TestCase):
    """纯数据约束：档位形状、包含关系、默认方向。"""

    def test_expected_levels_exist(self):
        ids = {item["id"] for item in policy.POLICIES}
        self.assertEqual(ids, {"readonly", "probe", "audit", "full"})

    def test_exactly_one_default_and_it_is_the_widest(self):
        defaults = [item for item in policy.POLICIES if item["default"]]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0]["id"], policy.DEFAULT_POLICY)
        widest = max(policy.POLICIES, key=lambda item: item["level"])
        self.assertEqual(defaults[0]["id"], widest["id"])

    def test_levels_are_strictly_monotonic(self):
        """高档位必须**包含**低档位的全部工具。

        允许"能审计但不能探测"这类正交组合会把四个档位变成十六个，而使用者要回答的
        问题始终只有一个——"这个会话能动到什么程度"。
        """
        order = ["readonly", "probe", "audit", "full"]
        previous = set()
        for name in order:
            current = set(policy.allowed_tools(name, "admin"))
            self.assertTrue(previous.issubset(current),
                            "%s 没有包含上一档的工具：%s" % (name, previous - current))
            previous = current

    def test_unknown_tool_is_denied_at_every_level(self):
        """未在清单里分级的工具，**所有档位**都拿不到。

        这是刻意的默认方向：将来加一个新工具却忘了分级，症状是"模型看不见它"
        （可见、可查、可修），而不是"它在只读档里悄悄可用了"（事后才发现）。
        """
        for item in policy.POLICIES:
            self.assertFalse(policy.permits(item["id"], "run_shell", "admin"))
            self.assertFalse(policy.permits(item["id"], "", "admin"))

    def test_unknown_level_falls_back_but_write_path_rejects(self):
        """读路径回退、写路径拒绝——两侧行为必须不同。"""
        self.assertEqual(policy.resolve("nope")["id"], policy.DEFAULT_POLICY)
        self.assertEqual(policy.resolve("")["id"], policy.DEFAULT_POLICY)
        self.assertIsNone(policy.get("nope"))

    def test_deny_reason_tells_you_how_to_fix_it(self):
        """"不行"必须能直接转述给用户：是档位不够还是角色不够。"""
        reason = policy.deny_reason("readonly", "start_scan", "operator")
        self.assertIn("权限档", reason)
        self.assertIn("审计", reason)          # 指出需要调到哪一档
        admin_only = policy.deny_reason("full", "install_plugin", "operator")
        self.assertIn("管理员", admin_only)
        unclassified = policy.deny_reason("full", "run_shell", "admin")
        self.assertIn("未在本平台的权限清单里分级", unclassified)

    def test_admin_tools_need_both_the_level_and_the_role(self):
        self.assertIn("install_plugin", policy.allowed_tools("full", "admin"))
        self.assertNotIn("install_plugin", policy.allowed_tools("full", "operator"))

    def test_no_level_promises_to_widen_the_scope(self):
        """任何档位的说明里都不得出现"放宽门禁"这类措辞。"""
        banned = ("放宽作用域", "绕过门禁", "解除限制", "无视作用域", "不受作用域限制")
        for item in policy.POLICIES:
            blob = item["summary"] + item["note"] + policy.prompt_block(item["id"])
            for phrase in banned:
                self.assertNotIn(phrase, blob, "%s 出现了 %s" % (item["id"], phrase))

    def test_prompt_block_says_it_is_a_narrowing_control(self):
        block = policy.prompt_block("readonly", "operator")
        self.assertIn("收窄", block)
        self.assertIn("权限档", block)
        self.assertIn("start_scan", block)      # 超出本档的动作要点名


class ToolLevelGuardTestCase(unittest.TestCase):
    """加工具却忘了分级，必须在**导入时**就炸，而不是变成安静的越权。"""

    def test_every_handler_is_classified_and_vice_versa(self):
        self.assertEqual(sorted(set(chat.HANDLERS)), sorted(set(policy.TOOL_LEVEL)))

    def test_guard_actually_fires(self):
        original = dict(policy.TOOL_LEVEL)
        try:
            policy.TOOL_LEVEL.pop("start_scan")
            with self.assertRaises(RuntimeError) as caught:
                chat._tool_level_guard()
            self.assertIn("start_scan", str(caught.exception))
        finally:
            policy.TOOL_LEVEL.clear()
            policy.TOOL_LEVEL.update(original)


# ---------------------------------------------------------------- 权限真的拦

class PolicyEnforcementTestCase(unittest.TestCase):
    """核心安全用例：绕过工具清单，直接调用 `execute_tool`。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("policy.db")
        # 作用域收到回环，越界目标会被门禁拒绝——下一组用例要用这一点。
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False,
                                         probe_scope=["127.0.0.0/8"])

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_readonly_cannot_start_a_scan_even_when_called_directly(self):
        """模型发出一个它"看不见"的工具名时，仍然必须被拦。

        过滤工具清单只影响模型**看得见**什么；发出一个名字不需要许可。
        这条用例模拟的正是"模型凭空/被诱导发出工具调用"的场面。
        """
        outcome = chat.execute_tool(self.plane, "start_scan",
                                    {"target": "/srv/app"}, "",
                                    "readonly", "admin", True)
        self.assertFalse(outcome["ok"])
        self.assertIn("权限拒绝", outcome["summary"])
        self.assertTrue(outcome["data"]["denied"])
        self.assertEqual(outcome["data"]["required"], "audit")
        self.assertEqual(self.store.jobs(), [], "被拒的调用不得产生任务")

    def test_readonly_can_still_read(self):
        outcome = chat.execute_tool(self.plane, "list_executors", {}, "",
                                    "readonly", "admin", True)
        self.assertTrue(outcome["ok"], outcome["summary"])

    def test_probe_level_allows_probe_but_not_audit(self):
        allowed = chat.execute_tool(self.plane, "start_probe",
                                    {"target": "127.0.0.1", "ports": "1-2"}, "",
                                    "probe", "admin", True)
        self.assertTrue(allowed["ok"], allowed["summary"])
        denied = chat.execute_tool(self.plane, "start_scan",
                                   {"target": "/srv/app"}, "",
                                   "probe", "admin", True)
        self.assertFalse(denied["ok"])
        self.assertIn("权限拒绝", denied["summary"])

    def test_operator_cannot_install_plugins_even_at_full_level(self):
        outcome = chat.execute_tool(self.plane, "install_plugin",
                                    {"target": "github:owner/repo"}, "",
                                    "full", "operator", True)
        self.assertFalse(outcome["ok"])
        self.assertIn("管理员", outcome["summary"])

    def test_tool_catalog_hides_what_the_level_cannot_use(self):
        names = {tool["name"] for tool in chat.tool_catalog("readonly", "operator")}
        self.assertIn("get_findings", names)
        self.assertNotIn("start_scan", names)
        self.assertNotIn("start_probe", names)
        # 每一档的目录都是全集的子集，且随档位单调增长
        probe = {tool["name"] for tool in chat.tool_catalog("probe", "operator")}
        self.assertTrue(names.issubset(probe))
        self.assertIn("start_probe", probe)


class ScopeIsNotWidenedTestCase(unittest.TestCase):
    """端到端：把档位开到**最高**，越界目标依然被拒。

    这是整个权限模块存在的意义所在。如果把档位当成解锁开关，这条用例会失败，
    而失败的方式是"任务被创建了"——那种缺陷在人工点几下界面时几乎不可能被发现。
    """

    def setUp(self):
        self.dir, self.store = support.temporary_store("scope-lock.db")

        def responder(record):
            if has_tool_result(record):
                return text_reply("openai_compat", "已尝试下发。")
            # 8.8.8.8 在 127.0.0.0/8 之外
            return tool_reply("openai_compat", "start_probe",
                              {"target": "8.8.8.8", "ports": "80"}, "call-1")

        self.model = ScriptedModel(responder)
        self.httpd, self.client = support.start(
            self.store, WEB_DIR, probe_scope=["127.0.0.0/8"])
        status, _payload = self.client.post("/api/v1/models", {
            "name": "假模型", "provider": "openai_compat",
            "baseUrl": self.model.base_url + "/v1", "model": "fake-1",
            "apiKeySource": "none", "temperature": 0.2, "maxTokens": 512,
            "timeoutS": 20})
        self.assertEqual(status, 201)

    def tearDown(self):
        self.model.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.dir.cleanup()

    def test_highest_level_still_obeys_the_scope_gate(self):
        status, payload = self.client.post("/api/v1/chat", {
            "message": "探测一下 8.8.8.8 的 80 端口",
            "policyId": "full",
        })
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["config"]["policyId"], "full")
        trace = payload["trace"]
        self.assertEqual(trace[0]["tool"], "start_probe")
        self.assertFalse(trace[0]["ok"], "越界目标竟然通过了")
        self.assertIn("作用域", trace[0]["summary"])
        self.assertEqual(self.store.jobs(), [], "越界目标不得产生任务")

    def test_readonly_refuses_before_the_scope_check_ever_runs(self):
        """档位不足时不该看得到门禁的报错。

        顺序很重要：先判权限、再判作用域。反过来的话，只读会话会从错误信息里
        读到"这个目标在作用域外"——那等于把门禁的判定结果泄漏给了不该发指令的会话。
        """
        status, payload = self.client.post("/api/v1/chat", {
            "message": "探测一下 127.0.0.1 的 80 端口",
            "policyId": "readonly",
        })
        self.assertEqual(status, 200, payload)
        trace = payload["trace"]
        self.assertEqual(trace[0]["tool"], "start_probe")
        self.assertFalse(trace[0]["ok"])
        self.assertIn("权限拒绝", trace[0]["summary"])
        self.assertEqual(self.store.jobs(), [])


# ---------------------------------------------------------------- Agent 目录

class AgentCatalogTestCase(unittest.TestCase):
    def test_builtin_agents_mirror_agent_modes(self):
        """内置 Agent 必须**就是** `agent_modes.MODES`，不是它的副本。

        两处各存一份会让"设置页的模式列表"与"对话实际用的模式"分岔，
        而这正是 `agent_modes` 存在的理由。
        """
        builtin = agents.builtin()
        self.assertEqual([item["id"] for item in builtin],
                         [mode["id"] for mode in agent_modes.MODES])
        for item, mode in zip(builtin, agent_modes.MODES):
            self.assertEqual(item["prompt"], mode["prompt"])
            self.assertEqual(item["steps"], mode["steps"])
            self.assertEqual(item["source"], "builtin")

    def test_dsh_presets_become_agents_with_a_unique_id(self):
        catalog = agents.all_agents(fake_snapshot())
        ids = [item["id"] for item in catalog]
        self.assertEqual(len(ids), len(set(ids)), "Agent id 重复：%s" % ids)
        self.assertIn("pentest", ids)
        self.assertIn("dsh-pentest", ids)
        # 未集成的预设不得进入目录
        self.assertNotIn("dsh-routing-suite", ids)
        self.assertEqual(len(catalog), len(agents.builtin()) + 1)

    def test_plugin_agent_label_shows_its_provenance(self):
        catalog = {item["id"]: item for item in agents.all_agents(fake_snapshot())}
        agent = catalog["dsh-pentest"]
        self.assertIn("@howmp/dsh-pentest", agent["label"])
        self.assertEqual(agent["source"], "@howmp/dsh-pentest")
        self.assertIn("DSH 插件", agent["sourceLabel"])

    def test_adapter_maps_foreign_tools_and_never_claims_they_exist(self):
        """适配段是这个模块存在的理由，逐条钉住它说了什么。"""
        catalog = {item["id"]: item for item in agents.all_agents(fake_snapshot())}
        prompt = catalog["dsh-pentest"]["prompt"]
        # 映射表要给出本平台的对应工具
        for here in ("get_chain_overview", "get_assets", "get_report",
                     "load_skill", "start_probe"):
            self.assertIn(here, prompt, "适配段没把 %s 列为本平台对应能力" % here)
        # 插件专有工具名要作为"外来名"出现，供模型识别并改写
        for foreign in ("pentest_submit", "pentest_state", "redteam_agent_slot",
                        "subagent"):
            self.assertIn(foreign, prompt)
        # 本平台没有的能力必须**明说没有**，并给出等价做法
        self.assertIn("本平台没有的能力", prompt)
        self.assertIn("不要假装有", prompt)
        self.assertIn("单会话工具循环", prompt)
        self.assertIn("不要让用户以为命令已经跑过", prompt)

    def test_plugin_persona_is_carried_into_the_prompt(self):
        catalog = {item["id"]: item for item in agents.all_agents(fake_snapshot())}
        self.assertIn("你是渗透测试指挥官", catalog["dsh-pentest"]["prompt"])

    def test_prompt_block_appends_the_skill_catalog(self):
        catalog = {item["id"]: item for item in agents.all_agents(fake_snapshot())}
        block = agents.prompt_block(catalog["dsh-pentest"], ["recon-pipeline"])
        self.assertIn("list_skills", block)
        self.assertIn("recon-pipeline", block)

    def test_unknown_agent_falls_back_but_get_returns_none(self):
        """读路径回退（插件被卸载后会话仍要能打开），写路径拒绝。"""
        self.assertIsNone(agents.get("not-a-real-agent", fake_snapshot()))
        fallback = agents.resolve("not-a-real-agent", fake_snapshot())
        self.assertTrue(fallback["default"])
        self.assertEqual(fallback["id"], agent_modes.DEFAULT_MODE)

    def test_adapt_accepts_both_shapes_without_losing_provenance(self):
        mode = agent_modes.get_mode("redteam")
        adapted = agents.adapt(mode)
        self.assertEqual(adapted["id"], "redteam")
        self.assertEqual(adapted["source"], "builtin")
        # 已经是 Agent 形态的原样返回：再包一层会把插件来源抹成 builtin
        plugin_agent = agents.dsh_agents(fake_snapshot())[0]
        self.assertIs(agents.adapt(plugin_agent), plugin_agent)
        self.assertEqual(agents.adapt(None)["id"], agent_modes.DEFAULT_MODE)

    def test_public_shape_hides_the_prompt(self):
        agent = agents.all_agents(fake_snapshot())[-1]
        payload = agents.public(agent, agent["id"])
        self.assertNotIn("prompt", payload)
        self.assertNotIn("persona", payload)
        self.assertGreater(payload["personaChars"], 0)
        self.assertTrue(payload["active"])

    def test_real_machine_discovery_has_the_right_shape(self):
        """对着本机真实 DSH 跑一次发现：只验形状，不断言具体装了哪些插件。"""
        snapshot = dshplugins.snapshot()
        self.assertIn("available", snapshot)
        self.assertIsInstance(snapshot["presets"], list)
        self.assertIsInstance(snapshot["skills"], list)
        for item in snapshot["presets"]:
            self.assertIn("integrated", item)
            self.assertIn("reason", item)
        for item in agents.all_agents(snapshot):
            self.assertTrue(item["id"])
            self.assertTrue(item["prompt"])


# ---------------------------------------------------------------- 技能手册

class SkillCatalogTestCase(unittest.TestCase):
    def setUp(self):
        self.dir, self.store = support.temporary_store("skills.db")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_front_matter_is_parsed_and_body_split(self):
        meta, body = skills.parse("---\nname: x\ndescription: \"有 空格\"\n"
                                 "role: recon\n---\n\n正文\n")
        self.assertEqual(meta["name"], "x")
        self.assertEqual(meta["description"], "有 空格")
        self.assertEqual(meta["role"], "recon")
        self.assertTrue(body.startswith("正文"))

    def test_file_without_front_matter_is_still_a_skill(self):
        meta, body = skills.parse("# 标题\n\n正文")
        self.assertEqual(meta, {})
        self.assertEqual(body, "# 标题\n\n正文")

    def test_only_integrated_plugin_skills_are_offered(self):
        items = skills.catalog(fake_snapshot(), self.dir.name)
        names = {item["name"] for item in items}
        self.assertEqual(names, {"recon-pipeline"})
        self.assertNotIn("modlens", names)

    def test_local_skill_overrides_a_plugin_skill_of_the_same_name(self):
        """本地同名手册要覆盖插件版本，且被覆盖的那一条要留痕。"""
        base = skills.local_dir(self.dir.name)
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "recon-pipeline.md"), "w",
                  encoding="utf-8") as handle:
            handle.write("---\nname: recon-pipeline\ndescription: 本地版本\n---\n我的流程\n")
        items = skills.catalog(fake_snapshot(), self.dir.name)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "本地")
        self.assertEqual(items[0]["description"], "本地版本")
        self.assertEqual(items[0]["shadowed"], ["recon-pipeline"])
        self.assertIn("我的流程", skills.body_of(items[0]))

    def test_find_is_case_and_whitespace_insensitive(self):
        items = skills.catalog(fake_snapshot(), self.dir.name)
        for probe in ("recon-pipeline", "RECON-PIPELINE", "  recon-pipeline  "):
            self.assertIsNotNone(skills.find(items, probe), probe)
        self.assertIsNone(skills.find(items, "no-such-skill"))
        self.assertIsNone(skills.find(items, ""))

    def test_search_requires_all_terms(self):
        items = skills.catalog(fake_snapshot(), self.dir.name)
        self.assertEqual(len(skills.search(items, "recon", "recon")), 0)  # 假快照无 role
        self.assertEqual(len(skills.search(items, "")), 1)
        self.assertEqual(len(skills.search(items, "recon 不存在的词")), 0)

    def test_local_skills_are_discovered_without_any_plugin(self):
        snapshot = dict(fake_snapshot(), available=False, presets=[], skills=[])
        base = skills.local_dir(self.dir.name)
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "my-playbook.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: my-playbook\nrole: custom\n---\n步骤\n")
        items = skills.catalog(snapshot, self.dir.name)
        self.assertEqual([item["name"] for item in items], ["my-playbook"])
        self.assertEqual(skills.roles(items), [{"id": "custom", "count": 1}])

    def test_real_machine_skills_keep_the_body_cap(self):
        for item in skills.catalog(dshplugins.snapshot(), self.dir.name):
            self.assertLessEqual(item["chars"], skills.MAX_BODY_CHARS + 40)
            self.assertTrue(item["name"])


# ---------------------------------------------------------------- 会话级配置

class SessionConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.dir, self.store = support.temporary_store("session.db")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_new_session_stores_empty_meaning_follow_the_default(self):
        meta = self.store.create_chat_session("t", "u1")
        self.assertEqual(meta["agentId"], "")
        self.assertEqual(meta["policyId"], "")
        self.assertEqual(meta["modelId"], "")

    def test_setting_one_field_leaves_the_others_alone(self):
        meta = self.store.create_chat_session("t", "u1")
        self.store.set_chat_session_config(meta["id"], policy_id="readonly")
        after = self.store.set_chat_session_config(meta["id"], model_id="m-1")
        self.assertEqual(after["policyId"], "readonly")
        self.assertEqual(after["modelId"], "m-1")
        self.assertEqual(after["agentId"], "")

    def test_empty_string_resets_to_following_the_default(self):
        """`None` 与 `""` 必须分开：前者是"这次请求没提它"，后者是"用户选了默认"。"""
        meta = self.store.create_chat_session("t", "u1", policy_id="readonly")
        self.assertEqual(meta["policyId"], "readonly")
        reset = self.store.set_chat_session_config(meta["id"], policy_id="")
        self.assertEqual(reset["policyId"], "")
        untouched = self.store.set_chat_session_config(meta["id"], policy_id="readonly")
        self.assertEqual(untouched["policyId"], "readonly")
        still = self.store.set_chat_session_config(meta["id"], model_id="m")
        self.assertEqual(still["policyId"], "readonly")

    def test_unknown_session_is_not_silently_created(self):
        self.assertIsNone(self.store.set_chat_session_config("C-nope", policy_id="full"))

    def test_migration_does_not_pin_old_sessions_to_the_widest_level(self):
        """v6 时代建的会话被迁移后，三项必须都是空串（跟随默认）。

        若默认值写成 `full`，这些会话会被钉死在最高权限档上，
        而它们的使用者从未做过这个选择。
        """
        import sqlite3
        path = os.path.join(self.dir.name, "legacy.db")
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE chat_sessions (id TEXT PRIMARY KEY, "
                    "title TEXT DEFAULT '', user_id TEXT DEFAULT '', "
                    "engagement_id TEXT DEFAULT '', created_at REAL, updated_at REAL)")
        raw.execute("INSERT INTO chat_sessions(id,title) VALUES('C-old','旧会话')")
        raw.execute("PRAGMA user_version=6")
        raw.commit()
        raw.close()
        legacy = server.Store(path)
        try:
            meta = legacy.get_chat_session("C-old")
            self.assertEqual(meta["agentId"], "")
            self.assertEqual(meta["policyId"], "")
            self.assertEqual(meta["modelId"], "")
            self.assertEqual(
                int(legacy.conn.execute("PRAGMA user_version").fetchone()[0]),
                SCHEMA_VERSION)
        finally:
            legacy.close()


# ---------------------------------------------------------------- 插件市场

class HubSafetyTestCase(unittest.TestCase):
    """安装目标串是不可信输入，而且这道闸是**硬性**的。

    目标会经 `dsh.cmd` 转交 `cmd.exe` 执行，`cmd.exe` 解释 `&`、`|`、`%`、`^`
    这些字符——即使我们用列表参数、`shell=False` 也一样，因为 .cmd 的解析发生在
    cmd.exe 里。因此 `validate_target` 不是防御性编程，它是唯一挡住注入的地方。
    """

    def test_cmd_metacharacters_are_rejected(self):
        for bad in ("foo&calc", "foo|calc", "foo>out", "foo<in", "foo^bar",
                    "foo%PATH%", "foo;bar", "foo`bar", "foo$(id)", "foo bar",
                    "foo'bar", 'foo"bar', "foo(bar)", "foo!bar"):
            ok, reason = hub.validate_target(bad)
            self.assertFalse(ok, "竟然放行了 %r" % bad)
            self.assertTrue(reason)

    def test_path_traversal_and_option_injection_are_rejected(self):
        for bad in ("../etc/passwd", "a/../b", "-x", "--save", ""):
            ok, _reason = hub.validate_target(bad)
            self.assertFalse(ok, "竟然放行了 %r" % bad)

    def test_real_install_targets_are_accepted(self):
        for good in ("dsh-plugin", "@howmp/dsh-pentest", "npm:foo@1.2.3",
                     "github:howmp/dsh-pentest#abc123", "dsh-purge@1.1.71"):
            ok, reason = hub.validate_target(good)
            self.assertTrue(ok, "%s 被误拒：%s" % (good, reason))

    def test_parse_keeps_the_target_out_of_a_pasted_command(self):
        from pentestd import hubcatalog
        self.assertEqual(
            hubcatalog.parse_install_target(
                "dsh plugin --profile web add github:ebony-vinyl/dsh-our-free-model"),
            "github:ebony-vinyl/dsh-our-free-model")
        self.assertEqual(hubcatalog.parse_install_target("@howmp/dsh-pentest"),
                         "@howmp/dsh-pentest")
        self.assertEqual(hubcatalog.parse_install_target(""), "")


class PluginQueueTestCase(unittest.TestCase):
    """队列行为。**所有用例都替换掉真正的安装函数**——测试绝不能改本机的 DSH。

    "没有隔离 home 时会拒绝执行"这条不变量**不在这里测**，虽然它看起来属于队列。
    原因：这里把 `hub.install` / `hub.remove` 整个换成了假的，而那道守卫就在真函数
    里面——替换掉它之后，测到的只是"假函数被调用了"。真正的覆盖在
    `tests/test_plugin_isolation.py::QueueRefusalTestCase`，那里只在最深处
    （`run_cli`）打桩。
    """

    def setUp(self):
        self.dir, self.store = support.temporary_store("plugin.db")
        self.calls = []
        self._saved_install = hub.install
        self._saved_remove = hub.remove
        # 装插件只允许落在平台自己的 home 里，所以执行器必须拿到一个 home——
        # 这里给的是临时目录，与"测试绝不改本机 DSH"是同一条规矩。
        self.iso_home = os.path.join(self.dir.name, "dsh-home")

        def fake_install(data_dir, target, profile="", proxy="", timeout=0,
                         dsh_home=""):
            self.calls.append(("install", target, profile, dsh_home))
            return {"ok": True, "action": "install", "target": target,
                    "exitCode": 0, "output": "模拟输出", "reason": ""}

        def fake_remove(data_dir, name, profile="", proxy="", timeout=0,
                        dsh_home=""):
            self.calls.append(("remove", name, profile, dsh_home))
            return {"ok": False, "action": "remove", "target": name,
                    "exitCode": 1, "output": "", "reason": "模拟失败"}

        hub.install = fake_install
        hub.remove = fake_remove
        self.runner = hub.OpRunner(self.store, self.dir.name,
                                   dsh_home_provider=lambda: self.iso_home)

    def tearDown(self):
        hub.install = self._saved_install
        hub.remove = self._saved_remove
        self.store.close()
        self.dir.cleanup()

    def _drain(self, timeout=5.0):
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            ops = self.store.plugin_ops(5)
            if ops and all(item["status"] != "running" for item in ops):
                return ops
            time.sleep(0.05)
        return self.store.plugin_ops(5)

    def test_bad_target_is_rejected_without_recording_anything(self):
        """被拒的目标不得进队列，也不得留下一条"曾经想装"的记录。"""
        outcome = self.runner.submit("install", "foo&calc", "unit", "u1")
        self.assertFalse(outcome["ok"])
        self.assertIsNone(outcome["op"])
        self.assertEqual(self.store.plugin_ops(10), [])
        self.assertEqual(self.calls, [])

    def test_valid_target_is_queued_and_recorded_before_it_runs(self):
        """先落库再执行：安装跑到一半进程被杀时，必须还看得出"这里启动过一次"。"""
        outcome = self.runner.submit("install", "github:howmp/dsh-pentest", "unit", "u1")
        self.assertTrue(outcome["ok"], outcome["reason"])
        self.assertEqual(outcome["op"]["status"], "running")
        ops = self._drain()
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["status"], "done")
        self.assertEqual(ops[0]["exitCode"], 0)
        self.assertEqual(ops[0]["userId"], "u1")
        # 第四个元素是隔离 home：**装到哪儿**是这条链路最要紧的一格，
        # 而它在界面上看不见，只能靠这里钉住。
        self.assertEqual(self.calls,
                         [("install", "github:howmp/dsh-pentest", "unit",
                           self.iso_home)])

    def test_failure_is_recorded_with_its_reason_and_output(self):
        self.runner.submit("remove", "dsh-something", "unit", "u1")
        ops = self._drain()
        self.assertEqual(ops[0]["status"], "failed")
        self.assertEqual(ops[0]["exitCode"], 1)
        self.assertIn("模拟失败", ops[0]["reason"])

    def test_internal_errors_do_not_kill_the_worker(self):
        """执行器内部异常必须兜住：否则 worker 静默死掉，队列从此不再被消费。"""
        hub.install = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        self.runner.submit("install", "some-plugin", "unit", "u1")
        ops = self._drain()
        self.assertEqual(ops[0]["status"], "failed")
        self.assertIn("boom", ops[0]["reason"])
        hub.install = lambda data_dir, target, profile="", proxy="", timeout=0, \
            dsh_home="": {"ok": True, "action": "install", "target": target,
                          "exitCode": 0, "output": "ok", "reason": ""}
        self.runner.submit("install", "another-plugin", "unit", "u1")
        ops = self._drain()
        self.assertEqual(ops[0]["status"], "done", "worker 没有活过来")

    def test_catalog_search_shape(self):
        """检索走真实目录缓存；不联网时也必须给出结构完整的空结果。"""
        page = hub.search(self.dir.name, "pentest", limit=5,
                          snapshot={"available": False, "profileDir": ""})
        self.assertIn("items", page)
        self.assertIn("total", page)
        self.assertIn("count", page)
        self.assertIn("hasMore", page)
        if page.get("ok"):
            for item in page["items"]:
                self.assertIn("installTarget", item)
                self.assertIn("repositoryUrl", item)
                self.assertTrue(hub.validate_target(item["installTarget"])[0]
                                or item["installTarget"] == "")

    def test_diagnostics_always_answers_with_a_shape(self):
        data = hub.diagnostics(self.dir.name, {"available": False, "profileDir": ""})
        self.assertIn("checks", data)
        self.assertIn("proxy", data)
        self.assertIn("summary", data)
        for check in data["checks"]:
            self.assertIn("key", check)
            self.assertIn("ok", check)


# ---------------------------------------------------------------- 入口接口

class SessionEntryApiTestCase(unittest.TestCase):
    """会话入口的 HTTP 契约。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("entry.db")
        self.httpd, self.client = support.start(self.store, WEB_DIR)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.dir.cleanup()

    def test_session_options_returns_all_three_choices_at_once(self):
        status, payload = self.client.get("/api/v1/session-options")
        self.assertEqual(status, 200)
        for key in ("agents", "models", "policies", "defaults", "skills", "dsh",
                    "draft"):
            self.assertIn(key, payload)
        self.assertIn("agentId", payload["defaults"])
        self.assertIn("policyId", payload["defaults"])
        # 三份列表必须都带 active 标记，否则界面无法回填"当前生效值"
        self.assertEqual(sum(1 for item in payload["agents"] if item["active"]), 1)
        self.assertEqual(sum(1 for item in payload["policies"] if item["active"]), 1)

    def test_last_choice_is_what_a_new_session_starts_from(self):
        """在会话里选过的三项，要成为**下一条**新会话的起点。

        测的是接口契约（`/session-options` 的 `draft`）而不是界面：界面拿它当
        预选值，预选值又会原样随第一条消息发出去，所以它必须就是"上次用过的那个
        组合"，不能是另算一套的默认值。
        """
        sid = self._session()["id"]
        status, payload = self.client.post(
            "/api/v1/chat/sessions/%s/config" % sid,
            {"agentId": "redteam", "policyId": "readonly"})
        self.assertEqual(status, 200, payload)
        status, options = self.client.get("/api/v1/session-options")
        self.assertEqual(status, 200)
        draft = options["draft"]
        self.assertEqual(draft["agentId"], "redteam")
        self.assertTrue(draft["agentPinned"], "选过的 Agent 要预选在上面，而不是跟随默认")
        self.assertEqual(draft["policyId"], "readonly")
        self.assertTrue(draft["policyPinned"])
        # 没选过模型就仍是"跟随全局启用项"：不能把默认模型写成一个显式选择，
        # 否则全局启用项以后改了，这个预选会把它顶掉。
        self.assertEqual(draft["modelId"], "")

    def test_last_choice_is_per_user(self):
        """「上次选择」是按人存的：下一个人登录后不该带着上一个人的预选。"""
        self.client.post("/api/v1/users", {"username": "operator-z",
                                            "password": "Operator-Pass9!",
                                            "role": "operator"})
        owner = self._user_id_of("operator-z")
        sid = self._session(owner)["id"]
        peer = support.Client(self.client.base)
        peer.login("operator-z", "Operator-Pass9!")
        status, payload = peer.post("/api/v1/chat/sessions/%s/config" % sid,
                                    {"policyId": "readonly"})
        self.assertEqual(status, 200, payload)
        _status, mine = peer.get("/api/v1/session-options")
        self.assertTrue(mine["draft"]["policyPinned"])
        _status, other = self.client.get("/api/v1/session-options")
        self.assertFalse(other["draft"]["policyPinned"],
                         "别人的选择不能出现在我的新建会话里")

    def test_last_choice_drops_entries_that_no_longer_exist(self):
        """上次选的预设/权限档没了，那一项要退回「跟随默认」。

        不核的话，新建会话会预选到一个发出去才报错的组合——界面看着正常，
        按下发送才收到 404。
        """
        admin = self._user_id_of("admin")
        self.store.set_chat_entry_choice(admin, "gone-agent", None, "gone-policy")
        status, options = self.client.get("/api/v1/session-options")
        self.assertEqual(status, 200)
        draft = options["draft"]
        self.assertFalse(draft["agentPinned"])
        self.assertFalse(draft["policyPinned"])
        self.assertEqual(draft["agentId"], options["defaults"]["agentId"])
        # 失效项要顺手从记录里抹掉，否则插件装回来时它会"自己冒出来"
        self.assertEqual(self.store.chat_entry_choice(admin), {})

    def test_entry_requires_login(self):
        anon = support.Client(self.client.base)
        for path in ("/api/v1/session-options", "/api/v1/agents", "/api/v1/policies",
                     "/api/v1/skills", "/api/v1/dsh", "/api/v1/hub/catalog"):
            status, _payload = anon.get(path)
            self.assertEqual(status, 401, path)

    def test_policies_endpoint_explains_it_is_a_narrowing_control(self):
        status, payload = self.client.get("/api/v1/policies")
        self.assertEqual(status, 200)
        self.assertIn("不改变", payload["note"])
        self.assertIn("门禁", payload["note"])

    def test_agents_endpoint_reports_why_a_plugin_was_not_adopted(self):
        status, payload = self.client.get("/api/v1/agents")
        self.assertEqual(status, 200)
        self.assertIn("dsh", payload)
        for item in payload["dsh"]["ignored"]:
            self.assertTrue(item["reason"], "未采纳的插件必须给出理由")

    def _session(self, user_id: str = ""):
        """直接建一条会话记录。

        刻意不走 `POST /chat`：那条路径要求先配好模型（对话靠模型决策工具调用），
        而这一组用例测的是**会话配置接口**，配不配模型与它无关。用 store 直接建，
        被测对象就只剩一个。
        """
        return self.store.create_chat_session("入口用例", user_id)

    def _user_id_of(self, username: str) -> str:
        _status, payload = self.client.get("/api/v1/users")
        for item in payload["users"]:
            if item["username"] == username:
                return item["id"]
        raise AssertionError("找不到用户 %s" % username)

    def test_session_config_write_rejects_unknown_ids(self):
        sid = self._session()["id"]
        status, payload = self.client.post(
            "/api/v1/chat/sessions/%s/config" % sid, {"agentId": "nope"})
        self.assertEqual(status, 404)
        status, payload = self.client.post(
            "/api/v1/chat/sessions/%s/config" % sid, {"policyId": "nope"})
        self.assertEqual(status, 404)
        status, payload = self.client.post(
            "/api/v1/chat/sessions/%s/config" % sid, {"modelId": "nope"})
        self.assertEqual(status, 404)
        # 三次都被拒之后，会话上不该留下任何一项
        meta = self.store.get_chat_session(sid)
        self.assertEqual((meta["agentId"], meta["policyId"], meta["modelId"]),
                         ("", "", ""))

    def test_operator_may_narrow_their_own_session(self):
        """收窄不需要管理员：它不会放宽任何东西。

        需要管理员的是**放宽**那一侧，而本平台没有"放宽门禁"这个动作。
        """
        status, _payload = self.client.post("/api/v1/users", {
            "username": "operator-a", "password": "Operator-Pass1!", "role": "operator"})
        self.assertEqual(status, 201)
        owner = self._user_id_of("operator-a")
        peer = support.Client(self.client.base)
        peer.login("operator-a", "Operator-Pass1!")
        sid = self._session(owner)["id"]
        status, payload = peer.post("/api/v1/chat/sessions/%s/config" % sid,
                                    {"policyId": "readonly"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["config"]["policyId"], "readonly")
        # 收窄之后该会话实际可用的工具确实变少了（服务端算的，不是界面算的）
        _status, detail = peer.get("/api/v1/chat/sessions/%s" % sid)
        self.assertEqual(detail["config"]["policyId"], "readonly")
        self.assertTrue(detail["config"]["policyPinned"])

    def test_operator_cannot_touch_another_users_session_config(self):
        self.client.post("/api/v1/users", {
            "username": "operator-d", "password": "Operator-Pass4!",
            "role": "operator"})
        foreign = self._session(self._user_id_of("operator-d"))["id"]
        status, payload = self.client.post(
            "/api/v1/chat/sessions/%s/config" % foreign, {"policyId": "readonly"})
        # 管理员可读全部会话，因此这里放行；改由**另一个操作员**来试才该被拒
        self.assertEqual(status, 200, payload)
        self.client.post("/api/v1/users", {
            "username": "operator-e", "password": "Operator-Pass5!",
            "role": "operator"})
        peer = support.Client(self.client.base)
        peer.login("operator-e", "Operator-Pass5!")
        status, payload = peer.post(
            "/api/v1/chat/sessions/%s/config" % foreign, {"policyId": "full"})
        self.assertEqual(status, 403)
        self.assertIn("其他用户", payload["error"])

    def test_operator_cannot_change_the_global_defaults(self):
        self.client.post("/api/v1/users", {
            "username": "operator-b", "password": "Operator-Pass2!",
            "role": "operator"})
        peer = support.Client(self.client.base)
        peer.login("operator-b", "Operator-Pass2!")
        status, payload = peer.post("/api/v1/policies/activate", {"id": "readonly"})
        self.assertEqual(status, 403)
        status, payload = peer.post("/api/v1/agents/activate", {"id": "redteam"})
        self.assertEqual(status, 403)

    def test_operator_cannot_install_plugins(self):
        self.client.post("/api/v1/users", {
            "username": "operator-c", "password": "Operator-Pass3!",
            "role": "operator"})
        peer = support.Client(self.client.base)
        peer.login("operator-c", "Operator-Pass3!")
        status, payload = peer.post("/api/v1/hub/install",
                                    {"target": "github:owner/repo"})
        self.assertEqual(status, 403)
        self.assertEqual(self.store.plugin_ops(10), [])

    def test_plugin_install_route_validates_before_queueing(self):
        status, payload = self.client.post("/api/v1/hub/install",
                                           {"target": "foo&calc"})
        self.assertEqual(status, 400)
        self.assertIn("不允许的字符", payload["error"])
        self.assertEqual(self.store.plugin_ops(10), [])

    def test_skill_detail_returns_the_body_and_a_data_notice(self):
        skills_list = skills.catalog(dshplugins.snapshot(), "")
        if not skills_list:
            self.skipTest("本机没有可用的技能手册")
        name = skills_list[0]["name"]
        status, payload = self.client.get("/api/v1/skills/%s" % name)
        self.assertEqual(status, 200)
        self.assertTrue(payload["skill"]["body"])
        status, payload = self.client.get("/api/v1/skills/definitely-not-a-skill")
        self.assertEqual(status, 404)

    def test_chat_tools_reflects_the_default_policy(self):
        """默认档位调窄之后，`/chat/tools` 给出的能力目录必须跟着变窄。

        界面上的「可用能力」如果永远显示全集，使用者就无从知道"这一档到哪为止"——
        而他会照着那份全集去要求模型做事，然后收获一串权限拒绝。
        """
        status, payload = self.client.get("/api/v1/chat/tools")
        self.assertEqual(status, 200)
        levels = {tool["name"]: tool["level"] for tool in payload["tools"]}
        self.assertEqual(levels.get("start_scan"), "audit")
        self.assertIn("权限档", payload["systemPrompt"])
        full_count = len(payload["tools"])
        self.assertEqual(full_count, payload["allToolCount"])
        self.assertEqual({tool["name"] for tool in payload["tools"]},
                         set(chat.HANDLERS), "管理员在 full 档应当看得到全部工具")

        status, _payload = self.client.post("/api/v1/policies/activate",
                                            {"id": "readonly"})
        self.assertEqual(status, 200)
        status, narrowed = self.client.get("/api/v1/chat/tools")
        names = {tool["name"] for tool in narrowed["tools"]}
        self.assertLess(len(names), full_count)
        self.assertNotIn("start_scan", names)
        self.assertNotIn("start_probe", names)
        self.assertIn("get_findings", names)
        self.assertEqual(narrowed["allToolCount"], full_count)
        # 系统提示里也要写明本档看不到什么，否则模型会去试它拿不到的动作
        self.assertIn("start_scan", narrowed["systemPrompt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
