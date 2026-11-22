'use strict';
/* ============================================================================
 * 折叠块 —— 会话页的左栏两块、右栏两块与「资产」页的资产目录共用一套
 *
 * ## 为什么抽成一个文件
 *
 * 折叠有三种失败形态，每一种都**不会让任何功能测试变红**，而且三种都只在界面上
 * 看得出来：
 *
 *   · **只藏内容、不藏外壳** —— 折起来之后剩一个空壳，而"它到底是折着的，还是
 *     本来就没有内容"分不出来；
 *   · **折了之后没有展开入口** —— 因为入口恰恰是被藏起来的那个头；
 *   · **刷新之后自己弹开** —— 刚折起来的东西下次打开又占着视野。
 *
 * 四处各写一遍就一定会有一处漏掉其中一条。所以机制只有一份，各块只声明自己的 key。
 *
 * ## 用法（全部声明在 index.html 上，不靠 JS 生成）
 *
 *   <button class="panel-head fold-head" type="button" data-fold="sessions">
 *     对话记录<span class="fold-mark" aria-hidden="true"></span>
 *   </button>
 *   ...
 *   <div class="chat-side-block" data-fold-part="sessions">…</div>
 *
 * **头在 HTML 里、不在渲染函数里**：这几块的正文每一帧都可能被重画（作业列表、
 * 子智能体行），头跟着重画的话，`aria-expanded` 与 `aria-controls` 会在两次渲染
 * 之间被抹掉，而表现是"点开之后过一会儿自己又变成折着的"。头是静态的，
 * 重画就碰不到它。
 *
 * ## 一处与 split.js 的刻意区分
 *
 * `split.js` 的文件头写着"**不做吸附、不做折叠**"，理由是折叠需要一套"折叠前
 * 宽度"的状态，而那条状态在刷新之后无法与用户的意图对齐。**那条理由在
 * split.js 里成立，在这里不适用**：这里的折叠不碰宽度。它是"藏起正文"，
 * `--split-chat` / `--split-assets` 一个字节都不动，所以"折叠前的宽度"根本
 * 不需要被记住——展开时宽度自己就在。同理，折起资产目录时那条拖拽条是
 * `display: none`，而 split.js 对不可见手柄本来就有一个早退
 * （`handleBox.width <= 0.5`），不需要它知道折叠这回事。
 * ==========================================================================*/

(function () {

  /* 存储键带版本号，与 split.js 同一个理由：以后改动结构时旧值直接作废，
     比写一段迁移代码更安全——它只是一份界面偏好。 */
  var STORAGE_KEY = 'pentest.fold.v1';

  /* 只记**折起来的**那些（true = 折起）。没写过的按展开算，于是以后新增一块
     不需要任何迁移：它的默认态就是展开，而它没被写进存储正是"没折过"。 */
  var folded = read();
  var bound = false;

  function read() {
    /* localStorage 在隐私模式、被企业策略禁用、或配额写满时**读取本身就会抛**。
       这条路径上漏一个 try，工作台启动就会被一份"上次折到哪"的偏好带崩，
       而症状与折叠毫无关系（整页起不来）。与 split.js 的 read/write 同一写法。 */
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      var parsed = raw ? JSON.parse(raw) : null;
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (err) { return {}; }
  }

  function write() {
    try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(folded)); }
    catch (err) { /* 存不下就只影响下次打开，不该影响这一次 */ }
  }

  function all(selector) {
    return Array.prototype.slice.call(document.querySelectorAll(selector));
  }

  /* 部件现场查，**不缓存 NodeList**：正文容器会被重画（清空再填），缓存的引用
     会变成游离节点，而那样折叠会静默地只作用于第一次渲染出来的那份内容。 */
  function partsOf(key) {
    return all('[data-fold-part="' + key + '"]');
  }

  function headsOf(key) {
    return all('[data-fold="' + key + '"]');
  }

  function keys() {
    var seen = [];
    all('[data-fold]').forEach(function (head) {
      var key = head.getAttribute('data-fold');
      if (key && seen.indexOf(key) < 0) seen.push(key);
    });
    return seen;
  }

  /** 把 a11y 关系补上。
   *
   *  `aria-controls` 由**这里**从部件身上取 id 拼出来，而不是手写在 HTML 上：
   *  手写的话它和 `data-fold-part` 是两份可以互相矛盾的声明，而矛盾的症状是
   *  读屏用户被告知"这按钮控制着 X"，实际收起来的是 Y——没有任何测试会红。 */
  function syncAria(head, key) {
    var ids = partsOf(key)
      .map(function (node) { return node.id; })
      .filter(Boolean);
    if (ids.length) head.setAttribute('aria-controls', ids.join(' '));
    head.setAttribute('aria-expanded', folded[key] ? 'false' : 'true');
  }

  /** 把某个 key 的折叠态落到 DOM 上。 */
  function paint(key) {
    var on = !!folded[key];
    partsOf(key).forEach(function (node) { node.classList.toggle('is-folded', on); });
    headsOf(key).forEach(function (head) { syncAria(head, key); });
  }

  /** 设成某个状态。`persist === false` 时只改这一次（初始化用）。 */
  function set(key, on, persist) {
    if (!key) return;
    if (on) folded[key] = true; else delete folded[key];
    if (persist !== false) write();
    paint(key);
  }

  function isFolded(key) { return !!folded[key]; }

  function toggle(key) { set(key, !folded[key]); }

  function init() {
    if (bound) return;
    bound = true;
    keys().forEach(function (key) {
      paint(key);                      // 先按存储值画一次，再挂事件
      headsOf(key).forEach(function (head) {
        head.addEventListener('click', function () { toggle(key); });
      });
    });
  }

  window.PentestFold = { init: init, set: set, toggle: toggle, isFolded: isFolded,
                         keys: keys, STORAGE_KEY: STORAGE_KEY };
  /* 与 split.js 同一个判断：这里只读 DOM 与一份设备上的偏好，既不请求接口
     也不渲染数据，未登录时折叠同样无害，所以不走 boot.js 的启动门。 */
  document.addEventListener('DOMContentLoaded', init);
})();
