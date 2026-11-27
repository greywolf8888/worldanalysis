"""Bounded file-IPC worker for real agent-driven source-frame capture.

Only this worker owns the project OS lock. The browser agent supplies actual
JPEG/JSON then an empty .ready marker; this process validates and commits each.
No browser, network, ASR, new generation or full-media file handling occurs here.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import time
from uuid import uuid4
from .store import Store
from .contracts import ContractError, safe_path, canonical, now, check_id
from .cloud_lifecycle import CloudLifecycle, LifecycleConfig, RemoteContainer
from .platform_first import PlatformFirst
from .platform_execution import SourceFrameExecution, _MAX_RECEIPT
from .platform_identity import content_identity
from .platform_materials import _json, _read
from .policy import GateError


def _save(root, relative, value):
    path=safe_path(root,relative,must_exist=False)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as stream:
        stream.write(json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8')+b'\n')
        stream.flush();os.fsync(stream.fileno())
    return relative


def _runtime_cache_authorization(authorizer, context):
    """Observe cache evidence, not permission for an existing-source read.

    No producer means unmeasured, never a fabricated cache grant or zero-cache
    claim (taskbook 8.4). An explicit producer's denial/bad proof stays fatal.
    The separate normal-read authorization and raw-media output ban still apply.
    """
    if authorizer is None:
        return {'state':'unmeasured','browser_task_ref':context['browser_task_ref'],
                'cache_policy_known':False,'full_media_cache_absence_unproved':True,
                'raw_media_file_output_forbidden':True}
    if not callable(authorizer):
        raise GateError('BROWSER_MEDIA_CACHE_AUDIT_REQUIRED: explicit cache authorizer is not callable')
    proof=authorizer(context)
    required={'state','evidence_class','browser_task_ref','media_cache_policy','producer_ref','evidence_ref','receipt_sha256'}
    if (type(proof) is not dict or set(proof)!=required or proof['state']!='granted'
            or proof['evidence_class']!='runtime_host_readback'
            or proof['browser_task_ref']!=context['browser_task_ref']
            or proof['media_cache_policy']!='no_disk_media_storage'):
        raise GateError('BROWSER_MEDIA_CACHE_AUDIT_REQUIRED: no applicable independent runtime readback')
    check_id(proof['producer_ref']);check_id(proof['evidence_ref'])
    import re
    if not isinstance(proof['receipt_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',proof['receipt_sha256']):
        raise GateError('BROWSER_MEDIA_CACHE_AUDIT_REQUIRED: auditable host receipt fingerprint missing')
    return proof


def _save_observation(root, relative, value):
    """Append a restart observation; never replace the old handoff/result."""
    path = safe_path(root, relative, must_exist=False)
    if path.exists():
        relative = str(PurePosixPath(relative).with_suffix('')) + '-observation-' + uuid4().hex + path.suffix
    return _save(root, relative, value)


def _prior_handoff(root, prefix, task, source):
    path = safe_path(root, prefix + '-ready.json', must_exist=False)
    if not path.exists():
        return None
    old = _json(_read(path, 1024**2))
    if (old.get('schema') != 'worldanalysis.source-frame-worker-handoff.v1'
            or any(old.get(k) != task[k] for k in ('job_id', 'parent_job_id', 'helper_sha256', 'capture_root'))
            or content_identity(old.get('source', {}).get('account_ref'), old.get('source', {})) != content_identity(source['account_ref'], source)
            or not isinstance(old.get('requests'), list) or len(old['requests']) > 1000):
        raise ContractError('prior handoff does not match this source-frame execution')
    base = PurePosixPath(task['capture_root'])
    for request in old['requests']:
        if (not isinstance(request, dict) or set(request) != {'requested_ms', 'relative_path', 'receipt_relative_path', 'ready_marker'}
                or type(request['requested_ms']) is not int or request['requested_ms'] not in task['requested_ms']):
            raise ContractError('invalid prior handoff request')
        for key, suffixes in (('relative_path', {'.jpg', '.jpeg'}), ('receipt_relative_path', {'.json'}), ('ready_marker', {'.ready'})):
            safe_path(root, request[key], must_exist=False)
            if not PurePosixPath(request[key]).is_relative_to(base) or PurePosixPath(request[key]).suffix.lower() not in suffixes:
                raise ContractError('prior handoff request escaped its immutable capture root')
        if PurePosixPath(request['ready_marker']).with_suffix('.json').as_posix() != request['receipt_relative_path']:
            raise ContractError('prior handoff marker/receipt disagree')
    return old


def _recover_local_results(executor, task):
    """Adopt fsynced ready results before requesting ANY new media permission."""
    root = executor.store.root
    base = safe_path(root, task['capture_root'], must_exist=False)
    pending = set(executor.pending_capture_targets(task['job_id'])['pending'])
    adopted, invalid = [], []
    if not pending or not base.exists():
        return {'adopted': adopted, 'invalid_local_results': invalid}
    for count, marker in enumerate(base.rglob('*.ready'), 1):
        if count > 10000:
            raise ContractError('bounded local ready-result scan exceeded')
        relative = marker.relative_to(root).as_posix()
        try:
            marker = safe_path(root, relative)
            if not marker.is_file() or marker.stat().st_size != 0 or marker.stat().st_nlink != 1:
                raise ContractError('ready marker must be an empty unlinked regular file')
            receipt = PurePosixPath(relative).with_suffix('.json').as_posix()
            receipt_path = safe_path(root, receipt)
            if receipt_path.stat().st_nlink != 1:
                raise ContractError('hardlinked frame receipt is forbidden')
            candidate = _json(_read(receipt_path, _MAX_RECEIPT))
            if not isinstance(candidate, dict) or type(candidate.get('requested_ms')) is not int:
                raise ContractError('bounded frame receipt target required')
            if candidate['requested_ms'] not in pending:
                continue
            # A self-reported target is only a filter. accept_frame performs
            # full source/proof/bytes/JPEG validation ONCE; only its verified
            # progress, not the receipt's claim, can remove a pending target.
            after = executor.accept_frame(task['job_id'], receipt)
            current_pending = set(after['pending'])
            adopted.extend(sorted(pending - current_pending))
            pending = current_pending
            if not pending:
                break
        except GateError:
            raise
        except (ContractError, OSError, KeyError, TypeError) as exc:
            # Keep incomplete/invalid IPC and orphan JPGs untouched as evidence.
            invalid.append({'ready_marker': relative, 'reason': str(exc)[:300]})
    return {'adopted': adopted, 'invalid_local_results': invalid}


def _new_requests(task, targets):
    attempt = task['capture_root'] + '/attempts/' + uuid4().hex
    return [{'requested_ms': t, 'relative_path': attempt + '/frames/' + str(t) + '.jpg',
             'receipt_relative_path': attempt + '/receipts/' + str(t) + '.json',
             'ready_marker': attempt + '/receipts/' + str(t) + '.ready'} for t in targets]


def _resume_capture(executor, task, *, root, source, handoff_prefix, runtime_authorizer, context, batch_limit):
    prior = _prior_handoff(root, handoff_prefix, task, source)
    local = _recover_local_results(executor, task)
    progress = executor.pending_capture_targets(task['job_id'])
    recovery = {'job_id': task['job_id'], 'observed_at': now(), 'prior_handoff_rechecked': prior is not None,
                **local, 'progress': progress, 'quality_accepted': False}
    # Even a blocked restart leaves an append-only local observation receipt.
    recovery_path = _save_observation(root, handoff_prefix + '-recovery.json', recovery)
    started = {'may_capture': False, 'may_send': False}
    requests = []
    cache_observation = None
    if progress['pending']:
        cache_observation = _runtime_cache_authorization(runtime_authorizer, context)
        _, info = executor._job(task['job_id'])
        executor.store.event('source_frame_runtime_cache_observation', task['job_id'], info['project_id'], cache_observation)
        started = executor.start(task['job_id'])
        if started['may_capture']:
            requests = _new_requests(task, progress['pending'][:batch_limit])
    return {'progress': progress, 'started': started, 'requests': requests, 'recovery': recovery_path,
            'runtime_cache_observation': cache_observation, **local}


def main(argv=None, *, runtime_authorizer=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',default='.')
    p.add_argument('--config',required=True)
    p.add_argument('--run',required=True)
    p.add_argument('--file',required=True)
    p.add_argument('--targets',required=True)
    p.add_argument('--helper',default='_tools/browser-source-frame.js')
    p.add_argument('--handoff-prefix',required=True)
    p.add_argument('--batch-limit',type=int,default=3)
    p.add_argument('--timeout-seconds',type=int,default=900)
    args=p.parse_args(argv)
    root=Path(args.root).resolve()
    if not 1<=args.batch_limit<=1000 or not 1<=args.timeout_seconds<=1800:
        raise ContractError('bounded worker limits required')
    load=lambda rel:json.loads(safe_path(root,rel).read_text(encoding='utf-8'))
    raw=load(args.config)
    cfg=LifecycleConfig(account_ref=raw['account_ref'],session_id=raw['session_id'],
        containers={k:RemoteContainer(**v) for k,v in raw['containers'].items()},
        protected_remote_ids=frozenset(raw['protected_remote_ids']),protected_paths=tuple(raw['protected_paths']))
    targets=load(args.targets)
    if targets['file_id']!=args.file:raise ContractError('target file identity mismatch')
    project_id=None
    def authorize(job):
        provider=json.loads(job['provider_json'])
        if (job['project_id']==project_id and job['operation']=='capture-source-frames'
                and provider.get('provider_id')=='browser-source-frame'
                and job['estimated_cost_minor']==0 and job['max_cost_minor']==0):
            return {'state':'granted','job_id':job['job_id'],'project_id':project_id,
                    'grant_ref':'user-taskbook-20261116-normal-existing-video-read'}
        return {'state':'denied'}
    with Store(root,submission_authorizer=authorize) as store:
        coord=PlatformFirst(store); run=coord.status(args.run,verify_artifacts=False)
        project_id=run['project_id']
        if run['inventory']['account_ref']!=cfg.account_ref:raise GateError('ACCOUNT_MISMATCH')
        row=store.connection.execute('SELECT folder_id FROM cloud_lifecycle_projects WHERE project_id=? AND account_ref=?',(project_id,cfg.account_ref)).fetchone()
        if row is None:raise GateError('CLOUD_PROJECT_BINDING_REQUIRED')
        flow=CloudLifecycle(store,cfg);record=flow.get_project(row['folder_id'])
        if record['owner_session']!=cfg.session_id or record['state']!='extracting':
            raise GateError('PROJECT_NOT_OWNED_AND_EXTRACTING')
        with flow.work_lock(row['folder_id']) as lease:
            flow._permission(record)
            executor=SourceFrameExecution(store,coord)
            task=executor.create(args.run,args.file,targets['requested_ms'],helper_path=args.helper,
                target_evidence_ref=targets['evidence_ref'],selection_basis=targets['selection_basis'])
            item=next(x for x in run['inventory']['files'] if x['file_id']==args.file)
            source={'account_ref':cfg.account_ref,**item}
            resumed=_resume_capture(executor,task,root=root,source=source,handoff_prefix=args.handoff_prefix,
                runtime_authorizer=runtime_authorizer,batch_limit=args.batch_limit,
                context={'browser_task_ref':cfg.session_id,'account_ref':cfg.account_ref,'job_id':task['job_id'],'full_media_disk_write_allowed':False})
            pending=resumed['progress']; started=resumed['started']; requests=resumed['requests']
            if not requests:
                status=executor.status(task['job_id'])
                result={'status': 'verified_complete_local_reuse' if status['quality_accepted'] else
                        'awaiting_independent_quality_review' if not pending['pending'] else 'no_capture_permitted',
                    'job_id':task['job_id'],'pid':os.getpid(),'progress':pending,'recovery':resumed['recovery'],
                    'quality_accepted':status['quality_accepted'],'adopted_local_targets':resumed['adopted'],
                    'runtime_cache_observation':resumed['runtime_cache_observation']}
                if not pending['pending']:result['manifest']=executor.prepare_manifest(task['job_id'])
                result_path=_save_observation(root,args.handoff_prefix+'-result.json',result)
                print(json.dumps({**result,'result':result_path},ensure_ascii=False),flush=True)
                return 0
            batch=[r['requested_ms'] for r in requests]
            ready={'schema':'worldanalysis.source-frame-worker-handoff.v1','job_id':task['job_id'],
                'parent_job_id':task['parent_job_id'],'pid':os.getpid(),'started_at':now(),
                'may_capture':True,'may_send':started['may_send'],'source':source,
                'helper_sha256':task['helper_sha256'],'capture_root':task['capture_root'],
                'requests':requests,'reused_frames':pending['valid_frames'],'pending_total':len(pending['pending']),
                'runtime_cache_observation':resumed['runtime_cache_observation'],
                'worker_scope':'project_os_lock_held_until_batch_committed_or_timeout'}
            ready_path=_save_observation(root,args.handoff_prefix+'-ready.json',ready)
            print(json.dumps({'status':'ready','job_id':task['job_id'],'pid':os.getpid(),
                'ready':ready_path,'targets':batch,'reused_frames':len(pending['valid_frames']),
                'may_send':started['may_send'],'may_capture':True},ensure_ascii=False),flush=True)
            accepted=[];deadline=time.monotonic()+args.timeout_seconds
            for request in requests:
                marker=safe_path(root,request['ready_marker'],must_exist=False)
                # Bounded IPC wait, not a page-readiness sleep or repeated media work.
                while not marker.exists():
                    if time.monotonic()>=deadline:
                        result={'status':'awaiting_browser_input','job_id':task['job_id'],'pid':os.getpid(),
                            'accepted_this_process':accepted,'progress':executor.pending_capture_targets(task['job_id']),
                            'runtime_cache_observation':resumed['runtime_cache_observation']}
                        _save_observation(root,args.handoff_prefix+'-result.json',result)
                        print(json.dumps({'status':result['status'],'job_id':task['job_id'],'pid':os.getpid()},ensure_ascii=False),flush=True)
                        return 0
                    time.sleep(0.2)
                executor.accept_frame(task['job_id'],request['receipt_relative_path'])
                accepted.append(request['requested_ms'])
                print(json.dumps({'accepted':request['requested_ms'],'job_id':task['job_id'],'pid':os.getpid()},ensure_ascii=False),flush=True)
            progress=executor.pending_capture_targets(task['job_id'])
            result={'status':'real_partial_checkpoint' if progress['pending'] else 'awaiting_independent_quality_review',
                'job_id':task['job_id'],'pid':os.getpid(),'accepted_this_process':accepted,'progress':progress,
                'runtime_cache_observation':resumed['runtime_cache_observation']}
            if not progress['pending']:result['manifest']=executor.prepare_manifest(task['job_id'])
            _save_observation(root,args.handoff_prefix+'-result.json',result)
            print(json.dumps({'status':result['status'],'job_id':task['job_id'],'pid':os.getpid(),
                'accepted_count':len(accepted),'remaining':len(progress['pending'])},ensure_ascii=False),flush=True)
            return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ContractError,GateError,OSError,ValueError) as exc:
        print(json.dumps({'status':'blocked','reason':str(exc),'remote_send_performed_by_worker':False},ensure_ascii=False),flush=True)
        raise SystemExit(2)
