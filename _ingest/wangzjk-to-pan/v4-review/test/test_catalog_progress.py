from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, mock

from catalog_progress import CatalogProgress, exact_analysis, title_key


SITE = 'wangzjk.com'
TITLE = '抖音实拍蹭流量短视频创作和涨粉教学'


def row(article, title=TITLE, transfer='observed', scope='unknown'):
    return {'site_id': SITE, 'article_id': article, 'title': title, 'status': transfer, 'scope': scope}


class CatalogProgressTest(TestCase):
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
            root = Path(temporary).resolve()
            note = root / TITLE / '课件' / f'{TITLE}.md'
            note.parent.mkdir(parents=True)
            note.write_text('# ' + TITLE, encoding='utf-8')
            local = mock.Mock()
            local.get.return_value = {'sync_state': 'live', 'data': {}}
            local.roots.return_value = [root]
            with mock.patch('catalog_progress.FORMAL_VAULT', root):
                progress = CatalogProgress(library, local)
                first = progress.read()
                self.assertEqual(progress.fields(plain, first), {'analysis_state': 'local_result_available'})
                self.assertEqual(progress.fields(saved, first), {'transfer_state': 'transferred_previously'})
                self.assertEqual(progress.fields(new, first), {})
                note.unlink()
                local.get.return_value = {'sync_state': 'live', 'data': {}}
                self.assertEqual(progress.fields(plain, progress.read()), {})

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
