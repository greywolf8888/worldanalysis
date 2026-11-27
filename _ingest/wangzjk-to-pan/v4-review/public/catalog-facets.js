'use strict';

const catalogFacets = (() => {
  const number = new Intl.NumberFormat('zh-CN');

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

  return {normalize, render, displayId, detailPath, labels};
})();
