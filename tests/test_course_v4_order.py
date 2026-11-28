"""Exercise V4 handoff through native dispatch and claim in a disposable workspace."""

from argparse import ArgumentParser
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_tools/course_production"))
import analysis_priority_order
import batch
import batch_dispatch
import handoff_order
from project import put, read


class Clock(datetime):
    current = datetime(2026, 11, 26, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz)


class V4DispatchTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.inputs = self.root / "分析目标下载地址"
        self.registry = self.root / "调度" / "分类.json"
        self.plan_file = self.root / "_exchange/dsh/plans/batch-1.json"
        self.sources = {}
        for category in ("01", "02", "03"):
            parent = self.inputs / category
            self.sources[category] = parent / "储存"
            self.sources[category].mkdir(parents=True)
            (parent / "分析结果/.obsidian").mkdir(parents=True)
        self.clock = patch.object(batch_dispatch, "datetime", Clock)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        Clock.current = datetime(2026, 11, 26, tzinfo=timezone.utc)

    def add_local(self, category, name, token):
        folder = self.sources[category] / name
        folder.mkdir()
        data = self.sample(token)
        (folder / f"{token}.pdf").write_bytes(data)

    def sample(self, token):
        return (f"%PDF-1.4\n{token}\n%%EOF\n").encode()

    def add_plan(self, tokens, mode="ready_ordered", *, revision=1, seq=1, demo=False,
                 order=None, batch_id="batch-1", batch_rank=None, site="site", authorization=True,
                 previous_root=None, reacquire=()):
        when = "2026-11-26T00:00:00+00:00"
        items = []
        for token in tokens:
            relative = f"_exchange/dsh/catalog/source-{token}.json"
            manifest = self.root / relative
            put(manifest, {
                "schema_version": 1, "record_type": "range_manifest", "source_id": f"source-{token}",
                "source_revision": 1, "scope": "recursive", "root_count": 1, "complete_recursive": True,
                "files": [{"file": f"{token}.pdf", "bytes": len(self.sample(token))}],
            })
            items.append({
                "queue_item_id": f"item-{token}", "resource_version_id": f"version-{token}",
                "site_id": site, "article_id": token if site == "wangzjk" else f"article-{token}",
                "article_revision": "revision-1",
                "source_id": f"source-{token}",
                "source_revision": 1, "range_manifest_ref": {
                    "path": relative, "sha256": handoff_order.sha_bytes(manifest.read_bytes()),
                }, "item_rank": (order or tokens).index(token) + 1, "transfer_task_id": f"transfer-{token}",
            })
            if previous_root:
                items[-1]["previous_cloud_root_id"] = previous_root
        authorization_path = f"_exchange/dsh/authorizations/confirm-{seq}-{revision}.json"
        receipt = {
            "schema_version": 1, "record_type": "user_confirmation", "record_id": f"confirm-{seq}",
            "producer": "dsh-ui", "revision": revision, "observed_at": when, "state": "confirmed",
            "execution_allowed": not demo, "demo": demo, "batch_id": batch_id,
            "confirmation_seq": seq, "confirmed_at": when, "confirmation_actor": "user",
            "order_mode": mode, "order_revision": revision,
            "selection_sha256": handoff_order.selection_sha(items),
            "ordered_items_sha256": handoff_order.ordered_items_sha(items),
            "reacquire_queue_item_ids": list(reacquire), "global_order_locked": batch_rank is not None,
        }
        if batch_rank is not None:
            receipt["batch_rank"] = batch_rank
        if authorization:
            put(self.root / authorization_path, receipt)
        ref = {"path": authorization_path, "sha256": "0" * 64}
        if authorization:
            ref["sha256"] = handoff_order.sha_bytes((self.root / authorization_path).read_bytes())
        value = {
            "schema_version": 1, "record_type": "confirmed_plan", "record_id": batch_id,
            "producer": "dsh", "revision": revision, "observed_at": when, "state": "confirmed",
            "execution_allowed": not demo, "demo": demo, "batch_id": batch_id,
            "confirmation_seq": seq, "confirmed_at": when, "authorization_ref": ref,
            "event_id": f"confirmed-{seq}-{revision}",
            "order_mode": mode, "order_revision": revision, "items": items,
            "global_order_locked": batch_rank is not None,
            "selection_sha256": handoff_order.selection_sha(items),
        }
        if batch_rank is not None:
            value["batch_rank"] = batch_rank
        put(self.plan_file.with_stem(batch_id), value)

    def dispatch(self, seconds=0, *, refresh=True):
        Clock.current += timedelta(seconds=seconds)
        return batch_dispatch.dispatch(self.inputs, self.registry, refresh=refresh, settle_seconds=10)

    def locate(self, category, name):
        path = Path(read(self.registry)["bindings"][category]["manifest"])
        queue = read(path)
        row, = [row for row in queue["projects"] if row["name"] == name]
        return path, queue, row

    def claim(self, category, name, *flags, success=True):
        manifest, _, row = self.locate(category, name)
        parser = ArgumentParser()
        batch.add_arguments(parser.add_subparsers(dest="action"))
        args = parser.parse_args(["batch", "claim", str(manifest), str(row["sequence"]),
                                  str(self.root / "教材" / name), *flags])
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = batch.run(args)
        self.assertEqual(code == 0, success, errors.getvalue())
        return json.loads(output.getvalue()) if success else errors.getvalue()

    def finish_fixture(self, category, name):
        path, queue, row = self.locate(category, name)
        row["status"] = batch.COMPLETE
        put(path, queue)

    def ready_B_and_C(self):
        self.add_local("01", "00-旧存量", "old")
        self.add_local("02", "01-先于B的同分类旧件", "old-B")
        self.add_local("02", "02-业务名和来源名不同", "B")
        self.add_local("03", "03-另一个名字", "C")
        self.dispatch()
        self.dispatch(11)

    def submit_local_priority(self, row, *, schema_version=2, anchor=None):
        entry = {"kind": "local", "key": "local:" + row["id"], "project_id": row["id"],
                 "title": row["name"], **(anchor or {})}
        body = {"schema_version": schema_version, "record_type": "analysis_priority_submission",
                "origin": "local_resource_library", "revision": 1,
                "submitted_at": Clock.current.isoformat(), "entries": [entry]}
        body["event_id"] = handoff_order.digest(body)
        path = self.root / analysis_priority_order.RELATIVE_DB
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS analysis_priority (singleton INTEGER, revision INTEGER, submitted TEXT)")
            db.execute("DELETE FROM analysis_priority")
            db.execute("INSERT INTO analysis_priority VALUES(1, 1, ?)", (json.dumps(body),))

    def test_verified_local_priority_claim_keeps_its_frozen_identity_on_retry(self):
        self.registry = self.root / analysis_priority_order.RELATIVE_REGISTRY
        self.ready_B_and_C()
        name = "02-业务名和来源名不同"
        _, queue, row = self.locate("02", name)
        with self.assertRaisesRegex(ValueError, "尚未注册"):
            analysis_priority_order.local_submission_anchor(self.root, "f" * 64)
        anchor = analysis_priority_order.local_submission_anchor(self.root, row["id"])
        self.assertEqual(anchor["source_sha256"], batch.identify(queue, row)[2]["source_sha256"])
        self.submit_local_priority(row, anchor=anchor)
        selected = self.dispatch(refresh=False)["next"]
        self.assertEqual(selected["id"], row["id"])
        self.assertEqual(selected["analysis_priority"]["source_sha256"], anchor["source_sha256"])
        claimed = self.claim("02", name)
        retry = self.claim("02", name)
        self.assertEqual(claimed["analysis_priority"], retry["analysis_priority"])
        self.assertEqual(claimed["source_sha256"], anchor["source_sha256"])
        self.finish_fixture("02", name)
        with self.assertRaisesRegex(ValueError, "尚未就绪"):
            analysis_priority_order.local_submission_anchor(self.root, row["id"])
        remaining = self.dispatch(refresh=False)
        self.assertNotEqual(remaining["next"]["id"], row["id"])
        self.assertNotIn("analysis_waiting", remaining)

    def test_stale_local_priority_does_not_claim_changed_bytes_or_block_other_work(self):
        self.registry = self.root / analysis_priority_order.RELATIVE_REGISTRY
        self.ready_B_and_C()
        name = "02-业务名和来源名不同"
        _, queue, row = self.locate("02", name)
        self.submit_local_priority(row, schema_version=1)
        self.assertEqual(self.dispatch(refresh=False)["next"]["name"], "00-旧存量")
        anchor = analysis_priority_order.local_submission_anchor(self.root, row["id"])
        self.submit_local_priority(row, anchor=anchor)
        frozen = self.dispatch(refresh=False)["next"]["analysis_priority"]
        path = self.sources["02"] / name / "B.pdf"
        previous = path.stat()
        path.write_bytes(self.sample("Z"))
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.assertEqual(batch.metadata(path.parent), row["metadata"])
        result = self.dispatch(refresh=False)
        self.assertEqual(result["next"]["name"], "00-旧存量")
        self.assertEqual(result["analysis_waiting"][0]["key"], frozen["key"])
        self.assertNotIn("analysis_readback", read(self.registry))
        self.dispatch()
        readback = read(self.registry)["analysis_readback"]
        self.assertEqual(readback["event_id"], frozen["event_id"])
        self.assertEqual(readback["revision"], frozen["revision"])
        self.assertEqual(readback["waiting"], result["analysis_waiting"])
        self.assertIsNotNone(datetime.fromisoformat(readback["observed_at"]).utcoffset())
        with self.assertRaisesRegex(ValueError, "网页提交的版本不同"):
            batch.claim(queue, row, self.root / "教材" / name, global_checked=True,
                        accepted_order={"analysis_priority": frozen})
        self.assertEqual(row["status"], batch.PENDING)

    def test_ready_ordered_active_and_native_claim_use_one_selection(self):
        self.ready_B_and_C()
        self.claim("01", "00-旧存量")
        self.add_plan(["A", "B", "C"])
        self.assertEqual(self.dispatch()["next"]["name"], "00-旧存量")
        self.assertIn("仍在处理", self.claim("02", "02-业务名和来源名不同", "--user-order-reason",
                                            "用户明确要求B", success=False))
        self.finish_fixture("01", "00-旧存量")
        self.assertEqual(self.dispatch(refresh=False)["next"]["v4_order"]["queue_item_id"], "item-B")
        claimed = self.claim("02", "02-业务名和来源名不同")
        self.assertEqual(claimed["v4_order"]["item_rank"], 2)
        self.add_local("01", "04-较晚到达的实体", "A")
        self.dispatch(1)
        self.assertEqual(self.dispatch(11)["next"]["name"], "02-业务名和来源名不同")
        self.finish_fixture("02", "02-业务名和来源名不同")
        self.assertEqual(self.dispatch(refresh=False)["next"]["v4_order"]["queue_item_id"], "item-A")
        self.claim("01", "04-较晚到达的实体")
        self.finish_fixture("01", "04-较晚到达的实体")
        self.assertEqual(self.dispatch(refresh=False)["next"]["v4_order"]["queue_item_id"], "item-C")

    def test_strict_waits_for_unmapped_first_item_without_blocking_independent_work(self):
        self.ready_B_and_C()
        self.add_plan(["A", "B", "C"], "strict")
        result = self.dispatch()
        self.assertEqual(result["next"]["name"], "00-旧存量")
        self.assertEqual(result["v4_order"]["waiting"]["queue_item_id"], "item-A")
        self.assertIn("01 第 1 项", self.claim("02", "02-业务名和来源名不同", success=False))
        self.claim("01", "00-旧存量")
        self.finish_fixture("01", "00-旧存量")
        self.claim("02", "01-先于B的同分类旧件")
        self.finish_fixture("02", "01-先于B的同分类旧件")
        self.assertIsNone(self.dispatch(refresh=False)["next"])
        self.assertIn("等待实收匹配", self.claim("02", "02-业务名和来源名不同", success=False))
        self.add_local("01", "A", "A")
        self.assertIsNone(self.dispatch(1)["next"])
        self.assertEqual(self.dispatch(11)["next"]["v4_order"]["queue_item_id"], "item-A")

    def test_rejects_demo_and_stale_keeps_last_accepted_revision(self):
        self.ready_B_and_C()
        self.add_plan(["A", "B", "C"], "strict", demo=True)
        rejected = self.dispatch()
        self.assertEqual(rejected["next"]["name"], "00-旧存量")
        self.assertEqual(len(rejected["v4_order"]["warnings"]), 1)
        self.add_plan(["A", "B", "C"], revision=2, order=["C", "B", "A"])
        self.assertEqual(self.dispatch()["next"]["v4_order"]["queue_item_id"], "item-C")
        self.add_plan(["A", "B", "C"], revision=1)
        stale = self.dispatch()
        self.assertEqual(stale["next"]["v4_order"]["queue_item_id"], "item-C")
        self.assertIn("拒绝旧修订", stale["v4_order"]["warnings"][0]["reason"])

    def test_newer_confirmation_first_and_lock_precedes_new_batch(self):
        self.ready_B_and_C()
        self.add_plan(["B"], batch_id="batch-1")
        self.add_plan(["C"], batch_id="batch-2", seq=2)
        self.assertEqual(self.dispatch()["next"]["v4_order"]["queue_item_id"], "item-C")
        self.add_plan(["B"], batch_id="batch-1", revision=2, batch_rank=1)
        self.assertEqual(self.dispatch()["next"]["v4_order"]["queue_item_id"], "item-B")

    def test_rejects_missing_or_changed_confirmation_proof(self):
        self.ready_B_and_C()
        self.add_plan(["B"], "strict", authorization=False)
        self.assertEqual(self.dispatch()["next"]["name"], "00-旧存量")
        self.add_plan(["B"], "strict")
        raw = read(self.plan_file)
        raw["authorization_ref"]["sha256"] = "f" * 64
        put(self.plan_file, raw)
        broken = self.dispatch()
        self.assertEqual(broken["next"]["name"], "00-旧存量")
        self.assertIn("摘要已变", broken["v4_order"]["warnings"][0]["reason"])
        self.add_plan(["B"], "strict")
        accepted = self.dispatch()
        self.assertEqual(accepted["next"]["v4_order"]["queue_item_id"], "item-B")

    def test_exact_historical_cleanup_blocks_without_separate_reacquisition(self):
        self.add_local("01", "与原名不同", "88")
        self.add_local("02", "普通积压", "old")
        self.dispatch()
        self.dispatch(11)
        state = self.root / "_ingest/wangzjk-to-pan/state"
        proof = state / "evidence/cleanup.json"
        put(proof, {"historical_readback": True})
        put(state / "downloaded-project-markers.json", {
            "schema": "worldanalysis-baidu-downloaded-project-markers.v1",
            "entries": [{"article_id": "88", "cloud_root_id": "1088",
                         "cloud_cleanup_state": "recycled_verified_exact_id",
                         "cloud_cleanup_result_ref": {"path": str(proof),
                                                      "sha256": handoff_order.sha_bytes(proof.read_bytes())}}],
        })
        put(state / "journal.json", {
            "schema": "wangzjk-to-pan.journal.v1",
            "operations": {"operation": {"state": "saved_root_verified", "article_ids": ["88"],
                                         "verified_rows": [{"target_id": "1088"}]}},
        })
        self.add_plan(["88"], site="wangzjk", previous_root="1088")
        held = self.dispatch()
        self.assertEqual(held["next"]["name"], "普通积压")
        self.assertIn("回收", held["v4_order"]["items"][0]["blocker"])
        self.add_plan(["88"], revision=2, site="wangzjk", previous_root="1088", reacquire=("item-88",))
        self.assertEqual(self.dispatch()["next"]["v4_order"]["queue_item_id"], "item-88")


if __name__ == "__main__":
    unittest.main()
