"""Inspect then unpack the user-supplied DevPack without overwriting anything."""
from __future__ import annotations
import hashlib
import json
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

SOURCE = Path('D:/下载/Worldanalysis_Agent_DevPack_20261110.zip')
WORKSPACE = Path('G:/Worldanalysis')
PREFIX = 'worldanalysis_agent_devpack_20261110'
EXPECTED_SHA256 = '53be5ff31a5cfad54303a776eb92dccccd0834ca0d2bd59cb132c9069e1b4dc4'


def unpack() -> dict:
    archive = SOURCE.read_bytes()
    digest = hashlib.sha256(archive).hexdigest()
    if digest != EXPECTED_SHA256:
        raise ValueError('Source archive changed after inspection')
    root = WORKSPACE.resolve(strict=True)
    target_root = root / PREFIX
    if target_root.exists() or target_root.is_symlink():
        raise FileExistsError('Destination already exists; no files overwritten')
    reserved = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)
    with zipfile.ZipFile(SOURCE) as zf:
        entries = zf.infolist()
        if not entries or len(entries) > 200 or sum(i.file_size for i in entries) > 20 * 1024 * 1024:
            raise ValueError('Archive size or entry limit exceeded')
        seen = set()
        for info in entries:
            name = info.filename
            parts = PurePosixPath(name).parts
            if not parts or parts[0] != PREFIX or '\\' in name or ':' in name or name.startswith('/'):
                raise ValueError('Invalid archive path')
            if any(p in ('.', '..') or p.rstrip(' .') != p or reserved.match(p) or any(ord(c) < 32 for c in p) for p in parts):
                raise ValueError('Unsafe archive path')
            if name.rstrip('/') != '/'.join(parts):
                raise ValueError('Noncanonical archive path')
            identity = '/'.join(parts).casefold()
            if identity in seen:
                raise ValueError('Duplicate archive path')
            seen.add(identity)
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError('Links and special archive entries are not allowed')
            if info.flag_bits & 1 or info.file_size > 10 * 1024 * 1024:
                raise ValueError('Encrypted or oversized entry')
            if info.file_size > max(1024 * 1024, info.compress_size * 200):
                raise ValueError('Suspicious compression ratio')
        # Full validation and CRC verification before creating any destination.
        staged = [(i, None if i.is_dir() else zf.read(i)) for i in entries]
    target_root.mkdir(exist_ok=False)
    results = []
    for info, data in staged:
        relative = PurePosixPath(info.filename)
        target = root.joinpath(*relative.parts)
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(data)
        actual = target.read_bytes()
        if actual != data:
            raise ValueError('Extracted bytes do not match archive')
        results.append({'path': relative.as_posix(), 'bytes': len(actual), 'sha256': hashlib.sha256(actual).hexdigest()})
    return {'schema': 'worldanalysis.devpack-extraction.v1', 'sourceSha256': digest, 'destination': str(target_root), 'files': results, 'fileCount': len(results), 'totalBytes': sum(r['bytes'] for r in results), 'originalArchivePreserved': True, 'overwrites': 0}


if __name__ == '__main__':
    try:
        print(json.dumps(unpack(), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({'ok': False, 'errorType': type(exc).__name__, 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
