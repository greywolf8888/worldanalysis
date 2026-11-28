"""Local-only, resumable whole-site planning; NEVER a browser/transfer executor.

Catalog and Journal are read-only inputs. Only crawl_state.JSON and its private
atomic-write/lease files are written, strictly below this isolated branch.
next_batch is a repeatable selection, NOT a claim; Journal.claim still has to
precede any normal-UI save. An existing lease is never expired or force-unlocked.

Dated resources sort by the newest real published_at/updated_at value. Undated
resources retain their observed listing order (then catalog insertion order);
IDs, titles, observation times and queue timestamps are NEVER publication dates.
Pagination uses only observed next_pages hrefs, depth-first in observed order,
with at most one listing per bounded batch. Frontier exhaustion is not proof of
whole-site completion. No network, browser, SQL, scheduling or cloud API exists.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit
import uuid

from .journal import Journal, canonical_share

BRANCH_ROOT = Path(__file__).resolve().parents[1]
HOME = "https://www.wangzjk.com/"
SCHEMA = "wangzjk-to-pan.crawl.v1"
_DATE_WINDOW_SCHEMA = "wangzjk-date-window-listing-evidence.v1"
_DATE_WINDOW_DATES = ("2026-11-18", "2026-11-24")
MAX_JSON_BYTES = 32 * 1024 * 1024
PHASES = frozenset({"active", "paused", "needs_human"})
KINDS = frozenset({"listing", "article", "transfer", "reconcile"})
INVALID_EVIDENCE = frozenset({"share_expired", "share_cancelled", "content_removed"})
GATES = frozenset({"blocked", "needs_login", "needs_access", "permission_denied", "captcha", "rate_limited"})
SAVED = frozenset({"saved", "saved_root_verified"})
_PRIVATE_KEYS = frozenset({"cookie", "cookies", "authorization", "password", "pwd", "token",
    "bdstoken", "sekey", "randsk", "sign", "signature", "dlink", "img_url", "access_token", "refresh_token"})


class CrawlError(ValueError):
    """Input/checkpoint failure; never evidence that a resource is invalid."""


class CrawlLocked(CrawlError):
    """An existing or changed coordinator lease needs manual reconciliation."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, label, maximum=2000):
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise CrawlError("Invalid " + label)
    if re.search(r"(?i)(?:cookie|authorization|password|token|bdstoken|dlink)\s*[:=]", value):
        raise CrawlError("Credential-like text is forbidden")
    return value


def _local(value):
    path = Path(value)
    if ".." in path.parts:
        raise CrawlError("Local traversal is forbidden")
    if not path.is_absolute():
        path = BRANCH_ROOT / path
    path = Path(os.path.abspath(path))
    try:
        parts = path.relative_to(BRANCH_ROOT).parts
    except ValueError:
        raise CrawlError("Local paths must stay inside the isolated branch") from None
    current = BRANCH_ROOT
    for part in parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise CrawlError("Local links/junctions are forbidden")
    try:
        path.resolve().relative_to(BRANCH_ROOT)
    except ValueError:
        raise CrawlError("Resolved path escaped the isolated branch") from None
    return path


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CrawlError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise CrawlError("Nonfinite JSON value")


def _read_json(path):
    path = _local(path)
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise CrawlError("An existing regular JSON input is required")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise CrawlError("Local JSON input exceeds the bounded size")
    raw = path.read_bytes()
    if len(raw) > MAX_JSON_BYTES:
        raise CrawlError("Local JSON input exceeds the bounded size")
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, json.JSONDecodeError):
        raise CrawlError("Invalid UTF-8 JSON input") from None
    if not isinstance(obj, dict):
        raise CrawlError("JSON input must be an object")
    return obj, raw


def _no_private(value):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in _PRIVATE_KEYS or normalized.endswith("_token"):
                raise CrawlError("Credential/temporary-ticket evidence is forbidden")
            _no_private(child)
    elif isinstance(value, list):
        for child in value:
            _no_private(child)
    elif isinstance(value, str) and re.search(r"(?i)(?:[?&](?:pwd|token|sign|sekey)=|(?:cookie|authorization|password)\s*[:=])", value):
        raise CrawlError("Credential-like evidence value is forbidden")


def _site_url(value, base=HOME):
    value = _text(value, "observed website href", 4096)
    if "\\" in value:
        raise CrawlError("Unsafe website href")
    try:
        p = urlsplit(urljoin(base, value))
        if (p.scheme != "https" or p.hostname not in {"wangzjk.com", "www.wangzjk.com"}
                or p.username is not None or p.password is not None or p.port not in (None, 443)
                or p.fragment or any(x in {".", ".."} for x in p.path.split("/"))
                or re.search(r"(?i)%(?:2e|2f|5c|00)", p.path)):
            raise CrawlError("Website URL is outside the authorized public scope")
        # Real query pagination is retained, not changed to guessed /page/N URLs.
        query = parse_qsl(p.query, keep_blank_values=True)
        if (len({k for k, _ in query}) != len(query)
                or any(k not in {"paged", "cat"} or not re.fullmatch(r"[0-9]+", v) for k, v in query)):
            raise CrawlError("Only observed public pagination query fields are allowed")
    except ValueError as exc:
        if isinstance(exc, CrawlError):
            raise
        raise CrawlError("Invalid observed website href") from None
    return urlunsplit(("https", "www.wangzjk.com", p.path or "/", p.query, ""))


def _listing_url(value, base=HOME):
    url = _site_url(value, base)
    path = urlsplit(url).path
    if (re.fullmatch(r"/[0-9]+[.]html", path) or path.startswith(("/pay-download", "/login", "/user", "/account"))):
        raise CrawlError("Observed listing URL cannot be an article or restricted entry")
    return url


def _article(record):
    if not isinstance(record, dict):
        raise CrawlError("Article projection must be an object")
    aid = record.get("article_id")
    if not isinstance(aid, str) or not re.fullmatch(r"[1-9][0-9]{0,23}", aid):
        raise CrawlError("Observed numeric article identity is required")
    url = _site_url(record.get("url"))
    if url != HOME + aid + ".html":
        raise CrawlError("Article ID/URL binding does not match")
    result = {"article_id": aid, "url": url, "title": _text(record.get("title"), "article title", 2000)}
    categories = record.get("categories", [])
    if not isinstance(categories, list) or any(not isinstance(c, str) for c in categories):
        raise CrawlError("Observed categories must be a list of strings")
    result["categories"] = list(dict.fromkeys(categories))
    for key in ("published_at", "updated_at"):
        value = record.get(key)
        if value is not None:
            result[key] = _text(value, key, 128)
    return result


def _date_key(value):
    """Sort only explicit ISO calendar values; never assign a missing date/zone."""
    if not isinstance(value, str) or not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:$|[T ])", value):
        return None
    try:
        if len(value) == 10:
            return (date.fromisoformat(value).toordinal(), 0, 0)
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(timezone.utc)
        return (stamp.date().toordinal(), stamp.hour * 3600 + stamp.minute * 60 + stamp.second, stamp.microsecond)
    except (ValueError, OverflowError):
        return None


def _ordering(article):
    dates = [(key, article[key], _date_key(article.get(key))) for key in ("published_at", "updated_at") if key in article]
    dates = [entry for entry in dates if entry[2] is not None]
    if not dates:
        return (1, 0, 0, 0), {}
    key, value, parts = max(dates, key=lambda entry: entry[2])
    return (0, *(-x for x in parts)), {"order_date": value, "order_date_field": key}


def _merge_article_metadata(target, incoming):
    """Keep observed dates when a later projection has fewer/staler fields."""
    previous = {key: target[key] for key in ("published_at", "updated_at") if key in target}
    target.update(deepcopy(incoming))
    for key, value in previous.items():
        old, new = _date_key(value), _date_key(target.get(key))
        if old is not None and (new is None or old > new):
            target[key] = value
    return target


def _scope(value):
    value = _text(value, "scope", 4200)
    if value in {"site", "platform:baidu", "platform:wangzjk"}:
        return value
    if value.startswith("listing:"):
        return "listing:" + _listing_url(value[8:])
    if value.startswith("article:") and re.fullmatch(r"[1-9][0-9]{0,23}", value[8:]):
        return value
    if value.startswith("share:"):
        return "share:" + canonical_share(value[6:])
    raise CrawlError("Unknown exact crawl scope")


def _listing(facts, url=None):
    if not isinstance(facts, dict):
        raise CrawlError("Listing facts must be an object")
    url = _listing_url(facts.get("url", url))
    if not isinstance(facts.get("articles"), list) or not isinstance(facts.get("next_pages"), list):
        raise CrawlError("Observed articles and next_pages arrays are required")
    articles, seen = [], set()
    for record in facts["articles"]:
        article = _article(record)
        if article["article_id"] not in seen:
            seen.add(article["article_id"]); articles.append(article)
    pages = list(dict.fromkeys(_listing_url(href, url) for href in facts["next_pages"]))
    return {"url": url, "articles": articles, "next_pages": pages}


def _calendar_day(value):
    return isinstance(value, str) and len(value) == 10 and _date_key(value) is not None


def _window_candidates(manifest):
    """Validate the one authorized supplement and its observed older boundary."""
    first, last = _DATE_WINDOW_DATES
    dates = manifest.get("dates")
    if (manifest.get("schema") != _DATE_WINDOW_SCHEMA or not isinstance(dates, dict)
            or (dates.get("from"), dates.get("through")) != (first, last) or dates.get("inclusive") is not True
            or manifest.get("source") != "normal_ui_visible_listing"
            or manifest.get("only_listing_metadata") is not True or manifest.get("scope_boundary_observed") is not True
            or manifest.get("stop_reason") != "observed_first_older_date_on_real_listing"
            or any(type(manifest.get(key)) is not int or manifest[key] != 0 for key in ("unknown_dates", "duplicate_rows"))):
        raise CrawlError("Date window requires the exact inclusive scope and a closed normal UI listing manifest")
    pages, rows = manifest.get("pages"), manifest.get("articles")
    if not isinstance(pages, list) or not pages or not isinstance(rows, list) or not rows:
        raise CrawlError("Date window needs observed listing pages and candidate articles")
    next_urls, visited, observed_rows = {HOME}, set(), 0
    for index, page in enumerate(pages):
        if not isinstance(page, dict):
            raise CrawlError("Malformed date-window listing page")
        url = _listing_url(page.get("url"))
        oldest, following, count = page.get("oldest_published_at"), page.get("observed_next_pages"), page.get("in_scope_rows")
        if (url not in next_urls or url in visited or not isinstance(following, list) or not _calendar_day(oldest)
                or (oldest < first) != (index == len(pages) - 1) or type(count) is not int or count < 0
                or type(page.get("date_unknown_rows")) is not int or page["date_unknown_rows"] != 0):
            raise CrawlError("Date window needs linked visible pages through the first older publication day")
        visited.add(url)
        next_urls = {_listing_url(href, url) for href in following}
        observed_rows += count
    candidates, counts = {}, {}
    for row in rows:
        article = _article(row)
        aid, day = article["article_id"], article.get("published_at")
        if aid in candidates or not _calendar_day(day) or not first <= day <= last:
            raise CrawlError("Date-window articles need unique identities and in-scope publication days")
        candidates[aid] = day
        counts[day] = counts.get(day, 0) + 1
    if observed_rows != len(candidates) or manifest.get("counts") != counts:
        raise CrawlError("Date-window listing counts do not match the unique articles")
    return candidates


def _date_addendum_candidates(manifest):
    """Accept only the separately authorized newer-day publication evidence."""
    schema = manifest.get("schema")
    day = "2026-11-25" if schema == "wangzjk-exact-newest-publication-day-roster.v1" else "2026-11-26"
    if schema == "wangzjk-exact-newest-publication-day-roster.v1":
        if (manifest.get("published_day") != day or manifest.get("website") != HOME
                or manifest.get("complete_day_boundary_observed") is not True
                or manifest.get("based_on_explicit_published_date_not_title_or_update_date") is not True):
            raise CrawlError("Newer day requires a proven boundary and real publication dates")
        rows = manifest.get("eligible_candidates")
    elif schema == "wangzjk-20261126-published-posts-refresh.v1":
        source = urlsplit(_text(manifest.get("source"), "source", 2048))
        params = dict(parse_qsl(source.query))
        if (manifest.get("publication_day") != day or source.scheme != "https" or source.netloc != "www.wangzjk.com"
                or source.path != "/wp-json/wp/v2/posts" or source.fragment or params.get("status") != "publish"
                or params.get("after") != day + "T00:00:00" or params.get("before") != day + "T23:59:59"
                or params.get("orderby") != "date" or params.get("order") != "desc"):
            raise CrawlError("Newer day requires the exact published posts boundary")
        raw = manifest.get("latest_day_rows")
        if (not isinstance(raw, list) or any(not isinstance(x, dict) or not isinstance(x.get("date"), str)
                or len(x["date"]) != 19 or not x["date"].startswith(day + "T") or _date_key(x["date"]) is None for x in raw)
                or [x["date"] for x in raw] != sorted((x["date"] for x in raw), reverse=True)):
            raise CrawlError("Newer day publication order is not observed")
        rows = [{"article_id": x.get("id"), "url": x.get("url"), "title": x.get("renderedTitle"), "published_at": day} for x in raw]
    else:
        raise CrawlError("Unsupported date addendum evidence")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise CrawlError("Date addendum needs a bounded observed publication list")
    candidates = {}
    for row in rows:
        a = _article(row)
        if a.get("published_at") != day or a["article_id"] in candidates:
            raise CrawlError("Duplicate or out-of-day addendum article")
        candidates[a["article_id"]] = day
    return candidates


def _window_catalog_match(aid, record, candidates):
    # Use the raw catalog row: preserved/merged listing metadata and updated_at
    # must not substitute for a missing or changed catalog publication day.
    return (aid in candidates and isinstance(record, dict) and record.get("published_at") == candidates[aid]
        and _article(record)["article_id"] == aid)


def _op_view(operation):
    fields = ("operation_id", "state", "ever_claimed", "article_ids", "account_ref",
        "destination", "category", "verification_scope")
    result = {key: deepcopy(operation[key]) for key in fields if key in operation}
    result["share_url"] = canonical_share(operation["share_url"])
    return result


class CrawlQueue:
    """One branch-local coordinator, with read-only, repeatable selection.

    Reads never create directories. Mutations take their own O_EXCL lease, or
    reuse this instance's explicitly held lease on the owning thread. Recovery
    from a leftover lease is manual; there is deliberately no unlock/TTL API.
    """

    def __init__(self, state_dir=BRANCH_ROOT / "state", *, catalog_path=None, journal=None):
        self.state_dir = _local(state_dir)
        self.path = self.state_dir / "crawl_state.JSON"
        self.lock_path = self.state_dir / ".crawl-coordinator.lease"
        self.catalog_path = _local(catalog_path if catalog_path is not None else self.state_dir / "catalog.json")
        if self.catalog_path in {self.path, self.lock_path}:
            raise CrawlError("Catalog must be a separate read-only input")
        self.journal = journal if journal is not None else Journal(self.state_dir)
        self._held = None
        self._thread = None

    def _empty(self):
        return {"schema": SCHEMA, "revision": 0, "phase": "active", "phase_reason": "",
            "phase_history": [], "listings": {}, "listing_order": [], "last_attempts": {}, "invalid": {}, "blocks": {},
            "discovered_articles": {}, "discovered_order": [], "observed_edges": {}}

    def _evidence(self, reference):
        path = _local(reference)
        if path.suffix.lower() != ".json" or path in {self.path, self.catalog_path, self.lock_path}:
            raise CrawlError("A separate branch-local business JSON evidence file is required")
        obj, raw = _read_json(path)
        _no_private(obj)
        return obj, {"path": path.relative_to(BRANCH_ROOT).as_posix(), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}

    def _check_evidence(self, ref):
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256", "bytes"}:
            raise CrawlError("Malformed evidence reference")
        obj, actual = self._evidence(ref["path"])
        if actual != ref:
            raise CrawlError("Crawl evidence changed; reconcile without replay")
        return obj

    def _load(self):
        path = _local(self.path)
        if not path.exists():
            return self._empty(), None
        obj, _ = _read_json(path)
        # Early v1 checkpoints lacked these append-only observation histories.
        # Migration is in memory only on reads; no file is created or reset.
        for key, default in (("discovered_articles", {}), ("discovered_order", []), ("observed_edges", {})):
            obj.setdefault(key, default)
        required = self._empty()
        if (obj.get("schema") != SCHEMA or type(obj.get("revision")) is not int or obj["revision"] < 0
                or obj.get("phase") not in PHASES or not required.keys() <= obj.keys()
                or any(not isinstance(obj[k], dict) for k in ("listings", "invalid", "blocks", "last_attempts", "discovered_articles", "observed_edges"))
                or not isinstance(obj["discovered_order"], list)
                or set(obj["discovered_order"]) != set(obj["discovered_articles"])
                or len(set(obj["discovered_order"])) != len(obj["discovered_order"])
                or not isinstance(obj["listing_order"], list) or not isinstance(obj["phase_history"], list)
                or set(obj["listing_order"]) != set(obj["listings"])
                or len(set(obj["listing_order"])) != len(obj["listing_order"])):
            raise CrawlError("Invalid crawl checkpoint; do not reset")
        _no_private(obj)
        for url, facts in obj["listings"].items():
            if _listing(facts, url)["url"] != url:
                raise CrawlError("Listing checkpoint identity mismatch")
            if facts.get("evidence") is not None:
                self._check_evidence(facts["evidence"])
        for aid, article in obj["discovered_articles"].items():
            if _article(article)["article_id"] != aid:
                raise CrawlError("Historical article identity mismatch")
        for url, pages in obj["observed_edges"].items():
            if _listing_url(url) != url or not isinstance(pages, list) or any(_listing_url(p, url) != p for p in pages):
                raise CrawlError("Invalid observed pagination history")
        for url, attempt in obj["last_attempts"].items():
            if _listing_url(url) != url or attempt.get("scope") != "listing:" + url:
                raise CrawlError("Invalid listing attempt checkpoint")
            if attempt.get("evidence") is not None:
                self._check_evidence(attempt["evidence"])
        for collection in ("blocks", "invalid"):
            for key, record in obj[collection].items():
                if _scope(key) != key or not isinstance(record, dict) or record.get("scope") != key:
                    raise CrawlError("Invalid scope checkpoint")
                if record.get("evidence") is not None:
                    self._check_evidence(record["evidence"])
                if collection == "invalid":
                    if record.get("evidence_type") not in INVALID_EVIDENCE or record.get("evidence") is None:
                        raise CrawlError("Invalidity must retain explicit evidence")
                    self._validate_invalid(key, record["evidence_type"], self._check_evidence(record["evidence"]))
                elif record.get("phase") not in PHASES or type(record.get("resolved")) is not bool:
                    raise CrawlError("Invalid blocking checkpoint")
                if collection == "blocks" and record.get("resolution", {}).get("evidence") is not None:
                    self._check_evidence(record["resolution"]["evidence"])
        candidates = _window_candidates(self._check_evidence(obj["date_window"])) if "date_window" in obj else None
        addenda = obj.get("date_window_addenda", [])
        if not isinstance(addenda, list) or len(addenda) > 16 or addenda and candidates is None:
            raise CrawlError("Date addenda require the original bound manifest")
        for ref in addenda:
            extra = _date_addendum_candidates(self._check_evidence(ref))
            if set(extra) & candidates.keys():
                raise CrawlError("Date addendum overlaps a locked publication identity")
            candidates.update(extra)
        return obj, candidates

    def _check_lease(self):
        if self._held is None or self._thread != threading.get_ident():
            raise CrawlLocked("This thread does not own the coordinator lease")
        path = _local(self.lock_path)
        if not path.is_file() or path.read_bytes() != self._held:
            raise CrawlLocked("Coordinator lease changed; stop and reconcile manually")

    @contextmanager
    def lease(self, owner="coordinator"):
        """Hold exclusive coordination; no TTL, PID probing or forced unlock."""
        owner = _text(owner, "coordinator owner", 128)
        if self._held is not None:
            raise CrawlLocked("Coordinator lease is already held on this instance")
        _local(self.state_dir).mkdir(parents=True, exist_ok=True)
        path = _local(self.lock_path)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise CrawlLocked("Coordinator lease exists; no TTL expiry or force unlock") from None
        raw = json.dumps({"schema": "wangzjk-to-pan.crawl-lease.v1", "owner": owner, "nonce": uuid.uuid4().hex}).encode("utf-8")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            self._held, self._thread = raw, threading.get_ident()
            yield self
        finally:
            try:
                self._check_lease()
                path.unlink()
            finally:
                self._held = self._thread = None

    @contextmanager
    def _mutation(self):
        if self._held is not None:
            self._check_lease()
            yield
            self._check_lease()
        else:
            with self.lease():
                yield

    def _save(self, data, before):
        if data == before:
            return False
        self._check_lease()
        data["revision"] += 1
        data["updated_at"] = _now()  # Queue metadata only; not a resource date.
        raw = (json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2) + "\n").encode("utf-8")
        if len(raw) > MAX_JSON_BYTES:
            raise CrawlError("Crawl checkpoint exceeds bounded size")
        temp = _local(self.state_dir / (".crawl-state-" + uuid.uuid4().hex + ".tmp"))
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            self._check_lease()
            os.replace(temp, _local(self.path))
        finally:
            if temp.exists():
                temp.unlink()
        return True

    @staticmethod
    def _blocked(item, data):
        scopes = {"site", item["scope"]}
        scopes.add("platform:baidu" if item["kind"] in {"transfer", "reconcile"} else "platform:wangzjk")
        scopes.update("article:" + aid for aid in item.get("article_ids", []))
        if "article_id" in item:
            scopes.add("article:" + item["article_id"])
        return [deepcopy(block) for scope, block in data["blocks"].items() if scope in scopes and not block["resolved"]]

    def configure_date_window(self, evidence_ref, *, human_confirmed=False):
        """Bind a branch-local manifest under the paused queue's lease; never resume it."""
        if human_confirmed is not True:
            raise CrawlError("Date window requires explicit human confirmation")
        with self._mutation():
            data, _ = self._load(); before = deepcopy(data)
            if data["phase"] != "paused":
                raise CrawlError("Date window can only be configured while the queue is paused")
            manifest, ref = self._evidence(evidence_ref)
            if "date_window" in data and data["date_window"] != ref:
                raise CrawlError("Existing date-window evidence cannot be replaced or extended")
            _window_candidates(manifest)
            data["date_window"] = ref
            self._save(data, before)
        return self.status()

    def append_date_addenda(self, evidence_refs, *, human_confirmed=False):
        """Pin new evidence without replacing the old manifest, cursor or blocks."""
        if human_confirmed is not True:
            raise CrawlError("Newer days require explicit human confirmation")
        if not isinstance(evidence_refs, (tuple, list)) or not 1 <= len(evidence_refs) <= 2:
            raise CrawlError("One or two newer day references are required")
        with self._mutation():
            data, candidates = self._load(); before = deepcopy(data)
            if data["phase"] != "paused" or candidates is None:
                raise CrawlError("Date addenda require the paused original queue and its existing manifest")
            entries = list(data.get("date_window_addenda", []))
            for evidence_ref in evidence_refs:
                manifest, ref = self._evidence(evidence_ref)
                if ref in entries:
                    continue
                extra = _date_addendum_candidates(manifest)
                if set(extra) & candidates.keys() or len(entries) >= 16:
                    raise CrawlError("Date addenda cannot replace or collide with an existing article")
                entries.append(ref)
                candidates.update(extra)
            data["date_window_addenda"] = entries
            self._save(data, before)
        return self.status()

    def window_article_allowed(self, article_id):
        """Read-only date/identity check, not a substitute for the other queue gates."""
        _, candidates = self._load()
        if candidates is None:
            return True
        if not isinstance(article_id, str) or article_id not in candidates:
            return False
        if not _local(self.catalog_path).exists():
            return False
        catalog, _ = _read_json(self.catalog_path)
        if not isinstance(catalog.get("articles"), dict):
            raise CrawlError("Invalid catalog input; do not treat it as an empty site")
        return _window_catalog_match(article_id, catalog["articles"].get(article_id), candidates)

    def status(self):
        """Read-only local derivation. Saved/open Journal operations are never removed."""
        data, window_candidates = self._load()
        catalog, catalog_raw = ({"articles": {}, "listings": {}, "coverage": {}}, b"")
        if _local(self.catalog_path).exists():
            catalog, catalog_raw = _read_json(self.catalog_path)
        if not isinstance(catalog.get("articles"), dict) or not isinstance(catalog.get("listings"), dict):
            raise CrawlError("Invalid catalog input; do not treat it as an empty site")
        journal_before = self.journal.status()
        operations = self.journal.operations()
        journal_after = self.journal.status()
        if journal_before.get("revision") != journal_after.get("revision"):
            raise CrawlError("Journal changed during read-only planning; take a fresh snapshot")

        listings = {}
        roots = [HOME]
        for url, facts in catalog["listings"].items():
            row = _listing(facts, url)
            listings[row["url"]] = row
            roots.append(row["url"])
        for url in data["listing_order"]:
            current = deepcopy(data["listings"][url])
            historical_edges = listings.get(url, {}).get("next_pages", [])
            current["next_pages"] = list(dict.fromkeys(current["next_pages"] + historical_edges))
            listings[url] = current
            roots.append(url)
        for url, pages in data["observed_edges"].items():
            if url not in listings:
                raise CrawlError("Pagination history lacks its observed source page")
            listings[url]["next_pages"] = list(dict.fromkeys(listings[url]["next_pages"] + pages))
        # Depth-first href traversal: page2's observed page3 is visited before
        # a more distant numbered href also observed on the homepage. No math.
        stack, seen, observed_order, frontier = list(reversed(list(dict.fromkeys(roots)))), set(), [], []
        while stack:
            url = stack.pop()
            if url in seen:
                continue
            seen.add(url)
            if url not in listings:
                frontier.append(url); continue
            observed_order.append(url)
            stack.extend(reversed(listings[url]["next_pages"]))

        articles = {}
        for url in observed_order:
            for row in listings[url]["articles"]:
                existing = articles.setdefault(row["article_id"], {})
                # The first observed order is retained. Date fields are actual
                # observations, not replaced with a rank or observation time.
                for key, value in row.items():
                    if key not in existing:
                        existing[key] = deepcopy(value)
        for aid in data["discovered_order"]:
            _merge_article_metadata(articles.setdefault(aid, {}), data["discovered_articles"][aid])
        for aid, record in catalog["articles"].items():
            projection = _article(record)
            if aid != projection["article_id"]:
                raise CrawlError("Catalog article key/identity mismatch")
            _merge_article_metadata(articles.setdefault(aid, {}), {**record, **projection})
        ordered = sorted(articles.values(), key=lambda a: _ordering(a)[0])
        rank = {a["article_id"]: n for n, a in enumerate(ordered)}
        date_info = {a["article_id"]: _ordering(a)[1] for a in ordered}
        share_refs, article_shares = {}, {}
        for article in ordered:
            aid = article["article_id"]
            shares = article.get("shares", [])
            if not isinstance(shares, list):
                raise CrawlError("Invalid catalog share list")
            article_shares[aid] = []
            for share in shares:
                if not isinstance(share, dict):
                    raise CrawlError("Invalid catalog share projection")
                url = canonical_share(share.get("url"))
                if url not in article_shares[aid]:
                    article_shares[aid].append(url)
                refs = share_refs.setdefault(url, [])
                if aid not in refs:
                    refs.append(aid)

        saved, open_ops, ops_by_share = [], [], {}
        for operation in operations:
            view = _op_view(operation)
            (saved if view.get("state") in SAVED else open_ops).append(view)
            ops_by_share.setdefault(view["share_url"], []).append(view)
        pending, deferred = [], []

        def offer(item):
            restrictions = self._blocked(item, data)
            if restrictions:
                deferred.append({**item, "reason": "scope_blocked", "blocks": restrictions})
            else:
                pending.append(item)

        for operation in open_ops:
            refs = operation.get("article_ids", [])
            refs = list(dict.fromkeys(refs + share_refs.get(operation["share_url"], [])))
            item = {**operation, "kind": "reconcile", "scope": "share:" + operation["share_url"],
                "article_ids": refs, "may_submit": False, "action": "inspect_existing_operation_no_replay"}
            if operation.get("state") == "blocked":
                deferred.append({**item, "reason": "journal_operation_blocked"})
            else:
                offer(item)

        emitted = set()
        for article in ordered:
            aid = article["article_id"]
            scope = "article:" + aid
            if scope in data["invalid"]:
                continue
            base = {"article_id": aid, "url": article["url"], "title": article["title"], **date_info[aid]}
            access = article.get("access_state", "not_checked")
            legacy = bool(article.get("legacy_state"))
            if not article_shares[aid]:
                item = {**base, "kind": "article", "scope": scope, "access_state": access}
                if article.get("transfer_status") in SAVED:
                    offer({**item, "kind": "reconcile", "action": "reconcile_catalog_saved_evidence", "may_submit": False})
                elif access in GATES or legacy:
                    deferred.append({**item, "reason": "legacy_needs_reconcile" if legacy else access, "phase": "needs_human"})
                elif access in {"not_checked", "temporary_failure", "fetch_failed", "timeout"} or article.get("pay_entries"):
                    offer({**item, "action": "inspect_authorized_entry" if article.get("pay_entries") else "collect_article"})
                else:
                    deferred.append({**item, "reason": "no_direct_entry_observed", "phase": "needs_human"})
            for url in article_shares[aid]:
                if url in emitted:
                    continue
                emitted.add(url)
                if url in ops_by_share or "share:" + url in data["invalid"]:
                    continue
                refs = share_refs[url]
                candidates = [articles[x] for x in refs if "article:" + x not in data["invalid"]]
                eligible = [a for a in candidates if a.get("access_state") not in GATES
                    and not a.get("legacy_state") and a.get("transfer_status") not in SAVED]
                item = {"kind": "transfer", "scope": "share:" + url, "share_url": url, "article_ids": refs,
                    "article_id": aid, "title": article["title"], "action": "inspect_share_then_journal_prepare",
                    "claim_required": True, **date_info[aid]}
                if any(a.get("transfer_status") in SAVED for a in candidates):
                    offer({**item, "kind": "reconcile", "action": "reconcile_catalog_saved_evidence", "may_submit": False})
                elif eligible:
                    first = eligible[0]
                    item["article_id"] = first["article_id"]
                    item["classification"] = deepcopy(first.get("classification"))
                    offer(item)
                elif candidates:
                    deferred.append({**item, "reason": "access_or_legacy_reconciliation_required", "phase": "needs_human"})
        # Existing operations come first for evidence-only reconciliation; dated
        # resources then descend, with stable undated listing order.
        pending.sort(key=lambda item: (0 if item["kind"] == "reconcile" else 1,
            min((rank.get(aid, len(rank)) for aid in item.get("article_ids", [item.get("article_id")])), default=len(rank))))
        for url in frontier:
            offer({"kind": "listing", "scope": "listing:" + url, "url": url, "action": "observe_listing",
                "pagination_basis": "seed_homepage" if url == HOME else "observed_next_pages_href"})

        unresolved_count = len(deferred) + len(open_ops)
        date_window = None
        if window_candidates is not None:
            first, old_last = _DATE_WINDOW_DATES
            last = max(old_last, max(window_candidates.values(), default=old_last))
            date_window = {"from": first, "through": last, "minimum_free_bytes": 0, "candidate_count": len(window_candidates)}
            allowed = {aid for aid in window_candidates
                if _window_catalog_match(aid, catalog["articles"].get(aid), window_candidates)}

            def in_window(item):
                # Do not pick historical work through a share alias. Keep the
                # original guards/dedup above and require every associated ID.
                refs = item.get("article_ids", [item.get("article_id")])
                return item["kind"] != "listing" and bool(refs) and all(aid in allowed for aid in refs)

            pending = [item for item in pending if in_window(item)]
            deferred = [item for item in deferred if in_window(item)]
            if data.get("date_window_addenda"):
                order = {aid: n for n, aid in enumerate(window_candidates)}
                def published_rank(item):
                    refs = item.get("article_ids", [item.get("article_id")])
                    newest = max(window_candidates[aid] for aid in refs)
                    return (0 if item["kind"] == "reconcile" else 1, -date.fromisoformat(newest).toordinal(),
                            min(order[aid] for aid in refs if window_candidates[aid] == newest))
                pending.sort(key=published_rank)

        external_lease = _local(self.lock_path).exists() and self._held is None
        if self._held is not None:
            self._check_lease()
        phase = data["phase"]
        if phase == "active" and (external_lease or not pending and deferred):
            phase = "needs_human"
        counts = {kind: sum(item["kind"] == kind for item in pending) for kind in sorted(KINDS)}
        continuation = {"objective": "whole_authorized_site_latest_to_history",
            "pagination_basis": "observed_next_pages_href_only", "next_listing_url": frontier[0] if frontier else None,
            "pending_listing_urls": frontier, "observed_listing_urls": observed_order,
            "observed_frontier_exhausted": not frontier, "whole_site_complete": False,
            "has_actionable_work": bool(pending) and phase == "active",
            "unresolved_count": unresolved_count, "default_batch_limit": 4, "max_batch_limit": 4,
            "coverage_note": "Observed frontier exhaustion is not proof of whole-site/resource completion",
            "date_ordering": "newest_explicit_published_at_or_updated_at; ties keep observed order",
            "missing_date_ordering": "observed_listing_order_only_no_inferred_date"}
        return deepcopy({"schema": SCHEMA, "revision": data["revision"], "phase": phase, "control_phase": data["phase"],
            "phase_reason": data["phase_reason"], "pending": pending, "pending_by_kind": counts,
            "article_order": [a["article_id"] for a in ordered], "article_count": len(articles),
            "unique_share_count": len(share_refs), "saved_operations": saved, "open_operations": open_ops,
            "deferred": deferred, "invalid": list(data["invalid"].values()), "blocks": list(data["blocks"].values()),
            "last_attempts": data["last_attempts"], "continuation": continuation, "date_window": date_window,
            "source_revisions": {"catalog_sha256": hashlib.sha256(catalog_raw).hexdigest() if catalog_raw else None,
                "journal_revision": journal_after.get("revision")},
            "lease": {"held": _local(self.lock_path).exists(), "owned_by_this_instance": self._held is not None,
                "requires_manual_reconciliation": external_lease, "auto_expiry": False},
            "cloud_submissions": 0, "claim_performed": False})

    def next_batch(self, limit=4, *, kinds=None):
        """Select (never claim), with a HARD total bound of four, one listing max."""
        if type(limit) is not int or not 1 <= limit <= 4:
            raise CrawlError("A bounded batch limit of 1..4 is required")
        if kinds is None:
            selected_kinds = KINDS
        else:
            if isinstance(kinds, str):
                kinds = [kinds]
            selected_kinds = frozenset(kinds)
            if not selected_kinds or not selected_kinds <= KINDS:
                raise CrawlError("Unknown or empty queue kind selection")
        view = self.status()
        items = []
        # Default planning finishes observed resource work before proposing the
        # next historical listing. Explicit kinds="listing" is an inspection
        # override, not the default automatic continuation policy.
        resource_work = kinds is None and any(x["kind"] != "listing" for x in view["pending"])
        if view["phase"] == "active":
            for item in view["pending"]:
                if resource_work and item["kind"] == "listing":
                    continue
                if item["kind"] not in selected_kinds or item["kind"] == "listing" and any(x["kind"] == "listing" for x in items):
                    continue
                items.append(item)
                if len(items) == limit:
                    break
        return {"phase": view["phase"], "items": items, "continuation": view["continuation"], "date_window": view["date_window"],
            "claim_performed": False, "cloud_submissions": 0, "revision": view["revision"]}

    def record_listing(self, facts, *, evidence_ref=None):
        """Record an observed page projection, never fetch or update the catalog.

        facts.url/articles/next_pages are required on success. Explicit gate or
        temporary_failure access_state records an attempt, NOT a visited page.
        A successful observation never clears a prior pause or scope block.
        """
        if not isinstance(facts, dict):
            raise CrawlError("Listing facts must be an object")
        url = _listing_url(facts.get("url"))
        access = facts.get("access_state", "public")
        failure = access in GATES or access in {"temporary_failure", "fetch_failed", "timeout"}
        projection = None if failure else _listing(facts)
        evidence = self._evidence(evidence_ref)[1] if evidence_ref is not None else None
        with self._mutation():
            data, _ = self._load(); before = deepcopy(data)
            if failure:
                record = {"scope": "listing:" + url, "outcome": access,
                    "reason": _text(facts.get("reason", access), "listing failure reason"), "evidence": evidence}
                previous = data["last_attempts"].get(url)
                if previous is None or {k: v for k, v in previous.items() if k != "recorded_at"} != record:
                    data["last_attempts"][url] = {**record, "recorded_at": _now()}
                if access in GATES:
                    self._put_block(data, "listing:" + url, record["reason"], "needs_human", evidence)
            else:
                if evidence is not None:
                    projection["evidence"] = evidence
                if url not in data["listings"]:
                    data["listing_order"].append(url)
                data["listings"][url] = projection
                for row in projection["articles"]:
                    aid = row["article_id"]
                    if aid not in data["discovered_articles"]:
                        data["discovered_order"].append(aid)
                    _merge_article_metadata(data["discovered_articles"].setdefault(aid, {}), row)
                edges = data["observed_edges"].setdefault(url, [])
                edges.extend(href for href in projection["next_pages"] if href not in edges)
            self._save(data, before)
        return self.status()

    @staticmethod
    def _validate_invalid(scope, evidence_type, obj):
        if evidence_type not in INVALID_EVIDENCE or obj.get("kind") != evidence_type or obj.get("confirmed") is not True:
            raise CrawlError("Explicit confirmed removal/expiry/cancellation evidence is required")
        if evidence_type == "content_removed":
            if not scope.startswith("article:"):
                raise CrawlError("Content removal evidence is article-specific")
            bindings = [obj["scope"] == scope] if "scope" in obj else []
            if "article_id" in obj:
                bindings.append(obj["article_id"] == scope[8:])
        else:
            if not scope.startswith("share:"):
                raise CrawlError("Share invalidity evidence is share-specific")
            bindings = [obj["scope"] == scope] if "scope" in obj else []
            if obj.get("share_url") is not None:
                bindings.append(canonical_share(obj["share_url"]) == scope[6:])
        if not bindings or not all(bindings):
            raise CrawlError("Invalidity evidence is not bound to this exact scope")

    def mark_invalid(self, scope, evidence_type, evidence_ref):
        """Only evidence-backed share_expired/share_cancelled/content_removed.

        Does not delete or downgrade saved/open operations, even for that share.
        """
        scope = _scope(scope)
        obj, ref = self._evidence(evidence_ref)
        self._validate_invalid(scope, evidence_type, obj)
        with self._mutation():
            data, _ = self._load(); before = deepcopy(data)
            old = data["invalid"].get(scope)
            record = {"scope": scope, "evidence_type": evidence_type, "evidence": ref}
            if old is not None and {k: v for k, v in old.items() if k != "recorded_at"} != record:
                raise CrawlError("Existing invalidity evidence is immutable; manual reconciliation required")
            if old is None:
                data["invalid"][scope] = {**record, "recorded_at": _now()}
            self._save(data, before)
        return self.status()

    @staticmethod
    def _put_block(data, scope, reason, phase, evidence):
        existing = data["blocks"].get(scope)
        if existing is not None and not existing["resolved"]:
            return  # First restriction is preserved, never silently downgraded.
        data["blocks"][scope] = {"scope": scope, "reason": reason, "phase": phase,
            "evidence": evidence, "resolved": False, "recorded_at": _now()}

    def block_scope(self, scope, reason, *, evidence_ref=None, phase="needs_human"):
        scope = _scope(scope)
        reason = _text(reason, "blocking reason")
        if phase not in PHASES:
            raise CrawlError("Invalid blocking phase")
        evidence = self._evidence(evidence_ref)[1] if evidence_ref is not None else None
        with self._mutation():
            data, _ = self._load(); before = deepcopy(data)
            self._put_block(data, scope, reason, phase, evidence)
            if scope == "site" and data["phase"] == "active" and phase != "active":
                data.update(phase=phase, phase_reason=reason)
            self._save(data, before)
        return self.status()

    def unblock_scope(self, scope, reason, *, human_confirmed=False, evidence_ref=None):
        """Explicit human recovery only; never called by listing or selection."""
        scope = _scope(scope)
        reason = _text(reason, "explicit recovery reason")
        if human_confirmed is not True:
            raise CrawlError("Scope recovery requires explicit human confirmation")
        evidence = self._evidence(evidence_ref)[1] if evidence_ref is not None else None
        with self._mutation():
            data, _ = self._load(); before = deepcopy(data)
            if scope not in data["blocks"]:
                raise CrawlError("Unknown blocked scope")
            block = data["blocks"][scope]
            if not block["resolved"]:
                block.update(resolved=True, resolution={"reason": reason, "human_confirmed": True,
                    "evidence": evidence, "recorded_at": _now()})
            self._save(data, before)
        return self.status()

    def recover_manual_verification(self, scope, episode, evidence_ref, *, human_confirmed=False):
        """Release only the checked verification stop and leave prior attempts untouched."""
        from .manual_verification import hold_id, timestamp, verification_blocks

        if human_confirmed is not True:
            raise CrawlError("Manual verification confirmation is required")
        with self._mutation():
            data, _ = self._load()
            block = next((row for row in verification_blocks(data) if row["scope"] == scope), None)
            if block is None or hold_id(block) != episode:
                raise CrawlError("The verification stop changed; read the current stop")
            proof, ref = self._evidence(evidence_ref)
            if not isinstance(proof, dict):
                raise CrawlError("Malformed verification readback")
            observed = timestamp(proof.get("observed_at"))
            blocked_at = timestamp(block.get("recorded_at"))
            if (proof.get("kind") != "manual_verification_recovery" or proof.get("scope") != scope
                    or proof.get("episode") != episode or proof.get("official_ui_readback") is not True
                    or proof.get("original_session_verified") is not True or proof.get("account_matches") is not True
                    or proof.get("original_page_accessible") is not True or proof.get("challenge_present") is not False
                    or type(proof.get("cloud_submissions")) is not int or proof["cloud_submissions"] != 0
                    or (scope == "platform:baidu" and proof.get("target_verified") is not True)
                    or observed is None or blocked_at is None or observed < blocked_at
                    or not 0 <= (timestamp(_now()) - observed).total_seconds() <= 300):
                raise CrawlError("A fresh same-session verification readback is required")
            before = deepcopy(data)
            block.update(resolved=True, resolution={"reason": "official manual verification readback",
                         "human_confirmed": True, "evidence": ref, "recorded_at": _now()})
            self._save(data, before)
        return {"scope": scope, "revision": data["revision"], "verification_recovered": True,
                "queue_phase": data["phase"], "cloud_submissions": 0}

    def set_phase(self, phase, reason, *, human_confirmed=False):
        """active/paused/needs_human; leaving a restriction requires a human.

        This does not clear scope restrictions, invalid evidence, or journal ops.
        """
        if phase not in PHASES:
            raise CrawlError("Invalid crawl control phase")
        reason = _text(reason, "phase reason")
        with self._mutation():
            data, _ = self._load(); before = deepcopy(data)
            if phase == "active" and data["phase"] != "active" and human_confirmed is not True:
                raise CrawlError("Resuming restricted control requires explicit human confirmation")
            if data["phase"] != phase or data["phase_reason"] != reason:
                data["phase_history"].append({"from": data["phase"], "to": phase, "reason": reason,
                    "human_confirmed": human_confirmed is True, "recorded_at": _now()})
                data.update(phase=phase, phase_reason=reason)
            self._save(data, before)
        return self.status()
