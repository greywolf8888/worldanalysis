"""Extract the text and references of a local HTML handout without rendering it."""
from html.parser import HTMLParser
import re

from worldanalysis.documents import DEFAULT_LIMITS, _decode


HTML_SUFFIXES = {".htm", ".html"}
BLOCK_TAGS = {
    "article", "aside", "blockquote", "caption", "dd", "details", "div", "dl", "dt",
    "figcaption", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "main",
    "nav", "ol", "p", "pre", "section", "summary", "table", "title", "tr", "ul",
}
SECTION_TAGS = {"article", "section", "details"}
NON_TEXT_TAGS = {"script", "style", "template"}
VISUAL_TAGS = {"img", "video", "audio", "source", "iframe", "canvas", "svg", "object", "embed"}


class HandoutParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.gaps = []
        self.parts = []
        self.location = None
        self.sections = []
        self.skip = []
        self.references = []
        self.dynamic_reported = False

    def locator(self, **fields):
        return {"line": self.getpos()[0], "sections": [key for _, key in self.sections if key], **fields}

    def check_capacity(self):
        if len(self.blocks) + len(self.references) + len(self.gaps) >= DEFAULT_LIMITS["max_blocks"]:
            raise ValueError("HTML 的可读取段落或引用过多")

    def add(self, locator, text):
        self.check_capacity()
        self.blocks.append({"locator": locator, "text": text})

    def flush(self):
        value = re.sub(r"[ \t\r\f]+", " ", "".join(self.parts)).strip()
        if value:
            self.add(self.location, value)
        self.parts.clear()
        self.location = None

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag in NON_TEXT_TAGS:
                self.skip.append(tag)
            return
        attrs = dict(attrs)
        if tag in NON_TEXT_TAGS:
            if tag != "style" and not self.dynamic_reported:
                self.check_capacity()
                self.gaps.append({"locator": self.locator(element=tag),
                                  "reason": "页面有动态内容或模板，需复核实际可见状态"})
                self.dynamic_reported = True
            self.skip.append(tag)
            return
        if tag in BLOCK_TAGS or tag == "br":
            self.flush()
        if tag in SECTION_TAGS:
            self.sections.append((tag, attrs.get("id")))
        if tag in {"td", "th"} and self.location is not None:
            self.parts.append(" | ")
        if tag == "a" and attrs.get("href"):
            self.check_capacity()
            self.references.append({"locator": self.locator(element=tag, field="href"), "text": attrs["href"]})
        if tag in VISUAL_TAGS:
            location = self.locator(element=tag)
            description = " · ".join(value for field in ("alt", "title", "src", "srcset", "data")
                                     if (value := attrs.get(field)))
            if description:
                self.add(location, description)
            self.check_capacity()
            self.gaps.append({"locator": location, "reason": "需检查 HTML 链接或嵌入的原始视听对象"})

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag in BLOCK_TAGS:
            self.flush()
        if self.sections and self.sections[-1][0] == tag:
            self.sections.pop()

    def handle_data(self, data):
        if not self.skip:
            if self.location is None and data.strip():
                self.location = self.locator()
            self.parts.append(data)


def local_html(file):
    limit = DEFAULT_LIMITS["max_xml_bytes"]
    with file.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("HTML 超出可直接读取的大小")
    text, encoding = _decode(data)
    document = HandoutParser()
    document.feed(text)
    document.close()
    document.flush()
    blocks = document.blocks + document.references
    if not blocks:
        document.gaps.append({"reason": "未读到静态正文或链接，需检查原页面"})
    return {"processing_status": "partial" if document.gaps else "extracted",
            "blocks": blocks, "gaps": document.gaps, "metadata": {"encoding": encoding, "mode": "static_html"}}
