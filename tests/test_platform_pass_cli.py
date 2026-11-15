"""Actual local CLI round trip with synthetic receipts, never a live pan claim."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from worldanalysis.store import Store


class PlatformPassCliTests(unittest.TestCase):
    def test_first_pass_then_precise_plan_and_restored_integrity(self):
        with tempfile.TemporaryDirectory(prefix='worldanalysis-platform-cli-') as temporary:
            root = Path(temporary)
            with Store(root) as store:
                project = store.create_project(topic='Synthetic platform CLI contract')
            (root / 'short.txt').write_text('一句很短但实际取得的课程文稿。', encoding='utf-8')
            original = (root / 'short.txt').read_bytes()
            folder = '/apps/教育分析学习/cli-contract'
            inventory = {
                'schema_version': '1.0', 'inventory_id': 'cli-enumeration',
                'account_ref': 'synthetic-account', 'root_path': folder,
                'enumeration_complete': True, 'evidence_ref': 'synthetic-list',
                'evidence_class': 'synthetic',
                'files': [{'file_id': str(i), 'path': folder + f'/{i}.mp4',
                           'source_revision': 'synthetic-r1', 'size_bytes': 1,
                           'duration_ms': 120000} for i in (1, 2)]}
            (root / 'inventory.json').write_text(json.dumps(inventory), encoding='utf-8')

            def cli(*args, synthetic=True, success=True):
                command = [sys.executable, '-B', '-m', 'worldanalysis.platform_pass', '--root', str(root)]
                if synthetic:
                    command += ['--synthetic-input']
                run = subprocess.run(command + list(args), capture_output=True, text=True,
                                     cwd=Path(__file__).resolve().parents[1], timeout=30)
                self.assertEqual(run.returncode, 0 if success else 2, run.stdout + run.stderr)
                return json.loads(run.stdout)

            cli('begin', '--project', project, '--inventory', 'inventory.json', synthetic=False, success=False)
            run = cli('begin', '--project', project, '--inventory', 'inventory.json')['run_id']
            cli('seal', '--run', run, success=False)
            cli('plan', '--run', run, success=False)

            observations = [
                ('1', 'manuscript', {'state': 'present', 'evidence_ref': 'synthetic-export',
                    'relative_path': 'short.txt', 'content_kind': 'transcript',
                    'coverage': {'state': 'sufficient', 'basis': 'full_transcript',
                                 'evidence_ref': 'synthetic-review'}}),
                ('1', 'courseware', {'state': 'missing', 'evidence_ref': 'synthetic-no-slides'}),
                ('2', 'manuscript', {'state': 'waiting', 'evidence_ref': 'synthetic-waiting'}),
                ('2', 'courseware', {'state': 'missing', 'evidence_ref': 'synthetic-no-slides-2'})]
            for number, (file_id, product, observation) in enumerate(observations):
                next_item = cli('next', '--run', run)
                self.assertEqual((next_item['file']['file_id'], next_item['product']), (file_id, product))
                (root / 'observation.json').write_text(json.dumps(observation), encoding='utf-8')
                cli('record', '--run', run, '--file-id', file_id, '--product', product,
                    '--observation', 'observation.json')
                if number < 3:
                    cli('seal', '--run', run, success=False)
            cli('seal', '--run', run)
            plan = cli('plan', '--run', run)
            self.assertEqual({(r['file_id'], r['modality']) for r in plan['supplements']},
                             {('1', 'visual'), ('2', 'visual')})
            self.assertEqual(len(plan['reconcile']), 1)
            self.assertFalse(plan['execution_performed'])
            repeated = cli('plan', '--run', run)
            self.assertEqual([x['job_id'] for x in plan['supplements']],
                             [x['job_id'] for x in repeated['supplements']])
            self.assertEqual((root / 'short.txt').read_bytes(), original)
            (root / 'short.txt').write_text('Changed after acquisition', encoding='utf-8')
            status = cli('status', '--run', run)
            self.assertEqual(len(status['integrity_issues']), 1)
            self.assertFalse(status['generation_enabled'])
            self.assertFalse(any(p.suffix.lower() in {'.mp4', '.mp3', '.wav'} for p in root.rglob('*')))


if __name__ == '__main__':
    unittest.main()
