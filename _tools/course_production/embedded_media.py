"""Find and verify original video parts carried inside a slide deck."""
from hashlib import file_digest
from pathlib import Path, PurePosixPath
import posixpath
from shutil import copyfileobj
from tempfile import NamedTemporaryFile
from urllib.parse import unquote
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from project import VIDEO_EXTENSIONS, local_child, media_source, sha


DIRECTORY = "附件内嵌视频"
PRESENTATION = "ppt/presentation.xml"
RELATIONSHIP_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def relationships(package, part, members):
    folder, filename = posixpath.split(part)
    locator = posixpath.join(folder, "_rels", filename + ".rels")
    if locator not in members:
        return {}
    root = ElementTree.fromstring(package.read(locator))
    targets = {}
    for row in root:
        if row.get("TargetMode") == "External" or not row.get("Target"):
            continue
        target = unquote(row.get("Target"))
        kind = row.get("Type", "").rsplit("/", 1)[-1]
        absolute = posixpath.join(folder, target) if not target.startswith("/") else target.lstrip("/")
        targets[row.get("Id")] = (kind, posixpath.normpath(absolute))
    return targets


def slide_locations(package, members, clips):
    locations = {clip: [] for clip in clips}
    source_order = ElementTree.fromstring(package.read(PRESENTATION))
    deck_links = relationships(package, PRESENTATION, members)
    slides = source_order.findall("./{*}sldIdLst/{*}sldId")
    for page, slide in enumerate(slides, 1):
        kind, part = deck_links.get(slide.get(RELATIONSHIP_NS + "id"), ("", ""))
        if kind != "slide" or part not in members:
            continue
        video_parts = {target for kind, target in relationships(package, part, members).values()
                       if kind in ("video", "media")}
        for clip in video_parts.intersection(locations):
            locations[clip].append(page)
    return locations


def keep_member(package, member, output, digest):
    if output.is_file() and sha(output) == digest:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=output.parent, delete=False) as stream, package.open(member) as source:
            temporary = Path(stream.name)
            copyfileobj(source, stream)
        if sha(temporary) != digest:
            raise ValueError(f"提取期间附件内嵌视频发生变化 {member}")
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def collect_embedded_videos(raw, sources, *, work=None):
    videos = []
    for source in sources:
        if source["extension"] not in {".pptx", ".pptm"}:
            continue
        path = local_child(raw, source["file"])
        clips = set()
        try:
            with ZipFile(path) as package:
                members = set(package.namelist())
                clips = {name for name in members
                         if PurePosixPath(name).parent == PurePosixPath("ppt/media")
                         and PurePosixPath(name).suffix.lower() in VIDEO_EXTENSIONS}
                if not clips:
                    continue
                locations = slide_locations(package, members, clips)
                for member, pages in sorted(locations.items(), key=lambda pair: (not pair[1], pair[1], pair[0])):
                    extension = PurePosixPath(member).suffix.lower()
                    with package.open(member) as stream:
                        digest = file_digest(stream, "sha256").hexdigest()
                    relative = f"{DIRECTORY}/{digest}{extension}"
                    if work is not None:
                        keep_member(package, member, local_child(work, relative), digest)
                    videos.append({
                        "file": f"{source['file']}!{member}", "extension": extension,
                        "bytes": package.getinfo(member).file_size, "sha256": digest,
                        "embedded": {"document": source["file"], "document_sha256": source["sha256"],
                                     "member": member, "pages": pages, "work_file": relative},
                    })
        except BadZipFile as error:
            # Let the normal attachment inspection report a non-Office or damaged deck.
            if not clips:
                continue
            raise ValueError(f"幻灯片内嵌原件无法读取 {source['file']}") from error
        except (ElementTree.ParseError, KeyError) as error:
            raise ValueError(f"无法定位幻灯片内嵌视频页码 {source['file']}") from error
    return videos


def verify_embedded_videos(project, sources, records):
    expected = {(row["file"], row["sha256"]): row["embedded"]
                for row in collect_embedded_videos(project.raw, sources)}
    actual = {(row["source"], row.get("source_sha256") or row.get("sha256")): row["embedded"]
              for row in records if "embedded" in row}
    if expected != actual:
        raise ValueError("原附件内嵌视频与课件采用范围不一致，请运行 prepare 并复核对应幻灯片")
    for (source, digest), embedded in actual.items():
        media_source(project.raw, project.work, {"source": source, "sha256": digest, "embedded": embedded},
                     verify_original=True)
