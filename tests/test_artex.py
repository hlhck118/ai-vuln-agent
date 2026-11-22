# -*- coding: utf-8 -*-
"""ARTEX 对照工具集的回归：59 条工具真的能跑、会拒绝、并且都过了闸门。

这一批工具与平台自有的 48 条走同一条路：声明在 `pentestd.artex_spec`、实现在
`pentestd.artex`、分级在 `policy.TOOL_LEVEL`。所以这里测的不是“artex 这个模块的
函数”，而是**它们挂上去之后平台还是不是原来那个平台**：

  * 三处（声明 / 实现 / 分级）必须一一对应，任何一处漂移都要当场红；
  * 每个 handler 的返回必须带 `ok` / `summary` / `data`，因为界面的工具卡片读这三个；
  * 写错参数必须说清楚**错在哪、该用什么**，而不是抛一个 KeyError；
  * 读路径（`_task`）不许创建目标——一次误点不该在工作台里留下一行垃圾。

用例按分节组织：注册契约 / 读写闭环 / 拒绝面 / 技能手册 / 工具与 MCP 登记。

**关于 `_payload_of`**：`store.get_node` 与 `list_nodes` 给的是原始节点（正文在
`payload` 下），而 `store.add_node` / `update_node_payload` / `store.findings` 给的
是 `models.public_node`（正文已摊平、没有 `payload` 键）。混读这两形状曾是真实缺陷
的来源（报完漏洞回来标题是空的、删资产时说不出有几条漏洞指向空），所以那几处
专门有用例盯着。
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import artex, artex_spec, chat, policy, skills, toolconfig, toolkit  # noqa: E402
from pentestd import server  # noqa: E402
from tests import support  # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


class _ArtexCase(unittest.TestCase):
    """每个用例一个临时库 + 一个目标。目标名刻意用文档地址段（10.10.10.x）。"""

    target = "10.10.10.250"

    def setUp(self):
        self.directory, self.store = support.temporary_store("artex.db")
        # 工具表要落到临时目录：`toolconfig.config_path` 在**调用时**读环境变量，
        # 所以同进程换位置是安全的；不换就会改写工程里真实的 data/tools.json。
        self.tools_path = os.path.join(self.directory.name, "tools.json")
        self._env = mock.patch.dict(os.environ,
                                    {toolconfig.ENV_CONFIG: self.tools_path})
        self._env.start()
        # 技能手册也落在临时目录：用工程真实的 data/skills 会把测试产物留在仓库里。
        self.plane = server.ControlPlane(self.store, WEB_DIR,
                                         require_executor_auth=False)
        self.plane.data_dir = self.directory.name
        self.eid = self.store.create_engagement(self.target, "artex 回归",
                                               "工单 #47")["id"]

    def tearDown(self):
        self._env.stop()
        toolkit.reload()          # 复位模块级适配器表，否则会串到下一个用例
        skills.invalidate()       # 技能目录有 TTL 缓存，不清会读到上一个用例的临时目录
        # 这里没有 HTTP 服务，所以不能用 `support.stop`：它第一句就 shutdown 一个
        # httpd，传 None 进去只会得到一个 AttributeError 把真正的失败盖住。
        self.store.close()
        self.directory.cleanup()

    # ---------------------------------------------------------------- 调用

    # 形参叫 tool 而不是 name：`create_skill` / `update_skill_file` 的第一个业务
    # 参数就叫 name，写成 `def ok(self, name, **args)` 会撞成
    # `ok() got multiple values for argument 'name'`（实测 13 个用例栽在这上面）。
    def call(self, tool, **args):
        """走 `execute_tool`：闸门 + 执行一起过。``policy_id="full"`` 是为了不
        让档位把用例挡在门外——档位本身另有用例测。"""
        return chat.execute_tool(self.plane, tool, json.dumps(args), self.eid,
                                 policy_id="full", role="admin")

    def ok(self, tool, **args):
        outcome = self.call(tool, **args)
        self.assertTrue(outcome["ok"], "%s 失败：%s" % (tool, outcome["summary"]))
        return outcome["data"]

    def fails(self, tool, expect, **args):
        outcome = self.call(tool, **args)
        self.assertFalse(outcome["ok"], "%s 本该失败却成功了" % tool)
        self.assertIn(expect, outcome["summary"],
                      "%s 的报错没说到点上：%s" % (tool, outcome["summary"]))
        return outcome


class RegistrationTestCase(unittest.TestCase):
    """声明 / 实现 / 分级三处必须一一对应。"""

    def test_spec_and_handlers_agree(self):
        declared = set(tool["name"] for tool in artex_spec.TOOLS)
        self.assertEqual(declared, set(artex.HANDLERS))

    def test_every_declaration_is_a_usable_schema(self):
        for tool in artex_spec.TOOLS:
            self.assertTrue(tool["name"], "有条声明没有名字")
            self.assertTrue(tool["label"], "%s 没有 label" % tool["name"])
            self.assertTrue(tool["description"],
                            "%s 没有 description" % tool["name"])
            schema = tool["parameters"]
            self.assertEqual(schema["type"], "object")
            self.assertFalse(schema["additionalProperties"],
                             "%s 允许了未声明的参数" % tool["name"])
            for key in schema["required"]:
                self.assertIn(key, schema["properties"],
                              "%s 把没声明的 %s 标成必填" % (tool["name"], key))

    def test_every_tool_is_leveled(self):
        for name in artex.HANDLERS:
            self.assertIn(name, policy.TOOL_LEVEL, "%s 没有分级" % name)

    def test_names_do_not_collide_with_platform_tools(self):
        from pentestd.artex import TOOLS as artex_tools
        names = [tool["name"] for tool in artex_tools]
        self.assertEqual(len(names), len(set(names)), "artex 自己重名了")
        stray = [name for name in names
                 if chat.TOOL_INDEX[name] is not None and
                 chat.TOOL_INDEX[name] not in artex_tools]
        self.assertEqual(stray, [], "这些名字被平台工具占用了：%s" % stray)

    def test_catalog_lists_them_at_full_policy(self):
        rows = chat.tool_catalog("full", "admin", True, plane=None)
        listed = set(row["name"] for row in rows)
        for name in artex.HANDLERS:
            self.assertIn(name, listed, "%s 没出现在完全作业档的目录里" % name)

    def test_readonly_catalog_hides_the_writers(self):
        rows = chat.tool_catalog("readonly", "operator", True, plane=None)
        listed = set(row["name"] for row in rows)
        self.assertIn("graph_overview", listed)
        self.assertNotIn("record_fact", listed, "只读档不该出现写入工具")


class ChainTestCase(_ArtexCase):
    """读写闭环：建意图 → 记事实 → 报漏洞 → 挂流量 → 复核 → 案卷。"""

    def test_overview_counts_move_with_writes(self):
        before = self.ok("graph_overview")
        self.assertEqual(before["taskId"], self.eid)
        self.assertEqual(before["target"], self.target)
        self.assertEqual(before["counts"]["goals"], 1, "建档应有 goal-1")

        made = self.ok("add_intent",
                       intents=[{"title": "摸一下入口", "detail": "先看首页"}])
        # 事实要挂在刚建的意图下：不给 intent_id 时 `_intent_id` 会补建一条
        # 「会话内记录」，这条用例就变成在数两条意图了。
        self.ok("record_fact", detail="首页 200", target="/",
                intent_id=made["intents"][0]["id"])
        after = self.ok("graph_overview")
        self.assertEqual(after["counts"]["intents"], 1)
        self.assertEqual(after["counts"]["facts"], 1)

    def test_facts_need_an_intent_and_get_one(self):
        data = self.ok("record_fact", detail="nginx/1.24")
        self.assertTrue(data["intentId"].startswith("intent-"),
                        "没给 intent 时应自动挂一条：%s" % data["intentId"])
        self.assertEqual(data["count"], 1)
        node = self.store.get_node(self.eid, data["facts"][0]["id"])
        self.assertEqual(node["payload"]["factKind"], "info")

    def test_report_finding_marks_verification_by_evidence(self):
        verified = self.ok("report_finding", summary="首页反射型 XSS",
                           severity="high", evidence="payload 在响应里原样返回")
        self.assertTrue(verified["verified"], "给了 evidence 就该算已证实")
        self.assertEqual(verified["finding"]["severity"], "high")

        static = self.ok("report_finding", summary="疑似目录穿越",
                         severity="low")
        self.assertFalse(static["verified"], "没有 evidence 不许算已证实")
        self.assertEqual(static["finding"]["verification"]["verdict"],
                         "confirmed_static")

    def test_report_finding_returns_the_row_it_just_wrote(self):
        # 这条钉住 `_payload_of`：`add_node` 回的是 public_node（正文已摊平），
        # 早先 `_finding_row` 去它下面找 `payload` 键，于是刚写完的漏洞在界面上
        # 标题是空的、等级退回 info。
        data = self.ok("report_finding", summary="SSH 允许口令登录",
                       severity="medium")
        self.assertEqual(data["finding"]["title"], "SSH 允许口令登录")
        self.assertEqual(data["finding"]["severity"], "medium")
        listed = self.ok("list_findings")["findings"]
        self.assertEqual(listed[0]["title"], "SSH 允许口令登录")
        self.assertEqual(listed[0]["severity"], "medium")

    def test_report_finding_refuses_unknown_severity(self):
        self.fails("report_finding", "severity 必须是", summary="x",
                   severity="catastrophic")

    def test_report_finding_needs_a_summary(self):
        self.fails("report_finding", "summary 不能为空")

    def test_finding_description_carries_the_vulnclass(self):
        data = self.ok("report_finding", summary="登录处可以注入",
                       vulnclass="sql-injection")
        self.assertTrue(data["finding"]["description"].startswith(
            "类别：sql-injection"))

    def test_update_finding_report_appends_instead_of_replacing(self):
        made = self.ok("report_finding", summary="弱口令", severity="medium")
        fid = made["finding"]["id"]
        changed = self.ok("update_finding_report", finding_id=fid,
                          report="补一句：口令是 admin/admin")
        self.assertIn("description", changed["changed"])
        node = self.store.get_node(self.eid, fid)
        body = node["payload"]["description"]
        self.assertIn("弱口令", body, "原来的正文不能被覆盖掉")
        self.assertIn("admin/admin", body)

    def test_update_finding_report_needs_something_to_change(self):
        made = self.ok("report_finding", summary="弱口令")
        self.fails("update_finding_report", "没有要改的内容",
                   finding_id=made["finding"]["id"])

    def test_goals_and_prove(self):
        self.ok("set_goals", text="拿到后台管理员权限")
        goals = self.ok("list_goals")
        self.assertEqual(goals["count"], 2, "goal-1 之外应多一条")
        target = [row for row in goals["goals"]
                  if row["target"] == "拿到后台管理员权限"][0]
        fact = self.ok("record_fact", detail="后台登录页存在")
        proved = self.ok("prove_goal", goal_id=target["id"],
                         evidence_id=fact["facts"][0]["id"],
                         reason="登录页可用")
        self.assertEqual(proved["goalId"], target["id"])
        self.assertEqual(proved["evidence"]["kind"], "fact")
        met = self.ok("goal_met", goal_id=target["id"], reason="已登录进后台")
        self.assertEqual(met["goalId"], target["id"])
        self.assertEqual(met["goal"]["state"], "met")

    def test_prove_goal_refuses_an_asset_as_evidence(self):
        self.ok("insert_assets", assets=[{"type": "subdomain",
                                          "value": "a.example.test"}])
        asset = self.ok("list_assets")["assets"][0]["id"]
        goals = self.ok("list_goals")
        self.fails("prove_goal", "证据必须是链路里的事实",
                   goal_id=goals["goals"][0]["id"], evidence_id=asset)

    def test_goal_met_needs_a_reason(self):
        goals = self.ok("list_goals")
        self.fails("goal_met", "reason 不能为空", goal_id=goals["goals"][0]["id"])

    def test_set_goals_refuses_empty(self):
        self.fails("set_goals", "目标内容不能为空")

    def test_intents_cannot_hang_on_two_anchors(self):
        fact = self.ok("record_fact", detail="x")
        self.fails("add_intent", "必须且只能挂一个锚点",
                   intents=[{"title": "双锚点", "goalId": "goal-1",
                             "derivedFromFactId": fact["facts"][0]["id"]}])

    def test_intents_need_a_title(self):
        self.fails("add_intent", "没有 title", intents=[{"detail": "没标题"}])

    def test_intents_need_a_batch(self):
        self.fails("add_intent", "intents 不能为空", title="顶层 title 不算")

    def test_kill_work_exhausts_the_intent(self):
        made = self.ok("add_intent", intents=[{"title": "直连数据库"}])
        killed = self.ok("kill_work", intent_id=made["intents"][0]["id"],
                         reason="端口不通")
        node = self.store.get_node(self.eid, killed["intentId"])
        self.assertEqual(node["state"], "exhausted")

    def test_steer_work_writes_a_correction_fact(self):
        made = self.ok("add_intent", intents=[{"title": "扫一遍子域"}])
        steered = self.ok("steer_work", intent_id=made["intents"][0]["id"],
                          message="先看 443")
        node = self.store.get_node(self.eid, steered["factId"])
        self.assertTrue(node["payload"]["detail"].startswith("纠偏指令："))

    def test_steer_work_needs_a_message(self):
        made = self.ok("add_intent", intents=[{"title": "扫一遍子域"}])
        self.fails("steer_work", "message 不能为空",
                   intent_id=made["intents"][0]["id"])


class ScopeTestCase(_ArtexCase):
    """约束 / 提示 / 资产 / 清理。"""

    def test_constraints_land_as_allow_and_deny(self):
        data = self.ok("add_task_scope", kind="allow",
                       value="10.10.10.0/24 网段内")
        self.assertEqual(data["allow"], 1)
        self.assertEqual(data["deny"], 0)
        self.ok("add_task_scope", entries=[{"kind": "deny", "text": "别动生产库"}])
        rows = self.store.list_constraints(self.eid)
        self.assertEqual(sorted(row["kind"] for row in rows), ["allow", "deny"])

    def test_constraints_refuse_a_third_kind(self):
        self.fails("add_task_scope", "约束类型只能是 allow 或 deny",
                   kind="maybe", value="说不清")

    def test_task_scope_refuses_an_empty_call(self):
        self.fails("add_task_scope", "要给一条约束")

    def test_hints_become_info_facts(self):
        data = self.ok("add_hint", text="同事说后台在 8443")
        node = self.store.get_node(self.eid, data["hints"][0]["id"])
        self.assertEqual(node["payload"]["factKind"], "info")
        self.assertIn("8443", node["payload"]["detail"])

    def test_hints_need_a_body(self):
        self.fails("add_hint", "至少要有一条提示正文")

    def test_assets_are_reused_by_type_and_value(self):
        first = self.ok("insert_assets", assets=[
            {"type": "subdomain", "value": "a.example.test"}])
        self.assertEqual(len(first["created"]), 1)
        second = self.ok("insert_assets", assets=[
            {"type": "subdomain", "value": "a.example.test"}])
        self.assertEqual(len(second["created"]), 0)
        self.assertEqual(len(second["reused"]), 1, "同一个资产不该建两行")

    def test_assets_refuse_an_unknown_type(self):
        self.fails("insert_assets", "asset.type 非法", assets=[
            {"type": "cluster", "value": "x"}])

    def test_assets_need_values(self):
        self.fails("insert_assets", "assets 不能为空")

    def test_delete_assets_by_host_reports_dangling_findings(self):
        kept = self.ok("insert_assets", assets=[
            {"type": "subdomain", "value": "a.example.test"}])
        asset = kept["created"][0]["id"]
        self.ok("report_finding", summary="弱口令", asset_ids=[asset])
        gone = self.ok("delete_assets_by_host", host="example.test")
        self.assertEqual(gone["count"], 1)
        self.assertEqual(len(gone["findingsWithDanglingAsset"]), 1,
                         "删资产前要说清有多少条漏洞会指向空")

    def test_delete_assets_needs_a_host(self):
        self.fails("delete_assets_by_host", "host 不能为空")

    def test_list_companies_rolls_up_by_last_two_labels(self):
        self.ok("insert_assets", assets=[
            {"type": "subdomain", "value": "a.example.test"},
            {"type": "subdomain", "value": "b.example.test"},
            {"type": "subdomain", "value": "x.other.test"}])
        data = self.ok("list_companies")
        names = [row["company"] for row in data["companies"]]
        self.assertIn("example.test", names)
        self.assertIn("other.test", names)
        example = [row for row in data["companies"]
                   if row["company"] == "example.test"][0]
        self.assertEqual(example["assets"], 2)

    def test_untested_assets_shrink_when_a_finding_points_at_them(self):
        made = self.ok("insert_assets", assets=[
            {"type": "subdomain", "value": "a.example.test"},
            {"type": "subdomain", "value": "b.example.test"}])
        self.assertEqual(self.ok("list_untested_assets")["count"], 2)
        self.ok("report_finding", summary="弱口令",
                asset_ids=[made["created"][0]["id"]])
        left = self.ok("list_untested_assets")
        self.assertEqual(left["count"], 1)
        self.assertEqual(left["assets"][0]["value"], "b.example.test")


class TrafficTestCase(_ArtexCase):
    """流量三件套：搜 / 读 / 取正文。"""

    def setUp(self):
        super().setUp()
        self.traffic = self.store.record_traffic(
            engagement_id=self.eid, method="POST",
            url="http://10.10.10.250/login", request_body="user=admin",
            status=302, reason="Found",
            response_body="\x00" * 4000, response_truncated=True)

    def test_search_needs_a_host(self):
        self.fails("traffic_search", "host 不能为空")

    def test_search_filters_by_host_and_contains(self):
        data = self.ok("traffic_search", host="10.10.10.250")
        self.assertEqual(data["count"], 1)
        self.assertEqual(self.ok("traffic_search", host="10.10.10.250",
                                 contains="/login")["count"], 1)
        self.assertEqual(self.ok("traffic_search", host="10.10.10.250",
                                 contains="/admin")["count"], 0)

    def test_get_returns_both_sides(self):
        data = self.ok("traffic_get", id=self.traffic)
        self.assertEqual(data["traffic"]["id"], self.traffic)
        # 正文是分页结构（text / nextOffset / hasMore…），不是一根裸字符串：
        # 一条几十万字符的响应要能继续读下去。
        self.assertIn("admin", data["request"]["body"]["text"])
        self.assertTrue(data["responseTruncated"])

    def test_blob_pages_the_body(self):
        data = self.ok("traffic_blob", hash=self.traffic, side="response",
                       offset=0, length=100)
        self.assertEqual(data["side"], "response")
        self.assertEqual(len(data["text"]), 100)
        self.assertTrue(data["hasMore"], "4000 字节只给了 100，还要能续读")

    def test_blob_needs_a_traffic_id(self):
        self.fails("traffic_blob", "hash 不能为空")

    def test_binding_a_finding_to_traffic_is_idempotent(self):
        made = self.ok("report_finding", summary="登录处可爆破")
        fid = made["finding"]["id"]
        first = self.ok("bind_finding_traffic", finding_id=fid,
                        traffic_refs=[self.traffic])
        self.assertEqual(len(first["added"]), 1)
        again = self.ok("bind_finding_traffic", finding_id=fid,
                        traffic_refs=[self.traffic])
        self.assertEqual(again["added"], [], "同一条流量不该挂两遍")
        self.assertEqual(len(again["trafficRefs"]), 1)

    def test_binding_refuses_an_unknown_traffic_id(self):
        made = self.ok("report_finding", summary="登录处可爆破")
        self.fails("bind_finding_traffic", "流量记录不存在",
                   finding_id=made["finding"]["id"], traffic_refs=["X-nope"])

    def test_binding_needs_refs(self):
        made = self.ok("report_finding", summary="登录处可爆破")
        self.fails("bind_finding_traffic", "traffic_refs 不能为空",
                   finding_id=made["finding"]["id"])

    def test_get_finding_traffic_summarises_without_a_side(self):
        made = self.ok("report_finding", summary="登录处可爆破",
                       traffic_refs=[self.traffic])
        data = self.ok("get_finding_traffic",
                       finding_id=made["finding"]["id"])
        self.assertEqual(data["side"], "summary")
        self.assertEqual(data["traffic"][0]["requestBytes"], len("user=admin"))
        self.assertNotIn("body", data["traffic"][0])
        with_body = self.ok("get_finding_traffic",
                            finding_id=made["finding"]["id"], side="request")
        self.assertIn("admin", with_body["traffic"][0]["body"]["text"])


class CaseTestCase(_ArtexCase):
    """案卷（分组）+ 复核记录。"""

    def _group(self):
        self.ok("report_finding", summary="TLS 证书未通过校验", severity="low")
        self.ok("report_finding", summary="TLS 证书未通过校验", severity="low")
        groups = self.ok("list_findings")["groups"]
        self.assertEqual(len(groups), 1, "同标题同等级应归成一个案卷")
        return groups[0]

    def test_case_report_is_written_to_every_record(self):
        group = self._group()
        data = self.ok("update_finding_case_report", case_id=group["key"],
                       version="v1", report="两处都是自签证书",
                       severity="medium", severity_reason="影响两个端口")
        self.assertEqual(len(data["records"]), 2)
        self.assertEqual(data["caseReport"]["version"], "v1")
        for rid in data["records"]:
            node = self.store.get_node(self.eid, rid)
            self.assertEqual(node["payload"]["caseReport"]["report"],
                             "两处都是自签证书")
            self.assertEqual(node["payload"]["severity"], "medium")

    def test_case_report_needs_a_version(self):
        group = self._group()
        self.fails("update_finding_case_report", "version 不能为空",
                   case_id=group["key"], report="x")

    def test_case_report_needs_a_body_or_a_title(self):
        group = self._group()
        self.fails("update_finding_case_report", "report 或 title 至少要给一个",
                   case_id=group["key"], version="v1")

    def test_case_report_refuses_an_unknown_case(self):
        self.fails("update_finding_case_report", "找不到这个案卷",
                   case_id="nope", version="v1", report="x")

    def test_get_finding_case_accepts_the_group_key(self):
        group = self._group()
        page = self.ok("get_finding_case", case_id=group["key"])
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["findings"][0]["severity"], "low")

    def test_retest_appends_and_keeps_history(self):
        made = self.ok("report_finding", summary="弱口令")
        fid = made["finding"]["id"]
        self.ok("record_finding_retest_result", finding_id=fid,
                verdict="reproduced", summary="还能登", evidence="又登了一次")
        second = self.ok("record_finding_retest_result", finding_id=fid,
                         verdict="fixed", summary="改了密码",
                         evidence="旧口令 401")
        self.assertEqual(second["count"], 2, "复核记录是追加，不是覆盖")
        node = self.store.get_node(self.eid, fid)
        self.assertEqual([row["verdict"] for row in node["payload"]["retests"]],
                         ["reproduced", "fixed"])

    def test_retest_refuses_an_unknown_verdict(self):
        made = self.ok("report_finding", summary="弱口令")
        self.fails("record_finding_retest_result", "verdict 必须是",
                   finding_id=made["finding"]["id"], verdict="maybe",
                   summary="说不清", evidence="x")

    def test_retest_needs_evidence(self):
        made = self.ok("report_finding", summary="弱口令")
        self.fails("record_finding_retest_result", "evidence 不能为空",
                   finding_id=made["finding"]["id"], verdict="fixed",
                   summary="改了")

    def test_review_records_every_decision(self):
        group = self._group()
        data = self.ok("complete_finding_case_review", summary="这轮过完了",
                       decisions=[{"case_id": group["key"],
                                   "verdict": "fixed", "reason": "已修复"}])
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["failed"], [])

    def test_review_reports_bad_decisions_instead_of_dropping_them(self):
        data = self.ok("complete_finding_case_review", summary="这轮过完了",
                       decisions=["不是对象", {"case_id": "nope",
                                             "verdict": "fixed"}])
        self.assertEqual(data["count"], 0)
        self.assertEqual(len(data["failed"]), 2, "两条都该被点出来")

    def test_review_needs_decisions(self):
        self.fails("complete_finding_case_review", "decisions 不能为空",
                   summary="这轮过完了")

    def test_merge_keeps_the_losers(self):
        first = self.ok("report_finding", summary="TLS 证书未通过校验",
                        severity="low")
        second = self.ok("report_finding", summary="TLS 证书不可信",
                         severity="high")
        a, b = first["finding"]["id"], second["finding"]["id"]
        merged = self.ok("merge_finding_records", finding_ids=[a, b],
                         title="TLS 证书问题", reason="同一件事")
        self.assertEqual(merged["severity"], "high", "合并取更严重的那一档")
        loser = self.store.get_node(self.eid, b)
        self.assertEqual(loser["payload"]["mergedInto"], [a])
        self.assertIn("已并入", loser["payload"]["description"])
        self.assertEqual(len(self.store.list_nodes(self.eid, "finding")), 2,
                         "合并只标不删：旧记录要能查")

    def test_merge_needs_two_records(self):
        made = self.ok("report_finding", summary="弱口令")
        self.fails("merge_finding_records", "至少要两条漏洞",
                   finding_ids=[made["finding"]["id"]], title="x", reason="y")

    def test_merge_needs_a_reason(self):
        first = self.ok("report_finding", summary="A")
        second = self.ok("report_finding", summary="B")
        self.fails("merge_finding_records", "reason 不能为空",
                   finding_ids=[first["finding"]["id"],
                                second["finding"]["id"]], title="合并")

    def test_suggest_merge_is_read_only(self):
        first = self.ok("report_finding", summary="TLS 证书未通过校验")
        second = self.ok("report_finding", summary="TLS 证书不可信")
        data = self.ok("suggest_finding_merge",
                       finding_ids=[first["finding"]["id"],
                                    second["finding"]["id"]],
                       title="合并后", reason="同一件事")
        self.assertEqual(data["proposal"]["records"], 2)
        self.assertEqual(len(self.store.list_nodes(self.eid, "finding")), 2,
                         "建议不该动任何记录")

    def test_suggest_merge_needs_two_ids(self):
        made = self.ok("report_finding", summary="弱口令")
        self.fails("suggest_finding_merge", "至少要给两条漏洞",
                   finding_ids=[made["finding"]["id"]])


class IntelligenceTestCase(_ArtexCase):
    """情报：只记“在哪儿见过什么形态”，不落明文口令。"""

    def test_intelligence_is_stored_with_its_kind(self):
        data = self.ok("record_intelligence", kind="credential",
                       title="登录页见到的口令形态",
                       content="admin 账号，口令长度 6，疑似弱口令",
                       confidence="observed", evidence="抓包里看到 form",
                       status="observed")
        node = self.store.get_node(self.eid, data["id"])
        self.assertEqual(node["payload"]["intelKind"], "credential")
        self.assertEqual(node["payload"]["intelStatus"], "observed")

    def test_intelligence_refuses_an_unknown_kind(self):
        self.fails("record_intelligence", "kind 必须是", kind="gossip",
                   title="x", content="y", evidence="z",
                   confidence="observed")

    def test_intelligence_needs_evidence(self):
        # 校验顺序是 kind → confidence → title → content → evidence：
        # 不给 confidence 会先撞上它，所以这条用例必须把它补上。
        self.fails("record_intelligence", "evidence 不能为空",
                   kind="lead", title="x", content="y",
                   confidence="inferred")


class TaskTestCase(_ArtexCase):
    """任务：列表 / 派生 / 暂停。"""

    def test_list_tasks_sees_this_target(self):
        data = self.ok("list_tasks")
        ids = [row["id"] for row in data["tasks"]]
        self.assertIn(self.eid, ids)

    def test_spawn_task_creates_a_child_it_can_pause(self):
        data = self.ok("spawn_task", description="子任务：打 443",
                       goal="拿到 443 的证书", parent_ref=self.eid)
        child = data["taskId"]
        self.assertNotEqual(child, self.eid)
        self.assertEqual(data["parentRef"], self.eid)
        self.assertEqual(data["sourceTaskIds"], [self.eid])
        meta = self.store.get_task(child)
        self.assertIn(self.eid, meta.get("sourceTaskIds") or [])

    def test_spawn_task_can_seed_an_intent(self):
        data = self.ok("spawn_task", description="子任务：打 443",
                       goal="拿到 443 的证书", seed_first_intent=True)
        intents = self.store.list_nodes(data["taskId"], "intent")
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["id"], data["seedIntent"])

    def test_spawn_task_refuses_too_many_parents(self):
        self.fails("spawn_task", "最多 8 个", description="子任务",
                   goal="x", source_task_ids=["E-%08x" % i for i in range(9)])

    def test_spawn_task_refuses_a_short_no_result_window(self):
        self.fails("spawn_task", "要么是 0", description="子任务", goal="x",
                   no_result_seconds=30)

    def test_spawn_task_needs_a_goal(self):
        self.fails("spawn_task", "goal 不能为空", description="子任务")

    def test_pause_task_flips_both_ways(self):
        self.assertTrue(self.ok("pause_task")["paused"])
        self.assertTrue(self.store.engagement_paused(self.eid))
        self.assertFalse(self.ok("pause_task", paused=False)["paused"])
        self.assertFalse(self.store.engagement_paused(self.eid))

    def test_company_scope_reuses_an_existing_target(self):
        data = self.ok("add_company_scope", company="example",
                       scope="example.test", reason="客户授权书 #9")
        self.assertTrue(data["created"])
        again = self.ok("add_company_scope", company="example",
                        scope="example.test")
        self.assertFalse(again["created"], "同一个 scope 不该建第二个目标")
        self.assertNotEqual(again["taskId"], self.eid)

    def test_company_scope_needs_both_halves(self):
        self.fails("add_company_scope", "company 不能为空",
                   scope="example.test")
        self.fails("add_company_scope", "scope 不能为空", company="example")


class SkillTestCase(_ArtexCase):
    """技能手册：写在 data/skills 下，不覆盖、不出目录。"""

    def test_create_writes_a_skill_md(self):
        data = self.ok("create_skill", name="My Probe",
                       description="探测前先看清楚",
                       instructions="1. 先看首页\n2. 再看 443")
        self.assertFalse(data["overwritten"])
        self.assertEqual(os.path.basename(data["path"]), "SKILL.md")
        with open(data["path"], encoding="utf-8") as handle:
            body = handle.read()
        self.assertTrue(body.startswith("---\nname: "))
        self.assertIn("description: 探测前先看清楚", body)
        self.assertIn("2. 再看 443", body)

    def test_create_does_not_overwrite_an_existing_one(self):
        # `overwritten` 恒为 False：重名不是一次「覆盖成功」，而是根本没写。
        first = self.ok("create_skill", name="My Probe", description="a",
                        instructions="original")
        second = self.ok("create_skill", name="My Probe", description="b",
                         instructions="replacement")
        self.assertFalse(second["overwritten"])
        with open(first["path"], encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("original", body)
        self.assertNotIn("replacement", body)

    def test_create_needs_both_description_and_body(self):
        self.fails("create_skill", "description 不能为空", name="x",
                   instructions="y")
        self.fails("create_skill", "instructions 不能为空", name="x",
                   description="y")

    def test_created_skills_show_up_in_the_catalog(self):
        data = self.ok("create_skill", name="My Probe", description="a",
                       instructions="b")
        rows = skills.catalog({}, self.plane.data_dir)
        # 目录里显示的是 frontmatter 的 `name:`（也就是当初传进来的原文），
        # 不是目录名 slug：`update_skill_file` 认的也是这一个。
        names = [row["name"] for row in rows]
        self.assertIn("My Probe", names)
        self.assertEqual(data["slug"], "My-Probe")
        row = [item for item in rows if item["name"] == "My Probe"][0]
        self.assertTrue(row["directory"], "目录型手册要标出来")
        self.assertEqual(os.path.basename(row["path"]), "SKILL.md")

    def test_update_writes_into_the_same_folder(self):
        made = self.ok("create_skill", name="My Probe", description="a",
                       instructions="b")
        updated = self.ok("update_skill_file", name=made["name"],
                          file="reference.md", content="# 参考\n")
        self.assertEqual(os.path.basename(updated["path"]), "reference.md")
        self.assertEqual(os.path.dirname(updated["path"]),
                         os.path.dirname(made["path"]))

    def test_update_refuses_to_escape_the_folder(self):
        made = self.ok("create_skill", name="My Probe", description="a",
                       instructions="b")
        self.fails("update_skill_file", "拒绝写到这份手册的目录之外",
                   name=made["name"], file="../../escape.md", content="x")

    def test_update_refuses_an_unknown_skill(self):
        self.fails("update_skill_file", "没有名为", name="nope", content="x")


class RegistryTestCase(_ArtexCase):
    """平台登记：自定义工具与 MCP 服务。"""

    def test_create_custom_tool_writes_the_table(self):
        data = self.ok("create_custom_tool", key="myprobe",
                       description="内网探针", kind="toolkit",
                       exec={"binaries": ["myprobe"]})
        self.assertEqual(data["path"], self.tools_path)
        self.assertTrue(os.path.isfile(self.tools_path))
        with open(self.tools_path, encoding="utf-8") as handle:
            rows = json.load(handle)["adapters"]
        self.assertIn("myprobe", [row["id"] for row in rows])

    def test_create_custom_tool_refuses_a_duplicate(self):
        self.ok("create_custom_tool", key="myprobe", description="x")
        self.fails("create_custom_tool", "已经登记过", key="myprobe")

    def test_create_custom_tool_refuses_exe_as_class(self):
        self.fails("create_custom_tool", "kind 在本平台是侵入类别",
                   key="myprobe", kind="exe")

    def test_update_custom_tool_needs_an_existing_key(self):
        self.fails("update_custom_tool", "还没登记过", key="nope")

    def test_update_custom_tool_keeps_what_was_not_mentioned(self):
        self.ok("create_custom_tool", key="myprobe", description="内网探针",
                kind="shell", exec={"binaries": ["myprobe"]})
        self.ok("update_custom_tool", key="myprobe", description="换个说法")
        with open(self.tools_path, encoding="utf-8") as handle:
            rows = json.load(handle)["adapters"]
        row = [item for item in rows if item["id"] == "myprobe"][0]
        self.assertEqual(row["class"], "shell", "没提到的字段不能被清掉")
        self.assertEqual(row["binaries"], ["myprobe"])
        self.assertEqual(row["purpose"], "换个说法")

    def test_create_mcp_registers_a_stdio_server(self):
        data = self.ok("create_mcp", name="demo", transport="stdio",
                       command="npx", args=["-y", "demo-mcp"], level="probe")
        self.assertEqual(data["id"], "demo")
        self.assertEqual(data["level"], "probe")
        row = self.store.get_mcp_server("demo")
        self.assertEqual(row["command"], "npx")

    def test_create_mcp_refuses_a_bad_id(self):
        self.fails("create_mcp", "服务 id 只能是", name="Bad Name",
                   transport="stdio", command="npx")

    def test_create_mcp_refuses_a_stdio_without_command(self):
        self.fails("create_mcp", "stdio 传输必须给出要启动的命令",
                   name="demo", transport="stdio")

    def test_create_mcp_refuses_a_bad_level(self):
        self.fails("create_mcp", "不支持的权限档", name="demo",
                   transport="stdio", command="npx", level="root")

    def test_create_mcp_refuses_a_duplicate(self):
        self.ok("create_mcp", name="demo", transport="stdio", command="npx")
        self.fails("create_mcp", "已经登记过", name="demo",
                   transport="stdio", command="npx")

    def test_update_mcp_keeps_command_and_timeout_when_only_renaming(self):
        # 这一条钉的是一个真实修过的缺陷：整条覆盖时如果不把原值补回，
        # “只改名称”会把 command 抹空、把超时从 30 秒还原成默认 60 秒。
        self.ok("create_mcp", name="demo", transport="stdio", command="npx",
                args=["demo-mcp"])
        row = self.store.get_mcp_server("demo")
        self.store.save_mcp_server(dict(row, timeout_s=30))
        self.ok("update_mcp", id="demo", label="演示服务")
        after = self.store.get_mcp_server("demo")
        self.assertEqual(after["command"], "npx", "命令被抹掉了")
        self.assertEqual(after["args"], ["demo-mcp"])
        self.assertEqual(after["timeout_s"], 30, "超时被悄悄改回去了")
        self.assertEqual(after["label"], "演示服务")

    def test_update_mcp_needs_an_existing_id(self):
        self.fails("update_mcp", "没有登记过这个 MCP 服务", id="nope")


class TaskResolutionTestCase(_ArtexCase):
    """`_task` 的解析顺序：explicit → fallback → session → 最近一个，且不创建。"""

    def test_explicit_task_id_wins(self):
        other = self.store.create_engagement("10.10.10.251")["id"]
        self.ok("add_intent", intents=[{"title": "在别的目标里"}],
                task_id=other)
        self.assertEqual(len(self.store.list_nodes(other, "intent")), 1)
        self.assertEqual(len(self.store.list_nodes(self.eid, "intent")), 0,
                         "task_id 指到哪儿就写哪儿")

    def test_unknown_task_id_is_a_clear_refusal(self):
        self.fails("list_facts", "engagement 不存在", task_id="E-00000000")

    def test_reading_without_any_target_says_what_to_do(self):
        empty_directory, empty_store = support.temporary_store("empty.db")
        try:
            plane = server.ControlPlane(empty_store, WEB_DIR,
                                        require_executor_auth=False)
            outcome = chat.execute_tool(plane, "graph_overview", "{}", "",
                                        policy_id="full", role="admin")
            self.assertFalse(outcome["ok"])
            self.assertIn("还没有任何目标", outcome["summary"])
            self.assertEqual(empty_store.list_engagements(), [],
                             "读路径不许顺手建一个目标")
        finally:
            empty_store.close()
            empty_directory.cleanup()


class GateTestCase(_ArtexCase):
    """闸门：档位与角色。同一批工具必须和平台自有工具一样被挡。"""

    def test_readonly_cannot_write(self):
        outcome = chat.execute_tool(self.plane, "record_fact",
                                    json.dumps({"detail": "x"}), self.eid,
                                    policy_id="readonly", role="operator")
        self.assertFalse(outcome["ok"])
        self.assertIn("权限拒绝", outcome["summary"])
        self.assertIn("只读观察", outcome["summary"])
        self.assertEqual(self.store.list_nodes(self.eid, "fact"), [])

    def test_probe_cannot_register_an_mcp_server(self):
        outcome = chat.execute_tool(self.plane, "create_mcp",
                                    json.dumps({"name": "demo",
                                                "transport": "stdio",
                                                "command": "npx"}),
                                    self.eid, policy_id="probe",
                                    role="operator")
        self.assertFalse(outcome["ok"])
        self.assertIn("完全作业", outcome["summary"])

    def test_operator_cannot_register_even_at_full(self):
        outcome = chat.execute_tool(self.plane, "create_mcp",
                                    json.dumps({"name": "demo",
                                                "transport": "stdio",
                                                "command": "npx"}),
                                    self.eid, policy_id="full",
                                    role="operator")
        self.assertFalse(outcome["ok"])
        self.assertIn("只有管理员", outcome["summary"])

    def test_read_tools_pass_at_readonly(self):
        outcome = chat.execute_tool(self.plane, "graph_overview", "{}",
                                    self.eid, policy_id="readonly",
                                    role="operator")
        self.assertTrue(outcome["ok"], outcome["summary"])

    def test_unknown_tool_lists_what_exists(self):
        outcome = chat.execute_tool(self.plane, "nope", "{}", self.eid,
                                    policy_id="full", role="admin")
        self.assertFalse(outcome["ok"])
        self.assertIn("不存在名为 nope 的工具", outcome["summary"])


class SmugglingTestCase(unittest.TestCase):
    """`execute()` 是 artex 自己的入口，它必须和 `invoke_tool` 一致地报错。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("artex2.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR,
                                         require_executor_auth=False)
        self.plane.data_dir = self.directory.name
        self.eid = self.store.create_engagement("10.10.10.252")["id"]

    def tearDown(self):
        skills.invalidate()
        self.store.close()
        self.directory.cleanup()

    def test_execute_refuses_an_unknown_tool(self):
        with self.assertRaises(ValueError) as caught:
            artex.execute(self.plane, "nope", {}, self.eid)
        self.assertIn("没有实现这条工具", str(caught.exception))

    def test_execute_runs_a_real_one(self):
        # `execute` 交回来的是 `_ok(...)` 原样：`summary` / `data` / `touched`，
        # `ok` 是 `invoke_tool` 那一层补上的，不该在这里出现。
        outcome = artex.execute(self.plane, "graph_overview", {}, self.eid)
        self.assertEqual(sorted(outcome), ["data", "summary", "touched"])
        self.assertEqual(outcome["data"]["taskId"], self.eid)


class PurityTestCase(unittest.TestCase):
    """声明文件里不该有实现：它是给 `policy` 与表格读的同一份清单。"""

    def test_spec_has_no_handlers_table(self):
        self.assertFalse(hasattr(artex_spec, "HANDLERS"))
        self.assertFalse(hasattr(artex_spec, "LEVELS"))

    def test_every_declaration_carries_the_four_sections(self):
        for tool in artex_spec.TOOLS:
            self.assertEqual(sorted(tool),
                             ["description", "label", "name", "parameters"],
                             "%s 的条目形状不对" % tool["name"])


if __name__ == "__main__":
    unittest.main()
