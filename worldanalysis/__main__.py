"""CLI for the existing workspace. Network and generation are never implicit."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path


def display(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False))


def routed_extract(path, output_dir, *, options=None):
    from .documents import extract
    if Path(path).suffix.lower() in {'.mp4','.mkv','.mov','.avi','.webm','.mp3','.m4a','.wav','.flac','.ogg'}:
        from .media import extract_media
        return extract_media(path, output_dir, options=options)
    options = dict(options or {})
    if 'ocr' not in options:
        from .ocr import ocr_image
        options['ocr'] = ocr_image
    if Path(path).suffix.lower() == '.pdf' and 'render_pdf_page' not in options:
        from .pdf_render import render_pdf_page
        options['render_pdf_page'] = render_pdf_page
    return extract(path, output_dir, options=options)


def route_config_revision(*, ocr_enabled=True):
    """Stable route/weights/code identity, never elapsed-time or token usage."""
    import hashlib
    from importlib import metadata
    from .policy import Policy
    package = Path(__file__).resolve().parent
    code = {name:hashlib.sha256((package/name).read_bytes()).hexdigest() for name in ('__main__.py','documents.py','ocr.py','pdf_render.py','policy.py','contracts.py')}
    versions = {}
    for name in ('pypdf','pypdfium2','Pillow','rapidocr-onnxruntime','onnxruntime'):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = 'not-installed'
    weights = {}
    if ocr_enabled:
        try:
            dist = metadata.distribution('rapidocr-onnxruntime')
            for name in ('ch_PP-OCRv4_det_infer.onnx','ch_ppocr_mobile_v2.0_cls_infer.onnx','ch_PP-OCRv4_rec_infer.onnx'):
                path = Path(dist.locate_file('rapidocr_onnxruntime/models/'+name))
                sha = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda:stream.read(1024*1024),b''):
                        sha.update(chunk)
                weights[name] = sha.hexdigest()
        except (metadata.PackageNotFoundError, OSError):
            weights = {'state':'not-available'}
    payload = {'policy':Policy().policy_version,'ocr_enabled':bool(ocr_enabled),'versions':versions,'code':code,'ocr_weight_sha256':weights}
    return 'route-'+hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def media_config_revision(options):
    import hashlib
    from importlib import metadata
    package = Path(__file__).resolve().parent
    code = {name:hashlib.sha256((package/name).read_bytes()).hexdigest() for name in ('media.py','media_store.py','ocr.py','policy.py','contracts.py')}
    versions = {}
    for name in ('av','faster-whisper','ctranslate2','rapidocr-onnxruntime','onnxruntime','Pillow'):
        try: versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError: versions[name] = 'not-installed'
    admission = package.parent/'_ledger'/'devpack'/'media-provider-models.json'
    model = json.loads(admission.read_bytes()) if admission.is_file() else {}
    profile = {'code':code,'versions':versions,'options':options,'model_revision':model.get('revision'),'model_files':model.get('files',[])}
    return 'media-route-'+hashlib.sha256(json.dumps(profile,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def parser():
    root = argparse.ArgumentParser(prog='python -m worldanalysis', description='本地混合证据、不可变发布与独立Agent分析；默认零商业调用。')
    root.add_argument('--root', type=Path, default=Path.cwd())
    sub = root.add_subparsers(dest='command', required=True)
    caps = sub.add_parser('capabilities', help='只读设备/材料盘点，不打开数据库或网络')
    caps.add_argument('--output', type=Path, help='可选：新建JSON记录，禁止覆盖')
    sub.add_parser('status')
    create = sub.add_parser('create-project')
    create.add_argument('--project-id')
    create.add_argument('--author')
    create.add_argument('--topic')
    create.add_argument('--edition')
    create.add_argument('--identity-evidence', action='append', default=[])
    ingest = sub.add_parser('ingest')
    ingest.add_argument('--project', required=True)
    ocr_mode = ingest.add_mutually_exclusive_group()
    ocr_mode.add_argument('--ocr', action='store_true', help='兼容选项：默认已自动选用注册本地OCR；不下载权重')
    ocr_mode.add_argument('--no-ocr', action='store_true', help='明确停用本地OCR；相应覆盖缺口仍保留')
    ingest.add_argument('--resume-media', action='store_true', help='仅续已登记的本地媒体检查点暂停；未知IO不重放')
    ingest.add_argument('--no-asr', action='store_true', help='媒体仅解码/画面；语音仍留未处理缺口，不冒称ASR完成')
    ingest.add_argument('--synthetic-input', action='store_true', help='显式标记自建校准素材；不能作真实课程验收')
    ingest.add_argument('inputs', nargs='+', type=Path)
    compose = sub.add_parser('compose', help='仅组合本项目已发布证据；不重新提取、不改变旧生产回执')
    compose.add_argument('--project', required=True)
    compose.add_argument('--bundle', action='append', required=True)
    verify = sub.add_parser('verify')
    verify.add_argument('--project', required=True)
    verify.add_argument('--bundle')
    control = sub.add_parser('control')
    control.add_argument('pipeline', choices=['collection','analysis'])
    control.add_argument('action', choices=['start','pause','resume'])
    source = sub.add_parser('observe-site', help='仅访问单个授权公开文章，不全站扫描，不获取浏览器凭据')
    source.add_argument('url')
    analysis = sub.add_parser('analysis')
    actions = analysis.add_subparsers(dest='analysis_action', required=True)
    claim = actions.add_parser('next')
    claim.add_argument('--project')
    claim.add_argument('--model-version', required=True)
    claim.add_argument('--prompt-version', default='general-analyzer-20261110')
    read = actions.add_parser('read')
    read.add_argument('--task', required=True)
    read.add_argument('--block', action='append', required=True)
    read.add_argument('--max-chars', type=int, default=16000)
    complete = actions.add_parser('complete')
    complete.add_argument('--task', required=True)
    complete.add_argument('result', type=Path)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    root = args.root.resolve(strict=True)
    if args.command == 'capabilities':
        from .capabilities import detect
        result = detect(root)
        if args.output:
            from .policy import contained_path
            output = args.output if args.output.is_absolute() else root / args.output
            relative = output.absolute().relative_to(root).as_posix()
            output = contained_path(root, relative, must_exist=False)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open('x', encoding='utf-8') as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
            display({'output': output, 'state': 'observed', 'not_a_parser_or_cloud_acceptance': True})
        else:
            display(result)
        return 0
    from .store import Store
    with Store(root) as store:
        if args.command == 'status':
            display({'projects': store.list_projects(), 'bundles': store.list_bundles(), 'controls': {p:store.pipeline_state(p) for p in ('collection','analysis')}, 'supplement_requests': store.list_supplements(), 'legacy_materials': 'preserved; new project reports do not replace historical 075 verification', 'commercial_calls': 'disabled_by_default', 'automatic_agent_subscription_token_reuse': False})
        elif args.command == 'create-project':
            project = store.create_project(project_id=args.project_id, author=args.author, topic=args.topic, edition=args.edition, identity_evidence=args.identity_evidence)
            display({'project_id':project})
        elif args.command == 'ingest':
            from .contracts import reject_links
            inputs = []
            for path in args.inputs:
                path = path if path.is_absolute() else root / path
                reject_links(path)
                path = path.resolve(strict=True)
                if not path.is_relative_to(root):
                    raise ValueError('This workspace input must remain under --root; manual input is never moved or deleted')
                inputs.append(path)
            import mimetypes
            media = [(mimetypes.guess_type(str(p))[0] or '').startswith(('audio/','video/')) for p in inputs]
            collected = []
            native_inputs = [p for p,is_media in zip(inputs,media) if not is_media]
            if native_inputs:
                options = {'ocr': None} if args.no_ocr else None
                native = store.ingest(args.project,native_inputs,routed_extract,options=options,config_version=route_config_revision(ocr_enabled=not args.no_ocr))
                if 'bundle_id' not in native:
                    display(native)
                    return 2
                collected.append(native)
            options = {'ocr_enabled':not args.no_ocr,'asr_enabled':not args.no_asr,'synthetic':bool(args.synthetic_input)}
            config = media_config_revision(options) if any(media) else None
            for path,is_media in zip(inputs,media):
                if not is_media:
                    continue
                derived = store.ingest_media(args.project,[path],options=options,config_version=config,resume=args.resume_media)
                if 'bundle_id' not in derived:
                    display({**derived,'ready_bundle_ids':[b['bundle_id'] for b in collected]})
                    return 2
                collected.append(derived)
            ids = list(dict.fromkeys(b['bundle_id'] for b in collected))
            bundle = store.compose(args.project,ids) if len(ids) > 1 else collected[0]
            if 'bundle_id' not in bundle:
                display(bundle)
                return 2
            display({'project_id':bundle['project_id'],'bundle_id':bundle['bundle_id'],'revision':bundle['bundle_revision'],'publish_id':bundle['publish_id'],'coverage':bundle['coverage'],'gaps':bundle['gaps'],'project_root':store.project_root(args.project),'analysis': 'Ready for independent agent reading; no summary has been fabricated'})
        elif args.command == 'compose':
            bundle = store.compose(args.project,args.bundle)
            display({'status':bundle.get('status','published'),'project_id':args.project,'bundle_id':bundle.get('bundle_id'),'coverage':bundle.get('coverage'),'new_extraction_executed':False})
            return 0 if 'bundle_id' in bundle else 2
        elif args.command == 'verify':
            bundle = store.get_bundle(args.project,args.bundle)
            display({'project_id':args.project,'bundle_id':bundle['bundle_id'],'revision':bundle['bundle_revision'],'publication':bundle['publication']['state'],'local_file_and_contract_checks':'passed','coverage':bundle['coverage'],'gap_count':len(bundle['gaps'])})
        elif args.command == 'control':
            display(store.control(args.pipeline,args.action))
        elif args.command == 'observe-site':
            from .acquisition import Acquisition
            observed = Acquisition(store.connection)
            result = observed.discover_public_article(args.url)
            store.event('public_article_observed',result['source_id'],payload=result)
            display(result)
        elif args.command == 'analysis':
            from .analysis import Analysis
            worker = Analysis(store)
            if args.analysis_action == 'next':
                display(worker.claim(project_id=args.project,model_version=args.model_version,prompt_version=args.prompt_version,mode='agent_assisted'))
            elif args.analysis_action == 'read':
                display(worker.read(args.task,args.block,max_chars=args.max_chars))
            else:
                display(worker.complete(args.task,args.result))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        # Do not print raw response URLs, private paths from connectors, or headers.
        from .policy import GateError
        from .contracts import ContractError
        if isinstance(exc,(GateError,ContractError,ValueError,FileNotFoundError)):
            display({'ok':False,'error_type':type(exc).__name__,'reason':str(exc),'no_completion_claim':True})
            sys.exit(2)
        raise
