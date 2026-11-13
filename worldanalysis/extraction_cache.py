"""Restricted-local raw extraction cache in the Store's *existing* SQLite.

Only database-published, get_bundle-verified native small-file extractions enter
this cache.  It is not a completion ledger, an LLM envelope, or a reconstruction
from Bundle text previews.  None/False means absent/not applicable; damaged cache
or evidence always raises ContractError, never a request to silently re-extract.
A verified legacy job with no row may be explicitly reparsed once by its caller.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import uuid

from .contracts import (ContractError, asset_key, canonical, check_id, hash_file,
                        now, redact_text, reject_links, safe_path,
                        validate_relative_path)

_VERSION = 1
_MAX_JSON = 16 * 1024 * 1024
_MAX_CACHE = 64 * 1024 * 1024
_MAX_OUTPUT = 256 * 1024 * 1024
_MAX_FILES = 10000
_MAX_NODES = 300000
_MAX_DEPTH = 64
_REQUIRED = {"media_type", "provider_id", "provider_version", "blocks", "files",
             "gaps", "metrics", "enumeration_state", "processing_status", "metadata"}


def _positive(value, name, maximum):
    if type(value) is not int or not 0 < value <= maximum:
        raise ContractError("invalid extraction cache " + name)
    return value


def _relative(value):
    # Also reject single Windows separators, independent of the host OS.
    if not isinstance(value, str) or "\\" in value:
        raise ContractError("invalid extraction cache relative path")
    return validate_relative_path(value)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ContractError("duplicate extraction cache JSON key")
        result[key] = value
    return result


@contextmanager
def _fail_closed():
    try:
        yield
    except ContractError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError, OverflowError) as exc:
        # No raw values/paths/credentials in the public diagnostic, and most
        # importantly no conversion of corrupted evidence into a cache miss.
        raise ContractError("invalid extraction cache interface/evidence") from exc


class Cache:
    """One coordinator/connection; every schema/data write uses transaction().

    JSON (per row and total), tree depth/node count, raw file count and copied
    bytes have hard limits. File bytes stay in immutable published artifacts,
    not SQLite. Credential-shaped raw strings are deliberately retained only in
    this restricted-local table; do not expose its contents to LLMs/reports/logs.
    """

    def __init__(self, store, *, max_json_bytes=_MAX_JSON,
                 max_cache_bytes=_MAX_CACHE, max_output_bytes=None):
        self.store = store
        self.max_json_bytes = _positive(max_json_bytes, "JSON bound", _MAX_JSON)
        self.max_cache_bytes = _positive(max_cache_bytes, "total bound", _MAX_CACHE)
        ceiling = min(store.max_file_bytes * 4, _MAX_OUTPUT)
        self.max_output_bytes = _positive(ceiling if max_output_bytes is None else max_output_bytes,
                                          "output bound", ceiling)
        with store.transaction():
            store.connection.execute("""
                CREATE TABLE IF NOT EXISTS extraction_cache(
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
                    project_id TEXT NOT NULL REFERENCES projects(project_id),
                    asset_id TEXT NOT NULL,
                    asset_revision TEXT NOT NULL,
                    config_version TEXT NOT NULL,
                    provider_json TEXT NOT NULL,
                    bundle_id TEXT NOT NULL REFERENCES publications(bundle_id),
                    publish_id TEXT NOT NULL REFERENCES publications(publish_id),
                    cache_version INTEGER NOT NULL CHECK(cache_version=1),
                    access_class TEXT NOT NULL CHECK(access_class='restricted_local'),
                    payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB))<=16777216),
                    payload_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(asset_id,asset_revision) REFERENCES asset_revisions(asset_id,asset_revision)
                )
            """)

    def _encode(self, value):
        pending = [(value, 0)]
        nodes = size = 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if nodes > _MAX_NODES or depth > _MAX_DEPTH:
                raise ContractError("extraction cache JSON complexity bound exceeded")
            if isinstance(item, str):
                if len(item) > self.max_json_bytes:
                    raise ContractError("extraction cache JSON byte bound exceeded")
                size += len(item.encode("utf-8")) + 2
            elif type(item) is dict:
                if nodes + len(pending) + len(item) * 2 > _MAX_NODES:
                    raise ContractError("extraction cache JSON complexity bound exceeded")
                if any(type(key) is not str for key in item):
                    raise ContractError("extraction cache requires string JSON keys")
                pending.extend((child, depth + 1) for pair in item.items() for child in pair)
                size += 2 + len(item) * 2
            elif type(item) is list:
                if nodes + len(pending) + len(item) > _MAX_NODES:
                    raise ContractError("extraction cache JSON complexity bound exceeded")
                pending.extend((child, depth + 1) for child in item)
                size += 2 + len(item)
            elif item is None or type(item) in (bool, int):
                size += 5
            elif type(item) is float and math.isfinite(item):
                size += 8
            else:
                raise ContractError("extraction cache requires finite plain JSON")
            if size > self.max_json_bytes:
                raise ContractError("extraction cache JSON byte bound exceeded")
        try:
            result = canonical(value)
        except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
            raise ContractError("invalid extraction cache JSON") from exc
        if len(result) > self.max_json_bytes:
            raise ContractError("extraction cache JSON byte bound exceeded")
        return result

    def _decode(self, text):
        if type(text) is not str or len(text) > self.max_json_bytes:
            raise ContractError("extraction cache JSON byte bound exceeded")
        try:
            data = text.encode("utf-8")
            if len(data) > self.max_json_bytes:
                raise ContractError("extraction cache JSON byte bound exceeded")
            value = json.loads(text, object_pairs_hook=_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
            if self._encode(value) != data:
                raise ContractError("noncanonical extraction cache JSON")
            return value
        except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
            raise ContractError("invalid extraction cache JSON") from exc

    def _supported(self, entry):
        if not isinstance(entry, dict) or not isinstance(entry.get("asset"), dict):
            raise ContractError("extraction cache requires a registered base entry")
        asset = entry["asset"]
        # Media descriptors are a different contract. Never hash/copy them as an
        # original_small_file, nor infer their meaning from a filename.
        if entry.get("original_kind", "original_small_file") != "original_small_file":
            return False
        if (entry.get("input_kind", "small_file") != "small_file"
                or entry.get("media_descriptor") or entry.get("media_registration")
                or entry.get("media_source_ref") or asset.get("media_source_ref")):
            return False
        if self.store.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_sources'").fetchone():
            if self.store.connection.execute("SELECT 1 FROM media_sources WHERE asset_id=? AND asset_revision=?",
                                             (asset.get("asset_id"), asset.get("asset_revision"))).fetchone():
                return False
        binding = asset.get("source_binding", {})
        return (binding.get("kind") == "local"
                and not str(asset.get("media_type", "")).startswith(("audio/", "video/")))

    def _base(self, entry, project_id):
        store = self.store
        asset = entry["asset"]
        for key in ("asset_id", "asset_revision", "source_ref"):
            check_id(asset.get(key))
        check_id(project_id)
        if entry.get("project_id", project_id) != project_id:
            raise ContractError("extraction cache entry project mismatch")
        row = store.connection.execute("""
            SELECT r.* FROM asset_revisions r JOIN assets a USING(asset_id)
            JOIN source_assets sa USING(asset_id,asset_revision)
            JOIN sources s USING(source_id)
            WHERE a.project_id=? AND r.asset_id=? AND r.asset_revision=?
                AND s.source_id=? AND s.project_id=? AND s.kind='local'
        """, (project_id, *asset_key(asset), asset["source_ref"], project_id)).fetchone()
        if (not row or asset.get("size_bytes") != row["size_bytes"]
                or entry.get("original_relative_path") != row["original_relative_path"]
                or entry.get("original_sha256") != row["sha256"]
                or asset["source_binding"].get("local_input_ref") != asset["source_ref"]):
            raise ContractError("extraction cache asset/source/original ownership mismatch")
        original = {"relative_path": _relative(row["original_relative_path"]),
                    "sha256": row["sha256"], "byte_length": row["size_bytes"]}
        root = store.project_root(project_id)
        path = safe_path(root, original["relative_path"])
        if original["byte_length"] > store.max_file_bytes or path.stat().st_size > store.max_file_bytes:
            raise ContractError("extraction cache original byte bound exceeded")
        if hash_file(path) != (original["sha256"], original["byte_length"]):
            raise ContractError("extraction cache original hash mismatch")
        return original

    def _scope(self, entry, job_id, config_version, supplied=None, *, complete=False):
        check_id(job_id)
        check_id(config_version)
        job = self.store.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not job:
            raise ContractError("unknown extraction cache job")
        if supplied is not None:
            for field in ("job_id", "project_id", "operation", "provider_json", "input_revision", "idempotency_key"):
                if supplied.get(field) != job[field]:
                    raise ContractError("extraction cache supplied job scope mismatch")
        asset = entry["asset"]
        if (job["operation"] != "native_extract"
                or job["input_revision"] not in {asset["asset_revision"], asset["asset_id"] + ":" + asset["asset_revision"]}
                or complete and job["state"] != "verified_complete"
                or job["state"] in {"submission_unknown", "submitted", "waiting"}):
            raise ContractError("extraction cache job revision/operation/state mismatch")
        provider = self._decode(job["provider_json"])
        if not isinstance(provider, dict) or provider.get("config_version") != config_version:
            raise ContractError("extraction cache provider/config mismatch")
        registered = self.store.connection.execute("""SELECT provider_json FROM provider_configurations
            WHERE provider_id=? AND implementation_version=? AND config_version=?""",
            (provider.get("provider_id"), provider.get("implementation_version"), config_version)).fetchone()
        if not registered or self._decode(registered[0]) != provider:
            raise ContractError("extraction cache provider configuration not registered")
        original = self._base(entry, job["project_id"])
        scope = {"project_id": job["project_id"], "asset_id": asset["asset_id"],
                 "asset_revision": asset["asset_revision"], "job_id": job_id,
                 "provider": provider, "config_version": config_version}
        return scope, original, job

    def _raw(self, raw):
        self._encode(raw)
        if not isinstance(raw, dict) or not _REQUIRED <= raw.keys() or type(raw.get("metadata")) is not dict:
            raise ContractError("extraction cache requires the complete raw interface")
        if any(type(raw[field]) is not list for field in ("files", "blocks", "gaps", "metrics")):
            raise ContractError("invalid extraction cache raw collections")
        if len(raw["files"]) > _MAX_FILES:
            raise ContractError("extraction cache file count bound exceeded")
        check_id(raw["provider_id"])
        if type(raw["provider_version"]) is not str or not raw["provider_version"]:
            raise ContractError("extraction cache raw provider version required")
        keys, paths = set(), set()
        for file in raw["files"]:
            if type(file) is not dict or not {"key", "relative_path", "kind", "media_type"} <= file.keys():
                raise ContractError("incomplete extraction cache raw file")
            check_id(file["key"])
            relative = _relative(file["relative_path"])
            if file["key"] in keys or relative.casefold() in paths:
                raise ContractError("duplicate extraction cache raw key/path")
            keys.add(file["key"])
            paths.add(relative.casefold())
        for block in raw["blocks"]:
            if (type(block) is not dict
                    or not {"kind", "origin", "locator", "file_keys", "processing_status", "reason"} <= block.keys()
                    or type(block["locator"]) is not dict or type(block["file_keys"]) is not list
                    or len(block["file_keys"]) != len(set(block["file_keys"]))
                    or not set(block["file_keys"]) <= keys):
                raise ContractError("invalid extraction cache raw block")
            geometry = block["locator"].get("geometry")
            if geometry and geometry.get("reference_object_ref") not in keys:
                raise ContractError("extraction cache raw geometry key missing")
        for derivation in raw["metadata"].get("derivations", []):
            for field in ("block_indexes", "source_block_indexes"):
                if any(type(i) is not int or not 0 <= i < len(raw["blocks"]) for i in derivation.get(field, [])):
                    raise ContractError("extraction cache derivation block missing")
            if not set(derivation.get("source_file_keys", [])) <= keys:
                raise ContractError("extraction cache derivation file key missing")

    @staticmethod
    def _signature(artifact):
        return (artifact["sha256"], artifact["byte_length"], artifact["kind"], artifact["media_type"])

    def _bundle(self, scope, bundle_id, publish_id, original):
        bundle = self.store.get_bundle(scope["project_id"], bundle_id)
        ref = scope["asset_id"], scope["asset_revision"]
        if (bundle["publish_id"] != publish_id or not any(asset_key(a) == ref for a in bundle["assets"])):
            raise ContractError("extraction cache publication scope mismatch")
        own = [a for a in bundle["artifacts"] if asset_key(a["asset_ref"]) == ref and a["job_id"] == scope["job_id"]]
        originals = [a for a in own if a["kind"] == "original_small_file"]
        if (not own or any(a["provider"] != scope["provider"] for a in own) or len(originals) != 1
                or any(originals[0][key] != original[key] for key in original)):
            raise ContractError("extraction cache artifact/job/provider/original mismatch")
        return bundle, own

    def _receipt(self, scope, job, original, known=None):
        receipt = self._decode(job["receipt_json"] or "{}")
        if not isinstance(receipt, dict) or not receipt.get("publish_id"):
            raise ContractError("extraction cache job lacks a publication receipt")
        row = self.store.connection.execute("""SELECT bundle_id FROM publications
            WHERE project_id=? AND publish_id=? AND state='published'""",
            (scope["project_id"], receipt["publish_id"])).fetchone()
        if not row:
            raise ContractError("extraction cache job receipt is not published")
        if known is None or known["publish_id"] != receipt["publish_id"]:
            self._bundle(scope, row["bundle_id"], receipt["publish_id"], original)

    def _projection(self, raw, scope, original, bundle, artifacts, bindings):
        """Verify the published projection without inventing/reversing raw text."""
        provider = scope["provider"]
        # Registration's extension-based media guess is not format evidence.
        # The actual published original artifact was created from this raw type.
        original_artifact = next(a for a in artifacts if a["kind"] == "original_small_file")
        if raw["media_type"] != original_artifact["media_type"]:
            raise ContractError("extraction cache raw/published-original media type mismatch")
        if (raw["provider_id"] != provider["provider_id"]
                or raw["provider_version"] != provider["implementation_version"]):
            raise ContractError("extraction cache raw provider mismatch")
        metadata = canonical(raw["metadata"])
        signature = hashlib.sha256(metadata).hexdigest(), len(metadata), "structured_data", "application/json"
        if not any(self._signature(a) == signature for a in artifacts):
            raise ContractError("extraction cache raw metadata does not match published evidence")
        filemap = {f["key"]: self._signature(f) for f in bindings}
        artifactmap = {a["artifact_id"]: a for a in artifacts}
        blocks = [b for b in bundle["blocks"] if asset_key(b["asset_ref"]) == (scope["asset_id"], scope["asset_revision"])]
        if len(blocks) != len(raw["blocks"]):
            raise ContractError("extraction cache raw block inventory mismatch")
        original_signature = original["sha256"], original["byte_length"], "original_small_file", raw["media_type"]
        for source, published in zip(raw["blocks"], blocks):
            locator, sealed = deepcopy(source["locator"]), deepcopy(published["locator"])
            if "geometry" in locator:
                locator["geometry"]["reference_object_ref"] = filemap[locator["geometry"]["reference_object_ref"]]
                ident = sealed.get("geometry", {}).get("reference_object_ref")
                if ident not in artifactmap:
                    raise ContractError("extraction cache published geometry artifact missing")
                sealed["geometry"]["reference_object_ref"] = self._signature(artifactmap[ident])
            if (locator != sealed or source["kind"] != published["kind"] or source["origin"] != published["origin"]
                    or source["processing_status"] != published["processing"]["status"]
                    or (redact_text(source["reason"])[0] or "extraction observation") != published["processing"]["reason"]
                    or published["processing"]["recovery_ref"] != scope["job_id"]):
                raise ContractError("extraction cache raw block/locator mismatch")
            expected = [filemap[key] for key in source["file_keys"]]
            if "text" in source:
                text = redact_text(str(source["text"]))[0]
                if published.get("text") != text[:16000]:
                    raise ContractError("extraction cache raw text differs from published projection")
                data = text.encode("utf-8")
                expected.append((hashlib.sha256(data).hexdigest(), len(data), "native_text", "text/plain"))
            elif "text" in published:
                raise ContractError("extraction cache raw text missing")
            if not expected and source["processing_status"] == "extracted":
                expected = [original_signature]
            actual = [self._signature(artifactmap[ident]) for ident in published["artifact_ids"]]
            if sorted(expected) != sorted(actual):
                raise ContractError("extraction cache raw file keys do not match published block")
        coverage = next(c for c in bundle["coverage"]["per_asset"]
                        if asset_key(c["asset_ref"]) == (scope["asset_id"], scope["asset_revision"]))
        def metrics(items):
            return [m for m in items if m["unit"] not in {"block", "visual_occurrence"}]
        status = raw["processing_status"]
        if status == "verified_complete" and (coverage["gap_ids"] or any(b["processing_status"] not in {"extracted", "not_applicable"} for b in raw["blocks"])):
            status = "partial"
        if (coverage["enumeration_state"] != raw["enumeration_state"]
                or coverage["processing_status"] != status or metrics(coverage["metrics"]) != metrics(raw["metrics"])):
            raise ContractError("extraction cache raw coverage mismatch")
        gaps = {g["gap_id"]: g for g in bundle["gaps"]}
        if len(raw["gaps"]) > len(coverage["gap_ids"]):
            raise ContractError("extraction cache raw gap inventory mismatch")
        for source, ident in zip(raw["gaps"], coverage["gap_ids"]):
            published = gaps[ident]
            if (any(source[key] != published[key] for key in ("kind", "severity"))
                    or redact_text(source["reason"])[0] != published["reason"]
                    or [blocks[i]["block_id"] for i in source["block_indexes"]] != [r["block_id"] for r in published["target_refs"]]):
                raise ContractError("extraction cache raw gap mismatch")

    def _bindings(self, entry, artifacts):
        result, total = [], 0
        for file in entry["raw"]["files"]:
            path = safe_path(Path(entry["output_dir"]), file["relative_path"])
            size = path.stat().st_size
            if size > self.max_output_bytes - total:
                raise ContractError("extraction cache output byte bound exceeded")
            sha, size = hash_file(path)
            total += size
            if total > self.max_output_bytes:
                raise ContractError("extraction cache output byte bound exceeded")
            signature = sha, size, file["kind"], file["media_type"]
            candidates = [a for a in artifacts if self._signature(a) == signature]
            if not candidates:
                raise ContractError("extraction cache raw file has no published artifact binding")
            # Identical bytes/kind/media may share a published file. Retain each
            # raw key/path separately, without hardcoding Store's output layout.
            artifact = min(candidates, key=lambda a: (a["relative_path"], a["artifact_id"]))
            result.append({"key": file["key"], "relative_path": file["relative_path"],
                           "artifact_id": artifact["artifact_id"], "artifact_relative_path": artifact["relative_path"],
                           "sha256": sha, "byte_length": size, "kind": file["kind"], "media_type": file["media_type"]})
        return result

    def _cached(self, row, scope, original, job):
        if (row["cache_version"] != _VERSION or row["access_class"] != "restricted_local"
                or any(row[key] != scope[key] for key in ("project_id", "asset_id", "asset_revision", "job_id", "config_version"))
                or self._decode(row["provider_json"]) != scope["provider"]
                or job["state"] != "verified_complete"):
            raise ContractError("extraction cache row scope/state mismatch")
        # Check lengths in SQLite before materializing payload text in the caller.
        payload = self._decode(row["payload_json"])
        if hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest() != row["payload_sha256"]:
            raise ContractError("extraction cache payload digest mismatch")
        if (type(payload) is not dict or set(payload) != {"version", "scope", "original", "bundle_id", "publish_id", "raw", "files"}
                or payload["version"] != _VERSION or payload["scope"] != scope or payload["original"] != original
                or payload["bundle_id"] != row["bundle_id"] or payload["publish_id"] != row["publish_id"]):
            raise ContractError("extraction cache payload scope mismatch")
        raw = payload["raw"]
        self._raw(raw)
        if type(payload["files"]) is not list or len(payload["files"]) != len(raw["files"]):
            raise ContractError("extraction cache raw file binding inventory mismatch")
        bundle, artifacts = self._bundle(scope, row["bundle_id"], row["publish_id"], original)
        artifactmap = {a["artifact_id"]: a for a in artifacts}
        total = 0
        for file, binding in zip(raw["files"], payload["files"]):
            if (type(binding) is not dict
                    or set(binding) != {"key", "relative_path", "artifact_id", "artifact_relative_path", "sha256", "byte_length", "kind", "media_type"}
                    or any(binding[key] != file[key] for key in ("key", "relative_path", "kind", "media_type"))):
                raise ContractError("extraction cache raw file binding mismatch")
            _relative(binding["artifact_relative_path"])
            artifact = artifactmap.get(binding["artifact_id"])
            if (not artifact or artifact["relative_path"] != binding["artifact_relative_path"]
                    or self._signature(artifact) != self._signature(binding)
                    or type(binding["byte_length"]) is not int or binding["byte_length"] < 0):
                raise ContractError("extraction cache artifact binding mismatch")
            total += binding["byte_length"]
            if total > self.max_output_bytes:
                raise ContractError("extraction cache output byte bound exceeded")
        self._receipt(scope, job, original, known=bundle)
        self._projection(raw, scope, original, bundle, artifacts, payload["files"])
        return payload

    def _row(self, job_id):
        total = self.store.connection.execute("SELECT COALESCE(SUM(length(CAST(payload_json AS BLOB))+length(CAST(provider_json AS BLOB))),0) FROM extraction_cache").fetchone()[0]
        if total > self.max_cache_bytes:
            raise ContractError("extraction cache total JSON byte bound exceeded")
        limits = [(key, 200) for key in ("job_id", "project_id", "asset_id", "asset_revision", "config_version", "bundle_id", "publish_id")]
        limits += [("payload_json", self.max_json_bytes), ("provider_json", self.max_json_bytes),
                   ("payload_sha256", 64), ("created_at", 64), ("access_class", 32)]
        sizes = self.store.connection.execute("SELECT " + ",".join("length(CAST(" + key + " AS BLOB))" for key, _ in limits)
                                              + " FROM extraction_cache WHERE job_id=?", (job_id,)).fetchone()
        if sizes and any(size is None or size > limit for size, (_, limit) in zip(sizes, limits)):
            raise ContractError("extraction cache row/JSON byte bound exceeded")
        return self.store.connection.execute("SELECT * FROM extraction_cache WHERE job_id=?", (job_id,)).fetchone()

    @_fail_closed()
    def remember(self, entry, bundle):
        """Seal the actual raw entry after publication. No new business DB."""
        if not self._supported(entry):
            return False
        raw = entry.get("raw")
        self._raw(raw)
        if str(raw["media_type"]).startswith(("audio/", "video/")):
            return False
        with self.store.transaction():
            scope, original, job = self._scope(entry, entry["job_id"], entry.get("config_version", "default"), complete=True)
            if bundle.get("project_id") != scope["project_id"]:
                raise ContractError("extraction cache bundle project mismatch")
            checked, artifacts = self._bundle(scope, bundle["bundle_id"], bundle["publish_id"], original)
            if canonical(bundle) != canonical(checked):
                raise ContractError("extraction cache requires the actual published bundle")
            # Never treat a whole published directory (manifest/plan/etc.) as raw.
            # Bound before Store._validate_raw hashes any raw output bytes.
            bindings = self._bindings(entry, artifacts)
            self.store._validate_raw(entry)
            self._receipt(scope, job, original, known=checked)
            self._projection(raw, scope, original, checked, artifacts, bindings)
            old = self._row(scope["job_id"])
            if old:
                previous = self._cached(old, scope, original, job)
                if previous["raw"] != raw or [(f["key"], f["relative_path"], self._signature(f)) for f in previous["files"]] != [(f["key"], f["relative_path"], self._signature(f)) for f in bindings]:
                    raise ContractError("published job already has different immutable cached raw")
                return True
            payload = {"version": _VERSION, "scope": scope, "original": original,
                       "bundle_id": checked["bundle_id"], "publish_id": checked["publish_id"],
                       "raw": raw, "files": bindings}
            data = self._encode(payload)
            total = self.store.connection.execute("SELECT COALESCE(SUM(length(CAST(payload_json AS BLOB))+length(CAST(provider_json AS BLOB))),0) FROM extraction_cache").fetchone()[0]
            provider = self._encode(scope["provider"])
            if total + len(data) + len(provider) > self.max_cache_bytes:
                raise ContractError("extraction cache total JSON byte bound exceeded")
            self.store.connection.execute("""INSERT INTO extraction_cache(
                job_id,project_id,asset_id,asset_revision,config_version,provider_json,bundle_id,publish_id,
                cache_version,access_class,payload_json,payload_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (scope["job_id"], scope["project_id"], scope["asset_id"], scope["asset_revision"],
                 scope["config_version"], provider.decode("utf-8"), checked["bundle_id"], checked["publish_id"],
                 _VERSION, "restricted_local", data.decode("utf-8"), hashlib.sha256(data).hexdigest(), now()))
            return True

    @_fail_closed()
    def restore(self, entry, job, config_version):
        """Return base + exact raw/output_dir/job_id/config_version, or None.

        All scope/state/get_bundle/hash checks precede copying. Each hit gets a
        fresh dedicated raw-only outdir, never the immutable evidence directory.
        An interrupted/failed copy raises and may leave its owned attempt for
        inspection; it cannot be mistaken for a successful restore or cache miss.
        """
        if not self._supported(entry):
            return None
        if not isinstance(job, dict):
            raise ContractError("extraction cache requires an explicit job record")
        with self.store.transaction():
            scope, original, current = self._scope(entry, job.get("job_id"), config_version, supplied=job)
            row = self._row(scope["job_id"])
            if not row:
                if current["state"] == "verified_complete":
                    self._receipt(scope, current, original)  # explicit legacy-only reparse, not a repair
                return None
            payload = self._cached(row, scope, original, current)
            outdir = Path(self.store.root) / "下载暂存" / scope["job_id"] / ("cache-" + uuid.uuid4().hex)
            reject_links(outdir)
            outdir.mkdir(parents=True, exist_ok=False)
            reject_links(outdir)
            root = self.store.project_root(scope["project_id"])
            for binding in payload["files"]:
                source = safe_path(root, binding["artifact_relative_path"])
                target = safe_path(outdir, binding["relative_path"], must_exist=False)
                target.parent.mkdir(parents=True, exist_ok=True)
                reject_links(target)
                sha, size = hashlib.sha256(), 0
                with source.open("rb") as src, os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600), "wb") as dst:
                    if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                        raise ContractError("extraction cache source is not a regular file")
                    while True:
                        chunk = src.read(min(65536, binding["byte_length"] - size + 1))
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > binding["byte_length"]:
                            raise ContractError("extraction cache artifact grew during restore")
                        sha.update(chunk)
                        dst.write(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
                reject_links(source)
                reject_links(target)
                if (sha.hexdigest(), size) != (binding["sha256"], binding["byte_length"]):
                    raise ContractError("extraction cache artifact changed during restore")
                if hash_file(target) != (binding["sha256"], binding["byte_length"]):
                    raise ContractError("extraction cache restored file hash mismatch")
                if source.stat().st_size != binding["byte_length"] or hash_file(source) != (binding["sha256"], binding["byte_length"]):
                    raise ContractError("extraction cache published file changed during restore")
            restored = {**deepcopy(entry), "raw": payload["raw"], "output_dir": outdir,
                        "job_id": scope["job_id"], "config_version": config_version}
            self.store._validate_raw(restored)
            self._base(restored, scope["project_id"])
            return restored
