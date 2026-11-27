"""Release gate for fact/source pairs independently checked by the test role."""

from datetime import datetime
import hashlib
import os
from pathlib import Path
import re

from project import ROOT, local_child, read, sha
from scope_review import SCHEMA as SCOPE_REVIEW_SCHEMA, SCOPE_FIELDS
from source_coverage import fingerprint, substantive_note


SCHEMA = 1


def required_facts(review):
    """Every explicit fact check needs an independent decision for the same row."""
    rows = []
    for claim in review["source_coverage"]["claims"]:
        for check in claim["fact_checks"]:
            rows.append({"claim_id": claim["id"], "paragraph_sha256": claim["paragraph_sha256"],
                         "anchor": check["anchor"], "origin": check["origin"], "source": check["source"],
                         "candidate_fact_sha256": fingerprint(check)})
    return rows


def identity(body, review):
    facts = required_facts(review)
    result = {"schema": SCHEMA, "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
              "scope_sha256": fingerprint(review["source_scope"]), "facts_sha256": fingerprint(facts)}
    if review.get("scope_review_schema") == SCOPE_REVIEW_SCHEMA:
        result["scope_review_sha256"] = fingerprint({k: review[k] for k in SCOPE_FIELDS})
        # Scope approval covers the received items even if no explicit critical facts exist.
        # Do not permit the writer to change any reviewed window/attachment decision after testing.
        result["received_review_sha256"] = fingerprint({k: review.get(k) for k in (
            "snapshot", "source_coverage", "episodes", "documents", "supplements", "follow_along")})
    return result


def needs_receipt(review):
    return bool(required_facts(review) or review.get("scope_review_schema") == SCOPE_REVIEW_SCHEMA)


def receipt_path(body, review):
    current = identity(body, review)
    runtime = Path(os.environ.get("WORLDANALYSIS_COURSE_RUNTIME", ROOT / ".runtime")).resolve()
    directory = local_child(runtime, "independent-fact-reviews")
    return local_child(directory, f"{fingerprint(current)}.json")


def review_request(body, review):
    """Copy candidate expectations only, never generate a tester approval."""
    request = {**identity(body, review), "receipt_path": str(receipt_path(body, review)),
               "reviewer": {"role": "", "thread_id": "", "reviewed_at": ""}, "decision": "",
               "facts": [{**row, "decision": "", "observation": "", "evidence": "", "evidence_files": []}
                         for row in required_facts(review)]}
    if review.get("scope_review_schema") == SCOPE_REVIEW_SCHEMA:
        request["scope_acceptance"] = {"scope_status": review["scope_status"], "prior_scope": review["scope_prior"], "decision": "",
            "coverage_observation": "", "gaps": [{**row, "decision": "", "review_observation": ""}
                for row in review["scope_gaps"]], "teaching": [{**row, "decision": "", "review_observation": ""}
                for row in review["scope_teaching"]], "resolutions": [{**row, "decision": "", "review_observation": ""}
                for row in review["scope_resolutions"]]}
    return request


def validate_independent_receipt(body, review, receipt):
    """Require a version-bound independent review of each fact, including attribution."""
    expected = required_facts(review)
    if not needs_receipt(review) and receipt is None:
        return
    if not isinstance(receipt, dict) or any(receipt.get(field) != value for field, value in identity(body, review).items()):
        raise ValueError("独立事实核验回执与当前正文、原件范围或事实归属不一致，须交测试 agent 复核")
    reviewer = receipt.get("reviewer")
    if (not isinstance(reviewer, dict) or reviewer.get("role") != "测试agent"
            or not substantive_note(reviewer.get("thread_id")) or receipt.get("decision") != "通过"):
        raise ValueError("独立事实核验未获测试 agent 明确签收")
    try:
        if datetime.fromisoformat(reviewer["reviewed_at"]).utcoffset() is None:
            raise ValueError("missing timezone")
    except (KeyError, ValueError, TypeError):
        raise ValueError("独立事实核验需记录带时区的实际复核日期") from None
    facts = receipt.get("facts")
    if not isinstance(facts, list) or len(facts) != len(expected):
        raise ValueError("独立事实核验缺项或包含不同版本的额外事实")
    for actual, requested in zip(facts, expected):
        if (not isinstance(actual, dict) or any(actual.get(field) != value for field, value in requested.items())
                or actual.get("decision") != "通过" or not substantive_note(actual.get("observation"))
                or not substantive_note(actual.get("evidence"))):
            raise ValueError(f"独立事实核验缺该句实际证据、结论或来源不一致 {requested['claim_id']} · {requested['anchor']}")
        files = actual.get("evidence_files", [])
        if not isinstance(files, list) or any(
            not isinstance(file, dict) or not isinstance(file.get("path"), str) or not file["path"].strip()
            or not isinstance(file.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", file["sha256"])
            for file in files
        ):
            raise ValueError(f"独立事实附带证据需登记本地相对路径和 SHA-256 {requested['claim_id']}")
    if review.get("scope_review_schema") == SCOPE_REVIEW_SCHEMA:
        scope_acceptance = receipt.get("scope_acceptance")
        if (not isinstance(scope_acceptance, dict) or scope_acceptance.get("scope_status") != review["scope_status"]
                or "prior_scope" not in scope_acceptance or scope_acceptance["prior_scope"] != review["scope_prior"]
                or scope_acceptance.get("decision") != "通过"
                or not substantive_note(scope_acceptance.get("coverage_observation"))):
            raise ValueError("范围与缺件需由测试 agent 独立核实并签收")
        for field, review_field in (("gaps", "scope_gaps"), ("teaching", "scope_teaching"), ("resolutions", "scope_resolutions")):
            actual = scope_acceptance.get(field)
            expected_rows = review[review_field]
            if not isinstance(actual, list) or len(actual) != len(expected_rows) or any(
                not isinstance(row, dict) or any(row.get(k) != v for k, v in expected.items())
                or row.get("decision") != "通过" or not substantive_note(row.get("review_observation"))
                for row, expected in zip(actual, expected_rows)
            ):
                raise ValueError("独立测试须逐项确认未收到缺件、此前缺件处置及实际教学原件证据")


def load_independent_receipt(body, review):
    path = receipt_path(body, review)
    if not path.is_file():
        if needs_receipt(review):
            raise ValueError(f"当前关键事实或范围待独立测试 agent 复核，回执应写入 {path}")
        return None
    receipt = read(path)
    validate_independent_receipt(body, review, receipt)
    # Optional attachments refer to the Worldanalysis directory, not the system disk.
    root = path.parent.parent.parent
    for row in receipt["facts"]:
        for proof in row.get("evidence_files", []):
            file = local_child(root, proof["path"])
            if not file.is_file() or sha(file) != proof["sha256"]:
                raise ValueError(f"独立事实附带证据文件缺失或变动 {row['claim_id']} · {proof['path']}")
    return receipt
