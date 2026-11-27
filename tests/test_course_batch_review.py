"""Batch migration and future alias/supplement lifecycle in an isolated fixture."""
from argparse import ArgumentParser
from contextlib import redirect_stderr, redirect_stdout
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
from project import named_entry, put, read, sha, write


class AuditReviewTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.source = self.root / "原件"
        self.source.mkdir()
        self.vault = self.root / "日常库"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.manifest = self.root / "批次.json"
        self.audit = self.root / "审计.json"
        self.control = self.root / "控制.json"
        for i in range(1, 7):
            write(self.source / f"第{i}项" / "说明.md", f"本地实际资料 {i}")
        queue = batch.scan_batch(self.source, self.vault)
        for row in queue["projects"]:
            _, _, identity = batch.identify(queue, row)
            row.update(identity, status=batch.DEFERRED, reason="旧待补记录", last_changed="2026-11-25T00:00:00+00:00")
        item = queue["projects"][0]
        entry = named_entry(self.vault / "第一课")
        write(entry, "# 第一课正式教材\n")
        item.update(status=batch.COMPLETE, publication={"entry": str(entry), "sha256": sha(entry)})
        queue["projects"][2]["reason"] = "已核原件，非课程工具留档"
        queue["projects"][4]["reason"] = "第4项同源，沿主项目缺口待补"
        put(self.manifest, queue)
        reuse = self.control_identity(queue["projects"][1])
        reuse.update(status="同源复用", existing_entry=str(entry), existing_md_sha256=sha(entry))
        archived = self.control_identity(queue["projects"][2])
        archived.update(status="留档")
        alias = self.control_identity(queue["projects"][4])
        alias.update(status="待补原件", canonical_sequence=4)
        put(self.control, {"resolved_source_reuses": [reuse], "deferred_production_records": [archived, alias]})
        audited = [{"sequence": row["sequence"], "id": row["id"], "folder": row["name"],
                    "batch_status": row["status"], "inventory": {"files": len(row["metadata"]),
                    "bytes": sum(item["bytes"] for item in row["metadata"])}} for row in queue["projects"]]
        audited[0].update(audit_status="formal_signed", formal_evidence=item["publication"])
        audited[1].update(audit_status="existing_formal_reuse",
                          reuse_target={"formal_sequence": 1, "entry": str(entry)})
        audited[2].update(audit_status="archive_or_restricted_not_missing", audit_summary="工具留档")
        for row in audited[3:]:
            row.update(audit_status="waiting_originals_or_evidence", waiting_group="C_尚待实际原件",
                       gap_summary="尚缺原始材料", candidate_next_step="复核现有来件",
                       ready_for_formal_claim=False)
        audited[4].update(waiting_group="B_同源共享待补断点", related_sequence=4)
        put(self.audit, {"schema": 1, "source_root": str(self.source),
                         "evidence_snapshot": {"ledger_sha256": sha(self.manifest),
                                               "production_control": str(self.control),
                                               "production_control_sha256": sha(self.control)},
                         "rows": audited})

    @staticmethod
    def control_identity(row):
        return {key: row[key] for key in ("sequence", "id", "source_sha256", "content_sha256")}

    def command(self, *args, success=True):
        parser = ArgumentParser()
        batch.add_arguments(parser.add_subparsers(dest="action"))
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = batch.run(parser.parse_args(["batch", *map(str, args)]))
        self.assertEqual(code == 0, success, out.getvalue() + err.getvalue())
        return json.loads(out.getvalue()) if success else err.getvalue()

    def test_full_snapshot_migration_and_once_only_status(self):
        before = self.manifest.read_bytes()
        plan = self.command("audit-migrate", self.manifest, self.audit, "--priority", 6, 5)
        self.assertFalse(plan["applied"])
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(plan["priority_sequence"], [6, 4])
        actual = self.command("audit-migrate", self.manifest, self.audit, "--priority", 6, 5, "--apply")
        self.assertEqual(actual["counts"], {batch.COMPLETE: 1, batch.REUSED: 1, batch.ARCHIVED: 1,
                                           batch.PENDING: 2, batch.DEFERRED: 1})
        self.assertEqual(actual["related_waiting"], {"5": 4})
        self.assertEqual(actual["next"], 6)
        self.command("defer", self.manifest, 6, "复审仍只有索引，缺实际原件")
        self.assertEqual(self.command("status", self.manifest)["next"], 4)
        self.command("defer", self.manifest, 4, "复审同课依旧待补")
        self.assertIsNone(self.command("status", self.manifest)["next"])
        self.assertIn("一次性复审", self.command("reopen", self.manifest, 6, success=False))
        before_repeat = self.manifest.read_bytes()
        self.assertTrue(self.command("audit-migrate", self.manifest, self.audit, "--apply")["already_applied"])
        self.assertEqual(self.manifest.read_bytes(), before_repeat)
        linked = read(self.manifest)["projects"][4]
        self.assertEqual(linked["re_review"]["state"], "共同待补")

    def test_tampered_source_or_control_never_mutates_manifest(self):
        before = self.manifest.read_bytes()
        write(self.source / "第3项" / "说明.md", "changed")
        self.assertIn("已经变化", self.command("audit-migrate", self.manifest, self.audit, "--apply", success=False))
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_alias_new_content_visible_and_controlled_detach(self):
        self.command("audit-migrate", self.manifest, self.audit, "--apply")
        queue = read(self.manifest)
        alias = queue["projects"][4]
        primary = queue["projects"][3]
        new = self.source / alias["name"] / "独立新增材料.md"
        write(new, "新独立的不同课程内容")
        candidates = batch_dispatch.changed_waiting_sources(queue)
        self.assertEqual(next(row for row in candidates if row["sequence"] == 5)["added"], [new.name])
        self.assertIn("alias-review", self.command("reopen", self.manifest, 5, "--accept-changes", "新材料", success=False))
        metadata = batch.metadata(batch.source_path(queue, alias))
        _, _, identity = batch.identify(queue, {**alias, "metadata": metadata, "source_sha256": None})
        proof = self.root / "关联证据.json"
        put(proof, {"schema": 1, "kind": "alias_review", "source_id": alias["id"],
                    "source_sha256": identity["source_sha256"], "canonical_id": primary["id"],
                    "canonical_source_sha256": primary["source_sha256"], "resolution": "pending",
                    "basis": "独立隔离夹具已核新增了新的教学材料"})
        pending = self.command("alias-review", self.manifest, 5, "--resolution", "pending",
                               "--evidence", proof, "--reason", "先核主项目是否纳入新资料")
        self.assertTrue(pending["requires_alias_resolution"])
        self.assertEqual(pending["same_source_primary"], 4)
        marked = read(proof)
        marked["resolution"] = "separate"
        put(proof, marked)
        separated = self.command("alias-review", self.manifest, 5, "--resolution", "separate",
                                  "--evidence", proof, "--reason", "核实为独立课程")
        self.assertEqual(separated["id"], alias["id"])
        self.assertEqual(separated["status"], batch.PENDING)
        self.assertNotIn("same_source_primary", separated)
        self.assertNotIn("requires_alias_resolution", separated)

    def test_special_priority_precedes_new_arrivals_then_restores_default(self):
        self.command("audit-migrate", self.manifest, self.audit, "--priority", 6, 4, "--apply")
        queue = read(self.manifest)
        arrival = {"sequence": 99, "id": "new", "name": "新来件", "status": batch.PENDING,
                   "arrival_at": "2026-11-26T00:00:00+00:00", "ready_at": "2026-11-26T00:01:00+00:00"}
        category1 = {"category": "早期", "manifest": str(self.manifest)}
        category2 = {"category": "新", "manifest": "new"}
        bound = [(category1, queue), (category2, {"projects": [arrival]})]
        self.assertEqual(batch_dispatch.choose_next(bound)[1]["sequence"], 6)
        for seq in (6, 4):
            row = queue["projects"][seq-1]
            batch.defer(queue, row, "复审后未取得原件")
        self.assertEqual(batch_dispatch.choose_next(bound)[1]["sequence"], 99)

    def test_bound_real_migration_refreshes_but_copied_manifest_does_not(self):
        queue = read(self.manifest)
        registry = self.root / "全局.json"
        queue["dispatch_registry"] = str(registry)
        put(self.manifest, queue)
        put(registry, {"bindings": {"课程": {"manifest": str(self.manifest)}}})
        audit = read(self.audit)
        audit["evidence_snapshot"]["ledger_sha256"] = sha(self.manifest)
        put(self.audit, audit)
        original = self.manifest.read_bytes()
        clone = self.root / "批次副本.json"
        clone.write_bytes(original)
        with patch("vault_catalog.refresh_catalog") as catalog:
            self.command("audit-migrate", clone, self.audit, "--apply")
            catalog.assert_not_called()
            self.assertEqual(self.manifest.read_bytes(), original)
            self.command("audit-migrate", self.manifest, self.audit, "--apply")
            catalog.assert_called_once_with(self.vault, manifests=(self.manifest,))
            catalog.reset_mock()
            self.command("audit-migrate", self.manifest, self.audit, "--apply")
            catalog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
