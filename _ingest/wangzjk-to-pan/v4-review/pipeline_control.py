"""Local operator status and the same fixed jobs for the website and CLI."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timedelta, timezone
import errno
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from time import monotonic, sleep
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
from uuid import uuid4

from catalog_model import ROOT, now
from collect_public import STATE, collect, collector_lock
from confirm_gate import readiness
from dsh_health import BRIDGE, probe_dsh
from member_queue import (
    DEFAULT_NON_PROJECTS, DEFAULT_OBSERVATIONS, build_worklist, read_non_projects, read_observations,
)

JOBS = {'collect': '采集公开目录', 'verify': '检查全链路', 'check_dsh': '检查 DSH 连接'}
CATALOG_RUNS = {'catalog_export': '导出简介识别', 'catalog_import': '保存简介识别', 'catalog_resume': '恢复简介任务'}
RUN_ID = re.compile(r'[a-f0-9]{32}\Z')
DB_PATH = ROOT / 'data' / 'pipeline-runs.sqlite3'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


LOCAL_HTTP = build_opener(ProxyHandler({}), NoRedirect())


class RunBusy(Exception):
    pass


def locked_elsewhere(error):
    return error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK)


class RunStore:
    def __init__(self, path):
        self.path = Path(path).absolute()

    @contextmanager
    def connect(self):
        if self.path.resolve() != self.path or self.path.is_symlink():
            raise ValueError('unbound run database')
        with closing(sqlite3.connect(self.path, timeout=3)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute('''CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, action TEXT NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, summary TEXT NOT NULL)''')
            connection.execute('''CREATE UNIQUE INDEX IF NOT EXISTS one_active_pipeline_run
                ON runs ((1)) WHERE state IN ('pending', 'running')''')
            yield connection

    def guard(self):
        path = self.path.with_name(self.path.stem + '.worker')
        if path.with_suffix('.lock').resolve() != path.with_suffix('.lock'):
            raise ValueError('unbound worker lock')
        return collector_lock(path)

    def recover(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat(timespec='milliseconds')
        try:
            with self.guard(), self.connect() as connection:
                connection.execute('''UPDATE runs SET state = 'failed', finished_at = ?, summary = ?
                    WHERE state = 'running' OR (state = 'pending' AND created_at < ?)''',
                    (now(), '上一次运行中断。请先查看当前进度，再决定是否重试。', cutoff))
        except OSError as error:
            if not locked_elsewhere(error):
                raise

    def reserve(self, action):
        if action not in JOBS:
            raise ValueError('unknown action')
        self.recover()
        key = uuid4().hex
        with self.connect() as connection:
            try:
                connection.execute('INSERT INTO runs VALUES (?, ?, ?, ?, NULL, NULL, ?)',
                                   (key, action, 'pending', now(), '等待启动'))
            except sqlite3.IntegrityError as error:
                raise RunBusy('已有操作正在运行，请稍后再试。') from error
        return key

    def claim(self, key):
        with self.connect() as connection:
            cursor = connection.execute('''UPDATE runs SET state = 'running', started_at = ?, summary = ?
                WHERE id = ? AND state = 'pending' ''', (now(), '正在处理', key))
            if cursor.rowcount != 1:
                return None
            return connection.execute('SELECT action FROM runs WHERE id = ?', (key,)).fetchone()['action']

    def finish(self, key, expected, state, summary):
        with self.connect() as connection:
            connection.execute('''UPDATE runs SET state = ?, finished_at = ?, summary = ?
                WHERE id = ? AND state = ?''', (state, now(), summary, key, expected))

    def record_catalog(self, action, summary, *, failed=False):
        if action not in CATALOG_RUNS or not isinstance(summary,str) or len(summary)>120:
            raise ValueError('invalid catalog run display')
        finished=now()
        with self.connect() as connection:
            connection.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?)',
                               (uuid4().hex,action,'failed' if failed else 'complete',finished,finished,finished,summary))

    def recent(self):
        if not self.path.exists():
            return []
        self.recover()
        with self.connect() as connection:
            labels={**JOBS,**CATALOG_RUNS}
            return [{**dict(row), 'label': labels[row['action']]} for row in connection.execute(
                'SELECT * FROM runs ORDER BY rowid DESC LIMIT 24') if row['action'] in labels]


def request_local(port, path):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('invalid local port')
    with LOCAL_HTTP.open(f'http://127.0.0.1:{port}/api/{path}', timeout=24) as response:
        data = response.read(12_000_001)
    if len(data) > 12_000_000:
        raise ValueError('local response too large')
    return json.loads(data)


def collect_catalog(port):
    try:
        with collector_lock(STATE):
            result = collect(STATE, 12, 4, ())
    except OSError as error:
        if locked_elsewhere(error):
            return 'waiting', '已有公开采集在进行，请稍后查看资料来源。'
        raise
    if result.get('state') != 'updated':
        if result.get('state') in {'official_publisher_gate', 'publisher_access_restricted'}:
            return 'blocked', '原站限制访问，采集进度已保留。'
        return 'failed', '原站目录读取未完成，请查看资料来源后再试。'
    try:
        live = request_local(port, 'meta')
        observed = live.get('discovery') or {}
        if live['public_checkpoint_pending_sync'] or observed.get('observed_at') != result.get('observed_at'):
            raise ValueError('checkpoint not yet read back')
    except (OSError, ValueError, KeyError):
        return 'waiting', '公开目录已保存，资源库读回待核实。'
    return 'complete', f"目录已入库，本次新增 {result['new_this_run']} 篇，补充图文 {result['details_added']} 篇。"


def verify_pipeline(port):
    meta = request_local(port, 'meta')
    linked = request_local(port, 'resources?baidu=1')
    local = request_local(port, 'local-status')
    draft = request_local(port, 'preconfirmation')
    order = request_local(port, 'analysis-priority')
    cleanup = request_local(port, 'cleanup-readiness')
    latest = request_local(port, 'meta')
    if meta.get('snapshot_id') != latest.get('snapshot_id') or latest.get('public_checkpoint_pending_sync'):
        return 'waiting', '目录正在更新，请稍后再次检查。'
    if (linked.get('total') != latest['totals']['baidu_linked_articles']
            or linked.get('snapshot_id') != latest['snapshot_id']
            or draft.get('snapshot_id') != latest['snapshot_id']
            or not isinstance(order.get('entries'), list)):
        return 'failed', '资源库、转存清单或分析顺序的读回结果不一致。'
    if local.get('sync_state') != 'live' or cleanup.get('sync_state') != 'live':
        return 'waiting', '本地进度或清理依据待同步，请稍后再次检查。'
    return 'complete', '已读回目录、百度入口、转存复核、本地进度、分析顺序和清理资格。'


def check_dsh(_port):
    return {
        'ready': ('complete', '已核实 DSH 原会话可接收交接。'),
        'busy': ('waiting', 'DSH 原会话正在处理，请稍后查看。'),
        'awaiting_user': ('waiting', 'DSH 原会话有待你处理的操作。'),
        'unavailable': ('blocked', '暂未核实 DSH 连接，请检查原会话。'),
    }[probe_dsh()['state']]


def worker(store, key, port):
    deadline = monotonic() + 4
    with ExitStack() as held:
        while True:
            try:
                held.enter_context(store.guard())
                break
            except OSError as error:
                if not locked_elsewhere(error) or monotonic() >= deadline:
                    raise
                sleep(0.05)
        action = store.claim(key)
        if action is None:
            return False
        try:
            state, summary = {'collect': collect_catalog, 'verify': verify_pipeline, 'check_dsh': check_dsh}[action](port)
        except (OSError, ValueError, TypeError, KeyError):
            state, summary = 'failed', '本次操作未能读回结果，请先核对当前进度。'
        store.finish(key, 'running', state, summary)
        return state == 'complete'


class PipelineControl:
    def __init__(self, port, *, store=None):
        self.port = port
        self.store = store if store is not None else RunStore(DB_PATH)

    def start(self, action):
        key = self.store.reserve(action)
        try:
            subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).absolute()),
                              '--port', str(self.port), '_worker', key], cwd=ROOT,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                             start_new_session=os.name != 'nt')
        except OSError:
            self.store.finish(key, 'pending', 'failed', '未能启动本机操作，请稍后重试。')
            raise
        return {'message': f'已提交{JOBS[action]}，可在运行记录查看进度。', 'run_id': key}

    def overview(self, lib, draft, local, pending, priority):
        runs = self.store.recent()
        active = next((row['action'] for row in runs if row['state'] in ('pending', 'running')), None)
        public = lib.discovery or {}
        member = build_worklist(lib, read_observations(DEFAULT_OBSERVATIONS),
                                non_projects=read_non_projects(DEFAULT_NON_PROJECTS), limit=1)['counts']
        progress = local.overview()
        cleanup = local.cleanup_preparation()
        preflight = readiness(lib, local, draft)
        ready = progress['sync_state'] == 'live'
        current = progress.get('current_production') or {}
        submitted = priority.get('submitted') or {}
        preservation = progress.get('local_preservation') or {}
        local_counts = progress.get('counts') or {}
        last_dsh_check = next((row for row in runs if row['action'] == 'check_dsh' and row['finished_at']), None)
        stages = [
            stage('collection', '公开采集', 'running' if active == 'collect' else 'waiting' if pending or not public.get('observed_at') else
                  'blocked' if public.get('stop_reason') else 'ready',
                  '公开目录正在更新。' if pending else '原站公开采集待核对。' if public.get('stop_reason') else
                  '从原站采集公开目录和图文。', public.get('observed_at'),
                  {'目录条目': lib.meta['totals']['articles']}, actions=[job_action('collect')] if not active else []),
            stage('member', '会员资料入口', 'waiting' if member['missing_direct_url'] else 'complete',
                  '在已登录的原站浏览器核对资料入口。', None,
                  {'已收录百度入口': member['known_direct_url'], '待补百度入口': member['missing_direct_url']},
                  links={'/member-import': '继续补录'}),
            stage('transfer', '选择与转存', 'blocked' if preflight['ordered_items'] else 'waiting',
                  '请按顺序确认所选项目，由 DSH 核对范围与网盘容量。',
                  last_dsh_check['finished_at'] if last_dsh_check else None,
                  {'清单项目': len(preflight['ordered_items']), '可确认': preflight['eligible_for_confirmation']},
                  links={'/?panel=transfer': '打开转存清单', '/?panel=confirmation': '转存复核'},
                  actions=[job_action('check_dsh')] if not active and BRIDGE.is_file() else []),
            stage('download', '本地下载', 'ready' if ready else 'waiting',
                  '按原流程手动下载，核对收件项目与实际文件。', progress.get('observed_at'),
                  {'本地登记项目': local_counts.get('registered_projects')} if ready else {},
                  links={'/?panel=progress': '查看处理进度'}),
            stage('analysis', '分析与独立验收', 'running' if ready and current else 'waiting',
                  '按已提交顺序交给正式分析，教材由独立测试验收。', current.get('updated_at'),
                  {'已提交顺位': len(submitted.get('entries', [])), '独立教材入口': local_counts.get('own_textbook_entries') if ready else None},
                  links={'/?panel=analysis': '安排分析顺序', '/?panel=progress': '查看分析与验收'}),
            stage('cleanup', '保全与清理', 'waiting', '核对教材可独立阅读，按已核实范围确认清理。',
                  cleanup.get('observed_at'), {'教材独立保全': preservation.get('installed_verified') if ready else None,
                  '新增网盘清理候选': cleanup.get('new_delete_candidates') if ready else None},
                  links={'/?panel=cleanup': '核对清理条件'}),
        ]
        return {'observed_at': now(), 'stages': stages, 'runs': runs, 'actions': [] if active else [job_action('verify')]}


def job_action(key):
    return {'id': key, 'label': JOBS[key]}


def stage(key, label, state, summary, at, metrics, *, links=None, actions=None):
    entries = [{'id': target, 'href': target, 'label': name} for target, name in (links or {}).items()]
    return {'id': key, 'label': label, 'state': state, 'summary': summary, 'observed_at': at,
            'metrics': [{'label': name, 'value': count} for name, count in metrics.items() if type(count) is int],
            'actions': entries + (actions or [])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('action', choices=['status', *JOBS, '_worker'])
    parser.add_argument('key', nargs='?')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or (args.action == '_worker') != (args.key is not None):
        parser.error('invalid local invocation')
    store = RunStore(DB_PATH)
    try:
        if args.action == 'status':
            print(json.dumps(request_local(args.port, 'control'), ensure_ascii=False, indent=2))
            return 0
        if args.action == '_worker':
            if not RUN_ID.fullmatch(args.key):
                parser.error('invalid run identity')
            return 0 if worker(store, args.key, args.port) else 2
        key = store.reserve(args.action)
        completed = worker(store, key, args.port)
        print(json.dumps(next(row for row in store.recent() if row['id'] == key), ensure_ascii=False))
        return 0 if completed else 2
    except (OSError, ValueError, RunBusy, sqlite3.Error) as error:
        print(str(error) if isinstance(error, RunBusy) else '本机操作未就绪。请检查资源库及项目运行目录。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
