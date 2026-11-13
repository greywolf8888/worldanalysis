(() => {
  'use strict'
  // Install only in an operator-selected page. This never clicks, sends, fetches,
  // reads headers/cookies, inspects query parameters or replaces a response.
  const key = 'WAInventoryObserver'
  const kind = 'wa-passive-inventory-observer-v1'
  if (window[key] && window[key].kind === kind) return window[key].read()
  const prototype = window.XMLHttpRequest.prototype
  const originalOpen = prototype.open
  const pending = new WeakMap()
  const blockedScopes = new Set()
  const usedGenerations = new Set()
  let enabled = true
  let epoch = 0
  let armed = null
  let matchedRequests = 0
  let receipts = []
  let overflow = false
  const own = (object, name) => {
    if (!object || typeof object !== 'object') return undefined
    const descriptor = Object.getOwnPropertyDescriptor(object, name)
    return descriptor && Object.hasOwn(descriptor, 'value') ? descriptor.value : undefined
  }
  const opaque = value => {
    if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value)) throw Error('OPAQUE_REFERENCE_REQUIRED')
    return value
  }
  const path = value => {
    if (typeof value !== 'string' || !value.startsWith('/') || value.length > 4096 || value !== value.normalize('NFC') || /[\\%?#:\x00-\x1f\x7f]/.test(value)) throw Error('CANONICAL_FOLDER_REQUIRED')
    if (value.length > 1 && value.endsWith('/')) value = value.slice(0, -1)
    if (value !== '/' && value.slice(1).split('/').some(part => !part || part === '.' || part === '..')) throw Error('CANONICAL_FOLDER_REQUIRED')
    return value
  }
  const parent = value => value.slice(0, value.lastIndexOf('/')) || '/'
  const fileId = value => {
    if (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0) return String(value)
    if (typeof value === 'string' && /^(0|[1-9][0-9]{0,31})$/.test(value)) return value
    throw Error('INVALID_FILE_ID')
  }
  const integer = value => Number.isSafeInteger(value) && value >= 0
  const cursor = value => typeof value === 'string' && /^[A-Za-z0-9_-]{0,256}$/.test(value)
  const flag = value => typeof value === 'boolean' ? value : value === 0 ? false : value === 1 ? true : undefined
  function normalizePagination(body, issues) {
    const result = {}
    for (const [output, names] of Object.entries({ page: ['page', 'page_num', 'current_page'], page_size: ['page_size', 'num'], total: ['total', 'total_count'], has_more: ['has_more', 'hasmore'], cursor: ['cursor'], next_cursor: ['next_cursor'] })) {
      let found = false
      for (const name of names) {
        const raw = own(body, name)
        if (raw === undefined) continue
        const value = output === 'has_more' ? flag(raw) : (output === 'cursor' || output === 'next_cursor') ? (cursor(raw) ? raw : undefined) : (integer(raw) && (output === 'total' || raw > 0) ? raw : undefined)
        if (value === undefined || (found && result[output] !== value)) { issues.push('INVALID_PAGINATION'); continue }
        result[output] = value
        found = true
      }
    }
    return result
  }
  function summarize(values) {
    const issues = [...new Set(values.flatMap(receipt => receipt.issues))]
    const list = []
    const seenIds = new Set(), seenPaths = new Set(), pages = new Map(), cursors = new Map()
    for (const receipt of values) {
      for (const item of receipt.list) {
        const folded = item.path.toLowerCase()
        if (seenIds.has(item.fs_id)) issues.push('DUPLICATE_FILE_ID')
        if (seenPaths.has(folded)) issues.push('DUPLICATE_PATH')
        seenIds.add(item.fs_id); seenPaths.add(folded); list.push(item)
      }
      const p = receipt.pagination
      if (p.has_more === false && p.next_cursor) issues.push('PAGINATION_CONFLICT')
      if (Object.hasOwn(p, 'page')) {
        if (pages.has(p.page)) issues.push('DUPLICATE_PAGE')
        pages.set(p.page, receipt)
      } else if (Object.hasOwn(p, 'cursor')) {
        if (cursors.has(p.cursor)) issues.push('DUPLICATE_CURSOR')
        cursors.set(p.cursor, receipt)
      }
    }
    let paginationComplete = false
    if (values.length && values.every(r => r.folder_matches === true && r.state === 'observed')) {
      if (pages.size === values.length) {
        const ordered = [...pages.entries()].sort((a, b) => a[0] - b[0])
        paginationComplete = ordered.every(([number, receipt], index) => number === index + 1 && receipt.pagination.has_more === (index < ordered.length - 1))
      } else if (cursors.size === values.length && cursors.has('')) {
        let current = '', visited = new Set()
        while (cursors.has(current) && !visited.has(current)) {
          visited.add(current)
          const p = cursors.get(current).pagination
          if (p.has_more === false) { paginationComplete = !p.next_cursor && visited.size === cursors.size; break }
          if (p.has_more !== true || !p.next_cursor) break
          current = p.next_cursor
        }
      }
    }
    const totals = values.filter(r => Object.hasOwn(r.pagination, 'total')).map(r => r.pagination.total)
    if (totals.some(total => total !== totals[0]) || (paginationComplete && totals.some(total => total !== list.length))) issues.push('TOTAL_CONFLICT')
    if (!paginationComplete) issues.push('PAGINATION_UNKNOWN_OR_INCOMPLETE')
    if (overflow) issues.push('RECEIPT_BUDGET_EXHAUSTED')
    const exactSizes = values.length > 0 && values.every(receipt => receipt.folder_matches === true && !receipt.issues.some(issue => ['INVALID_LIST', 'INVALID_LIST_ITEM', 'INVALID_RESPONSE', 'UNREADABLE_RESPONSE', 'CONFLICTING_FOLDER_EVIDENCE', 'INVALID_FOLDER_EVIDENCE'].includes(issue))) && list.every(item => integer(item.size))
    if (!exactSizes) issues.push('EXACT_SIZE_UNVERIFIED')
    const uniqueIssues = [...new Set(issues)]
    const blocked = values.some(r => r.state === 'blocked')
    const conflict = uniqueIssues.some(issue => /^(DUPLICATE_|TOTAL_CONFLICT|PAGINATION_CONFLICT)/.test(issue))
    return { state: blocked ? 'blocked' : conflict ? 'conflict' : uniqueIssues.length ? (list.length ? 'partial' : 'unknown') : 'complete', enumeration_complete: paginationComplete && !uniqueIssues.length, exact_sizes_verified: exactSizes, list, issues: uniqueIssues }
  }
  function project(xhr, context, receiptId, endpoint, evidenceRef) {
    const httpStatus = integer(xhr.status) ? xhr.status : 0
    const result = { schema: 'worldanalysis.browser-inventory-receipt.v1', evidence_class: context.evidence_class, request_generation_id: context.request_generation_id, receipt_id: receiptId, source_type: context.source_type, source_share_ref: context.source_share_ref, current_expected_folder: context.current_expected_folder, endpoint_path: endpoint, read_path_evidence_ref: evidenceRef, http_status: httpStatus, errno: null, state: 'unknown', folder_evidence: 'missing', folder_matches: null, owner_matches: null, list: [], pagination: {}, issues: [] }
    // A denial is retained even when its body is absent, HTML, malformed or unreadable.
    const deniedHttp = [113, 401, 403, 429].includes(httpStatus)
    if (deniedHttp) { result.state = 'blocked'; result.issues.push('HTTP_ACCESS_DENIED') }
    let body
    try {
      if (xhr.responseType === 'json') body = xhr.response
      else if (!xhr.responseType || xhr.responseType === 'text') {
        const text = xhr.responseText
        if (typeof text !== 'string' || text.length > 4 * 1024 * 1024) throw Error('BODY_BUDGET')
        body = JSON.parse(text)
      } else throw Error('UNSUPPORTED_RESPONSE_TYPE')
    } catch { result.issues.push('UNREADABLE_RESPONSE'); return result }
    if (!body || typeof body !== 'object' || Array.isArray(body)) { result.issues.push('INVALID_RESPONSE'); return result }
    const errno = own(body, 'errno')
    if (Number.isSafeInteger(errno)) result.errno = errno
    if ([113, 401, 403, 429].includes(errno)) { result.state = 'blocked'; result.issues.push('PROVIDER_ACCESS_DENIED') }
    if (['captcha', 'need_captcha', 'show_vcode', 'need_verify'].some(name => flag(own(body, name)) === true)) { result.state = 'blocked'; result.issues.push('CAPTCHA_REQUIRED') }
    if (result.state === 'blocked') return result
    if (httpStatus < 200 || httpStatus >= 300 || errno !== 0) { result.issues.push('NON_SUCCESS_RESPONSE'); return result }
    const data = own(body, 'data')
    const topList = own(body, 'list'), innerList = own(data, 'list')
    if (topList !== undefined && innerList !== undefined) { result.issues.push('AMBIGUOUS_LIST_CONTAINER'); return result }
    const container = topList !== undefined ? body : data
    const rawList = own(container, 'list')
    if (!Array.isArray(rawList) || rawList.length > 5000) { result.issues.push('INVALID_LIST'); return result }
    result.pagination = normalizePagination(container, result.issues)
    // Only compare non-secret owner identity locally. No raw UK is retained/exported.
    if (context.expected_owner_uk !== null) {
      const actual = own(body, 'owner_uk') ?? own(body, 'uk')
      try { if (actual !== undefined) result.owner_matches = fileId(actual) === context.expected_owner_uk } catch { result.issues.push('INVALID_OWNER_EVIDENCE') }
      if (result.owner_matches !== true) result.issues.push('OWNER_NOT_VERIFIED')
    }
    let metadataFolder = null
    for (const name of ['folder', 'path', 'dir', 'root']) {
      const value = own(container, name)
      if (value === undefined) continue
      try {
        const candidate = path(value)
        if (metadataFolder !== null && candidate !== metadataFolder) result.issues.push('CONFLICTING_FOLDER_EVIDENCE')
        metadataFolder = candidate
      } catch { result.issues.push('INVALID_FOLDER_EVIDENCE') }
    }
    const filtered = []
    for (const raw of rawList) {
      try {
        if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw Error('INVALID_ITEM')
        const fs_id = fileId(own(raw, 'fs_id'))
        const itemPath = path(own(raw, 'path'))
        const server_filename = own(raw, 'server_filename')
        const isdir = flag(own(raw, 'isdir'))
        if (typeof server_filename !== 'string' || !server_filename || server_filename !== itemPath.slice(itemPath.lastIndexOf('/') + 1) || isdir === undefined || itemPath === '/') throw Error('INVALID_ITEM')
        const size = own(raw, 'size')
        if (!integer(size)) result.issues.push('EXACT_SIZE_UNVERIFIED')
        filtered.push({ fs_id, path: itemPath, server_filename, size: integer(size) ? size : null, isdir: isdir ? 1 : 0 })
      } catch { result.issues.push('INVALID_LIST_ITEM') }
    }
    if (metadataFolder !== null) {
      result.folder_evidence = 'response_folder'
      result.folder_matches = metadataFolder === context.current_expected_folder
    } else if (filtered.length === rawList.length && filtered.length) {
      result.folder_evidence = 'list_parent_paths'
      result.folder_matches = filtered.every(item => parent(item.path) === context.current_expected_folder)
    }
    if (result.folder_matches === null) result.issues.push('MISSING_FOLDER_EVIDENCE')
    if (result.folder_matches === false || filtered.some(item => parent(item.path) !== context.current_expected_folder)) {
      result.folder_matches = false; result.folder_evidence = 'mismatch'; result.issues.push('FOLDER_MISMATCH')
    }
    // Never disclose rows from another folder, even if a late UI response is valid.
    if (result.folder_matches === true && !result.issues.includes('CONFLICTING_FOLDER_EVIDENCE') && !result.issues.includes('INVALID_FOLDER_EVIDENCE')) result.list = filtered
    result.issues = [...new Set(result.issues)]
    result.state = result.issues.length ? (result.list.length ? 'partial' : 'unknown') : 'observed'
    return result
  }
  function wrappedOpen(method, url) {
    const previous = pending.get(this)
    if (previous) previous()
    const context = armed, ownEpoch = epoch
    let endpoint = null, evidenceRef = null
    if (enabled && context && !blockedScopes.has(context.scopeKey) && typeof method === 'string' && method.toUpperCase() === 'GET' && typeof url === 'string') {
      try {
        // Strip query/fragment BEFORE URL construction. Never instantiate URLSearchParams,
        // read search/searchParams, coerce URL objects, retain a URL, or inspect query values.
        const clean = url.split(/[?#]/, 1)[0]
        const parsed = new URL(clean, context.origin)
        if (parsed.origin === context.origin && context.paths.has(parsed.pathname)) { endpoint = parsed.pathname; evidenceRef = context.paths.get(endpoint) }
      } catch { /* Unrelated requests are untouched and unrecorded. */ }
    }
    const result = Reflect.apply(originalOpen, this, arguments)
    if (!endpoint) return result
    matchedRequests++
    const receiptId = context.request_generation_id + '-' + matchedRequests
    const xhr = this
    let done = false
    const detach = () => {
      done = true
      xhr.removeEventListener('load', observe)
      if (pending.get(xhr) === detach) pending.delete(xhr)
    }
    const observe = () => {
      if (done) return
      detach()
      if (!enabled || ownEpoch !== epoch || armed !== context || blockedScopes.has(context.scopeKey)) return
      if (receipts.length >= 2048) { overflow = true; return }
      // Observer failures must never change the page's own callbacks/control flow.
      let receipt
      try { receipt = project(xhr, context, receiptId, endpoint, evidenceRef) } catch { receipt = { schema: 'worldanalysis.browser-inventory-receipt.v1', evidence_class: context.evidence_class, request_generation_id: context.request_generation_id, receipt_id: receiptId, source_type: context.source_type, source_share_ref: context.source_share_ref, current_expected_folder: context.current_expected_folder, endpoint_path: endpoint, read_path_evidence_ref: evidenceRef, http_status: 0, errno: null, state: 'unknown', folder_evidence: 'missing', folder_matches: null, owner_matches: null, list: [], pagination: {}, issues: ['UNREADABLE_RESPONSE'] } }
      receipts.push(receipt)
      if (receipt.state === 'blocked') blockedScopes.add(context.scopeKey)
    }
    pending.set(xhr, detach)
    xhr.addEventListener('load', observe)
    return result
  }
  const api = {
    kind,
    arm(options) {
      if (!enabled) throw Error('OBSERVER_RESTORED')
      const { current_expected_folder, source_share_ref, source_type, origin, request_generation_id, observed_read_paths, expected_owner_uk = null, evidence_class = 'normal_ui_passive' } = options
      const folder = path(current_expected_folder), ref = opaque(source_share_ref), generationId = opaque(request_generation_id)
      if (!['share', 'user_disk'].includes(source_type)) throw Error('INVALID_SOURCE_TYPE')
      if (!['synthetic', 'normal_ui_passive'].includes(evidence_class)) throw Error('INVALID_EVIDENCE_CLASS')
      if (origin !== window.location.origin || typeof origin !== 'string' || !/^https:\/\/[^/?#@]+$/.test(origin)) throw Error('SAME_ORIGIN_REQUIRED')
      if (usedGenerations.has(generationId)) throw Error('GENERATION_ID_REUSE')
      const scopeKey = source_type + ':' + ref
      if (blockedScopes.has(scopeKey)) throw Error('CHANNEL_BLOCKED_REQUIRES_OPERATOR_RECONCILIATION')
      const endpoint = source_type === 'share' ? '/share/list' : '/api/list'
      if (!Array.isArray(observed_read_paths) || observed_read_paths.length !== 1) throw Error('OBSERVED_PURE_READ_PATH_REQUIRED')
      const observed = observed_read_paths[0]
      if (observed.pathname !== endpoint || observed.pure_read !== true) throw Error('OBSERVED_PURE_READ_PATH_REQUIRED')
      const evidenceRef = opaque(observed.evidence_ref)
      const owner = expected_owner_uk === null ? null : fileId(expected_owner_uk)
      armed = { current_expected_folder: folder, source_share_ref: ref, source_type, origin, request_generation_id: generationId, paths: new Map([[endpoint, evidenceRef]]), expected_owner_uk: owner, evidence_class, scopeKey }
      usedGenerations.add(generationId); epoch++; receipts = []; matchedRequests = 0; overflow = false
      return this.read()
    },
    read() {
      const context = armed && { current_expected_folder: armed.current_expected_folder, source_share_ref: armed.source_share_ref, source_type: armed.source_type, request_generation_id: armed.request_generation_id, evidence_class: armed.evidence_class }
      return JSON.parse(JSON.stringify({ schema: 'worldanalysis.browser-inventory-observation.v1', active: enabled, context, matched_requests: matchedRequests, receipts, inventory: summarize(receipts) }))
    },
    restore() {
      enabled = false; epoch++; armed = null
      if (prototype.open === wrappedOpen) prototype.open = originalOpen
      if (window[key] === api) delete window[key]
      return { restored: true, own_wrapper_removed: prototype.open === originalOpen }
    },
  }
  prototype.open = wrappedOpen
  window[key] = api
  return api.read()
})()
