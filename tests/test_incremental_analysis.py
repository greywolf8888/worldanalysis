"""Small temporary synthetic P4 checks; never open the real DB/materials."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from worldanalysis.analysis import Analysis
from worldanalysis.bundle_merge import compose
from worldanalysis.contracts import ContractError, canonical, hash_file, now
from worldanalysis.incremental_analysis import prepare, reuse, validate_reused
from worldanalysis import incremental_analysis as incremental
from worldanalysis.store import Store


class SyntheticExtractor:
    provider_id = "synthetic-incremental-extractor"
    provider_version = "test-1"

    def __init__(self, *, image=False, gap=False):
        self.image, self.gap = image, gap

    def __call__(self, path, output, options=None):
        blocks = [{"kind": "paragraph", "origin": "native_extract",
            "locator": {"kind": "paragraph", "precision": "exact", "paragraph_index": 0},
            "text": path.read_bytes().decode("utf-8"), "file_keys": [],
            "processing_status": "extracted", "reason": "Actual synthetic source text"}]
        files = []
        if self.image:
            from PIL import Image
            Image.new("RGB", (3, 2), "red").save(output / "image.png")
            files.append({"key": "image", "relative_path": "image.png", "kind": "image", "media_type": "image/png"})
            blocks.append({"kind": "image_occurrence", "origin": "native_extract",
                "locator": {"kind": "paragraph", "precision": "exact", "paragraph_index": 1},
                "file_keys": ["image"], "processing_status": "extracted", "reason": "Actual synthetic image"})
        return {"media_type": "text/plain", "provider_id": self.provider_id,
            "provider_version": self.provider_version, "blocks": blocks, "files": files,
            "gaps": [{"kind": "semantic_gap", "severity": "important", "reason": "Synthetic open question", "block_indexes": [0]}] if self.gap else [],
            "metrics": [], "enumeration_state": "complete",
            "processing_status": "partial" if self.gap else "verified_complete", "metadata": {"synthetic": True}}


def cleanup(root):
    def writable(func, path, exc):
        Path(path).chmod(stat.S_IREAD | stat.S_IWRITE)
        func(path)
    shutil.rmtree(root, onerror=writable)


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wa-incremental-test-"))
        self.addCleanup(cleanup, self.root)
        self.store = Store(self.root)
        self.addCleanup(lambda: self.store.close())
        self.project = self.store.create_project(project_id="incremental-synthetic", topic="Synthetic P4")
        self.analysis = Analysis(self.store)
        self.extractor = SyntheticExtractor()
        self.old = self.native("old.txt", "Actual alpha instruction.")
        self.old_task = self.claim()
        self.accept(self.old, self.old_task)

    def native(self, name, text, extractor=None):
        path = self.root / name
        path.write_bytes(text.encode("utf-8"))
        return self.store.ingest(self.project, [path], extractor or self.extractor, config_version="synthetic-incremental-v1")

    def claim(self, **kw):
        return self.analysis.claim(self.project, model_version=kw.pop("model_version", "synthetic-model-v1"), **kw)

    def current(self, **kw):
        second = self.native("new.txt", "Actual beta prerequisite.")
        current = compose(self.store, self.project, [self.old["bundle_id"], second["bundle_id"]])
        return current, self.claim(**kw)

    def result(self, bundle, task, states=None):
        processing = []
        states = states or {}
        for block in bundle["blocks"]:
            ident, status = block["block_id"], states.get(block["block_id"], "read")
            refs = []
            if status == "read":
                if block["kind"] == "image_occurrence":
                    from io import BytesIO
                    from PIL import Image
                    def viewer(data, **metadata):
                        with Image.open(BytesIO(data)) as image:
                            image.load()
                            self.assertEqual(image.size, (3, 2))
                        return {"opened": True, "sha256": hashlib.sha256(data).hexdigest(),
                            "viewer_id": "synthetic-real-byte-viewer", "observation": "Actual 3 by 2 red synthetic image"}
                    refs = [self.analysis.open_image(task["task_id"], ident, opener=viewer)["receipt"]["receipt_id"]]
                else:
                    item = self.analysis.read(task["task_id"], [ident], max_chars=1000000)["items"][0]
                    refs = [item["receipt"]["receipt_id"]]
            exclusion = None
            if status == "excluded":
                exclusion = {"reason": "Synthetic explicit exclusion", "basis_refs": [self.ref(bundle, block)],
                    "recovery_ref": ident, "restore_action": "requeue_block"}
            processing.append({"block_id": ident, "asset_ref": deepcopy(block["asset_ref"]), "status": status,
                "priority": "normal", "reason": "Actual synthetic read" if status == "read" else "Explicit unread limitation",
                "read_receipt_refs": refs, "exclusion": exclusion})
        counts = Counter(p["status"] for p in processing)
        report_rel = task["output_relative_path"] + "/report.md"
        root = self.store.project_root(self.project)
        (root / report_rel).write_text("# Synthetic verified analysis\nActual source instructions are cited.\n", encoding="utf-8")
        status = "partial" if counts["pending"] or counts["failed"] else "completed"
        assertions = [{"assertion_id": "assertion-" + str(index), "kind": "extracted_fact",
            "statement": "Synthetic source block contains an actual instruction.",
            "evidence_refs": [self.ref(bundle, block)], "external_source_ids": [],
            "depends_on_assertion_ids": [], "verification_status": "supported", "uncertainty": "Synthetic contract evidence only"}
            for index, block in enumerate(bundle["blocks"]) if states.get(block["block_id"], "read") == "read"]
        return {"schema_version": "1.0-draft", "analysis_id": "result-" + task["task_id"],
            "analysis_revision": "v1", "project_id": self.project,
            "based_bundle_id": bundle["bundle_id"], "based_bundle_revision": bundle["bundle_revision"],
            "based_publish_id": bundle["publish_id"], "created_at": now(), "execution_status": status,
            "agent": deepcopy(task["agent"]), "block_processing": processing,
            "coverage": {"scope": "based_bundle_only", "source_inventory_state": bundle["coverage"]["inventory_state"],
                "discovered_blocks": len(processing), **{s + "_blocks": counts[s] for s in ("read", "failed", "pending", "excluded", "not_applicable")},
                "claim": "all_discovered_read" if counts["read"] == len(processing) else "partial",
                "limitations": ["Synthetic old limitation must survive incremental reuse."]},
            "assertions": assertions, "external_sources": [], "gaps": deepcopy(bundle["gaps"]), "supplement_requests": [],
            "main_report_relative_path": report_rel,
            "reports": [{"report_id": "report-main", "kind": "combined_report", "relative_path": report_rel, "sha256": hash_file(root / report_rel)[0]}],
            "program_validation": {"status": "passed", "validator_version": "synthetic-submitter", "checked_at": now(), "report_relative_path": report_rel}}

    def ref(self, bundle, block):
        return {"project_id": self.project, **block["asset_ref"], "block_id": block["block_id"],
            "bundle_id": bundle["bundle_id"], "bundle_revision": bundle["bundle_revision"], "locator": deepcopy(block["locator"])}

    def accept(self, bundle, task, result=None):
        result = result or self.result(bundle, task)
        path = self.store.project_root(self.project) / task["output_relative_path"] / "submission.json"
        path.write_bytes(canonical(result))
        self.analysis.complete(task["task_id"], path)
        return self.analysis.get_result(task["task_id"])

    def reseal(self, bundle, replacements=None):
        """Synthetic internally hashed current snapshot, not real evidence."""
        root = self.store.project_root(self.project)
        row = self.store.connection.execute("SELECT expected_files_json FROM publications WHERE publish_id=?", (bundle["publish_id"],)).fetchone()
        expected = json.loads(row[0])
        final = bundle["publication"]["manifest_relative_path"].rsplit("/", 1)[0]
        for ident, data in (replacements or {}).items():
            artifact = next(a for a in bundle["artifacts"] if a["artifact_id"] == ident)
            rel = final + "/replacement/" + Path(artifact["relative_path"]).name
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            sha, size = hash_file(path)
            artifact.update(relative_path=rel, sha256=sha, byte_length=size, artifact_revision="sha256-" + sha)
            expected[rel] = {"sha256": sha, "byte_length": size}
        index = next(a for a in bundle["artifacts"] if a["relative_path"] == final + "/blocks.jsonl")
        path = root / index["relative_path"]
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
        path.write_bytes(b"".join(canonical(b) + b"\n" for b in bundle["blocks"]))
        sha, size = hash_file(path)
        index.update(sha256=sha, byte_length=size, artifact_revision="sha256-" + sha)
        expected[index["relative_path"]] = {"sha256": sha, "byte_length": size}
        manifest = root / bundle["publication"]["manifest_relative_path"]
        manifest.chmod(stat.S_IREAD | stat.S_IWRITE)
        manifest.write_bytes(canonical(bundle))
        expected[bundle["publication"]["manifest_relative_path"]] = dict(zip(("sha256", "byte_length"), hash_file(manifest)))
        self.store.connection.execute("UPDATE publications SET bundle_json=?,manifest_sha256=?,expected_files_json=? WHERE publish_id=?",
            (canonical(bundle).decode(), hash_file(manifest)[0], canonical(expected).decode(), bundle["publish_id"]))

    def test_same_agent_inherited_reuse_and_new_block_pending(self):
        bundle, task = self.current()
        block_id = self.old["blocks"][0]["block_id"]
        before = self.store.connection.execute("SELECT COUNT(*) FROM read_receipts").fetchone()[0]
        delta = prepare(self.analysis, task["task_id"])
        self.assertEqual(delta["reusable_block_ids"], [block_id])
        self.assertEqual(len(delta["new_block_ids"]), 1)
        self.assertEqual(delta["pending_block_ids"], delta["new_block_ids"])
        self.assertTrue(all(p["status"] == "pending" for p in delta["block_processing"]))
        self.assertEqual(delta["reusable_assertions"][0]["evidence_refs"][0]["bundle_id"], bundle["bundle_id"])
        self.assertIn("Synthetic old limitation", delta["context"]["limitations"][0])
        self.assertIsNone(delta["context"]["usage"]["input_tokens"])
        self.assertEqual(before, self.store.connection.execute("SELECT COUNT(*) FROM read_receipts").fetchone()[0])
        with patch.object(self.analysis, "read", side_effect=AssertionError("no new delivery")), patch.object(self.analysis, "open_image", side_effect=AssertionError("no new image open")):
            reused = reuse(self.analysis, task["task_id"], [block_id])
        receipt = reused["items"][0]["receipt"]
        self.assertEqual(receipt["kind"], "reused")
        self.assertFalse(receipt["new_physical_read"])
        self.assertNotIn("artifact_hashes", receipt)
        self.assertNotIn("char_start", receipt)
        self.assertEqual(self.store.connection.execute("SELECT kind,content FROM read_receipts WHERE task_id=?", (task["task_id"],)).fetchone()[:], ("reused", None))
        block = next(b for b in bundle["blocks"] if b["block_id"] == block_id)
        self.assertTrue(self.analysis._has_read(self.analysis._task(task["task_id"]), block, bundle, [receipt["receipt_id"]]))
        self.assertEqual(self.analysis._task(task["task_id"])["state"], "running")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM analyses WHERE task_id=?", (task["task_id"],)).fetchone()[0], 0)

    def test_duplicate_reuse_is_idempotent(self):
        _, task = self.current()
        ident = self.old["blocks"][0]["block_id"]
        first = reuse(self.analysis, task["task_id"], [ident])
        second = reuse(self.analysis, task["task_id"], [ident])
        self.assertEqual(first, second)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM read_receipts WHERE task_id=?", (task["task_id"],)).fetchone()[0], 1)

    def test_model_prompt_or_provider_change_never_reuses(self):
        for overrides in ({"model_version": "other-model"}, {"prompt_version": "other-prompt"},
                          {"provider_id": "other-agent"}, {"provider_version": "other-version"}):
            with self.subTest(overrides=overrides):
                _, task = self.current(**overrides)
                delta = prepare(self.analysis, task["task_id"])
                self.assertFalse(delta["reusable_block_ids"])
                self.assertFalse(delta["reusable_assertions"])
                with self.assertRaises(ContractError):
                    reuse(self.analysis, task["task_id"], [self.old["blocks"][0]["block_id"]])

    def test_registered_block_producer_change_invalidates(self):
        bundle, task = self.current()
        old_id = self.old["blocks"][0]["block_id"]
        block = next(b for b in bundle["blocks"] if b["block_id"] == old_id)
        block["producer"] = self.store.register_provider("synthetic-new-producer", "test-2")
        self.reseal(bundle)
        delta = prepare(self.analysis, task["task_id"])
        self.assertIn(old_id, delta["changed_block_ids"])
        self.assertNotIn(old_id, delta["reusable_block_ids"])

    def test_complete_text_tail_not_inline_preview_is_compared(self):
        long = self.native("long.txt", "A" * 17000 + "OLD TAIL")
        old_task = self.claim()
        self.accept(long, old_task)
        current = compose(self.store, self.project, [long["bundle_id"]])
        task = self.claim()
        block = current["blocks"][0]
        text = next(a for a in current["artifacts"] if a["kind"] == "native_text" and Path(a["relative_path"]).name == block["block_id"] + ".txt")
        self.assertEqual(block["text"], long["blocks"][0]["text"])
        self.reseal(current, {text["artifact_id"]: ("A" * 17000 + "NEW TAIL").encode()})
        delta = prepare(self.analysis, task["task_id"])
        self.assertIn(block["block_id"], delta["changed_block_ids"])
        self.assertFalse(delta["reusable_block_ids"])

    def test_actual_image_read_can_reuse_but_changed_pixels_cannot(self):
        image_bundle = self.native("image-source.txt", "Actual image association.", SyntheticExtractor(image=True))
        old_task = self.claim()
        self.accept(image_bundle, old_task)
        current = compose(self.store, self.project, [image_bundle["bundle_id"]])
        task = self.claim()
        image_block = next(b for b in current["blocks"] if b["kind"] == "image_occurrence")
        self.assertIn(image_block["block_id"], prepare(self.analysis, task["task_id"])["reusable_block_ids"])
        receipt = reuse(self.analysis, task["task_id"], [image_block["block_id"]])["items"][0]["receipt"]
        self.assertTrue(validate_reused(self.analysis, self.analysis._task(task["task_id"]), image_block, current, receipt))
        from io import BytesIO
        from PIL import Image
        output = BytesIO()
        Image.new("RGB", (3, 2), "blue").save(output, format="PNG")
        image = next(a for a in current["artifacts"] if a["artifact_id"] in image_block["artifact_ids"] and a["kind"] == "image")
        self.reseal(current, {image["artifact_id"]: output.getvalue()})
        self.assertNotIn(image_block["block_id"], prepare(self.analysis, task["task_id"])["reusable_block_ids"])
        with self.assertRaises(ContractError):
            validate_reused(self.analysis, self.analysis._task(task["task_id"]), image_block, current, receipt)

    def test_wrong_project_bundle_version_and_supplied_receipt_raise(self):
        bundle, task = self.current()
        ident = self.old["blocks"][0]["block_id"]
        block = next(b for b in bundle["blocks"] if b["block_id"] == ident)
        receipt = reuse(self.analysis, task["task_id"], [ident])["items"][0]["receipt"]
        for key, value in (("project_id", "wrong-project"), ("bundle_revision", "v9999")):
            wrong = deepcopy(bundle)
            wrong[key] = value
            with self.assertRaises(ContractError):
                validate_reused(self.analysis, self.analysis._task(task["task_id"]), block, wrong, receipt)
        altered = deepcopy(receipt)
        altered["source_task_id"] = "wrong-task"
        with self.assertRaises(ContractError):
            validate_reused(self.analysis, self.analysis._task(task["task_id"]), block, bundle, altered)

    def test_tampered_accepted_original_receipt_raises_not_cache_miss(self):
        _, task = self.current()
        row = self.store.connection.execute("SELECT * FROM read_receipts WHERE task_id=?", (self.old_task["task_id"],)).fetchone()
        receipt = json.loads(row["receipt_json"])
        # It even has a valid content SHA and the old _has_read range, but no
        # longer contains the actual source slice. This must not be reused.
        content = "X" * len(row["content"])
        receipt["content_sha256"] = hashlib.sha256(content.encode()).hexdigest()
        self.store.connection.execute("UPDATE read_receipts SET content=?,receipt_json=? WHERE receipt_id=?", (content, canonical(receipt).decode(), row["receipt_id"]))
        with self.assertRaisesRegex(ContractError, "actual source read"):
            prepare(self.analysis, task["task_id"])

    def test_after_reuse_original_receipt_metadata_hash_is_pinned(self):
        bundle, task = self.current()
        ident = self.old["blocks"][0]["block_id"]
        block = next(b for b in bundle["blocks"] if b["block_id"] == ident)
        receipt = reuse(self.analysis, task["task_id"], [ident])["items"][0]["receipt"]
        row = self.store.connection.execute("SELECT * FROM read_receipts WHERE task_id=?", (self.old_task["task_id"],)).fetchone()
        original = json.loads(row["receipt_json"])
        original["created_at"] = "2000-01-01T00:00:00Z"
        self.store.connection.execute("UPDATE read_receipts SET receipt_json=? WHERE receipt_id=?", (canonical(original).decode(), row["receipt_id"]))
        with self.assertRaisesRegex(ContractError, "tampered"):
            validate_reused(self.analysis, self.analysis._task(task["task_id"]), block, bundle, receipt)

    def test_tampered_accepted_report_result_and_source_raise(self):
        bundle, task = self.current()
        accepted = self.analysis.get_result(self.old_task["task_id"])
        project_root = self.store.project_root(self.project)
        originals = [project_root / accepted["main_report_relative_path"],
            project_root / self.analysis._task(self.old_task["task_id"])["result_relative_path"],
            project_root / next(a for a in self.old["artifacts"] if a["kind"] == "original_small_file")["relative_path"]]
        for path in originals:
            with self.subTest(kind=path.name):
                data = path.read_bytes()
                path.chmod(stat.S_IREAD | stat.S_IWRITE)
                path.write_bytes(data[:-1] + (b"X" if data[-1:] != b"X" else b"Y"))
                try:
                    with self.assertRaises(ContractError):
                        prepare(self.analysis, task["task_id"])
                finally:
                    path.write_bytes(data)

    def test_old_pending_failed_excluded_are_never_promoted(self):
        for status in ("pending", "failed", "excluded"):
            with self.subTest(status=status):
                first = self.native(status + ".txt", "Actual first block for " + status + ".\n", SyntheticExtractor(image=True, gap=True))
                old_task = self.claim()
                unread = first["blocks"][-1]["block_id"]
                self.accept(first, old_task, self.result(first, old_task, {unread: status}))
                current = compose(self.store, self.project, [first["bundle_id"]])
                task = self.claim()
                delta = prepare(self.analysis, task["task_id"])
                self.assertNotIn(unread, delta["reusable_block_ids"])
                self.assertIn(unread, delta["pending_block_ids"])
                self.assertTrue(delta["context"]["open_gaps"])
                self.assertIn(status, [p["status"] for p in delta["context"]["prior_block_processing"]])
                with self.assertRaises(ContractError):
                    reuse(self.analysis, task["task_id"], [unread])

    def test_removed_assertion_dependency_invalidates_transitive_closure(self):
        second = self.native("dependency.txt", "Actual dependent instruction.")
        joined = compose(self.store, self.project, [self.old["bundle_id"], second["bundle_id"]])
        old_task = self.claim()
        result = self.result(joined, old_task)
        b_id = second["blocks"][0]["block_id"]
        fact = next(a for a in result["assertions"] if a["evidence_refs"][0]["block_id"] == b_id)
        result["assertions"].append({"assertion_id": "inference-child", "kind": "inference", "statement": "Synthetic dependent inference",
            "evidence_refs": [], "external_source_ids": [], "depends_on_assertion_ids": [fact["assertion_id"]],
            "verification_status": "unverified", "uncertainty": "Depends on removed evidence"})
        self.accept(joined, old_task, result)
        current = compose(self.store, self.project, [self.old["bundle_id"]])
        task = self.claim()
        delta = prepare(self.analysis, task["task_id"])
        self.assertIn(b_id, delta["removed_block_ids"])
        self.assertIn(fact["assertion_id"], delta["invalidated_assertion_ids"])
        self.assertIn("inference-child", delta["invalidated_assertion_ids"])
        self.assertEqual(len(delta["reusable_assertions"]), 1)

    def test_changed_block_dependency_invalidates_child_even_with_same_child_bytes(self):
        second = self.native("derived.txt", "Actual derived instruction.")
        joined = compose(self.store, self.project, [self.old["bundle_id"], second["bundle_id"]])
        parent = next(b for b in joined["blocks"] if b["block_id"] == self.old["blocks"][0]["block_id"])
        child = next(b for b in joined["blocks"] if b["block_id"] == second["blocks"][0]["block_id"])
        child["derived_from"] = [self.ref(joined, parent)]
        self.reseal(joined)
        old_task = self.claim()
        self.accept(joined, old_task)
        current = compose(self.store, self.project, [joined["bundle_id"]])
        task = self.claim()
        new_parent = next(b for b in current["blocks"] if b["block_id"] == parent["block_id"])
        new_parent["producer"] = self.store.register_provider("synthetic-dependency-producer", "test-2")
        self.reseal(current)
        delta = prepare(self.analysis, task["task_id"])
        self.assertIn(parent["block_id"], delta["changed_block_ids"])
        self.assertIn(child["block_id"], delta["changed_block_ids"])
        self.assertFalse(delta["reusable_assertions"])

    def test_multi_hop_is_ineligible_and_invalid_new_ids_are_atomic(self):
        current = compose(self.store, self.project, [self.old["bundle_id"]])
        task = self.claim()
        ident = current["blocks"][0]["block_id"]
        reused = reuse(self.analysis, task["task_id"], [ident])
        # Write a new report, reuse the existing historical receipt, and submit
        # through the actual integrated _has_read hook (no new source read).
        result = self.result(current, task, {ident: "pending"})
        result["block_processing"] = reused["block_processing"]
        result["coverage"].update(read_blocks=1, pending_blocks=0, claim="all_discovered_read")
        result["execution_status"] = "completed"
        result["assertions"] = prepare(self.analysis, task["task_id"])["reusable_assertions"]
        self.accept(current, task, result)
        newest = compose(self.store, self.project, [current["bundle_id"]])
        newest_task = self.claim()
        self.assertFalse(prepare(self.analysis, newest_task["task_id"])["reusable_block_ids"])
        with self.assertRaises(ContractError):
            reuse(self.analysis, newest_task["task_id"], [ident, "missing-block"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM read_receipts WHERE task_id=?", (newest_task["task_id"],)).fetchone()[0], 0)

    def test_admission_precedes_source_and_accepted_json_decode(self):
        _, task = self.current()
        # Existing stored values are observed before replacing only temporary DB
        # test rows. No real schema/material or production DB is changed.
        self.store.connection.execute("UPDATE analysis_publications SET outputs_json=? WHERE task_id=?",
            ('{"oversized":"' + "X" * 100 + '"}', self.old_task["task_id"]))
        with patch.object(incremental, "_MAX_OUTPUTS", 64), patch.object(self.analysis, "get_result", side_effect=AssertionError("must preflight first")):
            with self.assertRaisesRegex(ContractError, "journal-committed"):
                prepare(self.analysis, task["task_id"])
        with self.assertRaisesRegex(ContractError, "complexity"):
            incremental._loads("[" * 49 + "0" + "]" * 49)


if __name__ == "__main__":
    unittest.main()
