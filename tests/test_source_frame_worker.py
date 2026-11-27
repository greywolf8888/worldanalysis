import json
import hashlib
import io
import os
from unittest.mock import patch
from PIL import Image
from worldanalysis.store import Store
from worldanalysis.platform_first import PlatformFirst
from worldanalysis.platform_execution import SourceFrameExecution, RECEIPT_SCHEMA, REVIEW_SCHEMA
from worldanalysis.source_frame_worker import _resume_capture, _new_requests, _save_observation
from pathlib import Path
import tempfile
import unittest
from worldanalysis.source_frame_worker import _save, _runtime_cache_authorization
from worldanalysis.policy import GateError
from worldanalysis.contracts import safe_path

class SourceFrameWorkerFiles(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parents[1]/'_ledger/devpack/cloud-state-20261116/frame-repair-temp/test-tmp'
        base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base)
        self.root=Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
    def test_new_handoff_creates_parents_without_requiring_file(self):
        self.assertEqual(_save(self.root,'nested/ready.json',{'job_id':'job-real'}),'nested/ready.json')
        path=self.root/'nested/ready.json'
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),{'job_id':'job-real'})
        old=path.read_bytes()
        with self.assertRaises(FileExistsError):_save(self.root,'nested/ready.json',{'changed':True})
        self.assertEqual(path.read_bytes(),old)
    def test_pending_marker_path_is_valid_before_marker_exists(self):
        path=safe_path(self.root,'capture/receipts/2000.ready',must_exist=False)
        self.assertFalse(path.exists())
    def execution_fixture(self, *, start=True, targets=None):
        store=Store(self.root);self.addCleanup(store.close)
        project=store.create_project(topic='Synthetic ready-recovery fixture')
        coord=PlatformFirst(store,allow_synthetic=True)
        source={'account_ref':'fixture-account','file_id':'101','source_revision':'fixture-v1','size_bytes':100,
                'path':'/apps/教育分析学习/测试/fixture.mp4'}
        inventory={'schema_version':'1.0','inventory_id':'ready-fixture','account_ref':source['account_ref'],
            'root_path':'/apps/教育分析学习/测试','enumeration_complete':True,'evidence_ref':'synthetic-ready-list',
            'evidence_class':'synthetic','files':[{k:v for k,v in source.items() if k!='account_ref'}]}
        run=coord.begin_run(project,inventory)['run_id']
        for product in ('manuscript','courseware'):
            coord.record_product_observation(run,'101',product,{'state':'missing','evidence_ref':'synthetic-missing'})
        coord.seal_first_pass(run)
        (self.root/'capture.js').write_text('/* never executed synthetic helper */',encoding='utf-8')
        execution=SourceFrameExecution(store,coord)
        task=execution.create(run,'101',[0,1000] if targets is None else targets,helper_path='capture.js',target_evidence_ref='synthetic-targets')
        store.submission_authorizer=lambda row:{'state':'granted','grant_ref':'synthetic-read-grant','job_id':row['job_id'],'project_id':row['project_id']}
        if start:execution.start(task['job_id'])
        return execution,task,source

    def local_ready(self, task, source, target, *, receipt=True, marker=True, request=None):
        base=task['capture_root']
        request=request or {'requested_ms':target,'relative_path':base+'/frames/'+str(target)+'.jpg',
            'receipt_relative_path':base+'/receipts/'+str(target)+'.json','ready_marker':base+'/receipts/'+str(target)+'.ready'}
        path=self.root/request['relative_path'];path.parent.mkdir(parents=True,exist_ok=True)
        image=io.BytesIO();Image.new('RGB',(20,12),'blue').save(image,'JPEG');data=image.getvalue()
        with path.open('xb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        if receipt:
            _save(self.root,request['receipt_relative_path'],{'schema':RECEIPT_SCHEMA,'source':source,'requested_ms':target,
                'frame_callback':{'mediaTime':target/1000},'source_identity_check':{'file_id_match':True,'version_token_match':True},
                'helper_sha256':task['helper_sha256'],'relative_path':request['relative_path'],
                'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'width':20,'height':12,'format':'jpeg'})
        if marker:
            path=self.root/request['ready_marker'];path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('xb') as stream:stream.flush();os.fsync(stream.fileno())
        return request,data

    def resume(self, execution, task, source, authorizer=None):
        return _resume_capture(execution,task,root=self.root,source=source,handoff_prefix='ipc/restart',
            runtime_authorizer=authorizer,context={'browser_task_ref':'synthetic-browser-task'},batch_limit=3)

    @staticmethod
    def synthetic_host(context):
        return {'state':'granted','evidence_class':'runtime_host_readback','browser_task_ref':context['browser_task_ref'],
            'media_cache_policy':'no_disk_media_storage','producer_ref':'synthetic-host-fixture',
            'evidence_ref':'synthetic-host-receipt','receipt_sha256':'f'*64}

    def test_orphan_ready_is_verified_and_adopted_before_any_host_or_new_read_permission(self):
        execution,task,source=self.execution_fixture()
        requests=[self.local_ready(task,source,t)[0] for t in (0,1000)]
        old={'schema':'worldanalysis.source-frame-worker-handoff.v1',**{k:task[k] for k in ('job_id','parent_job_id','helper_sha256','capture_root')},
             'source':source,'requests':requests}
        _save(self.root,'ipc/restart-ready.json',old)
        _save(self.root,'ipc/restart-result.json',{'status':'old-observation-not-completion'})
        old_bytes=(self.root/'ipc/restart-ready.json').read_bytes()
        with patch('worldanalysis.source_frame_worker._runtime_cache_authorization',side_effect=AssertionError('no new media permission for local adoption')):
            with patch.object(execution,'start',side_effect=AssertionError('no new read submission for local adoption')):
                resumed=self.resume(execution,task,source)
                repeated=self.resume(execution,task,source)
        self.assertEqual(set(resumed['adopted']),{0,1000})
        self.assertEqual(resumed['requests'],[])
        self.assertEqual(resumed['progress']['pending'],[])
        self.assertFalse(execution.status(task['job_id'])['quality_accepted'])
        self.assertEqual(execution.status(task['job_id'])['state'],'submission_unknown')
        self.assertEqual(repeated['adopted'],[])
        self.assertNotEqual(resumed['recovery'],repeated['recovery'])
        self.assertEqual((self.root/'ipc/restart-ready.json').read_bytes(),old_bytes)
        result=_save_observation(self.root,'ipc/restart-result.json',{'status':'awaiting_independent_quality_review'})
        self.assertNotEqual(result,'ipc/restart-result.json')
        self.assertEqual(json.loads((self.root/'ipc/restart-result.json').read_text())['status'],'old-observation-not-completion')
        self.assertEqual(execution._events('source_frame_runtime_cache_observation',task['job_id']),[])

    def test_incomplete_orphan_jpg_is_preserved_with_unmeasured_cache_and_fresh_attempt(self):
        execution,task,source=self.execution_fixture()
        request,data=self.local_ready(task,source,0,receipt=False,marker=False)
        resumed=self.resume(execution,task,source)
        self.assertEqual(resumed['runtime_cache_observation']['state'],'unmeasured')
        self.assertFalse(resumed['runtime_cache_observation']['cache_policy_known'])
        self.assertTrue(resumed['runtime_cache_observation']['raw_media_file_output_forbidden'])
        self.assertEqual(execution._events('source_frame_runtime_cache_observation',task['job_id']),[resumed['runtime_cache_observation']])
        self.assertEqual(execution.status(task['job_id'])['pending'],[0,1000])
        self.assertEqual([r['requested_ms'] for r in resumed['requests']],[0,1000])
        newer=_new_requests(task,[0])[0]
        for fresh in resumed['requests']:
            self.assertIn('/attempts/',fresh['relative_path'])
            self.assertNotEqual(fresh['relative_path'],request['relative_path'])
            self.assertFalse((self.root/fresh['relative_path']).exists())
        self.assertNotEqual(newer['relative_path'],resumed['requests'][0]['relative_path'])
        self.assertEqual((self.root/request['relative_path']).read_bytes(),data)

    def test_ready_without_receipt_is_not_adopted_and_never_overwritten(self):
        execution,task,source=self.execution_fixture()
        request,data=self.local_ready(task,source,0,receipt=False)
        result=self.resume(execution,task,source)
        self.assertEqual(result['adopted'],[])
        self.assertEqual(result['runtime_cache_observation']['state'],'unmeasured')
        self.assertTrue(all(r['relative_path']!=request['relative_path'] for r in result['requests']))
        recovery=json.loads((self.root/'ipc/restart-recovery.json').read_text(encoding='utf-8'))
        self.assertEqual(recovery['adopted'],[])
        self.assertEqual(len(recovery['invalid_local_results']),1)
        self.assertTrue((self.root/request['ready_marker']).exists())
        self.assertEqual((self.root/request['relative_path']).read_bytes(),data)
        self.assertEqual(execution._events('source_frame_accepted',task['job_id']),[])

    def test_same_prefix_handoff_identity_is_rechecked_and_ungranted_local_results_fail_closed(self):
        execution,task,source=self.execution_fixture(start=False)
        requests=[self.local_ready(task,source,t)[0] for t in (0,1000)]
        with self.assertRaises(GateError):self.resume(execution,task,source)
        self.assertEqual(execution._events('source_frame_accepted',task['job_id']),[])
        bad={'schema':'worldanalysis.source-frame-worker-handoff.v1',**{k:task[k] for k in ('job_id','parent_job_id','helper_sha256','capture_root')},
            'source':{**source,'source_revision':'different-revision'},'requests':requests}
        _save(self.root,'ipc/restart-ready.json',bad)
        from worldanalysis.contracts import ContractError
        with self.assertRaisesRegex(ContractError,'prior handoff'):
            self.resume(execution,task,source)

    def test_partial_local_ready_commits_before_unmeasured_cache_frame_handoff(self):
        execution,task,source=self.execution_fixture()
        self.local_ready(task,source,0)
        orphan,data=self.local_ready(task,source,1000,receipt=False,marker=False)
        result=self.resume(execution,task,source)
        progress=execution.pending_capture_targets(task['job_id'])
        self.assertEqual(progress['pending'],[1000])
        self.assertEqual([f['requested_ms'] for f in progress['valid_frames']],[0])
        self.assertEqual(result['adopted'],[0])
        self.assertEqual([r['requested_ms'] for r in result['requests']],[1000])
        self.assertTrue(result['started']['may_capture'])
        self.assertEqual(result['runtime_cache_observation']['state'],'unmeasured')
        self.assertEqual((self.root/orphan['relative_path']).read_bytes(),data)
        self.assertFalse(execution.status(task['job_id'])['quality_accepted'])
        self.assertEqual(execution._events('source_frame_runtime_cache_observation',task['job_id']),[result['runtime_cache_observation']])

    def test_complete_corrupt_target_can_adopt_fresh_local_ready_without_host_and_old_qa_is_not_reused(self):
        execution,task,source=self.execution_fixture()
        first,_=self.local_ready(task,source,0)
        second,good=self.local_ready(task,source,1000)
        self.resume(execution,task,source)
        manifest=execution.prepare_manifest(task['job_id'])
        _save(self.root,'qa-old.json',{'schema':REVIEW_SCHEMA,'job_id':task['job_id'],'manifest_sha256':manifest['sha256'],
            'source':source,'state':'approved','basis':'source_frames','reviewer_ref':'synthetic-only-reviewer',
            'evidence_ref':'synthetic-only-qa','method':'independent_visual_review','all_frames_reviewed':True,
            'source_alignment_verified':True,'timeline_coverage_sufficient':True,'unresolved_gaps':[]})
        execution.finalize(task['job_id'],'qa-old.json')
        frozen=execution._job(task['job_id'])[0]['receipt_json']
        (self.root/first['relative_path']).write_bytes(b'synthetic corruption')
        replacement=_new_requests(task,[0])[0]
        self.local_ready(task,source,0,request=replacement)
        with patch('worldanalysis.source_frame_worker._runtime_cache_authorization',side_effect=AssertionError('local repair is not a new browser read')):
            resumed=self.resume(execution,task,source)
        self.assertEqual(resumed['adopted'],[0])
        self.assertEqual(resumed['progress']['pending'],[])
        self.assertEqual(execution.status(task['job_id'])['state'],'verified_complete')
        self.assertFalse(execution.status(task['job_id'])['quality_accepted'])
        self.assertEqual(execution._job(task['job_id'])[0]['receipt_json'],frozen)
        self.assertEqual((self.root/second['relative_path']).read_bytes(),good)
        self.assertNotEqual(execution.prepare_manifest(task['job_id'])['sha256'],manifest['sha256'])

    def test_complete_warm_ready_results_do_not_scan_or_decode_again(self):
        execution,task,source=self.execution_fixture(targets=list(range(0,700,100)))
        for target in task['requested_ms']:self.local_ready(task,source,target)
        self.resume(execution,task,source)
        manifest=execution.prepare_manifest(task['job_id'])
        _save(self.root,'qa-warm.json',{'schema':REVIEW_SCHEMA,'job_id':task['job_id'],'manifest_sha256':manifest['sha256'],
            'source':source,'state':'approved','basis':'source_frames','reviewer_ref':'synthetic-only-reviewer',
            'evidence_ref':'synthetic-only-warm-qa','method':'independent_visual_review','all_frames_reviewed':True,
            'source_alignment_verified':True,'timeline_coverage_sufficient':True,'unresolved_gaps':[]})
        execution.finalize(task['job_id'],'qa-warm.json')
        with patch.object(Path,'rglob',side_effect=AssertionError('no pending target means no ready scan')):
            with patch('worldanalysis.platform_execution._jpeg',side_effect=AssertionError('warm images must not decode')):
                result=self.resume(execution,task,source)
        self.assertEqual(result['adopted'],[])
        self.assertEqual(result['progress']['pending'],[])
        self.assertTrue(execution.status(task['job_id'])['quality_accepted'])

    def test_one_pending_target_does_not_decode_six_intact_ready_markers(self):
        execution,task,source=self.execution_fixture(targets=list(range(0,700,100)))
        requests=[self.local_ready(task,source,t)[0] for t in task['requested_ms']]
        self.resume(execution,task,source)
        (self.root/requests[0]['relative_path']).write_bytes(b'synthetic corruption')
        replacement=_new_requests(task,[0])[0]
        self.local_ready(task,source,0,request=replacement)
        receipt=self.root/replacement['receipt_relative_path'];original=receipt.read_bytes()
        spoof=json.loads(original);spoof['requested_ms']=100
        receipt.write_text(json.dumps(spoof),encoding='utf-8')
        with patch('worldanalysis.platform_execution._jpeg',side_effect=AssertionError('nonpending markers must not decode')):
            waiting=self.resume(execution,task,source)
        self.assertEqual(waiting['adopted'],[])
        self.assertEqual([r['requested_ms'] for r in waiting['requests']],[0])
        self.assertEqual(waiting['runtime_cache_observation']['state'],'unmeasured')
        self.assertEqual(execution.pending_capture_targets(task['job_id'])['pending'],[0])
        receipt.write_bytes(original)
        from worldanalysis.platform_execution import _jpeg
        with patch('worldanalysis.platform_execution._jpeg',wraps=_jpeg) as decode:
            result=self.resume(execution,task,source)
            self.assertEqual(decode.call_count,1)
        self.assertEqual(result['adopted'],[0])
        self.assertEqual(result['progress']['pending'],[])

    def test_missing_cache_producer_is_unmeasured_not_a_fake_grant(self):
        ctx={'browser_task_ref':'fixture-task'}
        self.assertEqual(_runtime_cache_authorization(None,ctx),
            {'state':'unmeasured','browser_task_ref':'fixture-task','cache_policy_known':False,
             'full_media_cache_absence_unproved':True,'raw_media_file_output_forbidden':True})
        for authorizer in (False,lambda _:True,lambda _:None):
            with self.assertRaisesRegex(GateError,'BROWSER_MEDIA_CACHE_AUDIT_REQUIRED'):
                _runtime_cache_authorization(authorizer,ctx)
    def test_queued_existing_source_can_start_with_persisted_unmeasured_cache_observation(self):
        execution,task,source=self.execution_fixture(start=False)
        result=self.resume(execution,task,source)
        self.assertTrue(result['started']['may_send'])
        self.assertTrue(result['started']['may_capture'])
        self.assertEqual([r['requested_ms'] for r in result['requests']],[0,1000])
        observation=result['runtime_cache_observation']
        self.assertEqual(observation['state'],'unmeasured')
        self.assertFalse(observation['cache_policy_known'])
        self.assertTrue(observation['full_media_cache_absence_unproved'])
        self.assertTrue(observation['raw_media_file_output_forbidden'])
        self.assertNotIn('grant_ref',observation);self.assertNotIn('producer_ref',observation)
        self.assertEqual(len(execution._events('source_frames_started',task['job_id'])),1)
        self.assertEqual(execution._events('source_frame_runtime_cache_admission',task['job_id']),[])
        with Store(self.root) as reader:
            saved=reader.connection.execute("SELECT payload_json FROM events WHERE kind='source_frame_runtime_cache_observation' AND record_id=?",(task['job_id'],)).fetchone()
            self.assertEqual(json.loads(saved[0]),observation)

    def test_unmeasured_cache_does_not_replace_the_normal_read_grant(self):
        execution,task,source=self.execution_fixture(start=False)
        execution.store.submission_authorizer=None
        result=self.resume(execution,task,source)
        self.assertEqual(result['runtime_cache_observation']['state'],'unmeasured')
        self.assertFalse(result['started']['may_capture'])
        self.assertEqual(result['requests'],[])
        self.assertEqual(execution._job(task['job_id'])[0]['state'],'queued')
        self.assertEqual(execution._events('source_frames_started',task['job_id']),[])

    def test_explicit_cache_denial_mismatch_or_bad_proof_never_falls_back_to_unmeasured(self):
        execution,task,source=self.execution_fixture(start=False)
        proof=self.synthetic_host({'browser_task_ref':'synthetic-browser-task'})
        for bad in ({**proof,'state':'denied'},{**proof,'browser_task_ref':'other-task'},{**proof,'receipt_sha256':'not-a-sha'}):
            with self.assertRaisesRegex(GateError,'BROWSER_MEDIA_CACHE_AUDIT_REQUIRED'):
                self.resume(execution,task,source,lambda _,bad=bad:bad)
            self.assertEqual(execution._job(task['job_id'])[0]['state'],'queued')
            self.assertEqual(execution._events('source_frames_started',task['job_id']),[])
            self.assertEqual(execution._events('source_frame_runtime_cache_observation',task['job_id']),[])

    def test_runtime_fixture_scope_must_match_and_is_not_real_audit(self):
        ctx={'browser_task_ref':'fixture-task'}
        proof={'state':'granted','evidence_class':'runtime_host_readback','browser_task_ref':'other-task',
            'media_cache_policy':'no_disk_media_storage','producer_ref':'synthetic-runtime-fixture',
            'evidence_ref':'synthetic-receipt','receipt_sha256':'a'*64}
        with self.assertRaisesRegex(GateError,'BROWSER_MEDIA_CACHE_AUDIT_REQUIRED'):
            _runtime_cache_authorization(lambda _:proof,ctx)
        proof['browser_task_ref']='fixture-task'
        self.assertEqual(_runtime_cache_authorization(lambda _:proof,ctx),proof)

if __name__=='__main__':unittest.main()
