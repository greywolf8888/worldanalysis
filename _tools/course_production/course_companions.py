"""Resolve and seal local companion files reachable from a course reading entry."""
from hashlib import sha256
from pathlib import PurePosixPath, PureWindowsPath
import re
from urllib.parse import unquote

from project import ENTRY, local_child, sha
from reader_review import markdown_segments


COMPANIONS = "配套资料"
ORIGINALS = "原始资料"
PREVIEW_MANIFEST = "companion-outputs.json"
LINK = re.compile(r"\]\((<[^<>\r\n]+>|[^)\r\n]+)\)")
COMMENT = re.compile(r"<!--[\s\S]*?-->")
WIKI = re.compile(r"\[\[[^\]\r\n]+\]\]")


def prose(body):
    return "\n".join(COMMENT.sub("", text) for code, text in markdown_segments(body) if not code)


def link_path(href):
    raw = href.removeprefix("<").removesuffix(">")
    filename, fragment, _ = raw.partition("#")
    if fragment and unquote(filename, errors="strict").casefold().endswith(".md"):
        raw = filename
    value = unquote(raw, errors="strict")
    if any(char in value for char in ("\\", "\0", "\r", "\n")):
        raise ValueError(f"课内资料入口路径无效 {href}")
    return value


def safe_parts(parts, href):
    if not parts or any(part in {"", ".", ".."} or ":" in part or part.endswith((".", " ")) for part in parts):
        raise ValueError(f"课内资料入口路径无效 {href}")
    return parts


def course_file_references(body):
    """Yield portable targets from the adjacent original and companion directories."""
    content = prose(body)
    if any(COMPANIONS in link for link in WIKI.findall(content)):
        raise ValueError("配套资料使用课内相对 Markdown 链接")
    for href in LINK.findall(content):
        raw = href.removeprefix("<").removesuffix(">")
        if raw.startswith("#"):
            continue
        value = unquote(raw)
        if not (value.startswith(("../", "..\\")) or value.lower().startswith("file:")
                or re.match(r"^[A-Za-z]:[/\\]", value)):
            continue
        value = link_path(href)
        parts = value.split("/")
        if len(parts) < 3 or parts[0] != ".." or parts[1] not in {ORIGINALS, COMPANIONS}:
            raise ValueError(f"课内资料入口超出本课目录 {href}")
        yield PurePosixPath(*safe_parts(parts[1:], href)).as_posix()


def nested_references(body, parent):
    content = prose(body)
    if WIKI.search(content):
        raise ValueError(f"配套资料内页使用相对 Markdown 链接 {parent}")
    for href in LINK.findall(content):
        raw = href.removeprefix("<").removesuffix(">")
        if raw.startswith("#") or raw.lower().startswith(("https://", "http://")):
            continue
        path = link_path(href)
        relative = PurePosixPath(*safe_parts(path.split("/"), href))
        yield (parent / relative).as_posix()


def companion_assets(root, body, *, draft=None):
    """Return only referenced files and their SHA-256, with drafts taking precedence."""
    pending = [name for name in course_file_references(body) if name.startswith(f"{COMPANIONS}/")]
    assets = {}
    while pending:
        name = pending.pop()
        if name in assets:
            continue
        candidate = local_child(draft, name) if draft is not None else None
        path = candidate if candidate is not None and candidate.exists() else local_child(root, name)
        if not path.is_file():
            raise ValueError(f"配套资料打不开 {name}")
        if path.suffix.casefold() == ".md":
            raw = path.read_bytes()
            pending.extend(nested_references(raw.decode("utf-8-sig"), PurePosixPath(name).parent))
            digest = sha256(raw).hexdigest()
        else:
            digest = sha(path)
        assets[name] = (path, digest)
    return dict(sorted(assets.items()))


def companion_hashes(root, body, *, draft=None):
    return {name: digest for name, (_, digest) in companion_assets(root, body, draft=draft).items()}


def verify_published_companions(root, publication):
    """Check the same reachable files against both the review snapshot and output record."""
    expected = publication.get("review", {}).get("snapshot", {}).get("companions", {})
    outputs = {}
    for name, digest in publication["outputs"].items():
        parts = PureWindowsPath(name).parts
        if not parts or parts[0] != COMPANIONS:
            continue
        if len(parts) < 2 or name != "/".join(safe_parts(parts, name)):
            raise ValueError(f"发布回执中的配套资料入口无效 {name}")
        outputs[name] = digest
    if expected != outputs or companion_hashes(root, publication["body"]) != outputs:
        raise ValueError("配套资料与正式验收版本不一致")
    return outputs


def image_outputs(publication):
    prefix = (ENTRY.parent / "原图").as_posix() + "/"
    return {name for name in publication["outputs"] if name.startswith(prefix)}
