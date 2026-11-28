from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace

from catalog_decisions import cross_author_playbooks
from catalog_relations import CatalogRelations
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
        temp=TemporaryDirectory(dir=Path(__file__).parent)
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

    def test_playbooks_require_current_related_evidence_and_distinct_public_attribution(self):
        title = 'AI 古建筑科普图文创作'
        left = item('501', title=title,
                    text='作者：陈兰\n第2版\n用 AI 绘制古建筑，逐图编写解说词。\n工具订阅每月20元。\n课程目录\n1. 绘制古建筑草图\n2. 逐图编写解说词')
        right = item('502', title=title,
                     text='主讲人：林江\n用 AI 绘制古建筑，按朝代组织选题。\n作品通过广告分成结算。\n宣传收益5000元。\n课程目录\n1. 绘制古建筑草图\n2. 按朝代组织选题')
        uncredited = item('503', title=title, text='用 AI 绘制古建筑，再制作科普配图。')
        same_author = item('504', title=title, text='作者：陈兰\n用 AI 绘制古建筑，给模型准备草图。')
        older = item('505', title=title + ' 第1版',
                     text='作者：陈兰\n第1版\n用 AI 绘制古建筑。\n课程目录\n1. 绘制古建筑草图\n2. 扫描手绘线稿')
        inputs = {row['article_key']: row for row in (left, right, uncredited, same_author, older)}
        current = {'analysis_state':'completed_received_scope', 'analysis_scope':'配套待补',
                   'textbook_href':'obsidian://open?path=G%3A%2Fverified.md',
                   'course_completion':{'received_analysis':'accepted_for_edition'}}
        local_name = {'analysis_state':'local_result_available', 'textbook_href':'obsidian://open?path=G%3A%2Fguess.md'}
        textbooks = {'501':current, '502':local_name}

        def project():
            classified, relations, _ = self.store.read(inputs, include_suppressed=True)
            rows = {key: {**relationship_input(row, classified.get(key, ('', None))[1]),
                          'id':f"rv_{row['article_id']}", 'site_label':'原站'}
                    for key, row in inputs.items()}
            model = CatalogRelations(rows.values(), relations)
            return cross_author_playbooks(SimpleNamespace(semantic_rows=rows, semantic_relations=relations,
                                                          relations=model),
                                          progress_for=lambda row: textbooks.get(row['article_id'], {}))

        relation = {'relation':'related', 'reason':'两篇原文都讲按图制作古建筑解说',
                    'evidence':[{'side':side, 'field':'paragraphs', 'text':'用 AI 绘制古建筑'}
                                for side in ('left', 'right')]}
        self.assertEqual(project(), [])
        self.store.correct_relation(left, uncredited, relation)
        self.store.correct_relation(left, same_author, relation)
        self.store.correct_relation(left, right, {**relation, 'relation':'contains'})
        self.assertEqual(project(), [])
        pair_key = self.store.correct_relation(left, right, relation)
        group, = project()
        pair = group['entries']
        self.assertEqual(group['theme'], title)
        self.assertEqual({row['article_key'] for row in pair}, {left['article_key'], right['article_key']})
        ours, other = [next(row for row in pair if row['author']==name) for name in ('陈兰', '林江')]
        self.assertEqual(ours['costs'], ['工具订阅每月20元。'])
        self.assertTrue(ours['formal']['received'])
        self.assertEqual(ours['recommendation']['action'], '先读教材，再核对补件')
        self.assertEqual(other['monetization'], ['作品通过广告分成结算。'])
        self.assertNotIn('formal', other)
        self.assertNotIn('textbook_href', other)
        self.assertEqual(other['recommendation']['action'], '可安排完整分析')
        comparison, = group['comparisons']
        self.assertTrue(all(quotes == ['用 AI 绘制古建筑'] for quotes in comparison['evidence'].values()))
        self.assertEqual(comparison['comparison']['shared_outline_count'], 1)
        for version, row in (('第2版', left), ('第1版', older)):
            self.store.correct(row, {**classification(row), 'edition':version, 'author':'陈兰',
                'evidence':[{'field':'paragraphs','text':text} for text in (version, '陈兰')]})
        pair_key = self.store.correct_relation(left, right, relation)
        self.store.correct_relation(left, older, {**relation, 'relation':'different_version',
                                                  'same_scope':True, 'newer':'left'})
        textbooks['505'] = textbooks.pop('501')
        group, = project()
        updated = next(row for row in group['entries'] if row['author']=='陈兰')
        history, = updated['lineage']
        self.assertEqual(history['relation'], 'older')
        self.assertEqual(history['comparison']['peer_only']['examples'], ['2. 扫描手绘线稿'])
        self.assertNotIn('formal', updated)
        self.assertEqual(updated['recommendation']['action'], '核对后增量分析')
        inputs[right['article_key']] = item('502', title=title, text='作者：林江\n新版采用自然场景拍摄。')
        self.assertEqual(project(), [])
        inputs[right['article_key']] = right
        self.store.undo(pair_key)
        self.assertEqual(project(), [])
