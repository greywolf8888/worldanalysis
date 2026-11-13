// Synthetic fake-XHR contract tests only; never evidence of a real cloud session.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
const source = fs.readFileSync(new URL('./browser-inventory-observer.js', import.meta.url), 'utf8')
const plain = value => JSON.parse(JSON.stringify(value))
const folder = '/synthetic-course'
const item = (id = '1', name = 'a.mp4', size = 12) => ({ fs_id: id, path: folder + '/' + name, server_filename: name, size, isdir: 0 })
const body = (list = [item()], extra = {}) => ({ errno: 0, path: folder, page: 1, has_more: false, list, ...extra })
function setup() {
  const network = [], parsedInputs = []
  class GuardURL extends URL {
    constructor(input, base) {
      assert(!/[?#]/.test(input), 'query/fragment must be removed before URL parsing')
      parsedInputs.push(input); super(input, base)
    }
    get search() { throw Error('must not read query') }
    get searchParams() { throw Error('must not read query parameters') }
    get username() { throw Error('must not read credentials') }
    get password() { throw Error('must not read credentials') }
  }
  class XHR extends EventTarget {
    constructor() { super(); this.status = 0; this.responseType = 'json'; this.readyState = 0; this.calls = [] }
    open(...args) { this.calls.push(args); this.readyState = 1; return 'original-open-result' }
    send(...args) { network.push(args); return 'original-send-result' }
    abort() { this.aborted = true }
    get responseURL() { throw Error('responseURL must not be read') }
    getAllResponseHeaders() { throw Error('headers must not be read') }
    complete(value = body(), status = 200) {
      this.status = status; this.readyState = 4; this.response = value
      if (this.responseType !== 'json') this.responseText = typeof value === 'string' ? value : JSON.stringify(value)
      for (const event of ['readystatechange', 'load', 'loadend']) this.dispatchEvent(new Event(event))
    }
  }
  const originalOpen = XHR.prototype.open, originalSend = XHR.prototype.send, originalAbort = XHR.prototype.abort
  const location = { origin: 'https://pan.baidu.com' }
  Object.defineProperty(location, 'href', { get() { throw Error('page URL must not be read') } })
  const window = { XMLHttpRequest: XHR, location }
  Object.defineProperty(window, 'document', { get() { throw Error('document/cookies must not be read') } })
  Object.defineProperty(window, 'fetch', { get() { throw Error('observer must not fetch') } })
  const context = vm.createContext({ window, URL: GuardURL, Reflect })
  vm.runInContext(source, context)
  const api = window.WAInventoryObserver
  const arm = (extra = {}) => api.arm({ current_expected_folder: folder, source_share_ref: 'synthetic-share', source_type: 'share', origin: location.origin, request_generation_id: 'generation-1', observed_read_paths: [{ pathname: '/share/list', pure_read: true, evidence_ref: 'synthetic-ui-path' }], evidence_class: 'synthetic', ...extra })
  const receive = (value = body(), url = '/share/list', status = 200) => { const xhr = new XHR(); xhr.open('GET', url, true); xhr.complete(value, status); return xhr }
  return { api, XHR, window, context, arm, receive, network, parsedInputs, originalOpen, originalSend, originalAbort }
}

test('expression injection and idempotent installation', () => {
  new Function('return (' + source + ')')
  const s = setup(), wrapper = s.XHR.prototype.open
  vm.runInContext(source, s.context)
  assert.equal(s.XHR.prototype.open, wrapper)
})
test('original arguments/return/send/abort/response and UI callbacks remain unchanged; no extra network', () => {
  const s = setup(); s.arm()
  const x = new s.XHR(), response = body()
  let calls = 0
  for (const name of ['readystatechange', 'load', 'loadend']) x.addEventListener(name, () => { calls++; assert.equal(x.response, response) })
  assert.equal(x.open('GET', '/share/list', true, undefined, undefined), 'original-open-result')
  assert.equal(x.send(), 'original-send-result')
  assert.equal(s.XHR.prototype.send, s.originalSend); assert.equal(s.XHR.prototype.abort, s.originalAbort)
  x.complete(response)
  assert.equal(calls, 3); assert.equal(x.response, response)
  assert.deepEqual(x.calls, [['GET', '/share/list', true, undefined, undefined]])
  assert.equal(s.network.length, 1)
  assert.equal(s.api.read().receipts[0].evidence_class, 'synthetic')
  assert.equal(s.api.read().inventory.enumeration_complete, true)
})
test('query credentials never parsed/read/exported and unknown response fields untouched', () => {
  const s = setup(); s.arm({ expected_owner_uk: '55' })
  const response = body(), rawItem = response.list[0]
  for (const target of [response, rawItem]) for (const key of ['Cookie', 'token', 'bdstoken', 'sekey', 'sign', 'dlink', 'url']) Object.defineProperty(target, key, { get() { throw Error('sensitive field accessed') } })
  response.owner_uk = '55'
  s.receive(response, '/share/list?bdstoken=NEVER_PARSE&sekey=NEVER_READ&sign=NEVER_EXPORT#NEVER_FRAGMENT')
  const result = plain(s.api.read())
  assert.equal(result.receipts[0].owner_matches, true)
  const serialized = JSON.stringify(result)
  for (const forbidden of ['NEVER_', 'owner_uk', 'bdstoken', 'Cookie', 'https:', '"55"']) assert(!serialized.includes(forbidden))
  assert.deepEqual(s.parsedInputs, ['/share/list'])
  assert.deepEqual(result.receipts[0].list, [item()])
})
test('actual observed pure-read evidence is required; no guessed endpoints', () => {
  const s = setup()
  for (const observed_read_paths of [[], [{ pathname: '/api/list', pure_read: true, evidence_ref: 'x' }], [{ pathname: '/share/list', pure_read: false, evidence_ref: 'x' }], [{ pathname: '/share/list?x=y', pure_read: true, evidence_ref: 'x' }]]) assert.throws(() => s.arm({ observed_read_paths }))
  assert.equal(s.network.length, 0)
})
for (const [method, url] of [['POST', '/share/list'], ['GET', '/api/list'], ['GET', '/share/list/extra'], ['GET', '/api/download'], ['GET', 'https://other.invalid/share/list'], ['GET', '/other']]) test('only exact same-origin read response: ' + method + ' ' + url, () => {
  const s = setup(); s.arm(); const x = new s.XHR(); x.open(method, url); x.complete()
  assert.equal(s.api.read().receipts.length, 0); assert.equal(s.network.length, 0)
})
test('URL objects are not coerced and armed origin cannot differ', () => {
  const s = setup(); s.arm()
  const x = new s.XHR(); x.open('GET', { toString() { throw Error('URL object coerced') } }); x.complete()
  assert.equal(s.api.read().receipts.length, 0)
  assert.throws(() => s.arm({ origin: 'https://other.invalid', request_generation_id: 'g2' }))
})
test('user disk and share endpoints/identity scopes stay separate', () => {
  const s = setup(); s.arm({ source_type: 'user_disk', observed_read_paths: [{ pathname: '/api/list', pure_read: true, evidence_ref: 'owned-ui' }] })
  s.receive(body(), '/share/list'); assert.equal(s.api.read().receipts.length, 0)
  s.receive(body(), '/api/list'); assert.equal(s.api.read().receipts[0].source_type, 'user_disk')
})
test('late old responses and pre-arm requests cannot enter new generation', () => {
  const s = setup(), pre = new s.XHR(); pre.open('GET', '/share/list')
  s.arm(); const old = new s.XHR(); old.open('GET', '/share/list')
  s.arm({ request_generation_id: 'generation-2' }); old.complete(); pre.complete()
  assert.equal(s.api.read().receipts.length, 0)
  s.receive(); assert.equal(s.api.read().receipts[0].request_generation_id, 'generation-2')
  assert.throws(() => s.arm({ request_generation_id: 'generation-1' }))
})
test('wrong folder is explicit and its rows never leave the page', () => {
  const s = setup(); s.arm(); s.receive(body([{ ...item(), path: '/wrong/DO_NOT_EXPORT.mp4', server_filename: 'DO_NOT_EXPORT.mp4' }], { path: '/wrong' }))
  const result = s.api.read()
  assert.equal(result.receipts[0].folder_matches, false)
  assert.deepEqual(plain(result.receipts[0].list), [])
  assert(!JSON.stringify(result).includes('DO_NOT_EXPORT'))
  assert.equal(result.inventory.enumeration_complete, false)
})
test('response folder or complete direct-child paths are required; empty unbound list is unknown', () => {
  const s = setup(); s.arm(); s.receive({ errno: 0, list: [], page: 1, has_more: false })
  assert.equal(s.api.read().receipts[0].folder_matches, null)
  assert.equal(s.api.read().inventory.enumeration_complete, false)
  s.arm({ request_generation_id: 'g2' }); s.receive({ errno: 0, list: [item()], page: 1, has_more: false })
  assert.equal(s.api.read().receipts[0].folder_evidence, 'list_parent_paths')
  assert.equal(s.api.read().inventory.enumeration_complete, true)
})
test('explicit empty folder can be complete, contradictory folder metadata cannot', () => {
  const s = setup(); s.arm(); s.receive(body([])); assert.equal(s.api.read().inventory.enumeration_complete, true)
  s.arm({ request_generation_id: 'g2' }); s.receive(body([], { root: '/other' }))
  assert.equal(s.api.read().inventory.enumeration_complete, false)
})
test('num=20 and ten rows without terminal/page evidence are not a total', () => {
  const s = setup(); s.arm(); const response = body(Array.from({ length: 10 }, (_, i) => item(String(i), i + '.mp4')), { num: 20 })
  delete response.has_more; delete response.page; s.receive(response)
  assert.equal(s.api.read().inventory.list.length, 10)
  assert.equal(s.api.read().inventory.enumeration_complete, false)
})
test('numbered pagination requires page one, contiguous pages and explicit has_more on each', () => {
  const s = setup(); s.arm(); s.receive(body([item('2', 'b.mp4')], { page: 2, has_more: false, total: 2 }))
  assert.equal(s.api.read().inventory.enumeration_complete, false)
  s.receive(body([item()], { page: 1, has_more: true, total: 2 }))
  assert.equal(s.api.read().inventory.enumeration_complete, true)
})
test('cursor chain starts at empty cursor and must have an explicit terminal', () => {
  const s = setup(); s.arm(); const first = body(); delete first.page
  Object.assign(first, { has_more: true, cursor: '', next_cursor: 'next_2' }); s.receive(first)
  const second = body([item('2', 'b.mp4')]); delete second.page
  Object.assign(second, { cursor: 'next_2', has_more: false, next_cursor: '' }); s.receive(second)
  assert.equal(s.api.read().inventory.enumeration_complete, true)
})
test('duplicate pages, IDs, paths and total inconsistencies are explicit conflicts, not deduplicated completeness', () => {
  const s = setup(); s.arm(); s.receive(); s.receive()
  const result = s.api.read().inventory
  for (const issue of ['DUPLICATE_PAGE', 'DUPLICATE_FILE_ID', 'DUPLICATE_PATH']) assert(result.issues.includes(issue))
  assert.equal(result.state, 'conflict'); assert.equal(result.enumeration_complete, false)
  s.arm({ request_generation_id: 'g2' }); s.receive(body(undefined, { total: 999 }))
  assert(s.api.read().inventory.issues.includes('TOTAL_CONFLICT'))
})
test('malformed IDs/paths/booleans/unsafe sizes and invalid metadata do not become exact complete observations', () => {
  for (const bad of [{ fs_id: 9007199254740992 }, { path: folder + '/../a.mp4' }, { isdir: '0' }, { size: -1 }, { size: 9007199254740992 }, { size: '12' }, { server_filename: 'wrong.mp4' }]) {
    const s = setup(); s.arm(); s.receive(body([{ ...item(), ...bad }]))
    assert.equal(s.api.read().inventory.enumeration_complete, false)
  }
  const s = setup(); s.arm(); s.receive(body(undefined, { cursor: 'https://secret.invalid/?sign=DROP' }))
  assert.equal(s.api.read().inventory.enumeration_complete, false); assert(!JSON.stringify(s.api.read()).includes('secret.invalid'))
})
test('contradictory numbered terminal/next-cursor metadata is explicit conflict', () => {
  const s = setup(); s.arm(); s.receive(body(undefined, { next_cursor: 'not_terminal' }))
  assert.equal(s.api.read().inventory.state, 'conflict')
  assert(s.api.read().inventory.issues.includes('PAGINATION_CONFLICT'))
  assert.equal(s.api.read().inventory.enumeration_complete, false)
})
test('a discarded invalid row cannot make exact-size evidence vacuously true', () => {
  const s = setup(); s.arm(); s.receive(body([{ ...item(), fs_id: 'not_an_id' }]))
  assert.equal(s.api.read().inventory.exact_sizes_verified, false)
})
test('owner is only a local comparison boolean; mismatch remains partial', () => {
  const s = setup(); s.arm({ expected_owner_uk: '55' }); s.receive(body(undefined, { uk: '56' }))
  assert.equal(s.api.read().receipts[0].owner_matches, false)
  assert.equal(s.api.read().inventory.enumeration_complete, false)
})
for (const status of [113, 401, 403, 429]) test('passive HTTP denial is retained even for non-JSON body: ' + status, () => {
  const s = setup(); s.arm(); const x = new s.XHR(); x.responseType = 'text'; x.open('GET', '/share/list'); x.complete('<html>DO_NOT_RETAIN</html>', status)
  assert.equal(s.api.read().inventory.state, 'blocked')
  assert.equal(s.api.read().receipts[0].http_status, status)
  assert(!JSON.stringify(s.api.read()).includes('DO_NOT_RETAIN'))
  s.receive(); assert.equal(s.api.read().receipts.length, 1)
  assert.throws(() => s.arm({ request_generation_id: 'g2' }))
  assert.equal(s.network.length, 0)
})
for (const extra of [{ errno: 113 }, { errno: 401 }, { errno: 403 }, { errno: 429 }, { need_captcha: 1 }]) test('provider/CAPTCHA denial blocks without alternate channel: ' + JSON.stringify(extra), () => {
  const s = setup(); s.arm(); s.receive(body(undefined, extra)); assert.equal(s.api.read().inventory.state, 'blocked'); assert.equal(s.network.length, 0)
})
test('malformed/unsupported/non-success responses stay unknown', () => {
  for (const value of [null, [], { errno: 0, list: 'bad' }, { errno: 9, secret: 'DO_NOT_RETAIN' }]) {
    const s = setup(); s.arm(); s.receive(value); assert.equal(s.api.read().inventory.enumeration_complete, false)
    assert(!JSON.stringify(s.api.read()).includes('DO_NOT_RETAIN'))
  }
})
test('XHR reuse, multiple load events, detached reads and cooperative restore', () => {
  const s = setup(); s.arm(); const x = new s.XHR(); x.open('GET', '/share/list'); x.open('GET', '/unrelated'); x.complete()
  assert.equal(s.api.read().receipts.length, 0)
  const y = s.receive(); y.dispatchEvent(new Event('load')); assert.equal(s.api.read().receipts.length, 1)
  s.api.read().receipts[0].list[0].path = '/mutated'
  assert.equal(s.api.read().receipts[0].list[0].path, item().path)
  const ours = s.XHR.prototype.open
  function later(...args) { return Reflect.apply(ours, this, args) }
  s.XHR.prototype.open = later; s.api.restore(); assert.equal(s.XHR.prototype.open, later)
  s.receive(); assert.equal(s.api.read().receipts.length, 1)
})
test('restore invalidates pending loads and restores own open wrapper', () => {
  const s = setup(); s.arm(); const x = new s.XHR(); x.open('GET', '/share/list'); s.api.restore(); x.complete()
  assert.equal(s.api.read().receipts.length, 0); assert.equal(s.XHR.prototype.open, s.originalOpen)
})
