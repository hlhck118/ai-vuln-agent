'use strict';
/* ============================================================================
 * 设置页签 —— 模型配置（多协议 / 预设 / 连接测试 / 启用）+ 用户管理（仅管理员）
 *
 * 依赖 app.js 暴露的 window.PentestUI（el / apiGet / apiPost / formatTime …）
 * 与 auth.js 暴露的 window.PentestAuth（角色判断、CSRF 由 apiPost 自动带上）。
 * 安全：所有接口文本走 textContent，绝不拼 HTML 字符串；动态行用 class + data-* 定位。
 * ==========================================================================*/

(function () {
  const PROVIDER_LABEL = {
    openai_compat: 'OpenAI 兼容', deepseek: 'DeepSeek 官方',
    anthropic: 'Anthropic', ollama: 'Ollama 原生',
  };
  /**
   * 思考模式的显示名。`''` 表示**不显式下发**，用官方默认（开启、力度 high）——
   * 它与 'enabled' 是两件不同的事："没说"和"要求开"。
   */
  const THINKING_LABEL = { '': '跟随官方默认（开启）', enabled: '开启', disabled: '关闭' };
  const EFFORT_LABEL = { '': '官方默认（high）' };
  /** 只有这个协议认 `reasoning_effort` / `thinking`。 */
  const DEEPSEEK_PROVIDER = 'deepseek';
  const KEY_SOURCE_LABEL = { env: '环境变量', file: '文件', inline: '直接填写', none: '不使用密钥' };
  const ROLE_LABEL = { admin: '管理员', operator: '操作员' };
  const MIN_PASSWORD_LENGTH = 10;
  /** 模型连通性测试比普通接口慢：给 120 秒预算，不占用页面的 15 秒请求预算。 */
  const TEST_TIMEOUT_MS = 120000;

  const view = {
    profiles: [], presets: [], activeId: null, modelsError: null,
    modes: [], activeModeId: '', modeError: null, modeBusy: null,
    users: [], usersError: null,
    editingId: null, pendingDelete: null, pendingUserDelete: null, pendingPasswordUser: null,
    tests: {}, inflight: null, loaded: false, bound: false,
    // 设置分组。默认落在「外观」——五组里只有它既与作业无关、也与权限无关，
    // 是唯一一项使用者会反复回来找、且第一次打开设置就该看见的。四组管理项
    // 都是"改了会影响别人"的配置，本来就该是有意去找的。
    group: 'appearance',
    // 权限档 / DSH 集成 / 插件市场
    policies: [], activePolicyId: '', policyError: null, policyBusy: null,
    // 工具审批（审查档位）。`null` 表示还没拉到，界面按"尚未加载"画。
    review: null, reviewError: null,
    dsh: null, dshError: null,
    // DSH 运行时通道：**部署口径**，只在设置页的 DSH 分组里出现，
    // 且保存按钮对非管理员禁用。DSH 集成只有这一条路（见 index.html 的说明）。
    dshRuntime: null, dshPurge: null,
    hub: { query: '', category: '', verified: false, offset: 0, items: [],
           total: 0, hasMore: false, error: null, stale: false, busy: false },
    hubInstalled: null, hubOps: [], hubDiag: null, hubPending: 0,
    // 安装队列是后台串行跑的，界面要主动跟进（见 syncOpsPolling）。
    opsTimer: null, announcedOps: null, knownAgentIds: null,
    // 自定义 Agent 预设的编辑状态。`agentEditingId` 非空 = 表单在改一条已有的；
    // `agentHandleTouched` 记录用户是否手动碰过标识——没碰过才跟着名称自动变，
    // 碰过之后再自动改会把他刚输的东西冲掉。
    agentEditingId: null, agentBusy: null, pendingAgentDelete: null,
    agentHandleTouched: false, skillChoices: null,
    // 外部 MCP 服务。`mcp` 是 GET /mcp 的原样返回（卡片、档位下拉、工具名都从
    // 那一份读，不在前端另算一遍——两边各算一次就会对不上）。
    mcp: null, mcpError: null, mcpBusy: null, mcpEditingId: null,
  };

  let dom = {};
  const ui = () => window.PentestUI;
  const byId = (id) => document.getElementById(id);
  /** 复用 app.js 的 DOM 工具（走命名空间，不依赖跨脚本的全局词法作用域）。 */
  const el = (tag, className, text) => window.PentestUI.el(tag, className, text);
  const clear = (node) => window.PentestUI.clear(node);
  const asArray = (value) => window.PentestUI.asArray(value);

  function cacheDom() {
    dom = {
      tab: document.querySelector('.tab[data-tab="settings"]'),
      meta: byId('settings-meta'), refresh: byId('settings-refresh'),
      list: byId('models-list'), form: byId('model-form'), formTitle: byId('model-form-title'),
      formMessage: byId('model-form-message'), submit: byId('model-submit'), cancel: byId('model-cancel'),
      preset: byId('model-preset'), presetHint: byId('model-preset-hint'),
      name: byId('model-name'), provider: byId('model-provider'), baseUrl: byId('model-base-url'),
      model: byId('model-model'), keySource: byId('model-key-source'), keyHint: byId('model-key-hint'),
      keyRefRow: byId('model-key-ref-row'), keyRefLabel: byId('model-key-ref-label'), keyRef: byId('model-key-ref'),
      keyInlineRow: byId('model-key-inline-row'), keyInline: byId('model-key-inline'),
      keyWarning: byId('model-key-warning'),
      temperature: byId('model-temperature'), maxTokens: byId('model-max-tokens'),
      timeout: byId('model-timeout'), note: byId('model-note'),
      thinking: byId('model-thinking'), reasoningEffort: byId('model-reasoning-effort'),
      thinkingRow: byId('model-thinking-row'), thinkingHint: byId('model-thinking-hint'),
      modeList: byId('agent-modes-list'), modeCurrent: byId('agent-mode-current'),
      modeNote: byId('agent-mode-note'), modeMessage: byId('agent-mode-message'),
      agentNew: byId('agent-new'), agentForm: byId('agent-form'),
      agentFormTitle: byId('agent-form-title'), agentFormScope: byId('agent-form-scope'),
      agentLabel: byId('agent-label'),
      agentHandle: byId('agent-handle'), agentHandlePrefix: byId('agent-handle-prefix'),
      agentSummary: byId('agent-summary'), agentNote: byId('agent-note'),
      agentSteps: byId('agent-steps'), agentSkills: byId('agent-skills'),
      agentPrompt: byId('agent-prompt'),
      agentPromptCount: byId('agent-prompt-count'),
      agentFormMessage: byId('agent-form-message'), agentSubmit: byId('agent-submit'),
      agentCancel: byId('agent-cancel'),
      nav: byId('settings-nav'),
      themeGrid: byId('theme-grid'), themeCurrent: byId('theme-current'),
      themeNote: byId('theme-note'),
      accentRow: byId('accent-row'), accentNote: byId('accent-note'),
      frostRow: byId('frost-row'), frostNote: byId('frost-note'),
      alphaRange: byId('alpha-range'), alphaValue: byId('alpha-value'),
      alphaNote: byId('alpha-note'),
      policyList: byId('policy-list'), policyCurrent: byId('policy-current'),
      policyNote: byId('policy-note'), policyMessage: byId('policy-message'),
      hubMeta: byId('hub-meta'), hubQuery: byId('hub-query'),
      hubCategory: byId('hub-category'), hubVerified: byId('hub-verified'),
      hubSearch: byId('hub-search'), hubMessage: byId('hub-message'),
      hubResults: byId('hub-results'), hubMore: byId('hub-more'),
      hubMoreRow: byId('hub-more-row'), hubInstalled: byId('hub-installed'),
      hubOps: byId('hub-ops'), hubDiag: byId('hub-diag'),
      hubOpsClear: byId('hub-ops-clear'), hubOpsHint: byId('hub-ops-hint'),
      hubDiagResult: byId('hub-diag-result'),
      hubInstalledRefresh: byId('hub-installed-refresh'),
    dshRuntimeMode: byId('dsh-runtime-mode'),
    dshRuntimeHint: byId('dsh-runtime-hint'),
    dshRuntimeState: byId('dsh-runtime-state'),
    dshRuntimeSave: byId('dsh-runtime-save'),
    scanEngineMode: byId('scan-engine-mode'),
    scanEngineHint: byId('scan-engine-hint'),
    scanEngineState: byId('scan-engine-state'),
    scanEngineSave: byId('scan-engine-save'),
    scanEngineMessage: byId('scan-engine-message'),
    reviewMode: byId('review-mode'), reviewHint: byId('review-hint'),
    reviewState: byId('review-state'), reviewSave: byId('review-save'),
    reviewWarning: byId('review-warning'), reviewMessage: byId('review-message'),
    purgeEnabled: byId('purge-enabled'),
    purgeRetry: byId('purge-retry'),
    purgeContinue: byId('purge-continue'),
    purgeContinueText: byId('purge-continue-text'),
    purgeInject: byId('purge-inject'),
    purgeState: byId('purge-state'),
    purgeMessage: byId('purge-message'),
    purgeSave: byId('purge-save'),
    dshMessage: byId('dsh-message'),
      usersCard: byId('users-card'), usersList: byId('users-list'), userForm: byId('user-form'),
      userMessage: byId('user-form-message'), userSubmit: byId('user-submit'),
      userUsername: byId('user-new-username'), userPassword: byId('user-new-password'), userRole: byId('user-new-role'),
      mcpMeta: byId('mcp-meta'), mcpNote: byId('mcp-note'), mcpRefresh: byId('mcp-refresh'),
      mcpMessage: byId('mcp-message'), mcpList: byId('mcp-list'),
      mcpForm: byId('mcp-form'), mcpFormTitle: byId('mcp-form-title'),
      mcpId: byId('mcp-id'), mcpLabel: byId('mcp-label'),
      mcpTransport: byId('mcp-transport'),
      mcpStdioRow: byId('mcp-stdio-row'), mcpHttpRow: byId('mcp-http-row'),
      mcpArgsRow: byId('mcp-args-row'), mcpEnvRow: byId('mcp-env-row'),
      mcpCommand: byId('mcp-command'), mcpArgs: byId('mcp-args'), mcpUrl: byId('mcp-url'),
      mcpLevel: byId('mcp-level'), mcpTimeout: byId('mcp-timeout'),
      mcpEnabled: byId('mcp-enabled'), mcpReview: byId('mcp-review'),
      mcpEnv: byId('mcp-env'), mcpNoteField: byId('mcp-note-field'),
      mcpSubmit: byId('mcp-submit'), mcpCancel: byId('mcp-cancel'),
      mcpFormMessage: byId('mcp-form-message'),
    };
  }

  /* ------------------------------------------------------------ 小工具 ---- */

  function showNotice(node, text, kind) {
    if (!node) return;
    node.textContent = text ? String(text) : '';
    node.className = 'notice' + (kind ? ' ' + kind : '');
    node.classList.toggle('hidden', !text);
  }

  function setButtonBusy(button, busy, busyText) {
    if (!button) return;
    if (busy) {
      if (button.dataset.idleText === undefined) button.dataset.idleText = button.textContent;
      button.textContent = busyText || '处理中…';
      button.disabled = true;
    } else {
      if (button.dataset.idleText !== undefined) button.textContent = button.dataset.idleText;
      button.disabled = false;
    }
  }

  function numberOrNull(value) {
    const text = String(value === undefined || value === null ? '' : value).trim();
    if (!text) return null;
    const num = Number(text);
    return Number.isFinite(num) ? num : null;
  }

  const providerLabel = (value) => PROVIDER_LABEL[value] || value || '未知协议';
  const findProfile = (id) => view.profiles.find((item) => String(item.id) === String(id)) || null;
  const findUser = (id) => view.users.find((item) => String(item.id) === String(id)) || null;
  const fmtTime = (value) => ui().formatTime(value);

  function paramText(profile) {
    const parts = [];
    if (profile.temperature !== null && profile.temperature !== undefined) parts.push(`temperature ${profile.temperature}`);
    if (profile.maxTokens) parts.push(`maxTokens ${profile.maxTokens}`);
    if (profile.timeoutS) parts.push(`timeoutS ${profile.timeoutS}`);
    // 思考模式只对 DeepSeek 有意义。没配过就不显示——空串是"跟随官方默认"，
    // 把那句写进摘要会让每一条 DeepSeek 配置都挂一行没有信息量的字。
    if (String(profile.provider || '') === DEEPSEEK_PROVIDER) {
      const thinking = String(profile.thinking || '');
      const effort = String(profile.reasoningEffort || '');
      if (thinking || effort) {
        const bits = [];
        if (thinking) bits.push(THINKING_LABEL[thinking] || thinking);
        if (effort) bits.push(`effort ${EFFORT_LABEL[effort] || effort}`);
        parts.push(`思考 ${bits.join(' · ')}`);
      }
    }
    return parts.length ? parts.join(' · ') : '使用后端默认值';
  }

  /**
   * 思考模式的两个字段只对 DeepSeek 官方协议有效。
   *
   * 隐藏而不是禁用：一条 OpenAI 兼容配置看到两个灰着的下拉框，只会以为
   * 自己少配了什么。切换协议时同步隐藏，避免"填了值但发不出去"的静默失配。
   */
  function applyProviderVisibility() {
    const isDeepseek = String(dom.provider.value || '') === DEEPSEEK_PROVIDER;
    if (dom.thinkingRow) dom.thinkingRow.classList.toggle('hidden', !isDeepseek);
    if (dom.thinkingHint) dom.thinkingHint.classList.toggle('hidden', !isDeepseek);
    if (!isDeepseek) {
      // 切走时清空，防止值留在 DOM 里被下一次收集带上去。它不会被后端接受
      // （协议不是 deepseek 时这些字段无人读取），但留着会让"编辑另一条配置"
      // 时看到一个不属于它的值。
      if (dom.thinking) dom.thinking.value = '';
      if (dom.reasoningEffort) dom.reasoningEffort.value = '';
    }
  }

  function keySourceText(profile) {
    const source = String(profile.apiKeySource || 'none');
    if (source === 'inline') return profile.hasInlineKey ? '直接填写（明文保存）' : '直接填写（尚未填写）';
    if (source === 'env' || source === 'file') return `${KEY_SOURCE_LABEL[source]} ${profile.apiKeyRef || '（未指定）'}`;
    return '不使用密钥';
  }

  /* -------------------------------------------------------- 模型配置渲染 ---- */

  function metaRow(label, value, className) {
    const row = el('div', 'model-meta-row');
    row.append(el('span', 'model-meta-label', label));
    row.append(el('span', 'model-meta-value ' + (className || ''), value));
    return row;
  }

  function actionButton(label, action, id, className) {
    const button = el('button', `btn sm ${className || ''}`.trim(), label);
    button.type = 'button';
    button.dataset.action = action;
    button.dataset.id = String(id);
    return button;
  }

  function testLine(profile) {
    const cached = view.tests[profile.id];
    const result = cached || profile.lastTest || null;
    const line = el('div', 'model-test');
    if (!result) { line.classList.add('idle'); line.textContent = '尚未测试连接'; return line; }
    if (result.pending) { line.classList.add('pending'); line.textContent = '正在测试连接…'; return line; }
    if (result.ok) {
      line.classList.add('ok');
      line.append(el('span', 'test-badge ok', '成功'));
      const latency = (result.latencyMs === undefined || result.latencyMs === null) ? '' : `${result.latencyMs} ms · `;
      line.append(el('span', 'test-text', `${latency}${result.message || '连接正常'}`));
      if (result.sample) line.append(el('pre', 'test-sample', ui().truncate(String(result.sample), 240)));
      // 连通性警告与"成功"是两件事，要同时看见：连上了，但某个参数组合会在
      // 后面咬人（比如思考模式开着而 maxTokens 偏小，回答会空）。
      // 只挂在成功分支里——失败时用户该先解决失败。
      if (result.warning) line.append(el('p', 'test-warning', String(result.warning)));
    } else {
      line.classList.add('err');
      line.append(el('span', 'test-badge err', '失败'));
      line.append(el('span', 'test-text', result.message || '连接失败'));
    }
    if (result.at) line.append(el('span', 'test-time', fmtTime(result.at)));
    return line;
  }

  function deleteControls(profile) {
    const wrap = el('span', 'model-delete');
    if (String(view.pendingDelete) === String(profile.id)) {
      wrap.append(el('span', 'muted', '确认删除该配置？'));
      wrap.append(actionButton('确认删除', 'delete-confirm', profile.id, 'danger'));
      wrap.append(actionButton('取消', 'delete-cancel', profile.id));
    } else {
      wrap.append(actionButton('删除', 'delete', profile.id, 'danger'));
    }
    return wrap;
  }

  function profileCard(profile) {
    const active = String(view.activeId) === String(profile.id) || profile.active === true;
    const card = el('article', 'model-card' + (active ? ' active' : ''));
    card.dataset.id = String(profile.id);

    const head = el('div', 'model-head');
    head.append(el('span', 'model-name', profile.name || profile.id));
    head.append(el('span', 'tag', providerLabel(profile.provider)));
    if (active) head.append(el('span', 'status-badge status-online', '当前启用'));
    head.append(el('span', 'spacer'));
    head.append(el('span', 'row-id', profile.id));
    card.append(head);

    const grid = el('div', 'model-meta');
    grid.append(metaRow('Base URL', profile.baseUrl || '（未填写）', 'mono'));
    grid.append(metaRow('模型', profile.model || '（未填写）', 'mono'));
    grid.append(metaRow('密钥来源', keySourceText(profile),
      String(profile.apiKeySource) === 'inline' ? 'warn' : ''));
    grid.append(metaRow('参数', paramText(profile)));
    if (profile.note) grid.append(metaRow('备注', profile.note));
    grid.append(metaRow('更新时间', fmtTime(profile.updatedAt)));
    card.append(grid);

    card.append(testLine(profile));

    const actions = el('div', 'model-actions');
    const testing = Boolean(view.tests[profile.id] && view.tests[profile.id].pending);
    const testButton = actionButton(testing ? '测试中…' : '测试连接', 'test', profile.id);
    testButton.disabled = testing;
    actions.append(testButton);
    const activateButton = actionButton(active ? '已启用' : '设为启用', 'activate', profile.id);
    activateButton.disabled = active;
    actions.append(activateButton);
    actions.append(actionButton('编辑', 'edit', profile.id));
    actions.append(deleteControls(profile));
    card.append(actions);
    return card;
  }

  function renderModels() {
    if (!dom.list) return;
    clear(dom.list);
    if (view.modelsError) {
      dom.list.append(ui().errorBlock('模型配置加载失败：' + view.modelsError));
      return;
    }
    if (!view.profiles.length) {
      dom.list.append(ui().emptyBlock('还没有模型配置。从下方「服务商预设」选一个填入端点，保存后即可用于扫描。'));
      return;
    }
    view.profiles.forEach((profile) => dom.list.append(profileCard(profile)));
  }

  function updateMeta() {
    if (!dom.meta) return;
    if (view.modelsError) { dom.meta.textContent = '加载失败'; return; }
    const active = findProfile(view.activeId);
    const readonly = !(window.PentestAuth && window.PentestAuth.isAdmin());
    dom.meta.textContent = `共 ${view.profiles.length} 个配置`
      + (active ? ` · 当前启用：${active.name || active.id}` : ' · 未启用任何配置')
      + (readonly ? ' · 当前账号只读（修改模型需要管理员）' : '');
  }

  /** 口令预检：优先用 auth.js 的同规则实现，保证与后端 password_problem 一致。 */
  function passwordProblemOf(password) {
    const auth = window.PentestAuth;
    if (auth && typeof auth.passwordProblem === 'function') return auth.passwordProblem(password);
    return String(password || '').length < MIN_PASSWORD_LENGTH ? `密码至少 ${MIN_PASSWORD_LENGTH} 位` : '';
  }

  /* -------------------------------------------------------- 模型配置交互 ---- */

  function fillPresetOptions() {
    if (!dom.preset) return;
    clear(dom.preset);
    dom.preset.append(new Option('（自定义 / 不套用预设）', ''));
    view.presets.forEach((preset) => {
      dom.preset.append(new Option(preset.label || preset.id, preset.id));
    });
  }

  function applyPreset() {
    const preset = view.presets.find((item) => String(item.id) === String(dom.preset.value));
    if (!preset) { dom.presetHint.textContent = '选中预设会自动填入协议、端点与模型名'; return; }
    if (preset.provider) dom.provider.value = preset.provider;
    if (preset.baseUrl) dom.baseUrl.value = preset.baseUrl;
    const models = Array.isArray(preset.models) ? preset.models : [];
    if (models.length) dom.model.value = models[0];
    if (!dom.name.value) dom.name.value = preset.label || preset.id;
    if (preset.note && !dom.note.value) dom.note.value = preset.note;
    const hints = [preset.label || preset.id, preset.apiKeySourceHint || ''].filter(Boolean);
    if (preset.scanCapable === false) hints.push('后端标记为不可用于扫描');
    dom.presetHint.textContent = hints.join(' · ');
    applyKeySource();
    applyProviderVisibility();
  }

  function applyKeySource() {
    const source = String(dom.keySource.value || 'env');
    const isInline = source === 'inline';
    const needsRef = source === 'env' || source === 'file';
    dom.keyRefRow.classList.toggle('hidden', !needsRef);
    dom.keyInlineRow.classList.toggle('hidden', !isInline);
    dom.keyWarning.classList.toggle('hidden', !isInline);
    dom.keyHint.textContent = isInline
      ? '明文保存在控制面数据库'
      : (needsRef ? (source === 'env' ? '填写环境变量名' : '填写包含密钥的文件路径') : '该端点不需要密钥');
    dom.keyRefLabel.textContent = source === 'file' ? '密钥文件路径' : '环境变量名';
    dom.keyRef.placeholder = source === 'file' ? '例如 /etc/pentest/deepseek.key' : '例如 DEEPSEEK_API_KEY';
  }

  function resetModelForm() {
    view.editingId = null;
    dom.form.reset();
    dom.preset.value = '';
    dom.provider.value = 'openai_compat';
    dom.keySource.value = 'env';
    dom.presetHint.textContent = '选中预设会自动填入协议、端点与模型名';
    dom.keyInline.placeholder = '直接填写密钥，明文保存';
    dom.formTitle.textContent = '新建模型配置';
    dom.cancel.classList.add('hidden');
    dom.submit.textContent = '保存配置';
    showNotice(dom.formMessage, '');
    applyKeySource();
    applyProviderVisibility();
  }

  function startEdit(id) {
    const profile = findProfile(id);
    if (!profile) return;
    view.editingId = profile.id;
    dom.name.value = profile.name || '';
    dom.provider.value = profile.provider || 'openai_compat';
    dom.baseUrl.value = profile.baseUrl || '';
    dom.model.value = profile.model || '';
    dom.keySource.value = profile.apiKeySource || 'none';
    dom.keyRef.value = profile.apiKeyRef || '';
    dom.keyInline.value = '';
    dom.keyInline.placeholder = profile.hasInlineKey ? '留空表示保留原密钥' : '直接填写密钥，明文保存';
    dom.temperature.value = profile.temperature === null || profile.temperature === undefined ? '' : profile.temperature;
    dom.maxTokens.value = profile.maxTokens || '';
    dom.timeout.value = profile.timeoutS || '';
    dom.note.value = profile.note || '';
    if (dom.thinking) dom.thinking.value = String(profile.thinking || '');
    if (dom.reasoningEffort) dom.reasoningEffort.value = String(profile.reasoningEffort || '');
    dom.preset.value = '';
    dom.formTitle.textContent = `编辑配置：${profile.name || profile.id}`;
    dom.cancel.classList.remove('hidden');
    dom.submit.textContent = '保存修改';
    showNotice(dom.formMessage, '');
    applyKeySource();
    applyProviderVisibility();
    dom.form.scrollIntoView({ block: 'nearest' });
  }

  function collectModelPayload() {
    const source = String(dom.keySource.value || 'env');
    const provider = String(dom.provider.value || 'openai_compat');
    const isDeepseek = provider === DEEPSEEK_PROVIDER;
    return {
      name: String(dom.name.value || '').trim(),
      provider,
      baseUrl: String(dom.baseUrl.value || '').trim(),
      model: String(dom.model.value || '').trim(),
      apiKeySource: source,
      apiKeyRef: (source === 'env' || source === 'file') ? String(dom.keyRef.value || '').trim() : '',
      apiKey: source === 'inline' ? String(dom.keyInline.value || '') : '',
      temperature: numberOrNull(dom.temperature.value),
      maxTokens: numberOrNull(dom.maxTokens.value),
      timeoutS: numberOrNull(dom.timeout.value),
      // 思考模式两项只在 DeepSeek 官方协议下收集。别的协议下它们没有含义，
      // 带上只会往库里存一个永远不会被读取的值——下次有人查库时会以为它生效。
      thinking: isDeepseek ? String(dom.thinking.value || '') : '',
      reasoningEffort: isDeepseek ? String(dom.reasoningEffort.value || '') : '',
      note: String(dom.note.value || '').trim(),
    };
  }

  function validateModel(payload) {
    if (!payload.name) return '请填写配置名称';
    if (!payload.baseUrl) return '请填写 Base URL';
    if (!payload.model) return '请填写模型名';
    if (payload.apiKeySource === 'env' && !payload.apiKeyRef) return '请填写环境变量名';
    if (payload.apiKeySource === 'file' && !payload.apiKeyRef) return '请填写密钥文件路径';
    if (payload.apiKeySource === 'inline' && !view.editingId && !payload.apiKey) return '请填写 API Key（或改用环境变量 / 文件）';
    if (payload.temperature !== null && (payload.temperature < 0 || payload.temperature > 2)) return 'temperature 需要介于 0 与 2 之间';
    if (payload.maxTokens !== null && payload.maxTokens < 1) return 'maxTokens 至少为 1';
    if (payload.timeoutS !== null && payload.timeoutS < 1) return 'timeoutS 至少为 1 秒';
    // 后端也会拒，但在这里拦一句能给到具体的取值集合。下拉框本身限制了取值，
    // 这条是**防御**：DOM 可以被手工改，或者将来换成一个自由输入框。
    if (payload.reasoningEffort && !/^(none|low|high|max)$/.test(payload.reasoningEffort)) {
      return 'reasoning_effort 只能是 none / low / high / max';
    }
    return null;
  }

  async function submitModel(event) {
    event.preventDefault();
    const payload = collectModelPayload();
    const problem = validateModel(payload);
    if (problem) { showNotice(dom.formMessage, problem, 'error'); return; }
    const previous = view.editingId ? findProfile(view.editingId) : null;
    ['temperature', 'maxTokens', 'timeoutS'].forEach((key) => {
      if (payload[key] !== null) return;
      if (previous && previous[key] !== null && previous[key] !== undefined) payload[key] = previous[key];
      else delete payload[key];                     // 新建时留空 → 交给后端默认值
    });
    setButtonBusy(dom.submit, true, '保存中…');
    showNotice(dom.formMessage, '');
    try {
      if (view.editingId) await ui().apiPost(`/models/${encodeURIComponent(view.editingId)}`, payload);
      else await ui().apiPost('/models', payload);
      showNotice(dom.formMessage, '配置已保存。', 'ok');
      resetModelForm();
      await load(true);
    } catch (error) {
      showNotice(dom.formMessage, '保存失败：' + error.message, 'error');
    } finally {
      setButtonBusy(dom.submit, false);
    }
  }

  async function testProfile(id) {
    view.tests[id] = { pending: true };
    renderModels();
    let result;
    try {
      const data = await ui().apiPost(`/models/${encodeURIComponent(id)}/test`, {}, undefined, TEST_TIMEOUT_MS);
      result = {
        ok: data.ok !== false,
        latencyMs: data.latencyMs,
        message: data.message || (data.ok === false ? '连接失败' : '连接正常'),
        sample: data.sample,
        at: Date.now() / 1000,
      };
    } catch (error) {
      result = { ok: false, message: '测试请求失败：' + error.message, at: Date.now() / 1000 };
    }
    view.tests[id] = result;
    const profile = findProfile(id);
    if (profile) profile.lastTest = result;
    renderModels();
  }

  async function activateProfile(id) {
    try {
      const data = await ui().apiPost(`/models/${encodeURIComponent(id)}/activate`, {});
      view.activeId = data && data.activeId !== undefined ? data.activeId : id;
      view.profiles.forEach((profile) => { profile.active = String(profile.id) === String(view.activeId); });
      renderModels();
      updateMeta();
    } catch (error) {
      ui().showError('启用配置失败：' + error.message);
    }
  }

  async function confirmDeleteProfile(id) {
    view.pendingDelete = null;
    try {
      await ui().apiPost(`/models/${encodeURIComponent(id)}/delete`, {});
      if (String(view.editingId) === String(id)) resetModelForm();
      await load(true);
    } catch (error) {
      ui().showError('删除配置失败：' + error.message);
      renderModels();
    }
  }

  function bindModelEvents() {
    dom.list.addEventListener('click', (event) => {
      const button = event.target.closest ? event.target.closest('[data-action]') : null;
      if (!button) return;
      const id = button.dataset.id, action = button.dataset.action;
      if (action === 'test') testProfile(id);
      else if (action === 'activate') activateProfile(id);
      else if (action === 'edit') startEdit(id);
      else if (action === 'delete') { view.pendingDelete = id; renderModels(); }
      else if (action === 'delete-cancel') { view.pendingDelete = null; renderModels(); }
      else if (action === 'delete-confirm') confirmDeleteProfile(id);
    });
    dom.form.addEventListener('submit', submitModel);
    dom.cancel.addEventListener('click', resetModelForm);
    dom.preset.addEventListener('change', applyPreset);
    dom.keySource.addEventListener('change', applyKeySource);
    // 协议决定思考模式那两个字段是否出现。绑在 change 上而不是每次渲染时算：
    // 使用者刚把协议改成 DeepSeek，就应该立刻看到多出来的两个下拉框。
    dom.provider.addEventListener('change', applyProviderVisibility);
    dom.refresh.addEventListener('click', () => { ui().hideError(); load(true); });
  }

  /* ---------------------------------------------------------- 用户管理 ---- */

  function userRoleText(role) {
    const key = String(role || '').toLowerCase();
    return ROLE_LABEL[key] || role || '未知';
  }

  function userActionCell(user) {
    const cell = el('td', 'user-actions');
    if (String(view.pendingUserDelete) === String(user.id)) {
      cell.append(el('span', 'muted', '确认删除？'));
      cell.append(actionButton('确认删除', 'user-delete-confirm', user.id, 'danger'));
      cell.append(actionButton('取消', 'user-delete-cancel', user.id));
      return cell;
    }
    cell.append(actionButton('重置密码', 'user-password', user.id));
    cell.append(actionButton(user.disabled ? '启用' : '停用', 'user-toggle', user.id));
    cell.append(actionButton('删除', 'user-delete', user.id, 'danger'));
    return cell;
  }

  function userRow(user) {
    const row = el('tr', user.disabled ? 'user-row disabled' : 'user-row');
    row.dataset.id = String(user.id);
    row.append(el('td', 'user-name', user.username || user.id));
    row.append(el('td', null, userRoleText(user.role)));
    row.append(el('td', 'mono', fmtTime(user.createdAt)));
    row.append(el('td', 'mono', fmtTime(user.lastLoginAt)));
    const status = el('td');
    status.append(el('span', user.disabled ? 'status-badge status-failed' : 'status-badge status-online',
      user.disabled ? '已停用' : '启用中'));
    row.append(status);
    row.append(userActionCell(user));
    return row;
  }

  function userPasswordRow(user) {
    const row = el('tr', 'user-password-row');
    const cell = el('td');
    cell.colSpan = 6;
    const box = el('div', 'inline-reset');
    box.append(el('span', 'muted', `为 ${user.username || user.id} 设置新密码（至少 ${MIN_PASSWORD_LENGTH} 位）：`));
    const input = el('input', 'input user-password-input');
    input.type = 'password';
    input.autocomplete = 'new-password';
    input.placeholder = '新密码';
    box.append(input);
    const save = actionButton('保存', 'user-password-save', user.id);
    const cancel = actionButton('取消', 'user-password-cancel', user.id);
    box.append(save, cancel);
    cell.append(box);
    row.append(cell);
    return row;
  }

  /** 用户管理卡片的可见性：**分组与角色两个条件缺一不可**。
   *
   *  单独在 `renderUsers` 里按角色切 `.hidden` 是不行的：`loadUsers` 是异步的，
   *  它会晚于 `showGroup` 完成，于是"切到插件市场 → 点刷新 → 用户管理卡片冒出来"
   *  ——卡片浮在另一组的下面，而两处都在改同一个 class，谁后跑谁赢。
   *  抽成一个函数，让"可见"只有一个判定点。
   */
  function applyUsersVisibility() {
    if (!dom.usersCard) return;
    const admin = !!(window.PentestAuth && window.PentestAuth.isAdmin());
    const wanted = admin && view.group === 'users';
    dom.usersCard.classList.toggle('hidden', !wanted);
  }

  function renderUsers() {
    if (!dom.usersList) return;
    const admin = window.PentestAuth ? window.PentestAuth.isAdmin() : false;
    applyUsersVisibility();
    if (!admin) { clear(dom.usersList); return; }
    clear(dom.usersList);
    if (view.usersError) { dom.usersList.append(ui().errorBlock('用户列表加载失败：' + view.usersError)); return; }
    if (!view.users.length) { dom.usersList.append(ui().emptyBlock('还没有其他用户，用下方表单创建操作员账号。')); return; }
    const table = el('table', 'user-table');
    const head = el('thead');
    const headRow = el('tr');
    ['用户名', '角色', '创建时间', '最后登录', '状态', '操作'].forEach((label) => headRow.append(el('th', null, label)));
    head.append(headRow);
    table.append(head);
    const body = el('tbody');
    view.users.forEach((user) => {
      body.append(userRow(user));
      if (String(view.pendingPasswordUser) === String(user.id)) body.append(userPasswordRow(user));
    });
    table.append(body);
    dom.usersList.append(table);
  }

  async function toggleUser(user, button) {
    setButtonBusy(button, true, '处理中…');
    try {
      const data = await ui().apiPost(`/users/${encodeURIComponent(user.id)}/disable`, { disabled: !user.disabled });
      const updated = (data && data.user) || null;
      if (updated) Object.assign(user, updated); else user.disabled = !user.disabled;
      renderUsers();
    } catch (error) {
      ui().showError('更新用户状态失败：' + error.message);
      setButtonBusy(button, false);
    }
  }

  async function deleteUser(id) {
    view.pendingUserDelete = null;
    try {
      await ui().apiPost(`/users/${encodeURIComponent(id)}/delete`, {});
      await loadUsers(true);
    } catch (error) {
      ui().showError('删除用户失败：' + error.message);
      renderUsers();
    }
  }

  async function saveUserPassword(id, input, button) {
    const password = String((input && input.value) || '');
    const problem = passwordProblemOf(password);
    if (problem) {
      showNotice(dom.userMessage, problem, 'error');
      return;
    }
    setButtonBusy(button, true, '保存中…');
    try {
      await ui().apiPost(`/users/${encodeURIComponent(id)}/password`, { password });
      view.pendingPasswordUser = null;
      showNotice(dom.userMessage, '密码已重置。', 'ok');
      renderUsers();
    } catch (error) {
      showNotice(dom.userMessage, '重置密码失败：' + error.message, 'error');
      setButtonBusy(button, false);
    }
  }

  function bindUserEvents() {
    dom.usersList.addEventListener('click', (event) => {
      const button = event.target.closest ? event.target.closest('[data-action]') : null;
      if (!button) return;
      const id = button.dataset.id, action = button.dataset.action;
      const user = findUser(id);
      if (!user) return;
      if (action === 'user-password') { view.pendingPasswordUser = id; view.pendingUserDelete = null; renderUsers(); }
      else if (action === 'user-password-cancel') { view.pendingPasswordUser = null; renderUsers(); }
      else if (action === 'user-password-save') {
        const input = dom.usersList.querySelector('.user-password-row .user-password-input');
        saveUserPassword(id, input, button);
      } else if (action === 'user-toggle') toggleUser(user, button);
      else if (action === 'user-delete') { view.pendingUserDelete = id; renderUsers(); }
      else if (action === 'user-delete-cancel') { view.pendingUserDelete = null; renderUsers(); }
      else if (action === 'user-delete-confirm') deleteUser(id);
    });
    dom.userForm.addEventListener('submit', submitUser);
  }

  async function submitUser(event) {
    event.preventDefault();
    const username = String(dom.userUsername.value || '').trim();
    const password = String(dom.userPassword.value || '');
    const role = String(dom.userRole.value || 'operator');
    if (!username) { showNotice(dom.userMessage, '请填写用户名', 'error'); return; }
    const problem = passwordProblemOf(password);
    if (problem) { showNotice(dom.userMessage, problem, 'error'); return; }
    setButtonBusy(dom.userSubmit, true, '创建中…');
    showNotice(dom.userMessage, '');
    try {
      await ui().apiPost('/users', { username, password, role });
      dom.userForm.reset();
      showNotice(dom.userMessage, '用户已创建。', 'ok');
      await loadUsers(true);
    } catch (error) {
      showNotice(dom.userMessage, '创建失败：' + error.message, 'error');
    } finally {
      setButtonBusy(dom.userSubmit, false);
    }
  }

  /* --------------------------------------------------------- Agent 预设 ---- */

  /* 这里原来有一行 `const AGENT_STEPS_MAX = 40;`：**定义完从没被用过**，而数字早就不
     对了——平台上限后来抬到 99999（`agents.MAX_STEPS`），它没跟。删掉而不是改成
     99999：改一次只是把同一次漂移推迟到下一次改上限，而下一个人会照它去校验，然后
     得出"预设最多 40 步"这个结论。每一档自己的预算在 `/agents` 的目录里（`steps`），
     顶格的判断由服务端给（见 `chat.steps_at_ceiling`）。 */

  /** Agent 目录：内置、自定义、DSH 插件三个来源合成的一条列表。 */
  async function loadAgentModes() {
    try {
      // 走 `/agents` 而不是旧的 `/agent-modes`：前者是本平台 Agent 目录的规范名字
      // （内置 + 自定义 + DSH 插件），`/agent-modes` 保留为兼容别名。
      const data = await ui().apiGet('/agents');
      view.modes = Array.isArray(data.agents) ? data.agents : [];
      view.activeModeId = data.activeId || '';
      view.modeError = null;
      // 这个接口顺带回一份 DSH 快照。**要立刻重画**已安装列表：视图里的
      // `view.dsh` 刚被换掉，而读到它的渲染函数只有一个，不重画就是"数据新了、
      // 界面还是旧的"。以前这一行的落点是那块集成状态，它去掉之后落点就是这里。
      if (data.dsh) { view.dsh = data.dsh; renderHubInstalled(null); }
    } catch (error) {
      view.modes = []; view.activeModeId = ''; view.modeError = error.message;
    }
    renderAgentModes();
  }

  function sourceBadge(mode) {
    if (mode.custom) return el('span', 'tag tag-custom', '自定义');
    if (mode.builtin) return el('span', 'tag', '内置');
    return el('span', 'tag tag-plugin', '插件');
  }

  function renderAgentModes() {
    if (!dom.modeList) return;
    clear(dom.modeList);
    const admin = !!(window.PentestAuth && window.PentestAuth.isAdmin());
    const active = view.modes.find((mode) => mode.id === view.activeModeId) || null;
    if (dom.modeCurrent) {
      dom.modeCurrent.textContent = active ? active.label
        : (view.modeError ? '加载失败' : '尚未加载');
    }
    // 「新建预设」对非管理员直接不出现，而不是留一个点不动、也不说为什么的按钮。
    if (dom.agentNew) dom.agentNew.classList.toggle('hidden', !admin);
    if (!view.modes.length) {
      dom.modeList.append(ui().emptyBlock(view.modeError
        ? 'Agent 预设加载失败：' + view.modeError : '登录后加载 Agent 预设。'));
      return;
    }
    view.modes.forEach((mode) => dom.modeList.append(modeCard(mode, admin)));
    if (dom.modeNote) {
      dom.modeNote.textContent = '预设决定产出口径与单轮步数预算，不改变探测作用域、'
        + '工具集与审计记录。'
        + (admin ? '点「设为默认」切换全局默认，下一轮对话生效；自定义的那几条可以'
                 + '就地编辑、删除，或从任意一条复制出新的一份。'
                 : '仅管理员可切换或编辑。');
    }
  }

  /** 一张预设卡片。
   *
   *  卡片本身是 `<article>` 而不是 `<button>`，里面单独放一个「设为默认」按钮——
   *  卡片上还要放编辑/删除/复制，而按钮里嵌按钮是非法 HTML：浏览器会把内层那个
   *  挪到外面去，症状是"点删除却切换了预设"。
   */
  function modeCard(mode, admin) {
    const busy = view.agentBusy === mode.id;
    const card = el('article', 'mode-option'
      + (mode.active ? ' selected' : '') + (busy ? ' busy' : ''));
    card.dataset.modeId = mode.id;

    const head = el('div', 'mode-head');
    head.append(el('span', 'mode-title', mode.label));
    head.append(sourceBadge(mode));
    if (mode.active) head.append(el('span', 'check-badge check-ok', '当前默认'));
    if (mode.edited) head.append(el('span', 'check-badge check-warn', '已覆盖'));
    const skills = asArray(mode.skills);
    head.append(el('span', 'mode-note', '单轮工具预算 ' + mode.steps + ' 次 · '
      + (skills.length ? '指定手册 ' + skills.length + ' 份' : '全部手册')));
    card.append(head);
    card.append(el('p', 'mode-summary', mode.summary || ''));
    if (mode.note) card.append(el('p', 'mode-note', mode.note));
    // 「已覆盖」的两个负担要分开说：改动是什么（原件当前是几），以及上游动过没有。
    // 后者不看的话，插件升级改了原始口径，使用者会在无声中一直读自己保存的那份副本，
    // 而界面上没有任何迹象说明它们已经不是同一个东西了。
    if (mode.edited && mode.baseLabel) {
      card.append(el('p', 'mode-note', '原件是「' + mode.baseLabel + '」，单轮预算 '
        + (mode.baseSteps == null ? '—' : mode.baseSteps) + ' 次；你的改动盖在它上面。'));
    }
    if (mode.baseChanged) {
      card.append(el('p', 'mode-note warn', '这条预设的来源在你保存之后更新过，'
        + '你的改动仍然生效并盖住新版本；要跟上新版本就点「恢复原始」。'));
    }
    card.append(el('p', 'mode-id', mode.id));

    const actions = el('div', 'mode-actions');
    const pick = el('button', 'btn sm', mode.active ? '已是默认' : '设为默认');
    pick.type = 'button';
    pick.dataset.mode = mode.id;
    pick.disabled = !admin || mode.active || busy || !!view.agentBusy;
    actions.append(pick);

    // 「复制为新预设」仍然在：它是"另起一档"的需求（不同的 id、不同的手册清单），
    // 与"改这一档"是两件事。
    const clone = presetAction('复制为新预设', 'clone', mode.id);
    clone.disabled = !admin || busy;
    actions.append(clone);

    // 「编辑」现在对三种来源都开。内置与插件来源保存出来的是一层**覆盖**：原件每次
    // 请求都从代码或插件快照重算，覆盖只存改过的那一份，**id 不变**——所以钉在这一档
    // 上的会话下一轮就跟上。老界面在这两种来源上原本是拒绝的，理由是"下一次请求会被
    // 覆盖"；那个理由已经由覆盖行解决了。留着拒绝会把使用者劝退到「复制为新预设」，
    // 而复制出来的 id 不同，改完对已有会话不生效（他会以为改了没用，而改动其实存着）。
    const edit = presetAction('编辑', 'edit', mode.id);
    edit.disabled = !admin || busy;
    actions.append(edit);
    if (mode.deletable) {
      actions.append(presetDeleteControls(mode));
    } else if (mode.canReset) {
      const restore = presetAction('恢复原始口径', 'reset', mode.id);
      restore.disabled = !admin || busy;
      actions.append(restore);
    }
    card.append(actions);
    return card;
  }

  function presetAction(label, action, id, className) {
    const button = el('button', `btn sm ${className || ''}`.trim(), label);
    button.type = 'button';
    button.dataset.presetAction = action;
    button.dataset.id = String(id || '');
    return button;
  }

  function presetDeleteControls(mode) {
    const wrap = el('span', 'mode-delete');
    if (String(view.pendingAgentDelete) === String(mode.id)) {
      wrap.append(el('span', 'muted', '删除后，钉在它上面的会话会改回「跟随默认」。'));
      wrap.append(presetAction('确认删除', 'delete-confirm', mode.id, 'danger'));
      wrap.append(presetAction('取消', 'delete-cancel', mode.id));
    } else {
      wrap.append(presetAction('删除', 'delete', mode.id, 'danger'));
    }
    return wrap;
  }

  async function activateAgentMode(modeId, button) {
    if (!modeId || view.agentBusy) return;
    view.agentBusy = modeId;
    if (button) button.classList.add('busy');
    showNotice(dom.modeMessage, '');
    try {
      await ui().apiPost('/agents/activate', { id: modeId });
      showNotice(dom.modeMessage, '已把这一档设为全局默认，下一轮对话生效。', 'ok');
    } catch (error) {
      showNotice(dom.modeMessage, '切换失败：' + error.message, 'error');
    } finally {
      view.agentBusy = null;
      await loadAgentModes();
    }
  }

  /* ------------------------------------------- 自定义预设：新建 / 编辑 / 删除 */

  /** 会话入口的 Agent 下拉框是另一份状态，改完预设必须叫它重新拉一次。
   *
   *  不刷的后果很具体：刚删掉的预设还留在入口条的下拉框里，用户选中它、下一轮
   *  对话返回 404"未知的 Agent 预设"——而那时他已经把这次删除忘得差不多了。
   */
  function refreshEntryBar() {
    const chat = window.PentestChat;
    if (chat && typeof chat.loadOptions === 'function') {
      chat.loadOptions().catch(() => {});
    }
  }

  /** 名称 → 标识的即时建议。与后端 `agents._slug` 同规则（小写、非字母数字变连字符）。 */
  function slugify(text) {
    return String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '').slice(0, 32);
  }

  /** 技能手册多选框。每次打开表单都重拉一次：装了新插件之后这里必须跟着变，
   *  缓存住的话用户会在一个过期的清单里勾选，而勾中的名字可能已经不存在。 */
  async function loadSkillChoices() {
    if (!dom.agentSkills) return [];
    let items = [];
    try {
      const data = await ui().apiGet('/skills');
      items = asArray(data.skills);
    } catch (error) { items = []; }
    clear(dom.agentSkills);
    dom.agentSkills.disabled = !items.length;
    if (!items.length) {
      dom.agentSkills.append(new Option('（本机还没有可用的技能手册）', ''));
      return items;
    }
    const byRole = {};
    items.forEach((item) => {
      const key = item.role || '未分类';
      (byRole[key] = byRole[key] || []).push(item);
    });
    Object.keys(byRole).sort().forEach((role) => {
      const group = document.createElement('optgroup');
      group.label = role;
      byRole[role].forEach((item) => {
        group.append(new Option(item.name + (item.source ? '（' + item.source + '）' : ''),
                                item.name));
      });
      dom.agentSkills.append(group);
    });
    return items;
  }

  function selectedSkills() {
    if (!dom.agentSkills) return [];
    return Array.prototype.filter.call(dom.agentSkills.options,
                                      (option) => option.selected)
      .map((option) => option.value)
      .filter(Boolean);
  }

  /** 作业口径的字数读数。
   *
   *  这段正文**没有字数上限**（上限已按本部署的设置撤掉，见 `pentestd/agents.py`），
   *  所以这个读数是"这件事"唯一的出口：它每一轮对话都会随系统提示原样下发，长度直接
   *  乘以轮数。静态文案能写"没有上限"，写不出"你现在写了多少"——而后者才是使用者判断
   *  依据。撤销上限时**别顺手删掉它**：删了之后同一句里那个倍数就只剩一句劝告。
   */
  function syncPromptCount() {
    if (!dom.agentPromptCount) return;
    const chars = String(dom.agentPrompt && dom.agentPrompt.value || '').length;
    dom.agentPromptCount.textContent = chars + ' 字';
  }

  function resetAgentForm() {
    view.agentEditingId = null;
    view.agentHandleTouched = false;
    if (dom.agentForm) dom.agentForm.reset();
    if (dom.agentHandle) {
      dom.agentHandle.value = '';
      dom.agentHandle.disabled = false;
    }
    if (dom.agentHandlePrefix) dom.agentHandlePrefix.classList.remove('hidden');
    if (dom.agentSteps) dom.agentSteps.value = '6';
    if (dom.agentFormScope) {
      dom.agentFormScope.textContent = '';
      dom.agentFormScope.classList.add('hidden');
    }
    if (dom.agentSkills) {
      Array.prototype.forEach.call(dom.agentSkills.options,
                                   (option) => { option.selected = false; });
    }
    if (dom.agentFormTitle) dom.agentFormTitle.textContent = '新建 Agent 预设';
    if (dom.agentSubmit) {
      dom.agentSubmit.textContent = '保存预设';
      dom.agentSubmit.dataset.idleText = '保存预设';
    }
    if (dom.agentCancel) dom.agentCancel.classList.add('hidden');
    if (dom.agentForm) dom.agentForm.classList.add('hidden');
    syncPromptCount();
    showNotice(dom.agentFormMessage, '');
  }

  /** 打开表单。`seed` 是回填值，`editingId` 非空表示编辑已有预设。
   *
   *  两者分开表达是有意的：编辑时标识**不可改**（改了 id 就变，钉在它上面的会话会
   *  静默退回"跟随默认"），所以那种情况下输入框要禁用并显示既有的标识。
   *
   *  `mode` 是列表里那条卡片的元数据，只在编辑内置/插件来源时传。**它决定表单顶上
   *  那条说明说不说、怎么说**：这两种来源保存出来的是一层覆盖，而"保存"这个词在
   *  两者上的含义不同——一个改写原件，另一个加一层盖上去。不说清楚的话，使用者会
   *  以为自己在改插件的安装内容，于是不敢动；或者以为改动是永久的，于是不点恢复。
   */
  async function openAgentForm(seed, editingId, mode) {
    await loadSkillChoices();
    resetAgentForm();
    const filled = seed || {};
    view.agentEditingId = editingId || null;
    if (dom.agentLabel) dom.agentLabel.value = filled.label || '';
    if (dom.agentSummary) dom.agentSummary.value = filled.summary || '';
    if (dom.agentNote) dom.agentNote.value = filled.note || '';
    if (dom.agentSteps) dom.agentSteps.value = String(filled.steps || 6);
    if (dom.agentPrompt) dom.agentPrompt.value = filled.prompt || '';
    syncPromptCount();
    const wanted = asArray(filled.skills);
    if (dom.agentSkills) {
      Array.prototype.forEach.call(dom.agentSkills.options, (option) => {
        option.selected = wanted.indexOf(option.value) >= 0;
      });
    }
    const overriding = !!(mode && !mode.custom && editingId);
    if (dom.agentFormScope) {
      if (overriding) {
        dom.agentFormScope.textContent = '这一档来自' + (mode.builtin ? '平台内置' : 'DSH 插件')
          + '，正文随' + (mode.builtin ? '程序' : '插件') + '更新而变。保存会在它之上加一层覆盖：'
          + '标识仍是 ' + editingId + '，所以钉在这一档上的会话下一轮就跟上；原件不动，'
          + '随时可以用「恢复原始口径」原样退回。';
        dom.agentFormScope.classList.remove('hidden');
      } else {
        dom.agentFormScope.textContent = '';
        dom.agentFormScope.classList.add('hidden');
      }
    }
    if (editingId) {
      const handle = String(editingId).replace(/^custom-/, '');
      if (dom.agentHandle) { dom.agentHandle.value = handle; dom.agentHandle.disabled = true; }
      if (dom.agentHandlePrefix) {
        dom.agentHandlePrefix.classList.toggle('hidden', overriding);
      }
      if (dom.agentFormTitle) {
        dom.agentFormTitle.textContent = (overriding ? '编辑（覆盖）' : '编辑预设')
          + '：' + (filled.label || editingId);
      }
      if (dom.agentSubmit) {
        dom.agentSubmit.textContent = overriding ? '保存覆盖' : '保存修改';
        dom.agentSubmit.dataset.idleText = dom.agentSubmit.textContent;
      }
      if (dom.agentCancel) dom.agentCancel.classList.remove('hidden');
    } else if (dom.agentHandle) {
      dom.agentHandle.value = slugify(filled.label || '');
      view.agentHandleTouched = false;
    }
    if (dom.agentForm) dom.agentForm.classList.remove('hidden');
    if (dom.agentForm && dom.agentForm.scrollIntoView) {
      dom.agentForm.scrollIntoView({ block: 'nearest' });
    }
  }

  async function editAgentPreset(mode) {
    // 正文不在列表里（`public()` 刻意不下发 prompt），所以编辑要单独取一次。
    // 取失败就明确报错并**不**打开表单：打开一张空口径的表单，用户一保存就把
    // 原来那段正文清空了，而他以为自己只是改了个名字。
    try {
      const data = await ui().apiGet('/agents/presets/' + encodeURIComponent(mode.id));
      await openAgentForm(data.preset || {}, mode.id, mode);
    } catch (error) {
      showNotice(dom.modeMessage, '读取预设正文失败：' + error.message, 'error');
    }
  }

  /** 撤掉一层覆盖。服务端做的是删掉覆盖行，原件从来不在库里，所以这一步无损。 */
  async function resetAgentPreset(mode, button) {
    if (!mode || !mode.id || view.agentBusy) return;
    view.agentBusy = mode.id;
    if (button) button.classList.add('busy');
    showNotice(dom.modeMessage, '');
    try {
      const data = await ui().apiPost(
        '/agents/presets/' + encodeURIComponent(mode.id) + '/reset', {});
      if (view.agentEditingId === mode.id) resetAgentForm();
      await loadAgentModes();
      refreshEntryBar();
      showNotice(dom.modeMessage, '已把「' + (data.label || mode.id) + '」恢复成原始口径：'
        + '单轮步数预算 ' + (data.steps == null ? '—' : data.steps) + ' 次。'
        + '钉在这一档上的会话（' + (data.affectedSessions || 0) + ' 条）不受影响。', 'ok');
    } catch (error) {
      showNotice(dom.modeMessage, '恢复失败：' + error.message, 'error');
    } finally {
      view.agentBusy = null;
      if (button) button.classList.remove('busy');
    }
  }

  async function submitAgentForm(event) {
    if (event) event.preventDefault();
    const payload = {
      label: String(dom.agentLabel && dom.agentLabel.value || '').trim(),
      summary: String(dom.agentSummary && dom.agentSummary.value || '').trim(),
      note: String(dom.agentNote && dom.agentNote.value || '').trim(),
      steps: String(dom.agentSteps && dom.agentSteps.value || '').trim(),
      skills: selectedSkills(),
      prompt: String(dom.agentPrompt && dom.agentPrompt.value || ''),
    };
    // 前端只做能**立刻说清**的两项（名称非空、口径非空）。名称与说明的字数、标识字符集、
    // 撞名一律以后端为准：两边各写一份规则，两份迟早不一致，而不一致时以哪一份为准是
    // 不确定的。**作业口径没有字数可校验**——上限已撤（见 pentestd/agents.py），
    // 字数只在旁边显示出来供人判断，不参与收发。
    if (!payload.label) {
      showNotice(dom.agentFormMessage, '请填写预设名称', 'error'); return;
    }
    if (!payload.prompt.trim()) {
      showNotice(dom.agentFormMessage,
                 '请填写作业口径——系统提示里这一整段就是它，空着等于选了一个没有取向的预设',
                 'error');
      return;
    }
    if (!view.agentEditingId) payload.handle = String(dom.agentHandle && dom.agentHandle.value || '').trim();
    const editing = view.agentEditingId;
    setButtonBusy(dom.agentSubmit, true, '保存中…');
    showNotice(dom.agentFormMessage, '');
    try {
      const data = editing
        ? await ui().apiPost('/agents/presets/' + encodeURIComponent(editing), payload)
        : await ui().apiPost('/agents/presets', payload);
      const presetId = (data && data.preset && data.preset.id) || '';
      const overridden = !!(data && data.overridden);
      resetAgentForm();
      await loadAgentModes();
      refreshEntryBar();
      scrollToAgentList();
      // 覆盖与新建要说不同的话：前者"原件还在、可以退回"，后者"这是你新加的一档"。
      // 共用一句"已更新"会让使用者以为内置档的正文被永久改掉了。
      showNotice(dom.modeMessage, (overridden
        ? '已把「' + payload.label + '」的改动存成一层覆盖（标识仍是 '
          + presetId + '，原件不动）；钉在这一档上的会话下一轮就跟上。'
        : (editing ? '已更新「' : '已创建「') + payload.label + '」'
          + (editing ? '' : '（' + presetId + '）')
          + '，可在会话入口选用，下一轮对话生效。'), 'ok');
    } catch (error) {
      // 原样显示后端的话。后端的校验比前端能编的更准（上限、字符集、撞名都在那边），
      // 换成"保存失败"会把唯一能指导下一步的信息丢掉。
      showNotice(dom.agentFormMessage, error.message || '保存失败', 'error');
    } finally {
      setButtonBusy(dom.agentSubmit, false);
    }
  }

  async function cloneAgentPreset(mode) {
    showNotice(dom.modeMessage, '');
    try {
      // 复制在**服务端**完成：正文不下发浏览器（系统提示是内部实现），所以这里
      // 只报一个"从哪来"，正文由服务端搬过去。拿到结果后直接打开编辑表单，
      // 用户马上能改口径——否则他得先在想"副本在哪"。
      const data = await ui().apiPost('/agents/presets/clone', { from: mode.id });
      const preset = (data && data.preset) || {};
      await loadAgentModes();
      await openAgentForm(preset, preset.id);
      showNotice(dom.agentFormMessage,
                 '已从「' + mode.label + '」复制出 ' + preset.id
                 + '，正文与步数一并带过来了。改完点保存。', 'ok');
    } catch (error) {
      showNotice(dom.modeMessage, '复制失败：' + error.message, 'error');
    }
  }

  async function deleteAgentPreset(presetId, button) {
    if (!presetId || view.agentBusy) return;
    view.agentBusy = presetId;
    setButtonBusy(button, true, '删除中…');
    showNotice(dom.modeMessage, '');
    try {
      const data = await ui().apiPost(
        '/agents/presets/' + encodeURIComponent(presetId) + '/delete', {});
      const moved = Number(data && data.affectedSessions) || 0;
      if (view.agentEditingId === presetId) resetAgentForm();
      showNotice(dom.modeMessage, '已删除 ' + presetId + '。'
        + (moved ? moved + ' 个会话原先钉在它上面，已改为「跟随默认」。'
                 : '没有会话钉在它上面。'), 'ok');
    } catch (error) {
      // 409 = 它正被当全局默认用。后端那句话里带着下一步（先切到别的预设），
      // 所以原样显示而不是换一句"删除失败"。
      showNotice(dom.modeMessage, '删除失败：' + error.message, 'error');
    } finally {
      view.agentBusy = null;
      view.pendingAgentDelete = null;
      await loadAgentModes();
      refreshEntryBar();
      scrollToAgentList();
    }
  }

  function bindModeEvents() {
    if (!dom.modeList) return;
    dom.modeList.addEventListener('click', (event) => {
      const closest = event.target.closest ? event.target.closest.bind(event.target) : null;
      if (!closest) return;
      const action = closest('[data-preset-action]');
      if (action) {
        const id = action.dataset.id;
        const mode = view.modes.find((item) => item.id === id);
        if (action.dataset.presetAction === 'edit') editAgentPreset(mode || { id: id });
        else if (action.dataset.presetAction === 'clone') cloneAgentPreset(mode || { id: id });
        else if (action.dataset.presetAction === 'reset') resetAgentPreset(mode, action);
        else if (action.dataset.presetAction === 'delete') {
          view.pendingAgentDelete = id; renderAgentModes();
        } else if (action.dataset.presetAction === 'delete-cancel') {
          view.pendingAgentDelete = null; renderAgentModes();
        } else if (action.dataset.presetAction === 'delete-confirm') {
          deleteAgentPreset(id, action);
        }
        return;
      }
      const pick = closest('[data-mode]');
      if (pick && !pick.disabled) activateAgentMode(pick.dataset.mode, pick);
    });
  }

  /** 保存/删除之后把视图收回列表。
   *
   *  打开表单时会把表单滚进视口（`scrollIntoView`），而表单随保存一起收起之后，
   *  设置面板会停在原来那个滚动位置——用户看到的是列表中间，上面那截（面板标题、
   *  「新建预设」）在视口外。他会以为"保存完界面跳到别处了"。
   */
  function scrollToAgentList() {
    if (dom.modeList && dom.modeList.scrollIntoView) {
      dom.modeList.scrollIntoView({ block: 'nearest' });
    }
  }

  function bindAgentFormEvents() {
    if (dom.agentNew) {
      dom.agentNew.addEventListener('click', () => {
        // 已经打开着就收起：同一个按钮按第二下应当是"算了"，而不是把填了一半的
        // 内容清空重来。
        if (dom.agentForm && !dom.agentForm.classList.contains('hidden')
            && !view.agentEditingId) {
          resetAgentForm();
          return;
        }
        openAgentForm(null, null);
      });
    }
    if (dom.agentForm) dom.agentForm.addEventListener('submit', submitAgentForm);
    if (dom.agentCancel) dom.agentCancel.addEventListener('click', () => resetAgentForm());
    if (dom.agentPrompt) dom.agentPrompt.addEventListener('input', syncPromptCount);
    if (dom.agentLabel && dom.agentHandle) {
      dom.agentLabel.addEventListener('input', () => {
        if (view.agentHandleTouched || view.agentEditingId) return;
        dom.agentHandle.value = slugify(dom.agentLabel.value);
      });
    }
    if (dom.agentHandle) {
      // 用户一旦自己动过标识，就不再跟着名称变——自动改写会把刚输的字符冲掉，
      // 而那种"我打的字自己没了"的故障没人会往"名称联动"上想。
      dom.agentHandle.addEventListener('input', () => {
        view.agentHandleTouched = true;
      });
    }
  }

  /* -------------------------------------------------------------- 分组 ---- */

  /** 切换设置分组。一次只显示一组：设置项涨到五组后，纵向长列表会让
   *  "模型配置在哪"变成需要滚动寻找的问题。 */
  function showGroup(name) {
    view.group = name || 'agents';
    const isAdmin = !!(window.PentestAuth && window.PentestAuth.isAdmin());
    const groups = document.querySelectorAll('.setup-card[data-settings-group]');
    Array.prototype.forEach.call(groups, (node) => {
      if (node === dom.usersCard) return;        // 它由 applyUsersVisibility 单独判定
      const wanted = node.dataset.settingsGroup === view.group;
      node.classList.toggle('hidden', !wanted);
    });
    applyUsersVisibility();
    // Skills 的数据（技能目录）只在真的切到这一组时才拉：它要读 `data/skills`
    // 与插件快照，而绝大多数人来设置页不是为它。拉到之前先画骨架，不画空白。
    if (view.group === 'skills' && window.PentestSkills) window.PentestSkills.load();
    if (dom.nav) {
      Array.prototype.forEach.call(dom.nav.querySelectorAll('[data-settings-group]'),
        (button) => {
          const on = button.dataset.settingsGroup === view.group;
          button.classList.toggle('active', on);
          button.setAttribute('aria-selected', on ? 'true' : 'false');
        });
    }
  }

  function bindNavEvents() {
    if (!dom.nav) return;
    dom.nav.addEventListener('click', (event) => {
      const button = event.target.closest
        ? event.target.closest('[data-settings-group]') : null;
      if (!button) return;
      showGroup(button.dataset.settingsGroup);
      if (view.group === 'plugins' && !view.hubInstalled) loadHubInstalled();
      // MCP 那一组要**现拉一次**：它的状态（连没连上）是会变的，而设置页整体
      // 加载可能发生在很久之前。只在还没拉过时拉，避免每次切回来都打一次接口。
      if (view.group === 'mcp' && !view.mcp) loadMcp();
      // DSH 运行时的状态也是会变的（另一台管理员刚打开、DSH 刚被装上），
      // 而设置页的整体加载可能发生在很久之前 —— 第一次切进来时现拉一次。
      // 「扫描执行」与它在同一个响应里，所以两组共用这一次拉取。
      if ((view.group === 'dsh' || view.group === 'scan') && !view.dshRuntime) {
        loadDshStatus();
      }
      // 审批档位单独一次请求（它挂在 `/harness` 上，和运行通道不是同一个响应）。
      // 同样只在没拉过时拉：档位是部署口径，不会在几秒内自己变。
      if (view.group === 'review' && !view.review) loadReviewMode();
    });
  }

  /* --------------------------------------------------------------- 外观 ---- */

  /** 外观分组。**纯客户端**——主题存在浏览器的 localStorage 里，不经过任何接口。
   *
   *  它和设置页其它五项有本质区别：那些是"改了会影响别人"的服务端配置，这一项只决定
   *  "这台机器上长什么样"。所以它不需要登录、不需要管理员，也不进审计记录。
   *
   *  卡片上的三个色块取自 web/theme.js 的 swatch，因此**不必先切过去就能看出它长什么样**。
   *  这对主题列表是必须的：只列主题名（"纸感""终端"）的话，使用者只能逐个点开试，
   *  而每"试"一次的代价是整页重绘一遍。 */
  function renderThemes() {
    if (!dom.themeGrid || !window.PentestTheme) return;
    const active = window.PentestTheme.current();
    clear(dom.themeGrid);
    window.PentestTheme.THEMES.forEach((theme) => {
      const on = theme.id === active;
      const card = el('button', 'theme-card' + (on ? ' active' : ''));
      card.type = 'button';
      // 用 aria-pressed 而不是 role=radio：这是一组互斥的**切换按钮**。说成单选组
      // 就得同时实现漫游 tabindex 与方向键，否则读屏用户拿到的是一个"按方向键毫无
      // 反应的单选组"——比不声明语义更糟。
      card.setAttribute('aria-pressed', on ? 'true' : 'false');
      card.dataset.themeId = theme.id;

      const chip = el('span', 'theme-chip');
      chip.setAttribute('aria-hidden', 'true');
      // **本文件里唯一允许写 inline 颜色的地方**：预览要画的必须是"另一套"主题的
      // 颜色，用 var() 只会画出当前主题。色值来自 theme.js 的 swatch，不在这里写字面量
      // ——契约测试会核对那三个值与 CSS 里对应主题块一致。
      chip.style.background = theme.swatch.bg;
      const bar = el('span', 'theme-chip-bar');
      bar.style.background = theme.swatch.accent;
      const lineA = el('span', 'theme-chip-line');
      lineA.style.background = theme.swatch.text;
      const lineB = el('span', 'theme-chip-line is-short');
      lineB.style.background = theme.swatch.text;
      chip.append(bar, lineA, lineB);

      const title = el('span', 'theme-title', theme.name);
      title.append(el('span', 'theme-mode', theme.mode === 'light' ? '亮色' : '深色'));
      const body = el('span', 'theme-body');
      body.append(title, el('span', 'theme-hint', theme.hint));
      card.append(chip, body);
      dom.themeGrid.append(card);
    });
    if (dom.themeCurrent) {
      const theme = window.PentestTheme.get(active);
      dom.themeCurrent.textContent = '当前：' + (theme ? theme.name : active);
    }
    if (dom.themeNote) {
      dom.themeNote.textContent = '主题存在这台机器的浏览器里（localStorage），'
        + '不写服务端配置、不随账号同步——换一台机器要重选。';
    }
  }

  function bindThemeEvents() {
    if (!dom.themeGrid) return;
    dom.themeGrid.addEventListener('click', (event) => {
      const card = event.target.closest ? event.target.closest('[data-theme-id]') : null;
      if (!card || !window.PentestTheme) return;
      window.PentestTheme.apply(card.dataset.themeId);
      // 立刻重画：选中高亮与「当前：xxx」都要跟着走。这里**不重新加载任何数据**——
      // 换主题和接口没有关系，顺手 reload 一次只会让整页闪一遍空列表。
      renderThemes();
      // 重点色那一排也要重画：它画的是「这一档在**当前明暗**下的值」，而换主题
      // 正好可能把明暗换边（纸感 ↔ 午夜），不重画的话色点与点下去的结果对不上。
      renderAccents();
      renderFrosts();
    });
  }

  /** 重点色：一行互斥的胶囊按钮，每个前面一个色点。
   *
   *  色点画的是**当前明暗那一族**的值（theme.js 的 ACCENTS.dark / .light）。这一排是
   *  跨主题的，选一次五套主题一起变——所以同一个"蓝"在纸感上是深蓝、在午夜上是亮蓝，
   *  只画一个值会在另一半主题下说谎。
   *
   *  「跟随主题」那一档没有色可画（它的意思是"不覆盖"），画一个虚线圈。 */
  function renderAccents() {
    if (!dom.accentRow || !window.PentestTheme) return;
    const T = window.PentestTheme;
    const active = T.accent();
    const light = T.mode() === 'light';
    clear(dom.accentRow);
    T.ACCENTS.forEach((item) => {
      const on = item.id === active;
      const chip = el('button', 'accent-chip' + (on ? ' active' : ''));
      chip.type = 'button';
      chip.setAttribute('aria-pressed', on ? 'true' : 'false');
      chip.dataset.accentId = item.id;
      const dot = el('span', 'accent-dot' + (item.id === 'theme' ? ' is-none' : ''));
      const value = light ? item.light : item.dark;
      if (value) dot.style.background = value;
      dot.setAttribute('aria-hidden', 'true');
      chip.append(dot, el('span', 'accent-name', item.name));
      dom.accentRow.append(chip);
    });
    if (dom.accentNote) {
      const item = T.getAccent(active);
      dom.accentNote.textContent = item.hint + ' 改的是重点色家族（按钮、选中态、焦点环、'
        + '品牌章），不动严重度配色——那四档颜色是读数，不是外观。';
    }
  }

  /** 磨砂：一行互斥的胶囊按钮。
   *
   *  它只影响模糊半径与饱和度，不动配色。当前主题本来就没有材质时明说一句——
   *  "换了没反应"会被读成功能坏了，而不是"这套主题不用材质"。 */
  function renderFrosts() {
    if (!dom.frostRow || !window.PentestTheme) return;
    const T = window.PentestTheme;
    const active = T.frost();
    const themed = T.current() === 'glass';
    clear(dom.frostRow);
    T.FROSTS.forEach((item) => {
      const on = item.id === active;
      const chip = el('button', 'frost-chip' + (on ? ' active' : ''));
      chip.type = 'button';
      chip.setAttribute('aria-pressed', on ? 'true' : 'false');
      chip.dataset.frostId = item.id;
      chip.append(el('span', null, item.name));
      dom.frostRow.append(chip);
    });
    if (dom.frostNote) {
      const item = T.getFrost(active);
      dom.frostNote.textContent = item.hint
        + (themed ? '' : '（当前主题不用材质，这一档只对「液态玻璃」生效。）');
    }
  }

  /** 透明度：一条 0-100 的滑块。
   *
   *  为什么是滑块而不是像上面三组那样排一排胶囊：这一项的量是**连续**的。"再来一点"
   *  是它唯一有意义的用法，切成五档会让人在相邻两档之间反复横跳、却总差那么一点。
   *  代价是它没有"当前档叫什么"可显示，所以数字必须实时跟着走——否则拖完之后
   *  界面变了、可它到底调到了多少没人知道（刷新之后也未必记得）。
   *
   *  input 事件而不是 change：拖的过程中就要看见变化。这一项只写五个内联自定义属性，
   *  没有布局重排（面令牌被引用的地方都是颜色），每帧一次的开销可以接受。 */
  function renderAlpha() {
    if (!dom.alphaRange || !window.PentestTheme) return;
    const T = window.PentestTheme;
    const value = T.alpha();
    dom.alphaRange.value = String(value);
    dom.alphaRange.setAttribute('aria-valuetext', value + '%');
    if (dom.alphaValue) dom.alphaValue.textContent = value + '%';
    if (dom.alphaNote) {
      dom.alphaNote.textContent = '只调卡片、侧栏、坞这些底的透明程度，不动文字色，'
        + '也不动窗口底——调到 0 时内容仍然读得出来，只是背后的壁纸透不进来。'
        + '和主题一样只存在这台机器的浏览器里。';
    }
  }

  function bindAppearanceEvents() {
    if (dom.alphaRange) {
      dom.alphaRange.addEventListener('input', () => {
        if (!window.PentestTheme) return;
        const value = window.PentestTheme.applyAlpha(dom.alphaRange.value);
        // 只更新数字与 aria，不重画整行：重画会把正在拖的滑块从手底下换掉，
        // 拖动当场中断（Firefox 上尤其明显）。
        if (dom.alphaValue) dom.alphaValue.textContent = value + '%';
        dom.alphaRange.setAttribute('aria-valuetext', value + '%');
      });
    }
    if (dom.accentRow) {
      dom.accentRow.addEventListener('click', (event) => {
        const chip = event.target.closest ? event.target.closest('[data-accent-id]') : null;
        if (!chip || !window.PentestTheme) return;
        window.PentestTheme.applyAccent(chip.dataset.accentId);
        renderAccents();
      });
    }
    if (dom.frostRow) {
      dom.frostRow.addEventListener('click', (event) => {
        const chip = event.target.closest ? event.target.closest('[data-frost-id]') : null;
        if (!chip || !window.PentestTheme) return;
        window.PentestTheme.applyFrost(chip.dataset.frostId);
        renderFrosts();
      });
    }
  }

  /* ------------------------------------------------- DSH 插件快照 ---- */

  /** 把 `/dsh` 的快照灌进 `view.dsh`，并画出「已安装的 DSH 插件」。
   *
   *  这里以前还画一块「DSH 插件集成状态」：两个 home 的路径对照、已采纳/未采纳
   *  的清单、以及"把电脑上那份的插件装入本项目"的一键入口。那一整块按要求去掉
   *  了，插件相关的界面现在只剩插件市场里那三节：目录、已安装、操作记录。
   *
   *  函数本身留下来，是因为 `renderHubInstalled` 要先有 `view.dsh` 才能画，而它
   *  在**打开插件市场分组之前**就会渲染一次（设置页整体加载时）。少了这一步，
   *  那一节会一直停在"登录后加载已安装插件。"——看起来像没登录。
   */
  async function loadDshStatus() {
    try {
      const data = await ui().apiGet('/dsh');
      view.dsh = data.dsh || null;
      view.hubPending = data.pending || 0;
      // DSH 集成只有一条路：运行时通道。它和 /dsh 的其它读数在同一个响应里，
      // 界面不需要再拼第二个接口。
      view.dshRuntime = data.runtime || null;
      view.dshPurge = data.purge || null;
      // 「扫描执行」与运行时通道相邻，但回答的是另一个问题（扫描由谁跑）。
      // 同一个响应带回来，设置页只发一次请求。
      view.scanEngine = data.scanEngine || null;
      view.dshError = null;
      renderHubInstalled(data);
      renderDshRuntime();
      renderScanEngine();
    } catch (error) {
      view.dsh = null; view.dshError = error.message;
      view.dshRuntime = null; view.dshPurge = null; view.scanEngine = null;
      renderHubInstalled(null);
      renderDshRuntime();
      renderScanEngine();
    }
  }

  /* -------------------------------------------------------- DSH 运行时 ---- */

  /** 运行时通道：DSH 集成唯一的入口。
   *
   *  为什么把"能不能开"也画出来：一个只能显示"已打开"的开关，在"其实缺 SDK 补丁"
   *  时与"真的在跑"长得一模一样，而这两件事的下一步动作完全不同——前者要去装，
   *  后者要去看日志。所以不可用时把 `reason` 原文摊在页面上。
   */
  /** 团队插件（AgentTeams）在**对话用的那个 profile** 里到底加没加载。
   *
   *  以前这一格不存在，于是“插件已安装”与“模型手里有没有 agent_teams_*”是两件
   *  看不出关系的事：包在、bundles 里没有，对话机制上就不会加载它。三种状态
   *  分开写：加载了 / 装了没生效 / 没装——第三种是常态，不该画成故障。
   */
  function teamsLine(runtime) {
    const teams = runtime && runtime.conversation && runtime.conversation.agentTeams;
    if (!teams) return '';
    if (teams.active) {
      return ` · AgentTeams 已随对话加载${teams.version ? `（v${teams.version}）` : ''}`;
    }
    if (teams.installed) return ' · AgentTeams 已安装但没进 bundles：这一轮模型看不到它';
    return ' · 未装 AgentTeams（模型只有原生 subagent）';
  }

  function renderDshRuntime() {
    const runtime = view.dshRuntime;
    const admin = !!(window.PentestAuth && window.PentestAuth.isAdmin());
    if (dom.dshRuntimeMode && runtime) {
      dom.dshRuntimeMode.value = runtime.enabled ? 'sdk' : 'off';
    }
    if (dom.dshRuntimeMode) dom.dshRuntimeMode.disabled = !admin;
    if (dom.dshRuntimeSave) dom.dshRuntimeSave.disabled = !admin;
    if (dom.dshRuntimeHint) {
      dom.dshRuntimeHint.textContent = !runtime ? '尚未加载'
        : (runtime.available
          ? `可用 · 项目内的 DSH：${runtime.home}（profile ${runtime.profile}）`
          : `当前**打不开**：${runtime.reason}`);
    }
    if (dom.dshRuntimeState) {
      dom.dshRuntimeState.textContent = !runtime ? '—'
        : (runtime.enabled ? '已打开：这一轮的对话由 DSH 执行' : '关闭中：走平台原生循环')
          + ` · 运行中的通道 ${runtime.running} 条`
          + teamsLine(runtime)
          + (admin ? '' : ' · 当前账号只读（改它需要管理员）');
    }
    renderPurge(admin);
  }

  /** 破甲注入：开关、注入段正文、拒答重试与自动续写。
   *
   *  为什么把"开着但正文为空"画出来而不是禁用保存：那是一个**合法但无效果**的
   *  状态（使用者可能先开开关、再去取正文）。禁用它等于让人误以为"这个开关需要
   *  权限"，而真相是它需要一段文字。
   */
  function renderPurge(admin) {
    const purge = view.dshPurge;
    if (dom.purgeEnabled && purge) dom.purgeEnabled.checked = !!purge.enabled;
    if (dom.purgeRetry && purge) dom.purgeRetry.value = String(purge.retryMax || 0);
    if (dom.purgeContinue && purge) {
      dom.purgeContinue.value = String(purge.continueMax || 0);
    }
    if (dom.purgeContinueText && purge) {
      dom.purgeContinueText.value = purge.continueText || '继续';
    }
    if (dom.purgeInject && purge && document.activeElement !== dom.purgeInject) {
      dom.purgeInject.value = purge.inject || '';
    }
    for (const node of [dom.purgeEnabled, dom.purgeRetry, dom.purgeContinue,
                        dom.purgeContinueText, dom.purgeInject, dom.purgeSave]) {
      if (node) node.disabled = !admin;
    }
    if (dom.purgeState) {
      if (!purge) {
        dom.purgeState.textContent = '—';
      } else {
        const bits = [purge.enabled ? '已启用' : '未启用',
                      `注入段 ${purge.injectChars} 字（无上限）`,
                      `重试 ${purge.retryMax} 次`,
                      `续写 ${purge.continueMax} 次`];
        if (purge.enabled && !purge.injectChars) {
          bits.push('⚠ 开着但正文为空：这一轮的提示词与关闭时完全一样');
        }
        if (purge.source) bits.push(`来源：${purge.source}`);
        if (!admin) bits.push('当前账号只读（改它需要管理员）');
        dom.purgeState.textContent = bits.join(' · ');
      }
    }
  }

  async function savePurge() {
    if (dom.purgeMessage) dom.purgeMessage.classList.add('hidden');
    const body = {
      enabled: !!(dom.purgeEnabled && dom.purgeEnabled.checked),
      inject: dom.purgeInject ? dom.purgeInject.value : '',
      retryMax: dom.purgeRetry ? Number(dom.purgeRetry.value || 0) : 0,
      continueMax: dom.purgeContinue ? Number(dom.purgeContinue.value || 0) : 0,
      continueText: dom.purgeContinueText ? dom.purgeContinueText.value : '继续',
    };
    try {
      const data = await ui().apiPost('/dsh/purge', body);
      view.dshPurge = data.purge || null;
      if (dom.purgeMessage) {
        dom.purgeMessage.textContent = (data.note || '已保存') +
          ((data.purge && data.purge.enabled && !data.purge.injectChars)
            ? '（注意：注入段是空的，现在与关闭时没有区别）' : '');
        dom.purgeMessage.classList.remove('hidden');
      }
    } catch (error) {
      if (dom.purgeMessage) {
        dom.purgeMessage.textContent = '保存失败：' + error.message;
        dom.purgeMessage.classList.remove('hidden');
      }
    }
    renderPurge(true);
  }

  function dshMessage(text) {
    if (!dom.dshMessage) return;
    dom.dshMessage.textContent = text || '';
    dom.dshMessage.classList.toggle('hidden', !text);
  }

  async function saveDshRuntime(mode) {
    dshMessage('');
    try {
      const data = await ui().apiPost('/dsh/runtime', { mode });
      view.dshRuntime = data;
      dshMessage('已保存：' + (data.enabled ? 'DSH 运行通道' : '平台原生循环')
                 + '。下一轮对话生效；正在跑的那一轮不受影响。');
    } catch (error) {
      dshMessage('保存失败：' + error.message);
    }
    renderDshRuntime();
  }

  function scanEngineMessage(text) {
    if (!dom.scanEngineMessage) return;
    dom.scanEngineMessage.textContent = text || '';
    dom.scanEngineMessage.classList.toggle('hidden', !text);
  }

  /** 扫描执行：一次扫描交给发掘引擎还是推理引擎。
   *
   *  除了画选中项，这里把**两条路各自能不能跑**也摊出来。只在被选中的那条上校验，
   *  等于让人切过去之后才发现缺件——而那时任务已经下发了，失败看起来会像偶发故障。
   *  缺件不阻止选择（现场常见"先切过去再装"），但必须当下说清缺什么。
   */
  function renderScanEngine() {
    const scan = view.scanEngine;
    const admin = !!(window.PentestAuth && window.PentestAuth.isAdmin());
    if (dom.scanEngineMode && scan) {
      dom.scanEngineMode.value = scan.mode || 'vulnagent';
    }
    if (dom.scanEngineMode) dom.scanEngineMode.disabled = !admin;
    if (dom.scanEngineSave) dom.scanEngineSave.disabled = !admin;
    if (dom.scanEngineHint) {
      if (!scan) {
        dom.scanEngineHint.textContent = '尚未加载';
      } else {
        const vuln = scan.vulnagent || {};
        const dsh = scan.dsh || {};
        dom.scanEngineHint.textContent =
          '发掘引擎：' + (vuln.available ? '可用' : '不可用（' + (vuln.reason || '原因未知') + '）')
          + ' · 推理引擎：' + (dsh.available
            ? '可用（profile ' + (dsh.profile || '?') + '）'
            : '不可用（' + (dsh.reason || '原因未知') + '）');
      }
    }
    if (dom.scanEngineState) {
      if (!scan) {
        dom.scanEngineState.textContent = '—';
      } else {
        const chosen = (scan.options || []).filter((item) => item.value === scan.mode)[0];
        dom.scanEngineState.textContent = '当前口径：'
          + (chosen ? chosen.label : scan.mode)
          + (admin ? '' : ' · 当前账号只读（改它需要管理员）');
      }
    }
  }

  async function saveScanEngine(mode) {
    scanEngineMessage('');
    try {
      const data = await ui().apiPost('/scan-engine', { mode });
      view.scanEngine = data;
      // `warning` 是服务端在"切过去了但现在跑不起来"时给的原文；直接接在保存
      // 回执后面，别把这句埋掉：下一次扫描失败时它是最先要看的解释。
      scanEngineMessage('已保存：' + (data.label || mode)
                        + '。下一轮扫描生效；已下发的任务不受影响。'
                        + (data.warning ? ' ' + data.warning : ''));
    } catch (error) {
      scanEngineMessage('保存失败：' + error.message);
    }
    renderScanEngine();
  }

  function bindDshEvents() {
    if (dom.dshRuntimeSave) {
      dom.dshRuntimeSave.addEventListener('click', () => {
        saveDshRuntime(dom.dshRuntimeMode ? dom.dshRuntimeMode.value : 'off');
      });
    }
    if (dom.scanEngineSave) {
      dom.scanEngineSave.addEventListener('click', () => {
        saveScanEngine(dom.scanEngineMode ? dom.scanEngineMode.value : 'vulnagent');
      });
    }
    if (dom.purgeSave) dom.purgeSave.addEventListener('click', savePurge);
    if (dom.reviewSave) {
      dom.reviewSave.addEventListener('click', () => {
        saveReview(dom.reviewMode ? dom.reviewMode.value : 'ask');
      });
    }
  }

  /* ------------------------------------------------------------ 工具审批 ---- */

  function reviewMessage(text) {
    if (!dom.reviewMessage) return;
    dom.reviewMessage.textContent = text || '';
    dom.reviewMessage.classList.toggle('hidden', !text);
  }

  /** 工具审批：谁点头。
   *
   *  选项**从接口拿**而不是写死在 HTML 里：档位名称与解释只有 `harness.catalog`
   *  一处来源，页面另存一份的话，加了新档位就会出现"接口有、下拉里没有"的静默
   *  缺失（这一处尤其不能：使用者会以为自己已经换成自动批准了）。
   */
  async function loadReviewMode() {
    try {
      const data = await ui().apiGet('/harness');
      view.review = data.review || null;
      view.reviewError = null;
    } catch (error) {
      view.review = null; view.reviewError = error.message;
    }
    renderReview();
  }

  function renderReview() {
    const review = view.review;
    const admin = !!(window.PentestAuth && window.PentestAuth.isAdmin());
    const modes = review && Array.isArray(review.modes) ? review.modes : [];
    if (dom.reviewMode && modes.length) {
      clear(dom.reviewMode);
      modes.forEach((item) => {
        const option = document.createElement('option');
        option.value = item.id;
        option.textContent = item.label;
        option.title = item.summary || '';
        dom.reviewMode.append(option);
      });
    }
    if (dom.reviewMode && review) dom.reviewMode.value = review.mode || 'ask';
    if (dom.reviewMode) dom.reviewMode.disabled = !admin || !review;
    if (dom.reviewSave) dom.reviewSave.disabled = !admin || !review;
    if (dom.reviewHint) {
      if (!review) {
        dom.reviewHint.textContent = view.reviewError
          ? '读取失败：' + view.reviewError : '尚未加载';
      } else if (!review.reviewedNow || !review.reviewedNow.length) {
        dom.reviewHint.textContent = '当前档位下没有动作需要人工批准：调用直接执行。';
      } else if (review.mode === 'auto') {
        // 同一份名单，两种说法：auto 下"要走审查单"与"会等人"是两件事，
        // 照抄「会先要人点头」会直接跟下面的警告打架——一张单子明明不会等谁。
        dom.reviewHint.textContent = '这些动作仍会建审查单（写进审计），但平台自动批准、不等人工：'
          + review.reviewedNow.join('、') + '。';
      } else {
        dom.reviewHint.textContent = '现在会先要人点头的动作：' + review.reviewedNow.join('、') + '。';
      }
    }
    if (dom.reviewState) {
      if (!review) {
        dom.reviewState.textContent = '—';
      } else {
        const chosen = modes.filter((item) => item.id === review.mode)[0];
        dom.reviewState.textContent = '当前档位：'
          + (chosen ? chosen.label : review.mode)
          + '（' + review.mode + '）'
          + (admin ? '' : ' · 当前账号只读（改它需要管理员）');
      }
    }
    if (dom.reviewWarning) {
      // 两段警告分开写：它们都危险，但危险的方式不同，不能共用一句话。
      const mode = review ? review.mode : '';
      let warning = '';
      if (mode === 'auto') {
        warning = '自动批准已生效：任何命令、任何侵入动作都不再等人点头，'
          + '由平台自己批。审查单仍会逐条建立、写明命令与理由，事后可查——'
          + '但出错时没有人替你拦一下——只在无人值守、目标范围已经定死时使用。';
      } else if (mode === 'off') {
        warning = '审查已关闭：调用不留审查单，事后也查不到"这一次是谁批的"。'
          + '与「自动批准并执行」的差别就在这里，别把两者当同一件事。';
      }
      dom.reviewWarning.textContent = warning;
      dom.reviewWarning.classList.toggle('hidden', !warning);
    }
    renderReviewMessageKeep();
  }

  /** 保存回执要留在卡片上；重画下拉框不该把它抹掉。 */
  function renderReviewMessageKeep() {
    if (dom.reviewMode && dom.reviewMessage) {
      dom.reviewMessage.classList.toggle('hidden', !dom.reviewMessage.textContent);
    }
  }

  async function saveReview(mode) {
    reviewMessage('');
    try {
      const data = await ui().apiPost('/harness/review', { mode });
      view.review = data.review || null;
      const chosen = ((view.review && view.review.modes) || []).filter((item) => item.id === mode)[0];
      reviewMessage('已保存：' + (chosen ? chosen.label : mode)
                    + '。下一轮对话生效；正在跑的那一轮不受影响。');
    } catch (error) {
      reviewMessage('保存失败：' + error.message);
    }
    renderReview();
  }

  /* ------------------------------------------------------------ 权限档 ---- */

  async function loadPolicies() {
    try {
      const data = await ui().apiGet('/policies');
      view.policies = Array.isArray(data.policies) ? data.policies : [];
      view.activePolicyId = data.activeId || '';
      view.policyError = null;
    } catch (error) {
      view.policies = []; view.activePolicyId = ''; view.policyError = error.message;
    }
    renderPolicies();
  }

  function renderPolicies() {
    if (!dom.policyList) return;
    clear(dom.policyList);
    const admin = !!(window.PentestAuth && window.PentestAuth.isAdmin());
    const active = view.policies.filter((item) => item.id === view.activePolicyId)[0] || null;
    if (dom.policyCurrent) {
      dom.policyCurrent.textContent = active ? active.label
        : (view.policyError ? '加载失败' : '尚未加载');
    }
    if (!view.policies.length) {
      dom.policyList.append(ui().emptyBlock(view.policyError
        ? '权限档加载失败：' + view.policyError : '登录后加载权限档。'));
      return;
    }
    view.policies.forEach((item) => {
      const busy = view.policyBusy === item.id;
      const option = el('button', 'mode-option'
        + (item.active ? ' selected' : '') + (busy ? ' busy' : ''));
      option.type = 'button';
      option.dataset.policy = item.id;
      option.disabled = !admin || item.active || !!view.policyBusy;
      const head = el('div', 'mode-head');
      head.append(el('span', 'mode-title', item.label));
      if (item.active) head.append(el('span', 'check-badge check-ok', '默认档'));
      head.append(el('span', 'mode-note', item.toolCount + ' 项能力'));
      option.append(head);
      option.append(el('p', 'mode-summary', item.summary || ''));
      if (item.note) option.append(el('p', 'mode-note', item.note));
      const tools = el('p', 'mode-note', '包含：' + (item.tools || []).join(' / '));
      option.append(tools);
      dom.policyList.append(option);
    });
    if (dom.policyNote) {
      dom.policyNote.textContent = '这里是**默认档位**；某个会话具体用哪一档，在'
        + '「控制台」的入口条上选。权限档只收窄、不放宽：调到最高档，越界目标依旧'
        + '被门禁拒绝。' + (admin ? '' : '仅管理员可改默认档。');
    }
  }

  async function activatePolicy(policyId, button) {
    if (!policyId || view.policyBusy) return;
    view.policyBusy = policyId;
    if (button) button.classList.add('busy');
    showNotice(dom.policyMessage, '');
    try {
      await ui().apiPost('/policies/activate', { id: policyId });
      showNotice(dom.policyMessage, '已切换默认权限档，新会话与未指定档位的会话生效。', 'ok');
    } catch (error) {
      showNotice(dom.policyMessage, '切换失败：' + error.message, 'error');
    } finally {
      view.policyBusy = null;
      await loadPolicies();
    }
  }

  function bindPolicyEvents() {
    if (!dom.policyList) return;
    dom.policyList.addEventListener('click', (event) => {
      const button = event.target.closest ? event.target.closest('[data-policy]') : null;
      if (!button || button.disabled) return;
      activatePolicy(button.dataset.policy, button);
    });
  }

  /* ---------------------------------------------------------- MCP 服务 ---- */

  /**
   * 外部 MCP 服务。**与设置页其它分组的根本差别**：这里的每一条都对应这个部署上
   * 的一个外部程序 —— 一条 stdio 服务就是这台机器上的一个进程。所以：
   *
   *   * 读（GET /mcp）只要登录：工具目录里本来就看得到它们，档位过滤也在服务端做；
   *   * 写、测、手工调用都要求管理员，而且**是服务端在判**（`_require_admin`）。
   *     前端这里不重复判一遍当作安全措施 —— 它只用来决定按钮显不显示。
   *   * 非管理员**看不到**命令、参数、地址、环境变量、请求头（`public_server` 里
   *     剔掉了），所以这一页不为它们渲染任何东西，而不是渲染成空的。
   */

  const MCP_STDIO_ROW = 'stdio';

  /** 文本域 → 参数数组：每行一个，去掉空行。 */
  function mcpLines(node) {
    return String(node && node.value || '')
      .split('\n').map((line) => line.trim()).filter((line) => line.length > 0);
  }

  /** 文本域 → 键值对象：每行 KEY=VALUE。**第一个等号**切开，值里可以有等号。 */
  function mcpPairs(node, label, message) {
    const out = {};
    const lines = mcpLines(node);
    for (let index = 0; index < lines.length; index += 1) {
      const at = lines[index].indexOf('=');
      if (at <= 0) {
        if (message) showNotice(message, label + '第 ' + (index + 1) + ' 行不是 KEY=VALUE：' + lines[index], 'error');
        return null;
      }
      out[lines[index].slice(0, at).trim()] = lines[index].slice(at + 1).trim();
    }
    return out;
  }

  /**
   * 一条服务 → 保存接口要的**完整**请求体。
   *
   * 抽出来是因为保存是"整条覆盖"：任何一次编辑（改个名字、点一下停用）都必须把
   * 没动的字段一起带回去，少一个就会被 `validate()` 当成"没填"而清空。上一版
   * 「停用」只发了 enabled，于是它会顺手把环境变量清掉——界面上没有任何提示。
   * 收在一处之后，加字段只需要改这一个函数。
   */
  function mcpPayloadFromServer(server, overrides) {
    return Object.assign({
      id: server.id, label: server.label, transport: server.transport,
      level: server.level, timeoutS: server.timeoutS, review: !!server.review,
      enabled: !!server.enabled, note: server.note || '',
      command: server.command || '', args: server.args || [],
      env: server.env || {}, cwd: server.cwd || '',
      url: server.url || '', headers: server.headers || {},
      insecure: !!server.insecure,
    }, overrides || {});
  }

  async function loadMcp(force) {
    try {
      const data = await ui().apiGet('/mcp');
      view.mcp = data;
      view.mcpError = null;
    } catch (error) {
      view.mcp = null; view.mcpError = error.message;
    }
    fillMcpLevels();
    renderMcp();
  }

  /** 一张卡片的正文。抽出来是为了让"卡片"与"卡片上的某个工具"能分开重画。 */
  function renderMcpServer(server) {
    const card = el('div', 'mcp-card' + (server.enabled ? '' : ' off'));
    card.dataset.mcpServer = server.id;
    const head = el('div', 'mcp-card-head');
    head.append(el('span', 'mode-title', server.label || server.id));
    head.append(el('span', 'mcp-id', server.id));
    head.append(el('span', 'check-badge' + (server.status === 'ready' ? ' check-ok' : ''),
      server.status === 'ready' ? '已连接'
        : (server.status === 'error' ? '连接失败' : '未探测')));
    if (!server.enabled) head.append(el('span', 'check-badge', '已停用'));
    if (server.review) head.append(el('span', 'check-badge', '需批准'));
    if (!server.permitted) {
      head.append(el('span', 'check-badge', '档位不够'));
    }
    head.append(el('span', 'mode-note', server.toolCount + ' 个工具'));
    head.append(el('span', 'mode-note', server.levelLabel || server.level));
    head.append(el('span', 'mode-note', server.transport));
    card.append(head);

    // 连不上的原因 / 编码提醒：两件事分开显示，因为它们要做的动作不同。
    if (server.lastError) {
      card.append(el('p', 'notice error', '上一次失败：' + server.lastError));
    }
    if (server.warning) {
      card.append(el('p', 'notice warn', server.warning));
    }
    const facts = [];
    if (server.serverName) facts.push('对端：' + server.serverName + ' ' + (server.serverVersion || ''));
    if (server.protocol) facts.push('协议 ' + server.protocol);
    if (server.command) facts.push('命令：' + [server.command].concat(server.args || []).join(' '));
    if (server.url) facts.push('地址：' + server.url);
    if (server.cwd) facts.push('工作目录：' + server.cwd);
    if ((server.envKeys || []).length) facts.push('环境变量：' + server.envKeys.join(', '));
    if ((server.headerKeys || []).length) facts.push('请求头：' + server.headerKeys.join(', '));
    if (typeof server.probedAt === 'number') facts.push('上次探测：' + ui().formatTime(server.probedAt));
    facts.push('超时 ' + server.timeoutS + ' 秒');
    if (facts.length) card.append(el('p', 'mode-note', facts.join('　·　')));
    if (server.note) card.append(el('p', 'mode-note', server.note));
    if (!server.admin) {
      card.append(el('p', 'mode-note', '（命令、参数、地址与环境变量只有管理员可见）'));
    }

    const actions = el('div', 'form-actions');
    if (server.admin) {
      const test = el('button', 'btn sm', '测试连接');
      test.type = 'button'; test.dataset.mcpAction = 'test'; test.dataset.mcpId = server.id;
      actions.append(test);
      const edit = el('button', 'btn sm', '编辑');
      edit.type = 'button'; edit.dataset.mcpAction = 'edit'; edit.dataset.mcpId = server.id;
      actions.append(edit);
      const toggle = el('button', 'btn sm', server.enabled ? '停用' : '启用');
      toggle.type = 'button'; toggle.dataset.mcpAction = 'toggle'; toggle.dataset.mcpId = server.id;
      actions.append(toggle);
      const drop = el('button', 'btn sm danger', '删除');
      drop.type = 'button'; drop.dataset.mcpAction = 'delete'; drop.dataset.mcpId = server.id;
      actions.append(drop);
    }
    if (view.mcpBusy === server.id) actions.append(el('span', 'muted', '处理中…'));
    card.append(actions);

    if (!server.toolCount) {
      card.append(el('p', 'mode-note', server.status === 'error'
        ? '连接失败，因此还没有工具清单。'
        : '还没有取到工具清单：点「测试连接」连一次。'));
      return card;
    }
    const tools = el('div', 'mcp-tools');
    (server.tools || []).forEach((tool) => {
      const row = el('div', 'mcp-tool');
      row.append(el('span', 'mcp-tool-name', tool.name));
      row.append(el('span', 'mcp-tool-exposed', tool.exposed));
      if (tool.readOnly) row.append(el('span', 'check-badge', '只读'));
      if (tool.schemaTrimmed) row.append(el('span', 'check-badge', '参数表已裁'));
      row.append(el('span', 'mcp-tool-note', tool.description || ''));
      if (server.admin) {
        const call = el('button', 'btn sm', '试调');
        call.type = 'button';
        call.dataset.mcpAction = 'call';
        call.dataset.mcpId = server.id;
        call.dataset.mcpTool = tool.exposed;
        row.append(call);
      }
      tools.append(row);
    });
    card.append(tools);
    return card;
  }

  function renderMcp() {
    if (!dom.mcpList) return;
    clear(dom.mcpList);
    if (dom.mcpMeta) {
      const data = view.mcp;
      dom.mcpMeta.textContent = view.mcpError ? '加载失败'
        : (data ? (data.servers || []).length + ' 个服务 · 当前档位下模型可见 '
            + (data.declared || 0) + ' 个 MCP 工具' : '尚未加载');
    }
    if (view.mcpError) {
      dom.mcpList.append(ui().emptyBlock('MCP 服务加载失败：' + view.mcpError));
      return;
    }
    if (!view.mcp) {
      dom.mcpList.append(ui().emptyBlock('登录后加载 MCP 服务。'));
      return;
    }
    const servers = view.mcp.servers || [];
    if (!servers.length) {
      dom.mcpList.append(ui().emptyBlock(
        '还没有登记 MCP 服务。下面填一个就能把外部工具接进「控制台」。'));
      return;
    }
    servers.forEach((server) => dom.mcpList.append(renderMcpServer(server)));

    // 活连接：stdio 服务是这台机器上的真实进程，值得能看一眼。
    const live = view.mcp.sessions || [];
    if (live.length) {
      dom.mcpList.append(el('p', 'mode-note',
        '当前已建立的连接：' + live.map((item) => item.id + '（'
          + item.transport + (item.alive ? '' : '，已断开') + '）').join('、')));
    }
    // 档位不够的服务要说出来：否则"我加了服务，模型却说没有"看起来像坏了。
    //
    // 这句与上面那张卡片读同一个字段：`server.levelLabel` 是后端
    // `mcp.public_server()` 按 `policy.POLICY_INDEX` 贴的中文档名（只读观察 /
    // 网络探测 / 源码审计 / 完全作业）。原先这里印 `server.level` 原值，
    // 于是同一张卡上徽章写「档位不够」、卡片头写「只读观察」、这句播报写
    // `readonly` —— 而「把权限档调高」这个建议，正是因为读者不知道
    // `readonly` 是高还是低才读不懂。认不出的档位后端给空串，回落原值。
    const blocked = servers.filter((server) => server.enabled && !server.permitted);
    if (blocked.length) {
      dom.mcpList.append(el('p', 'mode-note',
        '这些服务当前档位拿不到（模型看不到它们的工具）：'
        + blocked.map((server) => server.id + '（注册档位 '
          + (server.levelLabel || server.level) + '）').join('、')
        + '。要用它们，把对话入口的权限档调高。'));
    }
    // 声明上限：**必须说出来**。不说的话，第 49 个工具会静默地不存在。
    if (view.mcp.declaredTruncated) {
      dom.mcpList.append(el('p', 'mode-note',
        '注意：可用的 MCP 工具超过了一次对话能附的上限（'
        + (view.mcp.limits || {}).declarations + ' 个），超出部分**不会**出现在模型的'
        + '工具表里。按服务停用不再用的那几个，或把它们的档位调高。'));
    }
  }

  function fillMcpLevels() {
    if (!dom.mcpLevel) return;
    const levels = ((view.mcp || {}).levels || []);
    if (dom.mcpLevel.options.length === levels.length) return;
    clear(dom.mcpLevel);
    levels.forEach((item) => {
      const option = el('option', '', item.label + '（' + item.id + '）');
      option.value = item.id;
      dom.mcpLevel.append(option);
    });
    dom.mcpLevel.value = 'full';
  }

  function mcpTransportChanged() {
    if (!dom.mcpTransport) return;
    const stdio = (dom.mcpTransport.value || '') === MCP_STDIO_ROW;
    // 命令 / 参数 / 环境变量只有 stdio 用；地址只有 http 用。**两组都换**，
    // 不是只把另一组显示出来——留着上一次填的内容，切回来会以为它还生效。
    if (dom.mcpStdioRow) dom.mcpStdioRow.classList.toggle('hidden', !stdio);
    if (dom.mcpArgsRow) dom.mcpArgsRow.classList.toggle('hidden', !stdio);
    if (dom.mcpEnvRow) dom.mcpEnvRow.classList.toggle('hidden', !stdio);
    if (dom.mcpHttpRow) dom.mcpHttpRow.classList.toggle('hidden', stdio);
  }

  function resetMcpForm() {
    view.mcpEditingId = null;
    if (dom.mcpFormTitle) dom.mcpFormTitle.textContent = '添加 MCP 服务';
    ['mcpId', 'mcpLabel', 'mcpCommand', 'mcpArgs', 'mcpUrl', 'mcpEnv',
     'mcpNoteField'].forEach((key) => { if (dom[key]) dom[key].value = ''; });
    if (dom.mcpTransport) dom.mcpTransport.value = MCP_STDIO_ROW;
    if (dom.mcpLevel) dom.mcpLevel.value = 'full';
    if (dom.mcpTimeout) dom.mcpTimeout.value = '60';
    if (dom.mcpEnabled) dom.mcpEnabled.value = '1';
    if (dom.mcpReview) dom.mcpReview.checked = true;
    if (dom.mcpId) dom.mcpId.disabled = false;
    if (dom.mcpSubmit) dom.mcpSubmit.textContent = '保存并探测';
    mcpTransportChanged();
    showNotice(dom.mcpFormMessage, '');
  }

  /** 编辑：把一条服务的当前值读进表单。服务端给的是**可编辑形态**，不另算一遍。 */
  function openMcpForm(server) {
    view.mcpEditingId = server.id;
    if (dom.mcpFormTitle) dom.mcpFormTitle.textContent = '编辑 MCP 服务：' + server.id;
    dom.mcpId.value = server.id;
    // id 是工具名的一部分，改了会让已有会话里那些 mcp__旧id__x 全部失效 ——
    // 所以编辑时锁住它，要改就删了重建（代价是被明确告知过的）。
    dom.mcpId.disabled = true;
    dom.mcpLabel.value = server.label || '';
    dom.mcpTransport.value = server.transport || MCP_STDIO_ROW;
    dom.mcpCommand.value = server.command || '';
    dom.mcpArgs.value = (server.args || []).join('\n');
    dom.mcpUrl.value = server.url || '';
    dom.mcpEnv.value = (server.envKeys || []).map(
      (key) => key + '=' + ((server.env || {})[key] || '')).join('\n');
    dom.mcpLevel.value = server.level || 'full';
    dom.mcpTimeout.value = String(server.timeoutS || 60);
    dom.mcpEnabled.value = server.enabled ? '1' : '0';
    dom.mcpReview.checked = !!server.review;
    dom.mcpNoteField.value = server.note || '';
    if (dom.mcpSubmit) dom.mcpSubmit.textContent = '保存并探测';
    mcpTransportChanged();
    showNotice(dom.mcpFormMessage, '');
    if (dom.mcpForm && dom.mcpForm.scrollIntoView) {
      dom.mcpForm.scrollIntoView({ block: 'nearest' });
    }
  }

  async function submitMcpForm(event) {
    if (event) event.preventDefault();
    const id = (dom.mcpId.value || '').trim().toLowerCase();
    const label = (dom.mcpLabel.value || '').trim();
    if (!id) { showNotice(dom.mcpFormMessage, '服务 id 不能为空', 'error'); return; }
    if (!label) { showNotice(dom.mcpFormMessage, '名称不能为空', 'error'); return; }
    const env = mcpPairs(dom.mcpEnv, '环境变量', dom.mcpFormMessage);
    if (env === null) return;
    const payload = {
      id, label,
      transport: dom.mcpTransport.value || MCP_STDIO_ROW,
      level: dom.mcpLevel.value || 'full',
      timeoutS: Number(dom.mcpTimeout.value) || 60,
      enabled: dom.mcpEnabled.value === '1',
      review: !!dom.mcpReview.checked,
      note: (dom.mcpNoteField.value || '').trim(),
      env,
    };
    if (payload.transport === 'stdio') {
      payload.command = (dom.mcpCommand.value || '').trim();
      payload.args = mcpLines(dom.mcpArgs);
      if (!payload.command) {
        showNotice(dom.mcpFormMessage, 'stdio 传输必须填命令', 'error'); return;
      }
    } else {
      payload.url = (dom.mcpUrl.value || '').trim();
      if (!payload.url) { showNotice(dom.mcpFormMessage, 'HTTP 传输必须填地址', 'error'); return; }
    }
    setButtonBusy(dom.mcpSubmit, true, '保存并探测…');
    showNotice(dom.mcpFormMessage, '正在保存并连一次…');
    try {
      // 探测可能要等到超时（最长 600 秒），所以这一条**不走**默认 15 秒预算：
      // 被页面超时切断时请求其实还在服务端跑，而使用者看到的是一句"请求超时"，
      // 他会以为没保存成功，然后再按一次 —— 于是两次探测。
      const budget = (payload.timeoutS + 20) * 1000;
      const data = await ui().apiPost('/mcp', payload, null, budget);
      const probe = data.probe || {};
      showNotice(dom.mcpFormMessage, probe.ok
        ? '已保存，连上了，发现 ' + (probe.tools || []).length + ' 个工具。'
        : '已保存。但这次没连上：' + (probe.error || '未知原因')
          + '（配置已经存下来了，改好再点「测试连接」）',
        probe.ok ? 'ok' : 'error');
      resetMcpForm();
      await loadMcp(true);
    } catch (error) {
      showNotice(dom.mcpFormMessage, '保存失败：' + error.message, 'error');
    } finally {
      setButtonBusy(dom.mcpSubmit, false);
    }
  }

  async function mcpAction(action, serverId, toolName, button) {
    const server = ((view.mcp || {}).servers || []).filter(
      (item) => item.id === serverId)[0];
    if (!server) return;
    if (action === 'edit') { openMcpForm(server); return; }
    if (action === 'delete') {
      // 删除是不可逆的：确认一步，而且要说清"删掉之后模型手上会少哪些工具"。
      if (!window.confirm('删除 MCP 服务「' + (server.label || serverId) + '」？\n'
        + '它下面的 ' + server.toolCount + ' 个工具会立刻从对话里消失。')
      ) return;
    }
    if (action === 'call') {
      const text = window.prompt('给 ' + toolName + ' 的参数（JSON 对象）', '{}');
      if (text === null) return;
      let args = {};
      try { args = text.trim() ? JSON.parse(text) : {}; } catch (error) {
        showNotice(dom.mcpMessage, '参数不是合法 JSON：' + error.message, 'error'); return;
      }
      setButtonBusy(button, true, '调用中…');
      showNotice(dom.mcpMessage, '');
      try {
        const data = await ui().apiPost('/mcp/' + encodeURIComponent(serverId) + '/call',
          { name: toolName, arguments: args }, null, (server.timeoutS + 20) * 1000);
        showNotice(dom.mcpMessage, toolName + ' → ' + (data.summary || '（无返回）'),
          data.ok ? 'ok' : 'error');
      } catch (error) {
        showNotice(dom.mcpMessage, '调用失败：' + error.message, 'error');
      } finally {
        setButtonBusy(button, false);
        await loadMcp(true);
      }
      return;
    }
    if (action === 'toggle') {
      // 启用/停用走**同一个保存接口**（整条覆盖），所以必须带全字段。
      // 走 `mcpPayloadFromServer` 而不是手写一份 —— 少写一个字段就是一次静默清空。
      const payload = mcpPayloadFromServer(server, { enabled: !server.enabled });
      view.mcpBusy = serverId; renderMcp();
      try {
        await ui().apiPost('/mcp', payload, null, (server.timeoutS + 20) * 1000);
        showNotice(dom.mcpMessage, server.enabled ? '已停用。' : '已启用。', 'ok');
      } catch (error) {
        showNotice(dom.mcpMessage, '操作失败：' + error.message, 'error');
      } finally {
        view.mcpBusy = null;
        await loadMcp(true);
      }
      return;
    }
    if (action === 'test') {
      view.mcpBusy = serverId; renderMcp();
      showNotice(dom.mcpMessage, '');
      try {
        const data = await ui().apiPost('/mcp/' + encodeURIComponent(serverId) + '/test',
          {}, null, (server.timeoutS + 20) * 1000);
        const probe = data.probe || {};
        showNotice(dom.mcpMessage, probe.ok
          ? (server.label || serverId) + '：连上了，发现 ' + (probe.tools || []).length + ' 个工具。'
          : (server.label || serverId) + '：没连上——' + (probe.error || '未知原因'),
          probe.ok ? 'ok' : 'error');
      } catch (error) {
        showNotice(dom.mcpMessage, '测试失败：' + error.message, 'error');
      } finally {
        view.mcpBusy = null;
        await loadMcp(true);
      }
    }
  }

  function bindMcpEvents() {
    if (dom.mcpForm) dom.mcpForm.addEventListener('submit', submitMcpForm);
    if (dom.mcpCancel) dom.mcpCancel.addEventListener('click', () => resetMcpForm());
    if (dom.mcpRefresh) {
      dom.mcpRefresh.addEventListener('click', () => loadMcp(true));
    }
    if (dom.mcpTransport) dom.mcpTransport.addEventListener('change', mcpTransportChanged);
    if (dom.mcpList) {
      dom.mcpList.addEventListener('click', (event) => {
        const button = event.target.closest ? event.target.closest('[data-mcp-action]') : null;
        if (!button || button.disabled) return;
        mcpAction(button.dataset.mcpAction, button.dataset.mcpId,
          button.dataset.mcpTool, button);
      });
    }
  }

  /* ---------------------------------------------------------- 插件市场 ---- */

  function hubRow(item) {
    const row = el('article', 'hub-item');
    const head = el('div', 'hub-item-head');
    head.append(el('span', 'hub-item-name', item.name || item.id));
    if (item.version) head.append(el('span', 'hub-item-version', item.version));
    if (item.verified) head.append(el('span', 'check-badge check-ok', '已验证'));
    if (item.category) head.append(el('span', 'hub-item-cat', item.category));
    row.append(head);
    row.append(el('p', 'hub-item-id muted', item.id + (item.owner ? ' · ' + item.owner : '')));
    if (item.description) row.append(el('p', 'hub-item-desc', item.description));
    const features = (item.features || []).slice(0, 4);
    if (features.length) {
      const list = el('ul', 'hub-item-features');
      features.forEach((text) => list.append(el('li', null, text)));
      row.append(list);
    }
    const foot = el('div', 'hub-item-foot');
    foot.append(el('code', 'hub-item-target', item.installTarget || ''));
    foot.append(el('span', 'spacer'));
    // 仓库链接由**宿主**算好后下发（repositoryUrl）；前端不拼 href——
    // 这个值源自第三方字符串，前端直接当 href 用会变成一个可点击的注入点。
    if (item.repositoryUrl) {
      const link = el('a', 'link-btn', '仓库');
      link.href = item.repositoryUrl;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      foot.append(link);
    }
    const install = el('button', 'btn sm', '安装');
    install.type = 'button';
    install.dataset.install = item.installTarget || '';
    install.disabled = !item.installTarget;
    foot.append(install);
    row.append(foot);
    return row;
  }

  function renderHub() {
    if (!dom.hubResults) return;
    clear(dom.hubResults);
    const hub = view.hub;
    if (hub.error) {
      dom.hubResults.append(ui().errorBlock('插件目录不可用：' + hub.error));
    } else if (!hub.items.length) {
      dom.hubResults.append(ui().emptyBlock(hub.query
        ? '没有匹配「' + hub.query + '」的插件。'
        : '输入关键词后点「搜索」；目录共 ' + (hub.catalogTotal || 0) + ' 条。'));
    } else {
      hub.items.forEach((item) => dom.hubResults.append(hubRow(item)));
    }
    if (dom.hubMoreRow) dom.hubMoreRow.classList.toggle('hidden', !hub.hasMore);
    if (dom.hubMeta) {
      const parts = [];
      if (hub.catalogTotal) parts.push('目录 ' + hub.catalogTotal + ' 条');
      if (hub.total) parts.push('命中 ' + hub.total + ' 条');
      if (hub.items.length) parts.push('已显示 ' + hub.items.length + ' 条');
      if (hub.stale) parts.push('⚠ 使用缓存的旧目录（上游不可达）');
      dom.hubMeta.textContent = parts.length ? parts.join(' · ')
        : '尚未搜索。目录来自 DSH Plugin Hub，约 9.8 MB，首次拉取需要十几秒。';
    }
    fillCategories();
  }

  function fillCategories() {
    if (!dom.hubCategory) return;
    const current = view.hub.category;
    const items = (view.dsh && view.dsh.categories) || view.hub.categories || [];
    clear(dom.hubCategory);
    const all = el('option', null, '全部分类');
    all.value = '';
    dom.hubCategory.append(all);
    items.forEach((item) => {
      const option = el('option', null, item.id + '（' + item.count + '）');
      option.value = item.id;
      dom.hubCategory.append(option);
    });
    dom.hubCategory.value = current || '';
  }

  async function searchHub(append) {
    if (view.hub.busy) return;
    const offset = append ? view.hub.offset + view.hub.items.length : 0;
    view.hub.busy = true;
    setButtonBusy(dom.hubSearch, true, '拉取中…');
    showNotice(dom.hubMessage, append ? '' : '正在读取插件目录…');
    try {
      const params = [];
      params.push('q=' + encodeURIComponent(view.hub.query || ''));
      params.push('limit=20');
      params.push('offset=' + offset);
      if (view.hub.category) params.push('category=' + encodeURIComponent(view.hub.category));
      if (view.hub.verified) params.push('verified=1');
      // 查询串单独拼：路径字面量里不带 `?`。契约测试会从脚本里抽出接口路径去比对
      // 路由表，写成 `'/hub/catalog?' + q` 会让它抽出 `/api/v1/hub/catalog?`
      // 这个并不存在的路径——而那种失败看起来像"后端少了接口"，误导性很强。
      const query = '?' + params.join('&');
      const data = await ui().apiGet('/hub/catalog' + query);
      if (!data.ok) throw new Error(data.reason || '目录不可用');
      const items = Array.isArray(data.items) ? data.items : [];
      view.hub.items = append ? view.hub.items.concat(items) : items;
      view.hub.total = data.total || 0;
      view.hub.hasMore = Boolean(data.hasMore);
      view.hub.stale = Boolean(data.stale);
      view.hub.catalogTotal = data.catalogTotal || 0;
      view.hub.categories = data.categories || [];
      view.hub.error = null;
      showNotice(dom.hubMessage, '');
    } catch (error) {
      if (!append) view.hub.items = [];
      view.hub.error = error.message;
      showNotice(dom.hubMessage, '搜索失败：' + error.message, 'error');
    } finally {
      view.hub.busy = false;
      setButtonBusy(dom.hubSearch, false);
      renderHub();
    }
  }

  function renderHubInstalled(payload) {
    if (!dom.hubInstalled) return;
    clear(dom.hubInstalled);
    const data = view.dsh;
    if (!data) {
      // 分清"还没登录"与"拉取失败"：后者过去也走同一句话，于是一次接口故障
      // 被读成"登录后加载……"，而使用者已经在登录状态里了。
      dom.hubInstalled.append(ui().emptyBlock(view.dshError
        ? '插件状态加载失败：' + view.dshError : '登录后加载已安装插件。'));
      return;
    }
    if (!data.available) {
      // 这里必须区分两件事，否则文案会反过来吓人：`available: false` 在隔离之后
      // 几乎总是"项目内还没装插件"（正常状态），而不是"这台机器没有 DSH"。
      dom.hubInstalled.append(ui().emptyBlock(data.isolated
        ? '项目内还没有安装任何插件。在上面的搜索框里找一份装上即可。'
        : '未检测到 DSH 安装，已安装列表为空。'));
    } else {
      const list = (data.plugins || []);
      const withPayload = list.filter((item) => item.presets || item.skills);
      const withAgents = list.filter((item) => asArray(item.agents).length);
      // 「装了插件但对话里没变化」过去只能靠人猜。这里按**判定**把数字分成三类摆出来：
      // 给对话带来预设/技能的、只在 DSH 宿主里生效的、什么都没有的。中间那一类是
      // `graph-memory` / `dsh-agent-teams` / `dsh-turn-rewind` 的真实形态。
      const hosts = list.filter((item) => contribKind(item) === 'host');
      const blanks = list.filter((item) => contribKind(item) === 'empty');
      const parts = [(data.isolated ? '项目内 profile ' : 'profile ')
        + (data.profile || '') + ' 下共 ' + list.length + ' 个插件：'
        + withPayload.length + ' 个给对话带来 Agent 预设或技能手册（'
        + withAgents.length + ' 个可直接应用到会话）'];
      if (hosts.length) {
        parts.push(hosts.length + ' 个只在 DSH 宿主里生效，对这里的对话没有作用');
      }
      if (blanks.length) parts.push(blanks.length + ' 个没有可读内容');
      dom.hubInstalled.append(el('p', 'muted', parts.join('；') + '。'));
      list.forEach((item) => {
        const row = el('div', 'hub-installed-row');
        row.append(el('span', 'hub-item-name', item.name));
        row.append(el('span', 'muted', 'v' + (item.version || '?')));
        if (item.missing) row.append(el('span', 'check-badge check-bad', '目录缺失'));
        else if (!item.bundled) row.append(el('span', 'check-badge', '未在 bundles'));
        if (item.presets) row.append(el('span', 'hub-pill', '预设 ' + item.presets));
        if (item.skills) row.append(el('span', 'hub-pill', '技能 ' + item.skills));
        // 这个插件带来的 Agent 就地给一个「应用到会话」——只写"预设 2"的话，
        // 用户还得自己回对话页、拉开下拉框、按名字找，而名字是插件起的。
        const agents = asArray(item.agents);
        if (agents.length) {
          row.append(el('span', 'muted', '可应用到会话：'));
          agents.forEach((agent) => {
            row.append(applyButton(agent.id, agent.label));
            row.append(el('span', 'muted hub-apply-label', agent.label));
          });
        } else if (item.presets) {
          // 有预设却一个都没进目录：原因**就地**给出。
          // 这句话原来指向状态区里那份「已发现但未采纳」清单（写法是"原因…见下"），
          // 那一块拆掉之后，指针就落到了空处。而指到空处的症状比不说更糟：它读起来
          // 像界面漏渲染了一段，于是没人会去怀疑是插件自己的声明有问题。
          // （上面那句故意写成省略号版：契约用例按**字面**禁掉原措辞，分不出"声称"
          //  与"引用"，所以这里引用它也不能用原字面。）
          const why = ignoredReason(item.name);
          row.append(el('span', 'muted', why
            ? '预设未采纳为 Agent：' + why
            : '预设未采纳为 Agent（接口的 ignored 里没有给出原因）'));
        }
        // 判定行：**每一个**插件都有一句，包括什么都不做的那种。单独占一行
        // （`flex-basis: 100%`）是因为它是这一行里最长的文本，挤在中间会把插件名、
        // 版本与卸载按钮推散。理由与判定都来自后端（`dshplugins.contribution`），
        // 前端只负责显示——两处各算一遍，迟早有一处说错。
        const verdict = item.contribution;
        if (verdict && verdict.summary) {
          row.append(el('span', 'hub-verdict muted',
            verdict.summary + (verdict.detail ? '：' + verdict.detail : '')));
        }
        const landing = asArray(item.landing);
        if (landing.length) {
          row.append(el('span', 'hub-verdict muted', '同一能力本平台已有原生落点：'
            + landing.map((entry) => entry.label + '（'
              + asArray(entry.tools).join('、') + '）').join('、')
            + '，不需要为它装插件。'));
        }
        if (view.pendingOnly) { /* no-op：保留分支位以便将来按状态过滤 */ }
        const remove = el('button', 'link-btn hub-remove', '卸载');
        remove.type = 'button';
        remove.dataset.remove = item.name;
        row.append(remove);
        dom.hubInstalled.append(row);
      });
      if (payload && payload.pending) {
        dom.hubInstalled.append(el('p', 'muted', '队列中还有 ' + payload.pending
          + ' 个操作正在/等待执行。'));
      }
    }
    renderHubOps(payload);
    syncOpsPolling(payload);
  }

  /** 某个插件"装了却没贡献出 Agent"的原因，取自 `/dsh` 的 `ignored`。
   *
   *  服务端把理由放在一整张平表里（`{plugin, name, kind, reason}`），这里按插件名
   *  取第一条。取不到时**如实说没有**——这一条返回空串之后，调用点会印一句
   *  "接口里没有给出原因"，而不是留一句"见下"：那会指向一个不存在的段落。
   */
  function ignoredReason(name) {
    const rows = asArray((view.dsh || {}).ignored);
    const hit = rows.filter((item) => item.plugin === name)[0];
    return hit ? String(hit.reason || '') : '';
  }

  function renderHubOps(payload) {
    if (!dom.hubOps) return;
    clear(dom.hubOps);
    const ops = (payload && payload.ops) || view.hubOps || [];
    setHubOpsHint(ops);
    if (!ops.length) {
      dom.hubOps.append(ui().emptyBlock('还没有插件操作记录。'));
      return;
    }
    ops.forEach((op) => {
      const row = el('div', 'hub-op' + (op.status === 'failed' ? ' bad' : '')
        + (op.status === 'running' ? ' running' : ''));
      const head = el('div', 'hub-op-head');
      head.append(el('span', 'hub-op-action',
        op.action === 'install' ? '安装' : '卸载'));
      head.append(el('span', 'hub-item-name', op.target));
      head.append(el('span', 'muted', op.status === 'running' ? '执行中…'
        : (op.status === 'done' ? '成功' : '失败')
          + (op.exitCode === null || op.exitCode === undefined
            ? '' : '（退出码 ' + op.exitCode + '）')));
      head.append(el('span', 'spacer'));
      head.append(el('span', 'muted', fmtTime(op.createdAt)));
      row.append(head);
      if (op.reason) row.append(el('p', 'hub-op-reason', op.reason));
      if (op.output) {
        const detail = el('details', 'hub-op-output');
        detail.append(el('summary', 'muted', '命令行输出'));
        const pre = el('pre', 'hub-op-pre');
        pre.textContent = op.output;
        detail.append(pre);
        row.append(detail);
      }
      dom.hubOps.append(row);
    });
  }

  /** 折叠头上的那一行摘要。
   *
   *  它是**折起来之后唯一的信号**：标题右边挂个数字，才知道里面还有没有东西可看。
   *  所以 "0 条" 也要写出来，而不是留空——留空与"这一块还没加载"长得一模一样。
   */
  function setHubOpsHint(ops) {
    if (!dom.hubOpsHint) return;
    const live = ops.filter(opIsLive).length;
    dom.hubOpsHint.textContent = ops.length
      ? '共 ' + ops.length + ' 条' + (live ? ' · 执行中 ' + live : '')
      : '暂无记录';
  }

  /** 一键清空插件操作记录。
   *
   *  **只清已结束的**，与后端的默认范围一致（见 `store.clear_plugin_ops`）。
   *  执行中那几条是"这台机器上此刻正在跑一次 pnpm"的唯一痕迹，而且 `finish_plugin_op`
   *  之后还要回来更新它——清掉它会让一次跑到一半的安装变成查无此事。所以确认框里
   *  把"会删几条、会留几条"写成数字，让"留"这件事是被看见的，而不是被默认的。
   */
  async function clearHubOps(button) {
    const ops = asArray(view.hubOps);
    const live = ops.filter(opIsLive);
    const done = ops.length - live.length;
    if (!ops.length) {
      showNotice(dom.hubMessage, '还没有可清空的操作记录。', 'error');
      return;
    }
    // 原生确认框不认 Markdown，所以这里**不能**写 `**已结束**`——它会被原样念出来。
    // 强调只能在代码注释里写（上面那一段就是）。
    const confirmed = window.confirm('将删除 ' + done + ' 条已结束的操作记录'
      + '（成功 / 失败，含各自的命令行输出）。'
      + (live.length
        ? '\n\n执行中的 ' + live.length + ' 条会保留：它们还没跑完，清掉就再也看不出'
          + '这里正在装什么。'
        : '')
      + '\n\n清空是跨用户的动作——安装队列是整个部署共享的，清掉就等于把别人的'
      + '历史也删了。\n\n确认继续？');
    if (!confirmed) return;
    setButtonBusy(button, true, '清空中…');
    try {
      const data = await ui().apiPost('/hub/ops/clear', {});
      showNotice(dom.hubMessage, '已清空 ' + (data.deleted || 0) + ' 条'
        + (data.kept ? '，保留 ' + data.kept + ' 条（未结束）' : '') + '。', 'ok');
    } catch (error) {
      showNotice(dom.hubMessage, '清空失败：' + error.message, 'error');
    } finally {
      setButtonBusy(button, false);
      // 重拉而不是就地过滤：删掉几条之后列表长度变了，而后端才是唯一的事实来源。
      await loadHubInstalled();
    }
  }

  async function loadHubInstalled() {
    try {
      const data = await ui().apiGet('/hub/installed');
      view.dsh = data.dsh || view.dsh;
      view.hubPending = data.pending || 0;
      view.hubInstalled = data;
      view.hubOps = data.ops || [];
    } catch (error) {
      view.hubInstalled = null;
      showNotice(dom.hubMessage, '已安装列表加载失败：' + error.message, 'error');
    }
    // 第一次进入时把"当前已有的东西"记成基线：不记的话，历史里那些早就装完的
    // 操作会在第一次轮询时被当成"刚刚装完"，然后去刷一遍会话并弹提示。
    if (view.knownAgentIds === null) view.knownAgentIds = await knownAgentIds();
    if (view.announcedOps === null) {
      view.announcedOps = {};
      view.hubOps.forEach((op) => { view.announcedOps[op.id] = true; });
    }
    renderHubInstalled(view.hubInstalled);
    // 首次进入就要给出"这一屏该怎么用"：目录规模、首次拉取要十几秒。
    // 不调它的话 `#hub-meta` 会一直停在"尚未加载"，用户以为面板坏了。
    renderHub();
  }

  async function knownAgentIds() {
    try {
      const data = await ui().apiGet('/agents');
      return new Set(asArray(data.agents).map((item) => item.id));
    } catch (error) { return new Set(); }
  }

  /* ------------------------------------------------- 安装队列的跟进 ----
   *
   * 安装是**入队即返回**的（见 `hub.OpRunner`）：一次 `dsh plugin add` 要跑 pnpm
   * 与 git，几十秒到几分钟。控制台如果只显示"已入队"就不再动，用户永远看不到它
   * 装完，也就永远看不到新插件带来的 Agent ——"安装后应用到会话"这件事根本无从发生。
   *
   * 所以队列里有活就轮询，空了就停：不做无脑定时器，避免一个没人看的页面每 3 秒
   * 敲一次接口。
   */

  const OP_POLL_MS = 3000;

  function opIsLive(op) {
    const status = String((op && op.status) || '').toLowerCase();
    return status === 'running' || status === 'queued' || status === 'pending';
  }

  function syncOpsPolling(payload) {
    const ops = (payload && payload.ops) || view.hubOps || [];
    const live = Boolean(payload && payload.pending) || ops.some(opIsLive);
    if (live) {
      if (!view.opsTimer) view.opsTimer = setInterval(pollOps, OP_POLL_MS);
    } else if (view.opsTimer) {
      clearInterval(view.opsTimer);
      view.opsTimer = null;
    }
  }

  function stopOpsPolling() {
    if (view.opsTimer) clearInterval(view.opsTimer);
    view.opsTimer = null;
  }

  async function pollOps() {
    if (!window.PentestAuth || !window.PentestAuth.getUser()) { stopOpsPolling(); return; }
    let data = null;
    try { data = await ui().apiGet('/hub/ops'); }
    catch (error) { stopOpsPolling(); return; }   // 网络断了就停，不要每 3 秒弹一条错误
    const fresh = announceFinishedOps(data.ops || []);
    await loadHubInstalled();
    if (fresh.length) await reportNewAgents(fresh);
  }

  /** 找出这一轮新完成的操作。返回 done 的那些；首次调用只建基线，不报。 */
  function announceFinishedOps(ops) {
    if (view.announcedOps === null) {
      view.announcedOps = {};
      ops.forEach((op) => { view.announcedOps[op.id] = true; });
      return [];
    }
    const seen = view.announcedOps;
    const fresh = ops.filter((op) => !seen[op.id]);
    fresh.forEach((op) => { seen[op.id] = true; });
    return fresh.filter((op) => String(op.status || '').toLowerCase() === 'done');
  }

  /** 装完之后：把会话入口的 Agent 目录拉新，并把**新出现的** Agent 报出来。
   *
   *  只报"新出现的"而不是"这次装的那个插件带了什么"：插件名与 Agent 之间的对应
   *  关系要从目录里推，而目录还会受 relevance 判定影响（有的插件装了不贡献 Agent）。
   *  直接比前后两份目录，报出来的就一定是真的可选项。
   */
  async function reportNewAgents(finished) {
    const before = view.knownAgentIds || new Set();
    const after = await knownAgentIds();
    view.knownAgentIds = after;
    const chat = window.PentestChat;
    if (chat && chat.loadOptions) await chat.loadOptions();
    const added = [...after].filter((id) => !before.has(id));
    if (!added.length) {
      // 装完没多出任何 Agent 时，原因必须说准。这里原来是
      // "（该插件可能不提供预设，或在 DSH 侧还没重启加载）"——后半句是**假的**：
      // 宿主插件要的东西不在这条路上，重启 DSH 也不会变。原因从刚拉新的已安装
      // 清单里取（`contribution.kind === 'host'` 就是那种包）。
      showNotice(dom.hubMessage, noAgentReason(finished), 'warn');
      return;
    }
    let catalog = [];
    try { catalog = asArray((await ui().apiGet('/agents')).agents); } catch (error) { catalog = []; }
    const box = el('div', 'hub-applied');
    box.append(el('p', 'muted', '安装完成，新增 ' + added.length
      + ' 个可用 Agent。选一个应用到当前会话：'));
    const row = el('div', 'hub-applied-row');
    added.forEach((id) => {
      const item = catalog.filter((entry) => entry.id === id)[0] || { id: id, label: id };
      row.append(applyButton(item.id, item.label));
      row.append(el('span', 'muted', item.label));
    });
    box.append(row);
    box.append(el('p', 'muted', '插件装在项目自己的插件目录里，本平台直接读磁盘，'
      + '所以 Agent 预设与技能手册立刻就可用——不需要重启任何东西。'));
    clear(dom.hubMessage);
    // 这一格平时由 showNotice 管：它设 className，并在文本为空时加 hidden。
    // 这里直接塞 DOM，就必须自己把这两件事一起做掉——否则上一次
    // `showNotice(dom.hubMessage, '')` 留下的 `hidden` 会让这块刚装好的提示
    // **完全看不见**，而用户只会得出"装完了但什么也没发生"这个结论。
    dom.hubMessage.className = 'notice ok';
    dom.hubMessage.append(box);
  }

  /** 插件的判定类别，取不到时返回空串（老接口没有这个字段）。 */
  function contribKind(item) {
    return String(((item || {}).contribution || {}).kind || '');
  }

  /** 装完却没多出 Agent 时的原因。
   *
   *  优先说**那个插件自己**的判定：安装目标与插件名做子串匹配（目录里的
   *  `installTarget` 可能是 `@scope/name`、`github:owner/repo` 或带版本号的形状，
   *  而插件名总是它的一段）。匹配不到就退回一句**准确**的概述，绝不提"重启"。 */
  function noAgentReason(finished) {
    const list = asArray((view.dsh || {}).plugins);
    const targets = asArray(finished)
      .map((op) => String((op || {}).target || '').toLowerCase()).filter(Boolean);
    const hit = list.filter((item) => {
      const name = String(item.name || '').toLowerCase();
      return name && targets.some((text) => text.indexOf(name) >= 0
        || name.indexOf(text) >= 0);
    })[0];
    if (hit && hit.contribution && hit.contribution.detail) {
      return '安装完成，但对话里不会多出 Agent——' + hit.contribution.detail;
    }
    const hosts = list.filter((item) => contribKind(item) === 'host');
    if (hosts.length) {
      return '安装完成，但没有新增可用的 Agent 预设。已安装的插件里有 '
        + hosts.length + ' 个只在 DSH 宿主里生效——它们挂在平台自己的插件容器里，'
        + '而对话加载的是另一个 profile，所以它们不会改变对话行为（重启 DSH 也一样）。';
    }
    return '安装完成，但没有新增可用的 Agent 预设：这个包既没有 preset/，'
      + '也没有 skills/，本平台没有可读的内容。';
  }

  function applyButton(agentId, label) {
    const button = el('button', 'link-btn hub-apply', '应用到会话');
    button.type = 'button';
    button.dataset.applyAgent = agentId || '';
    button.title = '把当前对话会话的 Agent 换成「' + (label || agentId) + '」';
    return button;
  }

  /** 把某个 Agent 预设应用到**当前对话会话**。
   *
   *  走对话模块自己的入口（`PentestChat.applyAgent`），不在这里直接改下拉框：
   *  "已有会话就落库、新会话等第一条消息"这两条分支在那边已经写好了，这边另走
   *  一条就等于多一份会分叉的保存逻辑。
   */
  async function applyAgentToSession(button) {
    const agentId = button.dataset.applyAgent;
    const chat = window.PentestChat;
    if (!chat || !chat.applyAgent) {
      showNotice(dom.hubMessage, '对话模块还没加载，先切到「控制台」页签点一下再回来。', 'error');
      return;
    }
    setButtonBusy(button, true, '应用中…');
    try {
      const note = await chat.applyAgent(agentId);
      showNotice(dom.hubMessage, note || ('已把当前会话的 Agent 换成「' + agentId + '」，下一轮对话生效。'),
        note ? 'warn' : 'ok');
    } catch (error) {
      showNotice(dom.hubMessage, '应用失败：' + error.message, 'error');
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function installPlugin(target, button) {
    if (!target) return;
    const confirmed = window.confirm(
      '将执行：dsh plugin add ' + target + '\n\n'
      + '· 装进项目自己的插件目录，与电脑上的 DSH 隔离；\n'
      + '· 会在项目目录里跑 pnpm/git，改的是项目自己的 package.json 与依赖目录；\n'
      + '· 电脑上那份 DSH 完全不受影响；装完本平台立刻可用，不需要重启。\n\n确认继续？');
    if (!confirmed) return;
    setButtonBusy(button, true, '入队中…');
    try {
      const data = await ui().apiPost('/hub/install', { target });
      showNotice(dom.hubMessage, '已入队（操作号 ' + (data.op && data.op.id)
        + '）。安装是串行执行的；装的是项目内的插件目录，与电脑上的 DSH 无关。', 'ok');
    } catch (error) {
      showNotice(dom.hubMessage, '入队失败：' + error.message, 'error');
    } finally {
      setButtonBusy(button, false);
      loadHubInstalled();
    }
  }

  async function removePlugin(name, button) {
    const confirmed = window.confirm('将执行：dsh plugin remove ' + name
      + '\n\n· 从项目自己的插件目录卸载，不影响电脑上的 DSH；\n'
      + '· 电脑上那份就算装了同名插件也不会被碰。\n\n确认继续？');
    if (!confirmed) return;
    setButtonBusy(button, true, '入队中…');
    try {
      const data = await ui().apiPost('/hub/remove', { name });
      showNotice(dom.hubMessage, '已入队卸载（操作号 ' + (data.op && data.op.id) + '）。', 'ok');
    } catch (error) {
      showNotice(dom.hubMessage, '入队失败：' + error.message, 'error');
    } finally {
      setButtonBusy(button, false);
      loadHubInstalled();
    }
  }

  async function runDiagnostics() {
    setButtonBusy(dom.hubDiag, true, '探测中…');
    clear(dom.hubDiagResult);
    try {
      const data = await ui().apiGet('/hub/diagnostics');
      view.hubDiag = data;
      (data.checks || []).forEach((check) => {
        const row = el('div', 'hub-diag-row' + (check.ok ? '' : ' bad'));
        row.append(el('span', 'hub-diag-key', check.key));
        row.append(el('span', 'hub-diag-url muted', check.display || check.url));
        row.append(el('span', 'spacer'));
        row.append(el('span', 'muted', check.ok
          ? ('OK · ' + check.ms + ' ms') : ('不可达 · ' + (check.reason || ''))));
        dom.hubDiagResult.append(row);
      });
      const summary = el('p', 'muted', data.summary
        + '；代理 ' + (data.proxy || '（未配置，直连）')
        + '；CLI ' + (data.cli || '（未找到）'));
      dom.hubDiagResult.append(summary);
    } catch (error) {
      dom.hubDiagResult.append(ui().errorBlock('自检失败：' + error.message));
    } finally {
      setButtonBusy(dom.hubDiag, false);
    }
  }

  function bindHubEvents() {
    if (dom.hubSearch) {
      dom.hubSearch.addEventListener('click', () => {
        view.hub.query = dom.hubQuery.value.trim();
        view.hub.category = dom.hubCategory.value || '';
        view.hub.verified = Boolean(dom.hubVerified.checked);
        searchHub(false);
      });
    }
    if (dom.hubQuery) {
      dom.hubQuery.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        dom.hubSearch.click();
      });
    }
    if (dom.hubMore) dom.hubMore.addEventListener('click', () => searchHub(true));
    if (dom.hubDiag) dom.hubDiag.addEventListener('click', runDiagnostics);
    if (dom.hubInstalledRefresh) dom.hubInstalledRefresh.addEventListener('click', () => {
      loadHubInstalled();
    });
    // 安装/卸载都走事件委托：结果列表是动态重建的，逐项绑定会随每次搜索泄漏监听器。
    if (dom.hubResults) {
      dom.hubResults.addEventListener('click', (event) => {
        const button = event.target.closest ? event.target.closest('[data-install]') : null;
        if (button) installPlugin(button.dataset.install, button);
      });
    }
    if (dom.hubInstalled) {
      dom.hubInstalled.addEventListener('click', (event) => {
        const target = event.target.closest ? event.target.closest('[data-remove],[data-apply-agent]') : null;
        if (!target) return;
        // 「应用到会话」优先判：两个属性不会同时出现，但顺序写清楚比留给人猜好
        if (target.dataset.applyAgent !== undefined && target.dataset.applyAgent !== null
            && target.hasAttribute('data-apply-agent')) {
          applyAgentToSession(target);
          return;
        }
        removePlugin(target.dataset.remove, target);
      });
    }
    // 「清空记录」是**静态按钮**（在 index.html 上，不在渲染函数里），所以逐项绑定
    // 不会累积监听器——与上面两处列表不同，它们每帧重建，必须用委托。
    if (dom.hubOpsClear) {
      dom.hubOpsClear.addEventListener('click', () => clearHubOps(dom.hubOpsClear));
    }
  }

  async function loadUsers(force) {
    if (!window.PentestAuth || !window.PentestAuth.isAdmin()) {
      view.users = []; view.usersError = null; renderUsers(); return;
    }
    try {
      const data = await ui().apiGet('/users');
      view.users = Array.isArray(data.users) ? data.users : [];
      view.usersError = null;
    } catch (error) {
      view.users = [];
      view.usersError = error.message;
    }
    renderUsers();
  }

  async function load(force) {
    if (view.inflight && !force) return view.inflight;
    // 技能目录与模型配置一起开拉：切到 Skills 那一组时才拉的话，那一次点击要
    // 等一个往返，而它恰好是唯一需要"立刻看到列表"的一组（看的就是有哪些手册）。
    if (window.PentestSkills) window.PentestSkills.prefetch();
    if (dom.meta) dom.meta.textContent = '正在加载模型配置…';
    if (dom.list) { clear(dom.list); dom.list.append(ui().emptyBlock('正在加载模型配置…')); }
    view.inflight = (async () => {
      try {
        const data = await ui().apiGet('/models');
        view.profiles = Array.isArray(data.profiles) ? data.profiles : [];
        view.presets = Array.isArray(data.presets) ? data.presets : [];
        view.activeId = data.activeId === undefined ? null : data.activeId;
        view.modelsError = null;
        view.loaded = true;
        fillPresetOptions();
      } catch (error) {
        view.profiles = []; view.presets = []; view.activeId = null;
        view.modelsError = error.message;
        view.loaded = false;
      }
      view.tests = {};
      view.pendingDelete = null;
      renderModels();
      updateMeta();
      await loadAgentModes();
      await loadPolicies();
      await loadReviewMode();
      await loadDshStatus();
      await loadMcp();
      await loadUsers(force);
    })();
    try { await view.inflight; } finally { view.inflight = null; }
  }

  /** 登录后第一次进入设置页签才拉数据；未登录不请求任何设置接口。 */
  function ensureLoaded() {
    if (!window.PentestAuth || !window.PentestAuth.getUser()) return;
    if (view.loaded) return;
    load(false);
  }

  /** 退出登录 / 会话失效：清空上一个用户留下的数据视图（管理员列表不得残留）。 */
  function onSignedOut() {
    view.profiles = []; view.presets = []; view.activeId = null; view.modelsError = null;
    view.modes = []; view.activeModeId = ''; view.modeError = null; view.modeBusy = null;
    view.users = []; view.usersError = null; view.tests = {};
    view.policies = []; view.activePolicyId = ''; view.policyError = null; view.policyBusy = null;
    view.review = null; view.reviewError = null;
    view.dsh = null; view.dshError = null; view.hubInstalled = null; view.hubOps = [];
    view.hubDiag = null;
    // 退出登录必须停掉安装队列轮询：它带着上一个用户的凭据在跑，而 next user
    // 进来看见"队列里有活"会得到一个自己没发起过的操作。
    stopOpsPolling();
    view.announcedOps = null; view.knownAgentIds = null;
    view.hub.items = []; view.hub.total = 0; view.hub.hasMore = false;
    view.hub.error = null; view.hub.busy = false;
    view.editingId = null; view.pendingDelete = null; view.pendingUserDelete = null; view.pendingPasswordUser = null;
    view.agentEditingId = null; view.agentBusy = null; view.pendingAgentDelete = null;
    view.agentHandleTouched = false; view.skillChoices = null;
    view.mcp = null; view.mcpError = null; view.mcpBusy = null;
    view.mcpEditingId = null;
    view.inflight = null; view.loaded = false;
    if (dom.list) { clear(dom.list); dom.list.append(ui().emptyBlock('登录后加载模型配置。')); }
    if (dom.modeList) { clear(dom.modeList); dom.modeList.append(ui().emptyBlock('登录后加载 Agent 预设。')); }
    if (dom.modeCurrent) dom.modeCurrent.textContent = '尚未加载';
    if (dom.policyList) { clear(dom.policyList); dom.policyList.append(ui().emptyBlock('登录后加载权限档。')); }
    if (dom.policyCurrent) dom.policyCurrent.textContent = '尚未加载';
    if (dom.hubResults) clear(dom.hubResults);
    if (dom.hubInstalled) clear(dom.hubInstalled);
    if (dom.hubOps) clear(dom.hubOps);
    if (dom.hubDiagResult) clear(dom.hubDiagResult);
    if (dom.hubMeta) dom.hubMeta.textContent = '尚未加载';
    if (dom.mcpList) { clear(dom.mcpList); dom.mcpList.append(ui().emptyBlock('登录后加载 MCP 服务。')); }
    if (dom.mcpMeta) dom.mcpMeta.textContent = '尚未加载';
    showNotice(dom.mcpMessage, '');
    showNotice(dom.mcpFormMessage, '');
    resetMcpForm();
    if (dom.usersList) clear(dom.usersList);
    if (dom.usersCard) dom.usersCard.classList.add('hidden');
    if (dom.meta) dom.meta.textContent = '尚未加载';
    showNotice(dom.formMessage, '');
    showNotice(dom.userMessage, '');
    showNotice(dom.hubMessage, '');
    // 退出登录后 `view.dsh` 已经是 null，读过它的那一处必须重画一次——
    // 否则上一个用户装过什么会留在页面上（`clear()` 只清空，不给空态文案）。
    renderHubInstalled(null);
    resetModelForm();
    resetAgentForm();
  }

  /** 重新登录后强制刷新一次，避免显示上一个会话的配置。 */
  function reload() { return load(true); }

  function init() {
    if (view.bound) return;
    cacheDom();
    if (!dom.list) return;
    view.bound = true;
    bindModelEvents();
    bindModeEvents();
    bindUserEvents();
    bindPolicyEvents();
    bindHubEvents();
    bindNavEvents();
    bindThemeEvents();
    bindAppearanceEvents();
    bindAgentFormEvents();
    bindMcpEvents();
    bindDshEvents();
    if (dom.tab) dom.tab.addEventListener('click', ensureLoaded);
    applyKeySource();
    resetModelForm();
    resetAgentForm();
    dom.usersCard.classList.add('hidden');
    showGroup(view.group);
    if (dom.list) { clear(dom.list); dom.list.append(ui().emptyBlock('登录后加载模型配置。')); }
    if (dom.modeList) { clear(dom.modeList); dom.modeList.append(ui().emptyBlock('登录后加载 Agent 预设。')); }
    if (dom.policyList) { clear(dom.policyList); dom.policyList.append(ui().emptyBlock('登录后加载权限档。')); }
    if (dom.mcpList) { clear(dom.mcpList); dom.mcpList.append(ui().emptyBlock('登录后加载 MCP 服务。')); }
    renderThemes();
    renderAccents();
    renderFrosts();
    renderAlpha();
    renderHubInstalled(null);
  }

  window.PentestSettings = { init, load, reload, ensureLoaded, onSignedOut, renderUsers,
                             renderAgentModes, renderThemes, renderAccents, renderFrosts,
                             renderAlpha,
                             showGroup, enter: () => showGroup(view.group),
                             loadHubInstalled, searchHub };
})();
