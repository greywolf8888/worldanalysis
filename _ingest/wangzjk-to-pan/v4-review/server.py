"""Loopback resource library, review and fixed operator jobs."""
from __future__ import annotations
import argparse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from time import perf_counter
from urllib.parse import parse_qs, urlsplit
from catalog_model import CATEGORIES, STATUS, ROOT, compact, digest, now, open_index

from production_bridge import LocalProjection
from confirm_gate import readiness
from analysis_priority import AnalysisPriority, AnalysisPriorityError
from member_queue import MAX_LIMIT, DEFAULT_NON_PROJECTS, DEFAULT_OBSERVATIONS, build_worklist, read_backfill, read_non_projects, read_observations
from share_locations import MAX_BROWSER_IMPORT_BYTES, import_browser_share_locations, protected_owner_exports
from course_calendar import CalendarRequestError, course_calendar
from pipeline_control import JOBS, PipelineControl, RunBusy

INDEX={"/":("index.html","text/html"),"/style.css":("style.css","text/css"),"/app.js":("app.js","text/javascript"),"/production.js":("production.js","text/javascript"),
       "/priority.js":("priority.js","text/javascript"),"/priority.css":("priority.css","text/css"),
       "/member-import":("member-import.html","text/html"),"/member-import.js":("member-import.js","text/javascript"),
       "/courses":("courses.html","text/html"),"/courses.css":("courses.css","text/css"),"/courses.js":("courses.js","text/javascript"),
       "/control":("control.html","text/html"),"/control.css":("control.css","text/css"),"/control.js":("control.js","text/javascript")}
ID=re.compile(r'rv_[0-9a-f]{64}\Z')
DEFAULT={'q':'','category':'','status':'','site':'','from':'','to':'','order':'newest','view':'grid','selected':'0','baidu':'0','page':1,'scroll':0}
MAX_ID=5000

class RequestError(Exception):
    def __init__(self, code, msg): self.code=code; self.msg=msg


def filters(a, library):
    if type(a) is not dict or a.keys()-DEFAULT.keys():raise RequestError(400,'未知筛选参数')
    f={**DEFAULT,**a}
    for k in ('q','category','status','site','from','to','order','view','selected','baidu'):
        if not isinstance(f[k],str) or len(f[k])>250 or '\x00' in f[k]:raise RequestError(400,'筛选参数无效')
    if (f['category'] and f['category'] not in CATEGORIES or f['status'] and f['status'] not in STATUS
            or f['site'] and f['site'] not in {x['id'] for x in library.sites}
            or f['order'] not in ('title','oldest','newest') or f['view'] not in ('grid','list')
            or f['selected'] not in ('0','1') or f['baidu'] not in ('0','1')):
        raise RequestError(400,'筛选范围无效')
    try:
        for k in ('from','to'):
            if f[k] and (len(f[k])!=10 or date.fromisoformat(f[k]).isoformat()!=f[k]):raise ValueError()
        if f['from'] and f['to'] and f['from']>f['to']:raise ValueError()
        for k,L,H in [('page',1,100000),('scroll',0,2**24)]:
            if type(f[k]) is str and f[k].isascii() and len(f[k])<=9 and f[k].isdigit():f[k]=int(f[k])
            if type(f[k]) is not int or not L<=f[k]<=H:raise ValueError()
    except (ValueError,TypeError):raise RequestError(400,'页码、滚动或日期无效') from None
    return f


def select_page(rows,f):
    limit=12 if f['view']=='grid' else 16
    pages=max(1,(len(rows)+limit-1)//limit); page=min(f['page'],pages)
    return rows[(page-1)*limit:page*limit],page,pages,limit


def requestable(row):
    return row.get('requestable',row.get('selectable')) is True


class Draft:
    """Single fixed DB in data/ or a test's private directory. Revision CAS is transactional."""
    def __init__(self, path, lib):
        self.path=Path(path).absolute(); self.lib=lib
        if self.path.parent.resolve(strict=True)!=self.path.parent or self.path.resolve()!=self.path or self.path.is_symlink():
            raise ValueError('unsafe draft path')
    def connect(self):
        if self.path.resolve()!=self.path or self.path.is_symlink():raise ValueError('unsafe draft path')
        c=sqlite3.connect(self.path,timeout=3)
        c.execute('CREATE TABLE IF NOT EXISTS draft (single INTEGER PRIMARY KEY CHECK(single=1), body TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS review (identity TEXT PRIMARY KEY, body TEXT NOT NULL)')
        return c
    @staticmethod
    def blank():return {'revision':0,'ids':[],'prefs':dict(DEFAULT),'bindings':{},'updated_at':None}
    @classmethod
    def load(cls,c):
        row=c.execute('SELECT body FROM draft WHERE single=1').fetchone()
        return json.loads(row[0]) if row else cls.blank()
    def read(self):
        if not self.path.exists():return self.blank()
        c=self.connect()
        try:return self.load(c)
        finally:c.close()
    def entry(self,key,d):
        row=self.lib.by_id.get(key)
        if not row:return {'id':key,'title':'资料待重新核对','selectable':False,'requestable':False,'scope':'unknown','blocked_reason':'需要重新核对已选资料。'}
        s=self.lib.summary(row); stale=d['bindings'].get(key)!=self.bind(row)
        if stale:s={**s,'stale':True,'selectable':False,'requestable':False,'blocked_reason':'文章或证据版本改变，须取消后重新人工确认。'}
        return s
    @staticmethod
    def bind(r):return {k:r[k] for k in ('article_revision','source_id','source_revision','site_id','article_id','scope')}
    def show(self,d,entries=True):
        r={k:v for k,v in d.items() if k!='bindings'}
        if entries:r['entries']=[self.entry(i,d) for i in d['ids']]
        return r
    def mutate(self,body):
        allowed={'revision','action','id','selected','scope','filter','position','prefs','expected_count','snapshot_id'}
        if type(body) is not dict or body.keys()-allowed or type(body.get('revision')) is not int:raise RequestError(400,'缺草稿版本或含未知字段')
        c=self.connect()
        try:
            c.execute('BEGIN IMMEDIATE'); d=self.load(c); ids=d['ids']; action=body.get('action')
            if body['revision']!=d['revision']:raise RequestError(409,'草稿已被另一页更新，请重读，不会自动重试')
            if 'snapshot_id' in body and body['snapshot_id']!=self.lib.snapshot_id:raise RequestError(409,'目录版本已更新，请重新核对范围')
            def append(keys):
                valid=[k for k in keys if isinstance(k,str) and requestable(self.lib.by_id.get(k,{}))]
                if len(valid)!=len(keys):raise RequestError(422,'已有存档或受限项目不能加入转存清单')
                seen=set(ids)
                for k in valid:
                    if k not in seen:
                        ids.append(k); seen.add(k); d['bindings'][k]=self.bind(self.lib.by_id[k])
            k=body.get('id')
            if action in ('toggle','remove','move') and (not isinstance(k,str) or not ID.fullmatch(k)):raise RequestError(400,'无效的资料版本 ID')
            if action=='toggle':
                if type(body.get('selected')) is not bool:raise RequestError(400,'需要明确勾选状态')
                if body['selected']: append([k])
                elif k in ids:ids.remove(k); d['bindings'].pop(k,None)
            elif action=='remove':
                if k in ids:ids.remove(k); d['bindings'].pop(k,None)
            elif action=='clear':ids.clear();d['bindings'].clear()
            elif action=='move':
                n=body.get('position')
                if k not in ids or type(n) is not int or not 1<=n<=len(ids):raise RequestError(400,'无效的草稿位置')
                ids.remove(k); ids.insert(n-1,k)
            elif action=='prefs':d['prefs']=filters(body.get('prefs'),self.lib)
            elif action=='bulk':
                if body.get('scope') not in ('page','filtered','site_all') or body.get('snapshot_id')!=self.lib.snapshot_id or type(body.get('expected_count')) is not int:
                    raise RequestError(409,'必须先核对该目录快照的全选范围及精确数量')
                f=filters(body.get('filter',{}),self.lib)
                if body['scope']=='site_all':f={**DEFAULT,'order':f['order']}
                candidates=self.lib.match(f,set(ids))
                if body['scope']=='page':candidates=select_page(candidates,f)[0]
                keys=[r['id'] for r in candidates if requestable(r)]
                if len(keys)!=body['expected_count']:raise RequestError(409,'全选的可选数量已变化，请重新核对')
                append(keys)
            else:raise RequestError(400,'不支持的草稿动作')
            if len(ids)>MAX_ID:raise RequestError(422,'草稿规模超出安全上限')
            d['revision']+=1;d['updated_at']=now()
            c.execute('INSERT OR REPLACE INTO draft(single,body) VALUES(1,?)',(compact(d).decode(),));c.commit()
            return self.show(d)
        finally:c.close()
    def freeze(self,b):
        if type(b) is not dict or set(b)!={'revision','order_mode'} or type(b['revision']) is not int or b['order_mode'] not in ('ready_ordered','strict'):
            raise RequestError(400,'复核需要当前草稿版本和明确顺序模式')
        c=self.connect()
        try:
            c.execute('BEGIN IMMEDIATE');d=self.load(c)
            if b['revision']!=d['revision']:raise RequestError(409,'草稿版本改变，未冻结')
            if not d['ids']:raise RequestError(422,'没有已勾选资料')
            if any(not requestable(self.entry(k,d)) for k in d['ids']):raise RequestError(409,'有过期或不可选资料，未保存顺序')
            key=digest([self.lib.snapshot_id,d['revision'],d['ids'],d['bindings'],b['order_mode']]); old=c.execute('SELECT body FROM review WHERE identity=?',(key,)).fetchone()
            if old:return json.loads(old[0])
            items=[]
            for n,k in enumerate(d['ids'],1):
                r=self.lib.by_id[k]; items.append({'queue_item_id':'draft_'+digest([key,k]),'resource_version_id':k,
                    **{x:r[x] for x in ('article_revision','site_id','article_id','source_id','source_revision')},
                    'range_manifest_ref':None,'item_rank':n,'transfer_task_id':None})
            from v4_exchange import selection_sha256
            v={'schema_version':1,'record_type':'draft_review','producer':'dsh','event_id':'draft_'+key,'revision':d['revision'],
                'observed_at':now(),'created_at':now(),'state':'draft_review','execution_allowed':False,'authorization_ref':None,
                'order_mode':b['order_mode'],'items':items,'selection_sha256':selection_sha256(items),'scope':'local_review_not_authority',
                'verified_remaining_bytes':None,'blocked_reason':'资料范围、实际余量和用户明确授权未核；本记录不是 confirmed_plan，不能执行。'}
            c.execute('INSERT INTO review VALUES(?,?)',(key,compact(v).decode()));c.commit();return v
        finally:c.close()


class LiveSnapshot:
    """Accept an atomic local public checkpoint on demand; no polling thread or I/O to any website."""
    def __init__(self, library, draft, local, watch=None):
        self.watch=(watch,) if isinstance(watch,Path) else tuple(watch or ());self.lock=RLock();self.current=(library,draft,local)
        self.last_attempt=self.version() if self.watch else None;self.pending=False

    def version(self):
        versions = []
        paths = self.watch + protected_owner_exports()
        for position,path in enumerate(paths):
            try:
                if path.resolve(strict=True)!=path or not path.is_file():raise ValueError('unbound checkpoint')
                stat=path.stat();versions.append((path,stat.st_mtime_ns,stat.st_size,stat.st_ino))
            except FileNotFoundError:
                if position==0:raise
                versions.append(None)
        return tuple(versions)

    def get(self):
        with self.lock:
            if not self.watch:return (*self.current,False)
            try:
                key=self.version()
                if key!=self.last_attempt:
                    self.pending=True
                    cur,draft,local=self.current
                    lib=open_index()
                    if self.version()!=key:raise ValueError('checkpoint moved')
                    public_changed=lib.snapshot_id!=cur.snapshot_id
                    if public_changed or lib.share_locations!=cur.share_locations:
                        # The local projection uses the public rows and historical provenance, not share locators.
                        self.current=(lib,Draft(draft.path,lib),LocalProjection(lib) if public_changed else local)
                    self.last_attempt=key
                    self.pending=False
            except (OSError,ValueError,TypeError,KeyError):self.pending=True
            return (*self.current,self.pending)


class Handler(BaseHTTPRequestHandler):
    server_version='WAReview/4'
    def setup(self):
        super().setup(); self.connection.settimeout(8)
    def log_message(self,*args):pass
    def send(self,code,body,content='application/json'):
        if not isinstance(body,bytes):body=compact(body)
        self.send_response(code);self.send_header('Content-Type',content+'; charset=UTF-8'); self.send_header('Content-Length',str(len(body)))
        self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' https://www.wangzjk.com; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'; form-action 'none'")
        self.send_header('Referrer-Policy','no-referrer');self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY')
        self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
    def member_worklist(self, query):
        origins = self.headers.get_all('Origin', [])
        if self.headers.get_all('Sec-Fetch-Site', []) != ['same-origin'] or origins not in ([], ['http://'+self.server.authority]):
            raise RequestError(403, '请从本机的链接导入页读取')
        try:
            params = parse_qs(query, keep_blank_values=True, max_num_fields=2)
            if set(params) - {'limit', 'resume'} or any(len(values) != 1 for values in params.values()):
                raise ValueError()
            amount = params.get('limit', ['72'])[0]
            resume = params.get('resume', ['1'])[0]
            if not re.fullmatch(r'[1-9][0-9]{0,2}', amount) or not 1 <= int(amount) <= MAX_LIMIT or resume not in ('0', '1'):
                raise ValueError()
        except ValueError:
            raise RequestError(400, '本次读取数量或方式无效') from None
        with self.server.snapshot.lock:
            library, _, _, pending = self.server.snapshot.get()
            if pending:
                raise RequestError(503, '资料目录正在更新，请稍后再读取')
            before_id = read_backfill()['before_id'] if resume == '1' else None
            worklist = build_worklist(library, read_observations(DEFAULT_OBSERVATIONS),
                                      non_projects=read_non_projects(DEFAULT_NON_PROJECTS),
                                      limit=int(amount), before_id=before_id)
        return self.send(200, worklist)
    def save_member_progress(self):
        if (self.headers.get_all('Origin', []) != ['http://'+self.server.authority]
                or self.headers.get_all('Sec-Fetch-Site', []) != ['same-origin']
                or self.headers.get_content_type() != 'application/json' or self.headers.get_all('Transfer-Encoding')):
            raise RequestError(403, '请从本机的链接导入页保存')
        from member_progress import REPORT_BYTES, record_batch

        length = self.headers.get_all('Content-Length', [])
        if len(length) != 1 or not re.fullmatch(r'[0-9]{1,5}', length[0]) or not 1 <= int(length[0]) <= REPORT_BYTES:
            raise RequestError(413, '本批结果为空或超出长度，请分批保存')
        try:
            raw = self.rfile.read(int(length[0]))
            if len(raw) != int(length[0]):
                raise ValueError()
            body = json.loads(raw.decode('utf-8'))
            if type(body) is not dict or set(body) != {'advance', 'report'} or type(body['advance']) is not bool:
                raise ValueError()
        except (UnicodeError, ValueError, RecursionError):
            raise RequestError(400, '请检查本批核对结果的格式') from None
        try:
            with self.server.snapshot.lock:
                result = record_batch(body['report'], advance=body['advance'])
        except ValueError:
            return self.send(409, {'error': '请重新读取待采清单，核对本批项目和采集位置后再保存。'})
        except OSError:
            return self.send(503, {'error': '暂时无法保存。请保留本批结果，稍后重试。'})
        return self.send(200, result)
    def start_control_run(self):
        if (self.headers.get_all('Origin', []) != ['http://' + self.server.authority]
                or self.headers.get_all('Sec-Fetch-Site', []) != ['same-origin']
                or self.headers.get_content_type() != 'application/json' or self.headers.get_all('Transfer-Encoding')):
            raise RequestError(403, '请从本机控制台启动操作')
        size = self.headers.get_all('Content-Length', [])
        if len(size) != 1 or not re.fullmatch(r'[0-9]{1,3}', size[0]) or not 1 <= int(size[0]) <= 160:
            raise RequestError(413, '操作请求长度无效')
        try:
            raw = self.rfile.read(int(size[0]))
            if len(raw) != int(size[0]):
                raise ValueError()
            body = json.loads(raw.decode('utf-8'))
            if type(body) is not dict or set(body) != {'action'} or type(body['action']) is not str or body['action'] not in JOBS:
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError):
            raise RequestError(400, '该控制台操作不可用') from None
        try:
            return self.send(202, self.server.control.start(body['action']))
        except RunBusy as error:
            return self.send(409, {'error': str(error)})
    def route(self,method):
        if self.headers.get_all('Host')!=[self.server.authority]:raise RequestError(403,'仅支持本机绑定地址')
        if len(self.path)>4096 or not self.path.startswith('/') or self.path.startswith('//'):raise RequestError(400,'请求路径无效')
        u=urlsplit(self.path);p=u.path
        if p in INDEX and method=='GET' and (not u.query or p in ('/', '/courses')):
            name,m=INDEX[p]; f=ROOT/'public'/name
            if f.resolve(strict=True)!=f or not f.is_file():raise RequestError(404,'界面文件不可用')
            return self.send(200,f.read_bytes(),m)
        if p == '/api/member-worklist' and method == 'GET':
            return self.member_worklist(u.query)
        if p == '/api/member-progress' and method == 'POST' and not u.query:
            return self.save_member_progress()
        if p == '/api/control' and method == 'POST' and not u.query and self.server.control is not None:
            return self.start_control_run()
        lib,draft,local,pending=self.server.snapshot.get()
        analysis=AnalysisPriority(draft.path.with_name('analysis-priority.sqlite3'))
        if method=='GET':
            if p == '/api/course-calendar':
                try:
                    return self.send(200, course_calendar(lib, u.query))
                except CalendarRequestError as error:
                    return self.send(error.status, {'error': str(error)})
            if p == '/api/control' and not u.query and self.server.control is not None:
                return self.send(200, self.server.control.overview(lib, draft.read(), local, pending, analysis.read(lib, local)))
            if p=='/api/meta':return self.send(200,{**lib.meta,'production_integration':'v4-dsh-cutover-20261126-004','public_checkpoint_pending_sync':pending})
            if p=='/api/draft':return self.send(200,draft.show(draft.read()))
            if p=='/api/analysis-priority' and not u.query:return self.send(200,analysis.read(lib,local))
            if p=='/api/local-status' and not u.query:return self.send(200,local.overview())
            if p=='/api/local-projects':
                pr=parse_qs(u.query,keep_blank_values=True,max_num_fields=2)
                if set(pr)-{'page'} or len(pr.get('page',['1']))!=1:raise RequestError(400,'无效本地分页')
                return self.send(200,local.local_rows(filters({'page':pr.get('page',['1'])[0]},lib)['page']))
            if p=='/api/preconfirmation' and not u.query:return self.send(200,readiness(lib,local,draft.read()))
            if p=='/api/cleanup-readiness' and not u.query:return self.send(200,local.cleanup_preparation())
            if p=='/api/discovery-readiness' and not u.query:return self.send(200,{**local.discovery(),'public_checkpoint_pending_sync':pending})
            if p.startswith('/api/resource/') and ID.fullmatch(x:=p[len('/api/resource/'):]) and x in lib.by_id:
                row = lib.by_id[x]
                return self.send(200,{**row,'local_projection':local.detail(row),'baidu_share_links':lib.baidu_links(row),
                                      'project_entries':lib.project_entries(row)})
            if p=='/api/resources':
                t=perf_counter(); raw=parse_qs(u.query,keep_blank_values=True,max_num_fields=16)
                if any(len(v)!=1 for v in raw.values()):raise RequestError(400,'筛选参数重复')
                f=filters({k:v[0] for k,v in raw.items()},lib);d=draft.read();selected=set(d['ids'])
                rows=lib.match(f,selected); page,cur,pages,limit=select_page(rows,f); visible={r['id'] for r in page}
                return self.send(200,{'items':[lib.summary(r) for r in page],'total':len(rows),'eligible':sum(requestable(r) for r in rows),
                    'page':cur,'pages':pages,'limit':limit,'index_selectable':lib.requestable_count,'page_selectable':sum(requestable(r) for r in page),'max_draft_items':MAX_ID,
                    'draft':draft.show(d,False),'hidden_selected':len(selected-visible),'query_ms':round((perf_counter()-t)*1000,3),'snapshot_id':lib.snapshot_id})
        elif method=='POST' and p=='/api/confirm':
            raise RequestError(403,'需要原桌面真人授权、冻结范围和现场复核；此页不会写确认回执')
        elif method=='POST' and p=='/api/member-share-import' and not u.query:
            if (self.headers.get_all('Origin')!=['http://'+self.server.authority] or self.headers.get_content_type()!='application/json'
                    or self.headers.get('Sec-Fetch-Site')!='same-origin' or self.headers.get('Transfer-Encoding')):
                raise RequestError(403,'请从本机的链接导入页提交')
            try:
                length = int(self.headers.get('Content-Length','0'))
            except ValueError:
                raise RequestError(400,'导入文件长度无效') from None
            if len(self.headers.get_all('Content-Length',[]))!=1 or not 1<=length<=MAX_BROWSER_IMPORT_BYTES:
                raise RequestError(413,'批次为空或超过大小上限，请分批导入')
            payload = self.rfile.read(length)
            with self.server.snapshot.lock:
                library,_,_,pending = self.server.snapshot.get()
                if pending:
                    raise RequestError(409,'资料目录正在更新，请稍后导入')
                articles = {ref['article_id']:ref for row in library.by_id.values() for ref in row['sources']
                            if ref['site_id']=='wangzjk.com'}
                try:
                    result = import_browser_share_locations(payload, articles)
                except ValueError:
                    raise RequestError(422,'有项目无法匹配到资源库，或百度网盘链接格式无效') from None
                return self.send(200,result)
        elif method=='POST' and p in ('/api/draft','/api/review','/api/analysis-priority') and not u.query:
            if (self.headers.get_all('Origin')!=['http://'+self.server.authority] or self.headers.get_content_type()!='application/json'
                    or self.headers.get('Sec-Fetch-Site','same-origin')!='same-origin' or self.headers.get('Transfer-Encoding')):raise RequestError(403,'只接受本站页面的本地草稿请求')
            try:n=int(self.headers.get('Content-Length','0'))
            except ValueError:raise RequestError(400,'请求长度无效') from None
            if len(self.headers.get_all('Content-Length',[]))!=1 or not 1<=n<=4096:raise RequestError(413,'草稿参数过长或为空')
            try:o=json.loads(self.rfile.read(n).decode('utf-8'))
            except (ValueError,UnicodeError):raise RequestError(400,'草稿数据不可解析') from None
            with self.server.snapshot.lock:
                lib,draft,local,pending=self.server.snapshot.get()
                if pending:raise RequestError(409,'公开快照正在重新校验，未写草稿')
                if p=='/api/analysis-priority':return self.send(200,analysis.mutate(o,lib,local))
                return self.send(200,draft.mutate(o) if p=='/api/draft' else draft.freeze(o))
        raise RequestError(404,'找不到此操作')
    def serve(self,method):
        try:self.route(method)
        except (RequestError,AnalysisPriorityError) as e:
            detail={'error':e.msg};
            if e.code==409:
                _,draft,_,_=self.server.snapshot.get();detail['draft']=draft.show(draft.read())
            self.send(e.code,detail)
        except (sqlite3.Error,ValueError,OSError,TypeError):self.send(503,{'error':'本地数据读写未完成，原草稿保留。请读取当前版本，不要重复提交。'})
    def do_GET(self):self.serve('GET')
    def do_POST(self):self.serve('POST')


def make_server(library,db_path,port,*,local=None,watch=None,enable_control=False):
    draft=Draft(db_path,library); state=local if local is not None else LocalProjection(library)
    http=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    http.snapshot=LiveSnapshot(library,draft,state,watch);http.authority='127.0.0.1:'+str(http.server_port)
    http.control = PipelineControl(http.server_port) if enable_control else None
    return http

if __name__=='__main__':
    p=argparse.ArgumentParser(description='Worldanalysis local resource library');p.add_argument('--port',type=int,default=8767)
    args=p.parse_args(); lib=open_index()
    with make_server(lib,ROOT/'data'/'draft.sqlite3',args.port,watch=(ROOT/'data'/'discovery-head.json',ROOT/'data'/'codex-public.json'),enable_control=True) as h:
        print('Worldanalysis: http://'+h.authority+'/',flush=True)
        try:h.serve_forever()
        except KeyboardInterrupt:pass
