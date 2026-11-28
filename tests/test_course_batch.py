"""Exercise batch ordering, reuse and migration with isolated local files."""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_tools/course_production"))
import batch
from project import CourseProject, ENTRY, put, sha
from reader_review import snapshot
from independent_facts import identity as fact_review_identity, review_request
from scope_review import add_scope_summary
from source_coverage import coverage_review_template, fingerprint
from vault_delivery import confirm, install


class CourseBatchTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.source = self.root / "储存"
        self.source.mkdir()
        self.vault = self.root / "日常库"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.state = self.root / "批次.json"
        self.parser = ArgumentParser()
        batch.add_arguments(self.parser.add_subparsers(dest="action"))

    def command(self, *args, success=True):
        args = self.parser.parse_args(["batch", *map(str, args)])
        out, error = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            code = batch.run(args)
        self.assertEqual(code == 0, success, out.getvalue() + error.getvalue())
        return json.loads(out.getvalue()) if success else error.getvalue()

    def source_folder(self, name, data):
        path = self.source / name
        (path / "资料").mkdir(parents=True)
        (path / "资料/说明.md").write_text(data, encoding="utf-8")
        return path

    def existing_publication(self, source, package, *, vault=None, folder=None, observed=True, legacy=False,
                             scope_status="完整验收", scope_gaps=(), unclassified=False, scope_resolutions=()):
        project = CourseProject.initialize(source, package)
        prior_scope = None
        if project.entry.is_file():
            old_sha = sha(project.entry)
            previous = batch.read(project.ledger / f"publications/{old_sha}.json")
            if previous.get("scope_gaps"):
                prior_scope = {"publication_sha256": old_sha, "gaps": previous["scope_gaps"]}
        document = "资料/说明.md"
        text = f"# 项目\n\n## 实操\n\n准备资料。 <!-- 段落出处 {document} · 首段 -->\n"
        declaration = None
        if not legacy and not unclassified:
            declaration = {"scope_status": scope_status, "scope_gaps": list(scope_gaps),
                           "scope_resolutions": list(scope_resolutions),
                           "scope_teaching": [{"source": document + " · 首段", "anchor": "准备资料",
                                                "observation": "本地原说明记录了准备资料的实际步骤"}]}
            text = add_scope_summary(text, declaration)
        project.entry.parent.mkdir(exist_ok=True)
        project.entry.write_bytes(text.encode("utf-8"))
        body_sha = sha(project.entry)
        record = {
            "body": text, "outputs": {ENTRY.as_posix(): body_sha},
            "review": {"episodes": [], "snapshot": {
                **snapshot(project.folder, text), "evidence": {"ledger/intake.json": sha(project.intake)}}},
            "obsidian": {"checks": [{
                "document_sha256": body_sha, "mode": "preview", "reachedBottom": True,
                "horizontalOverflow": False, "visibleCitations": [],
                "closedAppendices": ["原课与配套资料"]}]}}
        if not legacy:
            scope = {"intake_sha256": sha(project.intake), "unreadable": [], "videos": [], "documents": [{
                "id": "D001", "source": document, "candidates": [],
                "units": [{"key": "file", "kind": "file", "value": None}]}]}
            coverage = coverage_review_template(scope, text)
            coverage["documents"][0]["units"][0].update(
                outcome="正文已覆盖", note="核对原说明的首段", anchor="准备资料")
            coverage["claims"][0].update(outcome="原件已核", note="已对照原说明")
            record["review"].update(schema=2, source_scope=scope, source_coverage=coverage)
            record["review"]["snapshot"]["source_scope_sha256"] = fingerprint(scope)
        if declaration is not None:
            record.update(scope_status=scope_status, scope_gaps=list(scope_gaps),
                          scope_resolutions=list(scope_resolutions))
            review = record["review"]
            review.update(scope_review_schema=1, scope_document_candidates=["D001"],
                          scope_prior=prior_scope, **declaration)
            receipt = review_request(text, review)
            receipt.update(reviewer={"role": "测试agent", "thread_id": "batch-test-fixture",
                                     "reviewed_at": "2026-11-26T00:00:00+08:00"}, decision="通过")
            acceptance = receipt["scope_acceptance"]
            acceptance.update(decision="通过", coverage_observation="隔离测试确认实际教学和已知未到手缺件")
            for item in acceptance["gaps"] + acceptance["teaching"] + acceptance.get("resolutions", []):
                item.update(decision="通过", review_observation="隔离测试已核实范围对应")
            review["independent_fact_review"] = receipt
        put(project.ledger / "publications" / f"{body_sha}.json", record)
        state = install(project, record, vault or self.vault, folder)
        if observed:
            self.record_reopen(project, record, state)
        return project, record, state

    def record_reopen(self, project, record, state):
        confirm(project, record, {
            **{key: state[key] for key in ("entry", "sha256", "installed_at")},
            "mode": "preview", "reopened": True, "reached_bottom": True,
            "references_collapsed": True, "visible_citations": False,
            "section_seen": "实操", "note": "重开同一版教材并浏览至文末"})

    def cached_publication(self, source, package):
        from prepare_documents import prepare_documents

        project, record, delivery = self.existing_publication(source, package)
        summary = prepare_documents(project.raw, project.validate_raw(), project.ledger, project.work)
        digests = record["review"]["snapshot"]["evidence"]
        digests["ledger/documents-summary.json"] = sha(project.ledger / "documents-summary.json")
        for item in summary["records"]:
            relative = item["result"]
            digests[f"work/{relative}"] = sha(project.work / relative)
        record["review"]["independent_fact_review"].update(fact_review_identity(record["body"], record["review"]))
        put(project.ledger / "publications" / f"{delivery['sha256']}.json", record)
        return project, record, delivery, summary

    def initialize(self, *args):
        return self.command("init", self.source, "--manifest", self.state, "--vault", self.vault, *args)

    def test_natural_order_and_exact_duplicate_decision(self):
        first = self.source_folder("第2项", "same source")
        self.source_folder("第10项", "same source")
        (self.source / "第99项 空项目").mkdir()
        (self.source / "未归属.txt").write_text("outside any course", encoding="utf-8")
        before = list(self.root.iterdir())
        plan = self.command("plan", self.source)
        self.assertEqual(list(self.root.iterdir()), before)
        self.assertEqual([row["name"] for row in plan["entries"][:2]], [first.name, "第10项"])
        self.assertEqual(plan["counts"], {batch.PENDING: 2, batch.DEFERRED: 1})
        self.assertEqual(plan["unassigned"], ["未归属.txt"])
        self.assertEqual(plan["possible_duplicate_groups_by_size"], [[1, 2]])
        self.initialize()
        package = self.root / "第2项教材"
        second = self.root / "第10项教材"
        self.assertIn("当前应处理第 1 项", self.command("claim", self.state, 2, second, success=False))
        self.assertIn("与第 [2] 项完全一致", self.command("claim", self.state, 1, package, success=False))
        row = self.command("claim", self.state, 1, package, "--separate", "文件相同，已核实两项需要独立入口")
        self.assertEqual(row["duplicates"], [2])
        self.assertEqual(row["status"], batch.ACTIVE)
        saved = self.state.read_bytes()
        self.assertEqual(self.command("claim", self.state, 1, package), row)
        self.assertEqual(self.state.read_bytes(), saved)
        self.assertIn("已绑定课程包", self.command("claim", self.state, 1, second, success=False))
        self.assertEqual(self.command("defer", self.state, 1, "等待原附件")["status"], batch.DEFERRED)
        self.assertEqual(self.command("status", self.state)["next"], 2)
        self.assertEqual(self.command("reopen", self.state, 1)["status"], batch.PENDING)
        self.assertEqual(self.command("status", self.state)["next"], 1)
        self.assertEqual(self.command("claim", self.state, 1, package)["package"], str(package))

    def test_wrong_package_and_changed_original_need_explicit_recheck(self):
        original = self.source_folder("甲项目", "first")
        wrong = self.source_folder("乙项目", "first")
        wrong_package = self.root / "乙的包"
        CourseProject.initialize(wrong, wrong_package)
        result = self.initialize()
        index = next(r["sequence"] for r in result["entries"] if r["name"] == original.name)
        if index != 1:
            self.command("defer", self.state, 1, "先核对另一项目")
        saved = self.state.read_bytes()
        error = self.command("claim", self.state, index, wrong_package, "--separate", "分别归档", success=False)
        self.assertIn("另一批次原件", error)
        self.assertEqual(self.state.read_bytes(), saved)
        bound = self.root / "正确课程包"
        self.command("claim", self.state, index, bound, "--separate", "分别归档")
        source_file = original / "资料/说明.md"
        info = source_file.stat()
        source_file.write_text("other", encoding="utf-8")
        os.utime(source_file, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertIn("原件指纹", self.command("claim", self.state, index, bound, success=False))
        self.command("defer", self.state, index, "收到修订版，待再次核对")
        self.command("reopen", self.state, index, "--accept-changes", "原件同大小修订已核对")
        row = self.command("claim", self.state, index, bound)
        self.assertEqual(row["duplicates"], [])
        self.assertIn("history", batch.read(self.state)["projects"][index - 1])

    def test_finish_requires_the_expected_named_vault_note_and_current_receipt(self):
        source = self.source_folder("原始课程", "课程说明")
        self.initialize()
        package = self.root / "独立课程包"
        self.command("claim", self.state, 1, package, "--course-folder", "保留旧目录名")
        project, publication, state = self.existing_publication(
            source, package, folder="保留旧目录名", observed=False)
        self.assertIn("同入口重开记录", self.command("finish", self.state, 1, package, success=False))
        self.record_reopen(project, publication, state)
        target = Path(state["entry"])
        original = target.read_bytes()
        target.write_bytes(original + b"\nedit")
        self.assertIn("不一致", self.command("finish", self.state, 1, package, success=False))
        target.write_bytes(original)
        row = self.command("finish", self.state, 1, package)
        self.assertEqual(row["status"], batch.COMPLETE)
        self.assertEqual(row["publication"]["entry"], str(target))
        self.assertEqual(row["publication"]["sha256"], sha(target))
        saved = self.state.read_bytes()
        self.assertEqual(self.command("finish", self.state, 1, package), row)
        self.assertEqual(self.state.read_bytes(), saved)

    def test_cannot_finish_into_wrong_vault_or_different_course_folder(self):
        source = self.source_folder("录课", "原片提示")
        self.initialize()
        package = self.root / "录课教材"
        self.command("claim", self.state, 1, package, "--course-folder", "原课教材")
        different = self.root / "临时验证库"
        (different / ".obsidian").mkdir(parents=True)
        self.existing_publication(source, package, vault=different, folder="原课教材")
        self.assertIn("绑定的日常阅读库", self.command("finish", self.state, 1, package, success=False))
        self.assertEqual(self.command("status", self.state)["counts"], {batch.ACTIVE: 1})

    def test_does_not_accept_a_legacy_publication_for_new_queue_work(self):
        source = self.source_folder("文档项目", "课程说明")
        self.initialize()
        package = self.root / "旧教材"
        self.command("claim", self.state, 1, package)
        self.existing_publication(source, package, legacy=True)
        self.assertIn("当前来源覆盖验收", self.command("finish", self.state, 1, package, success=False))
        self.assertEqual(self.command("status", self.state)["counts"], {batch.ACTIVE: 1})

    def test_new_schema_without_scope_still_cannot_be_new_complete_acceptance(self):
        source = self.source_folder("文档项目", "课程说明")
        self.initialize()
        package = self.root / "无明确范围的新旧稿"
        self.command("claim", self.state, 1, package)
        self.existing_publication(source, package, unclassified=True)
        self.assertIn("缺明确范围签收状态", self.command("finish", self.state, 1, package, success=False))
        self.assertEqual(self.command("status", self.state)["counts"], {batch.ACTIVE: 1})

    def test_core_and_support_scopes_advance_queue_and_keep_same_entry(self):
        source = self.source_folder("第1项 原有课", "首章课程说明")
        second = self.source_folder("第2项", "另一门原课程")
        self.initialize()
        package = self.root / "第一课程包"
        self.command("claim", self.state, 1, package, "--course-folder", "原有同名阅读入口")
        gap = {"item": "续讲.mp4", "kind": "核心", "source": "资料/说明.md · 首段",
               "impact": "原课程续讲需要补齐", "obtain": "取得课方同版本独立原片"}
        self.existing_publication(source, package, folder="原有同名阅读入口",
                                  scope_status="核心待补", scope_gaps=[gap])
        finished = self.command("finish", self.state, 1, package)
        self.assertEqual(finished["status"], batch.CORE_PENDING)
        self.assertEqual(finished["scope_gaps"], [gap])
        old_entry = finished["publication"]["entry"]
        self.assertEqual(self.command("status", self.state)["next"], 2)

        second_package = self.root / "第二课程包"
        self.command("claim", self.state, 2, second_package)
        support = {**gap, "item": "可选课后练习模板", "kind": "非核心"}
        self.existing_publication(second, second_package, scope_status="配套待补", scope_gaps=[support])
        self.assertEqual(self.command("finish", self.state, 2, second_package)["status"], batch.SUPPLEMENT_PENDING)
        self.assertIsNone(self.command("status", self.state)["next"])

        reopened = self.command("reopen", self.state, 1, "--accept-changes", "再次对照原课程范围")
        self.assertEqual(reopened["publication"]["entry"], old_entry)
        self.command("claim", self.state, 1, package)
        self.existing_publication(source, package, folder="原有同名阅读入口")
        self.assertIn("此前明确缺件", self.command("finish", self.state, 1, package, success=False))
        resolution = {"item": gap["item"], "source": "资料/说明.md · 首段",
                      "resolution": "经复核原缺件不成立", "note": "隔离测试核实该续讲是相同章节重复标识"}
        self.existing_publication(source, package, folder="原有同名阅读入口", scope_resolutions=[resolution])
        updated = self.command("finish", self.state, 1, package)
        self.assertEqual(updated["status"], batch.COMPLETE)
        self.assertEqual(updated["scope_resolutions"], [resolution])
        self.assertEqual(updated["publication"]["entry"], old_entry)

    def test_partial_scope_must_carry_previous_gap_or_verified_resolution(self):
        old = {"scope_gaps": [{"item": "旧缺件"}]}
        with self.assertRaisesRegex(ValueError, "上一版缺件"):
            batch.require_gap_disposition(old, {"scope_status": "核心待补",
                "scope_gaps": [{"item": "另一个缺件"}], "scope_resolutions": []})
        with self.assertRaisesRegex(ValueError, "旧项目已登记缺件"):
            batch.require_gap_disposition({"re_review": {"gaps_before_review": "旧审计反映缺件"}},
                {"scope_status": "完整验收", "scope_gaps": [], "scope_resolutions": []})

    def test_same_source_alias_reuses_one_partial_edition_and_changed_source_waits_for_review(self):
        source = self.source_folder("第1项 主课", "课程正文")
        alias = self.source_folder("第2项 同源来件", "课程正文")
        self.initialize()
        saved = batch.read(self.state)
        saved["projects"][1].update(status=batch.DEFERRED, same_source_primary=1,
                                    re_review={"state": "关联待复审"})
        put(self.state, saved)
        package = self.root / "主课程包"
        self.command("claim", self.state, 1, package)
        gap = {"item": "配套练习文件", "kind": "非核心", "source": "资料/说明.md · 首段",
               "impact": "练习辅助尚缺", "obtain": "取得课方授权的原件"}
        self.existing_publication(source, package, scope_status="配套待补", scope_gaps=[gap])
        primary = self.command("finish", self.state, 1, package)
        queue = batch.read(self.state)
        related = queue["projects"][1]
        self.assertEqual(related["status"], batch.REUSED)
        self.assertEqual(related["publication"], primary["publication"])
        self.assertEqual(related["scope_gaps"], [gap])
        self.assertEqual(len([p for p in self.vault.glob("*/课件/*.md")]), 1)
        (alias / "独立新增材料.txt").write_text("新教案", encoding="utf-8")
        meta = batch.metadata(alias)
        _, _, identity = batch.identify(queue, {**related, "metadata": meta, "source_sha256": None})
        evidence = self.root / "同源补料核证.json"
        proof = {"schema": 1, "kind": "alias_review", "source_id": related["id"],
                 "source_sha256": identity["source_sha256"], "canonical_id": primary["id"],
                 "canonical_source_sha256": primary["source_sha256"], "resolution": "pending",
                 "basis": "夹具核实新增的材料需要主课复核"}
        put(evidence, proof)
        pending = self.command("alias-review", self.state, 2, "--resolution", "pending",
                               "--evidence", evidence, "--reason", "主课待加入材料")
        self.assertTrue(pending["requires_alias_resolution"])
        self.command("finish", self.state, 1, package)
        self.assertEqual(batch.read(self.state)["projects"][1]["status"], batch.DEFERRED)
        proof.update(resolution="reuse", canonical_publication_sha256=primary["publication"]["sha256"])
        put(evidence, proof)
        self.assertIn("主稿仍为旧版", self.command("alias-review", self.state, 2, "--resolution", "reuse",
                                                "--evidence", evidence, "--reason", "复核同源",
                                                success=False))

    def test_external_canonical_package_reuses_only_the_verified_existing_entry(self):
        self.source_folder("同源来件", "课程正文")
        historical_source = self.root / "旧课程原件"
        (historical_source / "资料").mkdir(parents=True)
        (historical_source / "资料/说明.md").write_text("课程正文", encoding="utf-8")
        historical_package = self.root / "历史课程包"
        project, _, observed = self.existing_publication(historical_source, historical_package,
            folder="真实旧入口", legacy=True)
        self.initialize()
        queue = batch.read(self.state)
        row = queue["projects"][0]
        _, _, identity = batch.identify(queue, row)
        evidence = self.root / "历史同源核证.json"
        proof = {"schema": 1, "kind": "same_source", "source_id": row["id"],
                 "source_sha256": identity["source_sha256"],
                 "canonical_package": str(project.folder),
                 "canonical_source_sha256": batch.source_fingerprint(project.validate_raw()),
                 "canonical_publication_sha256": "0" * 64,
                 "basis": "隔离测试已核实两份同源原件正文和实际历史签收入口"}
        put(evidence, proof)
        unchanged = self.state.read_bytes()
        self.assertIn("历史主包同源证据", self.command("reuse", self.state, 1, "--canonical-package",
            historical_package, "--evidence", evidence, "--reason", "核实同源", success=False))
        self.assertEqual(self.state.read_bytes(), unchanged)
        proof["canonical_publication_sha256"] = observed["sha256"]
        put(evidence, proof)
        reused = self.command("reuse", self.state, 1, "--canonical-package", historical_package,
            "--evidence", evidence, "--reason", "核实同源")
        self.assertEqual(reused["status"], batch.REUSED)
        self.assertIsNone(reused["scope_status"])
        self.assertEqual(reused["reuse_target"]["package"], str(project.folder))
        self.assertEqual(reused["publication"]["entry"], observed["entry"])
        self.assertEqual(len([p for p in self.vault.glob("*/课件/*.md")]), 1)
        self.assertIsNone(self.command("status", self.state)["next"])

    def test_refresh_keeps_existing_ids_and_claims_pending_in_natural_order(self):
        initial = self.source_folder("第20项", "课程说明")
        old_entry = self.initialize()["entries"][0]
        self.source_folder("第10项", "另一份课")
        self.assertEqual(self.command("status", self.state)["new"], ["第10项"])
        update = self.command("refresh", self.state)
        self.assertEqual(update["new"], [])
        self.assertEqual(update["next"], 2)
        self.assertEqual(update["entries"][0], old_entry)
        self.assertEqual(update["entries"][1]["name"], "第10项")
        (initial / "后续资料.md").write_text("新增在旧项目内", encoding="utf-8")
        drift, = self.command("refresh", self.state)["metadata_drift"]
        self.assertEqual(drift["sequence"], 1)

    def test_missing_active_package_cannot_be_silently_replaced(self):
        source = self.source_folder("文档项目", "课程说明")
        self.initialize()
        package = self.root / "课程包"
        CourseProject.initialize(source, package)
        self.command("claim", self.state, 1, package)
        backup = self.root / "移动后的课程包"
        package.rename(backup)
        self.assertEqual(self.command("status", self.state)["package_drift"], [1])
        self.assertIn("不在原路径", self.command("claim", self.state, 1, package, success=False))
        self.assertIn("已绑定课程包", self.command("claim", self.state, 1, backup, success=False))

    def test_supplement_preserves_sequence_and_reuses_the_named_vault_entry(self):
        source = self.source_folder("第2项", "课程说明")
        self.source_folder("第3项", "另一门课")
        self.initialize()
        previous = CourseProject.initialize(source, self.root / "初版")
        before = self.command("claim", self.state, 1, previous.folder, "--course-folder", "已有课程名")
        combined = self.root / "补料原件"
        (combined / "资料").mkdir(parents=True)
        (combined / "资料/说明.md").write_text("课程说明", encoding="utf-8")
        (combined / "首讲.md").write_text("从同课完整版本核出的新增材料", encoding="utf-8")
        package = self.root / "新版"
        project = CourseProject.initialize(combined, package)

        reason = "同课另一版的共有材料已比对，补回首讲"
        result = self.command("supplement", self.state, 1, package, "--reason", reason)
        self.assertEqual(result["status"], batch.ACTIVE)
        self.assertEqual(result["course_folder"], before["course_folder"])
        self.assertEqual(result["source_sha256"], before["source_sha256"])
        self.assertEqual(result["supplemented_sha256"], batch.source_fingerprint(project.validate_raw()))
        self.assertIn("补回首讲", result["supplement_reason"])
        self.assertEqual(len(previous.validate_raw()), 1)
        self.assertEqual(batch.read(self.state)["projects"][0]["history"][-1]["package"], str(previous.folder))
        self.assertEqual(self.command("status", self.state)["next"], 1)
        once = self.state.read_bytes()
        self.assertEqual(self.command("supplement", self.state, 1, package, "--reason", reason), result)
        self.assertEqual(self.command("claim", self.state, 1, package), result)
        self.assertEqual(self.state.read_bytes(), once)
        self.existing_publication(combined, package, folder=before["course_folder"])
        finished = self.command("finish", self.state, 1, package)
        self.assertEqual(finished["status"], batch.COMPLETE)
        self.assertEqual(Path(finished["publication"]["entry"]).name, "已有课程名.md")
        self.assertEqual(self.command("status", self.state)["next"], 2)

    def test_supplement_cannot_replace_an_existing_original(self):
        source = self.source_folder("课程", "课程原件")
        self.initialize()
        previous = CourseProject.initialize(source, self.root / "初版")
        self.command("claim", self.state, 1, previous.folder)
        combined = self.root / "补料原件"
        (combined / "资料").mkdir(parents=True)
        (combined / "资料/说明.md").write_text("替换内容", encoding="utf-8")
        (combined / "首讲.md").write_text("新增原件", encoding="utf-8")
        project = CourseProject.initialize(combined, self.root / "新版")
        unchanged = self.state.read_bytes()
        error = self.command("supplement", self.state, 1, project.folder, "--reason", "待补第一讲", success=False)
        self.assertIn("逐件保留原快照", error)
        self.assertEqual(self.state.read_bytes(), unchanged)
        self.assertEqual(len(previous.validate_raw()), 1)

    def test_delta_compares_signed_history_by_bytes_even_after_reopen(self):
        source = self.source_folder("课程", "原件")
        removable = source / "旧附录.txt"
        removable.write_text("原始附录", encoding="utf-8")
        self.initialize()
        old, _, delivery = self.existing_publication(source, self.root / "已验收包")
        self.command("claim", self.state, 1, old.folder)
        self.command("finish", self.state, 1, old.folder)

        description = source / "资料/说明.md"
        stamp = description.stat()
        description.write_text("新版", encoding="utf-8")
        os.utime(description, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        removable.unlink()
        (source / "新附录.txt").write_text("新增材料", encoding="utf-8")
        self.command("reopen", self.state, 1, "--accept-changes", "记录实收修订")
        new = CourseProject.initialize(source, self.root / "新工作包")
        self.command("claim", self.state, 1, new.folder)
        old.raw.rename(old.folder / "已保全的原件")
        unchanged_manifest = self.state.read_bytes()

        result = self.command("delta", self.state, 1)
        self.assertTrue(result["read_only"])
        self.assertTrue(result["already_in_progress"])
        self.assertEqual(result["baseline"]["publication_sha256"], delivery["sha256"])
        self.assertEqual(result["disposition"], "registered_range_changed")
        self.assertEqual([item["file"] for item in result["changed"]], ["资料/说明.md"])
        self.assertEqual([item["file"] for item in result["removed"]], ["旧附录.txt"])
        self.assertEqual([item["file"] for item in result["added"]], ["新附录.txt"])
        self.assertEqual(self.state.read_bytes(), unchanged_manifest)

    def test_delta_preserves_prior_external_supplements_without_claiming_disk_presence(self):
        source = self.source_folder("课程", "保留资料")
        self.initialize()
        old = CourseProject.initialize(source, self.root / "初版")
        self.command("claim", self.state, 1, old.folder)
        combined = self.root / "原件整合"
        (combined / "资料").mkdir(parents=True)
        (combined / "资料/说明.md").write_bytes((source / "资料/说明.md").read_bytes())
        (combined / "补件.txt").write_text("另一已核原件", encoding="utf-8")
        new, _, _ = self.existing_publication(combined, self.root / "后续班次", folder=old.folder.name)
        self.command("supplement", self.state, 1, new.folder, "--reason", "核实同版原件")
        self.command("finish", self.state, 1, new.folder)

        result = self.command("delta", self.state, 1)
        self.assertEqual(result["disposition"], "unchanged")
        self.assertEqual([item["file"] for item in result["unchanged"]], ["资料/说明.md"])
        self.assertEqual([item["file"] for item in result["carried_supplements"]], ["补件.txt"])
        self.assertFalse(result["removed"])

    def test_delta_stage_reuses_only_pinned_static_parse_and_never_inherits_signoff(self):
        import prepare_documents

        source = self.source_folder("课程", "原件步骤")
        self.initialize()
        old, record, delivery, summary = self.cached_publication(source, self.root / "已验收")
        relative = summary["records"][0]["result"]
        digests = record["review"]["snapshot"]["evidence"]
        self.command("claim", self.state, 1, old.folder)
        self.command("finish", self.state, 1, old.folder)
        before = self.state.read_bytes()

        other = self.root / "另一个来源"
        (other / "资料").mkdir(parents=True)
        (other / "资料/说明.md").write_bytes((source / "资料/说明.md").read_bytes())
        (other / "新练习.txt").write_text("补充练习", encoding="utf-8")
        target = self.root / "新的取材工作包"
        args = ("delta", self.state, 1, "--candidate", other, "--stage-package", target)
        self.assertIn("核实确属同一课程版次", self.command(*args, success=False))
        self.assertFalse(target.exists())
        args += ("--same-edition-reason", "线下核实为同版原件及新增练习")
        result = self.command(*args)
        self.assertEqual(result["reused_static_evidence"], 1)
        new = CourseProject.open(target)
        self.assertFalse(new.entry.exists())
        self.assertFalse((new.ledger / "publications").exists())
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.command(*args), result)

        with patch.object(prepare_documents, "inspect_document", wraps=prepare_documents.inspect_document) as parse:
            updated = prepare_documents.prepare_documents(new.raw, new.validate_raw(), new.ledger, new.work)
        self.assertEqual([call.args[0].name for call in parse.call_args_list], ["新练习.txt"])
        self.assertEqual(len(updated["records"]), 2)

        (old.work / relative).write_text("篡改的取材", encoding="utf-8")
        second = self.command("delta", self.state, 1, "--candidate", other)
        self.assertFalse(second["reusable_evidence"])
        digests[f"work/{relative}"] = sha(old.work / relative)
        put(old.ledger / "publications" / f"{delivery['sha256']}.json", record)
        repinned = self.command("delta", self.state, 1, "--candidate", other)
        self.assertFalse(repinned["reusable_evidence"])

    def test_delta_new_edition_requires_separate_identity_and_uses_only_unchanged_evidence(self):
        source = self.source_folder("课程原版", "原件步骤")
        (source / "训练.txt").write_text("第一版的训练", encoding="utf-8")
        (source / "旧附录.txt").write_text("旧资料", encoding="utf-8")
        self.initialize()
        old, _, delivery, _ = self.cached_publication(source, self.root / "原版教材工作包")
        self.command("claim", self.state, 1, old.folder)
        self.command("finish", self.state, 1, old.folder)
        before = self.state.read_bytes()

        candidate = self.source_folder("课程第二版", "原件步骤")
        (candidate / "训练.txt").write_text("第二版的训练", encoding="utf-8")
        (candidate / "新附录.txt").write_text("新资料", encoding="utf-8")
        target = self.root / "新版教材工作包"
        stage = ("delta", self.state, 1, "--candidate", candidate, "--stage-package", target)
        self.assertIn("完整保留旧原件", self.command(*stage, success=False))
        self.assertFalse(target.exists())
        result = self.command(*stage, "--new-edition-reason", "作者新版清单与此候选的新增和修订已对应")
        self.assertEqual(result["stage_kind"], "new_edition")
        self.assertNotIn("project_id", result)
        self.assertEqual(result["reused_static_evidence"], 1)
        self.assertEqual(self.state.read_bytes(), before)

        new = CourseProject.open(target)
        plan = batch.read(new.work / "增量取材.json")
        self.assertEqual([item["file"] for item in plan["changed"]], ["训练.txt"])
        self.assertEqual([item["file"] for item in plan["removed"]], ["旧附录.txt"])
        self.assertEqual([item["file"] for item in plan["added"]], ["新附录.txt"])
        self.assertEqual([item["file"] for item in plan["reused_static_evidence"]], ["资料/说明.md"])
        old_row = batch.read(self.state)["projects"][0]
        self.assertEqual(plan["comparison_project_id"], old_row["id"])
        with self.assertRaisesRegex(ValueError, "另一个正式项目身份"):
            batch.require_staged_edition(new, old_row)
        with self.assertRaisesRegex(ValueError, "不能作为同版补件"):
            batch.require_staged_edition(new)
        self.assertFalse(new.entry.exists())
        self.assertFalse((new.ledger / "publications").exists())

        self.command("refresh", self.state)
        claimed = self.command("claim", self.state, 2, new.folder)
        self.assertNotEqual(claimed["id"], old_row["id"])
        self.assertNotIn("publication", claimed)
        self.assertEqual(batch.read(self.state)["projects"][0], old_row)
        self.assertEqual(sha(Path(delivery["entry"])), delivery["sha256"])

    def test_simultaneous_claims_cannot_bind_different_course_packages(self):
        self.source_folder("文档项目", "课程说明")
        self.initialize()
        cli = Path(batch.__file__).with_name("course.py")

        def invoke(package):
            return subprocess.run(
                [sys.executable, "-X", "utf8", "-B", str(cli), "batch", "claim",
                 str(self.state), "1", str(package)],
                capture_output=True, text=True, encoding="utf-8", timeout=20)

        with ThreadPoolExecutor(max_workers=2) as executor:
            replies = list(executor.map(invoke, [self.root / "包甲", self.root / "包乙"]))
        self.assertEqual(sorted(reply.returncode for reply in replies), [0, 2])
        succeeded = next(reply for reply in replies if not reply.returncode)
        self.assertEqual(self.command("status", self.state)["entries"][0]["package"],
                         json.loads(succeeded.stdout)["package"])

    def test_legacy_migration_preserves_current_and_requires_explicit_supplement_proof(self):
        original = self.source_folder("001 原项目", "课程说明")
        self.source_folder("002 缺少正文", "仅可见目录")
        working = self.source_folder("003 正在编辑", "新项目草案")
        root = self.root / "旧批次"
        root.mkdir()
        earlier = root / "核验原件与作者参考"
        earlier_source = earlier / "资料/说明.md"
        earlier_source.parent.mkdir(parents=True)
        earlier_source.write_bytes((original / "资料/说明.md").read_bytes())
        (earlier / "作者官方材料.md").write_text("同版补充", encoding="utf-8")
        package = self.root / "旧课程包"
        project, _, published = self.existing_publication(earlier, package, legacy=True)
        current_package = self.root / "继续工作"
        CourseProject.initialize(working, current_package)
        data = batch.scan_batch(self.source, self.vault)
        old = {"入库来源": str(self.source), "日常库": str(self.vault), "项目": [
            {"序号": row["sequence"], "目录名": row["name"], "原件目录": str(self.source / row["name"]),
             "文件": [{"路径": item["file"], "大小": item["bytes"], "最后修改_ns": item["mtime_ns"]}
                     for item in row["metadata"]]} for row in data["projects"]]}
        put(root / batch.LEGACY_QUEUE, old)
        report = root / "交付结果.json"
        put(report, {"verified": True, "package_verified": True, "entry": str(project.entry),
                     "vault_delivery": published})
        put(root / "001-已验收项目.json", {
            "次序": 1, "入口": published["entry"], "版本": published["sha256"], "交付回执": str(report)})
        put(root / "002-原始教程待提供.json", {"序号": 2, "状态": "缺少原正文"})
        put(root / batch.LEGACY_CURRENT, {
            "次序": 3, "原件": str(working), "目标项目": current_package.name,
            "课程包": str(current_package), "进度": "已转写待校稿"})
        saved = {path.name: sha(path) for path in root.glob("*.json")}
        plan = self.command("plan", self.source, "--vault", self.vault, "--legacy-directory", root)
        self.assertEqual(plan["historical_supplements_pending_confirmation"], [1])
        self.assertEqual(plan["next"], 3)
        self.assertFalse(self.state.exists())
        self.assertIn("补料快照", self.command(
            "init", self.source, "--manifest", self.state, "--vault", self.vault,
            "--legacy-directory", root, success=False))
        self.assertFalse(self.state.exists())
        loaded = self.initialize("--legacy-directory", root, "--supplemented", 1)
        self.assertEqual(loaded["counts"], {batch.COMPLETE: 1, batch.DEFERRED: 1, batch.ACTIVE: 1})
        self.assertEqual(loaded["hash_verified"], 2)
        self.assertEqual(loaded["entries"][2]["resume_note"], "已转写待校稿")
        self.assertEqual(loaded["entries"][0]["publication"]["entry"], published["entry"])
        self.assertEqual(self.command("finish", self.state, 1, package)["status"], batch.COMPLETE)
        self.assertEqual({path.name: sha(path) for path in root.glob("*.json")}, saved)

        self.state = self.root / "延后历史复验.json"
        deferred = self.initialize("--legacy-directory", root, "--supplemented", 1, "--defer-history")
        self.assertEqual(deferred["counts"], {batch.HISTORICAL: 1, batch.DEFERRED: 1, batch.ACTIVE: 1})
        self.assertEqual(deferred["hash_verified"], 1)
        self.assertEqual(deferred["next"], 3)
        self.assertEqual(self.command("finish", self.state, 1, package)["status"], batch.COMPLETE)


if __name__ == "__main__":
    unittest.main()
