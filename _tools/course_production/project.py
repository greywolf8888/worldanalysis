"""Paths, immutable intake and atomic writes for a single local course."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from shutil import copy2
from tempfile import NamedTemporaryFile, TemporaryDirectory


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "整理结果/课程教材"
STATE = ".course"
ENTRY = Path("课件/00 开始学习.md")
RECOVERY_DIR = "媒体恢复"
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".mpg", ".mpeg",
    ".mts", ".m2ts", ".wmv", ".flv", ".ogv", ".ts",
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    if path.is_file() and path.read_bytes() == data:
        return
    pending = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            pending = Path(stream.name)
            stream.write(data)
        pending.replace(path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def put(path, data):
    write(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def copy_file(source, target, digest):
    if target.is_file() and sha(target) == digest:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        pending = Path(stream.name)
    try:
        copy2(source, pending)
        if sha(pending) != digest:
            raise ValueError(f"复制期间原件发生变化 {source}")
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)


def local_child(root, name):
    """Resolve a registered relative path without following links in the package."""
    relative = Path(name)
    if relative.anchor or not relative.parts or ".." in relative.parts:
        raise ValueError(f"无效的课内相对路径 {name}")
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink() or path.is_junction():
            raise ValueError(f"课内路径是链接或目录联接 {path}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"课内路径越界 {name}")
    return path


def named_entry(folder):
    return local_child(folder, ENTRY.parent / f"{folder.name}.md")


def recovery_files(work, relative, original_sha256, expected_sha256=None):
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError("须给出课内的媒体恢复清单路径")
    directory = local_child(work, RECOVERY_DIR)
    manifest = local_child(directory, relative)
    if not manifest.is_file() or (expected_sha256 and sha(manifest) != expected_sha256):
        raise ValueError(f"媒体恢复清单缺失或已变动 {relative}")
    spec = read(manifest)
    if (not isinstance(spec, dict) or spec.get("original_sha256") != original_sha256
            or not all(isinstance(spec.get(key), str) and spec[key].strip()
                       for key in ("file", "sha256", "method", "scope"))
            or "\n" in spec["scope"] or "\r" in spec["scope"]):
        raise ValueError(f"媒体恢复清单须对应原件摘要、恢复方法及可核对范围 {relative}")
    recovered = local_child(directory, spec["file"])
    if recovered.suffix.lower() not in VIDEO_EXTENSIONS or not recovered.is_file() or sha(recovered) != spec["sha256"]:
        raise ValueError(f"恢复视频缺失或已变动 {recovered.name}")
    return recovered, manifest, spec


def media_source(raw, work, record, *, verify_original=False):
    original = local_child(raw, record["source"])
    recovery = record.get("recovery")
    if (verify_original or recovery) and sha(original) != record["sha256"]:
        raise ValueError(f"原视频身份已变 {record['source']}")
    if recovery:
        return recovery_files(work, recovery["manifest"], record["sha256"], recovery["manifest_sha256"])[0]
    return original


def require_readable_videos(media):
    if unreadable := media.get("unreadable"):
        files = "、".join(row["source"] for row in unreadable)
        raise ValueError(f"原视频仍有取材缺口，补查后才能完成整课验收。待补原件 {files}")


def inventory_files(root):
    result = []
    if not root.is_dir():
        raise ValueError(f"找不到原件目录 {root}")

    def visit(folder):
        for path in folder.iterdir():
            if path.is_symlink() or path.is_junction():
                raise ValueError(f"原件目录包含链接，请先提供实际文件 {path}")
            if path.is_dir():
                visit(path)
            elif path.is_file():
                before = path.stat()
                digest = sha(path)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError(f"盘点期间原件发生变化 {path}")
                result.append({"file": path.relative_to(root).as_posix(), "bytes": before.st_size,
                               "extension": path.suffix.lower(), "sha256": digest})
            else:
                raise ValueError(f"原件包含非普通文件 {path}")

    visit(root)
    return sorted(result, key=lambda row: (row["file"].casefold(), row["file"]))


@contextmanager
def file_lock(path):
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass(frozen=True)
class CourseProject:
    folder: Path

    @property
    def state(self):
        return local_child(self.folder, STATE)

    @property
    def work(self):
        return local_child(self.state, "work")

    @property
    def ledger(self):
        return local_child(self.state, "ledger")

    @property
    def raw(self):
        return local_child(self.folder, "原始资料")

    @property
    def art(self):
        return local_child(self.work, "选用素材")

    @property
    def preview(self):
        return local_child(self.work, "阅读预览")

    @property
    def entry(self):
        legacy = local_child(self.folder, ENTRY)
        if legacy.is_file():
            return legacy
        named = named_entry(self.folder)
        return named if named.is_file() else legacy

    @property
    def intake(self):
        return self.ledger / "intake.json"

    def locked(self):
        return file_lock(self.state / "lock")

    def validate_raw(self):
        expected = read(self.intake)["sources"]
        if inventory_files(self.raw) != expected:
            raise ValueError("入库后的原件有增删或变更，请恢复原快照或新建另一份课程包")
        return expected

    @classmethod
    def open(cls, path):
        project = cls(path.resolve(strict=True))
        marker = project.state / "project.json"
        if not marker.is_file() or read(marker).get("schema") != 1:
            raise ValueError(f"请给出 start 创建的课程包目录 {project.folder}")
        if not project.intake.is_file() or not project.raw.is_dir():
            raise ValueError(f"课程尚未完成入库，请用 start 继续 {project.folder}")
        return project

    @classmethod
    def initialize(cls, source, output=None):
        source = source.resolve(strict=True)
        if not source.is_dir() or (source / STATE / "project.json").exists() or not source.name:
            raise ValueError("请提供未封装的课程原件文件夹")
        folder = (output or DEFAULT_OUTPUT / source.name).resolve()
        if folder.is_relative_to(source) or source.is_relative_to(folder):
            raise ValueError("课程原件和课程包需要处于两个独立目录")
        folder.mkdir(parents=True, exist_ok=True)
        project = cls(folder)
        if not (project.state / "project.json").is_file() and any(
            child.name != STATE for child in folder.iterdir()
        ):
            raise ValueError(f"请选择空目录作为课程包 {folder}")
        project.state.mkdir(exist_ok=True)
        marker = project.state / "project.json"
        with project.locked():
            if marker.is_file():
                identity = read(marker)
                if identity.get("schema") != 1 or Path(identity["origin"]) != source:
                    raise ValueError("同名课程包属于另一份原件，请换一个输出目录")
            else:
                if project.raw.exists() or project.entry.parent.exists() or any(
                    item.name != "lock" for item in project.state.iterdir()
                ):
                    raise ValueError("输出目录中已有未登记的资料，请换一个空目录")
                put(marker, {"schema": 1, "origin": str(source), "name": source.name})

            inputs = inventory_files(source)
            if not inputs:
                raise ValueError("课程原件文件夹为空")
            if project.intake.is_file():
                if read(project.intake)["sources"] != inputs:
                    raise ValueError("输入文件夹已变化，请为更新版使用新的输出目录")
                project.validate_raw()
                return project
            if project.raw.exists():
                if inventory_files(project.raw) != inputs:
                    raise ValueError("中断的原件快照与输入不一致，请换一个输出目录")
            else:
                with TemporaryDirectory(prefix="intake-", dir=project.state) as scratch:
                    stage = Path(scratch) / "原始资料"
                    for record in inputs:
                        rel = record["file"]
                        copy_file(local_child(source, rel), local_child(stage, rel), record["sha256"])
                    stage.replace(project.raw)
            put(project.intake, {"sources": inputs})
        return project
