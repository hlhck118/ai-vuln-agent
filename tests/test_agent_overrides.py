# -*- coding: utf-8 -*-
"""内置与插件来源的 Agent 预设**就地编辑**：覆盖层的语义、以及它为什么不是复制。

这一组盯的是一件事：**改一档预设，要能让已经钉在它上面的会话下一轮就跟上。**

老版本只允许三种来源里的"自建"那一种被编辑，另外两种给的是「复制为新预设」。
那条路能改内容，但改完是一条 **id 不同**的新预设——用户把「渗透测试模式」的单轮
步数从 6 改成 77，回会话一看还是 6，因为那条会话钉的是 `pentest`，而他的改动落在
`custom-pentest` 上。他会得出"改了没用"的结论，而改动其实好好地存着。

所以改法换成**叠加**：覆盖行的 `id` 与被覆盖的预设相同，原件不落库（它每次都从
代码常量或插件快照重算），于是

* 钉在这一档上的会话下一轮自动读到新值（`test_override_reaches_a_pinned_session`）；
* 「恢复原始」是删掉覆盖行，不是重建（`test_reset_is_lossless`）；
* 插件卸载后那一层不生效但也不丢（`test_a_source_that_vanished_keeps_the_row`）。

复制仍然保留：那是"另起一档"的需求，与"改这一档"是两件事，两条路各有各的用例
（`test_cloning_leaves_the_pinned_session_alone` 是覆盖的对照组）。

另一半是本轮的另一项改动：单轮步数预算的上限从 40 抬到 99999。上限**没有消失**，
它仍然是唯一的入参校验点，下限与类型照旧（`StepsCeilingTestCase`）。
"""
import json
import os
import sqlite3
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agent_modes, agents, server                        # noqa: E402
from pentestd.store import SCHEMA_VERSION, Store                        # noqa: E402
from pentestd.models import ValidationError                             # noqa: E402
from tests import support                                               # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


def fake_snapshot(plugin_present=True):
    """一份形状与 `dshplugins.snapshot()` 一致的假快照，可以模拟"插件被卸载"。"""
    presets = []
    if plugin_present:
        presets.append({
            "id": "redteam", "name": "红队演练模式", "description": "插件自带",
            "order": 0, "persona": "你是红队指挥官。", "path": "/fake/rt",
            "composition": "", "plugin": "dsh-purge", "pluginVersion": "1.2.3",
            "presetId": "dsh-purge:redteam", "integrated": True, "reason": "集成"})
    return {
        "available": True, "home": "/fake/dsh", "profile": "unit",
        "profileDir": "/fake/dsh/profiles/unit", "profiles": ["unit"],
        "plugins": [{"name": "dsh-purge", "version": "1.2.3", "bundled": True,
                     "missing": False, "presets": 1, "skills": 0}] if plugin_present
        else [],
        "presets": presets, "skills": [],
    }


def override_row(preset_id, **overrides):
    """一行 `is_override` 的存储层形态。默认值就是"只改了步数"那种情形。"""
    row = {"id": preset_id, "label": "我改过的", "summary": "", "note": "",
           "prompt": "改过的正文。", "steps": 77, "skills": [],
           "isOverride": True, "baseRev": "0" * 16}
    row.update(overrides)
    return row


# ------------------------------------------------------- 覆盖在目录层怎么生效

class OverlayCatalogTestCase(unittest.TestCase):
    """纯函数层：`agents.all_agents` 收到行之后目录长什么样。"""

    def catalog(self, rows, snapshot=None):
        """走 `agents.catalog`（公开形态）而不是 `all_agents`（内部形态）。

        两者只差几个字段，而差的正好是这一组要断言的那些：`edited` / `canReset` /
        `deletable` / `base*` 是 `public()` 加上去的。拿内部形态断言会得到 KeyError，
        而那个 KeyError 看起来像"服务端没发这些字段"——实际是测试读错了层。
        """
        return {item["id"]: item for item in
                agents.catalog("", snapshot or fake_snapshot(), rows)}

    def test_an_override_shares_the_id_of_what_it_covers(self):
        """覆盖行与被覆盖的预设**同 id**，所以目录里只有一条。

        这条看起来像废话，它是整套办法的支点：id 相同才有"钉在这一档上的会话跟着变"，
        而目录里仍然只有一条才有"下拉框里不出现两个渗透测试模式"。两条同时成立才算对。
        """
        builtin_id = agent_modes.default_mode()["id"]
        catalog = self.catalog([override_row(builtin_id, steps=77)])
        entry = [item for item in
                 agents.catalog("", fake_snapshot(), [override_row(builtin_id)])]
        self.assertEqual(len([item for item in entry if item["id"] == builtin_id]), 1,
                         "同一个 id 在目录里只能出现一次")
        item = catalog[builtin_id]
        self.assertEqual(item["steps"], 77, "覆盖值必须生效")
        self.assertTrue(item["edited"])
        self.assertTrue(item["canReset"])
        self.assertFalse(item["deletable"], "内置档不能被删掉，只能恢复")

    def test_the_original_is_still_readable_next_to_the_override(self):
        """原件没有消失：`base*` 给的就是它，界面靠这个说清"原件是哪一个"。"""
        builtin_id = agent_modes.default_mode()["id"]
        origin = agent_modes.default_mode()
        item = self.catalog([override_row(builtin_id)])[builtin_id]
        self.assertEqual(item["baseLabel"], origin["label"])
        self.assertEqual(item["baseSteps"], origin["steps"])
        self.assertNotEqual(item["label"], item["baseLabel"],
                            "覆盖值与原件的标签不同，界面才分辨得出改没改")

    def test_losing_the_source_keeps_the_row_but_hides_the_preset(self):
        """插件卸载：预设从目录里消失是对的（那一档不存在了），改动不丢。

        删掉那一行才是错的——"卸载一个插件"不该顺手删掉使用者写过的东西；而装上
        之后改动立刻回来，也不需要他再补一遍。
        """
        rows = [override_row("dsh-purge")]
        self.assertIn("dsh-purge", self.catalog(rows, fake_snapshot(True)))
        gone = self.catalog(rows, fake_snapshot(False))
        self.assertNotIn("dsh-purge", gone, "插件不在了，这一档就不该出现在目录里")
        self.assertIn("dsh-purge", self.catalog(rows, fake_snapshot(True)),
                      "插件回来，改动跟着回来")

    def test_a_vanished_source_leaves_no_stray_entry_behind(self):
        """没能应用的行不许被当成"一条新预设"混进目录。

        这是 `custom_agents` 里那个 `continue` 存在的全部理由：漏了它，一条 `is_override`
        的行会以基础形态（prompt 是覆盖值、没有 source/kind）出现在下拉框里——一条
        点开看不出出处的幽灵预设。
        """
        ids = [item["id"] for item in
               agents.all_agents(fake_snapshot(False), [override_row("dsh-purge")])]
        self.assertNotIn("dsh-purge", ids)
        self.assertEqual(sorted(ids), sorted(item["id"] for item in
                                             agents.all_agents(fake_snapshot(False), [])))

    def test_baseChanged_is_true_only_when_the_upstream_moved(self):
        """指纹对得上就是"上游没动"，对不上就是"上游动过"。**不能反着猜。**

        覆盖行里存的是保存那一刻原件的内容副本。插件升级改了原始口径之后，使用者的
        副本会在无声中盖住新版本——不说出来的话，他以为自己读的是插件的口径，而
        界面上没有任何迹象说明两者已经不是同一个东西了。
        """
        builtin_id = agent_modes.default_mode()["id"]
        origin = [item for item in agents.builtin() if item["id"] == builtin_id][0]
        row = override_row(builtin_id, baseRev=agents.base_rev(origin))
        self.assertFalse(self.catalog([row])[builtin_id]["baseChanged"],
                         "指纹一致时不能报'上游变过'——那会让每一次编辑都挂一条假提醒")
        moved = override_row(builtin_id, baseRev="f" * 16)
        self.assertTrue(self.catalog([moved])[builtin_id]["baseChanged"])

    def test_a_self_built_row_is_never_treated_as_an_override(self):
        """自建预设（`isOverride` 缺省）照旧进目录，不被当成覆盖滤掉。

        这一条钉的是**缺省方向**。旧库里每一行都是自建预设，把它们读成覆盖会让
        "我建的预设全没了"——而那种失败在界面上只表现为一个空列表。
        """
        rows = [{"id": "custom-lateral", "label": "内网横向", "prompt": "横向优先。",
                 "steps": 14, "skills": []}]
        catalog = self.catalog(rows)
        self.assertIn("custom-lateral", catalog)
        self.assertFalse(catalog["custom-lateral"]["edited"])
        self.assertTrue(catalog["custom-lateral"]["deletable"])


# ---------------------------------------------------- 老库打开时加列的方向

class OverlayMigrationTestCase(unittest.TestCase):
    """schema v13 → v14 加两列。加列本身没什么，**默认值的方向**才是要钉的东西。"""

    def test_an_old_database_gets_the_columns_with_the_old_semantics(self):
        directory, _unused = support.temporary_store("overlay-migrate.db")
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "old.db")
        conn = sqlite3.connect(path)
        # v13 的 agent_presets：没有 is_override，也没有 base_rev
        conn.executescript(
            "CREATE TABLE agent_presets ("
            " id TEXT PRIMARY KEY, label TEXT NOT NULL, summary TEXT DEFAULT '',"
            " note TEXT DEFAULT '', prompt TEXT NOT NULL,"
            " steps INTEGER NOT NULL DEFAULT 6, skills TEXT DEFAULT '[]',"
            " derived_from TEXT DEFAULT '', created_by TEXT DEFAULT '',"
            " created_at REAL, updated_at REAL);")
        conn.execute("INSERT INTO agent_presets(id,label,prompt,steps)"
                     " VALUES('custom-old','老预设','老正文',9)")
        conn.execute("PRAGMA user_version=13")
        conn.commit()
        conn.close()

        store = Store(path)
        self.addCleanup(store.close)
        columns = {row["name"] for row in
                   store.conn.execute("PRAGMA table_info(agent_presets)")}
        self.assertIn("is_override", columns)
        self.assertIn("base_rev", columns)
        row = store.get_agent_preset("custom-old")
        self.assertFalse(row["isOverride"],
                         "既有行必须按**旧语义**（自建预设）读，不能变成覆盖")
        self.assertEqual(row["baseRev"], "")
        # 而且它真的还在目录里——只断言列名存在是不够的
        catalog = {item["id"]: item for item in
                   agents.all_agents(None, store.list_agent_presets())}
        self.assertIn("custom-old", catalog)
        self.assertEqual(catalog["custom-old"]["steps"], 9)

    def test_schema_version_is_at_least_14(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 14)


# -------------------------------------------------------------- HTTP 全链路

class OverlayHttpTestCase(unittest.TestCase):
    """端到端：改一档内置预设，看**钉在它上面的会话**下一条配置里的步数。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("overlay-http.db")
        self.httpd, self.client = support.start(self.store, WEB_DIR)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.dir)

    def pinned_session(self, agent_id):
        session = self.store.create_chat_session("钉住的")
        self.client.post("/api/v1/chat/sessions/%s/config" % session["id"],
                         {"agentId": agent_id})
        return session["id"]

    def session_steps(self, session_id):
        status, payload = self.client.get("/api/v1/chat/sessions/%s" % session_id)
        self.assertEqual(status, 200, payload)
        return payload["config"]["steps"]

    def test_override_reaches_a_pinned_session(self):
        """**这一条是整件事的理由。** 会话钉在内置档上，改那一档，它下一轮就跟上。

        换成"复制为新预设再改"，这里得到的是 6——一条 id 不同的新预设改不动这条会话。
        用例断言的是 77，而 77 只可能来自覆盖。
        """
        preset_id = agent_modes.default_mode()["id"]
        session_id = self.pinned_session(preset_id)
        before = self.session_steps(session_id)
        self.assertEqual(before, agent_modes.default_mode()["steps"])
        status, payload = self.client.post(
            "/api/v1/agents/presets/%s" % preset_id, {"steps": 77})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["overridden"])
        self.assertEqual(self.session_steps(session_id), 77,
                         "钉在这一档上的会话必须读到新的步数")
        config = self.client.get(
            "/api/v1/chat/sessions/%s" % session_id)[1]["config"]
        self.assertEqual(config["agentId"], preset_id, "会话的 Agent 选择没有变")

    def test_cloning_leaves_the_pinned_session_alone(self):
        """对照组：复制出来的预设 id 不同，所以钉在原档上的会话**不受影响**。

        把这条与上一条放在一起看，才是"覆盖与复制是两件事"的完整证据。单独一条
        "克隆能用"说明不了任何关于覆盖的事。
        """
        preset_id = agent_modes.default_mode()["id"]
        session_id = self.pinned_session(preset_id)
        status, payload = self.client.post("/api/v1/agents/presets/clone",
                                           {"from": preset_id})
        self.assertEqual(status, 201, payload)
        clone_id = payload["preset"]["id"]
        self.assertNotEqual(clone_id, preset_id)
        self.client.post("/api/v1/agents/presets/%s" % clone_id, {"steps": 88})
        self.assertEqual(self.session_steps(session_id),
                         agent_modes.default_mode()["steps"],
                         "改副本不该动到钉在原档上的会话")

    def test_reset_is_lossless_and_keeps_the_pins(self):
        """「恢复原始」回到原件，会话的 Agent 选择原样留着。"""
        preset_id = agent_modes.default_mode()["id"]
        session_id = self.pinned_session(preset_id)
        origin = agent_modes.default_mode()
        self.client.post("/api/v1/agents/presets/%s" % preset_id,
                         {"steps": 77, "label": "我的档"})
        status, payload = self.client.post(
            "/api/v1/agents/presets/%s/reset" % preset_id, {})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["steps"], origin["steps"])
        self.assertIsNone(self.store.get_agent_preset(preset_id),
                          "覆盖行必须真的被删掉，否则它会一直盖着")
        self.assertEqual(self.session_steps(session_id), origin["steps"])
        self.assertEqual(
            self.store.get_chat_session(session_id)["agentId"], preset_id,
            "撤一层覆盖不该把会话的 Agent 选择清掉——那个 id 仍然存在")

    def test_the_override_lands_with_the_sources_fingerprint(self):
        """覆盖行里存的是**保存那一刻原件**的指纹。没有它就没有"上游动过"这件事。"""
        preset_id = agent_modes.default_mode()["id"]
        origin = [item for item in agents.builtin() if item["id"] == preset_id][0]
        self.client.post("/api/v1/agents/presets/%s" % preset_id, {"steps": 77})
        row = self.store.get_agent_preset(preset_id)
        self.assertTrue(row["isOverride"])
        self.assertEqual(row["baseRev"], agents.base_rev(origin))
        self.assertNotEqual(row["baseRev"], "",
                            "空指纹会被 `_overlaid` 判成'上游没动'，等于静默关掉提醒")

    def test_a_self_built_preset_still_cannot_pose_as_an_override(self):
        """自建预设走创建路径，`isOverride` 必须是假。

        真的变成覆盖的话，它会去找一条同 id 的内置预设来盖，找不到就不生效——表现是
        "我新建的预设保存成功但下拉框里没有"。
        """
        status, payload = self.client.post(
            "/api/v1/agents/presets",
            {"label": "内网横向专项", "handle": "lateral", "prompt": "横向优先。",
             "steps": 14, "skills": []})
        self.assertEqual(status, 201, payload)
        self.assertFalse(payload["overridden"])
        self.assertFalse(self.store.get_agent_preset("custom-lateral")["isOverride"])

    def test_the_write_needs_an_account(self):
        """覆盖是改**全局共享**的目录，权限与「切换默认」同级：匿名请求必须被挡。"""
        self.dir2, self.store2 = support.temporary_store("overlay-anon.db")
        try:
            httpd, anon = support.start(self.store2, WEB_DIR, admin=False)
            try:
                status, _payload = anon.post(
                    "/api/v1/agents/presets/%s" % agent_modes.default_mode()["id"],
                    {"steps": 77})
                self.assertIn(status, (401, 403), status)
                self.assertIsNone(
                    self.store2.get_agent_preset(agent_modes.default_mode()["id"]),
                    "被拦下的请求不能留下任何一行")
            finally:
                support.stop(httpd, self.store2)
        finally:
            self.dir2.cleanup()

    def test_the_merge_base_is_the_effective_value_not_the_original(self):
        """第二次编辑的合并起点是**当前生效值**。

        起点取原件的话，"只改名字"会把上一次改的步数悄悄退回原件——而使用者刚刚才
        把它改成 77，他会以为保存把它弄回去了。
        """
        preset_id = agent_modes.default_mode()["id"]
        self.client.post("/api/v1/agents/presets/%s" % preset_id, {"steps": 77})
        status, payload = self.client.post(
            "/api/v1/agents/presets/%s" % preset_id, {"label": "改个名字"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["preset"]["steps"], 77)
        self.assertEqual(payload["preset"]["label"], "改个名字")


# ---------------------------------------------------- 步数上限：40 → 99999

class StepsCeilingTestCase(unittest.TestCase):
    """上限抬到 99999 之后，**边界没消失**，只是位置变了。

    这个常量是唯一的入参校验点：没有它，一个手滑打出的负号或一个很长的数字会直接
    进入 `chat` 的循环判断（`steps >= max_steps`），而那种输入的表现是"这一轮永远
    不结束"。所以要同时钉住三件事：新上限生效、下限照旧、垃圾输入照样被挡。
    """

    def validate(self, **overrides):
        body = {"label": "档", "handle": "x1", "prompt": "正文", "steps": 6}
        body.update(overrides)
        return agents.validate_preset(body, [])

    def test_the_ceiling_is_99999(self):
        self.assertEqual(agents.MAX_STEPS, 99999)
        self.assertEqual(self.validate(steps=99999)["steps"], 99999,
                         "上限本身必须是合法的——边界值被拒就是上限没抬到位")

    def test_above_the_ceiling_is_still_rejected(self):
        with self.assertRaises(ValidationError) as caught:
            self.validate(steps=agents.MAX_STEPS + 1)
        self.assertIn("超出范围", str(caught.exception))

    def test_the_reason_for_having_a_ceiling_is_still_stated(self):
        """报错要说清"为什么有上下限"，否则下一个人会以为它是随手写的魔数。"""
        with self.assertRaises(ValidationError) as caught:
            self.validate(steps=0)
        message = str(caught.exception)
        self.assertIn("单轮步数预算超出范围", message)
        self.assertIn("建任务", message)

    def test_the_floor_and_the_types_are_unchanged(self):
        for bad in (0, -3):
            with self.assertRaises(ValidationError, msg=str(bad)):
                self.validate(steps=bad)
        with self.assertRaises(ValidationError):
            self.validate(steps=True)          # bool 是 int 的子类
        with self.assertRaises(ValidationError):
            self.validate(steps="很多步")
        self.assertEqual(self.validate(steps="1234")["steps"], 1234,
                         "HTML 表单过来的一律是字符串，必须接受")

    def test_the_defaults_are_untouched(self):
        """抬上限**不是**改默认。新预设的缺省仍是 6，内置两档仍是它们原来的数。"""
        self.assertEqual(agents.DEFAULT_STEPS, 6)
        self.assertEqual(agents.validate_preset(
            {"label": "档", "handle": "x2", "prompt": "正文"}, [])["steps"], 6)
        self.assertEqual([item["steps"] for item in agents.builtin()],
                         [mode["steps"] for mode in agent_modes.MODES])


# ------------------------------------------------------------- 界面契约

class OverlayUiContractTestCase(unittest.TestCase):
    """界面必须**跟着改**，否则能力有了而入口还在劝人绕路。

    老界面对内置与插件来源画的是"内置口径随程序更新而变，不能就地改"，并把
    「复制为新预设」说成唯一的改动途径。那句话现在是假话：改是能改的，只是改出来
    一层覆盖。留着它的后果很具体——使用者照着它去点「复制为新预设」，得到一条 id
    不同的新预设，改完对已有会话不生效，而他没有任何线索知道为什么。
    """

    def setUp(self):
        with open(os.path.join(WEB_DIR, "settings.js"), "r", encoding="utf-8") as handle:
            self.js = handle.read()
        with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as handle:
            self.html = handle.read()

    def test_the_old_refusal_is_gone(self):
        for stale in ("不能就地改", "唯一的改动途径"):
            self.assertFalse(
                stale in self.js,
                "界面还在说内置/插件来源不能改，而它现在能改：%s" % stale)

    def test_every_source_gets_an_edit_button(self):
        """编辑按钮不再挂在 `mode.custom` 分支上。"""
        self.assertIn("presetAction('编辑', 'edit', mode.id)", self.js)
        self.assertFalse(
            "if (mode.custom) {\n      const edit" in self.js,
            "编辑按钮不能只在自建预设上画")

    def test_the_reset_action_is_wired_to_end_to_end(self):
        """从按钮到端点的整条链都要在：少一环就是一颗点不动的按钮。"""
        self.assertIn("presetAction('恢复原始口径', 'reset', mode.id)", self.js)
        self.assertIn("=== 'reset'", self.js, "点击分支要认得这个动作")
        self.assertIn("/reset'", self.js, "要真的发出那个请求")
        # 用 `with`：直接 `open(...).read()` 会把句柄留到回收那一刻，而
        # `-W error::ResourceWarning` 下这会变成一条**与断言无关的**失败。
        with open(os.path.join(ROOT, "pentestd", "server.py"), "r",
                  encoding="utf-8") as fh:
            self.assertIn("reset_agent_preset", fh.read(),
                          "服务端必须有对应的处理函数")

    def test_the_form_says_what_saving_actually_does(self):
        """保存前要说清"这会在原件之上加一层覆盖"，而不是让使用者自己猜。"""
        self.assertIn("agent-form-scope", self.html)
        self.assertIn("agentFormScope", self.js)
        self.assertIn("一层覆盖", self.js)

    def test_the_step_input_matches_the_new_ceiling(self):
        """输入框的 max 与服务端的校验必须一致。

        不一致有两种后果，都不会被发现：max 小 → 浏览器挡住合法值（用户以为坏了）；
        max 大 → 提交才报错（用户以为保存失败）。
        """
        self.assertIn('id="agent-steps"', self.html)
        self.assertIn('max="99999"', self.html)
        self.assertNotIn('max="40"', self.html)

    def test_the_base_value_is_surfaced_on_the_card(self):
        """卡片要说清原件是什么，否则「恢复原始」是一颗不知道会回到哪里的按钮。"""
        for needed in ("baseLabel", "baseSteps", "baseChanged"):
            self.assertIn(needed, self.js, "卡片没有用上服务端给的 %s" % needed)

    def test_the_server_actually_sends_those_fields(self):
        """界面读了字段，服务端就得真的下发——两边各写一半是这类改动最常见的裂缝。"""
        spec = agents.public(agents.default_agent())
        for field in ("edited", "canReset", "baseChanged", "baseLabel", "baseSteps",
                      "editable", "deletable"):
            self.assertIn(field, spec, "public() 少了字段：%s" % field)
        self.assertTrue(json.dumps(spec, ensure_ascii=False))
