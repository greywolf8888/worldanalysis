"""Media bridge tests. FakeWorker is synthetic contract data, never real ASR."""
import hashlib
import json
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch
import wave
from worldanalysis.contracts import ContractError, hash_file
from worldanalysis.store import Store
from worldanalysis.media import _identity


def cleanup(path):
    def writable(func,p,exc):
        Path(p).chmod(stat.S_IWRITE);func(p)
    shutil.rmtree(path,onerror=writable)


class FakeWorker:
    provider_id='synthetic-media-bridge'
    provider_version='test-1'
    def __init__(self,*,pause=False,bad_source=False,fail=False):
        self.calls=0;self.pause=pause;self.bad_source=bad_source;self.fail=fail
    def __call__(self,source,out,*,options=None):
        self.calls+=1
        if self.fail: raise RuntimeError('synthetic interrupted worker')
        out.mkdir(parents=True,exist_ok=True)
        # Worker internals are not publication artifacts.
        (out/'.worker-private').write_bytes(b'checkpoint')
        p=out/f'window-{self.calls}.json'
        p.write_text(json.dumps({'synthetic':True,'window':self.calls}),encoding='utf-8')
        files=[];blocks=[]
        for index,file in enumerate(sorted(out.glob('window-*.json'))):
            data=file.read_bytes();key=f'w{index}'
            files.append({'key':key,'relative_path':file.name,'kind':'inventory','media_type':'application/json','sha256':hashlib.sha256(data).hexdigest(),'byte_length':len(data)})
            blocks.append({'kind':'audio_segment','origin':'native_extract','locator':{'kind':'time','precision':'exact','start_ms':index*1000,'end_ms':(index+1)*1000},'file_keys':[key],'processing_status':'extracted','reason':'Synthetic descriptor, not actual speech inference'})
        pending=self.pause and self.calls==1
        ident=_identity(source)
        if self.bad_source:ident['path']=str(source.parent/'unrelated.wav')
        return {'media_type':'audio/wav','provider_id':self.provider_id,'provider_version':self.provider_version,'blocks':blocks,'files':files,'gaps':[{'kind':'temporal_gap','severity':'important','reason':'CHECKPOINT_PAUSE: synthetic test batch','block_indexes':[]}] if pending else [],'metrics':[{'unit':'millisecond','expected':None,'observed':len(blocks)*1000,'processed':0,'excluded':0,'method':'full_audio_windows','ranges':[]}],'enumeration_state':'partial' if pending else 'complete','processing_status':'partial','metadata':{'synthetic':True,'production_acceptance':'not_a_real_ASR_test','source_identity':ident,'input_bytes_received':self.calls*64,'derivations':[]}}


class MediaStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=Path(tempfile.mkdtemp(prefix='wa-media-store-'))
        self.addCleanup(cleanup,self.temp)
        self.store=Store(self.temp)
        self.addCleanup(lambda:self.store.close() if self.store.connection else None)
        self.project=self.store.create_project(project_id='media-test',topic='synthetic media bridge')
        self.source=self.temp/'source.wav'
        with wave.open(str(self.source),'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(b'\x00\x00'*8000)
    def ingest(self,worker,**kw):
        return self.store.ingest_media(self.project,[self.source],worker,config_version='test-config-v1',**kw)

    def test_descriptor_is_not_full_media_hash_and_derived_bundle_valid(self):
        worker=FakeWorker()
        real_hash=hash_file
        def guarded(path):
            self.assertNotEqual(Path(path).resolve(),self.source.resolve(),'must not hash full media input')
            return real_hash(path)
        with patch('worldanalysis.store.hash_file',side_effect=guarded),patch('worldanalysis.media_store.hash_file',side_effect=guarded):
            bundle=self.ingest(worker)
        self.assertEqual(bundle['coverage']['claimed_status'],'partial')
        self.assertEqual(worker.calls,1)
        originals=[a for a in bundle['artifacts'] if a['relative_path'].endswith('.source.json')]
        self.assertEqual(len(originals),1)
        self.assertEqual(originals[0]['kind'],'inventory')
        descriptor=json.loads((self.store.project_root(self.project)/originals[0]['relative_path']).read_bytes())
        self.assertIsNone(descriptor['source_content_sha256'])
        self.assertFalse(descriptor['source_copy_retained'])
        self.assertFalse(any(a['kind']=='original_small_file' for a in bundle['artifacts']))
        self.assertFalse(any('.worker-private' in a['relative_path'] for a in bundle['artifacts']))
        self.assertTrue(any('stat metadata' in g['reason'] for g in bundle['gaps']))
        self.assertEqual(self.store.get_bundle(self.project)['bundle_id'],bundle['bundle_id'])

    def test_same_input_and_config_reuses_without_decode(self):
        worker=FakeWorker();first=self.ingest(worker);again=self.ingest(worker)
        self.assertEqual(first['bundle_id'],again['bundle_id']);self.assertEqual(worker.calls,1)

    def test_collection_pause_prevents_source_registration(self):
        self.store.control('collection','pause')
        worker=FakeWorker();value=self.ingest(worker)
        self.assertEqual(value['status'],'blocked');self.assertEqual(worker.calls,0)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM media_sources').fetchone()[0],0)

    def test_wrong_source_fails_closed_and_unknown_attempt_not_replayed(self):
        worker=FakeWorker(bad_source=True)
        with self.assertRaises(ContractError):self.ingest(worker)
        with self.assertRaises(ContractError):self.ingest(worker)
        self.assertEqual(worker.calls,1)
        row=self.store.connection.execute('SELECT * FROM media_executions').fetchone()
        self.assertEqual(row['state'],'unknown_io');self.assertGreater(row['reserved_bytes'],0)

    def test_worker_exception_retains_io_reservation(self):
        worker=FakeWorker(fail=True)
        with self.assertRaises(RuntimeError):self.ingest(worker)
        with self.assertRaises(ContractError):self.ingest(worker)
        self.assertEqual(worker.calls,1)

    def test_explicit_checkpoint_resume_keeps_old_windows(self):
        worker=FakeWorker(pause=True);first=self.ingest(worker)
        cached=self.ingest(worker)
        self.assertEqual(worker.calls,1);self.assertEqual(first['bundle_id'],cached['bundle_id'])
        second=self.ingest(worker,resume=True)
        self.assertNotEqual(first['bundle_id'],second['bundle_id']);self.assertEqual(worker.calls,2)
        self.assertEqual(len(second['blocks']),2)
        row=self.store.connection.execute('SELECT * FROM media_executions').fetchone()
        self.assertEqual(row['received_bytes'],128);self.assertEqual(row['reserved_bytes'],0)

    def test_publication_crash_reuses_durable_raw_not_inference(self):
        worker=FakeWorker()
        with patch.object(self.store,'_after_rename',side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):self.ingest(worker)
        self.assertEqual(worker.calls,1)
        self.store.recover_publications()
        result=self.ingest(worker)
        self.assertEqual(worker.calls,1);self.assertEqual(result['publication']['state'],'published')
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM publications').fetchone()[0],1)

    def test_changed_source_new_revision_old_derived_bundle_stays_readable(self):
        worker=FakeWorker();first=self.ingest(worker)
        with self.source.open('ab') as f:f.write(b'changed')
        second=self.ingest(worker)
        self.assertNotEqual(first['assets'][0]['asset_revision'],second['assets'][0]['asset_revision'])
        self.assertEqual(self.store.get_bundle(self.project,first['bundle_id'])['bundle_id'],first['bundle_id'])

    def test_options_cannot_expand_scope_or_resource_policy(self):
        worker=FakeWorker()
        with self.assertRaises(ContractError):self.ingest(worker,options={'allowed_root':self.temp.parent})
        with self.assertRaises(ContractError):self.ingest(worker,options={'ram_byte_cap':8*1024**3})
        self.assertEqual(worker.calls,0)

    def test_demux_only_uses_registered_file_object_not_native_file_protocol(self):
        import sys
        import types
        from worldanalysis.media import _open,_limits
        fake=types.SimpleNamespace(open=lambda reader,**kwargs:kwargs)
        with patch.dict(sys.modules,{'av':fake}):
            options=_open(object(),_limits())['options']
        self.assertEqual(options['protocol_whitelist'],'pipe')
        self.assertEqual(options['enable_drefs'],'0')

    def test_multi_source_is_rejected_before_worker_until_merge_is_connected(self):
        worker=FakeWorker()
        value=self.store.ingest_media(self.project,[self.source,self.source],worker)
        self.assertEqual(value['status'],'unsupported');self.assertEqual(worker.calls,0)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM media_sources').fetchone()[0],0)

    def test_actual_bounded_decoder_to_bundle_synthetic_audio(self):
        bundle=self.store.ingest_media(self.project,[self.source],options={'asr_enabled':False,'ocr_enabled':False,'synthetic':True},config_version='real-decoder-synthetic-v1')
        self.assertEqual(bundle['publication']['state'],'published')
        self.assertTrue(bundle['blocks'])
        self.assertFalse(any(b['origin']=='asr' for b in bundle['blocks']))
        self.assertTrue(any(g['kind']=='unprocessed' for g in bundle['gaps']))
        packet=json.loads(self.store.connection.execute('SELECT raw_json FROM media_executions').fetchone()[0])
        self.assertGreater(packet['metadata']['input_bytes_received'],0)
        self.assertFalse(packet['metadata']['full_input_hash_performed'])


if __name__=='__main__':unittest.main()
