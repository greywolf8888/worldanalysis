"""Static Codex projection binding; separate identity, presence, coverage and authority."""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys
from threading import Lock
from time import monotonic
from urllib.parse import quote, unquote
from course_completion import completion_fields, current_received_issue, verified_companion_install
from course_followups import CourseFollowups
from catalog_model import ROOT, SITE, exact_root_fact, now, read_bound, safe_text, source_key

BASE=ROOT.parents[2]
CONTRACT=BASE/'_exchange/control/decisions/v4-local-integration-20261126-r3.json'
CONTRACT_SHA='4b1c112b2daa3356808dac676e88594dde6fd27cb5a3adba0e1cca7404b615ff'
SCRIPT=BASE/'_tools/v4-local-status-preservation.py'
SCRIPT_SHA='9de1256e2f9cb8012ea201fd931090de3c1362950ae95e5724b58aad7e6fcf69'
HISTORICAL_SCRIPT = BASE / '.runtime/course-production-worktree/_tools/course_production/history_inventory.py'
HEX=re.compile(r'[a-f0-9]{64}\Z')
PROJECTION_CACHE_SECONDS = 25
OVERVIEW_LINK = re.compile(r'^- \[.*\]\(\.\./([^()]+)\) · 分析 ')


def stamp(value):
    try:
        x=datetime.fromisoformat(value.replace('Z','+00:00'))
        return x if x.tzinfo is not None else None
    except (ValueError, AttributeError):return None


def exact_map(rows):
    """No title fallback and no many-to-one conflict overwrite."""
    m=defaultdict(set)
    for r in rows:
        a,c,p=(r.get(k) for k in ('article_id','cloud_root_id','local_project_id'))
        evidence=r.get('evidence',{}); fp=evidence.get('sha256'); ep=evidence.get('path','')
        ep=ep.removeprefix('_ingest/wangzjk-to-pan/') if isinstance(ep,str) else ''
        if (r.get('site_id')==SITE and r.get('basis')=='historical_exact_root_and_source_sha256'
            and isinstance(a,str) and a.isascii() and a.isdecimal() and isinstance(c,str) and c.isascii() and c.isdecimal()
            and isinstance(p,str) and 0<len(p)<=140 and isinstance(fp,str) and HEX.fullmatch(fp)
            and isinstance(ep,str) and ep.startswith('state/evidence/') and '/' not in ep[15:] and '..' not in ep):m[(a,c)].add(p)
    return {k:next(iter(v)) for k,v in m.items() if len(v)==1}


def file_inside(value, roots):
    if not isinstance(value,str) or len(value)>1500:return None
    p=Path(value)
    if not p.is_absolute() or p.suffix.lower()!='.md':return None
    try:
        if p.resolve(strict=True)!=p or not p.is_file():return None
        for root in roots:
            if p.is_relative_to(root):return p
    except (OSError,ValueError):pass
    return None


def overview_textbook_order(overview, root):
    """Read ordering from the generated analysis-date list of the formal vault."""
    start, end = '<!-- Worldanalysis:course-catalog -->', '<!-- /Worldanalysis:course-catalog -->'
    try:
        with overview.open('rb') as stream:
            raw = stream.read(4_000_001)
        if len(raw) > 4_000_000:
            return []
        text = raw.decode('utf-8-sig')
    except (OSError, ValueError):
        return []
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) >= text.index(end):
        return []
    managed = text.split(start, 1)[1].split(end, 1)[0]
    order = []
    in_analysis_view = False
    for line in managed.splitlines():
        if line.startswith('## '):
            in_analysis_view = line == '## 按分析日期（新到旧）'
        if not in_analysis_view or not (match := OVERVIEW_LINK.match(line)):
            continue
        try:
            decoded = unquote(match[1], errors='strict')
        except ValueError:
            continue
        relative = Path(decoded)
        if (relative.is_absolute() or relative.as_posix() != decoded or len(relative.parts) != 3
                or relative.parts[0].startswith('.') or relative.parts[1] != '课件'
                or relative.parts[2] != relative.parts[0] + '.md' or any(c in decoded for c in '\\:\0')):
            continue
        order.append(root / relative)
    return list(dict.fromkeys(order))


def legacy_reuse_textbooks(projects, roots, overview_paths):
    """Show a same-edition reused note as an entrance only, without creating a project or copying a sign-off."""
    candidates = defaultdict(set)
    for project in projects:
        book = project.get('textbook') or {}
        matched = book.get('registered_sha256')
        if (book.get('relation') != 'same_source_reuse' or book.get('state') != 'registered_edition_matches'
                or (project.get('acceptance') or {}).get('state') != 'same_source_reuse'
                or not isinstance(matched, str) or not HEX.fullmatch(matched)
                or book.get('current_sha256') != matched or not isinstance(book.get('entry'), str)):
            continue
        path = Path(book['entry'])
        if path in overview_paths:
            candidates[path].add(matched)
    items = {}
    for path, hashes in candidates.items():
        if len(hashes) != 1 or not file_inside(str(path), roots):
            continue
        try:
            with path.open('rb') as stream:
                body = stream.read(12_000_001)
            if len(body) > 12_000_000 or sha256(body).hexdigest() not in hashes:
                continue
        except OSError:
            continue
        items[path] = {'title': safe_text(path.stem, 500), 'category': overview_paths[path],
                       'textbook_state': 'legacy_reuse_entry', 'course_completion': {'textbook': 'legacy_entry_pending'},
                       'analysis_date': None, 'production_date': None,
                       'obsidian_href': 'obsidian://open?path=' + quote(path.as_posix(), safe='')}
    return items


def formal_review_stage(project, data):
    analysis = project.get('analysis') or {}
    if analysis.get('state') != 'in_progress':
        return project
    progress = data.get('current_production') or {}
    paths = progress.get('evidence') or {}
    body_sha = progress.get('candidate_sha256')
    if (progress.get('local_project_id') != project.get('local_project_id')
            or not isinstance(body_sha, str) or not HEX.fullmatch(body_sha)
            or not paths.get('candidate_manifest') or not paths.get('independent_review_request')):
        return project
    try:
        path = Path(paths['independent_review_request'])
        candidate = Path(paths['candidate_manifest'])
        results = BASE / '整理结果'
        if (not path.is_relative_to(results) or not candidate.is_relative_to(results)
                or candidate.resolve(strict=True) != candidate or not candidate.is_file()
                or current_received_issue(project, BASE)):
            return project
        request, _ = read_bound(path, 16_000_000)
        if request.get('body_sha256') != body_sha or request.get('decision') != '':
            return project
    except (OSError, ValueError, TypeError, AttributeError):
        return project
    return {**project, 'analysis': {**analysis, 'state': 'awaiting_acceptance'}}


def projection_snapshot():
    contract,ch=read_bound(CONTRACT,80_000)
    spec=contract['projection']
    if (ch!=CONTRACT_SHA or contract.get('revision')!=3 or spec['sha256']!=SCRIPT_SHA
        or Path(spec['path'])!=SCRIPT or spec['read_only'] is not True
        or spec['command']!=['python','-B','-X','utf8',SCRIPT.as_posix()]):
        raise ValueError('invalid pinned local projection contract')
    def pinned():
        if SCRIPT.resolve(strict=True)!=SCRIPT or not SCRIPT.is_file() or SCRIPT.stat().st_size>100_000 or sha256(SCRIPT.read_bytes()).hexdigest()!=SCRIPT_SHA:
            raise ValueError('projection script drift')
    pinned()
    res=subprocess.run([sys.executable,'-B','-X','utf8',str(SCRIPT),'--with-legacy-links'],cwd=BASE,
                       env={'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,timeout=22,check=False)
    pinned()
    if res.returncode!=0 or len(res.stdout)>4_000_000:raise ValueError('local projection unavailable')
    d=json.loads(res.stdout); at=stamp(d.get('observed_at'))
    if d.get('schema_version')!=1 or d.get('projection_status') not in ('available','partial') or not at or abs((datetime.now(timezone.utc)-at).total_seconds())>180:
        raise ValueError('invalid or stale projection')
    return d


def historical_textbooks():
    try:
        if HISTORICAL_SCRIPT.resolve(strict=True) != HISTORICAL_SCRIPT:
            return []
        response = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(HISTORICAL_SCRIPT),
                                   '--read-historical'], cwd=BASE, capture_output=True, timeout=8, check=True)
        data = json.loads(response.stdout) if len(response.stdout) <= 1_000_000 else None
        if isinstance(data, dict) and isinstance(data.get('entries'), list):
            return data['entries']
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return []


CLEANUP_STAGES={
    'candidate_present':('present_identity_matches','not_applicable'),
    'candidate_identity_unverified':('unverified','unknown'),
    'candidate_missing_unverified':('absent_unexplained','unknown'),
    'cleanup_pending_reconciliation':('unverified','unknown'),
}


def preservation_view(book, cleanup, body_bytes_match=True):
    # The signed derivative and receipt combination is read by the R3 projector.
    # Never infer deletion, completeness or freed bytes from a missing directory.
    unknown={'preservation_state':'unknown','independent_review_state':'unknown','formal_reader_state':'unknown',
             'local_cleanup_state':'unknown','local_cleanup_physical_state':'unknown','local_cleanup_reader_state':'unknown',
             'local_cleanup_scope':None,'actual_released_bytes':None}
    given=book.get('preservation'); obs=book.get('current_sha256'); reg=book.get('registered_sha256')
    if (not body_bytes_match or book.get('state')!='registered_independent_derivative_matches' or book.get('relation')!='own'
        or not isinstance(given,dict) or given.get('state')!='installed_verified' or given.get('independent_review_state')!='verified'
        or given.get('formal_reader_state') not in ('verified','awaiting_reopen')
        or not all(isinstance(h,str) and HEX.fullmatch(h) for h in (obs,reg)) or obs==reg
        or given.get('derived_body_sha256')!=obs or given.get('parent_body_sha256')!=reg):return unknown
    stage={**unknown,'preservation_state':'installed_verified','independent_review_state':'verified','formal_reader_state':given['formal_reader_state']}
    if cleanup.get('scope')!='one_reading_vault_copy':return stage
    name,physical,post=(cleanup.get(k) for k in ('state','physical_state','post_cleanup_reader_state'))
    evidence=cleanup.get('evidence')
    if name=='removed':
        if (physical!='removed_verified' or given['formal_reader_state']!='verified' or post not in ('awaiting_reopen','verified')
            or not isinstance(evidence,dict) or not evidence.get('receipt') or (post=='verified' and not evidence.get('post_cleanup_reader'))):return stage
    elif CLEANUP_STAGES.get(name)!=(physical,post):return stage
    return {**stage,'local_cleanup_state':name,'local_cleanup_physical_state':physical,'local_cleanup_reader_state':post,
            'local_cleanup_scope':'one_reading_vault_copy'}


class LocalProjection:
    def __init__(self, lib, load=projection_snapshot):
        self.lib=lib;self.load=load;self.guard=Lock();self.last_good=None;self.loaded_at=-10000000
        self.followups=CourseFollowups(BASE)
        self.last_result=None
        self.history_guard=Lock(); self.history_at=-float('inf'); self.history=[]
        self.ops={};self.catalog={};self.markers={};self.exact_recycle={}
        if getattr(lib,'provenance',None):
            state=ROOT.parent/'state'; expected=lib.provenance
            for name in ('journal.json','catalog.json','downloaded-project-markers.json','crawl_state.JSON'):
                d,h=read_bound(state/name)
                if h!=expected[name]:raise ValueError('upstream changed before production integration')
                if name=='journal.json':self.ops=d['operations']
                elif name=='catalog.json':self.catalog=d['articles']
                elif name=='downloaded-project-markers.json':
                    self.markers={(r['article_id'],r['cloud_root_id']):r for r in d['entries'] if isinstance(r.get('article_id'),str) and isinstance(r.get('cloud_root_id'),str)}
                else:self.crawl={'phase':d.get('phase'),'global_holds':[k for k,v in d.get('blocks',{}).items() if k in ('site','platform:wangzjk','platform:baidu') and not v.get('resolved')],'revision':d.get('revision')}
        else:self.crawl={'phase':'unavailable','global_holds':[],'revision':None}
    def get(self):
        with self.guard:
            if monotonic()-self.loaded_at<PROJECTION_CACHE_SECONDS:return self.last_result
            try:
                d=self.load(); self.last_good=d
                result={'data':d,'sync_state':'live' if d['projection_status']=='available' else 'partial','observed_at':d['observed_at']}
            except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError,AssertionError):
                result={'data':self.last_good,'sync_state':'pending_sync','observed_at':(self.last_good or {}).get('observed_at')}
            self.last_result=result;self.loaded_at=monotonic();return result
    def analysis_anchor(self, project_id):
        script = BASE / '.runtime/course-production-worktree/_tools/course_production/analysis_priority_order.py'
        if script.resolve(strict=True) != script or not script.is_file():
            raise ValueError('正式分析入口不可用')
        try:
            result = subprocess.run(
                [sys.executable, '-B', '-X', 'utf8', str(script), '--anchor', project_id],
                cwd=BASE, capture_output=True, timeout=120, check=True,
            )
        except subprocess.SubprocessError as error:
            raise ValueError('本地项目当前无法核对') from error
        if len(result.stdout) > 8_000:
            raise ValueError('本地项目返回无效')
        return json.loads(result.stdout)
    @staticmethod
    def roots(d):
        roots=[]
        for c in d.get('categories') or []:
            if not isinstance(c,dict) or not isinstance(c.get('vault'),str):continue
            p=Path(c['vault'])
            try:
                if p.is_absolute() and p.is_relative_to(BASE) and p.resolve(strict=True)==p and p.is_dir():roots.append(p)
            except (OSError,ValueError):pass
        return roots
    def project_view(self,p,d,basis):
        p=formal_review_stage(p,d)
        dw=p.get('download') or {};an=p.get('analysis') or {};ac=p.get('acceptance') or {};bk=p.get('textbook') or {};cl=p.get('cleanup') or {};curr=p.get('dates') or {}
        f=file_inside(bk.get('entry'),self.roots(d)); verified_hash=bk.get('current_sha256'); expected_hash=bk.get('registered_sha256')
        same_bytes=False
        companions_readable=True
        if f and isinstance(verified_hash,str) and HEX.fullmatch(verified_hash):
            try:
                with f.open('rb') as stream: raw=stream.read(12_000_001)
                same_bytes=len(raw)<=12_000_000 and sha256(raw).hexdigest()==verified_hash
                if same_bytes and bk.get('state') == 'registered_edition_matches' and bk.get('relation') == 'own':
                    companions_readable=verified_companion_install(p, f, BASE, raw)
            except OSError:pass
        preservation=preservation_view(bk,cl,same_bytes)
        integrity=('same_registered_bytes' if same_bytes and verified_hash==expected_hash else
                   'same_verified_independent_derivative' if preservation['preservation_state']=='installed_verified' else 'unverified_or_changed')
        href='obsidian://open?path='+quote(f.as_posix(),safe='') if f else None
        completion = completion_fields(p, BASE) if same_bytes or an.get('state') == 'in_progress' else completion_fields({'analysis': an})
        if not companions_readable:
            completion['textbook'] = 'companions_unavailable'
            completion.pop('cleanup', None)
        return {'project_id':p.get('local_project_id'),'title':safe_text(p.get('title'),500),'category':p.get('category'),'sequence':p.get('sequence'),
                'link_basis':basis,'receipt_status':p.get('batch_status'),'directory_present':dw.get('directory_present'),'download_complete':dw.get('download_complete'),
                'file_count_in_registration':dw.get('registered_file_count'),'analysis_state':an.get('state'),'analysis_scope':an.get('scope_status'),'scope_gaps':an.get('scope_gap_count'),
                'acceptance_state':ac.get('state'),'textbook_state':bk.get('state'),'textbook_integrity':integrity,'obsidian_href':href,
                'analysis_date':curr.get('analysis'),'production_date':curr.get('production'),
                'course_completion':completion,**preservation}
    def exact_targets(self,row):
        found=set(); roots=set(); histories=set()
        for source in row.get('sources',[]):
            aid=source['article_id']; cat=self.catalog.get(aid,{})
            observed={source_key(s.get('url')) for s in cat.get('shares',[])}-{None}
            for oref in row.get('operations',[]):
                o=self.ops.get(oref['id']); fact=exact_root_fact(o) if o else None
                if fact and source.get('site_id')==SITE and aid in o.get('article_ids',[]) and fact['share_key'] in observed:
                    for k in fact['target_ids']:found.add((aid,k));roots.add(k)
                    histories.add(o['operation_id'])
        return found, roots, histories
    def detail(self,row):
        pairs, all_ids, _ = self.exact_targets(row)
        if pairs:
            r = self.get()
            d = (r['data'] or {}) if r['sync_state'] in ('live', 'partial') else {}
        else:
            # A new share alone cannot prove a historical root or join a signed project.
            cached = self.last_result if monotonic() - self.loaded_at < PROJECTION_CACHE_SECONDS else None
            r = cached or {'observed_at': None, 'sync_state': 'not_applicable'}
            d = {}
        links=exact_map(d.get('legacy_links') or []); present=set()
        projects={x['local_project_id']:x for x in d.get('projects',[])}; known=set()
        for a,k in pairs:
            if (a,k) in links: known.add(links[(a,k)])
            if (a,k) in self.markers and self.markers[(a,k)].get('cloud_cleanup_state')=='recycled_verified_exact_id':present.add(k)
        only=next(iter(known)) if len(known)==1 else None
        # A trusted historical join presents related state, but never proves current download completeness for the scope.
        v=self.project_view(projects[only],d,'historical_exact_article_and_cloud_root_plus_source_digest') if only in projects else None
        return {'observed_at':r['observed_at'],'sync_state':r['sync_state'],'linked_project':v,'link_state':'ambiguous' if len(known)>1 else 'observed_historical' if v else 'no_exact_link',
                'computed_cloud_historical_roots':len(all_ids),'verified_recycled_targets':len(present),
                'cloud_cleanup_state':'all_historical_targets_recycled' if present and present==all_ids else 'some_historical_targets_recycled' if present else 'unknown',
                'scope_download_complete':None,'scope_local_ready':None,'current_cloud_presence':None}
    def overview(self):
        s=self.get();d=s['data'] or {};l=exact_map(d.get('legacy_links') or []); counts=d.get('counts') or {}
        rows=d.get('projects') or []; ids={p['local_project_id'] for p in rows}; active=d.get('current_production') or {}; selected=next((p for p in rows if p['local_project_id']==active.get('local_project_id')),None)
        exact={p for p in l.values() if p in ids}
        fresh=s['sync_state'] in ('live','partial')
        statuses=[preservation_view(p.get('textbook') or {},p.get('cleanup') or {}) for p in rows] if fresh else []
        stages={'installed_verified':sum(t['preservation_state']=='installed_verified' for t in statuses),
                'cleanup_states':dict(Counter(t['local_cleanup_state'] for t in statuses)),
                'verified_post_cleanup_reopens':sum(t['local_cleanup_reader_state']=='verified' for t in statuses),
                'observed_at':s['observed_at'],'released_bytes':None}
        return {'sync_state':s['sync_state'],'observed_at':s['observed_at'],'counts':counts,'legacy_exact_identity_links':len([k for k,p in l.items() if p in ids]),
                'linked_local_projects':len(exact),'independent_local_projects':len(ids-exact),
                'projection_contract_revision':3,'local_preservation':stages,
                'current_production':{**self.project_view(selected,d,'registered_current_production'),'phase':active.get('phase'),'updated_at':active.get('updated_at')} if fresh and selected else None,
                'scope_note':'本地目录已见、完整下载、分析、验收、教材、清理分别读取；历史双键只连接过去确证身份。',
                'cloud_live_confirmation_for_cleanup':False,'cloud_deletion_allowed':False,'current_site_total_unknown':True}
    def local_rows(self,page):
        r=self.get();d=(r['data'] or {}) if r['sync_state'] in ('live','partial') else {}
        rows=d.get('projects') or [];start=(page-1)*16;links=set(exact_map(d.get('legacy_links') or []).values())
        return {'sync_state':r['sync_state'],'observed_at':r['observed_at'],'page':page,'count':len(rows),
                'items':[self.project_view(p,d,'historical_exact_link_available' if p['local_project_id'] in links else 'independently_registered') for p in rows[start:start+16]]}
    def historical_items(self, roots, formal_entries):
        with self.history_guard:
            if monotonic() - self.history_at >= PROJECTION_CACHE_SECONDS:
                self.history = historical_textbooks()
                self.history_at = monotonic()
            records = self.history
        items = {}
        seen = set(formal_entries)
        for row in records:
            if row.get('status') != 'historical_same_edition_readback':
                continue
            entry = row.get('entry')
            if not isinstance(entry, str) or Path(entry).is_absolute():
                continue
            path = file_inside(str(BASE / entry), roots)
            if not path or path in seen:
                continue
            try:
                with path.open('rb') as stream:
                    body = stream.read(12_000_001)
                if len(body) > 12_000_000 or sha256(body).hexdigest() != row['edition_sha256']:
                    continue
            except OSError:
                continue
            day = stamp(row.get('historical_readback_timestamp'))
            seen.add(path)
            items[path] = {'project_id': row['id'], 'title': safe_text(row['title'], 500),
                          'category': row['category'], 'textbook_state': 'registered_edition_matches',
                          'course_completion': {'textbook': 'historical_readable'},
                          'obsidian_href': 'obsidian://open?path=' + quote(path.as_posix(), safe=''),
                          'historical_readback_date': row.get('historical_readback_date') if day else None}
        return dict(sorted(items.items(), key=lambda pair: (pair[1]['title'], pair[1]['project_id'])))

    def textbooks(self, page):
        observation = self.get()
        fresh = observation['sync_state'] in ('live', 'partial')
        data = (observation['data'] or {}) if fresh else {}
        projects = data.get('projects', [])
        rows = [project for project in projects if completion_fields(project).get('textbook')]

        def date_order(project):
            analysis = ((project.get('dates') or {}).get('analysis') or {}).get('date')
            return -int(analysis.replace('-', '')) if isinstance(analysis, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', analysis) else 0

        rows.sort(key=lambda project: (date_order(project), project.get('title') or '', project['local_project_id']))
        start = (page - 1) * 16
        roots = self.roots(data)
        formal_entries = {Path(entry) for project in projects
                          if isinstance(entry := (project.get('textbook') or {}).get('entry'), str)}
        history = self.historical_items(roots, formal_entries) if roots else {}
        overviews = []
        overview_paths = {}
        for root in sorted(roots):
            overview = file_inside(str(root / '00 入口/00 项目总览.md'), (root,))
            if overview:
                overviews.append({'title': root.parent.name, 'obsidian_href': 'obsidian://open?path='+quote(overview.as_posix(), safe='')})
                overview_paths.update((path, root.parent.name) for path in overview_textbook_order(overview, root))
        formal = {Path(project['textbook']['entry']): project for project in rows if project['textbook'].get('entry')}
        reused = legacy_reuse_textbooks(projects, roots, overview_paths)
        readers = {**reused, **history}
        entries = dict.fromkeys([*formal, *history, *reused])
        ordered = [path for path in overview_paths if path in entries]
        ordered.extend(path for path in entries if path not in overview_paths)
        items = [self.project_view(formal[path], data, 'independently_registered') if path in formal else readers[path]
                 for path in ordered[start:start+16]]
        return {'sync_state': observation['sync_state'], 'page': page, 'count': len(ordered),
                'items': [item for item in items if item['course_completion'].get('textbook')], 'overviews': overviews}
    def cleanup_preparation(self):
        s=self.get();d=s['data'] or {};l=exact_map(d.get('legacy_links') or []);ids={p['local_project_id'] for p in d.get('projects',[])}
        prior={k for k,v in self.markers.items() if v.get('cloud_cleanup_state')=='recycled_verified_exact_id'}
        mapped=sum(k in l and l[k] in ids for k in prior);blocks=[]
        for r in self.lib.by_id.values():
            if len(blocks)>=6:break
            keys,all_ids,_=self.exact_targets(r)
            if all_ids and not any(k in prior for k in keys):
                blocks.append({'article_id':r['article_id'],'resource_version_id':r['id'],'title':r['title'],'historical_roots':len(all_ids),
                      'state':'needs_live_self_owned_id_and_exact_file_scope','A':'current exact local files, completeness and availability unverified',
                      'B':'current signed textbook coverage for every cloud child unverified','cloud_only_children':None,'deletion_authorized':False})
        return {'observed_at':s['observed_at'],'sync_state':s['sync_state'],'historic_exact_recycled':len(prior),'prior_recycled_linked_to_current_local_registration':mapped,
                'A_verified_current':0,'B_verified_current':0,'new_delete_candidates':0,'needs_evidence_examples':blocks,'execution_allowed':False}
    def discovery(self):
        return {'catalog_observed_at':self.lib.observed_at,'existing_discovery_control':self.crawl,'public_checkpoint':getattr(self.lib,'discovery',None),
                'discovery_network_started_by_review_ui':False,'old_whole_site_transfer':'paused',
                'state':'public_refresh_observed' if getattr(self.lib,'discovery',None) else 'wait_original_read_only_discovery_control',
                'next':'公开发现断点只读；后续按总控明确范围接续，不恢复旧转存、游标或调度。',
                'confirmed_transfers_only':True,'public_details_observed':self.lib.meta['totals']['details'],'known_total':None}
