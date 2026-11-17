# -*- coding: utf-8 -*-
"""规划接口上的 `insecure`：关掉 TLS 校验这件事，谁能要求。

## 针对的缺陷

`POST /api/v1/jobs/plan` 的请求体里有一个 `insecure` 字段，一路传到
`providers.chat_once(..., insecure=True)`，那里的实现是
`ssl._create_unverified_context()`。**同一面旗帜在 `test_model` 里是管理员动作**
（`server.py:3146` 的 `_require_admin` 在前，`:3179` 才读这个字段），而这条路上
曾经一行守卫都没有。

为什么这一处比看起来严重：这条路上带的是**平台自己的模型密钥**。
`_planner_model` 取的是「设置」页签里启用中的那条配置，不是请求方自己的密钥。
任何一个登录用户都能让平台握着一枚真密钥去跟一个不校验证书的端点说话 —— 路径上
的一台中间设备，或者把 `baseUrl` 指向自己机器的攻击者，用一张自签证书就能把这枚
密钥接走，而日志里只有一条「规划失败」。

## 用例守的四条

  1. 普通用户带旗 → 403，而且**请求根本没发出去**（被拒的请求不留副作用）；
  2. 管理员带旗 → 照旧能连（收紧不能把正常用法一起收掉）；
  3. 不带旗时，自签端点连**握手都完不成** —— 这一条钉住「旗确实是那个洞」：
     少了它，前一条 403 也可能只是被别的原因挡下的；
  4. 不带旗的普通规划照常工作（降级路径不受影响）。

自签证书复用 `tests/test_provider_fallback.py` 里固化好的那对 PEM：那是本机没有
openssl 时的替代品，只签给 `localhost` 与 `api.deepseek.com`、只在回环地址上监听、
没有任何保密价值。再复制一份在这里只会多一处需要同步的私钥。
"""
import os
import shutil
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests import support                                      # noqa: E402
from tests.test_provider_fallback import (_TEST_CERT_PEM,       # noqa: E402
                                          _TEST_KEY_PEM)

OPERATOR = ("ut-plan-tls-operator", "Operator-Pass1!")
MODEL_KEY = "sk-PLATFORM-SECRET-123"


class SelfSignedEndpoint:
    """一个**只讲 TLS、证书自签**的模型端点，并记下自己被敲了几次。

    记数是这个夹具的全部价值：它能区分「被 403 挡在前面」与「放行了但模型调用失败」
    —— 后者的响应码同样是 200 + ok:false，只看响应是分不出来的。
    """

    def __init__(self, cert_dir):
        self.requests = 0
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                endpoint.requests += 1
                body = b'{"choices":[{"index":0,"finish_reason":"stop",' \
                       b'"message":{"role":"assistant","content":"{\\"tasks\\":[]}"}}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(os.path.join(cert_dir, "cert.pem"),
                                os.path.join(cert_dir, "key.pem"))
        self.httpd.socket = context.wrap_socket(self.httpd.socket,
                                                server_side=True)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=3)


class PlanTlsGateTests(unittest.TestCase):

    def setUp(self):
        self.cert_dir = tempfile.mkdtemp(prefix="dsh-plan-tls-")
        self.addCleanup(shutil.rmtree, self.cert_dir, True)
        for name, text in (("cert.pem", _TEST_CERT_PEM),
                           ("key.pem", _TEST_KEY_PEM)):
            with open(os.path.join(self.cert_dir, name), "w",
                      encoding="ascii") as handle:
                handle.write(text)
        # 端点必须在起控制面之前就位：模型配置要指向它的端口。
        self.endpoint = SelfSignedEndpoint(self.cert_dir)
        self.addCleanup(self.endpoint.stop)
        # 环境里若配了代理，请求会整个改道，连的就不是这个端点——
        # 那时用例会以一种与主题无关的方式变绿或变红。
        self._real_proxies = urllib.request.getproxies
        self.addCleanup(setattr, urllib.request, "getproxies", self._real_proxies)
        urllib.request.getproxies = lambda: {}

        self.directory, self.store = support.temporary_store("plan-tls.db")
        self.httpd, self.admin = support.start(self.store, support.WEB_DIR)
        self.base = self.admin.base
        self.addCleanup(support.stop, self.httpd, self.store, self.directory)

        status, payload = self.admin.post("/api/v1/users", {
            "username": OPERATOR[0], "password": OPERATOR[1],
            "role": "operator"})
        self.assertEqual(status, 201, payload)
        self.operator = support.Client(self.base)
        status, _ = self.operator.login(*OPERATOR)
        self.assertEqual(status, 200)

    def activate_a_model(self):
        """启一条指向自签端点的配置：密钥是**平台的**，不是请求方的。"""
        status, payload = self.admin.post("/api/v1/models", {
            "name": "自签端点", "provider": "openai_compat",
            "baseUrl": "https://127.0.0.1:%d/v1" % self.endpoint.port,
            "model": "gpt-test", "apiKey": MODEL_KEY})
        self.assertEqual(status, 201, payload)
        profile_id = payload["profile"]["id"]
        status, payload = self.admin.post(
            "/api/v1/models/%s/activate" % profile_id, {})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["activeId"], profile_id)

    def plan(self, client, body):
        return client.post("/api/v1/jobs/plan", body)

    def test_an_operator_cannot_switch_off_tls_verification(self):
        """普通用户带旗就是一次越权请求：403，且一个字节都没发出去。"""
        self.activate_a_model()
        before = self.endpoint.requests
        status, payload = self.plan(self.operator,
                                    {"intent": "扫一下 127.0.0.1",
                                     "insecure": True})
        self.assertEqual(status, 403, payload)
        self.assertIn("管理员", payload.get("error", ""))
        self.assertEqual(self.endpoint.requests, before,
                         "被拒的请求不得产生任何网络动作")

    def test_an_admin_can_still_switch_off_tls_verification(self):
        """管理员照旧能用：这条能力本身是为「自签/内网端点」准备的，不能收掉。"""
        self.activate_a_model()
        before = self.endpoint.requests
        status, payload = self.plan(self.admin,
                                    {"intent": "扫一下 127.0.0.1",
                                     "insecure": True})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.endpoint.requests, before + 1,
                         "管理员带旗时请求应当真的发出去了")

    def test_without_the_flag_a_self_signed_endpoint_is_never_reached(self):
        """不带旗时握手就失败：旗确实是那个洞，不是别的东西在挡。"""
        self.activate_a_model()
        before = self.endpoint.requests
        status, payload = self.plan(self.admin, {"intent": "扫一下 127.0.0.1"})
        # 模型失败是结论而不是协议错误，所以仍是 200 + ok:false。
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload.get("ok"), payload)
        self.assertEqual(self.endpoint.requests, before,
                         "校验证书时自签端点不该收到任何请求")

    def test_planning_without_a_model_still_works_for_an_operator(self):
        """没启用模型时走确定性降级，普通用户不受这道闸影响。"""
        status, payload = self.plan(self.operator,
                                    {"intent": "扫一下 10.1.2.0/24"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["plan"]["source"], "fallback")


if __name__ == "__main__":
    unittest.main()
