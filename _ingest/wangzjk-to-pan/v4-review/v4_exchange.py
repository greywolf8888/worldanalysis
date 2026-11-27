"""Interchange v1 shape and read-only export; this task has no execution authorization."""
from datetime import datetime
from hashlib import sha256
from pathlib import PurePosixPath
import re
from catalog_model import compact,now

HEX=re.compile(r'[a-f0-9]{64}\Z')
OPAQUE=re.compile(r'[A-Za-z0-9_.:-]{1,200}\Z')
ITEM={'queue_item_id','resource_version_id','site_id','article_id','source_id','source_revision','range_manifest_ref','item_rank','transfer_task_id'}
REF_PREFIX=('_exchange/dsh/catalog/', '_exchange/dsh/plans/', '_exchange/dsh/authorizations/', '_exchange/control/decisions/', '_ingest/wangzjk-to-pan/v4-review/evidence/')


def selection_sha256(items):
    frozen=[{k:v for k,v in x.items() if k!='item_rank'} for x in sorted(items,key=lambda r:r['queue_item_id'])]
    return sha256(compact(frozen)).hexdigest()


def relative_file(name):
    if not isinstance(name,str) or not name or len(name)>1400 or re.search(r'[\\:\x00-\x1f\x7f]',name):return False
    p=PurePosixPath(name)
    return not p.is_absolute() and p.as_posix()==name and all(x not in ('','.','..') and not x.endswith((' ','.')) for x in p.parts)


def ref(v):
    return (isinstance(v,dict) and set(v)=={'path','sha256'} and relative_file(v['path'])
            and v['path'].startswith(REF_PREFIX) and isinstance(v['sha256'],str) and HEX.fullmatch(v['sha256']) is not None)


def timestamp(s):
    try:return isinstance(s,str) and datetime.fromisoformat(s.replace('Z','+00:00')).utcoffset() is not None
    except ValueError:return False


def validate_manifest(x):
    if (type(x) is not dict or x.get('schema_version')!=1 or x.get('record_type')!='range_manifest'
        or x.get('scope') not in ('recursive','selected_roots_only') or not all(isinstance(x.get(k),str) and OPAQUE.fullmatch(x[k]) for k in ('source_id','source_revision'))
        or not isinstance(x.get('files'),list)):
        return False
    if x['scope']=='recursive' and (not x['files'] or x.get('complete_recursive') is not True):return False
    files=set()
    for f in x['files']:
        if type(f) is not dict or set(f)!={'file','bytes'} or not relative_file(f['file']) or type(f['bytes']) is not int or f['bytes']<0 or f['file'] in files:return False
        files.add(f['file'])
    return True


def validate_confirmed_shape(p):
    if (type(p) is not dict or p.get('schema_version')!=1 or p.get('record_type')!='confirmed_plan' or p.get('producer')!='dsh'
            or p.get('state')!='confirmed' or p.get('execution_allowed') is not True or p.get('demo') is True
            or not ref(p.get('authorization_ref')) or not timestamp(p.get('confirmed_at'))
            or not isinstance(p.get('batch_id'),str) or not OPAQUE.fullmatch(p['batch_id'])
            or p.get('order_mode') not in ('ready_ordered','strict')):return False
    if any(type(p.get(k)) is not int or p[k]<1 for k in ('confirmation_seq','revision','order_revision')) or p['revision']!=p['order_revision']:return False
    if 'batch_rank' in p and (p.get('global_order_locked') is not True or type(p['batch_rank']) is not int or p['batch_rank']<1):return False
    rows=p.get('items');ids=set(); tasks=set(); versions=set()
    if not isinstance(rows,list) or not 1<=len(rows)<=5000:return False
    for index,r in enumerate(rows,1):
        if (type(r) is not dict or not ITEM<=r.keys() or type(r.get('item_rank')) is not int or r['item_rank']!=index
                or not ref(r['range_manifest_ref']) or not all(isinstance(r[k],str) and OPAQUE.fullmatch(r[k]) for k in ITEM-{'range_manifest_ref','item_rank'})):
            return False
        if r['queue_item_id'] in ids or r['transfer_task_id'] in tasks or r['resource_version_id'] in versions:return False
        ids.add(r['queue_item_id']); tasks.add(r['transfer_task_id']); versions.add(r['resource_version_id'])
    return p.get('selection_sha256')==selection_sha256(rows)


def review_execution_admission(record):
    # A valid shape is never an authority. This module has no executor callback.
    shape=validate_confirmed_shape(record)
    return {'checked_schema':shape,'execution_allowed':False,'reason':
            'NO_TASK_EXECUTION_AUTHORIZATION' if shape else 'NO_VALID_LIVE_CONFIRMED_PLAN'}


def observation(library,task_id):
    return {'schema_version':1,'record_type':'catalog_observation','producer':'dsh','event_id':task_id+'-catalog-observation',
        'record_id':task_id+'-catalog-observation','task_id':task_id,'revision':1,'observed_at':now(),'state':'snapshot_observed',
        'execution_allowed':False,'snapshot_id':library.snapshot_id,'totals':library.meta['totals'],'upstream_sha256':library.provenance,
        'sites':[x['checkpoint'] for x in library.sites],
        'items':[{k:v[k] for k in ('id','project_id','site_id','article_id','article_revision','source_id','source_revision','category','status','scope','detail_state','selectable')}
            | {'resource_version_id':v['id'],'aliases':[{'site_id':a['site_id'],'article_id':a['article_id'],'article_revision':a['article_revision']} for a in v['sources']]} for v in library.by_id.values()],
        'historical_transfer_evidence_scope':'selected_roots_only_when_proven_not_current_storage_or_complete_download',
        'range_manifest_available_for_new_transfers':False,'live_confirmed_plan_published':False,'official_transfer_result_published':False}
