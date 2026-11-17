# -*- coding: utf-8 -*-
"""跨源清单（`cors_origins`）：DSH 面板从哪个源能读平台接口。

**受信地址（`dsh_web_trusted_hosts`）那一半连同 `pentestd/dsh_web.py` 一起删掉了**：
它守的是"平台自己拉起的那个 DSH 宿主认哪些 `Host`"，而平台不再拉起任何 DSH ——
界面在插件里，宿主是使用者自己的 DSH，栅栏由 DSH 自己管。留着一份没人消费的清单，
只会让人以为"加个域名"这件事还归平台管。

留下的这一套判据不变：**逐字比对**，写入时就拒掉永远不可能命中的项。
"""
import os
import sys
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import cli, server                                     # noqa: E402
from tests import support                                            # noqa: E402


class NormalizeOriginTestCase(unittest.TestCase):
    """跨源来源的规范化：只接受"能命中 `Origin` 头"的写法。"""

    def test_accepts_what_an_origin_header_can_contain(self):
        for value, expected in (
                ("http://39997.cn:8790", "http://39997.cn:8790"),
                ("https://39997.cn", "https://39997.cn"),
                ("http://10.0.0.5:8788/", "http://10.0.0.5:8788"),
                ("  HTTP://39997.cn:8790  ", "http://39997.cn:8790"),
                ("http://[::1]:8790", "http://[::1]:8790"),
                ("http://127.0.0.1", "http://127.0.0.1")):
            self.assertEqual(expected, server.normalize_cors_origin(value), value)

    def test_rejects_what_could_never_match(self):
        """带路径、没 scheme、通配：**写入时就拒**。

        收下它们的代价不是报错，而是"清单里有一条、却永远不生效" —— 使用者只能
        从"面板还是连不上"倒推回来，而他手上唯一的线索是那份看起来没问题的清单。
        """
        for value in ("39997.cn", "http://39997.cn:8790/api", "http://a.cn/x?y=1",
                      "*", "", "   ", "http://", "http://a b", "ftp://a.cn",
                      "http://a.cn:99999", None, 123):
            self.assertEqual("", server.normalize_cors_origin(value), repr(value))


class OriginStorageTestCase(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("access.db")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_set_dedupes_and_keeps_order(self):
        final = server.set_cors_origins(
            self.store, ["http://b.cn", "http://a.cn", "http://b.cn", "http://a.cn/"])
        self.assertEqual(["http://b.cn", "http://a.cn"], final)
        self.assertEqual(final, server.extra_cors_origins(self.store))

    def test_removal_is_an_explicit_write(self):
        server.set_cors_origins(self.store, ["http://a.cn", "http://b.cn"])
        self.assertEqual(["http://b.cn"],
                         server.set_cors_origins(self.store, ["http://b.cn"]))


class CrossOriginHttpTestCase(unittest.TestCase):
    """走真实 HTTP：库里的来源被放行、别的仍然被拒。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("cors.db")
        self.httpd, self.client = support.start(self.store)
        self.base = self.client.base
        status, payload = self.client.get("/api/v1/console-token")
        self.assertEqual(status, 200, payload)
        self.token = payload["token"]

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def origin_of(self, origin: str):
        """发一条带 `Origin` 的请求，回响应里的那个头（没有就是 None）。"""
        request = urllib.request.Request(
            self.base + "/api/v1/panel",
            headers={"Origin": origin, "Authorization": "Bearer " + self.token})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.headers.get("Access-Control-Allow-Origin")
        except urllib.error.HTTPError as exc:                       # noqa: BLE001
            return exc.headers.get("Access-Control-Allow-Origin")

    def test_database_list_is_honoured_without_any_env_var(self):
        """库里那份**不需要环境变量、也不需要重启**。

        这一条是这个功能的主诉求：DSH 面板跑在它自己的源上，加一个来源要能在运行中
        生效 —— 重启一个正在给人用的控制面是要挑时间的。
        """
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(server.CORS_ORIGINS_ENV, None)
            # 回环来源是**内置放行**的（任意端口），所以判据必须用一个非回环源 ——
            # 拿 127.0.0.1 试，改与不改都是同一个结果，那条用例会空转。
            self.assertIsNone(self.origin_of("http://10.0.0.5:19387"))
            server.set_cors_origins(self.store, ["http://10.0.0.5:19387"])
            self.assertEqual("http://10.0.0.5:19387",
                             self.origin_of("http://10.0.0.5:19387"))

    def test_only_the_exact_origin_is_honoured(self):
        """逐字比对：端口不同、scheme 不同、多一个子域，都不放行。"""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(server.CORS_ORIGINS_ENV, None)
            server.set_cors_origins(self.store, ["http://39997.cn:8790"])
            for origin in ("http://39997.cn:8791", "https://39997.cn:8790",
                           "http://evil.39997.cn:8790", "http://39997.cn"):
                self.assertIsNone(self.origin_of(origin), origin)

    def test_env_and_database_lists_are_merged(self):
        """两处都算数：环境变量适合部署时给定，库里那份适合运行中加。"""
        server.set_cors_origins(self.store, ["http://39997.cn:8790"])
        with mock.patch.dict(os.environ,
                             {server.CORS_ORIGINS_ENV: "http://10.9.9.9:8790"}):
            self.assertEqual("http://10.9.9.9:8790",
                             self.origin_of("http://10.9.9.9:8790"))
            self.assertEqual("http://39997.cn:8790",
                             self.origin_of("http://39997.cn:8790"))

    def test_a_broken_store_value_does_not_break_the_request(self):
        """清单读坏了只意味着"没有额外来源"，不该让一条跨源请求变成 500。"""
        self.store.set_setting(server.SETTING_CORS_ORIGINS, None)
        self.assertIsNone(self.origin_of("http://39997.cn:8790"))


class AccessCliTestCase(unittest.TestCase):
    """命令行的那一层：看现状、加、删，以及"乱填就一个字节都不写"。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("access-cli.db")
        self.store.close()          # 命令自己会开一份（照部署的路径走）
        self.db = os.path.join(self.directory.name, "access-cli.db")

    def tearDown(self):
        self.directory.cleanup()

    def run_cli(self, *argv):
        parser = cli.build_parser()
        args = parser.parse_args(["access", *argv, "--db", self.db])
        with mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()) as out:
            code = args.func(args)
        return code, out.getvalue()

    def settings_of(self, key):
        from pentestd.store import Store
        store = Store(self.db)
        try:
            return str(store.get_setting(key, "") or "")
        finally:
            store.close()

    def test_add_origin_persists_and_normalizes(self):
        code, text = self.run_cli("add-origin", "http://39997.cn:8790/")
        self.assertEqual(0, code, text)
        self.assertEqual("http://39997.cn:8790",
                         self.settings_of(server.SETTING_CORS_ORIGINS))

    def test_bad_origin_writes_nothing_at_all(self):
        """一半写进去、一半被拒，会让"清单现在是什么"变成要去库里查的问题。"""
        code, text = self.run_cli("add-origin", "http://ok.cn", "39997.cn:8790")
        self.assertEqual(2, code, text)
        self.assertIn("一个字节都没写", text)
        self.assertEqual("", self.settings_of(server.SETTING_CORS_ORIGINS))

    def test_remove_origin(self):
        self.run_cli("add-origin", "http://39997.cn:8790")
        code, text = self.run_cli("remove-origin", "http://39997.cn:8790")
        self.assertEqual(0, code, text)
        self.assertEqual("", self.settings_of(server.SETTING_CORS_ORIGINS))

    def test_listing_says_it_takes_effect_immediately(self):
        """清单的生效方式是"立即"，而这件事只有界面/命令行说得清。

        说错的代价：使用者改完发现没生效，于是去重启整个平台 —— 而他本来什么都不用做。
        """
        _code, text = self.run_cli("list")
        self.assertIn("立即生效", text)
        self.assertIn("127.0.0.1", text)

    def test_host_actions_are_gone(self):
        """`add-host` / `remove-host` 随那份清单一起删了。

        留着它们，命令会"成功"地往一个没人读的设置里写东西 —— 那是这一轮里最
        容易留下的那种债：看起来做了事，实际什么都没发生。
        """
        with self.assertRaises(SystemExit):
            parser = cli.build_parser()
            with mock.patch("sys.stderr", new_callable=lambda: __import__("io").StringIO()):
                parser.parse_args(["access", "add-host", "39997.cn", "--db", self.db])


if __name__ == "__main__":
    unittest.main()
