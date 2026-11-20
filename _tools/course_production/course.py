"""Prepare, write, and accept one textbook from a local course folder."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project import CourseProject, ENTRY, ROOT, local_child, put, read, sha

sys.path.insert(0, str(ROOT))

ORDER = "讲次顺序.json"


def gpu_from_args(args, backend, configured=None):
    if getattr(args, "cpu", False):
        if args.gpu_exe or args.model:
            raise ValueError("请在 --cpu 和 GPU 路线中选择一种")
        return None
    if configured and not (args.gpu_exe or args.model):
        if configured["backend"] == "cpu":
            return None
        exe, model = configured["exe"], configured["model"]
    else:
        exe = args.gpu_exe or os.environ.get("WORLDANALYSIS_COURSE_ASR_EXE")
        model = args.model or os.environ.get(f"WORLDANALYSIS_COURSE_{backend.upper()}_MODEL")
        if not model and backend == "sensevoice" and not args.gpu_exe:
            return None
    if not exe or not model:
        raise ValueError("GPU 转写需要同时指定本地 CrispASR 可执行文件和相应权重")
    from gpu_asr import VulkanASR

    device = getattr(args, "device", None)
    if device is None and configured:
        device = configured.get("device")
    return VulkanASR(exe, model, backend, device)


def prepare(project, args):
    from prepare import classify, prepare_project_media
    from prepare_documents import prepare_documents
    from review_media import scan_project

    sources = project.validate_raw()
    videos = classify(sources)
    put(project.ledger / "inventory.json", {"sources": sources})
    documents = prepare_documents(project.raw, sources, project.ledger, project.work)
    project.work.mkdir(parents=True, exist_ok=True)
    runtime = project.ledger / "asr-settings.json"
    preferred = read(runtime) if runtime.is_file() else None
    gpu = gpu_from_args(args, "sensevoice", preferred) if videos else None
    if gpu:
        put(runtime, {"backend": "sensevoice", "exe": str(gpu.executable), "model": str(gpu.model),
                      "device": gpu.device["index"]})
    elif videos and args.cpu:
        put(runtime, {"backend": "cpu"})
    media = prepare_project_media(project, videos, documents["records"], gpu)
    scan_project(project, media["videos"], args.workers)
    from transcript import update_timeline

    stale = []
    for row in media["videos"]:
        try:
            update_timeline(project, row["lesson"])
        except (ValueError, OSError) as error:
            stale.append(str(error))
    order = project.work / ORDER
    if not order.exists():
        put(order, [row["lesson"] for row in videos])
    if stale:
        raise ValueError("；".join(stale))
    print(f"已准备 {len(videos)} 个原视频和 {documents['total']} 件附件")
    print(f"原件 {project.raw}")
    print(f"编辑与取证 {project.work}")
    print(f"工作稿 {project.work / '教材.md'}")


def preview(project):
    from acceptance import TEMPLATE, prepare_review
    from publish import compile_textbook, render_preview

    project.validate_raw()
    order = read(project.work / ORDER)
    deliver, receipt, figures = compile_textbook(
        project.folder, project.work, project.ledger, art_root=project.art, order=order)
    body = deliver[ENTRY.as_posix()]
    render_preview(project.preview, deliver, figures, project.art)
    template = prepare_review(project, body, order, receipt)
    put(project.work / TEMPLATE, template)
    return body, figures, template


def accept(project, obsidian_exe):
    from acceptance import REVIEW, commit, validate_review, validate_review_proofs, verify_publication
    from reader_review import snapshot

    body, figures, template = preview(project)
    review_path = project.work / REVIEW
    if not review_path.is_file():
        raise ValueError(f"请先逐集对照原片并只读走查，填写 {review_path}")
    review = read(review_path)
    validate_review(body, figures, review, template)
    validate_review_proofs(project, review)
    env = dict(os.environ)
    if obsidian_exe:
        env["OBSIDIAN_EXE"] = str(obsidian_exe.resolve(strict=True))
    node = shutil.which("node")
    if not node:
        raise ValueError("Obsidian 阅读验收需要可调用的 Node.js")
    subprocess.run([node, str(ROOT / "_tools/course_production/verify_obsidian.cjs"),
                    "--project", str(project.folder), str(project.preview)], env=env, check=True)
    render = read(project.preview / "obsidian-render.json")
    current = (project.preview / ENTRY).read_text(encoding="utf-8")
    if current != body or snapshot(project.preview, current) != {
        key: template["snapshot"][key] for key in ("text_sha256", "images")
    } or any(sha(local_child(project.state, name)) != digest
             for name, digest in template["snapshot"]["evidence"].items()):
        raise ValueError("阅读检查期间预览或配图已修改，请重新验收")
    validate_review_proofs(project, review)
    project.validate_raw()
    commit(project, body, figures, review, render)
    return verify_publication(project, raw_verified=True)


def show_status(project):
    media = project.ledger / "media-summary.json"
    inventory = read(project.intake)
    print(json.dumps({
        "project": str(project.folder), "original_files": len(inventory["sources"]),
        "prepared_videos": len(read(media)["videos"]) if media.is_file() else None,
        "work": str(project.work),
        "draft": str(project.work / "教材.md"),
        "preview": str(project.preview / ENTRY) if (project.preview / ENTRY).is_file() else None,
        "published_entry": str(project.entry) if project.entry.is_file() else None,
    }, ensure_ascii=False, indent=2))


def preparation_options(parser):
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=3, metavar="1–8")
    parser.add_argument("--gpu-exe", type=Path, help="可选本地 CrispASR Vulkan 可执行文件")
    parser.add_argument("--model", type=Path, help="可选 SenseVoiceSmall GGUF 权重")
    parser.add_argument("--device", type=int, help="CrispASR 诊断显示的 Vulkan 序号")
    parser.add_argument("--cpu", action="store_true", help="切换到本地 CPU 转写")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    start = commands.add_parser("start", help="从用户提供的文件夹入库，提取文字、转写和画面候选")
    start.add_argument("source", type=Path)
    start.add_argument("--output", type=Path, help="独立课程包完整路径")
    preparation_options(start)
    rerun = commands.add_parser("prepare", help="用入库快照续作或应用媒体设置")
    rerun.add_argument("project", type=Path)
    preparation_options(rerun)
    for name, help_text in (
        ("status", "查看课程包内的当前产物"),
        ("preview", "编译一篇阅读稿和逐集验收填写模板"),
        ("accept", "绑定原片复核和真实阅读检查后发布"),
        ("verify", "复验已发布正文、配图、原件及回执"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("project", type=Path)
        if name == "accept":
            command.add_argument("--obsidian-exe", type=Path)
    window = commands.add_parser("window", help="连续解码原片小窗以核对菜单和同屏变化")
    window.add_argument("project", type=Path)
    window.add_argument("lesson", type=int)
    window.add_argument("start", type=float)
    window.add_argument("end", type=float)
    window.add_argument("--label", required=True)
    window.add_argument("--step", type=float, default=.1)
    window.add_argument("--crop", nargs=4, type=int)
    keep = commands.add_parser("keep", help="从原视频或附件登记真实 PNG")
    keep.add_argument("project", type=Path)
    keep.add_argument("picks", type=Path, help="JSON 取图清单")
    refine = commands.add_parser("refine", help="用本地 Whisper Turbo 或 Qwen3-ASR 独立复识候选")
    refine.add_argument("project", type=Path)
    refine.add_argument("--gpu-exe", type=Path)
    refine.add_argument("--model", type=Path)
    refine.add_argument("--device", type=int)
    refine.add_argument("--backend", choices=("whisper", "qwen3"), default="whisper")
    refine.add_argument("--lesson", type=int)
    refine.add_argument("--range", nargs=2, action="append", type=float, metavar=("START", "END"),
                        help="回查字幕区间内的争议，可多次提供，单位秒")
    reading = commands.add_parser("read", help="按时间顺序输出当前采用稿与画面候选")
    reading.add_argument("project", type=Path)
    reading.add_argument("lesson", type=int)
    reading.add_argument("--start", type=float, default=0)
    reading.add_argument("--end", type=float)
    reading.add_argument("--observations", action="append", default=[],
                         help="可多次引用观察 JSON，使用相对于 .course/work 的路径")
    adopt = commands.add_parser("adopt", help="把经原课和复识对照确认的补识纳入采用稿")
    adopt.add_argument("project", type=Path)
    adopt.add_argument("lesson", type=int)
    adopt.add_argument("decisions", type=Path)
    visual = commands.add_parser("observe", help="按呈现时间打开原片帧，可对局部做时序 OCR")
    visual.add_argument("project", type=Path)
    visual.add_argument("lesson", type=int)
    visual.add_argument("start", type=float)
    visual.add_argument("end", type=float)
    visual.add_argument("--step", type=float, default=.5)
    visual.add_argument("--crop", nargs=4, type=int)
    visual.add_argument("--ocr", action="store_true")
    args = parser.parse_args(argv)

    try:
        project = (CourseProject.initialize(args.source, args.output) if args.action == "start"
                   else CourseProject.open(args.project))
        if args.action == "status":
            show_status(project)
            return 0
        with project.locked():
            if args.action in ("start", "prepare"):
                prepare(project, args)
            elif args.action == "window":
                from frames import review_window

                review_window(project.folder, args.lesson, args.start, args.end, args.step, args.label, args.crop,
                              ledger=project.ledger, work=project.work)
            elif args.action == "keep":
                from frames import keep as keep_frames

                keep_frames(project.folder, read(args.picks), append=True,
                            ledger=project.ledger, output_root=project.art)
            elif args.action == "refine":
                from gpu_asr import refine_candidates

                project.validate_raw()
                refine_candidates(project, gpu_from_args(args, args.backend), args.lesson, args.range)
                print(project.work / "媒体补查/语音复识.json")
            elif args.action == "adopt":
                from transcript import timeline_paths, update_timeline

                update_timeline(project, args.lesson, read(args.decisions))
                print(timeline_paths(project, args.lesson)[0])
            elif args.action == "read":
                from transcript import reading_packet

                print(json.dumps(reading_packet(project, args.lesson, args.start, args.end, args.observations),
                                 ensure_ascii=False, indent=2))
            elif args.action == "observe":
                from visual import observe

                print(observe(project, args.lesson, args.start, args.end, args.step, args.crop, args.ocr))
            elif args.action == "preview":
                preview(project)
                print(project.preview / ENTRY)
                print(project.work / "验收填写模板.json")
            else:
                from acceptance import verify_publication

                record = accept(project, args.obsidian_exe) if args.action == "accept" else verify_publication(project)
                print(json.dumps({"verified": True, "entry": str(project.entry),
                                  "videos": len(record["review"]["episodes"]),
                                  "attachments": len(record["review"]["documents"]),
                                  "images": len(record["outputs"]) - 1}, ensure_ascii=False, indent=2))
        return 0
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", "replace")[-1500:] if isinstance(error.stderr, bytes) else ""
        print(f"课程阶段未完成：本地工具返回 {error.returncode}。{detail}", file=sys.stderr)
    except (OSError, ValueError, RuntimeError, LookupError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"课程阶段未完成：{error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
