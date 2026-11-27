import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Contract tests only: no browser, network, credentials or real subtitle fixture.
const script = fs.readFileSync(new URL('./browser-platform-observer.js', import.meta.url), 'utf8');
const sourcePath = '/apps/教育分析学习/待分析/fixture/lesson.mp4';
const target = 'https://fixture.baidupcs.com/netdisk-subtitle/version-fixture_01.srt';
const subtitle = '1\n00:00:00,000 --> 00:00:01,000\n仅用于测试的字幕。\n';
const config = {file_id: '101', source_revision: 'version-fixture', path: sourcePath, version_token: 'version-fixture'};
const settle = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };

function fixture() {
  const trace = {opens: [], sends: 0, fetches: [], clones: 0};
  const openResult = {};
  class FakeXHR {
    constructor() { this.listeners = []; this.responseType = ''; this.responseText = ''; this.status = 0; }
    open(...args) { trace.opens.push({self: this, args}); return openResult; }
    send() { trace.sends++; }
    addEventListener(name, fn, options) { this.listeners.push({name, fn, once: !!options?.once}); }
    removeEventListener(name, fn) { this.listeners = this.listeners.filter(x => x.name !== name || x.fn !== fn); }
    async complete(fields) {
      Object.assign(this, fields);
      for (const listener of [...this.listeners]) {
        if (listener.name !== 'loadend') continue;
        if (listener.once) this.removeEventListener(listener.name, listener.fn);
        await listener.fn.call(this);
      }
      await settle();
    }
  }
  const response = {
    status: 200, bodyUsed: false,
    clone() { trace.clones++; return {text: async () => subtitle}; },
    async text() { this.bodyUsed = true; return subtitle; }
  };
  const fetchResult = Promise.resolve(response);
  const originalFetch = function (...args) { trace.fetches.push({self: this, args}); return fetchResult; };
  const window = {fetch: originalFetch};
  const location = {origin: 'https://pan.baidu.com', href: 'https://pan.baidu.com/fixture?path=' + encodeURIComponent(sourcePath)};
  vm.runInNewContext(script, {window, location, XMLHttpRequest: FakeXHR, URL, TextEncoder, TextDecoder, Blob});
  window.installWAPlatformObserver(config);
  return {window, location, XHR: FakeXHR, trace, openResult, originalFetch, response, fetchResult,
    get state() { return window.WAPlatformObserver; }};
}

test('installation sends nothing and preserves ordinary XHR open arguments/return', () => {
  const f = fixture();
  assert.equal(f.trace.opens.length, 0);
  assert.equal(f.trace.sends, 0);
  assert.equal(f.trace.fetches.length, 0);
  const xhr = new f.XHR();
  const args = ['GET', target, true];
  assert.equal(xhr.open(...args), f.openResult);
  assert.equal(f.trace.opens[0].self, xhr);
  assert.deepEqual(f.trace.opens[0].args, args);
  f.state.restore();
  assert.equal(f.window.fetch, f.originalFetch);
  assert.equal(f.state.active, false);
});

test('Blob subtitles are read without altering the original XHR response', async () => {
  const f = fixture(), xhr = new f.XHR();
  const body = new Blob([subtitle], {type: 'text/plain;charset=utf-8'});
  xhr.open('GET', target);
  Object.defineProperty(xhr, 'responseText', {get() { throw Error('Blob responseText must not be read'); }});
  await xhr.complete({responseType: 'blob', response: body, status: 200});
  assert.equal(xhr.response, body);
  assert.equal(f.state.observations.length, 1);
  const result = f.state.observations[0];
  assert.equal(result.content, subtitle);
  assert.equal(result.bytes, new TextEncoder().encode(subtitle).length);
  assert.equal(result.transport, 'xhr-blob');
  assert.equal(result.source_file_id, config.file_id);
  assert.equal(result.temporary_url_retained, false);
});

test('fetch keeps the original promise and body, observing only a clone', async () => {
  const f = fixture(), input = new URL(target), init = {method: 'GET'};
  const answer = f.window.fetch(input, init);
  assert.equal(answer, f.fetchResult);
  assert.equal(await answer, f.response);
  await settle();
  assert.equal(f.trace.fetches.length, 1);
  assert.equal(f.trace.fetches[0].args[0], input);
  assert.equal(f.trace.fetches[0].args[1], init);
  assert.equal(f.trace.clones, 1);
  assert.equal(f.response.bodyUsed, false);
  assert.equal(f.state.observations[0].content, subtitle);
  assert.equal(await f.response.text(), subtitle);
});

test('a response arriving after a source-path switch is not claimed', async () => {
  const f = fixture(), xhr = new f.XHR();
  xhr.open('GET', target);
  f.location.href = 'https://pan.baidu.com/fixture?path=' + encodeURIComponent(sourcePath + '.other');
  await xhr.complete({status: 200, responseType: 'arraybuffer', response: new TextEncoder().encode(subtitle).buffer});
  assert.equal(f.state.observations.length, 0);
  assert.ok(f.state.errors.includes('SOURCE_CHANGED'));
});

test('signed-ticket URLs and credential-shaped headers are refused', async () => {
  const shapedBodies = [
    'https://example.invalid/resource?sign=fixture-ticket-123',
    'https://example.invalid/resource?chapter=1&bdstoken=fixture-ticket-123',
    'https://example.invalid/resource?access_token=fixture-ticket-123',
    'https://example.invalid/resource?jstoken=fixture-ticket-123',
    'Cookie: BDUSS=fixture-only-not-real',
    'Authorization: Bearer fixture-only-not-real'
  ];
  for (const suffix of shapedBodies) {
    const f = fixture(), xhr = new f.XHR();
    xhr.open('GET', target);
    await xhr.complete({status: 200, responseType: 'text', responseText: subtitle + '\n' + suffix});
    assert.equal(f.state.observations.length, 0);
    assert.ok(f.state.errors.includes('NOT_PLAIN_SUBTITLE'));
  }
});

test('public caption URLs and cookie terminology remain literal without requests or request-URL retention', async () => {
  const f = fixture(), xhr = new f.XHR();
  const requestURL = target + '?sign=fixture-request-ticket-123';
  const caption = subtitle + 'cookie 是课程术语；参考 https://example.org/docs?chapter=1\n';
  xhr.open('GET', requestURL);
  await xhr.complete({status: 200, responseType: 'text', responseText: caption});
  assert.equal(f.state.observations.length, 1);
  const observation = f.state.observations[0];
  assert.equal(observation.content, caption);
  assert.equal(observation.bytes, new TextEncoder().encode(caption).length);
  assert.equal(observation.source_file_id, config.file_id);
  assert.equal(observation.source_revision, config.source_revision);
  assert.equal(observation.temporary_url_retained, false);
  assert.equal(JSON.stringify(observation).includes(requestURL), false);
  assert.equal(JSON.stringify(observation).includes('fixture-request-ticket-123'), false);
  assert.equal(f.trace.fetches.length, 0);
  assert.equal(f.trace.sends, 0);
  assert.equal(f.trace.opens.length, 1); // Only the simulated page's ordinary open above.
  assert.equal(f.state.errors.length, 0);
});

test('reopening an unsent matching XHR cannot claim a later unrelated response', async () => {
  const f = fixture(), xhr = new f.XHR();
  xhr.open('GET', target); // No send or completion: browser listeners survive another open.
  xhr.open('GET', 'https://example.invalid/unrelated');
  xhr.send();
  await xhr.complete({status: 200, responseType: 'text', responseText: subtitle});
  assert.equal(f.trace.sends, 1);
  assert.equal(f.state.observations.length, 0);
});

test('XHR reuse during Blob.text cannot claim the earlier asynchronous read', async () => {
  const f = fixture(), xhr = new f.XHR();
  let release;
  const body = new Blob([subtitle]);
  body.text = () => new Promise(resolve => { release = resolve; });
  xhr.open('GET', target);
  const completion = xhr.complete({status: 200, responseType: 'blob', response: body});
  assert.equal(typeof release, 'function');
  xhr.open('GET', 'https://example.invalid/unrelated');
  release(subtitle);
  await completion;
  assert.equal(f.state.observations.length, 0);
});
