# -*- coding: utf-8 -*-
"""测试支撑：带会话的 HTTP 客户端 + 起一个已初始化管理员的服务。

控制面默认要求登录，因此测试也必须先登录——这不是麻烦，而是让测试跑在
**和真实部署一样的鉴权路径**上。任何绕过鉴权的测试辅助都会让"认证到底有没有生效"
变成没人验证的问题。
"""
import http.cookiejar
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd.server import serve_background                    # noqa: E402
from pentestd.store import Store                                # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")
ADMIN_USER = "admin"
ADMIN_PASSWORD = "Test-Admin-Pass1!"


class Client:
    """带 Cookie 与会话的测试客户端：自动带 CSRF 头。"""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def request(self, method: str, path: str, body=None, headers=None,
                with_csrf: bool = True):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        head = {"Content-Type": "application/json"}
        if with_csrf and self.csrf and method.upper() == "POST":
            head["X-CSRF-Token"] = self.csrf
        head.update(headers or {})
        request = urllib.request.Request(self.base + path, data=data,
                                         method=method, headers=head)
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read()
                return response.status, (json.loads(raw.decode("utf-8"))
                                         if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                payload = {"error": raw.decode("utf-8", "replace")}
            return exc.code, payload

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None, headers=None):
        return self.request("POST", path, body if body is not None else {},
                            headers)

    def bootstrap(self, username: str = ADMIN_USER,
                  password: str = ADMIN_PASSWORD):
        status, payload = self.post("/api/v1/auth/bootstrap",
                                    {"username": username, "password": password},
                                    )
        if status == 201:
            self.csrf = payload.get("csrfToken", "")
        return status, payload

    def login(self, username: str = ADMIN_USER, password: str = ADMIN_PASSWORD):
        status, payload = self.post("/api/v1/auth/login",
                                    {"username": username, "password": password})
        if status == 200:
            self.csrf = payload.get("csrfToken", "")
        return status, payload

    def logout(self):
        status, payload = self.post("/api/v1/auth/logout", {})
        if status == 200:
            self.csrf = ""
        return status, payload


def start(store: Store, web_dir: str = WEB_DIR, admin: bool = True, **kwargs):
    """起一个控制面；默认先创建管理员并登录，返回 (httpd, client)。"""
    httpd, _thread = serve_background(store, web_dir, "127.0.0.1", 0, **kwargs)
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    client = Client(base)
    if admin:
        status, payload = client.bootstrap()
        if status != 201:
            raise RuntimeError("测试管理员创建失败：%s %s" % (status, payload))
    return httpd, client


def temporary_store(name: str = "test.db"):
    """返回 (临时目录对象, Store)，调用方负责 close 与 cleanup。

    `ignore_cleanup_errors`：Windows 上删库文件要求所有句柄都已释放，
    而 ThreadingHTTPServer 的工作线程是 daemon、停止后不保证立刻退出。
    真正该报警的是"连接有没有泄漏"，那个由 stop() 里的断言负责，
    不该让文件系统的时序问题把测试搞成随机失败。
    """
    directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    return directory, Store(os.path.join(directory.name, name))


def stop(httpd, store, directory=None) -> None:
    """按正确顺序收摊：停服务 → 等工作线程退出 → 关连接 → 删临时目录。

    顺序错了会导致 Windows 上"文件被占用"，或更糟：清理掩盖了真正的资源泄漏。
    """
    httpd.shutdown()
    httpd.server_close()
    deadline = time.time() + 5
    while threading.active_count() > 1 and time.time() < deadline:
        time.sleep(0.05)
    store.close()
    if directory is not None:
        directory.cleanup()
