"""合并链路视图（跨全部 engagement）、会话归属自愈，以及 `/graph` 的参数形状。

共同点是：**做错了不会以它自己的名义报错**。同名节点塌成一个、窗口把账丢了一条、
会话归属没连上——全都表现为界面上少了一点内容，而所有手工检查都会通过；而 `nodes`
参数认错了形状时，报出来的是"链路加载失败：nodes 必须是整数、all，或留空"——
一句听起来像**用户输入有问题**的话，实际是接口两边各自都对、只是没接上。所以这里
的用例断言的是**不变量**，不是"函数返回了点东西"。

依赖：`support.temporary_store`（临时库 + 自动清理）、`support.start`（真起 HTTP）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pentestd import graph, server as server_module                  # noqa: E402
from pentestd import store as store_module                           # noqa: E402
from pentestd.store import Store                                     # noqa: E402

from tests import support                                            # noqa: E402


def _populate(store: Store, target: str) -> str:
    """在一个 engagement 里造一条完整链路，**局部 id 刻意与别处重名**。

    重名是关键：`nodes` 的主键是 `(engagement_id, id)`，所以 `asset-1` 在每个
    engagement 里都各有一份（真实库里 1207 个节点只有 138 个不同 id）。
    """
    eid = store.create_engagement(target, "合并视图测试", "ENG-TEST")["id"]
    intent = store.add_node(eid, "intent", {"title": "侦察 " + target,
                                            "goalId": "goal-1"})
    fact = store.add_node(eid, "fact", {"intentId": intent["id"],
                                        "detail": "端口开放 " + target})
    store.add_node(eid, "finding",
                   {"intentId": intent["id"], "title": "漏洞 " + target,
                    "severity": "high",
                    "reproducibleSteps": ["打开页面", "提交载荷"]})
    store.add_node(eid, "asset", {"type": "app", "value": target})
    store.add_node(eid, "asset", {"type": "service", "value": target + ":80",
                                  "parentId": "asset-1"})
    return eid


class MergedViewTestCase(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("merged.db")
        self.first = _populate(self.store, "10.0.0.5")
        self.second = _populate(self.store, "10.0.0.9")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def _view(self, limit=0):
        return self.store.combined_view(limit)

    @staticmethod
    def _all_nodes(view):
        nodes = list(view["goals"])
        for key in ("intents", "facts", "findings", "assets"):
            nodes.extend(view["nodes"][key])
        return nodes

    # ------------------------------------------------------------ 不塌名

    def test_same_local_id_in_two_engagements_stays_two_nodes(self):
        """两个目标里的 `asset-1` 必须是**两个**节点，不是一个。

        这是合并视图存在的全部理由。塌成一个之后图仍然画得出来、边也仍然连得上
        ——只是连错了地方，而图上没有任何一处会显示"这里出错了"。
        """
        view = self._view()
        ids = [node["id"] for node in self._all_nodes(view)]
        self.assertEqual(len(ids), len(set(ids)), "合并视图里出现了重复 id")
        local = [node_id.split(":", 1)[1] for node_id in ids]
        self.assertGreater(len(local) - len(set(local)), 0,
                           "测试数据本身没有重名，这条用例什么也没验证到")
        for eid in (self.first, self.second):
            self.assertIn("%s:asset-1" % eid, ids)

    def test_every_node_carries_its_engagement(self):
        """每个节点都要自报归属。

        `engagement` 字段本身在合并视图里已经不存在，所以这是界面上回答
        "这条证据是哪个目标的"唯一依据（节点详情面板读它）。
        """
        view = self._view()
        for node in self._all_nodes(view):
            self.assertIn(node.get("engagementId"), (self.first, self.second),
                          "节点没有归属：%s" % node["id"])
            self.assertTrue(node.get("engagementTarget"),
                            "节点没有带上目标地址：%s" % node["id"])

    def test_reference_fields_point_at_namespaced_nodes(self):
        """锚点字段必须也加前缀，否则资产树会掉节点、详情面板静默查不到东西。"""
        view = self._view()
        known = {node["id"] for node in self._all_nodes(view)}
        checked = 0
        for node in self._all_nodes(view):
            for field in ("intentId", "parentId", "affectedAssetId", "goalId"):
                value = node.get(field)
                if not value:
                    continue
                checked += 1
                self.assertIn(value, known,
                              "%s.%s 指向不存在的节点 %s" % (node["id"], field, value))
                self.assertNotEqual(value, value.split(":", 1)[-1],
                                    "%s.%s 没有加 engagement 前缀：%s"
                                    % (node["id"], field, value))
        self.assertGreater(checked, 0, "测试数据里没有任何锚点字段")

    def test_edges_never_dangle(self):
        view = self._view()
        known = {node["id"] for node in self._all_nodes(view)}
        for edge in view["edges"]:
            self.assertIn(edge["sourceId"], known, "边指向不存在的源节点：%s" % edge["id"])
            self.assertIn(edge["targetId"], known, "边指向不存在的目标节点：%s" % edge["id"])

    def test_layout_places_every_visible_node(self):
        """布局必须给每个可见节点坐标。

        `build_layout` 对拿不到坐标的节点**静默丢弃**（只保留两端都有位置的边），
        所以"图上少一个节点"和"那个节点不存在"从画面上分不出来。
        """
        view = self._view()
        view["layout"] = graph.build_layout(self._all_nodes(view), view["edges"])
        placed = {node["id"] for node in view["layout"]["nodes"]}
        for node in self._all_nodes(view):
            self.assertIn(node["id"], placed, "布局漏掉了节点：%s" % node["id"])

    # ---------------------------------------------------------- 窗口不丢账

    def test_nothing_is_lost_without_a_window(self):
        """不设窗口就**什么都不该缺**：`shown` 必须等于总数。

        差一条也是错——它是那种"数字对不上、又查不出差在哪"的问题，而诱因通常是
        一个只有 goal、还没做过任何工作的 engagement 被窗口逻辑顺手丢掉了。
        """
        view = self._view(0)
        self.assertEqual(view["truncated"]["dropped"], 0)
        self.assertEqual(view["totals"]["shown"], view["totals"]["nodes"])
        self.assertEqual(len(self._all_nodes(view)), view["totals"]["nodes"])
        self.assertEqual(len(view["goals"]), len(self.store.list_engagements()),
                         "每个 engagement 都要有自己的 goal")

    def test_window_reports_what_it_dropped(self):
        view = self._view(4)
        self.assertGreater(view["truncated"]["dropped"], 0, "窗口没有生效")
        self.assertEqual(view["totals"]["nodes"],
                         view["totals"]["shown"] + view["truncated"]["dropped"],
                         "shown + dropped 必须等于总数，否则界面上的横幅会撒谎")

    def test_window_keeps_the_goal_of_every_engagement_it_shows(self):
        """被展示的每个目标都要带上自己的 goal。

        少了它，那些被切掉前半段的链路在图上是一堆悬空的意图——看上去像链路
        本来就断在那里。
        """
        view = self._view(4)
        shown = {node["engagementId"] for node in self._all_nodes(view)}
        goals = {goal["engagementId"] for goal in view["goals"]}
        self.assertTrue(shown <= goals,
                        "这些目标在窗口里却没有 goal：%s" % sorted(shown - goals))


class SessionEngagementHealTestCase(unittest.TestCase):
    """会话归属自愈：从会话**自己产出过的证据**里反推它属于哪个目标。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("heal.db")
        self.eid = _populate(self.store, "10.0.0.5")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def _reopen(self):
        """关掉再打开：自愈发生在 `Store.__init__`，只重开才验证得到它。"""
        self.store.close()
        self.store = Store(os.path.join(self.directory.name, "heal.db"))
        return self.store

    def _session(self, engagement_id="", with_evidence=True):
        meta = self.store.create_chat_session("对话", "u1", engagement_id)
        payload = {"text": "扫一下"}
        if with_evidence:
            payload["trace"] = [{"tool": "start_scan",
                                 "data": {"jobId": "job-1", "engagementId": self.eid}}]
        self.store.append_chat_message(meta["id"], "assistant", payload)
        return meta["id"]

    def test_unassigned_session_adopts_the_engagement_from_its_own_messages(self):
        session_id = self._session()
        self.assertEqual(self.store.get_chat_session(session_id)["engagementId"], "")
        self.store = self._reopen()
        self.assertEqual(self.store.get_chat_session(session_id)["engagementId"], self.eid)

    def test_heal_is_idempotent(self):
        session_id = self._session()
        self.store = self._reopen()
        first = self.store.get_chat_session(session_id)["engagementId"]
        self.store = self._reopen()
        self.assertEqual(self.store.get_chat_session(session_id)["engagementId"], first)

    def test_heal_never_overwrites_an_existing_engagement(self):
        """已有归属**绝不覆盖**：归属是"这条对话当初是为谁开的"，不该漂移。"""
        other = _populate(self.store, "10.0.0.9")
        session_id = self._session(engagement_id=other)
        self.store = self._reopen()
        self.assertEqual(self.store.get_chat_session(session_id)["engagementId"], other,
                         "自愈覆盖了一个已有的归属")

    def test_heal_leaves_sessions_without_evidence_alone(self):
        """问过"有哪些执行节点"这类对话没有产出证据，反推不出来就保持未归属。

        猜一个归属比留空更糟：它会出现在某个目标的对话列表里，而那个目标
        跟这条对话毫无关系。
        """
        session_id = self._session(with_evidence=False)
        self.store = self._reopen()
        self.assertEqual(self.store.get_chat_session(session_id)["engagementId"], "")

    def test_heal_ignores_engagements_that_no_longer_exist(self):
        session_id = self._session()
        self.store.conn.execute("DELETE FROM engagements WHERE id=?", (self.eid,))
        self.store.conn.commit()
        self.store = self._reopen()
        self.assertEqual(self.store.get_chat_session(session_id)["engagementId"], "",
                         "自愈把一个已经不存在的目标填了进去")


class MergePrimitiveTestCase(unittest.TestCase):
    def test_merged_id_round_trips_on_the_engagement_part(self):
        """前缀里的分隔符不能与 engagement 自身的分隔符同形。

        用 `-` 拼的话 `E-ab12cd34-asset-1` 无法可靠还原出 `E-ab12cd34`，
        而还原不出来就无从知道这个节点属于哪个目标。
        """
        merged = store_module.merged_id("E-ab12cd34", "asset-1")
        self.assertEqual(merged.split(":", 1)[0], "E-ab12cd34")
        self.assertEqual(merged.split(":", 1)[1], "asset-1")

    def test_first_engagement_id_finds_it_at_any_depth(self):
        finder = store_module.first_engagement_id
        self.assertEqual(finder({"engagementId": "E-1"}), "E-1")
        self.assertEqual(finder({"trace": [{"data": {"engagementId": "E-2"}}]}), "E-2")
        self.assertEqual(finder({"a": [{"b": {"c": [{"engagementId": "E-3"}]}}]}), "E-3")
        self.assertEqual(finder({"engagementId": "   "}), "", "空白不算命中")
        self.assertEqual(finder({"nothing": "here"}), "")


class QueryParameterShapeTestCase(unittest.TestCase):
    """查询参数的**形状**只在一处认：`server._query_text`。

    `_dispatch` 用 `parse_qs` 填 `ctx["query"]`（值是列表），而测试与内部调用方
    常常直接塞裸字符串。两种形状的差异不会以它自己的名义报出来：
    `(query.get(key) or [""])[0]` 遇到字符串取的是**第一个字符** ——
    `{"limit": "50"}` 变成 `5`，一个看起来完全正常的页大小。
    """

    def test_a_bare_string_is_never_truncated_to_its_first_character(self):
        """裸字符串必须原样取出，不能被 `[0]` 切成首字符。

        `"50"` 若变成 `"5"`，列表只给 5 条而不是 50 条，且没有任何地方会报错；
        使用者只会觉得"这一页怎么这么短"。
        """
        self.assertEqual(server_module._query_text({"limit": "50"}, "limit"), "50")
        self.assertEqual(server_module._query_text({"nodes": "200"}, "nodes"), "200")
        self.assertEqual(server_module._int_of({"limit": "50"}, "limit", 30), 50)

    def test_a_list_takes_its_first_value(self):
        """`parse_qs` 的产物 `{"nodes": ["200"]}` 要取出 `"200"`。"""
        self.assertEqual(server_module._query_text({"nodes": ["200", "all"]},
                                                   "nodes"), "200")
        self.assertEqual(server_module._int_of({"limit": ["50"]}, "limit", 30), 50)

    def test_unusable_values_degrade_to_empty(self):
        """取不到、给 `None`、给空列表都给空串。

        容器类型刻意不当字符串用：`str({...})` 会得到 `"{'a': 1}"`，一个
        看起来像参数值、实际是 Python 字面量的东西，比空串难查得多。
        """
        for query in ({}, None, {"q": None}, {"q": []}, {"q": ()}, {"q": {}},
                      {"q": set()}, {"other": ["x"]}):
            with self.subTest(query=query):
                self.assertEqual(server_module._query_text(query, "q"), "")

    def test_int_of_falls_back_to_its_default(self):
        """写歪了回落到默认值 —— 这一条 `_int_of` 与 `get_graph` 的取舍**故意相反**。"""
        self.assertEqual(server_module._int_of({}, "limit", 30), 30)
        self.assertEqual(server_module._int_of({"limit": ["abc"]}, "limit", 30), 30)
        self.assertEqual(server_module._int_of({"limit": [""]}, "limit", 30), 30)


class GraphQueryParameterTestCase(unittest.TestCase):
    """`/api/v1/graph?nodes=…` 的取值形式与窗口语义。

    这一组**走真实 HTTP**（socket → `_dispatch` → `parse_qs` → `get_graph`），不是
    直接调处理函数 —— 要钉的恰恰是**传输层给的形状**。

    这个处理函数曾经按裸字符串读 `ctx["query"].get("nodes")`，于是
    `str(["200"]) == "['200']"`、`int()` 必然失败：`nodes=200` 与 `nodes=all`
    **两种取值都被 400 拒掉**。前端**永远**会带上这个参数（`app.js` 的 `loadGraph`
    固定拼 `?nodes=all`），所以链路页每一次加载都失败、
    顶栏计数退回 `—`，而屏幕上写的是"链路加载失败：nodes 必须是整数、all，或留空"
    ——一句读起来像**用户把参数写错了**的话。

    一个自己捏 ctx 去调处理函数的用例**看不见这个缺陷**，因为那种 ctx 的形状永远
    是对的。这就是这一组必须连 HTTP 一起测的理由。
    """

    def setUp(self):
        self.directory, self.store = support.temporary_store("graphquery.db")
        # 必须造够节点：`nodes=8` 要真的切掉东西，否则"窗口根本没生效"的实现
        # 在这组用例上也会全绿。
        for offset in range(3):
            _populate(self.store, "10.0.0.%d" % (5 + offset))
        self.httpd, self.client = support.start(self.store, support.WEB_DIR)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def _graph(self, query_string):
        suffix = ("?" + query_string) if query_string else ""
        return self.client.get("/api/v1/graph" + suffix)

    def _window(self, query_string):
        status, payload = self._graph(query_string)
        self.assertEqual(status, 200, "nodes=%r 被拒：%s" % (query_string, payload))
        return payload["totals"], payload["truncated"]

    # ---------------------------------------------------------- 取值形式

    def test_every_documented_form_is_accepted(self):
        """整数、`all`、`0`、`-1`、留空、不带 —— 六种都是接口允许的取值。

        留空与不带在 `parse_qs` 之后没有区别（空值会被丢掉），但调用方会写这两种，
        所以两种都钉。**六种曾经全部是 400**，唯一过得去的是"不带"。
        """
        for query_string in ("nodes=8", "nodes=all", "nodes=0", "nodes=-1",
                             "nodes=", ""):
            with self.subTest(query=query_string):
                self._window(query_string)

    def test_a_window_really_cuts_the_graph(self):
        """`nodes=8` 必须真的切掉东西，并按账本报告丢了几个。

        刻意**不**断言 `shown == 8`：窗口限的是**非 goal 节点池**，被切到的
        engagement 的 goal、以及锚点引用到的源节点都会被额外保留（见
        `store.combined_view` 的两条保留规则），所以 `shown` 允许大于 limit。
        第一版就是这么断的，实测 `shown=11` —— 那条断言钉的是一个并不存在的契约，
        而它会以"实现错了"的名义挂掉一个正确的实现。
        """
        totals, truncated = self._window("nodes=8")
        full_totals, _ = self._window("nodes=all")
        self.assertGreater(truncated["dropped"], 0, "窗口没有生效")
        self.assertLess(totals["shown"], full_totals["shown"],
                        "窗口没有切掉任何可见节点")
        self.assertEqual(totals["nodes"], totals["shown"] + truncated["dropped"],
                         "shown + dropped 必须等于总数，否则横幅会撒谎")

    def test_the_window_budgets_non_goal_nodes_only(self):
        """`dropped` 必须**正好**是"非 goal 节点总数 − limit"。

        这是窗口真正的语义：它比 `shown == limit` 弱，比"看起来少了点东西"强，
        而且不依赖测试数据里有多少个 engagement。
        """
        full_status, full = self._graph("nodes=all")
        self.assertEqual(full_status, 200)
        non_goal = full["totals"]["nodes"] - len(full["goals"])
        self.assertGreater(non_goal, 8, "测试数据不够大，这条什么也没验证到")

        status, windowed = self._graph("nodes=8")
        self.assertEqual(status, 200)
        self.assertEqual(windowed["truncated"]["dropped"], non_goal - 8)
        self.assertGreaterEqual(windowed["totals"]["shown"], 8,
                                "窗口里至少要有那 8 个节点")

    def test_the_unlimited_forms_lose_nothing(self):
        """`all` / `0` / `-1` 都必须等价于不设上限。

        三个别名是给三种调用者写的（页面上的"全部"、命令行习惯的 `0`、以及把
        `-1` 当"无限"的库），它们必须落到同一个窗口上。
        """
        for query_string in ("nodes=all", "nodes=0", "nodes=-1"):
            with self.subTest(query=query_string):
                totals, truncated = self._window(query_string)
                self.assertEqual(truncated["dropped"], 0, "不设上限却丢了节点")
                self.assertEqual(totals["shown"], totals["nodes"])

    def test_a_larger_window_shows_at_least_as_much_as_a_smaller_one(self):
        """窗口单调：8 个节点的窗口不能比 3 个的看到更少。"""
        small, _ = self._window("nodes=3")
        large, _ = self._window("nodes=8")
        self.assertLessEqual(small["shown"], large["shown"])

    # ---------------------------------------------------------- 写歪了

    def test_garbage_is_still_rejected_loudly(self):
        """写歪的值仍然 400，而且要说清是哪一个参数。

        这一条与上面几条是**成对**的。修好形状之后很容易顺手把校验放宽成
        "_int_of 那样看不懂就用默认值"：那样 `nodes=abc` 会安静地给出一张按默认
        窗口截过的图，而调用方以为自己要的是别的东西 —— 界面上看不出它没照办。
        """
        status, payload = self._graph("nodes=abc")
        self.assertEqual(status, 400)
        self.assertIn("nodes", payload.get("error", ""))

    def test_the_default_window_is_a_window_not_a_refusal(self):
        """不传参数要给一张图，不是错误。

        默认值本身是 `store.MERGED_WINDOW`（读常量，不在用例里抄数字）；这里钉的是
        "省略参数走的是窗口这条路"，而不是那个数字。测试数据小于任何合理窗口，
        所以此时一个节点都不该丢。
        """
        totals, truncated = self._window("")
        self.assertEqual(truncated["dropped"], 0)
        self.assertEqual(totals["shown"], totals["nodes"])
        self.assertGreater(totals["nodes"], 3, "测试数据不够大，这条什么也没验证到")


if __name__ == "__main__":
    unittest.main(verbosity=2)
