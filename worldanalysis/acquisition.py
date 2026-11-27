"""Authorized website/storage adapter boundary and durable transfer reconciliation.

This module never imports a browser profile or reads authentication material.
A live connector is supplied by the authorized Agent/UI and must resolve current
paths before each operation. A stored receipt is not evidence of a later read.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit, urlunsplit
import json
import sqlite3
from http.client import HTTPException
from urllib.request import build_opener, HTTPRedirectHandler, Request
from urllib.error import HTTPError, URLError
from .policy import GateError, Policy, ByteBudget, local_relative


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def fetch_public_article(url: str, policy: Policy | None = None, *, max_bytes: int = 2 * 1024**2, timeout: int = 15) -> str:
    """Ordinary public HTML only; no cookies, profiles or alternate denied channels."""
    policy = policy or Policy()
    policy.require_site(url)

    class SiteRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            policy.require_site(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    budget, chunks = ByteBudget(max_bytes), []
    try:
        with build_opener(SiteRedirect()).open(Request(url, headers={'Accept': 'text/html'}), timeout=timeout) as response:
            policy.require_site(response.url)
            if response.status != 200:
                raise GateError('SITE_RESPONSE_NOT_SUCCESS')
            if 'html' not in response.headers.get('Content-Type', '').lower():
                raise GateError('SITE_RESPONSE_NOT_HTML')
            while True:
                remaining = budget.limit - budget.received
                if not remaining:
                    # Do not read one unbudgeted byte to discover overflow.
                    raise GateError('ARTICLE_BUDGET_EXHAUSTED')
                chunk = response.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                budget.charge(len(chunk)); chunks.append(chunk)
            charset = response.headers.get_content_charset() or 'utf-8'
            return b''.join(chunks).decode(charset, errors='strict')
    except HTTPError as exc:
        raise GateError('WEBSITE_ACCESS_DENIED' if exc.code in (401, 403, 429) else 'SITE_HTTP_FAILURE', str(exc.code)) from None
    except (URLError, TimeoutError, ConnectionError, HTTPException):
        raise GateError('SITE_NETWORK_FAILURE', 'No valid HTTP response; no alternate credential channel used') from None


class ArticleParser(HTMLParser):
    def __init__(self, base_url, policy):
        super().__init__(convert_charrefs=True)
        self.base_url, self.policy = base_url, policy
        self.ignored = 0
        self.article_depth = 0
        self.parts, self.links = [], []
        self.title = ''
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style', 'noscript'):
            self.ignored += 1
        if tag == 'title':
            self.in_title = True
        if tag == 'article':
            self.article_depth += 1
        if tag == 'a' and attrs.get('href'):
            url = urljoin(self.base_url, attrs['href'])
            p = urlsplit(url)
            if p.scheme in ('http', 'https') and not p.username and not p.password:
                if p.hostname in self.policy.allowed_site_hosts or p.hostname == 'pan.baidu.com':
                    self.links.append(url)

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.ignored = max(0, self.ignored - 1)
        if tag == 'title':
            self.in_title = False
        if tag == 'article':
            self.article_depth = max(0, self.article_depth - 1)

    def handle_data(self, text):
        if self.in_title:
            self.title += text
        if not self.ignored and self.article_depth and text.strip():
            self.parts.append(text.strip())


class Acquisition:
    """Uses the same SQLite connection as Store; no competing business ledger."""
    def __init__(self, connection: sqlite3.Connection, policy: Policy | None = None, *, grant_resolver=None):
        self.connection = connection
        self.policy = policy or Policy()
        self.grant_resolver = grant_resolver
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS site_probes(
                article_url TEXT PRIMARY KEY, state TEXT NOT NULL,
                attempts INTEGER NOT NULL, next_run_at TEXT,
                safe_reason TEXT NOT NULL, observed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS site_discoveries(
                source_id TEXT PRIMARY KEY, article_url TEXT NOT NULL,
                content_revision TEXT NOT NULL, title TEXT NOT NULL,
                description TEXT NOT NULL, restricted_links_json TEXT NOT NULL,
                observed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cloud_operations(
                operation_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                share_ref TEXT NOT NULL, destination TEXT NOT NULL,
                manifest_json TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL,
                grant_ref TEXT NOT NULL, state TEXT NOT NULL,
                remote_task_ref TEXT, reason TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cloud_bindings(
                operation_id TEXT NOT NULL, source_share_ref TEXT NOT NULL,
                source_file_id TEXT NOT NULL, destination_account_ref TEXT NOT NULL,
                destination_fs_id TEXT NOT NULL, verified_path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, is_directory INTEGER NOT NULL,
                verified_at TEXT NOT NULL,
                PRIMARY KEY(operation_id,source_share_ref,source_file_id));
        ''')
        connection.commit()

    def discover_public_article(self, article_url: str) -> dict:
        self.policy.require_site(article_url)
        row = self.connection.execute('SELECT state,attempts,next_run_at FROM site_probes WHERE article_url=?', (article_url,)).fetchone()
        if row and (row[0] == 'blocked' or row[1] >= self.policy.max_attempts):
            raise GateError('SOURCE_PROBE_STOPPED', 'Current channel requires reconciliation, not repeated requests')
        if row and row[2] and datetime.fromisoformat(row[2]) > datetime.now(timezone.utc):
            raise GateError('SOURCE_RETRY_NOT_DUE')
        attempts = (row[1] if row else 0) + 1
        self.connection.execute('INSERT INTO site_probes VALUES(?,?,?,?,?,?) ON CONFLICT(article_url) DO UPDATE SET state=excluded.state,attempts=excluded.attempts,next_run_at=NULL,safe_reason=excluded.safe_reason,observed_at=excluded.observed_at', (article_url,'probing',attempts,None,'read-only request registered before execution',now()))
        self.connection.commit()
        try:
            html = fetch_public_article(article_url,self.policy)
            result = self.source_observation(article_url,html)
        except GateError as exc:
            transient = exc.code == 'SITE_NETWORK_FAILURE' and attempts < self.policy.max_attempts
            due = (datetime.now(timezone.utc) + timedelta(seconds=min(900,30 * 2**attempts))).isoformat() if transient else None
            self.connection.execute('UPDATE site_probes SET state=?,next_run_at=?,safe_reason=?,observed_at=? WHERE article_url=?', ('failed_transient' if transient else 'blocked',due,exc.code,now(),article_url))
            self.connection.commit()
            raise
        self.connection.execute('UPDATE site_probes SET state=?,next_run_at=NULL,safe_reason=?,observed_at=? WHERE article_url=?', ('observed','Actual article body retained; not a course completion claim',now(),article_url))
        self.connection.commit()
        return result

    def source_observation(self, article_url: str, html: str) -> dict:
        """Consume actual observed HTML. No website description becomes course text."""
        self.policy.require_site(article_url)
        if len(html.encode('utf-8')) > 8 * 1024**2:
            raise GateError('ARTICLE_BUDGET_EXHAUSTED')
        parser = ArticleParser(article_url, self.policy)
        parser.feed(html)
        description = '\n'.join(parser.parts)
        if not description:
            raise GateError('ARTICLE_BODY_NOT_OBSERVED', 'No article body; no course-content claim')
        links = list(dict.fromkeys(parser.links))
        source_id = 'site-' + sha256(article_url.encode()).hexdigest()[:24]
        revision = digest({'html': html})
        self.connection.execute('INSERT INTO site_discoveries VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET content_revision=excluded.content_revision,title=excluded.title,description=excluded.description,restricted_links_json=excluded.restricted_links_json,observed_at=excluded.observed_at', (source_id, article_url, revision, parser.title.strip(), description, json.dumps(links, ensure_ascii=False), now()))
        self.connection.commit()
        return {'source_id': source_id, 'content_revision': revision, 'role': 'source_description_not_course_text', 'title': parser.title.strip(), 'description_characters': len(description), 'authorized_entry_refs': ['entry-' + digest([source_id, i])[:24] for i, _ in enumerate(links)], 'source_snapshot_retained_locally': True}

    def authorized_entry(self, source_id: str, entry_ref: str) -> str:
        row = self.connection.execute('SELECT restricted_links_json FROM site_discoveries WHERE source_id=?', (source_id,)).fetchone()
        if not row:
            raise GateError('UNKNOWN_SOURCE')
        for index, link in enumerate(json.loads(row[0])):
            if entry_ref == 'entry-' + digest([source_id, index])[:24]:
                return link  # Connector-private: never include this URL in model/report payloads.
        raise GateError('UNKNOWN_ENTRY')

    def _require_grant(self, grant_ref, share_ref, path, operation):
        # Only the host/operator's grant registry can grant. A Plan/string cannot.
        grant = self.grant_resolver(grant_ref) if callable(self.grant_resolver) else None
        if not grant or grant.get('state') != 'granted' or operation not in grant.get('operations', []) or share_ref not in grant.get('share_refs', []):
            raise GateError('CURRENT_AUTHORIZATION_REQUIRED')
        try:
            expires = datetime.fromisoformat(grant['expires_at'].replace('Z', '+00:00'))
            if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
                raise ValueError('expired')
        except (ValueError, KeyError, TypeError):
            raise GateError('AUTHORIZATION_EXPIRED') from None
        if not any(path == self.policy.require_cloud_path(r) or path.startswith(self.policy.require_cloud_path(r) + '/') for r in grant.get('roots', [])):
            raise GateError('GRANT_SCOPE_MISMATCH')

    def prepare_transfer(self, project_id: str, share_ref: str, destination: str, manifest: list[dict], *, grant_ref: str, enumeration_complete: bool) -> dict:
        destination = self.policy.require_cloud_path(destination)
        self._require_grant(grant_ref, share_ref, destination, 'transfer')
        if not grant_ref or not share_ref or not enumeration_complete:
            raise GateError('UNVERIFIED_TRANSFER_SCOPE')
        if not manifest:
            raise GateError('EMPTY_SHARE_MANIFEST')
        normalized, seen_ids, seen_paths = [], set(), set()
        for item in manifest:
            sid, relative = str(item['source_file_id']), local_relative(item['relative_path'])
            if not sid or sid in seen_ids or relative.casefold() in seen_paths or type(item['size_bytes']) is not int or item['size_bytes'] < 0 or type(item['is_directory']) is not bool:
                raise GateError('INVALID_SHARE_MANIFEST')
            seen_ids.add(sid); seen_paths.add(relative.casefold())
            normalized.append({'source_file_id': sid, 'relative_path': relative, 'size_bytes': item['size_bytes'], 'is_directory': item['is_directory']})
        normalized.sort(key=lambda i: i['relative_path'])
        key = digest([project_id, share_ref, destination, normalized])
        op_id = 'transfer-' + key[:32]
        self.connection.execute('INSERT OR IGNORE INTO cloud_operations VALUES(?,?,?,?,?,?,?,?,?,?,?)', (op_id, project_id, share_ref, destination, json.dumps(normalized, ensure_ascii=False), key, grant_ref, 'queued', None, 'Manifest enumerated; not yet submitted', now()))
        self.connection.commit()
        return self.operation(op_id)

    def operation(self, operation_id: str) -> dict:
        cursor = self.connection.execute('SELECT * FROM cloud_operations WHERE operation_id=?', (operation_id,))
        row = cursor.fetchone()
        if row is None:
            raise GateError('UNKNOWN_OPERATION')
        value = dict(zip([c[0] for c in cursor.description], row))
        value['manifest'] = json.loads(value.pop('manifest_json'))
        value['may_submit'] = value['state'] == 'queued'
        return value

    def before_submit(self, operation_id: str, *, resolve_destination) -> dict:
        op = self.operation(operation_id)
        if not op['may_submit']:
            raise GateError('RECONCILE_BEFORE_RESUBMIT')
        self._require_grant(op['grant_ref'], op['share_ref'], op['destination'], 'transfer')
        current = resolve_destination(op['destination'])
        if self.policy.require_cloud_path(current) != op['destination']:
            raise GateError('DESTINATION_CHANGED')
        # Durable before the UI/network side effect; crash can never reset to queued.
        changed = self.connection.execute('UPDATE cloud_operations SET state=?,reason=?,updated_at=? WHERE operation_id=? AND state=?', ('submission_unknown', 'Submission may have happened; reconcile, never blindly replay', now(), operation_id, 'queued'))
        self.connection.commit()
        if changed.rowcount != 1:
            raise GateError('RECONCILE_BEFORE_RESUBMIT')
        return self.operation(operation_id)

    def record_submission(self, operation_id: str, *, remote_task_ref: str | None = None,
                          errno: int = 0, captcha_required: bool = False):
        op = self.operation(operation_id)
        if op['state'] != 'submission_unknown':
            raise GateError('INVALID_SUBMISSION_STATE')
        if type(errno) is not int or type(captcha_required) is not bool:
            raise GateError('INVALID_SUBMISSION_RECEIPT')
        if errno in (113, 401, 403, 429):
            state, reason = 'blocked', 'Provider refused; do not change channel or retry this operation'
        elif captcha_required:
            if remote_task_ref is not None:
                raise GateError('INVALID_SUBMISSION_RECEIPT')
            state, reason = 'captcha_required', 'Observed verification required; not a successful transfer receipt'
        elif errno:
            state, reason = 'submission_unknown', 'Non-success response; reconcile existing target before considering another action'
        else:
            state, reason = 'submitted', 'UI receipt retained; target read-back still required'
        if remote_task_ref is not None and (not isinstance(remote_task_ref, str) or not remote_task_ref or len(remote_task_ref) > 128 or not all(c.isascii() and (c.isalnum() or c in '._-') for c in remote_task_ref)):
            raise GateError('INVALID_TASK_REFERENCE')
        self.connection.execute('UPDATE cloud_operations SET state=?,remote_task_ref=?,reason=?,updated_at=? WHERE operation_id=?', (state, remote_task_ref, reason, now(), operation_id))
        self.connection.commit()
        return self.operation(operation_id)

    def handle_transfer_captcha(self, operation_id, challenge, *, resolve_destination,
                                challenge_is_current, apply_solution, solver=None):
        """Use the opt-in provider only for a current host-classified challenge.

        This does not implement a browser driver or permit a second transfer.
        Project policy and environment credentials load only on this branch.
        """
        from .transfer_captcha import handle
        return handle(self, operation_id, challenge, resolve_destination=resolve_destination,
                      challenge_is_current=challenge_is_current, apply_solution=apply_solution,
                      solver=solver)

    def reconcile(self, operation_id: str, observed: list[dict], *, resolve_asset, destination_account_ref: str, enumeration_complete: bool) -> dict:
        op = self.operation(operation_id)
        if op['state'] == 'blocked':
            raise GateError('CHANNEL_BLOCKED')
        self._require_grant(op['grant_ref'], op['share_ref'], op['destination'], 'inventory')
        if not enumeration_complete:
            raise GateError('DESTINATION_ENUMERATION_INCOMPLETE')
        expected = {i['source_file_id']: i for i in op['manifest']}
        actual, bindings = {}, []
        for item in observed:
            if item.get('source_share_ref') != op['share_ref']:
                raise GateError('WRONG_SOURCE_SHARE')
            sid = str(item['source_file_id'])
            if sid not in expected or sid in actual:
                raise GateError('WRONG_OR_DUPLICATE_SOURCE_ID')
            eid = str(item['destination_fs_id'])
            current = resolve_asset(eid)
            path = self.policy.require_cloud_path(current['path'])
            want = op['destination'] + '/' + expected[sid]['relative_path']
            if current.get('account_ref') != destination_account_ref or path != want or current.get('size_bytes') != expected[sid]['size_bytes'] or current.get('is_directory') != expected[sid]['is_directory']:
                raise GateError('DESTINATION_IDENTITY_MISMATCH')
            if eid in [b[4] for b in bindings]:
                raise GateError('DUPLICATE_DESTINATION_ID')
            actual[sid] = True
            bindings.append((operation_id, op['share_ref'], sid, destination_account_ref, eid, path, current['size_bytes'], int(current['is_directory']), now()))
        if set(actual) != set(expected):
            return {'state': 'partial', 'missing_source_file_ids': sorted(set(expected) - set(actual)), 'verified_complete': False}
        with self.connection:
            for binding in bindings:
                self.connection.execute('INSERT OR REPLACE INTO cloud_bindings VALUES(?,?,?,?,?,?,?,?,?)', binding)
            self.connection.execute('UPDATE cloud_operations SET state=?,reason=?,updated_at=? WHERE operation_id=?', ('verified_complete', 'Every source item bound to an actual current destination identity and path', now(), operation_id))
        return {'state': 'verified_complete', 'verified_items': len(bindings), 'operation_id': operation_id}

    def reauthorize_verified_read(self, operation_id: str, *, grant_ref: str) -> dict:
        """Bind a fresh live host grant to completed evidence, never resubmit it.

        Old grant records remain immutable; a Plan or an expired grant cannot
        renew itself. Blocked/unknown/incomplete operations are not unblocked.
        """
        op = self.operation(operation_id)
        if op['state'] != 'verified_complete':
            raise GateError('TRANSFER_NOT_RECONCILED')
        self._require_grant(grant_ref, op['share_ref'], op['destination'], 'inventory')
        self._require_grant(grant_ref, op['share_ref'], op['destination'], 'read_stream')
        with self.connection:
            changed = self.connection.execute('UPDATE cloud_operations SET grant_ref=?,reason=?,updated_at=? WHERE operation_id=? AND state=? AND grant_ref=?', (grant_ref, 'Fresh host read authorization; existing transfer and bindings unchanged, no resubmission', now(), operation_id, 'verified_complete', op['grant_ref'])).rowcount
            if changed != 1:
                raise GateError('OPERATION_CHANGED')
        return self.operation(operation_id)

    def read_verified_asset(self, operation_id: str, source_file_id: str, *, resolve_asset, open_reader):
        op = self.operation(operation_id)
        if op['state'] != 'verified_complete':
            raise GateError('TRANSFER_NOT_RECONCILED')
        self._require_grant(op['grant_ref'], op['share_ref'], op['destination'], 'read_stream')
        row = self.connection.execute('SELECT destination_account_ref,destination_fs_id,verified_path,size_bytes,is_directory FROM cloud_bindings WHERE operation_id=? AND source_share_ref=? AND source_file_id=?', (operation_id, op['share_ref'], str(source_file_id))).fetchone()
        if not row:
            raise GateError('UNKNOWN_SOURCE_BINDING')
        account, fid, previous_path, size, is_directory = row
        current = resolve_asset(fid)  # Re-resolve on every operation: move-out revokes this read.
        path = self.policy.require_cloud_path(current['path'])
        if current.get('account_ref') != account or path != previous_path or current.get('size_bytes') != size or bool(current.get('is_directory')) != bool(is_directory):
            raise GateError('SOURCE_BINDING_CHANGED')
        if is_directory:
            raise GateError('ASSET_IS_DIRECTORY')
        return open_reader(fid)
