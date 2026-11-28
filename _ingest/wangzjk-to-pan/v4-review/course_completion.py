"""Display independent course facts from the local production projection."""

from hashlib import file_digest, sha256
import json
from pathlib import Path
import re
from urllib.parse import unquote


COVERAGE_WORK_FILES = {
    'intake_sha256': 'ledger/intake.json',
    'draft_sha256': 'work/教材.md',
    'review_sha256': 'work/验收记录.json',
    'scope_declaration_sha256': 'work/范围验收.json',
    'review_template_sha256': 'work/验收填写模板.json',
    'media_inventory_sha256': 'ledger/media-inventory.json',
    'documents_summary_sha256': 'ledger/documents-summary.json',
}
RECEIVED_ISSUES = {
    'received_coverage_missing', 'received_unreadable', 'received_unresolved', 'received_reported_missing',
}
NOTE_LINK = re.compile(r'\]\((<[^<>\r\n]+>|[^)\r\n]+)\)')


def checked_file(path, root, size_limit):
    if not path.is_relative_to(root) or path.resolve(strict=True) != path or not path.is_file():
        raise ValueError('invalid course evidence path')
    with path.open('rb') as stream:
        data = stream.read(size_limit + 1)
    if len(data) > size_limit:
        raise ValueError('course evidence exceeds its read limit')
    return data


def verified_companion_install(project, entry, workspace, body):
    """Check the current installed companion files only for books that link them."""
    try:
        targets = (unquote(link.removeprefix('<').removesuffix('>')) for link in NOTE_LINK.findall(body.decode('utf-8')))
        mentioned = {name.removeprefix('../') for name in targets if name.startswith('../配套资料/')}
        if not mentioned:
            return True
        results = workspace / '整理结果'
        manifest = Path(project['evidence']['manifest'])
        snapshot = json.loads(checked_file(manifest, results, 32_000_000))
        matches = [row for row in snapshot['projects'] if row.get('id') == project['local_project_id']]
        if len(matches) != 1:
            return False
        row = matches[0]
        body_sha = sha256(body).hexdigest()
        if row['publication']['sha256'] != body_sha or Path(row['publication']['entry']) != entry:
            return False
        package = Path(row['package'])
        publication_file = package / f'.course/ledger/publications/{body_sha}.json'
        publication = json.loads(checked_file(publication_file, results, 32_000_000))
        signed_snapshot = publication['review']['snapshot']
        outputs = publication['outputs']
        # The batch binds the vault filename; the receipt uses the package's single body slot.
        body_slots = [path for key in outputs if (path := Path(key)).as_posix() == key
                      and len(path.parts) == 2 and path.parts[0] == '课件' and path.suffix == '.md'
                      and ':' not in key and '\\' not in key]
        if (publication['schema'] != 1 or publication['body'].encode('utf-8') != body
                or signed_snapshot['text_sha256'] != body_sha or len(body_slots) != 1
                or outputs[body_slots[0].as_posix()] != body_sha):
            return False
        reviewed = signed_snapshot['companions']
        companion_outputs = {key: value for key, value in outputs.items() if key.startswith('配套资料/')}
        if not isinstance(reviewed, dict) or reviewed != companion_outputs or not mentioned <= reviewed.keys():
            return False
        root = entry.parent.parent
        for filename, digest in reviewed.items():
            relative = Path(filename)
            if ('\\' in filename or ':' in filename or len(relative.parts) < 2
                    or relative.parts[0] != '配套资料' or '..' in relative.parts):
                return False
            path = root / relative
            if path.resolve(strict=True) != path or not path.is_file():
                return False
            with path.open('rb') as stream:
                if file_digest(stream, 'sha256').hexdigest() != digest:
                    return False
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return False


def current_received_issue(project, workspace):
    if (project.get('analysis') or {}).get('state') != 'in_progress':
        return None
    try:
        root = workspace / '整理结果'
        manifest = Path(project['evidence']['manifest'])
        snapshot = json.loads(checked_file(manifest, root, 32_000_000).decode('utf-8-sig'))
        matches = [row for row in snapshot['projects'] if row.get('id') == project['local_project_id']]
        if len(matches) != 1 or matches[0]['status'] != '处理中':
            return None
        course = Path(matches[0]['package']) / '.course'
        record_path = course / 'work/实收核对.json'
        raw = checked_file(record_path, root, 16_000)
        result = json.loads(raw.decode('utf-8-sig'))
        issue = result.get('received_analysis')
        if result.get('schema') != 1 or issue not in RECEIVED_ISSUES:
            return None
        for field, filename in COVERAGE_WORK_FILES.items():
            path = course / filename
            expected = result[field]
            if expected is None and field != 'intake_sha256' and not path.exists():
                continue
            if sha256(checked_file(path, root, 16_000_000)).hexdigest() != expected:
                return None
        return issue if checked_file(record_path, root, 16_000) == raw else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return None


def completion_fields(project, workspace=None):
    analysis = project.get('analysis') or {}
    review = project.get('acceptance') or {}
    book = project.get('textbook') or {}
    cleanup = project.get('cleanup') or {}
    preservation = book.get('preservation') or {}
    own = book.get('relation') == 'own'
    reader = own and (
        book.get('state') == 'registered_edition_matches'
        or (book.get('state') == 'registered_independent_derivative_matches'
            and preservation.get('formal_reader_state') == 'verified')
    )
    scope = analysis.get('scope_status')
    signed = (reader and scope in ('完整验收', '核心待补', '配套待补')
              and review.get('state') == 'scope_accepted'
              and scope == review.get('scope_status')
              and analysis.get('state') in ('completed', 'completed_received_scope'))
    result = {}
    if signed:
        result['originals'] = {
            '核心待补': 'core_missing',
            '配套待补': 'support_missing',
            '完整验收': 'documented_scope_received',
        }[scope]
        result['received_analysis'] = 'accepted_for_edition'
        result['textbook'] = {
            '核心待补': 'received_scope_usable',
            '配套待补': 'core_usable',
            '完整验收': 'documented_scope_usable',
        }[scope]
    elif reader and review.get('state') in ('historical_same_edition', 'historical_same_edition_awaiting_review'):
        result['textbook'] = 'historical_readable'
    if analysis.get('state') == 'awaiting_originals':
        result['originals'] = 'awaiting_originals'
    if analysis.get('state') in ('queued', 'in_progress', 'awaiting_acceptance'):
        issue = current_received_issue(project, workspace) if workspace is not None else None
        result['received_analysis'] = issue or analysis['state']
    preserved = (reader and book.get('state') == 'registered_independent_derivative_matches'
                 and preservation.get('state') == 'installed_verified'
                 and preservation.get('independent_review_state') == 'verified')
    evidence = cleanup.get('evidence') or {}
    if preserved and cleanup.get('scope') == 'one_reading_vault_copy':
        if (cleanup.get('state'), cleanup.get('physical_state'), cleanup.get('post_cleanup_reader_state')) == (
            'removed', 'removed_verified', 'verified'
        ) and evidence.get('receipt') and evidence.get('post_cleanup_reader'):
            result['cleanup'] = 'one_copy_removed'
        elif signed and scope == '核心待补':
            result['cleanup'] = 'retain_for_core_gap'
        elif signed and cleanup.get('state') == 'candidate_present' and cleanup.get('physical_state') == 'present_identity_matches':
            result['cleanup'] = 'one_copy_needs_authorization_check'
    elif signed and scope == '核心待补':
        result['cleanup'] = 'retain_for_core_gap'
    return result
