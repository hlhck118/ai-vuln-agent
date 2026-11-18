'use strict';
/* ============================================================================
 * 内部分栏拖拽 —— 四条分栏线的宽度交给使用者
 *
 * 控制台里有四处"一栏 + 一条 hairline + 另一栏"的结构：
 *   对话页  左栏（对话记录 / 目标） ｜ 消息流 ｜ 右栏（任务 / 子智能体，点一行就地展开）
 *   链路页  图画布 ｜ 节点详情
 *   资产页  资产树 ｜ 资产详情
 *   报告页  任务报告目录 ｜ 报告正文
 * 这四个宽度原先都是写死的（296 / 360 / 480 px，报告页那一页没有分栏）。写死的问题
 * 不是"不好看"，而是**没有一个正确的取值**：目标地址可以很长、节点详情可能是一整段
 * 原始 JSON、资产树可能只剩三层、任务报告目录要放下状态徽章加目标末段——同一块地方
 * 在不同数据下需要不同的宽度，而写死的那个数字只能照顾其中一种，剩下的靠横向滚动
 * 或换行硬扛。
 *
 * 宽度落在 CSS 自定义属性上（--split-chat / --split-graph / --split-assets /
 * --split-report），
 * **不写 inline-style**。区别在窄屏：媒体查询只要不引用这个变量，窄屏布局就
 * 整体忽略它；而 inline-style 的优先级高于任何媒体查询——那样窄屏下会把
 * 上下文栏顶成一个 296px 高的横条，恰好压掉它本该让给消息流的高度。
 *
 * 刻意不做的两件事：
 *   · **不做吸附、不做折叠**。两者都需要一套"折叠前宽度"的状态，而那条状态在
 *     刷新之后无法与用户当下的意图对齐；双击复位已经够用，且结果是可预期的。
 *   · **不用 dblclick 事件做复位**。pointerdown 上的 preventDefault 会吃掉
 *     鼠标兼容事件，dblclick 在部分浏览器里就不再触发；复位是这条线上唯一的
 *     "撤销"入口，不能建立在会静默失效的事件上。自己按时间戳判定。
 *
 * 整个文件包在 IIFE 里：app.js 有顶层的 `const clamp`，这里再写一个同名顶层
 * 绑定会直接抛重复声明，而不是"后者覆盖前者"。
 * ==========================================================================*/

(function () {

  /* 存储键带版本号。以后改动结构（加一条分栏、把像素换成比例）时，旧值直接
     作废比写一段迁移代码更安全——它只是一份界面偏好，丢了没有任何代价。 */
  var STORAGE_KEY = 'pentest.split.v1';

  /* 四条分栏。
   * `def` 必须与样式表里 `:root` 的默认值一致：双击复位回到它，两者一旦分叉，
   * 复位就会把栏位弹到一个**从来没有出现过**的宽度。
   * `min` 是最小可用宽度。再窄下去栏里的内容不是换行而是被硬切——那正是这张
   * 样式表反复在防的表现（见 style.css 文件头第 2 条与 CssLayoutGuardTestCase）。
   * `ratio` 是上限占容器的比例：上限不设的话，把上下文栏拉到 1400px 只会让
   * 消息流变成一条缝，而使用者此时看不到"我把另一栏挤没了"的因果。
   *
   * 「报告」那一条默认 320px：目录里每一行要放下状态徽章 + 类型标签 + 目标末段，
   * 而它还允许折起来，所以不必为了省宽度把默认值压到 240。 */
  var SPLITTERS = [
    { id: 'chat',   pane: '.chat-side',   token: '--split-chat',   min: 240, ratio: 0.60, def: 296 },
    /* 右侧栏：默认 380px。它装任务与子智能体两块，点开一行之后展开面（含执行过程
       那种逐行文本）就长在那一行下面，比左栏宽一档才读得动；`ratio` 取 0.5，上限是
       容器的一半——它与左栏、消息流三者共享宽度，两条 60% 上限叠起来会把消息流挤没。 */
    { id: 'detail', pane: '.chat-detail', token: '--split-detail', min: 300, ratio: 0.50, def: 380 },
    { id: 'graph',  pane: '.side-panel',  token: '--split-graph',  min: 280, ratio: 0.62, def: 360 },
    { id: 'assets', pane: '.tree-pane',   token: '--split-assets', min: 200, ratio: 0.70, def: 480 },
    { id: 'report', pane: '.report-side', token: '--split-report', min: 220, ratio: 0.60, def: 320 },
    /* 漏洞页的目标栏：默认 300。它只放目标名与两行计数，比报告目录窄一点。
       `ratio` 取 0.40——右栏是要读的漏洞卡片，目标栏不该占掉一半。 */
    { id: 'findings', pane: '.findings-side', token: '--split-findings', min: 200, ratio: 0.40, def: 300 },
    /* 流量页：左边是记录表、右边是那条的原文。默认 560px —— 表里有时间、方法、
       地址、状态、耗时五列，地址那一串还会被截，栏再窄就只剩时间与方法两列。
       `ratio` 取 0.70：右栏读的是原文，正文本身会换行，不需要跟左栏一样宽。 */
    { id: 'traffic', pane: '.traffic-list-pane', token: '--split-traffic', min: 320, ratio: 0.70, def: 560 },
    /* 请求重发页：左边是那张表单、右边是流量列表（点一行填进去）。默认 420px ——
       列表只有时间与地址两列在这里有用，而左边那张表单要放下 URL 输入框与两个
       多行文本框。`ratio` 取 0.60：表单是这一页的主体，侧栏不该占掉一半。 */
    { id: 'repeater', pane: '.traffic-side', token: '--split-repeater', min: 280, ratio: 0.60, def: 420 },
  ];

  /* 手抖阈值。与链路图的点选/拖拽共用同一个数字：两处都是"我按下了但没打算
     拖动"，手感应当是同一个。 */
  var CLICK_SLOP = 4;
  /* 双击复位的窗口。320ms 取的是系统双击间隔的常见上限。 */
  var DOUBLE_TAP_MS = 320;
  /* 键盘步进：方向键 24px、Shift+方向键 8px（微调）。 */
  var KEY_STEP = 24, KEY_STEP_FINE = 8;

  var sizes = read();
  var drag = null;
  var lastTap = { id: '', at: 0 };
  var resizeTimer = 0;
  var started = false;

  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

  function read() {
    /* localStorage 在隐私模式、被企业策略禁用、或配额写满时**读取本身就会抛**。
       这条路径上任何异常都不该让控制台起不来——它只记着"上次拖到哪里了"，
       拿不到就等于第一次打开。 */
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      return raw ? (JSON.parse(raw) || {}) : {};
    } catch (err) { return {}; }
  }

  function write() {
    try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(sizes)); } catch (err) { /* 存不下就只影响下次打开 */ }
  }

  function byId(id) {
    for (var i = 0; i < SPLITTERS.length; i++) {
      if (SPLITTERS[i].id === id) return SPLITTERS[i];
    }
    return null;
  }

  /** 取一条分栏的三个节点：手柄、它控制的栏、以及共同容器。 */
  function partsOf(item) {
    var handle = document.querySelector('.split-handle[data-split="' + item.id + '"]');
    if (!handle || !handle.parentElement) return null;
    var pane = handle.parentElement.querySelector(item.pane);
    return pane ? { handle: handle, pane: pane, box: handle.parentElement } : null;
  }

  /** 这一条分栏此刻的可用上下限。 */
  function boundsOf(item, box) {
    var wide = box.getBoundingClientRect().width;
    /* 容器量出来是 0，说明这个页签还没显示（面板 display:none，或控制台尚未
       登录）。此时**不能**拿它算上限：算出来是 0，会把宽度压到 min 并写进
       存储——用户下次打开就发现自己调好的宽度没了。退回视口宽度，
       它是此刻唯一还有意义的参考。 */
    var base = wide > 0 ? wide : window.innerWidth;
    var hi = Math.max(item.min, Math.round(base * item.ratio));
    return { lo: Math.min(item.min, hi), hi: hi };
  }

  function setWidth(item, px) {
    document.documentElement.style.setProperty(item.token, Math.round(px) + 'px');
  }

  function apply(item, px, persist) {
    if (!isFinite(px)) return;
    setWidth(item, px);
    if (persist) { sizes[item.id] = Math.round(px); write(); }
    syncAria(item);
  }

  /** 把手柄的无障碍读数对齐到当前宽度。读的是"意图宽度"而不是实测宽度：
     控制台未登录时那三栏量不到尺寸，而 aria-valuenow 应当是设定值。 */
  function syncAria(item) {
    var parts = partsOf(item);
    if (!parts) return;
    var px = Math.round(isFinite(sizes[item.id]) ? sizes[item.id] : item.def);
    parts.handle.setAttribute('aria-valuenow', String(px));
    parts.handle.setAttribute('aria-valuemin', String(item.min));
    parts.handle.setAttribute('aria-valuemax', String(boundsOf(item, parts.box).hi));
  }

  /** 拖动方向。**不写死在配置里**：由"这一栏在线左边还是右边"现场判定。
      写死的话，把节点详情挪到画布左侧就会变成拖动方向相反，
      而症状是"能拖，只是反的"——没有人会把这种表现当成缺陷报上来。 */
  function signOf(paneBox, handleBox) {
    return paneBox.left <= handleBox.left ? 1 : -1;
  }

  function onPointerDown(ev) {
    if (ev.pointerType === 'mouse' && ev.button !== 0) return;
    var item = byId(ev.currentTarget.getAttribute('data-split'));
    if (!item) return;
    var parts = partsOf(item);
    if (!parts) return;
    var handleBox = parts.handle.getBoundingClientRect();
    /* 窄屏下这条线被媒体查询隐藏（对话栏折到顶部一行），此时不该还能拖：
       把它"拖宽"等于给一个已经变成横条的栏设一个高度，而那个值在宽屏回来
       之后毫无意义。手柄不可见 = 这条分栏此刻不存在。 */
    if (handleBox.width <= 0.5) return;

    var now = Date.now();
    if (lastTap.id === item.id && now - lastTap.at < DOUBLE_TAP_MS) {
      lastTap.at = 0;
      apply(item, item.def, true);          // 双击复位
      ev.preventDefault();
      return;
    }
    lastTap.id = item.id;
    lastTap.at = now;

    drag = {
      item: item,
      parts: parts,
      sign: signOf(parts.pane.getBoundingClientRect(), handleBox),
      startX: ev.clientX,
      startW: parts.pane.getBoundingClientRect().width,
      moved: false,
    };
    parts.handle.classList.add('dragging');
    if (parts.handle.setPointerCapture) {
      /* 捕获让指针移出窗口后仍然收到事件。失败不致命：document 上的监听
         照样收到冒泡上来的 pointermove，只是移出窗口时会断。 */
      try { parts.handle.setPointerCapture(ev.pointerId); } catch (err) { /* 不阻塞拖动 */ }
    }
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    ev.preventDefault();
  }

  function onPointerMove(ev) {
    if (!drag) return;
    var dx = ev.clientX - drag.startX;
    if (!drag.moved) {
      if (Math.abs(dx) < CLICK_SLOP) return;   // 手抖不算拖动
      drag.moved = true;                        // 跨过阈值才开始真的改宽度
    }
    var bound = boundsOf(drag.item, drag.parts.box);
    apply(drag.item, clamp(drag.startW + drag.sign * dx, bound.lo, bound.hi), true);
  }

  function onPointerUp() {
    if (!drag) return;
    drag.parts.handle.classList.remove('dragging');
    drag = null;
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  }

  function onKeyDown(ev) {
    var item = byId(ev.currentTarget.getAttribute('data-split'));
    if (!item) return;
    var parts = partsOf(item);
    if (!parts) return;
    var handleBox = parts.handle.getBoundingClientRect();
    if (handleBox.width <= 0.5) return;
    var paneBox = parts.pane.getBoundingClientRect();
    var dir = ev.key === 'ArrowLeft' ? -1 : (ev.key === 'ArrowRight' ? 1 : 0);
    if (!dir) {
      if (ev.key === 'Home' || ev.key === 'Enter') { apply(item, item.def, true); ev.preventDefault(); }
      return;
    }
    /* 方向键移动的是**手柄**，不是栏：手柄往左，左侧的栏变窄、右侧的栏变宽。
       所以栏宽的变化量是 sign × 手柄位移——向右的栏与向左的栏自然是反的，
       这与鼠标拖动完全同感。 */
    var step = ev.shiftKey ? KEY_STEP_FINE : KEY_STEP;
    var bound = boundsOf(item, parts.box);
    apply(item, clamp(paneBox.width + signOf(paneBox, handleBox) * dir * step, bound.lo, bound.hi), true);
    ev.preventDefault();
  }

  /** 按存储值重设全部三条分栏。
   *  应用时夹到当前可用范围，但**不把夹过的结果写回存储**：把窗口临时拖窄
   *  不该永久改掉用户的宽度设定，窗口拉回来时宽度应当自己回来。 */
  function applyAll() {
    SPLITTERS.forEach(function (item) {
      var px = sizes[item.id];
      if (!isFinite(px)) return;
      var parts = partsOf(item);
      if (!parts) return;
      var bound = boundsOf(item, parts.box);
      setWidth(item, clamp(px, bound.lo, bound.hi));
      syncAria(item);
    });
  }

  function init() {
    if (started) return;
    started = true;
    SPLITTERS.forEach(function (item) {
      var parts = partsOf(item);
      if (!parts) return;
      parts.handle.setAttribute('title', '拖动调整宽度，双击复位');
      parts.handle.addEventListener('pointerdown', onPointerDown);
      parts.handle.addEventListener('keydown', onKeyDown);
      syncAria(item);
    });
    document.addEventListener('pointermove', onPointerMove);
    document.addEventListener('pointerup', onPointerUp);
    document.addEventListener('pointercancel', onPointerUp);
    window.addEventListener('resize', function () {
      /* 只在停下来之后重算。拖动窗口边缘时 resize 每帧都发，而每设一次
         自定义属性都会让这三栏重新布局一遍。 */
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(applyAll, 120);
    });
    applyAll();
  }

  /** 全部恢复默认宽度。排障用，也是"我把栏拖乱了"的兜底出口。 */
  function reset() {
    sizes = {};
    write();
    SPLITTERS.forEach(function (item) {
      setWidth(item, item.def);
      syncAria(item);
    });
  }

  window.PentestSplit = { init: init, reset: reset };
  /* 与 app.js 不同，这里**不经过 boot.js 的启动门**：它只读 DOM 与设备上的
     一份偏好，既不请求接口也不渲染数据，未登录时设置宽度同样无害。 */
  document.addEventListener('DOMContentLoaded', init);
})();
