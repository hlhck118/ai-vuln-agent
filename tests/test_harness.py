# -*- coding: utf-8 -*-
"""DSH 能力桥：终端、授权审查、能力对照表。

这一组用例的核心不是"函数返回了什么"，而是三件**只有真跑一遍才知道**的事：

1. **审查挂在执行之前。** 只查"审查单建了"的用例，在命令早就跑完之后照样全绿。
   所以这里在审查单出现的那一刻去问文件系统，并且断言它**还不存在**。
2. **拒绝与超时都不执行。** 批准路径跑通证明不了拒绝路径没执行——两条路径共用
   `chat._reviewed`，而"忘了看 status"这种错只在拒绝那一次才暴露。
3. **档位先于表判定。** `chat_review=off` 必须能关掉 `REVIEW_ALWAYS`。这个顺序我写
   反过（先查表后查档），`tools/harness_demo.py` 的第三个场景当场抓出来：命令跑完
   了，而审查单也建了。这里有一条用例专门钉它。
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import approval, chat, harness, policy, server, terminal  # noqa: E402
from pentestd.store import Store                                        # noqa: E402

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
    """最小模型端点：整包 JSON 回话，脚本由 `responder(record)` 决定。

    刻意不用 `tests/test_chat_stream.py` 里那个 SSE 版本：那个类服务的是分帧与
    回退，而这里要验的是**循环与审查**，走整包返回能让"哪一次请求对应哪一步"
    一眼看清。
    """

    def __init__(self, responder):
        self.responder = responder
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                record = {"path": self.path, "body": json.loads(raw or "{}")}
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


class PlaneCase(unittest.TestCase):
    """公共夹具：临时库 + 真控制面 + 自动清理。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(os.path.join(self.directory.name, "pentest.db"))
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)

    def set_mode(self, mode):
        self.store.set_setting(harness.SETTING_KEY, mode)

    def run_turn(self, model, **kwargs):
        """跑完一轮，返回 (全部事件, done 事件, 最后一条 assistant 消息)。"""
        events, done, last = [], None, None
        for event in chat.run_turn_stream(
                self.plane, model.profile(), "k",
                [{"role": "user", "text": "在控制面跑一条命令"}],
                policy_id=kwargs.pop("policy_id", "full"),
                max_steps=kwargs.pop("max_steps", 6), stream=False, **kwargs):
            events.append(event)
            if event.get("type") == "done":
                done = event
            if event.get("type") == "persist":
                last = event["message"]
        return events, done, last

    def shell_command(self, path, marker):
        name, _prefix = terminal.resolve_shell()
        if name in ("pwsh", "powershell"):
            return "Set-Content -LiteralPath '%s' -Value '%s'" % (path, marker)
        return "printf '%%s' '%s' > '%s'" % (marker, path)


# ------------------------------------------------------------------ 能力对照表

class CapabilityTableTestCase(unittest.TestCase):
    """对照表里写下的每一句都要能被核。"""

    def test_ids_are_unique(self):
        ids = [item["id"] for item in harness.CAPABILITIES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(ids),
                         ["loop", "review", "subagent", "team", "terminal"])

    def test_landed_capabilities_are_really_wired(self):
        """`landed` 的每一项，工具都必须在 `chat.HANDLERS` 与 `policy.TOOL_LEVEL` 里。

        这条断言是"对照表不会变成一份过期的说明"的落点：把某一项标成 landed
        而工具其实不在，或者反过来（工具接上了、表还写着 planned），都会红。
        """
        for item in harness.CAPABILITIES:
            if item["status"] != "landed":
                continue
            for tool in item["tools"]:
                self.assertIn(tool, chat.HANDLERS,
                              "%s 标成 landed，但 %s 不在 chat.HANDLERS 里"
                              % (item["id"], tool))
                self.assertIn(tool, policy.TOOL_LEVEL,
                              "%s 的工具 %s 没有在 policy.TOOL_LEVEL 里分级"
                              % (item["id"], tool))

    def test_every_capability_names_its_dsh_row(self):
        for item in harness.CAPABILITIES:
            self.assertTrue(item["dsh"].strip(), item["id"])
            self.assertTrue(item["invariant"].strip(), item["id"])
            self.assertTrue(item["limit"].strip(),
                            "%s 没写边界；没有边界的能力说明是不完整的" % item["id"])

    def test_review_tables_do_not_drift(self):
        """`REVIEW_ALWAYS` 与 `REVIEW_STRICT` 里的每个名字都得是真工具。"""
        names = set(chat.HANDLERS)
        for tool in harness.REVIEW_ALWAYS + harness.REVIEW_STRICT:
            self.assertIn(tool, names, "审查表里点了一个不存在的工具：%s" % tool)
        self.assertEqual(harness.REVIEW_ALWAYS, ("run_command",))


# ------------------------------------------------------------------ 审查分档

class TierTestCase(unittest.TestCase):
    """分档逻辑。三条边界，每条都对应一种真实的错法。"""

    def test_run_command_is_always_reviewed_in_ask(self):
        self.assertEqual(harness.tier_of("run_command", "ask"), "always")
        self.assertTrue(harness.needs_review("run_command"))

    def test_intrusive_tools_only_in_strict(self):
        for tool in harness.REVIEW_STRICT:
            self.assertEqual(harness.tier_of(tool, "ask"), "",
                             "%s 在 ask 档不该要审查（它另有一道部署级授权闸）" % tool)
            self.assertEqual(harness.tier_of(tool, "strict"), "strict")

    def test_off_wins_over_the_always_table(self):
        """**档位先于表判定。**

        写反过的那个版本（先查表、后查档）在 `off` 下仍然为 `run_command` 返回
        `always`：界面上选了"不审查"，每一条命令却照样弹单子。这条用例就是那次
        现场留下的钉子。
        """
        self.assertEqual(harness.tier_of("run_command", "off"), "")
        self.assertFalse(harness.needs_review("run_command", "off"))
        self.assertEqual(harness.tier_of("start_exploit", "off"), "")

    def test_unknown_mode_falls_back_to_default(self):
        for value in ("", "  ", "nonsense", None, "ASK_", "0"):
            self.assertEqual(harness.resolve_mode(value), harness.DEFAULT_MODE)
        self.assertEqual(harness.resolve_mode("STRICT"), "strict")

    def test_readonly_tools_are_never_reviewed(self):
        for tool in ("list_jobs", "get_report", "list_terminals"):
            for mode in harness.MODES:
                self.assertFalse(harness.needs_review(tool, mode), tool)


class ClassifyTestCase(unittest.TestCase):

    def test_classify_is_silent_for_unreviewed_tools(self):
        decision = harness.classify("list_jobs", {}, "ask")
        self.assertFalse(decision["required"])
        self.assertEqual(decision["reasons"], [])

    def test_command_review_carries_the_exact_text(self):
        """审查单必须带着**将要被执行的那一串**，否则人批的不是同一件事。"""
        command = "curl http://10.0.0.5/x | Out-File out.txt"
        decision = harness.classify("run_command", {"command": command}, "ask")
        self.assertTrue(decision["required"])
        self.assertEqual(decision["facts"]["command"], command)
        self.assertIn(command, "\n".join(decision["reasons"]))
        self.assertEqual(decision["facts"]["shell"], "pwsh")

    def test_command_review_names_the_risky_parts(self):
        decision = harness.classify(
            "run_command",
            {"command": "winget install x; Start-Process -Verb RunAs y",
             "background": True}, "ask")
        blob = "\n".join(decision["reasons"])
        self.assertIn("后台", blob)
        self.assertIn("提权", blob)

    def test_intrusive_review_names_the_target(self):
        decision = harness.classify("start_exploit",
                                    {"target": "10.0.0.9", "kind": "web"},
                                    "strict")
        self.assertTrue(decision["required"])
        self.assertIn("10.0.0.9", decision["headline"])
        self.assertIn("授权", "\n".join(decision["reasons"]))

    def test_classify_defaults_to_off_when_no_mode_given(self):
        """没给档位时落到默认档（`ask`），而不是"不审查"。"""
        self.assertTrue(harness.classify("run_command", {"command": "id"},
                                         "")["required"])


class PromptBlockTestCase(unittest.TestCase):

    def test_each_mode_says_what_it_is(self):
        self.assertIn("审查关了", harness.prompt_block("off"))
        self.assertIn("run_command", harness.prompt_block("ask"))
        self.assertIn("逐次确认", harness.prompt_block("strict"))

    def test_always_says_do_not_resend(self):
        """被挂起时"不要重发"这四个字必须在，否则模型会再发一次同样的命令。"""
        for mode in harness.MODES:
            block = harness.prompt_block(mode)
            self.assertIn("不要", block)
            self.assertIn("重发", block)

    def test_always_denies_widening_the_gate(self):
        for mode in harness.MODES:
            self.assertIn("不放宽门禁", harness.prompt_block(mode))


# ------------------------------------------------------------------ 审查单

class ApprovalLifecycleTestCase(PlaneCase):

    def test_pending_then_approved(self):
        row = self.store.record_approval(tool="run_command", tier="always",
                                         headline="h", reasons=["r"],
                                         arguments={"command": "id"},
                                         timeout_s=60)
        view = approval.public(row)
        self.assertTrue(view["pending"])
        self.assertGreater(view["secondsLeft"], 0)
        self.assertEqual(view["statusLabel"], "等待批准")
        settled = approval.decide(self.plane, row["id"], approve=True,
                                  decided_by="u-1", note="同意")
        self.assertEqual(settled["status"], "approved")
        self.assertEqual(settled["decidedBy"], "u-1")
        self.assertEqual(settled["secondsLeft"], 0)

    def test_deciding_twice_is_refused(self):
        """只生效一次。静默以"最后一次为准"等于谁后点谁赢。"""
        row = self.store.record_approval(tool="run_command", timeout_s=60)
        approval.decide(self.plane, row["id"], approve=True, decided_by="u-1")
        with self.assertRaises(approval.ApprovalError):
            approval.decide(self.plane, row["id"], approve=False,
                            decided_by="u-2")
        self.assertEqual(self.store.get_approval(row["id"])["status"], "approved")

    def test_tier_label_is_chinese_and_falls_back_to_the_raw_word(self):
        """档位是自由文本，认不出来的必须原样印出来。

        `harness.tier_of` 只会给 always / strict / 空，但库里这一列是自由文本：
        早期版本、插件自己建的闸都往这里写（`tests/test_review_ownership.py`
        的夹具就写 `tier="shell"`）。给未知值编一个中文，等于把「我不知道这是
        什么」伪装成「这是另一种已知档位」——而这一页的用途正是事后追责。
        """
        row = self.store.record_approval(tool="run_command", tier="always",
                                         headline="h", timeout_s=60)
        self.assertEqual(approval.public(row)["tierLabel"], "每次都拦")
        other = self.store.record_approval(tool="run_command", tier="shell",
                                           headline="h", timeout_s=60)
        self.assertEqual(approval.public(other)["tierLabel"], "shell")

    def test_tier_label_is_not_the_review_mode_name(self):
        """单子上的档位与「闸照开在哪一档」是两件事，词不能合一。

        `harness.MODES` 的 strict 是会话档位，单子上的 strict 说的是「这条属于
        侵入动作那一类」。拿档位名当档位说明用，一张 ask 档下拦下来的
        run_command 单子会被读成「这是在 strict 档下拦的」。

        注意这里比的是**整词相等**，不是子串：四档的说明里本来就要提「侵入
        动作」这四个字（那档管的就是这些动作），把子串也禁掉等于禁掉解释。
        """
        modes = harness.catalog(harness.DEFAULT_MODE)["review"]["modes"]
        self.assertEqual(len(harness.MODES), len(modes),
                         "四档各有各的说明，数量必须对得上")
        mode_words = set(harness.MODES) | {item["label"] for item in modes}
        for tier_label in approval.TIER_LABELS.values():
            self.assertNotIn(tier_label, mode_words,
                             "单子档位「%s」与某个会话档位同名了" % tier_label)
    def test_submit_strips_the_marks_from_the_card(self):
        """审查卡上的字是 `textContent`，星号与反引号会被原样读到。

        病史：`harness.classify` 只在自己那条出口滤过一次；MCP 工具
        （`mcp.classify`）与子智能体计划（`team.review_facts` 钩子）各自往
        `approval.submit` 里塞理由，于是卡片上还能看到「这是**外部 MCP 服务**
        提供的工具」。闸放在**唯一写入口**：谁塞进来都过同一道，漏不掉。

        只滤给人看的两格。`arguments` 是命令原文——滤它等于改证据。
        """
        row = approval.submit(
            self.plane, tool="run_command", tier="always",
            headline="执行 **id**",
            reasons=["这是**外部服务**给的", "看 `data/tools` 那一行"],
            facts={"command": "id"},
            arguments={"command": "echo **not filtered**"},
            timeout_s=60)
        self.assertEqual(row["headline"], "执行 id")
        self.assertEqual(row["reasons"],
                         ["这是外部服务给的", "看 data/tools 那一行"])
        self.assertEqual(row["arguments"]["command"], "echo **not filtered**",
                         "参数是命令原文，不许被当成一句给人读的话顺手洗掉")
        # 判据要读**库里那一行**：`public()` 也有一道（给早先落库的单子），
        # 只查返回值的话，写入口那道闸坏掉时这条用例仍然是绿的。
        stored = self.store.get_approval(row["id"])
        self.assertEqual(stored["reasons"],
                         ["这是外部服务给的", "看 data/tools 那一行"],
                         "落库的 reasons 还带着记号")
        self.assertEqual(stored["headline"], "执行 id", "落库的 headline 还带着记号")

    def test_the_public_shape_also_cleans_rows_written_around_the_gate(self):
        """库里早先的单子带着记号，读的时候也得能读。

        写入口那道闸是后加的；上一版落库的单子仍原样躺在 `reasons` 列里。
        所以在 `public()` 上再滤一遍——**只改呈现**，不动库里那一行：读接口
        不写库，与它处理「过期了却还挂着 pending」是同一条理由。
        """
        raw = self.store.record_approval(
            tool="run_command", tier="always", headline="执行 **id**",
            reasons=["这是**外部服务**给的"], timeout_s=60)
        self.assertIn("**", raw["reasons"][0], "夹具要真的带记号")
        view = approval.public(raw)
        self.assertEqual(view["headline"], "执行 id")
        self.assertEqual(view["reasons"], ["这是外部服务给的"])
        self.assertEqual(self.store.get_approval(raw["id"])["reasons"],
                         ["这是**外部服务**给的"], "库里的那一行不许被改写")

    def test_non_human_settle_cannot_touch_a_decided_row(self):
        row = self.store.record_approval(tool="run_command", timeout_s=60)
        approval.decide(self.plane, row["id"], approve=True, decided_by="u-1")
        self.assertIsNone(self.store.settle_approval(row["id"], "expired", "x"))
        self.assertEqual(self.store.get_approval(row["id"])["status"], "approved")

    def test_non_human_settle_does_take_a_pending_row(self):
        """超时与"这一轮结束"走的就是这条 —— 它必须能收掉**还没人裁决**的单子。

        与上一条配对：一条钉"不能碰已裁决的"，一条钉"必须能碰没裁决的"。
        只写前者的实现在超时路径上会静默失效，而那个失效看起来像"审查卡住了"。
        """
        row = self.store.record_approval(tool="run_command", timeout_s=60)
        self.assertIsNotNone(self.store.settle_approval(row["id"], "expired",
                                                        "超时"))
        self.assertEqual(self.store.get_approval(row["id"])["status"], "expired")

    def test_deciding_a_missing_row_is_an_error_not_a_silent_true(self):
        with self.assertRaises(approval.ApprovalError):
            approval.decide(self.plane, "R-nope", approve=True)

    def test_timeout_clamps(self):
        self.assertEqual(approval.timeout_of(None), harness.DEFAULT_TIMEOUT_S)
        self.assertEqual(approval.timeout_of("nonsense"),
                         harness.DEFAULT_TIMEOUT_S)
        # 0 与负数一律回退到默认值，而不是夹到最小 —— 夹到最小的症状是
        # "每一张单子在下一次轮询里立刻过期"，看起来像使用者手慢。
        self.assertEqual(approval.timeout_of(0), harness.DEFAULT_TIMEOUT_S)
        self.assertEqual(approval.timeout_of(-5), harness.DEFAULT_TIMEOUT_S)
        self.assertEqual(approval.timeout_of(10 ** 9), harness.TIMEOUT_MAX_S)
        self.assertEqual(approval.timeout_of(1), harness.TIMEOUT_MIN_S)


class ApprovalWaitTestCase(PlaneCase):
    """等待的三条出口。它们决定"没人应答时会不会执行"，而那是这个模块的全部意义。"""

    def _expire(self, review_id):
        """把 `expires_at` 挪到过去：等价于"等够了期限"，但不用真等。"""
        self.store.conn.execute(
            "UPDATE approvals SET expires_at=? WHERE id=?", (time.time() - 1,
                                                             review_id))
        self.store.conn.commit()

    def test_timeout_is_a_refusal_not_a_later_retry(self):
        row = self.store.record_approval(tool="run_command", timeout_s=30,
                                         reasons=["x"])
        self._expire(row["id"])
        status, settled = approval.wait(self.plane, row["id"], interval=0.01)
        self.assertEqual(status, "expired")
        self.assertIn("超时", settled["note"])
        self.assertEqual(approval.verdict(settled)["allowed"], False)

    def test_a_dropped_connection_is_not_an_exit(self):
        """客户端断开**不是**等待的一条出口。

        这一条原来叫 `test_closed_stream_settles_the_review`，断言断开就把单子
        收成 `expired`。那个行为两个方向都不对：一是它只在"写失败"的那一刻才
        观察得到，而等待期间一个字节都不写（生成器停在 `wait` 里），所以人刷新
        页面时它**根本不触发** —— 同一个动作两种结果；二是它把"看的人走了"
        当成了"等的人走了"，于是刷新一下就把人的裁决权弄丢了。

        现在等待期只剩超时一条出口，断开与否由 `awaited()` 单独回答。真丢连接、
        再从另一条连接裁决的端到端版本在 `tests/test_review_survives_reload.py`；
        这里钉的是"它不再有办法看见连接"。
        """
        row = self.store.record_approval(tool="run_command", timeout_s=30)
        with self.assertRaises(TypeError):
            approval.wait(self.plane, row["id"], is_closed=lambda: True)
        self.assertEqual("pending", self.store.get_approval(row["id"])["status"],
                         "探针传不进去，也就不该有任何东西被它改写")

    def test_a_decision_arriving_while_waiting_is_picked_up(self):
        row = self.store.record_approval(tool="run_command", timeout_s=300)

        def decide_soon():
            time.sleep(0.15)
            approval.decide(self.plane, row["id"], approve=True,
                            decided_by="u-9", note="边等边批")

        thread = threading.Thread(target=decide_soon, daemon=True)
        thread.start()
        status, settled = approval.wait(self.plane, row["id"], interval=0.02)
        thread.join(timeout=3)
        self.assertEqual(status, "approved")
        self.assertEqual(settled["decidedBy"], "u-9")

    def test_verdict_text_is_quotable(self):
        """拒绝的那句话是模型唯一能转述的东西，它必须写清"谁拒的"。"""
        row = self.store.record_approval(tool="run_command", timeout_s=30)
        approval.decide(self.plane, row["id"], approve=False, decided_by="老王",
                        note="这台机器上不许跑")
        outcome = approval.outcome(approval.public(
            self.store.get_approval(row["id"])))
        self.assertFalse(outcome["ok"])
        self.assertIn("老王", outcome["summary"])
        self.assertIn("这台机器上不许跑", outcome["summary"])
        self.assertIn("不要重发", outcome["summary"])

    def test_approved_outcome_says_so(self):
        row = self.store.record_approval(tool="run_command", timeout_s=30)
        approval.decide(self.plane, row["id"], approve=True, decided_by="u-1")
        outcome = approval.outcome(approval.public(
            self.store.get_approval(row["id"])))
        self.assertTrue(outcome["ok"])
        self.assertTrue(outcome["data"]["approved"])


# ------------------------------------------------------------------ 终端

class TerminalRunTestCase(PlaneCase):

    def test_foreground_captures_output_and_exit_code(self):
        outcome = terminal.run(self.plane, {"command": "Write-Output 'hello-term'",
                                            "shell": "", "timeoutS": 30})
        self.assertIn("退出码 0", outcome["summary"])
        self.assertIn("hello-term", outcome["data"]["output"])
        terminal_id = outcome["data"]["terminalId"]
        row = self.store.get_terminal(terminal_id)
        self.assertEqual(row["status"], "exited")
        self.assertEqual(row["exitCode"], 0)
        self.assertTrue(os.path.isfile(row["outputPath"]),
                        "输出必须落到文件里：控制面重启之后它是唯一的完整副本")

    def test_nonzero_exit_is_reported_as_itself(self):
        outcome = terminal.run(self.plane, {"command": "exit 3", "timeoutS": 20})
        self.assertEqual(outcome["data"]["exitCode"], 3)
        self.assertIn("退出码 3", outcome["summary"])

    def test_timeout_kills_the_command(self):
        command = "Start-Sleep -Seconds 20" \
            if terminal.resolve_shell()[0] in ("pwsh", "powershell") else "sleep 20"
        started = time.time()
        outcome = terminal.run(self.plane, {"command": command, "timeoutS": 1})
        self.assertLess(time.time() - started, 12,
                        "超时之后必须真的终止，而不是等它自己跑完")
        self.assertEqual(self.store.get_terminal(
            outcome["data"]["terminalId"])["status"], "timeout")
        self.assertIn("超时", outcome["summary"])

    def test_arguments_are_validated(self):
        with self.assertRaises(terminal.TerminalError):
            terminal.run(self.plane, {"command": ""})
        with self.assertRaises(terminal.TerminalError):
            terminal.run(self.plane, {"command": "id", "shell": "klingon"})
        with self.assertRaises(terminal.TerminalError):
            terminal.run(self.plane, {"command": "id",
                                      "cwd": os.path.join(self.directory.name,
                                                          "nope")})

    def test_ids_are_checked_before_touching_the_store(self):
        for bad in ("../etc/passwd", "T-zz", "", "T-1a2b3c4d5", None):
            with self.assertRaises(terminal.TerminalError, msg=repr(bad)):
                terminal.read(self.plane, {"terminalId": bad})

    def test_read_of_a_missing_terminal_says_so(self):
        with self.assertRaises(terminal.TerminalError):
            terminal.read(self.plane, {"terminalId": "T-aaaaaaaa"})


class TerminalBackgroundTestCase(PlaneCase):

    def test_background_runs_and_is_readable(self):
        pending = "Start-Sleep -Seconds 1; Write-Output 'bg-done'" \
            if terminal.resolve_shell()[0] in ("pwsh", "powershell") \
            else "sleep 1; echo bg-done"
        outcome = terminal.start_background(self.plane, {"command": pending})
        terminal_id = outcome["data"]["terminalId"]
        self.assertEqual(outcome["data"]["status"], "running")
        self.assertTrue(int(outcome["data"]["pid"]) > 0)
        deadline = time.time() + 30
        status = "running"
        while time.time() < deadline:
            row = self.store.get_terminal(terminal_id)
            status = row["status"]
            if status != "running":
                break
            time.sleep(0.2)
        self.assertEqual(status, "exited")
        read = terminal.read(self.plane, {"terminalId": terminal_id})
        self.assertIn("bg-done", read["data"]["output"])

    def test_kill_stops_a_running_background_command(self):
        long_running = "Start-Sleep -Seconds 60" \
            if terminal.resolve_shell()[0] in ("pwsh", "powershell") else "sleep 60"
        outcome = terminal.start_background(self.plane, {"command": long_running})
        terminal_id = outcome["data"]["terminalId"]
        time.sleep(0.4)
        killed = terminal.kill(self.plane, {"terminalId": terminal_id})
        self.assertTrue(killed["data"]["killable"])
        self.assertEqual(self.store.get_terminal(terminal_id)["status"], "killed")
        deadline = time.time() + 10
        while terminal._pid_alive(int(outcome["data"]["pid"])) \
                and time.time() < deadline:
            time.sleep(0.2)
        self.assertFalse(terminal._pid_alive(int(outcome["data"]["pid"])),
                         "kill 之后那个 PID 不应该还活着")

    def test_kill_refuses_a_foreign_process_and_an_exited_one(self):
        """进程不是这个控制面起的（重启过）时**不杀**。

        去杀一个我们并不拥有的 PID 是危险的：PID 会被复用，那样杀掉的可能是
        别的程序。这条用例钉的是"如实说停不了"，而不是"尽力而为"。
        """
        row = self.store.record_terminal(terminal_id=terminal.new_id(),
                                         command="x", background=True,
                                         status="running", pid=os.getpid() + 999999)
        outcome = terminal.kill(self.plane, {"terminalId": row["id"]})
        self.assertFalse(outcome["data"]["killable"])
        self.assertEqual(self.store.get_terminal(row["id"])["status"], "running")

        done = self.store.record_terminal(terminal_id=terminal.new_id(),
                                          command="x", status="exited",
                                          exit_code=0)
        outcome = terminal.kill(self.plane, {"terminalId": done["id"]})
        self.assertIn("没有可停的进程", outcome["summary"])


class TerminalOutputTestCase(unittest.TestCase):

    def test_model_view_keeps_head_and_tail(self):
        text = "HEAD" + "x" * 20000 + "TAIL"
        view = terminal.model_view(text)
        self.assertTrue(view.startswith("HEAD"))
        self.assertTrue(view.endswith("TAIL"))
        self.assertIn("中间省略", view)
        self.assertLess(len(view), 5000)

    def test_short_output_is_untouched(self):
        self.assertEqual(terminal.model_view("abc"), "abc")

    def test_decode_survives_a_bad_byte(self):
        """有乱码的真实输出，比一句"解码失败"有用。"""
        self.assertIn("ok", terminal._decode(b"ok\xff\xfe"))

    def test_data_dir_follows_the_database(self):
        directory = tempfile.mkdtemp(prefix="term-")
        store = Store(os.path.join(directory, "pentest.db"))

        class Fake:
            pass

        plane = Fake()
        plane.store = store
        self.assertEqual(os.path.dirname(terminal.data_dir(plane)),
                         os.path.dirname(os.path.abspath(store.path)))
        store.close()


# ------------------------------------------------------------------ 循环

class ReviewLoopTestCase(PlaneCase):
    """审查在**对话循环**里的行为。这一段是"能不能执行"的真正判定点。"""

    def setUp(self):
        super().setUp()
        self.target = os.path.join(self.directory.name, "landed.txt")
        self.command = self.shell_command(self.target, "landed")

    def model(self):
        state = {"n": 0}

        def responder(_record):
            state["n"] += 1
            if state["n"] == 1:
                return _reply_tool("run_command",
                                   {"command": self.command, "timeoutS": 30})
            return _reply_text("命令已经跑过了。")

        model = FakeModel(responder)
        self.addCleanup(model.stop)
        return model

    def decide_with(self, status):
        """把 `approval.wait` 换成"立刻给出这个终态"。"""
        def fake_wait(plane, review_id, **kwargs):
            row = plane.store.get_approval(review_id)
            if status == "approved":
                row = plane.store.decide_approval(review_id, approve=True,
                                                  decided_by="tester") or row
            elif status == "denied":
                row = plane.store.decide_approval(review_id, approve=False,
                                                  decided_by="tester",
                                                  note="不批") or row
            else:
                row = plane.store.settle_approval(review_id, "expired",
                                                  "测试置位") or row
            return row["status"], approval.public(row)
        return mock.patch.object(approval, "wait", fake_wait)

    def test_approved_review_runs_the_command(self):
        with self.decide_with("approved"):
            events, done, _last = self.run_turn(self.model())
        self.assertTrue(done["ok"], done)
        self.assertTrue(os.path.exists(self.target),
                        "批准之后命令必须真的执行")
        review_events = [e for e in events if e.get("type") == "review"]
        self.assertEqual([e["phase"] for e in review_events],
                         ["pending", "settled"])
        self.assertTrue(review_events[0]["review"]["pending"])
        self.assertEqual(len(self.store.list_terminals()), 1)
        entry = [t for t in done["trace"] if t["tool"] == "run_command"][0]
        self.assertEqual(entry["reviewId"], review_events[0]["review"]["id"])
        self.assertEqual(entry["reviewStatus"], "approved")

    def test_the_command_has_not_run_when_the_review_is_raised(self):
        """**这一条是整组用例里最要紧的。**

        只断言"审查单建了"的话，命令早就跑完了也照样全绿。所以在 pending 那一刻
        去问文件系统 —— 它必须还不存在。
        """
        seen = {}

        def fake_wait(plane, review_id, **kwargs):
            seen["existed"] = os.path.exists(self.target)
            row = plane.store.decide_approval(review_id, approve=True,
                                              decided_by="tester") or \
                plane.store.get_approval(review_id)
            return row["status"], approval.public(row)

        with mock.patch.object(approval, "wait", fake_wait):
            _events, done, _last = self.run_turn(self.model())
        self.assertTrue(done["ok"])
        self.assertIn("existed", seen)
        self.assertFalse(seen["existed"], "审查必须在执行**之前**")
        self.assertTrue(os.path.exists(self.target))

    def test_denied_review_never_runs_the_command(self):
        with self.decide_with("denied"):
            events, done, _last = self.run_turn(self.model())
        self.assertFalse(os.path.exists(self.target),
                         "拒绝之后命令**不能**执行")
        self.assertEqual(self.store.list_terminals(), [])
        entry = [t for t in done["trace"] if t["tool"] == "run_command"][0]
        self.assertFalse(entry["ok"])
        self.assertIn("拒绝", entry["summary"])
        self.assertEqual(entry["reviewStatus"], "denied")

    def test_expired_review_never_runs_the_command(self):
        """超时按不放行处理。fail-closed 是这一处的全部意义。"""
        with self.decide_with("expired"):
            _events, done, _last = self.run_turn(self.model())
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(self.store.list_terminals(), [])
        entry = [t for t in done["trace"] if t["tool"] == "run_command"][0]
        self.assertIn("没有执行", entry["summary"])

    def test_off_mode_does_not_raise_a_review(self):
        self.set_mode("off")
        events, done, _last = self.run_turn(self.model())
        self.assertEqual([e for e in events if e.get("type") == "review"], [])
        self.assertTrue(os.path.exists(self.target))
        self.assertEqual(self.store.list_approvals(), [])
        turn = [e for e in events if e.get("type") == "turn"][0]
        self.assertEqual(turn["reviewMode"], "off")

    def test_turn_event_reports_the_mode(self):
        self.set_mode("strict")
        with self.decide_with("approved"):
            events, _done, _last = self.run_turn(self.model())
        turn = [e for e in events if e.get("type") == "turn"][0]
        self.assertEqual(turn["reviewMode"], "strict")

    def test_prompt_tells_the_model_the_mode(self):
        for mode, needle in (("off", "审查关了"), ("ask", "run_command"),
                             ("strict", "逐次确认")):
            self.set_mode(mode)
            self.assertIn(needle, chat.build_system_prompt(
                ["10.0.0.0/8"], None, "full", "admin", True, None, mode))


class ReviewWithinSessionTestCase(PlaneCase):
    """审查单必须能按**会话**查回来 —— 界面刷新之后靠的就是它。"""

    def test_reviews_are_listed_per_session(self):
        self.store.record_approval(session_id="s-1", tool="run_command",
                                   timeout_s=60)
        self.store.record_approval(session_id="s-2", tool="run_command",
                                   timeout_s=60)
        mine = self.plane.list_reviews({"query": {"sessionId": ["s-1"]},
                                        "body": {}, "params": {}})[1]
        self.assertEqual(len(mine["reviews"]), 1)
        self.assertEqual(mine["reviews"][0]["sessionId"], "s-1")
        self.assertEqual(mine["pendingCount"], 1)
        self.assertEqual(mine["mode"], harness.DEFAULT_MODE)
        # 档位的中文名一起给。界面空态那句「闸照状态是「ask」」读不出来是什么，
        # 而设置页同一个档位写的是「审查任意命令」——同一件事两个词。
        self.assertEqual(mine["modeLabel"], harness.MODE_LABELS[harness.DEFAULT_MODE])
        # 夹过之后的实际上限一并回：界面靠 `len(reviews) >= limit` 判断这一批是不是
        # 被截断了。前端若自己写死一份 100，接口这边改了它不知道。
        self.assertEqual(mine["limit"], 20)

    def test_the_reported_limit_is_the_clamped_one(self):
        """`limit` 回的是夹过之后的值，不是请求里那个数。

        请求 `limit=5000` 时服务端只给 100 条；若把请求值原样回给界面，
        `len(reviews) >= limit` 永远不成立，页头就永远不说自己被截断了。"""
        for asked, expect in ((5000, 100), (0, 1), (7, 7)):
            body = self.plane.list_reviews({"query": {"limit": [str(asked)]},
                                            "body": {}, "params": {}})[1]
            self.assertEqual(body["limit"], expect, "limit=%s 没夹到 %s" % (asked, expect))

    def test_every_mode_has_a_chinese_label_and_unknown_words_pass_through(self):
        """四档一个都不能少；认不出的取值原样回，不编中文。

        `mode_label` 与 `approval.TIER_LABELS` 同一条：词表只解释**自己产出的**
        取值，别的原样印出来——读者至少知道自己看到的不是认得出的词，而不是被
        一个编出来的中文名糊过去。"""
        self.assertEqual(sorted(harness.MODE_LABELS), sorted(harness.MODES))
        for mode in harness.MODES:
            self.assertTrue(harness.MODE_LABELS[mode].strip())
        labels = list(harness.MODE_LABELS.values())
        self.assertEqual(len(labels), len(set(labels)), "两个档位共用一个中文名")
        self.assertEqual(harness.mode_label("shell"), "shell")
        self.assertEqual(harness.mode_label(""), "")
        self.assertEqual(harness.mode_label(" STRICT "), harness.MODE_LABELS["strict"])

    def test_only_pending_can_be_decided_through_the_api(self):
        row = self.store.record_approval(tool="run_command", timeout_s=60)
        ctx = {"params": {"review_id": row["id"]}, "body": {"approve": True},
               "query": {}, "user": {"id": "u-1", "role": "admin"}}
        # **要有人在等**才算可裁决 —— 见下面那一条。这里测的是"第二次裁决报
        # 409"，所以先把等待者摆上。
        with approval.waiting(row["id"]):
            self.assertEqual(self.plane.decide_review(ctx)[1]["review"]["status"],
                             "approved")
        with self.assertRaises(server.ApiError) as caught:
            self.plane.decide_review(ctx)
        self.assertEqual(caught.exception.status, 409)

    def test_a_review_with_nobody_waiting_cannot_be_decided(self):
        """没有等待者时**拒绝裁决**，而不是把 `approved` 记下来。

        记下来的最坏形态：库里写着"已批准"，而那条命令永远不会执行 —— 事后翻
        审计会看到一次"批过的执行"，实际什么都没发生，与使用者以为的正好相反。
        走到这一步说明他点的是一张过期卡片：渲染与点击之间等待者没了
        （控制面重启过，或发起它的那一轮已经收了）。
        """
        row = self.store.record_approval(tool="run_command", timeout_s=60)
        ctx = {"params": {"review_id": row["id"]}, "body": {"approve": True},
               "query": {}, "user": {"id": "u-1", "role": "admin"}}
        with self.assertRaises(server.ApiError) as caught:
            self.plane.decide_review(ctx)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("等待者", caught.exception.message)
        self.assertEqual("pending", self.store.get_approval(row["id"])["status"],
                         "被拒的裁决不许改库里的状态")

    def test_missing_review_is_404_and_missing_flag_is_400(self):
        with self.assertRaises(server.ApiError) as caught:
            self.plane.decide_review({"params": {"review_id": "R-nope"},
                                      "body": {"approve": True}, "query": {},
                                      "user": None})
        self.assertEqual(caught.exception.status, 404)
        row = self.store.record_approval(tool="run_command", timeout_s=60)
        with self.assertRaises(server.ApiError) as caught:
            self.plane.decide_review({"params": {"review_id": row["id"]},
                                      "body": {}, "query": {}, "user": None})
        self.assertEqual(caught.exception.status, 400)


class ModeSettingTestCase(PlaneCase):

    def test_unknown_mode_is_refused_not_silently_defaulted(self):
        """写路径必须明确拒绝。

        静默回退的症状是"点了开关但它没生效"，而这一处最不能出现那个形态：
        使用者会以为自己已经打开了审查。
        """
        with self.assertRaises(server.ApiError) as caught:
            self.plane.set_review_mode({"body": {"mode": "sometimes"},
                                        "query": {}, "params": {},
                                        "user": {"role": "admin"}})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.store.get_setting(harness.SETTING_KEY, "缺省"), "缺省")

    def test_setting_round_trips(self):
        status, payload = self.plane.set_review_mode(
            {"body": {"mode": "strict"}, "query": {}, "params": {},
             "user": {"role": "admin"}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["review"]["mode"], "strict")
        self.assertEqual(self.plane._review_mode(), "strict")

    def test_catalog_marks_which_tools_are_reviewed(self):
        catalog = harness.catalog("ask")
        by_id = {item["id"]: item for item in catalog["capabilities"]}
        self.assertEqual(by_id["terminal"]["reviewed"], ["run_command"])
        self.assertEqual(by_id["subagent"]["reviewed"], [])
        self.assertEqual(catalog["review"]["reviewedNow"], ["run_command"])
        strict = harness.catalog("strict")
        # 侵入式那一批**不属于任何一条能力行**（它们是权限档那一侧的东西），
        # 所以"现在开着几道闸"这个问题的答案必须在 review 块里给全，
        # 不能只靠逐条能力的 reviewed 拼出来。
        self.assertIn("start_exploit", strict["review"]["reviewedNow"])
        self.assertIn("run_command", strict["review"]["reviewedNow"])
        self.assertEqual(harness.catalog("off")["review"]["reviewedNow"], [])

    def test_reviewed_tools_tracks_the_mode(self):
        self.assertEqual(harness.reviewed_tools("ask"), ["run_command"])
        self.assertEqual(harness.reviewed_tools("off"), [])
        self.assertEqual(harness.reviewed_tools("nonsense"),
                         harness.reviewed_tools("ask"))


# ------------------------------------------------------------------ 老库

class SchemaUpgradeTestCase(unittest.TestCase):
    """旧库打开就要能用：两张新表由 `CREATE TABLE IF NOT EXISTS` 直接补齐。"""

    def test_v17_database_gains_the_new_tables(self):
        directory = tempfile.mkdtemp(prefix="v17-")
        path = os.path.join(directory, "pentest.db")
        first = Store(path)
        first.conn.execute("DROP TABLE approvals")
        first.conn.execute("DROP TABLE terminals")
        first.conn.execute("PRAGMA user_version=17")
        first.conn.commit()
        first.close()
        again = Store(path)                  # 这一步就是"旧程序建的库，用新程序打开"
        tables = {row["name"] for row in again.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("approvals", tables)
        self.assertIn("terminals", tables)
        again.record_approval(tool="run_command", timeout_s=30)
        again.close()

    def test_a_newer_database_is_refused(self):
        directory = tempfile.mkdtemp(prefix="v99-")
        path = os.path.join(directory, "pentest.db")
        store = Store(path)
        store.conn.execute("PRAGMA user_version=99")
        store.conn.commit()
        store.close()
        with self.assertRaises(RuntimeError):
            Store(path)


# ------------------------------------------------------------------ 卡片接线

class ReviewCardContractTestCase(unittest.TestCase):
    """审查卡的四个端点必须同时存在：SSE 事件名、JS 分支、CSS 样式、服务端路由。

    **只改一处而漏掉另一处的症状全都不报错**，这是这一组用例存在的全部理由：

      * 少 CSS   → 按钮看着像一行普通文字（点得动，但没人知道那是个按钮）
      * 少路由   → 点下去 404，而卡片上写着"批准并执行"
      * 少 JS    → 界面根本不出这张卡，用户只看到这一轮停了
      * 事件名改了 → 前端当未知事件丢掉，与"少 JS"同一个症状

    四者读的是同一份文件树，所以它们是能对上的；对不上就是有人漏了。
    """

    def _read(self, *parts):
        with open(os.path.join(ROOT, *parts), "r", encoding="utf-8") as handle:
            return handle.read()

    def test_server_emits_the_event_the_client_handles(self):
        chat_js = self._read("web", "chat.js")
        chat_py = self._read("pentestd", "chat.py")
        self.assertIn("case 'review':", chat_js)
        self.assertIn('"type": "review"', chat_py)

    def test_card_classes_are_both_emitted_and_styled(self):
        chat_js = self._read("web", "chat.js")
        css = self._read("web", "style.css")
        for name in ("chat-review", "chat-review-card", "chat-review-head",
                     "chat-review-title", "chat-review-tool",
                     "chat-review-headline", "chat-review-reasons",
                     "chat-review-wait", "chat-review-actions",
                     "chat-review-note", "chat-review-primary",
                     "chat-review-sending", "chat-review-done"):
            self.assertIn(name, chat_js, "chat.js 不再产出这个类名：%s" % name)
            self.assertIn("." + name, css, "style.css 里没有这个类名的样式：%s" % name)

    def test_primary_button_only_uses_tokens_defined_in_every_theme(self):
        """批准键的配色取 `--accent-dim` + `--on-accent-deep` 这一对。

        `--accent` / `--on-accent` 在绝大部分"强调色替换"主题块里没有重新定义，
        取它们会让换一个强调色就出现浅底浅字 —— 而那个组合恰好只在换过色的主题里
        才难看，默认主题上看不出来。这一条钉的是"换色之后仍然成对"。
        """
        css = self._read("web", "style.css")
        block = css.split(".chat-review-primary {")[1].split("}")[0]
        self.assertIn("var(--accent-dim)", block)
        self.assertIn("var(--on-accent-deep)", block)
        self.assertNotIn("var(--on-accent)", block)

    def test_the_client_posts_to_a_route_that_exists(self):
        chat_js = self._read("web", "chat.js")
        server_py = self._read("pentestd", "server.py")
        self.assertIn("/reviews/${encodeURIComponent(reviewId)}", chat_js)
        self.assertIn('"/api/v1/reviews/{review_id}"', server_py)
        self.assertIn('"/api/v1/reviews"', server_py)

    def test_pending_card_says_when_it_expires_and_that_a_refresh_is_safe(self):
        """卡片上必须写清超时后果，而且**不许**再写"刷新会作废"。

        少第一句的表现是"用户看着一张卡片发呆，以为它会一直等他"，而超时之后
        命令没跑、他也不知道是自己错过了。

        第二句原来正是反的（"离开这一页也会作废"）。断开只意味着**看的人**走了，
        等它的那个轮次还在服务端跑着，刷新回来照样能批 —— 说错方向的代价比不说
        还大：它会让人做出"不敢刷新"这种没必要的迁就。
        """
        chat_js = self._read("web", "chat.js")
        card = chat_js.split("function reviewCard(")[1].split("\n  }")[0]
        self.assertIn("secondsLeft", card)
        self.assertIn("不执行", card)
        self.assertIn("还会在", card, "要说清刷新不会弄丢它")
        self.assertNotIn("离开这一页", card, "那句是反的，不许回来")


if __name__ == "__main__":
    unittest.main(verbosity=2)
