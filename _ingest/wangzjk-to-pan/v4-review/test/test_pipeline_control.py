from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from pipeline_control import RunBusy, RunStore, worker


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


if __name__ == '__main__':
    unittest.main()
