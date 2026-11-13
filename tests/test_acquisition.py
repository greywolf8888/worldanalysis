import sqlite3
import unittest
from unittest.mock import patch
from worldanalysis.acquisition import Acquisition
from worldanalysis.policy import Policy, GateError, ByteBudget, canonical_cloud_path


class ScopeTests(unittest.TestCase):
    def test_exact_decoded_cloud_scope(self):
        p = Policy()
        for path in ['/apps/教育分析学习', '/apps/教育分析学习/', '/apps/教育分析学习/课程/a.pdf']:
            self.assertTrue(p.require_cloud_path(path))
        for path in ['/apps/教育分析学习其他/a', '/apps/bdpan/a', '/wangzjk.com', '/apps/教育分析学习/../x', '/apps/教育分析学习//x', '/apps/教育分析学习/%2e%2e/x', '/apps\\教育分析学习', 'apps/教育分析学习']:
            with self.subTest(path=path), self.assertRaises(GateError):
                p.require_cloud_path(path)

    def test_zero_budget_and_generation_default_denied(self):
        with self.assertRaises(GateError): Policy().require_generation()
        with self.assertRaises(GateError): Policy().require_external(0, True)
        with self.assertRaises(GateError): Policy().require_site('https://wangzjk.com.evil.test/a')

    def test_cumulative_budget_includes_retries(self):
        budget = ByteBudget(10)
        budget.charge(6)
        with self.assertRaises(GateError): budget.charge(5)
        self.assertEqual(budget.received, 6)
        budget.charge(4)
        self.assertEqual(budget.received, 10)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.grant = {'state':'granted','operations':['transfer','inventory','read_stream'],'share_refs':['share-1'],'roots':['/apps/教育分析学习'],'expires_at':'2099-01-01T00:00:00Z'}
        self.a = Acquisition(self.db, grant_resolver=lambda _: self.grant)
        self.path = '/apps/教育分析学习/新课'
        self.manifest = [{'source_file_id': 'share-file-1', 'relative_path': '课件.txt', 'size_bytes': 20, 'is_directory': False}]
        self.op = self.a.prepare_transfer('project-1','share-1',self.path,self.manifest,grant_ref='user-plan',enumeration_complete=True)
        self.current = {'account_ref':'account-1', 'path':self.path+'/课件.txt','size_bytes':20,'is_directory':False}
        self.observed = [{'source_share_ref':'share-1','source_file_id':'share-file-1','destination_fs_id':'dest-file-99'}]

    def reconcile(self, rows=None):
        return self.a.reconcile(self.op['operation_id'], self.observed if rows is None else rows, resolve_asset=lambda fid:self.current,destination_account_ref='account-1',enumeration_complete=True)

    def test_model_string_cannot_grant_permission_and_expiry_revokes(self):
        denied=Acquisition(self.db)
        with self.assertRaises(GateError):
            denied.prepare_transfer('project-1','share-1',self.path,self.manifest,grant_ref='model-approved',enumeration_complete=True)
        self.grant['expires_at']='2000-01-01T00:00:00Z'
        with self.assertRaises(GateError):
            self.a.before_submit(self.op['operation_id'],resolve_destination=lambda p:p)

    def test_deduplicated_transfer_and_unknown_no_replay(self):
        again = self.a.prepare_transfer('project-1','share-1',self.path,self.manifest,grant_ref='user-plan',enumeration_complete=True)
        self.assertEqual(self.op['operation_id'],again['operation_id'])
        self.a.before_submit(self.op['operation_id'],resolve_destination=lambda p:p)
        self.assertEqual(self.a.operation(self.op['operation_id'])['state'],'submission_unknown')
        with self.assertRaises(GateError): self.a.before_submit(self.op['operation_id'],resolve_destination=lambda p:p)

    def test_submitted_not_complete_then_verified_destination_mapping(self):
        self.a.before_submit(self.op['operation_id'],resolve_destination=lambda p:p)
        receipt=self.a.record_submission(self.op['operation_id'],remote_task_ref='receipt-1')
        self.assertEqual(receipt['state'],'submitted')
        self.assertEqual(self.reconcile()['state'],'verified_complete')
        calls=[]
        self.a.read_verified_asset(self.op['operation_id'],'share-file-1',resolve_asset=lambda _:self.current,open_reader=lambda fid:calls.append(fid))
        self.assertEqual(calls,['dest-file-99'])

    def test_move_out_revokes_read_without_invoking_reader(self):
        self.reconcile()
        self.current['path']='/apps/教育分析学习其他/课件.txt'
        with self.assertRaises(GateError):
            self.a.read_verified_asset(self.op['operation_id'],'share-file-1',resolve_asset=lambda _:self.current,open_reader=lambda _:self.fail('reader must not run'))

    def test_share_identity_cannot_cross_between_transfers(self):
        self.observed[0]['source_share_ref']='different-share'
        with self.assertRaises(GateError): self.reconcile()

    def test_source_and_destination_ids_are_independent(self):
        self.assertNotEqual(self.manifest[0]['source_file_id'],self.observed[0]['destination_fs_id'])
        self.assertTrue(self.reconcile()['verified_items']==1)

    def test_incomplete_target_and_wrong_account_not_complete(self):
        self.assertEqual(self.reconcile([])['state'],'partial')
        self.current['account_ref']='account-2'
        with self.assertRaises(GateError): self.reconcile()

    def test_access_refusal_blocks_without_route_fallback(self):
        self.a.before_submit(self.op['operation_id'],resolve_destination=lambda p:p)
        self.a.record_submission(self.op['operation_id'],errno=403)
        with self.assertRaises(GateError): self.reconcile()
        with self.assertRaises(GateError): self.a.before_submit(self.op['operation_id'],resolve_destination=lambda p:p)

    def test_source_denial_is_durable_and_network_retry_is_bounded(self):
        url='https://www.wangzjk.com/55.html'
        with patch('worldanalysis.acquisition.fetch_public_article',side_effect=GateError('WEBSITE_ACCESS_DENIED')) as fetch:
            with self.assertRaises(GateError): self.a.discover_public_article(url)
            with self.assertRaises(GateError): self.a.discover_public_article(url)
            self.assertEqual(fetch.call_count,1)
        row=self.db.execute('SELECT state,attempts FROM site_probes WHERE article_url=?',(url,)).fetchone()
        self.assertEqual(row,('blocked',1))
        url='https://www.wangzjk.com/56.html'
        with patch('worldanalysis.acquisition.fetch_public_article',side_effect=GateError('SITE_NETWORK_FAILURE')) as fetch:
            with self.assertRaises(GateError): self.a.discover_public_article(url)
            with self.assertRaises(GateError): self.a.discover_public_article(url)
            self.assertEqual(fetch.call_count,1)
        self.assertIsNotNone(self.db.execute('SELECT next_run_at FROM site_probes WHERE article_url=?',(url,)).fetchone()[0])

    def test_article_is_description_and_private_entries_are_opaque(self):
        result=self.a.source_observation('https://www.wangzjk.com/1.html','<title>课程</title><article>介绍<a href="https://pan.baidu.com/s/abc?pwd=private">下载</a></article>')
        self.assertEqual(result['role'],'source_description_not_course_text')
        self.assertNotIn('private',str(result))
        self.assertEqual(len(result['authorized_entry_refs']),1)
        self.assertIn('private',self.a.authorized_entry(result['source_id'],result['authorized_entry_refs'][0]))

    def test_duplicate_manifest_and_unfinished_inventory_rejected(self):
        with self.assertRaises(GateError):
            self.a.prepare_transfer('p','s',self.path,self.manifest*2,grant_ref='g',enumeration_complete=True)
        with self.assertRaises(GateError):
            self.a.prepare_transfer('p','s',self.path,self.manifest,grant_ref='g',enumeration_complete=False)


if __name__=='__main__': unittest.main()
