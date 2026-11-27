"""Catalog pagination and bulk selection against a synthetic directory."""

import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from server import make_server
from test_course_calendar import library


class CatalogPageLimits(unittest.TestCase):
    def setUp(self):
        catalog = library(last_article=32)
        catalog.meta = {}
        unavailable = next(iter(catalog.by_id.values()))
        for row in catalog.by_id.values():
            row['requestable'] = row is not unavailable
        catalog.requestable_count = 31

        temporary = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.http = make_server(catalog, Path(temporary.name) / 'draft.sqlite3', 0)
        self.addCleanup(self.http.server_close)
        runner = Thread(target=self.http.serve_forever, daemon=True)
        runner.start()
        self.addCleanup(runner.join)
        self.addCleanup(self.http.shutdown)

    def request(self, path, payload=None):
        client = http.client.HTTPConnection(self.http.authority, timeout=5)
        headers = {'Origin': 'http://' + self.http.authority, 'Content-Type': 'application/json'}
        try:
            client.request('GET' if payload is None else 'POST', path,
                           None if payload is None else json.dumps(payload).encode(), headers)
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def resources(self, query):
        status, result = self.request('/api/resources?' + query)
        self.assertEqual(status, 200, result)
        return result

    def test_expanded_page_and_bulk_use_the_same_rows(self):
        status, meta = self.request('/api/meta')
        self.assertEqual(status, 200, meta)
        self.assertEqual(meta['catalog_page_limits'], [16, 20, 24, 28, 32])
        self.assertEqual(self.resources('view=grid')['limit'], 12)
        self.assertEqual(self.resources('view=list')['limit'], 16)

        first = self.resources('view=list&limit=20')
        second = self.resources('view=list&limit=20&page=2')
        first_ids = {row['id'] for row in first['items']}
        second_ids = {row['id'] for row in second['items']}
        self.assertEqual((first['limit'], first['pages'], len(first_ids), len(second_ids)), (20, 2, 20, 12))
        self.assertFalse(first_ids & second_ids)

        request = {
            'action': 'bulk', 'revision': 0, 'scope': 'page', 'snapshot_id': first['snapshot_id'],
            'expected_count': first['page_selectable'], 'filter': {'view': 'list'},
        }
        self.assertEqual(self.request('/api/draft', request)[0], 409)
        request['filter']['limit'] = 20
        status, draft = self.request('/api/draft', request)
        self.assertEqual(status, 200, draft)
        self.assertEqual(set(draft['ids']), {row['id'] for row in first['items'] if row['requestable']})

    def test_rejects_unrecognized_limits(self):
        for invalid in ('0', '19', '99', '020'):
            with self.subTest(limit=invalid):
                self.assertEqual(self.request('/api/resources?limit=' + invalid)[0], 400)
