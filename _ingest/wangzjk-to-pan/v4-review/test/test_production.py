"""Production contract regression. Synthetic approvals stay in memory, never publish files."""
import json
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
from urllib.parse import quote
import unittest
from catalog_model import compact, Library, open_index, SITE
from course_completion import verified_companion_install
from production_bridge import CONTRACT, LocalProjection, exact_map
from confirm_gate import inspect_for_original_executor,receipt_binds,readiness,ordered_items_sha256
from v4_exchange import selection_sha256

def fixture(scope='recursive'):
    day=datetime(2026,11,26,15,0,tzinfo=timezone.utc)
    raw=compact({'schema_version':1,'record_type':'range_manifest','scope':scope,'complete_recursive':scope=='recursive','root_count':1,'source_id':'test-source','source_revision':'v1','files':[{'file':'folder/file.bin','bytes':99}]})
    item={'queue_item_id':'test-item','resource_version_id':'test-version','site_id':SITE,'article_id':'1','source_id':'test-source','source_revision':'v1','range_manifest_ref':{'path':'_exchange/dsh/catalog/test-no-file.json','sha256':sha256(raw).hexdigest()},'item_rank':1,'transfer_task_id':'test-task'}
    p={'schema_version':1,'record_type':'confirmed_plan','producer':'dsh','state':'confirmed','execution_allowed':True,'batch_id':'test-batch','revision':1,'order_revision':1,'confirmation_seq':1,'confirmed_at':day.isoformat(),'order_mode':'strict','items':[item]}
    p['selection_sha256']=selection_sha256(p['items']); receipt={**{k:p[k] for k in ('schema_version','batch_id','revision','order_revision','confirmation_seq','confirmed_at','order_mode','selection_sha256')},'record_type':'user_confirmation','record_id':'test-only','producer':'dsh-ui','state':'confirmed','execution_allowed':True,'observed_at':day.isoformat(),'confirmation_actor':'user','ordered_items_sha256':ordered_items_sha256(p['items'])}
    auth=compact(receipt);p['authorization_ref']={'path':'_exchange/dsh/authorizations/test-only.json','sha256':sha256(auth).hexdigest()}
    obs={**{k:item[k] for k in ('resource_version_id','site_id','article_id','source_id','source_revision')},'observed_at':day.isoformat(),'evidence_class':'normal_ui_dom','account_matches':True,'source_document_fresh':True,'source_root_count_verified':True,'target_list_complete':True,'target_collision':False,'original_root_count':1,'destination_binding_verified':True,'range_manifest_sha256':item['range_manifest_ref']['sha256']}
    capacity={'observed_at':day.isoformat(),'evidence_class':'normal_ui_dom','account_matches':True,'minimum_free_bytes':10**11,'remaining_bytes':10**11+200}
    args={'host_attestation_id':'memory-test','trusted_host_check':lambda a,b:True,'load_manifest':lambda r:raw,'observe_exact':lambda r:obs,'capacity':capacity,'duplicate_block':lambda x,y:False,'crawl':{'phase':'paused','platform_cleared':True,'original_session_account':True,'no_other_coordinator':True},'clock':day}
    return p,auth,args

class ConfirmationGate(unittest.TestCase):
    def test_plausible_receipt_cannot_replace_host_attestation(self):
        p,a,k=fixture();self.assertTrue(receipt_binds(p,a));k['trusted_host_check']=None
        self.assertEqual(inspect_for_original_executor(p,a,**k)['code'],'NO_TRUSTED_DESKTOP_USER_DECISION')
    def test_in_memory_pass_still_does_not_allow_execution(self):
        p,a,k=fixture(); r=inspect_for_original_executor(p,a,**k); self.assertEqual(r['admission'],'preflight_matches');self.assertFalse(r['execution_allowed']);self.assertEqual(r['selected_bytes'],99)
    def test_scope_and_order_and_revision_are_not_interchangeable(self):
        p,a,k=fixture();self.assertFalse(receipt_binds({**p,'order_mode':'ready_ordered'},a)); self.assertFalse(receipt_binds({**p,'order_revision':2,'revision':2},a));self.assertFalse(receipt_binds({**p,'demo':True},a));self.assertFalse(receipt_binds(p,a+b' '))
        p,a,k=fixture('selected_roots_only'); self.assertEqual(inspect_for_original_executor(p,a,**k)['code'],'EXACT_RECURSIVE_SCOPE_REQUIRED')
    def test_capacity_stale_at_floor_and_package_above_reserve(self):
        p,a,k=fixture();k['clock']+=timedelta(seconds=31); self.assertEqual(inspect_for_original_executor(p,a,**k)['code'],'FRESH_ACTUAL_ACCOUNT_CAPACITY_REQUIRED')
        p,a,k=fixture();k['capacity']['remaining_bytes']=10**11; self.assertEqual(inspect_for_original_executor(p,a,**k)['code'],'CAPACITY_RESERVE_HIT_OR_UNKNOWN')
        p,a,k=fixture();k['capacity']['remaining_bytes']=10**11+99; self.assertEqual(inspect_for_original_executor(p,a,**k)['code'],'PACKAGE_SCOPE_CROSSES_RESERVE')
    def test_unknown_duplicate_proof_and_platform_hold(self):
        p,a,k=fixture();k['duplicate_block']=lambda x,y:None;self.assertEqual(inspect_for_original_executor(p,a,**k)['code'],'SOURCE_OR_TARGET_PREVIOUSLY_RESERVED_OR_CLAIMED')
        p,a,k=fixture();k['crawl']['platform_cleared']=False;self.assertEqual(inspect_for_original_executor(p,a,**k)['code'],'ORIGINAL_COORDINATOR_OR_PLATFORM_HOLD')

class ProductionProjection(unittest.TestCase):
    def test_companion_install_binds_signed_body_slot_to_the_installed_entry(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as scratch:
            workspace = Path(scratch).resolve()
            entry = workspace / '分析结果/课程原名/课件/课程原名.md'
            entry.parent.mkdir(parents=True)
            body = '# 课程原名\n[模板](../配套资料/模板.txt)\n'.encode('utf-8')
            entry.write_bytes(body)
            companion_name = '配套资料/模板.txt'
            companion = entry.parent.parent / companion_name
            companion.parent.mkdir()
            companion_bytes = '完整模板'.encode('utf-8')
            companion.write_bytes(companion_bytes)
            body_sha = sha256(body).hexdigest()
            companions = {companion_name: sha256(companion_bytes).hexdigest()}
            results = workspace / '整理结果'
            manifest = results / '批次状态.json'
            package = results / '课程工作包'
            receipt = package / f'.course/ledger/publications/{body_sha}.json'
            receipt.parent.mkdir(parents=True)
            published = {'entry': entry.as_posix(), 'sha256': body_sha}
            manifest.write_bytes(compact({'projects': [{
                'id': 'expected-id', 'package': package.as_posix(), 'publication': published,
            }]}))
            project = {'local_project_id': 'expected-id', 'evidence': {'manifest': manifest.as_posix()}}
            signed = {'schema': 1, 'body': body.decode('utf-8'), 'outputs': dict(companions),
                      'review': {'snapshot': {'text_sha256': body_sha, 'companions': companions}}}
            for slot in ('课件/00 开始学习.md', '课件/教材.md'):
                with self.subTest(slot=slot):
                    signed['outputs'] = {**companions, slot: body_sha}
                    receipt.write_bytes(compact(signed))
                    self.assertTrue(verified_companion_install(project, entry, workspace, body))

            for broken in (
                {**signed, 'outputs': {**companions, '别处/教材.md': body_sha}},
                {**signed, 'outputs': {**signed['outputs'], '课件/另一本.md': body_sha}},
                {**signed, 'body': '# 另一版课件'},
                {**signed, 'review': {'snapshot': {'text_sha256': 'a' * 64, 'companions': companions}}},
            ):
                receipt.write_bytes(compact(broken))
                self.assertFalse(verified_companion_install(project, entry, workspace, body))

            receipt.write_bytes(compact(signed))
            moved = entry.with_name('其他课程.md')
            moved.write_bytes(body)
            self.assertFalse(verified_companion_install(project, moved, workspace, body))
            companion.write_text('已变更模板', encoding='utf-8')
            self.assertFalse(verified_companion_install(project, entry, workspace, body))

    def test_textbook_overview_retains_one_readable_legacy_entry_without_status_or_dates(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as scratch:
            vault = Path(scratch).resolve() / '分析结果'
            overview = vault / '00 入口/00 项目总览.md'
            overview.parent.mkdir(parents=True)

            def entry(title, contents):
                path = vault / title / '课件' / (title + '.md')
                path.parent.mkdir(parents=True)
                path.write_bytes(contents)
                return path

            legacy, registered, altered, absent_from_overview = (
                entry(title, title.encode('utf-8')) for title in ('旧课', '正式课', '已变更', '未列出')
            )
            links = '\n'.join(
                f'- [{path.stem}](../{quote(path.relative_to(vault).as_posix())}) · 分析 待核实'
                for path in (legacy, registered, altered)
            )
            overview.write_text(
                f'<!-- Worldanalysis:course-catalog -->\n## 按分析日期（新到旧）\n{links}\n'
                '<!-- /Worldanalysis:course-catalog -->\n', encoding='utf-8',
            )

            def projection(path, project_id, relation, digest=None):
                digest = digest or sha256(path.read_bytes()).hexdigest()
                return {'local_project_id': project_id, 'title': path.stem, 'textbook': {
                    'entry': path.as_posix(), 'state': 'registered_edition_matches', 'relation': relation,
                    'current_sha256': digest, 'registered_sha256': digest,
                }, 'acceptance': {'state': 'same_source_reuse' if relation == 'same_source_reuse'
                                 else 'historical_same_edition'}}

            rows = [projection(registered, 'owner', 'own'),
                    projection(legacy, 'alias-1', 'same_source_reuse'),
                    projection(legacy, 'alias-2', 'same_source_reuse'),
                    projection(altered, 'changed', 'same_source_reuse', 'a' * 64),
                    projection(absent_from_overview, 'outside-overview', 'same_source_reuse')]
            box = LocalProjection(SimpleNamespace(), load=lambda: {
                'projection_status': 'available', 'observed_at': '2026-11-26T14:00:00Z',
                'categories': [{'vault': vault.as_posix()}], 'projects': rows,
            })
            box.history_at = monotonic()
            page = box.textbooks(1)
            self.assertEqual(page['count'], 2)
            self.assertEqual([book['title'] for book in page['items']], ['旧课', '正式课'])
            item = page['items'][0]
            self.assertEqual(item['course_completion'], {'textbook': 'legacy_entry_pending'})
            self.assertNotIn('project_id', item)
            self.assertNotIn('acceptance_state', item)
            self.assertIsNone(item['analysis_date'])
            self.assertIsNone(item['production_date'])

    def test_duplicate_double_key_and_title_not_used(self):
        row={'site_id':SITE,'article_id':'33','cloud_root_id':'123','local_project_id':'project1','title':'unrelated name','basis':'historical_exact_root_and_source_sha256','evidence':{'path':'_ingest/wangzjk-to-pan/state/evidence/a.json','sha256':'a'*64}}
        self.assertEqual(exact_map([row]),{('33','123'):'project1'});self.assertEqual(exact_map([row,{**row,'local_project_id':'other'}]),{});self.assertEqual(exact_map([{**row,'article_id':None}]),{})
    @unittest.skipUnless(CONTRACT.is_file(), 'requires the locally provisioned production projection')
    def test_real_bridge_and_cleanup_do_not_infer_current_completeness(self):
        lib=open_index();box=LocalProjection(lib); o=box.overview();self.assertIn(o['sync_state'],('live','partial'));self.assertGreaterEqual(o['legacy_exact_identity_links'],100);self.assertFalse(o['cloud_deletion_allowed']);self.assertGreaterEqual(o['counts']['registered_projects'],160)
        r=box.cleanup_preparation();self.assertEqual(r['historic_exact_recycled'],163);self.assertEqual(r['new_delete_candidates'],0)
        for p in list(lib.by_id.values())[:16]:
            v=box.detail(p);self.assertIsNone(v['scope_download_complete']);self.assertIsNone(v['current_cloud_presence'])
    def test_pending_sync_retains_observed_time_and_draft_remains_inert(self):
        lib=Library([]);lib.snapshot_id='synthetic-review';box=LocalProjection(lib,load=lambda:{'observed_at':'2026-11-26T14:00:00Z','projection_status':'available'})
        self.assertEqual(box.get()['sync_state'],'live')
        def broken():raise ValueError('test unavailable')
        box.load=broken;box.loaded_at=-10000000; g=box.get(); self.assertEqual(g['sync_state'],'pending_sync');self.assertEqual(g['observed_at'],'2026-11-26T14:00:00Z')
        p=readiness(lib,box,{'ids':[],'bindings':{},'revision':0});self.assertEqual(p['eligible_for_confirmation'],0);self.assertIsNone(p['actual_remaining_bytes']);self.assertFalse(p['execution_allowed'])
