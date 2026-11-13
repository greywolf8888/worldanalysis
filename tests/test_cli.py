import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from worldanalysis.__main__ import main, parser, routed_extract, route_config_revision
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
