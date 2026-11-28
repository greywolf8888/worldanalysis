"""Confirmation-transport validation and review preparation, with no writer or executor."""
from __future__ import annotations
from datetime import datetime,timezone
from hashlib import sha256
from pathlib import PurePosixPath
from catalog_model import compact,digest
from production_bridge import stamp
from v4_exchange import validate_confirmed_shape,validate_manifest,selection_sha256

AUTH_DIR='_exchange/dsh/authorizations/'


def ordered_items_sha256(items):
    return digest([{'queue_item_id':r['queue_item_id'],'item_rank':r['item_rank']}
                   for r in sorted(items,key=lambda r:r['item_rank'])])


def receipt_binds(plan, authorization_bytes):
    """Protocol match, not proof of the actual user. Caller's host attestation is separate."""
    if not validate_confirmed_shape(plan) or not isinstance(authorization_bytes,bytes) or len(authorization_bytes)>16384:return False
    ref=plan['authorization_ref']; name=PurePosixPath(ref['path']).name
    if ref['path']!=AUTH_DIR+name or not name.endswith('.json') or sha256(authorization_bytes).hexdigest()!=ref['sha256']:return False
    import json
    try:r=json.loads(authorization_bytes)
    except (TypeError,ValueError,UnicodeError):return False
    if (type(r) is not dict or r.get('demo') is True or type(r.get('schema_version')) is not int or r['schema_version']!=1
            or r.get('record_type')!='user_confirmation' or r.get('producer')!='dsh-ui' or r.get('record_id')!=name[:-5]
            or r.get('state')!='confirmed' or r.get('execution_allowed') is not True or r.get('confirmation_actor')!='user'
            or type(r.get('revision')) is not int or type(r.get('order_revision')) is not int or type(r.get('confirmation_seq')) is not int
            or r['revision']!=plan['order_revision'] or not stamp(r.get('observed_at'))):return False
    return (all(r.get(k)==plan.get(k) for k in ('batch_id','confirmation_seq','confirmed_at','order_mode','order_revision','selection_sha256'))
            and all(type(r.get(k)) is type(plan.get(k)) and r.get(k)==plan.get(k) and (k in r)==(k in plan)
                    for k in ('batch_rank','global_order_locked'))
            and r.get('ordered_items_sha256')==ordered_items_sha256(plan['items']))


def fresh(proof,maximum,clock):
    t=stamp(proof.get('observed_at')) if isinstance(proof,dict) else None
    return bool(t and 0<=(clock-t).total_seconds()<=maximum)


def inspect_for_original_executor(plan,auth_bytes,*,host_attestation_id=None,trusted_host_check=None,load_manifest=None,observe_exact=None,capacity=None,
                                  duplicate_block=None,crawl=None,clock=None,floor=100_000_000_000):
    """Validate a frozen invocation only; neither claim nor call the original runner.

    A future wrapper must supply the DSH-confirmed actual actor and run the existing
    journal/account/occupied-name checks again immediately before prepare/claim/save.
    Never accept the facts, attestation, callbacks or paths as HTTP input.
    """
    no=lambda r:{'admission':'blocked','code':r,'execution_allowed':False,'confirmed_items':[]}
    if not receipt_binds(plan,auth_bytes):return no('USER_RECEIPT_SCOPE_OR_ORDER_UNPROVEN')
    context={'receipt_sha256':plan['authorization_ref']['sha256'],'batch_id':plan['batch_id'],'order_revision':plan['order_revision'],
             'selection_sha256':plan['selection_sha256'],'ordered_items_sha256':ordered_items_sha256(plan['items'])}
    if (not isinstance(host_attestation_id,str) or not host_attestation_id or not callable(trusted_host_check) or trusted_host_check(host_attestation_id,context) is not True):
        return no('NO_TRUSTED_DESKTOP_USER_DECISION')
    clock=clock or datetime.now(timezone.utc)
    if (not isinstance(crawl,dict) or crawl.get('phase')!='paused' or crawl.get('platform_cleared') is not True
          or crawl.get('original_session_account') is not True or crawl.get('no_other_coordinator') is not True):return no('ORIGINAL_COORDINATOR_OR_PLATFORM_HOLD')
    if not fresh(capacity,30,clock) or capacity.get('account_matches') is not True or capacity.get('evidence_class')!='normal_ui_dom':return no('FRESH_ACTUAL_ACCOUNT_CAPACITY_REQUIRED')
    free=capacity.get('remaining_bytes')
    if type(floor) is not int or floor<=0 or capacity.get('minimum_free_bytes')!=floor or type(free) is not int or free<=floor:return no('CAPACITY_RESERVE_HIT_OR_UNKNOWN')
    if len(plan['items'])>4 or not all(callable(x) for x in (load_manifest,observe_exact,duplicate_block)):return no('BOUNDED_EXACT_FACT_BINDINGS_REQUIRED')
    seen=set(); planned=[]; cost=0
    for x in plan['items']:
        ref=x['range_manifest_ref']; raw=load_manifest(ref)
        if not isinstance(raw,bytes) or len(raw)>3*1024*1024 or sha256(raw).hexdigest()!=ref['sha256']:return no('MISSING_OR_CHANGED_RANGE')
        import json
        try:m=json.loads(raw)
        except (TypeError,ValueError,UnicodeError):return no('RANGE_PARSE_FAILED')
        if (not validate_manifest(m) or m.get('demo') is True or m['scope']!='recursive' or m.get('complete_recursive') is not True
                or any(m.get(k)!=x[k] for k in ('source_id','source_revision'))):return no('EXACT_RECURSIVE_SCOPE_REQUIRED')
        f=observe_exact(x)
        if (not fresh(f,30,clock) or f.get('evidence_class') not in ('normal_ui_dom','normal_ui_read_response')
                or f.get('account_matches') is not True or f.get('source_root_count_verified') is not True
                or f.get('source_document_fresh') is not True or f.get('target_list_complete') is not True or f.get('target_collision') is not False
                or f.get('original_root_count',0)!=m.get('root_count') or type(m.get('root_count')) is not int or m['root_count']<1
                or f.get('destination_binding_verified') is not True or f.get('range_manifest_sha256')!=ref['sha256']
                or any(f.get(k)!=x[k] for k in ('resource_version_id','site_id','article_id','source_id','source_revision'))):return no('LIVE_ROOT_SCOPE_OR_TARGET_DRIFT')
        if x['source_id'] in seen or duplicate_block(x,f) is not False:return no('SOURCE_OR_TARGET_PREVIOUSLY_RESERVED_OR_CLAIMED')
        seen.add(x['source_id']);cost+=sum(a['bytes'] for a in m['files']);planned.append(x['transfer_task_id'])
    if free-cost<=floor:return no('PACKAGE_SCOPE_CROSSES_RESERVE')
    # This is a boundary report only. No automatic production execution in this task.
    return {'admission':'preflight_matches','code':'HAND_OFF_TO_ORIGINAL_COORDINATOR_FOR_INDEPENDENT_RECHECK',
            'execution_allowed':False,'confirmed_items':planned,'selected_bytes':cost,'ordered_items_sha256':context['ordered_items_sha256']}


def readiness(library,bridge,draft):
    ordered=[]
    for rank,k in enumerate(draft['ids'],1):
        r=library.by_id.get(k); bind=draft['bindings'].get(k); a=r or {}
        stale=not r or not isinstance(bind,dict) or any(a.get(p)!=bind.get(p) for p in ('source_id','source_revision','article_revision','site_id','article_id','scope'))
        why=['STALE_VERSION' if stale else 'SOURCE_SCOPE_AND_TOTAL_BYTES_UNVERIFIED','NO_FRESH_ACCOUNT_CAPACITY','NO_TRUSTED_DESKTOP_CONFIRMATION']
        owned_location=library.share_locations.get((a.get('site_id'),a.get('article_id')),())
        has_bound_owner_share=(a.get('requestable') is True and not a.get('action_pending')
                               and len(owned_location)==1 and 'owner_export' in owned_location[0].origins)
        if not a.get('selectable') and not has_bound_owner_share:why.append('NOT_ELIGIBLE_OR_PRIOR_RESERVATION')
        ordered.append({'item_rank':rank,'resource_version_id':k,'article_id':a.get('article_id'),'title':a.get('title'),'reasons':why,'range_manifest_ref':None,'range_total_bytes':None})
    examples=[]
    for r in library.match({'order':'newest'},set()):
        if len(examples)>=2:break
        if (r.get('site_id')=='wangzjk.com' and r.get('published_at')=='2026-11-25' and r.get('selectable') is True and not r.get('operations') and r.get('weak_candidate_count')==0):
            examples.append({'article_id':r['article_id'],'resource_version_id':r['id'],'title':r['title'],'url':r['url'],'state':'draft_candidate_only_no_live_preflight'})
    return {'state':'await_verified_scope_and_user','snapshot_id':library.snapshot_id,'draft_revision':draft['revision'],
            'draft_order_sha256':digest([library.snapshot_id,draft['revision'],[(x['item_rank'],x['resource_version_id']) for x in ordered]]),
            'ordered_items':ordered,'eligible_for_confirmation':0,'actual_remaining_bytes':None,'minimum_free_bytes':getattr(library,'minimum_free_bytes',100_000_000_000),
            'confirmation_channel':'codex_control_record_of_actual_user_reply','candidate_examples_not_selected':examples,'platform_hold_present':bool(bridge.crawl.get('global_holds')),
            'confirmation_actor':None,'authorization_ref':None,'execution_allowed':False,'original_executor_invoked':False}
