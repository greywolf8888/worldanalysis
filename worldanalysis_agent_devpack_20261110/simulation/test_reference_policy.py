"""Known-fixture policy tests; no network, model, or performance tests."""
from decimal import Decimal as D
from pathlib import Path
import sqlite3
import tempfile
import unittest

from reference_policy import (
    BundlePublisher, ByteBudget, CommercialBudget, CourseIdentity, Evidence,
    PolicyError, SimulatedCrash, asr_hours, asr_price_cny, contact_decision,
    dedup_decision, encoded_gb, evidence_state, network_lower_bound_hours,
    pcm_bytes, failure_action, validate_baidu_path, verify_range_response,
)


class CapacityTests(unittest.TestCase):
    def test_3mbps_30_and_100_hours(self):
        self.assertEqual([encoded_gb(h, 3) for h in (30, 100)], [D("40.5"), D(135)])

    def test_1mbps_30_and_100_hours(self):
        self.assertEqual([encoded_gb(h, 1) for h in (30, 100)], [D("13.5"), D(45)])

    def test_48kbps_30_and_100_hours(self):
        self.assertEqual([encoded_gb(h, ".048") for h in (30, 100)], [D(".648"), D("2.16")])

    def test_pcm16_30_and_100_hours(self):
        self.assertEqual([pcm_bytes(h * 3600) / D(10**9) for h in (30, 100)],
                         [D("3.456"), D("11.52")])

    def test_float32_30_and_100_hours(self):
        self.assertEqual([pcm_bytes(h * 3600, bytes_per_sample=4) / D(10**9)
                          for h in (30, 100)], [D("6.912"), D("23.04")])

    def test_five_minute_raw_audio_not_total_process_memory(self):
        self.assertEqual(pcm_bytes(300), D(9_600_000))
        self.assertEqual(pcm_bytes(300, bytes_per_sample=4), D(19_200_000))

    def test_20mbps_network_bounds(self):
        for hours in (30, 100):
            for mbps in (D(3), D(1), D(".048")):
                with self.subTest(hours=hours, mbps=mbps):
                    self.assertEqual(network_lower_bound_hours(encoded_gb(hours, mbps), 20),
                                     D(hours) * mbps / D(20))

    def test_assumed_rtfs(self):
        self.assertEqual([asr_hours(30, r) for r in (".02", ".05", ".10")],
                         [D(".6"), D("1.5"), D(3)])
        self.assertEqual([asr_hours(100, r) for r in (".02", ".05", ".10")],
                         [D(2), D(5), D(10)])

    def test_official_price_snapshot(self):
        self.assertEqual([asr_price_cny(h, "beijing") for h in (30, 100)],
                         [D("23.76"), D("79.2")])
        self.assertEqual([asr_price_cny(h, "singapore") for h in (30, 100)],
                         [D("28.08"), D("93.6")])

    def test_invalid_units_rejected(self):
        for value in (-1, "NaN", "Infinity"):
            with self.subTest(value=value), self.assertRaises(PolicyError):
                encoded_gb(value, 1)
        with self.assertRaises(PolicyError):
            network_lower_bound_hours(1, 0)


class EvidenceTests(unittest.TestCase):
    def test_tiny_txt_is_not_discarded(self):
        self.assertEqual(evidence_state(Evidence(text="密码见第2页", coverage="complete")),
                         "READY_VERIFIED")

    def test_empty_text_is_not_verified(self):
        self.assertEqual(evidence_state(Evidence(coverage="complete")), "READY_WITH_GAPS")

    def test_missing_images_block_verification(self):
        evidence = Evidence("文字很完整", "complete", 3, 2)
        self.assertEqual(evidence_state(evidence), "READY_WITH_GAPS")

    def test_unknown_image_inventory_blocks_verification(self):
        self.assertEqual(evidence_state(Evidence("正文", "complete", None, 0)), "READY_WITH_GAPS")

    def test_unknown_coverage_blocks_verification(self):
        self.assertEqual(evidence_state(Evidence(text="一些文稿")), "READY_WITH_GAPS")

    def test_ai_summary_is_not_full_transcription(self):
        evidence = Evidence("概要", "complete", 0, 0, True, "ai_summary")
        self.assertEqual(evidence_state(evidence), "READY_WITH_GAPS")

    def test_full_transcript_with_complete_fixture_coverage(self):
        evidence = Evidence("已核实逐字稿", "complete", 1, 1, True, "full_transcript")
        self.assertEqual(evidence_state(evidence), "READY_VERIFIED")

    def test_silent_visual_material_is_kept(self):
        evidence = Evidence("", "complete", 2, 2, False, "none")
        self.assertEqual(evidence_state(evidence), "READY_VERIFIED")


class IdentityAndContactTests(unittest.TestCase):
    def test_same_author_content_version_reuses_with_provenance(self):
        a = CourseIdentity("author-a", "fingerprint-1", "v1")
        self.assertEqual(dedup_decision(a, a), "REUSE_CONTENT_KEEP_PROVENANCE")

    def test_different_authors_are_kept(self):
        a = CourseIdentity("author-a", "fingerprint-1", "v1")
        b = CourseIdentity("author-b", "fingerprint-1", "v1")
        self.assertEqual(dedup_decision(a, b), "KEEP_SEPARATE_AUTHORS")

    def test_different_contents_are_kept(self):
        a = CourseIdentity("author-a", "fingerprint-1", "v1")
        b = CourseIdentity("author-a", "fingerprint-2", "v1")
        self.assertEqual(dedup_decision(a, b), "KEEP_DIFFERENT_CONTENT")

    def test_changed_version_is_not_silently_collapsed(self):
        a = CourseIdentity("author-a", "fingerprint-1", "v1")
        b = CourseIdentity("author-a", "fingerprint-1", "v2")
        self.assertEqual(dedup_decision(a, b), "KEEP_VERSION_RELATION")

    def test_unknown_identity_does_not_authorize_skip(self):
        a = CourseIdentity(None, "fingerprint-1", None)
        self.assertEqual(dedup_decision(a, a), "KEEP_PENDING_IDENTITY")

    def test_unknown_edition_does_not_authorize_skip(self):
        a = CourseIdentity("author-a", "fingerprint-1", None)
        self.assertEqual(dedup_decision(a, a), "KEEP_PENDING_IDENTITY")

    def test_original_contact_is_kept(self):
        self.assertEqual(contact_decision(), "KEEP_WITH_SOURCE")

    def test_repeated_contact_is_flagged_not_deleted(self):
        self.assertEqual(contact_decision(True), "KEEP_AND_FLAG_FOR_REVIEW")


class PathAndRangeTests(unittest.TestCase):
    def test_authorized_root_and_child(self):
        for path in ("/apps/教育分析学习", "/apps/教育分析学习/", "/apps/教育分析学习/课程/视频.mp4"):
            self.assertEqual(validate_baidu_path(path), path.rstrip("/"))

    def test_prefix_collision_is_rejected(self):
        with self.assertRaises(PolicyError):
            validate_baidu_path("/apps/教育分析学习副本/video.mp4")

    def test_parent_dot_and_relative_paths_rejected(self):
        for path in ("/apps/教育分析学习/../其他", "/apps/教育分析学习/./a", "apps/教育分析学习/a"):
            with self.subTest(path=path), self.assertRaises(PolicyError):
                validate_baidu_path(path)

    def test_encoded_traversal_and_encoded_separator_rejected(self):
        for suffix in ("%2e%2e/other", "%252e%252e/other", "a%2fb", "a%5cb"):
            with self.subTest(suffix=suffix), self.assertRaises(PolicyError):
                validate_baidu_path("/apps/教育分析学习/" + suffix)

    def test_ambiguous_paths_rejected(self):
        for path in ("/apps/教育分析学习//a", "/apps/教育分析学习/\\other", "/apps/教育分析学习/a\x00"):
            with self.subTest(path=path), self.assertRaises(PolicyError):
                validate_baidu_path(path)

    def test_alternative_app_root_not_implicitly_authorized(self):
        with self.assertRaises(PolicyError):
            validate_baidu_path("/apps/bdpan/video.mp4")

    def test_range_200_disables_range_route(self):
        self.assertFalse(verify_range_response(200, "bytes 0-99/1000", 0, 99, 100))

    def test_range_206_is_verified(self):
        self.assertTrue(verify_range_response(206, "bytes 0-99/1000", 0, 99, 100))

    def test_range_wrong_body_or_offset_rejected(self):
        self.assertFalse(verify_range_response(206, "bytes 0-99/1000", 0, 99, 99))
        self.assertFalse(verify_range_response(206, "bytes 1-100/1000", 0, 99, 100))

    def test_unknown_total_not_treated_as_verified(self):
        self.assertFalse(verify_range_response(206, "bytes 0-99/*", 0, 99, 100))

    def test_byte_budget_clamps_next_read(self):
        budget = ByteBudget(100)
        budget.account_read(80)
        self.assertEqual(budget.allowance(64), 20)
        budget.account_read(20)
        self.assertEqual(budget.allowance(64), 0)

    def test_retry_bytes_still_count(self):
        budget = ByteBudget(100)
        budget.account_read(60)
        budget.account_read(30)
        self.assertEqual(budget.allowance(60), 10)

    def test_unexpected_overrun_stops_and_records_observed_bytes(self):
        budget = ByteBudget(100)
        with self.assertRaises(PolicyError):
            budget.account_read(101)
        self.assertEqual(budget.observed, 101)


class AuthorizationAndBudgetTests(unittest.TestCase):
    def test_permission_failure_does_not_switch_tools(self):
        for code in ("ACCESS_DENIED", "POLICY_BLOCKED", "AUTH_EXPIRED"):
            self.assertEqual(failure_action(code), "BLOCKED_NO_TOOL_SWITCH")

    def test_transient_failure_stays_in_authorized_route(self):
        self.assertEqual(failure_action("TIMEOUT"), "BOUNDED_RETRY_SAME_AUTHORIZED_ROUTE")

    def test_zero_budget_blocks_even_zero_estimate_new_external_request(self):
        budget = CommercialBudget(D(0), external_allowed=True)
        with self.assertRaises(PolicyError):
            budget.reserve("new-job", 0)

    def test_money_does_not_imply_permission_to_export(self):
        budget = CommercialBudget(D(100), external_allowed=False)
        with self.assertRaises(PolicyError):
            budget.reserve("new-job", 1)

    def test_unknown_submission_keeps_reservation_and_cannot_resubmit(self):
        budget = CommercialBudget(D(10), external_allowed=True)
        self.assertEqual(budget.reserve("request-1", 8), "RESERVED")
        budget.begin_submit("request-1")
        budget.mark_unknown("request-1")
        self.assertEqual(budget.reserved_total, D(8))
        self.assertEqual(budget.reserve("request-1", 8), "EXISTING_REQUEST_DO_NOT_RESUBMIT")
        with self.assertRaises(PolicyError):
            budget.begin_submit("request-1")
        with self.assertRaises(PolicyError):
            budget.reserve("request-2", 3)

    def test_dispatching_state_prevents_duplicate_send_before_receipt(self):
        budget = CommercialBudget(D(10), external_allowed=True)
        budget.reserve("request-1", 1)
        budget.begin_submit("request-1")
        with self.assertRaises(PolicyError):
            budget.begin_submit("request-1")

    def test_known_remote_task_is_retained(self):
        budget = CommercialBudget(D(10), external_allowed=True)
        budget.reserve("request-1", 1)
        budget.begin_submit("request-1")
        budget.mark_submitted("request-1", "remote-task-fixture")
        self.assertEqual(budget.reservations["request-1"].remote_task_id, "remote-task-fixture")


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="worldanalysis_policy_test_")
        self.base = Path(self.temp.name)
        self.db = sqlite3.connect(self.base / "fixture.sqlite")
        self.publisher = BundlePublisher(self.base, self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_crash_after_rename_recovers_idempotently(self):
        with self.assertRaises(SimulatedCrash):
            self.publisher.publish("course_1", "rev_1", {"text": "已核实材料"}, True)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bundles").fetchone()[0], 0)
        self.assertTrue((self.base / "published/course_1/rev_1/manifest.json").is_file())
        self.db.close()
        self.db = sqlite3.connect(self.base / "fixture.sqlite")
        self.publisher = BundlePublisher(self.base, self.db)
        self.assertEqual(self.publisher.recover(), 1)
        self.assertEqual(self.publisher.recover(), 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bundles").fetchone()[0], 1)

    def test_repeated_publish_preserves_single_revision(self):
        one = self.publisher.publish("course_1", "rev_1", {"text": "材料"})
        two = self.publisher.publish("course_1", "rev_1", {"text": "材料"})
        self.assertEqual(one, two)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bundles").fetchone()[0], 1)

    def test_same_revision_cannot_overwrite_different_payload(self):
        self.publisher.publish("course_1", "rev_1", {"text": "原材料"})
        with self.assertRaises(PolicyError):
            self.publisher.publish("course_1", "rev_1", {"text": "不同材料"})

    def test_tampered_orphan_not_registered_as_ready(self):
        with self.assertRaises(SimulatedCrash):
            self.publisher.publish("course_1", "rev_1", {"text": "原材料"}, True)
        (self.base / "published/course_1/rev_1/evidence.json").write_text("{}", "utf-8")
        with self.assertRaises(PolicyError):
            self.publisher.recover()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bundles").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

