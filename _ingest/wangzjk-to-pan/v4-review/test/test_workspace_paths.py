from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import stat
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from workspace_paths import (
    CATALOG_CHECKPOINT_SCHEMA, CATALOG_DETAILS_SCHEMA, CLOUD_DESTINATION, INPUT, LEGACY_VERSION,
    PATH_CONFIG, REGISTRY, activate_version, cloud_destination, job_target, path_bindings,
    preflight, reader_roots, rollback_version, save_version, snapshot,
)


class WorkspacePathsTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name).resolve()
        self.registry = self.workspace / REGISTRY
        self.registry.parent.mkdir(parents=True)
        self.source = self.workspace / INPUT / "分类" / "储存"
        self.vault = self.source.parent / "分析结果"
        self.source.mkdir(parents=True)
        self.vault.mkdir()
        self.manifest = self.registry.parent / "批次.json"
        binding = {"source": str(self.source), "vault": str(self.vault)}
        self.manifest.write_text(json.dumps({**binding, "dispatch_registry": str(self.registry)}), encoding="utf8")
        self.registry.write_text(json.dumps({
            "schema": 1, "root": str(self.workspace / INPUT),
            "bindings": {"分类": {**binding, "category": "分类", "manifest": str(self.manifest)}},
        }), encoding="utf8")

    def verify(self, roles, **options):
        options.setdefault("revision", snapshot(self.workspace)["revision"])
        return preflight(roles, workspace=self.workspace, **options)

    def test_preflight_preserves_the_current_binding_and_does_not_create_a_directory(self):
        proposal = "未来收件"
        before = self.registry.read_bytes()
        initial = snapshot(self.workspace)
        self.assertEqual(initial["categories"][0]["state"], "observed")
        self.assertEqual(initial["categories"][0]["test_vault_state"], "missing")
        first = self.verify({"intake": proposal})
        self.assertEqual(first["proposal_id"], self.verify({"intake": proposal})["proposal_id"])
        self.assertFalse(first["applied"])
        self.assertFalse((self.workspace / proposal).exists())
        self.assertEqual(self.registry.read_bytes(), before)
        (self.vault.parent / "测试分析稿件").mkdir()
        with self.assertRaisesRegex(ValueError, "登记已变化"):
            self.verify({"intake": proposal}, revision=initial["revision"])

    def test_preflight_rejects_overlap_traversal_reparse_and_unavailable_capacity(self):
        for target in (self.source, self.vault.parent, self.manifest, "../超出范围"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.verify({"history_backup": str(target)})
        with self.assertRaisesRegex(ValueError, "其他拟用目录重叠"):
            self.verify({"intake": "未来", "analysis_work": "未来/子目录"})
        link = self.workspace / "链接"
        link.mkdir()
        real_lstat = Path.lstat

        def reparse_lstat(path, *args, **kwargs):
            meta = real_lstat(path, *args, **kwargs)
            if path == link:
                return SimpleNamespace(st_mode=meta.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return meta

        with patch.object(Path, "lstat", reparse_lstat), self.assertRaisesRegex(ValueError, "重解析点"):
            self.verify({"intake": "链接/新目录"})
        with patch("workspace_paths.disk_usage", return_value=SimpleNamespace(free=100)):
            with self.assertRaisesRegex(ValueError, "所需空间"):
                self.verify({"intake": "新目录"}, minimum_free_bytes=101)

    def test_saved_switch_and_rollback_only_rebind_future_work(self):
        initial = snapshot(self.workspace)
        legacy = path_bindings(workspace=self.workspace, category="分类")
        self.assertEqual(legacy["paths"]["delivery_vault"], self.vault.as_posix())
        self.assertIsNone(legacy["paths"]["history_backup"])
        proposal = {role["key"]: f'未来工作/{role["key"]}' for role in initial["proposal_roles"]}
        before = self.registry.read_bytes()
        preview = self.verify(proposal)
        args = {"workspace": self.workspace, "revision": preview["revision"], "proposal_id": preview["proposal_id"]}
        with self.assertRaisesRegex(ValueError, "预检结果不一致"):
            save_version({**proposal, "intake": "另一个收件目录"}, **args)
        candidate = save_version(proposal, **args)
        version = candidate["saved_version_id"]
        self.assertEqual(path_bindings(workspace=self.workspace), legacy)
        with self.assertRaisesRegex(ValueError, "尚未启用"):
            path_bindings(workspace=self.workspace, version_id=version)
        self.assertFalse((self.workspace / "未来工作").exists())
        with self.assertRaisesRegex(ValueError, "先准备"):
            activate_version(version, revision=candidate["revision"], workspace=self.workspace)
        for relative in proposal.values():
            (self.workspace / relative).mkdir(parents=True)
        new_source = self.workspace / proposal["intake"] / "保留原件.txt"
        new_source.write_text("用户目录中的实际文件", encoding="utf-8")
        active = activate_version(version, revision=candidate["revision"], workspace=self.workspace)
        pinned = path_bindings(workspace=self.workspace)
        self.assertEqual(pinned["version_id"], version)
        self.assertEqual(pinned["paths"], {key: (self.workspace / relative).as_posix() for key, relative in proposal.items()})
        self.assertEqual(active["path_config"]["previous_version_id"], LEGACY_VERSION)
        self.assertEqual(reader_roots(self.workspace), (self.vault, Path(pinned["paths"]["delivery_vault"])))
        backup = job_target("history_backup", workspace=self.workspace, version_id=version, name="盘点.json")
        self.assertEqual(backup, Path(pinned["paths"]["history_backup"]) / "盘点.json")
        with self.assertRaisesRegex(ValueError, "单一文件或项目名"):
            job_target("history_backup", workspace=self.workspace, name="../越界.json")
        with self.assertRaisesRegex(ValueError, "登记已变化"):
            rollback_version(revision=candidate["revision"], workspace=self.workspace)
        restored = rollback_version(revision=active["revision"], workspace=self.workspace)
        self.assertEqual(restored["path_config"]["active_version_id"], LEGACY_VERSION)
        self.assertEqual(path_bindings(workspace=self.workspace), legacy)
        self.assertEqual(path_bindings(workspace=self.workspace, version_id=version), pinned)
        self.assertEqual(reader_roots(self.workspace), (self.vault,))
        self.assertEqual(new_source.read_text(encoding="utf-8"), "用户目录中的实际文件")
        self.assertEqual(self.registry.read_bytes(), before)
        (self.workspace / proposal["history_backup"]).rmdir()
        unavailable = path_bindings(workspace=self.workspace, version_id=version)
        self.assertEqual(unavailable["unavailable_roles"], ["history_backup"])
        self.assertIsNone(unavailable["paths"]["history_backup"])
        self.assertEqual(unavailable["paths"]["intake"], pinned["paths"]["intake"])

    def test_formal_registry_blocks_incompatible_switch_before_any_write(self):
        proposal = {"intake": "备用收件", "delivery_vault": "备用教材"}
        with patch("workspace_paths.BASE", self.workspace):
            preview = self.verify(proposal)
            saved = save_version(proposal, workspace=self.workspace, revision=preview["revision"],
                                 proposal_id=preview["proposal_id"])
            info = saved["path_config"]["versions"][0]
            self.assertEqual(set(info["blocked_roles"]), set(proposal))
            before = (self.workspace / PATH_CONFIG).read_bytes()
            with self.assertRaisesRegex(ValueError, "正式批次仍使用原收件根"):
                activate_version(info["version_id"], workspace=self.workspace, revision=saved["revision"])
            self.assertEqual((self.workspace / PATH_CONFIG).read_bytes(), before)

            proposal = {"catalog_data": "备用采集"}
            preview = self.verify(proposal, revision=saved["revision"])
            saved = save_version(proposal, workspace=self.workspace, revision=preview["revision"],
                                 proposal_id=preview["proposal_id"])
            version = saved["saved_version_id"]
            self.assertIn("catalog_data", saved["path_config"]["versions"][0]["blocked_roles"])
            target = self.workspace / proposal["catalog_data"]
            target.mkdir()
            for name in ("codex-public.json", "public-details.json"):
                (target / name).write_text("{}", encoding="utf-8")
            (target / "pipeline-runs.sqlite3").write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "请先准备采集目录"):
                activate_version(version, workspace=self.workspace, revision=saved["revision"])

    def test_catalog_activation_rejects_header_only_data_and_uses_readable_copy(self):
        target = self.workspace / "候选采集目录"
        target.mkdir()
        checkpoint = target / "codex-public.json"
        details = target / "public-details.json"
        database = target / "pipeline-runs.sqlite3"
        checkpoint.write_text("{", encoding="utf-8")
        details.write_text(json.dumps({"schema": CATALOG_DETAILS_SCHEMA, "site_id": "wangzjk.com",
                                       "articles": {}}), encoding="utf-8")
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY)")
        with patch("workspace_paths.BASE", self.workspace):
            proposal = {"catalog_data": str(target)}
            preview = self.verify(proposal)
            saved = save_version(proposal, workspace=self.workspace, revision=preview["revision"],
                                 proposal_id=preview["proposal_id"])
            version = saved["saved_version_id"]
            config = self.workspace / PATH_CONFIG
            before = config.read_bytes()
            with self.assertRaisesRegex(ValueError, "无法完整读取"):
                activate_version(version, workspace=self.workspace, revision=saved["revision"])
            self.assertEqual(config.read_bytes(), before)
            checkpoint.write_text(json.dumps({"schema": CATALOG_CHECKPOINT_SCHEMA, "site": "wangzjk.com",
                                               "articles": {}, "details": {}, "history": {},
                                               "collection": {"read_only": True}}), encoding="utf-8")
            active = activate_version(version, workspace=self.workspace, revision=saved["revision"])
            self.assertEqual(path_bindings(workspace=self.workspace)["paths"]["catalog_data"], target.as_posix())
            self.assertEqual(rollback_version(workspace=self.workspace, revision=active["revision"])
                             ["path_config"]["active_version_id"], LEGACY_VERSION)

    def test_new_cloud_requests_read_one_configured_root(self):
        self.assertEqual(cloud_destination(self.workspace), CLOUD_DESTINATION)
        path = self.workspace / PATH_CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
        candidate = {"schema_version": 1, "workspace": self.workspace.as_posix(), "versions": {},
                     "switches": [], "cloud_destination": "/apps/Worldanalysis/统一收件"}
        path.write_text(json.dumps(candidate), encoding="utf-8")
        self.assertEqual(cloud_destination(self.workspace), candidate["cloud_destination"])
        self.assertEqual(snapshot(self.workspace)["cloud_destination"]["path"], candidate["cloud_destination"])
        for unsafe in ("/", "/apps//收件", "/apps/../收件", "/apps/%2e%2e/收件", "/apps/收件/"):
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(ValueError, "唯一的实际路径"):
                path.write_text(json.dumps({**candidate, "cloud_destination": unsafe}), encoding="utf-8")
                cloud_destination(self.workspace)

    def test_competing_save_and_failed_replace_preserve_consistent_versions(self):
        revision = snapshot(self.workspace)["revision"]
        barrier = Barrier(2)

        def save(target):
            proposal = {"intake": target}
            preview = self.verify(proposal, revision=revision)
            barrier.wait(timeout=5)
            try:
                return save_version(proposal, revision=revision, proposal_id=preview["proposal_id"],
                                    workspace=self.workspace)["saved_version_id"]
            except ValueError as error:
                self.assertRegex(str(error), "正在更新|登记已变化")
                return None

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(save, ("并发预案一", "并发预案二")))
        self.assertEqual(sum(version is not None for version in results), 1)
        settings = self.workspace / PATH_CONFIG
        before = settings.read_bytes()
        current = snapshot(self.workspace)
        proposal = {"analysis_work": "故障时不写入"}
        preview = self.verify(proposal, revision=current["revision"])
        with patch("workspace_paths.os.replace", side_effect=OSError("interrupted")), self.assertRaises(OSError):
            save_version(proposal, revision=preview["revision"], proposal_id=preview["proposal_id"],
                         workspace=self.workspace)
        self.assertEqual(settings.read_bytes(), before)
        self.assertEqual(snapshot(self.workspace), current)
        self.assertFalse(list(settings.parent.glob("workspace-path-*.next")))


if __name__ == "__main__":
    unittest.main()
