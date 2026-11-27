from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from pipeline_control import RunBusy, RunStore, collect_catalog, current_discovery, verify_pipeline, worker


class PipelineControlTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir=HERE)
        self.addCleanup(temporary.cleanup)
        self.store = RunStore(Path(temporary.name).resolve() / 'runs.sqlite3')

    def test_one_active_job_and_persisted_readback(self):
        key = self.store.reserve('verify')
        with self.assertRaises(RunBusy):
            self.store.reserve('collect')
        with patch('pipeline_control.verify_pipeline', return_value=('complete', '已读回')) as verify:
            self.assertTrue(worker(self.store, key, 8768))
            self.assertFalse(worker(self.store, key, 8768))
        verify.assert_called_once_with(8768)
        recovered = RunStore(self.store.path).recent()[0]
        self.assertEqual((recovered['state'], recovered['summary']), ('complete', '已读回'))
        self.store.reserve('collect')

    def test_disconnected_worker_does_not_look_complete_or_retry_automatically(self):
        key = self.store.reserve('collect')
        with self.store.guard():
            self.assertEqual(self.store.claim(key), 'collect')
            self.assertEqual(self.store.recent()[0]['state'], 'running')
        self.assertEqual(self.store.recent()[0]['state'], 'failed')
        self.assertEqual(len(self.store.recent()), 1)

    def test_abandoned_start_can_be_recovered(self):
        self.store.reserve('verify')
        old = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        with self.store.connect() as database:
            database.execute('UPDATE runs SET created_at = ?', (old,))
        self.assertEqual(self.store.recent()[0]['state'], 'failed')
        self.store.reserve('verify')

    def test_scheduled_slot_checks_both_source_bounds(self):
        current = datetime.fromisoformat('2026-11-27T22:00:00+08:00')
        marker = {'completed_at': '2026-11-27T18:02:00+08:00',
                  'published_through': '2026-11-27T18:01:00', 'modified_through': '2026-11-27T18:01:00'}
        self.assertTrue(current_discovery(marker, current))
        self.assertFalse(current_discovery({**marker, 'modified_through': '2026-11-27T17:58:00'}, current))
        self.assertFalse(current_discovery({**marker, 'published_through': '2026-11-27T12:00:00'}, current))
        self.assertFalse(current_discovery({'observed_at': '2026-11-27T20:00:00+08:00'}, current))
        self.assertTrue(current_discovery(marker, datetime.fromisoformat('2026-11-28T05:59:59+08:00')))
        self.assertFalse(current_discovery(marker, datetime.fromisoformat('2026-11-28T06:00:00+08:00')))

    def test_scheduled_collection_reuses_completed_window_and_reads_it_back(self):
        marker = {'completed_at': '2026-11-27T18:02:00+08:00',
                  'published_through': '2026-11-27T18:01:00', 'modified_through': '2026-11-27T18:01:00'}
        local = {'collection': {'observed_at': '2026-11-27T20:00:00+08:00', 'last_discovery': marker}}
        live = {'public_checkpoint_pending_sync': False, 'discovery': {'last_discovery': marker}}
        with patch('pipeline_control.collector_lock', return_value=MagicMock()), \
                patch('pipeline_control.datetime', wraps=datetime) as clock, \
                patch('pipeline_control.load', return_value=local), \
                patch('pipeline_control.collect') as fetch, \
                patch('pipeline_control.request_local', return_value=live) as readback:
            clock.now.return_value = datetime.fromisoformat('2026-11-27T22:00:00+08:00')
            self.assertEqual(collect_catalog(8768, scheduled=True)[0], 'complete')
            fetch.assert_not_called()
            readback.assert_called_once_with(8768, 'meta', timeout=60)

    def test_incomplete_sweep_is_reported_without_reusing_an_older_success(self):
        result = {'state': 'updated', 'discovery_status': 'incomplete', 'last_discovery': {'completed_at': 'old'}}
        with patch('pipeline_control.collector_lock', return_value=MagicMock()), \
                patch('pipeline_control.collect', return_value=result), \
                patch('pipeline_control.request_local') as readback:
            self.assertEqual(collect_catalog(8768)[0], 'waiting')
            readback.assert_not_called()

    def test_full_check_uses_sources_for_links_and_projects_for_filters(self):
        revision = {'snapshot_id': 'catalog'}
        replies = {
            'meta': {**revision, 'public_checkpoint_pending_sync': False,
                     'totals': {'baidu_linked_articles': 3},
                     'categories': [{'id': 'growth', 'count': 2}],
                     'platforms': [{'id': 'github', 'count': 1}]},
            'resources?baidu=1': {**revision, 'total': 2, 'source_total': 3},
            'resources?category=growth': {**revision, 'total': 2},
            'resources?platform=github': {**revision, 'total': 1},
            'local-status': {'sync_state': 'live'}, 'preconfirmation': revision,
            'analysis-priority': {'entries': []}, 'cleanup-readiness': {'sync_state': 'live'},
        }
        with patch('pipeline_control.request_local', side_effect=lambda port, path: replies[path]):
            self.assertEqual(verify_pipeline(8768)[0], 'complete')
            replies['resources?platform=github']['total'] = 0
            self.assertEqual(verify_pipeline(8768)[0], 'failed')


if __name__ == '__main__':
    unittest.main()
