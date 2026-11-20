"""Read course archive members offline using numbered files inside the work directory."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from threading import Event, Timer
from zipfile import BadZipFile

from project import put, sha
from worldanalysis.documents import DEFAULT_LIMITS, _safe_member


ARCHIVE_SIGNATURES = {
    ".rar": (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00"),
    ".7z": (b"7z\xbc\xaf\x27\x1c",),
}
MAX_CONVERSION_SECONDS = 60


def archive_tool():
    if os.name == "nt":
        system_tar = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
        if system_tar.is_file():
            return str(system_tar)
    tool = shutil.which("bsdtar")
    if not tool and sys.platform == "darwin":
        tool = "/usr/bin/tar"
    if not tool:
        raise ValueError("读取 RAR 或 7z 需要本机的 libarchive tar 或 bsdtar")
    return tool


def convert_to_tar(source, stream):
    # bsdtar copies archive entries to stdout. It never writes their stored paths.
    cap = DEFAULT_LIMITS["max_archive_uncompressed_bytes"] + 32 * 1024**2
    cmd = [archive_tool(), "-cf", "-", "--format", "pax", "--no-acls", "--no-xattrs",
           "--no-fflags", "@" + str(source.resolve())]
    with subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        timed_out = Event()

        def stop_after_timeout():
            timed_out.set()
            process.kill()

        timer = Timer(MAX_CONVERSION_SECONDS, stop_after_timeout)
        timer.start()
        try:
            while chunk := process.stdout.read(64 * 1024):
                if stream.tell() + len(chunk) > cap:
                    raise ValueError("压缩包展开超出工作区读取上限")
                stream.write(chunk)
            code = process.wait()
            if timed_out.is_set():
                raise ValueError("压缩包读取超时")
            if code:
                raise ValueError(f"本机归档工具无法完整读取压缩包（退出码 {code}）")
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            timer.join()
    stream.seek(0)


def read_archive(source, out, inspect_document):
    limits = DEFAULT_LIMITS
    if source.stat().st_size > limits["max_input_bytes"]:
        raise ValueError("压缩包超出直接读取大小上限")
    with source.open("rb") as original:
        if not original.read(8).startswith(ARCHIVE_SIGNATURES[source.suffix.lower()]):
            raise ValueError("压缩包文件头与扩展名不一致")
    members, blocks, gaps = [], [], []
    seen = set()
    total_bytes = 0
    files = out / "档内原件"
    try:
        with tempfile.TemporaryFile(dir=out) as stream:
            convert_to_tar(source, stream)
            files.mkdir()
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                for index, entry in enumerate(archive, 1):
                    if index > limits["max_archive_members"]:
                        gaps.append({"reason": "超过可逐件展开的成员数量上限"})
                        break
                    path = entry.name
                    location = {"kind": "archive_member", "member_path": path, "member_index": index}
                    member = {"path": path, "index": index, "size": entry.size}
                    members.append(member)
                    canonical = path.rstrip("/").casefold()
                    if not _safe_member(path) or canonical in seen:
                        reason = "档内名称不可安全定位或有重名"
                    elif entry.isdir():
                        member["status"] = "directory"
                        seen.add(canonical)
                        continue
                    elif not entry.isreg():
                        reason = "档内链接或特殊文件需另行核查"
                    elif (not 0 <= entry.size <= limits["max_member_bytes"] or
                          total_bytes + entry.size > limits["max_archive_uncompressed_bytes"]):
                        reason = "档内文件超出读取大小上限"
                    else:
                        reason = ""
                    seen.add(canonical)
                    if reason:
                        gaps.append({"locator": location, "reason": reason})
                        member["status"] = "review"
                        continue

                    total_bytes += entry.size
                    suffix = Path(path).suffix.lower()
                    safe_suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,12}", suffix) else ".bin"
                    target = files / f"{index:05}{safe_suffix}"
                    with archive.extractfile(entry) as content, target.open("xb") as output:
                        shutil.copyfileobj(content, output)
                    member.update(file=target.relative_to(out).as_posix(), sha256=sha(target))
                    asset_dir = out / "档内取材" / f"{index:05}"
                    try:
                        result = inspect_document(target, asset_dir)
                    except (OSError, ValueError, BadZipFile, subprocess.TimeoutExpired) as error:
                        result = {"processing_status": "failed", "blocks": [], "gaps": [{"reason": str(error)}]}
                    if asset_dir.is_dir():
                        result["asset_root"] = asset_dir.relative_to(out).as_posix()
                    details = asset_dir.with_suffix(".json")
                    put(details, result)
                    member.update(status=result["processing_status"], result=details.relative_to(out).as_posix())
                    for block in result["blocks"]:
                        if block.get("text"):
                            blocks.append({"locator": {**location, "member_locator": block.get("locator", {})},
                                           "text": block["text"]})
                    for gap in result["gaps"]:
                        gaps.append({"locator": location, "reason": gap["reason"]})
    except tarfile.TarError as error:
        raise ValueError(f"无法读取归档工具返回的成员数据：{error}") from error
    if not members:
        gaps.append({"reason": "没有读到档内成员，请核对原压缩包"})
    return {"processing_status": "partial" if gaps else "extracted", "blocks": blocks,
            "gaps": gaps, "archive_members": members}
