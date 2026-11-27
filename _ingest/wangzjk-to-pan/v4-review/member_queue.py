"""Read the member article worklist without visiting pages or recording attempts."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from catalog_model import ART, Library, open_index, read_bound
from share_locations import ARTICLE_ORIGIN, SITE


OBSERVATION_SCHEMA = "wa-v4-member-observations.v1"
NON_PROJECT_SCHEMA = "wa-v4-member-non-projects.v1"
WORKLIST_SCHEMA = "wa-v4-member-worklist.v1"
BACKFILL_SCHEMA = "wa-v4-member-backfill.v1"
DEFAULT_OBSERVATIONS = Path(__file__).resolve().parent / "data" / "member-observations.json"
DEFAULT_NON_PROJECTS = DEFAULT_OBSERVATIONS.with_name("member-non-projects.json")
DEFAULT_BACKFILL = DEFAULT_OBSERVATIONS.with_name("member-backfill.json")
MAX_OBSERVATIONS = 100_000
MAX_NON_PROJECTS = 20_000
MAX_NON_PROJECT_BYTES = 4_000_000
MAX_LIMIT = 128
REVISION = re.compile(r"arv_[0-9a-f]{64}")
OUTCOME = re.compile(r"[a-z][a-z0-9_]{0,63}")
NO_ENTRY = "no_baidu_entry"
ACCESS_REQUIRED = "article_access_required"
REVIEW_OUTCOMES = frozenset({NO_ENTRY, "baidu_entry_pending", "page_not_readable"})
NON_PROJECT_REASONS = ("daily_news", "site_notice")

def read_observations(path: Path) -> dict[tuple[str, str, str], frozenset[str]]:
    """Read optional, per-revision evidence. Contradictory outcomes remain reviewable."""
    if not path.exists():
        return {}
    doc, _ = read_bound(path)
    if not isinstance(doc, dict) or doc.get("schema") != OBSERVATION_SCHEMA:
        raise ValueError("Invalid member observation schema")
    rows = doc.get("items")
    if not isinstance(rows, list) or len(rows) > MAX_OBSERVATIONS:
        raise ValueError("Invalid member observation count")
    out = defaultdict(set)
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"Invalid member observation at item {number}")
        site, aid, rev, outcome = (row.get(k) for k in ("site_id", "article_id", "article_revision", "outcome"))
        if not (
            isinstance(site, str) and 1 <= len(site) <= 253
            and isinstance(aid, str) and ART.fullmatch(aid)
            and isinstance(rev, str) and REVISION.fullmatch(rev)
            and isinstance(outcome, str) and OUTCOME.fullmatch(outcome)
        ):
            raise ValueError(f"Invalid member observation at item {number}")
        out[(site, aid, rev)].add(outcome)
    return {key: frozenset(values) for key, values in out.items()}


def read_non_projects(path: Path) -> dict[tuple[str, str, str], str]:
    """Load explicit exclusions for the same site's exact article revisions."""
    if not path.exists():
        return {}
    doc, _ = read_bound(path, MAX_NON_PROJECT_BYTES)
    if not isinstance(doc, dict) or set(doc) != {"schema", "items"} or doc["schema"] != NON_PROJECT_SCHEMA:
        raise ValueError("Invalid member non-project schema")
    rows = doc["items"]
    if not isinstance(rows, list) or len(rows) > MAX_NON_PROJECTS:
        raise ValueError("Invalid member non-project count")
    out = {}
    fields = ("site_id", "article_id", "article_revision", "reason")
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) != set(fields):
            raise ValueError(f"Invalid member non-project at item {number}")
        site, aid, rev, reason = (row[k] for k in fields)
        if not (
            site == SITE
            and isinstance(aid, str) and ART.fullmatch(aid)
            and isinstance(rev, str) and REVISION.fullmatch(rev)
            and reason in NON_PROJECT_REASONS
        ):
            raise ValueError(f"Invalid member non-project at item {number}")
        key = (site, aid, rev)
        if key in out:
            raise ValueError(f"Duplicate member non-project at item {number}")
        out[key] = reason
    return out


def read_backfill(path: Path = DEFAULT_BACKFILL) -> dict:
    """Read the durable sync cursor recorded after a consecutive historical batch."""
    if not path.exists():
        return {"schema": BACKFILL_SCHEMA, "before_id": None}
    doc, _ = read_bound(path)
    if not isinstance(doc, dict) or doc.get("schema") != BACKFILL_SCHEMA or "before_id" not in doc:
        raise ValueError("Invalid member backfill schema")
    before_id = doc.get("before_id")
    if before_id is not None and (not isinstance(before_id, str) or not ART.fullmatch(before_id)):
        raise ValueError("Invalid saved member backfill cursor")
    return doc


def article_refs(library: Library) -> dict[str, tuple[str, str]]:
    refs = {}
    for row in library.by_id.values():
        for ref in row["sources"]:
            if ref["site_id"] != SITE:
                continue
            aid, rev, url = (ref[k] for k in ("article_id", "article_revision", "url"))
            if not ART.fullmatch(aid) or not REVISION.fullmatch(rev) or url != f"{ARTICLE_ORIGIN}/{aid}.html":
                raise ValueError("Invalid canonical article identity in catalog")
            identity = (rev, url)
            if refs.setdefault(aid, identity) != identity:
                raise ValueError("Conflicting article revisions in catalog")
    return refs


def build_worklist(
    library: Library,
    observations: dict[tuple[str, str, str], frozenset[str]],
    *,
    non_projects: dict[tuple[str, str, str], str] | None = None,
    limit: int = 24,
    before_id: str | None = None,
    include_no_entry: bool = False,
    only_access_required: bool = False,
    only_review: bool = False,
) -> dict:
    """List exact article revisions with no known direct locator, newest article IDs first."""
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"Limit must be between 1 and {MAX_LIMIT}")
    if before_id is not None and (not isinstance(before_id, str) or not ART.fullmatch(before_id)):
        raise ValueError("Invalid article ID cursor")
    if sum((include_no_entry, only_access_required, only_review)) > 1:
        raise ValueError("Select one of the member review modes")

    refs = article_refs(library)
    exclusions = non_projects or {}
    with_link = no_entry = access_required = review_pending = 0
    non_resource_by_reason = dict.fromkeys(NON_PROJECT_REASONS, 0)
    unresolved_by_outcome = Counter()
    pending = []
    for aid in sorted(refs, key=int, reverse=True):
        rev, url = refs[aid]
        if library.share_locations.get((SITE, aid)):
            with_link += 1
            continue
        key = (SITE, aid, rev)
        reason = exclusions.get(key)
        if reason in NON_PROJECT_REASONS:
            non_resource_by_reason[reason] += 1
            continue
        outcomes = observations.get(key, frozenset())
        if outcomes == {NO_ENTRY}:
            no_entry += 1
        restricted = ACCESS_REQUIRED in outcomes
        review = bool(outcomes) and outcomes <= REVIEW_OUTCOMES and not restricted
        access_required += restricted
        review_pending += review
        last_outcome = next(iter(outcomes)) if len(outcomes) == 1 else "conflicting_observations" if outcomes else None
        if restricted or review:
            unresolved_by_outcome[ACCESS_REQUIRED if restricted else last_outcome] += 1
        if only_access_required:
            selected = restricted
        elif only_review:
            selected = review
        else:
            selected = not restricted and (include_no_entry or not review)
        if not selected:
            continue
        pending.append({"site_id": SITE, "article_id": aid, "article_revision": rev, "url": url,
                        "last_outcome": ACCESS_REQUIRED if restricted else last_outcome})

    if before_id is not None:
        pending = [row for row in pending if int(row["article_id"]) < int(before_id)]
    items = pending[:limit]
    non_resource = sum(non_resource_by_reason.values())
    missing = len(refs) - with_link - non_resource
    return {
        "schema": WORKLIST_SCHEMA,
        "site_id": SITE,
        "snapshot_id": library.snapshot_id,
        "before_id": before_id,
        "counts": {
            "indexed_articles": len(refs),
            "known_direct_url": with_link,
            "verified_non_resource": non_resource,
            "verified_non_resource_by_reason": non_resource_by_reason,
            "missing_direct_url": missing,
            "no_baidu_entry_current_revision": no_entry,
            "access_required_current_revision": access_required,
            "review_pending_current_revision": review_pending,
            "unresolved_by_outcome": dict(unresolved_by_outcome),
            "regular_pending": missing - access_required - review_pending,
            "matching_after_cursor": len(pending),
            "returned": len(items),
        },
        "items": items,
        "next_before_id": items[-1]["article_id"] if len(pending) > len(items) else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="List exact articles missing a known Baidu share locator.")
    parser.add_argument("--limit", type=int, default=24, help=f"Maximum returned articles, 1–{MAX_LIMIT}")
    position = parser.add_mutually_exclusive_group()
    position.add_argument("--before-id", help="Exclusive descending article ID cursor")
    position.add_argument("--resume", action="store_true", help="Use the saved historical backfill cursor")
    review = parser.add_mutually_exclusive_group()
    review.add_argument("--include-no-entry", action="store_true", help="Include unresolved source checks, excluding site access restrictions")
    review.add_argument("--only-access-required", action="store_true", help="List only currently restricted article revisions")
    review.add_argument("--only-review", action="store_true", help="List only unresolved source checks, excluding site access restrictions")
    parser.add_argument("--observations", type=Path, default=DEFAULT_OBSERVATIONS, help="Optional member observation JSON")
    args = parser.parse_args()
    try:
        before_id = read_backfill().get("before_id") if args.resume else args.before_id
        worklist = build_worklist(
            open_index(), read_observations(args.observations),
            non_projects=read_non_projects(DEFAULT_NON_PROJECTS),
            limit=args.limit, before_id=before_id, include_no_entry=args.include_no_entry,
            only_access_required=args.only_access_required, only_review=args.only_review,
        )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Member worklist unavailable: {exc}\n")
    print(json.dumps(worklist, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
