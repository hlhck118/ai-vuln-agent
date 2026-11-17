# -*- coding: utf-8 -*-
"""智能体团队与 Agent 循环：依赖、派活、计划审批、循环的四种停法。

这一组用例的核心不是"函数返回了什么"，而是四件**只有真跑一遍才知道**的事：

1. **依赖是先决条件，不是提示。** 点名一张没就绪的任务必须**被拒绝**，而拒绝的
   那句话里要有缺哪一个先决任务。只断言"没跑"的用例会放过一种更坏的实现：
   照样跑，只是记成"未就绪"。
2. **子智能体的工具调用也过审查。** 这是我自己上一轮留下的洞：`_run_child` 直接
   调 `execute_tool`，而那条路上没有审查 —— 于是 `delegate` 一行就绕开了
   `chat_review`。所以这里**在审查单出现时去问文件系统**，文件必须还不存在。
3. **循环的每一轮都从父轮预算里扣。** "循环 = 预算加倍"是这条能力最容易写错、
   也最难从结果上看出来的地方（结果看起来完全正常，只是这轮多花了三倍）。
4. **计划审批卡上真的有计划。** 只断言"建了一张 team_run 的审查单"是不够的：
   卡片上没有"谁去、做什么、手上有什么工具"的话，读的人没有任何判断材料，
   而那张单子在界面上看起来完全正常。
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import (approval, chat, harness, loop, policy, server,   # noqa: E402
                      subagent, team, terminal)
from pentestd.store import Store                                      # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


# ------------------------------------------------------------------ 假模型

def _reply_text(text):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


def _reply_tool(name, arguments, call_id="call-1"):
    return {"choices": [{"message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name,
                                     "arguments": json.dumps(arguments,
                                                             ensure_ascii=False)}}]},
        "finish_reason": "tool_calls"}]}


class FakeModel:
    """最小模型端点。脚本由 `responder(record)` 决定 —— 与 `test_harness.py` 同款。"""

    def __init__(self, responder):
        self.responder = responder
        self.requests = []
        outer = self
        self.lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                record = {"path": self.path, "body": json.loads(raw or "{}")}
                with outer.lock:
                    outer.requests.append(record)
                payload = json.dumps(outer.responder(record) or {},
                                     ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self):
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d/v1" % (host, port)

    def profile(self):
        return {"provider": "openai_compat", "baseUrl": self.base_url,
                "model": "fake-1", "temperature": 0.0, "maxTokens": 512,
                "timeoutS": 20, "name": "假模型"}

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def drive(runner):
    """抽干一个"yield 事件、return 结果"的生成器。返回 `(事件, 返回值)`。"""
    events = []
    while True:
        try:
            events.append(next(runner))
        except StopIteration as stop:
            return events, stop.value


class Decider(threading.Thread):
    """扮演"看着界面点按钮的人"。读的是**库里的表**，不是内存钩子。

    `action` 三种：`approve` / `deny` / `expire`。第三种不是"点拒绝"，而是**时间
    到了** —— 两者在结果上一样（都不执行），但库里要能区分是谁的主意。它也是
    "超时按拒绝处理"那条 fail-closed 的唯一快速验证方式：真等 300 秒不现实。
    """

    def __init__(self, store, action="approve", note=""):
        super().__init__(daemon=True)
        self.store = store
        self.action = action
        self.note = note
        self.seen = []
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            for row in self.store.list_approvals(status="pending", limit=20):
                if row["id"] in self.seen:
                    continue
                self.seen.append(row["id"])
                time.sleep(0.05)
                if self.action == "expire":
                    self.store.settle_approval(row["id"], "expired",
                                               note=self.note or "测试里不等超时")
                else:
                    self.store.decide_approval(
                        row["id"], approve=self.action == "approve",
                        decided_by="tester", note=self.note)
            time.sleep(0.05)


class PlaneCase(unittest.TestCase):
    """公共夹具：临时库 + 真控制面 + 假模型 + 自动清理。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(os.path.join(self.directory.name, "pentest.db"))
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.sessions = {"sessionId": "S-test", "userId": "U-1"}

    def set_mode(self, mode):
        self.store.set_setting(harness.SETTING_KEY, mode)
        self.addCleanup(self.store.set_setting, harness.SETTING_KEY,
                        harness.DEFAULT_MODE)

    def shell_command(self, path, marker):
        name, _prefix = terminal.resolve_shell()
        if name in ("pwsh", "powershell"):
            return "Set-Content -LiteralPath '%s' -Value '%s'" % (path, marker)
        return "printf '%%s' '%s' > '%s'" % (marker, path)

    def build_team(self, members=("m1", "m2"), goal="两件有先后的事"):
        team_row = team.create(self.plane, {"name": "演示队", "goal": goal},
                               self.sessions)["data"]["team"]
        for name in members:
            team.add_member(self.plane, {"name": name, "role": name,
                                         "brief": "负责 %s 那一步" % name},
                            self.sessions)
        return team_row

    def targets(self):
        return (os.path.join(self.directory.name, "first.txt"),
                os.path.join(self.directory.name, "second.txt"))


# ------------------------------------------------------------------ 依赖图

class GraphTestCase(unittest.TestCase):
    """依赖判定的纯逻辑。三条边界，每条对应一种真实的错法。"""

    def task(self, tid, status="pending", deps=()):
        return {"id": tid, "status": status, "dependsOn": list(deps),
                "seq": 1, "subject": tid}

    def test_ready_requires_every_dependency_done(self):
        tasks = [self.task("A", "done"), self.task("B", "pending", ["A"]),
                 self.task("C", "pending", ["A", "D"]),
                 self.task("D", "pending")]
        self.assertEqual([t["id"] for t in team.ready(tasks)], ["B", "D"])

    def test_running_is_not_ready(self):
        """在飞的任务不算就绪：再派一次会在目标上做两遍。"""
        self.assertEqual(team.ready([self.task("A", "running")]), [])

    def test_a_failed_dependency_is_not_the_same_as_waiting(self):
        """两种"没就绪"要说清是哪一种 —— 后一种是**等不到的**。"""
        by_id = {"A": self.task("A", "failed"), "B": self.task("B", "pending",
                                                               ["A"])}
        reason = team.blocked_reason(by_id["B"], by_id)
        self.assertIn("失败", reason)
        self.assertNotIn("等先决任务", reason)

    def test_a_missing_dependency_is_never_ready(self):
        """指向不存在的任务 → **永不就绪**，而不是"没有依赖"。"""
        tasks = [self.task("A", "pending", ["K-nope"])]
        self.assertEqual(team.ready(tasks), [])
        self.assertIn("K-nope", team.blocked_reason(tasks[0], {"A": tasks[0]}))

    def test_normalize_deps_rejects_unknown_ids(self):
        with self.assertRaises(team.TeamError) as caught:
            team.normalize_deps([self.task("A")], "T-1", ["K-nope"])
        self.assertIn("K-nope", str(caught.exception))

    def test_a_cycle_is_refused_with_the_path(self):
        """环上的任务**永远不会就绪**，而每一张看起来都正常 —— 必须拒绝并指出环。"""
        tasks = [self.task("A", deps=["B"]), self.task("B")]
        with self.assertRaises(team.TeamError) as caught:
            team.normalize_deps(tasks, "T-1", ["A"], self_id="B")
        message = str(caught.exception)
        self.assertIn("环", message)
        self.assertIn("A", message)

    def test_self_dependency_is_refused(self):
        with self.assertRaises(team.TeamError):
            team.normalize_deps([self.task("A")], "T-1", ["A"], self_id="A")


# ------------------------------------------------------------------ 任务板

class BoardTestCase(PlaneCase):

    def test_dependency_refuses_a_named_task_that_is_not_ready(self):
        """**依赖是先决条件，不是提示** —— 这条是团队与一次派活的全部差别。"""
        self.build_team()
        first = team.add_task(self.plane, {"subject": "先跑", "assignee": "m1",
                                          "description": "第一步"},
                              self.sessions)["data"]["task"]
        second = team.add_task(self.plane, {"subject": "后跑", "assignee": "m2",
                                           "description": "第二步",
                                           "dependsOn": [first["id"]]},
                               self.sessions)["data"]["task"]
        outcome, charged = drive(team.run(
            self.plane, {"taskIds": [second["id"]]}, profile={}, api_key="k",
            max_steps=6, steps=0, policy_id="full",
            session=self.sessions))[1]
        self.assertFalse(outcome["ok"])
        self.assertIn("还没就绪", outcome["summary"])
        self.assertIn(first["id"], outcome["summary"],
                      "拒绝时要指出缺哪一个先决任务，否则模型只能猜")
        self.assertEqual(charged, 0)
        self.assertEqual(self.store.get_team_task(second["id"])["status"],
                         "pending", "没就绪的任务不该被认领")

    def test_the_board_says_why_a_task_is_not_ready(self):
        self.build_team()
        first = team.add_task(self.plane, {"subject": "先跑", "assignee": "m1"},
                              self.sessions)["data"]["task"]
        team.add_task(self.plane, {"subject": "后跑", "assignee": "m2",
                                   "dependsOn": [first["id"]]}, self.sessions)
        snapshot = team.board(self.plane, self.store.find_team("S-test")["id"])
        waiting = [t for t in snapshot["tasks"] if t["subject"] == "后跑"][0]
        self.assertFalse(waiting["ready"])
        self.assertIn(first["id"], waiting["waitingOn"])

    def test_a_member_that_does_not_exist_is_refused(self):
        self.build_team()
        with self.assertRaises(team.TeamError) as caught:
            team.add_task(self.plane, {"subject": "随便", "assignee": "nobody"},
                          self.sessions)
        self.assertIn("nobody", str(caught.exception))
        self.assertIn("m1", str(caught.exception), "要列出有哪些成员")

    def test_a_typo_in_a_named_tool_is_refused_and_a_narrow_档_is_not(self):
        """拼错工具名是**错误**（拒掉），档位不够是**越界**（降级记录）。

        这两类刻意分开处理，理由与 `subagent.parse_specs` 相同：前者改一次参数
        就能重试，而后者拒掉会让"这条目标需要更宽的档"永远不被说出来。
        """
        self.build_team()
        with self.assertRaises(team.TeamError):
            team.add_member(self.plane, {"name": "m3", "tools": ["run_comand"]},
                            self.sessions,
                            tool_names=sorted(policy.TOOL_LEVEL))
        row = team.add_member(self.plane, {"name": "m4",
                                           "tools": ["run_command"]},
                              self.sessions, tool_names=sorted(policy.TOOL_LEVEL))
        self.assertEqual(row["data"]["member"]["tools"], ["run_command"])

    def test_update_fixes_a_task_but_not_after_it_ran(self):
        self.build_team()
        task = team.add_task(self.plane, {"subject": "写错了", "assignee": "m1"},
                             self.sessions)["data"]["task"]
        team.update(self.plane, {"taskId": task["id"], "subject": "改对了"},
                    self.sessions)
        self.assertEqual(self.store.get_team_task(task["id"])["subject"], "改对了")
        self.store.start_team_task(task["id"])
        with self.assertRaises(team.TeamError) as caught:
            team.update(self.plane, {"taskId": task["id"], "subject": "又想改"},
                        self.sessions)
        self.assertIn("不能改", str(caught.exception))

    def test_the_board_survives_a_restart(self):
        """任务板是**会被用户盯着看**的那份状态：刷新页面它必须还在。

        这一条是那三张表存在的理由。所以这里换一个 `Store` 实例去读同一个文件，
        而不是接着用同一个连接。
        """
        self.build_team()
        team.add_task(self.plane, {"subject": "看得见吗", "assignee": "m1"},
                      self.sessions)
        path = self.store.path
        self.store.close()
        reopened = Store(path)
        self.addCleanup(reopened.close)
        rows = reopened.list_teams()
        self.assertEqual(len(rows), 1)
        tasks = reopened.list_team_tasks(rows[0]["id"])
        self.assertEqual([t["subject"] for t in tasks], ["看得见吗"])
        self.assertEqual([m["name"] for m in reopened.list_team_members(
            rows[0]["id"])], ["m1", "m2"])

    def test_a_child_cannot_run_a_team_or_a_loop(self):
        """不递归。子智能体点名也不行 —— 扇出量不能由模型自己决定。"""
        spec = subagent.Spec(role="kid", objective="自己开个队",
                             wanted=["team_run", "agent_loop", "team_status"])
        names, dropped = subagent.resolve_tools(spec, "full", "", True)
        self.assertNotIn("team_run", names)
        self.assertNotIn("agent_loop", names)
        self.assertIn("team_status", names, "看一眼兄弟们在干什么不算递归")
        reasons = {d["name"]: d["reason"] for d in dropped}
        self.assertIn("子智能体不能调用它", reasons.get("team_run", ""))
        self.assertIn("不能再派活", reasons.get("team_run", ""))

    def test_every_forbidden_tool_has_a_stated_reason(self):
        """名单与原因是**同一份**：加一个禁止项必须顺带回答"为什么"。

        分开写的话，名单加了一个、原因忘了写，那个工具被拒时给出的是一句没有
        信息的话；而原因写了、名单漏了，则是一条静默的越权。
        """
        self.assertEqual(set(subagent.CHILD_FORBIDDEN),
                         set(subagent.FORBIDDEN_REASONS))
        for name in sorted(subagent.CHILD_FORBIDDEN):
            self.assertTrue(subagent.forbidden_reason(name).strip(), name)
        self.assertIn("不能再派活", subagent.forbidden_reason("agent_loop"))
        self.assertIn("任务板", subagent.forbidden_reason("team_add_task"))


# ------------------------------------------------------------------ 真派活

class DispatchTestCase(PlaneCase):
    """真起子智能体、真跑工具、真写回任务行。"""

    def setUp(self):
        super().setUp()
        self.set_mode("off")
        first, second = self.targets()
        outer = self

        def responder(record):
            body = record["body"]
            text = json.dumps(body, ensure_ascii=False)
            # 收尾调用（不带 tools）或已经拿到过工具结果 → 交报告
            tools = body.get("tools")
            done = any(m.get("role") == "tool"
                       for m in (body.get("messages") or []))
            if not tools or done:
                return _reply_text("结论：%s 这一步做完了\n证据：文件已写出\n"
                                   "建议：无" % ("m1" if "m1" in text else "m2"))
            marker = "m1" if "m1" in text else "m2"
            path = first if marker == "m1" else second
            return _reply_tool("run_command",
                               {"command": outer.shell_command(path, marker),
                                "timeoutS": 20}, "call-%s" % marker)

        self.model = FakeModel(responder)
        self.addCleanup(self.model.stop)

    def run_team(self, args=None, **kwargs):
        return drive(team.run(
            self.plane, args or {}, profile=self.model.profile(), api_key="k",
            max_steps=kwargs.pop("max_steps", 8), steps=0, policy_id="full",
            session=self.sessions, **kwargs))

    def test_ready_tasks_run_and_the_result_is_written_back(self):
        row = self.build_team()
        first, second = self.targets()
        team.add_task(self.plane, {"subject": "第一步", "assignee": "m1",
                                   "description": "写 first.txt"},
                      self.sessions)
        team.add_task(self.plane, {"subject": "第二步", "assignee": "m2",
                                   "description": "写 second.txt"},
                      self.sessions)
        events, (outcome, charged) = self.run_team()
        self.assertTrue(outcome["ok"], outcome["summary"])
        self.assertTrue(os.path.exists(first))
        self.assertTrue(os.path.exists(second),
                        "两张互相独立的任务都就绪，就该都在这一批里跑")
        board = team.board(self.plane, row["id"])
        states = {t["subject"]: t["status"] for t in board["tasks"]}
        self.assertEqual(states, {"第一步": "done", "第二步": "done"})
        self.assertTrue(all("这一步做完了" in t["summary"]
                            for t in outcome["data"]["tasks"]),
                        "结论要写回任务行 —— 否则刷新之后任务板上只剩一个状态")
        self.assertGreater(charged, 0, "子智能体的工具调用要计入父轮预算")
        self.assertTrue(any(e.get("type") == "subagent" for e in events),
                        "子智能体的进度要作为一等事件流出去")

    def test_a_dependent_task_runs_only_after_its_dependency(self):
        row = self.build_team()
        first, second = self.targets()
        a = team.add_task(self.plane, {"subject": "先跑", "assignee": "m1",
                                       "description": "写 first.txt"},
                          self.sessions)["data"]["task"]
        b = team.add_task(self.plane, {"subject": "后跑", "assignee": "m2",
                                       "description": "写 second.txt",
                                       "dependsOn": [a["id"]]},
                          self.sessions)["data"]["task"]
        # 第一轮：只有 A 就绪
        _, (outcome, _) = self.run_team()
        self.assertTrue(os.path.exists(first))
        self.assertFalse(os.path.exists(second))
        self.assertIn("新就绪", outcome["summary"])
        # 第二轮：A 完成了，B 才就绪
        _, (outcome2, _) = self.run_team()
        self.assertEqual(outcome2["data"]["ran"], [b["id"]])
        self.assertTrue(os.path.exists(second),
                        "先决任务完成后，依赖它的任务必须真的能跑")
        states = {t["id"]: t["status"]
                  for t in team.board(self.plane, row["id"])["tasks"]}
        self.assertEqual(states[a["id"]], "done")
        self.assertEqual(states[b["id"]], "done")

    def test_a_task_left_to_the_captain_is_reported_not_spawned(self):
        """队长那份活不会被 `run` 碰到 —— 它要由队长自己做、用 team_done 记完成。"""
        row = self.build_team()
        task = team.add_task(self.plane, {"subject": "队长自己的活",
                                          "description": "不该被派出去"},
                             self.sessions)["data"]["task"]
        _, (outcome, charged) = self.run_team()
        self.assertEqual(outcome["data"]["spawned"], 0)
        self.assertEqual([t["id"] for t in outcome["data"]["captainOwned"]],
                         [task["id"]])
        self.assertEqual(charged, 0, "什么都没派出去就不该扣预算")
        self.assertEqual(self.store.get_team_task(task["id"])["status"], "pending",
                         "留给队长的活**不该被认领** —— 认领了却没人跑，它就会"
                         "永远停在「在跑」，而依赖它的任务永远不就绪")
        team.done(self.plane, {"taskId": task["id"], "summary": "我自己做完了"},
                  self.sessions)
        self.assertEqual(self.store.get_team_task(task["id"])["status"], "done")

    def test_a_stale_running_task_is_recovered(self):
        """上一次被中断留下的「在跑」必须退回待办，否则下游永远不就绪。"""
        row = self.build_team()
        task = team.add_task(self.plane, {"subject": "卡住的那张",
                                          "assignee": "m1",
                                          "description": "写 first.txt"},
                             self.sessions)["data"]["task"]
        self.store.start_team_task(task["id"])          # 假装上一次断了
        events, (outcome, _) = self.run_team()
        self.assertTrue(any(e.get("type") == "team" and e.get("phase") == "recover"
                            for e in events),
                        "要说出「上一次被中断」，不能悄悄改状态")
        self.assertEqual(self.store.get_team_task(task["id"])["status"], "done",
                         "退回待办之后它就该被重新跑起来（卡住的不是活，是状态）")

    def test_an_empty_dispatch_is_a_real_answer_not_an_error(self):
        """没有就绪的任务不是失败：它是一句有用的回答（等谁、还差什么）。"""
        row = self.build_team()
        task = team.add_task(self.plane, {"subject": "唯一一张", "assignee": "m1",
                                          "description": "x"},
                             self.sessions)["data"]["task"]
        self.store.start_team_task(task["id"])
        self.store.finish_team_task(task["id"], ok=True, summary="做完了")
        _, (outcome, charged) = self.run_team()
        self.assertTrue(outcome["ok"])
        self.assertIn("没有就绪的任务", outcome["summary"])
        self.assertEqual(charged, 0)

    def test_a_budget_that_cannot_be_split_refuses_the_whole_batch(self):
        self.build_team()
        team.add_task(self.plane, {"subject": "一", "assignee": "m1",
                                   "description": "x"}, self.sessions)
        team.add_task(self.plane, {"subject": "二", "assignee": "m2",
                                   "description": "y"}, self.sessions)
        _, (outcome, charged) = self.run_team(max_steps=2)
        self.assertFalse(outcome["ok"])
        self.assertIn("预算", outcome["summary"])
        self.assertEqual(charged, 0)
        states = {t["subject"]: t["status"] for t in team.board(
            self.plane, self.store.find_team("S-test")["id"])["tasks"]}
        self.assertEqual(states, {"一": "pending", "二": "pending"},
                         "摊不开就整批拒掉，不能派出去一半")


# ------------------------------------------------------------------ 子智能体也过审查

class ChildReviewTestCase(PlaneCase):
    """**上一轮留下的洞**：子智能体调 `run_command` 此前绕过审查。"""

    def setUp(self):
        super().setUp()
        self.set_mode("ask")
        self.target = os.path.join(self.directory.name, "child.txt")
        outer = self

        def responder(record):
            body = record["body"]
            if not body.get("tools"):
                return _reply_text("结论：命令跑完了\n证据：文件\n建议：无")
            if any(m.get("role") == "tool" for m in (body.get("messages") or [])):
                return _reply_text("结论：命令跑完了\n证据：文件\n建议：无")
            return _reply_tool("run_command",
                               {"command": outer.shell_command(outer.target, "kid"),
                                "timeoutS": 20}, "call-1")

        self.model = FakeModel(responder)
        self.addCleanup(self.model.stop)

    def delegate(self):
        mode = harness.resolve_mode(self.store.get_setting(harness.SETTING_KEY))
        gate = chat._child_gate(self.plane, policy_id="full", role="",
                                auth_required=True, fallback_eid="",
                                session=self.sessions, review_mode=mode)
        return drive(subagent.run_delegation(
            self.plane,
            {"children": [{"role": "kid", "objective": "在控制面写一个文件"}]},
            profile=self.model.profile(), api_key="k", max_steps=6, steps=0,
            policy_id="full", session=self.sessions, gate=gate))

    def test_a_child_command_is_reviewed_before_it_runs(self):
        decider = Decider(self.store, action="expire")
        decider.start()
        self.addCleanup(decider.stop_event.set)
        self.delegate()
        rows = self.store.list_approvals(limit=5)
        self.assertEqual([r["tool"] for r in rows], ["run_command"],
                         "子智能体调 run_command 必须留下审查单")
        self.assertEqual(rows[0]["status"], "expired",
                         "没人应答 → 按拒绝处理（fail-closed）")
        self.assertFalse(os.path.exists(self.target),
                         "审查没通过，命令就**不该**跑过")

    def test_an_approved_child_command_runs(self):
        decider = Decider(self.store, action="approve")
        decider.start()
        self.addCleanup(decider.stop_event.set)
        self.delegate()
        self.assertTrue(os.path.exists(self.target),
                        "批准之后命令必须真的跑过 —— 光有单子不算")
        self.assertEqual(self.store.list_approvals(limit=5)[0]["status"],
                         "approved")

    def test_a_denied_child_command_does_not_run(self):
        decider = Decider(self.store, action="deny", note="不许")
        decider.start()
        self.addCleanup(decider.stop_event.set)
        self.delegate()
        self.assertFalse(os.path.exists(self.target))
        row = self.store.list_approvals(limit=5)[0]
        self.assertEqual(row["status"], "denied")
        self.assertEqual(row["decidedBy"], "tester",
                         "是人拒的，不是超时 —— 两者要分得开")

    def test_a_command_that_needs_no_review_still_runs_without_one(self):
        """`chat_review=off` 下，孩子那条路也不能凭空多出一张单子。"""
        self.set_mode("off")
        decider = Decider(self.store, action="deny")   # 谁批都批不到
        decider.start()
        self.addCleanup(decider.stop_event.set)
        self.delegate()
        self.assertTrue(os.path.exists(self.target))
        self.assertEqual(self.store.list_approvals(limit=5), [])


# ------------------------------------------------------------------ 循环

class LoopTestCase(PlaneCase):
    """四种停法，每一种都要能被认出来，而且每一轮都要扣预算。"""

    def setUp(self):
        super().setUp()
        self.set_mode("off")
        outer = self
        self.conclusion_fn = lambda text: "结论：推进一步\n证据：文件\n建议：无"

        def responder(record):
            body = record["body"]
            text = json.dumps(body, ensure_ascii=False)
            if not body.get("tools") or any(
                    m.get("role") == "tool" for m in (body.get("messages") or [])):
                return _reply_text(outer.conclusion_fn(text))
            return _reply_tool("run_command",
                               {"command": outer.shell_command(
                                   os.path.join(outer.directory.name, "loop.txt"),
                                   "x"), "timeoutS": 20}, "call-1")

        self.model = FakeModel(responder)
        self.addCleanup(self.model.stop)

    def run_loop(self, args=None, **kwargs):
        return drive(loop.run(
            self.plane, args or {"goal": "把这件事推完"},
            profile=self.model.profile(), api_key="k",
            max_steps=kwargs.pop("max_steps", 10), steps=kwargs.pop("steps", 0),
            policy_id="full", session=self.sessions, **kwargs))

    def test_the_goal_is_required(self):
        _, (outcome, charged) = self.run_loop({"goal": "  "})
        self.assertFalse(outcome["ok"])
        self.assertIn("目标", outcome["summary"])
        self.assertEqual(charged, 0)

    def test_a_report_saying_done_stops_it_after_one_round(self):
        self.conclusion_fn = lambda text: "结论：这件事已完成\n证据：x\n建议：无"
        _, (outcome, charged) = self.run_loop({"goal": "推完", "maxRounds": 4})
        self.assertEqual(outcome["data"]["stopReason"], "reportedDone")
        self.assertEqual(len(outcome["data"]["rounds"]), 1)

    def test_the_same_conclusion_twice_stops_it(self):
        """收敛了还继续跑就是烧钱 —— 而"无进展"是本平台唯一能可靠看出的信号。"""
        _, (outcome, _) = self.run_loop({"goal": "推完", "maxRounds": 4})
        self.assertEqual(outcome["data"]["stopReason"], "noProgress")
        self.assertEqual(len(outcome["data"]["rounds"]), 2)

    def test_different_conclusions_run_to_the_cap(self):
        """关键词判据会漏 —— 所以轮数上限才是兜底，这一条钉的就是那个兜底。"""
        self.conclusion_fn = lambda text: (
            "结论：第 %s 轮推进了一点\n证据：x\n建议：无"
            % ("1" if "第 1 /" in text else "2"))
        _, (outcome, _) = self.run_loop({"goal": "推完", "maxRounds": 2})
        self.assertEqual(outcome["data"]["stopReason"], "maxRounds")
        self.assertEqual(len(outcome["data"]["rounds"]), 2)
        summaries = [r["summary"] for r in outcome["data"]["rounds"]]
        self.assertNotEqual(summaries[0], summaries[1],
                            "两轮结论不同才不会触发「收敛」判定")

    def test_every_round_is_charged_to_the_parent_budget(self):
        """**循环不是预算加倍。** 写错的话结果看起来完全正常，只是这轮多花了几倍。"""
        _, (outcome, charged) = self.run_loop({"goal": "推完", "maxRounds": 4},
                                              max_steps=10)
        rounds = len(outcome["data"]["rounds"])
        self.assertEqual(charged, rounds, "每一轮一次工具调用，就该扣一步")
        self.assertEqual(outcome["data"]["budget"]["charged"], charged)
        self.assertLessEqual(charged, 10 - 1, "总扣数不能超过父轮留给它的额度")

    def test_a_budget_that_cannot_pay_for_another_round_stops(self):
        _, (outcome, charged) = self.run_loop({"goal": "推完", "maxRounds": 6},
                                              max_steps=3)
        self.assertEqual(outcome["data"]["stopReason"], "budget")
        self.assertEqual(len(outcome["data"]["rounds"]), 1)
        self.assertLessEqual(charged, 2)

    def test_rounds_are_clamped_not_refused(self):
        self.assertEqual(loop.rounds_of(None), loop.DEFAULT_ROUNDS)
        self.assertEqual(loop.rounds_of(0), 1)
        self.assertEqual(loop.rounds_of(-3), 1)
        self.assertEqual(loop.rounds_of(999), loop.MAX_ROUNDS)
        self.assertEqual(loop.rounds_of("3"), 3)

    def test_each_round_is_a_fresh_child_that_only_gets_the_conclusions(self):
        """循环**不共享上下文**：一轮的活只能靠报告传下去，这是它的边界。"""
        events, (outcome, _) = self.run_loop({"goal": "推完", "maxRounds": 3})
        starts = [e for e in events
                  if e.get("type") == "subagent" and e.get("phase") == "start"]
        # 默认结论两轮相同 → 第二轮就收敛停掉。所以这里是**两个各跑一轮的孩子**，
        # 而不是"一个孩子跑了 3 步"—— 这个区别是整条能力的形状。
        self.assertEqual([e["role"] for e in starts], ["循环#1", "循环#2"])
        self.assertEqual(outcome["data"]["stopReason"], "noProgress")
        bodies = [json.dumps(r["body"], ensure_ascii=False)
                  for r in self.model.requests]
        second = [b for b in bodies if "第 2 / 3 轮" in b]
        self.assertTrue(second, "第二轮必须带着「这是第几轮」的说明出发")
        self.assertIn("第 1 轮结论", second[0],
                      "前一轮的结论要传给下一轮 —— 那是唯一的传递通道")


# ------------------------------------------------------------------ 计划审批

class PlanReviewTestCase(PlaneCase):
    """strict 档下，派团队任务先要一张**计划单**，而单子上要有计划。"""

    def test_the_plan_card_carries_who_does_what_with_which_tools(self):
        self.set_mode("strict")
        row = self.build_team()
        team.add_task(self.plane, {"subject": "扫一遍", "assignee": "m1",
                                   "description": "对目标做连通性探测"},
                      self.sessions)
        outer = self

        def responder(record):
            body = record["body"]
            if body.get("tools") and not any(
                    m.get("role") == "tool" for m in (body.get("messages") or [])):
                return _reply_tool("team_run", {}, "call-1")
            return _reply_text("结论：已派活\n证据：x\n建议：无")

        model = FakeModel(responder)
        self.addCleanup(model.stop)
        decider = Decider(self.store, action="approve")
        decider.start()
        self.addCleanup(decider.stop_event.set)
        events, done, last = self.parent_turn(model)
        rows = [r for r in self.store.list_approvals(limit=10)
                if r["tool"] == "team_run"]
        self.assertEqual(len(rows), 1, "strict 档下派活要先要一张计划单")
        card = rows[0]
        self.assertEqual(card["status"], "approved")
        batch = card["facts"].get("batch") or []
        self.assertEqual([item["subject"] for item in batch], ["扫一遍"])
        self.assertEqual(batch[0]["assignee"], "m1",
                         "计划单上要说清**谁去** —— 没有它这张单子没法判断")
        self.assertTrue(batch[0]["role"])
        joined = "\n".join(card["reasons"])
        self.assertIn("m1", joined, "给人读的那几句里也要有这计划")
        self.assertIn("派活", joined, "标题要写清这是「派活」，不是某个具体动作")
        self.assertIn("派出子智能体", card["headline"])
        self.assertTrue(any(e.get("type") == "review" for e in events))
        self.assertIsNotNone(done)

    def test_在_ask_档_派活不要计划单(self):
        """`ask` 的语义是"审查任意命令"，不该顺手把派活也拦下来。"""
        self.set_mode("ask")
        team_row = self.build_team()
        team.add_task(self.plane, {"subject": "扫一遍", "assignee": "m1",
                                   "description": "x"}, self.sessions)
        model = FakeModel(lambda record: _reply_text("结论：x\n证据：y\n建议：无"))
        self.addCleanup(model.stop)
        self.parent_turn(model)          # 只跑一轮纯文本，不调工具
        self.assertEqual([r for r in self.store.list_approvals(limit=5)
                          if r["tool"] == "team_run"], [])

    def parent_turn(self, model, **kwargs):
        events, done, last = [], None, None
        for event in chat.run_turn_stream(
                self.plane, model.profile(), "k",
                [{"role": "user", "text": "把这批活派出去"}],
                policy_id="full", max_steps=kwargs.pop("max_steps", 6),
                stream=False, session=self.sessions, **kwargs):
            events.append(event)
            if event.get("type") == "done":
                done = event
            if event.get("type") == "persist":
                last = event["message"]
        return events, done, last


class ForbiddenToolTestCase(PlaneCase):
    """**闸门，不是清单。** 子智能体真的去调一个禁止的工具时必须被拦住。

    这一条对应的是这一轮补上的缺陷：`CHILD_FORBIDDEN` 此前只作用在**给模型看的
    那张表**上（`resolve_tools`），而 `chat.execute_tool` 并不知道"我是不是一个
    孩子"。于是一个被工具结果诱导、或者纯粹自己编出工具名的子智能体，调
    `team_add_task` 会**真的改掉队长的任务板**（那个 handler 就在 `HANDLERS` 里，
    权限档也放行）。`team_run` 只是碰巧被"本轮没有挂载运行时"挡住 —— 挡住的
    理由不是这里不让，而那正是"闸门分裂"的样子。
    """

    def setUp(self):
        super().setUp()
        self.set_mode("off")
        outer = self

        def responder(record):
            body = record["body"]
            if not body.get("tools") or any(
                    m.get("role") == "tool" for m in (body.get("messages") or [])):
                return _reply_text("结论：我试了\n证据：被拒\n建议：无")
            return _reply_tool("team_add_task",
                               {"subject": "我加一张", "assignee": ""}, "call-1")

        self.model = FakeModel(responder)
        self.addCleanup(self.model.stop)

    def test_a_child_cannot_write_to_the_task_board(self):
        row = self.build_team()
        gate = chat._child_gate(self.plane, policy_id="full", role="",
                                auth_required=True, fallback_eid="",
                                session=self.sessions, review_mode="off")
        events, (outcome, _) = drive(subagent.run_delegation(
            self.plane,
            {"children": [{"role": "kid", "objective": "往任务板上加一张任务"}]},
            profile=self.model.profile(), api_key="k", max_steps=6, steps=0,
            policy_id="full", session=self.sessions, gate=gate))
        self.assertEqual(self.store.list_team_tasks(row["id"]), [],
                         "子智能体**不能**改掉队长的任务板")
        refused = [e for e in events
                   if e.get("type") == "subagent" and e.get("phase") == "refused"]
        self.assertEqual([e["name"] for e in refused], ["team_add_task"],
                         "拒绝要有自己的一条事件，不能只体现成工具结果里的一行字")
        child = outcome["data"]["children"][0]
        self.assertFalse(child["toolCalls"][0]["ok"])
        self.assertIn("子智能体不能调用它", child["toolCalls"][0]["brief"])
        self.assertEqual(self.store.list_approvals(limit=5), [],
                         "被禁止的工具不该先弹一张审查单再拒 —— 那不是审查，是噪音")


if __name__ == "__main__":
    unittest.main()
