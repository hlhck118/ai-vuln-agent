# -*- coding: utf-8 -*-
"""DSH 运行时通道的审查闸门：`POST /api/v1/dsh/approval`。

## 这一份为什么走真实 HTTP

要验的不是"函数返回了什么"，而是**人能不能看见那张卡片、点下去那条调用会不会
真的继续**。链路上有三个进程角色：桥（没有 Cookie，只有控制台令牌）、控制面
（等待者集合在自己的进程里）、界面（按会话去 `/api/v1/reviews` 取卡片）。把它们
拆成 mock 之后，"卡片到底会不会出现"这件事就没人测了。

所以每条用例都从**桥的视角**发请求，并且都去 `/api/v1/reviews` 上把那张单子当成
界面会看到的样子取回来 —— 这正是"审查真的接上了"与"接口 200 但没人能点"的区别。

## 口径

本通道到期无人裁决按**放行**继续（用户定的：无人裁决时直接放行），而默认通道是
过期即作废。两条用例都要有：只测放行的话，把默认通道也改成放行不会有人发现。
"""
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import auth, harness, approval                    # noqa: E402
from tests import support                                       # noqa: E402


class DshApprovalRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("dsh-approval.db")
        self.httpd, self.client = support.start(self.store)
        self.base = self.client.base
        self.data_dir = os.path.dirname(os.path.abspath(self.store.path))
        self.token = auth.read_console_token(auth.console_token_path(self.data_dir))
        self.assertTrue(self.token, "控制面启动时应当已经确保令牌存在（桥要用）")

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    # ------------------------------------------------------------ 工具
    def ask(self, tool="run_command", arguments=None, session_id="sess-1",
            token=None):
        """从**桥那边**发一次"要不要人先看一眼"的询问。"""
        payload = json.dumps({"tool": tool,
                              "arguments": arguments if arguments is not None else {"command": "whoami"},
                              "session": {"sessionId": session_id}}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token != "":
            headers["Authorization"] = "Bearer " + (token or self.token)
        request = urllib.request.Request(self.base + "/api/v1/dsh/approval",
                                         data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, (json.loads(raw.decode("utf-8")) if raw else {})

    def in_background(self, **kwargs):
        """把询问放到后台线程里：它会**阻塞**到裁决或到期，而界面要走同一台服务。"""
        box = {}

        def run():
            try:
                box["result"] = self.ask(**kwargs)
            except Exception as exc:                   # noqa: BLE001
                box["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker, box

    def pending_cards(self, session_id="sess-1"):
        status, payload = self.client.get(
            "/api/v1/reviews?status=pending&sessionId=%s" % session_id)
        self.assertEqual(status, 200, payload)
        return payload.get("reviews") or []

    def wait_for_card(self, session_id="sess-1", timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            cards = self.pending_cards(session_id)
            if cards:
                return cards[0]
            time.sleep(0.05)
        self.fail("界面上始终没有出现审查卡片（桥的询问没有被当成等待者）")

    def decide(self, review_id, approve):
        status, payload = self.client.post("/api/v1/reviews/%s" % review_id,
                                           {"approve": approve})
        self.assertEqual(status, 200, payload)
        return payload

    # ------------------------------------------------------------ 用例
    def test_a_needing_review_tool_shows_a_card_and_continues_after_approval(self):
        """链路的最短闭环：卡片出现 → 批准 → 那条调用继续。

        `awaiting` 必须是 True：它为 False 时界面**不画按钮**（那正是桥自己开单
        会踩的坑 —— 等待者在桥进程里，控制面看不到）。
        """
        worker, box = self.in_background()
        card = self.wait_for_card()
        self.assertEqual(card["tool"], "run_command")
        self.assertEqual(card["sessionId"], "sess-1")
        self.assertTrue(card.get("awaiting"), "等待者不在控制面进程里：界面不会画按钮")
        self.decide(card["id"], True)
        worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        status, result = box["result"]
        self.assertEqual(status, 200)
        self.assertTrue(result["required"])
        self.assertTrue(result["allowed"])
        self.assertEqual(result["review"]["status"], "approved")

    def test_denial_stops_the_call_and_returns_a_transcript(self):
        worker, box = self.in_background(arguments={"command": "rm -rf /"})
        card = self.wait_for_card()
        self.decide(card["id"], False)
        worker.join(timeout=20)
        status, result = box["result"]
        self.assertEqual(status, 200)
        self.assertTrue(result["required"])
        self.assertFalse(result["allowed"])
        self.assertTrue(result["reason"])
        # 不放行时要有一句能转述给模型的话，否则模型只会看到"工具失败了"
        self.assertIsNotNone(result["outcome"])
        self.assertFalse(result["outcome"].get("ok", True))

    def test_a_tool_that_needs_no_review_returns_at_once(self):
        status, result = self.ask(tool="list_jobs", arguments={})
        self.assertEqual(status, 200)
        self.assertFalse(result["required"])
        self.assertTrue(result["allowed"])
        self.assertEqual(self.pending_cards(), [])     # 没开出一张没人要看的单子

    def test_expiry_continues_and_says_so_on_the_ticket(self):
        """到期无人裁决 → 放行，**但单子上写明为什么**。

        不写这一句的话，事后翻审计会看到一条"批过的执行"，而实际上没人看过它。
        """
        with mock.patch.object(harness, "DEFAULT_TIMEOUT_S", 1), \
                mock.patch.object(harness, "TIMEOUT_MIN_S", 1):
            status, result = self.ask(tool="run_command", arguments={"command": "id"})
        self.assertEqual(status, 200)
        self.assertTrue(result["required"])
        self.assertTrue(result["allowed"], "本通道的口径是无人裁决即放行")
        self.assertIn("无人裁决", result["reason"])
        self.assertEqual(result["review"]["status"], "expired")
        self.assertIn("无人裁决", result["review"]["note"])

    def test_the_session_is_required_and_enforced(self):
        """审查单挂不上对话 = 界面上出现一张"不知道属于谁"的卡片，而它点下去会
        执行命令。所以缺 sessionId 直接 400，不猜。"""
        status, payload = self.ask(session_id="")
        self.assertEqual(status, 400, payload)

    def test_the_bridge_must_hold_the_console_token(self):
        status, payload = self.ask(token="ptc_wrong")
        self.assertEqual(status, 401, payload)

    def test_another_wrong_token_still_cannot_approve_its_own_request(self):
        """令牌能**问**，但裁决要走 Cookie 会话 —— 桥不能自己批准自己。"""
        worker, box = self.in_background()
        card = self.wait_for_card()
        request = urllib.request.Request(
            self.base + "/api/v1/reviews/%s" % card["id"],
            data=json.dumps({"approve": True}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.token},
            method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 403)   # 没有 CSRF 头
        caught.exception.close()
        # 收尾：让人在界面这一侧真的批掉，别把等待线程留在那里
        self.decide(card["id"], True)
        worker.join(timeout=20)
        self.assertEqual(box["result"][0], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
