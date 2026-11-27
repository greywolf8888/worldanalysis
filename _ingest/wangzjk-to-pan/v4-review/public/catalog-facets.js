'use strict';

const catalogFacets = (() => {
  const number = new Intl.NumberFormat('zh-CN');
  const transferLabels = new Map(Object.entries({
    transferred: '已转存',
    transferred_previously: '曾转存',
    recycled: '已回收',
  }));
  const analysisLabels = new Map(Object.entries({
    queued: '待分析',
    in_progress: '分析中',
    awaiting_originals: '待原件',
    same_source_reuse: '同源复用',
    archive_only: '留档',
    completed: '已分析',
    completed_received_scope: '已分析实收资料',
    local_result_available: '已有本地分析',
    awaiting_acceptance: '待验收',
    awaiting_re_review: '待复验',
  }));

  function text(tag, value, className) {
    const element = document.createElement(tag);
    element.textContent = value;
    if (className) element.className = className;
    return element;
  }

  function normalize(values, meta) {
    const categories = (meta.categories || []).filter(item => item.label && item.id !== 'unsorted');
    const platforms = meta.platforms || [];
    const aliases = meta.category_aliases || {};
    const category = aliases[values.category] || values.category;
    const choice = categories.find(item => item.id === category);
    return {
      ...values,
      category: choice?.id || '',
      subcategory: choice?.subcategories?.some(item => item.id === values.subcategory) ? values.subcategory : '',
      platform: platforms.some(item => item.id === values.platform) ? values.platform : '',
    };
  }

  function control(item, field, value, change) {
    const button = text('button', '', 'topic');
    button.type = 'button';
    button.dataset.facet = field;
    button.dataset.value = item.id;
    button.setAttribute('aria-pressed', String(value === item.id));
    button.append(text('span', item.label));
    if (Number.isSafeInteger(item.count)) button.append(text('small', number.format(item.count)));
    button.addEventListener('click', () => change(field, item.id));
    return button;
  }

  function render(targets, meta, selected, change) {
    const {category, platform, subcategory} = targets;
    const active = document.activeElement;
    const focused = [category, platform, subcategory].some(node => node.contains(active)) ? active : null;
    const count = meta.totals?.projects ?? meta.project_total;
    const categories = (meta.categories || []).filter(item => item.label && item.id !== 'unsorted');
    const platforms = meta.platforms || [];
    const choices = categories.find(item => item.id === selected.category)?.subcategories || [];

    const fill = (field, items) => items.map(item => control(item, field, selected[field], change));
    category.replaceChildren(...fill('category', [{id: '', label: '全部业务', count}, ...categories]));
    subcategory.hidden = !selected.category || !choices.length;
    subcategory.replaceChildren(...(subcategory.hidden ? [] : fill('subcategory', [{id: '', label: '全部细分'}, ...choices])));

    platform.replaceChildren(...fill('platform', [{id: '', label: '全部平台', count}, ...platforms]));

    if (focused?.dataset.facet) {
      const candidates = [category, platform, subcategory].flatMap(node => [...node.querySelectorAll('button')]);
      candidates.find(button => button.dataset.facet === focused.dataset.facet &&
        button.dataset.value === focused.dataset.value)?.focus({preventScroll: true});
    }
  }

  function displayId(row) {
    return row.display_id || row.article_key || row.id;
  }

  function detailPath(row, section = '') {
    const query = new URLSearchParams({resource: row.id});
    if (row.article_key) query.set('article', row.article_key);
    if (section) query.set('details', section);
    return '/?' + query;
  }

  function labels(row, active = '', maximum = 2) {
    const values = Array.isArray(row.platforms) ? row.platforms.filter(item => item?.label) : [];
    const prioritized = values.find(item => item.id === active);
    const ordered = prioritized ? [prioritized, ...values.filter(item => item !== prioritized)] : values;
    return [...new Set(ordered.map(item => item.label))].slice(0, maximum);
  }

  function statusText(row) {
    const legacyTransfer = row.status === 'verified' ? '曾转存' : row.status === 'recycled' ? '已回收' : '';
    const transfer = Object.hasOwn(row, 'transfer_state') ? transferLabels.get(row.transfer_state) : legacyTransfer;
    const access = row.status === 'restricted' ? '原文访问受限' : '';
    let analysis = analysisLabels.get(row.analysis_state);
    if (['completed', 'completed_received_scope'].includes(row.analysis_state) &&
        ['核心待补', '配套待补'].includes(row.analysis_scope)) {
      analysis = `已分析（${row.analysis_scope}）`;
    }
    return [transfer, analysis, access].filter(Boolean).join(' · ');
  }

  return {normalize, render, displayId, detailPath, labels, statusText};
})();
