"""Maintain the reading index from the notes actually present in the Obsidian vault."""
from os.path import relpath
from pathlib import Path
from collections import Counter
import re
from urllib.parse import quote

from project import file_lock, local_child, named_entry, put, read, sha, write
from vault_dates import (ACCEPTED_BATCH_STATES, DATE_INDEX, HISTORICAL_STATUS, UNBOUND_STATUS,
                         UNCONFIRMED_STATUS, current_scope, from_batch, from_current_delivery,
                         load_dates, merge_dates, production_updates)


LEGACY_CATALOG = "00 项目总览.md"
CATALOG = Path("00 入口") / LEGACY_CATALOG
START = "<!-- Worldanalysis:course-catalog -->"
END = "<!-- /Worldanalysis:course-catalog -->"
HEADING = "# 项目总览"
CATALOG_SOURCES = Path(".obsidian/worldanalysis-catalog-sources.json")
SCOPE_STATUSES = ("完整验收", "配套待补", "核心待补")
READING_STATUSES = (*SCOPE_STATUSES, HISTORICAL_STATUS, UNCONFIRMED_STATUS, UNBOUND_STATUS)


def published_notes(vault):
    for folder in vault.iterdir():
        if (folder.name.startswith(".") or not folder.is_dir()
                or folder.is_symlink() or folder.is_junction()):
            continue
        try:
            entry = named_entry(folder)
        except ValueError:
            continue
        if entry.is_file() and not set(entry.parent.rglob("*.md")) - {entry}:
            try:
                yield entry, sha(entry)
            except FileNotFoundError:
                continue


def configured_sources(vault, packages, manifests):
    path = local_child(vault, CATALOG_SOURCES)
    previous = read(path) if path.is_file() else {"schema": 1, "packages": [], "manifests": []}
    if previous.get("schema") != 1 or any(
            not isinstance(previous.get(key), list) or any(not isinstance(value, str) for value in previous[key])
            for key in ("packages", "manifests")):
        raise ValueError("总览的数据来源登记无效")
    return {"schema": 1,
            "packages": list(dict.fromkeys([*previous["packages"], *(str(Path(p).resolve()) for p in packages)])),
            "manifests": list(dict.fromkeys([*previous["manifests"], *(str(Path(p).resolve()) for p in manifests)]))}


def batch_rows(vault, configured):
    """Keep known live batch sources across refreshes; missing manifests do not invent statuses."""
    rows = {}
    for manifest in configured["manifests"]:
        path = Path(manifest)
        if not path.is_file():
            continue
        queue = read(path)
        if Path(queue["vault"]).resolve() != vault:
            raise ValueError("批次状态与总览阅读库不同")
        for row in queue["projects"]:
            identity = row.get("id") or (manifest, row.get("sequence"))
            rows[identity] = row
    return list(rows.values())


def batch_binding(row, vault, current, package_scopes):
    # A same-source alias may reference its parent's note, but it does not own a second note.
    if row.get("status") == "同源复用" or row.get("same_source_primary"):
        return None, False
    publication = row.get("publication") or {}
    if publication.get("entry"):
        entry = Path(publication["entry"])
        if entry.is_relative_to(vault):
            relative = entry.relative_to(vault).as_posix()
            if relative in current and current[relative] == publication.get("sha256"):
                return relative, True
    if row.get("package"):
        scope = package_scopes.get(str(Path(row["package"]).resolve()))
        if scope:
            return scope["entry"], scope["status"] != UNBOUND_STATUS
    folder = row.get("course_folder")
    if folder and Path(folder).name == folder:
        try:
            entry = named_entry(local_child(vault, folder)).relative_to(vault).as_posix()
        except ValueError:
            return None, False
        # Same-name legacy notes can show their queue label, but this is not a version match.
        if entry in current:
            return entry, False
    return None, False


def scope_and_queue(vault, notes, configured):
    current = {entry.relative_to(vault).as_posix(): digest for entry, digest in notes}
    rows = batch_rows(vault, configured)
    paths = dict.fromkeys([*configured["packages"], *(str(Path(row["package"]).resolve()) for row in rows
                                                     if row.get("package") and row.get("status") != "同源复用")])
    packages = {path: scope for path in paths if (scope := current_scope(path, vault, current))}
    by_entry = {relative: {"status": HISTORICAL_STATUS, "gap_count": 0, "queue": []} for relative in current}
    trusted_scopes = {}
    for scope in packages.values():
        relative = scope["entry"]
        identity = (scope["status"], scope["gap_count"])
        if relative not in trusted_scopes:
            trusted_scopes[relative] = identity
            by_entry[relative].update(status=scope["status"], gap_count=scope["gap_count"])
        elif trusted_scopes[relative] != identity:
            by_entry[relative].update(status=UNBOUND_STATUS, gap_count=0)
    pending = []
    for row in rows:
        relative, exact = batch_binding(row, vault, current, packages)
        if not relative:
            pending.append(row)
            continue
        item = by_entry[relative]
        status = row.get("status", "状态待核")
        publication = row.get("publication") or {}
        if (exact and status in ("已验收", "历史验收待复验") and not publication.get("scope_status")
                and item["status"] == UNCONFIRMED_STATUS and publication.get("sha256") == current[relative]):
            item["status"] = HISTORICAL_STATUS
        historical_gap = bool(row.get("source_gap") or (row.get("re_review") or {}).get("gaps_before_review"))
        item["queue"].append({"status": status, "display_status": pending_status(row), "exact": exact,
                              "source_gap": historical_gap and item["status"] not in SCOPE_STATUSES,
                              "legacy_accepted": exact and status in ("已验收", "历史验收待复验")
                              and not publication.get("scope_status")})
    return by_entry, pending


def escape_label(text):
    return re.sub(r"([\\\[\]`*_~])", r"\\\1", str(text).replace("\r", " ").replace("\n", " "))


def pending_status(row):
    status = row.get("status", "状态待核")
    if status == "待处理" and (row.get("re_review") or {}).get("state") == "待复审":
        return "待复审"
    if status == "待补原件" and row.get("same_source_primary"):
        return "同源待补"
    return f"{status}（入口待核）" if status in ACCEPTED_BATCH_STATES else status


def catalog_body(vault, index, notes, statuses, pending):
    entries = [(entry, index["entries"].get(entry.relative_to(vault).as_posix(), {}).get(digest, {}))
               for entry, digest in notes]
    lines = [
        f"已发布 **{len(entries)}** 个项目（实际在库入口，是否通过同版验收见范围状态）。两个视图来自同一批同名教材。",
        "",
        "在库入口范围状态：" + "；".join(f"{status} {sum(row['status'] == status for row in statuses.values())}"
                                         for status in READING_STATUSES) + "。",
        "",
        "分析日期取通过同版验收的实质内容分析记录；制作日期取核实的核心原件日期。无足够证据显示待核实。",
    ]
    def order(item, field):
        entry, dates = item
        day = dates.get(field, {}).get("date")
        return (-int(day.replace("-", "")) if day else 0, entry.stem.casefold(), entry.name)

    def production_display(dates):
        record = dates.get("production", {})
        if not record.get("date"):
            return "待核实"
        qualifier = {"exact": "", "estimated": "（约，已知最早）", "known_earliest": "（已知最早）"}
        return record["date"] + qualifier.get(record.get("certainty"), "（待核实）")

    for field, heading in (("analysis", "按分析日期（新到旧）"), ("production", "按制作日期（新到旧）")):
        lines.extend(["", f"## {heading}", ""])
        for entry, dates in sorted(entries, key=lambda item: order(item, field)):
            label = escape_label(entry.stem)
            location = quote(Path(relpath(entry, vault / CATALOG.parent)).as_posix(), safe="/")
            analysis = dates.get("analysis", {}).get("date", "待核实")
            item = statuses[entry.relative_to(vault).as_posix()]
            status = item["status"] + (f"（{item['gap_count']}项）" if item["gap_count"] else "")
            queue = item["queue"]
            labels = []
            if item["status"] == HISTORICAL_STATUS and any(row["legacy_accepted"] for row in queue):
                labels.append("旧验收")
            if any(row["source_gap"] for row in queue):
                labels.append("来源待补（台账）")
            for row in queue:
                if (not row["exact"] or row["status"] not in ACCEPTED_BATCH_STATES
                        or item["status"] in (UNCONFIRMED_STATUS, UNBOUND_STATUS)):
                    labels.append(("台账 " if row["exact"] else "同名台账 ") + row["display_status"])
            suffix = "".join(f" · {escape_label(value)}" for value in dict.fromkeys(labels))
            lines.append(f"- [{label}]({location}) · 分析 {analysis} · 制作 {production_display(dates)} · 状态 {status}{suffix}")
    if pending:
        counts = Counter(pending_status(row) for row in pending)
        lines.extend(["", "## 尚无独立教材入口的批次项目", "",
                      f"共 **{len(pending)}** 个批次记录（不计入已发布入口）：" +
                      "；".join(f"{escape_label(status)} {count}" for status, count in sorted(counts.items())) + "。", ""])
        for row in pending:
            sequence = row.get("sequence")
            number = f"第 {sequence:03} 项 · " if isinstance(sequence, int) else ""
            label = escape_label(row.get("course_folder") or row.get("name") or "项目名称待核")
            relation = (f" · 关联第 {row['same_source_primary']:03} 项" if isinstance(row.get("same_source_primary"), int) else "")
            lines.append(f"- {number}{label} · 状态 {escape_label(pending_status(row))}{relation}")
    return "\n".join(lines).rstrip()


def managed_parts(text):
    if text.count(START) != 1 or text.count(END) != 1 or text.index(START) > text.index(END):
        return None
    prefix, remainder = text.split(START, 1)
    _, suffix = remainder.split(END, 1)
    return prefix, suffix


def refresh_catalog(vault, *, packages=(), manifests=(), production_evidence=()):
    vault = Path(vault).resolve(strict=True)
    config = local_child(vault, ".obsidian")
    if not config.is_dir():
        raise ValueError(f"请选择实际使用的 Obsidian 库 {vault}")
    note = local_child(vault, CATALOG)
    with file_lock(local_child(config, "worldanalysis-catalog.lock"), blocking=True):
        legacy = local_child(vault, LEGACY_CATALOG)
        legacy_bytes = legacy.read_bytes() if legacy.is_file() else b""
        legacy_text = legacy_bytes.decode("utf-8", errors="replace")
        old_parts = managed_parts(legacy_text)
        generated_legacy = (old_parts is not None and "\ufffd" not in legacy_text
                            and old_parts[0].lstrip("\ufeff").strip() == HEADING and not old_parts[1].strip())
        if note.exists():
            original = note.read_bytes().decode("utf-8")
        elif generated_legacy:
            original = legacy_text
        else:
            original = f"{HEADING}\n\n{START}\n{END}\n"
        parts = managed_parts(original)
        if parts is None:
            return note
        prefix, suffix = parts
        newline = "\r\n" if "\r\n" in original else "\n"
        dates = load_dates(vault)
        updates = [from_current_delivery(package, vault) for package in packages]
        for manifest in manifests:
            updates.extend(from_batch(manifest, vault))
        for evidence in production_evidence:
            updates.extend(production_updates(evidence, vault))
        merge_dates(dates, updates)
        configured = configured_sources(vault, packages, manifests)
        notes = list(published_notes(vault))
        statuses, pending = scope_and_queue(vault, notes, configured)
        if dates["entries"]:
            put(local_child(vault, DATE_INDEX), dates)
        if configured["packages"] or configured["manifests"]:
            put(local_child(vault, CATALOG_SOURCES), configured)
        section = f"\n\n{catalog_body(vault, dates, notes, statuses, pending)}\n\n".replace("\n", newline)
        write(note, f"{prefix}{START}{section}{END}{suffix}")
        if generated_legacy and legacy.is_file() and legacy.read_bytes() == legacy_bytes:
            legacy.unlink()
    return note
