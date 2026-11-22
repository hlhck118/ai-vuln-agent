# -*- coding: utf-8 -*-
"""**一个写入动作**的端到端判据：真 JSON-RPC 帧 → 真 MCP 服务端 → 平台自己的闸门。

不 mock 任何一层：子进程是 `tools/platform_mcp_server.py` 本身，"调下去"走的是
`chat.execute_tool`。所以这一份给出的证据是两件事同时成立：

* 模型**看得见**那个动作（`tools/list` 里有它，且标着 `write`）；
* 它**调得动**，但越界仍被平台拒 —— 放行一个动作不等于把闸门搬走。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import dsh_bridge as br                                # noqa: E402
from pentestd import policy                                          # noqa: E402
from pentestd.store import Store                                     # noqa: E402

SERVER = os.path.join(ROOT, "tools", "platform_mcp_server.py")


class BridgeWriteTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bridge-write-")
        self.db = os.path.join(self.dir, "pentest.db")
        store = Store(self.db)
        store.set_setting("dsh_bridge_tier", "readonly")
        store.close()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    # ------------------------------------------------------------ 工具
    def grant(self, *names):
        store = Store(self.db)
        try:
            return br.set_write_actions(store, list(names))
        finally:
            store.close()

    def call(self, frames):
        env = dict(os.environ)
        env["PENTEST_HOME"] = ROOT
        env["PENTEST_DB"] = self.db
        # 父进程按 UTF-8 解 stderr（下面的 .decode("utf-8")），所以子进程也得按 UTF-8
        # 写 —— 否则在 GBK 控制台上中文提示会变成一串替换符，断言就变成在测代码页。
        env["PYTHONIOENCODING"] = "utf-8:replace"
        env.pop("PENTEST_TIER", None)      # 让服务端现读库（产品路径）
        env.pop("PENTEST_WRITE", None)
        proc = subprocess.Popen([sys.executable, SERVER], cwd=ROOT, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        payload = "".join(json.dumps(frame, ensure_ascii=False) + "\n" for frame in frames)
        out, err = proc.communicate(payload.encode("utf-8"), timeout=180)
        replies = {}
        for line in out.decode("utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                replies[item.get("id")] = item
        return replies, err.decode("utf-8", "replace")

    def tools_of(self, replies):
        result = replies[2].get("result") or {}      # id 2 = tools/list
        return {tool["name"]: tool for tool in result.get("tools") or []}

    def payload_of(self, replies, ident):
        reply = replies[ident].get("result") or {}
        text = (reply.get("content") or [{}])[0].get("text", "")
        try:
            return json.loads(text)
        except ValueError:
            return {"raw": text}

    FRAMES = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "test", "version": "1"}}},
              {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]

    # ------------------------------------------------------------ 用例
    def test_without_a_grant_the_write_tool_is_invisible(self):
        """默认纯读：模型**看不见**它。看不见比"看得见但被拒"更省，也更难被绕。"""
        replies, _err = self.call(self.FRAMES)
        tools = self.tools_of(replies)
        self.assertNotIn("start_probe", tools)
        # 数字不写死：它随只读档的工具表一起变（加 context_* 三项时这里是第三次漏改）。
        # 钉住"等于只读档能拿到的那些"比钉住"等于 23"更接近这条用例想说的话。
        self.assertEqual(sorted(tools),
                         sorted(policy.allowed_tools("readonly", role="admin")))
        self.assertFalse([name for name, tool in tools.items()
                          if (tool.get("annotations") or {}).get("write")])

    def test_a_granted_action_appears_marked_as_a_write(self):
        self.grant("start_probe")
        replies, _err = self.call(self.FRAMES)
        tools = self.tools_of(replies)
        self.assertIn("start_probe", tools)
        self.assertTrue((tools["start_probe"].get("annotations") or {}).get("write"))
        self.assertIn("写入动作", tools["start_probe"]["description"])
        # 21 → 24：只读档加了 context_* 三项。同样不写死，跟着工具表走。
        self.assertEqual(len(policy.allowed_tools("readonly", role="admin")) + 1,
                         len(tools))
        # 只多这一个：其它写入面不许跟着露头
        for name in ("start_scan", "run_command", "start_exploit", "install_plugin"):
            self.assertNotIn(name, tools)

    def test_the_scope_gate_still_sits_in_the_platform(self):
        """放行**不等于**绕过作用域。

        这是这套设计里最要紧的一条：动作被放行之后，它仍旧走平台的
        `execute_tool`，而探测作用域门禁在那一层 —— 越界目标一样被拒。
        """
        self.grant("start_probe")
        replies, err = self.call(self.FRAMES + [
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "start_probe", "arguments": {"target": "203.0.113.7"}}}])
        body = self.payload_of(replies, 3)
        self.assertFalse(body.get("ok"))
        self.assertIn("作用域", body.get("summary", ""))
        self.assertIn("203.0.113.7", body.get("summary", ""))
        # 抬档留痕：审计要能回答"只读桥为什么有权下发探测"
        self.assertIn("按 probe 档执行", err)

    def test_a_name_outside_the_exposure_set_is_refused(self):
        self.grant("start_probe")
        replies, _err = self.call(self.FRAMES + [
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "run_command", "arguments": {"command": "id"}}}])
        body = self.payload_of(replies, 4)
        self.assertFalse(body.get("ok"))
        self.assertIn("不在当前暴露档位", body.get("summary", ""))

    def test_an_in_scope_call_actually_enqueues_a_job(self):
        """放行的动作**真的落地**：库里多一个任务，`list_jobs` 看得见。

        目标用回环端口：它在默认作用域内，而这个临时库里没有连进来的执行节点，
        所以任务停在 pending —— 既证明了写入真的发生了，又不会去扫任何东西。
        """
        self.grant("start_probe")
        replies, _err = self.call(self.FRAMES + [
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "start_probe",
                        "arguments": {"target": "127.0.0.1:8788", "ports": "8788",
                                      "objective": "桥的写入动作冒烟",
                                      "authorization": "SANDBOX-1"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "list_jobs", "arguments": {}}}])
        body = self.payload_of(replies, 5)
        self.assertTrue(body.get("ok"), body)
        data = body.get("data") or {}
        self.assertTrue(data.get("jobId"))
        self.assertTrue(data.get("engagementId"))
        self.assertEqual("probe", data.get("kind"))
        listing = self.payload_of(replies, 6)
        self.assertIn("1 个作业", listing.get("summary", ""))

    def test_revoking_takes_it_away_again(self):
        """收回之后模型看不见它 —— 这也是"一个动作一个决定"里的那个"收回"。"""
        self.grant("start_probe")
        self.assertIn("start_probe", self.tools_of(self.call(self.FRAMES)[0]))
        self.grant()
        self.assertNotIn("start_probe", self.tools_of(self.call(self.FRAMES)[0]))

    def test_the_env_cannot_widen_what_the_database_forbids(self):
        """补丁里的 `PENTEST_WRITE` 是**生成那一刻**的值；库才是产品路径。

        只靠环境变量的话，"界面显示纯只读、模型却能写"这种分叉没有任何防线。
        """
        env_frames = self.FRAMES
        store = Store(self.db)
        store.set_setting(br.SETTING_WRITE, "")
        store.close()
        env = {"PENTEST_WRITE": "start_probe"}
        proc = subprocess.Popen(
            [sys.executable, SERVER], cwd=ROOT,
            env={**os.environ, "PENTEST_HOME": ROOT, "PENTEST_DB": self.db, **env},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        payload = "".join(json.dumps(frame) + "\n" for frame in env_frames)
        out, _err = proc.communicate(payload.encode("utf-8"), timeout=180)
        tools = {}
        for line in out.decode("utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if item.get("id") == 2:
                    tools = {tool["name"] for tool in
                             (item.get("result") or {}).get("tools") or []}
        self.assertNotIn("start_probe", tools,
                         "库说没放行，环境变量不该把它加回来")


if __name__ == "__main__":
    unittest.main(verbosity=2)
