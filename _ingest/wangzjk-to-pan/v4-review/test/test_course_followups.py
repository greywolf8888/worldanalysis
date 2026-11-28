from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

REVIEW = Path(__file__).resolve().parents[1]
WORKSPACE = REVIEW.parents[2]
sys.path.insert(0, str(REVIEW))

from catalog_model import compact, digest
from course_followups import active_handoff, binding_for, related_followups, verified_bindings


class CourseFollowupsTest(unittest.TestCase):
    def write(self, relative, value):
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = compact(value)
        path.write_bytes(raw)
        return {'path': relative, 'sha256': sha256(raw).hexdigest()}

    def receipt(self, kind, record_id, producer, state):
        return {'schema_version': 1, 'record_type': kind, 'record_id': record_id,
                'producer': producer, 'state': state, 'execution_allowed': True,
                'revision': 1, 'observed_at': '2026-11-28T04:00:00+08:00'}

    def test_verified_intake_survives_root_alias_only_with_exact_edition(self):
        legacy_session = 'session-00000000-0000-4000-8000-000000000001'
        self.enterContext(patch.dict(os.environ, {'WORLDANALYSIS_LEGACY_DSH_SESSION_ID': legacy_session}))
        handoff = active_handoff(WORKSPACE)
        self.assertTrue(handoff, 'production handoff validator required for this test')
        with TemporaryDirectory(prefix='V4-DEMO-followup-', dir=Path(__file__).parent) as scratch:
            self.workspace = Path(scratch).resolve()
            root = {'name': '原始课包', 'source_id': '123', 'is_dir': True, 'size': 0}
            files = [{'file': '原始课包/讲义.pdf', 'bytes': 128}]
            metadata = [{'file': '讲义.pdf', 'bytes': 128, 'mtime_ns': 42}]
            key = 'f' * 64
            version = 'rv_' + digest(['unverified_article_reference', ['wangzjk.com', '10', [key]]])
            identity = {'resource_version_id': version, 'site_id': 'wangzjk.com', 'article_id': '10',
                        'article_revision': 'arv_' + 'a' * 64, 'source_id': 'src_' + key,
                        'source_revision': 'sr_' + digest([[root], ['原始课包'], files])}
            manifest = self.write('_exchange/dsh/catalog/range.json', {
                **identity, 'schema_version': 1, 'record_type': 'range_manifest', 'scope': 'recursive',
                'complete_recursive': True, 'root_count': 1, 'roots': [root], 'directories': ['原始课包'],
                'files': files, 'share_sha256': 'b' * 64,
                'recursive_evidence': 'normal_ui_exact_directory_listing', 'destination': '/测试目录',
            })
            item = {**identity, 'queue_item_id': 'one', 'transfer_task_id': 'transfer', 'item_rank': 1,
                    'range_manifest_ref': manifest}
            order = {'batch_id': 'batch', 'order_revision': 1, 'confirmation_seq': 1,
                     'confirmed_at': '2026-11-28T04:00:00+08:00', 'order_mode': 'ready_ordered',
                     'selection_sha256': handoff['selection_sha']([item])}
            order_evidence = {**order, 'ordered_items_sha256': handoff['ordered_items_sha']([item])}
            selection = self.write('_exchange/control/decisions/selection.json', {
                **self.receipt('frozen_transfer_selection', 'selection', 'codex-control', 'frozen'),
                **order_evidence, 'items': [item],
            })
            decision = self.write('_exchange/control/decisions/decision.json', {
                **self.receipt('user_confirmation', 'decision', 'codex-control', 'confirmed'), **order_evidence,
                'confirmation_actor': 'user', 'authorized_task_id': 'task', 'frozen_selection_ref': selection,
                'operation': 'one_bounded_normal_baidu_ui_transfer_batch',
                'user_message_reference': 'isolated contract fixture',
            })
            task = self.write('_exchange/control/tasks/task.json', {
                **self.receipt('control_task', 'task', 'codex-control', 'ready'), 'task_id': 'task',
                'plan_version': 'V4', 'target_session_id': legacy_session,
                'authorization_ref': decision, 'inputs': {'frozen_selection_ref': selection},
            })
            approval = self.write('_exchange/dsh/authorizations/approval.json', {
                **self.receipt('user_confirmation', 'approval', 'dsh-ui', 'confirmed'), **order_evidence,
                'confirmation_actor': 'user', 'control_decision_ref': decision,
                'confirmation_channel': 'codex_control_record_of_actual_user_reply',
            })
            plan = {**self.receipt('confirmed_plan', 'batch', 'dsh', 'confirmed'), **order,
                    'authorization_ref': approval, 'confirmation_task_ref': task, 'items': [item]}
            self.write('_exchange/dsh/plans/batch.json', plan)
            project_id = 'p1'
            source = self.workspace / '分析目标下载地址/02/储存'
            vault = self.workspace / '分析目标下载地址/02/分析结果'
            folder = source / '手动下载文件夹'
            intake = {**self.receipt('local_intake', 'one', 'codex-course', 'matched'),
                      'queue_item_id': 'one', 'batch_id': 'batch', 'accepted_order_revision': 1,
                      'range_manifest_ref': manifest, 'local_project_id': project_id,
                      'source_folder': folder.relative_to(self.workspace).as_posix(),
                      'files': metadata, 'analysis_ready': True, 'download_complete': True,
                      **{field: identity[field] for field in ('resource_version_id', 'source_id', 'source_revision')}}
            self.write('_exchange/codex/intake/one.json', intake)
            batch_path = '整理结果/batch.json'
            batch = {'source': str(source), 'vault': str(vault),
                     'projects': [{'id': project_id, 'name': folder.name, 'metadata': metadata}]}
            self.write(batch_path, batch)
            registry = {'bindings': {'02': {'source': str(source), 'vault': str(vault),
                         'manifest': str(self.workspace / batch_path)}}, 'v4_handoff': {
                             'plans': {'batch': plan}, 'intakes': {'one': intake},
                         }}
            index = verified_bindings(self.workspace, registry, handoff)
            row = {'id': version, 'site_id': 'wangzjk.com', 'article_id': '10',
                   'article_revision': identity['article_revision']}
            expected = {'local_project_id': project_id, 'intake_download_complete': True}
            self.assertEqual(binding_for(index, row), expected)
            alias = 'rv_' + digest([key, 'selected_roots_only', [['123', '原始课包', True, 0]]])
            self.assertEqual(binding_for(index, {**row, 'id': alias}), expected)
            self.assertIsNone(binding_for(index, {**row, 'id': alias, 'article_revision': 'arv_' + 'c' * 64}))
            self.assertIsNone(binding_for(index, {**row, 'action_pending': True}))

            production = Path(handoff['__file__']).parent
            with patch.object(sys, 'path', [str(production), *sys.path]):
                resolver = runpy.run_path(str(production / 'analysis_priority_order.py'))['resolved_priorities']
            formal_row = {'id': project_id}
            submitted_entry = {'kind': 'article', 'resource_id': alias, 'site_id': row['site_id'],
                               'article_id': row['article_id'], 'article_revision': row['article_revision'],
                               'key': 'article:wangzjk.com:10'}
            submitted = {'entries': [submitted_entry], 'revision': 4, 'event_id': 'explicit-order'}
            planned = [SimpleNamespace(ready=True, row=formal_row, item=item, plan=plan)]
            ready = lambda binding, selected_row: True
            self.assertEqual(resolver(submitted, [({}, formal_row)], planned, ready, self.workspace)
                             [project_id]['rank'], 1)
            submitted_entry['article_revision'] = 'arv_' + 'c' * 64
            self.assertEqual(resolver(submitted, [({}, formal_row)], planned, ready, self.workspace), {})

            blocked = deepcopy(registry)
            blocked['v4_handoff']['blocked'] = {'one': '已回收'}
            self.assertEqual(verified_bindings(self.workspace, blocked, handoff), {})
            decision_path = self.workspace / decision['path']
            unmodified = decision_path.read_bytes()
            decision_path.write_bytes(unmodified + b' ')
            self.assertEqual(verified_bindings(self.workspace, registry, handoff), {})
            decision_path.write_bytes(unmodified)
            self.write(batch_path, {**batch, 'projects': [{**batch['projects'][0], 'metadata': []}]})
            self.assertEqual(verified_bindings(self.workspace, registry, handoff), {})
            self.write(batch_path, batch)
            (self.workspace / '_exchange/codex/intake/one.json').unlink()
            self.assertEqual(verified_bindings(self.workspace, registry, handoff), {})

    def test_candidates_use_current_catalog_relations_without_execution_fields(self):
        ordinary = {'id': 'project', 'article_key': 'site:10', 'article_revision': 'revision',
                    'title': '可核对的原发布', 'source_path': 'internal', 'scope': 'recursive'}
        detail = {'other_sources': [ordinary, {**ordinary, 'action_pending': True}],
                  'other_versions': [{**ordinary, 'article_revision': 'another'}]}
        first, second = related_followups(detail)
        self.assertEqual(set(first), {'id', 'article_key', 'article_revision', 'title', 'relation'})
        self.assertEqual(first['relation'], 'same_edition_source')
        self.assertEqual(second['relation'], 'different_edition')


if __name__ == '__main__':
    unittest.main()
