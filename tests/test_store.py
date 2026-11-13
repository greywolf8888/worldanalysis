"""Synthetic interface/adversarial tests; never inspect old materials or APIs."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from worldanalysis.contracts import ContractError, hash_file
from worldanalysis.store import Store


def text_extract(path, output_dir, options=None):
    return {"media_type":"text/plain","provider_id":"synthetic-text","provider_version":"1",
        "blocks":[{"kind":"paragraph","origin":"native_extract","locator":{"kind":"paragraph","precision":"exact","paragraph_index":0},"text":path.read_text(encoding="utf-8"),"file_keys":[],"processing_status":"extracted","reason":"Synthetic actual text read"}],
        "files":[],"gaps":[],"metrics":[],"enumeration_state":"complete","processing_status":"verified_complete","metadata":{"sample":"synthetic"}}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: self.store.close())
        self.project = self.store.create_project(author="A",topic="T")
        self.input = self.root / "two-lines.txt"
        self.input.write_text("First instruction.\nSecond prerequisite.",encoding="utf-8")

    def ingest(self, extractor=text_extract):
        return self.store.ingest(self.project,[self.input],extractor)

    def test_default_single_sqlite_and_readonly_copy(self):
        before = hash_file(self.input)
        bundle = self.ingest()
        self.assertEqual(self.store.db_path,self.root / "整理结果" / "worldanalysis.sqlite3")
        self.assertEqual(hash_file(self.input),before)
        original = next(a for a in bundle["artifacts"] if a["kind"] == "original_small_file")
        copy = self.store.project_root(self.project) / original["relative_path"]
        self.assertEqual(hash_file(copy),before)
        self.assertFalse(copy.stat().st_mode & stat.S_IWRITE)
        self.assertEqual(bundle["blocks"][0]["text"],self.input.read_text())
        self.assertEqual(bundle["coverage"]["claimed_status"],"verified_complete")

    def test_identity_unknown_does_not_merge_and_authors_isolated(self):
        another = self.store.create_project(author="B",topic="T")
        uncertain = self.store.create_project(author="A",topic="T")
        self.assertNotEqual(another,self.project)
        self.assertNotEqual(uncertain,self.project)
        with self.assertRaises(ContractError):
            self.store.create_project(project_id=self.project,author="other",topic="T")

    def test_repeated_ingest_and_restart_are_idempotent(self):
        first = self.ingest()
        second = self.ingest()
        self.assertEqual(first["publish_id"],second["publish_id"])
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.get_bundle(self.project)["publish_id"],first["publish_id"])
        self.assertEqual(len(self.store.list_bundles(self.project)),1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='bundle_published'").fetchone()[0],1)

    def test_same_parser_config_reuses_evidence_but_new_version_is_new_job(self):
        calls = []
        def counted(path,out,options=None):
            calls.append(1)
            return text_extract(path,out,options)
        first = self.store.ingest(self.project,[self.input],counted,config_version="synthetic-config-v1")
        again = self.store.ingest(self.project,[self.input],counted,config_version="synthetic-config-v1")
        self.assertEqual(first["publish_id"],again["publish_id"])
        self.assertEqual(len(calls),1)
        second = self.store.ingest(self.project,[self.input],counted,config_version="synthetic-config-v2")
        self.assertNotEqual(first["publish_id"],second["publish_id"])
        self.assertEqual(len(calls),2)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],2)

    def test_exact_multi_input_set_reuses_jobs_despite_elapsed_metadata(self):
        calls = []
        def timed(path,out,options=None):
            calls.append(path.name)
            raw = text_extract(path,out,options)
            raw["metadata"] = {"elapsed_seconds":len(calls)*0.1234567}
            return raw
        second = self.root/"second.txt"
        second.write_text("Different actual source.",encoding="utf-8")
        first = self.store.ingest(self.project,[self.input,second],timed,config_version="multi-v1")
        self.assertEqual(len(calls),2)
        repeated = self.store.ingest(self.project,[self.input,second],timed,config_version="multi-v1")
        reversed_set = self.store.ingest(self.project,[second,self.input],timed,config_version="multi-v1")
        self.assertEqual(len(calls),2)
        self.assertEqual(first["bundle_id"],repeated["bundle_id"])
        self.assertEqual(first["bundle_id"],reversed_set["bundle_id"])
        self.assertEqual(len(self.store.list_bundles()),1)
        changed_config = self.store.ingest(self.project,[self.input,second],timed,config_version="multi-v2")
        self.assertEqual(len(calls),4)
        self.assertNotEqual(first["bundle_id"],changed_config["bundle_id"])

    def test_asset_revision_retains_old_source_version(self):
        first = self.ingest()
        self.input.write_text("Different source revision.",encoding="utf-8")
        second = self.ingest()
        self.assertEqual(first["assets"][0]["asset_id"],second["assets"][0]["asset_id"])
        self.assertNotEqual(first["assets"][0]["asset_revision"],second["assets"][0]["asset_revision"])
        self.assertEqual(self.store.get_bundle(self.project,first["bundle_id"])["bundle_id"],first["bundle_id"])

    def test_same_bytes_renamed_reuses_asset_within_project(self):
        first = self.ingest()
        other = self.root / "renamed.txt"
        other.write_bytes(self.input.read_bytes())
        second = self.store.ingest(self.project,[other],text_extract)
        self.assertEqual(first["assets"][0]["asset_id"],second["assets"][0]["asset_id"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0],2)

    def test_manifest_tamper_cannot_be_consumed(self):
        bundle = self.ingest()
        path = self.store.project_root(self.project) / bundle["publication"]["manifest_relative_path"]
        path.chmod(stat.S_IREAD|stat.S_IWRITE)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["blocks"][0]["text"] = "invented"
        path.write_text(json.dumps(doc),encoding="utf-8")
        with self.assertRaises(ContractError):
            self.store.get_bundle(self.project)
        self.store.recover_publications()
        self.assertEqual(self.store.list_bundles()[0]["state"],"quarantined")

    def test_file_tamper_and_unmanifested_file_cannot_be_consumed(self):
        bundle = self.ingest()
        art = next(a for a in bundle["artifacts"] if a["kind"] == "native_text")
        path = self.store.project_root(self.project) / art["relative_path"]
        path.chmod(stat.S_IREAD|stat.S_IWRITE)
        path.write_text("tampered",encoding="utf-8")
        with self.assertRaises(ContractError):
            self.store.get_bundle(self.project)

    def test_unmanifested_extra_file_rejected(self):
        bundle = self.ingest()
        (self.store.project_root(self.project) / bundle["publication"]["manifest_relative_path"]).with_name("unlisted.txt").write_text("extra")
        with self.assertRaises(ContractError):
            self.store.get_bundle(self.project)

    def test_crash_after_rename_before_db_commit_recovers_same_publish_id(self):
        def crash(publish_id):
            raise RuntimeError("simulated crash after rename")
        self.store._after_rename = crash
        with self.assertRaisesRegex(RuntimeError,"simulated"):
            self.ingest()
        row = self.store.connection.execute("SELECT * FROM publications").fetchone()
        self.assertEqual(row["state"],"validated")
        self.assertTrue((self.store.project_root(self.project)/row["final_relative_path"]).is_dir())
        with self.assertRaises(ContractError):
            self.store.get_bundle(self.project)
        ident = row["publish_id"]
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.get_bundle(self.project)["publish_id"],ident)
        self.assertEqual(len(self.store.list_bundles()),1)
        self.store.recover_publications()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='bundle_published'").fetchone()[0],1)

    def test_published_missing_file_quarantined_on_restart(self):
        bundle = self.ingest()
        path = self.store.project_root(self.project) / bundle["publication"]["manifest_relative_path"]
        path.chmod(stat.S_IREAD|stat.S_IWRITE)
        path.unlink()
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.list_bundles()[0]["state"],"quarantined")

    def test_orphan_directory_is_never_imported(self):
        orphan = self.store.project_root(self.project)/"证据"/"v9999"
        orphan.mkdir(parents=True)
        (orphan/"manifest.json").write_text("{}")
        self.store.recover_publications()
        self.assertEqual(self.store.list_bundles(),[])

    def test_independent_controls_persist(self):
        self.store.control("collection","pause")
        self.assertEqual(self.ingest()["status"],"blocked")
        self.assertEqual(self.store.pipeline_state("analysis"),"running")
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.pipeline_state("collection"),"paused")
        self.store.control("collection","resume")
        self.assertIn("bundle_id",self.ingest())

    def test_large_file_rejected_before_hash(self):
        self.store.max_file_bytes = 1
        with patch("worldanalysis.store.hash_file",side_effect=AssertionError("must not hash")):
            with self.assertRaisesRegex(ContractError,"small-file"):
                self.ingest()

    def test_media_route_explicitly_blocked_without_hash_or_extractor(self):
        with patch("worldanalysis.store.hash_file",side_effect=AssertionError("must not hash media")):
            result = self.store.ingest_media(self.project,[self.input])
        self.assertEqual(result["status"],"blocked")

    def test_unknown_cost_reservation_and_default_permission_fail_closed(self):
        provider = self.store.register_provider("synthetic-provider","1")
        unknown = self.store.queue_job(self.project,"request_generation",provider,"source-test",estimated_cost_minor=None,max_cost_minor=100)
        self.assertFalse(self.store.begin_submission(unknown["job_id"])["may_send"])
        known = self.store.queue_job(self.project,"request_generation",provider,"source-other",estimated_cost_minor=10,max_cost_minor=100)
        self.assertFalse(self.store.begin_submission(known["job_id"])["may_send"])
        self.assertEqual(self.store.connection.execute("SELECT reserved_cost_minor FROM jobs WHERE job_id=?",(unknown["job_id"],)).fetchone()[0],0)

    def test_submission_unknown_is_not_resent_and_reservation_survives_restart(self):
        self.store.submission_authorizer = lambda job:{"state":"granted","grant_ref":"synthetic-test-only","job_id":job["job_id"],"project_id":job["project_id"]}
        provider = self.store.register_provider("synthetic-provider","1")
        job = self.store.queue_job(self.project,"request_generation",provider,"source-test",estimated_cost_minor=10,max_cost_minor=100)
        first = self.store.begin_submission(job["job_id"])
        self.assertTrue(first["may_send"])
        self.assertFalse(self.store.begin_submission(job["job_id"])["may_send"])
        self.store.close()
        self.store = Store(self.root)
        self.assertFalse(self.store.begin_submission(job["job_id"])["may_send"])
        row = self.store.reconcile_job(job["job_id"],state="submission_unknown",receipt={"query":"unknown"})
        self.assertEqual(row["reserved_cost_minor"],10)
        with self.assertRaises(ContractError):
            self.store.reconcile_job(job["job_id"],state="queued",receipt={"bad":"resubmit"})

    def test_publish_rejects_foreign_wrong_operation_revision_provider_or_queued_job(self):
        other = self.store.create_project(topic="foreign")
        base = self.store._register_input(self.project,self.input)
        out = self.root/"manual-extraction"
        out.mkdir()
        raw = text_extract(self.input,out)
        provider = self.store.register_provider(raw["provider_id"],raw["provider_version"])
        foreign_provider = self.store.register_provider("different-provider","1")
        ref = base["asset"]
        exact = ref["asset_id"]+":"+ref["asset_revision"]
        cases = [(other,"request_generation",exact,provider,"queued"),(self.project,"request_generation",exact,provider,"running"),(self.project,"native_extract","wrong-revision",provider,"running"),(self.project,"native_extract",exact,foreign_provider,"running"),(self.project,"native_extract",exact,provider,"queued")]
        for index,(project,operation,revision,p,state) in enumerate(cases):
            with self.subTest(case=index):
                job = self.store.queue_job(project,operation,p,revision,config_version=f"case-{index}",estimated_cost_minor=0)
                self.store.connection.execute("UPDATE jobs SET state=? WHERE job_id=?",(state,job["job_id"]))
                entry = {**base,"raw":raw,"output_dir":out,"job_id":job["job_id"]}
                with self.assertRaisesRegex(ContractError,"job"):
                    self.store.publish(self.project,[entry])
                unchanged = self.store.connection.execute("SELECT project_id,state FROM jobs WHERE job_id=?",(job["job_id"],)).fetchone()
                self.assertEqual(tuple(unchanged),(project,state))
        self.assertEqual(self.store.list_bundles(),[])

    def test_recovery_rechecks_job_project_before_marking_complete(self):
        other = self.store.create_project(topic="other")
        def corrupt_after_rename(publish_id):
            self.store.connection.execute("UPDATE jobs SET project_id=? WHERE operation='native_extract'",(other,))
            raise RuntimeError("crash with tampered job binding")
        self.store._after_rename = corrupt_after_rename
        with self.assertRaises(RuntimeError):
            self.ingest()
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.list_bundles()[0]["state"],"quarantined")
        job = self.store.connection.execute("SELECT project_id,state FROM jobs WHERE operation='native_extract'").fetchone()
        self.assertEqual(tuple(job),(other,"running"))

    def test_raw_escape_and_undeclared_output_rejected(self):
        def bad(path,out,options=None):
            result = text_extract(path,out)
            (out/"stray.txt").write_text("real but not listed")
            return result
        with self.assertRaisesRegex(ContractError,"exactly enumerate"):
            self.ingest(bad)

    def test_raw_symlink_rejected_when_supported(self):
        def bad(path,out,options=None):
            result = text_extract(path,out)
            try:
                (out/"link.txt").symlink_to(self.input)
            except OSError:
                self.skipTest("OS does not grant symlink creation")
            result["files"] = [{"key":"link","relative_path":"link.txt","kind":"native_text","media_type":"text/plain"}]
            return result
        with self.assertRaises(ContractError):
            self.ingest(bad)

    def test_literal_contact_retains_value_and_unknown_attribution(self):
        self.input.write_text("微信：synthetic_support\nEmail: tutor@example.com\n",encoding="utf-8")
        bundle = self.ingest()
        self.assertEqual(len(bundle["contacts"]),2)
        for contact in bundle["contacts"]:
            self.assertEqual(contact["role"],"unknown")
            self.assertEqual(contact["classification"],"contact")
            self.assertEqual(contact["disposition"],"restricted_local")
            self.assertEqual(contact["evidence_refs"][0]["block_id"],bundle["blocks"][0]["block_id"])
            value = next(a for a in bundle["artifacts"] if a["artifact_id"] == contact["value_artifact_id"])
            self.assertEqual(value["access_class"],"restricted_local")
            self.assertTrue((self.store.project_root(self.project)/value["relative_path"]).read_text())

    def test_actual_document_raw_interface_preserves_placement_metadata(self):
        from worldanalysis.documents import extract
        self.input.write_bytes("两句合成说明。\r\n微信：synthetic_support\r\n".encode("utf-8"))
        bundle = self.ingest(extract)
        self.assertEqual(len(bundle["contacts"]),1)
        metadata = [a for a in bundle["artifacts"] if "/metadata/" in a["relative_path"]]
        self.assertEqual(len(metadata),1)
        self.assertEqual(hash_file(self.store.project_root(self.project)/metadata[0]["relative_path"])[0],metadata[0]["sha256"])

    def test_restricted_credentials_preserved_not_leaked(self):
        self.input.write_text("safe instruction\nCookie: BDUSS=synthetic-secret\n",encoding="utf-8")
        bundle = self.ingest()
        self.assertNotIn("BDUSS=synthetic-secret",json.dumps(bundle))
        original = next(a for a in bundle["artifacts"] if a["kind"] == "original_small_file")
        self.assertEqual(original["access_class"],"restricted_local")
        self.assertIn("BDUSS=synthetic-secret",(self.store.project_root(self.project)/original["relative_path"]).read_text())


if __name__ == "__main__":
    unittest.main()
