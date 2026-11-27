'use strict';

(() => {
  const endpoint = '/api/analysis-priority';
  const localId = /^[0-9a-f]{64}$/;
  const resourceId = /^rv_[0-9a-f]{64}$/;
  const sourceId = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
  const maxEntries = 500;
  const states = {
    queued: '待分析',
    in_progress: '分析中',
    awaiting_originals: '待补原件',
    awaiting_intake: '等待本地资料',
    source_changed: '来源版本有变',
    pending_sync: '进度待同步',
    completed: '已分析',
    completed_received_scope: '已分析实收资料',
    same_source_reuse: '同源复用',
    archive_only: '留档',
    awaiting_re_review: '待复验',
  };
  const timeFormat = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Singapore', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });

  let snapshot = null;
  let selection = null;
  let opener = null;
  let controls = null;
  let busy = false;
  let confirming = false;
  let feedback = '';
  let feedbackError = false;

  function priorityKey(target) {
    return target.kind === 'local'
      ? 'local:' + target.project_id
      : 'article:' + target.site_id + ':' + target.article_id;
  }

  function validSnapshot(data) {
    return data && Number.isSafeInteger(data.revision) && data.revision >= 0 &&
      Array.isArray(data.entries) && data.entries.length <= maxEntries &&
      data.entries.every(entry => entry && typeof entry.key === 'string') &&
      new Set(data.entries.map(entry => entry.key)).size === data.entries.length &&
      (data.submitted === null || (data.submitted && Number.isSafeInteger(data.submitted.revision) &&
        Array.isArray(data.submitted.entries)));
  }

  function ensureDialog() {
    if (controls) return controls;
    let dialog = $('analysis-order');
    if (!dialog) {
      dialog = el('dialog', null);
      dialog.id = 'analysis-order';
      document.body.append(dialog);
    }
    let body = $('analysis-body');
    if (!body || !dialog.contains(body)) {
      body = el('div', null);
      body.id = 'analysis-body';
      dialog.append(body);
    }
    dialog.classList.add('analysis-order');
    body.classList.add('analysis-content');

    let title = dialog.querySelector('h2');
    if (!title) {
      const header = el('header', null, 'analysis-head');
      title = el('h2', '分析顺序');
      header.append(title);
      dialog.insertBefore(header, dialog.firstChild);
    }
    title.id ||= 'analysis-title';
    dialog.setAttribute('aria-labelledby', title.id);
    let close = dialog.querySelector('[data-analysis-close], [data-close]');
    if (!close) {
      close = button('关闭', 'analysis-close');
      close.setAttribute('data-analysis-close', '');
      title.parentElement.append(close);
    }
    if (!close.hasAttribute('data-close')) close.addEventListener('click', () => closeDialog(dialog));
    const status = el('p', '', 'analysis-feedback');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    const area = el('div', null, 'analysis-rows');
    body.replaceChildren(status, area);
    dialog.addEventListener('close', () => {
      confirming = false;
      if (opener?.isConnected) opener.focus({preventScroll: true});
      opener = null;
    });
    controls = {dialog, status, area, close};
    return controls;
  }

  function action(label, callback, {focus, disabled = false, primary = false} = {}) {
    const node = button(label, primary ? 'primary' : '');
    node.disabled = disabled;
    if (focus) node.dataset.priorityFocus = focus;
    node.addEventListener('click', callback);
    return node;
  }

  function positionForm(label, last, callback, {focus, value = '', disabled = false, verb = '移到'} = {}) {
    const form = el('form', null, 'analysis-position');
    const input = el('input', null);
    input.type = 'number';
    input.inputMode = 'numeric';
    input.min = '1';
    input.max = String(last);
    input.step = '1';
    input.required = true;
    input.disabled = disabled;
    input.value = value;
    input.placeholder = '序号';
    input.setAttribute('aria-label', label + '，第 1 到第 ' + last + ' 位');
    if (focus) input.dataset.priorityFocus = focus;
    const submit = button(verb);
    submit.type = 'submit';
    submit.disabled = disabled;
    form.append(input, submit);
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (busy || confirming || !input.reportValidity()) return;
      const position = input.valueAsNumber;
      if (Number.isSafeInteger(position) && position >= 1 && position <= last) callback(position);
    });
    return form;
  }

  function selectionPanel(disabled) {
    const frame = el('section', null, 'analysis-selection');
    const title = el('h3', selection.title);
    const same = snapshot.entries.find(entry => entry.key === priorityKey(selection.target));
    let line = selection.target.kind === 'local' ? '本地项目' : '来源资源 · 等待本地资料';
    if (same) line += ' · 当前第 ' + same.rank + ' 位';
    frame.append(title, el('p', line, 'analysis-muted'));
    const actions = el('div', null, 'analysis-actions');
    const readyTarget = {...selection.target};
    const full = !same && snapshot.entries.length >= maxEntries;
    const add = position => void change({action: 'add', target: readyTarget, position});
    const tail = Math.min(maxEntries, snapshot.entries.length + (same ? 0 : 1));
    actions.append(
      action('放第 1 位', () => add(1), {disabled: disabled || full, focus: 'selection-first'}),
      action('放第 2 位', () => add(2), {disabled: disabled || full, focus: 'selection-second'}),
      positionForm('放到指定位置', tail, add, {disabled: disabled || full, focus: 'selection-position', verb: '放到'}),
    );
    frame.append(actions);
    return frame;
  }

  function priorityList(disabled) {
    const list = el('ol', null, 'analysis-list');
    list.setAttribute('role', 'list');
    list.setAttribute('aria-label', '已安排的分析顺序');
    const count = snapshot.entries.length;
    for (const [index, entry] of snapshot.entries.entries()) {
      const item = el('li', null, 'analysis-entry');
      const about = el('div', null, 'analysis-entry-info');
      const kind = entry.kind === 'local' ? '本地项目' : '来源资源';
      const title = entry.title || '待核项目';
      about.append(el('p', title, 'analysis-entry-title'),
        el('p', kind + ' · ' + (states[entry.state] || '进度待核实'), 'analysis-muted'));
      const actions = el('div', null, 'analysis-actions');
      const rank = index + 1;
      const number = el('span', String(rank), 'analysis-rank');
      number.setAttribute('aria-label', '第 ' + rank + ' 位');
      const move = position => void change({action: 'move', key: entry.key, position}, 'position-' + entry.key);
      const first = action('第 1 位', () => move(1), {disabled: disabled || rank === 1});
      first.setAttribute('aria-label', title + '，移到第 1 位');
      const second = action('第 2 位', () => move(2), {disabled: disabled || rank === 2 || count < 2});
      second.setAttribute('aria-label', title + '，移到第 2 位');
      const remove = action('移除', () => void change({action: 'remove', key: entry.key}, 'analysis-submit'),
        {disabled, focus: 'remove-' + entry.key});
      remove.setAttribute('aria-label', '移除 ' + title);
      actions.append(first, second,
        positionForm(title + '的目标顺位', count, move, {disabled, value: rank, focus: 'position-' + entry.key}),
        remove);
      item.append(number, about, actions);
      list.append(item);
    }
    return list;
  }

  function submissionFrame() {
    const region = el('section', null, 'analysis-submission');
    const count = snapshot.entries.length;
    const sent = snapshot.submitted;
    if (sent) {
      const value = Date.parse(sent.submitted_at);
      const date = Number.isFinite(value) ? timeFormat.format(value) + ' 已提交' : '上次提交';
      const detail = date + ' · ' + sent.entries.length + ' 项';
      region.append(el('p', detail + (sent.revision === snapshot.revision ? '' : ' · 当前草稿有未提交改动'),
        'analysis-muted'));
      if (sent.revision !== snapshot.revision && sent.entries.length) {
        const previous = el('details', null, 'analysis-previous');
        const list = el('ol', null);
        for (const entry of sent.entries) list.append(el('li', entry.title || '待核项目'));
        previous.append(el('summary', '查看上次提交顺序'), list);
        region.append(previous);
      }
    }
    if (confirming) {
      const line = count ? '提交当前 ' + count + ' 项分析顺序？' : '提交空顺序，清除上次的优先顺序？';
      region.append(el('p', line, 'analysis-confirmation'));
      const actions = el('div', null, 'analysis-actions');
      actions.append(
        action('取消', () => { confirming = false; render('analysis-submit'); },
          {disabled: busy, focus: 'confirm-cancel'}),
        action('确认提交', () => void change({action: 'submit'}, 'analysis-submit'),
          {disabled: busy, focus: 'confirm-accept', primary: true}),
      );
      region.append(actions);
      return region;
    }
    region.append(action('提交分析顺序', () => {
      confirming = true;
      render('confirm-cancel');
    }, {
      primary: true, focus: 'analysis-submit',
      disabled: busy || sent?.revision === snapshot.revision || (!count && !sent?.entries.length),
    }));
    return region;
  }

  function render(preferredFocus) {
    const {dialog, status, area, close} = ensureDialog();
    if (!dialog.open) return;
    const focused = preferredFocus || document.activeElement?.dataset.priorityFocus;
    status.textContent = feedback;
    status.hidden = !feedback;
    status.classList.toggle('analysis-error', feedbackError);
    area.setAttribute('aria-busy', String(busy));
    if (!snapshot) {
      const children = [el('p', busy ? '正在加载分析顺序…' : '暂无可显示的顺序。', 'analysis-muted')];
      if (!busy) children.push(action('重新读取', () => void refresh(), {focus: 'analysis-retry'}));
      area.replaceChildren(...children);
    } else {
      const disabled = busy || confirming;
      const children = [el('p', '资料可分析后，才可按优先顺位接续。正在分析的项目继续处理。', 'analysis-muted')];
      if (selection) children.push(selectionPanel(disabled));
      if (snapshot.entries.length) children.push(priorityList(disabled));
      else children.push(el('p', '还没有安排分析顺序。可从资源或本地项目加入。', 'analysis-empty'));
      children.push(submissionFrame());
      area.replaceChildren(...children);
    }
    if (focused) {
      const target = [...area.querySelectorAll('[data-priority-focus]')]
        .find(node => node.dataset.priorityFocus === focused && !node.disabled);
      if (target) target.focus({preventScroll: true});
      else if (!busy && preferredFocus) close.focus({preventScroll: true});
    }
  }

  function applySnapshot(value) {
    if (!validSnapshot(value)) throw new Error('unexpected analysis-priority response');
    snapshot = value;
  }

  async function refresh(afterConflict = false) {
    if (busy) return;
    busy = true;
    confirming = false;
    snapshot = null;
    feedback = '';
    feedbackError = false;
    render();
    try {
      applySnapshot(await api(endpoint));
      if (afterConflict) feedback = '顺序已变动，已重新读取。请核对后再操作。';
    } catch {
      feedback = '未能读取分析顺序，请重试。';
      feedbackError = true;
    } finally {
      busy = false;
      render(selection && !afterConflict ? 'selection-first' : undefined);
    }
  }

  async function change(update, preferredFocus) {
    if (!snapshot || busy || (confirming && update.action !== 'submit')) return;
    const request = {...update, revision: snapshot.revision};
    busy = true;
    feedback = '';
    feedbackError = false;
    render();
    try {
      applySnapshot(await api(endpoint, request));
      feedback = update.action === 'submit' ? '已提交分析顺序，等待正式队列接收。' : '分析顺序草稿已保存。';
    } catch (error) {
      if (error.status === 409) {
        busy = false;
        await refresh(true);
        return;
      }
      feedback = error.status === 422 ? '无法应用这次变更。请核对项目或来源版本后重试。'
        : '未能保存分析顺序。请重试。';
      feedbackError = true;
    } finally {
      if (busy) {
        busy = false;
        confirming = false;
        render(preferredFocus);
      }
    }
  }

  function show(target, origin) {
    opener = origin;
    selection = target;
    confirming = false;
    openDialog(ensureDialog().dialog.id);
    if (busy) render();
    else void refresh();
  }

  function projectAction(target, title) {
    const node = button('安排分析', 'analysis-priority-trigger');
    node.setAttribute('aria-haspopup', 'dialog');
    node.setAttribute('aria-label', '安排分析：' + title);
    node.addEventListener('click', event => {
      event.stopPropagation();
      show({target, title}, node);
    });
    return node;
  }

  window.analysisArticleAction = row => {
    const id = row?.resource_version_id || row?.id;
    const site = row?.site_id;
    const article = row?.article_id;
    if (typeof id !== 'string' || !resourceId.test(id) || typeof site !== 'string' ||
        !sourceId.test(site) || typeof article !== 'string' || !sourceId.test(article)) return null;
    return projectAction({kind: 'article', resource_id: id, site_id: site, article_id: article}, row.title || '来源资源');
  };

  window.analysisLocalAction = project => {
    const id = project?.project_id || project?.local_project_id;
    if (typeof id !== 'string' || !localId.test(id)) return null;
    return projectAction({kind: 'local', project_id: id}, project.title || '本地项目');
  };

  const launch = $('analysis-open');
  if (launch) {
    launch.setAttribute('aria-haspopup', 'dialog');
    launch.addEventListener('click', () => show(null, launch));
  }
})();
