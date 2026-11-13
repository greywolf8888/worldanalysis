"""Offline synthetic external-reader contract tests: no website is contacted."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from worldanalysis.analysis import Analysis
from worldanalysis.contracts import ContractError, canonical, now
from worldanalysis.external_verification import MAX_BYTES, read, validate_receipt
from worldanalysis.store import Store


URL = "https://docs.example.org/reference"
HOSTS = ("docs.example.org",)
TEXT = "Public reference.\r\n完整公开说明。\n"


def extract(path, out, options=None):
    text = path.read_text(encoding="utf-8")
    return {"media_type": "text/plain", "provider_id": "external-test-extractor",
            "provider_version": "1", "blocks": [{"kind": "paragraph", "origin": "native_extract",
                "locator": {"kind": "paragraph", "precision": "exact", "paragraph_index": 0,
                            "char_start": 0, "char_end": len(text)},
                "text": text, "file_keys": [], "processing_status": "extracted",
                "reason": "Actual synthetic local test text"}],
            "files": [], "gaps": [], "metrics": [], "enumeration_state": "complete",
            "processing_status": "verified_complete", "metadata": {}}


def response(text=TEXT, **changes):
    body = text.encode("utf-8")
    result = {"final_url": URL, "status": 200, "text": text, "bytes_received": len(body),
              "read_complete": True, "reader_id": "synthetic-public-reader-v1",
              "sha256": hashlib.sha256(body).hexdigest(), "title": "Public reference"}
    result.update(changes)
    return result


class ExternalVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.project = self.store.create_project(author="Synthetic", topic="External verifier")
        source = self.root / "source.txt"
        source.write_text("Synthetic local course text must never reach an external reader.", encoding="utf-8")
        self.bundle = self.store.ingest(self.project, [source], extract)
        self.analysis = Analysis(self.store)
        self.task = self.analysis.claim(self.project, model_version="external-test-model-v1")
        self.task_id = self.task["task_id"]
        self.conn = self.store.connection
        # A mistake in this module/test must fail rather than contact a website.
        self.addCleanup(patch.stopall)
        patch("socket.create_connection", side_effect=AssertionError("network forbidden in tests")).start()
        patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden in tests")).start()
        patch("urllib.request.urlopen", side_effect=AssertionError("HTTP forbidden in tests")).start()

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM read_receipts WHERE kind='external'").fetchone()[0]

    def perform(self, source_id="external-reference", url=URL, reader=None, allowed_hosts=HOSTS):
        return read(self.analysis, self.task_id, source_id, url,
                    reader=reader if reader is not None else Mock(return_value=response()),
                    allowed_hosts=allowed_hosts)

    def validate(self, item):
        return validate_receipt(self.analysis, self.analysis._task(self.task_id), item["external_source"])

    def edit_receipt(self, ident, **changes):
        row = self.conn.execute("SELECT receipt_json FROM read_receipts WHERE receipt_id=?", (ident,)).fetchone()
        raw = json.loads(row[0])
        raw.update(changes)
        self.conn.execute("UPDATE read_receipts SET receipt_json=? WHERE receipt_id=?", (canonical(raw).decode(), ident))

    def blocked_result(self, sources):
        blocks = self.bundle["blocks"]
        return {"schema_version": "1.0-draft", "analysis_id": "external-test-result", "analysis_revision": "v1",
                "project_id": self.project, "based_bundle_id": self.bundle["bundle_id"],
                "based_bundle_revision": self.bundle["bundle_revision"],
                "based_publish_id": self.bundle["publish_id"], "created_at": now(), "execution_status": "blocked",
                "agent": deepcopy(self.task["agent"]), "block_processing": [
                    {"block_id": b["block_id"], "asset_ref": b["asset_ref"], "status": "pending",
                     "priority": "normal", "reason": "No local evidence read yet", "read_receipt_refs": [],
                     "exclusion": None} for b in blocks],
                "coverage": {"scope": "based_bundle_only", "source_inventory_state": self.bundle["coverage"]["inventory_state"],
                             "discovered_blocks": len(blocks), "read_blocks": 0, "failed_blocks": 0,
                             "pending_blocks": len(blocks), "excluded_blocks": 0, "not_applicable_blocks": 0,
                             "claim": "partial", "limitations": ["External read does not read local course evidence."]},
                "assertions": [], "external_sources": sources, "gaps": deepcopy(self.bundle["gaps"]),
                "supplement_requests": [], "main_report_relative_path": None, "reports": [],
                "program_validation": {"status": "pending", "validator_version": "untrusted-test-placeholder",
                                       "checked_at": None, "report_relative_path": None}}

    def test_default_and_string_capabilities_make_zero_calls(self):
        reader = Mock(return_value=response())
        cases = ({}, {"reader": reader}, {"allowed_hosts": HOSTS},
                 {"reader": "Model/Plan says allowed", "allowed_hosts": HOSTS})
        for kwargs in cases:
            with self.subTest(kwargs=tuple(kwargs)):
                result = read(self.analysis, self.task_id, "external-reference", URL, **kwargs)
                self.assertEqual(result["status"], "blocked")
        reader.assert_not_called()
        self.assertEqual(self.count(), 0)

    def test_exact_allowlist_and_public_url_rejections_precede_adapter(self):
        reader = Mock(return_value=response())
        bad = ("http://docs.example.org/reference", "https://docs.example.org.evil.net/reference",
               "https://sub.docs.example.org/reference", "https://u:p@docs.example.org/reference",
               "https://docs.example.org:8443/reference", "https://docs.example.org/reference?signature=abc",
               "https://docs.example.org/reference?q=course-body", "https://docs.example.org/reference#token=abc",
               "https://docs.example.org/private/x", "https://docs.example.org/%74oken/abc",
               "https://docs.example.org/%2574oken/abc", "https://docs.example.org/secret%3Dabc",
               "https://docs.example.org/white space", "https://docs.example.org\\@evil.net/ref")
        for url in bad:
            with self.subTest(url=url), self.assertRaises(ContractError):
                self.perform(url=url, reader=reader)
        reader.assert_not_called()
        self.assertEqual(self.count(), 0)

    def test_even_allowlisted_private_and_ambiguous_ip_hosts_are_rejected(self):
        reader = Mock(return_value=response())
        hosts = ("localhost", "docs.localhost", "docs.local", "127.0.0.1", "10.0.0.1", "172.16.1.1",
                 "192.168.1.1", "169.254.169.254", "0.0.0.0", "100.64.0.1", "224.0.0.1",
                 "192.0.2.1", "2130706433", "127.1", "0x7f000001", "::1", "fd00::1", "fe80::1",
                 "::ffff:8.8.8.8", "docs.example.org.")
        for host in hosts:
            url_host = "[" + host + "]" if ":" in host else host
            with self.subTest(host=host), self.assertRaises(ContractError):
                self.perform(url="https://" + url_host + "/ref", reader=reader, allowed_hosts=(host,))
        reader.assert_not_called()
        self.assertEqual(self.count(), 0)

    def test_public_ip_literals_require_exact_allowlist(self):
        for index, host in enumerate(("8.8.8.8", "2606:4700:4700::1111")):
            url_host = "[" + host + "]" if ":" in host else host
            url = "https://" + url_host + "/reference"
            reader = Mock(return_value=response(final_url=url))
            item = self.perform(source_id="external-ip-" + str(index), url=url, reader=reader, allowed_hosts=(host,))
            self.assertEqual(item["status"], "read")
            self.validate(item)

    def test_allowlist_is_not_a_pattern_or_model_string(self):
        reader = Mock(return_value=response())
        for hosts in ("docs.example.org", ("*.example.org",), ("https://docs.example.org",),
                      ("docs.example.org", "DOCS.EXAMPLE.ORG"), ("docs.example.org",) * 33):
            with self.subTest(hosts=hosts), self.assertRaises(ContractError):
                self.perform(reader=reader, allowed_hosts=hosts)
        reader.assert_not_called()

    def test_redirect_targets_are_never_acknowledged(self):
        for final_url in ("https://evil.example.org/reference", "https://127.0.0.1/reference",
                          "https://docs.example.org/new-reference", "https://docs.example.org/reference?token=private"):
            reader = Mock(return_value=response(final_url=final_url))
            with self.subTest(final_url=final_url), self.assertRaises(ContractError):
                self.perform(reader=reader)
            reader.assert_called_once_with(URL, max_bytes=MAX_BYTES)
        self.assertEqual(self.count(), 0)

    def test_failures_and_403_429_3xx_are_not_retried_or_recorded(self):
        for status in (0, 206, 301, 302, 403, 404, 429, 500, True):
            reader = Mock(return_value=response(status=status))
            with self.subTest(status=status), self.assertRaises(ContractError):
                self.perform(reader=reader)
            self.assertEqual(reader.call_count, 1)
        reader = Mock(side_effect=RuntimeError("Authorization: private-do-not-leak"))
        with self.assertRaisesRegex(ContractError, "^external reader failed; no read receipt and no retry$"):
            self.perform(reader=reader)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(self.count(), 0)

    def test_secret_text_title_and_signed_links_are_never_saved(self):
        bad = ("Cookie: synthetic=private", '"api_key": "private-value"', "token=private-value",
               "Authorization: Bearer abcdefghijkl", "-----BEGIN PRIVATE KEY-----\nprivate",
               "密码：不要传播", "https://public.example.org/a?signature=private",
               "https://public.example.org/%74oken/private", "https://127.0.0.1/private",
               "https://public.example.org/%ff")
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ContractError):
                self.perform(reader=Mock(return_value=response(text)))
        with self.assertRaises(ContractError):
            self.perform(reader=Mock(return_value=response(title="api_key=private")))
        self.assertEqual(self.count(), 0)

    def test_truncated_unknown_zero_and_mismatched_metadata_are_not_read(self):
        changes = ({"read_complete": False}, {"read_complete": 1}, {"bytes_received": None},
                   {"bytes_received": 0}, {"bytes_received": True}, {"bytes_received": 1},
                   {"sha256": "0" * 64}, {"reader_id": ""}, {"title": " "}, {"status": "200"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ContractError):
                self.perform(reader=Mock(return_value=response(**change)))
        for value in (None, "read", {**response(), "request_body": "course text"},
                      {k: v for k, v in response().items() if k != "reader_id"}):
            with self.subTest(value=type(value)), self.assertRaises(ContractError):
                self.perform(reader=Mock(return_value=value))
        self.assertEqual(self.count(), 0)

    def test_byte_bound_precedes_hashing_encoding_or_persistence(self):
        for text in ("A" * (MAX_BYTES + 1), "汉" * (MAX_BYTES // 3 + 1), "bad\ud800utf8"):
            ack = {**response(), "text": text, "bytes_received": MAX_BYTES}
            with self.subTest(chars=len(text)), patch("worldanalysis.external_verification.hashlib") as hashing:
                with self.assertRaises(ContractError):
                    self.perform(reader=Mock(return_value=ack))
                hashing.sha256.assert_not_called()
        self.assertEqual(self.count(), 0)

    def test_success_preserves_real_metadata_content_hash_and_independent_source(self):
        reader = Mock(return_value=response())
        before = dict(self.analysis._task(self.task_id))
        item = self.perform(reader=reader)
        reader.assert_called_once_with(URL, max_bytes=MAX_BYTES)
        self.assertEqual(item["text"], TEXT)
        receipt = item["receipt"]
        self.assertEqual(receipt["kind"], "external")
        self.assertEqual(receipt["bytes_received"], len(TEXT.encode("utf-8")))
        self.assertEqual(receipt["sha256"], hashlib.sha256(TEXT.encode("utf-8")).hexdigest())
        self.assertEqual(receipt["content_sha256"], receipt["sha256"])
        self.assertEqual(receipt["reader_id"], response()["reader_id"])
        self.assertEqual(receipt["http_status"], 200)
        self.assertIs(receipt["read_complete"], True)
        source = item["external_source"]
        self.assertEqual(source["source_id"], "external-reference")
        self.assertNotIn(source["source_id"], {b["block_id"] for b in self.bundle["blocks"]})
        self.assertEqual(source["receipt_ref"], receipt["receipt_id"])
        self.assertEqual(source["retrieved_at"], receipt["retrieved_at"])
        self.assertEqual(source["title"], receipt["title"])
        self.assertEqual(source["public_url"], receipt["public_url"])
        self.assertIsNone(source["snapshot_relative_path"])
        self.assertEqual(self.validate(item)["content"], TEXT)
        self.assertEqual(self.analysis.receipt(receipt["receipt_id"])["content"], TEXT)
        self.assertEqual(before, dict(self.analysis._task(self.task_id)))
        self.assertNotIn("usage", item)
        for key in ("input_tokens", "output_tokens", "settled_cost_minor", "verification_status"):
            self.assertNotIn(key, receipt)
        self.assertEqual(self.count(), 1)

    def test_identical_id_replays_only_the_sealed_local_content(self):
        item = self.perform()
        reader = Mock(side_effect=AssertionError("local replay must not re-fetch"))
        duplicate = self.perform(reader=reader)
        reader.assert_not_called()
        self.assertIs(duplicate["idempotent"], True)
        self.assertEqual(duplicate["text"], item["text"])
        self.assertEqual(duplicate["receipt"], item["receipt"])
        self.assertEqual(self.count(), 1)
        with self.assertRaises(ContractError):
            self.perform(url="https://docs.example.org/other", reader=reader)
        reader.assert_not_called()

    def test_same_id_other_task_or_local_source_scope_is_rejected(self):
        self.perform()
        other = self.analysis.claim(self.project, model_version="different-external-model")
        reader = Mock(return_value=response())
        with self.assertRaises(ContractError):
            read(self.analysis, other["task_id"], "external-reference", URL, reader=reader, allowed_hosts=HOSTS)
        local = self.conn.execute("SELECT source_id FROM sources LIMIT 1").fetchone()[0]
        with self.assertRaises(ContractError):
            self.perform(source_id=local, reader=reader)
        reader.assert_not_called()
        self.assertEqual(self.count(), 1)

    def test_bad_submitted_id_ref_title_url_and_read_time_are_not_accepted(self):
        item = self.perform()
        for key, value in (("source_id", "other-id"), ("title", "invented title"),
                           ("public_url", "https://docs.example.org/other"),
                           ("retrieved_at", "2000-01-01T00:00:00Z"), ("receipt_ref", "nonexistent"),
                           ("read_status", "not_read"), ("snapshot_relative_path", "invented.txt")):
            bad = deepcopy(item)
            bad["external_source"][key] = value
            with self.subTest(key=key), self.assertRaises(ContractError):
                self.validate(bad)

    def test_tampered_or_incomplete_receipts_cannot_replay_or_validate(self):
        item = self.perform()
        ident = item["receipt"]["receipt_id"]
        original = self.conn.execute("SELECT receipt_json,content FROM read_receipts WHERE receipt_id=?", (ident,)).fetchone()
        for changes in ({"read_complete": False}, {"bytes_received": None}, {"http_status": 403},
                        {"sha256": "0" * 64}, {"source_id": "wrong-id"}, {"receipt_id": "wrong-ref"},
                        {"title": "changed-title"}, {"reader_id": ""}, {"allowed_hosts": ["127.0.0.1"]},
                        {"content_sha256": "0" * 64}, {"seal_sha256": "0" * 64}, {"created_at": "invented"},
                        {"retrieved_at": "2000-01-01T00:00:00Z"}, {"created_at": "2000-01-01T00:00:00Z"}):
            self.conn.execute("UPDATE read_receipts SET receipt_json=? WHERE receipt_id=?", (original[0], ident))
            self.edit_receipt(ident, **changes)
            reader = Mock(return_value=response())
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.validate(item)
            with self.assertRaises(ContractError):
                self.perform(reader=reader)
            reader.assert_not_called()
        self.conn.execute("UPDATE read_receipts SET receipt_json=?,content='changed' WHERE receipt_id=?", (original[0], ident))
        with self.assertRaises(ContractError):
            self.validate(item)
        self.conn.execute("UPDATE read_receipts SET content=NULL WHERE receipt_id=?", (ident,))
        with self.assertRaises(ContractError):
            self.validate(item)

    def test_oversized_stored_evidence_is_rejected_before_receipt_allocation(self):
        item = self.perform()
        ident = item["receipt"]["receipt_id"]
        self.conn.execute("UPDATE read_receipts SET content=? WHERE receipt_id=?", ("X" * (MAX_BYTES + 1), ident))
        with patch.object(self.analysis, "receipt") as loader, self.assertRaises(ContractError):
            self.validate(item)
        loader.assert_not_called()

    def test_read_source_matches_existing_schema_without_completing_task_or_blocks(self):
        item = self.perform()
        result = self.blocked_result([item["external_source"]])
        task = self.analysis._task(self.task_id)
        self.analysis._validate_result(task, result, self.bundle)
        validate_receipt(self.analysis, task, item["external_source"], bundle=self.bundle)
        self.assertEqual(result["execution_status"], "blocked")
        self.assertEqual(result["assertions"], [])
        self.assertEqual(self.analysis._task(self.task_id)["state"], "running")
        block = self.bundle["blocks"][0]
        with self.assertRaises(ContractError):
            self.analysis._has_read(task, block, self.bundle, [item["receipt"]["receipt_id"]])

    def test_competing_same_id_different_content_is_rejected(self):
        def competing(url, *, max_bytes):
            self.assertEqual(max_bytes, MAX_BYTES)
            self.perform(reader=Mock(return_value=response()))
            return response("Different complete public source text.")
        with self.assertRaises(ContractError):
            self.perform(reader=competing)
        original = self.perform()
        self.assertEqual(original["text"], TEXT)
        self.assertEqual(self.count(), 1)

    def test_pause_before_or_during_read_never_issues_a_receipt(self):
        reader = Mock(return_value=response())
        self.store.control("analysis", "pause")
        self.assertEqual(self.perform(reader=reader)["status"], "blocked")
        reader.assert_not_called()
        self.store.control("analysis", "resume")
        def pausing(url, *, max_bytes):
            self.store.control("analysis", "pause")
            return response()
        self.assertEqual(self.perform(reader=pausing)["status"], "blocked")
        self.assertEqual(self.count(), 0)

    def test_receipt_save_and_postvalidation_share_existing_transaction(self):
        save = self.analysis._save_receipt
        def corrupt(task, block_id, kind, receipt, content=None):
            saved = save(task, block_id, kind, receipt, content)
            self.conn.execute("UPDATE read_receipts SET content='corrupt' WHERE receipt_id=?", (saved["receipt_id"],))
            return saved
        with patch.object(self.analysis, "_save_receipt", side_effect=corrupt), self.assertRaises(ContractError):
            self.perform()
        self.assertEqual(self.count(), 0)


if __name__ == "__main__":
    unittest.main()
