# -*- coding: utf-8 -*-
"""`pentestd/orchestrator.py` 与它依赖的意图队列的用例。

这一层钉的不是"能跑起来"，而是编排最容易悄悄退化的三件事：

* **认领必须是原子的。** 队列的判据压在 `UPDATE ... WHERE` 里（store.claim_intent），
  两个工人读到同一条意图各自开工，等于同一件事做两遍、两条互相矛盾的证据链写进
  同一个目标。CAS 一旦被改回"先读再写"，只有并发下才复现，所以这里直接对
  `rowcount` 与 `frontier` 的可见性下断言。
* **派活与规划的顺序不能反。** 先规划的话，规划器每一拍都看到"队列空、没人跑"，
  于是每一拍硬塞一条意图——队列爆掉之前没人看得出来。
* **做不成的意图写 blocked，不写回队列。** 放回队列会让一条做不成的意图被无限
  重领，每轮烧一次模型调用，而症状只是"一直在跑、一直没进展"。

派活全部换成假的 `subagent.spawn`：真派活要模型与网络，单测里既不稳也不该等。
"""
import threading
import time
import unittest
from unittest import mock

from tests import support
from pentestd import chat, orchestrator, policy, server, subagent
from pentestd.models import ValidationError


def _drive(runner):
    """抽干生成器，返回 (事件列表, 生成器的返回值)。与 tests/test_team.py 同款。"""
    events = []
    while True:
        try:
            events.append(next(runner))
        except StopIteration as stop:
            return events, stop.value


class FakeSpawn:
    """假 `subagent.spawn`：记账、扣预算、按开关回成功或失败，不碰网络。"""

    def __init__(self, ok=True, charges=1, errors=None):
        self.ok = ok
        self.charges = charges
        # 指定"第几个孩子失败"：批量派活里失败的那一条要单独标 blocked，
        # 全都成功或全都失败都验证不到这个分支。
        self.errors = dict(errors or {})
        self.roles = []
        self.objectives = []
        self.budgets = []
        self.arguments = []

    def __call__(self, specs, **kwargs):
        self.roles.append([spec.role for spec in specs])
        self.objectives.append([spec.objective for spec in specs])
        budget = kwargs["budget"]
        self.budgets.append(budget)
        self.arguments.append(dict(kwargs))
        # **一个 spec 一个孩子**。真 spawn 就是这么回的，而这里曾经不管
        # 传进来几条都只回一条 —— 那样一来，按 spec 逐条对齐孩子的调用方
        # （work_batch 的 zip）在夹具下永远只验证得到第一条，
        # 而"派了两条却只落一条"这种错会安静地躲过所有断言。
        total = max(1, len(specs))
        ok = self.ok
        charges = max(0, int(self.charges))
        children = []
        for index in range(total):
            role = specs[index].role if index < len(specs) else ""
            errors = self.errors
            child_ok = ok if index + 1 not in errors else False
            children.append({"index": index + 1, "role": role, "ok": child_ok,
                             "steps": charges, "durationMs": 12,
                             "summary": "结论" if child_ok else "",
                             "evidence": "证据" if child_ok else "",
                             "error": "" if child_ok
                                      else (errors.get(index + 1) or "子智能体超时")})

        def generator():
            # 每个孩子各自扣自己那 share 的账：不带 index 的 take 会全部记在
            # 第 0 个槽上，共享预算里「谁花掉了额度」就分不清了。
            for index in range(total):
                for _ in range(charges):
                    budget.take(index + 1)
            for child in children:
                yield {"type": "subagent", "phase": "start", "index": child["index"],
                       "total": total, "role": child["role"]}
                yield {"type": "subagent", "phase": "done", "index": child["index"],
                       "total": total, "role": child["role"], "ok": child["ok"],
                       "summary": child["summary"]}
            failed = [child for child in children if not child["ok"]]
            return {"ok": not failed, "summary": "汇总", "spawned": total,
                    "succeeded": total - len(failed), "failed": len(failed),
                    "concurrency": total, "budget": budget.public(),
                    "children": children}

        return generator()


class _Case(unittest.TestCase):
    """共用夹具：真 store、真 ControlPlane，只有 spawn 是假的。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("orchestrator.db")
        self.eid = self.store.create_engagement("10.10.10.110", "拿下控制台",
                                               "本人授权")["id"]
        self.plane = server.ControlPlane(self.store, support.WEB_DIR,
                                         auth_required=False)
        orchestrator.reset()
        self._original_spawn = subagent.spawn
        self.spawn = FakeSpawn()
        # orchestrator 里的 `subagent` 就是 pentestd.subagent 这个模块对象，
        # 改它的属性等于改全局；tearDown 必须还原，否则会漏进别的用例。
        subagent.spawn = self.spawn

    def tearDown(self):
        subagent.spawn = self._original_spawn
        orchestrator.reset()
        self.store.close()
        self.directory.cleanup()

    def _intent(self, title, priority=5, **extra):
        payload = {"title": title, "goalId": "goal-1", "priority": priority}
        payload.update(extra)
        return self.store.add_node(self.eid, "intent", payload)

    def _use_model(self):
        """给 ControlPlane 配一个启用中的模型：编排每条决策都要模型，
        没有 active 配置时 `tick` 第一句就返回，什么都测不到。"""
        self.store.save_profile("m-fake", "夹具", "openai_compat",
                                "http://127.0.0.1:9/v1", "fake-1", "none",
                                "", None, 0.2, 512, 20, "")
        self.store.activate_profile("m-fake")

    def _rewind(self, seconds=30.0):
        """把"上次规划时间"推到过去，跳过防抖与心跳的等待。

        这两个窗口都是给人用的（别一有风吹草动就烧一次模型），测试里没必要真等。
        """
        state = orchestrator._state(self.eid)
        state["lastPlan"] = time.time() - float(seconds)


class IntentQueueTests(_Case):
    """意图队列：认领的互斥、排序与可见性。"""

    def test_second_claim_of_the_same_intent_loses(self):
        intent = self._intent("打 80 端口")
        first = self.store.claim_intent(self.eid, intent["id"], "work#1")
        self.assertIsNotNone(first)
        self.assertEqual("work#1", first["owner"])
        # 同一条再领一次必须输：否则同一件事会被做两遍。
        self.assertIsNone(self.store.claim_intent(self.eid, intent["id"], "work#2"))

    def test_claim_requires_an_owner(self):
        intent = self._intent("打 80 端口")
        with self.assertRaises(ValidationError):
            self.store.claim_intent(self.eid, intent["id"], "   ")

    def test_claim_refuses_an_intent_that_is_no_longer_alive(self):
        intent = self._intent("打 80 端口")
        self.store.update_node_state(self.eid, intent["id"], "done")
        self.assertIsNone(self.store.claim_intent(self.eid, intent["id"], "work#1"))
        self.store.update_node_state(self.eid, intent["id"], "blocked")
        self.assertIsNone(self.store.claim_intent(self.eid, intent["id"], "work#1"))

    def test_frontier_is_ordered_by_priority_then_seq(self):
        low = self._intent("低", priority=1)
        high = self._intent("高", priority=9)
        middle = self._intent("中", priority=5)
        self.assertEqual([high["id"], middle["id"], low["id"]],
                         [node["id"] for node in self.store.frontier(self.eid)])

    def test_frontier_hides_claimed_intents(self):
        first = self._intent("一", priority=9)
        second = self._intent("二", priority=5)
        self.store.claim_intent(self.eid, first["id"], "work#1")
        self.assertEqual([second["id"]],
                         [node["id"] for node in self.store.frontier(self.eid)])

    def test_release_puts_the_intent_back_without_touching_its_state(self):
        intent = self._intent("一")
        self.store.claim_intent(self.eid, intent["id"], "work#1")
        self.assertTrue(self.store.release_intent(self.eid, intent["id"]))
        node = self.store.get_node(self.eid, intent["id"])
        self.assertEqual("", node["owner"])
        self.assertEqual("active", node["state"])
        self.assertEqual([intent["id"]],
                         [row["id"] for row in self.store.frontier(self.eid)])

    def test_reset_owner_clears_what_a_dead_process_left_behind(self):
        first = self._intent("一")
        second = self._intent("二")
        self.store.claim_intent(self.eid, first["id"], "work#1")
        self.store.claim_intent(self.eid, second["id"], "work#2")
        self.assertEqual(2, self.store.reset_intents_owner(self.eid))
        self.assertEqual(2, len(self.store.frontier(self.eid)))
        # 幂等：没有残留时再清一次是 0，不是报错。
        self.assertEqual(0, self.store.reset_intents_owner(self.eid))

    def test_has_active_intent_counts_the_claimed_not_only_the_free_ones(self):
        intent = self._intent("一")
        self.store.claim_intent(self.eid, intent["id"], "work#1")
        # 只看 frontier 会答"没有活"，而与工人认领存在竞态——这正是要避免的。
        self.assertTrue(self.store.has_active_intent(self.eid))
        self.assertEqual([], self.store.frontier(self.eid))
        self.store.update_node_state(self.eid, intent["id"], "done")
        self.assertFalse(self.store.has_active_intent(self.eid))

    def test_has_open_goal_follows_the_goal_state(self):
        self.assertTrue(self.store.has_open_goal(self.eid))
        self.store.update_node_state(self.eid, "goal-1", "met")
        self.assertFalse(self.store.has_open_goal(self.eid))

    def test_orchestration_panel_can_tell_who_is_running(self):
        """面板要能区分「待领」与「在跑」：两种都算 active，含义却不同——
        前者是"平台随时会动手"，后者是"已经在动手，改它之前先想清楚"。"""
        intent = self._intent("打 80 端口")
        idle = [i for i in self.store.orchestration(self.eid)["intents"]
                if i["id"] == intent["id"]][0]
        self.assertEqual("", idle["owner"])
        self.store.claim_intent(self.eid, intent["id"], "work#1")
        busy = [i for i in self.store.orchestration(self.eid)["intents"]
                if i["id"] == intent["id"]][0]
        self.assertEqual("work#1", busy["owner"])
        # 状态列没变：owner 是运行期标识，不该顺手把意图改成别的状态。
        self.assertEqual("active", busy["state"])


class RoundTests(_Case):
    """一拍里的两件事：领活、规划。"""

    def test_work_round_takes_the_highest_priority_intent(self):
        low = self._intent("低", priority=1)
        high = self._intent("高", priority=9)
        self._use_model()
        result = orchestrator.work_round(
            self.plane, self.eid, owner="work#1",
            profile={"model": "fake-1"}, api_key="k", policy_id="full")
        self.assertIsNotNone(result)
        self.assertEqual(high["id"], result["intentId"])
        self.assertTrue(result["ok"])
        # 活的正文必须写清是哪一条意图：工人只看得到这段文字。
        self.assertIn(high["id"], self.spawn.objectives[0][0])
        self.assertEqual("done", self.store.get_node(self.eid, high["id"])["state"])
        self.assertEqual("active", self.store.get_node(self.eid, low["id"])["state"])

    def test_work_round_marks_a_failed_intent_blocked_instead_of_requeueing(self):
        intent = self._intent("做不成的一件")
        self._use_model()
        self.spawn.ok = False
        result = orchestrator.work_round(
            self.plane, self.eid, owner="work#1",
            profile={"model": "fake-1"}, api_key="k", policy_id="full")
        self.assertFalse(result["ok"])
        node = self.store.get_node(self.eid, intent["id"])
        self.assertEqual("blocked", node["state"])
        self.assertEqual("", node["owner"])
        # 关键断言：它没回到队列。否则每一拍都会重新领到它、再烧一次模型调用。
        self.assertEqual([], self.store.frontier(self.eid))
        # 但 blocked 有显式的复活口，人解开环境问题后点一次就回来。
        self.assertEqual(1, self.store.rerun_blocked_intents(self.eid)["count"])
        self.assertEqual([intent["id"]],
                         [row["id"] for row in self.store.frontier(self.eid)])

    def test_work_round_releases_the_owner_even_when_it_succeeds(self):
        intent = self._intent("一")
        self._use_model()
        orchestrator.work_round(self.plane, self.eid, owner="work#1",
                                profile={"model": "fake-1"}, api_key="k",
                                policy_id="full")
        self.assertEqual("", self.store.get_node(self.eid, intent["id"])["owner"])

    def test_work_round_returns_none_when_the_queue_is_empty(self):
        self._use_model()
        self.assertIsNone(orchestrator.work_round(
            self.plane, self.eid, owner="work#1",
            profile={"model": "fake-1"}, api_key="k", policy_id="full"))
        self.assertEqual([], self.spawn.roles)      # 没活就不该派子智能体

    def test_work_round_reports_the_budget_it_actually_spent(self):
        self._intent("一")
        self._use_model()
        self.spawn.charges = 3
        result = orchestrator.work_round(self.plane, self.eid, owner="work#1",
                                         profile={"model": "fake-1"}, api_key="k",
                                         policy_id="full", budget=10)
        self.assertEqual(3, result["charged"])
        self.assertEqual(10, self.spawn.budgets[0].limit)

    def test_insecure_reaches_the_child(self):
        """TLS 校验的开关必须一路传到子智能体。

        这条链有四层（run → tick → work_round/plan_round → _spawn_one → spawn），
        任何一层写成默认值 False，症状都是"用户在对话里点开了跳过校验，工人却仍在
        校验"——而工人连的是同一批自签名的目标，失败的样子就是一条普通的连接错误，
        没人会往这个方向查。
        """
        self._intent("一")
        self._use_model()
        orchestrator.work_round(self.plane, self.eid, owner="work#1",
                                profile={"model": "fake-1"}, api_key="k",
                                policy_id="full", insecure=True)
        self.assertTrue(self.spawn.arguments[0].get("insecure"))
        orchestrator.plan_round(self.plane, self.eid,
                                profile={"model": "fake-1"}, api_key="k",
                                policy_id="full", insecure=True)
        self.assertTrue(self.spawn.arguments[1].get("insecure"))
        # 反过来也必须成立：不传时是 False，不是"上次那个 True 留在某个层的默认值里"。
        self._intent("二")
        orchestrator.work_round(self.plane, self.eid, owner="work#2",
                                profile={"model": "fake-1"}, api_key="k",
                                policy_id="full")
        self.assertFalse(self.spawn.arguments[2].get("insecure"))

    def test_plan_round_stamps_the_state_and_clears_the_dirty_flag(self):
        self._use_model()
        orchestrator.kick(self.eid)
        self.assertTrue(orchestrator._state(self.eid)["dirty"])
        before = orchestrator._state(self.eid)["lastPlan"]
        result = orchestrator.plan_round(self.plane, self.eid,
                                         profile={"model": "fake-1"}, api_key="k",
                                         policy_id="full")
        state = orchestrator._state(self.eid)
        self.assertTrue(result["ok"])
        self.assertFalse(state["dirty"])
        self.assertEqual(1, state["plans"])
        self.assertGreater(state["lastPlan"], before)
        self.assertEqual(["规划"], self.spawn.roles[0])

    def test_plan_objective_demands_an_intent_only_when_the_queue_is_empty(self):
        empty = orchestrator.plan_objective(self.store, self.eid)
        self.assertIn(orchestrator.PLAN_FLOOR, empty)
        # 图里必须看得见目标本身；退化成一个编号（goal-1）就等于让规划器
        # 对着一个编号定方向。
        self.assertIn("10.10.10.110", empty)
        self._intent("在跑的一件")
        busy = orchestrator.plan_objective(self.store, self.eid)
        self.assertNotIn(orchestrator.PLAN_FLOOR, busy)
        self.assertIn("可以不产出", busy)

    def test_plan_objective_carries_the_trigger_reason(self):
        text = orchestrator.plan_objective(self.store, self.eid,
                                           trigger="上一批活已经干完了")
        self.assertIn("## 触发原因", text)
        self.assertIn("上一批活已经干完了", text)


class TickTests(_Case):
    """一拍的整体节奏：先派活、再规划，以及各种该停下的条件。"""

    def test_tick_without_a_model_does_nothing_and_says_why(self):
        self._intent("一")
        report = orchestrator.tick(self.plane, max_workers=1,
                                   engagement_id=self.eid)
        self.assertEqual(0, report["worked"])
        self.assertEqual(0, report["planned"])
        self.assertIn("模型配置", report["skipped"])
        self.assertEqual([], self.spawn.roles)

    def test_tick_works_before_it_plans(self):
        self._intent("一")
        self._use_model()
        self._rewind()
        report = orchestrator.tick(self.plane, max_workers=1,
                                   engagement_id=self.eid)
        self.assertEqual(1, report["worked"])
        self.assertEqual(1, report["planned"])
        # 顺序反了的话，规划器每一拍都看到"队列空、没人跑"，于是每拍硬塞一条。
        self.assertEqual(["执行", "规划"], [roles[0] for roles in self.spawn.roles])

    def test_tick_stops_dispatching_at_max_workers(self):
        for index in range(3):
            self._intent("第 %d 件" % (index + 1), priority=index)
        self._use_model()
        self._rewind()
        report = orchestrator.tick(self.plane, max_workers=2,
                                   engagement_id=self.eid)
        self.assertEqual(2, report["worked"])
        # 一次 tick 别把队列抽干：抽干后本轮规划看到空队列会立刻再规划，节奏乱。
        self.assertEqual(1, len(self.store.frontier(self.eid)))

    def test_tick_skips_a_paused_engagement(self):
        self._intent("一")
        self._use_model()
        self._rewind()
        self.store.set_engagement_paused(self.eid, True)
        report = orchestrator.tick(self.plane, max_workers=1,
                                   engagement_id=self.eid)
        self.assertEqual(0, report["worked"])
        self.assertIn("暂停", report["skipped"])
        self.assertEqual([], self.spawn.roles)

    def test_tick_skips_a_met_goal_without_calling_it_skipped(self):
        self._intent("一")
        self._use_model()
        self._rewind()
        self.store.update_node_state(self.eid, "goal-1", "met")
        report = orchestrator.tick(self.plane, max_workers=1,
                                   engagement_id=self.eid)
        self.assertEqual(0, report["worked"])
        self.assertEqual(0, report["planned"])
        # 不是"停机"：这一行做完了，不该被报成需要人处理的跳过原因。
        self.assertEqual("", report["skipped"])
        self.assertEqual([], self.spawn.roles)

    def test_tick_waits_out_the_debounce_when_it_just_planned(self):
        self._use_model()
        orchestrator._state(self.eid)["dirty"] = False
        orchestrator._state(self.eid)["lastPlan"] = time.time()
        report = orchestrator.tick(self.plane, max_workers=1,
                                   engagement_id=self.eid)
        self.assertEqual(0, report["planned"])
        self.assertEqual([], self.spawn.roles)

    def test_tick_plans_when_the_graph_was_kicked_even_with_work_left(self):
        self._intent("一")
        self._use_model()
        self._rewind()
        orchestrator._state(self.eid)["dirty"] = False
        # 队列里还有活、也远不到 600 秒心跳，但图刚变过（人手动加了意图 / 目标）——
        # dirty 若不参与判定，`kick` 就只是一个只写不读的标记。
        orchestrator.kick(self.eid)
        report = orchestrator.tick(self.plane, max_workers=0,
                                   engagement_id=self.eid)
        self.assertEqual(1, report["planned"])

    def test_tick_runs_a_task_that_brought_its_own_model(self):
        """任务行上单独指定的模型，后台必须认。

        这一条钉的是「手动建的任务不会执行」：会话自带模型选择，所以会话里跑的
        任务看着正常；而手建的任务全靠后台每一拍去解析模型，后台若只认全局启用项，
        没配全局模型的机器上它每一拍都在第一句返回，任务页永远停在「未开始」。"""
        self._intent("一")
        own = self.store.save_profile(
            "m-own", "这条任务自己的", "openai_compat", "http://127.0.0.1:9/v1",
            "fake-1", "none", "", None, 0.2, 512, 20, "")
        self.store.update_task(self.eid, {"llmProfileId": own["id"]})
        self._rewind()
        report = orchestrator.tick(self.plane, max_workers=1,
                                   engagement_id=self.eid)
        self.assertNotIn("模型配置", report["skipped"])
        self.assertNotEqual([], self.spawn.roles)

    def test_one_task_without_a_model_does_not_stop_the_others(self):
        """一个目标没配模型，只该让它自己这一拍空转，不该把整拍按停。

        模型取在循环外面的时候，`list_engagements` 里只要有一条配不上，
        同一拍里**所有**目标一起不动——而症状是「有的任务跑、有的永远不跑」，
        排查时最容易被当成那几条任务自己的问题。"""
        self._intent("一")
        bare = self.store.create_engagement("10.10.10.111", "拿下 111", "本人授权")["id"]
        own = self.store.save_profile(
            "m-own", "这条任务自己的", "openai_compat", "http://127.0.0.1:9/v1",
            "fake-1", "none", "", None, 0.2, 512, 20, "")
        self.store.update_task(self.eid, {"llmProfileId": own["id"]})
        self._rewind()
        orchestrator._state(bare)["lastPlan"] = time.time() - 30.0
        self.assertIsNone(self.store.active_profile())
        report = orchestrator.tick(self.plane, max_workers=1)
        self.assertEqual(1, report["planned"])
        self.assertEqual(self.eid, report["plans"][0]["engagementId"])

    def test_tick_does_not_wait_for_the_heartbeat_when_the_queue_is_empty(self):
        self._use_model()
        # 队列空、dirty 已清、只过了防抖窗口（远不到 600 秒心跳）——这时必须规划，
        # 否则一个刚建好的目标要干等十分钟才动第一下。
        orchestrator._state(self.eid)["dirty"] = False
        self._rewind(orchestrator.DEBOUNCE_SECONDS + 1.0)
        report = orchestrator.tick(self.plane, max_workers=1,
                                   engagement_id=self.eid)
        self.assertEqual(1, report["planned"])

    def test_tick_takes_the_task_worker_count_as_how_many_run_at_once(self):
        """任务行上配的并发数，后台必须认。

        那个列此前只被任务面读写（建任务、改任务、行上显示），编排侧一次都没
        读过——于是界面上写着 3，实际永远一条一条跑，而"为什么我的任务这么慢"
        在那条链路上查不出来：库里那个数看着是对的。
        """
        for index in range(3):
            self._intent("第 %d 件" % (index + 1), priority=index)
        self._use_model()
        self._rewind()
        self.store.update_task(self.eid, {"workerCount": 3})
        report = orchestrator.tick(self.plane, max_workers=1,
                                   engagement_id=self.eid)
        self.assertEqual(3, report["worked"], "配了 3 并发却只跑了一条")
        # 三条意图各成一个 spec、**同一次 spawn 里一起派**：分三次 spawn 也对，
        # 但那不是这里要的（那等于串行三次，只是没等前一次跑完）。
        # 只看"执行"那一批：这一拍末尾还会规划一次（那是另一条路）。
        batches = [roles for roles in self.spawn.roles if roles[0] == "执行"]
        self.assertEqual([["执行"] * 3], batches)
        self.assertEqual(3, len(self.spawn.objectives[0]))
        self.assertEqual(3, len(set(self.spawn.objectives[0])), "三条意图派成了同一个活")

    def test_a_task_worker_count_of_one_stays_on_the_serial_path(self):
        """1 是默认值，语义是"一次一条"，不该被当成"没配"而走批量那条路。

        两条路的分界要是写成 >= 1，串行那条就永远走不到了；而串行与批量的
        差别不只是快慢——串行一次只领一条，规划器下一拍看到的是"剩下的还在队列里"。
        """
        for index in range(3):
            self._intent("第 %d 件" % (index + 1), priority=index)
        self._use_model()
        self._rewind()
        self.store.update_task(self.eid, {"workerCount": 1})
        report = orchestrator.tick(self.plane, max_workers=1,
                                   engagement_id=self.eid)
        self.assertEqual(1, report["worked"])
        self.assertEqual([["执行"]],
                         [roles for roles in self.spawn.roles if roles[0] == "执行"])


class BatchWorkTests(_Case):
    """一次领 N 条一起派：并行不是把同一条活派三次。"""

    def setUp(self):
        super().setUp()
        self._use_model()

    def test_work_batch_claims_one_intent_per_route(self):
        first = self._intent("先看后台", priority=9)
        second = self._intent("再试弱口令", priority=8)
        results = orchestrator.work_batch(
            self.plane, self.eid, routes=2, profile={"model": "fake-1"},
            api_key="k", policy_id="full")
        self.assertEqual(2, len(results))
        self.assertEqual({first["id"], second["id"]},
                         {item["intentId"] for item in results})
        self.assertEqual(2, len(self.spawn.objectives[0]),
                         "两条路由只派了一个子智能体")
        self.assertIn(first["id"], self.spawn.objectives[0][0])
        self.assertIn(second["id"], self.spawn.objectives[0][1])

    def test_every_batched_intent_leaves_the_queue_with_its_own_state(self):
        """成与败都各有各的落点：一条失败不能把别的那条也算成做完。

        批量最容易写错的地方就是"按整批一个结果记"——那会让一条走不通的意图
        在库里显示 done，而它其实什么都没产出。
        """
        self._intent("能走通", priority=9)
        self._intent("走不通", priority=8)
        self.spawn.errors = {2: "子智能体超时"}
        results = orchestrator.work_batch(
            self.plane, self.eid, routes=2, profile={"model": "fake-1"},
            api_key="k", policy_id="full")
        self.assertEqual([True, False], [item["ok"] for item in results])
        self.assertEqual([], self.store.frontier(self.eid),
                         "领出来的意图必须离开 frontier，不能等下一拍再领一次")
        nodes = self.store.list_nodes(self.eid, kind="intent")
        states = sorted(node["state"] for node in nodes)
        self.assertEqual(["blocked", "done"], states,
                         "一条失败应该标 blocked，另一条标 done")

    def test_work_batch_releases_the_claim_even_when_the_child_fails(self):
        """认领过的意图要放手。

        不放的话那条意图的 owner 一直挂着，进程重启前谁也再领不动它；
        而界面上它只是"一条正在跑的意图"，看起来完全正常。
        """
        intent = self._intent("一")
        self.spawn.errors = {1: "子智能体超时"}
        orchestrator.work_batch(self.plane, self.eid, routes=1,
                                profile={"model": "fake-1"}, api_key="k",
                                policy_id="full")
        node = self.store.get_node(self.eid, intent["id"])
        self.assertEqual("", node.get("owner") or "")

    def test_work_batch_spreads_the_budget_across_the_specs(self):
        """预算按条数放大，不是几条共用一份。

        不放大时三条意图挤在一份额度里：第一条跑满、后两条连开头都开不了，
        症状是"并行之后反而更慢了"——因为后两条是空转的。
        """
        for index in range(3):
            self._intent("第 %d 件" % (index + 1), priority=index)
        results = orchestrator.work_batch(
            self.plane, self.eid, routes=3, profile={"model": "fake-1"},
            api_key="k", policy_id="full", budget=4)
        self.assertEqual(12, self.spawn.budgets[0].limit)
        self.assertEqual(3, sum(item["charged"] for item in results))

    def test_work_batch_returns_nothing_when_the_queue_is_empty(self):
        self.assertEqual([], orchestrator.work_batch(
            self.plane, self.eid, routes=4, profile={"model": "fake-1"},
            api_key="k", policy_id="full"))
        self.assertEqual([], self.spawn.roles, "队列空时不该派子智能体")

class RunTests(_Case):
    """对话里点"推进编排"：事件、预算与失败口径。"""

    def _run(self, args, **kwargs):
        options = {"profile": {"model": "fake-1"}, "api_key": "k",
                   "max_steps": 12, "steps": 0, "policy_id": "full",
                   "fallback_eid": ""}
        options.update(kwargs)
        return _drive(orchestrator.run(self.plane, args, **options))

    def test_run_without_a_target_reports_an_error_event(self):
        events, (result, charged) = self._run({})
        self.assertEqual("error", events[0]["phase"])
        self.assertFalse(result["ok"])
        self.assertIn("编排要指定目标", result["summary"])
        self.assertEqual(0, charged)

    def test_run_falls_back_to_the_current_engagement(self):
        self._intent("一")
        self._use_model()
        self._rewind()
        events, (result, _charged) = self._run({}, fallback_eid=self.eid)
        self.assertTrue(result["ok"], result["summary"])
        self.assertEqual(self.eid, result["data"]["engagementId"])

    def test_run_rejects_an_unknown_engagement(self):
        with self.assertRaises(KeyError):
            self._run({"engagementId": "E-not-there"})

    def test_run_yields_round_and_done_events(self):
        self._intent("一")
        self._use_model()
        self._rewind()
        events, (result, charged) = self._run({"engagementId": self.eid,
                                               "rounds": 1})
        phases = [event["phase"] for event in events]
        self.assertEqual(["round", "done"], phases)
        self.assertTrue(result["ok"], result["summary"])
        self.assertIn("派了 1 个活", result["summary"])
        self.assertEqual(self.eid, result["touched"][0])
        self.assertGreater(charged, 0)
        self.assertEqual(charged, result["data"]["budget"]["charged"])

    def test_run_hands_insecure_down_to_the_tick(self):
        """`run` 这一层也要传：它是唯一知道"这一轮对话开了跳过校验"的地方。"""
        self._intent("一")
        self._use_model()
        self._rewind()
        self._run({"engagementId": self.eid, "rounds": 1}, insecure=True)
        self.assertTrue(self.spawn.arguments[0].get("insecure"))

    def test_run_stops_when_the_budget_cannot_pay_for_another_child(self):
        self._intent("一")
        self._use_model()
        self._rewind()
        events, (result, charged) = self._run({"engagementId": self.eid,
                                               "rounds": 3},
                                              max_steps=subagent.CHILD_MIN_TOOL_CHARGES,
                                              steps=0)
        # 预算只够一步时连一个孩子都养不起：生成器必须立刻收尾，而不是硬派。
        self.assertEqual([], [e for e in events if e["phase"] == "round"])
        self.assertFalse(result["ok"])
        self.assertEqual(0, charged)

    def test_starting_up_releases_owners_left_by_a_dead_process(self):
        low = self._intent("低级", priority=1)
        self.store.claim_intent(self.eid, low["id"], "work#7")
        self.assertEqual([], self.store.frontier(self.eid))
        # 进程重启：旧 owner 指向的线程已经不在了。不解开的话，这条意图既不会被重领、
        # 也不会出现在 frontier 里——症状与"探索停摆"一模一样。
        self.assertEqual(1, orchestrator._release_dead_owners(self.plane))
        self.assertEqual([low["id"]],
                         [n["id"] for n in self.store.frontier(self.eid)])

    def test_releasing_dead_owners_is_idempotent(self):
        low = self._intent("低级", priority=1)
        self.store.claim_intent(self.eid, low["id"], "work#7")
        self.assertEqual(1, orchestrator._release_dead_owners(self.plane))
        # 第二次没有可解的：不能把别人刚领到的也解开（那是并发下唯一会出事的方向）。
        self.assertEqual(0, orchestrator._release_dead_owners(self.plane))

    def test_run_forever_survives_a_failing_tick(self):
        calls = []
        original_tick = orchestrator.tick
        stop = threading.Event()

        def broken(plane, **kwargs):
            calls.append(1)
            stop.set()
            raise RuntimeError("这一拍炸了")

        orchestrator.tick = broken
        try:
            thread = threading.Thread(
                target=orchestrator.run_forever, args=(self.plane, stop),
                daemon=True)
            thread.start()
            thread.join(5)
        finally:
            orchestrator.tick = original_tick
        self.assertGreaterEqual(len(calls), 1)
        # 一拍出事不能带走线程：带走了编排就永久停摆，而界面只显示"有活没人干"。
        self.assertFalse(thread.is_alive())


class WiringTests(unittest.TestCase):
    """接线：角色工具表里的每个名字都必须真的能执行、真的分过级。"""

    def test_every_role_tool_exists_and_has_a_level(self):
        names = sorted(set(orchestrator.PLANNER_TOOLS)
                       | set(orchestrator.WORKER_TOOLS))
        missing_handlers = [name for name in names if name not in chat.HANDLERS]
        missing_levels = [name for name in names if name not in policy.TOOL_LEVEL]
        self.assertEqual([], missing_handlers)
        self.assertEqual([], missing_levels)

    def test_orchestrate_is_a_runtime_tool_and_a_handler(self):
        self.assertIn(orchestrator.TOOL, chat._RUNTIME_TOOLS)
        self.assertIn(orchestrator.TOOL, chat.HANDLERS)
        self.assertIn(orchestrator.TOOL, policy.TOOL_LEVEL)

    def test_wanted_drops_names_the_policy_never_levelled(self):
        wanted = orchestrator._wanted(["graph_overview", "not_a_tool"])
        self.assertEqual(["graph_overview"], wanted)
