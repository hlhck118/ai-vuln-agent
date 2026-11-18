# -*- coding: utf-8 -*-
"""`pentestd/subagent.py` 的用例。

重点不在"派活能跑通"，而在**五条不变量各自有没有一道真的守得住的门**：

1. 只收窄不放宽 —— 子智能体拿到的工具必须来自父档位；
2. 不递归 —— 子智能体永远拿不到 `delegate`；
3. 计入父轮预算 —— 派活之后父轮的步数计数器必须变大；
4. 每个子智能体单独过闸 —— 子智能体的工具调用走的是同一个 `execute_tool`；
5. 失败不吞 —— 一个子智能体挂了，原因必须出现在父轮的结果里。

这些性质里最容易被"顺手优化掉"的是第 1 条与第 3 条：它们不会让任何东西报错，
只会让权限档悄悄失效、让预算悄悄翻倍。所以每一条都配了一个**变异就能红的**
断言（见文件末尾的 `InvariantGuardTestCase`）。
"""
import json
import os
import re
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import chat, policy, server, subagent                      # noqa: E402
from tests import support                                                # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")

ROLE_RE = re.compile(r"## 你的角色\s*\n\s*(\S+)")
CONC_V1 = "/v1"


# ---------------------------------------------------------------- 脚本化模型

def text_reply(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


def tool_reply(name: str, arguments: dict, call_id: str = "call-1") -> dict:
    return {"choices": [{"message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name,
                                     "arguments": json.dumps(arguments,
                                                             ensure_ascii=False)}}]},
        "finish_reason": "tool_calls"}]}


def role_of(record) -> str:
    """从请求体里认出这是**哪个角色**的系统提示。

    子智能体的角色写在系统提示的 `## 你的角色` 下面（`subagent.CHILD_PROMPT`）。
    父轮的系统提示里没有这一段，所以返回空串——测试据此区分"这次请求是父轮还是
    某个子智能体"，而不需要两个模型端点。
    """
    for message in record["body"].get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(block.get("text", "") for block in content
                               if isinstance(block, dict))
        if not isinstance(content, str):
            continue
        found = ROLE_RE.search(content)
        if found:
            return found.group(1)
    return ""


class ScriptedModel:
    """脚本化模型端点：`responder(record) -> dict`。

    与 `test_chat.py` 的那份**故意各写一份**：测试之间互相 import 会让改名变成
    一件牵动两处的事，而这里只需要四十行。
    """

    def __init__(self, responder):
        self.responder = responder
        self.requests: list = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
                record = {"path": self.path, "body": body}
                outer.requests.append(record)
                payload = json.dumps(outer.responder(record) or {}) \
                    .encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d" % (host, port)

    @property
    def profile(self) -> dict:
        return {"provider": "openai_compat", "baseUrl": self.base_url + CONC_V1,
                "model": "fake-1", "temperature": 0.0, "maxTokens": 512,
                "timeoutS": 20}

    def tools_offered_to(self, role: str) -> set:
        """这个角色的**每一次**请求里，工具表出现过的名字并集。

        `_run_child` 是按角色把工具表交给模型的，所以这里能看到"模型实际被允许
        看见什么"——比断言 `resolve_tools` 的返回值更接近真实（它绕过了整条装配
        路径），也更难被"改了别处"骗过去。
        """
        names = set()
        for record in self.requests:
            if role_of(record) != role:
                continue
            for tool in record["body"].get("tools") or []:
                function = tool.get("function") or {}
                if function.get("name"):
                    names.add(function["name"])
        return names

    def prompts_for(self, role: str) -> list:
        out = []
        for record in self.requests:
            if role_of(record) != role:
                continue
            for message in record["body"].get("messages") or []:
                content = message.get("content")
                if isinstance(content, str) and "## 你的角色" in content:
                    out.append(content)
        return out

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def drain(generator):
    """抽干一个"yield 事件、return 结果"的生成器，返回 `(事件, 结果)`。"""
    events = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stop:
            return events, stop.value


class Base(unittest.TestCase):
    def setUp(self):
        self.dir, self.store = support.temporary_store("subagent.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)
        self.models = []

    def tearDown(self):
        for model in self.models:
            model.stop()
        self.store.close()
        self.dir.cleanup()

    def scripted(self, responder) -> ScriptedModel:
        model = ScriptedModel(responder)
        self.models.append(model)
        return model


# ---------------------------------------------------------------- 参数契约

class SpecContractTestCase(unittest.TestCase):
    """结构错误整次失败；工具名问题降级记录。这两类的分界要钉住。"""

    def test_children_is_required(self):
        for bad in ({}, {"children": []}, {"children": "recon"},
                    {"children": None}):
            with self.assertRaises(subagent.SpecError, msg=repr(bad)):
                subagent.parse_specs(bad)

    def test_role_must_be_unique_and_the_reason_is_about_attribution(self):
        with self.assertRaises(subagent.SpecError) as caught:
            subagent.parse_specs({"children": [
                {"role": "recon", "objective": "a"},
                {"role": "recon", "objective": "b"}]})
        # 失败原因必须说清是"分不清谁报的"，而不是"不许重复"这种没有理由的规则
        self.assertIn("分不清", str(caught.exception))

    def test_objective_is_required_and_the_reason_explains_isolation(self):
        with self.assertRaises(subagent.SpecError) as caught:
            subagent.parse_specs({"children": [{"role": "recon"}]})
        self.assertIn("看不到", str(caught.exception))

    def test_too_many_children_is_refused_with_the_limit(self):
        with self.assertRaises(subagent.SpecError) as caught:
            subagent.parse_specs({"children": [
                {"role": "r%d" % i, "objective": "x"}
                for i in range(subagent.MAX_CHILDREN + 1)]})
        self.assertIn(str(subagent.MAX_CHILDREN), str(caught.exception))

    def test_lengths_are_capped(self):
        with self.assertRaises(subagent.SpecError):
            subagent.parse_specs({"children": [
                {"role": "r", "objective": "x" * (subagent.OBJECTIVE_CHARS + 1)}]})
        with self.assertRaises(subagent.SpecError):
            subagent.parse_specs({"children": [
                {"role": "r" * (subagent.ROLE_CHARS + 1), "objective": "x"}]})
        with self.assertRaises(subagent.SpecError):
            subagent.parse_specs({"children": [
                {"role": "r", "objective": "x", "tools": "start_probe"}]})

    def test_valid_specs_round_trip(self):
        specs = subagent.parse_specs({"children": [
            {"role": " recon ", "objective": "看 10.0.0.0/24",
             "tools": ["start_probe", "start_probe", " "]},
            {"role": "web", "objective": "查 80 端口"}]})
        self.assertEqual([s.role for s in specs], ["recon", "web"])
        # 工具名去重且丢掉空项，顺序按声明
        self.assertEqual(specs[0].wanted, ["start_probe"])
        self.assertEqual(specs[1].wanted, [])


class BudgetTestCase(unittest.TestCase):
    """不变量 3 的底座：并发扣同一个计数器，一次都不能超。"""

    def test_budget_never_overshoots_under_concurrency(self):
        budget = subagent.Budget(10)
        granted = []
        lock = threading.Lock()

        def worker():
            for _ in range(50):
                if budget.take():
                    with lock:
                        granted.append(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(budget.used, 10)
        self.assertEqual(len(granted), 10)
        self.assertEqual(budget.left, 0)

    def test_zero_budget_grants_nothing(self):
        budget = subagent.Budget(0)
        self.assertFalse(budget.take())
        self.assertEqual(budget.used, 0)

    def test_public_shape(self):
        budget = subagent.Budget(3)
        budget.take()
        self.assertEqual(budget.public(), {"limit": 3, "charged": 1, "left": 2,
                                           "perChild": 3})

    # ---------------------------------------------------------- 保底份额
    #
    # 下面三条钉的是同一个缺陷：**池子被并发线程先到先得地抢**。跑得最快那个
    # 吃光，其余的子智能体一步都跑不成——而它们各已经烧掉一次模型调用，还报成
    # "在第 1 步停下"。抢输的是谁取决于线程调度，所以当时连复现都要靠错开时序。

    def test_a_greedy_child_cannot_eat_a_siblings_floor(self):
        """1 号抢得再凶，也只能动自己那一份。"""
        budget = subagent.Budget(3, shares=3)
        self.assertEqual(budget.per_child, 1)
        self.assertTrue(budget.take(1))
        self.assertFalse(budget.take(1), "1 号把兄弟的保底也吃掉了")
        # 2 号、3 号各还留着一次，谁也抢不走
        self.assertTrue(budget.take(2))
        self.assertTrue(budget.take(3))
        self.assertEqual(budget.used, 3)
        self.assertFalse(budget.take(1))
        self.assertFalse(budget.take(4), "第 4 个槽位不该凭空多出额度")

    def test_the_spare_is_shared_once_everyone_has_their_floor(self):
        """除不尽的余数做公共池——保底之外谁先到谁用，总额仍然封顶。"""
        budget = subagent.Budget(7, shares=3)          # 2 保底 + 1 公共
        self.assertEqual(budget.per_child, 2)
        for slot in (1, 2, 3):
            self.assertTrue(budget.take(slot))
            self.assertTrue(budget.take(slot))
        # 三份保底都吃满了，只剩那 1 次公共额度
        self.assertTrue(budget.take(1), "公共额度没被拿出来用")
        self.assertFalse(budget.take(2))
        self.assertEqual(budget.used, 7)

    def test_the_split_hands_out_exactly_the_limit(self):
        """保底 × 人数 + 余数 必须**正好**是 `limit`：少一个单位是白白浪费，
        多一个就是破了不变量 3。

        这条同时说明**硬顶那句为什么杀不死**（`take()` 里的
        `if self._used >= self.limit`）：恒等式成立时，保底与余数已经把总额封死，
        把那句改成 `if False:` 不会有任何用例变红（我试过，M4 = 0 条）。硬顶是
        备胎；这条钉的是它所依赖的那个恒等式，恒等式一破它就成了唯一的一道。
        """
        for limit, shares in ((3, 3), (7, 3), (6, 6), (10, 4), (2, 5), (1, 6),
                              (9, 7), (0, 4)):
            budget = subagent.Budget(limit, shares=shares)
            taken = 0
            for _ in range(50):
                for slot in range(1, shares + 1):
                    if budget.take(slot):
                        taken += 1
            self.assertEqual(taken, limit, (limit, shares))

    def test_splitting_never_raises_the_total_above_the_limit(self):
        """分份额不能把总额放大——不变量 3 在切分之后必须原样成立。

        **下标里故意混进不存在的槽位**（0 / shares+1 / 99）。这是我第一版实现
        真的漏掉的一处：保底是无条件按下标发的，于是 3 人份的池子上 `take(4)`
        能扣出第 4 次，总额破了 `limit`。生产里下标只可能是 1..shares，但
        "只有在调用方守规矩时才成立"的上限不算上限。
        """
        stray = (0, None, 7, 99)
        for limit, shares in ((3, 3), (7, 3), (6, 6), (10, 4), (2, 5), (1, 6)):
            budget = subagent.Budget(limit, shares=shares)
            taken = 0
            for _ in range(50):
                for slot in tuple(range(1, shares + 1)) + stray:
                    if budget.take(slot):
                        taken += 1
            self.assertLessEqual(taken, limit, (limit, shares))
            self.assertEqual(budget.used, taken, (limit, shares))

    def test_shares_of_one_is_the_old_behaviour(self):
        """不带份额时整池归一个人——老调用点（`take()` 无参）语义没有变。"""
        budget = subagent.Budget(2)
        self.assertEqual(budget.per_child, 2)
        self.assertTrue(budget.take())
        self.assertTrue(budget.take())
        self.assertFalse(budget.take())

    # ---------------------------------------------------------- 共享模式
    #
    # `floor=` 是平台按目标派活用的：每人只留保底，其余进公共池。它**不改变
    # 默认路径**——不传 `floor` 时，上面那些用例的行为逐字节不变。

    def test_a_floor_shares_whatever_is_left_in_one_pool(self):
        budget = subagent.Budget(10, shares=2, floor=2)
        self.assertEqual(budget.per_child, 2, "共享模式下 perChild 应该是保底")
        # 1 号用到 8 次（自己的 2 次 + 公共池里的 6 次），早已超过均分的 5 次。
        for _ in range(8):
            self.assertTrue(budget.take(1), "公共池没给 1 号用")
        # 2 号的保底一刻都没被动过。
        self.assertTrue(budget.take(2))
        self.assertTrue(budget.take(2))
        self.assertFalse(budget.take(2))
        self.assertEqual(budget.used, 10)

    def test_a_floor_still_protects_a_sibling_when_the_pool_is_tight(self):
        """池子连把保底都发不出来（`limit < floor × shares`）时，公共池是 0。"""
        budget = subagent.Budget(3, shares=2, floor=2)
        self.assertEqual(budget.per_child, 2)
        self.assertTrue(budget.take(1))
        self.assertTrue(budget.take(1))
        # 没有公共池：1 号吃完自己那两份就停，剩下那 1 次留给 2 号。
        self.assertFalse(budget.take(1), "池子不够发保底时却还能接着扣")
        self.assertTrue(budget.take(2))
        self.assertFalse(budget.take(2))
        self.assertEqual(budget.used, 3)

    def test_without_a_floor_the_split_is_still_equal(self):
        """默认路径：`delegate` 与 `spawn_teammate` 仍然是均分，不是共享。"""
        budget = subagent.Budget(9, shares=2)          # 4 保底 + 1 公共
        self.assertEqual(budget.per_child, 4)
        for _ in range(4):
            self.assertTrue(budget.take(1))
        self.assertTrue(budget.take(1), "余数做的那 1 次公共额度没被拿出来用")
        self.assertFalse(budget.take(1))
        for _ in range(4):
            self.assertTrue(budget.take(2))
        self.assertFalse(budget.take(2))
        self.assertEqual(budget.used, 9)


# ---------------------------------------------------------------- 不变量 1/2

class NarrowingTestCase(unittest.TestCase):
    """**最重要的一个类。** 收窄失效不会让任何东西报错，只会让权限档变哑。"""

    def spec(self, **kwargs):
        base = {"role": "recon", "objective": "x"}
        base.update(kwargs)
        return subagent.Spec(**base)
    def test_delegate_is_never_reachable_from_a_child(self):
        self.assertIn(subagent.DELEGATE_TOOL, subagent.CHILD_FORBIDDEN)
        for level in ("readonly", "probe", "audit", "full"):
            names, _ = subagent.resolve_tools(self.spec(), level, "", True)
            self.assertNotIn(subagent.DELEGATE_TOOL, names,
                             "不变量 2 被破坏：%s 档的子智能体拿到了派活工具" % level)

    def test_child_tool_set_is_a_subset_of_the_parent_level(self):
        for level in ("readonly", "probe", "audit", "full"):
            parent = set(policy.allowed_tools(level)) - subagent.CHILD_FORBIDDEN
            names, _ = subagent.resolve_tools(self.spec(), level, "", True)
            self.assertLessEqual(set(names), parent,
                                 "不变量 1 被破坏：%s 档的子智能体超出了父档位" % level)

    def test_a_probe_parent_cannot_hand_out_a_full_only_tool(self):
        names, dropped = subagent.resolve_tools(
            self.spec(wanted=["start_exploit", "start_probe"]), "probe", "", True)
        self.assertEqual(names, ["start_probe"])
        self.assertEqual([d["name"] for d in dropped], ["start_exploit"])
        # 降级原因要能直接转述给用户：不是"不行"，是"档位不够"
        self.assertIn("权限档", dropped[0]["reason"])

    def test_deployment_level_tools_are_refused_even_at_full(self):
        names, dropped = subagent.resolve_tools(
            self.spec(wanted=["install_plugin", "clear_finished_jobs",
                              "get_findings"]), "full", "admin", True)
        self.assertEqual(names, ["get_findings"])
        self.assertEqual({d["name"] for d in dropped},
                         {"install_plugin", "clear_finished_jobs"})

    def test_unknown_tool_names_are_recorded_not_silently_ignored(self):
        names, dropped = subagent.resolve_tools(
            self.spec(wanted=["run_shell"]), "full", "", True)
        self.assertEqual(names, [])
        self.assertEqual(dropped[0]["reason"], "不存在这个工具")

    def test_empty_wishlist_means_use_the_whole_allowed_set(self):
        names, dropped = subagent.resolve_tools(self.spec(), "probe", "", True)
        self.assertEqual(dropped, [])
        self.assertIn("start_probe", names)
        self.assertNotIn("start_scan", names)          # audit 档才有
        self.assertNotIn("install_plugin", names)


class ModelVisibilityTestCase(Base):
    """上一条测的是 `resolve_tools` 的返回值；这一条测**模型实际看见了什么**。

    两者不是一回事：`resolve_tools` 对了但装配工具表时用了全集，权限照样是哑的。
    所以这里从真实请求体里读 `tools` 字段。
    """

    def test_model_never_sees_delegate_or_deployment_tools(self):
        def responder(record):
            if role_of(record) == "recon":
                return text_reply("结论：看完了\n证据：7 台存活\n建议：无")
            return text_reply("done")

        model = self.scripted(responder)
        specs = subagent.parse_specs({"children": [
            {"role": "recon", "objective": "看 10.0.0.0/24 的存活主机"}]})
        _, aggregate = drain(subagent.spawn(
            specs, plane=self.plane, profile=model.profile, api_key="k",
            budget=subagent.Budget(6), policy_id="probe"))
        offered = model.tools_offered_to("recon")
        self.assertTrue(offered, "子智能体一次工具都没拿到，这条断言就没意义了")
        self.assertNotIn(subagent.DELEGATE_TOOL, offered)
        self.assertNotIn("install_plugin", offered)
        self.assertNotIn("clear_finished_jobs", offered)
        self.assertNotIn("start_exploit", offered)     # full 档才有
        self.assertIn("start_probe", offered)          # probe 档应当有
        self.assertEqual(aggregate["children"][0]["policy"], "probe")

    def test_the_child_prompt_says_it_cannot_delegate(self):
        def responder(record):
            if role_of(record) == "recon":
                return text_reply("结论：无\n证据：无\n建议：无")
            return text_reply("done")

        model = self.scripted(responder)
        specs = subagent.parse_specs({"children": [
            {"role": "recon", "objective": "看存活"}]})
        drain(subagent.spawn(specs, plane=self.plane, profile=model.profile,
                             api_key="k", budget=subagent.Budget(6),
                             policy_id="probe"))
        prompt = model.prompts_for("recon")[0]
        # 提示词必须**明确**告诉它不能派活、也看不到兄弟——否则它会按 DSH 的心智
        # 模型去等一个异步回报。
        self.assertIn("不能", prompt)
        self.assertIn("子智能体", prompt)
        self.assertIn("结论：", prompt)


# ---------------------------------------------------------------- 报告解析

class ReportTestCase(unittest.TestCase):
    def test_structured_three_lines(self):
        summary, evidence, fmt = subagent.parse_report(
            "结论：10.0.0.0/24 有 7 台存活\n证据：10.0.0.1:80 open；10.0.0.2:22 open\n建议：对 10.0.0.2 做指纹")
        self.assertEqual(summary, "10.0.0.0/24 有 7 台存活")
        self.assertIn("10.0.0.1:80", evidence)
        self.assertEqual(fmt, "structured")

    def test_multi_line_evidence_is_kept_together(self):
        _, evidence, fmt = subagent.parse_report(
            "结论：查完了\n证据：第一行\n第二行\n建议：无")
        self.assertEqual(fmt, "structured")
        self.assertIn("第一行", evidence)
        self.assertIn("第二行", evidence)

    def test_free_format_loses_nothing(self):
        """没按格式写时**绝不能丢掉它的工作**。"""
        raw = "我扫了 10.0.0.0/24，发现 7 台主机，其中 10.0.0.1 开了 80 和 443。"
        summary, evidence, fmt = subagent.parse_report(raw)
        self.assertEqual(fmt, "free")
        self.assertTrue(summary)
        self.assertIn("7 台主机", evidence)
        self.assertIn("10.0.0.1", evidence)

    def test_empty_text_is_reported_as_empty_not_as_success(self):
        summary, evidence, fmt = subagent.parse_report("   ")
        self.assertEqual(fmt, "free")
        self.assertIn("没有产出", evidence)
        self.assertTrue(summary)


# ---------------------------------------------------------------- 派活端到端

class DelegationTestCase(Base):
    """真实 HTTP 模型 + 真实控制面。子智能体的工具调用**真的建任务**。"""

    def run_delegate(self, children, responder, policy_id="probe", budget=6):
        model = self.scripted(responder)
        specs = subagent.parse_specs({"children": children})
        events, aggregate = drain(subagent.spawn(
            specs, plane=self.plane, profile=model.profile, api_key="k",
            budget=subagent.Budget(budget), policy_id=policy_id))
        return model, events, aggregate

    def test_children_return_reports_and_failures_are_kept(self):
        def responder(record):
            role = role_of(record)
            if role == "recon":
                return text_reply("结论：7 台存活\n证据：10.0.0.1:80 open\n建议：无")
            if role == "web":
                return text_reply("结论：80 端口是 nginx\n证据：Server: nginx/1.24\n建议：无")
            # creds 这个角色的模型直接失败——它必须**带着原因**回到汇总里
            return {"error": {"message": "模型端点 500"}}

        model, events, aggregate = self.run_delegate(
            [{"role": "recon", "objective": "看存活"},
             {"role": "web", "objective": "看 80"},
             {"role": "creds", "objective": "看口令"}], responder)
        self.assertEqual(aggregate["spawned"], 3)
        self.assertEqual(aggregate["succeeded"], 2)
        self.assertEqual(aggregate["failed"], 1)
        by_role = {c["role"]: c for c in aggregate["children"]}
        self.assertIn("7 台存活", by_role["recon"]["summary"])
        self.assertIn("nginx", by_role["web"]["evidence"])
        self.assertFalse(by_role["creds"]["ok"])
        self.assertTrue(by_role["creds"]["error"], "失败原因被吞掉了")
        # 一行摘要里，成功的结论与失败的原因都要在
        self.assertIn("recon", aggregate["summary"])
        self.assertIn("creds", aggregate["summary"])
        self.assertIn("失败", aggregate["summary"])

    def test_child_tool_call_really_creates_a_job(self):
        """子智能体的工具调用走的是**同一个** `execute_tool`，副作用是真的。"""
        state = {}

        def responder(record):
            role = role_of(record)
            if role == "recon":
                if not state.get("called"):
                    state["called"] = True
                    return tool_reply("start_probe",
                                      {"target": "10.10.10.250",
                                       "objective": "看存活"}, "call-1")
                return text_reply("结论：已下发\n证据：10.10.10.250\n建议：无")
            return text_reply("结论：无\n证据：无\n建议：无")

        model, events, aggregate = self.run_delegate(
            [{"role": "recon", "objective": "探测 10.10.10.250"}], responder)
        jobs = self.store.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["target"], "10.10.10.250")
        self.assertEqual(jobs[0]["kind"], "probe")
        child = aggregate["children"][0]
        self.assertEqual(child["steps"], 1)
        self.assertEqual(child["toolCalls"][0]["name"], "start_probe")
        self.assertTrue(child["toolCalls"][0]["ok"])
        # 这一格是"这条任务是谁派的"的唯一凭据（界面靠它把任务与子智能体连起来）。
        # 落库那份与流里那一帧都要带，缺一个的症状不同：落库缺 → 刷新之后连线消失，
        # 流里缺 → 刚下发那一刻点不动，而两种都"看起来正常"。
        self.assertEqual(child["toolCalls"][0]["jobId"], jobs[0]["id"])
        tool_frames = [event for event in events
                       if event.get("type") == "subagent" and event.get("phase") == "tool"]
        self.assertEqual([frame.get("jobId") for frame in tool_frames], [jobs[0]["id"]],
                         "流里的工具帧没带 jobId，实时那一刻就连不起来")

    def test_events_cover_every_phase_in_order(self):
        def responder(record):
            if role_of(record) == "recon":
                return text_reply("结论：无\n证据：无\n建议：无")
            return text_reply("done")

        _, events, _ = self.run_delegate(
            [{"role": "recon", "objective": "x"}], responder)
        phases = [event["phase"] for event in events
                  if event["type"] == "subagent"]
        self.assertEqual(phases, ["start", "step", "done"])
        for event in events:
            self.assertEqual(event["total"], 1)
            self.assertEqual(event["index"], 1)
            self.assertEqual(event["role"], "recon")

    def test_children_run_concurrently_not_one_by_one(self):
        """不并发的话，"派活"就只是一次更贵的顺序执行——那它没有存在价值。

        **判据是区间重叠，不是墙钟。** 这里原先量的是总耗时（`elapsed < 0.40 × 3 × 0.85`
        = 1.02s）。单跑一直绿，放进全量发现里却红过一次：2 150 秒的套件把机器拖热之后，
        同一份并发逻辑量出 1.30s，而 `aggregate["concurrency"]` 仍是 3 —— 一个"看起来
        像顺序执行"的假警报。墙钟上限越紧越容易假红，而"顺序执行"这个结论本来就不需要
        靠总耗时去猜：并发就是**几个子智能体同时在跑**，所以直接记下每个子智能体进出
        模型的时刻，看三个窗口有没有同时张开。
        """
        delay = 0.40
        windows = {}
        lock = threading.Lock()

        def responder(record):
            role = role_of(record)
            if not role:
                return text_reply("done")
            began = time.time()
            try:
                time.sleep(delay)
                return text_reply("结论：无\n证据：无\n建议：无")
            finally:
                with lock:
                    windows[role] = (began, time.time())

        _, _, aggregate = self.run_delegate(
            [{"role": "r%d" % i, "objective": "x"} for i in range(3)], responder)
        self.assertEqual(aggregate["succeeded"], 3)
        self.assertEqual(aggregate["concurrency"], 3)
        self.assertEqual(sorted(windows), ["r0", "r1", "r2"],
                         "三个子智能体没有都真的走到模型")
        spans = [windows["r%d" % i] for i in range(3)]
        latest_start = max(span[0] for span in spans)
        earliest_end = min(span[1] for span in spans)
        self.assertLess(latest_start, earliest_end,
                        "三个子智能体没有同时在跑（各自的窗口 %s）：看起来是顺序执行的"
                        % ", ".join("%.2f..%.2f" % span for span in spans))

    def test_every_child_has_its_own_prompt_and_role(self):
        def responder(record):
            if role_of(record):
                return text_reply("结论：无\n证据：无\n建议：无")
            return text_reply("done")

        model, _, _ = self.run_delegate(
            [{"role": "recon", "objective": "侦察网段 A"},
             {"role": "web", "objective": "查 Web 服务"}],
            responder)
        recon = model.prompts_for("recon")[0]
        web = model.prompts_for("web")[0]
        self.assertIn("侦察网段 A", recon)
        self.assertNotIn("查 Web 服务", recon)
        self.assertIn("查 Web 服务", web)
        self.assertNotIn("侦察网段 A", web)


# ---------------------------------------------------------------- 不变量 3

class BudgetChargingTestCase(Base):
    """派活不是预算加倍。这是最容易被"顺手优化掉"的一条。"""

    def test_child_tool_calls_charge_the_parent_step_counter(self):
        state = {}

        def responder(record):
            role = role_of(record)
            if not role:
                # 父轮：先派活，之后再给结论（收尾调用不带工具）
                if not state.get("delegated"):
                    state["delegated"] = True
                    return tool_reply("delegate", {"children": [
                        {"role": "recon", "objective": "探测并核对"}]}, "call-p")
                return text_reply("已派活并汇总。")
            done = state.get("child_tools", 0)
            if done == 0:
                state["child_tools"] = 1
                return tool_reply("list_executors", {}, "call-c1")
            if done == 1:
                state["child_tools"] = 2
                return tool_reply("list_executors", {}, "call-c2")
            return text_reply("结论：节点 1 个\n证据：exec-1\n建议：无")

        model = self.scripted(responder)
        result = chat.run_turn(self.plane, model.profile, "k",
                               [{"role": "user", "text": "派几个人去看看"}],
                               policy_id="probe", max_steps=3)
        self.assertTrue(result["ok"], result.get("message"))
        # 父轮预算 3：delegate 自身 1 步 + 子智能体的 2 次工具调用 = 3
        self.assertEqual(result["steps"], 3,
                         "子智能体的工具调用没有计入父轮预算（不变量 3）")
        # 预算正好用尽 → 父轮走收尾路径，而不是"还能再派一轮"
        self.assertTrue(result.get("truncated"))
        delegate_entries = [entry for entry in result["trace"]
                            if entry["tool"] == subagent.DELEGATE_TOOL]
        self.assertEqual(len(delegate_entries), 1)
        # 轨迹里的 `data` 是**序列化后的字符串**（`_trace_entry` 对它做过截断），
        # 所以要显式解析——直接按字典取键会得到 "string indices must be integers"。
        payload = json.loads(delegate_entries[0]["data"])
        self.assertEqual(payload["budget"]["charged"], 2)

    def test_exhausted_budget_refuses_to_spawn_at_all(self):
        """预算已尽时**连模型都不该问**——派不出去就直说，别烧一次调用。"""
        model = self.scripted(lambda record: text_reply("不该被调用"))
        specs = subagent.parse_specs({"children": [
            {"role": "recon", "objective": "x"}]})
        events, outcome = drain(subagent.run_delegation(
            self.plane, {"children": [{"role": "recon", "objective": "x"}]},
            profile=model.profile, api_key="k", max_steps=2, steps=2,
            policy_id="probe"))
        self.assertEqual(events, [])
        self.assertFalse(outcome[0]["ok"])
        self.assertIn("预算", outcome[0]["summary"])
        self.assertEqual(outcome[1], 0)
        self.assertEqual(model.requests, [], "预算已尽却还是发了模型请求")

    def test_returned_charged_matches_what_was_actually_taken(self):
        state = {}

        def responder(record):
            role = role_of(record)
            if not role:
                return text_reply("done")
            if not state.get("used"):
                state["used"] = True
                return tool_reply("list_executors", {}, "c1")
            return text_reply("结论：无\n证据：无\n建议：无")

        model = self.scripted(responder)
        events, outcome = drain(subagent.run_delegation(
            self.plane, {"children": [{"role": "recon", "objective": "x"}]},
            profile=model.profile, api_key="k", max_steps=10, steps=1,
            policy_id="probe"))
        result, charged = outcome
        self.assertTrue(result["ok"], result["summary"])
        self.assertEqual(charged, 1)
        self.assertEqual(result["data"]["budget"]["charged"], charged)

    # ------------------------------------------------- 扇出量必须付得起
    #
    # 池子按人数摊开（`Budget(limit, shares=len(specs))`），摊不开就整次拒掉。
    # 放它跑起来的代价是：有几个子智能体被派了出去，一步都没跑成，却各已经
    # 烧掉一次模型调用——而"谁被牺牲"取决于线程调度。

    def test_fanout_bigger_than_the_pool_is_refused_without_asking_the_model(self):
        model = self.scripted(lambda record: text_reply("不该被调用"))
        args = {"children": [{"role": role, "objective": "x"}
                             for role in ("a", "b", "c", "d", "e", "f")]}
        events, outcome = drain(subagent.run_delegation(
            self.plane, args, profile=model.profile, api_key="k",
            max_steps=5, steps=1, policy_id="probe"))
        self.assertEqual(events, [], "拒了却还是发了子智能体事件")
        self.assertEqual(outcome[1], 0, "拒了却还是记了预算")
        self.assertEqual(model.requests, [], "拒了却还是发了模型请求")
        self.assertFalse(outcome[0]["ok"])
        self.assertIsNone(outcome[0]["data"])
        # 报错必须给出**能改的数**：只说"摊不开"的话，模型只能照原样再试一次。
        # 3 步的池子、每人至少 2 步 → 最多 1 个（边界随 `CHILD_MIN_TOOL_CHARGES`
        # 一起变：1 步只够把任务发出去，取不回结果，而"发出去却取不回"正是要防的）。
        self.assertIn("最多派 1 个", outcome[0]["summary"])

    def test_fanout_equal_to_the_pool_is_allowed(self):
        """边界：6 步派 3 个是**可以**的（每人恰好两步）。拒的只有摊不开。

        每人两步而不是一步，理由在 `CHILD_MIN_TOOL_CHARGES`：一步的下发完就
        没额度了，它连"任务跑完了吗、结果是什么"都问不了。实测六次派活里五次
        空手而归，形状就是这个。
        """
        def responder(record):
            role = role_of(record)
            if not role:
                return text_reply("done")
            done = sum(1 for m in record["body"].get("messages") or []
                       if m.get("role") == "tool")
            if done:
                return text_reply("结论：%s 看完了\n证据：无\n建议：无" % role)
            return tool_reply("list_executors", {}, "c-" + role)

        model = self.scripted(responder)
        args = {"children": [{"role": role, "objective": "x"}
                             for role in ("a", "b", "c")]}
        _, (result, charged) = drain(subagent.run_delegation(
            self.plane, args, profile=model.profile, api_key="k",
            max_steps=8, steps=1, policy_id="probe"))
        self.assertTrue(result["ok"], result["summary"])
        self.assertEqual(charged, 3)
        self.assertEqual([c["steps"] for c in result["data"]["children"]],
                         [1, 1, 1])

    def test_a_greedy_child_cannot_starve_its_siblings(self):
        """**这一条就是"父轮的步数预算已用尽……子智能体在第 1 步停下"那个缺陷。**

        侦察那个一路猛跑，另外两个**等它先跑**——把"谁先到"钉死。不钉的话这条
        用例的成败取决于线程调度，也就没法当回归用：当时正是靠错开时序才复现
        出来的。断言的是**每一个被派出去的人至少跑成一步**：老实现里跑得最快的
        那个会把 3 次全占了，另外两个各 0 步，报告里那句就是上面那句。
        """
        recon_finished = threading.Event()

        def responder(record):
            role = role_of(record)
            if not role:
                return text_reply("done")
            done = sum(1 for m in record["body"].get("messages") or []
                       if m.get("role") == "tool")
            if role == "recon":
                if done < 3:
                    return tool_reply("list_executors", {}, "g%d" % done)
                recon_finished.set()
                return text_reply("结论：recon 看完了\n证据：无\n建议：无")
            if not done:
                recon_finished.wait(1.0)          # 让 recon 先把池子抢一遍
                return tool_reply("list_executors", {}, "s-" + role)
            return text_reply("结论：%s 看完了\n证据：无\n建议：无" % role)

        model = self.scripted(responder)
        args = {"children": [{"role": role, "objective": "x"}
                             for role in ("recon", "web", "creds")]}
        _, (result, charged) = drain(subagent.run_delegation(
            self.plane, args, profile=model.profile, api_key="k",
            max_steps=8, steps=1, policy_id="probe"))
        steps = {c["role"]: c["steps"] for c in result["data"]["children"]}
        # 要断的是"**每一个人都跑成了**"，不是"每人恰好一步"。写成等值会把
        # "recon 想连跑 3 次"这种正常行为判成失败——而它正是保底要允许的事。
        self.assertTrue(all(value >= 1 for value in steps.values()),
                        "保底没有兑现，有人一步都没跑成：%r" % steps)
        self.assertTrue(all(value <= 2 for value in steps.values()),
                        "有人超出了自己那份保底：%r" % steps)
        self.assertLessEqual(charged, 4)


class ModelCallCeilingTestCase(Base):
    """模型调用的天花板可以**抬高**，但不会被压到 `CHILD_MODEL_CALLS` 以下。

    平台按目标派活把预算放宽到 60，如果孩子仍然 8 次模型调用就收尾，那池子再
    大也花不出去（一次模型调用平均只带出一次工具调用）。`fanout.run` 因此传
    `max_model_calls = max(CHILD_MODEL_CALLS, limit // 人数)`。
    """

    def _run(self, **kwargs):
        state = {"n": 0}

        def responder(record):
            if not role_of(record):
                return text_reply("不该有父轮请求")
            state["n"] += 1
            return tool_reply("list_executors", {}, "call-%d" % state["n"])

        model = self.scripted(responder)
        specs = [subagent.Spec(role="recon", objective="一直调工具，不交报告")]
        events, aggregate = drain(subagent.spawn(
            specs, plane=self.plane, profile=model.profile, api_key="k",
            budget=subagent.Budget(30), policy_id="probe", **kwargs))
        return model, aggregate

    def test_the_default_ceiling_is_unchanged(self):
        """`delegate` 不传这个参数，行为必须与以前完全一样。"""
        _model, aggregate = self._run()
        child = aggregate["children"][0]
        self.assertEqual(child["steps"], subagent.CHILD_MODEL_CALLS)
        self.assertIn("%d 次模型调用上限" % subagent.CHILD_MODEL_CALLS,
                      child["error"])

    def test_a_raised_ceiling_lets_the_child_use_more_of_the_pool(self):
        _model, aggregate = self._run(max_model_calls=12)
        child = aggregate["children"][0]
        self.assertEqual(child["steps"], 12, "放宽后的天花板没有生效")
        self.assertIn("12 次模型调用上限", child["error"])
        self.assertEqual(aggregate["budget"]["charged"], 12)


class EmptyChildTestCase(unittest.TestCase):
    """收尾路径的**有依据**判据：有文本不等于有结论。

    `_wrap_up` 走的是"预算或时限用尽，交报告吧"这条收尾路径。一次工具都没跑成
    的子智能体，上下文里只有它自己的 objective 和一句"该交报告了"——模型只能
    照着目标编。老实现把这段文本直接当成结论，于是父模型会看到一个 ✓ 挨着一条
    凭空写出来的结论，而它和一份真报告长得完全一样。
    """

    class _StubChat:
        """`_wrap_up` 只需要一个能调用的 `chat_once`。"""

        def __init__(self, text):
            self.text = text

        def chat_once(self, profile, api_key, messages, schemas, prompt,
                      insecure=False):
            return {"ok": True, "text": self.text}

    def wrap(self, result, text):
        subagent._wrap_up(self._StubChat(text), {}, "k",
                          [{"role": "user", "text": "x"}], "p", False,
                          result, lambda event: None, 1, 1,
                          subagent.Spec("recon", "x"))
        return result

    def test_a_child_that_ran_nothing_is_never_certified(self):
        result = subagent.ChildResult(1, "recon", "probe")
        result.status = "running"
        self.wrap(result, "结论：侦察完成，未发现异常\n证据：无\n建议：无")
        self.assertFalse(result.ok, "一次工具都没跑成，却被判成成功")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.summary, "", "凭空写出来的结论被当成了结论")
        self.assertIn("没有依据", result.error)

    def test_a_child_that_ran_a_tool_is_still_certified(self):
        """正面：有工具结果托着，收尾报告照旧算数。少了这一条，
        "不判成功"会被一条把所有收尾都判死的实现骗过去。"""
        result = subagent.ChildResult(1, "recon", "probe")
        result.status = "running"
        result.steps = 1
        result.tool_calls.append({"name": "list_executors", "ok": True,
                                  "brief": "", "durationMs": 1})
        self.wrap(result, "结论：侦察完成，未发现异常\n证据：无\n建议：无")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.status, "done")
        self.assertIn("侦察完成", result.summary)

    def test_the_real_cause_survives_the_grounding_check(self):
        """判死时，报错里必须还是**真正的原因**（预算/时限），不能被
        "报告没有依据"顶掉——那是症状，不是病因。"""
        result = subagent.ChildResult(1, "recon", "probe")
        result.status = "running"
        result.error = "父轮的步数预算已用尽（上限 3 步，每人保底 1 步）。"
        self.wrap(result, "结论：编的\n证据：无\n建议：无")
        self.assertIn("步数预算", result.error)


# ---------------------------------------------------------------- 闸门

class GateTestCase(Base):
    """不变量 4：派活过的是**同一道闸**。"""

    def test_delegate_is_denied_in_readonly(self):
        outcome = chat.execute_tool(self.plane, subagent.DELEGATE_TOOL,
                                    {"children": [{"role": "r",
                                                   "objective": "x"}]}, "",
                                    "readonly")
        self.assertFalse(outcome["ok"])
        self.assertIn("权限拒绝", outcome["summary"])
        self.assertTrue(outcome["data"]["denied"])
        self.assertEqual(outcome["data"]["required"], "probe")

    def test_denied_delegate_never_touches_the_model_or_the_children(self):
        model = self.scripted(lambda record: text_reply("不该被调用"))
        result = chat.run_turn(
            self.plane, model.profile, "k",
            [{"role": "user", "text": "派活"}], policy_id="readonly",
            max_steps=6, agent={"id": "builtin-pentest", "label": "渗透",
                                "prompt": "", "steps": 6})
        # 只读档看不到 delegate，模型无从调用它——这一轮正常结束
        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(result["subagents"], [])
        for trace in result["trace"]:
            self.assertNotEqual(trace["tool"], subagent.DELEGATE_TOOL)
        self.assertNotIn(subagent.DELEGATE_TOOL,
                         {tool["name"] for tool in
                          policy.filter_tools(chat.TOOLS, "readonly", "", True)})

    def test_delegate_outside_the_loop_fails_loudly(self):
        """handler 那一格必须**明确失败**，不能返回一个看起来成功的空结果。

        否则"派活其实没发生"会变成一件要靠数工具调用才能发现的事。
        """
        outcome = chat.execute_tool(self.plane, subagent.DELEGATE_TOOL,
                                    {"children": [{"role": "r",
                                                   "objective": "x"}]}, "",
                                    "full", "admin", True)
        self.assertFalse(outcome["ok"])
        self.assertIn("运行时", outcome["summary"])
        self.assertIn("派活只能在对话循环内", outcome["summary"])

    def test_delegate_stays_in_sync_with_the_policy_table(self):
        # `_tool_level_guard` 在 import 时就会跑；这里把它的两个方向都显式断言一遍，
        # 免得将来有人把守卫去掉而没人发现。
        self.assertIn(subagent.DELEGATE_TOOL, chat.HANDLERS)
        self.assertIn(subagent.DELEGATE_TOOL, policy.TOOL_LEVEL)
        self.assertEqual(policy.TOOL_LEVEL[subagent.DELEGATE_TOOL], "probe")
        self.assertIn(subagent.DELEGATE_TOOL,
                      {tool["name"] for tool in chat.TOOLS})
        # 只读档拿不到、probe 档起拿得到——这条边界是本模块刻意的决定（见
        # `policy.TOOL_LEVEL` 里 `delegate` 那一段），不是巧合，所以钉住它。
        self.assertNotIn(subagent.DELEGATE_TOOL,
                         policy.allowed_tools("readonly", "", True))
        self.assertIn(subagent.DELEGATE_TOOL,
                      policy.allowed_tools("probe", "", True))

    def test_bad_arguments_fail_without_calling_the_model(self):
        model = self.scripted(lambda record: text_reply("不该被调用"))
        events, outcome = drain(subagent.run_delegation(
            self.plane, {"children": []}, profile=model.profile, api_key="k",
            max_steps=6, steps=0, policy_id="probe"))
        self.assertEqual(events, [])
        self.assertFalse(outcome[0]["ok"])
        self.assertIn("参数不合法", outcome[0]["summary"])
        self.assertEqual(model.requests, [])


# ---------------------------------------------------------------- 断连

class CancellationTestCase(Base):
    def test_closing_the_generator_stops_the_children(self):
        """客户端断连后子智能体要在**下一个步边界**退出，不能跑到超时。

        做不到立刻中断（在飞的模型调用是阻塞的），但"置位 stop 并等线程收尾"
        与"一直跑到 150 秒超时"是两件不同的事——后者会让一个已经没人看的
        请求占着线程与模型配额。
        """
        started = threading.Event()

        def responder(record):
            if role_of(record):
                started.set()
                return tool_reply("list_executors", {}, "c1")
            return text_reply("done")

        model = self.scripted(responder)
        specs = subagent.parse_specs({"children": [
            {"role": "recon", "objective": "x"}]})
        generator = subagent.spawn(specs, plane=self.plane,
                                   profile=model.profile, api_key="k",
                                   budget=subagent.Budget(50), policy_id="probe")
        next(generator)                     # 至少跑起来
        self.assertTrue(started.wait(10), "子智能体没有开始")
        begin = time.time()
        generator.close()                   # 模拟客户端断连
        self.assertLess(time.time() - begin, 30.0,
                        "关闭生成器没有及时收回子智能体线程")


# ---------------------------------------------------------------- 变异守卫

class InvariantGuardTestCase(Base):
    """把"最容易被顺手优化掉"的两条各打一个变异，证明守卫真的会红。

    这个类不测实现，它测**别的测试有没有牙**：如果哪天有人把
    `resolve_tools` 改成返回全集、或把 `charged` 改成 0，上面的断言必须失败。
    做法是就地替换掉那个函数 / 那一行，跑一次，确认结果变了，然后恢复。
    """

    def test_narrowing_guard_has_teeth(self):
        original = subagent.resolve_tools

        def widened(spec, policy_id, role, auth_required):
            names, _ = original(spec, policy_id, role, auth_required)
            return sorted(set(names) | {subagent.DELEGATE_TOOL}), []

        subagent.resolve_tools = widened
        try:
            names, _ = subagent.resolve_tools(subagent.Spec("r", "x"),
                                              "probe", "", True)
            self.assertIn(subagent.DELEGATE_TOOL, names)
        finally:
            subagent.resolve_tools = original
        names, _ = subagent.resolve_tools(subagent.Spec("r", "x"),
                                          "probe", "", True)
        self.assertNotIn(subagent.DELEGATE_TOOL, names)

    def test_budget_guard_has_teeth(self):
        """把预算改成一问就给，子智能体立刻多跑——说明那道上限真的在拦。

        这不是重复 `BudgetTestCase`：那一条测的是计数器本身，这一条测的是
        **`_run_child` 有没有真的用它在拦**。把 `take()` 换成"永远同意"，
        同一个用例必须看到不一样的数字；看不到，就说明上限形同虚设。
        """
        class Unbounded(subagent.Budget):
            def take(self, index=None):
                with self._lock:
                    self._used += 1
                    return True

        WRAP = "预算已用尽"

        def responder(record):
            if not role_of(record):
                return text_reply("done")
            messages = record["body"].get("messages") or []
            if any(WRAP in (m.get("content") or "") for m in messages
                   if isinstance(m.get("content"), str)):
                return text_reply("结论：收到了\n证据：无\n建议：无")
            tool_calls = sum(1 for m in messages
                             if isinstance(m.get("content"), str)
                             and "list_executors" in m.get("content"))
            if tool_calls < 3:
                return tool_reply("list_executors", {}, "c%d" % tool_calls)
            return text_reply("结论：无\n证据：无\n建议：无")

        model = self.scripted(responder)
        args = {"children": [{"role": "recon", "objective": "x"}]}

        def charged_once():
            model.requests.clear()
            _, (_, charged) = drain(subagent.run_delegation(
                self.plane, args, profile=model.profile, api_key="k",
                max_steps=3, steps=0, policy_id="probe"))
            return charged

        bounded = charged_once()          # 预算 = 3 - 0 - 1 = 2
        self.assertEqual(bounded, 2)
        original = subagent.Budget
        subagent.Budget = Unbounded
        try:
            unbounded = charged_once()
        finally:
            subagent.Budget = original
        self.assertGreater(
            unbounded, bounded,
            "上限没有起作用：改成不拦之后计费没变（%d → %d），这条用例没有牙"
            % (bounded, unbounded))


# ---------------------------------------------------------------- 落库形状

class PersistedRowShapeTestCase(Base):
    """派活结果必须**随消息落库**，而且落库的形状与前端那一行**逐键一致**。"""

    def delegate_once(self, responder):
        """父轮**只派一次**活：第二次及以后直接给结论。

        不这样包一层的话，父循环会一直派下去（脚本化的父模型每次都要求派活），
        一组用例就变成"跑满 max_steps 的 N 次派活"——测的已经不是落库形状了。
        （这也顺带说明：`subagents` 是**累加**的，多轮派活会攒在一起，这是对的。）
        """
        state = {"delegated": False}

        def wrapped(record):
            if not role_of(record):
                if not state["delegated"]:
                    state["delegated"] = True
                    return tool_reply("delegate", {"children": [
                        {"role": "recon", "objective": "看存活"},
                        {"role": "web", "objective": "看 80"}]}, "call-p")
                return text_reply("已派出 2 个子智能体并汇总完毕。")
            return responder(record)

        model = self.scripted(wrapped)
        result = chat.run_turn(
            self.plane, model.profile, "k",
            [{"role": "user", "text": "派几个人去看看"}],
            policy_id="probe", max_steps=8)
        return model, result

    def test_rows_ride_along_with_the_assistant_message(self):
        """只活在流里的话，刷新一次"那几个子智能体带回了什么"就全没了。"""
        def responder(record):
            if role_of(record) == "recon":
                return text_reply("结论：7 台存活\n证据：10.0.0.1\n建议：无")
            return text_reply("结论：nginx\n证据：Server 头\n建议：无")

        _, result = self.delegate_once(responder)
        self.assertTrue(result["ok"], result.get("message"))
        carried = [message for message in result["appended"]
                   if message.get("role") == "assistant" and message.get("subagents")]
        self.assertEqual(len(carried), 1, "派活结果没有挂到 assistant 消息上")
        rows = carried[0]["subagents"]
        self.assertEqual([row["role"] for row in rows], ["recon", "web"])
        self.assertEqual({row["state"] for row in rows}, {"done"})
        self.assertTrue(all(row["summary"] for row in rows), "结论没落库")
        self.assertEqual([row["index"] for row in rows], [1, 2])
        self.assertEqual({row["total"] for row in rows}, {2})
        self.assertEqual(rows[0]["policy"], "probe")
        self.assertTrue(rows[0]["toolCount"] > 0, "能力数没落库")
        # 接口返回的顶层 `subagents` 与落库那份**是同一份**，不是一件事两个生产者
        self.assertEqual(result["subagents"], rows)

    def test_failure_reason_is_persisted_too(self):
        """失败也要落库——否则刷新之后"失败的那个"变成一行没有原因的空白。"""
        def responder(record):
            if role_of(record) == "recon":
                return {"error": {"message": "上游 502"}}
            return text_reply("结论：nginx\n证据：Server 头\n建议：无")

        _, result = self.delegate_once(responder)
        rows = [message for message in result["appended"]
                if message.get("subagents")][0]["subagents"]
        failed = [row for row in rows if row["state"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("502", failed[0]["error"])
        self.assertEqual([row["state"] for row in rows], ["failed", "done"])

    def test_only_the_tools_that_opened_a_job_carry_one(self):
        """`_tool_row()` 只在真开了任务时给出 `jobId`。

        给没开任务的工具补一个空串，前端就会把每一枚芯片都画成可点——点了没反应，
        比不可点更糟。所以这里钉两半：有 jobId 的原样带，没有的那个键**不存在**。
        """
        opened = chat._tool_row({"name": "start_probe", "ok": True, "brief": "已下发",
                                "durationMs": 12, "jobId": "job-1a2b3c4d"})
        self.assertEqual(opened["jobId"], "job-1a2b3c4d")
        self.assertEqual(set(opened), {"label", "ok", "brief", "durationMs", "jobId"})
        plain = chat._tool_row({"name": "browse", "ok": True, "brief": "看了"})
        self.assertNotIn("jobId", plain, "没开任务的工具不该带一个空的 jobId")
        self.assertEqual(set(plain), {"label", "ok", "brief", "durationMs"})
        # 空串 / None 与"没有"是同一件事：派活工具失败时也会回一个没有 jobId 的 data
        self.assertNotIn("jobId", chat._tool_row({"name": "start_probe", "ok": False,
                                                  "jobId": ""}))

    def test_persisted_row_keys_match_the_frontend_row(self):
        """跨文件契约：后端落库的键 == 前端 `newSubagentRow()` 的键。

        两边各写一份形状、只改了一边，症状是"刷新之后那一块缺字段或画不出来"，
        而**实时看着一切正常**——只有刷新才现形。所以直接从 `web/chat.js` 把
        那一行的键抠出来比。`note`（报告没按格式给出的过程说明）就是这么发现的：
        前端行有、落库行没有。
        """
        with open(os.path.join(ROOT, "web", "chat.js"), encoding="utf-8") as handle:
            js = handle.read()
        start = js.index("function newSubagentRow")
        body = js[start:js.index("}", start)]
        front = set(re.findall(r"(\w+)\s*:", body))
        back = set(chat._subagent_rows({"spawned": 1, "children": [{"index": 1}]})[0])
        self.assertTrue(front, "没能从 chat.js 里抠出前端行的键，这条断言就白写了")
        self.assertEqual(back, front,
                         "前端行与落库行的键不一致：仅后端有 %s，仅前端有 %s"
                         % (sorted(back - front), sorted(front - back)))


if __name__ == "__main__":
    unittest.main()
