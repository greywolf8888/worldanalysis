"""Project verified DSH handoffs into the catalog without operating the queue."""

from collections import Counter, defaultdict
import json
from pathlib import Path
import runpy
from threading import Lock
from time import monotonic

from workspace_paths import INPUT, REGISTRY, RESULTS, WORKTREE


HANDOFF_PATH = Path('_tools/course_production/handoff_order.py')
SHARED_INTAKE_FIELDS = ('queue_item_id', 'resource_version_id', 'source_id',
                        'source_revision', 'range_manifest_ref')
CATALOG_LIMIT = 8
CACHE_SECONDS = 25
MAX_RECORD_BYTES = 32 * 1024 * 1024


def exact_file(workspace, relative):
    path = workspace / relative
    if not path.is_relative_to(workspace) or path.resolve(strict=True) != path or not path.is_file():
        raise ValueError('正式记录不可读')
    return path


def active_handoff(workspace):
    for relative in (WORKTREE / HANDOFF_PATH, HANDOFF_PATH):
        try:
            path = exact_file(workspace, relative)
        except FileNotFoundError:
            continue
        return runpy.run_path(str(path))
    return {}


def registered_projects(workspace, registry, read_json, requested):
    records = defaultdict(list)
    for binding in registry['bindings'].values():
        try:
            manifest = Path(binding['manifest'])
            if not manifest.is_relative_to(workspace / RESULTS):
                continue
            queue = read_json(exact_file(workspace, manifest.relative_to(workspace)))
            if queue['source'] != binding['source'] or queue['vault'] != binding['vault']:
                continue
            for row in queue['projects']:
                if row['id'] in requested:
                    records[row['id']].append((Path(queue['source']) / row['name'], row['metadata']))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return records


def verified_bindings(workspace, registry, handoff):
    state = registry.get('v4_handoff', {})
    plans, intakes = state.get('plans', {}), state.get('intakes', {})
    if not plans or not intakes:
        return {}
    read_json, workspace_file = handoff['read_json'], handoff['workspace_file']
    registered = registered_projects(workspace, registry, read_json,
                                     {entry['local_project_id'] for entry in intakes.values()})
    blocked = state.get('blocked', {})
    held = frozenset(state.get('held', ()))
    candidates = defaultdict(list)
    by_project = Counter()
    for batch_id, plan in plans.items():
        try:
            if not plan.get('confirmation_task_ref'):
                continue
            filename = workspace_file(workspace, f'_exchange/dsh/plans/{batch_id}.json')
            if read_json(filename) != plan:
                continue
            handoff['check_confirmation'](workspace, plan)
            if plan['batch_id'] != batch_id:
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for item in plan['items']:
            item_id = item['queue_item_id']
            intake = intakes.get(item_id)
            if not intake or item_id in blocked or not item.get('article_revision'):
                continue
            try:
                project = intake['local_project_id']
                if (project in held or len(registered[project]) != 1 or intake['batch_id'] != batch_id
                        or intake['accepted_order_revision'] != plan['order_revision']
                        or any(intake[field] != item[field] for field in SHARED_INTAKE_FIELDS)):
                    continue
                filename = workspace_file(workspace, f'_exchange/codex/intake/{item_id}.json')
                if read_json(filename) != intake:
                    continue
                handoff['validate_intake'](intake)
                folder, files = registered[project][0]
                if (workspace_file(workspace, intake['source_folder']) != folder
                        or files != intake['files']):
                    continue
                scope, shapes = handoff['load_range'](workspace, item, local_shapes=True, require_dsh=True)
                received = handoff['files_by_path'](files, local=True)
                matching = [shape for shape in shapes if received and all(shape.get(name) == size
                            for name, size in received.items())]
                if (scope != 'recursive' or len(matching) != 1
                        or (received == matching[0]) != intake['download_complete']):
                    continue
                ref = item['range_manifest_ref']
                manifest = read_json(workspace_file(workspace, ref['path']), ref)
                keys = tuple(handoff['priority_article_keys'](item, manifest))
            except (OSError, ValueError, KeyError, TypeError):
                continue
            by_project[project] += 1
            link = {'local_project_id': project, 'intake_download_complete': intake['download_complete']}
            for key in keys:
                candidates[key].append(link)
    return {key: links[0] for key, links in candidates.items()
            if len(links) == 1 and by_project[links[0]['local_project_id']] == 1}


class CourseFollowups:
    """A short-lived, read-only view of committed local intake identities."""

    def __init__(self, workspace):
        self.workspace = Path(workspace)
        self.lock = Lock()
        self.stamp = None
        self.checked_at = 0
        self.index = {}
        self.analysis_readback = None

    def read(self):
        with self.lock:
            try:
                path = exact_file(self.workspace, REGISTRY)
                stat = path.stat()
                stamp = stat.st_mtime_ns, stat.st_size, stat.st_ino
                now = monotonic()
                if self.stamp == stamp and now - self.checked_at < CACHE_SECONDS:
                    return self.index
                if stat.st_size > MAX_RECORD_BYTES:
                    raise ValueError('正式记录过大')
                with path.open('rb') as stream:
                    raw = stream.read(MAX_RECORD_BYTES + 1)
                if len(raw) > MAX_RECORD_BYTES:
                    raise ValueError('正式记录过大')
                registry = json.loads(raw)
                source = self.workspace / INPUT
                if registry.get('schema') != 1 or Path(registry['root']) != source:
                    raise ValueError('正式记录属于不同的项目')
                state = registry.get('v4_handoff', {})
                handoff = active_handoff(self.workspace) if state.get('plans') and state.get('intakes') else {}
                index = verified_bindings(self.workspace, registry, handoff) if handoff else {}
                readback = registry.get('analysis_readback')
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                stamp, now, index = None, monotonic(), {}
                readback = None
            self.stamp, self.checked_at = stamp, now
            self.analysis_readback = readback
            if index != self.index:
                self.index = index
            return self.index

    def waiting_for(self, submitted):
        self.read()
        with self.lock:
            data = self.analysis_readback
            if (not submitted or not isinstance(data, dict) or data.get('schema_version') != 1
                    or (data.get('event_id'), data.get('revision')) != (submitted['event_id'], submitted['revision'])):
                return set()
            waiting = data.get('waiting')
            if not isinstance(waiting, list) or len(waiting) > len(submitted['entries']):
                return set()
            ranks = {item['key']: rank for rank, item in enumerate(submitted['entries'], 1)}
            return {item['key'] for item in waiting if isinstance(item, dict)
                    and isinstance(item.get('key'), str) and ranks.get(item['key']) == item.get('rank')}


def binding_for(index, row):
    if row.get('action_pending') or row.get('demo'):
        return None
    return index.get(tuple(row.get(key) for key in ('id', 'site_id', 'article_id', 'article_revision')))


def related_followups(detail):
    """The catalog relation can suggest where to look for sources, without admitting materials."""
    candidates = []
    for field, kind in (('other_sources', 'same_edition_source'), ('other_versions', 'different_edition')):
        for other in detail.get(field, ()):
            if other.get('action_pending'):
                continue
            candidates.append({key: other[key] for key in ('id', 'article_key', 'article_revision', 'title') if key in other}
                              | {'relation': kind})
            if len(candidates) == CATALOG_LIMIT:
                return candidates
    return candidates
