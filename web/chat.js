'use strict';
/* ============================================================================
 * 会话页签 —— 平台里那个完整的 DSH 网页应用：所有命令通过自然语言下达
 *
 * 这里的对话框不是"提示词套壳"：每一轮都由服务端跑一个完整的工具循环
 * （模型决策 → 控制面执行工具 → 结果回灌 → 直到给出结论），模型能调用的动作
 * 见「可用能力」。因此界面上刻意不做任何本地推测——出现的每个作业号、每条漏洞、
 * 每个计数都来自服务端返回的真实数据，并在调用轨迹里可回查。
 *
 * ## 会话入口的三项选择
 *
 * 输入框上方有三个下拉：**Agent（作业口径）/ 模型 / 权限档**。三项都是
 * **会话级**的，不同于设置页里的全局默认值：
 *
 *   * 没选过（“跟随默认”）→ 存空串，实际生效值由服务端每轮现算；
 *   * 选过 → 存具体 id，钉住，不被别人的全局切换带走。
 *
 * 因此这里的下拉在"跟随默认"时会显示服务端算出来的当前值，并在提示里说明
 * 它是跟随来的。界面必须说清这一点：否则用户会以为"显示 A 就等于选了 A"，
 * 而后台换了默认值时这个下拉会莫名其妙地变。
 *
 * 三条与其它页签一致的约束：
 *   1) 全部内容 createElement + textContent 落 DOM，不拼 HTML 字符串；
 *   2) 无外部资源引用（内网离线可用），因此不引入 markdown 库，
 *      回答正文复用 app.js 的 renderMarkdown；
 *   3) 引用的每个 id 都必须在 index.html 里静态存在（契约测试会查）。
 *
 * 一处刻意的取舍：**没有配置模型时不给降级路径**。工具调用的决策必须来自真实模型，
 * 用规则凑一个"看起来在思考"的假象比直接说"请先配模型"更糟。所以此时只显示一条
 * 明确的引导，输入框直接禁用。
 * ==========================================================================*/

(function () {
  /** 空闲看门狗：多久没收到**任何**事件就认定连接死了。
   *
   *  这里刻意没有"整轮超时"。一轮对话可能跑几分钟（模型 + 工具），给整轮设超时
   *  会误杀正常的长任务；完全不设又会让一次悄悄死掉的连接永远转下去。
   *  按事件之间计时同时满足两边：还在往前走就不会被掐，真卡住了才报。
   *  工具执行期间服务端不发事件，所以这个窗口要够宽（工具 + 模型收尾）。 */
  const STREAM_IDLE_MS = 180000;
  const ROLE_LABEL = { user: '我', assistant: 'AI 助手' };

  /** 工具名 → 现成的自然语言说法。**每条都绑一个工具**。
   *
   *  它原来是「提示」那一格的候选清单（渲染时跟这一档真的拿得到的工具取交集）。
   *  「提示」格撤掉之后它没有跟着消失，因为**能力目录那二十多张卡片用的就是这份
   *  文案**——点「可用能力」里的一张卡，填进去的是这句话，而不是"用「start_scan」"。
   *  同一句话只写一遍：两处各写一套的话，同一个动作在两边说法不同，而使用者会以为
   *  它们是两件事。
   *
   *  条目按"大多数人第一件想做的事"排——这个顺序现在直接就是目录里的阅读顺序，
   *  所以「哪几件最常用」这件事仍然由它承担。
   *  ⚠️ 每条的 `tool` 必须是 `chat.HANDLERS` 里真的有的名字：写错一个不会报错，
   *  它只会被静静过滤掉，而症状是那张卡退回「用「<名称>」」的骨架——看起来也像
   *  一种设计。`tests/test_ui_contract.py` 里有一条专门钉这个。 */
  const PHRASINGS = [
    { tool: 'start_scan', text: '扫描 /srv/app，目标是审计文件上传逻辑' },
    { tool: 'start_probe', text: '对目标做一次非侵入式探测，先看端口与服务指纹' },
    { tool: 'get_findings', text: '列出最近一次扫描发现的漏洞' },
    { tool: 'list_executors', text: '现在有哪些执行节点在线？' },
    { tool: 'get_report', text: '给我最近一次测试的报告要点' },
    { tool: 'get_chain_overview', text: '把当前证据链路整体过一遍，讲讲结论' },
    { tool: 'list_jobs', text: '现在有哪些任务在跑？' },
    { tool: 'get_assets', text: '这个目标有哪些资产和端点？' },
    { tool: 'list_engagements', text: '列出全部目标，以及各自的任务与漏洞计数' },
    { tool: 'get_authz', text: '当前的侵入授权状态是什么？' },
    { tool: 'plan_tasks', text: '帮我把这次作业拆成几个任务' },
    { tool: 'start_exploit', text: '对已确认的漏洞做一次应用层利用验证' },
    { tool: 'load_skill', text: '用 recon-pipeline 这份手册铺开目标的资产面' },
    { tool: 'list_skills', text: '有哪些技能手册可以用？' },
    { tool: 'list_toolkit', text: '本机装了哪些工具可以用？' },
  ];

  const view = {
    sessions: [], sessionId: null, messages: [], tools: [], model: null,
    hasModel: false, loaded: false, sending: false, bound: false,
    // 服务端只送回**最近** 400 条。这里记"更早还有多少条没送回来"，记录
    // 顶部据此说一句——不说的话，被切掉的那几百条看起来像从未存在过。
    earlierCount: 0,
    // 「加载更早」那次请求还在飞（按钮要禁用，防止点两下拿回同一页）。
    earlierLoading: false,
    // 使用者**自己**动过记录区（滚轮 / 触摸 / 翻页键）。"滚到顶上自动取一页"
    // 只在这之后才允许：`renderLog` 一画完就贴到底，若记录比视口还短，
    // `scrollTop` 也是 0——那一帧就满足"在顶上"，自动取会把"打开会话"变成
    // 连着往下翻好几页，而使用者一下都没动过。程序化滚动（贴底、插页后的
    // 补偿）不置位，它们都由代码发起。
    logTouched: false,
    inflight: null, sessionsInflight: null,
    // 上一轮结束时思考块是不是**展开着**的。收尾要把它交给记录里那一条（见 finish
    // 与 renderLog）：不接力的话，正在读思考的人会在答案出现的一瞬间被折回去。
    carryReasoningOpen: false,
    // 「对话列表这一次请求失败了」的**错误对象本身**（没有失败就是 null）。
    // 与 `loaded` 分开是因为它们是两种不同的未知：`loaded:false` 是"还没拉到"，
    // 而失败之后 `loaded` 可能仍是 true（上一次成功过），此时已知的那份列表还在。
    // 两者的界面动作完全不同——一个该等，一个该说清是哪一次请求失败了。
    sessionsError: null,
    // 页面**手上这份会话列表**是在哪个指纹上拉下来的（见 `syncFromPulse`）。与
    // `stamps` 分开：那个记的是"服务端上一轮说了什么"，这个记的是"我手上这份有多新"。
    // `undefined` = 不清楚（刚拉过一份）→ 下一轮探询补拉一次并记上；**拉失败时清回去**，
    // 于是下一轮会再试一次，而不是把一份旧列表说成新的。
    sessionsStamp: undefined,
    // 上一次把页面范围跟到哪条对话的哪个目标（`adoptSessionEngagement`）。`null` =
    // 还没跟过。记的是 `(sid, owner)` 这一**对**：只记 sid 的话，一条对话在第一次
    // 产出型调用之后才被绑上目标（`engagement_id` 从空变有），那次绑定就漏跟；
    // 只记 owner 的话，两条对话挂到同一个目标时切过去会漏跟。
    scopeSync: null,
    // 换目标时被"一轮还在跑"挡住的那次对话切换（`alignSessionToGoal`）：值是目标 id，
    // 由 `finish()` 收尾补上。空串 = 没有待办。**不能省掉这一格**：硬切会让正在跑的
    // 那一轮把正文逐条写进另一个目标的记录里。
    pendingGoalSwitch: '',
    // 使用者**在这台页面上**打开过对话（含按「新对话」清空）。用来区分"还没打开过"
    // 与"他刚清空"——后者不该被 `autoOpenFirstSession` 用第一条顶掉。
    everOpened: false,
    // 目标列表（侧边栏第二块，常驻栏底）。**数据由 app.js 持有**（`PentestUI.state.engagementList`）
    // ——目标同时决定漏洞/资产/报告的范围，两份快照必然会在新目标出现时显示得不一样。
    goals: [],
    // —— 四块联动（m03903）。下面的字段都只属于侧栏这一块，不进 `state`。
    // 「任务」与「子智能体」两块**只列这条对话的**（m14866）。范围没有第二档，
    // 也没有手工开关：它由主区开着哪条对话唯一决定。原先这里还有一个
    // `sideJobsScope` 三档闸门（m15165）与两层目标级派活缓存（m03903），
    // 现在都不需要了——范围收成一档之后，"这一屏是哪一批"不再是个需要回答的问题。
    // 使用者是否**亲手**换过范围（点目标行 / 对话组头 / 任务行）。置位之后，
    // 后台的会话列表刷新不再把 `state.eid` 拽回打开着的那条对话的目标；
    // 点开一条对话（`openSession`）即清零，并把 `scopeSync` 一并清掉。
    // **换范围本身现在会把打开着的对话一起换过去**（`alignSessionToGoal`），所以这个
    // 标记只剩"心跳别夺回"和"那一轮还在跑、切换被挂起"两个用途。
    manualGoal: false,
    // 会话入口三项。`config` 是服务端算出的**实际生效**值，`pinned` 表示这一项
    // 在本会话里被显式选过，而不是跟随全局默认。
    options: null, config: null,
    // 新建会话时三项下拉的**起点**：服务端在 `/session-options` 里给的 `draft`，
    // 也就是"这个人上一次的选择"（已逐项核过预设/模型/权限档还在不在）。
    // 单独一个字段而不是拿 `config` 乘就：`config` 是"这个会话自己的选择"，
    // 两者混用会让界面在切回新会话时分不清"没有选择"与"选择了默认"。
    draft: null,
    // 两个技能字段**形状不同**，改版前共用 `skills` 一个名字：`loadOptions()` 往里
    // 塞角色统计（`[{id,count}]`），`loadSkills()` 往里塞手册条目（`[{name,role,…}]`）。
    // 谁最后跑谁说了算——而 `renderSkills()` 只读 `options.skills.roles`，所以这个
    // 错位一直没露出来（症状会是"技能清单在某些路径下变成只有几个名字"）。分开命名。
    skillList: [], skillRoles: [],
    // `/skills` 回来过没有。**必须与"列表为空"分开**：本机一份技能手册都没有是
    // 正常状态（全新安装就是这样），那时界面上写着"加载中…"永远不结束——一句
    // 一直挂着的谎，而且它不告诉使用者怎么才能有。
    skillLoaded: false,
    // 审查单（授权审查），按 id 存。**刻意不放在 `live` 里** —— 这是这一版改的
    // 关键：卡片原来挂在那一轮的实时气泡上，而 `live` 是纯客户端状态，刷新一次
    // 就没了，可是单子还在服务端等着人批、等它的那一轮也还在跑。放在视图级之后，
    // 它由 `loadReviews()` 每次载入会话从服务端取回，刷新不再丢东西。
    // 两个都留着：`reviewOrder` 管顺序（对象自己不记插入序），`reviews` 管按 id
    // 覆盖（同一张单子会来两帧 `pending` / `settled`，各画一块就会出现两张卡片，
    // 其中一张永远停在"等待批准"）。
    reviews: {}, reviewOrder: [],
    // 浮层里**上一次画出来的是哪一批单子**（`id+pending` 串起来的指纹）。
    // 只用来决定“要不要重新定位滚动位置”：心跳每 3 秒重画一次，无条件归位会把
    // 正在层内往回翻的人反复拽回末尾；而批次没变时，位置本来就该待在原处。
    reviewsKey: '',
    // 流式的实时视图与中断控制器。`live` 只用于"边跑边看"，**不是**消息真相：
    // 每轮结束都以服务端落库的那份为准，否则刷新一次就会看到两个版本的回答。
    live: null, abort: null,
    // **每个会话自己的那一轮**（见 `runOf` / `parkRun` / `adoptRun`）。
    // 上面那三个字段只是"**正看着的**那个会话的那一轮"的投影，不是全部：
    // 流跑在浏览器里，切到别的对话去看**不会**让它停下来，它继续往自己那一份
    // `live` 里写；切回来时把那一份搬回屏上，字一个都不少。
    // 键是 `sessionId`；还没拿到 id 的新会话用空串占位，`start` 帧落地时换键。
    runs: {},
    // 排队任务（这一轮还在跑时又排下的那几条）。**服务端是唯一真相**：这里存的
    // 是它最近一次给的那份列表；`queueEditing` 只是"哪一行正在就地编辑"这一个
    // 纯界面状态（不落服务端——编辑到一半刷新页面，回来的该是原句，不是半句话）。
    // `queueActive` 是服务端说的"这个会话有一轮在跑"，与 `sending`（这一页正在
    // 看那一条流）分开：断开之后服务端还在跑，那时插话仍然能送进去。
    queue: [], queueEditing: '', queueActive: false,
    // 输入辅助坞当前显示哪一格：`none` / `tools` / `skills`。
    // **一个枚举**而不是 `toolsOpen`/`skillsOpen` 两个布尔：两个布尔有 4 种组合而
    // 只有 3 种合法，"两个都开"得靠"后点的关掉先点的"来维持——状态错乱正来自
    // 这种约定。枚举让非法组合根本无法表示。
    // 默认 `none`（收起）：这一页唯一的操作目标是输入框，一进来就在它上面压着
    // 二十多张卡片，等于把目标推下去。点一颗按钮展开、再点同一颗收起。
    dock: 'none',
    // 工作台心跳带回来的指纹里**属于本模块的那两块**（sessions / messages）上一次
    // 的值。空对象 = 还没收到过，此时不做任何"变了"的判断（第一轮只建立基线）。
    stamps: {},
    // —— 详情（就地展开）。左栏的「目标」「对话记录」与右栏的「任务」「子智能体」点开
    // 之后看的就是它（任务与子智能体这两块本身就住在这一栏里，在上面）。
    // `detail` 是**唯一**的选中态（kind + id），行上的 `is-picked` 是它渲染
    // 出来的结果、不是第二份状态；`detailOpen` 只管这一栏拉没拉开。
    // `detailSig` 是上一次渲染用的签名：轮询拿到新数据时先比签名，变了才重画——
    // 每一拍都重画会把读的人正在看的位置拽回去。
    detail: { kind: '', id: '', row: null }, detailOpen: false, detailSig: '',
    // 默认拉开**只做一次**。`detailOpen` 本身不落存储：收起之后刷新页面会重新
    // 拉开，这是有意的——“右边那一栏”是这一页的常驻部件，不是临时浮层。
    detailAutoOpened: false,
    // 任务执行过程的轮询器。与「作业」页签那条 SSE 是同一份数据的两种读法，
    // 这里走 `/jobs/{id}/events?after=lastId` 增量拉：一次最多 300 行，拉完还有
    // 就接着拉。2 秒一拍是折中——更密地拉只是多一份轮询负载，人眼读不出差别。
    detailLog: null,
    // 排队任务（服务端内存队列的本地投影）。**真相在服务端**：`queue` 只是最近
    // 一次列表请求给的那份，所以每次增删改都拿响应里的 `items` 直接覆盖，不在
    // 本地拼。`queueEditing` 是唯一一个纯 UI 状态（"哪一行正在就地编辑"）：
    // 编辑到一半刷新页面，该回来的是原句，不是半句话——所以它不落服务端。
    queue: [], queueEditing: '', queueActive: false,
  };

  let dom = {};
  /* 自动执行的运行状态。**刻意与 `view` 分开**：`view` 里的东西都是"服务端事实的
     本地投影"，而这个只是一个浏览器里的定时器与计数（开关不落库，刷新回默认开）。
     混进去会让 `onSignedOut` / `openSession` 那些"换一个人、换一个会话就重置"的
     路径顺手清掉它——而失败恢复恰恰要跨过一次 `openSession`（`finish()` 会重取
     记录）。`failed` 记"这一轮是真的失败了"，供 `finish()` 判断要不要清零计数。 */
  const auto = {
    on: true, attempts: 0, failed: false,
    timer: null, ticker: null, due: 0,
  };
  const ui = () => window.PentestUI;
  const byId = (id) => document.getElementById(id);
  const el = (tag, className, text) => window.PentestUI.el(tag, className, text);
  const clear = (node) => window.PentestUI.clear(node);
  const asArray = (value) => window.PentestUI.asArray(value);

  /** 输入框跟着内容长高（t78）：一行起，长到上限之后改成框内滚动。
   *
   *  它**必须是模块级的**（改版前是 `cacheAndBind()` 里的一个局部 const）：发送、
   *  附加成任务这两条路径都会**用程序改值**清空输入框，而 `fitInput` 只挂在
   *  `input` 事件上——程序改值不触发那个事件，框就会停在刚才那个高度，空着一个
   *  大盒子。两处各写一遍高度逻辑必然有一份先漂移（上限是官方
   *  `--dsh-composer-text-max-height: 336px`）。
   */
  const INPUT_MAX_HEIGHT = 336;
  function fitInput() {
    if (!dom.input) return;
    dom.input.style.height = 'auto';
    const need = dom.input.scrollHeight;
    dom.input.style.height = Math.min(INPUT_MAX_HEIGHT, need) + 'px';
    dom.input.style.overflowY = need > INPUT_MAX_HEIGHT ? 'auto' : 'hidden';
  }

  /** 一次流式读取被打断的来由；没被打断返回空串。
   *
   *  **为什么不能只看 `error.name`**：`AbortController.abort(reason)` 会把 reason
   *  原样当成 `fetch` 的拒绝值——不是 `DOMException`。于是 `error.name` 与
   *  `error.message` **都是 undefined**，界面就会打出「对话请求失败：undefined」。
   *  这个错误在这里被真实复现过（探针：点一次发送、再点停止 → 错误条
   *  「对话请求失败：undefined」），所以判断依据换成**信号自己的 reason**，
   *  `error` 只当兜底。
   */
  function interruptionOf(error, signal) {
    const reason = (signal && signal.aborted) ? signal.reason : error;
    if (reason === 'user' || reason === 'idle' || reason === 'signout') return reason;
    if (reason && reason.name === 'AbortError') return 'abort';
    return '';
  }

  /** 把任何拒绝值变成一句能读的话。`undefined`/`null` 绝不允许冒到界面上。 */
  function describeError(error) {
    if (!error) return '没有带回任何原因';
    if (typeof error === 'string') return error;
    if (error.message) return String(error.message);
    if (error.error) return String(error.error);
    try {
      const text = JSON.stringify(error);
      if (text && text !== '{}') return text;
    } catch (ignored) { /* 循环引用之类：下面那行兜底 */ }
    return String(error);
  }

  function cacheDom() {
    dom = {
      tab: document.querySelector('.tab[data-tab="dsh"]'),
      meta: byId('chat-meta'), refresh: byId('chat-refresh'),
      newButton: byId('chat-new'), toolsButton: byId('chat-tools'),
      sessions: byId('chat-sessions'), log: byId('chat-log'),
      goals: byId('chat-goals'),
      // 审查卡画在**对话记录里**（t80 起，和 DSH 桌面端一样），所以这里不再有
      // `reviewSlot` —— 那一格（`#chat-review-slot`）已经从 index.html 撤掉，
      // 再取它只会得到 null，而 `renderReviews` 每次都会因为它提前返回。
      sessionsHint: byId('chat-sessions-hint'),
      // 上下文栏后两块。它们的**头**在 index.html 里（折叠状态与 aria 挂在头上，
      // 跟着重画会丢），这里只取正文容器与头里那个读数位。
      sideJobs: byId('chat-side-job-list'), sideJobsHint: byId('chat-side-jobs-hint'),
      sideSubagents: byId('chat-side-subagent-list'),
      sideSubagentsHint: byId('chat-side-subagents-hint'),
      // 右侧栏的**外壳**。栏头两个读数与关按钮是静态节点，重画只改它们的
      // textContent；详情本身不在这里——它展开在被点中那一行下面（见 `mountInline`）。
      detail: byId('chat-detail'),
      detailTitle: byId('chat-detail-title'), detailKind: byId('chat-detail-kind'),
      detailClose: byId('chat-detail-close'),
      // 分栏线不是 id，只能用选择器找。收起时它也藏（`is-folded` 是 split.js 与
      // fold.js 共用的"没有可拖的东西"），否则栏没了、线还在，那条 1px 竖线看着
      // 就像"这里本来有东西、只是没画出来"。
      detailHandle: document.querySelector('.split-handle[data-split="detail"]'),
      quick: byId('chat-quick'), input: byId('chat-input'),
      // 审查卡那一层（m18801：浮在卡片上沿，见 index.html 那条注释）。它是**常驻
      // 节点**，空的时候靠 `hidden` 收起来 —— 不能每次现造：现造的那一块只有
      // 自己认识，"上一次画的那一块"就没有任何人收得掉。
      reviewFloat: byId('chat-review-float'),
      // 动作按钮只有一颗（发送 / 停止是它的两种身份，见 setSending）：
      // 这里**不许**再出现 `byId('chat-stop')` —— 那个 id 已经不存在，取到的是
      // null，而后面每一处 `dom.stop.X` 都会抛，抛在渲染路径上就是"这一块不见了"。
      send: byId('chat-send'), model: byId('chat-model'),
      agent: byId('chat-agent'), modelSelect: byId('chat-model-select'),
      policy: byId('chat-policy'), entryHint: byId('chat-entry-hint'),
      // 权限 chip 与它的菜单（原生 `dom.policy` 仍是真相源，见 index.html 那段注释）。
      policyBtn: byId('chat-policy-btn'), policyName: byId('chat-policy-name'),
      policyMenu: byId('chat-policy-menu'),
      // 模型 chip 与它的菜单：与权限那颗同一套（原生 `dom.modelSelect` 仍是真相源）。
      modelBtn: byId('chat-model-btn'), modelName: byId('chat-model-name'),
      modelMenu: byId('chat-model-menu'),
      // 自动执行：一行开关 + 卡片上方的倒计时行。
      auto: byId('chat-auto'), autoNote: byId('chat-auto-note'),
      autoText: byId('chat-auto-text'), autoCancel: byId('chat-auto-cancel'),
      // 排队任务（卡片上方、输入框前面那一块）。整块靠 `hidden` 收起：没有排队时
      // 它连边距都不占，卡片高度与记录区叠进深度都不动。
      queue: byId('chat-queue'), queueList: byId('chat-queue-list'),
      queueTitle: byId('chat-queue-title'), queueHint: byId('chat-queue-hint'),
      skillsButton: byId('chat-skills'),
      toolsCount: byId('chat-tools-count'), skillsCount: byId('chat-skills-count'),
      // 选项行：模型的 ```options 块变成的一排按钮（见 renderChoice）。它在卡片**里面**
      // 的第一行，不是卡片上方单独的一格——"选项"与"回答"是同一件事的两半。
      choice: byId('chat-choice'),
    };
  }

  /* ------------------------------------------------------ 会话入口三项选择 ---- */

  /** 用一个对象填充 select。`items` = [{ id, label, note? }]。 */
  function fillSelect(node, items, selected, emptyLabel) {
    clear(node);
    if (emptyLabel) {
      const option = el('option', null, emptyLabel);
      option.value = '';
      node.append(option);
    }
    items.forEach((item) => {
      const option = el('option', null, item.label + (item.note ? ' · ' + item.note : ''));
      option.value = String(item.id);
      // t100：模型那颗 chip 的菜单把选项当数据源（`<option>` 里放不进 DOM，菜单又是
      // 自绘的），于是把结构原样挂在节点上，省得回头去拆拼好的那句文字——
      // 「跟随全局启用项（DeepSeek · deepseek-flash）」里也有一个 `' · '`，
      // 按分隔符拆会把占位符拆成两截。这三项谁先到都一样，挂着没有副作用。
      option.itemLabel = item.label;
      option.itemNote = item.note || '';
      node.append(option);
    });
    node.value = selected === undefined || selected === null ? '' : String(selected);
    // 选中值不在选项里（例如插件被卸载、模型被删掉）时浏览器会静默落到第一项，
    // 而那与"用户选的是它"看起来完全一样。这里显式补一个占位项并标注，
    // 让"这一档已经没了"在界面上看得见。
    if (node.value !== String(selected === undefined || selected === null ? '' : selected)) {
      const stale = el('option', null, '（已失效：' + selected + '）');
      stale.value = String(selected);
      node.prepend(stale);
      node.value = String(selected);
    }
    return node.value;
  }

  /* ------------------------------------------------------ 模型格的宽度 ----
   *
   * 模型名是变长的，而 `<select>` 的宽带默认取**最长那个选项**：选着
   * 「DeepSeek · deepseek-flash」的时候，框里会空出一截——最长的那一项往往是
   * 「跟随全局启用项（DeepSeek · deepseek-flash）」。这就是"框比字宽"的真正来源：
   * 不是内边距给大了，是框按别的字量了宽。
   *
   * t100 之前这里靠两层：CSS 的 `field-sizing: content`（认它的浏览器按选中项算宽）
   * + 这里量一遍文字写成 `--model-w`。那一格现在是**自绘的 chip**（`<button>`，原生
   * select 退成 1px 隐形层），`field-sizing` 对它没有意义——`<button>` 的固宽行为本就
   * 是"取内容"，所以只剩这里量这一层：文字 + 左右内边距 + 箭头 + 两者之间的缝。
   *
   * 量出来的仍是 `--model-w`、仍写在这个 chip 上（CSS 拿它当 width 的首选，外层
   * `.chat-model` 那 240px 才是不许越过的封顶）——"框比字宽"和"静默少一个字"这两笔
   * 旧账与量法都不变，只是被量的节点换了。
   *
   * 字体必须取控件自己的 computed font：拿 `--sans` 的默认值猜，长名字会差几个像素，
   * 偏小就又把最后一个字切掉了。canvas 只建一次——宽度测量不该每画一次造一个上下文。
   */
  const measureCanvas = document.createElement('canvas');
  function textWidth(node, text) {
    const style = window.getComputedStyle(node);
    const ctx = measureCanvas.getContext('2d');
    if (!ctx) return 0;
    ctx.font = [style.fontStyle, style.fontWeight, style.fontSize, style.fontFamily]
      .filter(Boolean).join(' ');
    return ctx.measureText(text).width;
  }

  /** 原生 `#chat-model-select` 的当前值 → chip 上的字。
   *
   *  字取的是**选中那一项的 text**（`fillSelect` 拼的「名字 · 型号」/ 占位符），
   *  与原生控件此前显示的完全同一句——chip 只是把它挪到看得见的那一层上。
   */
  function syncModelChip() {
    const select = dom.modelSelect;
    const name = dom.modelName;
    if (!select || !name) return;
    const option = select.options[select.selectedIndex];
    const text = option ? option.text : '';
    name.textContent = text;
    if (dom.modelBtn) {
      // ≤392 那一档里文字是 `display: none`：名字必须另有出处（读屏 + 悬停）。
      dom.modelBtn.setAttribute('aria-label', '模型选择，当前：' + (text || '未选择'));
      dom.modelBtn.title = text || '这一轮用哪个模型';
    }
  }

  /** 让模型格**只比自己那行字宽一点**：文字 + 左右内边距 + 箭头 + 两者之间的缝。
   *
   *  t100 量的对象从 `<select>` 换成了 chip：原生控件那套宽度机制（`field-sizing`
   *  与"intrinsic 取最长选项"）已经不在这一格上生效，但"框比字宽"和"静默少一个
   *  字"这两笔旧账还在，所以仍然是"量出来的 width 为主、封顶靠外层"那一套。
   */
  function syncModelWidth() {
    const btn = dom.modelBtn;
    if (!btn) return;
    const text = dom.modelName ? dom.modelName.textContent : '';
    if (!text) {
      btn.style.removeProperty('--model-w');
      return;
    }
    const style = window.getComputedStyle(btn);
    const pad = (parseFloat(style.paddingLeft) || 0)
      + (parseFloat(style.paddingRight) || 0);
    // 箭头（含与文字之间那道缝）也要算进去：漏掉它，末字会被挤出可读区。
    // 只剩图标那一档箭头是 `display: none`，那时它不该再占宽。
    const chevNode = btn.querySelector('.chat-model-chev');
    const chev = chevNode && window.getComputedStyle(chevNode).display !== 'none'
      ? chevNode.offsetWidth + (parseFloat(style.columnGap) || 0) : 0;
    // +1 是亚像素余量：少这一像素，末字会被 `text-overflow: ellipsis` 切出一个「…」，
    // 看起来像名字被截短了——而那正是这一轮要消掉的观感。
    btn.style.setProperty('--model-w',
      Math.ceil(textWidth(btn, text) + pad + chev) + 1 + 'px');
  }

  async function loadOptions() {
    try {
      const payload = await ui().apiGet('/session-options');
      view.options = payload;
      view.draft = payload.draft || null;
      view.skillRoles = asArray(payload.skills && payload.skills.roles);
    } catch (error) {
      view.options = null;
      view.draft = null;
      ui().showError('会话选项加载失败：' + error.message);
    }
    renderEntry();
    renderDock();
    refreshComposer();
  }

  // 来源标签按**服务端给的分类**取，不拿 item.builtin 去猜：那个布尔只有两位，
  // 分不出"插件"和"自定义"，而自定义预设正是这一版新加进来的东西——猜的结果是把
  // 用户自己写的预设标成"插件"，他会去插件市场里找一个不存在的包。
  const AGENT_KIND_TAGS = { builtin: '内置', custom: '自定义', plugin: '插件' };

  /** 统计标签重名次数。标签**不设**唯一约束——"两个同名预设"不是错误输入，是用户的
   *  选择；但下拉框里两条长得一模一样时，选错的那一下决定的是**整轮作业口径**，而
   *  用户没有别的线索可以对照。所以只在真的重名时才补 id，常见情形保持干净。 */
  function countLabels(items) {
    const seen = {};
    asArray(items).forEach((item) => { seen[item.label] = (seen[item.label] || 0) + 1; });
    return seen;
  }

  function agentNote(item, dupeLabels) {
    const tag = AGENT_KIND_TAGS[item.kind] || item.sourceLabel || '';
    if (dupeLabels[item.label] > 1) return (tag ? tag + ' · ' : '') + item.id;
    return tag;
  }

  /** 三项下拉的渲染。以**服务端算出的 config** 为准回填，不以本地意图为准。 */
  function renderEntry() {
    if (!dom.agent || !view.options) return;
    const options = view.options;
    // 还没建会话时显示的是**上次的选择**（服务端 `draft`）：新建会话沿用它，
    // 而不是每次都弹回"跟随默认"——那正是这一版要改掉的行为。
    const config = view.config || view.draft || {};
    const pinnedAgent = config.agentPinned ? (config.agentId || '') : '';
    const pinnedPolicy = config.policyPinned ? (config.policyId || '') : '';
    const pinnedModel = config.modelId || '';

    const dupeLabels = countLabels(options.agents);

    fillSelect(dom.agent,
      asArray(options.agents).map((item) => ({
        id: item.id, label: item.label,
        note: agentNote(item, dupeLabels),
      })),
      pinnedAgent, '跟随默认（' + ((options.defaults || {}).agentId || '') + '）');

    // 模型这项的"跟随默认"要**说出跟的是哪一个**：Agent 与权限两项的占位符早就
    // 这么做了（「跟随默认（pentest）」），只有模型这项落在后面——于是选了"跟随
    // 全局启用项"之后，这一行里没有任何地方写着真正会跑的是哪一个模型。
    // 素材来自 `/models`（`view.model` 是全局启用项）。它和 `/session-options`
    // 谁先回来不确定，所以 `loadModelState()` 完成时会回来重画一次——少了那一句，
    // 先到的是 /session-options 时占位符会停在没有名字的版本上，而此后没有任何
    // 事件会去修它。
    const globalModel = view.model ? (view.model.name || view.model.id) : '';

    fillSelect(dom.modelSelect,
      asArray(options.models).map((item) => ({
        id: item.id, label: item.name || item.id, note: item.model,
      })),
      pinnedModel,
      globalModel ? '跟随全局启用项（' + globalModel + '）' : '跟随全局启用项');
    // 选项换了一批、选中项可能也换了：chip 上的字与宽度都跟着重算
    // （字先换，宽度才有依据）。
    syncModelChip();
    syncModelWidth();

    fillSelect(dom.policy,
      asArray(options.policies).map((item) => ({
        id: item.id, label: item.label, note: item.toolCount + ' 项能力',
      })),
      pinnedPolicy, '跟随默认（' + ((options.defaults || {}).policyId || '') + '）');
    // chip 上的字跟着**当前值**走（含"跟随默认"那种空值），而不是跟着 `config`。
    syncPolicyChip();

    // 入口那几项都对齐了，开关也跟着服务端走：它是同一个 config 里的一项。
    syncAutoFromConfig();
    renderEntryHint();
  }

  function renderEntryHint() {
    if (!dom.entryHint) return;
    const config = view.config || view.draft || {};
    // 三项的来源要说清：`draft` 的那个选择还没落到**这个**会话上，发第一条消息
    // 才生效。都叫"本会话已选"的话，使用者会以为已经定下来了。
    const inherited = !view.config && !!view.draft;
    const parts = [];
    if (config.agentLabel) {
      parts.push('本轮 Agent：' + config.agentLabel
        + (config.agentPinned ? (inherited ? '（沿用上次选择）' : '（本会话已选）')
          : '（跟随默认）'));
    } else if (view.options) {
      parts.push('本轮 Agent：跟随默认');
    }
    if (config.policyId) {
      const policy = asArray(view.options && view.options.policies)
        .filter((item) => item.id === config.policyId)[0];
      parts.push('权限：' + ((policy && policy.label) || config.policyId)
        + (config.policyPinned ? (inherited ? '（沿用上次选择）' : '（本会话已选）')
          : '（跟随默认）'));
    }
    if (view.options && view.options.skills) {
      parts.push('技能 ' + (view.options.skills.count || 0) + ' 份');
    }
    dom.entryHint.textContent = parts.length ? parts.join(' · ') : '';
    dom.entryHint.title = parts.join('\n');
  }

  /* ------------------------------------------------- 权限 chip 与它的菜单 ----
   *
   * 形态照 DSH 桌面端：输入框里一颗 28px 的 chip（盾牌 + 档名 + 上箭头），点开是
   * 向上弹的菜单，四档一屏选完。**逻辑一点没换**：原生 `#chat-policy` 仍是唯一
   * 真相源，`fillSelect` / `commitEntry` / 发送时带 `policyId` 全都照旧；chip 只
   * 负责"把 select 的当前值说给眼睛看"和"把点击翻译成一次 select 变更"。
   *
   * 为什么不重写一套状态：权限档早就不是纯 UI —— 已有会话上它会被 `commitEntry`
   * 落库，新会话则随第一条消息带上（服务端在 `start` 帧里回 `config`）。另起一套
   * 状态机等于让"界面上显示的档位"和"服务端记着的档位"各有各的来源，而它们漂开
   * 的时候，症状是"模型说它不能执行，而界面写着完全作业"。
   */

  function policyList() { return asArray(view.options && view.options.policies); }

  function policyItem(id) {
    return policyList().filter((item) => item.id === id)[0] || null;
  }

  /** `#chat-policy` 的当前值 → chip 上的字。空值（"跟随默认"）要说**跟的是哪一档**，
   *  否则 chip 只写着"权限"三个字，而那一行里没有任何地方告诉你这一轮到底能做什么。 */
  function syncPolicyChip() {
    if (!dom.policyBtn || !dom.policyName) return;
    const chosen = (dom.policy && dom.policy.value) || '';
    const fallback = (view.options && view.options.defaults
      && view.options.defaults.policyId) || '';
    const item = policyItem(chosen) || policyItem(fallback);
    const label = (item && item.label) || '权限';
    const suffix = (!chosen && item) ? '（默认）' : '';
    dom.policyName.textContent = label + suffix;
    dom.policyBtn.setAttribute('aria-label', '权限档，当前：' + label + suffix);
    dom.policyBtn.title = (item && (item.summary || item.note))
      || '权限档：这一轮的智能体最多能做哪些事';
  }

  function hidePolicyMenu(restoreFocus) {
    if (!dom.policyMenu) return;
    dom.policyMenu.hidden = true;
    clear(dom.policyMenu);
    if (dom.policyBtn) {
      dom.policyBtn.setAttribute('aria-expanded', 'false');
      if (restoreFocus) dom.policyBtn.focus();
    }
  }

  function togglePolicyMenu() {
    if (!dom.policyMenu) return;
    if (!dom.policyMenu.hidden) { hidePolicyMenu(true); return; }
    renderPolicyMenu();
    dom.policyMenu.hidden = false;
    dom.policyBtn.setAttribute('aria-expanded', 'true');
    const first = dom.policyMenu.querySelector('button');
    if (first) first.focus();
  }

  function renderPolicyMenu() {
    const menu = dom.policyMenu;
    clear(menu);
    const list = policyList();
    if (!list.length) {
      menu.append(el('p', 'chat-policy-head', '权限档还没取回来（会话选项未加载）'));
      return;
    }
    const chosen = (dom.policy && dom.policy.value) || '';
    const fallback = (view.options.defaults && view.options.defaults.policyId) || '';
    const effectiveId = chosen || fallback;
    menu.append(el('p', 'chat-policy-head', '这一轮最多能做哪些事'));
    list.forEach((item) => {
      const button = el('button', 'chat-policy-item');
      button.type = 'button';
      button.setAttribute('role', 'menuitemradio');
      button.setAttribute('aria-checked', item.id === effectiveId ? 'true' : 'false');
      const main = el('span', 'chat-policy-item-main');
      const label = el('span', 'chat-policy-item-label', item.label);
      if (item.id === 'full') label.append(el('span', 'chat-policy-item-badge', '高危'));
      main.append(label);
      // 菜单里那句短注取 `note`（"适合…"），`summary` 是完整定义、放进 title：
      // 菜单是"选哪一档"，不是读文档的地方，一行说得完就够。
      if (item.note) main.append(el('span', 'chat-policy-item-note', item.note));
      button.append(main);
      if (item.id === effectiveId) {
        button.append(el('span', 'chat-policy-item-check', '✓'));
      } else if (item.id === fallback) {
        button.append(el('span', 'chat-policy-item-default', '默认'));
      }
      button.title = item.summary || '';
      button.addEventListener('click', () => choosePolicy(item.id));
      menu.append(button);
    });
    menu.append(el('p', 'chat-policy-foot',
      '改动立即生效，并记在本会话上（与 Agent、模型两项一致）。'));
  }

  /** 选一档。**只有切到「完全作业」才多问一句**——照官方对 full-access 的做法：
   *  它放行的是应用层利用验证、凭据测试与任意命令；而门禁与审查闸**不因此放宽**，
   *  这句必须写在确认里，否则会被读成"点了就解锁一切"。 */
  function choosePolicy(id) {
    if (!dom.policy) return;
    if (id === (dom.policy.value || '')) { hidePolicyMenu(true); return; }
    if (id === 'full') { renderPolicyConfirm('full'); return; }
    applyPolicy(id);
  }

  function applyPolicy(id) {
    dom.policy.value = id;
    /* 走**原来的 change 处理**：已有会话立刻 `commitEntry(false)` 落库，新会话只
       更新提示、等第一条消息带走。chip 自己不改 `view.config` —— 真正的生效值由
       服务端回，`commitEntry` 之后会 `renderEntry()` 把 chip 重新对齐一次。 */
    dom.policy.dispatchEvent(new Event('change', { bubbles: true }));
    syncPolicyChip();
    hidePolicyMenu(true);
  }

  function renderPolicyConfirm(id) {
    const menu = dom.policyMenu;
    clear(menu);
    const item = policyItem(id) || { label: '完全作业' };
    const box = el('div', 'chat-policy-confirm');
    box.append(el('p', 'chat-policy-confirm-title',
      '确认启用' + item.label + '？'));
    box.append(el('p', 'chat-policy-confirm-text',
      '这一轮的智能体可以直接下发应用层利用验证、凭据测试与任意命令。'
      + '它不会放宽探测作用域门禁与授权审查闸——那两道闸照旧，'
      + '越界目标仍会被拒绝。仅建议在你信任当前任务时使用。'));
    const check = el('label', 'chat-policy-check');
    const tick = el('input');
    tick.type = 'checkbox';
    check.append(tick, el('span', '', '我已了解风险，并愿意继续'));
    box.append(check);
    const actions = el('div', 'chat-policy-confirm-actions');
    const cancel = el('button', 'btn', '取消');
    const confirm = el('button', 'btn chat-review-primary', '启用' + item.label);
    cancel.type = 'button';
    confirm.type = 'button';
    confirm.disabled = true;
    tick.addEventListener('change', () => { confirm.disabled = !tick.checked; });
    cancel.addEventListener('click', () => hidePolicyMenu(true));
    confirm.addEventListener('click', () => applyPolicy(id));
    actions.append(cancel, confirm);
    box.append(actions);
    menu.append(box);
    tick.focus();
  }

  /* ------------------------------------------------------ 模型 chip 的菜单 ----
   *
   * 与权限那颗同一套（菜单形状共用 style.css 里那一条分组规则）：原生 `<select>`
   * 只当真相源，菜单自己画。用户这一轮的两件事都落在这里——
   *   · 「去除蓝底」：行底色只有 `:hover` 那一档（`--panel-3`，中性面）；
   *   · 「选择的模型后面增加√」：选中那一条的行尾打勾。
   */

  /** 选项 → 菜单行。结构是 `fillSelect` 挂上去的，**不**去拆拼好的那句文字。 */
  function modelItems() {
    const select = dom.modelSelect;
    if (!select) return [];
    return Array.from(select.options).map((option) => ({
      id: option.value,
      label: option.itemLabel || option.text,
      note: option.itemNote || '',
    }));
  }

  function hideModelMenu(restoreFocus) {
    if (!dom.modelMenu) return;
    dom.modelMenu.hidden = true;
    clear(dom.modelMenu);
    if (dom.modelBtn) {
      dom.modelBtn.setAttribute('aria-expanded', 'false');
      if (restoreFocus) dom.modelBtn.focus();
    }
  }

  function toggleModelMenu() {
    if (!dom.modelMenu) return;
    if (!dom.modelMenu.hidden) { hideModelMenu(true); return; }
    renderModelMenu();
    dom.modelMenu.hidden = false;
    dom.modelBtn.setAttribute('aria-expanded', 'true');
    const first = dom.modelMenu.querySelector('button');
    if (first) first.focus();
  }

  function renderModelMenu() {
    const menu = dom.modelMenu;
    clear(menu);
    const items = modelItems();
    if (!items.length) {
      menu.append(el('p', 'chat-model-head', '模型还没取回来（会话选项未加载）'));
      return;
    }
    const chosen = (dom.modelSelect && dom.modelSelect.value) || '';
    menu.append(el('p', 'chat-model-head', '这一轮用哪个模型'));
    items.forEach((item) => {
      const button = el('button', 'chat-model-item');
      button.type = 'button';
      button.setAttribute('role', 'menuitemradio');
      button.setAttribute('aria-checked', item.id === chosen ? 'true' : 'false');
      const main = el('span', 'chat-model-item-main');
      main.append(el('span', 'chat-model-item-label', item.label));
      if (item.note) main.append(el('span', 'chat-model-item-note', item.note));
      button.append(main);
      // 勾**只在菜单里**打：收起态的 chip 上不出现「✓」——那是"从这些里挑了这一个"
      // 的记号，收起之后只剩一个值，没有"别的"可比。
      if (item.id === chosen) button.append(el('span', 'chat-model-item-check', '✓'));
      button.addEventListener('click', () => chooseModel(item.id));
      menu.append(button);
    });
    menu.append(el('p', 'chat-model-foot',
      '改动立即生效，并记在本会话上（与 Agent、权限两项一致）。'));
  }

  /** 选一项：**改原生 select 再派发 change**，落库走原来那条路（与 `applyPolicy` 同一做法）。 */
  function chooseModel(id) {
    if (!dom.modelSelect) return;
    if (id === (dom.modelSelect.value || '')) { hideModelMenu(true); return; }
    dom.modelSelect.value = id;
    dom.modelSelect.dispatchEvent(new Event('change', { bubbles: true }));
    syncModelChip();
    hideModelMenu(true);
  }

  /* -------------------------------------------------------- 自动执行 ----
   *
   * 参考 dsh-client-auto-continue：一轮因为**非人为原因**失败（网络、超时、5xx、
   * 429、输出被截断）时，隔一小会儿自动再发一句"继续"，把人从"盯着屏幕等它自己
   * 恢复"里解放出来。这里只做它的核心四件事，不做那套统计面板与通知：
   *
   *   1. **分类**：认得出的暂时性错误才续；401/403、认证、余额/配额、模型不存在、
   *      上下文超限这类**重试也没用**的直接跳过，并且**说一句为什么没动**——
   *      静默跳过与"它根本没反应"在屏幕上长得一模一样。
   *   2. **退避**：连续失败时 3s → 20s → 40s → 80s → 120s，封顶 5 次。
   *      对端已经不舒服了还一秒一次地砸，是把一次抖动拖成一次事故。
   *   3. **可取消**：倒计时行上有「不继续」，点发送/停止也一律取消——人一动，
   *      自动的那只手就该松开。
   *   4. **幂等提示**：续跑文案里明写"先确认上一步已经执行过的命令的实际状态"，
   *      因为失败可能发生在工具**已经执行之后**（一次 git push、一次扫描下发），
   *      无脑重放会把副作用做两遍。
   *
   * 刻意**不做**"成功之后也继续"的自主循环：那是另一个开关（需要人明确想让它一直
   * 干活），混在这里会让"失败恢复"变成"停不下来的机器人"。
   */
  const AUTO_MAX = 5;
  const AUTO_DELAYS = [3000, 20000, 40000, 80000, 120000];
  /* 永久性失败：重试不会变好。`token` 只在"上限 / 超限"这类搭配里出现，单独一个
     token 可能只是普通单词（比如一次凭据测试的输出），不能一律判死。 */
  const AUTO_PERMANENT = /(401|403|认证|鉴权|凭据|密钥|API\s*Key|余额|配额|quota|模型不存在|not\s*found|上下文长度|context\s*length|max_tokens|token\s*上限)/i;

  /* **开关落在服务端，不进浏览器存储。** 真相源只有一处：`chat_sessions.auto_drive`，
     界面这颗胶囊只是它的投影（见 test_new_session_starts_from_the_last_choice——
     chat.js 里出现浏览器存储就当场红；另存一份就是第二份真相，换个人登录会带着
     上一个人的预选）。
     `auto.on` 因此在两处出生：还没有会话时是本地默认（开），一旦这个会话的
     config 回来了就由服务端那份说了算（`syncAutoFromConfig`）。 */

  /** 把会话上存的那一份同步到界面上（`config.autoDrive` 是真相）。
   *
   *  只在**已经打开一个会话**时同步：新建会话还没有 id，那时开关就是本地意图，
   *  发第一条消息时随请求体一起带走（见 `streamTurn`）。
   */
  function syncAutoFromConfig() {
    if (!view.sessionId || !view.config) return;
    const want = view.config.autoDrive === true;
    if (auto.on === want) return;
    auto.on = want;
    renderAutoSwitch();
    if (!auto.on) cancelAutoContinue();
  }

  function renderAutoSwitch() {
    if (!dom.auto) return;
    dom.auto.setAttribute('aria-pressed', auto.on ? 'true' : 'false');
    dom.auto.title = auto.on
      ? '自动执行已开：这一轮失败后自动发送「继续」，正常收尾后也会自己接着开下一轮'
        + '（点一下关掉）'
      : '自动执行已关闭：失败恢复与"收尾后接着干"都不会自己发生（点一下打开）';
  }

  function showAutoNote(text) {
    if (!dom.autoNote || !dom.autoText) return;
    dom.autoText.textContent = text;
    dom.autoNote.hidden = false;
    // 这一行本身就是「显示在输入框上面」的一条：它一出现，卡片的上沿就往上
    // 长了一截，锚点必须当场重算（观察器那条路要等下一帧，会被饿死）。
    try { syncNoticeAnchor(); } catch (ignored) { /* 位置量不出来不该让提示消失 */ }
  }

  function hideAutoNote() {
    if (!dom.autoNote || dom.autoNote.hidden) return;
    dom.autoNote.hidden = true;
    // 这一行收起来，卡片上沿落回去，通知要跟着落回来（否则会悬在半空）。
    try { syncNoticeAnchor(); } catch (ignored) { /* 同上 */ }
  }

  function clearAutoTimer() {
    if (auto.timer) window.clearTimeout(auto.timer);
    if (auto.ticker) window.clearInterval(auto.ticker);
    auto.timer = null;
    auto.ticker = null;
    auto.due = 0;
  }

  /** 人动手了（发消息 / 点停止 / 关开关 / 切会话）：把待发的续跑和倒计时一起撤掉。 */
  function cancelAutoContinue() {
    clearAutoTimer();
    hideAutoNote();
  }

  function scheduleAutoContinue(reason) {
    auto.failed = true;
    if (!auto.on) return;
    const why = String(reason || '').trim();
    if (AUTO_PERMANENT.test(why)) {
      showAutoNote('不自动执行：这次失败看起来不是暂时性的（'
        + why.slice(0, 60) + '），重试也不会变好。');
      return;
    }
    if (auto.attempts >= AUTO_MAX) {
      showAutoNote('已自动接着跑了 ' + AUTO_MAX + ' 次仍未成功，先停在这里——'
        + '请看一眼上面那条错误再决定。');
      return;
    }
    const delay = AUTO_DELAYS[Math.min(auto.attempts, AUTO_DELAYS.length - 1)];
    auto.attempts += 1;
    clearAutoTimer();
    auto.due = Date.now() + delay;
    const paint = () => {
      const left = Math.max(0, Math.ceil((auto.due - Date.now()) / 1000));
      showAutoNote('这一轮失败了：' + (why.slice(0, 60) || '未知原因') + '。'
        + (left > 0 ? left + ' 秒后自动发送「继续」…' : '正在发送「继续」…'));
    };
    paint();
    auto.ticker = window.setInterval(paint, 500);
    auto.timer = window.setTimeout(() => {
      clearAutoTimer();
      fireAutoContinue(why);
    }, delay);
  }

  function fireAutoContinue(reason) {
    if (!auto.on) return;
    // 已经有人开了一轮（比如用户自己发了）：不再插话，也不算一次尝试。
    // **但要说一句**：静默地丢掉这一次，看起来就是"自动执行没生效"。
    if (view.sending) {
      auto.attempts = Math.max(0, auto.attempts - 1);
      showAutoNote('自动执行暂停：这个会话还有一轮在跑，等它结束再试。');
      return;
    }
    // 没有可用模型时 `sendMessage` 会在入口直接返回——那一句"继续"就凭空消失了。
    // 这是最容易被误读成"功能没生效"的一种，所以在这里先拦下来并说清。
    if (!canSend()) {
      showAutoNote('自动执行暂停：现在没有可用的模型，先到「设置」页签配置并启用一条。');
      return;
    }
    hideAutoNote();
    const text = '继续'
      + (reason ? '（上一轮中断：' + reason.slice(0, 60) + '）' : '')
      + '。先确认上一步已经执行过的命令现在是什么状态，不要重复已经生效的操作。';
    sendMessage(text);
  }

  /** 把入口那几项提交到会话（或作为发送时的一次性参数带上）。
   *
   *  `autoDrive` 与三项一起走**同一个**往返：分开两次写会出现"权限存了、开关
   *  没存"，而界面上两颗控件都长得像已经生效。
   */
  async function commitEntry(force) {
    if (!view.sessionId) return view.config;
    const body = {
      agentId: (dom.agent && dom.agent.value) || '',
      modelId: (dom.modelSelect && dom.modelSelect.value) || '',
      policyId: (dom.policy && dom.policy.value) || '',
      autoDrive: auto.on === true,
    };
    try {
      const payload = await ui().apiPost(
        '/chat/sessions/' + encodeURIComponent(view.sessionId) + '/config', body);
      view.config = payload.config || view.config;
      if (force) ui().hideError();
    } catch (error) {
      ui().showError('会话选项保存失败：' + error.message);
    }
    renderEntry();
    return view.config;
  }

  /* ---------------------------------------------------------- 输入辅助坞 ---- */
  /* 三格：提示 / 可用能力 / 技能手册。三者互斥，当前在哪一格记在 `view.dock`，
   * 内容区 `#chat-quick` 把它反映到自己的 `data-dock` 上（样式表的模式块按属性
   * 选择器取，理由见 style.css：同为单类名的两条规则，谁赢只看源码顺序）。 */

  /** 把一句话填进输入框。
   *
   *  **空框直接填，已有内容则追加**。点一下示例就抹掉用户刚打了一半的那行字，
   *  是这类"辅助输入"最典型的破坏性行为——而它看起来只是"按了一下提示"。
   *  追加而不是插到光标处：光标可能停在句子中间，往那儿插会把句子劈开。
   *
   *  刻意**不自动发送**：这些说法里有扫描与利用验证，误触一下就直接下发作业。
   *  填进去、光标停末尾，发不发由人按下去。 */
  function fillInput(text) {
    if (!dom.input || dom.input.disabled || !text) return;
    const current = dom.input.value;
    dom.input.value = current.trim()
      ? current.replace(/\s+$/, '') + ' ' + text : text;
    dom.input.focus();
    // 光标放末尾：追加之后若停在插入点前面，接着打字会插进句子中间。
    const end = dom.input.value.length;
    try {
      dom.input.setSelectionRange(end, end);
    } catch (error) { /* 个别环境不支持，那就只聚焦 */ }
  }

  /** 一个可点的目录项。`fills` 是点击后**真的**会填进输入框的那句话——界面把它
   *  显示出来（`.dock-chip-fill`），于是"承诺"与"实际"只有一个来源。 */
  function dockChip(name, tag, note, fills, title) {
    const chip = el('button', 'dock-chip');
    chip.type = 'button';
    const head = el('span', 'dock-chip-name', name);
    if (tag) head.append(el('span', 'dock-chip-tag', tag));
    chip.append(head);
    if (note) chip.append(el('span', 'muted', note));
    chip.append(el('span', 'dock-chip-fill', '点一下填：「' + fills + '」'));
    // 完整说法进 title：卡片里那一行会被 CSS 按行数截断，而截断处正是使用者判断
    // "点下去会发生什么"的地方——看不全就等于没有。
    chip.title = title || fills;
    chip.addEventListener('click', () => fillInput(fills));
    return chip;
  }

  /** 工具名 → 现成的自然语言说法（见表头的 `PHRASINGS`）。 */
  function phrasingFor(name) {
    const hit = PHRASINGS.filter((item) => item.tool === name)[0];
    return hit ? hit.text : '';
  }

  function renderTools() {
    clear(dom.quick);
    if (!view.tools.length) {
      dom.quick.append(el('span', 'muted', '工具目录加载中…'));
      return;
    }
    view.tools.forEach((tool) => {
      const params = asArray(tool.parameters);
      // 点击填进去的是**自然语言**，不是工具名：模型看不到"用户点了哪张卡片"，
      // 它只看到一句话。已知工具都配一句现成说法（`PHRASINGS`），其余给
      // 「用「<名称>」」的骨架，光标停在末尾接着写参数。
      const fills = phrasingFor(tool.name) || ('用「' + (tool.label || tool.name) + '」');
      dom.quick.append(dockChip(tool.label || tool.name, tool.level || '',
        params.length ? '参数：' + params.join(' / ') : '无参数', fills,
        [tool.description, '点一下填：「' + fills + '」'].filter(Boolean).join('\n')));
    });
  }

  /** 技能手册目录。**每一份手册一行、可点**——改版前这里只列"角色 + 份数"，
   *  然后让人自己去记手册名；而 `/skills` 早就把整份目录（含名字）取回来了，
   *  只是没人用（`loadSkills` 存进 `view.skills`，`renderSkills` 读的却是
   *  `options.skills.roles`，两边从没对上过）。 */
  function renderSkills() {
    clear(dom.quick);
    const list = asArray(view.skillList);
    if (!list.length) {
      // 空态与加载中是**两回事**，文案必须分开：本机没装技能手册是正常状态，
      // 而"加载中…"永远停在那里既不真、也没告诉人怎么才能有。
      dom.quick.append(el('span', 'muted', view.skillLoaded
        ? '本机还没有技能手册。往控制面 data/skills/ 放 .md 手册，'
          + '或安装带技能的红队插件（如 dsh-purge），之后这里会出现。'
        : '技能手册目录加载中…'));
      return;
    }
    list.forEach((skill) => {
      const fills = '用 ' + skill.name + ' 这份手册';
      dom.quick.append(dockChip(skill.name, skill.role || '未分类',
        skill.description || skill.whenToUse || skill.source || '', fills,
        [skill.description, skill.whenToUse ? '什么时候用：' + skill.whenToUse : '',
         '点一下填：「' + fills + '」'].filter(Boolean).join('\n')));
    });
  }

  async function loadSkills() {
    try {
      const payload = await ui().apiGet('/skills');
      view.skillList = asArray(payload.skills);
    } catch (error) {
      view.skillList = [];
      ui().showError('技能手册加载失败：' + error.message);
    }
    view.skillLoaded = true;
    renderDock();
  }

  /** 切到某一格。点当前这一格 = 收起（回到 `none`）——两颗按钮自己就是开关，
   *  "怎么关掉它"不必再找第三个控件。
   *
   *  改版前"点当前这一格"对「提示」是空操作，因为那一格是默认态、关掉就没内容
   *  可看。现在没有默认格了，**收起必须可达**：否则默认收起之后第一次展开就再也
   *  收不回去，而它占的正是输入框上方最贵的那几十像素。 */
  function setDock(mode) {
    view.dock = view.dock === mode ? 'none' : mode;
    renderDock();
    // 每次打开都重取：装了新插件之后这个目录必须跟着变，而界面上没有别的地方
    // 能触发它刷新。服务端有 60 秒目录缓存，重取的代价很小。
    if (view.dock === 'skills') loadSkills();
  }

  /** 收起坞。**单独一个函数，不复用 `setDock` 的开关语义。**
   *
   *  `setDock(mode)` 里那一行是"点当前这一格就收起"的开关，它**必须知道用户点的是
   *  哪一格**；而"点别处收起"这条路上没有那一格——拿 `setDock('tools')` 去收，
   *  用户正看着技能手册时会被**换成能力目录**，两格内容看着都正常，于是表现为
   *  「点哪儿都不对」。收起的去向只有一个：`none`。
   *
   *  已经收起时直接返回：下面的文档级监听在这页**每一次点击**上都会被调到，而它
   *  按的正是这一页唯一那个操作目标（输入框）旁边的热路径。每次都重画一遍坞
   *  （清空 + 重建二十多张卡片）是白做的工，虽然看不出错。 */
  function closeDock() {
    if (view.dock === 'none') return;
    view.dock = 'none';
    renderDock();
  }

  /** 这次点击是不是落在坞上：内容区，或那两颗入口按钮。
   *
   *  **按钮算坞内**。否则点另一颗按钮会先被文档级监听收起、再被它自己的 toggle
   *  展开——两次都发生了，但顺序反了就成了"点一下没反应"（收起后立刻展开，看上去
   *  一直开着）。点**当前**那颗的语义本来就是收起，那一步由它自己的 toggle 完成，
   *  这里同样不该插手；两处都管同一个状态的时候，谁赢取决于监听顺序，而那是源码
   *  里两行代码的先后，没人会记得。
   *
   *  `closest` 缺失（很老的浏览器）或目标是文本节点时按「坞外」处理：误判成坞外
   *  的代价是多收一次（再点一下就能开），误判成坞内的代价是**永远收不起来**。
   *  两种错法不对称，所以往收得起来的那边倒。 */
  function insideDock(target) {
    if (!target || !target.closest) return false;
    if (dom.quick && dom.quick.contains(target)) return true;
    return Boolean(target.closest('.dock-tab'));
  }

  /** 重画整块坞：两颗按钮的选中态与计数，内容区按 `view.dock` 分派。
   *
   *  `none` 时**清空**内容区，而不是留着上一次那批卡片：CSS 会把那一块隐藏，而
   *  DOM 里仍然挂着二十多张卡片——收起是一个状态，不是一次视觉遮挡。留着它们的
   *  代价是"DOM 里有的东西"与"页面上有的东西"开始不一致，而夹具、截图与后面任何
   *  一条 `querySelector` 都读 DOM。 */
  function renderDock() {
    if (!dom.quick) return;
    dom.quick.setAttribute('data-dock', view.dock);
    [[dom.toolsButton, 'tools'], [dom.skillsButton, 'skills']]
      .forEach(([node, id]) => {
        if (!node) return;
        const on = id === view.dock;
        node.classList.toggle('on', on);
        node.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
    if (dom.toolsCount) {
      dom.toolsCount.textContent = view.tools.length ? String(view.tools.length) : '';
    }
    if (dom.skillsCount) {
      const count = view.skillList.length
        || ((view.options && view.options.skills && view.options.skills.count) || 0);
      dom.skillsCount.textContent = count ? String(count) : '';
    }
    if (view.dock === 'tools') renderTools();
    else if (view.dock === 'skills') renderSkills();
    else clear(dom.quick);
  }

  async function loadTools() {
    try {
      const payload = await ui().apiGet('/chat/tools');
      view.tools = asArray(payload.tools);
      if (payload.allToolCount) view.allToolCount = payload.allToolCount;
      if (payload.agentMode) view.agentMode = payload.agentMode;
    } catch (error) {
      view.tools = [];
      ui().showError('能力目录加载失败：' + error.message);
    }
    renderDock();
    renderMeta();
  }

  /* ------------------------------------------------------------ 模型状态 ---- */

  async function loadModelState() {
    try {
      const payload = await ui().apiGet('/models');
      const activeId = payload.activeId;
      let active = null;
      asArray(payload.profiles).forEach((profile) => {
        if (String(profile.id) === String(activeId)) active = profile;
      });
      view.model = active;
      view.hasModel = Boolean(active);
    } catch (error) {
      view.model = null;
      view.hasModel = false;
    }
    renderModel();
    // 模型这项的占位符要写出"跟的是哪一个"（见 renderEntry），而那份素材正是在
    // 这里才到齐的：`/session-options` 与 `/models` 谁先回来不确定，两处都重画
    // 一次才能保证占位符里一定有名字。少了这一句，先到的是 /session-options 时
    // 它会停在没有名字的版本上，而没有任何后续事件会去修它。
    renderEntry();
    renderDock();
    renderMeta();
    refreshComposer();
  }

  /** 重算输入区"能不能发"。
   *
   *  `canSend()` 取决于**异步到达**的模型信息（`view.hasModel` / `view.options.models`），
   *  而 `ensureLoaded()` 一进门就调了一次 `setSending(false)`——那时模型还没回来，
   *  发送按钮会被判成"没模型"而禁用，之后再没有人重算它。
   *  症状很难归因：首屏进来发送按钮是灰的，点一下「会话」页签又活了，
   *  看起来像随机失灵。所以凡是会改变"能不能发"的加载完成后，都要回来重算一次。
   */
  function refreshComposer() {
    setSending(view.sending);
  }

  function renderModel() {
    if (!dom.model) return;
    clear(dom.model);
    // 平时**一句话都不说**：全局启用项的名字，紧挨着的那个选择器占位符已经印了一份
    //（「跟随全局启用项（DeepSeek · deepseek-flash）」），同一行里再印一遍只是重复。
    // 有可选模型就不拦人：会话级模型选择意味着"全局没启用"不等于"没法用"。
    const choices = asArray(view.options && view.options.models);
    const usable = view.hasModel || choices.length > 0;
    if (usable) return;
    // 只剩这一种情况要开口：**一个模型都不存在**。它没有第二份——下拉会是空的，
    // 而空下拉既不解释原因、也不给出去设置的路。
    {
      dom.model.append(el('span', 'chat-model-missing',
        '未启用模型：对话需要模型来决策工具调用，请先到「设置」页签配置并启用'));
      const jump = el('button', 'btn sm', '去设置');
      jump.type = 'button';
      jump.addEventListener('click', () => ui().switchTab('settings'));
      dom.model.append(jump);
    }
  }

  /* -------------------------------------------------------------- 目标 ---- */

  // 目标选择的**单一真相**在 app.js（PentestUI.state.eid）：它同时决定
  // 「漏洞 / 资产 / 报告」的范围与顶栏那个只读指示。这里只读它、不改它，
  // 改也只经 `PentestUI.selectEngagement` —— 两处各存一份选中态，必然出现
  // "侧边栏显示的是 A、漏洞列表读的是 B"。
  const selectedEid = () => String((ui().state && ui().state.eid) || '');
  const engagementList = () => asArray(ui().state && ui().state.engagementList);
  const engagementMeta = (eid) => engagementList().find((item) => item.id === eid) || null;

  /** 目标列表（侧边栏第二块，常驻栏底）。数据由 app.js 持有，这里只负责画。
   *
   *  点一个目标做两件事，而且必须同时发生：① 把它设为「漏洞 / 资产 / 报告」的范围；
   *  ② 让它名下的对话出现在下面的「对话记录」里。只切范围不切列表，使用者会以为
   *  对话丢了；只切列表不切范围，读到的漏洞就没有归属可查。
   */
  function renderGoals(list) {
    if (list) view.goals = asArray(list);
    if (!dom.goals) return;
    // #chat-goals 自己既是列表又是滚动区（它就是 .panel-body），清空会把
    // scrollTop 归零。心跳每 3 秒就整块重画一次，正在往下看的人会被顶回顶部——
    // 与 renderSessions 同一条处理：先记下来，画完还回去。
    const scrolled = dom.goals.scrollTop;
    clear(dom.goals);
    if (!view.goals.length) {
      dom.goals.append(el('p', 'muted', '还没有目标。下发一次作业会自动建一个。'));
      markDetailPicks();
      syncDetail('goal');
      return;
    }
    // 顺序**照后端给的画**（created_at DESC，新建的在最前）。点一个目标不再把它
    // 挪到第一行（m14866 撤掉了那条）：那一行的位置每点一次就变，两次回来找同一台
    // 机器都得重新扫一遍。选中的是哪一个由高亮与「当前」标出来。
    const eid = selectedEid();
    view.goals.forEach((item) => dom.goals.append(goalRow(item, eid)));
    if (scrolled) dom.goals.scrollTop = scrolled;
    // 重画之后“被选中的那一行”是新的 DOM：高亮与详情都要重新落上去。
    markDetailPicks();
    syncDetail('goal');
  }

  /* ------------------------------------------------------- 目标的扫描面 ---- */

  /** 某个目标名下的任务。数据取自 `ui().state.jobs`（与「作业」页是**同一份**快照），
   *  不另发一次请求——两处各拉一次 /jobs 会在某一次刷新后给出两份不同的清单，
   *  而"同一个东西在两处长得不同"是最难被当成缺陷报上来的那种问题。 */
  function goalJobs(eid) {
    const all = ui().state ? ui().state.jobs : null;
    return asArray(all).filter((job) => (job.engagementId || '') === eid);
  }

  /** 某个目标下**实际扫描过的具体地址**：任务的 `target`，去重后按名字排序。
   *
   *  这一行是这一块真正缺的东西，也是它必须存在的理由：会话自动创建的目标一律叫
   *  「渗透目标」（服务端 `store.DEFAULT_SESSION_LABEL`），于是「目标」这一栏里每个
   *  目标读起来都一样，而"这个目标到底扫了什么"在界面上没有任何地方回答得了。
   *  真正的地址、域名与端口在任务的 `target` 上。
   *
   *  它**不是**资产树：资产是扫出来的结果（含子域、服务、端点），而这里是下发过任务的
   *  那些目标。两者都会出现在界面上，问的是不同的问题——"我让谁扫了什么"与
   *  "扫出了什么"。混在一起会让一次失败的扫描看起来像"什么都没扫过"。
   */
  function goalScanTargets(eid) {
    const seen = [];
    asArray(goalJobs(eid)).forEach((job) => {
      const target = String(job.target || '').trim();
      if (target && seen.indexOf(target) < 0) seen.push(target);
    });
    return seen.sort();
  }

  /** 把地址清单压成「主机（+N 个端口）」，**只在主机本身也是一个目标时才合并**。
   *
   *  为什么要压：真库里 12 个扫描目标里有 10 个是 `182.117.165.148:<端口>`。原样列出
   *  要占 3 行（真浏览器量过：20/18/53/35），而这一块的高度上限是整栏的 26%——
   *  一行的预算是 110px 上下，装不下。压完是两个词条，一行放得下，而且比三个裸地址
   *  更清楚："这台机器上打了 10 个端口"一眼看得出来。
   *
   *  为什么合并条件必须是"主机本身在清单里"：那样才算**确认**过 `h` 与 `h:port` 的
   *  从属关系。只按最后一个冒号切的话，IPv6（`[::1]:80`）、带端口的 URL、以及
   *  `host:port` 与 `host` 不相干的巧合都会被切错，而切错的症状是"某个目标从清单里
   *  消失了"——不报错，只是少一个。
   */
  function goalScanGroups(scans) {
    const children = {};
    scans.forEach((target) => {
      const cut = target.lastIndexOf(':');
      const host = cut > 0 ? target.slice(0, cut) : '';
      if (host && scans.indexOf(host) >= 0) {
        if (!children[host]) children[host] = [];
        children[host].push(target);
      }
    });
    const out = [];
    scans.forEach((target) => {
      if (children[target]) {
        out.push(target + `（+${children[target].length} 个端口）`);
        return;
      }
      // 已经归到主机那个词条里的，不再单独列一次。
      const cut = target.lastIndexOf(':');
      if (cut > 0 && scans.indexOf(target.slice(0, cut)) >= 0) return;
      out.push(target);
    });
    return out;
  }

  /** 行内联显示几个词条，超出的折成「另有 N 个」。
   *  完整清单（逐条地址，换行分隔）在 `title` 里，两种形态都有。 */
  const GOAL_SCAN_SHOWN = 2;

  function goalRow(item, eid) {
    const on = item.id === eid;
    // 外层是 `div` 而不是 `button`：行上还要放一颗删除按钮，而 `<button>` 里嵌
    // `<button>` 是非法结构——浏览器会把内层那颗甩到外面，于是"点删除"实际点到
    // 的是外层，变成切换范围。选中范围的那部分因此收进 `.chat-goal-pick`。
    //
    // 同理，**行内不许再放任何可交互的东西**（`<details>` 的摘要、按钮、链接都不行）：
    // 它们都落在 `.chat-goal-pick` 这颗粒子里。所以详细内容只有"显示出来"这一条路，
    // 不能靠"点一下展开"。这也是这里必须自己封顶、把完整清单让给 `title` 的原因。
    const row = el('div', 'chat-goal' + (on ? ' on' : ''));
    row.dataset.engagementId = item.id;
    const pick = el('button', 'chat-goal-pick');
    pick.type = 'button';

    const head = el('div', 'chat-goal-head');
    head.append(el('span', 'chat-goal-target', item.target || item.id));
    // 末段 id：跨页面对号时用的是它（任务、漏洞、报告、链路节点的归属都写 id，
    // 而名称会重名——每一个会话自动建的目标都叫「渗透目标」）。
    head.append(el('span', 'chat-goal-id', item.id));
    pick.append(head);

    /* —— 详细扫描目标：两行，"扫了多少"每行都给，"扫的是哪些"只给选中那一行。
       `jobsLoaded` 必须看：任务还没拉回来时 `goalJobs()` 返回空，那时写"还没有扫描
       目标"是一句假话，而它与"确实没扫过"的下一步动作刚好相反（一个该等，一个该去下发）。 */
    const jobs = goalJobs(item.id);
    const scans = goalScanTargets(item.id);
    const jobsLoading = !(ui().state && ui().state.jobsLoaded);
    const scanLine = el('span', 'chat-goal-scan');
    if (jobsLoading) scanLine.textContent = '扫描目标正在加载…';
    else if (!jobs.length) scanLine.textContent = '还没有扫描目标（未下发过作业）';
    else scanLine.textContent = `扫描目标 ${scans.length} 个 · 作业 ${jobs.length} 条`;
    pick.append(scanLine);

    /* **地址清单只画在选中那一行上。这是量出来的取舍，不是省事。**
       这一块的高度上限是整栏的 26%（见 style.css 那条规则的注释，它是有意留的）。
       真浏览器量过（1120×1180，栏宽 296px）：窗口 900px 高时，这一块的正文只有
       ~100px；而一行目标带四行文字是 149px（20/18/53/35）——一个都装不下，会被
       拦腰截断，且截断处正好落在中间，看起来像"这行本来就这么长"。
       本文件里那套量法留在 `.scratch/t54/measure_rows.*`（并排量了 11 种形状）。
       所以：**"扫了多少"每行都给**（这是这一块整体要回答的问题），
       **"扫的是哪些"只给正在用的那一个目标**。
       另一条压缩：地址先经 `goalScanGroups` 折成「主机（+N 个端口）」，两个词条一行。
       非选中行看得到证据分布与任务数，所以"这个目标值不值得点进去"仍然答得上。 */
    if (on && !jobsLoading && scans.length) {
      const groups = goalScanGroups(scans);
      const list = el('span', 'chat-goal-scan-list');
      list.textContent = groups.length > GOAL_SCAN_SHOWN
        ? groups.slice(0, GOAL_SCAN_SHOWN).join('、')
          + `（另有 ${groups.length - GOAL_SCAN_SHOWN} 个）`
        : groups.join('、');
      list.title = scans.join('\n');          // 逐条原始地址（未折叠），换行分隔
      pick.append(list);
    }

    /* —— 计数。**这一行必须是一个行高，而"一个行高"只有 21 个字符**——这是量出来的：
       栏宽 296px、等宽小字号下，`182.117.165.148（+10 个端口）、` 正好折在 22 字符处，
       数过三版：`意图 75 · 事实 149 · 漏洞 4 · 资产 60 · 对话 1 · 11 分钟前`（折成两行）、
       `节点 288 · 漏洞 4 · 资产 60 · 对话 1`（28 字符，仍折）、这一版 18 字符（一行）。
       折一行的代价是硬的：行高从 91px 顶到 125px，而这一块的正文在最常见的窗口高度下
       只有 100px——**最后一行会被拦腰截断**，且截断处落在字中间，看起来像"这行就这么长"。
       所以只留决策要用的三个：漏洞（结果）、资产（面）、对话（与下面那一块的连接）。
       总量、意图、事实、创建/更新时间全在 `pick.title` 里，一项没丢。
       词表取自 app.js（`ui().kindLabel`，与链路图、节点详情同一张表），不在这里另写。
       对话数在**没拉到列表时不许写 0**：那是"不知道"，不是"没有"。 */
    const kindLabel = ui().kindLabel || ((kind) => kind);
    const counts = item.counts || {};
    const nodes = ['intents', 'facts', 'findings', 'assets']
      .reduce((sum, key) => sum + (Number(counts[key]) || 0), 0);
    const chats = view.sessions.filter((session) => session.engagementId === item.id).length;
    const parts = [
      `${kindLabel('finding')} ${Number(counts.findings) || 0}`,
      `${kindLabel('asset')} ${Number(counts.assets) || 0}`,
      view.loaded ? `对话 ${chats}` : '对话 —',
    ];
    pick.append(el('span', 'chat-goal-meta', parts.join(' · ')));

    // 授权只在**有**的时候显示一行。空着不写"未授权"：那是另一个判断（有作用域设置
    // 管着它），写在每一行上会变成噪音，而噪音会把真正有授权的那一行淹掉。
    if (item.authorization) {
      const auth = el('span', 'chat-goal-auth', '授权：' + item.authorization);
      auth.title = item.authorization;
      pick.append(auth);
    }

    // 行里放不下的都在这儿：完整四项计数、创建/更新时间、逐条原始地址清单。
    // `title` 不是"锦上添花的补充"，它是这一行**唯一**能装下全部的出口——
    // 行内不许放可交互元素（见上），所以没有"点一下展开"这条退路。
    pick.title = [item.id,
                  item.target ? '目标：' + item.target : '',
                  item.objective ? '目标说明：' + item.objective : '',
                  item.authorization ? '授权：' + item.authorization : '',
                  ['intent', 'fact', 'finding', 'asset']
                    .map((kind) => `${kindLabel(kind)} ${Number(counts[kind + 's']) || 0}`)
                    .join(' · ') + ` · 节点 ${nodes} · 对话 ` + (view.loaded ? chats : '未知'),
                  item.createdAt ? '创建：' + ui().formatTime(item.createdAt) : '',
                  item.updatedAt ? '更新：' + ui().formatTime(item.updatedAt) : '',
                  scans.length ? '扫描目标（' + scans.length + '）：\n' + scans.join('\n') : '']
      .filter(Boolean).join('\n') + '\n（点击：设为漏洞 / 资产 / 报告的范围，并列出它的对话）';
    pick.addEventListener('click', async () => {
      if (item.id !== selectedEid()) await ui().selectEngagement(item.id);
      view.manualGoal = true;           // 同上：手工换的范围，后台刷新不许夺回
      renderSessions();                 // 它会连带重画目标行（行上带着"几条对话"）
      // 另外两块也重画：这一栏的四块共用 `state.eid`，不重画就会停在旧目标上
      // ——那正是"点了目标、旁边却没动"的形态。
      renderSideJobs();
      renderSideSubagents();
      // 切范围与看详情是同一件事的两面：点一个目标，右边就该摊开这个目标。
      openDetail('goal', item.id, true, item);
      // 最后一步：**打开着的对话也要换过去**。不换的话，目标高亮甲、对话记录里
      // 蓝底那条还是乙的——同一屏上两个"当前"（m00482）。
      alignSessionToGoal(item.id);
    });
    row.append(pick);


    const del = el('button', 'chat-goal-del', '删除');
    del.type = 'button';
    del.title = '删掉这个目标及其全部证据（链路 / 漏洞 / 资产）；对话会保留，只是不再归属它';
    del.addEventListener('click', () => deleteGoal(item));
    row.append(del);
    return row;
  }

  /** 删掉一个目标**及其全部证据**。
   *
   *  确认框里带出它名下有多少节点，而不是只问一句"确定删除？"——这个动作删的是
   *  证据（链路、漏洞、资产），不是一条调度记录；一句话的确认框会把"删掉一个扫错
   *  的地址"和"丢掉一整轮作业的结论"问成同一件事。
   */
  async function deleteGoal(item) {
    const counts = item.counts || {};
    const nodes = ['intents', 'facts', 'findings', 'assets']
      .reduce((sum, key) => sum + (Number(counts[key]) || 0), 0);
    if (!window.confirm(
      `确定删除目标「${item.target || item.id}」？\n\n`
      + `它会带走这个目标名下的全部证据（约 ${nodes} 个节点，含漏洞与资产）。\n`
      + `与它关联的对话会保留，只是不再归属任何目标。\n`
      + `这个动作不可撤销。`)) return;
    try {
      await ui().apiPost(`/engagements/${encodeURIComponent(item.id)}/delete`, {});
    } catch (error) {
      ui().showError('删除目标失败：' + error.message);
      return;
    }
    // 删掉的是当前选中时直接清空 `state.eid`，**不走 `selectEngagement('')`**：
    // 那个入口会顺手拉一次当前页签的数据，而此刻它指向的目标已经不存在了。
    if (item.id === selectedEid()) ui().state.eid = '';
    await ui().refreshAll();
    renderSessions();
  }

  /* ------------------------------------------------------------ 会话列表 ---- */

  /** 删掉一条对话——它**独占**的目标与任务跟着走。
   *
   *  ## 为什么删对话要连带删目标
   *
   *  对话是这里唯一的生命周期入口：它第一次下发作业（或第一次写证据）时，服务端
   *  `ensure_session_engagement` 就把它绑到一个目标上，此后这一轮的全部作业、链路、
   *  漏洞、资产都落在那个目标里（架构设计 §12.8.7）。只删对话的结果是侧边栏
   *  留下一个 0 条对话的目标、作业页留下一批再也查不到出处的执行记录——"我删掉了
   *  那段工作"于是变成"我还得再去删一次"，而第二次多半没人做。
   *
   *  ## 确认框必须分三种，不能只问一句"确定删除？"
   *
   *    * **独占**（这个目标只有这一条对话）：连目标一起删，所以把节点数与任务数
   *      念出来——删掉的是证据，不是一个名字；
   *    * **共用**（还有别的对话绑在同一个目标上）：只删对话。目标大家共用，删了它
   *      等于顺手删掉别人的证据；
   *    * **没有归属**：只删对话，也就不必提目标。
   *
   *  非管理员还有第四种：删证据是管理员动作，服务端会只删对话。前端这里先看一眼
   *  `isAdmin()` 是为了**不承诺做不到的事**，不是权限判断本身——真正的判断在
   *  服务端，前端这份身份随时可能过时（比如刚被降权）。
   *
   *  响应里的 `goal` 是唯一真相：走没走目标以它为准，不以这里的推测为准。
   */
  async function deleteSession(session) {
    const eid = String(session.engagementId || '').trim();
    const others = eid
      ? view.sessions.filter((one) => one.id !== session.id && one.engagementId === eid)
      : [];
    const meta = eid ? engagementMeta(eid) : null;
    const counts = (meta && meta.counts) || {};
    const nodes = ['intents', 'facts', 'findings', 'assets']
      .reduce((sum, key) => sum + (Number(counts[key]) || 0), 0);
    const jobs = eid ? goalJobs(eid).length : 0;
    const admin = Boolean(window.PentestAuth && window.PentestAuth.isAdmin
      && window.PentestAuth.isAdmin());
    const targetName = (meta && meta.target) || eid || '未命名目标';
    const title = session.title || '未命名对话';
    let ask = `确定删除对话「${title}」？\n\n`;
    if (!eid) {
      ask += '这条对话还没有归属目标，删掉只丢聊天记录。\n这个动作不可撤销。';
    } else if (!admin) {
      ask += `它属于目标「${targetName}」，而删除证据要管理员：\n`
        + '这次只会删掉对话，目标与它名下的证据保留。\n这个动作不可撤销。';
    } else if (others.length) {
      ask += `它属于目标「${targetName}」，那个目标还有 ${others.length} 条对话共用：\n`
        + '这次只删掉这条对话，目标与证据保留（要删目标，在它那一行点删除）。\n'
        + '这个动作不可撤销。';
    } else {
      ask += `它会连同目标「${targetName}」一起删掉：\n`
        + `约 ${nodes} 个节点（含漏洞与资产）与 ${jobs} 条任务记录都会消失。\n`
        + '这个动作不可撤销。';
    }
    if (!window.confirm(ask)) return;
    let goal = {};
    try {
      const payload = await ui().apiPost(
        `/chat/sessions/${encodeURIComponent(session.id)}/delete`, {});
      goal = (payload && payload.goal) || {};
    } catch (error) {
      ui().showError('删除对话失败：' + error.message);
      return;
    }
    if (session.id === view.sessionId) {
      view.sessionId = null;
      view.messages = [];
      view.earlierCount = 0;
    }
    // 目标真的走了 → 当前范围要清空并整页重取。**先清 `state.eid` 再刷新**：那个
    // 范围此刻指向的目标已经不存在，任何一次"按选中目标取数据"都会落空。
    if (goal.deleted && goal.deleted === selectedEid()) ui().state.eid = '';
    await loadSessions(true);
    if (goal.deleted) await ui().refreshAll();
    if (!view.sessionId && view.sessions.length) openSession(view.sessions[0].id);
    else renderLog();
  }

  function sessionRow(session) {
    const row = el('div', 'chat-session' + (session.id === view.sessionId ? ' on' : ''));
    const open = el('button', 'chat-session-open');
    open.type = 'button';
    // 标题是**单行省略**的（见 style.css 的 `.chat-session-title`），完整值挂
    // `title` 里——一列里行高忽高忽低会让眼睛找不到节奏，而标题只是用来认的。
    const title = session.title || '未命名对话';
    open.title = title;
    open.append(el('span', 'chat-session-title', title));
    const parts = [];
    if (session.messageCount) parts.push(session.messageCount + ' 条');
    if (session.updatedAt) parts.push(ui().formatRelative(session.updatedAt));
    open.append(el('span', 'chat-session-meta', parts.join(' · ')));
    open.addEventListener('click', () => openSession(session.id));
    const remove = el('button', 'link-btn chat-session-del', '删除');
    remove.type = 'button';
    remove.addEventListener('click', (event) => {
      event.stopPropagation();
      deleteSession(session);
    });
    row.append(open, remove);
    return row;
  }

  /** 会话按**目标**分组后的绘制顺序（m03903）。
   *
   *  **全部对话都在这里，一条也不藏。** 此前这一块只列"当前目标那一批"，其余用
   *  一行「另有 N 条属于其他目标」概括——而使用者要看的是"我到底有哪些对话"。
   *  分组解决的是另一个问题：全列出来之后必须看得出"哪几条属于同一个目标"，
   *  否则从上往下读是一盘散沙，点一条就换一次范围会让人以为列表自己在动。
   *
   *  顺序固定为：**全部目标按组内最近一条对话的时间倒序**，最后是「未归属」。
   *  当前目标那一组只有「当前」标签与蓝底，**不挪位置**（m17536）：点一条对话会
   *  把 `state.eid` 换成它归属的目标，一挪就成了"点谁、谁那一组跳到最顶"——
   *  这一列是导航，每点一次就重排一次，正在读的人当场丢位置。
   */
  function sessionGroups() {
    const byGoal = new Map();
    const orphans = [];
    view.sessions.forEach((session) => {
      const owner = String(session.engagementId || '');
      if (!owner) { orphans.push(session); return; }
      if (!byGoal.has(owner)) byGoal.set(owner, []);
      byGoal.get(owner).push(session);
    });
    const groups = [];
    byGoal.forEach((sessions, owner) => {
      // 组内顺序：**按更新时间倒序**（m14866 撤掉了「打开着的那条排最前」）。
      // 点一条对话不再把它的位置搬到组首：那一列每点一次就重排一次，正在读的位置
      // 跟着跳，「再回去找刚才那条」变成重新扫一遍。正在读的是哪一条由蓝底标出来。
      sessions.sort((a, b) => String(b.updatedAt || '')
        .localeCompare(String(a.updatedAt || '')));
      groups.push({ eid: owner, sessions, latest: String(sessions[0].updatedAt || '') });
    });
    // 组序只看**组内最近一条对话的时间**（m17536）。这里原先把 selectedEid() 那一组
    // 提到最前，理由是"切换范围的入口就挂在当前目标的标题上"；可点一条对话会把
    // state.eid 换成它归属的目标，于是每点一次，那一组就往上窜——表现正是
    // "会话记录还是会置顶"。谁是当前那一组由 sessionGroupRow() 的「当前」标签说。
    groups.sort((a, b) => b.latest.localeCompare(a.latest));
    return { groups, orphans };
  }

  /** 分组标题：这一组属于哪个目标、有几条，点它把范围切过去。
   *
   *  以前这里只有一条给「未归属」用的纯文字分隔线。现在它承担两件事：把"同一目标"
   *  这个事实写在对话上方，以及给出"从对话切到目标"的那条路——否则全量列出之后，
   *  想按目标收窄只能滚回最上面的目标块去点，而那一块常常已经滚出视野。
   */
  function sessionGroupRow(group, isCurrent) {
    const meta = engagementMeta(group.eid);
    const row = el('button', 'chat-sessions-divider chat-sessions-goal'
      + (isCurrent ? ' on' : ''));
    row.type = 'button';
    row.title = '点击：把「漏洞 / 资产 / 报告」与另外三块都切到这个目标';
    // 名字会被 CSS 截成一行（左栏只有那么宽，`.chat-sessions-goal-name` 是
    // `overflow: hidden`）：完整值必须挂在 title 上，否则长目标名被切掉之后
    // 没有第二处能读到它，而"这是哪台机器"正是这一行唯一的用处。
    const name = meta ? (meta.target || meta.id) : '目标 ' + group.eid;
    const label = el('span', 'chat-sessions-goal-name', name);
    label.title = name;
    row.append(label);
    row.append(el('span', 'chat-sessions-goal-count', group.sessions.length + ' 条'));
    // 目标已被删但会话还挂着它：标题保留（那些对话仍然要能看见），但不做成
    // 可点的范围入口——选一个列表里没有的 id 只会让三块面板各报一次 404。
    if (!meta) return row;
    if (isCurrent) row.append(el('span', 'chat-sessions-goal-tag', '当前'));
    row.addEventListener('click', () => chooseGoal(group.eid));
    return row;
  }

  /** 点分组标题 → 切范围；另外三块一起跟上（这一栏的四块共用 `state.eid`）。
   *
   *  只调 `selectEngagement` 是不够的：它管漏洞 / 资产 / 报告，而任务与子智能体
   *  两块是从这里画的，不重画就会停在旧目标上——那正是"点了目标、旁边却没动"
   *  的形态。三句放在同一处，别的入口（任务行、打开会话）复用同一套。
   */
  function chooseGoal(eid) {
    const next = String(eid || '');
    const state = ui().state || {};
    if (String(state.eid || '') === next) return;
    // 这一下是**使用者亲手**换的范围。记下来：后台的列表刷新（`loadSessions` →
    // `adoptSessionEngagement`）不许再把人拽回打开着的那条对话的目标。
    view.manualGoal = true;
    ui().selectEngagement(next);
    renderSessions();
    renderSideJobs();
    renderSideSubagents();
    // 目标与对话必须对应（m00482）：这一句管"换目标 = 换到它名下的对话"。
    alignSessionToGoal(next);
  }

  /* ------------------------------------------------ 目标与打开着的对话 ---- */

  /** 换目标时把**打开着的对话**一起换过去：目标与对话必须对应（m00482）。
   *
   *  `adoptSessionEngagement()` 只管"换对话 → 范围跟过去"这一个方向。反过来的那一步
   *  一直是空的：范围停在甲、打开着的还是乙名下的对话，于是「目标」块高亮甲、
   *  「对话记录」里蓝底那条属于乙——同一屏上两个"当前"，而读者无从知道右边这份
   *  数据到底在讲哪台机器。
   *
   *  ## 三条分支，各挡一种"更省事但更错"的写法
   *
   *  * 打开着的那条**已经属于这个目标** → 什么都不做。否则在一个目标下点过一次，就会
   *    把人挑好的那条较早的对话换回"最近一条"——点一下就把人的选择改掉。
   *  * 这个目标**名下没有对话** → 清成「新对话」（`newSession()`）。留着上一条只是把
   *    "不对应"挪到看不见的地方；而"这下面还没有对话"这句 `paintSessions` 本来就会说。
   *  * 有一轮**正在跑**（`view.sending`）→ 不硬切。那一轮的流会把正文逐条写进当前这个
   *    视图，切走就是"乙的回答落在甲的记录里"。记进 `view.pendingGoalSwitch`，由
   *    `finish()` 收尾时补上，对话记录那一块会写明在等什么。
   */
  function alignSessionToGoal(eid) {
    const next = String(eid || '');
    if (!next) { view.pendingGoalSwitch = ''; return; }
    // 列表里还没有打开着的这一条时**不下判断**（列表还没拉回来）：猜错一次就会把
    // 刚发出去的那一轮从屏幕上清掉。注意这个守卫**只挡"有 id 但列表里没有"**——
    // 「新对话」是一个 id 为空的草稿，它本来就没有归属，换目标时照样要能跟着走，
    // 否则在空目标下起了一条新对话、再点回有对话的目标，就停在空白里不动了。
    const open = view.sessionId
      ? view.sessions.filter((one) => one.id === view.sessionId)[0] : null;
    if (view.sessionId && !open) return;
    if (open && String(open.engagementId || '') === next) { view.pendingGoalSwitch = ''; return; }
    if (view.sending) { view.pendingGoalSwitch = next; return; }
    view.pendingGoalSwitch = '';
    // 列表还没到就别拿"这个目标名下没有对话"下结论：那句话是一个断言，不是默认值。
    if (!view.loaded) return;
    const mine = view.sessions.filter((one) => String(one.engagementId || '') === next);
    // 草稿本来就没有归属，不必再清一遍（`newSession()` 会把它已经排上的东西一并收掉）。
    if (!mine.length) { if (open) newSession(); return; }
    // "最近一条"按 `updatedAt` 挑，不靠服务端返回顺序：那份顺序没有写进契约。
    const latest = mine.slice().sort((a, b) =>
      String(b.updatedAt || '').localeCompare(String(a.updatedAt || '')))[0];
    openSession(latest.id);
  }

  function renderSessions() {
    if (!dom.sessions) return;
    // **滚动位置记一下再还回去。** 这个列表待在自己的滚动容器里
    // （`.chat-side-scroll`，「目标」那块在它外面），而它会在换页签、每次列表刷新、以及一轮对话**每落一条
    // 消息**时被重画——重画一次就把正在往下读的人顶回顶部。而"我读着读着它自己跳上去了"
    // 会被当成浏览器抽风，不会当成缺陷报上来。
    // 与报告页目录每 10 秒重画是同一件事，处理方式照抄那里（见 `renderReportSide`）。
    const scroller = dom.sessions.closest('.chat-side-scroll');
    const scrolled = scroller ? scroller.scrollTop : null;
    paintSessions();
    if (scroller && scrolled) scroller.scrollTop = scrolled;
  }

  function paintSessions() {
    if (!dom.sessions) return;
    clear(dom.sessions);
    const eid = selectedEid();
    const meta = engagementMeta(eid);
    // **`mine` 在没选目标时必须是空的。** 写成 `filter(s => s.engagementId === eid)`
    // 时，`eid === ''` 会让它等于「未归属」那一批，于是同一批对话被画两遍：一遍
    // 光着、一遍挂在「未归属」标题下。可达路径是"删掉当前选中的目标"（`deleteGoal`
    // 把 `state.eid` 清空），而症状是同一个对话在列表里出现两次。
    const mine = eid ? view.sessions.filter((session) => session.engagementId === eid) : [];
    // 归属为空的历史对话（在这次改动之前建的，且反推不出目标——例如只问过
    // "有哪些执行节点"）在 `sessionGroups()` 里并成最后一组。它们必须仍然可见：
    // 不列出来就在界面上消失了，而"对话不见了"比"对话没有目标"严重得多。

    /* 栏头是**这一块折起来之后唯一的读数**（与子智能体那一块同一条理由：折上之后
       "有 3 条对话"这件事不能从屏幕上消失）。所以条数写在栏头，不只写在正文里。
       三种"还没有"必须分得开，这里就是它们各自的那一行。 */
    if (view.sessionsError) setHint(dom.sessionsHint, '加载失败');
    else if (!view.loaded) setHint(dom.sessionsHint, '正在加载…');
    else {
      // 栏头给出两个数：**本目标有几条**（当前范围）与**一共几条**（全量列表）。
      // 只写本目标那个数时，全量列出之后读起来像"列表里只剩这几条"；只写总数时，
      // 又看不出当前范围收窄到了多少。
      setHint(dom.sessionsHint, eid
        ? '本目标 ' + mine.length + ' 条 / 共 ' + view.sessions.length + ' 条'
        : '全部 ' + view.sessions.length + ' 条');
    }

    // "目标换了、对话还没换"这个中间态必须自己说出来。不说的话屏幕上同时有两个
    // "当前"，而读者会以为是它没反应——实际是在等正在跑的那一轮收尾。
    if (view.pendingGoalSwitch) {
      dom.sessions.append(el('p', 'muted',
        '这一轮还在跑：收尾后自动打开本目标的对话（现在这条属于另一个目标）。'));
    }

    if (view.sessionsError) {
      /* 请求失败**不改口**。原来这里把 `view.sessions` 清成空数组，于是任何一次网络
         抖动都会让这一块改说"还没有对话"——一句假话，而且它会一直停在那儿（要等
         下一轮指纹变化才可能自愈，而指纹不变时它永远自愈不了）。现在：已知的那份
         列表原样留在 `view.sessions` 里，这里只说明是哪一次请求失败了，并给一个
         重试入口（不给的话，唯一的出路是刷新整个页面）。 */
      const fail = el('p', 'muted chat-sessions-fail',
        '对话列表加载失败：' + (view.sessionsError.message || view.sessionsError));
      const retry = el('button', 'link-btn chat-sessions-retry', '重试');
      retry.type = 'button';
      retry.addEventListener('click', () => loadSessions(true));
      fail.append(' ', retry);
      dom.sessions.append(fail);
    } else if (!view.loaded) {
      // 「还没拉到」与「确实没有」必须分开：以前两者都画成"还没有对话"，而这两件事
      // 的下一步动作完全不同（一个该等，一个该去提问）。
      dom.sessions.append(el('p', 'muted', '正在加载对话列表…'));
    } else if (!view.sessions.length) {
      dom.sessions.append(el('p', 'muted', '还没有对话。在右边提问即会新建一条。'));
    } else {
      /* **全部对话都列出来**（m03903 的原话），按目标分组。以前这里只画当前
         目标那一批、其余用「另有 N 条」概括：那让"我有哪些对话"变成一个必须
         逐个点目标才能拼出来的答案。 */
      const { groups, orphans } = sessionGroups();
      if (eid && !groups.some((group) => group.eid === eid)) {
        // 选中的目标名下一条都没有：这一句**必须说**。否则整块看起来只是"一堆
        // 别的目标的对话"，而这个目标到底有没有对话、该怎么让它有，无从得知。
        dom.sessions.append(el('p', 'muted',
          `这个目标名下还没有对话。在右边提问会新建一条；若它第一条任务打的就是`
          + `${(meta && meta.target) || '这台机器'}，就归到这里。`));
      }
      groups.forEach((group) => {
        dom.sessions.append(sessionGroupRow(group, group.eid === eid));
        group.sessions.forEach((session) => dom.sessions.append(sessionRow(session)));
      });
      if (orphans.length) {
        dom.sessions.append(el('div', 'chat-sessions-divider', '未归属（没有关联目标）'));
        orphans.forEach((session) => dom.sessions.append(sessionRow(session)));
      }
    }
    renderGoals();       // 目标行上写着"几条对话"，它依赖上面这份列表
  }

  async function loadSessions(force) {
    if (!window.PentestAuth || !window.PentestAuth.getUser()) return;
    if (view.sessionsInflight && !force) return view.sessionsInflight;
    const inflight = (async () => {
      try {
        const payload = await ui().apiGet('/chat/sessions');
        view.sessions = asArray(payload.sessions);
        view.loaded = true;
        view.sessionsError = null;
        adoptSessionEngagement();
        autoOpenFirstSession();
      } catch (error) {
        // **不清空已知的列表**（见 `paintSessions` 里那段）。只记下这一次失败。
        view.sessionsError = error;
        // 手上这份的来路跟着变回"不清楚"：心跳是按"这一轮问过"记指纹的，它不知道
        // 结果。不清的话，这份旧列表就再也等不到下一次重拉——指纹已经"对上了"。
        view.sessionsStamp = undefined;
        ui().showError('对话列表加载失败：' + error.message);
      }
      renderSessions();
      // 会话列表变了 → 目标范围下"最近一条对话"可能换了，子智能体那块跟着重取。
      renderSideSubagents();
      renderMeta();
    })();
    view.sessionsInflight = inflight;
    try { await inflight; } finally {
      // 只在"还是我这一份"时才清。无条件清会把**后一次**（`force` 进来的）请求的
      // 在途标记抹掉，于是第三个调用者会再打一次请求，而三条并发响应谁后到谁说了算。
      if (view.sessionsInflight === inflight) view.sessionsInflight = null;
    }
    return undefined;
  }

  /** 栏头上那个读数位。四处都写 `if (node) node.textContent = …` 太吵，
   *  而少写一次 `if` 就是一个"某次重画之后栏头报错"的静默崩溃。 */
  function setHint(node, text) { if (node) node.textContent = text; }

  /** 当前这条对话属于某个目标 → 把范围跟到它上面。
   *
   *  ## 为什么必须跟
   *
   *  侧边栏「对话记录」是**按选中的目标**列出来的（`paintSessions` 里那个 `mine`），
   *  所以不跟的话，正在看的这条对话不会出现在它自己的列表里。而服务端在第一次产出型
   *  调用时就已经把它绑到那个目标上了（`store.ensure_session_engagement`）——绑的是
   *  **会话自己**，与"侧边栏选中了哪个"无关。使用者的原话是"目标和对话记录没有生成"：
   *  目标确实建出来了，只是侧边栏这两块都没跟上。
   *
   *  ## 换对话时必须真的跟过去
   *
   *  「漏洞」「资产」两页的范围就是 `state.eid`。服务端现在把**一条对话里的全部作业**
   *  都写进它自己的那一个目标（`chat.session_engagement`），所以"会话范围"与"该会话
   *  的目标范围"是同一件事。此前这里只在一个都没选时才跟：于是从对话 A（目标甲）切到
   *  对话 B（目标乙）之后，两页仍然列着甲的漏洞与资产——标签写着 B、内容是甲，这正是
   *  使用者报的"同一会话的东西列不全、串味"。
   *
   *  ## 什么时候不跟
   *
   *  * **同一条对话里，使用者明确选过的目标不被后台刷新夺回。** 判据是 `(sid, owner)`
   *    这一对**变没变**，而不是心跳每拉一次列表就跟一次；不这样去重的话，侧栏每 3 秒
   *    重拉一次就会把手动选的目标弹回去。**反过来的那一半不在这里**：点目标会把打开着
   *    的对话一起换过去（`alignSessionToGoal`），所以"范围在甲、对话却是乙的"不是一条
   *    可以长期停留的状态；`manualGoal` 现在挡的是心跳这条路径，以及"那一轮还在跑、
   *    切换被挂起"的那个窗口。
   *  * **归属为空时不动**：修复之前的对话没把归属写进库（`engagement_id` 为空），
   *    此时清空范围会把它们已有的证据挡在门外。新对话在第一次产出型调用前也会短暂
   *    处于这个状态，跟一次空归属没有好处。
   *
   *  调用点只有两个：列表拉回来之后、以及打开一条对话之后——都是"我刚刚知道这条对话
   *  是谁的"那一刻。
   */
  function adoptSessionEngagement() {
    const state = ui().state || {};
    const sid = view.sessionId;
    if (!sid) return;                               // 没有打开的对话，没有可跟的
    const mine = asArray(view.sessions).filter((one) => one.id === sid)[0];
    if (!mine) return;                              // 列表里还没有这一条，别下判断
    const owner = String(mine.engagementId || '');
    const last = view.scopeSync || {};
    if (last.sid === sid && last.owner === owner) return;   // 这一对已经跟过
    if (!owner) {                                   // 空归属：见上面第二段
      view.scopeSync = { sid, owner };
      return;
    }
    // 归属的目标可能已经不在了（删目标不会顺手解除绑定）：选一个列表里没有的 id
    // 只会让三块面板各报一次 404。列表为空时不判——那是"还没拉回来"，下一个心跳
    // 会再跟一次（这一对先不记进 `scopeSync`，所以不会漏）。
    const goals = asArray(state.engagementList);
    if (goals.length && !goals.some((goal) => String(goal.id) === owner)) return;
    view.scopeSync = { sid, owner };
    if (String(state.eid || '') === owner) return;  // 已经在看这个目标，别重载一遍
    // **使用者亲手换过范围之后，后台刷新不许把他拽回来。** 这一函数有两条入口：
    // 点开一条对话（那一刻 `manualGoal` 刚被清掉，应该跟），与列表刷新（`loadSessions`
    // 的心跳，可能是任何时刻）。后者若也硬跟，就会出现"点着目标甲看任务，心跳一到
    // 范围自己跳回乙"——一个只在后台刷新时发作的跳动，最难被当成缺陷报上来。
    if (view.manualGoal) return;
    ui().selectEngagement(owner);
    // 选中态变了，这一块必须重画——与目标行点击那条路同一句、同一理由：这一块的
    // 正文是**按选中的目标**筛出来的，不重画它就还写着"未选择目标 · 0 条"。
    // `selectEngagement` 里 `state.eid` 是第一句赋的（同步），所以这里不用等它。
    renderSessions();
    // 目标 / 对话 / 任务 / 子智能体四块共用同一个范围（m03903）：选中态一变，
    // 另外两块也得跟着重画，否则就是"点了对话、旁边没动"。
    renderSideJobs();
    renderSideSubagents();
  }

  /** 一条都没打开过、而列表里已经有了 → 打开第一条（与 `ensureLoaded` 同一条规则）。
   *
   *  **只在"从没打开过"时做。** 使用者在界面上按过「新对话」之后 `sessionId` 是
   *  **他清空的**，那时自动打开第一条会把他刚清空的输入区又塞满历史记录。
   *
   *  为什么需要它：启动那一刻列表可能还是空的（旧对话被删光了、或这是全新一台机器），
   *  而第一条对话是**之后**才出现的。启动路径里那条 `openSession(view.sessions[0].id)`
   *  只在启动那一次跑，赶不上——于是侧边栏列着"另有 1 条对话属于其他目标"，而主区
   *  一片空白，两边都言之凿凿。
   */
  function autoOpenFirstSession() {
    if (view.sessionId || view.everOpened || !view.sessions.length) return;
    openSession(view.sessions[0].id);
  }

  function setHint(node, text) { if (node) node.textContent = text; }

  /** 当前打开的会话 id（没开就是空串）。工作台心跳要用它去要"这个会话的消息指纹"。 */
  function currentSessionId() { return view.sessionId || ''; }

  /** 工作台心跳 → 会话列表与当前会话的消息跟着变新。
   *
   *  指纹是"变了没有"，不是内容：
   *    * `sessions` 抓新会话 / 改名 / 删除 / 条数变化——`store.append_chat_message`
   *      每落一条都会顺手推一下 `chat_sessions.updated_at`，所以一轮对话的"N 条"
   *      会自己长；
   *    * `messages` 抓**当前打开那个会话**又多了几条，也就是"别处（另一个标签页、
   *      另一个设备）正在回答"。
   *
   *  ## 为什么只在"这一轮不是我在跑"时才重取消息
   *
   *  本标签页正在发送时，消息由 SSE 逐条落地。这时再整份拉一次会与那条流打架：
   *  流里刚追加的那条会被整份覆盖掉，看起来是"回答闪了一下又退回去"。
   *
   *  ## 为什么直接拉消息、而不复用 `openSession`
   *
   *  `openSession` 会连入口三项（Agent / 模型 / 权限）的 `config` 一起重取并回填到
   *  下拉上。而它被心跳调用时，那件事是**有害**的：使用者刚在下拉里选好、还没发出去的
   *  那一项，会在 3 秒后被悄悄改回服务端的值。这里只要消息。
   */
  function syncFromPulse(stamps) {
    const next = stamps || {};
    const previous = view.stamps || {};
    const messagesChanged = previous.messages !== undefined
      && previous.messages !== next.messages;
    view.stamps = next;
    /* 会话列表那一块判的是"**我手上这份列表**是不是旧的"，不是"服务端上一轮说了什么"
       （与 app.js 的目标列表同一条理由，见那里的 `listStamps`）：启动时
       `loadSessions()` 与第一次探询是并发的，服务端这一轮的值可能已经比页面手上的新
       ——拿它当基线，"新会话出现了"就永远不会被判定成变化，侧边栏「对话记录」
       会一直停在启动那一刻，而底下还会如实写着"另有 N 条对话属于其他目标"。 */
    if (view.sessionsStamp !== next.sessions) {
      view.sessionsStamp = next.sessions;   // 拉失败时 `loadSessions` 会清回"不清楚"
      loadSessions(true);
    }
    if (!messagesChanged || !view.sessionId || view.sending) return;
    const box = dom.log;
    const keepTop = box ? box.scrollTop : 0;
    const stay = box ? nearBottom() : true;
    ui().apiGet(`/chat/sessions/${encodeURIComponent(view.sessionId)}`)
      .then((payload) => {
        // 请求飞在路上时使用者换了会话 → 这份是别人的，丢掉。
        if (!payload || !payload.session || payload.session.id !== view.sessionId) return;
        adoptMessages(payload.messages, payload.earlierCount);
        renderLog();
        // `renderLog` 会滚到底（"打开一个会话"的语义）。这里只是刷新，正在往上翻
        // 记录的人不该被拽下去。
        if (box && !stay) box.scrollTop = keepTop;
      })
      .catch(() => { /* 心跳失败不报：下一轮 3 秒后还有一次 */ });
  }

  /* ------------------------------------------------------ 上下文栏 · 任务 ---- */

  /** 上下文栏的「任务」块。
   *
   *  **数据归 `app.js`**（`state.jobs`，与「作业」页签是同一份），这里只负责画
   *  ——与 `renderGoals` 同一分工，理由也一样：两处各拉一次 `/jobs` 会各自持有
   *  自己的快照，然后在新任务出现时显示得不一样，而"同一个东西在两处长得不同"
   *  是最难被当成缺陷报上来的那种问题。
   *
   *  状态词表、排序、类型名全部取自 `ui().jobView`，**不在这里再写一份**：
   *  同一批任务在两处用两套词，症状是「执行中」与「运行中」并存——读的人会
   *  以为那是两种不同的状态，而不是同一个东西的两种写法。
   *
   *  `error` 由 app.js 传进来：刚跑完那次请求的是它，只有它知道失败原因。
   */
  function renderSideJobs(error) {
    // 任务块重画之后，那一行“被选中的任务”是新的 DOM、任务对象也可能换了一份，
    // 所以高亮要在**整块画完之后**补一次，详情也要按新数据同步一次。
    // 包一层而不是把两句挤进 paintSideJobs 里每一个 return 之前：那里有五个早退，
    // 漏一个的症状是“选了任务 A，列表刷新后详情停在 A 的旧数据上”——它看起来
    // 完全正常，只有耗时那一格不动，没人会怀疑。
    paintSideJobs(error);
    markDetailPicks();
    syncDetail('job');
  }

  function paintSideJobs(error) {
    // 目标行上的「扫描目标 N 个 · 任务 M 条」读的就是这一批任务，所以任务一变就得
    // 连带重画它。**放在本函数最前面**：下面三个早退（没有 DOM、加载失败、空列表）
    // 都会提前 return，放在后面等于「作业列表空了之后目标行永远停在旧数字上」——
    // 而那个旧数字看起来完全正常。
    renderGoals();
    if (!dom.sideJobs) return;
    const jobView = ui().jobView;
    if (!jobView) return;
    clear(dom.sideJobs);
    if (error) {
      setHint(dom.sideJobsHint, '加载失败');
      dom.sideJobs.append(el('p', 'muted', '作业列表加载失败：' + error.message));
      return;
    }
    if (!ui().state.jobsLoaded) {
      // 栏头已经写着「正在加载…」。正文这句不复述它，而是趁等待告诉使用者
      // **这一块的性质**——它只列这条对话派下去的，不是全部。
      setHint(dom.sideJobsHint, '正在加载…');
      dom.sideJobs.append(el('p', 'muted',
        '这一块列这条对话派下去的作业；全部作业在「作业」页签。'));
      return;
    }
    if (!asArray(ui().state.jobs).length) {
      // 栏头写的是「暂无」，所以正文不再说一遍「还没有任务」，只说**怎么才会有**。
      setHint(dom.sideJobsHint, '暂无');
      dom.sideJobs.append(el('p', 'muted',
        '在「会话」里对助手说一句，作业就有了一条。'));
      return;
    }
    /* —— 只列**这条对话**派下去的任务（m14866）。这一块原先是三档闸门
       （这条对话 → 本目标 → 全部目标，m15165），使用者的判断是「只显示当前会话的内容，
       取消看全部」：一块会自动改范围的列表，读之前永远要先确认"这一屏是哪一批"。
       现在范围只有一档，而且由主区开着哪条对话唯一决定，没有手工开关。
       **不动 `state.eid`**：那一个决定漏洞 / 资产 / 报告的范围。 */
    const rows = sessionJobs(jobView);
    if (!rows.length) {
      setHint(dom.sideJobsHint, '这条对话暂无');
      dom.sideJobs.append(el('p', 'muted',
        '这条对话还没有下发过作业。作业归到哪条对话，由下发它的那一轮决定；'
        + '手工下发与命令行下发的作业不带对话归属，只在「作业」页签里。'));
      return;
    }
    // `compare` 已经把执行中排在前面，所以往下读的第一眼就是还活着的那些。
    const live = rows.filter((job) => !jobView.finished.has(jobView.statusOf(job))).length;
    setHint(dom.sideJobsHint, live ? '未结束 ' + live : '全部已结束');
    // **一条都不截**（m14866）。原先只列前 5 条，多出来的靠一颗「查看全部」按钮
    // 指去「作业」页签——而这一块回答的正是「这条对话还派着几件活」，
    // "另有 3 条要再点一次才看得见"恰是这个问题回答不了的地方。
    rows.forEach((job) => dom.sideJobs.append(sideJobRow(job)));
  }

  /** 这一块列的是**哪一批**任务：只有一批——这条对话派下去的那些。
   *
   *  范围由「主区开着哪条对话」唯一决定，没有手工开关（m14866）。原先它是个三档
   *  闸门（这条对话 → 本目标 → 全部目标，m15165），每一档还各配一颗往回按的按钮；
   *  使用者的判断是「只显示当前会话的内容，取消看全部」——一块会自己改范围的列表，
   *  读之前永远要先确认"这一屏是哪一批"，而它回答的问题只有一个：**这条对话还派着
   *  几件活**。
   *
   *  没有 `sessionId` 的老任务（手工下发、命令行下发）因此不在这里出现。它们不是
   *  "被藏起来"：这一块从来不声称自己是全部，「作业」页签才是，而它一直在那儿。
   */
  function sessionJobs(jobView) {
    const id = String(view.sessionId || '');
    if (!id) return [];
    return asArray(ui().state.jobs)
      .filter((job) => String(job.sessionId || '') === id)
      .sort(jobView.compare);
  }
  /** 点侧栏的一条任务 → 打开详情，并把范围跟到它所属的目标。
   *
   *  「作业」页签是全局列表、不跟范围；侧栏这一块只列这条对话的任务，理论上那条任务
   *  的目标就是这条对话的目标——但使用者可以在开着对话的情况下手工点目标行换范围，
   *  于是右边写着甲的任务、左边三块已经是乙的东西。点一条任务时把范围跟回去，
   *  同一次点击就只有一个范围（m00482 那条「两个方向都要走通」的同一个道理）。
   *  目标已被删（不在 `engagementList` 里）时不跟：选一个不存在的 id 只会让三块面板
   *  各报一次 404。
   */
  function pickJob(job) {
    const owner = String(job.engagementId || '');
    const state = ui().state || {};
    const known = asArray(state.engagementList)
      .some((goal) => String(goal.id) === owner);
    if (owner && known && String(state.eid || '') !== owner) {
      view.manualGoal = true;           // 点任务行也是手工换范围
      ui().selectEngagement(owner);
      renderSessions();
      renderSideJobs();
      renderSideSubagents();
      // 任务行的目标换了 = 对话也得换过去（与点目标行同一条规则，m00482）。
      alignSessionToGoal(owner);
    }
    openDetail('job', job.id, true, job);
  }

  /** 侧栏一行任务：状态、类型、时间、目标。
   *  比「作业」页签那份窄得多，所以耗时、作业号、执行节点、AI 规划出处全部留在
   *  那边——**两份的详略不同是成立的，因为问的是两个问题**：这里是"还有活在跑吗"，
   *  那边是"这条任务到底怎么样"。 */
  function sideJobRow(job) {
    const jobView = ui().jobView;
    const status = jobView.statusOf(job);
    // 行是 `<button>`：它现在的第一职责是“点开看详情”，而按钮天然带焦点、回车
    // 与读屏语义。以前它是不可点的 `div`，所以没有这层顾虑；改成可交付的控件
    // 之后，`div` + addEventListener 在键盘上按不动，等于对一半使用者不存在。
    const row = el('button', 'chat-side-job');
    row.type = 'button';
    row.dataset.jobId = job.id;
    row.title = '点击在右侧看详情';
    row.addEventListener('click', () => pickJob(job));
    const line = el('div', 'chat-side-job-line');
    line.append(el('span', 'status-badge status-' + status, jobView.label[status]));
    line.append(el('span', 'tag', jobView.kindLabel(job)));
    line.append(el('span', 'spacer'));
    const created = el('span', 'chat-side-job-meta', ui().formatRelative(job.createdAt));
    created.title = ui().formatTime(job.createdAt);
    line.append(created);
    row.append(line);
    const target = el('span', 'chat-side-job-target', job.target || '（无目标）');
    target.title = job.target || '';
    row.append(target);
    return row;
  }

  /* -------------------------------------------------- 上下文栏 · 子智能体 ---- */

  /** 这一块显示的是**哪一次**派活。
   *
   *  优先正在进行的那一轮：派活是同步的（父轮要等它们全部跑完），那几分钟里这一块
   *  是唯一的进度来源，而它此刻还没落库、不在 `view.messages` 里。
   *  否则取**最近一次**带子智能体的消息——不是全部累积：这一块回答的是"现在/刚才
   *  那几个兄弟走到哪了"，把整场会话几十条派活叠在一起就没人读得完，而那是主区
   *  消息列表的活。
   *
   *  名字里**不出现 `sideSubagentRow` 这个子串**（所以不是 `sideSubagentRows`）：
   *  那是当年被删掉的那个逐行自绘函数的名字，而契约测试正是按这个子串做反向断言。
   *  取一个包含它的名字会让那条断言去报一个不存在的问题——反过来，也会让下一次
   *  真正想搜"它回来没有"的人搜到一处无关的命中。
   */
  function currentSubagentRows() {
    if (view.live && asArray(view.live.subagents).length) return view.live.subagents;
    for (let i = view.messages.length - 1; i >= 0; i -= 1) {
      const rows = asArray(view.messages[i].subagents);
      if (rows.length) return rows;
    }
    return [];
  }

  /** 上下文栏的「子智能体」块。
   *
   *  **与主区共用 `subagentBlock()`**，不是另写一个简版——这一点是硬要求。
   *  这一块曾经被整个删掉，理由是"侧栏那份只有摘要、主区那份带完整工具链，
   *  两者对不上时读的人没有依据判断该信哪一份"。那条理由没有被推翻，它是被
   *  **消掉**的：两处调同一个函数，详细程度无从不同。删掉的是第二条渲染路径，
   *  不是这个位置——所以第 2 条渲染路径不许以任何形式回来（契约测试钉着）。
   */
  /** 这一块该显示谁的子智能体：**只有这条对话的**（m14866）。
   *
   *  它原先是跟选中目标走的（m03903）：主区开着的那条对话不属于选中目标时，
   *  就去拉"这个目标名下最近一条对话"，再在正文里标一行来源。那条路现在没有了——
   *  这一块与「任务」块是同一条判据（`sessionJobs`）：翻的就是主区这一屏的东西。
   *
   *  少掉的不只是那次网络请求与两层缓存（`goalSubagentCache` / `goalSubagentWaiting`），
   *  还有一整类状态：这一块曾经可能停在"正在取…"、停在"取派活失败"、停在"本目标
   *  还没有对话"——而使用者要看的是"刚才那几个兄弟走到哪了"。
   */
  function subagentScope() {
    return { rows: currentSubagentRows(), waiting: false };
  }

  function renderSideSubagents() {
    if (!dom.sideSubagents) return;
    clear(dom.sideSubagents);
    const rows = subagentScope().rows;
    if (!rows.length) {
      // 栏头写的是「本会话还没派过」。正文不再把同一句话说第二遍，只说
      // **什么时候它会动**——栏头答"现在有没有"，正文答"以后怎么看"。
      setHint(dom.sideSubagentsHint, '本会话还没派过');
      dom.sideSubagents.append(el('p', 'muted',
        '助手派出子智能体时，这一块会跟着那条消息一起动。'));
      // 只解释“以后怎么看”是不够的：人已经站在这儿了，缺的是**怎么让它发生**。
      // 不放按钮：派不派是模型的判断，一颗“在这里派活”的按钮点下去不会长出
      // 行来，只会变成一个点了没反应的假按钮。也不放“填一句并行请求试试”
      // 那种填字芯片：它把“该怎么措辞”藏进一次点击，而读者真正要的是能把
      // 下面这句读完、据此自己决定要不要派。
      //
      // 2 这个数字不是拍脑袋的：单轮步数预算按人头摊，每个子智能体保底 3 步。
      // 真模型实测：红队预设 10 步下派 2 个（各 1 步）双双完成；派 3 个则三个
      // 全部撞预算失败——写了这句，使用者才不会以为是功能坏了。
      dom.sideSubagents.append(el('p', 'muted',
        '派活由模型判断，写成互不相干的几路它才会派；一轮派 2 个最稳，'
        + '3 个会把步数预算摊薄到每人只剩 3 步。'));
      markDetailPicks();
      syncDetail('subagent');
      return;
    }
    const running = rows.filter((row) => row.state === 'running').length;
    setHint(dom.sideSubagentsHint,
      '最近一次派活' + (running ? ' · 跑动中 ' + running : ''));
    // 与主区**同一个** `subagentBlock()`（契约测试钉着“只有一条渲染路径”）：
    // 可点这件事因此只能在渲染之后补，不能另写一个带按钮的版本。
    const block = subagentBlock(rows);
    wireSubagentPicks(block, rows);
    dom.sideSubagents.append(block);
    markDetailPicks();
    syncDetail('subagent');
  }

  /* ------------------------------------------------------ 详情（就地展开） ---- */

  /* 这一节回答的是“点开之后”的问题：各块自己只放判断要不要点的那几行，完整内容
     就展开在被点中那一行**下面**——任务的状态 / 耗时 / 执行节点 / 结果摘要与
     **执行过程**（跑着的那条边跑边长），目标的作用域、实扫地址、名下任务与对话，
     子智能体那几步工具调用与它最后带回来的结论。
     四条实现约束：
       · 选中态只有一份（`view.detail`）。行上的 `is-picked` 是它渲染出来的
         **结果**，不是第二份状态——两份状态迟早不一致，症状是“点 A 亮 B”。
       · 详情是**投影**，不是快照。任务心跳一变就重画；打开那一刻拷一份行数据
         存起来的话，执行中的任务会一直停在“排队”。
       · 收起不是销毁：`view.detail` 留着，重新拉开看到的还是同一条；只有换会话
         （子智能体那份）、退出登录、或删掉目标才会清空（`closeDetail`）。
       · 不另开会话：详情里的数据全部来自本页已有的快照，只有任务执行过程多发一个
         `/jobs/{id}/events`（那是页面上本来就有、只是以前只在「作业」页签才能看到的
         东西）。

     **这一节以前是把内容画进右栏底部那片 `.chat-detail-body` 的**（用户原话：
     "右侧边栏取消详情内容，把功能放到任务和子智能体中，详细内容在这两个框中直接
     显示"）。那片正文被拿掉之后，三个渲染器各收一个 `host` 参数——host 就是被点中
     那一行后面那个 `.chat-detail-inline`。渲染逻辑一行没改，改的只是“画到哪儿”：
     行与展开面是**兄弟**（行是 `button`，按钮里不能套块级内容）。 */

  /** 三种详情各自在栏头写什么。 */
  const DETAIL_KIND = { goal: '目标', job: '作业', subagent: '子智能体' };

  /** 执行过程最多同时留多少行。末尾永远是最新的，滚出去的行数会在头部写明。 */
  const DETAIL_LOG_KEEP = 600;
  /** 任务日志一页多少行（与后端 `JOB_EVENT_PAGE` 同一个量级，但不强求相等：
   *  这里只要“一次拿一页、拿完接着拿”。 */
  const DETAIL_LOG_PAGE = 300;
  const DETAIL_LOG_INTERVAL = 2000;

  /** 某个 id 是不是当前选中的那一项。带 kind 比：两种 id 命名空间不重叠，但
   *  真撞上时“点任务却亮起一行目标”是能发生的。 */
  function detailPicked(kind, id) {
    return view.detail.kind === kind
      && String(view.detail.id) === String(id == null ? '' : id);
  }

  /** 子智能体在**这一批**里的身份。后端给的行只有 `index` / `total` / `role`
   *  （`pentestd/chat.py` 的 `_subagent_rows`），没有全局 id，所以“第 3 个（共 3 个）”
   *  就是它的身份。换一批之后同一个序号是另一个人——换会话 / 新派活时必须清掉
   *  选中态，这就是 `detailKey` 里带上 `total` 的原因（总数的变化通常就是换了一批）。 */
  function subagentKeyOf(row) {
    if (!row) return '';
    return String(Number(row.index) || 0) + '/' + String(Number(row.total) || 0);
  }

  /** 这条任务是**谁**派出来的 —— 现算，不落库。
   *
   *  `jobs` 表里没有"下发者"这一列（`pentestd/store.py` 的 DDL），`create_job`
   *  的载荷里也没有。任务与子智能体之间唯一的联系，就在子智能体那条工具记录
   *  的 `tool.jobId` 上（由 `pentestd/subagent.py` 从派活工具的 `data.jobId`
   *  抄进工具记录）。所以这一格每次都是反查出来的——好处是刷新之后照样查得到，
   *  而且不用为了一个显示字段给 `jobs` 表加列。
   *
   *  找不到返回 `null`，**不做任何猜测**。两种"没有"都要保持安静：
   *   · 父轮自己直接调 `start_scan` 开出来的任务，本来就没有子智能体；
   *   · DSh 原生 subagent 那条路只拿得到文本 body（`pentestd/dsh_runtime.py`
   *     的 `_child_feed`），没有结构化的 jobId。
   *  这两种在界面上都是"这一段不出现"，比写一句"未知"要准确。 */
  function jobIssuer(jobId) {
    const id = String(jobId || '');
    if (!id) return null;
    const rows = currentSubagentRows();
    for (let i = 0; i < rows.length; i += 1) {
      const tools = asArray(rows[i].tools);
      for (let j = 0; j < tools.length; j += 1) {
        if (String(tools[j].jobId || '') === id) return { row: rows[i], tool: tools[j] };
      }
    }
    return null;
  }

  /** 把选中态落到那些行上（左栏的对话、右栏的任务与子智能体）。**只改类名，
   *  不重画**：重画会把滚动位置打回顶部，而“点第 8 条时跳回第 1 条”比没有高亮
   *  更烦人。 */
  function markDetailPicks() {
    const paint = (node, kind, id) => {
      if (!node || !node.classList) return;
      const value = id == null ? '' : String(id);
      if (!value) return;
      const hit = detailPicked(kind, value);
      node.classList.toggle('is-picked', hit);
      if (hit) node.setAttribute('aria-current', 'true');
      else node.removeAttribute('aria-current');
    };
    // 选中项已经不在这一批里（任务被清掉、换了一个会话）：高亮无处可画，
    // 但也不在这里篡改状态——展开面自己会说实话（它找不到那一行时会退回列表末尾，
    // 找不到那条记录时会在面里写「已经不在了」）。
    // 这里必须 `Array.from`：`asArray` 是 `Array.isArray` 的别名（给接口返回的
    // JSON 用的），HTMLCollection / NodeList 传进去得到的是空数组——高亮会静默
    // 地一条都不画，而所有断言“点了应该有 is-picked”的检查都会红成“没反应”。
    if (dom.goals) {
      Array.from(dom.goals.children).forEach(
        (node) => paint(node, 'goal', node.dataset && node.dataset.engagementId));
    }
    if (dom.sideJobs) {
      Array.from(dom.sideJobs.children).forEach(
        (node) => paint(node, 'job', node.dataset && node.dataset.jobId));
    }
    if (dom.sideSubagents) {
      Array.from(dom.sideSubagents.querySelectorAll('[data-subagent-index]')).forEach(
        (node) => paint(node, 'subagent', node.dataset.subagentKey));
    }
    // 主区里那些"下发了任务"的工具行（m14866）也带着这同一个选中态。它们不归某一
    // 个列表块，所以按类名整页扫；而它们与右栏那一条**说的是同一件事**，两处不
    // 同步的症状是"右边高亮在 A、左边高亮在 B"，读的人会以为点了两个东西。
    Array.from(document.querySelectorAll('.chat-trace-item[data-job-id]')).forEach(
      (node) => paint(node, 'job', node.dataset.jobId));
  }

  /** 拉开 / 收起这一栏。只动类名与 aria，不碰选中态——收起是“先不看”，
   *  不是“忘掉刚才看的是什么”（`view.detail` 留着，重新拉开还是同一条）。
   *
   *  收起时必须把展开面一起收掉：任务与子智能体那两块在右栏里（整栏 `display:
   *  none` 就跟着看不见了），但「目标」那块在**左栏**——不管它的话，右栏关了、
   *  左栏还摊着一份详情，而那颗 × 看起来什么也没做。 */
  function setDetailOpen(open) {
    view.detailOpen = Boolean(open);
    if (dom.detail) {
      dom.detail.classList.toggle('is-open', view.detailOpen);
      dom.detail.setAttribute('aria-hidden', view.detailOpen ? 'false' : 'true');
    }
    if (dom.detailHandle) dom.detailHandle.classList.toggle('is-folded', !view.detailOpen);
    renderDetail();
  }

  /** 点开一项。`autoOpen` 为真时顺带把栏拉开——收起状态下点任何一项都必须能
   *  看见它，否则收起就是一条单向路。`row` 是可选的行对象快照：列表刷新后先按
   *  id 去找活的，找不到才回退到它。 */
  function openDetail(kind, id, autoOpen, row) {
    if (!DETAIL_KIND[kind] || id == null || id === '') return;
    const same = view.detail.kind === kind && String(view.detail.id) === String(id);
    view.detail = { kind, id: String(id), row: row || (same ? view.detail.row : null) };
    view.detailSig = '';
    // `setDetailOpen` 自己会画一次，所以这条路上不重复画。
    if (autoOpen && !view.detailOpen) setDetailOpen(true);
    else renderDetail();
    markDetailPicks();
  }

  /** 真正清空选中：换会话、退出登录、新建会话时才用。**Esc 与 × 不走这里**。 */
  function closeDetail() {
    stopDetailLog();
    view.detail = { kind: '', id: '', row: null };
    view.detailSig = '';
    if (dom.detail) {
      dom.detail.classList.remove('is-open');
      dom.detail.setAttribute('aria-hidden', 'true');
    }
    view.detailOpen = false;
    renderDetail();
    markDetailPicks();
  }

  /* —— 重画与同步 —— */

  /** 展开面该挂在**哪一块**里。三种详情各归各的列表：任务归「任务」块、
   *  子智能体归「子智能体」块，目标是左栏那块「目标」。 */
  function detailHostOf(kind) {
    if (kind === 'goal') return dom.goals;
    if (kind === 'job') return dom.sideJobs;
    if (kind === 'subagent') return dom.sideSubagents;
    return null;
  }

  /** 这一块里“被点中那一行”的节点。找不到就是那一行已经不在这一块里了
   *  （任务掉出了「最近五条」，或换了目标）——那时详情挂在块的末尾，内容照给、
   *  栏头照写它叫什么，只是不再贴着某一行。 */
  function pickedNodeOf(host, kind) {
    if (!host) return null;
    const id = String(view.detail.id);
    if (kind === 'subagent') {
      return Array.from(host.querySelectorAll('[data-subagent-index]'))
        .find((node) => node.dataset && node.dataset.subagentKey === id) || null;
    }
    const key = kind === 'goal' ? 'engagementId' : 'jobId';
    return Array.from(host.children)
      .find((node) => node.dataset && node.dataset[key] === id) || null;
  }

  /** 把所有展开面从 DOM 里摘掉。**按类名整页扫**，不是只看 `view.detail` 现在
   *  指的那一块：换会话 / 换目标时上一个选中项的展开面还在原来那块列表里，只清
   *  当前那一块的话它会一直留在那儿，直到那一块被下一次重画清空。 */
  function clearInline() {
    Array.from(document.querySelectorAll('.chat-detail-inline')).forEach(
      (node) => node.remove());
  }

  /** 造一个空的展开面并挂到被点中那一行**后面**。行与它是**兄弟**：行是 `button`，
   *  按钮里不能套块级内容（读屏会把整段念成按钮名，点击也会被行的监听先吃掉）。
   *
   *  子智能体的行藏在 `.chat-subagents-list` 里，所以插在**行自己的父节点**里，
   *  不是插进 host——插进 host 会排到整个 `.chat-subagents` 包裹块外面，看起来
   *  像“详情跑到列表底下去”了。 */
  function mountInline(host, row, kind) {
    if (!host) return null;
    const panel = el('div', 'chat-detail-inline');
    panel.dataset.detailKind = kind;
    if (row && row.parentNode) row.parentNode.insertBefore(panel, row.nextSibling);
    else host.append(panel);
    return panel;
  }

  /** 重画展开面。**收起状态下不画**：整栏 `display: none`，画了也看不见，却每
   *  3 秒白烧一次 DOM。旧的那一份先摘掉——它是投影，不是增量。 */
  function renderDetail() {
    stopDetailLog();
    clearInline();
    const kind = view.detail.kind;
    if (!view.detailOpen || !kind) {
      if (dom.detailKind) dom.detailKind.textContent = '详情';
      if (dom.detailTitle) dom.detailTitle.textContent = '未选中';
      return;
    }
    const host = detailHostOf(kind);
    const panel = mountInline(host, pickedNodeOf(host, kind), kind);
    if (!panel) return;
    // 三方分派：每个渲染器只多认一个“画进哪个盒子”，内容逻辑一字未改。
    if (kind === 'job') renderJobDetail(panel);
    else if (kind === 'goal') renderGoalDetail(panel);
    else renderSubagentDetail(panel);
  }
  /** 数据一变就重画，**但只在真变时**。签名取的是“渲染用得到的字段”：
   *  整体 stringify 也能当签名，但 `toolOutput` 会有上万字节，每 3 秒比一次
   *  就是白烧 CPU；而只取这几格又不会漏掉任何会影响画面的东西。
   *
   *  签名之外还要看**展开面还在不在**：列表每重画一次，同一个容器里的展开面就跟着
   *  被 `clear()` 掉一次。只比签名的话，任务状态没变时这一块会**静默地空着**——
   *  数据是对的、高亮也在，就是没有内容。 */
  function syncDetail(kind) {
    const current = view.detail.kind;
    if (!current) return;
    if (kind && current !== kind) return;
    // 收起状态下不重画：重新拉开时 `setDetailOpen` 会走一遍 `renderDetail`，
    // 而这里每 3 秒去重建一屏看不见的 DOM 只是白费。
    if (!view.detailOpen) return;
    let next = null;
    if (current === 'job') next = jobById(view.detail.id);
    else if (current === 'goal') next = engagementMeta(view.detail.id);
    else next = currentSubagentRows().find(
      (row) => subagentKeyOf(row) === String(view.detail.id)) || null;
    if (!next) {
      // 不在了：把残留的展开面收掉，别让上一次的内容留在列表里冒充现状。
      clearInline();
      view.detailSig = '';
      return;
    }
    view.detail.row = next;
    const sig = JSON.stringify([current, detailSigOf(current, next)]);
    const mounted = pickedNodeOf(detailHostOf(current), current);
    if (sig === view.detailSig && mounted) return;
    renderDetail();
  }

  function detailSigOf(kind, row) {
    if (!row) return '';
    if (kind === 'job') {
      return [row.status, row.elapsedMs, row.claimedBy, row.finishedAt, row.error,
        row.chainError, row.written, row.summary, row.agents];
    }
    if (kind === 'goal') {
      return [row.target, row.objective, row.authorization, row.counts,
        row.updatedAt, goalJobs(row.id).length, goalScanTargets(row.id).length];
    }
    return row;
  }

  /* —— 小构件 —— */

  /** 详情里的一段「键 / 值」。值是空串 / null / undefined 时**整行不画**：
   *  一屏“目标：—、类型：—、错误：—”会把真正有值的那两行淹掉。 */
  function detailFacts(rows) {
    const dl = el('dl', 'chat-detail-facts');
    rows.forEach((pair) => {
      const value = pair[1];
      if (value === '' || value === null || value === undefined) return;
      const row = el('div', 'chat-detail-fact');
      row.append(el('dt', null, pair[0]));
      const dd = el('dd');
      if (typeof value === 'string' || typeof value === 'number') {
        dd.textContent = String(value);
      } else {
        dd.append(value);
      }
      row.append(dd);
      dl.append(row);
    });
    return dl;
  }

  function detailSection(title, node) {
    if (!node) return null;
    const box = el('section', 'chat-detail-section');
    box.append(el('h3', 'chat-detail-h', title));
    box.append(node);
    return box;
  }

  function detailPara(text, className) {
    if (!text) return null;
    return el('p', className || 'chat-detail-text', String(text));
  }

  /** `summary` 的标量键铺成一列。**不猜哪些值得看**：白名单会漏掉新增的键，
   *  而“结论里少了一格”没有任何提示。对象 / 数组折成一行 JSON（不递归展开，
   *  那是下面“原始结果”那一块的事）。 */
  function renderSummaryFields(summary) {
    const keys = Object.keys(summary || {});
    if (!keys.length) return el('p', 'muted', '这一步没有留下摘要。');
    const box = el('div', 'chat-detail-summary');
    let shown = 0;
    keys.forEach((key) => {
      const value = summary[key];
      if (value === null || value === undefined || value === '') return;
      const row = el('div', 'chat-detail-summary-row');
      row.append(el('span', 'chat-detail-key', key));
      const text = typeof value === 'object' ? JSON.stringify(value) : String(value);
      const node = el('span', 'chat-detail-value', text);
      node.title = text;
      row.append(node);
      box.append(row);
      shown += 1;
    });
    if (!shown) return el('p', 'muted', '这一步没有留下摘要。');
    return box;
  }

  /* —— 任务详情 —— */

  function jobById(id) {
    const jobs = ui().state ? ui().state.jobs : null;
    return asArray(jobs).find((job) => String(job.id) === String(id)) || null;
  }

  /** 一条任务的跳转行（状态徽标 + 种类 + 目标）。
   *
   *  与目标详情里那一串任务链接（`renderGoalDetail`）同一个写法：整行可点，不做成
   *  链接色——它不是"去另一页"，而是这一栏换个东西看。点下去交给 `focusJob()`，
   *  而不是直接 `openDetail()`：`focusJob` 会先把右栏「任务」块展开、在快照里找不到
   *  时补拉一次，这两件事少了任何一件，这一下就是"点了没反应"。
   *
   *  快照里没有这条任务时**不退化成纯文本**：只写作业号，照样可点——刚下发的任务
   *  在别处还没被拉进 `state.jobs`，而这恰恰是最该点得动的时候。 */
  function jobLink(jobId, hint) {
    const id = String(jobId || '');
    if (!id) return null;
    const job = jobById(id);
    const jobView = ui().jobView || {};
    const btn = el('button', 'chat-detail-link');
    btn.type = 'button';
    if (job && typeof jobView.statusOf === 'function') {
      const status = jobView.statusOf(job);
      btn.append(el('span', 'status-badge status-' + status,
        (jobView.label || {})[status] || ''));
      const kind = typeof jobView.kindLabel === 'function' ? jobView.kindLabel(job) : '';
      btn.append(el('span', 'chat-detail-link-text',
        (kind ? kind + ' · ' : '') + (job.target || job.id)));
    } else {
      btn.append(el('span', 'chat-detail-link-text',
        (hint ? hint + ' · ' : '') + id));
    }
    btn.addEventListener('click', () => { focusJob(id); });
    return btn;
  }

  /** 一条任务的完整展开面，画进 `host`（被点中那一行后面的 `.chat-detail-inline`）。
   *  以前没有参数、画进右栏底部那片正文；正文拿掉之后只多了这一个参数，
   *  其余每一行的内容与顺序都没动。 */
  function renderJobDetail(host) {
    const job = jobById(view.detail.id) || view.detail.row;
    const jobView = ui().jobView || {};
    if (dom.detailKind) dom.detailKind.textContent = DETAIL_KIND.job;
    if (dom.detailTitle) dom.detailTitle.textContent = job ? (job.target || job.id) : view.detail.id;
    if (!host) return;
    if (!job) {
      host.append(el('p', 'muted',
        '这条作业已经不在列表里了（可能被清空，或换了目标）。'));
      view.detailSig = '';
      return;
    }
    view.detail.row = job;
    view.detailSig = JSON.stringify(['job', detailSigOf('job', job)]);
    const status = jobView.statusOf ? jobView.statusOf(job) : (job.status || '');
    const finishedSet = jobView.finished || null;
    const finished = finishedSet ? finishedSet.has(status) : false;
    const head = el('div', 'chat-detail-row');
    head.append(el('span', 'status-badge status-' + status,
      (jobView.label && jobView.label[status]) || status));
    if (jobView.kindLabel) head.append(el('span', 'tag', jobView.kindLabel(job)));
    if (job.incremental) head.append(el('span', 'tag', '增量'));
    host.append(head);

    const written = job.written || {};
    const writtenText = Object.keys(written).length
      ? Object.keys(written).map((key) => `${(ui().kindLabel && ui().kindLabel(key)) || key} ${written[key]}`).join(' · ')
      : '';
    host.append(detailFacts([
      ['目标', job.target || '（无目标）'],
      ['作业号', job.id],
      ['下发', ui().formatTime(job.createdAt)],
      ['结束', job.finishedAt ? ui().formatTime(job.finishedAt) : ''],
      ['耗时', job.elapsedMs ? ui().formatDuration(job.elapsedMs) : ''],
      ['执行节点', job.claimedBy || '（尚未被领取）'],
      ['写入链路', writtenText],
    ]));

    // 规划出处：这条任务是从哪句人话里拆出来的。作业页签有这一行，详情里也要有——
    // 否则“这条任务是谁让它跑的”只有回到列表那条窄缝里才能看到。
    const payload = job.payload || {};
    if (payload.planId) {
      host.append(detailSection('下发依据', el('div', 'chat-detail-note',
        [payload.planEvidence ? `依据你的原话「${payload.planEvidence}」` : '',
          payload.planReason || '', `提案 ${payload.planId}`].filter(Boolean).join(' · '))));
    }

    const errorNode = job.error ? detailPara(job.error, 'chat-detail-bad') : null;
    const chainNode = job.chainError ? detailPara('写链路失败：' + job.chainError, 'chat-detail-bad') : null;
    if (errorNode) host.append(detailSection('错误', errorNode));
    if (chainNode) host.append(detailSection('链路', chainNode));

    host.append(detailSection('结论', renderSummaryFields(job.summary)));

    // 智能体协作：与作业页签同一份数据（`summary.agents` → `job.agents`），
    // 但排成“可以一直往下读”的段落，而不是列表里那根时间条。
    const agents = asArray(job.agents);
    if (agents.length) {
      const list = el('div', 'chat-detail-agents');
      agents.forEach((agent) => {
        const card = el('div', agent.state === 'idle' ? 'chat-detail-agent is-idle' : 'chat-detail-agent');
        const row = el('div', 'chat-detail-row');
        row.append(el('span', 'chat-detail-agent-name', agent.label || agent.role || '执行者'));
        if (Number(agent.actions) > 1) row.append(el('span', 'chat-detail-meta', `${agent.actions} 次动作`));
        row.append(el('span', 'spacer'));
        row.append(el('span', 'chat-detail-meta',
          agent.state === 'idle' ? '—' : ui().formatDuration(agent.elapsedMs || 0)));
        card.append(row);
        const line = agent.summary || agent.duty || '';
        if (line) card.append(el('p', 'chat-detail-text', line));
        list.append(card);
      });
      host.append(detailSection('智能体协作', list));
    }

    // 这条任务是**谁**派出来的。与上面「智能体协作」不是一回事：那一段说的是
    // 任务跑起来之后的执行者，这一段说的是派活的人。父轮自己直接开的任务没有
    // 这一段（`jobIssuer` 返回 null），界面上就是没有——不写"未知"。
    const issuer = jobIssuer(job.id);
    if (issuer) {
      const btn = el('button', 'chat-detail-link');
      btn.type = 'button';
      btn.append(el('span', 'status-badge status-' + (issuer.row.state || 'running'),
        SUBAGENT_STATE[issuer.row.state] || issuer.row.state || ''));
      btn.append(el('span', 'chat-detail-link-text',
        `${issuer.row.role || '子智能体'}（第 ${Number(issuer.row.index) || 0}/`
        + `${Number(issuer.row.total) || 0} 个）· 用 ${issuer.tool.label || '工具'} 开出`));
      btn.addEventListener('click', () => {
        openDetail('subagent', subagentKeyOf(issuer.row), true, issuer.row);
      });
      host.append(detailSection('派出它的子智能体', btn));
    }

    // 执行过程。**已结束的任务也要看得到**：那是“它到底一步步做了什么”的唯一
    // 原始记录，只给跑着的任务看等于把最该复盘的记录锁起来。
    startDetailLog(job, finished, host);

    if (Number(job.toolOutputBytes) > 0 && job.toolOutput) {
      const details = el('details', 'chat-detail-raw');
      details.append(el('summary', null,
        `原始输出（${job.toolOutputBytes} 字节）`));
      details.append(el('pre', 'chat-detail-pre', job.toolOutput));
      host.append(detailSection('原始输出', details));
    }
  }

  /* —— 任务执行过程（轮询增量） —— */

  function stopDetailLog() {
    const live = view.detailLog;
    view.detailLog = null;
    if (live && live.timer) window.clearInterval(live.timer);
  }

  function detailLogAppend(live, payload) {
    asArray(payload && payload.events).forEach((event) => {
      live.lines.push(String(event.message == null ? '' : event.message));
    });
    const lastId = Number(payload && payload.lastId);
    if (lastId > live.lastId) live.lastId = lastId;
    live.hasMore = Boolean(payload && payload.hasMore);
    live.dropped = Number(payload && payload.dropped) || 0;
    const finishedSet = (ui().jobView && ui().jobView.finished) || null;
    if (finishedSet && payload && payload.status && finishedSet.has(payload.status)) {
      live.finished = true;
    }
    if (live.lines.length > DETAIL_LOG_KEEP) {
      // 只留末尾：前面的行已经滚出可视区，留在 DOM 里只是让“打开一条跑了两小时的
      // nmap 任务”把标签页拖慢。滚掉的条数在头部写明，不会看起来像“日志就这么长”。
      live.hidden += live.lines.length - DETAIL_LOG_KEEP;
      live.lines = live.lines.slice(-DETAIL_LOG_KEEP);
    }
  }

  function detailLogPaint(live) {
    if (!live || !live.text) return;
    live.text.textContent = live.lines.join('\n');
    if (live.idle) live.idle.hidden = live.lines.length > 0;
    const parts = ['执行过程'];
    if (live.lines.length) parts.push(`${live.lines.length} 行`);
    else if (!live.finished) parts.push('正在等第一行…');
    if (live.hidden) parts.push(`上面还有 ${live.hidden} 行未显示`);
    if (live.dropped) parts.push(`服务端已滚掉 ${live.dropped} 行`);
    if (live.finished) parts.push('已结束');
    if (live.meta) live.meta.textContent = parts.join(' · ');
    // 贴底跟随：只有本来就在底部时才跟着滚。读历史的时候每 2 秒被拽回底部，
    // 会让“往回翻两行看看刚才那句”变成一件做不到的事。
    const box = live.text.parentElement;
    if (box && live.sticky !== false) box.scrollTop = box.scrollHeight;
  }

  /** 建执行过程那一块，并按任务是否结束决定要不要起轮询。
   *  已结束的任务只拉一次（可能分几页），不装定时器——它的日志不会再变。
   *  `host` 是被点中那一行后面的展开面：这块日志跟着详情走，不再固定挂在栏底。 */
  function startDetailLog(job, finished, host) {
    stopDetailLog();
    if (!job || !host) return;
    const box = el('div', 'chat-detail-log');
    const meta = el('div', 'chat-detail-log-meta');
    const text = el('pre', 'chat-detail-log-text');
    const idle = el('p', 'muted chat-detail-log-idle', '等待第一行输出…');
    box.append(meta, text, idle);
    host.append(box);
    const live = {
      jobId: job.id, text, meta, idle, lines: [], lastId: 0, hidden: 0,
      dropped: 0, hasMore: false, finished: Boolean(finished), busy: false,
      timer: null, sticky: true,
    };
    view.detailLog = live;
    box.addEventListener('scroll', () => {
      live.sticky = (box.scrollHeight - box.scrollTop - box.clientHeight) < 24;
    });
    const pull = async () => {
      if (view.detailLog !== live || live.busy) return;
      live.busy = true;
      try {
        // 一次最多补 8 页（2400 行）；剩下的交给下一拍，免得一次拉一万行
        // 把这一帧卡住。
        let guard = 0;
        do {
          const payload = await ui().apiGet(
            `/jobs/${encodeURIComponent(live.jobId)}/events`
            + `?after=${live.lastId}&limit=${DETAIL_LOG_PAGE}`);
          if (view.detailLog !== live) return;      // 换了一条 / 关掉了：这一页丢掉
          detailLogAppend(live, payload);
          guard += 1;
        } while (live.hasMore && guard < 8 && view.detailLog === live);
        detailLogPaint(live);
      } catch (error) {
        // 取不到不是“没有输出”：写一行原因，别让人对着空框以为任务没动静。
        if (live.meta) live.meta.textContent = '执行过程读取失败：' + error.message;
      } finally {
        live.busy = false;
      }
      if (view.detailLog !== live) return;
      if (live.finished && !live.hasMore && live.timer) {
        window.clearInterval(live.timer);
        live.timer = null;
        detailLogPaint(live);
      }
    };
    detailLogPaint(live);
    if (live.finished) {
      pull();
    } else {
      live.timer = window.setInterval(pull, DETAIL_LOG_INTERVAL);
      pull();
    }
  }

  /* —— 目标详情 —— */

  /** 一个目标的完整展开面。挂在**左栏「目标」块**里，紧跟被点中那一行——
   *  这块是"目标选择的唯一入口"，它的详情自然也该在这里，不在右栏。 */
  function renderGoalDetail(host) {
    const goal = engagementMeta(view.detail.id) || view.detail.row;
    if (dom.detailKind) dom.detailKind.textContent = DETAIL_KIND.goal;
    if (dom.detailTitle) dom.detailTitle.textContent = goal ? (goal.target || goal.id) : view.detail.id;
    if (!host) return;
    if (!goal) {
      host.append(el('p', 'muted', '这个目标已经不在了（可能被删掉）。'));
      view.detailSig = '';
      return;
    }
    view.detail.row = goal;
    view.detailSig = JSON.stringify(['goal', detailSigOf('goal', goal)]);
    const kindLabel = ui().kindLabel || ((kind) => kind);
    const counts = goal.counts || {};
    const nodes = ['intents', 'facts', 'findings', 'assets']
      .reduce((sum, key) => sum + (Number(counts[key]) || 0), 0);
    if (goal.id === selectedEid()) host.append(el('p', 'chat-detail-note',
      '这就是当前选中的目标——漏洞 / 资产 / 报告都按它算。'));
    host.append(detailFacts([
      ['目标号', goal.id],
      ['扫描目标', goal.target || '（未命名）'],
      ['目标说明', goal.objective || ''],
      ['授权', goal.authorization || '（未填，按全局范围限制判定）'],
      ['链路节点', `${nodes} 个（${kindLabel('intent')} ${Number(counts.intents) || 0} · `
        + `${kindLabel('fact')} ${Number(counts.facts) || 0} · `
        + `${kindLabel('finding')} ${Number(counts.findings) || 0} · `
        + `${kindLabel('asset')} ${Number(counts.assets) || 0}）`],
      ['创建', goal.createdAt ? ui().formatTime(goal.createdAt) : ''],
      ['更新', goal.updatedAt ? ui().formatTime(goal.updatedAt) : ''],
    ]));

    // 实扫过的地址。**不折成“主机（+N 个端口）”**：左栏那一行已经折过了，
    // 这里再折一次，详情就没有存在的意义了。
    const scans = goalScanTargets(goal.id);
    if (scans.length) {
      const list = el('div', 'chat-detail-list');
      scans.forEach((target) => list.append(el('div', 'chat-detail-li', target)));
      host.append(detailSection(`实扫过的地址（${scans.length}）`, list));
    }

    // 这个目标名下的任务。点一行先切到那条任务归属的目标，再就地展开它。
    const jobs = goalJobs(goal.id);
    if (jobs.length) {
      const jobView = ui().jobView;
      const list = el('div', 'chat-detail-list');
      jobs.forEach((job) => {
        const status = jobView.statusOf(job);
        const btn = el('button', 'chat-detail-link');
        btn.type = 'button';
        btn.append(el('span', 'status-badge status-' + status, jobView.label[status]));
        btn.append(el('span', 'chat-detail-link-text',
          `${jobView.kindLabel(job)} · ${job.target || job.id}`));
        btn.addEventListener('click', () => openDetail('job', job.id, true, job));
        list.append(btn);
      });
      host.append(detailSection(`作业（${jobs.length}）`, list));
    }

    // 这个目标带着的对话。点一行就打开它（不切目标：对话属于自己的范围）。
    const chats = asArray(view.sessions).filter(
      (session) => session.engagementId === goal.id);
    if (chats.length) {
      const list = el('div', 'chat-detail-list');
      chats.forEach((session) => {
        const btn = el('button', 'chat-detail-link');
        btn.type = 'button';
        btn.append(el('span', 'chat-detail-link-text', session.title || session.id));
        btn.addEventListener('click', () => { openSession(session.id); });
        list.append(btn);
      });
      host.append(detailSection(`对话（${chats.length}）`, list));
    }
  }

  /* —— 子智能体详情 —— */

  /** 一个子智能体的完整展开面，挂在「子智能体」块里紧跟被点中那一行。 */
  function renderSubagentDetail(host) {
    const row = view.detail.row;
    if (dom.detailKind) dom.detailKind.textContent = DETAIL_KIND.subagent;
    if (!host) return;
    if (!row) {
      if (dom.detailTitle) dom.detailTitle.textContent = '子智能体';
      host.append(el('p', 'muted',
        '这一批子智能体已经不在这一页上了（可能换了会话，或又派出了一批新的）。'));
      view.detailSig = '';
      return;
    }
    view.detailSig = JSON.stringify(['subagent', detailSigOf('subagent', row)]);
    const total = Number(row.total) || 0;
    const index = Number(row.index) || 0;
    if (dom.detailTitle) {
      dom.detailTitle.textContent = `${row.role || '子智能体'}（第 ${index}/${total} 个）`;
    }
    const head = el('div', 'chat-detail-row');
    const stateWord = SUBAGENT_STATE[row.state] || row.state || '';
    head.append(el('span', 'chat-detail-state' + (row.state === 'failed' ? ' is-bad' : ''),
      stateWord));
    if (row.policy) head.append(el('span', 'tag', row.policy));
    head.append(el('span', 'spacer'));
    if (row.durationMs) head.append(el('span', 'chat-detail-meta', ui().formatDuration(row.durationMs)));
    host.append(head);
    host.append(detailFacts([
      ['角色', row.role || ''],
      ['权限档', row.policy || ''],
      ['工具', `${Number(row.toolCount) || 0} 次`],
      ['步数', row.steps ? `${row.steps} 步` : ''],
    ]));
    const summary = detailPara(row.summary, 'chat-detail-text chat-detail-lead');
    if (summary) host.append(detailSection('带回来的结论', summary));
    if (row.error) host.append(detailSection('错误', detailPara(row.error, 'chat-detail-bad')));
    if (row.note) host.append(detailSection('备注', detailPara(row.note)));

    const tools = asArray(row.tools);
    if (tools.length) {
      const list = el('ol', 'chat-detail-steps');
      tools.forEach((tool) => {
        const item = el('li', tool.ok === false ? 'chat-detail-step is-bad' : 'chat-detail-step');
        const line = el('div', 'chat-detail-row');
        line.append(el('span', 'chat-detail-step-label', tool.label || '工具调用'));
        if (tool.durationMs) line.append(el('span', 'chat-detail-meta', ui().formatDuration(tool.durationMs)));
        item.append(line);
        // 这一步开了任务，就把那条任务摆出来。与目标详情里那条任务链接同一个写法
        // （`.chat-detail-link` + 状态徽标）：点它不是"去另一页"，而是这一栏换个东西看。
        if (tool.jobId) item.append(jobLink(tool.jobId, '下发的作业'));
        if (tool.brief) item.append(el('p', 'chat-detail-text', tool.brief));
        if (tool.ok === false && !tool.brief) item.append(el('p', 'chat-detail-bad', '这一次没有成功。'));
        list.append(item);
      });
      host.append(detailSection(`工具调用（${tools.length}）`, list));
    }
    host.append(el('p', 'muted chat-detail-foot',
      '主区那条消息下面也有一份同样的记录（同一个渲染器画的）。'));
  }

  /** 给 `subagentBlock()` 画出来的行补上“点开看详情”。
   *  **不另写渲染器**：`subagentNode` 仍由两处共用（契约测试钉着），可点只
   *  是在渲染后补的一层行为。`rows` 与节点按顺序一一对应。 */
  function wireSubagentPicks(block, rows) {
    if (!block || !rows) return;
    // `Array.from` 而不是 `asArray`：后者是 `Array.isArray` 的别名，NodeList 传进去
    // 得到空数组——那会让这一整块“看起来渲染了、点不动”，而且没有任何报错。
    const nodes = Array.from(block.querySelectorAll('.chat-subagent-row'));
    nodes.forEach((node, i) => {
      const row = asArray(rows)[i];
      if (!row) return;
      node.dataset.subagentIndex = String(Number(row.index) || i);
      node.dataset.subagentKey = subagentKeyOf(row);
      node.classList.add('is-pickable');
      if (node.tagName !== 'BUTTON') {
        node.setAttribute('role', 'button');
        node.setAttribute('tabindex', '0');
      }
      const open = () => openDetail('subagent', subagentKeyOf(row), true, row);
      node.addEventListener('click', open);
      node.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        open();
      });
    });
  }

  /* ------------------------------------------------------------ 消息渲染 ---- */

  /** 这条工具调用**下发了哪一条任务**（没有就是空串）。
   *
   *  后端 `_trace_entry`（`pentestd/chat.py`）把 `data` 落成了**截断后的 JSON
   *  字符串**，所以要 parse 一次；`arguments` 里也可能带 `jobId`，但那是"要它去查
   *  哪一条"，不是"这一次派出了哪一条"——两个都看，先看前者。
   *
   *  只认最外层那一格。往深处猜（`result.job`、`jobs[0]`）看着更聪明，失败形态却
   *  是"点了跳到另一条任务上"——而那看起来完全正常，只是详情写的是别人。
   */
  function traceJobId(item) {
    const pick = (value) => {
      let data = value;
      if (typeof data === 'string') {
        try { data = JSON.parse(data); } catch (err) { return ''; }
      }
      if (!data || typeof data !== 'object') return '';
      return String(data.jobId || '');
    };
    return pick(item && item.data) || pick(item && item.arguments);
  }

  /** 点会话里的一行工具轨迹 → 右栏那一条任务。
   *
   *  三件都要做，少一件这一下就是"点了没反应"：
   *   · 右栏的「任务」块可能折着（fold.js 存的是上次的口味），先展开；
   *   · 详情栏可能收着，`pickJob()` 里那条 `openDetail(..., true)` 会拉开它；
   *   · 那一行可能在滚动区外面，画完要 `scrollIntoView`——否则高亮落在了看不见的地方。
   *
   *  快照里找不到这条任务时**先补拉一次**：刚下发的任务、以及在别的页签里清过列表
   *  之后，`ui().state.jobs` 里都还没有它。补拉仍没有才走"这条任务已经不在了"那条路
   *  （`renderJobDetail` 会自己这么说）——不静默什么都不做。
   */
  async function focusJob(jobId) {
    const id = String(jobId || '');
    if (!id) return;
    if (window.PentestFold) window.PentestFold.set('jobs', false);
    let job = jobById(id);
    if (!job && typeof ui().loadJobs === 'function') {
      await ui().loadJobs(true, true);
      job = jobById(id);
    }
    if (job) {
      pickJob(job);
      const node = pickedNodeOf(dom.sideJobs, 'job');
      if (node && node.scrollIntoView) node.scrollIntoView({ block: 'nearest' });
      return;
    }
    openDetail('job', id, true, null);
  }

  function traceBlock(trace) {
    const items = asArray(trace);
    const box = el('details', 'chat-trace');
    const failed = items.filter((item) => !item.ok).length;
    const head = el('summary', 'chat-trace-head',
      `调用轨迹 · ${items.length} 次` + (failed ? `（失败 ${failed} 次）` : ''));
    box.append(head);
    const list = el('ol', 'chat-trace-list');
    items.forEach((item) => {
      const row = el('li', 'chat-trace-item' + (item.ok ? '' : ' bad'));
      // 这一行下发了任务就变成可点的（m14866）：点了右栏那一条就会展开。
      // 没有下发的那几次（读列表、查报告）保持不可点——把每一行都做成按钮，
      // 会让人以为"每一行都有个对应物"，而大多数行没有。
      const jobId = traceJobId(item);
      if (jobId) {
        row.classList.add('is-pickable');
        row.dataset.jobId = jobId;
        row.setAttribute('role', 'button');
        row.setAttribute('tabindex', '0');
        row.title = '在右侧打开这条作业';
        // `原始返回` 那个 <details> 自己也要能被点开：不拦的话，点开它同时把
        // 详情栏换成这条任务，两件事一起发生，看起来像"点折叠却跳走了"。
        row.addEventListener('click', (event) => {
          if (event.target && event.target.closest('details')) return;
          focusJob(jobId);
        });
        row.addEventListener('keydown', (event) => {
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          focusJob(jobId);
        });
      }
      const title = el('div', 'chat-trace-title');
      title.append(el('span', 'chat-trace-tool', item.label || item.tool || '工具'));
      title.append(el('span', 'chat-trace-time', `${item.durationMs || 0} ms`));
      row.append(title);
      row.append(el('p', 'chat-trace-args', JSON.stringify(item.arguments || {})));
      row.append(el('p', 'chat-trace-summary', item.summary || ''));
      if (item.data) {
        const detail = el('details', 'chat-trace-data');
        detail.append(el('summary', 'muted', '原始返回'));
        const pre = el('pre', 'chat-trace-pre');
        pre.textContent = String(item.data);
        detail.append(pre);
        row.append(detail);
      }
      list.append(row);
    });
    box.append(list);
    return box;
  }

  /**
   * 思考过程折叠块（DeepSeek 思考模式的 `reasoning_content`）。
   *
   * 默认折叠：它是模型的思考草稿，不是结论。放在正文**前面**（时间顺序如此，
   * 而且它解释了下面那段结论是怎么来的），但折叠状态下只占一行。
   * 用 `<details>` 而不是自己写开关：展开/收起是浏览器原生行为，还能被
   * 查找（Ctrl+F）命中，而自绘的"展开"会把那段文字对页面查找隐藏掉。
   *
   * **`open` 必须由调用方传进来，不能只靠 DOM。** 第一次画它的时候要接住
   * `view.live.reasoningOpen`（可能是上一轮接力过来的）；之后的重画**不再重建**这一块
   * （`syncReasoning` 原地追加文字），展开态与框内滚动都活在同一个节点上——所以第一次
   * 点开之后它就一直开着，直到使用者自己收起。toggle 事件由 `renderLive` 在造节点时接一次。
   * 已落库的那一条（`messageNode`）没有这个负担：它只画一次，展开状态由 DOM 自己管。
   */
  function reasoningBlock(reasoning, streaming, open) {
    const text = String(reasoning || '').trim();
    if (!text) return null;
    const box = el('details', 'chat-reasoning' + (streaming ? ' chat-reasoning-live' : ''));
    box.open = !!open;
    const head = el('summary', 'chat-reasoning-head', '思考过程');
    head.append(el('span', 'chat-reasoning-size', `${text.length} 字`));
    box.append(head);
    const pre = el('pre', 'chat-reasoning-text');
    pre.textContent = text;          // 思考原文不做 Markdown 渲染：它可能含未闭合的标记
    box.append(pre);
    return box;
  }

  /** 把**已有的**思考块更新到最新文字，不新建节点。
   *
   *  这是上一版留下的第二个坑。上一版把展开状态接出去了（点开不再被折回），但整块
   *  仍是每帧 `clear` 重建的：`<details>`、`<pre>`、字数标签每 ≤16ms 换一套新 DOM，
   *  文字从"被追加"变成"被整段替换"。表现就是「一直在刷新」——正在看的那一行、
   *  刚选中的文字、框内滚动位置每帧都留不住，流式反而读不出来。
   *
   *  所以这里只做增量：新文字以上一版为前缀时 **append 差值**，否则才整段替换
   *  （模型把思考改写重来时才会发生）。字数标签就地改，`open`、`scrollTop`、
   *  toggle 监听全部沿用同一份 DOM —— 一帧里变动的只有那段新增文字。
   */
  function syncReasoning(box, reasoning, streaming) {
    if (!box) return null;
    const text = String(reasoning || '').trim();
    const pre = box.querySelector('.chat-reasoning-text');
    if (!pre) return box;
    box.classList.toggle('chat-reasoning-live', !!streaming);
    const shown = pre.textContent || '';
    if (text !== shown) {
      if (text.indexOf(shown) === 0) {
        pre.append(document.createTextNode(text.slice(shown.length)));
      } else {
        pre.textContent = text;
      }
    }
    const size = box.querySelector('.chat-reasoning-size');
    if (size) {
      const label = `${text.length} 字`;
      if (size.textContent !== label) size.textContent = label;
    }
    return box;
  }

  /* ---------------------------------------------------------------- 选项 ----
   *
   *  模型问"要哪一个"时，把选项摆成一份**能勾的清单**：竖着排、可以多选，最后留一格
   *  「其他」——清单里没有的那件事，自己写。勾完点「其他」框右端那颗**发送图标**
   *  （或 Ctrl/⌘+Enter），那几句按顺序发出去。
   *
   *  **为什么要有这一段。** 没有它的时候，模型在正文里列几个选项，使用者得把其中
   *  一个的措辞敲回输入框——长选项要一字不差地抄，抄错一个字就是另一件事。约定一个
   *  固定形状（围栏 `options`）之后，界面能把它们变成勾选框，而勾完的回路**复用**
   *  正常发送：文字原样成为用户消息，走 sendMessage 的同一条路（带三项选择、开流、
   *  失败分类、收尾）。另开一条"选项专用发送"的路，必然有一天少带一个参数。
   *
   *  **竖排、可多选的理由**（t89）：横排一行那版是为"一个选项、一句话"设计的；而模型
   *  给的经常是"这几件里挑，也可以都做"。一条长句横着放不下，只能截断成省略号，还得
   *  横滚着读；竖排一行一件事、互相不抢宽度，勾几件都看得见。
   *
   *  **它是提示词的一部分**（见 pentestd/chat.py 的工作方式第 7 条）：不写进提示词，
   *  模型只会继续用列表；而写了却不在界面上认，形状就白约定了。两头各一处，缺一不可。
   */

  /** 从一段正文里切出 `options` 块。返回 `{ body, options }`。
   *
   *  - 只认围栏块（`` ```options ``），不认普通列表：正文里的列表到处都是，
   *    拿一个"看起来像选项"的启发式去猜，迟早会把一条说明性列表变成按钮。
   *  - 允许行首 `- ` / `* ` / `+ ` / `1. ` / `1、`：模型不一定会守格式，
   *    多认这几种前缀不会把正文误伤（它们只在围栏**里面**才有意义）。
   *  - 最多取 8 个：一行按钮放不下更多，而横着滚动的选项行没人会滚。
   *  - 没有选项时**原样返回**：宁可让那段围栏显示成代码，也不要吞掉正文。
   */
  const OPTIONS_FENCE = /^[ \t]*```[ \t]*options[ \t]*\r?\n([\s\S]*?)```/m;

  function splitOptions(text) {
    const raw = String(text || '');
    const match = raw.match(OPTIONS_FENCE);
    if (!match) return { body: raw, options: [] };
    const options = String(match[1] || '')
      .split(/\r?\n/)
      .map((line) => line.replace(/^[ \t]*(?:[-*+]|\d+[.)、])[ \t]*/, '').trim())
      .filter((line) => line && line !== '```');
    if (!options.length) return { body: raw, options: [] };
    const body = (raw.slice(0, match.index)
      + raw.slice(match.index + match[0].length)).trim();
    return { body, options: options.slice(0, 8) };
  }

  /** 现在该不该有选项行——有的话是哪几个。
   *
   *  只认**最后一条**消息，而且必须是助手说的：
   *    - 使用者已经接话（最后一条是 user）→ 那批选项已经用过，收起来；
   *    - 这一轮还在跑（sending）→ 点不了，摆着只会让人点空；
   *    - 最后一条是 error / interrupted → 它没有"请选择"的意思。
   *  工具结果（role=tool）跳过不算"最后一条"：它们不改变"谁在等谁回答"。
   */
  function pendingOptions() {
    if (view.sending) return [];
    for (let i = view.messages.length - 1; i >= 0; i -= 1) {
      const message = view.messages[i];
      if (message.role === 'tool') continue;
      if (message.role !== 'assistant') return [];
      if (message.error || message.interrupted) return [];
      return splitOptions(message.text).options;
    }
    return [];
  }

  /** 面板上勾着的选项（按选项原文）与「其他」里手写的那几行 —— 这次要发的事。
   *
   *  顺序固定：先按清单原顺序，再按手写行的顺序。顺序要稳——同一批勾选两次发出去的
   *  话不一样，模型看到的就不是"照做"，而是"又变了"，会重新决策。
   */
  function choiceTasks(options) {
    const picked = view.choicePicked || {};
    const tasks = options.filter((text) => picked[text]);
    String(view.choiceOther || '').split(/\r?\n/).forEach((line) => {
      const text = line.trim();
      if (text) tasks.push(text);
    });
    return tasks;
  }

  /** 勾中的几件事 → 要发出去的那一句话。
   *
   *  **只勾了一件时逐字发那一句**：与 t89 之前"点一个就走"发出去的完全同一条消息，
   *  模型那边看不出区别，使用者也不用学新格式。多件时排成编号列表——模型收到的仍是
   *  同一批选项原文，编号只告诉它"这几件都要做"，而不是"从中挑一件"。
   */
  function choiceMessage(tasks) {
    if (tasks.length === 1) return tasks[0];
    return '继续这几件事：\n'
      + tasks.map((text, i) => (i + 1) + '. ' + text).join('\n');
  }

  /** 这批选项的"指纹"。变了才重建——重建会把勾选、焦点、手写内容一起丢掉。 */
  function choiceSignature(options) {
    return options.join('\n');
  }

  /** 刷新面板头部的读数（「已选 N 件」）。勾一下、写一个字都会走到这里。
   *
   *  ⚠️ t99：这一格**不再有**发送键（用户：「不再使用任务选择框内的发送，去掉这个
   *  按钮，统一使用输入框中的发送」）。勾选状态影响的不再是"面板里那颗键发不发"，而是
   *  输入框那一颗**叫什么**——所以这里收完读数就把名字交出去（见 `syncSendName()`）。
   */
  function syncChoice(options) {
    const tasks = choiceTasks(options);
    const count = dom.choice.querySelector('.chat-choice-count');
    if (count) count.textContent = tasks.length ? '已选 ' + tasks.length + ' 件' : '一件都没选';
    syncSendName();
  }

  /** 输入框那颗发送键的名字。
   *
   *  它有三种身份：空闲＝「发送」，跑着＝「停止」，面板上有待发的一批＝「发送所选（N 件）」。
   *  ⚠️ t99 之后这件事只有它一家要管：面板里那颗键已经删掉，页面上就剩这一颗圆钮，
   *  而"按下去会发出去几件"除了图标只有 `aria-label` / `title` 说得出——念着「发送」却发出
   *  三件、或念着「停止」而不说话，都是点错按钮。
   */
  function syncSendName() {
    if (!dom.send) return;
    let label = '发送';
    if (view.sending) label = '停止';
    else {
      const tasks = choiceTasks(pendingOptions());
      if (tasks.length) label = '发送所选（' + tasks.length + ' 件）';
    }
    dom.send.setAttribute('aria-label', label);
    dom.send.title = label;
  }

  /** 造一遍面板。只在选项集合变了（或它第一次出现）时调用。 */
  function buildChoice(options) {
    clear(dom.choice);
    const picked = (view.choicePicked = {});
    const head = el('div', 'chat-choice-head');
    head.append(el('span', 'chat-choice-ask', '选一件或几件继续：'));
    head.append(el('span', 'chat-choice-count'));
    dom.choice.append(head);

    const list = el('ul', 'chat-choice-list');
    options.forEach((text) => {
      const item = el('li', 'chat-choice-item');
      const label = el('label', 'chat-choice-label');
      const box = el('input', 'chat-choice-box');
      box.type = 'checkbox';
      // 整行都能点：`label` 会把点击转发给框里的 input，不必给文字单独挂事件。
      label.append(box, el('span', 'chat-choice-text', text));
      box.addEventListener('change', () => {
        if (box.checked) picked[text] = true; else delete picked[text];
        item.classList.toggle('on', box.checked);
        syncChoice(options);
      });
      item.append(label);
      list.append(item);
    });
    dom.choice.append(list);

    // 「其他」：清单里没有的那件事，自己写。一行一件，与勾中的选项一起发。
    const other = el('div', 'chat-choice-other');
    const otherBox = el('input', 'chat-choice-box');
    otherBox.type = 'checkbox';
    const otherLabel = el('label', 'chat-choice-label');
    otherLabel.append(otherBox,
      el('span', 'chat-choice-text', '其他：自己写一件事（一行一件）'));
    const input = el('textarea', 'chat-choice-other-input');
    // 一行（t95 用户要求）：两行的空框把"写一句话"的入口画成了"写一段话"，而这里只是
    // 清单外补一件的地方。默认一行高，写长了由 CSS 的 `field-sizing` 自己长（见 style.css）。
    input.rows = 1;
    input.placeholder = '例如：换一个端口再扫一遍';
    input.value = view.choiceOther || '';
    // ⚠️ t99（用户要求「不再使用任务选择框内的发送，去掉这个按钮，统一使用输入框中的
    // 发送」）：这一格**没有自己的发送键**。整批（勾中的几件 + 这里手写的几行）由输入框
    // 那一颗承担——`sendPrimary()` 先看有没有勾选，有就走 `sendChoice()`；名字由
    // `syncSendName()` 写成「发送所选（N 件）」，所以"按一下会发出去几件"仍然问得出来。
    // t95 曾在框右侧并一颗 `.chat-send`、t96 去掉它的变体类名；这两版都还留着**第二颗**
    // 按钮，只是外观被对齐了。用户现在要的是"没有第二颗"——删掉，不留只在面板里出现的孪生。
    // 「其他」这一格于是只剩两件：勾标一行、一个一行高的框（框自己占满整宽）。
    const markOther = () => {
      const has = !!input.value.trim();
      otherBox.checked = has;
      other.classList.toggle('on', has);
      syncChoice(options);
    };
    otherBox.addEventListener('change', () => {
      // 取消勾 = 把那几行删掉；勾上（或点头一次）只是把光标送进框里，不算一件事。
      if (!otherBox.checked && input.value.trim()) {
        input.value = '';
        view.choiceOther = '';
      }
      input.focus();
      markOther();
    });
    input.addEventListener('input', () => {
      view.choiceOther = input.value;
      markOther();
    });
    // 框自己占满这一格的宽度（t99）：右侧不再有键，也就不必给谁让出 34px 的行宽。
    other.append(otherLabel, input);
    dom.choice.append(other);

    markOther();
  }

  /** 把待选的选项画进卡片（或收起）。
   *
   *  选项集合没变时**不重建**：`renderLog()` 在一场对话里会被反复调用（收尾、切会话、
   *  队列变化），每调一次就 `clear` 一遍，勾到一半的勾、写到一半的字、正在闪的光标
   *  全没了。
   */
  function renderChoice() {
    if (!dom.choice) return;
    const options = pendingOptions();
    if (!options.length) {
      if (!dom.choice.hidden) {
        clear(dom.choice);
        dom.choice.hidden = true;
        view.choiceSig = '';
        view.choicePicked = {};
        view.choiceOther = '';
      }
      // 面板收起 = 「发送所选（N 件）」不再成立，那颗键要叫回「发送」。放在这一层而不是
      // 内层 if 里：名字是**渲染结果**，任何一条走到这里的路径都该重算一次（否则发完这批
      // 之后它还挂着件数，直到下一次 setSending 才改口）。
      syncSendName();
      return;
    }
    const signature = choiceSignature(options);
    if (dom.choice.hidden || signature !== view.choiceSig) {
      view.choiceSig = signature;
      view.choicePicked = {};
      dom.choice.hidden = false;
      buildChoice(options);
      return;
    }
    syncChoice(options);
  }

  /** 把勾中的几件事一次发出去。
   *
   *  与 `send()` 的区别只有一处：不从输入框取值，所以不清框、不动焦点——使用者可能
   *  正在框里写别的，勾选项这件事不该把那半句话删掉。其余（模型闸、失败计数、取消
   *  待发的自动执行续跑）逐条照 `send()`，因为"人自己发"的语义完全一样。
   */
  function sendChoice() {
    if (view.sending) return;
    const tasks = choiceTasks(pendingOptions());
    if (!tasks.length) return;
    if (!canSend()) {
      ui().showError('未启用模型：会话需要模型来决策工具调用，请先到「设置」页签配置'
        + '一条，或在对话框上方的「模型」里选一条已有的。');
      return;
    }
    // 这一批选项用掉了：先清状态再发，面板随即收起，下一轮不会以"还勾着上次那几件"
    // 的样子回来。
    view.choiceSig = '';
    view.choicePicked = {};
    view.choiceOther = '';
    auto.attempts = 0;
    auto.failed = false;
    cancelAutoContinue();
    sendMessage(choiceMessage(tasks));
  }

  /**
   * 「这一轮没有正文」那句话。落库的消息带了 `finishNote`（服务端按 `finish_reason`
   * 写的）时就用它——"预算被思考吃光"和"模型只想不说"是两件事：前者改配置就好，
   * 后者只能再问一次。用同一句话盖掉，就没人知道该动哪里。
   */
  function emptyTurnNote(message) {
    const note = (message && message.finishNote) || '';
    return note ? '这一轮没有产出正文：' + note + '。'
                : '这一轮没有产出正文（模型只做了思考或工具调用）。';
  }

  function messageNode(message) {
    const mine = message.role === 'user';
    const wrap = el('article', 'chat-msg '
      + (mine ? 'chat-msg-user' : 'chat-msg-ai')
      + (message.error ? ' chat-msg-error' : ''));
    const head = el('header', 'chat-msg-head');
    head.append(el('span', 'chat-msg-role',
      ROLE_LABEL[message.role] || message.role));
    if (message.at) head.append(el('span', 'chat-msg-at', ui().formatClock(message.at)));
    wrap.append(head);
    if (!mine) {
      const reasoning = reasoningBlock(message.reasoning, false);
      if (reasoning) wrap.append(reasoning);
    }
    const body = el('div', 'chat-msg-body');
    // 轨迹挂了没有，正文与下面"见下方"两处都要用。
    const hasTrace = asArray(message.trace).length;
    if (message.error) {
      body.append(el('p', 'chat-msg-error-text', '这一轮没有跑完：' + message.error));
    } else if (message.interrupted) {
      // 中途被停止的那一轮：说清"到哪儿为止"，而不是留一个空气泡让人猜。
      // "见下方"只有在**确实有轨迹**时才说得出口：没有轨迹时这句指向一片
      // 空白，读者会以为自己漏看了什么。
      body.append(el('p', 'chat-typing',
        '这一轮已中止（连接断开），没有继续执行。'
        + (hasTrace ? '已完成的工具调用见下方。' : '')));
    } else if (mine) {
      body.append(el('p', 'chat-msg-text', message.text || ''));
    } else if (message.text) {
      const parts = splitOptions(message.text);
      if (parts.options.length) {
        // 选项块**搬到输入框里**了（renderChoice）：气泡里再印一遍，同一个问题就有
        // 两份，而且文字版的那份看着像"要你自己抄"。正文只留"为什么问"。
        // 正文被切空时给一句指路——空泡泡看起来像这一轮答空了。
        body.append(parts.body
          ? ui().renderMarkdown(parts.body)
          : el('p', 'muted', '这一轮只给了选项：在下方输入框里点一个就开始。'));
      } else {
        body.append(ui().renderMarkdown(message.text));
      }
    } else {
      // 模型这一轮一个字正文都没写（常见于"想完 / 调完工具就停了"）。
      // 气泡不能**空着**：空的看起来像页面还没画完，而这条已经落库、不会再变。
      // 也不能再说"（模型没有返回文本）"：那句话只说了"没东西"，没说"为什么"，
      // 读者会以为是平台把回答弄丢了。
      body.append(el('p', 'muted', emptyTurnNote(message)));
    }
    wrap.append(body);
    if (hasTrace) wrap.append(traceBlock(message.trace));
    if (asArray(message.subagents).length) {
      // 派活区块也要能刷新后重看——它是"这一轮做过什么"的另一半，和调用轨迹
      // 同一个性质。只活在流里的话，用户刷新一次就再也看不到那几个子智能体
      // 各自带回了什么，而结论里往往只写着"已派出 3 个子智能体并汇总完毕"：
      // **结论留下来了，依据没了**。服务端落库的形状与流里的行**逐键一致**，
      // 所以这里直接复用同一个渲染器，不另写一份。
      // 主区这一份也点得开：与侧栏共用同一份行对象、同一个展开机制——两处的
      // “点开看”因此不会给出两个版本（那正是当初删掉侧栏那一块的旧理由）。
      const rows = asArray(message.subagents);
      const block = subagentBlock(rows);
      wireSubagentPicks(block, rows);
      wrap.append(block);
    }
    return wrap;
  }

  /** 一次往回翻多少条。与后端 `chat_session_detail` 的页大小一致。 */
  const MESSAGE_PAGE = 400;

  /** 这一条要不要出现在记录里。
   *
   *  工具结果已在轨迹里；"只为携带工具调用"的那条 assistant 也不渲染：它本来
   *  就没有正文，画出来就是一句"这一轮没有产出正文"，而它真正想表达的东西（调了
   *  哪个工具、成没成）在下一条消息的「调用轨迹」里。留着只会让人以为那一轮答空了。
   *
   *  **中止的那条例外**：它是这一轮最后落库的一条（`chat.py` 的
   *  `tail("", interrupted=True)`），轨迹挂在它身上。跳过它就等于"发生了什么、
   *  到哪儿停下"一个字都不剩，只剩一个空气泡。
   *
   *  收在这里是因为「加载更早」把旧消息插到顶上时要用**同一份**判据——两处各写
   *  一遍，早晚会分叉。
   */
  function messageVisible(message) {
    if (message.role === 'tool') return false;
    return !(message.role === 'assistant' && !String(message.text || '').trim()
      && asArray(message.toolCalls).length && !message.error
      && !message.interrupted);
  }

  /** 收下服务端这份窗口，但**别把已经翻出来的旧消息丢掉**。
   *
   *  `openSession` 与心跳刷新拿到的都是"尾巴那一页"；使用者如果已经点过
   *  「加载更早」，手上这一段比服务端的窗口长。直接赋值会把它截掉——看起来就像
   *  刚翻上去的几百条"自己又没了"。只在两段**首尾相接**时才拼（旧段最后一条
   *  `seq + 1` 等于新窗口第一条）：中间有空洞说明这份旧段已经不完整，留着比丢掉
   *  更容易误导。
   */
  function adoptMessages(fresh, earlierCount) {
    const list = asArray(fresh);
    const count = Math.max(0, Number(earlierCount) || 0);
    const first = list.length ? Number(list[0].seq) : 0;
    const keep = view.messages.filter((message) => Number(message.seq) < first);
    if (keep.length && Number(keep[keep.length - 1].seq) + 1 === first) {
      view.messages = keep.concat(list);
      view.earlierCount = Math.max(0, count - keep.length);
      return;
    }
    view.messages = list;
    view.earlierCount = count;
  }

  function earlierButtonLabel() {
    return '加载更早的 ' + Math.min(MESSAGE_PAGE, view.earlierCount) + ' 条';
  }

  /** 记录顶部那一行：上面还有多少条，以及"去拿"的入口。
   *
   *  只说不给按钮，等于告诉使用者"东西丢了"；而这条对话在库里是完整的，
   *  往回翻一次就能看见（见 `loadEarlier`）。
   */
  function earlierHint() {
    const row = el('div', 'chat-earlier');
    row.append(el('span', 'chat-earlier-text',
      '更早的 ' + view.earlierCount + ' 条没有载入'));
    const button = el('button', 'chat-earlier-btn',
      view.earlierLoading ? '正在加载…' : earlierButtonLabel());
    button.type = 'button';
    // 两个"不用瞄准按钮"的入口写在 `title` 上：不占版面，而不写就没人知道
    // 滚到顶会自动取。
    button.title = 'Alt+↑ 也能加载';
    row.title = '滚到最上面会自动取一页；Alt+↑ 也可以';
    button.disabled = !!view.earlierLoading;
    button.addEventListener('click', loadEarlier);
    row.append(button);
    return row;
  }

  /** 滚到顶上就自动取一页：往回读到底的人，下一步一定是"再要一页"。
   *
   *  三道限制都不是装饰：
   *  - 只有**使用者自己滚过**（`view.logTouched`）才允许。`renderLog` 一画完就
   *    贴到底，若记录比视口短，`scrollTop` 也是 0——不设这道门，打开长对话会
   *    自己连翻好几页。
   *  - 内容得真的长过一屏，"在顶上"才说明位置；否则那只是"没有可滚的东西"。
   *  - 每插一页都会把 `scrollTop` 顶高同样多（见 `loadEarlier` 末尾），此后
   *    就不在顶上了。所以不需要节流：链式触发自己停住，不会一口气翻完。
   */
  function earlierAutoLoad() {
    if (!view.logTouched || view.earlierLoading) return;
    if (!view.earlierCount || !view.sessionId) return;
    const box = dom.log;
    if (!box || box.scrollTop > 2) return;
    if (box.scrollHeight <= box.clientHeight + 4) return;
    loadEarlier({ auto: true });
  }

  /** 点击「加载更早」：拿游标前面那一页，**插到顶上**而不是整段重画。
   *
   *  为什么不能调 `renderLog`：它会滚到底，也会重建正在读的那些节点（思考块
   *  的展开态、选中的文字全丢）。所以这里自己拼 DOM，并共用 `messageVisible` /
   *  `messageNode`。
   */
  async function loadEarlier(options) {
    // 点击传进来的是事件对象（没有 `auto`），所以只认显式的 `true`。
    const auto = Boolean(options && options.auto === true);
    if (view.earlierLoading || !view.sessionId) return;
    const first = view.messages[0];
    if (!first || !first.seq) return;
    view.earlierLoading = true;
    const button = dom.log && dom.log.querySelector('.chat-earlier-btn');
    if (button) { button.disabled = true; button.textContent = '正在加载…'; }
    try {
      const payload = await ui().apiGet(`/chat/sessions/${encodeURIComponent(view.sessionId)}`
        + `?before=${encodeURIComponent(first.seq)}`);
      // 请求飞在路上时换了会话 → 这份是别人的，丢掉（与心跳刷新同一条）。
      if (!payload || !payload.session || payload.session.id !== view.sessionId) return;
      view.earlierLoading = false;
      const older = asArray(payload.messages);
      const row = dom.log && dom.log.querySelector('.chat-earlier');
      if (!older.length) {
        // 服务端说没有了：把这一行摘掉。留着就是一句永远点不动的提示。
        view.earlierCount = 0;
        if (row) row.remove();
        return;
      }
      const box = dom.log;
      const keepTop = box ? box.scrollTop : 0;
      const oldHeight = box ? box.scrollHeight : 0;
      view.messages = older.concat(view.messages);
      view.earlierCount = Math.max(0, Number(payload.earlierCount) || 0);
      const fragment = document.createDocumentFragment();
      older.forEach((message) => {
        if (messageVisible(message)) fragment.append(messageNode(message));
      });
      if (row) box.insertBefore(fragment, row.nextSibling);
      else box.insertBefore(fragment, box.firstChild);
      if (row) {
        if (view.earlierCount > 0) row.replaceWith(earlierHint());
        else row.remove();
      }
      // 内容长高了，滚动位置跟着挪同样多：否则"往上翻"会把人弹回底部。
      if (box) box.scrollTop = keepTop + (box.scrollHeight - oldHeight);
    } catch (error) {
      view.earlierLoading = false;
      // 自动取失败一次就退回手动：停在顶上会一秒一个请求，而使用者根本没
      // 按过任何东西。静默是刻意的——那一次是"顺手"，弹窗会打断正在读的
      // 一段；但按钮还在，按下去仍然会给出原因。
      if (auto) view.logTouched = false;
      if (!auto) ui().showError('更早的对话加载失败：' + describeError(error));
      const fallback = dom.log && dom.log.querySelector('.chat-earlier-btn');
      if (fallback) {
        fallback.disabled = false;
        fallback.textContent = earlierButtonLabel();
      }
    }
  }

  function renderLog() {
    if (!dom.log) return;
    clear(dom.log);
    // 这一趟画完会贴到底（见函数末尾），所以"使用者自己滚过"归零：不归零
    // 的话，切会话或一轮收尾之后的第一帧就可能被判成"他滚过"。
    view.logTouched = false;
    // 收尾接力：上一轮收尾时思考块是展开着的，这一条记录里要把它接上（见文末那段）。
    // 标记**在这里就消费掉**：下面两个早退（空态、仍在生成）都不该把一次展开留到
    // 下一次重画——那会在切会话之后把别人的思考块打开。
    const carryReasoning = view.carryReasoningOpen;
    view.carryReasoningOpen = false;
    // 侧栏那一块跟着走。放在**空态那个早退之前**：本会话一条消息都没有时，
    // 侧栏也必须是空态——早退之后再调，上一场会话留下的子智能体行会一直挂在那里，
    // 而它看起来完全正常（有角色、有步数、有结论），只是属于别人。
    renderSideSubagents();
    if (!view.messages.length) {
      const empty = el('div', 'chat-welcome');
      empty.append(el('h2', 'chat-welcome-title', '用一句话下达任务'));
      empty.append(el('p', 'muted',
        '对话由模型驱动：它会先查现状（节点、任务、历史结果），再决定是否下发扫描，'
        + '每次调用都记在「调用轨迹」里。扫描只在有授权的目标上发起。'));
      dom.log.append(empty);
      renderReviews();
      dom.log.scrollTop = dom.log.scrollHeight;
      return;
    }
    // 长对话只送回最近 400 条（见后端 `chat_messages_tail`）。页顶这一行说的是
    // "上面还有"，并且给一个**取回来**的入口——只说"丢了"而不给路，等于把一句
    // 实话变成一句没法处理的坏消息。用户报的"我发的消息自己消失了"，一半就是
    // 它：老的滑出窗口，而没有任何一处说"还有"。
    if (view.earlierCount > 0) dom.log.append(earlierHint());
    view.messages.forEach((message) => {
      // 判据收在 `messageVisible` 里（含"中止的那条例外"与理由）；
      // 「加载更早」插到顶上时用的是同一份。
      if (!messageVisible(message)) return;
      dom.log.append(messageNode(message));
    });
    // 进行中的那一轮画成"正在生成"的气泡，由流式事件驱动（见 renderLive）。
    // 它和已落库的消息**不重叠**：服务端只在本轮结束时才把消息算作已定稿。
    // 只在**正看着这一轮所属的会话**时才画：流式是后台在跑，切到别的对话去看
    // 记录是正常操作，此时把正在生成的气泡挂到别人的窗口里就是张冠李戴。
    if (view.sending && view.live
        && (!view.live.sessionId || view.live.sessionId === view.sessionId)) {
      renderLive();
    }
    // 上一轮收尾时思考块是展开着的：这一刻记录里已经换成落库的那一条，把它接上。
    // 两重限定缺一不可：**最后一条消息**得是带思考的助手消息（否则会打开更早那一轮的
    // 思考块），而它渲染出来的**最后一块**才是这一轮的。标记在上面已经消费掉，
    // 所以这一段只负责"这一次要不要开"。
    if (carryReasoning) {
      const lastMessage = view.messages[view.messages.length - 1];
      const blocks = dom.log.querySelectorAll('.chat-reasoning');
      if (lastMessage && lastMessage.reasoning && blocks.length) {
        blocks[blocks.length - 1].open = true;
      }
    }
    // 审查卡跟着记录一起画：它现在是**记录的一部分**（t80），而不是输入框上方
    // 单独的一格。刷新、切会话、SSE 的 `settled` 帧都会走回这里，所以卡片的位置
    // 与"这一轮跑到哪儿了"始终一致。
    renderReviews();
    // 选项行跟着记录一起重算：它是"最后一条消息在不在等回话"的投影，和审查卡
    // 同一个性质（都是服务端事实）。放在最后：它读的是 `view.messages` 的末尾。
    renderChoice();
    dom.log.scrollTop = dom.log.scrollHeight;
  }

  function renderMeta() {
    if (!dom.meta) return;
    // 失败时也要说。不说的话这一行会一直停在「正在加载…」上——那是一句会一直挂着的
    // 假话，而且它与侧边栏那句「对话列表加载失败」不矛盾、只是把它盖住了。
    if (view.sessionsError) { dom.meta.textContent = '对话列表加载失败'; return; }
    const parts = [];
    if (view.loaded) parts.push(`对话 ${view.sessions.length} 条`);
    // 「可用能力 M 项 · 技能手册 K 份」两个计数**搬到了坞里的按钮上**：计数写在它
    // 统计的那份目录的入口上更好找；而留在页头就等于同一件事在两处显示，数字对
    // 不上时没人知道该信哪一份。这里只留"有多少条对话"——对话列表在侧边栏，
    // 页头这一行是它唯一的计数。
    dom.meta.textContent = parts.length ? parts.join(' · ') : '正在加载…';
  }

  /* ------------------------------------------------------------ 打开会话 ---- */

  async function openSession(sessionId) {
    if (!sessionId) return;
    view.everOpened = true;
    view.sessionId = sessionId;
    // 切回来时先把**这个会话那一轮**挂回屏上（如果它还在跑）：不挂的话，接下来那次
    // `attachTurn` 会因为 `runOf` 已经命中而跳过，界面上就一个气泡都没有。
    adoptRun(sessionId);
    // 点开一条对话 = "就看这条"。清掉手工标记与去重记录，下面 `adoptSessionEngagement()`
    // 才能把范围跟到它归属的目标（否则会被上一次手工换的范围、或旧的去重记录挡住）。
    view.manualGoal = false;
    view.scopeSync = null;
    // 明确点开一条对话 = 这一下由它决定看哪条：上一条被"一轮在跑"挡住的目标切换到此
    // 作废（否则收尾时会把刚点开的这条又换走）。
    view.pendingGoalSwitch = '';
    renderSessions();
    try {
      const payload = await ui().apiGet(`/chat/sessions/${encodeURIComponent(sessionId)}`);
      adoptMessages(payload.messages, payload.earlierCount);
      // 服务端给出的 config 是**这一轮实际生效**的三项，回填到下拉上。
      // 不回填就会出现"切到另一个会话，下拉还显示上一个会话的选择"。
      view.config = payload.config || null;
    } catch (error) {
      view.messages = [];
      view.earlierCount = 0;
      view.config = null;
      ui().showError('对话内容加载失败：' + describeError(error));
    }
    // 换了会话，**子智能体那一份选中态必须清掉**：它是“最近一次派活”里的第 3 个
    // 兄弟，换一场之后那个“第 3 个”是另一个人。任务与目标按 id 存，id 不跨会话
    // 重号，所以留着重画仍是同一条。
    if (view.detail.kind === 'subagent') closeDetail();
    renderEntry();
    renderLog();
    // 审查单**必须**在这里取回来：刷新页面之后它是卡片唯一的来源（SSE 那条流
    // 已经随页面一起断了，而它以前是卡片的唯一来源）。排在消息与正文之后 ——
    // 它不该拖慢正文的显示。
    await loadReviews();
    adoptSessionEngagement();
    // 右栏那两块只列**这条对话的**任务与派活，所以要在这里重画一次。
    // 不能只靠 `adoptSessionEngagement` 里那两句：它在"这条对话的目标就是当前目标"
    // 时第一句就 return 了，而那正是最常见的情形——切到同一目标下的另一条对话时，
    // 任务块会停在上一条对话的任务上，看起来完全正常。
    renderSideJobs();
    renderSideSubagents();
    // 最后一件事：如果服务端还有一轮在跑，把它接回来。放在渲染都做完之后——
    // 接上要先把已落库的那几条收下，再拿重放补上“正在写的这一段”。
    await attachTurn(sessionId);
    // 排队那块也在这时候对一次：换会话之后它必须换成这一条会话排的东西。
    // 放在 `attachTurn` 之后——接上时那一轮可能已经在用某条排队任务，先接再取
    // 才不会把"它刚刚开跑的那一条"多画一次。
    await loadQueue();
  }

  function newSession() {
    view.everOpened = true;              // "他清空的"，不是"还没打开过"
    view.sessionId = null;
    // 「新对话」只是把屏上的投影摘下来，**不掐**上一个会话那一轮：它还在服务端跑，
    // 也还在表里收事件（切回去能看到它继续写字，见 `adoptRun`）。
    view.live = null;
    view.abort = null;
    setSending(false);
    // 「新对话」也是明确的一下：待办的目标切换一并作废（新对话还没有归属可对应）。
    view.pendingGoalSwitch = '';
    view.messages = [];
    view.earlierCount = 0;
    // 清空会话也要清掉审查单那一格：那些单子挂在**上一个**会话上。
    view.reviews = {};
    view.reviewOrder = [];
    // 右栏跟着一起空掉：它上面挂着的还是上一个会话的任务 / 子智能体。
    closeDetail();
    renderReviews();
    // 新会话 = 新的一次：上一个会话的失败恢复不该追到这里来。
    cancelAutoContinue();
    auto.attempts = 0;
    auto.failed = false;
    // 排队任务是**会话**的：上一个会话排着的那几条不能留在新会话的输入框上面
    // （它们仍排在服务端那一条会话里，切回去还在）。
    view.queue = [];
    view.queueActive = false;
    view.queueEditing = '';
    renderQueue();
    // 新会话的三项回到**上次的选择**（服务端在 `/session-options` 的 `draft` 里给了，
    // 已经逐项核过还在不在），由 `renderEntry()` 回填。先把本地值清空是为了
    // `renderEntry()` 因`options` 还没回来而提前返回时不留上一个会话的值。
    // 不在这里猜默认值：猜出来的"默认 Agent 是哪个"会在全局默认刚被改过时猜错。
    view.config = null;
    if (dom.agent) dom.agent.value = '';
    if (dom.policy) dom.policy.value = '';
    if (dom.modelSelect) dom.modelSelect.value = '';
    // 清空之后 chip 上的字也要跟着空（`:empty` 会让它不占位，见 style.css）。
    syncModelChip();
    renderSessions();
    renderEntry();
    renderLog();
    // 新对话**也是**一次范围变化：任务与子智能体两块跟的是「这条对话」，
    // 不重画的话它们会停在上一条对话的内容上，而屏上已经没有任何东西提示这一点。
    renderSideJobs();
    renderSideSubagents();
    if (dom.input) dom.input.focus();
  }

  /* ------------------------------------------------------------ 发送 ---- */

  /* 两颗图标（t80）：与 DSH 桌面端那一颗圆钮同一套——向上箭头是"发出去"，实心
     方块是"停下"。都画成 SVG 而不是用「↑ / ■」两个字符：字符的视觉大小取决于
     字体，换一台机器就偏；而这一颗按钮的几何在两种身份之间**不能动**。 */
  const ICON_SEND = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" '
    + 'stroke="currentColor" stroke-width="2.1" stroke-linecap="round" '
    + 'stroke-linejoin="round"><path d="M12 19V5"/><path d="m5 12 7-7 7 7"/></svg>';
  const ICON_STOP = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">'
    + '<rect x="6.5" y="6.5" width="11" height="11" rx="2.5"/></svg>';

  function setSending(busy) {
    view.sending = busy;
    if (dom.send) {
      /* **一颗按钮，两种身份**：空闲＝发送（↑），跑着＝停止（■）。
         几何（宽高、圆角、在那一行里的位置）全在 CSS 里，两种情况共用同一条规则，
         所以换状态时它**不会动**——动的是图标、名字与配色（`.is-stop`）。

         ⚠️ 忙碌时它**必须可点**，所以这里不能写 `disabled = locked`：`locked` 在跑着
         的时候恒为真，而那正是这颗按钮唯一的用途——按下去打断。锁上它等于"跑起来
         之后没有任何刹车"，而失败时的表现是"按钮在、点不动"，与"我没点到"一模一样。
         它只在**空闲且没有模型**时禁用（和改版前发送按钮的禁用条件一致）。

         图标与名字都跟着状态走，也是这一版新加的：只有图标的按钮，`aria-label` /
         `title` 是它**唯一**的名字——念着「发送」而去停止，和点错按钮没有区别。
         （改版前这两个属性静态写在 index.html 上，因为那时它只有一种身份。） */
      dom.send.disabled = !busy && !canSend();
      dom.send.classList.toggle('is-stop', busy);
      dom.send.innerHTML = busy ? ICON_STOP : ICON_SEND;
      // 名字跟着状态走，但**不止两种**（t99）：面板上有勾选时它叫「发送所选（N 件）」。
      // 统一由 `syncSendName()` 算——写死成「停止」/「发送」两种会把件数盖掉。
      syncSendName();
    }
    /* 输入框**不禁用**（t86）。以前跑着的时候把它 `disabled`，于是"想补一句话"
       只能等这一轮跑完——而这一轮可能还要跑十几步。现在跑着时它照样能打字，回车
       把那句话变成一条**排队任务**（见 `send()` 与 `queueMessage`），要立刻说的
       就点那条任务上的「插话发送」。刹车仍然是上面这颗按钮，它没有为此让位：
       两种身份（发送/停止）与输入框可不可编辑是两件事。
       排队行的按钮名字跟这次状态走（插话 / 现在发送），所以要跟着重画。 */
    renderQueue();
  }

  /** 打断正在跑的这一轮。
   *
   *  **先让服务端停，再断开这条接收流。** 旧版只 abort：那时执行方就是这条连接，
   *  断开即停止。现在断开只是“不看了”（刷新页面也是同一条路），所以必须显式
   *  说一声——不说的话界面写着“已停止”而服务端还在往下跑、还在落库。
   *
   *  已经执行完的工具调用不会回滚（扫描任务仍在队列里），这句提示必须说清。
   */
  function stopTurn() {
    const sessionId = view.sessionId || '';
    // 刹车踩的是**屏上这一轮**（`view.abort` 就是它的控制器）；这个会话若只在服务端
    // 跑着、本页没接上（`view.abort` 为空），仍然要发那一声 `/chat/stop`。
    if (!view.abort && !sessionId) return;
    // 人按了停止：自动执行的续跑立刻作废。否则它会把这颗“刹车”当成一次失败，
    // 三秒后再发一句“继续”——那是这条功能最坏的一种表现。
    cancelAutoContinue();
    ui().showError('已停止。这一轮会在当前这一步之后结束——已经下发的作业不会撤回，'
      + '可在「作业」页签查看。');
    // **还挂着的那几张审查单要按拒绝结掉。** 只 abort 不会让服务端的等待结束：
    // 等待期间它一个字节都不写，所以看不见连接断开，那一轮会一直停在审查单上
    // 直到超时——而入口闸在那一段时间里会挡住下一轮。
    denyPendingReviews();
    if (sessionId) {
      // 失败不弹错：停止是“下一次模型调用之前生效”，服务端可能刚好在这期间
      // 自己跑完了（那时 404/409 都正常）；而连接断开仍会让它在下一步之前看到信号。
      ui().apiPost('/chat/stop', { sessionId }).catch(() => {});
    }
    if (view.abort) view.abort.abort('user');
  }

  /** 把还在等的审查单按「使用者中止了这一轮」结掉。
   *
   *  这里只**发出**裁决请求（不 await），紧接着调用方就 `abort()`。顺序是有意的：
   *  断开先生效、裁决随后落地，于是服务端那一轮从 `wait` 里出来、往下走一步，
   *  而它写回来的第一帧正好撞上已经断开的连接 —— 它在**下一次模型调用之前**
   *  看到断开并停下。反过来（裁决落地时连接还好着）它会照常把剩下几步跑完。
   *
   *  失败不弹错：这是一次收尾，用户已经在看"已停止接收"那一句了。
   */
  function denyPendingReviews() {
    Object.keys(view.reviews || {}).forEach((id) => {
      const row = view.reviews[id];
      if (!row || !row.pending || !row.awaiting) return;
      ui().apiPost(`/reviews/${encodeURIComponent(id)}`,
                   { approve: false, note: '使用者中止了这一轮（点了停止）' })
        .catch(() => {});
    });
  }

  /** 能不能发。模型可以在会话里现选，因此"有可选模型"也算能发。 */
  function canSend() {
    if (view.hasModel) return true;
    return asArray(view.options && view.options.models).length > 0;
  }

  /** 从输入框发一条。读框、清框，然后交给 `sendMessage`。
   *
   *  拆成两层是为了**自动执行**：那一句"继续"也要走完全同一条发送路径（带三项、
   *  开流、失败分类、收尾），只是不经过输入框。两条路径各写一遍的话，必然有
   *  某一边少带一个参数、或者忘了 setSending——而症状是"自动续跑的那几轮界面不动"，
   *  看起来像它根本没跑。
   */
  function send() {
    if (!dom.input) return;
    const text = dom.input.value.trim();
    if (!text) return;
    if (!canSend()) {
      ui().showError('未启用模型：会话需要模型来决策工具调用，请先到「设置」页签配置'
        + '一条，或在对话框上方的「模型」里选一条已有的。');
      return;
    }
    /* 跑着的时候回车 = **附加成一条任务**（t86），而不是"什么都不做"。
       改版前这里直接 `return`，于是界面给人的印象是"按了没反应"——字还留在框里
       而这一轮没有半点反应。现在它排进输入框上方那块列表，等这一轮结束自动开跑；
       要立刻说的，点那条任务上的「插话发送」（走 `/chat/interject`，送进正在跑的
       这一轮）。两种去处都在那一条上，不在输入框上——所以输入框不必禁用。 */
    const busy = view.sending;
    dom.input.value = '';
    // 清空之后要让框缩回去：`fitInput` 只在 input 事件里跑，而这里是程序改值。
    fitInput();
    // 人自己发 = 一次新的开始：把失败计数与待发的续跑一起清掉（不再替刚才那次收尾）。
    auto.attempts = 0;
    auto.failed = false;
    cancelAutoContinue();
    if (busy) queueMessage(text);
    else sendMessage(text);
  }

  /** 输入框那颗发送键**现在**该发什么。
   *
   *  ⚠️ t99：面板里那颗键删掉之后，"发这一批"也走这颗键——它是页面上唯一的一颗。
   *  分派规则只有一条：**面板上有勾选就发那一批，否则发框里的字**。
   *  一次点击只做一件事：勾着 2 件、框里又写了半句话，按一下**只发那一批**。两件一起发的
   *  话会同时开两条流（第二条撞 409，失败路径还把字塞回框里），而使用者只按了一次；框里
   *  那半句话原样留着，这一批发完还能接着写。
   *
   *  回车**不**走这里：输入框里按回车就是"发我打的这行字"（见 keydown 那条绑定），哪怕面板
   *  上还勾着几件——刚写下的那句话比勾选更新，也更符合"我在对话框里按了回车"的直觉。
   */
  function sendPrimary() {
    if (choiceTasks(pendingOptions()).length) { sendChoice(); return; }
    send();
  }

  /* ------------------------------------------------- 一轮 = 一个会话一份 ----
   *
   *  `view.live` / `view.abort` / `view.sending` 只是"**正看着的**那个会话的
   *  那一轮"的投影，真正的持有者是下面这张表。理由是一件很朴素的事：流跑在
   *  浏览器里，而"去看别的对话"是正常操作——切走不该把正在跑的那一轮掐掉，
   *  也不该让它的字继续往别人的窗口里淌。
   *
   *  一张表三件事：
   *    · 切走（`openSession` / `newSession`）只是把投影摘下来，表里那一项继续收事件；
   *    · 切回（`adoptRun`）把同一份 `live` 挂回屏上——**同一个对象**，所以已经在跑
   *      的 `applyEvent` 不需要知道自己在后台，字一个都不丢；
   *    · 收尾（`retractRun`）才把它从表里拿掉。
   */
  function emptyLive() {
    return { step: 0, maxSteps: 0, stepsAtCeiling: false, text: '',
             reasoning: '', tools: [], notices: [], policy: '', subagents: [] };
  }

  /** 这个会话现在有没有一轮在跑。没有就返回 null（不是"空对象"）。 */
  function runOf(sessionId) {
    const run = view.runs[String(sessionId || '')];
    return run && run.controller ? run : null;
  }

  /** 开一轮：登记进表，并立刻挂到屏上（发送的人当然正看着它）。 */
  function startRun(sessionId) {
    const key = String(sessionId || '');
    // 同一个键上若还挂着上一轮，先把那一条**从表里摘掉**再登记新的：新会话的第一轮
    // 在 `start` 帧到达之前键是空串，若使用者在这几十毫秒里又发了一句「新对话」，
    // 直接覆盖会把上一轮从表里挤丢（它的流还在跑，却再也切不回去）。摘掉而不是
    // abort：那一轮是真实存在的活，让人自己点停止。
    if (view.runs[key]) retractRun(view.runs[key]);
    const run = { sessionId: key, live: emptyLive(), controller: new AbortController() };
    view.runs[key] = run;
    view.live = run.live;
    view.abort = run.controller;
    return run;
  }

  /** 新会话的 id 在 `start` 帧才产生：表里的键跟着换，否则切回来找不到这一项。 */
  function renameRun(run, sessionId) {
    const key = String(sessionId || '');
    if (!run || String(run.sessionId || '') === key) return;
    if (view.runs[String(run.sessionId || '')] === run) delete view.runs[String(run.sessionId || '')];
    run.sessionId = key;
    view.runs[key] = run;
  }

  /** 把某一轮从表里拿掉（它已经收尾了）。投影是否清空由调用方决定。 */
  function retractRun(run) {
    if (!run) return;
    Object.keys(view.runs || {}).forEach((key) => {
      if (view.runs[key] === run) delete view.runs[key];
    });
    run.controller = null;
  }

  /** 切到某个会话时，把它的那一轮（如果在跑）搬回屏上。 */
  function adoptRun(sessionId) {
    const run = runOf(sessionId);
    view.live = run ? run.live : null;
    view.abort = run ? run.controller : null;
    setSending(Boolean(run));
  }

  /** 这一轮是不是正挂在屏上（= 它的事件可以直接动界面）。 */
  function onScreenRun(run) {
    return Boolean(run && view.live === run.live);
  }

  function sendMessage(text) {
    // 只有**正看着的这个会话**在跑才拦：别的会话在跑不关这里的事（见上面那张表）。
    if (view.sending) return;
    if (!canSend()) return;      // 入口那条提示由 send() 负责说
    ui().hideError();
    view.messages = view.messages.concat([{ role: 'user', text }]);
    // 这一轮的**临时**视图：只用于"边跑边看"。它不会被写进 view.messages——
    // 最终记录一律以服务端落库的那一份为准（见 finish()），否则刷新一次就会
    // 出现"界面上的回答"和"记下来的回答"两份不同的东西。
    // 登记成**这个会话那一轮**：切走之后它还在这里，切回来就靠它把字搬回屏上。
    const run = startRun(view.sessionId || '');
    setSending(true);
    renderLog();
    const controller = run.controller;
    streamTurn(text, controller, run)
      .catch((error) => {
        // 先看**信号**，再看错误对象：`abort('user')` 之后 fetch 的拒绝值就是
        // 那个字符串，没有 name 也没有 message（见 `interruptionOf`）。
        const interrupted = interruptionOf(error, controller.signal);
        if (interrupted === 'user' || interrupted === 'signout') return;  // 人停的 / 退出的
        if (interrupted === 'idle') {
          // 看门狗掐的：这一轮很可能还在服务端跑着，所以**不续跑**——再发一句
          // 只会撞上 409。正确动作是接回去（刷新页面走的就是那条路）。
          // 紧接着 `finish()` 会 `openSession` 再问一次“在跑吗”；不拦它就会
          // 接上去、再等三分钟、再掐——一个没有出口的圈（见 `attachTurn`）。
          view.attachPaused = true;
          ui().showError('连接已经 3 分钟没有收到任何新事件，界面停止接收。'
            + '这一轮仍在服务端继续跑：刷新页面可以再接上。');
          return;
        }
        if (interrupted) return;               // 其它 AbortError：不是故障
        if (error && error.status === 409) {
          // 服务端说这个会话还有一轮没跑完。这不是“请求失败了”，而是“你按得太快”：
          // 把乐观插进去的那条撤回输入框（别把人刚打的字丢掉），然后让紧接着的
          // `finish()` 去 `openSession` —— 那条路径上的 `attachTurn` 会把正在跑
          // 的那一轮接回来。**不在这里自己调 attachTurn**：finish 会把 view.live
          // 清掉再重开一次，接上两次会在屏幕上留下两个“正在生成”的气泡。
          const last = view.messages[view.messages.length - 1];
          if (last && last.role === 'user' && last.text === text) {
            view.messages = view.messages.slice(0, -1);
          }
          if (dom.input && !dom.input.value) dom.input.value = text;
          auto.attempts = 0;
          auto.failed = false;
          ui().showError('这个会话还有一轮在跑：已经接上它。刚才那条消息没有发出去，'
            + '字已放回输入框，等这一轮结束再发。');
          return;
        }
        auto.failed = true;
        ui().showError('对话请求失败：' + describeError(error));
        // 失败是自动执行唯一关心的信号：分类、退避、倒计时都在这里开始。
        // **只在它正挂在屏上时**才续：切到别的对话之后这一轮的失败不该往那个
        // 会话里发「继续」（`sendMessage` 用的是 `view.sessionId`）。
        if (onScreenRun(run)) scheduleAutoContinue(describeError(error));
      })
      .then(() => finish(run));
  }

  /** 收尾：不论成功、失败还是被停止，都回到"以服务端为准"的状态。 */
  async function finish(run) {
    // 收的是**哪一轮**，不是"此刻在看哪一条对话"：切到别的对话去看记录之后，
    // 后台那一轮照样会跑完——按 `view.sessionId` 收尾就会把别人的视图按这一轮的
    // 结果重画一遍，还会把使用者正在看的那一轮的气泡抹掉（见 `runOf` 上面那段）。
    const sessionId = (run && run.sessionId) || view.sessionId;
    const mine = run ? run.live : view.live;      // 这一轮自己的实时视图
    const onScreen = Boolean(mine && view.live === mine);   // 它此刻挂在屏上吗
    // 换目标时被"一轮在跑"拦下的那次切换（`alignSessionToGoal`）先抓到手上：紧接着的
    // `openSession` 会清掉这个标记，而它恰恰要活过这一趟。
    // 待办只在**它自己挂在屏上**时才取：后台那一轮收尾时去补一次「换目标」的切换，
    // 会把使用者正看着的窗口换走（那也是串味的一种）。字面照旧，判据挪到取值上。
    const pending = onScreen ? view.pendingGoalSwitch : '';
    // 先把它从表里拿掉：紧接着的 `openSession` 会问服务端「还有一轮在跑吗」，
    // 这一项若还挂着，`adoptRun` 会把一个已经收尾的气泡重新挂回屏上。
    retractRun(run);
    if (onScreen) {
      view.abort = null;
      // 收尾会把实时气泡整个换掉（紧接着的 `openSession` 按服务端落库的那份重画）。
      // 使用者若正把思考块展开着读，那一换就折回去了——而答案恰恰在这时出现。
      // 把状态交给 `renderLog`，由它接到落库那一条上（见那里的注释）。
      if (view.live && view.live.reasoningOpen) view.carryReasoningOpen = true;
      view.live = null;
      setSending(false);
    }
    if (onScreen) {
      if (sessionId) await openSession(sessionId);
      else renderLog();
    }
    await loadSessions(true);
    // 收尾之后才补那次被拦下的切换。若 `openSession` 已经把队列里的下一条接了上来
    // （`setSending(true)`），**必须把它留着**：此刻切走会打断那条流。
    if (pending && !view.sending) { view.pendingGoalSwitch = ''; alignSessionToGoal(pending); }
    else if (pending) view.pendingGoalSwitch = pending;
    /* 排队那块**不在这里再拉一次**：上面那次 `openSession` 末尾已经在
       `attachTurn` 之后拉过了（若服务端趁这一下自动开了队列里的下一条，接上的
       就是那一轮）。两处都拉只是每次收尾多一趟往返，而两份响应还会互相覆盖。 */
    // **成功之后才清零**。放在这里而不是每次发送之前：自动执行要跨过这一趟
    // `openSession`（它会把这一轮的记录重新取回来），若在这里无条件清零，
    // 连续失败时计数永远是 1，退避也就永远停在第一档。
    // 同理：后台那一轮的成败不该动自动执行的计数（那是屏上这一轮的状态）。
    if (onScreen && !auto.failed) { auto.attempts = 0; cancelAutoContinue(); }
  }

  /* ---------------------------------------------------------- 排队任务 ----
   *
   * 一轮正在跑的时候，输入框照样能打字；回车不新开一轮，而是**附加成一条任务**，
   * 列在输入框上方，等这一轮收尾时由服务端自动开下一条（`_turn_finished` →
   * `_drain_queue`）。每一条行末尾另有一颗"送进去"的箭头图标（悬浮提示「插话发送」）：
   * 它不进队列，直接送进正在跑的
   * 那一轮（`/chat/interject` → `TurnRun.interject`），模型下一步就按它来。
   *
   * 为什么队列放在**服务端**（`pentestd/turnqueue.py`）："下一轮什么时候开始"由
   * 收尾那一刻决定，而页面不在那条时间线上——只存在页面里的话，刷新一次就没了，
   * 而那一轮仍在跑、还在落库。为什么不落库：条目开跑的那一刻就变成一条 `user`
   * 消息落进 `chat_messages`，在那之前它只是"将要发生的事"。
   *
   * 所以这一块的状态都由服务端给（`items`），本地只留 `queueEditing` 这一个纯 UI
   * 状态。按"本地拼一份、服务端再对一次"来写，必然出现两份不一样的东西。
   */

  /** 这一轮现在在不在跑。两个来源取或：本页正在看这条流（`sending`），或服务端
   *  说这个会话有一轮活着（`queueActive`——刷新之后还没接上时就是它）。
   *  行末尾那颗图标跟着它换（箭头 = 插话、播放 = 现在发送）：换错了，点下去的结果
   *  就和悬浮提示不符。 */
  function queueBusy() {
    return Boolean(view.sending || view.queueActive);
  }

  function queueHintText() {
    if (!view.queue.length) return '';
    if (queueBusy()) {
      return '这一轮还在跑：回车会附加成任务，等它结束自动执行；要马上说，点行末尾那颗箭头图标。';
    }
    return '这一轮已经停了：点行末尾那颗播放图标，立刻开始这一条。';
  }

  /* 一排图标（t89）：文字按钮「插话发送 / 编辑 / 删除」三颗就把一行撑满了，而这一
     行的正文才是内容。三颗都换成 SVG，名字挪到 `title`（鼠标悬浮提示）与
     `aria-label`（读屏）——**两者都要**：只写 title 读屏叫不出名字，只写
     aria-label 鼠标停上去什么也不显示。 */
  const ICON_QUEUE_INTERJECT = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" '
    + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    + 'stroke-linejoin="round"><path d="m15 14 5-5-5-5"/><path d="M4 20v-7a4 4 0 0 1 4-4h12"/></svg>';
  const ICON_QUEUE_START = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">'
    + '<path d="M8 5.2v13.6L19 12Z"/></svg>';
  const ICON_QUEUE_EDIT = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" '
    + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    + 'stroke-linejoin="round"><path d="M12 20h9"/>'
    + '<path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
  const ICON_QUEUE_TRASH = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" '
    + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    + 'stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/>'
    + '<path d="M19 6l-1 14H6L5 6"/><path d="M10 11v5M14 11v5"/></svg>';
  const ICON_QUEUE_SAVE = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" '
    + 'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" '
    + 'stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
  const ICON_QUEUE_CANCEL = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" '
    + 'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" '
    + 'stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';

  /** 一颗图标按钮。`act` 决定行为与配色，`label` 同时当读屏名与悬浮提示。 */
  function queueButton(act, icon, label, extra) {
    const btn = el('button', 'chat-queue-btn' + (extra ? ' ' + extra : ''), null);
    btn.type = 'button';
    btn.dataset.act = act;
    btn.innerHTML = icon;
    btn.setAttribute('aria-label', label);
    btn.title = label;
    return btn;
  }

  /** 一行排队任务。编辑态与平常态共用同一个 `<li>`（`queueEditing` 记着 id）。 */
  function queueRow(item, index) {
    const li = el('li', 'chat-queue-item');
    li.dataset.id = String(item.id || '');
    const editing = view.queueEditing === item.id;
    if (editing) {
      const box = el('textarea', 'chat-queue-edit');
      box.value = String(item.text || '');
      box.setAttribute('aria-label', '编辑这条排队任务');
      box.dataset.act = 'edit-text';
      li.append(box);
    } else {
      const text = el('p', 'chat-queue-text', String(item.text || ''));
      // 三行截断（.chat-queue-text 的 -webkit-line-clamp）：一条可以是一整段需求，
      // 全展开会把输入框顶出屏幕。完整那句挂在 title 上，点「编辑」也能看到。
      text.title = String(item.text || '');
      li.append(text);
      // 没开跑的那一条把原因也说清（审查单没结、会话没了……）。条目留着：队列是
      // 内存态，这个列表是它唯一的可见面。
      if (item.error) li.append(el('p', 'chat-queue-error', String(item.error)));
    }
    const row = el('div', 'chat-queue-row');
    row.append(el('span', null, '第 ' + (index + 1) + ' 条'));
    const actions = el('div', 'chat-queue-actions');
    if (editing) {
      actions.append(queueButton('save', ICON_QUEUE_SAVE, '保存（Ctrl/⌘+Enter）'),
                     queueButton('cancel', ICON_QUEUE_CANCEL, '取消（Esc）'));
    } else {
      const busy = queueBusy();
      actions.append(queueButton('send', busy ? ICON_QUEUE_INTERJECT : ICON_QUEUE_START,
                     busy ? '插话发送：送进正在跑的这一轮' : '现在发送：立刻开始这一条'),
                     queueButton('edit', ICON_QUEUE_EDIT, '编辑'),
                     queueButton('delete', ICON_QUEUE_TRASH, '删除'));
    }
    row.append(actions);
    li.append(row);
    return li;
  }

  function renderQueue() {
    if (!dom.queue || !dom.queueList) return;
    const rows = asArray(view.queue);
    if (!rows.length) {
      dom.queue.hidden = true;
      clear(dom.queueList);
      return;
    }
    dom.queue.hidden = false;
    if (dom.queueTitle) dom.queueTitle.textContent = '排队中 · ' + rows.length + ' 条';
    if (dom.queueHint) {
      const hint = queueHintText();
      dom.queueHint.textContent = hint;
      // 头那一行放不下整句时按 ellipsis 截断（.chat-queue-hint），完整那句在这里。
      if (hint) dom.queueHint.title = hint;
      else dom.queueHint.removeAttribute('title');
    }
    clear(dom.queueList);
    rows.forEach((item, index) => dom.queueList.append(queueRow(item, index)));
    // 正在编辑的那一行把光标放回它的框里：重画换掉了整个 textarea 节点。
    if (view.queueEditing) {
      const box = dom.queueList.querySelector('[data-act="edit-text"]');
      if (box) {
        box.focus();
        const end = box.value.length;
        try { box.setSelectionRange(end, end); } catch (err) { /* 个别浏览器不支持 */ }
      }
    }
  }

  /** 拉一次这个会话排着的任务。打开会话、以及每轮收尾之后各来一次。 */
  async function loadQueue() {
    if (!dom.queue) return;
    const sessionId = view.sessionId || '';
    if (!sessionId) {
      view.queue = []; view.queueActive = false; view.queueEditing = '';
      renderQueue();
      return;
    }
    try {
      const payload = await ui().apiGet(
        '/chat/queue?sessionId=' + encodeURIComponent(sessionId));
      // 这一趟往返回来时可能已经换了会话：只认当前会话的那一份。
      if ((payload.sessionId || '') !== (view.sessionId || '')) return;
      view.queue = asArray(payload.items);
      view.queueActive = payload.active === true;
      if (view.queueEditing
          && !view.queue.some((item) => String(item.id) === String(view.queueEditing))) {
        view.queueEditing = '';            // 编辑到一半的那条已经开跑或被删了
      }
      renderQueue();
    } catch (error) {
      // 队列是内存态：取不到就是取不到。不能把上一次会话的那几条继续画在输入框
      // 上面——那会让"排着的东西"看起来还在，而服务端已经没有这一份。
      view.queue = []; view.queueActive = false;
      renderQueue();
    }
  }

  /** 跑着的时候按回车 → 一条排队任务。 */
  async function queueMessage(text) {
    const body = String(text || '').trim();
    const sessionId = view.sessionId || '';
    if (!body) return;
    if (!sessionId) {
      // 没有会话就没有排队的落点。正常路径到不了这里（跑着就说明有会话），
      // 真到了也不要丢字。
      if (dom.input && !dom.input.value) { dom.input.value = body; fitInput(); }
      return;
    }
    try {
      const payload = await ui().apiPost('/chat/queue', { sessionId, message: body });
      view.queue = asArray(payload.items);
      if (payload.started) view.queueActive = true;
      renderQueue();
    } catch (error) {
      // 排不进去（队列满 / 太长 / 会话没了）就把字放回输入框——这是唯一保证不丢的
      // 地方；队列那边确实没留下任何东西。
      if (dom.input && !dom.input.value) { dom.input.value = body; fitInput(); }
      ui().showError('附加成任务失败：' + describeError(error));
    }
  }

  /** 一条排队任务上的四个动作：编辑 / 保存 / 取消 / 删除 / 现在送出去。 */
  async function queueAction(act, li) {
    const itemId = li ? (li.dataset.id || '') : '';
    const sessionId = view.sessionId || '';
    if (!itemId || !sessionId) return;
    const item = asArray(view.queue).find((row) => String(row.id) === itemId);
    if (!item && act !== 'cancel') return;   // 它已经不在了（重画之前的那一下点击）
    try {
      if (act === 'cancel') {
        view.queueEditing = '';
        renderQueue();
        return;
      }
      if (act === 'edit') {
        view.queueEditing = itemId;
        renderQueue();
        return;
      }
      if (act === 'save') {
        const box = li.querySelector('[data-act="edit-text"]');
        const text = String((box && box.value) || '').trim();
        if (!text) {
          ui().showError('这条任务现在是空的：写一句话，或者点「取消」。');
          return;
        }
        const payload = await ui().apiPost('/chat/queue/update',
                                          { sessionId, id: itemId, message: text });
        view.queue = asArray(payload.items);
        view.queueEditing = '';
        renderQueue();
        return;
      }
      if (act === 'delete') {
        const payload = await ui().apiPost('/chat/queue/delete', { sessionId, id: itemId });
        view.queue = asArray(payload.items);
        renderQueue();
        return;
      }
      if (act === 'send') {
        const payload = await ui().apiPost('/chat/queue/send', { sessionId, id: itemId });
        view.queue = asArray(payload.items);
        view.queueEditing = '';
        renderQueue();
        if (payload.mode === 'started') {
          // 服务端已经开了这一轮，而这条流是**本页新起**的：接回来（它自己会
          // setSending(true) 并一直读到结束）。接不上（这一轮已经跑完）就按落库的
          // 那份重画一次。
          await attachTurn(sessionId);
          if (!view.sending) await openSession(sessionId);
        }
        return;
      }
    } catch (error) {
      ui().showError('排队任务操作失败：' + describeError(error));
      // 服务端那份可能已经变了（它刚开跑 / 已被删）：以它为准重取一次。
      await loadQueue();
    }
  }

  /** 把一条 SSE 响应读进 `view.live`：**发送**与**接上正在跑的那一轮**共用它。
   *
   *  服务端两边走的是同一条转发器（`_follow_chat`），事件契约完全一样，所以
   *  消费方也不该有两份实现。两份的典型症状是“发送正常、接回来的那一轮少掉了
   *  思考块或工具轨迹”——而那种差异只有真的刷新过才看得到。
   */
  async function consumeStream(response, controller, run) {
    // 认准**自己这一轮**：切走之后 `view.live` 指向别人的那一份，按它写就是
    // 把后台会话的字打到前台窗口里（见 `runOf` 上面那段）。
    const signal = controller.signal;
    const live = run ? run.live : view.live;
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    const parser = sseParser();
    const watchdog = idleWatchdog(controller);
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        // 心跳注释帧也会让 `read()` 返回，所以这一句同时给“多久没收到东西”的看门狗续命：
        // 一次工具调用可能跑好几分钟，而服务端每 15 秒会发一个注释帧。
        watchdog.beat();
        parser.push(decoder.decode(value, { stream: true })).forEach((frame) => applyEvent(frame, run));
        if (live && live.error) return;
      }
      const rest = decoder.decode();
      if (rest) parser.push(rest).forEach((frame) => applyEvent(frame, run));
      parser.flush().forEach((frame) => applyEvent(frame, run));
    } finally {
      watchdog.stop();
      try { reader.cancel(); } catch (error) { /* 连接已断，取消本身失败可忽略 */ }
    }
    // 流**干净地关了**不等于这一轮跑完了：服务端重启、代理掐断、进程被杀，
    // 都可能是一个再正常不过的 EOF。不认这一种，界面上就是"停在半句、按钮变回
    // 发送、什么都没发生"——而使用者能做的只有自己再发一句。插件（auto-continue）
    // 把这种叫 interrupted，与 error 同等对待，这里也照做。
    if (!live || signal.aborted) return;      // 人点了停止：不是故障
    if (!live.error && !live.done) {
      live.error = '连接在没有收到结束帧的情况下断了';
      if (onScreenRun(run)) scheduleAutoContinue(live.error);
      return;
    }
    // 另一种"看起来成功"的失败：结束帧来了，但这一轮**一个字正文都没说**
    // （只想了、或只跑了工具）。插件把它归进"不该算完成"：这种回合十有八九是
    // 输出预算被思考吃光，重来一次通常就好了。
    if (!live.error && live.done && !visibleOutput(live)) {
      // 原因一起说。只写"没有产出"，读者只能猜是模型懒还是平台丢包；带着
      // `length` 就一目了然：max_tokens 太小，改配置即可。
      // 不带「这一轮」：外面那两句（「这一轮没有跑完：」、「这一轮失败了：」）已经带了主语，
      // 再加一次就是「这一轮没有跑完：这一轮没有产出正文」。
      live.error = '没有产出正文' + (live.finishNote ? '（' + live.finishNote + '）' : '');
      if (onScreenRun(run)) scheduleAutoContinue(live.error);
    }
  }

  /** 这一轮有没有**说出正文**。只有空话回合才续跑，正常回合一律不动。 */
  function visibleOutput(live) {
    if (!live) return false;
    // **思考不算**：只想了 3919 个字、一个字没说就收尾，在记录里就是一条正文为空的
    // 助手消息（库里真有这种：seq 281，text 空、reasoning 3919、trace 0）。那种
    // "看起来停住了"的回合正是需要续跑的一类，把它当成有产出就永远不续。
    if (String(live.text || '').trim()) return true;
    if ((live.segments || []).length) return true;
    return false;
  }

  async function streamTurn(text, controller, run) {
    const response = await ui().apiStream('/chat/stream', {
      message: text,
      sessionId: view.sessionId || '',
      // 会话归属**不在这里定**：新建会话先不绑目标，等第一条产出型任务落下，
      // 由它的 target 决定归属（`store.ensure_session_engagement(..., target=…)`）。
      // 此前这里传的是侧边栏当前选中的目标，于是"在 A 目标下新开一条对话去打 B"
      // 会把 B 的证据写进 A 的目标——目标页永远只有一行，也看不出这条对话对应
      // 哪台机器。已有会话同样置空：归属是这条对话当初的事实，不随侧边栏漂移。
      engagementId: '',
      // 三项随消息一起带上：界面上的"改完直接发"因此只有一次往返，
      // 不会出现"选择生效了但没记下来"的中间态。
      agentId: (dom.agent && dom.agent.value) || '',
      modelId: (dom.modelSelect && dom.modelSelect.value) || '',
      policyId: (dom.policy && dom.policy.value) || '',
      // 开关随消息一起带上：新会话就是在这里才落库的（建会话那一步会把它写进去）。
      autoDrive: auto.on === true,
    }, controller.signal);
    // 带上 `run`：收尾要看的是**这一轮**有没有说出正文，而不是屏上那一轮。
    await consumeStream(response, controller, run);
  }

  /** 接上一条**已经在跑**的流。 */
  async function streamAttach(sessionId, controller, run) {
    // `since=0` = 整轮重放。不从“现在这一刻”接，是因为客户端手上什么都没有：
    // 正文是逐条 `delta` 拼出来的，只从当前接上就会缺掉前半句，而那半句往往
    // 正是“它为什么要调这个工具”的交代。
    const response = await ui().apiStreamGet(
      `/chat/stream?sessionId=${encodeURIComponent(sessionId)}&since=0`, controller.signal);
    await consumeStream(response, controller, run);
  }

  /** 服务端还有一轮在跑就把它接回来（刷新页面之后走的就是这条路）。
   *
   *  先问 `GET /chat/live`：没有轮次时只有这一次廉价的往返，随后照常只读记录。
   *  这件事**必须便宜**：每次打开会话都会问一次。
   */
  async function attachTurn(sessionId) {
    // 这个会话已经有一轮在表里跑着（本页起的，或刚切回来）：`adoptRun` 会把它
    // 挂回屏上，再 attach 一次就是同一个会话两条流、两遍字。
    if (!sessionId || runOf(sessionId)) return;
    // 看门狗刚把上一条流掐断（见 `sendMessage` 的 idle 分支）：紧接着的
    // `finish()` 还会再问一次“在跑吗”。不跳过这一次就会**立刻再接上**、
    // 再等三分钟、再掐——“接不上就重接”的圈。跳过之后记录区照常显示
    // 已落库的部分；使用者手动刷新（`reload`）会再走一遍，那时标记已清。
    if (view.attachPaused) { view.attachPaused = false; return; }
    let state = null;
    try {
      state = await ui().apiGet(`/chat/live?sessionId=${encodeURIComponent(sessionId)}`);
    } catch (error) {
      return;      // 只是“有没有在跑”的旁问：失败不该拦着看历史
    }
    if (!state || !state.active) return;
    const info = state.run || {};
    if (info.cancelling) return;   // 已经在停的那一轮：接上去只会看到一个不会动的气泡
    // **这一轮已经落库的那几条先从记录里摘掉**：重放会把整轮重新画一遍，不摘
    // 就会同一段话出现两次，而且两次的边界正好落在工具调用处。
    const mine = asArray(info.persistedSeqs).map(Number);
    if (mine.length) {
      view.messages = view.messages.filter((one) => mine.indexOf(Number(one.seq)) < 0);
    }
    // 登记成**这个会话那一轮**（与「发送」走同一张表）：切走之后它照样把字收进
    // 自己那份 `live`，切回来才有一个完整的气泡，而不是从半句接着长。
    const turn = startRun(sessionId);
    setSending(true);
    renderLog();
    const controller = turn.controller;
    try {
      await streamAttach(sessionId, controller, turn);
    } catch (error) {
      // 与「发送」那条路同一个坑：`abort('user')` 的拒绝值是**字符串**，
      // `error.name`/`error.message` 都是 undefined，于是界面会打出
      // 「没接上正在跑的那一轮：undefined」——一句既没信息又不对的话。
      const interrupted = interruptionOf(error, controller.signal);
      // **不在这里调 finish()**：调了就会经 `openSession` 再问一次“在跑吗”，
      // 于是接不上就重接、重接再失败，绕成环。这里只是把界面放回可操作的状态。
      // 摘的是**这一轮**；它若正挂在屏上才动界面（切走了就不该重画别人的窗口）。
      const shown = onScreenRun(turn);
      retractRun(turn);
      if (shown) {
        view.abort = null;
        setSending(false);
        renderLog();
      }
      if (interrupted === 'user' || interrupted === 'signout') {
        // 人停的 / 退出的：留一个不再变化的气泡比不留更糟（它看起来还在生成）。
        // 记录区会停在这一轮开始之前；下一次打开会话就以服务端落库的为准。
        if (shown) view.live = null;
        renderLog();
        return;
      }
      if (interrupted === 'idle') {
        view.attachPaused = true;   // 见 `attachTurn` 与「刷新会话」按钮
        if (shown) view.live = null;
        renderLog();
        ui().showError('接上之后 3 分钟没有收到任何新事件，界面停止接收。'
          + '那一轮仍在服务端继续跑，刷新页面可以再试一次。');
        return;
      }
      if (interrupted) return;          // 其它 AbortError：不是故障
      ui().showError('没接上正在跑的那一轮：' + describeError(error)
        + '。它仍在服务端继续跑，刷新页面可以再试一次。');
      return;
    }
    await finish(turn);
  }

  /** 空闲看门狗：**不是整轮超时**，而是"多久没收到任何事件"。
   *
   *  一轮对话可能要跑几分钟（模型 + 工具），给整轮设超时会误杀正常的长任务；
   *  但完全不设又会让一次悄悄死掉的连接永远转下去。按"事件之间"计时同时满足两边：
   *  只要还在往前走就不会被掐，真的卡住了才报。
   */
  function idleWatchdog(controller) {
    let timer = null;
    const arm = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        // 掐**自己这一条**流，不是 `view.abort`。后者是"屏上那一轮"的投影：
        // 切到别的对话之后它指向别人，按它掐会掐错人——后台那一轮还在跑，
        // 却把使用者正在看的那一轮掐死了。
        if (controller) controller.abort('idle');
      }, STREAM_IDLE_MS);
    };
    arm();
    if (controller && controller.signal) {
      controller.signal.addEventListener('abort', () => clearTimeout(timer), { once: true });
    }
    return { beat: arm, stop: () => clearTimeout(timer) };
  }

  /** SSE 帧解析：喂进解码后的片段，产出 `{ event, data }`。
   *
   *  按空行分帧，**不清空残留**——一个网络包可能正好切在帧中间，把没见到空行的
   *  尾巴丢掉就会偶发丢事件，而且只在网络慢的时候出现。残留留在缓冲区等下一片。
   */
  function sseParser() {
    let buffer = '';
    const drain = (flush) => {
      const out = [];
      let cut = buffer.indexOf('\n\n');
      while (cut >= 0) {
        const block = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        const frame = parseFrame(block);
        if (frame) out.push(frame);
        cut = buffer.indexOf('\n\n');
      }
      if (flush && buffer.trim()) {
        const frame = parseFrame(buffer);
        if (frame) out.push(frame);
        buffer = '';
      }
      return out;
    };
    return {
      push(chunk) { buffer += String(chunk).replace(/\r\n/g, '\n'); return drain(false); },
      flush() { return drain(true); },
    };
  }

  function parseFrame(block) {
    let event = 'message';
    const data = [];
    block.split('\n').forEach((line) => {
      if (!line || line.startsWith(':')) return;          // 注释帧（心跳）按规范忽略
      const colon = line.indexOf(':');
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? '' : line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'event') event = value;
      else if (field === 'data') data.push(value);
    });
    if (!data.length) return null;
    try {
      return { event, data: JSON.parse(data.join('\n')) };
    } catch (error) {
      return null;      // 帧本身不是合法 JSON：丢掉它，但不能因此打断整条流
    }
  }

  /** 把一帧 `persist` 里**插话**的那条 user 消息塞进记录。
   *
   *  回答不走这里：它只在收尾时由 `finish()` 按落库的那份整体重画一次。
   *  但插话的那一句必须**立刻**出现——按下「插话发送」之后屏幕上半点反应都没有，
   *  和"没送进去"看起来一模一样。
   *
   *  按 `seq` 去重：收尾时 `finish()` 会把同一条从服务端再取回来一次，不去重就会
   *  在记录里叠成两条一样的字。
   */
  function insertPersisted(message, seq) {
    if (!message || message.role !== 'user') return;
    const key = (seq === undefined || seq === null) ? '' : String(seq);
    if (key && view.messages.some((row) => String(row.seq) === key)) return;
    const row = Object.assign({}, message);
    if (key) row.seq = Number(seq);
    view.messages = view.messages.concat([row]);
  }

  /** 一个事件 → 它自己那一轮的 `live`，并安排一次重绘。
   *
   *  `run` 是**这条流的持有者**，不是"屏上那一轮"。两者的区别正是这一整块存在的
   *  理由：切到别的对话去之后，后台那一轮的 delta 照样要收进它自己那份 `live`
   *  （否则切回来只有一个半截的气泡），但**不许**动别人的窗口、别人的会话指针、
   *  别人的审查卡与记录。所以凡是要碰 `view.*` 的地方都先问 `onScreenRun(run)`。
   */
  function applyEvent(frame, run) {
    const live = run ? run.live : view.live;
    if (!live) return;
    const shown = onScreenRun(run) || !run;
    const data = frame.data || {};
    switch (frame.event) {
      case 'start': {
        // 新会话的 id 在这里才产生。**不在这里重新拉会话**：那一趟往返会打断
        // 正在往外冒的字，而这一轮结束后 `finish()` 本来就会整体对齐一次。
        const actual = data.sessionId || live.sessionId || view.sessionId || '';
        live.sessionId = actual;
        // 表里的键跟着换：新会话发送时键是空串，不换的话切走再切回就找不到它了。
        if (run) renameRun(run, actual);
        // **只有它正挂在屏上时**才动会话指针。切到别的对话去看记录之后，后台那一轮
        // 收到 `start` 帧把使用者拽回它身上，正是"切换会话显示当前会话内容"的反面。
        // 它自己的 id 记在 `live.sessionId` 里，切回来照用。
        if (shown) view.sessionId = actual;
        if (shown && data.config) view.config = data.config;
        // 新会话在这里才第一次拿到 config（建会话那一刻它就已经带着开关落库了）。
        if (shown) syncAutoFromConfig();
        live.maxSteps = data.maxSteps || live.maxSteps;
        live.stepsAtCeiling = data.stepsAtCeiling === true;
        live.policy = (data.config && data.config.policyId) || '';
        live.agent = (data.config && data.config.agentLabel) || '';
        break;
      }
      case 'turn':
        live.maxSteps = data.maxSteps || live.maxSteps;
        // 缺这个字段时保持"有上限"（旧服务端 + 新页面）：少报一个真上限，比多报一个
        // 假上限糟得多——真会上限会在一轮中途把作业切停。
        if (data.stepsAtCeiling !== undefined) {
          live.stepsAtCeiling = data.stepsAtCeiling === true;
        }
        break;
      case 'step':
        live.step = data.step || live.step;
        break;
      case 'delta':
        live.text += data.text || '';
        break;
      case 'reasoning':
        // 思考过程与正文分开累积。它们最终会一起落库（正文进 text、思考进
        // reasoning），所以这里分开存不会丢东西，只是各自显示。
        live.reasoning = (live.reasoning || '') + (data.text || '');
        break;
      case 'tool':
        live.tools.push({ label: data.label || data.name || '工具', ok: data.ok !== false,
                          brief: data.brief || '', durationMs: data.durationMs || 0 });
        // 上一次的文字已经定性了（模型说完话才去调工具），先落成已完成的一段，
        // 免得新一段文字接在旧句子后面看起来像同一句话
        if (live.text.trim()) { live.segments = (live.segments || []).concat([live.text]); live.text = ''; }
        break;
      case 'notice':
        live.notices.push(data.message || '');
        break;
      case 'error':
        live.error = data.error || '模型调用失败';
        // 自动执行的另一个入口：服务端把失败作为**帧**发回来（模型调用失败、
        // 步数到顶等），这时 `streamTurn` 的 promise 不会 reject —— 不在这里接，
        // 那一类失败就永远不会触发续跑，而这恰恰是线上最常见的一类。
        // 续跑只对**屏上那一轮**有意义：`sendMessage('继续')` 发往 `view.sessionId`，
        // 后台那一轮的失败若在这里排一次，"继续"就发到另一个会话里去了。
        if (shown) {
          auto.failed = true;
          scheduleAutoContinue(live.error);
        }
        break;
      case 'done':
        live.done = true;
        // 「这一轮为什么没有正文」：`length` 表示输出预算（max_tokens）在思考
        // 阶段就用完了——思考 token 也算 completion，正文于是为空。空着收尾时必须
        // 把原因带下去，否则界面上只剩一句"模型只做了思考或工具调用"。
        live.finishNote = data.finishNote || '';
        live.finishReason = data.finishReason || '';
        break;
      case 'subagent':
        // 绝不把 live.text 落成已完成段落：子智能体在跑的时候父轮并没有"说完一段话"，
        // 那一步是 `tool` 事件独有的语义，照搬会把父轮的正文切成好几块。
        applySubagent(live, data);
        break;
      case 'review':
        // 授权审查：工具调用被**挂起**在这里等人裁决。同一张单子两帧（先 pending
        // 后 settled），按 id 覆盖而不是追加 —— 否则界面上会出现两张一模一样的
        // 卡片，而其中一张永远停在"等待批准"。
        //
        // 它落进 `view.reviews`（视图级，不是这一轮的临时气泡）：卡片长在输入框
        // 上方那一格，刷新之后由 `loadReviews()` 从服务端取回同一份东西。
        // 单子属于**发起它的那个会话**（`public()` 里带 `sessionId`）：卡片长在输入框
        // 上方，切到别的对话去之后不该跟过来——那张单子是别人的工具调用在等裁决。
        // 切回去时 `loadReviews()` 会从服务端取到它，不会丢。
        if (shown || !(data.review && data.review.sessionId)) applyReview(data.review || {});
        break;
      case 'persist':
        // 插话的那一句落库了（服务端 `_chat_turn_events` 收到 `persist` 就写一条
        // `user` 消息）。它要**马上**出现在记录里，见 `insertPersisted`。
        // 插话的那一句落库了（服务端 `_chat_turn_events` 收到 `persist` 就写一条
        // `user` 消息）。它属于这一轮那个会话，切走了就不该插进别人的记录。
        if (shown) insertPersisted(data.message, data.seq);
        break;
      default:
        break;      // 其余 persist 等事件不影响实时视图：最终记录以服务端为准
    }
    // 后台那一轮不驱动重绘：`renderLive` 画的是 `view.live`，它没变，白跑一趟。
    if (shown) scheduleLiveRender();
  }

  /* -------------------------------------------------------- 子智能体派活 ----
   *
   * 父轮派出的子智能体在跑的时候，会在同一条流上插播 `subagent` 帧（服务端由
   * `pentestd/subagent.py` 发出，经 `delegate` 生成器原样转发）。契约里的四种
   * phase：`start`（开始跑）/ `step`（第 N 次模型调用）/ `tool`（执行完一次工具）/
   * `done`（跑完了）。
   *
   * 两条约束是这个区块的全部难点：
   *
   *   1) **同一个 `index` 是一个子智能体**，所以后续事件是"更新那一行"，不是
   *      "再开一行"。按到达顺序画的话，一个跑了 2 步、调了 1 次工具的子智能体会
   *      占 4 行，而 `total` 说这次只派了 3 个——界面自己跟自己矛盾，且没人能从
   *      屏幕上看出哪几行其实是同一个。
   *   2) **认不出来的 phase 一律忽略**。事件字段是后端往后加的，前端不可能比它
   *      先知道；这里抛异常的代价不是"少画一行"，而是整条流的消费循环被打断——
   *      屏幕上正往外冒的字会停住、还弹一条错误，而原因只是一帧多余的进度事件。
   *      这不是假想：后端此刻就会发第五种 `note`（报告格式没跟上时），它不在契约
   *      的四类里。
   */

  const SUBAGENT_PHASES = ['start', 'step', 'tool', 'note', 'done'];
  const SUBAGENT_STATE = { running: '跑动中', done: '已完成', failed: '失败' };

  /** 子智能体行下面那句原因，连同它的前缀。
   *
   *  `row.error` 装的是**两件不同的事**：跑挂了（`state === 'failed'`），
   *  和"跑完了但被截断"（`state === 'done'`，例如父轮预算用尽，子智能体
   *  用已有的数据交了报告）。两者都带原因，但只有前者是失败。
   *
   *  一律写成"未完成："会让一行同时挂着绿色的「已完成」和红色的「未完成：」——
   *  而这一行正是使用者判断"这趟到底算不算数"的地方，自相矛盾比不显示更糟。
   *  所以前缀跟状态走，颜色也跟状态走（见 style.css 里 `.done` 那条）。
   */
  function subagentErrorLine(row) {
    return (row.state === 'done' ? '受限：' : '未完成：') + row.error;
  }

  function newSubagentRow(index) {
    return { index: index, total: 0, role: '', policy: '', toolCount: 0,
             state: 'running', steps: 0, durationMs: 0, summary: '', error: '',
             note: '', tools: [] };
  }

  /** 认领这个 `index` 的那一行；没有就新建，并把这一帧里的公共字段吃掉。 */
  function subagentRow(live, data) {
    const index = Number(data.index) || 0;
    const rows = live.subagents || (live.subagents = []);
    let row = rows.filter((one) => one.index === index)[0];
    if (!row) {
      row = newSubagentRow(index);
      rows.push(row);
    }
    if (data.total) row.total = Number(data.total) || row.total;
    if (data.role) row.role = String(data.role);
    if (data.policy) row.policy = String(data.policy);
    if (data.toolCount) row.toolCount = Number(data.toolCount) || 0;
    return row;
  }

  /** 一帧 `subagent` → 它那一行。认识返回 true；不认识返回 false 且什么都不动。 */
  function applySubagent(live, data) {
    const phase = String(data.phase || '');
    if (SUBAGENT_PHASES.indexOf(phase) < 0) return false;   // 兜底：忽略，绝不抛
    const row = subagentRow(live, data);
    if (phase === 'step') {
      row.state = 'running';
      if (data.step !== undefined) row.steps = Number(data.step) || 0;
    } else if (phase === 'tool') {
      row.state = 'running';
      const tool = { label: data.name || '工具', ok: data.ok !== false,
                     brief: data.brief || '',
                     durationMs: Number(data.durationMs) || 0 };
      // 派活类工具的那一帧带 jobId（`pentestd/subagent.py` 从 `data.jobId` 抄来）。
      // 有它，右栏才画得出"这个子智能体下发了哪条任务"和反向的"这条任务是谁派的"。
      // 没有它就不加这个键——界面按"有没有 jobId"决定那一片点不点得动。
      if (data.jobId) tool.jobId = String(data.jobId);
      row.tools.push(tool);
    } else if (phase === 'note') {
      // 报告没按三行格式给出（`subagent.py` 在 `format:"free"` 时发这一帧）。
      // 它不是错误——内容还在，只是没排好版——所以只挂一行说明，**不改状态**：
      // 标成失败会让用户以为这个子智能体什么都没查出来，而实际上它查到了。
      row.state = 'running';
      if (data.message) row.note = String(data.message);
    } else if (phase === 'done') {
      // `ok` 是成败的唯一判据（契约：它只在 tool 与 done 上有意义）。`status` 只
      // 用来区分"自己跑挂"和"被父轮中止"，两者都不算成功，所以不改变状态标签。
      row.state = data.ok === false ? 'failed' : 'done';
      if (data.steps !== undefined) row.steps = Number(data.steps) || 0;
      if (data.durationMs !== undefined) row.durationMs = Number(data.durationMs) || 0;
      if (data.summary) row.summary = String(data.summary);
      if (data.error) row.error = String(data.error);
    } else {
      row.state = 'running';      // 'start'：行刚建出来，状态本来就是 running
    }
    return true;
  }

  let liveFrame = null;

  function scheduleLiveRender() {
    if (liveFrame) return;
    const run = () => {
      liveFrame = null;
      renderLive();
    };
    liveFrame = window.requestAnimationFrame ? window.requestAnimationFrame(run)
      : setTimeout(run, 60);
  }

  /** 只重画最后那一条"正在生成"的气泡，不整片重排。
   *
   *  整片重排会把用户往上滚的位置顶回去——看着正在写的回答却读不了前面任何东西。
   */
  /** 正在生成的气泡由**三个固定槽位**组成：head / reasoning / body，尾巴排在 body 之后。
   *
   *  旧写法每帧 `clear(node)` 再整块重画：哪怕思考块被摘下来复用，它也是每 16ms
   *  从文档里摘下再插回一次。而思考块是使用者会滚轮、会拖滚动条、会选文字读的对象——
   *  滚轮落下的那一格常常正好打在"这一瞬间不在文档里"的元素上，拖动滚动条更是在半路
   *  被换掉，表现就是「能打开，但滚不动」。槽位固定之后，一帧里只有文字的增量被追加。
   */
  function renderLive() {
    if (!dom.log || !view.live) return;
    let node = dom.log.querySelector('.chat-live');
    if (!node) {
      node = el('article', 'chat-msg chat-msg-ai chat-live');
      dom.log.append(node);
    }
    // 槽位只建一次：head 与 body 是**常驻节点**，每帧只换它们自己的内容；思考块插在
    // 两者之间，也从不再搬动。`__live` 是这块气泡自己的标记，不是会话状态。
    if (!node.__live) {
      node.__live = true;
      clear(node);
      node.__head = el('header', 'chat-msg-head');
      node.__body = el('div', 'chat-msg-body');
      node.append(node.__head, node.__body);
    }
    // 结束时摘掉光标：还闪着光标会让人以为它随时会继续说话
    node.className = 'chat-msg chat-msg-ai chat-live'
      + (view.live.done || view.live.error ? ' chat-live-done' : '');
    const keepAtBottom = nearBottom();
    refill(node.__head, liveHead());
    paintLiveReasoning(node);
    paintLiveBody(node);
    // 尾巴（子智能体 / 工具芯片）每帧重建：它们没有需要保留的交互状态，而且排在
    // **思考块之后**——重建动不到正在滚动、或正在选中文字的那一块。
    while (node.__body.nextSibling) node.removeChild(node.__body.nextSibling);
    // 审查单**不画在这里**：它由 `renderReviews` 画在记录末尾（更早是画在气泡里，
    // 那样等于只活在客户端、刷新一次就没了；而单子还在服务端等人批）。
    // 那一块同样由 SSE 驱动，所以实时性不受影响。
    // 子智能体区块排在正文之后、工具芯片之前：它讲的是"这一轮在等谁"，
    // 而芯片讲的是"这一轮自己调了什么"——先后顺序与发生顺序一致。
    if (view.live.subagents && view.live.subagents.length) {
      node.append(subagentBlock(view.live.subagents));
    }
    const failed = view.live.tools.filter((tool) => !tool.ok).length;
    if (view.live.tools.length || view.live.notices.length) node.append(liveSide(failed));
    // 侧栏那一块也跟着走：子智能体在跑的那几分钟里，它是唯一能一直看见的进度，
    // 而它不必等整轮结束（这一轮整个跑完才落库）。这里是 rAF 节流的，
    // 所以最多一帧重画一次，代价与主区那一块同量级。
    renderSideSubagents();
    if (keepAtBottom) dom.log.scrollTop = dom.log.scrollHeight;
  }

  /** 把刚建好的元素的内容搬进常驻槽位，**槽位自己的身份不动**。 */
  function refill(slot, fresh) {
    clear(slot);
    while (fresh.firstChild) slot.appendChild(fresh.firstChild);
    return slot;
  }

  /** 思考块：**建一次，之后只追加**。
   *
   *  两件事必须同时成立，缺一个都会退回「一直在刷新 / 滚不动」：
   *    - 节点身份不变：不摘、不重插，位置固定夹在 head 与 body 之间；
   *    - 文字只在末尾追加（`syncReasoning`），字数标签就地改。
   *
   *  滚动只在"本来就在底部"时跟尾巴。使用者把轮子往上翻之后这里**一格都不碰**它——
   *  不然每帧都会把他拽回最新那一段，看起来像滚不动。
   */
  function paintLiveReasoning(node) {
    const text = String(view.live.reasoning || '').trim();
    let box = node.__reasoning || null;
    if (!text) {
      if (box) {
        box.remove();
        node.__reasoning = null;
      }
      return;
    }
    let atBottom = true;
    if (box) {
      const pre = box.querySelector('.chat-reasoning-text');
      atBottom = pre ? pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24 : true;
      syncReasoning(box, text, !view.live.done);
    } else {
      box = reasoningBlock(text, !view.live.done, view.live.reasoningOpen);
      if (!box) return;
      // toggle 只在造节点时挂一次：节点往后的每一帧都复用。
      box.addEventListener('toggle', () => {
        if (view.live) view.live.reasoningOpen = box.open;
      });
      node.__reasoning = box;
      node.insertBefore(box, node.__body);
    }
    const shown = box.querySelector('.chat-reasoning-text');
    if (shown && box.open && atBottom) shown.scrollTop = shown.scrollHeight;
  }

  function nearBottom() {
    if (!dom.log) return false;
    return dom.log.scrollHeight - dom.log.scrollTop - dom.log.clientHeight < 120;
  }

  /** 本轮**实际**走了多少步、调用了几个工具。
   *
   *  这一对是这一轮唯一"发生过的事实"，与预算无关。收尾之后原来只剩「已完成」三个
   *  字，于是"这一轮到底走了多少步"从来没有地方显示过——它只活在流里的 `step` 事件
   *  上，刷新一次就没了。
   */
  function ranSteps(live) {
    const calls = live.tools.length ? ` · 已调用 ${live.tools.length} 次` : '';
    if (!live.step) return calls;
    return ` · 本轮共 ${live.step} 步` + calls;
  }

  /** 跑动中的读数。上限**只在它真会拦住这一轮时**才报。
   *
   *  预算等于平台上限（`stepsAtCeiling`，比如把预设的步数调到 99999）时不再显示：
   *  「第 18 步 / 最多 99999 步」里的后一半拦不住任何人，它只是把配置念一遍，而
   *  前一半已经就是"本轮实际步数"。有真预算（6/10/12/40…）时照旧显示——那个数会
   *  在一轮中途把作业切停，是使用者唯一能提前看到的信号。
   */
  function liveProgress(live) {
    const cap = (live.maxSteps && !live.stepsAtCeiling)
      ? ` / 最多 ${live.maxSteps} 步` : '';
    return `第 ${live.step} 步` + cap
      + (live.tools.length ? ` · 已调用 ${live.tools.length} 次` : '');
  }

  function liveHead() {
    const head = el('header', 'chat-msg-head');
    head.append(el('span', 'chat-msg-role', 'AI 助手'));
    const live = view.live;
    if (live.error) {
      head.append(el('span', 'chat-live-badge bad', '这一轮没有跑完' + ranSteps(live)));
    } else if (live.done) {
      head.append(el('span', 'chat-live-badge', '已完成' + ranSteps(live)));
    } else if (live.step) {
      head.append(el('span', 'chat-live-badge', liveProgress(live)));
    } else {
      head.append(el('span', 'chat-live-badge', '正在思考…'));
    }
    if (live.agent) head.append(el('span', 'chat-msg-at', live.agent));
    return head;
  }

  function liveBody() {
    const body = el('div', 'chat-msg-body');
    const live = view.live;
    const segments = live.segments || [];
    segments.forEach((piece) => {
      // 选项块在流里也不显示成代码：切掉它，只画正文那一半。按钮要等整轮结束
      // 才出现（跑到一半点不了），而把围栏原样画出来则是"让你自己抄"。
      const part = splitOptions(piece);
      const text = part.options.length ? part.body : piece;
      if (text) body.append(ui().renderMarkdown(text));
    });
    if (live.error) {
      body.append(el('p', 'chat-msg-error-text', '这一轮没有跑完：' + live.error));
    } else if (live.text) {
      const parts = splitOptions(live.text);
      if (parts.body) body.append(ui().renderMarkdown(parts.body));
      else if (!live.done) body.append(el('p', 'chat-typing', '正在推理与调用工具…'));
      else body.append(el('p', 'muted', '这一轮只给了选项：在下方输入框里点一个就开始。'));
    } else if (live.reasoning && !live.done) {
      // 只吐了思考、正文还没开始：说"正在推理"而不是"正在调用工具"——
      // 后者会让用户以为是平台慢，实际是模型在想。
      body.append(el('p', 'chat-typing', '正在推理…'));
    } else if (!live.tools.length && !live.done) {
      body.append(el('p', 'chat-typing', '正在推理与调用工具…'));
    } else if (live.done && !segments.length) {
      // 跑完了、正文一个字都没有：留空是**最坏**的一版——框还在，却分不清是
      // "模型没说话"还是"界面没画出来"。而这时 head 已经写着「已完成」，
      // 再补一句「正在推理…」是在报一件不会再发生的事。
      body.append(el('p', 'muted', emptyTurnNote(live)));
    }
    return body;
  }

  /** 正文里"不会再变"的那一截到哪儿为止。
   *
   *  切点只取**代码围栏之外的最后一个空行**。这不是启发式：`renderMarkdown`
   *  （`web/app.js:1587`）逐块解析，一个空行既结束段落，也结束列表、表格与引用，
   *  所以按空行切开再分别渲染，与整段一次渲染**结果完全相同**——包括"宽松列表"里
   *  的编号：渲染器在空行处就结束列表，下一项本来就从 1 重新开始。
   *  围栏是唯一不能让空行切开的块：`<pre>` 里的空行是内容，切开会变成两个代码块。
   */
  function stablePrefix(text) {
    const lines = String(text || '').split('\n');
    let fence = false, offset = 0, cut = 0;
    for (let i = 0; i < lines.length; i += 1) {
      if (/^\s*```/.test(lines[i])) fence = !fence;
      offset += lines[i].length + (i < lines.length - 1 ? 1 : 0);
      if (!fence && lines[i].trim() === '' && i < lines.length - 1) cut = offset;
    }
    return cut;
  }

  /** 正文此刻是"还在长"（stream），还是一种一次性的占位/说明。
   *
   *  只有 `stream` 走增量；其余几种都是几十个字的固定片语，而且彼此是离散切换，
   *  整块重画（`liveBody()`）更省事，也不会让"正在推理…"这类提示闪。
   */
  function bodyMode(live, parts) {
    if (live.error) return 'error';
    if (parts.body) return 'stream';
    if (parts.options.length) return live.done ? 'options-only' : 'working';
    if (live.reasoning && !live.done) return 'thinking';
    if (!live.tools.length && !live.done) return 'working';
    if (live.done && !(live.segments || []).length) return 'empty';
    return 'idle';
  }

  /** 正文：**已经定型的块只渲染一次，每帧只重画还在长的尾巴。**
   *
   *  旧写法每帧 `refill(node.__body, liveBody())`——整篇回答（每一段、每个代码块、
   *  每张表格）都重新解析、重新建节点。长回答里这等于每 16ms 抖一次整页正文：
   *  选中的文字、刚滚到的位置都可能被换掉，而真正在变的只有最后那一段。
   *  现在 `stablePrefix()` 把"空行之后不会再变"的部分定格，只有尾巴逐帧重画；
   *  工具调用落定的段落（`live.segments`）同样只画一次。
   */
  function paintLiveBody(node) {
    const live = view.live;
    const body = node.__body;
    const state = node.__bodyState || (node.__bodyState = {
      mode: '', segs: 0, cut: 0, source: '', prefix: [], tail: [], tailText: null });
    const segments = live.segments || [];
    const parts = splitOptions(live.text || '');
    const mode = bodyMode(live, parts);
    if (mode !== 'stream') {
      if (state.mode !== mode) {
        state.mode = mode;
        state.segs = segments.length;
        state.cut = 0; state.source = '';
        state.prefix = []; state.tail = []; state.tailText = null;
        refill(body, liveBody());
      }
      return;
    }
    const text = parts.body;
    // 进入正文流（或段落数变了）：把已定型的段落画一次，之后不再碰它们
    if (state.mode !== 'stream' || state.segs !== segments.length) {
      state.mode = 'stream';
      state.segs = segments.length;
      state.cut = 0; state.source = '';
      state.prefix = []; state.tail = []; state.tailText = null;
      clear(body);
      segments.forEach((piece) => {
        const part = splitOptions(piece);
        const done = part.options.length ? part.body : piece;
        if (done) body.append(ui().renderMarkdown(done));
      });
    }
    if (!text.startsWith(state.source)) {
      // 只有两条路会走到这里：正文被改写，或末尾的选项围栏刚被切掉。
      state.prefix.forEach((item) => item.remove());
      state.tail.forEach((item) => item.remove());
      state.prefix = []; state.tail = []; state.cut = 0; state.tailText = null;
    }
    state.source = text;
    const cut = stablePrefix(text);
    if (cut > state.cut) {
      const nodes = Array.from(ui().renderMarkdown(text.slice(state.cut, cut)).childNodes);
      const anchor = state.tail.length ? state.tail[0] : null;
      nodes.forEach((item) => body.insertBefore(item, anchor));
      state.prefix = state.prefix.concat(nodes);
      state.cut = cut;
    }
    const rest = text.slice(state.cut);
    if (rest !== state.tailText) {
      state.tail.forEach((item) => item.remove());
      state.tail = Array.from(ui().renderMarkdown(rest).childNodes);
      state.tail.forEach((item) => body.append(item));
      state.tailText = rest;
    }
  }

  function liveSide(failed) {
    const box = el('div', 'chat-live-tools');
    view.live.notices.forEach((text) => box.append(el('p', 'chat-live-notice', text)));
    view.live.tools.forEach((tool) => {
      const chip = el('span', 'chat-live-chip' + (tool.ok ? '' : ' bad'));
      chip.append(el('span', 'chat-live-chip-name', tool.label));
      chip.append(el('span', 'chat-live-chip-time', ui().formatDuration(tool.durationMs)));
      if (!tool.ok && tool.brief) chip.title = tool.brief;
      box.append(chip);
    });
    if (failed) box.append(el('p', 'chat-live-notice bad', `${failed} 次调用失败`));
    return box;
  }

  /* ------------------------------------------------------------ 授权审查 ----
   *
   * 服务端在工具执行**之前**把这一条调用挂起，发两帧 `review`（`pending` 与
   * `settled`）。界面的职责只有两件：把"为什么问你"讲清楚，以及把这个回答送回
   * 去（`POST /reviews/{id}`）。
   *
   * **它为什么不再长在气泡里。** 气泡是 `view.live`，纯客户端状态：刷新一次卡片
   * 就没了，而单子还在库里 `pending`、等它的那个轮次还在服务端跑着。使用者因此
   * 进过一个死角——卡片没了批不了，想发下一轮又被入口闸以 409 挡住。现在卡片是
   * **服务端事实的投影**：`loadReviews()` 每次载入会话取一次，`applyReview()` 接
   * SSE 那两帧，两者写进同一个 `view.reviews`。
   *
   * 三件必须在界面上说清的事，都不是装饰：
   *
   *   1) **这一轮停在这里。** 不说的话，等待看起来就是"平台卡了"。
   *   2) **超时按不执行处理。** 使用者有权知道放着不管的后果。
   *   3) **刷新不会弄丢它。** 这一句从前是反的（"离开这一页会作废"），而那既
   *      不成立、又让人白白去等超时：断开只意味着看的人走了。说错方向的代价比
   *      不说还大，所以这里有契约钉着（见 tests/test_review_client.py）。
   */
  function applyReview(review) {
    if (!review || !review.id) return;
    if (!view.reviews[review.id]) view.reviewOrder.push(review.id);
    view.reviews[review.id] = review;
    renderReviews();
  }

  /** 从服务端取回"这张会话上还有哪些审查单在等人批"。
   *
   *  刷新页面之后**只有这一条路**能把卡片找回来。取失败不该让会话开不了：卡片
   *  没了，但对话本身还能看、还能发——所以这里吞掉异常，只把那一格清空。
   */
  async function loadReviews() {
    const sessionId = view.sessionId;
    view.reviews = {};
    view.reviewOrder = [];
    if (!sessionId) { renderReviews(); return; }
    try {
      const payload = await ui().apiGet(
        `/reviews?status=pending&sessionId=${encodeURIComponent(sessionId)}`);
      // 整份替换而不是合并：服务端说的是"现在还有这些"，本地那份可能含已经裁决
      // 过的单子（它们在记录里，不在这一格）。
      asArray(payload.reviews).forEach((row) => {
        // 查询条件是 `status=pending`，但**库里的 `pending` 不等于"还在等人"**：
        // 推着单子走的是等它的那个进程，那个进程一死（控制面重启、崩溃），单子就
        // 停在 `pending`，而服务端会把它按 `expired` 报回来（见 `approval.public`）。
        // 这一类不收进这一格——这一格放的是"现在要你回答的"，已经没有等待者的
        // 那些另有那一行提示。收进来只会多一张永远不动的"已作废"卡片，而它上面
        // 刚印着"到时间会自动收掉"。
        if (!row || !row.id || row.pending === false) return;
        view.reviews[row.id] = row;
        view.reviewOrder.push(row.id);
      });
    } catch (error) {
      // 故意留空：这一格取不回来不是"对话坏了"。
    }
    renderReviews();
  }

  /** 把这一批审查单画到输入框上方那一格。
   *
   *  分三类，**顺序就是使用者该处理的顺序**：
   *    - `waiting`：还等着人批的 —— 有按钮，排最前；
   *    - `done`：这一轮里刚裁决过的 —— 显示结果，让"我点过了"看得见；
   *    - `orphan`：库里有、但**没有等待者**了 —— 不给按钮。
   *
   *  最后一类是要紧的：没有等待者意味着发起它的那一轮已经不在了（控制面重启过，
   *  或那一轮收了），批准它不会有命令执行。画一个按下去什么都不发生的按钮，比
   *  不画更坏 —— 服务端在 `decide_review` 里也会拒掉这种裁决（409），两处判据
   *  同一个来源（`approval.awaited`）。
   *
   *  **`done` 只停留，不常驻**：留住 `DONE_LINGER_MS` 让人看见结果（"我点过了"），
   *  然后自己走掉。这一格的契约是"这一页唯一要人动手的东西"，而一张写着「已批准」
   *  的卡片不要人做任何事 —— 它占着输入框上方那一条、把输入框往下推，却无可做之事。
   *  结果不会因此丢失：它已经在对话里（审查通过后工具照常执行，工具结果落在记录区）。
   *
   *  ⚠️ `DONE_LINGER_MS + DONE_FADE_MS` **必须小于** `refreshTranscriptSoon` 那个
   *  最长延迟（9000）——否则"卡片自己走了"与"记录区重取之后顺手清掉了"在时间上
   *  分不开，`probe_boot_chain` 第五场就失去了判别力（它量的正是"没有别的原因时
   *  卡片走没走"）。那条关系有契约钉着，改这个数会当场红。
   */
  const DONE_LINGER_MS = 6000;
  const DONE_FADE_MS = 600;

  function renderReviews() {
    /* 画在**输入卡片上沿那一层浮层**里（m18801）。
     *
     * 原先它 append 到 `#chat-log` 末尾。两个问题都是"占流内高度"带来的：
     *   1) 待批时记录区最后几行被顶到卡片上沿底下，读那几行要先把卡片批掉；
     *   2) 裁决过的卡片还要停留 6 秒再淡出（`DONE_LINGER_MS`），这 6 秒里记录区
     *      连着抖两次（长出来、缩回去），正读到末尾的人被推来推去。
     * 浮层不占那一列的高度：它盖在卡片上沿之上，记录区末尾的让位由
     * `syncChatTail()` 按它的上沿量（与坞同一套做法）。
     */
    const box = dom.reviewFloat;
    if (!box) return;
    // 这一层是**我们自己的一格**：它不属于任何一条落库消息（`renderLog` 的
    // `clear(log)` 不会替我们带走它），所以每次画之前自己清一次。
    clear(box);
    const rows = (view.reviewOrder || [])
      .map((id) => view.reviews[id]).filter(Boolean);
    const waiting = rows.filter((row) => row.pending && row.awaiting);
    const orphan = rows.filter((row) => row.pending && !row.awaiting);
    const done = rows.filter((row) => !row.pending);
    if (!waiting.length && !orphan.length && !done.length) {
      box.hidden = true;
      // 空掉之后把钉住的高度摘掉，否则下一轮打开会先按旧值画一帧再跳。
      pinReviewHeight();
      markReviewsSettled();
      return;
    }
    // 一句"为什么这里会停住"：卡片上写着要批什么，但"不批会怎样"必须说清——
    // 这一轮是真的停在这里等，不是正在后台跑。
    if (waiting.length) {
      box.append(el('p', 'chat-review-intro',
        '这一轮停在这里等你回答：下面这条命令点「允许一次」才会执行。'));
    }
    waiting.forEach((row) => box.append(reviewCard(row)));
    done.forEach((row) => box.append(reviewCard(row)));
    if (orphan.length) {
      box.append(el('p', 'chat-review-orphan',
        `另有 ${orphan.length} 张审查单已经没有等待者了（发起它的那一轮已经结束，`
        + '或控制面重启过），批准它们不会有命令执行；到时间会自动收掉。'));
    }
    box.hidden = false;
    /* 高度钉在“一张卡”上（m20719）：量完再决定这一层多高，见 `pinReviewHeight()`。
       这一层自己可以滚，多出来的卡片在层内滚，不再把浮层撑高。 */
    pinReviewHeight();
    /* 只在**这一批单子真的变了**时挪一次滚动位置。
     *
     * 原来是无条件 `box.scrollTop = box.scrollHeight`：心跳每 3 秒重画一次这一格，
     * 每次都把滚动条拽回末尾 —— 正在浮层里往回翻看第二、第三张单子的人被反复推走。
     * 而“批次变了”正是唯一需要重新定位的时刻：新来一张待批的（要让人看见它）、
     * 或刚裁决掉一张（点完必须看见「已批准」）。 */
    const key = rows.map((row) => row.id + (row.pending ? 'p' : 'd')).join('|');
    if (key !== view.reviewsKey) {
      view.reviewsKey = key;
      scrollReviewsIntoView(box, done.length ? 'done' : 'waiting');
    }
    markReviewsSettled();
  }

  /** 审查层刚变过（有内容 / 空了 / 高度变了）：让记录区的末尾重新量一次。
   *
   *  末尾让位是 `app.js` 的 `syncChatTail()` 按**实测矩形**写的，而浮层的出现与
   *  消失都不触发那条 ResizeObserver 一定跟得上（浮动元素脱出流内，父盒尺寸可能
   *  一点没变）。所以画完主动叫一次。它内部有去重（值没变就不写样式），多叫无害。
   */
  function markReviewsSettled() {
    const api = ui();
    if (api && typeof api.syncChatTail === 'function') api.syncChatTail();
  }

  /** 把浮层的高度**钉在“一张卡”上**（m20719）。
   *
   *  为什么要这一条：`renderReviews()` 把待批的单子全部纵向堆进同一层浮层，层高
   *  因此等于“卡片数 × 卡片高”。同时挂三四条时它能把半屏占掉，而使用者此刻要做的事
   *  只有一件——回答**最上面那一张**。高度钉住之后，“还有几张”体现为层内可滚，
   *  不再体现为浮层越长越高。
   *
   *  为什么在 JS 里量、不写成 CSS：卡片高随 headline / reasons 的行数与窗口宽度变，
   *  写死 px 一定对不上；而 CSS 没有“第一个子元素多高”这种表达。所以量一次实测高度，
   *  写成自定义属性让 `.chat-review` 的 `max-height` 去用（见 style.css 那一条）。
   *
   *  量的是**卡片自己的矩形**，不是浮层的：浮层是 flex 容器且卡片没有 `flex` 简写，
   *  `min-height: auto` 让卡片下限停在 min-content —— 即使浮层正被 `max-height` 裁着，
   *  卡片量到的仍是它**需要**的高度。这也是这条路不需要“先清掉自己的值再量”的原因
   *  （`theme.js` 的 `paintAlpha` 要清，是因为它读的正是自己写进去的那个 CSS 值）。
   *
   *  幂等、不动 DOM：只写一个自定义属性。窗口 resize 时重算也安全——
   *  `decideReview()` 期间按钮是 disabled 的，重建 DOM 会把那个状态弄丢。
   */
  function pinReviewHeight() {
    const box = dom.reviewFloat;
    if (!box) return;
    const card = box.hidden ? null : box.querySelector('.chat-review-card');
    if (!card) {
      // 收起 / 只剩一句说明时不留旧值：下次打开会先按旧高度画一帧再跳。
      box.style.removeProperty('--review-pin');
      return;
    }
    const style = window.getComputedStyle(box);
    const gap = parseFloat(style.rowGap || style.gap) || 0;
    let height = card.getBoundingClientRect().height;
    const intro = box.querySelector('.chat-review-intro');
    if (intro) height += intro.getBoundingClientRect().height + gap;
    // 兜底封顶与 CSS 里那条一致：窗口再矮也不许把输入卡片挤出视野。
    const cap = Math.min(window.innerHeight * 0.52, 460);
    box.style.setProperty('--review-pin', Math.round(Math.min(height, cap)) + 'px');
  }

  /** 把浮层滚到“该看的那一张”（m20719）。
   *
   *  浮层高度钉在一张卡上之后，第二张起就在**折叠线以下**了 —— 不滚过去等于
   *  “排队了两张，界面上只看得见一张，也没有任何提示”。所以这一批单子一变就挪一次：
   *  有刚裁决过的卡片时滚到它（点完必须看见「已批准」），否则滚回第一张待批的。
   *
   *  `offsetTop` 直接可用：`.chat-review` 是 `position: absolute`，它本身就是卡片
   *  的 offsetParent，那个值正好是“相对浮层内容顶端”的距离。
   */
  function scrollReviewsIntoView(box, kind) {
    const card = box.querySelector(kind === 'done'
      ? '.chat-review-card.chat-review-done' : '.chat-review-card');
    box.scrollTop = card ? Math.max(0, card.offsetTop) : 0;
  }

  function reviewCard(review) {
    const card = el('div', 'chat-review-card'
      + (review.pending ? '' : ' chat-review-done'));
    const head = el('div', 'chat-review-head');
    head.append(el('span', 'chat-review-title',
      review.pending ? '需要你确认这条命令' : review.statusLabel));
    if (review.tool) head.append(el('span', 'chat-review-tool', review.tool));
    card.append(head);
    if (review.headline) card.append(el('p', 'chat-review-headline', review.headline));
    if (review.reasons && review.reasons.length) {
      const list = el('ul', 'chat-review-reasons');
      review.reasons.forEach((text) => list.append(el('li', '', text)));
      card.append(list);
    }
    if (review.pending && review.awaiting) {
      card.append(el('p', 'chat-review-wait',
        `这一轮停在这里等你回答。${review.secondsLeft} 秒内不回答会按「不执行」`
        + '处理；刷新或换一个页签打开这个会话，它都还会在。'));
      const row = el('div', 'chat-review-actions');
      const approve = el('button', 'btn chat-review-primary', '批准并执行');
      const deny = el('button', 'btn', '拒绝');
      approve.type = 'button';
      deny.type = 'button';
      approve.addEventListener('click', () => decideReview(review.id, true, card));
      deny.addEventListener('click', () => decideReview(review.id, false, card));
      row.append(approve, deny);
      card.append(row);
    } else if (review.note) {
      card.append(el('p', 'chat-review-note',
        `${review.decidedBy || '系统'}：${review.note}`));
    }
    if (!review.pending) scheduleReviewLeave(review.id, card);
    return card;
  }

  /** 把一张**已经裁决过**的卡片在 `DONE_LINGER_MS` 之后请出那一格。
   *
   *  两步，不是一步：
   *    1. 先加 `.chat-review-leaving` 淡出 `DONE_FADE_MS`（那一下是"它走了"的可见
   *       交代；硬切掉会像界面出了错）；
   *    2. 再从 `view.reviews` / `view.reviewOrder` 里**删掉它**并重画。
   *
   *  第 2 步不能省：那一格每次都从这两个结构重建，只删 DOM 的话下一次
   *  `renderReviews()`（心跳、SSE 的 `settled`、切会话都会触发）会把它捞回来，
   *  表现成"卡片消失一秒又回来"。删干净之后，若没有别的单子，`slot.hidden = true`
   *  会把整格收掉 —— 空着的一格不留边距，也不留"这里有东西但没显示"的错觉。
   */
  function scheduleReviewLeave(reviewId, card) {
    window.setTimeout(() => {
      card.classList.add('chat-review-leaving');
      window.setTimeout(() => {
        delete view.reviews[reviewId];
        view.reviewOrder = (view.reviewOrder || []).filter((id) => id !== reviewId);
        renderReviews();
      }, DONE_FADE_MS);
    }, DONE_LINGER_MS);
  }

  /** 送一个裁决上去。**不做乐观更新**：卡片等 SSE 的 `settled` 帧来改自己。
   *
   *  先改界面的写法在这里是错的 —— 批了之后服务端可能因为"已经超时"而回 409，
   *  那时界面显示"已批准"而命令并没有跑，是最坏的一种不一致。
   *  **保留接口端点名不写死中文**：这张卡片的按钮只发请求，措辞留在服务端。
   */
  async function decideReview(reviewId, approve, card) {
    const buttons = Array.from(card.querySelectorAll('button'));
    buttons.forEach((button) => { button.disabled = true; });
    try {
      const payload = await ui().apiPost(
        `/reviews/${encodeURIComponent(reviewId)}`, { approve: !!approve });
      card.classList.add('chat-review-sending');
      if (payload && payload.review) applyReview(payload.review);
      // **这一页上没有活的流时要说一声。** 那说明这次点击发生在刷新之后：服务端
      // 那一轮还在跑，裁决会让它继续，而它跑完的结论不会自己走到这一页上。
      if (!view.live) {
        ui().showError('已上报裁决。这一轮会在服务端继续，结果稍后出现在对话记录里'
          + '（可点「刷新会话」立刻看）。');
        refreshTranscriptSoon();
      }
    } catch (error) {
      buttons.forEach((button) => { button.disabled = false; });
      ui().showError('审查裁决失败：' + error.message);
    }
  }

  /** 裁决之后把对话记录再取一次 —— 只在"这一页没有活的流"时用。
   *
   *  取三次而不是一次：服务端的 `wait` 是轮询的（裁决写进库到那一轮真的往下走
   *  之间隔着零点几秒），而紧接着工具本身的耗时是未知的。所以这里**只承诺"再取
   *  一次"**、不承诺"一定取到了结果"——取早了用户看到的是旧记录，比不刷新更让人
   *  困惑，所以提示语里另外给了「刷新会话」这条手动路。
   *  每一次都重新判 `view.live`：期间要是又发了一轮，就不再插手。
   */
  function refreshTranscriptSoon() {
    const sessionId = view.sessionId;
    if (!sessionId) return;
    [1200, 4000, 9000].forEach((delay) => {
      window.setTimeout(() => {
        if (view.sessionId === sessionId && !view.live) openSession(sessionId);
      }, delay);
    });
  }

  /** 子智能体派活区块：挂在"正在生成"的那条气泡里的独立一块。
   *
   *  **不做折叠**：派活是同步的（父轮要等它们全部跑完），这是那几分钟里唯一的进度
   *  来源；折成一行等于让人只能看着"3 个子智能体在跑"，而他想知道的是哪一个走到了
   *  哪一步、哪一个挂了、为什么挂。行数等于本次派活的数量，不随事件数量增长，
   *  所以它不会越长越乱。
   */
  function subagentBlock(rows) {
    const box = el('div', 'chat-subagents');
    const head = el('div', 'chat-subagents-head');
    const running = rows.filter((row) => row.state === 'running').length;
    const failed = rows.filter((row) => row.state === 'failed').length;
    head.append(el('span', 'chat-subagents-title', '子智能体'));
    const parts = [rows.length + ' 个'];
    if (running) parts.push('跑动中 ' + running);
    if (failed) parts.push('失败 ' + failed);
    head.append(el('span', 'chat-subagents-count', parts.join(' · ')));
    box.append(head);
    const list = el('div', 'chat-subagents-list');
    rows.forEach((row) => list.append(subagentNode(row)));
    box.append(list);
    return box;
  }

  function subagentNode(row) {
    const node = el('div', 'chat-subagent-row ' + row.state);
    const head = el('div', 'chat-subagent-head');
    head.append(el('span', 'chat-subagent-index',
      '#' + row.index + (row.total ? ' / ' + row.total : '')));
    head.append(el('span', 'chat-subagent-role', row.role || '子智能体'));
    if (row.policy) {
      head.append(el('span', 'chat-subagent-policy',
        row.policy + (row.toolCount ? ' · ' + row.toolCount + ' 项能力' : '')));
    }
    head.append(el('span', 'chat-subagent-state', SUBAGENT_STATE[row.state] || row.state));
    const meta = [];
    if (row.steps) meta.push(row.steps + ' 步');
    if (row.durationMs) meta.push(ui().formatDuration(row.durationMs));
    if (meta.length) head.append(el('span', 'chat-subagent-meta', meta.join(' · ')));
    node.append(head);
    if (row.tools.length) {
      const tools = el('div', 'chat-subagent-tools');
      row.tools.forEach((tool) => {
        // 复用 live 芯片那一套类名，**不另抄一份颜色**：子智能体调的工具和父轮调的
        // 工具是同一种东西，长得不一样只会让人以为它们是两类事件。
        //
        // 派活类工具带着 `jobId`——那一枚芯片换成按钮，点一下去右栏看那条任务。
        // 别的工具仍是 `span`：它没有可去的地方，做成按钮只会让人白点一次。
        const chipTag = tool.jobId ? 'button' : 'span';
        const chip = el(chipTag, 'chat-live-chip' + (tool.ok ? '' : ' bad'));
        chip.append(el('span', 'chat-live-chip-name', tool.label));
        chip.append(el('span', 'chat-live-chip-time', ui().formatDuration(tool.durationMs)));
        if (tool.brief) chip.title = tool.brief;
        if (tool.jobId) {
          chip.classList.add('chat-live-chip-job');
          chip.type = 'button';
          chip.title = (tool.brief ? tool.brief + '\n' : '') + '点开看这条作业';
          chip.addEventListener('click', (event) => {
            // 这一块本身可能挂在 `.chat-subagent-row`（可点、且外层有 details）里，
            // 不拦住冒泡就会顺手把那一行也点开、把外层折叠块也翻一次。
            event.stopPropagation();
            event.preventDefault();
            focusJob(tool.jobId);
          });
        }
        tools.append(chip);
      });
      node.append(tools);
    }
    if (row.summary) node.append(el('p', 'chat-subagent-summary', row.summary));
    if (row.note) node.append(el('p', 'chat-subagent-note-line', row.note));
    // 失败行必须能一眼认出来：左边的竖线、状态字与这句原因三处同时变。
    // 只标一处的话，"没跑完"和一个"跑完但没话可说"的行看起来是一样的。
    if (row.error) node.append(el('p', 'chat-subagent-error', subagentErrorLine(row)));
    return node;
  }

  /* 侧边栏的「子智能体」区**曾经整块删除**，本轮又搬了回来——所以这里记一笔，
   * 免得下一个人读到两段互相矛盾的注释。
   *
   *  那时它是 `renderSideSubagents` + `sideSubagentRow` 两个函数，渲染的是
   *  `message.subagents`，而主区里 `subagentBlock` 渲染的是**同一份数据**：
   *  同一件事在屏幕上出现两次，而两处的详细程度不同（侧栏那版只有摘要，主区那版带
   *  完整工具链）。两者对不上时，读的人没有任何依据判断该信哪一份。
   *
   *  删的是**第二条渲染路径**，不是这个位置带来的信息。本轮用户明确要求把子智能体
   *  放到左侧会话下面，于是位置回来了——而当初那条理由是用**同一个函数**消掉的，
   *  不是被无视的：`sideSubagentRow` 这个名字没有回来，`renderSideSubagents` 现在
   *  只做一件事：取出行、交给 `subagentBlock(rows)`。详细程度无从不同，
   *  也就无从对不上。
   *
   *  侧栏那一份与主区那一份的差别现在只剩**样式**（栏头已经写着"子智能体"，
   *  所以把区块内部那行同名标题藏掉），而样式不构成第二种说法。
   */

  /* ------------------------------------------------------------ 启动 ---- */

  function cacheAndBind() {
    if (view.bound || !dom.log) return;
    view.bound = true;
    /* **一颗按钮两种身份**：跑着的时候它叫「停止」，所以要在这里分派。
       改版前是两颗按钮（「发送」+ 一颗 `hidden` 切换的「停止」），各绑各的。
       判据用 `view.sending` 而不是"按钮上有没有 `.is-stop`"：那个 class 是**渲染
       结果**，拿它当条件就是把"画对了"当成"能点对"——两处一旦不同步（比如某条
       早退路径没走到 setSending），按钮会长成停止的样子、点下去却什么都不做。 */
    dom.send.addEventListener('click', () => {
      if (view.sending) stopTurn();
      else sendPrimary();
    });
    dom.input.addEventListener('keydown', (event) => {
      // t78：**输入法组字期间的回车不是发送**。中文/日文输入法里那一下是在"确认
      // 候选词"，官方输入区也按这个规矩放行（它的提示行在组字时整段隐藏）。不做
      // 这一条的话，打"扫描"时会先把半截拼音当作一条消息发出去。
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        send();
      }
    });
    /* 排队任务那一块的点击：**一个委托监听**，不给每一行各绑一遍。行是每次重画
       新造的节点，逐行绑等于每次重画都要重新绑一次（漏一个的症状是"这颗按钮有时候
       点不动"）；而 `data-act` 上的枚举只有那五个值。 */
    if (dom.queueList) {
      dom.queueList.addEventListener('click', (event) => {
        const btn = event.target.closest('[data-act]');
        // 编辑框自己带 `data-act="edit-text"`：点它只是把光标放进去，不是动作。
        if (!btn || btn.dataset.act === 'edit-text') return;
        event.preventDefault();
        queueAction(btn.dataset.act, btn.closest('.chat-queue-item'));
      });
      // 就地编辑里：Esc 取消，Ctrl/⌘+Enter 保存。**回车在这里只是换行**——那是
      // 一段多行需求，与输入框"回车就发"是两回事。
      dom.queueList.addEventListener('keydown', (event) => {
        const box = event.target.closest('[data-act="edit-text"]');
        if (!box) return;
        if (event.key === 'Escape') {
          event.preventDefault();
          queueAction('cancel', box.closest('.chat-queue-item'));
          return;
        }
        if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
          event.preventDefault();
          queueAction('save', box.closest('.chat-queue-item'));
        }
      });
    }
    // t78：**输入框跟着内容长高**，与官方输入区同一套行为——一行起，长到上限
    // （官方 `--dsh-composer-text-max-height: 336px`）之后改成框内滚动，而不是
    // 把动作行顶出去。下限不在这里管：`.chat-input` 的 min-height 是那条下限
    // 契约（`--chat-input-floor`，四条行高 + 上下内边距），`height: auto` 之后
    // 由 min-height 兜住，所以这里只写"内容需要多高"。函数本体在模块级（见
    // 上面的 `fitInput`）：发送清空输入框时也要用它把框缩回去。
    dom.input.addEventListener('input', fitInput);
    fitInput();
    dom.newButton.addEventListener('click', newSession);
    // 右栏的关按钮。`openDetail` 是它的反向操作，两者改的是同一份状态。
    if (dom.detailClose) dom.detailClose.addEventListener('click', () => setDetailOpen(false));
    dom.refresh.addEventListener('click', async () => {
      ui().hideError();
      // 手动刷新就是「再试一次接上」：清掉看门狗留下的那个一次性标记，
      // 否则紧接着的 `openSession → attachTurn` 会把它当成同一次失败而跳过。
      view.attachPaused = false;
      await loadOptions();
      await loadSessions(true);
      await loadModelState();
      if (view.sessionId) await openSession(view.sessionId);
    });
    // 坞里两个互斥按钮。文字**不跟着状态变**（改版前是"可用能力 ↔ 收起能力"）：
    // 现在有选中态这一层视觉，而随状态改标签会让同一颗按钮在两个字之间跳、
    // 旁边那颗却不动——看起来像两颗不同的东西。收起交给"再点一次"，理由见 setDock。
    [[dom.toolsButton, 'tools'], [dom.skillsButton, 'skills']]
      .forEach(([node, mode]) => {
      if (!node) return;
      node.addEventListener('click', () => {
        ui().hideError();
        setDock(mode);
      });
    });
    // 点坞外任意位置把坞收起来。
    //
    // 挂在 `document` 的**冒泡**阶段：两颗按钮自己的 click 先跑完（该切的那一格已经
    // 切过去了），这里只回答一个问题——这次点的是不是坞外。落在坞内（内容区 / 两颗
    // 入口）就什么都不做，理由见 `insideDock`。
    //
    // **不 `preventDefault`、不抢焦点**：收起是副作用最小的一次状态变化。用户点哪儿
    // 就还留在哪儿，输入框里已经打好的那行字一个字不动（`closeDock` 只清坞的内容区）。
    document.addEventListener('click', (event) => {
      // 权限菜单与坞是同一类"临时摊开的东西"，共用这一个文档级监听：
      // 再挂一个的话，同一次点击会在两个 handler 里各跑一遍（tests/test_dock_dismiss
      // 专门钉住"文档级 click 只许一处"，那条契约就是为这一天写的）。
      // 用 `composedPath()` 判"这次点击是不是落在 chip / 菜单里"。
      // 不能只看 `target.closest('.chat-policy')`：点菜单项时 `choosePolicy` 会同步
      // `clear(menu)`，等事件冒到 document，那个按钮已经**从文档里摘掉**了，
      // `closest` 返回 null —— 于是"选一档"会被当成"点了外面"，确认框当场被关掉
      // （切「完全作业」那一步尤其明显：点了没反应）。路径在派发那一刻就固定了，
      // 摘掉节点也不会变。
      // 权限与模型两颗 chip 共用这一次派发（文档级 click 只许一处，见上一条注释）。
      const path = typeof event.composedPath === 'function' ? event.composedPath() : null;
      const insideOf = (name) => !path || path.some((node) => node && node.classList
        && node.classList.contains(name));
      if (dom.policyMenu && !dom.policyMenu.hidden && !insideOf('chat-policy')) {
        hidePolicyMenu(false);
      }
      if (dom.modelMenu && !dom.modelMenu.hidden && !insideOf('chat-model')) {
        hideModelMenu(false);
      }
      if (view.dock === 'none' || insideDock(event.target)) return;
      closeDock();
    });
    // 键盘上的等价物，与 `auth.js` 的密码弹窗同一语义：这类"临时摊开的东西"按 Esc
    // 就收回去。只收坞——`closeDock` 已经是收起的空操作，不必在这里再判一次。
    // 权限菜单同属"临时摊开的东西"：Esc 一起收（不抢焦点，收完还在原来那处）。
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      closeDock();
      if (dom.policyMenu && !dom.policyMenu.hidden) hidePolicyMenu(false);
      if (dom.modelMenu && !dom.modelMenu.hidden) hideModelMenu(false);
      // 详情栏也属于“临时摊开的东西”，Esc 一起收；但只收起、不清选中——
      // 收起是“先不看”，重新拉开看到的应该还是刚才那一条。
      if (view.detailOpen) setDetailOpen(false);
    });
    // 「加载更早」的两个无按钮入口。
    // 为什么要键盘入口：往回翻这件事发生在**正往上读**的时候，手在键盘上；
    // 只有页顶那个小胶囊的话，等于要求先把视线移开、再把鼠标挪过去。
    // 为什么先判"焦点在输入框里"：输入框里 Alt+↑ 是编辑器的行移动，抢过来
    // 就是坏——那条路本来能用，改成不能用是退步。
    const editing = (node) => Boolean(node && node.tagName
      && (node.isContentEditable || node.tagName === 'INPUT'
        || node.tagName === 'TEXTAREA' || node.tagName === 'SELECT'));
    // 滚轮/触摸才置位"使用者自己动过"；程序化滚动不置位（见 `earlierAutoLoad`）。
    ['wheel', 'touchmove'].forEach((name) => {
      dom.log.addEventListener(name, () => { view.logTouched = true; },
        { passive: true });
    });
    // 窗口宽度变一次，审查卡的换行就变一次，卡片高度也跟着变 —— 钉住的那个高度
    // 必须重算，否则窗口拉窄之后浮层比卡片矮，正文被裁掉一截。
    // 只重算、不重画 DOM：`decideReview()` 期间按钮是 disabled 的，重建会丢那个状态。
    window.addEventListener('resize', pinReviewHeight);
    dom.log.addEventListener('scroll', earlierAutoLoad, { passive: true });
    document.addEventListener('keydown', (event) => {
      if (event.defaultPrevented || editing(event.target)) return;
      if (event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey
          && event.key === 'ArrowUp') {
        if (!view.earlierCount || view.earlierLoading) return;
        event.preventDefault();
        loadEarlier({ auto: true });
        return;
      }
      // 翻页键同样算"自己在往回读"：按过 PageUp/Home 之后滚到顶，应当继续取。
      if (!event.altKey && !event.ctrlKey && !event.metaKey
          && ['PageUp', 'Home', 'ArrowUp'].includes(event.key)) {
        view.logTouched = true;
      }
    });
    // 权限 chip：点开 / 点关。
    if (dom.policyBtn) dom.policyBtn.addEventListener('click', togglePolicyMenu);
    // 模型 chip：同一套（它只是原生 `#chat-model-select` 的皮，见 index.html 那段）。
    if (dom.modelBtn) dom.modelBtn.addEventListener('click', toggleModelMenu);
    // 自动执行的开关：**落在服务端**（`chat_sessions.auto_drive`），它只控制"失败后
    // 要不要自己再发一句继续"与"正常收尾后要不要自己接着开一轮"，不改变任何门禁；
    // 关掉时把待发的那一次也撤销。已有会话当场落库，还没有会话时先记在界面上，
    // 由第一条消息一起带走（同三项选择那套）。
    if (dom.auto) {
      dom.auto.addEventListener('click', () => {
        auto.on = !auto.on;
        renderAutoSwitch();
        if (!auto.on) cancelAutoContinue();
        if (view.sessionId) commitEntry(false);
      });
    }
    if (dom.autoCancel) {
      dom.autoCancel.addEventListener('click', () => cancelAutoContinue());
    }
    // 三项选择：改动即落库（已有会话）或只记在界面上（新会话，发送时一并带上）。
    [ [dom.agent, 'agentId'], [dom.modelSelect, 'modelId'], [dom.policy, 'policyId'] ]
      .forEach(([node, key]) => {
        if (!node) return;
        node.addEventListener('change', () => {
          // 换了模型，chip 上的字与框宽都要跟着新名字走：否则短名字后面会留着
          // 上一项的宽度（字先换，宽度才有依据）。
          if (node === dom.modelSelect) { syncModelChip(); syncModelWidth(); }
          // 权限档换了，chip 上的字要跟着换（`commitEntry` 回来时还会再对齐一次）。
          if (node === dom.policy) syncPolicyChip();
          if (view.sessionId) {
            commitEntry(false);
            return;
          }
          // 新会话还没落库：无法保存，`commitEntry` 会在发送时随消息带上三项。
          // 这里只更新提示，不改 view.config——真实的生效值由服务端算。
          const hint = '本会话尚未创建，选择将在发出第一条消息时生效';
          if (dom.entryHint) dom.entryHint.textContent = hint;
        });
      });
    // 开关的初始外观在绑定时画一次：HTML 里的 aria-pressed 只是"没脚本时的默认"，
    // 真正该显示的是 `auto.on`（默认开）。
    renderAutoSwitch();
    if (dom.tab) dom.tab.addEventListener('click', ensureLoaded);
  }

  function ensureLoaded() {
    if (!window.PentestAuth || !window.PentestAuth.getUser()) return undefined;
    // **正在跑的那一轮不能被这里"收尾"。** 切回本页签不等于这一轮结束了：SSE 还在
    // 后台跑，而 `view.sending === false` 会让紧接着的 `renderLog()` 把正在生成的
    // 那条气泡摘掉——它是页面上**最新的一条内容**（`renderLog` 只在
    // `view.sending && view.live` 时画它）。表现是每次切回来都看见它"消失一下"，
    // 直到下一帧增量重新画出来；若这一轮正卡在一次工具调用里（几秒没有增量），
    // 它就一直不在。同一个标志还决定 `syncFromPulse` 要不要在本轮进行中整份重取
    // 消息（那会与 SSE 打架，见那里的注释），以及输入区是不是被解锁（解锁后能再发
    // 一条，而服务端正在跑的那一轮还占着这个会话）。
    //
    // 判据用 `view.abort`：它在这一轮开始前赋值、在 `finish()` 里清空，是"有一轮在飞"
    // 的唯一真相。只有**没有在跑的轮次**才清 `sending`——那是"页面不确定上一轮收没
    // 收尾"（例如登录后重载）时的兜底，与"从别的页签切回来"不是一回事。
    // 表里还跑着的那一轮也要认：切走再切回来时 `view.abort` 可能已经是空的（投影被
    // 「新对话」摘下来了），而那个会话那一轮仍在跑——把它挂回屏上，别让 `setSending`
    // 把界面按"这一轮结束了"处理。
    if (!view.abort && runOf(view.sessionId)) adoptRun(view.sessionId);
    if (!view.abort) setSending(false);
    // 右栏那一块任务读数不在作业页签里，所以它不会跟着作业页签的自动刷新走。
    // 这一句把它接上：立刻拉一次 + 与作业页签共用一个定时器（`watchJobs` 是那一
    // 处入口）。少了它，那一块会一直停在登录那一刻的"登录后显示"。
    ui().watchJobs();
    // 右栏默认拉开一次（宽屏）。它是任务与子智能体两块的家：收起状态下这两块整片
    // 不可见，而"右边还有两块"这件事没人会去猜（这与"功能在、但看不见"是同一类
    // 缺陷）。注意这里**不再**靠一句空态提示当发现入口——展开面已经就地长在被点中
    // 那一行下面（`.chat-detail-inline`），点一下自己就出来了，不必先知道栏里有它。
    // 窄屏不自动拉开：那一档它会变成消息流下方 40vh 的一块，一进页面就占掉一半。
    if (!view.detailAutoOpened) {
      view.detailAutoOpened = true;
      // 门槛定在 1200px，不是 900px：左栏 296 + 详情栏 380 两条固定宽度，窄于
      // 1200 时消息流只剩 300 多像素——一进页面就三栏挤着，比看不到详情更难受。
      // 窄一两档的人自己点一行即可（`openDetail` 会顺手把栏拉开）。
      if (!window.matchMedia || !window.matchMedia('(max-width: 1199px)').matches) {
        setDetailOpen(true);
      }
      renderDetail();
    }
    loadTools();
    loadOptions();
    loadModelState();
    return loadSessions(false).then(() => {
      if (!view.sessionId && view.sessions.length) return openSession(view.sessions[0].id);
      renderEntry();
      renderLog();
      return undefined;
    });
  }

  function reload() {
    view.loaded = false;
    return ensureLoaded();
  }

  function onSignedOut() {
    // 退出时**每一条**流都要断，不只是屏上那一条：后台那几条仍在往服务端读字，
    // 而这个页面已经没有会话可写（`view.sessionId` 紧接着就清空）。
    Object.keys(view.runs || {}).forEach((key) => {
      const one = view.runs[key];
      if (one && one.controller) one.controller.abort('signout');
    });
    view.runs = {};
    if (view.abort) view.abort.abort('signout');
    view.abort = null;
    view.live = null;
    // 自动执行的开关**落在服务端**（会话级），但待发的计时器属于上一个登录者的这一轮：
    // 换人或退出时必须撤掉，否则它会对着"已签出"的页面发一句继续。
    cancelAutoContinue();
    auto.attempts = 0;
    auto.failed = false;
    view.sessions = [];
    view.sessionId = null;
    view.messages = [];
    view.earlierCount = 0;
    view.reviews = {};
    view.reviewOrder = [];
    view.model = null;
    view.hasModel = false;
    view.loaded = false;
    // 上一个用户那次请求的失败**不能留给下一个人**：留着的话他登录后看到的第一句话
    // 是"对话列表加载失败"，而那是别人的一次抖动。
    view.sessionsError = null;
    view.options = null;
    view.config = null;
    // 右栏里挂着的是上一个人点开的那条任务 / 那个目标，必须一起空掉。
    closeDetail();
    // 上次选择是**按人**存的，不能让下一个人登录前看到上一个人的预选。
    view.draft = null;
    // 工具目录与技能目录都是**按人过滤**的（权限档、Agent 预设），退出登录必须
    // 一并清掉：留着的话，下一个人登录前看到的仍是上一个人的能力清单。
    view.tools = [];
    view.skillList = [];
    view.skillRoles = [];
    view.skillLoaded = false;
    // 坞收回默认的收起态并清空内容。不收的话会停在上一个人看过的目录页上，
    // 而那时目录已经清空——看到的是一格空的坞。
    view.dock = 'none';
    if (dom.quick) {
      dom.quick.setAttribute('data-dock', 'none');
      clear(dom.quick);
      dom.quick.append(el('span', 'muted', '登录后可用'));
    }
    if (dom.sessions) { clear(dom.sessions); dom.sessions.append(el('p', 'muted', '登录后显示对话列表。')); }
    if (dom.goals) { clear(dom.goals); dom.goals.append(el('p', 'muted', '登录后显示目标列表。')); }
    if (dom.sessionsHint) dom.sessionsHint.textContent = '全部目标';
    // 后两块一并清掉。不清的话，下一个人登录前看到的是**上一个人的**任务与子智能体
    // ——它们的形状完全正常（有状态、有角色、有结论），只是不属于他。
    if (dom.sideJobs) { clear(dom.sideJobs); }
    if (dom.sideJobsHint) dom.sideJobsHint.textContent = '登录后显示';
    if (dom.sideSubagents) { clear(dom.sideSubagents); }
    if (dom.sideSubagentsHint) dom.sideSubagentsHint.textContent = '登录后显示';
    if (dom.log) { clear(dom.log); }
    if (dom.meta) dom.meta.textContent = '登录后可用';
    if (dom.entryHint) dom.entryHint.textContent = '登录后可选择 Agent / 模型 / 权限。';
    [dom.agent, dom.modelSelect, dom.policy].forEach((node) => { if (node) clear(node); });
    syncModelChip();
    renderModel();
  }

  /** 由设置页调用：把某个 Agent 预设选到**当前会话**上。
   *
   *  设置页在插件市场里点「应用到会话」时走这里。不直接改 `view.config`，而是
   *  触发下拉框的 change 事件——那条路径已经处理了"已有会话就落库、新会话只提示
   *  等第一条消息"这两个分支，另写一份保存逻辑必然与它分叉，而分叉的表现是
   *  "界面上选了，实际生效的是另一个"。
   *
   *  返回值约定：空串 = 已落库生效；非空串 = 需要提醒用户的一句话。
   */
  async function applyAgent(agentId) {
    const id = String(agentId || '').trim();
    if (!id) return '缺少 Agent id，无法应用。';
    // 先把会话上下文准备好：没加载过就加载、没选中会话就选中第一条。
    //
    // 少了这一步会**静默丢掉刚做的选择**：在设置页点「应用到会话」时，如果对话模块
    // 还没列出任何会话，`view.sessionId` 是空，于是这里只改了表单；等会话随后加载完，
    // `renderEntry()` 会按服务端配置把下拉框整只重建，刚选的 Agent 被抹掉——而当时
    // 给出的提示还写着"将在发出第一条消息时生效"，那句话在这种情况下是假的。
    if (!view.sessionId) await ensureLoaded();
    if (!view.options) await loadOptions();
    const has = (payload) => asArray(payload && payload.agents).some((item) => item.id === id);
    if (!has(view.options)) {
      // 插件刚装完、目录还是旧的，或者这个 Agent 已经随插件卸载消失了
      await loadOptions();
      if (!has(view.options)) return '当前 Agent 目录里没有「' + id + '」，可能需要重启 DSH 让插件加载。';
    }
    renderEntry();
    dom.agent.value = id;
    dom.agent.dispatchEvent(new Event('change'));
    setSending(false);
    if (!view.sessionId) return '本会话尚未创建，「' + id + '」将在发出第一条消息时生效。';
    return '';
  }

  function init() {
    if (view.bound) return;
    cacheDom();
    if (!dom.log) return;
    cacheAndBind();
    // 目标列表由 app.js 拉取，它可能**早于**这里初始化就已经把数据放好了
    // （`renderGoals(list)` 在没有 DOM 时会只存不画）。不补这一句，先加载完
    // 目标的那些情况下侧边栏会一直是空的。
    renderGoals();
    // 作业列表同样可能**早于**这里就已经加载完（`renderSideJobs` 在没有 DOM 时
    // 直接返回，什么都不记）。不补这一句，先加载完任务的启动顺序下，
    // 侧栏的任务块会一直停在"正在加载…"。
    renderSideJobs();
    renderDetail();
    renderDock();
    renderLog();
    renderModel();
    // 数据在登录后由 boot.js 触发 reload：这里的 init 也会在未登录时被调用，
    // 此时发请求既没意义又会在页面上留下半个空态。
    onSignedOut();
  }

  window.PentestChat = { init, reload, ensureLoaded, onSignedOut, loadSessions,
                         loadTools, loadOptions, loadModelState, openSession, send,
                         applyAgent,
                         // 目标列表的渲染归这里，数据归 app.js（`PentestUI`）。
                         // app.js 在目标变化时调它，两边因此共用一份 `/engagements`。
                         renderGoals,
                         // 任务同理，共用一份 `/jobs`。**由 app.js 在每次作业列表
                         // 变化时调用**，包括加载失败那一次——它必须把 error 传进来，
                         // 否则侧栏会继续显示上一批任务，而面板刚说过加载失败。
                         renderSideJobs,
                         // 右侧栏。`renderDetail` 暴露出来是为了让“数据到了但
                         // 没走到重画”的路径可以补一次（与 app.js 那几个 render* 同一
                         // 性质）；`openDetail` 留给调试与后续入口。
                         renderDetail, openDetail, closeDetail,
                         // 侧边栏读数的新鲜度：工作台心跳（`app.js` 的 `pumpPulse`）
                         // 每 3 秒问一次"变了没有"，变了才由这里去拉。
                         // `currentSessionId` 是心跳要那两块的指纹时必须带的参数。
                         currentSessionId, syncFromPulse,
                         // 会话里点一行工具轨迹 → 右栏那一条任务（m14866）。
                         // 那条路径要展开折叠的面板、拉开详情栏、把行滚进视野，
                         // 三件事都在 chat.js 里；对外暴露是为了让契约测试能直接验
                         // "点了之后右栏确实换成了这一条"，而不必驱动真实 DOM。
                         focusJob,
                         // 把审查浮层的高度重新钉到一张卡上（m20719）。同样是为了
                         // `tools/probe_chat_tail.py`：那套探针用真实浏览器、自己往浮层
                         // 里塞夹具卡片，走不到 `renderReviews()`，钉高就得由它主动叫一次。
                         pinReviewHeight };
})();
