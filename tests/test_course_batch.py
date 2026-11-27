"""Exercise batch ordering, reuse and migration with isolated local files."""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_tools/course_production"))
import batch
from project import CourseProject, ENTRY, put, sha
from reader_review import snapshot
from source_coverage import coverage_review_template, fingerprint
from vault_delivery import confirm, install


class CourseBatchTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.source = self.root / "储存"
        self.source.mkdir()
        self.vault = self.root / "日常库"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.state = self.root / "批次.json"
        self.parser = ArgumentParser()
        batch.add_arguments(self.parser.add_subparsers(dest="action"))

    def command(self, *args, success=True):
        args = self.parser.parse_args(["batch", *map(str, args)])
        out, error = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            code = batch.run(args)
        self.assertEqual(code == 0, success, out.getvalue() + error.getvalue())
        return json.loads(out.getvalue()) if success else error.getvalue()

    def source_folder(self, name, data):
        path = self.source / name
        (path / "资料").mkdir(parents=True)
        (path / "资料/说明.md").write_text(data, encoding="utf-8")
        return path

    def existing_publication(self, source, package, *, vault=None, folder=None, observed=True, legacy=False):
        project = CourseProject.initialize(source, package)
        document = "资料/说明.md"
        text = f"# 项目\n\n## 实操\n\n准备资料。 <!-- 段落出处 {document} · 首段 -->\n"
        project.entry.parent.mkdir()
        project.entry.write_bytes(text.encode("utf-8"))
        body_sha = sha(project.entry)
        record = {
            "body": text, "outputs": {ENTRY.as_posix(): body_sha},
            "review": {"episodes": [], "snapshot": {
                **snapshot(project.folder, text), "evidence": {"ledger/intake.json": sha(project.intake)}}},
            "obsidian": {"checks": [{
                "document_sha256": body_sha, "mode": "preview", "reachedBottom": True,
                "horizontalOverflow": False, "visibleCitations": [],
                "closedAppendices": ["原课与配套资料"]}]}}
        if not legacy:
            scope = {"intake_sha256": sha(project.intake), "unreadable": [], "videos": [], "documents": [{
                "id": "D001", "source": document, "candidates": [],
                "units": [{"key": "file", "kind": "file", "value": None}]}]}
            coverage = coverage_review_template(scope, text)
            coverage["documents"][0]["units"][0].update(
                outcome="正文已覆盖", note="核对原说明的首段", anchor="准备资料")
            coverage["claims"][0].update(outcome="原件已核", note="已对照原说明")
            record["review"].update(schema=2, source_scope=scope, source_coverage=coverage)
            record["review"]["snapshot"]["source_scope_sha256"] = fingerprint(scope)
        put(project.ledger / "publications" / f"{body_sha}.json", record)
        state = install(project, record, vault or self.vault, folder)
        if observed:
            self.record_reopen(project, record, state)
        return project, record, state

    def record_reopen(self, project, record, state):
        confirm(project, record, {
            **{key: state[key] for key in ("entry", "sha256", "installed_at")},
            "mode": "preview", "reopened": True, "reached_bottom": True,
            "references_collapsed": True, "visible_citations": False,
            "section_seen": "实操", "note": "重开同一版教材并浏览至文末"})

    def initialize(self, *args):
        return self.command("init", self.source, "--manifest", self.state, "--vault", self.vault, *args)

    def test_natural_order_and_exact_duplicate_decision(self):
        first = self.source_folder("第2项", "same source")
        self.source_folder("第10项", "same source")
        (self.source / "第99项 空项目").mkdir()
        (self.source / "未归属.txt").write_text("outside any course", encoding="utf-8")
        before = list(self.root.iterdir())
        plan = self.command("plan", self.source)
        self.assertEqual(list(self.root.iterdir()), before)
        self.assertEqual([row["name"] for row in plan["entries"][:2]], [first.name, "第10项"])
        self.assertEqual(plan["counts"], {batch.PENDING: 2, batch.DEFERRED: 1})
        self.assertEqual(plan["unassigned"], ["未归属.txt"])
        self.assertEqual(plan["possible_duplicate_groups_by_size"], [[1, 2]])
        self.initialize()
        package = self.root / "第2项教材"
        second = self.root / "第10项教材"
        self.assertIn("当前应处理第 1 项", self.command("claim", self.state, 2, second, success=False))
        self.assertIn("与第 [2] 项完全一致", self.command("claim", self.state, 1, package, success=False))
        row = self.command("claim", self.state, 1, package, "--separate", "文件相同，已核实两项需要独立入口")
        self.assertEqual(row["duplicates"], [2])
        self.assertEqual(row["status"], batch.ACTIVE)
        saved = self.state.read_bytes()
        self.assertEqual(self.command("claim", self.state, 1, package), row)
        self.assertEqual(self.state.read_bytes(), saved)
        self.assertIn("已绑定课程包", self.command("claim", self.state, 1, second, success=False))
        self.assertEqual(self.command("defer", self.state, 1, "等待原附件")["status"], batch.DEFERRED)
        self.assertEqual(self.command("status", self.state)["next"], 2)
        self.assertEqual(self.command("reopen", self.state, 1)["status"], batch.PENDING)
        self.assertEqual(self.command("status", self.state)["next"], 1)
        self.assertEqual(self.command("claim", self.state, 1, package)["package"], str(package))

    def test_wrong_package_and_changed_original_need_explicit_recheck(self):
        original = self.source_folder("甲项目", "first")
        wrong = self.source_folder("乙项目", "first")
        wrong_package = self.root / "乙的包"
        CourseProject.initialize(wrong, wrong_package)
        result = self.initialize()
        index = next(r["sequence"] for r in result["entries"] if r["name"] == original.name)
        if index != 1:
            self.command("defer", self.state, 1, "先核对另一项目")
        saved = self.state.read_bytes()
        error = self.command("claim", self.state, index, wrong_package, "--separate", "分别归档", success=False)
        self.assertIn("另一批次原件", error)
        self.assertEqual(self.state.read_bytes(), saved)
        bound = self.root / "正确课程包"
        self.command("claim", self.state, index, bound, "--separate", "分别归档")
        source_file = original / "资料/说明.md"
        info = source_file.stat()
        source_file.write_text("other", encoding="utf-8")
        os.utime(source_file, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertIn("原件指纹", self.command("claim", self.state, index, bound, success=False))
        self.command("defer", self.state, index, "收到修订版，待再次核对")
        self.command("reopen", self.state, index, "--accept-changes", "原件同大小修订已核对")
        row = self.command("claim", self.state, index, bound)
        self.assertEqual(row["duplicates"], [])
        self.assertIn("history", batch.read(self.state)["projects"][index - 1])

    def test_finish_requires_the_expected_named_vault_note_and_current_receipt(self):
        source = self.source_folder("原始课程", "课程说明")
        self.initialize()
        package = self.root / "独立课程包"
        self.command("claim", self.state, 1, package, "--course-folder", "保留旧目录名")
        project, publication, state = self.existing_publication(
            source, package, folder="保留旧目录名", observed=False)
        self.assertIn("同入口重开记录", self.command("finish", self.state, 1, package, success=False))
        self.record_reopen(project, publication, state)
        target = Path(state["entry"])
        original = target.read_bytes()
        target.write_bytes(original + b"\nedit")
        self.assertIn("不一致", self.command("finish", self.state, 1, package, success=False))
        target.write_bytes(original)
        row = self.command("finish", self.state, 1, package)
        self.assertEqual(row["status"], batch.COMPLETE)
        self.assertEqual(row["publication"]["entry"], str(target))
        self.assertEqual(row["publication"]["sha256"], sha(target))
        saved = self.state.read_bytes()
        self.assertEqual(self.command("finish", self.state, 1, package), row)
        self.assertEqual(self.state.read_bytes(), saved)

    def test_cannot_finish_into_wrong_vault_or_different_course_folder(self):
        source = self.source_folder("录课", "原片提示")
        self.initialize()
        package = self.root / "录课教材"
        self.command("claim", self.state, 1, package, "--course-folder", "原课教材")
        different = self.root / "临时验证库"
        (different / ".obsidian").mkdir(parents=True)
        self.existing_publication(source, package, vault=different, folder="原课教材")
        self.assertIn("绑定的日常阅读库", self.command("finish", self.state, 1, package, success=False))
        self.assertEqual(self.command("status", self.state)["counts"], {batch.ACTIVE: 1})

    def test_does_not_accept_a_legacy_publication_for_new_queue_work(self):
        source = self.source_folder("文档项目", "课程说明")
        self.initialize()
        package = self.root / "旧教材"
        self.command("claim", self.state, 1, package)
        self.existing_publication(source, package, legacy=True)
        self.assertIn("当前来源覆盖验收", self.command("finish", self.state, 1, package, success=False))
        self.assertEqual(self.command("status", self.state)["counts"], {batch.ACTIVE: 1})

    def test_refresh_keeps_existing_ids_and_claims_pending_in_natural_order(self):
        initial = self.source_folder("第20项", "课程说明")
        old_entry = self.initialize()["entries"][0]
        self.source_folder("第10项", "另一份课")
        self.assertEqual(self.command("status", self.state)["new"], ["第10项"])
        update = self.command("refresh", self.state)
        self.assertEqual(update["new"], [])
        self.assertEqual(update["next"], 2)
        self.assertEqual(update["entries"][0], old_entry)
        self.assertEqual(update["entries"][1]["name"], "第10项")
        (initial / "后续资料.md").write_text("新增在旧项目内", encoding="utf-8")
        drift, = self.command("refresh", self.state)["metadata_drift"]
        self.assertEqual(drift["sequence"], 1)

    def test_missing_active_package_cannot_be_silently_replaced(self):
        source = self.source_folder("文档项目", "课程说明")
        self.initialize()
        package = self.root / "课程包"
        CourseProject.initialize(source, package)
        self.command("claim", self.state, 1, package)
        backup = self.root / "移动后的课程包"
        package.rename(backup)
        self.assertEqual(self.command("status", self.state)["package_drift"], [1])
        self.assertIn("不在原路径", self.command("claim", self.state, 1, package, success=False))
        self.assertIn("已绑定课程包", self.command("claim", self.state, 1, backup, success=False))

    def test_supplement_preserves_sequence_and_reuses_the_named_vault_entry(self):
        source = self.source_folder("第2项", "课程说明")
        self.source_folder("第3项", "另一门课")
        self.initialize()
        previous = CourseProject.initialize(source, self.root / "初版")
        before = self.command("claim", self.state, 1, previous.folder, "--course-folder", "已有课程名")
        combined = self.root / "补料原件"
        (combined / "资料").mkdir(parents=True)
        (combined / "资料/说明.md").write_text("课程说明", encoding="utf-8")
        (combined / "首讲.md").write_text("从同课完整版本核出的新增材料", encoding="utf-8")
        package = self.root / "新版"
        project = CourseProject.initialize(combined, package)

        reason = "同课另一版的共有材料已比对，补回首讲"
        result = self.command("supplement", self.state, 1, package, "--reason", reason)
        self.assertEqual(result["status"], batch.ACTIVE)
        self.assertEqual(result["course_folder"], before["course_folder"])
        self.assertEqual(result["source_sha256"], before["source_sha256"])
        self.assertEqual(result["supplemented_sha256"], batch.source_fingerprint(project.validate_raw()))
        self.assertIn("补回首讲", result["supplement_reason"])
        self.assertEqual(len(previous.validate_raw()), 1)
        self.assertEqual(batch.read(self.state)["projects"][0]["history"][-1]["package"], str(previous.folder))
        self.assertEqual(self.command("status", self.state)["next"], 1)
        once = self.state.read_bytes()
        self.assertEqual(self.command("supplement", self.state, 1, package, "--reason", reason), result)
        self.assertEqual(self.command("claim", self.state, 1, package), result)
        self.assertEqual(self.state.read_bytes(), once)
        self.existing_publication(combined, package, folder=before["course_folder"])
        finished = self.command("finish", self.state, 1, package)
        self.assertEqual(finished["status"], batch.COMPLETE)
        self.assertEqual(Path(finished["publication"]["entry"]).name, "已有课程名.md")
        self.assertEqual(self.command("status", self.state)["next"], 2)

    def test_supplement_cannot_replace_an_existing_original(self):
        source = self.source_folder("课程", "课程原件")
        self.initialize()
        previous = CourseProject.initialize(source, self.root / "初版")
        self.command("claim", self.state, 1, previous.folder)
        combined = self.root / "补料原件"
        (combined / "资料").mkdir(parents=True)
        (combined / "资料/说明.md").write_text("替换内容", encoding="utf-8")
        (combined / "首讲.md").write_text("新增原件", encoding="utf-8")
        project = CourseProject.initialize(combined, self.root / "新版")
        unchanged = self.state.read_bytes()
        error = self.command("supplement", self.state, 1, project.folder, "--reason", "待补第一讲", success=False)
        self.assertIn("逐件保留原快照", error)
        self.assertEqual(self.state.read_bytes(), unchanged)
        self.assertEqual(len(previous.validate_raw()), 1)

    def test_simultaneous_claims_cannot_bind_different_course_packages(self):
        self.source_folder("文档项目", "课程说明")
        self.initialize()
        cli = Path(batch.__file__).with_name("course.py")

        def invoke(package):
            return subprocess.run(
                [sys.executable, "-X", "utf8", "-B", str(cli), "batch", "claim",
                 str(self.state), "1", str(package)],
                capture_output=True, text=True, encoding="utf-8", timeout=20)

        with ThreadPoolExecutor(max_workers=2) as executor:
            replies = list(executor.map(invoke, [self.root / "包甲", self.root / "包乙"]))
        self.assertEqual(sorted(reply.returncode for reply in replies), [0, 2])
        succeeded = next(reply for reply in replies if not reply.returncode)
        self.assertEqual(self.command("status", self.state)["entries"][0]["package"],
                         json.loads(succeeded.stdout)["package"])

    def test_legacy_migration_preserves_current_and_requires_explicit_supplement_proof(self):
        original = self.source_folder("001 原项目", "课程说明")
        self.source_folder("002 缺少正文", "仅可见目录")
        working = self.source_folder("003 正在编辑", "新项目草案")
        root = self.root / "旧批次"
        root.mkdir()
        earlier = root / "核验原件与作者参考"
        earlier_source = earlier / "资料/说明.md"
        earlier_source.parent.mkdir(parents=True)
        earlier_source.write_bytes((original / "资料/说明.md").read_bytes())
        (earlier / "作者官方材料.md").write_text("同版补充", encoding="utf-8")
        package = self.root / "旧课程包"
        project, _, published = self.existing_publication(earlier, package, legacy=True)
        current_package = self.root / "继续工作"
        CourseProject.initialize(working, current_package)
        data = batch.scan_batch(self.source, self.vault)
        old = {"入库来源": str(self.source), "日常库": str(self.vault), "项目": [
            {"序号": row["sequence"], "目录名": row["name"], "原件目录": str(self.source / row["name"]),
             "文件": [{"路径": item["file"], "大小": item["bytes"], "最后修改_ns": item["mtime_ns"]}
                     for item in row["metadata"]]} for row in data["projects"]]}
        put(root / batch.LEGACY_QUEUE, old)
        report = root / "交付结果.json"
        put(report, {"verified": True, "package_verified": True, "entry": str(project.entry),
                     "vault_delivery": published})
        put(root / "001-已验收项目.json", {
            "次序": 1, "入口": published["entry"], "版本": published["sha256"], "交付回执": str(report)})
        put(root / "002-原始教程待提供.json", {"序号": 2, "状态": "缺少原正文"})
        put(root / batch.LEGACY_CURRENT, {
            "次序": 3, "原件": str(working), "目标项目": current_package.name,
            "课程包": str(current_package), "进度": "已转写待校稿"})
        saved = {path.name: sha(path) for path in root.glob("*.json")}
        plan = self.command("plan", self.source, "--vault", self.vault, "--legacy-directory", root)
        self.assertEqual(plan["historical_supplements_pending_confirmation"], [1])
        self.assertEqual(plan["next"], 3)
        self.assertFalse(self.state.exists())
        self.assertIn("补料快照", self.command(
            "init", self.source, "--manifest", self.state, "--vault", self.vault,
            "--legacy-directory", root, success=False))
        self.assertFalse(self.state.exists())
        loaded = self.initialize("--legacy-directory", root, "--supplemented", 1)
        self.assertEqual(loaded["counts"], {batch.COMPLETE: 1, batch.DEFERRED: 1, batch.ACTIVE: 1})
        self.assertEqual(loaded["hash_verified"], 2)
        self.assertEqual(loaded["entries"][2]["resume_note"], "已转写待校稿")
        self.assertEqual(loaded["entries"][0]["publication"]["entry"], published["entry"])
        self.assertEqual(self.command("finish", self.state, 1, package)["status"], batch.COMPLETE)
        self.assertEqual({path.name: sha(path) for path in root.glob("*.json")}, saved)

        self.state = self.root / "延后历史复验.json"
        deferred = self.initialize("--legacy-directory", root, "--supplemented", 1, "--defer-history")
        self.assertEqual(deferred["counts"], {batch.HISTORICAL: 1, batch.DEFERRED: 1, batch.ACTIVE: 1})
        self.assertEqual(deferred["hash_verified"], 1)
        self.assertEqual(deferred["next"], 3)
        self.assertEqual(self.command("finish", self.state, 1, package)["status"], batch.COMPLETE)


if __name__ == "__main__":
    unittest.main()
