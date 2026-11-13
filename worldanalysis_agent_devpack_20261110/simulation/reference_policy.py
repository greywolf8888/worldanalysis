"""Offline reference policies, NOT the Worldanalysis production pipeline.

Standard-library only. No network, model, cloud credentials, or user-file access.
BundlePublisher reads/writes only the test-owned directory supplied by its caller.
Known fixtures prove the stated predicates, not intelligent classification,
real-service compatibility, exactly-once networking, concurrency, or resource use.
"""

from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


class PolicyError(ValueError):
    pass


def dec(value):
    value = Decimal(str(value))
    if not value.is_finite() or value < 0:
        raise PolicyError("a finite nonnegative value is required")
    return value


def encoded_gb(hours, mbps):
    """Decimal GB, excluding headers, retries, and container overhead."""
    return dec(hours) * dec(mbps) * Decimal("0.45")


def pcm_bytes(seconds, sample_rate=16000, bytes_per_sample=2, channels=1):
    return dec(seconds) * dec(sample_rate) * dec(bytes_per_sample) * dec(channels)


def network_lower_bound_hours(size_gb, effective_mbps):
    speed = dec(effective_mbps)
    if speed == 0:
        raise PolicyError("effective throughput must be positive")
    return dec(size_gb) * Decimal(8_000) / speed / Decimal(3600)


def asr_hours(audio_hours, measured_or_assumed_rtf):
    return dec(audio_hours) * dec(measured_or_assumed_rtf)


def asr_price_cny(audio_hours, region):
    # Official qwen3-asr-flash-filetrans list-price snapshot, 2026-11-10.
    # Not a live quote; excludes storage, retries, free tiers, and other services.
    prices = {"beijing": Decimal("0.00022"), "singapore": Decimal("0.00026")}
    if region not in prices:
        raise PolicyError("unknown pricing region")
    return dec(audio_hours) * Decimal(3600) * prices[region]


@dataclass(frozen=True)
class Evidence:
    text: str = ""
    coverage: str = "unknown"
    expected_images: int | None = 0
    exported_images: int = 0
    audio_expected: bool = False
    transcript_kind: str = "none"


def evidence_state(evidence):
    """The caller supplies fixture facts; this function does NOT discover facts."""
    if evidence.coverage != "complete":
        return "READY_WITH_GAPS"
    if evidence.expected_images is None:
        return "READY_WITH_GAPS"
    if evidence.expected_images < 0 or evidence.exported_images < 0:
        raise PolicyError("negative image counts")
    if evidence.exported_images != evidence.expected_images:
        return "READY_WITH_GAPS"
    if evidence.audio_expected and evidence.transcript_kind != "full_transcript":
        return "READY_WITH_GAPS"
    if not evidence.text.strip() and evidence.exported_images == 0:
        return "READY_WITH_GAPS"
    return "READY_VERIFIED"


@dataclass(frozen=True)
class CourseIdentity:
    author_id: str | None
    content_fingerprint: str | None
    edition: str | None


def dedup_decision(left, right):
    if not all((left.author_id, right.author_id,
                left.content_fingerprint, right.content_fingerprint)):
        return "KEEP_PENDING_IDENTITY"
    if left.author_id != right.author_id:
        return "KEEP_SEPARATE_AUTHORS"
    if left.content_fingerprint != right.content_fingerprint:
        return "KEEP_DIFFERENT_CONTENT"
    if not left.edition or not right.edition:
        return "KEEP_PENDING_IDENTITY"
    if left.edition != right.edition:
        return "KEEP_VERSION_RELATION"
    return "REUSE_CONTENT_KEEP_PROVENANCE"


def contact_decision(repeated_across_unrelated_courses=False):
    # Repetition is only a signal. Neither return value authorizes deletion.
    return ("KEEP_AND_FLAG_FOR_REVIEW" if repeated_across_unrelated_courses
            else "KEEP_WITH_SOURCE")


def validate_baidu_path(path, allowed_root="/apps/教育分析学习"):
    """Strict already-decoded POSIX path contract, not a URL decoder.

    Percent-encoded inputs are rejected, not repeatedly decoded. A production
    connector must establish one canonical path representation before this gate.
    This lexical gate alone does not prove remote API scope or server identity.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        raise PolicyError("absolute POSIX path required")
    if "\\" in path or "\x00" in path or "//" in path:
        raise PolicyError("ambiguous path syntax")
    if re.search(r"%[0-9a-fA-F]{2}", path):
        raise PolicyError("encoded path is outside the input contract")
    parts = path.split("/")[1:]
    if any(part in (".", "..") for part in parts):
        raise PolicyError("path traversal or dot segment")
    candidate = path.rstrip("/")
    if candidate != allowed_root and not candidate.startswith(allowed_root + "/"):
        raise PolicyError("outside authorized root")
    return candidate


def verify_range_response(status, content_range, requested_start,
                          requested_end, observed_body_bytes):
    """A 200 reply disables the Range route; never read its full body by default."""
    if status != 206 or requested_start < 0 or requested_end < requested_start:
        return False
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range or "")
    if not match:
        return False
    start, end, total = map(int, match.groups())
    return (total > end >= start == requested_start
            and end == min(requested_end, total - 1)
            and observed_body_bytes == end - start + 1)


@dataclass
class ByteBudget:
    limit: int
    observed: int = 0

    def __post_init__(self):
        if self.limit < 0 or self.observed < 0:
            raise PolicyError("invalid byte budget")

    def allowance(self, requested):
        if requested < 0:
            raise PolicyError("negative read request")
        return min(requested, max(0, self.limit - self.observed))

    def account_read(self, actual):
        if actual < 0:
            raise PolicyError("negative observed bytes")
        self.observed += actual
        if self.observed > self.limit:
            raise PolicyError("cumulative byte ceiling exceeded; stop reading")


def failure_action(error_code):
    if error_code in {"ACCESS_DENIED", "POLICY_BLOCKED", "AUTH_EXPIRED"}:
        return "BLOCKED_NO_TOOL_SWITCH"
    if error_code in {"TIMEOUT", "TRANSIENT_NETWORK"}:
        return "BOUNDED_RETRY_SAME_AUTHORIZED_ROUTE"
    return "INSPECT_WITHOUT_NEW_AUTHORITY"


@dataclass
class Reservation:
    estimate: Decimal
    state: str = "RESERVED"
    remote_task_id: str | None = None


@dataclass
class CommercialBudget:
    ceiling: Decimal
    external_allowed: bool = False
    reservations: dict = field(default_factory=dict)

    def __post_init__(self):
        self.ceiling = dec(self.ceiling)

    @property
    def reserved_total(self):
        return sum((r.estimate for r in self.reservations.values()), Decimal(0))

    def reserve(self, request_key, estimated_cny):
        # Stable request keys include source version, chunk, provider and params.
        estimate = dec(estimated_cny)
        if request_key in self.reservations:
            return "EXISTING_REQUEST_DO_NOT_RESUBMIT"
        if not self.external_allowed or self.ceiling == 0:
            raise PolicyError("new external submission is not authorized")
        if self.reserved_total + estimate > self.ceiling:
            raise PolicyError("budget ceiling exceeded")
        self.reservations[request_key] = Reservation(estimate)
        return "RESERVED"

    def begin_submit(self, request_key):
        item = self.reservations[request_key]
        if item.state != "RESERVED":
            raise PolicyError("do not issue an ambiguous or duplicate submission")
        # Production must persist this transition BEFORE sending a request.
        item.state = "DISPATCHING"

    def mark_unknown(self, request_key):
        self.reservations[request_key].state = "SUBMISSION_UNKNOWN"
        # Keep the reservation until reconciliation proves the actual outcome.

    def mark_submitted(self, request_key, remote_task_id):
        item = self.reservations[request_key]
        item.remote_task_id = remote_task_id
        item.state = "SUBMITTED"


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


class SimulatedCrash(RuntimeError):
    pass


class BundlePublisher:
    """Single-process fixture publisher for a rename/DB recovery example only.

    No concurrency, adversarial symlink, power-loss, Windows-locking, or real
    filesystem durability guarantees are made by this demonstration.
    """
    def __init__(self, base, connection):
        self.base = Path(base)
        self.db = connection
        self.db.execute("""CREATE TABLE IF NOT EXISTS bundles (
            bundle_id TEXT NOT NULL, revision TEXT NOT NULL, digest TEXT NOT NULL,
            PRIMARY KEY (bundle_id, revision))""")
        self.db.commit()

    @staticmethod
    def check_id(value):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise PolicyError("unsafe fixture identifier")

    def register(self, manifest):
        row = (manifest["bundle_id"], manifest["revision"], manifest["digest"])
        self.db.execute("INSERT OR IGNORE INTO bundles VALUES (?, ?, ?)", row)
        existing = self.db.execute(
            "SELECT digest FROM bundles WHERE bundle_id=? AND revision=?", row[:2]
        ).fetchone()
        if existing != (row[2],):
            self.db.rollback()
            raise PolicyError("revision identity conflict")
        self.db.commit()

    def verify_published(self, directory):
        manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
        self.check_id(manifest["bundle_id"])
        self.check_id(manifest["revision"])
        if directory.parent.name != manifest["bundle_id"] or directory.name != manifest["revision"]:
            raise PolicyError("directory identity mismatch")
        digest = hashlib.sha256((directory / "evidence.json").read_bytes()).hexdigest()
        if digest != manifest["digest"]:
            raise PolicyError("published evidence checksum mismatch")
        return manifest

    def publish(self, bundle_id, revision, payload, crash_after_rename=False):
        self.check_id(bundle_id)
        self.check_id(revision)
        evidence = canonical_json(payload)
        digest = hashlib.sha256(evidence).hexdigest()
        final = self.base / "published" / bundle_id / revision
        if final.exists():
            manifest = self.verify_published(final)
            if manifest["digest"] != digest:
                raise PolicyError("cannot overwrite an immutable evidence revision")
            self.register(manifest)
            return final
        stage = self.base / "staging" / bundle_id / revision
        stage.mkdir(parents=True, exist_ok=False)
        manifest = {"bundle_id": bundle_id, "revision": revision, "digest": digest}
        for filename, data in (("evidence.json", evidence),
                               ("manifest.json", canonical_json(manifest))):
            with (stage / filename).open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        final.parent.mkdir(parents=True, exist_ok=True)
        stage.rename(final)
        if crash_after_rename:
            raise SimulatedCrash("fixture interruption after rename, before DB commit")
        self.register(self.verify_published(final))
        return final

    def recover(self):
        checked = 0
        for manifest_path in sorted((self.base / "published").glob("*/*/manifest.json")):
            self.register(self.verify_published(manifest_path.parent))
            checked += 1
        return checked

