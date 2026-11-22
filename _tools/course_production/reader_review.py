"""Record the follow-along review of a compiled textbook and its inline figures."""
import hashlib
from html import unescape
from pathlib import Path
import re
from urllib.parse import unquote

from project import ENTRY, sha


ITEMS = {
    "input": "准备和输入",
    "operation": "入口和顺序",
    "setting": "参数或选择条件",
    "result": "产物和验收",
    "recovery": "卡点和修正",
}
IMAGE = re.compile(r"!\[[^\]\n]+\]\(([^)\n]+)\)")
FENCE = re.compile(r"^ {0,3}(\x60{3,}|~{3,})(.*)$")


def markdown_segments(body):
    """Keep source markers inside fenced examples literal."""
    fence = None
    lines = []
    for line in body.splitlines(keepends=True):
        mark = FENCE.match(line.rstrip("\r\n"))
        if not fence and mark:
            yield False, "".join(lines)
            lines = [line]
            fence = mark[1]
            continue
        lines.append(line)
        if fence and mark and mark[1][0] == fence[0] and len(mark[1]) >= len(fence) and not mark[2].strip():
            yield True, "".join(lines)
            fence, lines = None, []
    if lines:
        yield bool(fence), "".join(lines)


def chapters(body):
    sections = {}
    part = None
    fence = None
    for line in body.splitlines():
        mark = FENCE.match(line)
        if mark:
            token, rest = mark.groups()
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not rest.strip():
                fence = None
        elif not fence and line.startswith("## "):
            title = line[3:].strip()
            if title in sections:
                raise ValueError(f"跟做定位重复 {title}")
            part = sections[title] = []
            continue
        if part is not None:
            part.append(line)
    return {title: "\n".join(lines) for title, lines in sections.items()}


def validate_cases(body, cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError("跟做检查需要记录关键操作和原件依据")
    indexed = chapters(body)
    names = set()
    for case in cases:
        goal = case.get("goal", "").strip()
        if not goal or goal in names:
            raise ValueError("每项跟做任务需要有唯一名称")
        names.add(goal)
        content = indexed.get(case.get("chapter"))
        if content is None:
            raise ValueError(f"{goal} 找不到所核验的正文标题")
        for key, label in ITEMS.items():
            phrase = case.get("checks", {}).get(key)
            if not isinstance(phrase, str) or not phrase.strip() or phrase not in content:
                raise ValueError(f"{goal} 的{label}在核验章节中无法定位")
        sources = case.get("sources", [])
        source_text = unescape(content)
        if not sources or any(not isinstance(s, str) or not s.strip() or s not in source_text for s in sources):
            raise ValueError(f"{goal} 的原课时间、页码或单元格在该章节中无法定位")
        figures = {Path(unquote(path)).name for path in IMAGE.findall(content)}
        chosen = case.get("figures", [])
        if any(name not in figures for name in chosen):
            raise ValueError(f"{goal} 缺少核验过的配图")
        if not chosen and not case.get("visual_note", "").strip():
            raise ValueError(f"{goal} 需要记录配图或足以跟做的文字、表格依据")


def snapshot(course, body):
    parent = (course / ENTRY).parent
    images = {}
    for href in dict.fromkeys(IMAGE.findall(body)):
        relative = unquote(href)
        item = (parent / relative).resolve()
        if not item.is_relative_to((parent / "原图").resolve()) or not item.is_file():
            raise ValueError(f"跟做检查找不到课内原图 {relative}")
        images[relative] = sha(item)
    return {"text_sha256": hashlib.sha256(body.encode("utf8")).hexdigest(), "images": images}
