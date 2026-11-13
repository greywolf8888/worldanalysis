"""Seven TempStore-only exact-window reconciliation and atomicity checks."""
from copy import deepcopy
import json
import stat
import unittest
from unittest.mock import patch

from test_audio_publication import Fixture
from worldanalysis import audio_delivery as delivery
from worldanalysis import audio_publication as publication
from worldanalysis.contracts import ContractError, canonical, hash_file


class AudioDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.f.setup(self)
        self.request = self.f.request()
        self.bundle = None

    def publish(self, *, max_received_bytes=None):
        result = self.f.publish(request_id=self.request["request_id"], max_received_bytes=max_received_bytes)
        self.bundle = result["bundle"]
        self.assertEqual(result["request_state"], "queued")
        return self.bundle

    def reconcile(self, request=None, bundle=None):
        return delivery.reconcile_audio_window(self.f.store, (request or self.request)["request_id"], (bundle or self.bundle)["bundle_id"])

    def state(self, request=None):
        return self.f.store.connection.execute("SELECT state FROM supplement_requests WHERE request_id=?", ((request or self.request)["request_id"],)).fetchone()[0]

    def immutable(self):
        tables = ("publications", "jobs", "plans", "providers", "provider_configurations", "analyses", "analysis_tasks", "read_receipts")
        rows = {t: [tuple(r) for r in self.f.store.connection.execute("SELECT * FROM " + t + " ORDER BY 1")] for t in tables}
        files = {p.relative_to(self.f.root).as_posix(): hash_file(p) for p in self.f.root.rglob("*")
                 if p.is_file() and not p.name.endswith((".sqlite3", ".sqlite3-wal", ".sqlite3-shm"))}
        return rows, files

    def test_exact_publication_fulfils_once_without_extraction_or_listening_and_restarts(self):
        from worldanalysis.media_store import identity
        publication.primitive.extract_audio_window(self.f.root, self.f.source, "窗口派生/window.wav", 2940, 4420,
            expected_identity=identity(self.f.source), max_received_bytes=1048576)
        with patch.object(publication.primitive, "_decode", side_effect=AssertionError("existing window cannot decode")):
            self.publish(max_received_bytes=1048576)
        before = self.immutable()
        with patch.object(publication.primitive, "extract_audio_window", side_effect=AssertionError("delivery cannot decode")), patch.object(self.f.store, "publish", side_effect=AssertionError("delivery cannot publish")):
            result = self.reconcile()
            self.assertEqual(result["status"], "fulfilled")
            self.assertFalse(result["idempotent"])
            self.assertFalse(result["semantic_read_performed"])
            self.assertFalse(result["gaps_resolved"])
            self.assertFalse(result["new_publication_created"])
            self.assertIsNone(result["source_content_sha256"])
            self.assertTrue(self.reconcile()["idempotent"])
        self.assertEqual(self.state(), "fulfilled")
        self.assertEqual(self.immutable(), before)
        self.f.restart()
        self.assertTrue(self.reconcile()["idempotent"])
        self.assertEqual(self.immutable(), before)
        self.assertEqual(self.f.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='audio_window_delivered'").fetchone()[0], 1)
        self.f.assert_original(self)

    def test_other_genuine_same_window_proposal_reuses_same_publication(self):
        other = self.f.request("second-audio-request")
        self.publish()
        before = self.immutable()
        self.assertEqual(self.reconcile(request=other)["status"], "fulfilled")
        self.assertEqual(self.state(), "queued")
        self.assertTrue(self.reconcile(request=other)["idempotent"])
        self.assertEqual(self.reconcile()["status"], "fulfilled")
        self.assertEqual(self.immutable(), before)
        self.assertEqual(self.f.store.connection.execute("SELECT COUNT(*) FROM audio_window_deliveries").fetchone()[0], 2)

    def test_wrong_exact_window_nonpublication_and_wrong_asset_proposals_cannot_fulfil(self):
        wrong = self.f.request("wrong-window", start=2000, end=3000)
        self.publish()
        before = self.immutable()
        with self.assertRaises(ContractError):
            self.reconcile(request=wrong)
        with self.assertRaises(ContractError):
            self.reconcile(bundle=self.f.bundle)
        other = self.f.store.create_project(project_id="unrelated-project", topic="No authority")
        with self.assertRaises(ContractError):
            publication.publish_audio_window(self.f.store, other, self.f.bundle["bundle_id"], self.f.asset["asset_id"], self.f.asset["asset_revision"], 2940, 4420, output_relative="窗口派生/no.wav")
        self.assertEqual(self.state(), "queued")
        self.assertEqual(self.state(wrong), "queued")
        # New unrelated project is the only change, never a delivery/publication.
        self.assertEqual(self.immutable()[0], before[0])

    def test_queued_database_only_or_mutated_api_proposal_has_no_authority(self):
        self.publish()
        before = self.immutable()
        row = self.f.store.connection.execute("SELECT * FROM supplement_requests WHERE request_id=?", (self.request["request_id"],)).fetchone()
        fake = deepcopy(self.request)
        fake["request_id"] = "fake-database-only"
        self.f.store.connection.execute("INSERT INTO supplement_requests VALUES(?,?,?,?,?,?,?)", (fake["request_id"], row["project_id"], row["task_id"], row["publish_id"], "queued", canonical(fake).decode(), row["created_at"]))
        with self.assertRaisesRegex(ContractError, "provenance"):
            self.reconcile(request=fake)
        changed = deepcopy(self.request)
        changed["minimal_targets"][0]["locator"]["start_ms"] += 1
        self.f.store.connection.execute("UPDATE supplement_requests SET request_json=? WHERE request_id=?", (canonical(changed).decode(), self.request["request_id"]))
        with self.assertRaisesRegex(ContractError, "provenance"):
            self.reconcile()
        self.assertEqual(self.state(), "queued")
        self.assertEqual(self.immutable(), before)

    def test_source_stat_change_blocks_delivery_but_history_get_remains_readable(self):
        self.publish()
        with self.f.source.open("ab") as handle:
            handle.write(b"source changed outside window")
        with self.assertRaises(ContractError):
            self.reconcile()
        self.assertEqual(self.state(), "queued")
        self.assertEqual(self.f.store.get_bundle(self.f.project, self.bundle["bundle_id"]), self.bundle)
        self.f.assert_original(self, check_source=False)

    def test_fulfilled_replay_rechecks_actual_sha_intention_job_and_never_listens(self):
        self.publish()
        result = self.reconcile()
        store = self.f.store
        job_id = self.bundle["blocks"][-1]["processing"]["recovery_ref"]
        row = store.connection.execute("SELECT receipt_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        store.connection.execute("UPDATE jobs SET receipt_json=? WHERE job_id=?", (canonical({"publish_id": self.f.bundle["publish_id"]}).decode(), job_id))
        with self.assertRaises(ContractError):
            self.reconcile()
        store.connection.execute("UPDATE jobs SET receipt_json=? WHERE job_id=?", (row[0], job_id))
        event = store.connection.execute("SELECT * FROM events WHERE kind='audio_window_derivation_intended' AND record_id=?", (self.bundle["publish_id"],)).fetchone()
        payload = json.loads(event["payload_json"])
        payload["specification"]["locator"]["start_ms"] += 1
        store.connection.execute("UPDATE events SET payload_json=? WHERE event_id=?", (canonical(payload).decode(), event["event_id"]))
        with self.assertRaises(ContractError):
            self.reconcile()
        store.connection.execute("UPDATE events SET payload_json=? WHERE event_id=?", (event["payload_json"], event["event_id"]))
        store.control("collection", "pause")
        self.assertTrue(self.reconcile()["idempotent"])
        path = self.f.project_root / result["relative_path"]
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.chmod(stat.S_IWRITE)
        path.write_bytes(data)
        with self.assertRaises(ContractError):
            self.reconcile()
        self.assertEqual(self.state(), "fulfilled")
        self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM read_receipts").fetchone()[0], 0)
        self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='audio_window_delivered'").fetchone()[0], 1)

    def test_pause_and_event_fault_roll_back_request_delivery_row_and_event_atomically(self):
        self.publish()
        store = self.f.store
        store.control("collection", "pause")
        self.assertEqual(self.reconcile()["status"], "blocked")
        self.assertEqual(self.state(), "queued")
        self.assertIsNone(store.connection.execute("SELECT name FROM sqlite_master WHERE name='audio_window_deliveries'").fetchone())
        store.control("collection", "resume")
        before = self.immutable()
        original = store.event
        def fail(kind, *args, **kwargs):
            if kind == "audio_window_delivered":
                raise RuntimeError("synthetic atomic delivery fault")
            return original(kind, *args, **kwargs)
        with patch.object(store, "event", side_effect=fail), self.assertRaisesRegex(RuntimeError, "atomic delivery fault"):
            self.reconcile()
        self.assertEqual(self.state(), "queued")
        self.assertIsNone(store.connection.execute("SELECT name FROM sqlite_master WHERE name='audio_window_deliveries'").fetchone())
        self.assertEqual(self.immutable(), before)
        self.assertEqual(self.reconcile()["status"], "fulfilled")


if __name__ == "__main__":
    unittest.main()
