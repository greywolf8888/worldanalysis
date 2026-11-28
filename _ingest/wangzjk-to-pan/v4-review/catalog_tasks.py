"""Read the local catalog and run resumable text-only Agent batches."""
import argparse
from collections import Counter
import sqlite3
import sys
from pathlib import Path

from catalog_model import ROOT, SEMANTIC_DB, SITE, catalog_input_stamp, open_index
from catalog_platforms import PLATFORMS
from catalog_relations import candidate_pairs, article_identity
from pipeline_control import RunStore
from catalog_semantics import CatalogStore, MAX_TASKS, atomic_jsonl, encode, parse_jsonl, relationship_input, usable
from catalog_topics import BUSINESS_CATEGORIES, CATEGORIES, NO_TOPIC, browse_topic_matches


def catalog_path(value):
    path=Path(value).absolute()
    workspace=ROOT.parents[2]
    if not path.resolve().is_relative_to(workspace) or path.resolve()!=path:
        raise ValueError('任务文件须位于项目内的普通目录')
    return path


def named_inputs(library, requested):
    lookup={}
    for row in library.semantic_inputs.values():
        aid=row['article_id']
        lookup.setdefault(aid,[]).append(row)
        lookup[f"{row['site_id']}:{aid}"]=[row]
    selected=[]
    for value in dict.fromkeys(requested):
        direct=library.semantic_inputs.get(value)
        matches=[direct] if direct else lookup.get(value,[])
        if len(matches)!=1:
            raise ValueError('请指定唯一的来源文章或站点与文章号')
        if matches[0] not in selected:
            selected.append(matches[0])
    return selected


def candidates(library, args):
    statuses=library.semantic_classified
    source_inputs=library.semantic_inputs
    explicit=named_inputs(library,args.article)
    classified=[row for row in source_inputs.values() if statuses.get(row['article_key'],('',None))[1]]
    eligible=(explicit if explicit else classified)
    eligible=sorted(eligible,key=lambda row:(row['source_modified'] or row['published_at'] or '',row['article_id']),reverse=True)
    start=getattr(args,'offset',0)
    target_keys={article_identity(row) for row in eligible[start:start+args.targets]}
    current=[relationship_input(row,statuses.get(key,('',None))[1]) for key,row in source_inputs.items()]
    if not target_keys:
        return [],0,{}
    pairs,truncated=candidate_pairs(current,keys=target_keys,limit=args.candidate_limit)
    indexed={article_identity(row):row for row in current}
    return pairs,truncated,indexed


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    commands=p.add_subparsers(dest='action',required=True)
    commands.add_parser('inventory')
    commands.add_parser('status')
    audit=commands.add_parser('audit-topics',help='核对资源库分类计数和抽样')
    audit.add_argument('--category', choices=[*BUSINESS_CATEGORIES, *CATEGORIES, NO_TOPIC])
    audit.add_argument('--sample',type=int,default=0)
    audit.add_argument('--queue-output',type=catalog_path,help='导出待补原站正文的文章号')
    export=commands.add_parser('export')
    export.add_argument('--kind',choices=['classification','relation'],default='classification')
    export.add_argument('--article',action='append',default=[])
    export.add_argument('--include-excerpt',action='store_true')
    export.add_argument('--limit',type=int,default=MAX_TASKS)
    export.add_argument('--targets',type=int,default=20)
    export.add_argument('--candidate-limit',type=int,default=30)
    export.add_argument('--offset',type=int,default=0)
    export.add_argument('--pair',action='append',default=[])
    export.add_argument('--reclaim',action='store_true')
    export.add_argument('--output',type=catalog_path,required=True)
    resume=commands.add_parser('resume')
    resume.add_argument('--limit',type=int,default=MAX_TASKS)
    resume.add_argument('--output',type=catalog_path,required=True)
    imp=commands.add_parser('import')
    imp.add_argument('input',type=catalog_path)
    correction=commands.add_parser('correct')
    correction.add_argument('--article',required=True)
    correction.add_argument('input',type=catalog_path)
    review=commands.add_parser('correct-relation')
    review.add_argument('--pair',required=True)
    review.add_argument('input',type=catalog_path)
    undo=commands.add_parser('undo')
    undo.add_argument('relation_id')
    return p


def record_short(action, summary, *, rejected=False):
    try:
        RunStore(SEMANTIC_DB).record_catalog('catalog_'+action,summary,failed=rejected)
    except (OSError,sqlite3.Error):
        print('目录操作已落盘，本次运行记录稍后可查。',file=sys.stderr)


def audit_topics(library, category, sample):
    if not 0 <= sample <= 50:
        raise ValueError('抽样数须在 0 至 50 之间')
    raw = tuple(library.semantic_rows.values())
    for row in raw:
        label, subtopics = CATEGORIES.get(row['category'], ('', ()))
        if (row['category'] and not label or label != row['category_label']
                or row['business_category'] not in ('', *BUSINESS_CATEGORIES)
                or row['business_category_label'] != BUSINESS_CATEGORIES.get(row['business_category'], '')
                or row['subcategory'] and row['subcategory'] not in subtopics
                or row['secondary_category'] and row['secondary_category'] not in CATEGORIES
                or any(PLATFORMS.get(p['id']) != p['label'] for p in row['platforms'])):
            raise ValueError(f"分类或平台字段无效：{row['site_id']} / {row['article_id']}")
    facets = library.meta['categories']
    platforms = library.meta['platforms']
    for option in facets:
        query = {'category': option['id']}
        if len(library.match(query, set())) != option['count']:
            raise ValueError('大类计数与实际筛选结果不一致')
        for subcategory in option['subcategories']:
            if len(library.match({**query, 'subcategory': subcategory['id']}, set())) != subcategory['count']:
                raise ValueError('细分计数与实际筛选结果不一致')
    for platform in platforms:
        if len(library.match({'platform': platform['id']}, set())) != platform['count']:
            raise ValueError('平台计数与实际筛选结果不一致')
    chosen = [row for row in raw if category is None or (category == NO_TOPIC and not row['business_category'])
              or browse_topic_matches(row, category, '')]
    chosen.sort(key=lambda row:(row['published_at'],row['article_id']))
    count = min(sample,len(chosen))
    picked = [chosen[position * (len(chosen)-1) // max(1,count-1)] for position in range(count)]
    return {'sources':len(raw),'projects':library.meta['totals']['projects'],
            'primary':dict(Counter(row['business_category'] or NO_TOPIC for row in raw)),
            'without_topic':sum(not row['business_category'] for row in raw),
            'introduction_coverage':dict(Counter(row['coverage'] for row in raw)),
            'categories':facets,'platforms':platforms,
            'sample':[{'site':row['site_id'],'article':row['article_id'],'title':row['title'],
                       'category':row['business_category_label'],'previous_category':row['category_label'],
                       'subcategory':row['subcategory']} for row in picked]}


def main():
    p=parser()
    try:
        args=p.parse_args()
        store=CatalogStore(SEMANTIC_DB)
        library=open_index()
        inputs=library.semantic_inputs
        def guard():
            if library.catalog_inputs_stamp!=catalog_input_stamp():
                raise ValueError('资料正文刚更新，请重新读取任务后继续')
        if args.action in ('inventory','status'):
            status=store.inventory(inputs)
            print(encode({**status,'presentation':library.relations.stats(),
                          'reused_classifications':len(library.reused_classifications),
                          'legacy_shared_articles':library.article_count-len(library.by_id)}))
        elif args.action=='audit-topics':
            report=audit_topics(library,args.category,args.sample)
            if args.queue_output:
                if args.category not in (None,NO_TOPIC):
                    raise ValueError('原站补采清单仅适用于缺少分类的资料')
                pending=[row for row in library.semantic_rows.values() if row['site_id']==SITE
                         and not row['category'] and row['coverage']!='complete']
                pending.sort(key=lambda row:(len(row['excerpt']),row['article_id']))
                atomic_jsonl(args.queue_output,(int(row['article_id']) for row in pending))
                report['queued_for_public_read']=len(pending)
            print(encode(report))
        elif args.action=='export':
            if args.kind=='classification':
                explicit=named_inputs(library,args.article)
                available = (row for row in inputs.values() if row['article_key'] not in library.reused_classifications)
                chosen=sorted(explicit or available,
                    key=lambda r:(r['source_modified'] or r['published_at'] or '',r['article_id']),reverse=True)
                items=[(row,) for row in chosen if usable(row,args.include_excerpt)]
                truncated=0
                candidate_count=None
            else:
                if not 1<=args.targets<=120 or args.offset<0:
                    raise ValueError('候选目标数须为 1 至 120，位置不得为负')
                if args.pair:
                    items=[]
                    for pair in args.pair:
                        selected=named_inputs(library,pair.split(','))
                        if len(selected)!=2:
                            raise ValueError('每组关系须指定两个不同来源')
                        rows=sorted(selected,key=article_identity)
                        items.append(tuple(relationship_input(row,library.semantic_classified.get(row['article_key'],('',None))[1])
                                           for row in rows))
                    truncated=0
                else:
                    pairs,truncated,indexed=candidates(library,args)
                    items=[tuple(indexed[article_identity(pair[side])] for side in ('left','right')) for pair in pairs]
                candidate_count=len(items)
            tasks=store.export(items,args.kind,args.limit,args.output,reclaim=args.reclaim,guard=guard)
            record_short('export',f'已导出 {len(tasks)} 项待处理资料。')
            print(encode({'output':str(args.output),'mode':'host_agent','kind':args.kind,'tasks':len(tasks),
                          'candidate_pairs':candidate_count,'candidate_truncated':truncated}))
        elif args.action=='resume':
            tasks=store.resume(args.output,args.limit,inputs)
            record_short('resume',f'已恢复 {len(tasks)} 项未完成资料。')
            print(encode({'output':str(args.output),'tasks':len(tasks)}))
        elif args.action=='import':
            result=store.import_results(parse_jsonl(args.input),inputs,guard)
            record_short('import',f"已保存 {len(result['accepted'])} 项，已有 {len(result['duplicate'])} 项，待重读 {len(result['rejected'])} 项。", rejected=bool(result['rejected']))
            print(encode(result))
            return 1 if result['rejected'] else 0
        elif args.action=='correct':
            source=named_inputs(library,[args.article])[0]
            result=parse_jsonl(args.input)
            if len(result)!=1:
                raise ValueError('人工修正文件须包含一项完整分类结果')
            print(encode({'article':args.article,'manual_revision':store.correct(source,result[0],guard)}))
        elif args.action=='correct-relation':
            sources=named_inputs(library,args.pair.split(','))
            rows=parse_jsonl(args.input)
            if len(sources)!=2 or len(rows)!=1:
                raise ValueError('人工关系修正须指定两个来源和一项判定')
            key=store.correct_relation(*sources,rows[0],guard)
            print(encode({'relation_id':key,'manual':True}))
        elif args.action=='undo':
            store.undo(args.relation_id)
            print(encode({'relation_id':args.relation_id,'revoked':True}))
    except (OSError,ValueError,TypeError) as error:
        p.exit(2,str(error)+'\n')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
