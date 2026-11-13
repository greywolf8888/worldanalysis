"""Synthetic real-Cache tests; all sources, SQLite and files are OS-temp only.

The small harness below exercises the proposed coordinator hook with the actual
Store publication/get_bundle/validation methods; it is NOT a monkeypatched fake
cache nor a claim that an unhooked Store.ingest already does incremental reuse.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from worldanalysis.contracts import ContractError, canonical, hash_file, safe_path
from worldanalysis.extraction_cache import Cache
from worldanalysis.store import Store


def raw_extract(path, outdir, *, elapsed=0, rich=False):
    text = path.read_text(encoding="utf-8")
    raw = {"media_type": "text/plain", "provider_id": "synthetic-cache-native", "provider_version": "1",
           "blocks": [{"kind": "paragraph", "origin": "native_extract",
                       "locator": {"kind": "paragraph", "precision": "exact", "paragraph_index": 0},
                       "text": text, "file_keys": [], "processing_status": "extracted", "reason": "Synthetic actual read"}],
           "files": [], "gaps": [], "metrics": [], "enumeration_state": "complete",
           "processing_status": "verified_complete", "metadata": {"synthetic": True, "elapsed_seconds": elapsed}}
    if rich:
        from PIL import Image
        (outdir / "nested").mkdir()
        Image.new("RGB", (24, 16), (13, 177, 29)).save(outdir / "nested/pixels.png")
        (outdir / "nested/ocr.txt").write_text("Synthetic OCR hypothesis", encoding="utf-8")
        # Same bytes, separate raw key/path: mapping may legally share bytes.
        (outdir / "second-ocr.txt").write_text("Synthetic OCR hypothesis", encoding="utf-8")
        raw["files"] = [
            {"key": "pixels", "relative_path": "nested/pixels.png", "kind": "image", "media_type": "image/png"},
            {"key": "ocr", "relative_path": "nested/ocr.txt", "kind": "native_text", "media_type": "text/plain; charset=utf-8"},
            {"key": "ocr-copy", "relative_path": "second-ocr.txt", "kind": "native_text", "media_type": "text/plain; charset=utf-8"}]
        geometry = {"reference_object_ref": "pixels", "coordinate_space": "original", "unit": "pixel",
                    "origin": "top_left", "frame_size": {"width": 24, "height": 16},
                    "original_size": {"width": 24, "height": 16, "unit": "pixel"},
                    "rotation_degrees": 0, "bbox_xywh": [1, 2, 10, 10],
                    "transform_to_original": {"method": "identity", "operations": []}}
        locator = {"kind": "page", "precision": "exact", "page_number": 1, "geometry": geometry}
        raw["blocks"] += [
            {"kind": "image_occurrence", "origin": "native_extract", "locator": deepcopy(locator),
             "file_keys": ["pixels"], "processing_status": "extracted", "reason": "Synthetic preserved pixels"},
            {"kind": "paragraph", "origin": "ocr", "locator": deepcopy(locator), "text": "Synthetic OCR hypothesis",
             "file_keys": ["ocr", "ocr-copy"], "processing_status": "extracted", "reason": "Synthetic OCR execution"}]
        raw["metadata"]["derivations"] = [{"block_indexes": [2], "source_block_indexes": [1],
            "source_file_keys": ["pixels"], "provider_id": "synthetic-cache-ocr", "provider_version": "ocr-7",
            "uncertainty": "Synthetic recognition hypothesis, not author original",
            "metadata": {"confidence": 0.75, "boxes": [{"bbox": [1, 2, 10, 10], "text": "Synthetic OCR hypothesis"}]}}]
    return raw


class ExtractionCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="worldanalysis-extraction-cache-")
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.store.close())
        self.project = self.store.create_project(topic="Synthetic cache tests")
        self.cache = Cache(self.store)
        self.calls = []
        self.a = self.root / "A.txt"
        self.b = self.root / "B.txt"
        self.a.write_text("Synthetic A first revision", encoding="utf-8")
        self.b.write_text("Synthetic B first revision", encoding="utf-8")
        self.sequence = 0

    def prepare(self, path=None, *, project=None, config="cache-v1", version="1", provider_id="synthetic-cache-native"):
        base = self.store._register_input(project or self.project, path or self.a)
        provider = self.store.register_provider(provider_id, version, config_version=config)
        ref = base["asset"]
        job = self.store.queue_job(project or self.project, "native_extract", provider,
            ref["asset_id"] + ":" + ref["asset_revision"], config_version=config, estimated_cost_minor=0)
        return base, job

    def current(self, job):
        return dict(self.store.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone())

    def extract_entry(self, base, job, *, config="cache-v1", rich=False):
        self.sequence += 1
        out = self.root / ("synthetic-raw-" + str(self.sequence))
        out.mkdir()
        with self.store.transaction():
            self.store.connection.execute("UPDATE jobs SET state='running' WHERE job_id=? AND state!='verified_complete'", (job["job_id"],))
        path = safe_path(self.store.project_root(job["project_id"]), base["original_relative_path"])
        self.calls.append(path.read_text(encoding="utf-8"))
        raw = raw_extract(path, out, elapsed=len(self.calls) * 0.1234567, rich=rich)
        provider = json.loads(job["provider_json"])
        raw["provider_id"], raw["provider_version"] = provider["provider_id"], provider["implementation_version"]
        entry = {**base, "raw": raw, "output_dir": out, "job_id": job["job_id"], "config_version": config}
        self.store._validate_raw(entry)
        return entry

    def publish_one(self, *, rich=False, remember=True, path=None):
        base, job = self.prepare(path)
        entry = self.extract_entry(base, job, rich=rich)
        bundle = self.store.publish(self.project, [entry])
        if remember:
            self.assertTrue(self.cache.remember(entry, bundle))
        return base, self.current(job), entry, bundle

    def ingest_via_real_helper(self, paths, *, config="cache-v1", version="1"):
        entries = []
        for path in paths:
            base, job = self.prepare(path, config=config, version=version)
            restored = self.cache.restore(base, job, config)
            entries.append(restored if restored is not None else self.extract_entry(base, job, config=config))
        bundle = self.store.publish(self.project, entries)
        for entry in entries:
            self.cache.remember(entry, bundle)
        return bundle

    def payload(self, job):
        row = self.store.connection.execute("SELECT * FROM extraction_cache WHERE job_id=?", (job["job_id"],)).fetchone()
        return dict(row), json.loads(row["payload_json"])

    def corrupt_payload(self, job, mutate, *, reseal=False):
        row, payload = self.payload(job)
        mutate(payload)
        text = canonical(payload)
        with self.store.transaction():
            self.store.connection.execute("UPDATE extraction_cache SET payload_json=?,payload_sha256=? WHERE job_id=?",
                (text.decode(), hashlib.sha256(text).hexdigest() if reseal else row["payload_sha256"], job["job_id"]))

    def tamper_file(self, path, data):
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
        path.write_bytes(data)

    def test_helper_incremental_add_change_order_and_elapsed_not_a_key(self):
        first = self.ingest_via_real_helper([self.a])
        self.assertEqual(len(self.calls), 1)
        mixed = self.ingest_via_real_helper([self.a, self.b])
        self.assertEqual(self.calls, ["Synthetic A first revision", "Synthetic B first revision"])
        reverse = self.ingest_via_real_helper([self.b, self.a])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(mixed["bundle_id"], reverse["bundle_id"])
        self.b.write_text("Synthetic B second revision", encoding="utf-8")
        changed = self.ingest_via_real_helper([self.a, self.b])
        self.assertEqual(self.calls[-1], "Synthetic B second revision")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(first["assets"][0]["asset_revision"], next(a for a in changed["assets"] if a["asset_id"] == first["assets"][0]["asset_id"])["asset_revision"])
        row = self.store.connection.execute("SELECT payload_json FROM extraction_cache ORDER BY created_at LIMIT 1").fetchone()
        self.assertEqual(json.loads(row[0])["raw"]["metadata"]["elapsed_seconds"], 0.1234567)

    def test_new_provider_config_and_original_revision_have_distinct_jobs(self):
        self.ingest_via_real_helper([self.a])
        self.ingest_via_real_helper([self.a], config="cache-v2")
        self.ingest_via_real_helper([self.a], config="cache-v2", version="2")
        self.a.write_text("Synthetic A revised", encoding="utf-8")
        self.ingest_via_real_helper([self.a], config="cache-v2", version="2")
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM extraction_cache").fetchone()[0], 4)

    def test_exact_raw_files_geometry_ocr_lineage_survive_new_publication(self):
        base, job, entry, bundle = self.publish_one(rich=True)
        expected = deepcopy(entry["raw"])
        shutil.rmtree(entry["output_dir"])
        restored = self.cache.restore(base, job, "cache-v1")
        self.assertEqual(restored["raw"], expected)
        self.assertNotEqual(restored["output_dir"], entry["output_dir"])
        self.assertEqual({p.relative_to(restored["output_dir"]).as_posix() for p in restored["output_dir"].rglob("*") if p.is_file()}, {f["relative_path"] for f in expected["files"]})
        self.assertFalse((restored["output_dir"] / "manifest.json").exists())
        self.assertEqual(restored["raw"]["blocks"][2]["locator"]["geometry"]["reference_object_ref"], "pixels")
        self.store._validate_raw(restored)
        other, otherjob = self.prepare(self.b)
        second = self.store.publish(self.project, [restored, self.extract_entry(other, otherjob)])
        self.cache.remember(restored, second)
        ocr = next(b for b in second["blocks"] if b["origin"] == "ocr")
        self.assertEqual(ocr["producer"]["provider_id"], "synthetic-cache-ocr")
        self.assertEqual(ocr["producer"]["implementation_version"], "ocr-7")
        self.assertEqual(len(ocr["derived_from"]), 2)
        self.assertEqual(len(self.calls), 2)
        # Job receipt now names the second Bundle; the original cache binding is
        # still verified and reusable, not blindly tied to a mutable receipt.
        again = self.cache.restore(base, self.current(job), "cache-v1")
        self.assertEqual(again["raw"], expected)
        self.assertNotEqual(again["output_dir"], restored["output_dir"])
        row, payload = self.payload(job)
        self.assertEqual(row["bundle_id"], bundle["bundle_id"])
        self.assertEqual([f["key"] for f in payload["files"]], [f["key"] for f in expected["files"]])

    def test_full_native_text_not_reconstructed_from_visible_preview(self):
        full = "Synthetic long source " * 1500
        self.a.write_text(full, encoding="utf-8")
        base, job, entry, bundle = self.publish_one()
        self.assertEqual(len(bundle["blocks"][0]["text"]), 16000)
        restored = self.cache.restore(base, job, "cache-v1")
        self.assertEqual(restored["raw"]["blocks"][0]["text"], full)
        self.assertGreater(len(restored["raw"]["blocks"][0]["text"]), 16000)

    def test_credential_shaped_raw_only_in_restricted_local_cache(self):
        full = "Synthetic instruction\n密码：fake-local-only-password\nSynthetic followup"
        self.a.write_text(full, encoding="utf-8")
        base, job, entry, bundle = self.publish_one()
        self.assertNotIn("fake-local-only-password", canonical(bundle).decode())
        row, payload = self.payload(job)
        self.assertEqual(row["access_class"], "restricted_local")
        self.assertIn("fake-local-only-password", payload["raw"]["blocks"][0]["text"])
        restored = self.cache.restore(base, job, "cache-v1")
        self.assertEqual(restored["raw"]["blocks"][0]["text"], full)
        events = [r[0] for r in self.store.connection.execute("SELECT payload_json FROM events")]
        self.assertNotIn("fake-local-only-password", "".join(events))

    def test_no_second_database_and_ddl_insert_use_store_transaction(self):
        with patch.object(self.store, "transaction", wraps=self.store.transaction) as transactions:
            cache = Cache(self.store)
            base, job, entry, bundle = self.publish_one(remember=False)
            self.assertTrue(cache.remember(entry, bundle))
            self.assertGreaterEqual(transactions.call_count, 2)
        self.assertEqual([r[1] for r in self.store.connection.execute("PRAGMA database_list")], ["main"])
        self.assertEqual(list(self.root.rglob("*.sqlite3")), [self.store.db_path])
        self.assertFalse(self.store.connection.in_transaction)

    def test_outer_coordinator_rollback_also_rolls_back_cache_insert(self):
        base, job, entry, bundle = self.publish_one(remember=False)
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store.transaction():
                self.cache.remember(entry, bundle)
                raise RuntimeError("rollback")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM extraction_cache").fetchone()[0], 0)

    def test_cache_survives_store_restart(self):
        base, job, entry, bundle = self.publish_one(rich=True)
        self.store.close()
        self.store = Store(self.root)
        self.cache = Cache(self.store)
        restored = self.cache.restore(base, self.current(job), "cache-v1")
        self.assertEqual(restored["raw"], entry["raw"])

    def test_legacy_published_without_raw_has_explicit_miss_then_one_reparse(self):
        base, job, entry, bundle = self.publish_one(remember=False)
        self.assertIsNone(self.cache.restore(base, job, "cache-v1"))
        reparsed = self.extract_entry(base, job)
        next_bundle = self.store.publish(self.project, [reparsed])
        self.cache.remember(reparsed, next_bundle)
        self.assertIsNotNone(self.cache.restore(base, self.current(job), "cache-v1"))
        self.assertEqual(len(self.calls), 2)

    def test_unpublished_running_entry_cannot_be_remembered(self):
        base, job = self.prepare()
        entry = self.extract_entry(base, job)
        with self.assertRaises(ContractError):
            self.cache.remember(entry, {"project_id": self.project, "bundle_id": "not-published", "publish_id": "not-published"})
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM extraction_cache").fetchone()[0], 0)

    def test_caller_fake_bundle_cannot_be_remembered(self):
        base, job, entry, bundle = self.publish_one(remember=False)
        bundle = deepcopy(bundle)
        bundle["blocks"][0]["text"] = "Forged visible text"
        with self.assertRaises(ContractError):
            self.cache.remember(entry, bundle)

    def test_raw_metadata_text_locator_and_file_keys_must_match_published(self):
        base, job, entry, bundle = self.publish_one(rich=True, remember=False)
        mutations = [lambda r: r["metadata"].update(forged=True),
                     lambda r: r["blocks"][0].update(text="forged"),
                     lambda r: r["blocks"][0]["locator"].update(paragraph_index=99),
                     lambda r: r["blocks"][2].update(file_keys=[])]
        for mutate in mutations:
            bad = {**entry, "raw": deepcopy(entry["raw"])}
            mutate(bad["raw"])
            with self.subTest(mutate=mutate), self.assertRaises(ContractError):
                self.cache.remember(bad, bundle)

    def test_raw_escape_absolute_windows_and_backslash_paths_fail_before_open(self):
        base, job, entry, bundle = self.publish_one(rich=True, remember=False)
        for relative in ("../escape", "/absolute", "C:/absolute", "nested\\escape", "nested/../escape", "nested//escape"):
            bad = {**entry, "raw": deepcopy(entry["raw"])}
            bad["raw"]["files"][0]["relative_path"] = relative
            with self.subTest(path=relative), self.assertRaises(ContractError):
                self.cache.remember(bad, bundle)

    def test_entire_evidence_directory_is_not_raw(self):
        base, job, entry, bundle = self.publish_one(rich=True, remember=False)
        evidence = (self.store.project_root(self.project) / bundle["publication"]["manifest_relative_path"]).parent
        bad = {**entry, "output_dir": evidence}
        with self.assertRaises((ContractError, FileNotFoundError)):
            self.cache.remember(bad, bundle)

    def test_wrong_project_original_revision_provider_config_and_supplied_job_fail(self):
        base, job, entry, bundle = self.publish_one()
        other = self.store.create_project(topic="Synthetic foreign")
        wrong_base, _ = self.prepare(project=other)
        wrong_revision = deepcopy(base)
        wrong_revision["asset"]["asset_revision"] = "asset-wrong"
        wrong_original = deepcopy(base)
        wrong_original["original_sha256"] = "0" * 64
        wrong_job = dict(job, provider_json=canonical({**json.loads(job["provider_json"]), "provider_id": "forged"}).decode())
        for given, supplied, config in ((wrong_base, job, "cache-v1"), (wrong_revision, job, "cache-v1"),
                (wrong_original, job, "cache-v1"), (base, wrong_job, "cache-v1"), (base, job, "cache-v2")):
            with self.subTest(config=config, given=given["asset"]["asset_id"]), self.assertRaises(ContractError):
                self.cache.restore(given, supplied, config)

    def test_wrong_cached_scope_is_not_silently_treated_as_miss(self):
        base, job, entry, bundle = self.publish_one()
        with self.store.transaction():
            self.store.connection.execute("UPDATE extraction_cache SET config_version='wrong-config' WHERE job_id=?", (job["job_id"],))
        with self.assertRaises(ContractError):
            self.cache.restore(base, job, "cache-v1")

    def test_job_state_change_prevents_restore(self):
        base, job, entry, bundle = self.publish_one()
        for state in ("running", "queued", "failed", "submission_unknown"):
            with self.subTest(state=state):
                with self.store.transaction():
                    self.store.connection.execute("UPDATE jobs SET state=? WHERE job_id=?", (state, job["job_id"]))
                with self.assertRaises(ContractError):
                    self.cache.restore(base, self.current(job), "cache-v1")

    def test_corrupt_payload_json_digest_and_binding_fail_without_fallback(self):
        base, job, entry, bundle = self.publish_one(rich=True)
        row, original = self.payload(job)
        for text, checksum in (("{", row["payload_sha256"]), (row["payload_json"], "0" * 64)):
            with self.subTest(text=text[:10]):
                with self.store.transaction():
                    self.store.connection.execute("UPDATE extraction_cache SET payload_json=?,payload_sha256=? WHERE job_id=?", (text, checksum, job["job_id"]))
                with self.assertRaises(ContractError):
                    self.cache.restore(base, job, "cache-v1")
        with self.store.transaction():
            self.store.connection.execute("UPDATE extraction_cache SET payload_json=?,payload_sha256=? WHERE job_id=?", (row["payload_json"], row["payload_sha256"], job["job_id"]))
        self.corrupt_payload(job, lambda p: p["files"][0].update(artifact_relative_path="../escape"), reseal=True)
        with self.assertRaises(ContractError):
            self.cache.restore(base, job, "cache-v1")
        self.assertEqual(len(self.calls), 1)

    def test_resealed_forged_raw_text_still_fails_published_projection(self):
        base, job, entry, bundle = self.publish_one()
        self.corrupt_payload(job, lambda p: p["raw"]["blocks"][0].update(text="Forged after cache write"), reseal=True)
        with self.assertRaises(ContractError):
            self.cache.restore(base, job, "cache-v1")

    def test_tampered_manifest_file_extra_file_and_legacy_receipt_fail(self):
        # Independent isolated temporary ledgers for every tamper mode.
        for mode in ("manifest", "file", "extra", "legacy"):
            with self.subTest(mode=mode):
                base, job, entry, bundle = self.publish_one(rich=True, remember=mode != "legacy")
                root = self.store.project_root(self.project)
                manifest = root / bundle["publication"]["manifest_relative_path"]
                if mode in {"manifest", "legacy"}:
                    self.tamper_file(manifest, b"{}")
                elif mode == "file":
                    _, payload = self.payload(job)
                    self.tamper_file(root / payload["files"][0]["artifact_relative_path"], b"changed")
                else:
                    (manifest.parent / "unmanifested.txt").write_text("extra", encoding="utf-8")
                with self.assertRaises(ContractError):
                    self.cache.restore(base, job, "cache-v1")
                # Do not reuse the corrupted publication for the next mode.
                self.project = self.store.create_project(topic="Synthetic separate tamper scope")

    def test_cache_does_not_silently_repair_or_overwrite_damage_on_remember(self):
        base, job, entry, bundle = self.publish_one()
        self.corrupt_payload(job, lambda p: p["raw"]["metadata"].update(corrupted=True))
        with self.assertRaises(ContractError):
            self.cache.remember(entry, bundle)
        _, payload = self.payload(job)
        self.assertTrue(payload["raw"]["metadata"]["corrupted"])

    def test_raw_output_and_cache_json_have_hard_budgets(self):
        base, job, entry, bundle = self.publish_one(rich=True, remember=False)
        with self.assertRaises(ContractError):
            Cache(self.store, max_output_bytes=1).remember(entry, bundle)
        with self.assertRaises(ContractError):
            Cache(self.store, max_json_bytes=100).remember(entry, bundle)
        with self.assertRaises(ContractError):
            Cache(self.store, max_cache_bytes=100).remember(entry, bundle)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM extraction_cache").fetchone()[0], 0)
        self.cache.remember(entry, bundle)
        with self.assertRaises(ContractError):
            Cache(self.store, max_json_bytes=100).restore(base, job, "cache-v1")
        with self.assertRaises(ContractError):
            Cache(self.store, max_output_bytes=1).restore(base, job, "cache-v1")

    def test_raw_cyclic_nonfinite_and_deep_json_fail(self):
        base, job, entry, bundle = self.publish_one(remember=False)
        cycle = []
        cycle.append(cycle)
        deep = []
        for _ in range(70):
            deep = [deep]
        for value in (cycle, deep, float("nan"), {"not-json"}):
            bad = {**entry, "raw": deepcopy(entry["raw"])}
            bad["raw"]["metadata"]["invalid"] = value
            with self.subTest(kind=type(value).__name__), self.assertRaises(ContractError):
                self.cache.remember(bad, bundle)

    def test_duplicate_raw_keys_paths_and_missing_derivation_keys_fail(self):
        base, job, entry, bundle = self.publish_one(rich=True, remember=False)
        for mutate in (lambda r: r["files"][1].update(key="pixels"),
                       lambda r: r["files"][1].update(relative_path="NESTED/pixels.png"),
                       lambda r: r["metadata"]["derivations"][0].update(source_file_keys=["missing"])):
            bad = {**entry, "raw": deepcopy(entry["raw"])}
            mutate(bad["raw"])
            with self.assertRaises(ContractError):
                self.cache.remember(bad, bundle)

    def test_media_markers_are_not_applicable_without_reading_media(self):
        base, job = self.prepare()
        marked = [dict(base, original_kind="inventory"), dict(base, media_registration=True),
                  {**deepcopy(base), "asset": {**base["asset"], "media_type": "video/mp4"}}]
        for entry in marked:
            with self.subTest(marker=entry.keys()), patch("worldanalysis.extraction_cache.hash_file", side_effect=AssertionError("media must not be hashed")):
                self.assertIsNone(self.cache.restore(entry, job, "cache-v1"))
                self.assertFalse(self.cache.remember(entry, {}))

    def test_magic_identified_image_with_unknown_extension_roundtrips(self):
        from PIL import Image
        from worldanalysis.documents import extract, PROVIDER_ID, PROVIDER_VERSION
        path = self.root / "synthetic-magic.unknownext"
        Image.new("RGB", (24, 16), (13, 177, 29)).save(path, format="PNG")
        base = self.store._register_input(self.project, path)
        self.assertEqual(base["asset"]["media_type"], "application/octet-stream")
        provider = self.store.register_provider(PROVIDER_ID, PROVIDER_VERSION, config_version="cache-v1")
        ref = base["asset"]
        job = self.store.queue_job(self.project, "native_extract", provider,
            ref["asset_id"] + ":" + ref["asset_revision"], config_version="cache-v1", estimated_cost_minor=0)
        with self.store.transaction():
            self.store.connection.execute("UPDATE jobs SET state='running' WHERE job_id=?", (job["job_id"],))
        out = self.root / "synthetic-magic-raw"
        out.mkdir()
        raw = extract(safe_path(self.store.project_root(self.project), base["original_relative_path"]), out, options={"synthetic": True})
        self.assertEqual(raw["media_type"], "image/png")
        entry = {**base, "raw": raw, "output_dir": out, "job_id": job["job_id"], "config_version": "cache-v1"}
        bundle = self.store.publish(self.project, [entry])
        original = next(a for a in bundle["artifacts"] if a["kind"] == "original_small_file")
        self.assertEqual(original["media_type"], "image/png")
        self.assertTrue(self.cache.remember(entry, bundle))
        restored = self.cache.restore(base, self.current(job), "cache-v1")
        self.assertEqual(restored["raw"], raw)
        self.store._validate_raw(restored)
        registered_type = self.store.connection.execute("SELECT media_type FROM asset_revisions WHERE asset_id=? AND asset_revision=?", (ref["asset_id"], ref["asset_revision"])).fetchone()[0]
        self.assertEqual(registered_type, "application/octet-stream")

    def test_reparse_point_flags_reject_raw_and_restore_destination_without_os_privilege(self):
        from types import SimpleNamespace
        base, job, entry, bundle = self.publish_one(rich=True, remember=False)
        lstat = Path.lstat
        flagged = entry["output_dir"]
        def reparse(path, *args, **kwargs):
            observed = lstat(path, *args, **kwargs)
            if path == flagged:
                return SimpleNamespace(st_mode=observed.st_mode, st_file_attributes=0x400)
            return observed
        with patch.object(Path, "lstat", reparse), self.assertRaises(ContractError):
            self.cache.remember(entry, bundle)
        self.cache.remember(entry, bundle)
        flagged = self.root / "下载暂存"
        flagged.mkdir(exist_ok=True)
        with patch.object(Path, "lstat", reparse), self.assertRaises(ContractError):
            self.cache.restore(base, job, "cache-v1")

    def test_media_registry_alone_is_not_applicable_without_byte_reads(self):
        from worldanalysis.media_store import MediaRegistration
        media = self.root / "synthetic-registration-only.mp4"
        media.write_bytes(b"synthetic descriptor test, not decodable media")
        entry = MediaRegistration(self.store).register(self.project, media)
        entry.pop("original_kind")
        entry.pop("original_media_type")
        entry.pop("media_registration")
        entry["asset"]["media_type"] = "application/octet-stream"
        with patch("worldanalysis.extraction_cache.hash_file", side_effect=AssertionError("registry media must not be hashed")):
            self.assertIsNone(self.cache.restore(entry, {}, "cache-v1"))
            self.assertFalse(self.cache.remember(entry, {}))

    def test_copy_time_artifact_tamper_fails_instead_of_returning_partial_raw(self):
        base, job, entry, bundle = self.publish_one(rich=True)
        check = self.cache._cached
        def mutate_after_checks(*args):
            payload = check(*args)
            path = self.store.project_root(self.project) / payload["files"][0]["artifact_relative_path"]
            self.tamper_file(path, b"changed during bounded restore")
            return payload
        with patch.object(self.cache, "_cached", side_effect=mutate_after_checks), self.assertRaises(ContractError):
            self.cache.restore(base, job, "cache-v1")
        self.assertEqual(len(self.calls), 1)

    def test_total_cache_budget_is_enforced_on_reads_too(self):
        base, job, entry, bundle = self.publish_one()
        with self.assertRaises(ContractError):
            Cache(self.store, max_cache_bytes=100).restore(base, job, "cache-v1")

    def test_raw_symlink_and_published_symlink_are_rejected_when_available(self):
        base, job, entry, bundle = self.publish_one(rich=True, remember=False)
        path = entry["output_dir"] / "nested/ocr.txt"
        data = path.read_bytes()
        path.unlink()
        target = self.root / "synthetic-link-target.txt"
        target.write_bytes(data)
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest("OS does not grant symlink creation to this test process")
        with self.assertRaises(ContractError):
            self.cache.remember(entry, bundle)
        path.unlink()
        path.write_bytes(data)
        self.cache.remember(entry, bundle)
        _, payload = self.payload(job)
        artifact = self.store.project_root(self.project) / payload["files"][1]["artifact_relative_path"]
        artifact.chmod(stat.S_IREAD | stat.S_IWRITE)
        artifact.unlink()
        artifact.symlink_to(target)
        with self.assertRaises(ContractError):
            self.cache.restore(base, job, "cache-v1")


if __name__ == "__main__":
    unittest.main()
