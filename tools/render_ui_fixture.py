# -*- coding: utf-8 -*-
"""生成一张「控制台某一屏」的离线夹具（开发 / 评审用）。

为什么要这个脚本：界面改动"看起来对不对"只有渲染出来才知道，而控制台默认要求
登录、还要有一批真实任务数据才看得出布局问题。这个脚本起一个临时控制面、**真跑
一次网络探测**、把真实接口响应抓下来内联进一份自包含 HTML —— 于是无头浏览器打开
它就能看到与运行时一致的那一屏，不需要任何账号口令，也不依赖网络。

与 `render_layout_png.py` 的分工：那个画的是链路布局，这个画的是控制台页面。

用法：
    python tools/render_ui_fixture.py out.html [--tab setup] [--kind probe]

`--kind probe` 会把任务类型切到网络探测，用来检查探测相关字段（端口、作用域）
的排版；`--kind scan` 则检查源码审计那一套字段。

几个会**真点一次**的开关（不是只画静态画面）：`--stream` 点「发送」回放 SSE 帧；
`--job-mode plan` 点「规划任务」；`--apply-agent` 点插件市场的「应用到会话」再切到
对话页签；`--preset-form new|edit` 点「新建预设」/「编辑」，前者还会填满字段并保存。
它们的结果都以 base64 或原文写进 `<html>` 的属性（`data-apply-agent-result`、
`data-preset-result`、`data-preset-step` 等），用 `--dump-dom` 读回来即可断言——
照应 `tools/probe_graph_click.py` 的做法。

`--subagent` 是"子智能体派活"那一屏：往真实流里插一串 `subagent` 帧（3 个子智能体，
分别停在已完成 / 跑动中 / 失败上，另有一帧前端不认识、后端真会发的 `phase: note`），
隐含 `--stream`，并默认停在最后一个 `done` 之前。`--png` 再用无头浏览器出一张图，
存到与 `out` 同名的 `.png`（产物一般就放在 `.dsh-vision-toolkit/artifacts/` 下）。

**这一屏看的是"正在跑"的那一段。** 子智能体区块有两份来源：流里的 `subagent` 帧（画在
"正在生成"的气泡上），以及**落库消息上的 `subagents` 字段**（刷新后用同一个渲染器重画，
见 `chat.py` 的 `_subagent_rows`）。两者形状逐键一致，由 `tests/test_subagent.py` 的跨文件
断言钉住——`note` 字段就是那条断言发现的。

（以前它只能在流里看：`chat._trace_entry` 把 `delegate` 的返回值塞进 trace 的 `data` 时会被
`TRACE_DATA_LIMIT` 截断成非法 JSON，而消息上没有独立的 `subagents` 字段——于是刷新一次，
"哪几个子智能体各自带回了什么"就没了，只剩结论里那句"已派出 3 个子智能体"。）

所以这份夹具**不再**依赖"落库没有子智能体结构"这个前提。它停在 `done` 之前，只是因为
**捕获的那份 `persist` 帧是加 `subagents` 字段之前录的**——夹具回放的是真捕获的帧，不会
自己编一个字段出来。真实链路上刷新后这一块是在的：`tools/subagent_demo.py` 结尾打印的
那三行就是真落库行，不是夹具造的。
"""
import argparse
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agent, dshplugins                         # noqa: E402
from pentestd.server import serve_background                  # noqa: E402
from pentestd.store import Store                              # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")
ENDPOINTS = ["/api/v1/health", "/api/v1/setup", "/api/v1/jobs",
             "/api/v1/executors", "/api/v1/baselines", "/api/v1/engagements",
             "/api/v1/models", "/api/v1/scope", "/api/v1/users",
             "/api/v1/agent-modes",
             # 会话入口三项选择与 DSH 集成：这一屏要看到的是"默认值跟随全局、
             # 插件来源的 Agent 也在列表里"，因此这些接口必须回放**真实响应**，
             # 而不是夹具里另造一份。
             "/api/v1/session-options", "/api/v1/agents", "/api/v1/policies",
             "/api/v1/skills", "/api/v1/dsh",
             "/api/v1/hub/ops", "/api/v1/hub/installed",
             # 第三道闸的状态卡。夹具的假后端只回放这里列出的接口，漏一个的后果
             # 不是空白而是**"加载失败：夹具未准备该接口"**——那和真实缺陷长得
             # 一模一样，排查时会一路往后端查。
             "/api/v1/authz",
             # 外部工具卡（部署页签里唯一会改这台机器的一处）。它的每条拒绝文案
             # 都写在响应里，所以必须回放真实响应。
             "/api/v1/tools", "/api/v1/tools/ops",
             # 全局链路。它是**全部 engagement 合并**后的视图（合并 + 加前缀 +
             # 切窗口在服务端完成），所以只能回放真实响应——夹具自己造一份的话，
             # 造出来的必然是没有重名节点的理想数据，而"同名节点塌成一个"恰恰
             # 是这张图最容易出的错。
             "/api/v1/graph",
             "/api/v1/chat/tools", "/api/v1/chat/sessions"]

# 演示用的意图：一句话里同时含"作用域内的网段""范围外的地址""代码路径"三种目标，
# 于是提案列表会同时出现可下发、被门禁拒绝、需要先配模型三种状态。这一屏要让人
# 看见的正是**门禁在起作用**，而不是一切顺利的样子。
PLAN_INTENT = ("先看一下 10.0.0.0/24 这个办公段开了哪些服务，"
               "再确认 8.8.8.8 的常见端口，另外审计一下 /srv/app 的登录逻辑")

# 流式那一屏要说的话：一句能同时触发"下发任务"和"给结论"的请求，于是截图上既有
# 工具芯片（证明工具在流式通道上真的跑到了），也有正在往外冒的回答正文。
STREAM_MESSAGE = "扫描 /srv/demo，目标是审计上传逻辑"
# 对话记录里先垫一段别的问答：截图要能看出"历史在上、正在跑的在下面"，
# 而不是同一句话上下各出现一次。
HISTORY_MESSAGE = "现在有哪些执行节点在线？"

# 夹具里不引 auth.js / boot.js：直接渲染控制台，不需要登录门。
#
# **web/ 下新增脚本时必须加进这张表**。夹具是脱离项目目录打开的（截图产物落在
# artifacts/ 下），留在 <script src="..."> 里的相对路径会 404，而页面照常渲染——
# 只是**少了那个模块的全部行为**。症状是"夹具看起来全对，只有某某交互不响应"，
# 而截图恰恰看不出来。split.js 就是这样漏过一次：分栏条画得出来、按下去没反应。
#
# fold.js（本轮新增）又漏了第二次，而且比第一次更难看出来：折叠头是纯 HTML+CSS，
# **画得出来、箭头也在**，只是点下去没有任何反应——而那与"用户根本没点"长得
# 一模一样。所以这次除了补进表里，还加了一条跨文件断言
# （`test_ui_contract.FixtureScriptCoverageTestCase`）：这两张表合起来必须覆盖
# web/ 下的每一个 .js。名单靠人记，就一定会被忘掉第二次。
# panels.js（本轮新增）是第三次：仪表盘 / 流量 / 请求重发 / 工作空间 / 审批记录 /
# 日志六页的**全部**渲染与按钮绑定都在它里面。漏掉它，夹具上那六个页签点得到、
# 点开是空的——空面板与"这一页就是这样"长得一模一样，截图同样看不出来。
INLINE_SCRIPTS = ("app.js", "split.js", "fold.js", "rail.js", "theme.js",
                  "settings.js", "chat.js", "guide.js", "skills.js", "panels.js")
DROPPED_SCRIPTS = ("auth.js", "boot.js")

# 子智能体派活：**按 `pentestd/subagent.py` 的真实发帧写**，不是照前端契约文档想象的。
# 字段就是从那里抄的（`_run_child` 的四条 emit），包括 done 上的 `status`。
#
# 三条刻意的选择：
#   * 3 个子智能体，**分别停在三种状态上**（已完成 / 跑动中 / 失败）——只画一种状态的
#     夹具，看不出"状态列到底有没有在变"；
#   * 失败的那个带 `error`（界面上的"未完成：…"就从它来），且 `summary` 为空——
#     后端失败时确实可能交不出结论；
#   * 中间插一帧 **`phase: "note"`**。它真实存在（`subagent.py` 报告格式没跟上时发），
#     却**不在**前端契约的四类里。它留在夹具里是有用的：这张截图同时证明了"多出来的
#     phase 不会把整轮对话搞崩"——夹具把任何异常记到 `data-fixture-errors`，
#     用 `--dump-dom` 读得到。
SUBAGENT_SCRIPT = [
    ("subagent", {"phase": "start", "index": 1, "total": 3, "role": "recon",
                  "policy": "probe", "toolCount": 2, "dropped": []}),
    ("subagent", {"phase": "step", "index": 1, "total": 3, "role": "recon", "step": 1}),
    ("subagent", {"phase": "tool", "index": 1, "total": 3, "role": "recon",
                  "name": "start_probe", "ok": True,
                  "brief": "已下发探测任务 job-403f8682", "durationMs": 12}),
    ("subagent", {"phase": "step", "index": 1, "total": 3, "role": "recon", "step": 2}),
    ("subagent", {"phase": "note", "index": 1, "total": 3, "role": "recon",
                  "message": "报告未按三行格式给出，已按原文收录。"}),
    ("subagent", {"phase": "done", "index": 1, "total": 3, "role": "recon",
                  "ok": True, "status": "done",
                  "summary": "10.0.0.0/24 有 7 台存活主机，开放端口集中在 22/80/445",
                  "steps": 2, "durationMs": 1234, "error": ""}),
    ("subagent", {"phase": "start", "index": 2, "total": 3, "role": "web",
                  "policy": "audit", "toolCount": 4, "dropped": ["toolkit_probe"]}),
    ("subagent", {"phase": "step", "index": 2, "total": 3, "role": "web", "step": 1}),
    ("subagent", {"phase": "tool", "index": 2, "total": 3, "role": "web",
                  "name": "list_findings", "ok": True,
                  "brief": "3 条待验证的上传缺陷", "durationMs": 41}),
    ("subagent", {"phase": "start", "index": 3, "total": 3, "role": "creds",
                  "policy": "audit", "toolCount": 3, "dropped": []}),
    ("subagent", {"phase": "step", "index": 3, "total": 3, "role": "creds", "step": 1}),
    ("subagent", {"phase": "tool", "index": 3, "total": 3, "role": "creds",
                  "name": "spray_creds", "ok": False,
                  "brief": "目标不在探测作用域内，已拒绝：10.20.0.0/24",
                  "durationMs": 8}),
    ("subagent", {"phase": "done", "index": 3, "total": 3, "role": "creds",
                  "ok": False, "status": "failed", "summary": "",
                  "steps": 1, "durationMs": 512,
                  "error": "模型调用失败：连接超时"}),
]

# 截图用的浏览器：与 tools/probe_graph_click.py 同一份候选表（那个文件用同一个浏览器
# 读 DOM 断言，这个只出图）。EDGE_PATH 可以覆盖。
from _browser import BROWSERS    # noqa: E402


def _browser() -> str:
    for path in BROWSERS:
        if path and os.path.exists(path):
            return path
    return ""


def screenshot(html_path: str, png_path: str, width: int = 1440, height: int = 1000,
               wait_ms: int = 12000) -> str:
    """无头浏览器把夹具截成 PNG，返回 PNG 路径。

    本机没有 Edge / Chrome 时返回空串而**不是**失败：图是给人看的，缺浏览器不该让
    `--png` 变成一次构建中断（用法与 `probe_graph_click.py` 对缺浏览器的处理一致）。

    `--virtual-time-budget` 而不是"等 N 秒"：夹具里的流式回放是一串 `setTimeout`，
    虚拟时钟把它们一次推到底，截图因此停在**确定的一帧**上，不受机器快慢影响。
    """
    browser = _browser()
    if not browser:
        print("SKIP：本机没找到 Edge / Chrome，跳过截图。")
        return ""
    profile = tempfile.mkdtemp(prefix="ui-shot-profile-")
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
           "--hide-scrollbars", "--user-data-dir=" + profile,
           "--window-size=%d,%d" % (int(width), int(height)),
           "--virtual-time-budget=%d" % int(wait_ms),
           "--screenshot=" + os.path.abspath(png_path),
           "file:///" + os.path.abspath(html_path).replace("\\", "/")]
    subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                   errors="replace", timeout=300, check=False)
    if not os.path.exists(png_path):
        print("FAIL：浏览器没有生成截图：%s" % png_path)
        return ""
    return png_path

FIXTURE_HEAD = r"""<script>
// 无头浏览器没有控制台可看：某处一抛异常，页面只会"少一块"，而少的那块恰恰是最
// 需要看见的。这里把异常与未处理的 Promise 拒绝记到 <html> 的属性上，
// `msedge --headless --dump-dom` 就能直接读到，不必再靠肉眼猜哪一步没跑。
window.__FIXTURE_ERRORS__ = [];
function __fixtureNote__(text) {
  window.__FIXTURE_ERRORS__.push(text);
  document.documentElement.setAttribute(
    'data-fixture-errors', window.__FIXTURE_ERRORS__.join(' | '));
}
window.addEventListener('error', function (event) {
  __fixtureNote__('error: ' + String((event.error && event.error.stack) || event.message));
});
window.addEventListener('unhandledrejection', function (event) {
  __fixtureNote__('rejection: ' + String((event.reason && event.reason.stack) || event.reason));
});
// 夹具不引 auth.js（不需要登录门），但设置页会用 window.PentestAuth 判断角色：
// Agent 预设能否切换、用户管理卡片是否显示都取决于它。给一个管理员桩，让夹具
// 渲染的是最常见的那一屏（可切换、可见用户管理），而不是一个永远点不动的空壳。
// csrfHeaders 也必须给：apiPost 每次都会调它，缺了这个函数，所有写操作
// （包括"规划任务"）都会失败在一个与界面无关的地方，看起来像产品坏了。
window.PentestAuth = {
  getUser: function () { return { id: 'u-fixture', username: 'admin', role: 'admin' }; },
  isAdmin: function () { return true; },
  getRole: function () { return 'admin'; },
  getToken: function () { return ''; },
  csrfHeaders: function (base) { return base || {}; },
};
// 夹具：把真实抓到的接口响应按路径回放，前端代码一行不改。
window.__FIXTURE_DATA__ = __DATA__;
window.fetch = function (input, init) {
  var data = window.__FIXTURE_DATA__;
  var url = String(input);
  var index = url.indexOf('/api/v1');
  var raw = (index >= 0 ? url.slice(index) : url);
  var path = raw.split('?')[0];
  var query = raw.indexOf('?') >= 0 ? raw.slice(raw.indexOf('?')) : '';
  // **带查询串的键优先。** `/engagements/{eid}/report` 与
  // `/engagements/{eid}/report?job=job-xxxx` 是**两篇不同的文档**（汇总报告 vs 某一次
  // 执行的任务报告），而老写法一律按路径回放，于是夹具里点一次任务看起来只是把汇总
  // 报告又渲染了一遍——正文一模一样、卡头却换了名字。那正是夹具最不该做的事：
  // 它得"漏"一块让人发现，而不是编一份看起来对的内容。
  var key0 = (query && data[path + query]) ? path + query : path;
  var method = String((init && init.method) || 'GET').toUpperCase();
  // 会话入口三项选择的保存（`POST /chat/sessions/{id}/config`）：**把请求体回显**成
  // 一份 config。固定回放一份预先抓好的响应是错的——那份响应和这次请求无关，于是
  // "选一项 → 落库 → 回填"这条闭环在夹具里看起来永远是失败的：保存成功了，
  // `renderEntry()` 却按旧响应把下拉框填回默认值。界面是对的，是夹具在撒谎，
  // 而这条闭环正是「插件安装后应用到会话」的终点。
  if (method === 'POST' && /\/chat\/sessions\/[^\/]+\/config$/.test(path)) {
    var sent = {};
    try { sent = JSON.parse((init && init.body) || '{}') || {}; } catch (e) { sent = {}; }
    var known = (data['/api/v1/agents'] || {}).agents || [];
    var label = '';
    for (var k = 0; k < known.length; k++) {
      if (sent.agentId && known[k].id === sent.agentId) label = known[k].label;
    }
    return Promise.resolve(new Response(JSON.stringify({
      config: {
        agentId: sent.agentId || '', agentLabel: label,
        agentPinned: !!sent.agentId,
        policyId: sent.policyId || '', policyPinned: !!sent.policyId,
        modelId: sent.modelId || '', steps: 0, role: 'admin', authRequired: true
      }
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  }
  // 流式接口单独处理：回放**真实的 SSE 帧**（夹具生成时真跑了一轮，逐帧收下来的），
  // 中间留出间隔，于是浏览器侧走的完全是真实解析路径——分帧、增量文本、工具事件。
  // 帧跑完 __STREAMSTOP__ 帧就**停住而不关闭流**：这样截图停在"正在生成"的那一刻，
  // 不依赖截图时机，也就不会因为机器快慢截到不同的画面。
  if (path === '/api/v1/chat/stream') {
    var frames = data.__stream__ || [];
    var stop = __STREAMSTOP__;
    var stream = new ReadableStream({
      start: function (controller) {
        var encoder = new TextEncoder();
        var cursor = 0;
        (function pump() {
          if (cursor >= frames.length) { controller.close(); return; }
          if (stop && cursor >= stop) return;      // 停在半途，流保持打开
          var frame = frames[cursor++];
          controller.enqueue(encoder.encode(
            'event: ' + frame[0] + '\ndata: ' + JSON.stringify(frame[1]) + '\n\n'));
          setTimeout(pump, __STREAMGAP__);
        })();
      },
    });
    return Promise.resolve(new Response(stream, {
      status: 200, headers: { 'Content-Type': 'text/event-stream' },
    }));
  }
  // 新建 / 修改自定义预设：**按请求体回显**，并把结果并进内存里的 `/agents` 目录，
  // 让"保存 → 表单收起 → 列表里出现新条目 → 提示条念出 id"这条闭环真的走通。
  // 固定回放一份抓好的 201 是错的：那份的 id 与这次提交无关，而提示条要念出 id，
  // 念错了会让人以为"创建成功但 id 不对"。
  if (method === 'POST' && /\/agents\/presets(\/[^\/]+)?$/.test(path)) {
    var pbody = {};
    try { pbody = JSON.parse((init && init.body) || '{}') || {}; } catch (e) { pbody = {}; }
    var pmatch = path.match(/\/agents\/presets\/([^\/]+)$/);
    var pid = pmatch ? decodeURIComponent(pmatch[1])
                     : ('custom-' + (pbody.handle || 'new'));
    var made = {
      id: pid, label: pbody.label || pid, summary: pbody.summary || '',
      note: pbody.note || '', prompt: pbody.prompt || '',
      steps: Number(pbody.steps) || 6, skills: pbody.skills || [],
      derivedFrom: '', createdBy: '', createdAt: 0, updatedAt: 0
    };
    var catalog = data['/api/v1/agents'];
    if (!catalog) { catalog = data['/api/v1/agents'] = { agents: [], activeId: '', dsh: null }; }
    var seen = false;
    catalog.agents = (catalog.agents || []).map(function (item) {
      if (item.id !== pid) return item;
      seen = true;
      return Object.assign({}, item, {
        label: made.label, summary: made.summary, note: made.note,
        steps: made.steps, skills: made.skills
      });
    });
    if (!seen) {
      catalog.agents.push({
        id: pid, label: made.label, summary: made.summary, note: made.note,
        steps: made.steps, source: 'custom', sourceLabel: '自定义', kind: 'custom',
        builtin: false, custom: true, editable: true, deletable: true,
        skills: made.skills, personaChars: 0, default: false, active: false
      });
    }
    // `/session-options` 是**会话入口那条下拉框**读的那一份（`chat.loadOptions`），
    // 必须跟着一起更新。只并进 `/agents` 的话，这个假后端会自己造出"新建成功、下拉
    // 框里却没有"这个症状——而它和真实缺陷长得一模一样，排查时会一路查到后端去。
    // 同一个目录的两份视图保持同步，是假后端的职责，不是被测代码的。
    var sopt = data['/api/v1/session-options'];
    if (sopt && sopt.agents) {
      var soptSeen = false;
      sopt.agents = sopt.agents.map(function (item) {
        if (item.id !== pid) return item;
        soptSeen = true;
        return Object.assign({}, item, {
          label: made.label, summary: made.summary,
          steps: made.steps, skills: made.skills
        });
      });
      if (!soptSeen) {
        sopt.agents.push({
          id: pid, label: made.label, summary: made.summary,
          steps: made.steps, skills: made.skills,
          source: 'custom', sourceLabel: '自定义', kind: 'custom',
          builtin: false, custom: true, editable: true, deletable: true,
          default: false, active: false
        });
      }
    }
    data['/api/v1/agents/presets/' + pid] = { preset: made, pinnedSessions: 0 };
    return Promise.resolve(new Response(JSON.stringify({
      preset: made, created: !pmatch
    }), { status: pmatch ? 200 : 201, headers: { 'Content-Type': 'application/json' } }));
  }
  // 删除自定义预设：回一份带影响条数的结论，并把目录里那条摘掉——不摘的话
  // "删除成功，列表里还在"会让夹具自己看起来像坏的。
  if (method === 'POST' && /\/agents\/presets\/[^\/]+\/delete$/.test(path)) {
    var dpid = decodeURIComponent(path.match(/\/agents\/presets\/([^\/]+)\/delete$/)[1]);
    var cat2 = data['/api/v1/agents'];
    if (cat2 && cat2.agents) {
      cat2.agents = cat2.agents.filter(function (item) { return item.id !== dpid; });
    }
    // 两份视图一起摘（理由见上面新建那一支的注释）。
    var sopt2 = data['/api/v1/session-options'];
    if (sopt2 && sopt2.agents) {
      sopt2.agents = sopt2.agents.filter(function (item) { return item.id !== dpid; });
    }
    return Promise.resolve(new Response(JSON.stringify({
      deleted: true, id: dpid, affectedSessions: 0
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  }
  // 排队任务（t86）：夹具给它两条，否则这一块在截图里永远不出现——而它恰恰是最
  // 容易摆错位置的一块（要贴在输入框上方、不能进 `.chat-field` 参与框高）。
  // 第二条故意带 `error`：界面上"这条没能开跑：…"那一行也只有这里看得到。
  // `sessionId` 必须**回显 query 里那个**：`loadQueue()` 会丢掉与当前会话不符的
  // 响应（否则会把上一个会话排的东西画到这个输入框上），写死一个 id 等于没这份桩。
  if (method === 'GET' && path === '/api/v1/chat/queue') {
    var qsid = '';
    try { qsid = decodeURIComponent((query.match(/[?&]sessionId=([^&]*)/) || [])[1] || ''); }
    catch (e) { qsid = ''; }
    return Promise.resolve(new Response(JSON.stringify({
      sessionId: qsid,
      items: [
        { id: 'q-fixture-1', text: '顺手看一眼 /etc/hosts，确认有没有内网解析记录。',
          at: 0, userId: 'u-fixture', role: 'admin', error: '' },
        { id: 'q-fixture-2', text: '扫完把结果按端口归一下类，别只贴原始输出。',
          at: 0, userId: 'u-fixture', role: 'admin',
          error: '这条没能开跑：上一轮被停止，排队的不会自动续跑。' },
      ],
      active: false, limit: 50
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  }
  var payload = data[key0];
  if (payload === undefined) {
    payload = data[key0.replace(/\/engagements\/[^\/]+/, '/engagements/E-xxx')];
  }
  if (payload === undefined) {
    payload = data[key0.replace(/\/chat\/sessions\/[^\/]+$/, '/chat/sessions/C-xxx')];
  }
  // 会话入口三项选择的保存路径（`POST /chat/sessions/{id}/config`）也要能回放。
  // 上面那条正则锚在行尾，匹配不到带 `/config` 后缀的路径——于是"在会话入口选一项"
  // 这条真实链路在夹具里永远走不通，而它恰好是「插件安装后应用到会话」的终点。
  if (payload === undefined) {
    payload = data[key0.replace(/\/chat\/sessions\/[^\/]+\//, '/chat/sessions/C-xxx/')];
  }
  var body = payload === undefined
    ? JSON.stringify({ error: '夹具未准备该接口：' + path })
    : JSON.stringify(payload);
  return Promise.resolve(new Response(body, {
    status: payload === undefined ? 404 : 200,
    headers: { 'Content-Type': 'application/json' },
  }));
};
</script>
"""

# 必须是 raw 字符串：这段 JS 里有 `replace(/\s+/g, ' ')`，Python 不认 `\s` 这个转义，
# 会报 SyntaxWarning，将来还会变成硬错误。别改成普通字符串。
FIXTURE_FOOTER = r"""<script>
window.addEventListener('load', function () {
  setTimeout(function () {
    var gate = document.getElementById('auth-gate');
    if (gate) gate.classList.add('hidden');
    // 真实运行时由 boot.js 在拿到登录态后揭开控制台；夹具直接揭开。
    var shell = document.getElementById('console-shell');
    if (shell) shell.classList.remove('hidden');
    var ui = window.PentestUI;
    if (!ui) return;
    // 这一档在命令行叫 `chat`（"要看对话那一屏"），面板的 id 却是 `dsh`
    // （`#panel-dsh`，页签的 data-tab 也是 dsh）。两个名字各在一处都对，但
    // `switchTab('chat')` 找不到面板：整页停在空白，而空白与"夹具没生成"看起来
    // 一模一样。映射只做这一次，下面各处判的都是 PAGE。
    var PAGE = __TAB__ === 'chat' ? 'dsh' : __TAB__;
    ui.switchTab(PAGE);
    // 设置页的 init 与数据加载都由 boot.js 驱动（真实运行时按启动时序执行），而夹具
    // 不引 boot.js。只调 load 不够：dom 还没 cacheDom，整个模块会静默什么都不做，
    // 渲染出来的会是一份没跑过 JS 的静态 HTML。
    if (PAGE === 'settings' && window.PentestSettings) {
      window.PentestSettings.init();
      window.PentestSettings.load(true);
      // 分组切换走真实入口（bindNavEvents → showGroup），顺带验证事件绑定。
      if (__GROUP__) {
        var nav = document.querySelector(
          '#settings-nav [data-settings-group="' + __GROUP__ + '"]');
        if (nav) nav.click();
      }
      // 插件市场：填关键词并点「搜索」，走真实代码路径（→ searchHub → 回放的 fetch
      // → renderHub）。不直接调 searchHub，是为了顺带验证事件绑定与查询串拼装。
      if (__HUBQ__) {
        var box = document.getElementById('hub-query');
        if (box) box.value = __HUBQ__;
        var run = document.getElementById('hub-search');
        if (run) run.click();
      }
    }
    // 使用说明页：正文是本地静态数据，但**渲染由 PentestGuide.init() 驱动**——不调它
    // 就只画得出 index.html 里那句「正在准备正文…」占位。这一支原先缺着，于是
    // `--tab guide` 截出来的永远是一块空白，而页内说明书恰恰是最需要「看一眼」的页面：
    // 行内标记、表格溢出、目录高亮这些只有画出来才知道对没对。
    // 不调 ensureLoaded()：那会去请求 /setup 与 /models，而夹具要的是正文。
    if (PAGE === 'guide' && window.PentestGuide) {
      window.PentestGuide.init();
    }
    // 对话页：init 只绑定不拉数据（未登录时不该发请求），因此还要显式 reload 一次。
    // 夹具不引 boot.js，这一步等价于 boot.js 在登录后做的那一下。
    if (PAGE === 'report' && __REPORTJOB__ > 0) {
      // 报告页左侧目录里的行是**异步**画出来的（任务列表要先拉回来），所以要等。
      // 真点一次那一行，走完整的事件委托路径——不直接调 showReport()，顺带验证
      // "委托绑定 + 换文档 + 卡头改写"这一整条链路。结果写进 data-report-job
      // （`msedge --headless --dump-dom` 读得到）。
      //
      // 选的是**任务行**，`[data-job-id]` 因此要排掉空串那一个——空串正是
      // 「本目标汇总」，它是 `data-job-id=""`，属性选择器照样命中它。
      var picks = 0;
      (function pick() {
        var rows = document.querySelectorAll(
          '#report-side .rpt-row[data-job-id]:not([data-job-id=""])');
        if (rows.length >= __REPORTJOB__) {
          rows[__REPORTJOB__ - 1].click();
          var head = document.getElementById('report-doc-head');
          document.documentElement.setAttribute('data-report-job',
            head ? head.textContent : '(no head)');
          return;
        }
        if (picks++ < 80) setTimeout(pick, 50);
        else __fixtureNote__('报告目录里始终没有出现任务行');
      })();
    }
    if (PAGE === 'dsh' && window.PentestChat) {
      window.PentestChat.init();
      window.PentestChat.reload();
      if (__STREAM__) {
        // 走真实交互：等输入框可用 → 填内容 → 点「发送」。不直接调 send()，
        // 是为了顺带验证事件绑定、以及"发送期间禁用输入/显示停止按钮"这些状态切换。
        // 等待是必须的：reload 要先把会话与模型拉回来，没模型时发送按钮是禁用的。
        var tries = 0;
        (function attempt() {
          var box = document.getElementById('chat-input');
          var go = document.getElementById('chat-send');
          if (box && go && !go.disabled) {
            box.value = __STREAMMSG__;
            go.click();
            __fixtureNote__('clicked after ' + tries + ' tries');
            return;
          }
          if (tries++ < 80) setTimeout(attempt, 50);
          else __fixtureNote__('send button never became enabled');
        })();
      }
    }
    // 「插件安装后应用到会话」的验收：真点一次「应用到会话」按钮，再切到对话页签
    // 看会话入口的 Agent 有没有跟上。不直接调 applyAgent，是为了顺带验证事件委托
    // ——那颗按钮是动态生成的，走的是 hubInstalled 上的 [data-apply-agent] 委托。
    if (__APPLY_AGENT__) {
      var tries = 0;
      // 每一步都留痕：无头浏览器没有控制台，探针"什么都没写出来"和"跑到一半断了"
      // 从结果上看是完全一样的，而这两件事的下一步排查方向完全不同。
      var step = function (name) {
        document.documentElement.setAttribute('data-apply-agent-step', name);
      };
      step('start');
      (function attempt() {
        var btn = document.querySelector('[data-apply-agent]');
        if (!btn) {
          if (tries++ < 80) return setTimeout(attempt, 100);
          step('button-not-found');
          __fixtureNote__('没找到「应用到会话」按钮');
          return;
        }
        var wanted = btn.getAttribute('data-apply-agent');
        step('clicking:' + wanted);
        // 对话模块必须先 init：boot.js 在真实运行时无条件调它，而夹具不引 boot.js。
        // 不 init 的话 dom 是空的，applyAgent 会在 "dom.agent.value = id" 上抛异常。
        var chat = window.PentestChat;
        if (chat && chat.init) chat.init();
        btn.click();
        setTimeout(function () {
          // 切到对话页签再读：截图要能直接看见会话入口那三项里的 Agent 变成了谁
          if (ui.switchTab) ui.switchTab('dsh');
          if (chat && chat.ensureLoaded) chat.ensureLoaded();
          setTimeout(function () {
            var sel = document.getElementById('chat-agent');
            var hint = document.getElementById('chat-entry-hint');
            var notice = document.getElementById('hub-message');
            step('done');
            document.documentElement.setAttribute(
              'data-apply-agent-result',
              btoa(unescape(encodeURIComponent(JSON.stringify({
                wanted: wanted,
                selected: sel ? sel.value : null,
                optionCount: sel ? sel.options.length : 0,
                options: sel ? Array.prototype.map.call(sel.options, function (o) { return o.value; }) : [],
                hint: hint ? hint.textContent : '',
                notice: notice ? notice.textContent.replace(/\s+/g, ' ').trim().slice(0, 160) : ''
              })))));
          }, 1200);
        }, 700);
      })();
    }
    // 把 Agent 下拉**展开成可见列表**。原生 `<select>` 的下拉层不参与页面渲染，
    // 截图截不到里面的选项；设 size=N 会让它退化成列表框，每条预设连同来源标签都画在
    // 页面上。用来检查"每条预设被标成了哪一类来源"——这一列错了不会让任何东西报错，
    // 只会让人去插件市场里找一个不存在的包。
    if (__EXPANDAGENT__) {
      var estep = function (name) {
        document.documentElement.setAttribute('data-expand-agent-step', name);
      };
      var etries = 0;
      var esettled = false;
      (function eattempt() {
        // 与 --preset-form 同时用时，**先等它建完再切页签**：那个探针在设置页按卡片
        // 操作，提前切走它会找不到卡片。等到结果出现，下拉框里才会有重名的那一条
        // ——"标签重名时补 id"只在真的重名时触发，不这么排就永远走不到那条分支。
        if (__PRESETFORM__ && !document.documentElement.getAttribute('data-preset-result')) {
          if (etries++ < 150) return setTimeout(eattempt, 100);
        }
        var chat = window.PentestChat;
        if (chat && chat.init) chat.init();
        if (ui.switchTab) ui.switchTab('dsh');
        if (chat && chat.ensureLoaded) chat.ensureLoaded();
        // `ensureLoaded` / `loadOptions` 都是异步的，而"选项数 ≥ 2"这个重试条件在
        // **上一次**渲染之后就已经满足了。所以第一遍只负责把请求发出去，等它落定
        // 再读第二遍；不这样的话读到的是旧列表，新建的预设永远不在里面，而探针会
        // 如实报告"没有这一条"——一个会让人相信的错误结论。
        if (!esettled) {
          esettled = true;
          return setTimeout(eattempt, 1500);
        }
        var sel = document.getElementById('chat-agent');
        if (!sel || sel.options.length < 2) {
          if (etries++ < 80) return setTimeout(eattempt, 100);
          estep('no-options');
          return __fixtureNote__('Agent 下拉没有选项');
        }
        sel.size = sel.options.length;
        sel.style.width = '460px';
        estep('expanded:' + sel.options.length);
        document.documentElement.setAttribute('data-agent-options',
          btoa(unescape(encodeURIComponent(JSON.stringify(
            Array.prototype.map.call(sel.options, function (o) {
              return { value: o.value, text: (o.textContent || '').replace(/\s+/g, ' ').trim() };
            }))))));
      })();
    }
    // 自定义预设表单的验收：真点「新建预设」/「编辑」，填字段，点保存，再把提示条
    // 的内容带回来。走真实交互（事件绑定、payload 收集、提交后的重绘）而不是直接调
    // submitAgentForm —— 后者只能证明那个函数存在。
    if (__PRESETFORM__) {
      var pstep = function (name) {
        document.documentElement.setAttribute('data-preset-step', name);
      };
      // 期望值由夹具算出来（它知道这条预设声明了几份手册），而不是写死在探针里：
      // 本机没装技能手册时那个数就是 0，"勾选为 0"是对的，不该报成缺陷。
      document.documentElement.setAttribute('data-preset-expect-skills',
        String(__PRESETSKILLS__));
      pstep('start');
      var ptries = 0;
      (function pattempt() {
        var list = document.getElementById('agent-modes-list');
        var cards = list ? list.querySelectorAll('[data-mode-id]') : [];
        if (!cards.length) {
          if (ptries++ < 80) return setTimeout(pattempt, 100);
          pstep('no-cards');
          return __fixtureNote__('预设卡片一张都没有');
        }
        var want = __PRESETFORM__;
        var target = null;
        if (want === 'edit') {
          target = list.querySelector('[data-preset-action="edit"]');
          if (!target) { pstep('no-edit-button'); return __fixtureNote__('没有「编辑」按钮'); }
        } else {
          target = document.getElementById('agent-new');
          if (!target) { pstep('no-new-button'); return __fixtureNote__('没有「新建预设」按钮'); }
        }
        pstep('clicking:' + want);
        target.click();
        setTimeout(function () {
          // 展开态在**这里**读，不在 click 之后立刻读：`openAgentForm` 要先 await
          // 一次手册目录（`loadSkillChoices`），同步读永远读到"还没展开"，而那与
          // "点了没反应"从结果上一样。保存会把表单收起，所以这个值必须在提交前取。
          document.documentElement.setAttribute('data-preset-open-after-click',
            document.getElementById('agent-form').classList.contains('hidden')
              ? 'no' : 'yes');
          if (want === 'edit') {
            // **不提交**：编辑表单是一张要给人看的画面，提交掉就看不见了。
            // 回填对不对从这里读，"更新有没有生效"由 HTTP 用例覆盖。
            var handle = document.getElementById('agent-handle');
            var prompt = document.getElementById('agent-prompt');
            var boxes = document.getElementById('agent-skills');
            var title = document.getElementById('agent-form-title');
            var form = document.getElementById('agent-form');
            document.documentElement.setAttribute('data-preset-handle',
              handle ? handle.value : '');
            document.documentElement.setAttribute('data-preset-handle-locked',
              handle && handle.disabled ? 'yes' : 'no');
            document.documentElement.setAttribute('data-preset-prompt-chars',
              prompt ? String(prompt.value.length) : '0');
            document.documentElement.setAttribute('data-preset-skills-selected',
              boxes ? String(Array.prototype.filter.call(boxes.options, function (o) {
                return o.selected; }).length) : '0');
            document.documentElement.setAttribute('data-preset-title',
              encodeURIComponent(title ? title.textContent : ''));
            document.documentElement.setAttribute('data-preset-form-open',
              form && !form.classList.contains('hidden') ? 'yes' : 'no');
            pstep('done');
            return;
          }
          var set = function (id, value) {
            var node = document.getElementById(id);
            if (!node) return;
            node.value = value;
            // 走 input 事件：名称→标识的自动联动就挂在这个事件上，直接赋值
            // 不改 DOM 是测不到那条联动的。
            node.dispatchEvent(new Event('input', { bubbles: true }));
          };
          set('agent-label', '内网横向专项');
          set('agent-summary', '拿到第一台跳板之后的横向推进口径');
          set('agent-note', '适合已经有一个可用入口、目标是证明可达路径的场合。');
          set('agent-steps', '14');
          set('agent-prompt', '以横向移动为主线。每一步先说明当前所处的网段与已达边界，'
            + '再决定下一步走哪条；推不动的那条明确标成阻塞，不要平均用力。');
          var boxes = document.getElementById('agent-skills');
          if (boxes && boxes.options && boxes.options.length) {
            boxes.options[0].selected = true;
          }
          var handleBox = document.getElementById('agent-handle');
          document.documentElement.setAttribute('data-preset-handle',
            handleBox ? handleBox.value : '(none)');
          pstep('filled');
          var submit = document.getElementById('agent-submit');
          if (submit) { submit.click(); pstep('submitted'); }
          setTimeout(function () {
            var msg = document.getElementById('agent-mode-message');
            var form = document.getElementById('agent-form');
            document.documentElement.setAttribute('data-preset-result',
              encodeURIComponent(msg ? msg.textContent : '(no notice)'));
            document.documentElement.setAttribute('data-preset-form-open',
              form && !form.classList.contains('hidden') ? 'yes' : 'no');
            pstep('done');
          }, 1200);
        }, 800);
      })();
    }
    var select = document.getElementById('job-kind');
    if (select) {
      select.value = __KIND__;
      select.dispatchEvent(new Event('change'));
    }
    // 「可用能力」面板默认折叠，而折叠时整块内容**在 DOM 里根本不存在**
    // （`renderTools` 只在展开时才建节点）。于是"工具目录长什么样"这件事
    // 任何一张截图都看不到——包括刚接上那几件侵入式工具之后的目录。
    //
    // 点一次之后要**轮询**再写属性：`reload()` 是异步的，它拉回工具目录后会
    // 再渲一次，点完立刻读会读到"还没开"。
    if (__DOCK__) {
      var dockBtn = document.getElementById('chat-' + __DOCK__);
      if (dockBtn) dockBtn.click();
      var dockTries = 0;
      (function checkDock() {
        // 判据是 CSS 模式块**自己**用的那个属性（`[data-dock="…"]`），不是另起一个
        // 类名：两个来源说同一件事时，改了一个忘了另一个的症状是截图里目录正常、
        // 断言却说没打开。
        var root = document.querySelector('#chat-quick[data-dock="' + __DOCK__ + '"]');
        // ⚠️ 但**不能只等属性**。属性是同步写上的，卡片要等 `/chat/tools` 或
        // `/skills` 回来才建——只等属性的话，记录到的是"还没加载完"的那一瞬间。
        // （实测踩过：prompts 与 tools 两个模式都记成了 NO-CHIP，而页面上卡片
        // 明明是好的，因为检查跑在数据到达之前。）
        var first = document.querySelector(
          '#chat-quick .dock-chip, #chat-quick .chat-quick-chip');
        if (root && first) {
          document.documentElement.setAttribute('data-tools-open', 'true');
          // 再真点一次第一项，把「点击 → 填进输入框」这条链走完，结果写进
          // data-dock-fill。只看图判断不了这件事：输入框可能被滚动遮住，而
          // "点下去到底填没填进去"恰恰是这一版改动的全部意义。
          var input = document.getElementById('chat-input');
          if (input) {
            var before = input.value;
            first.click();
            document.documentElement.setAttribute('data-dock-fill',
              (before ? 'APPEND:' : 'REPLACE:') + input.value);
            // 再点**第二**张卡片：这时输入框已经有内容了，于是顺带走一遍"追加"
            // 那条分支。两条都必须看——"点一下把用户刚打了一半的字抹掉"正是这类
            // 交互最容易犯的错，而它**只在非空输入框上**才表现出来。
            var chips = document.querySelectorAll(
              '#chat-quick .dock-chip, #chat-quick .chat-quick-chip');
            if (chips.length > 1) {
              chips[1].click();
              document.documentElement.setAttribute('data-dock-fill2', input.value);
            }
          } else {
            document.documentElement.setAttribute('data-dock-fill', 'NO-INPUT');
          }
          return;
        }
        if (dockTries++ < 120) return setTimeout(checkDock, 100);
        document.documentElement.setAttribute('data-tools-open', root ? 'true' : 'false');
        document.documentElement.setAttribute('data-dock-fill',
          first ? 'NO-INPUT' : 'NO-CHIP');
        __fixtureNote__('dock panel never opened');
      })();
    }
    // AI 规划：走真实代码路径——点切换按钮（事件委托 → applyJobMode）、填意图、
    // 点"规划任务"（→ runPlan → 被回放的 fetch 拦住 → renderPlan）。不直接调
    // applyJobMode/runPlan，是为了顺带验证事件绑定有没有接对。
    if (__JOBMODE__ === 'plan') {
      var mode = document.querySelector('.job-mode-btn[data-job-mode="plan"]');
      if (mode) mode.click();
      var intent = document.getElementById('plan-intent');
      if (intent) intent.value = __PLANINTENT__;
      var run = document.getElementById('plan-run');
      if (run) run.click();
    }
    // 模型配置表单：把协议切到指定值。
    //
    // 「思考模式 / reasoning_effort」两行只在协议 = DeepSeek 官方时出现，而表单
    // 默认是 OpenAI 兼容——不主动切一次，那一整块新表单在任何截图里都不存在，
    // 于是没人会看出它长什么样、有没有填错。
    //
    // 切完把结果记在 <html> 上：`hidden` 是真看不见还是被写进了 class，肉眼分不出，
    // 属性可以直接读。用完 `--dump-dom` 断言，**不要靠看图**。
    if (__MODELPROVIDER__) {
      var providerSel = document.getElementById('model-provider');
      if (providerSel) {
        providerSel.value = __MODELPROVIDER__;
        providerSel.dispatchEvent(new Event('change', { bubbles: true }));
        document.documentElement.setAttribute('data-model-provider',
                                              providerSel.value);
        var thinkingRow = document.getElementById('model-thinking-row');
        document.documentElement.setAttribute(
          'data-thinking-row-hidden',
          thinkingRow ? String(thinkingRow.classList.contains('hidden')) : 'missing');
        var hint = document.getElementById('model-thinking-hint');
        document.documentElement.setAttribute(
          'data-thinking-hint-hidden',
          hint ? String(hint.classList.contains('hidden')) : 'missing');
      }
    }
    // 控制台的滚动发生在内部容器里，整页截图看不到下半部分，因此用 focus 把目标
    // 元素滚进视口（scrollIntoView 会滚动最近的可滚动祖先）。**延后一拍再滚**：
    // 规划是异步的（await 一次回放的 fetch），立刻滚只会滚到一张还没有内容的空列表。
    setTimeout(function () {
      var focus = document.getElementById('__FOCUS__');
      if (focus) focus.scrollIntoView({ block: 'start' });
    }, __ROLLDELAY__);
  }, 250);
});
</script>
"""


class _ProbeTarget(http.server.BaseHTTPRequestHandler):
    """探测目标：一个会回标题的最小 HTTP 服务，让夹具里有真实的探测结果。"""

    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"<html><head><title>Fixture Target</title></head><body>ok</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Server", "FixtureTarget/1.0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _StreamingModel:
    """会流式回话的假模型：第一轮要求调用工具，第二轮逐片给出结论。

    夹具要的是**真实的 SSE 帧**，而不是在 HTML 里手写一段假事件——手写的那份迟早
    和真实帧格式脱节，于是"夹具好看、产品坏了"。所以这里起一个真的流式端点，
    让控制面按正常路径跑完整轮，再把收到的帧原样回放给浏览器。
    """

    def __init__(self, gap: float = 0.0):
        self.gap = gap
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                body = json.loads(raw or "{}")
                outer.requests.append(body)
                has_tool = any(message.get("role") == "tool"
                               for message in body.get("messages") or [])
                user_text = _last_user_text(body)
                # 端点必须**两种都答**：整包与流式走的是同一条业务，只不过一条
                # 一次给完、一条分片给。只会 SSE 的话，非流式那条路会拿到一坨
                # 事件流去当 JSON 解析，报出"响应不是合法 JSON"——而那个错误
                # 看起来像模型配置错了，离真正的原因很远。
                if not body.get("stream"):
                    payload = json.dumps(outer.json_script(has_tool, user_text),
                                         ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                for frame in outer.script(has_tool, user_text):
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                    if outer.gap:
                        time.sleep(outer.gap)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self):
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d" % (host, port)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def plan(self, has_tool, user_text=""):
        """这一轮该干什么：`("tool", 名字, 参数)` 还是 `("text", 文本, None)`。

        两种响应形态共用这一段，是为了让"整包"和"流式"答的是同一件事——
        分开写两份迟早会出现"流式扫 A、整包扫 B"这种对不上的现象。
        两种形态都返回三元组，解包处就不必各写一支。
        """
        if not has_tool:
            # 按问题选工具，让"对话记录"读起来自然：问节点状态就查节点，
            # 要求扫描就下发扫描。两个都答成一个工具，历史那一屏会显得答非所问。
            if "节点" in user_text:
                return ("tool", "list_executors", {})
            return ("tool", "start_scan", {"target": "/srv/demo",
                                           "objective": "审计上传逻辑"})
        if "节点" in user_text or not user_text:
            return ("text", "当前有 1 个执行节点在线：fixture-node（能力：scan）。", None)
        return ("text", "已完成。`start_scan` 已把 /srv/demo 加入源码审计队列，"
                        "审计目标是上传逻辑；结果可在「任务列表」与「漏洞」页签查看。",
                None)

    def json_script(self, has_tool, user_text=""):
        kind, first, second = self.plan(has_tool, user_text)
        if kind == "tool":
            return {"choices": [{"message": {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "call-1", "type": "function",
                                "function": {"name": first,
                                             "arguments": json.dumps(
                                                 second, ensure_ascii=False)}}]},
                "finish_reason": "tool_calls"}]}
        return {"choices": [{"message": {"role": "assistant", "content": first},
                             "finish_reason": "stop"}]}

    def script(self, has_tool, user_text=""):
        kind, first, second = self.plan(has_tool, user_text)
        if kind == "tool":
            raw = json.dumps(second, ensure_ascii=False)
            step = max(1, len(raw) // 4)
            frames = [_sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call-1", "type": "function",
                 "function": {"name": first, "arguments": ""}}]},
                "finish_reason": None}]})]
            if raw != "{}":
                for index in range(0, len(raw), step):
                    frames.append(_sse({"choices": [{"delta": {"tool_calls": [
                        {"index": 0, "function": {
                            "arguments": raw[index:index + step]}}]},
                        "finish_reason": None}]}))
            frames.append(_sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}))
            frames.append("data: [DONE]\n\n")
            return frames
        text = first
        frames = []
        size = max(1, len(text) // 8)
        for index in range(0, len(text), size):
            frames.append(_sse({"choices": [
                {"delta": {"content": text[index:index + size]}, "finish_reason": None}]}))
        frames.append(_sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
        frames.append("data: [DONE]\n\n")
        return frames


def _last_user_text(body) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _sse(payload) -> str:
    return "data: %s\n\n" % json.dumps(payload, ensure_ascii=False)


def _stream_post(base: str, path: str, body: dict):
    """POST 一个流式接口，把 SSE 帧收成 `[[事件名, 数据], …]`。"""
    request = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    frames, event, data = [], "", []
    with urllib.request.urlopen(request, timeout=60) as response:
        for raw in response:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line == "":
                if data:
                    frames.append([event or "message",
                                   json.loads("\n".join(data))])
                event, data = "", []
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)
    return frames


def _post(base: str, path: str, body: dict) -> dict:
    request = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=30) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


# 本机很可能**一份技能手册都没有**：夹具起在隔离的 dsh-home 上，`data/skills/` 也是
# 空的，而 `/api/v1/skills` 回的是真实目录 → `{"skills": [], "total": 0}`。于是
# 「技能手册」那一格在任何机器上都是空态，而"目录长什么样、点一下往输入框里填什么"
# 正是要看的东西。所以像 `SUBAGENT_SCRIPT` / `STREAM_MESSAGE` 一样，夹具自己造一份
# 有数据的版本（`--fake-skills`）。名字和角色照真实的 dsh-purge 手册取，免得看的人
# 以为线上长这样。
FAKE_SKILLS = [
    ("recon-pipeline", "信息收集",
     "从域名到资产面的完整铺开：子域枚举、存活探测、指纹识别、URL 收集。",
     "目标只有一个域名、还没有资产清单时。"),
    ("subdomain-takeover", "信息收集",
     "比对 CNAME 与解析结果，找出可接管的悬挂记录。",
     "资产面铺开之后，筛一类特定的高危配置。"),
    ("smb-relay", "内网",
     "在已获取立足点的网段里收集 SMB 签名配置，评估中继可行性。",
     "拿到第一个立足点、准备横向时。"),
    ("win-privesc", "内网",
     "服务、计划任务、权限与补丁面的本地提权检查清单。",
     "手上已有一个普通权限的 shell 时。"),
    ("weblogic-exploit", "利用",
     "WebLogic 反序列化点的识别与验证步骤。",
     "指纹识别出 WebLogic 之后。"),
]


def fake_skills() -> dict:
    """造一份 `/api/v1/skills` 的**部分**覆盖：只换 skills/total/all/roles。

    `dsh` 那一块是控制面实况（插件装没装、隔离没隔离），照原样留着——连它一起
    伪造的话，「说明」页会显示一个不存在的插件环境。
    """
    items = [{"name": name, "description": desc, "whenToUse": when, "role": role,
              "source": "夹具", "note": "", "chars": 4200, "enabled": True,
              "shadowed": []}
             for name, role, desc, when in FAKE_SKILLS]
    counts = {}
    for item in items:
        counts[item["role"]] = counts.get(item["role"], 0) + 1
    return {"skills": items, "total": len(items), "all": len(items),
            "roles": [{"id": key, "count": counts[key]} for key in
                      sorted(counts, key=lambda k: (-counts[k], k))]}


def collect(intrusive=None, intrusive_targets=None,
            authz_runtime=None, authz_targets="", real_plugins=False) -> dict:
    """起临时控制面 + 目标服务，真跑一次探测、一次入队与一次流式对话，抓回全部响应。

    `intrusive` / `intrusive_targets` 直接透给控制面：默认空 = 全部关闭，于是
    「部署」页自检显示的是默认态；给了才看得到放行态那一版。

    `authz_runtime` / `authz_targets` 走**另一条路**：启动参数留空，起完之后用接口
    改一次。两者画出来的是不同的东西——前者是"启动参数给的授权"，后者是"管理员在
    界面上改过"，面板上那条「本次运行中已改」与可复制的启动命令**只在后一条路上
    出现**。不主动构造，那段分支就永远没有被看过。
    """
    workspace = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    store = Store(os.path.join(workspace.name, "fixture.db"))
    target = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeTarget)
    target.daemon_threads = True
    target.handle_error = lambda *args: None
    threading.Thread(target=target.serve_forever, daemon=True).start()
    httpd, _thread = serve_background(store, WEB_DIR, "127.0.0.1", 0,
                                      auth_required=False,
                                      intrusive=intrusive,
                                      intrusive_targets=intrusive_targets)
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    if real_plugins:
        # 「已安装插件」那一节的判定行只有在**真的装了插件**时才看得到，而夹具默认起在
        # 临时 workspace 上（那里一个插件都没有，于是永远只能看到空态）。把平台 home
        # 指到项目里那一份，**只读**它：`snapshot()` 只读 package.json 与
        # `cordis.patch.yml`，不写、不执行。
        #
        # **必须在 `serve_background` 之后**：起控制面时它会把平台 home 指到自己的
        # data_dir（真实部署里那是对的），先设后起等于没设。
        dshplugins.set_platform_home(os.path.join(
            os.path.dirname(WEB_DIR), "data", dshplugins.PLATFORM_HOME_DIR))
        dshplugins.invalidate()
    model = _StreamingModel()
    try:
        _post(base, "/api/v1/jobs", {
            "kind": "probe", "target": "127.0.0.1",
            "ports": str(target.server_address[1]),
            "objective": "本机暴露面自检", "authorization": "AUTH-FIXTURE-001"})
        executor = agent.LocalExecutor(store, name="fixture-node", verbose=False)
        executor.run_once()                       # 真跑：连通性 + 指纹 + 链路写入
        # 再真跑一次源码审计：只有源码审计会产出智能体协作轨迹。目标成对给出缺陷版
        # 与修复版，差分验证才跑得出 verified——缺 patched 就只剩"静态确认"，
        # 轨迹里的验证阶段会退化成 inconclusive，看不出真实形态。
        demo = os.path.join(ROOT, "prototype", "demo")
        scan_job = _post(base, "/api/v1/jobs", {
            "kind": "scan", "target": os.path.join(demo, "vulnapp"),
            "patched": os.path.join(demo, "vulnapp_patched"),
            "objective": "示例代码审计（含智能体协作轨迹）",
            "authorization": "AUTH-FIXTURE-002"})
        executor.run_once()
        _post(base, "/api/v1/jobs", {             # 再放一个待执行的审计任务
            "kind": "scan", "target": os.path.join(demo, "webapp"),
            "objective": "示例代码审计（尚未领取）"})
        # 对话页必须有一个启用中的模型，否则输入框是禁用的、发送按钮也点不动，
        # 那一屏就成了空壳。所以这里登记一个真的会流式回话的端点。
        _post(base, "/api/v1/models", {
            "name": "流式演示模型", "provider": "openai_compat",
            "baseUrl": model.base_url + "/v1", "model": "fixture-1",
            "apiKeySource": "none", "temperature": 0.2, "maxTokens": 512,
            "timeoutS": 30})
        # 再登记一条 **DeepSeek 官方** 配置，走真实接口。设置页里只有这个协议会
        # 出现「思考模式 / reasoning_effort」两个下拉框，不真有一条这样的配置，
        # 那一整块表单在截图里永远看不到，也就没人会发现它长什么样。
        # maxTokens 故意留 2048：思考模式下这偏小，测试连接会给出一行提示——
        # 那一行同样需要被看到。
        created = _post(base, "/api/v1/models", {
            "name": "DeepSeek 官方（生产）", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com", "model": "deepseek-flash",
            "apiKeySource": "env", "apiKeyRef": "DEEPSEEK_API_KEY",
            "temperature": 0.2, "maxTokens": 2048, "timeoutS": 90,
            "thinking": "enabled", "reasoningEffort": "high",
            "note": "思考模式开着，maxTokens 偏小"})
        # 直接写库而不是调 `/models/{id}/test`：那是**真发请求**到
        # api.deepseek.com 的接口，夹具不该有出网行为（离线跑会超时、CI 里
        # 会变成一个随机失败的用例）。这里要的只是那一行提示长什么样。
        deepseek_id = (created.get("profile") or {}).get("id")
        if deepseek_id:
            store.record_profile_test(
                deepseek_id, True, "连接正常", 412,
                warning="思考模式已开启，但 max_tokens=2048 偏小——推理 token 也算在 "
                        "completion 里，预算可能被推理吃光，表现为回答为空而 "
                        "finish_reason=length。建议提到 4096 以上，或留空以使用官方"
                        "默认（思考模式 64K）。 未指定 reasoning_effort 已配 high。")
        # 分两步取帧，为的是截图能读：先留下一段**别的**问答当历史，再动捕一次
        # 流式回合。同一句话出现两遍（一遍跑完的、一遍正在跑的）看着像重复，
        # 而"先问节点、再让它扫描"才是一段正常的对话。
        #
        # 会话要**带上目标**（就是刚才那次审计建出来的那个）。不带的话它落在
        # 「未归属」里，于是侧边栏那条"点目标 → 看它的对话"的路径在夹具里永远
        # 显示不出来——而那正是这一屏要展示的东西。
        history = _post(base, "/api/v1/chat", {
            "message": HISTORY_MESSAGE,
            "engagementId": scan_job.get("engagementId") or ""})
        stream = _stream_post(base, "/api/v1/chat/stream",
                              {"message": STREAM_MESSAGE})
        # 动捕用的那条会话随后删掉。它只用来取真实的 SSE 帧，留在这里会让对话
        # 列表多出一条没人看过的记录；浏览器要跑的那一轮会自己建一条。
        captured = next((data for name, data in stream if name == "start"), {})
        if captured.get("sessionId") and captured["sessionId"] != history.get("sessionId"):
            _post(base, "/api/v1/chat/sessions/%s/delete" % captured["sessionId"], {})
        # 会话归属：把那条历史对话挂到**界面会默认选中的目标**上（`/engagements`
        # 按创建时间倒序，第一条就是最新建的那个，也就是这一轮流式对话刚建的
        # `/srv/demo`）。不挂的话它落在「未归属」里，于是侧边栏那条"点目标 → 读
        # 它名下的对话"的路径在夹具里永远显示不出来——而那正是这一屏要展示的东西。
        #
        # 直接改库而不走接口：归属的正常写入路径是"**建会话时**带上目标"，而那条
        # 路只有浏览器那一侧走得到（夹具这两条会话是脚本用 `/chat` 建的）。这里
        # 补的是一个夹具数据的前置条件，不是产品行为。
        newest = (store.list_engagements() or [{}])[0].get("id") or ""
        if newest and history.get("sessionId"):
            store.conn.execute("UPDATE chat_sessions SET engagement_id=? WHERE id=?",
                               (newest, history["sessionId"]))
            store.conn.commit()
        # 自定义 Agent 预设：截图要能看到「自定义」徽章、编辑/删除按钮，以及编辑时
        # 表单里回填的正文与手册勾选。所以夹具里**真建一条**（走真实接口，不是往
        # `/agents` 的响应里塞一条假的）——塞假的那条会与 `/agents/presets/{id}`
        # 对不上，而编辑表单正是取那一个，于是夹具会显示一个缺口径的空表单。
        #
        # 正文之外的字段也要给：**什么都不声明**的预设编不出回归（一张空表单也能
        # "回填正确"）。所以挑一份真实存在的手册声明进去，编辑态必须有勾选。
        # 本机没装出技能手册时（空目录）就不声明，期望值跟着变成 0——断言里那份
        # 期望值也从这里传出去，避免把"这台机器没装插件"报成界面缺陷。
        preset_skill_count = 0
        try:
            available = ((_get(base, "/api/v1/skills") or {}).get("skills") or [])
            declared = [available[0]["name"]] if available else []
            preset_skill_count = len(declared)
            _post(base, "/api/v1/agents/presets", {
                "label": "内网横向专项", "handle": "lateral",
                "summary": "拿到第一台跳板之后的横向推进口径",
                "note": "适合已经有一个可用入口、目标是证明可达路径的场合。",
                "steps": 14, "skills": declared,
                "prompt": "以横向移动为主线。每一步先说明当前所处的网段与已达边界，"
                          "再决定下一步走哪条；推不动的那条明确标成阻塞，不要平均用力。"
                          "结论按「已到达 / 已验证 / 已阻塞」三段组织。"})
        except Exception:                        # noqa: BLE001 —— 建不出预设不该毁掉整张夹具
            preset_skill_count = 0
        if authz_runtime:
            # 走**真实接口**而不是往响应里塞假字段：塞假的那条会绕开服务端的四条校验，
            # 于是"界面上看起来能开"与"服务端真的接受"这两件事在夹具里再也对不上。
            try:
                _post(base, "/api/v1/authz", {
                    "enabled": [t.strip() for t in str(authz_runtime).split(",") if t.strip()],
                    "targets": [t.strip() for t in str(authz_targets).split(",") if t.strip()]})
            except Exception:                    # noqa: BLE001 —— 设不上不该毁掉整张夹具
                pass
        data = {path: _get(base, path) for path in ENDPOINTS}
        data["__presetSkills__"] = preset_skill_count
        # 自定义预设的详情按**真实 id** 存一份，不搞占位键：编辑的是具体哪一条，
        # 占位键会把"点编辑 A 却填进 B 的正文"这种缺陷盖掉，而那正是要防的。
        for _preset in (data.get("/api/v1/agents") or {}).get("agents") or []:
            if _preset.get("custom"):
                _key = "/api/v1/agents/presets/%s" % _preset["id"]
                try:
                    data[_key] = _get(base, _key)
                except Exception:                # noqa: BLE001
                    continue
        data["__stream__"] = stream
        data["__streamMessage__"] = STREAM_MESSAGE
        sessions = (data.get("/api/v1/chat/sessions") or {}).get("sessions") or []
        if sessions:
            # 带 id 的会话详情按占位键存一份，由页面里的回放函数把任意 id 映射过来
            data["/api/v1/chat/sessions/C-xxx"] = _get(
                base, "/api/v1/chat/sessions/%s" % sessions[0]["id"])
            # `POST .../config` 不走这里：它在页面里的假 fetch 中按请求体回显
            # （见 FIXTURE_HEAD）。固定抓一份的响应会和那次请求无关。
        # 规划接口是 POST，要**真的跑一次**才拿得到提案。夹具里没配模型，因此走的是
        # 确定性降级路径——那一屏展示的正是"没模型也能拆，但源码审计仍被挡在门外"，
        # 比一切顺利的样子更接近真实部署的第一天。
        data["/api/v1/jobs/plan"] = _post(base, "/api/v1/jobs/plan",
                                          {"intent": PLAN_INTENT})
        # 插件市场那一屏要有真实条目才看得出排版（名称/版本/已验证/特性/安装目标/
        # 仓库链接）。走真实接口取一小页即可；目录本身有磁盘缓存，不会每次都拉 9.8 MB。
        try:
            data["/api/v1/hub/catalog"] = _get(
                base, "/api/v1/hub/catalog?q=pentest&limit=6")
        except Exception:                       # noqa: BLE001 —— 离线时夹具仍要能出图
            data["/api/v1/hub/catalog"] = {"ok": False, "items": [], "total": 0,
                                           "count": 0, "limit": 6, "offset": 0,
                                           "hasMore": False,
                                           "reason": "夹具生成时目录不可达"}
        # 带 id 的子资源（/engagements/{eid}/…）：**每个 engagement 各存一份真实响应**。
        #
        # 以前只存第一份、再用 E-xxx 占位键糊弄所有 id，于是夹具里每个 engagement
        # 长得一模一样——"切换 engagement（或切换节点），内容却没跟着变"这类缺陷
        # 在夹具里永远看不出来，只有真实运行时才暴露。占位键保留，指向节点最多的
        # 那一份，作为页面用未知 id 取数时的兜底。
        engagements = (data.get("/api/v1/engagements") or {}).get("engagements") or []
        # 任务列表**重新抓一次**：`data` 是在上面那批 POST 之前建的，而 AI 规划那一步
        # 真的会下发一个任务（`planner` 的降级路径也会）。不重抓的话，报告页会出现
        # 「目录里没有这一条、正文的执行记录里却有」——一个只有夹具才有的矛盾，
        # 而它会把人引向"侧边栏漏渲染了一行"这个并不存在的缺陷。
        try:
            data["/api/v1/jobs"] = _get(base, "/api/v1/jobs")
        except Exception:                            # noqa: BLE001
            pass
        all_jobs = (data.get("/api/v1/jobs") or {}).get("jobs") or []
        best_eid, best_score = "", -1
        for item in engagements:
            eid = item["id"]
            for suffix in ("", "/findings", "/assets", "/report"):
                key = "/api/v1/engagements/%s%s" % (eid, suffix)
                try:
                    data[key] = _get(base, key)
                except Exception:                   # noqa: BLE001 —— 单个子资源拿不到不该毁掉整张夹具
                    continue
            # **每一个任务各抓一份任务报告**（`/report?job=...`）。
            # 只抓汇总那一份的话，报告页左侧的目录点下去会回放同一篇正文——而卡头
            # 换了名字。夹具于是自己造出一个"点哪条都一样"的假象，它和真实缺陷长得
            # 一模一样，排查时会一路查到后端去。假后端的职责是把真实链路铺通，
            # 不是把没人看的地方糊过去。
            for job in all_jobs:
                if (job.get("engagementId") or "") != eid:
                    continue
                key = "/api/v1/engagements/%s/report?job=%s" % (eid, job["id"])
                try:
                    data[key] = _get(base, key)
                except Exception:                   # noqa: BLE001
                    continue
            counts = ((data.get("/api/v1/engagements/%s" % eid) or {})
                      .get("engagement") or {}).get("counts") or {}
            score = sum(int(counts.get(name) or 0) for name in
                        ("intents", "facts", "findings", "assets")) + (1 if counts else 0)
            if score > best_score:
                best_eid, best_score = eid, score
        if best_eid:
            for suffix in ("", "/findings", "/assets", "/report"):
                key = "/api/v1/engagements/%s%s" % (best_eid, suffix)
                if key in data:
                    data["/api/v1/engagements/E-xxx%s" % suffix] = data[key]
            for job in all_jobs:
                if (job.get("engagementId") or "") != best_eid:
                    continue
                key = "/api/v1/engagements/%s/report?job=%s" % (best_eid, job["id"])
                if key in data:
                    data["/api/v1/engagements/E-xxx/report?job=%s" % job["id"]] = data[key]
    finally:
        httpd.shutdown()
        httpd.server_close()
        target.shutdown()
        target.server_close()
        model.stop()
        store.close()
        workspace.cleanup()
    return data


def _inject_subagent(frames: list) -> list:
    """把子智能体那一串帧插进真实帧里，返回一份新列表。

    插在**第一个 `tool` 帧之后**：那时父轮已经调过一次工具（界面上有芯片了），
    子智能体接着跑，看起来就是"父轮把活派出去、正在等它们"这一段。这一轮没有
    `tool` 帧（模型一次就答完了）时挂在末尾——至少还是同一条对话。
    """
    out = [list(frame) for frame in frames]
    at = len(out)
    for index, frame in enumerate(out):
        if frame and frame[0] == "tool":
            at = index + 1
            break
    return out[:at] + [[name, payload] for name, payload in SUBAGENT_SCRIPT] + out[at:]


def _stop_before_done(frames: list) -> int:
    """回放停在最后一个 `done` 帧**之前**，返回那个下标。

    `--stream-stop` 的语义是"放到第 N 帧就停住、且不关闭流"，所以传 `done` 的下标
    就等于不放 `done`。帧一跑完，`finish()` 会用服务端落库的那份重画整条消息——而
    **这份夹具捕获的 `persist` 帧是早在消息带 `subagents` 字段之前录的**，回放它就
    等于回放"落库没有子智能体结构"的旧行为，实时气泡里那一块会跟着消失，截图就永远
    截不到。

    这不代表真实链路如此：`chat._subagent_rows()` 现在把行挂在消息上了，刷新之后用的是
    同一个渲染器。这里停住纯粹是夹具的帧太旧，不是产品行为。找不到 `done` 就返回 0
    （跑完），此时夹具至少还是能出图，而不是停在一个空页面上。
    """
    for index in range(len(frames) - 1, -1, -1):
        if frames[index] and frames[index][0] == "done":
            return index
    return 0


def build(out_path: str, data: dict, tab: str = "setup", kind: str = "probe",
          focus: str = "", job_mode: str = "manual", group: str = "",
          hub_query: str = "", stream: bool = False, stream_stop: int = 0,
          apply_agent: bool = False, preset_form: str = "",
          expand_agent: bool = False, subagent: bool = False,
          model_provider: str = "", dock: str = "", theme: str = "",
          accent: str = "", frost: str = "", report_job: int = 0) -> str:
    def read(name):
        with open(os.path.join(WEB_DIR, name), "r", encoding="utf-8") as handle:
            return handle.read()

    # 子智能体那一屏：真点一次「发送」，但流里插进 SUBAGENT_SCRIPT 那串帧。
    # 两个默认值都由这里定，而不是让调用方每次记得传：`--subagent` 少了 `stream`
    # 就是一张空截图（没有正在跑的那一轮，区块无处可挂），少了 stop 就会在帧跑完
    # 时被落库版本替换掉。
    if subagent:
        frames = _inject_subagent(data.get("__stream__") or [])
        data["__stream__"] = frames
        stream = True
        if not stream_stop:
            stream_stop = _stop_before_done(frames)

    html = read("index.html")
    html = html.replace('<link rel="stylesheet" href="style.css">',
                        "<style>\n%s\n</style>" % read("style.css"))
    for name in DROPPED_SCRIPTS:
        html = html.replace('<script src="%s"></script>' % name, "")
    for name in INLINE_SCRIPTS:
        html = html.replace('<script src="%s"></script>' % name,
                            "<script>\n%s\n</script>" % read(name))
    # 主题：夹具页面里 localStorage 是空的，所以首屏那段脚本里的默认值就是最终生效的
    # 那一个。直接改那个默认值，比往页面里塞一段 localStorage 更省事，也不会因为
    # headless 的存储策略不同而时灵时不灵。
    #
    # 注意这也意味着**不传 --theme 时渲染的是产品默认主题**（液态玻璃），而不是
    # 午夜——想看午夜得显式指定。夹具不该有自己的默认主题，否则截图与线上不一致。
    #
    # 三条外观轴各自改自己那一行的默认值。替换的是 `apply(...)` 的第四个实参，
    # 而不是 `var pick = '...'`——首屏脚本现在一次落四个属性（主题 / 重点色 / 磨砂 / 透明度；
    # 前三项走 apply()，透明度那一段是另一套写法），
    # 一个变量已经装不下它。找不到就当场报错，而不是静默渲染成默认档：静默的话，
    # 截出来的是默认外观，而调用方以为自己在看另一档。
    def force(attr, default, value, label):
        if not value:
            return html
        needle = "'%s', '%s')" % (attr, default)
        if needle not in html:
            raise SystemExit("找不到首屏脚本里 %s 的默认值，%s 无法生效"
                             "（index.html 里那段被改过？）" % (label, label))
        return html.replace(needle, "'%s', '%s')" % (attr, value))

    html = force("pentest.theme", "glass", theme, "--theme")
    html = force("pentest.accent", "theme", accent, "--accent")
    html = force("pentest.frost", "theme", frost, "--frost")
    head = FIXTURE_HEAD.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    head = (head.replace("__STREAMSTOP__", str(int(stream_stop)))
                .replace("__STREAMGAP__", "45"))
    footer = (FIXTURE_FOOTER.replace("__TAB__", json.dumps(tab))
              .replace("__KIND__", json.dumps(kind))
              .replace("__JOBMODE__", json.dumps(job_mode))
              .replace("__PLANINTENT__", json.dumps(PLAN_INTENT, ensure_ascii=False))
              .replace("__GROUP__", json.dumps(group or ""))
              .replace("__HUBQ__", json.dumps(hub_query or ""))
              .replace("__STREAM__", "true" if stream else "false")
              .replace("__APPLY_AGENT__", "true" if apply_agent else "false")
              .replace("__EXPANDAGENT__", "true" if expand_agent else "false")
              .replace("__PRESETFORM__", json.dumps(preset_form or ""))
              .replace("__MODELPROVIDER__", json.dumps(model_provider or ""))
              .replace("__DOCK__", json.dumps(dock or ""))
              .replace("__PRESETSKILLS__",
                       str(int(data.get("__presetSkills__") or 0)))
              .replace("__STREAMMSG__",
                       json.dumps(data.get("__streamMessage__") or STREAM_MESSAGE,
                                  ensure_ascii=False))
              .replace("__ROLLDELAY__", "400" if job_mode == "plan" else "0")
              .replace("__REPORTJOB__", str(int(report_job or 0)))
              .replace("__FOCUS__", focus or ""))
    html = html.replace("</head>", head + "</head>")
    html = html.replace("</body>", footer + "</body>")

    directory = os.path.dirname(os.path.abspath(out_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(html)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="生成控制台离线渲染夹具")
    parser.add_argument("out", help="输出 HTML 路径")
    parser.add_argument("--tab", default="setup",
                        choices=["graph", "chat", "findings", "assets", "report",
                                 "jobs", "setup", "settings", "guide"])
    parser.add_argument("--kind", default="probe", choices=["probe", "scan"])
    parser.add_argument("--job-mode", default="manual", choices=["manual", "plan"],
                        help="任务下发方式页签：manual（手动指定）或 plan（AI 规划）")
    parser.add_argument("--focus", default="",
                        help="渲染后滚到该元素 id（控制台的滚动在内部容器里，"
                             "整页截图看不到下半部分）")
    parser.add_argument("--group", default="",
                        help="设置页的初始分组：agents / models / policies / plugins / users")
    parser.add_argument("--hub-query", default="",
                        help="设置页插件市场里预填的关键词（渲染前会自动点一次搜索）")
    parser.add_argument("--stream", action="store_true",
                        help="对话页：真点一次「发送」，回放抓到的 SSE 帧")
    parser.add_argument("--stream-stop", type=int, default=0,
                        help="回放到第几帧停住（0=跑完）。停在半途能截到"
                             "「正在生成」的画面，且不受截图时机影响")
    parser.add_argument("--apply-agent", action="store_true",
                        help="设置页插件市场：真点一次「应用到会话」，再切到对话页签，"
                             "把会话入口的 Agent 选中值写进 data-apply-agent-result")
    parser.add_argument("--preset-form", default="", choices=["", "new", "edit"],
                        help="设置页 Agent 预设：new 真点「新建预设」并填满字段后保存，"
                             "edit 真点某张自定义卡片上的「编辑」；结果写进 "
                             "data-preset-result / data-preset-step")
    parser.add_argument("--expand-agent", action="store_true",
                        help="会话入口：把 Agent 下拉展开成列表（截得到每条选项），"
                             "并把全部选项写进 data-agent-options"
                             "（base64 JSON，用 msedge --dump-dom 读回）")
    parser.add_argument("--subagent", action="store_true",
                        help="对话页：往真实流里插一串 subagent 事件（3 个子智能体，"
                             "分别停在已完成/跑动中/失败），用来截「子智能体派活」那一屏；"
                             "隐含 --stream，并默认停在最后一个 done 之前")
    parser.add_argument("--dock", choices=["tools", "skills"], default="",
                        help="对话页：真点一次坞里那一格（可用能力 / 技能手册）再截。"
                             "没展开的那一格内容**在 DOM 里根本不存在**（只在该模式下"
                             "才建节点），所以这是唯一能看到目录长什么样的方式；"
                             "点击结果写进 data-dock-fill / data-dock-fill2，展开结果"
                             "写进 data-tools-open，都可用 --dump-dom 断言。"
                             "留空 = 停在默认的**收起态**（坞里没有任何一格被展开）")
    parser.add_argument("--tools", action="store_true",
                        help="（旧名，等价于 --dock tools，保留是为了不打断既有调用）")
    parser.add_argument("--real-plugins", action="store_true",
                        help="用项目 data/dsh-home 里**真实的**插件目录渲染「已安装」那一节"
                             "（只读）。不传时夹具起在临时目录上，那里一个插件都没有，"
                             "于是判定行、宿主插件那一类永远看不到")
    parser.add_argument("--fake-skills", action="store_true",
                        help="把 /api/v1/skills 换成一份**有数据**的合成目录（只换 "
                             "skills/total/all/roles，dsh 那块保留实况）。本机一份手册"
                             "都没装时，不加这个参数的话「技能手册」那一格永远是空态")
    parser.add_argument("--intrusive", default="",
                        help="侵入式动作放行类别（逗号分隔或 all），当成 "
                             "--allow-intrusive 传给控制面。默认空 = 全部关闭，"
                             "于是「部署」页自检显示的是默认态而不是放行态。"
                             "必须与 --intrusive-targets 一起给，否则控制面会"
                             "fail-closed 拒绝每一次调用")
    parser.add_argument("--intrusive-targets", default="",
                        help="侵入式动作的目标白名单，逗号分隔")
    parser.add_argument("--authz-runtime", default="",
                        help="启动参数留空、起完之后**用接口**开启这几类（逗号分隔），"
                             "画的是「管理员在界面上改过」那一版：面板上会出现"
                             "「本次运行中已改」、回到启动参数的说明与可复制的启动命令。"
                             "与 --intrusive 是两条不同的路径，不要同时用")
    parser.add_argument("--authz-targets", default="",
                        help="--authz-runtime 搭配的白名单（逗号分隔）")
    parser.add_argument("--model-provider", default="",
                        help="设置页模型配置表单：把协议切到该值（如 deepseek），"
                             "用来截「思考模式 / reasoning_effort」那一行；"
                             "结果写进 data-model-provider / "
                             "data-thinking-row-hidden / data-thinking-hint-hidden")
    parser.add_argument("--theme", default="",
                        choices=["", "glass", "midnight", "paper", "terminal", "contrast"],
                        help="强制渲染成某一套主题；默认不传，即产品默认主题（液态玻璃）")
    parser.add_argument("--accent", default="",
                        choices=["", "theme", "blue", "indigo", "cyan", "purple", "magenta"],
                        help="强制渲染成某一档重点色；默认不传，即「跟随主题」")
    parser.add_argument("--frost", default="",
                        choices=["", "theme", "off", "soft", "mid", "strong"],
                        help="强制渲染成某一档磨砂；默认不传，即「跟随主题」")
    parser.add_argument("--report-job", type=int, default=0,
                        help="报告页：目录画出来之后真点第 N 条**任务**行（1 起），"
                             "截那一篇任务报告。0 = 停在「本目标汇总」。"
                             "点击结果（正文卡头那行字）写进 data-report-job，"
                             "可用 --dump-dom 断言。")
    parser.add_argument("--png", action="store_true",
                        help="渲染后用无头 Edge/Chrome 截一张图，存到与 out 同名的 .png"
                             "（本机没有浏览器时只打印 SKIP，不算失败）")
    args = parser.parse_args()

    data = collect(args.intrusive, args.intrusive_targets,
                   args.authz_runtime, args.authz_targets, args.real_plugins)
    if args.fake_skills:
        payload = data.get("/api/v1/skills")
        if isinstance(payload, dict):
            payload.update(fake_skills())
        else:
            data["/api/v1/skills"] = fake_skills()
    # `--png` 是个开关，PNG 的路径由 out 推导。把 out 写成 `.png` 会让浏览器
    # 拿一个 png 当页面渲染，结果是**一张纯黑图**外加一个加载失败的图标——
    # 看起来像"页面崩了"，实际只是调用方式错了。这里当场改掉而不是记在文档里。
    if args.png and args.out.lower().endswith(".png"):
        fixed = os.path.splitext(args.out)[0] + ".html"
        print("提示：out 应为 .html（PNG 路径由它推导）；已改用 %s" % fixed)
        args.out = fixed
    path = build(args.out, data, args.tab, args.kind, args.focus, args.job_mode,
                 args.group, args.hub_query, args.stream, args.stream_stop,
                 args.apply_agent, args.preset_form, args.expand_agent, args.subagent,
                 args.model_provider, args.dock or ("tools" if args.tools else ""),
                 args.theme, args.accent, args.frost, args.report_job)
    jobs = data.get("/api/v1/jobs", {}).get("jobs", [])
    print("已生成 %s" % path)
    print("  页签 %s ｜ 任务类型 %s ｜ 任务 %d 个 ｜ 节点 %d 个 ｜ 流式帧 %d 帧"
          % (args.tab, args.kind, len(jobs),
             len(data.get("/api/v1/executors", {}).get("executors", [])),
             len(data.get("__stream__") or [])))
    if args.subagent:
        print("  子智能体事件 %d 帧（3 个：1 已完成 / 1 跑动中 / 1 失败），"
              "回放停在 done 之前" % len(SUBAGENT_SCRIPT))
    if args.png:
        shot = screenshot(path, os.path.splitext(os.path.abspath(path))[0] + ".png")
        if shot:
            print("  截图 %s" % shot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
