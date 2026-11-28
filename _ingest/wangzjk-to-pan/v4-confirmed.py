"""Exact V4 plan, provenance and original-lease policy. No browser or HTTP code."""
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
from pathlib import Path
import re
import sys
from unicodedata import normalize

BRANCH=Path(__file__).resolve().parent
WORKSPACE=BRANCH.parents[1]
sys.path[:0]=[str(BRANCH/'v4-review'),str(BRANCH)]
from catalog_model import compact, digest, ORIGIN, SITE, TARGET, source_key
from v4_exchange import validate_confirmed_shape, validate_manifest, selection_sha256, relative_file
from confirm_gate import ordered_items_sha256, receipt_binds, fresh
from lib.journal import Journal, root_name_collision_key
from lib.crawl import CrawlQueue

EXECUTOR_ROUTE='.runtime/dsh-worldanalysis/transfer-route.json'
DEDICATED_BINDING='.runtime/dsh-worldanalysis/desktop-session-readback.json'
TASKS='_exchange/control/tasks/'
DECISIONS='_exchange/control/decisions/'
TRANSFER_REQUESTS='_exchange/control/transfer-requests/'
RECEIPTS='_exchange/dsh/receipts/'
AUTH='_exchange/dsh/authorizations/'
PLANS='_exchange/dsh/plans/'
RANGES='_exchange/dsh/catalog/'
HEX=re.compile(r'[0-9a-f]{64}\Z')
KEY=re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}\Z')
ORDER_KEYS=('batch_rank','global_order_locked')
TYPES=('article_id','site_id','resource_version_id','source_id','source_revision')
FLOOR=100_000_000_000


def require(test,reason):
    if not test:raise ValueError(reason)


def receive_target(root=WORKSPACE):
    from workspace_paths import cloud_destination
    return cloud_destination(root)


def execution_route(root=WORKSPACE):
    path=root/EXECUTOR_ROUTE
    require(path.resolve()==path and not path.is_symlink(),'EXECUTOR_ROUTE_INVALID')
    try:
        with path.open('rb') as stream:
            raw=stream.read(4097)
    except FileNotFoundError:
        return {'schema_version':1,'mode':'paused','revision':0}
    require(len(raw)<=4096,'EXECUTOR_ROUTE_INVALID')
    route=json.loads(raw)
    require(type(route) is dict and route.get('schema_version')==1
            and type(route.get('revision')) is int and route['revision']>0
            and route.get('mode') in ('paused','dedicated'),'EXECUTOR_ROUTE_INVALID')
    return route


def execution_target(task_ref,root=WORKSPACE):
    route=execution_route(root)
    require(route['mode']=='dedicated','FORMAL_EXECUTOR_PAUSED')
    require(route.get('task_ref')==task_ref,'DEDICATED_EXECUTOR_ONLY_FOR_BOUND_TASK')
    binding,_=pin(route.get('desktop_binding_ref'),'.runtime/dsh-worldanalysis/',8192,root)
    binding_version = binding.get('schema_version', 1)
    root_bound = ((binding_version == 1 and Path(binding.get('project_path','')) == root)
                  or (binding_version == 2 and binding.get('project_path') == '.'))
    require(route['desktop_binding_ref']['path']==DEDICATED_BINDING
            and binding.get('purpose')=='read_only_preflight' and root_bound
            and isinstance(route.get('session_id'),str) and route['session_id']==binding.get('session_id')
            and re.fullmatch(r'session-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',route['session_id']),
            'DEDICATED_DESKTOP_BINDING_CHANGED')
    readback,_=pin(route.get('readback_ref'),'_exchange/dsh/receipts/',65_536,root)
    require(readback.get('session_id')==route['session_id'] and readback.get('producer')=='dsh'
            and readback.get('execution_allowed') is False,'DEDICATED_READBACK_CHANGED')
    browser=route.get('browser_binding')
    require(type(browser) is dict and set(browser)=={'browserSession','siteTab','panTab','accountTab'}
            and all(isinstance(value,str) and 0<len(value)<=128 for value in browser.values())
            and len({browser['siteTab'],browser['panTab'],browser['accountTab']})==3,'DEDICATED_BROWSER_BINDING_INVALID')
    return route['session_id'],browser


def pin(ref,prefix,limit=2_000_000,root=WORKSPACE):
    require(type(ref) is dict and set(ref)=={'path','sha256'},'EXACT_REFERENCE_REQUIRED')
    p,k=ref['path'],ref['sha256']; require(isinstance(p,str) and p.startswith(prefix) and relative_file(p)
        and isinstance(k,str) and HEX.fullmatch(k) and re.fullmatch(r'[a-zA-Z0-9_-]{1,160}\.json',p[len(prefix):]),'REFERENCE_OUTSIDE_PROTOCOL')
    f=root/p; require(f.resolve(strict=True)==f and f.is_file(),'REFERENCE_IS_NOT_REGULAR_FILE')
    with f.open('rb') as fd: raw=fd.read(limit+1)
    require(len(raw)<=limit and hmac.compare_digest(sha256(raw).hexdigest(),k),'REFERENCE_BYTES_CHANGED')
    d=json.loads(raw);require(type(d) is dict,'REFERENCE_JSON_OBJECT_REQUIRED');return d,raw


def bytes_for(o):return compact(o)+b'\n'


def reference(p,d):return {'path':p,'sha256':sha256(bytes_for(d)).hexdigest()}


def roots(rows):
    require(isinstance(rows,list) and 1<=len(rows)<=20,'COMPLETE_ORIGINAL_ROOTS_REQUIRED')
    seen=set(); names=set(); clean=[]
    for x in rows:
        i,n,k,b=(x.get(z) for z in ('source_id','name','is_dir','size'))
        require(isinstance(i,str) and re.fullmatch(r'[a-zA-Z0-9_:-]{1,96}',i) and i not in seen and isinstance(n,str)
                and 1<=len(n)<=260 and relative_file(n) and '/' not in n and type(k) is bool and k and type(b) is int and b==0,'ORIGINAL_DIRECTORY_ROOT_REQUIRED')
        nk=root_name_collision_key(n); require(nk not in names,'AMBIGUOUS_ORIGINAL_ROOT_NAME');seen.add(i);names.add(nk)
        clean.append({'name':n,'source_id':i,'is_dir':k,'size':b})
    return sorted(clean,key=lambda x:x['source_id'])


def range_facts(item,manifest):
    require(validate_manifest(manifest) and manifest.get('demo') is not True and manifest.get('scope')=='recursive'
            and manifest.get('recursive_evidence')=='normal_ui_exact_directory_listing'
            and all(item[k]==manifest.get(k) for k in TYPES),'RECURSIVE_RANGE_BINDING_REQUIRED')
    require(isinstance(item.get('article_revision'),str) and item['article_revision']==manifest.get('article_revision'), 'FROZEN_ARTICLE_REVISION_MISMATCH')
    rs=roots(manifest.get('roots'));require(type(manifest.get('root_count')) is int and len(rs)==manifest['root_count'],'ROOT_COUNT_NOT_LOCKED')
    require(len(manifest['files'])<=10000,'FILE_RANGE_TOO_LARGE')
    files=sorted(manifest['files'],key=lambda f:f['file']); potential={r['name'] for r in rs}; path_keys=set()
    for f in files:
        path=f['file']; canonical=normalize('NFKC',path).casefold()
        require('/' in path and path.split('/',1)[0] in potential and canonical not in path_keys,'FILE_SCOPE_OVERLAPS_OR_ESCAPES_ROOT');path_keys.add(canonical)
    dirs=manifest.get('directories')
    require(isinstance(dirs,list) and len(rs)<=len(dirs)<=1200 and all(isinstance(x,str) and relative_file(x) for x in dirs),'EXACT_RECURSIVE_DIRECTORY_SET_REQUIRED')
    dirs=sorted(dirs);dset=set(dirs); require(len(dset)==len(dirs) and len({normalize('NFKC',x).casefold() for x in dirs})==len(dirs) and potential<=dset
            and all((x.rsplit('/',1)[0] in dset if '/' in x else x in potential) for x in dirs)
            and all(x['file'].rsplit('/',1)[0] in dset for x in files)
            and not path_keys&{normalize('NFKC',x).casefold() for x in dirs},'RECURSIVE_DIRECTORY_ORIGIN_MISMATCH')
    require(manifest.get('source_revision')=='sr_'+digest([rs,dirs,files]),'CHANGED_SOURCE_REVISION')
    amount=sum(f['bytes'] for f in files); require(0<amount<2**53,'EXACT_PAYLOAD_BYTES_REQUIRED')
    sh=manifest.get('share_sha256'); ar=manifest.get('article_revision')
    require(isinstance(sh,str) and HEX.fullmatch(sh) and isinstance(ar,str) and re.fullmatch(r'arv_[a-f0-9]{64}',ar), 'STRICT_SOURCE_BINDING_REQUIRED')
    return {'roots':rs,'files':files,'directories':dirs,'bytes':amount,'destination':manifest.get('destination'),'source_sha256':sh,'article_revision':ar}


def web_intent_binds(task, decision, items, root):
    inputs=task.get('inputs') or {}
    if inputs.get('kind')!='confirmed_transfer':
        require(not any('receive_target' in item for item in items),'FROZEN_BROWSER_REQUEST_REQUIRED')
        return
    request_ref=inputs.get('transfer_request_ref')
    receipt_ref=inputs.get('scope_receipt_ref')
    require(request_ref==decision.get('transfer_request_ref') and receipt_ref==decision.get('scope_receipt_ref'),
            'FROZEN_BROWSER_REQUEST_REQUIRED')
    request,_=pin(request_ref,TRANSFER_REQUESTS,65_536,root)
    receipt,_=pin(receipt_ref,RECEIPTS,65_536,root)
    scope_id=task['task_id']+'-scope'
    require(keys_match(request,record_type='transfer_request',record_id=task['task_id'],execution_allowed=False)
            and keys_match(receipt,record_type='dsh_receipt',producer='dsh',task_id=scope_id,
                           session_id=task['target_session_id'],state='ready_for_review',execution_allowed=False),
            'FROZEN_BROWSER_SCOPE_RECEIPT_REQUIRED')
    scope_path=root/TASKS/(scope_id+'.json')
    require(scope_path.resolve(strict=True)==scope_path and scope_path.is_file(),'FROZEN_BROWSER_SCOPE_TASK_REQUIRED')
    with scope_path.open('rb') as stream:
        scope_raw=stream.read(65_537)
    require(len(scope_raw)<=65_536 and sha256(scope_raw).hexdigest()==receipt.get('task_sha256'),
            'FROZEN_BROWSER_SCOPE_TASK_REQUIRED')
    scope_task=json.loads(scope_raw)
    require(keys_match(scope_task,execution_allowed=False,authorization_ref=None,task_id=scope_id)
            and (scope_task.get('inputs') or {}).get('transfer_request_ref')==request_ref,
            'FROZEN_BROWSER_SCOPE_TASK_REQUIRED')
    proof=(receipt.get('result') or {}).get('dedicated_transfer_readback') or {}
    require(proof.get('transfer_request_ref')==request_ref
            and proof.get('range_manifests')==[item['range_manifest_ref'] for item in items]
            and request.get('receive_target')==inputs.get('receive_target')==decision.get('receive_target')==proof.get('target'),
            'FROZEN_BROWSER_SCOPE_CHANGED')
    ordered=request.get('items')
    bound=('resource_version_id','site_id','article_id','article_revision','queue_item_id','item_rank')
    require(isinstance(ordered,list) and len(ordered)==len(items) and request.get('order_mode')==decision.get('order_mode')
            and all(type(before) is dict and all(before.get(key)==after.get(key) for key in bound)
                    for before,after in zip(ordered,items,strict=True)),
            'FROZEN_BROWSER_ORDER_CHANGED')


def keys_match(record, **expected):
    return all(key in record and type(record[key]) is type(value) and record[key]==value
               for key,value in expected.items())


def build_artifacts(task_ref,*,root=WORKSPACE,clock=None,expected_session=None):
    """Only consumes the actual request handed to the session with a bound hash."""
    clock=clock or datetime.now(timezone.utc)
    task,_=pin(task_ref,TASKS,65_536,root); task_id=task_ref['path'][len(TASKS):-5]
    target,browser=(expected_session,None) if expected_session is not None else execution_target(task_ref,root)
    require(task.get('record_type')=='control_task' and task.get('target_session_id')==target and task.get('schema_version')==1
            and task.get('producer')=='codex-control' and task.get('state')=='ready' and task.get('plan_version')=='V4'
            and task_id==task.get('task_id') and task.get('demo') is not True,'EXACT_BOUND_TASK_REQUIRED')
    if browser is not None:
        route=execution_route(root)
        require((task.get('inputs') or {}).get('intake_binding')==route.get('intake_binding'),
                'FROZEN_TASK_INTAKE_CHANGED')
        require((task.get('inputs') or {}).get('kind')!='confirmed_transfer'
                or (task.get('inputs') or {}).get('scope_receipt_ref')==route.get('readback_ref'),
                'FROZEN_TASK_READBACK_CHANGED')
    require(task.get('authorization_ref') is not None,'NO_AUTHORIZED_LIVE_TRANSFER_TASK')
    decision,db=pin(task['authorization_ref'],DECISIONS,65_536,root)
    require(decision.get('schema_version')==1 and decision.get('record_type')=='user_confirmation' and decision.get('producer')=='codex-control'
            and decision.get('state')=='confirmed' and decision.get('execution_allowed') is True and decision.get('confirmation_actor')=='user'
            and decision.get('demo') is not True and decision.get('authorized_task_id')==task_id
            and decision.get('operation')=='one_bounded_normal_baidu_ui_transfer_batch'
            and isinstance(decision.get('user_message_reference'),str) and 1<=len(decision['user_message_reference'])<=250
            and fresh({'observed_at':decision.get('confirmed_at')},1800,clock),'EXPLICIT_CODEX_RECORDED_USER_AUTHORIZATION_REQUIRED')
    sel,_=pin(task.get('inputs',{}).get('frozen_selection_ref'),DECISIONS,65_536,root)
    require(sel.get('schema_version')==1 and sel.get('record_type')=='frozen_transfer_selection' and sel.get('state')=='frozen'
            and sel.get('demo') is not True and decision.get('frozen_selection_ref')==task['inputs']['frozen_selection_ref'],'NO_FIXED_FROZEN_SELECTION')
    items=sel.get('items'); batch=sel.get('batch_id')
    require(isinstance(items,list) and 1<=len(items)<=4 and all(type(x) is dict for x in items)
            and isinstance(batch,str) and KEY.fullmatch(batch)
            and not re.fullmatch(r'CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]',batch,re.I),'BOUNDED_FROZEN_BATCH_REQUIRED')
    web_intent_binds(task,decision,items,root)
    revision=decision.get('order_revision')
    require(type(revision) is int and revision>=1,'INVALID_ORDER_REVISION')
    require(revision==1 and not any(k in sel or k in decision for k in ('previous_plan_ref','initial_confirmation_ref')),
            'BATCH_ORDER_REVISION_NOT_ENABLED_NO_REPLAY')
    auth_id='v4-'+digest([task_ref,batch,revision])[:40]
    plan={k:decision.get(k) for k in ('batch_id','confirmation_seq','confirmed_at','order_mode','order_revision','selection_sha256')}
    require(batch==plan['batch_id'] and all(sel.get(k)==decision.get(k) for k in ('order_revision','order_mode','selection_sha256','ordered_items_sha256'))
            and type(decision.get('revision')) is int and decision['revision']==plan['order_revision'],'CONFIRMED_SELECTION_REVISION_OR_ORDER_CHANGED')
    plan.update(schema_version=1,record_type='confirmed_plan',producer='dsh',state='confirmed',execution_allowed=True,revision=plan['order_revision'],items=items,
                record_id=batch,event_id=auth_id,observed_at=plan['confirmed_at'],confirmation_task_ref=task_ref)
    locked={k:sel[k] for k in ORDER_KEYS if k in sel}
    require(locked=={k:decision[k] for k in ORDER_KEYS if k in decision}
            and (not locked or type(locked.get('global_order_locked')) is bool
                 and locked['global_order_locked']==('batch_rank' in locked)), 'GLOBAL_ORDER_NOT_USER_BOUND')
    plan.update(locked)
    user={k:plan[k] for k in ('batch_id','confirmation_seq','confirmed_at','order_mode','order_revision','selection_sha256','revision')}
    user.update(locked)
    user.update(schema_version=1,record_type='user_confirmation',producer='dsh-ui',record_id=auth_id,event_id=auth_id,state='confirmed',execution_allowed=True,
                observed_at=plan['confirmed_at'],confirmation_actor='user',ordered_items_sha256=decision.get('ordered_items_sha256'),
                confirmation_channel='codex_control_record_of_actual_user_reply',control_decision_ref=task['authorization_ref'])
    plan['authorization_ref']=reference(AUTH+auth_id+'.json',user)
    require(validate_confirmed_shape(plan) and receipt_binds(plan,bytes_for(user)) and user['ordered_items_sha256']==ordered_items_sha256(items), 'FROZEN_ORDER_OR_SCOPE_DIGEST_FAILED')
    require(len({x['article_id'] for x in items})==len(items) and len({x['source_id'] for x in items})==len(items),'DUPLICATE_FROZEN_ITEM')
    checked=[]
    for item in items:
        r,_=pin(item['range_manifest_ref'],RANGES,2_000_000,root);checked.append(range_facts(item,r))
    return plan,user,reference(PLANS+batch+'.json',plan),checked


def load_for_execution(task_ref,plan_ref,*,root=WORKSPACE,clock=None):
    p,u,ref,facts=build_artifacts(task_ref,root=root,clock=clock)
    require(plan_ref==ref,'PLAN_NOT_FROM_THIS_USER_DECISION')
    current,pb=pin(plan_ref,PLANS,65_536,root); auth,ab=pin(p['authorization_ref'],AUTH,65_536,root)
    require(pb==bytes_for(p) and ab==bytes_for(u) and receipt_binds(current,ab),'FROZEN_PLAN_OR_AUTHORIZATION_CHANGED')
    return current,facts


def current_article(catalog, share_locations, item):
    aid=item['article_id']
    article=catalog.get(aid,{})
    require(item['site_id']==SITE and article.get('url')==ORIGIN+'/'+aid+'.html',
            'EXACT_AUTHORIZED_PUBLISHER_ENTRY_REQUIRED')
    if len(article.get('shares',[]))==1 and article.get('access_state')=='public':
        return article
    locations=share_locations.get((SITE,aid),())
    require('receive_target' in item and article.get('access_state') in ('unknown','public')
            and not article.get('shares') and len(locations)==1 and 'owner_export' in locations[0].origins,
            'EXACT_AUTHORIZED_PUBLISHER_ENTRY_REQUIRED')
    location=locations[0]
    return {**article,'shares':[{'url':location.url,'code':location.extraction_code}]}


def confirmed_entries(plan,facts,view,catalog,versions,ops,markers,*,owned=False,root=WORKSPACE,share_locations=None):
    require(view['control_phase']=='paused' and (owned or not view['lease']['held']),'ORIGINAL_QUEUE_MUST_REMAIN_PAUSED_AND_EXCLUSIVE')
    block={r['scope'] for r in view.get('blocks',[]) if not r.get('resolved')}|{r['scope'] for r in view.get('invalid',[])}
    require(not block&{'site','platform:baidu','platform:wangzjk'},'EXISTING_PLATFORM_HOLD_UNCHANGED')
    result=[]; used=set(); seen_share=set(); names=set()
    for x,scope in zip(plan['items'],facts,strict=True):
        aid=x['article_id']; a=current_article(catalog,share_locations or {},x); shares=a['shares']
        key=shares[0].get('url'); sk=source_key(key); ver=versions.get(x['resource_version_id'],{})
        linked=[o for o in ops if aid in o.get('article_ids',[]) or key==o.get('share_url')]
        require(not linked,'HISTORICAL_OR_UNKNOWN_OPERATION_NO_REPLAY')
        eligible=ver.get('selectable') is True or ('receive_target' in x and ver.get('requestable') is True
                                                  and not ver.get('action_pending'))
        require(sk and ver.get('article_id')==aid and ver.get('article_revision')==scope['article_revision'] and eligible
            and x['source_id']=='src_'+sk and sha256(key.encode()).hexdigest()==scope['source_sha256'] and key not in seen_share,'FROZEN_ARTICLE_VERSION_OR_SHARE_CHANGED')
        if 'receive_target' in x:
            dest=receive_target(root)
            require(x['receive_target']==dest==scope['destination'],'EXACT_RECEIVE_TARGET_CHANGED')
        else:
            folder=a.get('classification',{}).get('folder',''); dest=TARGET+'/'+folder
            require(folder and '/' not in folder and chr(92) not in folder and dest==scope['destination'],'EXACT_CLASSIFIED_DESTINATION_CHANGED')
        require(not block&{'article:'+aid,'share:'+key},'ARTICLE_OR_SHARE_HOLD_UNCHANGED')
        require(not any(m.get('article_id')==aid or m.get('share_sha256')==scope['source_sha256'] for m in markers),'DOWNLOADED_OR_CLEANED_OBJECT_NO_AUTOMATIC_REACQUIRE')
        kn={(dest,root_name_collision_key(z['name'])) for z in scope['roots']}
        require(not kn&names and not any((o.get('destination'),root_name_collision_key(r['name'])) in kn for o in ops for r in o.get('roots',[])), 'SAME_NAME_NEEDS_RECONCILIATION')
        require(x['item_rank'] not in used,'FROZEN_RANK_REUSED');used.add(x['item_rank']);names.update(kn);seen_share.add(key)
        result.append({'article':a,'share_url':key,'confirmed_item':x,'range':scope})
    return result


def review_selection(library,items,facts,*,root=WORKSPACE,owned=False):
    state=root/'_ingest/wangzjk-to-pan/state'
    queue=CrawlQueue(state)
    marks=json.loads((state/'downloaded-project-markers.json').read_bytes())['entries']
    return confirmed_entries({'items':items}, facts, queue.status(), library.adapters[SITE].articles,
                             library.by_id, Journal(state).operations(), marks, root=root, owned=owned,
                             share_locations=library.share_locations)


def revalidate_capacity(fact,budget,*,floor=FLOOR,clock=None):
    require(type(fact) is dict and fact.get('allow') is True and fresh(fact,30,clock or datetime.now(timezone.utc))
        and type(fact.get('remaining_bytes')) is int and type(budget) is int and budget>0
        and fact.get('minimum_free_bytes')==floor and fact['remaining_bytes']-budget>floor,'LIVE_EXACT_PACKAGE_CAPACITY_RESERVE')


def exact_claim_input(entry,o):
    source=o.get('source') or {}; observed=o.get('v4_recursive') or {}; scope=entry['range']; now=datetime.now(timezone.utc)
    require(str(o.get('article_id'))==entry['article']['article_id'] and source.get('share_url')==entry['share_url']
        and o.get('selected_destination')==scope['destination'] and roots(source.get('roots'))==scope['roots']
        and type(o.get('declared_count')) is int and o['declared_count']==len(scope['roots']) and fresh(source,30,now), 'UNAPPROVED_SCOPE_OR_SELECTION')
    require(observed.get('range_manifest_sha256')==entry['confirmed_item']['range_manifest_ref']['sha256'] and observed.get('scope')=='recursive'
        and observed.get('evidence_class')=='normal_ui_exact_directory_listing' and observed.get('source_revision')==entry['confirmed_item']['source_revision']
        and observed.get('whole_share') is True and fresh(observed,120,now),'LIVE_RECURSIVE_RANGE_DRIFT_OR_EXPIRED')
    revalidate_capacity(o.get('v4_capacity'),scope['bytes'])


def queue_guard(view,ref,lock_sha,state_path):
    p=state_path/'.crawl-coordinator.lease'
    require(isinstance(lock_sha,str) and HEX.fullmatch(lock_sha) and p.resolve()==p and p.is_file(),'ACTUAL_ORIGINAL_COORDINATOR_LEASE_REQUIRED')
    raw=p.read_bytes();o=json.loads(raw)
    require(sha256(raw).hexdigest()==lock_sha and o.get('owner')=='v4:'+ref['sha256'][:24]
            and view.get('control_phase')=='paused' and view['lease']['held'],'ACTUAL_ORIGINAL_COORDINATOR_LEASE_CHANGED')


def claim_guard(o,queue,journal,*,clock=None):
    """Verify the current selected version before native occupancy and Journal claim."""
    v=o['v4']; task,ref,rank=(v[k] for k in ('task_ref','plan_ref','rank')); q=queue.status();queue_guard(q,ref,v.get('lease_sha256'),queue.state_dir)
    plan,facts=load_for_execution(task,ref,clock=clock)
    require(type(rank) is int and 1<=rank<=len(plan['items']),'NEXT_FROZEN_RANK_REQUIRED')
    # The exact verified success prefix of this uninterrupted invocation only.
    ops=journal.operations(); byshare={x['share_url']:x for x in ops}; done=v.get('prefix_operations',[])
    require(isinstance(done,list) and len(done)==rank-1 and len(set(done))==len(done),'INVALID_SUCCESS_PREFIX')
    from catalog_model import open_index
    lib=open_index()
    cat=lib.adapters[SITE].articles
    for n,k in enumerate(done):
        a=current_article(cat,lib.share_locations,plan['items'][n]); old=byshare.get(a['shares'][0]['url'])
        require(old and old['operation_id']==k and old['state']=='submitted' and old.get('receipt_success') is True,'FROZEN_PREFIX_NOT_ACTUALLY_SUBMITTED')
    # Own previous submitted rows cannot enter this call as new work.
    marks=json.loads((queue.state_dir/'downloaded-project-markers.json').read_bytes())['entries']
    current={**plan,'items':plan['items'][rank-1:]}; entries=confirmed_entries(current,facts[rank-1:],q,cat,lib.by_id,ops,marks,
                                                                           owned=True,share_locations=lib.share_locations)
    exact_claim_input(entries[0],o)
    fp=json.loads((queue.state_dir/'capacity-policy.json').read_bytes())
    require(fp.get('minimum_free_bytes')==FLOOR,'PERSISTED_CAPACITY_POLICY_CHANGED')
    binding={'plan_ref':ref,'task_ref':task,'queue_item_id':entries[0]['confirmed_item']['queue_item_id'],'item_rank':rank,
             'range_manifest_ref':entries[0]['confirmed_item']['range_manifest_ref'],'capacity_observation':o['v4_capacity']}
    return binding, entries[0]['article'], facts[rank-1]['destination']


def main():
    mode=sys.argv[1]; body=json.loads(sys.argv[2]); task=body['task_ref']
    def emit(x):print(compact(x).decode(),flush=True)
    if mode=='stage':
        p,a,f,scopes=build_artifacts(task); from catalog_model import open_index
        review_selection(open_index(),p['items'],scopes)
        emit({'plan':p,'authorization':a,'plan_ref':f,'authorization_ref':p['authorization_ref'],'plan_bytes':bytes_for(p).decode(),'authorization_bytes':bytes_for(a).decode(),'package_bytes':sum(v['bytes'] for v in scopes),'browser_binding':execution_target(task)[1]});return
    if mode=='tap':
        ref=body['plan_ref']; q=CrawlQueue(BRANCH/'state'); view=q.status(); queue_guard(view,ref,body['lease_sha256'],q.state_dir)
        plan,scopes=load_for_execution(task,ref); rank=body['rank']; require(type(rank) is int and 1<=rank<=len(scopes),'NEXT_FROZEN_RANK_REQUIRED')
        opid=body['operation_id']; j=Journal(q.state_dir); pending=j.get(opid); require(pending['state']=='submission_unknown' and pending['ever_claimed'] is True,'DURABLY_CLAIMED_OPERATION_REQUIRED')
        cur=plan['items'][rank-1]; exact=scopes[rank-1]; from catalog_model import open_index
        lib=open_index(); cat=lib.adapters[SITE].articles; marks=json.loads((q.state_dir/'downloaded-project-markers.json').read_bytes())['entries']
        a=current_article(cat,lib.share_locations,cur); v=lib.by_id.get(cur['resource_version_id'],{}); cal=sha256(pending['share_url'].encode()).hexdigest()
        blocks={z['scope'] for z in view['blocks'] if not z['resolved']}|{z['scope'] for z in view['invalid']}
        require(not blocks&{'site','platform:baidu','platform:wangzjk','article:'+cur['article_id'],'share:'+pending['share_url']}
              and not any(m.get('article_id')==cur['article_id'] or m.get('share_sha256')==cal for m in marks)
              and a['shares'][0]['url']==pending['share_url'] and v.get('article_revision')==exact['article_revision']
              and pending['article_ids']==[cur['article_id']] and pending['destination']==exact['destination'] and roots(pending['roots'])==exact['roots'] and cal==exact['source_sha256'], 'FROZEN_BINDING_CHANGED_AFTER_CLAIM')
        emit({'ready':True});return
    require(mode=='lease','WRONG_ENTRY')
    ref=body['plan_ref']; q=CrawlQueue(BRANCH/'state')
    with q.lease('v4:'+ref['sha256'][:24]):
        p,s=load_for_execution(task,ref); from catalog_model import open_index
        entries=review_selection(open_index(),p['items'],s,owned=True)
        emit({'entries':entries,'lease_sha256':sha256(q.lock_path.read_bytes()).hexdigest(),'selection_sha256':p['selection_sha256'],'browser_binding':execution_target(task)[1]})
        for line in sys.stdin:
            msg=json.loads(line)
            if msg=={'close':True}:break
            require(msg.get('scope') in {'platform:baidu',*['share:'+x['share_url'] for x in entries]} and isinstance(msg.get('reason'),str),'HOLD_SCOPE_OUTSIDE_SELECTED_PLAN')
            q.block_scope(msg['scope'],msg['reason'],evidence_ref=msg['evidence']);emit({'recorded':True})

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,TypeError,KeyError,UnicodeError) as e:
        msg=str(e);print(compact({'blocked':msg if re.fullmatch(r'[A-Z0-9_]+',msg) else 'BOUND_INPUT_MISSING_OR_INVALID'}).decode(),flush=True)
        sys.exit(2)
