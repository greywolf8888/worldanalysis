"""The course calendar uses calendar dates and exact checked source identities."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from catalog_model import Library, ORIGIN, SITE, Wangzjk, digest
from catalog_semantics import CatalogStore
from course_calendar import CalendarRequestError, PAGE_SIZE, course_calendar, is_course


def library(last_article=52):
    articles = {}
    for number in range(1, last_article + 1):
        article_id = str(number)
        day = "2026-01-12" if number <= 50 else "2026-01-01" if number == 51 else "2026-01-13"
        articles[article_id] = {
            "article_id": article_id,
            "url": f"{ORIGIN}/{article_id}.html",
            "published_at": day,
            "title": f"测试资料 {article_id}",
            "shares": [],
            "categories": [],
            "access_state": "public",
        }
    fixture = Library([Wangzjk({
        "catalog.json": {"articles": articles},
        "journal.json": {"operations": {}},
        "downloaded-project-markers.json": {"entries": []},
        "capacity-policy.json": {"minimum_free_bytes": 100_000_000_000},
        "crawl_state.JSON": {},
    }, {})])
    fixture.snapshot_id = "calendar-fixture-" + str(last_article)
    return fixture


class CourseCalendar(unittest.TestCase):
    def test_catalog_classifies_each_site_from_its_own_evidence(self):
        primary = library(last_article=1).adapters[SITE]
        primary.articles['1']['title'] = '抖音创作者计划实操课程'

        class SecondSite:
            site_id = 'source.example'
            site_label = '第二资料来源'
            entry_origin = 'https://source.example'

            def discover(self, cursor, scope):
                return ['1'], None

            def checkpoint(self):
                return {'site_id': self.site_id}

            def project_page_url(self, article_id):
                return f'{self.entry_origin}/articles/{article_id}'

            def read_article(self, article_id):
                base = primary.read_article(article_id)
                identity = digest([self.site_id, article_id])
                url = self.project_page_url(article_id)
                title = '开源视频压缩工具'
                source = {**base['sources'][0], 'site_id': self.site_id, 'url': url, 'title': title,
                          'article_revision': 'arv_' + identity}
                return {**base, 'site_id': self.site_id, 'site_label': self.site_label, 'url': url,
                        'id': 'rv_' + identity, 'project_id': 'p_' + identity, 'title': title,
                        'article_revision': source['article_revision'], 'sources': [source]}

            def catalog_evidence(self, article_id):
                return {}, {'clean_text': '这款工具在电脑上批量压缩视频。', 'coverage': 'complete',
                            'source_kind': 'public_rest', 'github_links': ('https://github.com/example/media-tool',)}

        catalog = Library([primary, SecondSite()])
        catalog.snapshot_id = 'two-source-catalog'
        with TemporaryDirectory() as temporary:
            catalog.attach_catalog(CatalogStore(Path(temporary) / 'catalog.sqlite3'), {})
        self.assertEqual({row['site_id']: row['category'] for row in catalog.semantic_rows.values()},
                         {SITE: 'growth', SecondSite.site_id: 'software'})
        with patch('course_calendar.read_non_projects', return_value={}):
            result = course_calendar(catalog, 'category=software&platform=github')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['platforms'], [{'id': 'github', 'label': 'GitHub'}])

    def test_new_news_brief_stays_out_without_a_manual_exclusion(self):
        source = [{"site_id":SITE,"article_id":"1","article_revision":"arv-test"}]
        self.assertFalse(is_course({"category":"news","subcategory":"新闻简报","sources":source},{}))
        self.assertTrue(is_course({"category":"commerce","subcategory":"店铺运营","sources":source},{}))

    def test_cursor_date_and_revision_scoped_exclusions(self):
        original = library()
        by_article = {row["article_id"]: row for row in original.by_id.values()}
        exclusions = {
            (SITE, "52", by_article["52"]["article_revision"]): "site_notice",
            (SITE, "51", "arv_" + "0" * 64): "daily_news",
        }
        with patch("course_calendar.read_non_projects", return_value=exclusions):
            original.share_locations[(SITE, "50")] = ("private test locator",)
            first = course_calendar(original, "")
            self.assertEqual(first["total"], 51)
            self.assertEqual(len(first["items"]), PAGE_SIZE)
            self.assertEqual(first["items"][0]["id"], by_article["50"]["id"])
            self.assertTrue(first["items"][0]["has_baidu"])
            self.assertNotIn("private test locator", str(first))

            after = urlencode({"after": first["next"]})
            second = course_calendar(original, after)
            self.assertEqual(second["total"], 51)
            self.assertEqual([row["id"] for row in second["items"]],
                             [by_article[article]["id"] for article in ("2", "1", "51")])
            self.assertIsNone(second["next"])
            with self.assertRaises(CalendarRequestError) as updated_catalog:
                course_calendar(library(last_article=53), after)
            self.assertEqual(updated_catalog.exception.status, 409)

            dated = course_calendar(original, "day=2026-01-02")
            self.assertEqual([row["id"] for row in dated["items"]], [by_article["51"]["id"]])
            with self.assertRaises(CalendarRequestError) as bad_date:
                course_calendar(original, "day=2026-02-30")
            self.assertEqual(bad_date.exception.status, 400)
            with self.assertRaises(CalendarRequestError) as stale_cursor:
                course_calendar(original, "day=2026-01-02&" + after)
            self.assertEqual(stale_cursor.exception.status, 409)
