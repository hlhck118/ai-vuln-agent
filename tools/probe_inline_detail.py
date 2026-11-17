"""真浏览器探针：详情就地展开 + 左栏两块贴在一起（m12911 的三项）。

夹具里用人会做的手势点两下行，然后量它们落地的地方：

1. 点一行任务 → 展开面必须是那一行**后面的兄弟**，不是右栏另起一片；
2. 展开面长在 #chat-side-job-list 里（不是 body、不是栏底），整页只此一个；
3. 再点另一行 → 旧的展开面必须消失（同一时刻只展开一条）；
4. 点一行目标 → 同样的规则落在 #chat-goals 里那一行下面；
5. 「目标」块紧跟对话记录，不许被顶到栏底（.chat-side-scroll 的 flex 从
   `1 1 auto` 改成 `0 1 auto`：basis 按内容取高，只有高到装不下才收缩）；
6. 会话行是扁平行：无边框、无底色、标题单行省略、删除按钮默认不显形。

跑法：python tools/probe_inline_detail.py [--png out.png]
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

OUT_DIR = os.path.join(ROOT, ".scratch", "t-inline")
CACHE = os.path.join(ROOT, ".scratch", "reason", "data.json")

STATE = r"""
JSON.stringify({
  jobRows: document.querySelectorAll("#chat-side-job-list .chat-side-job").length,
  jobIds: Array.prototype.map.call(
    document.querySelectorAll("#chat-side-job-list .chat-side-job"),
    function (n) { return String(n.dataset.jobId || ""); }),
  inlines: document.querySelectorAll(".chat-detail-inline").length,
  inlineKind: ((document.querySelector(".chat-detail-inline") || {}).dataset
               || {}).detailKind || "",
  inlineParent: ((document.querySelector(".chat-detail-inline") || {})
                 .parentElement || {}).id || "",
  inlinePrev: (function () {
    var panel = document.querySelector(".chat-detail-inline");
    var prev = panel && panel.previousElementSibling;
    if (!prev) { return ""; }
    var names = String(prev.className || "").split(/\s+/);
    return names[0] === "chat-session" ? names.join(" ") : names[0];
  })(),
  inlinePrevId: (((document.querySelector(".chat-detail-inline") || {})
                  .previousElementSibling || {}).dataset || {}).jobId || "",
  inlineText: ((document.querySelector(".chat-detail-inline") || {})
               .textContent || "").replace(/\s+/g, " ").slice(0, 200),
  inRail: !!((document.querySelector(".chat-detail-rail .chat-detail-inline"))),
  open: (function () {
    var box = document.getElementById("chat-detail");
    return !!box && box.classList.contains("is-open");
  })(),
  openVisible: (function () {
    var box = document.getElementById("chat-detail");
    return !!box && !!box.offsetParent;
  })(),
  headKind: ((document.getElementById("chat-detail-kind") || {}).textContent || "").trim(),
  headTitle: ((document.getElementById("chat-detail-title") || {}).textContent || "").trim(),
  goalRows: document.querySelectorAll("#chat-goals .chat-goal").length,
  goalInlineId: (((document.querySelector("#chat-goals .chat-detail-inline") || {})
                  .previousElementSibling || {}).dataset || {}).engagementId || "",
  geom: (function () {
    var side = document.querySelector(".chat-side");
    var scroll = document.querySelector(".chat-side-scroll");
    var goals = document.getElementById("chat-side-goals");
    if (!side || !scroll || !goals) { return null; }
    var s = side.getBoundingClientRect();
    var c = scroll.getBoundingClientRect();
    var g = goals.getBoundingClientRect();
    return {
      sideBottom: Math.round(s.bottom),
      scrollBottom: Math.round(c.bottom),
      scrollHeight: Math.round(c.height),
      scrollContent: scroll.scrollHeight,
      goalsTop: Math.round(g.top),
      goalsBottom: Math.round(g.bottom),
      gap: Math.round(g.top - c.bottom),
      tail: Math.round(s.bottom - g.bottom),
    };
  })(),
  session: (function () {
    /* 量**没被选中**的那一行：选中行的底色是 accent-soft，用它验"扁平行
       没有底色"必然失败。夹具里永远至少两条对话。 */
    var rows = document.querySelectorAll("#chat-sessions .chat-session");
    var row = null;
    for (var i = 0; i < rows.length; i += 1) {
      if (!rows[i].classList.contains("on")) { row = rows[i]; break; }
    }
    if (!row) { row = rows[0]; }
    if (!row) { return null; }
    var cs = getComputedStyle(row);
    var title = row.querySelector(".chat-session-title");
    var open = row.querySelector(".chat-session-open");
    var del = row.querySelector(".chat-session-del");
    var ts = title ? getComputedStyle(title) : null;
    return {
      border: cs.borderTopWidth + " " + cs.borderBottomWidth,
      radius: cs.borderTopLeftRadius,
      bg: cs.backgroundColor,
      padY: cs.paddingTop,
      titleWrap: ts ? ts.whiteSpace : "",
      titleOverflow: ts ? ts.textOverflow : "",
      titleWeight: ts ? ts.fontWeight : "",
      titleText: title ? (title.textContent || "").trim() : "",
      titleTitle: open ? (open.title || "") : "",
      delOpacity: del ? getComputedStyle(del).opacity : "",
      coarse: matchMedia("(any-pointer: coarse)").matches,
    };
  })(),
  errors: (window.__FIXTURE_ERRORS__ || []).slice(-2),
})
"""

CLICK_JOB = r"""
(function (index) {
  var rows = document.querySelectorAll("#chat-side-job-list .chat-side-job");
  if (!rows[index]) { return "NO-ROW"; }
  rows[index].click();
  return String(rows[index].dataset.jobId || "");
})(%d)
"""

CLICK_GOAL = r"""
(function (index) {
  var rows = document.querySelectorAll("#chat-goals .chat-goal");
  if (!rows[index]) { return "NO-ROW"; }
  var pick = rows[index].querySelector(".chat-goal-pick");
  if (!pick) { return "NO-PICK"; }
  pick.click();
  return String(rows[index].dataset.engagementId || "");
})(%d)
"""


def load():
    """真实夹具 + 一处放大：同一个目标名下多放两条任务。

    原夹具里每个目标名下只有一条任务，而"点第二行、旧的展开面要收掉"这件事
    恰恰要两个兄弟行才能看见。多出来的两条从第一条复制，只换 id 与状态——
    形状必须和真任务一模一样，否则展开面读的是假字段。
    """
    if os.path.exists(CACHE):
        with io.open(CACHE, encoding="utf-8") as handle:
            data = json.load(handle)
    else:
        data = render_ui_fixture.collect()
    jobs = data.get("/api/v1/jobs", {}).get("jobs") or []
    engs = data.get("/api/v1/engagements", {}).get("engagements") or []
    if not jobs or not engs:
        return data
    extra = []
    for eng in engs:
        eid = eng.get("id") or ""
        for jid in jobs:
            if (jid.get("engagementId") or "") == eid:
                base = jid
                break
        else:
            base = jobs[0]
        for index, status in enumerate(("queued", "failed"), start=1):
            one = json.loads(json.dumps(base))
            one["id"] = "job-inline-%s-%d" % (eid.replace("E-", ""), index)
            one["engagementId"] = eid
            one["status"] = status
            extra.append(one)
    data["/api/v1/jobs"]["jobs"] = jobs + extra
    data["/api/v1/jobs"]["total"] = len(jobs) + len(extra)

    # 会话列表同样要"至少两条"：扁平行的量测必须落在**没被选中**的那一行上，
    # 选中行按设计有 accent-soft 底色，拿它验"没有底色"永远失败。
    sessions = data.get("/api/v1/chat/sessions", {}).get("sessions") or []
    if sessions:
        twin = json.loads(json.dumps(sessions[0]))
        twin["id"] = "C-inline-twin"
        twin["title"] = ("一条很长的对话标题，用来确认它只占一行、不换行、末尾给省略号")
        twin["updatedAt"] = float(twin.get("updatedAt") or 0) - 60
        data["/api/v1/chat/sessions"]["sessions"] = sessions + [twin]
        detail = data.get("/api/v1/chat/sessions/" + sessions[0]["id"])
        if detail:
            clone = json.loads(json.dumps(detail))
            clone.get("session", {})["id"] = "C-inline-twin"
            clone.get("session", {})["title"] = twin["title"]
            data["/api/v1/chat/sessions/C-inline-twin"] = clone
    return data


def state(cdp):
    return json.loads(cdp.eval(STATE))


def wait_state(cdp, predicate, seconds=10.0, step=0.1):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        last = state(cdp)
        try:
            if predicate(last):
                return last
        except Exception:                              # noqa: BLE001
            pass
        time.sleep(step)
    return last


def shoot(cdp, path):
    raw = cdp.call("Page.captureScreenshot", format="png").get("data")
    with open(path, "wb") as handle:
        handle.write(base64.b64decode(raw))
    return path


def open_page():
    browser = _browser()
    if not browser:
        return None
    profile = tempfile.mkdtemp(prefix="inline-profile-")
    port = 9900 + (os.getpid() % 90)
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
    return proc, cdp


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    png = os.path.join(OUT_DIR, "inline.png")
    if "--png" in sys.argv:
        png = sys.argv[sys.argv.index("--png") + 1]
    if not _browser():
        print("SKIP：本机没找到 Edge / Chrome，跳过就地展开探针。")
        return 0

    path = os.path.join(OUT_DIR, "probe.html")
    render_ui_fixture.build(path, load(), tab="chat")
    opened = open_page()
    if not opened:
        print("FAIL：浏览器没起来")
        return 2
    proc, cdp = opened
    checks = []
    shot = ""

    def check(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)})
        print("%-46s %s  %s" % (name, "OK  " if ok else "FAIL", detail))

    try:
        cdp.call("Page.navigate",
                 url="file:///" + os.path.abspath(path).replace("\\", "/"))
        ready = wait_state(cdp, lambda s: s["jobRows"] >= 2 and s["goalRows"] >= 1,
                           seconds=20.0)
        check("页面起来了，任务与目标都画出来了",
              bool(ready and ready["jobRows"] >= 2 and ready["goalRows"] >= 1),
              "jobs=%s goals=%s" % ((ready or {}).get("jobRows"),
                                    (ready or {}).get("goalRows")))

        # ① 点第一行任务 → 展开面插在它后面，且在列表里（不是栏底）
        first = cdp.eval(CLICK_JOB % 0)
        got = wait_state(cdp, lambda s: s["inlines"] == 1 and s["inlineKind"] == "job")
        check("点一行任务 → 展开面出现，且只有一块",
              got["inlines"] == 1 and got["inlineKind"] == "job",
              "inlines=%s kind=%s" % (got["inlines"], got["inlineKind"]))
        check("展开面是那一行后面的兄弟（不是另起一片）",
              str(got["inlinePrev"]).split(" ")[0] == "chat-side-job"
              and got["inlinePrevId"] == first,
              "prev=%s id=%s want=%s" % (got["inlinePrev"], got["inlinePrevId"],
                                         first))
        check("展开面长在任务列表里，不在右栏别处",
              got["inlineParent"] == "chat-side-job-list",
              "parent=%s" % got["inlineParent"])
        check("右栏拉开，栏头写的是这一条任务",
              got["open"] and got["openVisible"] and got["headKind"] == "任务"
              and got["headTitle"] != "未选中",
              "open=%s visible=%s kind=%s title=%s" % (
                  got["open"], got["openVisible"], got["headKind"],
                  got["headTitle"]))
        check("展开面里是这一条的内容（状态 / 任务号 / 结论至少两样）",
              got["inlineText"].count("任务号") >= 1
              and (got["inlineText"].count("执行节点") >= 1
                   or got["inlineText"].count("结论") >= 1),
              got["inlineText"][:110])

        # ② 点另一行 → 旧的必须消失
        second = cdp.eval(CLICK_JOB % 1)
        got = wait_state(cdp, lambda s: s["inlines"] == 1
                         and s["inlinePrevId"] == second)
        check("再点另一行 → 只留新的那一块，旧的收掉",
              got["inlines"] == 1 and got["inlinePrevId"] == second,
              "inlines=%s prev=%s want=%s" % (got["inlines"],
                                              got["inlinePrevId"], second))

        # ③ 目标：同样的规则，落在 #chat-goals 里
        cdp.eval(CLICK_GOAL % 0)
        got = wait_state(cdp, lambda s: s["inlines"] == 1
                         and s["inlineKind"] == "goal")
        check("点一行目标 → 展开面落在目标列表里那一行下面",
              got["inlineKind"] == "goal" and got["inlineParent"] == "chat-goals"
              and bool(got["goalInlineId"]),
              "kind=%s parent=%s after=%s" % (got["inlineKind"],
                                              got["inlineParent"],
                                              got["goalInlineId"]))
        check("切到目标之后，任务块那块旧的展开面没留着",
              got["inlines"] == 1, "inlines=%s" % got["inlines"])

        # ④ 左栏几何：目标块紧跟对话记录，不许沉到栏底
        geom = got["geom"] or {}
        fits = bool(geom) and geom["scrollContent"] <= geom["scrollHeight"] + 2
        check("目标块紧贴对话记录（间距就是栏内那一道 gap）",
              bool(geom) and (not fits or geom["gap"] <= 24),
              "gap=%s fits=%s scroll=%s/%s" % (geom.get("gap"), fits,
                                               geom.get("scrollHeight"),
                                               geom.get("scrollContent")))
        check("内容装得下时，栏底留白在目标块下面而不是它上面",
              bool(geom) and (not fits or geom["tail"] >= 32),
              "tail=%s fits=%s sideBottom=%s goalsBottom=%s" % (
                  geom.get("tail"), fits, geom.get("sideBottom"),
                  geom.get("goalsBottom")))

        # ⑤ 会话行：扁平行、单行省略、删除按钮默认不显形
        row = got["session"] or {}
        check("会话行没有边框也没有底色（扁平行）",
              row.get("border") == "0px 0px" and row.get("bg") in
              ("rgba(0, 0, 0, 0)", "transparent"),
              "border=%s bg=%s radius=%s" % (row.get("border"), row.get("bg"),
                                             row.get("radius")))
        check("会话标题单行省略，完整值挂在 title 上",
              row.get("titleWrap") == "nowrap"
              and row.get("titleOverflow") == "ellipsis"
              and bool(row.get("titleTitle"))
              and row.get("titleTitle") == row.get("titleText"),
              "wrap=%s overflow=%s title=%r" % (row.get("titleWrap"),
                                                row.get("titleOverflow"),
                                                row.get("titleTitle")))
        # 触屏 / 无 hover 的设备上它按设计常显（否则删不掉），所以这里分开判：
        # 有精确指针时必须是 0，粗指针时必须是 1。headless 浏览器两种都报 true，
        # 那时按 coarse 这一支算。
        want_opacity = "1" if row.get("coarse") else "0"
        check("删除按钮只在悬停/聚焦时显形（触屏常显）",
              row.get("delOpacity") == want_opacity,
              "opacity=%s want=%s coarse=%s" % (row.get("delOpacity"),
                                                want_opacity,
                                                row.get("coarse")))
        check("页面没有报错", not got["errors"],
              json.dumps(got["errors"], ensure_ascii=False)[:120])
        if png:
            shot = shoot(cdp, png)
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