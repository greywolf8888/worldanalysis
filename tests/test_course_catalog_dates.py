"""Date views are based on exact note editions and explicit local receipt evidence."""
from pathlib import Path
import os
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_tools/course_production"))

from project import CourseProject, ENTRY, named_entry, put, read, sha, write
from vault_catalog import CATALOG, START, END, refresh_catalog
from vault_dates import DATE_INDEX
from vault_delivery import confirm, install


class DateCatalogTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.vault = self.root / "日常库"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.catalog = self.vault / CATALOG

    def course(self, name, stamp=None, confirmed=True):
        original = self.root / "课源" / name
        write(original / "核心课.txt", "作者的核心课件\n")
        project = CourseProject.initialize(original, self.root / "课程包" / name)
        body = f"# {name}\n\n## 实操\n\n准备课件。\n"
        write(project.entry, body)
        digest = sha(project.entry)
        record = {"body": body, "outputs": {ENTRY.as_posix(): digest},
                  "review": {"snapshot": {"evidence": {"ledger/intake.json": sha(project.intake)}}},
                  "obsidian": {"checks": [{"document_sha256": digest, "reachedBottom": True}]}}
        if stamp:
            record["analysis_completed_at"] = stamp
        put(project.ledger / f"publications/{digest}.json", record)
        state = install(project, record, self.vault)
        if confirmed:
            self.confirm(project, record, state)
        return project, record, state

    def confirm(self, project, record, state):
        return confirm(project, record, {
            **{k: state[k] for k in ("entry", "sha256", "installed_at")},
            "mode": "preview", "reopened": True, "reached_bottom": True, "references_collapsed": True,
            "visible_citations": False, "section_seen": "实操", "note": "真实同版回执夹具"})

    def page(self):
        return self.catalog.read_text(encoding="utf-8")

    def production_record(self, project, state, day, certainty="exact", complete=True):
        return {"package": str(project.folder), "edition_sha256": state["sha256"],
                "all_core_dates_verified": complete,
                "sources": [{"file": "核心课.txt", "sha256": sha(project.raw / "核心课.txt"),
                             "date": day, "certainty": certainty, "evidence": "原件显式日期的核对夹具"}]}

    def test_unknown_until_same_edition_reader_acceptance_and_no_mtime(self):
        project, record, state = self.course("甲课", "2026-11-24T16:01:00+00:00", confirmed=False)
        self.assertIn("分析 待核实", self.page())
        self.assertFalse((self.vault / DATE_INDEX).exists())
        self.confirm(project, record, state)
        self.assertIn("分析 2026-11-25", self.page())
        self.assertIn("制作 待核实", self.page())
        snapshot = self.catalog.read_bytes()
        os.utime(Path(state["entry"]), (100000000, 100000000))
        self.confirm(project, record, state)
        refresh_catalog(self.vault)
        self.assertEqual(self.catalog.read_bytes(), snapshot)

    def test_two_static_views_and_production_scope(self):
        first, _, fstate = self.course("A 先分析", "2026-11-20T02:00:00+00:00")
        second, _, sstate = self.course("B 后分析", "2026-11-24T02:00:00+00:00")
        self.course("C 无日期")
        evidence = self.root / "制作证据.json"
        put(evidence, {"schema": 1, "entries": [
            self.production_record(first, fstate, "2025-10-10"),
            self.production_record(second, sstate, "2024-07-02", certainty="estimated", complete=False)]})
        old = f"# 项目总览\n\n[独立测试稿](obsidian://test)\n\n{START}\n{END}\n手工笔记。\n"
        write(self.catalog, old)
        refresh_catalog(self.vault, production_evidence=[evidence])
        page = self.page()
        analysis, production = page.split("## 按制作日期（新到旧）")
        self.assertLess(analysis.index("- [B 后分析]"), analysis.index("- [A 先分析]"))
        self.assertLess(analysis.index("- [A 先分析]"), analysis.index("- [C 无日期]"))
        self.assertLess(production.index("- [A 先分析]"), production.index("- [B 后分析]"))
        self.assertLess(production.index("- [B 后分析]"), production.index("- [C 无日期]"))
        self.assertIn("制作 2024-07-02（约，已知最早）", page)
        self.assertIn("[独立测试稿](obsidian://test)", page)
        self.assertTrue(page.endswith("手工笔记。\n"))
        self.assertEqual(page.count("- [A 先分析]"), 2)

    def test_legacy_same_edition_date_and_batch_reanalysis(self):
        old, _, state = self.course("历史旧课")
        new, _, nstate = self.course("历史修订课")
        manifest = self.root / "批次.json"
        item = self.root / "001-已验收项目.json"
        report = self.root / "验收结果.json"
        put(report, {"verified": True, "package_verified": True,
                     "vault_delivery": {"entry": state["entry"], "sha256": state["sha256"]}})
        put(item, {"入口": state["entry"], "版本": state["sha256"], "日期时间": "2026-11-01T12:00:00+08:00",
                   "交付回执": str(report)})
        rows = [
            {"sequence": 1, "status": "已验收", "package": str(old.folder), "last_changed": "2026-11-24T00:00:00+00:00",
             "publication": {"sha256": state["sha256"]}, "legacy_record": str(item),
             "history": [{"status": "历史验收待复验", "publication": {"sha256": state["sha256"]}}]},
            {"sequence": 2, "status": "已验收", "package": str(new.folder), "last_changed": "2026-11-23T12:00:00+08:00",
             "publication": {"sha256": nstate["sha256"]},
             "history": [{"status": "处理中", "publication": None}]}]
        put(manifest, {"vault": str(self.vault), "projects": rows})
        refresh_catalog(self.vault, manifests=[manifest])
        self.assertIn("分析 2026-11-01", self.page())
        self.assertIn("分析 2026-11-23", self.page())
        rows[1]["history"].append({k: v for k, v in rows[1].items() if k != "history"})
        rows[1]["last_changed"] = "2026-11-30T00:00:00+00:00"
        put(manifest, {"vault": str(self.vault), "projects": rows})
        refresh_catalog(self.vault, manifests=[manifest])
        self.assertNotIn("分析 2026-11-30", self.page())
        # Same path with different bytes must no longer display dated proof.
        write(Path(nstate["entry"]), "# 手工改写尚未核验\n")
        refresh_catalog(self.vault)
        self.assertNotIn("分析 2026-11-23", self.page())

    def test_production_conflict_rejected(self):
        project, _, state = self.course("制作证据冲突")
        record = self.production_record(project, state, "2025-10-01")
        record["sources"].append({**record["sources"][0], "date": "2025-10-03"})
        evidence = self.root / "错误制作日期.json"
        put(evidence, {"schema": 1, "entries": [record]})
        before = self.catalog.read_bytes()
        with self.assertRaisesRegex(ValueError, "冲突"):
            refresh_catalog(self.vault, production_evidence=[evidence])
        self.assertEqual(self.catalog.read_bytes(), before)
        self.assertIn("制作 待核实", self.page())

    def test_publication_stamp_only_on_new_semantic_edition(self):
        from acceptance import commit
        from source_coverage import CLAIMS_VERSION
        source = self.root / "新稿原件"
        write(source / "资料.txt", "课源")
        project = CourseProject.initialize(source, self.root / "新稿包")
        body = "# 内容\n\n## 学习\n\n原件正文。\n"
        write(project.preview / ENTRY, body)
        digest = sha(project.preview / ENTRY)
        review = {"schema": 2, "source_scope": {"fixture": True},
                  "source_coverage": {"claims_version": CLAIMS_VERSION},
                  "scope_status": "完整验收", "scope_gaps": [], "scope_resolutions": []}
        render = {"checks": [{"document_sha256": digest, "mode": "preview", "reachedBottom": True,
                              "horizontalOverflow": False, "visibleCitations": [],
                              "closedAppendices": ["原课与配套资料"]}]}
        # This fixture tests only timestamp semantics; the real new scope and
        # independent evidence gates have their own end-to-end tests.
        with (patch("acceptance.datetime") as clock,
              patch("acceptance.validate_source_coverage"), patch("acceptance.validate_scope_review"),
              patch("acceptance.load_independent_receipt", return_value=None),
              patch("acceptance.publication_receipt")):
            clock.now.return_value = datetime(2026, 11, 24, tzinfo=timezone.utc)
            commit(project, body, [], review, render)
            receipt = project.ledger / f"publications/{digest}.json"
            first = read(receipt)
            self.assertEqual(first["analysis_completed_at"], "2026-11-24T00:00:00+00:00")
            clock.now.return_value = datetime(2026, 11, 25, tzinfo=timezone.utc)
            commit(project, body, [], review, render)
            self.assertEqual(read(receipt)["analysis_completed_at"], first["analysis_completed_at"])
            first.pop("analysis_completed_at")
            put(receipt, first)
            commit(project, body, [], review, render)
            self.assertNotIn("analysis_completed_at", read(receipt))


if __name__ == "__main__":
    unittest.main()
