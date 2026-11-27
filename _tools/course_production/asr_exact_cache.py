"""Content-addressed reuse of automatically recognized, identical PCM only."""

import hashlib
import json
import math

from project import put, read


SCHEMA = 1


def digest(value):
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def pcm_digest(chunks):
    result = hashlib.sha256()
    for chunk in chunks:
        result.update(chunk)
    return result.hexdigest()


def valid_part(part, window, duration):
    if not isinstance(part, dict) or part.get("window") != window or not isinstance(part.get("cues"), list):
        return False
    start = window["timeline_start"]
    end = min(duration, start + window["samples"] / window["sample_rate"])
    for row in part["cues"]:
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            return False
        left, right = row.get("start"), row.get("end")
        if (type(left) not in (float, int) or type(right) not in (float, int)
                or not math.isfinite(left) or not math.isfinite(right)
                or not max(0, start - .01) <= left < right <= end + .01):
            return False
    return True


def read_record(path, identity, duration):
    """A malformed or changed cache is a miss; the original can be recognized afresh."""
    try:
        record = read(path)
        if (not isinstance(record, dict) or record.get("schema") != SCHEMA
                or record.get("identity") != identity
                or record.get("payload_sha256") != digest(record.get("part"))
                or not valid_part(record.get("part"), identity["window"], duration)):
            return None
        return record["part"]
    except (OSError, ValueError, TypeError):
        return None


def write_record(path, identity, part, duration):
    if valid_part(part, identity["window"], duration):
        put(path, {"schema": SCHEMA, "identity": identity, "part": part,
                   "payload_sha256": digest(part)})
