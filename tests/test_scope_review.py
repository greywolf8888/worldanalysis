"""Received teaching must pass the original gate even when other material is absent."""

from copy import deepcopy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "_tools/course_production"
with patch.object(sys, "path", [str(TOOLS), *sys.path]):
    from acceptance import previous_scope_context
    from independent_facts import review_request, validate_independent_receipt
    from project import ENTRY, ReceivedCoverageError, put, read, sha, write
    from scope_review import (add_scope_summary, document_teaching_candidates, legacy_scope_summary,
                              publication_scope, scope_summary, validate_scope_review)
    from source_coverage import claim_review_rows, coverage_review_template, validate_source_coverage


VIDEO = "第 1 讲 00:00–01:00"
DOCUMENT = "讲义.pdf · 第 1 页"
BODY = ("# 画板入门\n\n## 第 1 讲\n\n打开画板菜单并核对选项。 "
        f"<!-- 段落出处 {VIDEO} -->\n\n## 配套讲义\n\n保存完成的预览。 <!-- 段落出处 {DOCUMENT} -->\n")
PROOF = {"source": VIDEO, "anchor": "打开画板菜单", "observation": "收到的完整录屏显示菜单打开和可选项"}
GAP = {"item": "下一课练习原件.mp4", "kind": "核心", "source": VIDEO,
       "impact": "下一讲练习过程无法从现有第一讲核对", "obtain": "收齐原课程第二讲视频后复核"}


def mixed_fixture(status="完整验收", gaps=(), *, resolutions=(), prior=None):
    declaration = {"scope_status": status, "scope_gaps": list(gaps), "scope_teaching": [PROOF.copy()],
                   "scope_resolutions": list(resolutions)}
    body = add_scope_summary(BODY, declaration)
    scope = {"videos": [{"lesson": 1, "display_number": 1, "source": "01.mp4", "duration_s": 60,
                        "kind": "video", "windows": [[0, 60]], "zones": [], "candidates": []}],
             "documents": [{"id": "D001", "source": "讲义.pdf", "units": [{"key": "page:1", "kind": "page", "value": 1}],
                            "candidates": []}], "unreadable": []}
    coverage = coverage_review_template(scope, body)
    coverage["videos"][0]["windows"][0].update(outcome="正文已覆盖", note="录屏出现并演示画板菜单")
    coverage["documents"][0]["units"][0].update(outcome="正文已覆盖", note="讲义原页给出保存预览说明", anchor="保存完成的预览")
    for claim in coverage["claims"]:
        claim.update(outcome="原件已核", note="已对照本隔离夹具的相邻实际来源")
    review = {"schema": 2, "scope_review_schema": 1, **declaration, "scope_prior": prior, "scope_document_candidates": ["D001"],
              "source_scope": scope, "source_coverage": coverage, "episodes": [{"lesson": 1,
                  "watched": [{"kind": "教学", "range_s": [0, 60], "note": "逐项核录屏操作"}],
                  "source_points": [{"kind": "操作", "range_s": [0, 60], "anchor": "打开画板菜单"}]}]}
    return body, review


def approved(body, review):
    """Explicitly synthetic isolated tester receipt, never a production signature."""
    receipt = review_request(body, review)
    receipt.update(reviewer={"role": "测试agent", "thread_id": "scope-test-fixture", "reviewed_at": "2026-11-26T00:00:00+08:00"},
                   decision="通过")
    scope = receipt["scope_acceptance"]
    scope.update(decision="通过", coverage_observation="本隔离测试已逐集逐件覆盖实际录屏及讲义")
    for row in scope["gaps"] + scope["teaching"] + scope["resolutions"]:
        row.update(decision="通过", review_observation="本隔离夹具确认原件内容、缺件性质及其位置")
    return receipt


class ScopeReviewTest(unittest.TestCase):
    def test_full_core_and_support_scope_keep_received_material_review(self):
        for status, gaps in (("完整验收", []), ("配套待补", [{**GAP, "kind": "非核心", "item": "外部练习空白模板"}]),
                             ("核心待补", [GAP])):
            with self.subTest(status=status):
                body, review = mixed_fixture(status, gaps)
                validate_source_coverage(body, review["source_scope"], review)
                validate_scope_review(body, review)
                receipt = approved(body, review)
                validate_independent_receipt(body, review, receipt)
                stored = {"body": body, "scope_status": status, "scope_gaps": list(gaps), "scope_resolutions": [],
                          "review": {**review, "independent_fact_review": receipt}}
                self.assertEqual(publication_scope(stored), {"scope_status": status, "scope_gaps": list(gaps), "scope_resolutions": []})

    def test_received_attachment_cannot_be_declared_external_or_omitted(self):
        body, review = mixed_fixture("核心待补", [GAP])
        review["source_coverage"]["documents"][0]["units"].clear()
        with self.assertRaisesRegex(ValueError, "附件.*页、表或档内成员"):
            validate_source_coverage(body, review["source_scope"], review)
        body, review = mixed_fixture("配套待补", [{**GAP, "item": "讲义.pdf", "kind": "非核心"}])
        with self.assertRaisesRegex(ReceivedCoverageError, "已收到的原件不能重报") as problem:
            validate_scope_review(body, review)
        self.assertEqual(problem.exception.state, "received_reported_missing")

    def test_learning_supplement_is_visible_independently_verified_and_does_not_cover_originals(self):
        original, review = mixed_fixture("核心待补", [GAP])
        link = "https://example.org/official"
        paragraph = f"**延伸学习。** 可以按[官方练习说明]({link})试做。"
        marker = f"<!-- 学习补充 {GAP['item']} -->"
        body = f"{original}\n{paragraph}\n{marker}\n<!-- 外部出处 {link} -->\n"
        claim = claim_review_rows(body)[-1]
        claim.update(outcome="外部核验", note="练习是另找的示例，不是缺少的课程原片",
                     fact_checks=[{"anchor": paragraph, "origin": "外部", "source": link,
                                   "note": "已在独立来源对照公开练习步骤"}])
        review["source_coverage"]["claims"].append(claim)
        validate_source_coverage(body, review["source_scope"], review)
        validate_scope_review(body, review)
        signed = approved(body, review)
        for fact in signed["facts"]:
            fact.update(decision="通过", observation="补充练习与原片归属分开", evidence="公开说明同段")
        validate_independent_receipt(body, review, signed)
        self.assertEqual(signed["scope_acceptance"]["gaps"][0]["item"], GAP["item"])

        with self.assertRaisesRegex(ValueError, "实际仍缺"):
            validate_source_coverage(body.replace(marker, "<!-- 学习补充 另一个项目 -->"), review["source_scope"], review)
        with self.assertRaisesRegex(ValueError, "外部来源链接"):
            validate_source_coverage(body.replace(f"]({link})", "](https://example.org/other)"), review["source_scope"], review)
        with self.assertRaisesRegex(ValueError, "只引用外部资料"):
            validate_source_coverage(body.replace(marker, f"{marker}\n<!-- 段落出处 {VIDEO} -->"),
                                     review["source_scope"], review)
        with self.assertRaises(ReceivedCoverageError) as problem:
            validate_source_coverage(body.replace("打开画板菜单并核对选项。", "已收到的操作被删掉。"),
                                     review["source_scope"], review)
        self.assertEqual(problem.exception.state, "received_coverage_missing")
        with self.assertRaisesRegex(ValueError, "当前正文"):
            validate_independent_receipt(body.replace("试做。", "练习。"), review, signed)

    def test_reader_gap_notice_is_natural_and_legacy_receipt_still_matches(self):
        body, review = mixed_fixture("核心待补", [GAP])
        self.assertIn(GAP["impact"], body)
        self.assertNotIn("原件证据", body)
        self.assertNotIn("验收范围：", body)
        legacy = body.replace(scope_summary(review), legacy_scope_summary(review))
        receipt = approved(legacy, review)
        stored = {"body": legacy, "scope_status": "核心待补", "scope_gaps": [GAP], "scope_resolutions": [],
                  "review": {**review, "independent_fact_review": receipt}}
        self.assertEqual(publication_scope(stored)["scope_gaps"], [GAP])

    def test_incomplete_scope_cannot_claim_complete_and_stale_scope_signature_fails(self):
        with self.assertRaisesRegex(ValueError, "实际缺件归属冲突"):
            mixed_fixture("完整验收", [GAP])
        body, review = mixed_fixture("核心待补", [GAP])
        receipt = approved(body, review)
        altered_body, altered_review = mixed_fixture("配套待补", [{**GAP, "kind": "非核心"}])
        with self.assertRaisesRegex(ValueError, "不一致"):
            validate_independent_receipt(altered_body, altered_review, receipt)
        with self.assertRaisesRegex(ValueError, "独立事实核验回执"):
            validate_independent_receipt(body, review, None)
        modified = deepcopy(review)
        modified["source_coverage"]["documents"][0]["units"][0]["note"] = "写作者随后改变附件覆核决定"
        with self.assertRaisesRegex(ValueError, "不一致"):
            validate_independent_receipt(body, modified, receipt)
        with self.assertRaisesRegex(ValueError, "正文缺少同版"):
            validate_scope_review(body.replace(GAP["impact"], "可以完成全部练习。"), review)
        self.assertEqual(publication_scope({"body": BODY, "review": {"schema": 2}}),
                         {"scope_status": None, "scope_gaps": [], "scope_resolutions": []})

    def test_old_signed_gap_requires_independently_reviewed_resolution(self):
        prior = {"publication_sha256": "a" * 64, "gaps": [GAP]}
        body, review = mixed_fixture(prior=prior)
        with self.assertRaisesRegex(ValueError, "此前明确缺件不得静默删除"):
            validate_scope_review(body, review)
        body, review = mixed_fixture("配套待补", [{**GAP, "kind": "非核心"}], prior=prior)
        with self.assertRaisesRegex(ValueError, "此前明确缺件不得静默删除"):
            validate_scope_review(body, review)
        resolution = {"item": GAP["item"], "source": DOCUMENT, "resolution": "经复核原缺件不成立",
                      "note": "对照隔离课程目录说明此前课名是讲义同一个练习，而非第二讲"}
        body, review = mixed_fixture(prior=prior, resolutions=[resolution])
        validate_scope_review(body, review)
        receipt = approved(body, review)
        validate_independent_receipt(body, review, receipt)
        changed = deepcopy(receipt)
        changed["scope_acceptance"]["resolutions"].clear()
        with self.assertRaisesRegex(ValueError, "逐项确认"):
            validate_independent_receipt(body, review, changed)
        altered = deepcopy(review)
        altered["scope_resolutions"][0]["note"] += "。"
        with self.assertRaisesRegex(ValueError, "不一致"):
            validate_independent_receipt(body, altered, receipt)

    def test_previous_scope_context_is_retained_on_same_edition_retry(self):
        with TemporaryDirectory() as scratch:
            project = SimpleNamespace(entry=Path(scratch) / ENTRY, ledger=Path(scratch) / "ledger")
            def store(body, review):
                write(project.entry, body)
                digest = sha(project.entry)
                record = {"body": body, "outputs": {ENTRY.as_posix(): digest},
                          "scope_status": review["scope_status"], "scope_gaps": review["scope_gaps"],
                          "scope_resolutions": review["scope_resolutions"],
                          "review": {**review, "independent_fact_review": approved(body, review)}}
                put(project.ledger / f"publications/{digest}.json", record)
                return digest
            previous_body, previous_review = mixed_fixture("核心待补", [GAP])
            prior_digest = store(previous_body, previous_review)
            self.assertIsNone(previous_scope_context(project, previous_body, read))
            resolution = {"item": GAP["item"], "source": DOCUMENT, "resolution": "经复核原缺件不成立",
                          "note": "隔离课程目录证明该附件是已收练习的另一名称"}
            expected_prior = {"publication_sha256": prior_digest, "gaps": [GAP]}
            updated_body, updated_review = mixed_fixture(prior=expected_prior, resolutions=[resolution])
            self.assertEqual(previous_scope_context(project, updated_body, read), expected_prior)
            store(updated_body, updated_review)
            self.assertEqual(previous_scope_context(project, updated_body, read), expected_prior)

    def test_pointer_only_extraction_cannot_satisfy_teaching_proof(self):
        with TemporaryDirectory() as scratch:
            project = SimpleNamespace(ledger=Path(scratch) / "ledger", work=Path(scratch) / "work")
            put(project.ledger / "documents-summary.json", {"records": [
                {"id": "D001", "file": "指针.txt", "result": "pointer.json"},
                {"id": "D002", "file": "教学链接.url", "result": "url.json"},
                {"id": "D003", "file": "真实教程.txt", "result": "content.json"},
                {"id": "D004", "file": "理论与参考.txt", "result": "theory.json"},
                {"id": "D005", "file": "下载指引.txt", "result": "promotional.json"}]})
            put(project.work / "pointer.json", {"blocks": [{"text": "完整课程\n链接 https://example.invalid/course\n提取码：abcd"}]})
            put(project.work / "url.json", {"blocks": [{"text": "https://example.invalid/course"}]})
            put(project.work / "content.json", {"blocks": [{"text": "设置画板尺寸，保存预览并检查输出。"}]})
            put(project.work / "theory.json", {"blocks": [{"text": "色相与明度共同决定感知中的视觉层次。\n参考 https://example.invalid/theory"}]})
            put(project.work / "promotional.json", {"blocks": [{"text": "完整课程\n请访问下方链接获取全部资料 https://example.invalid/course\n提取码：abcd"}]})
            self.assertEqual(document_teaching_candidates(project), ["D003", "D004"])
        body, review = mixed_fixture()
        review["scope_teaching"] = [{"source": DOCUMENT, "anchor": "保存完成的预览", "observation": "已核对实际教学原件"}]
        review["scope_document_candidates"] = []
        with self.assertRaisesRegex(ValueError, "单独指针不得出版"):
            validate_scope_review(body, review)
