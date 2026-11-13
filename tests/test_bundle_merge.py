"""Temporary evidence only; real bounded WAV decode, never real data or ASR."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from worldanalysis.bundle_merge import compose
from worldanalysis import bundle_merge
from worldanalysis.contracts import ContractError, asset_key, canonical, hash_file, validate_plan
from worldanalysis.store import Store


def cleanup(path):
    def writable(func, name, exc):
        Path(name).chmod(stat.S_IWRITE)
        func(name)
    shutil.rmtree(path, onerror=writable)


class TextExtractor:
    provider_id = "synthetic-compose-text"
    provider_version = "test-1"

    def __init__(self, statuses=None):
        self.calls = 0
        self.statuses = statuses or ["extracted"]

    def __call__(self, source, output_dir, options=None):
        self.calls += 1
        text = source.read_text(encoding="utf-8")
        blocks = [{"kind": "paragraph", "origin": "native_extract",
            "locator": {"kind": "paragraph", "precision": "exact", "paragraph_index": index},
            "text": text if index == 0 else "Synthetic retained state " + status,
            "file_keys": [], "processing_status": status, "reason": "Actual bounded text read"}
            for index, status in enumerate(self.statuses)]
        partial = any(s != "extracted" for s in self.statuses)
        return {"media_type": "text/plain", "provider_id": self.provider_id,
            "provider_version": self.provider_version, "blocks": blocks, "files": [],
            "gaps": [{"kind": "unprocessed", "severity": "important", "reason": "Synthetic unresolved work", "block_indexes": list(range(1, len(blocks)))}] if partial else [],
            "metrics": [], "enumeration_state": "complete", "processing_status": "partial" if partial else "verified_complete",
            "metadata": {"synthetic": True, "actual_format": "UTF-8 text"}}


class BundleMergeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wa-compose-"))
        self.addCleanup(cleanup, self.root)
        self.store = Store(self.root)
        self.addCleanup(lambda: self.store.close())
        self.project = self.store.create_project(project_id="compose-project", topic="Synthetic composition")
        self.extractor = TextExtractor()

    def native(self, name="one.txt", text="Actual first instruction.", *, extractor=None, config="compose-test-v1", project=None):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return self.store.ingest(project or self.project, [path], extractor or self.extractor, config_version=config)

    def pair(self):
        return self.native(), self.native("two.txt", "Second actual instruction.")

    def ids(self, *bundles):
        return [b["bundle_id"] for b in bundles]

    def frozen_tables(self):
        names = ("jobs", "providers", "provider_configurations", "extraction_cache", "media_sources", "media_executions")
        return {name: [tuple(r) for r in self.store.connection.execute("SELECT * FROM " + name + " ORDER BY 1,2")]
                for name in names}

    def files(self, bundles):
        root = self.store.project_root(self.project)
        paths = {a["relative_path"] for b in bundles for a in b["artifacts"]}
        paths.update(b["publication"]["manifest_relative_path"] for b in bundles)
        return {p: hash_file(root / p) for p in paths}

    def plan(self, bundle):
        rel = bundle["publication"]["manifest_relative_path"].rsplit("/", 1)[0] + "/plan.json"
        return json.loads((self.store.project_root(self.project) / rel).read_bytes())

    def reseal_plan(self, bundle, mutate):
        """Synthetic internally hashed journal, to exercise missing scope guards."""
        bundle = deepcopy(bundle)
        plan = self.plan(bundle)
        mutate(plan)
        root = self.store.project_root(self.project)
        plan_art = next(a for a in bundle["artifacts"] if a["relative_path"].endswith("/plan.json"))
        path = root / plan_art["relative_path"]
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
        path.write_bytes(canonical(plan))
        sha, size = hash_file(path)
        plan_art.update(sha256=sha, byte_length=size, artifact_revision="sha256-" + sha)
        manifest = root / bundle["publication"]["manifest_relative_path"]
        manifest.chmod(stat.S_IREAD | stat.S_IWRITE)
        manifest.write_bytes(canonical(bundle))
        row = self.store.connection.execute("SELECT expected_files_json FROM publications WHERE publish_id=?", (bundle["publish_id"],)).fetchone()
        expected = json.loads(row[0])
        expected[plan_art["relative_path"]] = {"sha256": sha, "byte_length": size}
        expected[bundle["publication"]["manifest_relative_path"]] = dict(zip(("sha256", "byte_length"), hash_file(manifest)))
        with self.store.transaction():
            self.store.connection.execute("UPDATE plans SET plan_json=? WHERE plan_id=?", (canonical(plan).decode(), plan["plan_id"]))
            self.store.connection.execute("UPDATE publications SET bundle_json=?,manifest_sha256=?,expected_files_json=? WHERE publish_id=?", (canonical(bundle).decode(), hash_file(manifest)[0], canonical(expected).decode(), bundle["publish_id"]))
        return bundle

    def reseal_bundle(self, bundle):
        root = self.store.project_root(self.project)
        relative = bundle["publication"]["manifest_relative_path"]
        row = self.store.connection.execute("SELECT expected_files_json FROM publications WHERE publish_id=?", (bundle["publish_id"],)).fetchone()
        expected = json.loads(row[0])
        index = next(a for a in bundle["artifacts"] if a["relative_path"] == relative.rsplit("/", 1)[0] + "/blocks.jsonl")
        path = root / index["relative_path"]
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
        path.write_bytes(b"".join(canonical(b) + b"\n" for b in bundle["blocks"]))
        sha, size = hash_file(path)
        index.update(sha256=sha, byte_length=size, artifact_revision="sha256-" + sha)
        expected[index["relative_path"]] = {"sha256": sha, "byte_length": size}
        manifest = root / relative
        manifest.chmod(stat.S_IREAD | stat.S_IWRITE)
        manifest.write_bytes(canonical(bundle))
        expected[relative] = dict(zip(("sha256", "byte_length"), hash_file(manifest)))
        self.store.connection.execute("UPDATE publications SET bundle_json=?,manifest_sha256=?,expected_files_json=? WHERE publish_id=?", (canonical(bundle).decode(), hash_file(manifest)[0], canonical(expected).decode(), bundle["publish_id"]))

    def test_native_union_retains_exact_evidence_and_historical_plan_facts(self):
        first, second = self.pair()
        frozen, before = self.frozen_tables(), self.files([first, second])
        result = compose(self.store, self.project, self.ids(first, second))
        self.assertEqual(len(result["assets"]), 2)
        self.assertEqual(result["coverage"]["claimed_status"], "verified_complete")
        self.assertEqual(result["coverage"]["scope"], "listed_assets")
        self.assertEqual({b["block_id"]: b for b in result["blocks"]}, {b["block_id"]: b for s in (first, second) for b in s["blocks"]})
        actual = {a["artifact_id"]: a for a in result["artifacts"]}
        for source in (first, second):
            for artifact in source["artifacts"]:
                self.assertEqual(actual[artifact["artifact_id"]], artifact)
        plan = self.plan(result)
        validate_plan(plan)
        original_plans = [self.plan(b) for b in (first, second)]
        for field, key in (("asset_plans", lambda p: asset_key(p["asset_ref"])), ("capability_observations", lambda p: p["observation_id"]), ("permission_checks", lambda p: p["check_id"])):
            self.assertEqual({key(p): p for p in plan[field]}, {key(p): p for s in original_plans for p in s[field]})
        self.assertEqual(self.frozen_tables(), frozen)
        self.assertEqual(self.files([first, second]), before)
        base = self.store.project_root(self.project) / result["publication"]["manifest_relative_path"]
        self.assertEqual({p.name for p in base.parent.iterdir()}, {"manifest.json", "plan.json", "blocks.jsonl", "validation.json"})
        self.assertEqual([json.loads(line) for line in base.with_name("blocks.jsonl").read_bytes().splitlines()], result["blocks"])
        report = json.loads(base.with_name("validation.json").read_bytes())
        self.assertFalse(report["new_extraction_executed"])
        self.assertFalse(report["historical_plan_is_new_authorization"])
        self.assertEqual(len(report["source_publications"]), 2)

    def test_reverse_duplicate_input_is_idempotent_after_restart(self):
        first, second = self.pair()
        result = compose(self.store, self.project, self.ids(first, second))
        again = compose(self.store, self.project, self.ids(second, first, first))
        self.assertEqual(result, again)
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(compose(self.store, self.project, self.ids(second, first)), result)
        self.assertEqual(len(self.store.list_bundles()), 3)
        self.assertEqual(self.extractor.calls, 2)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='bundle_published' AND record_id=?", (result["publish_id"],)).fetchone()[0], 1)

    def test_partial_states_unclosed_gaps_contacts_and_full_text_survive(self):
        statuses = ["extracted", "deferred", "failed", "excluded", "not_applicable", "discovered"]
        extractor = TextExtractor(statuses)
        first = self.native(text="Long source. " * 1600 + " help@example.test", extractor=extractor)
        second = self.native("two.txt", "Independent source.")
        before = self.files([first, second])
        result = compose(self.store, self.project, self.ids(first, second))
        self.assertEqual(result["coverage"]["claimed_status"], "partial")
        self.assertEqual(sorted(result["gaps"], key=lambda g: g["gap_id"]), sorted(first["gaps"], key=lambda g: g["gap_id"]))
        self.assertEqual(result["contacts"], first["contacts"])
        self.assertTrue(any(c["disposition"] == "restricted_local" for c in result["contacts"]))
        own = [b for b in result["blocks"] if asset_key(b["asset_ref"]) == asset_key(first["assets"][0])]
        self.assertEqual(own, first["blocks"])
        self.assertEqual([b["processing"]["status"] for b in own], statuses)
        art = next(a for a in result["artifacts"] if a["artifact_id"] in own[0]["artifact_ids"])
        self.assertGreater(art["byte_length"], 16000)
        self.assertEqual(self.files([first, second]), before)

    def test_actual_wav_decode_publication_and_native_composition_without_reexecution(self):
        native = self.native()
        wav = self.root / "actual-synthetic.wav"
        with wave.open(str(wav), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"\x00\x00" * 8000)
        media = self.store.ingest_media(self.project, [wav], options={"asr_enabled": False, "ocr_enabled": False, "synthetic": True}, config_version="actual-compose-decoder-v1")
        packet = json.loads(self.store.connection.execute("SELECT raw_json FROM media_executions").fetchone()[0])
        self.assertGreater(packet["metadata"]["input_bytes_received"], 0)
        self.assertFalse(packet["metadata"]["full_input_hash_performed"])
        self.assertTrue(media["blocks"])
        self.assertFalse(any(b["origin"] == "asr" for b in media["blocks"]))
        frozen, before = self.frozen_tables(), self.files([native, media])
        # Composition is of old immutable evidence, not a replay of current media.
        wav.unlink()
        with patch("worldanalysis.media.extract_media", side_effect=AssertionError("must not decode again")), patch.object(self.extractor, "__call__", side_effect=AssertionError("must not parse again")), patch("worldanalysis.extraction_cache.Cache.restore", side_effect=AssertionError("must not reconstruct media raw")):
            result = compose(self.store, self.project, self.ids(media, native))
        self.assertEqual(result["coverage"]["claimed_status"], "partial")
        self.assertEqual(self.frozen_tables(), frozen)
        self.assertEqual(self.files([native, media]), before)
        ref = asset_key(media["assets"][0])
        inherited = [a for a in result["artifacts"] if asset_key(a["asset_ref"]) == ref]
        self.assertFalse(any(a["kind"] == "original_small_file" for a in inherited))
        descriptor = next(a for a in inherited if a["relative_path"].endswith(".source.json"))
        self.assertEqual(descriptor["kind"], "inventory")
        self.assertEqual(descriptor["media_type"], "application/json")
        value = json.loads((self.store.project_root(self.project) / descriptor["relative_path"]).read_bytes())
        self.assertIsNone(value["source_content_sha256"])
        self.assertFalse(value["source_copy_retained"])
        self.assertEqual([b for b in result["blocks"] if asset_key(b["asset_ref"]) == ref], media["blocks"])
        self.assertTrue(set(g["gap_id"] for g in media["gaps"]) <= set(g["gap_id"] for g in result["gaps"]))
        self.assertEqual(compose(self.store, self.project, self.ids(native, media))["bundle_id"], result["bundle_id"])

    def test_unknown_extension_actual_png_publication_composes_by_sealed_format(self):
        from PIL import Image
        from worldanalysis.documents import extract
        native = self.native()
        path = self.root / "actual-pixels.unknownext"
        Image.new("RGB", (24, 16), (13, 177, 29)).save(path, format="PNG")
        build = self.store._build_bundle
        def with_registration_label(*args):
            bundle, plan = build(*args)
            # Compatible historical snapshots may retain the registration's
            # filename guess. Actual original bytes and sealed Plan stay PNG.
            bundle["assets"][0]["media_type"] = "application/octet-stream"
            return bundle, plan
        with patch.object(self.store, "_build_bundle", side_effect=with_registration_label):
            image = self.store.ingest(self.project, [path], extract, options={"synthetic": True}, config_version="actual-magic-compose-v1")
        original = next(a for a in image["artifacts"] if a["kind"] == "original_small_file")
        self.assertEqual(original["media_type"], "image/png")
        self.assertEqual(self.plan(image)["asset_plans"][0]["media_type"], "image/png")
        registered = self.store.connection.execute("SELECT media_type FROM asset_revisions WHERE asset_id=? AND asset_revision=?", asset_key(image["assets"][0])).fetchone()[0]
        self.assertEqual(registered, "application/octet-stream")
        self.store.get_bundle(self.project, image["bundle_id"])
        frozen, before = self.frozen_tables(), self.files([native, image])
        with patch("worldanalysis.documents.extract", side_effect=AssertionError("must not parse image again")):
            result = compose(self.store, self.project, self.ids(native, image))
        self.assertEqual(result["coverage"]["claimed_status"], "partial")
        self.assertTrue(any(a == original for a in result["artifacts"]))
        self.assertEqual([b for b in result["blocks"] if asset_key(b["asset_ref"]) == asset_key(image["assets"][0])], image["blocks"])
        self.assertEqual(self.frozen_tables(), frozen)
        self.assertEqual(self.files([native, image]), before)

    def test_overlapping_composed_bundle_deduplicates_same_producer_job(self):
        first, second = self.pair()
        combined = compose(self.store, self.project, self.ids(first, second))
        result = compose(self.store, self.project, self.ids(first, combined))
        self.assertEqual(len(result["assets"]), 2)
        self.assertEqual(len(result["blocks"]), 2)
        self.assertEqual(self.extractor.calls, 2)
        self.assertEqual(len([a for a in result["artifacts"] if a["kind"] == "original_small_file"]), 2)

    def test_same_job_republication_deduplicates_without_silent_semantic_loss(self):
        first = self.native(text="Actual text and help@example.test")
        asset = first["assets"][0]
        original = next(a for a in first["artifacts"] if a["kind"] == "original_small_file")
        out = self.root / "manual-raw"
        out.mkdir()
        entry = {"asset": asset, "original_relative_path": original["relative_path"], "original_sha256": original["sha256"], "raw": self.extractor(self.root / "one.txt", out), "output_dir": out,
                 "job_id": original["job_id"], "config_version": "compose-test-v1"}
        # Catalog change permits a new wrapper for the same actual producer job.
        extra = self.root / "registered-only.txt"
        extra.write_text("Registered but not part of either selected snapshot.")
        self.store._register_input(self.project, extra)
        republished = self.store.publish(self.project, [entry])
        result = compose(self.store, self.project, self.ids(first, republished))
        self.assertEqual(len(result["assets"]), 1)
        self.assertEqual(len(result["blocks"]), 1)
        self.assertEqual(len(result["contacts"]), 1)

    def test_revision_conflict_requires_caller_selection(self):
        first = self.native()
        changed = self.native(text="Changed revision.")
        with self.assertRaisesRegex(ContractError, "revision/config/producer"):
            compose(self.store, self.project, self.ids(first, changed))
        self.assertEqual(len(self.store.list_bundles()), 2)

    def test_config_conflict_requires_caller_selection(self):
        first = self.native()
        changed = self.native(config="compose-test-v2")
        with self.assertRaisesRegex(ContractError, "revision/config/producer"):
            compose(self.store, self.project, self.ids(first, changed))
        self.assertEqual(len(self.store.list_bundles()), 2)

    def test_foreign_project_and_unpublished_bundle_fail_closed(self):
        first = self.native()
        other = self.store.create_project(project_id="other-project", topic="Not the same project")
        foreign = self.native("foreign.txt", "Foreign evidence.", project=other)
        for ids in (self.ids(first, foreign), ["bundle-unknown"]):
            with self.subTest(ids=ids), self.assertRaises(ContractError):
                compose(self.store, self.project, ids)
        self.store.connection.execute("UPDATE publications SET state='quarantined' WHERE bundle_id=?", (first["bundle_id"],))
        with self.assertRaises(ContractError):
            compose(self.store, self.project, self.ids(first))

    def test_external_directory_not_accepted_as_source_or_bundle_id(self):
        external = Path(tempfile.mkdtemp(prefix="wa-outside-compose-"))
        self.addCleanup(cleanup, external)
        source = external / "outside.txt"
        source.write_text("External immutable copy still lacks workspace permission.")
        bundle = self.store.ingest(self.project, [source], self.extractor, config_version="compose-test-v1")
        with self.assertRaisesRegex(ContractError, "outside authorized"):
            compose(self.store, self.project, self.ids(bundle))
        for ids in ([str(external)], ["../bundle-other"], bundle, "bundle-id", []):
            with self.subTest(ids=ids), self.assertRaises(ContractError):
                compose(self.store, self.project, ids)

    def test_manifest_tamper_is_rejected_before_new_publication(self):
        bundle = self.native()
        manifest = self.store.project_root(self.project) / bundle["publication"]["manifest_relative_path"]
        manifest.chmod(stat.S_IREAD | stat.S_IWRITE)
        data = manifest.read_bytes()
        manifest.write_bytes(data.replace(b"Actual", b"Forged"))
        with self.assertRaises(ContractError):
            compose(self.store, self.project, self.ids(bundle))
        self.assertEqual(len(self.store.list_bundles()), 1)

    def test_missing_or_tampered_inherited_artifact_fails_closed(self):
        first, second = self.pair()
        for bundle, remove in ((first, False), (second, True)):
            with self.subTest(remove=remove):
                artifact = next(a for a in bundle["artifacts"] if a["kind"] == "native_text")
                path = self.store.project_root(self.project) / artifact["relative_path"]
                path.chmod(stat.S_IREAD | stat.S_IWRITE)
                if remove:
                    path.unlink()
                else:
                    path.write_bytes(b"X" * artifact["byte_length"])
                with self.assertRaises(ContractError):
                    compose(self.store, self.project, self.ids(bundle))
        self.assertEqual(len(self.store.list_bundles()), 2)

    def test_symbolic_link_or_reparse_artifact_fails_closed(self):
        bundle = self.native()
        artifact = next(a for a in bundle["artifacts"] if a["kind"] == "native_text")
        path = self.store.project_root(self.project) / artifact["relative_path"]
        original = self.root / "replacement.txt"
        original.write_bytes(path.read_bytes())
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
        path.unlink()
        try:
            os.symlink(original, path)
        except OSError:
            # Windows may deny symlink creation without Developer Mode. Exercise
            # the same fail-closed lstat reparse branch without elevation.
            path.write_bytes(original.read_bytes())
            real_lstat = Path.lstat
            def tagged_lstat(candidate):
                info = real_lstat(candidate)
                if candidate == path:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info
            with patch.object(Path, "lstat", tagged_lstat):
                with self.assertRaisesRegex(ContractError, "links|reparse"):
                    compose(self.store, self.project, self.ids(bundle))
            return
        with self.assertRaisesRegex(ContractError, "links|reparse"):
            compose(self.store, self.project, self.ids(bundle))

    def test_wrong_job_scope_or_provider_is_rejected(self):
        first, second = self.pair()
        job = first["artifacts"][0]["job_id"]
        saved = dict(self.store.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job,)).fetchone())
        other = self.store.create_project(project_id="wrong-job-project", topic="Different scope")
        for field, bad in (("input_revision", "wrong-revision"), ("operation", "ocr"), ("provider_json", "{}"), ("state", "running"), ("project_id", other)):
            with self.subTest(field=field), self.assertRaisesRegex(ContractError, "job"):
                self.store.connection.execute("UPDATE jobs SET " + field + "=? WHERE job_id=?", (bad, job))
                compose(self.store, self.project, self.ids(first, second))
            self.store.connection.execute("UPDATE jobs SET " + field + "=? WHERE job_id=?", (saved[field], job))
        self.assertEqual(len(self.store.list_bundles()), 2)

    def test_registered_provider_configuration_tamper_is_rejected(self):
        first = self.native()
        provider = first["artifacts"][0]["provider"]
        self.store.connection.execute("UPDATE provider_configurations SET provider_json='{}' WHERE provider_id=?", (provider["provider_id"],))
        with self.assertRaisesRegex(ContractError, "producer configuration"):
            compose(self.store, self.project, self.ids(first))

    def test_wrong_capability_scope_despite_hashed_source_plan_is_rejected(self):
        first = self.native()
        self.reseal_plan(first, lambda p: p["capability_observations"][0]["scope"].update(source_ref="different-source"))
        self.store.get_bundle(self.project, first["bundle_id"])
        with self.assertRaisesRegex(ContractError, "capability scope"):
            compose(self.store, self.project, self.ids(first))

    def test_wrong_permission_scope_despite_hashed_source_plan_is_rejected(self):
        first = self.native()
        self.reseal_plan(first, lambda p: p["permission_checks"][0].update(scope_ref="different-asset"))
        self.store.get_bundle(self.project, first["bundle_id"])
        with self.assertRaisesRegex(ContractError, "permission scope"):
            compose(self.store, self.project, self.ids(first))

    def test_cumulative_json_and_manifest_bounds_precede_get_bundle(self):
        first, second = self.pair()
        lengths = [self.store.connection.execute("SELECT length(CAST(bundle_json AS BLOB)) FROM publications WHERE bundle_id=?", (b["bundle_id"],)).fetchone()[0] for b in (first, second)]
        for limit_name, value in (("_MAX_MANIFEST_BYTES", max(lengths)), ("_MAX_SOURCE_JSON", 1)):
            with self.subTest(limit=limit_name), patch.object(bundle_merge, limit_name, value), patch.object(self.store, "get_bundle", side_effect=AssertionError("must admit before materializing JSON")):
                with self.assertRaisesRegex(ContractError, "byte bound"):
                    compose(self.store, self.project, self.ids(first, second))

    def test_entity_and_file_bounds_precede_get_bundle(self):
        first, second = self.pair()
        for limit_name, value in (("_MAX_BLOCKS", 1), ("_MAX_FILES", 1), ("_MAX_FILE_BYTES", 1)):
            with self.subTest(limit=limit_name), patch.object(bundle_merge, limit_name, value), patch.object(self.store, "get_bundle", side_effect=AssertionError("must admit before loading")):
                with self.assertRaisesRegex(ContractError, "bound"):
                    compose(self.store, self.project, self.ids(first, second))

    def test_recover_validated_stage_without_reparse(self):
        first, second = self.pair()
        frozen = self.frozen_tables()
        with patch.object(self.store, "_finish_publication", side_effect=RuntimeError("synthetic pre-rename crash")):
            with self.assertRaisesRegex(RuntimeError, "pre-rename"):
                compose(self.store, self.project, self.ids(first, second))
        row = self.store.connection.execute("SELECT * FROM publications ORDER BY revision DESC LIMIT 1").fetchone()
        self.assertEqual(row["state"], "validated")
        self.assertTrue((self.store.project_root(self.project) / row["staging_relative_path"]).is_dir())
        self.store.close()
        self.store = Store(self.root)
        result = compose(self.store, self.project, self.ids(second, first))
        self.assertEqual(result["publish_id"], row["publish_id"])
        self.assertEqual(self.frozen_tables(), frozen)
        self.assertEqual(self.extractor.calls, 2)

    def test_recover_renamed_publication_keeps_original_job_receipts(self):
        first, second = self.pair()
        frozen = self.frozen_tables()
        with patch.object(self.store, "_after_rename", side_effect=RuntimeError("synthetic post-rename crash")):
            with self.assertRaisesRegex(RuntimeError, "post-rename"):
                compose(self.store, self.project, self.ids(first, second))
        row = self.store.connection.execute("SELECT * FROM publications ORDER BY revision DESC LIMIT 1").fetchone()
        self.assertEqual(row["state"], "validated")
        self.assertTrue((self.store.project_root(self.project) / row["final_relative_path"]).is_dir())
        self.store.close()
        self.store = Store(self.root)
        result = self.store.get_bundle(self.project, row["bundle_id"])
        self.assertEqual(result["publish_id"], row["publish_id"])
        self.assertEqual(self.frozen_tables(), frozen)
        self.assertEqual(self.extractor.calls, 2)
        self.assertEqual(len(self.store.list_bundles()), 3)

    def test_incomplete_staging_is_quarantined_not_reconstructed(self):
        first, second = self.pair()
        with patch("worldanalysis.bundle_merge._build", side_effect=RuntimeError("synthetic incomplete stage")):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                compose(self.store, self.project, self.ids(first, second))
        self.store.recover_publications()
        self.assertEqual(self.store.list_bundles()[-1]["state"], "quarantined")
        with self.assertRaisesRegex(ContractError, "recover/reconcile"):
            compose(self.store, self.project, self.ids(first, second))
        self.assertEqual(self.extractor.calls, 2)

    def test_source_manifest_is_rechecked_when_only_composed_bundle_is_read(self):
        first, second = self.pair()
        result = compose(self.store, self.project, self.ids(first, second))
        path = self.store.project_root(self.project) / first["publication"]["manifest_relative_path"]
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(ContractError):
            self.store.get_bundle(self.project, result["bundle_id"])

    def test_same_job_conflicting_evidence_is_not_silently_deduplicated(self):
        first = self.native()
        original = next(a for a in first["artifacts"] if a["kind"] == "original_small_file")
        out = self.root / "conflicting-raw"
        out.mkdir()
        raw = self.extractor(self.root / "one.txt", out)
        raw["blocks"][0]["text"] = "Different evidence for exactly the same producer job."
        changed = self.store.publish(self.project, [{"asset": first["assets"][0], "original_relative_path": original["relative_path"], "original_sha256": original["sha256"], "raw": raw, "output_dir": out, "job_id": original["job_id"], "config_version": "compose-test-v1"}])
        with self.assertRaisesRegex(ContractError, "same-job asset evidence"):
            compose(self.store, self.project, self.ids(first, changed))
        self.assertEqual(len(self.store.list_bundles()), 2)

    def test_producer_receipt_must_be_same_project_published_evidence(self):
        first = self.native()
        other = self.store.create_project(project_id="receipt-other-project", topic="Different receipt scope")
        foreign = self.native("receipt-other.txt", "Other evidence.", project=other)
        self.store.connection.execute("UPDATE jobs SET receipt_json=? WHERE job_id=?", (canonical({"publish_id": foreign["publish_id"]}).decode(), first["artifacts"][0]["job_id"]))
        with self.assertRaisesRegex(ContractError, "receipt publication scope"):
            compose(self.store, self.project, self.ids(first))
        self.assertEqual(len(self.store.list_bundles()), 2)

    def test_producer_json_bound_precedes_get_bundle(self):
        first = self.native()
        self.store.connection.execute("UPDATE jobs SET receipt_json=? WHERE job_id=?", ("x" * 4097, first["artifacts"][0]["job_id"]))
        with patch.object(self.store, "get_bundle", side_effect=AssertionError("must bound ledger JSON first")):
            with self.assertRaisesRegex(ContractError, "metadata byte bound"):
                compose(self.store, self.project, self.ids(first))

    def test_explicit_source_snapshot_pins_preserve_ids_locators_and_dag(self):
        first, second = self.pair()
        original = next(a for a in first["artifacts"] if a["kind"] == "original_small_file")
        ref = {"project_id": self.project, **first["blocks"][0]["asset_ref"],
               "artifact_id": original["artifact_id"], "bundle_id": first["bundle_id"],
               "bundle_revision": first["bundle_revision"], "locator": {"kind": "asset", "precision": "exact"}}
        first["blocks"][0]["derived_from"] = [deepcopy(ref)]
        self.reseal_bundle(first)
        self.store.get_bundle(self.project, first["bundle_id"])
        before = self.files([first, second])
        result = compose(self.store, self.project, self.ids(first, second))
        copied = next(b for b in result["blocks"] if b["block_id"] == first["blocks"][0]["block_id"])
        expected = dict(ref, bundle_id=result["bundle_id"], bundle_revision=result["bundle_revision"])
        self.assertEqual(copied["derived_from"], [expected])
        self.assertEqual(copied["locator"], first["blocks"][0]["locator"])
        self.assertEqual(self.files([first, second]), before)

    def test_collection_pause_does_not_register_new_publication(self):
        first, second = self.pair()
        self.store.control("collection", "pause")
        result = compose(self.store, self.project, self.ids(first, second))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(self.store.list_bundles()), 2)


if __name__ == "__main__":
    unittest.main()
