"""Read-only P0 inventory: observed facts, never capability claims from imports alone."""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
import ctypes
import json
import os
import shutil
import subprocess
import sys


def now():
    return datetime.now(timezone.utc).isoformat()


def hardware(root: Path) -> dict:
    result = {'observed_at': now(), 'platform': sys.platform, 'python': sys.version.split()[0], 'cpu_count': os.cpu_count()}
    disk = shutil.disk_usage(root)
    result['disk'] = {'total_bytes': disk.total, 'free_bytes': disk.free, 'used_bytes': disk.used}
    if os.name == 'nt':
        class Memory(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong), ('total', ctypes.c_ulonglong), ('available', ctypes.c_ulonglong), ('page_total', ctypes.c_ulonglong), ('page_available', ctypes.c_ulonglong), ('virtual_total', ctypes.c_ulonglong), ('virtual_available', ctypes.c_ulonglong), ('extended_available', ctypes.c_ulonglong)]
        memory = Memory()
        memory.length = ctypes.sizeof(memory)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            result['ram'] = {'physical_total_bytes': memory.total, 'physical_available_bytes': memory.available, 'load_percent': memory.load}
    exe = shutil.which('nvidia-smi')
    if exe:
        command = [exe, '--query-gpu=name,memory.total,memory.free,driver_version', '--format=csv,noheader,nounits']
        try:
            run = subprocess.run(command, capture_output=True, text=True, timeout=15, check=True)
            result['gpu'] = [{'name': p[0].strip(), 'vram_total_mib': int(p[1]), 'vram_free_mib': int(p[2]), 'driver': p[3].strip()} for line in run.stdout.splitlines() if len(p := line.split(',')) == 4]
        except (subprocess.SubprocessError, ValueError):
            result['gpu'] = {'state': 'unknown', 'reason': 'GPU resource query failed'}
    else:
        result['gpu'] = {'state': 'unknown', 'reason': 'nvidia-smi unavailable'}
    result['executables'] = {p: shutil.which(p) for p in ('ffmpeg', 'ffprobe', 'tesseract', 'git')}
    available_ram = result.get('ram', {}).get('physical_available_bytes')
    result['initial_limits'] = {'ram_bytes': min(4 * 1024**3, int(available_ram * .35)) if available_ram else None, 'temporary_cache_bytes': min(8 * 1024**3, int(disk.free * .2)), 'gpu_workers': 1, 'ocr_workers': 1}
    return result


def inventory(root: Path) -> dict:
    """File metadata only: no full-media hashing and no network side effects."""
    base = Path(root) / 'wangzjk.com'
    counts, samples, errors = Counter(), {}, []
    if not base.is_dir():
        return {'scope': 'local wangzjk.com inventory only, not cloud inventory', 'counts': {}, 'smallest_samples': {}, 'errors': [], 'enumeration_state': 'unknown', 'reason': 'Local legacy source directory not present; no zero-completeness claim'}
    for current, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink() and not (hasattr(Path(current) / d, 'is_junction') and (Path(current) / d).is_junction())]
        for name in files:
            p = Path(current) / name
            if p.is_symlink():
                continue
            try:
                size = p.stat().st_size
            except OSError:
                errors.append(str(p.relative_to(root)))
                continue
            suffix = p.suffix.lower() or '(none)'
            counts[suffix] += 1
            sample = {'path': p.relative_to(root).as_posix(), 'bytes': size}
            if suffix not in samples or size < samples[suffix]['bytes']:
                samples[suffix] = sample
    return {'scope': 'local wangzjk.com inventory only, not cloud inventory', 'counts': dict(sorted(counts.items())), 'smallest_samples': samples, 'errors': errors, 'enumeration_state': 'partial' if errors else 'complete'}


def detect(root: Path) -> dict:
    root = Path(root).resolve(strict=True)
    packages = {}
    for package in ('python-docx', 'python-pptx', 'openpyxl', 'Pillow', 'pypdf', 'pypdfium2', 'jsonschema', 'rapidocr-onnxruntime', 'onnxruntime', 'faster-whisper', 'av'):
        try:
            dist = metadata.distribution(package)
            packages[package] = {'version': dist.version, 'state': 'implemented_dependency_not_yet_sample_verified', 'license': dist.metadata.get('License-Expression') or dist.metadata.get('License') or 'see installed license files'}
        except metadata.PackageNotFoundError:
            packages[package] = {'state': 'not_installed'}
    dbs = [p.relative_to(root).as_posix() for p in root.rglob('*.sqlite*') if '.runtime' not in p.parts]
    return {'schema': 'worldanalysis.capabilities.v1', 'observed_at': now(), 'workspace': str(root), 'hardware': hardware(root), 'inventory': inventory(root), 'packages': packages, 'business_databases_found': [p for p in dbs if p == '整理结果/worldanalysis.sqlite3'], 'runtime_checkpoint_files': [p for p in dbs if '.media-checkpoint' in p], 'other_sqlite_files_unclassified': [p for p in dbs if p != '整理结果/worldanalysis.sqlite3' and '.media-checkpoint' not in p and not p.endswith(('-wal','-shm'))], 'legacy': {'material_ledger': '_ledger/075-materials.json', 'mode': 'preserved_read_only_history', 'source_index': 'No persistent article index located in the inspected workspace; existing project documents retained.'}, 'authorization': {'agent_analysis': 'current_user_authorized_agent_assisted', 'commercial_api': 'blocked_zero_budget_no_external_processing', 'baidu_generation': 'disabled', 'cloud_root': '/apps/教育分析学习/', 'cloud_root_live_verification': 'pending', 'credential_export': 'disabled'}, 'claims': {'hardware': 'verified', 'local_inventory': 'verified_metadata_only', 'parser_import': 'implemented_not_complete_extraction', 'schema_files': 'documented_until_contract_tests_pass', 'legacy_course_materials': 'separate existing validator required'}}
