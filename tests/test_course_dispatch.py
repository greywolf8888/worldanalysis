"""Regression cases for the category-aware, finish-current-first dispatcher."""
from argparse import ArgumentParser
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_tools/course_production"))
import batch
import batch_dispatch
from project import put, read


class Clock(datetime):
    current = datetime(2026, 11, 25, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz)


class DispatchTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.inputs = self.root / "分析目标下载地址"
        self.inputs.mkdir()
        self.registry = self.root / "调度" / "分类.json"
        self.legacy = self.root / "旧批次.json"
        Clock.current = datetime(2026, 11, 25, tzinfo=timezone.utc)
        self.clock = patch.object(batch_dispatch, "datetime", Clock)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def category(self, name, source="储存"):
        cat = self.inputs / name
        folder = cat / source
        folder.mkdir(parents=True)
        (cat / "分析结果" / ".obsidian").mkdir(parents=True)
        (cat / "正在分析").mkdir()
        (cat / "测试分析稿件").mkdir()
        return folder

    def course(self, source, name, text="独立课程"):
        folder = source / name
        folder.mkdir()
        (folder / "原课.md").write_text(text, encoding="utf-8")
        return folder

    def run_dispatch(self, refresh=True, seconds=0, legacy=()):
        Clock.current += timedelta(seconds=seconds)
        return batch_dispatch.dispatch(self.inputs, self.registry, legacy, refresh=refresh, settle_seconds=10)

    def existing(self, source, active=False):
        queue = batch.scan_batch(source, source.parent / "分析结果")
        if active:
            row = queue["projects"][0]
            row.update(status=batch.ACTIVE, package=str(self.root / "未完成课程包"))
        put(self.legacy, queue)
        return queue

    def command(self, *args, success=True):
        parser = ArgumentParser()
        batch.add_arguments(parser.add_subparsers(dest="action"))
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = batch.run(parser.parse_args(["batch", *map(str, args)]))
        self.assertEqual(code == 0, success, stdout.getvalue() + stderr.getvalue())
        return stdout.getvalue() if success else stderr.getvalue()

    def test_category_not_course_and_old_ledger_preserved(self):
        source = self.category("02_短视频与自媒体")
        self.course(source, "11 教程")
        old = self.existing(source, active=True)
        self.course(source.parent, "分类里的输出示例")
        self.course(source.parent / "测试分析稿件", "测试稿")
        (source / "分析结果").mkdir()
        (source / "正在分析").mkdir()
        (source / "测试分析稿件").mkdir()
        uncertain = self.inputs / "03_未明确布局"
        uncertain.mkdir()
        self.course(uncertain, "直接放置的疑似项目")
        before = self.legacy.read_bytes()
        plan = self.run_dispatch(False, legacy=[self.legacy])
        self.assertEqual(self.legacy.read_bytes(), before)
        self.assertFalse(self.registry.exists())
        self.assertEqual(plan["registered_projects"], 1)
        self.assertEqual(plan["present_projects"], 1)
        self.assertEqual(plan["next"]["name"], "11 教程")
        self.assertEqual(plan["unrecognized_categories"][0]["direct_candidates"], ["直接放置的疑似项目"])
        self.run_dispatch(legacy=[self.legacy])
        self.assertEqual(read(self.legacy)["projects"], old["projects"])
        self.assertEqual(self.run_dispatch()["next"]["id"], old["projects"][0]["id"])

    def test_finish_current_then_newest_ready_across_categories(self):
        source = self.category("02_旧")
        self.course(source, "11 处理中")
        self.course(source, "22 旧积压", "不同内容")
        old = self.existing(source, active=True)
        self.run_dispatch(legacy=[self.legacy])

        target1 = self.course(source, "999 新到件一")
        first = self.run_dispatch(seconds=1)
        self.assertEqual(first["next"]["name"], "11 处理中")
        self.assertEqual(first["categories"][0]["new_waiting"][0]["name"], target1.name)
        src2 = self.category("03_新", source="课程原件")
        target2 = self.course(src2, "901 新到件二", "第二个")
        second = self.run_dispatch(seconds=4)
        self.assertEqual(second["next"]["name"], "11 处理中")
        self.run_dispatch(seconds=11)
        self.assertEqual(self.run_dispatch(False)["next"]["id"], old["projects"][0]["id"])
        queue = read(self.legacy)
        queue["projects"][0]["status"] = batch.COMPLETE
        put(self.legacy, queue)  # Test fixture stands in for a successful finish.
        chosen = self.run_dispatch(False)["next"]
        self.assertEqual(chosen["name"], target2.name)
        self.assertEqual(chosen["category"], "03_新")
        error = self.command("claim", self.legacy, 3, self.root / "错误领取", success=False)
        self.assertIn("全局当前不可领取", error)
        q2 = read(Path(chosen["manifest"]))
        q2["projects"][0]["status"] = batch.COMPLETE
        put(Path(chosen["manifest"]), q2)
        self.assertEqual(self.run_dispatch(False)["next"]["name"], target1.name)
        q1 = read(self.legacy)
        q1["projects"][2]["status"] = batch.COMPLETE
        put(self.legacy, q1)
        self.assertEqual(self.run_dispatch(False)["next"]["name"], "22 旧积压")

    def test_new_file_change_or_partial_never_claimed_before_settle(self):
        source = self.category("02_旧")
        self.course(source, "22 旧积压")
        self.existing(source)
        self.run_dispatch(legacy=[self.legacy])
        new = self.course(source, "99 新课程")
        (new / "未完成.part").write_text("..." , encoding="utf-8")
        self.run_dispatch(seconds=1)
        result = self.run_dispatch(seconds=60)
        self.assertEqual(result["next"]["name"], "22 旧积压")
        self.assertIn("传输", result["categories"][0]["new_waiting"][0]["detail"])
        (new / "未完成.part").rename(new / "02 教材.txt")
        self.run_dispatch(seconds=1)
        self.assertEqual(self.run_dispatch(seconds=9)["next"]["name"], "22 旧积压")
        (new / "02 教材.txt").write_text("继续写入……", encoding="utf-8")
        self.assertEqual(self.run_dispatch(seconds=2)["next"]["name"], "22 旧积压")
        self.assertEqual(self.run_dispatch(seconds=11)["next"]["name"], "99 新课程")

    def test_possible_rename_never_duplicates_identity(self):
        source = self.category("02_分类")
        original = self.course(source, "11 原名")
        self.existing(source)
        self.run_dispatch(legacy=[self.legacy])
        original.rename(source / "12 改名")
        result = self.run_dispatch(seconds=11)
        self.assertEqual(result["registered_projects"], 1)
        self.assertEqual(result["categories"][0]["identity_pending"][0]["possible_sequences"], [1])

    def test_existing_formal_vault_requires_existing_ledger(self):
        source = self.category("02_分类")
        self.course(source, "11 原件")
        self.course(source.parent / "分析结果", "11 已交付")
        with self.assertRaisesRegex(ValueError, "权威批次"):
            self.run_dispatch()
        self.assertFalse(self.registry.exists())

    def test_direct_course_layout_only_with_explicit_marker(self):
        cat = self.inputs / "03_直属课程"
        cat.mkdir()
        self.course(cat, "项目一")
        self.course(cat, "项目二", "另一份")
        (cat / "正在分析").mkdir()
        (cat / "测试分析稿件").mkdir()
        self.assertEqual(self.run_dispatch(False)["present_projects"], 0)
        put(cat / batch_dispatch.CATEGORY_MARKER, {"schema": 1, "layout": "direct-projects"})
        first = self.run_dispatch()
        self.assertEqual(first["present_projects"], 2)
        self.assertEqual(first["registered_projects"], 2)
        self.assertIsNone(first["next"])
        settled = self.run_dispatch(seconds=11)
        self.assertEqual(settled["next"]["name"], "项目一")
        self.assertEqual(settled["next"]["source_folder"], str(cat / "项目一"))
        self.assertTrue((cat / "分析结果" / ".obsidian").is_dir())

    def test_direct_course_with_clear_core_file_but_not_nested_subcategory(self):
        cat = self.inputs / "03_直属课程"
        cat.mkdir()
        good = self.course(cat, "项目一")
        (good / "01.mp4").write_bytes(b"fixture")
        ambiguous = self.course(cat, "更深的一层")
        self.course(ambiguous, "嵌套子项目")
        marked = self.course(cat, "项目二")
        put(marked / batch_dispatch.PROJECT_MARKER, {"schema": 1, "kind": "course-project"})
        first = self.run_dispatch()
        self.assertEqual(first["registered_projects"], 2)
        self.assertEqual(first["unrecognized_categories"][0]["direct_candidates"], [ambiguous.name])
        self.course(cat, "后到的纯说明目录")
        settled = self.run_dispatch(seconds=11)
        self.assertEqual(settled["registered_projects"], 2)
        self.assertEqual(settled["next"]["category"], "03_直属课程")

    def test_explicit_user_order_changes_only_an_eligible_claim(self):
        source = self.category("02_旧")
        self.course(source, "11 旧课", "第一课")
        second = self.course(source, "22 旧课", "第二课")
        self.existing(source)
        self.run_dispatch(legacy=[self.legacy])
        other = self.category("03_新")
        self.course(other, "99 新课", "最新")
        self.run_dispatch(seconds=1)
        view = self.run_dispatch(seconds=11)
        chosen = view["next"]
        self.assertEqual(chosen["name"], "99 新课")
        self.assertIn("全局当前不可领取", self.command("claim", self.legacy, 2, self.root / "不按顺序",
                                                        success=False))
        before = self.legacy.read_bytes()
        self.assertIn("具体依据", self.command("claim", self.legacy, 2, self.root / "第二包",
                                               "--user-order-reason", "  ", success=False))
        self.assertEqual(self.legacy.read_bytes(), before)
        directive = "用户明确要求本轮先完成22旧课"
        row = json.loads(self.command("claim", self.legacy, 2, self.root / "第二包",
                                      "--user-order-reason", directive))
        self.assertEqual(row["name"], second.name)
        self.assertEqual(row["user_order_reason"], directive)
        self.assertTrue(row["user_order_at"])
        self.assertEqual(self.run_dispatch(False)["next"]["sequence"], 2)
        other_manifest = Path(chosen["manifest"])
        before_other = other_manifest.read_bytes()
        self.assertIn("仍在处理", self.command("claim", other_manifest, 1, self.root / "其他包",
                                               "--user-order-reason", "用户要求先做新课", success=False))
        self.assertEqual(other_manifest.read_bytes(), before_other)
        unchanged = self.legacy.read_bytes()
        self.command("claim", self.legacy, 2, self.root / "第二包")
        self.assertEqual(self.legacy.read_bytes(), unchanged)
        self.command("defer", self.legacy, 2, "等待原件")
        self.assertEqual(self.run_dispatch(False)["next"]["name"], "99 新课")

    def test_explicit_user_order_cannot_claim_new_unsettled_project(self):
        source = self.category("02_旧")
        self.course(source, "11 旧积压", "完整原件")
        self.existing(source)
        self.run_dispatch(legacy=[self.legacy])
        self.course(source, "99 新课", "传输候选")
        result = self.run_dispatch(seconds=1)
        self.assertEqual(result["next"]["name"], "11 旧积压")
        before = self.legacy.read_bytes()
        self.assertIn("尚未就绪", self.command("claim", self.legacy, 2, self.root / "新包",
                                               "--user-order-reason", "用户要求先做新课", success=False))
        self.assertEqual(self.legacy.read_bytes(), before)

    def test_catalog_updates_only_after_actual_dispatch_or_claim_change(self):
        source = self.category("02_旧")
        self.course(source, "22 旧积压")
        self.existing(source)
        with patch("vault_catalog.refresh_catalog") as catalog:
            self.run_dispatch(legacy=[self.legacy])
            self.assertEqual(catalog.call_count, 1)
            catalog.reset_mock()
            self.run_dispatch(False)
            self.run_dispatch()
            catalog.assert_not_called()
            self.course(source, "99 新来件", "独立新课程的具体实操")
            self.run_dispatch(seconds=1)
            self.assertEqual(catalog.call_count, 1)
            catalog.reset_mock()
            self.run_dispatch(seconds=11)
            catalog.assert_called_once()
            catalog.reset_mock()
            self.command("claim", self.legacy, 2, self.root / "新包")
            catalog.assert_called_once()
            catalog.reset_mock()
            self.command("claim", self.legacy, 2, self.root / "新包")
            catalog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
