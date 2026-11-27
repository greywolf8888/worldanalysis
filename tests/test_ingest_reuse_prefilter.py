"""Warm exact reuse with real document extraction in workspace-local tempDBs only."""
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from worldanalysis import documents
from worldanalysis.contracts import ContractError
from worldanalysis.store import Store


class IngestReusePrefilterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            prefix=".ingest-reuse-prefilter-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.project = self.store.create_project(topic="Synthetic warm reuse prefilter")
        self.a, self.b = self.root / "A.txt", self.root / "B.txt"
        self.a.write_text("Synthetic source A.", encoding="utf-8")
        self.b.write_text("Synthetic unrelated source B.", encoding="utf-8")
        self.first = self.ingest([self.a])
        self.later = self.ingest([self.b])

    def ingest(self, paths, config="reuse-prefilter-v1"):
        return self.store.ingest(self.project, paths, documents.extract, config_version=config)

    def identities(self):
        return {table: [tuple(row) for row in self.store.connection.execute(
                    "SELECT * FROM " + table + " ORDER BY 1")]
                for table in ("jobs", "providers", "provider_configurations", "publications")}

    def reuse_only(self, expected, paths, *, error=None):
        before = self.identities()
        original = self.store.get_bundle

        def only_matching(project_id, bundle_id=None):
            self.assertEqual((project_id, bundle_id), (self.project, expected["bundle_id"]),
                             "unrelated publication reached full get_bundle verification")
            return original(project_id, bundle_id)

        with patch.object(self.store, "get_bundle", side_effect=only_matching) as verified, \
                patch.object(documents, "_text", wraps=documents._text) as extracted:
            if error:
                with self.assertRaisesRegex(ContractError, error):
                    self.ingest(paths)
                result = None
            else:
                result = self.ingest(paths)
                self.assertEqual(result, expected)
            verified.assert_called_once_with(self.project, expected["bundle_id"])
            extracted.assert_not_called()
        self.assertEqual(self.identities(), before, "warm reuse changed jobs/provider/config/publications")
        return result

    def test_older_exact_bundle_skips_newer_unrelated_bundle(self):
        self.assertNotEqual(self.first["bundle_id"], self.later["bundle_id"])
        self.reuse_only(self.first, [self.a])

    def test_same_assets_with_different_jobs_are_not_candidates(self):
        changed = self.ingest([self.a], config="reuse-prefilter-v2")
        self.assertEqual(changed["assets"], self.first["assets"])
        self.assertNotEqual({a["job_id"] for a in changed["artifacts"]},
                            {a["job_id"] for a in self.first["artifacts"]})
        self.reuse_only(self.first, [self.a])

    def test_superset_is_skipped_and_reordered_exact_set_reuses(self):
        mixed = self.ingest([self.a, self.b])
        self.reuse_only(self.first, [self.a])
        self.reuse_only(mixed, [self.b, self.a])

    def test_matching_artifact_tamper_still_fails_full_verification(self):
        artifact = next(a for a in self.first["artifacts"] if a["kind"] == "native_text")
        path = self.store.project_root(self.project) / artifact["relative_path"]
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
        path.write_text("Synthetic tampered artifact.", encoding="utf-8")
        self.reuse_only(self.first, [self.a], error="hash mismatch")

    def test_matching_ledger_graph_tamper_is_not_swallowed(self):
        self.store.connection.execute(
            "UPDATE publications SET bundle_json=json_set(bundle_json,'$.blocks[0].text',?) "
            "WHERE bundle_id=?", ("Synthetic tampered graph.", self.first["bundle_id"]))
        self.reuse_only(self.first, [self.a], error="identity/manifest mismatch")


if __name__ == "__main__":
    unittest.main()
