"""Local candidate retrieval and display-only course relationships."""

from __future__ import annotations

from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date
from hashlib import sha256
from math import log1p
import re
from unicodedata import normalize


RELATION_TYPES = frozenset({"same_version", "different_version", "related", "contains", "uncertain"})
FILTER_PLATFORM_ROLES = frozenset({"use", "acquire", "receive"})


def article_identity(row):
    return (row["site_id"], str(row["article_id"]))


def _analysis(row):
    return row.get("analysis") or {}


def _text(value):
    return re.sub(r"[\W_]+", "", normalize("NFKC", str(value or "")).casefold())


def _key_hash(prefix, identity):
    return prefix + sha256("\0".join(identity).encode("utf-8")).hexdigest()


def _task_ref(row):
    return {**dict(zip(("site_id", "article_id"), article_identity(row))), "input_hash": row["input_hash"]}


def _grams(text, cap):
    terms = {text[i:i+n] for n in (2, 3) for i in range(len(text) - n + 1)}
    if len(terms) <= cap:
        return terms
    ordered = sorted(terms)
    return {ordered[position * len(ordered) // cap] for position in range(cap)}


def _passage_grams(paragraphs):
    grams = set()
    relevant = []
    for paragraph in paragraphs:
        text = _text(paragraph)
        if len(text) >= 12:
            relevant.append(text)
    if not relevant:
        return grams, None
    # Include evenly spaced sections, including the end of long introductions.
    count = min(len(relevant), 24)
    positions = [i * (len(relevant) - 1) // max(count - 1, 1) for i in range(count)]
    for i in positions:
        text = relevant[i]
        sampled = text if len(text) <= 768 else text[:256] + " " + text[len(text)//2-128:len(text)//2+128] + " " + text[-256:]
        grams.update(_grams(sampled, max(24, 384 // count)))
    exact = None
    if sum(map(len, relevant)) >= 96 and len(grams) >= 44:
        exact = sha256("\n".join(relevant).encode("utf-8")).hexdigest()
    return grams, exact


def candidate_pairs(inputs, *, keys=None, limit=30):
    """Return bounded candidate edges for target sources; scores only schedule reading."""
    if type(limit) is not int or not 1 <= limit <= 120:
        raise ValueError("候选数量应为 1 至 120")
    rows = [row for row in inputs if row.get("input_hash")]
    ids = [article_identity(row) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("候选输入的来源文章身份重复")
    targets = set(keys) if keys is not None else set(ids)
    postings = defaultdict(list)
    signals = []
    for number, row in enumerate(rows):
        info = _analysis(row)
        title = _text(row.get("title"))
        author, series = _text(info.get("author")), _text(info.get("series"))
        grams, exact = _passage_grams(row.get("paragraphs") or ())
        fields = {
            "title": _grams(title[:1000], 320),
            "introduction": grams,
            "series": {series} if len(series) >= 4 else set(),
            "author": {author} if len(author) >= 2 else set(),
            "exact_text": {exact} if exact else set(),
        }
        signals.append(fields)
        for name, terms in fields.items():
            for term in terms:
                postings[name, term].append(number)

    weights = {"title": 2.4, "introduction": 2.7, "series": 4.1, "author": 1.0, "exact_text": 4.5}
    max_df = min(240, max(30, len(rows) // 32))
    ordered_edges = {}
    truncated = 0
    for number, fields in enumerate(signals):
        if ids[number] not in targets:
            continue
        overlaps = defaultdict(Counter)
        for name, terms in fields.items():
            for term in sorted(terms):
                matches = postings[name, term]
                if len(matches) < 2 or len(matches) > max_df:
                    continue
                benefit = log1p(len(rows) / len(matches))
                for other in matches:
                    if other != number:
                        overlaps[other][name] += benefit
        ranked = []
        for other, channels in overlaps.items():
            points = 0.0
            matched_channels = []
            for name, total in channels.items():
                if name in ("series", "author", "exact_text"):
                    similarity = 1.0
                else:
                    size = len(fields[name]) + len(signals[other][name])
                    similarity = min(1.0, 2 * total / (size * max(1, log1p(len(rows) / 2))))
                    if similarity < (0.09 if name == "title" else 0.17):
                        continue
                points += weights[name] * similarity
                matched_channels.append(name)
            if points and set(matched_channels) != {"author"}:
                ranked.append((round(points * 1000), ids[other], other, sorted(matched_channels)))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        truncated += max(0, len(ranked) - limit)
        for score, _, other, channels in ranked[:limit]:
            a, b = sorted(((ids[number], number), (ids[other], other)))
            key = (a[0], b[0])
            edge = {"left": _task_ref(rows[a[1]]), "right": _task_ref(rows[b[1]]),
                    "candidate_score": score, "candidate_channels": channels}
            if key not in ordered_edges or score > ordered_edges[key]["candidate_score"]:
                ordered_edges[key] = edge
    return sorted(ordered_edges.values(), key=lambda r: (-r["candidate_score"], article_identity(r["left"]),
                                                          article_identity(r["right"]))), truncated


_CN_NUMBER = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUMERIC_EDITION = re.compile(r"(?:v(?:ersion)?\s*)?(\d{1,4}(?:\.\d{1,4}){0,3})(?:\s*版)?", re.I)
_CN_EDITION = re.compile(r"第([零一二三四五六七八九十]|\d{1,4}|[一二三四五六七八九]?十[一二三四五六七八九]?)版")


def _edition(value):
    if not isinstance(value, str):
        return None
    text = normalize("NFKC", value).strip()
    if match := re.fullmatch(r"((?:19|20)\d{2})(?:\s*版)?", text):
        return "year", (int(match[1]),)
    if match := _NUMERIC_EDITION.fullmatch(text):
        digits = tuple(map(int, match[1].split(".")))
        return "number", digits + (0,) * (4 - len(digits))
    if match := _CN_EDITION.fullmatch(text):
        number = match[1]
        if number.isdecimal():
            return "number", (int(number), 0, 0, 0)
        if "十" in number:
            hundreds, _, remainder = number.partition("十")
            return "number", (10 * _CN_NUMBER.get(hundreds, 1) + _CN_NUMBER.get(remainder, 0), 0, 0, 0)
        return "number", (_CN_NUMBER[number], 0, 0, 0)
    return None


def _date_span(value, precision):
    formats = {"year": r"\d{4}", "month": r"\d{4}-\d{2}", "day": r"\d{4}-\d{2}-\d{2}"}
    if not isinstance(value, str) or precision not in formats or not re.fullmatch(formats[precision], value):
        return None
    try:
        if precision == "day":
            first = date.fromisoformat(value)
            return first, first
        if precision == "year":
            year = int(value)
            return date(year, 1, 1), date(year, 12, 31)
        year, month = map(int, value.split("-"))
        return date(year, month, 1), date(year, month, monthrange(year, month)[1])
    except ValueError:
        return None


def _version_date(row):
    spans = [
        span for item in _analysis(row).get("dates") or ()
        if item.get("meaning") == "version" and (span := _date_span(item.get("value"), item.get("precision")))
    ]
    if not spans:
        return None, False
    first, last = max(left for left, _ in spans), min(right for _, right in spans)
    if first > last:
        return None, True
    return (first, last), False


def _date_compare(left, right):
    if not left or not right:
        return 0
    if left[1] < right[0]:
        return -1
    if left[0] > right[1]:
        return 1
    return 0


def version_compare(left, right):
    """Return -1/1 only when independent edition/date evidence establishes the order."""
    left_edition, right_edition = _edition(_analysis(left).get("edition")), _edition(_analysis(right).get("edition"))
    left_date, left_conflict = _version_date(left)
    right_date, right_conflict = _version_date(right)
    if left_conflict or right_conflict:
        return 0
    date_order = _date_compare(left_date, right_date)
    edition_order = _edition_compare(left_edition, right_edition)
    if date_order and edition_order and date_order != edition_order:
        return 0
    return date_order or edition_order


def _edition_compare(left, right):
    if not left or not right or left[0] != right[0]:
        return 0
    return (left[1] > right[1]) - (left[1] < right[1])


def _platforms(row):
    values = _analysis(row).get("platforms") or ()
    return frozenset(item["id"] for item in values
                     if item.get("role") in FILTER_PLATFORM_ROLES and item.get("id"))


def _platform_conflict(left, right):
    first, second = _platforms(left), _platforms(right)
    return bool(first and second and first != second)


def _known_conflict(left, right, *, same_version):
    first, second = _analysis(left), _analysis(right)
    for field in ("author", "scope"):
        a, b = _text(first.get(field)), _text(second.get(field))
        if a and b and "unknown" not in (a, b) and a != b:
            return True
    if same_version:
        a, b = _edition(first.get("edition")), _edition(second.get("edition"))
        ldate, lconflict = _version_date(left)
        rdate, rconflict = _version_date(right)
        return bool(_platform_conflict(left, right) or _edition_compare(a, b)
                    or lconflict or rconflict or _date_compare(ldate, rdate))
    return False


def _version_label(row):
    info = _analysis(row)
    if info.get("edition"):
        return str(info["edition"])
    version_date, conflict = _version_date(row)
    if version_date and not conflict:
        texts = [(item["precision"], item["value"]) for item in info.get("dates") or ()
                 if item.get("meaning") == "version" and _date_span(item.get("value"), item.get("precision"))]
        return max(texts, key=lambda x: len(x[1]))[1] if texts else None
    return None


class _Groups:
    """Merge components with a stable anchor from the first confirmed join."""
    def __init__(self, members):
        self.anchor = {key: key for key in members}
        self.members = {key: {key} for key in members}
        self.first = {}

    def join(self, a, b, order):
        a, b = self.anchor[a], self.anchor[b]
        if a == b:
            return
        winner, removed = sorted((a, b), key=lambda key: (self.first.get(key, order), key))
        self.first[winner] = min(self.first.get(winner, order), self.first.pop(removed, order))
        moved = self.members.pop(removed)
        self.members[winner].update(moved)
        self.anchor.update((key, winner) for key in moved)


class CatalogRelations:
    """A snapshot of confirmed relationships. Legacy resource bindings are left in place."""
    def __init__(self, rows, relationships):
        self.rows = {}
        for row in rows:
            key = article_identity(row)
            if key in self.rows:
                raise ValueError("目录来源文章身份重复")
            self.rows[key] = row
        id_counts = Counter(row.get("id") for row in self.rows.values())
        self.ambiguous = {key for key, row in self.rows.items()
                          if row.get("action_pending") is True or id_counts[row.get("id")] > 1}
        self.stale = 0
        current = {}
        for order, relation in enumerate(relationships):
            if relation.get("relation") not in RELATION_TYPES and not relation.get("revoked"):
                continue
            left_ref, right_ref = relation.get("left", {}), relation.get("right", {})
            if not left_ref.get("site_id") or not left_ref.get("article_id") or not right_ref.get("site_id") or not right_ref.get("article_id"):
                continue
            l, r = article_identity(left_ref), article_identity(right_ref)
            if l == r:
                continue
            key = tuple(sorted((l, r)))
            current[key] = (order, relation)
        self.versions = _Groups(self.rows)
        self.series = _Groups(self.rows)
        self.newer = defaultdict(set)
        self.older = defaultdict(set)
        self.active = Counter()
        approved_same = set()
        version_edges = []
        unrelated = defaultdict(set)
        self.conflicts = 0
        self.pending_order = 0

        for pair, (order, relation) in current.items():
            if relation.get("revoked"):
                continue
            l, r = article_identity(relation["left"]), article_identity(relation["right"])
            if any(key not in self.rows or not relation[side].get("input_hash")
                   or relation[side]["input_hash"] != self.rows[key].get("input_hash")
                   for key, side in ((l, "left"), (r, "right"))):
                self.stale += 1
                continue
            kind = relation["relation"]
            if (kind in ("same_version", "different_version")
                    and _known_conflict(self.rows[l], self.rows[r], same_version=kind == "same_version")):
                self.conflicts += 1
                continue
            self.active[kind] += 1
            if kind == "same_version":
                approved_same.add(pair)
            elif kind == "different_version":
                version_edges.append((order, l, r, relation))
            elif kind in ("related", "contains"):
                unrelated[l].add(r)
                unrelated[r].add(l)

        for a, b in sorted(approved_same, key=lambda p: current[p][0]):
            left = self.versions.members[self.versions.anchor[a]]
            right = self.versions.members[self.versions.anchor[b]]
            if left == right:
                continue
            if any(tuple(sorted((l, r))) not in approved_same for l in left for r in right):
                self.conflicts += 1
                continue
            self.versions.join(a, b, current[a, b][0])

        for key, group in self.versions.members.items():
            for member in sorted(group - {key}):
                self.series.join(key, member, self.versions.first[key])
        for order, l, r, relation in sorted(version_edges):
            a, b = self.versions.anchor[l], self.versions.anchor[r]
            if a == b:
                self.conflicts += 1
                continue
            left = self.series.members[self.series.anchor[l]]
            right = self.series.members[self.series.anchor[r]]
            if left != right and any(unrelated[key] & right for key in left if key in unrelated):
                self.conflicts += 1
                continue
            self.series.join(l, r, order)
            if relation.get("same_scope") is not True or _platform_conflict(self.rows[l], self.rows[r]):
                continue
            comparison = version_compare(self.rows[l], self.rows[r])
            direction = "left" if comparison > 0 else "right" if comparison < 0 else None
            if direction is None or relation.get("newer") not in (None, direction):
                self.pending_order += 1
                continue
            new, old = (a, b) if direction == "left" else (b, a)
            self.newer[old].add(new)
            self.older[new].add(old)
        self.family_versions = {family: {self.versions.anchor[key] for key in members}
                                for family, members in self.series.members.items() if len(members) > 1}
        self.display_ids = {key: _key_hash("cv_", key) for key in self.versions.members}
        self.series_ids = {key: _key_hash("cs_", key) for key in self.series.members}

    def _ref(self, key):
        row = self.rows[key]
        return {**{field: row.get(field) for field in ("id", "article_key", "article_revision", "title",
                                                       "site_id", "article_id", "site_label", "published_at")},
                "display_id": self.display_ids[self.versions.anchor[key]],
                "version_label": _version_label(row),
                "action_pending": key in self.ambiguous}

    def _rep(self, members, preferred=()):
        if len(members) == 1:
            return next(iter(members))

        def score(key):
            row = self.rows[key]
            paragraphs = row.get("paragraphs") or ()
            return (key in preferred, row.get("coverage") in ("complete", "full"), bool(paragraphs),
                    sum(map(len, paragraphs)), bool(row.get("excerpt")), bool(row.get("cover")),
                    row.get("published_at") or "", key)
        return max(members, key=score)

    def _display(self, key, *, groups=None):
        row = self.rows[key]
        version = self.versions.anchor[key]
        series = self.series.anchor[key]
        candidates = self.versions.members if groups is None else groups
        group = candidates[version]
        neighbors = (self.family_versions.get(series, frozenset()) - {version}) & candidates.keys()
        reps = {remote: self._ref(self._rep(candidates[remote])) for remote in neighbors}
        others = [reps[remote] for remote in self._ordered(neighbors, key=lambda remote: (
            reps[remote].get("published_at") or "", reps[remote].get("article_key") or ""))]
        result = {
            **row,
            "display_id": self.display_ids[version],
            "series_id": self.series_ids[series],
            "version_label": _version_label(row),
            "source_count": len(group),
            "version_count": 1 + len(others),
            "other_sources": [self._ref(other) for other in sorted(group - {key})],
            "other_versions": others,
            "action_pending": key in self.ambiguous,
        }
        if groups is not None:
            siblings = neighbors | {version}
            result["_matched_versions"] = tuple(self.display_ids[member] for member in siblings)
            result["_matched_sources"] = tuple(self.rows[source].get("article_key") or source
                                                for member in siblings for source in groups[member])
        return result

    def details(self, row):
        return self._display(article_identity(row))

    def _layers(self, keys):
        counts = {key: len(self.newer.get(key, frozenset()) & keys) for key in keys}
        frontier = {key for key, count in counts.items() if not count}
        while frontier:
            yield frontier
            following = set()
            for key in frontier:
                for old in self.older.get(key, frozenset()) & keys:
                    counts[old] -= 1
                    if not counts[old]:
                        following.add(old)
            frontier = following

    def _ordered(self, keys, *, key):
        visited = set()
        for layer in self._layers(keys):
            yield from sorted(layer, key=key, reverse=True)
            visited.update(layer)
        yield from sorted(keys - visited, key=key, reverse=True)

    def _visible(self, groups, preferred=frozenset()):
        visible = set(groups)
        if not self.newer:
            return visible
        covered = set()
        related = (self.newer.keys() | self.older.keys()) & groups.keys()
        for layer in self._layers(related):
            # A hidden middle edition cannot hide an unrelated older course.
            for key in layer:
                if not (groups[key] & preferred) and self.newer.get(key, frozenset()) & (visible & covered):
                    visible.remove(key)
            covered.update(layer)
        return visible

    def _matched(self, matching_rows, keep_selected=()):
        groups = defaultdict(set)
        preferred = set()
        keep_selected = set(keep_selected)
        for row in matching_rows:
            key = article_identity(row)
            if key not in self.rows:
                continue
            groups[self.versions.anchor[key]].add(key)
            if row.get("id") in keep_selected:
                preferred.add(key)
        return groups, preferred

    def count(self, matching_rows):
        """Count displayed projects without constructing each card and its references."""
        groups, preferred = self._matched(matching_rows)
        return len(self._visible(groups, preferred))

    def project(self, matching_rows, *, keep_selected=()):
        """Choose representatives and older versions strictly within the caller's matched set."""
        groups, preferred = self._matched(matching_rows, keep_selected)
        visible = self._visible(groups, preferred)
        return [self._display(self._rep(group, preferred), groups=groups)
                for key, group in groups.items() if key in visible]

    def stats(self):
        displayed = len(self._visible(self.versions.members, set()))
        return {"active_relations": dict(self.active), "stale_relations": self.stale,
                "conflicting_relations": self.conflicts,
                "pending_version_order": self.pending_order,
                "pending_relations": self.stale + self.conflicts + self.active["uncertain"] + self.pending_order,
                "source_articles": len(self.rows), "content_versions": len(self.versions.members),
                "confirmed_series": sum(len(versions) > 1 for versions in self.family_versions.values()),
                "hidden_older_versions": len(self.versions.members) - displayed,
                "displayed_projects": displayed,
                "folded_sources": len(self.rows) - len(self.versions.members)}
