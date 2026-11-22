# -*- coding: utf-8 -*-
"""侧边栏读数的实时刷新（`/pulse`）。

报上来的缺陷是**目标和对话记录不能实时刷新**：这两个读数块此前只在启动、切页签、
以及控制台自己下发任务之后才刷新。于是"对话里让模型建了个目标 / 跑完一次产出证据 /
开了新会话"在侧边栏上看不见，要手动刷整页才出来——而让模型干活、人看侧边栏恰恰是
这套系统的主要用法。

修法不是"每 3 秒重拉两个列表"，是先探**变了没有**再决定拉哪一份。所以本文的重心
不在"能不能拉到"，而在两条容易被写歪的性质：

1. **没变的时候指纹必须一模一样。** 指纹只要自己会漂，前端就会每 3 秒重画一次列表
   容器，而两个列表里都是按钮——表现是"鼠标停在哪一行哪一行就闪"，以及点不下去。
   `test_同一个状态连着问两次给出同一串指纹` 是这一条的地基。
2. **变了的那一块才动。** 目标多了一个节点时 `engagements` 那块不该变，否则每次任务
   都会让目标列表整体重画，而目标的标题根本没变。
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import server                                                # noqa: E402
from tests import support                                                  # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


def _source(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def _body(text, marker, end="\n}"):
    """从源码里切出一个函数体（含缩进）——契约测试用，不执行它。"""
    start = text.index(marker)
    stop = text.index(end, start)
    return text[start:stop]


class ChangeStampsTestCase(unittest.TestCase):
    """指纹的语义：变了会变，没变**一定**不变。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("pulse.db")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_同一个状态连着问两次给出同一串指纹(self):
        """整个机制的地基。

        指纹自己会漂的话，前端每 3 秒重画一次列表——而"列表在闪"看起来像界面问题，
        不像判据问题，于是没人会去查 `change_stamps`。
        """
        self.assertEqual(self.store.change_stamps(), self.store.change_stamps())
        eid = self.store.create_engagement("10.0.0.1")["id"]
        sid = self.store.create_chat_session("t", "", eid, "", "", "")["id"]
        self.store.append_chat_message(sid, "user", {"text": "hi"})
        first = self.store.change_stamps(sid)
        self.assertEqual(first, self.store.change_stamps(sid),
                         "把每一块都写过一遍之后再问，也必须一模一样")
        # 值本身也要看得出是什么，否则"两个空字典相等"这种退化也能让上一句通过。
        self.assertTrue(first["engagements"].startswith("n=1 m="))
        self.assertTrue(first["nodes"].startswith("n=1 "))
        # 会话那一行**两个信号都要在**：改名是 UPDATE，它不动 rowid，只有那一半
        # 时间戳看得见；反过来新建 / 删除动 rowid。少任何一个都会漏掉一种变化。
        self.assertTrue(first["sessions"].startswith("n=1 m="),
                        "会话指纹缺了行号那一半")
        self.assertIn("u=", first["sessions"], "会话指纹缺了时间戳那一半")
        self.assertTrue(first["messages"].startswith("n=1 s="))

    def test_a_new_engagement_moves_only_the_engagements_stamp(self):
        before = self.store.change_stamps()
        self.store.create_engagement("10.0.0.1")
        after = self.store.change_stamps()
        self.assertNotEqual(before["engagements"], after["engagements"])
        self.assertEqual(before["sessions"], after["sessions"])
        # 新建 engagement 会连带写入它那条 goal 节点，所以 nodes 那块**应该**变
        # ——把这条写出来是为了让上面那句"只有 engagements 变"的措辞不外溢到
        # nodes 上（真写成"只有它变"就是错的）。
        self.assertNotEqual(before["nodes"], after["nodes"])

    def test_new_evidence_moves_only_the_nodes_stamp(self):
        eid = self.store.create_engagement("10.0.0.1")["id"]
        before = self.store.change_stamps()
        self.store.add_node(eid, "asset", {"value": "https://10.0.0.1/"})
        after = self.store.change_stamps()
        self.assertNotEqual(before["nodes"], after["nodes"])
        self.assertEqual(before["engagements"], after["engagements"],
                         "目标本身没变，它那一块不该动——动了就是每次任务都重画目标列表")
        self.assertEqual(before["sessions"], after["sessions"])

    def test_a_new_edge_always_comes_with_the_nodes_stamp(self):
        """链路里那根边**不可能**在节点指纹不动的情况下多出来。

        边只在 `add_node` 里、与它那个节点同一个事务落库（`models.edges_for` 是
        唯一的 `INSERT INTO edges`），所以 `nodes` 那块（行数 + 最大 rowid）已经把
        "多了一条边"一起盖住，不需要单独一格 `edges` 指纹。

        把这条前提钉在测试里，是因为它只在"边真的只跟节点一起出现"时成立：哪天
        有人在别处补一条边（旧数据接线、把两个已存在的节点连起来），链路会在**节点
        数与最大行号都没变**的情况下少画/多画一根边，而没有任何指纹会动。
        """
        eid = self.store.create_engagement("10.0.0.1")["id"]
        before = self.store.change_stamps()
        self.store.add_node(eid, "intent", {"title": "先摸一下", "goalId": "goal-1"})
        after = self.store.change_stamps()
        self.assertGreater(len(self.store.list_edges(eid)), 0,
                           "这个意图本该带一条 spawns 边")
        self.assertNotEqual(before["nodes"], after["nodes"],
                            "边落库了，节点指纹却没动——链路会漏掉这根边")

    def test_a_new_session_moves_the_sessions_stamp(self):
        before = self.store.change_stamps()
        self.store.create_chat_session("t", "", "", "", "", "")
        after = self.store.change_stamps()
        self.assertNotEqual(before["sessions"], after["sessions"])
        self.assertEqual(before["engagements"], after["engagements"])

    def test_a_new_message_moves_both_session_stamps(self):
        """一轮对话落一条消息 → 侧边栏那一行的"N 条"要跟着长。

        它靠的是 `append_chat_message` 顺手推 `chat_sessions.updated_at`。那一句
        哪天被去掉，症状是"N 条"停在打开时那个数字——而列表本身看着完全正常。

        中间那 2 毫秒与下面改名那条是同一个理由（也是同一个 `updated_at` 毫秒
        分辨率）：`create_chat_session` 与 `append_chat_message` 在同进程里连着跑，
        很容易落在同一毫秒里，于是两个指纹**本来就一样**——不等，断言的就是产品的
        分辨率，而不是"落一条消息会不会被发现"。第一次撞上时它是这样红的：

            AssertionError: 'n=1 m=1 u=1792150328.265000'
                         == 'n=1 m=1 u=1792150328.265000'

        中间的 `before` 只用来取"改之前"的快照，跨毫秒的等待放在写之前就够了。
        """
        sid = self.store.create_chat_session("t", "", "", "", "", "")["id"]
        before = self.store.change_stamps(sid)
        time.sleep(0.002)
        self.store.append_chat_message(sid, "user", {"text": "hi"})
        after = self.store.change_stamps(sid)
        self.assertNotEqual(before["sessions"], after["sessions"])
        self.assertNotEqual(before["messages"], after["messages"])

    def test_renaming_a_session_moves_the_sessions_stamp(self):
        """改名要能被指纹发现——它是**会话列表那一行会跟着变**的唯一信号。

        中间那个 2 毫秒的等待不是凑数：改名是 UPDATE，它只动 `updated_at`，而那个
        字段的分辨率是**毫秒**（`models.now()` = `round(time.time(), 3)`）。同进程里
        连着跑，创建与改名可能落在同一毫秒里，指纹本来就分辨不出来——而真实路径上
        一次改名要走一趟 HTTP，远不止 1 毫秒。**不等就等于在断言产品的分辨率，
        而不是"改名会不会被发现"**，于是一条正确的实现会随机器负载随机变红。
        """
        sid = self.store.create_chat_session("t", "", "", "", "", "")["id"]
        before = self.store.change_stamps()
        time.sleep(0.002)
        self.store.rename_chat_session(sid, "换个名字")
        self.assertNotEqual(before["sessions"], self.store.change_stamps()["sessions"])

    def test_the_messages_stamp_is_per_session(self):
        one = self.store.create_chat_session("一", "", "", "", "", "")["id"]
        two = self.store.create_chat_session("二", "", "", "", "", "")["id"]
        self.store.append_chat_message(two, "user", {"text": "hi"})
        stamp_one = self.store.change_stamps(one)["messages"]
        self.store.append_chat_message(one, "user", {"text": "hi"})
        self.assertNotEqual(stamp_one, self.store.change_stamps(one)["messages"])
        # 二没有说话，它那条不该因为一说了话而变化。
        self.assertEqual("n=1 s=1", self.store.change_stamps(two)["messages"])

    def test_the_messages_stamp_is_absent_when_no_session_is_given(self):
        """没开会话时不该多一块：多出来的字段会让调用方以为"这个会话有消息"。"""
        self.assertNotIn("messages", self.store.change_stamps())
        self.assertIn("messages", self.store.change_stamps("C-nope"))

    def test_deleting_a_row_still_moves_the_stamp(self):
        """行数 + 最大行号两面都要抓。

        只按行数：删了再加一条（同样的行数）看不出来。只按最大行号：删掉最后一条
        也看不出来（最大行号还是它）。两条一起才两头都盖到。
        """
        eid = self.store.create_engagement("10.0.0.1")["id"]
        before = self.store.change_stamps()
        self.store.delete_engagement(eid)
        after = self.store.change_stamps()
        self.assertNotEqual(before["engagements"], after["engagements"])
        self.assertNotEqual(before["nodes"], after["nodes"])

    def test_the_traffic_stamp_is_narrowed_to_one_session(self):
        """「流量」那一格按**会话**收口，不按人。

        按人收口的话，同一个人的另一个会话（或者另一个标签页里跑着的扫描）每发
        一条请求，当前这一页都要白重画一次表格——而画面上永远是那几条。按会话收
        口之后这一格答的是「**这条对话**里又发出了请求没有」，与 `list_traffic` 的
        `session_id` 是同一条口径。

        `traffic_user` 与 `traffic_session` 是**两层**（谁 + 哪条对话），所以这里
        两个都传：只按会话不按人的话，别人的会话 id 撞进来就会串。
        """
        self.store.record_traffic(session_id="C-one", user_id="u1",
                                  method="GET", url="http://127.0.0.1/a")
        one = self.store.change_stamps(traffic_user="u1",
                                       traffic_session="C-one")["traffic"]
        self.assertTrue(one.startswith("n=1 "), one)
        # 另一条对话里什么都没有：它的那一格必须是 n=0，否则心跳会一直判「变了」。
        empty = self.store.change_stamps(traffic_user="u1",
                                         traffic_session="C-two")["traffic"]
        self.assertEqual("n=0 m=0", empty)
        self.store.record_traffic(session_id="C-two", user_id="u1",
                                  method="GET", url="http://127.0.0.1/b")
        after = self.store.change_stamps(traffic_user="u1",
                                         traffic_session="C-one")["traffic"]
        self.assertEqual(one, after, "另一条对话发了请求，这一条不该跟着动")
        self.assertNotEqual(
            empty,
            self.store.change_stamps(traffic_user="u1",
                                     traffic_session="C-two")["traffic"])

    def test_the_traffic_stamp_is_absent_unless_someone_asks_for_it(self):
        """没人看流量页时这一格根本不该出现在指纹里。

        多报一格不是「多一点信息」：调用方会拿它跟前一轮比，而这一格在会话切换
        时天然会动——于是坐在别的页签上也会每 3 秒重画一次流量表。
        """
        self.assertNotIn("traffic", self.store.change_stamps())
        self.store.record_traffic(user_id="u1", method="GET",
                                  url="http://127.0.0.1/a")
        self.assertIn("traffic", self.store.change_stamps(traffic_user="u1"))

    def test_jobs_are_deliberately_not_in_the_stamps(self):
        """任务读数由既有定时器（10 秒）驱动。两处驱动就会出现两套节奏。"""
        self.assertNotIn("jobs", self.store.change_stamps())


class PulseRouteTestCase(unittest.TestCase):

    def setUp(self):
        self.dir, self.store = support.temporary_store("pulse-http.db")
        self.engine = self.store.create_engagement("10.0.0.1")

    def tearDown(self):
        support.stop(getattr(self, "httpd", None), self.store, self.dir)

    def _start(self):
        self.httpd, self.client = support.start(self.store, WEB_DIR)
        return self.client

    def test_the_route_needs_a_login(self):
        with open(os.path.join(ROOT, "pentestd", "server.py"), encoding="utf-8") as fh:
            source = fh.read()
        line = 'r.add("GET", "/api/v1/pulse", self.pulse)'
        self.assertIn(line, source, "路由没挂上")
        self.assertNotIn("auth=", line, "它读的是部署里有几个目标、几个会话，不许公开")
        # 真的打一次：没登录时必须是 401，而不是"能读到部署里有几个目标"。
        self.httpd, client = support.start(self.store, WEB_DIR, admin=False)
        status, _payload = client.get("/api/v1/pulse")
        self.assertEqual(401, status)

    def test_the_route_returns_stamps(self):
        client = self._start()
        status, payload = client.get("/api/v1/pulse")
        self.assertEqual(200, status)
        stamps = payload["stamps"]
        self.assertIn("engagements", stamps)
        self.assertIn("nodes", stamps)
        self.assertIn("sessions", stamps)
        self.assertNotIn("messages", stamps, "没带 sessionId 就不该有那一块")

    def test_the_route_takes_the_session_id(self):
        client = self._start()
        sid = self.store.create_chat_session("t", "", self.engine["id"], "", "", "")["id"]
        self.store.append_chat_message(sid, "user", {"text": "hi"})
        status, payload = client.get("/api/v1/pulse?sessionId=" + sid)
        self.assertEqual(200, status)
        self.assertEqual("n=1 s=1", payload["stamps"]["messages"])

    def test_two_polls_in_a_row_agree(self):
        """走 HTTP 也要成立：这是前端"变了才重画"的全部依据。"""
        client = self._start()
        first = client.get("/api/v1/pulse")[1]["stamps"]
        second = client.get("/api/v1/pulse")[1]["stamps"]
        self.assertEqual(first, second)

    def test_a_change_made_out_of_band_shows_up_in_the_next_poll(self):
        """**这就是这条链路要解决的那件事**：别处（模型把活干了、另一个标签页）
        写进去的东西，下一次探询必须看得见。"""
        client = self._start()
        before = client.get("/api/v1/pulse")[1]["stamps"]
        self.store.add_node(self.engine["id"], "asset", {"value": "https://10.0.0.1/"})
        after = client.get("/api/v1/pulse")[1]["stamps"]
        self.assertNotEqual(before["nodes"], after["nodes"])


class PulseWiringContractTestCase(unittest.TestCase):
    """前端接线：探询的**形状**是这份交付里最容易退化的部分。

    定时器谁都会加；容易丢掉的是"变了才重画"和"绝不动选中目标"这两条——而它们
    正是这个缺陷不该以另一种形式回来的保证。
    """

    def setUp(self):
        self.app = _source("web", "app.js")
        self.chat = _source("web", "chat.js")

    def test_the_pulse_uses_the_change_stamp_route(self):
        self.assertIn("'/pulse?sessionId='", self.app)
        self.assertIn("&engagementId=", self.app,
                      "漏洞/资产的指纹要按**目标**取——全局取值会让别的目标多一条"
                      "漏洞就重画当前这个页签")

    def test_only_the_changed_blocks_are_refetched(self):
        """比对指纹之后才拉——没有比对就等于每 3 秒重画一次列表。"""
        body = _body(self.app, "async function pumpPulse()")
        # 目标那两块比的是"**我手上这份列表**是在哪个指纹上拉下来的"（`listStamps`），
        # 不是"服务端上一轮说了什么"。这两者的差别就是本轮修掉的那个缺陷，见
        # `SidebarCatchUpTestCase.test_the_list_baseline_is_what_the_page_loaded`。
        self.assertIn("state.listStamps", body)
        self.assertIn("have.engagements !== stamps.engagements", body)
        self.assertIn("have.nodes !== stamps.nodes", body)
        self.assertIn("changed('findings')", body)
        self.assertIn("changed('assets')", body)

    def test_a_stamp_that_was_not_measured_before_is_not_a_change(self):
        """上一轮没量过这一项不算变化。

        它管的是一个具体的误伤：刚选完目标那一刻，上一轮的指纹里根本没有
        `findings` 这一项，直接比就会判成"变了"、立刻重拉并重画一遍——而页签内容
        是几毫秒前才加载的。用户看到的是刚打开就被刷了一下。
        """
        body = _body(self.app, "async function pumpPulse()")
        self.assertIn("previous[key] !== undefined", body)

    def test_the_quiet_engagement_refresh_never_touches_the_selection(self):
        """定时刷新绝不能改选中目标。

        `loadEngagements()` 会把选中目标拉到列表第一条，并在目标消失时重取当前页签
        的数据。定时路径调它一次，使用者正在看的漏洞列表就会在 3 秒后被换掉。
        """
        body = _body(self.app, "async function refreshEngagementList()")
        self.assertNotIn("selectEngagement(", body)
        self.assertIn("loadEngagements()", body,
                      "只有「选中的目标被删了」那一条支路该走它，否则漏洞页会一直"
                      "显示一个已经不存在的目标的数据")

    def test_the_pulse_runs_on_the_tabs_that_show_what_it_refreshes(self):
        """心跳只跑在**看得见那些读数**的页签上：控制台 / 漏洞 / 资产。

        漏掉漏洞与资产那两个的后果最直接：跑完一次扫描，证据先落库，人正盯着那个
        页签等它出现——而它永远不出现，除非手动刷整页。
        """
        body = _body(self.app, "function loadTabData(tab)")
        for tab in ("dsh", "findings", "assets"):
            self.assertIn("tab === '%s'" % tab, body)
        self.assertIn("stopPulseAuto()", body)

    def test_findings_and_assets_are_refetched_only_on_their_own_tab(self):
        """重拉要判**当前页签**，不能"哪份变了就拉哪份"。

        不判的话，坐在漏洞页签上时资产树也会跟着重画——而重画正是这整套心跳从头到尾
        在避免的那件事（画掉容器里正在被读的正文）。
        """
        body = _body(self.app, "async function pumpPulse()")
        self.assertIn("state.tab === 'findings' && changed('findings')", body)
        self.assertIn("(state.tab === 'findings' || state.tab === 'assets')", body)

    def test_a_tab_switch_does_not_trust_the_cached_findings_and_assets(self):
        """切回漏洞 / 资产页签要**强制重取**，不能相信上次留下的缓存。

        使用者报的是"漏洞、资产不能自动加载，需要刷新"。心跳并不能救：它只重拉
        **当前页签**，而那一次扫描是在「控制台」里跑完的——变化在那边就被记进指纹了，
        切回来时已经判不出"变了"。所以进入页签这一下必须不信缓存。
        """
        body = _body(self.app, "function loadTabData(tab)")
        self.assertIn("loadAssets(true);", body,
                      "漏洞页签切进来时没有强制重拉资产（卡片上的资产名会旧）")
        # 正文取哪一份由**漏洞页的范围**决定（左栏选中了具体目标，还是「全部目标」），
        # 但两条分支都是 `(true)`——跨页签进来一律不信缓存。
        self.assertIn("if (state.findingsScope === '') loadAllFindings(true); else loadFindings(true);",
                      body, "漏洞页签切进来时没有强制重拉（仍会显示上次的缓存）")
        self.assertIn("else if (tab === 'assets') loadAssets(true);", body,
                      "资产页签切进来时没有强制重拉（仍会显示上次的缓存）")

    def test_the_graph_tab_is_opted_in_but_not_polled_blindly(self):
        """链路页**不再是**被排除的那一个，但也不是"每 3 秒重取一次"。

        它原先被排除，理由是真的：新节点会改变布局尺寸，而那时的 `renderGraph` 一判到
        布局指纹变化就复位缩放与平移，于是自动刷新等于每 3 秒把视角拽回去一次。本轮那
        两条前提都改了（重取之后视角保留，只在画布还没量到尺寸时摆正），于是它接进同
        一趟心跳。但判的是**指纹**而不是时间：`loadGraph(true)` 要重跑服务端布局，一
        张几千节点的图那是秒级的活，而 `/pulse` 只是一次紧凑查询。

        它还有自己的开关（页头那颗「自动刷新」芯片），所以条件里必须带 `state.graphAuto`
        ——关掉之后这一页回到改版前那个"靠手点刷新链路"的样子，心跳也不该在后台空转。
        """
        body = _body(self.app, "function loadTabData(tab)")
        start = body.index("const pulseTab")
        gate = body[start:body.index("startPulseAuto", start)]
        self.assertIn("tab === 'graph' && state.graphAuto", gate,
                      "链路页没有被接进心跳，或没有受页头芯片控制（关不掉）")
        for tab in ("jobs", "report", "setup"):
            self.assertNotIn("'%s'" % tab, gate,
                             "启动心跳的条件里不该出现 %r" % tab)
        pump = _body(self.app, "async function pumpPulse()")
        self.assertIn("state.tab === 'graph'", pump, "心跳里没有链路这一支")
        self.assertIn("changed('nodes')", pump, "链路重取没有判节点指纹")
        # 指纹看不见的两处变化（目标改名、就地补写节点内容）只能靠切页签时重取补上。
        self.assertIn("loadGraph(state.graphAuto)", body,
                      "切回链路页不重取：目标改名与就地补写的内容都看不见")

    def test_the_repeater_tab_runs_the_pulse_too(self):
        """请求重发页右栏挂着**同一份**流量列表，所以它也要进心跳名单。

        漏掉它的症状很具体：在重发页上看着右栏，模型在左边对话里又发了一条请求，
        那一栏不动——而人以为这一页是"实时的"。它与流量页的区别只在"点一行会填进
        左边那张表"，数据来源完全一样。
        """
        body = _body(self.app, "function loadTabData(tab)")
        start = body.index("const pulseTab")
        gate = body[start:body.index("startPulseAuto", start)]
        self.assertIn("tab === 'repeater'", gate, "重发页没接进心跳")
        self.assertIn("tab === 'traffic'", gate, "流量页那一支被顶掉了")

    def test_a_session_switch_refetches_the_traffic_even_when_the_stamp_agrees(self):
        """从一条空对话切到另一条空对话：指纹一字不差，而列表该整张换掉。

        两条对话都还是 `"n=0 m=0"`，只比指纹的话屏幕上会留着上一条对话的表——
        而表里那几条请求属于**别的**对话。所以「这一轮量的是哪条对话」要单独记，
        它不是指纹的一部分。
        """
        body = _body(self.app, "async function pumpPulse()")
        self.assertIn("state.trafficSession", body)
        self.assertIn("switchedSession", body)
        # 两个页签都要：流量页看得见，重发页右栏是同一份。
        self.assertIn("state.tab === 'traffic' || state.tab === 'repeater'", body)

    def test_the_traffic_requests_carry_the_session_id(self):
        """前端不传 `sessionId` 就等于后端那两层收口全都用不上。

        缺了这一格的症状全部落在"看不见"上：列表把别的对话的请求一起列出来、
        重发页发出去的那条立刻被自己的过滤器筛掉、心跳那一格按人取于是白重画。
        三处都在 panels.js 里，所以这里逐处钉。
        """
        panels = _source("web", "panels.js")
        self.assertIn("function trafficSession()", panels)
        self.assertIn("currentSessionId", panels)
        self.assertIn("&sessionId=", panels)
        self.assertIn("sessionId: trafficSession()", panels,
                      "重发时没把会话带上去，那一条会落到\"没有会话\"里")

    def test_both_traffic_lists_are_painted_from_one_pass(self):
        """流量页左栏与重发页右栏是**同一批记录**：一次取、两处画。

        各拉一次的话，两边的选中态、截断提示与"到最近 N 条"会分别漂——而它们是
        同一个问题的两个视图。
        """
        panels = _source("web", "panels.js")
        body = _body(panels, "async function loadTraffic(quiet)")
        self.assertIn("paintTrafficList(host, records, false)", body)
        self.assertIn("#repeater-traffic", body)

    def test_the_findings_list_keeps_its_scroll_position(self):
        """漏洞列表会在数据变化时整段重画，而此刻人很可能正读到第五条。

        不还滚动位置的表现是"新证据一到、页面跳回顶部"——那会让人不敢开着这个页签，
        于是自动刷新等于没做。
        """
        body = _body(self.app, "function renderFindings(error)")
        self.assertIn("scrollTop", body)
        self.assertIn("paintFindings(error)", body)

    def test_the_stamps_are_kept_across_a_tab_switch(self):
        """切走再回来，攒下的变化要在**第一次**探询上被发现。

        清掉指纹的表现是"我切回来了，怎么还是旧的"——而它只差一轮（3 秒），
        比一直不刷新更难被认出来。
        """
        body = _body(self.app, "function stopPulseAuto()")
        self.assertNotIn("state.pulse = null", body)

    def test_the_chat_side_compares_its_own_stamps(self):
        # 导出表在 `syncFromPulse` 之后又多了 `focusJob`（m14866：会话里点工具
        # 轨迹跳到右栏那一条）与 `pinReviewHeight`（m20719：探针自己塞夹具卡片，
        # 走不到 `renderReviews()`），所以这里钉的是"这两项都在表里"，
        # 不是"表以它们结尾"。
        self.assertIn("currentSessionId, syncFromPulse,", self.chat)
        self.assertIn("focusJob,", self.chat)
        body = _body(self.chat, "function syncFromPulse(stamps)")
        self.assertIn("view.sessionsStamp !== next.sessions", body)
        self.assertIn("previous.messages !== next.messages", body)

    def test_a_running_turn_is_not_overwritten_by_a_poll(self):
        """本标签页正在发送时消息由 SSE 逐条落地；再整份拉一次会把它盖回去。"""
        body = _body(self.chat, "function syncFromPulse(stamps)")
        self.assertIn("view.sending", body)

    def test_the_session_list_keeps_its_scroll_position(self):
        """一轮里这个列表会被重画好几次（每落一条消息指纹都变）。

        不还滚动位置的表现是"我读着读着它自己跳回顶部"——会被当成浏览器抽风。
        """
        body = _body(self.chat, "function renderSessions()", "\n  }\n")
        self.assertIn("scrollTop", body)
        self.assertIn("paintSessions()", body)

    def test_the_message_refresh_does_not_reload_the_entry_selection(self):
        """心跳只换消息，不碰入口三项。

        复用 `openSession` 会把 `config` 一起重取并回填——使用者刚选好、还没发出去的
        Agent / 模型 / 权限会在 3 秒后被悄悄改回服务端的值。
        """
        body = _body(self.chat, "function syncFromPulse(stamps)", "\n  }\n")
        self.assertNotIn("openSession(", body)
        self.assertIn("/chat/sessions/", body)


class SidebarCatchUpTestCase(unittest.TestCase):
    """侧边栏那两个块**自己跟上**的三个前提，以及它们各自守着的那个缺陷。

    使用者报的是"目标和对话记录没有生成"——目标确实建出来了（他提问时平台自动建的，
    `store.ensure_session_engagement`），而侧边栏那两块一直写着「还没有目标。下发一次
    作业会自动建一个。」与「未选择目标 · 0 条」。三个缺陷叠在一起，少修一个症状就不一样：

      ① **启动那条路上一个心跳入口都没有**。`state.tab` 写死 `'graph'`，而 `index.html`
         上标 `active` 的是对话；心跳只挂在"切页签"与"选中目标"两条路上，而目标列表为空
         时 `loadEngagements()` 会提前返回、根本走不到后者。实测（真实启动链）：
         整个启动过程对 `/pulse` 的请求数是 **0**。
      ② **基线取错了**。比的是"服务端上一轮说了什么"，不是"我手上这份是在哪个指纹上
         拉下来的"。启动那次拉取与第一次探询是并发的，一旦错开（他是在目标出现**之后**
         才第一次切页签），"目标出现了"就永远不被判成变化。
      ③ **没人把当前对话的目标选上**。服务端在第一次产出型调用时就把它绑上了，而
         「对话记录」是按**选中**的目标列出来的——不跟，正在看的那条对话就永远不出现在
         它自己的列表里，旁边还写着"另有 1 条对话属于其他目标"。

    这三条的真实启动链验证（含**把任一处改回旧语义就会红**的对照）在
    `tools/probe_boot_chain.py` 的两场"没人动过的页面"里；这里守的是规则的**形状**，
    好让改动它的人先读到理由。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _source("web", "app.js")
        cls.chat = _source("web", "chat.js")

    # --------------------------------------------------- ① 启动就要起心跳

    def test_the_boot_path_starts_the_pulse(self):
        """`init()` 与 `refreshAll()` 都必须走到 `loadTabData`。

        心跳此前只挂在 `switchTab` / `selectEngagement` 里那一句上，而真实启动
        （boot.js 的 `enterConsole`）走的是 `PentestUI.init()`——目标列表为空时
        `loadEngagements()` 连 `selectEngagement` 都不会调，于是心跳一次都没起。
        """
        for marker in ("function init()", "async function refreshAll()"):
            self.assertIn("loadTabData(state.tab)", _body(self.app, marker),
                          "%s 里少了那一句：启动/换用户之后没有人起心跳" % marker)

    def test_the_visible_tab_is_read_from_the_dom(self):
        """`state.tab` 不许写死字面量——它是"哪一块是可见的"，只有 HTML 那一处权威。

        写死 `'graph'` 的那个版本与 `index.html` 上标着 `active` 的对话不一致，而心跳
        按它决定起不起（`startPulseAuto` 的名单里有对话、没有链路）——打开页面停在对话
        上，那两个块的心跳一次都没起过。
        """
        self.assertIn("tab: ''", self.app,
                      "state.tab 的初值必须是空串（不知道），由启动时从界面读")
        body = _body(self.app, "function activeTabFromDom()")
        self.assertIn(".tab.active", body)
        self.assertIn("dataset.tab", body)
        init = _body(self.app, "function init()")
        self.assertIn("state.tab = activeTabFromDom();", init)
        self.assertLess(init.index("state.tab = activeTabFromDom();"),
                        init.index("loadTabData(state.tab)"),
                        "读界面必须排在按它起心跳之前")

    # --------------------------------------------------- ② 基线是"我手上这份"

    def test_the_list_baseline_is_what_the_page_loaded(self):
        """目标列表：记的是"这份列表是在哪个指纹上拉下来的"，并在拉失败时清回"不清楚"。"""
        self.assertIn("listStamps: null", self.app)
        boot = _body(self.app, "async function loadEngagements()")
        self.assertIn("state.listStamps = null", boot,
                      "刚拉过一份列表、而不知道它对应哪个指纹 → 下一轮补拉一次并记上")
        quiet = _body(self.app, "async function refreshEngagementList()")
        self.assertIn("state.listStamps = null", quiet,
                      "拉失败时必须清回「不清楚」：调用方已经按「这一轮问过」记了指纹，"
                      "而列表其实没更新，那份旧的会一直等下一次真变化")

    def test_the_session_list_baseline_is_what_the_page_loaded(self):
        """会话列表：与目标列表同一条规则，只是换了一个持有者（chat.js）。"""
        self.assertIn("sessionsStamp: undefined", self.chat)
        body = _body(self.chat, "async function loadSessions(force)")
        self.assertIn("view.sessionsStamp = undefined", body,
                      "拉失败时要清回「不清楚」，否则这份旧列表再也等不到下一次重拉")

    # --------------------------------------------------- ③ 当前对话的目标要跟上

    def test_the_open_conversation_adopts_its_goal(self):
        """切到哪条对话，「漏洞/资产」的范围就跟到它的目标；同一条对话里不抢手动选择。

        三个要点，各挡一种写法：
          * **换对话要真的跟过去**——服务端把一条对话的全部任务写进它自己的一个目标
            （`chat.session_engagement`），所以"会话范围"就是"该目标的范围"。只在一个
            都没选时才跟，会让标签写着对话 B、内容还是对话 A 的漏洞与资产；
          * **同一对话内不覆盖使用者的手动选择**（"看另一个目标的数据、同时在这条对话
            里提问"是合法用法），判据是 `(sid, owner)` 这一对变没变，而不是每次心跳都跟；
          * **要重画**——这一块的正文按选中的目标筛，不重画就还写着"未选择目标 · 0 条"。
        """
        body = _body(self.chat, "function adoptSessionEngagement()")
        self.assertIn("view.scopeSync", body, "去重靠 `(sid, owner)` 这一对，不能每次心跳都跟")
        self.assertIn("last.sid === sid", body)
        self.assertIn("last.owner === owner", body)
        self.assertIn("view.sessionId", body)
        self.assertIn("selectEngagement(", body)
        self.assertIn("renderSessions()", body)
        for marker in ("async function loadSessions(force)",
                       "async function openSession(sessionId)"):
            self.assertIn("adoptSessionEngagement()", _body(self.chat, marker),
                          "%s 里少了一次采纳：那正是「我刚知道这条对话是谁的」那一刻"
                          % marker)

    def test_a_late_first_conversation_is_opened_once(self):
        """第一条对话**之后**才出现时，要自动打开它——但使用者按过「新对话」之后不许。

        `ensureLoaded` 里那条 `openSession(view.sessions[0].id)` 只在启动那一次跑；
        启动时列表是空的话它赶不上，于是侧边栏列着"另有 1 条对话属于其他目标"、主区
        一片空白。而"他清空的"与"还没打开过"必须分开——前者被自动打开会把刚清空的
        输入区又塞满历史。
        """
        body = _body(self.chat, "function autoOpenFirstSession()")
        self.assertIn("view.everOpened", body)
        self.assertIn("openSession(", body)
        self.assertIn("view.everOpened = true;", _body(self.chat, "function newSession()"),
                      "按了「新对话」就算「打开过」，否则下一次列表刷新会把它顶掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
