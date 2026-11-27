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
from catalog_model import ART, ORIGIN, ROOT, SITE, compact, safe_image, safe_text
from project_detail import project

STATE = ROOT / 'data' / 'codex-public.json'
SCHEMA = 'wa-v4-codex-public-source.v1'
HOME_TZ = timezone(timedelta(hours=8))
PAGE_SIZE = 100
MAX_CATALOG = 60_000
MAX_RESPONSE = 10 * 1024 * 1024
MAX_STATE = 64 * 1024 * 1024
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

    def listing(self, page, **window):
        rows, headers, _, digest = self.get(
            page=page, per_page=PAGE_SIZE, orderby='id', order='desc', status='publish',
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


def date_only(value):
    if not isinstance(value, str) or not re.fullmatch(r'20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d',value):
        return None
    try:
        return datetime.fromisoformat(value).date().isoformat()
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
    day = date_only(row.get('date'))
    title = headline((row.get('title') or {}).get('rendered'), 600)
    if not ART.fullmatch(article_id) or row.get('link') != url or not day or not title:
        return None
    featured = (row.get('_embedded') or {}).get('wp:featuredmedia') or []
    cover = next((link for attachment in featured if isinstance(attachment, dict)
                  and attachment.get('id') == row.get('featured_media')
                  and (link := safe_image(attachment.get('source_url')))), None)
    excerpt_data = row.get('excerpt') or {}
    excerpt = headline(excerpt_data.get('rendered'), 360) if excerpt_data.get('protected') is False else ''
    return {'article_id':article_id,'url':url,'published_at':day,'title':title,
            'publisher_modified':row.get('modified'),'cover_url':cover,'public_excerpt':excerpt}


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


def detail_candidates(state, seed=()):
    articles = state['articles']
    order = sorted(articles, key=lambda key:(articles[key]['published_at'], int(key)), reverse=True)
    for key in dict.fromkeys([*seed, *order]):
        if key not in articles:
            continue
        if state['attempts'].get(key) == articles[key].get('publisher_modified') and key in state['attempts']:
            continue
        yield key


def capture_projection(payload, key, timestamp, source, checksum):
    try:
        return project({'article_id':key,'http':200,'observed_at':timestamp,'payload':payload,
                        'source':source,'body_sha256':checksum}, block_limit=800, markup_limit=400_000)
    except (ValueError,etree.LxmlError):
        return {'state':'unavailable','observed_at':timestamp}


def collect(path, max_pages, max_details, seed):
    state = load(path)
    if state['official_site_hold']:
        return {'state':'publisher_access_restricted','resumable':True}
    clock = datetime.now(HOME_TZ).replace(microsecond=0)
    timestamp = clock.isoformat()
    client = WordPress()
    history = state['history']
    before_size = len(state['articles'])
    details_added = 0

    def status(stop=None):
        state['collection'] = {
            'observed_at':datetime.now(HOME_TZ).isoformat(timespec='seconds'),
            'stop_reason':stop,'publisher_total':history.get('publisher_total'),
            'catalog_complete':history.get('completed') is True and history.get('unmapped') == 0,
            'history_observed':history['collected'],'next_page':history['next_page'],
            'new_this_run':len(state['articles'])-before_size,'details_added':details_added,
            'read_only':True
        }
        publish(state, path)
        return {'state':stop or 'updated','articles':len(state['articles']),
                'covers':sum(bool(row['cover_url']) for row in state['articles'].values()), **state['collection']}

    try:
        history['before'] = history['before'] or clock.isoformat().split('+')[0]
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
                        state['articles'][row['article_id']] = row
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
                    state['articles'][row['article_id']] = row
                else:
                    history['unmapped'] += 1
            history['next_page'] += 1
            history['completed'] = page >= pages and history['collected'] == total
            if history['completed'] or index == 0 or (index+1) % 8 == 0:
                status()
        for _, key in zip(range(max_details), detail_candidates(state, seed)):
            try:
                payload, _, source, checksum = client.get('/'+key,_fields=DETAIL_FIELDS)
            except PublicReadError as exc:
                if exc.status in (404, 410):
                    state['attempts'][key] = state['articles'][key].get('publisher_modified')
                    continue
                raise
            parsed = capture_projection(payload, key, timestamp, source, checksum)
            state['attempts'][key] = state['articles'][key].get('publisher_modified')
            if parsed.get('state') == 'public_sample':
                # Materialized detail is bounded independently from complete metadata coverage.
                if len(state['details']) < 400 or key in state['details']:
                    details_added += int(key not in state['details'])
                    parsed['collector']='codex'
                    state['details'][key] = parsed
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
