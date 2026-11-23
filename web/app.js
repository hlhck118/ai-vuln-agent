'use strict';
/* ============================================================================
 * AI 智能体渗透测试工作台 —— 前端主逻辑
 * 纯原生 JavaScript + SVG：无框架、无构建、无 CDN、无 npm 依赖。
 * 分区：常量 / 工具 / API / 状态 / 顶栏 / 链路图 / 节点详情 / 数据加载 /
 *       漏洞 / 资产 / Markdown / 部署面板 / 作业 / 事件 / 启动。
 * 安全：接口数据一律经 textContent、createTextNode 落 DOM，绝不拼 HTML 字符串。
 * ==========================================================================*/

/* ---------------------------------------------------------- 1. 常量 ---- */
const API_BASE = '/api/v1';
const REQUEST_TIMEOUT = 15000;
// 缩放：**600% 是基准**。
//
// 读数的分母是"整幅可见"（`resetView` 复位到 `scale = 1`，而 viewBox 就是整图，
// 所以 100% = 整幅塞进画布）。600% 因此是"整幅的六倍"——这个平台认定的基准倍率：
// 一进来就该有这么近，而不是从整幅开始往上爬。
//
// 两件事曾经把它做坏，症状都是"链路显示还是太小"：
//
// 1. **上限是个死数 `MAX_SCALE = 6`，而初始缩放会被它夹住**（原来的
//    `scale = Math.min(initial, MAX_SCALE)`）。对几百个节点的合并视图，
//    `initialGraphScale()` 算出的是 `1 / fit`（1 布局单位 = 1 CSS 像素），
//    可能到 18；夹到 6 之后就**停在 600%**——于是 600% 既是起点又是墙，
//    而"再放大一点"做不到。现在上限跟着每张图算（`graphView.ceiling`），
//    600% 从"墙"变成"基准线"。
// 2. **画布还没量到尺寸时就算了初始缩放。** `#graph-canvas` 在别的页签上是
//    0×0，`fitScale()` 返回 0，只能退回 1（= 100%）；而 `renderGraph` 平时
//    被 `graphLayoutKey` 挡在"换图才复位"之外，于是那次算错的缩放**一直留着**。
//    现在这种情形返回基准，并且在画布第一次量到尺寸时重算一次。
const MIN_SCALE = 0.15;
const BASE_SCALE = 6;                 // 600%：默认不低于它
const BASE_HEADROOM = 5;              // 从基准之上还能再放大五倍（600% → 3000%）
// 「点了一下」与「拖了一下」的分界，单位是**屏幕像素**。
//
// 这个数字必须用屏幕像素而不是 viewBox 用户坐标：viewBox 由后端 layout 给出，
// 常常比显示区域宽好几倍（实测 2190 用户单位挤在 456px 里，1 屏幕像素 = 4.8 用户
// 单位）。用用户坐标定阈值，手抖 1 像素就跨过阈值，于是每一次点选都被判成拖拽、
// 被 endDrag 吞掉——表现就是"切换节点，节点详情不变"。
const CLICK_SLOP_PX = 4;
/* 卡上那个「这是什么」的读法。**八个词，不是一个 kind 一个词**：
 *  `begin` / `hint` 是显示类型（见 `displayKindOf`），库里并没有这两类节点——
 *  起点是一颗 `state=origin` 的事实（任务根），提示是一条 `channel=hint` 的事实。
 *  后端 `pentestd/graph.py` 的 `DISPLAY_LABELS` 是同一份词表的另一处，改一处要改两处。
 *
 *  `digest` 叫「压缩」而不是「摘要」：这一页上「摘要」还指对话摘要、报告摘要、
 *  令牌摘要，同一个词在三处指三件事，读图的人只能靠上下文猜。压缩说的正是它的
 *  行为——把若干条记录压成一段。 */
const KIND_LABEL = { begin: '起点', goal: '目标', hint: '提示', intent: '意图', fact: '事实', finding: '漏洞', digest: '压缩', asset: '资产' };
// 节点的生命周期状态（与 `models.STATE_LABELS` 同一套说法）：它回答「这条记录的进展」，
// 与 `kind` 回答的「这是什么」分开——摘要有 active / superseded，事实有 origin / confirmed。
// `blocked` / `pending` 是编排面新增的两档（意图可以被挂起或判定走进死路）。原先这份表
// 只有七个词，于是那两档在卡上会退回显示英文原文——状态是给人扫读的，露出原词等于没有。
const STATE_LABEL = { origin: '起点', confirmed: '已确认', done: '已完成', exhausted: '已穷尽', met: '已达成', active: '进行中', superseded: '已取代', blocked: '已阻塞', pending: '待跟进' };
/* 类型芯片与图例的顺序（照 ARTEX 的画布图例：起点在最前，它是整张图的入口）。
 *  这份表必须与 `state.kindFilter` 的键**完全一致**，漏一个那一类就关不掉。 */
const KIND_ORDER = ['begin', 'goal', 'hint', 'intent', 'fact', 'finding', 'digest'];
const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'info'];
const SEVERITY_LABEL = { critical: '严重', high: '高危', medium: '中危', low: '低危', info: '信息' };
/* 关系的中文名。四条是**画布上真画出来的**（其余两条见 `EDGE_HIDDEN_KINDS`）：
 *
 *  * `spawns` 目标 → 意图，叫「派生」；
 *  * `derived_from` 事实 → 新意图，叫「意图链」——它就是上一个意图留下的疑点
 *    又长出一条新意图的那一步；
 *  * `yields` 意图 → 事实，叫「产出」；
 *  * `proves` 意图 → 漏洞，叫「证明」。
 *
 * 换过一轮：原先 `spawns` 叫「意图链」而它连的是目标→意图，`derived_from` 叫
 * 「推导自」而它连的是两条意图之间的接力，两个词互相顶着。换过来之后边名自己
 * 说清谁派生谁。后端 `pentestd/models.py` 的 `EDGE_LABELS` 是同一份。 */
const EDGE_LABEL = { spawns: '派生', derived_from: '意图链', yields: '产出', proves: '证明', covers: '覆盖', parent: '归属' };
/** 图谱关系类型。**这里只有类型，没有颜色**——颜色由 CSS 令牌（--edge-*）下发，
 *  见 style.css 的 .gedge 一节。SVG 的表现属性不认 var()，颜色写在这里就等于
 *  整组绕过主题：换主题时节点换了色，边还是深色主题那一套。 */
/* 画布上画的关系，只有这四种。`covers` 是「压缩吸收了谁」、`parent` 是资产父子，
 *  两条都是**结构**而不是推进：画出来会让压缩节点向它吸收的每条记录发散一把虚线，
 *  整张图立刻从一条链变成一张网。它们仍在 layout 里（详情面板与离线 SVG 用），
 *  只是不上画布——见 `EDGE_HIDDEN_KINDS`。 */
const EDGE_KINDS = ['spawns', 'derived_from', 'yields', 'proves'];
/** 有数据、但不画在画布上的关系。详情面板与导出仍然列它们。 */
const EDGE_HIDDEN_KINDS = ['covers', 'parent'];
/** 没见过的 kind 按这个上色。必须与 .gedge 基础规则里的默认令牌一致。 */
const EDGE_FALLBACK_KIND = 'spawns';
/** 详情面板里「关联边」的排列顺序：先按关系的方向语义（派生 → 意图链 → 产出 →
 *  证明 → 覆盖 → 归属），同一种关系再按对端在布局里的列从左到右。
 *  它回答的是「这条链从我这儿怎么往下走」，所以必须有个稳定顺序——
 *  按数据到达顺序列出来，同一条记录两次点开顺序都不一样（作业是并发写的）。 */
const EDGE_SORT_ORDER = ['spawns', 'derived_from', 'yields', 'proves', 'covers', 'parent'];
/* 资产类型的中文说法。**这不是唯一的来源**：权威词表在 `pentestd/models.py` 的
 * `ASSET_TYPE_LABELS`，平台把结果贴成 `asset.typeLabel`（`store.asset_tree()` 里贴）。
 * 留这一份只是兜底——本地缓存的旧快照、或者直接读 `state.assets` 里没经过后端的那几条，
 * `typeLabel` 会缺席。取值顺序永远先看 `typeLabel`，再看这张表。
 */
const ASSET_TYPE_LABEL = { 'root-domain': '根域', subdomain: '子域', ip: 'IP', service: '服务', app: '应用', endpoint: '端点' };
const assetTypeWord = (asset) => (asset && (asset.typeLabel || ASSET_TYPE_LABEL[asset.type])) || (asset && asset.type) || '';
/** 各页签的加载中文案（只给当前页签显示，避免误导）。 */
const LOADING_TEXT = { findings: '正在加载漏洞数据…', assets: '正在加载资产数据…', report: '正在加载报告…' };
/** 详情面板的字段中文别名；未列出的一律显示原始键名。 */
const FIELD_LABEL = {
  id: 'ID', kind: '节点类型', title: '标题', detail: '详情', description: '描述', severity: '严重度', factKind: '事实类型',
  target: '目标', objective: '目标说明', authorization: '授权', confidence: '置信度', intentId: '所属意图',
  affectedAssetId: '影响资产', reproducibleSteps: '可复现步骤', parentId: '父资产', type: '资产类型', value: '资产值',
  meta: '元信息', createdAt: '创建时间', updatedAt: '更新时间', lastSeen: '最近心跳', sourceId: '源节点',
  targetId: '目标节点', status: '状态', load: '负载', capabilities: '能力', name: '名称', children: '子资产',
  seq: '序号',
  // 合并视图给每个节点补的两个字段。没有标签的话它们在详情面板里显示为原始键名
  // （`engagementId`），而这是使用者判断"这条证据是哪个目标的"唯一依据。
  engagementId: '所属目标', engagementTarget: '目标地址', goalId: '所属目标节点',
  derivedFromFactId: '意图链',
};

/* ---------------------------------------------------------- 2. 工具 ---- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const asArray = (v) => (Array.isArray(v) ? v : []);
const clamp = (v, min, max) => Math.min(max, Math.max(min, v));
const emptyBlock = (text) => el('div', 'empty', text);
const errorBlock = (text) => el('div', 'empty error', text);
/** 严重度的规范值：认不出的或空的都落回 `info`。
 *
 *  后端给的是原值，直接拼进类名会得到 `sev-` 或 `sev-unknown`，样式表里没有这两种，
 *  圆点又回到没有颜色的老问题。全站所有档位渲染（漏洞卡片、详情页、过滤条、节点卡）
 *  都必须先过这里。 */
const severityOf = (v) => { const s = String(v || '').toLowerCase(); return SEVERITY_ORDER.includes(s) ? s : 'info'; };
/** 卡上那个「这是什么」：**七个结构类型 + 一个派生出来的「起点」**。
 *
 *  `state=origin` 的事实就是任务根（整张图最左边那张），画成事实会和它右边一堆
 *  探测结果长得一样，而它其实是「这条链路从哪来」的那一条。后端 `build_layout`
 *  已经算好 `displayKind`，这里只兜底——本地缓存的旧快照、或者一次没走布局的
 *  节点，都还没有那个字段。 */
const displayKindOf = (node) => (node && (node.displayKind || node.kind)) || 'intent';
/** 优先级 → 三档语气。照 ARTEX 的 `prioTone`：8 以上红灯、5 以上黄灯、其余中性。
 *
 *  三档而不是按 1-9 线性插值：这条读数只在「这张卡要不要先看」这一件事上有意义，
 *  九个色阶会让人以为 P6 与 P7 之间有可分辨的差别，而实际上没有。 */
const priorityTone = (p) => (p >= 8 ? 'red' : p >= 5 ? 'amber' : 'slate');
const cssEscape = (v) => (window.CSS && window.CSS.escape ? window.CSS.escape(String(v)) : String(v).replace(/["\\\]]/g, '\\$&'));

/** 创建 HTML 元素：文本走 textContent，天然转义。 */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
/** 创建 SVG 元素（SVG 属性必须逐个 setAttribute）。 */
function svgEl(tag, attrs) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  Object.keys(attrs || {}).forEach((key) => node.setAttribute(key, String(attrs[key])));
  return node;
}
function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
/** 秒级（或毫秒级）时间戳 → 本地可读时间。 */
function formatTime(value) {
  const num = Number(value);
  if (!Number.isFinite(num) || num <= 0) return '—';
  const date = new Date(num > 1e12 ? num : num * 1000), pad = (n) => String(n).padStart(2, '0');
  if (Number.isNaN(date.getTime())) return String(value);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
/** 字段值 → 文本；时间类键自动格式化。 */
function formatValue(value, key) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') return /At$|lastSeen/i.test(key || '') ? formatTime(value) : String(value);
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (Array.isArray(value)) return value.length ? value.map((v) => (v && typeof v === 'object' ? JSON.stringify(v) : String(v))).join('\n') : '（空）';
  return typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
}
/** 按字符数截断并补省略号（避免图内文字溢出节点框）。 */
function truncate(text, max) {
  const str = String(text === null || text === undefined ? '' : text);
  return str.length > max ? str.slice(0, max - 1) + '…' : str;
}

/* ----------------------------------------------------------- 3. API ---- */
/** CSRF 头由 auth.js 提供（未启用登录时原样返回），保证所有 POST 都带上令牌。 */
function csrfHeaders(base) {
  return window.PentestAuth ? window.PentestAuth.csrfHeaders(base) : (base || {});
}
/** 失败响应 → Error；带上 status，供登录页区分 401（密码错）/ 423（尝试过多）。 */
function apiError(response, data) {
  const error = new Error((data && data.error) || `HTTP ${response.status} ${response.statusText || ''}`.trim());
  error.status = response.status;
  return error;
}
/** 401 → 立即回登录页；登录、创建管理员、改密码这些接口用 401 表达业务结果，交给表单处理。 */
function noteAuthStatus(response, path) {
  if (response.status === 401 && window.PentestAuth) window.PentestAuth.handleUnauthorized(path);
}
/** 统一 GET：错误响应 {"error":"..."} 抛该消息，超时抛超时消息。 */
async function apiGet(path) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
  try {
    const response = await fetch(API_BASE + path, { signal: controller.signal, headers: { Accept: 'application/json' } });
    const raw = await response.text();
    let data = null;
    try { data = raw ? JSON.parse(raw) : null; } catch (parseError) { data = null; }
    if (!response.ok) { noteAuthStatus(response, path); throw apiError(response, data); }
    if (data === null) throw new Error('响应不是合法 JSON');
    return data;
  } catch (error) {
    if (error && error.name === 'AbortError') throw new Error(`请求超时（${REQUEST_TIMEOUT / 1000}s）`);
    throw error;
  } finally { clearTimeout(timer); }
}
/** 统一 POST：JSON 请求体；headers 供注册凭据等场景附加，CSRF 令牌自动带上。
 *  timeoutMs 可选：模型连通性测试这类慢接口需要比默认 15 秒更长的预算。 */
async function apiPost(path, body, headers, timeoutMs) {
  const budget = Number(timeoutMs) > 0 ? Number(timeoutMs) : REQUEST_TIMEOUT;
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), budget);
  try {
    const response = await fetch(API_BASE + path, {
      method: 'POST', signal: controller.signal,
      headers: Object.assign({ Accept: 'application/json', 'Content-Type': 'application/json' }, csrfHeaders(headers)),
      body: JSON.stringify(body || {}),
    });
    const raw = await response.text();
    let data = null;
    try { data = raw ? JSON.parse(raw) : null; } catch (parseError) { data = null; }
    if (!response.ok) { noteAuthStatus(response, path); throw apiError(response, data); }
    if (data === null) throw new Error('响应不是合法 JSON');
    return data;
  } catch (error) {
    if (error && error.name === 'AbortError') throw new Error(`请求超时（${budget / 1000}s）`);
    throw error;
  } finally { clearTimeout(timer); }
}

/** 统一 POST 的流式版本：返回**原始** Response，由调用方自己读 body。
 *
 *  刻意不在这里读 body：流式响应的全部意义就是"边到边处理"，而 `apiPost` 里的
 *  `await response.text()` 会把流攒成整包——那样界面上一切正常，只是又变回了干等，
 *  而且问题极难发现（接口是流式的，用法不是）。
 *
 *  非 2xx 仍然走统一的错误转换：参数错误、过期登录这些发生在事件流开始之前，
 *  所以它们还是普通的 JSON 响应，调用方照常用同一套错误提示处理。
 *  `signal` 只用于"用户主动停止"与空闲看门狗，不设整轮超时——一轮对话可能跑几分钟。
 */
async function apiStream(path, body, signal) {
  const response = await fetch(API_BASE + path, {
    method: 'POST', signal,
    headers: Object.assign({ Accept: 'text/event-stream', 'Content-Type': 'application/json' },
      csrfHeaders()),
    body: JSON.stringify(body || {}),
  });
  if (!response.ok) {
    const raw = await response.text();
    let data = null;
    try { data = raw ? JSON.parse(raw) : null; } catch (parseError) { data = null; }
    noteAuthStatus(response, path);
    throw apiError(response, data);
  }
  return response;
}

/** GET 版的流：接上一条**已经在跑**的流（`GET /chat/stream`）。
 *
 *  只读、无副作用，所以不带 CSRF 头（分派器只对非 GET 校验它）；路径里的查询串
 *  由调用方拼好——这个函数只负责"发出去、非 2xx 转成统一错误、把流交回去"。
 */
async function apiStreamGet(path, signal) {
  const response = await fetch(API_BASE + path, {
    method: 'GET', signal, headers: { Accept: 'text/event-stream' },
  });
  if (!response.ok) {
    const raw = await response.text();
    let data = null;
    try { data = raw ? JSON.parse(raw) : null; } catch (parseError) { data = null; }
    noteAuthStatus(response, path);
    throw apiError(response, data);
  }
  return response;
}

/* ---------------------------------------------------------- 4. 状态 ---- */
const state = {
  eid: null,                                       // 当前 engagement 标识（漏洞/资产/报告的范围）
  engagementList: [],
  // GET /graph 的完整响应：**全部 engagement 合并**的链路视图。
  // 「探索链路」不再按 engagement 过滤（合并视图里每个节点自带 engagementId，
  // 归属没有丢），所以图和 eid 是两条独立的加载路径，不能共用一个字段。
  graph: null,
  // **不再有节点上限**。曾经有一个 `graphLimit`（默认 200）配一个下拉框，理由是
  // "全量 1207 个节点铺开是 2190 × 50184 px，适配窗口后是一条竖线"。那个理由在
  // 当初成立，现在不成立：缩放改成按图取上限之后，「适应窗口」能看全貌、滚轮能
  // 一路放到一个字一个字读，而"我要看全部"本来就是这个页签唯一的用途。
  // 留一个默认砍掉 80% 节点的窗口，只会让人以为链路本来就是断的。
  graphLoaded: false, graphInflight: null,
  graphLayoutKey: '',                              // 布局指纹：变了才知道"要不要提示有新节点"
  // 「探索链路」跟着心跳自动更新吗（页头那颗「自动刷新」芯片）。默认开：这一页唯一
  // 的用途是"看全部"，而等数据出现的方式原来只有手点一次「刷新链路」。
  graphAuto: true,
  // 链路范围（编排面）：`single` 只看当前目标，`all` 才把全部目标叠成一张。
  // 默认单目标——合并图在 20 多个目标时是一张几百个节点的网，它回答不了
  // 「这个目标现在走到哪一步」，而那才是这一页要回答的问题。
  graphScope: 'single',
  // 编排面板的数据：`GET /engagements/{eid}/orchestration` 原样留着。
  // 它与 `graph` 是两份而不是同一份：图回答「发现了什么」，它回答「接下来
  // 往哪儿走」，两者的失效时机不同（改一次状态只影响后者）。
  orchestration: null, orchestrationEid: '',
  // 面板里那条「刚才做了什么」的提示。它存在 state 而不是 DOM 里：面板每次
  // 都是整块重画，写进节点的文案活不过下一次重画。
  orchMessage: { text: '', kind: '' },
  // 上一次算初始缩放时**画布量到尺寸了没有**。0×0 的画布算出来的那个值不可信
  // （见 `BASE_SCALE` 那段），所以第一次量到尺寸时要再算一次——用状态记着它做过，
  // 而不是每次渲染都重算（那会把使用者刚调好的缩放丢掉）。
  graphScaleMeasured: false, graphMeasureRetry: 0, graphMeasureRect: null,
  nodeIndex: new Map(),                            // id → { kind, raw }，详情面板据此展示全部字段
  findings: [], findingGroups: [], assets: [], markdown: '',
  // 漏洞页的范围：空串 = 「全部目标」（跨目标分组看板），否则就是某个目标的 id。
  // 它**不是**第二个 `eid`：当前选中目标仍是唯一真相，这里只记"漏洞页要不要按它
  // 收窄"，所以它是 `selectEngagement` 的跟随者，不是并列的一份状态。
  findingsScope: '',
  // 「全部目标」视图的数据（`/findings` 一次拿回所有目标的分组）。与 `findings`
  // 并列而不是复用：两者可能同时在使用——左栏读合计，正文读当前 scope 那一份。
  findingsByTarget: [], findingsTotals: null,
  allFindingsLoaded: false, allFindingsInflight: null,
  // 报告页当前显示的是哪一份文档：`reportJob` 为空 = 本目标汇总，否则是那次任务。
  // 只存在内存里，**不进 URL**：刷新回到汇总是一个可预期的结果，而把作业号写进
  // 地址栏会让人以为"这篇报告可以被分享"——它绑定的是这台机器上的一个任务 id。
  // `reportDoc` 是**后端确认过**的那一份（卡头读它，不猜）。
  reportJob: '', reportDoc: null,
  // 上一次**真的取回来**的那篇文档的路径（`reportPath(...)` 的结果）。
  // `loaded.report` 只记得"取过一篇"，记不住是哪一篇；判据必须带上路径，
  // 详见 `loadReport`。
  reportPath: '',
  // 界面上**当前可见**的页签。空串 = 还不知道，由 `init()` 从界面读（`activeTabFromDom`）。
  // 这里**不写死字面量**：它写的是 `'graph'` 而 `index.html` 上标着 `active` 的是对话，
  // 于是"屏幕上是哪一块"与"状态里说是哪一块"从启动那一刻就不一致。代价不只是启动时
  // 多画/少画一张图——心跳按它决定起不起（`startPulseAuto` 的名单），而屏幕上是对话、
  // 状态里是链路，于是**侧边栏那两个块的心跳一次都没起过**。
  tab: '',
  // 当前正在跟随哪条任务的执行过程（null = 没在跟随）。它是**内存态**，
  // 刻意不进 URL：一条 SSE 长连接不该因为有人分享了地址而被别人接过去。
  jobLog: null,
  // 七个类型一个不能少（与 `KIND_ORDER` 同一组键）：漏掉哪个，那一类的节点就在
  // 启动那一秒被判定成「已关闭」，而芯片却是亮着的——图上一整类节点凭空消失，
  // 芯片点两下才回来。
  kindFilter: { begin: true, goal: true, hint: true, intent: true, fact: true, finding: true, digest: true },
  severityFilter: new Set(SEVERITY_ORDER),
  keyword: '', selectedNodeId: null, selectedAssetId: null,
  loaded: { findings: false, assets: false, report: false }, inflight: {},
  setup: null, executors: [], jobs: [],                      // 部署面板数据
  setupLoaded: false, executorsLoaded: false, jobsLoaded: false,
  authz: null, authzLoaded: false,                           // 第三道闸：GET /authz
  // 「外部工具」卡：清单（GET /tools）+ 操作队列的轮询状态。
  // `toolNote` 是操作结束后那一行说明；`toolSeenOps` 记住已报道过的终态，
  // 免得每次重画都把同一批旧记录再播报一遍。
  tools: null, toolsLoaded: false, toolsTimer: null, toolsPolling: false,
  toolConfirm: '', toolNote: '', toolSeenOps: null,
  // 折叠状态（分组与「最近操作」）。两者**默认都收起**：十几项平铺是 5 个分组标题
  // 加约 90 行正文（版本 / 渠道 / 占用 / 路径 / 用途）。
  // 记的是"哪几组是打开的"，不是"哪几组是关的"：空集正好就是默认值（全收起）。
  toolOpen: new Set(), toolOpsOpen: false,
  jobsTimer: null, jobsInflight: null,                       // 作业列表自动刷新定时器
  // 侧边栏两个读数块（目标 / 对话记录）的新鲜度：上一轮的指纹，以及它那个定时器。
  // `pulse` 存的是 `/pulse` 返回的那组字符串——**比对它**才知道要不要重拉列表。
  pulse: null, pulseTimer: null,
  // 上一轮心跳量的是**哪一条对话**的流量（`/pulse` 的 `sessionId`）。换对话时流量指纹
  // 可能两边都是空的（`"n=0 m=0"`），只看指纹判不出变化，而列表该整张换掉。
  // `null` = 还没量过，第一轮不算换。
  trafficSession: null,
  // 页面**手上这份目标列表**是在哪个指纹上拉下来的（见 `pumpPulse`）。与 `pulse:`
  // 分开：那个记的是"服务端上一轮说了什么"，这个记的是"我手上这份东西有多新"。
  // `null` = 不清楚（启动刚拉过、或上一次拉失败了）→ 下一轮补拉一次并把指纹记上。
  listStamps: null,
  // 下发方式与 AI 规划的当前提案。提案在生成时就已落库（"模型当时说了什么"
  // 事后要能查），但"勾了哪几条"是此刻的选择——刷新页面就该重来。把半途的
  // 勾选当成长久状态，只会让人分不清到底哪一次是真的。
  jobMode: 'manual', plan: null, planSelected: new Set(), planCommitted: new Map(),
};
/** 图的缩放平移与交互索引（只影响视图，不改后端 layout）。
 *
 *  `ceiling` 是**这张图**的缩放上限，每次重算初始缩放时一起算
 *  （见 `renderGraph`）。做成每张图一个值而不是一个全局常数，理由见 `BASE_SCALE`
 *  上面那段：常数会把合并视图那种"初始就该到 18 倍"的图夹在 600%，而 600% 是
 *  **基准**——一进来的落点，不是能到多远。
 *
 *  `dragScale` 与 `frame` 都是**拖动的代价**留下的（见 `measureDragScale` /
 *  `scheduleTransform`）：一次拖动只量一次比例、每帧只重画一次。
 *
 *  这里没有"缩放读数"了：百分比撤掉之后，`scale` 只在内部用来换算，不再有人把它
 *  写成字。
 */
const graphView = { scale: 1, tx: 0, ty: 0, ceiling: BASE_SCALE * BASE_HEADROOM,
                    dragging: false, moved: false, touched: false,
                    pressNodeId: null, lastX: 0, lastY: 0, pressX: 0, pressY: 0,
                    dragScale: { sx: 1, sy: 1 }, frame: 0,
                    // 单卡偏移：id → { dx, dy }（用户坐标）。与 scale/tx/ty 同级，都是**视图**
                    // 状态——布局一变就被后端覆盖，所以不能写进 layout 里。
                    pins: new Map(), dragNodeId: null, dragNode: null };
// 画布没有 DOM，所以"这一帧要画什么"全部记在这里：`nodes`/`edges` 是折叠之后真正
// 上画的集合，`boxes` 是它们的框（含 w/h，命中测试与拖卡都用它算几何），
// `hidden` 是被类型芯片关掉的那些 id，`hoverId` 是光标停在哪张卡上。
// 原来那些 `*-Items` 是 SVG 元素句柄，画布上一并不存在。
const graphIndex = { nodes: [], edges: [], boxes: null, neighbors: new Map(), liveIds: null,
                     hidden: new Set(), hoverId: null, keep: null, minimapSize: null };

/* ------------------------------------------------- 5. 错误条与状态栏 ---- */
let errorTimer = null;
/** 接口失败时显示错误条（不白屏），15 秒后自动收起。 */
function showError(message) {
  // 报错通路自己不许再抛。它是"出错时唯一还站着的那条路"：调用方在 catch 里调它
  // （auth.js 的启动监听、boot.js 的启动编排都在此列），它若在这里抛，就把
  // "一个模块没起来"放大成"整页没起来"，而真正的原因反而被这个二次异常盖住。
  const text = $('#error-text'), bar = $('#error-bar');
  if (!text || !bar) {
    if (window.console && window.console.error) window.console.error('[错误] ' + String(message));
    return;
  }
  text.textContent = String(message);
  bar.classList.remove('hidden');
  // 显示的这一帧就要是对的：观察器走 requestAnimationFrame，而它会被
  // 饿死（无头/后台标签实测整轮只有 6~8 帧），锚点于是落后一整个布局变化。
  // 报错通路不许再抛，所以裹在 try 里。
  try { syncNoticeAnchor(); } catch (ignored) { /* 量不出位置不该盖住真正的错误 */ }
  clearTimeout(errorTimer);
  errorTimer = setTimeout(() => { if (bar) bar.classList.add('hidden'); }, 15000);
}
function hideError() {
  clearTimeout(errorTimer);
  const bar = $('#error-bar');
  if (bar) bar.classList.add('hidden');
}
/* 通知栈的锚点：「显示在输入框上面」。
 * 位置只能测出来——卡片上沿取决于输入框行数、目录是否展开、有没有选项行，
 * CSS 里没有任何一个变量能表达"距视口底还有多远"。所以这里写 `--notice-bottom`，
 * 样式只负责把它用在 `bottom` 上。
 * 观察器挂在输入卡片上：它自己长高、变矮（目录展开/收起、textarea 换行、
 * 选项行出现）都会触发重算，不需要哪个调用点记得"改完布局再调一次"。 */
let noticeObserver = null;
let noticeMutations = null;
let noticePending = false;
let noticeBottom = '';
function syncNoticeAnchor() {
  const stack = $('#notice-stack');
  if (!stack) return;
  const compose = $('.chat-compose');
  const card = $('.chat-field');            /* 画出来的卡片：贴的是它，不是布局盒 */
  // `getClientRects()` 对任何不可见状态都返回空（display:none、被换走的页签、
  // 还没登录的整页），比"高度是否大于 0"更稳——卡片完全可能是 0 高的。
  const shown = compose && compose.getClientRects().length > 0;
  if (!shown) {
    // 没有输入卡片：回到页面顶部。通知不能因为换了个页签就消失。
    if (stack.dataset.anchor !== 'top') stack.dataset.anchor = 'top';
    if (noticeBottom !== '') {
      stack.style.removeProperty('--notice-bottom');
      noticeBottom = '';
    }
    return;
  }
  // 参照只有**一个**：画出来的那张卡片（`.chat-field`）的上沿。
  //
  // ⚠️ 三条都不能用 `.chat-compose`：
  //   1. 卡片是**叠进**记录区的（`.chat-field` 有
  //      `margin-top: calc(-1 * var(--chat-overlap))`），所以 `.chat-compose` 的
  //      上沿是**布局盒**的上沿，比真正画出来的圆角卡片低一整段。实测
  //      compose.top 740 而 card.top 626——按 compose 贴会让浮层落进卡片内部；
  //   2. `.chat-compose` 还包着卡片上方会临时长出来的几行（排队中的消息、
  //      模型告警、自动继续那一行）。按"最高可见边缘"贴，浮层就被它们顶上去：
  //      实测排队 1 条 → 离卡片 47px，4 条 → 121px，自动继续那行出现 → 254px，
  //      坞展开 → 82px。这正是用户说的"不贴着输入框"；
  //   3. 用户的原话是「准确说弹窗应该是浮层贴」——它属于这张卡片，就该贴着卡片上沿。
  //
  // 代价是：上面那几种行出现时，浮层会盖住它们的最后一行。这个代价是**选定的**，
  // 不是疏忽——浮层 `pointer-events: none`（空白处不拦点击，只有错误条自己可点）、
  // 15 秒自动收起、错误条右侧有唯一的关闭按钮；而"离输入框 200 多像素"会让人
  // 根本认不出这条提示属于哪张卡片。要改回"不遮挡"，把这几行加回候选名单即可。
  const top = (card || compose).getBoundingClientRect().top;
  /* 浮层底边与卡片上沿的空隙：8 = 看得出是"贴着的一层皮"，又不至于糊成一张卡。
     想贴死写 0；想更远改这一个数（其余位置都从它推出来）。 */
  const gap = 8;
  // 那一片比视口还高时（记录区被压到底），把浮层夹在视口里，别推到屏幕外面去。
  const bottom = Math.round(clamp(window.innerHeight - top + gap, gap, window.innerHeight - 64)) + 'px';
  if (stack.dataset.anchor !== 'composer') stack.dataset.anchor = 'composer';
  if (bottom === noticeBottom) return;              /* 位置没变就别写样式，省一次重排 */
  noticeBottom = bottom;
  stack.style.setProperty('--notice-bottom', bottom);
}
/* 一帧最多重算一次。初始化时卡片还在异步长出来（状态、技能、选项行），
 * 它的**尺寸可能没变而位置变了**——只挂 ResizeObserver 会漏掉这种，
 * 所以面板子树的结构/属性变化也接上。 */
function scheduleNoticeAnchor() {
  if (noticePending) return;
  noticePending = true;
  const run = () => {
    if (!noticePending) return;   /* 另一条路已经跑过了 */
    noticePending = false;
    syncNoticeAnchor();
    syncChatTail();
  };
  requestAnimationFrame(run);
  /* rAF 会被饿死：后台标签根本不画帧，无头环境也要靠"下一帧"才轮到回调
     （实测一整轮 30 秒只跑了 6~8 帧，于是每次布局变化后锚点都停在上一帧的位置，
     排队/告警那几行出现时浮层就差出几十到两百多像素）。补一条定时器兜底：
     谁先到谁执行，另一个看到 noticePending 已落回 false 就直接返回。
     32ms 大约两帧：肉眼看不出来，但不会被饿死。 */
  setTimeout(run, 32);
}
/* ------------------------------------------------- 记录区末尾与卡片的距离 ---- */
/** 最后一格内容与输入卡片上沿之间的空隙，**量出来**，不靠算式推。
 *
 *  背景（这是用户第三次说"离输入框太远"，前两次修的是浮层，这次是对话本身）：
 *
 *  * 设计上的不变式写在 style.css 的 `.chat-log { padding-bottom }` 那一段里：
 *    `--chat-overlap`（= 卡片高度）同时供两侧用——记录区底边内边距与卡片的负
 *    外边距，于是"滚到底时最后一行字停在卡片上沿之上 --chat-fade"。那条不变式
 *    的前提是**记录区盒底恰好等于卡片上沿 + 叠进深度**。
 *  * 前提被 `.chat-compose` 里那几行打破了：卡片顶上还压着 compose 自己的上内边距
 *    （12px）与 flex `gap`（16px），以及会临时长出来的排队行 / 模型告警 / 自动继续
 *    行。它们把卡片往下推，而 `--chat-overlap` 一个字都没变。
 *  * 实测（1452×900，详情栏收起，记录区 826×602）：记录区盒底到卡片上沿只有
 *    148px，而内边距按 176+12=188 写 —— 于是滚到底时最后一行离卡片上沿 **40px**
 *    （设计意图是 12px）。审查卡还带 12px 下边距，卡片一出现就是 **52px**；若末尾
 *    再跟一个空气泡（26px）就是 **90px** —— 用户截图里那条空白带正是它。
 *  * 算不出来就别硬算：排队行的条数、告警行在不在一行里，CSS 都看不见。所以这里
 *    量一次真实矩形，把内边距写成"真实叠进深度 + 渐隐长度"。量的是**两个已画出来
 *    的盒子**，与上面所有临时行无关。
 *
 *  与 `syncNoticeAnchor` 的关系：两者读同一对矩形（`.chat-log` 盒底、`.chat-field`
 *  上沿），触发源也共用那条 rAF + 32ms 的调度，不再另起一套观察器。
 */
function chatTailFade() {
  const main = $('.chat-main');
  const raw = main ? getComputedStyle(main).getPropertyValue('--chat-fade') : '';
  const value = parseFloat(raw);
  return Number.isFinite(value) && value >= 0 ? value : 12;
}
let chatTailValue = '';
function syncChatTail() {
  const log = $('#chat-log');
  const card = $('.chat-field');
  if (!log || !card) return;
  // 换页签/未登录/首帧时这对盒子可能还没画出来：`getClientRects()` 对任何不可见
  // 状态都返回空，比量出来的 0 更可靠（0 会被当成"叠进 0px"，把内边距写成 12px）。
  if (!log.getClientRects().length || !card.getClientRects().length) return;
  /* t93：坞（可用能力 / 技能手册）展开时是**卡片上沿的一张浮层**（脱出文档流，
     见 style.css 的 `.chat-quick`）。它盖在记录区最下面那一段上，所以末尾的内边距
     要量到**它**的上沿，而不是卡片的上沿——否则会有一行字卡在坞底下。
     取两者中更高的那一个（坞收起时 rect 为空，直接跳过）。 */
  let anchor = card.getBoundingClientRect().top;
  const dock = $('#chat-quick');
  if (dock && dock.getClientRects().length) {
    const dockTop = dock.getBoundingClientRect().top;
    if (dockTop < anchor) anchor = dockTop;
  }
  /* m18801：审查卡也是卡片上沿的一张浮层（`.chat-review`，同一套 `bottom: 100%`
     几何），它比坞更高，所以末尾要让到**它的**上沿去。不纳入这里的话，待批时
     记录区最后几行会压在卡片底下 —— 而"卡片占着位置却看不见最后一句"正是这次
     要修的症状之一，只是换了个方向。 */
  const reviews = $('#chat-review-float');
  if (reviews && reviews.getClientRects().length) {
    const reviewTop = reviews.getBoundingClientRect().top;
    if (reviewTop < anchor) anchor = reviewTop;
  }
  const overlap = log.getBoundingClientRect().bottom - anchor;
  /* 负值不是异常，而且**是那几行可见时的常态**：排队列表与告警行各自用
     `margin/padding-bottom: var(--chat-overlap)` 给自己留出与卡片同高的空档
     （见 `.chat-queue` 与 `.chat-notice:not(:empty)`）——它们一出现，整条
     `.chat-compose` 就长出 400 多像素，卡片跟着掉到记录区**下面**。
     这时没有可补偿的叠进深度，末尾只留渐隐那一段；再按 CSS 那条
     `padding-bottom: calc(overlap + fade)` 留 400 多像素，就等于在可见的排队行上方
     又白留一整屏（改之前正是这个症状）。不夹到 0 的话会写进一个负数内边距
     （无效声明）或留下上一轮的过大值。 */
  const pad = Math.max(0, Math.round(Math.max(0, overlap) + chatTailFade()));
  const value = pad + 'px';
  if (value === chatTailValue) return;             /* 没变就别写样式，省一次重排 */
  chatTailValue = value;
  log.style.paddingBottom = value;
}
function wireNoticeAnchor() {
  syncNoticeAnchor();
  syncChatTail();                                  /* 记录区末尾与卡片的距离：量一次 */
  scheduleNoticeAnchor();                           /* 首帧布局未必是最终的，下一帧再对齐一次 */
  window.addEventListener('resize', syncNoticeAnchor);
  if (window.ResizeObserver) {
    if (noticeObserver) noticeObserver.disconnect();
    noticeObserver = new ResizeObserver(scheduleNoticeAnchor);
    // 卡片、记录区、坞：任何一个变高变矮都可能把卡片推上推下。
    ['.chat-compose', '#chat-log', '#chat-quick', '#chat-review-float'].forEach((selector) => {
      const node = $(selector);
      if (node) noticeObserver.observe(node);
    });
  }
  if (window.MutationObserver) {
    if (noticeMutations) noticeMutations.disconnect();
    const panel = $('#panel-dsh') || document.body;
    // 消息进记录区、坞展开、选项行出现——这些都不一定改变被观察元素的尺寸，
    // 但都会让卡片上沿移动，所以结构/属性变化也算一次"可能要对齐"。
    noticeMutations = new MutationObserver(scheduleNoticeAnchor);
    noticeMutations.observe(panel, {
      childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'hidden', 'style'],
    });
  }
}
/** health + executors → 顶栏执行节点在线状态。 */
async function loadStatus() {
  try {
    const [health, executorData] = await Promise.all([apiGet('/health'), apiGet('/executors')]);
    renderExecutors(asArray(executorData.executors), health);
  } catch (error) {
    $('#stat-executor-text').textContent = '执行节点 不可用';
    $('#stat-executor-dot').className = 'dot offline';
    showError('状态接口失败：' + error.message);
  }
}
function renderExecutors(executors, health) {
  const online = executors.filter(executorIsOnline);
  const pending = health && health.pendingJobs !== undefined ? ` · 待处理 ${health.pendingJobs}` : '';
  const caps = online.flatMap((item) => asArray(item.capabilities)).join('/');
  // title 里那一行也要中文：它原先印 item.status 原值，于是鼠标停在顶栏上读到的是
  // fixture-node [online] 负载 0，而同一颗节点的徽章上写的是「在线」。
  const lines = executors.map((item) => `${item.name || item.id || '?'} [${executorStatusWord(item)}] 负载 ${item.load ?? 0}`);
  $('#stat-executor-dot').className = 'dot ' + (online.length ? 'online' : 'offline');
  $('#stat-executor-text').textContent = `执行节点 ${online.length}/${executors.length} 在线${pending}`;
  $('#stat-executors').title = (lines.length ? lines.join('\n') : '无执行节点')
    + (caps ? `\n能力：${caps}` : '') + (health && health.version ? `\n后端版本：${health.version}` : '');
}
/** 顶栏节点/边计数 + 截断横幅：数据来自**全局合并视图**（`state.graph`）。
 *
 *  链路不再按 engagement 过滤，所以这两个数字是**全部目标加起来**的。把它们
 *  留在"当前目标"的口径上，会让顶栏写着 28 而图上画着 208 个节点——一个看过
 *  去像渲染错误、实际是两套口径的数字。
 */
function renderGraphStats(view) {
  const totals = (view && view.totals) || {}, truncated = (view && view.truncated) || {};
  const dropped = Number(truncated.dropped) || 0, refused = Number(truncated.refused) || 0;
  const banner = $('#truncate-banner');
  $('#stat-nodes').textContent = `节点 ${Number(totals.nodes) || 0}`;
  $('#stat-edges').textContent = `边 ${Number(totals.edges) || 0}`;
  $('#stat-truncated').textContent = `丢弃 ${dropped} · 拒绝 ${refused}`;
  $('#stat-truncated').classList.toggle('hidden', dropped === 0 && refused === 0);
  banner.classList.toggle('hidden', dropped === 0);
  if (dropped > 0) {
    banner.textContent = `全部 ${Number(totals.engagements) || 0} 个目标的链路共 `
      + `${Number(totals.nodes) || 0} 个节点，图仅展示最近 ${Number(totals.shown) || 0} 个`
      + `（窗口外丢弃 ${dropped} 条）——改「节点上限」可以看得更多`;
    // 显示的这一帧就要是对的：锚点观察器走 requestAnimationFrame，而它会被
    // 饿死（无头/后台标签实测整轮只有 6~8 帧），于是落后一整个布局变化。
    // 这条通路不许再抛，所以裹在 try 里。
    try { syncNoticeAnchor(); } catch (ignored) { /* 量不出位置不该盖住真正的错误 */ }
  }
}
/** 漏洞/资产徽章：这两个计数由**当前选中的 engagement** 决定。
 *
 *  栏脚那颗只读的范围读数（`#stat-engagement`）已经撤掉：范围的名字由对话侧边栏
 *  那块给，而且给得更具体（扫了哪些地址、多少条任务、多少条漏洞）。这里只剩导航
 *  徽章——它们挂在页签上，是"哪一页有新东西"的唯一读数。
 */
function renderScopeStats(eid) {
  const meta = (state.engagementList || []).find((item) => item.id === eid) || null;
  const counts = (meta && meta.counts) || {};
  // 计数为 0 时留空而不是写 "0"：.tab-badge:empty 才会隐藏徽章（写 0 会让 :empty 不成立）
  const setBadge = (selector, value) => {
    const node = $(selector);
    if (node) node.textContent = Number(value) > 0 ? String(Number(value)) : '';
  };
  // 「全部目标」时徽章是**跨目标合计**：它挂在页签上，回答的是"漏洞页现在有多少条"，
  // 而这一页此刻展示的正是所有目标的合计。目标列表还没拉到（counts 全空）时退回
  // 列表自带的条数之和，保证启动那一秒不是空白。
  setBadge('#badge-findings', state.findingsScope === '' ? findingsTotalCount() : counts.findings);
  setBadge('#badge-assets', counts.assets);
}
/** 漏洞页「全部目标」时的徽章数：优先读服务端的合计，没取到时按目标列表里每条的
 *  `counts.findings` 相加（启动那一秒聚合快照还没回来，徽章不该是空的）。
 */
function findingsTotalCount() {
  if (state.findingsTotals) return Number(state.findingsTotals.total) || 0;
  return (state.engagementList || []).reduce(
    (sum, item) => sum + Number((item.counts || {}).findings || 0), 0);
}

/* -------------------------------------------------------- 6. 链路图 ---- */
/** layout 节点补齐坐标尺寸，返回绘线所需的中心点。 */
function nodeBox(node) {
  const x = Number(node.x) || 0, y = Number(node.y) || 0, w = Number(node.w) || 220, h = Number(node.h) || 60;
  return { x, y, w, h, cx: x + w / 2, cy: y + h / 2 };
}
/** 视图里的全部 goal。合并视图是数组（每个 engagement 一个），单 engagement 视图是单值。 */
function goalsOf(view) {
  if (!view) return [];
  const list = asArray(view.goals).filter(Boolean);
  if (list.length) return list;
  return view.goal && view.goal.id ? [view.goal] : [];
}
/** id → 原始对象 索引，供详情面板展示全部字段。 */
function buildNodeIndex(view) {
  const index = new Map(), groups = (view && view.nodes) || {};
  // 只读 `goal` 的话，合并图里除第一个之外的 goal 点开都是"当前数据中找不到节点"——
  // 22 个目标里有 21 个是这种情况，而它们恰好是整张图最上面那一列。
  goalsOf(view).forEach((raw) => { if (raw && raw.id) index.set(raw.id, { kind: 'goal', raw }); });
  [['intents', 'intent'], ['facts', 'fact'], ['findings', 'finding'], ['digests', 'digest'], ['assets', 'asset']].forEach(([key, kind]) =>
    asArray(groups[key]).forEach((raw) => { if (raw && raw.id) index.set(raw.id, { kind, raw }); }));
  return index;
}
/** 压缩折叠：被「压缩」吸收掉的记录不再单画，它的边改挂到那张压缩卡上。
 *
 *  为什么折叠：压缩存在的意义就是「这些细节我看过了，结论在这一段里」。不折叠，
 *  压缩卡只是在链路右边多出一列孤岛——它吸收的十几条事实照样铺在链路里，读的人
 *  还是得一条条读，而压缩本身反而成了噪音。折叠之后，那个位置只剩一句结论，
 *  想看细节点开它（详情面板列成员，照 ARTEX 的抽屉）。
 *
 *  三条规矩，都照 ARTEX 的 `collapseDigestGraph`：
 *
 *  * 只留 `state=active` 的压缩——被取代的那一代整个丢掉，它的成员回到链路里
 *    （否则它们会变成一群**没有归属**的孤儿：压缩没了，谁来认领这些事实）；
 *  * 成员收走之后，原本连成员的边改挂到压缩上，自环丢掉、同向同类型的去重；
 *  * 折叠之后若「产出 A→B」与「意图链 B→A」成了同一件事的两面，丢掉 `derived_from`
 *    ——背着箭头画一条反方向的线，读起来像两件事。
 *
 *  折叠只在**画布**上发生：`state.graph.edges` 与 `layout.edges` 一个字不动，
 *  详情面板与被收走的成员照样查得到，离线 SVG 也照旧画全。 */
function collapseDigests(nodes, edges) {
  const active = new Set();
  nodes.forEach((node) => {
    if (node.kind === 'digest' && (node.state || 'active') === 'active') active.add(node.id);
  });
  if (!active.size) return { nodes, edges: asArray(edges) };
  const member = new Map();
  asArray(edges).forEach((edge) => {
    if (edge.kind === 'covers' && active.has(edge.source)) member.set(edge.target, edge.source);
  });
  const kept = nodes.filter((node) => (node.kind === 'digest'
    ? active.has(node.id)                                  // 被取代的压缩整个不画
    : !member.has(node.id)));                              // 被吸收的记录画进压缩里
  // 全都收走就说明这张图里只有压缩与它吸收的东西，收完只剩孤零零几张压缩卡——
  // 那时宁可摊开：空图比折叠图更难读。
  if (!kept.length) return { nodes, edges: asArray(edges) };
  const moved = (id) => member.get(id) || id;
  const seen = new Set(), produced = new Set(), out = [];
  asArray(edges).forEach((edge) => {
    if (edge.kind === 'covers') return;                    // 覆盖是结构，不画
    const source = moved(edge.source), target = moved(edge.target);
    if (source === target) return;                         // 自环：压缩吸收自己的成员
    const key = source + '|' + edge.kind + '|' + target;
    if (seen.has(key)) return;
    seen.add(key);
    if (edge.kind === 'yields') produced.add(target + '|' + source);
    out.push(Object.assign({}, edge, { source, target }));
  });
  return {
    nodes: kept,
    edges: out.filter((edge) => !(edge.kind === 'derived_from'
      && produced.has(edge.source + '|' + edge.target))),
  };
}
/** 单卡偏移（用户坐标）。拖开的卡不该在心跳重画之后自己弹回去。 */
function pinOffset(id) {
  const pin = graphView.pins.get(id);
  return pin ? { dx: Number(pin.dx) || 0, dy: Number(pin.dy) || 0 } : { dx: 0, dy: 0 };
}
function boxWithPin(box, id) {
  if (!box) return null;
  const pin = pinOffset(id);
  const x = box.x + pin.dx, y = box.y + pin.dy;
  return { x, y, w: box.w, h: box.h, cx: x + box.w / 2, cy: y + box.h / 2 };
}
/** 拖卡：偏移已经由 pointermove 写进 `graphView.pins`，这里只负责把画面跟上。
 *
 *  旧版按 id 找到那个 `<g>` 改 `transform`、再只重画它那几条边（350 张卡时整张
 *  重绘约 40 ms）。画布没有"某个节点"这个对象可改——它是每帧现画的一层像素——
 *  所以这里退回整张重绘。代价由 `scheduleTransform` 那一层兜住：一帧最多一次。 */
function applyPin(id) {
  if (!id) return;
  paintGraph();
}
function clearPins() {
  graphView.pins.clear();
  paintGraph();
}
/** 画边：坐标全部来自后端 layout。
 *
 *  只画 `EDGE_KINDS` 里那四种推进关系。`covers` / `parent` 有数据但不画：压缩节点
 *  吸收了十几条记录，每条一条虚线，画布就从一条链变成一张网，而这两条关系在详情
 *  面板里读得比在图上清楚（它们是「归属」，不是「推进到哪一步」）。
 *
 *  几何与离线导出的 SVG 是**同一份**：都是从源卡右缘中点连到目标卡左缘中点的三次
 *  贝塞尔，回边（目标在源左边）改走两张卡的下缘中点。`d` 字段留给那段字符串对照，
 *  画布这边用拆开的四个端点与两个控制点。 */
function edgeGeometry(from, to) {
  const back = to.x + to.w / 2 < from.x + from.w / 2;
  let x1, y1, x2, y2;
  if (back) {
    x1 = from.x + from.w / 2; y1 = from.y + from.h;
    x2 = to.x + to.w / 2; y2 = to.y + to.h;
  } else {
    x1 = from.x + from.w; y1 = from.y + from.h / 2;
    x2 = to.x; y2 = to.y + to.h / 2;
  }
  const mid = back ? (x1 + x2) / 2 : x1 + (x2 - x1) / 2;
  return {
    x1, y1, x2, y2, cx1: mid, cy1: y1, cx2: mid, cy2: y2,
    d: "M " + x1 + " " + y1 + " C " + mid + " " + y1 + ", " + mid + " " + y2 + ", " + x2 + " " + y2,
    mx: mid, my: (y1 + y2) / 2,
  };
}
/* ---- 画布取色：主题令牌是唯一色源 ----------------------------------------
 *
 *  画布没法"继承样式"：`ctx.strokeStyle` 只吃字符串。所以每次重画先把需要的令牌
 *  读成一份快照（`graphPalette`），绘制函数只认快照里的名字，不再碰 CSS。
 *
 *  快照带一个 probe：`--kind-goal` 的取值 + 当前 `data-theme`。主题一换，probe 就变，
 *  下一次重画自动重建——不必让主题脚本反过来通知画布。`--kind-goal` 在五套主题里
 *  各不相同，所以它够用；只比 `data-theme` 会漏掉"在设置页里改令牌"那种情形。 */
const GRAPH_STATES = ['origin', 'confirmed', 'done', 'exhausted', 'met', 'active', 'superseded', 'blocked', 'pending'];
const GRAPH_TONES = ['red', 'amber', 'slate'];
const EDGE_TOKEN = { spawns: 'spawns', derived_from: 'derived', yields: 'yields', proves: 'proves', parent: 'parent', covers: 'covers' };
let graphPaletteCache = null;
/** 一张画布需要的全部颜色与字体，按主题缓存。 */
function graphPalette() {
  const root = document.documentElement, styles = window.getComputedStyle(root);
  const read = (name) => String(styles.getPropertyValue(name) || '').trim();
  const probe = read('--kind-goal') + '|' + (root.dataset ? root.dataset.theme : '') + '|' + read('--bg');
  if (graphPaletteCache && graphPaletteCache.probe === probe) return graphPaletteCache;
  const surface = $('#graph-surface');
  // `--sans` 里套着 `var(--sans-system)`，`getPropertyValue` 拿到的是原文，喂不进
  // `ctx.font`。所以字体从**元素的计算样式**取：CSS 给 `.graph-surface` 声明了
  // `font: var(--fs-small)/1.35 var(--sans)`，算完就是一条可用的字体栈。
  const surfaceFont = surface ? window.getComputedStyle(surface).fontFamily : '';
  const palette = {
    probe,
    bg: read('--bg'), panel: read('--panel-2'), border: read('--border'), borderStrong: read('--border-strong'),
    text: read('--text'), muted: read('--muted'), dim: read('--text-dim'),
    accent: read('--accent'), accentSoft: read('--accent-soft'),
    severity: { critical: read('--sev-critical'), high: read('--sev-high'), medium: read('--sev-medium'), low: read('--sev-low'), info: read('--sev-info') },
    kind: {}, edge: {}, stateBg: {}, stateFg: {}, prioBg: {}, prioFg: {},
    fontSans: surfaceFont || 'sans-serif',
    fontMono: read('--mono') || 'monospace',
    fsCaption: parseFloat(read('--fs-caption')) || 12,
    fsSmall: parseFloat(read('--fs-small')) || 13,
    hl: read('--hl-filter') || '',
    hlStrong: read('--hl-filter-strong') || '',
  };
  ['begin', 'goal', 'hint', 'intent', 'fact', 'finding', 'digest', 'asset'].forEach((kind) => {
    palette.kind[kind] = read('--kind-' + kind);
  });
  Object.keys(EDGE_TOKEN).forEach((kind) => {
    palette.edge[kind] = read('--edge-' + EDGE_TOKEN[kind]);
  });
  GRAPH_STATES.forEach((name) => {
    palette.stateBg[name] = read('--state-' + name + '-bg');
    palette.stateFg[name] = read('--state-' + name + '-fg');
  });
  GRAPH_TONES.forEach((tone) => {
    palette.prioBg[tone] = read('--prio-' + tone + '-bg');
    palette.prioFg[tone] = read('--prio-' + tone + '-fg');
  });
  graphPaletteCache = palette;
  return palette;
}
/* ---- 画布的坐标系 --------------------------------------------------------
 *
 *  布局坐标（用户单位）→ 屏幕：`screen = fit * (scale * u + t)`。`fit` 是"整幅塞进
 *  画布"的等比系数（`preserveAspectRatio="xMinYMin meet"` 那件事，现在由这里算），
 *  `scale` 是基准倍数（600% = 6）。三个量都在 `graphView` 里，与改造前**完全一样**
 *  ——视图数学没有跟着绘制方式改，改的只是把 `transform` 写进 SVG 还是写进 2D 上下文。
 *
 *  拖动之所以"跟不上鼠标"，根子就在这层换算：`measureDragScale` 只除了 `fit`，漏了
 *  `scale`，于是屏幕上挪 1 像素被当成 6 个用户单位。这里是**同一个 `k = fit * scale`**
 *  同时喂给绘制、命中测试、滚轮锚点与拖动。 */
/** 画布元素与它的量测。量不到（页签还没露出来）时返回 null。 */
function surfaceMetrics() {
  const surface = $('#graph-surface'), canvas = $('#graph-canvas');
  if (!surface || !canvas) return null;
  const rect = surface.getBoundingClientRect();
  const width = Number(surface.dataset.width) || 0, height = Number(surface.dataset.height) || 0;
  if (!rect.width || !rect.height || !width || !height) return null;
  const fit = Math.min(rect.width / width, rect.height / height);
  if (!fit) return null;
  return { surface, rect, width, height, fit, k: fit * graphView.scale };
}
/** 整幅塞进画布的系数。init() 那一刻画布常是 0×0，返回 0 表示"量不到"。 */
function fitScale() {
  const surface = $('#graph-surface'), canvas = $('#graph-canvas');
  if (!surface || !canvas) return 0;
  const width = Number(surface.dataset.width) || 0, height = Number(surface.dataset.height) || 0;
  const rect = canvas.getBoundingClientRect();
  if (!width || !height || !rect.width || !rect.height) return 0;
  return Math.min(rect.width / width, rect.height / height);
}
/** 冻结「屏幕像素 → 用户坐标」的比例：**一次拖动只量一次**。
 *
 *  屏幕位移 = `fit * scale` × 用户位移，所以反过来要乘它的**倒数**。原来这里只除了
 *  `fit`（`1 / fit`），漏掉 `scale`——默认 600% 时屏幕每挪 1 像素被换算成 6 个用户
 *  单位，图比光标快六倍，症状就是"拖动移动太快、不跟鼠标"。
 *
 *  量不到（画布还没露出来）时返回 `null`，这一次拖动整段不平移。 */
function measureDragScale() {
  const metrics = surfaceMetrics();
  if (!metrics || !metrics.k) return null;
  // 等比缩放下两个方向是同一把尺子。
  return { sx: 1 / metrics.k, sy: 1 / metrics.k };
}
/** 客户端像素 → 用户坐标。滚轮锚点用它，`k` 与拖动同一把尺子。 */
function toUserSpace(clientX, clientY) {
  const metrics = surfaceMetrics();
  if (!metrics) return null;
  const scale = graphView.scale || 1;
  return {
    x: (clientX - metrics.rect.left) / metrics.k - graphView.tx / scale,
    y: (clientY - metrics.rect.top) / metrics.k - graphView.ty / scale,
  };
}
/** 命中测试：屏幕点 → 用户坐标 → 落在哪张卡上（后画的在上，所以倒着找）。 */
function hitNode(clientX, clientY) {
  const point = toUserSpace(clientX, clientY);
  const boxes = graphIndex.boxes;
  if (!point || !boxes) return null;
  const nodes = graphIndex.nodes || [], hidden = graphIndex.hidden || new Set();
  for (let i = nodes.length - 1; i >= 0; i -= 1) {
    const node = nodes[i];
    if (hidden.has(node.id)) continue;
    const box = boxWithPin(boxes.get(node.id), node.id);
    if (!box) continue;
    if (point.x >= box.x && point.x <= box.x + box.w && point.y >= box.y && point.y <= box.y + box.h) return node.id;
  }
  return null;
}
/** 验收探针用的坐标表：每张可见卡的中心落在**客户端坐标**上。
 *
 *  画布上没有"某张卡"这个元素可以 `querySelector`，点选探针只能按坐标点。换算与
 *  `toUserSpace` 互逆（`clientX = rect.left + k * u + fit * tx`），几何复用 `boxWithPin`，
 *  所以折叠掉的、被类型芯片滤掉的卡不会出现在这张表里——探针点不到看不见的东西。 */
function graphNodePoints() {
  const metrics = surfaceMetrics();
  const boxes = graphIndex.boxes;
  if (!metrics || !boxes) return [];
  const rect = metrics.rect, k = metrics.k;
  const hidden = graphIndex.hidden || new Set();
  const out = [];
  (graphIndex.nodes || []).forEach((node) => {
    if (hidden.has(node.id)) return;
    const box = boxWithPin(boxes.get(node.id), node.id);
    if (!box) return;
    out.push({
      id: node.id,
      x: rect.left + k * (box.x + box.w / 2) + metrics.fit * graphView.tx,
      y: rect.top + k * (box.y + box.h / 2) + metrics.fit * graphView.ty,
    });
  });
  return out;
}
/** 缩放：以 (ux, uy) 这个**用户坐标**为锚点。
 *
 *  `screen = fit * (s*u + t)` 里锚点要不动：`s'*u + t' = s*u + t`，于是
 *  `t' = u - (s'/s) * (u - t)`。与改造前的公式逐字相同——换绘制方式不该改手感。 */
function zoomAt(ux, uy, factor) {
  const next = clamp(graphView.scale * factor, MIN_SCALE, graphView.ceiling);
  if (!next || next === graphView.scale) return;
  const ratio = next / graphView.scale;
  graphView.tx = ux - ratio * (ux - graphView.tx);
  graphView.ty = uy - ratio * (uy - graphView.ty);
  graphView.scale = next;
  graphView.touched = true;
  paintGraph();
}
function zoomByFactor(factor) {
  const metrics = surfaceMetrics();
  if (!metrics) return;
  const rect = metrics.rect;
  const point = toUserSpace(rect.left + rect.width / 2, rect.top + rect.height / 2);
  if (point) zoomAt(point.x, point.y, factor);
}
/** 适应窗口：整幅可见 = `scale = 1`（`fit` 就是"整幅塞进画布"）。 */
function resetView() {
  // 卡片偏移也一起清：那颗按钮的语义是"把画面交回布局"，留下几张被拖出去的卡，
  // 看上去就是"适应窗口没生效"。
  graphView.pins.clear();
  graphView.scale = 1;
  graphView.tx = 0;
  graphView.ty = 0;
  // 「适应窗口」= 主动接受重新适配，所以把"人动过视角"这条标记清掉：之后到来的
  // 新节点可以再自动适配一次。保留它的话，点过适应窗口的人反而再也看不到自动跟随。
  graphView.touched = false;
  paintGraph();
}
/** 首次摆正：把视图的左上角对到布局内容的左上角。
 *
 *  为什么不能只管 `tx = ty = 0`：原点在**布局的**左上角，而卡片不在原点上——
 *  `MARGIN` 让每列都从 40 起画，同一层的卡片又按列居中排，于是"所有卡的最小 x"
 *  与"最小 y"根本不在同一张卡上，取那个角会落在空格里。真正该对齐的是**第一列的
 *  顶卡**。
 *
 *  上边距还要让开左上角那块图例：它是一张压在画布上的卡（`pointer-events: none`），
 *  默认 600% 下第一张卡本来就在它右边，但列宽与窗口尺寸一变就可能被压住。
 *  让开的方式是**求交**：图例偏在左边，若只让它底边以下，整幅就被下推一整条横带，
 *  白扔一屏高度；所以把顶卡投到屏幕坐标，只在横向真的压上时才补那一段。 */
function alignViewToStart(boxes) {
  const metrics = surfaceMetrics();
  if (!metrics || !boxes || !boxes.size) return;
  const rect = metrics.rect, fit = metrics.fit;
  let minX = Infinity;
  boxes.forEach((box) => { minX = Math.min(minX, box.x); });
  let minY = Infinity;
  let topBox = null;
  boxes.forEach((box) => {
    if (Math.abs(box.x - minX) < 1 && box.y < minY) { minY = box.y; topBox = box; }
  });
  if (!isFinite(minX) || !isFinite(minY) || !topBox) return;
  const padX = 16, padY = 16;
  graphView.tx = padX / fit - minX * graphView.scale;
  graphView.ty = padY / fit - minY * graphView.scale;
  // 让开图例：**不能只看图例的底边**。图例是左上角一块宽 180 左右的卡，第一列的顶卡
  // 本来就贴着左边，窗口一窄，卡的左上角就钻到图例右半边底下——只把整幅往下推到图例
  // 底边以下，等于为一个偏左的遮挡物白扔一整条横带的高度。
  // 所以按矩形求交：先把顶卡用当前变换投到屏幕坐标，真压上了才推，推的距离正好是压住
  // 的那一段。判据用屏幕像素，因为这个遮挡物只在屏幕上存在，不在布局坐标里。
  const legend = $('.graph-legend');
  const legendBox = legend ? legend.getBoundingClientRect() : null;
  if (!legendBox || !legendBox.width || !legendBox.height) return;
  const scale = graphView.scale;
  const k = fit * scale;
  const gap = 8;
  const cardLeft = rect.left + k * topBox.x + fit * graphView.tx;
  const cardRight = cardLeft + k * topBox.w;
  const cardTop = rect.top + k * topBox.y + fit * graphView.ty;
  const overlapsX = cardLeft < legendBox.right + gap && cardRight > legendBox.left - gap;
  const need = legendBox.bottom + gap - cardTop;
  if (!overlapsX || need <= 0) return;
  // 屏幕像素 → 用户单位：`screen = fit * (scale * u + t)`，所以推 d 像素是 `t += d / fit`。
  graphView.ty += need / fit;
}
/** 初始缩放：**不低于基准 600%**，但可以更大（合并视图常需要 18 倍才看得清一张卡）。
 *
 *  量不到画布尺寸时（页签还没露出来）返回基准：那时算出来的任何值都不可信，宁可先
 *  落在基准上，等第一次量到尺寸再重算（见 `retryMeasureGraph`）。 */
function initialGraphScale() {
  const fit = fitScale();
  if (!fit) return BASE_SCALE;
  return Math.max(BASE_SCALE, 1 / fit);
}
/** 量到画布尺寸之前不许把视角钉死。 */
const MAX_MEASURE_RETRY = 60;
/** 画布第一次量到尺寸时补上那次没算准的视角。
 *
 *  `init()` 无条件取一次链路，而那时页签常常还没露出来（`#graph-canvas` 是 0x0），
 *  算出来的缩放与摆正都不可信。`ResizeObserver` 只报"尺寸**变了**"，而这里要的是
 *  "尺寸**有了**"——夹具里画布从 0x0 直接稳定到目标尺寸，之后再没有变化事件。
 *
 *  所以除了观察器，还自己排一帧：要求**连续两帧读到同一个尺寸**才落定（首帧常常是
 *  过渡值：侧栏还在收、字体还没换）。rAF 与 `setTimeout(80)` 两条路都排——无头浏览器
 *  的虚拟时钟下 rAF 不一定响，只靠它会让截图永远停在空网格上。 */
function retryMeasureGraph() {
  if (state.graphScaleMeasured) return;
  if (state.graphMeasureRetry >= MAX_MEASURE_RETRY) return;
  state.graphMeasureRetry += 1;
  const canvas = $('#graph-canvas');
  const again = () => {
    if (state.graphScaleMeasured) return;
    const rect = canvas ? canvas.getBoundingClientRect() : null;
    const w = rect ? Math.round(rect.width) : 0, h = rect ? Math.round(rect.height) : 0;
    const last = state.graphMeasureRect;
    state.graphMeasureRect = { width: w, height: h };
    if (!w || !h || !last || last.width !== w || last.height !== h) { retryMeasureGraph(); return; }
    renderGraph();
  };
  if (window.requestAnimationFrame) window.requestAnimationFrame(again);
  window.setTimeout(again, 80);
}
/** 把一次重画推迟到下一帧，且**同一帧内只画一次**。
 *
 *  为什么需要队列：pointermove 的频率不由帧率决定。高回报率鼠标（1000 Hz）或者一次
 *  卡顿之后的追帧，都会在一帧里送来好几次 move；画布是整张重绘，每一次都画一遍
 *  等于把同一帧画十遍，而只有最后一次看得见。队列把"每次事件一次重绘"变成"每帧一次"。
 *
 *  累加的量仍然是 `graphView.tx/ty`（相对位移），队列只决定**什么时候画出去**，
 *  所以拖动不会丢增量——丢增量会让图比光标走得慢，那是拖动最容易察觉的坏味道。 */
function scheduleTransform() {
  if (graphView.frame) return;
  const flush = () => { graphView.frame = 0; paintGraph(); };
  // rAF 缺席时退回同步画（老浏览器 / 测试环境）：宁可慢一档，也不要把这一帧的
  // 位移丢掉——丢掉的表现是"图偶尔不动"。
  graphView.frame = window.requestAnimationFrame ? window.requestAnimationFrame(flush) : (flush(), 0);
}
/* ---- 绘制 ----------------------------------------------------------------
 *
 *  画布是**一层像素**：没有"某个节点"这个对象可以单独改，所以每次重画整张。
 *  代价靠三个地方收着：重画只在 `scheduleTransform` 的帧边界上发生；拖动期间不画
 *  边上的中文标签（344 条边各一次描边+填充是最贵的一段）；命中测试与 hover 都走
 *  `hitNode`，不去遍历 DOM（也没有 DOM 可遍历）。
 *
 *  **不做动画**：改造前的"进行中的意图"是流动虚线 + 图标块呼吸，靠 CSS animation
 *  跑；画布上同样的效果要每帧整张重绘，350 张卡时那一帧是几十毫秒，会与拖动抢帧。
 *  现在"在跑"这条信息由两处静态表达承担：连向它的边是虚线且更亮，它的图标块带一圈
 *  强调色描边。读得出来，而且不花帧。 */
function roundRectPath(ctx, x, y, w, h, r) {
  const radius = Math.min(r, Math.abs(w) / 2, Math.abs(h) / 2);
  ctx.beginPath();
  if (ctx.roundRect) { ctx.roundRect(x, y, w, h, radius); return; }
  ctx.moveTo(x + radius, y);
  ctx.lineTo(x + w - radius, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
  ctx.lineTo(x + w, y + h - radius);
  ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
  ctx.lineTo(x + radius, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
  ctx.lineTo(x, y + radius);
  ctx.quadraticCurveTo(x, y, x + radius, y);
  ctx.closePath();
}
/** 按像素宽度截断并补省略号（画布上没有排版引擎替我们折行）。 */
function clipText(ctx, text, maxWidth) {
  const str = String(text === null || text === undefined ? '' : text);
  if (!str) return '';
  if (ctx.measureText(str).width <= maxWidth) return str;
  let cut = str.length;
  while (cut > 1) {
    cut -= 1;
    const candidate = str.slice(0, cut) + '…';
    if (ctx.measureText(candidate).width <= maxWidth) return candidate;
  }
  return '…';
}
/** 按宽度折行，最多 `limit` 行，最后一行超出的部分用省略号。 */
function wrapText(ctx, text, maxWidth, limit) {
  const str = String(text === null || text === undefined ? '' : text);
  if (!str) return [];
  const out = [];
  let rest = str;
  while (rest && out.length < limit) {
    if (ctx.measureText(rest).width <= maxWidth) { out.push(rest); rest = ''; break; }
    let cut = rest.length;
    while (cut > 1 && ctx.measureText(rest.slice(0, cut)).width > maxWidth) cut -= 1;
    out.push(rest.slice(0, cut));
    rest = rest.slice(cut);
  }
  if (rest) out[out.length - 1] = clipText(ctx, out[out.length - 1] + rest, maxWidth);
  return out;
}
/** 箭头：14×14 的闭合三角（照 ARTEX 的 `MarkerType.ArrowClosed, 14`），尖端落在端点。 */
function drawArrow(ctx, geo) {
  const angle = Math.atan2(geo.y2 - geo.cy2, geo.x2 - geo.cx2);
  if (!isFinite(angle)) return;
  ctx.save();
  ctx.translate(geo.x2, geo.y2);
  ctx.rotate(angle);
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(-14, -5);
  ctx.lineTo(-14, 5);
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}
/** 画所有边。`graphIndex.keep` 是 hover 时的"相关集"（本人 + 邻居）。 */
function drawEdges(ctx, palette) {
  const boxes = graphIndex.boxes, edges = graphIndex.edges || [];
  if (!boxes || !edges.length) return;
  const hidden = graphIndex.hidden || new Set();
  const hoverId = graphIndex.hoverId;
  edges.forEach((edge) => {
    if (EDGE_HIDDEN_KINDS.indexOf(edge.kind) >= 0) return;
    if (hidden.has(edge.source) || hidden.has(edge.target)) return;
    const from = boxes.get(edge.source), to = boxes.get(edge.target);
    if (!from || !to) return;
    const geo = edgeGeometry(boxWithPin(from, edge.source), boxWithPin(to, edge.target));
    const kind = EDGE_KINDS.indexOf(edge.kind) >= 0 ? edge.kind : EDGE_FALLBACK_KIND;
    const related = !hoverId || edge.source === hoverId || edge.target === hoverId;
    const live = graphIndex.liveIds && graphIndex.liveIds.has(edge.target);
    const color = palette.edge[kind] || palette.edge[EDGE_FALLBACK_KIND];
    ctx.save();
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = related && hoverId ? 2.4 : 2;
    ctx.globalAlpha = related ? (live ? 0.9 : (hoverId ? 1 : 0.55)) : 0.07;
    if (live) ctx.setLineDash([6, 6]);
    ctx.beginPath();
    ctx.moveTo(geo.x1, geo.y1);
    ctx.bezierCurveTo(geo.cx1, geo.cy1, geo.cx2, geo.cy2, geo.x2, geo.y2);
    ctx.stroke();
    ctx.setLineDash([]);
    drawArrow(ctx, geo);
    // 标签：拖动期间整段跳过（344 条边各描一次边再填一次字，是这一帧最贵的一段）。
    if (!graphView.dragging && related) {
      ctx.globalAlpha = hoverId ? 0.95 : 0.85;
      ctx.font = palette.fsCaption + 'px ' + palette.fontSans;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'bottom';
      ctx.lineWidth = 3;
      ctx.strokeStyle = palette.bg;
      ctx.strokeText(EDGE_LABEL[edge.kind] || edge.kind || '关系', geo.mx, geo.my - 4);
      ctx.fillStyle = palette.muted;
      ctx.fillText(EDGE_LABEL[edge.kind] || edge.kind || '关系', geo.mx, geo.my - 4);
    }
    ctx.restore();
  });
}
/** 画一张卡。四角的位置与改造前的 DOM 卡一一对应：
 *
 *  左上  类型图标块 + 中文类型名 ………………………………………………… 右上 优先级（P7）
 *  第二行  标题（单行，超出省略号）
 *  第三~五行  摘要（最多三行，末行省略号）
 *  底行  状态徽章 ………………………………………………………………………………… #编号 */
function drawNodeCard(ctx, palette, node, box) {
  const kind = displayKindOf(node);
  const color = palette.kind[kind] || palette.kind.intent || palette.text;
  const severity = node.severity ? severityOf(node.severity) : '';
  const selected = state.selectedNodeId === node.id;
  const hovered = graphIndex.hoverId === node.id;
  const related = graphIndex.keep && graphIndex.keep.has(node.id);
  const live = kind === 'intent' && (node.state || 'active') === 'active';
  const priority = Number(node.priority) || 0;
  const stateText = node.stateLabel || STATE_LABEL[node.state] || '';
  const stateName = node.state || 'active';
  const dashed = kind === 'begin' || kind === 'hint' || kind === 'digest';
  const x = box.x, y = box.y, w = box.w, h = box.h;
  const padX = 11, padY = 9;
  const innerW = w - padX * 2;

  ctx.save();
  // 悬停时无关的卡压暗：整张画布读起来就只剩"这一条链"。
  ctx.globalAlpha = graphIndex.hoverId && !related ? 0.16 : 1;
  roundRectPath(ctx, x, y, w, h, 12);
  ctx.fillStyle = palette.panel;
  ctx.fill();
  // 描边：类型色；严重度压过类型色（一条 critical 漏洞必须第一眼被认出来）。
  let stroke = color, lineWidth = 1;
  if (dashed) ctx.setLineDash([6, 4]);
  if (severity && palette.severity[severity]) { stroke = palette.severity[severity]; lineWidth = severity === 'critical' || severity === 'high' ? 2 : 1; }
  ctx.lineWidth = lineWidth;
  ctx.strokeStyle = stroke;
  ctx.stroke();
  ctx.setLineDash([]);
  // 选中 / 悬停：强调色描边 + 一圈软光环（与改造前的 box-shadow 光环同一个意思）。
  if (selected || hovered) {
    roundRectPath(ctx, x - 2, y - 2, w + 4, h + 4, 14);
    ctx.lineWidth = selected ? 3 : 2;
    ctx.strokeStyle = palette.accentSoft;
    ctx.stroke();
    roundRectPath(ctx, x, y, w, h, 12);
    ctx.lineWidth = 2;
    ctx.strokeStyle = palette.accent;
    ctx.stroke();
  }
  ctx.save();
  roundRectPath(ctx, x, y, w, h, 12);
  ctx.clip();

  // 图标块：16×16、圆角 5，底色是类型色混 18%。画布没有 `color-mix`，用一次
  // 半透明的同色填充代替——视觉上是同一件事（底色只是那一点色）。
  const glyphY = y + padY;
  ctx.save();
  if ((selected || hovered) && palette.hl) ctx.filter = selected ? (palette.hlStrong || palette.hl) : palette.hl;
  roundRectPath(ctx, x + padX, glyphY, 16, 16, 5);
  ctx.globalAlpha = 0.18;
  ctx.fillStyle = color;
  ctx.fill();
  ctx.globalAlpha = 1;
  if (live) {
    ctx.lineWidth = 2;
    ctx.strokeStyle = palette.accent;
    ctx.stroke();
  }
  ctx.beginPath();
  ctx.arc(x + padX + 8, glyphY + 8, 3, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
  // 类型名：12px / 600，颜色就是类型色。
  ctx.font = '600 ' + palette.fsCaption + 'px ' + palette.fontSans;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  ctx.fillStyle = color;
  const label = KIND_LABEL[kind] || kind;
  ctx.fillText(label, x + padX + 22, glyphY + 8);
  ctx.restore();

  // 右上：优先级徽章。0 是"没定优先级"，不是"P0"。
  if (priority > 0) {
    const tone = priorityTone(priority);
    ctx.font = '600 ' + palette.fsCaption + 'px ' + palette.fontMono;
    const chipText = 'P' + priority;
    const chipW = ctx.measureText(chipText).width + 12;
    const chipH = palette.fsCaption + 6;
    roundRectPath(ctx, x + w - padX - chipW, glyphY - 1, chipW, chipH, 6);
    ctx.fillStyle = palette.prioBg[tone] || palette.border;
    ctx.fill();
    ctx.fillStyle = palette.prioFg[tone] || palette.dim;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(chipText, x + w - padX - chipW / 2, glyphY - 1 + chipH / 2);
  }

  // 标题：单行。
  let cursorY = glyphY + 16 + 3;
  ctx.font = '600 ' + palette.fsSmall + 'px ' + palette.fontSans;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'top';
  ctx.fillStyle = palette.text;
  ctx.fillText(clipText(ctx, node.label || node.id, innerW), x + padX, cursorY + 2);
  cursorY += palette.fsSmall + 6;

  // 摘要：最多三行，行高 1.35。
  if (node.subtitle) {
    ctx.font = palette.fsSmall + 'px ' + palette.fontSans;
    ctx.fillStyle = palette.muted;
    const lineHeight = palette.fsSmall * 1.35;
    const rows = wrapText(ctx, node.subtitle, innerW, 3);
    rows.forEach((row, index) => ctx.fillText(row, x + padX, cursorY + index * lineHeight));
  }

  // 底行：状态徽章在左、编号靠右。它钉在卡底，与内容多少无关。
  const footH = palette.fsCaption + 6;
  const footY = y + h - padY - footH;
  if (stateText) {
    ctx.font = '600 ' + palette.fsCaption + 'px ' + palette.fontSans;
    const badgeW = ctx.measureText(stateText).width + 14;
    roundRectPath(ctx, x + padX, footY, badgeW, footH, footH / 2);
    ctx.fillStyle = palette.stateBg[stateName] || palette.border;
    ctx.fill();
    ctx.fillStyle = palette.stateFg[stateName] || palette.dim;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(stateText, x + padX + 7, footY + footH / 2);
  }
  ctx.font = palette.fsCaption + 'px ' + palette.fontMono;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  ctx.fillStyle = palette.dim;
  ctx.fillText('#' + node.id, x + w - padX, footY + footH / 2);

  ctx.restore();
  ctx.restore();
}
/** 画所有卡。顺序即 `layout.nodes`（后端按列排好），后画的压在上面。 */
function drawNodes(ctx, palette) {
  const boxes = graphIndex.boxes, nodes = graphIndex.nodes || [];
  if (!boxes) return;
  const hidden = graphIndex.hidden || new Set();
  nodes.forEach((node) => {
    if (hidden.has(node.id)) return;
    const box = boxWithPin(boxes.get(node.id), node.id);
    if (!box) return;
    drawNodeCard(ctx, palette, node, box);
  });
}
/** 主重画：清画布 → 按当前视角设变换 → 边 → 卡 → 缩略图视野框。
 *
 *  画布尺寸走 `devicePixelRatio`：CSS 尺寸交给布局（100% × 100%），后备缓冲按物理
 *  像素铺满，否则高分屏上整张图是糊的。`k = fit * scale` 与命中测试、拖动、滚轮
 *  锚点共用一个换算，见本节开头那段坐标系说明。 */
function paintGraph() {
  const surface = $('#graph-surface');
  if (!surface || surface.classList.contains('hidden')) { syncMinimapWindow(); return; }
  const rect = surface.getBoundingClientRect();
  const width = Number(surface.dataset.width) || 0, height = Number(surface.dataset.height) || 0;
  if (!rect.width || !rect.height || !width || !height) { syncMinimapWindow(); return; }
  const dpr = window.devicePixelRatio || 1;
  const pxW = Math.max(1, Math.round(rect.width * dpr)), pxH = Math.max(1, Math.round(rect.height * dpr));
  if (surface.width !== pxW) surface.width = pxW;
  if (surface.height !== pxH) surface.height = pxH;
  const ctx = surface.getContext('2d');
  if (!ctx) return;
  const fit = Math.min(rect.width / width, rect.height / height);
  const k = fit * graphView.scale;
  const palette = graphPalette();
  // hover 的"相关集"：本人 + 直接邻居。边与卡都读它，不必各自再查一遍。
  const keep = new Set();
  if (graphIndex.hoverId) {
    keep.add(graphIndex.hoverId);
    (graphIndex.neighbors.get(graphIndex.hoverId) || new Set()).forEach((id) => keep.add(id));
  }
  graphIndex.keep = keep;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, pxW, pxH);
  ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * fit * graphView.tx, dpr * fit * graphView.ty);
  drawEdges(ctx, palette);
  drawNodes(ctx, palette);
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  syncMinimapWindow();
}
/** `paintGraph` 的旧名字：`applyTransform` 是"把视角写出去"的那一步，而画布上写视角
 *  与重画是同一件事。留着这个薄封装是为了让 `loadGraph` / 心跳那条路径读起来不变。 */
function applyTransform() {
  paintGraph();
}
/** 「链路已更新」提示：只在**有新数据、而视角被保留**时出现。
 *
 *  两个条件缺一不可：布局没变时新节点就落在原处，没什么可提示的；视角没被动过时
 *  `renderGraph` 会自己重新适配，新节点直接看得见。真正需要它的是第三种情况——
 *  人已经放大到某个节点上，新数据落在视野外，而页面此刻**不该**擅自移动视角。
 *  于是把"要不要重新适配"这个决定交回给使用者：点它 = 接受视角重置。 */
function setGraphStale(flag) {
  const node = $('#graph-stale');
  if (!node) return;
  node.classList.toggle('hidden', !flag);
}
/** 主渲染：节点位置完全采用后端 layout，前端不做布局计算。 */
function renderGraph() {
  const surface = $('#graph-surface'), empty = $('#graph-empty'), layout = state.graph && state.graph.layout;
  const nodes = asArray(layout && layout.nodes);
  if (!nodes.length) {
    if (surface) surface.classList.add('hidden');
    if (empty) {
      empty.classList.remove('hidden');
      empty.textContent = state.graph ? '链路里还没有任何节点' : '正在加载链路…';
    }
    graphIndex.nodes = []; graphIndex.edges = []; graphIndex.boxes = new Map();
    graphIndex.liveIds = new Set(); graphIndex.hoverId = null;
    if (graphIndex.minimapSize) { graphIndex.minimapSize = null; renderMinimap([], new Map(), 0, 0); }
    setGraphStale(false);                                // 空图上没有"新节点在视野外"这回事
    return;
  }
  if (surface) surface.classList.remove('hidden');
  if (empty) empty.classList.add('hidden');
  const width = Number(layout.width) || 1200, height = Number(layout.height) || 800;
  if (surface) { surface.dataset.width = String(width); surface.dataset.height = String(height); }
  // 折叠只发生在画这一步：`layout` 本身不动，详情面板与离线 SVG 拿到的还是全量。
  const view = collapseDigests(nodes, layout.edges);
  const boxes = new Map();
  view.nodes.forEach((node) => boxes.set(node.id, nodeBox(node)));
  graphIndex.boxes = boxes;
  graphIndex.nodes = view.nodes;
  graphIndex.edges = view.edges;
  // 卡片偏移跨重画保留：心跳每 3 秒重画一次，人拖开的卡不该自己弹回去。
  // 反向也要清——被折叠掉或已不在窗口里的卡，偏移留着只会越积越多。
  Array.from(graphView.pins.keys()).forEach((id) => { if (!boxes.has(id)) graphView.pins.delete(id); });
  // 进行中的意图：边与卡都要知道这一批里谁在跑，所以先算一次集合。
  graphIndex.liveIds = new Set();
  view.nodes.forEach((node) => {
    if (displayKindOf(node) === 'intent' && (node.state || 'active') === 'active') graphIndex.liveIds.add(node.id);
  });
  if (graphIndex.hoverId && !boxes.has(graphIndex.hoverId)) graphIndex.hoverId = null;
  renderMinimap(view.nodes, boxes, width, height);
  buildNeighborIndex();
  // **视图与选中项一律跨重画保留**，只在"画布还没量到尺寸"时摆正一次。
  //
  // 这里原来写的是"布局指纹变了就重置"（`graphLayoutKey`）。链路接进心跳之后那条
  // 规则每 3 秒就会把视角拽回去一次——而新节点恰恰改变布局尺寸，被拽回去的还是
  // 刚调好的那个位置。所以重置条件收紧成"没量过"：
  //
  //   * `!state.graphScaleMeasured`：`init()` 无条件调 `loadGraph`，那时页签可能
  //     还没露出来（`#graph-canvas` 的 rect 是 0×0），那一次算出的缩放不可信，
  //     必须重算一次（症状否则是"图小到读不出，怎么调都在同一个错的位置上"）；
  //   * 量到之后，布局变化只更新缩放**上限**与那条提示，不动 scale/tx/ty/选中。
  //
  // 没手动动过视角的人（`graphView.touched`）另算：那样把视角跟着新布局重新适配
  // 一次，看到的才是"数据长出来了"，而不是"我盯着的永远是左上角那一块"。一旦
  // 他自己缩放过或拖过，就再也不擅自移动。
  const layoutKey = width + 'x' + height + ':' + nodes.length;
  const layoutChanged = state.graphLayoutKey !== layoutKey;
  const measurable = fitScale() > 0;
  const initial = initialGraphScale();
  graphView.ceiling = Math.max(BASE_SCALE * BASE_HEADROOM, initial * 1.5);
  if (!state.graphScaleMeasured) {
    state.graphScaleMeasured = measurable;
    graphView.scale = clamp(initial, MIN_SCALE, graphView.ceiling);
    graphView.tx = 0;
    graphView.ty = 0;
    graphView.touched = false;
    alignViewToStart(boxes);
  } else if (layoutChanged && !graphView.touched) {
    graphView.scale = clamp(initial, MIN_SCALE, graphView.ceiling);
    alignViewToStart(boxes);
  }
  state.graphLayoutKey = layoutKey;
  setGraphStale(layoutChanged && graphView.touched);
  applyKindFilter();                                     // 里面会重画（含选中态）
  // 重画之后再喂一次详情面板：心跳每 3 秒换掉整张图，右侧那一栏读的却是上一批数据。
  // 选中项本身跨重画保留（`state.selectedNodeId` 不动），这里只是让它显示的东西也跟上。
  if (state.selectedNodeId) renderNodeDetail(state.selectedNodeId);
  if (!state.graphScaleMeasured) retryMeasureGraph();
}
/** 缩略图：整张布局压成一张小图，**只读**。它回答的是「我现在看的是哪一块」。
 *
 *  这一块仍然是 SVG（`#minimap-svg`）：它只画几十个矩形、不需要文字、也不需要与
 *  主画布共用坐标系，用 SVG 反而省掉一次 DPR 换算。节点按类型上色，与卡上的图标
 *  块同一个令牌源；看不见字——这个尺度上文字只会糊成一片噪点。 */
function renderMinimap(nodes, boxes, width, height) {
  const svg = $('#minimap-svg');
  if (!svg) return;
  clear(svg);
  if (!nodes.length || !width || !height) { graphIndex.minimapSize = null; return; }
  svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  nodes.forEach((node) => {
    const box = boxes.get(node.id);
    if (!box) return;
    svg.append(svgEl('rect', {
      class: 'minimap-node kind-' + displayKindOf(node),
      x: box.x, y: box.y, width: box.w, height: box.h, rx: '6',
    }));
  });
  graphIndex.minimapSize = { width, height };
}
/** 缩略图上的视野框：把当前视角换算成缩略图里的一个矩形。
 *
 *  三套坐标要对齐：**缩略图元素**（180×120 的卡里 inset 4px）、它按 `xMidYMid meet`
 *  装 viewBox 后的实际画布（`fit`）、主画布的屏幕像素。任何一步漏了都会让框偏小或
 *  偏在角落（漏 `fit` 时框只有真实的四成；漏主画布的 `fit` 时框不随缩放变）。 */
function syncMinimapWindow() {
  const mask = $('#minimap-window'), canvas = $('#graph-canvas'), svg = $('#minimap-svg');
  const size = graphIndex.minimapSize;
  if (!mask || !canvas || !svg || !size) { if (mask) mask.classList.add('hidden'); return; }
  const cRect = canvas.getBoundingClientRect();
  const sRect = svg.getBoundingClientRect();
  const hRect = mask.parentElement ? mask.parentElement.getBoundingClientRect() : null;
  if (!cRect.width || !cRect.height || !sRect.width || !sRect.height || !hRect) { mask.classList.add('hidden'); return; }
  if (!size.width || !size.height) { mask.classList.add('hidden'); return; }
  const fit = Math.min(sRect.width / size.width, sRect.height / size.height);
  if (!fit) { mask.classList.add('hidden'); return; }
  const ox = sRect.left - hRect.left + (sRect.width - size.width * fit) / 2;
  const oy = sRect.top - hRect.top + (sRect.height - size.height * fit) / 2;
  // 主画布那层"整幅塞进元素框"的系数：视野宽 = 元素宽 / (scale * 它)。
  const metrics = surfaceMetrics();
  const base = metrics ? metrics.fit : 0;
  if (!base) { mask.classList.add('hidden'); return; }
  const left = -graphView.tx / graphView.scale, top = -graphView.ty / graphView.scale;
  const viewW = cRect.width / (graphView.scale * base), viewH = cRect.height / (graphView.scale * base);
  mask.style.left = (ox + left * fit) + 'px';
  mask.style.top = (oy + top * fit) + 'px';
  mask.style.width = (viewW * fit) + 'px';
  mask.style.height = (viewH * fit) + 'px';
  mask.classList.remove('hidden');
}
/** 邻接表：hover 时高亮"这一条链"要用（无向，因为读图时两边都算关联）。 */
function buildNeighborIndex() {
  const map = new Map();
  (graphIndex.edges || []).forEach((edge) => {
    if (!map.has(edge.source)) map.set(edge.source, new Set());
    if (!map.has(edge.target)) map.set(edge.target, new Set());
    map.get(edge.source).add(edge.target);
    map.get(edge.target).add(edge.source);
  });
  graphIndex.neighbors = map;
}
/** 类型芯片：把被关掉的类型记进 `graphIndex.hidden`，然后重画。
 *
 *  改造前是给每张卡 toggle `filtered-out` 类；画布上"不画"就是真的跳过，
 *  连它的边也一起跳过（边两端都得在场，否则会留下指向空处的线）。 */
function applyKindFilter() {
  const hidden = new Set();
  (graphIndex.nodes || []).forEach((node) => {
    if (state.kindFilter[displayKindOf(node)] === false) hidden.add(node.id);
  });
  graphIndex.hidden = hidden;
  paintGraph();
}
/** hover 高亮：本人与直接邻居保持原样，其余压暗。空 id = 清掉高亮。 */
function highlightNeighborhood(id) {
  if (graphIndex.hoverId === (id || null)) return;
  graphIndex.hoverId = id || null;
  paintGraph();
}
function clearHighlight() {
  highlightNeighborhood(null);
}
/* --------------------------------------------------- 7. 节点详情面板 ---- */
/** 原始字段 → 键值对列表（含未识别字段）。 */
function kvList(raw) {
  const list = el('dl', 'kv'), keys = Object.keys(raw || {});
  if (!keys.length) { list.append(el('dt', 'kv-key', '—'), el('dd', 'kv-val', '无字段')); return list; }
  keys.forEach((key) => {
    const term = el('dt', 'kv-key', FIELD_LABEL[key] || key);
    if (FIELD_LABEL[key]) term.title = key;
    list.append(term, el('dd', 'kv-val', formatValue(raw[key], key)));
  });
  return list;
}
/** 详情抽屉的一节：一个标题 + 一块内容。
 *
 *  分节是 ARTEX 那个 Sheet 的形状：卡头说「这是什么」，往下依次是基本、原始字段、
 *  压缩成员、关联边。旧版把标题、原始字段、关联边直接摊在同一列里，靠留白分界，
 *  字段一多（一条 fact 有十几个键）就分不清哪一行还算「结论」。
 */
function detailGroup(title, node) {
  const group = el('section', 'detail-group');
  group.append(el('div', 'detail-section', title), node);
  return group;
}
/** 详情面板：该节点的全部原始字段 + 关联边。 */
function renderNodeDetail(id) {
  const body = $('#node-detail-body'), entry = id ? state.nodeIndex.get(id) : null;
  clear(body);
  if (!id) { body.append(emptyBlock('点击左侧节点查看其全部原始字段')); return; }
  if (!entry) { body.append(emptyBlock(`当前数据中找不到节点 ${id}`)); return; }
  // 类型徽章走**显示类型**而不是结构类型：卡片上写「起点」「提示」的那些节点，点开
  // 之后如果写回「事实」，读的人会以为自己点错了。显示类型只有后端算得准
  // （`graph.display_kind`：origin 的事实是起点、channel=hint 的事实是提示），所以这里
  // 到 layout 里取同一个节点的 `displayKind`，不在前端把那条规则再写一遍。
  const layoutNode = asArray(state.graph && state.graph.layout && state.graph.layout.nodes)
    .find((node) => node.id === id);
  const kind = displayKindOf(layoutNode || { kind: entry.kind });
  const sev = entry.raw.severity ? severityOf(entry.raw.severity) : null;
  // 卡头：图标块与卡片左上角那枚同源（由 kind-* 上色），右边两行——类型名与编号。
  // 旧版把类型/优先级/状态/严重度做成四颗并排的小胶囊，字号一样、颜色各异，扫一眼
  // 分不出哪个是"它是什么"；换成卡头之后，结论部分归到下面「基本」的四行里。
  const head = el('div', 'detail-head');
  const glyph = el('span', 'detail-glyph');
  glyph.classList.add('kind-' + kind);
  glyph.append(el('span', 'detail-dot'));
  const names = el('div', 'detail-names');
  names.append(el('div', 'detail-name', KIND_LABEL[kind] || kind), el('div', 'detail-id', id));
  head.append(glyph, names);
  body.append(head);
  // 「基本」：四行结论。它们都能在下面的原始字段里找到，但那要读十几个键——
  // 而点开一张卡时想问的正是「这是什么、走到哪一步、多急、多严重」。
  const basic = el('dl', 'kv');
  const row = (label, value) => {
    if (!value) return;
    basic.append(el('dt', 'kv-key', label), el('dd', 'kv-val', value));
  };
  row('类型', KIND_LABEL[kind] || kind);
  row('状态', STATE_LABEL[entry.raw.state] || entry.raw.state || '');
  row('优先级', Number(entry.raw.priority) > 0 ? 'P' + Number(entry.raw.priority) : '');
  row('严重度', sev ? SEVERITY_LABEL[sev] : '');
  body.append(detailGroup('基本', basic), detailGroup('原始字段', kvList(entry.raw)));
  // 压缩的成员清单。画布上被吸收的记录已经不单画了，这里就是它们唯一的入口——
  // 折叠是为了「先读结论」，不是「看不见细节」。成员按 id 回查索引，点一下就地
  // 换成那一条的详情（它不在画布上，所以不会高亮任何卡片，但字段一个不少）。
  if (kind === 'digest') body.append(digestMembers(entry.raw));
  // 关联边要**按链条的顺序**列，不能按数据到达顺序（那是并发的作业写进来的顺序）。
  // 主键是关系本身的方向语义（派生 → 意图链 → 产出 → 证明 → 覆盖 → 归属），
  // 次键是对端在布局里的那一列：同一种关系的三条边，左边那条先读。这两个键合起来
  // 就是"这条链从我这儿往哪走、走出去了几步"。
  const columnOf = (nodeId) => {
    const hit = asArray(state.graph && state.graph.layout && state.graph.layout.nodes).find((node) => node.id === nodeId);
    return hit ? Number(hit.x) || 0 : 0;
  };
  const related = asArray(state.graph && state.graph.edges)
    .filter((edge) => edge.sourceId === id || edge.targetId === id)
    .sort((left, right) => {
      const order = (edge) => { const at = EDGE_SORT_ORDER.indexOf(edge.kind); return at < 0 ? EDGE_SORT_ORDER.length : at; };
      if (order(left) !== order(right)) return order(left) - order(right);
      const far = (edge) => (edge.sourceId === id ? edge.targetId : edge.sourceId);
      return columnOf(far(left)) - columnOf(far(right));
    });
  if (!related.length) return;
  const list = el('ul', 'edge-list');
  const section = detailGroup(`关联边（${related.length}）`, list);
  related.forEach((edge) => {
    const outgoing = edge.sourceId === id, item = el('li', 'edge-item');
    item.append(el('span', 'edge-kind', EDGE_LABEL[edge.kind] || edge.kind || '关系'));
    item.append(el('span', 'edge-dir', `${outgoing ? '→' : '←'} ${outgoing ? edge.targetId : edge.sourceId}`));
    list.append(item);
  });
  body.append(section);
}
/** 压缩的成员清单：它吸收了哪些记录，逐条列出来并可以点进去。
 *
 *  成员 id 在 payload.memberIds 里分 facts / intents 两组（这两个词是数据形状，不是
 *  中文说法），合并成一个列表——对读的人而言「这条记录是事实还是意图」已经写在每条
 *  的小字里，不必再分成两节。
 *
 *  找不到的那条也要列：压缩记的是写入当时的 id 集合，而节点可能已被窗口截掉。
 *  悄悄跳过会让人以为压缩吸收了 3 条，而它写着 5 条。 */
function digestMembers(raw) {
  const ids = [];
  const members = (raw && raw.memberIds) || {};
  ['facts', 'intents'].forEach((key) => asArray(members[key]).forEach((id) => ids.push(String(id))));
  const box = el('div', 'detail-body'), title = `压缩成员（${ids.length}）`;
  if (!ids.length) { box.append(emptyBlock('这一代压缩没有吸收任何记录')); return detailGroup(title, box); }
  const list = el('ul', 'member-list');
  ids.forEach((id) => {
    const hit = state.nodeIndex.get(id), item = el('li', 'member-row');
    const button = el('button', 'member-open');
    button.type = 'button';
    button.append(el('span', 'member-kind', hit ? (KIND_LABEL[displayKindOf(hit.raw)] || hit.kind) : '已不在窗口内'));
    button.append(el('span', 'member-text', hit ? truncate(String(hit.raw.detail || hit.raw.title || hit.raw.name || id), 60) : id));
    button.append(el('span', 'member-id', id));
    if (hit) { button.addEventListener('click', () => renderNodeDetail(id)); }
    else { button.disabled = true; }
    item.append(button); list.append(item);
  });
  box.append(list);
  return detailGroup(title, box);
}
function selectNode(id) {
  state.selectedNodeId = id;
  // 画布上"选中"不是一个元素上的类，而是这一帧重画时换一套描边——所以只标记 + 重画。
  if (graphIndex.boxes) paintGraph();
  renderNodeDetail(id);
}

/* ------------------------------------------------------ 8. 数据加载 ---- */
function setPanelMessage(selector, message) {
  const node = $(selector);
  if (!node) return;
  clear(node);
  node.append(message);
}
/** 带缓存与并发去重的资源加载；apply(data, error) 负责落库与渲染。 */
function loadResource(key, path, apply, force) {
  if (!state.eid) return Promise.resolve();
  if (force) state.loaded[key] = false;
  if (state.loaded[key]) return Promise.resolve();
  if (state.inflight[key]) return state.inflight[key];
  state.inflight[key] = apiGet(path)
    .then((data) => { state.loaded[key] = true; apply(data, null); })
    .catch((error) => { state.loaded[key] = false; apply(null, error); })
    .finally(() => { delete state.inflight[key]; });
  return state.inflight[key];
}
const engagementPath = (suffix) => `/engagements/${encodeURIComponent(state.eid)}${suffix}`;
/** 漏洞 / 资产 / 报告共用一个加载器：只差接口路径、字段名与渲染回调。 */
function loadFindings(force) {
  return loadResource('findings', engagementPath('/findings'), (data, error) => {
    if (error) {
      state.findings = []; state.findingGroups = [];
      showError('漏洞列表加载失败：' + error.message);
    } else {
      // `findings` 一条不少（筛选、退路渲染、实例正文都靠它），`groups` 是**同一次
      // 响应**里的派生视图——按问题合并之后的卡片。两个字段同时来、同时走：只更新
      // 其中之一就会出现"卡片数与记录数对不上"而界面看起来完全正常。
      state.findings = asArray(data.findings);
      state.findingGroups = asArray(data.groups);
    }
    renderFindings(error);
  }, force);
}
/** 「全部目标」的漏洞：一次拿回所有目标的分组（`GET /findings`）。
 *
 *  为什么不复用 `loadResource`：它的缓存键写在路径里（带 eid），而这一份是跨目标
 *  共用的；更要紧的是它**不是** `state.eid` 的从属资源——没选目标时它也该能取
 *  （"几个目标同时在打，一共发现了什么"不依赖当前停在哪个）。
 */
function loadAllFindings(force) {
  if (force) state.allFindingsLoaded = false;
  if (state.allFindingsLoaded) return Promise.resolve();
  if (state.allFindingsInflight) return state.allFindingsInflight;
  state.allFindingsInflight = apiGet('/findings')
    .then((data) => {
      state.findingsByTarget = asArray(data.targets);
      state.findingsTotals = data.totals || null;
      state.allFindingsLoaded = true;
      renderFindings(null);
      renderFindingsTargets();
      renderScopeStats(state.eid);
    })
    .catch((error) => {
      state.allFindingsLoaded = false;
      showError('全部目标的漏洞加载失败：' + error.message);
      renderFindings(error);
    })
    .finally(() => { state.allFindingsInflight = null; });
  return state.allFindingsInflight;
}
function loadAssets(force) {
  return loadResource('assets', engagementPath('/assets'), (data, error) => {
    if (error) { state.assets = []; showError('资产列表加载失败：' + error.message); }
    else state.assets = asArray(data.assets);
    renderAssets(error);
  }, force);
}
function loadReport(force) {
  const jobId = state.reportJob || '';
  // 作业报告取**它自己所属的目标**，不是当前选中的目标（见 reportPath 的注释）。
  const owner = jobId ? ((jobOf(jobId) || {}).engagementId || state.eid) : state.eid;
  const path = reportPath(jobId, owner);
  // 缓存必须按**文档**分辨：`loaded.report` 只记得"取过一篇"，它不知道取的是哪一篇。
  // 换了一篇（换任务 / 换目标 / 从作业报告回汇总）却不重取的表现是**正文停在上一篇
  // markdown 上**，而卡头读的是 `reportDoc`——两处各说一份文档。这条路没有报错、
  // 也没有空态，只是内容错了，所以判据只能落在"这一篇取过没有"上。
  const switchedDoc = state.reportPath !== path;
  state.reportPath = path;
  return loadResource('report', path, (data, error) => {
    if (error) {
      state.markdown = '';
      state.reportDoc = null;
      showError((jobId ? `作业报告 ${jobId} 加载失败：` : '报告加载失败：') + error.message);
      setPanelMessage('#report-body', errorBlock('报告加载失败：' + error.message));
      $('#report-meta').textContent = '';
      renderReportDocHead();
      return;
    }
    state.markdown = String(data.markdown || '');
    // 卡头读**响应里**的那一份，不猜：`state.jobs` 可能还没加载出来（那时按作业号
    // 找不到 job），而卡头写着「本目标汇总」、正文却是某次任务的报告，是最糟的一种
    // 不一致——两处都言之凿凿，读者只能猜该信哪个。
    state.reportDoc = {jobId: String(data.jobId || ''), eid: String(data.engagementId || state.eid || ''),
                       job: data.job || null};
    renderReport();
  }, force || switchedDoc);
}
/** 加载**全局链路**：全部 engagement 合并成一张图（`store.combined_view`）。
 *
 *  它和当前选中的目标无关，所以切换目标**不重取**这张图——那是"链路取消使用
 *  engagement 方式"的直接后果，也是它值得单独一条加载路径的原因。
 */
async function loadGraph(force) {
  if (force) state.graphLoaded = false;
  if (state.graphLoaded) { renderGraph(); return; }
  if (state.graphInflight) return state.graphInflight;
  // **永远取全部**。窗口是服务端切的（会连带保留锚点与每个目标的 goal），所以
  // "要多少节点"只能作为查询参数发出去，不能在前端过滤——前端过滤会把边一起丢掉，
  // 留下一堆孤立节点，看上去像链路本来就是断的。上限撤掉之后这里只剩一个取值。
  // 范围：默认只看当前目标（一条链路、一列目标、一列意图……），只有选了
  // 「全部目标」才取合并视图。合并视图会把 20 多个目标的节点画进同一张网，
  // 而分列布局按节点数增长——那不是「信息更全」，是「看不出这一步属于谁」。
  const single = state.graphScope === 'single' && state.eid;
  state.graphInflight = single
    ? apiGet('/engagements/' + encodeURIComponent(state.eid))
    : apiGet('/graph?nodes=all')
    .then((view) => {
      Object.assign(state, { graph: view, nodeIndex: buildNodeIndex(view), graphLoaded: true });
      renderGraphStats(view);
      renderGraph();
    })
    .catch((error) => {
      // **手上已经有图的时候不要清空它**：自动刷新的一次失败就白屏，比"图旧一点"糟得多。
      // 只有从来没取到过图（初始加载失败）才降级成空态。失败后 `graphLoaded` 保持真，
      // 于是切页签不会立刻再打一次；下一次指纹变化（或手点「刷新链路」）会再试。
      if (!state.graph) {
        Object.assign(state, { graph: null, nodeIndex: new Map(), graphLoaded: false });
        renderGraph();
        $('#stat-nodes').textContent = '节点 —';
        $('#stat-edges').textContent = '边 —';
        $('#truncate-banner').classList.add('hidden');
      } else {
        state.graphLoaded = true;
      }
      showError('链路加载失败：' + error.message);
    })
    .finally(() => { state.graphInflight = null; });
  return state.graphInflight;
}
/* ------------------------------------------------------ 9. 编排面板 ---- */

/** 右栏第二块：目标 / 意图 / 约束 / 暂停。
 *
 *  它回答「接下来往哪儿走」，与图回答的「已经发现了什么」是两件事，所以不画在
 *  图上，也不是只读的：加目标、开意图、改状态、立约束都在这里做完，写完立刻重取
 *  链路（新节点要出现在图上）。
 *
 *  为什么不做成「在图上拖拽」：图是**证据**，它的形状由记录之间的语义决定
 *  （spawns / yields / proves / covers），拖一条边不会让某件事变得成立。能编排的
 *  是「接下来做什么」——目标是它，意图是它的下一层，约束是它的边界。
 */
function setOrchMessage(text, kind) {
  state.orchMessage = { text: text || '', kind: kind || '' };
  const node = document.querySelector('.orch-message');
  // 面板还没画（或刚被重画）时没有节点可写：文案留在 state 里，下一次
  // renderOrchestration 会把它带上。**这就是不用 id 取节点的理由**——节点
  // 是渲染的产物，随渲染消失，而「刚才那条操作成功了」得活过这一次重画。
  if (!node) return;
  node.textContent = state.orchMessage.text;
  node.className = 'orch-message' + (kind ? ' ' + kind : '');
}

async function loadOrchestration(force) {
  if (!state.eid) {
    Object.assign(state, { orchestration: null, orchestrationEid: '' });
    renderOrchestration();
    return Promise.resolve();
  }
  if (!force && state.orchestrationEid === state.eid && state.orchestration) {
    renderOrchestration();
    return Promise.resolve();
  }
  try {
    const data = await apiGet('/engagements/' + encodeURIComponent(state.eid) + '/orchestration');
    Object.assign(state, { orchestration: data, orchestrationEid: state.eid });
  } catch (error) {
    Object.assign(state, { orchestration: null, orchestrationEid: '' });
    setOrchMessage('编排信息加载失败：' + error.message, 'error');
  }
  renderOrchestration();
  return Promise.resolve();
}

function orchButton(text, onClick, className) {
  const button = el('button', className || 'btn xs ghost', text);
  button.type = 'button';
  button.addEventListener('click', onClick);
  return button;
}

function orchRow(rows, row) {
  rows.append(row);
}

function orchSection(title, hint, rows) {
  const block = el('section', 'orch-block');
  const head = el('div', 'orch-block-head');
  head.append(el('strong', '', title));
  if (hint) head.append(el('span', 'orch-hint', hint));
  block.append(head);
  if (rows.childNodes.length) block.append(rows);
  else block.append(el('p', 'orch-empty', '还没有记录'));
  return block;
}

/** 一次写操作 + 重取。两个都重取：新节点要上图，计数要跟上。 */
async function orchAction(path, body, done) {
  try {
    await apiPost(path, body || {});
    setOrchMessage(done || '已保存', 'ok');
    await loadOrchestration(true);
    await loadGraph(true);
  } catch (error) {
    setOrchMessage(error.message, 'error');
  }
}

function orchPath(suffix) {
  return '/engagements/' + encodeURIComponent(state.eid) + suffix;
}

function renderOrchestration() {
  const body = $('#orchestration-body');
  if (!body) return;
  clear(body);
  if (!state.eid) {
    body.append(el('p', 'muted', '编排是按目标的：先在左侧选中一个目标，再给它加目标、开意图、立约束。'));
    return;
  }
  const data = state.orchestration;
  if (!data) {
    // 加载失败也走这里：失败后不会有人再来改这段文案，只说「正在加载」等于
    // 永远在加载。state.orchMessage 里那句话是 loadOrchestration 的 catch 留下的。
    const pending = state.orchMessage || { text: '', kind: '' };
    body.append(el('div', 'orch-message' + (pending.kind ? ' ' + pending.kind : ''),
      pending.text || '正在加载编排信息…'));
    return;
  }
  const counts = data.counts || {};

  const head = el('div', 'orch-head');
  head.append(el('span', 'orch-state' + (data.paused ? ' paused' : ''),
    data.paused ? '已暂停' : '进行中'));
  head.append(el('span', 'orch-counts',
    '目标 ' + (counts.goals || 0) + '（达成 ' + (counts.goalsMet || 0) + '）'
    + ' · 意图 ' + (counts.intents || 0) + '（活跃 ' + (counts.intentsOpen || 0)
    + ' / 阻塞 ' + (counts.intentsBlocked || 0) + '）'
    + ' · 约束 ' + (counts.constraints || 0)));
  head.append(orchButton(data.paused ? '恢复探索' : '暂停探索',
    () => orchAction(orchPath(data.paused ? '/resume' : '/pause'), {},
      data.paused ? '探索已恢复' : '探索已暂停'), 'btn sm'));
  body.append(head);
  const message = state.orchMessage || { text: '', kind: '' };
  body.append(el('div', 'orch-message' + (message.kind ? ' ' + message.kind : ''),
    message.text));

  // 目标：规划器按它派发意图，达成一条就标记它。
  const goalRows = el('ul', 'orch-list');
  (data.goals || []).forEach((goal) => {
    const row = el('li', 'orch-row');
    row.append(el('span', 'orch-badge kind-goal', goal.stateLabel || goal.state));
    row.append(el('span', 'orch-text', goal.text || goal.id));
    if (goal.state !== 'met') {
      row.append(orchButton('标记达成', () => orchAction(
        orchPath('/nodes/' + encodeURIComponent(goal.id) + '/state'),
        { state: 'met' }, '目标已标记达成')));
    }
    goalRows.append(row);
  });
  const goalSection = orchSection('目标', '规划器据此派发意图、评估完成', goalRows);
  const goalForm = el('form', 'orch-form');
  const goalInput = el('input', 'input');
  goalInput.type = 'text';
  goalInput.placeholder = '加一条可验证的目标，例如「拿到后台管理员权限」';
  const goalSubmit = el('button', 'btn sm', '加目标');
  goalSubmit.type = 'submit';
  goalForm.append(goalInput, goalSubmit);
  goalForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = (goalInput.value || '').trim();
    if (!text) return;
    goalInput.value = '';
    orchAction(orchPath('/goals'), { text: text }, '目标已添加');
  });
  goalSection.append(goalForm);
  body.append(goalSection);

  // 意图：能开、能改状态、能删。这是人在对话之外介入探索的主要入口。
  const intentRows = el('ul', 'orch-list');
  (data.intents || []).forEach((intent) => {
    const row = el('li', 'orch-row');
    row.append(el('span', 'orch-badge state-' + (intent.state || ''),
      intent.stateLabel || intent.state || ''));
    row.append(el('span', 'orch-text', intent.title || intent.id));
    const select = el('select', 'select xs-select');
    (data.intentStates || []).forEach((value) => {
      const option = el('option', '', STATE_LABEL[value] || value);
      option.value = value;
      if (value === intent.state) option.selected = true;
      select.append(option);
    });
    select.addEventListener('change', () => orchAction(
      orchPath('/nodes/' + encodeURIComponent(intent.id) + '/state'),
      { state: select.value }, '状态已改为「' + (STATE_LABEL[select.value] || select.value) + '」'));
    row.append(select);
    row.append(orchButton('删除', () => orchAction(
      orchPath('/nodes/' + encodeURIComponent(intent.id) + '/delete'), {}, '意图已删除')));
    intentRows.append(row);
  });
  const intentSection = orchSection('意图', '一条意图 = 一个待验的方向', intentRows);
  const intentForm = el('form', 'orch-form');
  const intentInput = el('input', 'input');
  intentInput.type = 'text';
  intentInput.placeholder = '开一条后续意图，例如「枚举 /api 未授权接口」';
  const intentGoal = el('select', 'select xs-select');
  (data.goals || []).forEach((goal) => {
    const option = el('option', '', goal.id + ' · ' + (goal.text || '').slice(0, 18));
    option.value = goal.id;
    intentGoal.append(option);
  });
  const intentSubmit = el('button', 'btn sm', '开意图');
  intentSubmit.type = 'submit';
  intentForm.append(intentInput, intentGoal, intentSubmit);
  intentForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const title = (intentInput.value || '').trim();
    if (!title) return;
    intentInput.value = '';
    orchAction(orchPath('/intents'), { title: title, goalId: intentGoal.value }, '意图已创建');
  });
  intentSection.append(intentForm);
  body.append(intentSection);

  // 约束：allow / deny，注入到 Agent 指令里定义探索边界。
  const constraintRows = el('ul', 'orch-list');
  (data.constraints || []).forEach((item) => {
    const row = el('li', 'orch-row');
    row.append(el('span', 'orch-badge constraint-' + item.kind, item.kindLabel || item.kind));
    row.append(el('span', 'orch-text', item.text));
    row.append(orchButton('删除', () => orchAction(
      orchPath('/constraints/' + encodeURIComponent(item.id) + '/delete'), {}, '约束已删除')));
    constraintRows.append(row);
  });
  const constraintSection = orchSection('运行约束', '例如「只测当前端口」「禁止爆破」', constraintRows);
  const constraintForm = el('form', 'orch-form');
  const kindSelect = el('select', 'select xs-select');
  (data.constraintKinds || []).forEach((item) => {
    const option = el('option', '', item.label);
    option.value = item.value;
    kindSelect.append(option);
  });
  const constraintInput = el('input', 'input');
  constraintInput.type = 'text';
  constraintInput.placeholder = '写一条边界，例如「禁止 brute force」';
  const constraintSubmit = el('button', 'btn sm', '加约束');
  constraintSubmit.type = 'submit';
  constraintForm.append(kindSelect, constraintInput, constraintSubmit);
  constraintForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = (constraintInput.value || '').trim();
    if (!text) return;
    constraintInput.value = '';
    orchAction(orchPath('/constraints'), { kind: kindSelect.value, text: text }, '约束已添加');
  });
  constraintSection.append(constraintForm);
  body.append(constraintSection);
}

function bindOrchestrationEvents() {
  const single = $('#graph-scope-single'), all = $('#graph-scope-all');
  const applyScope = (scope) => {
    state.graphScope = scope;
    state.graphLoaded = false;
    [single, all].forEach((button) => {
      if (!button) return;
      const on = button.dataset.scope === scope;
      button.classList.toggle('on', on);
      button.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    loadGraph(true);
    loadOrchestration(false);
  };
  [single, all].forEach((button) => {
    if (button) button.addEventListener('click', () => applyScope(button.dataset.scope));
  });
  // 右栏两块：节点详情 / 编排。它们共用同一栏宽度，所以只能一次显示一块。
  $$('.side-tab').forEach((button) => {
    button.addEventListener('click', () => {
      const side = button.dataset.side;
      $$('.side-tab').forEach((item) => item.classList.toggle('on', item === button));
      $('#node-detail-body').classList.toggle('hidden', side !== 'detail');
      $('#orchestration-body').classList.toggle('hidden', side !== 'orchestration');
      if (side === 'orchestration') loadOrchestration(false);
    });
  });
}
/** 切换目标：只影响「漏洞 / 资产 / 报告」的范围；链路是全局的，不跟着重置。 */
async function selectEngagement(eid) {
  Object.assign(state, {
    eid, findings: [], findingGroups: [], assets: [], markdown: '',
    // **只在这里**把报告文档重置回汇总：换目标是一个明确的"我要看这个目标"的动作，
    // 报告页的范围跟着它走。而目录里点一次任务只换文档、不换目标（见 showReport）——
    // 两件事的语义不同，所以重置只发生在前者。
    reportJob: '', reportDoc: null,
    selectedAssetId: null, inflight: {},
    loaded: { findings: false, assets: false, report: false },
  });
  // 【地址栏不再承载目标】这里原来是 `?engagement=<id>` 的**唯一写入点**：每选一次
  // 目标就把它 replaceState 进 URL。撤掉它的理由不是"不好看"——那个后缀让每一次
  // 启动的地址都长得不一样，而它携带的信息（当前是哪个目标）对话侧边栏那块已经
  // 写着，而且更具体（扫了哪些地址、多少条任务）。两处里唯一会被复制、粘贴、存成
  // 书签、出现在访问日志里的就是 URL，于是它成了最容易过期的那个真相源。
  // 现在目标只有一个来路：`loadEngagements()` 按列表定位；地址栏永远是根路径。
  // 漏洞页若收窄在某个目标上，换目标就跟着换（"我看的是这个目标"这个意图不变）；
  // 「全部目标」则保持全部。左栏高亮由 `renderFindingsTargets` 重画。
  if (state.findingsScope !== '') state.findingsScope = eid;
  // 单目标视图是「这个目标的链路」，换目标必须重取；合并视图与目标无关，
  // 保持原样——这正是两种范围该有的差别。
  if (state.graphScope === 'single') { state.graphLoaded = false; }
  state.orchestrationEid = '';
  renderScopeStats(eid);
  renderFindingsTargets();
  const panels = { findings: '#findings-list', assets: '#asset-tree', report: '#report-body' };
  Object.keys(panels).forEach((name) => setPanelMessage(panels[name],
    emptyBlock(name === state.tab ? LOADING_TEXT[name] : '尚未加载，切换页签后自动拉取')));
  setPanelMessage('#asset-detail', emptyBlock('请在左侧选择资产'));
  loadTabData(state.tab);
}
/** 按页签按需加载数据；部署与作业页签都与 engagement 无关。
 *
 *  任务读数的自动刷新跟着**三个**页签走：作业页签（完整列表）、对话页签（上下文
 *  栏里那一块）、报告页签（左侧的作业报告目录）。后两处都不是作业页签，不接进去
 *  就会停在进入那一刻的内容——而目录里那几行状态正是"这个任务跑完了没有"的唯一
 *  读数。其余页签一律停：多留在后台跑一轮就是一次没人看的请求。
 *
 *  报告页接进来还有一个副作用要记住：**每 10 秒重画一次目录**。`renderReportSide`
 *  因此会把容器的滚动位置记下来再还回去，否则"我正在读的那一条"会被顶回顶部。
 */
function loadTabData(tab) {
  if (tab !== 'jobs' && tab !== 'dsh' && tab !== 'report') stopJobsAuto();
  // 心跳跟着**看得见那些读数的页签**走（哪几个、为什么——见 `startPulseAuto`）。
  // 链路页看 `state.graphAuto`：页头那颗「自动刷新」芯片关掉之后，这一页就回到
  // "靠手点刷新链路"的老样子，心跳也不必在后台空转。
  // 仪表盘**不在**这个名单里，与链路页的取舍方向相反：它整页都是读数与列表，
  // 每 3 秒重画一次的表现是"列表在手底下闪"，而这一页回答的是"现在有什么"，
  // 切进来时那一次加载已经够新。要给新数据就按页头那颗「刷新」。
  // 流量页也接进来（m14866「流量根据会话任务实时展示」）：那一页列的是**发出去过的
  // 请求**，而请求是模型在对话里跑工具时发出去的——人在流量页上等的正是"又发了一条"。
  // 不接的话它只在切进来的那一刻读一次，之后整个扫描跑完都不动。
  // 请求重发页也从这一轮里拿东西：它右栏挂的就是流量列表（点一行填进左边那张表），
  // 而人在这一页上等的正是「刚才发出去的那条回来了没有」。不接的话，这一页的列表
  // 只停在切进来那一刻——发完一条自己看不见自己。
  const pulseTab = tab === 'dsh' || tab === 'findings' || tab === 'assets'
    || tab === 'traffic' || tab === 'repeater' || (tab === 'graph' && state.graphAuto);
  if (pulseTab) startPulseAuto();
  else stopPulseAuto();
  if (tab === 'jobs') {
    loadJobsTab(false);
    loadJobs(true, true);                                // 每次进入都刷一次任务，其余走缓存
    startJobsAuto();
    return;
  }
  if (tab === 'dsh') {
    // 对话页签自己管会话，没有要按目标加载的数据；但上下文栏那一块任务读数要有
    // 东西可显示、而且要会自己长。**必须排在下面 `!state.eid` 那个早退之前**——
    // 没有选中目标时它照样该显示，与链路页签同一条理由。
    watchJobs();
    return;
  }
  // 新增的八页（仪表盘 / 任务 / 计划任务 / 流量 / 请求重发 / 工作空间 /
  // 审批记录 / 日志）由 `panels.js` 自己渲染，**同样排在 `!state.eid` 早退之前**：
  // 它们回答的是"整个平台上发生了什么"，与有没有选中某个目标无关。
  //
  if (tab === 'dashboard' || tab === 'tasks' || tab === 'schedules'
      || tab === 'traffic' || tab === 'repeater' || tab === 'workspace'
      || tab === 'approvals' || tab === 'logs') {
    if (window.PentestPanels) window.PentestPanels.load(tab);
    return;
  }
  if (tab === 'setup') { loadSetupTab(false); return; }
  // 设置页：重放当前分组。Skills 住在设置里，技能目录只在切到那一组时才拉
  // （另有 load() 开头的一次预取，见 settings.js）——所以每次进设置页
  // 都要把分组再报一遍，否则离开再回来，那一组会停在最后一次渲染的旧读数上。
  if (tab === 'settings') {
    if (window.PentestSettings && window.PentestSettings.enter) window.PentestSettings.enter();
    return;
  }
  // 链路是**全局合并视图**，不跟着 eid 变：**没有选中目标时它照样该显示**，所以
  // 这一支排在 eid 判空之前。变新的活主要交给心跳（`pumpPulse` 判到节点/目标
  // 指纹变了才整图重取，见 `startPulseAuto`）。
  //
  // 但指纹有两处**看不见的**变化，只能靠"切进来就重取"补：
  //   * 目标改名是 UPDATE——`engagements` 指纹只数行数与最大行号（见
  //     `store.change_stamps`），行数没动它就不动，而链路里的目标节点标签、
  //     对话页的目标列表读的都是这一行；
  //   * 在原行上就地补写节点内容（启动自愈给旧漏洞补 verification），行数与
  //     最大 rowid 同样不动。
  // 重取现在不再把人拽回原点：`renderGraph` 只在"从没手动动过视角"时才重新适配
  // （见那里的注释）。自动刷新关掉时仍旧走缓存——那颗芯片的语义就是"这一页由我
  // 手点"，这时不该有后台请求。
  if (tab === 'graph') { loadGraph(state.graphAuto); loadOrchestration(false); return; }
  if (tab === 'report') {
    // 目录列的是**全部**任务，所以它同样不该等 `state.eid`：没有选中目标时那一行
    // 「本目标汇总」会明说"未选择目标"，而下面那些任务照常列出来。
    watchJobs();
    loadReport(false);
    return;
  }
  if (!state.eid) return;
  // 漏洞 / 资产每次切进来都**强制重拉**。心跳只重拉"当前页签"，而人常常是在「会话」
  // 里等一次扫描跑完再切过来：那期间的变化已经被心跳记进指纹（`state.pulse`），正文却还是
  // 上一次退出时留下的缓存，于是"切回来看到旧的、按 F5 才对"。缓存只在同一页签内复用
  // （心跳与分页），跨页签进入不再相信它。
  if (tab === 'findings') {
    // 漏洞卡片要读资产名，所以资产永远跟着拉；而正文取哪一份由**漏洞页的范围**
    // 决定——「全部目标」时那份聚合快照与当前目标无关（左栏可能正停在别的目标上）。
    loadAssets(true);
    if (state.findingsScope === '') loadAllFindings(true); else loadFindings(true);
  }
  else if (tab === 'assets') loadAssets(true);
}

/* ------------------------------------------------------------ 9. 漏洞 ---- */
function assetLabel(assetId) {
  if (!assetId) return '—';
  const asset = state.assets.find((item) => item.id === assetId);
  if (!asset) return assetId;
  const type = assetTypeWord(asset);
  return `${asset.value || asset.id}${type ? `（${type}）` : ''}`;
}
/* 筛选判据对**两种形状**都成立：分组（`state.findingGroups` 里的一项）与单条
 * 记录。分组那条路径是常态，单条那条是 `groups` 缺席时的退路（见 `paintFindings`）。
 *
 * 为什么关键词要问**整组**：使用者搜的是一个端口号或一段 banner，而他看到的是
 * 卡片。按组里某一条判会出现"筛掉了同组的两条、留下一条"，那张卡片上的「3 处」
 * 随即变成谎话——而那个数正是分组要传达的东西。
 */
const matchSeverity = (item) => state.severityFilter.has(severityOf(item.severity));
function matchKeyword(item, byId, labelOf) {
  const keyword = state.keyword.trim().toLowerCase();
  if (!keyword) return true;
  // 跨目标视图里"影响资产"要读**那个目标自己**的资产名，所以标签函数是参数：
  // 直接调 `assetLabel` 只能查到当前选中目标的资产树。
  const label = labelOf || assetLabel;
  const records = item.ids
    ? item.ids.map((id) => byId.get(id)).filter(Boolean)
    : [item];
  const fields = [item.title, item.severity];
  records.forEach((record) => {
    fields.push(record.id, record.title, record.description, record.affectedAssetId,
      label(record.affectedAssetId), asArray(record.reproducibleSteps).join(' '));
  });
  return fields.filter(Boolean).join(' ').toLowerCase().includes(keyword);
}
/** 严重度倒序（critical → info），同档按创建时间新 → 旧。
 *
 *  **分组视图不用它排序**：服务端返回的 `groups` 已经按同一个规则排好了（它排的
 *  是 `findings`，组的先后顺序跟着那个顺序走）。前端再排一遍就是第二处排序规则，
 *  两处迟早不一致。这个函数只服务 `groups` 缺席时的平铺退路。
 */
function compareSeverity(a, b) {
  const diff = SEVERITY_ORDER.indexOf(severityOf(a.severity)) - SEVERITY_ORDER.indexOf(severityOf(b.severity));
  return diff !== 0 ? diff : (Number(b.createdAt) || 0) - (Number(a.createdAt) || 0);
}
/** 复现步骤折叠块。单条卡片与组内实例共用一份——两处各写一遍，迟早只有一处被改。 */
function stepsBlock(finding) {
  const steps = asArray(finding.reproducibleSteps), details = el('details', 'finding-steps');
  details.open = steps.length <= 5;                          // 长步骤默认收起，保住"先扫一遍"的首屏
  details.append(el('summary', null, `可复现步骤（${steps.length}）`));
  if (steps.length) {
    const list = el('ol', 'steps');
    steps.forEach((step) => list.append(el('li', null, step)));
    details.append(list);
  } else details.append(el('p', 'empty', '该记录未提供可复现步骤'));
  return details;
}
/** 这条算不算**已证实**。
 *
 *  服务端现在把 `verified` 直接放在分组上、把 `verification` 放在记录上；两个都读
 *  是因为页面与服务端会**各升各的**（升级先到一边是常态）。读不到的字段读作未证实
 *  ——这与服务端的判据同源（没有证据等级记录就是未证实），不会把一条真已证的漏洞
 *  悄悄升成可信，只会多让它进一次复核。
 */
function findingVerified(item) {
  if (typeof item.verified === 'boolean') return item.verified;
  const raw = item.verification;
  return !!(raw && raw.verdict === 'verified');
}
/** 卡片头那枚等级徽章。色以外的差异靠文字：未证实那枚多一个「待复核」。 */
function gradeChip(verified) {
  return el('span', verified ? 'finding-grade' : 'finding-grade is-unverified',
            verified ? '已证实' : '未证实');
}
/** “凭什么算数”那一行。
 *
 *  判据原先只存在数据库里（`verification.method/note`），而页面上只有一枚徽章——
 *  于是「已证实」三个字之外什么都没说，读者只能去翻对话记录。这一行把「怎么证的」
 *  和「证到了什么」直接写在卡片上，也是“已证实不该只是一句话”的一层约束。
 */
function verificationBasis(finding) {
  const raw = finding.verification;
  if (!raw || !raw.verdict) return '';
  const parts = [raw.method, raw.note].filter(Boolean);
  return parts.length ? '依据：' + parts.join(' · ') : '';
}
function findingCard(finding, labelOf) {
  const sev = severityOf(finding.severity), card = el('article', `finding-card sev-${sev}`);
  const verified = findingVerified(finding);
  const label = labelOf || assetLabel;
  const head = el('header', 'finding-head'), meta = el('div', 'finding-meta');
  head.append(el('span', `sev-badge sev-${sev}`, SEVERITY_LABEL[sev]), gradeChip(verified),
    el('h3', 'finding-title', finding.title || '（无标题）'),
    el('span', 'finding-id', finding.id || ''));
  meta.append(el('span', 'muted', '影响资产：' + label(finding.affectedAssetId)),
    el('span', 'muted', '发现时间：' + formatTime(finding.createdAt)));
  if (finding.intentId) meta.append(el('span', 'muted', '来源意图：' + finding.intentId));
  const basis = verificationBasis(finding);
  if (basis) meta.append(el('span', 'muted finding-basis', basis));
  card.append(head, meta);
  if (finding.description) {
    const desc = el('p', 'finding-desc', finding.description);
    desc.title = finding.description;                        // CSS 只留 4 行，完整描述靠 title 找回
    card.append(desc);
  }
  card.append(stepsBlock(finding));
  return card;
}
/** 组里**一条**记录的正文：资产 · 时间 · 编号 + 描述 + 步骤。
 *
 *  刻意不重复等级徽章与标题：同一组里它们逐字相同，重复三遍只会把"这是三条"这件
 *  事重新糊掉——而分组要传达的恰好是"一处问题、三处证据"。
 */
function findingInstanceBlock(finding, labelOf) {
  const block = el('div', 'finding-instance'), head = el('div', 'finding-instance-head');
  const label = labelOf || assetLabel;
  head.append(el('span', 'muted', label(finding.affectedAssetId)),
    el('span', 'muted', formatTime(finding.createdAt)),
    el('span', 'finding-id', finding.id || ''));
  block.append(head);
  const basis = verificationBasis(finding);
  if (basis) block.append(el('p', 'finding-basis', basis));
  if (finding.description) {
    const desc = el('p', 'finding-desc', finding.description);
    desc.title = finding.description;
    block.append(desc);
  }
  block.append(stepsBlock(finding));
  return block;
}
/** 一组同问题的卡片。`count === 1` 时**照旧**渲染单条卡片。
 *
 *  单条不换样子是有意的：绝大多数问题只有一处证据，而"展开三次才读得到正文"是
 *  给多数情况强加的代价，只为让少数情况统一。
 */
function findingGroupCard(group, byId, labelOf) {
  const instances = group.ids.map((id) => byId.get(id)).filter(Boolean);
  if (instances.length <= 1) return instances.length ? findingCard(instances[0], labelOf) : null;
  const sev = severityOf(group.severity), card = el('article', `finding-card sev-${sev}`);
  const verified = typeof group.verified === 'boolean'
    ? group.verified : findingVerified(instances[0]);
  const head = el('header', 'finding-head'), meta = el('div', 'finding-meta');
  const idLabel = el('span', 'finding-id', group.ids[0] || '');
  idLabel.title = `同组记录（${group.count} 条）：` + group.ids.join('、');
  head.append(el('span', `sev-badge sev-${sev}`, SEVERITY_LABEL[sev]), gradeChip(verified),
    el('h3', 'finding-title', group.title || '（无标题）'),
    el('span', 'finding-count', `${group.count} 处`), idLabel);
  const label = labelOf || assetLabel;
  const labels = group.assetIds.map(label);
  const affected = labels.length
    ? labels.slice(0, 2).join('、') + (labels.length > 2 ? ` 等 ${labels.length} 个资产` : '')
    : '—';
  meta.append(el('span', 'muted', '影响资产：' + affected),
    el('span', 'muted', `首见：${formatTime(group.firstAt)}`));
  if (group.intentIds.length) meta.append(el('span', 'muted', '来源意图：' + group.intentIds.join('、')));
  card.append(head, meta);
  const details = el('details', 'finding-instances');
  details.open = group.count <= 5;
  details.append(el('summary', null, `${group.count} 处记录（同问题、不同资产，各留原文）`));
  instances.forEach((instance) => details.append(findingInstanceBlock(instance, labelOf)));
  card.append(details);
  return card;
}
/** 重画时不改变正在看的位置。
 *
 *  漏洞列表会在**数据变了**的时候整段重画（心跳驱动的自动刷新），而此刻使用者很
 *  可能正读到第五条。位置若由重画决定，表现就是"新证据一到、页面跳回顶部"——那会
 *  让人不敢开着这个页签。滚动容器在上层（`.panel-body` 或整页），所以从容器往上
 *  找第一个**真能滚**的祖先，而不是假定某一个选择器。
 */
function scrollHostOf(node) {
  for (let current = node; current; current = current.parentElement) {
    const style = getComputedStyle(current);
    if (/(auto|scroll|overlay)/.test(style.overflowY) && current.scrollHeight > current.clientHeight) {
      return current;
    }
  }
  return null;
}
/** 漏洞页的一段（「已证实」或「未证实」）。
 *
 *  为什么是两段而不是给未证实那批换一种卡片：这仍然是**同一份台账**，两个分类
 *  只是排序视图。换卡片形状会让人以为它们是两种东西（两套字段、两种导出），
 *  而它们只差一个"有没有证据链"。分区标题 + 卡片上一枚等级徽章就够说清这件事。
 */
function findingSection(title, hint, verified, nodes, unit) {
  const section = el('section', verified ? 'finding-section' : 'finding-section is-unverified');
  const head = el('div', 'finding-section-head');
  head.append(el('h2', 'finding-section-title', title),
    el('span', 'finding-section-count', `${nodes.length} ${unit}`),
    el('span', 'finding-section-hint', hint));
  section.append(head);
  nodes.forEach((node) => section.append(node));
  return section;
}
/** 两段都画出来（哪一段空就只画另一段；两段都空在调用处已经提前返回）。 */
function paintFindingSections(wrap, yes, no, unit) {
  if (yes.length) {
    wrap.append(findingSection('已证实', '亲手复现过、有证据链', true, yes, unit));
  }
  if (no.length) {
    wrap.append(findingSection('未证实', '静态匹配或未复现，待人工核对', false, no, unit));
  }
}
/** 「全部目标」视图：一个目标一段，段内仍是「已证实 / 未证实」两段。
 *
 *  为什么不复用 `paintFindings` 的单目标路径：那是"一个目标的台账"，抬头只有两段；
 *  这里多一层目标标题。复用 `paintFindingSections` 是有意的——卡片形状、等级徽章、
 *  两段判据只有一份实现，两个视图只差外层容器。
 *
 *  `labelOf` 读的是**那段自己**的 `assetLabels`：跨目标时界面手上只有当前目标的
 *  资产树，直接查别的目标会把资产名全部退化成 `asset-xxxx`。
 */
function paintGroupedFindings(wrap) {
  const buckets = state.findingsByTarget;
  if (!buckets.length) {
    wrap.append(emptyBlock(state.allFindingsLoaded
      ? '还没有任何目标产出漏洞' : '正在读取全部目标的漏洞…'));
    return;
  }
  let total = 0;
  buckets.forEach((bucket) => {
    const findings = asArray(bucket.findings);
    const byId = new Map(findings.map((item) => [item.id, item]));
    const labelOf = (assetId) => (bucket.assetLabels || {})[assetId] || assetLabel(assetId);
    const grouped = asArray(bucket.groups).length > 0;
    const cards = grouped
      ? asArray(bucket.groups).filter(matchSeverity)
          .filter((group) => matchKeyword(group, byId, labelOf))
          .map((group) => ({ group, card: findingGroupCard(group, byId, labelOf) }))
          .filter((entry) => entry.card)
          .map((entry) => ({ group: entry.group, node: entry.card }))
      : findings.filter(matchSeverity)
          .filter((item) => matchKeyword(item, byId, labelOf)).sort(compareSeverity)
          .map((item) => ({ group: item, node: findingCard(item, labelOf) }));
    if (!cards.length) return;
    total += cards.length;
    const section = el('section', 'finding-target-group');
    const head = el('header', 'finding-target-head');
    head.append(el('h2', 'finding-target-title', bucket.target || bucket.engagementId || '（未命名目标）'),
      el('span', 'finding-target-count',
        `${cards.length} ${grouped ? '项' : '条'} · 已证实 ${cards.filter((entry) => findingVerified(entry.group)).length}`),
      el('span', 'finding-target-id', bucket.engagementId || ''));
    section.append(head);
    paintFindingSections(section,
      cards.filter((entry) => findingVerified(entry.group)).map((entry) => entry.node),
      cards.filter((entry) => !findingVerified(entry.group)).map((entry) => entry.node),
      grouped ? '项' : '条');
    wrap.append(section);
  });
  if (!total) wrap.append(emptyBlock('当前筛选条件下没有匹配的漏洞'));
}
/** 漏洞页左栏的一行：目标名 + 条数。`meta` 为空即「全部目标」那一行。 */
function targetRow(scope, meta) {
  const row = el('button', 'target-row');
  row.type = 'button';
  row.dataset.targetScope = scope;
  const counts = (meta && meta.counts) || {};
  const isAll = scope === '';
  if (isAll) row.classList.add('target-all');
  row.append(el('span', 'target-name', isAll ? '全部目标' : (meta.target || meta.id || '（未命名目标）')));
  const bits = [];
  if (isAll) {
    bits.push(`${(state.engagementList || []).length} 个目标`);
    if (state.findingsTotals) bits.push(`${Number(state.findingsTotals.total) || 0} 条漏洞`);
    else if (!state.allFindingsLoaded) bits.push('读取中…');
  } else {
    bits.push(`${Number(counts.findings) || 0} 条漏洞`);
    bits.push(`${Number(counts.assets) || 0} 个资产`);
  }
  row.append(el('span', 'target-meta', bits.join(' · ')));
  if (scope === state.findingsScope) { row.classList.add('on'); row.setAttribute('aria-current', 'true'); }
  return row;
}
/** 漏洞页左栏：全部目标 + 每个目标一行。
 *  数据是 `state.engagementList`——与侧边栏同一次 `/engagements` 快照，不另发请求；
 *  条数取那条目标自带的 `counts`。 */
function renderFindingsTargets() {
  const host = $('#findings-targets');
  if (!host) return;
  clear(host);
  host.append(targetRow('', null));
  (state.engagementList || []).forEach((meta) => host.append(targetRow(meta.id, meta)));
  if (!(state.engagementList || []).length) {
    host.append(el('p', 'empty', '还没有目标。下发一次作业会自动建一个。'));
  }
}
/** 点左栏：换范围。具体目标走 `selectEngagement`（它同时决定漏洞/资产/报告的范围）；
 *  「全部目标」只是放开范围并确保聚合快照是最新的。 */
async function selectFindingsScope(scope) {
  const next = String(scope || '');
  if (next === state.findingsScope) return;
  state.findingsScope = next;
  if (next) { await selectEngagement(next); return; }
  renderFindingsTargets();
  renderFindings(null);
  await loadAllFindings(false);
  renderScopeStats(state.eid);
}
function renderFindings(error) {
  const wrap = $('#findings-list');
  const host = wrap ? scrollHostOf(wrap) : null, keep = host ? host.scrollTop : 0;
  paintFindings(error);
  if (host) host.scrollTop = keep;
}
function paintFindings(error) {
  const wrap = $('#findings-list');
  clear(wrap);
  renderSeverityChips();
  if (error) { wrap.append(errorBlock('漏洞列表加载失败：' + error.message)); return; }
  // 「全部目标」是另一条渲染路径：它是跨目标的看板，不是某个目标的台账。
  if (state.findingsScope === '') { paintGroupedFindings(wrap); return; }
  if (!state.findings.length) {
    wrap.append(emptyBlock(state.eid ? '该 engagement 暂无漏洞记录' : '请先选择一个 engagement'));
    return;
  }
  const byId = new Map(state.findings.map((item) => [item.id, item]));
  if (!state.findingGroups.length) {
    // `groups` 缺席 → 退回**平铺**渲染，而不是显示"没有匹配的漏洞"。
    // 那不是假想的情况：页面开着的时候服务端升级，旧 JS 拿到的响应里就没有
    // `groups`，而"筛完了什么都不剩"与"这个目标真的没有漏洞"看起来一模一样。
    const items = state.findings.filter(matchSeverity)
      .filter((item) => matchKeyword(item, byId)).sort(compareSeverity);
    if (!items.length) { wrap.append(emptyBlock('当前筛选条件下没有匹配的漏洞')); return; }
    paintFindingSections(wrap,
      items.filter((item) => findingVerified(item)).map((item) => findingCard(item)),
      items.filter((item) => !findingVerified(item)).map((item) => findingCard(item)), '条');
    return;
  }
  const groups = state.findingGroups.filter(matchSeverity)
    .filter((group) => matchKeyword(group, byId));
  if (!groups.length) { wrap.append(emptyBlock('当前筛选条件下没有匹配的漏洞')); return; }
  const cards = groups.map((group) => ({ group, card: findingGroupCard(group, byId) }))
    .filter((entry) => entry.card);
  paintFindingSections(wrap,
    cards.filter((entry) => findingVerified(entry.group)).map((entry) => entry.card),
    cards.filter((entry) => !findingVerified(entry.group)).map((entry) => entry.card), '项');
}

/* ------------------------------------------------------------ 10. 资产 ---- */
/** 依据 parentId 建树；children 字段作为兜底来源。 */
function buildAssetTree(assets) {
  const byId = new Map(assets.map((item) => [item.id, item])), parentOf = new Map(), childrenOf = new Map();
  assets.forEach((item) => { if (item.parentId && byId.has(item.parentId)) parentOf.set(item.id, item.parentId); });
  assets.forEach((item) => asArray(item.children).forEach((childId) => {
    if (byId.has(childId) && !parentOf.has(childId)) parentOf.set(childId, item.id);
  }));
  assets.forEach((item) => {
    const parentId = parentOf.get(item.id) || null;
    childrenOf.set(parentId, [...(childrenOf.get(parentId) || []), item]);
  });
  return { childrenOf, roots: childrenOf.get(null) || [] };
}
function assetButton(asset) {
  const button = el('button', 'tree-btn', asset.value || asset.id);
  button.type = 'button';
  button.dataset.assetId = asset.id;
  button.insertBefore(el('span', 'tree-type', `[${assetTypeWord(asset) || '未知'}]`), button.firstChild);
  return button;
}
/** 单个树节点（含递归子层级），visited 用于环保护。 */
function assetTreeItem(asset, childrenOf, visited) {
  const item = el('li', 'tree-item'), children = asArray(childrenOf.get(asset.id));
  item.append(assetButton(asset));
  if (!children.length) return item;
  const list = el('ul', 'tree-list');
  children.forEach((child) => {
    if (visited.has(child.id)) return;
    visited.add(child.id);
    list.append(assetTreeItem(child, childrenOf, visited));
  });
  if (list.childNodes.length) item.append(list);
  return item;
}
function renderAssetTree() {
  const wrap = $('#asset-tree');
  clear(wrap);
  if (!state.assets.length) {
    wrap.append(emptyBlock(state.eid ? '该 engagement 暂无资产记录' : '请先选择一个 engagement'));
    return;
  }
  const { childrenOf, roots } = buildAssetTree(state.assets), list = el('ul', 'tree-list root');
  if (roots.length) {
    const visited = new Set(roots.map((asset) => asset.id));
    roots.forEach((asset) => list.append(assetTreeItem(asset, childrenOf, visited)));
  } else {
    // 无可判定根（parentId 指向不存在的资产或存在环）时平铺，避免记录丢失
    state.assets.forEach((asset) => list.append(assetTreeItem(asset, childrenOf, new Set())));
  }
  wrap.append(list);
  $$('#asset-tree .tree-btn').forEach((button) => button.classList.toggle('on', button.dataset.assetId === state.selectedAssetId));
}
function renderAssetDetail(assetId) {
  const body = $('#asset-detail'), asset = assetId ? state.assets.find((item) => item.id === assetId) : null;
  clear(body);
  if (!assetId) { body.append(emptyBlock('请在左侧资产树中选择一个资产')); return; }
  if (!asset) { body.append(emptyBlock(`当前数据中找不到资产 ${assetId}`)); return; }
  const head = el('div', 'detail-title');
  head.append(el('span', 'detail-badge', assetTypeWord(asset) || '资产'), el('span', 'detail-id', asset.id));
  body.append(head, kvList(asset));
  const children = asArray(asset.children).length
    ? asArray(asset.children)
    : state.assets.filter((item) => item.parentId === asset.id).map((item) => item.id);
  body.append(el('div', 'detail-section', `子资产（${children.length}）`));
  if (!children.length) { body.append(emptyBlock('该资产没有子资产')); return; }
  const list = el('ul', 'child-list');
  children.forEach((childId) => {
    const child = state.assets.find((item) => item.id === childId), item = el('li');
    const button = el('button', 'child-btn', child ? `${child.value || child.id}（${childId}）` : childId);
    button.type = 'button';
    button.dataset.assetId = childId;
    item.append(button);
    list.append(item);
  });
  body.append(list);
}
function renderAssets(error) {
  if (error) {
    setPanelMessage('#asset-tree', errorBlock('资产列表加载失败：' + error.message));
    renderAssetDetail(null);
    return;
  }
  renderAssetTree();
  if (!state.assets.some((item) => item.id === state.selectedAssetId)) state.selectedAssetId = null;
  renderAssetDetail(state.selectedAssetId);
}
function selectAsset(assetId) {
  state.selectedAssetId = assetId;
  $$('#asset-tree .tree-btn').forEach((button) => button.classList.toggle('on', button.dataset.assetId === assetId));
  renderAssetDetail(assetId);
}

/* ---------------------------------------------------- 11. Markdown 渲染 ---- */
const INLINE_RE = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*\n]+\*|\[([^\]]+)\]\(([^)\s]+)\))/g;
const isFence = (line) => /^\s*```/.test(line);
const isHeading = (line) => /^#{1,6}\s+/.test(line);
const isQuote = (line) => /^\s*>/.test(line);
const isUnordered = (line) => /^\s*[-*+]\s+/.test(line);
const isOrdered = (line) => /^\s*\d+[.)]\s+/.test(line);
const isRule = (line) => /^\s*([-*_])\s*(\1\s*){2,}$/.test(line);
const isTableStart = (lines, i) => /\|/.test(lines[i] || '') && i + 1 < lines.length && /\|/.test(lines[i + 1])
  && /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[i + 1]);
const isPlainLine = (lines, i) => !/^\s*$/.test(lines[i]) && !isFence(lines[i]) && !isHeading(lines[i]) && !isQuote(lines[i])
  && !isUnordered(lines[i]) && !isOrdered(lines[i]) && !isRule(lines[i]) && !isTableStart(lines, i);

/** 行内元素：代码 / 粗体 / 斜体 / 链接 —— 全部 DOM 构造（追加字符串即纯文本节点）。 */
function appendInline(parent, text) {
  const source = String(text === null || text === undefined ? '' : text);
  let cursor = 0, match;
  INLINE_RE.lastIndex = 0;
  while ((match = INLINE_RE.exec(source)) !== null) {
    const token = match[0];
    if (match.index > cursor) parent.append(source.slice(cursor, match.index));
    if (token.startsWith('`')) parent.append(el('code', 'md-inline-code', token.slice(1, -1)));
    else if (token.startsWith('**')) parent.append(el('strong', null, token.slice(2, -2)));
    else if (token.startsWith('*')) parent.append(el('em', null, token.slice(1, -1)));
    else if (token.startsWith('[')) {
      const link = el('a', 'md-link', match[2]), href = match[3];
      link.href = /^(https?:|mailto:|#|\/)/i.test(href) ? href : '#';
      if (/^https?:/i.test(href)) { link.target = '_blank'; link.rel = 'noopener noreferrer'; }
      parent.append(link);
    } else parent.append(token);
    cursor = match.index + token.length;
  }
  if (cursor < source.length) parent.append(source.slice(cursor));
  return parent;
}
const ANY_ITEM_RE = /^(\s*)(\d+[.)]|[-*+])\s+(.*)$/;
/** 列表块（更深缩进的项按子列表嵌套，标记类型可不同）：返回节点与下一行下标。 */
function renderListBlock(lines, start, ordered) {
  const itemRe = ordered ? /^(\s*)\d+[.)]\s+(.*)$/ : /^(\s*)[-*+]\s+(.*)$/;
  const baseIndent = lines[start].match(itemRe)[1].length, list = el(ordered ? 'ol' : 'ul', 'md-list');
  let index = start;
  while (index < lines.length) {
    const any = lines[index].match(ANY_ITEM_RE);
    if (!any || any[1].length < baseIndent) break;
    if (any[1].length > baseIndent) {                    // 归入上一项的子列表
      const nested = renderListBlock(lines, index, isOrdered(lines[index]));
      (list.lastChild || list).append(nested.node);
      index = nested.next;
      continue;
    }
    const match = lines[index].match(itemRe);
    if (!match) break;                                   // 同缩进但换了标记类型：交给外层当成新列表
    const item = el('li');
    appendInline(item, match[2]);
    list.append(item);
    index += 1;
  }
  return { node: list, next: index };
}
const splitTableRow = (line) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
/** 表格块：返回节点与下一行下标。 */
function renderTableBlock(lines, start) {
  const head = el('thead'), headRow = el('tr'), body = el('tbody'), table = el('table', 'md-table');
  splitTableRow(lines[start]).forEach((cell) => {
    const th = el('th');
    appendInline(th, cell);
    headRow.append(th);
  });
  head.append(headRow);
  let index = start + 2;
  while (index < lines.length && /\|/.test(lines[index]) && !/^\s*$/.test(lines[index])) {
    const row = el('tr');
    splitTableRow(lines[index]).forEach((cell) => {
      const td = el('td');
      appendInline(td, cell);
      row.append(td);
    });
    body.append(row);
    index += 1;
  }
  table.append(head, body);
  return { node: table, next: index };
}
/** 极简 Markdown：标题 / 无序列表 / 有序列表 / 表格 / 代码块 / 粗体 / 引用块 / 分割线。 */
function renderMarkdown(markdown) {
  const lines = String(markdown || '').replace(/\r\n?/g, '\n').split('\n');
  const fragment = document.createDocumentFragment();
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    if (isFence(line)) {
      const language = line.trim().replace(/^```/, '').trim(), code = [], pre = el('pre', 'md-code');
      for (index += 1; index < lines.length && !isFence(lines[index]); index += 1) code.push(lines[index]);
      index += 1;                                          // 跳过结束栅栏
      pre.append(el('code', language ? `language-${language}` : null, code.join('\n')));
      fragment.append(pre);
      continue;
    }
    if (isHeading(line)) {
      const heading = el('h' + line.match(/^(#{1,6})/)[1].length, 'md-h');
      appendInline(heading, line.replace(/^#{1,6}\s+/, ''));
      fragment.append(heading);
      index += 1;
      continue;
    }
    if (isRule(line)) { fragment.append(el('hr', 'md-hr')); index += 1; continue; }
    if (isQuote(line)) {
      const quote = [];
      while (index < lines.length && isQuote(lines[index])) { quote.push(lines[index].replace(/^\s*>\s?/, '')); index += 1; }
      const blockquote = el('blockquote', 'md-quote');
      blockquote.append(renderMarkdown(quote.join('\n')));
      fragment.append(blockquote);
      continue;
    }
    if (isTableStart(lines, index)) {
      const table = renderTableBlock(lines, index);
      fragment.append(table.node);
      index = table.next;
      continue;
    }
    if (isUnordered(line) || isOrdered(line)) {
      const list = renderListBlock(lines, index, isOrdered(line));
      fragment.append(list.node);
      index = list.next;
      continue;
    }
    const paragraph = [line];
    for (index += 1; index < lines.length && isPlainLine(lines, index); index += 1) paragraph.push(lines[index]);
    const p = el('p', 'md-p');
    appendInline(p, paragraph.join(' '));
    fragment.append(p);
  }
  return fragment;
}
function renderReport() {
  renderReportDocHead();
  const body = $('#report-body');
  clear(body);
  if (!state.markdown) {
    body.append(emptyBlock(state.eid ? '该 engagement 暂无报告内容' : '请先选择一个 engagement'));
    $('#report-meta').textContent = '';
    return;
  }
  body.append(renderMarkdown(state.markdown));
  $('#report-meta').textContent = `${state.markdown.length} 字符 · ${state.markdown.split('\n').length} 行`;
}
/** 报告正文卡头：写清**当前显示的是哪一份文档**。
 *
 *  目录里点一条之后正文就换掉了，而页头那行读数只写了字数——少了这一行，读者无从
 *  确认自己看的是汇总还是某一次执行。跨目标浏览时它更关键：卡头里带着目标，而当前
 *  选中的目标可能在别处（点一次任务**不切换目标**，见 showReport）。
 */
function renderReportDocHead() {
  const name = $('#report-doc-name');
  if (!name) return;
  const doc = state.reportDoc;
  if (!doc || !doc.jobId) {
    name.textContent = '本目标汇总';
    name.title = state.eid ? `目标 ${state.eid} 名下全部执行合起来的那一篇` : '';
    return;
  }
  const job = doc.job || {};
  // 目标是完整路径时这一行有 60 多个字符，窄屏下会把卡头撑成两三行。省略号 +
  // `title` 里的完整值：这一段要的是"我读的是哪一份"，不是"把路径念完"。
  name.textContent = `作业报告 · ${doc.jobId} · ${job.target || targetOf(doc.eid) || doc.eid}`;
  name.title = name.textContent;
}
/** 复制：优先 Clipboard API，降级 textarea + execCommand。 */
async function copyReport() {
  if (!state.markdown) { showError('暂无可复制的报告内容'); return; }
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(state.markdown);
    else {
      const area = el('textarea');
      area.value = state.markdown;                         // value 赋值不涉及 HTML 解析
      area.setAttribute('readonly', 'readonly');
      area.style.cssText = 'position:fixed;opacity:0';
      document.body.append(area);
      area.select();
      const ok = document.execCommand('copy');
      area.remove();
      if (!ok) throw new Error('浏览器拒绝了复制操作');
    }
    $('#report-meta').textContent = '已复制到剪贴板';
  } catch (error) { showError('复制失败：' + error.message); }
}
function downloadReport() {
  if (!state.markdown) { showError('暂无可下载的报告内容'); return; }
  const link = el('a');
  link.href = URL.createObjectURL(new Blob([state.markdown], { type: 'text/markdown;charset=utf-8' }));
  // 文件名带**作业号**：目录里可以同时存着好几篇报告，下载下来全叫
  // `pentest-report-E-xxxx.md` 的话，落到下载目录里就再也分不出哪份是哪份——
  // 而它们的目标往往还是同一个（同一个目标跑了很多次）。汇总那篇保留目标号，
  // 因为它是"这个目标的报告"，不是某一次执行的。
  link.download = state.reportJob
    ? `pentest-report-${state.reportJob}.md`
    : `pentest-report-${state.eid || 'engagement'}.md`;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

/* ----------------------------------------------- 11b. 作业报告目录 ---- */
/*
 * 每个作业一篇报告：左栏列出**全部作业**（不只当前目标名下那些），点一条读那一次
 * 执行自己的报告。它与「作业」页读的是同一批数据（`state.jobs` 由本模块持有），
 * 但回答的是另一个问题——作业页说"跑到哪了"，这里说"跑出了什么、白纸黑字怎么写的"。
 *
 * ## 为什么目录要列全部作业，而不是只列当前目标的
 *
 * 判断一份报告可信度的第一步，是能一次看到**全部执行记录**：哪几次失败了、
 * 有没有重跑、重跑有没有把结论写脏。而一个目标的报告恰恰常常是"这几次执行合起来
 * 说了什么"。如果目录只列当前目标那几条，跨目标的那部分就被藏起来了——而它正是
 * 解释"为什么这份报告里有几条资产看着陌生"的答案。
 *
 * 代价是必须让**每一行自己说清是哪个目标**，否则跨目标那几条看起来就像这个目标的
 * 任务。所以行内显示目标末段，`title` 里放完整目标与作业号。
 *
 * ## 与「作业」页的排序不同，这是有意的
 *
 * 作业页按**状态**分组排（执行中 → 待执行 → 失败 → 已完成）：那是一个队列，问的是
 * "接下来轮到谁"。这里是**按时间倒序**：那是一个归档，问的是"最近跑过什么"。
 * 同一批数据、两个用途，用同一个排序反而两边都不对。
 */

/** 报告文档路径：`jobId` 为空是**本目标汇总**，否则是那一次执行的作业报告。
 *
 *  作业报告挂在**它自己所属的目标**下面（`eid` 由调用方给），而不是当前选中的目标：
 *  目录可以跨目标浏览，用一个别的目标的 id 去取这份文档，后端会（正确地）拒绝。
 *  这也是"点一次任务不会顺手切换你的目标"能成立的前提。
 */
const reportPath = (jobId, eid) => `/engagements/${encodeURIComponent(eid || state.eid)}/report`
  + (jobId ? `?job=${encodeURIComponent(jobId)}` : '');

/** 作业号 → 任务对象。任务被删掉之后返回 undefined，调用方要自己兜住。 */
const jobOf = (jobId) => state.jobs.find((job) => job.id === jobId) || null;
/** 目标号 → 目标值；列表还没加载出来时退回目标号本身。 */
function targetOf(eid) {
  const meta = state.engagementList.find((item) => item.id === eid);
  return (meta && meta.target) || eid || '';
}
/** 目标值的**短形式**：带路径分隔符的取末段，其余原样。
 *
 *  `basename()` 不能直接用：目标可以是 IP、网段或域名，`basename('10.0.0.0/24')`
 *  会得到 `24`——一个看起来完全正常、只是错的字符串。而在 320px 的目录栏里，
 *  一个 60 字符的完整路径会把一行撑成三行。
 */
function shortTarget(value) {
  const text = String(value === null || value === undefined ? '' : value);
  return /[\\/]/.test(text) ? (basename(text) || text) : text;
}
/** 一次执行往链路里写了几个节点（回传快照，不是当前图的条数）。 */
function writtenTotal(job) {
  const written = job.written || {};
  return ['intent', 'fact', 'finding', 'asset']
    .reduce((sum, key) => sum + (Number(written[key]) || 0), 0);
}

/** 目录里的一行。`job` 为 null 时画的是**本目标汇总**那一行。 */
function reportSideRow(job) {
  const row = el('button', 'rpt-row');
  row.type = 'button';
  const selected = job ? job.id === state.reportJob : !state.reportJob;
  if (selected) row.classList.add('on');
  const head = el('div', 'rpt-head');
  const meta = el('span', 'rpt-meta');

  if (!job) {
    row.dataset.jobId = '';
    head.append(el('span', 'tag tag-accent', '汇总'));
    const name = el('span', 'rpt-target', targetOf(state.eid) || '（未选择目标）');
    name.title = state.eid || '';
    head.append(name);
    const counts = (state.engagementList.find((item) => item.id === state.eid) || {}).counts || {};
    const nodes = ['intents', 'facts', 'findings', 'assets']
      .reduce((sum, key) => sum + (Number(counts[key]) || 0), 0);
    meta.textContent = [state.eid || '—', `${nodes} 节点`, '全部证据合成一篇'].join(' · ');
    row.title = '这个目标名下全部执行合起来的那一篇报告';
  } else {
    const status = jobStatusOf(job);
    row.dataset.jobId = job.id;
    head.append(el('span', `status-badge status-${status}`, JOB_STATUS_LABEL[status]));
    head.append(el('span', 'tag', jobKindLabel(job)));
    const name = el('span', 'rpt-target', basename(job.target) || job.target || '（无目标）');
    name.title = job.target || '';
    head.append(name);
    const bits = [];
    if (job.chainError) bits.push('证据写入失败');
    else if (status === 'done') bits.push(`证据 ${writtenTotal(job)}`);
    bits.push(formatRelative(job.createdAt));
    // 跨目标那几条必须自己把目标说出来：目录是全局的，而"这一条是哪个目标的"
    // 正是读者把它与上面那些区分开的唯一依据。
    // 只在**与这一行自己的目标不同**时才补——同一个目标（比如探测任务与它的目标
    // 都是 `127.0.0.1`）补一句只是把已经写在标题里的字再说一遍。
    const owner = job.engagementId && job.engagementId !== state.eid
      ? shortTarget(targetOf(job.engagementId)) : '';
    if (owner && owner !== job.target) bits.push('目标 ' + owner);
    meta.textContent = bits.join(' · ');
    if (job.chainError) meta.classList.add('bad');
    row.title = `${job.id} · ${job.target || '（无目标）'}\n${kindLine(job)}\n点击查看这一次执行自己的报告`;
  }
  row.append(head, meta);
  return row;
}
/** 行的 title 里那一行说明：状态 + 类型 + 证据条数。 */
function kindLine(job) {
  const status = jobStatusOf(job);
  return `${JOB_STATUS_LABEL[status]} · ${jobKindLabel(job)} · 证据 ${writtenTotal(job)} 个节点`;
}

/** 画作业报告目录。`error` 由 `renderJobs` 传下来——**加载失败不能静默**：
 *  目录会安静地留在上一批任务上，而下面正文刚说过"加载失败"，同一件事在两处
 *  给出相反的答案。 */
function renderReportSide(error) {
  const wrap = $('#report-side');
  if (!wrap) return;
  // 自动刷新每 10 秒重画一次，而"我正在读的那一行往下滚了一点"是会被这个重画
  // 悄悄取消掉的——重画会清空容器，滚动位置随之回到顶部。记一下再还回去。
  const scrolled = wrap.scrollTop;
  clear(wrap);
  const hint = $('#report-side-hint');
  if (error) {
    if (hint) hint.textContent = '加载失败';
    wrap.append(errorBlock('作业列表加载失败：' + error.message));
    return;
  }
  if (!state.jobsLoaded) {
    if (hint) hint.textContent = '正在加载…';
    wrap.append(emptyBlock('正在加载作业列表…'));
    return;
  }
  // 选中的那条任务不在了（被删掉），回到汇总。不这么做的表现是：正文还显示着
  // 一篇查不到出处的文档，而目录里没有任何一行是选中的。
  if (state.reportJob && !jobOf(state.reportJob)) {
    state.reportJob = '';
    loadReport(true);
  }
  if (hint) hint.textContent = state.jobs.length ? `${state.jobs.length} 次执行` : '暂无作业';
  wrap.append(el('div', 'rpt-group', '本目标'));
  wrap.append(reportSideRow(null));
  if (!state.jobs.length) {
    wrap.append(el('div', 'rpt-group', '全部作业'));
    wrap.append(emptyBlock('还没有作业：在「会话」里让模型下发第一个'));
    wrap.scrollTop = scrolled;
    return;
  }
  // 按时间倒序（新 → 旧）。与作业页按状态分组不同，理由见本节文件头注释。
  const ordered = state.jobs.slice().sort((a, b) => (Number(b.createdAt) || 0) - (Number(a.createdAt) || 0));
  wrap.append(el('div', 'rpt-group', `全部作业（${ordered.length}）`));
  ordered.forEach((job) => wrap.append(reportSideRow(job)));
  wrap.scrollTop = scrolled;
}

/** 换一篇报告。`jobId` 为空回到本目标汇总。
 *
 *  **只换文档，不换目标**：目录是一个跨目标的归档，读它不该把你的目标挪走
 *  （漏洞 / 资产 / 报告三个页签的范围都跟着目标走）。文档自己会说清它是谁的——
 *  见 `renderReport` 里那行卡头。
 */
function showReport(jobId) {
  const next = String(jobId || '');
  if (next === state.reportJob && state.loaded.report) return;
  state.reportJob = next;
  // 选中态立刻跟手。目录只在任务读数变化时（`renderJobs`，每 10 秒一次）重画，
  // 不补这一下的话，点完之后最长 10 秒里"我点的是哪一行"没有反馈——而正文已经换了，
  // 于是屏幕上唯一能说明"你在读哪篇"的卡头与左栏高亮互相矛盾。
  renderReportSide(null);
  loadReport(true);
}

/* ------------------------------------------------------ 12. 部署面板 ---- */
/* 第五个页签：看部署自检、生成节点接入命令、管理节点、下发作业、看基线与任务。
 * 接口契约：GET /setup、GET /executors、POST /executors/enroll、
 *          POST /executors/{id}/revoke、GET/POST /jobs、GET /baselines。 */

const CHECK_LEVELS = ['ok', 'warn', 'fail'];
const CHECK_LABEL = { ok: '正常', warn: '警告', fail: '失败' };
const JOB_STATUS_LABEL = { pending: '待执行', running: '执行中', done: '已完成', failed: '失败' };

/* 执行节点的状态词。库里存的就是 online / offline（store.list_executors 现算，
 * 超时的那条就地改成 offline），所以不译一遍，中文界面上出现的是两个英文词。
 *
 * 这张表与判定函数放在这里，是因为这个判定原先在四个地方各写了一遍
 * （顶栏 title、顶栏读数、部署页摘要、部署页每行徽章），每一处都是
 * String(item.status || '').toLowerCase() === 'online'——第四处还多写了
 * item.online === true ||。四份同样的字面量，改一处的失败形态是：顶栏说 1/2 在线，
 * 部署页里那两个都显示「离线」。
 *
 * 与插件侧 dsh-plugins/dsh-pentest-console/client.js 的 executorLabel 同一套规矩：
 * 认不出的状态原样印出来，不编「未知」。 */
const EXECUTOR_STATUS_LABEL = { online: '在线', offline: '离线' };
const executorStatusOf = (row) => {
  const raw = String((row && row.status) || '').trim().toLowerCase();
  if (raw === 'online' || raw === 'offline') return raw;
  // 老接口只给布尔 online，新接口两个字段都给；两者都没有才当成离线。
  if (row && row.online === true) return 'online';
  return raw ? raw : 'offline';
};
const executorIsOnline = (row) => executorStatusOf(row) === 'online';
/** 中文状态词。认不出的状态回落到原始字符串，与 jobKindLabel 同一条规矩。 */
const executorStatusWord = (row) => {
  const status = executorStatusOf(row);
  return EXECUTOR_STATUS_LABEL[status] || status;
};

/**
 * 作业类型的中文名。**键集必须与服务端 `server.JOB_KIND_LABELS` 完全一致**，
 * 这件事由测试跨文件钉住（`test_ui_contract` 会读服务端的 JOB_KINDS 与这里的
 * 键逐个比）。措辞可以不同——这里是列表里的一枚小标签，比服务端那套全称短，
 * 但**不能少键**：漏一个键的症状是那一类任务显示成裸 id。
 *
 * 这里曾经写成 `kind === 'probe' ? '网络探测' : '源码审计'`，于是所有非探测
 * 任务——包括应用层利用验证与凭据测试——都被标成「源码审计」。这种错很难被
 * 发现：标签本身看着合理，只是不对；而「这个任务到底是什么」恰恰是使用者看
 * 列表时唯一的判断依据。同一个列表里既有读代码的又有打网络的，标错了就全乱了。
 *
 * 未知取值回落到**原始 kind**，不回落到任何一个已知名字：显示成 `shell`
 * 至少让人知道该去查，显示成「源码审计」会让人以为已经懂了。
 */
const JOB_KIND_LABEL = {
  scan: '源码审计', probe: '网络探测',
  exploit: '应用层利用', creds: '凭据测试',
  shell: '载荷投递', pivot: '出口路由', toolkit: '外部工具',
};

/** 作业类型标签。优先用服务端给的 `kindLabel`，它没给才用本地表。 */
const jobKindLabel = (job) => ((job && (job.kindLabel || JOB_KIND_LABEL[job.kind]))
  || (job && job.kind) || '');
const JOB_STATUS_ORDER = ['running', 'pending', 'failed', 'done'];
/** 任务结果摘要里常见键的中文别名，未列出的键原样显示。 */
const SUMMARY_LABEL = { findings: '漏洞', slices: '切片', files: '文件', scanned: '已扫描',
  skipped: '跳过', changed: '变更', nodes: '节点', bytes: '体积', elapsedMs: '耗时',
  hosts: '地址', alive: '存活', openPorts: '开放端口', chainNodes: '链路节点' };
// runId / target / kind 属于任务自身（行内已有目标与作业号），摘要里再列一次只是噪声。
const SUMMARY_SKIP = ['incremental', 'error', 'runId', 'target', 'kind'];
// 探测任务没有差分验证与增量这两个概念。这些 0 不是"什么都没查到"，而是"不适用"——
// 原样列出来会让人以为漏了什么，而这恰恰是摘要最不该造成的误解。
const SUMMARY_SKIP_PROBE = ['verified', 'refuted', 'static', 'slices', 'hypotheses',
  'changedFiles', 'skippedSlices'];
const JOBS_REFRESH_MS = 10000;
/** 侧边栏读数（目标 / 对话记录）的探询节奏。
 *
 *  3 秒，比任务读数的 10 秒快一档，理由是它们回答的问题不同：任务那一块是"还有
 *  活在跑吗"，10 秒足够；这两个块回答的是"我刚让模型干的那件事生效了没有"——
 *  等十秒会让人以为没生效，然后去手动刷整页，而那个动作正是这套机制要消灭的。
 *
 *  代价很小：`/pulse` 不发列表内容，只发几个聚合出来的数字。
 */
const PULSE_REFRESH_MS = 3000;

/** 字节数 → 人类可读单位（B/KB/MB/GB/TB）。 */
function formatBytes(value) {
  const num = Number(value);
  if (!Number.isFinite(num) || num < 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = num, index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${index === 0 ? size : size.toFixed(size >= 100 ? 0 : 1)} ${units[index]}`;
}
/** 计数 → 千分位，便于快速读出量级。 */
function formatCount(value) {
  const num = Number(value);
  return Number.isFinite(num) ? num.toLocaleString('en-US') : '—';
}
/** 秒级时间戳 → 相对时间；未来时间或过旧时间退回绝对时间。 */
function formatRelative(value) {
  const num = Number(value);
  if (!Number.isFinite(num) || num <= 0) return '—';
  const stamp = num > 1e12 ? num : num * 1000, diff = Date.now() - stamp;
  if (diff < 0) return formatTime(num);
  const seconds = Math.floor(diff / 1000);
  if (seconds < 5) return '刚刚';
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  return days < 30 ? `${days} 天前` : formatTime(num);
}
/** 秒级时间戳（缺省取当下）→ HH:MM:SS。 */
function formatClock(value) {
  const num = Number(value), date = new Date(Number.isFinite(num) && num > 0
    ? (num > 1e12 ? num : num * 1000) : Date.now());
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
/** 毫秒 → 人类可读耗时。 */
function formatDuration(value) {
  const num = Number(value);
  if (!Number.isFinite(num) || num <= 0) return '—';
  if (num < 1000) return `${Math.round(num)} ms`;
  const seconds = num / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds >= 10 ? 0 : 1)} s`;
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
}
/** 布尔项 → 是 / 否 / —（后端可能不返回该字段）。 */
function formatFlag(value) {
  return value === undefined || value === null ? '—' : (value ? '是' : '否');
}
/** 路径末段：列表里只显示末段，完整路径放 title 属性。 */
function basename(path) {
  const text = String(path === null || path === undefined ? '' : path).replace(/[\\/]+$/, '');
  const parts = text.split(/[\\/]/);
  return parts[parts.length - 1] || text;
}
/** 摘要里的标量值 → 文本。 */
function formatScalar(value) {
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'number') return formatCount(value);
  return String(value);
}
/** 结果摘要：只渲染标量字段，避免把嵌套对象糊在页面上。 */
function formatSummary(summary, kind) {
  const skip = kind === 'probe' ? SUMMARY_SKIP.concat(SUMMARY_SKIP_PROBE) : SUMMARY_SKIP;
  const keys = Object.keys(summary || {}).filter((key) => !skip.includes(key)
    && ['number', 'string', 'boolean'].includes(typeof summary[key]));
  if (!keys.length) return '';
  return keys.map((key) => `${SUMMARY_LABEL[key] || key} ${key === 'elapsedMs'
    ? formatDuration(summary[key]) : formatScalar(summary[key])}`).join(' · ');
}
/** 复制文本：优先 Clipboard API，降级 textarea + execCommand；feedback 可空。 */
async function copyText(text, feedback) {
  const value = String(text === null || text === undefined ? '' : text);
  if (!value) { showError('没有可复制的内容'); return false; }
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
    else {
      const area = el('textarea');
      area.value = value;                                  // value 赋值不涉及 HTML 解析
      area.setAttribute('readonly', 'readonly');
      area.style.cssText = 'position:fixed;opacity:0';
      document.body.append(area);
      area.select();
      const ok = document.execCommand('copy');
      area.remove();
      if (!ok) throw new Error('浏览器拒绝了复制操作');
    }
    if (feedback) feedback.textContent = '已复制';
    return true;
  } catch (error) { showError('复制失败：' + error.message); return false; }
}
/** 复制按钮：点击后按钮自身显示「已复制」。 */
function copyButton(text, label, className) {
  const button = el('button', `btn sm ${className || ''}`.trim(), label || '复制');
  button.type = 'button';
  button.addEventListener('click', () => copyText(text, button));
  return button;
}
/** 执行节点资源路径；与 engagementPath 同形，便于契约测试还原。 */
const executorPath = (id, suffix) => `/executors/${encodeURIComponent(id)}${suffix}`;
/** 任务接口路径。id 由服务端生成，但界面不该依赖这个前提。 */
const jobPath = (id, suffix) => `/jobs/${encodeURIComponent(id)}${suffix}`;

/** 自检清单：三色 + 标题 + 详情 + 建议。 */
function renderChecks(checks) {
  const wrap = $('#setup-checks');
  clear(wrap);
  if (!checks.length) { wrap.append(emptyBlock('后端未返回自检项')); return; }
  const list = el('ul', 'check-list');
  checks.forEach((check) => {
    const level = CHECK_LEVELS.includes(check.level) ? check.level : 'warn';
    const item = el('li', `check-item check-${level}`), head = el('div', 'check-head');
    head.append(el('span', `check-badge check-${level}`, CHECK_LABEL[level]),
      el('span', 'check-title', check.title || check.id || '未命名检查'));
    item.append(head);
    if (check.detail) item.append(el('p', 'check-detail', check.detail));
    if (check.hint) {
      const hint = el('p', 'check-hint');
      hint.append(el('span', 'check-hint-label', '建议'), document.createTextNode(' ' + String(check.hint)));
      item.append(hint);
    }
    list.append(item);
  });
  wrap.append(list);
}
/** 键值行；copyText 让值本身可点击复制，copy 追加独立的复制按钮。
 *  每一组键值各自包成一个 .kv-item（HTML5 允许 dl 里放 div 分组）。
 *  不能把 dt/dd 直接平铺进 dl：.setup-kv 要多列排，平铺时 dt/dd 会被当成
 *  彼此独立的网格项，标签和值当场分家——第 2 列的标签配到第 1 列的值上去。
 *  包成 item 之后，多列发生在 item 之间，标签与值的对应关系锁在 item 内部。 */
function appendInfoRow(list, label, value, options) {
  const opts = options || {};
  const text = value === undefined || value === null || value === '' ? '—' : String(value);
  const item = el('div', 'kv-item');
  item.append(el('dt', 'kv-key', label));
  const cell = el('dd', 'kv-val');
  if (opts.copyText && text !== '—') {
    const button = el('button', 'link-btn', text);
    button.type = 'button';
    button.title = '点击复制';
    button.addEventListener('click', async () => { button.title = await copyText(text, null) ? '已复制到剪贴板' : '复制失败'; });
    cell.append(button);
  } else cell.append(el('span', 'info-value', text));
  if (opts.copy && text !== '—') cell.append(copyButton(text, '复制', 'inline-copy'));
  item.append(cell);
  list.append(item);
}
/** 鉴权四项 → 一行文本。 */
function authText(auth) {
  const info = auth || {};
  return ['节点令牌校验 ' + formatFlag(info.executorToken), '注册凭据 ' + formatFlag(info.enrollToken),
    'TLS ' + formatFlag(info.tls), '客户端证书 ' + formatFlag(info.clientCert)].join(' · ');
}
/** counts → 一行文本。 */
function countsText(counts) {
  const info = counts || {};
  return ['engagement ' + formatCount(info.engagements), '链路节点 ' + formatCount(info.nodes),
    '执行节点 ' + formatCount(info.executorsOnline) + '/' + formatCount(info.executors),
    '作业 ' + formatCount(info.jobs) + '（待执行 ' + formatCount(info.pendingJobs) + '）',
    '基线 ' + formatCount(info.baselines)].join(' · ');
}
/** 部署信息卡：地址 / 监听 / 数据库 / 引擎 / 鉴权 / 计数。 */
function renderSetupInfo(data) {
  const wrap = $('#setup-info'), list = el('dl', 'kv setup-kv');
  clear(wrap);
  const urls = data.urls || {}, database = data.database || {}, engineInfo = data.engine || {};
  const listen = data.listen || {}, web = data.web || {}, probeInfo = data.probe || {};
  appendInfoRow(list, 'UI 地址', urls.ui, { copyText: true });
  appendInfoRow(list, 'API 地址', urls.api, { copyText: true });
  appendInfoRow(list, '监听地址', listen.host === undefined ? '' : `${listen.host}:${listen.port}`);
  appendInfoRow(list, '数据库', database.path
    ? `${database.path}（${formatBytes(database.sizeBytes)} · schema v${formatCount(database.schemaVersion)}）` : '');
  // 工作区：对话产生的文件与缓存都落在这儿。放在部署信息卡而不是只写在文档里，
  // 是因为“我刚才让模型写的脚本去哪了”必须能在界面上直接答出来。
  const wsInfo = data.workspace || {};
  appendInfoRow(list, '工作区', wsInfo.root
    ? `${wsInfo.root}（会话 ${formatCount(wsInfo.sessionCount)} 个）` : '');
  // **两个引擎分开写**：`推理引擎` 是跑模型与工具循环的 DSH（项目自带的
  // `vendor/dsh`），`发掘引擎` 是真正执行扫描/验证的 vulnagent。
  // 合成一行「引擎」时，看的人会以为平台的引擎就是扫描器，而模型那半边不存在。
  const dshInfo = data.dsh || {};
  appendInfoRow(list, '推理引擎', dshInfo.enabled
    ? ('DSH ' + (dshInfo.version || '') + (dshInfo.profile ? '（' + dshInfo.profile + '）' : '')
      + (dshInfo.available ? ' · 项目内' : ' · ' + (dshInfo.reason || '不可用')))
    : '原生循环（运行通道未启用）');
  appendInfoRow(list, '发掘引擎', engineInfo.available ? (engineInfo.home || '已检测到')
    : (engineInfo.message || '未检测到可用引擎'));
  appendInfoRow(list, 'Web 资源', web.dir ? `${web.dir}${web.ready ? '' : '（未就绪）'}` : (web.ready ? '就绪' : ''));
  appendInfoRow(list, '鉴权', authText(data.auth));
  // 探测范围放在部署信息里而不是只留在启动参数：网络探测的边界必须一眼可见，
  // 否则"为什么这个目标被拒绝"每次都要去翻命令行。
  appendInfoRow(list, '探测范围', probeInfo.text || '—');
  appendInfoRow(list, '计数', countsText(data.counts));
  wrap.append(list);
}
/** 探测范围：网络探测的边界必须在页面上可见，否则"目标为什么被拒绝"只能靠猜。 */
/** 配额键名 → 中文标签。**兜底成键名本身**：少一条标签只是显示成 `maxHops`，
 *  信息没丢；反过来，用一张必须与后端同步的硬表，漏掉的那条在界面上看起来
 *  和"这一类没有这条上限"完全一样——那是配额最不该被误解的地方。 */
const LIMIT_LABELS = {
  maxSteps: '步数', maxRequests: '请求数', maxPayloadBytes: '单个载荷字节',
  callbackWaitS: '带外等待秒', maxAccounts: '账号数', maxAttemptsPerAccount: '每账号尝试次数',
  maxPasswords: '口令数', minDelayMs: '两次尝试最小间隔(ms)', lockoutBackoffMs: '锁定冷却(ms)',
  maxUploads: '上传次数', maxCommands: '命令条数', maxOutputBytes: '输出字节上限',
  commandTimeoutS: '命令超时秒', maxHops: '跳数', maxSessions: '会话数',
  ttlS: '存活秒', maxEgressTargets: '出口目标数', maxRuntimeS: '运行时长秒',
};
const limitText = (limits) => Object.keys(limits || {})
  .map((key) => (LIMIT_LABELS[key] || key) + ' ' + formatCount(limits[key])).join(' · ');

/** 去掉服务端文本里的 markdown 强调标记（`**…**`）。

    这些串（`requires` / `summary`）同时供给对话与模型——那边是 markdown，`**` 是
    强调；这里的节点走 `textContent`，星号会原样印出来，于是「它**执行节点的权限**
    运行…」看起来像排版坏了。标记不是内容，渲染前去掉。
    只去 `**`：反引号在 `flag` 一类的字段里是命令的一部分，误删会改坏可抄的命令。 */
function plainEmphasis(value) {
  return String(value == null ? '' : value).replace(/\*\*/g, '');
}

/** 第三道闸的状态：五类侵入动作各自是否放行、本次生效的配额、目标清单。
 *
 *  **本部署强制放行这道闸**（`authz.FORCE_OPEN`）：五类全部视为已授权，目标清单
 *  与启动参数都不参与判定。所以这里的第一件事是**把这件事说出来**——一块全亮的
 *  开关若没有一句话解释，读起来就是"有人把它们逐个打开了"，而那个人不存在，
 *  重启也改不掉它。
 *
 *  闸照判时（把 `FORCE_OPEN` 改回 False）仍然成立的那句：**默认全部关闭是推荐
 *  状态，不是异常**。全关时用中性色、不用警告色——把推荐配置画成故障，使用者会
 *  去"修"它，而"修"的方向恰好是把这道闸打开。反过来，**已放行的那几类才需要
 *  显眼**：那是"现在真的会改变目标状态"。
 *
 *  与「探测范围」的边界由这段代码自己说清：两者在页面上相邻，而它们是整份
 *  界面里最容易被读成同一件事的两项。可连接的网段不等于可打的机器。**强制放行下
 *  这话收进顶部横幅**——目标清单那一行与底部那段边界说明都不画（m01812），只留
 *  横幅里一句"另两道闸仍然生效"，否则卡片尾巴上会拖着三段没人读完的灰字。
 */
function renderAuthz(data) {
  const panel = $('#authz-panel'), summary = $('#authz-summary');
  if (!panel) return;
  clear(panel);
  const info = data || {};
  const classes = asArray(info.classes);
  if (!classes.length) {
    if (summary) summary.textContent = '后端未提供';
    panel.append(emptyBlock('后端未返回侵入类别列表'));
    return;
  }
  const on = classes.filter((item) => item.enabled);
  const canEdit = !!info.canEdit;
  const restored = (info.restore || {}).restored === true;
  // **强制放行必须在 `canEdit` 之前判。** 那种状态下服务端给的 `canEdit` 也是
  // false，而 false 的原因完全不同——那是"这道闸没有被判定的对象"，不是"你的
  // 角色不够"。混起来读，管理员会去查一个权限问题，而正确答案是这道闸已经在
  // 代码里被打开了。
  const forceOpen = !!info.forceOpen;
  if (summary) {
    summary.textContent = (forceOpen
      ? '强制放行（' + classes.length + ' 类全部视为已授权）'
      : (on.length
        ? on.length + ' / ' + classes.length + ' 类已放行'
        : classes.length + ' 类全部关闭（默认）'))
      // "已保存"与"延续上次"说的都是**启动参数那份与界面上这份之间的仲裁**。
      // 强制放行下那套仲裁不参与判定，继续显示会让"上次设的还在不在"看起来
      // 仍然是这里的问题——而它已经不是了。
      + (forceOpen ? '' : (info.persisted ? ' · 已保存' : ''))
      + (forceOpen ? '' : (restored ? ' · 延续上次'
                                    : (info.runtimeChanged ? ' · 与启动参数不同' : '')));
  }
  // 只读账号看到的是"为什么改不了"，而不是一颗点不动的按钮——**权限判定由服务端给**
  // （`canEdit`），前端不去读角色名：无鉴权模式下前端拿到的账号信息是空的，
  // 自己推会推出"这个人不能改"，而那个模式下其实谁都能改。
  if (forceOpen) {
    // 这张卡片的话到这里就完了：原先底部还有「目标清单」「本机外部工具」「边界说明」
    // 三段（m01812 要求收掉）。目标清单那一行与边界说明的三句结论都并进了这块横幅，
    // 本机外部工具那一行归「部署 → 外部工具」卡。
    // `forceOpenText` 来自 `authz.force_open_text()`，结尾不带句号，所以这里只补一个
    // 「。」——兜底文案自带句号，补两个会拼出「闸。。」。
    panel.append(el('p', 'authz-force',
      (info.forceOpenText || '本部署已强制放行第三道闸')
      // `forceOpenText` 已经说过"五类全部视为已授权"，这里不再重复第二遍——再说一次
      // 读者会以为第二遍是新消息。
      + '。下面五类因此全是亮的：这道闸不再参与判定，这里没有可改的状态，也不要去改'
      + '启动参数——探测范围与权限档仍然生效，三者取交集，不是替代。'));
  } else if (!canEdit) {
    panel.append(el('p', 'authz-readonly',
      '只有管理员能改第三道闸。当前账号可以查看、可以在「会话」里问，'
      + '但改授权需要管理员身份。'));
  }
  const list = el('ul', 'authz-list');
  classes.forEach((item) => {
    const row = el('li', item.enabled ? 'authz-item on' : 'authz-item');
    const head = el('div', 'authz-head');
    head.append(el('span', item.enabled ? 'authz-badge on' : 'authz-badge',
                   item.enabled ? '已放行' : '未开启'),
      el('span', 'authz-label', item.label || item.id || ''),
      el('span', 'authz-id', item.id || ''));
    if (canEdit) {
      const button = el('button', 'btn sm authz-toggle',
                        item.enabled ? '关闭' : '开启');
      button.type = 'button';
      button.title = item.enabled
        ? '收回这一类：立刻不能再发起；已经下发的作业不受影响'
        : '放行这一类。它' + plainEmphasis(item.requires || '会改变目标状态') + '。';
      button.addEventListener('click', () => toggleAuthzClass(item));
      head.append(el('span', 'spacer'), button);
    }
    row.append(head);
    if (item.enabled) {
      // 已放行的行给"它意味着什么"，未开启的行给"去哪儿开"。两边的信息需求
      // 不同：前者要判断这一枪打出去会发生什么，后者先要一个可执行的地址。
      if (item.requires) {
        row.append(el('p', 'authz-requires', '它' + plainEmphasis(item.requires) + '。'));
      }
      const limits = limitText(item.limits);
      if (limits) row.append(el('p', 'authz-meta', '本次生效的配额：' + limits));
    } else {
      row.append(el('p', 'authz-summary', plainEmphasis(item.summary || '')));
      row.append(el('p', 'authz-flag',
                    '开启方式：' + (item.flag || ('--allow-intrusive ' + item.id))));
    }
    list.append(row);
  });
  panel.append(list);

  // ---- 目标白名单：三态（不限制 / 收窄到这些 / 一律拒绝）----
  // **不能从 `targets` 是否为空推断**：空与 `none` 字面上都是"没有目标"，意思却
  // 相反——一个全放行、一个全拒绝。形态由服务端给（`authz.target_mode`）。
  const targets = asArray(info.targets);
  const targetMode = info.targetMode || (targets.length ? 'list' : 'any');
  const targetsText = info.targetsText || (targets.length ? targets.join('、') : '不限制');
  if (canEdit) {
    const row = el('div', 'authz-targets-edit');
    const input = el('input', 'input text-input authz-targets-input');
    input.type = 'text';
    input.autocomplete = 'off';
    // `none` 是一种**显式取值**，所以要能出现在输入框里。空串留给"不限制"——
    // 把两者显示成同一个样子，使用者就改不动自己的意图了。
    input.value = targetMode === 'none' ? 'none' : targets.join(',');
    input.placeholder = '留空 = 不限制目标；或 10.0.0.0/24,10.0.0.9:8443；填 none = 一律拒绝';
    const save = el('button', 'btn sm', '保存白名单');
    save.type = 'button';
    save.addEventListener('click', () => submitAuthz(asArray(info.enabled), input.value));
    row.append(el('span', 'field-label', '目标白名单'), input, save);
    panel.append(row);
    panel.append(el('p', 'authz-targets', '当前：' + targetsText));
  } else if (!forceOpen) {
    // 强制放行下不出现：目标清单不参与判定，那个值此刻不决定任何事，横幅已经说过
    // 一遍了（写两遍会让人以为它还在参与判定）。
    panel.append(el('p', 'authz-targets', '目标白名单：' + targetsText));
  }

  // ---- 与启动参数的分叉。这块设置**会保留**，所以这里说清的是"下次启动还用哪一份" ----
  const restore = info.restore || {};
  if (!forceOpen && (info.runtimeChanged || restore.restored)) {
    const boot = asArray((info.boot || {}).enabled);
    const box = el('div', 'authz-runtime');
    box.append(el('p', 'authz-meta',
      (restore.restored
        ? '这一份是上次设置的延续：启动参数没变，所以沿用了它。'
        : '以上与启动参数不同。')
      + '设置会保留到下次启动；一旦改动启动参数（--allow-intrusive / '
      + '--intrusive-targets），那份新参数会被采用，这里保存的会被丢弃。'
      + '启动参数给的是：' + (boot.length ? boot.join('、') : '全部关闭') + '。'));
    if (info.restartCommand) {
      const line = el('div', 'authz-cmd');
      line.append(el('code', 'authz-cmd-text', info.restartCommand),
                  copyButton(info.restartCommand, '复制命令', 'inline-copy'));
      box.append(line);
    }
    panel.append(box);
  }

  // 本机外部工具原先在这里列一行（已装 N 项 + 名单）。它归「部署 → 外部工具」卡
  // （`GET /api/v1/tools`）：那张卡有检测/安装/删除与最近操作，是这份清单**唯一**
  // 的归属——第二处迟早与第一处对不上，而"缺工具"本来也不在这张卡的判据里。

  // ---- 变更记录：谁在什么时候把哪几类从什么改成了什么 ----
  const ops = asArray(info.ops);
  if (ops.length) {
    const box = el('details', 'authz-ops');
    box.append(el('summary', 'authz-ops-head',
                  '变更记录（最近 ' + ops.length + ' 次）'));
    ops.forEach((op) => {
      box.append(el('p', 'authz-op-line',
        formatRelative(op.createdAt) + ' · ' + (op.actor || '（无鉴权模式）')
        + ' · ' + describeAuthzChange(op.before, op.after)));
    });
    panel.append(box);
  }

  if (!forceOpen) {
    // 强制放行下不出现：这一段的结论（另两道闸仍生效、放掉的是第三道闸、三者取交集
    // 不是替代）已经并进上面那块横幅，再讲一遍等于同一段话说两次。
    panel.append(el('p', 'authz-boundary',
      '侵入授权与探测范围是两份独立清单，取交集：把地址加进探测范围不会让它变得可打，'
      + '把权限档调到最高也不会。三道闸都在提示词之外，换 Agent 预设不改变这里任何一项。'));
  }
}
/** 第三道闸状态；与部署页签的其它块一样，已加载过就不重复请求。 */
async function loadAuthz(force) {
  if (state.authzLoaded && !force) return;
  try {
    const data = await apiGet('/authz');
    state.authz = data;
    state.authzLoaded = true;
    renderAuthz(data);
  } catch (error) {
    state.authzLoaded = false;
    const panel = $('#authz-panel'), summary = $('#authz-summary');
    if (summary) summary.textContent = '加载失败';
    if (panel) { clear(panel); panel.append(errorBlock('侵入授权加载失败：' + error.message)); }
  }
}
/** 变更记录里的一行：把两份快照的差异说成人话。
 *
 *  直接打印 before / after 两个 JSON 也能查，但审计流水的读者多数时候只想知道
 *  "这次是开了还是关了、动了哪几类"——多出来的括号与引号会让那一眼变慢。
 */
function describeAuthzChange(before, after) {
  const was = asArray((before || {}).enabled), now = asArray((after || {}).enabled);
  const opened = now.filter((id) => !was.includes(id));
  const closed = was.filter((id) => !now.includes(id));
  const parts = [];
  if (opened.length) parts.push('开启 ' + opened.join('、'));
  if (closed.length) parts.push('关闭 ' + closed.join('、'));
  const wasTargets = asArray((before || {}).targets).join(','),
        nowTargets = asArray((after || {}).targets).join(',');
  if (wasTargets !== nowTargets) {
    // "清空"就是"不限制"。流水里写成"清空白名单"会读成"这次把白名单删了"，
    // 而它实际的意思是"这一次把目标限制放开了"——审计流水最容易在这里被读反。
    parts.push(nowTargets ? '白名单 → ' + nowTargets : '白名单 → 不限制（清空）');
  }
  return parts.length ? parts.join(' · ') : '无变化';
}
/** 开/关某一类。两个方向都要过确认：这是把"能改变目标状态"的能力交出去或收回来。 */
async function toggleAuthzClass(item) {
  const info = state.authz || {};
  const enabled = asArray(info.enabled);
  const targets = asArray(info.targets);
  if (item.enabled) {
    if (!window.confirm('关闭「' + (item.label || item.id) + '」？\n\n'
      + '关闭后这一类立刻不能再发起；已经下发的作业不受影响。')) return;
    await submitAuthz(enabled.filter((id) => id !== item.id), targets);
    return;
  }
  // **不再拦"没填白名单"**：留空现在是"不限制目标"，是一个合法且默认的配置。
  // 仍然会被服务端拒的只有一种——类别开着而白名单写成 `none`，那种自相矛盾的组合
  // 由 `authz.incomplete_grant` 在请求里判，客户端不重复一遍（重复的判据迟早分叉）。
  if (!window.confirm('放行「' + (item.label || item.id) + '」？\n\n它'
    + (item.requires || '会改变目标状态') + '。\n'
    + '目标限制：' + (info.targetsText || '不限制') + '。\n\n'
    + '改动会保存到下次启动；除非下次的启动参数改了，那时以启动参数为准。')) return;
  await submitAuthz(enabled.concat([item.id]), targets);
}
/** 提交一次授权改动。整份 enabled + 白名单一起提交，不做增量补丁。
 *
 *  与接口的幂等语义配套：服务端拿到的是"改完之后应该是什么样"，所以重复提交同一个
 *  状态不会写出一条空流水。换成 `{add: 'exploit'}` 那种补丁式接口，两次点击之间
 *  若有人从别处改了同一份状态，补丁会叠加成一个谁都没要求过的结果。
 */
async function submitAuthz(enabled, targets) {
  const list = (typeof targets === 'string' ? targets.split(',') : asArray(targets))
    .map((item) => String(item).trim()).filter(Boolean);
  try {
    const data = await apiPost('/authz', { enabled: asArray(enabled), targets: list });
    state.authz = data;
    state.authzLoaded = true;
    renderAuthz(data);
  } catch (error) {
    showError('改侵入授权失败：' + error.message);
  }
}
/** 部署自检 + 信息卡。 */
function renderSetup(data) {
  const checks = asArray(data.checks), auth = data.auth || {};
  renderChecks(checks);
  renderSetupInfo(data);
  $('#enroll-hint').textContent = auth.enrollToken
    ? '控制面已启用注册凭据：请填写与服务端 --enroll-token 一致的凭据。'
    : '控制面未启用注册凭据，注册凭据可留空。';
  const failed = checks.filter((item) => item.level === 'fail').length;
  const warned = checks.filter((item) => item.level === 'warn').length;
  $('#setup-meta').textContent = `版本 ${data.version || '未知'} · 自检 ${checks.length} 项`
    + `（失败 ${failed} · 警告 ${warned}）· 刷新于 ${formatClock()}`;
}
/** 部署信息整体失败：自检与信息卡各自显示错误态，不白屏。 */
function renderSetupFailure(error) {
  setPanelMessage('#setup-checks', errorBlock('部署自检加载失败：' + error.message));
  setPanelMessage('#setup-info', errorBlock('部署信息加载失败：' + error.message));
  $('#setup-meta').textContent = '部署信息加载失败';
}
/** 一次性令牌警告 + 令牌 + 三份可复制命令。 */
function renderEnrollResult(data) {
  const wrap = $('#enroll-result');
  clear(wrap);
  const warning = el('div', 'token-warning');
  warning.append(el('strong', null, '一次性令牌，只显示这一次'),
    el('p', null, data.warning || '服务端只保留令牌摘要，关闭页面后无法再次查看，请立即保存。'));
  wrap.append(warning);
  const tokenRow = el('div', 'copy-row');
  tokenRow.append(el('span', 'copy-label', `节点 ${data.name || data.executorId || ''}`),
    el('code', 'copy-value', data.token || '（后端未返回令牌）'));
  if (data.token) tokenRow.append(copyButton(data.token, '复制令牌'));
  wrap.append(tokenRow);
  const commands = data.commands || {};
  [['powershell', 'PowerShell'], ['bash', 'Bash'], ['systemd', 'systemd']].forEach(([key, label]) => {
    const text = commands[key];
    if (!text) { wrap.append(el('p', 'notice', `${label} 命令：后端未提供`)); return; }
    const block = el('div', 'cmd-block'), head = el('div', 'cmd-head');
    head.append(el('span', 'cmd-label', label), copyButton(text, '复制命令'));
    block.append(head, el('pre', 'cmd-body', text));
    wrap.append(block);
  });
}
/** 控制面启用注册凭据时带请求头；留空则不带，由后端决定是否拒绝。 */
function enrollHeaders() {
  const token = $('#enroll-token').value.trim();
  return token ? { 'X-Enroll-Token': token } : {};
}
async function enrollExecutor() {
  const input = $('#enroll-name'), name = input.value.trim(), output = $('#enroll-result');
  const button = $('#enroll-btn');
  if (!name) { showError('请先填写节点名'); input.focus(); return; }
  button.disabled = true;
  clear(output);
  output.append(el('p', 'notice', '正在生成接入命令…'));
  try {
    const data = await apiPost('/executors/enroll', { name }, enrollHeaders());
    renderEnrollResult(data);
    await loadExecutors(true);
  } catch (error) {
    clear(output);
    output.append(el('p', 'notice error', '生成接入命令失败：' + error.message));
    showError('节点接入失败：' + error.message);
  } finally { button.disabled = false; }
}
/** 节点列表：名称 / id / 在线徽章 / 负载 / 最后心跳 / 吊销。 */
function renderSetupExecutors(error) {
  const wrap = $('#executor-list');
  clear(wrap);
  const online = state.executors.filter(executorIsOnline).length;
  $('#executor-count').textContent = state.executors.length
    ? `共 ${formatCount(state.executors.length)} 个 · 在线 ${formatCount(online)}` : '';
  // 摘要行在**折叠状态下**是唯一可见的一行，所以它要回答"我需不需要打开这块"。
  // 只回显计数做不到这件事：`共 1 个 · 在线 1` 看不出那一个是不是自己这台机器、
  // 也就是"要不要管"。
  //
  // 这里**不去猜哪个节点是本机的**。按名字等于 "local" 来判是错的：本机节点的名字
  // 是可配的（`--name`，测试夹具就叫 fixture-node），一旦不是 "local"，界面就会把
  // 唯一那个本机节点报成"0 个远程节点接入"——自相矛盾，比不说更糟。
  // "有节点在线就不用配"这句话与节点叫什么无关，也不依赖有几个。
  const summary = $('#executor-summary');
  if (summary) {
    const live = state.executors.filter(executorIsOnline).length;
    summary.textContent = error ? '节点列表加载失败'
      : (!state.executors.length ? '暂无节点，展开可生成接入命令'
        : (live ? '有节点在线，单机部署无需配置' : '节点全部离线'));
  }
  if (error) { wrap.append(errorBlock('节点列表加载失败：' + error.message)); return; }
  if (!state.executors.length) {
    wrap.append(emptyBlock('暂无执行节点：填节点名生成接入命令，在目标机器执行后即在此上线'));
    return;
  }
  const list = el('ul', 'row-list');
  state.executors.forEach((executor) => {
    const status = executorStatusOf(executor);
    const item = el('li', 'row'), head = el('div', 'row-head');
    head.append(el('span', `status-badge status-${status}`, executorStatusWord(executor)),
      el('span', 'row-title', executor.name || executor.id || '（未命名节点）'),
      el('span', 'spacer'), el('span', 'row-id', executor.id || ''));
    const revoke = el('button', 'btn sm danger', '吊销');
    revoke.type = 'button';
    revoke.dataset.executorId = executor.id || '';
    revoke.dataset.executorName = executor.name || executor.id || '';
    head.append(revoke);
    item.append(head);
    const meta = el('div', 'row-meta');
    // 相对时间只是"这次列表刷新时算出来"的快照（本页签不轮询节点列表），
    // 把绝对时间放进 title，避免把 3 分钟前的在线状态当成当前状态
    const beat = el('span', 'muted', '最后心跳 ' + formatRelative(executor.lastSeen));
    beat.title = formatTime(executor.lastSeen) + '（列表刷新时计算，非实时）';
    meta.append(el('span', 'muted', '负载 ' + formatCount(executor.load || 0)), beat);
    if (asArray(executor.capabilities).length) meta.append(el('span', 'muted', '能力 ' + asArray(executor.capabilities).join(' / ')));
    if (executor.registeredAt) meta.append(el('span', 'muted', '注册于 ' + formatRelative(executor.registeredAt)));
    item.append(meta);
    list.append(item);
  });
  wrap.append(list);
}
/** 吊销节点：二次确认后调用接口并刷新列表。 */
async function revokeExecutor(button) {
  const id = button.dataset.executorId, name = button.dataset.executorName || id;
  if (!id) { showError('该节点缺少 id，无法吊销'); return; }
  if (!window.confirm(`确定吊销节点「${name}」？令牌会立即失效，它正在执行的作业会被标记为失败。`)) return;
  const message = $('#executor-message');
  button.disabled = true;
  message.textContent = '正在吊销…';
  try {
    await apiPost(executorPath(id, '/revoke'), {});
    message.textContent = `已吊销节点「${name}」`;
    await loadExecutors(true);
  } catch (error) {
    message.textContent = '吊销失败：' + error.message;
    showError('吊销节点失败：' + error.message);
  } finally { button.disabled = false; }
}
const jobStatusOf = (job) => (JOB_STATUS_LABEL[String(job.status || '').toLowerCase()]
  ? String(job.status).toLowerCase() : 'pending');
/** 执行中的排前面，其次待执行、失败、已完成；同状态按创建时间新 → 旧。 */
function compareJobs(a, b) {
  const diff = JOB_STATUS_ORDER.indexOf(jobStatusOf(a)) - JOB_STATUS_ORDER.indexOf(jobStatusOf(b));
  return diff !== 0 ? diff : (Number(b.createdAt) || 0) - (Number(a.createdAt) || 0);
}
/**
 * 智能体协作轨迹：一次源码审计里七个角色各自做了什么、花了多久。
 *
 * 七个角色恒定全部列出——idle 的角色明说"本次未触发"，不静默省略。
 * 少一行会让人以为系统只有六个智能体，而这张表存在的意义恰好是反驳这个误解。
 * 耗时条按本轮总耗时归一，哪个阶段是瓶颈一眼可见（通常是调模型的两段）。
 */
function renderAgentTrace(job) {
  const agents = asArray(job.agents);
  if (!agents.length) return null;
  const total = agents.reduce((sum, item) => sum + (Number(item.elapsedMs) || 0), 0) || 1;
  const active = agents.filter((item) => item.state !== 'idle').length;
  const details = el('details', 'agent-trace');
  // 默认展开：这个区块存在的意义就是被看见。默认折叠等于又把协作过程藏起来，
  // 那正是它要解决的问题。代价是任务多时列表变长，用户可自行收起。
  details.open = true;
  details.append(el('summary', null, `智能体协作：${active}/${agents.length} 个角色触发`
    + `，编排总耗时 ${formatDuration(total)}`));
  const list = el('ul', 'agent-list');
  agents.forEach((agent) => {
    const idle = agent.state === 'idle';
    const item = el('li', idle ? 'agent-row agent-idle' : 'agent-row');
    const head = el('div', 'agent-head');
    head.append(el('span', 'agent-role', agent.label || agent.role));
    if (Number(agent.actions) > 1) head.append(el('span', 'agent-count', `${agent.actions} 次动作`));
    head.append(el('span', 'spacer'));
    head.append(el('span', 'agent-time', idle ? '—' : formatDuration(agent.elapsedMs)));
    item.append(head);
    const bar = el('span', 'agent-bar'), fill = el('span', 'agent-bar-fill');
    const share = idle ? 0 : (Number(agent.elapsedMs) || 0) / total * 100;
    fill.style.width = share ? Math.max(2, Math.round(share)) + '%' : '0%';
    bar.append(fill);
    item.append(bar);
    item.append(el('p', 'agent-summary', agent.summary || agent.duty || ''));
    list.append(item);
  });
  details.append(list);
  return details;
}
/** 作业列表：状态徽章 / 目标 basename / 增量标记 / 创建时间 / 耗时 / 摘要。 */
function renderJobs(error) {
  // 侧栏那一块**先画，而且在下面两个早退之前**。放在早退之后的话，"加载失败"
  // 与"一条任务都没有"这两条路径都不会走到它，于是侧栏会继续显示上一批任务
  // ——而面板刚刚说过加载失败，同一件事在两处给出相反的答案。
  renderJobList(error);
  // 报告页左侧的作业报告目录读的是同一批数据，同样要跟着走、同样要在早退之前。
  renderReportSide(error);
  const wrap = $('#job-list');
  clear(wrap);
  // 清空按钮的可用范围先算出来。加载失败时按"不可用"处理，而不是沿用上一次已知的
  // 条数——用一个过期的数字去描述"将清空几条"，比直接禁用更误导。
  updateJobsClearButton(error);
  // 导航徽章：**未结束的作业数**挂在左侧导航上。它是从别的页签看得见"还有活在跑"
  // 的唯一入口——作业页签不打开时，作业列表根本不渲染。
  // 加载失败时按 0 处理（隐藏徽章），而不是沿用上一次的条数：拿一个过期的数字
  // 去说"还有 3 个任务在跑"，比不说更误导。
  const badge = $('#badge-jobs');
  if (badge) {
    const open = error ? 0 : state.jobs.filter((job) => !JOB_FINISHED.has(jobStatusOf(job))).length;
    badge.textContent = open > 0 ? String(open) : '';
  }
  if (error) { wrap.append(errorBlock('作业列表加载失败：' + error.message)); return; }
  if (!state.jobs.length) { wrap.append(emptyBlock('暂无作业：在「会话」里让模型下发第一个作业')); return; }
  // 按**目标**分组（m14866「任务列表按照目标分组」）。这张表此前是一根长条，按状态
  // 排：同一个目标的四次执行被别的目标的任务隔开，读的人得自己在脑子里把它们挑出来。
  // 分组之后"这个目标跑过几次、各是什么结果"一眼可见；组内仍按 `compareJobs`（执行中
  // 在前、同状态新 → 旧），于是组与组之间是目标的顺序、组内是状态的顺序。
  const list = el('div', 'job-groups');
  jobGroups().forEach((group) => list.append(jobGroupBlock(group)));
  wrap.append(list);
}

/** 作业页的分组：一个目标一组。
 *
 *  三件事按这个顺序定，每一件都有它的理由：
 *
 *   1. **键是 `engagementId`**，不是目标字符串。目标值可以重复（两次都填了同一个
 *      域名），而目标号唯一；拿字符串分组会把两个目标的任务并成一组。
 *   2. **当前选中的目标置顶**。分组之后最常问的一句是"**这个**目标跑成什么样"，
 *      而它的位置若取决于别的东西（创建时间、最近执行），每次进这一页都要先找它。
 *   3. **其余按最近一次执行倒序**。这一页回答的是"最近在干什么"，不是"历史上发生过
 *      什么"——目标列表那边按创建时间排，那是另一个问题。
 *
 *  没有目标号的任务单独一组「未归属目标」并排在最后：手工下发与命令行下发的任务
 *  不带归属，它们**不是**被藏起来，而是本来就不属于任何目标。
 */
function jobGroups() {
  const buckets = new Map();
  state.jobs.forEach((job) => {
    const eid = String(job.engagementId || '');
    if (!buckets.has(eid)) buckets.set(eid, []);
    buckets.get(eid).push(job);
  });
  const groups = Array.from(buckets.entries()).map(([eid, jobs]) => ({
    eid,
    // 目标名走 `targetOf`（读 `state.engagementList` 那一份快照），列表还没加载出来时
    // 退回目标号本身 —— 与报告目录那一行同一条规矩，宁可印一个号也不印「未知」。
    label: eid ? targetOf(eid) : '未归属目标',
    latest: jobs.reduce((top, job) => Math.max(top, Number(job.createdAt) || 0), 0),
    jobs,
  }));
  groups.sort((a, b) => {
    const mine = (group) => (group.eid && group.eid === state.eid ? 0 : 1);
    const diff = mine(a) - mine(b);
    if (diff !== 0) return diff;
    const unowned = (group) => (group.eid ? 0 : 1);
    const tail = unowned(a) - unowned(b);
    return tail !== 0 ? tail : b.latest - a.latest;
  });
  return groups;
}

/** 一组任务：一行组标题（目标名 + 条数），下面那几条。 */
function jobGroupBlock(group) {
  const box = el('section', 'job-group');
  const heading = el('div', 'rpt-group');
  const name = el('span', 'job-group-name', group.label);
  name.title = group.eid || group.label;
  heading.append(name);
  heading.append(el('span', 'job-group-count', `${formatCount(group.jobs.length)} 条`));
  box.append(heading);
  const rows = el('ul', 'row-list');
  group.jobs.slice().sort(compareJobs).forEach((job) => {
    const status = jobStatusOf(job), item = el('li', 'row'), head = el('div', 'row-head');
    const isFollowed = Boolean(state.jobLog && state.jobLog.jobId === job.id);
    head.append(el('span', `status-badge status-${status}`, JOB_STATUS_LABEL[status]));
    // 作业类型必须显示：同一个列表里既有代码审计、又有网络探测、又有利用验证，
    // 不标出来"耗时 3 秒就完成"看起来像异常，实际是探测或工具调用的正常速度。
    head.append(el('span', 'tag', jobKindLabel(job)));
    // 扫描任务要标出「这一轮由谁跑」。两条路的证据强度不同：发掘引擎能做
    // 差分验证（可达「已证实」），推理引擎只在目标目录里只读审计（一律
    // 「未证实（静态）」）。不标出来，同一个列表里的结论读起来像出自同一条
    // 流水线，而它们的可信度其实差一档。
    const payload = job.payload || {};
    const scanEngine = String(payload.scanEngine || '');
    if (String(job.kind || '') === 'scan' && scanEngine) {
      const engineTag = el('span', 'tag tag-accent',
        scanEngine === 'dsh' ? '推理引擎（DSH）' : '发掘引擎（vulnagent）');
      engineTag.title = scanEngine === 'dsh'
        ? '由推理引擎（DSH 会话）在目标目录只读审计：结论一律标为静态匹配，未做差分验证'
        : '由发掘引擎（vulnagent）执行：可做差分验证（缺陷版复现、修复版不复现）';
      head.append(engineTag);
    }
    const title = el('span', 'row-title', basename(job.target) || '（无目标）');
    title.title = job.target || '';
    head.append(title);
    if (job.incremental) head.append(el('span', 'tag', '增量'));
    head.append(el('span', 'spacer'), el('span', 'row-id', job.id || ''));
    const remove = el('button', 'btn sm danger', '删除');
    remove.type = 'button';
    remove.dataset.jobId = job.id || '';
    remove.dataset.jobStatus = status;
    // 执行中的任务把代价写在按钮上：删掉它不会中止已经跑起来的那次执行，
    // 但结果会被丢弃（节点回传时控制面已无此任务）。不写清就是让人以为
    // "删了等于停了"——那是两件不同的事。
    remove.title = status === 'running'
      ? '正在执行：删除后本次执行仍会跑完，但结果会被丢弃'
      : '删除这条调度记录（不影响已写入的链路证据）';
    head.append(remove);
    item.append(head);
    // 「实时日志」按钮。**每一条任务都有**，不只是执行中的：一条已经失败的
    // 任务，它的日志才是最有价值的那份——而"跑完才涌出两万字节"正是它原本
    // 最难读的时刻。
    const logButton = el('button', 'btn sm', isFollowed ? '日志中' : '实时日志');
    logButton.type = 'button';
    logButton.dataset.jobLog = job.id || '';
    logButton.title = '跟随这条作业的执行过程（服务端推送，不用刷新）';
    head.append(logButton);
    const meta = el('div', 'row-meta');
    const created = el('span', 'muted', '创建 ' + formatRelative(job.createdAt));
    created.title = formatTime(job.createdAt);
    meta.append(created, el('span', 'muted', '耗时 ' + formatDuration(job.elapsedMs)));
    if (job.claimedBy) meta.append(el('span', 'muted', '执行节点 ' + job.claimedBy));
    if (job.engagementId) meta.append(el('span', 'muted', 'engagement ' + job.engagementId));
    item.append(meta);
    const summary = formatSummary(job.summary, job.kind);
    if (summary) item.append(el('p', 'row-summary', '结果：' + summary));
    else if (status === 'done') item.append(el('p', 'row-summary', '结果：后端未返回摘要'));
    if (job.error) item.append(el('p', 'row-error', '错误：' + job.error));
    const origin = renderJobOrigin(job);
    if (origin) item.append(origin);
    const trace = renderAgentTrace(job);
    if (trace) item.append(trace);
    // 日志面板长在**它自己那一行里面**。放在列表外面时，点第一行的
    // 「实时日志」，面板出现在整个列表的最下面——一条任务带七角色协作轨迹
    // 时那是一屏之外，点开的东西在哪儿得自己找。正文存在 `state.jobLog` 里，
    // 所以这里每次重画都能把它原样重建出来。
    if (isFollowed) item.append(jobLogPanel(state.jobLog));
    rows.append(item);
  });
  box.append(rows);
  return box;
}
/**
 * 由规划提案下发的任务，把"依据哪句话"显示在任务上。
 *
 * 这是规划功能散在审计链上的另一半：提案上写着"为什么这么拆"，任务上写着
 * "从哪份提案来的"。少了它，作业列表里会凭空多出几条没人知道出处的扫描。
 */
function renderJobOrigin(job) {
  const payload = job.payload || {};
  if (!payload.planId) return null;
  const parts = [];
  if (payload.planEvidence) parts.push(`依据你的原话「${payload.planEvidence}」`);
  if (payload.planReason) parts.push(payload.planReason);
  const note = el('p', 'row-origin');
  note.append(el('span', 'tag tag-accent', 'AI 规划'));
  note.append(el('span', null, ' ' + (parts.join(' · ') || '由规划提案下发')));
  note.title = `规划提案 ${payload.planId}`;
  return note;
}
/** 已结束（可被"清空"带走）的状态集合。 */
const JOB_FINISHED = new Set(['done', 'failed']);
/**
 * 清空按钮：没有已结束的任务时禁用，并把"清掉几条、保留几条"写进 title。
 *
 * 这里**不按角色禁用**。清空要求管理员，但"当前用户是不是管理员"由后端裁决：
 * 前端自己猜一次角色，在免登录部署（没有角色这回事）下就会把按钮错误地灰掉，
 * 而用户完全不知道为什么。后端的 403 带着明确消息回来，比一个说不出原因的
 * 禁用按钮更好用。
 */
function updateJobsClearButton(failed) {
  const button = $('#jobs-clear');
  if (!button) return;
  const finished = state.jobs.filter((job) => JOB_FINISHED.has(jobStatusOf(job))).length;
  const kept = state.jobs.length - finished;
  button.disabled = Boolean(failed) || finished === 0;
  if (failed) button.title = '作业列表未加载成功，无法判断可清空的范围';
  else if (!finished) button.title = '没有已结束的作业可清空（待执行与执行中的作业不会被清空）';
  else button.title = `清空 ${finished} 条已结束的作业；保留 ${kept} 条待执行/执行中`;
}
/** 删除单条任务：二次确认；执行中的那条要说明结果会被丢弃。 */
async function deleteJob(button) {
  const id = button.dataset.jobId, status = button.dataset.jobStatus || '';
  if (!id) { showError('该作业缺少 id，无法删除'); return; }
  const prompt = status === 'running'
    ? `作业 ${id} 正在执行。删除后它仍会跑完，但本次结果会被丢弃。确定删除？`
    : `确定删除作业 ${id}？\n只删这条调度记录，已写入的链路证据不受影响。`;
  if (!window.confirm(prompt)) return;
  button.disabled = true;
  const message = $('#jobs-message');
  if (message) message.textContent = '正在删除…';
  try {
    const data = await apiPost(jobPath(id, '/delete'), {});
    await loadJobs(true, false);
    if (message) {
      message.textContent = data && data.discarded
        ? `已删除作业 ${id}：它正在执行，那次结果会被丢弃`
        : `已删除作业 ${id}`;
    }
  } catch (error) {
    button.disabled = false;
    if (message) message.textContent = '';
    showError('删除作业失败：' + error.message);
  }
}
/**
 * 一条任务的**执行过程**：跟着它走到结束。
 *
 * 四个决定各自对应一个具体的坏结果，都是这类面板的经典故障：
 *
 * * **只允许一条流。** 每开一条就是一个长连接，而它在有日志时会一直有数据。
 *   点开三条任务就是三个连接，且前两个永远不会自己断——它们的任务在服务端
 *   还在跑。开新的先关旧的。
 * * **看到 `end` 必须主动 `close()`。** `EventSource` 在服务端关闭连接之后
 *   **会自动重连**，而我们的服务端恰恰是靠"关连接"表示"日志讲完了"。不显式
 *   关掉，浏览器会不停重连、每次把整份日志重放一遍——日志看起来在持续变长，
 *   而任务早就结束了。
 * * **只贴底。** 无条件滚到底会让"往上翻看前面发生了什么"变得不可能：每来
 *   一行就把你弹回底部。
 * * **断流要说清是断流，不是卡住。** 两者的下一步完全不同：一个该刷新，
 *   一个该等。
 */
const JOB_LOG_LINES = 800;          // 面板里最多留多少行（服务端另有 2000 行上限）

/**
 * 面板的 DOM **每次重画作业列表时重建**，而日志正文存在 `state.jobLog.lines` 里。
 *
 * 这是刻意的分工，不是偷懒：面板必须长在**它那一行里面**。放在列表外面
 * （先前的写法）时，点第一行的「实时日志」，面板出现在整个列表的最下面——
 * 一条任务带七角色协作轨迹时那是一屏之外，要点开的东西在哪儿得自己找。
 * 而列表每 10 秒会 `clear()` 整个容器一次，所以"长在行里"就必然要能重建。
 * 重建的代价是可控的：正文最多 800 行，且**只在 `renderJobs` 那一拍**发生；
 * 两次重画之间的新行仍然只做增量追加。
 */
function jobLogPanel(current) {
  const host = el('div', 'job-log');
  host.id = 'job-log';
  const head = el('div', 'job-log-head');
  const body = el('div', 'job-log-body');
  host.append(head, body);
  renderJobLogHead(head, current);
  const notices = jobLogNotice(current);
  if (notices) host.append(notices);
  current.lines.forEach((line) => body.append(el('div', 'job-log-line', line)));
  current.drawn = current.lines.length;
  if (!current.lines.length && !current.ended) {
    body.append(el('div', 'job-log-line job-log-idle',
                   '等待第一行输出…（这条作业还没写出任何过程记录）'));
  }
  if (current.sticky) body.scrollTop = body.scrollHeight;
  // 贴底与否由**用户的滚动**决定，不在这里猜。24px 的容差是给平滑滚动与
  // 分数像素留的余量——严格等于 0 会让"已经在底部"经常判成否。
  body.addEventListener('scroll', () => {
    const live = state.jobLog;
    if (!live || live.jobId !== current.jobId) return;
    live.sticky = (body.scrollHeight - body.scrollTop - body.clientHeight) < 24;
  });
  return host;
}

/** 被滚掉的开头、以及收尾说明。**必须说出来**：不说的后果是读者把"日志从
 *  中间开始"当成"任务从中间才动"，那会把一次正常的扫描读成一次没跑完的扫描。 */
function jobLogNotice(current) {
  const notes = [];
  if (current.dropped) {
    notes.push(`前面有 ${formatCount(current.dropped)} 行因为超出服务端保留上限`
      + '已经滚出窗口，下面从能看到的开始。');
  }
  if (current.ended) notes.push(current.ended);
  if (!notes.length) return null;
  const box = el('p', 'job-log-notice');
  box.textContent = notes.join(' ');
  return box;
}

/** 点一下「实时日志」：打开、切换、或关闭当前跟随。 */
function toggleJobLog(jobId) {
  if (!jobId) return;
  if (state.jobLog && state.jobLog.jobId === jobId) { closeJobLog(); return; }
  openJobLog(jobId);
}

function openJobLog(jobId) {
  closeJobLog();
  state.jobLog = { jobId, source: null, lines: [], drawn: 0, dropped: 0,
                   ended: '', sticky: true, lastTs: 0 };
  refreshJobButtons();          // 面板随之长出来，按钮变「日志中」
  scrollJobLogIntoView();
  if (typeof EventSource !== 'function') {
    // 浏览器不支持长连接。任务照常执行，只是这一屏看不到过程——说清楚，
    // 而不是给一个转不完的"等待第一行"。
    state.jobLog.ended = '这个浏览器不支持 EventSource，无法实时跟随。'
      + '作业本身照常执行，稍后刷新可看到它的结果。';
    renderJobLog();
    return;
  }
  const source = new EventSource(API_BASE + jobPath(jobId, '/stream'));
  state.jobLog.source = source;
  source.addEventListener('log', (event) => {
    const data = parseEvent(event);
    if (!data || data.message === undefined) return;
    const current = state.jobLog;
    if (!current || current.jobId !== jobId) return;
    current.lines.push(String(data.message));
    current.lastTs = data.ts || current.lastTs;
    if (current.lines.length > JOB_LOG_LINES) {
      current.lines = current.lines.slice(-JOB_LOG_LINES);   // 触发下一帧整段重画
    }
    renderJobLog();
  });
  source.addEventListener('dropped', (event) => {
    const data = parseEvent(event);
    if (state.jobLog && state.jobLog.jobId === jobId && data) {
      state.jobLog.dropped = Number(data.dropped) || 0;
      renderJobLog();
    }
  });
  source.addEventListener('beat', (event) => {
    const data = parseEvent(event);
    if (state.jobLog && state.jobLog.jobId === jobId && data && data.ts) {
      state.jobLog.lastTs = data.ts;
    }
    syncJobLogHeader();
  });
  ['end', 'gone', 'timeout'].forEach((name) => {
    source.addEventListener(name, (event) => {
      if (!state.jobLog || state.jobLog.jobId !== jobId) return;
      state.jobLog.ended = name === 'timeout'
        ? '单次跟随时长已达上限。点一次「实时日志」可重新跟随。'
        : (name === 'gone' ? '这条作业已被删除。'
                           : '这条作业的执行过程已结束。');
      // 必须显式关：EventSource 在服务端关闭连接之后**会自动重连**，而我们的
      // 服务端恰恰是靠"关连接"表示"日志讲完了"。不关就是不停重连、每次把整份
      // 日志重放一遍——日志看起来在持续变长，而任务早就结束了。
      detachJobLogSource();
      renderJobLog();
    });
  });
  source.onerror = () => {
    if (!state.jobLog || state.jobLog.jobId !== jobId) return;
    // 断流与"任务卡住"是两件事，而它们的下一步完全不同（刷新 vs 等待）。
    // 不区分的话，一次服务重启会被读成"这次扫描没动静了"。
    state.jobLog.ended = state.jobLog.ended
      || '与服务端的连接已断开。点一次「实时日志」可重新跟随。';
    detachJobLogSource();
    renderJobLog();
  };
}

/** 打开后把面板带进视野。列表长的时候，不滚一下等于没显示。 */
function scrollJobLogIntoView() {
  const host = $('#job-log');
  if (host && host.scrollIntoView) host.scrollIntoView({block: 'nearest'});
}

/** 只断流，保留面板与已收到的日志。 */
function detachJobLogSource() {
  const current = state.jobLog;
  if (current && current.source) {
    current.source.close();
    current.source = null;
  }
}

/** 关闭并收起面板。 */
function closeJobLog() {
  detachJobLogSource();
  state.jobLog = null;
  const host = $('#job-log');
  if (host && host.remove) host.remove();
  refreshJobButtons();          // 「日志中」→「实时日志」
}

/** 重画作业列表。列表没加载过时什么都不做——重画一个空列表会把"暂无任务"
 *  那行换成别的东西，而那与本次操作无关。 */
function refreshJobButtons() {
  if (state.jobs && state.tab === 'jobs') renderJobs(null);
}

function parseEvent(event) {
  try { return JSON.parse(event.data); } catch (error) { return null; }
}

/** 只重画标题行。心跳会调它——**不动正文、更不动流**。 */
function syncJobLogHeader() {
  const current = state.jobLog;
  const host = $('#job-log');
  if (!current || !host) return;
  const head = $('.job-log-head', host);
  if (head) renderJobLogHead(head, current);
}

function renderJobLogHead(head, current) {
  const job = (state.jobs || []).filter((item) => item.id === current.jobId)[0];
  const status = job ? jobStatusOf(job) : '';
  clear(head);
  head.append(el('span', 'job-log-title', '执行过程'),
              el('span', 'job-log-id', current.jobId));
  if (status) {
    head.append(el('span', `status-badge status-${status}`, JOB_STATUS_LABEL[status]));
  }
  if (job && job.elapsedMs) {
    head.append(el('span', 'muted', '耗时 ' + formatDuration(job.elapsedMs)));
  }
  head.append(el('span', 'muted', `${current.lines.length} 行`));
  if (current.lastTs) {
    // `ts` 就是 epoch 秒（`models.now()` 与任务时间戳同一把尺子），
    // **不再除以 1000**：除错的话"最后一行"会显示成 1970 年。
    head.append(el('span', 'muted', '最后一行 ' + formatRelative(current.lastTs)));
  }
  head.append(el('span', 'spacer'));
  const close = el('button', 'btn sm', '关闭');
  close.type = 'button';
  close.dataset.jobLogClose = '1';
  head.append(close);
}

/** 正文：只追加增量。
 *
 *  整段重画只发生在两处——`jobLogPanel`（列表那一拍）和裁剪之后（`drawn`
 *  大于现有行数）。这里**不再自己找容器重建**：面板可能因为列表刚被清空而
 *  暂时不在 DOM 里，那种情况下什么都不做是对的，下一拍 `renderJobs` 会把
 *  已有的 `lines` 全部补上。
 */
function renderJobLog() {
  const current = state.jobLog;
  if (!current) return;
  const host = $('#job-log');
  if (!host) return;
  const head = $('.job-log-head', host), body = $('.job-log-body', host);
  if (!head || !body) return;
  renderJobLogHead(head, current);

  if (current.drawn > current.lines.length) {      // 触发过裁剪 → 重画
    clear(body);
    current.drawn = 0;
  }
  const fragment = document.createDocumentFragment();
  for (let index = current.drawn; index < current.lines.length; index += 1) {
    fragment.append(el('div', 'job-log-line', current.lines[index]));
  }
  body.append(fragment);
  current.drawn = current.lines.length;

  const idle = $('.job-log-idle', body);
  if (idle && current.lines.length) body.removeChild(idle);
  if (!current.lines.length && !current.ended && !idle) {
    body.append(el('div', 'job-log-line job-log-idle',
                   '等待第一行输出…（这条作业还没写出任何过程记录）'));
  }

  // 提示块随状态变，所以每次都替换掉重建——它只有一行，代价可以忽略。
  const existing = $('.job-log-notice', host);
  if (existing) existing.remove();
  const notices = jobLogNotice(current);
  if (notices) host.insertBefore(notices, body);

  if (current.sticky) body.scrollTop = body.scrollHeight;
}

/** 清空已结束的作业：只删调度记录，不动已写入链路的证据。 */
async function clearJobs() {
  const button = $('#jobs-clear'), message = $('#jobs-message');
  const finished = state.jobs.filter((job) => JOB_FINISHED.has(jobStatusOf(job))).length;
  if (!finished) return;
  const kept = state.jobs.length - finished;
  if (!window.confirm(`确定清空 ${finished} 条已结束的作业？`
    + (kept ? `\n队列中还有 ${kept} 条待执行/执行中的作业，会保留。` : '')
    + '\n只删调度记录，已写入的链路证据不受影响。')) return;
  await runJobClear(button, message, {}, finished);
}

/** 一键清空：连队列里待执行/执行中的记录一起清。
 *
 *  它和上面那颗是**两种代价**的动作，所以分成两颗按钮、两次确认：清历史是日常操作，
 *  而取消别人排队中的任务不是——后者必须由人明确说出"全部"，不能和"清历史"共用
 *  一个默认值（后端同样只在 `all=true` 时才动 pending/running）。
 */
async function clearAllJobs() {
  const button = $('#jobs-clear-all'), message = $('#jobs-message');
  if (!state.jobs.length) return;
  if (!window.confirm(`确定清空全部 ${state.jobs.length} 条作业记录？\n`
    + '待执行与执行中的记录也会一并删除（执行中的作业会失去调度面）。\n'
    + '已写入的链路证据不受影响。')) return;
  await runJobClear(button, message, { all: true }, state.jobs.length);
}

async function runJobClear(button, message, body, expected) {
  if (button) button.disabled = true;
  if (message) message.textContent = '正在清空…';
  try {
    const data = await apiPost('/jobs/clear', body);
    await loadJobs(true, false);
    if (message) {
      message.textContent = `已清空 ${formatCount(data.deleted)} 条`
        + (data.kept ? `，保留 ${formatCount(data.kept)} 条` : '');
    }
  } catch (error) {
    if (button) button.disabled = false;
    if (message) message.textContent = '';
    showError('清空作业失败（约 ' + formatCount(expected) + ' 条）：' + error.message);
  }
}
/** 部署自检 + 信息 + 示例；已加载过就不重复请求。 */
async function loadSetup(force) {
  if (state.setupLoaded && !force) return;
  try {
    const data = await apiGet('/setup');
    state.setup = data;
    state.setupLoaded = true;
    renderSetup(data);
  } catch (error) {
    state.setupLoaded = false;
    renderSetupFailure(error);
    showError('部署信息加载失败：' + error.message);
  }
}
async function loadExecutors(force) {
  if (state.executorsLoaded && !force) return;
  try {
    const data = await apiGet('/executors');
    state.executors = asArray(data.executors);
    state.executorsLoaded = true;
    renderSetupExecutors(null);
  } catch (error) {
    state.executorsLoaded = false;
    state.executors = [];
    renderSetupExecutors(error);
    showError('执行节点列表加载失败：' + error.message);
  }
}
// ------------------------------------------------------------ 外部工具管理
/** 安装渠道 → 界面说法。**标识符留在数据里，中文只出现在这里**：
 *  同一个 channel 在十几个适配器里反复出现，写死在每一行就是十几处可以漂移的
 *  说法。数字不写进注释：新增适配器时没人会记得回来改它。 */
const TOOL_CHANNELS = {
  zip: '项目目录 · 压缩包',
  github_release: '项目目录 · GitHub 发布',
  github_archive: '项目目录 · GitHub 源码',
  npm: '项目目录 · npm 依赖',
  wsl: 'WSL 发行版 · apt',
  manual: '系统自带 · 手工安装',
};
/** 操作状态 → 徽章文字。`pending`/`queued` 是队列里还没拿到手的那些。 */
const TOOL_STATUS = { running: '进行中', queued: '排队中', pending: '排队中',
  done: '完成', failed: '失败', cancelled: '已取消' };
/** 还在跑的三种状态：有任意一条就要继续轮询。 */
const TOOL_LIVE = ['running', 'queued', 'pending'];
/** 工具操作的轮询节奏。比作业列表（10 秒）快得多，因为安装是**用户刚点下的
 *  那一件事**，而他正盯着那一行看。 */
const TOOLS_REFRESH_MS = 1500;
const TOOL_STATE_LABEL = { busy: '进行中…', ok: '已就绪', missing: '未安装',
  manual: '需手工安装' };

/** 一个工具此刻该显示成什么。四态，顺序就是优先级：有操作在跑 > 装着 > 能装 > 只能手工。 */
function toolStateOf(item, busy) {
  if (busy) return 'busy';
  if (item.installed) return 'ok';
  return item.installable ? 'missing' : 'manual';
}
/** 绝对路径 → 相对平台根的说法。换机器、换盘符之后这段文字仍然成立。 */
function toolShortPath(value, data) {
  const text = String(value || ''), root = String((data && data.rootPath) || '');
  if (text && root && text.indexOf(root) === 0) {
    return String((data && data.root) || 'data/tools') + text.slice(root.length).replace(/\\/g, '/');
  }
  return text;
}
/** 记下已经见过终态的操作，返回"这一轮新结束的"。首屏不返回：历史记录不是新闻。 */
function rememberToolOps(ops) {
  if (!state.toolSeenOps) state.toolSeenOps = new Set();
  const fresh = [];
  asArray(ops).forEach((op) => {
    if (TOOL_LIVE.includes(op.status) || state.toolSeenOps.has(op.id)) return;
    state.toolSeenOps.add(op.id);
    fresh.push(op);
  });
  return fresh;
}
/** 操作结束后那一行说明。**成与不成都要说**：队列安静下来而界面什么都没说，
 *  与"没反应"是同一件事。 */
function toolNoteText(fresh) {
  const ok = fresh.filter((op) => op.status === 'done');
  const bad = fresh.filter((op) => op.status !== 'done');
  const parts = [];
  if (ok.length) {
    parts.push('已完成：' + ok.map((op) => (op.adapter || '—')
      + (op.action === 'remove' ? '（删除）' : '（安装）')).join('、'));
  }
  if (bad.length) {
    parts.push('失败：' + bad.map((op) => (op.adapter || '—') + ' — '
      + ((String(op.reason || '').split('\n')[0] || TOOL_STATUS[op.status] || op.status || '')).slice(0, 120)).join('；'));
  }
  return parts.join(' · ');
}
/** 适配器表的来源与状态。三种形态，说的话各不相同：
 *
 *  * `error` 非空 → **配置没被采用**（整份退回内置表）。错误原文照抄，不去
 *    转述成"配置有误"——原文里有"第 3 项（nmap）的 dataFlags 里的 -x 不在
 *    arguments 里"这种能直接照着改的信息。
 *  * 文件存在且通过校验 → 只说路径与项数，正常状态不需要更多字。
 *  * 文件还不存在 → 内置表在工作，并告诉人文件在哪儿、怎么生成。
 */
function toolConfigLine(cfg) {
  const where = cfg.display || cfg.path || 'data/tools.json';
  const line = el('p', 'tool-config');
  if (cfg.error) {
    line.classList.add('tool-config-bad');
    line.append(el('span', 'tool-config-tag', '配置未采用'),
      el('span', '', `${cfg.error}（现在用的是内置表 ${formatCount(cfg.count)} 项，`
        + `文件：${where}）`));
    return line;
  }
  if (cfg.exists) {
    line.append(el('span', 'tool-config-tag', '适配器表'),
      el('span', '', `${where} · ${formatCount(cfg.count)} 项`));
    return line;
  }
  line.classList.add('tool-config-empty');
  line.append(el('span', 'tool-config-tag', '适配器表'),
    el('span', '', `内置表 ${formatCount(cfg.count)} 项 · 还没有 ${where}，`
      + '点「配置文件」生成一份，就能加自己的工具'));
  return line;
}
/** 「配置文件」：不存在就先写一份，再用系统默认程序打开它。
 *
 *  **打不开不算失败**：文件已经落在磁盘上了，把路径写进提示让人自己打开即可。
 *  在没有桌面会话的机器上，这是唯一诚实的答复；把它当错误抛出去，反而连
 *  "文件已经建好、位置在哪"都一起吞掉。
 */
async function openToolConfig() {
  const button = $('#tools-config');
  if (button) button.disabled = true;
  try {
    const out = await apiPost('/tools/config/open', {});
    const where = out.display || out.path || 'data/tools.json';
    state.toolNote = (out.created ? `已生成 ${where} 并` : '已') + '用系统默认程序打开。'
      + (out.opened ? '' : `没打开默认程序（${out.error || '原因未知'}），请手动打开 ${where}。`)
      + '改完保存，回来点「重新检测」生效。';
    await loadTools(true);
  } catch (error) {
    showError('打开适配器配置失败：' + error.message);
  } finally {
    if (button) button.disabled = false;
  }
}
/** 工具管理卡：分组清单 + 最近操作。数据来自 `GET /tools`（一份，不另拉）。 */
function renderTools(data) {
  const panel = $('#tools-panel');
  if (!panel) return;
  const info = data || {};
  const adapters = asArray(info.adapters), ops = asArray(info.ops);
  // 有操作在跑的那一项，按钮要换成"进行中"。**按 adapter 找**，因为同一个工具
  // 可能既装着又在删——那时队列里的那一条才是真话。
  const busy = new Map();
  ops.forEach((op) => { if (TOOL_LIVE.includes(op.status)) busy.set(op.adapter, op); });
  rememberToolOpen(panel);                    // 重画不许把用户展开的那一组收回去
  clear(panel);
  const summary = $('#tools-summary');
  if (summary) {
    summary.textContent = `已装 ${formatCount(info.installed)} / ${formatCount(info.total)}`
      + (info.manual && info.installed < info.total
        ? `（${formatCount(info.manual)} 项只能手工装）` : '')
      + ` · 工具根 ${info.root || '—'} · 检测于 ${formatClock(info.checkedAt)}`;
  }
  if (state.toolNote) panel.append(el('p', 'tool-note', state.toolNote));
  // 适配器表来源要画在**清单之前**：配置写错时平台会静默退回内置表继续工作，
  // 于是"我加的那个工具"看起来只是没装好——人就会去点安装、翻日志，唯独不会
  // 想到去改配置。这一行是唯一会说出"你的配置没被采用"的地方。
  if (info.config) panel.append(toolConfigLine(info.config));
  if (!adapters.length) { panel.append(emptyBlock('后端未返回外部工具清单')); return; }
  // 按分类分组：十几项平铺成一列读不完，而分类本来就是配方里已有的字段。
  // 分组顺序按**首次出现**排，不重排——顺序应该跟着后端给的清单走。
  const order = [], byClass = new Map();
  adapters.forEach((item) => {
    const key = item.classLabel || item.class || '其他';
    if (!byClass.has(key)) { byClass.set(key, []); order.push(key); }
    byClass.get(key).push(item);
  });
  // 一组一个 <details>，**默认收起**。收起省下来的是 90 行正文，代价是"这一组里
  // 少装了一项"看起来也消失了——所以标题行必须自带读数（见 toolGroupHead）。
  order.forEach((label) => {
    const rows = byClass.get(label);
    const box = el('details', 'tool-group');
    box.dataset.toolGroup = label;
    box.open = state.toolOpen.has(label);      // 重画之后还是用户刚才那个状态
    box.append(toolGroupHead(label, rows, busy));
    const list = el('ul', 'tool-list');
    rows.forEach((item) => list.append(toolRow(item, data, busy.get(item.id))));
    box.append(list);
    panel.append(box);
  });
  panel.append(el('p', 'tool-help',
    '装进项目目录的可以随时删掉重装；WSL 里那几项由发行版的 apt 管，删除要再确认一次；'
    + '系统自带的（如 OpenSSH）平台既不装也不删。'));
  if (ops.length) panel.append(toolOpList(ops));
}
/** 折叠状态要在重画之后还在。
 *
 *  这张卡会自己重画：有操作在跑时每 1.5 秒一次、装完之后 `loadTools(true)` 再一次，
 *  而 `details.open` 不在任何数据里——重画就等于替使用者把刚展开的那一组收回去。
 *  所以画之前先把**眼下 DOM 里开着的**收进 state，画的时候再放回去。
 *
 *  从 DOM 读而不是自己维护一份副本：副本要靠监听 `toggle` 才准，而 `toggle` 不冒泡、
 *  这些节点每轮都重建，漏一次之后两边就永久不一致（界面开着、state 记的是关的，
 *  下一次重画又把它关上——正是这次要修的毛病）。DOM 只有一份，读它不会不一致。
 */
function rememberToolOpen(panel) {
  const open = new Set();
  $$('details.tool-group[open]', panel).forEach((node) => {
    open.add(node.dataset.toolGroup || '');
  });
  state.toolOpen = open;
  const ops = panel.querySelector('details.tool-ops');
  if (ops) state.toolOpsOpen = !!ops.open;
}
/** 一组的折叠头：**收起时这一行就是那一组的全部可见信息**。
 *
 *  只写分类名的话，"表里少装了一项"在收起状态下完全看不见，而"少一项"正是
 *  这张卡要人动手的原因。所以头上带"已装 n/m"，缺的另挂一个徽章；有操作在跑也在
 *  这里说，否则进度被收进折叠里等于没报。
 */
function toolGroupHead(label, rows, busy) {
  const ready = rows.filter((row) => row.installed).length;
  const live = rows.filter((row) => busy.has(row.id)).length;
  const head = el('summary', 'tool-group-head');
  head.append(el('span', 'tool-group-name', label),
    el('span', 'tool-group-count', `已装 ${ready}/${rows.length}`));
  if (ready < rows.length) {
    head.append(el('span', 'tool-badge tool-missing', `缺 ${rows.length - ready}`));
  }
  if (live) head.append(el('span', 'tool-badge tool-busy', `${live} 进行中`));
  return head;
}
/** 一行工具：名字 + 状态徽章 + 版本 / 渠道 / 占用 + 路径 + 用途 + 动作。 */
function toolRow(item, data, op) {
  const phase = toolStateOf(item, !!op);
  const row = el('li', `tool-row tool-${phase}`);
  // 左侧是"这是什么"，右侧是"能对它做什么"，分两栏而不是上下堆：
  // 十几项上下堆时每项要多占 35px，光按钮就让这张卡长 400px 以上。
  const body = el('div', 'tool-body');
  const head = el('div', 'tool-head');
  head.append(el('span', 'tool-name', item.label || item.id),
    el('span', `tool-badge tool-${phase}`, TOOL_STATE_LABEL[phase]),
    el('span', 'tool-id', item.id));
  body.append(head);
  const facts = [];
  if (item.version) facts.push(item.version);
  facts.push(TOOL_CHANNELS[item.channel] || item.channel || '—');
  if (item.size) facts.push(formatBytes(item.size));
  if (item.installedAt) facts.push('装于 ' + formatTime(item.installedAt));
  body.append(el('p', 'tool-meta', facts.join(' · ')));
  const where = toolShortPath(item.execPath || item.path, data);
  if (where) {
    const line = el('p', 'tool-path');
    line.append(el('code', null, where));
    if (item.source) line.append(el('span', 'muted', ' · 来源 ' + item.source));
    body.append(line);
  }
  if (item.why) body.append(el('p', 'tool-why', item.why));
  if (phase === 'manual' && item.install) {
    const hint = el('p', 'tool-why');
    hint.append(document.createTextNode('手工安装：'), el('code', null, item.install));
    body.append(hint);
  }
  const actions = el('div', 'tool-actions');
  if (op) {
    actions.append(el('span', 'tool-badge tool-busy',
      op.action === 'remove' ? '正在删除…' : '正在安装…'));
  } else if (item.installed && item.channel === 'manual') {
    // 我们没装它，也就不删它。给一个能按的删除按钮是**谎报能力**。
    actions.append(el('span', 'muted', '系统自带，不由平台管理'));
  } else if (item.installed) {
    const confirm = state.toolConfirm === item.id;
    const remove = el('button', `btn sm danger${confirm ? ' on' : ''}`,
      confirm ? '确认删除（apt remove）' : '删除');
    remove.type = 'button';
    remove.dataset.toolAction = 'remove';
    remove.dataset.toolAdapter = item.id;
    actions.append(remove);
  } else if (item.installable) {
    const install = el('button', 'btn sm', '安装');
    install.type = 'button';
    install.dataset.toolAction = 'install';
    install.dataset.toolAdapter = item.id;
    actions.append(install);
  } else {
    actions.append(el('span', 'muted', '没有自动安装渠道'));
  }
  row.append(body, actions);
  return row;
}
/** 「最近操作」的折叠头：条数、失败数、有没有在跑。
 *
 *  这三样是"要不要展开"的依据。缺了它们，收起状态等于一句"最近操作"，
 *  而装失败恰恰是最该被看见的那一条。
 */
function toolOpsHead(ops) {
  const head = el('summary', 'tool-group-head');
  head.append(el('span', 'tool-group-name', '最近操作'),
    el('span', 'tool-group-count', `${ops.length} 条`));
  const failed = ops.filter((op) => op.status === 'failed').length;
  if (failed) head.append(el('span', 'tool-badge tool-failed', `${failed} 条失败`));
  if (ops.some((op) => TOOL_LIVE.includes(op.status))) {
    head.append(el('span', 'tool-badge tool-busy', '进行中'));
  }
  return head;
}
/** 最近操作。只留 8 条：这是一张"刚发生了什么"的表，不是审计日志。
 *
 *  **默认收起**：它是历史，而这张卡的主体是"这些工具现在怎么样"。收起不等于藏起来
 *  ——标题里带条数与失败数，装失败的那一次不用等使用者想到去展开。
 */
function toolOpList(ops) {
  const shown = ops.slice(0, 8);
  const box = el('details', 'tool-ops');
  box.open = state.toolOpsOpen;
  box.append(toolOpsHead(shown));
  const list = el('ul', 'tool-op-list');
  shown.forEach((op) => {
    const item = el('li', 'tool-op'), head = el('div', 'tool-op-head');
    const tone = op.status === 'done' ? 'ok' : (op.status === 'failed' ? 'failed' : 'busy');
    head.append(el('span', 'tool-op-time', formatClock(op.createdAt)),
      el('span', 'tool-op-name', op.adapter || '—'),
      el('span', 'tool-op-action', op.action === 'remove' ? '删除' : '安装'),
      el('span', `tool-badge tool-${tone}`, TOOL_STATUS[op.status] || op.status || '—'));
    item.append(head);
    const detail = String(op.reason || op.output || '').trim();
    if (detail) item.append(el('p', 'tool-op-detail', detail.split('\n')[0].slice(0, 240)));
    list.append(item);
  });
  box.append(list);
  return box;
}
/** 工具清单。`force` 用于装完之后重取（版本号、占用、注册表都变了）。 */
async function loadTools(force) {
  if (state.toolsLoaded && !force) return;
  try {
    const data = await apiGet('/tools');
    state.tools = data;
    state.toolsLoaded = true;
    // 这里只记"见过"，不播报：进这张卡时看到的都是历史，而历史不是新闻。
    rememberToolOps(asArray(data.ops));
    renderTools(data);
    if (asArray(data.ops).some((op) => TOOL_LIVE.includes(op.status))) startToolsWatch();
  } catch (error) {
    state.toolsLoaded = false;
    setPanelMessage('#tools-panel', errorBlock('外部工具清单加载失败：' + error.message));
    const summary = $('#tools-summary');
    if (summary) summary.textContent = '加载失败';
    showError('外部工具清单加载失败：' + error.message);
  }
}
/** 「重新检测」：丢掉 detect 缓存重跑一遍版本探测（约 0.4 秒），要求管理员。 */
async function checkTools() {
  const button = $('#tools-check');
  if (button) { button.disabled = true; button.textContent = '检测中…'; }
  try {
    const data = await apiPost('/tools/check', {});
    state.tools = data;
    state.toolsLoaded = true;
    state.toolNote = '';
    rememberToolOps(asArray(data.ops));
    renderTools(data);
    if (asArray(data.ops).some((op) => TOOL_LIVE.includes(op.status))) startToolsWatch();
  } catch (error) {
    showError('重新检测外部工具失败：' + error.message);
  } finally {
    if (button) { button.disabled = false; button.textContent = '重新检测'; }
  }
}
/** 点「安装」或「删除」。**删除 WSL 里那几项要两次点击**：那一条会动到发行版
 *  之外（别的使用者、别的脚本可能正依赖它），而 `confirm()` 是浏览器级模态——
 *  既没法被主题化，也没法让人看清"点掉的是哪一个工具"。 */
async function runToolAction(action, adapter, button) {
  const data = state.tools || {};
  const item = asArray(data.adapters).find((row) => row.id === adapter);
  if (!item) return;
  const needConfirm = action === 'remove' && item.channel === 'wsl';
  if (needConfirm && state.toolConfirm !== adapter) {
    state.toolConfirm = adapter;
    state.toolNote = `再点一次「确认删除」：这会执行 apt remove，把「${item.label || adapter}」`
      + '从 WSL 发行版里退回到"没有装"的状态（发行版本身保留）。';
    renderTools(data);
    setTimeout(() => {
      if (state.toolConfirm === adapter) {
        state.toolConfirm = '';
        renderTools(state.tools || {});
      }
    }, 8000);
    return;
  }
  state.toolConfirm = '';
  if (button) button.disabled = true;
  state.toolNote = `${item.label || adapter}：${action === 'install' ? '安装' : '删除'}已排队，完成后这里会说明结果。`;
  renderTools(data);
  const body = { adapter };
  if (needConfirm) body.confirm = true;
  try {
    await apiPost(action === 'install' ? '/tools/install' : '/tools/remove', body);
    await loadTools(true);
    startToolsWatch();
  } catch (error) {
    state.toolNote = '';
    if (button) button.disabled = false;
    renderTools(state.tools || {});
    showError((action === 'install' ? '安装' : '删除') + '「' + (item.label || adapter)
      + '」失败：' + error.message);
  }
}
/** 有操作在跑时的轮询。**只在有活时存在**：一个空转的定时器会在装完之后继续
 *  问"装完了吗"，而它每次打的服务端要重读一遍注册表。 */
function startToolsWatch() {
  if (state.toolsTimer) return;
  state.toolsTimer = setInterval(pollTools, TOOLS_REFRESH_MS);
}
function stopToolsWatch() {
  if (!state.toolsTimer) return;
  clearInterval(state.toolsTimer);
  state.toolsTimer = null;
}
async function pollTools() {
  if (state.toolsPolling) return;
  state.toolsPolling = true;
  try {
    const payload = await apiGet('/tools/ops');
    const ops = asArray(payload.ops), pending = Number(payload.pending) || 0;
    if (state.tools) {
      state.tools.ops = ops;
      state.tools.pending = pending;
      renderTools(state.tools);
    }
    if (pending > 0 || ops.some((op) => TOOL_LIVE.includes(op.status))) return;
    stopToolsWatch();
    // 收尾这一下才重取整张卡：版本号、占用、注册表都是那一件事的结果。
    // "完成/失败"那句也在这里说——队列安静下来而界面什么都不说，与"没反应"
    // 是同一件事（这一条在自动继续那次已经吃过一次）。
    const text = toolNoteText(rememberToolOps(ops));
    if (text) state.toolNote = text;
    await loadTools(true);
  } catch (error) {
    // 轮询失败就停：下一次进这张卡会重新起。1.5 秒一次地持续失败，只会把全局
    // 错误条刷成一堵墙，而它挡住的地方正是使用者要找的那个按钮。
    stopToolsWatch();
  } finally {
    state.toolsPolling = false;
  }
}
/** 作业列表；quiet 用于自动刷新，避免每次失败都弹全局错误条。 */
function loadJobs(force, quiet) {
  if (state.jobsLoaded && !force) return Promise.resolve();
  if (state.jobsInflight) return state.jobsInflight;
  state.jobsInflight = apiGet('/jobs')
    .then((data) => { state.jobs = asArray(data.jobs); state.jobsLoaded = true; renderJobs(null); })
    .catch((error) => {
      state.jobsLoaded = false;
      renderJobs(error);
      if (!quiet) showError('作业列表加载失败：' + error.message);
    })
    .finally(() => { state.jobsInflight = null; });
  return state.jobsInflight;
}
/** 进入部署页签：自检 / 部署信息 / 执行节点 / 侵入授权（已加载的不重复请求）。
 *  任务**不在这里**——它跟着「作业」页走了，那是另一个页签的加载路径。 */
function loadSetupTab(force) {
  renderSetupLoading();
  loadSetup(force);
  loadExecutors(force);
  loadAuthz(force);
  loadTools(force);
}
/** 进入作业页签：只拉作业列表。
 *
 *  这里原来还拉基线列表与下发表单要用的 `/setup`（示例、探测作用域）。下发入口与
 *  基线列表撤掉之后，那两条请求在本页没有消费者了——留下的只是每次切入两次白跑
 *  的网络往返，以及"示例加载失败"这种与作业页无关的错误提示。 */
function loadJobsTab(force) {
  renderJobsLoading();
  loadJobs(force, false);
}
/** 部署页的加载态。 */
function renderSetupLoading() {
  if (!state.setupLoaded) {
    setPanelMessage('#setup-checks', emptyBlock('正在加载部署自检…'));
    setPanelMessage('#setup-info', emptyBlock('正在加载部署信息…'));
  }
  if (!state.executorsLoaded) {
    setPanelMessage('#executor-list', emptyBlock('正在加载执行节点…'));
    const summary = $('#executor-summary');
    if (summary) summary.textContent = '正在加载…';
  }
  if (!state.authzLoaded) {
    setPanelMessage('#authz-panel', emptyBlock('正在加载侵入授权…'));
    const summary = $('#authz-summary');
    if (summary) summary.textContent = '正在加载…';
  }
  if (!state.toolsLoaded) {
    setPanelMessage('#tools-panel', emptyBlock('正在加载外部工具…'));
    const toolsSummary = $('#tools-summary');
    if (toolsSummary) toolsSummary.textContent = '正在加载…';
  }
}
/** 作业页的加载态：只有作业列表一块。 */
function renderJobsLoading() {
  if (!state.jobsLoaded) setPanelMessage('#job-list', emptyBlock('正在加载作业列表…'));
}
/** 把目标列表交给「会话」侧边栏渲染。
 *
 *  数据（`/engagements`）由本模块持有、渲染由对话模块负责，是为了不出现第二份
 *  请求——两份请求会各自持有自己的快照，然后在新目标出现时显示得不一样，而
 *  "同一个东西在两处长得不同"是最难被当成缺陷报上来的那种问题。
 */
function renderGoalList() {
  const chat = window.PentestChat;
  if (chat && typeof chat.renderGoals === 'function') chat.renderGoals(state.engagementList);
}
/** 把作业列表交给「会话」上下文栏渲染——数据（`/jobs`）由本模块持有。
 *
 *  与 `renderGoalList` 同一分工、同一理由：两处各拉一次 `/jobs` 会各自持有快照。
 *  这次多一个约束——**`error` 必须传下去**：侧栏那一块要能显示"加载失败"，
 *  否则它会安静地留在上一批数据上，而那看起来完全正常。
 */
function renderJobList(error) {
  const chat = window.PentestChat;
  if (chat && typeof chat.renderSideJobs === 'function') chat.renderSideJobs(error);
}
/** 下发作业可能新建 engagement：静默刷新列表，保持当前选择不变。
 *
 *  原来这里还负责重建顶栏那个 `<select>`；选择器撤掉之后这件事没有了，但
 *  **列表本身仍是唯一真相**——侧边栏、顶栏的目标指示、漏洞/资产徽章都读它。
 */
async function refreshEngagementOptions() {
  try {
    const list = asArray((await apiGet('/engagements')).engagements);
    state.engagementList = list;
    renderGoalList();
    // 报告目录里那一行「本目标汇总」显示的是目标值，它读 `state.engagementList`。
    // 少了这一句，刚下发完任务（可能顺带新建了目标）回到报告页，那一行还写着旧名字。
    renderReportSide(null);
    if (list.some((item) => item.id === state.eid)) renderScopeStats(state.eid);
    else if (list.length) await selectEngagement(list[0].id);   // 目标被删了，落到第一个
    else renderScopeStats('');
  } catch (error) {
    showError('目标列表刷新失败：' + error.message);
  }
}
/** 作业列表自动刷新：作业页签与对话页签内跑（两处都有作业读数），切走立刻停。 */
function startJobsAuto() {
  if (state.jobsTimer) return;
  state.jobsTimer = setInterval(() => loadJobs(true, true), JOBS_REFRESH_MS);
  $('#jobs-auto-hint').textContent = `每 ${JOBS_REFRESH_MS / 1000} 秒自动刷新`;
}
function stopJobsAuto() {
  if (!state.jobsTimer) return;
  clearInterval(state.jobsTimer);
  state.jobsTimer = null;
  $('#jobs-auto-hint').textContent = '自动刷新已暂停（回到本页签恢复）';
}
/** 让任务读数保持新鲜：立刻拉一次（走缓存），然后按固定节奏刷新。
 *
 *  这是**一处入口**，给"在作业页签之外也想看见任务读数"的地方用——目前只有对话
 *  页签的上下文栏。调用方不需要知道"拉一次"与"起定时器"是两件事，也不需要知道
 *  缓存与在途去重：那些都是本模块的事，露出去就一定会在某处漏掉一半。
 *
 *  少了这一句时那一块的表现很安静：它停在登录那一刻的内容（`onSignedOut()` 写下
 *  的"登录后显示"），直到用户切到作业页签再切回来。**看起来像"这个部署没有任务"**。
 */
function watchJobs() {
  loadJobs(false, true);
  startJobsAuto();
}

/* ------------------------------------------- 侧边栏读数的新鲜度（pulse） ---- */
/** 目标列表的**静默**刷新：只更新列表与依赖它的读数，**不动选择**。
 *
 *  与 `loadEngagements()` 的区别是刻意的。那个入口是启动用的：它把选中目标拉到
 *  列表第一条，目标凭空消失时还会重取当前页签的数据。定时刷新绝不能这么干——
 *  使用者正在看的漏洞列表会在 3 秒后被换成第一个目标的。
 *
 *  失败静默：这是后台心跳，下一轮 3 秒后还会拉。一次失败弹一条红字，会在网络抖动
 *  时刷满屏；而真正的失败仍然会暴露——`loadSessions` 与 `/engagements` 各自会报错。
 */
async function refreshEngagementList() {
  let list = [];
  try {
    list = asArray((await apiGet('/engagements')).engagements);
  } catch (error) {
    // 这一趟**没拉成** → 手上这份的来路重新变成"不清楚"，下一轮探询会再补一次。
    // 不这样做的失败形态很隐蔽：调用方已经把指纹记成"新的"了（它按"这一轮问过"记，
    // 不知道请求的结果），而列表其实没更新——那份旧的会一直停在那儿等**下一次**变化，
    // 而它可能再也不会来。
    state.listStamps = null;
    return;
  }
  state.engagementList = list;
  if (state.eid && !list.some((item) => item.id === state.eid)) {
    // 当前选中的目标被删了。这**不是**静默刷新能处理的情况：漏洞 / 资产 / 报告还
    // 显示着它的数据，而那个目标已经不存在了。交给启动那条路重新定位（它会
    // 重取当前页签）。放在这里而不是让调用方判，是因为只有拉完列表才知道这件事。
    await loadEngagements();
    return;
  }
  renderGoalList();
  renderFindingsTargets();         // 漏洞页左栏的目标名与条数
  renderScopeStats(state.eid);     // 导航上的漏洞 / 资产徽章
  // 报告目录里那一行「本目标汇总」显示的是**目标值**。不重画它的表现很具体：
  // 刚让模型建了个目标，切到报告页，那一行还写着旧名字。
  renderReportSide(null);
}
/** 一次"变了没有"的探询 → 只重拉真变了的那一份。
 *
 *  ## 为什么要先探再拉
 *
 *  直接每 3 秒重拉 `/engagements` 与 `/chat/sessions` 也能让读数变新，但那样每一
 *  轮都要**重画**列表容器——而两个列表里都是按钮。表现是"鼠标停在哪一行、哪一行
 *  就闪"，以及点不下去（元素在手底下被换掉）。探询把重画收敛到真的变了的时候。
 *
 *  ## 新旧指纹都存在**各自的模块**里
 *
 *  这里只比对目标那两块（本模块持有 `/engagements`），会话那两块交给 `chat.js`
 *  自己比（它持有 `/chat/sessions`）。不是图省事：两个列表本来就有两个主人，
 *  让其中一个替对方记"上一轮长什么样"，就又多出一份会过期的快照。
 *
 *  ## 漏洞与资产也走这一趟
 *
 *  它们和服务端那份指纹一样是**按目标**取的。前三块（目标 / 会话 / 消息）回答
 *  "侧边栏那几个数变了没有"，后两块（漏洞 / 资产）回答"这两个页签的正文变了没有"
 *  ——而后者恰恰是最需要自动的那一处：跑完一次扫描，证据先落库，人是盯着页面等它
 *  出现的。重拉只发生在**当前页签**上，且只重拉真的变了的那一份。
 */
async function pumpPulse() {
  const chat = window.PentestChat;
  const sessionId = chat && typeof chat.currentSessionId === 'function'
    ? String(chat.currentSessionId() || '') : '';
  let stamps = null;
  try {
    stamps = (await apiGet('/pulse?sessionId=' + encodeURIComponent(sessionId)
      + '&engagementId=' + encodeURIComponent(state.eid || ''))).stamps || null;
  } catch (error) { return null; }
  if (!stamps) return null;
  const previous = state.pulse;
  state.pulse = stamps;
  /* 上一轮心跳量的是**哪一条对话**的流量。换了对话要单独记一笔：两边都还是 0 条时
     指纹一字不差（`"n=0 m=0"`），而列表该整张换掉——只看指纹的话，从一条空对话切
     到另一条空对话，屏幕上留着上一条对话的表。`null` 表示还没量过，第一轮不算换。 */
  const switchedSession = state.trafficSession !== null && state.trafficSession !== sessionId;
  state.trafficSession = sessionId;
  /* 「目标」那两块判的是"**我手上这份列表**是不是旧的"，不是"服务端上一轮说了什么"。
     两者在启动那几秒里会分叉：`init()` 拉列表与第一次探询是**并发**的，探询先回、
     或者列表那一次请求失败，服务端这一轮的值就已经比页面手上的新——再拿它当基线，
     "目标出现了"这件事永远不会被判定成变化，侧边栏就一直停在「还没有目标。
     下发一次作业会自动建一个。」（实测：删掉旧目标后再提问，服务端建出了新目标，
     而页面上那句话从此没变过）。
     `listStamps === null` = 手上这份的来路不清楚（启动刚拉过、或上一次拉失败了）
     → 补拉一次并把指纹记上，于是"启动时拉到的"与"探询基线"只有一个来路。 */
  const have = state.listStamps;
  const listStale = !have || have.engagements !== stamps.engagements
    || have.nodes !== stamps.nodes;
  if (listStale) {
    state.listStamps = { engagements: stamps.engagements, nodes: stamps.nodes };
    await refreshEngagementList();
  }
  // "变了"只在**两边都量过这一项**时成立。上一轮没有这一项（比如那时还没选目标）
  // 不算变化：那一刻的页签内容是刚加载的，不存在过期。提到 `if (previous)` 外面——
  // 换对话那一条要与它并列判（见下面流量那段），而它也得在主块之外成立。
  const changed = (key) => Boolean(previous) && previous[key] !== undefined
    && previous[key] !== stamps[key];
  if (previous) {
    // 漏洞与资产各自独立判变化：加一条漏洞不该让资产树重画，反过来也一样——而
    // "重画"正是这一整套心跳从头到尾在避免的那件事。
    // 漏洞页的两种范围各有自己的"变了"信号：
    //   * 单目标：`findings`（那个目标的行数 + 最大 rowid）；
    //   * 全部目标：`nodes`。聚合快照里**别的**目标新落一条漏洞，当前目标的
    //     `findings` 指纹一动不动，只有全局节点数看得见。
    // 两个条件写成互斥的：同时成立时（比如当前目标自己多了一条）如果各拉一次，
    // 一轮心跳就是两次全量聚合——而第二趟拿到的还是第一趟刚写进去的那份。
    if (state.tab === 'findings' && state.findingsScope === '' && changed('nodes')) {
      await loadAllFindings(true);
    } else if (state.tab === 'findings' && changed('findings')) {
      await loadFindings(true);
    }
    if ((state.tab === 'findings' || state.tab === 'assets') && changed('assets')) {
      await loadAssets(true);
    }
    // 链路的指纹是**节点**（行数 + 最大 rowid）与**目标**：新节点、新证据边都从这两
    // 处来，而一张几千节点的图重取一次要重跑布局，所以只在判到变化时重取。
    // `/pulse` 本身只是一次纯读的紧凑查询，每 3 秒跑一趟不心疼。
    if (state.tab === 'graph' && state.graphAuto
        && (changed('nodes') || changed('engagements'))) {
      await loadGraph(true);
    }
  }
  // 流量：指纹是**那条对话里**的条数与最大行号（服务端按调用者与 `sessionId` 收口，
  // 见 `store.change_stamps` 的 `traffic_user` / `traffic_session`）。判当前页签，与
  // 漏洞 / 资产同一条规矩：别的页签上不该为它重画表格。
  //
  // 两个页签都要：流量页看得见这份列表，请求重发页的右栏也挂着同一份。
  // `switchedSession` 单独算：「换了对话」时指纹可能都是空的，而列表该换。
  if ((state.tab === 'traffic' || state.tab === 'repeater')
      && (switchedSession || changed('traffic'))) {
    const panels = window.PentestPanels;
    if (panels && typeof panels.loadTraffic === 'function') panels.loadTraffic(true);
  }
  if (chat && typeof chat.syncFromPulse === 'function') chat.syncFromPulse(stamps);
  return stamps;
}
/** 读数的自动刷新：跟着**看得见那些读数的页签**走。
 *
 *  规则是"看不见就别拉"——留在后台跑的每一轮都是一次没人看的请求。所以：
 *
 *   * `chat`      —— 侧边栏的「目标」与「对话记录」；
 *   * `findings`  —— 漏洞列表（跑完一次扫描，人就是盯着这里等证据出现的）；
 *   * `assets`    —— 资产树；
 *   * 其余页签一律停。
 *
 *  **链路页签也在这个名单里**（由页头那颗「自动刷新」芯片管，默认开）。它原先被排除
 *  在外，理由是真的：新节点会改变布局尺寸，而 `renderGraph` 当年"布局指纹一变就重置
 *  缩放与平移"，于是自动刷新等于每 3 秒把使用者刚调好的视角拽回去一次。现在那两条
 *  前提都改了——`renderGraph` 只在**画布还没量到尺寸**时摆正视角，重取之后
 *  scale/tx/ty 与选中项一律保留；新数据落在视野外时由 `#graph-stale` 那条提示把
 *  "要不要重新适配"交回使用者。所以链路这一路是"指纹变了才整图重取"，不是每 3 秒
 *  重算一次布局（`loadGraph(true)` 仍然不便宜，一张几千节点的图要重跑服务端布局）。
 *  报告页仍然不在名单里（左侧目录跟着作业列表走，正文不跟），靠它页头那颗「刷新报告」。
 *  两页原先合用栏脚那颗全局「刷新」，那颗已经撤掉了。
 */
function startPulseAuto() {
  if (state.pulseTimer) return;
  pumpPulse();
  state.pulseTimer = setInterval(pumpPulse, PULSE_REFRESH_MS);
}
function stopPulseAuto() {
  if (!state.pulseTimer) return;
  clearInterval(state.pulseTimer);
  state.pulseTimer = null;
  // **指纹刻意不清**：离开这些页签期间攒下的变化，要在回来的**第一次**探询上就被
  // 发现。清掉它，回来的那一轮只会重新建立基线，而页面还要再等一轮（3 秒）才更新
  // ——那 3 秒正好是"我切回来了，怎么还是旧的"。
}
function switchTab(name) {
  // 离开作业页就断掉日志流。不断的话，一个被换走的标签页会一直挂着一个
  // 长连接**直到这条任务跑完**——而定时器与连接都不会因为"页面被换走"
  // 自动消失。这条流的生命周期必须跟着它的可见性。
  if (state.tab === 'jobs' && name !== 'jobs') closeJobLog();
  // 离开部署页签就停掉工具操作轮询。理由同上面那条日志流：定时器不会因为
  // 页面被换走而自己消失，而它每 1.5 秒打一次服务端。
  if (state.tab === 'setup' && name !== 'setup') stopToolsWatch();
  // 左侧导航的「产出 → 报告」是这一页的**入口**，语义是"看这个目标的报告"，
  // 不是"回到我上次读到的那一篇"。实测（夹具里点一次作业行 → 切作业页 → 从导航
  // 回报告）：屏幕上还是那一篇作业报告，页头写着它的作业号，而使用者把这一页当
  // 交付物——他要的是汇总那一篇，于是只能自己再去左栏点一次「本目标汇总」。
  // 目录里点一行仍然只换文档（`showReport`），不经过这里，两者语义不同。
  // 刻意**不清** `reportDoc`：此刻正文还是旧那一篇，卡头也跟着它；等新文档落地时
  // 两处一起换。提前清掉会让卡头先写「本目标汇总」而正文还是作业报告。
  const enteringReport = name === 'report' && state.tab !== 'report';
  if (enteringReport) {
    state.reportJob = '';
    // 左栏高亮也得跟着回汇总。不补这一下，正文写着汇总、高亮还压在上次那条任务上
    // ——而高亮是读者判断"这一篇是哪儿来的"的唯一线索。
    renderReportSide(null);
  }
  state.tab = name;
  $$('.tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.tab === name));
  $$('.panel').forEach((panel) => panel.classList.toggle('active', panel.id === 'panel-' + name));
  // 换页签会换掉整块面板：输入卡片可能跟着出现/消失（会话之外没有它），
  // 通知栈要立刻跟到新位置，不能等到下一次尺寸变化。
  syncNoticeAnchor();
  syncChatTail();
  loadTabData(name);
}
/** 通用筛选芯片：items = [{ key, label, count?, cls? }]。 */
function renderChips(container, items, isOn, onToggle) {
  clear(container);
  items.forEach((item) => {
    const chip = el('button', `chip ${item.cls || ''}${isOn(item.key) ? ' on' : ''}`.trim());
    chip.type = 'button';
    chip.dataset.key = item.key;
    chip.setAttribute('aria-pressed', isOn(item.key) ? 'true' : 'false');
    chip.append(el('span', null, item.label));
    if (item.count !== undefined) chip.append(el('span', 'count', item.count));
    container.append(chip);
  });
  container.onclick = (event) => {
    const chip = event.target.closest ? event.target.closest('.chip') : null;
    if (chip) onToggle(chip.dataset.key);
  };
}
/** 节点类型过滤芯片。 */
function renderKindChips() {
  renderChips($('#kind-filters'), KIND_ORDER.map((kind) => ({ key: kind, label: KIND_LABEL[kind], cls: `kind-${kind}` })),
    (kind) => state.kindFilter[kind] !== false, (kind) => {
      state.kindFilter[kind] = state.kindFilter[kind] === false;
      clearHighlight();
      applyKindFilter();
      renderKindChips();
    });
}
/** 严重度筛选芯片（带各档数量）。 */
function renderSeverityChips() {
  // 计数按**分组**（也就是按卡片）来，不按记录条数。
  //
  // 原来数的是 `state.findings`，分组之后那个数会对不上：芯片说「低危 3」、点进去
  // 只有 1 张卡片。芯片是一个筛选控件，它给出的数应该**预告你将要看到几张卡片**；
  // 记录条数没有丢——每张卡片上写着「3 处」。
  renderChips($('#severity-filters'), SEVERITY_ORDER.map((sev) => ({
    key: sev, label: SEVERITY_LABEL[sev], cls: `sev-${sev}`,
    count: severitySource().filter((item) => severityOf(item.severity) === sev).length,
  })), (sev) => state.severityFilter.has(sev), (sev) => {
    if (state.severityFilter.has(sev)) state.severityFilter.delete(sev); else state.severityFilter.add(sev);
    renderFindings(null);
  });
}
/** 计数的来源：有分组就数分组，没有就数记录（与 `paintFindings` 的分支一致）。 */
function severitySource() {
  if (state.findingsScope === '') {
    // 「全部目标」：芯片预告的是**跨目标**会有几张卡片，所以把每个目标的 groups
    // 拼起来（缺 groups 的用它的 findings）。
    const out = [];
    state.findingsByTarget.forEach((bucket) => {
      if (asArray(bucket.groups).length) out.push(...asArray(bucket.groups));
      else out.push(...asArray(bucket.findings));
    });
    return out;
  }
  return state.findingGroups.length ? state.findingGroups : state.findings;
}
/** 图交互：滚轮缩放、拖拽平移、抬起点选、hover 高亮、工具栏按钮。 */
function bindGraphEvents() {
  // 变量名仍叫 `svg`：它是"绘制面"，改造前是 SVG 根，现在是 canvas。手势全挂在它
  // 上面——玻璃面板之外那一圈留白不该响应拖动。
  const svg = $('#graph-surface');
  svg.addEventListener('wheel', (event) => {              // 以光标位置为锚点缩放
    if (svg.classList.contains('hidden')) return;
    event.preventDefault();
    const point = toUserSpace(event.clientX, event.clientY);
    if (point) zoomAt(point.x, point.y, event.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });
  // 指针被 svg 捕获后 click 的 target 会变成 svg，故按下时记节点、抬起时判定点击
  svg.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || svg.classList.contains('hidden')) return;
    // 画布没有元素可以 `closest`：按下的点落在哪张卡上，要自己用框反查。
    const pressedId = hitNode(event.clientX, event.clientY);
    // 按在卡上 = 拖这张卡，按在空白 = 平移画布。两者共用一套手势状态（手抖阈值、
    // 冻结比例、每帧一次写），只在位移的落点上分叉——按在卡上改卡自己的偏移，
    // 按在空白改视角。
    Object.assign(graphView, {
      pressNodeId: pressedId, dragNodeId: pressedId,
      dragging: true, moved: false,
      lastX: event.clientX, lastY: event.clientY,
      pressX: event.clientX, pressY: event.clientY,
      // 比例在这一刻量一次，整个手势都用它（见 `measureDragScale`）。放在按下时而不是
      // 模块加载时，是因为分栏拖拽条会改画布宽度、"适应窗口"会改 viewBox 基准，
      // 每次重新按下都是一次**重新量的机会**——这正是"缓存"在这里安全的原因。
      dragScale: measureDragScale(),
    });
    svg.classList.add('dragging');
    if (pressedId) svg.classList.add('node-dragging');
    svg.setPointerCapture(event.pointerId);
  });
  svg.addEventListener('pointermove', (event) => {
    if (!graphView.dragging) return;
    // 先按**屏幕像素**判手抖，再决定这是一次拖拽。阈值没跨过就原样返回：
    // 既不平移，也不把 moved 置真——否则鼠标抖半像素，图会被推走一点，
    // 同时抬手的点选还被吞掉。
    if (!graphView.moved) {
      const slop = Math.abs(event.clientX - graphView.pressX)
        + Math.abs(event.clientY - graphView.pressY);
      if (slop < CLICK_SLOP_PX) return;
      graphView.moved = true;
      graphView.touched = true;                            // 人自己平移过（同上）
    }
    // 位移用**冻结的**比例换算，不再逐事件 `toUserSpace`（那每次都强读一次画布
    // 矩形，而它紧跟在上一次写入之后，等于每次事件强制一次布局）。
    const scale = graphView.dragScale;
    if (!scale) return;                                     // 按下时画布量不到尺寸
    const dx = (event.clientX - graphView.lastX) * scale.sx;
    const dy = (event.clientY - graphView.lastY) * scale.sy;
    graphView.lastX = event.clientX;
    graphView.lastY = event.clientY;
    if (graphView.dragNodeId) {
      // 拖卡：把这一步位移加到这张卡自己的偏移上，然后只重画它自己与它那几条边。
      // 不走 `scheduleTransform`（视角没变），也不整张重画（350 张卡时一次 40 ms）。
      const pin = graphView.pins.get(graphView.dragNodeId) || { dx: 0, dy: 0 };
      pin.dx += dx;
      pin.dy += dy;
      graphView.pins.set(graphView.dragNodeId, pin);
      applyPin(graphView.dragNodeId);
      return;
    }
    graphView.tx += dx;
    graphView.ty += dy;
    scheduleTransform();                                    // 每帧最多写一次
  });
  const endDrag = (event, cancelled) => {
    if (!graphView.dragging) return;
    graphView.dragging = false;
    svg.classList.remove('dragging');
    if (svg.hasPointerCapture(event.pointerId)) svg.releasePointerCapture(event.pointerId);
    // 被系统取消的手势（右键菜单、触摸被打断）不是一次点选，不该改选中态
    if (!cancelled && !graphView.moved && graphView.pressNodeId) selectNode(graphView.pressNodeId);
    graphView.pressNodeId = null;
    graphView.dragNodeId = null;
    svg.classList.remove('node-dragging');
  };
  svg.addEventListener('pointerup', (event) => endDrag(event, false));
  svg.addEventListener('pointercancel', (event) => endDrag(event, true));
  svg.addEventListener('mouseover', (event) => {           // hover 高亮相连的边与邻居
    const hoverId = hitNode(event.clientX, event.clientY);
    if (hoverId) highlightNeighborhood(hoverId);
  });
  svg.addEventListener('mouseout', () => clearHighlight());
  $('#graph-canvas').addEventListener('mouseleave', clearHighlight);
  $('#zoom-in').addEventListener('click', () => zoomByFactor(1.2));
  $('#zoom-out').addEventListener('click', () => zoomByFactor(1 / 1.2));
  $('#zoom-fit').addEventListener('click', resetView);
  // 「刷新链路」：自动刷新之外的手动入口。它换掉了原先栏脚那颗全局「刷新」——那一颗
  // 撤掉的理由写在 `index.html` 的栏脚注释里。`hideError()` 与部署页那颗一致：手动重取
  // 时上一次的报错要一起收掉，否则新数据旁边会挂着一条旧红字。
  $('#graph-refresh').addEventListener('click', () => { hideError(); loadGraph(true); });
  // 「自动刷新」芯片：它只决定**心跳要不要在这一页跑**，与数据本身无关。关掉之后
  // 数据不再自己进来，「刷新链路」是唯一入口（这与这一页改版前的行为完全一致）。
  const auto = $('#graph-auto');
  const paintAuto = () => {
    if (!auto) return;
    auto.classList.toggle('on', state.graphAuto);
    auto.setAttribute('aria-pressed', state.graphAuto ? 'true' : 'false');
  };
  if (auto) {
    auto.addEventListener('click', () => {
      state.graphAuto = !state.graphAuto;
      paintAuto();
      if (state.graphAuto) startPulseAuto(); else stopPulseAuto();
      // 关掉之后"有新数据"这件事只能靠手点，所以提示也收起来：它指向的动作是
      // "重新适配视角"，而此刻数据根本还没到，留着只会让人以为图已经更新过了。
      if (!state.graphAuto) setGraphStale(false);
    });
    paintAuto();
  }
  // 「链路已更新」提示：点它 = 接受一次重新适配（`resetView`），然后提示消失。
  const stale = $('#graph-stale');
  if (stale) stale.addEventListener('click', () => { hideError(); resetView(); setGraphStale(false); });
  // 画布第一次量到尺寸时补上那次没算准的视角。
  //
  //   `init()` 无条件取一次链路，而那时页签常常还没露出来（`#graph-canvas` 是 0x0），
  //   `fitScale()` 返回 0，`initialGraphScale()` 只能退回基准 600%，摆正也算不出该
  //   往哪挪——于是落点是**布局原点的左上角**，而卡片从 `MARGIN` 起画、同层的还按列
  //   居中，画面上就是一片空网格。这条观察器让「量到了」这件事自己触发一次重画：
  //   `state.graphScaleMeasured` 在那一趟里被置真，之后画布再变宽变窄都不再替他动视角
  //   （那是分栏拖拽的人为操作，不是「第一次看见」）。
  const canvasEl = $('#graph-canvas');
  if (canvasEl && window.ResizeObserver) {
    const canvasObserver = new ResizeObserver(() => {
      if (state.graphScaleMeasured || !state.graphLoaded) return;
      const rect = canvasEl.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) renderGraph();
    });
    canvasObserver.observe(canvasEl);
  }
  renderKindChips();
}
/** 页签、搜索、资产树、顶栏的绑定。 */
function bindEvents() {
  $$('.tab').forEach((tab) => tab.addEventListener('click', () => switchTab(tab.dataset.tab)));
  let debounce = null;
  $('#finding-search').addEventListener('input', (event) => {
    state.keyword = event.target.value;
    clearTimeout(debounce);
    debounce = setTimeout(() => renderFindings(null), 120);
  });
  // 漏洞页左栏：一次事件委托。列表会被心跳重画（目标增减），逐个绑会随每次重画
  // 再叠一批监听器——与作业列表、报告目录同一条理由。
  $('#findings-targets').addEventListener('click', (event) => {
    const row = event.target.closest ? event.target.closest('.target-row') : null;
    if (row) selectFindingsScope(row.dataset.targetScope || '');
  });
  $('#asset-tree').addEventListener('click', (event) => {
    const button = event.target.closest ? event.target.closest('.tree-btn') : null;
    if (button) selectAsset(button.dataset.assetId);
  });
  $('#asset-detail').addEventListener('click', (event) => {
    const button = event.target.closest ? event.target.closest('.child-btn') : null;
    if (button) selectAsset(button.dataset.assetId);
  });
  // 顶栏那个 `#engagement-select` 已经撤掉：选择搬进了「会话」的侧边栏，
  // 由 chat.js 经 `PentestUI.selectEngagement` 驱动。这里不再监听它。
  // 栏脚那颗全局「刷新」（`#refresh-btn`）同样撤掉了——它刷的是"你当前碰巧在的那
  // 一页 **加上** 状态 / 目标 / 整张链路"，而真正需要它把关的只有链路与报告两页，
  // 那两页现在各有一颗自己的（`#graph-refresh`，以及下面这颗）。`refreshAll()`
  // 本身留着：`boot.js` 换用户后、`chat.js` 删掉当前目标后会**直接调它**。
  $('#error-close').addEventListener('click', hideError);
  // 左侧目录每 10 秒跟着作业列表自己长（`renderReportSide`），但**正文不会**：
  // 它按任务取一次就停在那儿，而"这个任务跑完了没有"正是盯着这一页时最想知道的。
  $('#report-refresh').addEventListener('click', () => { hideError(); loadReport(true); });
  $('#report-copy').addEventListener('click', copyReport);
  $('#report-download').addEventListener('click', downloadReport);
  // 报告目录走事件委托，理由与下面作业列表那条一样：目录每 10 秒重建一次，
  // 在渲染时逐个 addEventListener 会随每次刷新叠加一批监听器。
  // `dataset.jobId` 为空串正是「本目标汇总」那一行，`showReport('')` 会回到汇总。
  $('#report-side').addEventListener('click', (event) => {
    const button = event.target.closest ? event.target.closest('.rpt-row') : null;
    if (button) showReport(button.dataset.jobId || '');
  });
  $('#setup-refresh').addEventListener('click', () => { hideError(); loadSetupTab(true); });
  $('#tools-check').addEventListener('click', () => { hideError(); checkTools(); });
  const configButton = $('#tools-config');
  if (configButton) configButton.addEventListener('click', () => { hideError(); openToolConfig(); });
  // 工具行是每次重画都新建的（轮询每 1.5 秒一次），所以按钮走事件委托，
  // 挂在**静态**的容器上。逐个 addEventListener 会随每次重画漏掉一批监听。
  $('#tools-panel').addEventListener('click', (event) => {
    const button = event.target.closest ? event.target.closest('[data-tool-action]') : null;
    if (!button) return;
    runToolAction(button.dataset.toolAction, button.dataset.toolAdapter, button);
  });
  $('#jobs-refresh').addEventListener('click', () => { hideError(); loadJobs(true, false); });
  $('#jobs-clear').addEventListener('click', clearJobs);
  $('#enroll-btn').addEventListener('click', enrollExecutor);
  $('#enroll-name').addEventListener('keydown', (event) => { if (event.key === 'Enter') enrollExecutor(); });
  $('#enroll-token').addEventListener('keydown', (event) => { if (event.key === 'Enter') enrollExecutor(); });
  $('#executor-list').addEventListener('click', (event) => {
    const button = event.target.closest ? event.target.closest('[data-executor-id]') : null;
    if (button) revokeExecutor(button);
  });
  // 作业列表的三类点击（删除 / 打开日志 / 关闭日志）全部走**同一个**委托，
  // 挂在这个**静态**容器上。
  //
  // 日志面板的关闭按钮必须在这里一起处理，**不能**另外 `$('#job-log')` 绑一次。
  // 面板是 `jobLogPanel()` 现造出来、塞进它自己那一行里的（见 `renderJobs`），
  // 而 `#job-list` 每 10 秒 `clear()` 一次、面板跟着重建。所以直接绑在面板上
  // 会错两次：
  //   * **初始化时就错**：`bindEvents()` 跑在任何渲染之前，此刻 `$('#job-log')`
  //     是 null，那一行当场抛。它抛在 `init()` 的中途，于是后面的
  //     `loadStatus` / `loadGraph` / `loadEngagements` 一行都不执行——表现是
  //     "顶栏、漏洞、资产全空"，而原因是一条日志面板的监听（症状离原因极远）；
  //   * **重建后又错**：就算它当时存在，第一次重建也会把监听丢掉，"关闭"从此
  //     没反应——而那时页面看起来一切正常，只是有一个按钮永远没回应。
  $('#job-list').addEventListener('click', (event) => {
    const button = event.target.closest ? event.target.closest('[data-job-id]') : null;
    if (button) deleteJob(button);
    const follow = event.target.closest ? event.target.closest('[data-job-log]') : null;
    if (follow) toggleJobLog(follow.dataset.jobLog);
    const closer = event.target.closest ? event.target.closest('[data-job-log-close]') : null;
    if (closer) closeJobLog();
  });
  $('#jobs-clear-all').addEventListener('click', clearAllJobs);
}
/** 整站重载：状态 + 全局链路 + 当前目标（含当前页签数据）；部署与作业页签另刷自己那几块。
 *
 *  **它不是任何按钮的处理函数。** 栏脚那颗全局「刷新」已经撤掉——手动刷新现在归各页
 *  自己的 `.ph-actions`（部署 `#setup-refresh`、链路 `#graph-refresh`、报告
 *  `#report-refresh`）。留着这个函数是因为有三处**代码**要它，而在那三处"整站重载"
 *  才是对的语义（换目标只是换一份数据，换用户是换一整个世界）：
 *
 *   * `boot.js` 换用户成功之后：上一个用户的数据必须整体换掉；
 *   * `chat.js` 删掉当前选中的目标之后：被删的目标已经不存在，而页签里挂着的还是
 *     它的数据；
 *   * 同一页面内**重新登录**之后——四个数据页签的缓存（`findings`/`assets`/
 *     `markdown`/`detail`）**刻意不显式清空**，靠这一趟强制重载覆盖（交互规范 §3.4
 *     的数据隔离那一行）。
 */
async function refreshAll() {
  hideError();
  await loadStatus();
  await loadGraph(true);                                 // 链路是全局的，刷新时整体重取
  if (state.eid) await selectEngagement(state.eid);
  else await loadEngagements();
  if (state.tab === 'setup') loadSetupTab(true);
  if (state.tab === 'jobs') loadJobsTab(true);
  // 心跳的入口与 `init()` 同一句、同一理由：上面两条路都可能**提前返回**
  // （目标列表为空时 `loadEngagements` 就是不走到 `selectEngagement` 的）。
  loadTabData(state.tab);
}

/* ---------------------------------------------------------- 14. 启动 ---- */
/** 拉取目标列表，定位到**列表第一条**——目标不再由 URL 决定。
 *
 *  选择器已经搬进「会话」的侧边栏，这里只负责**定位**，列表本身交给
 *  `renderGoalList()` 渲染。定位规则收敛成一条（第一条），所以启动地址永远是那个
 *  **裸根路径**（协议 + 主机 + 端口，后面不带任何查询串）——`selectEngagement()`
 *  已经不再回写 URL。
 */
async function loadEngagements() {
  dropStaleEngagementParam();
  let list = [], failure = null;
  try { list = asArray((await apiGet('/engagements')).engagements); } catch (error) { failure = error; }
  state.engagementList = list;
  // 这份列表是在哪个指纹上拉下来的**不知道**（`/engagements` 不回指纹）→ 下一次探询
  // 补一次并把指纹记上。少了这一句，启动那一刻拉到的空列表会与"服务端那时也是空的"
  // 混在一起：之后目标出现了，谁也判不出这是变化（见 `pumpPulse`）。
  state.listStamps = null;
  renderGoalList();
  if (failure || !list.length) {
    if (failure) showError('目标列表加载失败：' + failure.message);
    renderNoEngagement(failure ? '目标列表加载失败，请检查后端服务' : '后端暂无目标记录');
    return;
  }
  await selectEngagement(list[0].id);
}
/** 清掉地址栏里遗留的 `?engagement=`。
 *
 *  本程序自己**已经不生成**这个参数了（见 `selectEngagement`），但它会从别处到达：
 *  改版前存下来的书签、浏览器历史、以及文档里出现过的示例链接。留着它的表现是
 *  "启动地址上挂着一条已经不起作用的后缀"——而那条后缀看起来仍然像是能决定目标，
 *  下一个读它的人会照着它去解释行为。
 *
 *  删除它，**并且不按它选目标**：目标只有一个来路（列表第一条）。这是刻意的取舍
 *  ——旧链接不再能指定目标，换来的是"地址栏是什么样"只有一种形态。只删这一个
 *  参数，其余查询串原样保留（将来若有 `?tab=` 之类，不该被这里顺手抹掉）。
 */
function dropStaleEngagementParam() {
  const url = new URL(window.location.href);
  if (!url.searchParams.has('engagement')) return;
  url.searchParams.delete('engagement');
  window.history.replaceState(null, '', url);
}
/** 无可用目标时的空状态。
 *
 *  只清**按目标成篇**的那三块（漏洞 / 资产 / 报告）。链路是全局的，它的空态由
 *  `loadGraph` 自己负责——在这里把它一起清掉，会让"目标列表挂了但链路是好的"
 *  显示成两边都空，而那会把一个局部故障报成一个整体故障。
 */
function renderNoEngagement(reason) {
  // 目标全没了 → 漏洞页也回到「全部目标」：收窄在一个已经不存在（或被删掉的）
  // 目标上，左栏会高亮一条不存在的行，正文却还挂着它的旧数据。
  state.findingsScope = '';
  state.findingsByTarget = []; state.findingsTotals = null;
  state.allFindingsLoaded = false;
  renderScopeStats('');
  renderFindingsTargets();
  // 报告页那一栏是**全局**的任务归档，所以这里不能把它一并清掉：没有目标不等于
  // 没有任务历史（目标被删掉之后，任务记录还在，`delete_engagement` 只清会话绑定）。
  // 清空的只有"当前那篇文档"与它那张卡头。
  state.reportJob = '';
  state.reportDoc = null;
  renderReportDocHead();
  renderReportSide(null);
  renderFindings(null);
  setPanelMessage('#findings-list', emptyBlock(reason));
  setPanelMessage('#asset-tree', emptyBlock(reason));
  setPanelMessage('#asset-detail', emptyBlock('请在左侧选择资产'));
  setPanelMessage('#report-body', emptyBlock(reason));
}
/** 界面上**当前可见**的页签——HTML 里那个 `class="tab active"`。
 *
 *  存在的理由是"两处各写一份必然分叉"：`state.tab` 曾经硬写着 `'graph'`，而
 *  `index.html` 上标 `active` 的是对话。于是从启动那一刻起，"屏幕上是哪一块"与
 *  "状态里说是哪一块"就是两个答案，而**心跳按后者决定起不起**（`startPulseAuto`
 *  的名单里有对话、没有链路）——打开页面停在对话上，侧边栏那两个块的心跳一次都没起，
 *  它们停在启动那一刻的内容上再也不动。
 *
 *  读 DOM 而不是另写一个字面量：页签的可见性只有 HTML 那一处权威，`switchTab` 也是
 *  按同一批类名切换的。取不到（HTML 被改坏 / 脚本单独跑）时退回 `'dashboard'`：那是
 *  HTML 的默认可见页签（导航第一位、标着 `active`），而不是某个随便的取值。
 */
function activeTabFromDom() {
  const node = document.querySelector('.tab.active');
  return (node && node.dataset && node.dataset.tab) || 'dashboard';
}
function init() {
  bindEvents();
  bindOrchestrationEvents();
  bindGraphEvents();
  wireNoticeAnchor();                    // 通知浮层要先站到输入卡片上方
  renderSeverityChips();
  renderNodeDetail(null);
  state.tab = activeTabFromDom();        // 必须在 `loadTabData` **之前**：按它决定起不起心跳
  loadStatus();
  loadGraph(false);                                      // 链路不依赖目标，先起来
  loadEngagements();
  /* 心跳**必须在这里起一次**，不能只挂在"切页签才走"的那两条路上（`switchTab` /
     `selectEngagement` 里的 `loadTabData`）。真实启动走的是 boot.js 的 `enterConsole()`
     → 本函数，而上面那句 `loadEngagements()` 在**目标列表为空时会提前返回**：它走
     `renderNoEngagement()` 就直接 return，根本到不了 `selectEngagement`，于是心跳
     一次都没起过。而"还没有目标"正是最需要它的时候——下一次提问会在服务端建出第一个
     目标，侧边栏却停在「还没有目标。下发一次作业会自动建一个。」再也不动。
     实测（`tools/probe_boot_chain.py` 的真实启动链，目标列表为空的那一份）：
     整个启动过程对 `/pulse` 的请求数是 **0**。 */
  loadTabData(state.tab);
}

/* ------------------------------------------- 15. 供新增脚本复用的命名空间 ---- */
/** auth.js / settings.js / guide.js / chat.js 复用同一套 DOM 与 API 工具，避免重复实现。 */
window.PentestUI = {
  API_BASE, $, $$, asArray, clamp, el, clear, truncate, formatTime, formatRelative,
  formatClock, formatCount, formatDuration, emptyBlock, errorBlock, apiGet, apiPost, apiStream, apiStreamGet,
  showError, hideError,
  renderMarkdown, copyText, copyButton, loadTabData, switchTab, refreshAll, init, state,
  // 目标选择搬进对话侧边栏之后，选择这件事仍由本模块持有（它同时决定
  // 漏洞/资产/报告的范围与顶栏那个只读指示），所以入口必须对外暴露。
  selectEngagement, loadEngagements, loadGraph, renderScopeStats,
  // 漏洞页左栏与「全部目标」快照：chat.js 的侧边栏点目标时也要能把漏洞页的范围
  // 一起带过去，所以入口对外暴露。
  loadAllFindings, selectFindingsScope, renderFindingsTargets,
  // 任务的**状态词表与排序**。上下文栏（chat.js）渲染的是同一批任务，但它在另一个
  // 文件里——那份代码要判断"结束了没有""执行中排前面""这个 kind 叫什么"。
  // 让它自己去写一遍（哪怕只是抄一份字面量）的失败形态是：「执行中」与「运行中」
  // 在两个地方并存，而读的人会以为那是两种不同的状态。
  // 所以这里给的是**同一个函数、同一张表**，不是同名副本。
  jobView: {
    statusOf: jobStatusOf, label: JOB_STATUS_LABEL,
    finished: JOB_FINISHED, compare: compareJobs, kindLabel: jobKindLabel,
  },
  // 严重度的**顺序与中文名**。仪表盘要挑出「高危及以上」那几条，原先写的是
  // `['critical', 'high'].includes(...)` 这样一份字面量——后端 `panel()` 判的是
  // `severity_rank(...) <= 1`（权威顺序在 `models.SEVERITIES`），两份写法一旦
  // 有一边改了（新增档位、调整排序），仪表盘的「高危」就会与接口的 `highFindings`
  // 数不上同一批记录，而那种不一致没有任何东西会报警。
  severityView: {
    order: SEVERITY_ORDER, label: SEVERITY_LABEL, of: severityOf,
    rank: (v) => SEVERITY_ORDER.indexOf(severityOf(v)),
  },
  // 节点类型的中文词表（链路图、节点详情、漏洞卡片都用它）。上下文栏「目标」那一行
  // 要**逐类**报计数（意图 / 事实 / 漏洞 / 资产），而那份代码在 chat.js 里。
  // 让它自己抄一份字面量的失败形态与上面 `jobView` 一模一样：同一个 kind 在两处叫
  // 两个名字，读的人会以为那是两种不同的东西。所以这里给的是同一个函数。
  kindLabel: (kind) => KIND_LABEL[kind] || kind,
  // 上下文栏那一块任务读数要会自己长。对话页签在加载时调它，**与作业页签共用
  // 同一个定时器**（不是各起一个：两个定时器会让刷新节奏翻倍，而它们拉到的是
  // 同一份数据）。见 watchJobs 的注释。
  watchJobs,
  // 会话里的工具轨迹点一下要跳到右栏那一条任务（m14866）：那条路径先在快照里找，
  // 找不到就**补拉一次**——刚下发的任务、以及在别的页签里清过列表之后，快照里都
  // 还没有它。补拉这个动作的数据源在会话这边，所以入口要一起给出去。
  loadJobs,
  // 侧边栏两个读数块的新鲜度。对话页签进入时由 `loadTabData` 起，切走时停。
  // 对外暴露是为了让契约测试能直接验"探询会比对指纹、只在变了时才重拉"。
  pumpPulse,
  // 记录区末尾的让位（m18801）。审查卡改成浮层之后，它出现/消失都不一定改变
  // 被观察元素的尺寸（浮动元素脱出流内），所以画完卡片要主动叫一次。
  syncChatTail,
  // 验收探针的入口：画布上没有可查询的元素，点选/拖动只能按坐标点，而坐标换算
  // 只该有一份（与 toUserSpace 互逆）。它不改任何状态，只回答"可见的卡在哪"。
  graphNodePoints,
};
/** 由 boot.js 接管启动：未登录时先只渲染登录门，不渲染工作台内容。 */
if (!window.PentestConsoleGate) document.addEventListener('DOMContentLoaded', init);
