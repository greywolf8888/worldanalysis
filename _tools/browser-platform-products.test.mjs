import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createHash, webcrypto} from 'node:crypto';

// Fake DOM/decode/canvas: this tests contracts, not browser/image-decoder acceptance.
const script = fs.readFileSync(new URL('./browser-platform-products.js', import.meta.url), 'utf8');
const sourcePath = '/apps/教育分析学习/待分析/fixture/lesson.mp4';
const png = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
const pixels = new Uint8ClampedArray([0, 1, 2, 255, 3, 4, 5, 255]);
const hash = value => createHash('sha256').update(value).digest('hex');

function fixture(options = {}) {
  const trace = {paused: 0, decoded: 0, drawn: 0, scrolled: 0, digests: 0};
  const location = {origin: 'https://pan.baidu.com', href: 'https://pan.baidu.com/fixture?path=' + encodeURIComponent(sourcePath)};
  const sentences = (options.indices || [0, 1]).map(index => ({dataset: {index: String(index)}, textContent: '句' + index}));
  const paragraphs = sentences.map((sentence, index) => ({
    dataset: {starttime: String(index * 1000)}, textContent: sentence.textContent,
    hasAttribute: name => name === 'data-starttime', querySelectorAll: () => [sentence]
  }));
  const images = [0, 1].map(index => ({
    dataset: {index: String(index)}, complete: true, isConnected: true,
    naturalWidth: 2, naturalHeight: 1, src: 'data:image/png;base64,' + png.toString('base64'),
    async decode() { trace.decoded++; if (options.onDecode) await options.onDecode({location, images}); },
    scrollIntoView() { trace.scrolled++; }
  }));
  const document = {
    querySelectorAll(selector) {
      if (selector === 'video') return [{pause() { trace.paused++; }}];
      if (selector === '.ai-draft__p-sentence') return sentences;
      if (selector === '.ai-draft__p-paragraph') return paragraphs;
      if (selector === 'img.ai-course__apply-wrap-course') return images;
      throw Error('Unexpected DOM selector: ' + selector);
    },
    querySelector(selector) {
      const match = /data-index="([0-9]+)"/.exec(selector);
      return match ? images.find(image => image.dataset.index === match[1]) : null;
    },
    createElement(tag) {
      assert.equal(tag, 'canvas');
      return {getContext() { return {
        drawImage() { trace.drawn++; }, getImageData() { return {data: pixels}; }
      }; }};
    }
  };
  const window = {};
  vm.runInNewContext(script, {window, location, document, URL, atob, Uint8Array,
    fetch() { throw Error('Products helper must not fetch'); },
    crypto: {subtle: {digest(...args) {
      trace.digests++;
      if (options.onDigest) options.onDigest({location, images, call: trace.digests});
      return webcrypto.subtle.digest(...args);
    }}}
  });
  const api = window.WAPlatformProducts;
  const binding = {file_id: '101', source_revision: 'version-fixture', path: sourcePath, page_count: 2};
  api.bind(binding);
  return {api, location, binding, images, sentences, paragraphs, trace};
}

test('draft preserves all current paragraphs without claiming full media coverage', () => {
  const f = fixture(), result = f.api.draft();
  assert.equal(f.trace.paused, 1);
  assert.equal(result.sentence_count, 2);
  assert.equal(result.paragraphs.map(p => p.text).join('|'), '句0|句1');
  assert.equal(result.paragraphs[1].start_ms, 1000);
  assert.equal(result.source.file_id, f.binding.file_id);
  assert.equal(result.full_media_coverage, 'requires_independent_review');
  assert.equal(result.extraction_scope, 'all_current_dom_paragraphs');
});

test('noncontiguous draft/page indices and incomplete page lists are rejected', () => {
  for (const indices of [[0, 2], [0, 0]]) {
    assert.throws(() => fixture({indices}).api.draft(), /NONCONTIGUOUS_DRAFT/);
  }
  const f = fixture();
  f.images[1].dataset.index = '2';
  assert.throws(() => f.api.showPage(0), /NONCONTIGUOUS_PAGES/);
  f.images.pop();
  assert.throws(() => f.api.showPage(0), /PAGE_COUNT_MISMATCH/);
});

test('source-path changes reject synchronous reads and image reads', async () => {
  const f = fixture();
  f.location.href = 'https://pan.baidu.com/fixture?path=' + encodeURIComponent(sourcePath + '.other');
  assert.throws(() => f.api.draft(), /SOURCE_CHANGED/);
  assert.throws(() => f.api.showPage(0), /SOURCE_CHANGED/);
  await assert.rejects(f.api.page(0), /SOURCE_CHANGED/);
  assert.throws(() => f.api.bind(f.binding), /SOURCE_SCOPE_MISMATCH/);
});

test('normal data-URI output binds exact image bytes and displayed pixels', async () => {
  const f = fixture();
  assert.equal(f.api.showPage(0).page, 1);
  const result = await f.api.page(0);
  assert.equal(f.trace.decoded, 1);
  assert.equal(f.trace.drawn, 1);
  assert.equal(result.bytes, png.length);
  assert.equal(result.base64, png.toString('base64'));
  assert.equal(result.sha256, hash(png));
  assert.equal(result.pixel_sha256, hash(pixels));
  assert.equal(result.source.source_revision, f.binding.source_revision);
  assert.equal(result.not_native_container_bytes, true);
});

test('remote image URLs and source changes during either await are refused', async t => {
  await t.test('remote URL', async () => {
    const f = fixture();
    f.images[0].src = 'https://example.invalid/private-image';
    await assert.rejects(f.api.page(0), /NORMAL_DISPLAY_IMAGE_REQUIRED/);
    assert.equal(f.trace.decoded, 0);
  });
  await t.test('decode await', async () => {
    const f = fixture({onDecode({location}) {
      location.href = 'https://pan.baidu.com/fixture?path=' + encodeURIComponent(sourcePath + '.other');
    }});
    await assert.rejects(f.api.page(0), /SOURCE_CHANGED/);
  });
  await t.test('digest await', async () => {
    const f = fixture({onDigest({location, call}) {
      if (call === 1) location.href = 'https://pan.baidu.com/fixture?path=' + encodeURIComponent(sourcePath + '.other');
    }});
    await assert.rejects(f.api.page(0), /SOURCE_CHANGED/);
  });
  await t.test('same-path source-version rebind', async () => {
    let f;
    f = fixture({onDigest({call}) {
      if (call === 1) f.api.bind({...f.binding, source_revision: 'version-replaced'});
    }});
    await assert.rejects(f.api.page(0), /SOURCE_CHANGED/);
  });
});

test('same-path image updates during digest cannot mismatch base64, bytes and hash', async () => {
  const f = fixture({onDigest({images, call}) {
    if (call === 1) images[0].src = 'data:image/png;base64,' + Buffer.concat([png, Buffer.from([99, 100])]).toString('base64');
  }});
  try {
    const result = await f.api.page(0);
    const returned = Buffer.from(result.base64, 'base64');
    assert.equal(result.bytes, returned.length);
    assert.equal(result.sha256, hash(returned));
  } catch (error) {
    assert.match(error.message, /(?:PAGE|IMAGE|SOURCE).*CHANGED/);
  }
});
