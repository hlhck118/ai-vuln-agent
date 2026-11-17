"""真浏览器探针：目标 ⇄ 对话 的对应（m00482「目标和对话不对应」）。

夹具里造两条目标、两条分属它们的对话，然后用人会做的手势走一遍：

1. 点「检查工具」→ 范围跟到它名下的 127.0.0.1；
2. 点目标「39997.cn」→ 打开着的对话换成它名下的「对39997.cn进行渗透测试」；
3. 点一个名下没有对话的目标 → 起一条新对话（POST /chat/sessions 由桩接住）；
4. 再点回 127.0.0.1 → 回到「检查工具」；
5. 已属于当前目标的对话，再点一次目标不许被换成"最近一条"。

每一步都读 DOM 与 `PentestChat.currentSessionId()`，不读内部变量：探针要证明的是
"人看得见的三处（目标高亮 / 当前对话 / 消息记录）指的是同一个东西"。

跑法：python tools/probe_goal_session.py [--png out.png]
没装 Edge/Chrome 时打印 SKIP 并以 0 退出。
"""
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import render_ui_fixture                                        # noqa: E402
from probe_reasoning_stream import (CDP, _browser, _page_ws,    # noqa: E402
                                    _wait_endpoint)

OUT_DIR = os.path.join(ROOT, ".scratch", "t-switch")
CACHE = os.path.join(ROOT, ".scratch", "reason", "data.json")

A_ID, B_ID, C_ID = "E-a39997cn", "E-b127", "E-empty"
A_GOAL, B_GOAL, C_GOAL = "39997.cn", "127.0.0.1", "空目标"
S_A, S_B = "C-39997cn", "C-jiancha"
T_A, T_B = "对39997.cn进行渗透测试", "检查工具"

# 夹具的 fetch 桩是 body 里的内联脚本，会覆盖更早装上的封装；所以和别的探针一样等
# DOMContentLoaded 再包一层。`POST /chat/sessions`（给这个目标起一条新对话）在桩里
# 没有，会回一个会话列表而不是一条会话——不接住这一步就只能靠"它应该会"。
POST_SHIM = r"""
(function () {
  document.addEventListener('DOMContentLoaded', function () {
    var stub = window.fetch;
    window.fetch = function (input, init) {
      var url = String((input && input.url) || input || '');
      var method = String((init && init.method) || (input && input.method) || 'GET')
        .toUpperCase();
      if (method === 'POST' && /\/chat\/sessions(\?|$)/.test(url)) {
        return Promise.resolve(new Response(JSON.stringify({
          id: 'C-new', title: '', engagementId: 'E-empty',
          createdAt: Date.now() / 1000, updatedAt: Date.now() / 1000,
          messageCount: 0,
        }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
      }
      return stub.apply(this, arguments);
    };
  });
})();
"""

STATE = r"""
JSON.stringify({
  sid: (window.PentestChat && PentestChat.currentSessionId()) || '',
  goalOn: ((document.querySelector('#chat-goals .chat-goal.on .chat-goal-pick')
            || {}).textContent || '').trim().slice(0, 40),
  sessionOn: ((document.querySelector('.chat-session.on .chat-session-title')
               || {}).textContent || '').trim(),
  groupOn: ((document.querySelector('.chat-sessions-goal.on .chat-sessions-goal-name')
             || {}).textContent || '').trim(),
  groupCount: ((document.querySelector('.chat-sessions-goal.on .chat-sessions-goal-count')
                || {}).textContent || '').trim(),
  tag: ((document.querySelector('.chat-sessions-goal.on .chat-sessions-goal-tag')
         || {}).textContent || '').trim(),
  note: ((document.getElementById('chat-sessions-hint') || {}).textContent || '')
        .trim(),
  body: ((document.getElementById('chat-sessions') || {}).textContent || '')
        .replace(/\s+/g, ' ').slice(0, 240),
  listHint: (function () {
    var box = document.querySelector('#chat-sessions .chat-sessions-goal p.muted');
    return box ? (box.textContent || '').replace(/\s+/g, ' ').slice(0, 120) : '';
  })(),
  hint: (function () {
    var box = document.querySelector('#chat-sessions .chat-sessions-goal');
    return box ? (box.textContent || '').replace(/\s+/g, ' ').slice(0, 160) : '';
  })(),
  log: ((document.querySelector('.chat-log') || {}).textContent || '')
        .replace(/\s+/g, ' ').slice(-260),
  errors: (window.__FIXTURE_ERRORS__ || []).slice(-2),
})
"""

CLICK_GOAL = r"""
(function (name) {
  var rows = document.querySelectorAll('#chat-goals .chat-goal');
  for (var i = 0; i < rows.length; i += 1) {
    if ((rows[i].textContent || '').indexOf(name) >= 0) {
      var pick = rows[i].querySelector('.chat-goal-pick');
      if (!pick) { return 'NO-PICK'; }
      pick.click();
      return String(rows[i].textContent || '').slice(0, 40);
    }
  }
  return 'NO-ROW';
})(%s)
"""

CLICK_SESSION = r"""
(function (title) {
  var rows = document.querySelectorAll('#chat-sessions .chat-session');
  for (var i = 0; i < rows.length; i += 1) {
    var t = rows[i].querySelector('.chat-session-title');
    if (t && (t.textContent || '').indexOf(title) >= 0) {
      var open = rows[i].querySelector('.chat-session-open');
      if (!open) { return 'NO-OPEN'; }
      open.click();
      return String(t.textContent || '').trim();
    }
  }
  return 'NO-ROW';
})(%s)
"""


def _clone(obj):
    return json.loads(json.dumps(obj))


def synth():
    """在真实夹具数据上叠两条目标、两条对话（名字照用户截图的现场起）。"""
    if os.path.exists(CACHE):
        with io.open(CACHE, encoding="utf-8") as handle:
            data = json.load(handle)
    else:
        data = render_ui_fixture.collect()
    base_eng = _clone(data["/api/v1/engagements"]["engagements"][0])
    base_ses = _clone(data["/api/v1/chat/sessions"]["sessions"][0])
    base_det = _clone(data["/api/v1/chat/sessions/C-xxx"])

    def engagement(eid, target):
        item = _clone(base_eng)
        item.update({"id": eid, "target": target})
        for key in ("findings", "assets", "tasks", "conversations"):
            if isinstance(item.get(key), list):
                item[key] = []
            elif isinstance(item.get(key), int):
                item[key] = 0
        return item

    def session(sid, title, eid, age):
        item = _clone(base_ses)
        item.update({"id": sid, "title": title, "engagementId": eid,
                     "updatedAt": 1792587790.0 + age, "messageCount": 2})
        return item

    def detail(sid, title, eid, marker):
        item = _clone(base_det)
        item["session"].update({"id": sid, "title": title, "engagementId": eid})
        msgs = item.get("messages") or []
        if msgs:
            msgs[0]["text"] = marker
            item["messages"] = msgs[:1]
        return item

    data["/api/v1/engagements"]["engagements"] = [
        engagement(A_ID, A_GOAL), engagement(B_ID, B_GOAL),
        engagement(C_ID, C_GOAL)]
    data["/api/v1/chat/sessions"]["sessions"] = [
        session(S_A, T_A, A_ID, 0), session(S_B, T_B, B_ID, -900),
    ]
    data["/api/v1/chat/sessions/" + S_A] = detail(
        S_A, T_A, A_ID, "这条属于 " + A_GOAL + "：先看首页再决定入口。")
    data["/api/v1/chat/sessions/" + S_B] = detail(
        S_B, T_B, B_ID, "这条属于 " + B_GOAL + "：列出工具清单。")
    new_one = detail("C-new", "", C_ID, "")
    new_one["messages"] = []
    data["/api/v1/chat/sessions/C-new"] = new_one
    return data


def state(cdp):
    return json.loads(cdp.eval(STATE))


def click_goal(cdp, name):
    return cdp.eval(CLICK_GOAL % json.dumps(name, ensure_ascii=False))


def click_session(cdp, title):
    return cdp.eval(CLICK_SESSION % json.dumps(title, ensure_ascii=False))


def wait_state(cdp, predicate, seconds=6.0, step=0.1):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        last = state(cdp)
        try:
            if predicate(last):
                return last
        except Exception:
            pass
        time.sleep(step)
    return last


def shoot(cdp, path):
    raw = cdp.call("Page.captureScreenshot", format="png").get("data")
    with open(path, "wb") as handle:
        handle.write(base64.b64decode(raw))
    return path


def open_page(html):
    browser = _browser()
    if not browser:
        return None
    profile = tempfile.mkdtemp(prefix="switch-profile-")
    port = 9800 + (os.getpid() % 90)
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
           "--no-default-browser-check", "--hide-scrollbars",
           "--user-data-dir=" + profile, "--window-size=1440,1000",
           "--remote-debugging-port=%d" % port, "--remote-allow-origins=*",
           "about:blank"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    if not _wait_endpoint(port):
        proc.terminate()
        return None
    cdp = CDP(_page_ws(port))
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Page.addScriptToEvaluateOnNewDocument", source=POST_SHIM)
    return proc, cdp, port


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    png = os.path.join(OUT_DIR, "switch.png")
    if "--png" in sys.argv:
        png = sys.argv[sys.argv.index("--png") + 1]
    if not _browser():
        print("SKIP：本机没找到 Edge / Chrome，跳过目标与对话探针。")
        return 0

    path = os.path.join(OUT_DIR, "probe.html")
    render_ui_fixture.build(path, synth(), tab="chat")
    opened = open_page(path)
    if not opened:
        print("FAIL：浏览器没起来")
        return 2
    proc, cdp, port = opened
    checks = []
    shot = ""

    def check(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print("%-44s %s  %s" % (name, "OK  " if ok else "FAIL", detail))

    try:
        cdp.call("Page.navigate",
                 url="file:///" + os.path.abspath(path).replace("\\", "/"))
        ready = wait_state(
            cdp,
            lambda s: s["goalOn"] and s["sessionOn"] and s["sid"] != "",
            seconds=20.0)
        check("页面起来了，且已打开一条对话",
              bool(ready and ready["sid"]),
              (ready or {}).get("sessionOn") if ready else "")

        # ① 点「检查工具」→ 范围跟到 127.0.0.1
        click_session(cdp, T_B)
        got = wait_state(cdp, lambda s: s["sid"] == S_B and B_GOAL in s["goalOn"])
        check("点一条对话 → 目标高亮跟过去",
              got["sid"] == S_B and B_GOAL in got["goalOn"],
              "scope=%s session=%s" % (got["goalOn"], got["sessionOn"]))

        # ② 点目标「39997.cn」→ 对话换成它名下的那条（m00482 的核心）
        click_goal(cdp, A_GOAL)
        got = wait_state(cdp, lambda s: s["sid"] == S_A and A_GOAL in s["goalOn"])
        check("点目标 → 打开着的对话换成它名下的那条",
              got["sid"] == S_A and got["sessionOn"] == T_A,
              "scope=%s session=%s sid=%s" % (got["goalOn"], got["sessionOn"],
                                              got["sid"]))
        check("对话记录里就是这条对话的内容",
              A_GOAL in got["log"], got["log"][-90:])
        check("组头标的是当前目标，且计数只算这一个目标",
              got["groupOn"] == A_GOAL and got["tag"] == "当前",
              "group=%s %s count=%s" % (got["groupOn"], got["tag"],
                                        got["groupCount"]))
        if png:
            shot = shoot(cdp, png)

        # ③ 名下一个对话都没有的目标 → 清成「新对话」（会话在第一次发送时才建）
        click_goal(cdp, C_GOAL)
        got = wait_state(cdp, lambda s: s["sid"] == "" and C_GOAL in s["goalOn"])
        check("点没有对话的目标 → 清成新对话，不留别人的记录",
              got["sid"] == "" and C_GOAL in got["goalOn"] and not got["sessionOn"],
              "scope=%s sid=%r session=%r" % (got["goalOn"], got["sid"],
                                              got["sessionOn"]))
        check("这一栏自己说明它名下还没有对话",
              "名下还没有对话" in got["body"],
              got["body"][:110])

        # ④ 点回 127.0.0.1 → 回到「检查工具」
        click_goal(cdp, B_GOAL)
        got = wait_state(cdp, lambda s: s["sid"] == S_B)
        check("再点回另一个目标 → 回到它名下的对话",
              got["sid"] == S_B and got["sessionOn"] == T_B,
              "session=%s" % got["sessionOn"])

        # ⑤ 已属于当前目标的对话：再点一次目标不许被换掉
        click_goal(cdp, A_GOAL)
        wait_state(cdp, lambda s: s["sid"] == S_A)
        click_goal(cdp, A_GOAL)
        time.sleep(0.6)
        got = state(cdp)
        check("已属于这个目标的对话，再点一次目标也不换",
              got["sid"] == S_A, "sid=%s" % got["sid"])
        check("页面没有报错", not got["errors"],
              json.dumps(got["errors"], ensure_ascii=False)[:120])
    finally:
        try:
            cdp.close()
        finally:
            proc.terminate()

    bad = [item for item in checks if not item["ok"]]
    with io.open(os.path.join(OUT_DIR, "result.json"), "w",
                 encoding="utf-8") as handle:
        json.dump({"checks": checks, "shot": shot}, handle,
                  ensure_ascii=False, indent=2)
    print("\n%d/%d 通过" % (len(checks) - len(bad), len(checks)))
    if bad:
        print("FAILED: " + ", ".join(item["check"] for item in bad))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
