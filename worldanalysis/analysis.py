"""Persistent agent-assisted analysis, real bounded reads and checked submission.

This module never borrows subscription tokens, calls a model by default, or turns
an image pathname into a vision-read receipt. An image viewer must receive actual
bytes and acknowledge their digest; without that adapter the task stays unread.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .contracts import (ContractError, VALIDATOR_VERSION, acyclic, asset_key,
    canonical, check_id, digest, hash_file, interval_union, now, public_url,
    reject_links, safe_path, scan_secrets, unique, validate_evidence, validate_relative_path, validate_schema)
from .store import Store, _readonly, _write, new_id


class Analysis:
    def __init__(self, store: Store, *, api_worker=None):
        self.store = store
        self.connection = store.connection
        self.api_worker = api_worker

    def claim(self, project_id=None, *, model_version=None, prompt_version="general-analyzer-v1", mode="agent_assisted", provider_id="operator-agent", provider_version="agent-bridge-v1"):
        if self.store.pipeline_state("analysis") != "running":
            return {"status":"blocked","reason":"analysis paused"}
        if mode not in {"agent_assisted","api_worker"}:
            raise ContractError("unknown analysis mode")
        if mode == "api_worker" and not callable(self.api_worker):
            return {"status":"blocked","reason":"api_worker_not_configured; use agent_assisted; no model request sent"}
        if not model_version or not str(model_version).strip():
            return {"status":"blocked","reason":"actual_model_version_required"}
        check_id(prompt_version)
        provider = self.store.register_provider(provider_id,provider_version,runtime="approved_agent" if mode == "agent_assisted" else "approved_remote_api")
        agent = {"mode":mode,"provider":provider,"model_version":str(model_version),"prompt_version":prompt_version}
        scan_secrets(agent)
        if project_id:
            self.store.project_root(project_id)
        # Only the latest published version of each project is newly scheduled.
        query = "SELECT p.* FROM publications p WHERE p.state='published' AND NOT EXISTS(SELECT 1 FROM publications newer WHERE newer.project_id=p.project_id AND newer.state='published' AND newer.revision>p.revision)"
        rows = self.connection.execute(query+(" AND p.project_id=?" if project_id else "")+" ORDER BY p.created_at,p.project_id",(project_id,) if project_id else ()).fetchall()
        for row in rows:
            self.store.get_bundle(row["project_id"],row["bundle_id"])
            key = digest([row["publish_id"],agent])
            with self.store.transaction():
                task_id = new_id("analysis-task")
                outrel = "分析/"+task_id
                self.connection.execute("INSERT OR IGNORE INTO analysis_tasks(task_id,project_id,publish_id,idempotency_key,agent_json,state,output_relative_path,created_at) VALUES(?,?,?,?,?,?,?,?)", (task_id,row["project_id"],row["publish_id"],key,canonical(agent).decode(),"queued",outrel,now()))
                task = self.connection.execute("SELECT * FROM analysis_tasks WHERE idempotency_key=?",(key,)).fetchone()
                if task["state"] != "queued":
                    continue
                changed = self.connection.execute("UPDATE analysis_tasks SET state='running',claimed_at=? WHERE task_id=? AND state='queued'",(now(),task["task_id"])).rowcount
                if changed != 1:
                    continue
                self.store.event("analysis_claimed",task["task_id"],task["project_id"],{"publish_id":task["publish_id"],"model_version":str(model_version)})
            return self._envelope(self._task(task["task_id"]))
        return {"status":"idle","reason":"no_new_bundle_model_prompt_revision; running tasks require explicit resume"}

    next = claim

    def _task(self,task_id, *, require_running=False):
        check_id(task_id)
        row = self.connection.execute("SELECT * FROM analysis_tasks WHERE task_id=?",(task_id,)).fetchone()
        if not row:
            raise ContractError("unknown analysis task")
        if require_running and row["state"] != "running":
            raise ContractError("task is not running; do not duplicate completed/unknown work")
        return row

    def _bundle(self,task):
        row = self.connection.execute("SELECT bundle_id,state FROM publications WHERE publish_id=? AND project_id=?",(task["publish_id"],task["project_id"])).fetchone()
        if not row or row["state"] != "published":
            raise ContractError("task bundle no longer database-published")
        return self.store.get_bundle(task["project_id"],row["bundle_id"])

    def _envelope(self,task):
        bundle = self._bundle(task)
        root = self.store.project_root(task["project_id"])
        output = root / task["output_relative_path"]
        output.mkdir(parents=True,exist_ok=True)
        safe_path(root,task["output_relative_path"]+"/placeholder",must_exist=False)
        return {"status":task["state"],"task_id":task["task_id"],"project_id":task["project_id"],
            "bundle_id":bundle["bundle_id"],"bundle_revision":bundle["bundle_revision"],"publish_id":bundle["publish_id"],
            "source_catalog_revision":bundle["source_catalog_revision"],"agent":json.loads(task["agent_json"]),
            "project_root":str(root),"manifest_relative_path":bundle["publication"]["manifest_relative_path"],
            "output_relative_path":task["output_relative_path"],
            "blocks":[{"block_id":b["block_id"],"asset_ref":b["asset_ref"],"kind":b["kind"],"locator":b["locator"],"processing":b["processing"]} for b in bundle["blocks"]],
            "gaps":bundle["gaps"],"source_inventory_state":bundle["coverage"]["inventory_state"],
            "instructions":["Read all blocks or explicitly account for failed/pending/excluded/not_applicable.","read() is text only; image evidence requires open_image() with a real byte-consuming viewer.","Submit the original analysis schema JSON plus nonempty report files under output_relative_path.","No API is invoked in agent_assisted mode. No subscription credentials may be extracted."]}

    def resume(self,task_id):
        if self.store.pipeline_state("analysis") != "running":
            return {"status":"blocked","reason":"analysis paused"}
        task = self._task(task_id,require_running=True)
        return self._envelope(task)

    def _save_receipt(self,task,block_id,kind,receipt,content=None):
        ident = new_id("read-receipt")
        receipt = {**receipt,"receipt_id":ident,"task_id":task["task_id"],"block_id":block_id,"kind":kind,"publish_id":task["publish_id"],"created_at":now()}
        scan_secrets(receipt)
        if content is not None:
            scan_secrets(content)
            receipt["content_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self.store.transaction():
            self.connection.execute("INSERT INTO read_receipts VALUES(?,?,?,?,?,?,?)",(ident,task["task_id"],block_id,kind,canonical(receipt).decode(),content,now()))
        return receipt

    def receipt(self,receipt_id,*,_bundle=None):
        row = self.connection.execute("SELECT * FROM read_receipts WHERE receipt_id=?",(receipt_id,)).fetchone()
        if not row:
            raise ContractError("read receipt does not exist")
        task = self._task(row["task_id"])
        bundle = _bundle if _bundle is not None else self._bundle(task)
        if bundle["publish_id"] != task["publish_id"]:
            raise ContractError("receipt bundle does not belong to this task")
        receipt = json.loads(row["receipt_json"])
        if receipt["task_id"] != row["task_id"] or receipt["block_id"] != row["block_id"] or receipt["kind"] != row["kind"] or receipt["publish_id"] != task["publish_id"]:
            raise ContractError("read receipt identity mismatch")
        if row["content"] is not None and hashlib.sha256(row["content"].encode()).hexdigest() != receipt["content_sha256"]:
            raise ContractError("read receipt content corrupted")
        artifacts = {a["artifact_id"]:a for a in bundle["artifacts"]}
        for ident,sha in receipt.get("artifact_hashes",{}).items():
            if ident not in artifacts or artifacts[ident]["sha256"] != sha:
                raise ContractError("read receipt source version mismatch")
        return {**receipt,"content":row["content"]}

    def _text(self,block,bundle):
        artifacts = {a["artifact_id"]:a for a in bundle["artifacts"]}
        # Store creates one complete text artifact per text block. Inline text is
        # only a small preview and must never truncate the actual read scope.
        chosen = [artifacts[i] for i in block["artifact_ids"] if artifacts[i]["kind"] == "native_text" and Path(artifacts[i]["relative_path"]).name == block["block_id"]+".txt"]
        if chosen:
            a = chosen[0]
            if a["access_class"] == "restricted_local":
                raise ContractError("restricted_local text is not eligible for LLM delivery")
            text = safe_path(self.store.project_root(bundle["project_id"]),a["relative_path"]).read_bytes().decode("utf-8")
            return text,{a["artifact_id"]:a["sha256"]}
        text = block.get("text","")
        if not text:
            return "",{}
        return text,{}

    def read(self,task_id,block_ids,max_chars=16000):
        if self.store.pipeline_state("analysis") != "running":
            return {"status":"blocked","reason":"analysis paused","items":[]}
        if not isinstance(max_chars,int) or isinstance(max_chars,bool) or not 1 <= max_chars <= 1000000:
            raise ContractError("max_chars must be a positive bounded integer")
        if not isinstance(block_ids,list) or not block_ids or len(block_ids) != len(set(block_ids)):
            raise ContractError("block_ids must be unique")
        task = self._task(task_id,require_running=True)
        bundle = self._bundle(task)
        blocks = {b["block_id"]:b for b in bundle["blocks"]}
        if not set(block_ids) <= blocks.keys():
            raise ContractError("unknown or wrong-project block")
        items,remaining = [],max_chars
        for ident in block_ids:
            block = blocks[ident]
            visual_artifacts = [a for a in bundle["artifacts"] if a["artifact_id"] in block["artifact_ids"] and a["kind"] in {"image","page_render","keyframe"}]
            text,hashes = self._text(block,bundle)
            if block["kind"] in {"image_occurrence","chart"} or visual_artifacts and not text:
                items.append({"block_id":ident,"status":"requires_image_open","reason":"No text read can acknowledge image/page-render bytes."})
                continue
            if not text:
                items.append({"block_id":ident,"status":"unavailable","reason":"No extracted text; keep pending/failed or use an approved media reader."})
                continue
            previous = [json.loads(r[0]) for r in self.connection.execute("SELECT receipt_json FROM read_receipts WHERE task_id=? AND block_id=? AND kind='text'",(task_id,ident))]
            covered = sorted((r["char_start"],r["char_end"]) for r in previous if r.get("source_text_sha256") == hashlib.sha256(text.encode()).hexdigest())
            offset = 0
            for left,right in covered:
                if left > offset:
                    break
                offset = max(offset,right)
            if offset >= len(text):
                items.append({"block_id":ident,"status":"already_read","receipt_refs":[r["receipt_id"] for r in previous],"image_open_required":bool(visual_artifacts)})
                continue
            if remaining <= 0:
                items.append({"block_id":ident,"status":"pending","reason":"max_chars bound reached"})
                continue
            end = min(len(text),offset+remaining)
            content = text[offset:end]
            receipt = self._save_receipt(task,ident,"text",{"char_start":offset,"char_end":end,"total_chars":len(text),"source_text_sha256":hashlib.sha256(text.encode()).hexdigest(),"artifact_hashes":hashes},content)
            items.append({"block_id":ident,"status":"read" if end == len(text) and not visual_artifacts else "partial_read","text":content,"receipt":receipt,"remaining_chars":len(text)-end,"image_open_required":bool(visual_artifacts)})
            remaining -= len(content)
        status = "read" if items and all(i["status"] in {"read","already_read"} and not i.get("image_open_required") for i in items) else "partial"
        return {"status":status,"task_id":task_id,"items":items,"returned_chars":max_chars-remaining}

    def read_external(self,task_id,source_id,url,*,reader=None,allowed_hosts=()):
        from .external_verification import read
        return read(self,task_id,source_id,url,reader=reader,allowed_hosts=allowed_hosts)

    def open_image(self,task_id,block_id,artifact_id=None,*,opener=None):
        if self.store.pipeline_state("analysis") != "running":
            return {"status":"blocked","reason":"analysis paused"}
        if not callable(opener):
            return {"status":"blocked","reason":"image_viewer_not_configured; a pathname is not an image read receipt"}
        task = self._task(task_id,require_running=True)
        bundle = self._bundle(task)
        block = next((b for b in bundle["blocks"] if b["block_id"] == block_id),None)
        if block is None:
            raise ContractError("unknown or wrong-project image block")
        images = [a for a in bundle["artifacts"] if a["artifact_id"] in block["artifact_ids"] and a["kind"] in {"image","page_render","keyframe"}]
        if artifact_id:
            images = [a for a in images if a["artifact_id"] == artifact_id]
        if len(images) != 1:
            raise ContractError("select exactly one image artifact from this block")
        image = images[0]
        if image["access_class"] == "restricted_local":
            return {"status":"blocked","reason":"restricted_local image; no external model delivery"}
        path = safe_path(self.store.project_root(task["project_id"]),image["relative_path"])
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != image["sha256"]:
            raise ContractError("image changed before opening")
        # Decode and validate actual pixels, not just extension or pathname.
        try:
            from PIL import Image
            from io import BytesIO
            with Image.open(BytesIO(data)) as decoded:
                decoded.load()
                width,height = decoded.size
        except Exception as exc:
            raise ContractError("image could not be decoded; no image receipt issued") from exc
        ack = opener(data,media_type=image["media_type"],sha256=image["sha256"],width=width,height=height)
        if not isinstance(ack,dict) or ack.get("opened") is not True or ack.get("sha256") != image["sha256"] or not ack.get("viewer_id") or not str(ack.get("observation","")).strip():
            raise ContractError("viewer must acknowledge actual digest and a nonempty visual observation")
        scan_secrets(ack)
        receipt = self._save_receipt(task,block_id,"image",{"artifact_hashes":{image["artifact_id"]:image["sha256"]},"width":width,"height":height,"viewer_id":ack["viewer_id"],"observation":ack["observation"],"byte_length":len(data)})
        return {"status":"opened","receipt":receipt}

    def _has_read(self,task,block,bundle,refs):
        receipts = []
        for ident in refs:
            r = self.receipt(ident,_bundle=bundle)
            if r["task_id"] != task["task_id"] or r["block_id"] != block["block_id"]:
                raise ContractError("receipt belongs to a different task/block")
            receipts.append(r)
        if any(r['kind'] == 'reused' for r in receipts):
            from .incremental_analysis import validate_reused
            if len(receipts) != 1:
                raise ContractError('historical reuse receipt must stand alone')
            return validate_reused(self,task,block,bundle,receipts[0])
        artifacts = {a["artifact_id"]:a for a in bundle["artifacts"]}
        images = {i for i in block["artifact_ids"] if artifacts[i]["kind"] in {"image","page_render","keyframe"}}
        opened = {i for r in receipts if r["kind"] == "image" for i in r["artifact_hashes"]}
        images_read = bool(images) and images <= opened
        text,_ = self._text(block,bundle)
        if block["kind"] in {"image_occurrence","chart"} or images and not text:
            return images_read
        if images and not images_read:
            return False
        source_sha = hashlib.sha256(text.encode()).hexdigest()
        ranges = [(r["char_start"],r["char_end"]) for r in receipts if r["kind"] == "text" and r["source_text_sha256"] == source_sha and r["total_chars"] == len(text)]
        return bool(text) and interval_union(ranges) == len(text) and all(a >= 0 and b <= len(text) for a,b in ranges)

    def _validate_result(self,task,result,bundle):
        validate_schema("analysis",result)
        for key,expected in (("project_id",task["project_id"]),("based_bundle_id",bundle["bundle_id"]),("based_bundle_revision",bundle["bundle_revision"]),("based_publish_id",task["publish_id"])):
            if result[key] != expected:
                raise ContractError("analysis task/bundle/publish/project version mismatch")
        if result["agent"] != json.loads(task["agent_json"]):
            raise ContractError("analysis model/provider/prompt version mismatch")
        blocks = {b["block_id"]:b for b in bundle["blocks"]}
        processing = unique(result["block_processing"],"block_id","analysis block")
        if processing.keys() != blocks.keys():
            raise ContractError("every discovered block must be accounted exactly once")
        for ident,entry in processing.items():
            block = blocks[ident]
            if asset_key(entry["asset_ref"]) != asset_key(block["asset_ref"]):
                raise ContractError("analysis block asset version mismatch")
            for receipt in entry["read_receipt_refs"]:
                r = self.receipt(receipt,_bundle=bundle)
                if r["task_id"] != task["task_id"] or r["block_id"] != ident:
                    raise ContractError("analysis receipt belongs to another task/block")
            if entry["status"] == "read" and not self._has_read(task,block,bundle,entry["read_receipt_refs"]):
                raise ContractError("unread text ranges/image bytes cannot be claimed read")
            if entry["status"] == "not_applicable" and block["processing"]["status"] != "not_applicable":
                raise ContractError("not_applicable cannot discard real source blocks")
            if entry["status"] == "excluded":
                exclusion = entry["exclusion"]
                if exclusion["recovery_ref"] not in {block["processing"]["recovery_ref"],ident}:
                    raise ContractError("exclusion has no real recovery entry")
                for ref in exclusion["basis_refs"]:
                    validate_evidence(ref,bundle,allow_source_only=False)
        counts = Counter(e["status"] for e in processing.values())
        coverage = result["coverage"]
        for status in ("read","failed","pending","excluded","not_applicable"):
            if coverage[status+"_blocks"] != counts[status]:
                raise ContractError("analysis coverage count mismatch")
        if coverage["discovered_blocks"] != len(blocks) or coverage["source_inventory_state"] != bundle["coverage"]["inventory_state"]:
            raise ContractError("analysis source inventory mismatch")
        if coverage["claim"] == "all_discovered_read" and (not blocks or counts["read"] != len(blocks)):
            raise ContractError("false all_discovered_read")
        if result["execution_status"] == "completed" and (counts["pending"] or counts["failed"] or not counts["read"]):
            raise ContractError("pending/failed/empty work cannot masquerade as completed")
        if result["execution_status"] in {"partial","completed"} and (not counts["read"] or not result["assertions"] or not result["main_report_relative_path"] or not result["reports"]):
            raise ContractError("no evidence-based analysis/report; use blocked, not empty success")
        external = unique(result["external_sources"],"source_id","external source")
        for source in external.values():
            public_url(source["public_url"])
            if source["read_status"] == "read":
                r = self.receipt(source["receipt_ref"],_bundle=bundle)
                if r["task_id"] != task["task_id"] or r["kind"] != "external" or r.get("public_url") != source["public_url"]:
                    raise ContractError("external source has no actual matching read receipt")
            if source['read_status'] == 'read':
                from .external_verification import validate_receipt
                validate_receipt(self,task,source,bundle=bundle)
        assertions = unique(result["assertions"],"assertion_id","assertion ID")
        for assertion in assertions.values():
            for ref in assertion["evidence_refs"]:
                validate_evidence(ref,bundle,allow_source_only=False)
                if assertion["kind"] in {"extracted_fact","author_claim"} and (ref.get("block_id") not in processing or processing[ref["block_id"]]["status"] != "read"):
                    raise ContractError("source assertion must cite actually read blocks")
            for source in assertion["external_source_ids"]:
                if source not in external:
                    raise ContractError("unknown external assertion source")
                if assertion["kind"] == "external_fact" and external[source]["read_status"] != "read":
                    raise ContractError("unread external source cannot verify fact")
        acyclic(assertions,"depends_on_assertion_ids","assertion")
        steps = unique(result.get("procedure_steps",[]),"step_id","procedure step")
        for step in steps.values():
            if not set(step["assertion_ids"]) <= assertions.keys():
                raise ContractError("step refers to missing assertion")
            for ref in step["evidence_refs"]:
                validate_evidence(ref,bundle,allow_source_only=False)
            if step["status"] == "proposed_extension" and not any(assertions[a]["kind"] == "inference" for a in step["assertion_ids"]):
                raise ContractError("proposed extension needs a labeled inference")
        acyclic(steps,"depends_on_step_ids","procedure")
        gaps = unique(result["gaps"],"gap_id","analysis gap")
        for original in bundle["gaps"]:
            if original["status"] in {"open","requested"} and (original["gap_id"] not in gaps or gaps[original["gap_id"]]["status"] == "resolved"):
                raise ContractError("analysis cannot silently drop/resolve collection gaps")
        for gap in gaps.values():
            for ref in gap["target_refs"]+gap["resolution_refs"]:
                validate_evidence(ref,bundle)
        unique(result["supplement_requests"],"request_id","supplement request")
        for req in result["supplement_requests"]:
            if not set(req["gap_ids"]) <= gaps.keys() or req["status"] == "fulfilled":
                raise ContractError("supplement must reference real gaps and cannot self-fulfill")
            for ref in req["minimal_targets"]:
                validate_evidence(ref,bundle)
        reports = unique(result["reports"],"report_id","report ID")
        unique(result["reports"],lambda r:r["relative_path"].casefold(),"report path")
        root = self.store.project_root(task["project_id"])
        report_text = {}
        for report in reports.values():
            if not report["relative_path"].startswith(task["output_relative_path"]+"/"):
                raise ContractError("reports must be owned by this task")
            path = safe_path(root,report["relative_path"])
            report_bytes = path.read_bytes()
            if hashlib.sha256(report_bytes).hexdigest() != report["sha256"]:
                raise ContractError("report hash mismatch")
            text = report_bytes.decode("utf-8")
            if not text.strip():
                raise ContractError("empty report is not success")
            scan_secrets(text)
            report_text[report["relative_path"]] = text
        if result["main_report_relative_path"] is not None and result["main_report_relative_path"] not in report_text:
            raise ContractError("main report not in hashed reports")
        if result["execution_status"] in {"partial","completed"} and (counts["read"] != len(blocks) or bundle["gaps"]) and not coverage["limitations"]:
            raise ContractError("partial/unread/missing-image limitations must be explicit")
        return processing,report_text

    def complete(self,task_id,result_path):
        task = self._task(task_id)
        root = self.store.project_root(task["project_id"])
        path = Path(result_path).absolute()
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise ContractError("result must be inside the task project") from exc
        path = safe_path(root,rel)
        if not rel.startswith(task["output_relative_path"]+"/"):
            raise ContractError("result must be inside task-owned output directory")
        input_bytes = path.read_bytes()
        input_sha = hashlib.sha256(input_bytes).hexdigest()
        journal = self.connection.execute("SELECT * FROM analysis_publications WHERE task_id=?",(task_id,)).fetchone()
        if journal:
            if journal["submitted_sha256"] != input_sha:
                raise ContractError("task submission already journaled with a different input hash")
            return self._finish_submission(task_id,idempotent=True)
        if task["state"] in {"completed","partial","blocked","failed"}:
            existing = self.connection.execute("SELECT * FROM analyses WHERE task_id=?",(task_id,)).fetchone()
            if existing and json.loads(existing["result_json"]).get("analysis_id") == json.loads(input_bytes).get("analysis_id"):
                # Idempotence requires exact validated persisted content, not ID alone.
                self.get_result(task_id)
                submitted = self.connection.execute("SELECT payload_json FROM events WHERE kind='analysis_completed' AND record_id=?",(task_id,)).fetchone()
                if submitted and json.loads(submitted[0]).get("submitted_sha256") == input_sha:
                    return {"status":task["state"],"task_id":task_id,"analysis_id":existing["analysis_id"],"result_relative_path":existing["result_relative_path"],"idempotent":True}
            raise ContractError("task already finalized with different result")
        if task["state"] != "running" or self.store.pipeline_state("analysis") != "running":
            raise ContractError("analysis not running")
        result = json.loads(input_bytes)
        bundle = self._bundle(task)
        processing,texts = self._validate_result(task,result,bundle)
        if result["execution_status"] == "running":
            raise ContractError("running is not a completed submission")
        result = deepcopy(result)
        self._prepare_submission(task,input_sha,result,processing,texts,bundle)
        return self._finish_submission(task_id)

    def _prepare_submission(self,task,input_sha,result,processing,texts,bundle):
        task_id = task["task_id"]
        root = self.store.project_root(task["project_id"])
        final_rel = task["output_relative_path"]+"/accepted"
        staging_rel = task["output_relative_path"]+"/.accepted-staging-"+task_id
        reject_links(root / final_rel)
        if (root / final_rel).exists() or (root / staging_rel).exists():
            raise ContractError("unregistered analysis directory; never infer acceptance from orphan files")
        unread = [ident for ident,e in processing.items() if e["status"] != "read"]
        coverage = "# Evidence coverage / 证据范围\n\n"+f"Bundle: {bundle['bundle_id']} {bundle['bundle_revision']} / {bundle['publish_id']}\n\n"+f"Read: {result['coverage']['read_blocks']} / {len(processing)}; inventory: {bundle['coverage']['inventory_state']}.\n\n"+"Unread/failed/excluded/not-applicable blocks: "+(", ".join(unread) or "none")+"\n\n"+"Open collection/image gaps: "+(", ".join(g['gap_id'] for g in bundle["gaps"] if g["status"] in {"open","requested"}) or "none")+"\n\n"+"\n".join("- "+s for s in result["coverage"]["limitations"])+"\n\n---\n\n"
        outputs = {}
        main = result["main_report_relative_path"]
        for index,report in enumerate(result["reports"]):
            oldrel = report["relative_path"]
            newrel = final_rel+f"/report-{index:02d}.md"
            outputs[newrel] = (coverage if oldrel == main else "")+texts[oldrel]
            report["relative_path"] = newrel
            report["sha256"] = hashlib.sha256(outputs[newrel].encode("utf-8")).hexdigest()
            if oldrel == main:
                result["main_report_relative_path"] = newrel
        checked = now()
        validation_rel = final_rel+"/validation.json"
        outputs[validation_rel] = canonical({"status":"passed","task_id":task_id,"publish_id":bundle["publish_id"],"validator_version":VALIDATOR_VERSION,"checked_at":checked,"submitted_sha256":input_sha}).decode("utf-8")
        result["program_validation"] = {"status":"passed","validator_version":VALIDATOR_VERSION,"checked_at":checked,"report_relative_path":validation_rel}
        for req in result["supplement_requests"]:
            req["status"] = "blocked" if req["requires_scope_expansion"] else "queued_for_collection"
        validate_schema("analysis",result)
        result_rel = final_rel+"/analysis.json"
        result_json = canonical(result).decode("utf-8")
        outputs[result_rel] = result_json
        if sum(len(text.encode("utf-8")) for text in outputs.values()) > 32*1024*1024:
            raise ContractError("analysis output snapshot bound exceeded")
        expected = {rel:{"sha256":hashlib.sha256(text.encode("utf-8")).hexdigest(),"byte_length":len(text.encode("utf-8"))} for rel,text in outputs.items()}
        # Persist exact, validated textual output snapshots BEFORE writing a
        # staging file. Recovery never reruns a model or rereads mutable reports.
        with self.store.transaction():
            current = self._task(task_id,require_running=True)
            if current["publish_id"] != task["publish_id"]:
                raise ContractError("task version changed before submission registration")
            self.connection.execute("INSERT INTO analysis_publications(task_id,project_id,publish_id,submitted_sha256,state,staging_relative_path,final_relative_path,result_relative_path,result_sha256,result_json,expected_files_json,outputs_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(task_id,task["project_id"],task["publish_id"],input_sha,"staging",staging_rel,final_rel,result_rel,expected[result_rel]["sha256"],result_json,canonical(expected).decode(),canonical(outputs).decode(),checked))

    def _journal_files(self,journal,*,staging=False,check_files=True):
        task = self._task(journal["task_id"])
        root = self.store.project_root(task["project_id"])
        final_rel = journal["final_relative_path"]
        if journal["project_id"] != task["project_id"] or journal["publish_id"] != task["publish_id"] or final_rel != task["output_relative_path"]+"/accepted" or journal["staging_relative_path"] != task["output_relative_path"]+"/.accepted-staging-"+task["task_id"] or journal["result_relative_path"] != final_rel+"/analysis.json":
            raise ContractError("analysis publication journal identity/path mismatch")
        expected,outputs = json.loads(journal["expected_files_json"]),json.loads(journal["outputs_json"])
        if expected.keys() != outputs.keys():
            raise ContractError("analysis journal output set mismatch")
        for rel,text in outputs.items():
            validate_relative_path(rel)
            if not rel.startswith(final_rel+"/") or not isinstance(text,str) or (hashlib.sha256(text.encode("utf-8")).hexdigest(),len(text.encode("utf-8"))) != (expected[rel]["sha256"],expected[rel]["byte_length"]):
                raise ContractError("analysis journal output snapshot hash mismatch")
        if outputs[journal["result_relative_path"]] != journal["result_json"] or expected[journal["result_relative_path"]]["sha256"] != journal["result_sha256"]:
            raise ContractError("analysis journal result snapshot mismatch")
        if not check_files:
            return json.loads(journal["result_json"])
        base = root / (journal["staging_relative_path"] if staging else final_rel)
        reject_links(base)
        if not base.is_dir():
            raise ContractError("analysis publication directory missing")
        actual = set()
        for file in base.rglob("*"):
            reject_links(file)
            if file.is_file():
                actual.add(final_rel+"/"+file.relative_to(base).as_posix())
        if actual != set(expected):
            raise ContractError("analysis publication file set mismatch")
        for rel,item in expected.items():
            if hash_file(safe_path(base,rel[len(final_rel)+1:])) != (item["sha256"],item["byte_length"]):
                raise ContractError("analysis publication file hash mismatch")
        result = json.loads(journal["result_json"])
        if result["project_id"] != task["project_id"] or result["based_publish_id"] != task["publish_id"] or result["agent"] != json.loads(task["agent_json"]):
            raise ContractError("analysis publication task/model version mismatch")
        return result

    def _after_analysis_rename(self,task_id):
        """Fault-injection seam; no provider/model operation."""

    def _quarantine_submission(self,task_id,reason):
        with self.store.transaction():
            self.connection.execute("UPDATE analysis_publications SET state='quarantined',reason=? WHERE task_id=?",(type(reason).__name__+": "+str(reason)[:250],task_id))
            self.connection.execute("UPDATE analysis_tasks SET state='blocked',reason='analysis publication quarantined; explicit reconciliation required' WHERE task_id=?",(task_id,))
            self.store.event("analysis_submission_quarantined",task_id)

    def _finish_submission(self,task_id,*,idempotent=False):
        journal = self.connection.execute("SELECT * FROM analysis_publications WHERE task_id=?",(task_id,)).fetchone()
        if not journal or journal["state"] == "quarantined":
            raise ContractError("analysis submission unavailable/quarantined")
        if journal["state"] == "committed":
            self.get_result(task_id)
            return self._submission_response(journal,True)
        task = self._task(task_id,require_running=True)
        root = self.store.project_root(task["project_id"])
        stage,final = root/journal["staging_relative_path"],root/journal["final_relative_path"]
        try:
            self._journal_files(journal,check_files=False)
            reject_links(stage)
            reject_links(final)
            if final.exists():
                if stage.exists():
                    raise ContractError("both staged and renamed analysis outputs exist")
                self._journal_files(journal)
            else:
                stage.mkdir(parents=True,exist_ok=True)
                # Existing valid files can be resumed, never overwritten/deleted.
                for rel,text in json.loads(journal["outputs_json"]).items():
                    target = safe_path(stage,rel[len(journal["final_relative_path"])+1:],must_exist=False)
                    data = text.encode("utf-8")
                    if target.exists():
                        if hash_file(target) != (hashlib.sha256(data).hexdigest(),len(data)):
                            raise ContractError("staged analysis output tampered")
                    else:
                        _write(target,data)
                self._journal_files(journal,staging=True)
                with self.store.transaction():
                    self.connection.execute("UPDATE analysis_publications SET state='validated' WHERE task_id=? AND submitted_sha256=?",(task_id,journal["submitted_sha256"]))
                if stage.parent.stat().st_dev != final.parent.stat().st_dev:
                    raise ContractError("analysis rename crosses volumes")
                stage.rename(final)
                self._after_analysis_rename(task_id)
            with self.store.transaction():
                self.connection.execute("UPDATE analysis_publications SET state='renamed' WHERE task_id=? AND state!='committed'",(task_id,))
            journal = self.connection.execute("SELECT * FROM analysis_publications WHERE task_id=?",(task_id,)).fetchone()
            result = self._journal_files(journal)
            for file in final.rglob("*"):
                if file.is_file():
                    _readonly(file)
            with self.store.transaction():
                # Recheck sealed files, source publication and real receipts in
                # the commit transaction; no model is invoked during recovery.
                result = self._journal_files(journal)
                self._validate_result(task,result,self._bundle(task))
                self.connection.execute("INSERT INTO analyses VALUES(?,?,?,?,?,?,?,?,?)",(result["analysis_id"],result["analysis_revision"],task_id,task["project_id"],task["publish_id"],journal["result_json"],journal["result_sha256"],journal["result_relative_path"],now()))
                for req in result["supplement_requests"]:
                    existing = self.connection.execute("SELECT * FROM supplement_requests WHERE request_id=?",(req["request_id"],)).fetchone()
                    if existing:
                        old_request = json.loads(existing["request_json"])
                        if existing["task_id"] != task_id or {k:v for k,v in old_request.items() if k != "status"} != {k:v for k,v in req.items() if k != "status"}:
                            raise ContractError("supplement request ID reused by another revision/task")
                        self.connection.execute("UPDATE supplement_requests SET request_json=? WHERE request_id=?",(canonical(req).decode(),req["request_id"]))
                    else:
                        self.connection.execute("INSERT INTO supplement_requests VALUES(?,?,?,?,?,?,?)",(req["request_id"],task["project_id"],task_id,task["publish_id"],"blocked_scope_expansion" if req["requires_scope_expansion"] else "queued",canonical(req).decode(),now()))
                changed = self.connection.execute("UPDATE analysis_tasks SET state=?,completed_at=?,result_sha256=?,result_relative_path=? WHERE task_id=? AND project_id=? AND publish_id=? AND state='running'",(result["execution_status"],now(),journal["result_sha256"],journal["result_relative_path"],task_id,task["project_id"],task["publish_id"])).rowcount
                if changed != 1:
                    raise ContractError("task changed before analysis commit")
                expected = json.loads(journal["expected_files_json"])
                self.store.event("analysis_completed",task_id,task["project_id"],{"analysis_id":result["analysis_id"],"submitted_sha256":journal["submitted_sha256"],"result_sha256":journal["result_sha256"],"validation_sha256":expected[result["program_validation"]["report_relative_path"]]["sha256"]})
                self.connection.execute("UPDATE analysis_publications SET state='committed',reason=NULL WHERE task_id=? AND submitted_sha256=?",(task_id,journal["submitted_sha256"]))
            return self._submission_response(journal,idempotent)
        except (ContractError,ValueError,KeyError,sqlite3.IntegrityError) as exc:
            self._quarantine_submission(task_id,exc)
            raise

    def _submission_response(self,journal,idempotent):
        result = json.loads(journal["result_json"])
        return {"status":result["execution_status"],"task_id":journal["task_id"],"analysis_id":result["analysis_id"],"result_relative_path":journal["result_relative_path"],"main_report_relative_path":result["main_report_relative_path"],"result_sha256":journal["result_sha256"],"supplement_requests":len(result["supplement_requests"]),"idempotent":idempotent}

    def _recover_submissions(self):
        recovered = []
        for journal in self.connection.execute("SELECT * FROM analysis_publications ORDER BY created_at").fetchall():
            if journal["state"] == "quarantined":
                continue
            try:
                self._finish_submission(journal["task_id"],idempotent=True)
                recovered.append({"task_id":journal["task_id"],"state":"committed"})
            except (ContractError,OSError,ValueError,KeyError,sqlite3.IntegrityError) as exc:
                self._quarantine_submission(journal["task_id"],exc)
                recovered.append({"task_id":journal["task_id"],"state":"quarantined"})
        return recovered

    def get_result(self,task_id):
        task = self._task(task_id)
        self._bundle(task)
        journal = self.connection.execute("SELECT * FROM analysis_publications WHERE task_id=?",(task_id,)).fetchone()
        if journal:
            if journal["state"] != "committed":
                raise ContractError("analysis submission is not journal-committed")
            self._journal_files(journal)
        row = self.connection.execute("SELECT * FROM analyses WHERE task_id=?",(task_id,)).fetchone()
        if not row:
            raise ContractError("no accepted analysis revision")
        root = self.store.project_root(task["project_id"])
        path = safe_path(root,row["result_relative_path"])
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row["result_sha256"] or data.decode("utf-8") != row["result_json"]:
            raise ContractError("persisted analysis result tampered")
        result = json.loads(data)
        validate_schema("analysis",result)
        for report in result["reports"]:
            if hash_file(safe_path(root,report["relative_path"]))[0] != report["sha256"]:
                raise ContractError("accepted report tampered")
        validation_path = safe_path(root,result["program_validation"]["report_relative_path"])
        events = self.connection.execute("SELECT * FROM events WHERE kind='analysis_completed' AND record_id=?",(task_id,)).fetchall()
        if len(events) != 1:
            raise ContractError("analysis completion provenance requires review")
        event = events[0]
        payload = json.loads(event["payload_json"])
        validation_bytes = validation_path.read_bytes()
        validation = json.loads(validation_bytes)
        actual_sha = hashlib.sha256(validation_bytes).hexdigest()
        if validation.get("task_id") != task_id or validation.get("publish_id") != task["publish_id"] or validation.get("status") != "passed" or validation.get("validator_version") != result["program_validation"]["validator_version"]:
            raise ContractError("analysis validation receipt mismatch")
        if payload.get("validation_sha256"):
            if payload["validation_sha256"] != actual_sha:
                raise ContractError("analysis validation receipt hash mismatch")
        else:
            # Pre-index accepted revisions did not record a historical validation
            # file hash. Do NOT manufacture one or rewrite their event/report.
            # Cross-check the original result/submission event chain and perform
            # a fresh full program check, then pin only today's observed hash in
            # an explicitly provenance-labelled additive index.
            index = self.connection.execute("SELECT * FROM analysis_validation_indexes WHERE task_id=?",(task_id,)).fetchone()
            if index:
                if index["observed_sha256"] != actual_sha or index["result_sha256"] != row["result_sha256"] or index["event_id"] != event["event_id"] or index["submitted_sha256"] != payload.get("submitted_sha256") or index["validation_relative_path"] != result["program_validation"]["report_relative_path"]:
                    raise ContractError("legacy validation indexed hash/provenance mismatch")
            else:
                submitted_sha = payload.get("submitted_sha256","")
                if event["project_id"] != task["project_id"] or payload.get("analysis_id") != row["analysis_id"] or payload.get("result_sha256") != row["result_sha256"] or validation.get("submitted_sha256") != submitted_sha or not isinstance(submitted_sha,str) or len(submitted_sha) != 64 or any(c not in "0123456789abcdef" for c in submitted_sha):
                    raise ContractError("legacy validation provenance insufficient; requires explicit review")
                try:
                    vtime = datetime.fromisoformat(validation["checked_at"].replace("Z","+00:00"))
                    rtime = datetime.fromisoformat(result["program_validation"]["checked_at"].replace("Z","+00:00"))
                    etime = datetime.fromisoformat(event["created_at"].replace("Z","+00:00"))
                    if not vtime <= rtime <= etime or (etime-vtime).total_seconds() > 300:
                        raise ValueError("inconsistent receipt chronology")
                except (ValueError,TypeError,KeyError) as exc:
                    raise ContractError("legacy validation chronology requires explicit review") from exc
                self._validate_result(task,result,self._bundle(task))
                provenance = "legacy_current_full_revalidation; original_completion_event_result_and_submission_sha256; historical_validation_hash_not_recorded"
                with self.store.transaction():
                    self.connection.execute("INSERT OR IGNORE INTO analysis_validation_indexes VALUES(?,?,?,?,?,?,?,?)",(task_id,result["program_validation"]["report_relative_path"],actual_sha,row["result_sha256"],event["event_id"],submitted_sha,provenance,now()))
                pinned = self.connection.execute("SELECT observed_sha256 FROM analysis_validation_indexes WHERE task_id=?",(task_id,)).fetchone()
                if pinned[0] != actual_sha:
                    raise ContractError("legacy validation index changed concurrently")
        return result

    def validation_integrity(self,task_id):
        self.get_result(task_id)
        index = self.connection.execute("SELECT * FROM analysis_validation_indexes WHERE task_id=?",(task_id,)).fetchone()
        if index:
            return {"task_id":task_id,"mode":"legacy_cross_checked_not_historical_hash","observed_sha256":index["observed_sha256"],"provenance":index["provenance"],"observed_at":index["observed_at"]}
        event = self.connection.execute("SELECT payload_json FROM events WHERE kind='analysis_completed' AND record_id=?",(task_id,)).fetchone()
        return {"task_id":task_id,"mode":"original_completion_event_hash","validation_sha256":json.loads(event[0])["validation_sha256"]}

    def request_evidence(self,task_id,request):
        """Persist a proposal without cloud privileges, authorization or execution."""
        task = self._task(task_id,require_running=True)
        bundle = self._bundle(task)
        check_id(request["request_id"])
        required = {"request_id","gap_ids","reason","minimal_targets","requested_outputs","priority","status","requires_scope_expansion"}
        if set(request) != required or request["status"] not in {"proposed","queued_for_collection","blocked","accepted_limitation"} or request["priority"] not in {"blocking","important","optional"}:
            raise ContractError("invalid supplement request contract")
        if not request["gap_ids"] or len(request["gap_ids"]) != len(set(request["gap_ids"])) or not set(request["gap_ids"]) <= {g["gap_id"] for g in bundle["gaps"]}:
            raise ContractError("supplement references missing bundle gaps")
        allowed_outputs = {"inventory","original_text","original_image","page_render","table","formula","audio_window","transcript","video_clip","source_attribution"}
        if not isinstance(request["requires_scope_expansion"],bool) or not set(request["requested_outputs"]) <= allowed_outputs:
            raise ContractError("invalid supplement outputs or scope")
        scan_secrets(request)
        for ref in request.get("minimal_targets",[]):
            validate_evidence(ref,bundle)
        if not request.get("reason") or not request.get("requested_outputs"):
            raise ContractError("supplement needs reason and minimal requested outputs")
        with self.store.transaction():
            old = self.connection.execute("SELECT * FROM supplement_requests WHERE request_id=?",(request["request_id"],)).fetchone()
            if old:
                if old["task_id"] != task_id or json.loads(old["request_json"]) != request:
                    raise ContractError("supplement ID conflict")
            else:
                self.connection.execute("INSERT INTO supplement_requests VALUES(?,?,?,?,?,?,?)",(request["request_id"],task["project_id"],task_id,task["publish_id"],"blocked_scope_expansion" if request.get("requires_scope_expansion") else "queued",canonical(request).decode(),now()))
                self.store.event("supplement_requested",request["request_id"],task["project_id"])
        return {"request_id":request["request_id"],"status":"blocked" if request.get("requires_scope_expansion") else "queued_for_collection","executed":False}
