"""Offline draft contracts and fail-closed reconciliation of real evidence."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import parse_qsl, urlsplit

VALIDATOR_VERSION = "worldanalysis-contracts-1"
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "worldanalysis_agent_devpack_20261110" / "contracts"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_SECRET = re.compile(r"(?i)(?:\b(?:cookie|set-cookie|authorization)\s*[:=]\s*\S+|\bbearer\s+[A-Za-z0-9._~+/=-]{8,}|\b(?:access_token|refresh_token|token|client_secret|session_id|api[_-]?key|secret[_-]?key|private_key|password|passwd|BDUSS|STOKEN|BAIDUID)\s*[:=]\s*[^\s,;]+|\b(?:sk-[A-Za-z0-9_-]{12,}|AKIA[A-Z0-9]{16})\b)")
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_SENSITIVE_QUERY = re.compile(r"(?i)(token|secret|sign(?:ature)?|credential|auth|cookie|expires|password|key|ticket|session|download)")


class ContractError(ValueError):
    """Evidence is untrusted or inconsistent, not successful partial output."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def check_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ContractError("invalid opaque ID")
    return value


def validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2000:
        raise ContractError("invalid relative path")
    if any(ord(c) < 32 for c in value) or "\\" in value or ":" in value:
        raise ContractError("absolute/Windows/control path is forbidden")
    if value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/")):
        raise ContractError("path traversal is forbidden")
    if any(p.endswith((".", " ")) or p.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]} for p in value.split("/")):
        raise ContractError("ambiguous Windows path")
    return value


def reject_links(path: Path) -> None:
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ContractError("symbolic links/junctions/reparse points are forbidden")


def safe_path(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    validate_relative_path(relative)
    root = Path(root)
    reject_links(root)
    path = root.joinpath(*PurePosixPath(relative).parts)
    reject_links(path)
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=must_exist)
    try:
        if os.path.normcase(os.path.commonpath((str(resolved_root), str(resolved)))) != os.path.normcase(str(resolved_root)):
            raise ContractError("path escaped project root")
    except ValueError as exc:
        raise ContractError("path escaped project volume") from exc
    if must_exist and not path.is_file():
        raise ContractError("expected a regular file")
    return path


def hash_file(path: Path) -> tuple[str, int]:
    reject_links(path)
    h = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ContractError("not a regular file")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def public_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ContractError("not a credential-free public URL")
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if any(_SENSITIVE_QUERY.search(k) for k, _ in query):
            raise ContractError("signed/private URL is forbidden")
        if any(k.lower() not in {"v", "version", "lang", "hl", "page", "p", "id", "q"} for k, _ in query):
            raise ContractError("URL query not approved as public")
        if re.search(r"(?i)/(?:token|secret|auth|download|signed)/[^/]+", parsed.path) or _SECRET.search(url):
            raise ContractError("private URL path is forbidden")
    except (ValueError, TypeError) as exc:
        raise ContractError("invalid public URL") from exc


def contains_sensitive(text: str) -> bool:
    if _SECRET.search(text) or re.search(r'''(?i)(?:["'](?:cookie|set-cookie|authorization|access_token|refresh_token|token|client_secret|session_id|api[_-]?key|secret[_-]?key|private_key|password|BDUSS|STOKEN)["']\s*:\s*[^\s,;}]+|-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|(?:密码|口令|提取码|访问令牌|密钥)\s*[:：=]\s*\S+)''',text):
        return True
    for url in _URL.findall(text):
        try:
            public_url(url.rstrip(".,;)"))
        except ContractError:
            return True
    return False


def scan_secrets(value) -> None:
    if isinstance(value, str):
        if contains_sensitive(value):
            raise ContractError("credential/signed URL detected; retain only in restricted_local evidence")
    elif isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower().replace("-","_") in {"cookie","set_cookie","authorization","access_token","refresh_token","token","client_secret","session_id","api_key","secret_key","private_key","password","bduss","stoken"} and child:
                raise ContractError("credential field detected")
            scan_secrets(str(key))
            scan_secrets(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            scan_secrets(child)


def redact_text(text: str) -> tuple[str, bool]:
    restricted = contains_sensitive(text)
    return ("\n".join("[restricted_local: credential/private URL withheld]" if contains_sensitive(line) else line for line in text.splitlines()), True) if restricted else (text, False)


@lru_cache(maxsize=16)
def _schema_validator(kind: str, schema_dir: str):
    if kind not in {"plan", "bundle", "analysis"}:
        raise ContractError("unknown schema kind")
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry, Resource
        from referencing.exceptions import NoSuchResource
    except ImportError as exc:
        raise ContractError("jsonschema 4.25.1 is required; no online or permissive fallback") from exc
    base = Path(schema_dir)
    schemas = {name: json.loads((base / f"{name}.schema.json").read_text(encoding="utf-8")) for name in ("bundle", "plan", "analysis")}
    def offline(uri):
        raise NoSuchResource(ref=uri)
    registry = Registry(retrieve=offline)
    for name, schema in schemas.items():
        Draft202012Validator.check_schema(schema)
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(schema["$id"], resource)
        registry = registry.with_resource((base / f"{name}.schema.json").resolve().as_uri(), resource)
    checker = FormatChecker()
    @checker.checks("date-time")
    def rfc3339_with_zone(value):
        if not isinstance(value,str):
            return True
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})",value):
            return False
        try:
            return datetime.fromisoformat(value.upper().replace("Z","+00:00")).utcoffset() is not None
        except ValueError:
            return False
    return Draft202012Validator(schemas[kind], registry=registry, format_checker=checker)


def validate_schema(kind: str, document: dict, schema_dir: Path | None = None) -> None:
    scan_secrets(document)
    validator = _schema_validator(kind,str(Path(schema_dir or SCHEMA_DIR).resolve()))
    try:
        errors = sorted(validator.iter_errors(document), key=lambda e: str(list(e.absolute_path)))
    except Exception as exc:
        raise ContractError("schema reference cannot be resolved by the offline registry") from exc
    if errors:
        err = errors[0]
        raise ContractError(f"{kind} schema {'.'.join(map(str, err.absolute_path))}: {err.message}")
    scan_secrets(document)


def unique(items, key, label: str) -> dict:
    result = {}
    for item in items:
        ident = key(item) if callable(key) else item[key]
        if ident in result:
            raise ContractError(f"duplicate {label}")
        result[ident] = item
    return result


def asset_key(value: dict) -> tuple[str, str]:
    return value["asset_id"], value["asset_revision"]


def interval_union(ranges) -> int:
    intervals = sorted((int(a), int(b)) for a, b in ranges)
    total = 0
    left = right = None
    for a, b in intervals:
        if a < 0 or b < a:
            raise ContractError("invalid interval")
        if left is None:
            left, right = a, b
        elif a <= right:
            right = max(right, b)
        else:
            total += right - left
            left, right = a, b
    return total + (0 if left is None else right - left)


def acyclic(nodes: dict, edge_key: str, label: str) -> None:
    visiting, visited = set(), set()
    def visit(ident):
        if ident not in nodes:
            raise ContractError(f"missing {label} dependency")
        if ident in visiting:
            raise ContractError(f"cyclic {label}")
        if ident in visited:
            return
        visiting.add(ident)
        for dep in nodes[ident].get(edge_key, []):
            visit(dep)
        visiting.remove(ident)
        visited.add(ident)
    for ident in nodes:
        visit(ident)


def validate_locator(locator: dict, artifacts: dict | None = None) -> None:
    for a, b in (("start_ms", "end_ms"), ("char_start", "char_end")):
        if a in locator and b in locator and locator[b] < locator[a]:
            raise ContractError("reversed locator interval")
    if "geometry" not in locator:
        return
    g = locator["geometry"]
    if artifacts is not None and g["reference_object_ref"] not in artifacts:
        raise ContractError("geometry reference is not a real artifact")
    w, h = g["frame_size"]["width"], g["frame_size"]["height"]
    x, y, bw, bh = g["bbox_xywh"]
    if not all(math.isfinite(float(v)) for v in (w, h, x, y, bw, bh)):
        raise ContractError("non-finite geometry")
    if g["unit"] == "normalized" and (w != 1 or h != 1):
        raise ContractError("normalized frame must be 1x1")
    if x < 0 or y < 0 or x + bw > w + 1e-6 or y + bh > h + 1e-6:
        raise ContractError("bbox outside reference frame")
    t = g["transform_to_original"]
    ow, oh = g["original_size"]["width"], g["original_size"]["height"]
    if t["method"] == "identity":
        if t["operations"] or g["coordinate_space"] != "original" or g["rotation_degrees"] != 0 or g["origin"] != "top_left":
            raise ContractError("nonidentity transform labeled identity")
        if g["unit"] != g["original_size"]["unit"] or abs(w - ow) > 1e-6 or abs(h - oh) > 1e-6:
            raise ContractError("identity frame/units disagree")
    elif t["method"] == "explicit_mapping":
        if artifacts is not None and t["mapping_artifact_id"] not in artifacts:
            raise ContractError("missing mapping artifact")
        raise ContractError("explicit mapping requires a registered geometry verifier")
    else:
        m = t["matrix"]
        if not all(math.isfinite(float(v)) for v in m):
            raise ContractError("non-finite transform")
        det = m[0]*(m[4]*m[8]-m[5]*m[7])-m[1]*(m[3]*m[8]-m[5]*m[6])+m[2]*(m[3]*m[7]-m[4]*m[6])
        if abs(det) < 1e-12:
            raise ContractError("singular coordinate transform")
        for px, py in ((x,y),(x+bw,y),(x,y+bh),(x+bw,y+bh)):
            d = m[6]*px + m[7]*py + m[8]
            if abs(d) < 1e-12:
                raise ContractError("invalid homogeneous transform")
            tx, ty = (m[0]*px+m[1]*py+m[2])/d, (m[3]*px+m[4]*py+m[5])/d
            if tx < -1e-5 or ty < -1e-5 or tx > ow+1e-5 or ty > oh+1e-5:
                raise ContractError("mapped bbox outside original")


def validate_evidence(ref: dict, bundle: dict, *, allow_source_only: bool = True) -> None:
    if ref["project_id"] != bundle["project_id"]:
        raise ContractError("cross-project evidence")
    if ref.get("bundle_id", bundle["bundle_id"]) != bundle["bundle_id"] or ref.get("bundle_revision", bundle["bundle_revision"]) != bundle["bundle_revision"]:
        raise ContractError("wrong bundle evidence version")
    if (ref["asset_id"], ref["asset_revision"]) not in {asset_key(a) for a in bundle["assets"]}:
        raise ContractError("unknown asset evidence revision")
    if "block_id" in ref:
        block = next((b for b in bundle["blocks"] if b["block_id"] == ref["block_id"]), None)
        if block is None or asset_key(block["asset_ref"]) != (ref["asset_id"], ref["asset_revision"]):
            raise ContractError("unknown/mismatched evidence block")
        if ref["locator"] != block["locator"]:
            raise ContractError("evidence locator does not match actual block")
    if "artifact_id" in ref:
        a = next((a for a in bundle["artifacts"] if a["artifact_id"] == ref["artifact_id"]), None)
        if a is None or asset_key(a["asset_ref"]) != (ref["asset_id"], ref["asset_revision"]):
            raise ContractError("unknown/mismatched evidence artifact")
    if not allow_source_only and not ("block_id" in ref or "artifact_id" in ref):
        raise ContractError("source-only locator is not read evidence")
    validate_locator(ref["locator"], {a["artifact_id"]: a for a in bundle["artifacts"]})


def validate_bundle(bundle: dict, project_root: Path, *, path_resolver=None) -> None:
    validate_schema("bundle", bundle)
    assets = unique(bundle["assets"], asset_key, "asset revision")
    artifacts = unique(bundle["artifacts"], "artifact_id", "artifact ID")
    blocks = unique(bundle["blocks"], "block_id", "block ID")
    gaps = unique(bundle["gaps"], "gap_id", "gap ID")
    unique(bundle["contacts"], "contact_id", "contact ID")
    unique(bundle["artifacts"], lambda a: a["relative_path"].casefold(), "artifact path")
    resolver = path_resolver or (lambda rel: safe_path(project_root, rel))
    image_sizes = {}
    for artifact in artifacts.values():
        if asset_key(artifact["asset_ref"]) not in assets:
            raise ContractError("artifact references unknown asset")
        validate_relative_path(artifact["relative_path"])
        file = resolver(artifact["relative_path"])
        sha, size = hash_file(file)
        if sha != artifact["sha256"] or size != artifact["byte_length"]:
            raise ContractError("artifact file hash/size mismatch")
        if artifact["kind"] in {"image","page_render","keyframe"}:
            try:
                from PIL import Image
                with Image.open(file) as image:
                    image_sizes[artifact["artifact_id"]] = image.size
                    actual_media = Image.MIME.get(image.format)
                    image.verify()
                if actual_media and actual_media != artifact["media_type"]:
                    raise ContractError("image actual media type mismatch")
            except ContractError:
                raise
            except Exception as exc:
                raise ContractError("image bytes are not decodable") from exc
        if artifact["access_class"] != "restricted_local" and (artifact["media_type"].startswith("text/") or artifact["media_type"] in {"application/json", "application/x-ndjson"}):
            try:
                scan_secrets(file.read_text(encoding="utf-8"))
            except UnicodeDecodeError as exc:
                raise ContractError("invalid text artifact") from exc
    for block in blocks.values():
        if asset_key(block["asset_ref"]) not in assets:
            raise ContractError("block references unknown asset")
        validate_locator(block["locator"], artifacts)
        if "geometry" in block["locator"]:
            g = block["locator"]["geometry"]
            if g["reference_object_ref"] not in image_sizes:
                raise ContractError("geometry reference must be a decodable image artifact")
            if g["unit"] == "pixel" and tuple(g["frame_size"][k] for k in ("width","height")) != image_sizes[g["reference_object_ref"]]:
                raise ContractError("geometry frame does not match actual image dimensions")
        for ident in block["artifact_ids"]:
            if ident not in artifacts or asset_key(artifacts[ident]["asset_ref"]) != asset_key(block["asset_ref"]):
                raise ContractError("block artifact missing or wrong asset")
        if block["kind"] in {"image_occurrence", "chart"} and block["processing"]["status"] == "extracted":
            if not any(artifacts[a]["kind"] in {"image", "page_render", "keyframe"} for a in block["artifact_ids"]):
                raise ContractError("image occurrence has no real image")
        for ref in block["derived_from"] + block["processing"]["evidence_refs"]:
            validate_evidence(ref, bundle)
            if ref.get("block_id") == block["block_id"]:
                raise ContractError("self-referential block provenance")
    provenance = {ident:{"dependencies":[r["block_id"] for r in b["derived_from"] if "block_id" in r]} for ident,b in blocks.items()}
    acyclic(provenance,"dependencies","block provenance")
    for gap in gaps.values():
        for ref in gap["target_refs"] + gap["resolution_refs"]:
            validate_evidence(ref, bundle)
    for contact in bundle["contacts"]:
        if contact["value_artifact_id"] not in artifacts:
            raise ContractError("contact original evidence missing")
        for ref in contact["evidence_refs"] + contact["classification_evidence_refs"]:
            validate_evidence(ref, bundle, allow_source_only=False)
    coverage = bundle["coverage"]
    visuals = sum(b["kind"] in {"image_occurrence", "chart"} for b in blocks.values())
    if coverage["discovered_counts"] != {"assets": len(assets), "blocks": len(blocks), "visual_occurrences": visuals}:
        raise ContractError("coverage discovered sets/counts disagree")
    per_asset = unique(coverage["per_asset"], lambda a: asset_key(a["asset_ref"]), "coverage asset")
    if set(per_asset) != set(assets):
        raise ContractError("per-asset coverage set mismatch")
    for key, entry in per_asset.items():
        own = [b for b in blocks.values() if asset_key(b["asset_ref"]) == key]
        for gid in entry["gap_ids"]:
            if gid not in gaps:
                raise ContractError("coverage references missing gap")
        for metric in entry["metrics"]:
            if metric["processed"] + metric["excluded"] > metric["observed"] or (metric["expected"] is not None and metric["observed"] > metric["expected"]):
                raise ContractError("invalid coverage count")
            for loc in metric["ranges"]:
                validate_locator(loc, artifacts)
            unit = metric["unit"]
            if unit == "millisecond":
                if any(r["kind"] != "time" for r in metric["ranges"]):
                    raise ContractError("time coverage contains non-time locator")
                union = interval_union((r["start_ms"], r["end_ms"]) for r in metric["ranges"])
                if metric["processed"] != union:
                    raise ContractError("time coverage must use interval union")
            elif unit in {"block", "visual_occurrence"}:
                actual = own if unit == "block" else [b for b in own if b["kind"] in {"image_occurrence", "chart"}]
                if metric["observed"] != len(actual) or metric["processed"] != sum(b["processing"]["status"] == "extracted" for b in actual) or metric["excluded"] != sum(b["processing"]["status"] == "excluded" for b in actual):
                    raise ContractError("block/visual metric entity set mismatch")
            elif metric["ranges"]:
                field = {"page":"page_number", "slide":"slide_number", "sheet":"sheet_name"}[unit]
                if any(field not in r for r in metric["ranges"]) or metric["processed"] != len({r[field] for r in metric["ranges"]}):
                    raise ContractError("coverage locator set/count mismatch")
        if entry["processing_status"] == "verified_complete":
            if entry["enumeration_state"] != "complete" or any(b["processing"]["status"] not in {"extracted", "not_applicable"} for b in own):
                raise ContractError("false complete asset coverage")
    if coverage["claimed_status"] == "verified_complete":
        if coverage["inventory_state"] != "complete" or any(e["processing_status"] != "verified_complete" for e in per_asset.values()) or any(g["status"] in {"open", "requested"} for g in gaps.values()):
            raise ContractError("false verified_complete bundle")
    for ident,block in blocks.items():
        if block["kind"] in {"image_occurrence","chart"}:
            associated = [g for g in gaps.values() if any(r.get("block_id") == ident for r in g["target_refs"])]
            if block["origin"] != "visual_observation" and not any(g["kind"] in {"semantic_gap","missing_visual","unprocessed","unsupported","unreadable"} and g["status"] in {"open","requested","accepted_limitation"} for g in associated):
                raise ContractError("uninterpreted image occurrence lacks an explicit gap")
    for gid,gap in gaps.items():
        owners = {asset_key(e["asset_ref"]) for e in per_asset.values() if gid in e["gap_ids"]}
        targets = {(r["asset_id"],r["asset_revision"]) for r in gap["target_refs"]}
        if not owners or not targets <= owners:
            raise ContractError("gap/per-asset coverage set mismatch")
    receipt = bundle["program_validation"]
    if receipt["status"] == "passed" and not resolver(receipt["report_relative_path"]).is_file():
        raise ContractError("validation receipt missing")


def validate_plan(plan: dict, *, registered_providers: set | None = None) -> None:
    validate_schema("plan", plan)
    observations = unique(plan["capability_observations"], "observation_id", "capability observation")
    permissions = unique(plan["permission_checks"], "check_id", "permission check")
    jobs = unique([j for a in plan["asset_plans"] for j in a["jobs"]], "job_id", "job ID")
    unique(plan["asset_plans"], lambda a: asset_key(a["asset_ref"]), "planned asset")
    for job in jobs.values():
        p = job["provider"]
        if registered_providers is not None and (p["provider_id"], p["implementation_version"]) not in registered_providers:
            raise ContractError("provider not registered")
        for key in job["capability_observation_ids"]:
            if key not in observations or observations[key]["provider"] != p:
                raise ContractError("capability/provider reference mismatch")
        for key in job["permission_check_ids"]:
            if key not in permissions or permissions[key]["operation"] != job["operation"]:
                raise ContractError("permission/operation reference mismatch")
        for key in job["fallback_job_ids"]:
            if key not in jobs:
                raise ContractError("unknown fallback job")
        for name, value in job["budget"].items():
            if name.startswith("max_") and value > plan["hard_budget"][name]:
                raise ContractError("job budget exceeds hard budget")
    acyclic(jobs, "depends_on_job_ids", "plan dependency")
    acyclic(jobs, "fallback_job_ids", "plan fallback")
