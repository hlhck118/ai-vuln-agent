# -*- coding: utf-8 -*-
"""访问日志：绝不能让请求卡在 stderr 上。

这一组用例来自一次真实的挂死：控制台**接口全部正常**，而首页与静态资源全部超时、
一个字节都不返回。

原因不在路由里，在日志里。`BaseHTTPRequestHandler.send_response()` 的第一件事就是
调 `log_request()` → `log_message()`，默认实现把访问日志写到 **stderr**——也就是说
这一步在请求路径上，而且发生在任何响应头写出**之前**。当服务的 stdout/stderr 被一个
**没人抽干的管道**接着（包装脚本或 GUI 启动器以管道方式拉起、读取端先退出），管道
缓冲写满（约 64KB）之后 `write()` 会永久阻塞，那个请求就再也发不出响应。

症状之所以有误导性：`log_message` 原本对 `/api/` 直接 return。于是**接口全都正常、
页面全挂**——这个组合很容易被读成"前端资源路径错了"或"静态目录没配对"，而真正
要修的是一行日志。

这里钉三件事：

1. **落地**：访问日志写文件，写失败一律吞掉，不影响这次请求；
2. **分离**：请求路径不往 stderr 写访问日志（默认实现被换掉了，且实测没有字节过去）；
3. **别写满盘**：超过上限滚动一次。

关于第 2 条为什么用"内容里不许出现访问日志"而不是"stderr 必须一个字节都没有"：
服务里可能有别的东西在别的线程上写启动信息或警告，把断言写成"必须为空"会让这一组
用例因为与本病无关的原因随机失败——那种失败一旦出现，最可能的处理是被顺手删掉，
而不是被查清。真正要保证的性质是"**访问日志不走 stderr**"，就直接钉这个。
"""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from tests import support                                          # noqa: E402

from pentestd import server                                        # noqa: E402
from pentestd.server import BaseHTTPRequestHandler                 # noqa: E402
from pentestd.store import Store                                   # noqa: E402

ACCESS_SHAPE = '"GET / HTTP/1.1"'


class _StderrRecorder:
    """顶替 sys.stderr，只记账不阻塞。

    刻意**不**去复现"写满的管道"：那要求真的造一个填满的管道，行为带平台差异
    （Windows 与 Linux 的管道容量、非阻塞语义都不同），还依赖时序。而病因是
    "往 stderr 写了"，只要写了一次就已经暴露——记账比计时稳定得多。
    """

    def __init__(self):
        self.chunks = []
        self._lock = threading.Lock()

    def write(self, text):
        with self._lock:
            self.chunks.append(text)
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False

    def text(self):
        with self._lock:
            return "".join(self.chunks)


class AccessLogFileTests(unittest.TestCase):
    """`access_log()` 本身：落盘、不抛、超限滚动。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = os.path.join(self.tmp.name, "access.log")

    def tearDown(self):
        self.tmp.cleanup()

    def _read(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    def test_writes_a_timestamped_line(self):
        server.access_log(self.path, ACCESS_SHAPE + " 200 -")
        body = self._read()
        self.assertIn(ACCESS_SHAPE + " 200 -", body)
        self.assertRegex(body, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ",
                         "每行要有可读时间戳，否则事后对不上现场")

    def test_appends_rather_than_truncates(self):
        server.access_log(self.path, "first")
        server.access_log(self.path, "second")
        body = self._read()
        self.assertIn("first", body)
        self.assertIn("second", body)

    def test_empty_path_is_a_noop(self):
        # 没有数据目录（例如纯内存 store）时只是不记，不是错误
        server.access_log("", ACCESS_SHAPE + " 200 -")

    def test_unwritable_target_is_swallowed(self):
        # 把日志"文件"指向一个目录：open(..., 'a') 必然 OSError，不该冒到请求路径上
        server.access_log(self.tmp.name, ACCESS_SHAPE + " 200 -")

    def test_oversized_file_rotates(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("x" * (server.ACCESS_LOG_MAX_BYTES + 1))
        server.access_log(self.path, "after-rotation")
        self.assertTrue(os.path.exists(self.path + ".1"), "超过上限要滚动一份")
        self.assertLess(os.path.getsize(self.path), 1024, "滚动后要重新开始写")
        self.assertIn("after-rotation", self._read())


class RequestPathStaysOffStderrTests(unittest.TestCase):
    """真实服务 + 真实请求：访问日志不许出现在 stderr 上。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.store = Store(os.path.join(cls.tmp.name, "access.db"))
        cls.httpd, cls.client = support.start(cls.store)
        cls.base = cls.client.base
        # 数据目录由 store 路径决定，access_log 就落在它旁边
        cls.log_path = os.path.join(cls.tmp.name, "access.log")

    @classmethod
    def tearDownClass(cls):
        support.stop(cls.httpd, cls.store, cls.tmp)

    # -- 帮助 ---------------------------------------------------------------

    def _capture(self, action):
        """在 stderr 被顶替的情况下执行 action，返回 (结果, 记到的文本)。"""
        recorder = _StderrRecorder()
        saved = sys.stderr
        sys.stderr = recorder
        try:
            result = action()
        finally:
            sys.stderr = saved
        return result, recorder.text()

    def _fetch_page(self):
        request = urllib.request.Request(self.base + "/", method="GET")
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read()

    def _log_text(self):
        try:
            with open(self.log_path, encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return ""

    def _wait_for_log(self, needle, timeout=5.0):
        """等某一类日志行出现，返回当时的全文。

        服务在另一个线程上写文件，直接读会撞上时序；等一小会儿比 sleep 定长稳。
        """
        deadline = time.time() + timeout
        body = self._log_text()
        while needle not in body and time.time() < deadline:
            time.sleep(0.05)
            body = self._log_text()
        return body

    # -- 结构 ---------------------------------------------------------------

    def test_both_stderr_writing_hooks_are_replaced(self):
        """默认的 log_message / log_error 都写 stderr，两个都必须被换掉。

        这是一条结构性断言，直接对着"有人把默认实现改回来"这种回退。
        `httpd.RequestHandlerClass` 就是 `make_handler()` 造出来的那个类。
        """
        handler = self.httpd.RequestHandlerClass
        self.assertIsNot(handler.log_message, BaseHTTPRequestHandler.log_message,
                         "log_message 又用回了默认实现（默认实现写 stderr）")
        self.assertIsNot(handler.log_error, BaseHTTPRequestHandler.log_error,
                         "log_error 又用回了默认实现（send_error 会先调它）")

    # -- 行为 ---------------------------------------------------------------

    def test_page_request_keeps_stderr_quiet(self):
        (status, body), noise = self._capture(self._fetch_page)
        self.assertEqual(200, status)
        self.assertIn(b"<html", body[:2048].lower())
        self.assertNotIn(ACCESS_SHAPE, noise,
                         "首页请求把访问日志写到了 stderr：%r" % noise[:300])

    def test_page_request_line_lands_in_the_file(self):
        self._fetch_page()
        with open(self.log_path, encoding="utf-8") as handle:
            written = handle.read()
        self.assertIn(ACCESS_SHAPE + " 200", written,
                      "访问日志既没进 stderr 也没进文件——那就是被静默丢了")

    def test_api_request_keeps_stderr_quiet(self):
        """接口原本就被有意跳过，这条防的是"顺手改成全都记到 stderr"。"""
        (status, _payload), noise = self._capture(
            lambda: self.client.get("/api/v1/auth/state"))
        self.assertEqual(200, status)
        self.assertNotIn("/api/v1/auth/state", noise,
                         "接口调用写到了 stderr：%r" % noise[:300])

    def test_bad_request_line_keeps_stderr_quiet(self):
        """畸形请求走的是 send_error → log_error，另一条写 stderr 的路径。

        用裸 socket 发一个不是 HTTP 的请求行，才能真的踩到解析失败那条分支。

        这里**不**断言"收到了 400 响应"：请求行后面还剩一个没被读走的 `\\r\\n`，
        服务端关闭连接时内核会发 RST，客户端缓冲里那份响应会被一并丢弃——能不能
        看到响应取决于收发时序，拿它当断言等于给测试埋随机失败。改成断言**正面
        证据**：这条请求确实被处理了（日志文件里出现了它对应的 log_error 行）。
        """
        port = self.httpd.server_address[1]

        def noisy_request():
            conn = socket.create_connection(("127.0.0.1", port), timeout=10)
            try:
                conn.sendall(b"THIS IS NOT HTTP\r\n\r\n")
                conn.settimeout(5)
                try:
                    conn.recv(4096)
                except (socket.timeout, OSError):
                    pass
            finally:
                conn.close()

        _result, noise = self._capture(noisy_request)

        self.assertNotIn("THIS IS NOT HTTP", noise,
                         "畸形请求的日志写到了 stderr：%r" % noise[:300])
        written = self._wait_for_log("ERROR code 400")
        self.assertIn("ERROR code 400", written,
                      "畸形请求既没在 stderr 留痕也没在文件留痕——"
                      "要么没被处理，要么日志被静默丢了")


if __name__ == "__main__":
    unittest.main()
