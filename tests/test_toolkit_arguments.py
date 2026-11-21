# -*- coding: utf-8 -*-
"""`run_tool` 的参数面：适配器表之外的执行路径必须被挡住。

## 这一组守的是什么

`toolkit` 那一层的结构前提是**只有适配器表里的二进制会被执行**：`require()` 逐个查
安装、每个适配器一个侵入类别、一类一道准入闸。而 `ssh -o ProxyCommand=<程序>` 让 ssh
去拉起**表外的**那个程序——前提就没了。

实测（修复前，本机 OpenSSH 与 curl 都在）：

    run_tool(curl, ["-o", "<临时目录>/x.bat", "http://…"])         → 文件落地，exit 0
    run_tool(ssh,  ["-o", "ProxyCommand=<那个 x.bat>", "-N", "x"])  → x.bat 被执行

两步的参数都过得了 `validate_argument`：`ProxyCommand=C:/x/y.bat` 全是白名单字符
（字母、数字与 `_ . : / @ [ ] - + \\ =`）。所以这不是"参数写错了"，是一条**完整的
代码执行链**，而它当时只需要 `toolkit` 与 `pivot` 两类被放行。

## 留着的那一半

`curl -o <任意路径>` **没有**被堵：它是这个工具的文档化用法（保存响应以便交叉复核），
本平台刻意保留 curl 的理由就写在适配器表的 `why` 里。但它的确等于"以节点权限写任意
路径的文件"——这一条写在 `authz.CLASSES` 的 `requires` 里，让放行 `toolkit` 的人知道
自己在放行什么，而不是假装它不存在。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import toolkit                              # noqa: E402


class ExecOptionGuardTestCase(unittest.TestCase):
    """参数层：`-o Key=Value` 里的键。"""

    def test_every_exec_key_is_rejected(self):
        """`EXEC_OPTION_KEYS` 里每一个键都要真的被挡住。

        逐个键跑，而不是只试 `ProxyCommand`：这张表存在的意义就是"补新选项时只改
        一处"，而漏掉的那一个不会有任何症状——直到有人用它。
        """
        self.assertTrue(toolkit.EXEC_OPTION_KEYS, "执行类选项表不能是空的")
        for key in sorted(toolkit.EXEC_OPTION_KEYS):
            for written in (key, key.upper(), key.title()):
                with self.assertRaises(toolkit.ToolkitError, msg=written):
                    toolkit.validate_option("%s=/whatever" % written)

    def test_key_match_is_case_insensitive(self):
        """ssh 的选项键大小写不敏感，绕过尝试的第一招就是把首字母打成大写。"""
        for written in ("ProxyCommand=/x", "PROXYCOMMAND=/x", "Proxycommand=/x"):
            with self.assertRaises(toolkit.ToolkitError, msg=written):
                toolkit.validate_option(written)

    def test_a_quoted_or_punctuated_key_cannot_slip_past_the_table(self):
        """键归一化成"只留字母数字"——这条是**放行 `"` 之后必须补的**。

        原先 `"` 被字符集挡在门外，`-o '"ProxyCommand=calc.exe"'` 根本走不到键比对；
        现在它走得到，而 `"proxycommand"` 不在 `EXEC_OPTION_KEYS` 里——那就是一条
        随放行 `"` 一起长出来的新口子。带分隔符的写法 ssh 自己也不认（会报
        bad configuration option），所以归一并不会误伤任何合法选项。
        """
        for written in ('"ProxyCommand=calc.exe"', "proxy_command=calc.exe",
                        "Proxy-Command=calc.exe", "Proxy Command=calc.exe",
                        "  ProxyCommand =calc.exe"):
            with self.assertRaises(toolkit.ToolkitError, msg=written):
                toolkit.validate_option(written)

    def test_a_quoted_legit_value_is_still_accepted(self):
        """归一只放宽**比对**，不改变放行的值本身。"""
        self.assertEqual(toolkit.validate_option('StrictHostKeyChecking="no"'),
                         'StrictHostKeyChecking="no"')

    def test_only_the_key_is_judged(self):
        """按**键**判定，不按值。

        值可以是任意合法取值，按值判断会误伤 `StrictHostKeyChecking=no` 这一类；
        而"能不能拉起一个进程"这件事由键决定。
        """
        for value in ("StrictHostKeyChecking=no", "ConnectTimeout=5", "BatchMode=yes",
                      "PreferredAuthentications=publickey",
                      "UserKnownHostsFile=C:/Users/x/known_hosts"):
            self.assertEqual(toolkit.validate_option(value), value)

    def test_multi_value_options_now_pass_but_the_key_check_does_not_move(self):
        """`Ciphers=aes128-ctr,aes256-ctr` 现在能过了——但键的判定没跟着放。

        这一条以前叫 `test_multi_value_options_hit_the_global_charset_first`，
        内容是"已知边界：传不了……这一条留给将来一并处理"。现在处理了：
        值是数据、键是结构，两件事分成两档。
        **把边界改成断言**，是因为边界本身不会有人维护——它会在某个下午被当成
        一次回退，而回退和修复在 diff 里长得一样。
        """
        self.assertEqual(toolkit.validate_option("Ciphers=aes128-ctr,aes256-ctr"),
                         "Ciphers=aes128-ctr,aes256-ctr")
        # 放宽的是**值**，不是**键**：执行类选项照样拦。
        with self.assertRaises(toolkit.ToolkitError):
            toolkit.validate_option("ProxyCommand=calc.exe, -N")

    def test_build_command_blocks_and_keeps_the_rest(self):
        """拦一个不该拦的旁边那些：同一串里的其它参数要原样进 argv。"""
        with self.assertRaises(toolkit.ToolkitError):
            toolkit.build_command("ssh",
                                  ["-o", "ProxyCommand=calc.exe", "-N", "10.0.0.5"])
        argv = toolkit.build_command("ssh", ["-o", "ConnectTimeout=5", "-p", "2222",
                                             "-N", "10.0.0.5"])
        self.assertEqual(argv[-6:], ["-o", "ConnectTimeout=5", "-p", "2222",
                                     "-N", "10.0.0.5"])

    def test_adapters_without_option_flags_are_untouched(self):
        """没有声明 `optionFlags` 的适配器，`-o` 是它自己的意思。

        把 ssh 的规则套到所有适配器上，会让 curl 的 `-o`（= `--output`）一起被
        当成选项开关——那是过度修正，伤的是文档化用法。
        """
        for adapter in ("curl", "nmap", "sqlmap", "hydra", "ncrack"):
            entry = toolkit.ADAPTER_INDEX[adapter]
            self.assertFalse(entry.get("optionFlags", ()),
                             "%s 不该有 optionFlags" % adapter)

    def test_curl_output_flag_is_still_allowed(self):
        """`curl -o` 仍然放行——它是文档化用法，不是漏洞。

        钉住它是为了防止下一次"安全清理"顺手把 `-o` 也一起删掉：那时使用者会失去
        保存响应做交叉复核的能力，而那正是本平台刻意保留 curl 的理由。
        """
        entry = toolkit.ADAPTER_INDEX["curl"]
        self.assertIn("-o", entry["arguments"])


class ExecOptionEndToEndTestCase(unittest.TestCase):
    """整条链走一次：授权过了之后，参数层仍然挡住，且**不产生副作用**。"""

    GRANT = "AUTH-UNIT-TEST"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.marker = os.path.join(self.tmp.name, "marker.txt")
        self.payload = os.path.join(self.tmp.name, "payload.bat")
        with open(self.payload, "w", encoding="utf-8") as handle:
            handle.write("@echo off\r\necho X > \"%s\"\r\n" % self.marker)

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_goes_through_build_command(self):
        """`run()` 必须走 `build_command`。

        绕过它自己拼 argv 的实现，会让上面那一整组用例变成摆设——它们在测一个
        产品里没人走的函数。

        判据读的是**字节码里的全局名**，不是 `inspect.getsource`：后者按函数对象
        的行号回磁盘上重读文件，全量套件里前面任何一个用例改过源文件、或让
        `linecache` 记住了旧内容，这里读到的就是**别人的正文**——`Ran 3171
        tests` 那一次 `FAILED (failures=1)` 正是这么来的（单跑必过、全套必挂，
        而报错里贴出来的正文是 `toolkit.py` 里毫不相干的一段）。字节码在 import
        时刻就固定了，不随磁盘变化，也不会被同一进程里的其他用例改掉。
        """
        self.assertIn("build_command", toolkit.run.__code__.co_names,
                      "run() 自己拼 argv 就等于绕过了参数层")

    @unittest.skipUnless(toolkit.installed("ssh"), "本机没有 ssh 客户端")
    def test_the_chain_is_broken_before_any_process_starts(self):
        """断言的是**标记文件没有出现**，而不是"抛了异常"。

        只断言异常的话，"先起进程、再报错"的实现照样能过——而那正是这条链最坏的
        失败方式（它看起来被挡住了，实际上已经执行过）。
        """
        with self.assertRaises(toolkit.ToolkitError):
            toolkit.run("ssh",
                        ["-o", "ProxyCommand=" + self.payload.replace("\\", "/"),
                         "-N", "localhost"],
                        authorization=self.GRANT, grant_enabled=["pivot"],
                        grant_targets=["localhost"], host="localhost", timeout=10)
        self.assertFalse(os.path.exists(self.marker),
                         "参数检查发生在起进程之前——标记文件不该出现")


class DataArgumentCharsetTestCase(unittest.TestCase):
    """字符集两档：值是数据的开关放行什么，以及**没**放行什么。

    ## 为什么这一组必须存在

    平台自己装的 skill 就在教模型写 `curl -w '%{http_code}'`
    （`data/dsh-home/profiles/pentest-platform/node_modules/dsh-purge/skills/
    redteam/webshell-toolkit.md` 第 47 行），而闸把它拒了。**产品自己的文档推荐
    一条产品自己的闸会拒的命令**，使用者据此会得出"这个平台复核不了 webshell
    状态码"——真因是字符集，而错误信息当时只说了"含不允许的字符"。

    ## 这一组钉的是什么

    放宽的部分（`% { } ,` 空格 `;` `?` `"`）与没放宽的部分（`& | ^ < > \u0060 $`、
    控制符）各钉一遍。**只钉放宽的那一半不够**：下一次"顺手把禁表也改小一点"
    不会有任何症状——直到它不再是症状。

    ## 引号是第二轮的同一类错

    第一轮把 `% { } ,` 放开，是因为平台自己的 skill 教的命令被自己的闸拒了。
    第二轮是 `"`：Vaultwarden 注册那条 `{"email":"…"}` 被拒，报文里给出的
    "改用 `-d @payload.json`" 又落不了地——`run_tool` 的 cwd 是任务目录，调用方
    拿不到它。所以这一轮的口径是**按"有没有第二层解析器"分**，不是按字符分：
    直达 `.exe` 的 argv 放行 `"`（Windows 上 `subprocess` 的转义与子进程的
    `CommandLineToArgvW` 往返无损，有用例实测），`.cmd`/`.bat` 包装上整档回落、
    引号照样拒。顺带补的是 `validate_option` 的键归一化——`"` 一旦能进值，
    `-o '"ProxyCommand=…"'` 就会绕过键比对，这条口子是放行 `"` 带出来的。
    """

    def setUp(self):
        # 全组都注入**假的**适配器记录。这一组测的是字符集，而 `build_command`
        # 会先走 `require()`——本机没装 nmap 时它抛的是"未安装"，字符集的断言
        # 根本没机会跑。那种失败形态会被读成"某个适配器的问题"，
        # 而它其实在说"这台机器上没装 nmap"。
        self._saved = dict(toolkit._DETECTED)
        self._fake("curl", "C:/fake/bin/curl.exe", False)
        self._fake("nmap", "C:/fake/bin/nmap.exe", False)

    def tearDown(self):
        toolkit._DETECTED.clear()
        toolkit._DETECTED.update(self._saved)

    def _fake(self, adapter_id, path, wrapper):
        entry = dict(toolkit.ADAPTER_INDEX[adapter_id])
        entry.update({"path": path, "installed": True, "wrapper": wrapper})
        toolkit._DETECTED[adapter_id] = entry

    def test_curl_write_out_format_is_accepted(self):
        """平台自己的 skill 就在用这个写法，闸不能挡它。"""
        argv = toolkit.build_command("curl", ["-sS", "-o", "NUL", "-w",
                                              "%{http_code}", "http://10.0.0.5/x.jsp"])
        self.assertIn("%{http_code}", argv)

    def test_headers_and_comma_lists_are_accepted(self):
        """数据档要覆盖最普通的两类：带空格的 HTTP 头、逗号分隔的值列表。"""
        self.assertIn("Authorization: Bearer abc",
                      toolkit.build_command("curl", ["-H", "Authorization: Bearer abc"]))
        self.assertIn("80,443,8080",
                      toolkit.build_command("nmap", ["-p", "80,443,8080", "10.0.0.5"]))

    def test_semicolon_is_allowed_for_cookie_headers(self):
        """`;` 放行，理由是它在 cmd 里**不是**命令分隔符（那是 POSIX shell）。

        钉住它是因为它长得最像"该被禁的东西"：一份不假思索的清理会先删掉它，
        删掉之后 `Cookie: a=1; b=2` 就传不了，症状是"cookie 只能带一个"。
        """
        self.assertIn("Cookie: a=1; b=2",
                      toolkit.build_command("curl", ["-H", "Cookie: a=1; b=2"]))

    def test_injection_characters_survive_in_no_tier(self):
        """`& | ^ < >` `` ` `` `$` 在任何档都不放行，逐个钉。

        **`"` 不在这张表里**，而这一条是改过的：它在非包装的 `.exe` 上往返无损
        （见下面的往返用例），而真正危险的那条路（`.cmd` 包装）由 `wrapper` 整档
        回落挡住。原先把它和 `&` 列在一起，代价是"内联 JSON 请求体传不了"，
        而报文里给出的等价写法（`-d @payload.json`）在 `run_tool` 上落不了地
        ——它的 cwd 是任务目录，调用方拿不到。
        """
        for ch in '&|^<>`$':
            with self.assertRaises(toolkit.ToolkitError, msg="%r 被放过了" % ch):
                toolkit.build_command("curl", ["-H", "X: a%sb" % ch])

    def test_an_inline_json_body_is_accepted(self):
        """带引号的**内联**请求体是过的：这是 Vaultwarden 注册那条的形状。

        `-d` 声明在 `dataFlags` 里，而 `"` 现在是数据档放行的字符之一。
        """
        body = '{"email":"vwtest20266@example.com"}'
        built = toolkit.build_command("curl", ["-H", "Content-Type: application/json",
                                               "-d", body])
        self.assertIn(body, built)

    def test_the_file_form_still_passes(self):
        """文件写法没有因为放行引号而失效。"""
        self.assertIn("@payload.json",
                      toolkit.build_command("curl", ["-d", "@payload.json"]))

    def test_a_wrapper_binary_still_refuses_the_quoted_body(self):
        """`.cmd` 包装上引号照样拒，且消息要说清是包装造成的。

        **这是"放行 `"`"这笔交易的价码**：放宽只落在"参数走 argv 直达 .exe"这一条
        路上；一旦解析结果是 `.cmd`/`.bat`，整档回落到严格档。不钉住它，放行 `"`
        就等于在 cmd.exe 重新解析的那条路上也开了一道口子。
        """
        self._fake("curl", "C:/fake/curl.cmd", True)
        with self.assertRaises(toolkit.ToolkitError) as caught:
            toolkit.build_command("curl", ["-d", '{"a":1}'])
        self.assertIn("cmd.exe", str(caught.exception),
                      "回落时要说清是因为包装脚本")
        self.assertIn("@payload.json", str(caught.exception),
                      "被拒时仍要给出文件写法")

    def test_real_newline_is_refused_and_the_error_teaches_the_escape(self):
        """真换行仍然拒，但错误信息必须给出等价写法。

        只拒不说，读的人会去把 `-w` 整个删掉——而那正是平台自己教的写法。
        """
        with self.assertRaises(toolkit.ToolkitError) as caught:
            toolkit.build_command("curl", ["-w", "A\nB %{http_code}", "http://x/"])
        message = str(caught.exception)
        self.assertIn("换行", message)
        self.assertIn("\\n", message, "错误信息要说清 curl 认两字符的 \\n")

    def test_the_backslash_n_escape_is_accepted(self):
        """而转义写法是过的——两条合起来才构成"有等价写法"这句话。"""
        argv = toolkit.build_command("curl", ["-w", "A\\nB %{http_code}", "http://x/"])
        self.assertIn("A\\nB %{http_code}", argv)

    def test_undeclared_flags_stay_on_the_strict_tier(self):
        """没声明为数据开关的位置仍然是严格档。

        否则"数据档"是一句空话：任何 token 都能自称数据。
        """
        with self.assertRaises(toolkit.ToolkitError):
            toolkit.build_command("curl", ["%{http_code}"])          # 位置参数
        with self.assertRaises(toolkit.ToolkitError):
            toolkit.build_command("curl", ["-X", "GE%{x}"])          # -X 没声明

    def test_wrapper_binary_loses_the_data_tier(self):
        """`.cmd`/`.bat` 包装下，数据档自动回落到严格档。

        **这一条是整块改动的关键。** 同一个 `-w %{http_code}`，真实 `.exe` 上过、
        `.cmd` 包装上不过——因为 cmd.exe 会真的把 `%{http_code}` 展开成别的东西。
        不钉住它，"宽严只取决于参数角色"就是空的：还有一个变量（走哪条路）在决定
        结果，而它只体现在这一条断言里。
        """
        self._fake("curl", "C:/fake/curl.cmd", True)
        with self.assertRaises(toolkit.ToolkitError) as caught:
            toolkit.build_command("curl", ["-w", "%{http_code}", "http://x/"])
        self.assertIn("cmd.exe", str(caught.exception),
                      "回落时要说清是因为包装脚本，否则会被当成随机故障")
        self._fake("curl", "C:/fake/curl.exe", False)
        self.assertIn("%{http_code}",
                      toolkit.build_command("curl", ["-w", "%{http_code}", "http://x/"]))

    def test_query_string_urls_are_accepted(self):
        """带查询串的 URL 是普通目标形状，`?` 进来了就不该挡。"""
        self.assertIn("http://10.0.0.5/a?id=1",
                      toolkit.build_command("curl", ["-sS", "http://10.0.0.5/a?id=1"]))

    def test_declared_flags_are_also_documented(self):
        """声明了的开关必须也在 `arguments` 里列出来。

        `arguments` 是给模型看的（"这个适配器常用哪些参数"），两个表各写一份迟早
        分叉：分叉的症状是"某个参数能过闸但没人知道它能用"——没有人会因此报错，
        只会少用它，于是这个能力等于不存在。
        """
        for entry in toolkit.ADAPTERS:
            declared = (set(entry.get("optionFlags") or ())
                        | set(entry.get("dataFlags") or ()))
            listed = set(entry.get("arguments") or ())
            self.assertFalse(declared - listed,
                             "%s 声明了 %s 却没在 arguments 里列出"
                             % (entry["id"], sorted(declared - listed)))


class QuotedBodyRoundTripTestCase(unittest.TestCase):
    """`"` 放行的**依据**：真跑一次，看正文有没有原样到达。

    这一条不是"字符集里少了一个字符"的断言。放行 `"` 的全部理由，是"非包装的
    `.exe` 上引号由 Windows 自己转义、由子进程自己还原，往返无损"。那是关于
    `subprocess` + `CommandLineToArgvW` 的一个事实，只能实测——而它恰恰是最容易
    被下一次"安全清理"改回去的那一条。

    跑的是 `build_command` 的产物交给 `Popen`，与 `_stream_child` 的调用形状一致；
    中间隔着一个真实进程与一个真实 HTTP 请求，所以它验的是整条 argv 链，
    不是"某个字符串里有没有那个字符"。
    """

    BODY = '{"email":"vwtest20266@example.com","name":"a b"}'

    def setUp(self):
        self._saved = dict(toolkit._DETECTED)
        self._fake("curl", "C:/fake/bin/curl.exe", False)

    def tearDown(self):
        toolkit._DETECTED.clear()
        toolkit._DETECTED.update(self._saved)

    def _fake(self, adapter_id, path, wrapper):
        entry = dict(toolkit.ADAPTER_INDEX[adapter_id])
        entry.update({"path": path, "command": [path],
                      "execPath": path, "execCommand": [path],
                      "installed": True, "wrapper": wrapper})
        toolkit._DETECTED[adapter_id] = entry

    def test_the_body_arrives_byte_for_byte(self):
        """`Popen` 起真进程，服务端收到的正文与拼进去的那个字符串逐字节相同。"""
        import subprocess
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("content-length") or 0)
                received["body"] = self.rfile.read(size)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = ("http://127.0.0.1:%d/api/accounts/register"
                   % server.server_address[1])
            argv = toolkit.build_command(
                "curl", ["-s", "-o", "NUL", "-H",
                         "Content-Type: application/json",
                         "-d", self.BODY, url])
            # argv[0] 来自假条目里的 `command`（`_locate` 那边对应 `execCommand`）；
            # 这一条只复核参数层，真跑起来用本机的 curl。
            replaced = list(argv)
            replaced[0] = self._real_curl()
            proc = subprocess.Popen(replaced, shell=False,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
            proc.communicate(timeout=60)
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(received.get("body", b"").decode("utf-8"), self.BODY,
                         "引号在 argv 里往返一次，正文必须逐字节相同")

    @staticmethod
    def _real_curl():
        """本机真 curl 的绝对路径；没有就跳过这一条。"""
        import shutil
        found = shutil.which("curl")
        if not found:
            raise unittest.SkipTest("本机没有 curl")
        return found


class QuotedBodyDocumentationTestCase(unittest.TestCase):
    """放行 `"` 之后，**说不放行的地方必须全改掉**。

    这条守的是这一轮真正的风险：代码放宽了，而提示词、工具描述、技能手册、界面说明书
    还在教「引号在任何档都不放行，改用 `-d @payload.json`」。症状是模型读到手写的那三句
    改写规则，把一条本来能过的命令改成一条**落不了地**的（`run_tool` 的 cwd 是任务目录），
    然后放弃——与放行之前一样，只是多绕一步。

    它是**文档一致性**用例，不是行为用例：行为在 `QuotedBodyRoundTripTestCase` 里。两份
    都要有，因为文档与代码分叉时两边都不报错——读文档的人与跑代码的人看到的是两套规则。
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def read(self, *parts):
        with open(os.path.join(self.ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_the_reject_message_says_quotes_pass(self):
        """拒绝消息本身是第一现场：被拒的人只看得到它。"""
        with self.assertRaises(toolkit.ToolkitError) as caught:
            toolkit.validate_argument('{"a":"b&c"}', "参数值", data=True)
        message = str(caught.exception)
        self.assertIn("引号是放行的", message)
        self.assertIn("内联 JSON", message)

    def test_the_system_prompt_no_longer_forbids_quotes(self):
        """系统提示里那三条改写规则的第一条，已经从「改用文件」变成「直接传」。"""
        text = self.read("pentestd", "chat.py")
        self.assertIn("引号是放行的", text)
        self.assertNotIn('`& | ^ < > "` 在任何档都不放行', text)

    def test_the_skills_agree_with_the_code(self):
        """技能手册在同一件事上不能说反话——它们是模型跑任务时真去读的那一份。"""
        for name in ("web-surface.md", "service-fingerprint.md"):
            body = self.read("data", "skills", name)
            self.assertNotIn('`& | ^ < > "` 在任何档都不放行', body,
                             "%s 还在说引号不放行" % name)

    def test_the_data_flag_documentation_mentions_the_body(self):
        """`dataFlags` 那一行是给人改 `tools.json` 时看的，得说清它收哪些值。"""
        text = self.read("pentestd", "toolconfig.py")
        self.assertIn("内联 JSON 请求体", text)


if __name__ == "__main__":
    unittest.main()
