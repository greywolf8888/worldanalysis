"""Plain public introductions and collection attempts in the existing catalog database."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit


MAX_INTRO_BYTES = 256 * 1024
MAX_INTRO_TOTAL_BYTES = 256 * 1024 * 1024
MIN_INTRO_CHARACTERS = 24
PUBLISHER_TEASER = re.compile(r'(?:\[\s*(?:\.{3}|…)\s*\]|阅读全文|查看完整介绍)\s*$')
EXACT_BOILERPLATE = frozenset({
    '仅供学习交流，请勿用于商业用途。',
    '资源来自网络，仅供学习交流。',
    '如有侵权，请联系删除。',
})


def make_intro(site_id, article_id, paragraphs, *, publisher_modified=None, observed_at=None,
               source_kind='public_rest', has_images=False, partial_reason=None):
    """Keep visible paragraphs in order and remove only exact boilerplate for reading."""
    kept = []
    remaining = MAX_INTRO_BYTES
    for paragraph in paragraphs:
        if not paragraph:
            continue
        cost = len(paragraph.encode('utf-8')) + int(bool(kept))
        if cost > remaining:
            partial_reason = 'text_budget'
            break
        kept.append(paragraph)
        remaining -= cost
    raw = '\n'.join(kept)
    cleaned = '\n'.join(line for line in kept if line not in EXACT_BOILERPLATE)
    if not partial_reason and cleaned:
        if PUBLISHER_TEASER.search(cleaned):
            partial_reason = 'publisher_teaser'
        elif sum(character.isalnum() for character in cleaned) < MIN_INTRO_CHARACTERS:
            partial_reason = 'short_text'
    coverage = ('partial' if partial_reason else
                'complete' if cleaned else 'image_only' if has_images else 'missing')
    fingerprint = sha256(json.dumps([raw, cleaned, coverage, partial_reason],
                                    ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
    return {
        'site_id': site_id, 'article_id': article_id, 'raw_text': raw, 'clean_text': cleaned,
        'coverage': coverage, 'coverage_reason': partial_reason, 'source_kind': source_kind,
        'publisher_modified': publisher_modified, 'observed_at': observed_at, 'fingerprint': fingerprint,
    }


def legacy_intro(site_id, article_id, detail):
    """Reuse historical blocks whose original HTML coverage cannot be recovered."""
    if not isinstance(detail, dict) or detail.get('state') != 'public_sample':
        return None
    try:
        source = urlsplit(detail.get('article_url', ''))
    except (TypeError, ValueError):
        return None
    if (source.scheme != 'https' or source.netloc not in {site_id, 'www.' + site_id}
            or source.path != '/' + article_id + '.html' or source.query or source.fragment):
        return None
    from catalog_model import safe_text
    blocks = detail.get('blocks', [])
    if not isinstance(blocks, list):
        return None
    paragraphs = [safe_text(block.get('text'), MAX_INTRO_BYTES) for block in blocks
                  if isinstance(block, dict) and block.get('kind') == 'text']
    return make_intro(
        site_id, article_id, paragraphs,
        publisher_modified=detail.get('publisher_modified'), observed_at=detail.get('observed_at'),
        source_kind='existing_public_detail',
        has_images=any(isinstance(b, dict) and b.get('kind') == 'image' for b in blocks),
        partial_reason='historical_projection' if any(paragraphs) else None,
    )


def choose_intro(stored, legacy, publisher_modified):
    """A known newer article modification invalidates prior text for decisions."""
    for row in (stored, legacy):
        if row is not None and (not publisher_modified or not row.get('publisher_modified')
                                or row['publisher_modified'] == publisher_modified):
            return row
    return None


def _connect(path, *, write=False):
    path = Path(path).absolute()
    if path.resolve() != path or path.is_symlink():
        raise ValueError('unbound introduction database')
    connection = (sqlite3.connect(path, timeout=5) if write else
                  sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5))
    connection.row_factory = sqlite3.Row
    return connection


def _exists(connection, table):
    return connection.execute('SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?',
                              ('table', table)).fetchone() is not None


def read_intros(db_path):
    """Read only; do not initialize or write the database from the website."""
    if not Path(db_path).is_file():
        return {}
    with closing(_connect(db_path)) as connection:
        if not _exists(connection, 'catalog_intros'):
            return {}
        return {(row['site_id'], row['article_id']): {
            **dict(row), 'clean_text': row['raw_text'] if row['clean_text'] is None else row['clean_text'],
        } for row in connection.execute('SELECT * FROM catalog_intros')}


def intro_revision(db_path):
    if not Path(db_path).is_file():
        return 0
    with closing(_connect(db_path)) as connection:
        if not _exists(connection, 'catalog_intro_state'):
            return 0
        return connection.execute('SELECT revision FROM catalog_intro_state WHERE id = 1').fetchone()[0]


def read_attempts(db_path, site_id):
    if not Path(db_path).is_file():
        return {}
    with closing(_connect(db_path)) as connection:
        if not _exists(connection, 'catalog_intro_attempts'):
            return {}
        return {row['article_id']: dict(row) for row in connection.execute(
            'SELECT * FROM catalog_intro_attempts WHERE site_id = ?', (site_id,))}


def should_retry(previous, publisher_modified, now):
    if not previous or previous['publisher_modified'] != publisher_modified:
        return True
    if previous['state'] != 'failed' or previous['attempts'] >= 3:
        return False
    return now >= datetime.fromisoformat(previous['observed_at']) + timedelta(days=previous['attempts'])


def _ensure_tables(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS catalog_intros (
        site_id TEXT NOT NULL, article_id TEXT NOT NULL,
        raw_text TEXT NOT NULL, clean_text TEXT,
        coverage TEXT NOT NULL, coverage_reason TEXT, source_kind TEXT NOT NULL,
        publisher_modified TEXT, observed_at TEXT, fingerprint TEXT NOT NULL,
        payload_bytes INTEGER NOT NULL,
        PRIMARY KEY (site_id, article_id))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS catalog_intro_attempts (
        site_id TEXT NOT NULL, article_id TEXT NOT NULL, publisher_modified TEXT,
        observed_at TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL,
        PRIMARY KEY (site_id, article_id))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS catalog_intro_state (
        id INTEGER PRIMARY KEY CHECK (id = 1), revision INTEGER NOT NULL, total_bytes INTEGER NOT NULL)''')
    connection.execute('INSERT OR IGNORE INTO catalog_intro_state VALUES (1, 0, 0)')


def record_attempt(db_path, site_id, article_id, publisher_modified, observed_at, state, intro=None):
    """Save the introduction and its success together; no failed write advances progress."""
    if ((state == 'saved') != (intro is not None)
            or state not in {'saved', 'failed', 'missing', 'restricted', 'stale', 'source_missing'}):
        raise ValueError('invalid introduction attempt')
    if intro is not None and (intro['site_id'], intro['article_id'], intro['publisher_modified']) != (
            site_id, article_id, publisher_modified):
        raise ValueError('introduction attempt identity mismatch')
    with closing(_connect(db_path, write=True)) as connection, connection:
        connection.execute('BEGIN IMMEDIATE')
        _ensure_tables(connection)
        if intro is not None:
            old = connection.execute('SELECT * FROM catalog_intros WHERE site_id = ? AND article_id = ?',
                                     (site_id, article_id)).fetchone()
            raw, cleaned = intro['raw_text'], intro['clean_text']
            saved_cleaned = None if raw == cleaned else cleaned
            size = len(raw.encode('utf-8')) + len((saved_cleaned or '').encode('utf-8'))
            delta = size - (old['payload_bytes'] if old is not None else 0)
            total = connection.execute('SELECT total_bytes FROM catalog_intro_state WHERE id = 1').fetchone()[0]
            if total + delta > MAX_INTRO_TOTAL_BYTES:
                raise ValueError('introduction storage budget exceeded')
            if old is None or (old['fingerprint'], old['publisher_modified']) != (intro['fingerprint'], publisher_modified):
                fields = ('coverage', 'coverage_reason', 'source_kind', 'publisher_modified', 'observed_at', 'fingerprint')
                connection.execute('''INSERT OR REPLACE INTO catalog_intros (
                    site_id, article_id, raw_text, clean_text, coverage, coverage_reason, source_kind,
                    publisher_modified, observed_at, fingerprint, payload_bytes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                   (site_id, article_id, raw, saved_cleaned, *[intro[key] for key in fields], size))
                connection.execute('UPDATE catalog_intro_state SET revision = revision + 1, total_bytes = total_bytes + ? WHERE id = 1',
                                   (delta,))
        connection.execute('''INSERT INTO catalog_intro_attempts
            (site_id, article_id, publisher_modified, observed_at, state, attempts)
            VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT(site_id, article_id) DO UPDATE SET
                publisher_modified = excluded.publisher_modified, observed_at = excluded.observed_at,
                state = excluded.state, attempts = CASE
                    WHEN excluded.publisher_modified IS catalog_intro_attempts.publisher_modified
                        THEN catalog_intro_attempts.attempts + 1 ELSE 1 END''',
                           (site_id, article_id, publisher_modified, observed_at, state))
