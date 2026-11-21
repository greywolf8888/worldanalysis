"""Maintain the reading index from the notes actually present in the Obsidian vault."""
from datetime import datetime
from pathlib import Path
import re
from urllib.parse import quote

from project import file_lock, local_child, named_entry, write


CATALOG = "00 项目总览.md"
START = "<!-- Worldanalysis:course-catalog -->"
END = "<!-- /Worldanalysis:course-catalog -->"


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
        location = quote(entry.relative_to(vault).as_posix(), safe="/")
        lines.append(f"- [{label}]({location}) · {modified}")
    return "\n".join(lines).rstrip()


def refresh_catalog(vault):
    vault = Path(vault).resolve(strict=True)
    config = local_child(vault, ".obsidian")
    if not config.is_dir():
        raise ValueError(f"请选择实际使用的 Obsidian 库 {vault}")
    note = local_child(vault, CATALOG)
    with file_lock(local_child(config, "worldanalysis-catalog.lock"), blocking=True):
        original = note.read_bytes().decode("utf-8") if note.exists() else None
        if original is None:
            original = f"# 项目总览\n\n{START}\n{END}\n"
        else:
            if original.count(START) != 1 or original.count(END) != 1:
                return note
            if original.index(START) > original.index(END):
                return note
        prefix, remainder = original.split(START, 1)
        _, suffix = remainder.split(END, 1)
        newline = "\r\n" if "\r\n" in original else "\n"
        section = f"\n\n{catalog_body(vault)}\n\n".replace("\n", newline)
        write(note, f"{prefix}{START}{section}{END}{suffix}")
    return note
