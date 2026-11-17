# -*- coding: utf-8 -*-
"""`pentestd/dsh_runtime.py` 的离线测试。

## 夹具的来源（不是编的）

夹具帧逐字抄自真机探针 `.v2work/sdk-probe4.jsonl`（成功一轮）与
`sdk-probe2.jsonl`（失败一轮，缺 key）。**外层形状也照实测**：
`Mapping` 收到的是 `session.event` 的 **params**，即
`{"sessionId": …, "event": {"type": …, "seq": …, "time": …, "data": …}}`。
这一层外壳曾经被漏掉 ⇒ 所有事件都落进"认不出"，而单元测试当时是绿的
（因为夹具喂的是内层事件）。所以下面每个 `feed()` 都喂**真实外壳**。

尺寸削减两处，都在本文件里标注：`tools[]` 只留 `name`（实测带完整 schema）；
`stream[]` 截短（被测代码不解析它）。
"""
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pentestd import dsh_runtime as rt                                  # noqa: E402

SESSION = "sdk-probe-4"


# ---------------------------------------------------------------- 真实帧
def event(seq, kind, data):
    """一条 `session.event` 的**完整 params**（含 sessionId 外壳）。"""
    return {"sessionId": SESSION,
            "event": {"type": kind, "seq": seq, "time": 1792090609400 + seq, "data": data}}


def status(value):
    return {"sessionId": SESSION, "status": value}


ASSISTANT_DATA = {
    "turn": 1, "step": 1,
    "message": {
        "role": "assistant",
        "content": [{"type": "reasoning", "text": ""}, {"type": "text", "text": "就绪"}],
        "source": {"kind": "model", "provider": "deepseek-official", "model": "deepseek-flash"},
        "id": "692bf579-efdb-4f64-a4e8-d34d2e4a0ec5",
    },
    "usage": {"inputTokens": 234, "outputTokens": 3, "cacheReadTokens": 5760,
              "cacheWriteTokens": 0, "totalTokens": 5997},
    "stream": [{"type": "text-chunks", "time0": 1792090610268, "index": 1, "dt": [0],
                "texts": ["就", "绪"]}],
}

SDK_TOOLS = ["create_goal", "edit", "exit_plan_mode", "get_goal", "glob", "grep",
             "interrupt_agent", "job_kill", "job_list", "job_output", "list_agents",
             "pwsh", "read", "read_image", "send_message", "skill", "subagent",
             "subagent_fork", "todo_write", "update_goal", "web_fetch", "web_search",
             "workflow", "write"]

SUCCESS_ROUND = [
    event(0, "permission/preset", {"preset": "danger-full-access"}),
    event(1, "sandbox/mode", {"mode": "danger-full-access"}),
    event(2, "approval/policy", {"policy": "never"}),
    event(3, "agent/inbox/spliced", {"target": "next-turn", "start": 0, "inserted": [
        {"content": [{"type": "text", "text": "只回复两个字：就绪"}],
         "source": {"kind": "user"}, "role": "user",
         "id": "714c9b84-49c6-475e-ac76-0bbdfd47ed4a"}]}),
    status("running"),
    event(4, "turn/start", {"turn": 1}),
    event(6, "step/start", {"turn": 1, "step": 1}),
    event(11, "request/header", {"header": {
        "config": {"provider": "deepseek-official", "model": "deepseek-flash",
                   "maxTokens": 256000, "reasoningEffort": "high"},
        "tools": [{"name": n} for n in SDK_TOOLS]}}),
    event(13, "session/title", {"title": "只回复两个字：就绪", "messageSeqs": [8],
                                "source": {"kind": "fallback"}}),
    event(14, "assistant/message", ASSISTANT_DATA),
    event(15, "step/end", {"turn": 1, "step": 1}),
    event(16, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
    status("idle"),
]

FAILURE_ROUND = [
    event(11, "request/header", {"header": {"config": {"model": "deepseek-flash"},
                                            "tools": [{"name": "pwsh"}]}}),
    event(16, "turn/end", {"turn": 1, "reason": {
        "kind": "error",
        "error": {"message": 'llm-deepseek: no API key for provider route '
                             '"deepseek-official"; store DEEPSEEK_API_KEY through the '
                             'credentials service'}}}),
    status("idle"),
]


# ---------------------------------------------------------------- 假进程
class _Stream:
    def __init__(self, lines=()):
        self.q = queue.Queue()
        for line in lines:
            self.q.put(line)

    def feed(self, line):
        self.q.put(line)

    def __iter__(self):
        while True:
            try:
                item = self.q.get(timeout=0.05)
            except queue.Empty:
                continue
            if item is None:
                return
            yield item


class _Sink:
    """stdin 替身；可挂一个 responder，收到请求就回帧（模拟对端）。"""

    def __init__(self, responder=None):
        self.raw = []
        self.responder = responder

    def write(self, data):
        self.raw.append(data)
        if self.responder is not None:
            try:
                req = json.loads(data.decode("utf-8"))
            except Exception:
                return len(data)
            self.responder(req)
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass


class _FakePopen:
    def __init__(self, responder=None):
        self.stdout = _Stream()
        self.stderr = _Stream()
        self.stdin = _Sink(responder)
        self.returncode = None
        self._alive = True

    def poll(self):
        return None if self._alive else (self.returncode or 0)

    def wait(self, timeout=None):
        self._alive = False
        self.returncode = 0
        return 0

    def terminate(self):
        self._alive = False

    def kill(self):
        self._alive = False
        self.returncode = -9


class _ProcStub:
    """只给 `wait_idle` 用的进程存根（它只问 `alive()`）。"""

    def __init__(self, alive=True):
        self._alive = alive

    def alive(self):
        return self._alive


# ---------------------------------------------------------------- Mapping
class MappingTests(unittest.TestCase):
    def test_assistant_message_becomes_delta_with_real_wrapping(self):
        """**这一条钉住这次修的形状错**：喂真实外壳（含 `event` 外层）。

        漏掉外层解包时 `type` 是空 ⇒ 这里拿不到任何事件，而当时的测试仍是绿的。
        """
        m = rt.Mapping()
        out = m.feed(event(14, "assistant/message", ASSISTANT_DATA))
        self.assertEqual([e["type"] for e in out], ["delta"], out)
        self.assertEqual(out[0]["text"], "就绪")
        self.assertEqual(m.last_message["usage"]["totalTokens"], 5997)

    def test_reasoning_is_a_separate_event(self):
        data = {"message": {"content": [{"type": "reasoning", "text": "想一下"},
                                       {"type": "text", "text": "就绪"}]}}
        out = rt.Mapping().feed(event(1, "assistant/message", data))
        self.assertEqual([e["type"] for e in out], ["reasoning", "delta"])
        self.assertEqual(out[0]["text"], "想一下")
        self.assertEqual(out[1]["text"], "就绪")

    def test_turn_end_completed_maps_to_done_with_usage(self):
        m = rt.Mapping()
        m.feed(event(14, "assistant/message", ASSISTANT_DATA))
        out = m.feed(event(16, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}))
        self.assertEqual(out[0]["type"], "done")
        self.assertEqual(out[0]["usage"]["totalTokens"], 5997)
        self.assertEqual(m.last_turn["reason"], "completed")

    def test_turn_end_error_carries_the_real_message(self):
        m = rt.Mapping()
        out = m.feed(FAILURE_ROUND[1])
        self.assertEqual(out[0]["type"], "error")
        self.assertIn("no API key for provider route", out[0]["text"])
        self.assertEqual(m.last_turn["reason"], "error")

    def test_request_header_records_tools_without_inventing_an_event(self):
        """工具清单是事实、不是用户可见事件：记在状态里，并计入**已识别的内部事件**。

        它**不**进 `unknown` —— 见 `ExpectedInternalTests` 里那段说明：把已知的内部
        事件混进"认不出"，会让那条告警变成背景噪音，而它唯一的用处就是"这个运行时
        多了一种事件"。
        """
        m = rt.Mapping()
        out = m.feed(SUCCESS_ROUND[7])
        self.assertEqual(out, [])
        self.assertEqual(len(m.last_tools), 24)
        self.assertIn("skill", m.last_tools)
        self.assertEqual(m.last_config["model"], "deepseek-flash")
        self.assertEqual(m.expected.get("request/header"), 1)
        self.assertNotIn("request/header", m.unknown)

    def test_unknown_events_are_counted_never_faked(self):
        m = rt.Mapping()
        for feed in SUCCESS_ROUND:
            if feed.get("event"):
                m.feed(feed)
        # 实测到的内部事件：已识别，不进 unknown
        self.assertEqual(m.expected.get("permission/preset"), 1)
        self.assertEqual(m.expected.get("agent/inbox/spliced"), 1)
        self.assertNotIn("permission/preset", m.unknown)
        # 认得出的一律不计数
        self.assertNotIn("assistant/message", m.unknown)
        self.assertNotIn("turn/end", m.unknown)
        # 真正没见过的才算"认不出"
        m.feed({"sessionId": SESSION, "event": {"type": "brand/new", "data": {}}})
        self.assertEqual(m.unknown.get("brand/new"), 1)

    def test_event_without_type_is_named(self):
        m = rt.Mapping()
        m.feed({"sessionId": SESSION, "event": {"seq": 1}})
        self.assertEqual(m.unknown.get("(无 type)"), 1)


class TextExtractionTests(unittest.TestCase):
    def test_only_text_blocks_are_body(self):
        self.assertEqual(rt.text_of(ASSISTANT_DATA), "就绪")
        self.assertEqual(rt.reasoning_of(ASSISTANT_DATA), "")

    def test_inner_message_shape_also_works(self):
        inner = {"message": {"content": [{"type": "text", "text": "abc"}]}}
        self.assertEqual(rt.text_of(inner), "abc")


# ---------------------------------------------------------------- DshRuntime
class PromptWireTests(unittest.TestCase):
    def test_prompt_sends_content_blocks(self):
        """**这一条钉住 `content` → `contentBlocks` 的修正。**

        实测：发 `content` 服务端收不到内容；真机那轮成功对话用的是 `contentBlocks`。
        """
        seen = {}

        class Proc:
            def request(self, method, params, timeout=None):
                seen["method"], seen["params"] = method, params
                return {"messageId": "m-1"}

        r = rt.DshRuntime(home="C:/h", root="C:/r", dsh_path="dsh", patch="p")
        r.proc, r.ready = Proc(), True
        out = r.prompt("session-1", "你好")
        self.assertEqual(out["messageId"], "m-1")
        self.assertEqual(seen["method"], "session/prompt")
        self.assertIn("contentBlocks", seen["params"])
        self.assertNotIn("content", seen["params"])
        self.assertEqual(seen["params"]["contentBlocks"],
                         [{"type": "text", "text": "你好"}])

    def test_prompt_before_handshake_is_refused(self):
        r = rt.DshRuntime(home="C:/h", root="C:/r", dsh_path="dsh", patch="p")
        with self.assertRaises(rt.RuntimeUnavailable):
            r.prompt("s", "x")


class WaitIdleTests(unittest.TestCase):
    def _runtime_with(self, round_frames):
        r = rt.DshRuntime(home="C:/h", root="C:/r", dsh_path="dsh", patch="p")
        r.proc = _ProcStub(alive=True)
        for frame in round_frames:
            if "status" in frame:
                r.notifications.put(("session.status", frame))
            else:
                r.notifications.put(("session.event", frame))
        return r

    def test_running_status_does_not_finish_the_round(self):
        r = self._runtime_with(SUCCESS_ROUND)
        out = r.wait_idle(deadline_s=5.0)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["text"], "就绪")
        self.assertEqual(out["usage"]["totalTokens"], 5997)
        self.assertEqual(len(out["tools"]), 24)
        self.assertEqual(out["turn"]["reason"], "completed")

    def test_failure_round_is_ok_false_with_reason(self):
        r = self._runtime_with(FAILURE_ROUND)
        out = r.wait_idle(deadline_s=5.0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["turn"]["reason"], "error")
        self.assertIn("no API key", out["turn"]["error"])
        self.assertEqual([e["type"] for e in out["events"]], ["error"])

    def test_dead_process_does_not_hang(self):
        r = rt.DshRuntime(home="C:/h", root="C:/r", dsh_path="dsh", patch="p")
        r.proc = _ProcStub(alive=False)
        out = r.wait_idle(deadline_s=5.0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["events"], [])


class LaunchShapeTests(unittest.TestCase):
    def test_argv_uses_profile_and_patch_and_never_from_default_profile(self):
        """`--from-default-profile` 只在 profile 还不存在时能给；带它会直接启动失败。"""
        r = rt.DshRuntime(home="C:/h", root="C:/r", dsh_path="dsh.cmd",
                          patch="C:/p/sdk.yml", profile="pentest-platform",
                          bridge_patch="C:/p/bridge.yml")
        argv = r.argv()
        # `.cmd` 在 Windows 上不能直接当 argv[0]（WinError 193），必须交给 cmd /c；
        # 所以这里断言的是“入口还在 argv 里”，而不是 argv[0] 的字面值。
        self.assertIn("dsh.cmd", argv)
        entry = argv.index("dsh.cmd")
        self.assertTrue(entry == 0 or argv[entry - 1] == "/c")
        self.assertEqual(argv[argv.index("--profile") + 1], "pentest-platform")
        self.assertEqual(argv.count("--patch"), 2)
        self.assertNotIn("--from-default-profile", argv)

    def test_env_isolates_home_and_strips_inherited_profile(self):
        with mock.patch.dict(os.environ, {"DSH_PROFILE": "desktop",
                                          "DSH_PROFILE_DIR": "x",
                                          "DSH_SESSION_ID": "s"}, clear=False):
            r = rt.DshRuntime(home="C:/proj/dsh-home", root="C:/r", dsh_path="dsh",
                              patch="p", api_key="sk-1")
            env = r.env()
        self.assertEqual(env["DSH_HOME"], "C:/proj/dsh-home")
        for key in ("DSH_PROFILE", "DSH_PROFILE_DIR", "DSH_SESSION_ID"):
            self.assertNotIn(key, env)
        self.assertEqual(env["DEEPSEEK_API_KEY"], "sk-1")

    def test_env_has_no_key_when_none_given(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "from-parent"}, clear=False):
            r = rt.DshRuntime(home="C:/h", root="C:/r", dsh_path="dsh", patch="p")
            env = r.env()
        # 不传就保持调用方环境里的原样，不去"帮忙"塞一个
        self.assertEqual(env.get("DEEPSEEK_API_KEY"), "from-parent")

    def test_unavailable_reasons_are_specific(self):
        # 注意：构造器会把空的 dsh_path 换成 `locate_dsh()` 的结果（本机找得到），
        # 所以"PATH 上没有 dsh"这一支只能在构造之后手工置空才测得到。
        r = rt.DshRuntime(home="C:/h", root="C:/r", dsh_path="", patch="p")
        r.dsh_path = ""
        self.assertIn("dsh", r.unavailable_reason())
        r2 = rt.DshRuntime(home="C:/h", root="C:/r", dsh_path="dsh",
                           patch=os.path.join(tempfile.gettempdir(), "nope-does-not-exist.yml"))
        self.assertIn("补丁", r2.unavailable_reason())


class VendorTests(unittest.TestCase):
    """项目自带的那一份 DSH：找得到；找不到也**不回头去 PATH 上拿**。

    这组用例钉的是“封装”这个承诺本身。改动前 `locate_dsh()` 是
    `shutil.which("dsh")`，于是“本机装没装桌面版 / 桌面版有没有推送更新”
    会静默地决定平台用哪一份 DSH——同一个仓库在两台机器上行为不同，
    而版本的差异会以“某个工具突然不认参数”的形式从很远的地方冒出来。
    现在入口只认 vendor 里那个 `.js`，`command_for` 负责补 `node`，
    PATH 上的 dsh 一律当不存在。
    """

    def test_locator_returns_the_vendored_entry(self):
        fake = os.path.join(tempfile.gettempdir(), "vendor-dsh", "lib", "bin.js")
        with mock.patch.object(rt, "VENDOR_DSH_BIN", fake), \
                mock.patch.dict(os.environ, {"DSH_CLI": ""}, clear=False), \
                mock.patch.object(rt.os.path, "isfile", lambda p: p == fake):
            self.assertEqual(fake, rt.locate_dsh())

    def test_locator_never_falls_back_to_the_path(self):
        """vendor 里没有 → 结果是“没有”，而不是拿 PATH 上那份顶上。"""
        with mock.patch.object(rt, "VENDOR_DSH_BIN",
                               os.path.join(tempfile.gettempdir(), "nope", "bin.js")), \
                mock.patch.dict(os.environ, {"DSH_CLI": ""}, clear=False), \
                mock.patch.object(rt.os.path, "isfile", lambda p: False), \
                mock.patch.object(rt.shutil, "which",
                                  lambda name: r"R:\desktop\dsh.CMD"):
            self.assertEqual("", rt.locate_dsh())

    def test_command_for_js_prefixes_node(self):
        with mock.patch.object(rt, "node_executable", lambda: r"N:\node.exe"):
            self.assertEqual([r"N:\node.exe", "x/bin.js"], rt.command_for("x/bin.js"))
            self.assertEqual([r"N:\node.exe", "x/B.MJS"], rt.command_for("x/B.MJS"))

    def test_command_for_cmd_goes_through_comspec(self):
        with mock.patch.dict(os.environ, {"COMSPEC": r"C:\W\cmd.exe"}, clear=False):
            self.assertEqual([r"C:\W\cmd.exe", "/c", "x/dsh.cmd"],
                             rt.command_for("x/dsh.cmd"))
            self.assertEqual([r"C:\W\cmd.exe", "/c", "x/dsh.bat"],
                             rt.command_for("x/dsh.bat"))

    def test_command_for_a_plain_executable_is_untouched(self):
        self.assertEqual(["x/dsh.exe"], rt.command_for("x/dsh.exe"))
        self.assertEqual([], rt.command_for(""))

    def test_sdk_patch_is_the_vendored_one(self):
        vendor = tempfile.mkdtemp(prefix="vendor-dsh-")
        self.addCleanup(shutil.rmtree, vendor, True)
        target = os.path.join(vendor, "node_modules", "@deepseek-ai", "dsh-sdk-app",
                              "cordis.patch.yml")
        os.makedirs(os.path.dirname(target))
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("insert: []\n")
        with mock.patch.object(rt, "VENDOR_DSH_DIR", vendor):
            self.assertEqual(target, rt.locate_sdk_patch())
            # 入口传什么都不影响：补丁位置由“装上来的包”决定，不由启动方式决定
            self.assertEqual(target, rt.locate_sdk_patch("R:\\desktop\\dsh.CMD"))
        empty = tempfile.mkdtemp(prefix="vendor-dsh-empty-")
        self.addCleanup(shutil.rmtree, empty, True)
        with mock.patch.object(rt, "VENDOR_DSH_DIR", empty):
            self.assertEqual("", rt.locate_sdk_patch())

    def test_vendor_bin_goes_first_and_path_key_case_is_kept(self):
        """pnpm 在 `vendor/dsh/node_modules/.bin`；PATH 必须前置且不新建重复键。

        Windows 下环境变量名大小写不敏感，但 `dict(os.environ)` 有大小写：
        如果这里写 `env["PATH"]` 而原环境里是 `Path`，子进程会拿到两条。
        """
        vendor = tempfile.mkdtemp(prefix="vendor-dsh-bin-")
        self.addCleanup(shutil.rmtree, vendor, True)
        bin_dir = os.path.join(vendor, "node_modules", ".bin")
        os.makedirs(bin_dir)
        with mock.patch.object(rt, "VENDOR_DSH_DIR", vendor):
            self.assertEqual([bin_dir], rt.vendor_bin_dirs())
            env = {"Path": r"C:\other", "TEMP": "t"}
            out = rt.prepend_vendor_bin(env)
            self.assertIs(out, env)
            self.assertEqual(bin_dir + os.pathsep + r"C:\other", env["Path"])
            self.assertNotIn("PATH", env)
            # 没有 .bin 时不动 env（没装完的 vendor 不该把 PATH 改成一条空串）
            os.rmdir(bin_dir)
            untouched = {"Path": r"C:\other"}
            self.assertEqual(untouched, rt.prepend_vendor_bin(untouched))


class SdkProcessTests(unittest.TestCase):
    def _spawn(self, respond):
        fake = _FakePopen()
        fake.stdin.responder = lambda req: fake.stdout.feed(
            (json.dumps(respond(req)) + "\n").encode("utf-8"))
        return fake

    def test_request_matches_response_by_id(self):
        def respond(req):
            if req["method"] == "initialize":
                return {"jsonrpc": "2.0", "id": req["id"],
                        "result": {"serverInfo": {"name": "deepseek-harness-sdk-runtime",
                                                  "version": "0.0.1"}}}
            return {"jsonrpc": "2.0", "id": req["id"], "result": {"messageId": "m"}}

        fake = self._spawn(respond)
        with mock.patch.object(rt.subprocess, "Popen", lambda *a, **k: fake):
            proc = rt.SdkProcess(["dsh"], cwd="C:/r", env={})
            got = proc.request("initialize", {"cwd": "C:/r"}, timeout=5.0)
            self.assertEqual(got["serverInfo"]["name"], "deepseek-harness-sdk-runtime")
            again = proc.request("session/prompt", {"sessionId": "s"}, timeout=5.0)
            self.assertEqual(again["messageId"], "m")
            self.assertEqual(len(fake.stdin.raw), 2)

    def test_error_response_raises_with_code(self):
        def respond(req):
            return {"jsonrpc": "2.0", "id": req["id"],
                    "error": {"code": -32603, "message": "SDK server is not initialized"}}

        fake = self._spawn(respond)
        with mock.patch.object(rt.subprocess, "Popen", lambda *a, **k: fake):
            proc = rt.SdkProcess(["dsh"], cwd="C:/r", env={})
            with self.assertRaises(rt.RuntimeUnavailable) as ctx:
                proc.request("session/prompt", {}, timeout=5.0)
        self.assertIn("not initialized", str(ctx.exception))
        self.assertIn("-32603", str(ctx.exception))

    def test_request_timeout_is_explicit(self):
        fake = _FakePopen()                     # 对端从不回话
        with mock.patch.object(rt.subprocess, "Popen", lambda *a, **k: fake):
            proc = rt.SdkProcess(["dsh"], cwd="C:/r", env={})
            with self.assertRaises(rt.RuntimeUnavailable) as ctx:
                proc.request("initialize", {}, timeout=0.3)
        self.assertIn("没有应答", str(ctx.exception))

    def test_notifications_reach_the_callback(self):
        """**这一层收到的是线上帧**（带 `method`），不是 `session.event` 的 params。

        两层形状不同，混起来的表现正好是"通知全丢" —— 与这次修的
        `Mapping._map` 漏解包是同一类错。
        """
        seen = []
        fake = _FakePopen()
        wire = {"jsonrpc": "2.0", "method": "session.status",
                "params": {"sessionId": "s-1", "status": "idle"}}
        with mock.patch.object(rt.subprocess, "Popen", lambda *a, **k: fake):
            proc = rt.SdkProcess(["dsh"], cwd="C:/r", env={},
                                 on_notification=lambda m, p: seen.append((m, p)))
            fake.stdout.feed((json.dumps(wire) + "\n").encode("utf-8"))
            for _ in range(40):
                if seen:
                    break
                threading.Event().wait(0.05)
        self.assertEqual(seen[0][0], "session.status")
        self.assertEqual(seen[0][1]["status"], "idle")


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dsh-rt-cred-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_write_update_and_preserve_other_keys(self):
        path = os.path.join(self.dir, rt.CREDENTIALS_FILE)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("OTHER_KEY: keep\nDEEPSEEK_API_KEY: old\n")
        got = rt.ensure_credentials(self.dir, {"DEEPSEEK_API_KEY": "sk-new"})
        with open(got, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("OTHER_KEY: keep", body)
        self.assertIn("DEEPSEEK_API_KEY: sk-new", body)
        self.assertNotIn("old", body)
        self.assertFalse(os.path.exists(got + ".tmp"))

    def test_idempotent(self):
        rt.ensure_credentials(self.dir, {"A": "1"})
        path = rt.ensure_credentials(self.dir, {"A": "1"})
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "A: 1\n")


# ------------------------------------------------- 进程复用（真机上先踩到才补的）
class _Settings:
    """`channel()` 只需要 `get_setting`。"""

    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_setting(self, key, default=""):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value


class ChannelReuseTests(unittest.TestCase):
    """复用契约。

    这里钉的两条都是**真机上先踩到、后补上**的：取样口一帧没收到（复用判定要求
    "进程活着"，于是"还没启动"被当成不可复用，每次调用都换一个新对象），以及
    并发两轮会各起一个进程（同一个原因）。症状都不是报错，而是"挂上去的东西
    静默失效"和"缓存形同不存在"。
    """

    def setUp(self):
        rt._CHANNELS.clear()
        self.store = _Settings({"dsh_profile": "v2sdk"})
        self.home = os.path.join(tempfile.gettempdir(), "dsh-home-reuse")

    def tearDown(self):
        rt._CHANNELS.clear()

    def test_an_unstarted_channel_is_reused(self):
        first = rt.channel(self.store, self.home, profile="v2sdk")
        second = rt.channel(self.store, self.home, profile="v2sdk")
        self.assertIs(first, second)
        self.assertIsNone(first.proc)               # 还没启动，但**仍是同一条**
        self.assertEqual(1, len(rt._CHANNELS))

    def test_open_puts_the_given_cwd_on_the_process_and_on_initialize(self):
        """`cwd` 决定 DSH 自带 shell 的落点：子进程与 `initialize` 必须同一个。

        只喂一边（比如只给 `initialize`）不会报错，症状是"平台以为在会话目录、
        模型却写到了项目根"——项目根里那些 `probe_*.py` 就是这么来的。对话那条
        路传的是本会话工作区（见 `test_dsh_chat` 里的同名钉子）。
        """
        chan = rt.channel(self.store, self.home, profile="v2sdk")
        seen = {}

        class _Proc:
            stderr_tail = []

            def __init__(self, argv, cwd, env, on_notification=None):
                seen["spawn"] = cwd

            def request(self, method, params, timeout=None):
                seen["params"] = dict(params)
                return {"serverInfo": {"name": "stub"}, "protocolVersion": "1"}

            def alive(self):
                return True

        with mock.patch.object(rt, "SdkProcess", _Proc):
            chan.open(cwd="/tmp/ws-x", provider="deepseek-official",
                      model="deepseek-chat")
        self.assertEqual("/tmp/ws-x", seen["spawn"])
        self.assertEqual("/tmp/ws-x", seen["params"]["cwd"])

    def test_the_tap_survives_a_second_lookup(self):
        """真事故：取样口挂在对象 A 上，跑那一轮用的却是对象 B。"""
        first = rt.channel(self.store, self.home, profile="v2sdk")
        first.on_raw = lambda method, params: None
        second = rt.channel(self.store, self.home, profile="v2sdk")
        self.assertIsNotNone(second.on_raw)

    def test_a_dead_process_is_replaced(self):
        first = rt.channel(self.store, self.home, profile="v2sdk")
        first.proc = _ProcStub(alive=False)
        second = rt.channel(self.store, self.home, profile="v2sdk")
        self.assertIsNot(first, second)

    def test_a_live_process_is_reused(self):
        first = rt.channel(self.store, self.home, profile="v2sdk")
        first.proc = _ProcStub(alive=True)
        self.assertIs(first, rt.channel(self.store, self.home, profile="v2sdk"))

    def test_key_separates_model_and_provider(self):
        """`initialize` 把 provider/model 钉在进程上：换了模型却复用，症状是
        "界面选了新模型、回答还是旧模型"，而日志里一切正常。"""
        base = dict(profile="v2sdk", api_key="sk-x")
        a = rt.channel(self.store, self.home, provider="deepseek-official",
                       model="deepseek-chat", **base)
        b = rt.channel(self.store, self.home, provider="deepseek-official",
                       model="deepseek-reasoner", **base)
        self.assertIsNot(a, b)
        self.assertEqual(2, len(rt._CHANNELS))

    def test_key_never_contains_the_api_key(self):
        key = "sk-abcdefghijklmnopqrstuvwxyz012345"
        rt.channel(self.store, self.home, profile="v2sdk", api_key=key)
        joined = " ".join(rt._CHANNELS)
        self.assertNotIn(key, joined)
        self.assertNotIn("sk-", joined)

    def test_close_channels_reports_and_empties(self):
        rt.channel(self.store, self.home, profile="v2sdk")
        self.assertEqual(1, rt.close_channels())
        self.assertEqual(0, len(rt._CHANNELS))


# ------------------------------------------------------------- 会话身份（进程内）
class SessionIdentityTests(unittest.TestCase):
    """DSH 会话的复用边界。

    实测：跨进程拿同一个 id 去 `session/prompt`，服务端回
    `-32603 session "<id>" already exists` —— 新进程会尝试**创建**它，而持久层里有。
    所以身份挂在进程上；这条测试是那个结论的钉子。
    """

    def test_first_call_is_fresh_then_it_continues(self):
        chan = rt.DshRuntime(home="/h", root="/r")
        first = chan.session_for("s1")
        self.assertTrue(first["fresh"])
        self.assertTrue(first["id"].startswith("session-"))
        second = chan.session_for("s1")
        self.assertFalse(second["fresh"])
        self.assertEqual(first["id"], second["id"])

    def test_a_recreated_channel_gets_a_new_id(self):
        """等价于"换了一个进程"：必须**不是**同一条 id，否则服务端会拒掉。"""
        one = rt.DshRuntime(home="/h", root="/r").session_for("s1")["id"]
        two = rt.DshRuntime(home="/h", root="/r").session_for("s1")["id"]
        self.assertNotEqual(one, two)

    def test_two_platform_sessions_stay_separate(self):
        chan = rt.DshRuntime(home="/h", root="/r")
        self.assertNotEqual(chan.session_for("a")["id"], chan.session_for("b")["id"])

    def test_prompt_counts_turns_on_that_session(self):
        chan = rt.DshRuntime(home="/h", root="/r")
        sid = chan.session_for("s1")["id"]
        chan.proc = _StreamProcess()
        chan.ready = True
        chan.prompt(sid, "一")
        chan.prompt(sid, "二")
        self.assertEqual(2, chan.session_for("s1")["turn"])

    def test_remember_and_last_are_diagnostic_only(self):
        store = _Settings()
        self.assertEqual("", rt.last_session_id(store, "s1"))
        rt.remember_session(store, "s1", "session-abc")
        self.assertEqual("session-abc", rt.last_session_id(store, "s1"))
        # 它**不参与复用**：新通道仍然给出自己的新 id（见上一条）。
        self.assertNotEqual("session-abc",
                            rt.DshRuntime(home="/h", root="/r").session_for("s1")["id"])


class _StreamProcess:
    """`prompt` 链路的最小进程存根（只关心帧被写出去、以及轮次计数）。"""

    def __init__(self):
        self.frames = []
        self.stderr_tail = []

    def request(self, method, params, timeout=None):
        self.frames.append((method, params))
        return {"messageId": "m%d" % len(self.frames)}

    def alive(self):
        return True

    def close(self, graceful=5.0):
        pass


# --------------------------------------------- 工具事件（形状按真机帧逐字抄）
def _real_call(name="pwsh", call_id="call_00_BTYeVDPasYnodjVxeGRx2445"):
    """真机 `tool/call` 帧（v2 测形状时捞到的，字段照抄）。"""
    return {"sessionId": "session-feb0336c", "event": {
        "type": "tool/call", "seq": 18, "time": 1792091733644,
        "data": {"turn": 1, "step": 1, "callId": call_id, "name": name,
                 "arguments": "{\"command\": \"python -c \\\"print(7*6)\\\"\"}"}}}


def _real_result(call_id="call_00_BTYeVDPasYnodjVxeGRx2445", text="42\r\n",
                 is_error=False, with_flag=True):
    """真机 `tool/result` 帧：正文在 `data.message.content[].text`，名字**不在**这里。"""
    inner = {"role": "tool", "source": {"kind": "tool", "callId": call_id},
             "toolCallId": call_id, "content": [{"type": "text", "text": text}],
             "id": "fc8be9f1"}
    if with_flag:
        inner["isError"] = is_error
    return {"sessionId": "session-feb0336c", "event": {
        "type": "tool/result", "seq": 19, "time": 1792091734284,
        "data": {"turn": 1, "step": 1, "message": inner}, "surfaceOp": "append"}}


class ToolEventShapeTests(unittest.TestCase):
    """工具行必须带**名字**。

    真事故：第一版按猜的字段名取名字，于是结果那一行 name 为空——界面上会显示
    "执行了一个没有名字的东西"。名字只在 call 那一侧，所以要按 callId 接回来。
    """

    def test_call_event_carries_name_and_arguments(self):
        mapping = rt.Mapping()
        events = mapping.feed(_real_call())
        self.assertEqual("tool", events[0]["type"])
        self.assertEqual("call", events[0]["phase"])
        self.assertEqual("pwsh", events[0]["name"])
        self.assertIn("print(7*6)", events[0]["arguments"])

    def test_result_event_gets_the_name_from_the_call(self):
        mapping = rt.Mapping()
        mapping.feed(_real_call())
        events = mapping.feed(_real_result())
        self.assertEqual("result", events[0]["phase"])
        self.assertEqual("pwsh", events[0]["name"])     # ← 从 call 接回来的
        self.assertEqual("42\r\n", events[0]["text"])   # ← 从块数组里取，不是 json 转储

    def test_result_reports_error_only_when_stated(self):
        mapping = rt.Mapping()
        mapping.feed(_real_call())
        bad = mapping.feed(_real_result(is_error=True))[0]
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["okKnown"])
        unknown = rt.Mapping()
        unknown.feed(_real_call())
        quiet = unknown.feed(_real_result(with_flag=False))[0]
        self.assertTrue(quiet["ok"])            # 缺席时按没报错处理
        self.assertFalse(quiet["okKnown"])      # 但把"这条是我推的"说清楚

    def test_result_without_a_call_has_no_name_but_is_not_a_crash(self):
        events = rt.Mapping().feed(_real_result())
        self.assertEqual("", events[0]["name"])
        self.assertEqual("42\r\n", events[0]["text"])


class ExpectedInternalTests(unittest.TestCase):
    """`unknown` 必须留住信号价值。

    真机第一轮报"12 类认不出"，其中 6 类（`user/message`、`turn/start`…）是 DSH
    自己的内部事件。混在一起报，使用者学会忽略这条告警——于是真正的新事件也被忽略，
    而那是唯一值得被看见的变化。
    """

    def test_measured_internal_kinds_are_expected(self):
        mapping = rt.Mapping()
        for kind in ("user/message", "system/message", "turn/start", "step/start",
                     "step/end", "request/header", "request/context",
                     "agent/inbox/spliced", "approval/policy", "permission/preset",
                     "sandbox/mode", "session/title", "assistant/attempt",
                     "llm/retry", "llm/retry-started"):
            mapping.feed({"event": {"type": kind, "data": {}}})
        self.assertEqual({}, mapping.unknown)
        self.assertEqual(15, sum(mapping.expected.values()))

    def test_a_genuinely_new_kind_lands_in_unknown(self):
        mapping = rt.Mapping()
        mapping.feed({"event": {"type": "brand/new-事件", "data": {}}})
        self.assertEqual({"brand/new-事件": 1}, mapping.unknown)
        self.assertEqual({}, mapping.expected)


CHILD = "session-child-1"


def child_event(seq, kind, data, child=CHILD):
    """一条**子会话**的 `session.event` 完整 params。

    外层只有 `sessionId` 不同 —— 实测（`dsh-sdk-jsonrpc-server` 里那句
    `ctx.on("session/event", (session, event) => …)`）它对每个 session 都发一份，
    同一条管道、同一个外壳。所以这里不能另造一种形状。
    """
    return {"sessionId": child,
            "event": {"type": kind, "seq": seq, "time": 1792090609400 + seq, "data": data}}


def say(text, step=1):
    """一条助手消息（只留被测代码会读的那几项：`message.content[]` 与 `step`）。"""
    return {"turn": 1, "step": step,
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def tool_call(call_id, name, step=1):
    return {"turn": 1, "step": step, "callId": call_id, "name": name,
            "arguments": "{}"}


def tool_result(call_id, text, is_error=False, step=1):
    return {"turn": 1, "step": step,
            "message": {"role": "tool", "toolCallId": call_id,
                        "content": [{"type": "text", "text": text}],
                        "isError": is_error}}


def task(text):
    """子会话被派出去时拿到的第一条用户消息。"""
    return {"message": {"role": "user",
                        "content": [{"type": "text", "text": text}]}}


class SubagentVisibilityTests(unittest.TestCase):
    """DSH 原生 `subagent` 派出去的子会话 → 平台界面上的一行。

    真机事实（`dsh-sdk-jsonrpc-server`）：`session.event` 与 `session.status` 对**每个**
    session 都发一次。所以这里钉的是**分流**，不是"能不能收到"—— 收得到，但混进父轮会
    让父轮提前收尾（子会话先跑完就 idle）、正文串台（子会话的助手消息被拼进父轮回复）。

    行的键名要与 `chat.js` 的 `newSubagentRow()` 逐键一致：实时区块与刷新后的
    `message.subagents` 走同一个渲染器，多一个键不会报错，少一个键会让那一行残缺 ——
    而"少一个键"只在刷新之后才看得出来，流里看起来一切正常。
    """

    ROW_KEYS = {"index", "total", "role", "policy", "toolCount", "state", "steps",
                "durationMs", "summary", "error", "note", "tools"}

    def setUp(self):
        # 父轮已经说完、子会话还在跑时 `iter_turn` 会再等一个窗口（生产 45s）。
        # 测试里把它改小：那些用例要验的是"等不等"，不是"等多久"。
        self._settle = rt.SUBAGENT_SETTLE_S
        rt.SUBAGENT_SETTLE_S = 0.2
        self.addCleanup(setattr, rt, "SUBAGENT_SETTLE_S", self._settle)

    def mapping(self):
        mapping = rt.Mapping()
        mapping.active_session = SESSION
        return mapping

    def runtime(self):
        """不握手的通道：只往 `notifications` 里塞帧，不走子进程。"""
        home = tempfile.mkdtemp(prefix="dsh-rt-")
        self.addCleanup(shutil.rmtree, home, True)
        chan = rt.DshRuntime(home=home, root=os.path.dirname(home),
                             dsh_path="C:/nonexistent/dsh.js")
        chan.active_session = SESSION
        return chan

    # ---------------------------------------------------------------- 分流
    def test_child_text_never_leaks_into_the_parent(self):
        mapping = self.mapping()
        frames = mapping.feed(child_event(1, "assistant/message", say("子会话正文")))
        self.assertEqual([], [f for f in frames if f.get("type") in ("delta", "reasoning")],
                         "子会话正文被当成父轮的正文往外发了")
        self.assertEqual([], mapping.events, "子会话事件进了父轮的事件表")
        self.assertEqual("子会话正文", mapping.children[0]["summary"])

    def test_child_turn_end_does_not_end_the_parent_round(self):
        mapping = self.mapping()
        out = mapping.feed(child_event(1, "turn/end", {"turn": 1,
                                                       "reason": {"kind": "completed"}}))
        self.assertEqual([], out, "子会话的 turn/end 被当成了父轮结束")
        self.assertEqual(None, mapping.last_turn)

    def test_child_request_header_does_not_replace_the_parent_tools(self):
        mapping = self.mapping()
        mapping.feed(event(1, "request/header", {"header": {"tools": [{"name": "parent"}]}}))
        mapping.feed(child_event(2, "request/header",
                                 {"header": {"tools": [{"name": "child"}]}}))
        self.assertEqual(["parent"], mapping.last_tools)

    def test_child_idle_does_not_finish_the_parent_round(self):
        """子会话先跑完就 `idle` —— 那一下**不能**把父轮判成说完。"""
        chan = self.runtime()
        chan.notifications.put(("session.status", {"sessionId": CHILD, "status": "idle"}))
        chan.notifications.put(("session.event", child_event(1, "assistant/message",
                                                             say("子会话正文"))))
        chan.notifications.put(("session.event", event(2, "assistant/message",
                                                       say("父轮正文"))))
        chan.notifications.put(("session.status", {"sessionId": SESSION, "status": "idle"}))
        summary = [e for e in chan.iter_turn(deadline_s=10) if e["type"] == "summary"][0]
        self.assertEqual("父轮正文", summary["text"])
        self.assertEqual(1, len(summary["subagents"]))

    # ---------------------------------------------------------------- 过程
    def test_child_tool_result_becomes_one_chip(self):
        mapping = self.mapping()
        mapping.feed(child_event(1, "tool/call", tool_call("c1", "mcp__platform__run_tool")))
        frames = mapping.feed(child_event(2, "tool/result", tool_result("c1", "已下发任务")))
        self.assertEqual(1, len(frames))
        frame = frames[0]
        self.assertEqual("subagent", frame["type"])
        self.assertEqual("tool", frame["phase"])
        self.assertEqual("mcp__platform__run_tool", frame["name"],
                         "名字要从 tool/call 那一侧接回来（result 里只有 callId）")
        self.assertTrue(frame["ok"])
        row = mapping.children[0]
        self.assertEqual(1, row["toolCount"])
        self.assertEqual("已下发任务", row["tools"][0]["brief"])

    def test_child_tool_error_marks_the_chip(self):
        mapping = self.mapping()
        mapping.feed(child_event(1, "tool/call", tool_call("c1", "bash")))
        frames = mapping.feed(child_event(2, "tool/result",
                                         tool_result("c1", "拒绝", is_error=True)))
        self.assertFalse(frames[0]["ok"])
        self.assertFalse(mapping.children[0]["tools"][0]["ok"])

    def test_first_user_message_becomes_the_objective(self):
        mapping = self.mapping()
        mapping.feed(child_event(1, "user/message", task("查一下 /api/config")))
        mapping.feed(child_event(2, "user/message", task("继续")))
        self.assertEqual("任务：查一下 /api/config", mapping.children[0]["note"])

    def test_assistant_step_becomes_a_step_frame(self):
        mapping = self.mapping()
        frames = mapping.feed(child_event(1, "assistant/message", say("第二步行", step=2)))
        self.assertEqual([{"type": "subagent", "phase": "step", "index": 1,
                           "total": 0, "step": 2}], frames)
        self.assertEqual(2, mapping.children[0]["steps"])

    def test_catalog_becomes_the_task_label(self):
        """真机事实：父会话里每个子会话一条 `subagent/catalog`，`label` 才是任务名。

        子会话自己那条 `user/message` 装的是 DSH 注入的运行时说明（"Current runtime
        context…"）—— 拿它当任务，一行噪声就会挂到子智能体下面。
        """
        mapping = self.mapping()
        frames = mapping.feed(event(1, "subagent/catalog", {
            "version": 0, "childId": CHILD, "mode": "continuable",
            "label": "统计 web 下 .js 数量"}))
        self.assertEqual(1, len(frames))
        self.assertEqual("note", frames[0]["phase"])
        self.assertEqual("任务：统计 web 下 .js 数量", frames[0]["message"])
        self.assertEqual(1, frames[0]["total"], "目录先到，总数这时候就知道了")
        self.assertEqual("任务：统计 web 下 .js 数量", mapping.children[0]["note"])
        self.assertEqual({}, mapping.unknown, "catalog 认识就不能进认不出计数器")

    def test_runtime_context_is_not_mistaken_for_the_task(self):
        mapping = self.mapping()
        mapping.feed(child_event(1, "user/message", task(
            "Current runtime context. This snapshot supersedes earlier ones.")))
        self.assertEqual("", mapping.children[0]["note"])

    def test_catalog_label_beats_the_child_user_message(self):
        mapping = self.mapping()
        mapping.feed(event(1, "subagent/catalog", {"childId": CHILD, "label": "读 README"}))
        mapping.feed(child_event(2, "user/message", task("Current runtime context. …")))
        self.assertEqual("任务：读 README", mapping.children[0]["note"])

    def test_still_running_gets_a_note_at_wrap_up(self):
        """父轮收尾时还在跑的：不改状态，但要写明"之后平台看不到"。"""
        mapping = self.mapping()
        mapping.feed(event(1, "subagent/catalog", {"childId": CHILD, "label": "扫端口"}))
        mapping.subagent_started({"parentSessionId": SESSION, "childSessionId": CHILD})
        self.assertEqual(1, len(mapping.running_children()))
        frames = mapping.settle_notes()
        self.assertEqual(1, len(frames))
        self.assertEqual("note", frames[0]["phase"])
        self.assertEqual("running", mapping.children[0]["state"], "没跑完就不能标成完成")
        self.assertIn("平台这边看不到", mapping.children[0]["note"])

    def test_parent_idle_does_not_drop_a_late_child_finish(self):
        """真机形状：父轮 11s 就说完了，子会话晚一步才收尾（`subagent` 工具可异步返回）。

        收尾判据只认父会话 idle，所以那一下不能直接 break —— 否则那一行永远停在
        "跑动中"（刷新之后也是）。
        """
        chan = self.runtime()
        chan.notifications.put(("session.event", event(1, "assistant/message", say("父轮正文"))))
        chan.notifications.put(("subagent.started", {"parentSessionId": SESSION,
                                                     "childSessionId": CHILD}))
        chan.notifications.put(("session.status", {"sessionId": SESSION, "status": "idle"}))
        chan.notifications.put(("subagent.finished", {
            "provider": "spawn", "agentId": CHILD, "parentSessionId": SESSION,
            "childSessionId": CHILD, "status": "ok", "stopReason": "completed",
            "lastAssistantMessage": [{"type": "text", "text": "数完了：12"}]}))
        summary = [e for e in chan.iter_turn(deadline_s=10) if e["type"] == "summary"][0]
        self.assertEqual("done", summary["subagents"][0]["state"],
                         "父轮 idle 之后到的收尾帧被扔掉了")
        self.assertEqual("数完了：12", summary["subagents"][0]["summary"])

    # ---------------------------------------------------------------- 边界
    def test_started_makes_a_row_and_the_total_stays_unknown(self):
        mapping = self.mapping()
        frames = mapping.subagent_started({"parentSessionId": SESSION, "childSessionId": CHILD})
        self.assertEqual("start", frames[0]["phase"])
        self.assertEqual(1, frames[0]["index"])
        self.assertEqual(0, frames[0]["total"], "started 不知道总数，不能编一个")
        self.assertEqual(0, mapping.children[0]["total"])

    def test_finished_carries_summary_reason_and_duration(self):
        mapping = self.mapping()
        mapping.subagent_started({"parentSessionId": SESSION, "childSessionId": CHILD})
        mapping.feed(child_event(1, "assistant/message", say("看到")))
        frames = mapping.subagent_finished({
            "provider": "subagent", "agentId": CHILD,
            "parentSessionId": SESSION, "childSessionId": CHILD,
            "status": "ok", "stopReason": "completed",
            "lastAssistantMessage": [{"type": "reasoning", "text": "想"},
                                     {"type": "text", "text": "结论：开放注册"}]})
        row = mapping.children[0]
        self.assertEqual("done", row["state"])
        self.assertEqual("结论：开放注册", row["summary"],
                         "结论只看 text 块：把 reasoning 也拼进来就是拿思考当结论")
        self.assertEqual(1, row["total"])
        self.assertEqual("", row["error"])
        self.assertGreaterEqual(row["durationMs"], 0)
        self.assertEqual("done", frames[0]["phase"])
        self.assertTrue(frames[0]["ok"])

    def test_failed_child_carries_the_translated_reason(self):
        mapping = self.mapping()
        mapping.subagent_started({"parentSessionId": SESSION, "childSessionId": CHILD})
        mapping.subagent_finished({"provider": "subagent", "agentId": CHILD,
                                   "parentSessionId": SESSION, "childSessionId": CHILD,
                                   "status": "error", "stopReason": "aborted"})
        row = mapping.children[0]
        self.assertEqual("failed", row["state"])
        self.assertEqual("被父轮中止（subagent）", row["error"])

    def test_finished_without_start_is_still_reported(self):
        """进程外跑的派活只有这一条回执 —— "派出去了却没交代"比"过程不可见"糟得多。"""
        mapping = self.mapping()
        mapping.subagent_finished({"provider": "agent-teams", "agentId": CHILD,
                                   "parentSessionId": SESSION, "childSessionId": CHILD,
                                   "status": "ok", "stopReason": "completed",
                                   "lastAssistantMessage": [{"type": "text", "text": "看完了"}]})
        row = mapping.children[0]
        self.assertEqual("看完了", row["summary"])
        self.assertEqual(0, row["durationMs"], "没见过它开跑，耗时只能写 0（不编）")
        self.assertIn("过程不可见", row["note"])

    def test_rows_are_copies(self):
        mapping = self.mapping()
        mapping.feed(child_event(1, "tool/call", tool_call("c1", "bash")))
        mapping.feed(child_event(2, "tool/result", tool_result("c1", "ok")))
        rows = mapping.rows()
        rows[0]["tools"].append({"label": "改的", "ok": True, "brief": "", "durationMs": 0})
        self.assertEqual(1, len(mapping.children[0]["tools"]), "落库行是引用，后续事件会改它")

    def test_row_keys_match_the_native_loop(self):
        """与原生循环落库的行**同一套键名**（一个渲染器管两条通道）。"""
        from pentestd import chat as chat_module
        native = chat_module._subagent_rows({"spawned": 1, "children": [
            {"index": 1, "role": "侦察", "ok": True, "tools": [{"name": "bash"}]}]})
        mapping = self.mapping()
        mapping.subagent_started({"parentSessionId": SESSION, "childSessionId": CHILD})
        self.assertEqual(set(native[0]), set(mapping.rows()[0]))
        self.assertEqual(self.ROW_KEYS, set(mapping.rows()[0]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
