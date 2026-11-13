"""Actual Store.ingest hook expectations, not helper-harness success claims.

Until the coordinator hooks Cache.restore/remember these tests intentionally fail.
Every source, output and business SQLite is synthetic and in OS temp.
"""
import json
from pathlib import Path
import tempfile
import unittest

from worldanalysis.contracts import ContractError
from worldanalysis.extraction_cache import Cache
from worldanalysis.store import Store


class IncrementalIngestHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="worldanalysis-incremental-hook-")
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.store.close())
        self.project = self.store.create_project(topic="Synthetic ingest hook")
        Cache(self.store)
        self.a, self.b = self.root / "A.txt", self.root / "B.txt"
        self.a.write_text("Synthetic A", encoding="utf-8")
        self.b.write_text("Synthetic B", encoding="utf-8")
        self.calls = []
        def extractor(path, outdir, options=None):
            text = path.read_text(encoding="utf-8")
            self.calls.append(text)
            return {"media_type": "text/plain", "provider_id": extractor.provider_id,
                    "provider_version": extractor.provider_version,
                    "blocks": [{"kind": "paragraph", "origin": "native_extract",
                        "locator": {"kind": "paragraph", "precision": "exact", "paragraph_index": 0},
                        "text": text, "file_keys": [], "processing_status": "extracted", "reason": "Synthetic actual read"}],
                    "files": [], "gaps": [], "metrics": [], "enumeration_state": "complete",
                    "processing_status": "verified_complete", "metadata": {"synthetic": True, "elapsed": len(self.calls) * 0.777}}
        extractor.provider_id = "synthetic-incremental-hook"
        extractor.provider_version = "1"
        self.extractor = extractor

    def ingest(self, paths, config="hook-v1"):
        return self.store.ingest(self.project, paths, self.extractor, config_version=config)

    def test_callable_config_fingerprint_ignores_live_constant_reference_counts(self):
        from worldanalysis.store import _config_value
        def extractor_identity_sample():
            return {"synthetic-nonidentifier-key": "synthetic-noninterned-value"}
        before = _config_value(extractor_identity_sample)
        pinned_constants = list(extractor_identity_sample.__code__.co_consts)
        self.assertEqual(before, _config_value(extractor_identity_sample))
        self.assertTrue(pinned_constants)

    def test_actual_ingest_only_new_or_revised_input_is_parsed(self):
        self.ingest([self.a])
        first_mixed = self.ingest([self.a, self.b])
        self.assertEqual(self.calls, ["Synthetic A", "Synthetic B"])
        reordered = self.ingest([self.b, self.a])
        self.assertEqual(first_mixed["bundle_id"], reordered["bundle_id"])
        self.assertEqual(len(self.calls), 2)
        self.b.write_text("Synthetic B revised", encoding="utf-8")
        self.ingest([self.b, self.a])
        self.assertEqual(self.calls, ["Synthetic A", "Synthetic B", "Synthetic B revised"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM extraction_cache").fetchone()[0], 3)

    def test_actual_ingest_provider_or_config_changes_create_jobs(self):
        self.ingest([self.a])
        self.ingest([self.a, self.b])
        self.assertEqual(len(self.calls), 2)
        self.ingest([self.a, self.b], config="hook-v2")
        self.assertEqual(len(self.calls), 4)
        self.extractor.provider_version = "2"
        self.ingest([self.a, self.b], config="hook-v2")
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 6)

    def test_actual_mixed_ingest_rejects_damaged_cache_without_reextract(self):
        self.ingest([self.a])
        row = self.store.connection.execute("SELECT job_id FROM extraction_cache").fetchone()
        self.assertIsNotNone(row, "Store must remember published native raw before mixed-set reuse")
        with self.store.transaction():
            self.store.connection.execute("UPDATE extraction_cache SET payload_json='{}' WHERE job_id=?", (row[0],))
        with self.assertRaises(ContractError):
            self.ingest([self.a, self.b])
        self.assertEqual(self.calls, ["Synthetic A"])

    def test_actual_ingest_reuses_raw_after_restart(self):
        self.ingest([self.a])
        self.store.close()
        self.store = Store(self.root)
        self.ingest([self.a, self.b])
        self.assertEqual(self.calls, ["Synthetic A", "Synthetic B"])
        rows = list(self.store.connection.execute("SELECT payload_json FROM extraction_cache"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(json.loads(row[0])["raw"]["metadata"]["elapsed"] for row in rows), [0.777, 1.554])


if __name__ == "__main__":
    unittest.main()
