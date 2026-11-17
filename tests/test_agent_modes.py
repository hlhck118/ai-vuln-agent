# -*- coding: utf-8 -*-
"""Agent 预设：目录形状、持久化、HTTP 契约，以及"切了模式真的变了什么"。

本文件刻意不去测"切换接口返回 200"这种自证式断言。模式的意义在于它**改变了
下发给模型的东西**，因此这里的主力用例是：

* 两种模式的提示词确实不同，且各自都写着作用域门禁；
* `/chat/tools` 返回的系统提示就是实际下发的那一份（不是未注入的静态常量）；
* 单轮步数预算真的跟着模式走；
* 红队模式**不是**解锁开关——越界目标在两种模式下一样被拒。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agent_modes, chat, scope               # noqa: E402
from pentestd.store import Store                            # noqa: E402
from tests import support                                   # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


# ---------------------------------------------------------------- 目录形状

class ModeCatalogTestCase(unittest.TestCase):
    """纯数据约束，不依赖服务。"""

    def test_expected_modes_exist(self):
        ids = {mode["id"] for mode in agent_modes.MODES}
        self.assertIn("pentest", ids)
        self.assertIn("redteam", ids)

    def test_every_mode_is_complete(self):
        for mode in agent_modes.MODES:
            for field in ("id", "label", "summary", "note", "prompt"):
                self.assertTrue(str(mode.get(field) or "").strip(),
                                "%s 的 %s 不能为空" % (mode.get("id"), field))
            self.assertGreaterEqual(mode["steps"], 1, "%s 的步数预算必须为正"
                                    % mode["id"])

    def test_exactly_one_default(self):
        self.assertEqual(sum(1 for m in agent_modes.MODES if m["default"]), 1)
        self.assertEqual(agent_modes.default_mode()["id"], agent_modes.DEFAULT_MODE)

    def test_prompts_are_genuinely_different(self):
        """两种模式的提示词必须真的不同，否则"切模式"只是换了个名字。"""
        prompts = [mode["prompt"] for mode in agent_modes.MODES]
        self.assertEqual(len(set(prompts)), len(prompts))

    def test_redteam_prompt_says_the_gate_is_unchanged(self):
        """红队模式必须**明写**门禁不变。

        它是那个看起来"更强"的档位，最容易被读成解锁开关——而它改的只是取向。
        渗透模式是基准取向，不需要自我声明合规，因此这里只钉红队这一侧。
        """
        redteam = agent_modes.get_mode("redteam")
        self.assertIn("作用域", redteam["prompt"])
        self.assertIn("不改变门禁", redteam["prompt"])

    def test_no_mode_promises_to_bypass_the_gate(self):
        """任何模式都不得声称能绕过门禁。

        这条防的是将来有人加一个"解除限制"档——那会让作用域门禁变成摆设，
        而门禁是这个平台唯一不能谈判的部分。
        """
        banned = ("绕过作用域", "无视作用域", "忽略作用域", "不受作用域限制",
                  "绕过门禁", "解除限制", "越界也可以", "无需授权")
        for mode in agent_modes.MODES:
            for phrase in banned:
                self.assertNotIn(phrase, mode["prompt"],
                                 "%s 的提示词出现了 %s" % (mode["id"], phrase))

    def test_resolve_never_raises(self):
        """读路径遇到被改坏的配置值必须回退，而不是让对话整个不可用。"""
        for value in ("", "   ", "nope", None):
            self.assertEqual(agent_modes.resolve(value)["id"],
                             agent_modes.DEFAULT_MODE)

    def test_get_mode_reports_unknown(self):
        """写路径相反：未知 id 要能被识别出来，静默纠正会让用户以为切换成功了。"""
        self.assertIsNone(agent_modes.get_mode("nope"))
        self.assertIsNone(agent_modes.get_mode(""))

    def test_public_payload_hides_the_prompt(self):
        """列表接口不该把系统提示整段回传到浏览器。"""
        for entry in agent_modes.catalog("pentest"):
            self.assertNotIn("prompt", entry)
            self.assertIn("steps", entry)

    def test_catalog_marks_the_active_one(self):
        entries = {entry["id"]: entry for entry in agent_modes.catalog("redteam")}
        self.assertTrue(entries["redteam"]["active"])
        self.assertFalse(entries["pentest"]["active"])


# ---------------------------------------------------------------- 提示词渲染

class PromptRenderingTestCase(unittest.TestCase):
    def test_prompt_carries_scope_and_mode(self):
        texts = {}
        for mode in agent_modes.MODES:
            text = chat.build_system_prompt(["10.0.0.0/8"], mode)
            self.assertNotIn("{mode}", text, "模式占位符没有被替换")
            self.assertNotIn("{scope}", text, "作用域占位符没有被替换")
            self.assertIn("10.0.0.0/8", text)
            self.assertIn(mode["prompt"], text)
            texts[mode["id"]] = text
        self.assertNotEqual(texts["pentest"], texts["redteam"])

    def test_mode_argument_is_optional(self):
        """既有调用方（含 test_probe.py）只传作用域，仍须拿到完整提示词。"""
        text = chat.build_system_prompt(["10.0.0.0/8"])
        self.assertNotIn("{mode}", text)
        self.assertIn(agent_modes.default_mode()["prompt"], text)

    def test_legacy_constant_is_fully_rendered(self):
        self.assertNotIn("{mode}", chat.SYSTEM_PROMPT)
        self.assertNotIn("{scope}", chat.SYSTEM_PROMPT)


# ---------------------------------------------------------------- 持久化

class ModePersistenceTestCase(unittest.TestCase):
    def setUp(self):
        self.dir, self.store = support.temporary_store("agent-modes.db")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_missing_key_returns_default(self):
        self.assertEqual(self.store.get_setting(agent_modes.SETTING_KEY,
                                                agent_modes.DEFAULT_MODE),
                         agent_modes.DEFAULT_MODE)
        self.assertEqual(self.store.get_setting("nope", "fallback"), "fallback")

    def test_value_survives_reopen(self):
        """切换必须落库：只活在一个进程的内存里等于重启就丢。"""
        self.store.set_setting(agent_modes.SETTING_KEY, "redteam")
        again = Store(self.store.path)
        try:
            self.assertEqual(again.get_setting(agent_modes.SETTING_KEY), "redteam")
        finally:
            again.close()

    def test_overwrite_keeps_one_row(self):
        self.store.set_setting(agent_modes.SETTING_KEY, "redteam")
        self.store.set_setting(agent_modes.SETTING_KEY, "pentest")
        self.assertEqual(self.store.get_setting(agent_modes.SETTING_KEY), "pentest")
        self.assertEqual(self.store.list_settings()[agent_modes.SETTING_KEY],
                         "pentest")


# ---------------------------------------------------------------- HTTP 契约

class AgentModeApiTestCase(unittest.TestCase):
    def setUp(self):
        self.dir, self.store = support.temporary_store("agent-mode-api.db")
        self.httpd, self.client = support.start(self.store, WEB_DIR)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.dir.cleanup()

    def test_list_reports_modes_and_active(self):
        status, payload = self.client.get("/api/v1/agent-modes")
        self.assertEqual(status, 200)
        self.assertEqual(payload["activeId"], agent_modes.DEFAULT_MODE)
        ids = [mode["id"] for mode in payload["modes"]]
        self.assertIn("redteam", ids)
        for entry in payload["modes"]:
            self.assertNotIn("prompt", entry)

    def test_activate_switches_and_persists(self):
        status, payload = self.client.post("/api/v1/agent-modes/activate",
                                           {"id": "redteam"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["activeId"], "redteam")
        # 再读一次接口 + 直接查库：切换必须是真的，不是只改了这次响应
        self.assertEqual(self.client.get("/api/v1/agent-modes")[1]["activeId"],
                         "redteam")
        self.assertEqual(self.store.get_setting(agent_modes.SETTING_KEY), "redteam")

    def test_unknown_mode_is_rejected(self):
        status, payload = self.client.post("/api/v1/agent-modes/activate",
                                           {"id": "nope"})
        self.assertEqual(status, 404)
        self.assertIn("nope", payload.get("error", ""))

    def test_operator_can_read_but_not_switch(self):
        """操作员必须看得到当前口径（他的产出按这个口径解释），但不能改。"""
        self.assertEqual(self.client.post("/api/v1/users", {
            "username": "op-mode", "password": "Operator-Pass1!",
            "role": "operator"})[0], 201)
        self.client.logout()
        self.assertEqual(self.client.login("op-mode", "Operator-Pass1!")[0], 200)
        self.assertEqual(self.client.get("/api/v1/agent-modes")[0], 200)
        status, _ = self.client.post("/api/v1/agent-modes/activate",
                                     {"id": "redteam"})
        self.assertEqual(status, 403)
        self.assertEqual(self.store.get_setting(agent_modes.SETTING_KEY,
                                                agent_modes.DEFAULT_MODE),
                         agent_modes.DEFAULT_MODE)

    def test_anonymous_is_rejected(self):
        self.client.logout()
        self.assertEqual(self.client.post("/api/v1/agent-modes/activate",
                                          {"id": "redteam"})[0], 401)

    def test_switch_requires_csrf(self):
        status, _ = self.client.request("POST", "/api/v1/agent-modes/activate",
                                        {"id": "redteam"}, with_csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.store.get_setting(agent_modes.SETTING_KEY,
                                                agent_modes.DEFAULT_MODE),
                         agent_modes.DEFAULT_MODE)


# ---------------------------------------------------------------- 真的生效了吗

class ModeTakesEffectTestCase(unittest.TestCase):
    """"切了模式"必须能在下发给模型的东西上看到，否则这功能等于没有。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("agent-mode-effect.db")
        self.httpd, self.client = support.start(self.store, WEB_DIR)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.dir.cleanup()

    def test_chat_tools_shows_the_prompt_actually_used(self):
        """界面上的边界说明必须等于实际下发的那一份。

        这里曾经返回一个"未注入作用域与模式"的静态常量，界面因此显示一套规则、
        模型按另一套执行——用户照界面上的说法判断目标能不能扫，会得出错误结论。
        """
        payload = self.client.get("/api/v1/chat/tools")[1]
        mode = agent_modes.default_mode()
        self.assertIn(mode["prompt"], payload["systemPrompt"])
        self.assertNotIn("{scope}", payload["systemPrompt"])
        self.assertNotIn("{mode}", payload["systemPrompt"])
        self.assertEqual(payload["agentMode"]["id"], mode["id"])

    def test_steps_and_prompt_follow_the_switch(self):
        before = self.client.get("/api/v1/chat/tools")[1]
        self.client.post("/api/v1/agent-modes/activate", {"id": "redteam"})
        after = self.client.get("/api/v1/chat/tools")[1]
        redteam = agent_modes.get_mode("redteam")
        self.assertEqual(after["maxSteps"], redteam["steps"])
        self.assertNotEqual(before["maxSteps"], after["maxSteps"])
        self.assertIn(redteam["prompt"], after["systemPrompt"])
        self.assertNotIn(agent_modes.get_mode("pentest")["prompt"],
                         after["systemPrompt"])


class TurnBudgetTestCase(unittest.TestCase):
    """`run_turn` 的步数预算真的取自模式，而不是那个没人再用的模块常量。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("agent-mode-turn.db")
        self.plane = _StubPlane(self.store)
        self.prompts = []
        self._saved_once = chat.chat_once
        self._saved_exec = chat.execute_tool
        chat.chat_once = self._fake_once
        chat.execute_tool = _stub_execute

    def tearDown(self):
        chat.chat_once = self._saved_once
        chat.execute_tool = self._saved_exec
        self.store.close()
        self.dir.cleanup()

    def _fake_once(self, profile, api_key, messages, tools, prompt, insecure=False):
        self.prompts.append(prompt)
        if tools is None:                   # 收尾调用：只给文本
            return {"ok": True, "text": "收尾结论", "toolCalls": []}
        # 每次都请求一次工具调用，直到撞上步数上限
        return {"ok": True, "text": "", "toolCalls": [
            {"id": "c1", "name": "list_executors", "arguments": {}}]}

    def _run(self):
        return chat.run_turn(self.plane, {"provider": "openai_compat"}, "k",
                             [{"role": "user", "text": "看看现状"}])

    def test_budget_follows_the_active_mode(self):
        result = self._run()
        self.assertEqual(result["steps"], agent_modes.get_mode("pentest")["steps"])
        self.assertTrue(result.get("truncated"))
        self.assertIn(agent_modes.get_mode("pentest")["prompt"], self.prompts[-1])

        self.store.set_setting(agent_modes.SETTING_KEY, "redteam")
        result = self._run()
        self.assertEqual(result["steps"], agent_modes.get_mode("redteam")["steps"])
        self.assertIn(agent_modes.get_mode("redteam")["prompt"], self.prompts[-1])

    def test_explicit_max_steps_still_wins(self):
        """显式传参优先：调用方（测试或脚本）要能压过模式默认值。"""
        result = chat.run_turn(self.plane, {"provider": "openai_compat"}, "k",
                               [{"role": "user", "text": "x"}], max_steps=2)
        self.assertEqual(result["steps"], 2)

    def test_plane_without_mode_falls_back(self):
        class Bare:
            probe_scope = ["10.0.0.0/8"]

        self.assertEqual(chat._plane_mode(Bare())["id"], agent_modes.DEFAULT_MODE)


class ModeDoesNotBypassGateTestCase(unittest.TestCase):
    """本功能最重要的一条约束：模式改变取向，不改变门禁。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("agent-mode-gate.db")
        self.httpd, self.client = support.start(self.store, WEB_DIR,
                                                probe_scope=["10.0.0.0/8"])

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.dir.cleanup()

    def test_redteam_is_not_an_unlock_switch(self):
        self.assertEqual(self.client.post("/api/v1/agent-modes/activate",
                                          {"id": "redteam"})[0], 200)
        status, payload = self.client.post("/api/v1/jobs",
                                           {"kind": "probe", "target": "8.8.8.8",
                                            "ports": "80"})
        self.assertEqual(status, 403, payload)
        # 越界任务不得进队列：探测一旦发包就已经发生，事后补救没有意义
        self.assertEqual(self.store.jobs(), [])

    def test_pentest_mode_is_gated_the_same_way(self):
        """反向对照：两种模式下同一个越界目标得到同一个结论。"""
        self.assertEqual(self.client.post("/api/v1/agent-modes/activate",
                                          {"id": "pentest"})[0], 200)
        status, _ = self.client.post("/api/v1/jobs",
                                     {"kind": "probe", "target": "8.8.8.8",
                                      "ports": "80"})
        self.assertEqual(status, 403)
        self.assertEqual(self.store.jobs(), [])


# ---------------------------------------------------------------- 桩

class _StubPlane:
    """`run_turn` 只用到 agent_mode 与 probe_scope；其余（工具执行）被打桩。"""

    def __init__(self, store):
        self.store = store
        self.probe_scope = ["10.0.0.0/8"]

    def agent_mode(self):
        return agent_modes.resolve(
            self.store.get_setting(agent_modes.SETTING_KEY, agent_modes.DEFAULT_MODE))


def _stub_execute(plane, name, arguments, fallback_eid="", *args, **kwargs):
    """工具执行的桩。

    签名要跟着被替换的真函数走：`execute_tool` 现在多收权限档与角色三个参数。
    桩里用 `*args, **kwargs` 吞掉它们，是因为**这组用例测的是步数预算取自哪个
    Agent 预设**，工具本身怎么执行不在它的关注范围里。写成固定参数会让测试
    在每次 `execute_tool` 加参时都报一次类型错误，掩盖掉真正的回归。
    """
    return {"ok": True, "summary": "桩工具", "data": None, "text": "桩工具",
            "arguments": arguments or {}, "durationMs": 0, "touched": []}


if __name__ == "__main__":
    unittest.main()
