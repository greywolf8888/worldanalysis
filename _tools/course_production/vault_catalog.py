"""Maintain the reading index from the notes actually present in the Obsidian vault."""
from datetime import datetime
from os.path import relpath
from pathlib import Path
import re
from urllib.parse import quote

from project import file_lock, local_child, named_entry, write


LEGACY_CATALOG = "00 项目总览.md"
CATALOG = Path("00 入口") / LEGACY_CATALOG
START = "<!-- Worldanalysis:course-catalog -->"
END = "<!-- /Worldanalysis:course-catalog -->"
HEADING = "# 项目总览"


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
                yield entry.stat().st_mtime_ns, entry
            except FileNotFoundError:
                continue


def catalog_body(vault):
    entries = list(published_notes(vault))
    entries.sort(key=lambda item: (-item[0], item[1].stem.casefold(), item[1].name))
    lines = [
        f"已发布 **{len(entries)}** 个项目。教材发布后自动加入本页，最近更新在前。",
        "",
    ]
    for stamp, entry in entries:
        modified = datetime.fromtimestamp(stamp / 1_000_000_000).astimezone().strftime("%m-%d %H:%M:%S")
        label = re.sub(r"([\\\[\]`*_~])", r"\\\1", entry.stem)
        location = quote(Path(relpath(entry, vault / CATALOG.parent)).as_posix(), safe="/")
        lines.append(f"- [{label}]({location}) · {modified}")
    return "\n".join(lines).rstrip()


def managed_parts(text):
    if text.count(START) != 1 or text.count(END) != 1 or text.index(START) > text.index(END):
        return None
    prefix, remainder = text.split(START, 1)
    _, suffix = remainder.split(END, 1)
    return prefix, suffix


def refresh_catalog(vault):
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
        section = f"\n\n{catalog_body(vault)}\n\n".replace("\n", newline)
        write(note, f"{prefix}{START}{section}{END}{suffix}")
        if generated_legacy and legacy.is_file() and legacy.read_bytes() == legacy_bytes:
            legacy.unlink()
    return note
