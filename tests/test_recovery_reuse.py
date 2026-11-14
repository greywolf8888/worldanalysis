"""Five G-local synthetic checks: two physical reads, one excluded target."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_incremental_analysis as fixtures
from worldanalysis.analysis import Analysis
from worldanalysis.analysis_recovery import validate_result
from worldanalysis import incremental_analysis as incremental
from worldanalysis.contracts import ContractError, canonical
from worldanalysis.store import Store


def extract(path, output, options=None):
    raw = fixtures.SyntheticExtractor(image=True, gap=True)(path, output, options)
    raw["blocks"].append({"kind": "paragraph", "origin": "native_extract",
        "locator": {"kind": "paragraph", "precision": "exact", "paragraph_index": 2},
        "text": path.read_text(encoding="utf-8").splitlines()[-1], "file_keys": [],
        "processing_status": "extracted", "reason": "Synthetic placeholder for explicit exclusion"})
    return raw


class RecoveryReuseTests(unittest.TestCase):
    result, accept, ref = fixtures.IncrementalTests.result, fixtures.IncrementalTests.accept, fixtures.IncrementalTests.ref

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".recovery-reuse-test-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.addCleanup(lambda: self.store.close())
        self.project = self.store.create_project(project_id="recovery-reuse-synthetic")
        path = self.root / "synthetic.txt"
        path.write_text("Actual source instruction.\nSynthetic excluded placeholder.", encoding="utf-8")
        self.bundle = self.store.ingest(self.project, [path], extract)
        self.analysis = Analysis(self.store)
        self.source_task = self.analysis.claim(self.project, model_version="synthetic-recovery-reuse-model")
        self.target = self.bundle["blocks"][-1]["block_id"]
        result = self.result(self.bundle, self.source_task, {self.target: "excluded"})
        extra = {**deepcopy(result["gaps"][0]), "gap_id": "gap-analysis-only"}
        result["gaps"].append(extra)
        self.source_result = self.accept(self.bundle, self.source_task, result)
        self.physical = sorted(e["block_id"] for e in self.source_result["block_processing"] if e["status"] == "read")
        self.analysis.requeue_block(self.source_task["task_id"], self.target, reason="Synthetic explicit recovery")
        self.task = self.analysis.claim(self.project, model_version=self.source_task["agent"]["model_version"])

    def reuse(self):
        return incremental.reuse(self.analysis, self.task["task_id"], self.physical)

    def check_receipt(self, item):
        block = next(b for b in self.bundle["blocks"] if b["block_id"] == item["block_id"])
        return incremental.validate_reused(self.analysis, self.analysis._task(self.task["task_id"]), block, self.bundle, item["receipt"])

    def count(self, task_id=None):
        return self.store.connection.execute("SELECT COUNT(*) FROM read_receipts WHERE task_id=?", (task_id or self.task["task_id"],)).fetchone()[0]

    def test_exact_bound_source_reuses_only_two_physical_reads(self):
        delta = incremental.prepare(self.analysis, self.task["task_id"])
        self.assertEqual((delta["source_task_id"], delta["reusable_block_ids"], delta["pending_block_ids"]), (self.source_task["task_id"], self.physical, [self.target]))
        with patch.object(self.analysis, "read", side_effect=AssertionError("no new text delivery")), patch.object(self.analysis, "open_image", side_effect=AssertionError("no new image open")):
            result = self.reuse()
        for item in result["items"]:
            self.assertEqual((item["receipt"]["kind"], item["receipt"]["source_publish_id"], item["new_physical_read"]), ("reused", self.task["publish_id"], False))
            self.assertTrue(self.check_receipt(item))
        with self.assertRaises(ContractError): incremental.reuse(self.analysis, self.task["task_id"], [self.target])
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM read_receipts WHERE task_id=? AND kind IN ('text','image')", (self.task["task_id"],)).fetchone()[0], 0)

    def test_same_publication_without_binding_and_v1_are_rejected(self):
        result = self.reuse()
        row = self.store.connection.execute("SELECT * FROM analysis_recovery_requests WHERE recovery_task_id=?", (self.task["task_id"],)).fetchone()
        self.store.connection.execute("DELETE FROM analysis_recovery_requests WHERE recovery_task_id=?", (self.task["task_id"],))
        self.assertFalse(incremental.prepare(self.analysis, self.task["task_id"])["reusable_block_ids"])
        with self.assertRaisesRegex(ContractError, "not prior"): self.check_receipt(result["items"][0])
        self.store.connection.execute("INSERT INTO analysis_recovery_requests VALUES(?,?,?,?,?,?,?,?,?)", tuple(row))
        run = incremental._Run(self.analysis)
        task = run.task(self.task["task_id"])
        with self.assertRaisesRegex(ContractError, "requires v2"): incremental._plan(run, task, incremental._POLICY)
        _, prior, current, proofs = incremental._plan(run, task)
        run.policy_fingerprints(current, incremental._POLICY)
        ident = self.physical[0]
        v1 = self.analysis._save_receipt(task, ident, "reused", incremental._payload(task, prior, current, ident, proofs[ident], incremental._POLICY))
        with self.assertRaisesRegex(ContractError, "not prior"): self.check_receipt({"block_id": ident, "receipt": v1})

    def test_invalid_binding_hash_and_stale_source_issue_no_receipts(self):
        for field, wrong in (("source_task_id", self.task["task_id"]), ("source_result_sha256", "0" * 64), ("block_id", self.physical[0])):
            old = self.store.connection.execute("SELECT " + field + " FROM analysis_recovery_requests WHERE recovery_task_id=?", (self.task["task_id"],)).fetchone()[0]
            try:
                self.store.connection.execute("UPDATE analysis_recovery_requests SET " + field + "=? WHERE recovery_task_id=?", (wrong, self.task["task_id"]))
                with self.assertRaises(ContractError): self.reuse()
                self.assertEqual(self.count(), 0)
            finally:
                self.store.connection.execute("UPDATE analysis_recovery_requests SET " + field + "=? WHERE recovery_task_id=?", (old, self.task["task_id"]))
        self.store.connection.execute("UPDATE publications SET state='quarantined' WHERE publish_id=?", (self.task["publish_id"],))
        with self.assertRaises(ContractError): self.reuse()
        self.assertEqual(self.count(), 0)

    def test_recovery_from_reused_source_never_falls_back_or_multi_hops(self):
        reused = self.reuse()
        result = self.result(self.bundle, self.task, {b["block_id"]: "pending" for b in self.bundle["blocks"]})
        target = deepcopy(next(e for e in self.source_result["block_processing"] if e["block_id"] == self.target))
        result["block_processing"] = reused["block_processing"] + [target]
        result["coverage"].update(read_blocks=2, pending_blocks=0, excluded_blocks=1, claim="partial")
        result["assertions"], result["gaps"] = deepcopy(self.source_result["assertions"]), deepcopy(self.source_result["gaps"])
        self.accept(self.bundle, self.task, result)
        self.analysis.requeue_block(self.task["task_id"], self.target, reason="Synthetic second recovery attempt")
        newest = self.analysis.claim(self.project, model_version=self.task["agent"]["model_version"])
        delta = incremental.prepare(self.analysis, newest["task_id"])
        self.assertEqual(delta["source_task_id"], self.task["task_id"])
        self.assertFalse(delta["reusable_block_ids"])
        with self.assertRaises(ContractError): incremental.reuse(self.analysis, newest["task_id"], self.physical)
        self.assertEqual(self.count(newest["task_id"]), 0)

    def test_idempotent_receipts_pin_original_reads_and_preserve_gaps(self):
        old = [tuple(r) for r in self.store.connection.execute("SELECT * FROM read_receipts WHERE task_id=? ORDER BY receipt_id", (self.source_task["task_id"],))]
        first = self.reuse()
        self.assertEqual(first, self.reuse())
        self.assertEqual(self.count(), 2)
        self.assertEqual(old, [tuple(r) for r in self.store.connection.execute("SELECT * FROM read_receipts WHERE task_id=? ORDER BY receipt_id", (self.source_task["task_id"],))])
        self.assertEqual(canonical(self.analysis.get_result(self.source_task["task_id"])), canonical(self.source_result))
        validate_result(self.analysis, self.analysis._task(self.task["task_id"]), {"gaps": deepcopy(self.source_result["gaps"])})
        with self.assertRaisesRegex(ContractError, "preserve"): validate_result(self.analysis, self.analysis._task(self.task["task_id"]), {"gaps": []})
        row = self.store.connection.execute("SELECT * FROM read_receipts WHERE task_id=? AND kind='text'", (self.source_task["task_id"],)).fetchone()
        receipt = json.loads(row["receipt_json"])
        receipt["created_at"] = "2000-01-01T00:00:00Z"  # Corrupt only this disposable fixture.
        self.store.connection.execute("UPDATE read_receipts SET receipt_json=? WHERE receipt_id=?", (canonical(receipt).decode(), row["receipt_id"]))
        item = next(i for i in first["items"] if i["block_id"] == row["block_id"])
        with self.assertRaisesRegex(ContractError, "tampered"): self.check_receipt(item)
