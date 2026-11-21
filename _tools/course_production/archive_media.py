"""Read archive indexes and stream requested members without unpacking the archive."""
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import PurePosixPath
from stat import S_IFMT, S_IFREG
import subprocess
import tarfile
from tempfile import TemporaryFile
from threading import Event, Timer
from zipfile import ZipFile

from prepare_archive import ARCHIVE_SIGNATURES, archive_tool
from worldanalysis.documents import _safe_member


ARCHIVES = frozenset({".zip", *ARCHIVE_SIGNATURES})
MAX_MEMBERS = 100_000
MAX_LIST_BYTES = 32 * 1024**2
MAX_MEMBER_BYTES = 8 * 1024**3
MAX_INDEX_SECONDS = 60
MAX_MEMBER_SECONDS = 900


@dataclass(frozen=True)
class Member:
    name: str
    size: int | None = None
    ordinary: bool = True

    @property
    def suffix(self):
        return PurePosixPath(self.name).suffix.lower()


@contextmanager
def archive_output(arguments, timeout):
    with TemporaryFile() as errors, subprocess.Popen([archive_tool(), *arguments], stdin=subprocess.DEVNULL,
                                                       stdout=subprocess.PIPE, stderr=errors) as process:
        expired = Event()

        def stop():
            if process.poll() is None:
                expired.set()
                process.kill()

        timer = Timer(timeout, stop)
        timer.start()
        try:
            yield process.stdout
            code = process.wait()
            if expired.is_set():
                raise ValueError("归档工具读取超时")
            if code:
                errors.seek(0)
                detail = errors.read(1024).decode("mbcs" if os.name == "nt" else "utf-8", errors="replace").strip()
                detail = " ".join(detail.splitlines())
                raise ValueError(f"归档工具无法完整读取文件（退出码 {code}）{f'：{detail}' if detail else ''}")
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            timer.join()


def list_members(path):
    """List large archives by index, independent of the document expansion budget."""
    suffix = path.suffix.lower()
    if suffix == ".zip":
        with ZipFile(path) as package:
            entries = package.infolist()
            if len(entries) > MAX_MEMBERS:
                raise ValueError("压缩包成员数量超出可登记上限")
            members = [Member(row.filename, row.file_size,
                              not row.is_dir() and S_IFMT(row.external_attr >> 16) in (0, S_IFREG))
                       for row in entries]
    else:
        with path.open("rb") as source:
            if not source.read(8).startswith(ARCHIVE_SIGNATURES[suffix]):
                raise ValueError("压缩包文件头与扩展名不一致")
        with archive_output(["-tf", str(path)], MAX_INDEX_SECONDS) as stream:
            data = stream.read(MAX_LIST_BYTES + 1)
            if len(data) > MAX_LIST_BYTES:
                raise ValueError("压缩包索引超出可登记大小")
        # Windows' built-in libarchive emits file names using the Windows ANSI code page.
        names = data.decode("mbcs" if os.name == "nt" else "utf-8").splitlines()
        if len(names) > MAX_MEMBERS:
            raise ValueError("压缩包成员数量超出可登记上限")
        members = [Member(name, ordinary=not name.endswith("/")) for name in names]
    return members


def readable_members(members):
    names = Counter(row.name.rstrip("/").casefold() for row in members)
    return {row.name: row for row in members if row.ordinary and _safe_member(row.name)
            and names[row.name.casefold()] == 1}


def copy_member(source, output, limit):
    remaining = limit
    while chunk := source.read(min(1024**2, remaining + 1)):
        if len(chunk) > remaining:
            raise ValueError("档内成员超出可用读取上限")
        output.write(chunk)
        remaining -= len(chunk)


def extract_member(archive, member, target, *, limit=MAX_MEMBER_BYTES):
    """Materialize just one verified regular member at a caller-chosen local path."""
    if not member.ordinary or not _safe_member(member.name):
        raise ValueError("档内成员不可安全定位")
    if member.size is not None and member.size > limit:
        raise ValueError("档内成员超出可用读取上限")
    if archive.suffix.lower() == ".zip":
        with ZipFile(archive) as package, package.open(member.name) as source, target.open("xb") as output:
            copy_member(source, output, limit)
        return

    # libarchive glob matching needs quoting. Verify the returned tar entry's name and
    # type before reading it to avoid treating a glob match or a link as a media file.
    pattern = "".join(f"[{char}]" if char in "[*?" else char for char in member.name)
    args = ["-cf", "-", "--format", "pax", "--no-acls", "--no-xattrs", "--no-fflags",
            "--include", pattern, "@" + str(archive)]
    found = False
    with archive_output(args, MAX_MEMBER_SECONDS) as stream:
        with tarfile.open(fileobj=stream, mode="r|") as packet:
            for entry in packet:
                if found or entry.name != member.name or not entry.isreg() or entry.size > limit:
                    raise ValueError("归档工具返回的成员路径、类型或大小不一致")
                with packet.extractfile(entry) as source, target.open("xb") as output:
                    copy_member(source, output, limit)
                found = True
        if len(stream.read(MAX_LIST_BYTES + 1)) > MAX_LIST_BYTES:
            raise ValueError("归档工具返回了额外数据")
    if not found:
        raise ValueError("归档工具未返回要求的成员")
