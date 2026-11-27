import unittest
from unittest.mock import Mock, patch

from catalog_model import Library, ORIGIN, SITE, Wangzjk, merge_codex_collection
from catalog_semantics import article_key
from intro_inputs import make_intro


ARTICLE_ID = '42'
ARTICLE_URL = f'{ORIGIN}/{ARTICLE_ID}.html'
OLD = '2026-11-26T13:00:00'
CURRENT = '2026-11-27T17:00:00'
OLD_IMAGE = f'{ORIGIN}/wp-content/uploads/old.jpg'
CURRENT_IMAGE = f'{ORIGIN}/wp-content/uploads/current.jpg'
FEATURED = f'{ORIGIN}/wp-content/uploads/featured.jpg'
REPOSITORY = 'https://github.com/open-source/sample'


def article(*, excerpt='', cover=None):
    return {'article_id': ARTICLE_ID, 'url': ARTICLE_URL, 'title': '店铺运营实战教学',
            'published_at': '2026-11-26', 'publisher_modified': CURRENT,
            'public_excerpt': excerpt, 'cover_url': cover}


def detail(modified, text, image, *, observed='2026-11-27T18:00:00+08:00', links=()):
    return {'article_url': ARTICLE_URL, 'publisher_modified': modified, 'observed_at': observed,
            'state': 'public_sample', 'excerpt': text, 'cover_url': image, 'github_links': links,
            'blocks': [{'kind': 'text', 'text': text}, {'kind': 'image', 'url': image}]}


def library(row, facts, stored=None):
    state = {'catalog.json': {'articles': {ARTICLE_ID: row}}, 'journal.json': {'operations': {}},
             'downloaded-project-markers.json': {'entries': []}, 'crawl_state.JSON': {},
             'capacity-policy.json': {'minimum_free_bytes': 100_000_000_000}}
    lib = Library([Wangzjk(state, {'articles': {ARTICLE_ID: facts} if facts else {}})])
    store = Mock()
    store.read.return_value = ({}, [], set())
    lib.attach_catalog(store, {(SITE, ARTICLE_ID): stored} if stored else {})
    return lib


class CatalogSourceVersionTests(unittest.TestCase):
    def test_known_stale_detail_does_not_supply_blocks_excerpt_cover_or_search_evidence(self):
        old = detail(OLD, '旧简介介绍闲鱼开店流程。', OLD_IMAGE, links=(REPOSITORY,))
        stored = make_intro(SITE, ARTICLE_ID, ['旧正文介绍闲鱼的店铺布局和选品。'],
                            publisher_modified=OLD, source_kind='public_rest', github_links=(REPOSITORY,))
        for listing_excerpt, listing_cover in (('', None), ('新版店铺运营介绍', FEATURED)):
            with self.subTest(listing_excerpt=listing_excerpt):
                current = article(excerpt=listing_excerpt, cover=listing_cover)
                lib = library(current, old, stored)
                baseline = library(current, None)
                token = article_key(SITE, ARTICLE_ID)
                row = lib.semantic_rows[token]
                self.assertEqual(row, baseline.semantic_rows[token])
                self.assertEqual(lib.semantic_inputs[token], baseline.semantic_inputs[token])
                self.assertEqual(row['blocks'], [])
                self.assertEqual(row['excerpt'], listing_excerpt)
                self.assertEqual(row['cover']['url'] if row['cover'] else None, listing_cover)
                self.assertIsNone(row['article_detail_evidence'])
                self.assertEqual(lib.sites[0]['checkpoint']['detail_samples'], 0)

    def test_current_detail_and_stored_repository_remain_available(self):
        text = '这是新版淘宝店铺课程，讲解如何给店铺上架商品。'
        fresh = detail(CURRENT, text, CURRENT_IMAGE)
        exact = library(article(), fresh)
        row = exact.semantic_rows[article_key(SITE, ARTICLE_ID)]
        self.assertEqual(row['cover']['url'], CURRENT_IMAGE)
        self.assertEqual(row['blocks'], [{'kind': 'text', 'text': text},
                                         {'kind': 'image', 'url': CURRENT_IMAGE, 'alt': ''}])
        self.assertEqual(row['article_detail_evidence']['publisher_modified'], CURRENT)
        self.assertIn({'id': 'taobao', 'label': '淘宝'}, row['platforms'])

        stored = make_intro(SITE, ARTICLE_ID, [text], publisher_modified=CURRENT,
                            source_kind='public_rest', github_links=(REPOSITORY,))
        projected = library(article(), fresh, stored).semantic_rows[article_key(SITE, ARTICLE_ID)]
        self.assertEqual(projected['blocks'], row['blocks'])
        self.assertEqual((projected['category'], projected['subcategory']), (row['category'], row['subcategory']))
        self.assertIn({'id': 'github', 'label': 'GitHub'}, projected['platforms'])
        self.assertNotIn(REPOSITORY, str(projected))

    def test_undated_legacy_detail_remains_visible(self):
        legacy = detail(None, '旧记录中可读取的公开课程简介。', OLD_IMAGE)
        row = library(article(), legacy).semantic_rows[article_key(SITE, ARTICLE_ID)]
        self.assertEqual(row['blocks'][0]['text'], legacy['excerpt'])
        self.assertEqual(row['cover']['url'], OLD_IMAGE)

    def test_merge_prefers_matching_detail_version_and_exposes_only_allowed_report_fields(self):
        old = detail(OLD, '旧简介', OLD_IMAGE, observed='2026-11-27T19:00:00+08:00')
        current = detail(CURRENT, '新简介', CURRENT_IMAGE)
        marker = {'published_through': CURRENT, 'modified_through': CURRENT,
                  'completed_at': '2026-11-27T18:00:00+08:00'}
        report_fields = {'modified_notice': None, 'introductions_saved': 4,
                         'introduction_failures': 0, 'discovery_status': 'complete'}
        doc = {'schema': 'wa-v4-codex-public-source.v1', 'site': SITE, 'history': {},
               'articles': {ARTICLE_ID: article()},
               'collection': {'read_only': True, **report_fields, 'private': 'ignore',
                              'last_discovery': {**marker, 'private': 'ignore'}}}
        for incoming, existing in ((old, current), (current, old)):
            with self.subTest(incoming=incoming['publisher_modified']):
                facts = {ARTICLE_ID: existing}
                with patch('catalog_model.read_bound', return_value=(
                        {**doc, 'details': {ARTICLE_ID: incoming}}, 'hash')):
                    checksum, report = merge_codex_collection(None, {}, facts)
                self.assertEqual(facts[ARTICLE_ID], current)
                self.assertEqual(checksum, 'hash')
                self.assertEqual(report['last_discovery'], marker)
                self.assertEqual({key: report[key] for key in report_fields}, report_fields)
                self.assertNotIn('private', report)
