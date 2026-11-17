# -*- coding: utf-8 -*-
"""桥那一侧的审查闸门（`tools/platform_mcp_server.py`）：**拿不到答复就放行**。

## 为什么这条口径要单独立一份测试

平台的默认通道是"到期无人裁决 → 作废"，而 DSH 运行时通道是"→ 放行"（用户定的：
那种情况下没有人会被停下来问话，工具调用只是在等一个 HTTP 响应）。两者都合理，
但**放行是危险的那个方向**：任何一处写错，症状都是"工具照常执行，而没人看过它"，
并且不会有报错、不会有红字、界面一切正常。

所以这里把每一条 fail-open 的路径都拉出来走一遍：没配地址、没令牌、连不上、
平台 4xx/5xx、返回形状不对。每一条都必须 **allowed=True 且 reason 里说得清为什么**。
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_SPEC = importlib.util.spec_from_file_location(
    "platform_mcp_server",
    os.path.join(ROOT, "tools", "platform_mcp_server.py"))
mcp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mcp)


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ReviewCallTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bridge-gate-")
        self.env = {"PENTEST_HOME": self.dir,
                    "PENTEST_DB": os.path.join(self.dir, "pentest.db"),
                    "PENTEST_CONSOLE": "http://127.0.0.1:8788",
                    "PENTEST_SESSION": "sess-9"}
        self.token_path = os.path.join(self.dir, "console-token.txt")
        with open(self.token_path, "w", encoding="utf-8") as handle:
            handle.write("ptc_test\n")

    def tearDown(self):
        for name in os.listdir(self.dir):
            try:
                os.remove(os.path.join(self.dir, name))
            except OSError:
                pass
        os.rmdir(self.dir)

    def call(self, name="run_command", arguments=None):
        # clear=True：外层进程里可能真的配着 PENTEST_*（这台机器就在跑平台），
        # 不清掉的话"没配地址"那条用例会因为外部环境而测不到。
        with mock.patch.dict(os.environ, self.env, clear=True):
            return mcp.review_call(name, arguments or {"command": "id"})

    # ------------------------------------------------------------ fail-open
    def test_no_console_address_means_run(self):
        del self.env["PENTEST_CONSOLE"]
        verdict = self.call()
        self.assertTrue(verdict["allowed"])
        self.assertFalse(verdict["required"])
        self.assertIn("PENTEST_CONSOLE", verdict["reason"])

    def test_missing_token_means_run(self):
        os.remove(self.token_path)
        verdict = self.call()
        self.assertTrue(verdict["allowed"])
        self.assertIn("令牌", verdict["reason"])

    def test_connection_failure_means_run(self):
        with mock.patch.object(mcp.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("refused")):
            verdict = self.call()
        self.assertTrue(verdict["allowed"])
        self.assertIn("不可达", verdict["reason"])

    def test_a_server_error_means_run(self):
        error = urllib.error.HTTPError("http://x", 500, "boom", None, None)
        with mock.patch.object(mcp.urllib.request, "urlopen", side_effect=error):
            verdict = self.call()
        self.assertTrue(verdict["allowed"])
        self.assertIn("500", verdict["reason"])

    def test_a_shape_we_do_not_understand_means_run(self):
        with mock.patch.object(mcp.urllib.request, "urlopen",
                               return_value=_Response(["not", "an", "object"])):
            verdict = self.call()
        self.assertTrue(verdict["allowed"])

    # ------------------------------------------------------------ 真正的答复
    def test_denial_is_the_only_way_to_stop_the_call(self):
        with mock.patch.object(mcp.urllib.request, "urlopen",
                               return_value=_Response({"required": True,
                                                       "allowed": False,
                                                       "reason": "拒绝了",
                                                       "review": {"id": "R-1"}})):
            verdict = self.call()
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["review"]["id"], "R-1")

    def test_approval_and_expiry_both_continue(self):
        for payload in ({"required": True, "allowed": True, "reason": "",
                         "review": {"id": "R-1", "status": "approved"}},
                        {"required": True, "allowed": True,
                         "reason": "到期无人裁决，放行",
                         "review": {"id": "R-2", "status": "expired"}}):
            with mock.patch.object(mcp.urllib.request, "urlopen",
                                   return_value=_Response(payload)):
                verdict = self.call()
            self.assertTrue(verdict["allowed"])

    # ------------------------------------------------------------ 请求本身
    def test_the_request_carries_the_token_and_the_session(self):
        seen = {}

        def capture(request, timeout=None):
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data.decode("utf-8"))
            seen["url"] = request.full_url
            return _Response({"required": False, "allowed": True})

        with mock.patch.object(mcp.urllib.request, "urlopen", side_effect=capture):
            self.call("run_command")
        self.assertEqual(seen["url"], "http://127.0.0.1:8788/api/v1/dsh/approval")
        self.assertEqual(seen["auth"], "Bearer ptc_test")
        self.assertEqual(seen["body"]["tool"], "run_command")
        # 卡片要挂到那条对话上，否则界面不知道把它画在谁上面
        self.assertEqual(seen["body"]["session"], {"sessionId": "sess-9"})

    # ------------------------------------------------------------ MCP 结果形状
    def test_gate_turns_a_denial_into_a_tool_result_not_an_exception(self):
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(mcp.urllib.request, "urlopen",
                                  return_value=_Response({"required": True,
                                                          "allowed": False,
                                                          "reason": "不批",
                                                          "review": {"id": "R-1"}})):
            blocked = mcp.review_gate("run_command", {"command": "id"})
        self.assertIsNotNone(blocked)
        self.assertTrue(blocked["isError"])
        body = json.loads(blocked["text"])
        self.assertFalse(body["ok"])
        self.assertIn("不批", body["summary"])
        self.assertTrue(body["data"]["reviewed"])

    def test_gate_passes_through_when_allowed(self):
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(mcp.urllib.request, "urlopen",
                                  return_value=_Response({"required": True,
                                                          "allowed": True})):
            self.assertIsNone(mcp.review_gate("run_command", {"command": "id"}))


class PrincipalTests(unittest.TestCase):
    """桥执行工具时**必须**带着这条对话的 id。

    审查单那一侧一直带着它（上面那条用例钉着），执行那一侧曾经不带。而平台的归属
    判据正是 `session["sessionId"]`：拿不到就退回到"`create_job` 按 target 新建"。
    实测的后果是一条对话里 11 次工具调用建出 11 个目标——「漏洞」「资产」两页于是
    每次只显示最后一次调用的收获，使用者看到的是"活被拆散到了十几个目标里"。

    这条用例站在两处不一致的**中间**：只要 `principal()` 里少了会话，执行归属就
    与审查卡片的归属分叉，而这两处分叉时没有任何报错。
    """

    def setUp(self):
        self.env = {"PENTEST_HOME": os.path.dirname(os.path.abspath(
            os.path.join(ROOT, "data", "x"))), "PENTEST_SESSION": "sess-9"}

    def test_the_execution_carries_the_conversation(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual("sess-9", mcp.principal().get("sessionId"))

    def test_no_conversation_means_no_session_key(self):
        """没有 `PENTEST_SESSION`（不是按会话生成的补丁）→ **不给**这个键。

        不能给空串：`session_engagement` 只判"有没有"，空串与"没有这个键"在它那里
        是同一条路；但在数据上两者不是一回事——留一个空键会让"这条调用不在对话里"
        看起来像"在一条没有 id 的对话里"。读的人（审计、界面、下一轮排障）只有
        这个键的有无这一个信号。
        """
        del self.env["PENTEST_SESSION"]
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertNotIn("sessionId", mcp.principal())

    def test_the_tool_execution_carries_the_session(self):
        """执行那一步必须带着会话——它是"写进哪个目标"的唯一依据。

        审查单带着会话（上一条用例）而执行不带，正是"一次调用建一个目标"的形状：
        桥知道这条对话是谁，`chat.execute_tool` 却不知道，于是
        `chat.session_engagement` 拿不到会话、返回空串，`create_job` 就按 target
        新建一个目标。实测一条对话 11 次调用建了 11 个目标。

        所以这里从 `call_tool` 一路看到 `execute_tool` 的 `session` 实参，而不是只
        断言 `principal()` 的返回值——中间那一句漏传同样会让归属散掉，而且不会有
        任何报错。
        """
        seen = {}

        def execute(plane, name, arguments, policy_id=None, role="",
                    auth_required=False, session=None):
            seen["session"] = session
            seen["name"] = name
            return {"ok": True, "summary": "已下发", "data": {}}

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.dict(mcp.STATE, {"plane": None}), \
                mock.patch.object(mcp, "review_gate", return_value=None), \
                mock.patch.object(mcp, "make_plane", return_value=object()), \
                mock.patch("pentestd.chat.execute_tool", side_effect=execute):
            result = mcp.call_tool("start_probe", {"target": "10.0.0.9"})
        self.assertFalse(result["isError"], result)
        self.assertEqual("start_probe", seen.get("name"))
        self.assertEqual("sess-9", (seen.get("session") or {}).get("sessionId"))
        self.assertEqual("dsh-bridge", (seen.get("session") or {}).get("source"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
