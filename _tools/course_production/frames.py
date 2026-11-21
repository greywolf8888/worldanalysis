"""Draw review sheets from native video frames, then keep the chosen original pixels."""
from io import BytesIO
import math
import posixpath
import re
import shutil
import subprocess
import tempfile
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from pathlib import Path, PurePosixPath

from prepare import stamp
from project import local_child, media_source, put, read, sha
import av
from PIL import Image, ImageChops, ImageDraw


def get_native_frame(path, seconds, origin=0):
    """Seek to the preceding keyframe, then decode to the requested presentation time."""
    with av.open(str(path), options={"protocol_whitelist": "file,pipe"}) as container:
        video = container.streams.video[0]
        container.seek(int((seconds + origin) / video.time_base), stream=video, backward=True)
        last = None
        for frame in container.decode(video):
            if frame.time is None:
                continue
            if frame.time - origin >= seconds - 1e-6:
                return frame.to_image(), round(frame.time - origin, 6)
            last = frame
        if last is not None:
            present_until = last.time - origin + float((last.duration or 0) * last.time_base)
            if seconds < present_until + 1e-6:
                return last.to_image(), round(last.time - origin, 6)
    raise ValueError(f"原片在该时间后没有画面 {path.name} {seconds}")


def orig(path, number, seconds, *, ledger):
    doc = read(ledger / f"{number:02}.json")
    src = media_source(path / "原始资料", ledger.parent / "work", doc)
    if not 0 <= seconds < doc["duration_s"]:
        raise ValueError(f"取图时间超出第 {number} 讲")
    img, pts = get_native_frame(src, seconds, doc.get("timeline_origin_s", 0))
    return doc["source"], img, pts


def slide_picture(filename,page):
    # Preserve a screenshot embedded in the exact slide, including native pixels.
    with ZipFile(filename) as book:
        def rels(part):
            folder,base=posixpath.split(part)
            table=posixpath.join(folder,"_rels",base+".rels")
            return {item.get("Id"):posixpath.normpath(posixpath.join(folder,item.get("Target",""))).lstrip("/")
                    for item in ET.fromstring(book.read(table)) if item.get("TargetMode")!="External"}
        relationship="{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
        presentation="ppt/presentation.xml"
        ids=ET.fromstring(book.read(presentation)).findall(".//{*}sldId")
        if not 1<=page<=len(ids):
            raise ValueError(f"幻灯片页码无效 {page}")
        slide=rels(presentation)[ids[page-1].get(relationship+"id")]
        pictures=ET.fromstring(book.read(slide)).findall(".//{*}pic")
        sources={node.get(relationship+"embed") for pic in pictures for node in pic.findall(".//{*}blip")}
        if len(sources)!=1 or None in sources:
            raise ValueError(f"第 {page} 页需有唯一可核实的内嵌原图")
        item=rels(slide)[sources.pop()]
        if not item.startswith("ppt/") or "media" not in PurePosixPath(item).parts[:-1]:
            raise ValueError("幻灯片图像必须来自文档内嵌文件")
        with Image.open(BytesIO(book.read(item))) as image:
            return image.copy()


def rendered_slide(filename, page):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        raise ValueError("完整 PPTX 页需要 Windows PowerPoint；仍可选取已核实的内嵌原图")
    runner = Path(__file__).with_name("render_slide.ps1")
    with tempfile.TemporaryDirectory(prefix="course-slide-") as temporary:
        output = Path(temporary) / "slide.png"
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-File", str(runner),
             "-Source", str(filename), "-Page", str(page), "-Output", str(output)],
            capture_output=True, timeout=90,
        )
        if result.returncode or not output.is_file():
            detail = result.stderr.decode("utf-8", "replace").strip()[-380:]
            raise ValueError(f"无法渲染完整幻灯片 {filename.name} 第 {page} 页。{detail}")
        with Image.open(output) as image:
            return image.copy()


def document_image(path, doc_id, page, scale, *, ledger, full_slide=False):
    import pypdfium2 as pdfium
    if not isinstance(page, int) or isinstance(page, bool) or page < 1 or not math.isfinite(scale) or not 0 < scale <= 4:
        raise ValueError("使用正整数页码，PDF 取图倍率须大于 0 且不超过 4")
    inventory=read(ledger / "documents-summary.json")["records"]
    source=next(v["file"] for v in inventory if v["id"]==doc_id)
    raw=(path/"原始资料").resolve()
    filename=local_child(raw, source)
    if full_slide:
        if filename.suffix.lower() != ".pptx":
            raise ValueError("完整幻灯片渲染只用于 PPTX")
        return source, rendered_slide(filename, page)
    if filename.suffix.lower()==".pdf":
        with pdfium.PdfDocument(filename) as pdf:
            if page > len(pdf):
                raise ValueError(f"页码超出 PDF 范围 {page}")
            source_page=pdf[page-1]
            try:return source,source_page.render(scale=scale).to_pil().copy()
            finally:source_page.close()
    if filename.suffix.lower()==".pptx":
        return source,slide_picture(filename,page)
    if page!=1:
        raise ValueError("原始图片只能引用第一页")
    with Image.open(filename) as image:
        return source,image.copy()


def save_sheet(cells, output):
    col = min(4,len(cells))
    h=max(im.height for im,_ in cells)+23
    board=Image.new("RGB",(col*250,((len(cells)+col-1)//col)*h),"white")
    draw=ImageDraw.Draw(board)
    for i,(img,line) in enumerate(cells):
        x=i%col*250;y=i//col*h
        board.paste(img,(x,y))
        draw.text((x+4,y+h-19),line,fill="black")
    board.save(output,quality=92)


def review_window(path, lesson, start, stop, step, label, crop=None, *, ledger, work):
    metadata = read(ledger / f"{lesson:02}.json")
    if not (0 <= start < stop <= metadata["duration_s"] and math.isfinite(step) and step > 0):
        raise ValueError("片段区间超出原课，或取图间隔无效")
    if Path(label).name != label or label in ("", ".", ".."):
        raise ValueError("片段标签需要是独立文件名")
    raw = (path / "原始资料").resolve()
    source = media_source(raw, work, metadata)

    folder = local_child(work / "源片段复核", label)
    folder.mkdir(parents=True, exist_ok=True)
    frames, cells, sheets = [], [], []
    target = start

    def flush():
        filename = f"{len(sheets) + 1:03}.jpg"
        save_sheet(cells, folder / filename)
        sheets.append(filename)
        cells.clear()

    origin = metadata.get("timeline_origin_s", 0)
    with av.open(str(source), options={"protocol_whitelist": "file,pipe"}) as container:
        video = container.streams.video[0]
        if crop and not (0 <= crop[0] < crop[2] <= video.width and 0 <= crop[1] < crop[3] <= video.height):
            raise ValueError("裁图位置超出原始画幅")
        container.seek(int((start + origin) / video.time_base), stream=video, backward=True, any_frame=False)
        for frame in container.decode(video):
            pts = None if frame.time is None else frame.time - origin
            if pts is None or pts + 1e-6 < target:
                continue
            if pts > stop + 1e-6:
                break
            native = frame.to_image()
            image = native.crop(crop) if crop else native
            image.thumbnail((246, 390))
            frames.append({"pts": round(pts, 6), "sheet": len(sheets) + 1,
                           "cell": len(cells) + 1, "encoded_key_frame": bool(frame.key_frame)})
            cells.append((image, f"L{lesson:02} · {stamp(pts)}"))
            if len(cells) == 16:
                flush()
            target = start + (math.floor((pts - start + 1e-6) / step) + 1) * step
    if not frames:
        raise ValueError("这段时间内没有原始帧")
    if cells:
        flush()
    put(folder / "片段.json", {"course": path.name, "lesson": lesson, "source": metadata["source"],
        "source_sha256": metadata["sha256"], "window": [start, stop], "step_s": step,
        "crop": crop, "sheets": sheets, "frames": frames,
        "cues": [c for c in metadata["cues"] if c["start"] < stop and c["end"] > start]})
    print(f"{path.name} 第 {lesson} 讲 · {len(frames)} 张候选画面 · {folder}")


def diagram_source(path, ledger, work, spec):
    from transcript import valid_span

    file = local_child(work, spec["file"])
    with Image.open(file) as image:
        if image.format != "PNG":
            raise ValueError("整理示意图须是已完成的 PNG")
        image = image.copy()
    meta = read(ledger / f"{spec['lesson']:02}.json")
    valid_span(spec["range_s"], meta["duration_s"])
    if not spec["basis"].strip() or not spec["caption"].strip():
        raise ValueError("整理示意图需要标明该处原课依据和图注")
    if sha(local_child(path / "原始资料", meta["source"])) != meta["sha256"]:
        raise ValueError("整理示意图对应的课程原件已变")
    return meta["source"], image, sha(file)


def keep(path, entries, append=False, *, ledger, output_root):
    output=local_child(output_root, "课件/原图")
    output.mkdir(exist_ok=True,parents=True)
    manifest=ledger/"figures.json"
    rows=read(manifest) if append and manifest.is_file() else []
    known={entry["name"]:entry for entry in rows}
    staged=[]
    for entry in entries:
        stem=entry["name"]
        if Path(stem).name!=stem or not stem.strip() or stem in (".", "..") or re.search(r'[\\/:*?"<>|\[\]{}]', stem):
            raise ValueError("图片名需要是普通文件名，且能作为 Markdown 图片标题")
        if "diagram" in entry:
            spec = entry["diagram"]
            src, img, digest = diagram_source(path, ledger, output_root.parent, spec)
            locator = {"diagram": spec, "diagram_sha256": digest}
        elif "document" in entry:
            doc=entry["document"];page=entry["page"];scale=entry.get("scale",1)
            full_slide = entry.get("full_slide", False)
            src,img=document_image(path,doc,page,scale,ledger=ledger,full_slide=full_slide)
            locator={"document":doc,"page":page,"scale":scale}
            if full_slide:
                locator["full_slide"] = True
        else:
            lesson=entry["lesson"]
            src,img,pts=orig(path,lesson,entry["time"],ledger=ledger)
            locator={"lesson":lesson,"pts":pts}
        box=entry.get("crop",[0,0,*img.size])
        x,y,right,bottom=box
        if not(0<=x<right<=img.width and 0<=y<bottom<=img.height):
            raise ValueError((stem,box,img.size))
        filepath=output/(stem+".png")
        record={"name":filepath.relative_to(output_root).as_posix(),"source":src,**locator,
                "original_wh":list(img.size),"crop":list(box)}
        previous=known.get(record["name"])
        crop=img.crop(box)
        if append and filepath.is_file():
            with Image.open(filepath) as current:
                if current.size!=crop.size or ImageChops.difference(current.convert("RGB"),crop.convert("RGB")).getbbox():
                    raise ValueError(f"已有图像与补图不同，请用另一名称 {filepath.name}")
        else:
            staged.append((filepath,crop))
        if previous is not None and previous!=record:
            raise ValueError(f"同名原图有不同定位 {filepath.name}")
        if previous is None:
            known[record["name"]]=record
            rows.append(record)
    for filepath,crop in staged:
        pending=filepath.with_suffix(".png.partial")
        crop.save(pending,format="PNG")
        pending.replace(filepath)
    put(manifest,rows)
    print("原图清单",len(rows),"份，本次写入",len(staged),"份")
