'use strict';
/* ============================================================================
 * 启动编排 —— 先查登录态，再决定渲染登录门还是控制台
 *
 * 为什么单独一个文件：app.js 的 init() 会立刻拉 engagement / health，
 * 未登录时这些请求既没意义又会在页面上留下半个控制台。这里把启动权集中到一处：
 *   * 未登录 / 需要创建管理员 → 只显示 auth.js 的登录门，控制台保持 hidden；
 *   * 认证成功 → 取消 hidden，启动八个页签（app.js init + settings.js init +
 *     chat.js init + guide.js init）。
 *
 * **每个模块各自 try/catch：一个起不来，不该拖垮其余的。** 下标这一段整个跑在
 * auth.js 那条监听器的 try/catch 里，所以谁在这里抛，它后面的步骤一行都不执行。
 * 撞上过的一次是 app.js 的 `bindEvents()` 里一条指向"还没渲染出来"的节点的监听：
 * 结果**顶栏、漏洞、资产全空**，而错误条上那句话离原因极远（说的是日志面板）。
 * 现在的取舍：失败的模块名收进错误条、打到控制台，其余模块照常启动。缺一块功能
 * 还能用，而且缺的是哪一块是写明白的；整片不启动则什么也做不了。
 * ==========================================================================*/

(function () {
  let consoleStarted = false;
  const failures = [];

  /** 一个启动步骤；失败只记录，不中断后面的步骤。 */
  function safe(label, run) {
    try {
      run();
    } catch (error) {
      failures.push(label);
      if (window.console && window.console.error) window.console.error('[启动] ' + label + ' 失败', error);
    }
  }

  /** 所有启动步骤跑完之后统一报一次；全成功时一个字都不说。 */
  function reportFailures() {
    if (!failures.length) return;
    const names = failures.join('、');
    failures.length = 0;
    // 报错本身也可能不可用（失败的恰恰可能是 app.js），所以这里再挡一层。
    if (window.PentestUI && window.PentestUI.showError) {
      window.PentestUI.showError('界面初始化失败：' + names
        + '。其余部分已启动，可以继续使用；上述部分的功能会缺失。');
    }
  }

  function enterConsole() {
    if (consoleStarted) {
      safe('控制台数据', () => window.PentestUI.refreshAll());  // 换用户后刷新既有数据
      safe('设置', () => window.PentestSettings.reload());      // 配置与用户列表按新会话重取
      safe('对话', () => window.PentestChat.reload());          // 对话列表与可用能力按新会话重取
      safe('使用说明', () => window.PentestGuide.reload());      // 使用说明的状态条按新会话重探
      reportFailures();
      return;
    }
    consoleStarted = true;
    safe('设置', () => window.PentestSettings.init());
    safe('对话', () => window.PentestChat.init());
    safe('使用说明', () => window.PentestGuide.init());
    // 控制台（app.js）放最后：它同时是错误条的宿主，排在前面时它自己失败，会让
    // 上面几步的失败连说都说不出来。
    safe('控制台', () => window.PentestUI.init());
    // init 里有"只绑定一次"的守卫，首次进入不会顺带拉数据；对话页需要一次显式加载，
    // 否则登录后停在「控制台」页签时看到的是空列表（点击页签才加载是不可靠的）。
    safe('对话数据', () => window.PentestChat.reload());
    reportFailures();
  }

  function leaveConsole() {
    window.PentestSettings.onSignedOut();
    window.PentestChat.onSignedOut();
    window.PentestGuide.onSignedOut();
  }

  document.addEventListener('DOMContentLoaded', function () {
    // 主题兜底：正常情况下 <head> 里那段内联脚本已经把 data-theme 落好了，这里重读一次
    // localStorage 并核对，于是"内联脚本被 CSP 拦掉 / 那段被人删了"时仍是个正确的界面。
    // 必须排在设置页 init 之前——设置页要按当前主题把选中态画出来，属性晚一步到位，
    // 它就会把「液态玻璃」标成选中，而页面此刻正显示着午夜。
    safe('主题', () => window.PentestTheme.init());
    safe('认证监听', () => window.PentestAuth.onAuthChanged(function (user) {
      if (user) enterConsole(); else leaveConsole();
    }));
    safe('设置', () => window.PentestSettings.init());       // 设置页签的绑定与登录无关
    safe('对话', () => window.PentestChat.init());           // 对话页签：先绑定，登录后再拉数据
    safe('使用说明', () => window.PentestGuide.init());       // 使用说明页签：正文是本地静态数据，先渲染好
    // 这一句排在最后，而这正是它过去最危险的位置：上面任何一句抛出来，登录门就
    // 永远不会开始查状态——页面停在"控制台和登录门都 hidden"的空白上，没有任何
    // 提示。加上 safe 之后，登录这条路本身不再依赖别的东西起没起来。
    safe('认证启动', () => window.PentestAuth.start());      // 登录门 → 控制台（或反过来）
    reportFailures();
  });
})();
