"""Read-only transfer and analysis indicators for the resource library."""

import argparse
from collections import Counter, defaultdict
from pathlib import Path
import json
import re
from threading import Lock
from unicodedata import normalize
from urllib.parse import quote

from catalog_model import open_index
from catalog_relations import article_identity
from production_bridge import BASE, LocalProjection, exact_map, file_inside


FORMAL_VAULT = BASE / '分析目标下载地址' / '02_短视频与自媒体' / '分析结果'
_NUMBER = re.compile(r'^[(【\[]\s*\d{4,7}\s*期\s*[)】\]]\s*')
_FOLDER_PREFIX = re.compile(r'^(?:\d{3}-\d{8}|\d{4}-\d{2}-\d{2})[-_ ]+')
_PUBLISHER = re.compile(
    r'(?:\s*[【\[(]更多资源\s*(?:www\.)?abab9\.cn[】\])]|'
    r'[-_ ]+91创业网\s*(?:www\.)?abab9\.cn[-_ ]*收藏网址送项目)\s*$'
)
_AUTHORITATIVE_BOOK = {'registered_edition_matches', 'registered_independent_derivative_matches'}
_ACCEPTED = {'historical_same_edition', 'scope_accepted'}
_SCOPES = {'核心待补', '配套待补', '完整验收'}
_INCOMPLETE = {'queued', 'in_progress', 'awaiting_originals', 'archive_only'}


def title_key(title, *, folder=False):
    if not isinstance(title, str):
        return ''
    text = normalize('NFKC', title).casefold()
    if folder:
        text = _FOLDER_PREFIX.sub('', text)
    text = _PUBLISHER.sub('', _NUMBER.sub('', text))
    tokens = re.findall(r'\w+|(?<=\d)\.(?=\d)', text.replace('_', ' '))
    key = ''.join(tokens)
    return key if len(key) >= 10 else ''


def exact_analysis(project):
    """Trust only the batch state and the matching published edition."""
    analysis = project.get('analysis') or {}
    book = project.get('textbook') or {}
    review = project.get('acceptance') or {}
    state, acceptance = analysis.get('state'), review.get('state')
    if state in _INCOMPLETE:
        return {'analysis_state': state}
    if state == 'awaiting_re_review':
        return {'analysis_state': state} if book.get('state') in _AUTHORITATIVE_BOOK else {}
    if state == 'same_source_reuse':
        return {'analysis_state': state} if book.get('state') in _AUTHORITATIVE_BOOK else {}
    if state not in ('completed', 'completed_received_scope'):
        return {}
    if book.get('relation') != 'own':
        return {}
    if book.get('state') == 'registered_edition_mismatch':
        return {'analysis_state': 'awaiting_re_review'}
    if book.get('state') not in _AUTHORITATIVE_BOOK:
        return {'analysis_state': 'awaiting_acceptance'} if book.get('state') == 'edition_unverified' else {}
    scope = analysis.get('scope_status')
    if acceptance not in _ACCEPTED or (state == 'completed_received_scope'
            and (scope not in {'核心待补', '配套待补'} or scope != review.get('scope_status'))):
        return {'analysis_state': 'awaiting_acceptance'}
    fields = {'analysis_state': state, 'acceptance_state': acceptance}
    if scope in _SCOPES and scope == review.get('scope_status'):
        fields['analysis_scope'] = scope
    return fields


def present_notes(roots):
    """Only scan the standard same-name note in each official library folder."""
    notes = {}
    for root in roots:
        try:
            folders = tuple(root.iterdir())
        except OSError:
            continue
        for folder in folders:
            name = folder.name
            if name.startswith(('.', '00 ')):
                continue
            path = folder / '课件' / f'{name}.md'
            note = file_inside(str(path), (root,))
            if note:
                try:
                    if 0 < note.stat().st_size:
                        notes[note] = name
                except OSError:
                    continue
    return notes


class CatalogProgress:
    def __init__(self, library, local):
        self.library = library
        self.local = local
        self.lock = Lock()
        self.last_observation = self.last_index = None
        self.public_titles = defaultdict(set)
        self.rows = getattr(library, 'semantic_rows', library.by_id)
        for row in self.rows.values():
            if key := title_key(row['title']):
                self.public_titles[key].add(article_identity(row))

    def read(self):
        with self.lock:
            observation = self.local.get()
            if observation is self.last_observation:
                return self.last_index
            current = observation.get('sync_state') in ('live', 'partial')
            data = (observation.get('data') or {}) if current else {}
            roots = set(self.local.roots(data))
            try:
                if FORMAL_VAULT.is_dir() and FORMAL_VAULT.resolve(strict=True) == FORMAL_VAULT:
                    roots.add(FORMAL_VAULT)
            except OSError:
                pass
            names = defaultdict(set)
            notes = present_notes(roots)
            projects = defaultdict(list)
            for path, name in notes.items():
                if key := title_key(name, folder=True):
                    names[key].add(path)
            for project in data.get('projects', ()):
                projects[project['local_project_id']].append(project)
                book = project.get('textbook') or {}
                path = Path(book['entry']) if book.get('entry') else None
                if path not in notes or book.get('relation') != 'own':
                    continue
                downloaded = (project.get('download') or {}).get('directory')
                for title in (project.get('title'), Path(downloaded).name if downloaded else ''):
                    if key := title_key(title, folder=True):
                        names[key].add(path)
            self.last_observation = observation
            self.last_index = (
                observation.get('sync_state'), exact_map(data.get('legacy_links') or ()),
                projects, names, roots,
            )
            return self.last_index

    def fields(self, row, index, *, include_book=False):
        state = row.get('status')
        scope = row.get('scope')
        transfer = ({'verified': 'transferred_previously', 'recycled': 'recycled'}.get(state)
                    if scope == 'selected_roots_only' else None)
        result = {'transfer_state': transfer} if transfer else {}
        if row.get('action_pending') or row.get('demo'):
            return result
        _, exact, projects, names, roots = index
        # The formal link applies to the particular source version only.
        ids = set()
        if exact and scope == 'selected_roots_only':
            pairs, _, _ = self.local.exact_targets(row)
            ids = {exact[pair] for pair in pairs if pair in exact}
        if len(ids) > 1:
            return result
        if ids:
            matches = projects[next(iter(ids))]
            if len(matches) != 1:
                return result
            project = matches[0]
            analyzed = exact_analysis(project)
            result.update(analyzed)
            if include_book and analyzed.get('analysis_state') in {
                    'completed', 'completed_received_scope', 'same_source_reuse', 'awaiting_re_review'}:
                book = file_inside((project.get('textbook') or {}).get('entry'), roots)
                if book:
                    result['textbook_href'] = 'obsidian://open?path=' + quote(book.as_posix(), safe='')
            return result
        # A unique full name only identifies a local result. It cannot pass a review gate.
        key = title_key(row['title'])
        candidates = names.get(key, ())
        if len(candidates) == 1 and self.public_titles.get(key) == {article_identity(row)}:
            result['analysis_state'] = 'local_result_available'
            if include_book and (book := file_inside(str(next(iter(candidates))), roots)):
                result['textbook_href'] = 'obsidian://open?path=' + quote(book.as_posix(), safe='')
        return result


def main():
    parser = argparse.ArgumentParser(description='Read transfer and local analysis statuses from the current workspace.')
    parser.add_argument('--article', help='站点来源项目号')
    args = parser.parse_args()
    library = open_index()
    progress = CatalogProgress(library, LocalProjection(library))
    index = progress.read()
    if args.article:
        matched = [row for row in progress.rows.values() if row['article_id'] == args.article]
        if not matched:
            parser.error('未找到该来源项目')
        value = [{'site': row['site_id'], 'article': row['article_id'], 'title': row['title'],
                  **progress.fields(row, index)} for row in matched]
    else:
        fields = [progress.fields(row, index) for row in progress.rows.values()]
        value = {'source_articles': len(fields), 'local_state': index[0],
                 'transfer': dict(Counter(row['transfer_state'] for row in fields if 'transfer_state' in row)),
                 'analysis': dict(Counter(row['analysis_state'] for row in fields if 'analysis_state' in row))}
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    main()
