"""Eight TempStore-only reconciliation checks; frozen publisher stays unchanged."""
from copy import deepcopy
import json
import stat
import unittest
from unittest.mock import patch

import test_derived_publication as _fixtures
from worldanalysis import derived_delivery as delivery
from worldanalysis import derived_publication as publication
from worldanalysis.contracts import ContractError, canonical, hash_file


class DerivedDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.f = _fixtures.DerivedPublicationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.request = self.f.queue_request()

    @property
    def store(self):
        return self.f.store

    def published(self, **kwargs):
        result = self.f.publish(request_id=self.request["request_id"], **kwargs)
        self.bundle = result["bundle"]
        return self.bundle

    def reconcile(self, *, request=None, bundle=None):
        return delivery.reconcile_image_region(self.store, (request or self.request)["request_id"], (bundle or self.bundle)["bundle_id"])

    def state(self, request=None):
        return self.store.connection.execute("SELECT state FROM supplement_requests WHERE request_id=?", ((request or self.request)["request_id"],)).fetchone()[0]

    def immutable(self):
        tables = ("publications", "jobs", "plans", "providers", "provider_configurations", "analyses", "analysis_tasks", "read_receipts")
        rows = {t: [tuple(r) for r in self.store.connection.execute("SELECT * FROM " + t + " ORDER BY 1")] for t in tables}
        files = {p.relative_to(self.f.root).as_posix(): hash_file(p) for p in self.f.root.rglob("*")
                 if p.is_file() and p.name not in {"worldanalysis.sqlite3", "worldanalysis.sqlite3-wal", "worldanalysis.sqlite3-shm"}}
        return rows, files

    def test_existing_publication_fulfils_exact_request_and_revalidates_after_restart(self):
        self.published()
        before = self.immutable()
        with patch.object(publication.image_region, "crop_region", side_effect=AssertionError("delivery must not crop")), patch.object(self.store, "publish", side_effect=AssertionError("delivery must not publish")):
            result = self.reconcile()
            self.assertEqual(result["status"], "fulfilled")
            self.assertFalse(result["idempotent"])
            self.assertFalse(result["semantic_read_performed"])
            self.assertFalse(result["gaps_resolved"])
            self.assertFalse(result["new_publication_created"])
            self.assertTrue(self.reconcile()["idempotent"])
        self.assertEqual(self.state(), "fulfilled")
        self.assertEqual(before, self.immutable())
        self.f.restart()
        self.assertTrue(self.reconcile()["idempotent"])
        self.assertEqual(before, self.immutable())
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='image_region_delivered'").fetchone()[0], 1)
        self.assertEqual(hash_file(publication.__file__)[0], "d92c24b6eb089b22550328bfde0fccc006017ecfcb1c7fe8d914fe531bb63b9f")
        self.f.assert_original()

    def test_another_genuine_same_roi_request_can_use_first_requests_publication(self):
        second = self.f.queue_request("another-region-request")
        self.published()
        before = self.immutable()
        self.assertEqual(self.reconcile(request=second)["status"], "fulfilled")
        self.assertEqual(self.state(), "queued")
        self.assertTrue(self.reconcile(request=second)["idempotent"])
        self.assertEqual(self.reconcile()["status"], "fulfilled")
        self.assertEqual(before, self.immutable())
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM derived_image_region_deliveries").fetchone()[0], 2)

    def test_wrong_roi_non_derived_cross_project_and_different_source_deny(self):
        wrong = self.f.queue_request("wrong-roi", box=_fixtures.bbox(1, 1, 10, 7))
        self.published()
        with self.assertRaisesRegex(ContractError, "ROI"):
            self.reconcile(request=wrong)
        with self.assertRaises(ContractError):
            self.reconcile(bundle=self.f.bundle)
        other = self.store.create_project(project_id="another-project", topic="Separate source authorization")
        native = self.store.ingest(other, [self.f.source], self.f.extractor, config_version="other-scope")
        with self.assertRaises(ContractError):
            self.reconcile(bundle=native)
        revised = self.store.ingest(self.f.project, [self.f.source], self.f.extractor, config_version="other-native-config")
        block = revised["blocks"][0]
        artifact = next(a for a in revised["artifacts"] if a["artifact_id"] in block["artifact_ids"])
        derived = publication.publish_image_region(self.store, self.f.project, revised["bundle_id"], block["block_id"], artifact["artifact_id"],
            _fixtures.bbox(), output_relative="补采派生/other-source.png")
        with self.assertRaisesRegex(ContractError, "source/version"):
            self.reconcile(bundle=derived)
        self.assertEqual(self.state(), "queued")
        self.assertEqual(self.state(wrong), "queued")

    def test_model_or_database_only_fake_request_has_no_proposal_provenance(self):
        self.published()
        before = self.immutable()
        request = deepcopy(self.request)
        request["request_id"] = "fake-request"
        row = self.store.connection.execute("SELECT * FROM supplement_requests WHERE request_id=?", (self.request["request_id"],)).fetchone()
        self.store.connection.execute("INSERT INTO supplement_requests VALUES(?,?,?,?,?,?,?)", (request["request_id"], row["project_id"], row["task_id"], row["publish_id"], "queued", canonical(request).decode(), row["created_at"]))
        with self.assertRaisesRegex(ContractError, "provenance"):
            self.reconcile(request=request)
        self.assertEqual(self.state(request), "queued")
        self.assertEqual(before, self.immutable())

    def test_fulfilled_repeats_still_reject_changed_published_bitmap_while_paused(self):
        self.published()
        result = self.reconcile()
        self.store.control("collection", "pause")
        path = self.f.project_root / result["relative_path"]
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.chmod(stat.S_IWRITE)
        path.write_bytes(data)
        with self.assertRaisesRegex(ContractError, "hash"):
            self.reconcile()
        self.assertEqual(self.state(), "fulfilled")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='image_region_delivered'").fetchone()[0], 1)

    def test_resize_is_explicitly_unsupported_without_fulfilling(self):
        self.published(resize=(4, 3))
        before = self.immutable()
        with self.assertRaisesRegex(ContractError, "without resize"):
            self.reconcile()
        self.assertEqual(self.state(), "queued")
        self.assertEqual(before, self.immutable())

    def test_current_original_producer_and_intention_changes_deny(self):
        self.published()
        self.reconcile()
        source_bytes = self.f.source.read_bytes()
        self.f.source.write_bytes(b"changed native original input")
        with self.assertRaisesRegex(ContractError, "original source changed"):
            self.reconcile()
        self.f.source.write_bytes(source_bytes)
        event = self.store.connection.execute("SELECT * FROM events WHERE kind='image_region_derivation_intended' AND record_id=?", (self.bundle["publish_id"],)).fetchone()
        value = json.loads(event["payload_json"])
        value["specification"]["bbox"] = _fixtures.bbox(1, 1, 10, 7)
        self.store.connection.execute("UPDATE events SET payload_json=? WHERE event_id=?", (canonical(value).decode(), event["event_id"]))
        with self.assertRaisesRegex(ContractError, "intention"):
            self.reconcile()
        self.store.connection.execute("UPDATE events SET payload_json=? WHERE event_id=?", (event["payload_json"], event["event_id"]))
        job_id = self.bundle["blocks"][-1]["processing"]["recovery_ref"]
        self.store.connection.execute("UPDATE jobs SET receipt_json=? WHERE job_id=?", (canonical({"publish_id": self.f.bundle["publish_id"]}).decode(), job_id))
        with self.assertRaises(ContractError):
            self.reconcile()
        self.assertEqual(self.state(), "fulfilled")

    def test_pause_blocks_new_fulfilment_and_event_fault_rolls_back_all_delivery_writes(self):
        self.published()
        self.store.control("collection", "pause")
        self.assertEqual(self.reconcile()["status"], "blocked")
        self.assertEqual(self.state(), "queued")
        self.assertIsNone(self.store.connection.execute("SELECT name FROM sqlite_master WHERE name='derived_image_region_deliveries'").fetchone())
        self.store.control("collection", "resume")
        before = self.immutable()
        original = self.store.event
        def fail(kind, *args, **kwargs):
            if kind == "image_region_delivered":
                raise RuntimeError("synthetic atomic delivery fault")
            return original(kind, *args, **kwargs)
        with patch.object(self.store, "event", side_effect=fail), self.assertRaisesRegex(RuntimeError, "atomic delivery fault"):
            self.reconcile()
        self.assertEqual(self.state(), "queued")
        self.assertIsNone(self.store.connection.execute("SELECT name FROM sqlite_master WHERE name='derived_image_region_deliveries'").fetchone())
        self.assertEqual(before, self.immutable())
        self.assertEqual(self.reconcile()["status"], "fulfilled")
        self.store.control("collection", "pause")
        self.assertTrue(self.reconcile()["idempotent"])
        self.assertEqual(before, self.immutable())


if __name__ == "__main__":
    unittest.main()
