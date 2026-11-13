import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from worldanalysis.__main__ import main, parser, routed_extract, route_config_revision, media_config_revision
from worldanalysis.capabilities import inventory


class CLITests(unittest.TestCase):
    def test_short_documents_default_to_registered_local_processors(self):
        with patch('worldanalysis.documents.extract',return_value={'result':'captured'}) as extract:
            result=routed_extract(Path('source.pdf'),Path('unused'))
            self.assertEqual(result,{'result':'captured'})
            options=extract.call_args.kwargs['options']
            self.assertTrue(callable(options['ocr']))
            self.assertTrue(callable(options['render_pdf_page']))

    def test_no_ocr_is_an_explicit_limitation_not_fake_extraction(self):
        with patch('worldanalysis.documents.extract',return_value={}) as extract:
            routed_extract(Path('source.docx'),Path('unused'),options={'ocr':None})
            self.assertIsNone(extract.call_args.kwargs['options']['ocr'])
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            parser().parse_args(['ingest','--project','p','--ocr','--no-ocr','x.txt'])

    def test_route_revision_stable_but_ocr_policy_change_is_new_version(self):
        first=route_config_revision(ocr_enabled=True)
        self.assertEqual(first,route_config_revision(ocr_enabled=True))
        self.assertNotEqual(first,route_config_revision(ocr_enabled=False))

    def test_media_profile_stable_and_processing_choice_is_versioned(self):
        opts={'ocr_enabled':True,'asr_enabled':True,'synthetic':True}
        self.assertEqual(media_config_revision(opts),media_config_revision(opts))
        self.assertNotEqual(media_config_revision(opts),media_config_revision(dict(opts,asr_enabled=False)))

    def test_media_cli_routes_to_bounded_api_not_small_file_hash_path(self):
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()):
            (Path(temp)/'sample.wav').write_bytes(b'synthetic routing fixture, not a valid recording')
            with patch('worldanalysis.store.Store.ingest_media',return_value={'status':'blocked','reason':'synthetic routing only'}) as call:
                self.assertEqual(main(['--root',temp,'ingest','--project','unused','--synthetic-input','--no-asr','sample.wav']),2)
                self.assertTrue(call.call_args.kwargs['options']['synthetic'])
                self.assertFalse(call.call_args.kwargs['options']['asr_enabled'])

    def test_actual_mixed_cli_publishes_native_and_bounded_audio_without_asr_claim(self):
        import wave
        import shutil
        from worldanalysis.store import Store
        temp=Path(tempfile.mkdtemp())
        def cleanup():
            import stat
            def writable(func,path,error):Path(path).chmod(stat.S_IWRITE);func(path)
            shutil.rmtree(temp,onerror=writable)
        self.addCleanup(cleanup)
        s=Store(temp);s.create_project(project_id='mixed-cli');s.close()
        (temp/'sample.txt').write_text('Explicit synthetic companion document.',encoding='utf-8')
        with wave.open(str(temp/'sample.wav'),'wb') as w:
            w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\x00\x00'*1600)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code=main(['--root',str(temp),'ingest','--project','mixed-cli','--synthetic-input','--no-asr','--no-ocr','sample.wav','sample.txt'])
        self.assertEqual(code,0)
        summary=json.loads(out.getvalue());self.assertEqual(summary['coverage']['claimed_status'],'partial')
        s=Store(temp)
        try:
            bundle=s.get_bundle('mixed-cli',summary['bundle_id'])
            self.assertEqual(len(bundle['assets']),2)
            self.assertFalse(any(b['origin']=='asr' for b in bundle['blocks']))
        finally:s.close()

    def test_incremental_cli_is_explicit_and_does_not_fall_through_to_complete(self):
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()) as out:
            with patch('worldanalysis.incremental_analysis.prepare',return_value={'status':'prepared','task_id':'task-routing'}) as plan:
                self.assertEqual(main(['--root',temp,'analysis','delta','--task','task-routing']),0)
                self.assertEqual(plan.call_args.args[1],'task-routing')
                self.assertEqual(json.loads(out.getvalue())['status'],'prepared')
            out.seek(0);out.truncate(0)
            with patch('worldanalysis.incremental_analysis.reuse',return_value={'status':'reused','task_id':'task-routing'}) as reused:
                self.assertEqual(main(['--root',temp,'analysis','reuse','--task','task-routing','--block','block-one']),0)
                self.assertEqual(reused.call_args.args[1:],('task-routing',['block-one']))
                self.assertEqual(json.loads(out.getvalue())['status'],'reused')

    def test_local_supplement_cli_only_routes_explicit_request(self):
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()) as out:
            with patch('worldanalysis.store.Store.fulfil_available',return_value={'status':'pending','request_id':'request-routing','new_parse':False}) as worker:
                self.assertEqual(main(['--root',temp,'supplement-fulfil','--request','request-routing']),0)
                worker.assert_called_once_with('request-routing')
                self.assertEqual(json.loads(out.getvalue())['status'],'pending')

    def test_crop_cli_passes_explicit_pixel_box_without_publication(self):
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()) as out:
            with patch('worldanalysis.store.Store.crop_region',return_value={'status':'derived_only','bundle_published':False}) as crop:
                self.assertEqual(main(['--root',temp,'crop-region','--project','p','--bundle','b','--block','block','--artifact','image','--bbox','1','2','10','20','--resize','9','18','--output-relative','补采派生/r.png']),0)
                self.assertEqual(crop.call_args.args[4],{'unit':'pixel','origin':'top_left','xyxy':[1,2,10,20]})
                self.assertEqual(crop.call_args.kwargs['resize'],[9,18])
                self.assertFalse(json.loads(out.getvalue())['bundle_published'])

    def test_unknown_inventory_not_zero_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            result=inventory(Path(temp))
            self.assertEqual(result['enumeration_state'],'unknown')
            self.assertEqual(result['counts'],{})

    def test_new_status_is_explicit_not_course_success(self):
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()) as output:
            code=main(['--root',temp,'status'])
            value=json.loads(output.getvalue())
            self.assertEqual(code,0)
            self.assertEqual(value['projects'],[])
            self.assertFalse(value['automatic_agent_subscription_token_reuse'])
            self.assertEqual(value['commercial_calls'],'disabled_by_default')


if __name__=='__main__':unittest.main()
