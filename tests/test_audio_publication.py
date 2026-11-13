"""Eleven TempStore tests, short real WAVs/decoder, no real GDB/ASR/model/network."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

from worldanalysis import audio_publication as publication
from worldanalysis import media
from worldanalysis.analysis import Analysis
from worldanalysis.contracts import ContractError, canonical, hash_file, now, validate_bundle, validate_plan
from worldanalysis.store import Store


def cleanup(path):
    def writable(func, name, exc):
        Path(name).chmod(stat.S_IWRITE)
        func(name)
    shutil.rmtree(path, onerror=writable)


def pcm(start, count):
    return struct.pack("<" + "h" * count, *((i % 12000) - 6000 for i in range(start, start + count)))


class Fixture:
    def setup(self, owner, *, samples=96000):
        self.root = Path(tempfile.mkdtemp(prefix="synthetic-audio-publication-", dir=Path(__file__).parent)).resolve()
        owner.addCleanup(cleanup, self.root)
        self.store = Store(self.root)
        owner.addCleanup(lambda: self.store.close())
        self.project = self.store.create_project(project_id="audio-project", topic="Synthetic unheard native audio")
        self.source = self.root / "synthetic.wav"
        with wave.open(str(self.source), "wb") as sink:
            sink.setnchannels(1)
            sink.setsampwidth(2)
            sink.setframerate(16000)
            for i in range(0, samples, 4000):
                sink.writeframesraw(pcm(i, min(4000, samples - i)))
        def worker(source, output, *, options):
            # Real PyAV source decoding and inventories, no model/provider fake.
            output.mkdir(parents=True, exist_ok=True)
            raw = media._process(source, output, options)
            raw["blocks"].append({"kind": "paragraph", "origin": "native_extract", "locator": {"kind": "paragraph", "precision": "exact", "paragraph_index": 0},
                "text": "Separate literal contact fixture@example.invalid", "file_keys": [], "processing_status": "extracted", "reason": "Synthetic contact preservation fixture, not audio transcript"})
            raw["gaps"].extend({"kind": "semantic_gap", "severity": "informational", "reason": "Preserved synthetic historical gap " + str(i), "block_indexes": []} for i in range(3))
            return raw
        original = self.store._build_bundle
        def historic(*args):
            bundle, plan = original(*args)
            for gap, state in zip(bundle["gaps"][-3:], ("requested", "resolved", "accepted_limitation")):
                gap["status"] = state
                if state == "resolved":
                    block = bundle["blocks"][-1]
                    gap["resolution_refs"] = [{"project_id": self.project, **block["asset_ref"], "block_id": block["block_id"], "locator": deepcopy(block["locator"])}]
            return bundle, plan
        with patch.object(self.store, "_build_bundle", side_effect=historic):
            self.bundle = self.store.ingest_media(self.project, [self.source], worker, config_version="synthetic-native-v1",
                options={"asr_enabled": False, "ocr_enabled": False, "synthetic": True, "window_seconds": max(6, (samples + 15999) // 16000), "overlap_seconds": 0})
        self.asset = self.bundle["assets"][0]
        self.project_root = self.store.project_root(self.project)
        self.old_files = {a["relative_path"]: hash_file(self.project_root / a["relative_path"]) for a in self.bundle["artifacts"]}
        self.old_jobs = [tuple(r) for r in self.store.connection.execute("SELECT * FROM jobs ORDER BY job_id")]
        self.original_hash = hash_file(self.source)
        self.analysis_task = None

    def publish(self, *, start=2940, end=4420, output="窗口派生/window.wav", request_id=None, max_received_bytes=None):
        return publication.publish_audio_window(self.store, self.project, self.bundle["bundle_id"], self.asset["asset_id"], self.asset["asset_revision"],
            start, end, output_relative=output, request_id=request_id, max_received_bytes=max_received_bytes)

    def counts(self):
        return tuple(self.store.connection.execute("SELECT (SELECT COUNT(*) FROM jobs),(SELECT COUNT(*) FROM publications),(SELECT COUNT(*) FROM plans)").fetchone())

    def latest(self):
        return self.store.connection.execute("SELECT * FROM publications ORDER BY revision DESC LIMIT 1").fetchone()

    def restart(self):
        self.store.close()
        self.store = Store(self.root)

    def request(self, request_id="audio-request", *, start=2940, end=4420, target=None, outputs=None):
        analysis = Analysis(self.store)
        if self.analysis_task is None:
            task = analysis.claim(self.project, model_version="synthetic-proposal-not-listening-1")
            self.analysis_task = task["task_id"]
            result = {"schema_version": "1.0-draft", "analysis_id": "synthetic-audio-analysis", "analysis_revision": "v1", "project_id": self.project,
                "based_bundle_id": self.bundle["bundle_id"], "based_bundle_revision": self.bundle["bundle_revision"], "based_publish_id": self.bundle["publish_id"],
                "created_at": now(), "execution_status": "blocked", "agent": deepcopy(task["agent"]),
                "block_processing": [{"block_id": b["block_id"], "asset_ref": b["asset_ref"], "status": "pending", "priority": "normal", "reason": "Unread/unheard proposal fixture only", "read_receipt_refs": [], "exclusion": None} for b in self.bundle["blocks"]],
                "coverage": {"scope": "based_bundle_only", "source_inventory_state": self.bundle["coverage"]["inventory_state"], "discovered_blocks": len(self.bundle["blocks"]),
                    "read_blocks": 0, "failed_blocks": 0, "pending_blocks": len(self.bundle["blocks"]), "excluded_blocks": 0, "not_applicable_blocks": 0, "claim": "partial", "limitations": ["No semantic listening."]},
                "assertions": [], "external_sources": [], "gaps": deepcopy(self.bundle["gaps"]), "supplement_requests": [], "main_report_relative_path": None, "reports": [],
                "program_validation": {"status": "pending", "validator_version": "untrusted", "checked_at": None, "report_relative_path": None}}
            submission = self.project_root / task["output_relative_path"] / "proposal.json"
            submission.write_bytes(canonical(result))
            analysis.complete(task["task_id"], submission)
        request = {"request_id": request_id, "gap_ids": [self.bundle["gaps"][0]["gap_id"]], "reason": "Explicit audio window, not permission. Never parse 0..999999 from this reason.",
            "minimal_targets": [target or {"project_id": self.project, "asset_id": self.asset["asset_id"], "asset_revision": self.asset["asset_revision"],
                "locator": {"kind": "time", "precision": "exact", "start_ms": start, "end_ms": end}}],
            "requested_outputs": outputs or ["audio_window"], "priority": "important", "status": "proposed", "requires_scope_expansion": False}
        analysis.request_supplement(self.analysis_task, request)
        return request

    def assert_original(self, owner, *, check_source=True):
        owner.assertEqual(self.store.get_bundle(self.project, self.bundle["bundle_id"]), self.bundle)
        if check_source:
            owner.assertEqual(hash_file(self.source), self.original_hash)
        for relative, expected in self.old_files.items():
            owner.assertEqual(hash_file(self.project_root / relative), expected)
        for job in self.old_jobs:
            owner.assertEqual(tuple(self.store.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job[0],)).fetchone()), job)


class AudioPublicationTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.f.setup(self)

    def test_real_wav_original_schemas_store_analysis_unheard_and_all_state_preserved(self):
        f = self.f
        before = f.counts()
        result = f.publish()
        self.assertNotEqual(result["bundle_id"], f.bundle["bundle_id"])
        self.assertEqual(f.store.get_bundle(f.project, result["bundle_id"]), result)
        validate_bundle(result, f.project_root)
        plan = json.loads((f.project_root / result["publication"]["manifest_relative_path"]).with_name("plan.json").read_bytes())
        validate_plan(plan)
        self.assertEqual(result["assets"], f.bundle["assets"])
        self.assertEqual(result["blocks"][:-1], f.bundle["blocks"])
        self.assertEqual(result["artifacts"][:len(f.bundle["artifacts"])], f.bundle["artifacts"])
        self.assertEqual(result["contacts"], f.bundle["contacts"])
        self.assertTrue(result["contacts"])
        self.assertEqual(result["gaps"][:-1], f.bundle["gaps"])
        self.assertEqual({g["status"] for g in result["gaps"][:-1]}, {"open", "requested", "resolved", "accepted_limitation"})
        old_metric = next(m for m in f.bundle["coverage"]["per_asset"][0]["metrics"] if m["unit"] == "millisecond")
        self.assertEqual(result["coverage"]["per_asset"][0]["metrics"][0], old_metric)
        self.assertEqual(old_metric["processed"], 0)
        native = result["blocks"][-1]
        self.assertEqual(native["kind"], "audio_segment")
        self.assertNotIn("text", native)
        self.assertIn("UNHEARD", native["processing"]["reason"])
        wav = next(a for a in result["artifacts"] if a["kind"] == "clip")
        with wave.open(str(f.project_root / wav["relative_path"]), "rb") as sink:
            self.assertEqual((sink.getframerate(), sink.getnchannels(), sink.getsampwidth(), sink.getnframes()), (16000, 1, 2, 23680))
            self.assertEqual(sink.readframes(23680), pcm(2940 * 16, 23680))
        metadata = json.loads((f.project_root / wav["relative_path"]).with_name("audio-window.json").read_bytes())
        self.assertIsNone(metadata["source_content_sha256"])
        self.assertFalse(metadata["semantic_read_performed"])
        self.assertFalse(metadata["proof"]["asr_executed"])
        self.assertEqual(metadata["proof"]["received_bytes_scope"], "actual_container_bytes_including_probe_seek_rereads_and_mixed_streams")
        new_job = next(j for a in plan["asset_plans"] for j in a["jobs"] if j["job_id"] == wav["job_id"])
        self.assertEqual(new_job["estimate"]["input_bytes"], metadata["proof"]["received_bytes"])
        self.assertEqual(new_job["operation"], "native_extract")
        self.assertEqual(new_job["tier"], "structured_local")
        self.assertEqual(f.counts(), (before[0] + 1, before[1] + 1, before[2] + 2))
        analysis = Analysis(f.store)
        task = analysis.claim(f.project, model_version="synthetic-not-listening-1")
        self.assertEqual(task["bundle_id"], result["bundle_id"])
        self.assertEqual(analysis.read(task["task_id"], [native["block_id"]])["items"][0]["status"], "unavailable")
        self.assertEqual(f.store.connection.execute("SELECT COUNT(*) FROM read_receipts").fetchone()[0], 0)
        f.assert_original(self)

    def test_duplicate_cross_request_and_primitive_existing_wav_reuse_no_new_decoder(self):
        f = self.f
        first_request, second_request = f.request(), f.request("another-audio-request")
        # Existing primitive output is reusable, but not evidence until publication.
        from worldanalysis.media_store import identity
        publication.primitive.extract_audio_window(f.root, f.source, "窗口派生/window.wav", 2940, 4420, expected_identity=identity(f.source), max_received_bytes=1048576)
        before = f.counts()
        with patch.object(publication.primitive, "_decode", side_effect=AssertionError("existing window cannot decode")):
            # A mismatched default cap fails BEFORE even a new intention/job.
            with self.assertRaises(ContractError):
                f.publish(request_id=first_request["request_id"])
            for value in (True, 0, -1, 67108865):
                with self.subTest(cap=value), self.assertRaises(ContractError):
                    f.publish(request_id=first_request["request_id"], max_received_bytes=value)
            self.assertEqual(f.counts(), before)
            first = f.publish(request_id=first_request["request_id"], max_received_bytes=1048576)
        before = f.counts()
        with patch.object(publication.primitive, "extract_audio_window", side_effect=AssertionError("published repeat cannot extract")):
            second = f.publish(request_id=second_request["request_id"], output="窗口派生/alias.wav", max_received_bytes=1048576)
        self.assertEqual(first["publish_id"], second["publish_id"])
        self.assertTrue(second["reused"])
        self.assertEqual(f.counts(), before)
        self.assertFalse((f.root / "窗口派生/alias.wav").exists())
        self.assertEqual({r[0] for r in f.store.connection.execute("SELECT state FROM supplement_requests")}, {"queued"})
        f.assert_original(self)

    def test_bounds_wrong_revision_cross_project_and_output_paths_reject_before_job(self):
        f = self.f
        before = f.counts()
        with patch.object(publication.primitive, "extract_audio_window", side_effect=AssertionError("invalid cannot decode")):
            for start, end in ((True, 2), (0, True), (0.0, 1), (0, 0), (3, 2), (-1, 1), (0, 300001), (5900, 6100)):
                with self.subTest(start=start, end=end), self.assertRaises(ContractError):
                    f.publish(start=start, end=end)
            for output in ("synthetic.wav", "原始材料/old.wav", "证据/v0001/old.wav", "窗口派生/../bad.wav"):
                with self.assertRaises(ContractError):
                    f.publish(output=output)
            other = f.store.create_project(project_id="other-project", topic="No media grant")
            for project, revision in ((other, f.asset["asset_revision"]), (f.project, "wrong-revision")):
                with self.assertRaises(ContractError):
                    publication.publish_audio_window(f.store, project, f.bundle["bundle_id"], f.asset["asset_id"], revision, 1, 2, output_relative="窗口派生/no.wav")
        self.assertEqual(f.counts(), before)

    def test_permission_paid_budget_pause_and_wrong_request_reject_without_decoder(self):
        f = self.f
        wrong = f.request("wrong-time", start=1, end=2)
        unsupported = f.request("wrong-output", outputs=["transcript"])
        before = f.counts()
        real_source = publication._source
        def denied(*args):
            source = real_source(*args)
            source["plan"]["permission_checks"][0]["state"] = "denied"
            return source
        def budgetless(*args):
            source = real_source(*args)
            source["plan"]["hard_budget"]["max_input_bytes"] = 0
            return source
        with patch.object(publication.primitive, "extract_audio_window", side_effect=AssertionError("denied cannot decode")):
            for request in (wrong["request_id"], unsupported["request_id"], "unknown-request"):
                with self.assertRaises(ContractError):
                    f.publish(request_id=request)
            for altered in (denied, budgetless):
                with patch.object(publication, "_source", side_effect=altered), self.assertRaises(ContractError):
                    f.publish()
            job = f.old_jobs[0][0]
            f.store.connection.execute("UPDATE jobs SET max_cost_minor=1 WHERE job_id=?", (job,))
            with self.assertRaises(ContractError):
                f.publish()
            f.store.connection.execute("UPDATE jobs SET max_cost_minor=0 WHERE job_id=?", (job,))
            f.store.control("collection", "pause")
            self.assertEqual(f.publish()["status"], "blocked")
        self.assertEqual(f.counts(), before)
        self.assertFalse((f.root / "窗口派生").exists())

    def test_stat_change_before_after_decode_and_cached_repeat_are_not_hidden(self):
        f = self.f
        first = f.publish()
        before = f.counts()
        with f.source.open("ab") as handle:
            handle.write(b"outside-window changed")
        with patch.object(publication.primitive, "extract_audio_window", side_effect=AssertionError("changed cannot decode")), self.assertRaises(ContractError):
            f.publish()
        # A historical immutable publication remains readable; no source hash.
        self.assertEqual(f.store.get_bundle(f.project, first["bundle_id"]), first)
        self.assertEqual(f.counts(), before)
        f.assert_original(self, check_source=False)

    def test_actual_descriptor_timing_and_received_bytes_are_checked(self):
        f = self.f
        real = publication.primitive.extract_audio_window
        def forged(*args, **kwargs):
            result = real(*args, **kwargs)
            result["received_bytes_scope"] = "only_audio_transferred"
            return result
        with patch.object(publication.primitive, "extract_audio_window", side_effect=forged), self.assertRaises(ContractError):
            f.publish()
        self.assertEqual(f.latest()["state"], "staging")
        with patch.object(publication.primitive, "extract_audio_window", side_effect=AssertionError("unknown attempt cannot replay")), self.assertRaises(ContractError):
            f.publish()
        f.assert_original(self)

    def test_native_pcm_old_143003_samples_rounds_minus_ten_bytes_but_new_wav_exact(self):
        f = Fixture()
        f.setup(self, samples=143003)
        native = publication._native(f.store, publication._source(f.store, f.project, f.bundle["bundle_id"]),
            f.asset["asset_id"], f.asset["asset_revision"], publication._window(2940, 4420))
        descriptor = native["native_pcm_sources"][0]["descriptor"]
        self.assertEqual((descriptor["start_ms"], descriptor["end_ms"], descriptor["pcm_bytes"]), (0, 8938, 286006))
        self.assertEqual(descriptor["pcm_bytes"] - (descriptor["end_ms"] - descriptor["start_ms"]) * 32, -10)
        result = f.publish()
        artifact = next(a for a in result["artifacts"] if a["kind"] == "clip")
        with wave.open(str(f.project_root / artifact["relative_path"]), "rb") as check:
            self.assertEqual(check.getnframes(), 23680)
            self.assertEqual(check.readframes(23680), pcm(2940 * 16, 23680))
        self.assertEqual(artifact["byte_length"], 44 + 23680 * 2)
        self.assertEqual(f.store.get_bundle(f.project, result["bundle_id"]), result)
        f.assert_original(self)

    def test_native_pcm_old_descriptor_rejects_over_one_ms_odd_zero_or_bool_bytes(self):
        f = self.f
        source = publication._source(f.store, f.project, f.bundle["bundle_id"])
        read_json = publication._json
        before = f.counts()
        for value in (192034, 191966, 192001, 0, True):
            def malformed(*args, **kwargs):
                result = read_json(*args, **kwargs)
                if "pcm_bytes" in result:
                    result["pcm_bytes"] = value
                return result
            with self.subTest(pcm_bytes=value), patch.object(publication, "_json", side_effect=malformed), self.assertRaisesRegex(ContractError, "native_pcm_source"):
                publication._native(f.store, source, f.asset["asset_id"], f.asset["asset_revision"], publication._window(2940, 4420))
        self.assertEqual(f.counts(), before)
        f.assert_original(self)

    def test_validated_crash_recovery_preserves_receipts_and_no_decode(self):
        f = self.f
        with patch.object(f.store, "_finish_publication", side_effect=RuntimeError("validated fault")), self.assertRaisesRegex(RuntimeError, "validated fault"):
            f.publish()
        self.assertEqual(f.latest()["state"], "validated")
        with patch.object(publication.primitive, "extract_audio_window", side_effect=AssertionError("recovery cannot extract")):
            f.restart()
        self.assertEqual(f.latest()["state"], "published")
        f.assert_original(self)

    def test_rename_and_db_receipt_faults_recover_existing_journal(self):
        f = self.f
        with patch.object(f.store, "_after_rename", side_effect=RuntimeError("rename fault")), self.assertRaisesRegex(RuntimeError, "rename fault"):
            f.publish()
        self.assertEqual(f.latest()["state"], "validated")
        f.store.connection.execute("CREATE TRIGGER synthetic_audio_receipt_fault BEFORE UPDATE OF receipt_json ON jobs WHEN NEW.receipt_json IS NOT OLD.receipt_json BEGIN SELECT RAISE(FAIL,'synthetic receipt fault'); END")
        with self.assertRaises(Exception):
            f.store._finish_publication(f.latest()["publish_id"])
        self.assertEqual(f.latest()["state"], "renamed")
        self.assertEqual(f.store.connection.execute("SELECT COUNT(*) FROM jobs WHERE state='verified_complete'").fetchone()[0], 1)
        f.store.connection.execute("DROP TRIGGER synthetic_audio_receipt_fault")
        with patch.object(publication.primitive, "extract_audio_window", side_effect=AssertionError("recovery cannot extract")):
            f.restart()
        self.assertEqual(f.latest()["state"], "published")
        f.assert_original(self)

    def test_recovery_after_source_stat_change_quarantines_not_publish_or_decode(self):
        f = self.f
        with patch.object(f.store, "_finish_publication", side_effect=RuntimeError("prepared fault")), self.assertRaises(RuntimeError):
            f.publish()
        with f.source.open("ab") as handle:
            handle.write(b"source changed during crash")
        with patch.object(publication.primitive, "extract_audio_window", side_effect=AssertionError("recovery cannot extract")):
            f.restart()
        self.assertEqual(f.latest()["state"], "quarantined")
        self.assertEqual(f.store.connection.execute("SELECT COUNT(*) FROM publications WHERE state='published'").fetchone()[0], 1)
        f.assert_original(self, check_source=False)


if __name__ == "__main__":
    unittest.main()
