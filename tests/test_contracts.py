import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import stat

from worldanalysis.contracts import (ContractError, interval_union, safe_path,
    scan_secrets, reject_links, validate_bundle, validate_locator, validate_plan,
    validate_relative_path, validate_schema)
from worldanalysis.store import Store


def extract(path,out,options=None):
    return {"media_type":"text/plain","provider_id":"synthetic-contract-test","provider_version":"1","blocks":[{"kind":"paragraph","origin":"native_extract","locator":{"kind":"paragraph","precision":"exact","paragraph_index":0},"text":path.read_text(),"file_keys":[],"processing_status":"extracted","reason":"Synthetic content"}],"files":[],"gaps":[],"metrics":[],"enumeration_state":"complete","processing_status":"verified_complete","metadata":{}}


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.project = self.store.create_project(topic="synthetic")
        source = self.root/"input.txt"
        source.write_text("real short text")
        self.bundle = self.store.ingest(self.project,[source],extract)
        self.project_root = self.store.project_root(self.project)

    def test_original_schema_offline_and_formats_are_enabled(self):
        validate_schema("bundle",self.bundle)
        doc = copy.deepcopy(self.bundle)
        doc["created_at"] = "2026-11-13 12:00:00"
        with self.assertRaises(ContractError):
            validate_schema("bundle",doc)
        doc["created_at"] = "2026-11-13T12:00:00"
        with self.assertRaises(ContractError):
            validate_schema("bundle",doc)

    def test_offline_unknown_ref_never_fetches_and_reparse_is_rejected(self):
        from worldanalysis.contracts import SCHEMA_DIR
        local = self.root/"offline-schemas"
        local.mkdir()
        for name in ("bundle","plan","analysis"):
            schema = json.loads((SCHEMA_DIR/(name+".schema.json")).read_text(encoding="utf-8"))
            if name == "plan":
                schema = {"$schema":schema["$schema"],"$id":schema["$id"],"$ref":"https://must-not-fetch.invalid/remote.json"}
            (local/(name+".schema.json")).write_text(json.dumps(schema),encoding="utf-8")
        with patch("socket.socket",side_effect=AssertionError("network forbidden")):
            with self.assertRaisesRegex(ContractError,"offline registry"):
                validate_schema("plan",{},local)
        with patch.object(Path,"lstat",return_value=SimpleNamespace(st_mode=stat.S_IFREG,st_file_attributes=0x400)):
            with self.assertRaisesRegex(ContractError,"reparse"):
                reject_links(self.root/"junction")

    def test_relative_refs_plan_and_provider_registration(self):
        row = self.store.connection.execute("SELECT plan_json FROM plans").fetchone()
        plan = json.loads(row[0])
        validate_schema("plan",plan)
        validate_plan(plan,registered_providers={("synthetic-contract-test","1")})
        with self.assertRaisesRegex(ContractError,"not registered"):
            validate_plan(plan,registered_providers=set())

    def test_duplicate_ids_and_wrong_project_evidence(self):
        doc = copy.deepcopy(self.bundle)
        doc["blocks"].append(copy.deepcopy(doc["blocks"][0]))
        with self.assertRaisesRegex(ContractError,"duplicate"):
            validate_bundle(doc,self.project_root)
        doc = copy.deepcopy(self.bundle)
        block = doc["blocks"][0]
        block["derived_from"] = [{"project_id":"wrong-project",**block["asset_ref"],"locator":block["locator"]}]
        with self.assertRaisesRegex(ContractError,"cross-project"):
            validate_bundle(doc,self.project_root)

    def test_forged_coverage_counts_and_entity_sets(self):
        for change in ("blocks","visual_occurrences"):
            doc = copy.deepcopy(self.bundle)
            doc["coverage"]["discovered_counts"][change] += 1
            with self.assertRaisesRegex(ContractError,"sets/counts"):
                validate_bundle(doc,self.project_root)
        doc = copy.deepcopy(self.bundle)
        doc["coverage"]["per_asset"] = []
        with self.assertRaisesRegex(ContractError,"set mismatch"):
            validate_bundle(doc,self.project_root)

    def test_interval_union_not_sum_of_overlapping_windows(self):
        self.assertEqual(interval_union([(0,1000),(500,1500),(1500,2000)]),2000)
        doc = copy.deepcopy(self.bundle)
        doc["coverage"]["per_asset"][0]["metrics"].append({"unit":"millisecond","expected":2000,"observed":2000,"processed":2000,"excluded":0,"method":"full_audio_windows","ranges":[{"kind":"time","precision":"exact","start_ms":0,"end_ms":1500},{"kind":"time","precision":"exact","start_ms":1000,"end_ms":2000}]})
        validate_bundle(doc,self.project_root)
        doc["coverage"]["per_asset"][0]["metrics"][-1]["processed"] = 2500
        doc["coverage"]["per_asset"][0]["metrics"][-1]["observed"] = 3000
        doc["coverage"]["per_asset"][0]["metrics"][-1]["expected"] = 3000
        with self.assertRaisesRegex(ContractError,"interval union"):
            validate_bundle(doc,self.project_root)

    def test_reversed_locator_and_bad_geometry(self):
        with self.assertRaises(ContractError):
            validate_locator({"kind":"time","precision":"exact","start_ms":10,"end_ms":5})
        g = {"reference_object_ref":"image-real","coordinate_space":"original","unit":"pixel","origin":"top_left","frame_size":{"width":100,"height":100},"original_size":{"width":100,"height":100,"unit":"pixel"},"rotation_degrees":0,"bbox_xywh":[1,2,10,20],"transform_to_original":{"method":"identity","operations":[]}}
        locator = {"kind":"page","precision":"exact","page_number":1,"geometry":g}
        validate_locator(locator,{"image-real":{}})
        g["bbox_xywh"] = [90,90,20,20]
        with self.assertRaisesRegex(ContractError,"outside"):
            validate_locator(locator,{"image-real":{}})
        g["bbox_xywh"] = [1,2,10,20]
        g["coordinate_space"] = "derived"
        with self.assertRaisesRegex(ContractError,"nonidentity"):
            validate_locator(locator,{"image-real":{}})
        g["transform_to_original"] = {"method":"matrix_3x3","operations":["rotate"],"matrix":[0,0,0,0,0,0,0,0,0]}
        with self.assertRaisesRegex(ContractError,"singular"):
            validate_locator(locator,{"image-real":{}})

    def test_path_escape_windows_ambiguity_and_symlink(self):
        for path in ("../outside","a/../b","/absolute","C:/absolute","a\\b","a//b","a/./b","a.txt:ads","NUL.txt","dir/file. ","a\x00b"):
            with self.subTest(path=path):
                with self.assertRaises(ContractError):
                    validate_relative_path(path)
        outside = self.root/"outside.txt"
        outside.write_text("outside")
        try:
            (self.project_root/"link").symlink_to(outside)
        except OSError:
            return  # Windows without SeCreateSymbolicLinkPrivilege.
        with self.assertRaises(ContractError):
            safe_path(self.project_root,"link")

    def test_secrets_free_text_and_signed_urls_rejected(self):
        for text in ("Cookie: a=b", "Authorization: Bearer syntheticlongsecret", "https://example.com/file?signature=abcdef", "https://example.com/file?access_token=abc", "https://user:password@example.com/read"):
            with self.subTest(text=text):
                with self.assertRaises(ContractError):
                    scan_secrets({"prose":text})
        scan_secrets("https://example.com/document?v=1#section")

    def test_plan_dag_and_budget_parent_limits(self):
        plan = json.loads(self.store.connection.execute("SELECT plan_json FROM plans").fetchone()[0])
        job = plan["asset_plans"][0]["jobs"][0]
        job["depends_on_job_ids"] = [job["job_id"]]
        with self.assertRaisesRegex(ContractError,"cyclic"):
            validate_plan(plan)
        job["depends_on_job_ids"] = []
        job["budget"] = copy.deepcopy(job["budget"])
        job["budget"]["max_concurrency"] += 1
        with self.assertRaisesRegex(ContractError,"hard budget"):
            validate_plan(plan)

    def test_remote_mutation_needs_unknown_submission_policy(self):
        plan = json.loads(self.store.connection.execute("SELECT plan_json FROM plans").fetchone()[0])
        job = plan["asset_plans"][0]["jobs"][0]
        job["operation"] = "request_generation"
        job["side_effect"] = "remote_mutation"
        with self.assertRaises(ContractError):
            validate_schema("plan",plan)


if __name__ == "__main__":
    unittest.main()
