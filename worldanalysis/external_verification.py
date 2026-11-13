"""Opt-in, bounded public verification reads; no HTTP/browser implementation.

Host-only capability (never a Model/Plan string): ``reader(url, *, max_bytes)``
must perform ONE credential-free public HTTPS GET, with no request body, cookies,
auth, API keys, ambient proxy credentials or redirects. Before connecting it must
resolve/pin only globally routable addresses (also on reconnect), and enforce
``max_bytes`` while streaming, BEFORE accumulating/decoding the response body.
It returns exactly final_url/status/text/bytes_received/read_complete/reader_id/
sha256/title. Text is the COMPLETE strict UTF-8 response representation, without
extraction, normalization or truncation; sha256 and bytes_received describe those
same bytes. Unknown byte counts are not successful reads. The trusted adapter is
the network boundary; a Python callable is not sandboxed by this module.

Only an explicit callable AND exact-host allowlist permit a call. No query/search
URLs, fragments or redirects are supported: the URL itself must not upload course
material. No task, source material, local path or store is passed to the adapter.
Successful text lives in existing read_receipts.content, not a new DB or file.
Local idempotent replay verifies that sealed content, not remote freshness; use a
new source_id for a new observation. A read never verifies a claim or completes a
task. No token/cost/usage values are invented.

Minimal Analysis._validate_result hook inside the external read_status=read arm::

    from .external_verification import validate_receipt
    validate_receipt(self, task, source, bundle=bundle)

The host/CLI explicitly calls ``read(analysis, task_id, source_id, url,
reader=trusted_reader, allowed_hosts=("docs.example.org",))``. Do not derive either
capability from submitted model output. No monkeypatch/integration is installed
on import.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import ipaddress
import re
from urllib.parse import unquote, urlsplit

from .contracts import ContractError, canonical, check_id, digest, now, public_url, scan_secrets

MAX_BYTES = 262144
_MAX_URL = 2048
_MAX_RECEIPT = 16384
_POLICY = "public-external-complete-utf8-v1"
_RESPONSE_KEYS = {"final_url", "status", "text", "bytes_received", "read_complete",
                  "reader_id", "sha256", "title"}
_SOURCE_KEYS = {"source_id", "title", "source_type", "public_url", "retrieved_at",
                "read_status", "receipt_ref", "snapshot_relative_path"}
_EVIDENCE_KEYS = {"policy", "source_id", "project_id", "requested_url", "public_url",
                  "title", "source_type", "http_status", "bytes_received",
                  "read_complete", "reader_id", "sha256", "allowed_hosts", "retrieved_at"}
_MANAGED_KEYS = {"receipt_id", "task_id", "block_id", "kind", "publish_id",
                 "created_at", "content_sha256", "seal_sha256", "content"}
_SECRET = re.compile(
    r"(?i)(?:\b(?:cookie|set-cookie|authorization|access_token|refresh_token|token|"
    r"client_secret|session_id|api[_-]?key|secret[_-]?key|private_key|password|passwd|"
    r"BDUSS|STOKEN|BAIDUID)[\"']?\s*[:=]\s*\S+|\bbearer\s+\S+|"
    r"\bsk-[A-Za-z0-9_-]{12,}|\bAKIA[A-Z0-9]{16}\b|"
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"(?:密码|口令|提取码|访问令牌|密钥)\s*[:：=]\s*\S+)"
)
_LINK = re.compile(r"https?://[^\s<>\"']+", re.I)
_PRIVATE_PATH = re.compile(
    r"(?i)(?:^|/)(?:auth|authorization|cookie|credential|download|login|password|"
    r"private|secret|session|sign|signed|signature|token)(?:/|[=;:]|$)"
)


def _host(value):
    if type(value) is not str or not value or len(value) > 253 or not value.isascii():
        raise ContractError("external host must be an exact ASCII hostname or public IP")
    value = value.lower()
    if any(c in value for c in "%/@\\?#") or value.endswith("."):
        raise ContractError("ambiguous external host")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        labels = value.split(".")
        if (len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in labels)
                or all(re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", p) for p in labels)
                or value.endswith((".localhost", ".local", ".internal", ".lan", ".home"))):
            raise ContractError("external host is not a public DNS name")
        return value
    if (not address.is_global or address.is_multicast or address.is_reserved
            or getattr(address, "ipv4_mapped", None) is not None):
        raise ContractError("external IP must be globally routable unicast")
    return address.compressed


def _hosts(values):
    if type(values) not in (tuple, list) or not 1 <= len(values) <= 32:
        raise ContractError("external allowlist must contain 1..32 exact hosts")
    hosts = tuple(sorted({_host(value) for value in values}))
    if len(hosts) != len(values):
        raise ContractError("duplicate external allowlist host")
    return hosts


def _url(value, hosts):
    if (type(value) is not str or not 1 <= len(value) <= _MAX_URL or not value.isascii()
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
            or "\\" in value or "#" in value or "?" in value):
        raise ContractError("external URL must be bounded HTTPS with no query/fragment")
    try:
        parsed = urlsplit(value)
        if (not value.startswith("https://") or parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)):
            raise ContractError("external URL must be credential-free HTTPS on port 443")
        host = _host(parsed.hostname)
        if host not in hosts:
            raise ContractError("external host is not explicitly allowlisted")
        decoded = value
        for _ in range(3):
            public_url(decoded)
            if _SECRET.search(decoded) or _PRIVATE_PATH.search(urlsplit(decoded).path):
                raise ContractError("private/signed external URL")
            next_value = unquote(decoded, errors="strict")
            if next_value == decoded:
                break
            decoded = next_value
            if any(c.isspace() or ord(c) < 32 for c in decoded) or any(c in decoded for c in "?#\\"):
                raise ContractError("encoded private/ambiguous external URL")
        if "%" in decoded:
            raise ContractError("multiply encoded external URL")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ContractError("invalid public external URL") from exc
    return value


def _bounded_text(text, maximum=MAX_BYTES):
    # Check before encode/hash/JSON/save allocations. The host streaming boundary
    # must already have bounded its original response allocation.
    if type(text) is not str or not text or len(text) > maximum:
        raise ContractError("external text is empty or exceeds hard byte bound")
    size = 0
    for char in text:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise ContractError("external text is not strict UTF-8")
        size += 1 if code < 0x80 else 2 if code < 0x800 else 3 if code < 0x10000 else 4
        if size > maximum:
            raise ContractError("external UTF-8 byte bound exceeded")
    return text.encode("utf-8")


def _sensitive(text):
    scan_secrets(text)
    if _SECRET.search(text):
        raise ContractError("sensitive external content; no read receipt")
    for match in _LINK.finditer(text):
        value = match.group().rstrip(".,;)")
        for _ in range(3):
            public_url(value)
            parsed = urlsplit(value)
            _host(parsed.hostname)
            if (_SECRET.search(value) or _PRIVATE_PATH.search(parsed.path)
                    or parsed.fragment or any(c in parsed.netloc for c in "\\%")):
                raise ContractError("private/signed link in external content")
            try:
                decoded = unquote(value, errors="strict")
            except UnicodeError:
                raise ContractError("invalid encoded external content link") from None
            if decoded == value:
                break
            value = decoded


def _response(response, url, hosts):
    if type(response) is not dict or response.keys() != _RESPONSE_KEYS:
        raise ContractError("reader must return the exact bounded response contract")
    if type(response["status"]) is not int or response["status"] != 200:
        raise ContractError("external GET failed; no read receipt and no retry")
    if response["read_complete"] is not True:
        raise ContractError("external response incomplete/truncated; no read receipt")
    final_url = _url(response["final_url"], hosts)
    if final_url != url:
        raise ContractError("external redirects are forbidden; no read receipt")
    count = response["bytes_received"]
    if type(count) is not int or not 0 < count <= MAX_BYTES:
        raise ContractError("external byte count must be known, positive and bounded")
    body = _bounded_text(response["text"])
    sha = hashlib.sha256(body).hexdigest()
    if count != len(body) or type(response["sha256"]) is not str or response["sha256"] != sha:
        raise ContractError("external response byte count/digest mismatch")
    check_id(response["reader_id"])
    title = response["title"]
    if type(title) is not str or not title.strip() or len(title) > 1000:
        raise ContractError("external reader must provide a bounded nonempty title")
    _bounded_text(title, 4000)
    _sensitive(title)
    _sensitive(response["text"])
    return sha


def _slot(source_id):
    return "external:" + source_id


def _source(receipt):
    return {"source_id": receipt["source_id"], "title": receipt["title"],
            "source_type": receipt["source_type"], "public_url": receipt["public_url"],
            "retrieved_at": receipt["retrieved_at"], "read_status": "read",
            "receipt_ref": receipt["receipt_id"], "snapshot_relative_path": None}


def _load(analysis, task, ident, bundle):
    check_id(ident)
    row = analysis.connection.execute(
        "SELECT receipt_id,task_id,block_id,kind,length(CAST(receipt_json AS BLOB)) AS metadata_bytes,"
        "length(CAST(content AS BLOB)) AS content_bytes,created_at FROM read_receipts WHERE receipt_id=?",
        (ident,)).fetchone()
    if (not row or row["task_id"] != task["task_id"] or row["kind"] != "external"
            or row["content_bytes"] is None or not 0 < row["content_bytes"] <= MAX_BYTES
            or not 0 < row["metadata_bytes"] <= _MAX_RECEIPT):
        raise ContractError("external receipt missing, out of scope, or outside byte bounds")
    try:
        receipt = analysis.receipt(ident, _bundle=bundle)
        if type(receipt) is not dict or receipt.keys() != _EVIDENCE_KEYS | _MANAGED_KEYS:
            raise ContractError("invalid external receipt fields")
        source_id = check_id(receipt["source_id"])
        if (receipt["receipt_id"] != ident or receipt["task_id"] != task["task_id"]
                or receipt["publish_id"] != task["publish_id"]
                or receipt["project_id"] != task["project_id"]
                or receipt["block_id"] != _slot(source_id) or row["block_id"] != _slot(source_id)
                or receipt["kind"] != "external" or receipt["policy"] != _POLICY
                or receipt["source_type"] != "other_public_source"):
            raise ContractError("external receipt source/scope mismatch")
        stamps = []
        for stamp in (receipt["retrieved_at"], receipt["created_at"], row["created_at"]):
            if type(stamp) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", stamp):
                raise ContractError("invalid external read timestamp")
            stamps.append(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
        if not stamps[0] <= stamps[1] <= stamps[2] or (stamps[2] - stamps[0]).total_seconds() > 300:
            raise ContractError("external receipt timestamps do not match the sealed read")
        hosts = _hosts(receipt["allowed_hosts"])
        if list(hosts) != receipt["allowed_hosts"]:
            raise ContractError("noncanonical external host policy")
        url = _url(receipt["requested_url"], hosts)
        response = {"final_url": receipt["public_url"], "status": receipt["http_status"],
                    "text": receipt["content"], "bytes_received": receipt["bytes_received"],
                    "read_complete": receipt["read_complete"], "reader_id": receipt["reader_id"],
                    "sha256": receipt["sha256"], "title": receipt["title"]}
        sha = _response(response, url, hosts)
        if receipt["content_sha256"] != sha or receipt["seal_sha256"] != digest({k: receipt[k] for k in _EVIDENCE_KEYS}):
            raise ContractError("external sealed metadata/content hash mismatch")
        return receipt
    except ContractError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
        raise ContractError("invalid external receipt; no read accepted") from exc


def validate_receipt(analysis, task, source, *, bundle=None):
    """Return the persisted receipt after validating a submitted source, offline.

    Call from Analysis._validate_result for every external read source. This
    strengthens its legacy task/kind/URL check with complete content and source
    identity validation. It does not change assertion verification statuses.
    """
    if type(source) is not dict or source.keys() not in (_SOURCE_KEYS, _SOURCE_KEYS | {"published_at"}):
        raise ContractError("invalid external source fields")
    if source.get("published_at") is not None or source["read_status"] != "read":
        raise ContractError("external source is not a supported complete read")
    bundle = analysis._bundle(task) if bundle is None else bundle
    receipt = _load(analysis, task, source["receipt_ref"], bundle)
    if {k: source[k] for k in _SOURCE_KEYS} != _source(receipt):
        raise ContractError("external source id/title/URL/time/ref does not match receipt")
    return receipt


def _existing(analysis, task, source_id, bundle):
    if analysis.connection.execute("SELECT 1 FROM sources WHERE source_id=?", (source_id,)).fetchone():
        raise ContractError("external source_id conflicts with a local source")
    rows = analysis.connection.execute(
        "SELECT receipt_id FROM read_receipts WHERE block_id=? LIMIT 2", (_slot(source_id),)).fetchall()
    if len(rows) > 1:
        raise ContractError("external source_id has conflicting sealed receipts")
    return _load(analysis, task, rows[0]["receipt_id"], bundle) if rows else None


def read(analysis, task_id, source_id, url, *, reader=None, allowed_hosts=()):
    """Read one public source via an explicitly configured trusted host adapter.

    Missing capabilities/paused pipeline return blocked with no adapter call.
    Invalid URLs, failures and invalid acknowledgements raise ContractError once,
    without retry or receipt. Same source ID is immutable and global to the store:
    only its original task/project/publication + exact URL + verified sealed local
    content can replay. It is never a request to refresh that URL remotely.
    """
    if not callable(reader) or not allowed_hosts:
        return {"status": "blocked", "reason": "external_reader_and_explicit_allowlist_required"}
    if analysis.store.pipeline_state("analysis") != "running":
        return {"status": "blocked", "reason": "analysis paused"}
    hosts = _hosts(allowed_hosts)
    url = _url(url, hosts)
    check_id(source_id)
    task = analysis._task(task_id, require_running=True)
    bundle = analysis._bundle(task)
    # Short transaction around lookup/replay; never hold a DB lock over network IO.
    with analysis.store.transaction():
        previous = _existing(analysis, task, source_id, bundle)
        if previous:
            if previous["requested_url"] != url:
                raise ContractError("external source_id is sealed to a different URL")
            return {"status": "read", "idempotent": True, "text": previous["content"],
                    "external_source": _source(previous),
                    "receipt": {k: v for k, v in previous.items() if k != "content"}}
    try:
        response = reader(url, max_bytes=MAX_BYTES)
    except Exception:
        # Do not leak adapter exception messages (possibly credential-bearing).
        raise ContractError("external reader failed; no read receipt and no retry") from None
    sha = _response(response, url, hosts)
    evidence = {"policy": _POLICY, "source_id": source_id, "project_id": task["project_id"],
                "requested_url": url, "public_url": response["final_url"],
                "title": response["title"], "source_type": "other_public_source",
                "http_status": response["status"], "bytes_received": response["bytes_received"],
                "read_complete": True, "reader_id": response["reader_id"], "sha256": sha,
                "allowed_hosts": list(hosts), "retrieved_at": now()}
    evidence["seal_sha256"] = digest(evidence)
    if len(canonical(evidence)) > _MAX_RECEIPT - 2048:
        raise ContractError("external metadata exceeds hard byte bound")
    with analysis.store.transaction():
        current = analysis._task(task_id, require_running=True)
        if analysis.store.pipeline_state("analysis") != "running":
            return {"status": "blocked", "reason": "analysis paused during external read"}
        if current["publish_id"] != task["publish_id"] or current["project_id"] != task["project_id"]:
            raise ContractError("external task scope changed during read")
        analysis._bundle(current)
        previous = _existing(analysis, current, source_id, bundle)
        if previous:
            if (previous["requested_url"] != url or previous["content"] != response["text"]
                    or previous["sha256"] != sha or previous["title"] != response["title"]):
                raise ContractError("external source_id conflicts with sealed content")
            return {"status": "read", "idempotent": True, "text": previous["content"],
                    "external_source": _source(previous),
                    "receipt": {k: v for k, v in previous.items() if k != "content"}}
        receipt = analysis._save_receipt(current, _slot(source_id), "external", evidence, response["text"])
        # Validate through the same offline hook BEFORE the enclosing commit.
        source = _source(receipt)
        validate_receipt(analysis, current, source, bundle=bundle)
    return {"status": "read", "idempotent": False, "text": response["text"],
            "external_source": source, "receipt": receipt}
