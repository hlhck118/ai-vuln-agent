# -*- coding: utf-8 -*-
"""独立渗透测试工作台的测试。

覆盖四层：

  1. 存储层——id 规则、链路约束校验、窗口语义（截断但不断链）；
  2. 图与布局——分层正确、节点不重叠、边不悬空、SVG 可渲染；
  3. 控制面——REST 接口的返回结构、错误码、静态资源与导出；
  4. 执行面——本地节点与远程节点两条路径都要跑通"领任务 → 执行 → 写入链路"。

运行：python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agent as agent_module                      # noqa: E402
from pentestd import engine, graph                              # noqa: E402
from pentestd import models                                     # noqa: E402
from pentestd import server as server_module                     # noqa: E402
from pentestd.models import ValidationError                     # noqa: E402
from pentestd.store import Store                                # noqa: E402
from tests import support                                       # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")
ENGINE_HOME = engine.locate_engine()
VULNAPPL = os.path.join(ENGINE_HOME, "demo", "vulnapp") if ENGINE_HOME else ""


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        self.eid = self.store.create_engagement("demo-target", "测试目标", "测试授权")["id"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # ---------------------------------------------------------- id 与约束
    def test_goal_created_with_engagement(self):
        goal = self.store.get_node(self.eid, "goal-1")
        self.assertIsNotNone(goal)
        self.assertEqual(goal["payload"]["target"], "demo-target")

    def test_node_ids_are_deterministic_per_kind(self):
        first = self.store.add_node(self.eid, "intent", {"title": "A", "goalId": "goal-1"})
        second = self.store.add_node(self.eid, "intent", {"title": "B", "goalId": "goal-1"})
        self.assertEqual(first["id"], "intent-1")
        self.assertEqual(second["id"], "intent-2")

    def test_ids_are_scoped_per_engagement(self):
        other = self.store.create_engagement("another")["id"]
        node = self.store.add_node(other, "intent", {"title": "X", "goalId": "goal-1"})
        self.assertEqual(node["id"], "intent-1", "不同 engagement 的 id 计数相互独立")

    def test_finding_requires_reproducible_steps(self):
        intent = self.store.add_node(self.eid, "intent", {"title": "A", "goalId": "goal-1"})
        with self.assertRaises(ValidationError) as ctx:
            self.store.add_node(self.eid, "finding",
                                {"intentId": intent["id"], "title": "t", "severity": "high"})
        self.assertIn("reproducibleSteps", str(ctx.exception))

    def test_intent_requires_exactly_one_anchor(self):
        with self.assertRaises(ValidationError):
            self.store.add_node(self.eid, "intent", {"title": "A"})
        with self.assertRaises(ValidationError):
            self.store.add_node(self.eid, "intent",
                                {"title": "A", "goalId": "goal-1",
                                 "derivedFromFactId": "fact-1"})
        fact = self.store.add_node(
            self.eid, "fact",
            {"intentId": self.store.add_node(
                self.eid, "intent", {"title": "A", "goalId": "goal-1"})["id"],
             "detail": "x"})
        derived = self.store.add_node(self.eid, "intent",
                                      {"title": "B", "derivedFromFactId": fact["id"]})
        edges = [e for e in self.store.list_edges(self.eid)
                 if e["targetId"] == derived["id"]]
        self.assertEqual(edges[0]["kind"], "derived_from")

    def test_fact_and_finding_need_existing_intent(self):
        with self.assertRaises(ValidationError):
            self.store.add_node(self.eid, "fact", {"intentId": "intent-99", "detail": "x"})
        with self.assertRaises(ValidationError):
            self.store.add_node(self.eid, "finding",
                                {"intentId": "intent-99", "title": "t",
                                 "severity": "high", "reproducibleSteps": ["s"]})

    def test_enum_and_type_validation(self):
        intent = self.store.add_node(self.eid, "intent", {"title": "A", "goalId": "goal-1"})
        for payload in ({"intentId": intent["id"], "detail": "x", "kind": "bogus"},
                        {"intentId": intent["id"], "title": "t", "severity": "bogus",
                         "reproducibleSteps": ["s"]}):
            with self.assertRaises(ValidationError):
                kind = "fact" if "detail" in payload else "finding"
                self.store.add_node(self.eid, kind, payload)
        with self.assertRaises(ValidationError):
            self.store.add_node(self.eid, "asset", {"type": "bogus", "value": "v"})

    def test_asset_parent_must_exist(self):
        with self.assertRaises(ValidationError):
            self.store.add_node(self.eid, "asset",
                                {"type": "endpoint", "value": "/x", "parentId": "asset-9"})

    def test_the_asset_tree_carries_the_chinese_type_word(self):
        """资产树里每条资产都带 `typeLabel`，与网页资产树、链路图、插件资产页同源。

        在这之前词表只活在 `web/app.js` 的 `ASSET_TYPE_LABEL` 里：网页写「服务」、
        插件资产页印 `service`、SVG 导出的节点标签也是 `service · 8080`——同一份数据
        三种语言。词表收口到 `models.ASSET_TYPE_LABELS` 之后，出口处只贴一次。
        """
        self.store.add_node(self.eid, "asset", {"type": "service", "value": "10.0.0.1:80"})
        tree = self.store.asset_tree(self.eid)
        by_value = {item.get("value"): item for item in tree}
        self.assertEqual(by_value["10.0.0.1:80"]["typeLabel"], "服务")
        # `type` 原样留着：它是库里的值，校验与筛选用的就是它。
        self.assertEqual(by_value["10.0.0.1:80"]["type"], "service")

    def test_an_unknown_asset_type_keeps_its_own_word(self):
        """词表里没有的类型原样回：编一个「未知类型」会把「这条数据不对」变成
        「这条没什么好看的」。库是封闭的，但历史数据不会永远守规矩。"""
        self.assertEqual(models.asset_type_label("shell"), "shell")
        self.assertEqual(models.asset_type_label(""), "")
        self.assertEqual(models.asset_type_label(None), "")
        self.assertEqual(models.asset_type_label(" SERVICE "), "服务")
    def test_the_fact_subtype_survives_the_write_path(self):
        """事实的子类型要活到库里、也得活到摊平之后。

        它原先存在 `payload.kind` 上，而 `store.add_node` 紧接着用**结构类型**
        （`fact`）覆盖了同名键、`_insert_node` 又把 `kind` 从 payload 里摘掉——
        于是每条事实入库时都丢掉了自己的子类型，报告里那一格永远印 `[fact]`。
        两个不同的问题共用一个键，两边都只能二选一，而这里两边都要。
        """
        intent = self.store.add_node(self.eid, "intent",
                                     {"title": "侦察", "goalId": "goal-1"})
        self.store.add_node(self.eid, "fact",
                            {"intentId": intent["id"], "kind": "port",
                             "detail": "开放 22/80", "confidence": 0.9})
        raw = self.store.list_nodes(self.eid, "fact")[0]
        self.assertEqual(raw["kind"], "fact", "结构类型被覆盖了")
        self.assertEqual(raw["payload"].get("factKind"), "port",
                         "子类型没进库：报告里那一格只能是结构类型")
        # 摊平之后也不许互相覆盖：`public_node` 把 payload 摊到节点上，
        # 同名键会被 payload 盖掉——这正是当初选 `factKind` 的理由。
        public = models.public_node(raw)
        self.assertEqual(public["kind"], "fact")
        self.assertEqual(public["factKind"], "port")

    def test_every_fact_subtype_has_a_chinese_word(self):
        """`FACT_KIND_LABELS` 的键集就是 `FACT_KINDS`，表外的值原样回。"""
        self.assertEqual(set(models.FACT_KIND_LABELS), set(models.FACT_KINDS))
        for kind in models.FACT_KINDS:
            word = models.fact_kind_label(kind)
            self.assertTrue(word and word != kind, "%s 的中文名还是它自己" % kind)
        self.assertEqual(models.fact_kind_label("shell"), "shell")
        self.assertEqual(models.fact_kind_label(""), "")
        self.assertEqual(models.fact_kind_label(None), "")
        self.assertEqual(models.fact_kind_label(" HTTP "), "HTTP")

    def test_goal_is_appendable_but_never_empty(self):
        """目标可以追加，但不能是空的。

        原先这里守的是「一个 engagement 只有一条 goal」。编排面进来之后这条不成立：
        目标是可编的对象（ARTEX 的任务面板就是一份 goals 列表，可增、可删、达成后标
        `met`），守住的东西只剩一件——**别写进一条空目标**。规划器照着目标派发意图，
        空目标派出来的一律是「随便看看」，事后没人能从图上看出这一轮为什么被派出去。
        """
        second = self.store.add_node(self.eid, "goal", {"target": "再拿一台"})
        self.assertEqual(second["kind"], "goal")
        self.assertNotEqual(second["id"], "goal-1",
                            "追加的目标不能复用 create_engagement 写下的那条")
        self.assertEqual(second["target"], "再拿一台")
        with self.assertRaises(ValidationError):
            self.store.add_node(self.eid, "goal", {"target": "   "})

    # ---------------------------------------------------------- 窗口语义
    def test_window_truncates_but_keeps_chain_connected(self):
        intent = self.store.add_node(self.eid, "intent", {"title": "根意图", "goalId": "goal-1"})
        # 造出 260 条事实，远超窗口
        for index in range(260):
            self.store.add_node(self.eid, "fact",
                                {"intentId": intent["id"], "detail": "fact-%d" % index})
        view = self.store.view(self.eid, limit=200)
        self.assertGreater(view["engagement"]["truncated"]["dropped"], 0)
        self.assertEqual(view["engagement"]["truncated"]["refused"], 0,
                         "我们不拒绝写入，refused 恒为 0")
        visible = {n["id"] for n in view["nodes"]["facts"]}
        visible.add(intent["id"])
        visible.add("goal-1")
        for edge in view["edges"]:
            self.assertIn(edge["sourceId"], visible, "窗口内的边不能悬空")
            self.assertIn(edge["targetId"], visible, "窗口内的边不能悬空")
        self.assertIn(intent["id"], visible, "被窗口内记录引用的锚点必须保留")

    def test_full_data_is_never_lost(self):
        intent = self.store.add_node(self.eid, "intent", {"title": "A", "goalId": "goal-1"})
        for index in range(250):
            self.store.add_node(self.eid, "fact",
                                {"intentId": intent["id"], "detail": "f%d" % index})
        self.assertEqual(len(self.store.list_nodes(self.eid, "fact")), 250,
                         "窗口只作用于视图，底层不丢数据")


class GraphTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "g.db"))
        self.eid = self.store.create_engagement("graph-target")["id"]
        self.intent = self.store.add_node(self.eid, "intent",
                                          {"title": "侦察", "goalId": "goal-1"})
        self.fact = self.store.add_node(self.eid, "fact",
                                        {"intentId": self.intent["id"], "detail": "端口开放"})
        self.derived = self.store.add_node(self.eid, "intent",
                                           {"title": "深入", "derivedFromFactId": self.fact["id"]})
        self.finding = self.store.add_node(
            self.eid, "finding",
            {"intentId": self.derived["id"], "title": "SQL 注入", "severity": "high",
             "reproducibleSteps": ["打开页面", "提交载荷"]})
        self.asset = self.store.add_node(self.eid, "asset",
                                         {"type": "app", "value": "graph-target"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _layout(self):
        view = self.store.view(self.eid)
        nodes = [view["goal"]] + sum((view["nodes"][k] for k in
                                      ("intents", "facts", "findings", "assets")), [])
        return graph.build_layout([n for n in nodes if n], view["edges"])

    def test_layers_follow_the_chain(self):
        layout = self._layout()
        position = {n["id"]: n for n in layout["nodes"]}
        self.assertLess(position["goal-1"]["x"], position[self.intent["id"]]["x"])
        self.assertLess(position[self.intent["id"]]["x"], position[self.fact["id"]]["x"])
        self.assertLess(position[self.fact["id"]]["x"], position[self.derived["id"]]["x"])

    def test_assets_are_in_their_own_band(self):
        layout = self._layout()
        position = {n["id"]: n for n in layout["nodes"]}
        chain_x = max(position[i]["x"] for i in
                      ("goal-1", self.intent["id"], self.fact["id"],
                       self.derived["id"], self.finding["id"]))
        self.assertGreater(position[self.asset["id"]]["x"], chain_x)

    def test_the_asset_node_label_uses_the_chinese_type_word(self):
        """资产节点标签是给人看的（这张图还会导出成自包含的 SVG）。

        原先拼的是 `node.type + ' · ' + node.value`，于是图上写 `app · graph-target`，
        而网页资产树写「应用」——同一份数据的两种语言。
        """
        layout = self._layout()
        position = {n["id"]: n for n in layout["nodes"]}
        label = position[self.asset["id"]]["label"]
        self.assertIn("应用", label, "资产节点的类型没走中文词表：%s" % label)
        self.assertNotIn("app ·", label, "资产节点的类型还是机器词：%s" % label)

    def test_no_node_overlap(self):
        layout = self._layout()
        boxes = [(n["x"], n["y"], n["x"] + n["w"], n["y"] + n["h"])
                 for n in layout["nodes"]]
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                overlap = not (a[2] <= b[0] or b[2] <= a[0]
                               or a[3] <= b[1] or b[3] <= a[1])
                self.assertFalse(overlap, "节点 %d 与 %d 重叠" % (i, j))

    def test_edges_reference_existing_nodes(self):
        layout = self._layout()
        known = {n["id"] for n in layout["nodes"]}
        for edge in layout["edges"]:
            self.assertIn(edge["source"], known)
            self.assertIn(edge["target"], known)

    def test_svg_render_contains_all_nodes(self):
        layout = self._layout()
        svg = graph.render_svg(layout, "测试")
        self.assertTrue(svg.startswith("<svg"))
        for node in layout["nodes"]:
            self.assertIn(node["id"], svg)
        self.assertNotIn("None", svg)

    def test_svg_escapes_markup(self):
        layout = {"width": 200, "height": 100, "edges": [],
                  "nodes": [{"id": "intent-1", "kind": "intent", "x": 0, "y": 0,
                             "w": 100, "h": 40, "label": "<script>x</script>",
                             "subtitle": "", "severity": None}]}
        svg = graph.render_svg(layout)
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)


class ReferenceTestCase(unittest.TestCase):
    def test_deref_resolves_placeholders(self):
        payload = {"intentId": {"$ref": "intent:recon"}, "detail": "x",
                   "list": [{"$ref": "a"}, 1]}
        self.assertEqual(engine.deref(payload, {"intent:recon": "intent-3", "a": "fact-1"}),
                         {"intentId": "intent-3", "detail": "x", "list": ["fact-1", 1]})

    def test_deref_rejects_unknown_reference(self):
        with self.assertRaises(ValidationError):
            engine.deref({"intentId": {"$ref": "nope"}}, {})

    def test_write_chain_orders_and_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "c.db"))
            eid = store.create_engagement("chain")["id"]
            specs = [
                {"kind": "intent", "localRef": "i1",
                 "payload": {"title": "A", "goalId": "goal-1"}},
                {"kind": "fact", "localRef": "f1",
                 "payload": {"intentId": {"$ref": "i1"}, "detail": "d"}},
                {"kind": "intent", "payload": {"title": "B",
                                               "derivedFromFactId": {"$ref": "f1"}}},
            ]
            written = engine.write_chain(store, eid, specs)
            self.assertEqual(written, {"intent": 2, "fact": 1, "finding": 0, "asset": 0})
            edges = {(e["kind"], e["sourceId"], e["targetId"])
                     for e in store.list_edges(eid)}
            self.assertIn(("spawns", "goal-1", "intent-1"), edges)
            self.assertIn(("yields", "intent-1", "fact-1"), edges)
            self.assertIn(("derived_from", "fact-1", "intent-2"), edges)
            store.close()


class ControlPlaneTestCase(unittest.TestCase):
    """控制面 HTTP 接口（真实起服务，真实发请求）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(os.path.join(cls.tmp.name, "api.db"))
        # 控制面默认要求登录，测试也走真实鉴权路径
        cls.httpd, cls.client = support.start(cls.store, WEB_DIR)
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.close()
        cls.tmp.cleanup()

    def _get(self, path):
        return self.client.get(path)

    def _post(self, path, payload, token=""):
        headers = {"Authorization": "Bearer %s" % token} if token else None
        return self.client.post(path, payload, headers)

    def test_health(self):
        status, payload = self._get("/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        for key in ("engagements", "executors", "pendingJobs", "version"):
            self.assertIn(key, payload)

    def test_engagement_lifecycle(self):
        status, created = self._post("/api/v1/engagements",
                                     {"target": "api-target", "objective": "o",
                                      "authorization": "书面许可 A-1"})
        self.assertEqual(status, 201)
        eid = created["id"]
        status, view = self._get("/api/v1/engagements/%s" % eid)
        self.assertEqual(status, 200)
        self.assertEqual(view["goal"]["target"], "api-target")
        self.assertIn("layout", view, "视图必须带服务端算好的布局")
        self.assertEqual(view["layout"]["nodes"][0]["kind"], "goal")
        status, listed = self._get("/api/v1/engagements")
        self.assertIn(eid, [e["id"] for e in listed["engagements"]])

    def test_node_write_and_validation_error(self):
        _, created = self._post("/api/v1/engagements", {"target": "api-nodes"})
        eid = created["id"]
        status, _ = self._post("/api/v1/engagements/%s/nodes" % eid,
                               {"kind": "intent", "title": "A", "goalId": "goal-1"})
        self.assertEqual(status, 201)
        status, payload = self._post("/api/v1/engagements/%s/nodes" % eid,
                                     {"kind": "finding", "intentId": "intent-1",
                                      "title": "t", "severity": "high"})
        self.assertEqual(status, 400, "缺可复现步骤必须被拒绝")
        self.assertIn("reproducibleSteps", payload["error"])

    def test_findings_assets_report_and_export(self):
        _, created = self._post("/api/v1/engagements", {"target": "api-report"})
        eid = created["id"]
        self._post("/api/v1/engagements/%s/nodes" % eid,
                   {"kind": "intent", "title": "A", "goalId": "goal-1"})
        self._post("/api/v1/engagements/%s/nodes" % eid,
                   {"kind": "finding", "intentId": "intent-1", "title": "XSS",
                    "severity": "medium", "reproducibleSteps": ["step1"]})
        status, findings = self._get("/api/v1/engagements/%s/findings" % eid)
        self.assertEqual(status, 200)
        self.assertEqual(findings["findings"][0]["title"], "XSS")
        self.assertEqual(findings["findings"][0]["reproducibleSteps"], ["step1"])
        status, assets = self._get("/api/v1/engagements/%s/assets" % eid)
        self.assertEqual(status, 200)
        self.assertEqual(assets["assets"], [])
        status, report = self._get("/api/v1/engagements/%s/report" % eid)
        self.assertIn("XSS", report["markdown"])
        with self.client.opener.open(
                self.base + "/api/v1/engagements/%s/export" % eid,
                timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn("<svg", html)
        self.assertIn("XSS", html)

    def test_the_node_endpoint_refuses_a_self_declared_job_id(self):
        """`jobId` 不接受请求体指定：溯源不由提交方决定。

        这条路径是任何登录用户都能打的，而 `store.add_node` 又是内部写入器
        （`engine.write_chain`）共用的——那边 `job_id` 由函数自己盖章，所以共用层
        没有这道闸。没它是这样：`POST /nodes` 带上 `{"jobId": "job-别人"}` 就
        能得到一条「某次真实执行跑出来的」节点，作业报告按 jobId 把节点归到那次
        执行下。
        """
        _, created = self._post("/api/v1/engagements", {"target": "api-provenance"})
        eid = created["id"]
        self._post("/api/v1/engagements/%s/nodes" % eid,
                   {"kind": "intent", "title": "A", "goalId": "goal-1"})
        status, payload = self._post("/api/v1/engagements/%s/nodes" % eid,
                                     {"kind": "finding", "intentId": "intent-1",
                                      "title": "伪造归属", "severity": "low",
                                      "reproducibleSteps": ["step1"],
                                      "jobId": "job-someone-else"})
        self.assertEqual(status, 400)
        self.assertIn("jobId", payload["error"])
        _, findings = self._get("/api/v1/engagements/%s/findings" % eid)
        self.assertEqual(findings["findings"], [], "被拒绝的写入不能留下节点")

    def test_verified_needs_a_note_but_static_hits_do_not(self):
        """标「已证实」必须写下来看到了什么；「静态命中」不强制。

        `finding_verified` 只看 `verification.verdict` 一个字段，整份报告的
        「已证实/未证实」二分就是它在撑。平台无法替使用者判断真伪，但可以让
        「已证实」必须附观测——与智能体那条 record_finding（verified=true 时必须
        给 evidence）同一条口径。反过来，静态命中本来就没有回显可贴，强求 note
        只会把人逼去编一句。
        """
        _, created = self._post("/api/v1/engagements", {"target": "api-verified"})
        eid = created["id"]
        self._post("/api/v1/engagements/%s/nodes" % eid,
                   {"kind": "intent", "title": "A", "goalId": "goal-1"})
        base = {"kind": "finding", "intentId": "intent-1", "title": "疑似 RCE",
                "severity": "critical", "reproducibleSteps": ["curl /admin"]}

        bare = dict(base, verification={"verdict": "verified", "method": "看出来的"})
        status, payload = self._post("/api/v1/engagements/%s/nodes" % eid, bare)
        self.assertEqual(status, 400)
        self.assertIn("note", payload["error"])

        static = dict(base, title="目录可列出",
                      verification={"verdict": "confirmed_static"})
        status, _ = self._post("/api/v1/engagements/%s/nodes" % eid, static)
        self.assertEqual(status, 201, "静态命中没有回显可贴，不该被要求 note")

        proven = dict(base, title="后台口令泄漏",
                      verification={"verdict": "verified", "method": "直接观测",
                                    "note": "GET /admin -> 200，明文口令在 HTML 里"})
        status, node = self._post("/api/v1/engagements/%s/nodes" % eid, proven)
        self.assertEqual(status, 201)
        self.assertEqual(node["verification"]["verdict"], "verified")
        _, findings = self._get("/api/v1/engagements/%s/findings" % eid)
        self.assertEqual(findings["counts"]["verified"], 1,
                         "只有写了观测的那条算已证实")

    def test_the_internal_writer_still_stamps_the_job_id(self):
        """收紧 HTTP 层不能把内部写入器一起卡住：任务写链仍要能盖章。

        两道闸都只加在 `add_node_api` 上，`engine.write_chain` 走的是
        `store.add_node`。这条用例钉住的就是「闸在 HTTP，不在存储层」。
        """
        _, created = self._post("/api/v1/engagements", {"target": "api-chain"})
        eid = created["id"]
        job = self.store.create_job("probe", "10.0.0.1", eid, "", {})
        engine.write_chain(self.store, eid, [
            {"kind": "intent", "localRef": "i",
             "payload": {"title": "测绘", "goalId": "goal-1"}},
            {"kind": "finding",
             "payload": {"intentId": {"$ref": "i"}, "title": "目录可列出",
                         "severity": "medium",
                         "reproducibleSteps": ["open /"],
                         "verification": {"verdict": "verified",
                                          "method": "直接观测", "note": "200 + 索引页"}}},
        ], job_id=job["id"])
        rows = self.store.list_nodes(eid, "finding")
        self.assertEqual(rows[0]["payload"].get("jobId"), job["id"])

    def test_unknown_engagement_returns_404(self):
        status, _payload = self.client.get("/api/v1/engagements/E-nope")
        self.assertEqual(status, 404)

    def test_static_index_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("<html", body.lower())

    def test_path_traversal_blocked(self):
        request = urllib.request.Request(self.base + "/../pentestd/store.py")
        try:
            with self.client.opener.open(request, timeout=10) as response:
                body = response.read().decode("utf-8", "ignore")
            self.assertNotIn("class Store", body)
        except urllib.error.HTTPError as exc:
            self.assertIn(exc.code, (403, 404))

    def test_backslash_traversal_blocked(self):
        """反斜杠穿越：只把 URL 折成正斜杠是挡不住的。

        Windows 上反斜杠也是分隔符，而 posixpath.normpath 不认它。修复前这四条里
        有两条会真的吐出文件——data/pentest.db 整库 14MB、pentestd/server.py 源码。
        断言 403 而不是 404：403 说明请求走到了归属校验，而不是碰巧文件不存在。
        """
        hostile = (
            "/x%5C..%5C..%5Cdata%5Cpentest.db",
            "/a%5C..%5C..%5Cpentestd%5Cserver.py",
            "/%2e%2e/pentestd/server.py",
            "/..%5C..%5C..%5CWindows%5Cwin.ini",
        )
        for path in hostile:
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.client.opener.open(self.base + path, timeout=10).read()
                self.assertEqual(ctx.exception.code, 403,
                                 "%s 必须被拒绝，而不是被读出来" % path)

    def test_static_assets_still_served_after_traversal_fix(self):
        """收紧路径校验不能顺手把正常资源锁死——少了 .js 前端就白屏。"""
        for path in ("/", "/app.js", "/chat.js", "/style.css"):
            with self.subTest(path=path):
                with self.client.opener.open(self.base + path, timeout=10) as response:
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.read(), "%s 不应为空" % path)

    def test_executor_registration_and_heartbeat(self):
        status, info = self._post("/api/v1/executors/register",
                                  {"name": "ut-node", "capabilities": ["scan"]})
        self.assertEqual(status, 201)
        self.assertIn("executorId", info)
        self.assertIn("token", info)
        # 心跳必须带节点令牌
        status, _ = self._post("/api/v1/executors/%s/heartbeat" % info["executorId"],
                               {"load": 2}, token=info["token"])
        self.assertEqual(status, 200)
        status, listed = self._get("/api/v1/executors")
        self.assertIn("ut-node", [e["name"] for e in listed["executors"]])

    def test_heartbeat_without_token_is_rejected(self):
        _, info = self._post("/api/v1/executors/register", {"name": "ut-noauth"})
        status, payload = self._post("/api/v1/executors/%s/heartbeat"
                                     % info["executorId"], {"load": 1})
        self.assertEqual(status, 401, "缺令牌必须被拒绝")
        self.assertIn("Authorization", payload["error"])

    def test_heartbeat_with_wrong_token_is_rejected(self):
        _, first = self._post("/api/v1/executors/register", {"name": "ut-a"})
        _, second = self._post("/api/v1/executors/register", {"name": "ut-b"})
        # 拿 B 的令牌冒充 A
        status, _ = self._post("/api/v1/executors/%s/heartbeat" % first["executorId"],
                               {"load": 1}, token=second["token"])
        self.assertEqual(status, 401, "令牌必须与节点绑定")

    def test_revoking_an_executor_requires_an_admin(self):
        """吊销节点必须管理员才能做，operator 一律 403。

        这条用例的来历值得留着：`revoke_executor` 函数体里原本没有
        `_require_admin`，而路由默认 `auth="session"`，于是**任何 operator 都能
        吊销任意执行节点**——实测用 operator 打这个接口拿到 200，节点立刻从列表里
        消失，它正在跑的任务被置为失败。任务队列是全部署共享的，所以这个洞等于
        "普通用户能反复掐断别人（含管理员）的探测"。

        用 operator 而不是"未登录"来测：未登录在路由层就被挡成 401，那条路径本来
        就走不通，测了也说明不了这个接口自己有没有把关。
        """
        status, _ = self._post("/api/v1/users", {"username": "ut-operator",
                                                 "password": "Operator-Pass1!",
                                                 "role": "operator"})
        self.assertEqual(status, 201, "造一个 operator 用于对照")
        operator = support.Client(self.base)
        status, _ = operator.login("ut-operator", "Operator-Pass1!")
        self.assertEqual(status, 200)

        _, info = self._post("/api/v1/executors/register", {"name": "ut-revoke-denied"})
        status, payload = operator.post(
            "/api/v1/executors/%s/revoke" % info["executorId"], {})
        self.assertEqual(status, 403, "operator 不应能吊销节点")
        self.assertIn("管理员", payload["error"])
        _, listed = self._get("/api/v1/executors")
        self.assertIn("ut-revoke-denied", [e["name"] for e in listed["executors"]],
                      "被拒绝的请求不能把节点删掉")

    def test_admin_can_revoke_an_executor(self):
        _, info = self._post("/api/v1/executors/register", {"name": "ut-revoke-ok"})
        status, payload = self._post(
            "/api/v1/executors/%s/revoke" % info["executorId"], {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["revoked"])
        _, listed = self._get("/api/v1/executors")
        self.assertNotIn("ut-revoke-ok", [e["name"] for e in listed["executors"]],
                         "管理员吊销后节点应真的消失")

    def test_token_is_not_stored_in_plaintext(self):
        _, info = self._post("/api/v1/executors/register", {"name": "ut-hash"})
        row = self.store.conn.execute(
            "SELECT token_hash FROM executors WHERE id=?",
            (info["executorId"],)).fetchone()
        self.assertNotEqual(row["token_hash"], info["token"],
                            "库里不得存明文令牌")
        self.assertEqual(len(row["token_hash"]), 64, "应存 sha256 摘要")
        self.assertTrue(self.store.verify_executor_token(info["executorId"],
                                                         info["token"]))

    def test_job_pull_requires_token(self):
        _, info = self._post("/api/v1/executors/register", {"name": "ut-pull"})
        # 已注册节点但不带令牌 → 401
        request = urllib.request.Request(
            self.base + "/api/v1/jobs/pull?executorId=%s" % info["executorId"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 401)
        payload = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertIn("Authorization", payload["error"])

    def test_job_created_for_new_engagement(self):
        status, payload = self._post("/api/v1/jobs", {"target": "job-target"})
        self.assertEqual(status, 201)
        self.assertIn("jobId", payload)
        self.assertIn("engagementId", payload)
        status, jobs = self._get("/api/v1/jobs")
        self.assertTrue(any(j["id"] == payload["jobId"] for j in jobs["jobs"]))
    # -------------------------------------------------- 节点身份只有管理员能造
    def test_enrolling_an_executor_requires_an_admin(self):
        """operator 不能凭空造节点身份。

        令牌是准入凭据，而任务队列是**全局**的（`claim_job` 按 kind 过滤、
        按创建时间取最早一条，不区分是谁下发的）。所以自造一个节点身份 =
        能把别人刚下发的任务领走，再用它自己的令牌回传一份"结论"——回传接口
        只校验"令牌属于领这个任务的节点"，对伪造者天然放行。

        用 operator 而不是未登录来测：未登录在路由层就 401 了，那样测不出
        这个接口自己有没有把关。
        """
        status, _ = self._post("/api/v1/users", {
            "username": "ut-enroll-operator", "password": "Operator-Pass1!",
            "role": "operator"})
        self.assertIn(status, (201, 409), "建 operator 失败：%s" % status)
        operator = support.Client(self.base)
        status, _ = operator.login("ut-enroll-operator", "Operator-Pass1!")
        self.assertEqual(status, 200)
        status, payload = operator.post("/api/v1/executors/enroll", {"name": "ut-evil"})
        self.assertEqual(status, 403, "operator 不应能创建节点身份")
        self.assertIn("管理员", payload.get("error", ""))

    def test_an_admin_can_still_enroll(self):
        status, payload = self._post("/api/v1/executors/enroll", {"name": "ut-good"})
        self.assertEqual(status, 201, payload)
        self.assertIn("token", payload)

    # ---------------------------------------- 任务载荷里的密钥只下发给节点
    def test_job_payload_never_carries_the_model_key_to_the_browser(self):
        """模型密钥必须到执行节点，但**不能**回到浏览器。

        `create_job` 把整份模型配置（含 apiKey 明文）写进 jobs.payload，
        而 `GET /api/v1/jobs` 与 `GET /api/v1/jobs/{id}` 都会把 payload 原样
        回给任何登录用户——包括 operator，也包括"只想看看这条任务跑得怎么样"
        的那个人。摘掉的方向必须是默认摘（`claim_job` 显式开口），因为
        `get_job` 有七八个调用点，漏一处就是一条完整的泄漏路径。
        """
        status, body = self._post("/api/v1/models", {
            "name": "ut-leaky", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com", "model": "deepseek-chat",
            "apiKeySource": "inline", "apiKey": "sk-ut-secret-0123456789"})
        self.assertEqual(status, 201, body)
        profile = body["profile"]
        status, _ = self._post("/api/v1/models/%s/activate" % profile["id"], {})
        self.assertEqual(status, 200)

        status, job = self._post("/api/v1/jobs", {"kind": "scan", "target": "ut-target"})
        self.assertEqual(status, 201, job)
        job_id = job["jobId"]

        status, detail = self._get("/api/v1/jobs/%s" % job_id)
        self.assertEqual(status, 200)
        self.assertNotIn("sk-ut-secret-0123456789", json.dumps(detail),
                         "任务详情不得回传模型密钥")
        status, listing = self._get("/api/v1/jobs")
        self.assertEqual(status, 200)
        self.assertNotIn("sk-ut-secret-0123456789", json.dumps(listing),
                         "任务列表不得回传模型密钥")
        model = (((detail.get("job") or {}).get("payload") or {}).get("model") or {})
        self.assertEqual("", model.get("apiKey") or "",
                         "应当显式置空而不是删掉这个键——删了看不出「这里本来有配置」")
        self.assertTrue(model.get("hasApiKey"),
                        "要能区分「没配密钥」与「配了但不给你看」")

        # 节点那条路必须仍然是明文，否则扫描真的会跑不动。
        # 这里直接读 `with_secrets=True`，而不是走 `claim_job`：队列是全局的，
        # 领到的不一定是刚建的那一条；而两条路读同一个方法，验一条就够。
        pulled = self.store.get_job(job_id, with_secrets=True)
        self.assertIsNotNone(pulled)
        self.assertEqual("sk-ut-secret-0123456789",
                         ((pulled.get("payload") or {}).get("model") or {}).get("apiKey"),
                         "执行节点必须拿得到明文密钥")


@unittest.skipUnless(ENGINE_HOME, "未找到漏洞发掘引擎（vulnagent）")
class ExecutorTestCase(unittest.TestCase):
    """执行面：本地节点与远程节点两条路径。"""

    def test_local_executor_writes_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "local.db"))
            eid = store.create_engagement(VULNAPPL, "本机扫描", "测试授权")["id"]
            job = store.create_job("scan", VULNAPPL, eid,
                                   os.path.join(ENGINE_HOME, "demo", "vulnapp_patched"))
            executor = agent_module.LocalExecutor(store, work_root=tmp, verbose=False)
            self.assertTrue(executor.run_once())
            finished = store.get_job(job["id"])
            self.assertEqual(finished["status"], "done")
            counts = store.get_engagement_meta(eid)["counts"]
            self.assertEqual(counts["findings"], 6)
            self.assertGreater(counts["intents"], 0)
            self.assertGreater(counts["facts"], 0)
            self.assertEqual(counts["assets"], 1)
            store.close()

    def test_remote_executor_full_path(self):
        """起控制面 → 注册节点 → 建任务 → 领任务 → 执行 → 回传 → 链路落库。

        注意分工：**建任务是控制台动作**（需要登录会话），节点只负责领任务与回传。
        节点拿着自己的令牌去建任务会被拒——这正是应该的。
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "remote.db"))
            httpd, console = support.start(store, WEB_DIR)
            port = httpd.server_address[1]
            client = agent_module.ControlClient("http://127.0.0.1:%d" % port)
            try:
                # 节点令牌建不了任务
                status, _ = client.create_job(VULNAPPL)
                self.assertEqual(status, 401, "建任务是控制台动作，节点不该能建")
                # 控制台建任务
                status, created = console.post("/api/v1/jobs", {
                    "target": VULNAPPL,
                    "patched": os.path.join(ENGINE_HOME, "demo", "vulnapp_patched"),
                    "objective": "远程节点执行"})
                self.assertEqual(status, 201)
                node = agent_module.RemoteExecutor(client, "ut-remote",
                                                   work_root=tmp, verbose=False)
                self.assertTrue(node.register())
                self.assertTrue(node.run_once(), "应领取并执行任务")
                finished = store.get_job(created["jobId"])
                self.assertEqual(finished["status"], "done")
                self.assertEqual(store.get_engagement_meta(
                    created["engagementId"])["counts"]["findings"], 6)
                self.assertFalse(node.run_once(), "没有更多任务时应返回 False")
            finally:
                httpd.shutdown()
                httpd.server_close()
                store.close()


class EngineTranslationTestCase(unittest.TestCase):
    def test_chain_specs_shape(self):
        result = {
            "target": {"name": "t", "root": "/tmp/t", "file_count": 2, "loc": 20,
                       "hash": "abc"},
            "slices": [{"slice_id": "S-1", "path": "/tmp/t/a.py", "symbol": "f",
                        "start_line": 1, "end_line": 5,
                        "graph_context": ["ctx"], "risk_score": 2.0}],
            "hypotheses": [{"hyp_id": "H-1", "slice_id": "S-1", "cwe": "CWE-89",
                            "title": "SQL 注入", "file": "/tmp/t/a.py", "line": 3,
                            "confidence": 0.8, "status": "verified",
                            "rationale": "拼接 SQL"}],
            "findings": [{"finding_id": "F-1", "cwe": "CWE-89", "title": "SQL 注入",
                          "severity": "high", "file": "/tmp/t/a.py", "line": 3,
                          "symbol": "f", "impact": "读取任意数据",
                          "remediation": "参数化查询",
                          "evidence_chain": [],
                          "verification": {"verdict": "verified", "mode": "dynamic",
                                           "poc_path": "poc_H-1.py",
                                           "poc_source": "print(1)",
                                           "target_runs": [
                                               {"target": "vulnerable",
                                                "parsed": {"rows": 2}},
                                               {"target": "patched",
                                                "parsed": {"rows": 0}}],
                                           "observed": {"effect_on_vulnerable": True},
                                           "note": "差分成立"}}],
            "chains": [{"chain_id": "C-1", "steps": ["F-1"], "narrative": "组合利用",
                        "severity": "critical"}],
            "incremental": {"enabled": False},
        }
        specs = engine.build_chain_specs(result)
        kinds = [s["kind"] for s in specs]
        self.assertEqual(kinds.count("finding"), 1)
        self.assertIn("asset", kinds)
        # 意图必须先于由它产出的事实/漏洞出现，否则写入时锚点不存在
        first_fact = kinds.index("fact")
        self.assertLess(kinds.index("intent"), first_fact)
        anchor_edges = [(kinds.index("intent"), first_fact)]
        for before, after in anchor_edges:
            self.assertLess(before, after)
        steps = [s for s in specs if s["kind"] == "finding"][0][
            "payload"]["reproducibleSteps"]
        self.assertGreaterEqual(len(steps), 1)
        self.assertTrue(any("沙箱" in s for s in steps))

    def test_static_finding_gets_manual_steps(self):
        result = {"target": {"name": "t", "root": "/tmp/t"},
                  "slices": [], "hypotheses": [], "chains": [],
                  "findings": [{"finding_id": "F", "cwe": "CWE-798",
                                "title": "硬编码凭据", "severity": "high",
                                "file": "/tmp/t/a.py", "line": 1, "symbol": "x",
                                "evidence_chain": [{"kind": "static", "ref": "a.py:1"}],
                                "verification": {"verdict": "confirmed_static",
                                                 "mode": "static"}}]}
        specs = engine.build_chain_specs(result)
        steps = [s for s in specs if s["kind"] == "finding"][0][
            "payload"]["reproducibleSteps"]
        self.assertTrue(any("人工复核" in s for s in steps))


class PageSizeGuardTestCase(unittest.TestCase):
    """列表接口的 `limit` 必须有上下界。**`-1` 是这条规则的全部理由。**

    SQLite 把 `LIMIT -1` 读作"不设上限"。因此一个只看"能不能转成 int"的解析器会把
    `?limit=-1` 放行成"把这张表整份读出来"——而它比默认值还短一个字符，触发方式
    与正常翻页毫无区别。`?limit=999999` 是同一件事的另一种写法：不被拒绝，只是慢。

    两条路都要钉住：① HTTP 层（`_page_size`）；② `store` 层（`_row_limit`）——
    因为 `tool_ops()` / `plugin_ops()` 也被 CLI 与内部任务直接调用，那一侧没有
    查询串可夹。只守住其中一层，另一层的调用方仍然能传进一个负数。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(os.path.join(cls.tmp.name, "page.db"))
        cls.httpd, cls.client = support.start(cls.store, WEB_DIR)
        # 行数要**超过** `PAGE_MAX`，否则"夹到上限"与"数据就这么多"分不开，
        # 断言会通过而缺陷还在。250 > 200。
        for index in range(250):
            cls.store.record_tool_op("install", "adapter-%02d" % index,
                                     channel="zip", source="s")
            cls.store.record_plugin_op("install", "pkg-%02d" % index, "prof", "u1")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.close()
        cls.tmp.cleanup()

    def _ops_count(self, path):
        status, payload = self.client.get(path)
        self.assertEqual(status, 200, "%s 应当成功" % path)
        return len(payload["ops"])

    def test_negative_limit_falls_back_to_the_default(self):
        """`-1` 必须等价于"没写"，而不是"全部"。"""
        self.assertEqual(self._ops_count("/api/v1/tools/ops"),
                         self._ops_count("/api/v1/tools/ops?limit=-1"))
        self.assertEqual(self._ops_count("/api/v1/hub/ops"),
                         self._ops_count("/api/v1/hub/ops?limit=-1"))

    def test_an_oversized_limit_is_capped_not_refused(self):
        """夹到上限而不是回 400：翻页写大一点是笔误，不该变成一次报错。"""
        self.assertEqual(server_module.PAGE_MAX,
                         self._ops_count("/api/v1/tools/ops?limit=999999"))

    def test_zero_and_garbage_fall_back_to_the_default(self):
        for query in ("?limit=0", "?limit=abc", "?limit="):
            with self.subTest(query=query):
                self.assertEqual(self._ops_count("/api/v1/tools/ops"),
                                 self._ops_count("/api/v1/tools/ops" + query))

    def test_store_clamps_before_the_sql_ever_sees_it(self):
        """绕开 HTTP 的那条路：CLI 与内部任务调的就是这两个函数。"""
        self.assertEqual(30, len(self.store.tool_ops(-1)))
        self.assertEqual(30, len(self.store.plugin_ops(-1)))
        self.assertEqual(200, len(self.store.tool_ops(10 ** 9)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
