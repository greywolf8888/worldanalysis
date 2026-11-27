/* Inject with the authorized dsh_browser_execute only. No network or credentials.
 * Existing DOM text/images are observations, never automatic coverage approval. */
(() => {
  'use strict';
  const namespace = 'WAPlatformProducts';
  const need = (ok, message) => { if (!ok) throw Error(message); };
  let source = null;
  const actualPath = () => new URL(location.href).searchParams.get('path');
  const guard = () => { need(source && actualPath() === source.path, 'SOURCE_CHANGED'); return source; };
  const hex = bytes => Array.from(new Uint8Array(bytes), x => x.toString(16).padStart(2, '0')).join('');
  const api = {
    bind({file_id, path, source_revision, page_count}) {
      need(location.origin === 'https://pan.baidu.com', 'WRONG_ORIGIN');
      need(typeof file_id === 'string' && /^[0-9]{1,32}$/.test(file_id), 'SOURCE_ID_REQUIRED');
      need(typeof path === 'string' && path.startsWith('/apps/教育分析学习/') && actualPath() === path, 'SOURCE_SCOPE_MISMATCH');
      need(typeof source_revision === 'string' && source_revision.length > 0, 'SOURCE_REVISION_REQUIRED');
      need(page_count == null || Number.isSafeInteger(page_count) && page_count > 0 && page_count <= 1000, 'INVALID_PAGE_COUNT');
      document.querySelectorAll('video').forEach(v => v.pause());
      source = {file_id, path, source_revision, page_count};
      return {bound: true, source: {...source}};
    },
    draft() {
      const s = guard();
      const nodes = [...document.querySelectorAll('.ai-draft__p-sentence')];
      need(nodes.length > 0, 'DRAFT_NOT_LOADED');
      need(nodes.every((n, i) => Number(n.dataset.index) === i), 'NONCONTIGUOUS_DRAFT');
      const paragraphs = [...document.querySelectorAll('.ai-draft__p-paragraph')].map(n => ({
        start_ms: n.hasAttribute('data-starttime') ? Number(n.dataset.starttime) : null,
        sentence_indices: [...n.querySelectorAll('.ai-draft__p-sentence')].map(s => Number(s.dataset.index)),
        text: n.textContent
      }));
      need(paragraphs.every(p => p.start_ms === null || Number.isFinite(p.start_ms) && p.start_ms >= 0), 'INVALID_PARAGRAPH_TIME');
      return {schema: 'worldanalysis.browser-draft.v1', observed_at: new Date().toISOString(), source: {...s},
        method: 'existing_platform_dom', sentence_count: nodes.length, paragraphs,
        sentence_times: 'not_exposed_in_dom', extraction_scope: 'all_current_dom_paragraphs',
        full_media_coverage: 'requires_independent_review'};
    },
    showPage(index) {
      guard(); need(Number.isSafeInteger(index) && index >= 0 && index < source.page_count, 'INVALID_PAGE');
      const nodes = [...document.querySelectorAll('img.ai-course__apply-wrap-course')];
      need(nodes.length === source.page_count, 'PAGE_COUNT_MISMATCH');
      need(nodes.every((n, i) => Number(n.dataset.index) === i), 'NONCONTIGUOUS_PAGES');
      nodes[index].scrollIntoView({block: 'center'});
      return {page: index + 1, width: nodes[index].naturalWidth, height: nodes[index].naturalHeight, encoded_chars: nodes[index].src.length};
    },
    async page(index) {
      const s = guard();
      need(Number.isSafeInteger(index) && index >= 0 && index < s.page_count, 'INVALID_PAGE');
      const image = document.querySelector('img.ai-course__apply-wrap-course[data-index="' + index + '"]');
      need(image && image.complete && image.naturalWidth > 0, 'PAGE_NOT_READY');
      need(/^data:image\/(?:png|jpeg|jpg|webp);base64,/.test(image.src), 'NORMAL_DISPLAY_IMAGE_REQUIRED');
      const capturedSrc = image.src;
      await image.decode();
      need(guard() === s && image.src === capturedSrc, 'SOURCE_CHANGED_DURING_CAPTURE');
      const base64 = capturedSrc.split(',')[1];
      const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
      need(bytes.length > 0 && bytes.length <= 4 * 1024 * 1024, 'PAGE_BYTE_BOUND');
      const canvas = document.createElement('canvas'); canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
      const ctx = canvas.getContext('2d', {willReadFrequently: true}); ctx.drawImage(image, 0, 0);
      const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
      const format = bytes[0] === 255 && bytes[1] === 216 ? 'jpeg' : bytes[0] === 137 && bytes[1] === 80 ? 'png' : null;
      need(format, 'UNSUPPORTED_DISPLAY_IMAGE_MAGIC');
      const sha256 = hex(await crypto.subtle.digest('SHA-256', bytes));
      const pixel_sha256 = hex(await crypto.subtle.digest('SHA-256', pixels));
      need(guard() === s && image.isConnected !== false && image.src === capturedSrc, 'SOURCE_CHANGED_DURING_CAPTURE');
      return {schema: 'worldanalysis.browser-courseware-page.v1', observed_at: new Date().toISOString(), source: {...s},
        page: index + 1, width: canvas.width, height: canvas.height, bytes: bytes.length, format,
        sha256, pixel_sha256,
        representation: 'normally_displayed_platform_page', not_native_container_bytes: true,
        base64};
    }
  };
  window[namespace] = api;
  return {installed: true, namespace};
})()
