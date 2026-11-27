"""The compact, read-only projection used by the course calendar."""

from __future__ import annotations

from datetime import date
import re
from urllib.parse import parse_qs

from catalog_model import CATEGORIES, Library, digest
from member_queue import DEFAULT_NON_PROJECTS, read_non_projects


PAGE_SIZE = 48
CURSOR = re.compile(r"[0-9a-f]{64}\.rv_[0-9a-f]{64}\Z")
PARAMETERS = {"q", "category", "site", "day", "after"}


class CalendarRequestError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def read_query(query: str, sites: set[str]) -> dict[str, str]:
    try:
        fields = parse_qs(query, keep_blank_values=True, max_num_fields=len(PARAMETERS))
        if fields.keys() - PARAMETERS or any(len(values) != 1 for values in fields.values()):
            raise ValueError
        params = {key: value[0] for key, value in fields.items()}
        if len(params.get("q", "")) > 200 or "\x00" in params.get("q", ""):
            raise ValueError
        category = params.get("category", "")
        site = params.get("site", "")
        if (category and category not in CATEGORIES) or (site and site not in sites):
            raise ValueError
        day = params.get("day", "")
        if day and date.fromisoformat(day).isoformat() != day:
            raise ValueError
        after = params.get("after", "")
        if after and not CURSOR.fullmatch(after):
            raise ValueError
    except ValueError:
        raise CalendarRequestError(400, "课程筛选或日期无效。") from None
    return params


def is_course(row: dict, exclusions: dict) -> bool:
    sources = row["sources"]
    return bool(sources) and row.get("demo") is not True and any(
        (source["site_id"], source["article_id"], source.get("article_revision")) not in exclusions
        for source in sources
    )


def summary(row: dict, share_locations: dict) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "published_at": row["published_at"] or None,
        "category_label": row["category_label"],
        "site_label": row["site_label"],
        "has_baidu": any(
            share_locations.get((source["site_id"], source["article_id"])) for source in row["sources"]
        ),
    }


def course_calendar(library: Library, query: str) -> dict:
    sites = [{"id": site["id"], "label": site["label"]} for site in library.sites]
    params = read_query(query, {site["id"] for site in sites})
    exclusions = read_non_projects(DEFAULT_NON_PROJECTS)
    filter_values = {
        "q": params.get("q", ""),
        "category": params.get("category", ""),
        "site": params.get("site", ""),
        "to": params.get("day", ""),
    }
    revision = digest([library.snapshot_id, sorted(exclusions.items()), filter_values])
    after = params.get("after", "")
    if after and not after.startswith(revision + "."):
        raise CalendarRequestError(409, "课程目录或筛选已更新，请重新加载。")
    matches = library.match(filter_values, set())
    rows = [row for row in matches if is_course(row, exclusions)]
    start = 0
    if after:
        resource_id = after.partition(".")[2]
        for index, row in enumerate(rows):
            if row["id"] == resource_id:
                start = index + 1
                break
        else:
            raise CalendarRequestError(409, "课程目录已更新，请重新加载。")
    end = min(start + PAGE_SIZE, len(rows))
    return {
        "items": [summary(row, library.share_locations) for row in rows[start:end]],
        "total": len(rows),
        "next": revision + "." + rows[end - 1]["id"] if end < len(rows) else None,
        "categories": [{"id": tag, "label": label} for tag, label in CATEGORIES.items()],
        "sites": sites,
    }
