"""Analysis contract tests use synthetic files and a byte-consuming test viewer."""
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import stat
import tempfile
import unittest

from worldanalysis.analysis import Analysis
from worldanalysis.contracts import ContractError, canonical, hash_file, now
from worldanalysis.store import Store


def extract(path,out,options=None):
    blocks = []
    offset = 0
    for i,text in enumerate(path.read_bytes().decode("utf-8").splitlines(keepends=True)):
        blocks.append({"kind":"paragraph","origin":"native_extract","locator":{"kind":"paragraph","precision":"exact","paragraph_index":i,"char_start":offset,"char_end":offset+len(text)},"text":text,"file_keys":[],"processing_status":"extracted","reason":"Actual synthetic source text"})
        offset += len(text)
    return {"media_type":"text/plain","provider_id":"synthetic-analysis-test","provider_version":"1","blocks":blocks,"files":[],"gaps":[],"metrics":[],"enumeration_state":"complete","processing_status":"verified_complete","metadata":{"sample":"synthetic"}}


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.addCleanup(lambda:self.store.close())
        self.project = self.store.create_project(author="Synthetic",topic="Tests")
        self.source = self.root/"source.txt"
        self.source.write_bytes(b"Alpha instruction.\r\nBeta prerequisite.")
        self.bundle = self.store.ingest(self.project,[self.source],extract)
        self.analysis = Analysis(self.store)
        self.task = self.analysis.claim(self.project,model_version="synthetic-test-model-v1")
        self.project_root = self.store.project_root(self.project)

    def result(self,status="completed",statuses=None,read=True):
        processing = []
        for block in self.bundle["blocks"]:
            state = statuses.get(block["block_id"],"read") if statuses else "read"
            refs = []
            if state == "read" and read:
                self.analysis.read(self.task["task_id"],[block["block_id"]],max_chars=100000)
                refs = [r[0] for r in self.store.connection.execute("SELECT receipt_id FROM read_receipts WHERE task_id=? AND block_id=?",(self.task["task_id"],block["block_id"]))]
            processing.append({"block_id":block["block_id"],"asset_ref":block["asset_ref"],"status":state,"priority":"normal","reason":"Synthetic bounded source read" if state == "read" else "Explicit unread limitation","read_receipt_refs":refs,"exclusion":None})
        counts = Counter(p["status"] for p in processing)
        outrel = self.task["output_relative_path"]
        report_rel = outrel+"/report.md"
        report = self.project_root/report_rel
        report.parent.mkdir(parents=True,exist_ok=True)
        report.write_text("# Actual synthetic analysis\n\nThe first source block states the alpha instruction.\n",encoding="utf-8")
        first = next((b for b in self.bundle["blocks"] if next(p for p in processing if p["block_id"] == b["block_id"])["status"] == "read"),self.bundle["blocks"][0])
        ref = {"project_id":self.project,**first["asset_ref"],"block_id":first["block_id"],"locator":first["locator"]}
        return {"schema_version":"1.0-draft","analysis_id":"synthetic-analysis","analysis_revision":"v1","project_id":self.project,"based_bundle_id":self.bundle["bundle_id"],"based_bundle_revision":self.bundle["bundle_revision"],"based_publish_id":self.bundle["publish_id"],"created_at":now(),"execution_status":status,"agent":copy.deepcopy(self.task["agent"]),"block_processing":processing,"coverage":{"scope":"based_bundle_only","source_inventory_state":self.bundle["coverage"]["inventory_state"],"discovered_blocks":len(processing),**{k+"_blocks":counts[k] for k in ("read","failed","pending","excluded","not_applicable")},"claim":"all_discovered_read" if counts["read"] == len(processing) else "partial","limitations":(["Unread blocks and collection image gaps remain; no complete-source claim."] if counts["read"] != len(processing) or self.bundle["gaps"] else [])},"assertions":[{"assertion_id":"assertion-alpha","kind":"extracted_fact","statement":"The source contains an alpha instruction.","evidence_refs":[ref],"external_source_ids":[],"depends_on_assertion_ids":[],"verification_status":"supported","uncertainty":"Synthetic contract test, not a production analysis."}] if counts["read"] else [],"external_sources":[],"gaps":copy.deepcopy(self.bundle["gaps"]),"supplement_requests":[],"main_report_relative_path":report_rel,"reports":[{"report_id":"report-main","kind":"combined_report","relative_path":report_rel,"sha256":hash_file(report)[0]}],"program_validation":{"status":"passed" if status == "completed" else "pending","validator_version":"untrusted-submitter-placeholder","checked_at":now() if status == "completed" else None,"report_relative_path":report_rel if status == "completed" else None}}

    def submit(self,result):
        path = self.project_root/self.task["output_relative_path"]/"submission.json"
        path.write_bytes(canonical(result))
        return self.analysis.complete(self.task["task_id"],path)

    def test_real_text_read_receipts_preserve_crlf_and_can_be_reread(self):
        block = self.bundle["blocks"][0]
        item = self.analysis.read(self.task["task_id"],[block["block_id"]],100)["items"][0]
        self.assertEqual(item["text"],"Alpha instruction.\r\n")
        self.assertEqual(item["receipt"]["total_chars"],block["locator"]["char_end"])
        receipt = self.analysis.receipt(item["receipt"]["receipt_id"])
        self.assertEqual(receipt["content"],item["text"])
        self.assertEqual(receipt["content_sha256"],hashlib.sha256(item["text"].encode()).hexdigest())

    def test_complete_real_hashed_result_and_idempotent_resubmission(self):
        result = self.result()
        accepted = self.submit(result)
        self.assertEqual(accepted["status"],"completed")
        final = self.project_root/accepted["result_relative_path"]
        self.assertEqual(hash_file(final)[0],accepted["result_sha256"])
        doc = json.loads(final.read_text(encoding="utf-8"))
        self.assertEqual(doc["program_validation"]["validator_version"],"worldanalysis-contracts-1")
        main = (self.project_root/doc["main_report_relative_path"]).read_text(encoding="utf-8")
        self.assertIn("Evidence coverage",main)
        self.assertTrue(self.submit(result)["idempotent"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0],1)

    def test_accepted_reports_and_validation_receipt_are_reverified(self):
        accepted = self.submit(self.result())
        result = self.analysis.get_result(self.task["task_id"])
        validation = self.project_root/result["program_validation"]["report_relative_path"]
        validation.chmod(stat.S_IREAD|stat.S_IWRITE)
        validation.write_text("{}",encoding="utf-8")
        with self.assertRaisesRegex(ContractError,"hash mismatch"):
            self.analysis.get_result(self.task["task_id"])

    def test_wrong_and_duplicate_read_block_ids_rejected(self):
        block = self.bundle["blocks"][0]["block_id"]
        for ids in ([block,block],["unknown-block"]):
            with self.assertRaises(ContractError):
                self.analysis.read(self.task["task_id"],ids,100)

    def _fail_analysis_event(self):
        original = self.store.event
        def fail(kind,*args,**kwargs):
            if kind == "analysis_completed":
                raise RuntimeError("injected analysis event transaction failure")
            return original(kind,*args,**kwargs)
        self.store.event = fail
        return original

    def test_analysis_event_failure_same_input_retries_without_rereading(self):
        result = self.result()
        count = self.store.connection.execute("SELECT COUNT(*) FROM read_receipts").fetchone()[0]
        original = self._fail_analysis_event()
        with self.assertRaisesRegex(RuntimeError,"injected"):
            self.submit(result)
        journal = self.store.connection.execute("SELECT * FROM analysis_publications").fetchone()
        self.assertEqual(journal["state"],"renamed")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0],0)
        self.assertEqual(self.store.connection.execute("SELECT state FROM analysis_tasks WHERE task_id=?",(self.task["task_id"],)).fetchone()[0],"running")
        self.store.event = original
        accepted = self.submit(result)
        self.assertTrue(accepted["idempotent"])
        self.assertEqual(accepted["result_sha256"],journal["result_sha256"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM read_receipts").fetchone()[0],count)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='analysis_completed'").fetchone()[0],1)

    def test_analysis_event_failure_recovers_on_store_restart(self):
        result = self.result()
        self._fail_analysis_event()
        with self.assertRaises(RuntimeError):
            self.submit(result)
        self.store.close()
        self.store = Store(self.root)
        self.analysis = Analysis(self.store)
        self.assertEqual(self.analysis.get_result(self.task["task_id"])["execution_status"],"completed")
        self.assertEqual(self.store.connection.execute("SELECT state FROM analysis_publications").fetchone()[0],"committed")
        self.assertTrue(self.submit(result)["idempotent"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0],1)

    def test_journal_rejects_new_input_and_quarantines_output_tamper(self):
        result = self.result()
        original = self._fail_analysis_event()
        with self.assertRaises(RuntimeError):
            self.submit(result)
        self.store.event = original
        changed = copy.deepcopy(result)
        changed["analysis_revision"] = "v2"
        with self.assertRaisesRegex(ContractError,"different input hash"):
            self.submit(changed)
        journal = self.store.connection.execute("SELECT * FROM analysis_publications").fetchone()
        report = self.project_root/json.loads(journal["result_json"])["main_report_relative_path"]
        report.chmod(stat.S_IREAD|stat.S_IWRITE)
        report.write_text("tampered after rename",encoding="utf-8")
        with self.assertRaisesRegex(ContractError,"hash mismatch"):
            self.submit(result)
        self.assertEqual(self.store.connection.execute("SELECT state FROM analysis_publications").fetchone()[0],"quarantined")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0],0)
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(report.read_text(),"tampered after rename")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0],0)

    def test_partial_staging_resumes_snapshots_not_mutable_reports(self):
        import worldanalysis.analysis as module
        result = self.result()
        original_write = module._write
        calls = []
        def fail_second(path,data):
            calls.append(str(path))
            if len(calls) == 2:
                raise OSError("synthetic disk interruption")
            return original_write(path,data)
        with patch.object(module,"_write",side_effect=fail_second):
            with self.assertRaises(OSError):
                self.submit(result)
        self.assertEqual(self.store.connection.execute("SELECT state FROM analysis_publications").fetchone()[0],"staging")
        # Source prose is no longer trusted/read on recovery; the exact already
        # validated output snapshot in SQLite is the only resumed payload.
        (self.project_root/result["main_report_relative_path"]).write_text("changed mutable input report",encoding="utf-8")
        self.store.close()
        self.store = Store(self.root)
        self.analysis = Analysis(self.store)
        accepted = self.analysis.get_result(self.task["task_id"])
        text = (self.project_root/accepted["main_report_relative_path"]).read_text(encoding="utf-8")
        self.assertIn("Actual synthetic analysis",text)
        self.assertNotIn("changed mutable input report",text)

    def test_rename_before_analysis_db_commit_recovers_once(self):
        result = self.result()
        def crash(task_id):
            raise RuntimeError("injected analysis rename crash")
        self.analysis._after_analysis_rename = crash
        with self.assertRaisesRegex(RuntimeError,"rename crash"):
            self.submit(result)
        self.assertEqual(self.store.connection.execute("SELECT state FROM analysis_publications").fetchone()[0],"validated")
        self.store.close()
        self.store = Store(self.root)
        self.analysis = Analysis(self.store)
        self.assertEqual(self.analysis.get_result(self.task["task_id"])["execution_status"],"completed")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='analysis_completed'").fetchone()[0],1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0],1)

    def test_legacy_completion_without_validation_hash_gets_only_additive_index(self):
        accepted = self.submit(self.result())
        task_id = self.task["task_id"]
        # Model an accepted pre-journal revision without changing its actual
        # immutable reports/result/validation bytes or original submitted SHA.
        self.store.connection.execute("DELETE FROM analysis_publications WHERE task_id=?",(task_id,))
        event = self.store.connection.execute("SELECT * FROM events WHERE kind='analysis_completed' AND record_id=?",(task_id,)).fetchone()
        payload = json.loads(event["payload_json"])
        payload.pop("validation_sha256")
        legacy_payload = canonical(payload).decode()
        self.store.connection.execute("UPDATE events SET payload_json=? WHERE event_id=?",(legacy_payload,event["event_id"]))
        report = self.project_root/accepted["main_report_relative_path"]
        result_path = self.project_root/accepted["result_relative_path"]
        before = hash_file(report),hash_file(result_path)
        result = self.analysis.get_result(task_id)
        self.assertEqual(result["execution_status"],"completed")
        self.assertEqual(self.analysis.validation_integrity(task_id)["mode"],"legacy_cross_checked_not_historical_hash")
        index = self.store.connection.execute("SELECT * FROM analysis_validation_indexes WHERE task_id=?",(task_id,)).fetchone()
        self.assertIn("historical_validation_hash_not_recorded",index["provenance"])
        self.assertEqual(before,(hash_file(report),hash_file(result_path)))
        self.assertEqual(self.store.connection.execute("SELECT payload_json FROM events WHERE event_id=?",(event["event_id"],)).fetchone()[0],legacy_payload)
        validation = self.project_root/result["program_validation"]["report_relative_path"]
        validation.chmod(stat.S_IREAD|stat.S_IWRITE)
        validation.write_text("{}",encoding="utf-8")
        with self.assertRaises(ContractError):
            self.analysis.get_result(task_id)
        self.assertEqual(self.store.connection.execute("SELECT state FROM analysis_tasks WHERE task_id=?",(task_id,)).fetchone()[0],"completed")

    def test_legacy_missing_provenance_requires_review_without_fabricated_hash(self):
        self.submit(self.result())
        task_id = self.task["task_id"]
        self.store.connection.execute("DELETE FROM analysis_publications WHERE task_id=?",(task_id,))
        event = self.store.connection.execute("SELECT * FROM events WHERE kind='analysis_completed' AND record_id=?",(task_id,)).fetchone()
        payload = json.loads(event["payload_json"])
        payload.pop("validation_sha256")
        payload.pop("submitted_sha256")
        self.store.connection.execute("UPDATE events SET payload_json=? WHERE event_id=?",(canonical(payload).decode(),event["event_id"]))
        with self.assertRaisesRegex(ContractError,"requires explicit review"):
            self.analysis.get_result(task_id)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM analysis_validation_indexes").fetchone()[0],0)
        self.assertEqual(self.store.connection.execute("SELECT state FROM analysis_tasks WHERE task_id=?",(task_id,)).fetchone()[0],"completed")

    def test_new_model_does_not_reingest_and_duplicate_claims_do_not_requeue(self):
        self.assertEqual(self.analysis.claim(self.project,model_version="synthetic-test-model-v1")["status"],"idle")
        another = self.analysis.claim(self.project,model_version="synthetic-test-model-v2")
        self.assertEqual(another["status"],"running")
        self.assertEqual(another["publish_id"],self.task["publish_id"])
        self.assertNotEqual(another["task_id"],self.task["task_id"])
        self.assertEqual(len(self.store.list_bundles()),1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0],1)

    def test_claim_requires_actual_model_and_api_worker_is_blocked(self):
        self.assertEqual(self.analysis.claim(self.project)["status"],"blocked")
        response = self.analysis.claim(self.project,model_version="unconfigured-model",mode="api_worker")
        self.assertEqual(response["status"],"blocked")
        self.assertIn("not_configured",response["reason"])

    def test_independent_pause_resume_preserves_task(self):
        self.store.control("analysis","pause")
        self.assertEqual(self.analysis.read(self.task["task_id"],[self.bundle["blocks"][0]["block_id"]])["status"],"blocked")
        self.assertEqual(self.store.pipeline_state("collection"),"running")
        self.store.control("analysis","resume")
        self.assertEqual(self.analysis.resume(self.task["task_id"])["task_id"],self.task["task_id"])

    def test_all_pending_cannot_masquerade_as_completed_or_partial_success(self):
        states = {b["block_id"]:"pending" for b in self.bundle["blocks"]}
        for status in ("completed","partial"):
            result = self.result(status,states)
            with self.assertRaises(ContractError):
                self.submit(result)

    def test_missing_duplicate_wrong_project_or_version_rejected(self):
        for mutation in ("missing","duplicate","project","publish","model"):
            with self.subTest(mutation=mutation):
                result = self.result()
                if mutation == "missing":
                    result["block_processing"].pop()
                elif mutation == "duplicate":
                    result["block_processing"].append(copy.deepcopy(result["block_processing"][0]))
                elif mutation == "project":
                    result["project_id"] = "project-other"
                elif mutation == "publish":
                    result["based_publish_id"] = "publish-other"
                else:
                    result["agent"]["model_version"] = "model-other"
                with self.assertRaises(ContractError):
                    self.submit(result)

    def test_unread_and_partial_text_read_not_accepted(self):
        result = self.result(read=False)
        # Schema requires a receipt, but an existing unrelated block receipt
        # is also rejected rather than treated as proof by ID alone.
        other = self.bundle["blocks"][1]
        r = self.analysis.read(self.task["task_id"],[other["block_id"]],1)["items"][0]["receipt"]["receipt_id"]
        for item in result["block_processing"]:
            item["read_receipt_refs"] = [r]
        with self.assertRaises(ContractError):
            self.submit(result)

    def test_text_chunks_must_cover_full_artifact_not_inline_preview(self):
        self.source.write_text("x"*17001,encoding="utf-8")
        self.bundle = self.store.ingest(self.project,[self.source],extract)
        self.task = self.analysis.claim(self.project,model_version="long-text-test")
        block = self.bundle["blocks"][0]
        self.assertEqual(len(block["text"]),16000)
        self.assertIn("preview",block["uncertainty"])
        item = self.analysis.read(self.task["task_id"],[block["block_id"]],16000)["items"][0]
        self.assertEqual(item["status"],"partial_read")
        result = self.result(read=False)
        result["block_processing"][0]["read_receipt_refs"] = [item["receipt"]["receipt_id"]]
        with self.assertRaisesRegex(ContractError,"unread text"):
            self.submit(result)
        tail = self.analysis.read(self.task["task_id"],[block["block_id"]],2000)["items"][0]
        self.assertEqual(len(tail["text"]),1001)
        result["block_processing"][0]["read_receipt_refs"].append(tail["receipt"]["receipt_id"])
        self.assertEqual(self.submit(result)["status"],"completed")

    def test_receipts_from_other_task_and_receipt_tampering_rejected(self):
        result = self.result()
        other = self.analysis.claim(self.project,model_version="another-model")
        foreign = self.analysis.read(other["task_id"],[self.bundle["blocks"][0]["block_id"]],100)["items"][0]["receipt"]["receipt_id"]
        result["block_processing"][0]["read_receipt_refs"] = [foreign]
        with self.assertRaisesRegex(ContractError,"another task"):
            self.submit(result)
        self.store.connection.execute("UPDATE read_receipts SET content='invented' WHERE receipt_id=?",(foreign,))
        with self.assertRaisesRegex(ContractError,"corrupted"):
            self.analysis.receipt(foreign)

    def test_report_tampering_and_secret_leak_rejected(self):
        result = self.result()
        report = self.project_root/result["reports"][0]["relative_path"]
        report.write_text("Changed after manifest hash",encoding="utf-8")
        with self.assertRaisesRegex(ContractError,"report hash"):
            self.submit(result)
        report.write_text("Cookie: synthetic=secret",encoding="utf-8")
        result["reports"][0]["sha256"] = hash_file(report)[0]
        with self.assertRaisesRegex(ContractError,"credential"):
            self.submit(result)

    def test_assertion_wrong_project_or_unread_external_source_rejected(self):
        result = self.result()
        result["assertions"][0]["evidence_refs"][0]["project_id"] = "wrong-project"
        with self.assertRaises(ContractError):
            self.submit(result)
        result = self.result()
        result["assertions"][0]["depends_on_assertion_ids"] = [result["assertions"][0]["assertion_id"]]
        with self.assertRaisesRegex(ContractError,"cyclic"):
            self.submit(result)

    def test_partial_report_shows_unread_and_queues_supplement_persistently(self):
        missing = self.bundle["blocks"][1]
        result = self.result("partial",{missing["block_id"]:"pending"})
        ref = {"project_id":self.project,**missing["asset_ref"],"block_id":missing["block_id"],"locator":missing["locator"]}
        gap = {"gap_id":"synthetic-unread-gap","kind":"unprocessed","severity":"important","status":"open","reason":"Need original text read","target_refs":[ref],"resolution_refs":[]}
        result["gaps"].append(gap)
        result["supplement_requests"] = [{"request_id":"synthetic-supplement","gap_ids":[gap["gap_id"]],"reason":"Read the exact missing block","minimal_targets":[ref],"requested_outputs":["original_text"],"priority":"important","status":"proposed","requires_scope_expansion":False}]
        accepted = self.submit(result)
        self.assertEqual(accepted["status"],"partial")
        text = (self.project_root/accepted["main_report_relative_path"]).read_text(encoding="utf-8")
        self.assertIn(missing["block_id"],text)
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.list_supplements()[0]["state"],"queued")

    def test_post_accepted_supplement_is_proposal_only_and_hash_provenanced(self):
        missing=self.bundle['blocks'][1]
        result=self.result('partial',{missing['block_id']:'pending'})
        ref={'project_id':self.project,**missing['asset_ref'],'block_id':missing['block_id'],'locator':missing['locator']}
        result['gaps'].append({'gap_id':'post-accepted-gap','kind':'unprocessed','severity':'important','status':'open','reason':'Need the bounded original text','target_refs':[ref],'resolution_refs':[]})
        accepted=self.submit(result)
        result_path=self.project_root/accepted['result_relative_path'];before=hash_file(result_path)
        request={'request_id':'post-accepted-request','gap_ids':['post-accepted-gap'],'reason':'Deliver the existing text without rerunning parser','minimal_targets':[ref],'requested_outputs':['original_text'],'priority':'important','status':'proposed','requires_scope_expansion':False}
        queued=self.analysis.request_supplement(self.task['task_id'],request)
        self.assertFalse(queued['executed']);self.assertEqual(queued['status'],'queued_for_collection')
        self.assertEqual(hash_file(result_path),before)
        self.assertEqual(self.analysis.get_result(self.task['task_id'])['execution_status'],'partial')
        event=json.loads(self.store.connection.execute("SELECT payload_json FROM events WHERE kind='supplement_requested' AND record_id=?",(request['request_id'],)).fetchone()[0])
        self.assertEqual(event['task_id'],self.task['task_id']);self.assertEqual(event['publish_id'],self.bundle['publish_id'])
        self.assertEqual(event['request_sha256'],hashlib.sha256(canonical(request)).hexdigest())
        self.analysis.request_supplement(self.task['task_id'],request)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM supplement_requests').fetchone()[0],1)
        denied=copy.deepcopy(request);denied['request_id']='invalid-post-gap';denied['gap_ids']=['not-in-accepted-evidence']
        with self.assertRaises(ContractError):self.analysis.request_supplement(self.task['task_id'],denied)

    def test_not_applicable_cannot_hide_real_evidence(self):
        result = self.result("partial",{self.bundle["blocks"][1]["block_id"]:"not_applicable"})
        with self.assertRaisesRegex(ContractError,"not_applicable"):
            self.submit(result)

    def test_page_render_hidden_block_requires_real_image_open(self):
        from PIL import Image
        def with_page_render(path,out,options=None):
            raw = extract(path,out)
            Image.new("RGB",(4,5),"white").save(out/"page.png")
            raw["files"] = [{"key":"page","relative_path":"page.png","kind":"page_render","media_type":"image/png"}]
            raw["blocks"].append({"kind":"hidden_object","origin":"native_extract","locator":{"kind":"page","precision":"page_only","page_number":1},"file_keys":["page"],"processing_status":"deferred","reason":"Rendered page not semantically read"})
            raw["gaps"] = [{"kind":"semantic_gap","severity":"important","reason":"Page render unread","block_indexes":[len(raw["blocks"])-1]}]
            raw["processing_status"] = "partial"
            return raw
        self.bundle = self.store.ingest(self.project,[self.source],with_page_render)
        self.task = self.analysis.claim(self.project,model_version="synthetic-page-render-test")
        block = self.bundle["blocks"][-1]
        response = self.analysis.read(self.task["task_id"],[block["block_id"]],100)
        self.assertEqual(response["status"],"partial")
        self.assertEqual(response["items"][0]["status"],"requires_image_open")
        self.assertEqual(self.bundle["coverage"]["discovered_counts"]["visual_occurrences"],0)
        opened = self.analysis.open_image(self.task["task_id"],block["block_id"],opener=lambda data,**kw:{"opened":True,"sha256":hashlib.sha256(data).hexdigest(),"viewer_id":"synthetic-byte-viewer","observation":"Actual 4x5 synthetic white page render decoded in the test adapter."})
        self.assertTrue(self.analysis._has_read(self.analysis._task(self.task["task_id"]),block,self.bundle,[opened["receipt"]["receipt_id"]]))

    def test_text_read_and_image_path_do_not_count_as_image_open(self):
        from PIL import Image
        def with_image(path,out,options=None):
            raw = extract(path,out)
            Image.new("RGB",(3,2),"red").save(out/"image.png")
            raw["files"] = [{"key":"image","relative_path":"image.png","kind":"image","media_type":"image/png"}]
            raw["blocks"].append({"kind":"image_occurrence","origin":"native_extract","locator":{"kind":"paragraph","precision":"exact","paragraph_index":2},"file_keys":["image"],"processing_status":"deferred","reason":"Image meaning not read"})
            raw["processing_status"] = "partial"
            return raw
        self.bundle = self.store.ingest(self.project,[self.source],with_image)
        self.task = self.analysis.claim(self.project,model_version="synthetic-vision-test")
        block = self.bundle["blocks"][-1]
        item = self.analysis.read(self.task["task_id"],[block["block_id"]],100)["items"][0]
        self.assertEqual(item["status"],"requires_image_open")
        self.assertEqual(self.analysis.open_image(self.task["task_id"],block["block_id"])["status"],"blocked")
        with self.assertRaises(ContractError):
            self.analysis.open_image(self.task["task_id"],block["block_id"],opener=lambda data,**kw:{"opened":True,"path":"not-evidence"})
        def synthetic_viewer(data,**kw):
            from io import BytesIO
            with Image.open(BytesIO(data)) as image:
                self.assertEqual(image.getpixel((0,0)),(255,0,0))
            return {"opened":True,"sha256":hashlib.sha256(data).hexdigest(),"viewer_id":"synthetic-byte-consuming-viewer","observation":"Synthetic red image, 3 by 2 actual pixels."}
        opened = self.analysis.open_image(self.task["task_id"],block["block_id"],opener=synthetic_viewer)
        self.assertEqual(opened["receipt"]["kind"],"image")
        self.assertEqual(opened["receipt"]["width"],3)
        self.assertTrue(self.bundle["gaps"])
        result = self.result("partial",{block["block_id"]:"pending"})
        entry = result["block_processing"][-1]
        entry["status"] = "read"
        entry["read_receipt_refs"] = [opened["receipt"]["receipt_id"]]
        result["coverage"]["read_blocks"] += 1
        result["coverage"]["pending_blocks"] -= 1
        result["coverage"]["claim"] = "all_discovered_read"
        accepted = self.submit(result)
        self.assertIn("Open collection/image gaps",(self.project_root/accepted["main_report_relative_path"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
