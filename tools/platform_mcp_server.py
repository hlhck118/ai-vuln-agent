# -*- coding: utf-8 -*-
"""平台工具 → DSH 宿主的 stdio MCP 服务端（由 DSH 按补丁行拉起，不由平台拉起）。

用法（补丁文件里就是这么写的）：

    python tools/platform_mcp_server.py
    环境：PENTEST_HOME / PENTEST_DB / PENTEST_TIER / PENTEST_SESSION（可选）
          / PENTEST_CONSOLE（控制面地址，审查闸门要用）/ PENTEST_WRITE（放行动作）

## 它是"薄"的，而且是故意的

工具清单、档位过滤、执行闸门**全部**在 `pentestd/` 里：

* `dsh_bridge.exposed_tools(tier, writes=…)` 决定这一档暴露哪些名字（内部走
  `policy.filter_tools`，再加上使用者**逐个放行**的写入动作）；
* `chat.execute_tool(plane, name, args, policy_id=…)` 执行 —— 权限档、作用域、
  侵入授权三道闸都在那里判，和平台自己的对话循环**同一个入口**。

## 写入动作走的是"它自己的档位"

只读档的桥要执行一个被放行的 `start_probe`，用的是 `probe` 档判权限（
`dsh_bridge.execution_policy`）—— 也就是说放行**一个动作**不等于把**整档**放宽：
其它只读工具照旧按只读档判。这一句是本文件里唯一一处"看起来在绕档位"的地方，
所以它必须写在最上面。

## 审查闸门在这里，不在宿主里

DSH 的审批缝只覆盖它自己的工具与 bash；挂在它下面的 MCP 服务（也就是本文件）拿到
的调用**不经过**那道缝。所以"要不要让人先看一眼"只能在这里问：回平台开一张审查单，
平台内部的等待者集合才会真的有一个"等着的人"（在桥进程里等的话，控制面看不到，
界面不画卡片，裁决接口还会回 409）。到期无人裁决按**放行**继续 —— 这是本通道
特有的口径，平台自己在审查单上写明。

这个脚本只做 MCP 传输层：分帧、方法分发、把结果包成 MCP 的 content 形状。它**不**
自己判权限，也不自己拼工具名 —— 多一处判断就多一处会与平台分叉的地方。

## 三条来自现场的写法（别改回去）

1. **按字节读写、显式 UTF-8**。Windows 上 `sys.stdout` 在管道里会按本地码页
   （cp936）编码，而 MCP 约定 UTF-8：症状是工具描述在宿主侧变成乱码，而不是报错
   （t68 实测踩过）。
2. **stdout 只承载帧**。任何日志都写 stderr；本脚本的 `log()` 因此走 stderr。
3. **懒造 plane**。`initialize`/`tools/list` 必须在毫秒级返回（宿主的初始化有
   预算），而控制面构造要读库、扫目录。所以第一次 `tools/call` 才造，且失败时
   返回一条**说明原因的工具结果**而不是让整个进程死掉。
"""

import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "pentest-platform"
SERVER_VERSION = "2.0.0"

STATE: Dict[str, Any] = {"plane": None, "store": None, "error": ""}


def log(message: str) -> None:
    """日志走 stderr —— stdout 是协议专用。"""
    try:
        sys.stderr.write("[platform-mcp] %s\n" % message)
        sys.stderr.flush()
    except OSError:
        pass


# ------------------------------------------------------------------ 审查闸门
#: 平台内部最多等 540 秒（`chat.DSH_REVIEW_MAX_S`），桥这边再多留 20 秒给往返与
#: 收尾：补丁里的 `toolCallTimeoutMs` 是 600 秒，所以等满 560 也不会先被宿主掐断。
REVIEW_HTTP_TIMEOUT_S = 560.0


def console_url() -> str:
    """控制面在哪儿（补丁 env `PENTEST_CONSOLE`）。空串 = 没配。"""
    return (os.environ.get("PENTEST_CONSOLE") or "").strip().rstrip("/")


def console_token() -> str:
    """控制面签发给"本机脚本"的令牌（`<数据目录>/console-token.txt`）。

    桥不是浏览器，没有 Cookie；平台的 `_require_session` 对这张令牌放行且免 CSRF
    —— 这条通道正是为它准备的。
    """
    db = os.environ.get("PENTEST_DB") or os.path.join(ROOT, "data", "pentest.db")
    path = os.path.join(os.path.dirname(db) or ".", "console-token.txt")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def bridge_session() -> str:
    """这条运行时服务的是哪条对话（补丁 env `PENTEST_SESSION`）。

    审查单要挂在一条对话上，界面的卡片也按对话过滤；空值意味着补丁是按会话生成
    的这一环没接上 —— 那种情况下不留白，读作"没有会话"。
    """
    return (os.environ.get("PENTEST_SESSION") or "").strip()


def review_call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """问平台：这条调用要不要人先看一眼？

    返回 `{"required": bool, "allowed": bool, "reason": str, "review": {...}}`。

    **放行口径是这条通道特有的**：拿不到答复就放行。没配地址、令牌缺失、连不上、
    平台回 4xx/5xx、到期没人裁决 —— 一律放行，原因写进返回值并落 stderr。默认
    通道（平台自己的对话循环）相反：过期即作废。口径由平台那一侧定，桥照着做，
    不在这里另立一套判据。
    """
    base = console_url()
    if not base:
        return {"required": False, "allowed": True,
                "reason": "没有配置控制面地址（PENTEST_CONSOLE），按放行继续"}
    token = console_token()
    if not token:
        return {"required": False, "allowed": True,
                "reason": "读不到控制面令牌（console-token.txt），按放行继续"}
    payload = {"tool": name, "arguments": arguments or {},
               "session": {"sessionId": bridge_session()}}
    request = urllib.request.Request(
        base + "/api/v1/dsh/approval",
        data=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=REVIEW_HTTP_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        # 4xx 多半是我们问得不对（缺会话、参数形状），5xx 是平台自己坏了；两者都
        # 不足以断言"这条危险"，按放行继续，但把状态码留在原因里，事后可追。
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:                              # noqa: BLE001
            detail = ""
        return {"required": False, "allowed": True,
                "reason": "审查接口回了 %s（%s），按放行继续"
                          % (exc.code, detail or "无说明")}
    except Exception as exc:                           # noqa: BLE001
        return {"required": False, "allowed": True,
                "reason": "审查接口不可达（%s: %s），按放行继续"
                          % (type(exc).__name__, exc)}
    if not isinstance(body, dict):
        return {"required": False, "allowed": True,
                "reason": "审查接口回了非对象结果，按放行继续"}
    return {"required": bool(body.get("required")),
            "allowed": bool(body.get("allowed")),
            "reason": str(body.get("reason") or ""),
            "review": body.get("review")}


def review_gate(name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """执行前的审查闸门：放行返回 None，被拒返回一条 MCP 工具结果。

    只审**平台的工具**：DSH 自己的 bash/文件工具在宿主进程里，走不到这里，所以
    "审查范围"这条边界是结构性的，不靠一份白名单维持。
    """
    verdict = review_call(name, arguments)
    if verdict.get("required") or verdict.get("review"):
        log("审查：%s required=%s allowed=%s %s"
            % (name, verdict.get("required"), verdict.get("allowed"),
               verdict.get("reason") or ""))
    if verdict.get("required") and not verdict.get("allowed"):
        return {"isError": True, "text": json.dumps(
            {"ok": False,
             "summary": "「%s」需要人先裁决，这次没有放行：%s"
                        % (name, verdict.get("reason") or "裁决未通过"),
             "data": {"review": verdict.get("review"), "reviewed": True}},
            ensure_ascii=False)}
    return None


# ------------------------------------------------------------------ 控制面
def _settings_store() -> Any:
    """只开库、不建 plane：`tools/list` 需要读两个设置（档位、放行清单）。

    为什么不能只靠环境变量：补丁里确实带着 `PENTEST_TIER` / `PENTEST_WRITE`，但那是
    **生成补丁那一刻**的值。使用者改了设置却没重新生成补丁时，环境变量会义无反顾地
    说旧值 —— 而"界面显示已放行、模型看不见那个工具"这种分叉，查起来只会怀疑桥坏了。
    读库是产品路径，环境变量留给"临时起一条限定档位的运行时"。

    失败一律吞掉并返回 None：桥不该因为库被锁/模式旧而整体不说话（见 `tier` 的注释）。
    """
    store = STATE.get("store")
    if store is not None:
        return store
    try:
        from pentestd import store as store_module
        db = os.environ.get("PENTEST_DB") or os.path.join(ROOT, "data", "pentest.db")
        if not os.path.isfile(db):
            return None
        STATE["store"] = store_module.Store(db)
        return STATE["store"]
    except Exception:                                  # noqa: BLE001
        return None


def tier() -> str:
    """当前暴露档位。**每次现读设置**，而不是启动时定死。

    为什么必须现读：桥是被一棵**长驻**的运行时加载的，而档位是使用者在设置页上改的。
    启动时定死的话，界面从 readonly 改成 full 之后，模型看得见的工具还是那一批——
    而这是一条安全边界（暴露面），"以为收紧了、其实没收紧"比反过来严重得多。

    优先级：平台设置 > 环境变量 > 默认。环境变量留给"临时起一条限定档位的运行时"
    这种用法（诊断、演习），设置才是产品路径。
    """
    from pentestd import dsh_bridge
    store = _settings_store()
    if store is not None:
        try:
            value = (dsh_bridge.tier_of(store) or "").strip()
            if value in dsh_bridge.tiers():
                return value
        except Exception:
            # 读设置失败（库被锁、schema 还旧）不该让工具**全部**消失：退回环境变量
            # 与默认档，而不是抛出去 —— 抛出去的症状是"整个桥突然说不了话"。
            pass
    value = (os.environ.get("PENTEST_TIER") or "").strip()
    if value in dsh_bridge.tiers():
        return value
    return dsh_bridge.DEFAULT_TIER


def writes() -> List[str]:
    """使用者显式放行的写入动作（**每次现读**，理由同 `tier()`）。

    优先级：平台设置 > 环境变量 `PENTEST_WRITE`。环境变量留给"临时起一条带写入的
    运行时"（诊断、演示）；设置才是产品路径 —— 它在设置页上是一次点击，
    在库里是一行可审计的记录。
    """
    from pentestd import dsh_bridge
    store = _settings_store()
    if store is not None:
        try:
            return dsh_bridge.write_actions_of(store)
        except Exception:
            pass
    raw = (os.environ.get("PENTEST_WRITE") or "").strip()
    out: List[str] = []
    for piece in raw.replace(";", ",").split(","):
        name = dsh_bridge.normalize_write_action(piece)
        if name and name not in out:
            out.append(name)
    return out


def make_plane() -> Any:
    """造一个**真的**控制面对象（不是替身）。

    走 `toolclient.build_plane` —— 它的 docstring 写明构造是纯本地的：不发包、
    不建线程、不绑端口。所以桥里造一个 plane 不会顺手起第二个执行节点。

    作用域与侵入授权：**默认最窄**（回环与私有网段、侵入类全关）。桥是"另一个调用方"，
    它的授权不该顺手继承"平台被授权了什么"——那两件事在授权文件里是两个条款。
    要放开就显式设 `dsh_bridge_scope` / `dsh_bridge_intrusive`。
    """
    from pentestd import store as store_module, toolclient, scope as scope_module
    db = os.environ.get("PENTEST_DB") or os.path.join(ROOT, "data", "pentest.db")
    st = store_module.Store(db)
    scope_text = (st.get_setting("dsh_bridge_scope", "") or "").strip()
    intrusive_text = (st.get_setting("dsh_bridge_intrusive", "") or "").strip()
    probe_scope = (scope_module.normalize_scope(scope_text.split(","))
                   if scope_text else scope_module.DEFAULT_SCOPE)
    intrusive = tuple(x.strip() for x in intrusive_text.split(",") if x.strip())
    plane = toolclient.build_plane(st, probe_scope, intrusive, ())
    STATE["store"], STATE["plane"] = st, plane
    log("控制面就绪：db=%s scope=%s intrusive=%s tier=%s"
        % (db, ",".join(probe_scope) or "(默认)", ",".join(intrusive) or "(全关)",
           tier()))
    return plane


def principal() -> Dict[str, Any]:
    """把操作记在谁名下，**以及记在哪条对话名下**。

    桥没有登录会话，但审计记录不能因此变成"某人" —— 取库里的第一个管理员，并在
    结果里带上 `source: dsh-bridge`，让"这条是谁发起的"在数据上可分辨。

    `sessionId` 是 `PENTEST_SESSION`，权威来源与审查单同一条（补丁按会话生成，
    见 `bridge_session`）。**漏掉它是有实际代价的**：平台执行工具时用
    `session["sessionId"]` 决定"这次动作写进哪个目标"（`chat.session_engagement`
    → `store.ensure_session_engagement`），拿不到会话就退回到
    "`create_job` 按 target 新建一个目标"。实测一条对话里 11 次工具调用建出了
    **11 个目标**，于是「漏洞」「资产」两页每次只显示最后一次调用的收获，而使用
    者看到的是"我的活被拆散到了十几个目标里"。审查单那边一直带着会话，所以现象
    是"卡片挂在对话上、证据却挂在别的目标上"——两处不一致最难查。
    """
    out: Dict[str, Any] = {"source": "dsh-bridge"}
    session_id = bridge_session()
    if session_id:
        out["sessionId"] = session_id
    st = STATE.get("store")
    if st is None:
        return out
    for attr in ("list_users", "users"):
        fn = getattr(st, attr, None)
        if callable(fn):
            try:
                for row in fn() or []:
                    if isinstance(row, dict) and row.get("username"):
                        out.update({"userId": row.get("id") or "",
                                    "username": row.get("username"),
                                    "role": row.get("role") or ""})
                        return out
            except Exception:                      # 读路径不该把桥弄死
                break
    return out


# ------------------------------------------------------------------ 工具面
def tool_definitions() -> List[Dict[str, Any]]:
    from pentestd import dsh_bridge
    out = []
    for entry in dsh_bridge.exposed_tools(tier(), writes=writes()):
        item = {"name": entry.get("name") or "",
                "description": entry.get("description") or entry.get("label") or "",
                # 平台自己的 `parameters` 已经是 JSON Schema，直接就是 MCP 要的形状。
                "inputSchema": entry.get("parameters") or {"type": "object",
                                                           "properties": {}}}
        if entry.get("write"):
            # 非标准字段，但 MCP 客户端会原样保留：它让"这条是放行来的写入"
            # 在调用方那一侧就可见，而不是只在平台的日志里。
            item["annotations"] = {"write": True}
        out.append(item)
    return out


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """执行一次工具调用。**与平台对话循环同一个入口**（`chat.execute_tool`）。"""
    from pentestd import chat, dsh_bridge
    granted = writes()
    allowed = {t["name"] for t in dsh_bridge.exposed_tools(tier(), writes=granted)}
    if name not in allowed:
        # 暴露面之外的名字直接拒 —— 与平台里"清单是提示、闸门在执行处"同一条原则：
        # 这里多判一次不越权（它只会更窄），而模型凭空写一个名字时它会先撞到这里。
        return {"isError": True, "text": json.dumps(
            {"ok": False, "summary": "工具 %s 不在当前暴露档位（%s）内" % (name, tier()),
             "data": {"exposed": sorted(allowed)[:60]}}, ensure_ascii=False)}
    # 审查闸门：**在暴露面检查之后、执行之前**。放在暴露面之前的话，一个不存在的
    # 名字也会开出一张给人看的审查单（"要不要执行 foo？"），而 foo 根本不知道是什么。
    blocked = review_gate(name, arguments or {})
    if blocked is not None:
        return blocked
    used = dsh_bridge.execution_policy(tier(), name)
    if used != tier():
        # 只在这一条被抬档时留痕：审计要能回答"这个只读桥为什么有权下发探测"，
        # 而答案必须是"使用者放行了这一个动作"，不是"档位被谁改了"。
        log("写入动作 %s 按 %s 档执行（桥档位 %s，放行清单 %s）"
            % (name, used, tier(), ",".join(granted) or "(空)"))
    plane = STATE.get("plane") or make_plane()
    outcome = chat.execute_tool(plane, name, arguments or {},
                                policy_id=used, role="", auth_required=True,
                                session=principal())
    body = {"ok": bool(outcome.get("ok")), "summary": outcome.get("summary") or "",
            "data": outcome.get("data")}
    return {"isError": not bool(outcome.get("ok")),
            "text": json.dumps(body, ensure_ascii=False, default=str)}


# ------------------------------------------------------------------ MCP 传输
def send(message: Dict[str, Any]) -> None:
    data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def reply(ident: Any, result: Any) -> None:
    send({"jsonrpc": "2.0", "id": ident, "result": result})


def fail(ident: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}})


def handle(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if method == "initialize":
        requested = params.get("protocolVersion")
        return {"protocolVersion": requested or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tool_definitions()}
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}
        if not name:
            return {"content": [{"type": "text", "text": "缺少工具名"}], "isError": True}
        result = call_tool(name, args)
        return {"content": [{"type": "text", "text": result["text"]}],
                "isError": result["isError"]}
    raise KeyError(method)


def main() -> int:
    log("启动：tier=%s home=%s pid=%d" % (tier(), ROOT, os.getpid()))
    stream = sys.stdin.buffer
    while True:
        raw = stream.readline()
        if not raw:
            break                                  # stdin 关闭 = 宿主收摊
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except ValueError:
            continue                               # 协议：格式错误的行忽略
        if not isinstance(frame, dict):
            continue
        ident, method = frame.get("id"), frame.get("method")
        if not isinstance(method, str):
            continue
        params = frame.get("params")
        if not isinstance(params, dict):
            params = {}
        if ident is None:
            continue                               # 通知（初始化的 initialized 等）
        try:
            reply(ident, handle(method, params))
        except KeyError:
            fail(ident, -32601, "method not found: %s" % method)
        except Exception as exc:                    # noqa: BLE001 —— 桥不该被一次调用弄死
            log("处理 %s 失败：%s\n%s" % (method, exc, traceback.format_exc()))
            if method == "tools/call":
                reply(ident, {"content": [{"type": "text",
                                           "text": "桥内部错误：%s" % exc}],
                              "isError": True})
            else:
                fail(ident, -32603, str(exc))
    log("stdin 关闭，退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
