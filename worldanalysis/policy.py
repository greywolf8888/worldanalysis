"""Program-enforced scope/resource boundaries, independent of model suggestions."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
import re
import unicodedata


class GateError(RuntimeError):
    def __init__(self, code: str, message: str = ''):
        self.code = code
        super().__init__(f'{code}: {message}' if message else code)


def canonical_cloud_path(value: str) -> str:
    if not isinstance(value, str) or not value.startswith('/') or value == '/':
        raise GateError('INVALID_SCOPE_PATH')
    if value != unicodedata.normalize('NFC', value) or any(c in value for c in ('\\', '%', '?', '#', ':')) or any(ord(c) < 32 for c in value):
        raise GateError('INVALID_SCOPE_PATH')
    if value.endswith('/'):
        value = value[:-1]
    if any(part in ('', '.', '..') for part in value[1:].split('/')):
        raise GateError('INVALID_SCOPE_PATH')
    return value


def local_relative(value: str) -> str:
    if not isinstance(value, str) or not value or '\\' in value or ':' in value or value.startswith('/'):
        raise GateError('INVALID_LOCAL_PATH')
    if value != unicodedata.normalize('NFC', value):
        raise GateError('INVALID_LOCAL_PATH')
    reserved = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)
    parts = value.split('/')
    if any(p in ('', '.', '..') or p.rstrip(' .') != p or reserved.match(p) or any(ord(c) < 32 for c in p) for p in parts):
        raise GateError('INVALID_LOCAL_PATH')
    return value


def contained_path(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    root = Path(root).resolve(strict=True)
    relative = local_relative(relative)
    current = root
    for part in relative.split('/'):
        current = current / part
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise GateError('LINK_NOT_ALLOWED')
    resolved = current.resolve(strict=must_exist)
    if not resolved.is_relative_to(root):
        raise GateError('OUTSIDE_WORKSPACE')
    return resolved


@dataclass(frozen=True)
class Policy:
    cloud_roots: tuple[str, ...] = ('/apps/教育分析学习',)
    allowed_site_hosts: tuple[str, ...] = ('wangzjk.com', 'www.wangzjk.com')
    request_new_generation: bool = False
    external_processing_enabled: bool = False
    project_budget_minor: int = 0
    daily_budget_minor: int = 0
    max_probe_bytes: int = 1024 * 1024
    max_cache_bytes: int = 8 * 1024**3
    max_ram_bytes: int = 4 * 1024**3
    max_attempts: int = 3
    policy_version: str = 'worldanalysis-20261110'

    def require_cloud_path(self, path: str) -> str:
        path = canonical_cloud_path(path)
        if not any(path == canonical_cloud_path(r) or path.startswith(canonical_cloud_path(r) + '/') for r in self.cloud_roots):
            raise GateError('OUTSIDE_AUTHORIZED_ROOT')
        return path

    def require_site(self, url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in self.allowed_site_hosts or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise GateError('SITE_NOT_AUTHORIZED')
        if any(k in parsed.query.lower() for k in ('token=', 'cookie=', 'password=', 'sign=', 'access_key=')):
            raise GateError('SENSITIVE_URL')
        return url

    def require_generation(self) -> None:
        if not self.request_new_generation:
            raise GateError('GENERATION_DISABLED')

    def require_external(self, estimated_cost_minor: int, upload_allowed: bool = False) -> None:
        if not self.external_processing_enabled or not upload_allowed:
            raise GateError('EXTERNAL_PROCESSING_DISABLED')
        if type(estimated_cost_minor) is not int or estimated_cost_minor < 0 or estimated_cost_minor > self.project_budget_minor:
            raise GateError('BUDGET_EXHAUSTED')


@dataclass
class ByteBudget:
    limit: int
    received: int = 0

    def __post_init__(self):
        if type(self.limit) is not int or self.limit < 0:
            raise ValueError('Invalid byte budget')

    def charge(self, amount: int):
        if type(amount) is not int or amount < 0 or self.received + amount > self.limit:
            raise GateError('BYTE_BUDGET_EXHAUSTED')
        self.received += amount
        return self.received
