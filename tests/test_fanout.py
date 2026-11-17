# -*- coding: utf-8 -*-
"""`pentestd/fanout.py` 的用例。

这里守的不是"能拆"，而是**拆错与拆漏的边界**：

* 拆漏（该拆没拆）只是退回顺序处理，代价小；
* 拆错（不该拆拆了）会把一句陈述拆成几份侦察，多花时间与 token，而且用户会
  看到六个子智能体在打两个只是被提到的地址。

所以 `PlanTestCase` 里"不该拆"的用例比"该拆"的多。`RunTestCase` 用假的
`subagent.spawn` 把预算与汇报形状钉住——真派活要模型与网络，不适合放进单测。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import fanout, subagent                                   # noqa: E402


class PlanTestCase(unittest.TestCase):
    """`fanout.plan`：什么时候该拆。"""

    def setUp(self):
        os.environ.pop(fanout._SKIP_ENV, None)

    def tearDown(self):
        os.environ.pop(fanout._SKIP_ENV, None)

    def test_two_targets_with_an_action_word_are_split(self):
        decision = fanout.plan("扫描 10.0.0.1 和 10.0.0.2 的 80、443 端口")
        self.assertEqual(decision["targets"], ["10.0.0.1", "10.0.0.2"])

    def test_a_statement_without_an_action_word_is_not_split(self):
        self.assertIsNone(fanout.plan("10.0.0.1 是网关，10.0.0.2 是 DNS"))

    def test_a_single_target_is_not_split(self):
        self.assertIsNone(fanout.plan("扫描 10.0.0.1 的 80 端口"))

    def test_fenced_code_is_treated_as_pasted_output(self):
        text = "看看这个\n```\n10.0.0.1 open 80\n10.0.0.2 open 443\n```"
        self.assertIsNone(fanout.plan(text))

    def test_a_negative_sentence_is_not_split(self):
        self.assertIsNone(fanout.plan("不要扫 10.0.0.1 和 10.0.0.2，先别动"))
        self.assertIsNone(fanout.plan("先别探测 10.0.0.1、10.0.0.2"))

    def test_file_names_are_not_domains(self):
        self.assertIsNone(fanout.plan("读一下 package.json 和 requirements.txt 的依赖"))
        self.assertIsNone(fanout.plan("检查 app.py 与 config.yml 的配置"))

    def test_a_url_and_its_host_count_once(self):
        decision = fanout.plan("看看 http://example.com/a 和 https://example.com/b")
        self.assertIsNone(decision)

    def test_two_hosts_in_urls_are_two_targets(self):
        decision = fanout.plan(
            "探测 http://a.example.com/x 和 https://b.example.com/y")
        self.assertEqual(decision["targets"],
                         ["http://a.example.com/x", "https://b.example.com/y"])

    def test_cidr_blocks_are_targets(self):
        decision = fanout.plan("扫描 10.0.0.0/24 和 10.0.1.0/24")
        self.assertEqual(decision["targets"], ["10.0.0.0/24", "10.0.1.0/24"])

    def test_domains_are_targets(self):
        decision = fanout.plan("对比 example.com 与 test.example.org 的指纹")
        self.assertEqual(decision["targets"], ["example.com", "test.example.org"])

    def test_english_action_words_work(self):
        decision = fanout.plan("scan 10.0.0.1 and 10.0.0.2")
        self.assertEqual(decision["targets"], ["10.0.0.1", "10.0.0.2"])

    def test_a_very_long_message_is_not_split(self):
        text = "扫描 10.0.0.1 和 10.0.0.2 " + ("说明" * fanout.MAX_TEXT_CHARS)
        self.assertIsNone(fanout.plan(text))

    def test_the_environment_switch_turns_it_off(self):
        os.environ[fanout._SKIP_ENV] = "0"
        self.assertIsNone(fanout.plan("扫描 10.0.0.1 和 10.0.0.2"))
        os.environ[fanout._SKIP_ENV] = "off"
        self.assertIsNone(fanout.plan("扫描 10.0.0.1 和 10.0.0.2"))

    def test_targets_keep_the_order_they_appear_in(self):
        decision = fanout.plan("扫 10.0.0.2 和 10.0.0.1 和 example.com")
        self.assertEqual(decision["targets"],
                         ["10.0.0.2", "10.0.0.1", "example.com"])

    def test_duplicates_collapse(self):
        decision = fanout.plan("扫描 10.0.0.1 和 10.0.0.1 和 10.0.0.2")
        self.assertEqual(decision["targets"], ["10.0.0.1", "10.0.0.2"])


class GroupTestCase(unittest.TestCase):
    """分桶：每人一个目标，超上限按出现顺序轮流分。"""

    def test_one_bucket_per_target(self):
        self.assertEqual(fanout._groups(["a", "b", "c"]),
                         [["a"], ["b"], ["c"]])

    def test_more_than_six_targets_fill_exactly_six_buckets(self):
        targets = ["t%d" % index for index in range(1, 10)]
        groups = fanout._groups(targets)
        self.assertEqual(len(groups), fanout.MAX_BUCKETS)
        self.assertEqual(groups[0], ["t1", "t7"])   # 轮流分：第 0 桶拿 1、7
        flat = [item for group in groups for item in group]
        self.assertEqual(sorted(flat), targets)     # 每个目标都落进且只落进一次

    def test_buckets_are_balanced(self):
        targets = ["t%d" % index for index in range(1, 8)]
        groups = fanout._groups(targets)
        sizes = sorted(len(group) for group in groups)
        self.assertEqual(sizes[-1] - sizes[0] <= 1, True)


class FakeSpawn:
    """假 `subagent.spawn`：只发 start/done，并按每个孩子扣一次预算。"""

    def __init__(self, failed=()):
        self.failed = set(failed)
        self.specs = []
        self.budget = None
        self.kwargs = {}

    def __call__(self, specs, **kwargs):
        self.specs = list(specs)
        self.budget = kwargs["budget"]
        self.kwargs = dict(kwargs)

        def generator():
            total = len(self.specs)
            for index, spec in enumerate(self.specs, 1):
                self.budget.take(index)
                yield {"type": "subagent", "phase": "start", "index": index,
                       "total": total, "role": spec.role}
                yield {"type": "subagent", "phase": "done", "index": index,
                       "total": total, "role": spec.role,
                       "ok": index not in self.failed,
                       "summary": "结论-%s" % spec.objective.split("：")[0][-4:]}
            children = []
            for index, spec in enumerate(self.specs, 1):
                ok = index not in self.failed
                children.append({
                    "index": index, "role": spec.role, "ok": ok,
                    "steps": 2, "durationMs": 1500,
                    "summary": "结论行%d" % index if ok else "",
                    "evidence": "证据行%d" % index if ok else "",
                    "error": "" if ok else "子智能体超时",
                })
            return {"ok": any(child["ok"] for child in children),
                    "summary": "汇总", "spawned": len(children),
                    "succeeded": sum(1 for child in children if child["ok"]),
                    "failed": sum(1 for child in children if not child["ok"]),
                    "concurrency": 1,
                    "budget": self.budget.public(),
                    "children": children}

        return generator()


class RunTestCase(unittest.TestCase):
    """`fanout.run`：预算、事件转发、汇报形状。"""

    def setUp(self):
        os.environ.pop(fanout._SKIP_ENV, None)
        self._original = fanout.subagent_module.spawn

    def tearDown(self):
        fanout.subagent_module.spawn = self._original
        os.environ.pop(fanout._SKIP_ENV, None)

    def _drain(self, fake, text, *, max_steps=999, steps=0):
        fanout.subagent_module.spawn = fake
        runner = fanout.run(
            None, text, profile={}, api_key="k", max_steps=max_steps,
            steps=steps, insecure=False, policy_id="full", role="redteam",
            auth_required=True, fallback_eid="E-1", session=None, gate=None)
        events = []
        while True:
            try:
                events.append(next(runner))
            except StopIteration as stop:
                return events, stop.value

    def test_two_targets_spawn_two_children_and_report_each(self):
        fake = FakeSpawn()
        events, result = self._drain(fake, "扫描 10.0.0.1 和 10.0.0.2 的端口")
        reports, payload, charged = result
        self.assertEqual(len(fake.specs), 2)
        self.assertIn("10.0.0.1", reports)
        self.assertIn("10.0.0.2", reports)
        self.assertIn("结论：结论行1", reports)
        self.assertEqual(payload["spawned"], 2)
        self.assertEqual(charged, 2)             # 假 spawn 每个孩子扣一次
        phases = [event.get("phase") for event in events
                  if event.get("type") == "subagent"]
        self.assertEqual(phases, ["start", "done", "start", "done"])
        self.assertTrue(any(event.get("kind") == "fanout" for event in events))

    def test_the_notice_comes_before_any_child_event(self):
        fake = FakeSpawn()
        events, _result = self._drain(fake, "扫描 10.0.0.1 和 10.0.0.2")
        kinds = [event.get("kind") or event.get("phase") for event in events]
        self.assertEqual(kinds[0], "fanout")

    def test_each_role_carries_its_target(self):
        fake = FakeSpawn()
        self._drain(fake, "扫描 10.0.0.1 和 10.0.0.2")
        roles = [spec.role for spec in fake.specs]
        self.assertEqual(roles, ["t1 10.0.0.1", "t2 10.0.0.2"])
        self.assertIn("10.0.0.1", fake.specs[0].objective)
        self.assertIn("只读侦察", fake.specs[0].objective)

    def test_the_budget_is_capped_even_when_the_parent_has_a_thousand_steps(self):
        fake = FakeSpawn()
        self._drain(fake, "扫描 10.0.0.1 和 10.0.0.2")
        self.assertEqual(fake.budget.limit, fanout.CHARGE_CAP)

    def test_a_short_parent_budget_is_used_as_is(self):
        fake = FakeSpawn()
        self._drain(fake, "扫描 10.0.0.1 和 10.0.0.2", max_steps=10)
        # limits = 10 - 0 - 1 = 9（留一步收尾），小于封顶 60。
        self.assertEqual(fake.budget.limit, 9)

    # ------------------------------------------------------- 共享池（m05366）
    #
    # 以前是**均分**：池子 60、2 个孩子，每人 30。先跑完的那个把 20 次锁在自己
    # 名下，另一个明明还能用却拿不到——池子没花完，活干得比池子允许的少。
    # 现在每人只留 `CHILD_MIN_TOOL_CHARGES` 次保底，其余全进公共池。

    def test_the_pool_is_shared_instead_of_split(self):
        fake = FakeSpawn()
        self._drain(fake, "扫描 10.0.0.1 和 10.0.0.2")
        budget = fake.budget
        self.assertEqual(budget.limit, fanout.CHARGE_CAP)
        # 每人名下只有保底，剩下的在公共池里——不是均分的 30。
        self.assertEqual(budget.per_child, subagent.CHILD_MIN_TOOL_CHARGES)
        # 1 号先吃 40 次（早超过均分的 30）也仍然是同一个池子里的钱。
        for _ in range(40):
            self.assertTrue(budget.take(1), "公共池没给 1 号用")
        # 2 号还能接着扣：自己的保底还剩 1 次，用完再从公共池里拿。
        self.assertTrue(budget.take(2))
        self.assertTrue(budget.take(2))

    def test_a_greedy_child_still_leaves_a_siblings_floor(self):
        """共享不等于先到先得：保底还在，喂不饱的槽位不会把兄弟饿死。"""
        fake = FakeSpawn()
        self._drain(fake, "扫描 10.0.0.1 和 10.0.0.2", max_steps=7)
        # limit = 7 - 0 - 1 = 6 → 保底 2 × 2 + 公共 2
        budget = fake.budget
        self.assertEqual((budget.limit, budget.per_child), (6, 2))
        # 假 spawn 已经给两个孩子各记了 1 次（那是它们发现自己那份保底的第一步）。
        self.assertEqual(budget.used, 2)
        for _ in range(3):                       # 1 号：自己剩的 1 + 公共 2
            self.assertTrue(budget.take(1))
        self.assertFalse(budget.take(1), "1 号把公共池和兄弟的保底一起吃掉了")
        self.assertTrue(budget.take(2), "2 号的保底被吃掉了")
        self.assertFalse(budget.take(2), "公共池已经空了，却还能接着扣")
        self.assertEqual(budget.used, 6)

    def test_the_children_are_allowed_more_model_calls_than_a_delegate_child(self):
        """预算放宽后模型调用也必须放宽，否则池子还是花不出去。"""
        fake = FakeSpawn()
        self._drain(fake, "扫描 10.0.0.1 和 10.0.0.2")
        # 2 个目标、池子 60 → 一人一半 30，高于 delegate 的 8。
        self.assertEqual(fake.kwargs.get("max_model_calls"), 30)

    def test_a_short_pool_does_not_lower_the_model_call_floor(self):
        fake = FakeSpawn()
        self._drain(fake, "扫描 10.0.0.1 和 10.0.0.2", max_steps=10)
        # limit 9 → 9 // 2 = 4，低于 `CHILD_MODEL_CALLS`，不能反向下压。
        self.assertEqual(fake.kwargs.get("max_model_calls"),
                         subagent.CHILD_MODEL_CALLS)

    def test_not_enough_budget_skips_with_a_notice(self):
        fake = FakeSpawn()
        events, result = self._drain(fake, "扫描 10.0.0.1 和 10.0.0.2",
                                     max_steps=6, steps=3)
        reports, payload, charged = result
        self.assertEqual(fake.specs, [])         # 一个孩子都没派
        self.assertEqual((reports, payload, charged), ("", None, 0))
        self.assertTrue(any(event.get("kind") == "fanout-skip" for event in events))

    def test_no_plan_returns_empty_and_spawns_nothing(self):
        fake = FakeSpawn()
        events, result = self._drain(fake, "10.0.0.1 是网关")
        self.assertEqual(result, ("", None, 0))
        self.assertEqual(events, [])
        self.assertEqual(fake.specs, [])

    def test_failures_are_reported_not_swallowed(self):
        fake = FakeSpawn(failed={2})
        events, result = self._drain(fake, "扫描 10.0.0.1 和 10.0.0.2")
        reports, payload, _charged = result
        self.assertIn("失败", reports)
        self.assertIn("子智能体超时", reports)
        self.assertEqual(payload["failed"], 1)

    def test_seven_targets_produce_six_children(self):
        fake = FakeSpawn()
        text = "扫描 " + " 和 ".join("10.0.0.%d" % index for index in range(1, 8))
        self._drain(fake, text)
        self.assertEqual(len(fake.specs), fanout.MAX_BUCKETS)
        self.assertEqual(len(fake.budget.public() and fake.specs), 6)


class ReportShapeTestCase(unittest.TestCase):
    """报告里的三行与"不要再派活"的收尾。"""

    def test_the_report_asks_for_a_summary_and_no_second_round(self):
        groups = [["10.0.0.1"], ["10.0.0.2"]]
        report = fanout._report(groups, {"children": [
            {"ok": True, "summary": "开了 80", "evidence": "nmap: 80 open",
             "steps": 3, "durationMs": 2000},
            {"ok": True, "summary": "开了 443", "evidence": "nmap: 443 open",
             "steps": 2, "durationMs": 1000},
        ]})
        self.assertIn("不要再派活", report)
        self.assertIn("结论：开了 80", report)
        self.assertIn("### 10.0.0.2", report)

    def test_a_missing_child_still_gets_a_line(self):
        report = fanout._report([["10.0.0.1"], ["10.0.0.2"]],
                                {"children": [{"ok": True, "summary": "ok"}]})
        self.assertIn("### 10.0.0.2", report)
        self.assertIn("没有返回原因", report)


class DshBranchTestCase(unittest.TestCase):
    """DSH 通道：平台报告进 seed，平台行与 DSH 原生行合并重排。"""

    class _Store:
        def get_setting(self, key, default=None):
            return default

        def set_setting(self, *args, **kwargs):
            return None

    class _Channel:
        bridge_patch = "patch"

        def __init__(self):
            self.seeded = []

        def open(self, **kwargs):
            return None

        def session_for(self, session_id):
            return {"id": "S-1", "fresh": True}

        def prompt(self, session_id, prompt):
            self.seeded.append(prompt)

        def iter_turn(self):
            yield {"type": "delta", "text": "汇总完毕"}
            yield {"type": "summary", "turn": {"reason": "completed"},
                   "subagents": [{"index": 1, "total": 1, "role": "web",
                                  "state": "done", "steps": 2, "toolCount": 3,
                                  "summary": "子会话结论", "tools": []}],
                   "usage": {}}

    def test_platform_rows_are_merged_with_dsh_rows_and_renumbered(self):
        import types
        from pentestd import chat

        channel = self._Channel()
        saved = (chat.dsh_runtime_module.channel, chat._dsh_home,
                 chat.dshplugins.conversation_teams,
                 chat.dsh_runtime_module.remember_session)
        chat.dsh_runtime_module.channel = lambda *a, **k: channel
        chat._dsh_home = lambda plane: "X"
        chat.dshplugins.conversation_teams = lambda home, profile: {"active": False}
        chat.dsh_runtime_module.remember_session = lambda *a, **k: None
        try:
            plane = types.SimpleNamespace(store=self._Store(), port=0)
            events = list(chat._run_turn_dsh(
                plane, {"provider": "deepseek", "model": "m"}, "k",
                [{"role": "user", "text": "扫描 10.0.0.1 和 10.0.0.2"}],
                "SYS", {"id": "C-1"}, "full", "redteam", 999, None,
                preface="## 平台派活报告\n不要再派活，只做汇总。",
                prior_subagents=[{"index": 1, "total": 1,
                                  "role": "t1 10.0.0.1", "state": "done"}]))
        finally:
            (chat.dsh_runtime_module.channel, chat._dsh_home,
             chat.dshplugins.conversation_teams,
             chat.dsh_runtime_module.remember_session) = saved

        persist = [event for event in events if event.get("type") == "persist"]
        self.assertTrue(persist, "DSH 这一轮没有落库事件")
        rows = persist[0]["message"]["subagents"]
        self.assertEqual([row["index"] for row in rows], [1, 2])
        self.assertEqual([row["total"] for row in rows], [2, 2])
        self.assertEqual(rows[0]["role"], "t1 10.0.0.1")
        self.assertEqual(rows[1]["role"], "web")
        self.assertIn("不要再派活", channel.seeded[0])
        self.assertIn("扫描 10.0.0.1 和 10.0.0.2", channel.seeded[0])


if __name__ == "__main__":
    unittest.main()
