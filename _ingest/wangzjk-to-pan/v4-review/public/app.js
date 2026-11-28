'use strict';

const $ = id => document.getElementById(id);
function el(tag, value = '', className = '') {
  const node = document.createElement(tag);
  if (value !== null) node.textContent = value;
  if (className) node.className = className;
  return node;
}
const numberFormat = new Intl.NumberFormat('zh-CN');
const fmt = value => numberFormat.format(value);
const amountFormat = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 2});
const DEFAULT = {q:'', category:'', subcategory:'', platform:'', status:'', site:'', from:'', to:'', order:'newest', view:'grid', selected:'0', baidu:'0', versions:'latest', page:1, scroll:0};
const urlFilters = ['q','category','subcategory','platform','status','site','from','to','order','selected','baidu','versions','page'];
const calendarFilters = ['q','category','subcategory','platform','site'];
const selection = window.worldanalysisSelection;
const facetTargets = {category:$('topics'), platform:$('platforms'), subcategory:$('subcategories')};
const resourceIdPattern = /^rv_[0-9a-f]{64}$/;
const articleKeyPattern = /^ca_[0-9a-f]{64}$/;
const PREFS_KEY = 'wa-v4-view:existing-library';
const resourceStates = {
  verified:'曾转存', recycled:'已回收', restricted:'原文访问受限'
};
const bulkLabels = {page:'本页全选', filtered:'筛选结果全选', site_all:'全库全选'};
const transferPageSize = 60;
const baseCatalogLimit = 16;

let filter = {...DEFAULT};
let meta = null;
let page = null;
let renderedFilter = null;
let renderedPageFilter = null;
let catalogResizeTimer;
let draft = {revision:0, ids:[]};
let draftDetails = null;
let querySequence = 0;
let queryController = null;
let draftPageRequest = 0;
let paintedFacets = null;
let readerSequence = 0;
let readerRow = null;
let readerOpener = null;
let loaded = false;
let querying = false;
let saving = false;
let confirmedAction = null;
let transferListPage = 1;

async function api(path, body, {signal} = {}) {
  const options = {cache:'no-store', credentials:'omit', signal};
  if (body !== undefined) Object.assign(options, {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
  });
  const response = await fetch(path, options);
  const data = await response.json();
  if (response.ok) return data;
  const error = new Error('请求未完成');
  error.status = response.status;
  error.current = data.draft;
  throw error;
}
function shortDate(value) {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}/.test(value) ? value.slice(0, 10) : '';
}
function showSize(bytes) {
  if (!Number.isSafeInteger(bytes) || bytes < 0) return '';
  for (const [factor, unit] of [[1e12,'TB'],[1e9,'GB'],[1e6,'MB'],[1e3,'KB']]) {
    if (bytes >= factor) return amountFormat.format(bytes / factor) + ' ' + unit;
  }
  return fmt(bytes) + ' 字节';
}
function button(label, className = '') {
  const b = el('button', label, className);
  b.type = 'button';
  return b;
}
function openDialog(id) {
  const dialog = $(id);
  if (!dialog.open) dialog.showModal();
}
function closeDialog(dialog) {
  if (dialog?.open) dialog.close();
}
function notice(id, text = '') {
  const node = $(id);
  node.textContent = text;
  node.hidden = !text;
}
function selectionFailure(error) {
  if (error.status === 409) return '资料或选择已更新，请重新核对。';
  if (error.status === 422) return '部分资料暂不能加入，请重新选择。';
  return '未能保存选择，请重试。';
}
function resourceState(row) {
  return catalogFacets.statusText(row);
}
function canRequestTransfer(row) {
  return !row.action_pending && (row.requestable ?? row.selectable);
}
function isPicked(row) {
  return selection.isPicked(row);
}
function priorityAction(row) {
  return !row.action_pending && typeof analysisArticleAction === 'function' ? analysisArticleAction(row) : null;
}
function imageURL(value) {
  try {
    const u = new URL(value);
    return u.origin === 'https://www.wangzjk.com' && u.pathname.startsWith('/wp-content/uploads/') &&
      !u.username && !u.password && !u.search && !u.hash && /\.(?:png|jpe?g|webp|gif)$/i.test(u.pathname) ? u.href : null;
  } catch { return null; }
}
function original(value, origin) {
  try {
    const u = new URL(value);
    return u.protocol === 'https:' && u.origin === origin && u.pathname !== '/' &&
      !u.username && !u.password && !u.search && !u.hash ? u.href : null;
  } catch { return null; }
}
function image(row, fallback) {
  const url = imageURL(row?.url);
  if (!url) return null;
  const img = el('img', null);
  img.alt = row.alt || '原文图片';
  img.loading = 'lazy';
  img.decoding = 'async';
  img.referrerPolicy = 'no-referrer';
  img.addEventListener('error', fallback, {once:true});
  img.src = url;
  return img;
}
function outsideLink(label, url, className = '') {
  const a = el('a', label, className);
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  return a;
}
function prefs() {
  try {
    const raw = localStorage.getItem(PREFS_KEY) ||
      localStorage.getItem('wa-v4-view:' + meta.snapshot_id) ||
      localStorage.getItem('wa-v4-view:0672be837662755c2539c596728da81da983bccf20dea500b20517df843f7081');
    const value = JSON.parse(raw);
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    return Object.fromEntries([...Object.keys(DEFAULT), 'pageLimit'].filter(k => Object.hasOwn(value, k)).map(k => [k, value[k]]));
  } catch { return {}; }
}
function remember() {
  if (!loaded) return;
  try {
    const scroll = Math.max(0, Math.round(window.scrollY));
    localStorage.setItem(PREFS_KEY, JSON.stringify({...filter, pageLimit:page?.limit ?? baseCatalogLimit, scroll}));
  } catch {}
}
function readLocationFilters() {
  const params = new URLSearchParams(location.search);
  if (!urlFilters.some(key => params.has(key))) return null;
  const result = {...DEFAULT};
  for (const key of urlFilters) {
    if (!params.has(key)) continue;
    const value = params.get(key);
    if (key === 'page') {
      if (/^[1-9]\d{0,5}$/.test(value) && Number(value) <= 100000) result.page = Number(value);
    } else if (value.length <= 200 && !value.includes('\0')) {
      result[key] = value;
    }
  }
  if (!['newest','oldest','title'].includes(result.order)) result.order = DEFAULT.order;
  if (result.versions !== 'all') result.versions = DEFAULT.versions;
  for (const key of ['selected','baidu']) if (result[key] !== '1') result[key] = '0';
  if (result.site && !meta.sites.some(item => item.id === result.site)) result.site = '';
  if (result.status && !resourceStates[result.status]) result.status = '';
  for (const key of ['from','to']) {
    const value = result[key];
    const date = new Date(value + 'T00:00:00Z');
    if (value && (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(date.getTime()) ||
        date.toISOString().slice(0, 10) !== value)) result[key] = '';
  }
  if (result.from > result.to && result.to) result.from = result.to = '';
  return catalogFacets.normalize(result, meta);
}
function writeLocationFilters(mode) {
  const params = new URLSearchParams(location.search);
  for (const key of ['resource','article','details','panel']) params.delete(key);
  for (const key of urlFilters) {
    if (filter[key] === DEFAULT[key]) params.delete(key);
    else params.set(key, filter[key]);
  }
  if (!urlFilters.some(key => params.has(key))) params.set('category', '');
  const next = location.pathname + '?' + params + location.hash;
  if (next !== location.pathname + location.search + location.hash) history[mode](null, '', next);
}
function setPressed(id, pressed) {
  $(id).setAttribute('aria-pressed', String(pressed));
}
function syncControls({search = true} = {}) {
  const fullVersions = meta.catalog_version_filter === true;
  $('versions').disabled = !fullVersions;
  if (!fullVersions) filter.versions = DEFAULT.versions;
  if (search) $('q').value = filter.q;
  for (const id of ['status','site','from','to','order','versions']) $(id).value = filter[id];
  setPressed('grid', filter.view === 'grid');
  setPressed('list', filter.view === 'list');
  setPressed('available', filter.baidu === '1');
  setPressed('selected', filter.selected === '1');
  paintTopics();
  const extraCount = ['site','from','to','status'].filter(k => filter[k]).length;
  notice('filter-count', extraCount ? String(extraCount) : '');
  const topic = meta.categories.find(item => item.id === filter.category);
  const platform = meta.platforms?.find(item => item.id === filter.platform);
  $('result-title').textContent = [topic?.label, platform?.label].filter(Boolean).join(' · ') || '全部资料';
  const params = new URLSearchParams(calendarFilters.filter(key => filter[key]).map(key => [key, filter[key]]));
  if (filter.versions === 'all') params.set('versions', 'all');
  if (filter.selected === '1') params.set('selected', '1');
  if (filter.to) params.set('day', filter.to);
  $('catalog-calendar-link').href = '/courses' + (params.size ? '?' + params : '');
}
function applyFilter(changes) {
  if (!loaded) return;
  if (changes.category !== undefined && changes.category !== filter.category) changes = {...changes, subcategory:''};
  filter = {...filter, q:$('q').value.trim(), ...changes, page:1};
  syncControls();
  void query({top:true, locationMode:'pushState'});
}
function paintTopics() {
  const {category, subcategory, platform} = filter;
  if (paintedFacets?.meta === meta && paintedFacets.category === category
      && paintedFacets.subcategory === subcategory && paintedFacets.platform === platform) return;
  paintedFacets = {meta, category, subcategory, platform};
  catalogFacets.render(facetTargets, meta, filter, (field, id) => {
    if (filter[field] !== id || $('q').value.trim() !== filter.q) {
      applyFilter({[field]:id});
    }
  });
}
function setChoices(id, data, empty, getLabel = row => row.label) {
  const select = $(id);
  const blank = el('option', empty);
  blank.value = '';
  select.replaceChildren(blank, ...data.map(row => {
    const option = el('option', getLabel(row));
    option.value = row.id;
    return option;
  }));
}
function setInteractionState() {
  const blocked = saving || querying || !page;
  $('cards').setAttribute('aria-busy', String(querying));
  for (const box of document.querySelectorAll('[data-resource-pick]')) box.disabled = blocked;
  for (const b of document.querySelectorAll('[data-scope]')) {
    b.disabled = blocked || !Number(b.dataset.count) || b.dataset.overLimit === 'true';
  }
  if (page) {
    $('first').disabled = $('prev').disabled = blocked || page.page <= 1;
    $('next').disabled = $('last').disabled = blocked || page.page >= page.pages;
    $('jump-number').disabled = $('jump-submit').disabled = blocked || page.pages <= 1;
    $('page-numbers').querySelectorAll('button').forEach(b => { b.disabled = blocked; });
  }
  if ($('draft').open) {
    $('freeze').disabled = saving || draftDetails?.revision !== draft.revision || !draftDetails.can_review;
    for (const control of $('draft-rows').querySelectorAll('[data-queue-action]')) {
      control.disabled = saving || draftDetails?.revision !== draft.revision || control.dataset.boundary === 'true';
    }
    $('clear-all').disabled = saving || !draft.ids.length;
  }
  $('clear-page-selection').disabled = blocked || !page?.items.some(isPicked);
  $('clear-catalog-selection').disabled = saving || !draft.ids.length;
}
function paintPick(box) {
  const picked = selection.isPicked(box.dataset.selectionId);
  box.checked = picked;
  const label = picked ? '取消选择' : '选择转存';
  box.nextElementSibling.textContent = label;
  box.setAttribute('aria-label', label + '：' + box.dataset.title);
  box.closest('label').hidden = !picked && box.dataset.requestable === 'false';
  const card = box.closest('article[data-version]');
  if (card) {
    if (picked) card.dataset.picked = 'true';
    else delete card.dataset.picked;
  }
}
function selectionControl(row, location) {
  const picked = isPicked(row);
  if (!canRequestTransfer(row) && !picked) return null;
  const label = el('label', null, 'pickline');
  const box = el('input', null, 'pick');
  box.type = 'checkbox';
  box.dataset.resourcePick = catalogFacets.displayId(row);
  box.dataset.selectionId = row.id;
  box.dataset.title = row.title;
  box.dataset.requestable = String(Boolean(canRequestTransfer(row)));
  box.disabled = saving || querying;
  box.addEventListener('change', () => {
    const next = box.checked;
    const hadFocus = document.activeElement === box;
    paintPick(box);
    const source = next && row.article_key ? {article_key:row.article_key} : {};
    void mutate('toggle', {id:row.id, selected:next, ...source}).then(() => {
      if (!hadFocus || ![document.body, box].includes(document.activeElement)) return;
      if (location === 'reader' && !$('reader').open) return;
      const region = $(location === 'reader' ? 'reader-selection' : 'cards');
      const replacement = Array.from(region.querySelectorAll('[data-resource-pick]')).find(input => input.dataset.resourcePick === catalogFacets.displayId(row));
      (replacement || (location === 'reader' ? $('reader').querySelector('[data-close]') : $('result-title'))).focus({preventScroll:true});
    });
  });
  label.append(box, el('span', '', location === 'card' ? 'card-choice-label' : ''));
  paintPick(box);
  return label;
}
function cardExcerpt(row) {
  const excerpt = (row.card_summary ?? row.excerpt)?.trim();
  if (!excerpt) return '';
  const title = row.title?.trim();
  const preview = title && excerpt.startsWith(title)
    ? excerpt.slice(title.length).replace(/^[\s：:·，、—-]+/, '')
    : excerpt;
  return /^(?:\[\s*(?:\.{3}|…)\s*\]|…)\s*$/u.test(preview) ? '' : preview;
}
function card(row) {
  const article = el('article', null, 'card');
  article.dataset.version = catalogFacets.displayId(row);
  if (isPicked(row)) article.dataset.picked = 'true';
  const main = el('div', null, 'card-main');
  const preview = button('', 'photo');
  const picture = image(row.cover, () => preview.remove());
  if (picture) {
    preview.setAttribute('aria-label', '查看封面与详情：' + row.title);
    preview.append(picture);
    preview.addEventListener('click', () => void openReader(row, preview));
    main.append(preview);
  }
  const copy = el('div', null, 'card-copy');
  const header = el('h3', null);
  const title = button(row.title, 'title');
  title.addEventListener('click', () => void openReader(row, title));
  header.append(title);
  copy.append(header);
  const excerpt = cardExcerpt(row);
  if (excerpt) copy.append(el('p', excerpt, 'excerpt'));
  main.append(copy);
  const foot = el('div', null, 'card-foot');
  const metadata = el('div', null, 'card-meta');
  const category = meta.categories.find(item => item.id === row.category);
  if (category) {
    const topic = button(category.label, 'card-topic');
    topic.addEventListener('click', () => applyFilter({category:category.id}));
    metadata.append(topic);
  }
  const platformLabels = catalogFacets.labels(row, filter.platform);
  if (platformLabels.length) metadata.append(el('span', platformLabels.join('、'), 'card-platform'));
  if (row.version_label) metadata.append(el('span', row.version_label, 'card-version'));
  const time = shortDate(row.published_at);
  if (time) {
    const date = el('time', time);
    date.dateTime = time;
    date.title = '发布日期';
    metadata.append(date);
  }
  if (row.site_label) metadata.append(el('span', row.site_label, 'card-source'));
  foot.append(metadata);
  const actions = el('div', null, 'card-tail');
  const relation = Number(row.version_count) > 1 ? 'versions' : Number(row.source_count) > 1 ? 'sources' : '';
  if (relation) {
    const c = button(relation === 'versions' ? '其他版本' : '其他来源', 'card-topic card-related');
    c.addEventListener('click', () => void openReader(row, c, relation));
    actions.append(c);
  }
  const state = resourceState(row);
  if (state) actions.append(el('span', state, 'card-state'));
  const control = selectionControl(row, 'card');
  if (control) actions.append(control);
  const analysis = priorityAction(row);
  if (analysis) actions.append(analysis);
  if (actions.childElementCount) foot.append(actions);
  article.append(main, foot);
  return article;
}
function cardsFrom(result) {
  const focused = document.activeElement;
  const active = focused?.closest('article[data-version]');
  const restore = active && $('cards').contains(active);
  const cardType = focused?.matches('[data-resource-pick]') ? '.pick' :
    focused?.matches('.photo') ? '.photo' : focused?.matches('.card-related') ? '.card-related' :
    focused?.matches('.card-topic') ? '.card-topic' : '.title';
  const cards = result.items.map(card);
  $('cards').className = 'cards ' + renderedFilter.view;
  $('cards').dataset.fullPage = String(result.items.length === result.limit);
  $('cards').dataset.pageLimit = String(result.limit);
  $('cards').replaceChildren(...cards);
  if (restore) cards.find(c => c.dataset.version === active.dataset.version)?.querySelector(cardType)?.focus({preventScroll:true});
}
function pagination() {
  const {page:current, pages, limit, total, items} = page;
  $('pagination').hidden = pages <= 1;
  const start = (current - 1) * limit + 1;
  $('page-range').textContent = total ? fmt(start) + '–' + fmt(start + items.length - 1) + ' 项，共 ' + fmt(total) + ' 项' : '';
  $('page-label').textContent = '第 ' + fmt(current) + ' / ' + fmt(pages) + ' 页';
  const jump = $('jump-number');
  jump.max = String(pages);
  jump.placeholder = String(current);
  if (jump.valueAsNumber === current || jump.valueAsNumber > pages) jump.value = '';
  const holder = $('page-numbers');
  const restoreFocus = holder.contains(document.activeElement);
  const values = pages <= 7 ? Array.from({length:pages}, (_, index) => index + 1) :
    current <= 4 ? [1, 2, 3, 4, 5, null, pages] :
      current >= pages - 3 ? [1, null, pages - 4, pages - 3, pages - 2, pages - 1, pages] :
        [1, null, current - 1, current, current + 1, null, pages];
  holder.replaceChildren(...values.map(n => {
    const item = el('li');
    if (n === null) {
      item.setAttribute('aria-hidden', 'true');
      item.append(el('span', '…', 'page-ellipsis'));
    } else {
      const label = (n === 1 ? '首页，第 ' : n === pages ? '末页，第 ' : '第 ') + fmt(n) + ' 页';
      const control = n === current ? el('span', fmt(n), 'active-page') : button(fmt(n));
      control.setAttribute('aria-label', label);
      if (n === current) control.setAttribute('aria-current', 'page');
      else control.addEventListener('click', () => goPage(n));
      item.append(control);
    }
    return item;
  }));
  if (restoreFocus) $('result-title').focus({preventScroll:true});
}
function renderSelection() {
  const selectedHere = page?.items.filter(isPicked).length ?? 0;
  const elsewhere = draft.ids.length - selectedHere;
  $('chosen').textContent = fmt(draft.ids.length);
  $('selection-tools').hidden = !draft.ids.length;
  notice('selection', draft.ids.length ? '已选 ' + fmt(draft.ids.length) + ' 项' +
    (elsewhere > 0 ? '，' + fmt(elsewhere) + ' 项不在本页' : '') : '');
  $('clear-page-selection').hidden = !selectedHere;
  for (const box of document.querySelectorAll('[data-resource-pick]')) paintPick(box);
}
function render() {
  cardsFrom(page);
  $('empty').hidden = page.total > 0;
  const tally = [fmt(page.total) + (filter.versions === 'all' ? ' 个版本' : ' 个项目')];
  if (Number.isSafeInteger(page.version_total) && page.version_total > page.total) tally.push(fmt(page.version_total) + ' 个版本');
  if (Number.isSafeInteger(page.source_total) && page.source_total > page.total) tally.push(fmt(page.source_total) + ' 篇来源');
  $('result-count').textContent = tally.join(' · ');
  renderSelection();
  pagination();
  const counts = {page:page.page_selectable, filtered:page.eligible, site_all:page.index_selectable};
  const projects = {page:page.items.length, filtered:page.total, site_all:page.index_total ?? meta.totals.projects};
  const pending = {page:page.page_selection_pending, filtered:page.selection_pending, site_all:page.index_selection_pending};
  const maxItems = Number.isSafeInteger(page.max_draft_items) && page.max_draft_items > 0 ? page.max_draft_items : 5000;
  notice('bulk-limit-note', Object.values(counts).some(count => count > maxItems) ?
    '每张清单最多 ' + fmt(maxItems) + ' 项，请缩小选择范围。' : '');
  for (const b of document.querySelectorAll('[data-scope]')) {
    const scope = b.dataset.scope;
    b.dataset.count = String(counts[scope]);
    b.dataset.projects = String(projects[scope] ?? counts[scope]);
    b.dataset.pending = String(pending[scope] || 0);
    b.dataset.overLimit = String(counts[scope] > maxItems);
    const countsLabel = [fmt(counts[scope]) + ' 项可选'];
    if (pending[scope]) countsLabel.push(fmt(pending[scope]) + ' 项待核实');
    b.replaceChildren(el('span', bulkLabels[scope]), el('small', countsLabel.join(' · ')));
  }
  syncControls({search:false});
}
function fittedCatalogLimit() {
  const cardGrid = $('cards');
  const style = getComputedStyle(cardGrid);
  const rowHeight = parseFloat(style.getPropertyValue('--desktop-card-row'));
  const columns = Number(style.getPropertyValue('--desktop-card-columns'));
  if (!rowHeight || !columns || !Array.isArray(meta?.catalog_page_limits)) return baseCatalogLimit;
  const gridTop = cardGrid.getBoundingClientRect().top + window.scrollY;
  const capacity = Math.floor((window.innerHeight - gridTop - 52) / rowHeight) * columns;
  return meta.catalog_page_limits.reduce((limit, size) => size <= capacity ? Math.max(limit, size) : limit, baseCatalogLimit);
}
function scheduleCatalogFit() {
  clearTimeout(catalogResizeTimer);
  catalogResizeTimer = setTimeout(() => {
    if (!page || saving || querying || document.querySelector('dialog[open]')) return;
    const limit = fittedCatalogLimit();
    if (limit === page.limit) return;
    const cards = Array.from($('cards').children);
    const offset = window.scrollY ? Math.max(0, cards.findIndex(card => card.getBoundingClientRect().bottom > 0)) : 0;
    const card = cards[offset];
    const anchor = card && window.scrollY ? {id:card.dataset.version, top:card.getBoundingClientRect().top} : null;
    filter.page = Math.floor(((page.page - 1) * page.limit + offset) / limit) + 1;
    void query({limit, anchor});
  }, 180);
}
async function query({top = false, restore = null, focusResult = false, limit = page?.limit ?? fittedCatalogLimit(), anchor = null, locationMode = null} = {}) {
  const token = ++querySequence;
  queryController?.abort();
  const controller = new AbortController();
  queryController = controller;
  const request = {...filter};
  const pageFilter = {...request, view:'list'};
  if (pageFilter.versions === DEFAULT.versions) delete pageFilter.versions;
  if (limit !== baseCatalogLimit) pageFilter.limit = limit;
  querying = true;
  notice('error');
  setInteractionState();
  try {
    const params = new URLSearchParams({...pageFilter, draft_revision:draft.revision});
    const result = await api('/api/resources?' + params, undefined, {signal:controller.signal});
    if (token !== querySequence) return;
    if (meta.snapshot_id !== result.snapshot_id) {
      const next = await api('/api/meta', undefined, {signal:controller.signal});
      if (token !== querySequence) return;
      if (next.snapshot_id === result.snapshot_id) {
        meta = next;
      }
    }
    page = result;
    renderedFilter = {...request, page:result.page};
    renderedPageFilter = {...pageFilter, page:result.page};
    filter.page = result.page;
    selection.adopt(result.draft);
    render();
    if (restore !== null) window.scrollTo(0, restore);
    if (anchor) {
      const keep = Array.from($('cards').children).find(card => card.dataset.version === anchor.id);
      if (keep) window.scrollBy(0, keep.getBoundingClientRect().top - anchor.top);
    }
    if (top) window.scrollTo(0, 0);
    if (focusResult) $('result-title').focus({preventScroll:true});
    if (locationMode) writeLocationFilters(locationMode);
    remember();
    scheduleCatalogFit();
  } catch {
    if (token !== querySequence) return;
    if (renderedFilter) {
      filter = {...renderedFilter};
      syncControls();
    }
    notice('error', '未能加载资料，请重试。');
  } finally {
    if (token === querySequence) {
      querying = false;
      setInteractionState();
    }
  }
}
function goPage(number) {
  if (!page || querying || saving || !Number.isSafeInteger(number) || number < 1 || number > page.pages || number === page.page) return;
  filter.page = number;
  void query({top:true, focusResult:true, locationMode:'pushState'});
}
async function mutate(action, values = {}, revision = draft.revision) {
  if (!loaded || !page || saving || querying) return;
  const reviewWasOpen = $('draft').open;
  saving = true;
  notice('error');
  if (reviewWasOpen) notice('review');
  setInteractionState();
  try {
    const requireSnapshot = action === 'bulk' || action === 'toggle' && values.selected;
    const payload = requireSnapshot ? {snapshot_id:page.snapshot_id, ...values} : values;
    await selection.mutate(action, payload, revision);
    if (reviewWasOpen && $('draft').open) await refreshDraft();
    if (filter.selected === '1' && action !== 'move') await query();
  } catch (error) {
    if (error.status === 409) {
      if (reviewWasOpen && $('draft').open) await refreshDraft();
      await query();
    }
    notice(reviewWasOpen && $('draft').open ? 'review' : 'error', selectionFailure(error));
  } finally {
    saving = false;
    renderSelection();
    setInteractionState();
  }
}
function queueButton(label, action, boundary = false) {
  const b = button(label, 'quiet-button');
  b.dataset.queueAction = 'true';
  b.dataset.boundary = String(boundary);
  b.disabled = saving || boundary;
  b.addEventListener('click', action);
  return b;
}
function moveTransfer(id, position) {
  if (saving || querying) return;
  transferListPage = Math.ceil(position / transferPageSize);
  void mutate('move', {id, position});
}
function paintTransferPages(total) {
  const pageCount = Math.max(1, Math.ceil(total / transferPageSize));
  const holder = $('draft-pages');
  transferListPage = Math.min(transferListPage, pageCount);
  holder.hidden = pageCount === 1;
  if (holder.hidden) return;
  const previous = queueButton('清单上一页', () => changeTransferPage(-1), transferListPage === 1);
  const following = queueButton('清单下一页', () => changeTransferPage(1), transferListPage === pageCount);
  holder.replaceChildren(previous,el('span','第 '+fmt(transferListPage)+' / '+fmt(pageCount)+' 页'),following);
}
async function changeTransferPage(offset) {
  if (saving) return;
  transferListPage += offset;
  if (!await refreshDraft()) return;
  $('draft').querySelector('.drawer-scroll').scrollTop = 0;
  $('draft-pages').querySelector('button:not(:disabled)')?.focus({preventScroll:true});
}
function paintDraft() {
  const {entries:items, ids} = draftDetails;
  const total = ids.length;
  notice('draft-note', total ? fmt(total) + ' 项，按下方顺序排列' : '转存清单为空。');
  $('draft-actions').hidden = !total;
  $('clear-all').hidden = !total;
  const focusedItem = document.activeElement?.closest('.draftline');
  const focusedItemId = focusedItem?.dataset.queueId;
  const focusedAction = document.activeElement?.getAttribute('aria-label');
  paintTransferPages(total);
  const offset = (transferListPage - 1) * transferPageSize;
  const rows = items.map((row, pageIndex) => {
    const index = offset + pageIndex;
    const li = el('li', null, 'draftline');
    li.dataset.queueId = row.id;
    const details = el('div', null, 'draft-copy');
    details.append(el('strong', row.title));
    const info = [row.category_label, row.site_label, resourceState(row), showSize(row.range_total_bytes),
      row.stale ? '资料已更新，请重新选择' : ''].filter(Boolean);
    if (info.length) details.append(el('p', info.join(' · '), 'muted'));
    const controls = el('div', null, 'draft-controls');
    const first = queueButton('移到首位', () => moveTransfer(row.id,1), index === 0);
    const second = queueButton('移到第二位', () => moveTransfer(row.id,2), index === 1 || total < 2);
    const up = queueButton('上移', () => moveTransfer(row.id,index), index === 0);
    const down = queueButton('下移', () => moveTransfer(row.id,index+2), index === total - 1);
    const remove = queueButton('取消选择', () => void mutate('remove', {id:row.id}));
    const n = el('input');
    n.type = 'number';
    n.min = '1';
    n.max = String(total);
    n.value = String(index + 1);
    n.setAttribute('aria-label', '指定位置：' + row.title);
    const jump = queueButton('移到此位', () => {
      if (n.reportValidity()) moveTransfer(row.id,Number(n.value));
    });
    for (const control of [first, second, up, down, remove, jump]) control.setAttribute('aria-label', control.textContent + '：' + row.title);
    controls.append(first, second, up, down, n, jump, remove);
    details.append(controls);
    li.append(details);
    return li;
  });
  $('draft-rows').start = offset + 1;
  $('draft-rows').replaceChildren(...rows);
  if (focusedItemId) {
    const target = rows.find(row => row.dataset.queueId === focusedItemId);
    const next = target && Array.from(target.querySelectorAll('button,input')).find(control =>
      control.getAttribute('aria-label') === focusedAction && control.dataset.boundary !== 'true');
    if (next) {
      next.disabled = false;
      next.focus({preventScroll:true});
    } else $('draft').querySelector('[data-close]').focus({preventScroll:true});
  }
  setInteractionState();
}
async function showDraft() {
  if (!loaded || saving) return;
  notice('review');
  if (!await refreshDraft()) {
    notice('error', '未能加载转存清单，请重试。');
    return;
  }
  openDialog('draft');
  setInteractionState();
}
async function refreshDraft() {
  const token = ++draftPageRequest;
  try {
    const value = await api('/api/draft?entry_page=' + transferListPage);
    if (token !== draftPageRequest || value.revision < draft.revision) return false;
    draftDetails = value;
    transferListPage = value.entry_page;
    selection.adopt(value);
    paintDraft();
    return true;
  } catch {
    if ($('draft').open) notice('review', '未能加载清单，请重试。');
    return false;
  }
}
function projectActions(row) {
  const entries = row.project_entries || [];
  const links = [];
  for (const s of entries) {
    const href = original(s.href, s.origin);
    if (!href) continue;
    const label = entries.length === 1 ? '打开原项目' : `原项目 · ${s.site_label}`;
    links.push(outsideLink(label, href, 'article-share'));
  }
  return links;
}
function paintReaderPick() {
  const holder = $('reader-selection');
  const refocus = holder.contains(document.activeElement);
  const control = readerRow ? selectionControl(readerRow, 'reader') : null;
  holder.replaceChildren(...(control ? [control] : []));
  if (refocus) control?.querySelector('input')?.focus({preventScroll:true});
}
function articleImage(row) {
  const figure = el('figure', null);
  const img = image(row, () => figure.replaceWith(el('p', '图片暂无法打开。', 'muted')));
  if (!img) return null;
  const link = outsideLink(null, img.src);
  link.setAttribute('aria-label', '查看原图');
  link.append(img);
  figure.append(link);
  if (row.alt) figure.append(el('figcaption', row.alt, 'muted'));
  return figure;
}
function baiduActions(row) {
  const urls = (row.baidu_share_links || []).filter(url => /^https:\/\/pan\.baidu\.com\/s\/[A-Za-z0-9_-]{1,129}(?:\?pwd=[A-Za-z0-9]{4})?$/.test(url));
  return urls.map((url, i) => outsideLink(urls.length > 1 ? `百度网盘 ${i + 1}` : '打开百度网盘', url, 'article-share'));
}
function relatedSection(kind, label, entries) {
  const choices = Array.isArray(entries) ? entries.filter(entry => entry && resourceIdPattern.test(entry.id) &&
    articleKeyPattern.test(entry.article_key)) : [];
  if (!choices.length) return null;
  const details = el('details', null, 'article-relations');
  details.dataset.relation = kind;
  details.append(el('summary', label + '（' + fmt(choices.length) + '）'));
  const list = el('ul', null);
  for (const entry of choices) {
    const li = el('li', null);
    const link = button(entry.title || '查看项目', 'relation-title');
    link.addEventListener('click', () => void openReader(entry));
    const caption = [entry.version_label, entry.site_label, shortDate(entry.published_at)].filter(Boolean).join(' · ');
    li.append(link);
    if (caption) li.append(el('span', caption, 'muted'));
    list.append(li);
  }
  details.append(list);
  return details;
}
const decisionRelations = {
  same_edition_source:'同版来源', different_version:'不同版本', newer:'较新版本', older:'较早版本',
  related:'同类玩法', contains:'内容范围相关', topic_candidate:'同类玩法线索',
};
function decisionEvidence(version) {
  const join = values => Array.isArray(values) ? values.join('、') : '';
  const coverage = version.analysis_scope || ({
    completed:'已有同版教材', completed_received_scope:'已有实收教材',
    same_source_reuse:'已有同源教材', local_result_available:'已有本地分析',
  })[version.analysis_state];
  return factTable([
    ['简介', version.introduction],
    ['简介署名', version.author],
    ['学习方向', version.objective],
    ['简介所列方法', join(version.methods)],
    ['简介所提工具', join(version.tools)],
    ['简介所述条件', join(version.conditions)],
    ['已有教材', coverage],
  ]);
}
function decisionDifference(record, focus = '当前版', peer = '对照版') {
  const difference = record.comparison || {};
  const describe = part => part?.count ? [`${part.count} 项`, ...part.examples].join(' · ') : '';
  return factTable([
    ['共同列出的课程条目', difference.shared_outline_count],
    [focus + '目录独列', describe(difference.focus_only)],
    [peer + '目录独列', describe(difference.peer_only)],
    [focus + '简介独提工具', difference.focus_tools?.join('、')],
    [peer + '简介独提工具', difference.peer_tools?.join('、')],
    [focus + '简介独列条件', difference.focus_conditions?.join('、')],
    [peer + '简介独列条件', difference.peer_conditions?.join('、')],
    [focus + '简介独列变现方式', difference.focus_monetization?.join('；')],
    [peer + '简介独列变现方式', difference.peer_monetization?.join('；')],
    [focus + '简介独列费用与投入', difference.focus_costs?.join('；')],
    [peer + '简介独列费用与投入', difference.peer_costs?.join('；')],
    [focus + '简介独列配套线索', difference.focus_materials?.join('；')],
    [peer + '简介独列配套线索', difference.peer_materials?.join('；')],
  ]);
}
function decisionSection(decision) {
  if (!decision?.focus) return null;
  const panel = el('details', null, 'article-relations');
  panel.dataset.relation = 'decision';
  panel.append(el('summary', '选择分析版本'));
  if (decision.advice) panel.append(el('p', decision.advice));
  panel.append(decisionEvidence(decision.focus));
  const choice = decision.comparisons || [];
  if (choice.length) {
    const list = el('ul');
    for (const record of choice) {
      const version = record.version;
      if (!version || !resourceIdPattern.test(version.id) || !articleKeyPattern.test(version.article_key)) continue;
      const entry = el('li');
      const route = button(version.title, 'relation-title');
      route.addEventListener('click', () => void openReader(version));
      entry.append(route);
      const attribution = record.author_relation === 'different' ? '公开介绍署名不同'
        : record.author_relation === 'same' ? '公开介绍署名相同' : '';
      const label = [decisionRelations[record.relation], version.version_label, version.site_label,
        shortDate(version.published_at), attribution].filter(Boolean).join(' · ');
      if (label) entry.append(el('span', label, 'muted'));
      entry.append(decisionEvidence(version));
      if (record.comparison) entry.append(decisionDifference(record));
      const openBook = textbook({obsidian_href:version.textbook_href});
      if (openBook) entry.append(openBook);
      list.append(entry);
    }
    if (list.children.length) panel.append(list);
  }
  return panel;
}
function playbookRoute(entry) {
  const title = button(entry.title, 'relation-title');
  title.addEventListener('click', () => {
    closeDialog($('playbooks'));
    void openReader(entry, $('playbooks-open'));
  });
  return title;
}
function playbookProfile(entry) {
  const row = el('li');
  const release = shortDate(entry.published_at);
  const caption = [entry.site_label, release ? '原站发布 ' + release : '', entry.version_label];
  const join = values => Array.isArray(values) ? values.join('；') : '';
  row.append(playbookRoute(entry), el('small', caption.filter(Boolean).join(' · '), 'muted'), decisionEvidence(entry));
  row.append(factTable([
    ['适用平台', join(entry.platforms)], ['原文所列版次时间', join(entry.version_dates)],
    ['简介所述变现方式', join(entry.monetization)], ['简介所述费用与投入', join(entry.costs)],
    ['简介所列独有配套线索', join(entry.materials)], ['待核实', join(entry.pending)],
  ]));
  if (entry.formal) {
    if (entry.formal.received) row.append(el('p', '同版实收资料已验收。', 'muted'));
    const book = textbook({obsidian_href:entry.formal.href});
    if (book) row.append(book);
  }
  if (entry.lineage?.length) {
    const history = el('details', null, 'article-relations');
    history.append(el('summary', '同系列版本与同课来源'));
    for (const record of entry.lineage) {
      const version = record.version;
      history.append(playbookRoute(version));
      const label = [decisionRelations[record.relation], version.version_label, shortDate(version.published_at)];
      history.append(el('small', label.filter(Boolean).join(' · '), 'muted'));
      if (record.comparison) history.append(decisionDifference(record, '当前版', record.relation === 'older' ? '旧版' : '对照版'));
      const book = textbook({obsidian_href:version.textbook_href});
      if (book) history.append(book);
    }
    row.append(history);
  }
  if (entry.recommendation) {
    row.append(factTable([['分析建议', entry.recommendation.action], ['依据', entry.recommendation.reason]]));
  }
  for (const quote of entry.quotes || []) row.append(el('blockquote', quote));
  return row;
}
function playbookGroup(group) {
  const section = el(group.theme ? 'details' : 'section', null, 'article-relations');
  if (group.theme) section.append(el('summary', group.theme));
  const entries = new Map(group.entries.map(entry => [entry.article_key, entry]));
  const list = el('ul', null, 'playbook-pair');
  list.setAttribute('aria-label', '专题项目，按原站发布日期排列');
  for (const entry of group.entries) list.append(playbookProfile(entry));
  section.append(list);
  if (group.comparisons?.length) {
    const details = el('details', null, 'article-relations');
    details.append(el('summary', '玩法变化与原文依据'));
    for (const comparison of group.comparisons) {
      const pair = el('div', null, 'playbook-pair');
      // The titles on each side identify the introduction that each quote describes.
      for (const side of ['left', 'right']) {
        const source = entries.get(comparison[side]);
        if (!source) continue;
        pair.append(playbookRoute(source));
        for (const quote of comparison.evidence?.[side] || []) pair.append(el('blockquote', quote));
      }
      if (comparison.reason) pair.append(el('p', comparison.reason));
      pair.append(decisionDifference(comparison, '前列项目', '后列项目'));
      details.append(pair);
    }
    section.append(details);
  }
  return section;
}
async function playbooksPanel() {
  const content = $('playbooks-body');
  content.replaceChildren(el('p', '加载中…', 'muted'));
  openDialog('playbooks');
  try {
    const {items} = await api('/api/playbooks');
    const groups = el('div');
    for (const group of items) groups.append(playbookGroup(Array.isArray(group) ? {entries:group} : group));
    content.replaceChildren(items.length ? groups : el('p', '暂无可比较的玩法。', 'muted'));
  } catch {
    const retry = button('重新加载');
    retry.addEventListener('click', () => void playbooksPanel());
    content.replaceChildren(el('p', '未能加载玩法专题。', 'notice'), retry);
  }
}
$('playbooks-open')?.addEventListener('click', () => void playbooksPanel());

function renderReader(row) {
  $('reader-title').textContent = row.title;
  const platforms = catalogFacets.labels(row, filter.platform, Infinity);
  notice('reader-meta', [row.site_label, shortDate(row.published_at), row.category_label,
    platforms.join('、'), row.version_label].filter(Boolean).join(' · '));
  notice('reader-state', resourceState(row));
  const article = $('reader-body');
  article.replaceChildren();
  const analysis = priorityAction(row);
  const textbookHref = row.textbook_href;
  const textbookLink = row.action_pending ? null : textbook({obsidian_href:textbookHref});
  const shareLinks = baiduActions(row);
  const projectLinks = projectActions(row);
  if (analysis || textbookLink || shareLinks.length || projectLinks.length) {
    const actions = el('div', null, 'article-actions');
    if (textbookLink) {
      textbookLink.classList.add('article-share');
      actions.append(textbookLink);
    }
    actions.append(...shareLinks, ...projectLinks);
    if (analysis) actions.append(analysis);
    article.append(actions);
  }
  const followups = row.related_followups || [];
  const sources = relatedSection('sources',
    followups.some(entry => entry.relation === 'same_edition_source') ? '可核对的其他来源' : '其他来源',
    row.other_sources);
  const versions = relatedSection('versions',
    followups.some(entry => entry.relation === 'different_edition') ? '可对照的其他版本' : '其他版本',
    row.other_versions);
  if (sources) article.append(sources);
  if (versions) article.append(versions);
  if (row.relationship_pending === true) article.append(el('p', '版本关系待核实', 'muted'));
  const decision = decisionSection(row.analysis_decision);
  if (decision) article.append(decision);
  const completion = courseCompletionRows(row);
  if (completion.length) {
    const details = el('details', null, 'article-relations');
    details.append(el('summary', '分析与资料'), factTable(completion));
    article.append(details);
  }
  const blocks = row.blocks || [];
  if (row.excerpt && !blocks.some(block => block.kind === 'text' && (block.text === row.excerpt || block.text.startsWith(row.excerpt)))) {
    article.append(el('p', row.excerpt, 'article-excerpt'));
  }
  const cover = imageURL(row.cover?.url);
  if (cover && !blocks.some(block => block.kind === 'image' && imageURL(block.url) === cover)) {
    const figure = articleImage(row.cover);
    if (figure) article.append(figure);
  }
  for (const block of blocks) {
    if (block.kind === 'text') article.append(el('p', block.text));
    if (block.kind !== 'image') continue;
    const figure = articleImage(block);
    if (figure) article.append(figure);
  }
}
function updateReaderPosition(key) {
  const index = page?.items.findIndex(row => catalogFacets.displayId(row) === key) ?? -1;
  $('reader-prev').disabled = index <= 0;
  $('reader-next').disabled = index < 0 || index >= page.items.length - 1;
}
async function openReader(item, opener, section = '') {
  const token = ++readerSequence;
  const requested = typeof item === 'string' ? {id:item} : item;
  const key = catalogFacets.displayId(requested);
  const summary = page?.items.find(row => catalogFacets.displayId(row) === key);
  if (opener) readerOpener = {element:opener, id:key};
  readerRow = null;
  $('reader-title').textContent = summary?.title || '资料详情';
  notice('reader-meta');
  notice('reader-state');
  $('reader-body').replaceChildren(el('p', '加载中…', 'muted'));
  paintReaderPick();
  updateReaderPosition(key);
  openDialog('reader');
  $('reader-scroll').scrollTop = 0;
  try {
    const params = requested.article_key ? '?' + new URLSearchParams({article:requested.article_key}) : '';
    const row = await api('/api/resource/' + encodeURIComponent(requested.id) + params);
    if (token !== readerSequence || !$('reader').open) return;
    readerRow = row;
    paintReaderPick();
    renderReader(row);
    updateReaderPosition(catalogFacets.displayId(row));
    if (['sources','versions'].includes(section)) {
      const details = $('reader-body').querySelector('[data-relation="' + section + '"]');
      if (details) {
        details.open = true;
        details.querySelector('summary').focus();
      }
    }
  } catch {
    if (token !== readerSequence) return;
    const retry = button('重新加载');
    retry.addEventListener('click', () => void openReader(requested, undefined, section));
    $('reader-body').replaceChildren(el('p', '未能加载资料详情。', 'notice'), retry);
  }
}
function neighbor(offset) {
  if (!readerRow) return;
  const index = page?.items.findIndex(row => catalogFacets.displayId(row) === catalogFacets.displayId(readerRow)) ?? -1;
  if (index < 0) return;
  const next = page.items[index + offset];
  if (next) void openReader(next);
}
async function showOverview() {
  const target = $('overview-body');
  target.replaceChildren(el('p', '加载中…', 'muted'));
  openDialog('overview');
  try {
    const info = await api('/api/meta');
    if (!$('overview').open) return;
    const heading = el('h3', '收录来源');
    const sites = el('ul', null, 'source-list');
    for (const site of info.sites) sites.append(el('li', site.label));
    const stats = [
      ['已收录文章', info.totals.articles], ['可在此阅读的图文', info.totals.details], ['已收录图片', info.totals.images],
      ['已收录百度网盘链接的文章', info.totals.baidu_linked_articles]
    ];
    const list = el('dl', null, 'fact-list');
    for (const [label, count] of stats) {
      const row = el('div', null, 'fact-row');
      row.append(el('dt', label), el('dd', fmt(count)));
      list.append(row);
    }
    target.replaceChildren(heading, sites, list);
  } catch {
    target.replaceChildren(el('p', '未能加载资料来源，请重试。', 'notice'));
  }
}
function resetFilters() {
  if (!loaded) return;
  filter = {...DEFAULT, view:filter.view};
  $('to').setCustomValidity('');
  $('more-filters').open = false;
  syncControls();
  void query({top:true, locationMode:'pushState'});
}

for (const close of document.querySelectorAll('[data-close]')) {
  close.addEventListener('click', () => closeDialog(close.closest('dialog')));
}
document.addEventListener('click', event => {
  for (const panel of document.querySelectorAll('.popover[open]')) {
    if (!panel.contains(event.target)) panel.open = false;
  }
});
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  for (const panel of document.querySelectorAll('.popover[open]')) {
    panel.open = false;
    if (panel.contains(document.activeElement)) panel.querySelector('summary').focus();
  }
});
$('reader').addEventListener('close', () => {
  readerSequence++;
  readerRow = null;
  const opener = readerOpener;
  readerOpener = null;
  if (opener?.element.isConnected) return;
  const card = Array.from($('cards').children).find(item => item.dataset.version === opener?.id);
  (card?.querySelector('.title') || $('result-title')).focus({preventScroll:true});
});
$('reader').addEventListener('keydown', event => {
  if (!event.altKey || !['ArrowLeft','ArrowRight'].includes(event.key)) return;
  event.preventDefault();
  neighbor(event.key === 'ArrowLeft' ? -1 : 1);
});
$('reader-prev').addEventListener('click', () => neighbor(-1));
$('reader-next').addEventListener('click', () => neighbor(1));
$('queue').addEventListener('click', () => void showDraft());
$('progress').addEventListener('click', () => void showOverview());
$('filters').addEventListener('submit', event => {
  event.preventDefault();
  applyFilter({});
});
$('apply-filters').addEventListener('click', () => {
  const from = $('from').value, to = $('to').value;
  if (from && to && from > to) {
    $('to').setCustomValidity('截止日不能早于起始日。');
    $('to').reportValidity();
    return;
  }
  $('to').setCustomValidity('');
  $('more-filters').open = false;
  applyFilter({status:$('status').value, site:$('site').value, from, to});
});
for (const date of ['from','to']) $(date).addEventListener('input', () => $('to').setCustomValidity(''));
$('reset').addEventListener('click', resetFilters);
$('empty-reset').addEventListener('click', resetFilters);
$('available').addEventListener('click', () => applyFilter({baidu:filter.baidu === '1' ? '0' : '1'}));
$('selected').addEventListener('click', () => applyFilter({selected:filter.selected === '1' ? '0' : '1'}));
$('order').addEventListener('change', () => applyFilter({order:$('order').value}));
$('versions').addEventListener('change', () => applyFilter({versions:$('versions').value}));
for (const view of ['grid','list']) $(view).addEventListener('click', () => {
  if (!loaded || filter.view === view) return;
  filter = {...filter, view};
  syncControls({search:false});
  void query();
});
$('first').addEventListener('click', () => goPage(1));
$('prev').addEventListener('click', () => { if (page) goPage(page.page - 1); });
$('next').addEventListener('click', () => { if (page) goPage(page.page + 1); });
$('last').addEventListener('click', () => { if (page) goPage(page.pages); });
$('page-jump').addEventListener('submit', event => {
  event.preventDefault();
  const jump = $('jump-number');
  if (jump.reportValidity()) goPage(jump.valueAsNumber);
});
for (const b of document.querySelectorAll('[data-scope]')) {
  b.addEventListener('click', () => {
    if (!page || !renderedFilter || saving || querying || b.dataset.overLimit === 'true') return;
    const count = Number(b.dataset.count);
    const projects = Number(b.dataset.projects);
    const pending = Number(b.dataset.pending);
    confirmedAction = {action:'bulk', revision:draft.revision, scope:b.dataset.scope, expected_count:count,
      filter:renderedPageFilter, snapshot_id:page.snapshot_id};
    $('bulk').open = false;
    const text = bulkLabels[b.dataset.scope] + '。' + fmt(projects) + ' 个项目中可选 ' + fmt(count) + ' 项资料。';
    notice('confirm-text', text + (pending ? '另有 ' + fmt(pending) + ' 项待核实。' : ''));
    $('apply-bulk').textContent = '选中资料';
    openDialog('confirm');
  });
}
function confirmClearSelection() {
  if (saving || !draft.ids.length) return;
  confirmedAction = {action:'clear', revision:draft.revision};
  notice('confirm-text', '清空转存清单中的 ' + fmt(draft.ids.length) + ' 项选择？');
  $('apply-bulk').textContent = '清空选择';
  openDialog('confirm');
}
$('clear-all').addEventListener('click', confirmClearSelection);
$('clear-catalog-selection').addEventListener('click', confirmClearSelection);
$('clear-page-selection').addEventListener('click', async () => {
  const ids = page?.items.filter(isPicked).map(row => row.id);
  if (!ids?.length) return;
  await mutate('remove_many', {ids});
  if ($('clear-page-selection').hidden) $('result-title').focus({preventScroll:true});
});
$('apply-bulk').addEventListener('click', () => {
  const command = confirmedAction;
  confirmedAction = null;
  closeDialog($('confirm'));
  if (command) {
    const {action, revision, ...values} = command;
    void mutate(action, values, revision);
  }
});
$('confirm').addEventListener('close', () => { if (!$('confirm').open) confirmedAction = null; });
$('freeze').addEventListener('click', async () => {
  if (!loaded || saving) return;
  saving = true;
  setInteractionState();
  try {
    const review = await api('/api/review', {revision:draft.revision, order_mode:$('mode').value});
    notice('review', '已保存 ' + fmt(review.items.length) + ' 项的顺序。');
  } catch (error) {
    notice('review', selectionFailure(error));
  } finally {
    saving = false;
    setInteractionState();
  }
});
window.addEventListener('pagehide', remember);
window.addEventListener('resize', scheduleCatalogFit);
document.addEventListener('close', scheduleCatalogFit, true);
window.addEventListener('popstate', () => {
  if (!loaded) return;
  filter = {...(readLocationFilters() || DEFAULT), view:filter.view};
  syncControls();
  void query({top:true});
});
selection.subscribe(value => {
  if (!value) return;
  const changed = draft.revision !== value.revision;
  draft = value;
  if (!loaded || !page || !changed) return;
  renderSelection();
  setInteractionState();
  if (saving || querying) return;
  if ($('draft').open && draftDetails?.revision !== draft.revision) void refreshDraft();
  if (filter.selected === '1') void query();
});

async function openLinkedView() {
  const link = new URLSearchParams(location.search);
  const resource = link.get('resource');
  if (resource && resourceIdPattern.test(resource)) {
    await openReader({id:resource, article_key:link.get('article')}, undefined, link.get('details'));
    return;
  }
  switch (link.get('panel')) {
    case 'transfer':
      await showDraft();
      break;
    case 'analysis':
      $('analysis-open').click();
      break;
    case 'progress':
      await localPanel();
      break;
    case 'cleanup': {
      await localPanel();
      const heading = $('local-body').querySelector('.cleanup-group')?.closest('section')?.querySelector('h3');
      if (heading && $('local-status').open) {
        heading.tabIndex = -1;
        heading.focus({preventScroll: true});
        heading.scrollIntoView({block: 'start'});
      }
      break;
    }
    case 'confirmation':
      await preconfirmPanel();
      break;
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  try {
    [meta] = await Promise.all([api('/api/meta'), selection.load()]);
    if ($('playbooks-open')) $('playbooks-open').hidden = meta.playbooks !== true;
    setChoices('status', meta.statuses.filter(row => resourceStates[row.id]), '全部状态', row => resourceStates[row.id]);
    setChoices('site', meta.sites, '全部来源');
    const {pageLimit, ...savedView} = prefs();
    const explicit = readLocationFilters();
    filter = catalogFacets.normalize({...DEFAULT, ...draft.prefs, ...savedView,
      ...(explicit ? {...explicit, view:savedView.view || draft.prefs?.view || DEFAULT.view} : {})}, meta);
    delete filter.limit;
    loaded = true;
    syncControls();
    const previousLimit = Number.isSafeInteger(pageLimit) && pageLimit >= baseCatalogLimit ? pageLimit : baseCatalogLimit;
    const limit = fittedCatalogLimit();
    if (!explicit && limit !== previousLimit) filter.page = Math.floor((filter.page - 1) * previousLimit / limit) + 1;
    await query({limit, restore:limit === previousLimit ? filter.scroll : null});
    await openLinkedView();
  } catch {
    $('cards').setAttribute('aria-busy', 'false');
    notice('error', '未能加载资源库，请刷新重试。');
  }
}, {once:true});
