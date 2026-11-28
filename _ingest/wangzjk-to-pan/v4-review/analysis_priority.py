"""Separate analysis-priority drafts and explicit submissions for the local user."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from catalog_progress import exact_analysis
from catalog_semantics import article_key
from course_followups import CourseFollowups, binding_for
from production_bridge import LocalProjection, formal_review_stage


LOCAL_ID = re.compile(r"[0-9a-f]{64}")
RESOURCE_ID = re.compile(r"rv_[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
MAX_ENTRIES = 500
TABLE = "analysis_priority"
SUBMISSIONS = "analysis_priority_submissions"
SCHEMA_VERSION = 2
FINISHED_STATES = {"completed", "completed_received_scope", "same_source_reuse"}
FINISHED_MESSAGE = "已有同版教材，可直接打开"
SOURCE_FIELDS = ("source_sha256", "metadata_sha256")


class AnalysisPriorityError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.msg = message


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def valid(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def project_rows(local):
    projection = local.get()
    if projection.get("sync_state") not in ("live", "partial"):
        return projection.get("sync_state"), {}
    data = projection.get("data") or {}
    rows = data.get("projects", [])
    counts = {}
    for row in rows:
        key = row.get("local_project_id")
        if valid(LOCAL_ID, key):
            counts.setdefault(key, []).append(formal_review_stage(row, data))
    return projection.get("sync_state"), {key: group[0] for key, group in counts.items() if len(group) == 1}


def article_source(library, resource_id, site_id, article_id):
    resource = library.by_id.get(resource_id)
    if resource is None:
        return None
    sources = [source for source in resource.get("sources", [])
               if (source.get("site_id"), source.get("article_id")) == (site_id, article_id)]
    if len(sources) != 1 or not valid(IDENTIFIER, sources[0].get("article_revision")):
        return None
    return sources[0]


def title_of(row):
    return " ".join(str(row.get("title") or "").split())[:500]


def ensure_needs_analysis(row):
    status = exact_analysis(row).get("analysis_state")
    if status in FINISHED_STATES:
        raise AnalysisPriorityError(422, FINISHED_MESSAGE)
    if status in ("archive_only", "awaiting_re_review", "awaiting_acceptance"):
        raise AnalysisPriorityError(422, "该项目需要按现有记录接续处理")


def ensure_article_needs_analysis(row, local):
    if not isinstance(local, LocalProjection):
        return
    link = binding_for(local.followups.read(), row)
    if link:
        _, projects = project_rows(local)
        project = projects.get(link["local_project_id"])
        if project:
            ensure_needs_analysis(project)
    linked = local.detail(row).get("linked_project") or {}
    if (linked.get("project_id") and link and linked["project_id"] != link["local_project_id"]):
        raise AnalysisPriorityError(422, "资料归属待核实，未加入分析顺序")
    if (linked.get("analysis_state") in FINISHED_STATES
            and linked.get("textbook_integrity") in ("same_registered_bytes", "same_verified_independent_derivative")):
        raise AnalysisPriorityError(422, FINISHED_MESSAGE)


def anchor(target, library, local):
    if not isinstance(target, dict):
        raise AnalysisPriorityError(400, "请选择具体项目")
    if set(target) == {"kind", "project_id"} and target["kind"] == "local":
        key = target["project_id"]
        if not valid(LOCAL_ID, key):
            raise AnalysisPriorityError(400, "本地项目标识无效")
        state, rows = project_rows(local)
        if state != "live":
            raise AnalysisPriorityError(409, "分析进度正在同步，请稍后重试")
        row = rows.get(key)
        if row is None:
            raise AnalysisPriorityError(422, "本地项目尚未注册或身份有冲突")
        ensure_needs_analysis(row)
        try:
            source = local.analysis_anchor(key)
        except (OSError, ValueError):
            raise AnalysisPriorityError(422, "本地原件尚未就绪或已有变化，请核对后再安排") from None
        if not isinstance(source, dict) or not all(valid(LOCAL_ID, source.get(k)) for k in SOURCE_FIELDS):
            raise AnalysisPriorityError(422, "本地原件尚未就绪或已有变化，请核对后再安排")
        return {**target, **{k: source[k] for k in SOURCE_FIELDS}, "key": "local:" + key, "title": title_of(row)}
    required = {"kind", "resource_id", "site_id", "article_id"}
    if set(target) == required and target["kind"] == "article":
        resource_id, site_id, article_id = (target[field] for field in ("resource_id", "site_id", "article_id"))
        if not valid(RESOURCE_ID, resource_id) or not all(valid(IDENTIFIER, v) for v in (site_id, article_id)):
            raise AnalysisPriorityError(400, "来源项目标识无效")
        source = article_source(library, resource_id, site_id, article_id)
        if hasattr(library, "semantic_rows"):
            exact = library.exact_article(resource_id, article_key(site_id, article_id))
            if exact is None or exact.get('action_pending'):
                raise AnalysisPriorityError(422, "资料归属待核实，未加入分析顺序")
            ensure_article_needs_analysis(exact, local)
        if source is None:
            raise AnalysisPriorityError(422, "未能匹配当前来源项目，请重开该条资源")
        return {**target, "key": f"article:{site_id}:{article_id}",
                "article_revision": source["article_revision"], "title": title_of(source)}
    raise AnalysisPriorityError(400, "分析顺位仅接受本地项目或已识别的来源项目")


def show_entries(entries, library, rows, bindings, waiting=()):
    rendered = []
    for rank, item in enumerate(entries, 1):
        if item["kind"] == "local":
            row = rows.get(item["project_id"])
            state = exact_analysis(row).get("analysis_state") if row else None
            observed = (row.get("source_version") or {}).get("source_sha256") if row else None
            if observed and item.get("source_sha256") and item["source_sha256"] != observed:
                state = "source_changed"
            display = {"state": state or "pending_sync", "title": title_of(row) if row else item["title"]}
        else:
            source = article_source(library, item["resource_id"], item["site_id"], item["article_id"])
            token = article_key(item["site_id"], item["article_id"])
            current = getattr(library, "semantic_rows", {}).get(token)
            if current is None and source:
                current = {**source, "id": item["resource_id"]}
            display = {"state": "source_changed", "title": title_of(current) if current else item["title"]}
            if current and not current.get("action_pending") and current["article_revision"] == item["article_revision"]:
                binding = binding_for(bindings, current)
                anchored = binding_for(bindings, {**current, "id": item["resource_id"]})
                same_binding = binding is not None and binding == anchored
                if current["id"] == item["resource_id"] or same_binding:
                    display["state"] = "awaiting_intake"
                    if same_binding and (project := rows.get(binding["local_project_id"])):
                        display["state"] = exact_analysis(project).get("analysis_state", "pending_sync")
                    if current["id"] != item["resource_id"]:
                        display["current_resource_id"] = current["id"]
        if item["key"] in waiting and display["state"] in ("queued", "pending_sync", "awaiting_intake"):
            display["state"] = "waiting_for_readiness"
        rendered.append({**item, **display, "rank": rank})
    return rendered


class AnalysisPriority:
    def __init__(self, path):
        self.path = Path(path).absolute()
        if self.path.parent.resolve(strict=True) != self.path.parent or self.path.resolve() != self.path:
            raise ValueError("分析顺位数据库路径无效")

    def _connection(self, *, readonly):
        if self.path.resolve() != self.path or self.path.is_symlink():
            raise ValueError("分析顺位数据库路径已变化")
        if readonly:
            return sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=3)
        connection = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        connection.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} ("
                           "singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,"
                           "draft TEXT NOT NULL, submitted TEXT)")
        connection.execute(f"CREATE TABLE IF NOT EXISTS {SUBMISSIONS} ("
                           "revision INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, body TEXT NOT NULL)")
        return connection

    @staticmethod
    def _load(connection):
        row = connection.execute(f"SELECT revision,draft,submitted FROM {TABLE} WHERE singleton=1").fetchone()
        return (row[0], json.loads(row[1]), json.loads(row[2]) if row[2] else None) if row else (0, [], None)

    @staticmethod
    def _render(revision, entries, submitted, library, local):
        known = [*entries, *(submitted or {}).get("entries", [])]
        followups = getattr(local, "followups", None)
        bindings = (followups.read() if isinstance(followups, CourseFollowups)
                    and any(item["kind"] == "article" for item in known) else {})
        sync, rows = project_rows(local) if bindings or any(item["kind"] == "local" for item in known) else (None, {})
        if sync not in ("live", "partial"):
            bindings = {}
        matches = bool(submitted and submitted.get("schema_version") == SCHEMA_VERSION and submitted["entries"] == entries)
        waiting = followups.waiting_for(submitted) if isinstance(followups, CourseFollowups) and submitted else ()
        snapshot = ({**submitted, "entries": show_entries(submitted["entries"], library, rows, bindings, waiting)}
                    if submitted is not None else None)
        return {
            "revision": revision, "entries": show_entries(entries, library, rows, bindings, waiting if matches else ()),
            "submitted": snapshot, "draft_matches_submission": matches,
        }

    def read(self, library, local):
        if not self.path.is_file():
            return self._render(0, [], None, library, local)
        connection = self._connection(readonly=True)
        try:
            return self._render(*self._load(connection), library, local)
        finally:
            connection.close()

    def mutate(self, body, library, local):
        if not isinstance(body, dict) or type(body.get("revision")) is not int:
            raise AnalysisPriorityError(400, "缺少当前分析顺位版本")
        action = body.get("action")
        base = {"action", "revision"}
        shapes = {"add": base | {"target", "position"}, "move": base | {"key", "position"},
                  "remove": base | {"key"}, "submit": base}
        if not isinstance(action, str) or action not in shapes or not set(body) <= shapes[action]:
            raise AnalysisPriorityError(400, "分析顺位操作无效")
        if "position" in body and (type(body["position"]) is not int or not 1 <= body["position"] <= MAX_ENTRIES):
            raise AnalysisPriorityError(400, "指定位置无效")
        entry = anchor(body.get("target"), library, local) if action == "add" else None
        connection = self._connection(readonly=False)
        try:
            connection.execute("BEGIN IMMEDIATE")
            revision, items, submitted = self._load(connection)
            repeated = (action == "submit" and submitted and submitted.get("schema_version") == SCHEMA_VERSION
                        and submitted["entries"] == items and 0 <= body["revision"] <= revision)
            if repeated:
                connection.rollback()
                return self._render(revision, items, submitted, library, local)
            if body["revision"] != revision:
                raise AnalysisPriorityError(409, "分析顺位已变动，请重新读取")
            if action in ("add", "move", "remove"):
                key = entry["key"] if entry is not None else body.get("key")
                current = next((item for item in items if item["key"] == key), None)
                if action != "add" and current is None:
                    raise AnalysisPriorityError(422, "该项目当前不在分析顺位表中")
                if action == "add" and current and any(current.get(field) != entry.get(field)
                                                        for field in ("resource_id", "article_revision", *SOURCE_FIELDS)):
                    raise AnalysisPriorityError(409, "资料版本已变化，请移除旧项目后重新加入")
                if action == "add" and current and "position" not in body:
                    connection.rollback()
                    return self._render(revision, items, submitted, library, local)
                if action == "move" and "position" not in body:
                    raise AnalysisPriorityError(400, "移动时须指定位置")
                if current:
                    items.remove(current)
                if action != "remove":
                    if len(items) >= MAX_ENTRIES:
                        raise AnalysisPriorityError(422, "分析顺位表已满")
                    # With fewer than two entries, inserting at second means the end of the available list.
                    index = min(body.get("position", len(items) + 1) - 1, len(items))
                    items.insert(index, entry or current)
            revision += 1
            if action == "submit":
                if any(item["kind"] == "local" and not all(valid(LOCAL_ID, item.get(k)) for k in SOURCE_FIELDS)
                       for item in items):
                    raise AnalysisPriorityError(422, "请移除旧的本地项目并重新安排")
                submitted = {"schema_version": SCHEMA_VERSION, "record_type": "analysis_priority_submission",
                             "origin": "local_resource_library", "revision": revision,
                             "submitted_at": datetime.now(timezone.utc).isoformat(), "entries": items}
                submitted["event_id"] = hashlib.sha256(json_bytes(submitted)).hexdigest()
                connection.execute(f"INSERT INTO {SUBMISSIONS} VALUES(?,?,?)",
                                   (revision, submitted["event_id"], json_bytes(submitted).decode()))
            connection.execute(f"INSERT OR REPLACE INTO {TABLE} VALUES(1,?,?,?)",
                               (revision, json_bytes(items).decode(),
                                json_bytes(submitted).decode() if submitted is not None else None))
            connection.commit()
        finally:
            connection.close()
        return self._render(revision, items, submitted, library, local)
