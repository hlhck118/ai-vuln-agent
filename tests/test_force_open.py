# -*- coding: utf-8 -*-
"""强制放行第三道闸（`authz.FORCE_OPEN`）：它放掉了什么，以及它**没有**放掉什么。

## 为什么单独一个文件，而不是并进 `test_authz.py`

`test_authz.py` 守的是那四条判定**各自的拒绝理由与措辞**——那是机制的不变量，
不该被"本部署今天不用它"改写。所以那边的模块级把强制放行关掉，这边的模块级让它
开着，两边各自断言一种行为。并进同一个模块只有两个选择：挑一种行为丢掉，或者让
`FORCE_OPEN` 在用例之间翻来翻去——后者失败时改不回来，会污染整轮。

## 这里守的四件事

1. **放掉了拒绝**。四条判定（类别、授权说明、目标清单、跳数）在强制放行下都不再
   拒人——其中"目标清单写 `none` 也照走"这条最要紧，因为它是唯一一处**语义被
   反转**的：`none` 原本的意思正好是"一律拒绝"。
2. **判定代码还在**。`authz.enforced()` 一开，四条拒绝逐条复活。少了这一组，
   "强制放行"与"把判定删了"在测试上分不开——而它们在下次要恢复这道闸时差别极大。
3. **每一处会说话的地方都在说同一句话**。判定被绕过之后，真正危险的不是漏放，
   而是**谎报**：给模型的提示、启动横幅、界面、`describe()` 若有任何一处继续按
   旧口径说"这一类未开启"，使用者会去改一条不参与判定的参数，改了也没有任何区别。
4. **没有顺手放掉不该放的**。类别名拼错仍然抛错；配额仍然随快照下发；另外两道闸
   （作用域、权限档）一个字没动。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import authz, chat, policy                        # noqa: E402
from pentestd.server import ApiError, ControlPlane              # noqa: E402
from pentestd.store import Store                                # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")

# 本模块要的是**强制放行开着**的状态。探针式地断言一声：这条在部署里被改成
# `False` 之后，本文件会整体失败——那正是想要的信号（部署决定变了，契约该重写），
# 而不是让一堆用例继续绿着、悄悄测着另一种行为。
if not authz.FORCE_OPEN:
    raise RuntimeError(
        "authz.FORCE_OPEN 已不是 True。本文件全部用例都在断言强制放行的行为，"
        "继续跑只会测到别的东西；要么把 FORCE_OPEN 改回 True，要么按新的部署"
        "决定重写本文件。")


class GateIsOpenTestCase(unittest.TestCase):
    """四条判定各自不再拒人。"""

    def test_no_classes_enabled_still_passes(self):
        # 原本这是第一道："「凭据测试」未在部署侧开启"。空参数启动的控制面
        # 在这条上是**默认**状态，不是误配。
        snapshot = authz.gate("creds", "", [], [], "10.0.0.5", 21)
        self.assertEqual("creds", snapshot["kind"])
        self.assertFalse(snapshot["enforced"])

    def test_empty_authorization_still_passes_and_says_so(self):
        snapshot = authz.gate("exploit", "", [], [], "10.0.0.5", 80)
        self.assertEqual(snapshot["grant"], authz.FORCE_GRANT)
        self.assertIn("强制放行", snapshot["grant"],
                      "链路里那一条空白凭据要说清它为什么是空的")

    def test_a_real_authorization_is_still_carried_through(self):
        # 强制放行不该把用户写下的凭据擦掉：它只是不再**要求**，不是不再记录。
        snapshot = authz.gate("exploit", "ENG-2026-0007", [], [], "10.0.0.5", 80)
        self.assertEqual("ENG-2026-0007", snapshot["grant"])

    def test_target_none_no_longer_refuses(self):
        """语义被反转的那一条。

        `none` 原本的意思是"一律拒绝"，而且它存在的**全部**理由就是让那个相反的
        意图有一个写得出来的形式。强制放行下它不再参与判定——但 `target_mode`
        仍然照实报 `none`，否则界面会把它显示成"不限制"，而那正好是它的反面。
        """
        snapshot = authz.gate("exploit", "", [], ["none"], "10.0.0.5", 80)
        self.assertEqual(["none"], snapshot["targets"])
        self.assertEqual("none", authz.target_mode(["none"]),
                         "形态照实报：不参与判定不等于改口说它是别的形态")

    def test_a_target_outside_the_whitelist_passes(self):
        snapshot = authz.gate("exploit", "", [], ["10.0.0.0/8"],
                              "192.168.1.5", 80)
        self.assertEqual(["10.0.0.0/8"], snapshot["targets"])

    def test_the_hop_ceiling_no_longer_refuses(self):
        over = int(authz.LIMITS["pivot"]["maxHops"]) + 5
        snapshot = authz.gate("pivot", "", [], [], "10.0.0.5", 1080, hop=over)
        self.assertEqual(over, snapshot["hop"])
        # 跳数上限**仍然出现在配额里**：执行侧要把本次生效的值写进报告，
        # 而报告里写一个从没生效过的上限比不写更糟。
        self.assertIn("maxHops", snapshot["limits"])


class MechanismIsIntactTestCase(unittest.TestCase):
    """`authz.enforced()` 一开，四条拒绝逐条复活。

    这一组是"我们没有把判定删掉"的**唯一证据**。没有它，把 `gate` 的判定整段
    删干净也能让上面那一组全绿——而两者在下一次要恢复这道闸时是两件完全不同的事。
    """

    def test_all_four_refusals_come_back(self):
        cases = [
            ({}, "类别"),
            ({"targets": ["10.0.0.5"]}, "授权说明为空"),
            ({"grant": "ENG-1234", "targets": ["none"]}, "目标清单 none"),
            ({"grant": "ENG-1234", "targets": ["10.0.0.0/8"],
              "host": "192.168.1.5"}, "目标不在白名单"),
        ]
        with authz.enforced():
            self.assertFalse(authz.FORCE_OPEN)
            for extra, label in cases:
                with self.assertRaises(authz.AuthzError,
                                       msg="%s 那条判定没有复活" % label):
                    authz.gate("exploit", extra.get("grant", ""), ["all"],
                               extra.get("targets", []),
                               extra.get("host", "10.0.0.5"), 80)
            with self.assertRaises(authz.AuthzError):
                authz.gate("exploit", "ENG-1234", ["all"], ["10.0.0.5"],
                           "10.0.0.5", 80,
                           hop=int(authz.LIMITS["pivot"]["maxHops"]) + 1)

    def test_enforced_restores_even_when_the_body_raises(self):
        """断言失败时上下文也要退出。

        少了 `finally`，第一条红掉的用例会把 `FORCE_OPEN` 永久留成 False——
        后面每一条都在测另一种行为，而它们看起来还是绿的。
        """
        with self.assertRaises(ValueError):
            with authz.enforced():
                self.assertFalse(authz.FORCE_OPEN)
                raise ValueError("boom")
        self.assertTrue(authz.FORCE_OPEN)

    def test_a_misspelled_class_still_raises(self):
        """强制放行放掉的是拒绝，不是校验。

        静默放行一个未知类别会让 `limits_for` 拿到空配额，症状是执行侧按
        "这一类没有上限"跑——而那是**放得更宽**，与"少拦一次"不是一回事。
        """
        with self.assertRaises(authz.AuthzError):
            authz.gate("expliot", "", [], [], "10.0.0.5", 80)


class EveryVoiceSaysTheSameThingTestCase(unittest.TestCase):
    """判定被绕过之后，真正的风险从"漏放"变成"**谎报**"。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(os.path.join(self.tmp.name, "open.db"))
        self.plane = ControlPlane(self.store, WEB_DIR, auth_required=False)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_enabled_classes_reports_all_five(self):
        # 这个函数是"哪些类别是开的"的**唯一**答案，`gate`、`describe`、界面、
        # 模型提示都读它。只改 `gate` 一处，其余四处会继续按"全关"说话。
        self.assertEqual(["exploit", "creds", "shell", "pivot", "toolkit"],
                         authz.enabled_classes(None))
        self.assertEqual(authz.enabled_classes(["all"]),
                         authz.enabled_classes([]))

    def test_incomplete_grant_is_no_longer_a_contradiction(self):
        # 三个部署入口都拿它决定要不要在启动时报错。强制放行下"开了类别 + 写 none"
        # 不再产生"开了却什么都做不了"那个后果，留着它会让一个此刻完全可用的
        # 组合被拒绝启动。
        self.assertFalse(authz.incomplete_grant(["exploit"], ["none"]))

    def test_describe_says_force_open_and_keeps_the_other_two_gates(self):
        text = authz.describe([], [])
        self.assertIn("强制放行", text)
        self.assertNotIn("未开启", text, "五类全部视为已授权，不该再说谁没开")
        self.assertTrue(authz.force_open_text())

    def test_the_endpoint_reports_force_open_and_all_classes_on(self):
        _status, payload = self.plane.get_authz({"query": {}})
        self.assertTrue(payload["forceOpen"])
        self.assertTrue(payload["forceOpenText"])
        self.assertTrue(payload["anyEnabled"])
        self.assertEqual([item["id"] for item in authz.CLASSES],
                         payload["enabled"])
        for item in payload["classes"]:
            self.assertTrue(item["enabled"], "%s 应报为已放行" % item["id"])
            self.assertEqual("", item["flag"], "已放行就不该再给开启方式")
        # 开关不画出来：服务端说不可编辑，而原因不是"角色不够"。
        self.assertFalse(payload["canEdit"])

    def test_the_panel_is_not_offered_as_an_editable_state(self):
        # 强制放行下"改这块设置"没有对象。静默成功会让使用者以为他改动了什么，
        # 而那个改动在下一次读取时又不见了（读取读的是 `enabled_classes`）。
        with self.assertRaises(ApiError) as ctx:
            self.plane.set_authz({"query": {}, "body": {"enabled": ["exploit"],
                                                        "targets": []},
                                  "headers": {}, "user": None})
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("FORCE_OPEN", str(ctx.exception),
                      "要说清去哪儿改回来，否则这是一条死路")

    def test_the_model_view_does_not_send_the_user_to_the_flags(self):
        out = chat._tool_get_authz(self.plane, {}, "", None)
        self.assertIn("强制放行", out["summary"])
        self.assertTrue(out["data"]["forceOpen"])
        hint = out["data"]["hint"]
        self.assertIn("直接调用", hint)
        self.assertIn("不参与判定", hint,
                      "要说明为什么不必去配那两条参数，而不只是说「不必」")
        self.assertNotIn("需要部署侧显式放行", hint)

    def test_the_policy_prompt_does_not_demand_the_flag(self):
        # 这一段每个会话都会随系统提示下发，是模型判断"能不能动手"的常驻依据。
        text = policy.prompt_block("full")
        self.assertIn("强制放行", text)
        self.assertNotIn("要求在部署侧用 `--allow-intrusive` 显式放行", text)

    def test_the_ui_reads_force_open_before_it_reads_can_edit(self):
        """界面上的先后次序是有意义的。

        `forceOpen` 与 `canEdit === false` 说的是两件不同的事（"这道闸没有被判定
        的对象" vs "你的角色不够"），而服务端在这两种状态下给出的 `canEdit` 都是
        false。判据若排在后面，管理员会被引去查一个不存在的权限问题。
        """
        with open(os.path.join(WEB_DIR, "app.js"), "r",
                  encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("info.forceOpen", body)
        self.assertIn("authz-force", body)
        self.assertLess(body.index("const forceOpen"),
                        body.index("if (forceOpen)"),
                        "先读标志再分支")
        with open(os.path.join(WEB_DIR, "style.css"), "r",
                  encoding="utf-8") as handle:
            self.assertIn(".authz-force", handle.read(),
                          "新类名必须有样式，否则那一句会混进相邻的块里")


class JobIsActuallyAcceptedTestCase(unittest.TestCase):
    """落到用户看得见的那一步：**任务真的进队列了**。

    只断言 `authz.gate` 放行证明的是"那个函数不抛了"——而用户问的是"我能不能
    动手"。两者之间隔着 `create_job` 的 kind 白名单、工具适配器归类、以及
    `_pivot_grant` 里那条独立的出口网段判定。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(os.path.join(self.tmp.name, "job.db"))
        self.plane = ControlPlane(self.store, WEB_DIR, auth_required=False)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _create(self, kind, target, **extra):
        body = {"kind": kind, "target": target}
        body.update(extra)
        return self.plane.create_job(
            {"params": {}, "query": {}, "headers": {}, "body": body,
             "path": "/api/v1/jobs", "clientAddress": ""})

    def test_an_exploit_job_is_accepted_with_no_configuration_at_all(self):
        status, payload = self._create("exploit", "http://10.0.0.5:8080")
        self.assertEqual(201, status)
        self.assertEqual(1, len(self.store.jobs()),
                         "放行要落到队列上，不是只让函数返回得好看")
        grant = payload["grant"]
        self.assertFalse(grant["enforced"],
                         "快照要记下这一次没有经过准入判定")
        self.assertEqual(authz.FORCE_GRANT, grant["grant"])

    def test_all_five_intrusive_kinds_go_through(self):
        # 逐个跑而不是抽查一个：类别是**按 kind 分派**的，漏接一类只会让那一类
        # 永远做不到，而其余四类全绿。
        for kind, target in (("exploit", "http://10.0.0.5:8080"),
                             ("creds", "10.0.0.5"),
                             ("shell", "http://10.0.0.5:8080"),
                             ("toolkit", "10.0.0.5")):
            status, payload = self._create(kind, target, adapter="nmap"
                                           if kind == "toolkit" else None,
                                           driver="http-basic"
                                           if kind == "creds" else None)
            self.assertEqual(201, status, "%s 类没有通过" % kind)
            self.assertFalse(payload["grant"]["enforced"], kind)

    def test_a_cidr_egress_is_not_refused_by_the_pivot_path(self):
        """`_pivot_grant` 里那条**独立于 `gate` 的**网段判定。

        白名单为空时它原本会拒掉**每一个**网段写法（拿空列表去比 `subnet_of`
        永远不成立）。于是"出口目标写成 CIDR"会成为唯一一处仍被拦的动作——而拦
        它的那道闸此刻不参与判定。单测 `authz.gate` 覆盖不到这里：它在这条路径上
        的第二处判定。
        """
        body = {"authorization": "", "hop": 0,
                "foothold": {"host": "10.0.0.5", "port": 1080, "kind": "ssh"},
                "egress": ["192.168.1.0/24"]}
        grant = self.plane._pivot_grant(body)
        self.assertFalse(grant["enforced"])
        # 闸照判时同一个请求必须被拒。这条**不是**多余的：没有它，上面那行断言
        # 在"这段代码根本走不到"的实现下也成立。
        with authz.enforced():
            with self.assertRaises(authz.AuthzError):
                self.plane._pivot_grant(body)

    def test_the_scope_gate_is_untouched(self):
        """另外两道闸一个字没动。

        强制放行放掉的是第三道。把作用域也顺手放开，会让"先判能不能打、再判
        打多少"里的第一层消失——而那一层是**另一份**清单，变化频率与审批人
        都不同。
        """
        plane = ControlPlane(self.store, WEB_DIR, auth_required=False,
                             probe_scope=["10.0.0.0/8"])
        with self.assertRaises(ApiError) as ctx:
            plane.create_job(
                {"params": {}, "query": {}, "headers": {},
                 "body": {"kind": "probe", "target": "192.168.1.5"},
                 "path": "/api/v1/jobs", "clientAddress": ""})
        self.assertEqual(403, ctx.exception.status, "探测作用域仍然要拦人")


if __name__ == "__main__":
    unittest.main()
