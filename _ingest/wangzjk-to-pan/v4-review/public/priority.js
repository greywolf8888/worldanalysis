'use strict';

(() => {
  const endpoint = '/api/analysis-priority';
  const localId = /^[0-9a-f]{64}$/;
  const resourceId = /^rv_[0-9a-f]{64}$/;
  const sourceId = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
  const capacity = 500;
  const finishedStates = new Set(['completed', 'completed_received_scope', 'same_source_reuse']);
  const labels = {
    queued: '待分析', in_progress: '分析中', awaiting_originals: '待补原件',
    awaiting_intake: '待本地资料', source_changed: '来源版本有变', pending_sync: '进度待同步',
    waiting_for_readiness: '原件待核对',
    completed: '已分析', completed_received_scope: '已分析实收资料',
    same_source_reuse: '同源复用', archive_only: '留档', awaiting_re_review: '待复验', awaiting_acceptance: '待验收',
  };
  const timeFormat = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Singapore', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });
  const expectedErrors = new Set([
    '已有同版教材，可直接打开', '该项目需要按现有记录接续处理',
    '本地原件尚未就绪或已有变化，请核对后再安排', '请移除旧的本地项目并重新安排',
    '分析进度正在同步，请稍后重试', '资料版本已变化，请移除旧项目后重新加入',
    '本地项目尚未注册或身份有冲突', '资料归属待核实，未加入分析顺序',
    '未能匹配当前来源项目，请重开该条资源', '该项目当前不在分析顺位表中', '分析顺位表已满',
  ]);

  let snapshot = null;
  let entries = new Map();
  let loaded = false;
  let selection = null;
  let activeKey = null;
  let opener = null;
  let view = null;
  let busy = false;
  let confirming = false;
  let notice = '';
  let failure = false;
  const rows = new Map();

  function el(tag, text = '', className = '') {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function button(label, callback, className = '') {
    const node = el('button', label, className);
    node.type = 'button';
    if (callback) node.addEventListener('click', callback);
    return node;
  }

  function entryTitle(entry) {
    return typeof entry?.title === 'string' && entry.title.trim() ? entry.title.trim() : '待核项目';
  }

  function keyOf(target) {
    return target.kind === 'local'
      ? `local:${target.project_id}` : `article:${target.site_id}:${target.article_id}`;
  }

  async function request(update) {
    const options = {cache: 'no-store', credentials: 'omit'};
    if (update) Object.assign(options, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(update),
    });
    const response = await fetch(endpoint, options);
    const data = await response.json();
    if (response.ok) return data;
    const error = new Error(expectedErrors.has(data?.error) ? data.error : '');
    error.status = response.status;
    throw error;
  }

  function validEntries(values) {
    return Array.isArray(values) && values.length <= capacity &&
      values.every(entry => entry && typeof entry.key === 'string' && entry.key) &&
      new Set(values.map(entry => entry.key)).size === values.length;
  }

  function applySnapshot(value) {
    const revisionOk = value && Number.isSafeInteger(value.revision) && value.revision >= 0;
    const submissionOk = value?.submitted === null || (value?.submitted &&
      Number.isSafeInteger(value.submitted.revision) && value.submitted.revision <= value.revision &&
      validEntries(value.submitted.entries));
    if (!revisionOk || !validEntries(value.entries) || !submissionOk) throw new Error();
    snapshot = value;
    entries = new Map(value.entries.map((entry, index) => [entry.key, {...entry, rank: index + 1}]));
    loaded = true;
    if (!entries.has(activeKey)) activeKey = value.entries[0]?.key ?? null;
  }

  function positionInput(onPosition, verb = '移到') {
    const form = el('form', '', 'analysis-position');
    const input = el('input');
    input.type = 'number';
    input.inputMode = 'numeric';
    input.min = '1';
    input.step = '1';
    input.required = true;
    input.placeholder = '顺位';
    input.setAttribute('aria-label', `${verb}第几位`);
    const submit = button(verb);
    submit.type = 'submit';
    form.append(input, submit);
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (canEdit() && input.reportValidity()) onPosition(input.valueAsNumber);
    });
    return {form, input, submit};
  }

  function ensureView() {
    if (view) return view;
    let dialog = document.getElementById('analysis-order');
    let body = document.getElementById('analysis-body');
    if (!dialog || !body || !dialog.contains(body)) {
      dialog = el('dialog');
      dialog.id = 'analysis-order';
      const frame = el('div');
      const header = el('header', '', 'analysis-head');
      const title = el('h2', '分析顺序');
      title.id = 'analysis-title';
      const close = button('关闭', () => dialog.close(), 'analysis-close');
      close.setAttribute('data-analysis-close', '');
      close.autofocus = true;
      header.append(title, close);
      body = el('div');
      body.id = 'analysis-body';
      frame.append(header, body);
      dialog.append(frame);
      document.body.append(dialog);
    }
    dialog.classList.add('analysis-order');
    dialog.setAttribute('aria-labelledby', 'analysis-title');
    body.classList.add('analysis-content');
    body.parentElement.classList.add('analysis-frame');

    const status = el('p', '', 'analysis-feedback');
    status.setAttribute('role', 'status');
    const retry = button('重新读取', () => void refresh());
    const loading = el('p', '正在读取分析顺序…', 'analysis-muted');
    const editable = el('fieldset', '', 'analysis-editable');
    editable.setAttribute('aria-label', '分析顺序草稿');
    const selected = el('section', '', 'analysis-selection');
    const board = el('section', '', 'analysis-board');
    const heading = el('h3', '分析草稿', 'analysis-board-heading');
    const count = el('span', '', 'analysis-muted');
    heading.append(count);

    const toolbar = el('div', '', 'analysis-toolbar');
    toolbar.setAttribute('role', 'group');
    toolbar.setAttribute('aria-label', '所选项目的排序操作');
    const current = el('p', '', 'analysis-current');
    const actions = el('div', '', 'analysis-actions');
    const top = button('置顶', () => moveActive(1));
    const up = button('上移', () => moveActive(entries.get(activeKey)?.rank - 1));
    const down = button('下移', () => moveActive(entries.get(activeKey)?.rank + 1));
    const last = button('置底', () => moveActive(entries.size));
    const position = positionInput(moveActive);
    actions.append(top, up, down, last, position.form);
    toolbar.append(current, actions);

    const list = el('ol', '', 'analysis-list');
    list.setAttribute('role', 'list');
    list.setAttribute('aria-label', '分析草稿中的项目');
    const empty = el('p', '草稿为空。可从课程表、资源库或本地项目加入。', 'analysis-empty');
    board.append(heading, toolbar, list, empty);
    editable.append(selected, board);
    body.replaceChildren(status, retry, loading, editable);

    const footer = el('footer', '', 'analysis-submission');
    const summary = el('p', '', 'analysis-muted');
    const previous = el('details', '', 'analysis-previous');
    const previousList = el('ol');
    previous.append(el('summary', '查看上次提交'), previousList);
    const submit = button('提交分析顺序', startConfirmation, 'primary');
    const confirm = el('div', '', 'analysis-confirm');
    const instruction = el('p', '', 'analysis-confirmation');
    const conditions = el('p', '正在分析的项目继续处理。新顺序从下一个项目生效。', 'analysis-muted');
    const decisions = el('div', '', 'analysis-actions');
    const cancel = button('取消', cancelConfirmation);
    const accept = button('确认提交', () => void change({action: 'submit'}), 'primary');
    decisions.append(cancel, accept);
    confirm.append(instruction, conditions, decisions);
    footer.append(summary, previous, submit, confirm);
    body.parentElement.append(footer);

    dialog.addEventListener('close', () => {
      confirming = false;
      selection = null;
      if (opener?.isConnected) opener.focus({preventScroll: true});
      opener = null;
    });
    dialog.addEventListener('cancel', event => {
      if (!confirming || busy) return;
      event.preventDefault();
      cancelConfirmation();
    });

    view = {dialog, body, status, retry, loading, editable, selected, count, toolbar,
      current, top, up, down, last, position, list, empty, footer, summary,
      previous, previousList, submit, confirm, instruction, cancel, accept,
      lastSelection: null, lastSnapshot: null, previousRevision: null, highlighted: null};
    return view;
  }

  function canEdit() {
    return loaded && !busy && !confirming;
  }

  function renderSelection() {
    const target = selection?.target;
    const key = target ? keyOf(target) : '';
    const member = entries.get(key);
    const stamp = target ? [key, target.resource_id, selection.title, member?.rank, member?.state, member?.current_resource_id, entries.size].join('\t') : '';
    view.selected.hidden = !selection;
    if (stamp === view.lastSelection) return;
    view.lastSelection = stamp;
    if (!target) {
      view.selected.replaceChildren();
      return;
    }
    const conflict = member && target.kind === 'article' &&
      ((member.current_resource_id || member.resource_id) !== target.resource_id || member.state === 'source_changed');
    const info = conflict ? '原条目的来源已变化，请先移除原条目。'
      : member ? `草稿第 ${member.rank} 位` : target.kind === 'article' ? '待本地资料' : '';
    const actions = el('div', '', 'analysis-actions');
    if (conflict) {
      actions.append(button('移除原条目', () => removeDraft(key)));
    } else {
      const full = !member && entries.size >= capacity;
      const size = entries.size + (member ? 0 : 1);
      const order = position => {
        if (member) moveDraft(key, position);
        else void change({action: 'add', target, ...(position === undefined ? {} : {position})}, key);
      };
      const first = button('放第 1 位', () => order(1));
      const second = button('放第 2 位', () => order(2));
      const tail = button(member ? '置底' : '加到末尾', () => order(member ? size : undefined), member ? '' : 'primary');
      first.disabled = full || member?.rank === 1;
      second.disabled = full || size < 2 || member?.rank === 2;
      tail.disabled = full || member?.rank === size;
      actions.append(first, second, tail);
      if (!member && !full && size > 2) {
        const position = positionInput(order, '放到');
        position.input.max = String(size);
        actions.append(position.form);
      }
    }
    view.selected.replaceChildren(el('h3', selection.title));
    if (info) view.selected.append(el('p', info, 'analysis-muted'));
    view.selected.append(actions);
    if (!member && entries.size >= capacity) view.selected.append(el('p', '草稿已满。移除项目后可继续加入。', 'analysis-muted'));
  }

  function makeRow(key) {
    const row = el('li', '', 'analysis-entry');
    const rank = el('span', '', 'analysis-rank');
    rank.setAttribute('aria-hidden', 'true');
    const about = el('div', '', 'analysis-entry-info');
    const title = button('', () => selectDraft(key), 'analysis-entry-title');
    title.setAttribute('aria-pressed', 'false');
    const state = el('p', '', 'analysis-muted');
    const remove = button('移除', () => removeDraft(key), 'analysis-remove');
    about.append(title, state);
    row.append(rank, about, remove);
    return {row, rank, title, state, remove};
  }

  function renderRows() {
    if (view.lastSnapshot === snapshot) return;
    view.lastSnapshot = snapshot;
    for (const [key, parts] of rows) {
      if (entries.has(key)) continue;
      parts.row.remove();
      rows.delete(key);
    }
    let cursor = view.list.firstElementChild;
    for (const [key, entry] of entries) {
      let parts = rows.get(key);
      if (!parts) {
        parts = makeRow(key);
        rows.set(key, parts);
      }
      const title = entryTitle(entry);
      const state = Object.hasOwn(labels, entry.state) ? labels[entry.state] : '';
      const rank = String(entry.rank);
      if (parts.rank.textContent !== rank) {
        parts.rank.textContent = rank;
        parts.title.setAttribute('aria-label', `第 ${rank} 位，${title}`);
      }
      if (parts.title.textContent !== title) {
        parts.title.textContent = title;
        parts.title.setAttribute('aria-label', `第 ${rank} 位，${title}`);
        parts.remove.setAttribute('aria-label', `从草稿移除 ${title}`);
      }
      if (parts.state.textContent !== state) parts.state.textContent = state;
      parts.state.hidden = !state;
      if (parts.row === cursor) cursor = cursor.nextElementSibling;
      else view.list.insertBefore(parts.row, cursor);
    }
    view.count.textContent = ` ${entries.size} 项`;
    view.empty.hidden = entries.size > 0;
    view.list.hidden = !entries.size;
  }

  function renderToolbar() {
    const entry = entries.get(activeKey);
    view.toolbar.hidden = !entry;
    if (!entry) return;
    const title = entryTitle(entry);
    view.current.textContent = `第 ${entry.rank} 位 · ${title}`;
    view.top.disabled = view.up.disabled = entry.rank <= 1;
    view.last.disabled = view.down.disabled = entry.rank >= entries.size;
    const {input, submit} = view.position;
    input.max = String(entries.size);
    input.disabled = submit.disabled = entries.size < 2;
    if (document.activeElement !== input) input.value = '';
  }

  function renderRowFocus() {
    if (view.highlighted && view.highlighted !== activeKey && rows.has(view.highlighted)) {
      const parts = rows.get(view.highlighted);
      parts.row.classList.remove('analysis-entry-active');
      parts.title.setAttribute('aria-pressed', 'false');
    }
    const selected = rows.get(activeKey);
    if (selected) {
      selected.row.classList.add('analysis-entry-active');
      selected.title.setAttribute('aria-pressed', 'true');
    }
    view.highlighted = selected ? activeKey : null;
  }

  function renderSubmission() {
    const sent = snapshot?.submitted;
    const count = entries.size;
    const dirty = sent && sent.revision !== snapshot.revision;
    const stamp = Date.parse(sent?.submitted_at);
    const date = Number.isFinite(stamp) ? ` · ${timeFormat.format(stamp)}` : '';
    view.footer.hidden = !snapshot;
    view.summary.textContent = sent
      ? `已提交 ${sent.entries.length} 项${date}${dirty ? ' · 草稿有未提交改动' : ''}` : '尚未提交分析顺序';
    view.previous.hidden = !dirty || !sent.entries.length;
    if (!view.previous.hidden && view.previousRevision !== sent.revision) {
      view.previousRevision = sent.revision;
      view.previousList.replaceChildren(...sent.entries.map(entry => el('li', entryTitle(entry))));
    }
    view.submit.hidden = confirming;
    view.submit.disabled = !canEdit() || snapshot?.draft_matches_submission === true ||
      sent?.revision === snapshot?.revision || (!count && !sent?.entries.length);
    view.confirm.hidden = !confirming;
    view.instruction.textContent = count ? `提交当前 ${count} 项分析顺序？` : '提交空顺序，撤回上次的优先安排？';
    view.cancel.disabled = view.accept.disabled = busy || !loaded;
  }

  function render() {
    if (!view?.dialog.open) return;
    view.status.textContent = notice;
    view.status.hidden = !notice;
    view.status.classList.toggle('analysis-error', failure);
    view.retry.hidden = loaded || busy;
    view.loading.hidden = !!snapshot || !busy;
    view.editable.hidden = !snapshot;
    view.editable.disabled = !canEdit();
    view.editable.setAttribute('aria-busy', String(busy));
    if (snapshot) {
      renderSelection();
      renderRows();
      renderToolbar();
      renderRowFocus();
    }
    renderSubmission();
  }

  function selectDraft(key) {
    if (!canEdit() || !entries.has(key) || key === activeKey) return;
    activeKey = key;
    renderRowFocus();
    renderToolbar();
  }

  function startConfirmation() {
    if (!canEdit() || view.submit.disabled) return;
    confirming = true;
    render();
    view.cancel.focus();
  }

  function cancelConfirmation() {
    if (busy) return;
    confirming = false;
    render();
    view.submit.focus();
  }

  function moveDraft(key, position) {
    const item = entries.get(key);
    if (!item || !Number.isSafeInteger(position) || position < 1 || position > entries.size || position === item.rank) return;
    void change({action: 'move', key, position}, key);
  }

  function moveActive(position) {
    moveDraft(activeKey, position);
  }

  function removeDraft(key) {
    if (!entries.has(key)) return;
    void change({action: 'remove', key});
  }

  async function refresh(afterConflict = '') {
    if (busy) return;
    busy = true;
    confirming = false;
    loaded = false;
    notice = '';
    failure = false;
    render();
    try {
      applySnapshot(await request());
      if (selection && entries.has(keyOf(selection.target))) activeKey = keyOf(selection.target);
      notice = afterConflict;
      failure = !!afterConflict;
    } catch {
      notice = '未能读取分析顺序。请重新读取。';
      failure = true;
    } finally {
      busy = false;
      render();
    }
  }

  async function change(update, requestedKey = null) {
    const submitting = update.action === 'submit';
    if (!snapshot || !loaded || busy || confirming !== submitting) return;
    const revision = snapshot.revision;
    const rank = entries.get(update.key)?.rank;
    const prior = activeKey;
    const focused = document.activeElement;
    busy = true;
    notice = '';
    failure = false;
    render();
    try {
      applySnapshot(await request({...update, revision}));
      if (requestedKey && entries.has(requestedKey)) activeKey = requestedKey;
      if (update.action === 'remove' && prior === update.key) {
        activeKey = snapshot.entries[rank - 1]?.key ?? snapshot.entries.at(-1)?.key ?? null;
      }
      notice = submitting ? '分析顺序已提交。' : '草稿已保存。';
    } catch (error) {
      busy = false;
      confirming = false;
      if (error.status === 409) {
        await refresh(error.message || '顺序已变动。已重新读取，请核对后再操作。');
        return;
      }
      if (!error.status) loaded = false;
      notice = error.message || (loaded ? '未能保存分析顺序，请重试。' : '未能确认操作结果。请重新读取顺序。');
      failure = true;
    } finally {
      busy = false;
      confirming = false;
      render();
      if (view?.dialog.open && loaded && focused?.isConnected && !focused.disabled && !focused.closest('[hidden]')) {
        focused.focus({preventScroll: true});
      } else if (view?.dialog.open && loaded && !submitting) {
        rows.get(activeKey)?.title.focus({preventScroll: true});
      }
    }
  }

  function show(project = null, origin = document.activeElement) {
    const {dialog, body} = ensureView();
    if (!dialog.open) opener = origin;
    selection = project;
    confirming = false;
    if (!dialog.open) dialog.showModal();
    body.scrollTop = 0;
    if (busy) render();
    else void refresh();
  }

  function projectAction(target, title, {compact = false} = {}) {
    const trigger = button('安排分析', event => {
      event.stopPropagation();
      show({target, title}, trigger);
    }, `analysis-priority-trigger${compact ? ' analysis-priority-compact' : ''}`);
    trigger.setAttribute('aria-haspopup', 'dialog');
    trigger.setAttribute('aria-label', `安排分析 ${title}`);
    return trigger;
  }

  function articleAction(row, options) {
    const id = row?.resource_version_id || row?.id;
    const site = row?.site_id;
    const article = row?.article_id;
    if (row?.action_pending || finishedStates.has(row?.analysis_state) ||
        typeof id !== 'string' || !resourceId.test(id) || typeof site !== 'string' ||
        !sourceId.test(site) || typeof article !== 'string' || !sourceId.test(article)) return null;
    return projectAction({kind: 'article', resource_id: id, site_id: site, article_id: article}, entryTitle(row), options);
  }

  function localAction(project, options) {
    const id = project?.project_id || project?.local_project_id;
    const bookMatches = ['same_registered_bytes', 'same_verified_independent_derivative'].includes(project?.textbook_integrity);
    if ((bookMatches && finishedStates.has(project?.analysis_state)) ||
        typeof id !== 'string' || !localId.test(id)) return null;
    return projectAction({kind: 'local', project_id: id}, entryTitle(project), options);
  }

  const open = origin => show(null, origin);
  window.worldanalysisAnalysisPriority = Object.freeze({articleAction, localAction, open});
  window.analysisArticleAction = articleAction;
  window.analysisLocalAction = localAction;
  const launch = document.getElementById('analysis-open');
  if (launch) {
    launch.setAttribute('aria-haspopup', 'dialog');
    launch.addEventListener('click', () => open(launch));
  }
})();
