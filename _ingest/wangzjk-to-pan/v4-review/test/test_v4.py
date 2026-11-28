"""Integration tests: fixture-only writes; real catalogue tests are GET/read-only."""
from copy import deepcopy
import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.parse import urlencode
from catalog_model import CATEGORIES, Library, Wangzjk, digest, exact_root_fact, safe_image
from project_detail import blocks
from server import Draft, make_server
from v4_exchange import observation, review_execution_admission, selection_sha256, validate_confirmed_shape, validate_manifest

S='https://pan.baidu.com/s/SYNTHETIC_V4'
BASE='https://www.wangzjk.com/'


def resource(n,share=S,title='样本标题'):
    return {'article_id':str(n),'url':BASE+str(n)+'.html','published_at':'2026-11-01','title':title,'shares':[{'url':share}], 'categories':[],'access_state':'public'}


def op(key, ids, root='src1', target='dst1', state='saved_root_verified',share=S):
    r={'source_id':root,'name':'模拟目录','is_dir':True,'size':0}
    return {'operation_id':key,'article_ids':ids,'share_url':share,'state':state,'receipt_success':True,'verification_scope':'selected_roots_only','roots':[r],'verified_rows':[dict(r,target_id=target)]}


def adapter(cards,records,markers=None):
    return Wangzjk({'catalog.json':{'articles':{r['article_id']:r for r in cards}},'journal.json':{'operations':{o['operation_id']:o for o in records}},
                    'downloaded-project-markers.json':{'entries':markers or []},'capacity-policy.json':{'minimum_free_bytes':100000000000},'crawl_state.JSON':{}},{})


class Demo:
    site_id='demo-secondary';site_label='隔离第二源（测试）'
    def discover(self,cursor,scope):return ([str(i) for i in range(1,33)],None) if cursor==0 else ([],None)
    def read_article(self,article_id):
        n=int(article_id); key=digest(['demo',article_id]); title=('滤镜' if n%2==0 else '教程')+' DEMO '+article_id
        r={'id':'rv_'+key,'article_id':article_id,'article_revision':'arv_'+key,'project_id':'p_'+key,'source_id':None,'source_revision':None,
           'site_id':self.site_id,'site_label':self.site_label,'title':title,'category':'tools','category_label':CATEGORIES['tools'],
           'published_at':'2026-11-01','url':None,'excerpt':'隔离合成数据','cover':None,'detail_state':'demo','status':'observed','status_label':'演示不可执行','selectable':n!=17,
           'blocked_reason':'demo=true · execution_allowed=false','scope':'unknown','keywords':[],'source_categories':[], 'blocks':[{'kind':'text','text':'仅正文检索标记'}],
           'sources':[{'site_id':self.site_id,'article_id':article_id,'article_revision':'arv_'+key,'title':title}], 'roots':[],'operations':[],'observed_at':None,'demo':True,'execution_allowed':False}
        return r
    def resolve_resource_ranges(self,article_id):return []
    def checkpoint(self):return {'demo':True,'execution_allowed':False,'site_id':self.site_id}


class IdentityAndProjection(unittest.TestCase):
    def test_real_roots_not_recursive_and_returned_target_must_differ(self):
        a=op('op1',['1']);r=exact_root_fact(a);self.assertIsNone(r['roots'][0]['bytes']);self.assertEqual(r['scope'],'selected_roots_only');a['verified_rows'][0]['target_id']='src1';self.assertIsNone(exact_root_fact(a))
    def test_same_link_and_name_without_range_is_not_equivalence(self):
        lib=Library([adapter([resource(1),resource(2)],[])]);self.assertEqual(len(lib.by_id),2);self.assertEqual([x['weak_candidate_count'] for x in lib.by_id.values()],[1,1]);self.assertTrue(all(x['source_id'] is None for x in lib.by_id.values()))
    def test_two_confirmed_aliases_same_root_proof_merge(self):
        lib=Library([adapter([resource(1),resource(2)],[op('op1',['1','2'])])]);self.assertEqual(len(lib.by_id),1);r=next(iter(lib.by_id.values()));self.assertEqual(len(r['sources']),2);self.assertFalse(r['selectable']);self.assertEqual(r['status'],'verified')
    def test_different_roots_preserve_versions_even_with_same_share(self):
        lib=Library([adapter([resource(1),resource(2)],[op('op1',['1']),op('op2',['2'],'src2','dst2')])]);self.assertEqual(len(lib.by_id),2)
    def test_partial_overlap_and_uncertain_claim_remain_held(self):
        a=op('op1',['1']); b=op('op2',['2'],'s2','t2','needs_reconcile');lib=Library([adapter([resource(1),resource(2)],[a,b])]);self.assertEqual(len(lib.by_id),2);self.assertTrue(all(not r['selectable'] for r in lib.by_id.values()));self.assertIn('needs_reconcile',{r['status'] for r in lib.by_id.values()})
    def test_only_exact_marker_join_is_recycled(self):
        m={'article_id':'1','cloud_root_id':'dst1','cloud_cleanup_state':'recycled_verified_exact_id','download_scope_complete':True};a=op('op1',['1']);r=adapter([resource(1)],[a],[m]).read_article('1');self.assertEqual(r['status'],'verified'); self.assertFalse(r['selectable']) # malformed marker cannot override genuine historic readback
        m['cloud_root_id']='111';a['verified_rows'][0]['target_id']='111';r=adapter([resource(1)],[a],[m]).read_article('1');self.assertEqual(r['status'],'recycled');self.assertFalse(r['selectable'])
    def test_parse_untrusted_body_without_active_elements_or_credentials(self):
        image=BASE+'wp-content/uploads/public.jpeg';raw='<p>首段</p><img src="'+image+'" onerror="exec()"><img src="https://evil.invalid/a.png"><script>bad()</script><div hidden>不该出现</div><a href="https://pan.baidu.com/s/NO_LEAK">压缩密码 ABCD</a><p>访问码:abcd 尾段</p>'
        r=blocks(raw); self.assertEqual([x['kind'] for x in r],['text','image','text']);self.assertNotIn('abcd',json.dumps(r));self.assertNotIn('NO_LEAK',json.dumps(r));self.assertIsNone(safe_image(image+'?call=x'))
    def test_protected_body_stays_out(self):self.assertIsNone(blocks('<p>登录后查看全文</p><p>受限原文</p>'))
    def test_catalog_observation_keeps_body_search_but_omits_credentials(self):
        library = Library([Demo()])
        library.snapshot_id = 'synthetic-review'
        library.meta = {'totals': {'articles': 32}}
        library.provenance = {}
        snapshot = observation(library, 'v4-self-test')
        self.assertEqual(snapshot['totals']['articles'], 32)
        self.assertEqual(len(snapshot['items']), 32)
        self.assertFalse(snapshot['execution_allowed'])
        rendered = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn('https://pan.baidu.com/s/', rendered)
        self.assertNotIn('account_ref', rendered)
        target = next(iter(library.by_id))
        self.assertIn(target, {row['id'] for row in library.match({'q': '仅正文检索标记'}, set())})


class UserDraft(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(dir=Path(__file__).absolute().parent); self.addCleanup(self.tmp.cleanup); self.path=Path(self.tmp.name)/'TEST-DEMO.sqlite3'
        self.lib=Library([Demo()]);self.lib.snapshot_id='synthetic-review';self.lib.meta={'demo':True,'execution_allowed':False}
        self.srv=None;self.restart();self.addCleanup(self.stop)
    def stop(self):
        if self.srv is not None:self.srv.shutdown();self.srv.server_close();self.srv=None
    def restart(self):
        self.stop(); self.srv=make_server(self.lib,self.path,0);self.thread=Thread(target=self.srv.serve_forever,daemon=True);self.thread.start()
    def request(self,path,obj=None,origin=True):
        c=http.client.HTTPConnection(self.srv.authority,timeout=5)
        try:
            h={'Content-Type':'application/json'}
            if origin:h['Origin']='http://'+self.srv.authority
            c.request('GET' if obj is None else 'POST',path,None if obj is None else json.dumps(obj).encode(),h); r=c.getresponse(); data=json.loads(r.read());return r.status,data
        finally:c.close()
    def page(self,**a):
        s,d=self.request('/api/resources?'+urlencode(a));self.assertEqual(s,200);return d
    def post(self,rev,action,**a):return self.request('/api/draft',{'revision':rev,'action':action,**a})
    def test_cross_page_hidden_and_restart_order(self):
        a=self.page(page=1);b=self.page(page=2);u,v=a['items'][0]['id'],b['items'][0]['id'];self.assertNotEqual(u,v)
        self.assertEqual(self.post(0,'toggle',id=u,selected=True)[0],200);self.assertEqual(self.page(page=2)['hidden_selected'],1)
        self.assertEqual(self.post(1,'toggle',id=v,selected=True)[0],200);self.assertEqual(self.post(2,'move',id=v,position=1)[1]['ids'],[v,u]);
        self.assertEqual(self.page(q='无匹配')['hidden_selected'],2);self.assertEqual(self.post(3,'prefs',prefs={'page':2,'scroll':147,'order':'title'})[0],200)
        self.restart();d=self.request('/api/draft')[1];self.assertEqual(d['ids'],[v,u]);self.assertEqual(d['prefs']['scroll'],147);self.assertEqual(d['prefs']['page'],2);self.assertEqual(self.page(order='title')['draft']['ids'],[v,u]);
    def test_three_bulk_fences_exact_and_conflict(self):
        a=self.page(q='滤镜',page=2);self.assertEqual(a['eligible'],16);self.assertEqual(a['page_selectable'],4);self.assertEqual(a['index_selectable'],31)
        common={'snapshot_id':self.lib.snapshot_id,'filter':{'q':'滤镜','page':2}}
        self.assertEqual(self.post(0,'bulk',scope='page',expected_count=3,**common)[0],409);self.assertEqual(self.post(0,'bulk',scope='page',expected_count=4,**common)[1]['ids'].__len__(),4)
        d=self.post(1,'bulk',scope='filtered',expected_count=16,**common)[1];self.assertEqual(len(d['ids']),16)
        self.assertEqual(self.post(0,'clear')[0],409);self.assertEqual(len(self.request('/api/draft')[1]['ids']),16)
        self.assertEqual(len(self.post(2,'bulk',scope='site_all',expected_count=31,**common)[1]['ids']),31)
    def test_extended_catalog_page_uses_the_displayed_bulk_scope(self):
        default_grid = self.page()
        default_list = self.page(view='list')
        expanded = self.page(view='list', limit=20)
        following = self.page(view='list', limit=20, page=2)
        self.assertEqual((default_grid['limit'], default_list['limit']), (12, 16))
        self.assertEqual((expanded['limit'], len(expanded['items']), len(following['items'])), (20, 20, 12))
        first_ids = {row['id'] for row in expanded['items']}
        self.assertTrue(first_ids.isdisjoint(row['id'] for row in following['items']))
        args = {'scope':'page', 'snapshot_id':expanded['snapshot_id'], 'expected_count':expanded['page_selectable']}
        self.assertEqual(self.post(0, 'bulk', filter={'view':'list'}, **args)[0], 409)
        status, draft = self.post(0, 'bulk', filter={'view':'list', 'limit':20}, **args)
        self.assertEqual(status, 200)
        self.assertEqual(set(draft['ids']), {row['id'] for row in expanded['items'] if row.get('requestable', row['selectable'])})
        for invalid in ('0', '19', '99', '020'):
            with self.subTest(limit=invalid):
                self.assertEqual(self.request('/api/resources?limit=' + invalid)[0], 400)

    def test_freeze_is_bound_idempotent_and_inert(self):
        x=self.page()['items'][0]['id'];self.assertEqual(self.post(0,'toggle',id=x,selected=True)[0],200)
        obj={'revision':1,'order_mode':'strict'};s,f=self.request('/api/review',obj);self.assertEqual(s,200);self.assertFalse(f['execution_allowed']);self.assertIsNone(f['authorization_ref']);self.assertEqual(f['selection_sha256'],selection_sha256(f['items']));
        self.assertEqual(self.request('/api/review',obj)[1],f);self.assertFalse(review_execution_admission(f)['execution_allowed']);self.assertFalse(validate_confirmed_shape(f));
        self.assertEqual(self.post(1,'clear')[0],200);self.assertEqual(self.request('/api/review',obj)[0],409)
    def test_blocked_forgery_and_route_and_unknown_recursive(self):
        n=next(x for x in self.page(page=2)['items'] if not x['selectable']);self.assertEqual(self.post(0,'toggle',id=n['id'],selected=True)[0],422)
        self.assertEqual(self.request('/api/draft',{'revision':0,'action':'clear'},origin=False)[0],403);self.assertEqual(self.request('/api/execute',{'rank':1})[0],404)
        self.assertEqual(self.request('/api/resources?q=x&q=y')[0],400);self.assertFalse(validate_manifest({'schema_version':1,'record_type':'range_manifest','source_id':'demo','source_revision':'x','scope':'recursive','files':[]}))
    def test_selection_fingerprint_is_set_not_order(self):
        a=[{'queue_item_id':'q2','item_rank':2,'source_revision':'v2'},{'queue_item_id':'q1','item_rank':1,'source_revision':'v1'}]
        b=[{**a[1],'item_rank':2},{**a[0],'item_rank':1}];self.assertEqual(selection_sha256(a),selection_sha256(b));b[0]['source_revision']='v3';self.assertNotEqual(selection_sha256(a),selection_sha256(b))

if __name__=='__main__':unittest.main()
