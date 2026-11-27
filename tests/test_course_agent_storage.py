"""Regression cases for G-only tool scratch and compact, persistent agent outputs."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


TOOLS = Path(__file__).resolve().parents[1] / "_tools/course_production"
sys.path.insert(0, str(TOOLS))
import run_course


class CourseAgentStorageTest(unittest.TestCase):
    def setUp(self):
        parent = run_course.course_runtime() / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        scratch = TemporaryDirectory(prefix="storage-test-", dir=parent)
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def test_large_result_saved_without_expanding_session_output(self):
        child = (
            "import json, os, pathlib, tempfile; "
            "t=pathlib.Path(tempfile.gettempdir()); "
            "(t/'proof.txt').write_text('temporary'); "
            "print(json.dumps([str(t),os.environ['TMP'],os.environ['TMPDIR'],"
            "os.environ['WORLDANALYSIS_COURSE_RUNTIME']])); print('x'*50000)"
        )
        output = io.StringIO()
        old = os.environ.get("TEMP")
        with contextlib.redirect_stdout(output):
            self.assertEqual(run_course.execute([sys.executable, "-B", "-c", child], self.root, True), 0)
        self.assertEqual(os.environ.get("TEMP"), old)
        logs = list((self.root / "agent-runs").iterdir())
        self.assertEqual(len(logs), 1)
        log = logs[0]
        lines = (log / "stdout.log").read_text(encoding="utf-8").splitlines()
        scratch, tmp, tmpdir, runtime = json.loads(lines[0])
        self.assertEqual(scratch, tmp)
        self.assertEqual(scratch, tmpdir)
        self.assertEqual(Path(runtime), self.root.resolve())
        self.assertEqual(Path(scratch).parent, self.root / "tmp")
        self.assertFalse(Path(scratch).exists())
        self.assertEqual(len(lines[1]), 50000)
        receipt = json.loads((log / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["exit_code"], 0)
        self.assertIsNone(receipt["scratch"])
        self.assertLess(len(output.getvalue()), 1500)
        self.assertIn("stdout.log", output.getvalue())

    def test_failed_run_retains_exact_diagnostics(self):
        child = (
            "import pathlib,sys,tempfile; "
            "(pathlib.Path(tempfile.gettempdir())/'proof.txt').write_text('failure'); "
            "print('required evidence unavailable',file=sys.stderr);sys.exit(7)"
        )
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(run_course.execute([sys.executable, "-B", "-c", child], self.root, True), 7)
        log = next((self.root / "agent-runs").iterdir())
        receipt = json.loads((log / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["exit_code"], 7)
        self.assertEqual((Path(receipt["scratch"]) / "proof.txt").read_text(), "failure")
        self.assertIn("required evidence unavailable", (log / "stderr.log").read_text())
        self.assertIn(receipt["scratch"], error.getvalue())

    def test_missing_obsidian_input_keeps_failure_receipt_in_project(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")
        project = self.root / "课程包"
        preview = project / ".course/work/阅读预览"
        preview.mkdir(parents=True)
        attempt = subprocess.run([node, str(TOOLS / "verify_obsidian.cjs"), "--project", str(project), str(preview)],
                                 capture_output=True, check=False)
        self.assertNotEqual(attempt.returncode, 0)
        qa = next(preview.parent.glob("obsidian-render-*"))
        proof = json.loads((qa / "failure-report.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(proof["folder"]), project)
        self.assertIn("ENOENT", proof["error"])
        self.assertFalse((preview / "obsidian-render.json").exists())


if __name__ == "__main__":
    unittest.main()
