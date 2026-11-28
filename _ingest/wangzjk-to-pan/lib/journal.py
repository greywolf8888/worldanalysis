"""Isolated, at-most-once UI-transfer checkpoints; no browser/network/credentials.

Relative local paths are branch-relative. All state and JSON evidence must stay
inside this module's wangzjk-to-pan branch. Construction/status/operations/get
never create a file. Mutations use an O_EXCL lock and atomic JSON replacement.
A remaining lock or uncertain submission is NEVER expired or automatically reset.

Evidence schemas (additional non-secret metadata, e.g. synthetic:true, is allowed):
* target_confirmation: kind, path, confirmed:true
* transfer_receipt: kind, operation_id, destination, account_ref, source_ids,
  success:true/false
* root_readback: kind, operation_id, destination, account_matches:true, rows
  (each row: name, is_dir, size, target_id; optional source_id).

A saved_root_verified state proves selected ROOT metadata only, not recursion,
file readability, local downloads, transcript completeness or account ownership.
The caller must obtain real UI evidence and supply a non-secret account_ref.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit
import uuid


BRANCH_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "wangzjk-to-pan.journal.v1"
STATES = frozenset({"prepared", "submission_unknown", "submitted",
                    "needs_reconcile", "blocked", "saved_root_verified"})
MAX_JSON_BYTES = 8 * 1024 * 1024
_SECRET_KEYS = frozenset({"cookie", "cookies", "set_cookie", "authorization",
    "proxy_authorization", "password", "passwd", "pwd", "token",
    "access_token", "refresh_token", "bdstoken", "sekey", "randsk",
    "sign", "signature", "dlink", "img_url", "extraction_code",
    "extract_code", "access_code"})


class JournalError(ValueError):
    """Fail-closed input, evidence or checkpoint error. Never implies safe replay."""


class JournalLocked(JournalError):
    """Another mutation (or an unclosed crash lock) requires human reconciliation."""


class IdentityConflict(JournalError):
    """An existing account/share plan or destination name reservation conflicts."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: object, label: str, maximum: int = 256) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or value != value.strip()
            or any(unicodedata.category(c) in {"Cc", "Cf"} for c in value)):
        raise JournalError(f"Invalid {label}")
    if re.search(r"(?i)(cookie|token|password|pwd|authorization|bdstoken|sekey|dlink)\s*[:=]", value):
        raise JournalError(f"Secret-like material is forbidden in {label}")
    return value


def _component(value: object, label: str) -> str:
    value = _text(value, label)
    if value in {".", ".."} or any(c in value for c in "/\\"):
        raise JournalError(f"Unsafe {label}")
    decoded = unquote(value)
    if (any(c in decoded for c in "/\\") or decoded in {".", ".."}
            or any(unicodedata.category(c) in {"Cc", "Cf"} for c in decoded)):
        raise JournalError(f"Encoded unsafe {label}")
    return value


def root_name_collision_key(name: str) -> str:
    """Conservative collision comparison only; never a stored or readback name."""
    return unicodedata.normalize("NFKC", name).casefold()


def _cloud_path(value: object) -> str:
    value = _text(value, "cloud path", 4096)
    if (not value.startswith("/") or value.startswith("//") or "\\" in value
            or "//" in value or (len(value) > 1 and value.endswith("/"))):
        raise JournalError("Cloud path must be an absolute canonical POSIX path")
    if value != "/":
        for part in value[1:].split("/"):
            _component(part, "cloud path component")
    return value


def _identity(value: object, label: str) -> str:
    value = _text(value, label, 128)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value):
        raise JournalError(f"Invalid opaque {label}")
    return value


def canonical_share(url: str) -> str:
    """Canonicalize ONLY real pan.baidu.com share locators; discard query/hash.

    /s/1ABC and /share/init?surl=ABC produce the same HTTPS URL. Input
    credentials, ports, lookalike hosts and malformed/ambiguous IDs are rejected.
    The original URL, password query and fragment are never retained.
    """
    if not isinstance(url, str) or url != url.strip() or len(url) > 4096:
        raise JournalError("Invalid share URL")
    if any(unicodedata.category(c) in {"Cc", "Cf"} for c in url):
        raise JournalError("Invalid share URL")
    try:
        parts = urlsplit(url)
        if (parts.scheme.lower() not in {"http", "https"}
                or parts.hostname != "pan.baidu.com" or parts.username is not None
                or parts.password is not None or parts.port is not None):
            raise JournalError("Only a credential-free Baidu share URL is allowed")
        match = re.fullmatch(r"/s/1([A-Za-z0-9_-]{1,128})/?", parts.path)
        if match:
            share_id = match.group(1)
        elif parts.path == "/share/init":
            values = parse_qs(parts.query, keep_blank_values=True).get("surl", [])
            if len(values) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", values[0]):
                raise JournalError("Missing or ambiguous Baidu share ID")
            share_id = values[0]
        else:
            raise JournalError("Unsupported Baidu share locator")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, JournalError):
            raise
        raise JournalError("Invalid share URL") from None
    return "https://pan.baidu.com/s/1" + share_id


def _roots(roots: object) -> list[dict]:
    if not isinstance(roots, list) or not roots or len(roots) > 1000:
        raise JournalError("A complete nonempty root selection is required")
    result, names, ids = [], set(), set()
    for root in roots:
        if not isinstance(root, dict) or not {"source_id", "name", "is_dir", "size"} <= root.keys():
            raise JournalError("Each selected root needs source_id/name/is_dir/size")
        source_id = _identity(root["source_id"], "source ID")
        name = _component(root["name"], "root name")
        size, is_dir = root["size"], root["is_dir"]
        if type(is_dir) is not bool or type(size) is not int or size < 0:
            raise JournalError("Root type and exact nonnegative integer size are required")
        if name in names or source_id in ids:
            raise JournalError("Duplicate root name or source ID")
        names.add(name)
        ids.add(source_id)
        result.append({"source_id": source_id, "name": name, "is_dir": is_dir, "size": size})
    return sorted(result, key=lambda item: item["name"])


def _rows(rows: object, roots: list[dict]) -> list[dict]:
    if not isinstance(rows, list) or len(rows) != len(roots):
        raise JournalError("Readback must cover exactly all selected roots")
    expected = {root["name"]: root for root in roots}
    source_ids = {root["source_id"] for root in roots}
    result, names, target_ids = [], set(), set()
    for row in rows:
        if not isinstance(row, dict) or not {"name", "is_dir", "size", "target_id"} <= row.keys():
            raise JournalError("Each readback row needs name/is_dir/size/target_id")
        name = _component(row["name"], "readback name")
        if name not in expected or name in names:
            raise JournalError("Unexpected or duplicate readback root")
        root = expected[name]
        if (type(row["is_dir"]) is not bool or type(row["size"]) is not int
                or row["is_dir"] != root["is_dir"] or row["size"] != root["size"]
                or ("source_id" in row and row["source_id"] != root["source_id"])):
            raise JournalError("Readback root metadata does not exactly match source")
        target_id = _identity(row["target_id"], "target ID")
        if target_id in source_ids or target_id in target_ids:
            raise JournalError("Target IDs must be unique and independent from all source IDs")
        names.add(name)
        target_ids.add(target_id)
        result.append({**root, "target_id": target_id})
    return sorted(result, key=lambda item: item["name"])


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JournalError("Duplicate JSON key")
        result[key] = value
    return result


def _no_constant(_value):
    raise JournalError("Nonfinite JSON value")


def _no_secrets(value):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in _SECRET_KEYS or normalized.endswith("_token"):
                raise JournalError("Credential or temporary-ticket fields are forbidden")
            _no_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _no_secrets(child)
    elif isinstance(value, str):
        if re.search(r"(?i)(?:[?&](?:pwd|token|sign|sekey)=|(?:cookie|authorization|password)\s*[:=])", value):
            raise JournalError("Credential-like evidence values are forbidden")


class Journal:
    """Single-branch JSON journal. Never touches SQL or another course ledger.

    state_dir must be inside BRANCH_ROOT; relative paths are branch-relative.
    Locks persist after crashes. Manual reconciliation, not age/TTL, is needed.
    Returned objects are copies, so callers cannot mutate checkpoints in memory.
    """

    def __init__(self, state_dir: str | Path):
        self.state_dir = self._local(state_dir)
        self.path = self.state_dir / "journal.json"
        self.lock_path = self.state_dir / ".mutation.lock"

    def _local(self, value: str | Path) -> Path:
        path = Path(value)
        if ".." in path.parts:
            raise JournalError("Local traversal is forbidden")
        if not path.is_absolute():
            path = BRANCH_ROOT / path
        path = Path(os.path.abspath(path))
        try:
            relative = path.relative_to(BRANCH_ROOT)
        except ValueError:
            raise JournalError("Local path is outside the isolated branch") from None
        current = BRANCH_ROOT
        for part in relative.parts:
            current = current / part
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise JournalError("Local links/junctions are forbidden")
        try:
            path.resolve().relative_to(BRANCH_ROOT)
        except ValueError:
            raise JournalError("Resolved local path is outside the isolated branch") from None
        return path

    def _read_json(self, path: Path) -> tuple[dict, bytes]:
        path = self._local(path)
        if not path.exists() or not stat.S_ISREG(path.stat().st_mode):
            raise JournalError("An existing regular JSON file is required")
        if path.stat().st_size > MAX_JSON_BYTES:
            raise JournalError("JSON evidence exceeds the bounded size")
        raw = path.read_bytes()
        if len(raw) > MAX_JSON_BYTES:
            raise JournalError("JSON evidence exceeds the bounded size")
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constant)
        except (UnicodeError, json.JSONDecodeError):
            raise JournalError("Invalid UTF-8 JSON") from None
        if not isinstance(data, dict):
            raise JournalError("A JSON object is required")
        _no_secrets(data)
        return data, raw

    def _evidence(self, reference: str | Path) -> tuple[dict, dict]:
        path = self._local(reference)
        if (path.suffix.lower() != ".json" or path == self.path
                or re.search(r"(?i)(cookie|credential|password|token|session)", path.name)):
            raise JournalError("Only non-credential business JSON evidence is allowed")
        data, raw = self._read_json(path)
        return data, {"path": path.relative_to(BRANCH_ROOT).as_posix(),
                      "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}

    def _check_evidence(self, record: dict) -> dict:
        if (not isinstance(record, dict) or set(record) != {"path", "sha256", "bytes"}
                or not isinstance(record["path"], str)
                or not isinstance(record["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
                or type(record["bytes"]) is not int or record["bytes"] < 0):
            raise JournalError("Malformed evidence checkpoint")
        data, current = self._evidence(record["path"])
        if current != record:
            raise JournalError("Checkpoint evidence changed; reconcile without resubmission")
        return data

    @staticmethod
    def _key(share: str, account: str) -> str:
        return "op-" + hashlib.sha256((account + "\0" + share).encode("utf-8")).hexdigest()

    def _load(self) -> dict:
        self._local(self.path)
        if not self.path.exists():
            return {"schema": SCHEMA, "revision": 0, "target": None, "operations": {}}
        data, _ = self._read_json(self.path)
        if (data.get("schema") != SCHEMA or type(data.get("revision")) is not int
                or data["revision"] < 0 or not isinstance(data.get("operations"), dict)
                or "target" not in data):
            raise JournalError("Invalid journal schema")
        target = data["target"]
        if target is not None:
            if not isinstance(target, dict) or not isinstance(target.get("evidence"), dict):
                raise JournalError("Invalid target checkpoint")
            if _cloud_path(target.get("path")) == "/":
                raise JournalError("Whole-account root cannot be the business target")
        for op_id, operation in data["operations"].items():
            if (not isinstance(operation, dict) or operation.get("operation_id") != op_id
                    or operation.get("state") not in STATES
                    or type(operation.get("ever_claimed")) is not bool):
                raise JournalError("Invalid operation checkpoint")
            share = canonical_share(operation.get("share_url"))
            account = _identity(operation.get("account_ref"), "account reference")
            if share != operation["share_url"] or self._key(share, account) != op_id:
                raise JournalError("Operation identity does not match its checkpoint key")
            if target is None:
                raise JournalError("Operations require a confirmed target")
            self._within_target(operation.get("destination"), target["path"])
            _roots(operation.get("roots"))
            if operation["state"] not in {"prepared", "blocked"} and not operation["ever_claimed"]:
                raise JournalError("Unclaimed operation cannot have submission state")
            if operation["state"] == "prepared" and operation["ever_claimed"]:
                raise JournalError("Claimed operation cannot be reset to prepared")
        return data

    @contextmanager
    def _locked(self):
        self._local(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._local(self.lock_path)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise JournalLocked("Mutation lock exists; no TTL expiry or automatic retry") from None
        nonce = uuid.uuid4().hex
        raw = json.dumps({"schema": "wangzjk-to-pan.lock.v1", "owner": nonce}).encode("utf-8")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            yield
        finally:
            # Remove only our own, unchanged lock. A different lock is never cleared.
            self._local(self.lock_path)
            if not self.lock_path.exists() or self.lock_path.read_bytes() != raw:
                raise JournalLocked("Mutation lock changed; inspect durable state, do not replay")
            self.lock_path.unlink()

    def _save(self, data: dict):
        data["revision"] += 1
        data["updated_at"] = _now()
        payload = (json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        if len(payload) > MAX_JSON_BYTES:
            raise JournalError("Journal exceeds the bounded size")
        temp = self.state_dir / (".journal-" + uuid.uuid4().hex + ".tmp")
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._local(self.path)
            os.replace(temp, self.path)
            if os.name != "nt" and hasattr(os, "O_DIRECTORY"):
                directory_fd = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temp.exists():
                temp.unlink()

    @staticmethod
    def _within_target(destination: object, target: str) -> str:
        destination = _cloud_path(destination)
        if destination != target and not destination.startswith(target + "/"):
            raise JournalError("Destination is outside the confirmed business root")
        return destination

    def _target(self, data: dict) -> str:
        if data["target"] is None:
            raise JournalError("Confirm the business target with set_target first")
        target = data["target"]
        evidence = self._check_evidence(target["evidence"])
        if (evidence.get("kind") != "target_confirmation" or evidence.get("confirmed") is not True
                or evidence.get("path") != target["path"]):
            raise JournalError("Invalid target confirmation evidence")
        return target["path"]

    @staticmethod
    def _operation(data: dict, op_id: str) -> dict:
        if op_id not in data["operations"]:
            raise JournalError("Unknown operation ID")
        return data["operations"][op_id]

    @staticmethod
    def _check_name_reservation(data: dict, operation: dict):
        """Call under _locked; uncertainty or closure never releases a root name."""
        keys = [root_name_collision_key(root["name"]) for root in operation["roots"]]
        names = set(keys)
        if len(names) != len(keys):
            raise IdentityConflict("Selected root names collide after NFKC/casefold; reconcile without renaming")
        for other in data["operations"].values():
            if (other["operation_id"] != operation["operation_id"]
                    and other["account_ref"] == operation["account_ref"]
                    and other["destination"] == operation["destination"]
                    and any(root_name_collision_key(root["name"]) in names for root in other["roots"])):
                raise IdentityConflict("Another share reserves the same destination root name; reconcile without replay")

    def status(self) -> dict:
        """Read only, even for a nonexistent state directory; never acquires a lock."""
        data = self._load()
        counts = {}
        for operation in data["operations"].values():
            state = operation["state"]
            counts[state] = counts.get(state, 0) + 1
        return deepcopy({"schema": SCHEMA, "revision": data["revision"],
            "target": data["target"], "operation_count": len(data["operations"]),
            "states": counts, "verification_scope": "selected_roots_only"})

    def operations(self) -> list[dict]:
        """Return independent copies of all checkpoints without writes."""
        return deepcopy(list(self._load()["operations"].values()))

    def get(self, op_id: str) -> dict:
        """Read one checkpoint without writes."""
        return deepcopy(self._operation(self._load(), op_id))

    def set_target(self, path: str, evidence_ref: str | Path) -> dict:
        """Bind an immutable, explicitly confirmed business root, never '/'."""
        path = _cloud_path(path)
        if path == "/":
            raise JournalError("Choose a dedicated business root, not the whole account")
        with self._locked():
            data = self._load()
            evidence, record = self._evidence(evidence_ref)
            if (evidence.get("kind") != "target_confirmation"
                    or evidence.get("confirmed") is not True or evidence.get("path") != path):
                raise JournalError("Evidence must explicitly confirm this exact business root")
            if data["target"] is not None:
                if data["target"]["path"] != path:
                    raise IdentityConflict("Business root is already bound; do not redirect prior operations")
                self._target(data)
                return deepcopy(data["target"])
            data["target"] = {"path": path, "evidence": record, "confirmed_at": _now()}
            self._save(data)
            return deepcopy(data["target"])

    def prepare(self, article_id: str, share_url: str, destination: str,
                roots: list[dict], category: str, account_ref: str) -> dict:
        """Plan once per canonical share + account, globally across this journal.

        New article/category references may be added to an existing operation,
        but a changed destination or root selection is rejected, never reopened.
        Different shares cannot reserve the same destination root name/account.
        All root sizes are exact integers; directory sizes do not imply recursion.
        """
        article_id = _text(article_id, "article ID", 20)
        if not re.fullmatch(r"[1-9][0-9]*", article_id):
            raise JournalError("A numeric website article ID is required")
        share_url = canonical_share(share_url)
        account_ref = _identity(account_ref, "account reference")
        category = _component(category, "category")
        roots = _roots(roots)
        op_id = self._key(share_url, account_ref)
        with self._locked():
            data = self._load()
            destination = self._within_target(destination, self._target(data))
            if op_id in data["operations"]:
                operation = data["operations"][op_id]
                if operation["destination"] != destination or operation["roots"] != roots:
                    raise IdentityConflict("Existing account/share has a different destination or source selection")
                changed = False
                for field, value in (("article_ids", article_id), ("category_refs", category)):
                    if value not in operation[field]:
                        operation[field].append(value)
                        changed = True
                if changed:
                    self._save(data)
                return deepcopy(operation)
            operation = {"operation_id": op_id, "state": "prepared", "ever_claimed": False,
                "article_ids": [article_id], "share_url": share_url,
                "account_ref": account_ref, "destination": destination,
                "category": category, "category_refs": [category], "roots": roots,
                "created_at": _now(), "receipts": [], "receipt_success": False,
                "verification_scope": "selected_roots_only"}
            self._check_name_reservation(data, operation)
            data["operations"][op_id] = operation
            self._save(data)
            return deepcopy(operation)

    def claim(self, op_id: str) -> dict:
        """Persist submission_unknown BEFORE any UI save click; at most once."""
        with self._locked():
            data = self._load()
            self._target(data)
            operation = self._operation(data, op_id)
            if operation["state"] != "prepared" or operation["ever_claimed"]:
                raise JournalError("Operation was already claimed or closed; never resubmit")
            self._check_name_reservation(data, operation)
            operation.update(state="submission_unknown", ever_claimed=True, claimed_at=_now())
            self._save(data)
            return deepcopy(operation)

    def receipt(self, op_id: str, evidence_ref: str | Path) -> dict:
        """Bind an existing, exact-operation success/failure JSON UI receipt.

        Late evidence may reconcile uncertainty; no method authorizes another
        save click. A failed receipt stays needs_reconcile, never prepared.
        """
        with self._locked():
            data = self._load()
            operation = self._operation(data, op_id)
            if not operation["ever_claimed"] or operation["state"] == "blocked":
                raise JournalError("Receipt requires an unblocked, previously claimed operation")
            evidence, record = self._evidence(evidence_ref)
            if (evidence.get("kind") != "transfer_receipt"
                    or evidence.get("operation_id") != op_id
                    or evidence.get("destination") != operation["destination"]
                    or evidence.get("account_ref") != operation["account_ref"]
                    or type(evidence.get("success")) is not bool
                    or not isinstance(evidence.get("source_ids"), list)
                    or any(not isinstance(value, str) for value in evidence["source_ids"])
                    or sorted(evidence["source_ids"]) != sorted(root["source_id"] for root in operation["roots"])
                    or (evidence.get("success") is True and "errno" in evidence
                        and (type(evidence["errno"]) is not int or evidence["errno"] != 0))
                    or (evidence.get("success") is True and "http_status" in evidence
                        and (type(evidence["http_status"]) is not int or not 200 <= evidence["http_status"] < 300))):
                raise JournalError("Receipt is not explicitly bound to this selection/account/destination")
            for old in operation["receipts"]:
                self._check_evidence(old["evidence"])
                if old["evidence"] == record:
                    return deepcopy(operation)
            if operation["state"] in {"submitted", "saved_root_verified"}:
                raise JournalError("A successful receipt is already bound; do not replace its outcome")
            operation["receipts"].append({"evidence": record, "success": evidence["success"], "recorded_at": _now()})
            operation["receipt_success"] = evidence["success"]
            operation["state"] = "submitted" if evidence["success"] else "needs_reconcile"
            self._save(data)
            return deepcopy(operation)

    def verify(self, op_id: str, readback_ref: str | Path, rows: list[dict],
               observed_destination: str, account_matches: bool) -> dict:
        """Verify exact selected-root metadata against independent target rows.

        Requires an earlier claim AND a hash-unchanged successful receipt, true
        account comparison and exact destination. Evidence rows must agree with
        arguments. Target IDs must be unique and disjoint from all source IDs.
        This does NOT certify descendant counts/bytes, readability or local work.
        """
        with self._locked():
            data = self._load()
            self._target(data)
            operation = self._operation(data, op_id)
            if (not operation["ever_claimed"] or not operation["receipt_success"]
                    or operation["state"] not in {"submitted", "needs_reconcile", "saved_root_verified"}):
                raise JournalError("Verification requires a claimed operation with a successful receipt")
            successful = [item for item in operation["receipts"] if item["success"]]
            if not successful or any(self._check_evidence(item["evidence"]).get("success") is not True for item in successful):
                raise JournalError("Successful receipt evidence is missing or changed")
            if account_matches is not True or _cloud_path(observed_destination) != operation["destination"]:
                raise JournalError("Readback account/destination does not exactly match the plan")
            verified_rows = _rows(rows, operation["roots"])
            evidence, record = self._evidence(readback_ref)
            if (evidence.get("kind") != "root_readback" or evidence.get("operation_id") != op_id
                    or evidence.get("destination") != observed_destination
                    or evidence.get("account_matches") is not True
                    or _rows(evidence.get("rows"), operation["roots"]) != verified_rows):
                raise JournalError("Readback evidence is not bound to these exact verification inputs")
            if operation["state"] == "saved_root_verified":
                if operation["readback"] != record or operation["verified_rows"] != verified_rows:
                    raise IdentityConflict("Completed verification is immutable")
                return deepcopy(operation)
            operation.update(state="saved_root_verified", readback=record,
                verified_rows=verified_rows, verified_at=_now(), verification_scope="selected_roots_only",
                recursive_verified=False)
            self._save(data)
            return deepcopy(operation)

    def _close(self, op_id: str, reason: str, state: str) -> dict:
        reason = _text(reason, "closure reason", 1000)
        with self._locked():
            data = self._load()
            operation = self._operation(data, op_id)
            if operation["state"] == "saved_root_verified":
                raise JournalError("Verified root checkpoint cannot be downgraded")
            if state == "needs_reconcile" and not operation["ever_claimed"]:
                raise JournalError("Only a previously claimed operation can be ambiguous")
            if operation["state"] == "blocked":
                if state == "blocked" and operation.get("closure_reason") == reason:
                    return deepcopy(operation)
                raise JournalError("Blocked operation cannot be reopened")
            if operation["state"] == state and operation.get("closure_reason") == reason:
                return deepcopy(operation)
            operation.update(state=state, closure_reason=reason, closed_at=_now())
            self._save(data)
            return deepcopy(operation)

    def ambiguous(self, op_id: str, reason: str) -> dict:
        """Close resubmission after uncertainty; later evidence-only reconciliation is allowed."""
        return self._close(op_id, reason, "needs_reconcile")

    def block(self, op_id: str, reason: str) -> dict:
        """Terminally block the operation; no claim, receipt or automatic reopening."""
        return self._close(op_id, reason, "blocked")

    blocked = block
