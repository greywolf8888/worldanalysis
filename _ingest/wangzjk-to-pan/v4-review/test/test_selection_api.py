from contextlib import closing
import json
from http.client import HTTPConnection
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from catalog_model import Library, ORIGIN, Wangzjk
from catalog_relations import CatalogRelations
from catalog_semantics import CatalogStore
from server import make_server


class SharedSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        articles = {}
        for index in range(1, 81):
            key = str(index)
            articles[key] = {'article_id': key, 'url': f'{ORIGIN}/{key}.html',
                             'title': f'淘宝运营课程 {index}', 'published_at': '2026-11-27',
                             'access_state': 'public', 'shares': []}
        source = Wangzjk({'catalog.json': {'articles': articles},
                          'journal.json': {'operations': {}},
                          'downloaded-project-markers.json': {'entries': []},
                          'crawl_state.JSON': {'phase': 'paused'},
                          'capacity-policy.json': {'minimum_free_bytes': 100_000_000_000}}, {})
        library = Library([source])
        library.snapshot_id = 'a'*64
        library.attach_catalog(CatalogStore(root/'catalog.sqlite3'), {})
        library.catalog_all = library.match({}, set())
        library.requestable_count = len(articles)
        progress = Mock()
        progress.read.return_value = {}
        modules = patch.multiple('server', LocalProjection=Mock(), CatalogProgress=Mock(return_value=progress))
        modules.start()
        self.addCleanup(modules.stop)
        exclusions = patch('course_calendar.read_non_projects', return_value={})
        exclusions.start()
        self.addCleanup(exclusions.stop)
        self.http = make_server(library, root/'draft.sqlite3', 0)
        self.addCleanup(self.http.server_close)
        runner = Thread(target=self.http.serve_forever, daemon=True)
        runner.start()
        self.addCleanup(runner.join)
        self.addCleanup(self.http.shutdown)

    def request(self, path, payload=None):
        connection = HTTPConnection(self.http.authority, timeout=5)
        try:
            headers = {'Origin': 'http://'+self.http.authority, 'Content-Type': 'application/json'}
            connection.request('GET' if payload is None else 'POST', path,
                               None if payload is None else json.dumps(payload), headers)
            result = connection.getresponse()
            return result.status, json.loads(result.read())
        finally:
            connection.close()

    def test_retried_analysis_submission_does_not_duplicate_the_durable_event(self):
        endpoint = '/api/analysis-priority'
        first, second = self.request('/api/course-calendar')[1]['items'][:2]
        def add(course, revision):
            return self.request(endpoint, {'action': 'add', 'revision': revision, 'target': {
                'kind': 'article', 'resource_id': course['id'], 'site_id': course['site_id'],
                'article_id': course['article_id'],
            }})
        self.assertEqual(add(first, 0)[0], 200)
        submitted_status, submitted = self.request(endpoint, {'action': 'submit', 'revision': 1})
        self.assertEqual(submitted_status, 200)
        self.assertEqual(submitted['submitted']['schema_version'], 2)
        self.assertEqual(self.request(endpoint, {'action': 'submit', 'revision': 1}), (200, submitted))
        self.assertTrue(submitted['draft_matches_submission'])
        self.assertEqual(add(second, 2)[0], 200)
        self.assertEqual(self.request(endpoint, {'action': 'submit', 'revision': 1})[0], 409)
        self.assertEqual(self.request(endpoint)[1]['submitted'], submitted['submitted'])
        database = self.http.snapshot.get()[1].path.with_name('analysis-priority.sqlite3')
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute('SELECT revision, event_id FROM analysis_priority_submissions').fetchall(),
                             [(2, submitted['submitted']['event_id'])])

    def test_local_order_binds_originals_and_reuses_an_existing_accepted_edition(self):
        local = self.http.snapshot.get()[2]
        queued = {'local_project_id': 'a' * 64, 'title': '新来件', 'analysis': {'state': 'queued'}}
        signed = {'local_project_id': 'b' * 64, 'title': '已有教材',
                  'analysis': {'state': 'completed', 'scope_status': '完整验收'},
                  'acceptance': {'state': 'scope_accepted', 'scope_status': '完整验收'},
                  'textbook': {'state': 'registered_edition_matches', 'relation': 'own'}}
        local.get.return_value = {'sync_state': 'live', 'data': {'projects': [queued, signed]}}
        local.analysis_anchor.return_value = {'source_sha256': 'c' * 64, 'metadata_sha256': 'd' * 64}
        request = {'action': 'add', 'revision': 0,
                   'target': {'kind': 'local', 'project_id': signed['local_project_id']}}
        self.assertEqual(self.request('/api/analysis-priority', request),
                         (422, {'error': '已有同版教材，可直接打开'}))
        local.analysis_anchor.assert_not_called()
        request['target']['project_id'] = queued['local_project_id']
        self.assertEqual(self.request('/api/analysis-priority', request)[0], 200)
        local.analysis_anchor.assert_called_once_with(queued['local_project_id'])
        status, result = self.request('/api/analysis-priority', {'action': 'submit', 'revision': 1})
        self.assertEqual(status, 200)
        settled = result['submitted']['entries'][0]
        self.assertEqual((settled['source_sha256'], settled['metadata_sha256']), ('c' * 64, 'd' * 64))

    def test_pick_from_calendar_then_remove_from_catalog(self):
        status, courses = self.request('/api/course-calendar')
        self.assertEqual(status, 200)
        course = courses['items'][0]
        self.assertTrue(course['requestable'])
        status, saved = self.request('/api/draft', {
            'action': 'toggle', 'revision': 0, 'id': course['id'], 'article_key': course['article_key'],
            'selected': True, 'compact': True, 'snapshot_id': courses['snapshot_id'],
        })
        self.assertEqual((status, saved['ids']), (200, [course['id']]))
        self.assertNotIn('entries', saved)
        self.assertEqual(self.request('/api/course-calendar?selected=1')[1]['total'], 1)
        self.assertEqual(self.request('/api/resources?selected=1&draft_revision=1')[1]['draft'], None)
        status, removed = self.request('/api/draft', {'action': 'remove_many', 'revision': 1,
                                                     'ids': [course['id']], 'compact': True})
        self.assertEqual((status, removed['ids']), (200, []))
        self.assertEqual(self.request('/api/course-calendar?selected=1')[1]['total'], 0)

    def test_short_search_terms_share_punctuation_and_facet_semantics(self):
        for endpoint in ('/api/resources','/api/course-calendar'):
            with self.subTest(endpoint=endpoint):
                query = '?category=commerce&platform=taobao&'
                spaced = self.request(endpoint + query + urlencode({'q': '淘宝 运营'}))
                punctuated = self.request(endpoint + query + urlencode({'q': '淘宝，运营'}))
                missing = self.request(endpoint + query + urlencode({'q': '淘宝；剪辑'}))
                self.assertEqual((spaced[0], punctuated[0], missing[0]), (200, 200, 200))
                self.assertEqual((spaced[1]['total'], punctuated[1]['total'], missing[1]['total']), (80, 80, 0))
                self.assertEqual([row['id'] for row in spaced[1]['items']],
                                 [row['id'] for row in punctuated[1]['items']])

    def test_playbooks_endpoint_is_read_only_and_leaves_unverified_index_empty(self):
        self.http.snapshot.get()[0].meta = {}
        self.assertEqual(self.request('/api/meta')[1]['playbooks'], True)
        self.assertEqual(self.request('/api/playbooks'), (200, {'items': []}))
        self.assertEqual(self.request('/api/playbooks', {})[0], 404)

    def test_all_versions_across_resource_pages_and_calendar_keeps_exact_pick(self):
        library = self.http.snapshot.get()[0]
        rows = {row['article_id']: row for row in library.semantic_rows.values()}
        old, newer = rows['1'], rows['80']
        for row, edition in ((old, '第一版'), (newer, '第二版')):
            row['analysis'] = {**row['analysis'], 'edition': edition}
        def source(row):
            return {field: row[field] for field in ('site_id','article_id','input_hash')}
        edge = {'left': source(old), 'right': source(newer), 'relation': 'different_version', 'same_scope': True}
        library.relations = CatalogRelations(rows.values(), [edge])
        library.catalog_all = library.match({}, set())

        latest_status, latest = self.request('/api/resources?page=5&limit=16')
        all_status, display_all = self.request('/api/resources?versions=all&page=5&limit=16')
        self.assertEqual((latest_status, latest['total'], all_status, display_all['total']), (200,79,200,80))
        self.assertNotIn(old['id'], {item['id'] for item in latest['items']})
        self.assertEqual(display_all['items'][-1]['id'], old['id'])
        self.assertEqual(self.request('/api/resources?versions=unknown')[0], 400)

        calendar_status, calendar = self.request('/api/course-calendar?versions=all')
        self.assertEqual((calendar_status, calendar['total']), (200,80))
        self.assertEqual(self.request('/api/course-calendar?after=' + calendar['next'])[0], 409)
        page = self.request('/api/course-calendar?versions=all&after=' + calendar['next'])[1]
        self.assertEqual(page['items'][-1]['id'], old['id'])
        status, saved = self.request('/api/draft', {
            'action': 'toggle', 'revision': 0, 'id': old['id'], 'article_key': old['article_key'],
            'selected': True, 'snapshot_id': calendar['snapshot_id'], 'compact': True,
        })
        self.assertEqual((status, saved['ids']), (200,[old['id']]))
        for endpoint in ('/api/course-calendar', '/api/resources'):
            for mode in ('latest','all'):
                status, response = self.request(f'{endpoint}?versions={mode}&selected=1')
                self.assertEqual((status, [item['id'] for item in response['items']]), (200,[old['id']]))

    def test_large_selection_pages_details_and_protects_a_previous_confirmation(self):
        status, all_items = self.request('/api/draft', {'action': 'bulk','revision': 0,
            'scope': 'site_all', 'expected_count': 80, 'snapshot_id': 'a'*64, 'compact': True})
        self.assertEqual((status, len(all_items['ids'])), (200,80))
        self.assertEqual(len(self.request('/api/draft?entry_page=1')[1]['entries']), 60)
        status, last = self.request('/api/draft?entry_page=99')
        self.assertEqual((status, last['entry_page'], len(last['entries'])), (200, 2, 20))
        self.assertTrue(last['can_review'])
        status, after = self.request('/api/draft', {'action': 'remove_many','revision': 1,
                                                   'ids': all_items['ids'][:60], 'compact': True})
        self.assertEqual((status, after['ids']), (200, all_items['ids'][60:]))
        status, conflict = self.request('/api/draft', {'action': 'clear','revision': 1,'compact': True})
        self.assertEqual((status, conflict['draft']['ids']), (409, after['ids']))
        self.assertEqual(self.request('/api/draft?view=selection')[1], after)
