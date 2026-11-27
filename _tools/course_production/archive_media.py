"""Read archive indexes and stream requested members without unpacking the archive."""
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import shutil
from stat import S_IFMT, S_IFREG
import subprocess
import sys
import tarfile
from tempfile import TemporaryFile
from threading import Event, Timer
from zipfile import ZipFile

from worldanalysis.documents import _safe_member


ARCHIVE_SIGNATURES = {
    ".rar": (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00"),
    ".7z": (b"7z\xbc\xaf\x27\x1c",),
}
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
    seven_zip_name: str | None = None

    @property
    def suffix(self):
        return PurePosixPath(self.name).suffix.lower()


def archive_tool():
    if os.name == "nt":
        system_tar = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
        if system_tar.is_file():
            return str(system_tar)
    tool = shutil.which("bsdtar") or ("/usr/bin/tar" if sys.platform == "darwin" else None)
    if not tool:
        raise ValueError("读取 RAR 或 7z 需要本机的 libarchive tar、bsdtar 或 7-Zip")
    return tool


def seven_zip_tool():
    configured = os.environ.get("WORLDANALYSIS_7ZIP")
    if configured:
        executable = Path(configured)
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("WORLDANALYSIS_7ZIP 须指向本机 7-Zip 可执行文件的绝对路径")
        return str(executable)
    return shutil.which("7z") or shutil.which("7zz")


@contextmanager
def archive_output(command, timeout):
    with TemporaryFile() as errors, subprocess.Popen(command, stdin=subprocess.DEVNULL,
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


def list_seven_zip_members(path, tool):
    with archive_output([tool, "l", "-slt", "-sccUTF-8", "-bd", "--", str(path)], MAX_INDEX_SECONDS) as stream:
        data = stream.read(MAX_LIST_BYTES + 1)
        if len(data) > MAX_LIST_BYTES:
            raise ValueError("压缩包索引超出可登记大小")
    lines = data.decode("utf-8-sig").splitlines()
    if "----------" not in lines:
        raise ValueError("7-Zip 未返回可核对的成员清单")
    members = []
    fields = {}
    for line in [*lines[lines.index("----------") + 1:], ""]:
        if line:
            key, separator, value = line.partition(" = ")
            if not separator or key in fields:
                raise ValueError("7-Zip 成员清单含未能安全定位的名称或字段")
            fields[key] = value
            continue
        if not fields:
            continue
        stored = fields.get("Path", "")
        size = fields.get("Size", "")
        if not stored or not size.isascii() or not size.isdecimal():
            raise ValueError("7-Zip 成员清单缺路径或大小")
        name = stored.replace("\\", "/")
        attrs = fields.get("Attributes", "")
        directory = fields.get("Folder") == "+" or attrs.startswith("D") or name.endswith("/")
        unix_mode = attrs.rsplit(" ", 1)[-1]
        special = (any(fields.get(key, "") not in ("", "-") for key in ("Symbolic Link", "Hard Link", "Alternate Stream"))
                   or fields.get("Encrypted") == "+" or unix_mode[:1] in {"l", "p", "s", "b", "c"})
        members.append(Member(name.rstrip("/") + "/" if directory else name, int(size),
                              ordinary=not directory and not special, seven_zip_name=stored))
        if len(members) > MAX_MEMBERS:
            raise ValueError("压缩包成员数量超出可登记上限")
        fields = {}
    return members


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
        if tool := seven_zip_tool():
            return list_seven_zip_members(path, tool)
        with archive_output([archive_tool(), "-tf", str(path)], MAX_INDEX_SECONDS) as stream:
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

    if member.seven_zip_name is not None:
        tool = seven_zip_tool()
        if not tool:
            raise ValueError("需要与索引时相同的 7-Zip 读取档内成员")
        with archive_output([tool, "x", "-so", "-bd", "-spd", "-bsp0", "-i!" + member.seven_zip_name,
                             "--", str(archive)], MAX_MEMBER_SECONDS) as source:
            with target.open("xb") as output:
                copy_member(source, output, min(limit, member.size))
                if output.tell() != member.size:
                    raise ValueError("7-Zip 成员字节数与索引不一致")
        return

    # libarchive glob matching needs quoting. Verify the returned tar entry's name and
    # type before reading it to avoid treating a glob match or a link as a media file.
    pattern = "".join(f"[{char}]" if char in "[*?" else char for char in member.name)
    args = ["-cf", "-", "--format", "pax", "--no-acls", "--no-xattrs", "--no-fflags",
            "--include", pattern, "@" + str(archive)]
    found = False
    with archive_output([archive_tool(), *args], MAX_MEMBER_SECONDS) as stream:
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
