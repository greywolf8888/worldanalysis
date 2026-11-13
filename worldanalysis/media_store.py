"""Bounded-media registration/publication bridge, sharing the business SQLite.

The stored source artifact is explicitly a stat-registration descriptor, NOT an
immutable audio/video copy and NOT a SHA256 claim about the source media bytes.
Only derived, bounded artifacts are copied and hashed for a published Bundle.
"""
from __future__ import annotations
import json
import mimetypes
from pathlib import Path
import sqlite3
import uuid
from .contracts import ContractError, asset_key, canonical, digest, hash_file, now, reject_links, safe_path, scan_secrets


def ensure_schema(connection):
    connection.executescript('''
    CREATE TABLE IF NOT EXISTS media_sources(
      asset_id TEXT NOT NULL, asset_revision TEXT NOT NULL, source_id TEXT NOT NULL,
      source_revision TEXT NOT NULL, identity_json TEXT NOT NULL,
      descriptor_relative_path TEXT NOT NULL, descriptor_sha256 TEXT NOT NULL,
      original_size_bytes INTEGER NOT NULL, observed_at TEXT NOT NULL,
      PRIMARY KEY(asset_id,asset_revision),
      FOREIGN KEY(asset_id,asset_revision) REFERENCES asset_revisions);
    CREATE TABLE IF NOT EXISTS media_executions(
      job_id TEXT PRIMARY KEY REFERENCES jobs, project_id TEXT NOT NULL REFERENCES projects,
      asset_id TEXT NOT NULL, asset_revision TEXT NOT NULL,
      worker_relative_path TEXT NOT NULL, limits_json TEXT NOT NULL,
      state TEXT NOT NULL, received_bytes INTEGER NOT NULL DEFAULT 0,
      reserved_bytes INTEGER NOT NULL DEFAULT 0, raw_json TEXT,
      bundle_id TEXT, reason TEXT, updated_at TEXT NOT NULL);
    ''')


def identity(path: Path):
    reject_links(path)
    stat = path.stat()
    if not path.is_file():
        raise ContractError('media source must be a regular file')
    return {'size_bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,'device':stat.st_dev,'inode':stat.st_ino,'basis':'local_stat_snapshot_not_full_content_hash'}


class MediaRegistration:
    def __init__(self, store):
        self.store, self.db = store, store.connection

    def record(self, ref):
        return self.db.execute('SELECT * FROM media_sources WHERE asset_id=? AND asset_revision=?', asset_key(ref)).fetchone()

    def register(self, project_id, path):
        from .store import _write, _readonly, new_id
        root = self.store.project_root(project_id)
        root.mkdir(parents=True,exist_ok=True)
        source_path = Path(path).absolute()
        reject_links(source_path)
        if not source_path.resolve(strict=True).is_relative_to(self.store.root.resolve(strict=True)):
            raise ContractError('media input must remain inside the authorized local workspace')
        source_path = source_path.resolve(strict=True)
        media_type = mimetypes.guess_type(source_path.name)[0] or ''
        if not media_type.startswith(('audio/','video/')):
            raise ContractError('bounded media route requires a media input; no bytes read')
        snapshot = identity(source_path)
        with self.store.transaction():
            source = self.db.execute('SELECT * FROM sources WHERE project_id=? AND local_path=?',(project_id,str(source_path))).fetchone()
            if source and source['kind'] != 'local_media_stat':
                raise ContractError('source path already has a different registration basis')
            source_id = source['source_id'] if source else new_id('source')
            if not source:
                self.db.execute('INSERT INTO sources VALUES(?,?,?,?,?)',(source_id,project_id,'local_media_stat',str(source_path),now()))
            revision_digest = digest([source_id,snapshot])
            source_revision, revision = 'source-stat-'+revision_digest, 'asset-stat-'+revision_digest
            prior = self.db.execute('SELECT asset_id FROM source_assets WHERE source_id=? LIMIT 1',(source_id,)).fetchone()
            asset_id = prior[0] if prior else new_id('asset')
            self.db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?)',(asset_id,project_id,now()))
            relative = f'原始材料/{asset_id}/{revision}.source.json'
            descriptor = {'schema':'worldanalysis.media-source-descriptor.v1','source_ref':source_id,'asset_ref':{'asset_id':asset_id,'asset_revision':revision},'identity':snapshot,'declared_media_type':media_type,'media_type_basis':'filename guess; actual bounded decoder probe is preserved separately','source_content_sha256':None,'source_copy_retained':False,'stored_sha256_scope':'this registration descriptor only','replay_requires_current_stat_and_scope_check':True}
            data = canonical(descriptor)
            destination = safe_path(root,relative,must_exist=False)
            if destination.exists():
                if destination.read_bytes() != data:
                    raise ContractError('media descriptor collision; no overwrite')
            else:
                _write(destination,data); _readonly(destination)
            sha,size = hash_file(destination)  # descriptor, never source media
            self.db.execute('INSERT OR IGNORE INTO source_revisions VALUES(?,?,?,?,?)',(source_id,source_revision,sha,snapshot['size_bytes'],now()))
            self.db.execute('INSERT OR IGNORE INTO asset_revisions VALUES(?,?,?,?,?,?,?)',(asset_id,revision,sha,snapshot['size_bytes'],media_type,relative,now()))
            self.db.execute('INSERT OR IGNORE INTO source_assets VALUES(?,?,?,?)',(source_id,source_revision,asset_id,revision))
            self.db.execute('INSERT OR IGNORE INTO media_sources VALUES(?,?,?,?,?,?,?,?,?)',(asset_id,revision,source_id,source_revision,canonical(snapshot).decode(),relative,sha,snapshot['size_bytes'],now()))
            self.store.event('media_source_stat_observed',source_id,project_id,{'asset_id':asset_id,'asset_revision':revision,'source_content_hash':'not_performed','descriptor_sha256':sha})
        registered_type = self.db.execute('SELECT media_type FROM asset_revisions WHERE asset_id=? AND asset_revision=?',(asset_id,revision)).fetchone()[0]
        return {'asset':{'asset_id':asset_id,'asset_revision':revision,'source_ref':source_id,'media_type':registered_type,'size_bytes':snapshot['size_bytes'],'source_binding':{'kind':'local','local_input_ref':source_id}},'original_relative_path':relative,'original_sha256':sha,'original_kind':'inventory','original_media_type':'application/json','media_registration':True}

    def validate(self, project_id, asset, bound, *, require_current=False):
        row = self.record(asset)
        if not row or row['source_id'] != asset['source_ref'] or bound is None:
            raise ContractError('media source registration missing or wrong source')
        root = self.store.project_root(project_id)
        descriptor_path = safe_path(root,row['descriptor_relative_path'])
        sha,size = hash_file(descriptor_path)
        descriptor = json.loads(descriptor_path.read_bytes())
        snapshot = json.loads(row['identity_json'])
        source_name = self.db.execute('SELECT local_path FROM sources WHERE source_id=?',(row['source_id'],)).fetchone()[0]
        declared_type = mimetypes.guess_type(source_name)[0] or ''
        expected = {'schema':'worldanalysis.media-source-descriptor.v1','source_ref':row['source_id'],'asset_ref':{k:asset[k] for k in ('asset_id','asset_revision')},'identity':snapshot,'declared_media_type':declared_type,'media_type_basis':'filename guess; actual bounded decoder probe is preserved separately','source_content_sha256':None,'source_copy_retained':False,'stored_sha256_scope':'this registration descriptor only','replay_requires_current_stat_and_scope_check':True}
        if descriptor != expected or sha != row['descriptor_sha256'] or sha != bound['sha256'] or bound['original_relative_path'] != row['descriptor_relative_path'] or asset['size_bytes'] != row['original_size_bytes'] or bound['size_bytes'] != row['original_size_bytes']:
            raise ContractError('media descriptor does not match its registered identity')
        if require_current:
            self.current_path(project_id,asset)
        return row

    def current_path(self, project_id, asset):
        row = self.record(asset)
        source = self.db.execute('SELECT local_path FROM sources WHERE source_id=? AND project_id=? AND kind=?',(asset['source_ref'],project_id,'local_media_stat')).fetchone()
        if not row or not source:
            raise ContractError('unknown registered media source')
        path = Path(source[0]).absolute()
        reject_links(path)
        if not path.resolve(strict=True).is_relative_to(self.store.root.resolve(strict=True)) or identity(path) != json.loads(row['identity_json']):
            raise ContractError('media source revision or authorized path changed')
        return path

    def output_limit(self, entry):
        row = self.db.execute('SELECT * FROM media_executions WHERE job_id=?',(entry.get('job_id'),)).fetchone()
        if not row or (row['asset_id'],row['asset_revision']) != asset_key(entry['asset']):
            raise ContractError('media extraction budget is not registered')
        return int(json.loads(row['limits_json'])['final_evidence_byte_cap'])

    def _materialize(self, project_id, entry, job, raw, worker, limits):
        from .store import _copy, new_id
        view = self.store.root/'下载暂存'/job['job_id']/new_id('media-publication-view')
        reject_links(view); view.mkdir(parents=True,exist_ok=False)
        seen, total = set(),0
        for file in raw['files']:
            relative = file['relative_path']
            if relative.casefold() in seen:
                raise ContractError('duplicate media raw file path')
            seen.add(relative.casefold())
            src = safe_path(worker,relative)
            sha,size = hash_file(src)
            if file.get('sha256') != sha or file.get('byte_length') != size:
                raise ContractError('media worker artifact bytes mismatch')
            total += size
            if total > int(limits['final_evidence_byte_cap']):
                raise ContractError('media final evidence byte budget exceeded')
            _copy(src,safe_path(view,relative,must_exist=False))
        entry.update(raw=raw,output_dir=view,job_id=job['job_id'],media_limits=limits)
        return entry

    def execute(self, project_id, entry, job, extractor, limits, *, resume=False):
        root = self.store.project_root(project_id)
        ref = entry['asset']
        config = canonical(limits).decode()
        with self.store.transaction():
            previous = self.db.execute('SELECT * FROM media_executions WHERE job_id=?',(job['job_id'],)).fetchone()
            if previous and (previous['project_id'] != project_id or (previous['asset_id'],previous['asset_revision']) != asset_key(ref) or previous['limits_json'] != config):
                raise ContractError('media execution/source/config conflict')
            if previous and previous['state'] in {'unknown_io','running'}:
                raise ContractError('media execution outcome/IO unknown; reconcile before retry')
            if previous and previous['bundle_id'] and (previous['state'] == 'complete' or not resume):
                return None,self.store.get_bundle(project_id,previous['bundle_id'])
            if previous and previous['raw_json'] and not previous['bundle_id']:
                self.current_path(project_id,ref)
                worker = safe_path(root,previous['worker_relative_path'],must_exist=False)
                if not worker.is_dir():
                    raise ContractError('prepared media worker evidence directory missing')
                # Publication/materialization can resume from a durable raw receipt,
                # without decoding or inference again and without new IO reservation.
                return self._materialize(project_id,entry,job,json.loads(previous['raw_json']),worker,limits),None
            if previous and previous['state'] != 'paused':
                raise ContractError('deterministic media stop requires a new reviewed configuration')
            worker_rel = previous['worker_relative_path'] if previous else '媒体工作/'+job['job_id']
            spent = previous['received_bytes'] if previous else 0
            remaining = int(limits['input_byte_cap']) - spent
            if remaining <= 0:
                raise ContractError('cumulative media input budget exhausted')
            self.db.execute('INSERT INTO media_executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET state=excluded.state,reserved_bytes=excluded.reserved_bytes,updated_at=excluded.updated_at',(job['job_id'],project_id,ref['asset_id'],ref['asset_revision'],worker_rel,config,'running',spent,remaining,previous['raw_json'] if previous else None,previous['bundle_id'] if previous else None,'Local attempt IO reserved before execution',now()))
            self.db.execute("UPDATE jobs SET state='running',updated_at=? WHERE job_id=? AND state!='verified_complete'",(now(),job['job_id']))
        source = self.current_path(project_id,ref)
        worker = safe_path(root,worker_rel,must_exist=False)
        run_options = dict(limits, input_byte_cap=remaining, allowed_root=str(self.store.root.resolve(strict=True)))
        reconciled = False
        try:
            raw = extractor(source,worker,options=run_options)
            self.current_path(project_id,ref)
            received = raw.get('metadata',{}).get('input_bytes_received')
            if type(received) is not int or received < spent or received - spent > remaining:
                raise ContractError('media worker did not provide reconcilable actual input-byte accounting')
            if not raw.get('blocks') or not raw.get('files'):
                raise ContractError('media worker returned no publishable real evidence')
            if len(canonical(raw)) > int(limits['max_result_bytes']):
                raise ContractError('media raw result byte budget exceeded')
            actual_type = raw.get('media_type','')
            if not actual_type.startswith(('audio/','video/')):
                raise ContractError('bounded decoder did not establish an actual media type')
            expected_identity = self.current_path(project_id,ref)
            source_stats = expected_identity.stat()
            observed_identity = raw.get('metadata',{}).get('source_identity')
            if not isinstance(observed_identity,dict) or Path(observed_identity.get('path','')).resolve() != expected_identity.resolve() or observed_identity.get('size') != source_stats.st_size or observed_identity.get('mtime_ns') != source_stats.st_mtime_ns:
                raise ContractError('decoder result belongs to a different source identity')
            # Persistent source integrity limitation is never hidden by ASR success.
            raw['gaps'].append({'kind':'semantic_gap','severity':'important','reason':'Original media remains a scoped local source; registration uses stat metadata, not a full content hash or immutable whole-media copy. Published derived files are independently hashed.','block_indexes':[]})
            raw['metadata']['source_registration'] = {'basis':'local_stat_snapshot_not_full_content_hash','source_ref':ref['source_ref'],'source_content_sha256':None,'whole_source_copy_retained':False}
            raw['processing_status'] = 'partial'
            paused = raw['enumeration_state'] != 'complete' and any('CHECKPOINT_PAUSE' in g['reason'] for g in raw['gaps'])
            state = 'paused' if paused else 'complete' if raw['enumeration_state'] == 'complete' else 'stopped'
            with self.store.transaction():
                if actual_type != ref['media_type']:
                    published = self.db.execute('SELECT 1 FROM media_executions WHERE asset_id=? AND asset_revision=? AND bundle_id IS NOT NULL LIMIT 1',(ref['asset_id'],ref['asset_revision'])).fetchone()
                    if published:
                        raise ContractError('decoded media type changed after publication; explicit reclassification required')
                    self.db.execute('UPDATE asset_revisions SET media_type=? WHERE asset_id=? AND asset_revision=?',(actual_type,ref['asset_id'],ref['asset_revision']))
                    ref['media_type'] = actual_type
                self.db.execute('UPDATE media_executions SET state=?,received_bytes=?,reserved_bytes=0,raw_json=?,reason=?,updated_at=? WHERE job_id=?', (state,received,canonical(raw).decode(),'Actual result/accounting reconciled; not a semantic completion claim',now(),job['job_id']))
            reconciled = True
            return self._materialize(project_id,entry,job,raw,worker,limits),None
        except Exception:
            with self.store.transaction():
                if reconciled:
                    self.db.execute('UPDATE media_executions SET reason=?,updated_at=? WHERE job_id=?',('Validated raw receipt retained; repair publication without repeating inference',now(),job['job_id']))
                else:
                    self.db.execute("UPDATE media_executions SET state='unknown_io',reason=?,updated_at=? WHERE job_id=?",('Attempt failed; IO reservation retained and no automatic replay',now(),job['job_id']))
            raise

    def mark_published(self, entries, bundle):
        with self.store.transaction():
            for entry in entries:
                self.db.execute('UPDATE media_executions SET bundle_id=?,updated_at=? WHERE job_id=?',(bundle['bundle_id'],now(),entry['job_id']))
