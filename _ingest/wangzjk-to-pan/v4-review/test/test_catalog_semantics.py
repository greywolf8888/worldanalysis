from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from catalog_semantics import CatalogStore, parse_jsonl, relationship_input, source_input


def item(aid='100', title='AI 写小说投稿', excerpt='应用 AI 辅助写作', text='完整教学介绍和课程目录，使用 AI 改善写作并投稿。'):
    row={'site_id':'example.cn','article_id':aid,'title':title,'excerpt':excerpt,'published_at':'2026-11-22'}
    intro={'clean_text':text,'coverage':'complete','source_kind':'author'}
    return source_input(row,intro,{'public_excerpt':excerpt})


def classification(source,category='content'):
    return {'category':category,'subcategory':'写作与小说' if category=='content' else 'AI 工具',
            'evidence':[{'field':'title','text':source['title']}]}


def answer(task,result):
    return {**{t:task[t] for t in ('schema','rules','task_id','generation')},'result':result}


class CatalogTaskTests(unittest.TestCase):
    def setUp(self):
        temp=TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.out=self.root/'tasks.jsonl'
        self.store=CatalogStore(self.root/'pipeline-runs.sqlite3')

    def task(self, *items, kind='classification', reclaim=False):
        return self.store.export([items],kind,1,self.out,reclaim=reclaim)[0]

    def test_resume_reclaim_and_late_result(self):
        source=item()
        first=self.task(source)
        self.assertEqual(self.store.resume(self.out),[first])
        self.assertEqual(parse_jsonl(self.out),[first])
        second=self.task(source,reclaim=True)
        self.assertEqual(first['task_id'],second['task_id'])
        self.assertGreater(second['generation'],first['generation'])
        submitted=[answer(first,classification(source)),answer(second,classification(source))]
        result=self.store.import_results(submitted,{source['article_key']:source})
        self.assertEqual(len(result['accepted']),1)
        self.assertEqual(len(result['rejected']),1)

    def test_excerpt_change_rejects_only_old_task(self):
        first, second = item(), item('200')
        older=self.task(first)
        another=self.task(second)
        current=item(excerpt='原摘要实际更新')
        self.assertNotEqual(first['input_hash'],current['input_hash'])
        third=item('300')
        result=self.store.import_results([answer(older,classification(first)),answer(another,classification(second))],
                  {a['article_key']:a for a in [current,second,third]})
        self.assertEqual(len(result['accepted']),1)
        self.assertEqual(len(result['rejected']),1)

    def test_same_result_does_not_advance_revision_and_manual_protection(self):
        source=item()
        inputs={source['article_key']:source}
        task=self.task(source)
        payload=answer(task,classification(source))
        self.assertEqual(len(self.store.import_results([payload],inputs)['accepted']),1)
        self.assertEqual(len(self.store.import_results([payload],inputs)['duplicate']),1)
        self.assertEqual(self.store.revision(),1)
        self.store.correct(source,classification(source,'automation'))
        self.assertEqual(self.store.revision(),2)
        self.assertEqual(len(self.store.import_results([payload],inputs)['rejected']),1)
        results=self.store.read(inputs)[0]
        self.assertEqual(results[source['article_key']][0],'manual')
        self.assertEqual(results[source['article_key']][1]['category'],'automation')
        changed=item(text='重新发布且改成了新的教学内容')
        self.assertEqual(self.store.read({changed['article_key']:changed})[0][changed['article_key']],
                         ('manual_pending',None))

    def test_pair_evidence_input_changes_invalidate_immediately(self):
        left=item()
        right=item('200')
        inputs={x['article_key']:x for x in (left,right)}
        task=self.task(relationship_input(left),relationship_input(right),kind='relation')
        result={'relation':'same_version','same_scope':True,'reason':'正文课程目标和独特教学文字相同',
                'evidence':[{'side':side,'field':'paragraphs','text':'使用 AI 改善写作并投稿'} for side in ('left','right')]}
        self.assertEqual(len(self.store.import_results([answer(task,result)],inputs)['accepted']),1)
        relations=self.store.read(inputs)[1]
        self.assertEqual(len(relations),1)
        self.store.correct(left,classification(left))
        self.assertEqual(self.store.read(inputs)[1:],([],1))
        self.store.undo(relations[0]['relation_id'])
        self.assertEqual(self.store.read(inputs)[1:],([],0))

    def test_evidence_cannot_use_wrong_field(self):
        source=item()
        task=self.task(source)
        result=classification(source)
        result['evidence'][0]['field']='excerpt'
        outcome=self.store.import_results([answer(task,result)],{source['article_key']:source})
        self.assertEqual(len(outcome['rejected']),1)
        self.assertEqual(self.store.revision(),0)
