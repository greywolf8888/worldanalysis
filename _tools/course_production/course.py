"""Prepare and review local textbooks and resume ordered course batches."""
import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project import CourseProject, ENTRY, ROOT, ReceivedCoverageError, local_child, put, read, require_readable_videos, sha

sys.path.insert(0, str(ROOT))

ORDER = "讲次顺序.json"
RECEIVED_REVIEW = "实收核对.json"


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
    from media_inventory import collect_course_media
    from prepare import prepare_project_media
    from prepare_documents import prepare_documents
    from review_media import scan_project

    sources = project.validate_raw()
    selection = collect_course_media(project, sources)
    videos = selection["selected"]
    loose = {row["file"]: row["lesson"] for row in videos if "embedded" not in row}
    sources = [dict(row, lesson=loose[row["file"]]) if row["file"] in loose else row for row in sources]
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
    media = prepare_project_media(project, videos, documents["records"], gpu,
                                  routing_unresolved=selection["unresolved"])
    scan_project(project, media["videos"], args.workers)
    from transcript import update_timeline

    stale = []
    for row in media["videos"]:
        try:
            update_timeline(project, row["lesson"])
        except (ValueError, OSError) as error:
            stale.append(str(error))
    order = project.work / ORDER
    numbers = [row["lesson"] for row in videos]
    if order.exists():
        previous = read(order)
        known = set(selection["lesson_ids"].values())
        if (not isinstance(previous, list) or any(type(number) is not int or number not in known for number in previous)
                or len(set(previous)) != len(previous)):
            raise ValueError("讲次顺序包含未登记或重复的讲号，请核对原件身份")
        updated = [number for number in previous if number in numbers]
        updated.extend(number for number in numbers if number not in previous)
        if updated != previous:
            put(order, updated)
    else:
        put(order, numbers)
    require_readable_videos(media)
    if stale:
        raise ValueError("；".join(stale))
    print(f"已准备 {len(videos)} 个教学媒体（其中独立音频 {sum(row.get('kind') == 'audio' for row in videos)} 个，"
          f"内嵌或档内 {sum('embedded' in row for row in videos)} 个）和 {documents['total']} 件附件")
    print(f"原件 {project.raw}")
    print(f"编辑与取证 {project.work}")
    print(f"工作稿 {project.work / '教材.md'}")


def preview(project):
    from acceptance import TEMPLATE, prepare_review
    from media_inventory import verify_course_media
    from publish import compile_textbook, render_preview
    from scope_review import add_scope_summary, declaration_from_project

    sources = project.validate_raw()
    media = read(project.ledger / "media-summary.json")
    require_readable_videos(media)
    verify_course_media(project, sources, media["videos"])
    order = read(project.work / ORDER)
    deliver, receipt, figures = compile_textbook(
        project.folder, project.work, project.ledger, art_root=project.art, order=order)
    scope_declaration = declaration_from_project(project)
    body = add_scope_summary(deliver[ENTRY.as_posix()], scope_declaration)
    deliver[ENTRY.as_posix()] = body
    render_preview(project.preview, deliver, figures, project.art,
                   companion_draft=project.work, companion_fallback=project.folder)
    template = prepare_review(project, body, order, receipt, scope_declaration=scope_declaration)
    put(project.work / TEMPLATE, template)
    return body, figures, template


def reviewed_preview(project):
    from acceptance import REVIEW, validate_review, validate_review_proofs

    body, figures, template = preview(project)
    review_path = project.work / REVIEW
    if not review_path.is_file():
        raise ValueError(f"请先逐集对照原片并只读走查，填写 {review_path}")
    review = read(review_path)
    validate_review(body, figures, review, template)
    validate_review_proofs(project, review)
    # The production author's JSON is not the independent tester's receipt.
    review.pop("independent_fact_review", None)
    from independent_facts import load_independent_receipt

    receipt = load_independent_receipt(body, review)
    if receipt is not None:
        review["independent_fact_review"] = receipt
    return body, figures, template, review


def check_review_status(project):
    """Report only the concrete disposition found by the current review run."""
    from acceptance import REVIEW, TEMPLATE
    from media_inventory import MEDIA_INVENTORY
    from scope_review import DECLARATION

    def optional_digest(path):
        return sha(path) if path.is_file() else None

    def inputs():
        return {"intake_sha256": sha(project.intake), "draft_sha256": optional_digest(project.work / "教材.md"),
                "review_sha256": optional_digest(project.work / REVIEW),
                "scope_declaration_sha256": optional_digest(project.work / DECLARATION),
                "media_inventory_sha256": optional_digest(project.ledger / MEDIA_INVENTORY),
                "documents_summary_sha256": optional_digest(project.ledger / "documents-summary.json")}

    before = inputs()
    try:
        _, _, template, _ = reviewed_preview(project)
    except ReceivedCoverageError as error:
        disposition = {"received_analysis": error.state, "location": error.location, "detail": str(error)}
        code = 1
    except (OSError, ValueError, RuntimeError, LookupError, TypeError, subprocess.SubprocessError) as error:
        disposition = {"received_analysis": "unverified", "detail": str(error)}
        code = 1
    else:
        disposition = {"received_analysis": "covered", "body_sha256": template["snapshot"]["text_sha256"],
                       "source_scope_sha256": template["snapshot"]["source_scope_sha256"]}
        code = 0
    after = inputs()
    if before != after:
        disposition = {"received_analysis": "unverified", "detail": "核对期间的原件清单或工作稿发生变化，请重试"}
        code = 1
    result = {"schema": 1, "observed_at": datetime.now(timezone.utc).isoformat(), **after,
              "review_template_sha256": optional_digest(project.work / TEMPLATE)}
    return {**result, **disposition}, code


def prepare_fact_request(project):
    from acceptance import REVIEW, TEMPLATE, edition_snapshot, validate_scope_against_template
    from independent_facts import review_request
    from source_coverage import validate_source_coverage

    preview_path = project.preview / ENTRY
    review = read(project.work / REVIEW)
    template = read(project.work / TEMPLATE)
    if (review.get("snapshot") != template["snapshot"] or review.get("source_scope") != template["source_scope"]
            or sha(preview_path) != template["snapshot"]["text_sha256"]):
        raise ValueError("请从当前已生成的同版预览填写验收记录，再生成独立事实待查清单")
    body = preview_path.read_text(encoding="utf-8")
    companions = template["snapshot"].get("companions")
    if companions and edition_snapshot(project.preview, body).get("companions") != companions:
        raise ValueError("预览的配套资料已变化，请重新核对这一版")
    validate_source_coverage(body, template["source_scope"], review)
    validate_scope_against_template(body, review, template)
    request = review_request(body, review)
    path = project.work / "独立事实核验待查.json"
    put(path, request)
    return path, request


def accept(project, obsidian_exe):
    from acceptance import commit, edition_snapshot, validate_review_proofs, verify_publication
    from media_inventory import verify_course_media

    body, figures, template, review = reviewed_preview(project)
    run_obsidian(obsidian_exe, "--project", project.folder, project.preview)
    render = read(project.preview / "obsidian-render.json")
    current = (project.preview / ENTRY).read_text(encoding="utf-8")
    if current != body or edition_snapshot(project.preview, current) != {
        key: template["snapshot"][key] for key in ("text_sha256", "images", "companions") if key in template["snapshot"]
    } or any(sha(local_child(project.state, name)) != digest
             for name, digest in template["snapshot"]["evidence"].items()):
        raise ValueError("阅读检查期间正文、配图或配套资料已修改，请重新验收")
    validate_review_proofs(project, review)
    sources = project.validate_raw()
    verify_course_media(project, sources, review["episodes"])
    commit(project, body, figures, review, render)
    return verify_publication(project, raw_verified=True)


def run_obsidian(executable, *arguments):
    env = dict(os.environ)
    if executable:
        env["OBSIDIAN_EXE"] = str(executable.resolve(strict=True))
    node = shutil.which("node")
    if not node:
        raise ValueError("Obsidian 阅读验收需要可调用的 Node.js")
    subprocess.run([node, str(ROOT / "_tools/course_production/verify_obsidian.cjs"),
                    *map(str, arguments)], env=env, check=True)


def verify_reader(project, publication, executable):
    from project import named_entry
    from vault_delivery import RENDER_DIR, automated_observation, confirm, delivery, verify_installed

    state = delivery(project)
    if not state:
        raise ValueError("请先交付到日常 Obsidian 阅读库")
    entry = verify_installed(project, publication, state)
    target = entry.parent.parent
    if entry != named_entry(target):
        raise ValueError("请先用 deliver 将日常笔记名迁移到项目名称")
    render = project.work / RENDER_DIR / f"{state['sha256']}.json"
    render.parent.mkdir(parents=True, exist_ok=True)
    arguments = ["--installed", target, entry.relative_to(target).as_posix(), render]
    if publication["review"]["snapshot"].get("companions"):
        arguments.append(local_child(project.ledger, f"publications/{state['sha256']}.json"))
    run_obsidian(executable, *arguments)
    observation = automated_observation(project, publication, state, render)
    put(render.parent / "当前回执.json", observation)
    return confirm(project, publication, observation)


def show_status(project):
    from media_inventory import MEDIA_INVENTORY

    media_file = project.ledger / "media-summary.json"
    media = read(media_file) if media_file.is_file() else None
    routes_file = project.ledger / MEDIA_INVENTORY
    routes = read(routes_file) if routes_file.is_file() else None
    inventory = read(project.intake)
    print(json.dumps({
        "project": str(project.folder), "original_files": len(inventory["sources"]),
        "prepared_videos": sum(row.get("kind") != "audio" for row in media["videos"]) if media is not None else None,
        "prepared_audio": sum(row.get("kind") == "audio" for row in media["videos"]) if media is not None else None,
        "embedded_videos": [row for row in media["videos"] if "embedded" in row] if media is not None else [],
        "unreadable_videos": media.get("unreadable", []) if media is not None else [],
        "media_inventory": str(routes_file) if routes is not None else None,
        "media_pending": routes["unresolved"] if routes is not None else [],
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
    from batch import add_arguments, run as run_batch

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    add_arguments(commands)
    catalog = commands.add_parser("refresh-catalog", help="按已交付笔记更新 Obsidian 项目总览")
    catalog.add_argument("vault", type=Path)
    catalog.add_argument("--batch", type=Path, action="append", default=[],
                         help="从当前同版验收及已记录批次中回填证实的分析日期")
    catalog.add_argument("--production-evidence", type=Path, action="append", default=[],
                         help="明确绑定核心原件和当前版次的制作日期证据")
    start = commands.add_parser("start", help="从用户提供的文件夹入库，提取文字、转写和画面候选")
    start.add_argument("source", type=Path)
    start.add_argument("--output", type=Path, help="独立课程包完整路径")
    preparation_options(start)
    rerun = commands.add_parser("prepare", help="用入库快照续作或应用媒体设置")
    rerun.add_argument("project", type=Path)
    preparation_options(rerun)
    for name, help_text in (
        ("status", "查看课程包内的当前产物"),
        ("coverage", "从原件和取材记录列出独立核查范围"),
        ("preview", "编译一篇阅读稿和逐集验收填写模板"),
        ("fact-review-template", "生成同版待独立核验的事实清单，不代替测试 agent 签收"),
        ("check-review", "运行双向内容验收，暂不发布或打开阅读器"),
        ("accept", "核验课程包预览并更新已绑定的日常阅读库"),
        ("verify", "复验课程包文件与交付状态"),
        ("deliver", "在日常 Obsidian 阅读库更新课程入口"),
        ("confirm-vault", "登记日常 Obsidian 库重开后的实际阅读结果"),
        ("verify-reader", "通过 Obsidian 接口重开和核阅日常入口的同版副本"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("project", type=Path)
        if name in ("accept", "verify-reader"):
            command.add_argument("--obsidian-exe", type=Path)
        if name == "coverage":
            command.add_argument("--write", action="store_true", help="保存核查范围和未填写的模板到课程工作区")
        if name == "check-review":
            command.add_argument("--status-json", action="store_true", help="输出本次实际发现的已收内容核对结果")
            command.add_argument("--record", action="store_true", help="把本次核对结果保存在课程工作区，供接力读取")
        if name == "verify":
            command.add_argument("--require-vault", action="store_true", help="要求日常阅读入口及同版重开核验")
        if name == "deliver":
            command.add_argument("--vault", type=Path, help="已在 Obsidian 使用的本地库路径")
            command.add_argument("--course-folder", help="本课在该库中的一级文件夹名")
            command.add_argument("--reviewed-previous", help="已核实身份、可替换的旧教材 SHA-256")
        if name == "confirm-vault":
            command.add_argument("observation", type=Path, help="日常阅读库的实际窗口核验记录")
    for action in ("check-historical-attachments", "accept-historical-attachments"):
        historical = commands.add_parser(action, help="自动核已具名签收的附件入口变化，沿用历史分析范围")
        historical.add_argument("project", type=Path)
        historical.add_argument("workspace", type=Path)
        historical.add_argument("sequence", type=int)
        historical.add_argument("signed", type=Path)
        if action == "accept-historical-attachments":
            historical.add_argument("--obsidian-exe", type=Path)
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
    if args.action == "check-review" and args.record and not args.status_json:
        parser.error("--record 须与 check-review --status-json 一起使用")
    if args.action == "batch":
        return run_batch(args)

    try:
        if args.action == "refresh-catalog":
            from vault_catalog import refresh_catalog

            print(refresh_catalog(args.vault, manifests=args.batch, production_evidence=args.production_evidence))
            return 0
        project = (CourseProject.initialize(args.source, args.output) if args.action == "start"
                   else CourseProject.open(args.project))
        if args.action == "status":
            show_status(project)
            return 0
        formal_write = args.action in ("accept", "deliver", "confirm-vault", "verify-reader", "accept-historical-attachments")
        guard = nullcontext()
        if formal_write:
            from batch_workers import publication_lock, require_worker_publication

            guard = publication_lock(project.folder)
        with guard, project.locked():
            if formal_write:
                require_worker_publication(project.folder, args.action, vault=getattr(args, "vault", None),
                                           folder=getattr(args, "course_folder", None))
            if args.action in ("start", "prepare"):
                prepare(project, args)
            elif args.action == "coverage":
                from source_coverage import build_source_manifest, coverage_review_template

                project.validate_raw()
                scope, _ = build_source_manifest(project)
                record = {"source_scope": scope, "source_coverage": coverage_review_template(scope)}
                if args.write:
                    path = project.work / "原件核查范围.json"
                    put(path, record)
                    print(path)
                else:
                    print(json.dumps(record, ensure_ascii=False, indent=2))
            elif args.action == "check-review":
                if args.status_json:
                    result, code = check_review_status(project)
                    if args.record:
                        put(project.work / RECEIVED_REVIEW, result)
                    print(json.dumps(result, ensure_ascii=False))
                    return code
                reviewed_preview(project)
                print("双向内容核对通过，尚未发布")
            elif args.action == "check-historical-attachments":
                from historical_companions import FIELD, prepare as prepare_historical

                result, _, _ = prepare_historical(project, args.workspace, args.sequence, args.signed)
                print(json.dumps({"body_sha256": result["outputs"][ENTRY.as_posix()],
                                  "parent_body_sha256": result[FIELD]["parent_body_sha256"],
                                  "companions": result["review"]["snapshot"]["companions"]}, ensure_ascii=False))
            elif args.action == "accept-historical-attachments":
                from historical_companions import accept as accept_historical

                print(json.dumps(accept_historical(project, args.workspace, args.sequence, args.signed,
                                                   args.obsidian_exe, run_obsidian), ensure_ascii=False))
            elif args.action == "fact-review-template":
                path, request = prepare_fact_request(project)
                print(f"{path} · {len(request['facts'])} 项待核事实")
                print(f"测试 agent 回执位置：{request['receipt_path']}")
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
                from scope_review import publication_scope
                from vault_delivery import confirm, delivery, delivery_status, install

                record = accept(project, args.obsidian_exe) if args.action == "accept" else verify_publication(project)
                edition_scope = publication_scope(record)
                if args.action == "deliver":
                    if record["review"].get("schema") != 2:
                        raise ValueError("旧版回执可复验现有交付，重新发布需要先完成新版双向内容验收")
                    print(json.dumps({**install(project, record, args.vault, args.course_folder, args.reviewed_previous),
                                      **edition_scope},
                                     ensure_ascii=False, indent=2))
                    return 0
                if args.action == "confirm-vault":
                    print(json.dumps({**confirm(project, record, read(args.observation)), **edition_scope}, ensure_ascii=False, indent=2))
                    return 0
                if args.action == "verify-reader":
                    print(json.dumps({**verify_reader(project, record, args.obsidian_exe), **edition_scope}, ensure_ascii=False, indent=2))
                    return 0
                if args.action == "accept" and delivery(project):
                    install(project, record)
                live = delivery_status(project, record, required=getattr(args, "require_vault", False))
                episodes = record["review"]["episodes"]
                print(json.dumps({"verified": live["status"] == "已记录日常库重开",
                                  "package_verified": True, "entry": str(project.entry),
                                  "source_review_version": record["review"]["schema"],
                                  "original_files": len(read(project.intake)["sources"]),
                                  "videos": sum(row.get("kind") != "audio" for row in episodes),
                                  "audio": sum(row.get("kind") == "audio" for row in episodes),
                                  "embedded_videos": sum("embedded" in row and row.get("kind") != "audio" for row in episodes),
                                  "attachments": len(record["review"]["documents"]),
                                  "images": len(record["review"]["snapshot"]["images"]),
                                  "companion_files": len(record["review"]["snapshot"].get("companions", {})),
                                   "vault_delivery": live, **edition_scope}, ensure_ascii=False, indent=2))
        return 0
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", "replace")[-1500:] if isinstance(error.stderr, bytes) else ""
        print(f"课程阶段未完成：本地工具返回 {error.returncode}。{detail}", file=sys.stderr)
    except (OSError, ValueError, RuntimeError, LookupError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"课程阶段未完成：{error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
