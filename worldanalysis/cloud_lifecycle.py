"""Cloud project lifecycle on the existing Store; no browser or network code.

Migration is explicit: back up the real DB, then call ensure_schema(store).
Receipts are supplied by the authorized platform adapter/operator, just like
PlatformFirst inventories. This module validates their scope, identity, class
and chronology; it cannot authenticate a caller's claim to have used a browser.
Never label model output or a success toast as a DirectoryReadback.

Typical use::

    CloudLifecycle.ensure_schema(store)  # only after the operator's DB backup
    flow = CloudLifecycle(store, config)
    with flow.work_lock(folder_id) as lease:
        flow.register_project(project_id, folder_id, source_listing, lock=lease,
                              owner_session=config.session_id,
                              ownership_ref="authorized-ownership-receipt")
        move = flow.prepare_move(folder_id, to_state="extracting",
                                 expected_state="pending", source=source_listing,
                                 destination=destination_listing, lock=lease)
        send = flow.begin_move(move["job_id"], source=source_listing,
                               destination=destination_listing, lock=lease)
        # Only if send["may_send"]: use the ordinary authorized platform UI.
        # Then obtain NEW source + destination listings and reconcile_move().

Store.submission_authorizer remains mandatory; no permissive grant is installed.
Supply protected owner IDs in LifecycleConfig.protected_sessions or the local
WORLDANALYSIS_PROTECTED_SESSION environment variable. Both sources are honored.
Without a protected owner policy, lifecycle operations cannot pass admission.
Locks protect cooperating processes on the same local filesystem, not unrelated
sessions using another DB/machine or remote actors. Do not delete a lock file.
Synthetic receipts are opt-in for offline tests and NEVER remote_confirmed.
Project archive admission additionally requires record_project_scope() recursive
root/child listings and record_scope_material() evidence for every nonvideo file,
on the original Store.events. Scope TTL is enforced only on archive send, not
on read-only queue content verification or local repair.
Project state is the observed remote LOCATION, not content acceptance. A project
in the complete container with unverified/broken content remains repair_required
and visible in the work queue; authorized repair moves use the same move protocol.
"""
from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
from types import SimpleNamespace
from typing import Mapping

from .contracts import ContractError, canonical, check_id, digest, now, reject_links, safe_path, scan_secrets
from .platform_first import PlatformFirst
from .policy import GateError, Policy, canonical_cloud_path

PROTECTED_SESSION_ENV = "WORLDANALYSIS_PROTECTED_SESSION"
STATES = ("pending", "extracting", "complete")
TRANSITIONS = {("pending", "extracting"), ("extracting", "complete"), ("complete", "extracting")}
_TABLE = "cloud_lifecycle_projects"
_MOVE_KIND = "cloud_move_intent"
_OPERATION = "cloud-project-move"
_DATE = re.compile(r"(?<![0-9])((?:19|20)[0-9]{2})(?:[-_.年]?)([0-9]{2})(?:[-_.月]?)([0-9]{2})(?:日)?(?![0-9])")


def _text(value, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ContractError("invalid lifecycle text")
    scan_secrets(value)
    return value


def _instant(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone required")
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ContractError("readback timestamp requires an explicit timezone") from exc


def _inside(path, root):
    return path == root or path.startswith(root + "/")


def _calendar(value):
    try:
        if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return date.fromisoformat(value).isoformat()
        _instant(value)  # Validate timezone without changing the source calendar day.
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except (TypeError, ValueError, ContractError):
        raise ContractError("invalid full date or timezone-qualified timestamp") from None


def choose_project_date(project_name, *, project_evidence_ref, course_root_name=None,
                        course_root_evidence_ref=None, published_at=None,
                        published_evidence_ref=None, original_created_at=None,
                        original_creation_evidence_ref=None):
    """Choose once, recording conflicts; never accept mtime/scanning time.

    Creation evidence must attest the original, pre-move creation time, not a
    local ctime or the creation time of a copy. Conflicting dates at the highest
    available priority stay unknown rather than picking the largest number.
    """
    _text(project_name); check_id(project_evidence_ref)
    candidates = []
    for source, name, ref in (("project_name", project_name, project_evidence_ref),
                              ("course_root_name", course_root_name, course_root_evidence_ref)):
        if name is None:
            if ref is not None:
                raise ContractError("date evidence reference without a name")
            continue
        _text(name); check_id(ref)
        for match in _DATE.finditer(name):
            try:
                value = date(*map(int, match.groups())).isoformat()
            except ValueError:
                value = None
            candidates.append({"source": source, "date": value, "raw": match.group(0),
                               "name": name, "evidence_ref": ref, "valid": value is not None})
    for source, value, ref in (("trusted_publication", published_at, published_evidence_ref),
                               ("original_creation", original_created_at, original_creation_evidence_ref)):
        if value is not None:
            check_id(ref)
            candidates.append({"source": source, "date": _calendar(value), "raw": value,
                               "evidence_ref": ref, "valid": True})
        elif ref is not None:
            raise ContractError("date evidence reference without a date")
    selected, source, reason = None, "unknown", "no reliable full date"
    for source in ("project_name", "course_root_name", "trusted_publication", "original_creation"):
        dates = {c["date"] for c in candidates if c["source"] == source and c["valid"]}
        if dates:
            if len(dates) == 1:
                selected = dates.pop()
                reason = "first unambiguous source in fixed priority order"
            else:
                reason = "conflicting full dates in highest-priority source; explicit review required"
                source = "unknown"
            break
    if selected is None:
        source = "unknown"
    return {"project_date": selected, "date_source": source,
            "confidence": "high" if source in {"project_name", "course_root_name"} else
                          "medium" if source == "trusted_publication" else
                          "low" if source == "original_creation" else "unknown",
            "candidates": candidates, "selection_reason": reason,
            "has_conflict": len({c["date"] for c in candidates if c["valid"]}) > 1}


@dataclass(frozen=True)
class RemoteContainer:
    folder_id: str
    path: str


@dataclass(frozen=True)
class RemoteEntry:
    folder_id: str
    path: str
    kind: str = "folder"


@dataclass(frozen=True)
class DirectoryReadback:
    account_ref: str
    folder_id: str
    path: str
    entries: tuple[RemoteEntry, ...]
    evidence_ref: str
    observed_at: str
    enumeration_complete: bool
    evidence_class: str = "real_authorized_browser"

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ContractError("readback must be an object")
        try:
            return cls(**{**value, "entries": tuple(RemoteEntry(**entry) for entry in value["entries"])})
        except (KeyError, TypeError) as exc:
            raise ContractError("missing/unknown readback fields") from exc


@dataclass(frozen=True)
class OperationReadback:
    """A platform's explicit non-execution result, not absence from a listing."""
    account_ref: str
    request_id: str
    state: str
    evidence_ref: str
    observed_at: str
    evidence_class: str = "real_authorized_browser"


@dataclass(frozen=True)
class LifecycleConfig:
    account_ref: str
    session_id: str
    containers: Mapping[str, RemoteContainer]
    protected_remote_ids: frozenset[str] = field(default_factory=frozenset)
    protected_paths: tuple[str, ...] = ()
    protected_sessions: frozenset[str] = field(default_factory=frozenset)
    readback_max_age_seconds: int = 300
    search_roots: tuple[str, ...] = ()
    max_search_rounds: int = 3
    max_search_directories: int = 32
    max_search_entries: int = 10000


class ProjectWorkLock:
    """Nonblocking kernel lock. No timeout/PID-based stealing; process exit releases it."""
    def __init__(self, path, identity, owner):
        self.path, self.identity, self.owner = Path(path), identity, owner
        self._stream = None
        self._pid = None

    def __enter__(self):
        if self._stream is not None:
            raise GateError("PROJECT_LOCK_ALREADY_HELD")
        reject_links(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        reject_links(self.path.parent)
        stream = self.path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0"); stream.flush(); os.fsync(stream.fileno())
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise GateError("PROJECT_WORK_LOCKED", "kernel lock held; do not delete or steal the lock file") from exc
        self._stream, self._pid = stream, os.getpid()
        try:
            stream.seek(1); stream.truncate()
            stream.write(canonical({"identity": self.identity, "owner": self.owner,
                                    "pid": self._pid, "acquired_at": now()}))
            stream.flush(); os.fsync(stream.fileno())
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
        self._pid = None


def _load_record(connection, account_ref, folder_id):
    row = connection.execute(f"SELECT * FROM {_TABLE} WHERE account_ref=? AND folder_id=?", (account_ref, folder_id)).fetchone()
    if row is None:
        raise ContractError("unknown stable cloud project identity")
    record = {**json.loads(row["record_json"]), "account_ref": row["account_ref"],
              "folder_id": row["folder_id"], "project_id": row["project_id"],
              "location_version": row["location_version"], "move_job_id": row["move_job_id"]}
    # Derived on every read, including old snapshots without this JSON field.
    record["repair_required"] = record["state"] == "complete" and record["content_state"] != "complete"
    return record


def _load_move(connection, job_id):
    check_id(job_id)
    job = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    rows = connection.execute("SELECT payload_json FROM events WHERE kind=? AND record_id=?", (_MOVE_KIND, job_id)).fetchall()
    if job is None or job["operation"] != _OPERATION or len(rows) != 1:
        raise ContractError("unknown or inconsistent lifecycle move")
    intent = json.loads(rows[0][0])
    if digest(intent) != job["input_revision"] or intent["project_id"] != job["project_id"]:
        raise ContractError("move intent identity/hash mismatch")
    return dict(job), intent


def _listing_data(listing, policy, *, account_ref, allow_synthetic=False):
    if not isinstance(listing, DirectoryReadback):
        raise ContractError("a DirectoryReadback, not a model completion flag, is required")
    if listing.account_ref != account_ref:
        raise GateError("ACCOUNT_IDENTITY_MISMATCH")
    allowed = {"real_authorized_browser", "synthetic"} if allow_synthetic else {"real_authorized_browser"}
    if listing.evidence_class not in allowed:
        raise GateError("PLATFORM_READBACK_REQUIRED")
    check_id(listing.folder_id); check_id(listing.evidence_ref)
    path = policy.require_cloud_path(listing.path)
    _instant(listing.observed_at)
    if type(listing.enumeration_complete) is not bool or not isinstance(listing.entries, (tuple, list)) or len(listing.entries) > 10000:
        raise ContractError("invalid readback enumeration")
    seen, paths, entries = set(), set(), []
    for item in listing.entries:
        if not isinstance(item, RemoteEntry):
            raise ContractError("invalid remote directory entry")
        check_id(item.folder_id)
        item_path = policy.require_cloud_path(item.path)
        if item.kind not in {"folder", "file"} or str(PurePosixPath(item_path).parent) != path:
            raise ContractError("listing entries must be direct children of the observed container")
        if item.folder_id in seen or item_path.casefold() in paths:
            raise ContractError("duplicate listing ID/path")
        seen.add(item.folder_id); paths.add(item_path.casefold())
        entries.append({"folder_id": item.folder_id, "path": item_path, "kind": item.kind})
    result = {"account_ref": account_ref, "folder_id": listing.folder_id, "path": path,
              "entries": entries, "evidence_ref": listing.evidence_ref,
              "observed_at": listing.observed_at, "enumeration_complete": listing.enumeration_complete,
              "evidence_class": listing.evidence_class}
    scan_secrets(result)
    if len(canonical(result)) > 2 * 1024**2:
        raise ContractError("readback exceeds 2 MiB")
    return result


def _reconciliation(intent, source, destination):
    fid = intent["folder_id"]
    in_source = [e for e in source["entries"] if e["folder_id"] == fid]
    in_dest = [e for e in destination["entries"] if e["folder_id"] == fid]
    collisions = [e for listing, expected in ((source, intent["source"]["path"]), (destination, intent["destination"]["path"]))
                  for e in listing["entries"] if e["path"].casefold() == expected.casefold() and e["folder_id"] != fid]
    if collisions:
        return "name_identity_conflict", collisions
    if in_source and in_dest:
        return "identity_at_both_sides", in_source + in_dest
    if in_dest:
        if in_dest[0]["kind"] != "folder" or in_dest[0]["path"] != intent["destination"]["path"]:
            return "identity_location_mismatch", in_dest
        return ("destination_only" if source["enumeration_complete"] and destination["enumeration_complete"] else "enumeration_incomplete"), []
    if in_source:
        if in_source[0]["kind"] != "folder" or in_source[0]["path"] != intent["source"]["path"]:
            return "identity_location_mismatch", in_source
        return "still_at_source_not_proof_of_non_execution", []
    if not source["enumeration_complete"] or not destination["enumeration_complete"]:
        return "enumeration_incomplete", []
    return "identity_missing_both_sides", []


class _PlatformReader(PlatformFirst):
    """Reuse the existing verifier without its schema-writing constructor."""
    def __init__(self, store, policy, allow_synthetic):
        self.store, self.policy, self.allow_synthetic = store, policy, allow_synthetic


class CloudLifecycle:
    @staticmethod
    def ensure_schema(store):
        """Explicit same-DB migration; the caller owns backup/real-DB approval."""
        with store.transaction():
            store.connection.execute(f"""CREATE TABLE IF NOT EXISTS {_TABLE}(
                account_ref TEXT NOT NULL, folder_id TEXT NOT NULL,
                project_id TEXT NOT NULL UNIQUE REFERENCES projects,
                record_json TEXT NOT NULL, location_version INTEGER NOT NULL DEFAULT 0,
                move_job_id TEXT REFERENCES jobs, PRIMARY KEY(account_ref,folder_id))""")

    def __init__(self, store, config: LifecycleConfig, policy=None, *, allow_synthetic=False):
        self.store, self.config = store, config
        self.policy = policy or Policy()
        self.allow_synthetic = allow_synthetic is True
        check_id(config.account_ref); check_id(config.session_id)
        if set(config.containers) != set(STATES):
            raise ContractError("configure exactly pending/extracting/complete real containers")
        self.containers = {}
        for state, container in config.containers.items():
            if not isinstance(container, RemoteContainer):
                raise ContractError("real remote container ID and path required")
            check_id(container.folder_id)
            self.containers[state] = {"folder_id": container.folder_id, "path": self.policy.require_cloud_path(container.path)}
        values = list(self.containers.values())
        if len({c["folder_id"] for c in values}) != 3 or len({str(PurePosixPath(c["path"]).parent) for c in values}) != 1:
            raise ContractError("state containers must be distinct siblings, not nested project directories")
        if len({c["path"].casefold() for c in values}) != 3:
            raise ContractError("state container paths collide")
        self.protected_paths = tuple(canonical_cloud_path(p) for p in config.protected_paths)
        if not isinstance(config.protected_sessions, (frozenset, set, tuple, list)):
            raise ContractError("protected_sessions must be a collection of opaque IDs")
        protected_sessions = {check_id(ident) for ident in config.protected_sessions}
        local_protected_session = os.environ.get(PROTECTED_SESSION_ENV)
        if local_protected_session is not None:
            protected_sessions.add(check_id(local_protected_session))
        self.protected_sessions = frozenset(protected_sessions)
        for ident in config.protected_remote_ids:
            check_id(ident)
        for bound, maximum in ((config.readback_max_age_seconds, 86400), (config.max_search_rounds, 10),
                               (config.max_search_directories, 100), (config.max_search_entries, 10000)):
            if type(bound) is not int or not 1 <= bound <= maximum:
                raise ContractError("invalid lifecycle freshness/search bound")
        self.search_roots = tuple(self.policy.require_cloud_path(p) for p in config.search_roots) or tuple(c["path"] for c in values)
        if len(self.search_roots) > 8:
            raise ContractError("identity search must stay narrowly bounded")
        if not store.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)).fetchone():
            raise ContractError("lifecycle schema absent: back up DB, then call CloudLifecycle.ensure_schema(store)")

    def work_lock(self, folder_id):
        check_id(folder_id)
        identity = digest([self.config.account_ref, folder_id])
        return ProjectWorkLock(Path(self.store.db_path).absolute().parent / "cloud-locks" / (identity + ".lock"), identity, self.config.session_id)

    def _lock(self, folder_id, lock):
        expected = self.work_lock(folder_id)
        if (not isinstance(lock, ProjectWorkLock) or lock._stream is None or lock._stream.closed
                or lock._pid != os.getpid() or lock.identity != expected.identity
                or lock.owner != self.config.session_id or lock.path.absolute() != expected.path.absolute()):
            raise GateError("PROJECT_WORK_LOCK_REQUIRED")

    def _permission(self, record, extra_paths=(), extra_ids=()):
        owner = record.get("owner_session")
        if not self.protected_sessions:
            raise GateError("PROTECTED_SESSIONS_NOT_CONFIGURED")
        if owner in self.protected_sessions:
            raise GateError("PROTECTED_SESSION_OWNERSHIP")
        if owner != self.config.session_id or not record.get("ownership_ref"):
            raise GateError("PROJECT_OWNERSHIP_UNKNOWN" if owner is None else "PROJECT_OWNED_BY_ANOTHER_SESSION")
        if record["folder_id"] in self.config.protected_remote_ids or any(i in self.config.protected_remote_ids for i in extra_ids):
            raise GateError("PROTECTED_REMOTE_ID")
        for path in (record["path"], *record.get("known_paths", []), *extra_paths):
            if any(_inside(path.casefold(), p.casefold()) or _inside(p.casefold(), path.casefold()) for p in self.protected_paths):
                raise GateError("PROTECTED_REMOTE_PATH")
        if record["containers"] != self.containers:
            raise GateError("CONTAINER_CONFIGURATION_CHANGED")

    def _listing(self, listing, container=None, *, fresh=False, not_before=None):
        value = _listing_data(listing, self.policy, account_ref=self.config.account_ref, allow_synthetic=self.allow_synthetic)
        if container and (value["folder_id"] != container["folder_id"] or value["path"] != container["path"]):
            raise GateError("CONTAINER_IDENTITY_MISMATCH")
        instant = _instant(value["observed_at"])
        age = (datetime.now(timezone.utc) - instant).total_seconds()
        if age < -30 or fresh and age > self.config.readback_max_age_seconds or not_before and instant < _instant(not_before):
            raise GateError("STALE_PLATFORM_READBACK")
        return value

    def get_project(self, folder_id):
        check_id(folder_id)
        return _load_record(self.store.connection, self.config.account_ref, folder_id)

    def _save(self, record, *, location_changed=False):
        values = {k: v for k, v in record.items() if k not in {"account_ref", "folder_id", "project_id", "location_version", "move_job_id"}}
        values["updated_at"] = now()
        values["repair_required"] = values["state"] == "complete" and values["content_state"] != "complete"
        self.store.connection.execute(f"UPDATE {_TABLE} SET record_json=?,location_version=location_version+?,move_job_id=? WHERE account_ref=? AND folder_id=?",
            (canonical(values).decode(), int(location_changed), record.get("move_job_id"), record["account_ref"], record["folder_id"]))

    def register_project(self, project_id, folder_id, listing, *, lock, owner_session=None, ownership_ref=None,
                         course_root_path=None, course_root_id=None, **date_inputs):
        """Bind an existing local project to an observed ID, never just its name.

        Unknown ownership is observable but never movable; ownership cannot be
        silently changed by a later scan. Date inputs are used only on first bind.
        """
        check_id(project_id); check_id(folder_id); self._lock(folder_id, lock)
        if not isinstance(listing, DirectoryReadback):
            raise ContractError("DirectoryReadback required")
        state = next((s for s, c in self.containers.items() if c["folder_id"] == listing.folder_id), None)
        if state is None:
            raise GateError("NOT_A_CONFIGURED_STATE_CONTAINER")
        value = self._listing(listing, self.containers[state], fresh=True)
        matches = [e for e in value["entries"] if e["folder_id"] == folder_id and e["kind"] == "folder"]
        if len(matches) != 1 or folder_id in {c["folder_id"] for c in self.containers.values()}:
            raise GateError("PROJECT_FOLDER_ID_NOT_READ_BACK")
        entry = matches[0]
        if owner_session is not None:
            check_id(owner_session); check_id(ownership_ref)
        elif ownership_ref is not None:
            raise ContractError("ownership evidence without an owner")
        with self.store.transaction():
            if not self.store.connection.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
                raise ContractError("register an existing Store project; no shadow project catalog")
            old = self.store.connection.execute(f"SELECT 1 FROM {_TABLE} WHERE account_ref=? AND folder_id=?", (self.config.account_ref, folder_id)).fetchone()
            if old:
                record = self.get_project(folder_id)
                if record["project_id"] != project_id or owner_session is not None and (record["owner_session"], record["ownership_ref"]) != (owner_session, ownership_ref):
                    raise GateError("STABLE_IDENTITY_OR_OWNERSHIP_CONFLICT")
                if record["path"] != entry["path"] or record["state"] != state:
                    raise GateError("LOCATION_CHANGE_REQUIRES_MOVE_RECONCILIATION")
                return record  # Freeze the date even when newer scan/mtime input exists.
            fixed_date = choose_project_date(PurePosixPath(entry["path"]).name, project_evidence_ref=value["evidence_ref"], **date_inputs)
            course_relative = date_inputs.get("course_root_name")
            if course_relative is not None and ("/" in course_relative or "\\" in course_relative or course_relative in {".", ".."}):
                raise ContractError("course root name must be one observed directory component")
            if course_root_path is not None:
                course_root_path = self.policy.require_cloud_path(course_root_path)
                if not course_relative or not course_root_path.startswith(entry["path"] + "/") or PurePosixPath(course_root_path).name != course_relative:
                    raise GateError("COURSE_ROOT_PROJECT_IDENTITY_MISMATCH")
                course_relative = course_root_path[len(entry["path"]) + 1:]
            if course_root_id is not None:
                check_id(course_root_id)
                if course_relative is None:
                    raise ContractError("course root ID without observed course root")
            record = {"state": state, "path": entry["path"], "parent_id": value["folder_id"],
                      "known_paths": [entry["path"]], "containers": self.containers,
                      "course_root_relative_path": course_relative, "course_root_id": course_root_id,
                      "owner_session": owner_session, "ownership_ref": ownership_ref,
                      "date": fixed_date, "initial_readback": {k: v for k, v in value.items() if k != "entries"},
                      "content_state": "unknown", "archive_pending": False, "platform_run_id": None,
                      "content_evidence": None, "created_at": now(), "updated_at": now()}
            self.store.connection.execute(f"INSERT INTO {_TABLE}(account_ref,folder_id,project_id,record_json) VALUES(?,?,?,?)",
                (self.config.account_ref, folder_id, project_id, canonical(record).decode()))
            self.store.event("cloud_project_registered", folder_id, project_id,
                {"account_ref": self.config.account_ref, "folder_id": folder_id, "path": entry["path"], "date": fixed_date, "ownership_ref": ownership_ref})
        return self.get_project(folder_id)

    @staticmethod
    def _keys(value, required):
        if type(value) is not dict or set(value) != set(required):
            raise ContractError("missing/unknown project-scope evidence fields")

    def _normalize_scope(self, record, value):
        self._keys(value, {"schema_version", "scope_id", "account_ref", "project_folder_id", "root_path", "evidence_class", "evidence_ref", "observed_at", "directories"})
        if value["schema_version"] != "1.0" or value["evidence_class"] != "real_authorized_browser":
            raise GateError("REAL_PROJECT_SCOPE_REQUIRED")
        check_id(value["scope_id"]); check_id(value["evidence_ref"])
        if value["account_ref"] != record["account_ref"] or value["project_folder_id"] != record["folder_id"] or value["root_path"] not in record["known_paths"]:
            raise GateError("PROJECT_SCOPE_IDENTITY_MISMATCH")
        observed = _instant(value["observed_at"])
        if (datetime.now(timezone.utc) - observed).total_seconds() < -30:
            raise GateError("STALE_PLATFORM_READBACK")
        directories = value["directories"]
        if type(directories) is not list or not 1 <= len(directories) <= 256 or len(canonical(value)) > 2 * 1024**2:
            raise ContractError("project-scope directory/byte bound exceeded")
        by_id, children, files, paths, gaps = {}, {}, {}, set(), []
        for directory in directories:
            self._keys(directory, {"folder_id", "path", "evidence_ref", "observed_at", "enumeration_complete", "entries"})
            fid = check_id(directory["folder_id"]); check_id(directory["evidence_ref"])
            path = self.policy.require_cloud_path(directory["path"])
            if not _inside(path, value["root_path"]) or fid in by_id or type(directory["enumeration_complete"]) is not bool:
                raise ContractError("invalid project directory identity/completeness")
            instant = _instant(directory["observed_at"])
            if instant > observed or (datetime.now(timezone.utc) - instant).total_seconds() < -30:
                raise ContractError("directory observation exceeds scope observation time")
            by_id[fid] = directory
            if not directory["enumeration_complete"]:
                gaps.append("directory_enumeration_incomplete:" + fid)
            if type(directory["entries"]) is not list or len(directory["entries"]) > 10000:
                raise ContractError("invalid project directory entries")
            for entry in directory["entries"]:
                if type(entry) is not dict or entry.get("kind") not in {"folder", "file"}:
                    raise ContractError("scope entry requires file/folder kind")
                self._keys(entry, {"file_id", "path", "kind"} if entry["kind"] == "folder" else
                           {"file_id", "path", "kind", "media_kind", "source_revision", "size_bytes"})
                eid = check_id(entry["file_id"])
                ep = self.policy.require_cloud_path(entry["path"])
                if str(PurePosixPath(ep).parent) != path or ep.casefold() in paths or eid in files or eid in children:
                    raise ContractError("duplicate or out-of-directory project identity")
                paths.add(ep.casefold())
                if entry["kind"] == "folder":
                    children[eid] = ep
                else:
                    if entry["media_kind"] not in {"video", "nonvideo"} or type(entry["size_bytes"]) is not int or entry["size_bytes"] < 0:
                        raise ContractError("invalid scoped file kind/size")
                    if PurePosixPath(ep).suffix.lower() in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".wmv", ".ts"} and entry["media_kind"] != "video":
                        raise GateError("VIDEO_CANNOT_BYPASS_PLATFORM_SCOPE")
                    if entry["source_revision"] is None:
                        gaps.append("source_revision_unknown:" + eid)
                    else:
                        check_id(entry["source_revision"])
                    files[eid] = entry
        if len(files) > 10000 or set(files) & set(by_id) or record["folder_id"] in children:
            raise ContractError("project-scope identity/count conflict")
        root = by_id.get(record["folder_id"])
        if root is None or root["path"] != value["root_path"]:
            gaps.append("project_root_not_enumerated")
        for fid, path in children.items():
            if fid not in by_id or by_id[fid]["path"] != path:
                gaps.append("child_directory_not_enumerated:" + fid)
        for fid, directory in by_id.items():
            if fid != record["folder_id"] and children.get(fid) != directory["path"]:
                gaps.append("directory_not_linked_from_parent:" + fid)
        # Provider opaque revision tokens are metadata, NOT MD5/content hashes.
        identities = [{k: item[k] for k in ("file_id", "source_revision", "size_bytes", "media_kind")} for _, item in sorted(files.items())]
        return {"scope": value, "files": files, "gaps": gaps,
                "scope_identity_sha256": digest([record["account_ref"], record["folder_id"], identities])}

    def _scope_event(self, record, scope_id=None, *, fresh=False):
        query = "SELECT payload_json FROM events WHERE kind='cloud_project_scope_observed' AND project_id=?"
        args = [record["project_id"]]
        if scope_id is not None:
            query += " AND record_id=?"; args.append(scope_id)
        rows = self.store.connection.execute(query + " ORDER BY rowid DESC" + (" LIMIT 1" if scope_id is None else ""), args).fetchall()
        if not rows:
            return None
        if scope_id is not None and len(rows) != 1:
            raise ContractError("project scope event identity is not unique")
        payload = json.loads(rows[0][0])
        if digest(payload["scope"]) != payload["scope_sha256"]:
            raise ContractError("project scope event hash mismatch")
        result = self._normalize_scope(record, payload["scope"])
        if result["scope_identity_sha256"] != payload["scope_identity_sha256"]:
            raise ContractError("project scope content identity mismatch")
        if fresh:
            if result["gaps"]:
                raise GateError("PROJECT_SCOPE_INCOMPLETE")
            for directory in result["scope"]["directories"]:
                if (datetime.now(timezone.utc) - _instant(directory["observed_at"])).total_seconds() > self.config.readback_max_age_seconds:
                    raise GateError("PROJECT_SCOPE_READBACK_STALE")
        return result

    def record_project_scope(self, folder_id, scope, *, lock):
        """Append a real recursive project observation to Store.events, never a DB/workflow.

        Missing pagination/child snapshots become an authoritative blocking head.
        Revisions are provider metadata. Paths and observation time do not enter
        scope_identity_sha256; observations are immutable and retained on moves.
        """
        self._lock(folder_id, lock)
        with self.store.transaction():
            record = self.get_project(folder_id); self._permission(record)
            normalized = self._normalize_scope(record, scope)
            old = self._scope_event(record, scope["scope_id"])
            if old is not None:
                if canonical(old["scope"]) != canonical(scope):
                    raise ContractError("scope_id is immutable")
            else:
                latest = self._scope_event(record)
                if latest and _instant(scope["observed_at"]) < _instant(latest["scope"]["observed_at"]):
                    raise GateError("PROJECT_SCOPE_OBSERVATION_ROLLBACK")
                self.store.event("cloud_project_scope_observed", scope["scope_id"], record["project_id"],
                    {"scope": scope, "scope_sha256": digest(scope), "scope_identity_sha256": normalized["scope_identity_sha256"]})
        return {"scope_id": scope["scope_id"], "scope_identity_sha256": normalized["scope_identity_sha256"],
                "file_count": len(normalized["files"]), "gaps": normalized["gaps"]}

    def _material_receipt(self, record, item, receipt):
        self._keys(receipt, {"receipt_id", "state", "source_revision", "size_bytes", "evidence_class", "evidence_ref", "observed_at", "read_basis", "reason", "artifacts"})
        check_id(receipt["receipt_id"]); check_id(receipt["evidence_ref"]); _text(receipt["reason"])
        if type(receipt["size_bytes"]) is not int or receipt["evidence_class"] != "real_authorized_browser" or (receipt["source_revision"], receipt["size_bytes"]) != (item["source_revision"], item["size_bytes"]):
            raise GateError("MATERIAL_SOURCE_IDENTITY_MISMATCH")
        if (datetime.now(timezone.utc) - _instant(receipt["observed_at"])).total_seconds() < -30:
            raise GateError("STALE_PLATFORM_READBACK")
        artifacts = receipt["artifacts"]
        if type(artifacts) is not list or len(artifacts) > 4:
            raise ContractError("material artifact bound exceeded")
        if receipt["state"] == "blocked":
            if artifacts or receipt["read_basis"] != "unavailable":
                raise ContractError("blocked material must not pretend to have content evidence")
            return
        if receipt["state"] not in {"retained", "excluded_by_content"} or receipt["read_basis"] not in {"original_file", "browser_content_readback"} or not artifacts:
            raise ContractError("material closure requires actual readable content evidence")
        if receipt["read_basis"] == "original_file" and (len(artifacts) != 1 or artifacts[0].get("size_bytes") != item["size_bytes"]):
            raise ContractError("original-file evidence must retain the observed source length")
        from .platform_materials import _read, _image_readable
        from .platform_products import _text_check, inspect_product
        total = 0
        for artifact in artifacts:
            self._keys(artifact, {"relative_path", "sha256", "size_bytes"})
            size, sha = artifact["size_bytes"], artifact["sha256"]
            if type(size) is not int or not 0 < size <= self.store.max_file_bytes or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ContractError("invalid material artifact length/hash")
            total += size
            if total > 64 * 1024**2:
                raise ContractError("material evidence byte bound exceeded")
            path = safe_path(self.store.root, artifact["relative_path"])
            data = _read(path, size)
            if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
                raise GateError("MATERIAL_EVIDENCE_MISSING_OR_CHANGED")
            suffix = path.suffix.lower()
            if suffix in {".txt", ".md", ".srt", ".vtt"}:
                text, _ = _text_check(data)
                readable = bool(text.strip())
            elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp"}:
                readable = _image_readable(data, path)[0]
            elif suffix in {".pdf", ".pptx"}:
                readable = inspect_product(self.store, "courseware", artifact["relative_path"])["readable"]
            else:
                readable = False
            if not readable:
                raise GateError("MATERIAL_CONTENT_EVIDENCE_NOT_READABLE")

    def record_scope_material(self, folder_id, scope_id, file_id, receipt, *, lock):
        """Record nonvideo evidence/blocked scope facts; no model completed flag.

        Exclusions require real body/readback artifacts plus an explicit reason,
        never a filename rule. The operator attests the source/readback binding;
        local hashes/readability are independently rechecked on every admission.
        """
        self._lock(folder_id, lock); check_id(scope_id); check_id(file_id)
        with self.store.transaction():
            record = self.get_project(folder_id); self._permission(record)
            scope = self._scope_event(record, scope_id)
            item = scope["files"].get(file_id) if scope else None
            if not item or item["media_kind"] != "nonvideo":
                raise GateError("MATERIAL_NOT_IN_BOUND_PROJECT_SCOPE")
            self._material_receipt(record, item, receipt)
            payload = {"account_ref": record["account_ref"], "project_folder_id": folder_id,
                       "scope_id": scope_id, "file_id": file_id, "source_revision": item["source_revision"],
                       "size_bytes": item["size_bytes"], "receipt": receipt}
            old = self.store.connection.execute("SELECT payload_json FROM events WHERE kind='cloud_scope_material_observed' AND record_id=?", (receipt["receipt_id"],)).fetchall()
            if old and (len(old) != 1 or json.loads(old[0][0]) != payload):
                raise ContractError("material receipt_id is immutable")
            if not old:
                self.store.event("cloud_scope_material_observed", receipt["receipt_id"], record["project_id"], payload)
        return {"file_id": file_id, "scope_id": scope_id, "state": receipt["state"], "receipt_id": receipt["receipt_id"]}

    def _project_scope_gate(self, record, inventory):
        scope = self._scope_event(record)
        if scope is None:
            return {"scope_id": None, "gaps": ["project_scope_not_bound"], "materials": []}
        gaps = ["project_scope:" + gap for gap in scope["gaps"]]
        files = scope["files"]
        videos = {fid: (f["source_revision"], f["size_bytes"]) for fid, f in files.items() if f["media_kind"] == "video"}
        run_videos = {f["file_id"]: (f["source_revision"], f["size_bytes"]) for f in inventory["files"]}
        if videos != run_videos:
            gaps.append("project_scope_video_inventory_mismatch")
        rows = self.store.connection.execute("SELECT payload_json FROM events WHERE kind='cloud_scope_material_observed' AND project_id=? ORDER BY rowid DESC LIMIT 10001", (record["project_id"],)).fetchall()
        if len(rows) > 10000:
            raise ContractError("material evidence event bound exceeded")
        latest, historical, materials = {}, {}, []
        for row in rows:
            payload = json.loads(row[0]); fid = payload["file_id"]
            item = files.get(fid)
            if item and fid not in latest and payload["account_ref"] == record["account_ref"] and payload["project_folder_id"] == record["folder_id"] and (payload["source_revision"], payload["size_bytes"]) == (item["source_revision"], item["size_bytes"]):
                latest[fid] = payload
        for fid, item in sorted(files.items()):
            if item["media_kind"] != "nonvideo":
                continue
            state, reason = "unreviewed", "no actual content receipt"
            payload = latest.get(fid)
            if payload:
                try:
                    if payload["scope_id"] not in historical:
                        historical[payload["scope_id"]] = self._scope_event(record, payload["scope_id"])
                    original = historical[payload["scope_id"]]
                    old = original["files"].get(fid) if original else None
                    if not old or (old["source_revision"], old["size_bytes"]) != (item["source_revision"], item["size_bytes"]):
                        raise GateError("MATERIAL_SCOPE_SOURCE_MISMATCH")
                    self._material_receipt(record, item, payload["receipt"])
                    state, reason = payload["receipt"]["state"], payload["receipt"]["reason"]
                except Exception as exc:
                    state, reason = "invalid", getattr(exc, "code", type(exc).__name__)
            if state not in {"retained", "excluded_by_content"}:
                gaps.append("project_material_" + state + ":" + fid)
            materials.append({"file_id": fid, "state": state, "reason": reason,
                              "receipt_id": payload["receipt"]["receipt_id"] if payload else None})
        return {"scope_id": scope["scope"]["scope_id"], "scope_identity_sha256": scope["scope_identity_sha256"],
                "file_count": len(files), "gaps": gaps, "materials": materials}

    def _content(self, record, run_id):
        """Reuse actual frozen run + product/hash checks; no caller completed flag."""
        platform = _PlatformReader(self.store, self.policy, self.allow_synthetic)
        status = platform.status(run_id, verify_artifacts=True)
        inventory = status["inventory"]
        content_roots = set(record["known_paths"])
        if record.get("course_root_relative_path"):
            content_roots.update(p + "/" + record["course_root_relative_path"] for p in record["known_paths"])
        if status["project_id"] != record["project_id"] or inventory["account_ref"] != record["account_ref"] or inventory["root_path"] not in content_roots:
            raise GateError("PLATFORM_RUN_PROJECT_IDENTITY_MISMATCH")
        gaps = []
        if status["state"] == "blocked": gaps.append("platform_run_blocked")
        if not inventory["enumeration_complete"]: gaps.append("inventory_not_complete")
        if not inventory["files"]: gaps.append("empty_inventory_requires_review")
        if not status["first_pass_sealed"]: gaps.append("first_pass_not_sealed")
        if inventory["evidence_class"] != "real_authorized_browser": gaps.append("non_real_inventory")
        if not status["artifact_integrity_checked"] or status["integrity_issues"]: gaps.append("artifact_integrity_failed")
        for row in status["products"]:
            obs, artifact, product = row["observation"], row["artifact"], row["product"]
            coverage = obs.get("coverage", {})
            basis = "full_transcript" if product == "manuscript" else "source_frames"
            if (obs.get("state") != "present" or obs.get("content_kind") == "summary"
                    or coverage.get("state") != "sufficient" or coverage.get("basis") != basis
                    or not coverage.get("evidence_ref") or coverage.get("missing_intervals")
                    or not artifact or artifact.get("readable") is not True):
                gaps.append(row["file_id"] + ":" + product + ":required_material_or_quality_gap")
            if row["generation_job_id"]:
                job = self.store.connection.execute("SELECT state FROM jobs WHERE job_id=?", (row["generation_job_id"],)).fetchone()
                if not job or job["state"] != "verified_complete":
                    gaps.append(row["file_id"] + ":" + product + ":unreconciled_generation")
        project_scope = self._project_scope_gate(record, inventory)
        gaps.extend(project_scope["gaps"])
        requests = self.store.connection.execute("SELECT request_id FROM supplement_requests WHERE project_id=? AND state NOT IN ('resolved','verified_complete')", (record["project_id"],)).fetchall()
        gaps.extend("open_supplement_request:" + r[0] for r in requests)
        return {"content_state": "complete" if not gaps else "incomplete", "gaps": gaps,
                "platform_run_id": run_id, "checked_at": now(), "platform_status_sha256": digest(status),
                "inventory_ref": inventory["evidence_ref"], "product_count": status["total_products"],
                "project_scope": project_scope}

    def refresh_content(self, folder_id, run_id, *, lock):
        self._lock(folder_id, lock)
        with self.store.transaction():
            record = self.get_project(folder_id); self._permission(record)
            evidence = self._content(record, run_id)
            record.update(content_state=evidence["content_state"], platform_run_id=run_id, content_evidence=evidence,
                          archive_pending=evidence["content_state"] == "complete" and record["state"] != "complete")
            self._save(record)
            self.store.event("cloud_content_checked", folder_id, record["project_id"], evidence)
        return self.get_project(folder_id)

    def _bound_content_evidence(self, record):
        """Read the already-bound run and actual files, with no ledger writes."""
        run_id = record.get("platform_run_id")
        try:
            if not run_id:
                raise GateError("CONTENT_COMPLETION_NOT_VERIFIED")
            evidence = self._content(record, run_id)
        except Exception as exc:
            evidence = {"content_state": "incomplete", "gaps": ["content_verification_unavailable"],
                        "platform_run_id": run_id, "checked_at": now(),
                        "verification_error": type(exc).__name__, "verification_code": getattr(exc, "code", None)}
        return evidence

    def _live_content_view(self, record):
        """Derive scheduling flags from current evidence, never from a stale cache."""
        view = dict(record)
        view["persisted_content_state"] = record["content_state"]
        if record.get("platform_run_id") or record["content_state"] == "complete":
            evidence = self._bound_content_evidence(record)
            view.update(content_state=evidence["content_state"], content_evidence=evidence,
                        content_check_mode="live_read_only")
        else:
            view["content_check_mode"] = "unbound"
        view["archive_pending"] = view["content_state"] == "complete" and view["state"] != "complete"
        view["repair_required"] = view["state"] == "complete" and view["content_state"] != "complete"
        return view

    def _review_archived_content(self, record):
        """Preserve a proven remote move even when content verification fails."""
        evidence = self._bound_content_evidence(record)
        record.update(content_state=evidence["content_state"], content_evidence=evidence,
                      archive_pending=evidence["content_state"] == "complete" and record["state"] != "complete")
        self.store.event("cloud_content_checked", record["folder_id"], record["project_id"], evidence)
        return record

    def _project_move_result(self, job_id, record):
        return {**self.move_status(job_id), "location_state": record["state"],
                "content_state": record["content_state"], "archive_pending": record["archive_pending"],
                "repair_required": record["state"] == "complete" and record["content_state"] != "complete"}

    def _expected_record(self, record, intent):
        if (record["state"] != intent["expected_state"] or record["path"] != intent["source"]["path"]
                or record["parent_id"] != intent["source"]["parent_id"] or record["location_version"] != intent["location_version"]):
            raise GateError("MOVE_EXPECTED_STATE_CHANGED")

    def _expected(self, record, intent, source, destination):
        self._expected_record(record, intent)
        a = self._listing(source, self.containers[intent["expected_state"]], fresh=True)
        b = self._listing(destination, self.containers[intent["to_state"]], fresh=True)
        verdict, details = _reconciliation(intent, a, b)
        if verdict != "still_at_source_not_proof_of_non_execution" or not a["enumeration_complete"] or not b["enumeration_complete"]:
            raise GateError("MOVE_PRECONDITION_NOT_MET", verdict)
        return a, b

    def prepare_move(self, folder_id, *, to_state, expected_state, source, destination, lock):
        self._lock(folder_id, lock)
        if (expected_state, to_state) not in TRANSITIONS:
            raise GateError("INVALID_LIFECYCLE_TRANSITION")
        with self.store.transaction():
            record = self.get_project(folder_id)
            dest = self.containers[to_state]
            dest_path = dest["path"] + "/" + PurePosixPath(record["path"]).name
            self._permission(record, (dest_path,), (record["parent_id"], dest["folder_id"]))
            if record["move_job_id"]:
                job, old = self._move(record["move_job_id"])
                if job["state"] != "verified_complete":
                    if old["to_state"] != to_state or old["expected_state"] != expected_state:
                        raise GateError("MOVE_RECONCILIATION_REQUIRED")
                    return {"job_id": job["job_id"], "status": job["state"], "may_send": False, "intent": old}
                if record["state"] == to_state:
                    return self.move_status(job["job_id"])
            intent = {"schema_version": "1.0", "account_ref": record["account_ref"], "folder_id": folder_id,
                      "project_id": record["project_id"], "expected_state": expected_state, "to_state": to_state,
                      "location_version": record["location_version"],
                      "source": {"parent_id": record["parent_id"], "path": record["path"]},
                      "destination": {"parent_id": dest["folder_id"], "path": dest_path},
                      "ownership_ref": record["ownership_ref"]}
            self._expected(record, intent, source, destination)
            if to_state == "complete":
                if not record["platform_run_id"]:
                    raise GateError("CONTENT_COMPLETION_NOT_VERIFIED")
                record = self.refresh_content(folder_id, record["platform_run_id"], lock=lock)
                if record["content_state"] != "complete":
                    return {"job_id": None, "status": "blocked", "may_send": False,
                            "reason": "required_material_gaps", "content_evidence": record["content_evidence"]}
            if to_state == "complete" and self._scope_event(record, fresh=True) is None:
                raise GateError("PROJECT_SCOPE_NOT_BOUND")
            if expected_state == "complete" and to_state == "extracting":
                record = self._review_archived_content(record)
                self._save(record)
                if record["content_state"] == "complete":
                    return {"job_id": None, "status": "blocked", "may_send": False, "reason": "repair_not_required"}
            provider = self.store.register_provider("cloud-lifecycle", "1.0", runtime="authorized_browser", config_version="cloud-lifecycle-v1")
            job = self.store.queue_job(record["project_id"], _OPERATION, provider, digest(intent),
                                       config_version="cloud-lifecycle-v1", estimated_cost_minor=0, max_cost_minor=0)
            if not self.store.connection.execute("SELECT 1 FROM events WHERE kind=? AND record_id=?", (_MOVE_KIND, job["job_id"])).fetchone():
                self.store.event(_MOVE_KIND, job["job_id"], record["project_id"], intent)
            record["move_job_id"] = job["job_id"]; self._save(record)
        return {"job_id": job["job_id"], "status": job["state"], "may_send": False, "intent": intent}

    def _move(self, job_id):
        job, intent = _load_move(self.store.connection, job_id)
        if intent["account_ref"] != self.config.account_ref:
            raise GateError("ACCOUNT_IDENTITY_MISMATCH")
        return job, intent

    def move_status(self, job_id):
        job, intent = self._move(job_id)
        receipt = json.loads(job["receipt_json"]) if job["receipt_json"] else None
        confirmed = False
        if job["state"] == "verified_complete":
            if not isinstance(receipt, dict) or receipt.get("state") not in {"remote_confirmed", "synthetic_confirmed"}:
                raise ContractError("verified move has no validated platform location receipt")
            try:
                a = self._listing(DirectoryReadback.from_dict(receipt["source"]), self.containers[intent["expected_state"]], not_before=self._submission_time(job_id))
                b = self._listing(DirectoryReadback.from_dict(receipt["destination"]), self.containers[intent["to_state"]], not_before=self._submission_time(job_id))
            except (KeyError, TypeError) as exc:
                raise ContractError("verified move receipt is incomplete") from exc
            real = a["evidence_class"] == b["evidence_class"] == "real_authorized_browser"
            if (_reconciliation(intent, a, b)[0] != "destination_only"
                    or receipt.get("folder_id") != intent["folder_id"] or receipt.get("account_ref") != intent["account_ref"]
                    or (receipt["state"] == "remote_confirmed") != real):
                raise ContractError("verified move receipt does not prove stable ID at destination only")
            confirmed = real
        return {"job_id": job_id, "status": "remote_confirmed" if confirmed else
                "synthetic_confirmed" if job["state"] == "verified_complete" else job["state"],
                "remote_confirmed": bool(confirmed), "may_send": False, "intent": intent, "receipt": receipt}

    def begin_move(self, job_id, *, source=None, destination=None, lock):
        """Commit submission_unknown before returning the one-use may_send grant."""
        if self.store.connection.in_transaction:
            raise GateError("OUTER_TRANSACTION_CANNOT_AUTHORIZE_SEND")
        with self.store.transaction():
            return self._begin_move(job_id, source=source, destination=destination, lock=lock)

    def _begin_move(self, job_id, *, source, destination, lock):
        job, intent = self._move(job_id); fid = intent["folder_id"]
        self._lock(fid, lock)
        record = self.get_project(fid)
        self._permission(record, (intent["destination"]["path"],), (intent["source"]["parent_id"], intent["destination"]["parent_id"]))
        if job["state"] != "queued":
            return self.move_status(job_id)
        self._expected(record, intent, source, destination)
        if intent["to_state"] == "complete":
            record = self.refresh_content(fid, record["platform_run_id"], lock=lock)
            if record["content_state"] != "complete":
                return {"job_id": job_id, "status": "blocked", "may_send": False,
                        "reason": "required_material_gaps", "content_evidence": record["content_evidence"]}
        if intent["to_state"] == "complete" and self._scope_event(record, fresh=True) is None:
            raise GateError("PROJECT_SCOPE_NOT_BOUND")
        if intent["expected_state"] == "complete" and intent["to_state"] == "extracting":
            record = self._review_archived_content(record)
            self._save(record)
            if record["content_state"] == "complete":
                return {"job_id": job_id, "status": "blocked", "may_send": False, "reason": "repair_not_required"}
        result = self.store.begin_submission(job_id)
        if result["may_send"]:
            self.store.event("cloud_move_submission_unknown", job_id, record["project_id"],
                             {"request_id": result["request_id"], "intent_sha256": digest(intent)})
        return {**result, "job_id": job_id, "intent": intent, "remote_confirmed": False}

    def _submission_time(self, job_id):
        row = self.store.connection.execute("SELECT created_at FROM events WHERE kind='cloud_move_submission_unknown' AND record_id=? ORDER BY created_at DESC LIMIT 1", (job_id,)).fetchone()
        if row is None:
            raise GateError("MOVE_NOT_SUBMITTED")
        return row[0]

    def reconcile_move(self, job_id, *, source, destination, lock):
        with self.store.transaction():
            job, intent = self._move(job_id); fid = intent["folder_id"]
            self._lock(fid, lock)
            record = self.get_project(fid)
            self._permission(record, (intent["destination"]["path"],), (intent["source"]["parent_id"], intent["destination"]["parent_id"]))
            if job["state"] == "verified_complete":
                # Repeated acknowledgements cannot hide content damage that arose
                # after the original move. Never replay or rewrite its location proof.
                if record["state"] == "complete":
                    record = self._review_archived_content(record)
                    self._save(record)
                return self._project_move_result(job_id, record)
            if job["state"] != "submission_unknown":
                raise GateError("MOVE_NOT_IN_RECONCILIATION")
            self._expected_record(record, intent)
            submitted_at = self._submission_time(job_id)
            a = self._listing(source, self.containers[intent["expected_state"]], fresh=True, not_before=submitted_at)
            b = self._listing(destination, self.containers[intent["to_state"]], fresh=True, not_before=submitted_at)
            verdict, details = _reconciliation(intent, a, b)
            confirmed = verdict == "destination_only"
            real = a["evidence_class"] == b["evidence_class"] == "real_authorized_browser"
            receipt = {"state": ("remote_confirmed" if real else "synthetic_confirmed") if confirmed else "submission_unknown",
                       "verdict": verdict, "account_ref": intent["account_ref"], "folder_id": fid,
                       "source": a, "destination": b, "conflicts": details,
                       "completion_scope": "remote_folder_location_only_not_content", "checked_at": now()}
            self.store.reconcile_job(job_id, state="verified_complete" if confirmed else "submission_unknown",
                                     receipt=receipt, settled_cost_minor=0 if confirmed else None)
            if confirmed:
                record.update(state=intent["to_state"], path=intent["destination"]["path"], parent_id=intent["destination"]["parent_id"])
                if record["path"] not in record["known_paths"]:
                    record["known_paths"].append(record["path"])
                if "complete" in {intent["expected_state"], intent["to_state"]}:
                    record = self._review_archived_content(record)
                record["archive_pending"] = record["content_state"] == "complete" and record["state"] != "complete"
                self._save(record, location_changed=True)
            self.store.event("cloud_move_reconciled", job_id, record["project_id"],
                             {"state": receipt["state"], "verdict": verdict, "source_ref": a["evidence_ref"], "destination_ref": b["evidence_ref"]})
            result = self._project_move_result(job_id, record)
            if verdict == "identity_missing_both_sides":
                result["identity_search"] = self.identity_search_plan(job_id)
            return result

    def retry_move(self, job_id, *, non_execution: OperationReadback, source, destination, lock):
        """Explicit safe retry of the SAME intent; never infer non-execution by absence.

        A platform receipt must correlate the actual previous request ID. If the
        platform cannot provide that correlation, keep unknown and do not retry.
        """
        if self.store.connection.in_transaction:
            raise GateError("OUTER_TRANSACTION_CANNOT_AUTHORIZE_SEND")
        with self.store.transaction():
            job, intent = self._move(job_id); fid = intent["folder_id"]
            self._lock(fid, lock)
            if job["state"] != "submission_unknown":
                return self.move_status(job_id)
            proof = non_execution
            if (not isinstance(proof, OperationReadback) or proof.account_ref != self.config.account_ref
                    or proof.request_id != job["request_id"] or proof.state != "not_executed"
                    or proof.evidence_class != "real_authorized_browser"):
                raise GateError("CORRELATED_PLATFORM_NON_EXECUTION_PROOF_REQUIRED")
            check_id(proof.evidence_ref)
            if _instant(proof.observed_at) < _instant(self._submission_time(job_id)):
                raise GateError("STALE_PLATFORM_READBACK")
            record = self.get_project(fid)
            self._permission(record, (intent["destination"]["path"],), (intent["destination"]["parent_id"],))
            a, b = self._expected(record, intent, source, destination)
            if min(_instant(a["observed_at"]), _instant(b["observed_at"])) < _instant(proof.observed_at):
                raise GateError("STALE_PLATFORM_READBACK")
            self.store.event("cloud_move_not_executed", job_id, record["project_id"],
                             {"request_id": proof.request_id, "evidence_ref": proof.evidence_ref, "observed_at": proof.observed_at})
            # This narrow reset is NOT the generic Store's unknown retry path:
            # the previous request was positively proven not executed above.
            self.store.connection.execute("UPDATE jobs SET state='queued',request_id=NULL,reserved_cost_minor=0,updated_at=? WHERE job_id=? AND state='submission_unknown'", (now(), job_id))
            return self._begin_move(job_id, source=source, destination=destination, lock=lock)

    def identity_search_plan(self, job_id):
        job, intent = self._move(job_id)
        used = self.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='cloud_identity_search' AND record_id=?", (job_id,)).fetchone()[0]
        return {"account_ref": intent["account_ref"], "folder_id": intent["folder_id"], "roots": list(self.search_roots),
                "remaining_rounds": max(0, self.config.max_search_rounds - used),
                "max_directories": self.config.max_search_directories, "max_entries": self.config.max_search_entries,
                "state": job["state"], "may_send": False, "never_reimport_or_match_by_name": True}

    def record_identity_search(self, job_id, listings, *, lock):
        with self.store.transaction():
            job, intent = self._move(job_id); fid = intent["folder_id"]
            self._lock(fid, lock); self._permission(self.get_project(fid))
            plan = self.identity_search_plan(job_id)
            if job["state"] != "submission_unknown" or plan["remaining_rounds"] == 0:
                raise GateError("IDENTITY_SEARCH_BOUND_REACHED_OR_NOT_UNKNOWN")
            if not isinstance(listings, (tuple, list)) or not 1 <= len(listings) <= plan["max_directories"]:
                raise ContractError("identity search directory bound exceeded")
            values = [self._listing(x, fresh=True, not_before=self._submission_time(job_id)) for x in listings]
            if sum(len(x["entries"]) for x in values) > plan["max_entries"] or any(not any(_inside(x["path"], r) for r in self.search_roots) for x in values):
                raise GateError("IDENTITY_SEARCH_OUTSIDE_BOUNDED_SCOPE")
            found = [e for v in values for e in v["entries"] if e["folder_id"] == fid]
            self.store.event("cloud_identity_search", job_id, intent["project_id"],
                             {"folder_id": fid, "readback_refs": [v["evidence_ref"] for v in values], "found": found,
                              "enumeration_complete": all(v["enumeration_complete"] for v in values)})
            return {"job_id": job_id, "state": "submission_unknown", "found": found,
                    "may_send": False, "requires_two_sided_reconciliation": True, "search": self.identity_search_plan(job_id)}

    def queue(self):
        """Read-only live gate; reconcile first, then date-sort work and repairs.

        Stored content_state is a checkpoint, never admission. Rehash bound run
        artifacts on every queue read and derive archive/repair flags in memory.
        Do not require a prior refresh_content/reconcile_move to discover damage.
        """
        rows = self.store.connection.execute(f"SELECT folder_id FROM {_TABLE} WHERE account_ref=?", (self.config.account_ref,)).fetchall()
        projects, denied, reconcile = [], [], []
        for row in rows:
            record = self.get_project(row[0])
            try:
                self._permission(record)
            except GateError as exc:
                denied.append({"folder_id": row[0], "reason": exc.code}); continue
            record = self._live_content_view(record)
            if record["move_job_id"]:
                move = self.move_status(record["move_job_id"])
                if move["status"] == "submission_unknown":
                    reconcile.append(move)
            if record["state"] in {"pending", "extracting"} or record["repair_required"]:
                action = "repair_content" if record["repair_required"] else "archive_only" if record["content_state"] == "complete" else "process_content"
                projects.append({**record, "action": action})
        projects.sort(key=lambda r: (-(date.fromisoformat(r["date"]["project_date"]).toordinal()) if r["date"]["project_date"] else 1,
                                     r["account_ref"], r["folder_id"]))
        reconcile.sort(key=lambda r: r["intent"]["folder_id"])
        return {"reconcile_first": reconcile, "projects": projects, "denied": denied,
                "next_action": "reconcile_move" if reconcile else "project" if projects else "idle",
                "next_folder_id": reconcile[0]["intent"]["folder_id"] if reconcile else projects[0]["folder_id"] if projects else None}


def main(argv=None):
    """Offline CLI: read-only existing DB, no Store init/migration/remote actions."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="existing Store SQLite DB (opened read-only)")
    parser.add_argument("--root", help="Store.root for live queue checks; inferred only for DBs inside 整理结果")
    subs = parser.add_subparsers(dest="command", required=True)
    status = subs.add_parser("status"); status.add_argument("--account")
    queue = subs.add_parser("queue"); queue.add_argument("--account")
    inspect = subs.add_parser("inspect-move"); inspect.add_argument("job_id")
    preview = subs.add_parser("reconcile-preview", help="offline verdict only; does not grant send or remote confirmation")
    preview.add_argument("job_id"); preview.add_argument("--source", required=True); preview.add_argument("--destination", required=True)
    dates = subs.add_parser("date"); dates.add_argument("project_name"); dates.add_argument("--course-root-name")
    args = parser.parse_args(argv)
    try:
        if args.command == "date":
            result = choose_project_date(args.project_name, project_evidence_ref="offline-name-input",
                course_root_name=args.course_root_name, course_root_evidence_ref="offline-course-root-input" if args.course_root_name else None)
        else:
            if not args.db:
                parser.error("--db is required except for date")
            path = Path(args.db).absolute(); reject_links(path)
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                if args.command in {"inspect-move", "reconcile-preview"}:
                    job, intent = _load_move(connection, args.job_id)
                    result = {"job_id": args.job_id, "job_state": job["state"], "intent": intent, "may_send": False, "read_only": True}
                    if args.command == "reconcile-preview":
                        values = []
                        for filename, side, state in ((args.source, "source", intent["expected_state"]), (args.destination, "destination", intent["to_state"])):
                            file = Path(filename); reject_links(file)
                            if file.stat().st_size > 2 * 1024**2:
                                raise ContractError("readback JSON exceeds 2 MiB")
                            value = _listing_data(DirectoryReadback.from_dict(json.loads(file.read_text(encoding="utf-8-sig"))), Policy(), account_ref=intent["account_ref"])
                            expected = intent[side]
                            if value["folder_id"] != expected["parent_id"] or value["path"] != str(PurePosixPath(expected["path"]).parent):
                                raise GateError("CONTAINER_IDENTITY_MISMATCH")
                            values.append(value)
                        result["preview_verdict"] = _reconciliation(intent, *values)[0]
                        result["remote_confirmed"] = False
                        result["note"] = "Preview only; API reconciliation also checks ownership, lock, freshness and submission chronology"
                else:
                    query = f"SELECT account_ref,folder_id FROM {_TABLE}" + (" WHERE account_ref=?" if args.account else "")
                    records = [_load_record(connection, row[0], row[1]) for row in connection.execute(query, (args.account,) if args.account else ())]
                    if args.command == "queue":
                        root = Path(args.root).absolute() if args.root else path.parent.parent if path.parent.name == "整理结果" else None
                        if root is None or not root.is_dir():
                            raise ContractError("queue needs --root for a nonstandard Store DB location")
                        reject_links(root)
                        store_view = SimpleNamespace(connection=connection, root=root, db_path=path, max_file_bytes=64 * 1024**2)
                        live = []
                        for record in records:
                            containers = {key: RemoteContainer(**value) for key, value in record["containers"].items()}
                            reader = CloudLifecycle(store_view, LifecycleConfig(record["account_ref"], "offline-cli", containers))
                            live.append(reader._live_content_view(record))
                        records = [{**r, "action": "repair_content" if r["repair_required"] else "archive_only" if r["content_state"] == "complete" else "process_content"}
                                   for r in live if r["state"] in {"pending", "extracting"} or r["repair_required"]]
                        records.sort(key=lambda r: (-(date.fromisoformat(r["date"]["project_date"]).toordinal()) if r["date"]["project_date"] else 1, r["account_ref"], r["folder_id"]))
                    result = {"read_only": True, "projects": records, "note": "Queue content checks are live/read-only; inventory only, no ownership/protection grant. Status is a stored checkpoint."}
            finally:
                connection.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ContractError, GateError, OSError, sqlite3.Error, ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc), "read_only": True}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
