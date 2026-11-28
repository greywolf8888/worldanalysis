"""Bind a local draft to its originals and read the latest submitted order."""
from argparse import ArgumentParser
from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

import handoff_order as handoff


RELATIVE_DB = "_ingest/wangzjk-to-pan/v4-review/data/analysis-priority.sqlite3"
RELATIVE_REGISTRY = "整理结果/.course-dispatch/分类索引.json"
ID = re.compile(r"[0-9a-f]{64}")
RESOURCE = re.compile(r"rv_[0-9a-f]{64}")
KEY_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _valid(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _prepare_record(revision, data):
    if not data or len(data.encode("utf-8")) > 1_000_000:
        return None
    record = json.loads(data)
    if (set(record) != {"schema_version", "record_type", "origin", "revision", "submitted_at", "entries", "event_id"}
            or type(record["schema_version"]) is not int or record["schema_version"] not in (1, 2)
            or record["record_type"] != "analysis_priority_submission"
            or record["origin"] != "local_resource_library" or type(record["revision"]) is not int
            or not 1 <= record["revision"] <= revision
            or not datetime.fromisoformat(record["submitted_at"]).tzinfo
            or not isinstance(record["entries"], list) or len(record["entries"]) > 500):
        return None
    body = {key: value for key, value in record.items() if key != "event_id"}
    frozen = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if hashlib.sha256(frozen).hexdigest() != record["event_id"]:
        return None
    seen = set()
    for item in record["entries"]:
        kind = item["kind"]
        if kind == "local" and _valid(ID, item.get("project_id")):
            if record["schema_version"] == 2 and not all(
                    _valid(ID, item.get(name)) for name in ("source_sha256", "metadata_sha256")):
                return None
            key = "local:" + item["project_id"]
        elif (kind == "article" and _valid(RESOURCE, item.get("resource_id"))
              and all(_valid(KEY_PART, item.get(field)) for field in ("site_id", "article_id", "article_revision"))):
            key = f"article:{item['site_id']}:{item['article_id']}"
        else:
            return None
        if item.get("key") != key or key in seen:
            return None
        seen.add(key)
    return record


def submitted_order(workspace):
    """Observe only submitted rows. A missing or unreadable board leaves the normal queue available."""
    workspace = Path(workspace)
    filename = workspace / RELATIVE_DB
    connection = None
    try:
        if not filename.is_file() or filename.resolve(strict=True) != filename or filename.is_symlink():
            return None
        connection = sqlite3.connect(filename.as_uri() + "?mode=ro", uri=True, timeout=1)
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute("SELECT revision,submitted FROM analysis_priority WHERE singleton=1").fetchone()
        return _prepare_record(*row) if row else None
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError):
        return None
    finally:
        if connection is not None:
            connection.close()


def local_submission_anchor(workspace, project_id):
    """Freeze an exact source snapshot for a uniquely registered local project."""
    import batch
    from batch_dispatch import local_priority_identity, registry_data

    if not _valid(ID, project_id):
        raise ValueError("本地项目身份无效")
    workspace = Path(workspace).resolve(strict=True)
    root = handoff.workspace_file(workspace, "分析目标下载地址")
    registry = handoff.workspace_file(workspace, RELATIVE_REGISTRY)
    info = registry_data(root, registry)
    matches = []
    for binding in info["bindings"].values():
        manifest = Path(binding["manifest"])
        if not manifest.is_relative_to(workspace / "整理结果"):
            raise ValueError("分类批次清单不在本地工作区")
        manifest = handoff.workspace_file(workspace, manifest.relative_to(workspace).as_posix())
        if not manifest.is_file():
            continue
        queue = batch.open_queue(manifest)
        if queue.get("dispatch_registry") != str(registry) or queue["source"] != binding["source"]:
            raise ValueError("分类批次与正式调度的身份不一致")
        matches.extend((binding, row) for row in queue["projects"] if row["id"] == project_id)
    if len(matches) != 1:
        raise ValueError("本地项目尚未注册或身份有冲突")
    return local_priority_identity(workspace, *matches[0])


def resolved_priorities(submitted, pairs, planned, local_ready, workspace=None, *, local_identity=None,
                        rejected_local=None, first_only=False):
    if not submitted:
        return {}
    by_project = defaultdict(list)
    by_source = defaultdict(list)
    for binding, row in pairs:
        by_project[row["id"]].append((binding, row))
    requested = {(item["site_id"], item["article_id"], item["article_revision"])
                 for item in submitted["entries"] if item["kind"] == "article"}
    for candidate in planned:
        if candidate.ready and candidate.row is not None:
            item = candidate.item
            identity = (item.get("site_id"), item.get("article_id"), item.get("article_revision"))
            if identity in requested:
                manifest = None
                if workspace is not None and candidate.plan.get("confirmation_task_ref"):
                    try:
                        handoff.load_range(workspace, item, local_shapes=True, require_dsh=True)
                        ref = item["range_manifest_ref"]
                        manifest = handoff.read_json(handoff.workspace_file(workspace, ref["path"]), ref)
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                for key in handoff.priority_article_keys(item, manifest):
                    by_source[key].append(candidate.row["id"])
    results = {}
    for rank, item in enumerate(submitted["entries"], 1):
        mode = item["kind"]
        if mode == "local":
            # Version 1 described a folder identity, without anchoring its bytes.
            if submitted.get("schema_version") != 2 or local_identity is None:
                continue
            project = item["project_id"]
        else:
            key = tuple(item[field] for field in ("resource_id", "site_id", "article_id", "article_revision"))
            matches = set(by_source[key])
            if len(matches) != 1:
                continue
            project = matches.pop()
        pair = by_project.get(project, ())
        if len(pair) != 1 or project in results:
            continue
        anchor = {}
        if mode == "local":
            anchor = {field: item[field] for field in ("source_sha256", "metadata_sha256")}
            matches = local_identity(*pair[0], anchor)
            if not matches:
                if matches is False and rejected_local is not None:
                    rejected_local[project] = {"key": item["key"], "rank": rank,
                                               "reason": "原件尚未就绪或与提交的版本不同"}
                continue
        elif not local_ready(*pair[0]):
            continue
        results[project] = {"rank": rank, "revision": submitted["revision"], "event_id": submitted["event_id"],
                            "key": item["key"], **anchor}
        if first_only:
            break
    return results


def main(argv=None):
    parser = ArgumentParser(description="核对并绑定明确选择的本地分析原件")
    parser.add_argument("--anchor", required=True, metavar="项目身份")
    args = parser.parse_args(argv)
    checkout = Path(__file__).resolve().parents[2]
    workspace = checkout.parent.parent if checkout.parent.name == ".runtime" else checkout
    try:
        print(json.dumps(local_submission_anchor(workspace, args.anchor), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
