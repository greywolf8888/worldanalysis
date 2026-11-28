from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_tools/course_production"))

import batch
from batch_dispatch import dispatch
from batch_workers import require_worker_publication, run_workers
from project import CourseProject, put, read


class AnalysisWorkersTests(unittest.TestCase):
    def test_three_worker_packages_recovery_fence_and_single_formal_handoff(self):
        worktree = Path(__file__).resolve().parents[1]
        with TemporaryDirectory(dir=worktree) as temporary:
            workspace = Path(temporary).resolve()
            root = workspace / "分析目标下载地址"
            source = root / "01 示例课程" / "储存"
            vault = root / "01 示例课程" / "分析结果"
            source.mkdir(parents=True)
            (vault / ".obsidian").mkdir(parents=True)
            for index in range(4):
                course = source / f"{index+1:02d} 项目"
                course.mkdir()
                (course / "原件.pdf").write_bytes(f"%PDF-1.4\n目标 {index}\n%%EOF\n".encode())
            guide_only = source / "00 课程指引"
            guide_only.mkdir()
            (guide_only / "下载说明.txt").write_text("仅供核对原件入口", encoding="utf-8")
            registry = workspace / "整理结果/.course-dispatch/分类索引.json"
            manifest = registry.parent / "批次状态.json"
            queue = batch.scan_batch(source, vault)
            queue["dispatch_registry"] = str(registry)
            put(manifest, queue)
            binding = {"category": source.parent.name, "source": str(source), "vault": str(vault),
                       "manifest": str(manifest), "layout": "container"}
            put(registry, {"schema": 1, "root": str(root), "bindings": {binding["category"]: binding}})
            run_workers(registry, "configure", limit=3)

            def reserve(index):
                try:
                    return run_workers(registry, "reserve", session=f"session-{index}")
                except ValueError as error:
                    self.assertIn("席位已满", str(error))
                    return None

            with ThreadPoolExecutor(max_workers=4) as pool:
                assigned = [job for job in pool.map(reserve, range(4)) if job]
            self.assertEqual(len(assigned), 3)
            self.assertEqual(len({job["package"] for job in assigned}), 3)
            self.assertTrue(all(job["sequence"] > 1 for job in assigned))
            self.assertEqual(run_workers(registry, "status")["in_use"], 3)
            self.assertIsNone(dispatch(root, registry)["next"])
            self.assertIsNone(batch.next_row(read(manifest), manifest))
            job = min(assigned, key=lambda item: item["sequence"])
            replacement = run_workers(registry, "recover", ticket=job["ticket"], session="replacement-session")
            self.assertNotEqual(replacement["package"], job["package"])
            with self.assertRaisesRegex(ValueError, "凭据已更换"):
                run_workers(registry, "ready", ticket=job["ticket"])
            with ThreadPoolExecutor(max_workers=3) as pool:
                projects = [replacement, *(item for item in assigned if item != job)]
                completed = list(pool.map(lambda item: CourseProject.initialize(Path(item["source"]), Path(item["package"])), projects))
                self.assertEqual(len({project.folder for project in completed}), 3)
            run_workers(registry, "ready", ticket=replacement["ticket"])
            with self.assertRaisesRegex(ValueError, "正式队列接手"):
                require_worker_publication(Path(replacement["package"]), "accept")
            self.assertEqual(dispatch(root, registry)["next"]["id"], replacement["project_id"])
            parser = ArgumentParser()
            batch.add_arguments(parser.add_subparsers(dest="command"))
            command = ["batch", "claim", str(manifest), str(replacement["sequence"]), replacement["package"]]
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as failure:
                self.assertEqual(batch.run(parser.parse_args(command + ["--worker-ticket", job["ticket"]])), 2)
                self.assertIn("当前领取凭据", failure.getvalue())
                self.assertEqual(batch.run(parser.parse_args(command + ["--worker-ticket", replacement["ticket"]])), 0)
            after = read(manifest)
            self.assertEqual(sum(row["status"] == batch.ACTIVE for row in after["projects"]), 1)
            require_worker_publication(Path(replacement["package"]), "deliver", vault=vault)
            with self.assertRaisesRegex(ValueError, "正式队列接手"):
                require_worker_publication(Path(job["package"]), "deliver", vault=vault)
            with self.assertRaisesRegex(ValueError, "已登记的教材位置"):
                require_worker_publication(Path(replacement["package"]), "deliver", vault=source)
            self.assertEqual(run_workers(registry, "configure", limit=1)["in_use"], 3)
            with self.assertRaisesRegex(ValueError, "席位已满"):
                run_workers(registry, "reserve", session="next")
            released = next(item for item in assigned if item != job)
            run_workers(registry, "release", ticket=released["ticket"])
            command = ["batch", "claim", str(manifest), str(released["sequence"]), released["package"]]
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as failure:
                self.assertEqual(batch.run(parser.parse_args(command)), 2)
                self.assertIn("没有有效的分析交接", failure.getvalue())
