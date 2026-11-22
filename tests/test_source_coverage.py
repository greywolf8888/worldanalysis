"""Reject source omissions and unsupported references without rendering a vault."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1] / "_tools/course_production"
with patch.object(sys, "path", [str(TOOLS), *sys.path]):
    from acceptance import validate_supplements
    from project import put
    from source_coverage import (build_source_manifest, coverage_review_template, document_candidates,
                                 document_locator_valid, document_units, validate_source_coverage, video_candidates)


START = "打开画板并确认比例。 <!-- 段落出处 第 1 讲 00:00–02:00 -->"
FINISH = "完成导出并检查预览。 <!-- 段落出处 第 1 讲 02:00–04:00 -->"
AUTHOR = "原作者联系方式为 teacher_2026。 <!-- 段落出处 第 1 讲 00:10–00:15 -->"
FIRST_PAGE = "画板说明中的参考比例。 <!-- 段落出处 配套讲义.pdf · 第 1 页 -->"
SECOND_PAGE = "课后练习说明。 <!-- 段落出处 配套讲义.pdf · 第 2 页 -->"
MEMBER = "还可以按压缩包内的规则复查导出。 <!-- 段落出处 配套练习.zip · 笔记/复查.md -->"


class SourceCoverageTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = Path(temp.name)
        ledger, work = state / "ledger", state / "work"
        project = SimpleNamespace(state=state, ledger=ledger, work=work, intake=ledger / "intake.json")
        self.project = project
        originals = [{"file": "原片.mp4", "sha256": "video", "extension": ".mp4"},
                     {"file": "配套讲义.pdf", "sha256": "pdf", "extension": ".pdf"},
                     {"file": "配套练习.zip", "sha256": "zip", "extension": ".zip"}]
        put(project.intake, {"sources": originals})
        put(ledger / "inventory.json", {"sources": originals})
        put(ledger / "media-summary.json", {"videos": [
            {"lesson": 1, "source": "原片.mp4", "sha256": "video", "duration_s": 240}
        ]})
        put(work / "讲次顺序.json", [1])
        put(ledger / "01.json", {"cues": [{"start": 10, "end": 15, "text": "我的联系方式：teacher_2026。"}]})
        put(work / "pdf.json", {"metadata": {"page_count": 2}, "blocks": [
            {"locator": {"page_number": 1}, "text": "参考比例"}
        ]})
        put(work / "zip.json", {"metadata": {"archive_members": [
            {"name": "笔记/", "is_directory": True}, {"name": "笔记/复查.md", "is_directory": False}
        ]}})
        put(ledger / "documents-summary.json", {"records": [
            {"id": "D001", **originals[1], "status": "partial", "result": "pdf.json"},
            {"id": "D002", **originals[2], "status": "partial", "result": "zip.json"},
        ]})
        self.body = "\n\n".join(("# 教材", "## 第 1 讲", START, FINISH, "## 随课资料", AUTHOR, FIRST_PAGE,
                                SECOND_PAGE, MEMBER))
        self.scope, self.evidence = build_source_manifest(project)
        coverage = coverage_review_template(self.scope, self.body)
        self.review = {"source_coverage": coverage, "episodes": [{
            "lesson": 1, "watched": [{"range_s": [0, 240], "kind": "教学", "note": "依次演示画板和导出"}],
            "source_points": [{"range_s": [0, 120], "anchor": "打开画板并确认比例"},
                              {"range_s": [120, 240], "anchor": "完成导出并检查预览"}],
        }]}
        video, = coverage["videos"]
        for window in video["windows"]:
            window.update(outcome="正文已覆盖", note="已回对本段操作顺序和看到的结果")
        for zone in video["channels"]:
            zone.update(outcome="已查", note="对照原画界面和画面角落的署名信息")
        candidate, = video["candidates"]
        candidate.update(kind="作者渠道", note="讲师口播主动给出自己的课程联系方式", anchor="原作者联系方式")
        first, second = coverage["documents"][0]["units"]
        first.update(outcome="正文已覆盖", note="参考比例保留于配套资料节", anchor="画板说明中的参考比例")
        second.update(outcome="正文已覆盖", note="读回扫描的练习页并补上用法", anchor="课后练习说明")
        member, = coverage["documents"][1]["units"]
        member.update(outcome="正文已覆盖", note="打开档内文档核对规则和使用场景", anchor="按压缩包内的规则复查导出")
        self.set_claims(self.body)

    def set_claims(self, body):
        claims = coverage_review_template(self.scope, body)["claims"]
        for claim in claims:
            claim.update(outcome="原件已核", note="已回对原始位置及对应操作的条件")
        self.review["source_coverage"]["claims"] = claims

    def test_full_review_and_independent_inventory(self):
        self.assertEqual(self.scope["videos"][0]["windows"], [[0, 120], [120, 240]])
        self.assertEqual([unit["key"] for unit in self.scope["documents"][0]["units"]], ["page:1", "page:2"])
        self.assertEqual(self.scope["videos"][0]["candidates"][0]["value"], "teacher_2026")
        self.assertIn("work/pdf.json", self.evidence)
        validate_source_coverage(self.body, self.scope, self.review)

    def test_pdf_links_are_reviewed_with_their_original_pages(self):
        links = [
            {"page_number": 2, "target": "https://example.org/supplement",
             "intersects_page_box": True, "loaded": False},
            {"page_number": 2, "target": "https://example.org/cropped",
             "intersects_page_box": False, "loaded": False},
        ]
        units = document_units({"metadata": {"page_count": 2, "pdf_hyperlinks": links}})
        self.assertNotIn("pdf_hyperlinks", units[0])
        self.assertEqual(units[1]["key"], "page:2")
        self.assertEqual(units[1]["pdf_hyperlinks"], links)

    def test_escaped_attachment_filename_still_resolves_to_the_original(self):
        self.scope["documents"][0]["source"] = "[26]--图示&练习.pdf"
        body = self.body.replace("配套讲义.pdf", "[26]&#45;&#45;图示&amp;练习.pdf")
        self.set_claims(body)
        validate_source_coverage(body, self.scope, self.review)

    def test_missing_lesson_or_unaccounted_times_are_rejected(self):
        video = self.review["source_coverage"]["videos"].pop()
        with self.assertRaisesRegex(ValueError, "原视频来源复核"):
            validate_source_coverage(self.body, self.scope, self.review)
        self.review["source_coverage"]["videos"].append(video)
        self.review["episodes"][0]["source_points"][0]["range_s"] = [0, 1]
        with self.assertRaisesRegex(ValueError, "1–120 秒未对应正文"):
            validate_source_coverage(self.body, self.scope, self.review)
        video["windows"][0]["outcome"] = "无独立教学"
        with self.assertRaisesRegex(ValueError, "时窗用途与实际来源要点不一致"):
            validate_source_coverage(self.body, self.scope, self.review)

    def test_missing_attachment_page_or_member_is_rejected(self):
        pdf, archive = self.review["source_coverage"]["documents"]
        second = pdf["units"].pop()
        with self.assertRaisesRegex(ValueError, "配套讲义.pdf 页、表或档内成员"):
            validate_source_coverage(self.body, self.scope, self.review)
        pdf["units"].append(second)
        archive["units"].clear()
        with self.assertRaisesRegex(ValueError, "配套练习.zip 页、表或档内成员"):
            validate_source_coverage(self.body, self.scope, self.review)

    def test_detected_author_channel_must_appear_in_reader_prose(self):
        body = self.body.replace(AUTHOR, "欢迎课后讨论。 <!-- 段落出处 第 1 讲 00:10–00:15 -->")
        self.set_claims(body)
        with self.assertRaisesRegex(ValueError, "已确认的作者渠道缺同段可见正文及原件出处"):
            validate_source_coverage(body, self.scope, self.review)
        self.review["source_coverage"]["videos"][0]["candidates"].clear()
        with self.assertRaisesRegex(ValueError, "作者候选"):
            validate_source_coverage(body, self.scope, self.review)

    def test_spoken_invitations_require_a_source_review_without_a_transcribed_name(self):
        cues = [
            {"start": 0, "end": 4, "text": "视频号可以发布自己的原创短片。"},
            {"start": 4, "end": 9, "text": "有兴趣可以关注一下我自己的公众号，课后案例还会继续分享。"},
            {"start": 9, "end": 14, "text": "可以在原视频底下留言，我会把提示词文件给你。"},
        ]
        candidates = video_candidates(1, 14, cues)
        self.assertEqual([item["range_s"] for item in candidates], [[4, 9], [9, 14]])
        self.assertTrue(all(item["value"] == "" for item in candidates))

    def test_class_support_roles_without_a_public_account_are_still_candidates(self):
        cues = [
            {"start": 0, "end": 4, "text": "笔记已经发到各位团长手里。"},
            {"start": 4, "end": 9, "text": "提示词需要的话可以找你们的团长。"},
            {"start": 9, "end": 14, "text": "不清楚的在交流群艾特咱们的值班老师。"},
            {"start": 14, "end": 20, "text": "不会的话也可以给我发信息。"},
        ]
        candidates = video_candidates(1, 20, cues)
        self.assertEqual([item["range_s"] for item in candidates], [[4, 9], [9, 14], [14, 20]])
        self.assertTrue(all(item["value"] == "" for item in candidates))

    def test_attachment_with_a_chinese_channel_name_remains_a_review_candidate(self):
        result = {"blocks": [{"text": "公众号：『示范课堂·第2站』。可查询课后问题。"}]}
        candidate, = document_candidates("D001", "说明.docx", result, {})
        self.assertEqual(candidate["value"], "示范课堂·第2站")
        self.assertEqual(candidate["unit"], "file")

    def test_screenshot_project_links_keep_their_pages_and_partial_values(self):
        result = {"metadata": {"page_count": 3}, "blocks": [
            {"locator": {"page_number": 1}, "text": "github.com/example-pr..."},
            {"locator": {"page_number": 2}, "text": "普通附录。"},
            {"locator": {"page_number": 3},
             "text": "github.com/\n example-project\n\n gitee.com/tutorials/demo"},
        ]}
        units = {item["key"]: item for item in document_units(result)}
        candidates = document_candidates("D001", "课程.pdf", result, units)
        self.assertEqual([(item["unit"], item["value"]) for item in candidates], [
            ("page:1", ""), ("page:3", "github.com/example-project"), ("page:3", "gitee.com/tutorials/demo"),
        ])
        self.assertIn("example-pr...", candidates[0]["cue"])

    def test_project_links_in_pdf_annotations_enter_review_with_stated_location(self):
        target = "https://github.com/example-project/course"
        result = {"metadata": {"page_count": 2, "pdf_hyperlinks": [
            {"page_number": 1, "target": target, "intersects_page_box": True},
            {"page_number": 2, "target": target, "intersects_page_box": False},
        ]}, "blocks": [
            {"locator": {"page_number": 1}, "text": "github.com/example-project/course"},
        ]}
        units = {item["key"]: item for item in document_units(result)}
        first, second = document_candidates("D001", "课程.pdf", result, units)
        self.assertEqual(first["value"], "github.com/example-project/course")
        self.assertEqual(second["unit"], "page:2")
        self.assertEqual(second["value"], target)
        self.assertIn("PDF 链接注释", second["cue"])

    def test_two_bare_project_links_in_the_same_paragraph_remain_distinct(self):
        result = {"blocks": [{"text": "github.com/example-a gitee.com/example-b"}]}
        candidates = document_candidates("D001", "补充.txt", result, {})
        self.assertEqual({item["value"] for item in candidates}, {"github.com/example-a", "gitee.com/example-b"})
        self.assertEqual(len({item["id"] for item in candidates}), 2)

    def test_original_supplement_reference_with_external_provenance_on_same_line(self):
        source = "配套讲义.pdf · 第 2 页"
        body = f"原作者入口。 <!-- 段落出处 {source} --> <!-- 外部出处 https://example.org/author -->"
        supplement = [{"anchor": "原作者入口", "sources": [source]}]
        validate_supplements(body, supplement, supplement)

    def test_manually_read_author_identifier_matches_the_same_source_block(self):
        self.scope["videos"][0]["candidates"].clear()
        video = self.review["source_coverage"]["videos"][0]
        video["candidates"].clear()
        screen = next(zone for zone in video["channels"] if zone["zone"] == "screen")
        screen["findings"].append({"kind": "作者渠道", "range_s": [11, 14], "anchor": "原作者联系方式",
                                   "value": "teacher_2026", "note": "连续原画都标记为课后答疑，已复读账号字样。"})
        validate_source_coverage(self.body, self.scope, self.review)

        for body in (self.body.replace("teacher_2026", "teacher_20260"),
                     self.body.replace("00:10–00:15", "00:15–00:20")):
            with self.subTest(body=body):
                self.set_claims(body)
                with self.assertRaisesRegex(ValueError, "已确认的作者渠道缺同段可见正文及原件出处"):
                    validate_source_coverage(body, self.scope, self.review)

    def test_nonexistent_page_inside_claimed_range_is_rejected(self):
        body = self.body.replace("第 1 页 -->", "第 1–999 页 -->")
        self.set_claims(body)
        with self.assertRaisesRegex(ValueError, "正文附件定位无法在原页"):
            validate_source_coverage(body, self.scope, self.review)

    def test_sheet_and_archive_locators_match_exactly(self):
        workbook = {"units": [{"kind": "sheet", "value": "Sheet1", "dimension": "A1:Z40"}]}
        self.assertTrue(document_locator_valid(workbook, "Sheet1 c3:F4"))
        self.assertFalse(document_locator_valid(workbook, "Sheet10 C3:F4"))
        self.assertFalse(document_locator_valid(workbook, "Sheet1 C3:F999"))
        archive = {"units": [{"kind": "member", "value": "笔记/复查.md"}]}
        self.assertTrue(document_locator_valid(archive, "档内 笔记/复查.md 第 1 段"))
        self.assertFalse(document_locator_valid(archive, "档内 笔记/复查.md.old"))

    def test_sheet_locators_use_actual_cells_when_declared_dimension_is_stale(self):
        source = {
            "metadata": {"sheets": [{"sheet_name": "工作表1", "dimension": "F43"},
                                    {"sheet_name": "示例", "dimension": "A1:B9"}]},
            "blocks": [
                {"locator": {"sheet_name": "工作表1", "cell_range": address}}
                for address in ("A3:F3", "A22:F22", "H23:K23", "H26:K26")
            ] + [{"locator": {"sheet_name": "示例", "cell_range": "A1:B2"}}],
        }
        first, second = document_units(source)
        self.assertEqual(first["observed_cell_bounds"], [1, 3, 11, 26])
        self.assertNotIn("observed_cell_bounds", second)
        workbook = {"units": [first, second]}
        self.assertTrue(document_locator_valid(workbook, "工作表1 A3:F22"))
        self.assertTrue(document_locator_valid(workbook, "工作表1 H23:K26"))
        self.assertFalse(document_locator_valid(workbook, "工作表1 F43"))
        self.assertFalse(document_locator_valid(workbook, "工作表1 L25:L26"))
        self.assertFalse(document_locator_valid(workbook, "示例 H23:K26"))

    def test_static_and_nested_members_use_full_archive_inventory(self):
        archive = "配套练习.zip"
        path = self.project.ledger / "media-inventory.json"
        original = {"source": archive, "document": archive, "member_chain": [], "member_count": 3,
                    "members": [{"path": "笔记/复查.md", "status": "file"},
                                {"path": "配套/样例.txt", "status": "file"},
                                {"path": "附件.zip", "status": "archive"}]}
        nested = {"source": f"{archive}!附件.zip", "document": archive, "member_chain": ["附件.zip"],
                  "member_count": 1, "members": [{"path": "讲师资料.pdf", "status": "file"}]}
        put(path, {"archives": [original, nested]})
        scope, _ = build_source_manifest(self.project)
        self.assertEqual([unit["key"] for unit in scope["documents"][1]["units"]], [
            "member:笔记/复查.md", "member:配套/样例.txt", "member:附件.zip", "member:附件.zip!讲师资料.pdf",
        ])
        nested["members"].clear()
        put(path, {"archives": [original, nested]})
        with self.assertRaisesRegex(ValueError, "压缩包未登记完整成员索引"):
            build_source_manifest(self.project)
