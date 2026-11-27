"""Baidu share locators bound to known articles. No provider I/O or transfer authority."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO
from unicodedata import category
from urllib.parse import parse_qs, urlsplit


SITE = "wangzjk.com"
ARTICLE_ORIGIN = "https://www.wangzjk.com"
BAIDU_HOST = "pan.baidu.com"
OWNER_SCHEMA = "wa-v4-owner-share-locations.v1"
PRIVATE_ROOT = Path(__file__).resolve().parents[3] / ".runtime" / "v4-private" / "share-locations"
MAX_EXPORT_BYTES = 8_000_000
MAX_EXPORT_ITEMS = 25000
MAX_ACTIVE_BATCHES = 32
MAX_BROWSER_IMPORT_BYTES = 100_000
ARTICLE_ID = re.compile(r"[1-9][0-9]{0,17}")
DIRECT_PATH = re.compile(r"/s/([A-Za-z0-9_-]{1,129})")
SHORT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
EXTRACTION_CODE = re.compile(r"[A-Za-z0-9]{4}")
ACCESS_GATES = frozenset({"blocked", "needs_login", "needs_access", "permission_denied", "captcha", "rate_limited"})


@dataclass(frozen=True, slots=True)
class ShareLocation:
    url: str = field(repr=False)
    origins: tuple[str, ...]
    extraction_code: str | None = field(default=None, repr=False)

    @property
    def href(self) -> str:
        return self.url + f"?pwd={self.extraction_code}" if self.extraction_code else self.url


ShareIndex = dict[tuple[str, str], tuple[ShareLocation, ...]]


def normalize_baidu_share_url(value: object) -> str | None:
    """Keep only the canonical HTTPS locator. Never return a query, code, or fragment."""
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        return None
    if any(c.isspace() or c == "\\" or category(c) in {"Cc", "Cf"} for c in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.netloc.lower() != BAIDU_HOST:
            return None
        match = DIRECT_PATH.fullmatch(parts.path)
        if match:
            token = match[1]
        elif parts.path == "/share/init":
            values = parse_qs(parts.query, keep_blank_values=True, max_num_fields=32).get("surl", ())
            if len(values) != 1 or not SHORT_ID.fullmatch(values[0]):
                return None
            token = "1" + values[0]
        else:
            return None
    except ValueError:
        return None
    return f"https://{BAIDU_HOST}/s/{token}"


def _article_url(site_id: object, article_id: object) -> str | None:
    if site_id == SITE and isinstance(article_id, str) and ARTICLE_ID.fullmatch(article_id):
        return f"{ARTICLE_ORIGIN}/{article_id}.html"
    return None


def _read_location(raw: object, origin: str, explicit_code: object = None) -> ShareLocation | None:
    url = normalize_baidu_share_url(raw)
    if url is None:
        return None
    try:
        codes = parse_qs(urlsplit(raw).query, max_num_fields=32).get("pwd", ())
    except ValueError:
        return None
    code = codes[0] if len(codes) == 1 else explicit_code if not codes else None
    if not isinstance(code, str) or not EXTRACTION_CODE.fullmatch(code):
        code = None
    return ShareLocation(url, (origin,), code)


def merge_share_locations(*groups: Iterable[ShareLocation]) -> tuple[ShareLocation, ...]:
    """Merge references for a single, already-bound article only."""
    origins_by_url: dict[str, set[str]] = {}
    codes_by_url: dict[str, set[str]] = {}
    for group in groups:
        for location in group:
            url = normalize_baidu_share_url(location.url)
            if url:
                origins_by_url.setdefault(url, set()).update(location.origins)
                if location.extraction_code:
                    codes_by_url.setdefault(url, set()).add(location.extraction_code)
    locations = []
    for url, origins in origins_by_url.items():
        codes = codes_by_url.get(url, ())
        code = next(iter(codes)) if len(codes) == 1 else None
        locations.append(ShareLocation(url, tuple(sorted(origins)), code))
    return tuple(locations)


def historical_share_locations(record: Mapping) -> tuple[ShareLocation, ...]:
    """Project catalog locators after matching the original article. This is historical evidence."""
    expected = _article_url(SITE, record.get("article_id"))
    shares = record.get("shares")
    if not expected or record.get("url") != expected or record.get("site_id", SITE) != SITE:
        return ()
    gate = record.get("access_state", "")
    if not isinstance(shares, list) or not isinstance(gate, str) or gate in ACCESS_GATES:
        return ()
    return merge_share_locations(
        location for share in shares if isinstance(share, dict)
        and (location := _read_location(share.get("url"), "historical_catalog", share.get("code")))
    )


def parse_owner_export(document: object, articles: Mapping[str, Mapping]) -> ShareIndex:
    """Validate an explicitly supplied owner export against the trusted article map."""
    if not isinstance(document, dict) or set(document) != {"schema", "items"} or document["schema"] != OWNER_SCHEMA:
        raise ValueError("Invalid owner share export schema")
    items = document["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_EXPORT_ITEMS:
        raise ValueError("Invalid owner share export item count")
    by_article: dict[tuple[str, str], list[ShareLocation]] = {}
    for position, item in enumerate(items, 1):
        if not isinstance(item, dict) or set(item) != {"site_id", "article_id", "article_url", "share_url"}:
            raise ValueError(f"Invalid owner share export item {position}")
        site_id, aid = item["site_id"], item["article_id"]
        expected = _article_url(site_id, aid)
        record = articles.get(aid) if expected else None
        if (not isinstance(record, Mapping) or item["article_url"] != expected or record.get("url") != expected
                or record.get("article_id") != aid or record.get("site_id", SITE) != SITE):
            raise ValueError(f"Unknown or mismatched article at item {position}")
        location = _read_location(item["share_url"], "owner_export")
        if location is None:
            raise ValueError(f"Unsupported Baidu share locator at item {position}")
        by_article.setdefault((site_id, aid), []).append(location)
    return {key: merge_share_locations(rows) for key, rows in by_article.items()}


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON field")
    return result


def _decode_document(payload: bytes) -> object:
    if len(payload) > MAX_EXPORT_BYTES:
        raise ValueError("Owner share export exceeds size limit")
    try:
        return json.loads(payload.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise ValueError("Invalid owner share export JSON") from None


def read_owner_export(source: Path | BinaryIO, articles: Mapping[str, Mapping]) -> ShareIndex:
    """Read an owner export from an explicit local file or binary stdin."""
    if isinstance(source, Path):
        with source.open("rb") as stream:
            return read_owner_export(stream, articles)
    return parse_owner_export(_decode_document(source.read(MAX_EXPORT_BYTES + 1)), articles)


def _protect(payload: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise OSError("User-protected owner share storage requires Windows")
    import ctypes as c
    from ctypes import wintypes as w

    class DataBlob(c.Structure):
        _fields_ = [("length", w.DWORD), ("data", c.POINTER(c.c_ubyte))]

    buffer = (c.c_ubyte * len(payload)).from_buffer_copy(payload)
    source, target = DataBlob(len(payload), buffer), DataBlob()
    crypt = c.WinDLL("crypt32", use_last_error=True)
    transform = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    transform.argtypes = [c.POINTER(DataBlob), c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p, w.DWORD, c.POINTER(DataBlob)]
    transform.restype = w.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; without LOCAL_MACHINE the blob is user-scoped.
    if not transform(c.byref(source), None, None, None, None, 1, c.byref(target)):
        raise OSError("Windows could not open the protected owner share data")
    try:
        return c.string_at(target.data, target.length)
    finally:
        free = c.WinDLL("kernel32").LocalFree
        free.argtypes = [c.c_void_p]
        free.restype = c.c_void_p
        free(target.data)


def _private_path(path: Path) -> Path:
    path = path.absolute()
    if path.parent != PRIVATE_ROOT or path.suffix != ".dpapi" or path.resolve() != path:
        raise ValueError("Use a regular .dpapi file in the project private share directory")
    return path


def _owner_document(index: ShareIndex) -> dict:
    return {"schema": OWNER_SCHEMA, "items": [
        {"site_id": site, "article_id": aid, "article_url": _article_url(site, aid), "share_url": loc.href}
        for (site, aid), locations in index.items()
        for loc in locations
        if "owner_export" in loc.origins
    ]}


def write_protected_owner_export(path: Path, index: ShareIndex, articles: Mapping[str, Mapping]) -> None:
    """Create one protected batch in the private project directory without overwriting a prior batch."""
    _write_protected_owner_export(path, index, articles, replace=False)


def _write_protected_owner_export(path: Path, index: ShareIndex, articles: Mapping[str, Mapping], *, replace: bool) -> None:
    path = _private_path(path)
    # Recheck the current article binding and normalize before writing.
    sanitized = parse_owner_export(_owner_document(index), articles)
    document = _owner_document(sanitized)
    payload = json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_EXPORT_BYTES:
        raise ValueError("Owner share export exceeds size limit")
    ciphertext = _protect(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(ciphertext)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, _private_path(path))
        else:
            os.link(temp, _private_path(path))
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def read_protected_owner_export(path: Path, articles: Mapping[str, Mapping]) -> ShareIndex:
    """Read only the explicitly named protected batch, rechecking the current article binding."""
    with _private_path(path).open("rb") as stream:
        payload = stream.read(MAX_EXPORT_BYTES + 65_537)
    if len(payload) > MAX_EXPORT_BYTES + 65_536:
        raise ValueError("Protected owner share file exceeds size limit")
    return parse_owner_export(_decode_document(_protect(payload, decrypt=True)), articles)


def protected_owner_exports() -> tuple[Path, ...]:
    """Find only active owner imports in the local private project directory."""
    if not PRIVATE_ROOT.exists():
        return ()
    if PRIVATE_ROOT.resolve(strict=True) != PRIVATE_ROOT or not PRIVATE_ROOT.is_dir():
        raise ValueError("Invalid owner share directory")
    paths = sorted(PRIVATE_ROOT.glob("owner-*.dpapi"))
    if len(paths) > MAX_ACTIVE_BATCHES:
        raise ValueError("Too many active owner share batches")
    for path in paths:
        if not _private_path(path).is_file():
            raise ValueError("Invalid owner share batch")
    return tuple(paths)


def import_browser_share_locations(raw: bytes, articles: Mapping[str, Mapping]) -> dict:
    if not 1 <= len(raw) <= MAX_BROWSER_IMPORT_BYTES:
        raise ValueError("Invalid browser import size")
    observed = parse_owner_export(_decode_document(raw), articles)
    path = PRIVATE_ROOT / "owner-browser.dpapi"
    prior = read_protected_owner_export(path, articles) if path.exists() else {}
    if not path.exists() and len(protected_owner_exports()) >= MAX_ACTIVE_BATCHES:
        raise ValueError("Too many active owner share batches")
    combined = {**prior, **observed}
    _write_protected_owner_export(path, combined, articles, replace=True)
    return {"articles_read": len(observed), "new_articles": len(observed.keys() - prior.keys()), "total_articles": len(combined)}


class _ImportParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid import arguments. See --help.\n")


def main() -> int:
    parser = _ImportParser(description="Validate an explicitly owner-authorized share export. Prints counts only.")
    parser.add_argument("--owner-authorized", action="store_true", required=True)
    parser.add_argument("--articles", type=Path, help="Trusted article snapshot. Omit to use this resource library's article directory.")
    parser.add_argument("--input", required=True, help="Explicit JSON export path, or - for stdin")
    parser.add_argument("--protected-out", type=Path, help=f"New owner-*.dpapi batch in {PRIVATE_ROOT} to show in the library")
    args = parser.parse_args()
    try:
        if args.articles:
            with args.articles.open("rb") as stream:
                payload = stream.read(24_000_001)
            if len(payload) > 24_000_000:
                raise ValueError("Trusted article map exceeds size limit")
            articles = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=_unique_object)["articles"]
        else:
            from catalog_model import open_index

            articles = {(ref["article_id"]): ref for row in open_index().by_id.values()
                        for ref in row["sources"] if ref["site_id"] == SITE}
        if not isinstance(articles, dict):
            raise ValueError("Invalid trusted article map")
        source = sys.stdin.buffer if args.input == "-" else Path(args.input)
        index = read_owner_export(source, articles)
        if args.protected_out:
            if not args.protected_out.name.startswith("owner-") or len(protected_owner_exports()) >= MAX_ACTIVE_BATCHES:
                raise ValueError("Invalid active owner batch name or count")
            write_protected_owner_export(args.protected_out, index, articles)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print("Owner share import was rejected. Check the export schema, article bindings, and output path.", file=sys.stderr)
        return 1
    print(json.dumps({"articles": len(index), "locators": sum(map(len, index.values())), "protected_saved": bool(args.protected_out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
