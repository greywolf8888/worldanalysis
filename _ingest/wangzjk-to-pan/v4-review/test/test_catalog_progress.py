from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, mock

from analysis_priority import show_entries
from catalog_progress import CatalogProgress, exact_analysis, title_key
from course_followups import CourseFollowups, REGISTRY
from catalog_semantics import article_key
from course_completion import COVERAGE_WORK_FILES, completion_fields
from production_bridge import formal_review_stage
from workspace_paths import INPUT, LEGACY_VAULT


SITE = 'wangzjk.com'
TITLE = '抖音实拍蹭流量短视频创作和涨粉教学'


def row(article, title=TITLE, transfer='observed', scope='unknown'):
    return {'site_id': SITE, 'article_id': article, 'title': title, 'status': transfer, 'scope': scope}


class CatalogProgressTest(TestCase):
    def test_only_matching_dispatch_and_independent_review_versions_change_the_view(self):
        with TemporaryDirectory(dir=Path(__file__).parent) as folder:
            workspace = Path(folder).resolve()
            entry = workspace / '整理结果' / 'candidate-review.json'
            entry.parent.mkdir()
            candidate = entry.with_name('candidate.json')
            candidate.write_text('{}', encoding='utf-8')
            active = {'local_project_id': 'project', 'analysis': {'state': 'in_progress'}}
            progress = {'current_production': {'local_project_id': 'project', 'candidate_sha256': 'a' * 64,
                        'evidence': {'candidate_manifest': str(candidate), 'independent_review_request': str(entry)}}}
            with mock.patch('production_bridge.BASE', workspace):
                entry.write_text(json.dumps({'body_sha256': 'b' * 64, 'decision': ''}), encoding='utf-8')
                self.assertEqual(formal_review_stage(active, progress), active)
                entry.write_text(json.dumps({'body_sha256': 'a' * 64, 'decision': ''}), encoding='utf-8')
                review = formal_review_stage(active, progress)
                self.assertEqual(exact_analysis(review)['analysis_state'], 'awaiting_acceptance')

            registry = workspace / REGISTRY
            registry.parent.mkdir()
            registry.write_text(json.dumps({'schema': 1, 'root': str(workspace / '分析目标下载地址'),
                'analysis_readback': {'schema_version': 1, 'revision': 2, 'event_id': 'a' * 64,
                                      'waiting': [{'key': 'local:project', 'rank': 1}]} }), encoding='utf-8')
            followups = CourseFollowups(workspace)
            submitted = {'revision': 2, 'event_id': 'a' * 64, 'entries': [{'key': 'local:project'}]}
            self.assertEqual(followups.waiting_for(submitted), {'local:project'})
            self.assertEqual(followups.waiting_for({**submitted, 'event_id': 'b' * 64}), set())

    def test_priority_survives_confirmed_catalog_identity_change_but_not_revision_change(self):
        current = {**row('1'), 'id': 'current', 'article_revision': 'one'}
        previous = {**current, 'kind': 'article', 'resource_id': 'previous', 'key': f'article:{SITE}:1'}
        library = SimpleNamespace(by_id={}, semantic_rows={article_key(SITE, '1'): current})
        projects = {'known': {'analysis': {'state': 'in_progress'}}}
        binding = {'local_project_id': 'known'}
        index = {('previous', SITE, '1', 'one'): binding, ('current', SITE, '1', 'one'): binding}
        shown = show_entries([previous], library, projects, index)[0]
        self.assertEqual((shown['state'], shown['resource_id'], shown['current_resource_id']),
                         ('in_progress', 'previous', 'current'))
        current['article_revision'] = 'two'
        changed = show_entries([previous], library, projects, index)[0]
        self.assertEqual(changed['state'], 'source_changed')
        self.assertNotIn('current_resource_id', changed)

    def test_source_gaps_do_not_override_received_analysis_and_cleanup_has_its_own_gate(self):
        project = {'analysis': {'state': 'completed_received_scope', 'scope_status': '核心待补'},
                   'acceptance': {'state': 'scope_accepted', 'scope_status': '核心待补'},
                   'textbook': {'state': 'registered_edition_matches', 'relation': 'own'}}
        self.assertEqual(completion_fields(project), {
            'originals': 'core_missing', 'received_analysis': 'accepted_for_edition',
            'textbook': 'received_scope_usable', 'cleanup': 'retain_for_core_gap',
        })
        project['analysis']['scope_status'] = project['acceptance']['scope_status'] = '配套待补'
        self.assertEqual(completion_fields(project), {
            'originals': 'support_missing', 'received_analysis': 'accepted_for_edition',
            'textbook': 'core_usable',
        })
        project['textbook']['state'] = 'registered_edition_mismatch'
        self.assertEqual(completion_fields(project), {})
        project['textbook']['state'] = 'registered_edition_matches'
        project['acceptance']['state'] = 'historical_same_edition'
        self.assertEqual(completion_fields(project), {'textbook': 'historical_readable'})

    def test_actual_new_intake_version_controls_public_status_and_ambiguity_is_blank(self):
        source = {**row('1'), 'id': 'rv_x', 'article_revision': 'now'}
        library = SimpleNamespace(by_id={'rv_x': source})
        project = {'local_project_id': 'local', 'analysis': {'state': 'in_progress'}}
        local = mock.Mock()
        local.get.return_value = {'sync_state': 'live', 'data': {'projects': [project]}}
        local.roots.return_value = []
        progress = CatalogProgress(library, local)
        index = {('rv_x', SITE, '1', 'now'): {'local_project_id': 'local'}}
        with mock.patch.object(progress.followups, 'read', return_value=index):
            projection = progress.read()
            self.assertEqual(progress.fields(source, projection, include_book=True), {
                'analysis_state': 'in_progress', 'course_completion': {'received_analysis': 'in_progress'},
            })
            self.assertEqual(progress.fields({**source, 'article_revision': 'older'}, projection), {})
            self.assertEqual(progress.fields({**source, 'article_revision': 'newer'}, projection), {})
            local.get.return_value = {'sync_state': 'pending_sync', 'data': {'projects': [project]}}
            self.assertEqual(progress.fields(source, progress.read()), {})

    def test_analysis_omission_requires_the_current_draft_receipt(self):
        with TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            workspace = Path(temporary).resolve()
            results = workspace / '整理结果'
            package = results / 'package'
            work = package / '.course'
            record = {'schema': 1, 'received_analysis': 'received_coverage_missing'}
            for key, filename in COVERAGE_WORK_FILES.items():
                path = work / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                data = filename.encode('utf-8')
                path.write_bytes(data)
                record[key] = sha256(data).hexdigest()
            (work / 'work/实收核对.json').write_text(json.dumps(record), encoding='utf-8')
            manifest = results / 'batch.json'
            manifest.write_text(json.dumps({'projects': [
                {'id': 'current', 'status': '处理中', 'package': str(package)},
            ]}), encoding='utf-8')
            project = {'local_project_id': 'current', 'evidence': {'manifest': str(manifest)},
                       'analysis': {'state': 'in_progress'}}
            self.assertEqual(completion_fields(project, workspace), {'received_analysis': 'received_coverage_missing'})
            (work / 'work/验收填写模板.json').write_text('新预览', encoding='utf-8')
            self.assertEqual(completion_fields(project, workspace), {'received_analysis': 'in_progress'})

    def test_normalization_preserves_explicit_versions(self):
        folder = '035-20260903-' + TITLE + '【更多资源www.abab9.cn】'
        self.assertEqual(title_key(folder, folder=True), title_key('（21546期）' + TITLE))
        self.assertNotEqual(title_key(TITLE + '2.0'), title_key(TITLE + '20'))
        self.assertNotEqual(title_key(TITLE + '更新1115'), title_key(TITLE + '更新1116'))

    def test_signed_scope_requires_current_note_and_matching_acceptance(self):
        project = {'analysis': {'state': 'completed_received_scope', 'scope_status': '核心待补'},
                   'acceptance': {'state': 'scope_accepted', 'scope_status': '核心待补'},
                   'textbook': {'state': 'registered_edition_matches', 'relation': 'own'}}
        self.assertEqual(exact_analysis(project), {'analysis_state': 'completed_received_scope',
                         'acceptance_state': 'scope_accepted', 'analysis_scope': '核心待补'})
        project['acceptance']['scope_status'] = '配套待补'
        self.assertEqual(exact_analysis(project), {'analysis_state': 'awaiting_acceptance'})
        project['textbook']['state'] = 'registered_edition_mismatch'
        self.assertEqual(exact_analysis(project), {'analysis_state': 'awaiting_re_review'})
        project['textbook']['state'] = 'missing'
        self.assertEqual(exact_analysis(project), {})

    def test_local_note_changes_independently_of_transfer(self):
        plain = row('1')
        saved = row('2', TITLE + '另一个做法', 'verified', 'selected_roots_only')
        new = row('3', TITLE + '更新1113')
        library = SimpleNamespace(by_id={'1': plain, '2': saved, '3': new})
        with TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            workspace = Path(temporary).resolve()
            source = workspace / INPUT / '新分类' / '储存'
            root = source.parent / '分析结果'
            note = root / TITLE / '课件' / f'{TITLE}.md'
            note.parent.mkdir(parents=True)
            note.write_text('# ' + TITLE, encoding='utf-8')
            source.mkdir()
            registry = workspace / REGISTRY
            registry.parent.mkdir(parents=True)
            manifest = registry.parent / '新分类.json'
            binding = {'source': str(source), 'vault': str(root)}
            manifest.write_text(json.dumps({**binding, 'dispatch_registry': str(registry)}), encoding='utf-8')
            registry.write_text(json.dumps({'schema': 1, 'root': str(workspace / INPUT),
                'bindings': {'新分类': {**binding, 'category': '新分类', 'manifest': str(manifest)}}}), encoding='utf-8')
            obsolete = (workspace / LEGACY_VAULT / TITLE / '课件' / f'{TITLE}.md')
            obsolete.parent.mkdir(parents=True)
            obsolete.write_text('# ' + TITLE, encoding='utf-8')
            local = mock.Mock()
            local.followups = CourseFollowups(workspace)
            local.get.return_value = {'sync_state': 'pending_sync'}
            local.roots.return_value = [workspace / LEGACY_VAULT]
            progress = CatalogProgress(library, local)
            first = progress.read()
            self.assertEqual(progress.fields(plain, first), {'analysis_state': 'local_result_available'})
            self.assertEqual(progress.fields(saved, first), {'transfer_state': 'transferred_previously'})
            self.assertEqual(progress.fields(new, first), {})
            note.unlink()
            local.get.return_value = {'sync_state': 'pending_sync'}
            self.assertEqual(progress.fields(plain, progress.read()), {})
            registry.unlink()
            local.get.return_value = {'sync_state': 'pending_sync'}
            self.assertEqual(progress.fields(plain, progress.read()), {'analysis_state': 'local_result_available'})

    def test_duplicate_name_and_stale_projection_do_not_set_completion(self):
        left, right = row('1'), row('2')
        library = SimpleNamespace(by_id={'1': left, '2': right})
        local = mock.Mock()
        local.get.return_value = {'sync_state': 'pending_sync',
                                  'data': {'projects': [{'local_project_id': 'p'}]}}
        local.roots.return_value = []
        with mock.patch('catalog_progress.present_notes', return_value={Path('G:/sample.md'): TITLE}):
            progress = CatalogProgress(library, local)
            index = progress.read()
            self.assertEqual(index[2], {})
            self.assertEqual(progress.fields(left, index), {})
            self.assertEqual(progress.fields(right, index), {})
