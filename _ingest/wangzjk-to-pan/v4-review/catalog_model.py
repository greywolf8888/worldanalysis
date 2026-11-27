"""Read-only V4 projection with a separate, loopback-only share link lookup."""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Protocol
from unicodedata import normalize
from urllib.parse import urlsplit

from catalog_relations import CatalogRelations
from catalog_semantics import CATEGORIES as TAXONOMY, CATEGORY_ALIASES, PLATFORMS as PLATFORM_LABELS
from catalog_semantics import CatalogStore, article_key, relationship_input, source_input
from catalog_topics import TOPIC_REVISION, displayed_secondary_topic, intro_preview, project_topic
from catalog_platforms import PLATFORM_REVISION, project_platforms
from intro_inputs import choose_intro, intro_revision, legacy_intro, read_intros
from share_locations import historical_share_locations, merge_share_locations, protected_owner_exports, read_protected_owner_export

ROOT = Path(__file__).absolute().parent
SEMANTIC_DB = ROOT / 'data' / 'pipeline-runs.sqlite3'
SITE = 'wangzjk.com'
ORIGIN = 'https://www.wangzjk.com'
TARGET = '/apps/教育分析学习/wangzjk.com'
CATEGORIES = {'commerce':'电商', 'ai_media':'AI 图文视频', 'growth':'运营增长',
              'business':'商业项目', 'tools':'工具与自动化', 'unsorted':''}
STATUS = {'observed':'发现入口 · 待核范围', 'verified':'历史根级验读',
          'recycled':'已回收 · 不自动重取', 'needs_reconcile':'旧提交待核 · 不重发',
          'restricted':'原站权限受限', 'unknown':''}
PLATFORMS = ('抖音','小红书','快手','视频号','微信','拼多多','淘宝','闲鱼','B站','TikTok','YouTube','公众号')
FILES = ('catalog.json','journal.json','downloaded-project-markers.json','capacity-policy.json','crawl_state.JSON','automation.json')
IMG_EXT = re.compile(r'\.(?:png|jpe?g|webp|gif)$', re.I)
SHARE = re.compile(r'https://pan\.baidu\.com/s/[A-Za-z0-9_-]+\Z')
ART = re.compile(r'[1-9][0-9]{0,17}\Z')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return sha256(compact(value)).hexdigest()


def read_bound(path, max_bytes=24*1024*1024):
    path = path.absolute()
    if path.resolve(strict=True) != path or not path.is_file():
        raise ValueError('非受限根内的常规文件')
    with path.open('rb') as f:
        raw = f.read(max_bytes+1)
    if len(raw) > max_bytes:
        raise ValueError('输入字节超出索引预算')
    return json.loads(raw), sha256(raw).hexdigest()


def norm(s):
    return normalize('NFKC', s).casefold()


def safe_text(value, ceiling=2000):
    if not isinstance(value,str):
        return ''
    value = re.sub(r'https?://[^\s<>「」“”]+', '[链接略]', value, flags=re.I)
    value = re.sub(r'(?:提取码|解压密码|访问密码|访问码|网盘密码|[/&?]pwd|\baccess_token\b|\btoken\b|\bpassword\b)\s*[:：=]?\s*[A-Za-z0-9_+/\-=]{3,}', '[访问凭据略]',value,flags=re.I)
    return ' '.join(value.split())[:ceiling]


def safe_image(s):
    if not isinstance(s,str) or not 0<len(s)<1200 or re.search(r'[\x00-\x20\x7f\\]', s):
        return None
    try:
        u=urlsplit(s)
    except ValueError:
        return None
    if u.scheme!='https' or u.netloc!='www.wangzjk.com' or u.query or u.fragment:
        return None
    if not u.path.startswith('/wp-content/uploads/') or not IMG_EXT.search(u.path):
        return None
    if any(x in u.path.lower() for x in ('..','%','//')):
        return None
    return s


def category(title, fallback=''):
    # Explicit business intent first; platforms are keywords, not categories.
    if re.search(r'带货|选品|供应链|店铺|商品转化|开店|亚马逊|电商|跨境|货源',title):return 'commerce'
    if re.search(r'定位|起号|引流|流量|涨粉|获客|私域|自然流|直播运营|账号运营|投放',title):return 'growth'
    if re.search(r'服务盈利|副业|商业模式|服务变现|变现项目|创业|接单|网赚|赚\s*钱|賺钱|行业模式',title):return 'business'
    if re.search(r'生图|作图|分镜|配音|视频导演|视频制作|短剧|漫剧|绘本|动画|剪辑|提示词.*(?:画|图)|海报|背景音乐',title,re.I):return 'ai_media'
    if re.search(r'工具|插件|软件|脚本|Agent|工作流|自动化|软件开发|投屏|量化',title,re.I):return 'tools'
    return {'03':'commerce','04':'growth','05':'tools'}.get(str(fallback)[:2],'unsorted')


def source_key(s):
    if isinstance(s,str) and SHARE.fullmatch(s):return digest(['baidu',s])
    return None


def exact_root_fact(o):
    """Proof covers selected roots only, not contents or total capacity."""
    if o.get('state')!='saved_root_verified' or o.get('receipt_success') is not True or o.get('verification_scope')!='selected_roots_only':
        return None
    sh = source_key(o.get('share_url')); source = o.get('roots'); target = o.get('verified_rows')
    if not sh or not isinstance(source,list) or not isinstance(target,list) or not source or len(source)!=len(target):return None
    def r(row):
        rid,n,k,b=(row.get(x) for x in ('source_id','name','is_dir','size'))
        if not isinstance(rid,str) or not rid or not isinstance(n,str) or not n or '/' in n or '\\' in n or type(k) is not bool or type(b) is not int or b<0:
            raise ValueError('invalid root shape')
        return [rid,n,k,b]
    try:
        rows=sorted(r(x) for x in source)
        verified=sorted(r(x) for x in target)
        ids={x[0] for x in rows}; tids={x.get('target_id') for x in target}
    except (ValueError,TypeError,AttributeError):return None
    if rows!=verified or len(ids)!=len(source) or len(tids)!=len(target) or not all(isinstance(x,str) and x for x in tids) or ids&tids:return None
    return {'identity':digest([sh,'selected_roots_only',rows]),'scope':'selected_roots_only',
            'roots':[{'name':safe_text(n),'is_dir':k,'bytes':None if k else b} for _,n,k,b in rows],
            'target_ids':tids,'share_key':sh}


class SourceAdapter(Protocol):
    site_id: str
    site_label: str
    entry_origin: str
    def project_page_url(self, article_id: str) -> str: ...
    def discover(self,cursor: int, scope: dict) -> tuple[list[str],int|None]: ...
    def read_article(self,article_id: str) -> dict: ...
    def resolve_resource_ranges(self,article_id: str) -> list[dict]: ...
    def checkpoint(self) -> dict: ...


class Wangzjk:
    site_id=SITE
    site_label='网坠金库'
    entry_origin=ORIGIN

    def project_page_url(self, article_id):
        if not ART.fullmatch(article_id):
            raise ValueError('article binding')
        return f'{self.entry_origin}/{article_id}.html'


    def __init__(self, state, details):
        self.articles=state['catalog.json']['articles']
        self.ops=list(state['journal.json']['operations'].values())
        self.markers=state['downloaded-project-markers.json']['entries']
        self.facts=details.get('articles',{})
        self.by_article=defaultdict(list); self.by_share=defaultdict(list)
        self.target_markers={}
        self.article_download_markers=defaultdict(set)
        self.scoped_holds={x for x,b in state['crawl_state.JSON'].get('blocks',{}).items() if not b.get('resolved') and x.startswith(('article:','share:'))}
        self.floor=state['capacity-policy.json']['minimum_free_bytes']
        for o in self.ops:
            sh=source_key(o.get('share_url'))
            if sh:self.by_share[sh].append(o)
            for aid in o.get('article_ids',[]): self.by_article[str(aid)].append(o)
        for m in self.markers:
            if m.get('cloud_cleanup_state')=='recycled_verified_exact_id' and str(m.get('cloud_root_id','')).isdigit():
                key=str(m['cloud_root_id']); self.target_markers[key]=m
                if m.get('article_id'): self.article_download_markers[str(m['article_id'])].add(key)

    def discover(self,cursor=0,scope=None):
        if not isinstance(cursor,int) or cursor<0: raise ValueError('bad cursor')
        scope=scope or {}; days=scope.get('days'); keys=sorted((i for i,a in self.articles.items()
                if not days or str(a.get('published_at',''))[:10] in days), key=lambda i:(self.articles[i].get('published_at') or '',int(i)),reverse=True)
        block=keys[cursor:cursor+128]; tail=cursor+len(block)
        return block, tail if tail<len(keys) else None

    def resolve_resource_ranges(self,article_id):
        a=self.articles[article_id]; url_keys={source_key(s.get('url')) for s in a.get('shares',[])}-{None}
        # Article-only evidence with a different/unseen current share is not transferable identity.
        return [fact for op in self.by_article[article_id] if (fact:=exact_root_fact(op)) and fact['share_key'] in url_keys]

    def read_article(self,article_id):
        a=self.articles[article_id]; expected=self.project_page_url(article_id)
        if a.get('url')!=expected:raise ValueError('article binding')
        shs=sorted({source_key(s.get('url')) for s in a.get('shares',[])}-{None})
        ops={o['operation_id']:o for o in self.by_article[article_id]}
        for sh in shs:
            # Same link without a known scope is only a duplicate-risk signal; never auto-merge.
            for o in self.by_share[sh]:ops[o['operation_id']]=o
        ranges=self.resolve_resource_ranges(article_id); keys={r['identity'] for r in ranges}
        states={o.get('state') for o in ops.values()}
        consensus=ranges[0] if len(keys)==1 and len(shs)==1 and states=={'saved_root_verified'} else None
        target_ids=set().union(*(x['target_ids'] for x in ranges)); gone={i for i in target_ids if i in self.target_markers}
        source_gone=bool(target_ids) and target_ids==gone
        title=safe_text(a.get('title'),1000); day=str(a.get('published_at') or '')[:10]
        restricted=a.get('access_state') in ('needs_login','needs_access','blocked','paywall')
        if any(x!='saved_root_verified' for x in states) or len(keys)>1 or (states and not ranges):
            status='needs_reconcile'; why='此前已有操作或范围冲突，须核旧回执，不重发。'
        elif source_gone:
            status='recycled'; why='历史精确目标 ID 已进入回收站；不自动重取。'
        elif ranges:
            status='verified'; why='历史转存回执和独立根级读回；不代表当前云端状态、整套下载或本地分析完成。'
        elif restricted:
            status='restricted'; why='原站权限未核准，不尝试绕过。'
        elif a.get('legacy_state') or self.article_download_markers[article_id] or 'article:'+article_id in self.scoped_holds or any('share:'+s.get('url','') in self.scoped_holds for s in a.get('shares',[])):
            status='needs_reconcile'; why='旧隔离或下载去重标记仍保留；未证明新版，不自动重取。'
        elif not shs:
            status='unknown'; why='未见可核的百度网盘公开入口。'
        else:
            status='observed'; why='允许加入草稿；转存范围、授权和实际容量仍须重新核对。'
        if isinstance(self.facts.get(article_id),dict): p=self.facts[article_id]
        else:p={}
        blocks=[]
        for x in p.get('blocks',[])[:800 if p.get('collector')=='codex' else 180]:
            if x.get('kind')=='text':
                if txt:=safe_text(x.get('text'),16000):blocks.append({'kind':'text','text':txt})
            elif x.get('kind')=='image' and (img:=safe_image(x.get('url'))):
                blocks.append({'kind':'image','url':img,'alt':safe_text(x.get('alt'))})
        featured=safe_image(a.get('cover_url'))
        cover=safe_image(p.get('cover_url'))
        if not cover:cover=next((x['url'] for x in blocks if x['kind']=='image'),None)
        excerpt=safe_text(p.get('excerpt'),1600)
        summary=safe_text(a.get('public_excerpt'),360)
        tags=[safe_text(x,160) for x in a.get('categories',[]) if isinstance(x,str)]; tags=list(filter(None,tags))
        search_text=norm(' '.join([title,*tags,excerpt,summary]+[x.get('text','') for x in blocks]))
        keywords=[x for x in PLATFORMS if norm(x) in search_text]
        cls=category(title,a.get('classification',{}).get('folder','')); ident=consensus['identity'] if consensus else None
        revision='arv_'+digest([SITE,article_id,title,day,tags,p.get('publisher_modified'),excerpt,blocks,cover])
        fallback=[SITE,article_id,shs]
        root_status='selected_roots_only' if ranges else 'unknown'
        return {'id':'rv_'+(ident or digest(['unverified_article_reference',fallback])), 'article_id':article_id,
            'article_revision':revision,'project_id':'p_'+(ident or digest([SITE,article_id])),
            'source_id':'src_'+ident if ident else None,'source_revision':'sr_'+digest([ident,consensus['roots']]) if ident else None,
            'site_id':SITE,'site_label':self.site_label,'title':title,'category':cls,'category_label':CATEGORIES[cls],
            'published_at':day,'url':expected,'excerpt':excerpt or summary,
            'cover':{'url':featured or cover,'alt':'原站图：'+title} if featured or cover else None,
            'detail_state':p.get('state','not_sampled') if blocks else ('restricted' if p.get('state')=='restricted' else 'not_sampled'),
            'status':status,'status_label':STATUS[status],'selectable':status=='observed','requestable':status in ('observed','unknown'),'blocked_reason':why,
            'scope':root_status,'keywords':keywords,'source_categories':tags,'blocks':blocks,'observed_at':p.get('observed_at'),
            'article_detail_evidence':{'article_id':article_id,'article_url':expected,'observed_at':p.get('observed_at'),
                'publisher_modified':p.get('publisher_modified'),'read_projection_sha256':p.get('public_api_sha256'),
                'source_api_url':p.get('public_api_url'),'retained_image_count':sum(b['kind']=='image' for b in blocks)} if blocks else None,
            'sources':[{'site_id':SITE,'article_id':article_id,'url':expected,'title':title,'article_revision':revision}],
            'roots':ranges[0]['roots'] if len(keys)==1 else [], 'operations':[{'id':o['operation_id'],'state':o['state']} for o in ops.values()],
            'range_file_count':None,'range_total_bytes':None,'local_status':'历史完整本地副本证据' if gone and all(self.target_markers[i].get('download_scope_complete') is True for i in gone) else '当前本地未独立核验',
            'analysis_status':'未接入正式验收台账','cleanup_status':'历史精确 ID 已回收' if source_gone else '未进行当前云盘复验',
            'old_references':shs,'weak_candidate_count':0}

    def checkpoint(self):
        return {'site_id':SITE,'state':'one_real_source','discovered':len(self.articles),'latest_article_at':max((r.get('published_at') or '' for r in self.articles.values()),default=None),
                'detail_samples':len(self.facts),'site_total':None,'next_cursor':None,'upstream_traversal_complete':False}


class Library:
    def __init__(self, adapters):
        rows=[]; self.sites=[]; self.share_locations={}
        self.project_page_entries = {}
        for adapter in adapters:
            self.sites.append({'id':adapter.site_id,'label':adapter.site_label,'checkpoint':adapter.checkpoint()})
            cursor=0; aliases=set()
            while cursor is not None:
                cards,cursor=adapter.discover(cursor,{})
                if any(x in aliases for x in cards) or len(cards)!=len(set(cards)):raise ValueError('adapter reused article identity')
                aliases.update(cards)
                for article_id in cards:
                    row = adapter.read_article(article_id)
                    if row.get('demo') is not True:
                        self.register_project_page(adapter, article_id, row)
                    rows.append(row)
        self.article_count=len(rows); self.raw_rows=rows; self.by_id={}; title_index=defaultdict(set); shares=defaultdict(set)
        for r in rows:
            key=re.sub(r'[^\w]+','',norm(r['title']))
            if key:title_index[key].add(r['id'])
            for sh in r.get('old_references',[]):shares[sh].add(r['id'])
        def score(row):
            return (len(row['blocks'])>0,bool(row['cover']),len(row['excerpt']),row['published_at'] or '',row['article_id'])
        versions=defaultdict(list)
        for r in rows:versions[r['id']].append(r)
        for key,group in versions.items():
            if len(group)>1 and (not group[0].get('source_id') or any(r.get('scope')!='selected_roots_only' or r.get('source_id')!=group[0]['source_id'] or r.get('source_revision')!=group[0]['source_revision'] for r in group)):
                raise ValueError('unproven identity overlap')
            top=max(group,key=score).copy(); hints=set(); src={}; oo={}
            for r in group:
                hints.update(title_index.get(re.sub(r'[^\w]+','',norm(r['title'])),()))
                for sh in r.pop('old_references',[]):hints.update(shares[sh]);
                for x in r['sources']:src[(x['site_id'],x['article_id'])]=x
                for x in r['operations']:oo[x['id']]=x
            top.pop('old_references',None); top['weak_candidate_count']=len(hints-{key}); top['sources']=list(src.values()); top['operations']=list(oo.values());
            top['versions']=[{'resource_version_id':key,'proof_scope':top['scope'],'source_revision':top['source_revision']}]
            if any(r['status'] in ('recycled','needs_reconcile','verified') for r in group):
                top['selectable']=False; top['requestable']=False
            self.by_id[key]=top
        self.search={k:norm(' '.join([v['title'],v['site_label'],v['site_id'],v['excerpt'],v['category_label'],*v['keywords'],*v['source_categories'],*[s['title'] for s in v['sources']],*[b.get('text','') for b in v['blocks']]])) for k,v in self.by_id.items()}
        self.selectable_count=sum(r['selectable'] for r in self.by_id.values())
        self.requestable_count=sum(r.get('requestable',r['selectable']) for r in self.by_id.values())

    @staticmethod
    def summary(r):
        omit = {'blocks','sources','roots','operations','versions','article_detail_evidence','analysis','input_hash',
                'coverage','other_sources','other_versions','paragraphs','excerpt_full','source_kind'}
        return {k:v for k,v in r.items() if k not in omit and not k.startswith('_')}

    def attach_catalog(self, source, store, intros):
        self.semantic_inputs = {}
        self.semantic_rows = {}
        for raw in self.raw_rows:
            site, aid = raw['site_id'], str(raw['article_id'])
            source_article = source.articles[aid]
            historic = legacy_intro(site, aid, source.facts.get(aid))
            saved = choose_intro(intros.get((site, aid)), historic, source_article.get('publisher_modified'))
            value = source_input(raw, saved, source_article)
            self.semantic_inputs[value['article_key']] = value
        classified, relations, self.stale_relations = store.read(self.semantic_inputs)
        self.semantic_classified = classified
        self.semantic_relations = relations
        pending={article_key(ref['site_id'],ref['article_id']) for relation in relations if relation['relation']=='uncertain'
                 for ref in (relation['left'],relation['right'])}
        seen = Counter(r['id'] for r in self.raw_rows)
        for raw in self.raw_rows:
            token = article_key(raw['site_id'],raw['article_id'])
            value = self.semantic_inputs[token]
            record_state, analysis = classified.get(token,('pending',None))
            valid = analysis or {}
            category,subtopic = project_topic(value['title'],value['paragraphs'],value['excerpt'],valid,manual=record_state=='manual')
            secondary = displayed_secondary_topic(category,valid,manual=record_state=='manual')
            live_platforms = project_platforms(value['title'],value['paragraphs'],value['excerpt'],valid,
                                               manual=record_state=='manual')
            row = {**raw, 'article_key':token, 'input_hash':relationship_input(value,valid)['input_hash'], 'analysis':valid,
                   'coverage':value['coverage'],'paragraphs':value['paragraphs'],'classification_state':{'agent':'verified','manual':'manual'}.get(record_state,'pending'),
                   'category':category,'category_label':TAXONOMY.get(category,('',()))[0],'subcategory':subtopic,
                   'secondary_category':secondary,
                   'platforms':[{'id':p,'label':PLATFORM_LABELS[p]} for p in live_platforms],
                   'version_label':valid.get('edition') or next((d['text'] for d in valid.get('dates',[]) if d['meaning']=='version'),''),
                   'action_pending':seen[raw['id']]!=1,'relationship_pending':token in pending}
            if row['action_pending']:
                row.update(requestable=False,selectable=False,blocked_reason='资料归属待核实。')
            if not row['excerpt'] and value['paragraphs']:
                row['excerpt']=safe_text(value['paragraphs'][0],360)
            row['card_summary']=intro_preview(value['title'],value['paragraphs'],value['excerpt'])
            row['_search']=norm(' '.join([raw['title'],raw['site_label'],raw['site_id'],row['excerpt'],
                row['category_label'],row['subcategory'],*(p['label'] for p in row['platforms']),*raw['source_categories'],*value['paragraphs']]))
            if value['paragraphs'] and (not row['blocks'] or value['coverage']=='complete'):
                text_blocks=[{'kind':'text','text':part} for part in value['paragraphs']]
                images=[b for b in row['blocks'] if b['kind']=='image']
                row['blocks']=[*text_blocks,*images]
                row['detail_state']='public_sample'
            self.semantic_rows[token]=row
        self.relations=CatalogRelations(self.semantic_rows.values(),relations)

    def exact_article(self, resource_id, token):
        row = getattr(self,'semantic_rows',{}).get(token)
        return row if row and row['id']==resource_id else None

    def facets(self, *, eligible=None):
        if not hasattr(self,'relations'):
            rows=list(self.by_id.values())
            return {
                'categories':[{'id':k,'label':v,'count':sum(r['category']==k for r in rows)} for k,v in CATEGORIES.items() if v],
                'platforms':[],'category_aliases':CATEGORY_ALIASES,'project_count':len(rows)}
        valid = [r for r in self.semantic_rows.values() if eligible is None or eligible(r)]
        project_count = self.relations.count(valid)
        category_sets=defaultdict(list)
        platform_sets=defaultdict(list)
        sub_sets=defaultdict(list)
        for row in valid:
            category_sets[row['category']].append(row)
            secondary=row.get('secondary_category')
            if secondary:
                category_sets[secondary].append(row)
            for platform in row['platforms']:
                platform_sets[platform['id']].append(row)
            if row['subcategory']:
                sub_sets[(row['category'],row['subcategory'])].append(row)
        def count(items):
            return self.relations.count(items) if items else 0
        categories=[]
        for key,(label,tags) in TAXONOMY.items():
            categories.append({'id':key,'label':label,'count':count(category_sets[key]),
                        'subcategories':[{'id':tag,'label':tag,'count':count(sub_sets[(key,tag)])}
                                         for tag in tags if sub_sets[(key,tag)]]})
        return {'categories':categories,'platforms':[{'id':p,'label':label,'count':count(platform_sets[p])}
                 for p,label in PLATFORM_LABELS.items() if p!='general' or platform_sets[p]],
                'category_aliases':CATEGORY_ALIASES,'project_count':project_count}

    def display_detail(self,row):
        if not hasattr(self,'relations'):
            return row
        projected = self.relations.details(row)
        if isinstance(projected,dict):
            return {**row,**projected}
        return row


    def register_project_page(self, adapter, article_id, row):
        identity = (adapter.site_id, article_id)
        origin = getattr(adapter, 'entry_origin', '')
        declared = urlsplit(origin)
        canonical_page = getattr(adapter, 'project_page_url', None)
        address = row.get('url')
        if not callable(canonical_page) or not isinstance(address, str) or address != canonical_page(article_id):
            raise ValueError('Unverified project entry')
        page = urlsplit(address)
        if (declared.scheme != 'https' or not declared.hostname or origin != f'https://{declared.netloc}'
                or declared.netloc != declared.hostname or page.netloc != declared.netloc or page.scheme != 'https'
                or not page.path.startswith('/') or page.path == '/' or page.query or page.fragment
                or len(address) > 2000 or re.search(r'[\x00-\x20\x7f\\]', address)
                or (row.get('site_id'), row.get('article_id')) != identity or identity in self.project_page_entries):
            raise ValueError('Invalid project entry')
        if not any((ref['site_id'], ref['article_id']) == identity and ref.get('url') == address for ref in row['sources']):
            raise ValueError('Project source does not bind to its entry')
        self.project_page_entries[identity] = {'href':address,'origin':origin,'site_label':adapter.site_label,'title':row['title']}

    def project_entries(self, row):
        return [self.project_page_entries[key] for ref in row['sources']
                if (key := (ref['site_id'], ref['article_id'])) in self.project_page_entries]

    def baidu_links(self, row):
        locations = (self.share_locations.get((ref['site_id'], ref['article_id']), ()) for ref in row['sources'])
        return [link.href for link in merge_share_locations(*locations)]

    def match(self,f,selected,*,eligible=None):
        terms=norm(f.get('q','')).split(); after,before=f.get('from',''),f.get('to',''); category,status,site=(f.get(x,'') for x in ('category','status','site'))
        if hasattr(self,'relations'):
            category= CATEGORY_ALIASES.get(category,category)
            subgroup, platform = f.get('subcategory',''),f.get('platform','')
            items=[row for row in self.semantic_rows.values()
                   if (eligible is None or eligible(row))
                   and (not terms or all(term in row['_search'] for term in terms))
                   and (not category or category in (row['category'],row.get('secondary_category')))
                   and (not subgroup or row['category']==category and row['subcategory']==subgroup)
                   and (not platform or any(p['id']==platform for p in row['platforms']))
                   and (not status or status==row['status']) and (not site or row['site_id']==site)
                   and (f.get('baidu')!='1' or (row['site_id'],row['article_id']) in self.share_locations)
                   and (not after or row['published_at']>=after)
                   and (not before or row['published_at'] and row['published_at']<=before)
                   and (f.get('selected')!='1' or row['id'] in selected and not row['action_pending'])]
            items=self.relations.project(items,keep_selected=selected if f.get('selected')=='1' else ())
            return self.sort_items(items,f)
        items=[r for k,r in self.by_id.items() if (not terms or all(t in self.search[k] for t in terms))
            and (not category or category==r['category']) and (not status or status==r['status'])
            and (not site or any(s['site_id']==site for s in r['sources']))
            and (f.get('baidu')!='1' or any((s['site_id'],s['article_id']) in self.share_locations for s in r['sources']))
            and (not after or r['published_at']>=after) and (not before or r['published_at'] and r['published_at']<=before)
            and (f.get('selected')!='1' or k in selected)]
        return self.sort_items(items,f)

    @staticmethod
    def sort_items(items,f):
        def key(r):return (r['published_at'] or '',int(r['article_id']) if str(r['article_id']).isdigit() else 0,r['id'])
        if f.get('order')=='title':return sorted(items,key=lambda r:(norm(r['title']),r['id']))
        if f.get('order')=='oldest':return sorted(items,key=lambda r:(not bool(r['published_at']),*key(r)))
        return sorted(items,key=key,reverse=True)


def merge_codex_collection(path, articles, facts):
    doc, checksum = read_bound(path, 64*1024*1024)
    history=doc.get('history'); collection=doc.get('collection')
    rows=doc.get('articles'); details=doc.get('details')
    if (doc.get('schema')!='wa-v4-codex-public-source.v1' or doc.get('site')!=SITE
            or not isinstance(rows,dict) or len(rows)>60_000 or not isinstance(details,dict) or len(details)>400
            or not isinstance(history,dict) or not isinstance(collection,dict) or collection.get('read_only') is not True):
        raise ValueError('公开资料目录校验失败')
    for key,row in rows.items():
        if (not ART.fullmatch(key) or not isinstance(row,dict) or row.get('article_id')!=key
                or row.get('url')!=ORIGIN+'/'+key+'.html' or not re.fullmatch(r'20\d\d-\d\d-\d\d',row.get('published_at',''))
                or set(row)-{'article_id','url','published_at','title','publisher_modified','cover_url','public_excerpt','publisher_published','first_collected_at'}):
            raise ValueError('公开文章身份校验失败')
        record=articles.get(key)
        if record is None:
            record={'article_id':key,'url':row['url'],'published_at':row['published_at'],'title':safe_text(row.get('title'),1000),
                    'classification':{},'categories':[],'shares':[],'access_state':'unknown'}
            articles[key]=record
        record['title']=safe_text(row.get('title'),1000) or record['title']
        record['published_at']=row['published_at']
        record['publisher_modified']=row.get('publisher_modified')
        if row.get('publisher_published'):
            record['publisher_published']=row['publisher_published']
        if row.get('first_collected_at'):
            record.setdefault('first_seen_at',row['first_collected_at'])
        record['cover_url']=safe_image(row.get('cover_url'))
        record['public_excerpt']=safe_text(row.get('public_excerpt'),360)
    for key,detail in details.items():
        if not isinstance(detail,dict) or key not in rows or detail.get('state')!='public_sample' or detail.get('article_url')!=rows[key]['url']:
            raise ValueError('公开正文与文章身份不符')
        previous=facts.get(key)
        if not previous or previous.get('state')!='public_sample' or str(detail.get('observed_at',''))>=str(previous.get('observed_at','')):
            facts[key]=detail
    return checksum, {k:collection.get(k) for k in ('observed_at','stop_reason','publisher_total','catalog_complete','history_observed','new_this_run','details_added','read_only')}


def catalog_input_stamp():
    paths=(ROOT.parent/'state'/'catalog.json',ROOT/'data'/'public-details.json',
           ROOT/'data'/'discovery-head.json',ROOT/'data'/'codex-public.json')
    version=[]
    for path in paths:
        try:
            stat=path.stat()
            version.append((stat.st_mtime_ns,stat.st_size,stat.st_ino))
        except FileNotFoundError:
            version.append(None)
    return (*version,intro_revision(SEMANTIC_DB))


def open_index():
    data_stamp=catalog_input_stamp()
    store=CatalogStore(SEMANTIC_DB)
    catalog_revision=store.revision()
    before={name:read_bound(ROOT.parent/'state'/name) for name in FILES}
    facts, ref=read_bound(ROOT/'data'/'public-details.json')
    # Compare bytes after assembling the multi-file snapshot; never publish a mixed generation.
    if any(read_bound(ROOT.parent/'state'/k)[1]!=v[1] for k,v in before.items()):raise ValueError('上游读取期间发生变化')
    state={k:v[0] for k,v in before.items()}; provenance={k:v[1] for k,v in before.items()}
    overlay_path=ROOT/'data'/'discovery-head.json'; overlay_hash=None; discovery=None; codex_hash=None
    baseline_articles=len(state['catalog.json']['articles'])
    if overlay_path.exists():
        overlay,overlay_hash=read_bound(overlay_path,3_000_000)
        if overlay.get('schema')!='wa-v4-public-discovery-checkpoint.v1' or overlay.get('read_only') is not True or overlay.get('site')!=SITE or overlay.get('no_transfer_claim_or_schedule') is not True:
            raise ValueError('独立发现输入不符合只读契约')
        articles=state['catalog.json']['articles']; new=overlay['additional_articles']; detail=overlay['detail_overlay']
        if (len(new)>2000 or len(detail)>1000 or any(not ART.fullmatch(i) or r.get('article_id')!=i or r.get('url')!=ORIGIN+'/'+i+'.html'
                or r.get('normal_publisher_listing') is not True or r.get('shares')!=[] or r.get('access_state')!='unknown'
                or not re.fullmatch(r'20[0-9]{2}-[0-9]{2}-[0-9]{2}',r.get('published_at','')) for i,r in new.items())):
            raise ValueError('独立发现只可附加公开条目，不可添加实际转存来源')
        existing=set(articles); articles.update({i:{**a,'title':safe_text(a['title'],1000)} for i,a in new.items() if i not in existing})
        for i,p in detail.items():
            if i in articles and i not in facts['articles'] and (p.get('state')!='public_sample' or p.get('article_url')==articles[i]['url']):facts['articles'][i]=p
        discovery={'observed_at':overlay.get('observed_at'),'day':overlay.get('day'),'day_count':overlay.get('visible_posts_count'),'frontier':overlay.get('original_queue_frontier'),
                   'new_articles':len(articles)-len(existing),'read_only':True,'stop_reason':overlay.get('stop_reason'),'pending_detail_count':len(overlay.get('next_uninspected_ids',[])),'checkpoint_sha256':overlay_hash}
    codex_path=ROOT/'data'/'codex-public.json'
    if codex_path.exists():
        codex_hash, public_status=merge_codex_collection(codex_path,state['catalog.json']['articles'],facts['articles'])
        discovery={**public_status,'collector':'codex','checkpoint_sha256':codex_hash,
                   'new_articles':len(state['catalog.json']['articles'])-baseline_articles}
    if state['crawl_state.JSON'].get('phase')!='paused' or state['capacity-policy.json'].get('minimum_free_bytes')!=100_000_000_000:
        raise ValueError('未核实旧自动转存暂停或 100 GB 持久策略')
    if facts.get('schema')!='wangzjk-v4-public-article-projection.v1' or facts.get('site_id')!=SITE:raise ValueError('详情数据契约不符')
    source=Wangzjk(state,facts); lib=Library([source]); lib.snapshot_id=digest([provenance,ref,overlay_hash,codex_hash]) if codex_hash else (digest([provenance,ref,overlay_hash]) if overlay_hash else digest([provenance,ref])); lib.discovery=discovery; lib.provenance=provenance; lib.detail_hash=ref
    if codex_hash and len(lib.sites)==1:
        lib.sites[0]['checkpoint']['site_total']=discovery.get('publisher_total')
        lib.sites[0]['checkpoint']['upstream_traversal_complete']=discovery.get('catalog_complete') is True
    historical = {(SITE, aid): links for aid, row in source.articles.items() if (links := historical_share_locations(row))}
    member_links = {}
    for path in protected_owner_exports():
        for key, links in read_protected_owner_export(path, source.articles).items():
            member_links[key] = merge_share_locations(member_links.get(key, ()), links)
    lib.share_locations = {**historical, **member_links}
    lib.operations_snapshot_id=lib.snapshot_id
    lib.catalog_revision=catalog_revision
    lib.attach_catalog(source,store,read_intros(SEMANTIC_DB))
    if catalog_input_stamp()!=data_stamp or store.revision()!=catalog_revision:
        raise ValueError('资料读取期间发生更新，请稍后重读')
    lib.catalog_inputs_stamp=data_stamp
    lib.snapshot_id=digest([lib.operations_snapshot_id,catalog_revision,data_stamp[-1],TOPIC_REVISION,PLATFORM_REVISION])
    facets=lib.facets()
    lib.catalog_all=lib.match({},set())
    lib.requestable_count=sum(r['requestable'] for r in lib.catalog_all)
    lib.minimum_free_bytes=source.floor; lib.observed_at=now(); cnt=Counter(o.get('state') for o in source.ops)
    lib.meta={'snapshot_id':lib.snapshot_id,'totals':{'articles':lib.article_count,'projects':facets['project_count'],'versions':len(lib.by_id),'details':sum(bool(r['blocks']) for r in lib.by_id.values()),
              'images':len({x['url'] for r in lib.by_id.values() for x in [*(y for y in r['blocks'] if y['kind']=='image'),*([r['cover']] if r['cover'] else [])]}),
              'operations':len(source.ops),'verified':cnt['saved_root_verified'],'needs_reconcile':len(source.ops)-cnt['saved_root_verified'],
              'recycled':len(source.target_markers),'selectable':lib.selectable_count,'requestable':lib.requestable_count,
              'baidu_linked_articles':len(lib.share_locations),'baidu_unique_links':len({link.url for links in lib.share_locations.values() for link in links})},'categories':facets['categories'],'platforms':facets['platforms'],'category_aliases':facets['category_aliases'],
              'statuses':[{'id':k,'label':v} for k,v in STATUS.items()],'sites':lib.sites,'minimum_free_bytes':source.floor,'execution_allowed':False,
              'observed_at':lib.observed_at,'discovery':discovery,'source_notes':facts.get('attempts',[]),'overview_note':'状态来自历史账和精确回收标记；实际容量未实时查询，本地分析与回收资格不推断。'}
    return lib
