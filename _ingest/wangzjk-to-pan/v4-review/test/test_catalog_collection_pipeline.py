import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import Mock, patch

import catalog_model
import collect_public as collector
import server
from catalog_semantics import article_key
from pipeline_control import request_local


DAY = '2026-11-27'
OLD = DAY + 'T15:00:00'
NEW = DAY + 'T18:10:00'
CURRENT = DAY + 'T18:11:00'


def post(article, title, text, *, published=NEW, modified=NEW):
    return {
        'id': int(article), 'status': 'publish', 'link': f'{collector.ORIGIN}/{article}.html',
        'date': published, 'modified': modified, 'title': {'rendered': title},
        'excerpt': {'protected': False, 'rendered': ''}, 'featured_media': 0,
        'content': {'protected': False, 'rendered': text},
    }


class CollectionPipeline(unittest.TestCase):
    def test_periodic_collection_refreshes_categories_in_both_live_pages(self):
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            workspace = Path(directory).resolve()
            root = workspace / 'review'
            data, state = root / 'data', workspace / 'state'
            data.mkdir(parents=True)
            state.mkdir()
            checkpoint, db = data / 'codex-public.json', data / 'pipeline-runs.sqlite3'
            fixtures = {
                'catalog.json': {'articles': {}}, 'journal.json': {'operations': {}},
                'downloaded-project-markers.json': {'entries': []},
                'capacity-policy.json': {'minimum_free_bytes': 100_000_000_000},
                'crawl_state.JSON': {'phase': 'paused'}, 'automation.json': {},
            }
            for name, content in fixtures.items():
                (state / name).write_text(json.dumps(content), encoding='utf-8')
            (data / 'public-details.json').write_text(json.dumps({
                'schema': 'wangzjk-v4-public-article-projection.v1', 'site_id': collector.SITE, 'articles': {}
            }), encoding='utf-8')
            earlier = post('20', '专项课程教学', '<p>从零开始进行淘宝店铺运营和商品上架。</p>',
                           published=OLD, modified=OLD)
            edited = post('20', '专项课程教学', '<p>本课程主要教使用模板与镜头完成短剧制作全过程。</p>', published=OLD)
            added = post('31', '开源视频压缩工具',
                         '<p>软件可批量压缩视频，保持原画质。<a href="https://github.com/public/tool">仓库</a></p>')
            initial = collector.load(checkpoint)
            initial['history'].update(before=OLD, completed=True)
            initial['head'] = {'after': OLD, 'next_page': 1}
            initial['modified'] = {'after': OLD, 'next_page': 1}
            initial['articles']['20'] = collector.normalize(earlier)
            initial['collection'] = {'read_only': True}
            collector.publish(initial, checkpoint)
            old_intro = collector.capture_projection(earlier, '20', OLD+'+08:00', 'fixture', 'test')['introduction']
            collector.record_attempt(db, collector.SITE, '20', OLD, OLD+'+08:00', 'saved', old_intro)

            progress = Mock()
            progress.read.return_value = {}
            with patch.multiple(catalog_model, ROOT=root, SEMANTIC_DB=db), \
                    patch('catalog_model.protected_owner_exports', return_value=()), \
                    patch('server.protected_owner_exports', return_value=()), \
                    patch('server.LocalProjection', return_value=object()), \
                    patch('server.CatalogProgress', return_value=progress), \
                    patch('course_calendar.read_non_projects', return_value={}):
                lib = catalog_model.open_index()
                previous = lib.semantic_rows[article_key(collector.SITE, '20')]
                self.assertEqual(previous['category'], 'commerce')
                with server.make_server(lib, data/'draft.sqlite3', 0, watch=checkpoint, catalog_db=db) as http:
                    runner = Thread(target=http.serve_forever, daemon=True)
                    runner.start()
                    try:
                        with patch('collect_public.WordPress') as client, \
                                patch('collect_public.datetime', wraps=datetime) as clock:
                            clock.now.return_value = datetime.fromisoformat(CURRENT+'+08:00')
                            client.return_value.listing.side_effect = [
                                ([added], 1, 1, 'head'), ([added, edited], 2, 1, 'modified'),
                            ]
                            client.return_value.get.return_value = (
                                [edited, added], {'X-WP-Total': '2'}, 'fixture', 'body'
                            )
                            with collector.collector_lock(checkpoint):
                                result = collector.collect(checkpoint, 12, 48, ())
                            self.assertEqual(client.return_value.get.call_count, 1)
                            client.return_value.get.reset_mock()
                            result_backfill = collector.collect(checkpoint, 0, 48, ('20', '31'), only_seed=True)
                            client.return_value.get.assert_not_called()
                        self.assertEqual(result['discovery_status'], 'complete')
                        self.assertEqual(result['introductions_saved'], 2)
                        self.assertEqual(result_backfill['last_discovery'], result['last_discovery'])
                        self.assertEqual(result_backfill['discovery_status'], 'details_only')

                        initial_draft = server.Draft(data/'draft.sqlite3', lib)
                        startup_race = server.LiveSnapshot(lib, initial_draft, object(), checkpoint, db)
                        just_started, _, _, pending = startup_race.get()
                        self.assertFalse(pending)
                        self.assertEqual(just_started.discovery['last_discovery'], result['last_discovery'])
                        meta = request_local(http.server_port, 'meta')
                        self.assertFalse(meta['public_checkpoint_pending_sync'])
                        self.assertEqual(meta['discovery']['last_discovery'], result['last_discovery'])
                        screen = request_local(http.server_port, 'resources?category=screen')
                        self.assertEqual(screen['items'][0]['id'], previous['id'])
                        self.assertEqual(screen['items'][0]['subcategory'], '短剧与漫剧')
                        self.assertEqual(request_local(http.server_port, 'resources?category=commerce')['total'], 0)
                        self.assertEqual(len(meta['categories']), 8)
                        content = request_local(http.server_port, 'resources?category=biz_content')
                        self.assertEqual(content['items'][0]['id'], previous['id'])
                        self.assertEqual(content['items'][0]['business_category_label'], '内容创作与自媒体')
                        calendar = request_local(http.server_port, 'course-calendar?category=software&platform=github')
                        self.assertEqual(calendar['total'], 1)
                        self.assertEqual(calendar['items'][0]['title'], '开源视频压缩工具')
                        tools = request_local(http.server_port, 'course-calendar?category=biz_technology&platform=github')
                        self.assertEqual(tools['total'], 1)
                        self.assertEqual(tools['items'][0]['business_category_label'], '技术工具与自动化')
                    finally:
                        http.shutdown()
                        runner.join()
