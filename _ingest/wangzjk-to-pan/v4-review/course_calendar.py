"""The compact, read-only projection used by the course calendar."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date
import re
from threading import Lock
from types import MappingProxyType
from urllib.parse import parse_qs

from catalog_model import Library, digest
from catalog_semantics import CATEGORIES, CATEGORY_ALIASES, PLATFORMS
from member_queue import DEFAULT_NON_PROJECTS, read_non_projects


PAGE_SIZE = 48
MAX_CACHED_SEARCHES = 8
MAX_CACHED_ROWS = 36_000
CURSOR = re.compile(r"[0-9a-f]{64}\.(?:rv|cv)_[0-9a-f]{64}\Z")
PARAMETERS = {"q", "category", "subcategory", "platform", "site", "day", "after", "selected", "versions"}


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
        if params.get("selected", "0") not in ("0", "1") or params.get("versions", "latest") not in ("latest", "all"):
            raise ValueError
        category = params.get("category", "")
        category = CATEGORY_ALIASES.get(category, category)
        sub = params.get("subcategory", "")
        platform = params.get("platform", "")
        site = params.get("site", "")
        if (category and category not in CATEGORIES or site and site not in sites
                or platform and platform not in PLATFORMS
                or sub and sub not in CATEGORIES.get(category,("",()))[1]):
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
    if (row.get('category'), row.get('subcategory')) in {('news', '新闻简报'), ('reference','站点说明')}:
        return False
    sources = row["sources"]
    return bool(sources) and row.get("demo") is not True and any(
        (source["site_id"], source["article_id"], source.get("article_revision")) not in exclusions
        for source in sources
    )


def summary(row: dict, share_locations: dict, selected: frozenset[str]) -> dict:
    action_pending = bool(row.get("action_pending"))
    return {
        "id": row["id"],
        **{key:row[key] for key in ("display_id","article_key","platforms","source_count","version_count",
                                  "version_label","classification_state","category","subcategory","site_id","article_id","status")
           if key in row},
        "action_pending": action_pending,
        "requestable": not action_pending and row.get("requestable", row.get("selectable")) is True,
        "selected": not action_pending and row["id"] in selected,
        "title": row["title"],
        "published_at": row["published_at"] or None,
        "category_label": row["category_label"],
        "site_label": row["site_label"],
        "has_baidu": any(
            share_locations.get((source["site_id"], source["article_id"])) for source in row["sources"]
        ),
    }


def fingerprint_exclusions(exclusions):
    return digest(sorted(exclusions.items()))


def display_key(row):
    return row.get("display_id", row["id"])


class CalendarCache:
    """Keep browsing matches per catalog. Local progress and selected views are read separately."""

    def __init__(self):
        self.lock = Lock()
        self.generation = None
        self.facets = None
        self.matches = OrderedDict()
        self.retained_rows = 0

    def get(self, library, generation, criteria, eligible, selected):
        with self.lock:
            if generation != self.generation:
                self.matches.clear()
                self.retained_rows = 0
                self.facets = (library.facets(eligible=eligible) if hasattr(library, "semantic_rows")
                               else library.facets())
                self.generation = generation
            key = tuple(criteria.items())
            selected_only = criteria["selected"] == "1"
            if not selected_only and key in self.matches:
                self.matches.move_to_end(key)
                rows, positions = self.matches[key]
                return rows, positions, self.facets
            if hasattr(library, "semantic_rows"):
                rows = tuple(library.match(criteria, selected, eligible=eligible))
            else:
                rows = tuple(row for row in library.match(criteria, selected) if eligible(row))
            positions = MappingProxyType({display_key(row): offset for offset, row in enumerate(rows)})
            if not selected_only and len(rows) <= MAX_CACHED_ROWS:
                while (len(self.matches) >= MAX_CACHED_SEARCHES
                       or self.retained_rows + len(rows) > MAX_CACHED_ROWS):
                    _, (removed, _) = self.matches.popitem(last=False)
                    self.retained_rows -= len(removed)
                self.matches[key] = (rows, positions)
                self.retained_rows += len(rows)
            return rows, positions, self.facets


def course_calendar(library: Library, query: str, *, progress=None, selected_ids=(), selection_revision=0) -> dict:
    sites = [{"id": site["id"], "label": site["label"]} for site in library.sites]
    params = read_query(query, {site["id"] for site in sites})
    exclusions = read_non_projects(DEFAULT_NON_PROJECTS)
    filter_values = {
        "q": params.get("q", ""),
        "category": CATEGORY_ALIASES.get(params.get("category", ""), params.get("category", "")),
        "subcategory": params.get("subcategory", ""),
        "platform": params.get("platform", ""),
        "site": params.get("site", ""),
        "to": params.get("day", ""),
        "selected": params.get("selected", "0"),
        "versions": params.get("versions", "latest"),
    }
    selected = frozenset(selected_ids)
    selection = (selection_revision, sorted(selected)) if filter_values["selected"] == "1" else None
    generation = (library.snapshot_id, fingerprint_exclusions(exclusions))
    revision = digest([generation, filter_values, selection])
    after = params.get("after", "")
    if after and not after.startswith(revision + "."):
        raise CalendarRequestError(409, "课程目录或筛选已更新，请重新加载。")

    def eligible(row):
        return is_course(row, exclusions)

    cache = getattr(library, "calendar_cache", None)
    if cache is None:
        cache = vars(library).setdefault("calendar_cache", CalendarCache())
    rows, positions, facets = cache.get(library, generation, filter_values, eligible, selected)
    start = 0
    if after:
        resource_id = after.partition(".")[2]
        position = positions.get(resource_id)
        if position is None:
            raise CalendarRequestError(409, "课程目录已更新，请重新加载。")
        start = position + 1
    end = min(start + PAGE_SIZE, len(rows))
    page = rows[start:end]
    pulse = progress.read() if progress is not None and page else None
    return {
        "items": [{**summary(row, library.share_locations, selected), **(progress.fields(row, pulse) if pulse else {})}
                  for row in page],
        "total": len(rows),
        "next": revision + "." + display_key(rows[end - 1]) if end < len(rows) else None,
        "snapshot_id": library.snapshot_id,
        "categories":facets["categories"], "platforms":facets["platforms"],
        "category_aliases":facets["category_aliases"], "project_total":facets["project_count"],
        "sites": sites,
    }
