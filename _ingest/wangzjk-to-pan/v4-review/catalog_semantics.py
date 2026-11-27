"""Local text evidence, classification tasks and version decisions."""
from contextlib import closing, contextmanager
from collections import Counter
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from tempfile import NamedTemporaryFile
from uuid import uuid4

from catalog_topics import CATEGORIES, CATEGORY_ALIASES, NO_TOPIC

RULES = 'catalog-zh-1'
SCHEMA = 1
MAX_TASKS = 20
MAX_BYTES = 512 * 1024
MAX_TEXT = 160_000
PLATFORMS = {
    'xiaohongshu':'小红书', 'douyin':'抖音', 'wechat_articles':'公众号', 'wechat_video':'视频号',
    'wechat':'微信', 'kuaishou':'快手', 'bilibili':'B站', 'tiktok':'TikTok', 'youtube':'YouTube',
    'taobao':'淘宝', 'xianyu':'闲鱼', 'pinduoduo':'拼多多', 'jd':'京东', 'amazon':'亚马逊',
    'meituan':'美团', 'zhihu':'知乎', 'toutiao':'头条', 'shipinhao_shop':'微信小店',
    'taptap':'TapTap', 'fanqie':'番茄小说', 'qidian':'起点', 'general':'通用',
}
ROLES = {'use', 'acquire', 'receive', 'tool', 'delivery', 'mention', 'excluded'}
FILTER_ROLES = {'use', 'acquire', 'receive'}
RELATIONS = {'same_version','different_version','related','contains','uncertain'}
COVERAGE = {'complete','partial','excerpt','image_only','missing'}
IDENTITY = re.compile(r'[a-zA-Z0-9_.-]{1,128}\Z')
KEY = re.compile(r'ca_[0-9a-f]{64}\Z')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return sha256(encode(value).encode()).hexdigest()


def article_key(site_id, article_id):
    return 'ca_' + fingerprint([site_id, str(article_id)])


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def source_input(row, intro, raw_article):
    summary = raw_article.get('public_excerpt') or row.get('excerpt') or ''
    coverage = intro['coverage'] if intro else ('excerpt' if summary else 'missing')
    paragraphs = (intro.get('clean_text') or '').splitlines() if intro else []
    source = {
        'site_id': row['site_id'], 'article_id': str(row['article_id']), 'article_key': article_key(row['site_id'], row['article_id']),
        'title': row['title'], 'paragraphs': [p for p in paragraphs if p.strip()], 'excerpt': summary,
        'coverage': coverage, 'published_at': row['published_at'],
        'publisher_published': raw_article.get('publisher_published'),
        'source_modified': raw_article.get('publisher_modified') or (intro or {}).get('publisher_modified'),
        'source_kind': (intro or {}).get('source_kind', 'listing'),
        'first_seen_at': raw_article.get('first_seen_at'),
    }
    source['input_hash'] = fingerprint([RULES, SCHEMA, {k:v for k,v in source.items() if k not in {'source_modified','first_seen_at'}}])
    return source


def usable(item, include_excerpt=False):
    text = ''.join(item['paragraphs'])
    if item['coverage'] in ('complete','partial') and len(text.strip()) >= 60:
        return len(text.encode()) <= MAX_TEXT
    return include_excerpt and len(item['excerpt'].strip()) >= 48


def relationship_input(item, analysis=None):
    analysis=analysis or {}
    key=fingerprint(['relationship',RULES,item['input_hash'],analysis])
    return {**item,'analysis':analysis,'input_hash':key}


def binding(item):
    return {k:item[k] for k in ('site_id','article_id','input_hash')}


def pair_key(left, right):
    return 'cr_' + fingerprint(sorted([left, right]))


def as_text(value, maximum=400):
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and c not in '\r\n\t' for c in value):
        raise ValueError('文本字段无效')
    return value.strip()


def evidence(value, sources):
    if not isinstance(value,list) or not 1 <= len(value) <= 24:
        raise ValueError('缺少原文依据')
    seen = set()
    checked = []
    for row in value:
        if not isinstance(row,dict) or set(row) - {'side','field','text'}:
            raise ValueError('依据字段无效')
        side = row.get('side','article')
        field = row.get('field')
        source = sources.get(side)
        snippet = as_text(row.get('text'),400)
        if not source or field not in ('title','paragraphs','excerpt') or len(snippet)<2:
            raise ValueError('依据来源无效')
        candidates = source['paragraphs'] if field == 'paragraphs' else [source[field]]
        if not any(snippet in candidate for candidate in candidates):
            raise ValueError('依据未在对应原文找到')
        if (side,snippet) not in seen:
            checked.append({'side':side,'field':field,'text':snippet})
            seen.add((side,snippet))
    return checked


def validate_classification(value, source):
    names = {'category','secondary_category','subcategory','platforms','goal','author','series','edition','dates','scope','evidence'}
    if not isinstance(value,dict) or set(value)-names:
        raise ValueError('分类结果字段无效')
    category = value.get('category')
    secondary = value.get('secondary_category') or ''
    field = value.get('subcategory') or ''
    if category not in (*CATEGORIES,NO_TOPIC) or (secondary and (secondary not in CATEGORIES or secondary in {category,'unsorted'})):
        raise ValueError('业务分类无效')
    if field and field not in CATEGORIES.get(category,('',()))[1]:
        raise ValueError('细分分类无效')
    checked = evidence(value.get('evidence'), {'article':source})
    platforms = value.get('platforms', [])
    if not isinstance(platforms,list) or len(platforms)>18:
        raise ValueError('平台结果无效')
    seen = set()
    for p in platforms:
        if not isinstance(p,dict) or set(p) != {'id','role','text'} or p['id'] not in PLATFORMS or p['role'] not in ROLES:
            raise ValueError('平台或用途无效')
        if p['id'] == 'general' and p['role'] != 'use':
            raise ValueError('通用平台只支持明确跨平台方法')
        if not any(row['text'] == p['text'] for row in checked) or (p['id'],p['role']) in seen:
            raise ValueError('平台缺少独立原文依据或重复')
        seen.add((p['id'],p['role']))
    scope = value.get('scope') or 'unknown'
    if scope not in ('complete','sample','bundle','service','unknown'):
        raise ValueError('课程范围无效')
    dates = value.get('dates', [])
    if not isinstance(dates,list) or len(dates)>8:
        raise ValueError('日期项无效')
    for d in dates:
        if not isinstance(d,dict) or set(d) != {'text','meaning','precision','value'}:
            raise ValueError('日期字段无效')
        num = {'year':4,'month':7,'day':10}.get(d['precision'])
        if not num or d['meaning'] not in ('version','material','example','history','other') or not isinstance(d['value'],str) or len(d['value'])!=num:
            raise ValueError('日期精度无效')
        if not any(d['text'] and d['text'] in row['text'] for row in checked):
            raise ValueError('日期缺少原文依据')
        normal = d['value'] + {4:'-01-01',7:'-01',10:''}[num]
        if date.fromisoformat(normal).isoformat()!=normal:
            raise ValueError('日期值无效')
    identity = {k:as_text(value.get(k),160 if k != 'goal' else 240) for k in ('goal','author','series','edition')}
    for k in ('author','edition'):
        if identity[k] and not any(identity[k] in row['text'] for row in checked):
            raise ValueError('作者或版次缺少原文依据')
    return {'category':category,'subcategory':field,'secondary_category':secondary,'platforms':platforms,
            **identity,'dates':dates,'scope':scope,'evidence':checked}


def validate_relation(value,left,right):
    if not isinstance(value,dict) or set(value)-{'relation','same_scope','newer','evidence','reason'}:
        raise ValueError('关系结果字段无效')
    label = value.get('relation')
    scope = value.get('same_scope',False)
    newer = value.get('newer')
    if label not in RELATIONS or type(scope) is not bool or newer not in (None,'left','right'):
        raise ValueError('关系或版本顺序无效')
    if (newer and (label!='different_version' or not scope)) or (label=='same_version' and not scope):
        raise ValueError('关系与版本范围有冲突')
    checked = evidence(value.get('evidence'), {'left':left,'right':right})
    if {row['side'] for row in checked} != {'left','right'}:
        raise ValueError('缺少双方原文依据')
    if label=='same_version' and all(row['field']!='paragraphs' for row in checked):
        raise ValueError('同版至少需要一方可辨识的正文依据')
    reason = as_text(value.get('reason'),400)
    if not reason:
        raise ValueError('缺少关系判定理由')
    return {'relation':label,'same_scope':scope,'newer':newer,'evidence':checked,'reason':reason}


DDL = '''
CREATE TABLE IF NOT EXISTS catalog_semantic_meta(single INTEGER PRIMARY KEY CHECK(single=1), revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS catalog_task (
    task_id TEXT PRIMARY KEY, kind TEXT NOT NULL, entity TEXT NOT NULL, generation INTEGER NOT NULL,
    signature TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, result_hash TEXT,
    UNIQUE(kind,entity,signature));
CREATE INDEX IF NOT EXISTS catalog_task_queue ON catalog_task(state,kind);
CREATE TABLE IF NOT EXISTS catalog_classification (
    article_key TEXT NOT NULL, input_hash TEXT NOT NULL, manual_revision INTEGER NOT NULL,
    source TEXT NOT NULL, result TEXT NOT NULL, recorded_at TEXT NOT NULL,
    PRIMARY KEY(article_key, input_hash, source, manual_revision));
CREATE TABLE IF NOT EXISTS catalog_override (
    entity TEXT PRIMARY KEY, generation INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS catalog_relation (
    event INTEGER PRIMARY KEY, relation_id TEXT NOT NULL, left_key TEXT NOT NULL, right_key TEXT NOT NULL,
    left_hash TEXT NOT NULL, right_hash TEXT NOT NULL, source TEXT NOT NULL, result TEXT NOT NULL,
    recorded_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS catalog_relation_history ON catalog_relation(relation_id,event);
'''


class CatalogStore:
    def __init__(self,path):
        self.path=Path(path).absolute()

    @contextmanager
    def connect(self, write=False):
        if self.path.resolve() != self.path or self.path.is_symlink():
            raise ValueError('目录数据文件路径无效')
        if not write and not self.path.is_file():
            yield None
            return
        address = str(self.path) if write else self.path.as_uri()+'?mode=ro'
        with closing(sqlite3.connect(address, uri=not write, timeout=5)) as db, db:
            db.row_factory=sqlite3.Row
            if write:
                db.executescript(DDL)
                db.execute('INSERT OR IGNORE INTO catalog_semantic_meta VALUES(1,0)')
                db.commit()
            yield db

    @staticmethod
    def has(db, table):
        return db is not None and db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',('table',table)).fetchone() is not None

    def revision(self):
        with self.connect() as db:
            if not self.has(db,'catalog_semantic_meta'):
                return 0
            return db.execute('SELECT revision FROM catalog_semantic_meta WHERE single=1').fetchone()[0]

    @staticmethod
    def increment(db):
        db.execute('UPDATE catalog_semantic_meta SET revision=revision+1 WHERE single=1')

    @staticmethod
    def overrides(db):
        return dict(db.execute('SELECT entity,generation FROM catalog_override'))

    @staticmethod
    def current_result(db, item):
        rows=db.execute("SELECT input_hash,source,result FROM catalog_classification WHERE article_key=? "
                        "ORDER BY manual_revision DESC,rowid DESC",(item['article_key'],)).fetchall()
        manual=next((row for row in rows if row['source']=='manual'),None)
        if manual is not None:
            return ('manual',json.loads(manual['result'])) if manual['input_hash']==item['input_hash'] else ('manual_pending',None)
        for row in rows:
            if row['input_hash']==item['input_hash']:
                return ('agent',json.loads(row['result']))
        return ('pending',None)

    def read(self,inputs):
        results, relation_rows, stale = {}, [], 0
        with self.connect() as db:
            if not self.has(db,'catalog_semantic_meta'):
                return results, relation_rows, stale
            db.execute('BEGIN')
            for row in db.execute('SELECT * FROM catalog_classification ORDER BY manual_revision DESC,rowid DESC'):
                key=row['article_key']
                item=inputs.get(key)
                if not item or key in results:
                    continue
                if row['source']=='manual':
                    results[key]=('manual',json.loads(row['result'])) if row['input_hash']==item['input_hash'] else ('manual_pending',None)
                elif row['input_hash']==item['input_hash']:
                    results[key]=('agent',json.loads(row['result']))
            for row in db.execute('''SELECT r.* FROM catalog_relation r JOIN
                    (SELECT relation_id,max(event) AS event,min(event) AS first_event FROM catalog_relation GROUP BY relation_id) z
                    USING(relation_id,event) ORDER BY z.first_event'''):
                if row['source']=='undo':
                    continue
                left,right = inputs.get(row['left_key']),inputs.get(row['right_key'])
                if not left or not right:
                    stale += 1
                    continue
                left=relationship_input(left,results.get(left['article_key'],('',None))[1])
                right=relationship_input(right,results.get(right['article_key'],('',None))[1])
                if row['left_hash']!=left['input_hash'] or row['right_hash']!=right['input_hash']:
                    stale += 1
                    continue
                relation_rows.append({'relation_id':row['relation_id'],'left':binding(left),'right':binding(right),
                                      **json.loads(row['result'])})
        return results,relation_rows,stale

    def inventory(self,inputs):
        results,relations,stale=self.read(inputs)
        summary=Counter(x['coverage'] for x in inputs.values())
        sources=Counter(x['site_id'] for x in inputs.values())
        with self.connect() as db:
            tasks=dict(db.execute('SELECT state,count(*) FROM catalog_task GROUP BY state')) if self.has(db,'catalog_task') else {}
            histories=db.execute('SELECT count(*) FROM catalog_relation').fetchone()[0] if self.has(db,'catalog_relation') else 0
        confirmed=sum(value is not None for _,value in results.values())
        return {'articles':len(inputs),'sources':dict(sources),'coverage':{k:summary[k] for k in sorted(COVERAGE)},
                'processed':confirmed,'unprocessed':len(inputs)-confirmed,'manual_pending':sum(s=='manual_pending' for s,_ in results.values()),
                'needs_intro':sum(not usable(x) for x in inputs.values()),'legacy_or_saved_body':summary['complete']+summary['partial'],
                'current_relationships':dict(Counter(r['relation'] for r in relations)), 'stale_relationships':stale,
                'relationship_history':histories,'tasks':tasks,'revision':self.revision()}

    def export(self,items,kind,limit,output,*,reclaim=False,guard=None):
        if kind not in ('classification','relation') or type(limit) is not int or not 1<=limit<=MAX_TASKS:
            raise ValueError('每批只能领取 1 至 20 项')
        document = []
        size = 0
        with self.connect(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            if guard:
                guard()
            overrides=self.overrides(db)
            existing={}
            for row in db.execute('SELECT * FROM catalog_task WHERE kind=?',(kind,)):
                existing[(row['entity'],row['signature'])]=row
            for parts in items:
                if len(document)>=limit:
                    break
                keys=[x['article_key'] for x in parts]
                entity=keys[0] if kind=='classification' else pair_key(*keys)
                if entity in overrides:
                    continue
                signature=fingerprint([kind, RULES, [x['input_hash'] for x in parts],overrides.get(entity,0)])
                old=existing.get((entity,signature))
                if old and (old['state']=='done' or (old['state']=='waiting' and not reclaim)):
                    continue
                task={
                    'schema':SCHEMA,'rules':RULES,'kind':kind,'task_id':old['task_id'] if old else uuid4().hex,
                    'generation':old['generation']+1 if old else 1,'manual_revision':overrides.get(entity,0),
                    'inputs':list(parts),
                }
                raw=encode(task)
                weight=len(raw.encode())+1
                if weight>MAX_BYTES or size+weight>MAX_BYTES:
                    continue
                size+=weight
                document.append(task)
                db.execute('''INSERT INTO catalog_task VALUES (?,?,?,?,?,?,?,NULL)
                    ON CONFLICT(task_id) DO UPDATE SET generation=excluded.generation,payload=excluded.payload,
                    state=excluded.state,result_hash=NULL''',
                    (task['task_id'],kind,entity,task['generation'],signature,raw,'waiting'))
        atomic_jsonl(output,document)
        return document

    def resume(self,output,limit=MAX_TASKS, inputs=None):
        if type(limit) is not int or not 1<=limit<=MAX_TASKS:
            raise ValueError('每批只能恢复 1 至 20 项')
        rows=[]; size=0
        with self.connect() as db:
            if self.has(db,'catalog_task'):
                for row in db.execute("SELECT payload FROM catalog_task WHERE state='waiting' ORDER BY rowid"):
                    task=json.loads(row['payload'])
                    if inputs is not None:
                        current=[inputs.get(x['article_key']) for x in task['inputs']]
                        if task['kind']=='relation':
                            current=[relationship_input(x,self.current_result(db,x)[1]) if x else None for x in current]
                        if any(not item or item['input_hash']!=old['input_hash'] for old,item in zip(task['inputs'],current)):
                            continue
                    weight=len(row['payload'].encode())+1
                    if len(rows)==limit or size+weight>MAX_BYTES:
                        break
                    rows.append(task); size+=weight
        atomic_jsonl(output,rows)
        return rows

    def import_results(self,document,inputs,guard=None):
        accepted=[]; duplicate=[]; rejected=[]
        with self.connect(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            if guard:
                guard()
            overrides=self.overrides(db)
            for position,row in enumerate(document,1):
                key=row.get('task_id') if isinstance(row,dict) else None
                try:
                    if type(key) is not str or not re.fullmatch(r'[0-9a-f]{32}',key):
                        raise ValueError('任务标识无效')
                    if set(row)!={'task_id','generation','schema','rules','result'} or row['schema']!=SCHEMA or row['rules']!=RULES:
                        raise ValueError('任务格式或规则版本已变化')
                    task=db.execute('SELECT * FROM catalog_task WHERE task_id=?',(key,)).fetchone()
                    if not task or type(row['generation']) is not int or row['generation']!=task['generation']:
                        raise ValueError('领取已失效，请恢复当前任务')
                    payload=json.loads(task['payload'])
                    if payload['manual_revision']!=overrides.get(task['entity'],0):
                        raise ValueError('已有人工修正')
                    current=[inputs.get(x['article_key']) for x in payload['inputs']]
                    if task['kind']=='relation':
                        current=[relationship_input(x,self.current_result(db,x)[1]) if x else None for x in current]
                    if any(not now or now['input_hash']!=old['input_hash'] for old,now in zip(payload['inputs'],current)):
                        raise ValueError('文章原文已变化')
                    result = validate_classification(row['result'],current[0]) if task['kind']=='classification' else validate_relation(row['result'],*current)
                    result_hash = fingerprint(result)
                    if task['state']=='done':
                        if result_hash!=task['result_hash']:
                            raise ValueError('该任务已完成，变更请用人工修正')
                        duplicate.append(key)
                        continue
                    if task['kind']=='classification':
                        db.execute('INSERT INTO catalog_classification VALUES (?,?,?,?,?,?)',
                            (task['entity'],current[0]['input_hash'],payload['manual_revision'],'agent',encode(result),stamp()))
                    else:
                        db.execute('''INSERT INTO catalog_relation
                            (relation_id,left_key,right_key,left_hash,right_hash,source,result,recorded_at) VALUES(?,?,?,?,?,?,?,?)''',
                            (task['entity'],*(x['article_key'] for x in current),*(x['input_hash'] for x in current),'agent',encode(result),stamp()))
                    db.execute("UPDATE catalog_task SET state='done',result_hash=? WHERE task_id=?",(result_hash,key))
                    accepted.append(key)
                except (ValueError,TypeError,KeyError,AttributeError) as error:
                    rejected.append({'line':position,'task_id':key,'reason':str(error)})
            if accepted:
                self.increment(db)
        return {'accepted':accepted,'duplicate':duplicate,'rejected':rejected}

    def correct(self, source, result, guard=None):
        result=validate_classification(result, source)
        key=source['article_key']
        with self.connect(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            if guard:
                guard()
            generation=self.overrides(db).get(key,0)+1
            db.execute('INSERT OR REPLACE INTO catalog_override VALUES(?,?)',(key,generation))
            db.execute('INSERT INTO catalog_classification VALUES (?,?,?,?,?,?)',
                       (key,source['input_hash'],generation,'manual',encode(result),stamp()))
            self.increment(db)
        return generation

    def correct_relation(self, left, right, result, guard=None):
        keys=(left['article_key'],right['article_key'])
        if keys[0]==keys[1]:
            raise ValueError('关系必须绑定两个不同来源')
        result=validate_relation(result,left,right)
        key=pair_key(*keys)
        with self.connect(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            if guard:
                guard()
            ordered=[relationship_input(item,self.current_result(db,item)[1]) for item in (left,right)]
            generation=self.overrides(db).get(key,0)+1
            db.execute('INSERT OR REPLACE INTO catalog_override VALUES(?,?)',(key,generation))
            db.execute('''INSERT INTO catalog_relation
                (relation_id,left_key,right_key,left_hash,right_hash,source,result,recorded_at) VALUES(?,?,?,?,?,?,?,?)''',
                (key,*keys,*(x['input_hash'] for x in ordered),'manual',encode(result),stamp()))
            self.increment(db)
        return key

    def undo(self, relation_id):
        with self.connect(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM catalog_relation WHERE relation_id=? ORDER BY event DESC LIMIT 1',(relation_id,)).fetchone()
            if not row or row['source']=='undo':
                raise ValueError('找不到可撤销的关系')
            generation=self.overrides(db).get(relation_id,0)+1
            db.execute('INSERT OR REPLACE INTO catalog_override VALUES(?,?)',(relation_id,generation))
            db.execute('''INSERT INTO catalog_relation
                (relation_id,left_key,right_key,left_hash,right_hash,source,result,recorded_at) VALUES(?,?,?,?,?,?,?,?)''',
                (relation_id,*(row[k] for k in ('left_key','right_key','left_hash','right_hash')),'undo',row['result'],stamp()))
            self.increment(db)


def atomic_jsonl(path,rows):
    path=Path(path).absolute()
    if path.resolve()!=path or path.is_symlink():
        raise ValueError('导出文件路径无效')
    path.parent.mkdir(parents=True,exist_ok=True)
    scratch=None
    try:
        with NamedTemporaryFile('w',encoding='utf-8',newline='\n',delete=False,dir=path.parent) as stream:
            scratch=Path(stream.name)
            for row in rows:
                stream.write(encode(row)+'\n')
        scratch.replace(path)
    finally:
        if scratch:
            scratch.unlink(missing_ok=True)


def parse_jsonl(path):
    path=Path(path).absolute()
    if path.resolve(strict=True)!=path or not path.is_file():
        raise ValueError('导入文件路径无效')
    with path.open('rb') as stream:
        raw=stream.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES:
        raise ValueError('导入超出大小上限')
    rows=[]
    for line in raw.decode('utf-8-sig').splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except (ValueError,RecursionError):
                rows.append(None)
    if len(rows)>MAX_TASKS:
        raise ValueError('一批最多导入 20 条')
    return rows
