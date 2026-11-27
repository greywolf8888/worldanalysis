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
  void refresh();
});
document.addEventListener('visibilitychange', () => {
  if (document.hidden) clearPoll();
  else if (!view.action && hasActiveRun()) void refresh();
});
window.addEventListener('pagehide', clearPoll);
window.addEventListener('pageshow', event => {
  if (event.persisted && !view.action) void refresh();
});
void refresh();
