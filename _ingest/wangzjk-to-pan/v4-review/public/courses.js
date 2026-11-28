'use strict';

const list = document.getElementById('calendar-list');
const total = document.getElementById('calendar-total');
const status = document.getElementById('calendar-status');
const more = document.getElementById('calendar-more');
const sentinel = document.getElementById('calendar-sentinel');
const filters = document.getElementById('calendar-filters');
const jump = document.getElementById('calendar-jump');
const datePosition = document.getElementById('calendar-position');
const latest = document.getElementById('calendar-latest');
const selectedFilter = document.getElementById('calendar-selected');
const removeVisible = document.getElementById('calendar-remove-visible');
const clearSelection = document.getElementById('calendar-clear');
const draftCount = document.getElementById('calendar-draft-count');
const selectionNote = document.getElementById('calendar-selection-note');
const clearDialog = document.getElementById('calendar-clear-dialog');
const clearNote = document.getElementById('calendar-clear-note');
const clearError = document.getElementById('calendar-clear-error');
const clearConfirm = document.getElementById('calendar-clear-confirm');
const selection = window.worldanalysisSelection;
const facetTargets = {
  category: document.getElementById('calendar-categories'),
  platform: document.getElementById('calendar-platforms'),
  subcategory: document.getElementById('calendar-subcategories'),
};
const formatNumber = new Intl.NumberFormat('zh-CN');
const formatWeekday = new Intl.DateTimeFormat('zh-CN', {weekday: 'long', timeZone: 'UTC'});
const resourcePattern = /^rv_[0-9a-f]{64}$/;
const cursorPattern = /^[a-zA-Z0-9_.:~-]{1,256}$/;
const dayPattern = /^\d{4}-\d{2}-\d{2}$/;
const groups = new Map();
const seen = new Set();
const coursesByResource = new Map();
let selections = readSelections(location.search);
let facetMeta = null;
let fullVersions = false;
let cursor = '';
let request = null;
let needsReload = false;
let draft = null;
let selectedIds = new Set();
let savingSelection = false;
let changePending = false;
let pendingControl = null;
let refreshSelected = false;
let clearRevision = null;

const observer = typeof IntersectionObserver === 'function' ? new IntersectionObserver(entries => {
  if (entries.some(entry => entry.isIntersecting) && !request) void nextPage(false);
}, {rootMargin: '0px 0px 450px 0px'}) : null;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function readSelections(search) {
  const params = new URLSearchParams(search);
  const day = params.get('day') || '';
  return {
    q: (params.get('q') || '').trim().slice(0, 200),
    category: params.get('category') || '',
    subcategory: params.get('subcategory') || '',
    platform: params.get('platform') || '',
    site: params.get('site') || '',
    day: dayPattern.test(day) ? day : '',
    selected: params.get('selected') === '1' ? '1' : '',
    versions: params.get('versions') === 'all' ? 'all' : '',
  };
}

function writeLocation(values) {
  const params = new URLSearchParams(Object.entries(values).filter(([, value]) => value));
  const next = location.pathname + (params.size ? '?' + params : '');
  if (next !== location.pathname + location.search) history.pushState(null, '', next);
}

function choose(values, selected, select, label) {
  select.replaceChildren(new Option(label, ''));
  for (const value of values) select.add(new Option(value.label, value.id));
  select.value = selected;
}

function paintFacets() {
  if (!facetMeta) return;
  catalogFacets.render(facetTargets, facetMeta, selections, (field, value) => {
    if (selections[field] === value && filters.elements.q.value.trim() === selections.q) return;
    const changes = {[field]: value, q: filters.elements.q.value.trim()};
    if (field === 'category' && value !== selections.category) changes.subcategory = '';
    reset({...selections, ...changes}, true);
  });
}

function linkLibrary(values) {
  const keys = ['q','category','subcategory','platform','site','selected','versions'];
  const query = new URLSearchParams(keys.filter(key => values[key]).map(key => [key, values[key]]));
  if (values.day) query.set('to', values.day);
  if (!query.size) query.set('category', '');
  document.getElementById('calendar-library-link').href = '/?' + query;
}

function dayGroup(day) {
  if (groups.has(day)) return groups.get(day);
  const section = node('section', undefined, 'calendar-group');
  const heading = node('h2', undefined, 'calendar-date');
  heading.id = 'calendar-date-' + (day || 'unknown');
  section.setAttribute('aria-labelledby', heading.id);
  if (day) {
    const time = node('time', day);
    time.dateTime = day;
    const calendarDay = new Date(day + 'T00:00:00Z');
    heading.append(time);
    if (Number.isFinite(calendarDay.getTime())) {
      heading.append(node('span', formatWeekday.format(calendarDay), 'calendar-weekday'));
    }
  } else {
    heading.textContent = '发布日期待核实';
  }
  const body = node('ul', undefined, 'calendar-group-body');
  section.append(heading, body);
  list.append(section);
  groups.set(day, body);
  return body;
}

function visibleSelection() {
  return [...selectedIds].filter(id => coursesByResource.get(id)?.some(view => (
    !view.element.hidden && selection?.isPicked(view.course)
  )));
}

function showSelectionNote(message = '') {
  selectionNote.textContent = message;
  selectionNote.hidden = !message;
}

function showClearError(message = '') {
  clearError.textContent = message;
  clearError.hidden = !message;
}

function paintSelectionBar() {
  const count = selectedIds.size;
  const busy = savingSelection || changePending;
  draftCount.textContent = draft ? formatNumber.format(count) : '';
  const visible = visibleSelection();
  clearSelection.hidden = !count;
  clearSelection.disabled = busy;
  removeVisible.hidden = !visible.length;
  removeVisible.disabled = busy;
  const clearChanged = clearDialog.open && draft?.revision !== clearRevision;
  clearConfirm.disabled = busy || !count || clearChanged;
  if (clearChanged && !busy) showClearError('转存清单已更新，请返回后重新核对。');
}

function paintPick(view) {
  const {course, button, snapshotId} = view;
  const picked = !course.action_pending && selection?.isPicked(course) === true;
  const requestable = !course.action_pending && course.requestable === true && snapshotId;
  button.hidden = !selection || (!picked && !requestable);
  button.disabled = !draft || button === pendingControl;
  button.textContent = picked ? '取消选择' : '选择转存';
  button.setAttribute('aria-pressed', String(picked));
}

function selectionChanged(next, saving) {
  const previous = draft;
  const before = selectedIds;
  draft = next;
  selectedIds = new Set(next?.ids || []);
  savingSelection = saving;
  const changed = [...before].filter(id => !selectedIds.has(id)).concat(
    [...selectedIds].filter(id => !before.has(id))
  );
  const selectedOnly = selections?.selected === '1';
  const affected = previous ? changed : coursesByResource.keys();
  for (const id of affected) {
    for (const view of coursesByResource.get(id) || []) {
      paintPick(view);
      if (!selectedOnly || !before.has(id) || selectedIds.has(id)) continue;
      if (view.element.contains(document.activeElement)) selectedFilter.focus({preventScroll: true});
      view.element.hidden = true;
      const group = view.element.closest('.calendar-group');
      if (group) group.hidden = !group.querySelector('.calendar-row:not([hidden])');
    }
  }

  if (selectedOnly && previous && next && (changed.length || previous.revision !== next.revision)) {
    refreshSelected = true;
  }
  paintSelectionBar();
  if (!saving && !changePending) refreshSelectedView();
}

function refreshSelectedView() {
  if (!refreshSelected || savingSelection || changePending) return;
  refreshSelected = false;
  if (selections?.selected !== '1') return;
  if (list.contains(document.activeElement)) selectedFilter.focus({preventScroll: true});
  reset(selections);
}

async function changeSelection(run, control, duringClear = false) {
  if (!draft || savingSelection || changePending) return false;
  const viewControl = control?.classList.contains('calendar-pick') ? control : null;
  changePending = true;
  pendingControl = viewControl;
  if (viewControl) viewControl.disabled = true;
  paintSelectionBar();
  showSelectionNote();
  showClearError();
  try {
    await run();
    return true;
  } catch (error) {
    const message = error?.status === 409 ? '资料或选择已更新，请重新核对。' : '未能保存选择，请重试。';
    if (duringClear && clearDialog.open) showClearError(message);
    else showSelectionNote(message);
    return false;
  } finally {
    changePending = false;
    pendingControl = null;
    if (viewControl) viewControl.disabled = !draft;
    paintSelectionBar();
    refreshSelectedView();
  }
}

function courseActions(course, element, snapshotId) {
  const actions = node('span', undefined, 'calendar-row-actions');
  if (course.action_pending) return actions;
  const pick = node('button', '选择转存', 'calendar-pick');
  pick.type = 'button';
  pick.setAttribute('aria-label', '转存选择 ' + course.title);
  const view = {element, course, button: pick, snapshotId};
  const linked = coursesByResource.get(course.id) || [];
  linked.push(view);
  coursesByResource.set(course.id, linked);
  pick.addEventListener('click', () => {
    void changeSelection(() => selection.toggle(course, !selection.isPicked(course), snapshotId), pick);
  });
  paintPick(view);
  actions.append(pick);
  const analysis = window.worldanalysisAnalysisPriority?.articleAction(course, {compact: true});
  if (analysis) actions.append(analysis);
  return actions;
}

function addCourses(items, multipleSites, snapshotId) {
  let firstLink = null;
  for (const course of items) {
    const key = catalogFacets.displayId(course);
    if (!resourcePattern.test(course.id) || seen.has(key)) continue;
    const row = node('li', undefined, 'calendar-row');
    const title = node('a', undefined, 'calendar-link');
    title.href = catalogFacets.detailPath(course);
    title.append(node('span', course.title || '查看课程详情', 'calendar-title'));
    const meta = node('span', undefined, 'calendar-meta');
    if (multipleSites && course.site_label) meta.append(node('span', course.site_label));
    if (course.category_label) meta.append(node('span', course.category_label));
    const platforms = catalogFacets.labels(course, selections.platform);
    if (platforms.length) meta.append(node('span', platforms.join('、')));
    const relation = course.version_count > 1 ? 'versions' : course.source_count > 1 ? 'sources' : '';
    if (relation) {
      const show = node('a', relation === 'versions' ? '其他版本' : '其他来源', 'calendar-relation');
      show.href = catalogFacets.detailPath(course, relation);
      meta.append(show);
    }
    if (course.has_baidu) meta.append(node('span', '有百度链接', 'calendar-baidu'));
    const states = catalogFacets.statusText(course).split(' · ').filter(Boolean);
    for (const state of states) meta.append(node('span', state, 'calendar-state'));
    row.append(title, meta, courseActions(course, row, snapshotId));
    const day = typeof course.published_at === 'string' && dayPattern.test(course.published_at)
      ? course.published_at : '';
    dayGroup(day).append(row);
    firstLink ||= title;
    seen.add(key);
  }
  paintSelectionBar();
  return firstLink;
}

async function nextPage(manual) {
  if (request || cursor === null || needsReload) return;
  const controller = new AbortController();
  const pageCursor = cursor;
  request = controller;
  observer?.unobserve(sentinel);
  more.disabled = true;
  list.setAttribute('aria-busy', 'true');
  status.textContent = seen.size ? '正在加载更多课程…' : '正在加载课程…';
  let readyToObserve = false;
  let firstNewLink = null;
  try {
    const params = new URLSearchParams(Object.entries(selections).filter(([, value]) => value));
    if (pageCursor) params.set('after', pageCursor);
    const response = await fetch('/api/course-calendar?' + params, {
      cache: 'no-store', credentials: 'omit', signal: controller.signal,
    });
    if (request !== controller) return;
    if (!response.ok) {
      if (response.status === 409) needsReload = true;
      throw new Error('课程读取未完成');
    }
    const data = await response.json();
    if (request !== controller) return;
    if (!Array.isArray(data.items) || !Number.isSafeInteger(data.total) ||
        data.next !== null && (typeof data.next !== 'string' || !cursorPattern.test(data.next) || data.next === pageCursor)) {
      throw new Error('课程目录不可读');
    }
    if (!pageCursor) {
      facetMeta = data;
      selections = catalogFacets.normalize(selections, facetMeta);
      paintFacets();
      linkLibrary(selections);
      choose(data.sites, selections.site, filters.elements.site, '全部来源');
      filters.elements.site.hidden = data.sites.length < 2;
    }
    firstNewLink = addCourses(data.items, data.sites.length > 1, data.snapshot_id);
    const tally = [formatNumber.format(data.total) + (selections.versions === 'all' ? ' 个版本' : ' 个项目')];
    if (data.version_total > data.total) tally.push(formatNumber.format(data.version_total) + ' 个版本');
    if (data.source_total > data.total) tally.push(formatNumber.format(data.source_total) + ' 篇来源');
    total.textContent = tally.join(' · ');
    cursor = data.next;
    more.textContent = '加载更多课程';
    more.hidden = cursor === null;
    status.textContent = !seen.size ? (selections.selected === '1' ? '当前范围没有已选课程。' : '没有符合条件的课程。')
      : cursor === null ? '已显示全部课程。'
      : '已显示 ' + formatNumber.format(seen.size) + ' 门';
    readyToObserve = cursor !== null;
  } catch {
    if (controller.signal.aborted || request !== controller) return;
    status.textContent = needsReload ? '课程目录已更新，请重新加载。' : '未能加载课程，请重试。';
    more.textContent = needsReload ? '重新加载课程表' : '重试加载';
    more.hidden = false;
  } finally {
    if (request === controller) {
      request = null;
      more.disabled = false;
      list.setAttribute('aria-busy', 'false');
      if (manual) firstNewLink?.focus();
      if (readyToObserve) observer?.observe(sentinel);
    }
  }
}

function reset(values, remember = false) {
  request?.abort();
  observer?.unobserve(sentinel);
  request = null;
  cursor = '';
  needsReload = false;
  groups.clear();
  seen.clear();
  coursesByResource.clear();
  list.replaceChildren();
  total.textContent = '';
  more.hidden = true;
  if (!fullVersions) values = {...values, versions: ''};
  selections = facetMeta ? catalogFacets.normalize(values, facetMeta) : values;
  selectedFilter.setAttribute('aria-pressed', String(selections.selected === '1'));
  filters.elements.q.value = values.q;
  filters.elements.site.value = values.site;
  filters.elements.versions.value = selections.versions === 'all' ? 'all' : 'latest';
  jump.elements.day.value = values.day;
  latest.hidden = !values.day;
  datePosition.hidden = !values.day;
  datePosition.textContent = values.day ? '查看 ' + values.day + ' 及之前发布的课程' : '';
  paintFacets();
  paintSelectionBar();
  linkLibrary(selections);
  if (remember) writeLocation(selections);
  void nextPage(false);
}

function searchCourses() {
  reset({
    ...selections,
    q: filters.elements.q.value.trim(),
    site: filters.elements.site.value,
    versions: filters.elements.versions.value === 'all' ? 'all' : '',
  }, true);
}

filters.addEventListener('submit', event => {
  event.preventDefault();
  searchCourses();
});
filters.elements.site.addEventListener('change', searchCourses);
filters.elements.versions.addEventListener('change', searchCourses);
jump.addEventListener('submit', event => {
  event.preventDefault();
  if (jump.reportValidity()) reset({...selections, day: jump.elements.day.value}, true);
});
latest.addEventListener('click', () => {
  reset({...selections, day: ''}, true);
  jump.elements.day.focus();
});
more.addEventListener('click', () => {
  if (needsReload) reset(selections);
  else void nextPage(true);
});
selectedFilter.addEventListener('click', () => {
  reset({...selections, selected: selections.selected === '1' ? '' : '1'}, true);
  if (selections.selected === '1') void loadSelection();
});
removeVisible.addEventListener('click', async () => {
  const ids = visibleSelection();
  if (!ids.length) return;
  if (await changeSelection(() => selection.removeMany(ids), removeVisible) && removeVisible.hidden) {
    selectedFilter.focus({preventScroll: true});
  }
});
clearSelection.addEventListener('click', () => {
  if (!selectedIds.size || changePending || savingSelection) return;
  clearRevision = draft.revision;
  clearNote.textContent = '取消转存清单中全部 ' + formatNumber.format(selectedIds.size) + ' 项选择？';
  showClearError();
  clearDialog.showModal();
  paintSelectionBar();
});
clearConfirm.addEventListener('click', async () => {
  if (!selectedIds.size || draft?.revision !== clearRevision) return;
  if (await changeSelection(() => selection.clear(clearRevision), clearConfirm, true)) {
    if (clearDialog.open) clearDialog.close();
    selectedFilter.focus({preventScroll: true});
  }
});
clearDialog.addEventListener('close', () => { clearRevision = null; });
async function loadSelection() {
  if (!selection) return;
  try {
    await selection.load();
  } catch {
    showSelectionNote('未能读取转存清单，请刷新页面。');
  }
}
selection?.subscribe(selectionChanged);
window.addEventListener('pageshow', event => {
  if (event.persisted) void loadSelection();
});
window.addEventListener('popstate', () => reset(readSelections(location.search)));
async function startCalendar() {
  filters.elements.versions.disabled = true;
  try {
    const response = await fetch('/api/meta', {cache: 'no-store'});
    if (response.ok) fullVersions = (await response.json()).catalog_version_filter === true;
  } catch {}
  filters.elements.versions.disabled = !fullVersions;
  reset(readSelections(location.search));
  void loadSelection();
}
void startCalendar();
