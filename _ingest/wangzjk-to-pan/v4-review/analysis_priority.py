"""Separate analysis-priority drafts and explicit submissions for the local user."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3


LOCAL_ID = re.compile(r"[0-9a-f]{64}")
RESOURCE_ID = re.compile(r"rv_[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
MAX_ENTRIES = 500
TABLE = "analysis_priority"


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
    rows = (projection.get("data") or {}).get("projects", [])
    counts = {}
    for row in rows:
        key = row.get("local_project_id")
        if valid(LOCAL_ID, key):
            counts.setdefault(key, []).append(row)
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
        return {**target, "key": "local:" + key, "title": title_of(row)}
    required = {"kind", "resource_id", "site_id", "article_id"}
    if set(target) == required and target["kind"] == "article":
        resource_id, site_id, article_id = (target[field] for field in ("resource_id", "site_id", "article_id"))
        if not valid(RESOURCE_ID, resource_id) or not all(valid(IDENTIFIER, v) for v in (site_id, article_id)):
            raise AnalysisPriorityError(400, "来源项目标识无效")
        source = article_source(library, resource_id, site_id, article_id)
        if source is None:
            raise AnalysisPriorityError(422, "未能匹配当前来源项目，请重开该条资源")
        return {**target, "key": f"article:{site_id}:{article_id}",
                "article_revision": source["article_revision"], "title": title_of(source)}
    raise AnalysisPriorityError(400, "分析顺位仅接受本地项目或已识别的来源项目")


def show_entries(entries, library, rows):
    rendered = []
    for rank, item in enumerate(entries, 1):
        if item["kind"] == "local":
            row = rows.get(item["project_id"])
            state = (row.get("analysis") or {}).get("state") if row else None
            display = {"state": state or "pending_sync", "title": title_of(row) if row else item["title"]}
        else:
            source = article_source(library, item["resource_id"], item["site_id"], item["article_id"])
            state = ("awaiting_intake" if source and source["article_revision"] == item["article_revision"]
                     else "source_changed")
            display = {"state": state, "title": title_of(source) if source else item["title"]}
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
        return connection

    @staticmethod
    def _load(connection):
        row = connection.execute(f"SELECT revision,draft,submitted FROM {TABLE} WHERE singleton=1").fetchone()
        return (row[0], json.loads(row[1]), json.loads(row[2]) if row[2] else None) if row else (0, [], None)

    @staticmethod
    def _render(revision, entries, submitted, library, local):
        known = [*entries, *(submitted or {}).get("entries", [])]
        rows = project_rows(local)[1] if any(item["kind"] == "local" for item in known) else {}
        snapshot = ({**submitted, "entries": show_entries(submitted["entries"], library, rows)}
                    if submitted is not None else None)
        return {"revision": revision, "entries": show_entries(entries, library, rows), "submitted": snapshot}

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
            if body["revision"] != revision:
                raise AnalysisPriorityError(409, "分析顺位已变动，请重新读取")
            if action in ("add", "move", "remove"):
                key = entry["key"] if entry is not None else body.get("key")
                current = next((item for item in items if item["key"] == key), None)
                if action != "add" and current is None:
                    raise AnalysisPriorityError(422, "该项目当前不在分析顺位表中")
                if action == "add" and current and any(current.get(field) != entry.get(field)
                                                        for field in ("resource_id", "article_revision")):
                    raise AnalysisPriorityError(409, "资料版本已变化，请移除旧项目后重新加入")
                if action == "add" and current and "position" not in body:
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
                submitted = {"schema_version": 1, "record_type": "analysis_priority_submission",
                             "origin": "local_resource_library", "revision": revision,
                             "submitted_at": datetime.now(timezone.utc).isoformat(), "entries": items}
                submitted["event_id"] = hashlib.sha256(json_bytes(submitted)).hexdigest()
            connection.execute(f"INSERT OR REPLACE INTO {TABLE} VALUES(1,?,?,?)",
                               (revision, json_bytes(items).decode(),
                                json_bytes(submitted).decode() if submitted is not None else None))
            connection.commit()
        finally:
            connection.close()
        return self._render(revision, items, submitted, library, local)
