# -*- coding: utf-8 -*-
"""载荷投递与落地确认：判定三档、反反射纪律、配额、授权闸。

这一组用例针对的是**判定规则本身**，不是"能不能发出请求"。所以夹具是一台真实的
本地 HTTP 服务，每个路由各自体现一种"看起来像落地"的形态：

* `/upload` + `/uploads/*` —— 真的存下我们投的正文并能取回（→ `landed`）
* `/upload-fixed`          —— 存住了，但存的是它页面里本来就有的那个串（**不是**
                              `landed`：判据必须是我们本次生成的标记）
* `/upload-reject`         —— 明确拒绝（HTTP 403，→ `not_landed`）
* `/exec`                  —— 真"执行"命令并回显（→ `landed`）
* `/blind`                 —— 收下命令、什么都不回（→ `undetermined`，**不是**
                              `not_landed`）
* `/reflect`               —— 把收到的请求原样回显（朴素判据在这里会造假"已落地"）
* `/callback`              —— 真的按命令里的地址回连一次（→ 带外判据 `landed`）

这几条放在一起，才说明白"为什么同一个 200 有时是落地、有时什么都不是"。
"""
import os
import re
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pentestd import authz, engine, exploit, oast, shell
from pentestd.store import Store

# 本文件里那几条授权用例守的是**闸照判**时的拒绝（类别没开、目标不在白名单里），
# 所以这一模块内把 `authz.FORCE_OPEN` 关掉；跑完还原。
_OPEN = authz.FORCE_OPEN


def setUpModule():
    authz.FORCE_OPEN = False


def tearDownModule():
    authz.FORCE_OPEN = _OPEN


# 夹具的"磁盘"与"命令历史"。判定必须靠真实副作用，所以这些都记在目标侧。
UPLOADS = {}
RECEIVED = []            # 目标侧收到的每一个请求 (method, path)
COMMANDS = []            # 目标侧收到的命令原文（已解码）
CALLBACK_ERRORS = []     # 夹具替目标发起的回连失败原因
UPLOAD_COUNT = [0]       # 收到的投递请求数（配额用例要数准它）

# 目标页面里**本来就存在**的、长得像我们标记的串。
# 拿它当判据就是把"目标有个字"当成"我投的东西生效了"。
STATIC_LOOKALIKE = "PTMdeadbeef0000"

MARKER_RE = re.compile(r"PTM[0-9a-f]{12}")
SPLIT_RE = re.compile(r"\$\(printf\s+'PTM%s'\s+([0-9a-f]{12})\)")


def _emulate(command: str) -> str:
    """把夹具当成一台最小 shell，只认我们真正会发出去的两种形态。

    * `$(printf 'PTM%s' <尾巴>)` —— 命令替换，执行后输出标记（默认展开形态）
    * `echo <字面串>`          —— 字面回显（`markerStyle=literal` 的形态）
    """
    out = []
    for found in SPLIT_RE.finditer(command or ""):
        out.append("PTM" + found.group(1))
    for found in re.finditer(r"\becho\s+([^\s;&|]+)", command or ""):
        token = found.group(1)
        if "PTM" in token and token not in out:
            out.append(token)
    return "".join(out)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):        # 不让夹具往 stderr 写访问日志
        return

    def _send(self, status, body=b"", ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ------------------------------------------------------------ GET

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        query = urllib.parse.parse_qs(parts.query)
        RECEIVED.append(("GET", path))

        if path.startswith("/uploads/"):
            name = path.rsplit("/", 1)[-1]
            if name in UPLOADS:
                return self._send(200, UPLOADS[name], "text/plain")
            return self._send(404, "not found")
        if path.startswith("/exec"):
            command = (query.get("cmd") or [""])[0]
            COMMANDS.append(command)
            out = _emulate(command)
            return self._send(200, "cmd output: %s" % (out or "none"))
        if path.startswith("/callback"):
            command = (query.get("cmd") or [""])[0]
            COMMANDS.append(command)
            target = ""
            found = re.search(r"https?://[^\s'\"&|;]+", command)
            if found:
                target = found.group(0)
                try:
                    # 夹具替"被投递的目标"真的回连一次。判定必须来自这次真实的
                    # 往返，而不是一个被 mock 出来的注册表回答。
                    with urllib.request.urlopen(target, timeout=5) as response:
                        response.read()
                except Exception as exc:              # noqa: BLE001
                    CALLBACK_ERRORS.append(repr(exc))
            return self._send(200, "called %s" % target)
        if path.startswith("/reflect"):
            # 把收到的请求原样打进正文（错误页、回显型调试接口的形态）。
            return self._send(200, "you sent: %s" % parts.query)
        if path.startswith("/blind"):
            return self._send(200, "accepted")
        return self._send(404, "not found")

    # ------------------------------------------------------------ POST

    def do_POST(self):
        parts = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        RECEIVED.append(("POST", parts.path))
        text = body.decode("utf-8", "replace")

        if parts.path.startswith("/upload"):
            UPLOAD_COUNT[0] += 1
            found = re.search(r'filename="([^"]+)"', text)
            if parts.path == "/upload-reject":
                return self._send(403, "forbidden")
            if not found:
                return self._send(400, "no filename")
            if parts.path == "/upload-fixed":
                # "存住了"，但存的是页面上本来就有的那个串。
                UPLOADS[found.group(1)] = STATIC_LOOKALIKE
                return self._send(201, "uploaded")
            content = text.rsplit("\r\n\r\n", 1)[-1].split("\r\n--")[0]
            UPLOADS[found.group(1)] = content
            return self._send(201, "uploaded")
        if parts.path.startswith("/exec"):
            fields = urllib.parse.parse_qs(text)
            command = (fields.get("cmd") or [""])[0]
            COMMANDS.append(command)
            out = _emulate(command)
            return self._send(200, "cmd output: %s" % (out or "none"))
        return self._send(404, "not found")


class Fixture(unittest.TestCase):
    """起一台本地夹具服务。端口让系统分配，测试之间互不干扰。"""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        UPLOADS.clear()
        COMMANDS.clear()
        CALLBACK_ERRORS.clear()
        UPLOAD_COUNT[0] = 0
        del RECEIVED[:]

    # ------------------------------------------------------------ 夹具辅助

    def grant(self, targets=None, enabled=None):
        return {"grant": "ENG-TEST-0001", "enabled": enabled or ["all"],
                "targets": targets or ["127.0.0.1"]}

    def traffic(self):
        """目标侧看到的请求数。用来证明"闸门没过时一个包都没发出去"。"""
        return len(RECEIVED)

    def upload(self, point="/upload", fetch="/uploads/{{marker}}.txt", **kwargs):
        args = {"fetchPath": fetch}
        args.update(kwargs.pop("args", {}))
        grants = kwargs.pop("grants", None) or self.grant()
        return shell.run(self.base, mode="upload", paths=[point], args=args,
                         grants=grants, **kwargs)

    def command(self, template="id; echo {{marker}}", path="/exec", **kwargs):
        args = {"command": template}
        args.update(kwargs.pop("args", {}))
        grants = kwargs.pop("grants", None) or self.grant()
        return shell.run(self.base, mode="command", paths=[path], args=args,
                         grants=grants, **kwargs)


class _FakeRegistry:
    """最小回连监听替身。只用 `shell` 真正依赖的那部分形态。

    真监听器由 `tests/test_oast.py` 用真实 UDP/TCP 覆盖；这里要钉的是
    "回连命中 → landed"这条规则的接线，与传输无关。
    """

    def __init__(self, hit=True, endpoint=None):
        self.endpoint = endpoint or {
            "httpHost": "127.0.0.1", "httpPort": 9999,
            "dnsHost": "127.0.0.1", "dnsPort": 9999,
            "publicHost": "127.0.0.1", "domain": "oast.local", "baseUrl": ""}
        self.hit = hit
        self.registered = []
        self.waited = []

    def register(self, token, label=""):
        self.registered.append((token, label))
        return token

    def hits(self, token=None):
        return []

    def wait(self, token, timeout_s):
        self.waited.append(token)
        known = [item[0] for item in self.registered]
        if self.hit and token in known:
            return {"token": token, "protocol": "http", "sourceIp": "10.0.0.9",
                    "path": "/oast/%s" % token, "elapsedMs": 9.0}
        return None


class UploadTestCase(Fixture):
    """投递与取回：落地确认只认我们本次生成的标记。"""

    def test_upload_then_fetch_is_landed(self):
        result = self.upload()
        entry = result["uploads"][0]
        self.assertEqual("landed", entry["verdict"],
                         "取回正文里带着本次证据标记，说明载荷真的落地了")
        self.assertEqual("fetch", entry["evidenceKind"])
        self.assertEqual(1, result["summary"]["landed"])
        self.assertEqual(0, result["summary"]["undetermined"])
        self.assertEqual(1, result["summary"]["uploads"])
        self.assertEqual(2, result["requests"], "一次投递 + 一次取回")

    def test_landed_upload_registers_a_foothold(self):
        foothold = self.upload()["foothold"]
        self.assertIsNotNone(foothold, "落地确认成功必须给出立足点")
        self.assertEqual("webshell", foothold["kind"])
        self.assertEqual("webshell", foothold["capability"])
        self.assertEqual("127.0.0.1", foothold["host"])
        self.assertEqual(self.port, foothold["port"])
        self.assertTrue(foothold["id"].startswith("webshell-"))
        self.assertIn("shell", foothold["tags"])
        self.assertEqual([], foothold["scope"],
                         "没观测到背后网段就留空，不能编一个")
        self.assertIn("不", foothold["note"].replace("**", ""),
                      "note 必须说清它证明的是什么、不证明什么")

    def test_foothold_scope_comes_from_the_operator(self):
        foothold = self.upload(args={"scope": ["10.0.0.0/24"]})["foothold"]
        self.assertEqual(["10.0.0.0/24"], foothold["scope"])

    def test_no_landing_means_no_foothold(self):
        # 没有落地就没有立足点：给它一个默认值会让下游 pivot 去连一台根本
        # 没被控制的机器，而那种故障表现为"上一步说拿到了、这一步说连不上"。
        result = self.upload(point="/upload-reject")
        self.assertIsNone(result["foothold"])
        self.assertEqual(0, result["summary"]["landed"])

    def test_static_lookalike_on_the_target_is_not_evidence(self):
        """目标页面里本来就有的、长得像标记的串，不能当判据。

        这一条是"把目标有个字当成我投的东西生效了"的唯一防线，所以在用例里
        用一个常量把它钉死：证据标记必须由我们现场生成，且必须在**取回的正文**
        里出现。
        """
        result = self.upload(point="/upload-fixed")
        entry = result["uploads"][0]
        self.assertNotEqual("landed", entry["verdict"])
        self.assertEqual("not_landed", entry["verdict"])
        self.assertNotEqual(STATIC_LOOKALIKE, entry["evidenceMarker"],
                            "判据标记必须现场生成，不能是目标页面里的常量")
        self.assertRegex(entry["evidenceMarker"], MARKER_RE)
        self.assertIsNone(result["foothold"])

    def test_caller_supplied_marker_is_ignored(self):
        # 允许调用方指定判据串，等于允许把"目标页面里本来就有的字"变成证据。
        result = self.upload(point="/upload-fixed",
                             args={"marker": STATIC_LOOKALIKE})
        entry = result["uploads"][0]
        self.assertNotEqual(STATIC_LOOKALIKE, entry["marker"])
        self.assertNotEqual(STATIC_LOOKALIKE, entry["evidenceMarker"])
        self.assertNotEqual("landed", entry["verdict"])

    def test_rejected_upload_is_not_landed(self):
        result = self.upload(point="/upload-reject")
        entry = result["uploads"][0]
        self.assertEqual("not_landed", entry["verdict"], "被拒是否定观测，不是'不知道'")
        self.assertIn("403", entry["reason"])
        self.assertEqual(1, result["summary"]["notLanded"])

    def test_evidence_marker_never_travels_in_a_request(self):
        # 判据标记只应出现在载体正文里。它一旦出现在请求行里，一个把请求回显
        # 回来的 404 页面就足以造出一次假的"落地"。
        result = self.upload()
        entry = result["uploads"][0]
        self.assertIn(entry["marker"], entry["fetchRequest"],
                      "文件名标记用的是文件名标记")
        self.assertNotIn(entry["evidenceMarker"], entry["fetchRequest"],
                         "证据标记不能出现在求证的请求里")
        self.assertNotIn(entry["evidenceMarker"], entry["request"])
        self.assertFalse(entry["reflected"])

    def test_multiple_points_stop_at_max_uploads(self):
        # 配额在**发出去之前**检查：多投出去的每一个都已经在目标上留了东西。
        result = shell.run(self.base, mode="upload",
                           paths=["/upload", "/upload2", "/upload3"],
                           args={"fetchPath": "/uploads/{{marker}}.txt"},
                           grants=self.grant(),
                           limits={"maxUploads": 2})
        self.assertEqual(2, UPLOAD_COUNT[0],
                         "目标侧必须**真的**只收到 2 次投递")
        self.assertEqual(2, result["summary"]["uploads"])
        self.assertEqual(2, len(result["uploads"]))
        self.assertTrue(any("上限" in note for note in result["degraded"]),
                        "被上限截停必须如实报告，而不是静默少跑两个点")
        self.assertIn("/upload3", " ".join(result["degraded"]))


class CommandTestCase(Fixture):
    """单条命令：`landed` 与 `undetermined` 的界线，以及反反射。"""

    def test_echoed_marker_is_landed(self):
        result = self.command()
        entry = result["commands"][0]
        self.assertEqual("landed", entry["verdict"])
        self.assertEqual("echo", entry["evidenceKind"])
        self.assertEqual(1, result["summary"]["landed"])

    def test_marker_is_not_sent_in_the_clear(self):
        """发出去的是命令的**两半**，不是标记本身。

        朴素写法 `;echo PTM<hex>` 里的那个串在请求里原样出现过，于是任何把请求
        回显回来的端点都会让判据"命中"。默认展开成 `$(printf 'PTM%s' <尾巴>)`
        之后，目标侧看到的命令里没有那个连续串，而执行后的输出里有。
        """
        result = self.command()
        entry = result["commands"][0]
        received = COMMANDS[-1]
        self.assertIn("printf", received, "目标侧看到的应当是命令替换形态")
        self.assertNotIn(entry["evidenceMarker"], received,
                         "判据串不能在发出去的字节里连续出现")
        self.assertNotIn(entry["evidenceMarker"], entry["request"])
        self.assertFalse(entry["reflected"])

    def test_silent_command_is_undetermined_not_not_landed(self):
        """命令无回显且无回连 → 未判定。

        这是本模块唯一真正重要的那道界线：`id > /tmp/x`、`curl ...` 都可以一声
        不响地成功，"没回显"推不出"没执行"。把它记成"没落地"，使用者会据此把
        这一类攻击面从范围里划掉，而真相是那一次根本没有观测手段。
        """
        result = self.command(path="/blind")
        entry = result["commands"][0]
        self.assertEqual("undetermined", entry["verdict"])
        self.assertNotEqual("not_landed", entry["verdict"])
        self.assertEqual(0, result["summary"]["notLanded"])
        self.assertEqual(1, result["summary"]["undetermined"])
        self.assertIn("没观测到不等于没执行", entry["reason"])
        self.assertIsNone(result["foothold"], "未判定不能长出立足点")

    def test_literal_marker_echo_is_reflected_not_landed(self):
        # 字面形态（cmd.exe 之类用不了 `$( )` 的场景）：命中允许，但不许升级。
        # 目标把我们的命令原样回显时，那只是一次反射，不是一次执行。
        result = self.command(template="echo {{marker}}", path="/exec",
                              args={"markerStyle": "literal"})
        entry = result["commands"][0]
        self.assertIn(entry["evidenceMarker"], COMMANDS[-1],
                      "字面形态下判据串确实会出现在命令里")
        self.assertTrue(entry["reflected"], "必须把那一次标成反射")
        self.assertNotEqual("landed", entry["verdict"])
        self.assertEqual("undetermined", entry["verdict"])
        self.assertIn("反射", entry["reason"])

    def test_reflecting_endpoint_cannot_forge_a_landing(self):
        # 把收到的请求原样打进正文的端点：默认展开下判据不在请求里，所以
        # 它既造不出"已落地"，也不该被记成"未落地"（那是一次没观测到）。
        result = self.command(path="/reflect")
        entry = result["commands"][0]
        self.assertNotEqual("landed", entry["verdict"])
        self.assertEqual("undetermined", entry["verdict"])
        self.assertFalse(entry["reflected"])

    def test_command_template_is_recorded_for_replay(self):
        result = self.command()
        entry = result["commands"][0]
        self.assertIn("printf", entry["command"], "报告里要说得清这次发的是什么")
        self.assertIn("cmd=", entry["position"])

    def test_execution_budget_stops_at_max_commands(self):
        result = shell.run(self.base, mode="command",
                           paths=["/exec", "/exec2", "/exec3"],
                           args={"command": "id; echo {{marker}}"},
                           grants=self.grant(), limits={"maxCommands": 2})
        self.assertEqual(2, len(COMMANDS), "目标侧必须**真的**只收到 2 条命令")
        self.assertEqual(2, result["summary"]["commands"])
        self.assertTrue(any("上限" in note for note in result["degraded"]))


class CallbackTestCase(Fixture):
    """带外回连：命令不回显时唯一的判据。"""

    def test_callback_hit_is_landed(self):
        registry = _FakeRegistry(hit=True)
        result = self.command(template="curl {{oastUrl}}", path="/callback",
                              registry=registry,
                              limits={"callbackWaitS": 0.5})
        entry = result["commands"][0]
        self.assertEqual("landed", entry["verdict"])
        self.assertEqual("callback", entry["evidenceKind"])
        self.assertTrue(entry["evidence"], "判定里必须留下回连记录本身")
        # 先登记、再等待：等一个没人登记的 token 是查不出任何东西的。
        self.assertEqual(1, len(registry.registered))
        self.assertEqual([registry.registered[0][0]], registry.waited)
        self.assertIn("/oast/", COMMANDS[-1], "命令里必须真的带上回连地址")

    def test_callback_timeout_is_undetermined_not_not_landed(self):
        registry = _FakeRegistry(hit=False)          # 从不命中
        result = self.command(template="curl {{oastUrl}}", path="/callback",
                              registry=registry,
                              limits={"callbackWaitS": 0.5})
        entry = result["commands"][0]
        self.assertEqual("undetermined", entry["verdict"],
                         "窗口内没回连只能说明没观测到，不能说明不成立")
        self.assertEqual(0, result["summary"]["notLanded"])

    def test_real_listener_callback_is_landed(self):
        """真监听器 + 真回连：夹具替目标发一次真实 HTTP 请求。

        用假注册表能钉住接线，但钉不住"回连地址真的可达、token 真的对得上"。
        这一条让两者都跑一遍真 socket。
        """
        listener = oast.Listener(http_host="127.0.0.1", http_port=0,
                                 dns_host="127.0.0.1", dns_port=0)
        listener.start()
        try:
            result = self.command(template="curl {{oastUrl}}", path="/callback",
                                  registry=listener,
                                  limits={"callbackWaitS": 3.0})
        finally:
            listener.stop()
        entry = result["commands"][0]
        self.assertEqual([], CALLBACK_ERRORS, "夹具发出的回连本身不能失败")
        self.assertEqual("landed", entry["verdict"])
        self.assertEqual("http", entry["evidence"].get("protocol"))
        self.assertTrue(str(entry["evidence"].get("sourceIp") or "").startswith(
            "127.0.0.1"), "回连来源就是夹具自己")

    def test_callback_reference_without_listener_is_refused(self):
        # 回连地址取不到值时必须在**发包之前**停：留空会让载荷变成一个指向空
        # 字符串的请求，而报告仍按"投递了载荷"记。
        with self.assertRaises(shell.ShellError) as caught:
            self.command(template="curl {{oastUrl}}", path="/callback")
        self.assertIn("oast", str(caught.exception))
        self.assertEqual(0, self.traffic(), "一个包都不该发出去")

    def test_without_callback_reference_the_limit_is_reported(self):
        result = self.command()
        self.assertTrue(any("带外回连" in note for note in result["degraded"]),
                        "只有回显这一个判据时，要如实说明缺什么观测手段")


class GrantTestCase(Fixture):
    """准入闸：执行侧必须自己再判一次，且判定不过就一个包都不发。"""

    def test_run_without_grant_is_refused(self):
        with self.assertRaises(authz.AuthzError):
            shell.run(self.base, mode="upload", paths=["/upload"],
                      args={"fetchPath": "/uploads/{{marker}}.txt"})
        self.assertEqual(0, self.traffic())

    def test_grant_text_too_short_is_refused(self):
        grants = {"grant": "短", "enabled": ["all"], "targets": ["127.0.0.1"]}
        with self.assertRaises(authz.AuthzError):
            self.upload(grants=grants)
        self.assertEqual(0, self.traffic())

    def test_class_not_enabled_is_refused(self):
        grants = {"grant": "ENG-TEST-0001", "enabled": ["exploit"],
                  "targets": ["127.0.0.1"]}
        with self.assertRaises(authz.AuthzError):
            self.upload(grants=grants)
        self.assertEqual(0, self.traffic())

    def test_target_outside_allowlist_is_refused(self):
        with self.assertRaises(authz.AuthzError):
            self.upload(grants=self.grant(targets=["10.0.0.0/8"]))
        self.assertEqual(0, self.traffic())

    def test_refusal_is_not_swallowed_into_a_verdict(self):
        # 准入不足不是"未判定"：两者处置完全不同（一个是去要授权，一个是补一个
        # 观测手段）。混起来会让一次越权的任务看起来像一次部分成功的任务。
        with self.assertRaises(authz.AuthzError):
            self.command(grants={"grant": "", "enabled": [], "targets": []})
        self.assertEqual(0, self.traffic())


class ParameterTestCase(Fixture):
    """参数错误在发包之前就要报出来，而不是投出去一半再发现。"""

    def test_missing_fetch_path_is_refused(self):
        with self.assertRaises(shell.ShellError) as caught:
            shell.run(self.base, mode="upload", paths=["/upload"],
                      args={}, grants=self.grant())
        self.assertIn("fetchPath", str(caught.exception))
        self.assertEqual(0, self.traffic(), "缺取回路径时不该先把文件投出去")

    def test_missing_delivery_point_is_refused(self):
        with self.assertRaises(shell.ShellError):
            shell.run(self.base, mode="upload",
                      args={"fetchPath": "/uploads/{{marker}}.txt"},
                      grants=self.grant())
        self.assertEqual(0, self.traffic())

    def test_missing_command_is_refused(self):
        with self.assertRaises(shell.ShellError):
            shell.run(self.base, mode="command", paths=["/exec"],
                      args={}, grants=self.grant())
        self.assertEqual(0, self.traffic())

    def test_command_without_any_criterion_is_refused(self):
        # 命令里既没有标记也没有回连地址时，它无论执行成败都只会得到"未判定"，
        # 等于白投一次（还在目标上留了痕迹）。
        with self.assertRaises(shell.ShellError) as caught:
            self.command(template="id")
        self.assertIn("判据", str(caught.exception))
        self.assertEqual(0, self.traffic())

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(shell.ShellError):
            shell.run(self.base, mode="wget", paths=["/upload"],
                      args={"fetchPath": "/x"}, grants=self.grant())
        self.assertEqual(0, self.traffic())

    def test_bad_marker_style_is_refused(self):
        with self.assertRaises(shell.ShellError):
            self.command(args={"markerStyle": "clever"})
        self.assertEqual(0, self.traffic())

    def test_unknown_template_variable_is_a_loud_error(self):
        # 留空会让取回路径退化成 `/uploads/.txt`，一个必然 404 的请求，而报告
        # 仍按"投了载荷、去取回了"记 —— 一次假的"没落地"。
        with self.assertRaises(shell.ShellError) as caught:
            self.upload(fetch="/uploads/{{nope}}.txt")
        self.assertIn("nope", str(caught.exception))
        self.assertEqual(0, self.traffic())


class OutputLimitTestCase(Fixture):
    """`shell.maxOutputBytes` 必须真的限住**判据看到多少正文**。

    这条配额以前只出现在两个地方：`authz.LIMITS` 的定义与报告的 `limits` 字段。
    执行侧一次都没读过它 —— 于是响应多大就读多大，判据在整段正文上跑，而报告
    仍然印着"单次输出上限 65536 字节"。上限没实现时，它给出的是一种不存在的保证。

    这里的判据是"截断发生了、并且被说出来"，不是"抛异常"：读得多不是错误，
    假装读全了才是。
    """

    def test_a_truncated_echo_is_reported_as_undetermined_not_as_executed(self):
        # 标记在正文末尾，上限只有 4 字节 → 判据根本没看到它。
        # 这时必须给未判定（"我没看完"），不能给已落地，也不能给"没执行"。
        result = self.command("id; echo {{marker}}", limits={"maxOutputBytes": 4})
        entry = result["commands"][0]
        self.assertTrue(entry.get("truncated"), "截断必须被记下来")
        self.assertEqual(4, entry.get("outputLimit"))
        self.assertGreater(entry.get("outputBytes"), 4, "要记原文字节数，不是截断后的")
        self.assertEqual("undetermined", entry["verdict"])
        self.assertIn("只判定了前 4 字节", entry["reason"])
        self.assertEqual(1, result["summary"]["truncated"])

    def test_a_generous_limit_still_reaches_the_marker(self):
        # 反向的一半：上限够大时判定必须照旧成立，别把配额做成一律未判定。
        result = self.command("id; echo {{marker}}", limits={"maxOutputBytes": 4096})
        entry = result["commands"][0]
        self.assertEqual("landed", entry["verdict"])
        self.assertNotIn("truncated", entry)
        self.assertEqual(0, result["summary"]["truncated"])

    def test_a_truncated_fetch_does_not_become_a_negative_verdict(self):
        # 取回成功但只看了前 4 字节：这时说"文件没落地"是把"没看完"讲成"没有"。
        result = self.upload(limits={"maxOutputBytes": 4})
        entry = result["uploads"][0]
        self.assertTrue(entry.get("truncated"))
        self.assertEqual("undetermined", entry["verdict"])
        self.assertIn("shell.maxOutputBytes", entry["reason"])

    def test_a_complete_fetch_is_still_landed(self):
        result = self.upload(limits={"maxOutputBytes": 4096})
        self.assertEqual("landed", result["uploads"][0]["verdict"])

    def test_the_limit_comes_from_the_grant_snapshot_not_a_constant(self):
        # 两个上限跑同一条命令，结果必须不同 —— 否则说明写死了一个数。
        tight = self.command("id; echo {{marker}}", limits={"maxOutputBytes": 4})
        loose = self.command("id; echo {{marker}}", limits={"maxOutputBytes": 4096})
        self.assertNotEqual(tight["commands"][0]["verdict"],
                            loose["commands"][0]["verdict"])


class ChainSpecTestCase(Fixture):
    """链路契约：`{kind, localRef, payload}` + `$ref`，而且要真的写得进去。"""

    @staticmethod
    def _refs(payload):
        found = []
        if isinstance(payload, dict):
            if set(payload) == {"$ref"}:
                return [payload["$ref"]]
            for value in payload.values():
                found += ChainSpecTestCase._refs(value)
        elif isinstance(payload, list):
            for item in payload:
                found += ChainSpecTestCase._refs(item)
        return found

    def _specs(self, result):
        return shell.build_chain_specs(result)

    def test_specs_have_kinds_refs_and_resolvable_links(self):
        specs = self._specs(self.upload())
        kinds = {spec["kind"] for spec in specs}
        self.assertTrue({"asset", "intent", "fact", "finding"} <= kinds)
        local_refs = [spec["localRef"] for spec in specs]
        self.assertEqual(len(local_refs), len(set(local_refs)), "localRef 必须唯一")
        for spec in specs:
            self.assertTrue(spec.get("localRef"), "每个规格都要有 localRef")
            self.assertIn("payload", spec)
        for spec in specs:
            for ref in self._refs(spec["payload"]):
                # `goal` 是写入器预置的引用（指向每个 engagement 固定的 goal-1），
                # 不属于本模块产出的 localRef，但同样必须能被解析。
                self.assertTrue(ref in local_refs or ref == engine.GOAL_REF,
                                "引用了不存在的节点：%s" % ref)
        # `$ref` 只能在**前面**定义过（goal 由写入器预置）：engine.write_chain
        # 按序解析，引到一个尚未产生的名字会让整条链路一个节点都写不进去。
        seen = {engine.GOAL_REF}
        for spec in specs:
            for ref in self._refs(spec["payload"]):
                self.assertIn(ref, seen, "%s 引用了后面才定义的 %s"
                              % (spec["localRef"], ref))
            seen.add(spec["localRef"])

    def test_specs_actually_write_into_a_real_chain(self):
        """把规格喂给真的写入器。结构对不代表写得进去——锚点、父资产、可复现
        步骤这几条只有 `store.add_node` 才会校验。"""
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        store = Store(os.path.join(directory.name, "chain.db"))
        try:
            engagement = store.create_engagement(self.base, "落地确认", "ENG-TEST-0001")
            written = engine.write_chain(store, engagement["id"],
                                         self._specs(self.upload()))
            nodes = store.list_nodes(engagement["id"])
        finally:
            store.close()
            directory.cleanup()
        self.assertEqual(1, written["intent"])
        self.assertEqual(1, written["finding"])
        self.assertGreaterEqual(written["asset"], 2, "目标资产 + 立足点资产")
        by_kind = {node["kind"]: node for node in nodes}
        # 锚点必须真的落到 goal-1 上：写一个字面串（例如 "GOAL-PENTEST"）会让
        # intent 的锚点校验失败，整条链路一个节点都写不进去。
        self.assertEqual("goal-1", by_kind["intent"]["payload"]["goalId"])
        assets = [node["payload"].get("value") for node in nodes
                  if node["kind"] == "asset"]
        self.assertIn(self.base + "/upload", assets)
        findings = [node["payload"] for node in nodes if node["kind"] == "finding"]
        self.assertEqual(1, len(findings))
        self.assertTrue(findings[0]["reproducibleSteps"],
                        "交付项必须有可复现步骤，否则写入器会拒收")

    def test_only_landed_produces_findings(self):
        landed = self._specs(self.upload())
        self.assertEqual(1, len([s for s in landed if s["kind"] == "finding"]))
        silent = self._specs(self.command(path="/blind"))
        self.assertEqual([], [s for s in silent if s["kind"] == "finding"],
                         "未判定不能生成交付项")
        rejected = self._specs(self.upload(point="/upload-reject"))
        self.assertEqual([], [s for s in rejected if s["kind"] == "finding"],
                         "未落地不能生成交付项")

    def test_undetermined_and_not_landed_are_kept_apart(self):
        silent = [s for s in self._specs(self.command(path="/blind"))
                  if s["localRef"].startswith("fact:undetermined")]
        self.assertEqual(1, len(silent))
        self.assertIn("未判定", silent[0]["payload"]["detail"])
        self.assertIn("不是「否」", silent[0]["payload"]["detail"])
        rejected = [s for s in self._specs(self.upload(point="/upload-reject"))
                    if s["localRef"].startswith("fact:notlanded")]
        self.assertEqual(1, len(rejected))
        self.assertIn("未落地", rejected[0]["payload"]["detail"])

    def test_no_foothold_means_no_foothold_asset(self):
        specs = self._specs(self.command(path="/blind"))
        self.assertEqual([], [s for s in specs if s["localRef"] == "asset:foothold"])

    def test_foothold_asset_is_parented_on_the_target(self):
        specs = self._specs(self.upload())
        foothold = [s for s in specs if s["localRef"] == "asset:foothold"]
        self.assertEqual(1, len(foothold))
        self.assertEqual({"$ref": "asset:root"}, foothold[0]["payload"]["parentId"])
        findings = [s for s in specs if s["kind"] == "finding"]
        self.assertEqual({"$ref": "asset:foothold"},
                         findings[0]["payload"]["affectedAssetId"])


if __name__ == "__main__":
    unittest.main()
