"""Exercise the browser request and owner authorization records without Netdisk."""

from contextlib import closing
from http.client import HTTPConnection
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace
import unittest

from catalog_model import Library, digest, source_key
from server import make_server
from test_v4 import S, adapter, resource
from transfer_requests import DECISIONS, RECEIPTS, RANGES, REQUESTS, TASKS, checked_selection, publish_authorized
from transfer_requests import publish_record, read_record, request_status


class TransferRequestsTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.lib = Library([adapter([resource(1101)], [])])
        self.lib.snapshot_id = "a" * 64
        self.local = SimpleNamespace(crawl={"global_holds": ["platform:baidu"]})
        self.http = None
        self.start_server()
        self.addCleanup(self.stop_server)
        self.site = next(iter(self.lib.by_id))
        self.assertEqual(self.request("/api/draft", {
            "action": "toggle", "id": self.site, "selected": True, "revision": 0,
        })[0], 200)
        self.input = {"revision": 1, "snapshot_id": self.lib.snapshot_id, "order_mode": "strict"}

    def start_server(self):
        self.http = make_server(self.lib, self.root / "draft.sqlite", 0, local=self.local,
                                path_workspace=self.root, enable_confirmation=True)
        self.thread = Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def stop_server(self):
        self.http.shutdown()
        self.thread.join()
        self.http.server_close()

    def request(self, path, body=None, *, origin=None, fetch_site="same-origin"):
        with closing(HTTPConnection(self.http.authority, timeout=5)) as client:
            headers = {"Origin": origin or "http://" + self.http.authority,
                       "Content-Type": "application/json", "Sec-Fetch-Site": fetch_site}
            client.request("GET" if body is None else "POST", path,
                           None if body is None else json.dumps(body), headers)
            reply = client.getresponse()
            return reply.status, json.load(reply)

    def test_browser_submits_scope_only_and_recovers_across_server_restart(self):
        readiness = self.request("/api/preconfirmation")[1]
        self.assertTrue(readiness["platform_hold_present"])
        self.assertTrue(readiness["request_available"])
        status, accepted = self.request("/api/confirm", self.input)
        self.assertEqual(status, 202)
        self.assertEqual(accepted["state"], "await_dsh_scope")
        ticket = accepted["request_id"]
        request, ref = read_record(self.root, REQUESTS, ticket)
        prep, _ = read_record(self.root, TASKS, ticket + "-scope")
        self.assertIsNone(request["items"][0]["source_id"])
        self.assertEqual(prep["inputs"]["transfer_request_ref"], ref)
        self.assertFalse(prep["execution_allowed"])
        self.assertIsNone(prep["authorization_ref"])
        self.assertFalse((self.root / DECISIONS).exists())
        self.assertFalse((self.root / TASKS / f"{ticket}.json").exists())
        self.stop_server()
        self.start_server()
        self.assertEqual(self.request("/api/confirm?request_id=" + ticket), (200, accepted))
        self.assertEqual(self.request("/api/confirm", self.input), (202, accepted))
        self.assertEqual(len(list((self.root / REQUESTS).glob("*.json"))), 1)

    def test_browsers_cannot_claim_operator_authority_or_submit_stale_choices(self):
        self.assertEqual(self.request("/api/confirm", self.input, origin="http://other.invalid")[0], 403)
        self.assertEqual(self.request("/api/confirm", self.input, fetch_site="none")[0], 403)
        self.assertEqual(self.request("/api/confirm", {**self.input, "confirmation_actor": "user"})[0], 400)
        self.assertEqual(self.request("/api/confirm", {**self.input, "revision": 0})[0], 409)
        self.assertEqual(self.request("/api/confirm", {**self.input, "snapshot_id": "b" * 64})[0], 409)
        self.assertFalse((self.root / REQUESTS).exists())

    def test_member_scope_requires_one_trusted_bound_locator(self):
        from share_locations import ShareLocation

        policy = runpy.run_path(str(Path(__file__).resolve().parents[2] / "v4-confirmed.py"))
        current_article = policy["current_article"]
        row = self.lib.by_id[self.site]
        site, aid = row["site_id"], row["article_id"]
        item = {"site_id": site, "article_id": aid, "receive_target": "/allowed"}
        article = {"article_id": aid, "url": row["url"], "shares": [], "access_state": "unknown"}
        bound = ShareLocation(S, ("owner_export",), "test")
        resolved = current_article({aid: article}, {(site, aid): (bound,)}, item)
        self.assertEqual(resolved["shares"], [{"url": S, "code": "test"}])
        with self.assertRaisesRegex(ValueError, "EXACT_AUTHORIZED_PUBLISHER_ENTRY_REQUIRED"):
            current_article({aid: article}, {(site, aid): (bound, bound)}, item)
        with self.assertRaisesRegex(ValueError, "EXACT_AUTHORIZED_PUBLISHER_ENTRY_REQUIRED"):
            current_article({aid: {**article, "access_state": "needs_login"}}, {(site, aid): (bound,)}, item)

    def test_verified_scope_places_exact_existing_executor_task(self):
        _, accepted = self.request("/api/confirm", self.input)
        ticket = accepted["request_id"]
        request, request_ref = read_record(self.root, REQUESTS, ticket)
        scope, scope_ref = read_record(self.root, TASKS, ticket + "-scope")
        policy_module = runpy.run_path(str(Path(__file__).resolve().parents[2] / "v4-confirmed.py"))
        policy = SimpleNamespace(**{name: policy_module[name] for name in ("pin", "range_facts", "build_artifacts")})
        roots = [{"source_id": "sample-dir", "name": "项目资料", "is_dir": True, "size": 0}]
        directories = ["项目资料"]
        files = [{"file": "项目资料/讲义.pdf", "bytes": 12}]
        target = request["receive_target"]
        identity = {key: request["items"][0][key] for key in (
            "resource_version_id", "site_id", "article_id", "article_revision")}
        manifest = {
            **identity, "schema_version": 1, "record_type": "range_manifest", "scope": "recursive",
            "complete_recursive": True, "root_count": 1, "roots": roots, "directories": directories,
            "files": files, "share_sha256": "c" * 64, "destination": target,
            "source_id": "src_" + source_key(S), "source_revision": "sr_" + digest([roots, directories, files]),
            "recursive_evidence": "normal_ui_exact_directory_listing",
        }
        manifest_ref = publish_record(self.root, RANGES, "scope-manifest", manifest)
        proof = {"transfer_request_ref": request_ref, "target": target, "range_manifests": [manifest_ref],
                 "capacity": {"minimum_free_bytes": 100, "remaining_bytes": 1000}}
        session = "session-11111111-1111-4111-8111-111111111111"
        receipt_ref = publish_record(self.root, RECEIPTS, scope["task_id"], {
            "producer": "dsh", "task_sha256": scope_ref["sha256"], "task_id": scope["task_id"],
            "record_type": "dsh_receipt", "session_id": session, "state": "ready_for_review",
            "execution_allowed": False,
            "result": {"dedicated_transfer_readback": proof},
        })
        self.assertEqual(request_status(self.root, ticket)["state"], "scope_response_received")
        review = self.http.snapshot.get()[1].freeze({"revision": 1, "order_mode": "strict"})
        with self.assertRaisesRegex(ValueError, "当前请求或接收目录"):
            checked_selection(self.root, ticket, review, self.lib, (receipt_ref, proof), policy,
                              receive_target=target + "/错误")
        selection = checked_selection(self.root, ticket, review, self.lib, (receipt_ref, proof),
                                      policy, receive_target=target)
        kwargs = {"session": session, "intake": {"root": str(self.root), "version_id": "test-version"}}
        official = publish_authorized(self.root, ticket, selection, **kwargs)
        self.assertEqual(publish_authorized(self.root, ticket, selection, **kwargs), official)
        plan, authorization, _, scope_facts = policy.build_artifacts(official, root=self.root, expected_session=session)
        self.assertEqual(plan["items"][0]["receive_target"], target)
        self.assertEqual(scope_facts[0]["bytes"], 12)
        workspace = Path(__file__).resolve().parents[4]
        receiver = runpy.run_path(str(workspace / "_tools/course_production/handoff_order.py"))
        auth_path = Path(plan["authorization_ref"]["path"])
        publish_record(self.root, auth_path.parent.as_posix() + "/", auth_path.stem, authorization)
        self.assertEqual(receiver["check_confirmation"](self.root, plan), set())
        self.assertEqual(request_status(self.root, ticket)["state"], "authorized_for_dsh")
        (self.root / RECEIPTS / (scope["task_id"] + ".json")).write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "REFERENCE_BYTES_CHANGED"):
            policy.build_artifacts(official, root=self.root, expected_session=session)
        with self.assertRaisesRegex(ValueError, "摘要已变"):
            receiver["check_confirmation"](self.root, plan)


if __name__ == "__main__":
    unittest.main()
