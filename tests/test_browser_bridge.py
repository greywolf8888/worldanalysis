"""Synthetic/temp-Store contract tests. No real browser, account or data access."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from worldanalysis.browser_bridge import BrowserBridge, SCHEMA
from worldanalysis.policy import GateError
from worldanalysis.store import Store

ROOT = "/apps/教育分析学习"
SHARE_ROOT = "/synthetic-course"
DESTINATION = ROOT + "/待分析/synthetic-course"


def item(fid="1", path=SHARE_ROOT + "/a.mp4", size=12, isdir=0):
    return dict(fs_id=fid, path=path, server_filename=path.rsplit("/", 1)[-1], size=size, isdir=isdir)


def receipt(generation="generation-1", index=1, folder=SHARE_ROOT, rows=None, **updates):
    value = dict(schema=SCHEMA, evidence_class="synthetic", request_generation_id=generation,
                 receipt_id=f"{generation}-{index}", source_type="share", source_share_ref="synthetic-share",
                 current_expected_folder=folder, endpoint_path="/share/list", read_path_evidence_ref="synthetic-ui-path",
                 http_status=200, errno=0, state="observed", folder_evidence="response_folder",
                 folder_matches=True, owner_matches=None, list=[item()] if rows is None else rows,
                 pagination={"page": 1, "has_more": False}, issues=[])
    value.update(updates)
    return value


class BrowserBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wa-synthetic-bridge-")
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: self.store.close())
        self.project = self.store.create_project(author="Synthetic author", topic="Synthetic topic")
        self.other = self.store.create_project(author="Other synthetic author", topic="Synthetic topic")
        self.grants = {"host-grant": dict(state="granted", grant_ref="host-grant", project_id=self.project,
            source_ref="synthetic-share", allowed_root=ROOT, operations=["inventory", "transfer"],
            expires_at="2099-01-01T00:00:00Z", current_goal_evidence_ref="host-current-goal", user_grant_evidence_ref="host-user-turn")}
        self.goals = {self.project: dict(project_id=self.project, evidence_ref="host-current-goal", state="active")}
        self.bridge = self.new_bridge()
        self.bridge.register_host_grant("host-grant")
        self.begin()
        self.bridge.arm_folder(self.project, "inventory-1", SHARE_ROOT, "generation-1")

    def new_bridge(self, **updates):
        options = dict(host_grant_resolver=lambda ref: deepcopy(self.grants.get(ref)),
                       current_goal_resolver=lambda project: deepcopy(self.goals.get(project)), allow_synthetic=True)
        options.update(updates)
        return BrowserBridge(self.store.connection, **options)

    def begin(self, enumeration="inventory-1", **updates):
        options = dict(grant_ref="host-grant", source_ref="synthetic-share", source_type="share",
                       root_folder=SHARE_ROOT, read_path_evidence_ref="synthetic-ui-path", evidence_class="synthetic")
        options.update(updates)
        return self.bridge.begin_inventory(self.project, enumeration, **options)

    def record(self, value=None, enumeration="inventory-1"):
        return self.bridge.record_receipt(self.project, enumeration, receipt() if value is None else value)

    def snapshot(self, folder=SHARE_ROOT, enumeration="inventory-1"):
        return self.bridge.folder_inventory(self.project, enumeration, folder)

    def manifest(self):
        return self.bridge.transfer_manifest(self.project, "inventory-1")

    def assert_gate(self, code, callable_, *args, **kwargs):
        with self.assertRaises(GateError) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_store_connection_only_and_no_second_business_database(self):
        db = sqlite3.connect(":memory:")
        try:
            self.assert_gate("STORE_CONNECTION_REQUIRED", BrowserBridge, db)
        finally:
            db.close()
        self.assertIs(self.bridge.connection, self.store.connection)
        self.assertEqual(len(self.store.connection.execute("PRAGMA database_list").fetchall()), 1)
        self.assertEqual(list(self.root.rglob("*.sqlite3")), [self.store.db_path])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 2)

    def test_recovery_grants_receipts_and_schema_are_idempotent(self):
        first = self.record()
        self.record()
        self.bridge.register_host_grant("host-grant")
        self.store.close(); self.store = Store(self.root); self.bridge = self.new_bridge()
        self.assertEqual(self.record(), first)
        self.assertEqual(self.manifest()["manifest"], [{"source_file_id": "1", "relative_path": "a.mp4", "size_bytes": 12, "is_directory": False}])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM browser_user_grants").fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM browser_inventory_receipts").fetchone()[0], 1)

    def test_plan_string_or_persisted_record_without_live_host_cannot_authorize(self):
        bridge = self.new_bridge(host_grant_resolver=None)
        self.assert_gate("HOST_USER_GRANT_REQUIRED", bridge.register_host_grant, "plan-approved")
        self.assert_gate("HOST_USER_GRANT_REQUIRED", bridge.resolve_grant, "host-grant", project_id=self.project)
        self.grants["plan-approved"] = {"state": "granted", "grant_ref": "plan-approved"}
        self.assert_gate("HOST_USER_GRANT_REQUIRED", self.bridge.register_host_grant, "plan-approved")
        self.record()
        self.store.close(); self.store = Store(self.root); bridge = BrowserBridge(self.store.connection, allow_synthetic=True)
        self.assert_gate("HOST_USER_GRANT_REQUIRED", bridge.transfer_manifest, self.project, "inventory-1")

    def test_current_grant_root_ops_expiry_and_goal_evidence_are_live(self):
        self.record()
        original = deepcopy(self.grants["host-grant"])
        for change, code in [({"expires_at": "2000-01-01T00:00:00Z"}, "AUTHORIZATION_EXPIRED"),
                             ({"state": "revoked"}, "HOST_USER_GRANT_REQUIRED"),
                             ({"operations": ["inventory"]}, "REGISTERED_GRANT_CHANGED"),
                             ({"source_ref": "different-share"}, "REGISTERED_GRANT_CHANGED"),
                             ({"allowed_root": ROOT + "其他"}, "OUTSIDE_AUTHORIZED_ROOT")]:
            with self.subTest(change=change):
                self.grants["host-grant"] = {**original, **change}
                self.assert_gate(code, self.bridge.prepare_transfer, self.project, "inventory-1", DESTINATION)
        self.grants["host-grant"] = original
        for update in [{"state": "paused"}, {"evidence_ref": "different-goal"}]:
            self.goals[self.project].update(update)
            self.assert_gate("CURRENT_GOAL_EVIDENCE_REQUIRED", self.manifest)
            self.goals[self.project] = dict(project_id=self.project, evidence_ref="host-current-goal", state="active")

    def test_missing_goal_and_invalid_grant_fields_are_not_trusted(self):
        self.assert_gate("HOST_USER_GRANT_REQUIRED", self.new_bridge(current_goal_resolver=None).register_host_grant, "host-grant")
        original = deepcopy(self.grants["host-grant"])
        for update in [{"operations": ["inventory", "inventory"]}, {"operations": ["generation"]}, {"expires_at": "2099-01-01"}, {"source_ref": "https://not-an-opaque-reference"}, {"Cookie": "SYNTHETIC_DO_NOT_ACCEPT"}]:
            with self.subTest(update=list(update)):
                self.grants["host-grant"] = {**original, **update}
                with self.assertRaises(GateError):
                    self.bridge.register_host_grant("host-grant")
        self.grants["host-grant"] = original

    def test_cross_project_scope_and_generation_identity_never_merge(self):
        self.assert_gate("CURRENT_AUTHORIZATION_REQUIRED", self.bridge.begin_inventory, self.other, "other-inventory", grant_ref="host-grant", source_ref="synthetic-share", source_type="share", root_folder=SHARE_ROOT, read_path_evidence_ref="synthetic-ui-path", evidence_class="synthetic")
        self.assert_gate("UNKNOWN_INVENTORY_SCOPE", self.bridge.record_receipt, self.other, "inventory-1", receipt())
        self.assert_gate("UNKNOWN_INVENTORY_SCOPE", self.bridge.transfer_manifest, self.other, "inventory-1")
        self.assert_gate("INVENTORY_SCOPE_CHANGED", self.begin, root_folder="/different")
        self.assert_gate("GRANT_SCOPE_MISMATCH", self.begin, "other-inventory", source_ref="different-share")
        self.assert_gate("FOLDER_OUTSIDE_INVENTORY", self.bridge.arm_folder, self.project, "inventory-1", SHARE_ROOT + "-sibling", "generation-2")

    def test_persisted_receipt_corruption_cannot_restore_as_a_complete_manifest(self):
        self.record()
        changed = receipt(rows=[item(size=999)])
        self.store.connection.execute("UPDATE browser_inventory_receipts SET receipt_json=?", (json.dumps(changed),))
        self.assert_gate("PERSISTED_RECEIPT_CHANGED", self.snapshot)
        self.assert_gate("PERSISTED_RECEIPT_CHANGED", self.manifest)
        self.assert_gate("PERSISTED_RECEIPT_CHANGED", self.record)

    def test_old_generation_wrong_folder_and_wrong_source_receipts_are_rejected(self):
        self.bridge.arm_folder(self.project, "inventory-1", SHARE_ROOT, "generation-2")
        self.assert_gate("STALE_OR_UNKNOWN_GENERATION", self.record, receipt())
        self.assert_gate("GENERATION_ID_REUSE", self.bridge.arm_folder, self.project, "inventory-1", SHARE_ROOT, "generation-1")
        for update in [{"source_share_ref": "other-share"}, {"source_type": "user_disk"}, {"current_expected_folder": "/wrong"}, {"endpoint_path": "/share/list?bdstoken=DO_NOT_ACCEPT"}, {"read_path_evidence_ref": "wrong-evidence"}]:
            with self.subTest(update=update):
                self.assert_gate("RECEIPT_SCOPE_MISMATCH", self.record, receipt("generation-2", **update))
        self.record(receipt("generation-2")); self.assertTrue(self.snapshot()["enumeration_complete"])

    def test_observation_and_receipt_ids_are_immutable_not_overwritten(self):
        self.record()
        self.assert_gate("RECEIPT_ID_COLLISION", self.record, receipt(rows=[item(size=99)]))
        self.assertEqual(self.snapshot()["items"][0]["size"], 12)
        self.assertEqual(self.begin()["root_folder"], SHARE_ROOT)

    def test_partial_unknown_and_empty_unbound_receipts_do_not_make_a_manifest(self):
        self.record(receipt(rows=[], state="unknown", folder_evidence="missing", folder_matches=None, issues=["MISSING_FOLDER_EVIDENCE"]))
        self.assertFalse(self.snapshot()["enumeration_complete"])
        self.assert_gate("SOURCE_ENUMERATION_INCOMPLETE", self.manifest)
        self.bridge.arm_folder(self.project, "inventory-1", SHARE_ROOT, "generation-2")
        self.record(receipt("generation-2", rows=[]))
        self.assertTrue(self.snapshot()["enumeration_complete"])
        self.assert_gate("EMPTY_SHARE_MANIFEST", self.manifest)

    def test_visible_ten_of_num20_with_unknown_page_or_terminal_is_partial(self):
        self.record(receipt(rows=[item(str(i), SHARE_ROOT + f"/{i}.mp4") for i in range(10)], pagination={"page_size": 20}))
        self.assertFalse(self.snapshot()["enumeration_complete"])
        self.assertIn("PAGINATION_UNKNOWN_OR_INCOMPLETE", self.snapshot()["issues"])
        self.assert_gate("SOURCE_ENUMERATION_INCOMPLETE", self.manifest)

    def test_all_numbered_pages_with_exact_terminal_and_consistent_total_required(self):
        self.record(receipt(index=2, rows=[item("2", SHARE_ROOT + "/b.txt")], pagination={"page": 2, "has_more": False, "total": 2}))
        self.assertFalse(self.snapshot()["enumeration_complete"])
        self.record(receipt(index=1, pagination={"page": 1, "has_more": True, "total": 2}))
        self.assertTrue(self.snapshot()["enumeration_complete"])
        self.assertEqual(len(self.manifest()["manifest"]), 2)

    def test_missing_page_gap_or_wrong_total_is_not_complete(self):
        for number, total in [(3, 2), (2, 3)]:
            with self.subTest(number=number, total=total):
                generation = "gap-" + str(number)
                self.bridge.arm_folder(self.project, "inventory-1", SHARE_ROOT, generation)
                self.record(receipt(generation, pagination={"page": 1, "has_more": True, "total": total}))
                self.record(receipt(generation, index=2, rows=[item("2", SHARE_ROOT + "/b")], pagination={"page": number, "has_more": False, "total": total}))
                self.assertFalse(self.snapshot()["enumeration_complete"])
                self.assert_gate("SOURCE_ENUMERATION_INCOMPLETE", self.manifest)

    def test_duplicate_page_and_cross_page_ids_paths_are_explicit_conflicts(self):
        cases = [(item(), 1, "DUPLICATE_PAGE"), (item("1", SHARE_ROOT + "/b"), 2, "DUPLICATE_FILE_ID"), (item("2", SHARE_ROOT + "/A.MP4"), 2, "DUPLICATE_PATH")]
        for index, (row, page, issue) in enumerate(cases):
            with self.subTest(issue=issue):
                generation = "duplicates-" + str(index)
                self.bridge.arm_folder(self.project, "inventory-1", SHARE_ROOT, generation)
                self.record(receipt(generation, pagination={"page": 1, "has_more": True}))
                self.record(receipt(generation, index=2, rows=[row], pagination={"page": page, "has_more": False}))
                self.assertEqual(self.snapshot()["state"], "conflict")
                self.assertIn(issue, self.snapshot()["issues"])
                self.assert_gate("SOURCE_ENUMERATION_INCOMPLETE", self.manifest)

    def test_cursor_chain_requires_start_no_cycle_and_real_terminal(self):
        self.record(receipt(pagination={"cursor": "", "next_cursor": "next_2", "has_more": True}))
        self.assertFalse(self.snapshot()["enumeration_complete"])
        self.record(receipt(index=2, rows=[item("2", SHARE_ROOT + "/b")], pagination={"cursor": "next_2", "next_cursor": "", "has_more": False}))
        self.assertTrue(self.snapshot()["enumeration_complete"])
        self.bridge.arm_folder(self.project, "inventory-1", SHARE_ROOT, "cyclic")
        self.record(receipt("cyclic", pagination={"cursor": "", "next_cursor": "next_2", "has_more": True}))
        self.record(receipt("cyclic", index=2, rows=[item("2", SHARE_ROOT + "/b")], pagination={"cursor": "next_2", "next_cursor": "next_2", "has_more": True}))
        self.assertFalse(self.snapshot()["enumeration_complete"])

    def test_recursive_manifest_requires_every_child_enumerated_and_exact_sizes(self):
        child = SHARE_ROOT + "/nested"
        self.record(receipt(rows=[item(), item("2", child, 0, 1)]))
        self.assertTrue(self.snapshot()["enumeration_complete"])
        self.assert_gate("SOURCE_ENUMERATION_INCOMPLETE", self.manifest)
        self.bridge.arm_folder(self.project, "inventory-1", child, "nested-1")
        self.record(receipt("nested-1", folder=child, rows=[item("3", child + "/note.txt", None)], state="partial", issues=["EXACT_SIZE_UNVERIFIED"]))
        self.assert_gate("SOURCE_ENUMERATION_INCOMPLETE", self.manifest)
        self.bridge.arm_folder(self.project, "inventory-1", child, "nested-2")
        self.record(receipt("nested-2", folder=child, rows=[item("3", child + "/note.txt", 5)]))
        result = self.manifest()
        self.assertEqual([row["relative_path"] for row in result["manifest"]], ["a.mp4", "nested", "nested/note.txt"])
        self.assertEqual(result["evidence_class"], "synthetic")
        self.assertEqual(result["request_generation_ids"], ["generation-1", "nested-2"])

    def test_cross_folder_duplicate_fsid_and_orphan_folder_are_rejected(self):
        child = SHARE_ROOT + "/nested"
        self.record(receipt(rows=[item("1", child, 0, 1)]))
        self.bridge.arm_folder(self.project, "inventory-1", child, "nested-1")
        self.record(receipt("nested-1", folder=child, rows=[item("1", child + "/note.txt", 5)]))
        self.assert_gate("RECURSIVE_ID_OR_PATH_CONFLICT", self.manifest)
        self.bridge.arm_folder(self.project, "inventory-1", child, "nested-2")
        self.record(receipt("nested-2", folder=child, rows=[]))
        self.bridge.arm_folder(self.project, "inventory-1", SHARE_ROOT + "/orphan", "orphan-1")
        self.assert_gate("ORPHAN_FOLDER_OBSERVATION", self.manifest)

    def test_source_identity_and_user_disk_destination_identity_are_independent(self):
        self.record()
        self.assertEqual(self.snapshot()["items"][0]["assetSourceId"], "1")
        self.assertIsNone(self.snapshot()["items"][0]["destinationId"])
        self.begin("owned-inventory", source_type="user_disk", root_folder=DESTINATION)
        self.bridge.arm_folder(self.project, "owned-inventory", DESTINATION, "owned-generation")
        self.record(receipt("owned-generation", folder=DESTINATION, rows=[item("999", DESTINATION + "/a.mp4")], source_type="user_disk", endpoint_path="/api/list"), "owned-inventory")
        owned = self.snapshot(DESTINATION, "owned-inventory")["items"][0]
        self.assertIsNone(owned["assetSourceId"]); self.assertEqual(owned["destinationId"], "999")
        self.assert_gate("SHARE_MANIFEST_REQUIRED", self.bridge.transfer_manifest, self.project, "owned-inventory")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='cloud_bindings'").fetchone()[0], 0)

    def test_user_disk_and_transfer_destination_must_stay_in_exact_allowed_root(self):
        self.record()
        for path in [ROOT + "其他/x", "/apps/bdpan/x", ROOT + "/../escape", ROOT + "/%2e%2e/escape"]:
            with self.subTest(path=path), self.assertRaises(GateError):
                self.begin("invalid-root", source_type="user_disk", root_folder=path)
            with self.subTest(destination=path), self.assertRaises(GateError):
                self.bridge.prepare_transfer(self.project, "inventory-1", path)
        self.assert_gate("RECEIPT_FOLDER_MISMATCH", self.record, receipt(rows=[item(path=SHARE_ROOT + "/nested/a.mp4")]))

    def test_invalid_paths_size_kind_and_identifier_cannot_become_exact(self):
        for update in [{"size": -1}, {"size": True}, {"size": 1.2}, {"size": "12"}, {"size": 2**53}, {"isdir": False}, {"isdir": "0"}, {"fs_id": 1}, {"fs_id": "bad-id"}, {"server_filename": "wrong"}, {"path": SHARE_ROOT + "/../a.mp4"}, {"path": SHARE_ROOT + "/a.mp4/"}]:
            with self.subTest(update=update), self.assertRaises(GateError):
                self.record(receipt(rows=[{**item(), **update}]))
        self.record(receipt(rows=[item(size=None)], state="partial", issues=["EXACT_SIZE_UNVERIFIED"]))
        self.assertFalse(self.snapshot()["exact_sizes_verified"])
        self.assert_gate("SOURCE_ENUMERATION_INCOMPLETE", self.manifest)

    def test_sensitive_unknown_fields_or_urls_rejected_without_echo_or_persistence(self):
        values = [receipt(Cookie="SYNTHETIC_SECRET"), receipt(owner_uk="SYNTHETIC_SECRET"),
                  receipt(rows=[{**item(), "dlink": "https://secret.invalid/?sign=SYNTHETIC_SECRET"}]),
                  receipt(pagination={"cursor": "https://secret.invalid/?sign=SYNTHETIC_SECRET"}),
                  receipt(issues=["SYNTHETIC_SECRET"])]
        for value in values:
            with self.assertRaises(GateError) as caught:
                self.record(value)
            self.assertNotIn("SYNTHETIC_SECRET", str(caught.exception))
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM browser_inventory_receipts").fetchone()[0], 0)

    def test_passive_denial_is_durable_and_cannot_rearm_or_change_channel(self):
        denied = receipt(rows=[], http_status=403, errno=None, state="blocked", folder_evidence="missing", folder_matches=None, issues=["HTTP_ACCESS_DENIED", "UNREADABLE_RESPONSE"])
        self.record(denied); self.assertEqual(self.record(denied), denied)
        self.assertEqual(self.snapshot()["state"], "blocked")
        self.store.close(); self.store = Store(self.root); self.bridge = self.new_bridge()
        self.assertEqual(self.snapshot()["state"], "blocked")
        self.assert_gate("CHANNEL_BLOCKED", self.bridge.arm_folder, self.project, "inventory-1", SHARE_ROOT, "generation-2")
        self.assert_gate("CHANNEL_BLOCKED", self.begin, "new-channel-inventory")
        self.assert_gate("CHANNEL_BLOCKED", self.begin)
        self.assert_gate("CHANNEL_BLOCKED", self.manifest)

    def test_provider_errno_and_captcha_block_even_with_http_200(self):
        for index, (errno, issues) in enumerate([(113, ["PROVIDER_ACCESS_DENIED"]), (401, ["PROVIDER_ACCESS_DENIED"]), (429, ["PROVIDER_ACCESS_DENIED"]), (0, ["CAPTCHA_REQUIRED"])]):
            grant_ref, source_ref = f"blocked-grant-{index}", f"blocked-source-{index}"
            self.grants[grant_ref] = {**self.grants["host-grant"], "grant_ref": grant_ref, "source_ref": source_ref}
            self.bridge.register_host_grant(grant_ref)
            enumeration, generation = f"blocked-inventory-{index}", f"blocked-generation-{index}"
            self.begin(enumeration, grant_ref=grant_ref, source_ref=source_ref)
            self.bridge.arm_folder(self.project, enumeration, SHARE_ROOT, generation)
            self.record(receipt(generation, rows=[], source_share_ref=source_ref, errno=errno, state="blocked", folder_matches=None, folder_evidence="missing", issues=issues), enumeration)
            self.assertEqual(self.snapshot(enumeration=enumeration)["state"], "blocked")

    def test_synthetic_is_explicit_and_not_a_production_fallback(self):
        production = self.new_bridge(allow_synthetic=False)
        self.assert_gate("SYNTHETIC_OBSERVATION_NOT_ALLOWED", production.begin_inventory, self.project, "production-inventory", grant_ref="host-grant", source_ref="synthetic-share", source_type="share", root_folder=SHARE_ROOT, read_path_evidence_ref="synthetic-ui-path", evidence_class="synthetic")
        self.assertEqual(self.begin()["evidence_class"], "synthetic")
        self.record()
        self.assert_gate("SYNTHETIC_OBSERVATION_NOT_ALLOWED", production.transfer_manifest, self.project, "inventory-1")
        self.assert_gate("SYNTHETIC_OBSERVATION_NOT_ALLOWED", production.record_receipt, self.project, "inventory-1", receipt())

    def test_host_callback_errors_are_sanitized_not_echoed(self):
        def failing(_):
            raise RuntimeError("SYNTHETIC_SECRET_CALLBACK_DETAIL")
        for bridge, code in [(self.new_bridge(host_grant_resolver=failing), "HOST_GRANT_RESOLUTION_FAILED"),
                             (self.new_bridge(current_goal_resolver=failing), "CURRENT_GOAL_RESOLUTION_FAILED")]:
            with self.assertRaises(GateError) as caught:
                bridge.register_host_grant("host-grant")
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("SYNTHETIC_SECRET", str(caught.exception))

    def test_terminal_cursor_conflict_and_invalid_row_proof_are_not_complete(self):
        self.record(receipt(pagination={"page": 1, "has_more": False, "next_cursor": "not_terminal"}))
        self.assertEqual(self.snapshot()["state"], "conflict")
        self.assertIn("PAGINATION_CONFLICT", self.snapshot()["issues"])
        self.assert_gate("SOURCE_ENUMERATION_INCOMPLETE", self.manifest)
        self.bridge.arm_folder(self.project, "inventory-1", SHARE_ROOT, "invalid-row")
        self.record(receipt("invalid-row", rows=[], state="unknown", issues=["INVALID_LIST_ITEM"]))
        self.assertFalse(self.snapshot()["exact_sizes_verified"])

    def test_prepare_cannot_commit_a_callers_active_store_transaction(self):
        self.record()
        with self.store.transaction():
            self.assert_gate("ACTIVE_STORE_TRANSACTION", self.bridge.prepare_transfer, self.project, "inventory-1", DESTINATION)
            self.assertTrue(self.store.connection.in_transaction)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='cloud_operations'").fetchone()[0], 0)

    def test_prepare_transfer_only_queues_complete_manifest_on_same_connection(self):
        self.assert_gate("SOURCE_ENUMERATION_INCOMPLETE", self.bridge.prepare_transfer, self.project, "inventory-1", DESTINATION)
        self.record()
        with patch("worldanalysis.acquisition.fetch_public_article", side_effect=AssertionError("no network allowed")):
            first = self.bridge.prepare_transfer(self.project, "inventory-1", DESTINATION)
            second = self.bridge.prepare_transfer(self.project, "inventory-1", DESTINATION)
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(first["state"], "queued")
        self.assertFalse(first["ui_transfer_implemented"])
        self.assertIsNone(first["remote_task_ref"])
        self.assertEqual(first["browser_evidence_class"], "synthetic")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM cloud_operations").fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM cloud_bindings").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
