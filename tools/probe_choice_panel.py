# -*- coding: utf-8 -*-
"""选项面板探针（t89）：竖排、多选、「其他」手写、「发送所选」发出去的是哪句话。

## 它回答的事情

t89 之前这一块是**横排一排按钮、点一个就走**。改成竖排多选之后，有四件事只有真浏览器
能证：面板真的竖着排、勾几件真能勾上、手写的那句真被带上、以及发出去的消息**长什么样**
（`sendMessage()` 收的是一整句，拼错了模型看到的就是另一件事）。

所以探针不只看截图，它做一次真操作：

1. 夹具里挂一条带 ```` ```options ```` 围栏的助手消息（回放的是真接口的形状）；
2. 打开页面 → 等面板出现，量几何：每行 `left` 相同、`top` 依次增大、面板高 > 100px
   （旧实现是一行 34px）；
3. 勾两件 + 在「其他」里手写一件 → 读数应为「已选 3 件」，而**输入框那一颗**发送键的
   `aria-label` 应变成「发送所选（3 件）」（t99：面板里已经没有自己的发送键了）；
4. 点**输入框那颗**发送键，**拦下 `POST /chat/stream` 的请求体**，断言消息形如
   `继续这几件事：\n1. …\n2. …\n3. 手写的`；
5. 再刷新一次，只勾一件 → 发出去的是**那一句原文**（不多一个前缀），这是"旧行为不变"
   的那一半。

拦请求体而不是读气泡：气泡只能证明"我发了点什么"，而这里要证明的是**那句话的措辞**。

## 用法

    python tools/probe_choice_panel.py

输出 `.scratch/choice/`：`panel-before.png`、`panel-picked.png`、`choice.json`。
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

OUT_DIR = os.path.join(ROOT, ".scratch", "choice")

OPTIONS = [
    "收口：现有成果出报告，四个网段标注未读取",
    "继续试：用 qemu monitor 读 guest 内存里的 config",
    "换路：你提供 OpenWrt 或 iKuai 的控制台/Web",
]
OTHER_TEXT = "顺便把报告导出成 PDF"

# 页面一加载就装"请求体记录器"：夹具的 fetch 桩是内联脚本、在 body 里执行的，所以
# 必须等 DOMContentLoaded 之后再包——更早包会被它整只覆盖。
RECORDER = r"""
(function () {
  document.addEventListener('DOMContentLoaded', function () {
    var stub = window.fetch;
    window.__sent = [];
    window.fetch = function (input, init) {
      var url = String(input);
      if (url.indexOf('/chat/stream') >= 0 && init && init.body) {
        try { window.__sent.push(JSON.parse(init.body)); } catch (e) {
          window.__sent.push({ raw: String(init.body) });
        }
      }
      return stub.apply(this, arguments);
    };
  });
})();
"""

GEOMETRY = r"""
(function () {
  var panel = document.querySelector('.chat-choice');
  if (!panel) return '';
  var rows = Array.prototype.slice.call(document.querySelectorAll('.chat-choice-item'));
  var rects = rows.map(function (row) { return row.getBoundingClientRect(); });
  var main = document.querySelector('.chat-main');
  var field = document.querySelector('.chat-field');
  var log = document.querySelector('.chat-log');
  var pr = panel.getBoundingClientRect();
  var fr = field.getBoundingClientRect();
  var send = document.getElementById('chat-send');
  var other = document.querySelector('.chat-choice-other-input');
  var sr = send ? send.getBoundingClientRect() : null;
  var orr = other ? other.getBoundingClientRect() : null;
  var keys = document.querySelectorAll('.chat-choice button').length;
  return JSON.stringify({
    hidden: panel.hidden,
    rows: rows.length,
    lefts: rects.map(function (r) { return Math.round(r.left); }),
    tops: rects.map(function (r) { return Math.round(r.top); }),
    panelHeight: Math.round(pr.height),
    overlapVar: getComputedStyle(main).getPropertyValue('--chat-overlap').trim(),
    cardHeight: Math.round(fr.height),
    logPadBottom: getComputedStyle(log).paddingBottom,
    count: (document.querySelector('.chat-choice-count') || {}).textContent || '',
    // t94：面板搬到卡片上方。下面这几项是那件事的可见面：位置（谁在谁上面）、
    // 让路（间距）。
    insideCard: field.contains(panel),
    panelAboveCard: fr.top > pr.bottom,
    gapToCard: Math.round(fr.top - pr.bottom),
    cardBottom: Math.round(fr.bottom),
    viewportHeight: window.innerHeight,
    choiceVar: getComputedStyle(main).getPropertyValue('--chat-choice-h').trim(),
    // t99：面板里**没有**发送键了，整批由输入框那一颗（#chat-send）发。所以这里有三个
    // 判据：面板里一颗按钮都没有（keys）、框占满整宽（右侧不再给键留槽）、以及那颗键的
    // 名字有没有替面板说出件数（mainSendAria）。
    panelKeys: keys,
    mainSendText: send ? (send.textContent || '').trim() : null,
    mainSendIcon: !!(send && send.querySelector('svg')),
    mainSendAria: send ? (send.getAttribute('aria-label') || '') : '',
    mainSendDiameter: sr ? Math.round(sr.width) : null,
    inputHeight: orr ? Math.round(orr.height) : null,
    inputRightGap: (orr && pr) ? Math.round(pr.right - orr.right) : null,
    mainSendDisabled: !!(send || {}).disabled
  });
})()
"""


def wait_js(cdp, expression, seconds=10.0, step=0.1):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cdp.eval(expression):
            return True
        time.sleep(step)
    return False


def wait_value(cdp, expression, seconds=10.0, step=0.1):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = cdp.eval(expression)
        if value:
            return value
        time.sleep(step)
    return None


def fixture(data):
    path = os.path.join(OUT_DIR, "choice.html")
    render_ui_fixture.build(path, data, tab="chat")
    return path


def with_options(data):
    """把一条带 ```options 围栏的助手消息挂到最后（形状照真接口）。"""
    payload = data.get("/api/v1/chat/sessions/C-xxx")
    if not payload or not payload.get("messages"):
        raise SystemExit("FAIL：夹具里没有会话消息，无法挂选项")
    messages = payload["messages"]
    messages.append({
        "id": 9001, "seq": 9001, "role": "assistant",
        "at": time.time() - 5,
        "text": ("三条路都试过了：qemu 拿不到 console，Wrt 那台不在范围内，"
                 "报告缺四个网段。\n\n```options\n"
                 + "\n".join("- " + item for item in OPTIONS)
                 + "\n```"),
    })
    payload["session"]["messageCount"] = len(messages)
    for item in data.get("/api/v1/chat/sessions", {}).get("sessions") or []:
        item["messageCount"] = len(messages)
    return data


def shoot(cdp, name):
    path = os.path.join(OUT_DIR, name)
    data = cdp.call("Page.captureScreenshot", format="png").get("data")
    with open(path, "wb") as handle:
        handle.write(base64.b64decode(data))
    return path


def scenario(cdp, kind):
    """跑一次：kind = 'multi'（勾两件 + 手写）或 'single'（只勾一件）。"""
    if not wait_js(cdp, "!!document.querySelector('.chat-choice:not([hidden])')"):
        raise SystemExit("FAIL[%s]：选项面板一直没出现" % kind)
    time.sleep(0.2)
    geometry = json.loads(cdp.eval(GEOMETRY))
    shot_a = shoot(cdp, "panel-%s-before.png" % kind)

    if kind == "multi":
        cdp.eval("(function(){var b=document.querySelectorAll("
                 "'.chat-choice-item .chat-choice-box');b[0].click();b[1].click();})()")
        cdp.eval("(function(){var t=document.querySelector('.chat-choice-other-input');"
                 "t.value=" + json.dumps(OTHER_TEXT) + ";"
                 "t.dispatchEvent(new Event('input',{bubbles:true}));})()")
    else:
        cdp.eval("(function(){var b=document.querySelectorAll("
                 "'.chat-choice-item .chat-choice-box');b[2].click();})()")
    time.sleep(0.15)
    after = json.loads(cdp.eval(GEOMETRY))
    shot_b = shoot(cdp, "panel-%s-picked.png" % kind)

    # t99：面板里那颗键没了——点输入框那一颗，它应当认出"面板上有勾选"并发出整批。
    cdp.eval("document.getElementById('chat-send').click()")
    sent = wait_value(cdp, "window.__sent && window.__sent.length && "
                           "JSON.stringify(window.__sent[window.__sent.length-1])")
    return {
        "geometry": geometry, "after": after,
        "sent": json.loads(sent) if sent else None,
        "shots": [shot_a, shot_b],
    }


def open_page(html_path, shots_dir=OUT_DIR):
    browser = _browser()
    if not browser:
        return None
    profile = tempfile.mkdtemp(prefix="choice-profile-")
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
    ws_url = _page_ws(port)
    cdp = CDP(ws_url)
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Page.addScriptToEvaluateOnNewDocument", source=RECORDER)
    return proc, cdp, port


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    if not _browser():
        print("SKIP：本机没找到 Edge / Chrome，跳过选项面板探针。")
        return 0
    with io.open(os.path.join(ROOT, ".scratch", "reason", "data.json"),
                 encoding="utf-8") as handle:
        data = json.load(handle)
    with_options(data)
    html = fixture(data)

    proc, cdp, port = open_page(html)
    if not proc:
        print("FAIL：浏览器没起来")
        return 2
    results = {}
    try:
        for kind in ("multi", "single"):
            cdp.call("Page.navigate",
                     url="file:///" + os.path.abspath(html).replace("\\", "/"))
            time.sleep(0.4)
            results[kind] = scenario(cdp, kind)
            time.sleep(0.5)
    finally:
        try:
            cdp.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

    problems = []
    multi = results["multi"]
    geom = multi["geometry"]
    if geom["hidden"]:
        problems.append("面板初始是 hidden")
    if geom["rows"] != len(OPTIONS):
        problems.append("选项行数 %s != %d" % (geom["rows"], len(OPTIONS)))
    if len(set(geom["lefts"])) != 1:
        problems.append("各行左边界不一致（不是竖排）：%s" % geom["lefts"])
    if geom["tops"] != sorted(geom["tops"]):
        problems.append("各行 top 不是递增（不是竖排）：%s" % geom["tops"])
    if geom["panelHeight"] <= 100:
        problems.append("面板只有 %spx 高（旧实现是 34px 一行）" % geom["panelHeight"])
    if geom["insideCard"]:
        problems.append("面板还在卡片里面：t94 之后它必须是卡片上一位兄弟")
    if not geom["panelAboveCard"]:
        problems.append("面板没有落在卡片上方（卡片 top=%s，面板 bottom=%s）"
                        % (geom.get("cardBottom"), geom["gapToCard"]))
    if geom["gapToCard"] < 2 or geom["gapToCard"] > 40:
        problems.append("面板与卡片间距 %spx 不合理（应为一条缝）" % geom["gapToCard"])
    if geom["choiceVar"]:
        problems.append("--chat-choice-h 又回来了（%r）：面板已不在卡片里，"
                        "没有人的高度需要被它驱动" % geom["choiceVar"])
    if geom["cardBottom"] > geom["viewportHeight"] + 1:
        problems.append("卡片被推出视口：底边 %s > 视口高 %s（发送键会点不到）"
                        % (geom["cardBottom"], geom["viewportHeight"]))
    if geom["panelKeys"]:
        problems.append("面板里还有 %s 颗按钮：t99 之后发送键只该有输入框那一颗"
                        % geom["panelKeys"])
    if geom["mainSendText"]:
        problems.append("输入框那颗发送键退回文字按钮了：%r" % geom["mainSendText"])
    if not geom["mainSendIcon"]:
        problems.append("输入框那颗发送键没有图标")
    if geom["mainSendDiameter"] != 34:
        problems.append("输入框那颗发送键直径 %s px，不是 34px" % geom["mainSendDiameter"])
    if geom.get("inputHeight") != 34:
        problems.append("「其他」框高 %s px，不是一行（应 = --chat-action-row 34px）"
                        % geom.get("inputHeight"))
    if geom.get("inputRightGap") is None or geom["inputRightGap"] > 20:
        problems.append("「其他」框没占满这一格：右边还空着 %s px（键让出的槽应当收回）"
                        % geom.get("inputRightGap"))
    # 开局一件都没勾 → 那颗键就该老老实实叫「发送」（它这一刻发的确实是框里的字）。
    if geom["mainSendAria"] != "发送":
        problems.append("一件都没勾时那颗键的名字是 %r，应为「发送」"
                        % geom["mainSendAria"])
    after = multi["after"]
    if after["count"] != "已选 3 件":
        problems.append("勾了 2 件 + 手写 1 件之后读数是 %r" % after["count"])
    if "发送所选（3 件）" not in after["mainSendAria"]:
        problems.append("勾了 3 件之后那颗键的名字没有跟着件数走：%r"
                        % after["mainSendAria"])
    message = (multi["sent"] or {}).get("message") or ""
    if not message.startswith("继续这几件事："):
        problems.append("多选发出去的不是编号列表：%r" % message[:60])
    for needle in OPTIONS[:2] + [OTHER_TEXT]:
        if needle not in message:
            problems.append("发出去的消息里缺 %r" % needle)
    single = results["single"]
    single_message = (single["sent"] or {}).get("message") or ""
    if single_message != OPTIONS[2]:
        problems.append("只勾一件时应当逐字发那一句，实得 %r" % single_message[:60])
    if "发送所选（1 件）" not in (single["after"] or {}).get("mainSendAria", ""):
        problems.append("只勾一件时那颗键的名字应当是「发送所选（1 件）」，实得 %r"
                        % (single["after"] or {}).get("mainSendAria"))

    report = {"problems": problems, "multi": multi, "single": single,
              "options": OPTIONS, "other": OTHER_TEXT}
    with io.open(os.path.join(OUT_DIR, "choice.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=1)

    print("面板：%s 行，高 %spx，在卡片**上方**（间距 %spx），卡片 %spx / 视口 %spx"
          % (geom["rows"], geom["panelHeight"], geom["gapToCard"],
             geom["cardHeight"], geom["viewportHeight"]))
    print("各行 left=%s top=%s" % (geom["lefts"], geom["tops"]))
    print("多选 → %r" % (multi["sent"] or {}).get("message"))
    print("单选 → %r" % single_message)
    if problems:
        print("结论：**不符**")
        for item in problems:
            print("  - " + item)
        return 1
    print("结论：PASS（面板在卡片上方 / 竖排 / 多选 / 其他 / 输入框那颗键发整批 / 措辞 / "
          "单件原文）")
    for item in results["multi"]["shots"] + results["single"]["shots"]:
        print("截图：%s" % item)
    return 0


if __name__ == "__main__":
    sys.exit(main())
