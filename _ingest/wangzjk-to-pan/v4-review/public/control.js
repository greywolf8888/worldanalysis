'use strict';

const byId = id => document.getElementById(id);
const number = new Intl.NumberFormat('zh-CN');
const date = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Singapore', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
const controls = Object.freeze({
  ready: ['可执行', 'neutral'], running: ['运行中', 'active'], queued: ['排队中', 'active'],
  waiting: ['待处理', 'attention'], pending: ['待处理', 'attention'],
  complete: ['已完成', 'good'], succeeded: ['已完成', 'good'],
  failed: ['失败', 'error'], unknown: ['待核实', 'neutral'],
  blocked: ['需处理', 'error'], paused: ['已暂停', 'attention'],
  skipped: ['已跳过', 'neutral'], interrupted: ['已中断', 'error'],
});
const actionsAllowed = new Set([
  '/', '/courses', '/member-import', '/?panel=transfer', '/?panel=analysis',
  '/?panel=progress', '/?panel=cleanup', '/?panel=confirmation',
]);
const recentActivity = new Set(['running', 'queued', 'pending']);
const view = {data: null, rendered: '', read: null, action: null, fresh: false, timer: null, budget: 0};
const verificationView = {holds: [], rendered: '', request: null, timer: null, suspended: false};

function element(tag, css, text) {
  const node = document.createElement(tag);
  if (css) node.className = css;
  if (text) node.textContent = text;
  return node;
}

function phrase(value, max = 320) {
  return typeof value === 'string' ? value.trim().slice(0, max) : '';
}

function message(id, text) {
  const node = byId(id);
  node.textContent = text;
  node.hidden = !text;
}

function showVerification(holds) {
  if (holds !== null) verificationView.holds = holds;
  const visible = verificationView.holds;
  const key = visible.length ? JSON.stringify([visible, holds !== null]) : '';
  if (verificationView.rendered === key) return;
  verificationView.rendered = key;
  const region = byId('control-verification');
  if (!visible.length) {
    region.replaceChildren();
    return;
  }
  const notice = element('p', 'control-notice');
  const status = holds === null ? '最新状态暂未核实。' : '';
  if (visible.every(hold => hold.general === true)) {
    notice.append(element('strong', '', '转存已暂停。'),
      ` ${status}请在专用 DSH 的「Worldanalysis 网盘交接」核对原因。`);
  } else {
    const detail = visible.map(hold => hold.label + hold.message).join(' ');
    notice.append(element('strong', '', '需要验证。'),
      ` ${detail}${status} 请在专用 DSH 的官方页面完成验证，然后在「Worldanalysis 网盘交接」发送「验证完成，继续」。`);
  }
  region.replaceChildren(notice);
}

function stopVerificationPoll() {
  window.clearTimeout(verificationView.timer);
  verificationView.timer = null;
  const request = verificationView.request;
  verificationView.request = null;
  request?.abort();
}

async function readVerification() {
  if (document.hidden || verificationView.suspended || verificationView.request) return;
  window.clearTimeout(verificationView.timer);
  const request = new AbortController();
  verificationView.request = request;
  try {
    const options = {
      headers: {'Accept': 'application/json'}, cache: 'no-store',
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(5000)]),
    };
    const response = await fetch('/api/verification-status', options);
    if (response.status === 404) {
      const priorResponse = await fetch('/api/preconfirmation', options);
      if (!priorResponse.ok) throw new Error();
      const prior = await priorResponse.json();
      if (typeof prior?.platform_hold_present !== 'boolean') throw new Error();
      const holds = prior.platform_hold_present ? [{id: 'platform-hold', general: true}] : [];
      if (verificationView.request === request) showVerification(holds);
      return;
    }
    if (!response.ok) throw new Error();
    const data = await response.json();
    if (data?.available !== true || !Array.isArray(data.holds)) throw new Error();
    const holds = data.holds.map(hold => ({id: phrase(hold?.id, 100),
      label: phrase(hold?.label, 50), message: phrase(hold?.message, 160)}));
    if (holds.some(hold => !hold.id || !hold.label || !hold.message)) throw new Error();
    if (verificationView.request === request) showVerification(holds);
  } catch {
    if (verificationView.request === request) showVerification(null);
  } finally {
    if (verificationView.request === request) {
      verificationView.request = null;
      if (!document.hidden && !verificationView.suspended) {
        verificationView.timer = window.setTimeout(readVerification, 15000);
      }
    }
  }
}

function recordTime(value) {
  const raw = phrase(value, 50);
  if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(raw)) return null;
  const instant = new Date(raw);
  if (!Number.isFinite(instant.getTime())) return null;
  const time = element('time', '', date.format(instant));
  time.dateTime = instant.toISOString();
  return time;
}

function stateFor(value) {
  return Object.hasOwn(controls, value) ? controls[value] : controls.unknown;
}

function pill(state) {
  const [label, tone] = stateFor(state);
  const node = element('span', 'control-state', label);
  node.dataset.tone = tone;
  return node;
}

function actionGroup(values, section) {
  const group = element('div', 'control-actions');
  if (!Array.isArray(values)) return group;
  for (const action of values) {
    const label = phrase(action?.label, 50);
    if (!label || action.enabled === false) continue;
    if (action.href !== undefined) {
      if (!actionsAllowed.has(action.href)) continue;
      const link = element('a', '', label);
      link.href = action.href;
      link.dataset.controlKey = `${section}/${action.href}`;
      group.append(link);
      continue;
    }
    const id = action.id;
    if (typeof id !== 'string' || !/^[a-z][a-z0-9_-]{0,39}$/.test(id)) continue;
    const button = element('button', section === 'page' && id === 'collect' ? 'primary' : '', label);
    button.type = 'button';
    button.dataset.controlAction = id;
    button.dataset.controlKey = `${section}/${id}`;
    button.disabled = !!view.action || !view.fresh;
    button.addEventListener('click', () => void perform(id, label));
    group.append(button);
  }
  return group;
}

function renderMetrics(items) {
  const metrics = element('dl', 'control-metrics');
  if (!Array.isArray(items)) return metrics;
  for (const metric of items) {
    const label = phrase(metric?.label, 65);
    const value = typeof metric?.value === 'number' && Number.isFinite(metric.value)
      ? number.format(metric.value) : phrase(metric?.value, 95);
    if (!label || !value) continue;
    const pair = element('div');
    pair.append(element('dt', '', label), element('dd', '', value));
    metrics.append(pair);
  }
  return metrics;
}

function stageRow(stage) {
  const label = phrase(stage?.label, 90);
  if (!label) return null;
  const row = element('li', 'control-stage');
  row.dataset.tone = stateFor(stage.state)[1];
  row.append(element('h3', '', label), pill(stage.state));
  const body = element('div', 'control-stage-body');
  const summary = phrase(stage.summary);
  if (summary) body.append(element('p', 'control-summary', summary));
  const metrics = renderMetrics(stage.metrics);
  if (metrics.childElementCount) body.append(metrics);
  const actions = actionGroup(stage.actions, `stage/${stage.id || label}`);
  const time = recordTime(stage.observed_at);
  if (time || actions.childElementCount) {
    const footer = element('div', 'control-stage-footer');
    if (actions.childElementCount) footer.append(actions);
    if (time) footer.append(time);
    body.append(footer);
  }
  if (body.childElementCount) row.append(body);
  return row;
}

function runRow(run) {
  const label = phrase(run?.label, 90);
  if (!label) return null;
  const row = element('li', 'control-run');
  const heading = element('div', 'control-run-heading');
  heading.append(element('h3', '', label), pill(run.state));
  row.append(heading);
  const summary = phrase(run.summary);
  if (summary) row.append(element('p', '', summary));
  const times = element('dl', 'control-run-dates');
  for (const [name, value] of [['开始', run.started_at], ['结束', run.finished_at]]) {
    const time = recordTime(value);
    if (!time) continue;
    const entry = element('dd');
    entry.append(time);
    times.append(element('dt', '', name), entry);
  }
  if (times.childElementCount) row.append(times);
  return row;
}

function renderList(id, note, rows, render, empty) {
  const target = byId(id);
  const items = rows.map(render).filter(Boolean);
  target.replaceChildren(...items);
  target.hidden = !items.length;
  message(note, items.length ? '' : empty);
}

function render(data) {
  const time = recordTime(data.observed_at);
  const readback = byId('control-readback');
  readback.replaceChildren();
  if (time) readback.append('状态读取于 ', time);
  readback.hidden = !time;

  const key = JSON.stringify([data.stages, data.runs, data.actions]);
  if (key === view.rendered) return;
  const focused = document.activeElement?.dataset.controlKey;
  view.rendered = key;
  byId('control-actions').replaceChildren(...actionGroup(data.actions, 'page').children);
  renderList('control-stages', 'control-stage-note', data.stages, stageRow, '暂无可显示的环节。');
  renderList('control-runs', 'control-run-note', data.runs, runRow, '暂无运行记录。');
  if (focused) {
    const next = Array.from(document.querySelectorAll('[data-control-key]'))
      .find(node => node.dataset.controlKey === focused && !node.disabled);
    next?.focus({preventScroll: true});
  }
}

function controlsState() {
  byId('control-refresh').disabled = !!view.read || !!view.action;
  byId('control-content').setAttribute('aria-busy', String(!!view.read));
  for (const button of document.querySelectorAll('button[data-control-action]')) {
    button.disabled = !!view.action || !view.fresh;
  }
}

function clearPoll() {
  if (view.timer === null) return;
  window.clearTimeout(view.timer);
  view.timer = null;
}

function hasActiveRun() {
  return view.data?.runs.some(run => recentActivity.has(run?.state));
}

function nextPoll() {
  clearPoll();
  if (document.hidden || view.action || !view.fresh || !view.budget || !hasActiveRun()) return;
  view.timer = window.setTimeout(() => {
    view.timer = null;
    view.budget -= 1;
    void refresh(false);
  }, 4500);
}

async function readStatus() {
  try {
    const response = await fetch('/api/control', {headers: {'Accept': 'application/json'}, cache: 'no-store'});
    if (!response.ok) throw new Error();
    const data = await response.json();
    if (!Array.isArray(data?.stages) || !Array.isArray(data?.runs)) throw new Error();
    view.data = data;
    view.fresh = true;
    render(data);
    message('control-error', '');
    return true;
  } catch {
    view.fresh = false;
    if (!view.data) {
      message('control-stage-note', '');
      message('control-run-note', '');
    }
    message('control-error', view.data
      ? '未能刷新。下方为上次读取的状态，请重试。' : '未能读取控制台，请刷新状态。');
    return false;
  }
}

function refresh(follow = true) {
  if (follow) view.budget = 28;
  if (view.read) return view.read;
  clearPoll();
  view.read = readStatus().finally(() => {
    view.read = null;
    controlsState();
    nextPoll();
  });
  controlsState();
  return view.read;
}

async function perform(action, label) {
  if (view.action || !view.fresh) return;
  clearPoll();
  view.action = action;
  controlsState();
  message('control-error', '');
  message('control-feedback', `正在提交${label}…`);
  try {
    if (view.read && !await view.read) {
      message('control-feedback', '');
      return;
    }
    const response = await fetch('/api/control', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
      body: JSON.stringify({action}),
    });
    const result = await response.json();
    if (!response.ok) {
      const reason = phrase(result?.error, 200);
      if (response.status === 409) await refresh();
      message('control-feedback', '');
      message('control-error', reason || '未能提交，请刷新状态后重试。');
      return;
    }
    message('control-feedback', phrase(result?.message, 200) || '已提交，正在读取运行状态。');
    await refresh();
  } catch {
    await refresh();
    message('control-feedback', '');
    message('control-error', '操作结果未返回，请先核对近期运行。');
  } finally {
    view.action = null;
    controlsState();
    nextPoll();
  }
}

byId('control-refresh').addEventListener('click', () => {
  message('control-feedback', '');
  void readVerification();
  void refresh();
});

const pathPanel = byId('control-paths');
const pathForm = byId('control-path-form');
const pathVersion = byId('control-path-version');
const pathSave = byId('control-path-save');
const pathActivate = byId('control-path-activate');
const pathRollback = byId('control-path-rollback');
let pathData = null;
let pathPreview = null;
let pathBusy = false;
let pathRequest = 0;

function pathRows(list, values) {
  list.replaceChildren(...values.flatMap(item => {
    const label = phrase(item.label);
    const path = phrase(item.path, 520);
    return label && path ? [element('dt', '', label), element('dd', '', path)] : [];
  }));
  return list.childElementCount > 0;
}

function showPathCandidate() {
  const config = pathData?.path_config;
  const candidate = config?.versions.find(item => item.version_id === pathVersion.value);
  const blockers = Object.values(candidate?.blocked_roles || {});
  const isCurrent = candidate?.version_id === config?.active_version_id;
  const isStale = candidate && !isCurrent && candidate.base_version_id !== config.active_version_id;
  pathActivate.disabled = pathBusy || !candidate || isCurrent || isStale || blockers.length > 0;
  message('control-path-blocker', blockers.join(' ') || (isStale ? '请根据目前使用的位置重新核对并保存。' : ''));
  const differing = (pathData?.proposal_roles || []).flatMap(({key, label}) => {
    const value = candidate?.paths[key];
    return value && value !== config.paths[key] ? [{label, path:value}] : [];
  });
  const list = byId('control-path-candidate');
  list.hidden = !pathRows(list, differing);
}

function showPaths(data, selectedVersion) {
  pathData = data;
  pathPreview = null;
  pathSave.hidden = true;
  const config = data.path_config;
  const roles = data.proposal_roles || [];
  const values = config ? roles.map(({key, label}) => ({
    label:phrase(label).replace(/^未来/, ''), path:config.paths[key],
  })) : [...data.paths];
  if (data.cloud_destination?.path) values.push({label:'已设网盘目录', path:data.cloud_destination.path});
  const visible = pathRows(byId('control-path-list'), values);
  const versioned = data.activation === 'versioned' && config;
  byId('control-path-fields').replaceChildren(...roles.map(({key, label}) => {
    const field = element('label', '', phrase(label));
    const input = element('input');
    input.name = key;
    input.maxLength = 520;
    input.placeholder = phrase(config?.paths[key], 520);
    field.append(input);
    return field;
  }));
  pathForm.hidden = !versioned || !roles.length || !data.revision;
  const versions = config?.versions || [];
  pathVersion.replaceChildren(...versions.map(version => {
    const time = recordTime(version.created_at)?.textContent || '保存的位置';
    const active = version.version_id === config.active_version_id;
    const option = element('option', '', active ? time + ' · 使用中' : time);
    option.value = version.version_id;
    return option;
  }));
  if (selectedVersion) pathVersion.value = selectedVersion;
  byId('control-path-versions').hidden = !versions.length;
  pathRollback.hidden = !config?.previous_version_id;
  const catalogPending = config?.paths.catalog_data && data.catalog_in_use
    && config.paths.catalog_data !== data.catalog_in_use;
  message('control-path-status', catalogPending ? '采集位置已切换。请重新启动资源库以读取新目录。'
    : visible ? '' : '暂时无法核对当前保存位置。');
  showPathCandidate();
}

pathPanel.addEventListener('toggle', async () => {
  const sequence = ++pathRequest;
  if (!pathPanel.open) return;
  pathData = null;
  pathForm.hidden = true;
  byId('control-path-versions').hidden = true;
  byId('control-path-candidate').hidden = true;
  message('control-path-blocker', '');
  message('control-path-status', '正在读取保存位置…');
  message('control-path-feedback', '');
  try {
    const response = await fetch('/api/workspace-paths', {cache: 'no-store'});
    if (!response.ok) throw new Error();
    const data = await response.json();
    if (!pathPanel.open || sequence !== pathRequest) return;
    showPaths(data);
  } catch {
    if (sequence === pathRequest) message('control-path-status', '未能读取保存位置，请重新打开。');
  }
});

async function pathOperation(action, fields, onSuccess, messageOnSuccess) {
  if (!pathData?.revision || pathBusy) return;
  const sequence = pathRequest;
  pathBusy = true;
  for (const input of pathPanel.querySelectorAll('input, select, button')) input.disabled = true;
  message('control-path-feedback', '正在处理保存位置…');
  try {
    const response = await fetch('/api/workspace-paths', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, ...fields, revision: pathData.revision}),
    });
    const data = await response.json();
    if (sequence !== pathRequest) return;
    if (response.ok) onSuccess(data);
    message('control-path-feedback', response.ok ? (phrase(data.message, 200) || messageOnSuccess)
      : (phrase(data.error, 200) || '路径未通过核对，请检查后重试。'));
  } catch {
    if (sequence === pathRequest) message('control-path-feedback', '结果未返回，请重新打开保存位置核对。');
  } finally {
    pathBusy = false;
    for (const input of pathPanel.querySelectorAll('input, select, button')) input.disabled = false;
    showPathCandidate();
  }
}

pathForm.addEventListener('input', () => {
  pathPreview = null;
  pathSave.hidden = true;
});
pathForm.addEventListener('submit', event => {
  event.preventDefault();
  const proposal = Object.fromEntries([...new FormData(pathForm)].map(([key, value]) => [key, value.trim()])
    .filter(([, value]) => value));
  if (!Object.keys(proposal).length) {
    message('control-path-feedback', '请填写需要调整的路径。');
    return;
  }
  void pathOperation('preview', {proposal}, data => {
    pathPreview = {proposal, proposal_id:data.proposal_id};
    pathSave.hidden = !data.proposal_id;
  }, '路径已核对，可以保存。');
});
pathVersion.addEventListener('change', showPathCandidate);
pathSave.addEventListener('click', () => {
  if (pathPreview) void pathOperation('save', pathPreview, data => showPaths(data, data.saved_version_id),
    '位置已保存，切换后供新任务使用。');
});
pathActivate.addEventListener('click', () => {
  void pathOperation('activate', {version_id:pathVersion.value}, data => showPaths(data, data.path_config.active_version_id),
    '已切换保存位置。');
});
pathRollback.addEventListener('click', () => {
  void pathOperation('rollback', {}, data => showPaths(data, data.path_config.active_version_id), '已恢复上一次保存位置。');
});
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearPoll();
    stopVerificationPoll();
  } else {
    void readVerification();
    if (!view.action && hasActiveRun()) void refresh();
  }
});
window.addEventListener('focus', () => void readVerification());
window.addEventListener('pagehide', () => {
  clearPoll();
  verificationView.suspended = true;
  stopVerificationPoll();
});
window.addEventListener('pageshow', event => {
  verificationView.suspended = false;
  if (event.persisted) void readVerification();
  if (event.persisted && !view.action) void refresh();
});
void readVerification();
void refresh();
