"""Persistent browser requests and the separate local operator confirmation."""

from argparse import ArgumentParser
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile

from catalog_model import compact, digest, now
from confirm_gate import fresh, ordered_items_sha256
from v4_exchange import OPAQUE, selection_sha256


REQUESTS = "_exchange/control/transfer-requests/"
TASKS = "_exchange/control/tasks/"
DECISIONS = "_exchange/control/decisions/"
RECEIPTS = "_exchange/dsh/receipts/"
RANGES = "_exchange/dsh/catalog/"
REQUEST_ID = re.compile(r"transfer-[a-f0-9]{40}\Z")
MAX_BATCH = 4
IDENTITY = ("resource_version_id", "site_id", "article_id", "article_revision")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def payload(record):
    return compact(record) + b"\n"


def reference(relative, raw):
    return {"path": relative, "sha256": sha256(raw).hexdigest()}


def protocol_path(workspace, prefix, record_id):
    require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}", record_id or "") is not None,
            "记录名称无效。")
    require(not re.fullmatch(r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]", record_id, re.I), "记录名称无效。")
    root = Path(workspace).resolve(strict=True)
    for name in Path(prefix).parts:
        root /= name
        require(not root.is_symlink() and not getattr(root, "is_junction", lambda: False)(), "交接目录不能使用链接。")
    path = root / (record_id + ".json")
    require(not path.is_symlink() and not getattr(path, "is_junction", lambda: False)() and path.resolve() == path,
            "交接记录位置不正确。")
    return path


def read_record(workspace, prefix, record_id, *, missing=False):
    relative = prefix + record_id + ".json"
    path = protocol_path(workspace, prefix, record_id)
    if missing and not path.exists():
        return None, None
    require(path.is_file(), "所需交接回执还未到。")
    with path.open("rb") as stream:
        raw = stream.read(65_537)
    require(len(raw) <= 65_536, "交接记录超过大小限制。")
    record = json.loads(raw)
    require(type(record) is dict, "交接记录格式不正确。")
    return record, reference(relative, raw)


def publish_record(workspace, prefix, record_id, record):
    """Install a complete record in one operation, leaving any earlier bytes intact."""
    path = protocol_path(workspace, prefix, record_id)
    raw = payload(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    protocol_path(workspace, prefix, record_id)
    with NamedTemporaryFile(dir=path.parent, prefix=".transfer-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.link(temporary, path)
        except FileExistsError:
            previous, found = read_record(workspace, prefix, record_id)
            require(previous == record and found == reference(prefix + path.name, raw),
                    "记录已有不同版本，请先核对已有交接。")
        finally:
            temporary.unlink(missing_ok=True)
    return reference(prefix + path.name, raw)


def base_record(record_type, identifier, state, stamp, *, allowed=False):
    return {"schema_version": 1, "record_type": record_type, "producer": "codex-control",
            "record_id": identifier, "state": state, "observed_at": stamp, "revision": 1,
            "execution_allowed": allowed}


def task_record(task_id, stamp, inputs, *, session=None, authorization=None):
    return {
        **base_record("control_task", task_id, "ready", stamp, allowed=authorization is not None),
        "plan_version": "V4", "task_id": task_id, "target_role": "dedicated_dsh",
        "receipt_path": f"{RECEIPTS}{task_id}.json",
        **({"target_session_id": session} if session else {}),
        "authorization_ref": authorization, "inputs": inputs,
    }


def request_status(workspace, request_id):
    require(REQUEST_ID.fullmatch(request_id or "") is not None, "请求编号无效。")
    request, request_ref = read_record(workspace, REQUESTS, request_id)
    scope_id = request_id + "-scope"
    task, task_ref = read_record(workspace, TASKS, request_id, missing=True)
    state = "await_dsh_scope"
    if task and task.get("execution_allowed") is True and (task.get("inputs") or {}).get("transfer_request_ref") == request_ref:
        receipt, _ = read_record(workspace, RECEIPTS, request_id, missing=True)
        if (receipt and receipt.get("producer") == "dsh" and receipt.get("record_type") == "dsh_receipt"
                and receipt.get("task_id") == request_id and receipt.get("task_sha256") == task_ref["sha256"]
                and receipt.get("session_id") == task.get("target_session_id")):
            state = "execution_response_received"
        else:
            state = ("authorized_for_dsh" if fresh(task, 1800, datetime.now(timezone.utc))
                     else "authorization_expired")
    else:
        receipt, _ = read_record(workspace, RECEIPTS, scope_id, missing=True)
        if receipt:
            _, scope_ref = read_record(workspace, TASKS, scope_id)
            proof = (receipt.get("result") or {}).get("dedicated_transfer_readback") or {}
            manifests = proof.get("range_manifests")
            ready = (receipt.get("state") == "ready_for_review" and receipt.get("producer") == "dsh"
                     and receipt.get("task_id") == scope_id and receipt.get("task_sha256") == scope_ref["sha256"]
                     and receipt.get("execution_allowed") is False and proof.get("transfer_request_ref") == request_ref
                     and isinstance(manifests, list) and len(manifests) == len(request["items"]))
            state = "scope_response_received" if ready else "scope_needs_followup"
    return {"request_id": request_id, "state": state, "item_count": len(request["items"])}


def submit_request(workspace, review, snapshot_id):
    """The website can freeze intent and ask for evidence. It cannot attest a user."""
    items = review["items"]
    require(1 <= len(items) <= MAX_BATCH, "每次最多核对 4 项转存资料。")
    require(all(all(isinstance(row.get(name), str) and OPAQUE.fullmatch(row[name])
                    for name in IDENTITY) for row in items), "请先核对资料的版本和发布来源。")
    from workspace_paths import cloud_destination

    receive_target = cloud_destination(workspace)
    request_id = "transfer-" + digest([review["event_id"], review["selection_sha256"], snapshot_id, receive_target])[:40]
    stamp = review["created_at"]
    data = {
        **base_record("transfer_request", request_id, "await_dsh_scope", stamp),
        "snapshot_id": snapshot_id, "draft_revision": review["revision"], "review_id": review["event_id"],
        "selection_sha256": review["selection_sha256"], "order_mode": review["order_mode"], "receive_target": receive_target,
        "items": [{name: row[name] for name in (*IDENTITY, "source_id", "source_revision", "queue_item_id", "item_rank")}
                  for row in items],
    }
    ref = publish_record(workspace, REQUESTS, request_id, data)
    scope_id = request_id + "-scope"
    poll = task_record(scope_id, stamp, {"kind": "dedicated_transfer_readback", "transfer_request_ref": ref})
    publish_record(workspace, TASKS, scope_id, poll)
    return request_status(workspace, request_id)


def checked_selection(workspace, request_id, current_review, library, checked_scope, policy, *, receive_target):
    """Bind each real recursive DSH manifest to the same version the website displayed."""
    checked = request_status(workspace, request_id)
    request, ref = read_record(workspace, REQUESTS, checked["request_id"])
    require(request["review_id"] == current_review["event_id"]
            and request["selection_sha256"] == current_review["selection_sha256"]
            and request["snapshot_id"] == library.snapshot_id,
            "网页资料或顺序已变化，请重新核对当前清单。")
    receipt_ref, proof = checked_scope
    require(proof.get("transfer_request_ref") == ref and proof.get("target") == receive_target
            and request["receive_target"] == receive_target,
            "专用 DSH 的核对回执与当前请求或接收目录不符。")
    manifests = proof.get("range_manifests")
    require(isinstance(manifests, list) and len(manifests) == len(request["items"]),
            "专用 DSH 需要核对每项递归资料范围。")
    items, facts = [], []
    for row, manifest_ref in zip(request["items"], manifests, strict=True):
        manifest, _ = policy.pin(manifest_ref, RANGES, root=workspace)
        item = {name: row[name] for name in (*IDENTITY, "queue_item_id", "item_rank")}
        source = manifest.get("source_id")
        require(isinstance(source, str) and OPAQUE.fullmatch(source)
                and (not row["source_id"] or row["source_id"] == source),
                "资料分享身份与网页版本不一致。")
        require(all(item[name] == manifest.get(name) for name in IDENTITY),
                "资料范围回执的来源或版本已变化。")
        item.update(source_id=source, source_revision=manifest.get("source_revision"),
                    range_manifest_ref=manifest_ref, transfer_task_id=request_id + "-" + str(row["item_rank"]),
                    receive_target=receive_target)
        fact = policy.range_facts(item, manifest)
        require(fact["destination"] == receive_target, "资料的接收目录与核对结果不一致。")
        items.append(item)
        facts.append(fact)
    require(len({item["source_id"] for item in items}) == len(items), "同一分享只能确认一次。")
    capacity = proof["capacity"]
    total = sum(row["bytes"] for row in facts)
    capacity_floor = capacity.get("minimum_free_bytes")
    require(type(capacity_floor) is int and capacity_floor > 0
            and type(capacity.get("remaining_bytes")) is int
            and capacity["remaining_bytes"] - total > capacity_floor, "核对后的容量不足。")
    return {
        "request": request, "request_ref": ref, "receipt_ref": receipt_ref, "items": items,
        "target": receive_target, "total_bytes": total, "facts": facts,
    }


def publish_authorized(workspace, request_id, selection, *, session, intake):
    """Only the operator entry calls this after a current verified scope and explicit input."""
    request_ref = selection["request_ref"]
    frozen_id = request_id + "-frozen"
    decision_id = request_id + "-approved"
    prior, _ = read_record(workspace, DECISIONS, decision_id, missing=True)
    stamp = prior["confirmed_at"] if prior else now()
    items = selection["items"]
    config = {"batch_id": request_id, "order_mode": selection["request"]["order_mode"], "order_revision": 1,
              "selection_sha256": selection_sha256(items), "ordered_items_sha256": ordered_items_sha256(items)}
    frozen = {
        **base_record("frozen_transfer_selection", frozen_id, "frozen", selection["request"]["observed_at"]),
        **config, "items": items,
    }
    frozen_ref = publish_record(workspace, DECISIONS, frozen_id, frozen)
    decision = {
        **base_record("user_confirmation", decision_id, "confirmed", stamp, allowed=True), **config,
        "confirmation_actor": "user", "confirmed_at": stamp, "confirmation_seq": 1,
        "authorized_task_id": request_id, "operation": "one_bounded_normal_baidu_ui_transfer_batch",
        "user_message_reference": "local_operator_console:" + request_id,
        "frozen_selection_ref": frozen_ref, "transfer_request_ref": request_ref,
        "scope_receipt_ref": selection["receipt_ref"], "intake_binding": intake, "target_session_id": session,
        "receive_target": selection["target"],
    }
    decision_ref = publish_record(workspace, DECISIONS, decision_id, decision)
    task = task_record(request_id, stamp, {
        "kind": "confirmed_transfer", "frozen_selection_ref": frozen_ref, "transfer_request_ref": request_ref,
        "scope_receipt_ref": selection["receipt_ref"], "intake_binding": intake, "receive_target": selection["target"],
    }, session=session, authorization=decision_ref)
    return publish_record(workspace, TASKS, request_id, task)


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "approve"))
    parser.add_argument("request_id")
    args = parser.parse_args(argv)
    from workspace_paths import BASE

    try:
        if args.action == "status":
            print(json.dumps(request_status(BASE, args.request_id), ensure_ascii=False))
            return
        require(sys.stdin.isatty() and sys.stdout.isatty(), "正式授权请由操作者在本机控制台输入。")
        from catalog_model import open_index
        from server import Draft, ROOT
        sys.path.insert(0, str(BASE / "_tools"))
        import dsh_dedicated

        def inspect():
            status = dsh_dedicated.cutover_inspect()
            require(status["mode"] == "paused" and status["legacy_queue"] == "paused"
                    and not status["platform_hold"] and not status["coordinator_held"] and not status["dispatch_unsettled"],
                    "先核对专用执行归属和平台暂停。旧队列须继续保持暂停。")
            binding = dsh_dedicated.read_desktop_binding()
            policy = dsh_dedicated.transfer_policy()
            request, _ = read_record(BASE, REQUESTS, args.request_id)
            lib = open_index()
            review = Draft(ROOT / "data" / "draft.sqlite3", lib).freeze({
                "revision": request["draft_revision"], "order_mode": request["order_mode"]})
            receive_target = policy.receive_target(BASE)
            proof = dsh_dedicated.verified_readback(args.request_id + "-scope", policy, binding,
                                                   expected_target=receive_target)
            selection = checked_selection(BASE, args.request_id, review, lib, proof, policy, receive_target=receive_target)
            policy.review_selection(lib, selection["items"], selection["facts"], root=BASE)
            intake = status["intake"]
            relative = Path(intake["root"]).relative_to(BASE).as_posix()
            return binding["session_id"], {**intake, "root": relative}, selection

        session, intake, selection = inspect()
        print("本次转存范围")
        for item, fact in zip(selection["items"], selection["facts"], strict=True):
            print(f"{item['item_rank']}. {item['site_id']} / {item['article_id']}，{len(fact['files'])} 个文件，{fact['bytes']} 字节")
            for root in fact["roots"]:
                print(f"   {root['name']}")
        print(f"网盘收件目录 {selection['target']}，合计 {selection['total_bytes']} 字节")
        confirm = "确认转存 " + digest([selection, intake, session])[:12]
        require(input(f"同意以上范围请输入 {confirm}：") == confirm, "未收到明确确认。")
        require((session, intake, selection) == inspect(), "确认期间资料或执行环境变化，请重新核对。")
        result = publish_authorized(BASE, args.request_id, selection, session=session, intake=intake)
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
