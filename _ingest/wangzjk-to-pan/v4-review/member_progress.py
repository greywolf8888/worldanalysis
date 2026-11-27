"""Checkpoint member page outcomes after reading the protected share catalog."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

from catalog_model import ART, digest, open_index, read_bound
from collect_public import collector_lock
from member_queue import (
    ACCESS_REQUIRED, DEFAULT_BACKFILL, DEFAULT_NON_PROJECTS, DEFAULT_OBSERVATIONS,
    MAX_LIMIT, MAX_OBSERVATIONS, OBSERVATION_SCHEMA, REVIEW_OUTCOMES, REVISION,
    article_refs, build_worklist, read_backfill,
    read_non_projects, read_observations,
)
from share_locations import SITE


BATCH_SCHEMA = "wa-v4-member-batch-evidence.v1"
REPORT_BYTES = 64 * 1024
OUTCOMES = {"linked", ACCESS_REQUIRED, *REVIEW_OUTCOMES}
SGT = timezone(timedelta(hours=8))


def validate_report(report: dict, *, advance: bool) -> list[dict]:
    if (not isinstance(report, dict) or report.get("schema") != BATCH_SCHEMA
            or report.keys() - {"schema", "before_id", "items"}):
        raise ValueError("Invalid member batch schema")
    cursor = report.get("before_id")
    if advance and "before_id" not in report:
        raise ValueError("Historical batch must provide its starting cursor")
    if cursor is not None and (not isinstance(cursor, str) or not ART.fullmatch(cursor)):
        raise ValueError("Invalid member batch starting cursor")
    items = report.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_LIMIT:
        raise ValueError("Invalid member batch size")
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"article_id", "article_revision", "outcome"}:
            raise ValueError("Invalid member batch row fields")
        aid, revision, outcome = (item[k] for k in ("article_id", "article_revision", "outcome"))
        if (not isinstance(aid, str) or not ART.fullmatch(aid) or aid in seen
                or not isinstance(revision, str) or not REVISION.fullmatch(revision)
                or not isinstance(outcome, str) or outcome not in OUTCOMES):
            raise ValueError("Invalid or repeated member batch row")
        seen.add(aid)
    return items


def atomic_json(path: Path, value: dict) -> None:
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         prefix=path.name + ".", suffix=".next", dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            json.dump(value, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def record_batch(report: dict, *, advance: bool, backfill_path: Path = DEFAULT_BACKFILL,
                 observations_path: Path = DEFAULT_OBSERVATIONS, non_projects_path: Path = DEFAULT_NON_PROJECTS) -> dict:
    rows = validate_report(report, advance=advance)
    with collector_lock(backfill_path):
        return record_locked(report, rows, advance, backfill_path, observations_path, non_projects_path)


def record_locked(report, rows, advance, backfill_path, observations_path, non_projects_path):
    state = read_backfill(backfill_path)
    previous_cursor = state["before_id"]
    last_id = rows[-1]["article_id"]
    batch_digest = digest(report)
    if advance and report["before_id"] != previous_cursor:
        if previous_cursor == last_id and state.get("last_historical_batch_digest") == batch_digest:
            return {**state["last_historical_batch"], "before_id": previous_cursor, "replayed": True}
        raise ValueError("Member backfill cursor changed, reload the worklist")

    current = open_index()
    refs = article_refs(current)
    exclusions = read_non_projects(non_projects_path)
    observations = read_observations(observations_path)
    additions = []
    for row in rows:
        aid = row["article_id"]
        revision = row["article_revision"]
        outcome = row["outcome"]
        if aid not in refs or revision != refs[aid][0] or (SITE, aid, revision) in exclusions:
            raise ValueError(f"Member batch article {aid} is outside the current revision worklist")
        has_link = bool(current.share_locations.get((SITE, aid)))
        if outcome == "linked" and not has_link:
            raise ValueError(f"Member batch article {aid} has no verified local link")
        if outcome != "linked" and not has_link:
            additions.append({"site_id": SITE, **row})

    if advance:
        reported = [row["article_id"] for row in rows]
        if sorted(reported, key=int, reverse=True) != reported or (previous_cursor and int(reported[0]) >= int(previous_cursor)):
            raise ValueError("Member historical batch is not in descending cursor order")
        worklist = build_worklist(current, observations, non_projects=exclusions,
                                  before_id=previous_cursor, limit=len(rows))
        # A successful import has already removed those IDs from the live worklist.
        pending = {row["article_id"] for row in worklist["items"]}
        if any(int(aid) > int(last_id) for aid in pending.difference(reported)):
            raise ValueError("Member historical batch would skip pending articles")

    if additions:
        document, _ = read_bound(observations_path) if observations_path.exists() else ({"schema": OBSERVATION_SCHEMA, "items": []}, None)
        for row in additions:
            key = tuple(row[field] for field in ("site_id", "article_id", "article_revision"))
            if row["outcome"] not in observations.get(key, ()):
                document["items"].append(row)
            observations[key] = observations.get(key, frozenset()) | {row["outcome"]}
        if len(document["items"]) > MAX_OBSERVATIONS:
            raise ValueError("Member observation count exceeded")
        # Evidence is saved first, so an interrupted checkpoint can replay this exact group.
        atomic_json(observations_path, document)

    summary = {"observed_at": datetime.now(SGT).isoformat(timespec="seconds"),
               "inspected": len(rows), "outcomes": dict(Counter(row["outcome"] for row in rows))}
    next_cursor = last_id if advance else previous_cursor
    counts = build_worklist(current, observations, non_projects=exclusions, limit=1)["counts"]
    state.update({"observed_at": summary["observed_at"], "before_id": next_cursor, "counts": counts,
                  "current_scope_complete": counts["missing_direct_url"] == 0,
                  "unresolved_observation_count": counts["access_required_current_revision"] + counts["review_pending_current_revision"],
                  "unresolved_observations": counts["unresolved_by_outcome"],
                  "last_batch": summary})
    if advance:
        state["last_historical_batch_digest"] = batch_digest
        state["last_historical_batch"] = summary
    atomic_json(backfill_path, state)
    return {**summary, "before_id": next_cursor, "replayed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Record read-back member outcomes in the local catalog checkpoint.")
    parser.add_argument("--input", metavar="FILE|-", required=True)
    parser.add_argument("--advance", action="store_true", help="Advance the historical cursor across this consecutive batch")
    args = parser.parse_args()
    try:
        if args.input == "-":
            raw = sys.stdin.buffer.read(REPORT_BYTES + 1)
        else:
            path = Path(args.input).resolve(strict=True)
            workspace = DEFAULT_BACKFILL.parents[4]
            if not path.is_relative_to(workspace) or not path.is_file():
                raise ValueError("Member batch input must be in the project workspace")
            with path.open("rb") as stream:
                raw = stream.read(REPORT_BYTES + 1)
        if len(raw) > REPORT_BYTES:
            raise ValueError("Member batch input is too large")
        result = record_batch(json.loads(raw), advance=args.advance)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Member batch not recorded: {exc}\n")
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
