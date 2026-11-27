"""Local CLI for platform-first inventory, product receipts and gap planning.

No browser, provider generation, media reads or fallback worker is executed.
Kept separate from the original media/document CLI so adding this orchestration
does not invalidate those extractors' existing configuration fingerprints.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contracts import ContractError
from .platform_first import PlatformFirst
from .policy import GateError, contained_path
from .store import Store


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--root', type=Path, default=Path.cwd())
    result.add_argument('--synthetic-input', action='store_true',
                        help='Only permit explicitly synthetic inventory for offline verification')
    actions = result.add_subparsers(dest='action', required=True)
    begin = actions.add_parser('begin', help='Register a frozen, actually observed inventory')
    begin.add_argument('--project', required=True)
    begin.add_argument('--inventory', type=Path, required=True)
    for action in ('next', 'status', 'seal', 'plan'):
        command = actions.add_parser(action)
        command.add_argument('--run', required=True)
    record = actions.add_parser('record', help='Validate and record one observed product')
    record.add_argument('--run', required=True)
    record.add_argument('--file-id', required=True)
    record.add_argument('--product', choices=('manuscript', 'courseware'), required=True)
    record.add_argument('--observation', type=Path, required=True)
    block = actions.add_parser('block', help='Stop a run without changing channels or erasing materials')
    block.add_argument('--run', required=True)
    block.add_argument('--reason', required=True)
    return result


def _read_json(root, path):
    path = path if path.is_absolute() else root / path
    try:
        relative = path.absolute().relative_to(root).as_posix()
    except ValueError:
        raise GateError('OUTSIDE_WORKSPACE') from None
    path = contained_path(root, relative)
    limit = 2 * 1024 * 1024
    if not path.is_file() or path.stat().st_size > limit:
        raise GateError('PLATFORM_RECEIPT_TOO_LARGE')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise GateError('PLATFORM_RECEIPT_TOO_LARGE')
    return json.loads(data.decode('utf-8-sig'))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        with Store(root) as store:
            coordinator = PlatformFirst(store, allow_synthetic=args.synthetic_input)
            if args.action == 'begin':
                result = coordinator.begin_run(args.project, _read_json(root, args.inventory))
            elif args.action == 'record':
                result = coordinator.record_product_observation(
                    args.run, args.file_id, args.product, _read_json(root, args.observation))
            elif args.action == 'block':
                result = coordinator.block_run(args.run, args.reason)
            else:
                method = {'next': coordinator.next_first_pass, 'status': coordinator.status,
                          'seal': coordinator.seal_first_pass, 'plan': coordinator.plan_missing}[args.action]
                result = method(args.run)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ContractError, GateError, OSError, ValueError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc),
                          'remote_execution_performed': False}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
