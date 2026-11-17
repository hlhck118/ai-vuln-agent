# -*- coding: utf-8 -*-
"""DSH 审查闸门的归属：谁能拿哪条对话去开单。

## 这个洞的形状

`POST /api/v1/dsh/approval` 是桥回控制面问「这条工具调用要不要人先看一眼」的
入口。它当初只验了两件事：调用方登录过、`session.sessionId` 不为空。**没有第三
件**：这条对话是不是调用者的。

后果比"多一张卡片"重，因为它连着三件事：

  1. 卡片按 `sessionId` 过滤后画在**别人**的界面上 —— 管理员会看到一条自己从没
     发起过的「在控制面执行命令」，被伪造的是"谁在要求什么"这条审计事实；
  2. 本通道的口径是**到期无人裁决即放行**（与原生轮相反），所以塞进去的单子十几
     秒后自己就成立了，事后翻审计只看到一条"批过的执行"；
  3. `sessionId` 指向一条不存在的对话时，归属字段回落成**请求体自报**的
     `userId` —— 于是连"挂在不存在的会话上"的单子都能写成任意人的。

判据与 `/reviews` 两个端点同一把（`_require_review_access`）：单子属于它挂的那
条对话，对话属于建它的那个人；判不出来（会话已被删）时只有管理员能碰。桥正常
运行时带的是控制台令牌，落到管理员，行为不变 —— 所以这几条用例同时钉住"收紧"
与"没有误伤桥"。
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

from pentestd import auth, harness                              # noqa: E402
from tests import support                                       # noqa: E402

OPERATOR = ("ut-dsh-operator", "Operator-Pass1!")


class DshApprovalOwnershipTests(unittest.TestCase):
    """三段：别人会话、自己会话、会话不存在。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("dsh-own.db")
        self.httpd, self.admin = support.start(self.store, support.WEB_DIR)
        self.base = self.admin.base
        self.token = auth.read_console_token(
            auth.console_token_path(os.path.dirname(os.path.abspath(self.store.path))))
        self.admin_id = self.store.get_user_by_name(support.ADMIN_USER)["id"]
        status, payload = self.admin.post("/api/v1/users", {
            "username": OPERATOR[0], "password": OPERATOR[1], "role": "operator"})
        self.assertEqual(status, 201, payload)
        self.operator_id = payload["user"]["id"]
        self.operator = support.Client(self.base)
        status, _ = self.operator.login(*OPERATOR)
        self.assertEqual(status, 200)
        self.admin_session = self.store.create_chat_session(
            "管理员那条", self.admin_id)["id"]
        self.operator_session = self.store.create_chat_session(
            "操作员那条", self.operator_id)["id"]

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    # ------------------------------------------------------------ 工具
    def ask(self, client, session_id, tool="list_jobs", arguments=None,
            extra=None):
        """从**桥的视角**问一次。默认挑不需要审查的工具，跑得快。"""
        body = {"tool": tool,
                "arguments": arguments if arguments is not None else {},
                "session": {"sessionId": session_id}}
        body.update(extra or {})
        return client.post("/api/v1/dsh/approval", body)

    def token_ask(self, session_id, tool="list_jobs", arguments=None):
        """桥的真实姿势：控制台令牌，没有 Cookie、没有 CSRF 头。"""
        payload = json.dumps({"tool": tool,
                              "arguments": arguments if arguments is not None else {},
                              "session": {"sessionId": session_id}}).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/v1/dsh/approval", data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.token}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, (json.loads(raw.decode("utf-8")) if raw else {})

    def reviews_in(self, session_id):
        status, payload = self.admin.get(
            "/api/v1/reviews?sessionId=%s" % session_id)
        self.assertEqual(status, 200, payload)
        return payload.get("reviews") or []

    # ------------------------------------------------------------ 用例
    def test_an_operator_cannot_open_a_review_on_someone_elses_session(self):
        """拿别人的会话 id 开单 → 403，且库里**一张都没多出来**。

        "被拒的请求不能留下副作用"在这里尤其要紧：卡片一旦落库，本通道到期就会
        自己放行，删不掉的是一条"执行过"的痕迹。所以断言回到库里数。
        """
        status, payload = self.ask(self.operator, self.admin_session)
        self.assertEqual(status, 403, payload)
        self.assertIn("其他用户", payload["error"])
        self.assertEqual([], self.reviews_in(self.admin_session),
                         "越权请求不能让别人的界面上多出一张卡片")

    def test_the_self_declared_user_id_is_ignored(self):
        """请求体里的 `session.userId` 不再能把单子记到别人头上。

        这是同一个洞的第二半：会话不存在时归属字段会回落到请求体自报的那个 id，
        于是审计里"谁在要求什么"整条是假的。修法是把这条兜底关在 `--no-auth`
        单机形态里；带着登录态的部署一律以登录身份为准。
        """
        status, payload = self.ask(
            self.operator, self.operator_session,
            extra={"session": {"sessionId": self.operator_session,
                               "userId": self.admin_id}})
        self.assertEqual(status, 200, payload)
        # `list_jobs` 不需要审查，不会开单；换个真会开单的工具来验归属字段。
        # 超时压到 1 秒：这一条只关心"单子记在谁名下"，不关心等多久。
        with mock.patch.object(harness, "DEFAULT_TIMEOUT_S", 1), \
                mock.patch.object(harness, "TIMEOUT_MIN_S", 1):
            status, payload = self.ask(
                self.operator, self.operator_session, tool="run_command",
                arguments={"command": "id"},
                extra={"session": {"sessionId": self.operator_session,
                                   "userId": self.admin_id}})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["required"])
        row = self.store.get_approval(payload["review"]["id"])
        self.assertEqual(self.operator_id, row["requestedBy"],
                         "单子必须记成真正发起的人，不是请求体里写的那个人")
        self.assertNotEqual(self.admin_id, row["requestedBy"])

    def test_your_own_session_still_works(self):
        """自己的会话照旧：开得了单，也自己批得了 —— 收紧不能把正常用法收掉。"""
        status, payload = self.ask(self.operator, self.operator_session)
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["required"], "不需要审查的工具不该开单")
        self.assertTrue(payload["allowed"])
        self.assertEqual([], self.reviews_in(self.operator_session))

    def test_the_bridge_token_is_not_affected(self):
        """桥带的是控制台令牌（管理员），它照样能给任何一条对话开单。

        这一条是"没有误伤"的那一半。桥不知道也不该知道会话归属：它就是那个
        正在跑这条对话的东西本身。
        """
        status, payload = self.token_ask(self.admin_session)
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["required"])

    def test_a_session_that_no_longer_exists_is_admin_only(self):
        """会话被删之后，非管理员既开不了、也查不了挂在上面的单子。

        否则"先把会话删了再拿 id 来开单"就是一条固定绕过：归属判据查的那条会话
        已经不在了，而卡片会画在……没人看的地方，到期自己就放行。
        """
        status, payload = self.ask(self.operator, "C-does-not-exist")
        self.assertEqual(status, 403, payload)
        self.assertIn("管理员", payload["error"])
        status, payload = self.token_ask("C-does-not-exist")
        self.assertEqual(status, 200, payload)

    def test_the_whole_loop_still_runs_on_your_own_session(self):
        """最短闭环：自己的会话开单 → 自己批 → 那条调用继续。

        没有这一条，前面几条"全都 403"看起来也能过 —— 那样把附门整个焊死同样绿。
        """
        box = {}

        def run():
            try:
                box["result"] = self.ask(
                    self.operator, self.operator_session, tool="run_command",
                    arguments={"command": "whoami"}, extra={"timeoutS": 30})
            except Exception as exc:                        # noqa: BLE001
                box["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        deadline = time.time() + 15
        card = None
        while time.time() < deadline:
            cards = self.reviews_in(self.operator_session)
            if cards:
                card = cards[0]
                break
            time.sleep(0.05)
        self.assertIsNotNone(card, "自己的会话上也没出现卡片")
        status, payload = self.operator.post(
            "/api/v1/reviews/%s" % card["id"], {"approve": True})
        self.assertEqual(status, 200, payload)
        worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        status, result = box["result"]
        self.assertEqual(status, 200, result)
        self.assertTrue(result["allowed"])
        self.assertEqual("approved", result["review"]["status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
