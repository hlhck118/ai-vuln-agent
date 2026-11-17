# -*- coding: utf-8 -*-
"""Agent 预设的增删改查：形态校验、目录合成、写路径门禁、删除的连带后果。

这一组用例盯的是几件**从界面看不出来**的事：

1. **改名字不能换 id。** id 一旦变了，每个钉在这个预设上的会话都会静默退回
   "跟随默认"——用户只看到"我改了个名字，会话的 Agent 怎么变了"。
2. **删不掉要说清为什么。** 正在被当全局默认用的预设必须拒绝删除（409），而不是
   顺手把默认改到别处；内置与插件来源的删除要回 400 并指到能解决问题的动作
   （「恢复原始」/「复制为新预设」），而不是 404 让人以为列表里那条是脏数据。
   **"能改"与"能删"是两件事**：三种来源都能就地编辑（内置与插件来源改出来的
   是一层覆盖，见 `test_agent_overrides.py`），只有自建预设能删掉。
3. **删预设会动到别人的会话。** 钉在它上面的会话在同一事务里改回"跟随默认"，
   影响条数如实报出——不报的话，用户无从知道刚才那一下动了多少东西。
4. **口径正文真的到了模型面前。** 前三条都在验"配置对了"，第四条用脚本化模型读
   **实际发出去的系统提示**，验"生效了"——两者之间正是最容易断掉的地方。
"""
import json
import os
import sqlite3
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agent_modes, agents, chat, server                # noqa: E402
from pentestd.models import ValidationError                            # noqa: E402
from pentestd.store import SCHEMA_VERSION                              # noqa: E402
from tests import support                                              # noqa: E402
from tests.test_chat import ScriptedModel, text_reply                   # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")

VALID = {"label": "内网横向专项", "handle": "lateral",
         "prompt": "以横向移动为主线，每一步标出当前所处网段与可达边界。",
         "summary": "内网横向专用口径", "note": "适合拿到第一台跳板之后。",
         "steps": 14, "skills": ["chisel-tunnel"]}


def fake_snapshot():
    """一份形状与 `dshplugins.snapshot()` 一致的假快照。

    刻意做成"两个插件各带手册"：只放一个插件的话，**"这个预设只拿到自己的手册"
    这条规则在测试里永远只走到一个分支**——而它正是这次要钉住的东西。
    """
    return {
        "available": True, "home": "/fake/dsh", "profile": "unit",
        "profileDir": "/fake/dsh/profiles/unit", "profiles": ["unit"],
        "plugins": [
            {"name": "dsh-purge", "version": "1.2.3", "bundled": True,
             "missing": False, "presets": 1, "skills": 2},
            {"name": "@howmp/dsh-pentest", "version": "9.9.9", "bundled": True,
             "missing": False, "presets": 1, "skills": 1},
        ],
        "presets": [
            {"id": "redteam", "name": "红队演练模式", "description": "插件自带",
             "order": 0, "persona": "你是红队指挥官。", "path": "/fake/rt",
             "composition": "", "plugin": "dsh-purge", "pluginVersion": "1.2.3",
             "presetId": "dsh-purge:redteam", "integrated": True, "reason": "集成"},
            {"id": "pentest", "name": "渗透模式", "description": "插件自带",
             "order": 0, "persona": "你是渗透测试指挥官。", "path": "/fake/pt",
             "composition": "", "plugin": "@howmp/dsh-pentest",
             "pluginVersion": "9.9.9", "presetId": "@howmp/dsh-pentest:pentest",
             "integrated": True, "reason": "集成"},
        ],
        "skills": [
            {"name": "chisel-tunnel", "path": "/fake/skills/chisel-tunnel.md",
             "plugin": "dsh-purge", "integrated": True, "reason": "集成"},
            {"name": "webshell-toolkit", "path": "/fake/skills/webshell-toolkit.md",
             "plugin": "dsh-purge", "integrated": True, "reason": "集成"},
            {"name": "recon-pipeline", "path": "/fake/skills/recon-pipeline.md",
             "plugin": "@howmp/dsh-pentest", "integrated": True, "reason": "集成"},
            {"name": "modlens", "path": "/fake/skills/modlens.md",
             "plugin": "@liustack/modlens", "integrated": False,
             "reason": "未发现安全作业相关内容"},
        ],
        "errors": [], "scannedAt": 0.0,
    }


# ------------------------------------------------------------------ 形态校验

class PresetValidationTestCase(unittest.TestCase):
    """纯校验：只判形态，不判内容（内容放不放开看门禁层，与这里无关）。"""

    def validate(self, **overrides):
        body = dict(VALID)
        body.update(overrides)
        return agents.validate_preset(body, [])

    def test_happy_path_normalizes_the_id(self):
        out = self.validate()
        self.assertEqual(out["id"], "custom-lateral")
        self.assertEqual(out["steps"], 14)
        self.assertEqual(out["skills"], ["chisel-tunnel"])

    def test_id_is_prefixed_so_it_cannot_shadow_a_builtin(self):
        """`handle` 取 builtin 的名字也不能撞上它——前缀是这道隔离本身。"""
        out = self.validate(handle="redteam")
        self.assertEqual(out["id"], "custom-redteam")

    def test_handle_collision_with_an_existing_preset_is_rejected(self):
        with self.assertRaises(ValidationError) as caught:
            agents.validate_preset(dict(VALID), ["pentest", "custom-lateral"])
        self.assertIn("已被占用", str(caught.exception))

    def test_non_ascii_label_needs_an_explicit_handle(self):
        """中文名挤不出 id，此时报错要**告诉人怎么办**，而不是让它随机生成。

        id 会写进数据库与会话记录，随机生成的话用户永远说不清"我的会话钉在哪个
        预设上"。所以宁可在这里拦下。
        """
        with self.assertRaises(ValidationError) as caught:
            self.validate(label="内网横向", handle="")
        message = str(caught.exception)
        self.assertIn("手动填一个", message)

    def test_bad_handles_are_rejected(self):
        for handle in ("has space", "1", "-lead", "x" * 40, "中文", "a.b", "a/b"):
            with self.assertRaises(ValidationError, msg=handle):
                self.validate(handle=handle)

    def test_handle_is_lowercased_rather_than_rejected(self):
        """大小写不是错误输入，是同一个 id 的另一种写法。

        库里的 id、下拉框里的 id、会话记录里的 id 必须是同一个字符串，否则
        "我选了它"和"库里存的它"在两处长得不一样，只能靠人肉核对。所以统一小写，
        而不是让用户为大写字母重填一次。
        """
        self.assertEqual(self.validate(handle="Lateral-2")["id"], "custom-lateral-2")

    def test_prompt_is_required_because_it_is_the_whole_posture(self):
        with self.assertRaises(ValidationError) as caught:
            self.validate(prompt="   ")
        self.assertIn("作业口径不能为空", str(caught.exception))

    def test_prompt_has_no_length_cap(self):
        """**口径正文不收字数上限**（本部署的设置；原为 8000）。

        撒掉的那道校验**只拦长度**，而它的理由（这段正文每轮都随系统提示下发，长度乘以
        对话轮数）今天仍然成立——所以撤掉的是拦门，不是那句提醒。提醒改由表单上的
        `N 字` 读数承担（`#agent-prompt-count`，见 `test_ui_contract` 的那条契约）。

        这条用例钉住的是"多长都原样收、原样回"。它的真实用途是**防止上限被顺手加回来**：
        加回来的表现不是报错，而是某个使用者刚写完一份详尽规范点保存时突然 400，而错的
        一方看起来是他自己。
        """
        long_prompt = "啊" * 200000
        self.assertEqual(self.validate(prompt=long_prompt)["prompt"], long_prompt)
        # 原来恰好卡在边界两侧的两条，现在同待遇：收。
        self.assertEqual(len(self.validate(prompt="啊" * 8000)["prompt"]), 8000)
        self.assertEqual(len(self.validate(prompt="啊" * 8001)["prompt"]), 8001)
        self.assertFalse(hasattr(agents, "MAX_PROMPT_CHARS"),
                         "上限常量还在：要么它被加回来了，要么留了个没人用的死常量")

    def test_steps_bounds_and_types(self):
        for bad in (0, agents.MAX_STEPS + 1, -3):
            with self.assertRaises(ValidationError, msg=str(bad)):
                self.validate(steps=bad)
        # True 是 int 的子类，不单独挡掉就会静默变成 1 次
        with self.assertRaises(ValidationError):
            self.validate(steps=True)
        with self.assertRaises(ValidationError):
            self.validate(steps="很多步")
        # 字符串形式的数字要接受：HTML 表单过来的一律是字符串
        self.assertEqual(self.validate(steps="9")["steps"], 9)

    def test_skills_must_be_a_list_and_is_deduped_in_order(self):
        with self.assertRaises(ValidationError):
            self.validate(skills="chisel-tunnel")
        self.assertEqual(self.validate(skills=["a", "b", "a", "  "])["skills"],
                         ["a", "b"])

    def test_update_keeps_the_id_even_when_the_label_changes(self):
        """**这条是整个文件里最重要的一条。**

        改名字若换 id，每个钉在这个预设上的会话都会静默退回"跟随默认"。
        用户看到的是"我改了个名字，会话的 Agent 怎么变了"，而库里两处都对得上，
        事后几乎无法从数据里还原出因果。
        """
        out = agents.validate_preset(dict(VALID, label="完全换了个名字", handle="whatever"),
                                     [], existing_id="custom-lateral")
        self.assertEqual(out["id"], "custom-lateral")
        self.assertEqual(out["label"], "完全换了个名字")


# ------------------------------------------------------------------ 目录合成

class AgentCatalogTestCase(unittest.TestCase):

    def rows(self, **overrides):
        row = {"id": "custom-lateral", "label": "内网横向专项",
               "prompt": "横向优先。", "summary": "说明", "note": "场合",
               "steps": 14, "skills": ["chisel-tunnel"]}
        row.update(overrides)
        return [row]

    def test_custom_sits_between_builtin_and_plugin(self):
        """顺序是有意的：内置（默认值所在档）→ 自定义（自己写的）→ 插件（别人给的）。"""
        ids = [item["id"] for item in
               agents.all_agents(fake_snapshot(), self.rows())]
        self.assertEqual(ids[:2], ["pentest", "redteam"])
        self.assertEqual(ids[2], "custom-lateral")
        self.assertIn("dsh-purge", ids[3:])

    def test_public_flags_editability_by_source(self):
        """三种来源都能编辑，但**只有自建预设能删除**。

        "能编辑"从前是 `kind == "custom"` 的专属，理由是"内置/插件来源就地改会在下一次
        请求被覆盖"。那个理由现在由覆盖行解决了（改出来的是一层同 id 的叠加，原件仍在
        代码或插件快照里），所以 `editable` 对三种来源都为真。

        `deletable` 仍然只有自建预设为真，而且**必须**如此：删掉一条内置档会让它从
        下拉框里消失，而使用者真正要的是"回到原来的口径"——那是覆盖行的删除，由
        `canReset` 表达，不是 `deletable`。
        """
        catalog = {item["id"]: item for item in agents.catalog(
            "custom-lateral", fake_snapshot(), self.rows())}
        custom = catalog["custom-lateral"]
        self.assertTrue(custom["custom"] and custom["editable"] and custom["deletable"])
        self.assertFalse(custom["edited"], "没被覆盖过的自建预设没有 edited 标记")
        self.assertFalse(custom["canReset"])
        for source_id in ("pentest", "dsh-purge"):
            item = catalog[source_id]
            self.assertTrue(item["editable"], source_id)
            self.assertFalse(item["deletable"], source_id)
            self.assertFalse(item["custom"], source_id)
            self.assertFalse(item["canReset"],
                             "没覆盖过的来源预设没有可恢复的内容：%s" % source_id)

    def test_an_overridden_builtin_flips_canReset_but_not_deletable(self):
        """覆盖过的内置档：三个标记各自的含义必须分清。

        `canReset` 为真表示"有一层改动可以撤掉"，`deletable` 仍为假表示"这一档本身
        不能从目录里拿走"。两者混成一个标记，界面就会给内置档画一颗删除按钮，而它
        点下去只能是一句拒绝。
        """
        rows = [{"id": "pentest", "label": "我改过的渗透档", "prompt": "改过的正文。",
                 "steps": 99, "skills": [], "isOverride": True, "baseRev": "deadbeef"}]
        catalog = {item["id"]: item for item in agents.catalog(
            "", fake_snapshot(), rows)}
        item = catalog["pentest"]
        self.assertTrue(item["editable"])
        self.assertTrue(item["edited"])
        self.assertTrue(item["canReset"])
        self.assertFalse(item["deletable"])
        self.assertEqual(item["steps"], 99, "覆盖值必须生效")
        self.assertEqual(item["baseLabel"], "渗透测试模式",
                         "原件名称要留着，界面靠它说清'原件是哪一个'")
        self.assertEqual(item["baseSteps"], 6, "原件步数同理")
        self.assertTrue(item["baseChanged"], "指纹对不上就是上游动过")

    def test_a_plugin_called_custom_gets_no_delete_button(self):
        """分类读 `kind`，不拿 `source` 比字符串——`source` 装的是插件的名字。

        插件名来自第三方、本平台不校验。用 `source == "custom"` 判断，一个恰好叫
        `custom` 的插件就会把它带来的预设标成可删除；那颗按钮点下去必然失败（写路径
        只认这条记录来自 `agent_presets` 表的**自建**行），表现为"点了没反应"
        ——最难归因的一种表现。

        `editable` 不再参与这条判定：编辑内置/插件来源是**允许**的（改出来一层覆盖），
        所以它现在对三种来源都真，拿它当权限位已经分辨不出任何东西。仍然要分辨的是
        `deletable`。

        这个插件名还会让预设的 id 真的变成 `custom`（`_dsh_id` 取包名去掉 scope），
        所以这不是一个假设出来的名字，它是这套 id 规则下的一个合法结果。
        """
        snapshot = fake_snapshot()
        for preset in snapshot["presets"]:
            if preset["plugin"] == "dsh-purge":
                preset["plugin"] = "custom"
        catalog = {item["id"]: item for item in agents.catalog("", snapshot)}
        self.assertIn("custom", catalog, "假快照的插件名应当派生出 id `custom`")
        hostile = [item for item in catalog.values() if item["kind"] == "plugin"]
        self.assertTrue(hostile, "假快照里应当有插件来源的预设")
        for item in hostile:
            self.assertFalse(item["deletable"], item["id"])
            self.assertFalse(item["custom"], item["id"])
            self.assertTrue(item["editable"], item["id"])

    def test_kind_is_never_confused_with_the_plugin_name(self):
        """三条来源的分类是固定的三个值，`source` 才是自由文本。"""
        for snapshot in (fake_snapshot(), None):
            for item in agents.catalog("", snapshot, self.rows()):
                self.assertIn(item["kind"], ("builtin", "custom", "plugin"),
                              item["id"])
        catalog = {item["id"]: item for item in agents.catalog(
            "", fake_snapshot(), self.rows())}
        self.assertEqual(catalog["pentest"]["kind"], "builtin")
        self.assertEqual(catalog["dsh-purge"]["kind"], "plugin")
        self.assertEqual(catalog["custom-lateral"]["kind"], "custom")
        # 插件的 `source` 是插件名——正因为它是自由文本，才不能拿来当分类
        self.assertEqual(catalog["dsh-purge"]["source"], "dsh-purge")
        self.assertEqual(catalog["pentest"]["source"], "builtin")

    def test_a_hand_built_row_without_kind_stays_uneditable(self):
        """缺 `kind` 时保守：少一个按钮是小事，多一个点不动的按钮会让人以为界面坏了。"""
        built = agents.custom_agents(self.rows())[0]
        built.pop("kind", None)
        item = agents.public(built)
        self.assertFalse(item["editable"])
        self.assertFalse(item["deletable"])
        self.assertFalse(item["custom"])
        self.assertEqual(item["kind"], "")

    def test_public_never_carries_the_prompt(self):
        for item in agents.catalog("", fake_snapshot(), self.rows()):
            self.assertNotIn("prompt", item, item["id"])
            self.assertNotIn("persona", item, item["id"])

    def test_no_custom_rows_keeps_the_old_shape(self):
        """不传自定义时目录与改动前完全一致——既有调用方与测试不因此变化。"""
        self.assertEqual([item["id"] for item in agents.all_agents(fake_snapshot())],
                         [item["id"] for item in
                          agents.all_agents(fake_snapshot(), None)])

    def test_resolve_falls_back_when_a_preset_was_deleted(self):
        """读路径必须容忍：会话钉着的预设被删掉之后，那个会话还要能打开。"""
        fallback = agents.resolve("custom-gone", fake_snapshot(), self.rows())
        self.assertEqual(fallback["id"], "pentest")
        self.assertIsNone(agents.get("custom-gone", fake_snapshot(), self.rows()))


# -------------------------------------------------------------- 手册清单收窄

class EffectiveSkillsTestCase(unittest.TestCase):
    """`agent["skills"]` 以前是**只写不读**的，而且写进去的是插件包名。

    这里把两件事一起钉住：规则本身，以及插件预设声明的手册名是**真的手册名**。
    """

    def catalog(self):
        return [
            {"name": "chisel-tunnel", "source": "dsh-purge"},
            {"name": "webshell-toolkit", "source": "dsh-purge"},
            {"name": "recon-pipeline", "source": "@howmp/dsh-pentest"},
            {"name": "my-own-thing", "source": "本地"},
        ]

    def test_empty_declaration_means_everything(self):
        self.assertEqual(len(agents.effective_skills({"skills": []}, self.catalog())), 4)

    def test_declared_list_narrows_the_catalog(self):
        names = agents.effective_skills({"skills": ["chisel-tunnel"]}, self.catalog())
        self.assertEqual(names, ["chisel-tunnel", "my-own-thing"])

    def test_local_manuals_are_never_narrowed_away(self):
        """使用者自己放进 `data/skills` 的册子，预设不该把它藏起来。

        藏了之后用户没法从界面上看出来少了什么——他只会发现模型不去加载自己刚写的
        那份手册，而原因在一段他以为无关的清单里。
        """
        names = agents.effective_skills({"skills": ["recon-pipeline"]}, self.catalog())
        self.assertIn("my-own-thing", names)
        self.assertNotIn("chisel-tunnel", names)

    def test_plugin_presets_declare_real_skill_names_not_the_package(self):
        catalog = {item["id"]: item for item in agents.all_agents(fake_snapshot())}
        self.assertEqual(catalog["dsh-purge"]["skills"],
                         ["chisel-tunnel", "webshell-toolkit"])
        self.assertEqual(catalog["dsh-pentest"]["skills"], ["recon-pipeline"])
        # 非集成的手册不进任何清单：列出来模型会去 load_skill，然后拿到"没有名为 X 的"
        for item in catalog.values():
            self.assertNotIn("modlens", item["skills"])

    def test_prompt_block_marks_a_narrowed_list_as_such(self):
        narrowed = agents.prompt_block({"label": "X", "prompt": "口径",
                                        "skills": ["a"]}, ["a", "b"])
        self.assertIn("本 Agent 指定的技能手册", narrowed)
        wide = agents.prompt_block({"label": "X", "prompt": "口径", "skills": []},
                                   ["a", "b"])
        self.assertIn("本 Agent 可用的技能手册", wide)


# ------------------------------------------------------------------ 存储层

class PresetStoreTestCase(unittest.TestCase):

    def setUp(self):
        self.dir, self.store = support.temporary_store("presets.db")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def save(self, preset_id="custom-lateral", **overrides):
        kwargs = {"label": "内网横向", "prompt": "横向优先。", "summary": "s",
                  "note": "n", "steps": 14, "skills": ["chisel-tunnel"]}
        kwargs.update(overrides)
        return self.store.save_agent_preset(preset_id, **kwargs)

    def test_round_trip(self):
        created = self.save(derived_from="pentest", user_id="U-1")
        self.assertEqual(created["id"], "custom-lateral")
        self.assertEqual(created["skills"], ["chisel-tunnel"])
        self.assertEqual(created["derivedFrom"], "pentest")
        self.assertEqual(len(self.store.list_agent_presets()), 1)

    def test_editing_does_not_move_the_row_in_the_list(self):
        """`created_at` 是排序键。编辑刷新它，卡片会在自己脚下跳位置。"""
        first = self.save("custom-a")
        self.save("custom-b")
        self.save("custom-a", label="改了名字")
        self.assertEqual([item["id"] for item in self.store.list_agent_presets()],
                         ["custom-a", "custom-b"])
        updated = self.store.get_agent_preset("custom-a")
        self.assertEqual(updated["createdAt"], first["createdAt"])
        self.assertEqual(updated["label"], "改了名字")

    def test_corrupt_skills_json_does_not_break_the_whole_list(self):
        """手工改坏过的一行不该把设置页首屏打空——那会让人连"删掉它"都做不了。"""
        self.save("custom-ok")
        self.store.conn.execute("UPDATE agent_presets SET skills='{not json' "
                                "WHERE id='custom-ok'")
        self.store.conn.commit()
        self.assertEqual(self.store.get_agent_preset("custom-ok")["skills"], [])
        self.assertEqual(len(self.store.list_agent_presets()), 1)

    def test_delete_clears_pins_and_reports_how_many(self):
        self.save()
        pinned = self.store.create_chat_session("钉住的")
        self.store.set_chat_session_config(pinned["id"], agent_id="custom-lateral")
        loose = self.store.create_chat_session("没钉的")

        result = self.store.delete_agent_preset("custom-lateral")
        self.assertTrue(result["deleted"])
        self.assertEqual(result["affectedSessions"], 1)
        self.assertIsNone(self.store.get_agent_preset("custom-lateral"))
        self.assertEqual(self.store.get_chat_session(pinned["id"])["agentId"], "")
        self.assertEqual(self.store.get_chat_session(loose["id"])["agentId"], "")

    def test_delete_refuses_while_it_is_the_global_default(self):
        """拒绝而不是顺手改默认：全局默认是一个**显式决定**，不该被删除动作代劳。"""
        from pentestd import agent_modes
        self.save()
        self.store.set_setting(agent_modes.SETTING_KEY, "custom-lateral")
        result = self.store.delete_agent_preset(
            "custom-lateral", guard_setting=agent_modes.SETTING_KEY)
        self.assertFalse(result["deleted"])
        self.assertIn("全局默认", result["reason"])
        self.assertIsNotNone(self.store.get_agent_preset("custom-lateral"),
                             "被拒的删除不得留下半个动作")

    def test_delete_of_a_missing_id_is_a_conclusion_not_an_error(self):
        result = self.store.delete_agent_preset("custom-nope")
        self.assertFalse(result["deleted"])
        self.assertEqual(result["affectedSessions"], 0)

    def test_legacy_database_gets_the_table(self):
        path = os.path.join(self.dir.name, "legacy.db")
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE chat_sessions (id TEXT PRIMARY KEY, "
                    "title TEXT DEFAULT '', user_id TEXT DEFAULT '', "
                    "engagement_id TEXT DEFAULT '', created_at REAL, updated_at REAL)")
        raw.execute("PRAGMA user_version=6")
        raw.commit()
        raw.close()
        legacy = server.Store(path)
        try:
            self.assertEqual(legacy.list_agent_presets(), [])
            self.assertEqual(int(legacy.conn.execute(
                "PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)
        finally:
            legacy.close()


# ------------------------------------------------------------------ 写路径门禁

class NonAdminTestCase(unittest.TestCase):
    """非管理员**只读可见**，写路径全部 403。

    预设是全局共享目录：一个人改了，别人的下一次对话就跟着变。这与"我在自己的会话里
    选哪个"是两件事，后者任何人都能做。
    """

    @classmethod
    def setUpClass(cls):
        cls.dir, cls.store = support.temporary_store("presets-auth.db")
        cls.httpd, cls.admin = support.start(cls.store, WEB_DIR)
        status, payload = cls.admin.post("/api/v1/users", {
            "username": "viewer", "password": "Viewer-Pass-123", "role": "operator"})
        if status != 201:
            raise RuntimeError("建测试用户失败：%s %s" % (status, payload))
        base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        cls.viewer = support.Client(base)
        if cls.viewer.login("viewer", "Viewer-Pass-123")[0] != 200:
            raise RuntimeError("测试用户登录失败")

    @classmethod
    def tearDownClass(cls):
        support.stop(cls.httpd, cls.store, cls.dir)

    def test_operator_can_read_the_catalog(self):
        status, payload = self.viewer.get("/api/v1/agents")
        self.assertEqual(status, 200)
        self.assertTrue(payload["agents"])

    def test_every_write_path_is_admin_only(self):
        cases = [("/api/v1/agents/presets", VALID),
                 ("/api/v1/agents/presets/custom-lateral",
                  {"label": "偷改"}),
                 ("/api/v1/agents/presets/custom-lateral/delete", {})]
        for path, body in cases:
            status, _payload = self.viewer.post(path, body)
            self.assertEqual(status, 403, path)
        status, _payload = self.viewer.get("/api/v1/agents/presets/custom-lateral")
        self.assertEqual(status, 403, "预设正文也不该给非管理员")
        # 被拒的请求不得留下任何东西
        self.assertEqual(self.store.list_agent_presets(), [])


# ------------------------------------------------------------------ 真起服务

class PresetHttpTestCase(unittest.TestCase):
    """走真实 HTTP：路由、CSRF、状态码、连带后果。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("presets-http.db")
        self.httpd, self.client = support.start(self.store, WEB_DIR)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.dir)

    def create(self, **overrides):
        body = dict(VALID)
        body.update(overrides)
        return self.client.post("/api/v1/agents/presets", body)

    def test_create_then_appears_in_the_catalog(self):
        status, payload = self.create()
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["preset"]["id"], "custom-lateral")
        status, catalog = self.client.get("/api/v1/agents")
        entry = [item for item in catalog["agents"]
                 if item["id"] == "custom-lateral"]
        self.assertEqual(len(entry), 1)
        self.assertTrue(entry[0]["editable"])

    def test_a_long_prompt_survives_the_whole_path(self):
        """8264 字从请求体到库里一字不少——这条是上限撤掉之后真正要守住的东西。

        长度取的是**真实触发过那个报错的数**（8264，原上限 8000）。留着它是因为
        "刚好越界"的那一条最容易被下一次调参顺手挡回去，而挡回去的表现是某个刚写完
        详尽规范的人点保存时突然 400。

        走的是真实 HTTP，所以顺带盖住路由与 JSON 编解码那两层：长度若在任一层被截断，
        库里的字数就对不上，而 `GET` 回来看到的是被截断之后的那一份——**它看起来完全
        正常**，只有数一遍才发现不对。
        """
        prompt = ("扫描前先确认授权范围；" * 800)[:8262] + "收尾"
        self.assertEqual(8264, len(prompt), "这条用例的字数本身写错了")
        status, payload = self.create(prompt=prompt)
        self.assertEqual(status, 201, payload)
        preset_id = payload["preset"]["id"]

        status, detail = self.client.get("/api/v1/agents/presets/" + preset_id)
        self.assertEqual(status, 200, detail)
        self.assertEqual(len(detail["preset"]["prompt"]), 8264,
                         "正文在某一层被截断了：读回来的不是写进去的那一份")
        self.assertEqual(detail["preset"]["prompt"], prompt)

    def test_create_requires_csrf(self):
        status, _payload = self.client.request("POST", "/api/v1/agents/presets",
                                              VALID, with_csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.store.list_agent_presets(), [])

    def test_bad_body_is_a_400_with_the_reason(self):
        status, payload = self.create(handle="Bad Handle")
        self.assertEqual(status, 400)
        self.assertIn("标识", payload.get("error", ""))

    def test_duplicate_handle_is_rejected(self):
        self.assertEqual(self.create()[0], 201)
        status, payload = self.create(label="另一个")
        self.assertEqual(status, 400)
        self.assertIn("已被占用", payload.get("error", ""))

    def test_detail_returns_the_prompt_and_the_pin_count(self):
        self.create()
        session = self.store.create_chat_session("钉住的")
        self.store.set_chat_session_config(session["id"], agent_id="custom-lateral")
        status, payload = self.client.get("/api/v1/agents/presets/custom-lateral")
        self.assertEqual(status, 200)
        self.assertEqual(payload["preset"]["prompt"], VALID["prompt"])
        self.assertEqual(payload["pinnedSessions"], 1)

    def test_update_merges_and_keeps_the_id(self):
        self.create()
        status, payload = self.client.post("/api/v1/agents/presets/custom-lateral",
                                           {"label": "改过的名字"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["preset"]["id"], "custom-lateral")
        self.assertEqual(payload["preset"]["label"], "改过的名字")
        self.assertEqual(payload["preset"]["prompt"], VALID["prompt"],
                         "只改名字不该把口径清空")

    def test_update_cannot_change_the_id_even_if_asked(self):
        self.create()
        status, payload = self.client.post("/api/v1/agents/presets/custom-lateral",
                                           {"handle": "newhandle", "label": "x"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["preset"]["id"], "custom-lateral")
        self.assertIsNone(self.store.get_agent_preset("custom-newhandle"))

    def test_builtin_ids_are_readable_and_editable_now(self):
        """内置档现在既能读正文、也能就地改——改出来的是一层**覆盖**。

        从前这两个动作都回 400（"内置来源不能就地改"）。现在它们成立，所以这条用例
        要钉的是**新的**边界：改完之后原件还在、`id` 没变、目录里仍然只有一条。
        """
        for preset_id in ("pentest", "redteam"):
            status, payload = self.client.get(
                "/api/v1/agents/presets/%s" % preset_id)
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["preset"]["prompt"], "编辑器必须拿得到正文")
            self.assertFalse(payload["preset"]["isOverride"],
                             "还没改过的内置档不是覆盖")
            status, payload = self.client.post(
                "/api/v1/agents/presets/%s" % preset_id, {"steps": 77})
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["overridden"], preset_id)
            self.assertEqual(payload["preset"]["id"], preset_id, "id 绝不能变")
            self.assertEqual(payload["preset"]["steps"], 77)
            entry = [item for item in self.client.get("/api/v1/agents")[1]["agents"]
                     if item["id"] == preset_id]
            self.assertEqual(len(entry), 1, "目录里不能出现两条同 id 的预设")
            self.assertEqual(entry[0]["steps"], 77)

    def test_builtin_and_plugin_ids_still_cannot_be_deleted(self):
        """能改不等于能删。删掉一条内置档会让它凭空消失，而使用者要的是回到原样。"""
        for preset_id in ("pentest", "redteam"):
            status, payload = self.client.post(
                "/api/v1/agents/presets/%s/delete" % preset_id, {})
            self.assertEqual(status, 400, preset_id)
            self.assertIn("恢复原始", payload.get("error", ""), preset_id)
            self.assertIn("复制为新预设", payload.get("error", ""), preset_id)
        # 一次覆盖都没有的时候，「恢复原始」也要说清"没有可恢复的内容"，
        # 而不是回一个"恢复成功"——那句话会让使用者去查一条不存在的改动。
        status, payload = self.client.post(
            "/api/v1/agents/presets/pentest/reset", {})
        self.assertEqual(status, 409, payload)
        self.assertIn("没有被改过", payload.get("error", ""))

    def test_reset_removes_the_override_and_keeps_the_sessions(self):
        """「恢复原始」撤的是那层覆盖，**不动钉在这一档上的会话**。

        清会话固定那一套是"删掉一条预设"的连带后果（钉住的 id 要没了）。覆盖的删除
        不是那种事：那个 id 仍然解析得出来（回到内置口径），所以把会话改成"跟随默认"
        会顺手删掉使用者做过的选择——一个没人要求过的副作用。
        """
        session = self.store.create_chat_session("钉在内置档上的")
        self.store.set_chat_session_config(session["id"], agent_id="pentest")
        before = self.store.get_chat_session(session["id"])["agentId"]
        self.client.post("/api/v1/agents/presets/pentest", {"steps": 77})
        status, payload = self.client.post(
            "/api/v1/agents/presets/pentest/reset", {})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["reset"])
        self.assertEqual(payload["affectedSessions"], 0)
        self.assertEqual(payload["steps"], 6, "恢复之后步数回到内置档的值")
        self.assertEqual(self.store.get_agent_preset("pentest"), None,
                         "覆盖行必须真的被删掉")
        self.assertEqual(self.store.get_chat_session(session["id"])["agentId"], before,
                         "会话的 Agent 选择不能因为撤一层覆盖就没了")
        entry = [item for item in self.client.get("/api/v1/agents")[1]["agents"]
                 if item["id"] == "pentest"][0]
        self.assertIn("渗透", entry["label"])
        self.assertFalse(entry["edited"], "恢复之后不该还挂着'已覆盖'")
        # 再点一次：没有覆盖可撤，必须明说，不能假装成功
        self.assertEqual(self.client.post(
            "/api/v1/agents/presets/pentest/reset", {})[0], 409)

    def test_reset_refuses_on_a_self_built_preset(self):
        """自建预设没有"原始口径"——它自己就是原件。回 409 并指向正确的动作。"""
        self.create()
        status, payload = self.client.post(
            "/api/v1/agents/presets/custom-lateral/reset", {})
        self.assertEqual(status, 409, payload)
        self.assertIn("自建预设", payload.get("error", ""))
        self.assertIsNotNone(self.store.get_agent_preset("custom-lateral"))

    def test_an_override_can_be_edited_twice_without_losing_fields(self):
        """第二次编辑的合并起点是**当前生效值**，不是原件。

        起点若取原件，"只改名字"会把上一次改的步数悄悄退回内置的 6——而使用者刚刚
        才把它改成 77，他会以为保存把它弄回去了。
        """
        self.client.post("/api/v1/agents/presets/pentest", {"steps": 77})
        status, payload = self.client.post(
            "/api/v1/agents/presets/pentest", {"label": "我的渗透档"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["preset"]["label"], "我的渗透档")
        self.assertEqual(payload["preset"]["steps"], 77,
                         "只改名字不该把步数退回原件")
        status, payload = self.client.get("/api/v1/agents/presets/pentest")
        self.assertTrue(payload["preset"]["isOverride"])
        self.assertEqual(payload["preset"]["prompt"],
                         agent_modes.default_mode()["prompt"],
                         "没动过的正文仍然是原件那一份")

    def test_delete_a_missing_custom_id_is_404(self):
        status, _payload = self.client.post(
            "/api/v1/agents/presets/custom-nope/delete", {})
        self.assertEqual(status, 404)

    def test_delete_reports_the_sessions_it_moved(self):
        self.create()
        session = self.store.create_chat_session("钉住的")
        self.store.set_chat_session_config(session["id"], agent_id="custom-lateral")
        status, payload = self.client.post(
            "/api/v1/agents/presets/custom-lateral/delete", {})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["deleted"])
        self.assertEqual(payload["affectedSessions"], 1)
        self.assertEqual(self.store.get_chat_session(session["id"])["agentId"], "")

    def test_delete_is_refused_while_it_is_the_default(self):
        self.create()
        self.assertEqual(
            self.client.post("/api/v1/agents/activate",
                             {"id": "custom-lateral"})[0], 200)
        status, payload = self.client.post(
            "/api/v1/agents/presets/custom-lateral/delete", {})
        self.assertEqual(status, 409, payload)
        self.assertIn("全局默认", payload.get("error", ""))
        self.assertIsNotNone(self.store.get_agent_preset("custom-lateral"))

    def test_a_deleted_preset_can_be_recreated_without_resurrecting_the_default(self):
        """删掉再建同名，全局默认必须**还是内置那一档**。

        如果删除时把悬空的默认值留在 `app_settings` 里，重新创建同名预设会让它
        "复活"成默认——而用户在这两次操作之间什么都没选择过。这就是拒绝删除而不是
        顺手改默认的理由。
        """
        from pentestd import agent_modes
        self.create()
        self.client.post("/api/v1/agents/activate", {"id": "custom-lateral"})
        self.client.post("/api/v1/agents/activate", {"id": "redteam"})
        self.assertEqual(self.client.post(
            "/api/v1/agents/presets/custom-lateral/delete", {})[0], 200)
        self.client.post("/api/v1/agents/presets", VALID)
        self.assertEqual(self.store.get_setting(agent_modes.SETTING_KEY), "redteam")
        self.assertEqual(self.client.get("/api/v1/agents")[1]["activeId"], "redteam")

    def test_clone_carries_the_posture_the_browser_never_saw(self):
        """从内置复制：正文由**服务端**搬过去，浏览器只拿到 id。

        这条同时钉住路由顺序：`/presets/clone` 若排在 `/presets/{preset_id}` 之后，
        这个请求会被当成"改一条 id 叫 clone 的预设"，返回 404——而 404 看起来像
        "克隆接口没写"，最容易往错的方向查。
        """
        from pentestd import agent_modes
        source = agent_modes.default_mode()
        status, payload = self.client.post("/api/v1/agents/presets/clone",
                                          {"from": source["id"]})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["clonedFrom"], source["id"])
        self.assertEqual(payload["preset"]["id"], "custom-pentest")
        self.assertEqual(payload["preset"]["prompt"], source["prompt"],
                         "正文必须一字不差地搬过来，否则复制出来是一份空口径的预设")
        self.assertEqual(payload["preset"]["steps"], source["steps"])
        self.assertIn("副本", payload["preset"]["label"])
        self.assertEqual(payload["preset"]["derivedFrom"], source["id"])

    def test_clone_twice_does_not_collide(self):
        for expected in ("custom-pentest", "custom-pentest-2"):
            status, payload = self.client.post("/api/v1/agents/presets/clone",
                                              {"from": "pentest"})
            self.assertEqual(status, 201, payload)
            self.assertEqual(payload["preset"]["id"], expected)

    def test_clone_a_custom_preset_drops_the_prefix(self):
        self.create()
        status, payload = self.client.post("/api/v1/agents/presets/clone",
                                          {"from": "custom-lateral"})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["preset"]["id"], "custom-lateral-2")
        self.assertEqual(payload["preset"]["steps"], VALID["steps"])
        self.assertEqual(payload["preset"]["skills"], VALID["skills"])

    def test_clone_accepts_an_explicit_label_and_rejects_unknown_sources(self):
        status, payload = self.client.post("/api/v1/agents/presets/clone",
                                          {"from": "pentest", "label": "我的一档"})
        self.assertEqual(status, 201)
        self.assertEqual(payload["preset"]["label"], "我的一档")
        status, _payload = self.client.post("/api/v1/agents/presets/clone",
                                           {"from": "not-a-real-agent"})
        self.assertEqual(status, 404)
        status, _payload = self.client.post("/api/v1/agents/presets/clone", {})
        self.assertEqual(status, 404)

    def test_clone_handle_derivation_is_one_authority(self):
        """标识规则只有一处权威——拆两处就会某个入口建出来点开 404。"""
        self.assertEqual(agents.clone_handle("pentest", []), "pentest")
        self.assertEqual(agents.clone_handle("custom-lateral", []), "lateral")
        self.assertEqual(agents.clone_handle("dsh-purge", []), "dsh-purge")
        self.assertEqual(agents.clone_handle("pentest", ["custom-pentest"]),
                         "pentest-2")
        self.assertEqual(agents.clone_handle("", []), "preset")
        self.assertEqual(agents.clone_handle("--", []), "preset")
        # 长名字撞名时后缀要占得住位，不能截成一模一样的串（那样会死循环）
        base30 = "a" * 30
        picked = agents.clone_handle("a" * 40, ["custom-" + base30])
        self.assertTrue(agents._HANDLE_RE.match(picked), picked)
        self.assertNotEqual(picked, base30)
        self.assertTrue(picked.endswith("-2"), picked)

    def test_created_preset_drives_a_session(self):
        """端到端：建预设 → 钉到会话 → 会话配置里的步数来自这个预设。"""
        self.create()
        session = self.store.create_chat_session("钉住的")
        status, payload = self.client.post(
            "/api/v1/chat/sessions/%s/config" % session["id"],
            {"agentId": "custom-lateral"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["config"]["agentId"], "custom-lateral")
        self.assertTrue(payload["config"]["agentPinned"])
        self.assertEqual(payload["config"]["steps"], 14,
                         "步数必须来自预设，不是内置的 6/10")


# ------------------------------------------------------- 口径真的发到模型面前

class PresetTurnTestCase(unittest.TestCase):
    """前几组验"配置对了"。这一组验"生效了"——两者之间正是最容易断掉的地方。

    界面显示的系统提示、`/chat/tools` 的预览、真正发出去的那一份，是三处独立算出来的
    东西。只对其中一处做断言的话，"预览里是 14 步、实际跑 6 步"这种偏差不会被发现。
    """

    POSTURE = "以横向移动为主线，每一步标出当前所处网段与可达边界。"

    def setUp(self):
        self.dir, self.store = support.temporary_store("presets-turn.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False,
                                         probe_scope=["127.0.0.0/8"])
        self.store.save_agent_preset(
            "custom-lateral", "内网横向专项", self.POSTURE, "说明", "场合", 14,
            ["chisel-tunnel", "webshell-toolkit"], derived_from="pentest")
        self.models = []

    def tearDown(self):
        for model in self.models:
            model.stop()
        self.store.close()
        self.dir.cleanup()

    def test_custom_posture_and_step_budget_reach_the_model(self):
        model = ScriptedModel(lambda record: text_reply("openai_compat", "收到。"))
        self.models.append(model)
        profile = {"provider": "openai_compat",
                   "baseUrl": model.base_url + "/v1", "model": "fake-1",
                   "temperature": 0.2, "maxTokens": 512, "timeoutS": 20}
        result = chat.run_turn(self.plane, profile, "k",
                               [{"role": "user", "text": "开始"}],
                               agent=self.plane.resolve_agent("custom-lateral"))
        self.assertTrue(result["ok"], result.get("message"))
        sent = model.requests[-1]["body"]["messages"][0]["content"]
        self.assertIn(self.POSTURE, sent, "自定义口径必须真的进系统提示")
        self.assertIn("内网横向专项", sent)

    def test_the_prompt_block_lists_only_the_declared_manuals(self):
        """确定性的那一半：拿一份合成目录走 `effective_skills` → `prompt_block`。

        "本地手册不被收窄"由 `EffectiveSkillsTestCase` 单独钉住；这里看的是
        **两段拼起来之后**模型读到的那一节长什么样——清单和它的标题必须一致，
        标题说"指定的"却列出全部，模型就会以为那几份是全部可用的。
        """
        items = [{"name": "chisel-tunnel", "source": "dsh-purge"},
                 {"name": "webshell-toolkit", "source": "dsh-purge"},
                 {"name": "recon-pipeline", "source": "@howmp/dsh-pentest"},
                 {"name": "my-own", "source": "本地"}]
        chosen = self.plane.resolve_agent("custom-lateral")
        self.assertEqual(chosen["skills"], ["chisel-tunnel", "webshell-toolkit"])
        block = agents.prompt_block(chosen, agents.effective_skills(chosen, items))
        self.assertIn("本 Agent 指定的技能手册", block)
        for name in ("chisel-tunnel", "webshell-toolkit", "my-own"):
            self.assertIn(name, block)
        self.assertNotIn("recon-pipeline", block,
                         "没声明的手册不该出现在这个预设的清单里")

    def test_the_preview_and_the_real_prompt_agree(self):
        """`/chat/tools` 的预览与真正下发的那一份必须同源。

        预览看的是**当前全局默认**预设，所以先把自定义那条切成默认——不切的话
        这条用例测的是内置档，`maxSteps` 会得到 6，而它看起来"也能过一半"。
        """
        from pentestd import agent_modes
        self.store.set_setting(agent_modes.SETTING_KEY, "custom-lateral")
        status, payload = self.plane.chat_tools({"user": None, "body": {},
                                                 "params": {}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["maxSteps"], 14)
        self.assertIn(self.POSTURE, payload["systemPrompt"])
        self.assertEqual(payload["agentMode"]["id"], "custom-lateral")
        entry = [item for item in payload["agents"] if item["id"] == "custom-lateral"]
        self.assertEqual(len(entry), 1, "自定义预设必须出现在同一份目录里")


if __name__ == "__main__":
    unittest.main()
