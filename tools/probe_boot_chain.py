# -*- coding: utf-8 -*-
"""启动链探针 —— 用真实页面（含 auth.js + boot.js）跑一遍 Console 的启动。

**为什么不能只靠 `tools/render_ui_fixture.py`。** 夹具**刻意丢掉了 auth.js 与
boot.js**（它不需要登录门），于是 app.js 走的是它自己那条自启动分支
（`if (!window.PentestConsoleGate) ... init`）。真实页面走的是另一条：
`boot.js enterConsole()` —— 那一段整个跑在 auth.js 监听器的 try/catch 里，**任何
一步抛出，它后面的步骤一行都不执行**。

2026-10-15 就在那条路上出过一次：`app.js bindEvents()` 里有一行
`$('#job-log').addEventListener(...)`，而 `#job-log` 是渲染时才造出来的节点，
初始化那一刻它是 null —— 于是 `init()` 在中途断掉，`loadStatus` / `loadGraph` /
`loadEngagements` 全都没执行。表现出来是"打开页面提示界面初始化失败，而且顶栏、
漏洞、资产全空"，而错误条上那句话在讲日志面板。**夹具看不见这条路径**，因为它在
夹具里根本不经过；它只回报了同一条 TypeError，而没有人读那份记录。

这个探针做三件事：

  1. 读 `web/index.html` 原样注入 `<base>`（让真实 `<script src>` 解析到 `web/` 下）
     与一个 fetch 桩（只回放合成数据，不出网），加进异常记录器与阶段标记；
  2. 用无头浏览器打开它、dump DOM；
  3. 读出 `<html data-probe-*>` 并按 JSON 汇报。

**桩必须跟着接口契约走。** 桩的形状不对时，报出来的是一个不存在的产品缺陷，
与真问题长得一模一样。所以这里只给**最小但合法**的载荷（`{"findings": []}` 这类），
并且断言的重点放在"启动链有没有跑完、有没有异常"上，而不是"渲染出来的东西长什么样"
——后者是夹具的活（它回放的是真实抓下来的响应）。

用法：
  python tools/probe_boot_chain.py                 # 人看的输出，有异常时退出码 1
  python tools/probe_boot_chain.py --json          # 只打 JSON（给测试用）
  python tools/probe_boot_chain.py --keep DIR      # 保留生成的 HTML 与 dump（排障用）
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB = os.path.join(ROOT, "web")

# 审查卡的正文由**真实**的 `harness.classify` + `approval.public` 生成（第五场）：
# 手抄一份会随着那两个模块改措辞而悄悄过期，而这一场要验的又不是某段文字。
sys.path.insert(0, ROOT)
from pentestd import approval, harness                            # noqa: E402

# 候选表与会话一样只有一份；`_browser` 这个名字被 probe_wordmark 等脚本 import。
from _browser import BROWSERS, find as _browser    # noqa: E402

# 发现 / 漏洞 / 资产 三类数据的最小合法载荷。字段名跟着渲染器读的那几个键走
# （`data.findings` / `data.assets`），够让那条加载路径真的被执行。
ENGAGEMENT_ID = "E-probe"
STUB = {
    "/api/v1/auth/state": {"authenticated": True, "csrfToken": "probe-token",
                           "user": {"id": "u1", "username": "probe", "role": "admin"},
                           "needsSetup": False, "passwordPolicy": "", "throttle": ""},
    "/api/v1/auth/me": {"authenticated": True, "csrfToken": "probe-token",
                        "user": {"id": "u1", "username": "probe", "role": "admin"}},
    "/api/v1/health": {"ok": True, "version": "probe", "pendingJobs": 0},
    "/api/v1/executors": {"executors": []},
    "/api/v1/engagements": {"engagements": [
        {"id": ENGAGEMENT_ID, "kind": "goal", "seq": 1, "target": "10.0.0.0/24",
         "objective": "探针", "authorization": "probe"}], "activeId": ENGAGEMENT_ID},
    "/api/v1/graph": {"nodes": [], "edges": [], "truncated": False, "totalNodes": 0},
    "/api/v1/jobs": {"jobs": []},
    "/api/v1/agents": {"agents": [], "activeId": "", "dsh": None},
    "/api/v1/models": {"models": [], "providerPresets": []},
    "/api/v1/policies": {"policies": []},
    "/api/v1/skills": {"skills": [], "roles": [], "total": 0},
    "/api/v1/users": {"users": []},
    "/api/v1/settings": {},
    "/api/v1/session-options": {"models": [], "agents": [], "policies": [], "defaults": {}},
    "/api/v1/chat/sessions": {"sessions": []},
    "/api/v1/chat/tools": {"tools": []},
    # 心跳。**它必须登记**：不登记录的是 `{}`，而 `pumpPulse` 拿不到 `stamps` 会安安静静
    # 地早退——于是"心跳根本没起"与"心跳起来了但没变化"在探针里长得一模一样。
    "/api/v1/pulse": {"stamps": {"engagements": "n=1 m=1", "nodes": "n=0 m=0",
                                 "sessions": "n=0 u=0"}},
    "/api/v1/hub/catalog": {"items": [], "total": 0, "categories": []},
    "/api/v1/hub/ops": {"ops": []},
    "/api/v1/dsh": {"plugins": [], "ignored": [], "available": [], "errors": []},
}

# 目标下的资源按后缀兜底（id 是变量，没法逐条列全）。
SUFFIX_STUB = {
    "/findings": {"findings": []},
    "/assets": {"assets": []},
    "/report": {"markdown": "", "generatedAt": 0, "jobId": ""},
}

PROBE_SCRIPT = r"""<script>
// 无头浏览器没有控制台可看：某处一抛，页面只会"少一块"，而少的那一块恰恰是最需要
// 看见的。把异常、未处理的拒绝、以及**每一个请求过的接口**都记到 <html> 的属性上，
// `--dump-dom` 就能直接读出来。
window.__PROBE__ = { errors: [], paths: [], stage: 'parsing', streamAborted: false,
                     streamOpened: false };
function __rec__(kind, text) {
  window.__PROBE__.errors.push(kind + ': ' + text);
  document.documentElement.setAttribute('data-probe-errors', window.__PROBE__.errors.join(' | '));
}
function __mark__(stage) {
  window.__PROBE__.stage = stage;
  document.documentElement.setAttribute('data-probe-stage', stage);
}
window.addEventListener('error', function (event) {
  __rec__('error', String((event.error && event.error.stack) || event.message));
});
window.addEventListener('unhandledrejection', function (event) {
  __rec__('rejection', String((event.reason && event.reason.stack) || event.reason));
});
var __STUB__ = __STUB_JSON__;
var __SUFFIX__ = __SUFFIX_JSON__;
var __MODE__ = __MODE_JSON__;
var __LATE_GOALS__ = __LATE_GOALS_JSON__;
var __LATE_SESSIONS__ = __LATE_SESSIONS_JSON__;
var __REVIEW_PENDING__ = __REVIEW_PENDING_JSON__;
var __REVIEW_DONE__ = __REVIEW_DONE_JSON__;
var __REVIEW_SESSION__ = __REVIEW_SESSION_JSON__;
var __reviewDecided__ = false;      // 第五场：裁决过之后 `/reviews?status=pending` 不再回它
// 「目标与对话是**之后**才出现的」这一场：判据是探询次数，于是整条时间线在虚拟时钟里
// 是确定的（第 3 次问起才算出现），不依赖真实时间。
var __pulseHits__ = 0;
var __engagementsAsked__ = 0;
var __sessionsAsked__ = 0;
function __lateReady__() { return __pulseHits__ >= 3; }
function __payload__(path) {
  if (__MODE__ === 'button-stack') {
    // 这一场要按得动那颗按钮，而 `canSend()` 要求"有模型可选"：桩里两个模型接口都是
    // 空的，于是那颗按钮在空闲态本来就是禁用的——点下去什么都不会发生，探针会得出
    // "点击没分派"这个**错误**结论。所以这里补一条模型。
    if (path === '/api/v1/session-options') {
      return { models: [{ id: 'm-probe', name: '探针模型', model: 'probe-flash' }],
               agents: [], policies: [], defaults: {} };
    }
  }
  if (__MODE__ === 'review-band') {
    // 这一场要的是"输入框上方那一格什么时候该空"：开一条会话（客户端自己去
    // `openSession` → `loadReviews`），那一格就挂上一张**待批**的单子。
    //
    // ⚠️ **桩要跟着裁决走**：`GET /reviews?status=pending` 在裁决之后不能再回那
    // 一条 —— 真实服务端不会。桩不跟着变时的症状（第一次跑就撞上了）：卡片被
    // 请走之后，下一次轮询又把**待批**那条捞回来，于是"那一格空掉了"永远不成立，
    // 报出来的是一条**不存在**的产品缺陷。假后端内部一致是它自己的职责。
    if (path === '/api/v1/pulse') {
      return { stamps: { engagements: 'n=1 m=1', nodes: 'n=0 m=0',
                         sessions: 'n=1 u=1792082864' } };
    }
    // 这一场要**让这一轮先跑起来**（`send()` 同步置上 `view.live`）：使用者的那一次
    // 正是"这一轮在跑、卡片挂着"，而只有在那种时刻，"卡片走了"才只可能是我这一步做的
    // （没有活流时，记录区那次重取会顺手清掉这一格，这一场就失去判别力）。
    // 所以补一条模型（`canSend()` 要求"有模型可选"），并把流交给下面那个永不结束的桩。
    if (path === '/api/v1/session-options') {
      return { models: [{ id: 'm-probe', name: '探针模型', model: 'probe-flash' }],
               agents: [], policies: [], defaults: {} };
    }
    if (path === '/api/v1/chat/sessions') {
      return { sessions: [{ id: __REVIEW_SESSION__, title: '探针会话',
                            engagementId: 'E-probe', updatedAt: 1792082864 }] };
    }
    if (path === '/api/v1/reviews') {
      return { reviews: __reviewDecided__ ? [] : [__REVIEW_PENDING__] };
    }
    // 点「批准并执行」之后客户端把裁决 POST 上来，服务端回**这一份**（已裁决的行）。
    if (path.indexOf('/api/v1/reviews/') === 0) {
      __reviewDecided__ = true;
      return { review: __REVIEW_DONE__ };
    }
  }
  if (__MODE__ === 'late-goal') {
    if (path === '/api/v1/pulse') {
      __pulseHits__++;
      return { stamps: {
        engagements: __lateReady__() ? 'n=1 m=1' : 'n=0 m=0',
        nodes: __lateReady__() ? 'n=128 m=261' : 'n=0 m=0',
        sessions: __lateReady__() ? 'n=1 u=1792082864.244' : 'n=0 u=0',
      } };
    }
    if (path === '/api/v1/engagements') {
      return __lateReady__() ? { engagements: __LATE_GOALS__ } : { engagements: [] };
    }
    if (path === '/api/v1/chat/sessions') {
      return __lateReady__() ? { sessions: __LATE_SESSIONS__ } : { sessions: [] };
    }
  }
  if (__MODE__ === 'stale-list') {
    // 启动那份列表是**旧的**：它取到的是"目标还不存在"的那一刻，而之后的每一次探询
    // 服务端都已经有了目标。页面手上那份与探询基线就此错开——这正是使用者实际遇到
    // 的那条时间线（打开页面时目标列表是空的，第一次提问在几秒后建出了目标，而他在
    // 那之后才第一次切页签，心跳于是拿"服务端当时的值"当成了基线）。
    if (path === '/api/v1/pulse') {
      __pulseHits__++;
      return { stamps: { engagements: 'n=1 m=1', nodes: 'n=128 m=261',
                         sessions: 'n=1 u=1792082864.244' } };
    }
    if (path === '/api/v1/engagements') {
      __engagementsAsked__++;
      return __engagementsAsked__ >= 2 ? { engagements: __LATE_GOALS__ } : { engagements: [] };
    }
    if (path === '/api/v1/chat/sessions') {
      __sessionsAsked__++;
      return __sessionsAsked__ >= 2 ? { sessions: __LATE_SESSIONS__ } : { sessions: [] };
    }
  }
  if (Object.prototype.hasOwnProperty.call(__STUB__, path)) return __STUB__[path];
  var keys = Object.keys(__SUFFIX__);
  for (var i = 0; i < keys.length; i++) {
    if (path.slice(-keys[i].length) === keys[i]) return __SUFFIX__[keys[i]];
  }
  // 没登记的路径给一个合法的空对象：`apiGet` 只要求"是合法 JSON"，拿到 {} 之后
  // 各渲染器走的是自己的空态分支。
  return {};
}
window.fetch = function (input, init) {
  var url = String(input), at = url.indexOf('/api/v1');
  var raw = (at >= 0 ? url.slice(at) : url), path = raw.split('?')[0];
  window.__PROBE__.paths.push(String((init && init.method) || 'GET') + ' ' + raw);
  // 分隔符不能用空格：每条记录自己就含一个空格（`GET /api/v1/x`），按空格切会把
  // 一条拆成两段，于是"这个接口取过没有"全部判成没取过。
  document.documentElement.setAttribute('data-probe-paths', window.__PROBE__.paths.join(' || '));
  // 按钮那一场：这一轮的流**永不结束**，于是 `view.sending` 一直是真、那颗按钮停在
  // 「停止」那一态——这正是它要验的那一刻。`signal` 上的 abort 监听器用来证明第二下
  // 点击**真的**走到了中止（而不是把按钮画成了停止、点下去什么都不做）。
  if ((__MODE__ === 'button-stack' || __MODE__ === 'review-band')
      && path === '/api/v1/chat/stream') {
    __PROBE__.streamOpened = true;
    if (init && init.signal) {
      init.signal.addEventListener('abort', function () {
        window.__PROBE__.streamAborted = true;
      });
    }
    return Promise.resolve(new Response(new ReadableStream({ start: function () {} }),
                                        { status: 200 }));
  }
  return Promise.resolve(new Response(JSON.stringify(__payload__(path)), {
    status: 200, headers: { 'Content-Type': 'application/json' } }));
};
// 启动链跑完之后**从界面上**点一遍漏洞与资产：这两个页签的数据是使用者报的那句
// "无法查看"的正主，只断言"启动没抛"还不够——它还得真的去取那份数据。
//
// **只在第一场里点。** 切页签会起心跳（`loadTabData` 就是它的一个入口），而另外两场
// 恰恰要验"**没人动过的页面**上侧边栏会不会自己跟上"——点了就把要验的那条入口
// 自己补上了，缺陷会被这一下遮住（实测：加上这两次点击之后，`state.tab` 写错、
// 启动根本不放心跳的那个缺陷在这一场里是**绿的**）。
document.addEventListener('DOMContentLoaded', function () {
  if (__MODE__ === 'default') {
    setTimeout(function () {
      __mark__('tabs');
      ['findings', 'assets'].forEach(function (name) {
        var tab = document.querySelector('[data-tab="' + name + '"]');
        if (tab) tab.click();
      });
      setTimeout(function () { __mark__('settled'); }, 400);
    }, 800);
  }
  // 第四场：**一颗按钮两种身份**（发送 ↔ 停止）。这一场不看任何渲染结果，
  // 只看真实点击之后那颗按钮**是什么**：空闲时叫发送、跑着时叫停止且**仍可点**，
  // 而第二下点击真的把请求中止掉了。
  //
  // 为什么要跑真浏览器而不是只读源码：这一改动的三处风险（忘了禁用/误禁用、
  // 点击没分派、名字没跟着状态走）**没有一处会让任何单元测试变红**，而界面上
  // 表现成"按钮在、点不动"或"点了没反应"——与"我没点到"长得一模一样。
  if (__MODE__ === 'button-stack') {
    setTimeout(function () {
      var btn = document.getElementById('chat-send');
      var input = document.getElementById('chat-input');
      function snap() {
        if (!btn) return 'no-button';
        // t80：图标换成了内联 SVG（与 DSH 桌面端同一颗），`textContent` 从此恒为空
        // ——图标在不在要问 DOM，不能读文本。
        var icon = btn.querySelector('svg') ? 'svg' : (btn.textContent || '').trim();
        return [icon,
                btn.getAttribute('aria-label') || '',
                btn.disabled ? 'disabled' : 'enabled',
                btn.classList.contains('is-stop') ? 'stop-style' : 'send-style'].join('|');
      }
      document.documentElement.setAttribute('data-probe-stop-exists',
        document.getElementById('chat-stop') ? 'yes' : 'no');
      document.documentElement.setAttribute('data-probe-send-idle', snap());
      __mark__('idle');
      if (input) input.value = '探针：这一轮不会真的发出去';
      if (btn) btn.click();
      // `send()` 里 setSending(true) 是**同步**执行的（fetch 之前），所以点完立刻读
      // 就是忙碌态——不需要等定时器，也就不会在虚拟时钟里读到中间态。
      document.documentElement.setAttribute('data-probe-send-busy', snap());
      __mark__('busy');
      if (btn) btn.click();                       // 第二下 = 停止
      document.documentElement.setAttribute('data-probe-send-abort',
        window.__PROBE__.streamAborted ? 'yes' : 'no');
      __mark__('stopped');
    }, 500);
  }
  // 第五场：**那张卡片的去留**（m18801 起卡片画在输入卡片上沿的浮层里）。
  //
  // 契约还是那三条，只是"格"换成了 `#chat-review-float`：待批的卡片在，裁决过的
  // 卡片停留一下就走（`chat.js` 的 `scheduleReviewLeave`），走掉之后那个浮层
  // 必须**收起**（`hidden`，`renderReviews()` 写的）——节点留着但 `hidden` 也
  // 不行的那一版会把它自己的一道底边留在输入框上方。
  //
  // 三个快照分别对应三件不同的事实：
  //   before —— 待批时浮层可见、写着「需要你确认这条命令」；
  //   after  —— 点完之后**卡片还在**（"我点过了"必须看得见，立刻收掉等于点了没反应）；
  //   later  —— 停留时间过了之后浮层必须**收起且一张卡不剩**。
  //
  // 读的是**浮层自己**，不是记录区：m18801 之后记录区里根本不会有审查卡，要是还读
  // `#chat-log`，这一场在"卡片压根没画出来"时也会是绿的（空记录区 = "一张不剩"）。
  //
  // ⚠️ **`later` 那个时刻必须落在一个窗口里**：晚于 `DONE_LINGER_MS + DONE_FADE_MS`
  // （卡片该走了），早于 `refreshTranscriptSoon` 最后一次重开（9000ms；那一次会
  // 重取记录、顺手把卡片清掉 —— 于是"卡片自己走了"与"被别的原因清掉了"再也分不开，
  // 这一场就没有判别力）。现在：点击在 1500、卡片该走 8100、那次重开 10500 → 快照在 **9500**。
  // 这两个数由 `chat.js` 的常量与 `test_ui_contract` 的一条契约共同保证。
  if (__MODE__ === 'review-band') {
    setTimeout(function () {
      // 卡片在**浮层**里：`.chat-field` 上沿那一层，脱出记录区的文档流。
      var layer = document.getElementById('chat-review-float');
      function snap(name) {
        var shown = !!layer && layer.getClientRects().length > 0;
        var cards = shown ? layer.querySelectorAll('.chat-review-card').length : 0;
        var text = shown ? layer.textContent : '';
        // `band` / `no-band` 这套词留在快照里不变（报告与单元测试都读它），
        // 含义是"浮层还看得见" / "浮层收起了"，**看得见但没有卡**也算留下了一圈
        // 空边——那正是 `hidden` 没写对时的形态。
        document.documentElement.setAttribute('data-probe-' + name, [
          shown ? 'band' : 'no-band', cards,
          text.indexOf('已批准') >= 0 ? 'done'
            : (text.indexOf('需要你确认这条命令') >= 0 ? 'pending' : 'none')
        ].join('|'));
      }
      var btn = layer ? layer.querySelector('.chat-review-actions button') : null;
      document.documentElement.setAttribute('data-probe-review-has-button',
        btn ? 'yes' : 'no');
      snap('review-before');
      // **先让这一轮跑起来**（`send()` 是同步置 `view.live` 的）：这一场的判别力
      // 全押在"点击那一刻这一轮在跑"上 —— 没有活流时裁决后的记录区重取（1.2s 那次）
      // 会顺手清掉卡片，于是"卡片自己走了"与"被别的原因清掉了"分不开。
      var input = document.getElementById('chat-input');
      var sendBtn = document.getElementById('chat-send');
      if (input) input.value = '探针：这一轮不会结束';
      if (sendBtn) sendBtn.click();
      document.documentElement.setAttribute('data-probe-review-live',
        window.__PROBE__.streamOpened ? 'yes' : 'no');
      setTimeout(function () {
        if (btn) btn.click();
        // `after` 取 **900ms**（> 淡出的 600ms）：它要回答的是"点完那一下到底看没看见
        // 结果"。取 300ms 的话，就算把停留时间调成 0，卡片**也还在**（淡出还没走完），
        // 于是"走得太急"这个回退抓不到（实测：那一版全绿）。
        setTimeout(function () { snap('review-after'); }, 900);
        setTimeout(function () { snap('review-later'); }, 8000);
      }, 300);
    }, 1500);
  }
  // 侧边栏那两个块**自己有没有跟上**：它们只在心跳判出"变了"之后才重画，所以这里
  // 等到心跳跑过几轮再读。读的是正文里的痕迹（目标行带着它的 id、对话行带着标题），
  // 而不是"函数被调用过"——后者在真实缺陷下照样为真。
  setTimeout(function () {
    var goals = document.getElementById('chat-goals');
    var sessions = document.getElementById('chat-sessions');
    document.documentElement.setAttribute('data-probe-goal-row',
      goals && goals.innerHTML.indexOf(__LATE_GOALS__[0].id) >= 0 ? 'yes' : 'no');
    document.documentElement.setAttribute('data-probe-session-row',
      sessions && sessions.innerHTML.indexOf(__LATE_SESSIONS__[0].title) >= 0 ? 'yes' : 'no');
    __mark__('sidebar');
  }, 7500);
});
</script>
"""

# 「目标与对话是之后才出现的」用的那一份数据。**这一场是使用者报的那一幕**：
# 目标列表在启动时是空的，于是 `loadEngagements()` 走 `renderNoEngagement()` 提前
# 返回、`selectEngagement()` 一次都不跑——而心跳原先只挂在 `selectEngagement` 与
# 切页签那两条路上，于是整个页面再没有任何东西会把侧边栏拉新。
LATE_GOAL = {"id": "E-late", "kind": "goal", "seq": 1, "target": "10.0.0.7",
             "objective": "迟到出现的目标", "authorization": "probe"}
LATE_SESSION = {"id": "C-late", "title": "迟到出现的对话",
                "engagementId": "E-late", "updatedAt": 1792082864.244}

# 第五场（审查卡的去留）用的那条命令。**要长、要在长 token 串里有空格** ——
# 使用者报的那张卡片上正是一条 PowerShell 一行流，它的换行行为就是被验的一部分。
REVIEW_SESSION = "C-review"
REVIEW_COMMAND = (
    "$ProgressPreference='SilentlyContinue'; foreach($p in "
    "@('/api/status','/api/system/status','/api/config','/api/graph/latest',"
    "'/api/report/status','/api/forum/log')){try{$r=Invoke-WebRequest -Uri "
    "('http://182.117.165.148:5000'+$p) -TimeoutSec 20 -UseBasicParsing; "
    "$b=$r.Content; $len=[Math]::Min(300,$b.Length); Write-Output ($p + \" -> HTTP \" "
    "+ [int]$r.StatusCode) } catch { $resp=$_.Exception.Response }}"
)


def review_rows():
    """两条审查单：待批的、已裁决的。

    形状取自 `approval.public`（界面读的就是它），正文取自 `harness.classify`。
    `awaiting` 由这里给定：待批那条**有人在等它**（`awaiting=True` → 画按钮），
    已裁决那条不可能是待批态。
    """
    decision = harness.classify("run_command", {"command": REVIEW_COMMAND})
    base = {
        "id": "R-probe", "tool": "run_command", "tier": decision["tier"],
        "headline": decision["headline"], "reasons": decision["reasons"],
        "facts": decision["facts"], "expiresAt": time.time() + 300, "timeoutS": 300,
        "createdAt": time.time(), "sessionId": REVIEW_SESSION,
        "decidedBy": "探针", "note": "",
    }
    return (approval.public(dict(base, status="pending"), awaiting=True),
            approval.public(dict(base, status="approved"), awaiting=False))



def build_harness(mode: str = "default") -> str:
    with open(os.path.join(WEB, "index.html"), "r", encoding="utf-8") as handle:
        html = handle.read()
    pending, done = review_rows()
    script = (PROBE_SCRIPT
              .replace("__STUB_JSON__", json.dumps(STUB, ensure_ascii=False))
              .replace("__SUFFIX_JSON__", json.dumps(SUFFIX_STUB, ensure_ascii=False))
              .replace("__MODE_JSON__", json.dumps(mode))
              .replace("__LATE_GOALS_JSON__", json.dumps([LATE_GOAL], ensure_ascii=False))
              .replace("__LATE_SESSIONS_JSON__", json.dumps([LATE_SESSION], ensure_ascii=False))
              .replace("__REVIEW_PENDING_JSON__", json.dumps(pending, ensure_ascii=False))
              .replace("__REVIEW_DONE_JSON__", json.dumps(done, ensure_ascii=False))
              .replace("__REVIEW_SESSION_JSON__", json.dumps(REVIEW_SESSION)))
    # <base> 让真实的 <script src="app.js"> 解析到 web/ 下，即使探针 HTML 落在别处。
    html = html.replace('<meta charset="utf-8">',
                        '<meta charset="utf-8">\n<base href="%s/">' % _web_url(), 1)
    return html.replace('<script>', script + '<script>', 1)


def _web_url() -> str:
    return "file:///" + WEB.replace("\\", "/")


def dump_dom(browser: str, page: str, budget_ms: int) -> str:
    """无头浏览器把 DOM 打出来。

    **stdout 必须接到一个文件上，不能让父进程"捕获输出"。** msedge 在 Windows 上
    是 GUI 子系统程序，没有可写的主机句柄，直接捕获拿到的是**空串**而不是报错
    ——静默的空，排查起来像探针没跑。这里交给子进程一个真实的文件句柄，于是
    Windows / Linux 是同一条代码路径。
    """
    profile = tempfile.mkdtemp(prefix="boot-chain-profile-")
    handle, out = tempfile.mkstemp(prefix="boot-chain-dom-", suffix=".html")
    os.close(handle)
    try:
        args = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
                "--user-data-dir=" + profile,
                "--virtual-time-budget=%d" % int(budget_ms), "--dump-dom", page]
        with open(out, "w", encoding="utf-8", errors="replace") as sink:
            subprocess.run(args, stdout=sink, stderr=subprocess.DEVNULL,
                           timeout=180, check=False)
        with open(out, "r", encoding="utf-8", errors="replace") as source:
            return source.read()
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        if not os.environ.get("BOOT_CHAIN_KEEP"):
            try:
                os.remove(out)
            except OSError:
                pass


def _attr(dom: str, name: str) -> str:
    marker = name + '="'
    at = dom.find(marker)
    if at < 0:
        return ""
    end = dom.find('"', at + len(marker))
    return dom[at + len(marker):end] if end > 0 else ""


def _unescape(text: str) -> str:
    return (text.replace("&quot;", '"').replace("&gt;", ">")
                .replace("&lt;", "<").replace("&amp;", "&"))


_BAR = re.compile(r'id="error-text"[^>]*>([^<]*)<')

# 启动链跑完就一定会被取的三样东西。它们是"控制台起来了"的最小证据，而且正好
# 覆盖使用者看到的那一面：这三个不取，顶栏的执行节点、链路、漏洞/资产就都是空的。
# 用前缀匹配——`/api/v1/graph?nodes=200` 也算法命中。
#
# `/api/v1/pulse` 是第四样，加它的理由是一次真实的漏检：侧边栏那两个读数块
# （目标 / 对话记录）靠它才能自己变新，而它**曾经一条路都没接上**（心跳只挂在
# "切页签"与"选中目标"那两条路上，启动链一条都不走）——探针那次是绿的，因为它的桩
# 里恰好有一个目标，于是 `loadEngagements()` 会走到 `selectEngagement()`。把"取过没有"
# 钉在这里，比钉在"某个函数被调用过"上结实：请求发生了，才说明那条路真的通了。
CORE_PATHS = ("/api/v1/health", "/api/v1/graph", "/api/v1/engagements", "/api/v1/pulse")


def probe(budget_ms: int = 12000, keep: str = "", mode: str = "default") -> dict:
    browser = _browser()
    if not browser:
        return {"browser": "", "errors": [], "paths": [], "missing_core": [],
                "error_bar": "", "stage": "", "mode": mode,
                "goal_row": False, "session_row": False, "ok": True}
    work = keep or tempfile.mkdtemp(prefix="boot-chain-")
    page = os.path.join(work, "boot-chain-%s.html" % mode)
    with open(page, "w", encoding="utf-8") as handle:
        handle.write(build_harness(mode))
    dom = dump_dom(browser, "file:///" + page.replace("\\", "/"), budget_ms)
    errors = [e for e in _unescape(_attr(dom, "data-probe-errors")).split(" | ") if e]
    bar = _BAR.search(dom)
    paths = [item for item in _unescape(_attr(dom, "data-probe-paths")).split(" || ") if item]
    called = [raw for raw in paths if raw.startswith("GET ")]
    missing = [want for want in CORE_PATHS
               if not any(raw.startswith("GET " + want) for raw in called)]
    return {
        "browser": browser,
        "page": page,
        "mode": mode,
        "errors": errors,
        "paths": paths,
        "missing_core": missing,
        "stage": _attr(dom, "data-probe-stage"),
        "error_bar": _unescape(bar.group(1)).strip() if bar else "",
        # 侧边栏那两个块自己有没有跟上（只在 late-goal 那场里判定）
        "goal_row": _attr(dom, "data-probe-goal-row") == "yes",
        "session_row": _attr(dom, "data-probe-session-row") == "yes",
        # 动作按钮那一场：空闲态 / 忙碌态各一次快照，外加"第二下点击有没有真的中止"
        "send_idle": _unescape(_attr(dom, "data-probe-send-idle")),
        "send_busy": _unescape(_attr(dom, "data-probe-send-busy")),
        "send_abort": _attr(dom, "data-probe-send-abort"),
        "stop_exists": _attr(dom, "data-probe-stop-exists"),
        # 审查卡那一场：三个时刻各一次快照（`band|卡片数|待批还是已批`）
        "review_before": _attr(dom, "data-probe-review-before"),
        "review_after": _attr(dom, "data-probe-review-after"),
        "review_later": _attr(dom, "data-probe-review-later"),
        "review_has_button": _attr(dom, "data-probe-review-has-button"),
        "review_live": _attr(dom, "data-probe-review-live"),
        "dom_bytes": len(dom),
        "ok": not errors and not missing and not (bar and bar.group(1).strip()),
    }


def _sidebar_scenario(mode: str, budget_ms: int = 12000, keep: str = "") -> dict:
    """两场"没人动过的页面"共用的判据：目标行与对话行**自己**出现了没有。

    判据只有这两条，都是正文里的痕迹（目标行带着它的 id、对话行带着标题），而不是
    "某个函数被调用过"——后者在真实缺陷下照样为真。
    """
    report = probe(budget_ms, keep, mode=mode)
    if not report.get("browser"):
        return {"ok": True, "problems": [], "paths": [], "errors": [],
                "missing_core": [], "goal_row": False, "session_row": False}
    problems = []
    if report["missing_core"]:
        problems.append("这几个接口一次都没取过：%s" % report["missing_core"])
    if report["errors"]:
        problems.append("抛了异常：%s" % report["errors"])
    if not report["goal_row"]:
        problems.append("目标行始终没出现：侧边栏那句「还没有目标。下发一次任务会自动"
                        "建一个。」没有变成那个目标")
    if not report["session_row"]:
        problems.append("对话行始终没出现：侧边栏「对话记录」一直写着「未选择目标 · 0 条」"
                        "与「另有 1 条对话属于其他目标」")
    return {"ok": not problems, "problems": problems, "paths": report["paths"],
            "errors": report["errors"], "missing_core": report["missing_core"],
            "goal_row": report["goal_row"], "session_row": report["session_row"]}


def probe_late_goal(budget_ms: int = 12000, keep: str = "") -> dict:
    """第二场：**目标与对话都是启动之后才出现的**，而页面**没人动过**。

    第一场的桩里一直有一个目标，于是 `loadEngagements()` 会走到 `selectEngagement()`
    ——那条路恰好（也仅仅）是心跳的一个入口。这一场把目标列表从空的开始：启动那句
    `loadEngagements()` 走 `renderNoEngagement()` 提前返回、`selectEngagement()` 一次
    都不跑，于是"心跳到底起没起"才是真的被验到了（而它当时一次都没起）。
    """
    return _sidebar_scenario("late-goal", budget_ms, keep)


def probe_stale_list(budget_ms: int = 12000, keep: str = "") -> dict:
    """第三场：**启动那份列表是旧的，而第一次探询服务端就已经有新东西了。**

    与第二场的差别只有一处，但那一处正是使用者实际遇到的时间线：他在目标出现**之后**
    才第一次切页签，于是心跳拿"服务端当时的值"当成了基线，而页面手上那份还是启动时
    拉到的空列表——两边就此错开，"目标出现了"再也不会被判定成变化。

    所以这一场把 `/pulse` 从一开始就说"有 1 个目标"，而 `/engagements` 第一次仍然回
    空：这才是"页面手上的 vs 服务端的"真正分叉的那一刻。
    """
    return _sidebar_scenario("stale-list", budget_ms, keep)


def probe_button_stack(budget_ms: int = 12000, keep: str = "") -> dict:
    """第四场：**一颗按钮两种身份**（发送 ↔ 停止），全靠真实点击验。

    这一改动的三处风险没有一处会让任何单元测试变红，而它们在界面上分别表现成
    "按钮在、点不动""点了没反应""名字与做的事对不上"：

      · **忙碌时被一起禁用**（写回 `disabled = locked`）——刹车被锁上；
      · **点击没分派**——跑着的时候点它等于再发一次，而 `send()` 开头那句
        `if (view.sending) return` 会让它什么都不做；
      · **名字没跟着状态走**——只有图标的按钮，`aria-label` 是它唯一的名字。

    所以这一场读三样东西：空闲态与忙碌态各自的 `{图标|名字|能不能点|是不是停止配色}`，
    以及第二下点击**有没有真的把请求中止掉**（桩在 `signal` 上挂了监听器——
    "把按钮画成停止"和"点下去真的停"是两件事）。
    """
    report = probe(budget_ms, keep, mode="button-stack")
    fields = ("idle", "busy", "abort", "stop_exists", "errors", "paths")
    if not report.get("browser"):
        return dict({"ok": True, "skipped": True, "problems": []},
                    **{key: "" for key in fields})

    def parts(raw):
        return ((raw or "").split("|") + [""] * 4)[:4]

    idle_icon, idle_name, idle_state, idle_style = parts(report["send_idle"])
    busy_icon, busy_name, busy_state, busy_style = parts(report["send_busy"])
    problems = []
    if report["errors"]:
        problems.append("抛了异常：%s" % report["errors"])
    if report["stop_exists"] == "yes":
        problems.append("DOM 里还有第二颗动作按钮（`#chat-stop`）：发送与停止应当是同一颗")
    if not idle_icon:
        problems.append("空闲态的按钮上没有图标（SVG 或文本都没有）：快照 %r"
                        % report["send_idle"])
    if idle_name != "发送":
        problems.append("空闲态不叫「发送」：名字 %r（快照 %r）"
                        % (idle_name, report["send_idle"]))
    if idle_state != "enabled":
        problems.append("空闲态这颗按钮是**禁用**的：桩里给了一条模型，它就该能点"
                        "（快照 %r）" % report["send_idle"])
    if not busy_icon:
        problems.append("跑着的时候按钮上没有图标：快照 %r" % report["send_busy"])
    if busy_name != "停止":
        problems.append("跑着的时候它没有换成「停止」：名字 %r（快照 %r）"
                        % (busy_name, report["send_busy"]))
    if busy_state != "enabled":
        problems.append("跑着的时候这颗按钮被禁用了——它是这一轮**唯一**的刹车"
                        "（快照 %r）" % report["send_busy"])
    if busy_style != "stop-style":
        problems.append("忙碌态没有换成停止的配色（`.is-stop` 没挂上）：快照 %r"
                        % report["send_busy"])
    if report["send_abort"] != "yes":
        problems.append("第二下点击**没有**中止请求：按钮长成了停止、点下去却没停")
    return {"ok": not problems, "problems": problems, "idle": report["send_idle"],
            "busy": report["send_busy"], "abort": report["send_abort"],
            "stop_exists": report["stop_exists"], "errors": report["errors"],
            "paths": report["paths"]}


def probe_review_band(budget_ms: int = 14000, keep: str = "") -> dict:
    """第五场：**那张审查卡的去留**（真实点击，真实 `chat.js`）。

    这一改的风险与第四场同类 —— 没有任何单元测试会因为它变红：
      · 裁决过的卡片**不走** → 输入框上方常驻一条"已批准"，占着位置却无可做之事
        （使用者报的就是这个："批准执行显示的位置有问题"）；
      · 裁决过的卡片**走得太急** → 点完那一下看不见结果，与"点了没反应"没有区别；
      · 走掉之后那一格**不收** → 留下一个带边距的空盒子，读起来像"这里有东西没显示"。

    所以量的是三个时刻：点之前 / 点完 300ms / 8000ms 之后。

    ⚠️ **这一场必须先让"这一轮"跑起来**（点击发送，桩给一条永不结束的流）。理由是
    判别力：不在跑的时候，裁决之后 `refreshTranscriptSoon` 那次记录区重取（1.2s）
    会把那一格顺手清掉 —— 于是"卡片自己走了"与"被别的原因清掉了"在时间上分不开，
    这一场就永远是绿的（**第一次写它时正是这样：三处回退全绿**）。在跑的时候
    `refreshTranscriptSoon` 不走（`view.live` 为真），能清掉卡片的只剩 `chat.js`
    那一个计时器本身。使用者的那一次也正是在跑的时候。

    都在虚拟时钟预算里 —— 这一场不花真实时间。
    """
    report = probe(budget_ms, keep, mode="review-band")
    fields = ("before", "after", "later", "has_button", "live", "errors")
    if not report.get("browser"):
        return dict({"ok": True, "skipped": True, "problems": []},
                    **{key: "" for key in fields})

    def parts(raw):
        bits = (raw or "").split("|")
        return (bits + ["", "", ""])[:3]

    before_band, before_cards, before_kind = parts(report["review_before"])
    after_band, after_cards, after_kind = parts(report["review_after"])
    later_band, later_cards, later_kind = parts(report["review_later"])
    problems = []
    if report["errors"]:
        problems.append("抛了异常：%s" % report["errors"])
    if report["review_live"] != "yes":
        problems.append("这一轮**没有**真的跑起来（桩没收到 `/chat/stream`）：没有活流时"
                        "记录区那次重取会把卡片清掉，这一场就分不出'卡片自己走了'，"
                        "报告出来的结论会是一条**不存在**的缺陷")
    if report["review_has_button"] != "yes":
        problems.append("待批的那张卡片上没有动作按钮——这一场就点不动了")
    if (before_band, before_kind) != ("band", "pending"):
        problems.append("点之前浮层里应当有那张待批的卡：快照 %r" % report["review_before"])
    if (after_band, after_kind) != ("band", "done") or after_cards != "1":
        problems.append("点完之后那张卡片必须**还在**（写着已批准）：「我点过了」要看"
                        "得见，立刻收掉等于点了没反应。快照 %r" % report["review_after"])
    if (later_band, later_cards) != ("no-band", "0") or later_kind != "none":
        problems.append("停留时间过了之后浮层必须**收起且一张卡都不剩**：它挂在输入卡片"
                        "上沿，一张写着已批准的卡片不要人做任何事，留着就是让输入框"
                        "一直往上退一截。快照 %r" % report["review_later"])
    return {"ok": not problems, "problems": problems,
            "before": report["review_before"], "after": report["review_after"],
            "later": report["review_later"],
            "has_button": report["review_has_button"], "live": report["review_live"],
            "errors": report["errors"], "paths": report["paths"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="用真实启动链（auth + boot + app）跑一遍控制台启动")
    parser.add_argument("--json", action="store_true", help="只打 JSON")
    parser.add_argument("--budget", type=int, default=12000, help="虚拟时间预算（毫秒）")
    parser.add_argument("--keep", default="", help="把生成的 HTML 与 dump 留在这个目录里")
    args = parser.parse_args()
    if args.keep:
        os.environ["BOOT_CHAIN_KEEP"] = "1"
    report = probe(args.budget, args.keep)
    report["late_goal"] = probe_late_goal(args.budget, args.keep)
    report["stale_list"] = probe_stale_list(args.budget, args.keep)
    report["button"] = probe_button_stack(args.budget, args.keep)
    report["review"] = probe_review_band(args.budget, args.keep)
    report["ok"] = bool(report["ok"] and report["late_goal"]["ok"]
                        and report["stale_list"]["ok"] and report["button"]["ok"]
                        and report["review"]["ok"])
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    elif not report["browser"]:
        print("SKIP：本机没找到 Edge / Chrome，跳过启动链探针。")
    else:
        print("浏览器      %s" % report["browser"])
        print("阶段        %s   （dom %d 字节）" % (report["stage"], report["dom_bytes"]))
        print("请求        %s" % (" ".join(report["paths"]) or "（一个都没有）"))
        if report["missing_core"]:
            print("没取到的    %s" % " ".join(report["missing_core"]))
        if report["error_bar"]:
            print("错误条      %s" % report["error_bar"])
        if report["errors"]:
            print("异常 %d 条：" % len(report["errors"]))
            for item in report["errors"]:
                print("  * %s" % item)
        else:
            print("异常        无")
        print("结论        %s" % ("通过" if report["ok"] else "没通过"))
        for label, key in (("第二场：目标与对话**启动之后才出现**，页面没人动过", "late_goal"),
                           ("第三场：启动那份列表是旧的，而第一次探询服务端已经有目标", "stale_list")):
            one = report[key]
            print()
            print(label)
            print("  侧边栏自己跟上了吗  目标行 %s ｜ 对话行 %s"
                  % ("有" if one["goal_row"] else "没有",
                     "有" if one["session_row"] else "没有"))
            print("  请求        %s" % (" ".join(one["paths"]) or "（一个都没有）"))
            if one["problems"]:
                print("  没通过：")
                for item in one["problems"]:
                    print("    - %s" % item)
            else:
                print("  结论        通过")
        one = report["button"]
        print()
        print("第四场：一颗按钮两种身份（发送 / 停止），真实点击")
        print("  空闲态          %s" % (one["idle"] or "（没读到）"))
        print("  跑起来之后      %s" % (one["busy"] or "（没读到）"))
        print("  第二下真的停了吗 %s" % ("停了" if one["abort"] == "yes" else "没停"))
        print("  DOM 里还有 #chat-stop 吗 %s" % (one["stop_exists"] or "（没读到）"))
        if one["problems"]:
            print("  没通过：")
            for item in one["problems"]:
                print("    - %s" % item)
        else:
            print("  结论        通过")
        one = report["review"]
        print()
        print("第五场：审查卡的去留（那一格只摆「要人动手」的东西）")
        print("  点之前          %s" % (one["before"] or "（没读到）"))
        print("  点完 900ms      %s" % (one["after"] or "（没读到）"))
        print("  8000ms 之后     %s" % (one["later"] or "（没读到）"))
        print("  这一轮真跑起来了吗 %s" % ("跑了" if one["live"] == "yes" else "没有"))
        if one["problems"]:
            print("  没通过：")
            for item in one["problems"]:
                print("    - %s" % item)
        else:
            print("  结论        通过")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
