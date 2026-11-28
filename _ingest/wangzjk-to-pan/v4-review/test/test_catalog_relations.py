"""Bounds and evidence gates for source folding and version display."""

from hashlib import sha256
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_model import Library
from catalog_relations import CatalogRelations, article_identity, candidate_pairs, version_compare
from catalog_semantics import CatalogStore, article_key, relationship_input, reusable_classification, source_input


def article(aid, title="教材示例", *, paragraphs=(), author=None, edition=None, version_date=None,
            platforms=(), scope="complete", published="2026-11-01", status="待分析", legacy=None):
    facts = {"author": author, "edition": edition, "scope": scope, "platforms": list(platforms), "dates": []}
    if version_date:
        raw, precision = version_date
        facts["dates"].append({"value": raw, "precision": precision, "meaning": "version", "text": raw})
    return {"id": legacy or f"rv_{aid}", "article_id": str(aid), "site_id": "catalog.test",
            "article_key": f"catalog.test/{aid}", "article_revision": f"arv_{aid}",
            "title": title, "site_label": "原站", "published_at": published, "analysis": facts,
            "paragraphs": list(paragraphs), "coverage": "complete" if paragraphs else "missing",
            "category": "content", "analysis_status": status,
            "input_hash": sha256(repr((aid, title, paragraphs, facts)).encode("utf-8")).hexdigest()}


def edge(left, right, kind, *, same_scope=True, newer=None, revoked=False):
    def ref(row):
        return {field: row[field] for field in ("site_id", "article_id", "input_hash")}
    return {"left": ref(left), "right": ref(right), "relation": kind, "same_scope": same_scope,
            "newer": newer, "evidence": [], "revoked": revoked}


class CandidateTests(unittest.TestCase):
    def test_rewritten_title_is_found_from_distinct_complete_passage(self):
        passage = (
            "先记录三栏睡眠观察数据并对齐设备分钟，按异常波形标注三种情况。"
            "第二章单独检验日记与遥测的一致性，再用颜色区分不同测量单位。"
            "第三章将实验表格连接到本地防误触界面，最后逐项复核记录缺漏。"
            "附件包含两种无信号时使用的对照图和重复记录处理练习。"
        )
        first = article(1, "用 Python 整理实验室记录", paragraphs=[passage])
        second = article(2, "夜间健康数据的留档与错误复查", paragraphs=[passage])
        other = article(3, "朋友圈运营", paragraphs=["咨询客服，点击下方获取资料。"])
        pairs, truncated = candidate_pairs((first, second, other), keys={article_identity(first)}, limit=3)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["right"]["article_id"], "2")
        self.assertIn("exact_text", pairs[0]["candidate_channels"])
        self.assertEqual(truncated, 0)
        self.assertEqual(len(CatalogRelations((first, second), ()).project((first, second))), 2)

    def test_short_template_is_not_exact_and_candidate_cap_is_recorded(self):
        rows = [article(n, "公众号课堂：手工复核竞品栏", paragraphs=["更多关注本站。"]) for n in range(7)]
        pairs, truncated = candidate_pairs(rows, keys={article_identity(rows[0])}, limit=2)
        self.assertEqual(len(pairs), 2)
        self.assertEqual(truncated, 4)
        self.assertTrue(all("exact_text" not in pair["candidate_channels"] for pair in pairs))


class ProjectionTests(unittest.TestCase):
    INTRO = [
        "先拆解原始经营流程，将每项操作对应到实际输入、输出文件和需要核实的参数条件，分别记录演示数据与正式经营数据。",
        "第一部分处理素材去向和客户复盘，第二部分对照现场执行清单、呈现条件和测试结果，第三部分实际还原参考模板及可替代路径。",
        "最后用五组事先记录的真实文件逐一复验，确认每种资料的展示位置、办理过程与必要授权，保存原版记录和复核结果。",
    ]

    def test_exact_complete_cross_site_text_uses_first_intake_but_preserves_source_selection(self):
        first = article(1, "展板设计与完整检查 2.0", paragraphs=self.INTRO, published="2026-11-27")
        later = {**article(2, first["title"], paragraphs=self.INTRO, published="2026-11-01"),
                 "site_id": "secondary.test", "article_key": "secondary.test/2"}
        seen = {first["article_key"]: "2026-11-27T12:00:00+08:00",
                later["article_key"]: "2026-11-28T12:00:00+08:00"}
        rows = (later, first)
        lib = CatalogRelations(rows, (), first_seen=seen)
        normal, = lib.project(rows)
        selected, = lib.project(rows, keep_selected={later["id"]})
        filtered, = lib.project((later,))
        self.assertEqual((normal["id"], selected["id"], filtered["id"]), (first["id"], later["id"], later["id"]))
        self.assertEqual(normal["display_id"], selected["display_id"])
        self.assertEqual(normal["other_sources"][0]["article_revision"], later["article_revision"])
        self.assertEqual(lib.stats()["exact_intro_folds"], 1)

    def test_exact_intro_requires_complete_and_no_conflict_or_withdrawal(self):
        first = article(1, "展板设计与完整检查 v2.0", paragraphs=self.INTRO, author="林峰", edition="v2.0")
        second = {**article(2, first["title"], paragraphs=self.INTRO, author="林峰", edition="2.0"),
                  "site_id": "secondary.test", "article_key": "secondary.test/2"}
        partial = {**second, "coverage": "partial"}
        author = {**second, "analysis": {**second["analysis"], "author": "赵琳"}}
        edition = {**second, "analysis": {**second["analysis"], "edition": "支线改订版"}}
        title = {**second, "title": first["title"].replace("2.0", "20")}
        for other, relationships in ((partial, ()), (author, ()), (edition, ()), (title, ()),
                                     (second, (edge(first, second, "same_version", revoked=True),)),
                                     (second, (edge(first, second, "uncertain"),))):
            with self.subTest(other=other["title"], relationships=relationships):
                rows = (first, other)
                self.assertEqual(len(CatalogRelations(rows, relationships).project(rows)), 2)

    def test_store_suppresses_exact_fold_after_operator_withdrawal(self):
        rows = (article(1, "展板设计与完整检查", paragraphs=self.INTRO),
                {**article(2, "展板设计与完整检查", paragraphs=self.INTRO), "site_id": "secondary.test"})
        inputs = [source_input(row, {"coverage":"complete", "clean_text":"\n".join(self.INTRO)}, {}) for row in rows]
        by_key = {row["article_key"]:row for row in inputs}
        relation = {"relation":"same_version", "same_scope":True, "reason":"同版资料实际核对",
                    "evidence":[{"side":side, "field":"paragraphs", "text":self.INTRO[0]} for side in ("left","right")]}
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as scratch:
            store = CatalogStore(Path(scratch) / "catalog.sqlite3")
            relation_id = store.correct_relation(*inputs, relation)
            store.undo(relation_id)
            self.assertEqual(store.read(by_key)[1], [])
            blocked = store.read(by_key, include_suppressed=True)[1]
            projected = [{**raw, **relationship_input(item)} for raw, item in zip(rows, inputs)]
            self.assertEqual(len(CatalogRelations(projected, blocked).project(projected)), 2)

    def test_exact_alias_reuses_text_verdict_without_extending_version_or_evidence_history(self):
        intro = [self.INTRO[0] + "本课演示在抖音发布制作完成的短视频。", *self.INTRO[1:]]
        first = article(1, "展板设计与完整检查", paragraphs=intro)
        second = {**article(2, first["title"], paragraphs=intro), "site_id":"secondary.test"}
        lib = Library.__new__(Library)
        lib.raw_rows = [{**row, "excerpt":"", "blocks":[], "source_categories":[]} for row in (first,second)]
        lib.adapters = {row["site_id"]:Mock() for row in lib.raw_rows}
        for adapter in lib.adapters.values():
            adapter.catalog_evidence.return_value = ({}, None)
        keys = [article_key(row["site_id"], row["article_id"]) for row in lib.raw_rows]
        store = Mock()
        verdict = {"category":"media", "subcategory":"短视频制作", "secondary_category":"",
                   "author":"林峰", "edition":"2.0",
                   "platforms":[{"id":"douyin", "role":"use", "text":intro[0]}],
                   "evidence":[{"field":"paragraphs", "text":intro[0]}]}
        store.read.return_value = ({keys[0]:("agent",verdict)}, [], 0)
        intros = {(row["site_id"],row["article_id"]):{"coverage":"complete","clean_text":"\n".join(intro)}
                  for row in lib.raw_rows}
        lib.attach_catalog(store, intros)
        target = lib.semantic_rows[keys[1]]
        self.assertEqual(target["classification_state"], "reused")
        self.assertEqual(lib.reused_classifications, {keys[1]})
        self.assertEqual(target["platforms"][0]["id"], "douyin")
        self.assertEqual(target["analysis"], {})
        self.assertEqual(target["input_hash"], relationship_input(lib.semantic_inputs[keys[1]])["input_hash"])
        self.assertNotIn(keys[1], lib.semantic_classified)
        donor, source = (lib.semantic_inputs[key] for key in keys)
        self.assertIsNone(reusable_classification(donor, {**source,"excerpt":"不同发布说明"}, verdict))
        self.assertIsNone(reusable_classification(donor, source, {**verdict, "evidence":[{"field":"excerpt","text":"说明"}]}))

    def test_full_source_group_has_direct_bindings_and_filter_sensitive_representative(self):
        first = article(1, "新版展板设计", published="2026-01-01")
        repost = article(2, "原作者的展板课程", paragraphs=["完整章节与原作者介绍"], published="2026-11-22")
        library = CatalogRelations((first, repost), [edge(first, repost, "same_version")])
        complete = library.project((first, repost))
        single = library.project((first,))
        self.assertEqual([row["article_id"] for row in complete], ["2"])
        self.assertEqual([row["article_id"] for row in single], ["1"])
        self.assertEqual(single[0]["published_at"], "2026-01-01")
        self.assertEqual(single[0]["display_id"], complete[0]["display_id"])
        self.assertEqual(single[0]["source_count"], 1)
        self.assertEqual(library.details(first)["other_sources"][0]["article_key"], repost["article_key"])
        self.assertEqual(library.stats()["folded_sources"], 1)

    def test_later_repost_does_not_rename_the_established_version(self):
        first, second, later = [article(n, f"同版展板设计{n}") for n in (20, 30, 10)]
        original = edge(first, second, "same_version")
        previous = CatalogRelations((first, second), (original,)).details(first)
        family = CatalogRelations((first, second, later), (
            original, edge(first, later, "same_version"), edge(second, later, "same_version"),
        ))
        next_row = family.details(first)
        self.assertEqual(previous["display_id"], next_row["display_id"])
        self.assertEqual(next_row["source_count"], 3)

    def test_same_author_is_not_the_same_course_and_scope_conflicts_do_not_fold(self):
        first = article(1, author="林峰", scope="complete")
        second = article(2, author="赵琳", scope="complete")
        sample = article(3, author="林峰", scope="sample")
        separate = article(4, title="同作者的其他课程", author="林峰", scope="complete")
        rows = (first, second, sample, separate)
        lib = CatalogRelations(rows, (edge(first, second, "same_version"), edge(first, sample, "same_version"),
                                    edge(first, separate, "related")))
        self.assertEqual(len(lib.project(rows)), 4)
        self.assertEqual(lib.stats()["pending_relations"], 2)

    def test_version_unknown_bridge_does_not_join_distinct_editions(self):
        old = article(1, edition="2025版")
        bridge = article(2)
        new = article(3, edition="2026版")
        rows = (old, bridge, new)
        lib = CatalogRelations(rows, (edge(old, bridge, "same_version"), edge(bridge, new, "same_version")))
        self.assertEqual(len(lib.project(rows)), 2)
        self.assertNotEqual(lib.details(old)["display_id"], lib.details(new)["display_id"])
        self.assertEqual(lib.stats()["pending_relations"], 1)

    def test_course_order_is_distinct_from_publication_and_keeps_exact_actions(self):
        same_resource = "rv_legacy"
        old = article(1, edition="2025版", published="2026-11-27", status="已验收", legacy=same_resource)
        new = article(2, edition="2026版", published="2026-01-01", legacy=same_resource)
        rows = (old, new)
        lib = CatalogRelations(rows, (edge(old, new, "different_version"),))
        page = lib.project(rows)
        self.assertEqual([row["article_id"] for row in page], ["2"])
        self.assertEqual(page[0]["analysis_status"], "待分析")
        self.assertTrue(page[0]["action_pending"])
        self.assertEqual(page[0]["other_versions"][0]["article_key"], old["article_key"])
        self.assertEqual(lib.project((old,))[0]["analysis_status"], "已验收")
        self.assertEqual(lib.stats()["hidden_older_versions"], 1)

    def test_selected_older_version_preserves_binding(self):
        old, new = article(1, edition="第一版"), article(2, edition="第二版")
        rows = (old, new)
        lib = CatalogRelations(rows, (edge(old, new, "different_version"),))
        self.assertEqual([row["id"] for row in lib.project(rows, keep_selected={old["id"]})], [old["id"], new["id"]])
        self.assertEqual(lib.project(rows)[0]["id"], new["id"])

    def test_platform_specializations_remain_parallel_even_when_marked_same_scope(self):
        old = article(1, edition="2025版", platforms=({"id": "xhs", "role": "use"},))
        new = article(2, edition="2026版", platforms=({"id": "douyin", "role": "acquire"},
                                                     {"id": "xhs", "role": "background"}))
        rows = (old, new)
        lib = CatalogRelations(rows, (edge(old, new, "different_version"),))
        self.assertEqual(len(lib.project(rows)), 2)
        self.assertEqual(lib.project((old,))[0]["article_id"], "1")
        self.assertEqual(lib.details(old)["version_count"], 2)

    def test_provider_republication_is_not_a_version_date_and_precision_must_be_disjoint(self):
        unknown = article(0, published="2027-10-10")
        yr = article(1, version_date=("2026", "year"))
        mo = article(2, version_date=("2026-11", "month"))
        earlier = article(3, version_date=("2025-03", "month"))
        self.assertEqual(version_compare(yr, unknown), 0)
        self.assertEqual(version_compare(yr, mo), 0)
        self.assertEqual(version_compare(yr, earlier), 1)
        conflict = article(4, edition="第3版", version_date=("2025-10", "month"))
        latter = article(5, edition="第2版", version_date=("2026-10", "month"))
        self.assertEqual(version_compare(conflict, latter), 0)
        self.assertEqual(version_compare(article(6, edition="2026版"), article(7, edition="v2.0")), 0)

    def test_stale_and_revoked_edges_stop_folding(self):
        first, second = article(1), article(2)
        old = edge(first, second, "same_version")
        changed = {**second, "input_hash": "updated"}
        stale = CatalogRelations((first, changed), (old,))
        self.assertEqual(stale.stats()["stale_relations"], 1)
        self.assertEqual(len(stale.project((first, changed))), 2)
        revoked = CatalogRelations((first, second), (old, edge(first, second, "same_version", revoked=True)))
        self.assertEqual(len(revoked.project((first, second))), 2)

    def test_independent_course_cannot_join_a_series_through_a_bridge(self):
        a, b, c = [article(n, edition=f"第{n}版") for n in (1, 2, 3)]
        rows = (a, b, c)
        lib = CatalogRelations(rows, (edge(a, b, "different_version"), edge(b, c, "different_version"),
                                     edge(a, c, "related")))
        self.assertNotEqual(lib.details(a)["series_id"], lib.details(c)["series_id"])
        self.assertEqual(lib.stats()["conflicting_relations"], 1)

    def test_conflicting_edition_direction_stays_visible_and_is_counted(self):
        first, second = article(1, edition="第一版"), article(2, edition="第二版")
        library = CatalogRelations((first, second), (edge(first, second, "different_version", newer="left"),))
        self.assertEqual(library.stats()["pending_version_order"], 1)
        self.assertEqual(len(library.project((first, second))), 2)

    def test_only_a_directly_confirmed_visible_version_can_hide_older_one(self):
        a, b, c = [article(n, edition=f"第{n}版") for n in (1, 2, 3)]
        rows = (a, b, c)
        lib = CatalogRelations(rows, (edge(a, b, "different_version"), edge(b, c, "different_version")))
        self.assertEqual([row["article_id"] for row in lib.project(rows)], ["1", "3"])
        self.assertEqual([row["version_label"] for row in lib.details(c)["other_versions"]], ["第2版", "第1版"])


if __name__ == "__main__":
    unittest.main()
