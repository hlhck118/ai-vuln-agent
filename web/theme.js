'use strict';
/* ============================================================================
 * 外观 —— 主题 / 重点色 / 磨砂 / 透明度的**唯一真相**
 *
 * 四件事各自一个属性，都落在 <html> 上；前三件由 CSS 各自消费，第四件由本文件算：
 *
 *   data-theme   五套主题        （style.css 1a 节）
 *   data-accent  重点色六档      （style.css 1b 节）
 *   data-frost   磨砂四档        （style.css 1c 节）
 *   data-alpha   面的不透明度    （**没有 CSS 块**，见下面「透明度」一段）
 *
 * 为什么是四个独立的属性而不是一个"外观"对象：这四项**互不依赖**——换主题不该重置
 * 重点色，调磨砂不该重置主题，调透明度前两项都不该动。合成一个对象之后，任何一次
 * 写入都要先读回另外几项，而"读回"这一步在首屏（那段内联脚本）里根本不存在。
 *
 * 三处消费者必须共用这一份定义，否则会出现"列表里能选、选了没反应"这类最难查的
 * 不一致：
 *
 *   1. index.html <head> 里的内联脚本 —— **首屏之前**就把属性落到 <html>。
 *      它不能引用本文件：那要多一次网络往返，而多出来的那一帧就是整页白闪
 *      （深色主题的用户每次刷新都会被闪一下浅色底，这正是"主题功能做了一半"的观感）。
 *      两边靠同一组常量对齐，契约测试逐字比对。
 *   2. 设置页「外观」分组 —— 渲染三组可选项、一条透明度滑块与实时预览。
 *   3. 任何需要知道"现在是哪一档"的地方 —— 读 current() / accent() / frost() /
 *      alpha()。
 *
 * 前三轴的取值全在 style.css 的 1a-1c 节。本文件**只认 id 和展示信息**，不复制颜色：
 * 复制过来的颜色迟早会和 CSS 漂移，而症状是"预览卡片和点下去的样子不一样"。
 * 唯一需要的色值是最下面那些预览点，它们有契约测试盯着与 CSS 核对。
 * 透明度一轴连 id 都不认——它读的就是 1a 节里那几个面令牌的**原值**，算完再写回去，
 * 所以它永远跟着主题走，换主题不需要在这里复制一份新的色号。
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

  /* 透明度。**唯一一轴没有 CSS 块**，而且它不能有：面令牌（--panel 一族）在
   * style.css 里是 hex 或 rgba 字面量，CSS 里没有"把这个颜色整体乘 0.6"的写法
   * （color-mix 只能在一层里混，而且它会把取值变成颜色解析器读不懂的表达式——
   * tests/test_theme.py 的 _color() 只认 hex / rgb() / rgba() / var 链）。
   *
   * 所以这一轴的值由本文件在运行时算：读回 CSS 里的原值 → 按百分比乘 alpha →
   * **内联**写回 <html>。内联优先级最高，消费面令牌的那一百多处一处都不用改。
   *
   * 只乘面令牌，**不动 --bg**：窗口底必须始终不透明，否则 0% 时桌面会从整页底下
   * 透出来，界面变成一团糊。原值本来就带 alpha 的（玻璃 .58）按比例再乘一次。 */
  const ALPHA_KEY = 'pentest.alpha';
  const ALPHA_ATTR = 'data-alpha';
  const DEFAULT_ALPHA = 100;
  const ALPHA_TOKENS = ['--panel', '--panel-2', '--panel-3', '--code-bg', '--overlay'];

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

  /** 透明度是 0..100 的整数。滑块与 localStorage 给的都可能是空串或字母，
   *  parseInt 之后 NaN 由 isFinite 拦住——不收敛的话，坏值会让 paintAlpha 算出
   * NaN 写进 style，那个属性整条失效，看起来就是"拖了没反应"。 */
  function normalizeAlpha(value) {
    const num = parseInt(value, 10);
    if (!isFinite(num)) return DEFAULT_ALPHA;
    if (num < 0) return 0;
    if (num > 100) return 100;
    return num;
  }

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

  function alpha() {
    return normalizeAlpha(document.documentElement.getAttribute(ALPHA_ATTR));
  }

  /** 当前主题是亮色还是深色。重点色预览与「亮色」标签都读它。 */
  function mode() {
    const theme = get(current());
    return theme ? theme.mode : 'dark';
  }

  /** 把主题落到 <html> 上。
   *
   *  换主题要把透明度**重算一遍**：面令牌的原值跟着主题走（玻璃是 rgba，纸感是
   *  hex），而内联写回的是"原值 × 百分比"，所以原值一变，写回的那份就过期了。
   *  不重算的症状很具体：在 60% 下从玻璃切到纸感，面还是玻璃那份半透明色，
   *  而设置页的滑块仍停在 60%——两个地方各自看都对。 */
  function apply(id, options) {
    const next = normalize(id);
    document.documentElement.setAttribute(ATTR, next);
    if (!options || options.persist !== false) writeStored(STORAGE_KEY, next);
    paintAlpha(alpha());
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

  /** 把一行颜色文本拆成 [r, g, b, a]。只认 hex（三位 / 六位）、rgb()、rgba()——
   *  style.css 1a 节里出现过的就这三种。认不出来返回 null，调用方跳过这个令牌，
   *  而不是写一个坏值把整条自定义属性作废（那会静默回落成 initial，比不生效更难查）。
   *
   *  为什么自己解：自定义属性**不会**被解析成计算色，getPropertyValue 拿回来的
   *  就是作者写的那串字面量原文。 */
  function parseColor(text) {
    const raw = String(text || '').trim();
    if (!raw) return null;
    if (raw.charAt(0) === '#') {
      let hex = raw.slice(1);
      if (hex.length === 3) {
        hex = hex.charAt(0) + hex.charAt(0) + hex.charAt(1) + hex.charAt(1)
            + hex.charAt(2) + hex.charAt(2);
      }
      if (hex.length !== 6) return null;
      const num = parseInt(hex, 16);
      if (!isFinite(num)) return null;
      return [(num >> 16) & 255, (num >> 8) & 255, num & 255, 1];
    }
    const match = raw.match(/^rgba?\(([^)]*)\)$/);
    if (!match) return null;
    const parts = match[1].split(/[\s,\/]+/).filter((one) => one.length > 0);
    if (parts.length < 3) return null;
    const rgb = parts.slice(0, 3).map((one) => {
      const num = parseFloat(one);
      return isFinite(num) ? num : 0;
    });
    const last = parts.length > 3 ? parseFloat(parts[3]) : 1;
    return [rgb[0], rgb[1], rgb[2], isFinite(last) ? last : 1];
  }

  /** 把透明度落到面令牌上。**读的是 CSS 原值，不是上次算出来的结果**。
   *
   *  顺序是刻意的：先摘掉自己的内联值，再读，最后写。若从当前值读，拖滑块时每一帧
   *  都会在上一帧的结果上再乘一次（100→60 变成 0.6 的等比衰减，很快就不透了），
   *  而症状是"拖到底和拖到一半看起来一样"。
   *
   *  100% 时**什么都不写**：把 CSS 原值抄成内联等于钉住一份拷贝，主题换色时那份拷贝
   *  不跟着走。撤掉内联之后又回到 CSS 的层叠，换主题自动生效。 */
  function paintAlpha(percent) {
    const root = document.documentElement;
    ALPHA_TOKENS.forEach((token) => { root.style.removeProperty(token); });
    const value = normalizeAlpha(percent);
    if (value >= 100) return;
    const scale = value / 100;
    const computed = window.getComputedStyle(root);
    const original = ALPHA_TOKENS.map((token) => [token, parseColor(computed.getPropertyValue(token))]);
    original.forEach((pair) => {
      const token = pair[0];
      const color = pair[1];
      if (!color) return;
      root.style.setProperty(token, 'rgba(' + color[0] + ', ' + color[1] + ', ' + color[2]
        + ', ' + (color[3] * scale).toFixed(3) + ')');
    });
  }

  /** 透明度唯一入口。**不 notify**：它不改配色，也没有哪一处要跟着重画
   *  （设置页自己调完就重画滑块那一行）。与 applyFrost 同理。 */
  function applyAlpha(percent, options) {
    const next = normalizeAlpha(percent);
    document.documentElement.setAttribute(ALPHA_ATTR, String(next));
    if (!options || options.persist !== false) writeStored(ALPHA_KEY, String(next));
    paintAlpha(next);
    return next;
  }

  /* 主题切换**不需要重绘任何东西**：图谱是 SVG + CSS，没有 canvas，也没有哪处
   * 用 getComputedStyle 把颜色读进 JS（唯一一处是上面的透明度重算，那是它自己的
   * 职责）。通知只是给"想跟着变"的模块留的口子
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
   *  设置页会显示成没选中任何一项。这里把四个属性拉回来，代价是四次属性写入。
   *
   *  透明度**总是重算**，不做"属性一样就跳过"的批发：内联脚本只写得了属性，
   *  写不了内联的令牌值（那要等样式表解析完）。所以 init() 里这一遍是页面生命
   *  周期中第一次真正把透明度作用到面令牌上——跳过它就等于刷新之后透明度失效。 */
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
    if (document.documentElement.getAttribute(ALPHA_ATTR) !== String(alpha())) {
      document.documentElement.setAttribute(ALPHA_ATTR, String(alpha()));
    }
    paintAlpha(alpha());
    return current();
  }

  return {
    /* 四项对齐锚点：内联脚本与契约测试都按这些名字比对 */
    STORAGE_KEY: STORAGE_KEY,
    ATTR: ATTR,
    DEFAULT_ID: DEFAULT_ID,
    ACCENT_KEY: ACCENT_KEY,
    ACCENT_ATTR: ACCENT_ATTR,
    DEFAULT_ACCENT: DEFAULT_ACCENT,
    FROST_KEY: FROST_KEY,
    FROST_ATTR: FROST_ATTR,
    DEFAULT_FROST: DEFAULT_FROST,
    ALPHA_KEY: ALPHA_KEY,
    ALPHA_ATTR: ALPHA_ATTR,
    DEFAULT_ALPHA: DEFAULT_ALPHA,
    ALPHA_TOKENS: ALPHA_TOKENS,
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
    normalizeAlpha: normalizeAlpha,
    current: current,
    accent: accent,
    frost: frost,
    alpha: alpha,
    mode: mode,
    apply: apply,
    applyAccent: applyAccent,
    applyFrost: applyFrost,
    applyAlpha: applyAlpha,
    subscribe: subscribe,
    init: init,
  };
})();
