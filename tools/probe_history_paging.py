# -*- coding: utf-8 -*-
"""长对话「加载更早」探针（t92）：真的点一下，把上一页拿回来。

## 它回答的事情

接口改成"只送最近 400 条"之后，记录顶部会写一句「更早的 N 条没有载入」。这一行
如果没有入口，等于告诉使用者"东西丢了"；有了入口，三件事只有真浏览器能证：

1. 那一行真的画出来了，按钮上的数字与 `earlierCount` 一致；
2. 点下去真的**往前拿一页**（请求带 `?before=<第一条的 seq>`），而不是又要一遍尾巴；
3. 拿回来的那些真的**插在顶上**、滚动位置不跳、旧节点不重建（整段重画会把
   思考块的展开态和选中的文字一起丢掉）；
4. **Alt+↑**（真键盘事件）与**滚到顶上**（真滚轮 + 真 scroll 事件）也各能取
   一页，且拿的是同一页（游标同样是 `before=<第一条的 seq>`）——三个入口
   走的必须是同一份取页逻辑，否则滚动补偿会有三个版本。

## 用法

    python tools/probe_history_paging.py

输出 `.scratch/paging/`：`paging-before.png`、`paging-after.png`、
`paging-keyboard.png`、`paging-auto.png`、`paging.json`。
无浏览器时打印 SKIP 并以 0 退出（与其它探针一致）。
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

OUT_DIR = os.path.join(ROOT, ".scratch", "paging")
SESSION = "C-xxx"
TAIL_FROM = 21          # 尾巴那一页从第 21 条起
TOTAL = 420             # 会话共 420 条 → 上面还有 20 条
EARLIER = TAIL_FROM - 1

# 拦截 `?before=` 那一类请求：夹具的桩按"路径"回放（`key0` 不带查询串），
# 分页这一页必须由探针自己给，否则点按钮拿回来的还是尾巴。
PAGER = r"""
(function () {
  document.addEventListener('DOMContentLoaded', function () {
    var stub = window.fetch;
    window.__pages = [];
    window.fetch = function (input, init) {
      var url = String(input && input.url ? input.url : input);
      if (url.indexOf('before=') >= 0) {
        window.__pages.push(url);
        var body = window.__earlier;
        return Promise.resolve(new Response(JSON.stringify(body), {
          status: 200, headers: { 'Content-Type': 'application/json' }
        }));
      }
      return stub.apply(this, arguments);
    };
  });
})();
"""

STATE = r"""
(function () {
  var log = document.querySelector('.chat-log');
  var row = document.querySelector('.chat-earlier');
  var btn = row && row.querySelector('.chat-earlier-btn');
  var first = document.querySelector('.chat-msg');
  return JSON.stringify({
    messages: document.querySelectorAll('.chat-msg').length,
    hint: row ? (row.querySelector('.chat-earlier-text') || {}).textContent || '' : '',
    button: btn ? btn.textContent : '',
    disabled: btn ? !!btn.disabled : null,
    scrollTop: log ? Math.round(log.scrollTop) : -1,
    scrollHeight: log ? Math.round(log.scrollHeight) : -1,
    firstText: first ? (first.querySelector('.chat-msg-text') || {}).textContent || '' : '',
    pages: (window.__pages || []).length,
    bodies: (window.__pages || []).slice()
  });
})()
"""


def wait_value(cdp, expression, seconds=10.0, step=0.1):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = cdp.eval(expression)
        if value:
            return value
        time.sleep(step)
    return None


def shoot(cdp, name):
    path = os.path.join(OUT_DIR, name)
    data = cdp.call("Page.captureScreenshot", format="png").get("data")
    with open(path, "wb") as handle:
        handle.write(base64.b64decode(data))
    return path


def press_alt_up(cdp):
    """真键盘 Alt+↑：走页面那个 keydown 监听，不直调 `loadEarlier()`。

    `modifiers=1` 是 Alt（CDP 的位掩码：Alt=1 / Ctrl=2 / Meta=4 / Shift=8）。
    直调会把"快捷键接没接上"这件事测掉——而那正是要看的东西。
    """
    for kind in ("keyDown", "keyUp"):
        cdp.call("Input.dispatchKeyEvent", type=kind, modifiers=1,
                 key="ArrowUp", code="ArrowUp",
                 windowsVirtualKeyCode=38, nativeVirtualKeyCode=38)


def synthetic(count, start, role="user"):
    out = []
    for index in range(count):
        seq = start + index
        out.append({"id": seq, "seq": seq, "role": role,
                    "at": time.time() - (count - index) * 60,
                    "text": "第 %d 条：夹具里的历史" % seq})
    return out


def build_fixture(data):
    payload = data.get("/api/v1/chat/sessions/" + SESSION)
    if not isinstance(payload, dict):
        raise SystemExit("FAIL：夹具数据里没有会话 %s" % SESSION)
    tail = synthetic(TOTAL - TAIL_FROM + 1, TAIL_FROM)
    earlier = synthetic(EARLIER, 1)
    payload["messages"] = tail
    payload["earlierCount"] = len(earlier)
    payload["pageSize"] = 400
    payload["session"]["messageCount"] = TOTAL
    earlier_payload = {"session": payload["session"], "messages": earlier,
                       "config": payload.get("config") or {},
                       "earlierCount": 0, "pageSize": 400}
    data.pop("__stream__", None)
    path = os.path.join(OUT_DIR, "paging.html")
    render_ui_fixture.build(path, data, tab="chat")
    return path, earlier_payload


def open_page(html_path, earlier_payload):
    browser = _browser()
    if not browser:
        return None
    profile = tempfile.mkdtemp(prefix="paging-profile-")
    port = 9900 + (os.getpid() % 90)
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
           "--no-default-browser-check", "--hide-scrollbars",
           "--user-data-dir=" + profile, "--window-size=1440,1000",
           "--remote-debugging-port=%d" % port, "--remote-allow-origins=*",
           "about:blank"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not _wait_endpoint(port):
        proc.terminate()
        return None
    cdp = CDP(_page_ws(port))
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Page.addScriptToEvaluateOnNewDocument",
             source=PAGER + "\nwindow.__earlier = "
             + json.dumps(earlier_payload, ensure_ascii=False) + ";")
    return proc, cdp, port


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    if not _browser():
        print("SKIP：本机没找到 Edge / Chrome，跳过「加载更早」探针。")
        return 0
    with io.open(os.path.join(ROOT, ".scratch", "reason", "data.json"),
                 encoding="utf-8") as handle:
        data = json.load(handle)
    html, earlier_payload = build_fixture(data)

    opened = open_page(html, earlier_payload)
    if not opened:
        print("FAIL：浏览器没起来")
        return 2
    proc, cdp, port = opened
    page_url = "file:///" + os.path.abspath(html).replace(os.sep, "/")
    problems = []
    try:
        cdp.call("Page.navigate",
                 url="file:///" + os.path.abspath(html).replace("\\", "/"))
        # 等记录画出来且顶部那行出现
        if not wait_value(cdp, "document.querySelectorAll('.chat-msg').length > 0"):
            raise SystemExit("FAIL：记录一直没画出来")
        if not wait_value(cdp, "!!document.querySelector('.chat-earlier-btn')"):
            raise SystemExit("FAIL：顶部没有「加载更早」入口")
        time.sleep(0.3)
        before = json.loads(cdp.eval(STATE))
        shot_a = shoot(cdp, "paging-before.png")

        # 先把记录滚到中间，用来验证"翻上去不把人弹走"。
        cdp.eval("(function(){var l=document.querySelector('.chat-log');"
                 "l.scrollTop=Math.round(l.scrollHeight*0.4);})()")
        time.sleep(0.2)
        parked = json.loads(cdp.eval(STATE))
        cdp.eval("document.querySelector('.chat-earlier-btn').click()")
        if not wait_value(cdp,
                          "document.querySelectorAll('.chat-msg').length === %d" % TOTAL):
            got = json.loads(cdp.eval(STATE))
            raise SystemExit("FAIL：点完之后记录是 %s 条，期望 %d 条"
                             % (got["messages"], TOTAL))
        time.sleep(0.2)
        after = json.loads(cdp.eval(STATE))
        shot_b = shoot(cdp, "paging-after.png")

        # --- 场景 B：真键盘 Alt+↑（不点按钮） ---
        # 带查询串重新导航：同一个 URL 的导航在部分版本里被当成同文档，脚本
        # 不会重跑；下面"记录应回到 %d 条"就是那次导航真的发生了的证明。
        cdp.call("Page.navigate", url=page_url + "?phase=keyboard")
        if not wait_value(cdp, "document.querySelectorAll('.chat-msg').length === %d"
                          % (TOTAL - EARLIER)):
            raise SystemExit("FAIL：重载之后记录不是 %d 条" % (TOTAL - EARLIER))
        time.sleep(0.3)
        if not wait_value(cdp, "!!document.querySelector('.chat-earlier-btn')"):
            raise SystemExit("FAIL：重载之后顶部没有「加载更早」入口")
        press_alt_up(cdp)
        if not wait_value(cdp,
                          "document.querySelectorAll('.chat-msg').length === %d"
                          % TOTAL):
            raise SystemExit("FAIL：Alt+↑ 没有把更早的一页取回来")
        time.sleep(0.2)
        keyboard = json.loads(cdp.eval(STATE))
        shot_c = shoot(cdp, "paging-keyboard.png")

        # --- 场景 C：真滚轮滚一下，再落到顶上（不点按钮、不按键盘） ---
        cdp.call("Page.navigate", url=page_url + "?phase=auto")
        if not wait_value(cdp, "document.querySelectorAll('.chat-msg').length === %d"
                          % (TOTAL - EARLIER)):
            raise SystemExit("FAIL：第二次重载之后记录不是 %d 条" % (TOTAL - EARLIER))
        time.sleep(0.3)
        # 真滚轮：自动取唯一的门是"使用者自己滚过"，这一下就是在推开那扇门。
        cdp.call("Input.dispatchMouseEvent", type="mouseWheel", x=700, y=460,
                 deltaX=0, deltaY=-160)
        time.sleep(0.2)
        # 落到顶上之后，浏览器自己会发 scroll —— 探针不直调 `earlierAutoLoad`。
        cdp.eval("(function(){var l=document.querySelector('.chat-log');"
                 "l.scrollTop = 0;})()")
        if not wait_value(cdp,
                          "document.querySelectorAll('.chat-msg').length === %d"
                          % TOTAL):
            raise SystemExit("FAIL：滚到顶上没有自动取一页")
        time.sleep(0.2)
        auto = json.loads(cdp.eval(STATE))
        shot_d = shoot(cdp, "paging-auto.png")
    finally:
        try:
            cdp.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

    if before["messages"] != TOTAL - EARLIER:
        problems.append("初始记录 %s 条，期望 %d" % (before["messages"], TOTAL - EARLIER))
    if ("更早的 %d 条" % EARLIER) not in before["hint"]:
        problems.append("顶部提示里没有「更早的 %d 条」：%r" % (EARLIER, before["hint"]))
    if "加载更早的 %d 条" % EARLIER not in before["button"]:
        problems.append("按钮文案不对：%r" % before["button"])
    if after["messages"] != TOTAL:
        problems.append("点完之后记录 %s 条，期望 %d" % (after["messages"], TOTAL))
    if after["hint"] or after["button"]:
        problems.append("已经到第一条了，顶部那行还在：%r / %r"
                        % (after["hint"], after["button"]))
    if not after["pages"]:
        problems.append("没有拦到 ?before= 请求——往回翻没有带游标")
    else:
        want = "before=%d" % TAIL_FROM
        if want not in after["bodies"][-1]:
            problems.append("游标不对：%r，期望包含 %r" % (after["bodies"][-1], want))
    if "第 1 条" not in after["firstText"]:
        problems.append("插到顶上的不是第 1 条，而是 %r" % after["firstText"][:40])
    if parked["scrollTop"] > 0 and after["scrollTop"] <= parked["scrollTop"]:
        problems.append("滚动位置被弹回去了：%s → %s"
                        % (parked["scrollTop"], after["scrollTop"]))
    if after["scrollHeight"] <= parked["scrollHeight"]:
        problems.append("内容没有变长：%s → %s"
                        % (parked["scrollHeight"], after["scrollHeight"]))
    # 两个无按钮入口：拿到的东西必须与点击那条路完全一致。
    for name, shot in (("Alt+↑", keyboard), ("滚到顶上", auto)):
        if shot["messages"] != TOTAL:
            problems.append("%s 之后记录 %s 条，期望 %d"
                            % (name, shot["messages"], TOTAL))
        if shot["hint"] or shot["button"]:
            problems.append("%s 之后顶部那行还在：%r / %r"
                            % (name, shot["hint"], shot["button"]))
        if not shot["pages"]:
            problems.append("%s 没有触发 ?before= 请求" % name)
        elif "before=%d" % TAIL_FROM not in shot["bodies"][-1]:
            problems.append("%s 的游标不对：%r" % (name, shot["bodies"][-1]))
        if "第 1 条" not in shot["firstText"]:
            problems.append("%s 插到顶上的不是第 1 条：%r"
                            % (name, shot["firstText"][:40]))

    report = {"problems": problems, "before": before, "parked": parked, "after": after,
              "keyboard": keyboard, "auto": auto,
              "tailFrom": TAIL_FROM, "total": TOTAL}
    with io.open(os.path.join(OUT_DIR, "paging.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=1)

    print("初始：%s 条记录，顶部 %r，按钮 %r"
          % (before["messages"], before["hint"], before["button"]))
    print("点击后：%s 条，顶部提示 %r，滚 %s → %s，首页 %r"
          % (after["messages"], after["hint"], parked["scrollTop"],
             after["scrollTop"], after["firstText"][:24]))
    print("请求：%s" % (after["bodies"][-1] if after["bodies"] else None))
    print("Alt+↑：%s 条，游标 %r" % (keyboard["messages"],
                                   keyboard["bodies"][-1] if keyboard["bodies"] else None))
    print("滚到顶上：%s 条，游标 %r" % (auto["messages"],
                                      auto["bodies"][-1] if auto["bodies"] else None))
    if problems:
        print("结论：**不符**")
        for item in problems:
            print("  - " + item)
        return 1
    print("结论：PASS（入口在 / 带游标 / 插到顶上 / 位置不跳 / 到底即摘 / "
          "Alt+↑ 与滚到顶同样取到同一页）")
    for shot in (shot_a, shot_b, shot_c, shot_d):
        print("截图：%s" % shot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
