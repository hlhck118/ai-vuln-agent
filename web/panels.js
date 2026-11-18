/* 新增六页的渲染（仪表盘 / 流量 / 请求重发 / 工作空间 / 审批记录 / 日志）。
 *
 * 为什么单独一个文件而不是继续塞进 app.js：那个文件已经 4300 行，而这几页共用
 * 一套很窄的东西——拉一次数据、按表格画出来、失败时给一段能读的说明。放进主文件
 * 会让"页签怎么切"和"某张表有几列"混在同一段里。
 *
 * 约定与 app.js 完全一致（断网内网、零外部资源）：
 *
 *   * **接口数据一律经 textContent 落 DOM**，绝不拼 HTML 字符串——这里的每一条
 *     记录都来自网络（URL、响应头、响应正文），拼字符串等于把它们当代码执行；
 *   * 只调 `window.PentestUI` 里已经有的工具，不自己再实现一份 `$ / el / apiGet`；
 *   * 每一页都要有"现在没有数据"的形态，而不是一片空白。
 */
(function () {
  'use strict';

  const UI = window.PentestUI;
  if (!UI) return;

  const { $, el, clear, emptyBlock, errorBlock, apiGet, apiPost, formatTime,
    formatCount, formatDuration, jobView, severityView } = UI;

  /* ------------------------------------------------------------ 仪表盘 */
  // 一次 `/panel` 请求换四块读数。**不另开接口**：概览页与资产/漏洞页读的必须是
  // 同一份快照，否则"概览说 18 个资产、资产页列 17 条"是最容易被当成 bug 的现象
  // （见 server.py 里 panel 的注释）。
  async function loadDashboard() {
    const host = $('#dashboard-body');
    if (!host) return;
    clear(host);
    host.append(el('div', 'muted', '正在读取概览…'));
    let data = null;
    try {
      data = await apiGet('/panel');
    } catch (error) {
      clear(host);
      host.append(errorBlock('概览加载失败：' + error.message));
      return;
    }
    clear(host);
    const platform = data.platform || {};
    const counts = data.counts || {};

    // 规模读数：四块，每块一个数。数值用 `formatCount` 会加千分位——
    // "1200" 与 "1,200" 在扫一眼时不是一回事。
    const stats = el('div', 'dash-stats');
    const card = (label, value, hint) => {
      const box = el('div', 'dash-stat');
      box.append(el('div', 'dash-stat-label', label));
      // `formatCount` 加千分位：这里是「扫一眼读量级」的位置，1200 与 1,200
      // 在读的时候不是一回事。上面那段注释一直是这么说的，代码却直接 `String()`——
      // 注释承诺了千分位，屏幕上没有。
      box.append(el('div', 'dash-stat-value', formatCount(value)));
      if (hint) box.append(el('div', 'dash-stat-hint', hint));
      return box;
    };
    stats.append(
      card('目标', platform.engagements || 0, '每个目标一条独立链路'),
      card('节点', platform.nodes || 0, '意图 / 事实 / 漏洞 / 资产'),
      card('资产', counts.assets || 0, `其中高危漏洞 ${formatCount(counts.highFindings || 0)} 条`),
      card('任务', counts.jobs || 0, `待执行 ${formatCount(platform.pendingJobs || 0)} · 执行中 ${formatCount(platform.runningJobs || 0)}`),
    );
    host.append(stats);

    // 执行节点：在线数来自 platform.executorsOnline。**离线也算一条**——
    // "0 个节点"与"3 个节点全离线"在排障时是两件事。
    const nodeCard = el('section', 'dash-card');
    nodeCard.append(el('h2', 'dash-card-title', '执行节点'));
    const online = platform.executorsOnline || 0;
    const totalExec = platform.executors || 0;
    nodeCard.append(el('p', 'muted',
      `在线 ${formatCount(online)} / 共 ${formatCount(totalExec)}` + (totalExec && !online ? '（全部离线：任务会一直排队）' : '')));
    host.append(nodeCard);

    // 最近任务：只列 8 条。这一页要回答"刚才发生了什么"，不是"历史上发生过什么"。
    const jobCard = el('section', 'dash-card');
    jobCard.append(el('h2', 'dash-card-title', '最近任务'));
    const jobs = (data.jobs || []).slice(0, 8);
    if (!jobs.length) {
      jobCard.append(emptyBlock('还没有任何任务'));
    } else {
      const list = el('div', 'dash-list');
      jobs.forEach((job) => {
        const row = el('div', 'dash-row');
        // 状态一律走 `PentestUI.jobView`（同一个 `jobStatusOf`、同一张中文表）。
        // 这里原先直接印 `job.status`：仪表盘上是 pending / done，而任务页把同一个
        // 状态印成「待执行」/「已完成」——一个词两种写法，读的人会当成两件事。
        // 圆点的类名同样取归一后的状态：库里的写法一变，类名就对不上，圆点会静默
        // 回落成灰的，看上去像这个任务没有状态。
        const status = jobView.statusOf(job);
        row.append(el('span', `dash-dot is-${status}`));
        const mainJob = el('span', 'dash-row-main', job.kindLabel || job.kind || '任务');
        mainJob.title = mainJob.textContent;   // 截断后的完整值（见 style.css .dash-row-main）
        row.append(mainJob);
        row.append(el('span', 'dash-row-meta', jobView.label[status] || status));
        row.append(el('span', 'dash-row-time', formatTime(job.createdAt)));
        list.append(row);
      });
      jobCard.append(list);
    }
    host.append(jobCard);

    // 高危漏洞：这一页不该只给一个数字。要能一眼看到"哪几条"，
    // 否则仪表盘就退化成一个装饰。
    const findCard = el('section', 'dash-card');
    findCard.append(el('h2', 'dash-card-title', '高危漏洞'));
    // 「高危及以上」按共享顺序判（`PentestUI.severityView`），不写死两个字面量：
    // 后端 `panel()` 用的是 `severity_rank(...) <= 1`，两处一旦有一边改了档位或
    // 排序，这一页挑出来的与 `counts.highFindings` 数的就不是同一批。
    const high = (data.findings || [])
      .filter((item) => severityView.rank(item.severity) <= 1).slice(0, 8);
    if (!high.length) {
      findCard.append(emptyBlock('没有高危记录'));
    } else {
      const list = el('div', 'dash-list');
      high.forEach((item) => {
        const row = el('div', 'dash-row');
        // 档位先归一再拼类名：`severity` 是后端原值，认不出的（或空的）直接拼进去
        // 就成了 `sev-` 或 `sev-unknown`，那两种类名样式表里没有，圆点会没有颜色。
        // `severityView.of` 与漏洞卡片、详情页、过滤条用的是同一个归一函数。
        row.append(el('span', `sev-dot sev-${severityView.of(item.severity)}`));
        const mainFind = el('span', 'dash-row-main', item.title || '（无标题）');
        mainFind.title = mainFind.textContent;
        row.append(mainFind);
        row.append(el('span', 'dash-row-meta', item.severityLabel || item.severity || ''));
        list.append(row);
      });
      findCard.append(list);
    }
    host.append(findCard);

    const meta = $('#dashboard-meta');
    if (meta) meta.textContent = '更新于 ' + formatTime(data.generatedAt);
  }

  /* ------------------------------------------------------------ 流量 */
  // 列表上这会儿有几条。只为一件事存在：「清空全部」的确认框要能念出个数——
  // 一个说不出删掉几条的确认框，读的人只会当成又一个要按掉的框。
  let trafficCount = 0;
  // 左侧列表里当前选中的那一条。选中态**只在前端存**：它回答的是「右边那份原文
  // 是哪一行」，属于看的人此刻站在哪里，不是数据。
  let trafficPicked = '';
  // 当前这条对话的 id。流量按会话收口（`list_traffic` 的 `session_id`）——这一页
  // 回答的是「**这条对话**里发出去过什么」，不是「我这个人发过什么」。不收口的话，
  // 上一个会话、或者另一个标签页里跑过的扫描，会一起堆在这里，而列表上没有任何
  // 东西说明它们不是这一条对话的。
  //
  // 拿不到会话（还没点开对话、或对话页还没起来）时退回空串 = 不收口，与后端
  // `session_id=""` 同一条口径：宁可多列几条，也别在这一页显示成「空」。
  function trafficSession() {
    const chat = window.PentestChat;
    if (!chat || typeof chat.currentSessionId !== 'function') return '';
    return String(chat.currentSessionId() || '');
  }

  function trafficQuery(limit) {
    const session = trafficSession();
    return '/traffic?limit=' + encodeURIComponent(limit)
      + (session ? '&sessionId=' + encodeURIComponent(session) : '');
  }

  // 把选中态画到两处列表上（流量页左栏、重发页侧栏）。两边是同一批记录，点了
  // 哪一条就在两边都标出来——否则从重发页切回流量页，会以为一条都没选中。
  function markTrafficPicked(id) {
    trafficPicked = String(id || '');
    ['#traffic-body', '#repeater-traffic'].forEach((selector) => {
      const host = $(selector);
      if (!host) return;
      const rows = host.querySelectorAll('tr[data-traffic-id]');
      for (let i = 0; i < rows.length; i += 1) {
        rows[i].classList.toggle('is-picked', rows[i].dataset.trafficId === trafficPicked);
      }
    });
  }

  // `intoForm` 为真时（从重发页右栏点的）顺手把这一条填进左边那张表——那一栏
  // 存在的全部理由就是这个。流量页左栏点的只换右边那份原文。
  async function pickTraffic(id, intoForm) {
    markTrafficPicked(id);
    const record = await showTraffic(id);
    if (intoForm && record) prefillRepeater(record);
  }

  // 列表**不带正文**（`withBody` 默认关）：一屏几十条，每条几十 KB 正文会把
  // 响应撑到几 MB，而列表上根本显示不了正文。点开某一条才取全文。
  // `quiet` 是**心跳那条路**专用的：`/pulse` 的流量指纹一变就调它一次（见
  // app.js 的 `pumpPulse`）。两条差别都与"这一页正被人读着"有关：
  //
  //   * 不写「正在读取流量…」——那是给"刚点进来"用的。心跳每几秒来一次，每次都
  //     在表格上方闪一行字，读的人会以为这一页一直在重连。
  //   * 失败**静默**。后台轮询失败一次的红字会盖在正在读的表上，而下一次 3 秒后
  //     就会自己好；真正的失败仍然会暴露——切页签那一次是 `quiet` 为假的加载。
  //
  // `#traffic-detail` 两条路都不动：它显示的是某一条记录的原文，与列表是否重画无关。
  async function loadTraffic(quiet) {
    const host = $('#traffic-body');
    if (!host) return;
    if (!quiet) {
      clear(host);
      host.append(el('div', 'muted', '正在读取流量…'));
    }
    let data = null;
    try {
      data = await apiGet(trafficQuery(100));
    } catch (error) {
      if (quiet) return;
      clear(host);
      host.append(errorBlock('流量列表加载失败：' + error.message));
      return;
    }
    clear(host);
    const records = data.records || [];
    trafficCount = records.length;
    const meta = $('#traffic-meta');
    // 列表也有上限（`_page_size` 缺省 50、上限 200，这一页要 100）。拿满时同样要
    // 说出来：页头只写「N 条」等于宣称这就是全部，而早先那些记录还在库里。
    // 上限取响应里的 `limit` 而不是写死 100——哪天前端改了口径，这句话跟着变。
    const capped = data.limit && records.length >= data.limit ? '（只到最近 ' + data.limit + ' 条）' : '';
    if (meta) {
      meta.textContent = records.length
        ? `${formatCount(records.length)} 条${capped}（${data.scope === 'all' ? '全部用户' : '仅自己'}）`
        : '还没有记录';
    }
    paintTrafficList(host, records, false);
    // 重发页右栏是**同一批记录**的另一张单子：那边只挑不改，所以列更少。
    // 两处画同一份数据、同一套点击语义，否则切页签时选中态会「丢」。
    paintTrafficList($('#repeater-traffic'), records, true);
    // 右栏还没人点过时给一句话。空着的话看起来像「这条记录没有内容」，
    // 而真正的意思是「还没选」。
    const detail = $('#traffic-detail');
    if (detail && !trafficPicked && records.length) {
      clear(detail);
      detail.append(emptyBlock('点左边任意一行，这里显示它的请求头、请求正文与响应。'));
    }
  }

  // 画一张流量列表。两处用它：流量页左栏（六列）与请求重发页右栏（两列）。
  // 侧栏只有四百来像素宽，状态与耗时在那里是噪声——那一栏的用途是「挑一条填进
  // 左边」，读的人只需要认出「哪一条」。
  function paintTrafficList(host, records, compact) {
    if (!host) return;
    clear(host);
    if (!records.length) {
      host.append(emptyBlock(compact
        ? '这条对话还没有手工请求。'
        : '这条对话还没有发出去过手工请求。到「请求重发」发一条，这里就会出现。'));
      return;
    }
    const columns = compact ? ['时间', '地址'] : ['时间', '方法', '地址', '状态', '耗时', ''];
    const table = el('table', 'data-table');
    const head = el('tr');
    columns.forEach((label) => head.append(el('th', null, label)));
    // 与「审批记录」那张表同一写法：先建 thead，再把行放进去。旧写法是
    // `el('thead').appendChild(head).parentNode`——结果一样，但它靠 `appendChild`
    // 的返回值再回头取父节点，读起来像在拼两样东西。
    const thead = el('thead');
    thead.append(head);
    table.append(thead);
    const body = el('tbody');
    records.forEach((record) => {
      // 整行可点：这一页的动作是「点一条、右边看它的原文」，而原来只有行尾那颗
      // 「查看」能点——一屏几十行，鼠标要横着走一个身位才落得到目标上。
      // 按钮留着：它是这一行**可点**的唯一可见线索，而整行热区没有形状。
      const tr = el('tr');
      tr.dataset.trafficId = record.id;
      tr.classList.add('traffic-row');
      if (record.id === trafficPicked) tr.classList.add('is-picked');
      tr.addEventListener('click', () => pickTraffic(record.id, compact));
      tr.append(el('td', 'mono', formatTime(record.createdAt)));
      if (!compact) tr.append(el('td', 'mono', record.method));
      const urlCell = el('td', 'mono clip');
      urlCell.textContent = record.url;                 // 不拼 HTML：URL 来自网络
      urlCell.title = record.url;
      tr.append(urlCell);
      if (!compact) {
        // 没拿到响应时状态是 0，而「0」看起来像一个状态码。说清楚。
        tr.append(el('td', 'mono', record.error ? '无响应' : String(record.status)));
        // 耗时走 `formatDuration`（与任务页、对话里的工具卡同一把尺子）：1200 ms 在这里
        // 写成「1.2 s」。同一个数在两页上有两种写法，读的人会以为其中一个是别的量。
        tr.append(el('td', 'mono', formatDuration(record.durationMs)));
        const actionCell = el('td');
        const open = el('button', 'btn sm', '查看');
        open.type = 'button';
        // 这颗按钮与整行是同一个动作。拦一次冒泡：不拦的话一次点击会走两遍
        // （按钮一次、冒泡到行再一次），两遍各自发一次取原文的请求。
        open.addEventListener('click', (event) => {
          event.stopPropagation();
          pickTraffic(record.id, false);
        });
        actionCell.append(open);
        tr.append(actionCell);
      }
      body.append(tr);
    });
    table.append(body);
    host.append(table);
  }

  async function showTraffic(id) {
    const host = $('#traffic-detail');
    if (!host) return;
    clear(host);
    host.append(el('div', 'muted', '正在读取原文…'));
    let record = null;
    try {
      record = await apiGet('/traffic/' + encodeURIComponent(id));
    } catch (error) {
      clear(host);
      host.append(errorBlock('这条记录读不出来：' + error.message));
      return null;
    }
    clear(host);
    const head = el('div', 'traffic-head');
    head.append(el('strong', null, `${record.method} ${record.url}`));
    head.append(el('span', 'muted', record.error ? record.error : `${record.status} ${record.reason}`));
    host.append(head);

    const block = (title, text) => {
      const box = el('section', 'traffic-block');
      box.append(el('h3', 'traffic-block-title', title));
      const pre = el('pre', 'traffic-pre');
      pre.textContent = text || '（空）';                // 正文来自网络，只走 textContent
      box.append(pre);
      return box;
    };
    const headerText = (record.requestHeaders || {});
    host.append(block('请求头', Object.keys(headerText).length
      ? Object.entries(headerText).map(([k, v]) => `${k}: ${v}`).join('\n') : ''));
    host.append(block('请求正文', record.requestBody));
    host.append(block('响应头', (record.responseHeaders || []).map((pair) => pair.join(': ')).join('\n')));
    host.append(block('响应正文' + (record.responseTruncated ? '（已截断）' : ''), record.responseBody));

    // 把这一条带进重发页：这才是「流量」这一页真正的用途——看到一条，改一个字段再发。
    const use = el('button', 'btn sm', '在重发页打开');
    use.type = 'button';
    use.addEventListener('click', () => {
      const repeat = window.PentestPanels.prefillRepeater;
      if (repeat) repeat(record);
      UI.switchTab('repeater');
    });
    host.append(use);
    // 取回来的整条记录交回去：重发页右栏点一行时要用它的正文去填表。
    return record;
  }

  /* ------------------------------------------------------------ 请求重发 */
  let repeaterState = null;

  function repeaterView() {
    const host = $('#repeater-body');
    if (!host) return;
    clear(host);
    const form = el('div', 'repeater-form');

    const field = (label, node) => {
      const wrap = el('label', 'repeater-field');
      wrap.append(el('span', 'field-label', label));
      wrap.append(node);
      return wrap;
    };

    const method = el('select', 'input');
    ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'].forEach((name) => {
      const option = el('option', null, name);
      option.value = name;
      method.append(option);
    });
    method.value = repeaterState.method || 'GET';
    method.addEventListener('change', () => { repeaterState.method = method.value; });

    const url = el('input', 'input mono');
    url.type = 'text';
    // 占位文案里**不写**带协议头的样例地址：那条守卫会把任何
    // 协议 + 主机 + 路径 形状的串当成外链资源（它剥掉 HTML 的 placeholder 属性，
    // 剥不掉 JS 里赋给 `.placeholder` 的字符串），而这条守卫存在的理由是内网离线
    // 环境不能白屏——占位文案写全一个地址，代价是这条守卫从此永远红。
    url.placeholder = '完整地址：协议 + 主机 + 路径';
    url.value = repeaterState.url || '';
    url.addEventListener('input', () => { repeaterState.url = url.value; });

    const headers = el('textarea', 'input mono');
    headers.rows = 5;
    headers.placeholder = '一行一条：Name: value';
    headers.value = repeaterState.headers || '';
    headers.addEventListener('input', () => { repeaterState.headers = headers.value; });

    const body = el('textarea', 'input mono');
    body.rows = 8;
    body.placeholder = '请求正文（GET / HEAD 不要填）';
    body.value = repeaterState.body || '';
    body.addEventListener('input', () => { repeaterState.body = body.value; });

    const send = el('button', 'btn primary', '发送');
    send.type = 'button';

    const line = el('div', 'repeater-line');
    line.append(method, url, send);
    form.append(line);
    form.append(field('请求头', headers));
    form.append(field('请求正文', body));
    host.append(form);

    const out = el('div', 'repeater-out');
    out.id = 'repeater-out';
    host.append(out);

    send.addEventListener('click', async () => {
      // 这颗按钮发的是**真请求**：双击就是两条。POST 一类的动作发两遍不是慢一点
      // 的问题——建了两个用户、下了两次单，而界面上只显示最后那份响应，看起来像
      // 只发了一次。等在飞的这一条回来再放开；失败也要放开（放在 finally 里），
      // 否则一次网络错误会让这颗按钮永久失灵。
      if (send.disabled) return;
      // 头在界面上是"一行一条"的文本。解析放在这里而不是交给后端：
      // 写歪了一行，报错要能指到那一行；后端只能看到一坨结构化数据，
      // 说得出"某条头里有换行"，说不出"你第 3 行少了个冒号"。
      //
      // 解析排在上锁**之前**：它下面有一条 `return`（第 N 行少了个冒号），
      // 而那条 return 走不到 `finally`——锁在它前面上，按钮就永远停在
      // 「发送中…」上，改好再点也没反应，只能刷新整页。
      const parsed = {};
      const lines = (repeaterState.headers || '').split('\n');
      for (let i = 0; i < lines.length; i += 1) {
        const text = lines[i].trim();
        if (!text) continue;
        const at = text.indexOf(':');
        if (at <= 0) {
          clear(out);
          out.append(errorBlock(`第 ${i + 1} 行请求头格式不对：${text}`));
          return;
        }
        parsed[text.slice(0, at).trim()] = text.slice(at + 1).trim();
      }
      send.disabled = true;
      const label = send.textContent;
      send.textContent = '发送中…';
      clear(out);
      out.append(el('div', 'muted', '正在发送…'));
      try {
        // `sessionId` 跟着这一条一起上去：服务端把它写进 `traffic.session_id`，
        // 而「流量」页与右边那一栏都按会话收口。漏了这一格，这里发出去的每一条都
        // 落到"没有会话"里，于是发完就在列表上找不到自己——而列表看上去完全正常。
        const record = await apiPost('/traffic/replay', {
          method: repeaterState.method,
          url: repeaterState.url,
          headers: parsed,
          body: repeaterState.body,
          engagementId: UI.state.eid || '',
          sessionId: trafficSession(),
        });
        clear(out);
        const head = el('div', 'traffic-head');
        head.append(el('strong', null, record.error ? '没有响应' : `${record.status} ${record.reason}`));
        head.append(el('span', 'muted', formatDuration(record.durationMs)));
        out.append(head);
        const pre = el('pre', 'traffic-pre');
        pre.textContent = record.error || record.responseBody || '（空响应）';
        out.append(pre);
        // 正文被 `httpreplay.MAX_BODY`（64 KB）截过要说出来：这里显示的是原文的
        // 前一段，而它看起来和一份完整的响应没有区别。「流量」页的详情早就标了
        // 「（已截断）」，这一页是同一次请求的另一个出口，漏了就成了两套说法。
        if (!record.error && record.responseTruncated) {
          out.append(el('p', 'muted', '响应正文超过 64 KB，上面只是前一段（完整原文没有留下来）。'));
        }
        // 刚发出去的这一条现在就在库里，右边那一栏要立刻长出来——不能等下一轮心跳
        // （最长 3 秒）。静默重取：这一页刚画好的结果块在 `#repeater-out` 里，
        // 列表重画不碰它。
        loadTraffic(true);
      } catch (error) {
        clear(out);
        out.append(errorBlock('发送失败：' + error.message));
      } finally {
        send.disabled = false;
        send.textContent = label;
      }
    });
  }

  function prefillRepeater(record) {
    if (!repeaterState) repeaterState = { method: 'GET', url: '', headers: '', body: '' };
    repeaterState.method = record.method || 'GET';
    repeaterState.url = record.url || '';
    const pairs = Object.entries(record.requestHeaders || {});
    repeaterState.headers = pairs.map(([k, v]) => `${k}: ${v}`).join('\n');
    repeaterState.body = record.requestBody || '';
    if ($('#repeater-body')) repeaterView();
  }

  /* ------------------------------------------------------------ 工作空间 */
  function renderWorkspace() {
    const host = $('#workspace-body');
    if (!host) return;
    clear(host);
    host.append(el('div', 'muted', '正在读取工作区…'));
    apiGet('/workspace').then((data) => {
      clear(host);
      const rows = [
        ['根目录', data.root],
        ['会话目录', data.sessions],
        ['缓存目录', data.cache],
        ['目录是否已建', data.exists ? '是' : '否（还没有写过任何东西）'],
        // 计数走 `formatCount`：这一格是「数一数」，与路径那几行不同——
        // 路径只可能原样印，数字在某天过千时要能一眼读出量级。
        ['会话子目录数', formatCount(data.sessionCount || 0)],
      ];
      const table = el('table', 'kv-table');
      rows.forEach(([key, value]) => {
        const tr = el('tr');
        tr.append(el('th', null, key));
        const cell = el('td', 'mono clip');
        cell.textContent = String(value);                  // 路径来自后端，仍走 textContent
        cell.title = String(value);
        tr.append(cell);
        table.append(tr);
      });
      host.append(table);
      // 这里要的是一枚真的 `<code>` 节点，不是把尖括号转义进字符串：`el()` 写的是
      // textContent，`\u003ccode\u003e` 会原样印在界面上，读者看到的是六个字符而不是
      // 一段命令行。同理也不拼 HTML——这个仓库里 `innerHTML` 是明令禁止的入口。
      const hint = el('p', 'muted', '这一页只读：打开它不会在盘上建目录。要清空请用命令行（');
      hint.append(el('code', null, '--purge'));
      hint.append(document.createTextNode('），因为那是不可撤销的动作。'));
      host.append(hint);
    }).catch((error) => {
      clear(host);
      host.append(errorBlock('工作区信息读不出来：' + error.message));
    });
  }

  /* ------------------------------------------------------------ 导出 */
  // 两页的「导出」都把服务端那份数据原样写进文件，不在浏览器里重排字段：
  // 导出的东西是拿去核账的（贴进工单、交给同事对一遍）。字段少一个、顺序变了，
  // 事后对不上时说不清是数据的问题还是这次导出的问题。
  //
  // 落盘走 Blob + 临时 <a>，与报告页那条下载同一条路（`downloadReport`）：
  // 服务端那边没有 Content-Disposition 通道，文件名与编码本来就得由界面负责一次。
  function saveFile(name, text, type) {
    const link = el('a');
    link.href = URL.createObjectURL(new Blob([text], { type: type }));
    link.download = name;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
  }

  // 文件名里只放时间，不放「副本」「最终」这类词：同一天导两次会互相覆盖或并存，
  // 而那个区别该由人自己决定，不该由文件名替他猜。
  function fileStamp() {
    const now = new Date();
    const pad = (value) => String(value).padStart(2, '0');
    return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
      + `-${pad(now.getHours())}${pad(now.getMinutes())}`;
  }

  /* ------------------------------------------------------------ 审批记录 */
  async function loadApprovals() {
    const host = $('#approvals-body');
    if (!host) return;
    clear(host);
    host.append(el('div', 'muted', '正在读取审查单…'));
    let data = null;
    try {
      // 不带 status：这一页要的是**全部**（含已批准、已拒绝、已过期），
      // 因为问题是"这次是谁点的头"，而不是"现在还有什么在等我"。
      data = await apiGet('/reviews?limit=100');
    } catch (error) {
      clear(host);
      host.append(errorBlock('审查单加载失败：' + error.message));
      return;
    }
    clear(host);
    const reviews = data.reviews || [];
    const meta = $('#approvals-meta');
    // 接口那侧的上限是 100 条（`server.list_reviews` 里写死的），一次不多给。
    // 拿满 100 条时要把这句话说出来：这一页标题是「审批记录」、文档里写的是
    // 「全部审查单」，而屏幕上只有最近一百条——不说，读的人会以为早先那些单子
    // 被谁删了。**不上调上限**：这页会被开着轮询，放大上限等于放大读放大。
    // 上限取响应里的 `limit`（服务端把夹过之后的实际值回过来了），不写死 100：
    // 写死的那份会在接口一侧改动时静默失准——要么在没截断时宣称截断，要么反过来。
    const capped = data.limit && reviews.length >= data.limit
      ? `（只到最近 ${data.limit} 条）` : '';
    if (meta) meta.textContent = `${formatCount(reviews.length)} 条${capped} · 待处理 ${formatCount(data.pendingCount || 0)}`;
    if (!reviews.length) {
      // 档位印中文（`modeLabel`，词表在 `harness.MODE_LABELS`）。原先这里拼的是
      // `data.mode` 原值，于是空态写「闸照状态是「ask」」，而设置页那个下拉框里
      // 同一档写的是「审查任意命令」——同一件事两个词。认不出的取值原样印出来。
      const modeWord = data.modeLabel || data.mode || 'unknown';
      host.append(emptyBlock('还没有审查单。闸照状态是「' + modeWord + '」时，需要审批的动作才会出现在这里。'));
      return;
    }
    const table = el('table', 'data-table');
    const head = el('tr');
    ['时间', '工具', '档位', '摘要', '状态', '决定人', ''].forEach((label) => head.append(el('th', null, label)));
    const thead = el('thead');
    thead.append(head);
    table.append(thead);
    const body = el('tbody');
    reviews.forEach((item) => {
      const tr = el('tr');
      tr.append(el('td', 'mono', formatTime(item.createdAt)));
      tr.append(el('td', 'mono', item.tool || ''));
      // 档位与状态都优先用后端给的中文（`approval.public` 的 tierLabel /
      // statusLabel）。原先印的是 `item.tier` 与 `item.status` 原值，于是这一页
      // 上是 always / strict / pending / expired，而同一次审查在对话里的审批卡片
      // 印的是「等待批准」——同一件事两个词，读的人得自己在脑子里建对照表。
      tr.append(el('td', null, item.tierLabel || item.tier || ''));
      const summary = el('td', 'clip');
      summary.textContent = item.headline || '';        // 摘要里可能有命令原文
      summary.title = item.headline || '';
      tr.append(summary);
      tr.append(el('td', null, item.statusLabel || item.status || ''));
      tr.append(el('td', null, item.decidedBy || '—'));

      // 「展开看参数与判定」是这一页**唯一**的价值所在：摘要那一格只有一行标题，
      // 而要回答「这条命令当时到底要干什么、平台为什么觉得它需要人看一眼」，
      // 得看 reasons 与 arguments——这两样 `approval.public` 早就随列表一起给了，
      // 不取是白扔。手册里写着「展开看参数与判定」，而这一页原先根本没有展开。
      // 展开的内容建在**同表的下一个 tr** 里而不是浮层：浮层要管定位、滚动与
      // 点外面关闭，而这里只有一个诉求——把这几行字放在它旁边。
      const toggleCell = el('td', 'approval-acts');
      const toggle = el('button', 'btn sm', '展开');
      toggle.type = 'button';
      // 「删除」与「展开」挤在同一格：删一条审批记录是个独立动作，但为它单开一列，
      // 表头就要多一个空标题，而这张表本来就有一格是空的（放展开按钮）。
      // 删的是**审批记录本身**，不是证据——链路里的发现与资产不随它消失，
      // 所以确认框里要把这句话写出来，否则没人敢点。
      const drop = el('button', 'btn sm danger', '删除');
      drop.type = 'button';
      drop.title = '删掉这一条审批记录（不影响链路里的证据）';
      drop.addEventListener('click', async () => {
        const ask = `确定删掉这条审批记录？\n\n${item.tool || '这次调用'} · ${item.headline || ''}\n\n`
          + '删掉就找不回来。它只删这条记录本身：链路里的发现与资产不受影响。';
        if (!window.confirm(ask)) return;
        const meta = $('#approvals-meta');
        try {
          await apiPost('/reviews/delete', { reviewId: item.id });
          loadApprovals();
        } catch (error) {
          if (meta) meta.textContent = '删除失败：' + error.message;
        }
      });
      toggleCell.append(toggle);
      toggleCell.append(drop);
      tr.append(toggleCell);
      body.append(tr);

      const detail = el('tr', 'approval-detail-row');
      const cell = el('td');
      cell.colSpan = 7;
      const box = el('div', 'approval-detail');
      if (item.reasons && item.reasons.length) {
        const list = el('ul', 'approval-reasons');
        item.reasons.forEach((text) => list.append(el('li', null, text)));
        box.append(list);
      } else {
        box.append(el('p', 'muted', '这条没有附理由。'));
      }
      // 参数原样摊开（`arguments` 是调用时的那一份，不是现在的状态）。
      // 只走 textContent：里面的值来自模型，可能含命令原文与路径。
      const args = item.arguments && Object.keys(item.arguments).length
        ? JSON.stringify(item.arguments, null, 2) : '';
      if (args) {
        box.append(el('div', 'traffic-block-title', '参数'));
        const pre = el('pre', 'traffic-pre');
        pre.textContent = args;
        box.append(pre);
      }
      if (item.note) box.append(el('p', 'muted', '裁决备注：' + item.note));
      cell.append(box);
      detail.append(cell);
      detail.hidden = true;
      body.append(detail);
      toggle.addEventListener('click', () => {
        detail.hidden = !detail.hidden;
        toggle.textContent = detail.hidden ? '展开' : '收起';
      });
    });
    table.append(body);
    host.append(table);
  }

  /* ------------------------------------------------------------ 日志 */
  async function loadLogs() {
    const host = $('#logs-body');
    if (!host) return;
    const input = $('#logs-limit');
    const limit = input && input.value ? input.value : '200';
    clear(host);
    host.append(el('div', 'muted', '正在读取日志…'));
    let data = null;
    try {
      data = await apiGet('/logs?limit=' + encodeURIComponent(limit));
    } catch (error) {
      clear(host);
      host.append(errorBlock('日志读取失败：' + error.message));
      return;
    }
    clear(host);
    const meta = $('#logs-meta');
    // 两种截断不是一回事，别用一句话说：
    //   * `limit` 是**我这次要几行**（页面自己填的），截了也知道总行数；
    //   * `data.truncated` 是**服务端只读了文件尾部**（`MAX_LOG_BYTES` 是 512 KB），
    //     此时 `data.total` 只是那一段里的行数，不是整个文件的行数。
    // 原先两种情况都写成「N 行（显示最后 M 行）· 已截到尾部」：文件 4 MB 时它会
    // 说「共 200 行」，而屏幕上这些只是最后那 512 KB 里的。
    if (meta) {
      if (!data.sizeBytes) {
        meta.textContent = '日志还是空的（这条路径下没有 access.log）';
      } else if (data.truncated) {
        meta.textContent = `显示最后 ${formatCount(data.lines.length)} 行 · 文件 ${(data.sizeBytes / 1024).toFixed(1)} KB，` +
          '只读了尾部那一段（更早的行这次没有读进来）';
      } else {
        meta.textContent = `${formatCount(data.total)} 行（显示最后 ${formatCount(data.lines.length)} 行）· ` +
          `${(data.sizeBytes / 1024).toFixed(1)} KB`;
      }
    }
    const pre = el('pre', 'traffic-pre logs-pre');
    // 日志行本身含来源 IP 与账号，但它**不是** HTML：只走 textContent。
    pre.textContent = (data.lines || []).join('\n') || '（空）';
    host.append(pre);
  }

  /* ------------------------------------------------------------ 分派 */
  const LOADERS = {
    dashboard: loadDashboard,
    traffic: loadTraffic,
    repeater: () => {
      if (!repeaterState) repeaterState = { method: 'GET', url: '', headers: '', body: '' };
      repeaterView();
      // 右栏那张流量单子也要有东西。`quiet` 为真：这一页刚进来，边上不该闪
      // 一行「正在读取流量…」，表格几毫秒后就位。
      loadTraffic(true);
    },
    workspace: renderWorkspace,
    approvals: loadApprovals,
    logs: loadLogs,
  };

  function load(tab) {
    const fn = LOADERS[tab];
    if (fn) fn();
  }

  window.PentestPanels = { load, prefillRepeater, loadDashboard, loadTraffic, loadApprovals, loadLogs };

  // 「刷新」「清空」这类按钮在这里绑，理由与上面同一条：它们只属于这几页。
  document.addEventListener('DOMContentLoaded', () => {
    const bind = (id, handler) => {
      const node = document.getElementById(id);
      if (node) node.addEventListener('click', handler);
    };
    bind('dashboard-refresh', loadDashboard);
    bind('traffic-refresh', loadTraffic);
    bind('traffic-clear', async () => {
      const host = $('#traffic-detail');
      // 这里要点一次确认，理由与界上其它删除动作一致（`clearJobs`、`clearHubOps`、
      // `removePlugin`、`removeSkill` 都有）：一次点击删掉的是一整张表。
      //
      // 而且删的是**比列表上看到的多**：列表按 `userId` 收口，只列自己那些；
      // 这一版还按**当前这条对话**收口（页头那句「N 条」数的是这条对话里的），
      // 于是屏幕上那几行离「整张表」的距离更远了。**这一条必须写进确认框**：
      // 否则「清空这 3 条」读起来就是「清掉我看到的这 3 条」。
      // 服务端 `clear_traffic()` 不带 `user_id` 就 `DELETE FROM traffic`，整张表。
      // 于是点了「清空全部」的管理员会顺手清掉别人留下的记录——那些记录里躺着
      // 请求头与响应正文，可能带着别人的 Cookie 与令牌。同一句话只写在按钮上
      // 是不够的：按钮写着「全部」，读的人更容易理解成「全部我列出来的这些」。
      //
      // 原生确认框不认 Markdown（同 settings.js 里那条注释），所以这里不写 `**`。
      const scope = trafficCount ? `这 ${formatCount(trafficCount)} 条` : '全部';
      const ask = `确定清空${scope}流量记录？\n\n`
        + '记录里存着请求头与响应正文（可能含 Cookie 与令牌），删掉就找不回来。\n'
        + '清空需要管理员：列表上只列得出现在这条对话里的那些，但清的时候整张表都会清。\n\n'
        + '链路里的证据不受影响——这里清的只是收发记录。';
      if (!window.confirm(ask)) return;
      try {
        const answer = await apiPost('/traffic/clear', {});
        if (host) {
          clear(host);
          host.append(el('p', 'muted', `已清空 ${formatCount(answer.removed || 0)} 条流量记录`));
        }
        loadTraffic();
      } catch (error) {
        if (host) { clear(host); host.append(errorBlock('清空失败：' + error.message)); }
      }
    });
    bind('workspace-refresh', renderWorkspace);
    bind('approvals-refresh', loadApprovals);
    // 导出与清空都不在「刷新」那条路上：一个把整批账取出文件，一个删掉它，
    // 两者都不是「重新看一眼」。
    bind('approvals-export', async () => {
      const meta = $('#approvals-meta');
      try {
        // 导出接口与列表同一道归属闸，但**不截到 100 条**：列表夹那一下是为了
        // 一屏看得完，照那个上限导出来的是「看着完整、其实只有最近一百条」的文件。
        const data = await apiGet('/reviews/export');
        saveFile(`审批记录-${fileStamp()}.json`,
          JSON.stringify(data, null, 2), 'application/json;charset=utf-8');
        if (meta) meta.textContent = `已导出 ${formatCount(data.total)} 条审批记录`;
      } catch (error) {
        if (meta) meta.textContent = '导出失败：' + error.message;
      }
    });
    bind('approvals-clear', async () => {
      const meta = $('#approvals-meta');
      const ask = '确定清空审批记录？\n\n'
        + '删的是审批记录本身（谁批准了哪条命令），链路里的发现与资产不受影响。\n'
        + '范围比列表大：管理员清的是整张表，其他身份只清自己名下对话的那些。\n'
        + '还在等裁决的单子不删 —— 删掉它，发起它的那一轮会一直转到超时。';
      if (!window.confirm(ask)) return;
      try {
        const answer = await apiPost('/reviews/clear', {});
        if (meta) {
          const rest = answer.kept ? `，留下 ${formatCount(answer.kept)} 条` : '';
          const held = answer.awaiting ? `，${formatCount(answer.awaiting)} 条仍在等裁决` : '';
          meta.textContent = `已清掉 ${formatCount(answer.deleted)} 条${rest}${held}`;
        }
        loadApprovals();
      } catch (error) {
        if (meta) meta.textContent = '清空失败：' + error.message;
      }
    });
    bind('logs-refresh', loadLogs);
    bind('logs-export', async () => {
      const meta = $('#logs-meta');
      try {
        // 导出的是**整份**（含滚动出来的 access.log.1），不是屏幕上这一段尾部：
        // 「只读了尾部」这件事是给眼睛省流量的，不是给出文件用的。
        const data = await apiGet('/logs/export');
        saveFile(`访问日志-${fileStamp()}.txt`,
          (data.lines || []).join('\n'), 'text/plain;charset=utf-8');
        if (meta) {
          meta.textContent = `已导出 ${formatCount(data.total)} 行（${(data.sizeBytes / 1024).toFixed(1)} KB）`;
        }
      } catch (error) {
        if (meta) meta.textContent = '导出失败：' + error.message;
      }
    });
    bind('logs-clear', async () => {
      const meta = $('#logs-meta');
      const ask = '确定清空访问日志？\n\n'
        + '连同滚动出来的上一份（access.log.1）一起删，删掉就找不回来。\n'
        + '这一页只列得到尾部一段，但清的是整份文件。\n\n'
        + '清完会留一行说明，所以文件不会是空的 —— 「谁在什么时候清过日志」也是审计要回答的问题。';
      if (!window.confirm(ask)) return;
      try {
        const answer = await apiPost('/logs/clear', {});
        if (meta) meta.textContent = `已清掉 ${(answer.removed / 1024).toFixed(1)} KB 的访问日志`;
        loadLogs();
      } catch (error) {
        if (meta) meta.textContent = '清空失败：' + error.message;
      }
    });
  });
}());
