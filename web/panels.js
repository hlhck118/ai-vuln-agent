/* 新增八页的渲染（仪表盘 / 任务 / 计划任务 / 流量 / 请求重发 / 工作空间 /
 * 审批记录 / 日志）。
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
    formatCount, formatDuration, formatBytes, API_BASE,
    jobView, severityView, switchTab } = UI;

  /* ------------------------------------------------------------ 仪表盘 */
  // 一次 `/dashboard` 换整屏读数：它回答「平台上现在什么样」——引擎模式、任务
  // 状态分布、资产类型分布、链路构成、作业队列、系统指标。**不再读 `/panel`**：
  // 那条接口服务的是「某个目标 + 最近四条」，两份视图的取舍不同，混用会让同一屏
  // 上的数字来自两个时刻。
  function statCard(host, label, value, hint) {
    const box = el('div', 'dash-stat');
    box.append(el('div', 'dash-stat-label', label));
    // `formatCount` 加千分位：这里是「扫一眼读量级」的地方，1200 与 1,200 在读的
    // 时候不是一回事。
    box.append(el('div', 'dash-stat-value', formatCount(value)));
    if (hint) box.append(el('div', 'dash-stat-hint', hint));
    host.append(box);
  }

  function dashCard(title) {
    const card = el('section', 'dash-card');
    card.append(el('h2', 'dash-card-title', title));
    return card;
  }

  function kvRow(list, label, value) {
    const row = el('div', 'dash-row');
    row.append(el('span', 'dash-row-main', label));
    row.append(el('span', 'dash-row-meta', value == null || value === '' ? '—' : String(value)));
    list.append(row);
    return row;
  }

  function countOf(map) {
    let total = 0;
    Object.keys(map || {}).forEach((key) => { total += Number(map[key]) || 0; });
    return total;
  }

  // 字节数只在指标卡里出现，写一个本地小函数就够，不必再进 PentestUI。
  function bytesText(value) {
    const size = Number(value) || 0;
    if (size >= 1024 * 1024 * 1024) return (size / 1024 / 1024 / 1024).toFixed(1) + ' GB';
    if (size >= 1024 * 1024) return (size / 1024 / 1024).toFixed(0) + ' MB';
    if (size >= 1024) return (size / 1024).toFixed(0) + ' KB';
    return size + ' B';
  }

  function percentText(block) {
    if (!block || !block.totalBytes) return '—';
    return (block.percent || 0) + '%（' + bytesText(block.usedBytes) + ' / '
      + bytesText(block.totalBytes) + '）';
  }

  async function loadDashboard() {
    const host = $('#dashboard-body');
    if (!host) return;
    clear(host);
    host.append(el('div', 'muted', '正在读取概览…'));
    let data = null;
    try {
      data = await apiGet('/dashboard');
    } catch (error) {
      clear(host);
      host.append(errorBlock('概览加载失败：' + error.message));
      return;
    }
    let metrics = null;
    try {
      metrics = await apiGet('/system/metrics');
    } catch (error) {
      // 指标是附加项：读不到不该让整屏空着。这一句也是给「为什么指标卡是空的」
      // 留的解释入口（卡片自己会再写一遍）。
      metrics = null;
    }
    clear(host);
    const labels = data.statusLabels || {};
    const tasks = data.tasks || {};
    const findings = data.findings || {};
    const jobs = data.jobs || {};
    const exploration = data.exploration || {};
    const meta = $('#dashboard-meta');
    if (meta) {
      meta.textContent = '引擎 ' + (labels[data.engineMode] || data.engineMode || '未知')
        + ' · 更新于 ' + formatTime(data.generatedAt);
    }

    const stats = el('div', 'dash-stats');
    statCard(stats, '任务', tasks.all || 0,
      '进行中 ' + formatCount(tasks.running || 0) + ' · 排队 ' + formatCount(tasks.queued || 0));
    statCard(stats, '资产', data.assets || 0, '按类型分布见下');
    statCard(stats, '漏洞', countOf(findings),
      '高危及以上 ' + formatCount((findings.critical || 0) + (findings.high || 0)));
    statCard(stats, '作业', jobs.total || 0,
      '待跑 ' + formatCount(jobs.pending || 0) + ' · 在跑 ' + formatCount(jobs.running || 0));
    statCard(stats, '流量', data.traffic || 0, '收发记录');
    host.append(stats);

    const active = data.activeTask;
    const activeCard = dashCard('当前任务');
    if (!active) {
      activeCard.append(emptyBlock('还没有任务：到「任务」页新建一个目标，就有第一条了。'));
    } else {
      activeCard.append(el('p', 'muted',
        (active.name || active.target || '未命名') + ' · '
        + (labels[active.status] || active.status || '')
        + (active.paused ? ' · 已暂停' : '')));
      if (active.goal) activeCard.append(el('p', 'muted', '目标：' + active.goal));
      const list = el('div', 'dash-list');
      kvRow(list, '目标达成', formatCount(active.goalsMet || 0) + ' / ' + formatCount(active.goalsTotal || 0));
      kvRow(list, '发现', countOf(active.findings || {}));
      kvRow(list, '在途作业', formatCount(active.inFlight || 0));
      kvRow(list, '累计运行', formatDuration(active.runSeconds || 0));
      kvRow(list, '最近活动', active.lastActivity ? formatTime(active.lastActivity) : '—');
      activeCard.append(list);
    }
    host.append(activeCard);

    // 两张「明细」卡都放在当前任务之后、链路之前：它们回答的是「现在最该管的事」，
    // 档位序与状态词都走共享表（severityView / jobView），不在这里再抄一份——抄了之后
    // 加一档、改一个说法，两处就会各说各话。
    const risky = (data.topFindings || [])
      .filter((item) => severityView.rank(item.severity) <= 1);
    const riskyCard = dashCard('高危漏洞');
    if (!risky.length) {
      riskyCard.append(emptyBlock('还没有高危及以上漏洞'));
    } else {
      const riskyList = el('div', 'dash-list');
      risky.forEach((item) => {
        const row = el('div', 'dash-row');
        const main = el('span', 'dash-row-main');
        main.append(el('span', `sev-dot sev-${severityView.of(item.severity)}`));
        main.append(el('span', 'finding-desc', item.name || '未命名漏洞'));
        row.append(main);
        row.append(el('span', 'dash-row-meta',
          (severityView.label[item.severity] || '信息') + ' · ' + (item.target || '—')));
        riskyList.append(row);
      });
      riskyCard.append(riskyList);
    }
    host.append(riskyCard);

    const recent = data.recentJobs || [];
    const jobCard = dashCard('最近作业');
    if (!recent.length) {
      jobCard.append(emptyBlock('还没有派过作业'));
    } else {
      const jobList = el('div', 'dash-list');
      recent.forEach((job) => {
        const row = el('div', 'dash-row');
        row.append(el('span', 'dash-row-main',
          jobView.kindLabel(job.kind) + ' · ' + (job.target || '—')));
        row.append(el('span', 'dash-row-meta',
          jobView.label(job.status) + ' · ' + formatTime(job.createdAt)));
        jobList.append(row);
      });
      jobCard.append(jobList);
    }
    host.append(jobCard);

    const chainCard = dashCard('探索链路');
    const chain = el('div', 'dash-list');
    kvRow(chain, '目标', exploration.goal || 0);
    kvRow(chain, '意图', exploration.intent || 0);
    kvRow(chain, '事实', exploration.fact || 0);
    kvRow(chain, '漏洞', exploration.finding || 0);
    kvRow(chain, '摘要', exploration.digest || 0);
    chainCard.append(chain);
    host.append(chainCard);

    const taskCard = dashCard('任务状态');
    const taskList = el('div', 'dash-list');
    ['running', 'queued', 'paused', 'done', 'idle', 'archived'].forEach((key) => {
      kvRow(taskList, labels[key] || key, formatCount(tasks[key] || 0));
    });
    taskCard.append(taskList);
    host.append(taskCard);

    const assetMap = data.assetCounts || {};
    const assetCard = dashCard('资产类型');
    const assetKeys = Object.keys(assetMap).sort();
    if (!assetKeys.length) {
      assetCard.append(emptyBlock('还没有收集到资产'));
    } else {
      const assetList = el('div', 'dash-list');
      assetKeys.forEach((key) => kvRow(assetList, key, formatCount(assetMap[key])));
      assetCard.append(assetList);
    }
    host.append(assetCard);

    const metricCard = dashCard('系统指标');
    if (!metrics) {
      metricCard.append(emptyBlock('没有读到系统指标（接口不可用）'));
    } else {
      const metricList = el('div', 'dash-list');
      const cpu = metrics.cpu || {};
      kvRow(metricList, 'CPU', (cpu.percent || 0) + '% · ' + formatCount(cpu.cores || 0) + ' 核');
      kvRow(metricList, '内存', percentText(metrics.memory));
      kvRow(metricList, '系统盘', percentText(metrics.disk));
      kvRow(metricList, '数据盘', percentText(metrics.dataDisk));
      if ((metrics.loadAverage || []).length) {
        kvRow(metricList, '负载', metrics.loadAverage.join(' / '));
      }
      (metrics.notes || []).forEach((note) => kvRow(metricList, '说明', note));
      metricCard.append(metricList);
    }
    host.append(metricCard);
  }

  /* ---------------------------------------------------------------- 任务 */
  // 这一页与「作业」页（jobs）不是一回事：那边一行是一次**执行**（一次扫描、一次
  // 探测，跑完就结束），这边一行是一个**目标**（E-xxxx，有目标、能暂停、会攒作业
  // 和漏洞）。**任务是一个目标，作业是一次执行**。
  //
  // 「状态」是**派生**的（后端 Store._task_status）：暂停优先于进行中，因为一个
  // 被暂停的目标上可能还有一条早先派出去的作业在跑。界面上照搬后端的判定，不自己
  // 再算一遍——两处各算一次就是两个状态词在同一个人眼前打架。
  //
  // 对照 ARTEX 补齐了四件事：分类 / 模板 / 新建 / 归档中心。
  //   · 分类回答「这条业务线下还有多少在跑」，计数由后端分三档给（全部/在办/归档）——
  //     把归档的算进去会让这个数字一直虚高，而虚高的数字没人会去核对；
  //   · 模板存**配置**，复制带**证据**，两件事不混：模板里混进上一次的探测结果是
  //     最容易发生、也最难发现的一种脏数据；
  //   · 新建走 store.create_task —— 与对话里长出来的任务落到同一张表、同一个写入器，
  //     所以列表里不必区分「这条是手建的还是说出来的」；
  //   · 归档中心把那批收起来的按分类分组摆出来：归档的用途是从默认视图里收起来，
  //     于是「我记得有一个跑过 xxx 的任务」只能去那儿找。
  //
  // 任务详情另有九块（情报 / 覆盖度 / 复测 / 拦截规则 / 模型 / 意图 / 范围 / 文件 /
  // 会话状态），**各自单独取数**：打开详情就同时打九个请求，多数时候人只看其中
  // 一块，剩下八份是白拉的。
  const TASK_STATES = ['running', 'queued', 'paused', 'done', 'idle'];
  const TASK_STATE_TEXT = {
    running: '进行中', queued: '排队中', paused: '已暂停', done: '已完成', idle: '未开始',
  };
  // 九块的顺序即阅读顺序：先看它攒了什么（情报），再看还剩多少没动（覆盖度），
  // 接着看动过的那些**修好了没**（复测）——这三块回答「打到哪一步了」。然后是
  // 「它跑出了什么」（文件）与「它现在还在动吗」（会话状态），这两块回答「此刻
  // 什么样」；后面才是「它会被什么拦住」（拦截规则）与「它用哪个模型」，这两块
  // 回答「为什么是这样」。意图与范围放最后：那是动手改配置的地方。
  const TASK_BLOCKS = [
    { key: 'intel', label: '情报', hint: '凭据 / 线索 / 敏感数据' },
    { key: 'coverage', label: '覆盖度', hint: '哪些资产动过、哪些还没有' },
    { key: 'findings', label: '复测', hint: '漏洞修好了没，每一次复核都留着' },
    { key: 'files', label: '文件', hint: '它名下对话的工作区里留下了什么' },
    { key: 'chat', label: '会话状态', hint: '现在还有哪条对话在跑' },
    { key: 'intercept', label: '拦截规则', hint: '全局 + 本任务合并后的那一组' },
    { key: 'llm', label: '模型', hint: '这条任务最终生效的那一份配置' },
    { key: 'intents', label: '意图', hint: '暂停 / 重跑 / 结束一条线' },
    { key: 'scope', label: '范围', hint: '允许 / 禁止条目，全量替换' },
  ];
  // 情报的三类与中文名。词表与后端 store.TASK_INTEL_LABELS 同源：这里只是
  // 没联网时的兜底，接口每次都会把 kindLabels 一起给回来。
  const TASK_INTEL_KINDS = ['credential', 'lead', 'sensitive_data'];
  const TASK_INTEL_LABELS = {
    credential: '凭据', lead: '线索', sensitive_data: '敏感数据',
  };
  const TASK_INTEL_STATUS = { observed: '亲眼看到', inferred: '推断' };
  const TASK_PROFILE_SOURCE = {
    task: '本任务指定的', global: '全局激活项', none: '还没有配置模型',
  };
  let tasksData = null;
  let tasksFilter = { status: '', archived: false, keyword: '', categoryId: '' };
  let tasksOpen = '';
  let tasksNote = { text: '', bad: false };
  // 分类与模板一次取全（它们都很小），模型列表只取 id 与显示名——新建表单里的
  // 那个下拉要能说清「这条任务用哪个模型」，而这个选择在别处（模型页）维护。
  let tasksCatalog = null;
  let tasksCatalogError = '';
  // 新建表单与归档中心两块状态。表单打开时记着当前选的模板（换模板要能重填），
  // 归档中心记着上次取回来的分组读数——它只在展开时取一次。
  let tasksForm = { open: false, templateId: '' };
  let tasksArchive = null;
  // 详情缓存：payload 是 get_task 的整份读数，open 是当前展开的那一块，data 按
  // 块名缓存已取回来的读数——切一块不重取另一块，翻回来也不重取。
  let taskDetail = { taskId: '', payload: null, open: '', data: {} };
  // 就地看文件：只记「现在铺开的是哪一条」和它的读数。切任务、再点一次、或者
  // 换一个文件都覆盖它——预览是临时视角，不是要留着的状态。
  let taskPeek = { path: '', data: null };
  // 情报的「改」是原地展开一条表单，不是弹窗：弹窗会盖住同一组里的其他几条，
  // 而改情报时最常要对照的正是旁边那几条（同一次探测留下的几条往往一起写）。
  let taskIntelEdit = { id: '', data: null };
  // 改规则时最常要对照的也是旁边那几条。
  let taskRuleEdit = { id: '', data: null };
  // 改目标那张表：与情报、规则同一套开合状态。
  let taskGoalEdit = { id: '', data: null };
  // 记一次复测的表单：同样原地展开。它挂在漏洞那一行下面，因为它要写的东西
  // （结论与依据）是**那条漏洞**的历史，不是这一块的历史。
  let taskRetestEdit = { id: '', data: null };
  // 「这条线说了什么」：意图下挂的事实就是它自己的那串记录。与文件预览同一套
  // 开合语义——一次只铺一条，再点一次收起，切块就丢。
  let taskIntentPeek = { id: '', data: null };

  function taskState(task) {
    return TASK_STATE_TEXT[task.status] || task.status || '未知';
  }

  function taskCounts(task) {
    const findings = task.findings || {};
    const jobs = task.jobs || {};
    const parts = [];
    parts.push('漏洞 ' + formatCount(findings.total || 0));
    parts.push('作业 ' + formatCount(jobs.total || 0)
      + (jobs.running ? '（在跑 ' + formatCount(jobs.running) + '）' : ''));
    parts.push('目标 ' + formatCount(task.goalsMet || 0) + '/'
      + formatCount(task.goalsTotal || 0));
    if (task.sessionCount) parts.push('对话 ' + formatCount(task.sessionCount));
    if (task.runSeconds) parts.push('运行 ' + formatDuration(task.runSeconds));
    return parts.join(' · ');
  }

  function taskNote(text, bad) {
    tasksNote = { text: text || '', bad: !!bad };
  }

  // 一个动作请求：成功后刷新列表。写接口自身会回权威计数，但列表的顺序（置顶、
  // 最近活动）也是这次动作的结果，所以照旧重取一遍，避免前端自己维护顺序。
  async function taskAction(path, body, done) {
    try {
      await apiPost(path, body || {});
      taskNote(done || '已提交', false);
    } catch (error) {
      taskNote(error.message, true);
    }
    await loadTasks();
  }

  function catalogCategories() {
    return (tasksCatalog && tasksCatalog.categories) || [];
  }

  function catalogTemplates() {
    return (tasksCatalog && tasksCatalog.templates) || [];
  }

  function catalogProfiles() {
    return (tasksCatalog && tasksCatalog.models) || [];
  }

  function categoryName(task) {
    if (task.categoryName) return task.categoryName;
    if (!task.categoryId) return '';
    const found = catalogCategories().filter((item) => item.id === task.categoryId)[0];
    return found ? found.name : '';
  }

  async function loadTaskCatalog() {
    // 三条读数一起取：它们各自都很小，而页面上「分类下拉 + 模板下拉 + 模型下拉」
    // 三处同时缺一个，表现都是同一个空下拉，分三次失败只会让人多点两次刷新。
    try {
      const answer = await Promise.all([
        apiGet('/task-categories'),
        apiGet('/task-templates'),
        apiGet('/models'),
      ]);
      tasksCatalog = {
        categories: (answer[0] && answer[0].categories) || [],
        uncategorized: (answer[0] && answer[0].uncategorized) || null,
        templates: (answer[1] && answer[1].templates) || [],
        models: (answer[2] && answer[2].profiles) || [],
        activeModelId: (answer[2] && answer[2].activeId) || '',
      };
      tasksCatalogError = '';
    } catch (error) {
      tasksCatalog = null;
      tasksCatalogError = '分类与模板加载失败：' + error.message;
    }
  }

  function openTask(taskId) {
    if (tasksOpen === taskId) {
      stopTaskChatTimer();
      tasksOpen = '';
      taskPeek = { path: '', data: null };
      taskIntelEdit = { id: '', data: null };
      taskRuleEdit = { id: '', data: null };
      taskGoalEdit = { id: '', data: null };
      taskRetestEdit = { id: '', data: null };
      taskIntentPeek = { id: '', data: null };
      taskDetail = { taskId: '', payload: null, open: '', data: {} };
      renderTasks();
      return;
    }
    // 换到另一条任务：上一条的会话状态定时器必须在这里停掉，否则它会继续替
    // 一条已经不在屏幕上的任务每两秒打一次请求。
    stopTaskChatTimer();
    tasksOpen = taskId;
    taskPeek = { path: '', data: null };
    taskIntelEdit = { id: '', data: null };
    taskRuleEdit = { id: '', data: null };
    taskGoalEdit = { id: '', data: null };
    taskRetestEdit = { id: '', data: null };
    taskIntentPeek = { id: '', data: null };
    taskDetail = { taskId: taskId, payload: null, open: '', data: {} };
    renderTasks();
    refreshTaskDetail(taskId);
  }

  async function refreshTaskDetail(taskId) {
    let payload = null;
    try {
      payload = await apiGet('/tasks/' + encodeURIComponent(taskId));
    } catch (error) {
      taskNote('读取任务详情失败：' + error.message, true);
      tasksOpen = '';
      renderTasks();
      return;
    }
    if (tasksOpen !== taskId) return;
    taskDetail.payload = payload;
    renderTasks();
  }

  // 展开/收起详情里的一块。已经取回来的读数直接用，不重取——切来切去是这一块
  // 最常见的用法，每切一次打一个请求等于让人替界面的缓存买单。
  function taskBlockPath(taskId, key) {
    return {
      intel: '/tasks/' + encodeURIComponent(taskId) + '/intelligence',
      coverage: '/tasks/' + encodeURIComponent(taskId) + '/coverage',
      findings: '/tasks/' + encodeURIComponent(taskId) + '/findings',
      files: '/tasks/' + encodeURIComponent(taskId) + '/files',
      chat: '/tasks/' + encodeURIComponent(taskId) + '/chat/status',
      intercept: '/tasks/' + encodeURIComponent(taskId) + '/intercept-rules',
      llm: '/tasks/' + encodeURIComponent(taskId) + '/llm',
      scope: '/tasks/' + encodeURIComponent(taskId) + '/scope',
    }[key];
  }

  // 取一块的读数（不画）。写动作之后要用它把那一块换成新的，而不是整页重刷——
  // 整棵详情树拆了重画会把正在填的表单里半行字一起丢掉。
  async function fetchTaskBlock(taskId, key) {
    const path = taskBlockPath(taskId, key);
    // 意图这一块的数据源是链路的合并视图（'/engagements/<id>/orchestration'），不是
    // tasks 下的子资源，所以它没有走上面那张路径表。**必须留成字面量模板直接喂给
    // apiGet**：界面契约守卫靠这两条正则还原动态路由，绕一层函数它就看不见这条
    // 路径了——症状是守卫全绿、界面对着不存在的接口发请求。
    const call = path
      ? apiGet(path)
      : apiGet(`/engagements/${encodeURIComponent(taskId)}/orchestration`);
    try {
      taskDetail.data[key] = await call;
    } catch (error) {
      taskDetail.data[key] = { error: '读取失败：' + error.message };
    }
    if (tasksOpen === taskId && taskDetail.open === key) renderTasks();
  }

  async function toggleTaskBlock(taskId, key) {
    if (taskDetail.taskId !== taskId) return;
    // 换块与收块都要先停掉上一块的自刷新：定时器是**这一块**的，它跟着块走。
    stopTaskChatTimer();
    // 预览也一起丢：它铺在文件清单下面，清单收起来了读数还留着，下次展开会先
    // 闪一屏上次看的内容。切到别的块一样要丢——那行已经不在屏幕上了。
    if (key !== 'files' || taskDetail.open === 'files') taskPeek = { path: '', data: null };
    // 改情报的表单也一样：切到别的块时它还开着，回来时那条记录可能已经不在了。
    if (key !== 'intel' || taskDetail.open === 'intel') taskIntelEdit = { id: '', data: null };
    // 改规则那张表同理。
    if (key !== 'intercept' || taskDetail.open === 'intercept') taskRuleEdit = { id: '', data: null };
    // 改目标那张表跟着意图块走。
    if (key !== 'intents' || taskDetail.open === 'intents') taskGoalEdit = { id: '', data: null };
    // 意图下的记录预览跟着意图块走。
    if (key !== 'intents' || taskDetail.open === 'intents') {
      taskIntentPeek = { id: '', data: null };
    }
    // 复测那张表跟着复测块走。
    if (key !== 'findings' || taskDetail.open === 'findings') {
      taskRetestEdit = { id: '', data: null };
    }
    if (taskDetail.open === key) {
      taskDetail.open = '';
      renderTasks();
      return;
    }
    taskDetail.open = key;
    renderTasks();
    if (Object.prototype.hasOwnProperty.call(taskDetail.data, key)) {
      if (key === 'chat') startTaskChatTimer(taskId);
      return;
    }
    await fetchTaskBlock(taskId, key);
    if (key === 'chat' && tasksOpen === taskId && taskDetail.open === 'chat') {
      startTaskChatTimer(taskId);
    }
  }
  function taskDetailFacts(detail) {
    // 一个任务「为什么叫这个名字、打的是谁、谁授权、哪几条对话挂在下面」——这几行
    // 是任务与对话之间那条链的凭据，漏掉它们，详情页就只剩一串作业。名字与说明
    // 都来自**第一句话**，所以标题用后端原样给的 goal/description，不重写。
    const facts = el('div', 'dash-list');
    const lines = [];
    if (detail.goal) lines.push(['目标', detail.goal]);
    if (detail.description) lines.push(['说明', detail.description]);
    if (detail.authorization) lines.push(['授权依据', detail.authorization]);
    if (detail.stopReason) lines.push(['停止原因', detail.stopReason]);
    if (detail.remark) lines.push(['备注', detail.remark]);
    lines.forEach((pair) => {
      const row = el('div', 'dash-row');
      row.append(el('span', 'dash-row-main', pair[0]));
      row.append(el('span', 'dash-row-meta', pair[1]));
      facts.append(row);
    });
    (detail.sessions || []).forEach((item) => {
      const row = el('div', 'dash-row');
      row.append(el('span', 'dash-row-main', item.title || item.id));
      row.append(el('span', 'dash-row-meta', '对话 · '
        + formatCount(item.messageCount || 0) + ' 条消息 · '
        + formatTime(item.updatedAt || 0)));
      facts.append(row);
    });
    return facts;
  }

  function taskField(label, node) {
    const box = el('label', 'sched-field');
    box.append(el('span', 'sched-label', label));
    box.append(node);
    return box;
  }

  function taskBlockHead(text) {
    return el('div', 'task-block-head', text);
  }

  function taskBlockIntel(taskId, data) {
    const box = el('div', 'task-block');
    if (data.error) {
      box.append(el('p', 'task-block-note', data.error));
      return box;
    }
    const labels = (data && data.kindLabels) || TASK_INTEL_LABELS;
    const items = (data && data.items) || [];
    if (!items.length) {
      box.append(el('p', 'task-block-note',
        '还没有情报。凭据、线索、敏感数据都记在这里——它们不构成漏洞，'
        + '但下一轮动手要靠它们。'));
    } else {
      const list = el('div', 'dash-list');
      items.forEach((item) => {
        const row = el('div', 'dash-row');
        row.append(el('span', 'dash-row-main',
          (labels[item.kind] || item.kindLabel || item.kind || '情报') + ' · '
          + (item.title || '（无标题）')));
        row.append(el('span', 'dash-row-meta',
          (item.content || '') + ' · '
          + (TASK_INTEL_STATUS[item.status] || item.status || '') + ' · '
          + formatTime(item.createdAt || 0)));
        // 改与删要管理员，与后端同一档：这两颗按 data.admin 决定画不画。
        // 画了不该画的按钮比不画更糟——人点了回 403，会以为是坏了。
        if (data.admin) {
          const acts = el('div', 'task-acts');
          const flip = el('button', 'btn xs',
            item.status === 'inferred' ? '标为亲眼看到' : '标为推断');
          flip.type = 'button';
          flip.dataset.act = 'intel-status';
          flip.dataset.id = taskId;
          flip.dataset.intel = item.id;
          acts.append(flip);
          const edit = el('button', 'btn xs',
            taskIntelEdit.id === item.id ? '收起' : '改');
          edit.type = 'button';
          edit.dataset.act = 'intel-edit';
          edit.dataset.id = taskId;
          edit.dataset.intel = item.id;
          acts.append(edit);
          const drop = el('button', 'btn xs', '删');
          drop.type = 'button';
          drop.dataset.act = 'intel-del';
          drop.dataset.id = taskId;
          drop.dataset.intel = item.id;
          acts.append(drop);
          row.append(acts);
        }
        list.append(row);
        if (taskIntelEdit.id === item.id) list.append(taskIntelEditForm(taskId, item));
      });
      box.append(list);
    }
    const form = el('div', 'task-form');
    form.dataset.taskIntel = taskId;
    const kind = el('select', 'select xs-select');
    TASK_INTEL_KINDS.forEach((key) => {
      const option = el('option', '', labels[key] || key);
      option.value = key;
      kind.append(option);
    });
    kind.dataset.field = 'kind';
    form.append(taskField('类别', kind));
    [['title', '标题', '这台机器上有可复用的口令'],
      ['content', '内容', '具体是什么、在哪儿看到的'],
      ['evidence', '证据', '可选：命令输出、响应片段']].forEach((spec) => {
      const input = el('input', 'input');
      input.type = 'text';
      input.placeholder = spec[2];
      input.dataset.field = spec[0];
      form.append(taskField(spec[1], input));
    });
    const submit = el('button', 'btn sm', '记一条情报');
    submit.type = 'button';
    submit.dataset.act = 'intel-add';
    submit.dataset.id = taskId;
    const acts = el('div', 'task-form-actions');
    acts.append(submit);
    form.append(acts);
    box.append(form);
    return box;
  }

  // 改一条情报：**原地展开**在那一行下面。
  //
  // 标题与内容在后端是挤在同一个 detail 里的（"标题：内容"），所以改其中一半时
  // 另一半必须由这里一起带上——只发 status 的那一次会把标题冲掉。表单里两个输入
  // 都预填了当前值，正是为了让"只改状态"的路径也把原文带回去。
  function taskIntelEditForm(taskId, item) {
    const form = el('div', 'task-form');
    form.dataset.taskIntelEdit = item.id;
    const kind = el('select', 'select xs-select');
    TASK_INTEL_KINDS.forEach((key) => {
      const option = el('option', '', (TASK_INTEL_LABELS || {})[key] || key);
      option.value = key;
      kind.append(option);
    });
    kind.dataset.field = 'kind';
    kind.value = item.kind || TASK_INTEL_KINDS[0];
    form.append(taskField('类别', kind));
    const status = el('select', 'select xs-select');
    Object.keys(TASK_INTEL_STATUS).forEach((key) => {
      const option = el('option', '', TASK_INTEL_STATUS[key]);
      option.value = key;
      status.append(option);
    });
    status.dataset.field = 'status';
    status.value = item.status || 'observed';
    form.append(taskField('状态', status));
    [['title', '标题', item.title || ''],
      ['content', '内容', item.content || ''],
      ['evidence', '证据', item.evidence || '']].forEach((spec) => {
      const input = el('input', 'input');
      input.type = 'text';
      input.placeholder = spec[1];
      input.value = spec[2];
      input.dataset.field = spec[0];
      form.append(taskField(spec[1], input));
    });
    const save = el('button', 'btn sm', '存下这一条');
    save.type = 'button';
    save.dataset.act = 'intel-save';
    save.dataset.id = taskId;
    save.dataset.intel = item.id;
    const acts = el('div', 'task-form-actions');
    acts.append(save);
    form.append(acts);
    return form;
  }

  // 覆盖度：资产是这一块的分母，所以它既是读数也是入口。此前只能在对话里让模型
  // 写 `insert_assets`——界面上列着一排资产，却补不进漏掉的那台机器、删不掉记错的
  // 那一条，只能回去跟模型说一句话再等一轮。
  //
  // 类型的中文说法与合法取值不在这里另抄：后端把 `assetTypes` / `assetTypeLabels`
  // 跟读数一起带回来，`models.ASSET_TYPE_LABELS` 改一次这边就跟着变一次。
  function taskBlockCoverage(taskId, data) {
    const box = el('div', 'task-block');
    if (data.error) {
      box.append(el('p', 'task-block-note', data.error));
      return box;
    }
    const summary = data.summary || {};
    box.append(el('p', 'task-block-note',
      (data.enabled ? '' : '这条任务没开覆盖度跟踪。')
      + '资产 ' + formatCount(summary.assets || 0)
      + ' · 动过 ' + formatCount(summary.tested || 0)
      + ' · 还没动 ' + formatCount(summary.untested || 0)
      + ' · 覆盖 ' + formatCount(summary.percent || 0) + '%'));
    const assets = data.assets || [];
    const labels = data.assetTypeLabels || {};
    if (!assets.length) {
      box.append(el('p', 'task-block-note',
        data.admin
          ? '这条任务还没有资产——先跑一次落点探测，或者在下面自己补一条。'
          : '这条任务还没有资产——先跑一次落点探测。'));
    } else {
      const list = el('div', 'dash-list');
      assets.forEach((asset) => {
        const row = el('div', 'dash-row');
        row.append(el('span', 'dash-row-main',
          (asset.typeLabel || labels[asset.type] || asset.type || '资产')
          + ' · ' + (asset.value || '')));
        row.append(el('span', 'dash-row-meta',
          (asset.tested ? '动过' : '还没动')
          + ' · 意图 ' + formatCount(asset.intents || 0)
          + ' · 漏洞 ' + formatCount(asset.findings || 0)));
        if (data.admin && asset.assetId) {
          const acts = el('span', 'task-acts');
          const drop = el('button', 'btn xs', '删');
          drop.type = 'button';
          drop.dataset.act = 'asset-del';
          drop.dataset.id = taskId;
          drop.dataset.asset = asset.assetId;
          acts.append(drop);
          row.append(acts);
        }
        list.append(row);
      });
      box.append(list);
    }
    if (data.admin) {
      const form = el('div', 'task-form');
      form.dataset.taskAssetAdd = taskId;
      const typePick = el('select', 'select xs-select');
      typePick.dataset.field = 'type';
      (data.assetTypes || []).forEach((value) => {
        const node = el('option', '', labels[value] || value);
        node.value = value;
        typePick.append(node);
      });
      form.append(taskField('类型', typePick));
      const valueInput = el('input', 'input');
      valueInput.type = 'text';
      valueInput.placeholder = '一条，例如 10.10.10.160 或 22/tcp ssh';
      valueInput.dataset.field = 'value';
      form.append(taskField('值', valueInput));
      // 父资产是个下拉而不是让人手打 id：`service` 的身份要连父资产一起比，
      // 打错一个字符就会在覆盖度里多出一台机器。
      const parentPick = el('select', 'select xs-select');
      parentPick.dataset.field = 'parentId';
      const none = el('option', '', '（无父资产）');
      none.value = '';
      parentPick.append(none);
      assets.forEach((asset) => {
        const node = el('option', '',
          (asset.typeLabel || labels[asset.type] || asset.type || '资产')
          + ' · ' + (asset.value || ''));
        node.value = asset.assetId;
        parentPick.append(node);
      });
      form.append(taskField('父资产', parentPick));
      const acts = el('div', 'task-form-actions');
      const add = el('button', 'btn sm', '补一条');
      add.type = 'button';
      add.dataset.act = 'asset-add';
      add.dataset.id = taskId;
      acts.append(add);
      form.append(acts);
      box.append(form);
      box.append(el('p', 'task-block-note',
        '按身份去重：同一个 IP 再写一次不会多出一行，只会告诉你「已经有了」。'
        + '删父资产不连坐子资产，也不删漏洞——删完会说谁还指着空处。'));
    } else {
      box.append(el('p', 'task-block-note', '补资产与删资产要管理员，与后端同一档；这里只给看。'));
    }
    return box;
  }
  // 文件区：任务没有自己的目录，它的文件就是**它名下那几条对话的工作区**（模型
  // 写东西时 cwd 就在那里）。所以这一块列的是「一列根」而不是一棵树——一条任务
  // 可能被好几条对话共用，把它们的目录混成一棵树会让人分不清哪份是哪次跑出来的。
  //
  // 只读：这一块不提供删除、不提供上传。工作区里那份才是原件，界面上做删除会
  // 变成「点错一下就少了一份证据」，而这件事没有撤销键。
  // 就地看一个文本文件：点「查看」把前 2 MiB 拉回来铺在那一行下面。
  //
  // 为什么不是「点文件名就下载」：多数时候人只是想确认「这是不是我要的那份」，
  // 而下载要占一个文件位置、还要再找一个能打开它的程序。二进制文件不在这里硬
  // 解成乱码——后端只回 `binary: true`，这里照实说去下载，那比铺一段 `\x00\x01` 诚实。
  async function loadTaskPeek(taskId, rel) {
    // 先只改本地状态再画一次：让人立刻看到「正在读」，而不是点了没反应。
    taskPeek = { path: rel, data: null };
    renderTasks();
    let data = null;
    try {
      data = await apiGet('/tasks/' + encodeURIComponent(taskId) + '/files/read?path='
        + encodeURIComponent(rel));
    } catch (error) {
      data = { error: '读取失败：' + error.message };
    }
    // 读回来时人可能已经切走或换了一个文件：不是当前要看的就别盖上去。
    if (taskPeek.path !== rel) return;
    taskPeek.data = data;
    if (tasksOpen === taskId && taskDetail.open === 'files') renderTasks();
  }

  function taskBlockPeek() {
    const box = el('div', 'task-block');
    const data = taskPeek.data;
    if (!data) {
      box.append(el('p', 'task-block-note', '读取中…'));
      return box;
    }
    if (data.error) {
      box.append(el('p', 'task-block-note', data.error));
      return box;
    }
    box.append(el('p', 'task-block-note', (data.path || '')
      + ' · ' + formatBytes(data.size || 0)
      + (data.truncated ? ' · 只显示前 ' + formatBytes(data.limit || 0) + '，其余要点下载' : '')));
    if (data.binary) {
      box.append(el('p', 'task-block-note', '二进制文件，没法当文本看。点「下载」取原件。'));
      return box;
    }
    box.append(el('pre', 'traffic-pre logs-pre', data.text || '（空文件）'));
    return box;
  }

  function taskBlockFiles(taskId, data) {
    const box = el('div', 'task-block');
    if (data.error) {
      box.append(el('p', 'task-block-note', data.error));
      return box;
    }
    const totals = data.totals || {};
    const limits = data.limits || {};
    box.append(el('p', 'task-block-note',
      '文件 ' + formatCount(totals.files || 0)
      + ' · 目录 ' + formatCount(totals.dirs || 0)
      + ' · ' + formatBytes(totals.bytes || 0)
      + '。只列它名下 ' + formatCount(data.sessionsTotal || 0) + ' 条对话的工作区'
      + '——在别处写下的文件不在这里。'));
    // 整批带走走归档包：逐个点下载在「三十个产物」这种量级上就是三十次点击，
    // 而它们本来就是同一次跑出来的东西。单文件的按钮仍在每一行上，两种粒度都留着。
    if ((data.totals || {}).files) {
      const batch = el('div', 'task-acts');
      const zip = el('button', 'btn xs', '打包下载（zip）');
      zip.type = 'button';
      zip.dataset.act = 'file-archive';
      zip.dataset.id = taskId;
      batch.append(zip);
      box.append(batch);
    }
    if (data.truncated) {
      box.append(el('p', 'task-block-note', '目录太大，只列了前 '
        + formatCount(limits.files || 0) + ' 项。'));
    }
    if (data.sessionsTruncated) {
      box.append(el('p', 'task-block-note', '对话太多，只列了前 '
        + formatCount(data.sessionCount || 0) + ' 条的工作区。'));
    }
    const sessions = data.sessions || [];
    if (!sessions.length) {
      box.append(el('p', 'task-block-note',
        '这条任务名下还没有对话的工作区。跑起一轮之后，模型写下的东西会出现在这里。'));
      return box;
    }
    sessions.forEach((item) => {
      box.append(taskBlockHead((item.title || item.sessionId)
        + (item.exists ? ' · ' + formatCount(item.count || 0) + ' 项' : ' · 还没有工作区')));
      if (!item.exists) return;
      if (!(item.entries || []).length) {
        box.append(el('p', 'task-block-note', '这个目录是空的。'));
        return;
      }
      const list = el('div', 'dash-list');
      item.entries.forEach((entry) => {
        const isDir = entry.kind === 'dir';
        const row = el('div', 'dash-row');
        row.append(el('span', 'dash-row-main',
          (isDir ? '目录 · ' : '') + (entry.name || entry.path)));
        row.append(el('span', 'dash-row-meta',
          (isDir ? '—' : formatBytes(entry.size || 0))
          + (entry.modified ? ' · ' + formatTime(entry.modified) : '')));
        if (!isDir) {
          const acts = el('div', 'task-acts');
          const peek = el('button', 'btn xs',
            taskPeek.path === entry.path ? '收起' : '查看');
          peek.type = 'button';
          peek.dataset.act = 'file-peek';
          peek.dataset.id = taskId;
          peek.dataset.path = entry.path || '';
          acts.append(peek);
          const get = el('button', 'btn xs', '下载');
          get.type = 'button';
          get.dataset.act = 'file-download';
          get.dataset.id = taskId;
          get.dataset.path = entry.path || '';
          acts.append(get);
          row.append(acts);
        }
        list.append(row);
        // 预览铺在它自己那一行下面，不弹窗：读完接着往下看的还是同一份清单，
        // 弹窗会让人多按一次关闭，而关闭之后位置还得重新找。
        if (!isDir && taskPeek.path === entry.path) list.append(taskBlockPeek());
      });
      box.append(list);
    });
    return box;
  }

  // 会话状态：一条任务名下几条对话**此刻**在不在跑。与文件区同一批会话，但回答的
  // 是另一个问题——文件回答「留下过什么」，这一块回答「现在还在动吗」。
  //
  // 这里的读数来自请求那一刻，不是推送。所以自动刷新只在**这一块展开着**的时候
  // 才起，收起就停：常态挂着一个每两秒的请求，等于让一个没人在看的块长期占着
  // 一条连接，而它的内容一秒前与一秒后通常一模一样。
  function taskBlockChat(taskId, data) {
    const box = el('div', 'task-block');
    if (data.error) {
      box.append(el('p', 'task-block-note', data.error));
      return box;
    }
    box.append(el('p', 'task-block-note',
      (data.active ? '正在跑 ' + formatCount(data.active) + ' 条。' : '没有对话在跑。')
      + ' 共 ' + formatCount(data.count || 0) + ' 条'
      + (data.scope === 'own' ? '（只看得到自己的，其他人的不在其中）' : '')
      + ' · 每 ' + formatCount(data.heartbeatSeconds || 0) + ' 秒取一次'));
    const sessions = data.sessions || [];
    if (!sessions.length) {
      box.append(el('p', 'task-block-note', '这条任务名下还没有对话。'));
      return box;
    }
    const list = el('div', 'dash-list');
    sessions.forEach((item) => {
      const row = el('div', 'dash-row');
      row.append(el('span', 'dash-row-main',
        (item.running ? '在跑 · ' : '') + (item.title || item.sessionId)));
      const bits = [];
      if (item.running) {
        bits.push(item.cancelling ? '正在取消' : '进行中');
        if (item.elapsedMs) bits.push('已跑 ' + formatDuration(Math.round(item.elapsedMs / 1000)));
        if (item.inbox) bits.push('排队 ' + formatCount(item.inbox));
      } else {
        bits.push('空闲');
      }
      bits.push('消息 ' + formatCount(item.messageCount || 0));
      if (item.updatedAt) bits.push(formatTime(item.updatedAt));
      row.append(el('span', 'dash-row-meta', bits.join(' · ')));
      // 只在**真的在跑**时给停止：空闲的对话上没有可停的东西，常驻一颗灰按钮
      // 只会让人以为界面在装样子。
      if (item.running) {
        const acts = el('span', 'task-acts');
        const stop = el('button', 'btn xs', item.cancelling ? '正在停…' : '停止');
        stop.dataset.act = 'chat-stop';
        stop.dataset.id = taskId;
        stop.dataset.session = item.sessionId;
        if (item.cancelling) stop.disabled = true;
        acts.append(stop);
        row.append(acts);
      }
      list.append(row);
    });
    box.append(list);
    return box;
  }

  // 会话状态那块的自刷新。**只在它展开着的时候起**，收起、切到别的块、关掉整条
  // 详情，三条路都要停：常态挂着一个两秒的请求，等于让一个没人在看的块长期占着
  // 一条连接，而它一秒前与一秒后通常一模一样。
  let taskChatTimer = null;

  function stopTaskChatTimer() {
    if (!taskChatTimer) return;
    clearInterval(taskChatTimer);
    taskChatTimer = null;
  }

  function startTaskChatTimer(taskId) {
    stopTaskChatTimer();
    const chat = taskDetail.data && taskDetail.data.chat;
    const beat = Math.max(2, Number((chat && chat.heartbeatSeconds) || 2) * 2);
    taskChatTimer = setInterval(() => { refreshTaskChat(taskId); }, beat * 1000);
  }

  async function refreshTaskChat(taskId) {
    // 已经被切走就安静收工：定时器回调比人多活一拍是常态，那一拍里不许它再改
    // 一个已经不在屏幕上的读数。
    if (tasksOpen !== taskId || taskDetail.open !== 'chat') { stopTaskChatTimer(); return; }
    try {
      taskDetail.data.chat = await apiGet('/tasks/' + encodeURIComponent(taskId) + '/chat/status');
    } catch (error) {
      taskDetail.data.chat = { error: '读取失败：' + error.message };
      stopTaskChatTimer();
    }
    if (tasksOpen === taskId && taskDetail.open === 'chat') renderTasks();
  }

  // 取回一个文件的原字节。走 <a download> 而不是先 fetch 成 Blob：这一条要的是
  // **文件本身**，把它读进内存再拼一次等于让浏览器多搬一遍，而大文件会直接卡住
  // 页面。接口回的 Content-Type 由浏览器自己认。
  function downloadTaskFile(path, name) {
    const link = el('a');
    link.href = API_BASE + path;
    link.download = name;
    document.body.append(link);
    link.click();
    link.remove();
  }

  function taskBlockIntercept(taskId, data) {
    const box = el('div', 'task-block');
    if (data.error) {
      box.append(el('p', 'task-block-note', data.error));
      return box;
    }
    const counts = data.counts || {};
    box.append(el('p', 'task-block-note',
      (data.enabled === false ? '拦截总开关是关的，规则还在但不再拦。' : '')
      + '生效 ' + formatCount(counts.enabled || 0)
      + ' / 共 ' + formatCount(counts.total || 0)
      + ' · 本任务自有 ' + formatCount(counts.task || 0)
      + ' · 内置 ' + formatCount(counts.builtin || 0)));
    const rules = data.rules || [];
    if (!rules.length) {
      box.append(el('p', 'task-block-note',
        '这一组里没有规则。要么全局也被清空了，要么内置的那几条还没复制到本任务名下。'));
    } else {
      const list = el('div', 'dash-list');
      rules.forEach((rule) => {
        const row = el('div', 'dash-row');
        row.append(el('span', 'dash-row-main',
          (rule.enabled ? '' : '（已停用）') + (rule.name || rule.id || '规则')));
        row.append(el('span', 'dash-row-meta',
          (rule.action || '') + ' · ' + (rule.pattern || '')
          + ' · ' + (rule.scope === 'task' ? '本任务' : '全局')));
        // 开关 / 改 / 删与后端同一档：这一组按 data.admin 决定画不画。
        // 只画的按钮点了回 403，比不画更糟——人第一反应是界面坏了。
        if (data.admin) {
          const ruleActs = el('span', 'task-acts');
          const flip = el('button', 'btn xs', rule.enabled ? '停用' : '启用');
          flip.type = 'button';
          flip.dataset.act = 'rule-toggle';
          flip.dataset.id = taskId;
          flip.dataset.rule = rule.id;
          ruleActs.append(flip);
          const edit = el('button', 'btn xs',
            taskRuleEdit.id === rule.id ? '收起' : '改');
          edit.type = 'button';
          edit.dataset.act = 'rule-edit';
          edit.dataset.id = taskId;
          edit.dataset.rule = rule.id;
          ruleActs.append(edit);
          const drop = el('button', 'btn xs', '删');
          drop.type = 'button';
          drop.dataset.act = 'rule-del';
          drop.dataset.id = taskId;
          drop.dataset.rule = rule.id;
          ruleActs.append(drop);
          row.append(ruleActs);
        }
        list.append(row);
        if (taskRuleEdit.id === rule.id) list.append(taskRuleEditForm(taskId, rule));
      });
      box.append(list);
    }
    if (!data.admin) {
      box.append(el('p', 'task-block-note', '改规则要管理员，这里只给看。'));
      return box;
    }
    const seed = el('button', 'btn sm', '把内置规则复制到本任务');
    seed.type = 'button';
    seed.dataset.act = 'intercept-seed';
    seed.dataset.id = taskId;
    const acts = el('div', 'task-form-actions');
    acts.append(seed);
    box.append(acts);
    return box;
  }


  // 改一条拦截规则：同样**原地展开**在那一行下面。
  //
  // 只放最常改的四项：名称、匹配内容、匹配方式、优先级。超时那一组（timeoutEnabled /
  // timeoutSeconds / timeoutAction）不进这张表——它是"拦不住的时候怎么办"，
  // 改它的场合比改匹配内容少得多，塞进来只会让这张表长出第二屏。
  // 从块缓存里捞一条规则：按钮上只有 id，改口径要拿它现在的那一份（尤其"当前是停了
  // 还是开着"），按界面文案反推迟早会与后端对不上。
  function ruleRow(taskId, ruleId) {
    const payload = (taskDetail.data || {}).intercept;
    const rows = (payload && payload.rules) || [];
    const hit = rows.filter((rule) => rule.id === ruleId)[0];
    if (hit) return hit;
    // 缓存已经过期（别处删过）：按"没捞到"处理，让请求自己回一句真话。
    return null;
  }

  function taskRuleEditForm(taskId, rule) {
    const form = el('div', 'task-form');
    form.dataset.taskRuleEdit = rule.id;
    form.dataset.taskRule = rule.id;
    const pattern = el('input', 'input');
    pattern.type = 'text';
    pattern.value = rule.pattern || '';
    pattern.placeholder = '要拦的正则或字面串';
    pattern.dataset.field = 'pattern';
    form.append(taskField('匹配内容', pattern));
    const matchType = el('select', 'select xs-select');
    [['regex', '正则'], ['literal', '字面串']].forEach((pair) => {
      const option = el('option', '', pair[1]);
      option.value = pair[0];
      matchType.append(option);
    });
    matchType.dataset.field = 'matchType';
    matchType.value = rule.matchType || 'regex';
    form.append(taskField('匹配方式', matchType));
    const priority = el('input', 'input');
    priority.type = 'text';
    priority.value = String(rule.priority == null ? '' : rule.priority);
    priority.placeholder = '1-100，小的先判';
    priority.dataset.field = 'priority';
    form.append(taskField('优先级', priority));
    const name = el('input', 'input');
    name.type = 'text';
    name.value = rule.name || '';
    name.dataset.field = 'name';
    form.append(taskField('名称', name));
    const message = el('input', 'input');
    message.type = 'text';
    message.value = rule.message || '';
    message.placeholder = '可选：命中时告诉模型的话';
    message.dataset.field = 'message';
    form.append(taskField('提示语', message));
    const save = el('button', 'btn sm', '存下这一条');
    save.type = 'button';
    save.dataset.act = 'rule-save';
    save.dataset.id = taskId;
    save.dataset.rule = rule.id;
    const acts = el('div', 'task-form-actions');
    acts.append(save);
    form.append(acts);
    return form;
  }

  // 一条意图下挂的事实：它在界面上就是「这条线开口说过的话」。
  //
  // 这些记录本来就存在（`GET /tasks/<id>/intents/<iid>/messages` 一直有），
  // 但界面从没展示过——于是「这条意图跑到哪儿了」只能靠链路图去猜，而图上
  // 每条事实只是一张卡，看不出先后。
  //
  // 排序用后端给的顺序（按节点 id 后缀，不是 createdAt）：同一秒写入的几行在
  // 时间戳上完全并列，按时间排会变成随机顺序——那比不排更让人误解。
  function taskBlockIntentPeek() {
    const box = el('div', 'task-block');
    const data = taskIntentPeek.data;
    if (!data) {
      box.append(el('p', 'task-block-note', '读取中…'));
      return box;
    }
    if (data.error) {
      box.append(el('p', 'task-block-note', data.error));
      return box;
    }
    const labels = data.kindLabels || {};
    const messages = data.messages || [];
    if (!messages.length) {
      box.append(el('p', 'task-block-note',
        '这条意图名下还没有记录。它可能刚派出去，也可能什么都没跑出来。'));
      return box;
    }
    const list = el('div', 'dash-list');
    messages.forEach((item) => {
      const row = el('div', 'dash-row');
      // 通道为 hint 的是**人写下的提示**，不是探测产物：混在一串「22 开」里，
      // 它读起来像一条观测结果，而它实际是一条指令。
      const flag = item.channel === 'hint' ? '提示 · ' : '';
      row.append(el('span', 'dash-row-main',
        flag + (labels[item.factKind] || item.factKindLabel || item.factKind || '记录')
        + ' · ' + (item.detail || '（空）')));
      row.append(el('span', 'dash-row-meta',
        (item.target ? item.target + ' · ' : '') + formatTime(item.createdAt || 0)));
      list.append(row);
    });
    box.append(list);
    box.append(el('p', 'task-block-note',
      '共 ' + formatCount(data.total || messages.length) + ' 条，按写下顺序排。'));
    return box;
  }

  async function loadIntentPeek(taskId, intentId) {
    taskIntentPeek = { id: intentId, data: null };
    renderTasks();
    let data;
    try {
      data = await apiGet('/tasks/' + encodeURIComponent(taskId)
        + '/intents/' + encodeURIComponent(intentId) + '/messages');
    } catch (error) {
      data = { error: '读取失败：' + error.message };
    }
    // 人可能已经切走或换了另一条：这时候把读数铺上去会盖掉他正在看的那一份。
    if (taskIntentPeek.id !== intentId) return;
    taskIntentPeek = { id: intentId, data: data };
    if (tasksOpen === taskId && taskDetail.open === 'intents') renderTasks();
  }
  function taskBlockLlm(taskId, data) {
    const box = el('div', 'task-block');
    if (data.error) {
      box.append(el('p', 'task-block-note', data.error));
      return box;
    }
    const profile = data.profile || null;
    const source = TASK_PROFILE_SOURCE[data.source] || data.source || '';
    box.append(el('p', 'task-block-note',
      '最终生效：' + source + (profile && profile.name ? '（' + profile.name + '）' : '')
      + (data.missing ? ' · 本任务指定的那一份已经不在了' : '')));
    if (profile) {
      const list = el('div', 'dash-list');
      [['提供方', profile.provider || ''],
        ['接口地址', profile.baseUrl || profile.base_url || ''],
        ['模型', profile.model || '']].forEach((pair) => {
        const row = el('div', 'dash-row');
        row.append(el('span', 'dash-row-main', pair[0]));
        row.append(el('span', 'dash-row-meta', pair[1] || '—'));
        list.append(row);
      });
      box.append(list);
    }
    const pick = el('select', 'select xs-select');
    const follow = el('option', '', '跟随全局（不单独指定）');
    follow.value = '';
    pick.append(follow);
    catalogProfiles().forEach((item) => {
      const option = el('option', '', (item.name || item.id) + ' · ' + (item.model || ''));
      option.value = item.id;
      pick.append(option);
    });
    pick.dataset.field = 'llmProfileId';
    pick.value = data.source === 'task' ? (data.profileId || '') : '';
    const form = el('div', 'task-form');
    form.dataset.taskLlm = taskId;
    form.append(taskField('这条任务用哪个模型', pick));
    const submit = el('button', 'btn sm', '保存');
    submit.type = 'button';
    submit.dataset.act = 'llm-save';
    submit.dataset.id = taskId;
    const acts = el('div', 'task-form-actions');
    acts.append(submit);
    form.append(acts);
    box.append(form);
    return box;
  }

  function taskBlockIntents(taskId, data) {
    const box = el('div', 'task-block');
    if (data.error) {
      box.append(el('p', 'task-block-note', data.error));
      return box;
    }
    const counts = data.counts || {};
    box.append(el('p', 'task-block-note',
      '目标 ' + formatCount(counts.goals || 0)
      + '（达成 ' + formatCount(counts.goalsMet || 0) + '）'
      + ' · 意图 ' + formatCount(counts.intents || 0)
      + ' · 进行中 ' + formatCount(counts.intentsOpen || 0)
      + ' · 阻塞 ' + formatCount(counts.intentsBlocked || 0)));
    // 目标先铺：意图是「照着目标派出去的一条线」，先看见目标才知道这些线在奔什么。
    // 目标可以加、可以改正文（改的是「要拿到什么」，不是状态），只有 goal-1 不能删
    // ——它是建档目标，被 engine 的 GOAL_ID 硬引用（后端 delete_node 会拒）。
    const goals = data.goals || [];
    if (!goals.length) {
      box.append(el('p', 'task-block-note', '这条任务连一条目标都没有——报告第一行会是空的。'));
    } else {
      const goalList = el('div', 'dash-list');
      goals.forEach((goal) => {
        const row = el('div', 'dash-row');
        row.append(el('span', 'dash-row-main',
          (goal.state === 'met' ? '（已达成）' : '') + (goal.text || goal.id)));
        row.append(el('span', 'dash-row-meta',
          (goal.stateLabel || goal.state || '') + (goal.objective ? ' · ' + goal.objective : '')));
        // 改与删照 admin 一位决定画不画：画了点了回 403，人第一反应是界面坏了。
        if (data.admin) {
          const goalActs = el('span', 'task-acts');
          const edit = el('button', 'btn xs', taskGoalEdit.id === goal.id ? '收起' : '改');
          edit.type = 'button';
          edit.dataset.act = 'goal-edit';
          edit.dataset.id = taskId;
          edit.dataset.goal = goal.id;
          goalActs.append(edit);
          if (goal.id !== 'goal-1') {
            const drop = el('button', 'btn xs', '删');
            drop.type = 'button';
            drop.dataset.act = 'goal-del';
            drop.dataset.id = taskId;
            drop.dataset.goal = goal.id;
            goalActs.append(drop);
          }
          row.append(goalActs);
        }
        goalList.append(row);
        if (taskGoalEdit.id === goal.id) goalList.append(taskGoalEditForm(taskId, goal));
      });
      box.append(goalList);
    }
    if (data.admin) {
      const addForm = el('div', 'task-form');
      addForm.dataset.taskGoalAdd = taskId;
      const addInput = el('input', 'input');
      addInput.type = 'text';
      addInput.placeholder = '加一条可验证的目标，例如「拿到后台管理员权限」';
      addInput.dataset.field = 'text';
      addForm.append(taskField('新目标', addInput));
      const addButton = el('button', 'btn sm', '加一条目标');
      addButton.type = 'button';
      addButton.dataset.act = 'goal-add';
      addButton.dataset.id = taskId;
      const addActs = el('div', 'task-form-actions');
      addActs.append(addButton);
      addForm.append(addActs);
      box.append(addForm);
    }
    const intents = data.intents || [];
    if (!intents.length) {
      box.append(el('p', 'task-block-note', '这条任务还没有拆出意图。'));
      return box;
    }
    intents.forEach((intent) => {
      const row = el('div', 'task-intent-row');
      row.append(el('span', 'dash-row-main', intent.title || intent.id));
      // 有执行工在跑时，状态仍是「进行中」——但「待领」与「在跑」对使用者的含义不同，
      // 一个说"平台随时会动手"，一个说"已经在动手，改它之前先想清楚"（口径同编排面板）。
      row.append(el('span', 'dash-row-meta', intent.stateLabel || intent.state || ''));
      if (intent.owner) row.append(el('span', 'orch-badge running', '在跑'));
      // 这一行的动作装在一个容器里：「看记录」人人都有，另外三个要管理员。
      // 不分成两个容器是因为这一行是 `1fr auto` 的两列网格——多一个容器就多
      // 占一格，四个动作会散成三行。
      //
      // 「看记录」不吃管理员：它读的是这条意图名下的事实，与对话记录同一种东西。
      // 把它也算进管理员那一档，非管理员就永远不知道这条线跑出了什么，只能去
      // 链路图里数卡片。
      const acts = el('div', 'task-acts');
      const peek = el('button', 'btn xs',
        taskIntentPeek.id === intent.id ? '收起记录' : '看记录');
      peek.type = 'button';
      peek.dataset.act = 'intent-peek';
      peek.dataset.id = taskId;
      peek.dataset.intent = intent.id;
      acts.append(peek);
      // 这三个动作要管理员（后端同一档）。非管理员看不到入口，而不是点了回 403。
      if (data.admin) {
        [['pause', '暂停'], ['rerun', '重跑'], ['done', '结束']].forEach((pair) => {
          const button = el('button', 'btn xs', pair[1]);
          button.type = 'button';
          button.dataset.act = 'intent-move';
          button.dataset.id = taskId;
          button.dataset.intent = intent.id;
          button.dataset.move = pair[0];
          acts.append(button);
        });
        row.append(acts);
      }
      box.append(row);
      if (taskIntentPeek.id === intent.id) box.append(taskBlockIntentPeek());
    });
    if (data.admin) {
      const rerun = el('button', 'btn sm', '把阻塞的意图全部放回进行中');
      rerun.type = 'button';
      rerun.dataset.act = 'intent-rerun-blocked';
      rerun.dataset.id = taskId;
      const footer = el('div', 'task-form-actions');
      footer.append(rerun);
      box.append(footer);
    } else {
      box.append(el('p', 'task-block-note',
        '改目标与动意图要管理员，这里只给看。'));
    }
    return box;
  }

  // 改一条目标：正文那一句是报告第一行，其余两项是给人看的说明。
  function taskGoalEditForm(taskId, goal) {
    const form = el('div', 'task-form');
    form.dataset.taskGoalEdit = goal.id;
    [['text', '目标', goal.text || ''],
      ['objective', '说明', goal.objective || ''],
      ['authorization', '授权依据', goal.authorization || '']].forEach((spec) => {
      const input = el('input', 'input');
      input.type = 'text';
      input.value = spec[2];
      input.placeholder = spec[1];
      input.dataset.field = spec[0];
      form.append(taskField(spec[1], input));
    });
    const save = el('button', 'btn sm', '存下这一条');
    save.type = 'button';
    save.dataset.act = 'goal-save';
    save.dataset.id = taskId;
    save.dataset.goal = goal.id;
    const acts = el('div', 'task-form-actions');
    acts.append(save);
    form.append(acts);
    return form;
  }

  // 复测块：这条任务名下的漏洞，以及每一次复核的结论。
  //
  // 它读的是任务的漏洞表（/tasks/<id>/findings），不是 /engagements/<eid>/findings：
  // 任务页手上只有任务号，而任务与目标是多对一——拿目标号去查会顺带把同目标的
  // 其他任务的漏洞一起列出来。
  //
  // 结论三种（仍可复现 / 已修复 / 无法确认）全部来自后端，这里不另写一份词表：
  // retestVerdicts 与 retestLabels 每次跟着列表一起下来。后端多出一种结论时，
  // 这个下拉自动多一项，不需要改这里。
  function taskRetestForm(taskId, finding) {
    const form = el('div', 'task-form');
    form.dataset.taskRetest = finding.id;
    const verdict = el('select', 'select xs-select');
    const verdicts = finding._verdicts || [];
    const labels = finding._labels || {};
    verdicts.forEach((value) => {
      const option = el('option', '', labels[value] || value);
      option.value = value;
      verdict.append(option);
    });
    // 默认落在「仍可复现」以外的**第一个**上：复核的常见起点是"它还开着"，但那是
    // 一条最不需要动脑的结论——默认值落在它上面，人会顺手点保存而不真的去看。
    verdict.value = verdicts.filter((value) => value !== 'reproduced')[0] || verdicts[0] || '';
    verdict.dataset.field = 'verdict';
    form.append(taskField('结论', verdict));
    // 两句正文都是必填：只记一句"已修复"而不写看到了什么，下一次复核的人无法
    // 判断上次是真的验过还是照着修复说明抄了一句。
    [['summary', '这次看到什么', '压一个单引号还能报错，返回了 500'],
      ['evidence', '依据', '请求与响应片段，或者命令输出']].forEach((spec) => {
      const input = el('input', 'input');
      input.type = 'text';
      input.placeholder = spec[2];
      input.dataset.field = spec[0];
      form.append(taskField(spec[1], input));
    });
    const save = el('button', 'btn sm', '记下这次复核');
    save.type = 'button';
    save.dataset.act = 'retest-save';
    save.dataset.id = taskId;
    save.dataset.finding = finding.id;
    const acts = el('div', 'task-form-actions');
    acts.append(save);
    form.append(acts);
    return form;
  }

  function taskBlockFindings(taskId, data) {
    const box = el('div', 'task-block');
    if (data.error) {
      box.append(el('p', 'task-block-note', data.error));
      return box;
    }
    const counts = data.counts || {};
    box.append(el('p', 'task-block-note',
      '漏洞 ' + formatCount(counts.total || 0)
      + ' · 已证实 ' + formatCount(counts.verified || 0)
      + ' · 复测过 ' + formatCount(counts.retested || 0)));
    const items = data.findings || [];
    if (!items.length) {
      box.append(el('p', 'task-block-note',
        '这条任务名下还没有漏洞记录。复测的对象是漏洞——先让上一轮把一条证下来。'));
      return box;
    }
    const labels = data.retestLabels || {};
    const verdicts = data.retestVerdicts || [];
    const list = el('div', 'dash-list');
    items.forEach((item) => {
      const row = el('div', 'dash-row');
      row.append(el('span', 'dash-row-main', item.title || '（无标题）'));
      const rows = item.retests || [];
      const last = rows.length ? rows[rows.length - 1] : null;
      // severityView.label 是**一张表**（SEVERITY_LABEL），不是函数：共享入口导出的是
      // 词表本身，取值照表下标，不做一次调用。
      const bits = [severityView.label[item.severity || 'info'] || '信息'];
      // 状态位只有「已确认 / 已修复」两种：它是复测**改**出来的那一位，
      // 而"上一次的结论"是另一件事——两者会不一致（修好之后又被复现，
      // 状态仍是 fixed，最后一条结论是 reproduced）。所以分开说。
      bits.push(item.state === 'fixed' ? '已修复' : '还开着');
      if (last) bits.push('上次：' + (labels[last.verdict] || last.verdict || ''));
      bits.push('复测 ' + formatCount(rows.length) + ' 次');
      row.append(el('span', 'dash-row-meta', bits.join(' · ')));
      if (data.admin) {
        const acts = el('span', 'task-acts');
        const open = el('button', 'btn xs',
          taskRetestEdit.id === item.id ? '收起' : '复测');
        open.type = 'button';
        open.dataset.act = 'retest-open';
        open.dataset.id = taskId;
        open.dataset.finding = item.id;
        acts.append(open);
        row.append(acts);
      }
      list.append(row);
      // 历次结论铺在那一行下面：这是这一块存在的理由——「修了又复发」只能从
      // 时间顺序里看出来，只留最后一次等于把最该看的那条信息删掉。
      if (rows.length) {
        const history = el('div', 'dash-list');
        rows.forEach((record) => {
          const line = el('div', 'dash-row');
          line.append(el('span', 'dash-row-main',
            (labels[record.verdict] || record.verdict || '') + ' · '
            + (record.summary || '')));
          line.append(el('span', 'dash-row-meta',
            (record.evidence || '') + ' · ' + (record.at || '')));
          history.append(line);
        });
        list.append(history);
      }
      if (data.admin && taskRetestEdit.id === item.id) {
        const filled = Object.assign({}, item, {
          _verdicts: verdicts, _labels: labels,
        });
        list.append(taskRetestForm(taskId, filled));
      }
    });
    box.append(list);
    // 这一句与那几颗按钮反过来：非管理员看到的是"为什么没有入口"，管理员看到的是
    // "为什么不能改上次那条"。两句话都不是废话，所以两边都写，只是内容不同。
    box.append(el('p', 'task-block-note', data.admin
      ? '复测只追加、不覆盖——历史上每一次都留着，写错了就再记一条。'
      : '记一次复核要管理员，与后端同一档；这里只给看历次结论。'));
    return box;
  }

  function taskBlockBody(taskId, key) {
    const data = taskDetail.data[key];
    if (!data) return el('div', 'muted', '正在读取…');
    if (key === 'intel') return taskBlockIntel(taskId, data);
    if (key === 'coverage') return taskBlockCoverage(taskId, data);
    if (key === 'findings') return taskBlockFindings(taskId, data);
    if (key === 'files') return taskBlockFiles(taskId, data);
    if (key === 'chat') return taskBlockChat(taskId, data);
    if (key === 'intercept') return taskBlockIntercept(taskId, data);
    if (key === 'llm') return taskBlockLlm(taskId, data);
    if (key === 'scope') return taskBlockScope(taskId, data);
    return taskBlockIntents(taskId, data);
  }

  function taskDetailBody(taskId) {
    const wrap = el('div', 'task-detail');
    wrap.dataset.taskDetail = taskId;
    const detail = taskDetail.taskId === taskId ? taskDetail.payload : null;
    if (!detail) {
      wrap.append(el('div', 'muted', '正在读取详情…'));
      return wrap;
    }
    const counts = detail.counts || {};
    const chain = el('div', 'task-detail-chain');
    ['goal', 'intent', 'fact', 'finding', 'digest', 'asset'].forEach((key) => {
      chain.append(el('span', 'task-detail-chip',
        key + ' ' + formatCount(counts[key] || 0)));
    });
    wrap.append(chain);
    wrap.append(taskDetailFacts(detail));
    wrap.append(taskCategoryRow(taskId, detail));
    // 五块的入口。收起时不显示内容，但按钮一直在：详情页的第一行要能回答
    // 「这条任务身上还有哪几件事可看」，而不是让人自己猜哪个词能点。
    const bar = el('div', 'task-acts');
    TASK_BLOCKS.forEach((block) => {
      const open = taskDetail.open === block.key;
      const button = el('button', 'btn xs', block.label + (open ? '（收起）' : ''));
      button.type = 'button';
      button.title = block.hint;
      button.dataset.act = 'block';
      button.dataset.id = taskId;
      button.dataset.block = block.key;
      bar.append(button);
    });
    wrap.append(bar);
    if (taskDetail.open) {
      const found = TASK_BLOCKS.filter((item) => item.key === taskDetail.open)[0];
      wrap.append(taskBlockHead((found && found.hint) || ''));
      wrap.append(taskBlockBody(taskId, taskDetail.open));
    }
    const jobs = detail.recentJobs || [];
    if (!jobs.length) {
      wrap.append(emptyBlock('这个目标还没有跑过作业'));
      return wrap;
    }
    const list = el('div', 'dash-list');
    jobs.forEach((job) => {
      const row = el('div', 'dash-row');
      row.append(el('span', 'dash-row-main',
        jobView.kindLabel(job.kind || job.kindLabel || '') + ' · ' + (job.target || '')));
      row.append(el('span', 'dash-row-meta', jobView.label(job.status)
        + ' · ' + formatTime(job.createdAt || 0)));
      list.append(row);
    });
    wrap.append(list);
    return wrap;
  }

  function taskRow(task) {
    const box = el('article', 'task-row');
    const head = el('div', 'task-head');
    if (task.pinned) head.append(el('span', 'task-pin', '置顶'));
    const name = el('span', 'task-name', task.name || task.target || '（未命名）');
    name.title = name.textContent;
    head.append(name);
    const cat = categoryName(task);
    if (cat) head.append(el('span', 'task-cat', cat));
    head.append(el('span', 'task-state state-' + (task.status || 'idle'), taskState(task)));
    if (task.archived) head.append(el('span', 'task-state state-archived', '已归档'));
    head.append(el('span', 'task-meta', taskCounts(task)));
    box.append(head);
    const sub = [];
    if (task.goal) sub.push('目标：' + task.goal);
    else if (task.objective) sub.push('目标：' + task.objective);
    if (task.remark) sub.push('备注：' + task.remark);
    if (task.target) sub.push('入口：' + task.target);
    if (sub.length) box.append(el('p', 'task-sub', sub.join(' · ')));
    const acts = el('div', 'task-acts');
    acts.append(taskButton('详情', 'detail', task));
    if (!task.archived) {
      acts.append(taskButton(task.paused ? '恢复' : '暂停', task.paused ? 'resume' : 'pause', task));
      acts.append(taskButton(task.pinned ? '取消置顶' : '置顶', 'pin', task));
    }
    acts.append(taskButton(task.archived ? '取消归档' : '归档', task.archived ? 'unarchive' : 'archive', task));
    box.append(acts);
    if (tasksOpen === task.id) box.append(taskDetailBody(task.id));
    return box;
  }

  // 行内按钮只带 data-act 与 data-id，事件绑在容器上（委托）：一份页面里
  // 可能有一百行，逐行 addEventListener 等于给自己攒一百个闭包。
  function taskButton(label, act, task) {
    const button = el('button', 'btn xs', label);
    button.type = 'button';
    button.dataset.act = act;
    button.dataset.id = task.id;
    return button;
  }

  function taskFilters(host) {
    const bar = el('div', 'task-filters');
    const select = el('select', 'select xs-select');
    const all = el('option', '', '全部状态');
    all.value = '';
    select.append(all);
    TASK_STATES.forEach((key) => {
      const option = el('option', '', TASK_STATE_TEXT[key]);
      option.value = key;
      if (tasksFilter.status === key) option.selected = true;
      select.append(option);
    });
    select.addEventListener('change', () => {
      tasksFilter.status = select.value;
      renderTasks();
    });
    bar.append(select);
    // 分类是**前端**筛的：后端 list_tasks 已经有状态/关键字两条筛选，而它的 counts
    // 按全部行算。再加一条后端筛选会让「计数为什么和列表条数不一样」多一个例外；
    // 一页最多 200 条，本地筛一遍更容易解释。
    const cats = el('select', 'select xs-select');
    const anyCat = el('option', '', '全部分类');
    anyCat.value = '';
    cats.append(anyCat);
    catalogCategories().forEach((item) => {
      const option = el('option', '', (item.name || item.id)
        + '（' + formatCount((item.counts && item.counts.all) || 0) + '）');
      option.value = item.id;
      if (tasksFilter.categoryId === item.id) option.selected = true;
      cats.append(option);
    });
    const uncat = el('option', '', '未分类');
    uncat.value = '__none__';
    if (tasksFilter.categoryId === '__none__') uncat.selected = true;
    cats.append(uncat);
    cats.addEventListener('change', () => {
      tasksFilter.categoryId = cats.value;
      renderTasks();
    });
    bar.append(cats);
    const archivedLabel = el('label', 'task-check');
    const archived = el('input');
    archived.type = 'checkbox';
    archived.checked = tasksFilter.archived;
    archived.addEventListener('change', () => {
      tasksFilter.archived = archived.checked;
      renderTasks();
    });
    archivedLabel.append(archived);
    archivedLabel.append(document.createTextNode('只看归档'));
    bar.append(archivedLabel);
    const search = el('input', 'input xs-input');
    search.type = 'search';
    search.placeholder = '按名称、目标、备注或编号找';
    search.value = tasksFilter.keyword;
    search.addEventListener('change', () => {
      tasksFilter.keyword = search.value.trim();
      loadTasks();
    });
    bar.append(search);
    host.append(bar);
  }

  // ---------------------------------------------------------------- 新建 / 归档
  // 表单建任务与对话建任务**落到同一个写入器**（`store.create_task` 走的就是
  // `create_engagement` + `update_task`），所以列表里不必标「这条是手建的」。
  // 表单这边多出来的只有「一次给全」：名字、分类、模板、范围条目在开口之前就定下来。
  //
  // 模板里存的是**配置**（目标写法、范围、模型、并行数），复制任务带的是**证据**
  // （作业、漏洞、资产）。两件事混在一起最容易发生，也最难发现：模板里混进上一次
  // 的探测结果之后，每一个照着它建的任务都带着别人家的资产。
  function formNode(host, field) {
    return host ? host.querySelector('[data-field="' + field + '"]') : null;
  }

  function formText(host, field) {
    const node = formNode(host, field);
    return node ? String(node.value || '').trim() : '';
  }

  function formLines(host, field) {
    const raw = formText(host, field);
    if (!raw) return [];
    return raw.split('\n').map((line) => line.trim()).filter((line) => line.length);
  }

  // 范围条目一行一条，「allow / deny + 内容」。**照抄模板里的写法**：模板列表与
  // 任务里那两条规则长得一样，人在这里学会的语法，到模板页仍然是同一句。
  function formScope(host) {
    const out = [];
    formLines(host, 'scope').forEach((line) => {
      const match = line.match(/^(allow|deny)\s+(.+)$/i);
      if (match) out.push({ kind: match[1].toLowerCase(), text: match[2].trim() });
      else out.push({ kind: 'allow', text: line });
    });
    return out;
  }

  function templateOptionText(item) {
    const bits = [];
    if (item.target) bits.push(item.target);
    if ((item.goals || []).length) bits.push('目标 ' + formatCount((item.goals || []).length) + ' 条');
    if ((item.scope || []).length) bits.push('范围 ' + formatCount((item.scope || []).length) + ' 条');
    return bits.length ? (item.name || item.id) + ' · ' + bits.join(' · ') : (item.name || item.id);
  }

  function taskSelect(field, options, current, placeholder) {
    const select = el('select', 'select');
    if (placeholder !== null) {
      const first = el('option', '', placeholder);
      first.value = '';
      select.append(first);
    }
    options.forEach((item) => {
      const option = el('option', '', item.text);
      option.value = item.value;
      if (item.value === current) option.selected = true;
      select.append(option);
    });
    select.dataset.field = field;
    return select;
  }

  // 模板一换就把表单填成模板的样子：**看得见才敢改**。只换下拉不填字段的形态是
  // 「我选了模板但不知道它带了什么」，建出来的任务与模板的差别全靠猜。
  function applyTemplate(form, templateId) {
    const found = catalogTemplates().filter((item) => item.id === templateId)[0];
    if (!found) return;
    const pairs = [
      ['name', found.name], ['target', found.target], ['objective', found.objective],
      ['authorization', found.authorization], ['remark', found.remark],
    ];
    pairs.forEach((pair) => {
      const node = formNode(form, pair[0]);
      if (node && !String(node.value || '').trim()) node.value = pair[1] || '';
    });
    const cat = formNode(form, 'categoryId');
    if (cat && found.categoryId) cat.value = found.categoryId;
    const llm = formNode(form, 'llmProfileId');
    if (llm && found.llmProfileId) llm.value = found.llmProfileId;
    const workers = formNode(form, 'workerCount');
    if (workers && found.workerCount) workers.value = String(found.workerCount);
    const coverage = formNode(form, 'coverageEnabled');
    if (coverage) coverage.checked = !!found.coverageEnabled;
  }

  function taskCreateForm() {
    const box = el('div', 'task-create');
    box.append(el('h2', 'dash-card-title', '新建任务'));
    box.append(el('p', 'task-sub',
      '从模板来省事，从对话来省话。两者落到同一张表：模板给的是配置（目标写法、'
      + '范围、模型、并行数），对话给的是那句「打谁、要什么」——把范围条目写成'
      + '「allow 10.10.10.0/24」一行一条，规则与模板页是同一套写法。'));
    const form = el('div', 'task-form');
    form.dataset.taskForm = 'create';
    const templates = catalogTemplates();
    if (templates.length) {
      const options = templates.map((item) => ({ value: item.id, text: templateOptionText(item) }));
      form.append(taskField('从模板开始（可选）',
        taskSelect('templateId', options, tasksForm.templateId, '不用模板，直接填')));
    } else if (tasksCatalogError) {
      form.append(el('p', 'task-block-note', tasksCatalogError));
    }
    [['name', '名称', '留空就用目标当名称'],
      ['target', '被测目标', '10.10.10.110 或 app.corp.local'],
      ['objective', '目标说明', '比如：拿下后台管理权限'],
      ['authorization', '授权依据', '工单号、合同、授权书编号'],
      ['remark', '备注', '可选']].forEach((spec) => {
      const input = el('input', 'input');
      input.type = 'text';
      input.placeholder = spec[2];
      input.dataset.field = spec[0];
      form.append(taskField(spec[1], input));
    });
    const cats = catalogCategories().map((item) => ({ value: item.id, text: item.name || item.id }));
    form.append(taskField('分类', taskSelect('categoryId', cats, '', '不分类')));
    const profiles = catalogProfiles().map((item) => ({
      value: item.id, text: (item.name || item.id) + ' · ' + (item.model || ''),
    }));
    form.append(taskField('模型（可选）',
      taskSelect('llmProfileId', profiles, '', '跟随全局激活项')));
    const workers = el('input', 'input');
    workers.type = 'number';
    workers.min = '0';
    workers.placeholder = '0 = 按平台默认';
    workers.dataset.field = 'workerCount';
    form.append(taskField('并行数', workers));
    const coverageLabel = el('label', 'task-check');
    const coverage = el('input');
    coverage.type = 'checkbox';
    coverage.dataset.field = 'coverageEnabled';
    coverageLabel.append(coverage);
    coverageLabel.append(document.createTextNode('开覆盖度跟踪'));
    form.append(coverageLabel);
    const goals = el('textarea', 'input');
    goals.rows = 3;
    goals.placeholder = '一行一个：拿到 webshell / 找到一个高危 / 摸清内网出口';
    goals.dataset.field = 'goals';
    form.append(taskField('目标清单（可空）', goals));
    const scope = el('textarea', 'input');
    scope.rows = 3;
    scope.placeholder = '一行一条：allow 10.10.10.0/24 ／ deny 10.10.10.5';
    scope.dataset.field = 'scope';
    form.append(taskField('范围条目（可空）', scope));
    const acts = el('div', 'task-form-actions');
    const submit = el('button', 'btn sm primary', '建这个任务');
    submit.type = 'button';
    submit.dataset.act = 'task-create';
    const cancel = el('button', 'btn sm ghost', '收起');
    cancel.type = 'button';
    cancel.dataset.act = 'task-new-toggle';
    acts.append(submit);
    acts.append(cancel);
    form.append(acts);
    box.append(form);
    return box;
  }

  // 「这一页不建任务」是上一版写在这里的结论。**它错了**：任务与对话的先后没有
  // 硬性关系——照着工单一次开三个目标是这个平台最常见的起手式，而对话开任务要求
  // 人先把那段话打对。两处都建会得到两种任务的担心并不成立：两条路径用的是同一个
  // `create_engagement`，差别只在入口，`createdBy` 记着是谁开的。所以这里是入口
  // 的**补充**，不是替代：对话仍然是默认路径，表单是照着工单落地的那一条。
  function taskNotice() {
    const box = el('div', 'task-create');
    box.append(el('h2', 'dash-card-title', '任务从一句话开始，也可以照着工单填'));
    box.append(el('p', 'task-sub',
      '在会话里说一句「帮我渗透 10.10.10.110 拿下后台」，任务就在那句话里成立：'
      + '名字取你说的第一句，被测目标从话里认出来，之后这条对话派出的作业、写下的'
      + '链路、漏洞与资产都归在它下面。同一台机器的下一次对话会并进同一个任务。'
      + '手上有工单、要一次开好几个目标时，用表单——两条路建出来的任务是同一种。'));
    const acts = el('div', 'task-form-actions');
    const chat = el('button', 'btn sm', '去会话开一个对话');
    chat.type = 'button';
    chat.dataset.act = 'new-chat';
    acts.append(chat);
    const create = el('button', 'btn sm' + (tasksForm.open ? ' ghost' : ''),
      tasksForm.open ? '收起表单' : '照工单新建');
    create.type = 'button';
    create.dataset.act = 'task-new-toggle';
    acts.append(create);
    if (!tasksForm.open) {
      const center = el('button', 'btn sm ghost', '归档中心');
      center.type = 'button';
      center.dataset.act = 'archive-center';
      acts.append(center);
    }
    box.append(acts);
    if (tasksForm.open) box.append(taskCreateForm());
    return box;
  }

  // 归档中心：把收起来的那批**按分类分组**摆出来。归档的用途是从默认视图里收起来，
  // 于是「我记得有一个跑过 xxx 的任务」只能来这儿找——而找的时候人记得的第一个
  // 线索通常是分类（去年那个甲方、上个月那批内网）。
  function taskArchivePanel() {
    const box = el('div', 'task-create');
    box.append(el('h2', 'dash-card-title', '归档中心'));
    if (!tasksArchive) {
      box.append(el('p', 'task-sub', '正在读取归档…'));
      return box;
    }
    if (tasksArchive.error) {
      box.append(el('p', 'task-sub', tasksArchive.error));
      return box;
    }
    const groups = tasksArchive.groups || [];
    if (!groups.length) {
      box.append(el('p', 'task-sub', '归档是空的。归档只把任务从默认列表里收起来，'
        + '不会删任何东西——所以这里空着就真的是没归档过。'));
      return box;
    }
    groups.forEach((group) => {
      box.append(taskBlockHead((group.categoryName || '未分类')
        + ' · ' + formatCount(group.count) + ' 个'));
      const list = el('div', 'dash-list');
      (group.tasks || []).forEach((item) => {
        const row = el('div', 'dash-row');
        row.append(el('span', 'dash-row-main', item.name || item.target || item.id));
        row.append(el('span', 'dash-row-meta',
          (item.target || '') + ' · ' + formatTime(item.updatedAt || 0)));
        const acts = el('div', 'task-acts');
        const back = el('button', 'btn xs', '取消归档');
        back.type = 'button';
        back.dataset.act = 'unarchive';
        back.dataset.id = item.id;
        acts.append(back);
        row.append(acts);
        list.append(row);
      });
      box.append(list);
    });
    return box;
  }

  function taskBlockScope(taskId, data) {
    const box = el('div', 'task-block');
    if (data.error) {
      box.append(el('p', 'task-block-note', data.error));
      return box;
    }
    const counts = data.counts || {};
    box.append(el('p', 'task-block-note',
      '允许 ' + formatCount(counts.allow || 0)
      + ' · 禁止 ' + formatCount(counts.deny || 0)
      + '。行上「删」只删那一条；底下那张表是**全量替换**：存一次就是存这一屏，'
      + '表里没写的行就是被删掉的规则。两条路都在，因为「删掉我刚加错的那条」不该'
      + '要求先把整屏读下来再整体发回——两个人同时改，后提交的会把前一个人刚加的冲掉。'));
    const entries = data.entries || [];
    if (entries.length) {
      const list = el('div', 'dash-list');
      entries.forEach((item) => {
        const row = el('div', 'dash-row');
        row.append(el('span', 'dash-row-main',
          (item.kind === 'deny' ? '禁止' : '允许') + ' · ' + (item.text || '')));
        row.append(el('span', 'dash-row-meta', item.createdBy || ''));
        if (data.admin && item.id) {
          const acts = el('span', 'task-acts');
          const drop = el('button', 'btn xs', '删');
          drop.type = 'button';
          drop.dataset.act = 'scope-del';
          drop.dataset.id = taskId;
          drop.dataset.entry = item.id;
          acts.append(drop);
          row.append(acts);
        }
        list.append(row);
      });
      box.append(list);
    } else {
      box.append(el('p', 'task-block-note', '还没有条目：这条任务的授权范围是空的。'));
    }
    if (data.admin) {
      const addForm = el('div', 'task-form');
      addForm.dataset.taskScopeAdd = taskId;
      const kindPick = el('select', 'select xs-select');
      kindPick.dataset.field = 'kind';
      [{ value: 'allow', text: '允许' }, { value: 'deny', text: '禁止' }].forEach((opt) => {
        const node = el('option', '', opt.text);
        node.value = opt.value;
        kindPick.append(node);
      });
      addForm.append(taskField('类型', kindPick));
      const entryInput = el('input', 'input');
      entryInput.type = 'text';
      entryInput.placeholder = '一条，例如 10.10.10.0/24';
      entryInput.dataset.field = 'text';
      addForm.append(taskField('条目', entryInput));
      const addActs = el('div', 'task-form-actions');
      const addButton = el('button', 'btn sm', '加一条');
      addButton.type = 'button';
      addButton.dataset.act = 'scope-add';
      addButton.dataset.id = taskId;
      addActs.append(addButton);
      addForm.append(addActs);
      box.append(addForm);
    }
    const form = el('div', 'task-form');
    form.dataset.taskScope = taskId;
    const area = el('textarea', 'input');
    area.rows = 4;
    area.placeholder = '一行一条：allow 10.10.10.0/24';
    area.dataset.field = 'entries';
    area.value = entries.map((item) => (item.kind || 'allow') + ' ' + (item.text || '')).join('\n');
    form.append(taskField('允许 / 禁止条目', area));
    const acts = el('div', 'task-form-actions');
    const save = el('button', 'btn sm', '保存这一屏');
    save.type = 'button';
    save.dataset.act = 'scope-save';
    save.dataset.id = taskId;
    acts.append(save);
    form.append(acts);
    box.append(form);
    return box;
  }

  function taskCategoryRow(taskId, detail) {
    const wrap = el('div', 'task-form');
    const cats = catalogCategories().map((item) => ({ value: item.id, text: item.name || item.id }));
    const pick = taskSelect('categoryId', cats, detail.categoryId || '', '不分类');
    wrap.append(taskField('分类', pick));
    const acts = el('div', 'task-form-actions');
    const save = el('button', 'btn sm', '存分类');
    save.type = 'button';
    save.dataset.act = 'cat-save';
    save.dataset.id = taskId;
    acts.append(save);
    wrap.append(acts);
    return wrap;
  }

  // 分类筛选是**前端**筛的。后端 list_tasks 已经有状态与关键字两条筛选，而它的
  // counts 按全部行算——再加一条后端筛选，「计数为什么和列表条数不一样」就会多
  // 一个例外，而那个例外没有任何人会去记。一页最多 200 条，本地筛一遍更好解释。
  function taskVisible(task) {
    const want = tasksFilter.categoryId;
    if (!want) return true;
    if (want === '__none__') return !task.categoryId;
    return task.categoryId === want;
  }

  function renderTasks() {
    const host = $('#tasks-body');
    if (!host) return;
    clear(host);
    const meta = $('#tasks-meta');
    const counts = (tasksData && tasksData.counts) || {};
    if (meta) {
      meta.textContent = '共 ' + formatCount(counts.all || 0) + ' 个任务'
        + ' · 进行中 ' + formatCount(counts.running || 0)
        + ' · 排队 ' + formatCount(counts.queued || 0)
        + ' · 已暂停 ' + formatCount(counts.paused || 0)
        + ' · 归档 ' + formatCount(counts.archived || 0);
    }
    taskFilters(host);
    if (tasksNote.text) {
      host.append(el('p', 'task-note' + (tasksNote.bad ? ' bad' : ''), tasksNote.text));
      tasksNote = { text: '', bad: false };
    }
    if (tasksArchive && tasksArchive.open) {
      host.append(taskArchivePanel());
      return;
    }
    if (!tasksData) {
      host.append(el('div', 'muted', '正在读取任务…'));
      host.append(taskNotice());
      return;
    }
    const all = tasksData.tasks || [];
    const items = all.filter(taskVisible);
    if (!items.length) {
      const why = tasksFilter.archived
        ? (all.length ? '这一档分类下没有归档的任务' : '没有归档的任务')
        : (all.length ? '这一档分类下没有任务' : '还没有任务。到会话说一句话——任务就在那句话里成立。');
      host.append(emptyBlock(why));
    } else {
      const list = el('div', 'task-list');
      items.forEach((item) => list.append(taskRow(item)));
      host.append(list);
      if (items.length < all.length) {
        host.append(el('p', 'task-sub', '这一类之外的 '
          + formatCount(all.length - items.length) + ' 个任务没显示——把分类切回「全部分类」就能看到。'));
      }
    }
    host.append(taskNotice());
  }

  async function loadTasks() {
    const host = $('#tasks-body');
    if (!host) return;
    const params = [];
    if (tasksFilter.status) params.push('status=' + encodeURIComponent(tasksFilter.status));
    if (tasksFilter.archived) params.push('archived=1');
    if (tasksFilter.keyword) params.push('q=' + encodeURIComponent(tasksFilter.keyword));
    params.push('limit=200');
    const query = params.join('&');
    // 分类与模板跟列表一起取：三个请求并发，失败也只报一次——分类下拉空着与
    // 列表空着在屏幕上都是「什么都没有」，分两次报错只会让人点两次刷新。
    const [list, _catalog] = await Promise.all([
      apiGet('/tasks?' + query).catch((error) => ({ error: error })),
      loadTaskCatalog(),
    ]);
    if (list && list.error) {
      clear(host);
      host.append(errorBlock('任务加载失败：' + list.error.message));
      return;
    }
    tasksData = list;
    renderTasks();
  }

  async function loadTaskArchive() {
    try {
      const data = await apiGet('/tasks/archive/summary');
      tasksArchive = { open: true, groups: data.groups || [], total: data.total || 0 };
    } catch (error) {
      tasksArchive = { open: true, error: '归档中心读取失败：' + error.message };
    }
    renderTasks();
  }

  // 新建：把表单读成一份 create_task 的入参。**字段缺省一律不传**——传空串等于
  // 明确要求「把这一项清空」，而建任务时清空一个字段是没有意义的动作，只会让
  // 后端那几处「表单 or 模板」的回退失效。
  async function submitTaskCreate(form) {
    if (!form) return;
    const body = {};
    ['name', 'target', 'objective', 'authorization', 'remark',
      'categoryId', 'templateId', 'llmProfileId'].forEach((key) => {
      const value = formText(form, key);
      if (value) body[key] = value;
    });
    const workers = formText(form, 'workerCount');
    if (workers) body.workerCount = Number(workers);
    const coverage = formNode(form, 'coverageEnabled');
    if (coverage) body.coverageEnabled = !!coverage.checked;
    const goals = formLines(form, 'goals');
    if (goals.length) body.goals = goals;
    const scope = formScope(form);
    if (scope.length) body.scope = scope;
    if (!body.target) {
      taskNote('被测目标不能为空：不知道打谁的任务，建出来也派不出活。', true);
      renderTasks();
      return;
    }
    try {
      const made = await apiPost('/tasks/create', body);
      tasksForm = { open: false, templateId: '' };
      taskNote('已建任务 ' + (made.name || made.id) + '，共 '
        + formatCount((made.counts && made.counts.goal) || 0) + ' 个目标。', false);
      tasksOpen = '';
      taskDetail = { taskId: '', payload: null, open: '', data: {} };
    } catch (error) {
      taskNote('建任务失败：' + error.message, true);
      renderTasks();
      return;
    }
    await loadTasks();
  }

  // 详情块里那几个动作：各自只发一条请求，成功后把**那一块**的缓存清掉重取，
  // 不整页重刷——重刷会把整棵详情树拆了重画，正在填的表单里丢了半行字是最
  // 典型的后果。
  async function runBlockAction(taskId, key, path, body, done) {
    try {
      await apiPost(path, body || {});
      taskNote(done || '已提交', false);
    } catch (error) {
      taskNote(error.message, true);
      renderTasks();
      return;
    }
    delete taskDetail.data[key];
    if (key === 'meta') {
      await refreshTaskDetail(taskId);
      return;
    }
    await loadTasks();
    if (tasksOpen === taskId && taskDetail.open === key) {
      taskDetail.open = '';
      await toggleTaskBlock(taskId, key);
    } else {
      renderTasks();
    }
  }

  function bindTaskEvents() {
    const host = $('#tasks-body');
    if (!host) return;
    // 换模板时把表单重填一遍：先收起再展开等于让人重填一次，而模板的用途
    // 正是「别重填」。只在字段还是空的时候填，已经写下的字不覆盖。
    host.addEventListener('change', (event) => {
      const pick = event.target.closest('[data-field="templateId"]');
      if (!pick) return;
      const form = pick.closest('[data-task-form]');
      tasksForm.templateId = pick.value;
      applyTemplate(form, pick.value);
    });
    // 点击委托是 async：`chat-stop` 要把回包里的那句话（停止之后排队的还在不在）
    // 原样说给使用者，同步处理器只能拿到一个还没落地的 Promise。
    host.addEventListener('click', async (event) => {
      const button = event.target.closest('button[data-act]');
      if (!button) return;
      const id = button.dataset.id;
      const act = button.dataset.act;
      const task = ((tasksData && tasksData.tasks) || [])
        .filter((item) => item.id === id)[0];
      if (act === 'new-chat') { switchTab('dsh'); return; }
      if (act === 'task-new-toggle') {
        tasksForm.open = !tasksForm.open;
        tasksArchive = null;
        renderTasks();
        return;
      }
      if (act === 'task-create') {
        submitTaskCreate(host.querySelector('[data-task-form="create"]'));
        return;
      }
      if (act === 'archive-center') { loadTaskArchive(); return; }
      if (act === 'detail') { openTask(id); return; }
      if (act === 'pause') { taskAction('/tasks/' + encodeURIComponent(id) + '/pause', {}, '已暂停'); return; }
      if (act === 'resume') { taskAction('/tasks/' + encodeURIComponent(id) + '/resume', {}, '已恢复'); return; }
      if (act === 'archive') {
        // 归档不改目标，只把它从默认列表里收起来；取消归档随时能回来。
        // 仍要念一句，因为这个动作会让它从当前列表里消失，读的人容易以为删了。
        if (!window.confirm('把任务「' + ((task && task.name) || id) + '」收进归档？\n\n'
          + '它不会被删除：勾上「只看归档」就能找回来。')) return;
        taskAction('/tasks/' + encodeURIComponent(id) + '/archive', {}, '已归档');
        return;
      }
      if (act === 'unarchive') {
        taskAction('/tasks/' + encodeURIComponent(id) + '/unarchive', {}, '已取消归档');
        return;
      }
      if (act === 'pin') {
        const pinned = !!(task && task.pinned);
        taskAction('/tasks/' + encodeURIComponent(id), { pinned: !pinned },
          pinned ? '已取消置顶' : '已置顶');
        return;
      }
      if (act === 'block') { toggleTaskBlock(id, button.dataset.block); return; }
      if (act === 'file-peek') {
        const rel = button.dataset.path || '';
        if (!rel) return;
        // 再点一次同一个文件就是收起：那个按钮上的字已经写着「收起」，点了没反应
        // 会让人以为坏了。
        if (taskPeek.path === rel && !taskPeek.data) { taskPeek = { path: '', data: null }; renderTasks(); return; }
        if (taskPeek.path === rel && taskPeek.data && !taskPeek.data.error) {
          taskPeek = { path: '', data: null };
          renderTasks();
          return;
        }
        loadTaskPeek(id, rel);
        return;
      }
      if (act === 'file-download') {
        // 路径的第一段是会话号，第二段起才是相对路径——后端按这两段判归属，
        // 所以这里不做任何拆解，原样带给下载接口。
        const rel = button.dataset.path || '';
        if (!rel) return;
        downloadTaskFile('/tasks/' + encodeURIComponent(id) + '/files/download?path='
          + encodeURIComponent(rel), rel.split('/').pop());
        return;
      }
      if (act === 'file-archive') {
        // 整批：不带 path，后端就按这条任务名下每一层根整棵打包，条目名自己带会话号前缀。
        const name = (task && (task.name || task.target)) || 'task';
        downloadTaskFile('/tasks/' + encodeURIComponent(id) + '/files/archive',
          name + '-files.zip');
        return;
      }
      if (act === 'cat-save') {
        const row = host.querySelector('[data-task-detail="' + id + '"] [data-field="categoryId"]');
        runBlockAction(id, 'meta', '/tasks/' + encodeURIComponent(id) + '/category',
          { categoryId: row ? row.value : '' }, '分类已存');
        return;
      }
      if (act === 'intel-add') {
        const form = host.querySelector('[data-task-intel="' + id + '"]');
        if (!form) return;
        const body = {};
        ['kind', 'title', 'content', 'evidence'].forEach((key) => {
          const value = formText(form, key);
          if (value) body[key] = value;
        });
        runBlockAction(id, 'intel',
          '/tasks/' + encodeURIComponent(id) + '/intelligence', body, '已记一条情报');
        return;
      }
      if (act === 'intel-edit') {
        const intelId = button.dataset.intel || '';
        if (!intelId) return;
        // 再点一次同一行就是收起。这里不重取数据：表单的初值来自已经拿到的列表行，
        // 重取一次再展开，等于让人多点一次却看不到任何变化。
        taskIntelEdit = taskIntelEdit.id === intelId
          ? { id: '', data: null } : { id: intelId, data: null };
        renderTasks();
        return;
      }
      if (act === 'intel-save') {
        const intelId = button.dataset.intel || '';
        const form = host.querySelector('[data-task-intel-edit="' + intelId + '"]');
        if (!intelId || !form) return;
        const body = {};
        ['kind', 'status', 'title', 'content'].forEach((key) => {
          const value = formText(form, key);
          if (value) body[key] = value;
        });
        // 证据**无条件带上**：后端把空串当"清掉这条旧证据"。跳过空值的话，一次误记
        // 的证据就再也删不掉了，只能整条删了重写。
        body.evidence = formText(form, 'evidence');
        // 标题与内容在后端是同一个 detail 的两半；表单里两个都预填了当前值，
        // 所以"只改状态"那条路径也会把原文带回去，不会把标题冲掉。
        taskIntelEdit = { id: '', data: null };
        runBlockAction(id, 'intel',
          '/tasks/' + encodeURIComponent(id) + '/intelligence/'
          + encodeURIComponent(intelId) + '/update', body, '这一条已存');
        return;
      }
      if (act === 'intel-status') {
        const intelId = button.dataset.intel || '';
        if (!intelId) return;
        const cached = (taskDetail.data && taskDetail.data.intel) || null;
        const row = cached && (cached.items || []).filter(
          (item) => item.id === intelId)[0];
        // 只发 status 一位：后端是合并语义，标题与内容会自己带上。这里也**只看**
        // 当前值决定翻到哪一边——按界面文案反推，迟早会与后端那套取值对不上。
        const next = (row && row.status === 'inferred') ? 'observed' : 'inferred';
        runBlockAction(id, 'intel',
          '/tasks/' + encodeURIComponent(id) + '/intelligence/'
          + encodeURIComponent(intelId) + '/update', { status: next },
          next === 'inferred' ? '已标为推断' : '已标为亲眼看到');
        return;
      }
      if (act === 'intel-del') {
        const intelId = button.dataset.intel || '';
        if (!intelId) return;
        if (!window.confirm('删掉这条情报？\n\n它挂在链路图上，删了之后那条边也会一起消失。')) return;
        taskIntelEdit = { id: '', data: null };
        runBlockAction(id, 'intel',
          '/tasks/' + encodeURIComponent(id) + '/intelligence/'
          + encodeURIComponent(intelId) + '/delete', {}, '这一条已删');
        return;
      }
      if (act === 'retest-open') {
        const findingId = button.dataset.finding || '';
        if (!findingId) return;
        // 再点一次收起，不重取：那一条的历史就在手上。
        taskRetestEdit = (taskRetestEdit.id === findingId)
          ? { id: '', data: null } : { id: findingId, data: null };
        renderTasks();
        return;
      }
      if (act === 'retest-save') {
        const findingId = button.dataset.finding || '';
        if (!findingId) return;
        const form = host.querySelector('[data-task-retest="' + findingId + '"]');
        if (!form) return;
        const body = {};
        ['verdict', 'summary', 'evidence'].forEach((key) => {
          const value = formText(form, key);
          if (value) body[key] = value;
        });
        // 两句正文缺一个后端就 400。这里先拦下来说清楚是哪一句缺，而不是让请求
        // 跑一趟再回一句"summary 不能为空"——那个字段名在界面上并不存在。
        if (!body.summary) {
          taskNote('写一句这次看到了什么：只记结论而不记看到了什么，下一次复核的人没法判断这次是不是真验过。', true);
          renderTasks();
          return;
        }
        if (!body.evidence) {
          taskNote('补一句依据：复核也要有依据，否则它与没复核一样。', true);
          renderTasks();
          return;
        }
        taskRetestEdit = { id: '', data: null };
        runBlockAction(id, 'findings',
          '/tasks/' + encodeURIComponent(id) + '/findings/'
          + encodeURIComponent(findingId) + '/retest', body, '这次复核已记下');
        return;
      }
      if (act === 'rule-toggle') {
        const ruleId = button.dataset.rule || '';
        if (!ruleId) return;
        const rule = ruleRow(button.dataset.id, ruleId);
        runBlockAction(id, 'intercept',
          '/tasks/' + encodeURIComponent(id) + '/intercept-rules/'
          + encodeURIComponent(ruleId) + '/toggle', {},
          rule && rule.enabled ? '这一条已停用' : '这一条已启用');
        return;
      }
      if (act === 'rule-edit') {
        const ruleId = button.dataset.rule || '';
        if (!ruleId) return;
        // 再点一次就收起，不重新拉数据：手上这一份就是刚才那一份。
        taskRuleEdit = (taskRuleEdit.id === ruleId)
          ? { id: '', data: null } : { id: ruleId, data: null };
        renderTasks();
        return;
      }
      if (act === 'rule-save') {
        const ruleId = button.dataset.rule || '';
        if (!ruleId) return;
        const form = host.querySelector('[data-task-rule="' + ruleId + '"]');
        if (!form) return;
        const body = {};
        ['name', 'pattern', 'matchType', 'priority', 'message'].forEach((key) => {
          const value = formText(form, key);
          if (value) body[key] = value;
        });
        taskRuleEdit = { id: '', data: null };
        runBlockAction(id, 'intercept',
          '/tasks/' + encodeURIComponent(id) + '/intercept-rules/'
          + encodeURIComponent(ruleId) + '/update', body, '这一条已存');
        return;
      }
      if (act === 'rule-del') {
        const ruleId = button.dataset.rule || '';
        if (!ruleId) return;
        // 删一条规则不会撤销任何已经发生过的事，但会**静默改变之后的拦截口径**——
        // 所以要说清楚它删掉的是什么，而不是只问一句「确定吗」。
        const rule = ruleRow(button.dataset.id, ruleId);
        const label = rule ? (rule.name || rule.pattern || ruleId) : ruleId;
        if (!window.confirm('删掉这条拦截规则？\n\n' + label
          + '\n\n删了之后它就不再拦任何工具调用。内置的那几条可以用「把内置规则复制到本任务」重新拿回来。')) return;
        taskRuleEdit = { id: '', data: null };
        runBlockAction(id, 'intercept',
          '/tasks/' + encodeURIComponent(id) + '/intercept-rules/'
          + encodeURIComponent(ruleId) + '/delete', {}, '这一条已删');
        return;
      }
      if (act === 'chat-stop') {
        const sessionId = button.dataset.session || '';
        if (!sessionId) return;
        button.disabled = true;
        // 走一条专用的请求而不是 `runBlockAction`：停止的**回话**本身是内容——
        // 「这一轮会在当前这一步之后结束」「排队里还有 N 条不会自动开」这两句，
        // 正是人按下按钮之后最需要知道的事，压成一句「已提交」等于没说。
        try {
          const answer = await apiPost('/tasks/' + encodeURIComponent(id)
            + '/chat/stop', { sessionId: sessionId });
          taskNote(answer.note || (answer.stopping ? '已请求停止。' : '这一轮刚好已经结束了。'),
            false);
        } catch (error) {
          taskNote(error.message, true);
          return;
        }
        if (taskDetail.data) delete taskDetail.data.chat;
        if (tasksOpen === id && taskDetail.open === 'chat') {
          await toggleTaskBlock(id, 'chat');
        }
        return;
      }
      if (act === 'llm-save') {
        const form = host.querySelector('[data-task-llm="' + id + '"]');
        runBlockAction(id, 'llm', '/tasks/' + encodeURIComponent(id) + '/llm',
          { profileId: form ? formText(form, 'llmProfileId') : '' }, '模型已指定');
        return;
      }
      if (act === 'asset-add') {
        const form = host.querySelector('[data-task-asset-add="' + id + '"]');
        if (!form) return;
        const value = formText(form, 'value');
        if (!value) {
          taskNote('值不能为空：一条资产没有名字，覆盖度里就没有它的位置。', true);
          renderTasks();
          return;
        }
        const row = { type: formText(form, 'type') || 'app', value: value };
        const parent = formText(form, 'parentId');
        if (parent) row.parentId = parent;
        let answer = null;
        try {
          answer = await apiPost('/tasks/' + encodeURIComponent(id) + '/assets',
            { assets: [row] });
        } catch (error) {
          taskNote(error.message, true);
          renderTasks();
          return;
        }
        // 复用与新增要分开说：人再写一次同一个 IP 时真正想问的是「它是不是已经有了」，
        // 回一句「已加一条」会让他以为覆盖度里多了一台机器。
        const reused = (answer && answer.reused) || [];
        taskNote(reused.length
          ? '这一条已经有了，复用了原来那条：' + (reused[0].value || value)
          : '已加一条：' + value, false);
        delete taskDetail.data.coverage;
        await loadTasks();
        if (tasksOpen === id && taskDetail.open === 'coverage') {
          taskDetail.open = '';
          await toggleTaskBlock(id, 'coverage');
        }
        return;
      }
      if (act === 'asset-del') {
        const assetId = button.dataset.asset || '';
        if (!assetId) return;
        const cov = (taskDetail.data || {}).coverage || {};
        const asset = (cov.assets || []).filter((item) => item.assetId === assetId)[0];
        // 删资产会影响覆盖度的分母与三处引用，念出来再删。
        if (!window.confirm('删掉这条资产？\n\n'
          + ((asset && ((asset.typeLabel || asset.type || '资产') + ' · ' + (asset.value || '')))
            || assetId)
          + '\n\n子资产不会跟着删（删父的本意通常是「这条记错了」，而子资产往往才是'
          + '真正测过的那些），漏洞也不会删。删完会说清楚谁还指着空处。')) return;
        let answer = null;
        try {
          answer = await apiPost('/tasks/' + encodeURIComponent(id) + '/assets/'
            + encodeURIComponent(assetId) + '/delete', {});
        } catch (error) {
          taskNote(error.message, true);
          renderTasks();
          return;
        }
        const kids = (answer && answer.children) || [];
        const dangling = (answer && answer.findingsWithDanglingAsset) || [];
        taskNote('这一条已删。'
          + (kids.length ? ' 有 ' + formatCount(kids.length) + ' 条子资产被提成根。' : '')
          + (dangling.length
            ? ' 有 ' + formatCount(dangling.length) + ' 条漏洞的受影响资产没了，记得回头改。'
            : ''), false);
        delete taskDetail.data.coverage;
        await loadTasks();
        if (tasksOpen === id && taskDetail.open === 'coverage') {
          taskDetail.open = '';
          await toggleTaskBlock(id, 'coverage');
        }
        return;
      }
      if (act === 'scope-save') {
        const form = host.querySelector('[data-task-scope="' + id + '"]');
        if (!form) return;
        // 提交的是**这一屏的全部行**：后端按 (kind,text) 做差分，删掉的行就是
        // 被删掉的规则。所以这里不挑，原样把每一行送上去。
        const entries = formScope(form);
        runBlockAction(id, 'scope', '/tasks/' + encodeURIComponent(id) + '/scope',
          { entries: entries }, '范围已存');
        return;
      }
      if (act === 'scope-add') {
        const form = host.querySelector('[data-task-scope-add="' + id + '"]');
        if (!form) return;
        const text = formText(form, 'text');
        if (!text) {
          taskNote('条目不能为空：空白的一条写下去，这一屏下次保存时才会把它当成一行。', true);
          renderTasks();
          return;
        }
        const kind = formText(form, 'kind') || 'allow';
        runBlockAction(id, 'scope',
          '/tasks/' + encodeURIComponent(id) + '/constraints',
          { kind: kind, text: text }, '已加一条');
        return;
      }
      if (act === 'scope-del') {
        const entryId = button.dataset.entry || '';
        if (!entryId) return;
        const scope = (taskDetail.data || {}).scope || {};
        const entry = (scope.entries || []).filter((item) => item.id === entryId)[0];
        // 删一条授权范围里的条目，等于说「这一单不许再碰那里」。念出来再删。
        if (!window.confirm('删掉这条范围？\n\n'
          + ((entry && ((entry.kind === 'deny' ? '禁止 · ' : '允许 · ') + entry.text)) || entryId)
          + '\n\n删掉之后它不再约束任何作业；整屏保存那张表是另一条路，两条都动同一份数据。')) return;
        runBlockAction(id, 'scope',
          '/tasks/' + encodeURIComponent(id) + '/constraints/'
          + encodeURIComponent(entryId) + '/delete', {}, '这一条已删');
        return;
      }
      if (act === 'intercept-seed') {
        taskRuleEdit = { id: '', data: null };
        runBlockAction(id, 'intercept',
          '/tasks/' + encodeURIComponent(id) + '/intercept-rules', {}, '内置规则已复制到本任务');
        return;
      }
      if (act === 'goal-edit') {
        const goalId = button.dataset.goal || '';
        if (!goalId) return;
        taskGoalEdit = (taskGoalEdit.id === goalId)
          ? { id: '', data: null } : { id: goalId, data: null };
        renderTasks();
        return;
      }
      if (act === 'goal-save') {
        const goalId = button.dataset.goal || '';
        if (!goalId) return;
        const form = host.querySelector('[data-task-goal-edit="' + goalId + '"]');
        if (!form) return;
        const body = {};
        // 三项都发：清空「说明」是一次真实意图，只发非空等于让它永远清不掉。
        ['text', 'objective', 'authorization'].forEach((key) => {
          body[key] = formText(form, key);
        });
        taskGoalEdit = { id: '', data: null };
        runBlockAction(id, 'intents',
          '/tasks/' + encodeURIComponent(id) + '/goals/'
          + encodeURIComponent(goalId) + '/update', body, '这一条已存');
        return;
      }
      if (act === 'goal-add') {
        const form = host.querySelector('[data-task-goal-add="' + id + '"]');
        if (!form) return;
        const text = formText(form, 'text');
        if (!text) {
          taskNote('目标内容不能为空：空目标写下去，报告第一行就是空白。', true);
          renderTasks();
          return;
        }
        runBlockAction(id, 'intents',
          '/tasks/' + encodeURIComponent(id) + '/goals', { text: text }, '已加一条目标');
        return;
      }
      if (act === 'goal-del') {
        const goalId = button.dataset.goal || '';
        if (!goalId) return;
        const goals = ((taskDetail.data || {}).intents || {}).goals || [];
        const goal = goals.filter((item) => item.id === goalId)[0];
        // 删目标不会撤回已经跑出来的证据，但会让「这条任务要拿到什么」少一条——
        // 而规划器是照这份清单派活的，所以要把删掉的那句念出来。
        if (!window.confirm('删掉这条目标？\n\n'
          + ((goal && goal.text) || goalId)
          + '\n\n已经跑出来的证据不受影响；但规划器之后不会再照着它派活。')) return;
        taskGoalEdit = { id: '', data: null };
        runBlockAction(id, 'intents',
          '/tasks/' + encodeURIComponent(id) + '/goals/'
          + encodeURIComponent(goalId) + '/delete', {}, '这一条已删');
        return;
      }
      if (act === 'intent-peek') {
        const intentId = button.dataset.intent || '';
        if (!intentId) return;
        // 再点一次就收起：与文件预览、改情报同一套开合，不重新取一遍数据。
        if (taskIntentPeek.id === intentId) {
          taskIntentPeek = { id: '', data: null };
          renderTasks();
          return;
        }
        await loadIntentPeek(id, intentId);
        return;
      }
      if (act === 'intent-move') {
        runBlockAction(id, 'intents',
          '/tasks/' + encodeURIComponent(id) + '/intents/'
          + encodeURIComponent(button.dataset.intent || '') + '/control',
          { action: button.dataset.move || '' }, '意图状态已改');
        return;
      }
      if (act === 'intent-rerun-blocked') {
        runBlockAction(id, 'intents', '/tasks/intents/rerun-blocked',
          { taskId: id }, '阻塞的意图已放回进行中');
      }
    });
  }

  /* ------------------------------------------------------------ 计划任务 */
  // 一条计划 = 一个**时间窗口规则** + 一组要放开的任务。窗口进：恢复任务；窗口出：
  // 暂停任务。判定全在后端（`pentestd/schedules.py`），前端只做表单一件事——把"哪天、
  // 几点、星期几"翻译成后端要的三个字段，再把它算出来的状态显示出来。
  //
  // **为什么不在前端算「下次窗口」**：算窗口要认时区、跨午夜、规则过期三种情况，
  // 前端算一份、后端算一份，两边差一分钟就是两个不同的答案。这一页所有时间词都
  // 来自 `schedules.view()`。
  const SCHED_STATE_TEXT = {
    waiting: '等待窗口', running: '窗口运行中', outside: '窗口外',
    completed: '已完成', error: '错误',
  };
  const SCHED_RULE_TEXT = { once: '指定日期', weekly: '按周' };
  const SCHED_ZONE_TEXT = { system: '系统时区', beijing: '北京时间 UTC+8' };
  const SCHED_KIND_TEXT = { enter: '进窗口', exit: '出窗口', manual: '手动运行' };
  const SCHED_WEEKDAYS = [[1, '周一'], [2, '周二'], [3, '周三'], [4, '周四'],
    [5, '周五'], [6, '周六'], [7, '周日']];
  let schedData = null;
  let schedEditing = null;
  let schedOpen = '';
  let schedNote = { text: '', bad: false };
  let schedCandidates = null;

  function schedState(item) {
    return SCHED_STATE_TEXT[item.state] || item.statusLabel || item.state || '未知';
  }

  function schedWhen(item) {
    const rule = SCHED_RULE_TEXT[item.rule] || item.rule || '';
    const parts = [rule];
    if (item.rule === 'once') {
      if (item.startDate) parts.push(item.startDate);
    } else if ((item.weekdayLabels || []).length) {
      parts.push(item.weekdayLabels.join('、'));
    }
    if (item.startTime) {
      parts.push(item.endTime ? item.startTime + '–' + item.endTime
        : item.startTime + ' 起 1 小时');
    }
    if (item.endDate) parts.push('到 ' + item.endDate);
    return parts.join(' · ');
  }

  function schedSetNote(text, bad) {
    schedNote = { text: text || '', bad: !!bad };
  }

  async function schedAction(path, body) {
    try {
      await apiPost(path, body || {});
      schedSetNote('已提交', false);
    } catch (error) {
      schedSetNote(error.message, true);
    }
    await loadSchedules();
  }

  async function loadSchedCandidates() {
    if (schedCandidates) return schedCandidates;
    try {
      const data = await apiGet('/tasks?limit=200');
      schedCandidates = data.tasks || [];
    } catch (error) {
      // 拿不到任务列表不该挡住「只建一条不绑任务的计划」这种事。
      schedCandidates = [];
    }
    return schedCandidates;
  }

  function schedWeekdayRow(form, item) {
    const row = el('div', 'sched-weekdays');
    const picked = item.weekdays || [];
    SCHED_WEEKDAYS.forEach((pair) => {
      const label = el('label', 'task-check');
      const box = el('input');
      box.type = 'checkbox';
      box.name = 'weekday';
      box.value = String(pair[0]);
      box.checked = picked.indexOf(pair[0]) >= 0;
      label.append(box);
      label.append(document.createTextNode(pair[1]));
      row.append(label);
    });
    form.append(row);
    return row;
  }

  function schedTaskRow(form, item, candidates) {
    const box = el('div', 'sched-tasks');
    box.append(el('div', 'sched-label', '要放开的任务'));
    if (!candidates.length) {
      box.append(el('p', 'task-sub', '还没有任务可绑；先到「任务」页建一个。'));
      form.append(box);
      return box;
    }
    const picked = item.taskIds || [];
    candidates.forEach((task) => {
      const label = el('label', 'task-check');
      const input = el('input');
      input.type = 'checkbox';
      input.name = 'task';
      input.value = task.id;
      input.checked = picked.indexOf(task.id) >= 0;
      label.append(input);
      label.append(document.createTextNode(
        (task.name || task.target || task.id) + '（' + (task.status || '') + '）'));
      box.append(label);
    });
    form.append(box);
    return box;
  }

  function schedField(form, label, node, hint) {
    const wrap = el('label', 'sched-field');
    wrap.append(el('span', 'sched-label', label));
    wrap.append(node);
    if (hint) wrap.append(el('span', 'task-sub', hint));
    form.append(wrap);
    return node;
  }

  function schedInput(kind, name, value) {
    const node = el('input', 'input');
    node.type = kind;
    node.name = name;
    if (value) node.value = value;
    return node;
  }

  function schedSelect(name, options, value) {
    const node = el('select', 'select');
    node.name = name;
    options.forEach((pair) => {
      const option = el('option', '', pair[1]);
      option.value = pair[0];
      if (value === pair[0]) option.selected = true;
      node.append(option);
    });
    return node;
  }

  async function schedFormView(host) {
    const item = schedEditing || {};
    const editing = !!item.id;
    const form = el('form', 'sched-form');
    form.append(el('h2', 'dash-card-title', editing ? '编辑计划' : '新建计划'));
    schedField(form, '计划名称', schedInput('text', 'name', item.name || ''),
      '列表上按名称、编号、任务编号搜索');
    schedField(form, '规则', schedSelect('rule',
      [['once', '指定日期'], ['weekly', '按周']], item.rule || 'weekly'),
      '指定日期只在当天生效；按周按星期几重复');
    schedField(form, '时区', schedSelect('timezone', [['system', '系统时区'],
      ['beijing', '北京时间 UTC+8']], item.timezone || 'system'),
      '窗口时间按这个时区解释');
    schedField(form, '开始日期', schedInput('date', 'startDate', item.startDate || ''),
      '按周可以留空 = 从本周开始');
    schedField(form, '结束日期', schedInput('date', 'endDate', item.endDate || ''),
      '留空 = 一直有效');
    form.append(el('div', 'sched-label', '星期（按周时必选）'));
    schedWeekdayRow(form, item);
    schedField(form, '开始时间', schedInput('time', 'startTime', item.startTime || ''),
      '窗口从这里开始');
    schedField(form, '结束时间', schedInput('time', 'endTime', item.endTime || ''),
      '留空 = 1 小时；早于开始时间表示跨午夜');
    schedField(form, '备注', schedInput('text', 'remark', item.remark || ''), '');
    const flags = el('div', 'sched-flags');
    [['enabled', '启用这条计划', item.enabled !== false],
      ['startImmediately', '窗口开启时立刻恢复任务', !!item.startImmediately],
    ].forEach((pair) => {
      const label = el('label', 'task-check');
      const box = el('input');
      box.type = 'checkbox';
      box.name = pair[0];
      box.checked = pair[2];
      label.append(box);
      label.append(document.createTextNode(pair[1]));
      flags.append(label);
    });
    form.append(flags);
    const candidates = await loadSchedCandidates();
    schedTaskRow(form, item, candidates);
    const acts = el('div', 'sched-acts');
    const submit = el('button', 'btn sm', editing ? '保存' : '创建');
    submit.type = 'submit';
    acts.append(submit);
    const cancel = el('button', 'btn sm ghost', '取消');
    cancel.type = 'button';
    cancel.addEventListener('click', () => {
      schedEditing = null;
      renderSchedules();
    });
    acts.append(cancel);
    form.append(acts);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const days = Array.prototype.slice
        .call(form.querySelectorAll('input[name="weekday"]'))
        .filter((box) => box.checked)
        .map((box) => Number(box.value));
      const tasks = Array.prototype.slice
        .call(form.querySelectorAll('input[name="task"]'))
        .filter((box) => box.checked)
        .map((box) => box.value);
      const read = (name) => {
        const node = form.querySelector('[name="' + name + '"]');
        return node ? node.value.trim() : '';
      };
      const flag = (name) => {
        const node = form.querySelector('[name="' + name + '"]');
        return !!(node && node.checked);
      };
      const body = {
        name: read('name'),
        rule: read('rule'),
        timezone: read('timezone'),
        startDate: read('startDate'),
        endDate: read('endDate'),
        startTime: read('startTime'),
        endTime: read('endTime'),
        weekdays: days,
        taskIds: tasks,
        remark: read('remark'),
        enabled: flag('enabled'),
        startImmediately: flag('startImmediately'),
      };
      // 后端会逐条报出缺哪个字段（"按周的计划要至少选一个星期"），所以这里
      // 不预校验——预校验写一份、后端写一份，两份话术迟早不一致。
      submit.disabled = true;
      try {
        if (editing) {
          await apiPost(`/schedules/${encodeURIComponent(item.id)}`, body);
        } else {
          await apiPost('/schedules', body);
        }
        schedEditing = null;
        schedSetNote('已保存', false);
      } catch (error) {
        schedSetNote(error.message, true);
      }
      submit.disabled = false;
      await loadSchedules();
    });
    host.append(form);
  }

  function schedTimeline(item) {
    const box = el('div', 'sched-timeline');
    box.dataset.schedRuns = item.id;
    box.append(el('div', 'muted', '正在读取运行时间线…'));
    return box;
  }

  async function loadSchedRuns(id) {
    const host = $('#schedules-body');
    const slot = host ? host.querySelector('[data-sched-runs="' + id + '"]') : null;
    if (!slot) return;
    let data = null;
    try {
      data = await apiGet(`/schedules/${encodeURIComponent(id)}/runs?limit=20`);
    } catch (error) {
      clear(slot);
      slot.append(errorBlock('读取时间线失败：' + error.message));
      return;
    }
    clear(slot);
    const runs = data.runs || [];
    if (!runs.length) {
      slot.append(emptyBlock('计划进入第一个窗口后会显示时间线'));
      return;
    }
    const list = el('div', 'dash-list');
    runs.forEach((run) => {
      const row = el('div', 'dash-row');
      row.append(el('span', 'dash-row-main',
        (SCHED_KIND_TEXT[run.kind] || run.kind || '运行')
        + (run.detail ? ' · ' + run.detail : '')));
      row.append(el('span', 'dash-row-meta', formatTime(run.createdAt || 0)));
      list.append(row);
    });
    slot.append(list);
  }

  function schedButton(label, act, item) {
    const button = el('button', 'btn xs', label);
    button.type = 'button';
    button.dataset.act = act;
    button.dataset.id = item.id;
    return button;
  }

  function schedRow(item) {
    const box = el('article', 'sched-row');
    const head = el('div', 'task-head');
    const name = el('span', 'task-name', item.name || item.id);
    name.title = name.textContent;
    head.append(name);
    head.append(el('span', 'task-state state-' + (item.enabled ? (item.state || 'outside') : 'idle'),
      item.enabled ? schedState(item) : '已停用'));
    head.append(el('span', 'task-meta', (SCHED_RULE_TEXT[item.rule] || item.rule || '')
      + ' · ' + (SCHED_ZONE_TEXT[item.timezone] || item.timezone || '')));
    box.append(head);
    box.append(el('p', 'task-sub', schedWhen(item)));
    const meta = [];
    meta.push('绑定任务 ' + formatCount((item.taskIds || []).length));
    meta.push('运行 ' + formatCount(item.runCount || 0) + ' 次');
    if (item.nextAt) meta.push('下次 ' + formatTime(item.nextAt));
    if (item.lastRun) meta.push('最近 ' + formatTime(item.lastRun.createdAt || 0));
    if (item.remark) meta.push('备注：' + item.remark);
    box.append(el('p', 'task-meta', meta.join(' · ')));
    const acts = el('div', 'task-acts');
    acts.append(schedButton('编辑', 'edit', item));
    acts.append(schedButton('立即运行', 'run', item));
    acts.append(schedButton(item.enabled ? '停用' : '启用',
      item.enabled ? 'disable' : 'enable', item));
    acts.append(schedButton('运行时间线', 'timeline', item));
    acts.append(schedButton('删除', 'delete', item));
    box.append(acts);
    if (schedOpen === item.id) box.append(schedTimeline(item));
    return box;
  }

  function renderSchedules() {
    const host = $('#schedules-body');
    if (!host) return;
    clear(host);
    const counts = (schedData && schedData.counts) || {};
    const meta = $('#schedules-meta');
    if (meta) {
      meta.textContent = '共 ' + formatCount(counts.total || 0) + ' 条'
        + ' · 启用 ' + formatCount(counts.enabled || 0)
        + ' · 窗口运行中 ' + formatCount(counts.running || 0)
        + ' · 停用 ' + formatCount(counts.disabled || 0);
    }
    if (schedNote.text) {
      host.append(el('p', 'task-note' + (schedNote.bad ? ' bad' : ''), schedNote.text));
      schedNote = { text: '', bad: false };
    }
    if (!schedData) {
      host.append(el('div', 'muted', '正在读取计划…'));
      return;
    }
    const items = schedData.schedules || [];
    if (!items.length && !schedEditing) {
      host.append(emptyBlock('还没有计划。新建一条，它会在窗口开启时恢复绑定的任务。'));
    } else if (items.length) {
      const list = el('div', 'sched-list');
      items.forEach((item) => list.append(schedRow(item)));
      host.append(list);
    }
    if (!schedEditing) {
      const newButton = el('button', 'btn sm', '新建计划');
      newButton.type = 'button';
      newButton.addEventListener('click', () => {
        schedEditing = {};
        renderSchedules();
      });
      host.append(newButton);
    } else {
      schedFormView(host);
    }
  }

  async function loadSchedules() {
    const host = $('#schedules-body');
    if (!host) return;
    try {
      schedData = await apiGet('/schedules?limit=200');
    } catch (error) {
      clear(host);
      host.append(errorBlock('计划加载失败：' + error.message));
      return;
    }
    renderSchedules();
    if (schedOpen) loadSchedRuns(schedOpen);
  }

  async function tickSchedules() {
    // 计划线程本来每 20 秒跑一轮；这个按钮给「等不及」和自测用。它会**真的**
    // 恢复/暂停任务，所以按钮上写的是「立即对表」而不是「模拟」。
    try {
      const data = await apiPost('/schedules/tick', {});
      const created = data.created || [];
      const skipped = data.skipped || [];
      schedSetNote(created.length
        ? '对表完成：处理了 ' + formatCount(created.length) + ' 条计划'
        : '对表完成：没有需要处理的窗口'
          + (skipped.length ? '（跳过 ' + formatCount(skipped.length) + ' 条）' : ''), false);
    } catch (error) {
      schedSetNote(error.message, true);
    }
    await loadSchedules();
  }

  function bindSchedEvents() {
    const host = $('#schedules-body');
    if (!host) return;
    host.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-act]');
      if (!button) return;
      const id = button.dataset.id;
      const act = button.dataset.act;
      const item = ((schedData && schedData.schedules) || [])
        .filter((row) => row.id === id)[0];
      if (act === 'edit') {
        schedEditing = item || {};
        renderSchedules();
        return;
      }
      if (act === 'timeline') {
        schedOpen = schedOpen === id ? '' : id;
        renderSchedules();
        if (schedOpen) loadSchedRuns(schedOpen);
        return;
      }
      if (act === 'run') {
        // 「立即运行」会**真的恢复**这条计划绑定的任务（等同窗口开启）。
        // 不是预览：任务会开始跑，所以先问一句，并念出绑了几个任务。
        const bound = ((item && item.taskIds) || []).length;
        if (!window.confirm('立即运行「' + ((item && item.name) || id) + '」？\n\n'
          + (bound ? '会恢复它绑定的 ' + formatCount(bound) + ' 个任务。' : '这条计划没有绑定任务。'))) return;
        schedAction(`/schedules/${encodeURIComponent(id)}/run`, {});
        return;
      }
      if (act === 'enable') { schedAction(`/schedules/${encodeURIComponent(id)}/enable`, {}); return; }
      if (act === 'disable') { schedAction(`/schedules/${encodeURIComponent(id)}/disable`, {}); return; }
      if (act === 'delete') {
        // 删掉的是规则本身；它的运行记录留在时间线里（后端 delete_schedule 只删
        // 计划行），所以这里不必说「记录也会没」。
        if (!window.confirm('删除计划「' + ((item && item.name) || id) + '」？\n\n'
          + '之后这条规则不再放开任务；已经跑过的运行记录会留下。')) return;
        schedAction(`/schedules/${encodeURIComponent(id)}/delete`, {});
      }
    });
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
        ? '这条对话还没有请求记录。'
        : '这条对话还没有请求记录。在「请求重发」发一条，或让模型在对话里用 '
        + 'http_request 打一条，这里就会出现。'));
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
        // 耗时走 `formatDuration`（与作业页、对话里的工具卡同一把尺子）：1200 ms 在这里
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
    tasks: loadTasks,
    schedules: loadSchedules,
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

  window.PentestPanels = { load, prefillRepeater, loadDashboard, loadTasks,
    loadSchedules, loadTraffic, loadApprovals, loadLogs };

  // 「刷新」「清空」这类按钮在这里绑，理由与上面同一条：它们只属于这几页。
  document.addEventListener('DOMContentLoaded', () => {
    const bind = (id, handler) => {
      const node = document.getElementById(id);
      if (node) node.addEventListener('click', handler);
    };
    bind('dashboard-refresh', loadDashboard);
    bind('tasks-refresh', loadTasks);
    // 头部这颗按钮与页脚提示里的按钮做同一件事：把人送回能说那句话的地方。
    // 任务不在这里建，见 taskNotice 的注释。
    bind('tasks-new-chat', () => switchTab('dsh'));
    bind('schedules-refresh', loadSchedules);
    bind('schedules-tick', tickSchedules);
    bindTaskEvents();
    bindSchedEvents();
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
