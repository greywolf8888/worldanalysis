import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import collect_public as collector
import intro_inputs
from project_detail import project


def wordpress_post(key, stamp, body='<p>这是公开课程简介。</p>', title='公开课程'):
    return {
        'id': int(key), 'link': collector.ORIGIN + '/' + str(key) + '.html', 'status': 'publish',
        'date': stamp, 'modified': stamp, 'featured_media': 0,
        'title': {'rendered': title}, 'excerpt': {'protected': False, 'rendered': '<p>短摘要</p>'},
        'content': {'protected': False, 'rendered': body},
    }


def projection(payload):
    return project({
        'article_id': str(payload['id']), 'http': 200, 'payload': payload,
        'observed_at': '2026-11-27T08:00:00+08:00', 'source': 'public_rest', 'body_sha256': 'test',
    }, include_intro=True)


class IntroInputTests(unittest.TestCase):
    def test_targeted_collection_keeps_article_scope_and_text_counts(self):
        stamp='2026-11-26T14:15:00'
        rows = {
            '12': wordpress_post('12', stamp, '<p>这是可读的原始课程介绍。'
                                 '<a href="https://github.com/public/example/releases/latest">仓库</a></p>'),
            '18': wordpress_post('18', stamp, '<img src="/unused.png">'),
            '23': wordpress_post('23', stamp),
        }
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'codex-public.json'
            state = collector.load(path)
            state['articles'] = {key: collector.normalize(row) for key,row in rows.items()}
            collector.publish(state, path)
            with patch.object(collector,'WordPress') as client:
                client.return_value.get.return_value = [rows['18'],rows['12']], {'X-WP-Total':'2'}, 'rest','test'
                out=collector.collect(path, 0, 20, ('12','18'), only_seed=True)
                self.assertEqual(client.return_value.get.call_count,1)
                self.assertEqual(client.return_value.get.call_args.kwargs['include'],'12,18')
            found=intro_inputs.read_intros(path.parent/'pipeline-runs.sqlite3')
            self.assertEqual(found[(collector.SITE,'12')]['github_links'], ('https://github.com/public/example',))
            self.assertEqual(found[(collector.SITE,'18')]['coverage'],'image_only')
            self.assertNotIn((collector.SITE,'23'),found)
            self.assertEqual(out['introductions_saved'],1)

    def test_public_batch_rejects_unexpected_members_before_applying(self):
        stamp='2026-11-26T14:15:00'
        with patch.object(collector,'WordPress') as client:
            client.return_value.get.return_value = [wordpress_post('99', stamp)], {'X-WP-Total':'1'}, 'rest','test'
            with self.assertRaises(collector.PublicReadError) as error:
                list(collector.selected_details(client.return_value, ('12','18')))
            self.assertEqual(error.exception.reason,'publisher_detail_collection_mismatch')

    def test_regular_batch_keeps_reading_after_one_unreadable_article(self):
        stamp = '2026-11-26T14:15:00'
        readable = wordpress_post('18', stamp)
        deferred = []
        with patch.object(collector,'WordPress') as client:
            client.return_value.get.side_effect = [
                ([readable], {'X-WP-Total': '1'}, 'rest', 'test'),
                collector.PublicReadError('publisher_http_error', 500),
            ]
            results = list(collector.selected_details(client.return_value, ('12','18'),
                                                     on_article_error=deferred.append))
        self.assertEqual(deferred, ['12'])
        self.assertEqual(results, [('18', readable, 'rest', 'test')])

    def test_visible_body_survives_restricted_download_and_long_detail_is_retained(self):
        long_text = '摘要之外的课程内容。' * 1800
        payload = wordpress_post('459', '2026-11-27T08:00:00', f'''
            <h2>课程介绍</h2><p>{long_text}末尾线索</p>
            <div class="download-box">登录后查看全文 <p>不应保留的数据</p></div>
            <p>第 2 课：课程作者的实操目录</p>''')
        result = projection(payload)
        intro = result['introduction']
        self.assertEqual(intro['coverage'], 'complete')
        self.assertIn('末尾线索', intro['raw_text'])
        self.assertIn('第 2 课', intro['clean_text'])
        self.assertNotIn('不应保留的数据', intro['raw_text'])
        self.assertEqual(len(result['blocks'][1]['text']), 16000)

    def test_unmarked_gate_never_admits_suffix_as_public_introduction(self):
        payload = wordpress_post('459', '2026-11-27T08:00:00',
                                 '<p>公开介绍</p><p>登录后查看全文</p><p>未确认可公开的内容</p>')
        intro = projection(payload)['introduction']
        self.assertEqual(intro['coverage'], 'partial')
        self.assertEqual(intro['raw_text'], '公开介绍')
        self.assertEqual(intro['coverage_reason'], 'unmarked_access_boundary')
        payload['content']['protected'] = True
        self.assertNotIn('introduction', projection(payload))

    def test_visible_github_anchors_are_additive_and_respect_access_boundaries(self):
        body = ('<p>细节在<a href="https://github.com/public/sample/releases/latest?key=private">'
                '<span>项目仓库</span></a>，以及 https://github.com/open-source/linked-notes。</p>'
                '<p><a href="https://github.com/hidden/not-rendered"><span hidden>不可见</span></a></p>'
                '<div class="download-box"><a href="https://github.com/hidden/download">受限链接</a></div>'
                '<p>公开前缀，登录后查看全文 <a href="https://github.com/hidden/not-public">受限链接</a></p>')
        result = projection(wordpress_post('459','2026-11-27T08:00:00',body))
        expected = ('https://github.com/public/sample', 'https://github.com/open-source/linked-notes')
        self.assertEqual(tuple(result['github_links']), expected)
        self.assertEqual(result['introduction']['github_links'], expected)
        self.assertNotIn('github.com', result['introduction']['raw_text'])
        self.assertNotIn('private', str(result))
        no_anchor = projection(wordpress_post('459','2026-11-27T08:00:00',
                                              body.replace('href="https://github.com/public/sample/releases/latest?key=private"', '')))
        self.assertEqual(result['introduction']['fingerprint'],no_anchor['introduction']['fingerprint'])

    def test_repository_changes_reload_intro_without_reversioning_text(self):
        timestamp = '2026-11-27T08:00:00+08:00'
        old_version = '2026-11-26T08:00:00'
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            db = Path(directory) / 'pipeline-runs.sqlite3'
            row = intro_inputs.make_intro(collector.SITE,'459',['介绍段落'],publisher_modified=old_version)
            def save(value):
                intro_inputs.record_attempt(db,collector.SITE,'459',value['publisher_modified'],timestamp,'saved',value)
            save(row)
            save({**row,'github_links':['https://github.com/public/source/tree/main']})
            found = intro_inputs.read_intros(db)[(collector.SITE,'459')]
            self.assertEqual(found['github_links'],('https://github.com/public/source',))
            self.assertEqual(found['fingerprint'],row['fingerprint'])
            self.assertEqual(intro_inputs.intro_revision(db),2)
            save({**row,'github_links':['https://github.com/public/source']})
            self.assertEqual(intro_inputs.intro_revision(db),2)
            save({**row,'publisher_modified':'2026-11-27T08:00:00'})
            self.assertNotIn('github_links',intro_inputs.read_intros(db)[(collector.SITE,'459')])

    def test_image_only_template_and_intro_budget_keep_truthful_coverage(self):
        page = wordpress_post('459', '2026-11-27T08:00:00',
                              '<p>仅供学习交流，请勿用于商业用途。</p><img src="bad">')
        intro = projection(page)['introduction']
        self.assertEqual(intro['coverage'], 'image_only')
        self.assertEqual(intro['clean_text'], '')
        self.assertIn('仅供学习交流', intro['raw_text'])
        with patch.object(intro_inputs, 'MAX_INTRO_BYTES', 30):
            intro = intro_inputs.make_intro(collector.SITE, '459', ['首段', '第二段' * 10])
        self.assertEqual(intro['coverage'], 'partial')
        self.assertEqual(intro['raw_text'], '首段')

    def test_older_body_is_not_selected_for_new_known_modified_date(self):
        detail = projection(wordpress_post('459', '2026-11-26T08:00:00'))
        legacy = intro_inputs.legacy_intro(collector.SITE, '459', detail)
        self.assertEqual(legacy['coverage'], 'partial')
        self.assertIsNone(intro_inputs.choose_intro(None, legacy, '2026-11-27T08:00:00'))
        self.assertEqual(legacy, intro_inputs.choose_intro(None, legacy, '2026-11-26T08:00:00'))

    def test_storage_failure_does_not_set_success_or_advance_revision(self):
        moment = '2026-11-27T08:00:00+08:00'
        with TemporaryDirectory() as directory:
            db = Path(directory) / 'pipeline-runs.sqlite3'
            row = intro_inputs.make_intro(collector.SITE, '459', ['原始课程介绍'])
            with patch.object(intro_inputs, 'MAX_INTRO_TOTAL_BYTES', 1):
                with self.assertRaises(ValueError):
                    intro_inputs.record_attempt(db, collector.SITE, '459', None, moment, 'saved', row)
            self.assertEqual(intro_inputs.intro_revision(db), 0)
            self.assertEqual(intro_inputs.read_attempts(db, collector.SITE), {})
            self.assertEqual(intro_inputs.read_intros(db), {})
            intro_inputs.record_attempt(db, collector.SITE, '459', None, moment, 'saved', row)
            intro_inputs.record_attempt(db, collector.SITE, '459', None, moment, 'saved', row)
            self.assertEqual(intro_inputs.intro_revision(db), 1)
            readback = intro_inputs.read_intros(db)[(collector.SITE, '459')]
            self.assertEqual(readback['clean_text'], row['clean_text'])
            self.assertEqual(readback['payload_bytes'], len(row['raw_text'].encode('utf-8')))

    def test_intro_after_four_hundred_graphic_details_and_old_attempt(self):
        stamp = '2026-11-26T08:00:00'
        body = '<p>项目介绍完整段落</p><h3>重命名后仍保留章节目录</h3>'
        rows = {str(i): collector.normalize(wordpress_post(str(i), stamp)) for i in range(1, 402)}
        existing = {str(i): projection(wordpress_post(str(i), stamp)) for i in range(1, 401)}
        for item in existing.values():
            item.pop('introduction')
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'codex-public.json'
            doc = collector.load(path)
            doc['articles'] = rows
            doc['details'] = existing
            doc['attempts']['401'] = stamp
            doc['history']['completed'] = True
            collector.publish(doc, path)
            fake = patch.object(collector, 'WordPress')
            with fake as client:
                client.return_value.get.return_value = wordpress_post('401', stamp, body), {}, 'rest', 'test'
                result = collector.collect(path, 0, 1, ('401',))
                client.return_value.get.assert_called_once()
            db = path.parent / 'pipeline-runs.sqlite3'
            intro = intro_inputs.read_intros(db)[(collector.SITE, '401')]
            self.assertEqual(intro['raw_text'], '项目介绍完整段落\n重命名后仍保留章节目录')
            self.assertEqual(len(collector.load(path)['details']), 400)
            self.assertEqual(intro_inputs.read_attempts(db, collector.SITE)['401']['state'], 'saved')
            self.assertEqual(result['introductions_saved'], 1)

    def test_modified_page_keeps_fixed_cutoff_when_publisher_times_arrive_out_of_order(self):
        stamp = '2026-11-27T08:00:00'
        lower = '2026-11-27T07:40:00'
        end = '2026-11-27T09:00:00'
        catalog = {'articles': {'35': collector.normalize(wordpress_post('35', '2026-11-25T08:00:00'))}}
        first = [
            wordpress_post('53', '2026-11-27T08:10:00'),
            wordpress_post('35', '2026-11-27T08:20:00', title='改动后的标题'),
        ]
        first[1]['date'] = '2026-11-25T08:00:00'
        second = [wordpress_post('41', '2026-11-27T08:25:00')]

        class Publisher:
            def listing(self, page, **kwargs):
                self.last = (page, kwargs)
                return (first if page == 1 else second), 3, 2, 'test'

        source = Publisher()
        self.assertEqual(collector.modified_discovery(source, catalog, end, stamp, end, 1), ['35', '53'])
        self.assertEqual(catalog['articles']['35']['title'], '改动后的标题')
        self.assertEqual(catalog['articles']['35']['published_at'], '2026-11-25')
        self.assertNotIn('first_collected_at', catalog['articles']['35'])
        self.assertEqual(catalog['articles']['53']['first_collected_at'], end)
        self.assertEqual(source.last, (1, {'sort':'modified', 'modified_after':lower, 'modified_before':end}))
        self.assertEqual(collector.modified_discovery(source, catalog, end, stamp, '2026-11-27T10:00:00', 1), ['41'])
        self.assertEqual(source.last, (2, {'sort':'modified', 'modified_after':lower, 'modified_before':end}))
        self.assertEqual(catalog['modified'], {'after':end, 'next_page':1})

    def test_modified_window_restarts_at_same_bound_on_duplicate_or_short_final_page(self):
        start = '2026-11-27T08:00:00'
        upper = '2026-11-27T09:00:00'
        first = [wordpress_post('35', '2026-11-27T08:15:00')]
        for final_page in (first, []):
            with self.subTest(final_page=final_page):
                state = {'articles': {}}

                class Publisher:
                    def listing(self, page, **kwargs):
                        return (first if page == 1 else final_page), 2, 2, 'test'

                collector.modified_discovery(Publisher(), state, upper, start, upper, 1)
                with self.assertRaises(collector.PublicReadError):
                    collector.modified_discovery(Publisher(), state, upper, start, '2026-11-27T10:00:00', 1)
                self.assertEqual(state['modified'], {'after':start, 'before':upper, 'next_page':1})

    def test_publication_window_keeps_bound_and_stops_on_duplicate(self):
        start = '2026-11-27T08:00:00'
        upper = '2026-11-27T09:00:00'
        row = wordpress_post('35', '2026-11-27T08:15:00')
        state = {'articles': {}, 'head': {'after': start, 'next_page': 1}}
        with patch.object(collector, 'WordPress') as client:
            source = client.return_value
            source.listing.return_value = [row], 2, 2, 'test'
            collector.published_discovery(source, state, upper, upper, 1)
            with self.assertRaises(collector.PublicReadError):
                collector.published_discovery(source, state, upper, '2026-11-27T10:00:00', 1)
            source.listing.assert_called_with(2, after=start, before=upper)
        self.assertEqual(state['head'], {'after': start, 'before': upper, 'next_page': 1})

    def test_retry_waits_and_exhausted_attempt_only_reopens_for_new_source(self):
        modified = '2026-11-26T08:00:00'
        failed = {'publisher_modified': modified, 'observed_at': modified, 'state':'failed', 'attempts':2}
        self.assertFalse(intro_inputs.should_retry(failed, modified, datetime.fromisoformat('2026-11-27T08:00:00')))
        self.assertTrue(intro_inputs.should_retry(failed, modified, datetime.fromisoformat('2026-11-28T08:00:00')))
        failed['attempts'] = 3
        self.assertFalse(intro_inputs.should_retry(failed, modified, datetime.fromisoformat('2026-12-01T08:00:00')))
        self.assertTrue(intro_inputs.should_retry(failed, '2026-12-01T08:00:00', datetime.fromisoformat('2026-12-01T08:00:00')))
