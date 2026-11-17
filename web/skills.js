/**
 * Skills —— 技能手册的浏览、导入与删除（住在「设置」里的一个分组）。
 *
 * 它由上一版的「演练台」拆出来，并且**去掉了那道授权门**：技能是"读一份 markdown"，
 * 它不下发任务、不碰目标、不改变权限档；真正挡住这件事的是资产与部署两页，而那
 * 两页本来就有自己的入口。留一道 20 秒倒计时，唯一确定的效果是"打开就想关掉"。
 *
 * 与 settings.js 的分工：这里只管技能目录这一份数据（列表 / 正文 / 导入 / 删除），
 * 设置页的其它分组各管各的配置；两者共用 window.PentestUI 的请求与 DOM 工具。
 *
 * 与 dsh-purge 那个浮窗的关系：**功能同源，实现不同**。那边是 React 组件 + 宿主 RPC，
 * 这边是原生 DOM + 平台接口；数据来自平台自己的 `data/skills/` 与插件包。
 */
(function () {
  'use strict';

  const ui = () => window.PentestUI;
  const byId = (id) => document.getElementById(id);
  const el = (tag, cls, text) => ui().el(tag, cls, text);
  const clear = (node) => ui().clear(node);
  const isAdmin = () => !!(window.PentestAuth && window.PentestAuth.isAdmin
    && window.PentestAuth.isAdmin());

  const view = {
    data: null,      // GET /skills 的原样返回
    error: null,
    busy: false,
    query: '',
    bodies: {},      // name → 正文；只存展开过的，避免一次拉 20 多份
    open: {},        // name → true（这一行正文是展开的）
    pending: null,   // 进行中的那一次加载
    bound: false,
  };

  function dom() {
    return {
      status: byId('skills-status'),
      query: byId('skills-query'),
      refresh: byId('skills-refresh'),
      dir: byId('skills-dir'),
      message: byId('skills-message'),
      list: byId('skills-list'),
      importBox: byId('skills-import-box'),
      importDir: byId('skills-import-dir'),
      importButton: byId('skills-import'),
    };
  }

  function message(text, kind) {
    const node = dom().message;
    if (!node) return;
    node.textContent = text || '';
    node.classList.toggle('hidden', !text);
    node.classList.toggle('error', kind === 'error');
  }

  function fmtCount(value) {
    try { return ui().formatCount(value); } catch (error) { return String(value); }
  }

  /* ------------------------------------------------------------ 渲染 */

  function filteredRows() {
    const rows = (view.data && view.data.skills) || [];
    const terms = (view.query || '').toLowerCase().split(/\s+/).filter(Boolean);
    if (!terms.length) return rows;
    return rows.filter((row) => {
      const blob = [row.name, row.description, row.whenToUse, row.role, row.source]
        .join(' ').toLowerCase();
      return terms.every((term) => blob.indexOf(term) !== -1);
    });
  }

  function tag(text, cls) {
    return el('span', 'tag' + (cls ? ' ' + cls : ''), text);
  }

  function renderRow(row) {
    const card = el('article', 'skill-row');
    const head = el('div', 'skill-row-head');
    head.append(el('span', 'skill-name', row.name || '(未命名)'));
    if (row.source === '本地') head.append(tag('本地'));
    else head.append(tag(row.source || '插件', 'tag-plugin'));
    if (row.imported) head.append(tag('导入'));
    if (row.role) head.append(tag(row.role));
    if (row.enabled === false) head.append(tag('已停用'));
    const meta = [];
    if (row.chars) meta.push(fmtCount(row.chars) + ' 字');
    // 目录型手册（SKILL.md 加旁边的参照表/脚本）：正文之外还有几份附件。
    // 这里只报个数，完整名单挂在 title 上——一行里塞九个路径会把这一行撑成三段。
    if (row.files && row.files.length) {
      const extras = el('span', 'skill-meta', '另有 ' + fmtCount(row.files.length) + ' 个附属文件');
      extras.title = row.files.join('\n');
      meta.push(extras);
    }
    if (row.shadowed && row.shadowed.length) meta.push('覆盖同名插件手册');
    if (meta.length) head.append(el('span', 'skill-meta', meta.join(' · ')));
    card.append(head);

    if (row.description) card.append(el('p', 'skill-desc', row.description));
    if (row.whenToUse) card.append(el('p', 'skill-when', '什么时候用：' + row.whenToUse));

    const actions = el('div', 'skill-actions');
    const toggle = el('button', 'btn sm ghost', view.open[row.name] ? '收起正文' : '查看正文');
    toggle.type = 'button';
    toggle.addEventListener('click', () => toggleBody(row.name));
    actions.append(toggle);
    if (row.deletable && isAdmin()) {
      const remove = el('button', 'btn sm ghost danger', '删除');
      remove.type = 'button';
      remove.title = '只删 data/skills 下这一份；插件带来的去插件市场卸载';
      remove.addEventListener('click', () => removeSkill(row.name));
      actions.append(remove);
    }
    card.append(actions);

    if (view.open[row.name]) {
      const body = view.bodies[row.name];
      card.append(el('pre', 'skill-body',
        body === undefined ? '正在读取正文…' : (body || '（这份手册没有正文）')));
    }
    return card;
  }

  function render() {
    const d = dom();
    const data = view.data;
    if (d.importBox) d.importBox.classList.toggle('hidden', !isAdmin());
    if (!data) {
      if (d.status) d.status.textContent = view.error
        ? '加载失败：' + view.error : '尚未加载';
      if (d.list) {
        clear(d.list);
        d.list.append(ui().emptyBlock(view.error
          ? '读取技能目录失败：' + view.error + '（点「刷新」重试）'
          : '正在读取技能目录…'));
      }
      return;
    }
    const rows = data.skills || [];
    if (d.status) {
      const total = fmtCount(rows.length) + ' 份手册'
        + (data.imported ? ' · 其中导入 ' + fmtCount(data.imported) + ' 份' : '');
      d.status.textContent = view.query
        ? '匹配 ' + fmtCount(filteredRows().length) + ' / ' + total : total;
    }
    if (d.dir) d.dir.textContent = data.dir ? '本地目录：' + data.dir : '';
    const shown = filteredRows();
    if (d.list) {
      clear(d.list);
      if (!shown.length) {
        d.list.append(ui().emptyBlock(rows.length
          ? '没有匹配「' + view.query + '」的手册。'
          : '技能目录是空的：插件里没有集成技能，data/skills 下也没有手册。'));
      } else {
        shown.forEach((row) => d.list.append(renderRow(row)));
      }
    }
  }

  /* ------------------------------------------------------------ 动作 */

  async function load(force) {
    if (view.busy && !force) return view.pending;
    // 已经有读数就不再走一轮网络：切到这一组（或 prefetch 已经拉过）时直接重画。
    // 想看最新目录点「刷新」，导入/删除之后也会自己强拉一次。
    if (view.data && !force) {
      render();
      return view.data;
    }
    view.busy = true;
    const status = dom().status;
    if (status) status.textContent = '正在读取技能目录…';
    view.pending = (async () => {
      try {
        view.data = await ui().apiGet('/skills');
        view.error = null;
        view.fetchedAt = Date.now();
      } catch (error) {
        view.error = error.message;
      }
      view.busy = false;
      render();
    })();
    return view.pending;
  }

  async function toggleBody(name) {
    if (view.open[name]) {
      view.open[name] = false;
      render();
      return;
    }
    view.open[name] = true;
    render();
    if (view.bodies[name] !== undefined) return;
    try {
      const data = await ui().apiGet('/skills/' + encodeURIComponent(name));
      view.bodies[name] = (data.skill || {}).body || '';
    } catch (error) {
      view.bodies[name] = '读取失败：' + error.message;
      message('读取「' + name + '」正文失败：' + error.message, 'error');
    }
    render();
  }

  async function removeSkill(name) {
    if (!window.confirm('删除「' + name + '」这一份手册？\n'
      + '只删 data/skills 下的文件，源目录不受影响。')) return;
    try {
      await ui().apiPost('/skills/remove', { name: name });
      delete view.bodies[name];
      delete view.open[name];
      message('已删除「' + name + '」。');
      await load(true);
    } catch (error) {
      message('删除失败：' + error.message, 'error');
    }
  }

  async function importSkills() {
    const d = dom();
    const dir = d.importDir ? d.importDir.value.trim() : '';
    if (!dir) {
      message('先填一个目录（放 *.md 手册的那一个）。', 'error');
      return;
    }
    if (d.importButton) d.importButton.disabled = true;
    try {
      const outcome = await ui().apiPost('/skills/import', { sourceDir: dir });
      const imported = (outcome.imported || []).length;
      const skipped = outcome.skipped || [];
      let text = '导入 ' + imported + ' 份';
      if (skipped.length) {
        text += '，跳过 ' + skipped.length + ' 份：'
          + skipped.map((item) => item.name + '（' + item.reason + '）').join('；');
      }
      const purge = outcome.purge || {};
      if (purge.imported) text += '；破甲注入段已一并导入（' + fmtCount(purge.chars) + ' 字）';
      // 只在真的碰到 dsh-purge 时才报破甲段的事：普通手册目录里没有它，
      // 每次导入都提一句「没导入」只会把真正的结果读不清。
      else if (purge.reason && /dsh-purge|读不了|是空的/.test(purge.reason)) {
        text += '；破甲注入段未导入：' + purge.reason;
      }
      message(text);
      await load(true);
    } catch (error) {
      message('导入失败：' + error.message, 'error');
    }
    if (d.importButton) d.importButton.disabled = false;
  }

  function bind() {
    const d = dom();
    if (d.refresh) d.refresh.addEventListener('click', () => load(true));
    if (d.query) {
      d.query.addEventListener('input', () => {
        view.query = d.query.value.trim();
        render();
      });
    }
    if (d.importButton) d.importButton.addEventListener('click', importSkills);
  }

  function ensureBound() {
    if (view.bound) return;
    view.bound = true;
    bind();
  }

  window.PentestSkills = {
    load(force) {
      ensureBound();
      return load(force === true);
    },
    /** 设置页刚打开时先悄悄拉一次：切到这一组就不必再等一轮。
     *  读数超过一分钟才重拉，否则直接把已有的画出来。 */
    prefetch() {
      ensureBound();
      if (view.busy) return view.pending;
      if (view.data && Date.now() - (view.fetchedAt || 0) < 60000) return view.data;
      return load(view.data ? true : false);
    },
    state: view,
  };
})();
