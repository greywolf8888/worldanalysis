"""Run the real regression suite and save a version-bound, non-overwriting receipt.
This verifies code and listed samples, never the whole unread course/catalog.
"""
from __future__ import annotations
import hashlib
import io
import json
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import worldanalysis  # activates only the project-local pinned dependency directory


def snapshot():
    paths = list((ROOT / 'worldanalysis').glob('*.py')) + list((ROOT / 'tests').glob('test_*.py')) + list((ROOT / 'worldanalysis_agent_devpack_20261110' / 'contracts').glob('*.json'))
    paths += [ROOT/'_tools'/'browser-inventory-observer.js',ROOT/'_tools'/'browser-inventory-observer_test.mjs',Path(__file__).resolve()]
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def main():
    began = time.monotonic()
    before = snapshot()
    log = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py')
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    print(log.getvalue(), end='')
    after = snapshot()
    node = Path('D:/桌面/awesome-dsh/.dsh-home/dsh-runtimes/dsh-primary-runtime/dependencies/node/bin/node.exe')
    legacy = subprocess.run([str(node), '--test', '--test-reporter=dot', '_tools/course-flow.test.mjs', '_tools/materials-ingest.test.mjs'], cwd=ROOT, capture_output=True, encoding='utf-8', timeout=120)
    print(legacy.stdout, end=''); print(legacy.stderr, end='', file=sys.stderr)
    observer = subprocess.run([str(node), '--test', '--test-reporter=dot', '_tools/browser-inventory-observer_test.mjs'], cwd=ROOT, capture_output=True, encoding='utf-8', timeout=120)
    print(observer.stdout, end=''); print(observer.stderr, end='', file=sys.stderr)
    from worldanalysis.store import Store
    from worldanalysis.analysis import Analysis
    with Store(ROOT) as store:
        bundles = {p:store.get_bundle(p) for p in ('real-shorttxt-meituan','real-docx-virtual-materials','real-pdf-163545','calibration-synthetic-speech')}
        analysis = Analysis(store)
        accepted = {t:analysis.get_result(t) for t in ('analysis-task-0193d2b5749b418c84f724b4b43154be','analysis-task-74a65236b89342fb91ed27c8d623a1d7','analysis-task-18fa937a54bf4561b4ddd105e6b26420')}
    passed = result.wasSuccessful() and before == after and legacy.returncode == 0 and observer.returncode == 0
    receipt = {
        'schema':'worldanalysis.integration-verification.v1',
        'checked_at':datetime.now(timezone.utc).isoformat(),
        'python':sys.executable, 'python_version':sys.version.split()[0],
        'tests_run':result.testsRun, 'failures':len(result.failures), 'errors':len(result.errors),
        'skipped':[{'test':str(t),'reason':reason} for t,reason in result.skipped],
        'unit_test_successful':result.wasSuccessful(), 'source_snapshot_unchanged':before == after,
        'source_revisions_sha256':after,
        'legacy_node_exit_code':legacy.returncode,
        'inventory_observer_node_exit_code':observer.returncode,
        'inventory_observer_scope':'synthetic passive observer tests, not live cloud acceptance',
        'legacy_node_scope':'existing coordinator and transcript ingestion suites',
        'actual_bundles':{p:{'bundle_id':b['bundle_id'],'revision':b['bundle_revision'],'publish_id':b['publish_id'],'coverage':b['coverage']['claimed_status'],'blocks':len(b['blocks']),'gap_count':len(b['gaps'])} for p,b in bundles.items()},
        'actual_accepted_reports':{t:{'execution_status':r['execution_status'],'project_id':r['project_id'],'main_report_relative_path':r['main_report_relative_path']} for t,r in accepted.items()},
        'elapsed_seconds':round(time.monotonic()-began,3),
        'passed_for_stated_scope':passed, 'whole_devpack_plan_complete':False,
        'scope_limitations':['Unit/contract, synthetic runtime and selected real local samples are distinct.','P2 live observer and full transfer/readback, native/media composition, real course and long-load acceptance remain incomplete. Single-source media Bundle integration has only synthetic runtime acceptance.','Partial reports and evidence gaps remain partial, regardless of this test result.'],
        'test_log':log.getvalue(), 'legacy_node_log':legacy.stdout+legacy.stderr,
        'inventory_observer_log':observer.stdout+observer.stderr,
    }
    out = ROOT / '_ledger' / 'devpack' / ('verification-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.json')
    with out.open('x', encoding='utf-8') as stream:
        json.dump(receipt,stream,ensure_ascii=False,indent=2)
    print(json.dumps({'verification_receipt':str(out),'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'passed_for_stated_scope':passed,'whole_plan_complete':False},ensure_ascii=False,indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
