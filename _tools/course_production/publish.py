"""Compile and render a single textbook with original figures and hidden provenance."""
from html import escape
from pathlib import Path
import re
from urllib.parse import quote, unquote

from frames import diagram_source, document_image, orig
from project import ENTRY, copy_file, local_child, read, require_readable_videos, sha, write
from reader_review import markdown_segments
from transcript import stamp
from PIL import Image, ImageChops


REF = re.compile(r"\{\{(D?\d+)\|([^{}]+)\}\}")
REF_RUN = re.compile(rf"(?:\*对照\*[ \t\u00a0]*)?{REF.pattern}(?:[ \t\u00a0]*{REF.pattern})*")
ART = re.compile(r"\{\{img:([^{}|]+)\|(\d+)\}\}")
WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
LOCAL_LINK = re.compile(r"\]\(((?:\.\./|file:///)[^)]+)\)")
LOCAL_IMAGE = re.compile(r"!\[[^\]\n]+\]\(([^)\n]+)\)")
# Obsidian does not resolve encoded commas or at signs in local image paths.
LOCAL_PATH_SAFE = "/+@,"


def clock(seconds):
    minutes, second = divmod(round(seconds), 60)
    return f"{minutes:02}:{second:02}"


def timecode(value):
    parts = value.strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"应使用 mm:ss 或 hh:mm:ss {value}")
    result = 0
    for part in parts:
        result = result * 60 + float(part)
    return result


def figure_location(info, display_numbers):
    if "diagram" in info:
        spec = info["diagram"]
        start, end = spec["range_s"]
        return f"第 {display_numbers[spec['lesson']]} 讲 {stamp(start)}–{stamp(end)} · {spec['basis']}"
    if "document" in info:
        return f"《{Path(info['source']).name}》第 {info['page']} 页"
    hours, remainder = divmod(round(info["pts"] * 100), 360000)
    minutes, centiseconds = divmod(remainder, 6000)
    prefix = f"{hours:02}:" if hours else ""
    return f"原课第 {display_numbers[info['lesson']]} 讲 {prefix}{minutes:02}:{centiseconds / 100:05.2f}"


def hidden_source(kind, detail):
    # Consecutive hyphens are not safe inside an HTML comment in Obsidian.
    payload = escape(detail, quote=False).replace("--", "&#45;&#45;")
    return f"<!-- {kind} {payload} -->"


def check_figure(course, info, *, ledger, art_root):
    if "diagram" in info:
        original, frame, digest = diagram_source(course, ledger, art_root.parent, info["diagram"])
        if digest != info["diagram_sha256"]:
            raise ValueError(f"整理示意图已变动 {info['name']}")
    elif "document" in info:
        original, frame = document_image(
            course, info["document"], info["page"], info["scale"], ledger=ledger,
            full_slide=info.get("full_slide", False),
        )
    else:
        original, frame, pts = orig(course, info["lesson"], info["pts"], ledger=ledger)
        if abs(info["pts"] - pts) >= .00001:
            raise ValueError(f"原图时间偏移 {info['name']}")
    if original != info["source"]:
        raise ValueError(f"原图来源不符 {info['name']}")
    expected = frame.crop(info["crop"]).convert("RGB")
    with Image.open(local_child(art_root, info["name"])) as image:
        if image.size != expected.size or ImageChops.difference(image.convert("RGB"), expected).getbbox():
            raise ValueError(f"原图与原件的裁图不符 {info['name']}")


def verify_links(course, deliver, *, art_root):
    count = 0
    for file, text in deliver.items():
        for value in WIKILINK.findall(text):
            name, _, anchor = value.split("|", 1)[0].partition("#")
            if name:
                prefix = course.name + "/"
                if not name.startswith(prefix):
                    raise ValueError(f"{file} 的课程链接缺少本课路径 {value}")
                target = name[len(prefix):]
                if Path(target).suffix.lower() not in {".md", ".png", ".jpg", ".jpeg", ".webp"}:
                    target += ".md"
            else:
                target = file
            disk = (course / target).resolve()
            if not disk.is_relative_to(course) or (target not in deliver and not disk.is_file()):
                raise ValueError(f"{file} 的课内链接不可用 {value}")
            if anchor:
                target_body = deliver.get(target)
                prefix = r"(?m)\s" if anchor.startswith("^") else r"(?m)^#{1,6} "
                if target_body is None or not re.search(prefix + re.escape(anchor) + r"\s*$", target_body):
                    raise ValueError(f"{file} 的标题或块定位不存在 {value}")
            count += 1
        for raw in LOCAL_LINK.findall(text):
            if raw.startswith("file:///"):
                raise ValueError("课程包里的原件出处使用可携带的相对链接")
            file_path = ((course / file).parent / unquote(raw)).resolve()
            allowed = course / "原始资料"
            if not file_path.is_relative_to(allowed) or not file_path.is_file():
                raise ValueError(f"{file} 的资料入口不可用 {raw}")
            count += 1
        for raw in LOCAL_IMAGE.findall(text):
            asset = ((art_root / file).parent / unquote(raw)).resolve()
            if not asset.is_relative_to(art_root / "课件/原图") or not asset.is_file():
                raise ValueError(f"{file} 的插图不可用 {raw}")
            count += 1
    return count


def compile_textbook(course, work, ledger, *, art_root, order):
    media = read(ledger / "media-summary.json")
    require_readable_videos(media)
    movies = {row["lesson"]: row for row in media["videos"]}
    if not isinstance(order, list) or any(type(number) is not int for number in order) or (
            len(order) != len(movies) or set(order) != set(movies)):
        raise ValueError("讲次顺序须恰好包含全部视频讲号，各一次")
    movies = {number: movies[number] for number in order}
    display_numbers = {number: position for position, number in enumerate(order, 1)}
    docs = {row["id"]: row for row in read(ledger / "documents-summary.json")["records"]}
    figure_path = ledger / "figures.json"
    manifest = read(figure_path) if figure_path.is_file() else []
    figures = {Path(i["name"]).stem: i for i in manifest}
    if len(figures) != len(manifest):
        raise ValueError("原图名重复")

    cited_docs = set()
    used_figures = set()
    cited_lessons = set()

    def render(author_file):
        text = author_file.read_text("utf-8-sig")
        if not text.startswith("# "):
            raise ValueError(f"教材需要主标题 {author_file}")
        text = re.sub(r"(?m)^\*\*页内目录\*\*.*\n?", "", text)

        def reference(match):
            ordinal, label = match.groups()
            if ordinal.startswith("D"):
                if ordinal not in docs:
                    raise ValueError(f"找不到附件编号 {ordinal}")
                doc = docs[ordinal]
                if ordinal not in cited_docs and sha(course / "原始资料" / doc["file"]) != doc["sha256"]:
                    raise ValueError(f"引用附件已变动 {doc['file']}")
                cited_docs.add(ordinal)
                return f"{doc['file']} · {label}"
            else:
                number = int(ordinal)
                marks = [timecode(part) for part in label.split("–")]
                if number not in movies or len(marks) > 2 or marks != sorted(marks) or not all(
                        0 <= t <= movies[number]["duration_s"] + .01 for t in marks):
                    raise ValueError(f"第 {number} 讲时间定位无效 {label}")
                cited_lessons.add(number)
                return f"第 {display_numbers[number]} 讲 {label}"

        def illustration(match):
            stem, width = match.groups()
            if stem not in figures or int(width) <= 0:
                raise ValueError(f"找不到已取原图或图片宽度无效 {stem}")
            info = figures[stem]
            used_figures.add(stem)
            if "document" in info:
                doc = docs[info["document"]]
                if info["document"] not in cited_docs and sha(course / "原始资料" / doc["file"]) != doc["sha256"]:
                    raise ValueError(f"配图附件已变动 {doc['file']}")
                cited_docs.add(info["document"])
            display_width = min(int(width), 600)
            image_path = Path(info["name"]).relative_to("课件").as_posix()
            title = stem.removeprefix("总教材-").removeprefix("补图-")
            prefix = match.string[match.string.rfind("\n", 0, match.start())+1:match.start()]
            indent = prefix if prefix.isspace() else ""
            provenance = hidden_source("配图出处", f"{figure_location(info, display_numbers)} · {title}")
            caption = f"\n\n{indent}*根据原课整理 · {info['diagram']['caption']}*" if "diagram" in info else ""
            return f"![{title}|{display_width}]({quote(image_path, safe=LOCAL_PATH_SAFE)}){caption}\n\n{indent}{provenance}"

        def references(match):
            expanded = (reference(ref) for ref in REF.finditer(match[0]))
            return hidden_source("段落出处", " · ".join(expanded))

        output = []
        for is_code, part in markdown_segments(text):
            if not is_code:
                part = ART.sub(illustration, REF_RUN.sub(references, part))
                if re.search(r"\{\{(?:img:|D?\d+\|)", part):
                    raise ValueError(f"课件含未解析的来源标记 {author_file}")
            output.append(part)
        return "".join(output).rstrip()

    body = render(work / "教材.md")
    if not cited_lessons and not cited_docs:
        raise ValueError("学习正文需要音视频或文档出处")

    def link_title(text):
        return text.replace("[", r"\[").replace("]", r"\]")

    index = ["> [!info]- 原课与配套资料", ">"]
    for number, info in movies.items():
        embedded = info.get("embedded")
        raw = embedded["document"] if embedded else info["source"]
        title = Path(raw).name
        if embedded and embedded.get("container"):
            title = f"{title} · 档内 {' › '.join(embedded['member_chain'])}"
        elif embedded:
            pages = "、".join(str(page) for page in embedded["pages"])
            position = f"第 {pages} 页" if pages else "页码未定位"
            title = f"{title} · {position}内嵌视频"
        url = "../" + quote("原始资料/" + raw, safe=LOCAL_PATH_SAFE)
        suffix = " · 定位基于恢复副本" if info.get("recovery") else ""
        if info.get("kind") == "audio":
            suffix += " · 独立音频"
        index.append(f"> - 第 {display_numbers[number]} 讲 · [{link_title(title)}]({url}) · {clock(info['duration_s'])}{suffix}")
    if cited_docs:
        index.extend([">", "> **配套资料**", ">"])
        for doc in sorted(cited_docs):
            raw = docs[doc]["file"]
            url = "../" + quote("原始资料/" + raw, safe=LOCAL_PATH_SAFE)
            index.append(f"> - [{link_title(Path(raw).name)}]({url})")
    if re.search(r"(?m)^>\s*\[!info\][+-]?\s*原课与配套资料\s*$", body):
        raise ValueError("原课入口由编译器统一放在文末")
    deliver = {ENTRY.as_posix(): body + "\n\n" + "\n".join(index) + "\n"}
    verify_links(course, deliver, art_root=art_root)
    images = [info for stem, info in figures.items() if stem in used_figures]
    expected = {Path(info["name"]).relative_to("课件").as_posix() for info in images}
    if {unquote(raw) for raw in LOCAL_IMAGE.findall(body)} != expected:
        raise ValueError("教材配图需要使用已登记的原图标记")
    for image in images:
        check_figure(course, image, ledger=ledger, art_root=art_root)
    return deliver, {"document_refs": sorted(cited_docs)}, images


def render_preview(preview, deliver, images, art_root):
    for info in images:
        source = local_child(art_root, info["name"])
        copy_file(source, local_child(preview, info["name"]), sha(source))
    for filename, body in deliver.items():
        write(local_child(preview, filename), body)
