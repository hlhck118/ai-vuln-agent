'use strict';
/* ============================================================================
 * 登录认证 —— 登录门 / 顶栏用户区 / 修改密码 / CSRF / 401 回登录页
 *
 * 设计要点：
 *   * 本文件位于 app.js 之前，第一件事是置 window.PentestConsoleGate，
 *     app.js 读到后不再自行启动；启动权交给 boot.js（先查登录态再决定渲染什么）。
 *   * 未登录时工作台外壳始终 hidden，不请求任何业务接口，避免"先渲染再遮罩"。
 *   * CSRF 令牌只放内存（auth.csrfToken），apiPost 通过 csrfHeaders() 自动带上。
 *   * 任何带 CSRF 的业务接口返回 401 → handleUnauthorized() 立刻切回登录门。
 * 安全：接口文本一律 textContent 落 DOM，不拼 HTML 字符串。
 * ==========================================================================*/

/* 标记工作台由 boot.js 接管启动（必须在 app.js 解析前生效）。 */
window.PentestConsoleGate = true;

(function () {
  const MIN_PASSWORD_LENGTH = 10;
  const ROLE_LABEL = { admin: '管理员', operator: '操作员' };
  /** 这些接口用 401 表达业务结果（未登录 / 密码错），不触发"会话失效跳登录门"。 */
  const EXEMPT_401 = ['/auth/state', '/auth/bootstrap', '/auth/login', '/auth/me',
    '/auth/logout', '/auth/password'];

  const auth = {
    csrfToken: null,        // 内存中的 CSRF 令牌
    user: null,             // { id, username, role }
    sessionExpiresAt: null,
    needsSetup: false,      // 后端还没有任何用户
    passwordPolicy: '',     // 后端下发的口令策略说明
    throttle: '',           // 后端下发的登录节流说明
    active: false,          // 工作台是否已进入
  };
  const listeners = [];     // (user|null) => void，boot.js 据此启动/收起工作台
  let bound = false;
  let submitting = false;

  const byId = (id) => document.getElementById(id);
  const dom = {};

  function cacheDom() {
    dom.gate = byId('auth-gate');
    dom.shell = byId('console-shell');
    dom.loading = byId('auth-loading');
    dom.setupForm = byId('auth-setup-form');
    dom.setupUser = byId('auth-setup-username');
    dom.setupPass = byId('auth-setup-password');
    dom.setupConfirm = byId('auth-setup-confirm');
    dom.setupMessage = byId('auth-setup-message');
    dom.setupSubmit = byId('auth-setup-submit');
    dom.loginForm = byId('auth-login-form');
    dom.loginUser = byId('auth-login-username');
    dom.loginPass = byId('auth-login-password');
    dom.loginMessage = byId('auth-login-message');
    dom.loginSubmit = byId('auth-login-submit');
    dom.setupHint = byId('auth-setup-hint');
    dom.loginHint = byId('auth-login-hint');
    dom.userBadge = byId('topbar-user');
    dom.logoutBtn = byId('logout-btn');
    dom.passwordOpen = byId('password-open');
    dom.overlay = byId('password-overlay');
    dom.passwordForm = byId('password-form');
    dom.passwordCurrent = byId('password-current');
    dom.passwordNew = byId('password-new');
    dom.passwordConfirm = byId('password-confirm');
    dom.passwordMessage = byId('password-message');
    dom.passwordSubmit = byId('password-submit');
    dom.passwordClose = byId('password-close');
    dom.passwordCancel = byId('password-cancel');
  }

  /* ------------------------------------------------------------ 小工具 ---- */

  function showMessage(node, text, kind) {
    if (!node) return;
    node.textContent = text ? String(text) : '';
    node.className = 'auth-message' + (kind ? ' ' + kind : '');
    node.classList.toggle('hidden', !text);
  }

  function setBusy(button, busy, busyText) {
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

  /** 只显示登录门里的一种形态：loading / setup / login。 */
  function showView(view, message, kind) {
    if (!dom.gate) return;
    dom.loading.classList.toggle('hidden', view !== 'loading');
    dom.setupForm.classList.toggle('hidden', view !== 'setup');
    dom.loginForm.classList.toggle('hidden', view !== 'login');
    if (view === 'setup') {
      showMessage(dom.setupMessage, message, kind || 'error');
      if (dom.setupUser && !dom.setupUser.value) dom.setupUser.focus();
    } else if (view === 'login') {
      showMessage(dom.loginMessage, message, kind || 'error');
      showMessage(dom.setupMessage, '');
      if (dom.loginUser && !dom.loginUser.value) dom.loginUser.focus();
    }
  }

  function renderUserBadge(user) {
    if (!dom.userBadge) return;
    if (!user) { dom.userBadge.textContent = '未登录'; return; }
    const role = ROLE_LABEL[String(user.role || '').toLowerCase()] || user.role || '未知角色';
    dom.userBadge.textContent = `${user.username || '未知用户'}（${role}）`;
  }

  function applyAuthState(data) {
    auth.csrfToken = (data && data.csrfToken) || null;
    auth.user = (data && data.user) || null;
    auth.sessionExpiresAt = (data && data.sessionExpiresAt) || null;
    auth.needsSetup = Boolean(data && data.needsSetup);
    auth.passwordPolicy = (data && data.passwordPolicy) || auth.passwordPolicy || '';
    auth.throttle = (data && data.throttle) || auth.throttle || '';
    renderHints();
  }

  function renderHints() {
    if (dom.setupHint) {
      dom.setupHint.textContent = '首次使用需要创建管理员。' + (auth.passwordPolicy ? '口令要求：' + auth.passwordPolicy : `密码至少 ${MIN_PASSWORD_LENGTH} 位。`);
    }
    if (dom.loginHint) {
      dom.loginHint.textContent = '工作台需要登录后才能访问。' + (auth.throttle ? auth.throttle + '。' : '');
    }
  }

  /** 与后端 password_problem 同规则的前端预检，避免白跑一次请求。 */
  function passwordProblem(password) {
    const value = String(password === undefined || password === null ? '' : password);
    if (value.length < MIN_PASSWORD_LENGTH) return `密码至少 ${MIN_PASSWORD_LENGTH} 位`;
    if (value.trim() !== value) return '密码首尾不能有空白字符';
    const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter((re) => re.test(value)).length;
    if (classes < 2) return '密码需包含大小写字母、数字、符号中的至少两类';
    if (new Set(value).size < 4) return '密码过于简单（重复字符太多）';
    return '';
  }

  /** 进入工作台：收起登录门，通知 boot.js 启动全部页签。 */
  function enterConsole() {
    auth.active = true;
    dom.gate.classList.add('hidden');
    dom.shell.classList.remove('hidden');
    renderUserBadge(auth.user);
    listeners.forEach((fn) => { try { fn(auth.user); } catch (error) { window.PentestUI.showError('界面初始化失败：' + error.message); } });
  }

  /** 回到登录门：清空令牌与用户，工作台整体隐藏（不保留上一个用户的数据视图）。 */
  function leaveConsole(message) {
    const wasActive = auth.active;
    auth.active = false;
    auth.csrfToken = null;
    auth.user = null;
    auth.sessionExpiresAt = null;
    dom.gate.classList.remove('hidden');     // 登录门必须重新显示，否则会话失效后是白屏
    dom.shell.classList.add('hidden');
    dom.overlay.classList.add('hidden');
    showMessage(dom.passwordMessage, '');
    renderUserBadge(null);
    showView('login', message || '', 'error');
    if (wasActive) listeners.forEach((fn) => { try { fn(null); } catch (error) { /* 收起失败不影响回登录门 */ } });
  }

  /* -------------------------------------------------------------- 接口 ---- */

  function csrfHeaders(base) {
    const headers = Object.assign({}, base || {});
    if (auth.csrfToken && !headers['X-CSRF-Token']) headers['X-CSRF-Token'] = auth.csrfToken;
    return headers;
  }

  /** 业务接口 401 → 立即回登录门；白名单接口的 401 由各自表单处理。 */
  function handleUnauthorized(path) {
    const target = String(path || '');
    if (EXEMPT_401.some((prefix) => target === prefix || target.startsWith(prefix + '/'))) return;
    if (!auth.active) return;
    leaveConsole('登录状态已失效，请重新登录。');
  }

  async function start() {
    if (!dom.gate) cacheDom();
    bind();
    showView('loading');
    try {
      const data = await window.PentestUI.apiGet('/auth/state');
      applyAuthState(data);
      if (data && data.needsSetup) { showView('setup'); return; }
      if (!data || !data.authenticated || !auth.user) { showView('login'); return; }
      // --no-auth：state 已回"已认证 + 本地调试用户"，且本来就没有会话/CSRF 体系。
      // 再补一次 /auth/me 只会吃到 401 把工作台弹回登录门（实测：state 200 /
      // me 401，界面显示"登录状态已失效"）。服务端两个端点已同答一份视图（后端
      // _no_auth_state），这里的前置挡道保证旧服务端也不会把工作台卡死。
      if (data.authRequired === false) { enterConsole(); return; }
      if (!auth.csrfToken) {
        const ok = await refreshSession();
        if (!ok) return;
      }
      enterConsole();
    } catch (error) {
      showView('login', '无法连接控制面：' + error.message);
    }
  }

  /** 用 /auth/me 补一次令牌（auth/state 未回传令牌时的兜底）。 */
  async function refreshSession() {
    try {
      const data = await window.PentestUI.apiGet('/auth/me');
      applyAuthState(data);
      return true;
    } catch (error) {
      if (error.status === 401) { showView('login', '登录状态已失效，请重新登录。'); return false; }
      showView('login', '无法连接控制面：' + error.message);
      return false;
    }
  }

  async function submitSetup(event) {
    event.preventDefault();
    if (submitting) return;
    const username = String(dom.setupUser.value || '').trim();
    const password = String(dom.setupPass.value || '');
    const confirm = String(dom.setupConfirm.value || '');
    if (!username) { showView('setup', '请填写用户名'); return; }
    const problem = passwordProblem(password);
    if (problem) { showView('setup', problem); return; }
    if (password !== confirm) { showView('setup', '两次输入的密码不一致'); return; }
    submitting = true;
    setBusy(dom.setupSubmit, true, '创建中…');
    try {
      const data = await window.PentestUI.apiPost('/auth/bootstrap', { username, password });
      dom.setupPass.value = '';
      dom.setupConfirm.value = '';
      dom.loginUser.value = username;
      applyAuthState(data);
      enterConsole();
    } catch (error) {
      if (error.status === 409) {
        showView('login', '已存在用户，请直接登录。');
        window.PentestUI.apiGet('/auth/state').then(applyAuthState).catch(() => { /* 状态查询失败就保持登录表单 */ });
      } else {
        showView('setup', error.message);
      }
    } finally {
      submitting = false;
      setBusy(dom.setupSubmit, false);
    }
  }

  async function submitLogin(event) {
    event.preventDefault();
    if (submitting) return;
    const username = String(dom.loginUser.value || '').trim();
    const password = String(dom.loginPass.value || '');
    if (!username) { showView('login', '请填写用户名'); return; }
    if (!password) { showView('login', '请填写密码'); return; }
    submitting = true;
    setBusy(dom.loginSubmit, true, '登录中…');
    try {
      const data = await window.PentestUI.apiPost('/auth/login', { username, password });
      dom.loginPass.value = '';
      applyAuthState(data);
      showMessage(dom.loginMessage, '');
      enterConsole();
    } catch (error) {
      if (error.status === 423) showView('login', '登录尝试过多，请稍后再试（约 5 分钟后解锁）。');
      else if (error.status === 401) showView('login', '用户名或密码错误');
      else showView('login', error.message);
    } finally {
      submitting = false;
      setBusy(dom.loginSubmit, false);
    }
  }

  async function logout() {
    try { await window.PentestUI.apiPost('/auth/logout', {}); } catch (error) { /* 退出失败也要回登录门 */ }
    leaveConsole('已退出登录。');
  }

  /* ---------------------------------------------------------- 修改密码 ---- */

  function openPasswordDialog() {
    if (!auth.active) return;
    dom.passwordCurrent.value = '';
    dom.passwordNew.value = '';
    dom.passwordConfirm.value = '';
    showMessage(dom.passwordMessage, '');
    dom.overlay.classList.remove('hidden');
    dom.passwordCurrent.focus();
  }

  function closePasswordDialog() {
    dom.overlay.classList.add('hidden');
    showMessage(dom.passwordMessage, '');
  }

  async function submitPassword(event) {
    event.preventDefault();
    const currentPassword = String(dom.passwordCurrent.value || '');
    const newPassword = String(dom.passwordNew.value || '');
    const confirm = String(dom.passwordConfirm.value || '');
    if (!currentPassword) { showMessage(dom.passwordMessage, '请填写当前密码', 'error'); return; }
    const problem = passwordProblem(newPassword);
    if (problem) { showMessage(dom.passwordMessage, problem, 'error'); return; }
    if (newPassword !== confirm) { showMessage(dom.passwordMessage, '两次输入的新密码不一致', 'error'); return; }
    setBusy(dom.passwordSubmit, true, '保存中…');
    try {
      await window.PentestUI.apiPost('/auth/password', { currentPassword, newPassword });
      dom.passwordCurrent.value = '';
      dom.passwordNew.value = '';
      dom.passwordConfirm.value = '';
      // 控制面改密后会重建会话并换发 CSRF 令牌：立刻用 /auth/me 取回新令牌，
      // 否则下一次 POST（退出登录、保存模型）会因令牌过期被 403 拒绝。
      const refreshed = await refreshSession();
      showMessage(dom.passwordMessage,
        refreshed ? '密码已更新。' : '密码已更新，但会话刷新失败，请重新登录。',
        refreshed ? 'ok' : 'error');
    } catch (error) {
      showMessage(dom.passwordMessage, error.status === 401 ? '当前密码不正确' : error.message, 'error');
    } finally {
      setBusy(dom.passwordSubmit, false);
    }
  }

  /* -------------------------------------------------------------- 绑定 ---- */

  function bind() {
    if (bound) return;
    bound = true;
    if (!dom.gate) cacheDom();
    dom.setupForm.addEventListener('submit', submitSetup);
    dom.loginForm.addEventListener('submit', submitLogin);
    dom.logoutBtn.addEventListener('click', logout);
    dom.passwordOpen.addEventListener('click', openPasswordDialog);
    dom.passwordClose.addEventListener('click', closePasswordDialog);
    dom.passwordCancel.addEventListener('click', closePasswordDialog);
    dom.passwordForm.addEventListener('submit', submitPassword);
    dom.overlay.addEventListener('click', (event) => { if (event.target === dom.overlay) closePasswordDialog(); });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !dom.overlay.classList.contains('hidden')) closePasswordDialog();
    });
  }

  window.PentestAuth = {
    start, onAuthChanged: (fn) => listeners.push(fn), csrfHeaders, handleUnauthorized,
    logout, openPasswordDialog, passwordProblem, minPasswordLength: MIN_PASSWORD_LENGTH,
    passwordPolicy: () => auth.passwordPolicy,
    isAdmin: () => Boolean(auth.user && auth.user.role === 'admin'),
    getUser: () => auth.user, getCsrfToken: () => auth.csrfToken,
  };
})();
