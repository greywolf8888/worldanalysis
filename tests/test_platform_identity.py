"""Focused identity/compatibility checks; all SQLite state is temporary."""
import tempfile
import unittest
from pathlib import Path

from worldanalysis.contracts import ContractError, digest
from worldanalysis.platform_identity import content_identity, material_content, queue_content_job
from worldanalysis.store import Store


class PlatformIdentityTests(unittest.TestCase):
    def test_content_identity_ignores_location_not_actual_content_or_account(self):
        item = {"file_id": "101", "source_revision": "source-v1", "size_bytes": 100,
                "path": "/apps/教育分析学习/待分析/一.mp4", "duration_ms": 120000}
        key = content_identity("account-a", item)
        moved = {**item, "path": "/apps/教育分析学习/已分析/一.mp4", "duration_ms": 120001}
        self.assertEqual(key, content_identity("account-a", moved))
        self.assertNotEqual(key, content_identity("account-b", item))
        for field, value in (("file_id", "102"), ("source_revision", "source-v2"), ("size_bytes", 101)):
            with self.subTest(field=field):
                self.assertNotEqual(key, content_identity("account-a", {**item, field: value}))

    def test_material_location_only_is_nonsemantic(self):
        value = {"source": {"provider": "baidu", "account_ref": "a", "file_id": "101", "path": "/old"},
                 "attachments": [{"sha256": "abc", "size_bytes": 3}]}
        moved = {**value, "source": {**value["source"], "path": "/new"}}
        self.assertEqual(material_content(value), material_content(moved))
        updated = {**moved, "attachments": [{"sha256": "def", "size_bytes": 3}]}
        self.assertNotEqual(material_content(value), material_content(updated))

    def test_inflight_legacy_job_wins_without_changing_its_revision_and_config_is_not_aliased(self):
        with tempfile.TemporaryDirectory(prefix="platform-identity-", dir=Path(__file__).resolve().parents[1]) as root, Store(root) as store:
            project = store.create_project(topic="Synthetic compatibility fixture")
            provider = store.register_provider("platform-first-generation", "1.0", config_version="platform-first-v1")
            old = store.queue_job(project, "platform-generate-manuscript", provider, "legacy-path-revision", config_version="platform-first-v1")
            store.reconcile_job(old["job_id"], state="submission_unknown", receipt={"evidence_ref": "old-request"})
            stable = digest(["stable", "content"])
            reused = queue_content_job(store, project, "platform-generate-manuscript", provider, stable, legacy_revisions=["legacy-path-revision"])
            self.assertEqual(reused["job_id"], old["job_id"])
            self.assertEqual(reused["input_revision"], "legacy-path-revision")
            self.assertEqual(queue_content_job(store, project, "platform-generate-manuscript", provider, stable)["job_id"], old["job_id"])
            changed_provider = store.register_provider("platform-first-generation", "1.0", config_version="different-config")
            changed = queue_content_job(store, project, "platform-generate-manuscript", changed_provider, stable,
                                        legacy_revisions=["legacy-path-revision"], config_version="different-config")
            self.assertNotEqual(changed["job_id"], old["job_id"])
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)

    def test_independent_config_mismatch_cannot_reuse_candidate_or_alias_and_compatible_inflight_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="platform-identity-config-", dir=Path(__file__).resolve().parents[1]) as root, Store(root) as store:
            project = store.create_project(topic="Synthetic independent task-config boundary")
            provider = store.register_provider("platform-first-generation", "1.0", config_version="config-a")
            operation, revision, legacy = "platform-generate-manuscript", "stable-content", "old-path-content"
            # Store historically permits the same provider with a different
            # independent task config, which is NOT a compatible helper result.
            wrong = store.queue_job(project, operation, provider, legacy, config_version="config-b")
            store.reconcile_job(wrong["job_id"], state="submission_unknown", receipt={"evidence_ref": "old-config-b-request"})
            with self.assertRaisesRegex(ContractError, "config_version must match"):
                queue_content_job(store, project, operation, provider, revision, legacy_revisions=[legacy], config_version="config-b")
            self.assertIsNone(queue_content_job(store, project, operation, provider, revision,
                                               legacy_revisions=[legacy], config_version="config-a", create=False))
            # Simulate an alias left by the old helper bug. Both the alias and
            # ordinary candidates must validate the independent config key.
            key = digest([project, operation, provider, revision, "config-a"])
            store.connection.execute("INSERT INTO platform_task_aliases VALUES(?,?)", (key, wrong["job_id"]))
            self.assertIsNone(queue_content_job(store, project, operation, provider, revision,
                                               legacy_revisions=[legacy], config_version="config-a", create=False))
            correct = store.queue_job(project, operation, provider, legacy, config_version="config-a")
            store.reconcile_job(correct["job_id"], state="submission_unknown", receipt={"evidence_ref": "old-config-a-request"})
            complete = store.queue_job(project, operation, provider, revision, config_version="config-a")
            store.reconcile_job(complete["job_id"], state="verified_complete", receipt={"evidence_ref": "completed-stable-task"})
            before = {row["job_id"]: dict(row) for row in store.connection.execute("SELECT * FROM jobs")}
            reused = queue_content_job(store, project, operation, provider, revision,
                                       legacy_revisions=[legacy], config_version="config-a")
            self.assertEqual(reused["job_id"], correct["job_id"])
            self.assertEqual(reused["state"], "submission_unknown")
            # Validated legacy aliases remain usable without resupplying the
            # old path revision; no job, provider or receipt is rewritten.
            self.assertEqual(queue_content_job(store, project, operation, provider, revision,
                                              config_version="config-a")["job_id"], correct["job_id"])
            after = {row["job_id"]: dict(row) for row in store.connection.execute("SELECT * FROM jobs")}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
