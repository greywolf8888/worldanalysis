"""Edition-bound date evidence for the reader catalog; never infer dates from mtimes."""
from datetime import date, datetime, timedelta, timezone
import hashlib
from pathlib import Path

from course_companions import verify_published_companions
from project import ENTRY, local_child, named_entry, read, sha


DATE_INDEX = Path(".obsidian/worldanalysis-project-dates.json")
BUSINESS_TZ = timezone(timedelta(hours=8))
ACCEPTED_BATCH_STATES = ("已验收", "已验收（配套待补）", "已分析（核心待补）")
HISTORICAL_STATUS = "历史已发布（未按新版范围复验）"
UNCONFIRMED_STATUS = "待同版重开核验"
UNBOUND_STATUS = "当前版待核验"

def date_at(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.astimezone(BUSINESS_TZ).date().isoformat() if stamp.tzinfo else None


def plain_date(value):
    try:
        return date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


def delivery_locator(package, vault, current=None):
    """Locate the exact installed note without treating installation as acceptance."""
    package = Path(package)
    try:
        delivery_path = local_child(package, ".course/ledger/obsidian-delivery.json")
        state = read(delivery_path)
        if Path(state["vault"]) != vault:
            return None
        entry = named_entry(local_child(vault, state["folder"]))
        relative = entry.relative_to(vault).as_posix()
        if Path(state["entry"]) != entry:
            return None
        if current is not None:
            digest = current.get(relative)
        else:
            digest = sha(entry) if entry.is_file() else None
        if not digest:
            return None
        return {"entry": relative, "sha256": digest, "state": state,
                "package": package, "delivery_path": delivery_path}
    except (OSError, ValueError, TypeError, KeyError):
        return None


def same_edition(package, vault, current=None):
    """Read the current note and independently bound publication/reader receipts."""
    locator = delivery_locator(package, vault, current)
    if not locator:
        return None
    package = locator["package"]
    state = locator["state"]
    digest = locator["sha256"]
    preservation = None
    try:
        parent = state["sha256"]
        if parent != digest:
            from preservation_delivery import installed_derivative

            preservation = installed_derivative(package, vault, vault / locator["entry"], parent, digest)
            if not preservation:
                return None
        observed = state.get("observation") or {}
        if (any(observed.get(k) != state.get(k) for k in ("entry", "sha256", "installed_at"))
                or observed.get("mode") != "preview"
                or not all(observed.get(k) is True for k in ("reopened", "reached_bottom", "references_collapsed"))
                or observed.get("visible_citations") is not False):
            return None
        publication_path = local_child(package, f".course/ledger/publications/{parent}.json")
        publication = read(publication_path)
        if (publication["outputs"][ENTRY.as_posix()] != parent
                or hashlib.sha256(publication["body"].encode("utf-8")).hexdigest() != parent):
            return None
        entry = local_child(vault, locator["entry"])
        companions = verify_published_companions(entry.parent.parent, publication)
        check = publication["obsidian"]["checks"]
        if (len(check) != 1 or check[0].get("document_sha256") != parent or check[0].get("reachedBottom") is not True
                or publication["obsidian"].get("companions", {}) != companions):
            return None
        if observed.get("method") == "日常入口同版快照的 Obsidian 接口重开" and not observed.get("render_sha256"):
            return None
        if observed.get("render_sha256"):
            render = local_child(package, f".course/work/日常库接口验收/{parent}.json")
            if sha(render) != observed["render_sha256"] or (companions and read(render).get("companions") != companions):
                return None
        intake = local_child(package, ".course/ledger/intake.json")
        intake_digest = sha(intake)
        if publication["review"]["snapshot"]["evidence"]["ledger/intake.json"] != intake_digest:
            return None
        relation = {"parent_sha256": parent, "preservation": preservation} if preservation else {}
        return {"entry": locator["entry"], "sha256": digest, **relation,
                "package": package, "publication": publication, "publication_path": publication_path,
                "delivery_path": locator["delivery_path"], "intake_sha256": intake_digest}
    except (OSError, ValueError, TypeError, KeyError):
        return None


def current_scope(package, vault, current):
    """Only display a new scope result after the current version's reader and proof are bound."""
    locator = delivery_locator(package, vault, current)
    if not locator:
        return None
    result = {"entry": locator["entry"], "status": UNCONFIRMED_STATUS, "gap_count": 0}
    edition = same_edition(package, vault, current)
    if not edition:
        return result if locator["state"].get("sha256") == locator["sha256"] else {**result, "status": UNBOUND_STATUS}
    publication = edition["publication"]
    if edition.get("preservation"):
        result["preservation"] = edition["preservation"]
    try:
        from scope_review import publication_scope

        scope = publication_scope(publication)
        if scope["scope_status"] is None:
            return {**result, "status": HISTORICAL_STATUS}
        return {**result, "status": scope["scope_status"], "gap_count": len(scope["scope_gaps"])}
    except (OSError, ValueError, TypeError, KeyError):
        return {**result, "status": UNBOUND_STATUS}


def date_identity(edition):
    identity = {key: edition[key] for key in ("entry", "sha256", "intake_sha256")}
    if edition.get("parent_sha256"):
        identity["parent_sha256"] = edition["parent_sha256"]
    return identity


def analysis_update(edition, when, source, kind):
    day = date_at(when)
    if not day:
        return None
    return {**date_identity(edition),
            "analysis": {"date": day, "timestamp": when, "source": str(source), "kind": kind}}


def from_current_delivery(package, vault):
    edition = same_edition(package, vault)
    if not edition:
        return None
    # A legacy re-open does not create an analysis-completion timestamp.
    update = analysis_update(edition, edition["publication"].get("analysis_completed_at"),
                             edition["publication_path"], "新正文版次实质分析及正式同版重开")
    return update or (date_identity(edition) if edition.get("parent_sha256") else None)


def legacy_date(path, edition, vault):
    try:
        info = read(path)
        expected = vault / edition["entry"]
        report = read(Path(info["交付回执"]))
        delivery = report["vault_delivery"]
        parent = edition.get("parent_sha256", edition["sha256"])
        if (Path(info["入口"]) != expected or info["版本"] != parent
                or report.get("verified") is not True or report.get("package_verified") is not True
                or Path(delivery["entry"]) != expected or delivery["sha256"] != parent):
            return None
        return analysis_update(edition, info.get("日期时间"), path, "同版单项正式验收记录")
    except (OSError, ValueError, TypeError, KeyError):
        return None


def from_batch(manifest, vault, sequence=None):
    queue = read(manifest)
    if Path(queue["vault"]) != vault:
        raise ValueError("日期回填批次与总览阅读库不同")
    for row in queue["projects"]:
        if sequence is not None and row["sequence"] != sequence:
            continue
        if not row.get("package") or row.get("status") not in (*ACCEPTED_BATCH_STATES, "处理中", "历史验收待复验"):
            continue
        edition = same_edition(row["package"], vault)
        if not edition:
            continue
        update = analysis_update(edition, edition["publication"].get("analysis_completed_at"),
                                 edition["publication_path"], "新正文版次实质分析及正式同版重开")
        if update:
            yield update
            continue
        # Same-edition dated single-course receipt has precedence over batch
        # migration and repeated checks, neither of which moves the analysis date.
        paths = [manifest.parent / f"{row['sequence']:03}-已验收项目.json"]
        if row.get("legacy_record"):
            paths.append(Path(row["legacy_record"]))
        updates = [item for path in dict.fromkeys(paths) if (item := legacy_date(path, edition, vault))]
        if updates:
            yield min(updates, key=lambda item: item["analysis"]["date"])
            continue
        previous = row.get("history", [])
        parent = edition.get("parent_sha256", edition["sha256"])
        if (row["status"] != "已验收" or (row.get("publication") or {}).get("sha256") != parent
                or not previous):
            if edition.get("parent_sha256"):
                yield date_identity(edition)
            continue
        same_version = [item for item in [*previous, row]
                        if (item.get("publication") or {}).get("sha256") == parent]
        first = same_version[0]
        # A change from a different edition or from active to accepted is a
        # durable workflow event. Same-edition historical rechecks alone are not.
        if first["status"] == "已验收":
            first_index = next(i for i, item in enumerate([*previous, row]) if item is first)
            if first_index > 0:
                update = analysis_update(edition, first.get("last_changed"), manifest,
                                         "批次首个同版正式验收事件")
                if update:
                    yield update
                    continue
        if edition.get("parent_sha256"):
            yield date_identity(edition)


def production_updates(evidence, vault):
    """Import explicit, original-bound date evidence; unknown stays unknown."""
    document = read(evidence)
    if document.get("schema") != 1 or not isinstance(document.get("entries"), list):
        raise ValueError("制作日期证据需要 schema=1 和 entries")
    for item in document["entries"]:
        edition = same_edition(item["package"], vault)
        if not edition or item["edition_sha256"] not in {edition["sha256"], edition.get("parent_sha256", edition["sha256"])}:
            raise ValueError("制作日期证据未绑定当前正式同版教材")
        intake = read(local_child(edition["package"], ".course/ledger/intake.json"))
        by_name = {source["file"]: source["sha256"] for source in intake["sources"]}
        found = []
        for source in item["sources"]:
            file = local_child(edition["package"] / "原始资料", source["file"])
            if (source["sha256"] != by_name.get(source["file"]) or sha(file) != source["sha256"]
                    or not plain_date(source["date"]) or source.get("certainty") not in ("exact", "estimated")
                    or not source.get("evidence", "").strip()):
                raise ValueError(f"制作日期证据缺少有效原件绑定或日期来源 {file}")
            found.append({key: source[key] for key in ("file", "sha256", "date", "certainty", "evidence")})
        if not found or len({source["file"] for source in found}) != len(found):
            raise ValueError("同一核心原件须先排除日期冲突并提供一个最可信的制作日期")
        earliest = min(found, key=lambda source: (source["date"], source["certainty"]))
        exact = item.get("all_core_dates_verified") is True and all(
            source["certainty"] == "exact" for source in found)
        certainty = "exact" if exact else ("estimated" if earliest["certainty"] == "estimated" else "known_earliest")
        yield {**date_identity(edition),
               "production": {"date": earliest["date"], "certainty": certainty,
                              "source": str(evidence), "sources": found}}


def load_dates(vault):
    path = local_child(vault, DATE_INDEX)
    if not path.exists():
        return {"schema": 1, "entries": {}}
    index = read(path)
    if index.get("schema") != 1 or not isinstance(index.get("entries"), dict):
        raise ValueError("项目日期索引格式无效")
    return index


def merge_dates(index, updates):
    for update in updates:
        if not update:
            continue
        records = index["entries"].setdefault(update["entry"], {})
        edition = records.setdefault(update["sha256"], {"intake_sha256": update["intake_sha256"]})
        if edition["intake_sha256"] != update["intake_sha256"]:
            raise ValueError("正文指纹相同但原始材料身份变更，需要重新核对日期")
        if parent := update.get("parent_sha256"):
            original = records.get(parent, {})
            if original and original["intake_sha256"] != edition["intake_sha256"]:
                raise ValueError("派生版父件的原件身份与日期证据不符")
            for key in ("analysis", "production"):
                if key in original and key not in edition:
                    edition[key] = original[key]
        if "analysis" in update:
            old = edition.get("analysis")
            if not old or update["analysis"]["date"] < old["date"]:
                edition["analysis"] = update["analysis"]
        if "production" in update:
            edition["production"] = update["production"]
        elif "production" not in edition and not parent:
            known = [record["production"] for record in records.values()
                     if record.get("intake_sha256") == edition["intake_sha256"] and record.get("production")]
            if known:
                edition["production"] = known[0]
