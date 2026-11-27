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
const DEFAULT = {q:'', category:'', status:'', site:'', from:'', to:'', order:'newest', view:'grid', selected:'0', baidu:'0', page:1, scroll:0};
const PREFS_KEY = 'wa-v4-view:existing-library';
const resourceStates = {
  observed:'资料范围待核对', verified:'曾转存', recycled:'已回收',
  needs_reconcile:'转存待核对', restricted:'原文访问受限', unknown:'转存方式待核实'
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
let draft = {revision:0, ids:[], entries:[]};
let querySequence = 0;
let readerSequence = 0;
let readerRow = null;
let readerOpener = null;
let loaded = false;
let querying = false;
let saving = false;
let confirmedAction = null;
let transferListPage = 1;

async function api(path, body) {
  const options = {cache:'no-store', credentials:'omit'};
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
  return resourceStates[row.status] || '';
}
function canRequestTransfer(row) {
  return row.requestable ?? row.selectable;
}
function priorityAction(row) {
  return typeof analysisArticleAction === 'function' ? analysisArticleAction(row) : null;
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
function setPressed(id, pressed) {
  $(id).setAttribute('aria-pressed', String(pressed));
}
function syncControls({search = true} = {}) {
  if (search) $('q').value = filter.q;
  for (const id of ['status','site','from','to','order']) $(id).value = filter[id];
  setPressed('grid', filter.view === 'grid');
  setPressed('list', filter.view === 'list');
  setPressed('available', filter.baidu === '1');
  setPressed('selected', filter.selected === '1');
  for (const control of $('topics').querySelectorAll('button')) {
    control.setAttribute('aria-pressed', String(filter.category === control.dataset.topic));
  }
  const extraCount = ['site','from','to','status'].filter(k => filter[k]).length;
  notice('filter-count', extraCount ? String(extraCount) : '');
  const topic = meta.categories.find(item => item.id === filter.category);
  $('result-title').textContent = topic?.label || '全部资料';
}
function applyFilter(changes) {
  if (!loaded) return;
  filter = {...filter, q:$('q').value.trim(), ...changes, page:1};
  syncControls();
  void query({top:true});
}
function paintTopics() {
  const all = {id:'', label:'全部', count:meta.totals.articles};
  const active = document.activeElement;
  const focussedTopic = $('topics').contains(active) ? active.dataset.topic : undefined;
  const controls = [all, ...meta.categories].map(topic => {
    const b = button('', 'topic');
    b.dataset.topic = topic.id;
    b.append(el('span', topic.label), el('small', fmt(topic.count)));
    b.setAttribute('aria-pressed', String(filter.category === topic.id));
    b.addEventListener('click', () => {
      if (filter.category !== topic.id || $('q').value.trim() !== filter.q) applyFilter({category:topic.id});
    });
    return b;
  });
  $('topics').replaceChildren(...controls);
  if (focussedTopic !== undefined) controls.find(b => b.dataset.topic === focussedTopic)?.focus({preventScroll:true});
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
function sameDraft(value) {
  if (value && value.revision >= draft.revision) draft = {...draft, ...value};
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
    const entries = draft.entries || [];
    $('freeze').disabled = saving || !entries.length || entries.some(row => !canRequestTransfer(row));
    for (const control of $('draft-rows').querySelectorAll('[data-queue-action]')) {
      control.disabled = saving || control.dataset.boundary === 'true';
    }
    $('clear-all').disabled = saving || !entries.length;
  }
}
function selectionControl(row, location) {
  const picked = draft.ids.includes(row.id);
  if (!canRequestTransfer(row) && !picked) return null;
  const label = el('label', null, 'pickline');
  const box = el('input', null, 'pick');
  box.type = 'checkbox';
  box.checked = picked;
  box.dataset.resourcePick = row.id;
  box.setAttribute('aria-label', '选择 ' + row.title);
  box.disabled = saving || querying;
  box.addEventListener('change', () => {
    const next = box.checked;
    const hadFocus = document.activeElement === box;
    box.checked = picked;
    void mutate('toggle', {id:row.id, selected:next}).then(() => {
      if (!hadFocus || ![document.body, box].includes(document.activeElement)) return;
      if (location === 'reader' && !$('reader').open) return;
      const region = $(location === 'reader' ? 'reader-selection' : 'cards');
      const replacement = Array.from(region.querySelectorAll('[data-resource-pick]')).find(input => input.dataset.resourcePick === row.id);
      (replacement || (location === 'reader' ? $('reader').querySelector('[data-close]') : $('result-title'))).focus({preventScroll:true});
    });
  });
  label.append(box, el('span', picked ? '已选' : '选择', location === 'card' ? 'card-choice-label' : ''));
  return label;
}
function cardExcerpt(row) {
  const excerpt = row.excerpt?.trim();
  if (!excerpt) return '';
  const title = row.title?.trim();
  const preview = title && excerpt.startsWith(title)
    ? excerpt.slice(title.length).replace(/^[\s：:·，、—-]+/, '')
    : excerpt;
  return /^(?:\[\s*(?:\.{3}|…)\s*\]|…)\s*$/u.test(preview) ? '' : preview;
}
function card(row) {
  const article = el('article', null, 'card');
  article.dataset.version = row.id;
  if (draft.ids.includes(row.id)) article.dataset.picked = 'true';
  const main = el('div', null, 'card-main');
  const preview = button('', 'photo');
  const picture = image(row.cover, () => preview.remove());
  if (picture) {
    preview.setAttribute('aria-label', '查看封面与详情：' + row.title);
    preview.append(picture);
    preview.addEventListener('click', () => void openReader(row.id, preview));
    main.append(preview);
  }
  const copy = el('div', null, 'card-copy');
  const header = el('h3', null);
  const title = button(row.title, 'title');
  title.addEventListener('click', () => void openReader(row.id, title));
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
  const time = shortDate(row.published_at);
  if (time) {
    const date = el('time', time);
    date.dateTime = time;
    metadata.append(date);
  }
  if (row.site_label) metadata.append(el('span', row.site_label, 'card-source'));
  foot.append(metadata);
  const actions = el('div', null, 'card-tail');
  const state = resourceState(row);
  if (state) actions.append(el('span', state, 'card-state'));
  const control = selectionControl(row, 'card');
  if (control) actions.append(control);
  const analysis = priorityAction(row);
  if (analysis) actions.append(analysis);
  foot.append(actions);
  article.append(main, foot);
  return article;
}
function cardsFrom(result) {
  const focused = document.activeElement;
  const active = focused?.closest('article[data-version]');
  const restore = active && $('cards').contains(active);
  const cardType = focused?.matches('[data-resource-pick]') ? '.pick' :
    focused?.matches('.photo') ? '.photo' : focused?.matches('.card-topic') ? '.card-topic' : '.title';
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
function render() {
  cardsFrom(page);
  $('empty').hidden = page.total > 0;
  $('result-count').textContent = fmt(page.total) + ' 项';
  $('chosen').textContent = fmt(draft.ids.length);
  notice('selection', draft.ids.length ? '已选 ' + fmt(draft.ids.length) + ' 项' +
    (page.hidden_selected ? '，' + fmt(page.hidden_selected) + ' 项不在本页' : '') : '');
  pagination();
  const counts = {page:page.page_selectable, filtered:page.eligible, site_all:page.index_selectable};
  const maxItems = Number.isSafeInteger(page.max_draft_items) && page.max_draft_items > 0 ? page.max_draft_items : 5000;
  notice('bulk-limit-note', Object.values(counts).some(count => count > maxItems) ?
    '每张清单最多 ' + fmt(maxItems) + ' 项，请缩小选择范围。' : '');
  for (const b of document.querySelectorAll('[data-scope]')) {
    const scope = b.dataset.scope;
    b.dataset.count = String(counts[scope]);
    b.dataset.overLimit = String(counts[scope] > maxItems);
    b.replaceChildren(el('span', bulkLabels[scope]), el('small', fmt(counts[scope])));
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
async function query({top = false, restore = null, focusResult = false, limit = page?.limit ?? fittedCatalogLimit(), anchor = null} = {}) {
  const token = ++querySequence;
  const request = {...filter};
  const pageFilter = {...request, view:'list'};
  if (limit !== baseCatalogLimit) pageFilter.limit = limit;
  querying = true;
  notice('error');
  setInteractionState();
  try {
    const result = await api('/api/resources?' + new URLSearchParams(pageFilter));
    if (token !== querySequence) return;
    if (meta.snapshot_id !== result.snapshot_id) {
      const next = await api('/api/meta');
      if (token !== querySequence) return;
      if (next.snapshot_id === result.snapshot_id) {
        meta = next;
        paintTopics();
      }
    }
    page = result;
    renderedFilter = {...request, page:result.page};
    renderedPageFilter = {...pageFilter, page:result.page};
    filter.page = result.page;
    sameDraft(result.draft);
    render();
    if (restore !== null) window.scrollTo(0, restore);
    if (anchor) {
      const keep = Array.from($('cards').children).find(card => card.dataset.version === anchor.id);
      if (keep) window.scrollBy(0, keep.getBoundingClientRect().top - anchor.top);
    }
    if (top) window.scrollTo(0, 0);
    if (focusResult) $('result-title').focus({preventScroll:true});
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
  void query({top:true, focusResult:true});
}
async function mutate(action, values = {}) {
  if (!loaded || !page || saving || querying) return;
  const reviewWasOpen = $('draft').open;
  saving = true;
  notice('error');
  if (reviewWasOpen) notice('review');
  setInteractionState();
  try {
    sameDraft(await api('/api/draft', {revision:draft.revision, action, snapshot_id:page.snapshot_id, ...values}));
    if (reviewWasOpen && $('draft').open) paintDraft();
    if ($('reader').open) paintReaderPick();
    await query();
  } catch (error) {
    sameDraft(error.current);
    if (reviewWasOpen && $('draft').open) paintDraft();
    if ($('reader').open) paintReaderPick();
    await query();
    notice(reviewWasOpen && $('draft').open ? 'review' : 'error', selectionFailure(error));
  } finally {
    saving = false;
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
function changeTransferPage(offset) {
  if (saving) return;
  transferListPage += offset;
  paintDraft();
  $('draft').querySelector('.drawer-scroll').scrollTop = 0;
  $('draft-pages').querySelector('button:not(:disabled)')?.focus({preventScroll:true});
}
function paintDraft() {
  const items = draft.entries || [];
  notice('draft-note', items.length ? fmt(items.length) + ' 项，按下方顺序排列' : '转存清单为空。');
  $('draft-actions').hidden = !items.length;
  $('clear-all').hidden = !items.length;
  const focusedItem = document.activeElement?.closest('.draftline');
  const focusedItemId = focusedItem?.dataset.queueId;
  const focusedAction = document.activeElement?.getAttribute('aria-label');
  paintTransferPages(items.length);
  const offset = (transferListPage - 1) * transferPageSize;
  const rows = items.slice(offset,offset+transferPageSize).map((row, pageIndex) => {
    const index = offset + pageIndex;
    const li = el('li', null, 'draftline');
    li.dataset.queueId = row.id;
    const details = el('div', null, 'draft-copy');
    details.append(el('strong', row.title));
    const info = [row.category_label, row.site_label, resourceState(row), showSize(row.range_total_bytes) || '资料大小待核实',
      row.stale ? '资料已更新，请重新选择' : ''].filter(Boolean);
    if (info.length) details.append(el('p', info.join(' · '), 'muted'));
    const controls = el('div', null, 'draft-controls');
    const first = queueButton('移到首位', () => moveTransfer(row.id,1), index === 0);
    const second = queueButton('移到第二位', () => moveTransfer(row.id,2), index === 1 || items.length < 2);
    const up = queueButton('上移', () => moveTransfer(row.id,index), index === 0);
    const down = queueButton('下移', () => moveTransfer(row.id,index+2), index === items.length - 1);
    const remove = queueButton('移出', () => void mutate('remove', {id:row.id}));
    const n = el('input');
    n.type = 'number';
    n.min = '1';
    n.max = String(items.length);
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
  try {
    sameDraft(await api('/api/draft'));
    notice('review');
    openDialog('draft');
    paintDraft();
  } catch { notice('error', '未能加载转存清单，请重试。'); }
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
function renderReader(row) {
  $('reader-title').textContent = row.title;
  notice('reader-meta', [row.site_label, shortDate(row.published_at), row.category_label, resourceState(row)].filter(Boolean).join(' · '));
  const article = $('reader-body');
  article.replaceChildren();
  const analysis = priorityAction(row);
  const shareLinks = baiduActions(row);
  const projectLinks = projectActions(row);
  if (analysis || shareLinks.length || projectLinks.length) {
    const actions = el('div', null, 'article-actions');
    actions.append(...shareLinks, ...projectLinks);
    if (analysis) actions.append(analysis);
    article.append(actions);
  }
  if (row.excerpt && !row.blocks.some(block => block.kind === 'text' && (block.text === row.excerpt || block.text.startsWith(row.excerpt)))) {
    article.append(el('p', row.excerpt, 'article-excerpt'));
  }
  const cover = imageURL(row.cover?.url);
  if (cover && !row.blocks.some(block => block.kind === 'image' && imageURL(block.url) === cover)) {
    const figure = articleImage(row.cover);
    if (figure) article.append(figure);
  }
  for (const block of row.blocks) {
    if (block.kind === 'text') article.append(el('p', block.text));
    if (block.kind !== 'image') continue;
    const figure = articleImage(block);
    if (figure) article.append(figure);
  }
  if (row.roots.length) {
    const section = el('section', null, 'article-section');
    const list = el('ul', null);
    for (const root of row.roots) {
      const details = root.is_dir ? '文件夹' : showSize(root.bytes);
      list.append(el('li', [root.name, details].filter(Boolean).join(' · ')));
    }
    section.append(el('h3', '曾转存的资料'), list);
    article.append(section);
  }
  if (typeof displayProductionDetail === 'function') displayProductionDetail(article, row);
}
function updateReaderPosition(id) {
  const index = page?.items.findIndex(row => row.id === id) ?? -1;
  $('reader-prev').disabled = index <= 0;
  $('reader-next').disabled = index < 0 || index >= page.items.length - 1;
}
async function openReader(id, opener) {
  const token = ++readerSequence;
  const summary = page?.items.find(row => row.id === id);
  if (opener) readerOpener = {element:opener, id};
  readerRow = summary || {id};
  $('reader-title').textContent = summary?.title || '资料详情';
  notice('reader-meta');
  $('reader-body').replaceChildren(el('p', '加载中…', 'muted'));
  paintReaderPick();
  updateReaderPosition(id);
  openDialog('reader');
  $('reader-scroll').scrollTop = 0;
  try {
    const row = await api('/api/resource/' + encodeURIComponent(id));
    if (token !== readerSequence || !$('reader').open) return;
    readerRow = row;
    paintReaderPick();
    renderReader(row);
    updateReaderPosition(id);
  } catch {
    if (token !== readerSequence) return;
    const retry = button('重新加载');
    retry.addEventListener('click', () => void openReader(id));
    $('reader-body').replaceChildren(el('p', '未能加载资料详情。', 'notice'), retry);
  }
}
function neighbor(offset) {
  const index = page?.items.findIndex(row => row.id === readerRow?.id) ?? -1;
  if (index < 0) return;
  const next = page.items[index + offset];
  if (next) void openReader(next.id);
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
  void query({top:true});
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
    confirmedAction = {action:'bulk', scope:b.dataset.scope, expected_count:count,
      filter:renderedPageFilter, snapshot_id:page.snapshot_id};
    $('bulk').open = false;
    notice('confirm-text', bulkLabels[b.dataset.scope] + '，共 ' + fmt(count) + ' 项可选资料。');
    $('apply-bulk').textContent = '选中资料';
    openDialog('confirm');
  });
}
$('clear-all').addEventListener('click', () => {
  confirmedAction = {action:'clear'};
  notice('confirm-text', '清空转存清单？');
  $('apply-bulk').textContent = '清空选择';
  openDialog('confirm');
});
$('apply-bulk').addEventListener('click', () => {
  const command = confirmedAction;
  confirmedAction = null;
  closeDialog($('confirm'));
  if (command) {
    const {action, ...values} = command;
    void mutate(action, values);
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

async function openLinkedView() {
  const link = new URLSearchParams(location.search);
  const resource = link.get('resource');
  if (resource && /^rv_[0-9a-f]{64}$/.test(resource)) {
    await openReader(resource);
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
    [meta, draft] = await Promise.all([api('/api/meta'), api('/api/draft')]);
    setChoices('status', meta.statuses, '全部状态', row => resourceState({status:row.id}) || '待核对');
    setChoices('site', meta.sites, '全部来源');
    const {pageLimit, ...savedView} = prefs();
    filter = {...DEFAULT, ...draft.prefs, ...savedView};
    delete filter.limit;
    if (filter.category && !meta.categories.some(row => row.id === filter.category)) filter.category = '';
    paintTopics();
    loaded = true;
    syncControls();
    const previousLimit = Number.isSafeInteger(pageLimit) && pageLimit >= baseCatalogLimit ? pageLimit : baseCatalogLimit;
    const limit = fittedCatalogLimit();
    if (limit !== previousLimit) filter.page = Math.floor((filter.page - 1) * previousLimit / limit) + 1;
    await query({limit, restore:limit === previousLimit ? filter.scroll : null});
    await openLinkedView();
  } catch {
    $('cards').setAttribute('aria-busy', 'false');
    notice('error', '未能加载资源库，请刷新重试。');
  }
}, {once:true});
