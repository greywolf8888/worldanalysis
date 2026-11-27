"""Codex public WordPress catalog collector. No account, gated content, or cloud operations."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from lxml import etree, html
from catalog_model import ART, ORIGIN, ROOT, SITE, compact, read_bound, safe_image, safe_text
from intro_inputs import choose_intro, legacy_intro, read_attempts, record_attempt, should_retry
from project_detail import project

STATE = ROOT / 'data' / 'codex-public.json'
SCHEMA = 'wa-v4-codex-public-source.v1'
HOME_TZ = timezone(timedelta(hours=8))
PAGE_SIZE = 100
MAX_CATALOG = 60_000
MAX_RESPONSE = 10 * 1024 * 1024
MAX_STATE = 64 * 1024 * 1024
MAX_DETAIL_BYTES = 24 * 1024 * 1024
MAX_DETAIL_ITEMS = 400
MODIFIED_OVERLAP = timedelta(minutes=20)
MAX_MODIFIED_PAGES = 3
DELAY = 2.65
POST_FIELDS = 'id,link,date,modified,status,title,excerpt,featured_media,_links,_embedded'
DETAIL_FIELDS = 'id,link,date,modified,status,title,content,excerpt,featured_media'
CHALLENGE = re.compile(r'just a moment|cf-chl|访问过于频繁|安全验证后继续', re.I)


class PublicReadError(Exception):
    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


class PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        if url.scheme != 'https' or url.netloc != 'www.wangzjk.com' or not url.path.startswith('/wp-json/wp/v2/posts'):
            raise PublicReadError('publisher_redirect_outside_catalog', code)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class WordPress:
    def __init__(self):
        self.opener = build_opener(PublicRedirect())
        self.next_request = 0.0

    def get(self, suffix='', **params):
        sleep(max(0.0, self.next_request - monotonic()))
        self.next_request = monotonic() + DELAY
        url = ORIGIN + '/wp-json/wp/v2/posts' + suffix + '?' + urlencode(params)
        try:
            with self.opener.open(Request(url, headers={
                'Accept':'application/json', 'User-Agent':'Worldanalysis/1.0 (public catalog reader)'
            }), timeout=25) as response:
                body = response.read(MAX_RESPONSE + 1)
                headers = response.headers
        except HTTPError as exc:
            if exc.code in (401, 403, 429):
                raise PublicReadError('official_publisher_gate', exc.code) from None
            raise PublicReadError('publisher_http_error', exc.code) from None
        except (URLError, OSError):
            raise PublicReadError('publisher_connection_unavailable') from None
        if len(body) > MAX_RESPONSE:
            raise PublicReadError('publisher_response_too_large')
        if CHALLENGE.search(body[:1000].decode('utf-8', 'replace')) and 'json' not in headers.get('Content-Type',''):
            raise PublicReadError('official_publisher_gate', 200)
        try:
            return json.loads(body), headers, url, sha256(body).hexdigest()
        except (ValueError, UnicodeError):
            raise PublicReadError('publisher_response_not_readable') from None

    def listing(self, page, *, sort='id', **window):
        rows, headers, _, digest = self.get(
            page=page, per_page=PAGE_SIZE, orderby=sort, order='desc', status='publish',
            _embed='wp:featuredmedia', _fields=POST_FIELDS, **window
        )
        try:
            total = int(headers['X-WP-Total'])
            pages = int(headers['X-WP-TotalPages'])
        except (TypeError, ValueError):
            raise PublicReadError('publisher_pagination_unverified') from None
        if (not isinstance(rows, list) or not 0 <= total <= MAX_CATALOG or pages != (total + PAGE_SIZE - 1)//PAGE_SIZE
                or (total and not 1 <= page <= pages) or len(rows) != min(PAGE_SIZE, max(0,total-(page-1)*PAGE_SIZE))):
            raise PublicReadError('publisher_page_size_changed')
        return rows, total, pages, digest


def local_clock(value):
    if not isinstance(value, str) or not re.fullmatch(r'20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d',value):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def headline(markup, limit):
    if not isinstance(markup, str) or len(markup) > 12_000:
        return ''
    try:
        return safe_text(html.fragment_fromstring(markup, create_parent='div').text_content(), limit)
    except etree.LxmlError:
        return ''


def normalize(row):
    if not isinstance(row, dict):
        raise PublicReadError('publisher_listing_schema_changed')
    article_id = str(row.get('id'))
    url = ORIGIN + '/' + article_id + '.html'
    published = local_clock(row.get('date'))
    modified = local_clock(row.get('modified'))
    title_data = row.get('title')
    title = headline(title_data.get('rendered'), 600) if isinstance(title_data, dict) else ''
    if not ART.fullmatch(article_id) or row.get('link') != url or not published or not title:
        return None
    embedded = row.get('_embedded')
    featured = embedded.get('wp:featuredmedia') if isinstance(embedded, dict) else None
    if not isinstance(featured, list):
        featured = []
    cover = next((link for attachment in featured if isinstance(attachment, dict)
                  and attachment.get('id') == row.get('featured_media')
                  and (link := safe_image(attachment.get('source_url')))), None)
    excerpt_data = row.get('excerpt')
    excerpt = (headline(excerpt_data.get('rendered'), 360)
               if isinstance(excerpt_data, dict) and excerpt_data.get('protected') is False else '')
    return {'article_id':article_id,'url':url,'published_at':published.date().isoformat(),'title':title,
            'publisher_published':published.isoformat(), 'publisher_modified':modified.isoformat() if modified else None,
            'cover_url':cover,'public_excerpt':excerpt}


def merge_article(articles, row, observed_at, *, preserve_cover=False):
    key = row['article_id']
    previous = articles.get(key)
    if previous is None:
        articles[key] = {**row, 'first_collected_at': observed_at}
        return True
    if previous.get('publisher_modified') and (not row['publisher_modified'] or
                                                row['publisher_modified'] < previous['publisher_modified']):
        return False
    changed = any(previous.get(field) != row[field] for field in ('title', 'published_at', 'publisher_modified', 'public_excerpt'))
    if preserve_cover:
        row = {**row, 'cover_url':previous.get('cover_url')}
    articles[key] = {**previous, **row}
    return changed


def load(path):
    if not path.exists():
        return {'schema':SCHEMA,'site':SITE,'articles':{},'details':{},'attempts':{},
                'history':{'before':None,'next_page':1,'page_digests':{},'collected':0,'unmapped':0,'last_id':None},
                'head':{},'collection':{},'official_site_hold':False}
    if path.resolve() != path.absolute() or path.stat().st_size > MAX_STATE:
        raise ValueError('collection file out of bounds')
    doc = json.loads(path.read_bytes())
    if doc['schema'] != SCHEMA or doc['site'] != SITE:
        raise ValueError('wrong local collection')
    return doc


def compress_ntfs_file(handle):
    if sys.platform != 'win32':
        return False
    import ctypes
    from ctypes import wintypes
    from msvcrt import get_osfhandle
    device_io = ctypes.WinDLL('kernel32',use_last_error=True).DeviceIoControl
    device_io.argtypes = [wintypes.HANDLE,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,
                          wintypes.LPVOID,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),wintypes.LPVOID]
    device_io.restype = wintypes.BOOL
    control_code = 0x0009C040  # FSCTL_SET_COMPRESSION
    compression = ctypes.c_ushort(1)  # COMPRESSION_FORMAT_DEFAULT
    returned = wintypes.DWORD()
    return bool(device_io(get_osfhandle(handle.fileno()),control_code,ctypes.byref(compression),
                          ctypes.sizeof(compression),None,0,ctypes.byref(returned),None))


def publish(state, path):
    body = compact(state)
    if len(body) > MAX_STATE:
        raise ValueError('collection budget exceeded')
    temp = path.with_suffix('.json.next')
    with temp.open('w+b') as dst:
        if len(body)>=512*1024:
            compress_ntfs_file(dst)
        dst.write(body)
        dst.flush()
        os.fsync(dst.fileno())
    temp.replace(path)


@contextmanager
def collector_lock(path):
    lock = path.with_suffix('.lock')
    with lock.open('a+b') as f:
        if sys.platform == 'win32':
            import msvcrt
            if f.tell() == 0:
                f.write(b'\0')
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def detail_candidates(state, attempts, legacy, clock, seed=()):
    articles = state['articles']
    order = sorted(articles, key=lambda key:(
        articles[key].get('publisher_modified') or articles[key].get('publisher_published') or articles[key]['published_at'],
        articles[key]['published_at'], int(key)), reverse=True)
    for key in dict.fromkeys([*seed, *order]):
        if key not in articles:
            continue
        modified = articles[key].get('publisher_modified')
        if not should_retry(attempts.get(key), modified, clock):
            continue
        intro = choose_intro(None, legacy_intro(SITE, key, legacy.get(key)), modified)
        if key not in seed and intro is not None and intro['coverage'] != 'missing':
            continue
        yield key


def existing_details(path, state):
    """Reuse prior local observations without requesting the site again."""
    cache = dict(state['details'])
    for name, schema, field in (
        ('public-details.json', 'wangzjk-v4-public-article-projection.v1', 'articles'),
        ('discovery-head.json', 'wa-v4-public-discovery-checkpoint.v1', 'detail_overlay'),
    ):
        place = path.parent / name
        if not place.exists():
            continue
        artifact, _ = read_bound(place, 3_000_000)
        if artifact.get('schema') != schema or artifact.get('site', artifact.get('site_id')) != SITE:
            continue
        rows = artifact.get(field)
        if not isinstance(rows, dict):
            continue
        for key, row in rows.items():
            if (isinstance(row, dict) and row.get('state') == 'public_sample'
                    and row.get('observed_at', '') > cache.get(key, {}).get('observed_at', '')):
                cache[key] = row
    return cache


def capture_projection(payload, key, timestamp, source, checksum):
    try:
        return project({'article_id':key,'http':200,'observed_at':timestamp,'payload':payload,
                        'source':source,'body_sha256':checksum}, block_limit=800, markup_limit=400_000,
                       include_intro=True)
    except (ValueError,etree.LxmlError):
        return {'state':'unavailable','observed_at':timestamp}


def modified_discovery(client, state, timestamp, start, upper, page_limit):
    """Read a resumable fixed window separately from publication-date discovery."""
    cursor = state.setdefault('modified', {'after': start, 'next_page': 1})
    after, before = local_clock(cursor['after']), local_clock(cursor.setdefault('before', upper))
    if after is None or before is None:
        raise ValueError('invalid modification cursor')
    if after >= before:
        return []
    lower = (after - MODIFIED_OVERLAP).isoformat()
    changed = []
    for _ in range(page_limit):
        page = cursor['next_page']
        try:
            rows, total, pages, _ = client.listing(page, sort='modified',
                                                   modified_after=lower, modified_before=cursor['before'])
        except PublicReadError as error:
            if page > 1 and (error.status == 400 or error.reason == 'publisher_page_size_changed'):
                cursor.clear()
                cursor.update(after=after.isoformat(), before=before.isoformat(), next_page=1)
            raise
        ids = [row.get('id') if isinstance(row, dict) else None for row in rows]
        dates = [row.get('modified') if isinstance(row, dict) else None for row in rows]
        seen = set(cursor.get('seen_ids', []))
        if (any(type(key) is not int for key in ids) or len(ids) != len(set(ids)) or seen.intersection(ids)
                or any(local_clock(date) is None or not lower <= date <= cursor['before'] for date in dates)
                or cursor.get('publisher_total', total) != total
                or (page >= pages and len(seen) + len(ids) != total)):
            # Keep the same upper bound and discard only the changed page checkpoint.
            for field in ('last_modified', 'publisher_total', 'seen_ids'):
                cursor.pop(field, None)
            cursor['next_page'] = 1
            raise PublicReadError('publisher_modified_pagination_changed')
        # The publisher honors the date window but can return an unsorted page.
        # Verify coverage by unique IDs and the advertised total, then prioritize newer text locally.
        for raw in sorted(rows, key=lambda row: (row['modified'], row['id']), reverse=True):
            row = normalize(raw)
            if row and merge_article(state['articles'], row, timestamp):
                changed.append(row['article_id'])
        if page >= pages:
            cursor.clear()
            cursor.update(after=before.isoformat(), next_page=1)
            break
        cursor.pop('last_modified', None)
        cursor.update(next_page=page+1, publisher_total=total,
                      seen_ids=[*cursor.get('seen_ids', []), *ids])
    return changed


def collect(path, max_pages, max_details, seed, *, intro_db=None):
    state = load(path)
    if state['official_site_hold']:
        return {'state':'publisher_access_restricted','resumable':True}
    clock = datetime.now(HOME_TZ).replace(microsecond=0)
    timestamp = clock.isoformat()
    client = WordPress()
    history = state['history']
    intro_db = intro_db or path.parent / 'pipeline-runs.sqlite3'
    before_size = len(state['articles'])
    details_added = 0
    introductions_saved = 0
    intro_failures = 0
    modified_notice = None
    changed = list(seed)

    def status(stop=None):
        state['collection'] = {
            'observed_at':datetime.now(HOME_TZ).isoformat(timespec='seconds'),
            'stop_reason':stop,'publisher_total':history.get('publisher_total'),
            'catalog_complete':history.get('completed') is True and history.get('unmapped') == 0,
            'history_observed':history['collected'],'next_page':history['next_page'],
            'new_this_run':len(state['articles'])-before_size,'details_added':details_added,
            'introductions_saved':introductions_saved,'introduction_failures':intro_failures,
            'modified_notice':modified_notice,
            'read_only':True
        }
        publish(state, path)
        return {'state':stop or 'updated','articles':len(state['articles']),
                'covers':sum(bool(row['cover_url']) for row in state['articles'].values()), **state['collection']}

    try:
        history['before'] = history['before'] or clock.isoformat().split('+')[0]
        modified_start = state['head'].get('after', history['before'])
        if history.get('completed') and not state['head']:
            state['head'] = {'after':history['before'],'next_page':1}
        if state['head']:
            head = state['head']
            head['before'] = head.get('before') or clock.isoformat().split('+')[0]
            for _ in range(max_pages):
                page = head['next_page']
                rows, total, pages, _ = client.listing(page, after=head['after'], before=head['before'])
                for raw in rows:
                    if row := normalize(raw):
                        if merge_article(state['articles'], row, timestamp):
                            changed.append(row['article_id'])
                if page >= pages:
                    head.update(after=head['before'], next_page=1)
                    head.pop('before')
                    break
                head['next_page'] += 1
        for index in range(max_pages):
            if history.get('completed'):
                break
            page = history['next_page']
            rows, total, pages, page_digest = client.listing(page, before=history['before'])
            ids = [row.get('id') for row in rows]
            if (any(type(number) is not int for number in ids) or ids != sorted(set(ids),reverse=True)
                    or (ids and history['last_id'] is not None and ids[0] >= history['last_id'])
                    or (history.get('publisher_total',total) != total)):
                raise PublicReadError('publisher_pagination_changed')
            history['publisher_total'] = total
            history['page_digests'][str(page)] = page_digest
            history['collected'] += len(rows)
            if ids:
                history['last_id'] = ids[-1]
            for raw in rows:
                if row := normalize(raw):
                    merge_article(state['articles'], row, timestamp)
                else:
                    history['unmapped'] += 1
            history['next_page'] += 1
            history['completed'] = page >= pages and history['collected'] == total
            if history['completed'] or index == 0 or (index+1) % 8 == 0:
                status()
        if history.get('completed') and max_pages:
            try:
                changed.extend(modified_discovery(client, state, timestamp, modified_start,
                                                  clock.isoformat().split('+')[0], min(max_pages, MAX_MODIFIED_PAGES)))
            except PublicReadError as error:
                if error.reason not in {'publisher_modified_pagination_changed', 'publisher_page_size_changed'} and error.status != 400:
                    raise
                modified_notice = error.reason
        if not max_details:
            return status()
        legacy = existing_details(path, state)
        attempts = read_attempts(intro_db, SITE)
        display_bytes = sum(len(compact(detail)) + 40 for detail in state['details'].values())
        for _, key in zip(range(max_details), detail_candidates(state, attempts, legacy, clock, changed)):
            modified = state['articles'][key].get('publisher_modified')
            try:
                payload, _, source, checksum = client.get('/'+key,_fields=DETAIL_FIELDS)
            except PublicReadError as exc:
                if exc.status in (404, 410):
                    record_attempt(intro_db, SITE, key, modified, timestamp, 'source_missing')
                    continue
                if exc.reason == 'publisher_http_error' or exc.reason in {
                    'publisher_response_too_large', 'publisher_response_not_readable'
                }:
                    record_attempt(intro_db, SITE, key, modified, timestamp, 'failed')
                    intro_failures += 1
                    continue
                raise
            parsed = capture_projection(payload, key, timestamp, source, checksum)
            intro = parsed.pop('introduction', None)
            if intro is None:
                result = 'restricted' if parsed['state'] == 'restricted' else 'failed'
                record_attempt(intro_db, SITE, key, modified, timestamp, result)
                intro_failures += result == 'failed'
                continue
            metadata = normalize(payload)
            current = metadata.get('publisher_modified') if metadata else None
            if (not metadata or current != intro['publisher_modified']
                    or (modified and (not current or current < modified))):
                record_attempt(intro_db, SITE, key, modified, timestamp, 'failed')
                intro_failures += 1
                continue
            merge_article(state['articles'], metadata, timestamp, preserve_cover=True)
            record_attempt(intro_db, SITE, key, current, timestamp, 'saved', intro)
            introductions_saved += 1
            if parsed['state'] == 'public_sample':
                parsed['collector'] = 'codex'
                previous = state['details'].get(key)
                delta = len(compact(parsed)) + 40 - (len(compact(previous)) + 40 if previous else 0)
                if (len(state['details']) < MAX_DETAIL_ITEMS or previous) and display_bytes+delta <= MAX_DETAIL_BYTES:
                    state['details'][key] = parsed
                    display_bytes += delta
                    details_added += int(previous is None)
        return status()
    except PublicReadError as exc:
        state['official_site_hold'] = exc.reason == 'official_publisher_gate'
        state['last_http'] = exc.status
        return status(exc.reason)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pages', type=int, default=12)
    parser.add_argument('--details', type=int, default=4)
    parser.add_argument('--seed-detail', action='append', default=[])
    args = parser.parse_args()
    if not 0 <= args.pages <= 160 or not 0 <= args.details <= 20 or not all(ART.fullmatch(value) for value in args.seed_detail):
        parser.error('batch out of bounds')
    with collector_lock(STATE):
        result = collect(STATE,args.pages,args.details,args.seed_detail)
    print(json.dumps(result,ensure_ascii=True))
    return 0 if result['state']=='updated' else 2


if __name__ == '__main__':
    raise SystemExit(main())
