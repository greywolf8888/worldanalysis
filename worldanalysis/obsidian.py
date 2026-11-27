"""Export verified, published evidence as a local Obsidian reading view.

Run ``python -m worldanalysis.obsidian --root WORKSPACE --project ID --vault VAULT``.
VAULT must be separate from both the source workspace and this code checkout.
No plugin, network request, media decoder, model, or source credential is used.

The database is opened with SQLite mode=ro and query_only. Unlike Store(root),
this reader does not create schemas, reconcile publications, or recover analysis
jobs. Store's existing publication verifier remains the authority. An invalid
publication fails before any Vault write; this command does not repair it.

Generated pages are replaceable views, never evidence or reading receipts.
Independent personal-note files are only created, never updated. Modified
generated pages are preserved and their new version is written as a merge
candidate. The export manifest records only the last content written by us.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import sys
import uuid

from .contracts import (
    ContractError, asset_key, canonical, check_id, hash_file, redact_text,
    reject_links, safe_path, scan_secrets, validate_relative_path,
)
from .store import Store

EXPORT_VERSION = "1"
STATE_SCHEMA = "worldanalysis.obsidian-view.v1"
MAX_TEXT_BYTES = 64 * 1024 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
IMAGE_KINDS = {"image", "page_render", "keyframe"}
IMAGE_SUFFIXES = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif", "BMP": "bmp", "TIFF": "tiff"}
ORIGINS = {
    "author_original": "作者原始内容", "native_extract": "原生提取内容",
    "ocr": "OCR 文字", "asr": "语音识别文字", "provider_ai_note": "平台 AI 笔记",
    "visual_observation": "画面观察", "model_inference": "模型推断", "unknown": "来源未知",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key(value: str) -> str:
    # Opaque IDs may contain ':' and be 200 characters long; neither is a safe
    # cross-platform filename. The complete identity remains in properties.
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _absolute(path, *, exists=False):
    path = Path(os.path.abspath(path))
    reject_links(path)
    return path.resolve(strict=exists)


def _is_inside(path, root):
    return path == root or path.is_relative_to(root)


def _separate_vault(root, vault, *, allow_workspace_delivery=False):
    root = _absolute(root, exists=True)
    vault = _absolute(vault)
    code_root = _absolute(Path(__file__).parent.parent, exists=True)
    # Explicit, narrow exception for a view-only delivery area. Do not turn this
    # into a general nested-vault permission: evidence, code and database parents
    # must stay disjoint. Link/junction checks still apply to every ancestor.
    delivery = root / "_deliveries" / "Obsidian"
    isolated_delivery = allow_workspace_delivery and _is_inside(vault, delivery)
    for protected in (root, code_root):
        if _is_inside(vault, protected) or _is_inside(protected, vault):
            if isolated_delivery and protected == root:
                continue
            raise ContractError("Vault must be separate from the evidence workspace and code checkout; only explicit _deliveries/Obsidian delivery nesting is allowed")
    if vault.exists() and not vault.is_dir():
        raise ContractError("Vault must be a directory")
    return root, vault


class _PublishedReader(Store):
    """Use Store verification without Store's writable initialization."""

    def __init__(self, root):
        self.root = _absolute(root, exists=True)
        self.results_root = self.root / "整理结果"
        self.db_path = self.results_root / "worldanalysis.sqlite3"
        reject_links(self.db_path)
        if not self.db_path.is_file():
            raise ContractError("existing Worldanalysis database required")
        self.connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only=ON")
        self.connection.execute("BEGIN")


def _literal(value) -> str:
    """Render untrusted source text as text, never as remote embeds or HTML."""
    value = redact_text(str(value))[0]
    value = html.escape(value, quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|$%~^=\-])", r"\\\1", value)


def _quote(value) -> str:
    return "\n".join("> " + _literal(line) + "  " for line in str(value).splitlines())


def _properties(values) -> str:
    # JSON scalar syntax is valid YAML. All properties are scalars or flat lists.
    scan_secrets(values)
    return "---\n" + "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False, allow_nan=False)}"
        for key, value in values.items()
    ) + "\n---\n\n"


def _time(value):
    value = int(value)
    seconds, ms = divmod(value, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


def _location(locator):
    if locator["kind"] == "time":
        return _time(locator["start_ms"]) + " — " + _time(locator["end_ms"])
    for field, label in (("page_number", "页"), ("slide_number", "幻灯片"), ("sheet_name", "工作表"), ("paragraph_index", "段落")):
        if field in locator:
            return label + " " + str(locator[field])
    return "内容块"


def _artifact_bytes(project_root, artifact, *, limit):
    if artifact["byte_length"] > limit:
        raise ContractError("export artifact exceeds bounded read limit")
    path = safe_path(project_root, artifact["relative_path"])
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit or (_sha(data), len(data)) != (artifact["sha256"], artifact["byte_length"]):
        raise ContractError("artifact changed after publication verification")
    return data


def _source_title(reader, project_id, asset):
    row = reader.connection.execute(
        "SELECT local_path FROM sources WHERE project_id=? AND source_id=?",
        (project_id, asset["source_ref"]),
    ).fetchone()
    path = row[0] if row else None
    if path:
        path = str(path)
        name = (PureWindowsPath(path) if "\\" in path else Path(path)).name
        # This is a display label only, never a filename or a verified source URL.
        return " ".join(redact_text(name)[0].split())[:300] or asset["asset_id"]
    return asset["asset_id"]


def _asset_metadata(project_root, asset, artifacts):
    """Read only bounded, published display metadata, never the input manifest.

    A platform package's declared video ID is useful for the reader but cannot
    replace Store's actual local source binding or establish video completeness.
    """
    expected = "/metadata/" + asset["asset_id"] + ".json"
    candidates = [a for a in artifacts.values() if a["kind"] == "structured_data"
                  and asset_key(a["asset_ref"]) == asset_key(asset)
                  and a["relative_path"].endswith(expected)
                  and a["access_class"] == "project_local"
                  and a["byte_length"] <= MAX_METADATA_BYTES]
    if not candidates:
        return {}
    value = json.loads(_artifact_bytes(project_root, candidates[0], limit=MAX_METADATA_BYTES))
    if not isinstance(value, dict):
        return {}
    result = {}
    for name in ("title", "source_material_id"):
        if isinstance(value.get(name), str):
            result[name] = " ".join(redact_text(value[name])[0].split())[:1000]
    declared = value.get("source_video_declared")
    if isinstance(declared, dict):
        for name in ("provider", "file_id"):
            if isinstance(declared.get(name), (str, int)) and not isinstance(declared[name], bool):
                result["declared_" + name] = " ".join(redact_text(str(declared[name]))[0].split())[:1000]
        duration = declared.get("duration_ms")
        if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0:
            result["declared_video_duration_ms"] = duration
    return result


def _text_for_block(project_root, block, artifacts, budget):
    # Store creates a complete per-block text artifact, distinct from the raw
    # whole-document text that can be shared by every block. Do not repeat the
    # raw document for each cue, nor export only the 16000-character preview.
    own = [artifacts[a] for a in block["artifact_ids"]
           if artifacts[a]["kind"] == "native_text"
           and artifacts[a]["relative_path"].endswith("/text/" + block["block_id"] + ".txt")]
    if own:
        artifact = own[0]
        if artifact["access_class"] == "restricted_local":
            return "[受限文字未导出]"
        data = _artifact_bytes(project_root, artifact, limit=MAX_TEXT_BYTES - budget[0])
        budget[0] += len(data)
        text = data.decode("utf-8")
    else:
        text = block.get("text", "")
        budget[0] += len(text.encode("utf-8"))
        if budget[0] > MAX_TEXT_BYTES:
            raise ContractError("export text budget exceeded; no truncated view published")
    return redact_text(text)[0]


def _image_map(project_root, bundle):
    from PIL import Image
    images, artifacts, warnings = {}, {}, []
    for artifact in bundle["artifacts"]:
        if artifact["kind"] not in IMAGE_KINDS or artifact["access_class"] == "restricted_local":
            continue
        if artifact["byte_length"] > MAX_IMAGE_BYTES:
            raise ContractError("image exceeds export byte bound")
        path = safe_path(project_root, artifact["relative_path"])
        with Image.open(path) as image:
            if image.width * image.height > 64_000_000:
                raise ContractError("image exceeds export pixel bound")
            extension = IMAGE_SUFFIXES.get(image.format)
            image.verify()
        if not extension:
            warnings.append("Unsupported image format omitted: " + artifact["artifact_id"])
            continue
        relative = "附件/" + artifact["sha256"] + "." + extension
        images.setdefault(relative, artifact)
        artifacts[artifact["artifact_id"]] = relative
    return images, artifacts, warnings


def _load_state(course, project_id):
    path = safe_path(course, "worldanalysis-export.json", must_exist=False)
    if not path.exists():
        return {"schema": STATE_SCHEMA, "project_id": project_id, "managed_files": {}}
    if path.stat().st_size > MAX_STATE_BYTES:
        raise ContractError("export state exceeds size bound")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != STATE_SCHEMA or value.get("project_id") != project_id or not isinstance(value.get("managed_files"), dict):
        raise ContractError("export state identity/schema mismatch")
    for relative, sha in value["managed_files"].items():
        validate_relative_path(relative)
        if relative == "worldanalysis-export.json" or not isinstance(sha, str) or not re.fullmatch("[a-f0-9]{64}", sha):
            raise ContractError("invalid managed-file record")
    return value


@contextmanager
def _export_lock(course):
    path = safe_path(course, ".worldanalysis-export.lock", must_exist=False)
    token = uuid.uuid4().hex.encode("ascii")
    try:
        with path.open("xb") as stream:
            stream.write(token)
    except FileExistsError as exc:
        raise ContractError("export already running or interrupted; inspect the existing export lock before retrying") from exc
    try:
        yield
    finally:
        reject_links(path)
        if path.is_file() and path.read_bytes() == token:
            path.unlink()


def _atomic_write(path, data, *, expected_sha=None, create_only=False):
    reject_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_links(path.parent)
    temporary = path.with_name(".worldanalysis-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        reject_links(path)
        if create_only:
            # Atomic no-replace publication, including on Windows. Both names
            # are on the same filesystem and the temporary is removed below.
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise ContractError("destination appeared while exporting") from exc
        else:
            if expected_sha is not None and (not path.is_file() or hash_file(path)[0] != expected_sha):
                raise ContractError("destination edited while exporting")
            os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class _ViewWriter:
    def __init__(self, course, state):
        self.course = course
        self.managed = dict(state["managed_files"])
        self.created, self.updated, self.unchanged, self.conflicts = [], [], [], []

    def _candidate(self, relative, data):
        original = PurePosixPath(relative)
        # Keep a candidate beside the original: all relative Obsidian links
        # remain correct, including links from a conflicted course overview.
        base = original.parent / (original.stem + "-待合并-" + _sha(data)[:20])
        for number in range(1000):
            suffix = "" if number == 0 else "-" + str(number)
            candidate = str(base) + suffix + original.suffix
            path = safe_path(self.course, candidate, must_exist=False)
            if path.exists():
                if hash_file(path)[0] == _sha(data):
                    self.managed[candidate] = _sha(data)
                    self.unchanged.append(candidate)
                    return candidate
                continue
            _atomic_write(path, data, create_only=True)
            self.managed[candidate] = _sha(data)
            self.created.append(candidate)
            return candidate
        raise ContractError("too many edited merge candidates")

    def write(self, relative, data, *, personal=False):
        path = safe_path(self.course, relative, must_exist=False)
        wanted = _sha(data)
        if not path.exists():
            _atomic_write(path, data, create_only=True)
            if not personal:
                self.managed[relative] = wanted
            self.created.append(relative)
            return relative
        if not path.is_file():
            raise ContractError("export file destination is not a regular file")
        if personal:
            self.unchanged.append(relative)
            return relative
        actual = hash_file(path)[0]
        if actual == wanted:
            self.managed[relative] = wanted
            self.unchanged.append(relative)
        elif self.managed.get(relative) == actual:
            _atomic_write(path, data, expected_sha=actual)
            self.managed[relative] = wanted
            self.updated.append(relative)
        else:
            candidate = self._candidate(relative, data)
            self.conflicts.append({"path": relative, "candidate": candidate})
            return candidate
        return relative


def _asset_page(bundle, asset, title, coverage, blocks, gaps, texts, image_paths, artifacts, metadata):
    properties = {
        "worldanalysis_view": "asset", "project_id": bundle["project_id"],
        "asset_id": asset["asset_id"], "asset_revision": asset["asset_revision"],
        "source_ref": asset["source_ref"], "source_kind": asset["source_binding"]["kind"],
        "bundle_id": bundle["bundle_id"], "publish_id": bundle["publish_id"],
        "bundle_revision": bundle["bundle_revision"], "media_type": asset["media_type"],
        "duration_ms": asset.get("duration_ms"), "inventory_state": coverage["enumeration_state"],
        "source_processing_status": coverage["processing_status"],
        "source_video_completeness": "not_established_by_export",
        "analysis_status": "not_assessed_by_export", "open_gap_count": sum(g["status"] in {"open", "requested"} for g in gaps),
        "origins": sorted({b["origin"] for b in blocks}),
    }
    if metadata.get("source_material_id"):
        properties["declared_source_material_id"] = metadata["source_material_id"]
    for name in ("declared_provider", "declared_file_id", "declared_video_duration_ms"):
        if name in metadata:
            properties[name] = metadata[name]
    if any(name in properties for name in ("declared_source_material_id", "declared_provider", "declared_file_id", "declared_video_duration_ms")):
        properties["declared_source_identity_status"] = "declared_unverified"
    lines = [_properties(properties), "# " + _literal(title), "",
             "[课程总览](../../课程总览.md) · [我的笔记](我的笔记.md)", "",
             "> 本页是已发布证据的阅读视图。处理状态仅针对证据包中列出的材料；导出不证明源视频全文或全部画面完整，也不登记人工／Agent 已读。", "",
             "## 来源与范围", "", "- 来源标识：" + _literal(asset["source_ref"]),
             "- 素材标识：" + _literal(asset["asset_id"]),
             "- 已发布版本：" + _literal(bundle["bundle_revision"]),
             "- 材料枚举状态：" + _literal(coverage["enumeration_state"]),
             "- 材料处理状态：" + _literal(coverage["processing_status"]),
             "- 原视频时长：" + (_time(asset["duration_ms"]) if asset.get("duration_ms") is not None else "未知／此素材未登记时长"),
             ""]
    if "declared_source_identity_status" in properties:
        lines += ["资料包内的视频标识／时长是提供者的申报信息，尚未核实网盘身份；本页来源绑定仍以正式证据记录为准。", ""]
    lines += ["## 内容与图像", ""]
    image_occurrences = 0
    for block in blocks:
        lines += ["### " + _literal(_location(block["locator"])) + " · " + ORIGINS[block["origin"]], "",
                  "块标识：" + _literal(block["block_id"]) + " · 状态：" + _literal(block["processing"]["status"]),
                  "定位精度：" + _literal(block["locator"]["precision"]), ""]
        if block["locator"].get("uncertainty_reason"):
            lines += ["定位说明：" + _literal(block["locator"]["uncertainty_reason"]), ""]
        text = texts[block["block_id"]]
        if text:
            lines += [_quote(text), ""]
        for artifact_id in block["artifact_ids"]:
            if artifact_id in image_paths:
                artifact = artifacts[artifact_id]
                role = {"keyframe": "源证据登记的关键帧", "page_render": "页面渲染", "image": "图片／笔记附图（不自动认定为原视频帧）"}[artifact["kind"]]
                lines += ["**" + role + "**", "", "![本地证据图片](../../" + image_paths[artifact_id] + ")", "",
                          "图像标识：" + _literal(artifact_id), ""]
                image_occurrences += 1
        if block.get("uncertainty"):
            lines += ["说明：" + _literal(block["uncertainty"]), ""]
        lines += ["^block-" + _key(block["block_id"]), ""]
    if not blocks:
        lines += ["该素材没有已发布内容块，不能据此认为材料为空或已完成。", ""]
    lines += ["## 缺口与待核实", ""]
    if not gaps:
        lines += ["当前证据包未登记该素材的缺口。此状态不外推为原视频全部信息完整。", ""]
    for gap in gaps:
        lines += ["- **" + _literal(gap["kind"]) + " / " + _literal(gap["status"]) + "**：" + _literal(gap["reason"]),
                  "  缺口标识：" + _literal(gap["gap_id"])]
    lines += ["", "本页由 Worldanalysis 生成。个人整理请写入“我的笔记”；若修改本页，再次导出会保留此稿并另存待合并版本。", ""]
    return "\n".join(lines).encode("utf-8"), image_occurrences


def export_vault(root, project_id, vault, *, bundle_id=None, allow_workspace_delivery=False):
    """Export a validated published Bundle; no cloud or business-state mutation."""
    check_id(project_id)
    if bundle_id is not None:
        check_id(bundle_id)
    root, vault = _separate_vault(root, vault, allow_workspace_delivery=allow_workspace_delivery)
    with _PublishedReader(root) as reader:
        bundle = reader.get_bundle(project_id, bundle_id)
        project_root = reader.project_root(project_id)
        project = reader.connection.execute("SELECT identity_json FROM projects WHERE project_id=?", (project_id,)).fetchone()
        title = json.loads(project[0]).get("topic") or project_id
        artifacts = {a["artifact_id"]: a for a in bundle["artifacts"]}
        images, image_artifacts, warnings = _image_map(project_root, bundle)
        budget = [0]
        texts = {b["block_id"]: _text_for_block(project_root, b, artifacts, budget) for b in bundle["blocks"]}
        pages, index_rows, occurrences = {}, [], 0
        for asset in bundle["assets"]:
            key = asset_key(asset)
            coverage = next(c for c in bundle["coverage"]["per_asset"] if asset_key(c["asset_ref"]) == key)
            blocks = [b for b in bundle["blocks"] if asset_key(b["asset_ref"]) == key]
            gaps = [g for g in bundle["gaps"] if g["gap_id"] in coverage["gap_ids"]]
            metadata = _asset_metadata(project_root, asset, artifacts)
            label = metadata.get("title") or _source_title(reader, project_id, asset)
            relative = "资料/" + _key(asset["asset_id"]) + "/资料.md"
            pages[relative], count = _asset_page(bundle, asset, label, coverage, blocks, gaps, texts, image_artifacts, artifacts, metadata)
            occurrences += count
            index_rows.append((asset, label, relative, coverage, len(gaps)))
        # All source verification/decoding precedes Vault creation. We copy only
        # allowlisted image artifacts, never media, raw originals or credentials.
        restricted = sum(a["access_class"] == "restricted_local" for a in bundle["artifacts"])
        media = sum(a["media_type"].startswith(("audio/", "video/")) for a in bundle["artifacts"])
        vault.mkdir(parents=True, exist_ok=True)
        reject_links(vault)
        course = vault / "Worldanalysis" / ("project-" + _key(project_id))
        reject_links(course)
        course.mkdir(parents=True, exist_ok=True)
        with _export_lock(course):
            state = _load_state(course, project_id)
            writer = _ViewWriter(course, state)
            # Image destinations must remain content-addressed. An edited image
            # is kept intact, with the correct bytes written to a merge candidate
            # and every generated page redirected to that candidate.
            remapped = {}
            for relative, artifact in images.items():
                data = _artifact_bytes(project_root, artifact, limit=MAX_IMAGE_BYTES)
                remapped[relative] = writer.write(relative, data)
            page_targets = {}
            for relative, data in pages.items():
                text = data.decode("utf-8")
                for original, replacement in remapped.items():
                    if original != replacement:
                        text = text.replace("(../../" + original + ")", "(../../" + replacement + ")")
                page_targets[relative] = writer.write(relative, text.encode("utf-8"))
                personal = str(PurePosixPath(relative).parent / "我的笔记.md")
                writer.write(personal, ("# 我的笔记\n\n[资料页](资料.md)\n\n此文件由你编辑；再次导出不会覆盖。\n\n").encode("utf-8"), personal=True)
            overview = [_properties({"worldanalysis_view": "course", "project_id": project_id,
                        "bundle_id": bundle["bundle_id"], "publish_id": bundle["publish_id"],
                        "bundle_revision": bundle["bundle_revision"], "coverage_scope": bundle["coverage"]["scope"],
                        "source_coverage_status": bundle["coverage"]["claimed_status"],
                        "source_video_completeness": "not_established_by_export", "analysis_status": "not_assessed_by_export"}),
                        "# " + _literal(title), "",
                        "本库展示已发布的文字和本地图片。没有复制完整视频／音频，没有请求平台生成新资料。", "",
                        "**范围：证据包列出的素材。全文与全部关键画面的覆盖、分析是否完成，不能由导出成功推断。**", "",
                        "- Bundle：" + _literal(bundle["bundle_id"]),
                        "- 发布标识：" + _literal(bundle["publish_id"]),
                        "- 源证据覆盖状态：" + _literal(bundle["coverage"]["claimed_status"]),
                        f"- 素材：{len(bundle['assets'])}；本地图像：{len(images)}；图像出现位置：{occurrences}",
                        f"- 未导出受限附件：{restricted}；未复制音视频附件：{media}", "",
                        "## 资料目录", "", "| 资料 | 类型 | 枚举 | 处理 | 缺口 |", "|---|---|---|---|---|"]
            for asset, label, relative, coverage, gap_count in index_rows:
                target = page_targets[relative]
                overview.append("| [" + _literal(label) + "](" + target + ") | " + _literal(asset["media_type"]) + " | " +
                                _literal(coverage["enumeration_state"]) + " | " + _literal(coverage["processing_status"]) + f" | {gap_count} |")
            overview += ["", "## 阅读与维护", "", "- 文字区分作者内容、原生提取、ASR、OCR、平台笔记和模型推断。",
                         "- 图片标签沿用源证据类型；平台附图不自动视为原视频关键帧。",
                         "- 时间只表示已发布内容的定位，本版不生成未经验证的播放器跳转链接。",
                         "- 点击或显示图片不会更新 Worldanalysis 的分析阅读回执。",
                         "- 个人内容写入各资料的“我的笔记”；程序页有修改冲突时保留原稿并提供候选。",
                         "- 导出期间请暂停编辑程序生成页；冲突检查不等于跨应用编辑锁。",
                         "- 历史视图不自动删除；本页目录仅列当前选定 Bundle 的素材。", ""]
            if writer.conflicts:
                overview += ["## 待合并", ""]
                for conflict in writer.conflicts:
                    overview.append("- 原稿 [" + _literal(conflict["path"]) + "](" + conflict["path"] + ")；新候选 [查看](" + conflict["candidate"] + ")")
                overview.append("")
            if warnings:
                overview += ["## 导出限制", "", *["- " + _literal(w) for w in warnings], ""]
            overview_target = writer.write("课程总览.md", "\n".join(overview).encode("utf-8"))
            new_state = {"schema": STATE_SCHEMA, "exporter_version": EXPORT_VERSION, "project_id": project_id,
                         "bundle_id": bundle["bundle_id"], "publish_id": bundle["publish_id"],
                         "managed_files": writer.managed, "conflicts": writer.conflicts,
                         "overview_path": overview_target}
            scan_secrets(new_state)
            state_path = safe_path(course, "worldanalysis-export.json", must_exist=False)
            state_data = canonical(new_state) + b"\n"
            if len(state_data) > MAX_STATE_BYTES:
                raise ContractError("export state exceeds size bound")
            if not state_path.exists():
                _atomic_write(state_path, state_data, create_only=True)
            elif state_path.read_bytes() != state_data:
                _atomic_write(state_path, state_data, expected_sha=hash_file(state_path)[0])
            return {"schema": "worldanalysis.obsidian-export-result.v1", "status": "partial" if warnings or writer.conflicts else "exported",
                    "project_id": project_id, "bundle_id": bundle["bundle_id"], "publish_id": bundle["publish_id"],
                    "bundle_revision": bundle["bundle_revision"], "course_path": str(course),
                    "overview_path": str(course / overview_target), "source_coverage_status": bundle["coverage"]["claimed_status"],
                    "analysis_status": "not_assessed_by_export", "asset_count": len(bundle["assets"]),
                    "image_count": len(images), "image_occurrence_count": occurrences,
                    "created_files": writer.created, "updated_files": writer.updated, "unchanged_files": writer.unchanged,
                    "conflicts": writer.conflicts, "omitted_restricted_artifacts": restricted,
                    "omitted_media_artifacts": media, "warnings": warnings}


def main(argv=None):
    parser = argparse.ArgumentParser(description="将正式发布的 Worldanalysis 证据导出为独立 Obsidian 阅读视图；不访问网络或改写原证据。")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--vault", type=Path, required=True, help="与源码及证据工作区分离的 Vault 目录")
    parser.add_argument("--bundle", help="可选：明确选择已发布的历史 Bundle")
    parser.add_argument("--allow-workspace-delivery", action="store_true",
                        help="显式允许仅 --root/_deliveries/Obsidian 及其子目录作为隔离阅读视图；其余嵌套仍拒绝")
    args = parser.parse_args(argv)
    try:
        result = export_vault(args.root, args.project, args.vault, bundle_id=args.bundle,
                              allow_workspace_delivery=args.allow_workspace_delivery)
    except (ContractError, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "failed", "reason": redact_text(str(exc))[0]}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
