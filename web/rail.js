'use strict';
/* ============================================================================
 * 收起左侧导航 —— 208px 的竖栏收成一条只留图标的窄条
 *
 * ## 为什么是这一条需求，而不是"把栏拖窄"
 *
 * 栏宽本来就能拖（split.js 管的是主区里那几条分栏线）。导航栏不在其中：它没有
 * 分栏手柄，宽度写死在样式表里。而"干活时想多留一点横向空间"是常态——尤其是
 * 1440px 的笔记本上，正文、会话栏、详情栏三列排开之后，每条消息都只剩二十来个
 * 汉字宽。收起之后省下的 144px 全部给正文。
 *
 * ## 三条"看着对、其实坏了"的写法
 *
 *   · **只把宽度收窄，不藏文字** —— 208px 换成 64px 之后，"探索链路"这类四字标签
 *     会在 44px 的内容宽里折成两行，导航变成一列乱码。所以收起态要同时把文字、
 *     分组标签与账号区藏掉，只留 16px 的图标。
 *   · **藏掉图标之外的东西，却不给回来的路** —— 按钮自己跟着一起藏起来是最容易
 *     写出来的一种：收起之后没有任何可点的东西，只能用开发者工具改回去。所以
 *     收起按钮**永远在**，只是从"图标 + 收起"变成一枚居中的图标。
 *   · **刷新之后自己弹开（或自己收回）** —— 界面偏好必须记住。这条与 split.js /
 *     fold.js 同一套：`pentest.<name>.v1`，读/写各带 try/catch。
 *
 * ## 与 fold.js 的刻意区分：折叠 ≠ 收起
 *
 * fold.js 折的是**块**（一块卡片的正文），它碰都不碰宽度，而且头本身就在卡上。
 * 这里收的是**整条栏**，改的正是宽度。两件事混在一个模块里，"折叠不许写宽度"
 * 那条不变式（tests/test_ui_contract.py 的 test_folding_never_touches_the_draggable_widths）
 * 就守不住了。所以状态类名也分开：fold.js 用 `is-folded`，这里用 `is-collapsed`。
 *
 * ## 窄屏（≤900px）为什么整条都不生效
 *
 * 900px 以下导航折成顶部横条（style.css 的 @media (max-width: 900px)），"收窄
 * 竖栏"在这个形态里没有意义。收起态的样式全部写在 @media (min-width: 901px)
 * 里，按钮在那条媒体查询里也被 `display: none` —— 否则横条上会出现一颗点了
 * 什么都不会发生的按钮。
 *
 * 与 split.js / fold.js 同一个判断：**不经过 boot.js 的启动门**。这里只读 DOM
 * 与一份设备上的偏好，既不请求接口也不渲染数据，未登录时收起同样无害。
 * ==========================================================================*/

(function () {

  /* 存储键带版本号，理由同 split.js：以后改动结构时旧值直接作废，比写迁移更安全。 */
  var STORAGE_KEY = 'pentest.rail.v1';

  /* 存的是**收起时为 'collapsed'、展开时为空串**，而不是 JSON：状态只有一个布尔，
     用不着序列化。空串与"从没写过"在读的时候等价（都按展开算），于是以后改动
     这个键的写法时，老值最坏也只是退回默认态。 */
  var MARK = 'collapsed';

  var collapsed = read();
  var bound = false;

  function read() {
    /* localStorage 在隐私模式、被企业策略禁用、或配额写满时**读取本身就会抛**。
       这条路径上漏一个 try，控制台启动就会被一份"上次收到哪"的偏好带崩，而症状
       与侧栏毫无关系（整页起不来）。与 split.js / fold.js 同一写法。 */
    try { return window.localStorage.getItem(STORAGE_KEY) === MARK; }
    catch (err) { return false; }
  }

  function write() {
    try { window.localStorage.setItem(STORAGE_KEY, collapsed ? MARK : ''); }
    catch (err) { /* 存不下只影响下次打开，不该影响这一次 */ }
  }

  /* 节点现场查，**不缓存**：控制台未登录时整块是 hidden 的，而 boot.js 换用户之后
     会重走一遍启动链；缓存下来的引用在这种重放里会变成游离节点。 */
  function railNode() { return document.getElementById('rail'); }
  function buttonNode() { return document.getElementById('rail-collapse'); }

  /** 把状态落到 DOM 与 a11y 上。
   *
   *  按钮上的 `aria-expanded` 与 `aria-label` 必须**跟着状态走**：收起之后那颗
   *  按钮的形状变了（文字没了、图标转了个向），只留一个"收起左侧导航"的名字，
   *  读屏用户会听到一个与眼前不符的动作名，而按下去恰好相反。 */
  function paint() {
    var rail = railNode();
    if (rail) rail.classList.toggle('is-collapsed', collapsed);
    var button = buttonNode();
    if (!button) return;
    button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    button.setAttribute('aria-label', collapsed ? '展开左侧导航' : '收起左侧导航');
    button.title = collapsed ? '展开左侧导航' : '收起左侧导航';
    paintTitles();
  }

  /** 收起之后页签只剩图标，**名字得有个去处**：鼠标停上去时要能读出来。
   *
   *  文字本身还在（只是 font-size: 0），所以 textContent 照样取得到——
   *  这也是这里不另存一份标签表的原因：以后加页签、改文案都不用回来同步。
   *  展开时把 title **删掉**：可见文字旁边再挂一份同名提示，是重复且碍事的。 */
  function paintTitles() {
    var tabs = document.querySelectorAll('.rail .tab');
    for (var i = 0; i < tabs.length; i += 1) {
      var tab = tabs[i];
      if (!collapsed) { tab.removeAttribute('title'); continue; }
      var name = (tab.textContent || '').replace(/\s+/g, ' ').trim();
      if (name) tab.title = name;
    }
  }

  /** 设成某个状态。`persist === false` 时只改这一次（初始化用）。 */
  function set(on, persist) {
    collapsed = Boolean(on);
    if (persist !== false) write();
    paint();
  }

  function toggle() { set(!collapsed); }

  function isCollapsed() { return collapsed; }

  function init() {
    if (bound) return;
    bound = true;
    paint();                                    // 先按存储值画一次，再挂事件
    var button = buttonNode();
    if (button) button.addEventListener('click', function () { toggle(); });
  }

  window.PentestRail = { init: init, set: set, toggle: toggle,
                         isCollapsed: isCollapsed, STORAGE_KEY: STORAGE_KEY };
  document.addEventListener('DOMContentLoaded', init);
})();
