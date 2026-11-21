# -*- coding: utf-8 -*-
"""缺陷报告 DSH-PT-2026-1015 里**在磁盘上仍然成立**的那几条的守卫。

报告一共 22 条。其中有六条在本文件写下的时刻已经修好并有各自的守卫
（D4/D5 长输出与任务列表在 `test_chat_paging.py`，D6 模板缺省、D8 目标身份、
D11 编码、D12 Npcap 降级在 `test_defect_regressions.py`），这里只收剩下的那批，
**一条缺陷一个 TestCase**，编号与报告一致，方便对着报告核。

| 编号 | 缺陷 | 这里守它的是 |
|---|---|---|
| D1 | 技能手册库为空，整条技能链路不可用 | `SkillCatalogEmptyTestCase` |
| D2 | 工具缺口与自动渠道零交集，`install_tool` 无目标可打 | `InstallChannelTestCase` |
| D3 | SSH 口令测试闭环断裂，责任在四处之间推诿 | `CredentialProtocolGapTestCase` |
| D7 | 子智能体 4 次模型调用，派活产出大面积空转 | `ChildBudgetTestCase` |
| D9 | 资产去重失败，资产图随重跑无限膨胀 | `AssetIdentityTestCase` |
| D10 | 报告给出的复现步骤依赖未安装的工具 | `ReproStepsTestCase` |
| D13 | 子智能体持有全套侵入式动作 | `ChildIntrusiveTestCase` |
| D14 | `delegate` 的 objective 长度上限未文档化 | `DelegateSchemaTestCase` |
| D15/D16 | 心跳字段失效、`load` 恒为 0 | `ExecutorTelemetryTestCase` |
| D17 | 截断行为在同一组读取接口内不一致 | `PagingConsistencyTestCase` |
| D19 | 字符集校验拒绝 `curl -A` | `CurlUserAgentTestCase` |

全部**离线**：不联网、不下载、不依赖本机装了哪个工具（需要"本机装了什么"的地方
一律注入）。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import chat, creds, engine, policy, probe, skills, subagent   # noqa: E402
from pentestd import server, toolinstall, toolkit                          # noqa: E402
from pentestd.store import Store                                          # noqa: E402
from tests import support                                                 # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


class _TempFixture(unittest.TestCase):
    """一个临时库 + 一个控制面。子类的 setUp 只管造数据。"""

    name = "defects.db"

    def setUp(self):
        self.directory, self.store = support.temporary_store(self.name)
        self.plane = server.ControlPlane(self.store, WEB_DIR,
                                         require_executor_auth=False)
        self.plane.data_dir = self.directory.name
        self.eid = self.store.create_engagement("10.10.10.250", "缺陷回归",
                                                "工单 #47")["id"]

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def call(self, name, args=None, eid=""):
        return chat.execute_tool(self.plane, name, args or {}, eid or self.eid,
                                 policy_id="full")


# ============================================================ D1

class SkillCatalogEmptyTestCase(_TempFixture):
    """D1 空目录必须说成空目录，而不是给一句必然扑空的下一步。"""

    name = "skills.db"

    def test_an_empty_catalog_says_so_and_says_why(self):
        """此前不论有没有东西，提示都是"用 load_skill 装载其中一份"。

        一个空目录配上这句话，读的人会照着走：去 `load_skill`、拿到"没有名为
        recon 的技能手册"，然后**没有理由**相信"本部署根本没有技能包"这个更可能
        的解释。系统提示里那句"先 list_skills 看目录"因此整条落空。
        """
        out = self.call("list_skills")
        self.assertIn("0", out["summary"])
        self.assertIn("没有任何技能手册", out["summary"])
        source = out["data"]["catalogSource"]
        self.assertEqual(0, source["localCount"])
        self.assertEqual(0, source["pluginCount"])
        self.assertIn("localDir", source)
        self.assertNotIn("用 load_skill 装载", out["data"]["hint"])
        self.assertIn("不要", out["data"]["hint"])

    def test_a_missing_skill_name_points_back_at_the_truth(self):
        """目录为空时 `load_skill` 的失败信息不该只说"没有这份手册"。"""
        out = self.call("load_skill", {"name": "recon"})
        self.assertFalse(out["ok"])
        self.assertIn("recon", out["summary"])

    def test_a_local_handbook_is_discovered_and_loadable(self):
        """真正的通道是 `data/skills/`。写一份进去，它必须出现在目录里、正文取得到。"""
        base = os.path.join(self.directory.name, "skills")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "probe-basics.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: probe-basics\ndescription: 探测基线\n"
                     "whenToUse: 第一次接触一个网段\nrole: recon\n---\n\n"
                     "正文第一行。\n")
        skills.invalidate()
        out = self.call("list_skills")
        names = [item["name"] for item in out["data"]["skills"]]
        self.assertIn("probe-basics", names)
        self.assertEqual(1, out["data"]["catalogSource"]["localCount"])
        loaded = self.call("load_skill", {"name": "probe-basics"})
        self.assertIn("正文第一行", loaded["data"]["body"])

    def test_the_shipped_catalog_is_not_empty(self):
        """随仓库发出去的那一套不能是空的——否则上面那些守卫只是把 0 说清楚。

        读的是**真实的** `data/skills`（不是临时目录）：这一条守的正是
        "交付时技能层到底有没有东西"。
        """
        items = skills.catalog({}, os.path.join(ROOT, "data"))
        self.assertTrue(items, "data/skills 里一份手册都没有")
        for item in items:
            self.assertTrue(item["name"], "有手册没解析出 name：%s" % item["path"])
            self.assertTrue(item["chars"], "有手册正文为空：%s" % item["path"])
            self.assertIn(item["role"],
                          ("plan", "recon", "vuln-scan", "exploit", "internal", ""),
                          "%s 的 role 不在平台约定的集合里" % item["name"])


# ============================================================ D2

class InstallChannelTestCase(unittest.TestCase):
    """D2 自动渠道那一串名字必须**从配方表算出来**，不能写死。"""

    def test_the_advertised_channels_come_from_the_recipe_table(self):
        """写死的那一版列的是 nmap/curl/chisel/sqlmap，而那四个恰好全都装了。"""
        expected = {name for name, spec in toolinstall.RECIPES.items()
                    if str(spec.get("channel") or "manual") != "manual"}
        self.assertEqual(expected, set(chat._AUTO_IDS))

    def test_the_description_names_every_auto_installable_adapter(self):
        spec = [item for item in chat.TOOLS if item["name"] == "install_tool"][0]
        for name in chat._AUTO_IDS:
            self.assertIn(name, spec["description"], "%s 没出现在描述里" % name)

    def test_the_description_tells_the_caller_to_stop_when_the_gap_is_empty(self):
        """缺的项与可安装的项不相交时，唯一正确的是"别反复试"。"""
        spec = [item for item in chat.TOOLS if item["name"] == "install_tool"][0]
        self.assertIn("一个都不在", spec["description"])
        self.assertIn("别反复试", spec["description"])

    def test_the_description_says_to_install_instead_of_working_around(self):
        """反过来的那一半同样必须写出来。

        实测里模型确实这么做过：某个客户端缺着，它没去装，而是把本该用
        `openssl` 核对整条证书链的那一步换成另一条只看得到一半信息的命令，
        然后把"取不到"写进了结论。平台能装而模型不知道要装，等于装不了。
        """
        spec = [item for item in chat.TOOLS if item["name"] == "install_tool"][0]
        self.assertIn("installable", spec["description"])
        self.assertIn("就地装它", spec["description"])

    @staticmethod
    def _row(adapter_id, installed, installable, klass="scan"):
        return {"id": adapter_id, "label": adapter_id, "purpose": "夹具",
                "installed": installed, "path": "x" if installed else "",
                "source": "project" if installed else "",
                "installable": installable, "install": "见官方文档",
                "why": "", "command": [], "class": klass}

    def _toolkit_summary(self, rows):
        """`_tool_list_toolkit` 的现状只从 `toolkit.detect()` 来——换掉它即可。"""
        saved = chat.toolkit_module.detect
        chat.toolkit_module.detect = lambda *a, **k: rows
        try:
            return chat._tool_list_toolkit(None, {}, "", None)
        finally:
            chat.toolkit_module.detect = saved

    def test_an_empty_gap_does_not_read_as_a_missing_capability(self):
        """「0 项平台可以自己装」读起来像"平台装不了东西"。

        而它真实的意思常常是"能装的都装好了"——这两句话的下一步动作恰好相反
        （放弃 vs. 去装别的）。使用者读到的就是这一句总览，他就是据此判断
        "这个平台到底能不能自己装工具"的，所以可装项为空时必须点名**已经装好的
        那几个**，而不是只报一个 0。
        """
        rows = [self._row("nmap", True, True),
                self._row("openssl", False, False, klass="toolkit")]
        out = self._toolkit_summary(rows)
        self.assertIn("nmap", out["summary"], "能装的都装好了，就该点名是哪一个")
        self.assertIn("已经装好了", out["summary"])
        self.assertNotIn("0 项", out["summary"])
        self.assertEqual(["openssl"], out["data"]["unavailable"])

    def test_a_real_gap_names_the_adapter_that_can_be_installed(self):
        rows = [self._row("nmap", True, True),
                self._row("openssl", False, True, klass="toolkit")]
        out = self._toolkit_summary(rows)
        self.assertIn("openssl", out["summary"])
        self.assertIn("install_tool", out["summary"])
        self.assertEqual(["openssl"], out["data"]["installable"])
        self.assertEqual(["openssl"],
                         [item["id"] for item in out["data"]["missing"]])

    def test_an_adapter_without_a_channel_reports_why(self):
        """**唯一没有渠道的**那个要给出**具体到这一条**的原因。

        用 `ssh` 而不是 `hydra`：hydra 现在有 wsl 渠道了（本机上还已经装好），
        于是它走的是"已经装在项目目录里了"那条分支——那也是一句真话，但它回答
        的不是这条用例问的问题。`ssh` 由 Windows 的可选组件提供，平台不去装它，
        所以它是"没有渠道"那条分支上唯一还在的样本。

        直接调 handler 而不是走 `execute_tool`：`install_tool` 只有管理员能调，
        而这条用例问的是"没有渠道时它说什么"，不是邀请闸门本身。
        """
        out = _temp_plane_call("install_tool", {"adapter": "ssh", "dryRun": True},
                               via_policy=False)
        self.assertIn("没有自动安装渠道", out["summary"])
        self.assertFalse(out["data"]["installable"])
        self.assertTrue(out["data"]["reason"])

    def test_the_admin_only_gate_is_still_there(self):
        """放宽的是措辞，不是邀请闸门——普通会话仍然拿不到它。"""
        out = _temp_plane_call("install_tool", {"adapter": "hydra", "dryRun": True})
        self.assertFalse(out["ok"])
        self.assertIn("管理员", out["summary"])


def _temp_plane_call(name, args, via_policy=True):
    directory, store = support.temporary_store("install.db")
    try:
        plane = server.ControlPlane(store, WEB_DIR, require_executor_auth=False)
        plane.data_dir = directory.name
        if not via_policy:
            return chat.HANDLERS[name](plane, dict(args), "", {})
        return chat.execute_tool(plane, name, args, "", policy_id="full")
    finally:
        store.close()
        directory.cleanup()


# ============================================================ D3

class CredentialProtocolGapTestCase(unittest.TestCase):
    """D3 闭环断在哪，必须一句话说得清，而不是把责任推给下一个工具。"""

    def _without_credential_clients(self):
        """把"两个口令客户端都没装"这件事**固定住**，返回原来的 detect 供还原。"""
        original = creds.toolkit.detect
        creds.toolkit.detect = lambda refresh=False: [
            {"id": "hydra", "source": "", "installable": True,
             "note": "", "install": ""},
            {"id": "ncrack", "source": "", "installable": True,
             "note": "", "install": ""}]
        return original

    @staticmethod
    def _every_delegated_client():
        """`DELEGATED_PROTOCOLS` 里点名的**每一个**客户端 id，去重保序。"""
        seen = []
        for spec in creds.DELEGATED_PROTOCOLS.values():
            for name in spec["clients"]:
                if name not in seen:
                    seen.append(name)
        return seen

    def _stub_clients(self, installed):
        """把 `detect` 固定成"这些客户端装齐了 / 一个都没有"，返回原函数供还原。

        ⚠️ **必须覆盖 `DELEGATED_PROTOCOLS` 里的每一个客户端。** 这个类原先只固定
        `hydra` + `ncrack`，于是 SMB（`netexec` / `smbclient`）与 WinRM（`evil-winrm`）
        仍然算缺 —— `blocked` 非空，句子走的是**另一支**。症状是"用例名说「客户端
        装好了」，实际测的是「有客户端没装」那一支"，而它对那一支的断言恰好也成立
        （"没有任何可用客户端"里就含"可用"两个字），所以一直是绿的。

        后果不是"少一条覆盖"，是**分支 A 一条覆盖都没有**：那句"全都有客户端可用"
        少一个去处、没人能发现，直到全平台的客户端真被装上、`test_credentials` 在
        真实部署上走进那一支才暴露出来。

        这里改成**从表里推**：`DELEGATED_PROTOCOLS` 新增一个协议时，桩会跟着长，
        不会再悄悄退回另一支。
        """
        rows = [{"id": name,
                 "source": ("/usr/bin/%s" % name) if installed else "",
                 "installable": not installed, "note": "", "install": ""}
                for name in self._every_delegated_client()]
        original = creds.toolkit.detect
        creds.toolkit.detect = lambda refresh=False: rows
        return original

    def test_the_gap_sentence_names_the_protocols_and_the_clients(self):
        """**先把"都没装"固定住**，再断言它会点名。

        不固定的话，这条用例的结论取决于**跑它的那台机器上装了什么**：本机
        的 hydra / ncrack 装好之后，句子会正确地变成"用已自动装好的客户端
        可用"（见下面那条），而"必须点名"就成了一句假话——它测的是机器，
        不是代码。
        """
        original = self._without_credential_clients()
        try:
            text = creds.gap_sentence()
        finally:
            creds.toolkit.detect = original
        for protocol in ("SSH", "SMB", "RDP", "WinRM"):
            self.assertIn(protocol, text, "没提到 %s" % protocol)
        self.assertIn("ncrack", text)
        self.assertIn("hydra", text)

    def test_when_every_client_is_installed_it_does_not_name_them(self):
        """反过来的一半：装齐了就不该再点名，但**仍然要给出去处**。

        两句话指向**相反的动作**——一句让人去装，一句让人直接用。装齐了还说
        前一句，使用者会去重复一个已经完成的动作。

        "仍然要给出去处"是补上的：这条用例原先只断 `assertIn("可用", text)`，
        而那正是漏掉缺陷的地方 —— 分支 A 承诺了能力、却没给地址，而"可用"两个字
        在**两支里都成立**，所以它绿的毫无信息量。

        ⚠️ 客户端必须**装齐**（`_stub_clients` 从表里推），否则句子仍在走分支 B，
        这条用例断的就是另一支了 —— 它此前就是这么绿的。
        """
        original = self._stub_clients(installed=True)
        try:
            text = creds.gap_sentence()
        finally:
            creds.toolkit.detect = original
        self.assertNotIn("没有任何可用客户端", text,
                         "装齐之后句子仍在走分支 B —— 这条用例没有覆盖到分支 A：%s"
                         % text)
        self.assertIn("可用", text)
        # "不点名"：装好了还写 hydra / ncrack，就是让人去重复一个已经完成的动作
        self.assertNotIn("hydra", text)
        self.assertNotIn("ncrack", text)
        # "但要有去处"：必须点出至少一条真入口
        self.assertTrue(
            any(entry in text for entry in ("list_toolkit", "run_tool", "install_tool")),
            "装了客户端之后这一支没有点出任何一条真入口：%s" % text)

    def test_both_branches_name_a_reachable_entry_point(self):
        """**不变量**：不管客户端装没装，那句话都必须点出一条真入口。

        上面几条各自固定一种部署状态；这一条把两支合成一个判据 —— 任何一支退回
        "只承诺能力、不给地址"，这里都红。分开写的时候容易只守住其中一支，而这次
        漏掉的恰好是另一支（分支 A）。

        每一支都先断言**自己确实走到了那一支**：不这样的话，"都装了"那个桩一旦
        漏掉某个客户端，句子会退回分支 B，而分支 B 有去处、断言照样过 —— 覆盖
        静默消失，看起来还是绿的。这正是上一版的失败方式。
        """
        states = {"都没装": (False, "没有任何可用客户端"),
                  "都装齐": (True, "各自都有已安装的客户端可用")}
        for label, (installed, marker) in states.items():
            original = self._stub_clients(installed=installed)
            try:
                text = creds.gap_sentence()
            finally:
                creds.toolkit.detect = original
            with self.subTest(state=label):
                self.assertIn(marker, text,
                              "%s：句子没有走到预期的那一支（找 %r）：%s"
                              % (label, marker, text))
                self.assertTrue(
                    any(entry in text for entry in
                        ("list_toolkit", "run_tool", "install_tool")),
                    "%s 那一支没有点出任何一条真入口：%s" % (label, text))

    def test_the_gap_sentence_is_honest_about_what_it_is_not(self):
        """它要**明确否定**"换个参数"与"去 list_toolkit 看看"这两条假出路。

        同样先固定成"没装"：装了之后这句话整个换成另一句（"用已自动装好的
        客户端可用"），那时"否定"本来就不该在。
        """
        original = self._without_credential_clients()
        try:
            text = creds.gap_sentence()
        finally:
            creds.toolkit.detect = original
        self.assertIn("list_toolkit", text)
        self.assertIn("测不了", text)

    def test_a_node_that_has_the_client_is_not_reported_as_broken(self):
        """装了 hydra 的节点上，"这个协议测不了"就是假话——报告必须跟着变。"""
        original = creds.toolkit.detect
        try:
            creds.toolkit.detect = lambda refresh=False: [
                {"id": "hydra", "source": "/usr/bin/hydra", "installable": False,
                 "note": "", "install": ""},
                {"id": "ncrack", "source": "", "installable": False,
                 "note": "", "install": ""}]
            row = [item for item in creds.protocol_report()
                   if item["protocol"] == "ssh"][0]
            self.assertTrue(row["usable"])
        finally:
            creds.toolkit.detect = original

    def test_the_refusal_carries_the_diagnosis(self):
        """`test_credentials` 缺 driver 时的失败信息里要带上那句话。"""
        out = _temp_plane_call("test_credentials", {"target": "10.0.0.5"})
        self.assertFalse(out["ok"])
        self.assertIn("driver 不能为空", out["summary"])
        self.assertIn("SSH", out["summary"])

    def test_the_description_does_not_promise_a_working_ssh_path(self):
        spec = [item for item in chat.TOOLS if item["name"] == "test_credentials"][0]
        self.assertNotIn("SSH/SMB/RDP 需要真实协议客户端，见 list_toolkit",
                         spec["description"])
        self.assertIn("断的", spec["description"])


# ============================================================ D7 / D13

class ChildBudgetTestCase(unittest.TestCase):
    """D7 一次派活要的模型调用是 N+3，4 只够 N=1。"""

    def test_the_model_call_budget_fits_a_real_delegation(self):
        """`结论从未回到指挥官手里`的直接成因：任务下发了、额度没了。"""
        self.assertGreaterEqual(subagent.CHILD_MODEL_CALLS, 8)

    def test_one_step_per_child_is_not_enough_to_delegate(self):
        """每个孩子只摊到 1 步 = 下发完就没额度取结果——那正是要拒掉的形状。"""
        self.assertGreaterEqual(subagent.CHILD_MIN_TOOL_CHARGES, 2)

    def test_the_guard_refuses_when_each_child_cannot_afford_the_round_trip(self):
        """只够 1 步的池子上派 2 个：整次拒掉，而不是派出去然后一步跑不成。

        `run_delegation` 是生成器（它一路 yield `subagent` 事件），所以要走完
        才能拿到最后那个 return 值。这条路径在**任何模型调用之前**就返回了。
        """
        args = {"children": [{"role": "a", "objective": "x"},
                             {"role": "b", "objective": "y"}]}
        outcome, charged = _delegate(args, max_steps=3, steps=1)
        self.assertFalse(outcome["ok"])
        self.assertIn("摊不开", outcome["summary"])
        self.assertEqual(0, charged)

    def test_a_roomy_budget_is_not_refused_for_the_same_reason(self):
        """同一批子智能体，预算够了就不该被这道闸拦下——否则它拦的是"派活"本身。"""
        args = {"children": [{"role": "a", "objective": "x"}]}
        outcome, _charged = _delegate(args, max_steps=9, steps=1, dry=True)
        self.assertNotIn("摊不开", outcome.get("summary") or "")


def _delegate(args, max_steps, steps, dry=False):
    """把 `run_delegation` 这个生成器走完，拿它的返回值。

    `dry=True` 时还没有进入模型调用就走掉——测的是"预算这道闸放不放行"，
    不是"跑得好不好"。
    """
    gen = subagent.run_delegation(None, args, profile={}, api_key="",
                                  max_steps=max_steps, steps=steps,
                                  policy_id="full")
    try:
        while True:
            next(gen)
            if dry:
                gen.close()
                return {"ok": True, "summary": "已开始执行"}, 0
    except StopIteration as stop:
        return stop.value


class ChildIntrusiveTestCase(unittest.TestCase):
    """D13 利用类工具要逐项点名才给。"""

    INTRUSIVE = ("start_exploit", "deliver_payload", "test_credentials",
                 "open_pivot", "run_tool")

    def test_an_unnamed_child_gets_none_of_the_intrusive_tools(self):
        spec = subagent.Spec(role="recon", objective="看看 10.0.0.5")
        names, _dropped = subagent.resolve_tools(spec, "full", "", False)
        admitted = [name for name in self.INTRUSIVE if name in names]
        self.assertEqual([], admitted, "没点名的子智能体拿到了：%s" % admitted)

    def test_an_unnamed_child_still_gets_the_read_and_probe_tools(self):
        """收紧的是**利用**那一簇，不是把孩子的工具全拿走——那样它什么都干不了。"""
        spec = subagent.Spec(role="recon", objective="看看 10.0.0.5")
        names, _dropped = subagent.resolve_tools(spec, "full", "", False)
        for name in ("get_chain_nodes", "list_jobs", "start_probe", "run_tool"):
            if name in self.INTRUSIVE:
                continue
            self.assertIn(name, names)

    def test_naming_a_tool_hands_it_over_and_that_is_the_authorization(self):
        spec = subagent.Spec(role="verify", objective="验证 upload 点",
                             wanted=("start_exploit",))
        names, dropped = subagent.resolve_tools(spec, "full", "", False)
        self.assertIn("start_exploit", names)
        self.assertEqual([], dropped)

    def test_the_deploy_level_tools_are_refused_even_when_named(self):
        """`CHILD_FORBIDDEN` 比 `CHILD_NEEDS_NAMING` 硬一档：点名也不给。"""
        spec = subagent.Spec(role="x", objective="y",
                             wanted=("delegate", "install_plugin", "install_tool"))
        names, dropped = subagent.resolve_tools(spec, "full", "", False)
        for name in ("delegate", "install_plugin", "install_tool"):
            self.assertNotIn(name, names)
        self.assertEqual(3, len(dropped))

    def test_the_policy_still_caps_a_named_tool(self):
        """点名不是越权的另一条路——父会话的档位仍然是外层那道闸。"""
        spec = subagent.Spec(role="x", objective="y", wanted=("start_exploit",))
        names, dropped = subagent.resolve_tools(spec, "readonly", "", False)
        self.assertNotIn("start_exploit", names)
        self.assertTrue(dropped)


class DelegateSchemaTestCase(unittest.TestCase):
    """D14 长度上限要出现在 schema 里，而不是只从报错里反推。"""

    def setUp(self):
        self.spec = [item for item in chat.TOOLS
                     if item["name"] == subagent.DELEGATE_TOOL][0]
        self.props = (self.spec["parameters"]["properties"]["children"]
                      ["items"]["properties"])

    def test_the_objective_limit_is_in_the_schema(self):
        self.assertEqual(subagent.OBJECTIVE_CHARS,
                         self.props["objective"]["maxLength"])
        self.assertIn(str(subagent.OBJECTIVE_CHARS),
                      self.props["objective"]["description"])

    def test_the_role_and_note_limits_are_there_too(self):
        self.assertEqual(subagent.ROLE_CHARS, self.props["role"]["maxLength"])
        self.assertEqual(subagent.NOTE_CHARS, self.props["note"]["maxLength"])

    def test_the_schema_and_the_parser_share_one_source(self):
        """两处各写一个数字，改一处就会分叉，而分叉的症状是"照着文档写却被拒"。

        判据读 `parse_specs.__code__.co_names`（字节码里的全局名），不是
        `inspect.getsource`：后者按函数对象的行号回磁盘重读源文件，走全量套件时
        同一进程里前面某个用例写过源文件、或让 `linecache` 记住旧内容，读回来的
        就是别人的正文，于是变成"单跑必过、全套必挂"（`tests/test_toolkit_arguments.py`
        里那条 `build_command` 断言就是这么翻的）。
        """
        names = subagent.parse_specs.__code__.co_names
        for constant in ("OBJECTIVE_CHARS", "ROLE_CHARS", "NOTE_CHARS"):
            self.assertIn(constant, names,
                          "parse_specs 没有用 %s，那它就从别处拿了另一个数" % constant)

    def test_the_tools_field_documents_the_naming_rule(self):
        description = self.props["tools"]["description"]
        for name in subagent.CHILD_NEEDS_NAMING:
            self.assertIn(name, description)


# ============================================================ D9

class AssetIdentityTestCase(_TempFixture):
    """D9 同一批 specs 写两遍，资产数不能长。"""

    name = "assets.db"

    def specs(self):
        """形状取自 `probe.build_chain_specs`：根资产与存活主机是同一条 ip。"""
        return [
            {"kind": "asset", "localRef": "asset:root",
             "payload": {"type": "ip", "value": "10.10.10.250", "meta": "ports=2"}},
            {"kind": "intent", "localRef": "intent:recon",
             "payload": {"title": "测绘", "detail": "x", "goalId": "goal-1"}},
            {"kind": "asset", "localRef": "asset:host:1",
             "payload": {"type": "ip", "value": "10.10.10.250", "meta": "open=2",
                         "parentId": {"$ref": "asset:root"}}},
            {"kind": "asset", "localRef": "asset:svc:22",
             "payload": {"type": "service", "value": "22/tcp ssh",
                         "parentId": {"$ref": "asset:host:1"}}},
        ]

    def test_rerunning_the_same_probe_does_not_grow_the_asset_graph(self):
        """报告实测 63 → 67 → 68 单调增长，就是这么来的。"""
        engine.write_chain(self.store, self.eid, self.specs(), job_id="job-1")
        first = len(self.store.asset_tree(self.eid))
        engine.write_chain(self.store, self.eid, self.specs(), job_id="job-2")
        self.assertEqual(first, len(self.store.asset_tree(self.eid)))

    def test_the_root_and_the_host_do_not_become_two_identical_ips(self):
        """报告的复现证据是 `asset-1` 与 `asset-2`：同一个 IP，后者是前者的子节点。"""
        engine.write_chain(self.store, self.eid, self.specs(), job_id="job-1")
        rows = self.store.asset_tree(self.eid)
        ips = [row for row in rows if row["type"] == "ip"]
        self.assertEqual(1, len(ips), "同一个 IP 登记了 %d 条" % len(ips))
        self.assertIsNone(ips[0]["parentId"], "根资产不该有父")

    def test_the_service_hangs_off_the_surviving_host_id(self):
        """去重之后 `$ref` 必须指到**存活的**那个 id，否则资产树会悬空。"""
        engine.write_chain(self.store, self.eid, self.specs(), job_id="job-1")
        rows = self.store.asset_tree(self.eid)
        host = [row for row in rows if row["type"] == "ip"][0]
        service = [row for row in rows if row["type"] == "service"][0]
        self.assertEqual(host["id"], service["parentId"])

    def test_the_same_service_on_another_host_stays_separate(self):
        """`service` 的值（`22/tcp ssh`）不含主机——按 (type, value) 去重会塌掉一台机器。"""
        engine.write_chain(self.store, self.eid, self.specs(), job_id="job-1")
        engine.write_chain(self.store, self.eid, [
            {"kind": "asset", "localRef": "asset:root",
             "payload": {"type": "ip", "value": "10.10.10.250"}},
            {"kind": "asset", "localRef": "asset:host:2",
             "payload": {"type": "ip", "value": "10.10.10.251",
                         "parentId": {"$ref": "asset:root"}}},
            {"kind": "asset", "localRef": "asset:svc:22b",
             "payload": {"type": "service", "value": "22/tcp ssh",
                         "parentId": {"$ref": "asset:host:2"}}},
        ], job_id="job-2")
        ssh = [row for row in self.store.asset_tree(self.eid)
               if row["value"] == "22/tcp ssh"]
        self.assertEqual(2, len(ssh), "两台主机上的同名服务被塌成了一条")

    def test_reuse_is_counted_apart_from_writing(self):
        """`written` 进的是报告——"写入 68 个"与"用到 68 个、0 个是新的"是两句话。"""
        first = engine.write_chain(self.store, self.eid, self.specs(), job_id="job-1")
        second = engine.write_chain(self.store, self.eid, self.specs(), job_id="job-2")
        # specs 里一共三条资产：根 ip、存活主机 ip（与根同值）、一条 service。
        # 第一次：命中 2 条新资产，根与主机合成 1 条（后到的那条复用先到的）。
        self.assertEqual(2, first["asset"])
        self.assertEqual(1, first.get("assetReused"))
        self.assertEqual(0, second["asset"])
        self.assertEqual(3, second.get("assetReused"))

    def test_a_different_value_is_still_a_new_node(self):
        """去重不能把"新发现"一起去掉。"""
        engine.write_chain(self.store, self.eid, self.specs(), job_id="job-1")
        engine.write_chain(self.store, self.eid, [
            {"kind": "asset", "localRef": "asset:new",
             "payload": {"type": "app", "value": "10.10.10.250:8080"}}], job_id="job-2")
        values = {row["value"] for row in self.store.asset_tree(self.eid)}
        self.assertIn("10.10.10.250:8080", values)


# ============================================================ D10

class ReproStepsTestCase(unittest.TestCase):
    """D10 平台自己产出的复现步骤，平台自己得能执行。"""

    def _with_clients(self, ids, flavour="cert"):
        original = probe._installed_clients
        try:
            probe._installed_clients = lambda: set(ids)
            return probe._tls_steps("10.10.10.250", 443, flavour)
        finally:
            probe._installed_clients = original

    def test_openssl_is_used_when_it_is_actually_installed(self):
        steps = self._with_clients(["openssl", "curl"])
        self.assertIn("openssl s_client", steps[0])

    def test_curl_replaces_openssl_when_only_curl_is_available(self):
        """报告里那个部署就是这样：只有 curl、没有 openssl。"""
        steps = self._with_clients(["curl", "nmap"])
        self.assertIn("curl", steps[0])
        self.assertNotIn("openssl", steps[0])

    def test_with_neither_the_step_says_the_detail_is_unavailable(self):
        """**"取不到"与"判不了"要分开。** 没有客户端时不是让人去装一个再来，
        而是指出已有的取证（verifyError + certSha256）已经回答了核心问题。"""
        steps = self._with_clients(["nmap", "sqlmap"])
        self.assertIn("没有可用的 TLS 复核客户端", steps[0])
        self.assertIn("certSha256", " ".join(steps))

    def test_the_deprecated_version_flavour_has_its_own_wording(self):
        steps = self._with_clients(["curl"], flavour="deprecated")
        self.assertIn("tlsv1.1", " ".join(steps))

    def test_every_step_list_has_at_least_one_action_and_one_reading(self):
        """两步起：一条命令 + 一句"看什么"。只有命令的话，读的人不知道怎么判。"""
        for ids in (["openssl"], ["curl"], []):
            steps = self._with_clients(ids)
            self.assertGreaterEqual(len(steps), 2)


# ============================================================ D15 / D16

class ExecutorTelemetryTestCase(_TempFixture):
    """D15/D16 心跳字段与负载必须是能用的数。"""

    name = "executors.db"

    def test_last_seen_seconds_ago_is_a_number_for_a_live_node(self):
        self.store.ensure_executor("ut-node", ["probe"])
        self.store.heartbeat("ut-node", 0)
        row = [item for item in self.store.list_executors()
               if item["name"] == "ut-node"][0]
        self.assertIsNotNone(row["lastSeenSecondsAgo"],
                             "在线节点的心跳距今秒数是 null")
        self.assertLess(row["lastSeenSecondsAgo"], 60)

    def test_load_counts_the_jobs_that_node_is_running(self):
        """此前读的是节点自报的数，而心跳只在"领到任务那一刻"写一次——
        一次三分钟的扫描期间它一直是 0。现在现算。"""
        executor_id = self.store.ensure_executor("ut-node", ["probe"])
        self.store.heartbeat(executor_id, 0)
        job = self.store.create_job("probe", "10.0.0.5", self.eid)
        self.store.heartbeat(executor_id, 9)        # 自报 9，实际一个任务在跑
        claimed = self.store.claim_job(executor_id)
        self.assertEqual(job["id"], claimed["id"])
        row = [item for item in self.store.list_executors()
               if item["name"] == "ut-node"][0]
        self.assertEqual(1, row["load"], "load 不是现算的")
        self.assertEqual(9, row["reportedLoad"], "自报值应当另存一份")

    def test_a_node_that_never_heartbeat_reports_none(self):
        """从没心跳过时给 `None`，不是 `0`——0 会被读成"刚刚还在"。"""
        executor_id = self.store.ensure_executor("ut-quiet", ["probe"])
        self.store.conn.execute("UPDATE executors SET last_seen=0 WHERE id=?",
                                (executor_id,))
        self.store.conn.commit()
        row = [item for item in self.store.list_executors()
               if item["name"] == "ut-quiet"][0]
        self.assertIsNone(row["lastSeenSecondsAgo"])

    def test_the_tool_passes_both_fields_through(self):
        executor_id = self.store.ensure_executor("ut-node", ["probe"])
        self.store.heartbeat(executor_id, 2)
        data = self.call("list_executors")["data"]
        row = [item for item in data["executors"] if item["name"] == "ut-node"][0]
        self.assertIn("lastSeenSecondsAgo", row)
        self.assertIn("reportedLoad", row)
        self.assertIsNotNone(row["lastSeenSecondsAgo"])


# ============================================================ D17

class PagingConsistencyTestCase(_TempFixture):
    """D17 同一组读取口必须给同一套分页键。"""

    name = "paging.db"

    ENVELOPE = ("total", "count", "limit", "offset", "nextOffset",
                "hasMore", "truncated")

    def setUp(self):
        super().setUp()
        intent = self.store.add_node(self.eid, "intent",
                                     {"title": "测绘", "detail": "x",
                                      "goalId": "goal-1"})
        for index in range(60):
            self.store.add_node(self.eid, "asset",
                                {"type": "service",
                                 "value": "%d/tcp svc%d" % (1000 + index, index)})
            self.store.add_node(self.eid, "fact",
                                {"intentId": intent["id"], "kind": "info",
                                 "target": "10.10.10.250", "detail": "f%d" % index})
        self.store.add_node(self.eid, "finding",
                            {"intentId": intent["id"], "title": "示例",
                             "severity": "low", "description": "d" * 500,
                             "reproducibleSteps": ["a", "b"]})
        self.store.create_job("probe", "10.10.10.250", self.eid)

    def _envelope_of(self, name, args=None):
        data = self.call(name, args)["data"]
        return data

    def test_every_list_reader_returns_the_same_envelope(self):
        """报告的原话是"调用方必须逐个人肉试探"——四种语义四种默认。"""
        for name in ("get_findings", "get_assets", "list_jobs", "get_chain_nodes",
                     "get_report"):
            data = self._envelope_of(name)
            for key in self.ENVELOPE:
                self.assertIn(key, data, "%s 缺 %s" % (name, key))

    def test_truncated_is_exactly_has_more(self):
        """两个键永远同值。留着 `truncated` 是因为界面与既有用例读它。"""
        for name in ("get_assets", "list_jobs", "get_chain_nodes"):
            data = self._envelope_of(name)
            self.assertEqual(data["hasMore"], data["truncated"], name)

    def test_assets_page_to_the_end_like_findings_do(self):
        """`get_assets` 此前是 20/68 且没有 `hasMore`——报告里那条"68 → 20"。"""
        seen, offset, guard = [], 0, 0
        while True:
            guard += 1
            self.assertLess(guard, 20)
            data = self._envelope_of("get_assets", {"offset": offset})
            seen += [row["id"] for row in data["assets"]]
            if not data["hasMore"]:
                break
            offset = data["nextOffset"]
        self.assertEqual(60, len(seen))
        self.assertEqual(len(set(seen)), len(seen), "翻页重复了")

    def test_the_summary_says_where_the_page_sits(self):
        """模型是靠这句话决定要不要接着读的。"""
        out = self.call("get_assets", {"limit": 5})
        self.assertIn("有 60 个资产", out["summary"])
        self.assertIn("接着读", out["summary"])

    def test_findings_mark_the_rows_whose_body_was_clipped(self):
        """描述超了 400 字、或者复现步骤被切到 6 条，要标出来。"""
        data = self._envelope_of("get_findings")
        self.assertTrue(data["findings"][0]["clipped"])

    def test_a_clipped_body_does_not_change_the_page_count(self):
        """分页先算、裁剪后做：反过来的话两个数字来自不同的集合。"""
        data = self._envelope_of("get_findings")
        self.assertEqual(1, data["total"])
        self.assertEqual(1, data["count"])


# ============================================================ D19

class CurlUserAgentTestCase(unittest.TestCase):
    """D19 `-A` 是常规开关，它的值是数据。"""

    def test_a_user_agent_with_spaces_and_parentheses_is_accepted(self):
        argv = ["-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "http://x/"]
        built = toolkit.build_command("curl", argv)
        self.assertIn("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", built)

    def test_the_long_form_works_too(self):
        built = toolkit.build_command(
            "curl", ["--user-agent", "curl/8.0 (x86_64)", "http://x/"])
        self.assertIn("curl/8.0 (x86_64)", built)

    def test_the_shell_metacharacters_are_still_refused(self):
        """放宽到数据档不等于放开 cmd 的元字符。"""
        for bad in ("a&b", "a|b", "a`b", "a$b"):
            with self.assertRaises(Exception):
                toolkit.build_command("curl", ["-A", bad, "http://x/"])

    def test_a_real_newline_is_still_refused(self):
        with self.assertRaises(Exception):
            toolkit.build_command("curl", ["-w", "a\nb", "http://x/"])

    def test_every_data_flag_is_also_a_declared_argument(self):
        """两张表各写一份迟早分叉，而分叉的症状是"能过闸但没人知道它能用"。"""
        for adapter in toolkit.ADAPTERS:
            declared = set(adapter.get("arguments") or ())
            for flag in adapter.get("dataFlags") or ():
                self.assertIn(flag, declared,
                              "%s 的 %s 在数据档里却没在 arguments 里"
                              % (adapter["id"], flag))


if __name__ == "__main__":
    unittest.main()
