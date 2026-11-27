"""Read the original DSH session through the installed Worldanalysis MCP adapter."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[3]
BRIDGE = ROOT / '_tools' / 'v4-dsh-control.mjs'
PROTOCOL = '2025-11-25'
SERVER_NAME = 'worldanalysis-dsh-control'


def _request(node, bridge, method, params, timeout):
    frames = (
        {'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': PROTOCOL, 'capabilities': {},
            'clientInfo': {'name': 'worldanalysis-console', 'version': '1'},
        }},
        {'method': 'notifications/initialized'},
        {'id': 2, 'method': method, 'params': params},
    )
    payload = ''.join(json.dumps({'jsonrpc': '2.0', **frame}) + '\n' for frame in frames)
    # Keep Node startup options outside the adapter invocation. The adapter owns
    # discovery, workspace validation, and the existing local connection.
    env = {key: value for key, value in os.environ.items() if key.upper() != 'NODE_OPTIONS'}
    result = subprocess.run(
        [node, str(bridge)], input=payload.encode('utf-8'), stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, cwd=ROOT, timeout=timeout, check=True, env=env,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    if len(result.stdout) > 65_536:
        raise ValueError('Unrecognized bridge response')
    replies = {}
    for line in result.stdout.splitlines():
        reply = json.loads(line)
        ident = reply['id']
        if (reply.get('jsonrpc') != '2.0' or type(ident) is not int or ident not in (1, 2)
                or ident in replies or 'error' in reply or not isinstance(reply.get('result'), dict)):
            raise ValueError('Unrecognized bridge response')
        replies[ident] = reply['result']
    init = replies[1]
    if init['protocolVersion'] != PROTOCOL or init['serverInfo']['name'] != SERVER_NAME:
        raise ValueError('Unexpected bridge protocol')
    return replies[2]


def _main_session(tools):
    schemas = {tool['name']: tool['inputSchema'] for tool in tools}
    field = 'sessionId'
    # The bridge reserves its handoff for the original main session. Read that
    # binding from the tool catalog without invoking the handoff tool.
    session = schemas['dsh_send_message']['properties'][field]['const']
    if (not isinstance(session, str) or not session
            or session not in schemas['dsh_get_session']['properties'][field]['enum']):
        raise ValueError('Unknown main session')
    return session


def _readiness(row):
    if (row.get('live') is not True or row.get('interactionMode') != 'ui'
            or row.get('permissionPresetMatches') is not True
            or row.get('sandboxMatchesWhenReported') is False
            or row.get('approvalMatchesWhenReported') is False):
        return 'unavailable'

    def positive(key):
        count = row.get(key)
        return type(count) is int and count > 0

    if any(positive(key) for key in ('pendingInputCount', 'pendingInteractionCount', 'desktopPendingCount')):
        return 'awaiting_user'
    if (row.get('turnSettled') is False or row.get('running') is True or row.get('planMode') is True
            or any(positive(key) for key in ('queueCount', 'activeWorkflowCount'))):
        return 'busy'
    if (row.get('turnSettled') is True and row.get('running') is False and row.get('planMode') is False
            and type(row.get('cursor')) is int and row['cursor'] >= 0
            and type(row.get('pendingInteractionCount')) is int and row['pendingInteractionCount'] == 0):
        return 'ready'
    return 'unavailable'


def probe_dsh():
    """Return current handoff readiness without invoking or authorizing transfer."""
    state = 'unavailable'
    try:
        node = shutil.which('node')
        if node and BRIDGE.resolve(strict=True) == BRIDGE and BRIDGE.is_file():
            catalog = _request(node, BRIDGE, 'tools/list', {}, timeout=2)
            session = _main_session(catalog['tools'])
            reply = _request(node, BRIDGE, 'tools/call', {
                'name': 'dsh_get_session', 'arguments': {'sessionId': session},
            }, timeout=6)
            row = reply.get('structuredContent')
            if (reply.get('isError') is False and isinstance(row, dict)
                    and row.get('sessionId') == session):
                state = _readiness(row)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError):
        pass
    return {'state': state, 'observed_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
