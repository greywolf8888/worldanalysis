"""Exercise retirement on disposable files while leaving signed courses alone."""

import hashlib
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1] / "_tools/course_production"
with patch.object(sys, "path", [str(TOOLS), *sys.path]):
    import retire_originals as retirement
    from preservation import file_identity
    from project import local_child, put, sha


OBSIDIAN = os.environ.get("WORLDANALYSIS_TEST_OBSIDIAN")


@unittest.skipUnless(os.name == "nt", "Windows exact-handle file operations")
class RetireOriginalsTest(unittest.TestCase):
    name = "录屏 #01.mp4"

    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.workspace = Path(scratch.name).resolve()
        self.batch = self.workspace / "整理结果/批次.json"
        self.package = self.workspace / "整理结果/课程包"
        self.origin = self.workspace / "收到/当前课程"
        self.vault = self.workspace / "成果"
        self.entry = self.vault / "当前课程/课件/当前课程.md"
        self.source = self.origin / self.name
        self.reader_copy = self.entry.parent.parent / "原始资料" / self.name
        self.canonical = self.package / "原始资料" / self.name
        self.data = os.urandom(1024 * 1024)
        for path in (self.source, self.reader_copy, self.canonical):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.data)
        (self.package / ".course/ledger").mkdir(parents=True)
        (self.vault / ".obsidian").mkdir()
        self.entry.parent.mkdir()
        self.entry.write_text("# 当前课程\n", "utf-8")
        put(self.batch, {"fixture": True})
        self.digest = hashlib.sha256(self.data).hexdigest()
        put(self.package / ".course/ledger/01.json", {"source": self.name, "audio_source": self.name,
            "sha256": self.digest, "audio_processed": True, "media": {"streams": [{"codec_type": "audio"}]},
            "speech_windows": [{"text": "仅为该测试准备的模拟原始语音"}]})
        self.identity = {"batch": str(self.batch), "sequence": 1, "project_id": "synthetic-only", "package": str(self.package),
                         "entry": str(self.entry), "vault": str(self.vault), "origin": str(self.origin)}
        self.manifest = {"retained": {}, "source_videos": {self.name: {"signed_intake_sha256": self.digest, "copies": {
            role: {"path": str(path), **file_identity(path)} for role, path in (
                ("manual_download", self.source), ("reading_vault", self.reader_copy), ("package_snapshot", self.canonical))}}}}
        self.row = {"status": "已验收"}
        self.intake = {"sources": [{"file": self.name, "sha256": self.digest}]}
        self.selection = {"batch": str(self.batch), "sequence": 1,
                          "copies": [{"file": self.name, "role": role} for role in retirement.ROLES]}
        binding = patch.object(retirement, "basis", return_value=(self.identity, self.manifest, self.row,
                                                                 self.intake, "完整验收"))
        self.basis = binding.start()
        self.addCleanup(binding.stop)

    def approve_fixture(self, scope):
        decision = local_child(self.workspace, "_exchange/control/decisions/test-only.json")
        put(decision, {"schema_version": 1, "record_type": "course_local_cleanup_authorization", "actor": "user",
                       "decision": "approve", "method": scope["method"], "scope_sha256": scope["scope_sha256"],
                       "candidates": scope["candidates"], "human_message_ref": "synthetic isolated test fixture"})
        return decision

    def test_staged_copies_restore_and_retire_with_readback(self):
        preview = retirement.plan(self.workspace, self.selection, stage_complete=True)
        scope = preview["scope"]
        self.assertTrue(preview["pressure"]["cleanup_preflight_needed"])
        self.assertEqual(len(scope["candidates"]), 2)
        decision = self.approve_fixture(scope)
        with self.assertRaisesRegex(ValueError, "尚未提交"):
            retirement.operate(self.workspace, scope, "finalize", decision)
        staged = retirement.operate(self.workspace, scope, "stage", decision)
        self.assertEqual(staged["state"], "staged")
        self.assertEqual(retirement.operate(self.workspace, scope, "status")["locations"], ["staged", "staged"])
        self.assertTrue(self.canonical.is_file())
        self.assertNotIn("observed_volume_delta_bytes", staged)
        self.assertEqual(retirement.operate(self.workspace, scope, "restore")["state"], "restored")
        self.assertTrue(self.source.is_file() and self.reader_copy.is_file())
        self.assertEqual(retirement.operate(self.workspace, scope, "stage", decision)["state"], "staged")
        with patch.object(retirement, "reader", return_value={"synthetic_reader_fixture": True}) as reader:
            result = retirement.operate(self.workspace, scope, "finalize", decision)
            self.assertEqual(reader.call_count, 2)
        self.assertEqual(result["state"], "verified")
        self.assertFalse(result["downstream"]["execution_allowed"])
        self.assertEqual(retirement.operate(self.workspace, scope, "status")["locations"], ["absent", "absent"])
        self.assertEqual(self.canonical.read_bytes(), self.data)
        self.assertIsInstance(result["observed_volume_delta_bytes"], int)

    def test_unauthorized_changed_and_protected_files_stay_at_original_paths(self):
        scope = retirement.plan(self.workspace, self.selection)["scope"]
        decision = self.approve_fixture(scope)
        with self.assertRaises(FileNotFoundError):
            retirement.operate(self.workspace, scope, "stage", decision.with_name("unapproved.json"))
        self.reader_copy.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "路径发生变化"):
            retirement.operate(self.workspace, scope, "stage", decision)
        self.assertTrue(self.source.is_file())
        self.reader_copy.write_bytes(self.data)
        self.basis.return_value = (self.identity, self.manifest, self.row, self.intake, "配套待补")
        with self.assertRaisesRegex(ValueError, "尚无完整验收"):
            retirement.plan(self.workspace, self.selection)
        self.basis.return_value = (self.identity, self.manifest, self.row, self.intake, "完整验收")
        self.entry.write_text(f"[尚需原片](<../原始资料/{self.name.replace('#', '%23')}>)", "utf-8")
        with self.assertRaisesRegex(ValueError, "仍引用"):
            retirement.plan(self.workspace, self.selection)

    def test_interrupted_final_removal_keeps_unknown_effect_out_of_handoff(self):
        scope = retirement.plan(self.workspace, self.selection)["scope"]
        decision = self.approve_fixture(scope)
        retirement.operate(self.workspace, scope, "stage", decision)
        remove = retirement.remove_exact_windows_file

        def interrupted(candidate):
            remove(candidate)
            raise OSError("simulate interruption before persisting disposition")

        with (patch.object(retirement, "reader", return_value={"synthetic_reader_fixture": True}),
              patch.object(retirement, "remove_exact_windows_file", side_effect=interrupted),
              self.assertRaises(OSError)):
            retirement.operate(self.workspace, scope, "finalize", decision)
        self.assertEqual(retirement.operate(self.workspace, scope, "status")["locations"], ["absent", "staged"])
        with self.assertRaisesRegex(ValueError, "禁止重复提交"):
            retirement.operate(self.workspace, scope, "finalize", decision)
        with self.assertRaisesRegex(ValueError, "还没有"):
            retirement.operate(self.workspace, scope, "handoff")
        self.assertEqual(self.canonical.read_bytes(), self.data)

    @unittest.skipUnless(OBSIDIAN and Path(OBSIDIAN).is_file(), "local Obsidian required")
    def test_actual_obsidian_reopens_detached_and_retired_synthetic_course(self):
        old = "> - 模拟原件出处。\n"
        new = "> - 可按正文复现实例。\n"
        body = "# 当前课程\n\n按示例说明复核设置。 <!-- 段落出处 合成测试输入 -->\n\n> [!info]- 原课与配套资料\n" + new
        self.entry.write_bytes(body.encode("utf-8"))
        self.manifest.update(course_dir="vault/当前课程", note="课件/当前课程.md",
            derived_body_sha256=sha(self.entry), parent_body_sha256=hashlib.sha256(body.replace(new, old).encode("utf-8")).hexdigest(),
            changes=[{"line": len(body.splitlines()), "original": old, "replacement": new}])
        self.identity["derived_body_sha256"] = sha(self.entry)
        scope = retirement.plan(self.workspace, self.selection)["scope"]
        decision = self.approve_fixture(scope)
        retirement.operate(self.workspace, scope, "stage", decision)
        receipt = retirement.operate(self.workspace, scope, "finalize", decision, Path(OBSIDIAN))
        handoff = retirement.operate(self.workspace, scope, "handoff")
        self.assertEqual(receipt["state"], "verified")
        self.assertNotEqual(receipt["detached_reader"]["path"], receipt["post_cleanup_reader"]["path"])
        self.assertEqual(handoff["current_body_sha256"], sha(self.entry))
        self.assertFalse(handoff["execution_allowed"])


if __name__ == "__main__":
    unittest.main()
