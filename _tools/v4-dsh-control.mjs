import { execFile, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { lstat, mkdir, open, readFile, realpath, unlink } from 'node:fs/promises';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const ROOT = fileURLToPath(new URL('..', import.meta.url)).replace(/[\\/]$/, '');
const TASKS = path.join(ROOT, '_exchange', 'control', 'tasks');
const RECEIPTS = path.join(ROOT, '_exchange', 'dsh', 'receipts');
const RUNTIME = path.join(ROOT, '.runtime');
const DISPATCHES = path.join(RUNTIME, 'v4-dsh-control-dispatches');
const DEDICATED = path.join(RUNTIME, 'dsh-worldanalysis');
const PROXY = path.join(DEDICATED, 'plugins', 'dsh-mcp-bridge', 'bin', 'dsh-mcp-stdio.mjs');
const DEDICATED_DISCOVERY = path.join(DEDICATED, 'desktop-bridge');
const DEDICATED_BINDING = path.join(DEDICATED, 'desktop-session-readback.json');
const DISPATCH_ROUTE = path.join(DEDICATED, 'transfer-route.json');
const UUID = '[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}';
const TASK_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$/;
const SERVER_INFO = { name: 'worldanalysis-dsh-control', version: '4.0.0' };
const PROTOCOLS = new Set(['2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25']);

function schema(properties = {}, required = []) {
  return { type: 'object', properties, required, additionalProperties: false };
}

const sessionArg = { type: 'string', pattern: `^session-${UUID}$` };
const TOOLS = [
  {
    name: 'dsh_list_workspaces',
    description: 'Verify the existing Worldanalysis workspace on the live desktop Host.',
    inputSchema: schema(),
  },
  {
    name: 'dsh_list_sessions',
    description: 'List the registered dedicated Worldanalysis session.',
    inputSchema: schema(),
  },
  {
    name: 'dsh_get_session',
    description: 'Get bounded session identity, permissions and activity without chat content.',
    inputSchema: schema({ sessionId: sessionArg }, ['sessionId']),
  },
  {
    name: 'dsh_get_dedicated_session',
    description: 'Read the verified Worldanalysis dedicated desktop session without sending a message or accessing Baidu.',
    inputSchema: schema(),
  },
  {
    name: 'dsh_send_message',
    description: 'Dispatch one already approved and enabled handoff to the registered dedicated Worldanalysis session.',
    inputSchema: schema({
      sessionId: sessionArg,
      taskId: { type: 'string', pattern: TASK_ID.source },
      taskPath: { type: 'string', description: 'Canonical project-relative task path or its exact current absolute path.' },
    }, ['sessionId', 'taskId', 'taskPath']),
  },
  {
    name: 'dsh_send_dedicated_readback',
    description: 'Notify the verified dedicated desktop about one persisted read-only task. No transfer, download, or retry.',
    inputSchema: schema({ taskId: { type: 'string', pattern: TASK_ID.source } }, ['taskId']),
  },
  {
    name: 'dsh_wait_session',
    description: 'Wait briefly for an existing UI-interaction-mode session. Read business results from the shared receipt.',
    inputSchema: schema({
      sessionId: sessionArg,
      afterSeq: { type: 'integer', minimum: 0 },
      timeoutMs: { type: 'integer', minimum: 1000, maximum: 15000 },
    }, ['sessionId']),
  },
];
const TOOL_NAMES = new Set(TOOLS.map(({ name }) => name));

class ControlError extends Error {}

function samePath(left, right) {
  return typeof left === 'string'
    && left.replaceAll('\\', '/').toLowerCase() === right.replaceAll('\\', '/').toLowerCase();
}

function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function onlyKeys(args, allowed) {
  if (!object(args) || Object.keys(args).some((key) => !allowed.includes(key))) {
    throw new ControlError('Invalid tool arguments.');
  }
}

function boundedInteger(value, min = 0, max = Number.MAX_SAFE_INTEGER) {
  return Number.isSafeInteger(value) && value >= min && value <= max;
}

function count(value) {
  return Array.isArray(value) ? value.length : null;
}

async function verifiedDiscovery(home) {
  try {
    const location = path.join(home, 'mcp-bridge.json');
    await ensureExactFile(location, 'file');
    const entry = JSON.parse(await readFile(location, 'utf8'));
    const endpoint = new URL(entry.url);
    if (entry.running !== true || !boundedInteger(entry.pid, 1) || typeof entry.instanceId !== 'string'
      || entry.instanceId.length === 0 || endpoint.protocol !== 'http:' || endpoint.hostname !== '127.0.0.1'
      || !boundedInteger(Number(endpoint.port), 1, 65535) || endpoint.pathname !== '/mcp'
      || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) throw new Error();
    const health = await fetch(new URL('/healthz', endpoint), { signal: AbortSignal.timeout(1000), redirect: 'error' });
    const data = await health.json();
    if (!health.ok || data.pid !== entry.pid || data.instanceId !== entry.instanceId) throw new Error();
    return { home, endpoint: endpoint.href, key: JSON.stringify([endpoint.href, entry.pid, entry.instanceId]) };
  } catch {
    throw new ControlError('No active, verified DSH desktop bridge on localhost.');
  }
}

class ProxyClient {
  #child;
  #sequence = 0;
  #pending = new Map();
  ready;

  constructor({ home, endpoint }) {
    const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => {
      const name = key.toUpperCase();
      return name !== 'DSH_HOME' && name !== 'NODE_OPTIONS' && !name.startsWith('DSH_MCP_');
    }));
    env.DSH_HOME = home;
    // All retries are explicit caller actions. The audited proxy replays
    // certain transport failures only inside its bounded resend window.
    env.DSH_MCP_OFFLINE_GRACE_MS = '0';
    this.#child = spawn(process.execPath, [PROXY, '--url', endpoint], {
      cwd: ROOT,
      env,
      stdio: ['pipe', 'pipe', 'ignore'],
      windowsHide: true,
    });
    const incoming = createInterface({ input: this.#child.stdout, crlfDelay: Infinity });
    incoming.on('line', (line) => {
      let frame;
      try { frame = JSON.parse(line); } catch { return; }
      const pending = this.#pending.get(frame?.id);
      if (!pending) return;
      const failure = frame.error ? new ControlError('DSH bridge could not complete the request.') : undefined;
      pending(failure, frame.result);
    });
    this.#child.on('error', () => this.#closed());
    this.#child.on('close', () => this.#closed());
    this.#child.stdin.on('error', () => this.#closed());
    this.ready = this.#initialize();
  }

  async #initialize() {
    await this.request('initialize', {
      protocolVersion: '2025-11-25', capabilities: {}, clientInfo: SERVER_INFO,
    });
    this.#write({ method: 'notifications/initialized' });
    // The upstream proxy answers initialize locally. With resend disabled,
    // positively observe the real tool catalog before forwarding any call.
    const deadline = Date.now() + 6000;
    do {
      const catalog = await this.request('tools/list', {}, 3000);
      if (catalog?.tools?.some(({ name }) => name === 'dsh_get_session')) return;
      await delay(125);
    } while (Date.now() < deadline);
    throw new ControlError('The verified DSH desktop bridge did not expose its live tool catalog.');
  }

  #closed() {
    for (const finish of this.#pending.values()) {
      finish(new ControlError('DSH bridge disconnected.'));
    }
  }

  #write(frame) {
    this.#child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', ...frame })}\n`);
  }

  request(method, params, timeoutMs = 65000) {
    return new Promise((resolve, reject) => {
      if (this.#child.stdin.destroyed || this.#child.exitCode !== null) {
        reject(new ControlError('DSH bridge disconnected.'));
        return;
      }
      const id = ++this.#sequence;
      const finish = (error, value) => {
        clearTimeout(timer);
        this.#pending.delete(id);
        if (error) reject(error);
        else resolve(value);
      };
      const timer = setTimeout(() => finish(new ControlError('DSH bridge response timed out.')), timeoutMs);
      this.#pending.set(id, finish);
      this.#write({ id, method, params });
    });
  }

  close() {
    this.#child.stdin.end();
  }

  stop() {
    this.#child.kill();
    this.#closed();
  }
}

const targets = new Map();

function forgetTarget(home, client = targets.get(home)?.client) {
  if (targets.get(home)?.client === client) targets.delete(home);
  client?.stop();
}

async function currentDiscovery(expectedKey, home = DEDICATED_DISCOVERY) {
  let discovery;
  try {
    discovery = await verifiedDiscovery(home);
  } catch (error) {
    forgetTarget(home);
    throw error;
  }
  if (targets.has(home) && targets.get(home).key !== discovery.key) forgetTarget(home);
  if (expectedKey !== undefined && discovery.key !== expectedKey) {
    throw new ControlError('DSH desktop bridge identity changed. Recheck the session before proceeding.');
  }
  return discovery;
}

async function upstream(name, args, expectedKey, home = DEDICATED_DISCOVERY) {
  const discovery = await currentDiscovery(expectedKey, home);
  if (!targets.has(home)) {
    try { await ensureExactFile(PROXY, 'file'); }
    catch { throw new ControlError('Prepare the approved project DSH bridge package before connecting.'); }
    targets.set(home, { key: discovery.key, client: new ProxyClient(discovery) });
  }
  const { client } = targets.get(home);
  let reply;
  try {
    await client.ready;
    await currentDiscovery(discovery.key, home);
    reply = await client.request('tools/call', { name, arguments: args });
    await currentDiscovery(discovery.key, home);
  } catch (error) {
    forgetTarget(home, client);
    throw error;
  }
  if (reply?.isError || !object(reply?.structuredContent)) {
    throw new ControlError('DSH desktop bridge rejected the call or returned an unsupported response.');
  }
  return reply.structuredContent;
}

async function dedicatedBinding() {
  await ensureExactFile(DEDICATED_BINDING, 'file');
  const row = JSON.parse(await readFile(DEDICATED_BINDING, 'utf8'));
  const rootBound = row.schema_version === 1 && samePath(row.project_path, ROOT)
    || row.schema_version === 2 && row.project_path === '.';
  if (!rootBound || row.purpose !== 'read_only_preflight'
    || !new RegExp(`^session-${UUID}$`).test(row.session_id) || !new RegExp(`^${UUID}$`).test(row.workspace_id)
    || typeof row.title !== 'string' || row.title.length < 1 || row.title.length > 128) {
    throw new ControlError('Dedicated desktop binding is invalid.');
  }
  return { sessionId: row.session_id, workspaceId: row.workspace_id, title: row.title, home: DEDICATED_DISCOVERY };
}

async function dispatchRoute() {
  let file;
  try {
    file = await ensureExactFile(DISPATCH_ROUTE, 'file');
  } catch (error) {
    if (error.code === 'ENOENT') return { mode: 'paused', revision: 0 };
    throw error;
  }
  if (file.size > 4096) throw new ControlError('Transfer routing record exceeded its size limit.');
  const route = JSON.parse(await readFile(DISPATCH_ROUTE, 'utf8'));
  if (!object(route) || route.schema_version !== 1 || !boundedInteger(route.revision, 1)
    || !['paused', 'dedicated'].includes(route.mode)) throw new ControlError('Invalid transfer routing record.');
  return route;
}

async function getSession(sessionId, expectedKey, binding) {
  const { workspaceId, title: boundTitle, home } = binding;
  if (!boundTitle || (binding.sessionId && binding.sessionId !== sessionId)) {
    throw new ControlError('Session outside the Worldanalysis binding.');
  }
  const row = await upstream('dsh_get_session', { sessionId }, expectedKey, home);
  const title = row.projections?.title;
  const rootDirectory = row.projections?.workingDirectory ?? row.cwd;
  if (row.sessionId !== sessionId || row.workspaceId !== workspaceId || !samePath(row.workspacePath, ROOT)
    || !samePath(rootDirectory, ROOT) || (row.cwd !== undefined && !samePath(row.cwd, ROOT))
    || title !== boundTitle) {
    throw new ControlError('The desktop Host did not verify the bound project session identity.');
  }
  return row;
}

function sessionSummary(row, binding) {
  const permissions = row.projections?.permissions;
  const preset = permissions?.preset ?? permissions?.currentValue;
  return {
    sessionId: row.sessionId,
    title: binding.title,
    workspaceId: binding.workspaceId,
    workspacePath: ROOT,
    live: row.live === true,
    interactionMode: row.interactionMode === 'ui' ? 'ui' : 'other',
    permissionPresetMatches: preset === 'danger-full-access',
    sandboxMatchesWhenReported: typeof permissions?.sandbox === 'string' ? permissions.sandbox === 'danger-full-access' : null,
    approvalMatchesWhenReported: typeof permissions?.approval === 'string' ? permissions.approval === 'never' : null,
    planMode: row.projections?.plan?.active === true,
    turnSettled: row.turnSettled === true,
    running: typeof row.state?.running === 'boolean' ? row.state.running : null,
    queueCount: count(row.state?.queue),
    pendingInputCount: count(row.state?.pendingInput),
    pendingInteractionCount: count(row.pendingInteractions),
    desktopPendingCount: count(row.desktopPendingEvents),
    activeWorkflowCount: count(row.activeWorkflowRuns) ?? 0,
    cursor: boundedInteger(row.state?.cursor) ? row.state.cursor : null,
  };
}

async function ensureExactFile(location, kind) {
  const entry = await lstat(location);
  const correctType = kind === 'directory' ? entry.isDirectory() : entry.isFile();
  if (!correctType || entry.isSymbolicLink() || !samePath(await realpath(location), location)) {
    throw new ControlError('Expected a regular file or directory under the canonical Worldanalysis path.');
  }
  return entry;
}

async function loadTask({ taskId, taskPath, sessionId }, readOnly) {
  if (typeof taskId !== 'string' || !TASK_ID.test(taskId) || /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$/i.test(taskId)) {
    throw new ControlError('Invalid task ID.');
  }
  const location = path.join(TASKS, `${taskId}.json`);
  const relative = `_exchange/control/tasks/${taskId}.json`;
  if (taskPath !== relative && !samePath(taskPath, location)) throw new ControlError('Task must match its exact shared handoff path.');
  try {
    const file = await ensureExactFile(location, 'file');
    if (file.size > 256 * 1024) throw new Error();
    const bytes = await readFile(location);
    if (bytes.length !== file.size) throw new Error();
    const task = JSON.parse(bytes.toString('utf8'));
    if (task.schema_version !== 1 || task.record_type !== 'control_task' || task.producer !== 'codex-control'
      || task.plan_version !== 'V4' || task.task_id !== taskId || task.state !== 'ready' || task.demo === true) throw new Error();
    if (readOnly) {
      onlyKeys(task, ['schema_version', 'record_type', 'producer', 'plan_version', 'task_id', 'state', 'target_role',
        'receipt_path', 'execution_allowed', 'authorization_ref', 'inputs', 'record_id', 'revision', 'observed_at']);
      onlyKeys(task.inputs, ['kind', 'transfer_request_ref']);
      if (task.target_role !== 'dedicated_dsh' || task.execution_allowed !== false || task.authorization_ref !== null
        || (task.record_id !== undefined && task.record_id !== taskId)
        || (task.revision !== undefined && task.revision !== 1)
        || task.receipt_path !== `_exchange/dsh/receipts/${taskId}.json`
        || !['dedicated_transfer_readback', 'dedicated_local_readback'].includes(task.inputs.kind)) throw new Error();
      const ref = task.inputs.transfer_request_ref;
      if (ref !== undefined) {
        onlyKeys(ref, ['path', 'sha256']);
        if (task.inputs.kind !== 'dedicated_transfer_readback'
          || !/^_exchange\/control\/transfer-requests\/[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}\.json$/.test(ref.path)
          || !/^[0-9a-f]{64}$/.test(ref.sha256)) throw new Error();
        const requestPath = path.join(ROOT, ref.path);
        const request = await ensureExactFile(requestPath, 'file');
        if (request.size > 65536 || createHash('sha256').update(await readFile(requestPath)).digest('hex') !== ref.sha256) throw new Error();
      }
    } else if (task.target_session_id !== sessionId || task.execution_allowed !== true
        || task.target_role !== 'dedicated_dsh' || task.inputs?.kind !== 'confirmed_transfer'
        || !object(task.authorization_ref)) throw new Error();
    return { relative, sha256: createHash('sha256').update(bytes).digest('hex'), kind: task.inputs?.kind };
  } catch {
    throw new ControlError('The canonical task file is absent, changed or lacks a valid ready handoff for this session.');
  }
}

async function reserve(location, value) {
  const handle = await open(location, 'wx');
  try {
    await handle.writeFile(`${JSON.stringify(value)}\n`);
    await handle.sync();
  } finally {
    await handle.close();
  }
}

async function verifyTransferPreflight(taskId) {
  try {
    const python = process.env.WORLDANALYSIS_PYTHON || 'python';
    const { stdout } = await promisify(execFile)(python,
      ['-B', '-X', 'utf8', path.join(ROOT, '_tools', 'dsh_dedicated.py'), 'preflight', '--task-id', taskId],
      { cwd: ROOT, timeout: 15000, maxBuffer: 65536, windowsHide: true });
    const state = JSON.parse(stdout);
    if (state.claim_enabled === true && state.execution_gate === 'ready_for_bound_task') return;
  } catch {
    // A missing or failed preflight never admits a formal notification.
  }
  throw new ControlError('The exact transfer task is not ready for formal dispatch.');
}

async function dispatch(args, readOnly = false) {
  onlyKeys(args, readOnly ? ['taskId'] : ['sessionId', 'taskId', 'taskPath']);
  const route = await dispatchRoute();
  if (!readOnly && route.mode !== 'dedicated') throw new ControlError('Worldanalysis formal dispatch is paused.');
  const binding = await dedicatedBinding();
  if (readOnly) args = { ...args, sessionId: binding.sessionId, taskPath: `_exchange/control/tasks/${args.taskId}.json` };
  if (args.sessionId !== binding.sessionId) throw new ControlError('Dispatch is restricted to the active transfer session.');
  const task = await loadTask(args, readOnly);
  if (!readOnly && (route.session_id !== binding.sessionId
    || route.desktop_binding_ref?.path !== '.runtime/dsh-worldanalysis/desktop-session-readback.json'
    || route.desktop_binding_ref?.sha256 !== createHash('sha256').update(await readFile(DEDICATED_BINDING)).digest('hex')
    || route.task_ref?.path !== `_exchange/control/tasks/${args.taskId}.json` || route.task_ref?.sha256 !== task.sha256)) {
    throw new ControlError('Dedicated dispatch is restricted to the exact authorized task.');
  }
  if (!readOnly) {
    await verifyTransferPreflight(args.taskId);
    const ref = route.readback_ref;
    if (!ref || !/^_exchange\/dsh\/receipts\/[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.json$/.test(ref.path)) {
      throw new ControlError('Dedicated readback reference is invalid.');
    }
    const location = path.join(ROOT, ref.path);
    const receipt = await ensureExactFile(location, 'file');
    if (receipt.size > 65536 || createHash('sha256').update(await readFile(location)).digest('hex') !== ref.sha256) {
      throw new ControlError('Dedicated readback changed. Verify the account again.');
    }
  }
  try {
    await lstat(path.join(RECEIPTS, `${args.taskId}.json`));
    throw new ControlError('A receipt already exists for this task. Read it without sending again.');
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  await ensureExactFile(RUNTIME, 'directory');
  await mkdir(DISPATCHES, { recursive: true });
  await ensureExactFile(DISPATCHES, 'directory');
  const record = { taskId: args.taskId, sha256: task.sha256, sessionId: binding.sessionId,
    ...(readOnly ? { kind: task.kind } : {}), at: new Date().toISOString() };
  const lock = path.join(DISPATCHES, '.active-dispatch.json');
  const attempted = path.join(DISPATCHES, `${args.taskId}.attempt.json`);
  try {
    await reserve(lock, record);
  } catch (error) {
    if (error.code === 'EEXIST') throw new ControlError('Another dispatch is active or uncertain. Reconcile it before sending.');
    throw error;
  }

  let ambiguous = false;
  try {
    const current = await dispatchRoute();
    if (JSON.stringify(current) !== JSON.stringify(route)) throw new ControlError('Transfer routing changed; recheck before dispatch.');
    const { key } = await currentDiscovery(undefined, binding.home);
    const before = sessionSummary(await getSession(binding.sessionId, key, binding), binding);
    if (!before.turnSettled || before.running !== false || (before.queueCount ?? 0) > 0
      || (before.pendingInputCount ?? 0) > 0 || before.pendingInteractionCount !== 0
      || (before.desktopPendingCount ?? 0) > 0 || before.activeWorkflowCount > 0 || before.planMode
      || before.interactionMode !== 'ui' || !before.permissionPresetMatches || before.sandboxMatchesWhenReported === false
      || before.approvalMatchesWhenReported === false || before.cursor === null) {
      throw new ControlError('Dispatch requires an idle main session, desktop UI approvals and its original permission preset.');
    }
    if ((await loadTask(args, readOnly)).sha256 !== task.sha256) throw new ControlError('The shared task changed during verification.');
    try {
      await reserve(attempted, { ...record, cursorBefore: before.cursor });
    } catch (error) {
      if (error.code === 'EEXIST') throw new ControlError('This task ID already has a dispatch attempt. Read its receipt and reconcile before any new task.');
      throw error;
    }
    ambiguous = true;
    const receipt = await upstream('dsh_send_message', {
      sessionId: binding.sessionId,
      text: (readOnly ? [
        `Worldanalysis 专用 DSH 只读交接 ${args.taskId}`,
        `请在当前项目目录读取 ${task.relative}，核对 SHA-256 ${task.sha256}。`,
        task.kind === 'dedicated_local_readback'
          ? '只核对当前实际项目目录、会话及文件身份，不访问网站或网盘。'
          : '只在同一受权普通界面核对当前账号、现行目标目录和空间。平台要求登录、验证或显示暂停时保留现场并记录原因，不操作认证、不转存、不下载、不清理。',
        '若只读任务带有转存申请引用，先核对申请文件指纹，沿申请顺序读回对应份数、递归文件、精确字节和已有同名占位。只写实际可证的范围文件。在 result.dedicated_transfer_readback 中保留同值的 transfer_request_ref 和按申请顺序排列的 range_manifests（每项为相对路径与 SHA-256 引用）。该申请没有执行授权。',
        '检查同名原回执，已写则只读。在 _exchange/dsh/receipts 下准备同任务名的一份实际回执，记录 session_id、当前 cwd、task_sha256 和已核实内容。不得添加权限或把未核实内容写为通过。',
      ] : [
        `Worldanalysis V4 总控交接任务 ${args.taskId}`,
        `请在当前项目目录读取 ${task.relative}，并核验其 SHA-256 为 ${task.sha256}。`,
        '仅按已绑定的任务范围、既有权限和明确授权执行。按 task_id 核对既有回执与实际状态，已执行则只返回证据，禁止重复执行。',
        ...(route.mode === 'dedicated' && task.kind === 'confirmed_transfer'
          ? ['按专用 DSH 工作流复用 _ingest/wangzjk-to-pan/fast-agent.mjs 的 publishConfirmedSelection 和 runConfirmedPlan，由其核对用户授权、范围、目标、当前会话及原协调锁。门禁失败时存入真实阻塞，保留原回执与暂停。'] : []),
        ...(route.mode === 'dedicated' ? ['获当次下载授权后，使用 _tools/dsh_dedicated.py preflight --task-id 读回的 handoff.intake.root 作为固定收件目录。逐项目登记真实文件后再交接。'] : []),
        '请在共享协议规定的 DSH 回执目录写入与此 task_id 匹配的真实回执。遇到需用户决定或授权的事项请在现有桌面界面提出。',
      ]).join('\n'),
      mode: 'queue', wait: false, clientTimeZone: 'Asia/Singapore',
    }, key, binding.home);
    if (receipt.accepted !== true || receipt.sessionId !== binding.sessionId
      || typeof receipt.requestId !== 'string' || !/^[0-9a-f-]{36}$/i.test(receipt.requestId)) {
      throw new Error();
    }
    const result = { accepted: true, taskId: args.taskId, sha256: task.sha256, sessionId: binding.sessionId,
      requestId: receipt.requestId, cursorBefore: boundedInteger(receipt.cursorBefore) ? receipt.cursorBefore : before.cursor,
      turnSettled: false };
    await reserve(path.join(DISPATCHES, `${args.taskId}.accepted.json`), { ...record, ...result, at: new Date().toISOString() });
    ambiguous = false;
    return result;
  } catch (error) {
    if (ambiguous) throw new ControlError('Dispatch outcome is unknown. Reconcile the live session and shared receipt before any retry.');
    throw error;
  } finally {
    if (!ambiguous) await unlink(lock);
  }
}

async function callTool(name, args = {}) {
  if (name === 'dsh_send_message') return dispatch(args);
  if (name === 'dsh_send_dedicated_readback') return dispatch(args, true);
  if (name === 'dsh_list_workspaces') {
    onlyKeys(args, []);
    const binding = await dedicatedBinding();
    const reply = await upstream(name, {});
    const row = reply.workspaces?.find(({ id, path: workspacePath }) => id === binding.workspaceId && samePath(workspacePath, ROOT));
    if (!row || !row.sessionIds?.includes(binding.sessionId)) throw new ControlError('The registered dedicated Worldanalysis workspace was not verified.');
    return { workspaces: [{ id: binding.workspaceId, path: ROOT, title: 'Worldanalysis',
      status: row.status === 'ok' ? 'ok' : 'unverified', sessionIds: [binding.sessionId] }] };
  }
  if (name === 'dsh_list_sessions') {
    onlyKeys(args, []);
    const binding = await dedicatedBinding();
    const row = await getSession(binding.sessionId, undefined, binding);
    return { sessions: [{ sessionId: binding.sessionId, running: row.state?.running === true,
      workspaceId: binding.workspaceId, cwd: ROOT }] };
  }
  if (name === 'dsh_get_session') {
    onlyKeys(args, ['sessionId']);
    const binding = await dedicatedBinding();
    return sessionSummary(await getSession(args.sessionId, undefined, binding), binding);
  }
  if (name === 'dsh_get_dedicated_session') {
    onlyKeys(args, []);
    const binding = await dedicatedBinding();
    return sessionSummary(await getSession(binding.sessionId, undefined, binding), binding);
  }
  if (name === 'dsh_wait_session') {
    onlyKeys(args, ['sessionId', 'afterSeq', 'timeoutMs']);
    const { sessionId, afterSeq, timeoutMs = 5000 } = args;
    if ((afterSeq !== undefined && !boundedInteger(afterSeq)) || !boundedInteger(timeoutMs, 1000, 15000)) {
      throw new ControlError('Invalid cursor or wait duration.');
    }
    const binding = await dedicatedBinding();
    const { key } = await currentDiscovery(undefined, binding.home);
    const row = await getSession(sessionId, key, binding);
    if (row.interactionMode !== 'ui') throw new ControlError('Polling requires desktop UI interaction mode.');
    const reply = await upstream(name, { sessionId, afterSeq, timeoutMs, maxMessages: 1 }, key, binding.home);
    if (reply.sessionId !== sessionId) throw new ControlError('Wait returned an unexpected session identity.');
    const reason = ['idle', 'input-required', 'timeout'].includes(reply.reason) ? reply.reason : 'unverified';
    return { sessionId, reason, turnSettled: reply.turnSettled === true,
      cursor: boundedInteger(reply.cursor) ? reply.cursor : null,
      activeWorkflowCount: count(reply.activeWorkflowRuns) ?? 0 };
  }
  throw new ControlError('Tool outside the Worldanalysis control scope.');
}

function toolResult(value, isError = false) {
  return { content: [{ type: 'text', text: JSON.stringify(value) }], structuredContent: value, isError };
}

async function currentToolSchema() {
  let binding;
  try { binding = await dedicatedBinding(); }
  catch (error) {
    if (error.code === 'ENOENT') return TOOLS;
    throw error;
  }
  const tools = structuredClone(TOOLS);
  for (const tool of tools) {
    if (!tool.inputSchema.properties?.sessionId) continue;
    tool.inputSchema.properties.sessionId = tool.name === 'dsh_send_message'
      ? { type: 'string', const: binding.sessionId } : { type: 'string', enum: [binding.sessionId] };
  }
  return tools;
}

async function handle(frame) {
  if (!object(frame) || frame.jsonrpc !== '2.0' || !('id' in frame)) return;
  const { id, method, params } = frame;
  const response = { jsonrpc: '2.0', id };
  if (method === 'initialize') {
    const requested = params?.protocolVersion;
    response.result = {
      protocolVersion: PROTOCOLS.has(requested) ? requested : '2025-11-25',
      capabilities: { tools: {} }, serverInfo: SERVER_INFO,
    };
  } else if (method === 'ping') {
    response.result = {};
  } else if (method === 'tools/list') {
    try {
      response.result = { tools: await currentToolSchema() };
    } catch {
      response.error = { code: -32001, message: 'Worldanalysis transfer routing could not be verified.' };
    }
  } else if (method === 'tools/call' && TOOL_NAMES.has(params?.name)) {
    try {
      response.result = toolResult(await callTool(params.name, params.arguments));
    } catch (error) {
      response.result = toolResult({ error: error instanceof ControlError ? error.message : 'Worldanalysis control operation failed.' }, true);
    }
  } else {
    response.error = { code: -32601, message: 'Method outside the Worldanalysis control scope.' };
  }
  process.stdout.write(`${JSON.stringify(response)}\n`);
}

const incoming = createInterface({ input: process.stdin, crlfDelay: Infinity });
const active = new Set();
incoming.on('line', (line) => {
  let frame;
  try { frame = JSON.parse(line); } catch { return; }
  const handling = handle(frame);
  active.add(handling);
  void handling.finally(() => active.delete(handling));
});
incoming.on('close', async () => {
  await Promise.allSettled(active);
  for (const target of targets.values()) target.client.close();
});
