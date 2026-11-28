"""The operator-facing view of existing verification holds."""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from hashlib import sha256
import json


PLATFORMS = {"platform:baidu": "百度网盘", "platform:wangzjk": "网坠金库", "site": "网坠金库"}
KNOWN_REASONS = {
    "Human verification required; stop this platform",
    "V4 share shows real verification; no save",
    "V4 real challenge before claim",
    "V4 actual verification before tap",
    "manual_verification_required",
}


def timestamp(value):
    if not isinstance(value, str) or len(value) > 50:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def verification_blocks(checkpoint):
    blocks = checkpoint["blocks"]
    if not isinstance(blocks, dict):
        raise ValueError("Invalid verification checkpoint")
    for scope in PLATFORMS:
        block = blocks.get(scope)
        if block is None:
            continue
        if not isinstance(block, dict) or block.get("scope") != scope or type(block.get("resolved")) is not bool:
            raise ValueError("Invalid verification hold")
        reason = block.get("reason")
        if block["resolved"] or block.get("phase") != "needs_human" or not isinstance(reason, str):
            continue
        if reason.startswith("Pan response "):
            try:
                result = json.loads(reason.removeprefix("Pan response "))
            except ValueError:
                continue
            if isinstance(result, dict) and result.get("message") == "verification_required":
                yield block
        elif reason in KNOWN_REASONS:
            yield block


def hold_id(block):
    value = json.dumps(block, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(value.encode("utf-8")).hexdigest()


def display_holds(checkpoint):
    return [
        {"id": hold_id(block), "label": PLATFORMS[block["scope"]],
         "message": "转存已暂停，等待验证。" if block["scope"] == "platform:baidu" else "资料读取已暂停，等待验证。",
         "observed_at": block["recorded_at"] if timestamp(block.get("recorded_at")) else None}
        for block in verification_blocks(checkpoint)
    ]


def read_status(crawl, public):
    holds, available = [], True
    for path, reader in ((crawl, _crawl_holds), (public, _public_holds)):
        try:
            if path.is_symlink() or path.resolve(strict=True) != path:
                raise ValueError("Unbound verification status")
            stat = path.stat()
            if stat.st_size > 64 * 1024 * 1024:
                raise ValueError("Verification status exceeds its size limit")
            holds.extend(reader(path, stat.st_mtime_ns, stat.st_size, stat.st_ino))
        except (OSError, ValueError, KeyError, TypeError):
            available = False
    return {"available": available, "holds": holds}


@lru_cache(maxsize=2)
def _crawl_holds(path, _modified, _size, _identity):
    checkpoint = json.loads(path.read_bytes())
    if not isinstance(checkpoint, dict) or checkpoint.get("schema") != "wangzjk-to-pan.crawl.v1":
        raise ValueError("Unknown verification checkpoint")
    return display_holds(checkpoint)


@lru_cache(maxsize=2)
def _public_holds(path, _modified, _size, _identity):
    checkpoint = json.loads(path.read_bytes())
    state = checkpoint["collection"]
    if not isinstance(state, dict) or type(checkpoint.get("official_site_hold")) is not bool:
        raise ValueError("Unknown public collection checkpoint")
    if checkpoint.get("official_site_hold") is not True or state.get("stop_reason") != "publisher_verification_required":
        return []
    observed = state.get("observed_at")
    at = observed if timestamp(observed) else None
    return [{"id": hold_id({"scope": "public_catalog", "observed_at": at}), "label": "网坠金库",
             "message": "公开采集已暂停，等待验证。", "observed_at": at}]
