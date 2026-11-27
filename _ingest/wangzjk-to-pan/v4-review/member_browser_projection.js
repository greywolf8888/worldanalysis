function readMemberArticle(articleId) {
  const article = document.querySelector('article');
  const text = article?.innerText || document.body?.innerText || '';
  if (/访问过于频繁|请完成安全验证|安全验证后继续访问|请先登录/.test(text.slice(0, 5000))) {
    return {outcome: 'access_hold', links: []};
  }
  if (location.origin !== 'https://www.wangzjk.com' || location.pathname !== `/${articleId}.html`
      || !article || !document.querySelector('h1')?.innerText.trim()) {
    return {outcome: 'page_not_readable', links: []};
  }

  const visible = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility === 'visible'
    && !el.closest('[hidden],[aria-hidden=true]');
  const baiduControl = el => /^百度(?:网盘|云盘)$/.test(el.innerText?.trim() || '')
    && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
  const copyAccessRequired = /内容需要权限查看|购买本内容|会员免费查看|权限不足|还未购买|登录后查看/;
  const links = new Map();
  const conflicting = new Set();
  let baiduEntry = false;
  let articleAccessRequired = /无权下载|请先购买/.test(text.slice(0, 5000));
  function remember(raw) {
    const address = raw.match(/^https:\/\/pan\.baidu\.com\/(?:s\/[A-Za-z0-9_-]{1,129}|share\/init)(?=[?\s#]|$)[^\s<>]*/)?.[0];
    if (!address) return;
    const url = new URL(address);
    const short = url.searchParams.getAll('surl');
    const path = url.pathname === '/share/init' && short.length === 1 && /^[A-Za-z0-9_-]{1,128}$/.test(short[0])
      ? `/s/1${short[0]}` : url.pathname;
    if (url.origin !== 'https://pan.baidu.com' || !/^\/s\/[A-Za-z0-9_-]{1,129}$/.test(path)) return;
    const key = url.origin + path;
    const params = url.searchParams.getAll('pwd');
    const code = params.length === 1 ? params[0] : !params.length && raw.slice(address.length).match(/^\s+提取码[：:\s]*([A-Za-z0-9]{4})\b/)?.[1];
    const validCode = /^[A-Za-z0-9]{4}$/.test(code || '') ? code : null;
    const previous = links.get(key);
    if (previous && validCode && previous !== validCode) conflicting.add(key);
    if (!links.has(key) || validCode) links.set(key, validCode);
  }

  function copyLink(control) {
    const text = (control.getAttribute('data-clipboard-text') || '').trim();
    if (copyAccessRequired.test(text)) {
      articleAccessRequired = true;
    } else {
      const leadingLabels = /^(?:(?:百度(?:网盘|云盘)(?:链接)?|分享链接|链接|地址)\s*[：:]\s*){1,2}/;
      remember(text.replace(leadingLabels, ''));
    }
  }
  for (const anchor of article.querySelectorAll('a[href]')) {
    if (!visible(anchor)) continue;
    remember(anchor.href);
    if (!baiduControl(anchor)) continue;
    baiduEntry = true;
    if (anchor.hasAttribute('data-clipboard-text')) copyLink(anchor);
    const url = new URL(anchor.href);
    if (url.origin !== location.origin || url.pathname.replace(/\/$/, '') !== `/pay-download/${articleId}`) continue;
    for (const copy of anchor.parentElement?.parentElement?.querySelectorAll('[data-clipboard-text]') || []) {
      if (visible(copy)) copyLink(copy);
    }
  }
  if (!links.size && !articleAccessRequired) {
    const embedded = [];
    for (const control of article.querySelectorAll('[data-clipboard-text]')) {
      if (!visible(control) || !baiduControl(control)) continue;
      baiduEntry = true;
      embedded.push({raw: control.getAttribute('data-clipboard-text') || '', copy: true});
    }
    for (const box of article.querySelectorAll('#posts-pay.paid-box')) {
      if (!visible(box)) continue;
      // Rendered text ignores CSS-hidden content. Exclude a box if hidden markup
      // can still contribute text to that rendering.
      const concealedText = Array.from(box.querySelectorAll('[hidden],[aria-hidden=true]'))
        .some(node => node.getClientRects().length > 0 && (node.innerText || node.textContent || '').trim());
      if (!concealedText) embedded.push({raw: box.innerText || '', copy: false});
    }

    articleAccessRequired = embedded.some(({raw}) => copyAccessRequired.test(raw) || /无权下载|请先购买/.test(raw));
    if (!articleAccessRequired) {
      const leadingLabelsOnly = /^(?:(?:百度(?:网盘|云盘)(?:链接)?|分享链接|链接|地址)\s*[：:]\s*)+$/;
      const singleLabelOnly = /^[^\s：:，。！？]+\s*[：:]$/;
      for (const {raw, copy} of embedded) {
        const starts = /(?:^|[\s：（【“]|付费资源|(?:百度(?:网盘|云盘)(?:链接)?|分享链接|链接|地址)\s*:)(?=https:\/\/pan\.baidu\.com\/)/g;
        for (const match of raw.matchAll(starts)) {
          if (copy && match[0] === '付费资源') continue;
          const start = match.index + match[0].length;
          const preceding = raw.slice(0, start);
          if (preceding.match(/\S+$/)?.[0].includes('://')) continue;
          const prefix = preceding.trim();
          if (copy && (!prefix || leadingLabelsOnly.test(prefix) || singleLabelOnly.test(prefix))) continue;
          remember(raw.slice(start));
        }
      }
      if (links.size > 1) {
        baiduEntry = true;
        links.clear();
      }
    }
  }
  const matches = [...links].map(([url, code]) => code && !conflicting.has(url) ? `${url}?pwd=${code}` : url);
  const outcome = matches.length ? 'linked'
    : articleAccessRequired ? 'article_access_required' : baiduEntry ? 'baidu_entry_pending' : 'no_baidu_entry';
  return {outcome, links: matches};
}
