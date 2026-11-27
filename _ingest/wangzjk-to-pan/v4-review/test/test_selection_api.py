import json
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import Mock, patch

from catalog_model import Library, ORIGIN, Wangzjk
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
