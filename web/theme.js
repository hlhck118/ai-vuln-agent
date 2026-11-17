'use strict';
/* ============================================================================
 * 外观 —— 主题 / 重点色 / 磨砂的**唯一真相**
 *
 * 三件事各自一个属性，都落在 <html> 上，由 CSS 各自消费：
 *
 *   data-theme   五套主题        （style.css 1a 节）
 *   data-accent  重点色六档      （style.css 1b 节）
 *   data-frost   磨砂四档        （style.css 1c 节）
 *
 * 为什么是三个独立的属性而不是一个"外观"对象：这三项**互不依赖**——换主题不该重置
 * 重点色，调磨砂不该重置主题。合成一个对象之后，任何一次写入都要先读回另外两项，
 * 而"读回"这一步在首屏（那三行内联脚本）里根本不存在。
 *
 * 三处消费者必须共用这一份定义，否则会出现"列表里能选、选了没反应"这类最难查的
 * 不一致：
 *
 *   1. index.html <head> 里的内联脚本 —— **首屏之前**就把这三个属性落到 <html>。
 *      它不能引用本文件：那要多一次网络往返，而多出来的那一帧就是整页白闪
 *      （深色主题的用户每次刷新都会被闪一下浅色底，这正是"主题功能做了一半"的观感）。
 *      两边靠同一组常量对齐，契约测试逐字比对。
 *   2. 设置页「外观」分组 —— 渲染三组可选项与实时预览。
 *   3. 任何需要知道"现在是哪一档"的地方 —— 读 current() / accent() / frost()。
 *
 * 取值全在 style.css 的 1a-1c 节。本文件**只认 id 和展示信息**，不复制颜色：
 * 复制过来的颜色迟早会和 CSS 漂移，而症状是"预览卡片和点下去的样子不一样"。
 * 唯一需要的色值是最下面那些预览点，它们有契约测试盯着与 CSS 核对。
 * ==========================================================================*/
window.PentestTheme = (function () {
  const STORAGE_KEY = 'pentest.theme';
  const ATTR = 'data-theme';
  const DEFAULT_ID = 'glass';

  const ACCENT_KEY = 'pentest.accent';
  const ACCENT_ATTR = 'data-accent';
  const DEFAULT_ACCENT = 'theme';

  const FROST_KEY = 'pentest.frost';
  const FROST_ATTR = 'data-frost';
  const DEFAULT_FROST = 'theme';

  /* 每套主题一句话说清**它为什么存在**，而不只是它什么颜色：
   * 使用者要选的是"我现在的处境"（亮环境、投影仪、整天泡终端），不是色号。
   *
   * mode 不是给 CSS 用的（那由 :root 上的 color-scheme 决定），是给卡片打
   * 「深色 / 亮色」标签用的——列表里必须一眼看出哪几套是亮色，否则在亮环境里
   * 逐个点开试，正是这类功能最容易让人失去耐心的地方。它还被重点色预览用来
   * 决定画哪一族色块（深浅两族的 --accent 明暗关系相反）。
   *
   * swatch = [底, 重点色, 正文]，三个值在 CSS 里各有一份，契约测试逐条核对它们
   * 确实出现在对应主题的块里。冗余是刻意的：预览必须能**不切换主题**就画出来。 */
  const THEMES = [
    {
      id: 'glass',
      name: '液态玻璃',
      mode: 'light',
      hint: '半透明磨砂 + 环境色晕。亮色，靠透光与上边缘高光的层次读材质。默认。',
      swatch: { bg: '#eaeef7', accent: '#1b63d6', text: '#0c1017' },
    },
    {
      id: 'midnight',
      name: '午夜',
      mode: 'dark',
      hint: '改版前的深色外观，逐令牌保留（说明文字提亮一档以过 AA）。不透明、无模糊，最省显卡。',
      swatch: { bg: '#0b0b0f', accent: '#4f8cff', text: '#f2f2f7' },
    },
    {
      id: 'paper',
      name: '纸感',
      mode: 'light',
      hint: '哑光亮色，不铺环境光。白昼、投屏、截图进报告时用；也是打印前的唯一选择。',
      swatch: { bg: '#f4f5f8', accent: '#0a63d6', text: '#14161c' },
    },
    {
      id: 'terminal',
      name: '终端',
      mode: 'dark',
      hint: '等宽字体、直角、零过渡。给整天泡在 tmux 里的人。',
      swatch: { bg: '#050805', accent: '#46d5c8', text: '#c6f5cf' },
    },
    {
      id: 'contrast',
      name: '高对比',
      mode: 'dark',
      hint: '纯黑白、无模糊、分隔线提到可见。弱对比屏与无障碍档。',
      swatch: { bg: '#000000', accent: '#8ec5ff', text: '#ffffff' },
    },
  ];

  /* 重点色。**深色档与亮色档各一个色值**，因为 --accent 的语义在两档里是相反的：
   * 深色主题它是亮填充配深字，亮色主题它是深填充配白字。只存一个色值的话，
   * 预览在另一档的主题下会画出一个"点下去不是这个颜色"的色块。
   *
   * 刻意**不提供**红 / 橙 / 黄 / 绿四段。那不是"少给几个选项"，而是本表一条既有原则：
   * 一支颜色不能一会儿表示"这一项生效"、一会儿表示"这一项危险"。这四段已经有主
   * （严重度四档与 --ok），重点色走进去之后，红色徽章到底是"选中"还是"严重"就分不出来了。
   *
   * 'theme' 这一档**没有对应的 CSS 块**——没有块就是没有覆盖。给它写一个"把主题自己的
   * 值再抄一遍"的块，会在主题自身换色时把旧值悄悄钉住。 */
  const ACCENTS = [
    { id: 'theme', name: '跟随主题', hint: '不覆盖，用每套主题自己的重点色。', dark: '', light: '' },
    { id: 'blue', name: '蓝', hint: '默认档。深色下亮蓝，亮色下深蓝。', dark: '#6497ea', light: '#1b63d6' },
    { id: 'indigo', name: '靛', hint: '偏冷的蓝紫，比蓝更沉。', dark: '#978aef', light: '#634ee9' },
    { id: 'cyan', name: '青', hint: '本表既有的一支：它不与任何严重度同族。', dark: '#1aa6c1', light: '#117386' },
    { id: 'purple', name: '紫', hint: '偏暖的紫。与图谱里"意图"节点同族，注意区分。', dark: '#c875ec', light: '#a41bdf' },
    { id: 'magenta', name: '品红', hint: '最靠红的一端。再往右就是危险色，所以到此为止。', dark: '#ea64cf', light: '#be179d' },
  ];

  /* 磨砂。默认档不写属性，取主题自己的取值——液态玻璃是 22px，其余主题是 0。
   * 档位只影响**模糊半径与饱和度**，不动任何配色：磨砂是材质，不是主题。
   *
   * 默认档的 id 与重点色的默认档同名（`theme`），两处是一个意思：「跟随主题」。
   * 它**不叫 system**：它跟的不是操作系统，是当前那套主题；系统级那个
   * prefers-reduced-transparency 是另一个方向的东西，而且它压在这一档之上。 */
  const FROSTS = [
    { id: 'theme', name: '跟随主题', hint: '液态玻璃 22px，其余主题本来就没有材质。' },
    { id: 'off', name: '关', hint: '不模糊。材质整个撤掉，界面回到平面。' },
    { id: 'soft', name: '轻', hint: '10px。侧栏与页头读得出材质，正文区几乎无感。' },
    { id: 'mid', name: '中', hint: '20px。层次最明显的一档。' },
    { id: 'strong', name: '强', hint: '34px。玻璃读起来像一块厚亚克力，代价是每帧一张更大的位图。' },
  ];

  const ids = THEMES.map((t) => t.id);
  const accentIds = ACCENTS.map((a) => a.id);
  const frostIds = FROSTS.map((f) => f.id);
  const listeners = [];

  function pick(list, id, fallback) {
    for (let i = 0; i < list.length; i += 1) {
      if (list[i].id === id) return list[i];
    }
    return fallback;
  }

  function get(id) { return pick(THEMES, id, null); }
  function getAccent(id) { return pick(ACCENTS, id, ACCENTS[0]); }
  function getFrost(id) { return pick(FROSTS, id, FROSTS[0]); }

  /** 任何来路不明的值（旧版本残留、手改过的 localStorage）一律收敛到默认档。 */
  function normalize(value) { return ids.indexOf(value) >= 0 ? value : DEFAULT_ID; }
  function normalizeAccent(value) { return accentIds.indexOf(value) >= 0 ? value : DEFAULT_ACCENT; }
  function normalizeFrost(value) { return frostIds.indexOf(value) >= 0 ? value : DEFAULT_FROST; }

  /** localStorage 在隐私模式、被企业策略禁用时会**读写即抛**。
   *  这里漏一个 try，整页会因为一份"上次选了哪套主题"的偏好而启动失败，
   *  而症状与主题毫无关系——排查要花很久。 */
  function readStored(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (err) {
      return null;
    }
  }

  function writeStored(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (err) {
      /* 存不下就算了：本次会话照常生效，只是下次打开回到默认。 */
    }
  }

  /** 当前生效的档位。纯读——不改 DOM，所以可以在任何地方安全调用。 */
  function current() {
    const attr = document.documentElement.getAttribute(ATTR);
    return ids.indexOf(attr) >= 0 ? attr : DEFAULT_ID;
  }

  function accent() {
    const attr = document.documentElement.getAttribute(ACCENT_ATTR);
    return accentIds.indexOf(attr) >= 0 ? attr : DEFAULT_ACCENT;
  }

  function frost() {
    const attr = document.documentElement.getAttribute(FROST_ATTR);
    return frostIds.indexOf(attr) >= 0 ? attr : DEFAULT_FROST;
  }

  /** 当前主题是亮色还是深色。重点色预览与「亮色」标签都读它。 */
  function mode() {
    const theme = get(current());
    return theme ? theme.mode : 'dark';
  }

  /** 把三项落到 <html> 上。这是唯一改 DOM 的入口。
   *
   *  三件事共用一个入口，是为了让"通知监听者"只发生一次——分开三个入口的话，
   *  一次"换成纸感 + 青 + 关磨砂"会触发三次回调，而每个回调都要重画一遍列表。 */
  function apply(id, options) {
    const next = normalize(id);
    document.documentElement.setAttribute(ATTR, next);
    if (!options || options.persist !== false) writeStored(STORAGE_KEY, next);
    notify(next);
    return next;
  }

  function applyAccent(id, options) {
    const next = normalizeAccent(id);
    document.documentElement.setAttribute(ACCENT_ATTR, next);
    if (!options || options.persist !== false) writeStored(ACCENT_KEY, next);
    notify(current());
    return next;
  }

  function applyFrost(id, options) {
    const next = normalizeFrost(id);
    document.documentElement.setAttribute(FROST_ATTR, next);
    if (!options || options.persist !== false) writeStored(FROST_KEY, next);
    return next;
  }

  /* 主题切换**不需要重绘任何东西**：图谱是 SVG + CSS，没有 canvas，也没有哪处
   * 用 getComputedStyle 把颜色读进 JS。通知只是给"想跟着变"的模块留的口子
   * ——重点色预览的色块要在主题明暗换边时重画，它是目前唯一的订阅者。 */
  function notify(id) {
    listeners.forEach((fn) => {
      try {
        fn(id);
      } catch (err) {
        /* 一个监听者抛错不该让整次切换失败。 */
      }
    });
  }

  function subscribe(fn) {
    if (typeof fn !== 'function') return function () {};
    listeners.push(fn);
    return function () {
      const i = listeners.indexOf(fn);
      if (i >= 0) listeners.splice(i, 1);
    };
  }

  /** 启动时对齐一次。内联脚本正常情况下已经做过同样的事，这里是**兜底**：
   *  内联脚本被 CSP 拦掉、或 <head> 里那段被谁删了，DOM 上就没有属性，
   *  于是 CSS 回落到 :root（午夜外观）而 current() 报"液态玻璃"——两边说法不一致，
   *  设置页会显示成没选中任何一项。这里把三个属性拉回来，代价是三次属性写入。 */
  function init() {
    if (document.documentElement.getAttribute(ATTR) !== normalize(readStored(STORAGE_KEY))) {
      apply(readStored(STORAGE_KEY), { persist: false });
    }
    if (document.documentElement.getAttribute(ACCENT_ATTR) !== normalizeAccent(readStored(ACCENT_KEY))) {
      applyAccent(readStored(ACCENT_KEY), { persist: false });
    }
    if (document.documentElement.getAttribute(FROST_ATTR) !== normalizeFrost(readStored(FROST_KEY))) {
      applyFrost(readStored(FROST_KEY), { persist: false });
    }
    return current();
  }

  return {
    /* 三项对齐锚点：内联脚本与契约测试都按这些名字比对 */
    STORAGE_KEY: STORAGE_KEY,
    ATTR: ATTR,
    DEFAULT_ID: DEFAULT_ID,
    ACCENT_KEY: ACCENT_KEY,
    ACCENT_ATTR: ACCENT_ATTR,
    DEFAULT_ACCENT: DEFAULT_ACCENT,
    FROST_KEY: FROST_KEY,
    FROST_ATTR: FROST_ATTR,
    DEFAULT_FROST: DEFAULT_FROST,
    THEMES: THEMES,
    ACCENTS: ACCENTS,
    FROSTS: FROSTS,
    ids: ids,
    accentIds: accentIds,
    frostIds: frostIds,
    get: get,
    getAccent: getAccent,
    getFrost: getFrost,
    normalize: normalize,
    normalizeAccent: normalizeAccent,
    normalizeFrost: normalizeFrost,
    current: current,
    accent: accent,
    frost: frost,
    mode: mode,
    apply: apply,
    applyAccent: applyAccent,
    applyFrost: applyFrost,
    subscribe: subscribe,
    init: init,
  };
})();
