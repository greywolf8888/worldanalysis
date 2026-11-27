"""Scope display uses exact note, publication, independent tester and reader evidence."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_tools/course_production"))

from independent_facts import review_request
from project import CourseProject, ENTRY, named_entry, put, read, sha, write
from scope_review import add_scope_summary, SCHEMA
from source_coverage import CLAIMS_VERSION
from vault_catalog import CATALOG, CATALOG_SOURCES, refresh_catalog
from vault_dates import HISTORICAL_STATUS, UNBOUND_STATUS, UNCONFIRMED_STATUS
from vault_delivery import confirm, install


class ScopeCatalogTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.vault = self.root / "阅读库"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.catalog = self.vault / CATALOG

    def course(self, name, status=None, gap_kind=None, independent=True, confirmed=True):
        raw = self.root / "原件" / name
        write(raw / "教案.txt", "准备课堂示例，并核对步骤。\n")
        project = CourseProject.initialize(raw, self.root / "项目包" / name)
        body = f"# {name}\n\n## 实操\n\n准备课堂示例，并核对步骤。\n"
        review = {"snapshot": {"evidence": {"ledger/intake.json": sha(project.intake)}}}
        top = {}
        if status:
            gaps = [] if not gap_kind else [{"item": "补充课程材料.pdf", "kind": gap_kind,
                "source": "教案.txt 教学说明中的配套材料提示", "impact": "该未收到的材料不在本版的讲解范围内",
                "obtain": "向原资料发布者核实其原件是否有公开的合法授权版本"}]
            declaration = {"scope_status": status, "scope_gaps": gaps, "scope_resolutions": [],
                "scope_teaching": [{"source": "教案.txt 课件原文", "anchor": "准备课堂示例",
                    "observation": "模拟原件中有明确课堂示例和步骤核对要求"}]}
            body = add_scope_summary(body, declaration)
            top = {key: declaration[key] for key in ("scope_status", "scope_gaps", "scope_resolutions")}
            review.update({**declaration, "schema": 2, "scope_review_schema": SCHEMA,
                "source_scope": {"documents": [], "videos": []},
                "scope_document_candidates": [], "scope_prior": None,
                "source_coverage": {"claims_version": CLAIMS_VERSION, "claims": []}})
            if independent:
                tester = review_request(body, review)
                tester.update(reviewer={"role": "测试agent", "thread_id": "catalog-scope-isolated-fixture",
                    "reviewed_at": "2026-11-26T01:00:00+08:00"}, decision="通过")
                scope = tester["scope_acceptance"]
                scope.update(decision="通过", coverage_observation="仅为独立的合成测试夹具，原件和正文边界明确")
                for row in [*scope["gaps"], *scope["teaching"], *scope["resolutions"]]:
                    row.update(decision="通过", review_observation="仅为合成回归测试中的独立范围证据")
                review["independent_fact_review"] = tester
        write(project.entry, body)
        digest = sha(project.entry)
        record = {**top, "body": body, "outputs": {ENTRY.as_posix(): digest}, "review": review,
                  "analysis_completed_at": "2026-11-26T01:00:00+08:00",
                  "obsidian": {"checks": [{"document_sha256": digest, "reachedBottom": True}]}}
        put(project.ledger / f"publications/{digest}.json", record)
        state = install(project, record, self.vault)
        if confirmed:
            self.reader(project, record, state)
        return project, record, state

    def reader(self, project, record, state):
        return confirm(project, record, {**{key: state[key] for key in ("entry", "sha256", "installed_at")},
            "mode": "preview", "reopened": True, "reached_bottom": True, "references_collapsed": True,
            "visible_citations": False, "section_seen": "实操", "note": "同版阅读到页末的合成测试夹具"})

    def page(self):
        return self.catalog.read_text(encoding="utf-8")

    def lines(self, name):
        return [line for line in self.page().splitlines() if line.startswith(f"- [{name}]")]

    def assert_status(self, name, *labels):
        lines = self.lines(name)
        self.assertEqual(len(lines), 2)
        for label in labels:
            self.assertTrue(all(label in line for line in lines), (label, lines))

    def row(self, sequence, project, state, status, **extras):
        return {"id": str(sequence), "sequence": sequence, "course_folder": project.folder.name,
            "package": str(project.folder), "status": status,
            "publication": {"entry": state["entry"], "sha256": state["sha256"]}, **extras}

    def test_scope_state_and_no_false_counts(self):
        first, frecord, fstate = self.course("合成完整课", "完整验收", confirmed=False)
        self.assert_status(first.folder.name, UNCONFIRMED_STATUS)
        refresh_catalog(self.vault)
        self.assert_status(first.folder.name, UNCONFIRMED_STATUS)
        self.reader(first, frecord, fstate)
        self.assert_status(first.folder.name, "状态 完整验收")
        partial, precord, pstate = self.course("合成配套待补课", "配套待补", "非核心")
        core, _, _ = self.course("合成核心待补课", "核心待补", "核心")
        untrusted, _, _ = self.course("合成无测试签收课", "完整验收", independent=False)
        self.assert_status(partial.folder.name, "状态 配套待补（1项）")
        self.assert_status(core.folder.name, "状态 核心待补（1项）")
        self.assert_status(untrusted.folder.name, UNBOUND_STATUS)
        self.assertIn("完整验收 1；配套待补 1；核心待补 1", self.page())

        # Altering only top-level metadata must not promote a partial course to full.
        receipt = partial.ledger / f"publications/{pstate['sha256']}.json"
        put(receipt, {**precord, "scope_status": "完整验收", "scope_gaps": []})
        refresh_catalog(self.vault)
        self.assert_status(partial.folder.name, UNBOUND_STATUS)
        self.assertIn("完整验收 1；配套待补 0；核心待补 1", self.page())
        # A receipt for another document identity cannot be borrowed either.
        wrong = dict(precord["review"]["independent_fact_review"], body_sha256="0" * 64)
        put(receipt, {**precord, "review": {**precord["review"], "independent_fact_review": wrong}})
        refresh_catalog(self.vault)
        self.assert_status(partial.folder.name, UNBOUND_STATUS)
        put(receipt, precord)
        refresh_catalog(self.vault)
        self.assert_status(partial.folder.name, "状态 配套待补（1项）")

        # Replacing the current reader note must invalidate the old explicit status and dates.
        write(Path(fstate["entry"]), "# 用户新编辑稿，尚未独立复核\n")
        refresh_catalog(self.vault)
        self.assert_status(first.folder.name, UNBOUND_STATUS, "分析 待核实")
        self.assertIn("完整验收 0；配套待补 1；核心待补 1", self.page())

    def test_legacy_and_batch_status_survive_empty_refresh(self):
        legacy, _, lstate = self.course("合成历史旧验收课")
        incomplete, _, istate = self.course("合成历史第58项")
        partial, _, pstate = self.course("合成新范围配套课", "配套待补", "非核心")
        manifest = self.root / "正式模拟批次.json"
        rows = [self.row(1, legacy, lstate, "已验收", source_gap={"item": "旧签收来源提示"}),
            self.row(58, incomplete, istate, "待补原件"),
            self.row(59, partial, pstate, "已验收（配套待补）"),
            {"id": "alias", "sequence": 60, "course_folder": "合成同源项目", "status": "同源复用",
                "package": str(legacy.folder), "publication": {"entry": lstate["entry"], "sha256": lstate["sha256"]}},
            {"id": "archive", "sequence": 61, "course_folder": "合成留档项目", "status": "留档"},
            {"id": "missing", "sequence": 62, "course_folder": "合成缺件项目", "status": "待补原件"},
            {"id": "not-installed", "sequence": 63, "course_folder": "合成未入库签收", "status": "已验收"}]
        put(manifest, {"vault": str(self.vault), "projects": rows})
        refresh_catalog(self.vault, manifests=(manifest,))
        self.assert_status(legacy.folder.name, HISTORICAL_STATUS, "旧验收", "来源待补（台账）")
        self.assert_status(incomplete.folder.name, HISTORICAL_STATUS, "台账 待补原件")
        self.assert_status(partial.folder.name, "状态 配套待补（1项）")
        self.assertIn("已发布 **3** 个项目", self.page())
        self.assertIn("共 **4** 个批次记录（不计入已发布入口）", self.page())
        self.assertIn("合成同源项目 · 状态 同源复用", self.page())
        self.assertIn("合成留档项目 · 状态 留档", self.page())
        self.assertIn("合成缺件项目 · 状态 待补原件", self.page())
        self.assertIn("合成未入库签收 · 状态 已验收（入口待核）", self.page())
        self.assertEqual(set(read(self.vault / CATALOG_SOURCES)["manifests"]), {str(manifest)})
        before = self.catalog.read_bytes()
        refresh_catalog(self.vault)
        self.assertEqual(before, self.catalog.read_bytes())
        rows[5]["status"] = "处理中"
        rows[1]["status"] = "待处理"
        rows[1]["re_review"] = {"state": "待复审", "gaps_before_review": "旧范围另缺完整来源"}
        rows.append({"id": "pending-alias", "sequence": 64, "course_folder": "同源待补附属项目",
                     "status": "待补原件", "same_source_primary": 58})
        put(manifest, {"vault": str(self.vault), "projects": rows})
        refresh_catalog(self.vault)
        self.assertIn("合成缺件项目 · 状态 处理中", self.page())
        self.assert_status(legacy.folder.name, HISTORICAL_STATUS, "旧验收", "来源待补（台账）")
        self.assert_status(incomplete.folder.name, HISTORICAL_STATUS, "台账 待复审", "来源待补（台账）")
        self.assertIn("同源待补附属项目 · 状态 同源待补 · 关联第 058 项", self.page())
        lines = [line for line in self.page().splitlines() if line.startswith("- [")]
        self.assertEqual(len(lines), 6)
        for line in lines:
            target = unquote(line.split("](", 1)[1].split(")", 1)[0])
            self.assertTrue((self.catalog.parent / target).resolve().is_file())
        self.assertEqual(len(list(self.vault.rglob("*.md"))), 4)


if __name__ == "__main__":
    unittest.main()
