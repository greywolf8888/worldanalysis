"""One authorized local invocation of existing APIs; no feature implementation."""
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import sys
import time
import traceback
import wave

ROOT = Path('G:/Worldanalysis')
sys.path.insert(0, str(ROOT))
from worldanalysis.store import Store
from worldanalysis.contracts import hash_file

PROJECT = 'site-163378-ai-video'
CONFIG = 'site-163378-first-real-20s-v1'
RECEIPT = ROOT / '_ledger/devpack/p2-browser-local-20261114.json'
OUT = ROOT / '_ledger/devpack/p2-first-real-20s-20261114.json'
WAV_REL = '窗口派生/site-163378-first-real-000000-005000.wav'
OPTIONS = dict(
    window_seconds=20, overlap_seconds=2, max_windows=1,
    asr_enabled=True, ocr_enabled=True, synthetic=False,
    device='cpu', cpu_threads=2, language='zh',
    model_dir=str(ROOT / '.runtime/site-packages/_worldanalysis_models/faster-whisper-tiny'),
    read_chunk_bytes=65536, probe_max_received_bytes=1048576,
    input_byte_cap=64*1024**2, pending_audio_byte_cap=4*1024**2,
    pending_image_byte_cap=16*1024**2, cache_byte_cap=32*1024**2,
    ram_byte_cap=2*1024**3, final_evidence_byte_cap=128*1024**2,
    frame_max_pixels=2073600, visual_probe_seconds=1, visual_period_seconds=5,
    max_records=256, max_result_bytes=8*1024**2, wall_timeout_seconds=300,
)
report = {
    'schema': 'worldanalysis.first-real-media-calibration.v1',
    'project_id': PROJECT, 'config_version': CONFIG, 'options': OPTIONS,
    'source_download_receipt': str(RECEIPT.relative_to(ROOT)),
    'source_download_receipt_sha256': 'cec46852b684b67bd85ff92f0fb20306924224c0a1cfa2c451b0571ec909b3ed',
    'authorization': 'lead explicitly granted exclusive Store writer for first20s and 0-5000ms WAV only',
    'no_network': True, 'no_install_or_download': True,
    'no_browser_or_credentials': True, 'no_full_source_hash_in_this_run': True,
    'no_whole_source_copy': True, 'no_resume_in_this_run': True,
    'no_semantic_analysis_or_listening_claim': True,
    'started_epoch_seconds': time.time(), 'steps': [],
}
store = None
failed = False

def phase(name):
    report['steps'].append({'phase': name, 'epoch_seconds': time.time()})
    print(name, flush=True)

def execution_rows(connection, source):
    return [dict(r) for r in connection.execute(
        'SELECT me.*,j.state AS job_state FROM media_executions me '
        'JOIN jobs j ON j.job_id=me.job_id JOIN media_sources ms '
        'ON ms.asset_id=me.asset_id AND ms.asset_revision=me.asset_revision '
        'JOIN sources s ON s.source_id=ms.source_id '
        'WHERE me.project_id=? AND s.local_path=?', (PROJECT, str(source)))]

try:
    if OUT.exists():
        raise RuntimeError('STOP: calibration receipt already exists; inspect it, never rerun blindly')
    expected_python = Path('D:/桌面/awesome-dsh/.dsh-home/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe')
    if Path(sys.executable).resolve() != expected_python.resolve():
        raise RuntimeError('STOP: unexpected interpreter')
    if not sys.dont_write_bytecode:
        raise RuntimeError('STOP: bytecode writes must be disabled')
    for name in ('TMP', 'TEMP', 'HF_HOME', 'XDG_CACHE_HOME'):
        location = Path(os.environ[name]).absolute()
        if not location.is_relative_to(ROOT):
            raise RuntimeError('STOP: temporary/cache path outside G workspace')
        location.mkdir(parents=True, exist_ok=True)
    phase('source_receipt_verified_before_store')
    receipt_bytes = RECEIPT.read_bytes()
    if hashlib.sha256(receipt_bytes).hexdigest() != report['source_download_receipt_sha256']:
        raise RuntimeError('STOP: download receipt changed')
    receipt = json.loads(receipt_bytes)
    if receipt['project_id'] != PROJECT or receipt['source_file_id'] != '701327767201446' or receipt['destination_fs_id'] != '196684496726590':
        raise RuntimeError('STOP: source binding mismatch')
    source = (ROOT / receipt['local']['final_file']).resolve(strict=True)
    report['source_relative'] = source.relative_to(ROOT).as_posix()
    report['source_download_sha256_external_receipt_only'] = receipt['local']['sha256']
    report['source_bytes'] = source.stat().st_size
    if not source.is_relative_to(ROOT.resolve()) or source.stat().st_size != 4110632:
        raise RuntimeError('STOP: source path/size mismatch')
    database = ROOT / '整理结果/worldanalysis.sqlite3'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as pre:
        pre.row_factory = sqlite3.Row
        pre.execute('PRAGMA query_only=ON')
        if not pre.execute('SELECT 1 FROM projects WHERE project_id=?', (PROJECT,)).fetchone():
            raise RuntimeError('STOP: existing project missing; no project will be created')
        prior = execution_rows(pre, source)
        report['prior_source_executions'] = [{k:v for k,v in r.items() if k not in ('raw_json','limits_json')} for r in prior]
        if prior:
            raise RuntimeError('STOP: existing exact-source media execution requires inspection; no replay')
    phase('exclusive_store_open')
    store = Store(ROOT)
    if store.pipeline_state('collection') != 'running':
        raise RuntimeError('STOP: collection is not running; no control change authorized')
    report['project_root'] = str(store.project_root(PROJECT).relative_to(ROOT))
    store.event('authorized_browser_local_media_calibration_started', CONFIG, PROJECT, {
        'download_receipt': report['source_download_receipt'],
        'download_receipt_sha256': report['source_download_receipt_sha256'],
        'source_file_id': receipt['source_file_id'], 'destination_fs_id': receipt['destination_fs_id'],
        'existing_transfer_id': receipt['existing_transfer_id'],
        'download_sha256_not_registration_hash': receipt['local']['sha256'],
        'window_ms': [0,20000], 'wav_window_ms': [0,5000],
    })
    phase('existing_ingest_media_first20s_once')
    bundle = store.ingest_media(PROJECT, [source], options=OPTIONS, config_version=CONFIG, resume=False)
    if 'bundle_id' not in bundle:
        report['ingest_result'] = bundle
        raise RuntimeError('STOP: ingest returned no published Bundle')
    if len(bundle['assets']) != 1:
        raise RuntimeError('STOP: expected unique source asset')
    asset = bundle['assets'][0]
    report['asset'] = asset
    report['native_bundle'] = {k:bundle[k] for k in ('bundle_id','publish_id','bundle_revision','coverage')}
    report['native_gaps'] = bundle['gaps']
    rows = execution_rows(store.connection, source)
    if len(rows) != 1:
        raise RuntimeError('STOP: expected one media execution')
    row = rows[0]
    raw = json.loads(row['raw_json'])
    md = raw['metadata']
    report['media_execution'] = {k:v for k,v in row.items() if k not in ('raw_json','limits_json')}
    report['worker_dir'] = str((store.project_root(PROJECT) / row['worker_relative_path']).relative_to(ROOT))
    report['native_artifacts'] = bundle['artifacts']
    report['native_blocks_index'] = [{k:b[k] for k in ('block_id','kind','origin','locator','artifact_ids')} for b in bundle['blocks']]
    report['actual_media_metadata'] = md
    report['raw_enumeration_state'] = raw['enumeration_state']
    report['actual_counts'] = {
        'audio_windows': len(md.get('audio_windows',[])),
        'keyframes': len(md.get('visual_frames',[])),
        'asr_blocks': sum(b['origin']=='asr' for b in bundle['blocks']),
        'ocr_blocks': sum(b['origin']=='ocr' for b in bundle['blocks']),
        'transcript_artifacts': sum(a['kind']=='transcript' for a in bundle['artifacts']),
    }
    for artifact in bundle['artifacts']:
        actual = hash_file(store.project_root(PROJECT) / artifact['relative_path'])
        if actual != (artifact['sha256'],artifact['byte_length']):
            raise RuntimeError('STOP: published artifact hash/bytes mismatch')
    report['native_published_artifact_hashes_verified'] = True
    if md.get('synthetic') is not False or len(md.get('audio_windows',[])) != 1:
        raise RuntimeError('STOP: expected exactly one real audio window')
    window = md['audio_windows'][0]
    if (window['start_ms'], window['end_ms']) != (0,20000) or row['state'] != 'paused':
        raise RuntimeError('STOP: sample did not end at intended checkpoint pause')
    if not md.get('visual_frames'):
        raise RuntimeError('STOP: no actual decoded keyframe was published')
    phase('existing_publish_audio_window_0_5000_once')
    audio = store.publish_audio_window(PROJECT,bundle['bundle_id'],asset['asset_id'],asset['asset_revision'],0,5000,
        output_relative=WAV_REL,max_received_bytes=16*1024**2)
    if 'bundle_id' not in audio:
        report['audio_result'] = audio
        raise RuntimeError('STOP: audio API returned no published Bundle')
    report['audio_bundle'] = {k:audio[k] for k in ('bundle_id','publish_id','bundle_revision','coverage')}
    report['audio_gaps'] = audio['gaps']
    clips = [a for a in audio['artifacts'] if a['media_type']=='audio/wav' and a['provider']['provider_id']=='local_audio_window']
    if len(clips) != 1:
        raise RuntimeError('STOP: expected unique published WAV')
    artifact = clips[0]
    published_wav = store.project_root(PROJECT) / artifact['relative_path']
    loose_wav = ROOT / WAV_REL
    wave_checks = []
    for wav in (loose_wav,published_wav):
        with wave.open(str(wav),'rb') as reader:
            check = {'path':str(wav.relative_to(ROOT)), 'sample_rate':reader.getframerate(),
                'sample_width':reader.getsampwidth(), 'channels':reader.getnchannels(),
                'sample_count':reader.getnframes(), 'compression':reader.getcomptype(),
                'bytes':wav.stat().st_size}
            if (check['sample_rate'],check['sample_width'],check['channels'],check['sample_count'],check['bytes']) != (16000,2,1,80000,160044):
                raise RuntimeError('STOP: actual WAV format/count/bytes mismatch')
            check['pcm_bytes_read_for_verification'] = len(reader.readframes(80001))
            if check['pcm_bytes_read_for_verification'] != 160000:
                raise RuntimeError('STOP: actual WAV PCM payload mismatch')
        check['sha256'] = hash_file(wav)[0]
        if check['sha256'] != artifact['sha256']:
            raise RuntimeError('STOP: WAV published SHA mismatch')
        wave_checks.append(check)
    report['wav_checks'] = wave_checks
    report['wav_artifact'] = artifact
    report['wav_primitive_manifest'] = json.loads(loose_wav.with_name(loose_wav.name+'.manifest.json').read_bytes())
    report['native_bundle_readback_id'] = store.get_bundle(PROJECT,bundle['bundle_id'])['bundle_id']
    report['audio_bundle_readback_id'] = store.get_bundle(PROJECT,audio['bundle_id'])['bundle_id']
    report['status'] = 'first20s_resources_and_first5s_wav_published_verified'
    report['scope_limits'] = ['not whole115.88s extraction','not human corrected transcript','not listened audio','sparse frames not complete teaching visuals','no semantic analysis','source registration remains stat descriptor']
    store.event('authorized_browser_local_media_calibration_verified', CONFIG, PROJECT, {
        'receipt_path': str(OUT.relative_to(ROOT)), 'download_receipt':report['source_download_receipt'],
        'native_bundle_id':bundle['bundle_id'],'audio_bundle_id':audio['bundle_id'],
        'counts':report['actual_counts'],'scope':'first20s checkpoint and unheard first5s WAV only',
    })
    phase('verification_complete_no_resume')
except Exception as exc:
    failed = True
    report['status'] = 'stopped_no_automatic_retry'
    report['error'] = {'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()}
    if store is not None:
        try:
            report['project_jobs_after_stop'] = [dict(r) for r in store.connection.execute(
                'SELECT job_id,state,operation,reason FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT 6',(PROJECT,))]
            report['project_publications_after_stop'] = [dict(r) for r in store.connection.execute(
                'SELECT publish_id,bundle_id,state,revision,staging_relative_path,final_relative_path FROM publications WHERE project_id=? ORDER BY revision DESC LIMIT 6',(PROJECT,))]
        except Exception as diagnostic:
            report['diagnostic_error'] = str(diagnostic)
    print('STOP: '+str(exc),flush=True)
finally:
    if store is not None:
        store.close()
        report['store_closed'] = True
    report['finished_epoch_seconds'] = time.time()
    report['elapsed_seconds'] = report['finished_epoch_seconds'] - report['started_epoch_seconds']
    if not OUT.exists():
        with OUT.open('x',encoding='utf-8') as handle:
            json.dump(report,handle,ensure_ascii=False,indent=2,allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
    print(json.dumps({'status':report['status'],'receipt':str(OUT),'store_closed':report.get('store_closed',False),'native_bundle':report.get('native_bundle'),'audio_bundle':report.get('audio_bundle'),'counts':report.get('actual_counts'),'error':report.get('error',{}).get('message')},ensure_ascii=False),flush=True)
if failed:
    raise SystemExit(2)
