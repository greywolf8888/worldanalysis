"""Redacted normal-UI inventory receipts on the *existing* Store.connection.

Host integration (no browser or HTTP implementation lives in this module)::

    bridge = BrowserBridge(store.connection, host_grant_resolver=host_user_grant,
                           current_goal_resolver=host_current_goal)
    bridge.register_host_grant("host-grant-ref")  # callback, never a Plan grant
    bridge.begin_inventory(project_id, "inventory-ref", grant_ref="host-grant-ref",
        source_ref="opaque-source-ref", source_type="share", root_folder="/course",
        read_path_evidence_ref="operator-observed-read-path")
    arm = bridge.arm_folder(project_id, "inventory-ref", "/course", "generation-1")
    # Operator adds browser-private origin and observed_read_paths, then arms the
    # JS observer. Normal UI refresh/pagination creates the requests, not us.
    bridge.record_receipt(project_id, "inventory-ref", redacted_ui_receipt)
    manifest = bridge.transfer_manifest(project_id, "inventory-ref")

The injected host callbacks must resolve an actual current human grant and goal;
model/Plan strings are not authority. Origins, credentials, URLs and raw owner
UKs are neither accepted nor stored. Synthetic evidence requires explicit test
mode and remains marked synthetic. A manifest is not a submitted transfer.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
import sqlite3
import unicodedata
import uuid

from .acquisition import Acquisition
from .policy import GateError, Policy, local_relative

SCHEMA = "worldanalysis.browser-inventory-receipt.v1"
SAFE_INTEGER = 2**53 - 1
DENIALS = {113, 401, 403, 429}
ISSUES = frozenset({
    "HTTP_ACCESS_DENIED", "PROVIDER_ACCESS_DENIED", "CAPTCHA_REQUIRED",
    "UNREADABLE_RESPONSE", "INVALID_RESPONSE", "NON_SUCCESS_RESPONSE",
    "AMBIGUOUS_LIST_CONTAINER", "INVALID_LIST", "INVALID_PAGINATION",
    "INVALID_OWNER_EVIDENCE", "OWNER_NOT_VERIFIED", "CONFLICTING_FOLDER_EVIDENCE",
    "INVALID_FOLDER_EVIDENCE", "EXACT_SIZE_UNVERIFIED", "INVALID_LIST_ITEM",
    "MISSING_FOLDER_EVIDENCE", "FOLDER_MISMATCH",
})
RECEIPT_FIELDS = frozenset({
    "schema", "evidence_class", "request_generation_id", "receipt_id", "source_type",
    "source_share_ref", "current_expected_folder", "endpoint_path", "read_path_evidence_ref",
    "http_status", "errno", "state", "folder_evidence", "folder_matches", "owner_matches",
    "list", "pagination", "issues",
})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _reference(value, *, limit=128):
    if not isinstance(value, str) or len(value) > limit or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise GateError("OPAQUE_REFERENCE_REQUIRED")
    return value


def _path(value):
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 4096 or value != unicodedata.normalize("NFC", value) or any(c in value for c in "\\%?#:") or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise GateError("INVALID_OBSERVATION_PATH")
    if value != "/" and value.endswith("/"):
        value = value[:-1]
    if value != "/" and any(p in ("", ".", "..") for p in value[1:].split("/")):
        raise GateError("INVALID_OBSERVATION_PATH")
    return value


def _inside(path, root):
    return root == "/" or path == root or path.startswith(root + "/")


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= SAFE_INTEGER


def _expires(value):
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError("expired")
    except (AttributeError, ValueError, TypeError):
        raise GateError("AUTHORIZATION_EXPIRED") from None
    return value


class BrowserBridge:
    """No separate DB, request channel, transfer UI, download or success adapter."""

    def __init__(self, connection: sqlite3.Connection, policy: Policy | None = None, *,
                 host_grant_resolver=None, current_goal_resolver=None, allow_synthetic=False):
        if not isinstance(connection, sqlite3.Connection) or not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'").fetchone():
            raise GateError("STORE_CONNECTION_REQUIRED")
        self.connection = connection
        self.policy = policy or Policy()
        self.host_grant_resolver = host_grant_resolver
        self.current_goal_resolver = current_goal_resolver
        self.allow_synthetic = allow_synthetic is True
        # These tables contain authorization/observation evidence only. Store and
        # Acquisition remain the sole business project/operation ledger.
        for sql in (
            "CREATE TABLE IF NOT EXISTS browser_user_grants(grant_ref TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects, grant_json TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS browser_inventory_scopes(enumeration_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects, source_ref TEXT NOT NULL, source_type TEXT NOT NULL, root_folder TEXT NOT NULL, grant_ref TEXT NOT NULL REFERENCES browser_user_grants, read_path_evidence_ref TEXT NOT NULL, evidence_class TEXT NOT NULL, state TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS browser_inventory_generations(request_generation_id TEXT PRIMARY KEY, enumeration_id TEXT NOT NULL REFERENCES browser_inventory_scopes, folder TEXT NOT NULL, state TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS browser_inventory_receipts(request_generation_id TEXT NOT NULL REFERENCES browser_inventory_generations, receipt_id TEXT NOT NULL, digest TEXT NOT NULL, receipt_json TEXT NOT NULL, PRIMARY KEY(request_generation_id,receipt_id))",
            "CREATE INDEX IF NOT EXISTS browser_inventory_current ON browser_inventory_generations(enumeration_id,folder,state)",
        ):
            connection.execute(sql)

    @contextmanager
    def _transaction(self):
        name = "browser_" + uuid.uuid4().hex
        self.connection.execute("SAVEPOINT " + name)
        try:
            yield
            self.connection.execute("RELEASE SAVEPOINT " + name)
        except BaseException:
            self.connection.execute("ROLLBACK TO SAVEPOINT " + name)
            self.connection.execute("RELEASE SAVEPOINT " + name)
            raise

    def _row(self, sql, params=()):
        cursor = self.connection.execute(sql, params)
        row = cursor.fetchone()
        return None if row is None else dict(zip([c[0] for c in cursor.description], row))

    def _project(self, project_id):
        _reference(project_id)
        if not self.connection.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
            raise GateError("UNKNOWN_PROJECT")

    def _host_grant(self, grant_ref):
        if not callable(self.host_grant_resolver) or not callable(self.current_goal_resolver):
            raise GateError("HOST_USER_GRANT_REQUIRED")
        grant_ref = _reference(grant_ref)
        try:
            raw = self.host_grant_resolver(grant_ref)
        except Exception:
            raise GateError("HOST_GRANT_RESOLUTION_FAILED") from None
        fields = {"state", "grant_ref", "project_id", "source_ref", "allowed_root", "operations", "expires_at", "current_goal_evidence_ref", "user_grant_evidence_ref"}
        if type(raw) is not dict or set(raw) != fields or raw.get("state") != "granted" or raw.get("grant_ref") != grant_ref:
            raise GateError("HOST_USER_GRANT_REQUIRED")
        value = {key: _reference(raw[key]) for key in ("grant_ref", "project_id", "source_ref", "current_goal_evidence_ref", "user_grant_evidence_ref")}
        self._project(value["project_id"])
        operations = raw["operations"]
        if type(operations) is not list or not operations or any(op not in ("inventory", "transfer", "read_stream") for op in operations) or len(set(operations)) != len(operations):
            raise GateError("INVALID_GRANT_OPERATIONS")
        value.update(state="granted", allowed_root=self.policy.require_cloud_path(raw["allowed_root"]), operations=sorted(operations), expires_at=_expires(raw["expires_at"]))
        try:
            current = self.current_goal_resolver(value["project_id"])
        except Exception:
            raise GateError("CURRENT_GOAL_RESOLUTION_FAILED") from None
        if type(current) is not dict or current.get("project_id") != value["project_id"] or current.get("evidence_ref") != value["current_goal_evidence_ref"] or current.get("state") not in ("active", "running"):
            raise GateError("CURRENT_GOAL_EVIDENCE_REQUIRED")
        return value

    def register_host_grant(self, grant_ref):
        """Only a host callback supplies grant fields; callers cannot pass a Plan."""
        value = self._host_grant(grant_ref)
        serialized = _json(value)
        with self._transaction():
            old = self._row("SELECT grant_json FROM browser_user_grants WHERE grant_ref=?", (grant_ref,))
            if old and old["grant_json"] != serialized:
                raise GateError("REGISTERED_GRANT_CHANGED")
            self.connection.execute("INSERT OR IGNORE INTO browser_user_grants VALUES(?,?,?)", (grant_ref, value["project_id"], serialized))
        return value.copy()

    def resolve_grant(self, grant_ref, *, project_id):
        """Resolver for Acquisition, still checked against live host/goal on use."""
        self._project(project_id)
        row = self._row("SELECT project_id,grant_json FROM browser_user_grants WHERE grant_ref=?", (grant_ref,))
        if not row or row["project_id"] != project_id:
            raise GateError("CURRENT_AUTHORIZATION_REQUIRED")
        live = self._host_grant(grant_ref)
        if _json(live) != row["grant_json"]:
            raise GateError("REGISTERED_GRANT_CHANGED")
        return {**live, "share_refs": [live["source_ref"]], "roots": [live["allowed_root"]]}

    def _require_grant(self, project_id, grant_ref, source_ref, operation, *, destination=None):
        grant = self.resolve_grant(grant_ref, project_id=project_id)
        if grant["source_ref"] != source_ref or operation not in grant["operations"]:
            raise GateError("GRANT_SCOPE_MISMATCH")
        if destination is not None and not _inside(self.policy.require_cloud_path(destination), grant["allowed_root"]):
            raise GateError("GRANT_SCOPE_MISMATCH")
        return grant

    def begin_inventory(self, project_id, enumeration_id, *, grant_ref, source_ref, source_type,
                        root_folder, read_path_evidence_ref, evidence_class="normal_ui_passive"):
        self._project(project_id)
        _reference(enumeration_id); _reference(source_ref); _reference(read_path_evidence_ref)
        if source_type not in ("share", "user_disk"):
            raise GateError("INVALID_SOURCE_TYPE")
        if evidence_class not in ("normal_ui_passive", "synthetic") or (evidence_class == "synthetic" and not self.allow_synthetic):
            raise GateError("SYNTHETIC_OBSERVATION_NOT_ALLOWED")
        root = _path(root_folder)
        grant = self._require_grant(project_id, grant_ref, source_ref, "inventory")
        if source_type == "user_disk":
            self.policy.require_cloud_path(root)
            if not _inside(root, grant["allowed_root"]):
                raise GateError("GRANT_SCOPE_MISMATCH")
        value = dict(enumeration_id=enumeration_id, project_id=project_id, source_ref=source_ref,
                     source_type=source_type, root_folder=root, grant_ref=grant_ref,
                     read_path_evidence_ref=read_path_evidence_ref, evidence_class=evidence_class, state="observing")
        with self._transaction():
            old = self._row("SELECT * FROM browser_inventory_scopes WHERE enumeration_id=?", (enumeration_id,))
            if self.connection.execute("SELECT 1 FROM browser_inventory_scopes WHERE project_id=? AND source_ref=? AND state='blocked'", (project_id, source_ref)).fetchone():
                raise GateError("CHANNEL_BLOCKED")
            if old and old != value:
                raise GateError("INVENTORY_SCOPE_CHANGED")
            self.connection.execute("INSERT OR IGNORE INTO browser_inventory_scopes VALUES(?,?,?,?,?,?,?,?,?)", tuple(value.values()))
        return value

    def _scope(self, project_id, enumeration_id):
        self._project(project_id)
        row = self._row("SELECT * FROM browser_inventory_scopes WHERE enumeration_id=? AND project_id=?", (enumeration_id, project_id))
        if not row:
            raise GateError("UNKNOWN_INVENTORY_SCOPE")
        if row["evidence_class"] == "synthetic" and not self.allow_synthetic:
            raise GateError("SYNTHETIC_OBSERVATION_NOT_ALLOWED")
        return row

    def arm_folder(self, project_id, enumeration_id, folder, request_generation_id):
        scope = self._scope(project_id, enumeration_id)
        if scope["state"] == "blocked":
            raise GateError("CHANNEL_BLOCKED")
        self._require_grant(project_id, scope["grant_ref"], scope["source_ref"], "inventory")
        folder = _path(folder); _reference(request_generation_id)
        if not _inside(folder, scope["root_folder"]):
            raise GateError("FOLDER_OUTSIDE_INVENTORY")
        if scope["source_type"] == "user_disk":
            self.policy.require_cloud_path(folder)
        with self._transaction():
            old = self._row("SELECT * FROM browser_inventory_generations WHERE request_generation_id=?", (request_generation_id,))
            if old and (old["enumeration_id"] != enumeration_id or old["folder"] != folder or old["state"] != "active"):
                raise GateError("GENERATION_ID_REUSE")
            if not old:
                self.connection.execute("UPDATE browser_inventory_generations SET state='superseded' WHERE enumeration_id=? AND folder=? AND state='active'", (enumeration_id, folder))
                self.connection.execute("INSERT INTO browser_inventory_generations VALUES(?,?,?,'active')", (request_generation_id, enumeration_id, folder))
        # Browser-private origin and actual pure-read endpoint evidence are added
        # by the operator, not derived from a URL or credential in Python.
        return dict(current_expected_folder=folder, source_share_ref=scope["source_ref"],
                    source_type=scope["source_type"], request_generation_id=request_generation_id,
                    evidence_class=scope["evidence_class"])

    def _validate_receipt(self, scope, generation, raw):
        if type(raw) is not dict or set(raw) != RECEIPT_FIELDS:
            raise GateError("REDACTED_RECEIPT_SCHEMA_REQUIRED")
        expected = {"schema": SCHEMA, "evidence_class": scope["evidence_class"],
                    "request_generation_id": generation["request_generation_id"], "source_type": scope["source_type"],
                    "source_share_ref": scope["source_ref"], "current_expected_folder": generation["folder"],
                    "endpoint_path": "/share/list" if scope["source_type"] == "share" else "/api/list",
                    "read_path_evidence_ref": scope["read_path_evidence_ref"]}
        if any(raw[key] != value for key, value in expected.items()):
            raise GateError("RECEIPT_SCOPE_MISMATCH")
        receipt_id = _reference(raw["receipt_id"], limit=160)
        if not re.fullmatch(re.escape(generation["request_generation_id"]) + r"-[1-9][0-9]{0,8}", receipt_id):
            raise GateError("INVALID_RECEIPT_ID")
        if not _integer(raw["http_status"]) or raw["http_status"] > 599 or (raw["errno"] is not None and (type(raw["errno"]) is not int or abs(raw["errno"]) > SAFE_INTEGER)):
            raise GateError("INVALID_RESPONSE_STATUS")
        if raw["state"] not in ("observed", "partial", "unknown", "blocked") or raw["folder_evidence"] not in ("response_folder", "list_parent_paths", "missing", "mismatch", "invalid"):
            raise GateError("INVALID_OBSERVATION_STATE")
        if any(value is not None and type(value) is not bool for value in (raw["folder_matches"], raw["owner_matches"])):
            raise GateError("INVALID_OBSERVATION_STATE")
        issues = raw["issues"]
        if type(issues) is not list or len(issues) > 64 or any(type(issue) is not str or issue not in ISSUES for issue in issues) or len(set(issues)) != len(issues):
            raise GateError("INVALID_OBSERVATION_ISSUES")
        pagination = raw["pagination"]
        if type(pagination) is not dict or set(pagination) - {"page", "page_size", "has_more", "total", "cursor", "next_cursor"}:
            raise GateError("INVALID_PAGINATION")
        for field, value in pagination.items():
            if field == "has_more":
                valid = type(value) is bool
            elif field in ("cursor", "next_cursor"):
                valid = isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{0,256}", value) is not None
            else:
                valid = _integer(value, 0 if field == "total" else 1)
            if not valid:
                raise GateError("INVALID_PAGINATION")
        rows = raw["list"]
        if type(rows) is not list or len(rows) > 5000:
            raise GateError("INVALID_OBSERVATION_LIST")
        for item in rows:
            if type(item) is not dict or set(item) != {"fs_id", "path", "server_filename", "size", "isdir"}:
                raise GateError("REDACTED_RECEIPT_SCHEMA_REQUIRED")
            if not isinstance(item["fs_id"], str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,31})", item["fs_id"]):
                raise GateError("INVALID_FILE_ID")
            path = _path(item["path"])
            if path == "/" or path != item["path"] or str(PurePosixPath(path).parent) != generation["folder"] or not item["server_filename"] or item["server_filename"] != PurePosixPath(path).name or raw["folder_matches"] is not True:
                raise GateError("RECEIPT_FOLDER_MISMATCH")
            if scope["source_type"] == "user_disk":
                self.policy.require_cloud_path(path)
            if (item["size"] is not None and not _integer(item["size"])) or type(item["isdir"]) is not int or item["isdir"] not in (0, 1):
                raise GateError("INVALID_EXACT_SIZE_OR_KIND")
        denied = raw["http_status"] in DENIALS or raw["errno"] in DENIALS or "CAPTCHA_REQUIRED" in issues
        if denied != (raw["state"] == "blocked") or (denied and rows):
            raise GateError("INVALID_DENIAL_RECEIPT")
        if raw["state"] == "observed" and (issues or raw["folder_matches"] is not True or raw["folder_evidence"] not in ("response_folder", "list_parent_paths") or (not rows and raw["folder_evidence"] != "response_folder") or raw["owner_matches"] is False or raw["errno"] != 0 or not 200 <= raw["http_status"] < 300 or any(item["size"] is None for item in rows)):
            raise GateError("UNPROVEN_OBSERVATION")
        return json.loads(_json(raw))

    def record_receipt(self, project_id, enumeration_id, receipt):
        scope = self._scope(project_id, enumeration_id)
        if type(receipt) is not dict:
            raise GateError("REDACTED_RECEIPT_SCHEMA_REQUIRED")
        generation = self._row("SELECT * FROM browser_inventory_generations WHERE request_generation_id=? AND enumeration_id=?", (receipt.get("request_generation_id"), enumeration_id))
        if not generation or generation["state"] != "active":
            raise GateError("STALE_OR_UNKNOWN_GENERATION")
        normalized = self._validate_receipt(scope, generation, receipt)
        serialized = _json(normalized); digest = sha256(serialized.encode()).hexdigest()
        with self._transaction():
            old = self._row("SELECT digest,receipt_json FROM browser_inventory_receipts WHERE request_generation_id=? AND receipt_id=?", (generation["request_generation_id"], receipt["receipt_id"]))
            if old:
                if old["digest"] != digest:
                    raise GateError("RECEIPT_ID_COLLISION")
                if old["receipt_json"] != serialized:
                    raise GateError("PERSISTED_RECEIPT_CHANGED")
                return normalized
            if scope["state"] == "blocked":
                raise GateError("CHANNEL_BLOCKED")
            self._require_grant(project_id, scope["grant_ref"], scope["source_ref"], "inventory")
            count = self.connection.execute("SELECT COUNT(*) FROM browser_inventory_receipts WHERE request_generation_id=?", (generation["request_generation_id"],)).fetchone()[0]
            if count >= 2048:
                raise GateError("RECEIPT_BUDGET_EXHAUSTED")
            self.connection.execute("INSERT INTO browser_inventory_receipts VALUES(?,?,?,?)", (generation["request_generation_id"], receipt["receipt_id"], digest, serialized))
            if normalized["state"] == "blocked":
                self.connection.execute("UPDATE browser_inventory_scopes SET state='blocked' WHERE enumeration_id=?", (enumeration_id,))
        return normalized

    def folder_inventory(self, project_id, enumeration_id, folder):
        scope = self._scope(project_id, enumeration_id); folder = _path(folder)
        generation = self._row("SELECT * FROM browser_inventory_generations WHERE enumeration_id=? AND folder=? AND state='active'", (enumeration_id, folder))
        receipts = []
        if generation:
            for stored in self.connection.execute("SELECT digest,receipt_json FROM browser_inventory_receipts WHERE request_generation_id=? ORDER BY receipt_id", (generation["request_generation_id"],)):
                try:
                    raw = json.loads(stored[1])
                    normalized = self._validate_receipt(scope, generation, raw)
                    serialized = _json(normalized)
                except (ValueError, TypeError, GateError):
                    raise GateError("PERSISTED_RECEIPT_CHANGED") from None
                if serialized != stored[1] or sha256(serialized.encode()).hexdigest() != stored[0]:
                    raise GateError("PERSISTED_RECEIPT_CHANGED")
                receipts.append(normalized)
        issues, rows, seen_ids, seen_paths, pages, cursors = set(), [], set(), set(), {}, {}
        for receipt in receipts:
            issues.update(receipt["issues"])
            for item in receipt["list"]:
                if item["fs_id"] in seen_ids:
                    issues.add("DUPLICATE_FILE_ID")
                if item["path"].casefold() in seen_paths:
                    issues.add("DUPLICATE_PATH")
                seen_ids.add(item["fs_id"]); seen_paths.add(item["path"].casefold()); rows.append(item)
            pagination = receipt["pagination"]
            if pagination.get("has_more") is False and pagination.get("next_cursor"):
                issues.add("PAGINATION_CONFLICT")
            target, key = (pages, pagination["page"]) if "page" in pagination else (cursors, pagination.get("cursor"))
            if key is not None:
                if key in target:
                    issues.add("DUPLICATE_PAGE" if target is pages else "DUPLICATE_CURSOR")
                target[key] = pagination
        complete = False
        if receipts and all(r["state"] == "observed" and r["folder_matches"] is True for r in receipts):
            if len(pages) == len(receipts):
                complete = all(number == index + 1 and page.get("has_more") is (index < len(pages) - 1) for index, (number, page) in enumerate(sorted(pages.items())))
            elif len(cursors) == len(receipts) and "" in cursors:
                current, visited = "", set()
                while current in cursors and current not in visited:
                    visited.add(current); page = cursors[current]
                    if page.get("has_more") is False:
                        complete = not page.get("next_cursor") and len(visited) == len(cursors)
                        break
                    if page.get("has_more") is not True or not page.get("next_cursor"):
                        break
                    current = page["next_cursor"]
        totals = [r["pagination"]["total"] for r in receipts if "total" in r["pagination"]]
        if len(set(totals)) > 1 or (complete and any(total != len(rows) for total in totals)):
            issues.add("TOTAL_CONFLICT")
        if not complete:
            issues.add("PAGINATION_UNKNOWN_OR_INCOMPLETE")
        invalid_size_evidence = {"INVALID_LIST", "INVALID_LIST_ITEM", "INVALID_RESPONSE", "UNREADABLE_RESPONSE", "CONFLICTING_FOLDER_EVIDENCE", "INVALID_FOLDER_EVIDENCE"}
        exact = bool(receipts) and all(r["folder_matches"] is True and not invalid_size_evidence.intersection(r["issues"]) for r in receipts) and all(_integer(item["size"]) for item in rows)
        if not exact:
            issues.add("EXACT_SIZE_UNVERIFIED")
        blocked = scope["state"] == "blocked" or any(r["state"] == "blocked" for r in receipts)
        conflict = any(issue.startswith("DUPLICATE_") or issue in ("TOTAL_CONFLICT", "PAGINATION_CONFLICT") for issue in issues)
        # Identity roles are separate; no same-name/size/share-ID equality binding.
        items = [{**item, "assetSourceId": item["fs_id"] if scope["source_type"] == "share" else None,
                  "destinationId": item["fs_id"] if scope["source_type"] == "user_disk" else None} for item in rows]
        return {"state": "blocked" if blocked else "conflict" if conflict else "complete" if complete and not issues else "partial" if rows else "unknown",
                "enumeration_complete": complete and not issues and not blocked,
                "exact_sizes_verified": exact, "source_type": scope["source_type"], "source_ref": scope["source_ref"],
                "folder": folder, "request_generation_id": None if not generation else generation["request_generation_id"],
                "evidence_class": scope["evidence_class"], "items": items, "issues": sorted(issues)}

    def transfer_manifest(self, project_id, enumeration_id):
        """Output Acquisition input only after all discovered folders are proved."""
        scope = self._scope(project_id, enumeration_id)
        if scope["state"] == "blocked":
            raise GateError("CHANNEL_BLOCKED")
        if scope["source_type"] != "share":
            raise GateError("SHARE_MANIFEST_REQUIRED")
        self._require_grant(project_id, scope["grant_ref"], scope["source_ref"], "inventory")
        pending, visited, manifest, seen_ids, seen_paths, generations = [scope["root_folder"]], set(), [], set(), set(), []
        while pending:
            folder = pending.pop()
            if folder in visited:
                raise GateError("RECURSIVE_ENUMERATION_CONFLICT")
            visited.add(folder)
            if len(visited) > 10000 or len(manifest) > 100000:
                raise GateError("MANIFEST_BUDGET_EXHAUSTED")
            snapshot = self.folder_inventory(project_id, enumeration_id, folder)
            if not snapshot["enumeration_complete"] or not snapshot["exact_sizes_verified"]:
                raise GateError("SOURCE_ENUMERATION_INCOMPLETE")
            generations.append(snapshot["request_generation_id"])
            for item in snapshot["items"]:
                if item["assetSourceId"] in seen_ids or item["path"].casefold() in seen_paths:
                    raise GateError("RECURSIVE_ID_OR_PATH_CONFLICT")
                seen_ids.add(item["assetSourceId"]); seen_paths.add(item["path"].casefold())
                relative = item["path"][len(scope["root_folder"].rstrip("/")) + 1:]
                relative = local_relative(relative)
                # local_relative has legacy permissive cases; Windows-invalid
                # segments are not a safe transfer manifest for this project.
                if any(re.search(r'[\\<>"|?*]', part) for part in relative.split("/")):
                    raise GateError("INVALID_MANIFEST_PATH")
                manifest.append(dict(source_file_id=item["assetSourceId"], relative_path=relative,
                                     size_bytes=item["size"], is_directory=bool(item["isdir"])))
                if item["isdir"]:
                    pending.append(item["path"])
        observed_folders = {r[0] for r in self.connection.execute("SELECT folder FROM browser_inventory_generations WHERE enumeration_id=? AND state='active'", (enumeration_id,))}
        if observed_folders != visited:
            raise GateError("ORPHAN_FOLDER_OBSERVATION")
        if not manifest:
            raise GateError("EMPTY_SHARE_MANIFEST")
        return {"project_id": project_id, "source_share_ref": scope["source_ref"], "manifest": sorted(manifest, key=lambda item: item["relative_path"]),
                "enumeration_complete": True, "exact_sizes_verified": True,
                "evidence_class": scope["evidence_class"], "request_generation_ids": sorted(generations)}

    def prepare_transfer(self, project_id, enumeration_id, destination):
        """Durable *queued* Acquisition record only, never real UI submission."""
        scope = self._scope(project_id, enumeration_id)
        destination = self.policy.require_cloud_path(destination)
        self._require_grant(project_id, scope["grant_ref"], scope["source_ref"], "transfer", destination=destination)
        verified = self.transfer_manifest(project_id, enumeration_id)
        # Acquisition initializes its tables with executescript/commit. Do not
        # let that implicitly commit a caller's unrelated Store transaction.
        if self.connection.in_transaction:
            raise GateError("ACTIVE_STORE_TRANSACTION")
        acquisition = Acquisition(self.connection, self.policy, grant_resolver=lambda ref: self.resolve_grant(ref, project_id=project_id))
        operation = acquisition.prepare_transfer(project_id, scope["source_ref"], destination, verified["manifest"], grant_ref=scope["grant_ref"], enumeration_complete=True)
        return {**operation, "browser_evidence_class": scope["evidence_class"], "ui_transfer_implemented": False}
