"""Use project-local format readers and stable source IDs to prepare the current course's documents."""
from codecs import BOM_UTF16_BE, BOM_UTF16_LE
from configparser import ConfigParser, Error as ConfigError
from pathlib import Path
import json
import subprocess
import tempfile
from zipfile import BadZipFile, ZipFile
from prepare_archive import ARCHIVE_SIGNATURES, read_archive
from prepare_html import HTML_SUFFIXES, local_html
from project import put, read, sha, write
from worldanalysis.documents import extract


INLINE_SUFFIXES = {".doc", ".xmind", ".url"} | HTML_SUFFIXES


def mindmap(file):
    with ZipFile(file) as z:
        if "content.json" not in z.namelist():
            return {"processing_status":"review","blocks":[],"gaps":[{"reason":"需要检查旧版思维导图的 XML"}]}
        item=z.getinfo("content.json")
        if item.file_size>16*1024**2:raise ValueError("思维导图超出可直接展开的大小")
        sheets=json.loads(z.read(item))
    blocks=[]
    for s in sheets:
        todo=[(s["rootTopic"],[])]
        while todo:
            node,prefix=todo.pop()
            chain=[*prefix,node.get("title","")]
            blocks.append({"locator":{"sheet":s.get("title"),"node":node.get("id"),"path":chain},
                           "text":" / ".join(chain)})
            for sub in node.get("children",{}).values():
                if isinstance(sub,list):todo.extend((v,chain) for v in reversed(sub))
    return {"processing_status":"extracted","blocks":blocks,"gaps":[]}


def legacy_doc(file):
    runner=Path(__file__).with_name("word_body.cjs")
    run=subprocess.run(["node",str(runner),str(file)],capture_output=True,timeout=45)
    if run.returncode:raise ValueError(run.stderr.decode("utf8","replace").strip()[:240])
    result=json.loads(run.stdout)
    blocks=[{"locator":{"part":part},"text":value} for part,value in result.items() if value and value.strip()]
    if blocks:return {"processing_status":"extracted","blocks":blocks,"gaps":[]}
    return {"processing_status":"review_visual","blocks":[],"gaps":[{"reason":"正文为空，待检查嵌图或版式"}]}


def internet_shortcut(file):
    """Read the locally supplied URL as evidence without opening it."""
    with file.open("rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("网址快捷方式超出可直接读取的大小")
    if data.startswith((BOM_UTF16_BE, BOM_UTF16_LE)):
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("gb18030")
    shortcut = ConfigParser(interpolation=None)
    try:
        shortcut.read_string(text)
        value = shortcut.get("InternetShortcut", "URL", fallback="").strip()
    except ConfigError as error:
        raise ValueError("无法读取网址快捷方式的原始字段") from error
    if not value or any(ord(char) < 32 for char in value):
        raise ValueError("网址快捷方式缺少可读的 URL 字段")
    return {"processing_status": "extracted",
            "blocks": [{"locator": {"part": "InternetShortcut", "field": "URL"}, "text": value}],
            "gaps": []}


def inspect_document(source, out, options):
    ext = source.suffix.lower()
    if ext == ".doc":
        return legacy_doc(source)
    if ext == ".xmind":
        return mindmap(source)
    if ext == ".url":
        return internet_shortcut(source)
    if ext in HTML_SUFFIXES:
        return local_html(source)
    out.mkdir(parents=True, exist_ok=True)
    return extract(source, out, options=options)


def prepare_documents(raw, records, meta, work):
    docs=work/"资料"
    docs.mkdir(parents=True,exist_ok=True)
    evidence=[]
    items = [row for row in records if "lesson" not in row]
    from worldanalysis.ocr import ocr_image
    from worldanalysis.pdf_render import render_pdf_page

    options = {"ocr": ocr_image, "render_pdf_page": render_pdf_page}
    for i,row in enumerate(items,1):
        source=raw/row["file"]
        key=row.get("sha256") or sha(source); ext=source.suffix.lower()
        cache_name = f"{key}-{ext.removeprefix('.') or 'file'}"
        if ext == ".url":
            cache_name += "-shortcut"
        if ext in HTML_SUFFIXES:
            cache_name += "-static"
        if ext in ARCHIVE_SIGNATURES:
            cache_name += "-members"
        cache=docs/(cache_name+".json")
        if cache.is_file():
            result=read(cache)
        else:
            try:
                if ext in INLINE_SUFFIXES:
                    result = inspect_document(source, None, options)
                else:
                    out = Path(tempfile.mkdtemp(prefix=key[:10]+"-", dir=docs))
                    if ext in ARCHIVE_SIGNATURES:
                        result = read_archive(source, out, lambda member, target: inspect_document(member, target, options))
                    else:
                        result = inspect_document(source, out, options)
                    result["asset_root"] = out.relative_to(work).as_posix()
            except (OSError, ValueError, BadZipFile, subprocess.TimeoutExpired) as error:
                result={"processing_status":"failed","blocks":[],"gaps":[{"reason":str(error)}]}
            if result["processing_status"]!="failed":put(cache,result)
        texts=[b for b in result["blocks"] if b.get("text")]
        item={"id":f"D{i:03}","file":row["file"],"sha256":key,"status":result["processing_status"],
              "text_blocks":len(texts),"text_chars":sum(len(b["text"]) for b in texts),
              "gaps":len(result["gaps"]),"result":cache.relative_to(work).as_posix() if cache.is_file() else None}
        if result["processing_status"] == "failed":
            item["gaps_detail"] = result["gaps"]
        evidence.append(item)
        # Save a readable locator-first view of each distinct document for the editorial pass.
        digest_text=docs/(cache_name+".md")
        if texts and not digest_text.exists():
            text="\n\n".join(f"{json.dumps(b.get('locator',{}),ensure_ascii=False)}\n{b['text']}" for b in texts)
            write(digest_text,text+"\n")
        if i%12==0 or i==len(items):
            put(meta/"documents-progress.json",{"total":len(items),"processed":i,"records":evidence})
            print(f"资料 {i}/{len(items)}，最近一份 {source.name}",flush=True)
    summary={"total":len(items),"records":evidence,"unique_blobs":len({r["sha256"] for r in evidence}),
             "text_files":sum(r["text_blocks"]>0 for r in evidence)}
    put(meta/"documents-summary.json",summary)
    return summary
