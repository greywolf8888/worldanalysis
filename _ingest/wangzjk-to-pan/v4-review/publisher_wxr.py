"""Preview a site-owner WXR export or import a verified download metadata field."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Mapping
from html import unescape
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit
from xml.etree.ElementTree import ParseError

from defusedxml.ElementTree import iterparse
from defusedxml.common import DefusedXmlException

from catalog_model import open_index
from member_queue import DEFAULT_NON_PROJECTS, article_refs, read_non_projects
from share_locations import (
    ARTICLE_ORIGIN, EXTRACTION_CODE, MAX_ACTIVE_BATCHES, MAX_EXPORT_ITEMS, OWNER_SCHEMA, PRIVATE_ROOT, SITE,
    normalize_baidu_share_url, parse_owner_export, protected_owner_exports, write_protected_owner_export,
)


WORKSPACE = Path(__file__).resolve().parents[3]
MAX_WXR_BYTES = 512 * 1024 * 1024
MAX_WXR_ITEMS = 300_000
MAX_FIELD_BYTES = 128 * 1024
MAX_DEPTH = 12
MAX_NODES = 1500
MIN_MATCHES = 2
META_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,79}\Z")
BAIDU_IN_FIELD = re.compile(
    r"(?<![0-9A-Za-z_/%?=&.-])https://pan\.baidu\.com/"
    r"(?:s/[A-Za-z0-9_-]{1,129}|share/init)(?=[?\s#<>'\"，。]|$)(?:\?[^<>\s\"'，。#]*)?"
)
CODE_FIELDS = frozenset({"pwd", "password", "code", "pass", "copy", "copy_text", "share_code", "extract_code"})
TEXT_CODE = re.compile(r"^\s*提取码[：:\s]*([A-Za-z0-9]{4})(?![A-Za-z0-9])")
WORDPRESS_URIS = tuple(f"{{{scheme}://wordpress.org/export/{version}/}}"
                       for scheme in ("http", "https") for version in ("1.0", "1.1", "1.2"))


def wp_name(tag):
    for namespace in WORDPRESS_URIS:
        if tag.startswith(namespace):
            return tag[len(namespace):]
    return None


class PhpArray:
    """Parse bounded PHP scalar arrays without loading PHP or accepting objects."""

    def __init__(self, raw: bytes):
        self.raw = raw
        self.offset = 0
        self.nodes = 0

    def expect(self, marker: bytes):
        if not self.raw.startswith(marker, self.offset):
            raise ValueError("Invalid serialized metadata")
        self.offset += len(marker)

    def until(self, separator: bytes, limit: int) -> bytes:
        end = self.raw.find(separator, self.offset, self.offset + limit + 1)
        if end < 0:
            raise ValueError("Invalid serialized metadata token")
        start = self.offset
        self.offset = end + 1
        return self.raw[start:end]

    def length(self) -> int:
        raw = self.until(b":", 8)
        if not raw.isdigit():
            raise ValueError("Invalid serialized metadata length")
        return int(raw)

    def read(self, depth=0):
        self.nodes += 1
        if depth > MAX_DEPTH or self.nodes > MAX_NODES:
            raise ValueError("Serialized metadata exceeds structure limit")
        token = self.raw[self.offset:self.offset + 1]
        self.expect(token)
        if token == b"N":
            self.expect(b";")
            return None
        self.expect(b":")
        if token == b"s":
            size = self.length()
            self.expect(b'"')
            start = self.offset
            self.offset += size
            self.expect(b'";')
            return self.raw[start:start + size].decode("utf-8")
        if token == b"a":
            size = self.length()
            if size > MAX_NODES:
                raise ValueError("Serialized metadata exceeds element limit")
            self.expect(b"{")
            values = {}
            for _ in range(size):
                key = self.read(depth + 1)
                if type(key) not in (int, str) or key in values:
                    raise ValueError("Invalid serialized metadata key")
                values[key] = self.read(depth + 1)
            self.expect(b"}")
            return values
        if token in (b"b", b"i", b"d"):
            raw = self.until(b";", 30)
            if token == b"b" and raw in (b"0", b"1"):
                return raw == b"1"
            if token == b"i" and re.fullmatch(rb"-?[0-9]+", raw):
                return int(raw)
            if token == b"d" and re.fullmatch(rb"-?[0-9.]+(?:[eE][+-]?[0-9]+)?", raw):
                return None
        raise ValueError("Unsupported serialized metadata type")


def json_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate metadata key")
    return result


def decode_field(raw: str):
    value = raw.strip()
    size = len(value.encode("utf-8"))
    if not 0 < size <= MAX_FIELD_BYTES:
        raise ValueError("Invalid metadata size")
    if value.startswith(("{", "[", '"')):
        return json.loads(value, object_pairs_hook=json_object)
    if re.match(r"^(?:[a-zA-Z]:|N;)", value) and not value.startswith(("https:", "http:")):
        parser = PhpArray(value.encode("utf-8"))
        decoded = parser.read()
        if parser.offset != size:
            raise ValueError("Trailing serialized metadata")
        return decoded
    return value


def field_links(raw: str) -> tuple[str, ...]:
    """Project only Baidu locators and optional codes from one selected field."""
    stack = [(decode_field(raw), None, 0)]
    codes_by_url = defaultdict(set)
    remaining = MAX_NODES
    while stack:
        node, sibling_code, depth = stack.pop()
        remaining -= 1
        if remaining < 0 or depth > MAX_DEPTH:
            raise ValueError("Metadata exceeds structure limit")
        if isinstance(node, Mapping):
            codes = {value for key, value in node.items()
                     if key in CODE_FIELDS and isinstance(value, str) and EXTRACTION_CODE.fullmatch(value)}
            sibling_code = next(iter(codes)) if len(codes) == 1 else None
            stack.extend((value, sibling_code, depth + 1) for value in node.values())
        elif isinstance(node, list):
            stack.extend((value, None, depth + 1) for value in node)
        elif isinstance(node, str):
            text = unescape(node)
            matches = list(BAIDU_IN_FIELD.finditer(text))
            for match in matches:
                url = normalize_baidu_share_url(match[0])
                if url is None:
                    continue
                code_params = parse_qs(urlsplit(match[0]).query, keep_blank_values=True, max_num_fields=32).get("pwd", [])
                match_code = TEXT_CODE.match(text[match.end():])
                codes = set(code_params)
                if match_code:
                    codes.add(match_code[1])
                if sibling_code and len(matches) == 1:
                    codes.add(sibling_code)
                codes_by_url[url].update(codes)
    return tuple(url + "?pwd=" + next(iter(codes)) if len(codes) == 1 and EXTRACTION_CODE.fullmatch(next(iter(codes)))
                 else url for url, codes in sorted(codes_by_url.items()))


def read_wxr(stream, articles, known, meta_key=None):
    """Stream a WXR and return countable proof plus newly bound candidates."""
    stack = []
    root = None
    seen_channel = False
    source = []
    counts = Counter()
    evidence = defaultdict(set)
    matched_articles = set()
    sightings = set()
    candidates = {}
    for event, element in iterparse(stream, events=("start", "end"), forbid_dtd=True):
        if event == "start":
            if not stack:
                root = element
                if root.tag != "rss":
                    raise ValueError("Input is not WXR")
            if len(stack) > 32:
                raise ValueError("WXR nesting limit exceeded")
            stack.append(element)
            continue
        if len(stack) == 2 and stack[0] is root and element.tag == "channel":
            if seen_channel:
                raise ValueError("Multiple WXR channels")
            seen_channel = True
        if len(stack) == 3 and stack[1].tag == "channel":
            tag = wp_name(element.tag)
            if tag in ("base_site_url", "base_blog_url"):
                source.append((element.text or "").strip().rstrip("/"))
            elif element.tag == "item":
                counts["items_read"] += 1
                if counts["items_read"] > MAX_WXR_ITEMS:
                    raise ValueError("WXR item limit exceeded")
                read_item(element, articles, known, meta_key, counts, evidence, sightings, matched_articles, candidates)
            stack[1].remove(element)
        stack.pop()
    if not seen_channel or not source or any(value != ARTICLE_ORIGIN for value in source):
        raise ValueError("WXR source does not match the current site")
    counts["candidate_articles"] = len(candidates)
    counts["candidate_links"] = sum(map(len, candidates.values()))
    counts["matched_verified_samples"] = len(matched_articles)
    proof = {key: len(ids) for key, ids in sorted(evidence.items()) if len(ids) >= MIN_MATCHES}
    return dict(counts), proof, candidates


def read_item(element, articles, known, meta_key, counts, evidence, sightings, matched_articles, candidates):
    values = defaultdict(list)
    for child in element:
        name = wp_name(child.tag) or child.tag
        values[name].append(child)

    def value(name):
        items = values[name]
        return (items[0].text or "").strip() if len(items) == 1 else ""

    if value("post_type") != "post" or value("status") != "publish":
        counts["not_published_posts"] += 1
        return
    aid, link = value("post_id"), value("link")
    article = articles.get(aid)
    if article is None or article["url"] != link:
        counts["unmatched_published_posts"] += 1
        return
    if aid in sightings:
        counts["duplicate_bound_posts"] += 1
        matched_articles.discard(aid)
        candidates.pop(aid, None)
        for ids in evidence.values():
            ids.discard(aid)
        return
    sightings.add(aid)
    counts["bound_published_posts"] += 1
    sample = known.get(aid, frozenset())
    fields = defaultdict(list)
    for field in values["postmeta"]:
        names = [part.text for part in field if wp_name(part.tag) == "meta_key"]
        if len(names) != 1 or not isinstance(names[0], str) or not META_KEY.fullmatch(names[0]):
            continue
        name = names[0]
        if (meta_key and name != meta_key) or (not meta_key and not sample):
            continue
        fields[name].extend(part.text or "" for part in field if wp_name(part.tag) == "meta_value")

    for name, raw_values in fields.items():
        if len(raw_values) != 1:
            counts["ambiguous_metadata_fields"] += 1
            continue
        raw = raw_values[0]
        if "pan.baidu.com/" not in raw:
            continue
        try:
            links = field_links(raw)
        except (ValueError, RecursionError):
            counts["unreadable_download_fields"] += 1
            continue
        if not links:
            continue
        if sample:
            obtained = {normalize_baidu_share_url(link) for link in links}
            if obtained == sample:
                evidence[name].add(aid)
                matched_articles.add(aid)
            else:
                counts["conflicting_verified_samples"] += 1
        elif meta_key:
            candidates[aid] = links


def main() -> int:
    parser = argparse.ArgumentParser(description="Privately preview or import the project export from the WordPress site owner.")
    parser.add_argument("--owner-authorized", action="store_true", required=True)
    parser.add_argument("--input", type=Path, required=True, help="Original local WXR in the project workspace")
    parser.add_argument("--meta-key", help="Download metadata key, verified against existing project links")
    parser.add_argument("--protected-out", type=Path, help=f"New private owner-*.dpapi batch in {PRIVATE_ROOT}")
    args = parser.parse_args()
    try:
        path = args.input.absolute()
        if (path.resolve(strict=True) != path or not path.is_relative_to(WORKSPACE) or not path.is_file()
                or not 0 < path.stat().st_size <= MAX_WXR_BYTES):
            raise ValueError("Input is outside the project workspace or exceeds the size limit")
        if args.meta_key is not None and not META_KEY.fullmatch(args.meta_key):
            raise ValueError("Invalid download metadata key")
        if args.protected_out and not args.meta_key:
            raise ValueError("Protected output requires an explicit download metadata key")
        library = open_index()
        exceptions = read_non_projects(DEFAULT_NON_PROJECTS)
        articles = {aid: {"article_id": aid, "site_id": SITE, "url": url} for aid, (revision, url) in article_refs(library).items()
                    if (SITE, aid, revision) not in exceptions}
        known = {aid: frozenset(loc.url for loc in locations) for (site, aid), locations in library.share_locations.items()
                 if site == SITE and aid in articles and locations}
        with path.open("rb") as stream:
            counts, evidence, candidates = read_wxr(stream, articles, known, args.meta_key)
        if args.protected_out:
            if (evidence.get(args.meta_key, 0) < MIN_MATCHES
                    or not 0 < counts["candidate_links"] <= MAX_EXPORT_ITEMS
                    or not args.protected_out.name.startswith("owner-") or len(protected_owner_exports()) >= MAX_ACTIVE_BATCHES):
                raise ValueError("No safe owner import could be bound to verified samples")
            projection = {"schema": OWNER_SCHEMA, "items": [
                {"site_id": SITE, "article_id": aid, "article_url": articles[aid]["url"], "share_url": link}
                for aid, links in candidates.items() for link in links]}
            index = parse_owner_export(projection, articles)
            write_protected_owner_export(args.protected_out, index, articles)
        print(json.dumps({"counts": counts, "matched_metadata_keys": evidence, "protected_saved": bool(args.protected_out)},
                         ensure_ascii=True))
    except (DefusedXmlException, ParseError, OSError, ValueError, TypeError, RecursionError, KeyError):
        print("Owner WXR rejected. Check its site, export scope, verified metadata key, and output path.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
