"""Map local media and archive members to traceable, stable course sources."""
from collections import defaultdict
from pathlib import Path, PurePosixPath
import re
from shutil import disk_usage
from subprocess import TimeoutExpired
from tarfile import TarError
from tempfile import TemporaryDirectory
import unicodedata
from zipfile import BadZipFile

from archive_media import ARCHIVES, MAX_MEMBER_BYTES, extract_member, list_members, readable_members
from embedded_media import DIRECTORY, collect_embedded_videos, verify_embedded_videos
from prepare import natural, probe_video
from project import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, copy_file, local_child, media_source, put, read, sha


MEDIA_INVENTORY = "media-inventory.json"
DECISIONS = "媒体归类.json"
ROUTING_REVISION = 3
MAX_NESTING = 3
MAX_NESTED_BYTES = 1024**3
MAX_PROJECT_MEMBERS = 250_000
MAX_DUPLICATE_PROBES = 24
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
ASSET_FOLDERS = frozenset({"_internal", "node_modules", "site-packages", ".minecraft", "sample_data", "bgm",
                           "音效", "常用音效", "世界音效", "配乐", "常用配乐", "常用bgm", "背景音乐",
                           "声音素材", "音效库", "soundeffects", "sfx"})
TEACHING = re.compile(r"(?:课程|课堂|教程|教学|录屏|讲解|配套说明|第\s*[\d一二三四五六七八九十]+\s*[讲课节]|"
                      r"(?:^|[\s_.-])(?:lesson|lecture|tutorial)(?=$|[\s_.\d-]))", re.I)
SAMPLE = re.compile(r"(?:样片|样例|成片|空镜|音效|配乐|背景音乐|(?:^|[\s_.-])(?:demo|sample|preview|bgm|sfx)(?=$|[\s_.\d-]))", re.I)
DUPLICATE_SUFFIX = re.compile(r"(?:\s*(?:\(\d+\)|\[\d+\])|[ _-]+(?:copy|副本)\s*\d*)$", re.I)
ARCHIVE_ERRORS = (BadZipFile, TarError, OSError, ValueError, RuntimeError, NotImplementedError)


def is_media(name):
    path = PurePosixPath(name)
    return path.suffix.lower() in MEDIA_EXTENSIONS and not path.name.lower().endswith(".d.ts")


def decide_default(row):
    embedded = row.get("embedded")
    if embedded and embedded.get("pages"):
        return "教学", "幻灯片播放关系指向此媒体"
    if embedded and not embedded.get("container"):
        return "待核", "幻灯片没有指向这项媒体的播放页，需核对实际用途"
    locators = [row["file"], *row.get("embedded", {}).get("member_chain", [])]
    parts = [part.casefold() for locator in locators for part in PurePosixPath(locator).parts[:-1]]
    stem = PurePosixPath(locators[-1]).stem
    asset = next((part for part in parts if part in ASSET_FOLDERS), None)
    teaching_name = TEACHING.search(stem)
    if asset and teaching_name:
        return "待核", f"文件名指向教学，但所在目录 {asset} 是素材或软件目录，需核对实际用途"
    if asset:
        return "参考", f"位于明确的素材或软件依赖目录 {asset}"
    if SAMPLE.search(stem):
        return "待核", "名称可能表示样片、预览或音效，需确认在教学中的用途"
    if teaching_name:
        return "教学", "文件名直接表明为课堂或教程媒体"
    if row["extension"] in VIDEO_EXTENSIONS and "embedded" not in row:
        return "教学", "单独收到的课程视频，仍须逐段核对内容和用途"
    return "待核", "需试听或对照原附件，确认这是教学讲解还是配套素材"


def read_decisions(project):
    path = local_child(project.work, DECISIONS)
    if not path.is_file():
        return {}, None
    decisions = read(path)
    if not isinstance(decisions, dict):
        raise ValueError("媒体归类须以原件定位作为键")
    for locator, row in decisions.items():
        if (not isinstance(row, dict) or set(row) - {"role", "reason", "target"}
                or row.get("role") not in {"教学", "参考", "重复"}
                or not isinstance(row.get("reason"), str) or not row["reason"].strip()
                or (row["role"] == "重复" and (not isinstance(row.get("target"), str) or row["target"] == locator
                                               or not row["target"].strip()))
                or (row["role"] != "重复" and "target" in row)):
            raise ValueError(f"媒体归类需有教学、参考或重复角色及核对依据。重复项需指明教学源 {locator}")
    return decisions, sha(path)


def media_sort(row):
    embedded = row.get("embedded")
    if embedded is None and row["extension"] in VIDEO_EXTENSIONS:
        return 0, natural(row["file"]), row["file"]
    if embedded and not embedded.get("container"):
        return 1, natural(embedded["document"]), not embedded["pages"], embedded["pages"], embedded["member"]
    return 2, natural(row["file"]), row["file"]


def stable_lessons(project):
    path = project.ledger / MEDIA_INVENTORY
    if path.is_file():
        previous = read(path)
        return previous["lesson_ids"], set(previous["legacy_sources"])
    legacy = project.ledger / "inventory.json"
    ids = {row["file"]: row["lesson"] for row in read(legacy)["sources"] if "lesson" in row} if legacy.is_file() else {}
    summary = project.ledger / "media-summary.json"
    if summary.is_file():
        media = read(summary)
        ids.update({row["source"]: row["lesson"] for row in media["videos"] + media.get("unreadable", [])})
    return ids, set(ids)


def find_course_media(project, sources, decisions, legacy_sources, scratch):
    candidates = []
    archives = []
    targets = {}
    unread = []
    paths = {}
    indexed = 0

    def remember(row, default=None):
        if row["file"] in targets:
            raise ValueError(f"媒体来源定位重名 {row['file']}")
        targets[row["file"]] = row
        candidate = decisions.get(row["file"])
        if candidate:
            row.update(candidate)
            row["reviewed"] = True
        else:
            row["role"], row["reason"] = default or decide_default(row)
        if row["extension"] in AUDIO_EXTENSIONS:
            row["kind"] = "audio"
        candidates.append(row)

    def archive_error(locator, error):
        row = {"source": locator, "reason": str(error)}
        decision = decisions.get(locator)
        if decision is None or decision["role"] != "参考":
            unread.append(row)
        else:
            row["decision"] = decision["reason"]
        return row

    def temporary(extension):
        folder = Path(scratch) / str(len(paths) + len(archives) + len(candidates))
        folder.mkdir(exist_ok=True)
        return folder / f"member{extension}"

    def scan(path, source, chain):
        nonlocal indexed
        locator = "!".join((source["file"], *chain))
        archive = {"source": locator, "document": source["file"], "document_sha256": source["sha256"],
                   "member_chain": list(chain)}
        archives.append(archive)
        try:
            entries = list_members(path)
        except ARCHIVE_ERRORS as error:
            archive.update(archive_error(locator, error))
            return
        if indexed + len(entries) > MAX_PROJECT_MEMBERS:
            archive.update(archive_error(locator, "本课程压缩包累计成员超出可登记上限"))
            return
        indexed += len(entries)
        members = readable_members(entries)
        archive["member_count"] = len(entries)
        archive["members"] = []
        archive["reported_media"] = []
        for member in entries:
            status = "directory" if member.name.endswith("/") else "unreadable" if member.name not in members else (
                "archive" if member.suffix in ARCHIVES else "media" if is_media(member.name) else "file")
            archive["members"].append({"path": member.name, "size": member.size, "status": status})
            if status == "directory" or not (is_media(member.name) or member.suffix in ARCHIVES):
                continue
            inner_chain = (*chain, member.name)
            inner_locator = "!".join((source["file"], *inner_chain))
            if member.name not in members:
                error = archive_error(inner_locator, "档内媒体或压缩包存在重名、路径异常或不是常规文件")
                if error not in archive.setdefault("gaps", []):
                    archive["gaps"].append(error)
                continue
            if member.suffix in ARCHIVES:
                if len(chain) >= MAX_NESTING:
                    archive.setdefault("gaps", []).append(archive_error(inner_locator, "超过已配置的压缩包递归层数"))
                    continue
                target = temporary(member.suffix)
                try:
                    extract_member(path, member, target, limit=MAX_NESTED_BYTES)
                    scan(target, source, inner_chain)
                except ARCHIVE_ERRORS as error:
                    archive.setdefault("gaps", []).append(archive_error(inner_locator, error))
                finally:
                    target.unlink(missing_ok=True)
                continue

            row = {"file": inner_locator, "extension": member.suffix, "bytes": member.size,
                   "embedded": {"document": source["file"], "document_sha256": source["sha256"],
                                "container": source["extension"].lstrip("."), "member": member.name,
                                "member_chain": list(inner_chain), "pages": []}}
            remember(row)
            archive["reported_media"].append(inner_locator)
            if row["role"] == "参考":
                continue
            target = temporary(member.suffix)
            try:
                extract_member(path, member, target, limit=min(MAX_MEMBER_BYTES, disk_usage(scratch).free))
                digest = sha(target)
            except ARCHIVE_ERRORS as error:
                target.unlink(missing_ok=True)
                row.update(role="待核", reason=str(error))
                continue
            row.update(sha256=digest, bytes=target.stat().st_size)
            if row["role"] == "教学":
                row["embedded"]["work_file"] = f"{DIRECTORY}/{digest}{member.suffix}"
                paths[inner_locator] = target
            else:
                target.unlink()

    for source in sources:
        if is_media(source["file"]):
            # Preserve established loose lesson assignments on package upgrades.
            legacy = ("教学", "沿用已有课包已登记的讲号，更新时保留课程身份") if source["file"] in legacy_sources else None
            remember(dict(source), legacy)
    for row in collect_embedded_videos(project.raw, sources, work=project.work):
        legacy = ("教学", "沿用当前课包的幻灯片内嵌讲号") if row["file"] in legacy_sources else None
        remember(row, legacy)
    for source in sources:
        if source["extension"] in ARCHIVES:
            scan(local_child(project.raw, source["file"]), source, ())
    for locator in decisions:
        if locator not in targets and not any(locator == archive["source"] or any(
            locator == gap["source"] for gap in archive.get("gaps", [])) for archive in archives):
            raise ValueError(f"找不到媒体归类中的原件 {locator}")
    return candidates, archives, unread, paths


def resolve_duplicates(candidates, legacy_ids):
    teaching = defaultdict(list)
    by_file = {row["file"]: row for row in candidates}
    for row in candidates:
        if row["role"] == "教学" and "sha256" in row:
            teaching[row["sha256"]].append(row)
    for related in teaching.values():
        related.sort(key=lambda row: (legacy_ids.get(row["file"], float("inf")), media_sort(row)))
    for row in candidates:
        if row["role"] in {"教学", "待核"} and (matches := teaching.get(row.get("sha256"))):
            canonical = matches[0]
            if row is not canonical:
                row.update(role="重复", target=canonical["file"], reason="完整文件 SHA-256 相同，沿用教学源")
    for row in candidates:
        if row["role"] == "重复":
            target = by_file.get(row["target"])
            if target is None or target["role"] != "教学":
                row.update(role="待核", reason=f"重复项须指向已选教学源 {row['target']}")


def detect_similar_names(project, candidates, legacy_ids, paths):
    """Flag plausible copies without merging recordings on title or duration alone."""
    groups = defaultdict(list)
    for row in candidates:
        if row["role"] not in {"教学", "待核"} or not row.get("sha256"):
            continue
        file = row.get("embedded", {}).get("member", row["file"])
        stem = unicodedata.normalize("NFKC", PurePosixPath(file).stem).casefold().strip()
        title = DUPLICATE_SUFFIX.sub("", stem).strip()
        if sum(char.isalnum() for char in title) >= 10:
            groups[row.get("kind", "video"), title].append(row)

    probes = {}

    def duration(row):
        locator = row["file"]
        if locator not in probes and len(probes) < MAX_DUPLICATE_PROBES:
            source = paths.get(locator)
            if source is None and "embedded" not in row:
                source = local_child(project.raw, locator)
            if source is None and row.get("embedded", {}).get("work_file") and "container" not in row["embedded"]:
                source = local_child(project.work, row["embedded"]["work_file"])
            try:
                probes[locator] = probe_video(source, row.get("kind", "video"))[1] if source else None
            except (OSError, ValueError, TimeoutExpired):
                probes[locator] = None
        return probes.get(locator)

    for (_, title), related in groups.items():
        if len(related) < 2 or not any(row["role"] == "教学" for row in related):
            continue
        related.sort(key=lambda row: (row["role"] != "教学", legacy_ids.get(row["file"], float("inf")),
                                      not row.get("reviewed", False), media_sort(row)))
        representatives = [related[0]]
        for row in related[1:]:
            if row.get("reviewed"):
                if row["role"] == "教学":
                    representatives.append(row)
                continue
            later = duration(row)
            for primary in representatives:
                earlier = duration(primary)
                if earlier is not None and later is not None:
                    similar = abs(earlier - later) <= max(.5, min(earlier, later) * .01)
                else:
                    big = max(primary["bytes"], row["bytes"])
                    similar = bool(big) and abs(primary["bytes"] - row["bytes"]) <= big * .15
                if similar:
                    row.update(role="待核", reason="文件名和时长或体积接近但摘要不同，需对照原视频确认重录、重编码或不同教程",
                               possible_duplicate={"target": primary["file"], "normalized_title": title,
                                                   "duration_s": later, "target_duration_s": earlier})
                    break
            else:
                if row["role"] == "教学":
                    representatives.append(row)


def collect_course_media(project, sources):
    decisions, decision_sha = read_decisions(project)
    path = project.ledger / MEDIA_INVENTORY
    intake_sha = sha(project.intake)
    if path.is_file():
        cached = read(path)
        if (cached.get("revision") == ROUTING_REVISION and cached["intake_sha256"] == intake_sha
                and cached["decisions_sha256"] == decision_sha and not cached["unresolved"]):
            try:
                for row in cached["selected"]:
                    if embedded := row.get("embedded"):
                        media_source(project.raw, project.work, {"source": row["file"], "sha256": row["sha256"],
                                     "embedded": embedded}, verify_original=True)
            except (OSError, ValueError):
                pass
            else:
                digest = sha(path)
                copy_file(path, project.ledger / "media-inventories" / f"{digest}.json", digest)
                return cached
    lesson_ids, legacy_sources = stable_lessons(project)
    project.work.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=project.work, prefix="媒体盘点-") as scratch:
        candidates, archives, unresolved, paths = find_course_media(project, sources, decisions, legacy_sources, scratch)
        resolve_duplicates(candidates, lesson_ids)
        detect_similar_names(project, candidates, lesson_ids, paths)
        selected = []
        for row in sorted(candidates, key=media_sort):
            locator = row["file"]
            if row["role"] == "待核":
                unresolved.append({"source": locator, "reason": row["reason"]})
            if row["role"] != "教学":
                if locator in lesson_ids:
                    row["previous_lesson"] = lesson_ids[locator]
                continue
            if locator in paths:
                target = local_child(project.work, row["embedded"]["work_file"])
                copy_file(paths[locator], target, row["sha256"])
            if locator not in lesson_ids:
                lesson_ids[locator] = max(lesson_ids.values(), default=0) + 1
            row["lesson"] = lesson_ids[locator]
            selected.append({key: row[key] for key in ("file", "sha256", "extension", "bytes", "lesson", "kind", "embedded")
                             if key in row})
    selected.sort(key=lambda row: row["lesson"])
    manifest = {"schema": 1, "revision": ROUTING_REVISION, "intake_sha256": intake_sha, "decisions_sha256": decision_sha,
                "lesson_ids": lesson_ids, "legacy_sources": sorted(legacy_sources),
                "archives": archives, "candidates": candidates,
                "unresolved": unresolved, "selected": selected}
    put(path, manifest)
    digest = sha(path)
    copy_file(path, project.ledger / "media-inventories" / f"{digest}.json", digest)
    return manifest


def verify_course_media(project, sources, records, *, require_current=True, manifest_sha256=None):
    current = project.ledger / MEDIA_INVENTORY
    if not require_current and manifest_sha256 is None:
        return verify_embedded_videos(project, sources, records)
    if require_current and not current.is_file():
        if any(row["extension"] in ARCHIVES | AUDIO_EXTENSIONS for row in sources):
            raise ValueError("原件含压缩包或独立音频，请重新运行 prepare 盘点全部媒体")
        return verify_embedded_videos(project, sources, records)
    if manifest_sha256 is not None and not re.fullmatch(r"[a-f0-9]{64}", manifest_sha256):
        raise ValueError("媒体归类摘要无效")
    path = current if require_current else local_child(project.ledger, f"media-inventories/{manifest_sha256}.json")
    if manifest_sha256 is not None and sha(path) != manifest_sha256:
        raise ValueError("媒体归类记录已变动")
    manifest = read(path)
    if manifest["intake_sha256"] != sha(project.intake):
        raise ValueError("媒体归类须对应当前原件快照")
    decisions = local_child(project.work, DECISIONS)
    if require_current and manifest["decisions_sha256"] != (sha(decisions) if decisions.is_file() else None):
        raise ValueError("媒体归类决定已变动，请重新运行 prepare")
    if manifest["unresolved"]:
        raise ValueError("仍有媒体或档内成员待核，请先确认归类和检查范围")
    expected = {row["lesson"]: (row["file"], row["sha256"], row.get("embedded"), row.get("kind", "video"))
                for row in manifest["selected"]}
    actual = {row["lesson"]: (row["source"], row.get("source_sha256") or row["sha256"],
                              row.get("embedded"), row.get("kind", "video")) for row in records}
    if expected != actual or len(actual) != len(records):
        raise ValueError("教学媒体和归类结果不一致，请重新运行 prepare")
    raw = {row["file"]: row["sha256"] for row in sources}
    for source, digest, embedded, _ in expected.values():
        if embedded:
            if raw.get(embedded["document"]) != embedded["document_sha256"]:
                raise ValueError(f"媒体父文件不在原件快照 {source}")
            media_source(project.raw, project.work, {"source": source, "sha256": digest, "embedded": embedded},
                         verify_original=True)
        elif raw.get(source) != digest:
            raise ValueError(f"教学媒体不在原件快照 {source}")
