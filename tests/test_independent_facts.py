"""A second review binds the actual fact/source relationship to the candidate version."""

from copy import deepcopy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1] / "_tools/course_production"
with patch.object(sys, "path", [str(TOOLS), *sys.path]):
    from independent_facts import load_independent_receipt, receipt_path, review_request, validate_independent_receipt
    from project import ENTRY, put, read, sha
    from course import prepare_fact_request
    from scope_review import add_scope_summary
    from source_coverage import CLAIMS_VERSION, claim_review_rows, coverage_review_template


ORIGINAL = "第 13 讲 00:26:27.30–00:27:51.00"
ADOBE = "https://helpx.adobe.com/premiere/desktop/get-started/keyboard-shortcuts/default-keyboard-shortcuts.html"
BODY = ("叠画适合交代时间或时空变化。课堂顺口举过 Ctrl+B。按 Adobe 默认键位，应用默认视频过渡"
        "在 Windows 为 Ctrl+D、在 Mac 为 Cmd+D。 "
        f"<!-- 段落出处 {ORIGINAL} --> <!-- 外部出处 {ADOBE} -->")


def candidate(body=BODY, changed="Ctrl+D"):
    claims = claim_review_rows(body)
    claims[0].update(outcome="混合来源", note="逐项记录原课口述和外部设置",
                     fact_checks=[
                         {"anchor": "课堂顺口举过 Ctrl+B", "origin": "原件", "source": ORIGINAL,
                          "note": "课堂口述的键位"},
                         {"anchor": f"在 Windows 为 {changed}、在 Mac 为 Cmd+D", "origin": "外部", "source": ADOBE,
                          "note": "对照外部默认键位表后的作者结论"},
                     ])
    return {"source_scope": {"fixture": "336 第13讲同一原始范围"},
            "source_coverage": {"claims_version": CLAIMS_VERSION, "claims": claims}}


def approved_test_fixture(body, review):
    """A deterministic mocked reviewer: observed values are explicit fixture ground truth."""
    receipt = review_request(body, review)
    receipt.update(reviewer={"role": "测试agent", "thread_id": "synthetic-independent-reviewer",
                             "reviewed_at": "2026-11-25T00:10:30+08:00"}, decision="通过")
    for row, (observation, evidence) in zip(receipt["facts"], [
        ("课堂原声举例 Ctrl+B", "独立测试夹具中的第13讲口述定位"),
        ("外部默认 Ctrl+D 与 Cmd+D", "独立测试夹具中的 Adobe 键位表"),
    ], strict=True):
        row.update(decision="通过", observation=observation, evidence=evidence)
    return receipt


class IndependentFactTest(unittest.TestCase):
    def test_mixed_source_swap_cannot_reuse_independent_approval(self):
        review = candidate()
        approved = approved_test_fixture(BODY, review)
        validate_independent_receipt(BODY, review, approved)
        swapped = deepcopy(review)
        first, second = swapped["source_coverage"]["claims"][0]["fact_checks"]
        first["origin"], second["origin"] = second["origin"], first["origin"]
        first["source"], second["source"] = second["source"], first["source"]
        with self.assertRaisesRegex(ValueError, "事实归属不一致"):
            validate_independent_receipt(BODY, swapped, approved)

    def test_new_body_and_refilled_false_author_review_need_new_independent_decision(self):
        approved = approved_test_fixture(BODY, candidate())
        wrong = BODY.replace("Ctrl+D", "Ctrl+X")
        with self.assertRaisesRegex(ValueError, "当前正文"):
            validate_independent_receipt(wrong, candidate(wrong, "Ctrl+X"), approved)
        rewritten = BODY.replace("按 Adobe 默认键位", "另行查对 Adobe 默认键位")
        revised = candidate(rewritten)
        new_receipt = approved_test_fixture(rewritten, revised)
        validate_independent_receipt(rewritten, revised, new_receipt)

    def test_request_is_not_approval_and_missing_observation_is_rejected(self):
        review = candidate()
        request = review_request(BODY, review)
        with self.assertRaisesRegex(ValueError, "明确签收"):
            validate_independent_receipt(BODY, review, request)
        approved = approved_test_fixture(BODY, review)
        approved["facts"][1]["observation"] = "已查"
        with self.assertRaisesRegex(ValueError, "实际证据"):
            validate_independent_receipt(BODY, review, approved)

    def test_separate_receipt_location_used_and_changes_create_new_identity(self):
        review = candidate()
        with TemporaryDirectory() as scratch, patch.dict("os.environ", {"WORLDANALYSIS_COURSE_RUNTIME": scratch}):
            path = receipt_path(BODY, review)
            request = review_request(BODY, review)
            self.assertEqual(Path(request["receipt_path"]), path)
            self.assertFalse(path.exists())
            with self.assertRaisesRegex(ValueError, "待独立测试"):
                load_independent_receipt(BODY, review)
            approval = approved_test_fixture(BODY, review)
            put(path, approval)
            self.assertEqual(load_independent_receipt(BODY, review), approval)
            self.assertEqual(read(path), approval)
            modified = deepcopy(review)
            modified["source_coverage"]["claims"][0]["fact_checks"][0]["note"] = "变动的作者核对记录"
            self.assertNotEqual(receipt_path(BODY, modified), path)
            with self.assertRaisesRegex(ValueError, "待独立测试"):
                load_independent_receipt(BODY, modified)

    def test_empty_document_has_no_fact_gate_unless_an_explicit_receipt_is_supplied(self):
        body = "从原件读过这一段。"
        review = {"source_scope": {"fixture": "simple document"},
                  "source_coverage": {"claims_version": CLAIMS_VERSION, "claims": claim_review_rows(body)}}
        validate_independent_receipt(body, review, None)
        empty = review_request(body, review)
        empty.update(reviewer={"role": "测试agent", "thread_id": "synthetic-reviewer",
                               "reviewed_at": "2026-11-25T00:10:30+08:00"}, decision="通过")
        validate_independent_receipt(body, review, empty)

    def test_optional_evidence_is_rehashed_at_release(self):
        review = candidate()
        with TemporaryDirectory() as scratch, patch.dict("os.environ", {"WORLDANALYSIS_COURSE_RUNTIME": str(Path(scratch) / "runtime")}):
            evidence = Path(scratch) / "independent-evidence.txt"
            evidence.write_text("模拟外部原页摘录：Ctrl+D / Cmd+D", encoding="utf-8")
            approval = approved_test_fixture(BODY, review)
            approval["facts"][1]["evidence_files"] = [{"path": evidence.name, "sha256": sha(evidence)}]
            put(receipt_path(BODY, review), approval)
            self.assertEqual(load_independent_receipt(BODY, review), approval)
            evidence.write_text("发生变更的证据", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "附带证据文件缺失或变动"):
                load_independent_receipt(BODY, review)

    def test_request_reuses_the_current_preview_without_creating_approval(self):
        original = "讲义.txt · 整份原件"
        declaration = {"scope_status": "完整验收", "scope_gaps": [], "scope_teaching": [
            {"source": original, "anchor": "先保存实际预览", "observation": "隔离教材原件的教学动作"}]}
        statement = add_scope_summary(f"# 原件课程\n\n先保存实际预览。 <!-- 段落出处 {original} -->\n\n"
                                      f"另行查对 Adobe 默认键位 Ctrl+D。 <!-- 外部出处 {ADOBE} -->", declaration)
        scope = {"videos": [], "documents": [{"id": "D001", "source": "讲义.txt", "candidates": [],
                  "units": [{"key": "file", "kind": "file", "value": None}]}], "unreadable": []}
        with (TemporaryDirectory() as scratch,
              patch.dict("os.environ", {"WORLDANALYSIS_COURSE_RUNTIME": str(Path(scratch) / "runtime")}),
              patch.object(sys, "path", [str(TOOLS), *sys.path])):
            work, preview = Path(scratch) / "work", Path(scratch) / "preview"
            project = SimpleNamespace(work=work, preview=preview)
            note = preview / ENTRY
            note.parent.mkdir(parents=True)
            note.write_bytes(statement.encode("utf-8"))
            snapshot = {"text_sha256": sha(note)}
            review = {"source_scope": scope, "snapshot": snapshot, "episodes": [],
                      "source_coverage": coverage_review_template(scope, statement), "scope_review_schema": 1,
                      **declaration, "scope_resolutions": [], "scope_prior": None, "scope_document_candidates": ["D001"]}
            review["source_coverage"]["documents"][0]["units"][0].update(outcome="正文已覆盖",
                note="隔离原件提供保存预览动作", anchor="先保存实际预览")
            review["source_coverage"]["claims"][0].update(outcome="原件已核", note="核对原件教的预览动作")
            claim = review["source_coverage"]["claims"][1]
            claim.update(outcome="外部核验", note="作者参照外部设置", fact_checks=[{
                "anchor": "Adobe 默认键位 Ctrl+D", "origin": "外部", "source": ADOBE,
                "note": "作者核对外部默认键位表"}])
            put(work / "验收填写模板.json", review)
            put(work / "验收记录.json", review)
            request_path, request = prepare_fact_request(project)
            self.assertEqual(read(request_path), request)
            self.assertFalse(Path(request["receipt_path"]).exists())
            note.write_bytes(statement.replace("Ctrl+D", "Ctrl+X").encode("utf-8"))
            with self.assertRaisesRegex(ValueError, "同版预览"):
                prepare_fact_request(project)
