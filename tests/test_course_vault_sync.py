"""Check course installation and the reader catalog in temporary Obsidian vaults."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_tools/course_production"))

from project import ENTRY, CourseProject, named_entry, sha, write
import vault_delivery
from vault_catalog import CATALOG, END, START, refresh_catalog


class CourseVaultSyncTest(unittest.TestCase):
    image = Path("课件/原图/操作图.png")
    original = Path("原始资料/说明.txt")

    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.vault = self.root / "日常阅读库"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.catalog = self.vault / CATALOG

    def course(self, name):
        source = self.root / "原件" / name
        write(source / "说明.txt", f"{name}的原始说明。\n")
        project = CourseProject.initialize(source, self.root / "课程包" / name)
        picture = project.folder / self.image
        picture.parent.mkdir(parents=True)
        picture.write_bytes(b"fixture image bytes used for publication hash checks")
        return project, self.edition(project, "发布稿")

    def edition(self, project, heading):
        body = (f"# {heading}\n\n## 实操\n\n"
                "![原图](原图/操作图.png)\n\n[原始说明](../原始资料/说明.txt)\n")
        write(project.entry, body)
        return {"body": body, "outputs": {ENTRY.as_posix(): sha(project.entry),
                                          self.image.as_posix(): sha(project.folder / self.image)}}

    def assert_present_in_catalog(self, name):
        page = unquote(self.catalog.read_text(encoding="utf-8"))
        self.assertIn(f"{name}/课件/{name}", page)

    def test_first_install_is_visible_only_as_a_complete_course(self):
        name = "第2项 图文练习"
        project, publication = self.course(name)
        target = self.vault / name
        copied = []
        copy = vault_delivery.copy_file

        def observe_copy(source, destination, digest):
            self.assertFalse(target.exists(), "a course folder became visible while preparing files")
            self.assertFalse(destination.is_relative_to(self.vault))
            copy(source, destination, digest)
            self.assertFalse(target.exists(), "a partially populated course folder was visible")
            copied.append(source)

        with patch.object(vault_delivery, "copy_file", side_effect=observe_copy):
            state = vault_delivery.install(project, publication, self.vault, name)

        self.assertEqual(set(copied), {project.entry, project.folder / self.image, project.folder / self.original})
        self.assertEqual(Path(state["entry"]), named_entry(target))
        self.assertEqual(vault_delivery.verify_installed(project, publication, state), named_entry(target))
        self.assert_present_in_catalog(name)

    def test_failed_copy_keeps_partial_course_out_of_the_vault_and_catalog(self):
        existing, current = self.course("已发布的项目")
        vault_delivery.install(existing, current, self.vault)
        before = self.catalog.read_bytes()
        name = "没完整拷完的课程"
        project, publication = self.course(name)
        copy = vault_delivery.copy_file

        def fail_after_creating_a_partial_file(source, destination, digest):
            if destination.name == self.image.name:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"truncated")
                raise OSError("interrupted fixture copy")
            copy(source, destination, digest)

        with patch.object(vault_delivery, "copy_file", side_effect=fail_after_creating_a_partial_file):
            with self.assertRaisesRegex(OSError, "interrupted fixture copy"):
                vault_delivery.install(project, publication, self.vault)

        self.assertFalse((self.vault / name).exists())
        self.assertIsNone(vault_delivery.delivery(project))
        self.assertEqual(self.catalog.read_bytes(), before)
        self.assertNotIn(name, unquote(self.catalog.read_text(encoding="utf-8")))

        state = vault_delivery.install(project, publication, self.vault)
        self.assertEqual(vault_delivery.verify_installed(project, publication, state), named_entry(self.vault / name))
        self.assert_present_in_catalog(name)

    def test_changed_source_hash_does_not_publish_the_course(self):
        refresh_catalog(self.vault)
        before = self.catalog.read_bytes()
        project, publication = self.course("原件发生变化的项目")
        (project.folder / self.image).write_bytes(b"different from the approved edition")

        with self.assertRaisesRegex(ValueError, "复制期间原件发生变化"):
            vault_delivery.install(project, publication, self.vault)

        self.assertFalse((self.vault / project.folder.name).exists())
        self.assertIsNone(vault_delivery.delivery(project))
        self.assertEqual(self.catalog.read_bytes(), before)

    def test_catalog_updates_and_preserves_prose_outside_managed_section(self):
        first, publication = self.course("第2项 原件核验")
        vault_delivery.install(first, publication, self.vault)
        self.assert_present_in_catalog(first.folder.name)
        start, end = START.encode("utf-8"), END.encode("utf-8")
        original = self.catalog.read_bytes().replace(b"\n", b"\r\n")
        _, remainder = original.split(start, 1)
        managed, _ = remainder.split(end, 1)
        prefix = "\ufeff# 项目总览\r\n\r\n这份目录也用于我的学习记录。\r\n\r\n".encode("utf-8")
        suffix = "\r\n\r\n## 本地学习计划\r\n\r\n下周复查第 3 讲。".encode("utf-8")
        self.catalog.write_bytes(prefix + start + managed + end + suffix)

        second, other = self.course("第10项 纯文档项目")
        vault_delivery.install(second, other, self.vault)
        for project in (first, second):
            self.assert_present_in_catalog(project.folder.name)
        updated = self.catalog.read_bytes()
        self.assertEqual(updated.split(start, 1)[0], prefix)
        self.assertEqual(updated.split(end, 1)[1], suffix)

        revised = self.edition(first, "修订稿")
        self.assertEqual(vault_delivery.install(first, revised)["entry"], str(named_entry(self.vault / first.folder.name)))
        revised_catalog = self.catalog.read_bytes()
        self.assertEqual(revised_catalog.split(start, 1)[0], prefix)
        self.assertEqual(revised_catalog.split(end, 1)[1], suffix)
        for project in (first, second):
            self.assert_present_in_catalog(project.folder.name)
        self.assertEqual(refresh_catalog(self.vault), self.catalog)
        self.assertEqual(self.catalog.read_bytes(), revised_catalog)

    def test_user_owned_catalog_stays_untouched_when_a_course_is_installed(self):
        note = "# 我自己写的总览\r\n\r\n跟做计划与读书笔记。\r\n".encode("utf-8")
        self.catalog.write_bytes(note)
        project, publication = self.course("单独交付的项目")

        state = vault_delivery.install(project, publication, self.vault)

        self.assertEqual(vault_delivery.verify_installed(project, publication, state), named_entry(self.vault / project.folder.name))
        self.assertEqual(refresh_catalog(self.vault), self.catalog)
        self.assertEqual(self.catalog.read_bytes(), note)

    def test_catalog_requires_the_current_named_entry_and_preserves_old_delivery_path(self):
        deep = self.vault / "收纳" / "深层同名项目"
        write(named_entry(deep), "# 不是一级项目\n")
        write(self.vault / "草稿" / "课件/普通笔记.md", "# 未发布\n")
        name = "已使用的旧课程目录"
        target = self.vault / name
        old_entry = target / ENTRY
        write(old_entry, "# 已有旧教材\n")
        old_sha = sha(old_entry)
        project, publication = self.course("课程包名称和库目录不同")

        self.assertEqual(refresh_catalog(self.vault), self.catalog)
        before = unquote(self.catalog.read_text(encoding="utf-8"))
        for excluded in (deep.name, "草稿", name):
            self.assertNotIn(excluded, before)
        self.assertEqual(sha(old_entry), old_sha)

        state = vault_delivery.install(project, publication, self.vault, name, reviewed_previous=old_sha)

        self.assertEqual(Path(state["entry"]), named_entry(target))
        self.assertFalse(old_entry.exists())
        self.assertEqual(sha(project.ledger / "obsidian-previous" / old_sha / ENTRY.name), old_sha)
        self.assert_present_in_catalog(name)


if __name__ == "__main__":
    unittest.main()
