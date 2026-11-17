# -*- coding: utf-8 -*-
"""一个对话一个目标：所有智能体探测到的链路/漏洞/资产/报告都归在它下面。

## 守的是什么

产出型工具此前**一个都不带** `engagementId`（`_intrusive_body`、`start_probe`、
`start_scan`、`open_pivot`、`run_tool` 只拼 target），而 `create_job` 在它为空时
**新建一个目标**。于是同一次作业被切碎：实测库里 11 个任务对应 11 个目标，其中
一批三个目标是**同一毫秒内**建出来的——那是三个子智能体各自下发任务。

后果不是"多几个目标"这么轻：链路、漏洞、资产、报告四份交付物全都按
`engagement_id` 成篇，切碎之后没有人能在一个地方读完整台机器上发现了什么。

## 为什么反过来也要判

"工具都带上了 `engagementId`"这件事只有写成断言才留在代码里。少一个工具就是
少一处归属，而症状是**分散**而不是报错——界面上只是多了一个目标，谁都不会发现。

**`engagementId` 一个字都不能进工具 schema。** 归属由会话来定，不由模型挑；让
模型能传它，等于让它自己决定"这次的东西算谁的"，而它挑的依据只会是"看起来像"。
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import chat                        # noqa: E402
from pentestd import models                      # noqa: E402
from pentestd import subagent                    # noqa: E402
from pentestd.server import ControlPlane         # noqa: E402
from pentestd.store import Store                 # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")

# 会下发任务、因而**必须**带归属的工具。少一个就是一处归属缺口。
JOB_TOOLS = ("start_scan", "start_probe", "start_exploit", "test_credentials",
             "deliver_payload", "open_pivot", "run_tool")

# 同一台机器的两种写法：不共用目标时它们会各建一个。
TARGET_A = "10.10.10.250"
TARGET_B = "10.10.10.251"


def drain(gen):
    """把 `run_delegation` 抽干，拿它的 `return` 值（生成器的返回值走 StopIteration）。"""
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            return stop.value


class EngagementMergeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "merge.db"))
        self.plane = ControlPlane(self.store, WEB_DIR, auth_required=False)
        self.session = self.store.create_chat_session(title="并发侦察", user_id="u-1")
        self.sid = self.session["id"]

    def tearDown(self):
        self.store.close()          # 不关句柄的话 Windows 上临时目录删不掉
        self.tmp.cleanup()

    def _ctx(self, body):
        return {"params": {}, "query": {}, "body": body, "headers": {},
                "path": "/api/v1/chat", "clientAddress": ""}

    def _probe(self, target, session=None):
        args = {"target": target, "ports": "22,80"}
        return chat.execute_tool(self.plane, "start_probe", args, "",
                                 "full", "", False, session)

    # ------------------------------------------------------------ 会话绑定
    def test_ensure_is_idempotent(self):
        """同一个会话拿两次拿到的是同一个目标——不然"一个对话一个目标"不成立。"""
        first = self.store.ensure_session_engagement(self.sid, label="对话作业")
        second = self.store.ensure_session_engagement(self.sid, label="对话作业")
        self.assertEqual(first, second)
        self.assertEqual(1, len(self.store.list_engagements()))

    def test_created_engagement_carries_the_default_label(self):
        """自动建出来的目标叫什么，以及**三处必须是同一个名字**。

        `relabel_engagement` 的注释里写着为什么改名必须连 goal 节点一起改：报告正文
        读的是 goal 节点上的 `target`。**这条理由在建的时候同样成立**——只写
        `engagements` 表的话，界面上叫一个名字、报告里印另一个，而报告是拿去交给客户
        的那一份，它印错的时候没有人会想到去核对第二处。

        默认名钉在这里而不只是写在常量里：改名是一次**产品决定**，它要能让一条用例
        变红才算落地。字面量放进断言，等于把"这次改名是有意的"也留了下来。

        两条路都要走：`chat` 显式传下来的那一次，和 `store` 自己的兜底那一次。
        只测一条的话，另一条会在改名时悄悄留在旧名字上——而它们的差别**只有在真正
        用到兜底时才现形**，也就是"某个直接调用 store 的入口建出来的目标叫旧名"。
        """
        eid = chat.session_engagement(self.plane, "", {"sessionId": self.sid})
        self.assertEqual("渗透目标", self.store.get_engagement_meta(eid)["target"])
        goal = models.public_node(self.store.get_node(eid, "goal-1"))
        self.assertEqual("渗透目标", goal["target"])
        self.assertIn("渗透目标", self.store.report_markdown(eid))

        # 兜底那条路（`label or DEFAULT_SESSION_LABEL`）单独走一遍。
        sid2 = self.store.create_chat_session(title="另一条", user_id="u-1")["id"]
        eid2 = self.store.ensure_session_engagement(sid2)          # 不传 label
        self.assertEqual("渗透目标", self.store.get_engagement_meta(eid2)["target"])

    def test_binding_is_persisted_on_the_session(self):
        """绑定必须落到 `chat_sessions.engagement_id`。

        **内存里记着不算数**：`/chat/stream` 每一轮都是从会话行读
        `fallback_eid`，不落库的话下一个请求就拿不到了，表现为"刷新之后一个
        对话又开始每次新建目标"。
        """
        eid = self.store.ensure_session_engagement(self.sid, label="对话作业")
        row = self.store.conn.execute(
            "SELECT engagement_id FROM chat_sessions WHERE id=?", (self.sid,)).fetchone()
        self.assertEqual(eid, row["engagement_id"])

    def test_missing_engagement_is_rebuilt(self):
        """绑定的目标被删掉之后要重建，不能一直指着一个不存在的 id。"""
        first = self.store.ensure_session_engagement(self.sid, label="对话作业")
        self.store.delete_engagement(first)
        second = self.store.ensure_session_engagement(self.sid, label="对话作业")
        self.assertNotEqual(first, second)
        self.assertTrue(self.store.get_engagement_meta(second)["id"])

    # ------------------------------------------------------------ 合并本身
    def test_repeated_probe_lands_in_one_engagement(self):
        """同一会话里探测两个不同的地址 → **一个**目标。"""
        first = self._probe(TARGET_A, {"sessionId": self.sid})
        second = self._probe(TARGET_B, {"sessionId": self.sid})
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertEqual(first["data"]["engagementId"], second["data"]["engagementId"])
        self.assertEqual(1, len(self.store.list_engagements()),
                         "两个任务建了两个目标，归属又散了")

    def test_jobs_and_nodes_share_the_engagement(self):
        """落在同一个目标上的是**真的**落下来了，不只是返回体里写着同一个 id。

        判据取 `jobs` 表而不是节点：`start_probe` 只**建任务**，事实要等执行节点
        跑完才写进来。用节点当判据的话，这条用例在"任务建对了、但归属没写进任务"
        时同样是绿的——那正好是要防的那一种。
        """
        eid = self._probe(TARGET_A, {"sessionId": self.sid})["data"]["engagementId"]
        self._probe(TARGET_B, {"sessionId": self.sid})
        jobs = self.store.jobs()
        self.assertEqual(2, len(jobs))
        self.assertEqual({eid}, set(job["engagementId"] for job in jobs),
                         "任务没有全部落在同一个目标下")
        self.assertEqual(1, len(self.store.list_engagements()))
        # 目标自己的 goal 节点在，说明它是个能用的目标而不是一行空记录。
        self.assertIsNotNone(self.store.get_node(eid, "goal-1"))

    def test_children_share_the_parents_engagement(self):
        """子智能体拿到的 eid 与父轮解析出来的是同一个。

        这就是"合并"的全部机制：**共用 id**，而不是事后归并。子智能体的
        `session` 与父轮是同一个对象（`spawn` 原样传下去），所以它们在会话上
        解析到的目标与父轮一致。
        """
        parent = chat.session_engagement(self.plane, "", {"sessionId": self.sid})
        child = chat.session_engagement(self.plane, "", {"sessionId": self.sid})
        self.assertEqual(parent, child)
        self.assertTrue(parent)

    def test_without_session_behaviour_is_unchanged(self):
        """没有会话时保持旧行为：各自按 target 新建。

        **不能把这个也合并掉。** CLI 与脚本化调用没有会话，"该归给谁"没有
        依据；硬造一个匿名目标会让"没有会话"和"有会话但没绑"在数据里长得一样，
        而这两种情况的处置完全不同。
        """
        self._probe(TARGET_A, None)
        self._probe(TARGET_B, None)
        self.assertEqual(2, len(self.store.list_engagements()))

    # ------------------------------------------------------------ 读工具
    def test_read_tools_follow_the_sessions_engagement(self):
        """读工具省略 engagementId 时读**这条会话**的目标，不是"最近那个目标"。

        实测症状不在写路径上：写已经全归到一个目标里，但 `get_findings` /
        `get_assets` 这类读工具取的是"最近创建的目标"。于是另一条对话刚建过目标
        之后，这一条读出来的就是**别人的**证据——不报错，只是把一份不属于自己的
        清单交给模型，而模型会拿它下结论。
        """
        mine = self._probe(TARGET_A, {"sessionId": self.sid})["data"]["engagementId"]
        other = self.store.create_chat_session(title="另一条对话")
        chat.execute_tool(self.plane, "start_probe", {"target": TARGET_B}, "",
                          "full", "", False, {"sessionId": other["id"]})
        newest = self.store.list_engagements()[0]["id"]
        self.assertNotEqual(mine, newest, "前提不成立：另一条对话没有建出新目标")
        out = chat.execute_tool(self.plane, "get_findings", {}, "", "full", "", False,
                                {"sessionId": self.sid})
        self.assertTrue(out["ok"], out)
        self.assertEqual(mine, out["data"]["engagementId"])

    def test_a_read_does_not_create_an_engagement(self):
        """读一个还没写过东西的会话：**不建目标**，退回到旧行为。

        读工具为了"取一个 id"要走过解析路径；如果那条路径顺手建一个目标，每读一次
        就多一行空目标，而"这个对话到底做过什么"会被这些空壳稀释。判据是目标总数
        不变，且读到的仍然是那个真实存在的目标。
        """
        known = self._probe(TARGET_A, {"sessionId": self.sid})["data"]["engagementId"]
        fresh = self.store.create_chat_session(title="还没开工")
        before = len(self.store.list_engagements())
        out = chat.execute_tool(self.plane, "get_chain_overview", {}, "", "full", "",
                                False, {"sessionId": fresh["id"]})
        self.assertTrue(out["ok"], out)
        self.assertEqual(before, len(self.store.list_engagements()),
                         "一次读操作建出了新目标")
        self.assertEqual(known, out["data"]["engagementId"])

    # ------------------------------------------------------------ 派活改名
    def test_delegation_does_not_rewrite_the_target(self):
        """派活**不准**改目标的 `target` 与 `objective`。

        这两列回答的是"被测的是什么"与"要达成什么"。此前派活会把 `target` 换成
        "子智能体：recon · web"、把 `goal-1` 上的 `objective` 换成"本目标由一次
        派活建立：…"。实测后果：目标名从 `10.10.10.250` 变成智能体名，报告第一行
        （读 `goal-1.target`）跟着变，而"被测的是什么"当场就没有别的地方能读到
        了。使用者报的是"已污染三次"。

        派出去的智能体是谁，`delegate` 的返回值与消息的 `subagents` 字段里都有
        ——那是它该在的地方，不必占用目标身份。
        """
        # 先给目标一个**真实身份**，再派活。顺序重要：先派活再设身份的话，这条
        # 用例只能证明"设身份发生在派活之后"，证明不了"派活没动它"。
        eid0 = self.store.ensure_session_engagement(
            self.sid, label=TARGET_A, objective="把面板打穿")
        before = self.store.get_engagement_meta(eid0)
        before_goal = models.public_node(self.store.get_node(eid0, "goal-1"))

        seen = {}

        def fake_spawn(specs, **kw):
            seen["roles"] = [spec.role for spec in specs]
            seen["fallback_eid"] = kw.get("fallback_eid")
            seen["session"] = kw.get("session")
            if False:                       # pragma: no cover —— 只是个生成器壳
                yield None

        original = subagent.spawn
        subagent.spawn = fake_spawn
        try:
            result, _charged = drain(subagent.run_delegation(
                self.plane, {"children": [{"role": "recon", "objective": "摸清网段"},
                                          {"role": "web", "objective": "看面板"}]},
                profile={}, api_key="", max_steps=8, steps=0,
                policy_id="full", session={"sessionId": self.sid}))
        finally:
            subagent.spawn = original

        eid = seen["fallback_eid"]
        self.assertTrue(eid, "派活没有把目标传给子智能体")
        self.assertEqual(["recon", "web"], seen["roles"], "派活本身要照常发生")
        self.assertEqual(eid, result["data"]["engagementId"])
        self.assertEqual([eid], result["touched"])
        self.assertEqual(eid0, eid, "派活换了一个目标")

        after = self.store.get_engagement_meta(eid)
        after_goal = models.public_node(self.store.get_node(eid, "goal-1"))
        self.assertEqual(TARGET_A, after["target"])
        self.assertEqual(before["objective"], after["objective"])
        # 报告正文读的是 goal 节点，两处必须一起不变——只断言表的话，正是当初
        # "界面一个名字、报告另一个名字"那个缺陷的形状。
        self.assertEqual(before_goal["target"], after_goal["target"])
        self.assertEqual(before_goal["objective"], after_goal["objective"])

    def test_delegation_survives_a_broken_engagement_lookup(self):
        """解析归属失败**不能**挡住派活。

        这里原本守的是另一个更细的坑：解析归属与"换标签"写在同一个 `try` 里，
        `relabel` 抛错会把刚解析出来的 `delegation_eid` 一起退回 `fallback_eid`
        （空串）——子智能体于是又各建一个目标，正是合并要修的那件事，而且只在
        这个少见分支上发生。

        换标签那一步已经整段删掉了（见 `test_delegation_does_not_rewrite_the_target`），
        所以这个分支现在只剩一件事要保证：**查不到归属时退回调用方给的那个，而
        不是把异常抛出去**。`fallback_eid` 为空是 CLI / 脚本化调用的正常形态。
        """
        seen = {}

        def fake_spawn(specs, **kw):
            seen["fallback_eid"] = kw.get("fallback_eid")
            if False:                       # pragma: no cover —— 生成器壳
                yield None

        def boom(*_args, **_kwargs):
            raise RuntimeError("磁盘满了")

        original_spawn = subagent.spawn
        original_ensure = self.store.ensure_session_engagement
        subagent.spawn = fake_spawn
        self.store.ensure_session_engagement = boom
        try:
            result, _charged = drain(subagent.run_delegation(
                self.plane, {"children": [{"role": "recon", "objective": "摸清网段"}]},
                profile={}, api_key="", max_steps=8, steps=0,
                policy_id="full", session={"sessionId": self.sid}))
        finally:
            subagent.spawn = original_spawn
            self.store.ensure_session_engagement = original_ensure

        self.assertEqual("", seen["fallback_eid"],
                         "查不到归属时应当退回调用方给的值（这里是空串）")
        self.assertEqual("", result["data"]["engagementId"])
        self.assertEqual([], result["touched"])

    def test_relabel_does_not_move_ownership(self):
        """换名字不动归属：节点、任务、边的 `engagement_id` 一个字都不改。"""
        eid = self._probe(TARGET_A, {"sessionId": self.sid})["data"]["engagementId"]
        before_nodes = [(n["id"], n["kind"]) for n in self.store.list_nodes(eid)]
        before_jobs = [job["id"] for job in self.store.jobs()]

        self.assertTrue(self.store.relabel_engagement(eid, "子智能体：recon · web"))

        after_nodes = [(n["id"], n["kind"]) for n in self.store.list_nodes(eid)]
        self.assertEqual(before_nodes, after_nodes)
        self.assertEqual(before_jobs, [job["id"] for job in self.store.jobs()])
        self.assertEqual({eid}, set(job["engagementId"] for job in self.store.jobs()))
        self.assertEqual(1, len(self.store.list_engagements()))

    def test_relabel_moves_the_goal_node_too(self):
        """`goal-1` 节点上的 target 也要改。

        报告正文读的是 goal 节点（`report_markdown` 里"目标 | %s"那一行），只改
        `engagements` 表就会变成"界面写着新名字、报告里印着旧名字"——两处对不上
        时读的人没有依据判断该信哪一份。
        """
        eid = self._probe(TARGET_A, {"sessionId": self.sid})["data"]["engagementId"]
        self.store.relabel_engagement(eid, "子智能体：recon")
        # 读的是**对外形状**（`public_node` 把 payload 摊平），也就是报告与界面
        # 实际读的那一层——直接读 `payload` 会在存储格式变了之后仍然绿着。
        goal = models.public_node(self.store.get_node(eid, "goal-1"))
        self.assertEqual("子智能体：recon", goal["target"])
        self.assertIn("子智能体：recon", self.store.report_markdown(eid))

    def test_relabel_refuses_an_empty_label(self):
        """空标签要拒绝：目标没有名字的话，界面上就是一行看不见的东西。"""
        eid = self.store.ensure_session_engagement(self.sid, label="对话作业")
        self.assertFalse(self.store.relabel_engagement(eid, "   "))
        self.assertEqual("对话作业", self.store.get_engagement_meta(eid)["target"])

    def test_relabel_reports_a_missing_engagement(self):
        self.assertFalse(self.store.relabel_engagement("E-nope1234", "子智能体：recon"))

    # ------------------------------------------------------------ 覆盖所有工具
    def test_every_job_tool_routes_through_session_engagement(self):
        """**七个产出型工具一个都不能漏。**

        判据用**字节码里的全局名**（`co_names`），不用 `inspect.getsource`。

        `getsource` 在调用时从磁盘读文件，而函数对象带的是 **import 时刻的行号**；
        两者之间只要有人改了 `chat.py`，取回的就是**另一个函数**的源码。这不是
        假想——全量跑动期间给工具描述加了几行（位置在 `start_scan` 之前），行号
        整体下移，这条断言当场报成"`start_scan` 没走会话归属"，而它读的是隔壁的
        `_tool_list_engagements`。**报错位置离真正的原因隔着一个函数。**

        `co_names` 是编译期定下的，文件怎么改都不影响已经加载的函数对象。仓库里
        `chat._handler_wiring_guard` 早就按 `LOAD_GLOBAL` 判未知全局名，这里是
        同一套做法。

        `_intrusive_body` 那三个（exploit / creds / shell）走的是共用函数，所以
        单独钉它自己：`engagementId` 是它写进请求体的**字面量**，出现在 `co_consts`
        里即说明它真的在写。
        """
        # 归属由会话决定这件事有**两个**入口：`session_engagement`（纯归属）与
        # `session_job_engagement`（归属 + 后台并发上限，见 `chat.BACKGROUND_LIMIT`）。
        # 产出型工具现在走的是后者——它内部还是调前者，只是多包了一层。断言写成
        # "二者之一"而不是钉死一个名字：要守的是"归属交给会话"这条不变量，
        # 不是某个函数名。
        ROUTERS = ("session_engagement", "session_job_engagement")
        for name in JOB_TOOLS:
            handler = chat.HANDLERS[name]
            used = [item for item in ROUTERS if item in handler.__code__.co_names]
            self.assertTrue(
                used, "工具 %s 没有把归属交给会话——它下发的东西会另起一个目标"
                      "（既不调 %s 也不调 %s）" % (name, ROUTERS[0], ROUTERS[1]))
        self.assertIn("engagementId", chat._intrusive_body.__code__.co_consts)

    def test_intrusive_body_ignores_engagement_from_args(self):
        """模型传 `engagementId` **无效**：归属由会话来定。

        白名单里没有这个键，所以哪怕模型自己写了一个，也进不了请求体——它无法
        把一次动作的产物挪到另一个目标名下。
        """
        body = chat._intrusive_body("exploit", {"target": TARGET_A,
                                                "engagementId": "E-forged00"},
                                    engagement_id="E-real0001")
        self.assertEqual("E-real0001", body["engagementId"])
        self.assertNotIn("E-forged00", str(body))

    def test_engagement_id_is_not_a_model_parameter(self):
        """七个工具的工具 schema 里都不许出现 `engagementId`。

        出现一个，模型就能自己挑目标——而它挑的依据是"看起来像"，不是会话归属。
        """
        by_name = {tool["name"]: tool for tool in chat.TOOLS}
        for name in JOB_TOOLS:
            schema = by_name[name]
            props = (schema.get("parameters") or {}).get("properties") or {}
            self.assertNotIn("engagementId", props,
                             "工具 %s 让模型自己传目标——归属会被模型挑走" % name)


class GoalPerTargetTestCase(unittest.TestCase):
    """目标的名字就是被测地址：不同对话打不同机器 → 目标页多一行对应的目标。

    需求原话是"不同的对话如果对应不同的渗透目标，在目标中应该新增对应的目标"。
    此前所有自动建的目标都叫「渗透目标」，而且**新对话一律继承侧边栏当前选中的
    那个**，于是第二台机器摸到的东西全写进第一台的目标里——目标页永远只有一行，
    也看不出哪条对话对应哪台机器。

    现在的规则有两条，缺一条就不成立：

      1. 新建会话不预设归属（界面发 `engagementId: ''`，服务端拿不到就不绑）；
      2. 第一次下发产出型任务时，用**那个任务的 target** 建/找目标。

    第 2 条同时回答了"同一台机器开两条对话"：并进同一行，而不是两行同名目标
    （`find_engagement_by_target`）。证据按机器合在一处才有用——按对话切碎正是
    这一整组用例最初要修的那件事。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "targets.db"))
        self.plane = ControlPlane(self.store, WEB_DIR, auth_required=False)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _session(self, title="对话"):
        return self.store.create_chat_session(title=title, user_id="u-1")["id"]

    def _probe(self, target, sid):
        out = chat.execute_tool(self.plane, "start_probe",
                                {"target": target, "ports": "22,80"}, "",
                                "full", "", False, {"sessionId": sid})
        self.assertTrue(out["ok"], out)
        return out["data"]["engagementId"]

    # ------------------------------------------------------------ 建名
    def test_first_task_names_the_goal_after_its_target(self):
        """名字取**任务的 target**，而且三处（表、goal 节点、报告）必须是同一个。"""
        sid = self._session()
        eid = self._probe(TARGET_A, sid)
        meta = self.store.get_engagement_meta(eid)
        self.assertEqual(TARGET_A, meta["target"])
        goal = models.public_node(self.store.get_node(eid, "goal-1"))
        self.assertEqual(TARGET_A, goal["target"])
        self.assertIn(TARGET_A, self.store.report_markdown(eid))
        self.assertEqual(1, len(self.store.list_engagements()))
        row = self.store.conn.execute(
            "SELECT engagement_id FROM chat_sessions WHERE id=?", (sid,)).fetchone()
        self.assertEqual(eid, row["engagement_id"], "绑定没有落库")

    def test_a_second_conversation_on_the_same_target_joins_the_first(self):
        """两台对话打同一台机器 → **一行**目标：证据按机器合在一处。"""
        one, two = self._session("第一条"), self._session("第二条")
        eid = self._probe(TARGET_A, one)
        again = self._probe(TARGET_A, two)
        self.assertEqual(eid, again, "同一台机器建了第二个目标，证据又被切开了")
        self.assertEqual(1, len(self.store.list_engagements()))
        row = self.store.conn.execute(
            "SELECT engagement_id FROM chat_sessions WHERE id=?", (two,)).fetchone()
        self.assertEqual(eid, row["engagement_id"])

    def test_a_second_conversation_on_another_target_gets_its_own_row(self):
        """两台对话打两台机器 → **两行**目标，各自写着对应的地址。"""
        one, two = self._session("第一条"), self._session("第二条")
        eid_a = self._probe(TARGET_A, one)
        eid_b = self._probe(TARGET_B, two)
        self.assertNotEqual(eid_a, eid_b)
        rows = {item["id"]: item["target"] for item in self.store.list_engagements()}
        self.assertEqual({eid_a: TARGET_A, eid_b: TARGET_B}, rows)
        # 任务也跟着各自的目标走——只断言目标名字的话，"目标是两行、任务还是
        # 全在最后那一行"这种错法照样绿。
        by_target = {}
        for job in self.store.jobs():
            by_target.setdefault(job["target"], set()).add(job["engagementId"])
        self.assertEqual({TARGET_A: {eid_a}, TARGET_B: {eid_b}}, by_target)

    # ------------------------------------------------------------ 占位名
    def test_the_placeholder_is_not_a_target(self):
        """占位名「渗透目标」不参与按地址匹配。

        拿它匹配的后果很具体：所有**还没落目标名**的对话会挤进同一个目标，而
        它们分属不同机器。这里同时钉住"占位目标仍然会被第一条任务改成真名"
        （`adopt_target_from_job`）——那条旧路径不能因为新入口而失效。
        """
        sid = self._session()
        eid = self.store.ensure_session_engagement(sid)      # 不传 target → 占位名
        self.assertEqual("渗透目标", self.store.get_engagement_meta(eid)["target"])
        self.assertEqual("", self.store.find_engagement_by_target("渗透目标"))
        self.assertEqual("", self.store.find_engagement_by_target("  "))
        # 第一条任务落下之后名字被改成真地址，而不是另起一行。
        self.assertEqual(eid, self._probe(TARGET_A, sid))
        self.assertEqual(TARGET_A, self.store.get_engagement_meta(eid)["target"])
        self.assertEqual(1, len(self.store.list_engagements()))

    def test_target_lookup_ignores_letter_case(self):
        """域名大小写不该产生两行（`LocalHost` 与 `localhost` 是同一台）。"""
        eid = self._probe("localhost", self._session())
        self.assertEqual(eid, self.store.find_engagement_by_target("LOCALHOST"))

    def test_the_first_target_wins_when_the_conversation_pivots(self):
        """一条对话先后摸两台机器：归属**不跟着漂**，仍是第一台。

        这是有意的取舍：一条对话就是一个目标（报告、漏洞、资产都按它成篇）。
        中途换机器是"从这台打到那台"，证据读起来仍该在同一个作业里；要分开就开
        新对话，新对话的第一条任务会自己建那一行。
        """
        sid = self._session()
        eid = self._probe(TARGET_A, sid)
        self.assertEqual(eid, self._probe(TARGET_B, sid))
        self.assertEqual(1, len(self.store.list_engagements()))
        self.assertEqual(TARGET_A, self.store.get_engagement_meta(eid)["target"])


class NewConversationKeepsNoBindingTestCase(unittest.TestCase):
    """新建对话**拒不接受**请求体里的 `engagementId`。

    前端已经改成发空串，但静态文件在浏览器里可能还是旧的那一份（刷新前就开着的
    标签页），而多一条"归属从哪来"的入口，就多一种"这条对话为什么挂在别人目标下"
    的解释。所以服务端这一侧也断：归属只由第一条产出型任务的目标决定。

    这条同时守着一个更容易踩的坑——不传 `engagementId` 时**不许**兜底到"最近创建的
    目标"（`_resolve_engagement` 的读路径就是这么退的）。会话归属要是也这么退，
    两条不同机器的对话还是会挤进同一行。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "new_conv.db"))
        self.plane = ControlPlane(self.store, WEB_DIR, auth_required=False)
        # `_chat_turn_setup` 要走到底需要一个能解析的模型（同 `test_approval_sweep`）。
        self.store.save_profile("m-fake", "夹具", "openai_compat",
                                "http://127.0.0.1:9/v1", "fake-1", "none", "",
                                None, 0.2, 512, 20, "")
        self.store.activate_profile("m-fake")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _setup(self, **body):
        body.setdefault("message", "扫一台新的")
        return self.plane._chat_turn_setup(
            {"body": body, "params": {}, "user": None})

    def test_the_body_engagement_id_is_ignored(self):
        eid = self.store.create_engagement(TARGET_A, "先把这一台放着")["id"]
        setup = self._setup(engagementId=eid)
        meta = self.store.get_chat_session(setup["sessionId"])
        self.assertEqual("", meta["engagementId"],
                         "新对话被请求体里的 engagementId 绑上了旧目标")

    def test_no_binding_falls_back_to_nothing(self):
        """连"最近创建的目标"也不许当兜底——那正是两台机器挤一行的那条路。"""
        self.store.create_engagement(TARGET_A, "")
        meta = self.store.get_chat_session(self._setup()["sessionId"])
        self.assertEqual("", meta["engagementId"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
