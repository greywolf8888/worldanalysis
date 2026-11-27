"""Project an explicitly sampled public WordPress response without persisting raw HTML."""
from hashlib import sha256
import json
import re
import sys
from lxml import etree, html
from catalog_model import ORIGIN, safe_image, safe_text

GATE=re.compile(r'内容需要权限查看|登录后查看全文|请(?:付费|购买|登录)后查看|无权查看|解锁全文|访问过于频繁|安全验证后继续|输入提取码')
HIDE=re.compile(r'(?:paywall|pay-download|hidden-content|hide-content|access-restricted|login-required|vip-visible|download-box|post-pay|wp-block-embed|\b(?:hide|hidden|d-none)\b)', re.I)
SKIP={'script','style','noscript','iframe','form','input','button','video','audio','object','embed','svg','template','source','track','meta','link'}
FENCE={'p','li','ul','ol','div','section','figure','figcaption','br','blockquote','tr','h1','h2','h3','h4','h5','h6','pre','hr'}


def blocks(raw, *, block_limit=180, markup_limit=95_000):
    if not isinstance(raw,str) or len(raw)>markup_limit or GATE.search(raw):return None
    parser=html.HTMLParser(recover=True,no_network=True,remove_comments=True)
    root=html.fragment_fromstring(raw,create_parent='div',parser=parser); out=[]; text=[]
    def flush():
        if txt:=safe_text(''.join(text),16000):out.append({'kind':'text','text':txt})
        text.clear()
    def walk(el,depth):
        if depth>65 or len(out)>block_limit:raise ValueError('projection budget')
        if not isinstance(el.tag,str):return
        tag=el.tag.lower(); a=el.attrib
        if (tag in SKIP or 'hidden' in a or a.get('aria-hidden')=='true' or re.search(r'display\s*:\s*none|visibility\s*:\s*hidden',a.get('style',''),re.I)
            or HIDE.search(a.get('class','')+' '+a.get('id',''))):return
        if tag in FENCE:flush()
        if tag=='img':
            if u:=safe_image(a.get('data-src')) or safe_image(a.get('src')):
                flush();out.append({'kind':'image','url':u,'alt':safe_text(a.get('alt',''),400)})
            return
        if tag=='a' and re.search(r'pan\.baidu\.com|/pay-download/|/(?:login|wp-login)',a.get('href',''),re.I):return
        if el.text:text.append(el.text)
        for child in el:
            walk(child,depth+1)
            if child.tail:text.append(child.tail)
        if tag in FENCE:flush()
    walk(root,0);flush();return out


def project(x, *, block_limit=180, markup_limit=95_000):
    a=str(x['article_id']); v=x.get('payload')
    if x.get('http') in (401,403,429):return {'state':'restricted','observed_at':x['observed_at']}
    if x.get('http')!=200 or not isinstance(v,dict) or str(v.get('id'))!=a or v.get('link')!=ORIGIN+'/'+a+'.html' or v.get('status')!='publish':
        return {'state':'unavailable','observed_at':x['observed_at']}
    if not isinstance(v.get('content'),dict) or v['content'].get('protected') is not False or not isinstance(v.get('excerpt'),dict) or v['excerpt'].get('protected') is not False:
        return {'state':'restricted','observed_at':x['observed_at']}
    seq=blocks(v['content'].get('rendered'),block_limit=block_limit,markup_limit=markup_limit); exc=blocks(v['excerpt'].get('rendered'))
    if seq is None or exc is None:return {'state':'restricted','observed_at':x['observed_at']}
    if not seq:return {'state':'unavailable','observed_at':x['observed_at']}
    return {'state':'public_sample','observed_at':x['observed_at'],'article_url':v['link'],'published_at':v.get('date'),
            'publisher_modified':v.get('modified'),'public_api_url':x['source'],'public_api_sha256':x['body_sha256'],
            'excerpt':' '.join(y['text'] for y in exc if y['kind']=='text'),'blocks':seq,
            'cover_url':next((y['url'] for y in seq if y['kind']=='image'),None),
            'cover_basis':'first_real_body_image_not_assumed_featured_media'}

if __name__=='__main__':
    inp=json.load(sys.stdin); out={}; attempts=[]
    for x in inp:
        ident=x['article_id']; reason=None
        try:got=project(x)
        except (ValueError,etree.LxmlError):
            got={'state':'unavailable','observed_at':x['observed_at']};reason='public_markup_budget_or_parse_limit'
        out[ident]=got
        attempts.append({'article_id':ident,'state':got['state'],'reason':reason,'http':x.get('http'),'truncated':x['truncated'],'observed_at':x['observed_at'],'source':x['source']})
    print(json.dumps({'schema':'wangzjk-v4-public-article-projection.v1','site_id':'wangzjk.com','articles':out,'attempts':attempts},ensure_ascii=False))
