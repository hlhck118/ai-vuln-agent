# -*- coding: utf-8 -*-
"""五类侵入式动作的端到端实跑：真控制面 → 真队列 → 真执行 → 真链路。

## 补的是哪一段

`tests/test_intrusive_wiring.py` 证明的是**准入**——请求进得了队列、被拒时队列
不长。`test_exploit.py` / `test_creds.py` / `test_shell.py` / `test_pivot_ops.py` /
`test_toolkit.py` 证明的是各自的**判据**。两批都是绿的，用户仍然可能一件事都
做不到：中间还隔着 `kind` 分发、载荷白名单、`grant` 下发、结果回写、链路写入，
而"接线断了"这四种症状只在**真的连着跑一次**的时候才现形。

这个脚本只做这一件事：起一个真会中招的目标，把五类各下发一次，让本地执行
节点真的执行，然后把控制面里长出来的链路节点打出来。

## 目标不是 mock

夹具自己会去做这些事：

* 去 GET 我们给的带外回连地址（所以 SSRF 的"已证" = 回连真的到了）
* 按 `printf <标记>` 真的回显那个标记（所以命令注入的"已证" = 我们的标记被回显）
* 把上传的文件按名字存下来，再从 `/uploads/<名字>` 吐回去
* 带伪装头时放行 `/admin`，不带时 403（所以请求头绕过是真的绕过了）
* 按 Basic 凭据决定 401 还是 200

所以每一类的结论都建立在**目标真的做了那件事**之上，而不是"我们的判据说它
做了"。这一点是刻意的：一个什么都答应的 mock 会让五类全部"跑通"，包括那些
实现错了的。

跑法::

    python tools/intrusive_e2e.py
"""

import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agent, authz, pivot, toolkit                    # noqa: E402
from tests import support                                            # noqa: E402

AUTHORIZATION = "E2E-0001 端到端夹具，目标为本机自建靶机"

BYPASS_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Original-URL",
                  "X-Rewrite-URL", "X-Real-IP")

# 夹具自己的 Basic 凭据。creds 的 http-basic 判据是"状态码不是 401/407 且响应里
# 没有 WWW-Authenticate"，所以夹具必须**两个条件都满足**才算命中。
BASIC_USER = "svc-deploy"
BASIC_PASS = "Svc-Deploy-9x!"


# ---------------------------------------------------------------- 目标夹具

def _simulate_shell(value: str):
    """迷你 shell：只实现 `printf <字面量>`。

    命令注入引擎生成的载荷形如 `;printf 3f9a...`、`$(printf 3f9a...)`、
    `'printf 3f9a...'`。这里把 `printf` 后面那个字面量取出来——**只取一个词**，
    与真实 shell 的行为一致（多词要引号，而引擎给的就是单字面量）。
    取不到就返回 None，表示"这个输入不是一次命令注入"。
    """
    match = re.search(r"printf\s+(\S+)", value or "")
    if not match:
        return None
    token = match.group(1).strip("'\"`()$;|&")
    return token or None


def _multipart_parts(body: bytes, content_type: str):
    """极简 multipart 解析：返回 {字段名: (文件名或 None, 内容字节)}。

    不用 `cgi`：它在 3.13 被移除了，而这里只需要"取一个文件字段"。
    """
    match = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not match:
        return {}
    boundary = b"--" + match.group(1).strip().encode()
    found = {}
    for chunk in body.split(boundary):
        if b"\r\n\r\n" not in chunk:
            continue
        head, _, rest = chunk.partition(b"\r\n\r\n")
        if rest.endswith(b"\r\n"):
            rest = rest[:-2]
        head_text = head.decode("utf-8", "replace")
        name = re.search(r'name="([^"]*)"', head_text)
        filename = re.search(r'filename="([^"]*)"', head_text)
        if name:
            found[name.group(1)] = (filename.group(1) if filename else None, rest)
    return found


class TargetFixture:
    """一个真的会中招的 HTTP 目标。

    路由：
        GET  /                     200，基线可达
        GET  /admin                403；带伪装头或路径被归一化过时 200
        GET  /api/fetch?url=...    **真的去 GET 那个 URL**（SSRF）
        GET  /api/ping?host=...    按 `printf <标记>` 真的回显标记（命令注入）
        POST /upload               存下上传的文件，返回 201
        GET  /uploads/<名字>       把存下的内容吐回去
        GET  /protected            按 Basic 凭据决定 401 还是 200
        GET  /actuator/health      200（顺手给 forced-browsing 一个真可达的路径）
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.uploads = {}
        self.oast_fetches = []
        self.requests = []
        fixture = self
        handler = _make_handler(fixture)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def note(self, text: str):
        with self.lock:
            self.requests.append(text)

    def record_oast(self, url: str):
        with self.lock:
            self.oast_fetches.append(url)

    def store_upload(self, name: str, content: bytes):
        with self.lock:
            self.uploads[name] = content

    def fetch_upload(self, name: str):
        with self.lock:
            return self.uploads.get(name)


def _make_handler(fixture):
    """把 handler 做成闭包，夹具实例不必挂在 server 属性上。"""

    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.0：不带 Content-Length 时靠关连接表达"发完了"。用 1.1 会让
        # 只认 Content-Length 的客户端等一个永远不来的尾巴（这一条在写 OAST
        # 监听器时踩过一次，见 test_oast.py 的注释）。
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        # ------------------------------------------------------------ 工具

        def _send(self, status: int, body: bytes = b"", headers=None):
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _text(self, status: int, text: str, headers=None):
            self._send(status, text.encode("utf-8"),
                       dict({"Content-Type": "text/plain; charset=utf-8"},
                            **(headers or {})))

        def _query(self):
            from urllib.parse import parse_qsl
            _, _, raw = self.path.partition("?")
            return dict(parse_qsl(raw, keep_blank_values=True))

        def _raw_path(self):
            return self.path.split("?")[0]

        def _bypass_header_present(self):
            return any(self.headers.get(name) for name in BYPASS_HEADERS)

        def _normalized_admin(self):
            """把原始路径归一化，用来判定"路径归一化绕过"。

            真实网关的这类缺陷长这样：鉴权中间件按**原始**路径匹配，而路由按
            **归一化后**的路径匹配，两者对 `/admin/` 与 `/admin` 的看法不一致。
            """
            raw = self._raw_path()
            if "%2f" in raw.lower() or "%00" in raw.lower():
                raw = raw.replace("%2f", "/").replace("%2F", "/")
                raw = re.sub("%00.*$", "", raw, flags=re.I)
            raw = re.sub(r";.*$", "", raw)          # /admin;x → /admin
            raw = re.sub(r"/+", "/", raw)           # //admin → /admin
            raw = raw.replace("/./", "/")           # /./admin → /admin
            raw = raw.rstrip("/.")                  # /admin/ → /admin
            if raw.endswith(".json"):
                raw = raw[:-5]
            return raw or "/"

        # ------------------------------------------------------------ GET

        def do_GET(self):
            path = self._raw_path()
            query = self._query()
            fixture.note("GET %s" % self.path)

            if path == "/":
                return self._text(200, "ok")

            if path == "/actuator/health" or path == "/.git/config":
                return self._text(200, "fixture: 这个路径本来就是公开的")

            if path.startswith("/admin"):
                if self._bypass_header_present():
                    fixture.note("  ← 带伪装头，放行")
                    return self._text(200, "admin panel（经伪装头放行）")
                if self._normalized_admin() == "/admin" and path != "/admin":
                    fixture.note("  ← 原始路径 %r 归一化后是 /admin，放行" % path)
                    return self._text(200, "admin panel（经路径归一化放行）")
                return self._text(403, "forbidden",
                                  {"WWW-Authenticate": 'Basic realm="admin"'})

            if path == "/api/fetch":
                url = query.get("url") or query.get("host") or ""
                if not url.lower().startswith(("http://", "https://")):
                    return self._text(400, "缺少 url")
                return self._do_ssrf(url)

            if path == "/api/ping":
                value = query.get("host") or query.get("url") or ""
                echoed = _simulate_shell(value)
                if echoed:
                    fixture.note("  ← 按 printf 回显标记 %s" % echoed)
                    return self._text(200, "result: %s\n" % echoed)
                return self._text(200, "ping %s ok" % value)

            if path.startswith("/uploads/"):
                name = path[len("/uploads/"):]
                content = fixture.fetch_upload(name)
                if content is None:
                    return self._text(404, "no such upload")
                return self._send(200, content,
                                  {"Content-Type": "text/plain; charset=utf-8"})

            if path == "/protected":
                return self._do_basic_auth()

            return self._text(404, "not found")

        def _do_ssrf(self, url: str):
            """真的去 GET 那个 URL。SSRF 的"已证"必须建立在回连真的发生之上。"""
            fixture.record_oast(url)
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    payload = response.read(512)
                fixture.note("  ← 回连成功 %s（%d 字节）" % (url, len(payload)))
                return self._text(200, "fetched %s" % url)
            except Exception as exc:
                fixture.note("  ← 回连失败 %s（%r）" % (url, exc))
                return self._text(502, "fetch failed: %r" % (exc,))

        def _do_basic_auth(self):
            header = self.headers.get("Authorization") or ""
            if header.lower().startswith("basic "):
                import base64
                try:
                    raw = base64.b64decode(header.split(None, 1)[1]).decode("utf-8")
                    user, _, password = raw.partition(":")
                except Exception:
                    user, password = "", ""
                if user == BASIC_USER and password == BASIC_PASS:
                    # 命中：200 **且不带 WWW-Authenticate**（两条都要满足）
                    return self._text(200, "welcome %s" % user)
            return self._text(401, "unauthorized",
                              {"WWW-Authenticate": 'Basic realm="fixture"'})

        # ------------------------------------------------------------ POST

        def do_POST(self):
            path = self._raw_path()
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            fixture.note("POST %s（%d 字节）" % (path, len(body)))

            if path == "/upload":
                parts = _multipart_parts(
                    body, self.headers.get("Content-Type") or "")
                saved = []
                for _field, (filename, content) in parts.items():
                    if filename:
                        fixture.store_upload(filename, content)
                        saved.append(filename)
                if not saved:
                    return self._text(400, "没有收到文件字段")
                fixture.note("  ← 已存下 %s" % "、".join(saved))
                return self._text(201, "uploaded %s" % "、".join(saved))

            return self._text(404, "not found")

    return Handler


# ---------------------------------------------------------------- 跑一次任务

def _wait_job(store, job_id, timeout=300.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = store.get_job(job_id)
        if job and job.get("status") in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.2)
    return store.get_job(job_id)


def _graph(client, engagement_id):
    """取回链路。返回 (节点列表, 元信息)。

    **`nodes` 不是列表，是四个分组的字典**（`intents` / `facts` / `findings` /
    `assets`）——界面就是按这个分组画的。当成列表读会得到"0 个节点"，而那个
    数字看起来像"证据一个都没写进去"，非常容易误判。截断标记也一并带回来：
    视图是开窗的，不报截断就会把"窗口装不下"说成"只有这么多"。
    """
    status, payload = client.get("/api/v1/engagements/%s/graph" % engagement_id)
    if status != 200:
        return [], {}
    raw = payload.get("nodes")
    flat = []
    if isinstance(raw, dict):
        for group in raw.values():
            if isinstance(group, list):
                flat.extend(group)
            elif isinstance(group, dict):
                flat.append(group)
    elif isinstance(raw, list):
        flat = raw
    nodes = [node for node in flat if isinstance(node, dict)]
    if payload.get("goal"):
        nodes = [payload["goal"]] + nodes
    return nodes, (payload.get("engagement") or {})


def _as_dict(value):
    """把 `store.get_job` 给的原始行字段解成字典。

    `get_job` 返回的是**库里的那一行**，`payload` / `result` 都是 JSON 字符串；
    解成字典的是控制面那一侧的 `_row_to_job`。这里直接用会拿到 str，而
    `str.get(...)` 的报错与"引擎没返回结果"长得完全不像，很容易查错方向。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _summarize(kind, job):
    summary = _as_dict(_as_dict(job.get("result")).get("summary"))
    bits = []
    for key in ("verified", "findings", "static", "refuted", "undetermined",
                "attempts", "found", "landed", "notLanded", "probed", "open",
                "exitCode", "chainNodes"):
        if key in summary:
            bits.append("%s=%s" % (key, summary[key]))
    return " ".join(bits)


def main():
    directory, store = support.temporary_store("intrusive-e2e.db")
    fixture = TargetFixture().start()
    httpd = None
    executor = None
    stop_flag = threading.Event()
    proxy = None
    try:
        print("目标夹具   %s（真会中招，不是 mock）" % fixture.base)

        # 立足点：一个真的 SOCKS5 服务端。这是 pivot 的入口——"经它可达什么"
        # 必须建立在真的经它连过之上。
        proxy = pivot.Socks5Server(allow=["127.0.0.1/32"])
        proxy.start()
        foothold = pivot.build_foothold(
            "socks5", "127.0.0.1", proxy.endpoint["port"],
            note="e2e 夹具立足点", scope=["127.0.0.1/32"])
        foothold = (foothold.to_dict() if hasattr(foothold, "to_dict")
                    else dict(foothold))
        print("立足点     socks5 @ 127.0.0.1:%d（真 SOCKS5 服务端）"
              % proxy.endpoint["port"])

        httpd, client = support.start(
            store, intrusive=["all"],
            intrusive_targets=["127.0.0.1", "localhost"])
        base = client.base
        print("控制面     %s（侵入授权：五类全开）" % base)

        executor = agent.LocalExecutor(store, name="e2e-local", verbose=True)
        worker = threading.Thread(
            target=executor.serve_forever,
            kwargs={"interval": 0.2,
                    "should_stop": stop_flag.is_set},
            daemon=True)
        worker.start()
        print("执行节点   %s 在线\n" % executor.executor_id)

        status, meta = client.post("/api/v1/engagements", {
            "name": "E2E 侵入式动作实跑", "target": "127.0.0.1",
            "objective": "五类侵入式动作端到端验证",
            "authorization": AUTHORIZATION})
        engagement_id = (meta or {}).get("id") or (meta or {}).get("engagementId")
        print("Engagement %s\n" % engagement_id)

        closed_port = _closed_port()
        bodies = {
            "exploit": {
                "kind": "exploit", "engagementId": engagement_id,
                "target": fixture.base + "/",
                "authorization": AUTHORIZATION,
                # 模板要显式点名：引擎拒绝"没指定模板"（那会让一次任务到底
                # 测过什么变成不可复现的）。这里挑的是夹具真能满足判据的那几个。
                "templates": ["ssrf-callback", "cmd-injection-marker",
                              "auth-header-bypass", "auth-path-bypass",
                              "upload-marker", "forced-browsing"],
                "paths": ["/admin", "/api/fetch", "/api/ping",
                          "/.git/config", "/actuator/health"],
                "params": ["host", "url"],
                "args": {"uploadPath": "/upload", "uploadField": "file",
                         "uploadFetchPath": "/uploads/{{marker}}.txt"},
                # 回连地址必须是**目标能到达**的；本机夹具 → 本机监听
                "oast": {"httpHost": "127.0.0.1", "httpPort": 0,
                         "dnsHost": "127.0.0.1", "dnsPort": 0,
                         "publicHost": "127.0.0.1", "domain": "oast.local"},
            },
            "creds": {
                "kind": "creds", "engagementId": engagement_id,
                "target": "127.0.0.1:%d" % fixture.port,
                "driver": "http-basic", "authorization": AUTHORIZATION,
                "accounts": [BASIC_USER, "admin"],
                "passwords": [BASIC_PASS, "wrong-one", "wrong-two"],
                "options": {"path": "/protected"},
            },
            "shell": {
                "kind": "shell", "engagementId": engagement_id,
                "target": fixture.base + "/", "mode": "upload",
                "authorization": AUTHORIZATION,
                "args": {"uploadPath": "/upload", "uploadField": "file",
                         "fetchPath": "/uploads/{{marker}}.txt"},
            },
            "pivot": {
                "kind": "pivot", "engagementId": engagement_id,
                "target": "127.0.0.1", "authorization": AUTHORIZATION,
                "foothold": foothold,
                "egress": ["127.0.0.1:%d" % fixture.port,
                           "127.0.0.1:%d" % closed_port],
            },
            "toolkit": {
                "kind": "toolkit", "engagementId": engagement_id,
                "target": "127.0.0.1", "adapter": "curl",
                "authorization": AUTHORIZATION,
                "toolArgs": ["-sS", "-i", fixture.base + "/"],
            },
        }

        print("=" * 78)
        results = {}
        for kind, body in bodies.items():
            before = len(store.jobs())
            status, payload = client.post("/api/v1/jobs", body)
            if status != 201:
                print("%-8s 进不了队列：%s %s" % (kind, status, payload))
                results[kind] = None
                continue
            job_id = payload["jobId"]
            job = _wait_job(store, job_id)
            results[kind] = job
            print("%-8s %s  %s  status=%s"
                  % (kind, job_id, payload.get("kindLabel"), job.get("status")))
            line = _summarize(kind, job)
            if line:
                print("         汇总 %s" % line)
            if job.get("status") != "done":
                result = _as_dict(job.get("result"))
                print("         错误 %s" % (result.get("error")
                                            or json.dumps(result,
                                                          ensure_ascii=False)[:300]))
            assert len(store.jobs()) == before + 1
        print("=" * 78)

        nodes, meta = _graph(client, engagement_id)
        by_kind = {}
        for node in nodes:
            by_kind.setdefault(node.get("kind"), []).append(node)
        counts = meta.get("counts") or {}
        print("\n链路节点合计 %d 个：%s"
              % (len(nodes), "、".join("%s×%d" % (k, len(v))
                                      for k, v in sorted(by_kind.items()))))
        print("库里的计数     %s%s"
              % (json.dumps(counts, ensure_ascii=False),
                 "（视图已截断）" if meta.get("truncated") else ""))

        for node in nodes:
            # `models.public_node` 把 payload 摊平到节点上，所以展示字段散在
            # `title` / `name` / `detail` / `target` 几处，按类型不同。
            title = (node.get("title") or node.get("name") or node.get("label")
                     or node.get("detail") or "")
            detail = (node.get("detail") or node.get("target")
                      or node.get("severity") or "")[:56]
            print("  %-8s %-34s %s"
                  % (node.get("kind"), str(title)[:34].replace("\n", " "),
                     str(detail).replace("\n", " ")))

        print("\n夹具实际被要求做的事：")
        for line in fixture.requests[-24:]:
            print("  %s" % line)
        print("\n带外回连收到 %d 次：%s"
              % (len(fixture.oast_fetches),
                 "、".join(url[:70] for url in fixture.oast_fetches[:4])))
        print("上传落盘 %d 个文件：%s"
              % (len(fixture.uploads), "、".join(sorted(fixture.uploads))))

        failed = [k for k, v in results.items()
                  if not v or v.get("status") != "done"]
        print("\n结论：五类中 %d 类跑通%s"
              % (5 - len(failed),
                 "" if not failed else "，未跑通：%s" % "、".join(sorted(failed))))
        return 0 if not failed else 1
    finally:
        stop_flag.set()
        time.sleep(0.5)
        if proxy is not None:
            proxy.stop()
        fixture.stop()
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if store is not None:
            store.close()
        directory.cleanup()


def _closed_port() -> int:
    """拿一个确定没人听的端口（本机连它也是超时，见 probe 那轮的实测）。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


if __name__ == "__main__":
    sys.exit(main())
