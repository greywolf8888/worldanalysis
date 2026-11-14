"""Seven G-local synthetic recovery checks; never open the business ledger."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_analysis as fixtures
from worldanalysis.analysis import Analysis
from worldanalysis.analysis_recovery import claim_pending, envelope, requeue_block
from worldanalysis.contracts import ContractError
from worldanalysis.store import Store


class RecoveryTests(unittest.TestCase):
    result, submit = fixtures.AnalysisTests.result, fixtures.AnalysisTests.submit
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".recovery-test-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.addCleanup(lambda: self.store.close())
        self.project = self.store.create_project(project_id="recovery-synthetic")
        path = self.root / "source.txt"
        path.write_bytes(b"Actual instruction.\nExcluded instruction.\nStill pending.\n")
        self.bundle = self.store.ingest(self.project, [path], fixtures.extract)
        self.analysis = Analysis(self.store)
        self.task = self.analysis.claim(self.project, model_version="synthetic-recovery-model")
        self.project_root = self.store.project_root(self.project)
        self.target, self.pending = [b["block_id"] for b in self.bundle["blocks"]][1:]
        result = self.result(status="partial", statuses={self.target: "excluded", self.pending: "pending"})
        target = next(e for e in result["block_processing"] if e["block_id"] == self.target)
        ref = {"project_id": self.project, **target["asset_ref"], "block_id": self.target, "locator": self.bundle["blocks"][1]["locator"]}
        target["exclusion"] = {"reason": "Synthetic exclusion", "basis_refs": [ref], "recovery_ref": self.target, "restore_action": "requeue_block"}
        result["gaps"].append({"gap_id": "gap-analysis-only", "kind": "semantic_gap", "severity": "important", "status": "open", "reason": "Keep the original open gap", "target_refs": [ref], "resolution_refs": []})
        self.submit(result)
        self.accepted = self.analysis.get_result(self.task["task_id"])

    def queue(self, **kw):
        return requeue_block(self.analysis, self.task["task_id"], self.target, reason=kw.pop("reason", "Explicit recovery"), **kw)

    def test_pending_persists_same_agent_pub_and_history(self):
        queued = self.queue()
        self.store.close()
        self.store = Store(self.root)
        self.analysis = Analysis(self.store)
        task = self.analysis._task(queued["recovery_task_id"])
        self.assertEqual((task["state"], task["agent_json"], task["publish_id"]), ("queued", self.analysis._task(self.task["task_id"])["agent_json"], self.task["publish_id"]))
        current = envelope(self.analysis, task, self.analysis._envelope(task))
        self.assertTrue(all(e["status"] == "pending" and not e["read_receipt_refs"] and e["exclusion"] is None for e in current["initial_block_processing"]))
        self.assertEqual((current["gaps"], current["source_block_processing"]), (self.accepted["gaps"], self.accepted["block_processing"]))
        self.assertEqual(self.analysis.get_result(self.task["task_id"]), self.accepted)

    def test_replay_conflict_and_pause_do_not_duplicate_or_advance(self):
        first = self.queue(request_id="request-explicit")
        before = self.store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.store.control("analysis", "pause")
        self.assertEqual(self.queue(request_id="request-explicit")["recovery_task_id"], first["recovery_task_id"])
        self.assertEqual(self.queue(reason="New paused request")["status"], "blocked")
        with self.assertRaises(ContractError): self.queue(request_id="request-explicit", reason="Different input")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM analysis_tasks").fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], before + 1)

    def test_claim_is_exact_agent_once_and_real_reads_execute(self):
        queued = self.queue()
        agent = self.task["agent"]
        self.assertIsNone(claim_pending(self.analysis, self.project, {**agent, "model_version": "other-real-model"}))
        self.assertIsNone(claim_pending(self.analysis, "other-project", agent))
        claimed = self.analysis.claim(self.project, model_version=agent["model_version"])
        self.assertEqual(claimed["task_id"], queued["recovery_task_id"])
        self.assertIsNone(claim_pending(self.analysis, self.project, agent))
        self.assertEqual(self.analysis.resume(claimed["task_id"])["recovery"]["initial_target_status"], "pending")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM read_receipts WHERE task_id=?", (claimed["task_id"],)).fetchone()[0], 0)
        item = self.analysis.read(claimed["task_id"], [self.target])["items"][0]
        self.assertEqual((item["status"], item["receipt"]["task_id"]), ("read", claimed["task_id"]))

    def test_nonexcluded_unknown_block_and_empty_reason_rejected(self):
        for block in (self.pending, "wrong-project-block"):
            with self.assertRaises(ContractError): requeue_block(self.analysis, self.task["task_id"], block, reason="Recover")
        with self.assertRaises(ContractError): self.queue(reason=" ")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM analysis_tasks").fetchone()[0], 1)

    def test_stale_source_and_legacy_without_journal_fail_closed(self):
        queued = self.queue()
        self.store.connection.execute("UPDATE publications SET state='quarantined' WHERE publish_id=?", (self.task["publish_id"],))
        with self.assertRaises(ContractError): claim_pending(self.analysis, self.project, self.task["agent"])
        self.assertEqual(self.analysis._task(queued["recovery_task_id"])["state"], "queued")
        self.store.connection.execute("UPDATE publications SET state='published' WHERE publish_id=?", (self.task["publish_id"],))
        self.store.connection.execute("DELETE FROM analysis_publications WHERE task_id=?", (self.task["task_id"],))
        with self.assertRaisesRegex(ContractError, "unsupported"): self.queue(reason="Another attempt")

    def test_event_failure_rolls_back_attempt_then_retry_is_idempotent(self):
        with patch.object(self.store, "event", side_effect=RuntimeError("injected event failure")):
            with self.assertRaises(RuntimeError): self.queue()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM analysis_tasks").fetchone()[0], 1)
        self.assertEqual(self.queue()["recovery_task_id"], self.queue()["recovery_task_id"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='analysis_recovery_requested'").fetchone()[0], 1)

    def test_submission_preserves_analysis_only_gaps_without_partial_commit(self):
        self.queue()
        self.task = self.analysis.claim(self.project, model_version=self.task["agent"]["model_version"])
        result = self.result(status="partial", statuses={self.pending: "pending"})
        result["analysis_id"] = "synthetic-recovery-result"
        gap = deepcopy(self.accepted["gaps"][0])
        for gaps in ([], [{**gap, "reason": "Changed history"}], [{**gap, "status": "resolved", "resolution_refs": deepcopy(gap["target_refs"])}]):
            result["gaps"] = gaps
            with self.assertRaisesRegex(ContractError, "preserve source accepted gaps"):
                self.submit(result)
            for table in ("analysis_publications", "analyses"):
                self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM " + table + " WHERE task_id=?", (self.task["task_id"],)).fetchone()[0], 0)
            self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='analysis_completed' AND record_id=?", (self.task["task_id"],)).fetchone()[0], 0)
            self.assertFalse(list((self.project_root / self.task["output_relative_path"]).glob("*accepted*")))
        result["gaps"] = [gap, {**deepcopy(gap), "gap_id": "gap-new-recovery-question"}]
        self.assertEqual(self.submit(result)["status"], "partial")
        self.assertEqual(self.analysis.get_result(self.task["task_id"])["gaps"], result["gaps"])
