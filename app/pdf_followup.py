"""Explicit, read-only follow-up of PDF feature warnings in a saved intake audit."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import csv
import hashlib
import html
from io import StringIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid

from audit_materials import AuditProgress, write_report
from library_paths import library_paths, file_identity, file_state
from library_style import BASE_CSS

MAX_FILE = 512 * 1024 * 1024
MAX_AUDIT = 32 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024
CATEGORIES = {
    'active_features': ('Active features (inspection complete)', 'Script, launch, multimedia or automatic-action structure was confirmed. This does not establish malicious intent.', 'Review the identified action objects. Consider a separate Dangerzone reading copy if you do not need the original interactive features.'),
    'historical_features': ('Features outside current definitions', 'Feature structures were found in physical definitions that are not selected by the resolved current cross-reference table. This is not evidence that those features execute.', 'Review the retained historical findings in context. They are not a malware verdict or a safety clearance.'),
    'embedded_content': ('Embedded content (inspection complete)', 'Embedded-file structure was confirmed. Attachments were not extracted or opened.', 'Review why attachments are present before deciding whether to use the original.'),
    'opening_view': ('Opening view setting found', 'An opening page/view destination was identified. This explains an OpenAction warning without establishing that the whole document is safe.', 'Read the other findings and limitations; no malware verdict is made.'),
    'keyword_unconfirmed': ('Keyword warning not confirmed', 'The original warning was not confirmed as a selected active feature in the parsed dictionaries. A coincidental string match is possible.', 'Retain the result as limited static evidence, not a safety clearance.'),
    'incomplete': ('Inspection incomplete', 'The inspection could not fully resolve the file within its supported formats and limits. Findings already collected still matter.', 'Read the limitations. Recheck separately or use an isolated specialist workflow; do not treat this as a passed check.'),
    'skipped': ('Not inspected', 'The file could not be matched to the completed checks in the saved audit, or was unavailable.', 'Run Check Incoming again for changed files, then explicitly start a new follow-up.'),
}


class FollowupError(ValueError):
    pass


class VerificationError(FollowupError):
    def __init__(self, message, code, **details):
        super().__init__(message)
        self.details = {'status': code, **details}


FEATURES = {
    'javascript': 'JavaScript-related structures',
    'automatic_javascript': 'JavaScript linked to automatic actions',
    'launch': 'Launch actions',
    'automatic_action': 'Automatic actions needing interpretation',
    'rich_media': 'Rich-media structures',
    'embedded': 'Attachment-related structures',
    'opening_view': 'Opening page/view settings',
    'external_action': 'External-link or data actions',
}
ACTIVE = {'javascript', 'automatic_javascript', 'launch', 'automatic_action', 'rich_media'}
COVERAGE = {'not_inspected': 'Not inspected', 'incomplete': 'Inspection incomplete',
            'complete_within_limits': 'Complete within supported checks'}


def inspection_status(row):
    if row['category'] == 'skipped':
        return 'not_inspected'
    return 'incomplete' if row['category'] == 'incomplete' or row['limitations'] else 'complete_within_limits'


def summary_counts(rows):
    counts = Counter()
    physical = Counter()
    for row in rows:
        counts.update({f['feature'] for f in row['findings']})
        physical.update({f['feature'] for f in row.get('physical_only_findings', [])})
    return {'coverage': dict(Counter(inspection_status(row) for row in rows)),
            'features_by_file': {key: counts[key] for key in FEATURES},
            'physical_only_features_by_file': {key: physical[key] for key in FEATURES},
            'files_with_active_features': sum(bool(ACTIVE & {f['feature'] for f in row['findings']}) for row in rows)}


def detail_lines(row):
    lines = []
    revision = row.get('revision_resolution', {})
    if revision.get('status') == 'resolved_current_xref':
        lines.append('Revision selection: current cross-reference definitions resolved using ' + revision['engine'] +
                     '; saved PDF versions: ' + str(revision.get('versions') or 'not recorded'))
    elif revision.get('status') == 'unresolved':
        lines.append('Revision selection remains unresolved: ' + revision.get('reason', 'Read the limitations'))
    counts = Counter(f['feature'] for f in row['findings'])
    if counts:
        lines.append('Findings: ' + '; '.join(f'{FEATURES.get(key, key)} ({number} objects)' for key, number in counts.items()))
    for review in row.get('script_reviews', []):
        lines.append(f'Object {review["object"]}, script text patterns: ' + '; '.join(review['observations']))
    for review in row.get('launch_reviews', []):
        lines.append(f'Object {review["object"]}, launch target: {review["target"]}' + ('; parameters present' if review['parameters_present'] else ''))
    physical = Counter(f['feature'] for f in row.get('physical_only_findings', []))
    if physical:
        lines.append('Physical-only findings outside current definitions: ' + '; '.join(f'{FEATURES.get(key, key)} ({number} objects)' for key, number in physical.items()))
    lines.extend('Limit: ' + note for note in row['limitations'])
    return lines


def select_paths(selection, paths):
    if not paths:
        return dict(selection, selection_scope='All feature-warning PDFs in the saved audit',
                    available_warning_pdfs=len(selection['files']))
    if len(paths) != len(set(paths)):
        raise FollowupError('The selected PDF list repeats a path')
    available = {row['relative_path']: row for row in selection['files']}
    if any(path not in available for path in paths):
        raise FollowupError('Every selected path must exactly match a feature-warning PDF in the saved audit')
    return dict(selection, files=[available[path] for path in paths],
                selection_scope='Explicit subset only; other PDFs were not checked in this run',
                available_warning_pdfs=len(available))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FollowupError('Repeated JSON key in the saved audit')
        result[key] = value
    return result


def local_file(root, relative):
    if not isinstance(relative, str) or len(relative) > 4096:
        raise FollowupError('Invalid audit file path')
    parts = PurePosixPath(relative)
    if (parts.is_absolute() or parts.as_posix() != relative or '\\' in relative
            or len(parts.parts) < 2 or parts.parts[0] not in ('Content', 'Incoming', 'Incoming Files')
            or any(p in ('.', '..') or p.startswith('.') for p in parts.parts)):
        raise FollowupError('Audit path is outside the permitted materials folders')
    target = root.joinpath(*parts.parts)
    cursor = target
    while cursor != root:
        if cursor.is_symlink():
            raise FollowupError('Symbolic links are not followed')
        cursor = cursor.parent
    if not target.resolve().is_relative_to(root):
        raise FollowupError('Audit path leaves the library')
    return target


def read_selection(layout):
    path = layout.reports / 'intake_audit.json'
    if layout.reports.is_symlink() or path.is_symlink() or not path.is_file():
        raise FollowupError('No saved intake audit was found. Run Check Incoming first, then choose Deeper PDF Check.')
    if not path.resolve().is_relative_to(layout.root) or path.stat().st_size > MAX_AUDIT:
        raise FollowupError('The saved audit is outside the library or exceeds the report size limit')
    raw = path.read_bytes()
    data = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique_object)
    if not isinstance(data, dict) or not isinstance(data.get('files'), list) or len(data['files']) > 100000:
        raise FollowupError('The saved intake audit has an unsupported format')
    selected, seen = [], set()
    for row in data['files']:
        if not isinstance(row, dict):
            raise FollowupError('The saved intake audit contains an invalid record')
        name, indicators = row.get('relative_path'), row.get('pdfid')
        if not isinstance(name, str) or not isinstance(indicators, str):
            raise FollowupError('The saved intake audit contains missing file details')
        if not name.lower().endswith('.pdf') or not indicators.startswith('Indicators found:'):
            continue
        # Check path syntax even for a missing file. Filesystem changes become
        # per-file skipped results later, while traversal is a report error.
        parts = PurePosixPath(name)
        if (parts.is_absolute() or parts.as_posix() != name or '\\' in name
                or len(parts.parts) < 2 or parts.parts[0] not in ('Content', 'Incoming', 'Incoming Files')
                or any(p in ('.', '..') or p.startswith('.') for p in parts.parts)):
            raise FollowupError('The audit contains an unsafe materials path')
        key = unicodedata.normalize('NFC', name).casefold()
        if key in seen:
            raise FollowupError('The saved intake audit repeats a PDF path')
        seen.add(key)
        selected.append(row)
    return {'audit_sha256': digest(raw), 'audit_date': data.get('scanned_at', 'not recorded'),
            'scanned_folder': data.get('scanned_folder', 'not recorded'), 'files': selected}


def source_info(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise FollowupError('The source is not an ordinary file')
    return info


def stamp(path):
    info = source_info(path)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def copy_verified(root, row, destination):
    expected = row.get('sha256')
    if not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise FollowupError('The saved audit has no valid SHA-256 fingerprint')
    if not isinstance(row.get('clamav'), str) or not row['clamav'].startswith('No detection'):
        raise FollowupError('The saved audit has no completed antivirus result without a detection for this file')
    if row.get('file_type_check') != 'PDF header present':
        raise FollowupError('The saved audit did not confirm the PDF header')
    path = local_file(root, row['relative_path'])
    before_info = source_info(path)
    before = (before_info.st_dev, before_info.st_ino, before_info.st_size,
              before_info.st_mtime_ns, before_info.st_ctime_ns)
    if type(row.get('size_bytes')) is not int or before[2] != row['size_bytes']:
        raise VerificationError('File size differs from the saved audit; a new audit is needed for this file',
                                'size_mismatch', expected_size=row.get('size_bytes'), observed_size=before[2])
    if before[2] > MAX_FILE:
        raise FollowupError('File exceeds the 512 MiB follow-up limit')
    hasher = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(descriptor, 'rb') as source, destination.open('xb') as target:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise FollowupError('The source changed to a non-file')
        if file_identity(opened) != file_identity(before_info):
            raise VerificationError('The source was replaced or changed while opening it; no document was parsed', 'source_changed_during_copy')
        size = 0
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE:
                raise FollowupError('The source grew beyond the follow-up limit')
            hasher.update(chunk)
            target.write(chunk)
        finished = os.fstat(source.fileno())
    observed = hasher.hexdigest()
    if stamp(path) != before or file_state(finished) != file_state(opened):
        raise VerificationError('The source changed during copying; no document was parsed',
                                'source_changed_during_copy', expected_sha256=expected, observed_sha256=observed)
    if observed != expected:
        raise VerificationError('The current fingerprint differs from the saved audit. The saved antivirus result cannot be reused; a new audit is needed for this file',
                                'fingerprint_mismatch', expected_sha256=expected, observed_sha256=observed,
                                source_stable_during_copy=True)
    return path, before


def worker_command(internal, copy):
    command = [sys.executable, '-I', '-B', str(internal / 'pdf_inspection.py'), str(copy)]
    if sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file():
        # Read restrictions are deliberately not claimed as filesystem
        # isolation. The worker can read local runtime files; it cannot write
        # files or use the network. It receives only a verified temporary copy.
        profile = '(version 1)(allow default)(deny network*)(deny file-write*)'
        return ['/usr/bin/sandbox-exec', '-p', profile, *command], 'macOS network and file-write restrictions'
    return command, 'Separate process; no operating-system sandbox available'


def inspect_one(layout, internal, row, timeout):
    started = time.monotonic()
    result = {'relative_path': row['relative_path'], 'audit_pdfid': row['pdfid'], 'sha256': row.get('sha256', ''),
              'category': 'skipped', 'priority': 'unresolved', 'findings': [], 'limitations': [],
              'verification': {'status': 'not_verified'}}
    try:
        with tempfile.TemporaryDirectory(prefix='pinakeys-pdf-copy-') as temp:
            copy = Path(temp) / 'input.pdf'
            path, original_stamp = copy_verified(layout.root, row, copy)
            result['verification'] = {'status': 'matched_saved_audit', 'sha256': row['sha256'],
                                      'source_stable_during_copy': True}
            argv, protection = worker_command(internal, copy)
            result['worker_protection'] = protection
            result['category'] = 'incomplete'
            completed = subprocess.run(argv, cwd=temp, stdin=subprocess.DEVNULL, capture_output=True,
                                       timeout=timeout, check=False,
                                       **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
            if len(completed.stdout) > MAX_OUTPUT or len(completed.stderr) > MAX_OUTPUT:
                raise FollowupError('Worker output exceeded its size limit')
            try:
                findings = json.loads(completed.stdout)
            except ValueError as exc:
                raise FollowupError('The inspection worker did not return a complete result') from exc
            if completed.returncode or not isinstance(findings, dict) or findings.get('error'):
                raise FollowupError(str(findings.get('error', 'Worker exited before completing'))[:240] if isinstance(findings, dict) else 'Invalid worker result')
            if findings.get('category') not in CATEGORIES or not isinstance(findings.get('findings'), list):
                raise FollowupError('Invalid inspection result')
            result.update(findings)
            if stamp(path) != original_stamp:
                result['category'] = 'incomplete'
                result['limitations'].append('The original file changed during inspection; these findings describe the verified copy')
                result['verification']['status'] = 'source_changed_during_inspection'
    except subprocess.TimeoutExpired:
        result['category'] = 'incomplete'
        result['limitations'].append(f'The inspection exceeded its {timeout:g}-second limit')
    except VerificationError as exc:
        result['verification'] = exc.details
        result['limitations'].append(str(exc))
    except (OSError, ValueError) as exc:
        result['limitations'].append(str(exc)[:320])
    result['inspection_status'] = inspection_status(result)
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return result


def escaped(value):
    return html.escape(str(value), quote=True)


def html_report(report):
    rows = report['files']
    counts = Counter(r['category'] for r in rows)
    summary = summary_counts(rows)
    row_ids = {id(row): index for index, row in enumerate(rows)}
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
             '<title>Deeper PDF findings · Pinakeys</title><style>' + BASE_CSS,
             'article{border-top:1px solid var(--rule);padding:22px 0}summary{cursor:pointer;color:var(--ink)}',
             'code,.path{overflow-wrap:anywhere}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px;border-bottom:1px solid var(--rule)}',
             '.badge{display:inline-block;background:var(--wash);padding:4px 10px;margin:0 6px 8px 0}li{margin:8px 0}',
             '</style></head><body><header class="site-header"><span class="brand">Pinakeys</span></header><main>',
             '<h1>Deeper PDF findings</h1><p class="lede">Evidence behind the saved audit’s PDF feature warnings. No malware verdict or safety certificate.</p>',
             f'<p><strong>{len(rows)} flagged PDFs selected.</strong> Originals were not renamed, moved, converted or written to. Nothing was opened in a document viewer.</p>',
             f'<p>Report: {escaped(report["created_at_utc"])} · Source audit: {escaped(report["audit_date"])}<br>Scope: {escaped(report["scanned_folder"])}</p>',
             f'<p>Inspection time: {escaped(report.get("elapsed_seconds", "not recorded"))} seconds.</p>',
             f'<p><strong>Selection:</strong> {escaped(report.get("selection_scope", "Saved audit selection"))}. '
             f'{len(rows)} selected out of {report.get("available_warning_pdfs", len(rows))} saved feature-warning PDFs.</p>',
             '<p>Each available file was checked against its saved SHA-256 fingerprint before a temporary copy was parsed. Scripts were not executed; attachments were not extracted or opened. This check does not find every possible PDF exploit.</p>',
             '<p>On macOS the worker denies network access and file writes where the built-in restriction tool is available. Other systems use a separate process without an operating-system sandbox. Per-file protection is recorded below.</p>',
             '<h2>Inspection coverage</h2><p>Completeness and features are counted separately. Complete means within this tool’s supported checks, not safe.</p>',
             '<table><thead><tr><th>Coverage</th><th>Files</th></tr></thead><tbody>']
    for key, label in COVERAGE.items():
        parts.append(f'<tr><td>{label}</td><td>{summary["coverage"].get(key, 0)}</td></tr>')
    parts.append('</tbody></table><h2>Features observed across all inspected files</h2><p>Includes partial findings from incomplete inspections. Counts overlap; each file is counted once per feature.</p><table><thead><tr><th>Feature</th><th>Files</th></tr></thead><tbody>')
    for key, label in FEATURES.items():
        parts.append(f'<tr><td>{label}</td><td>{summary["features_by_file"][key]}</td></tr>')
    parts.append('</tbody></table><p>For resolved revisions, these counts use current cross-reference definitions. When revisions remain unresolved, findings describe physical definitions and may not be current.</p>')
    if any(summary['physical_only_features_by_file'].values()):
        parts.append('<h2>Physical-only findings outside current definitions</h2><p>Retained evidence from older or unselected definitions. These counts do not imply current execution or malicious intent.</p><table><thead><tr><th>Feature</th><th>Files</th></tr></thead><tbody>')
        for key, label in FEATURES.items():
            if summary['physical_only_features_by_file'][key]:
                parts.append(f'<tr><td>{label}</td><td>{summary["physical_only_features_by_file"][key]}</td></tr>')
        parts.append('</tbody></table>')
    parts.append('<h2>Files needing closer feature review</h2><ul>')
    for index, row in enumerate(rows):
        if ACTIVE & {finding['feature'] for finding in row['findings'] + row.get('physical_only_findings', [])}:
            parts.append(f'<li><a href="#file-{index}">{escaped(row["relative_path"])}</a> · {COVERAGE[inspection_status(row)]}</li>')
    if not summary['files_with_active_features']:
        parts.append('<li>No selected active feature was identified. Check unresolved limitations below.</li>')
    parts.append('</ul><h2>File display groups</h2><p>These groups are exclusive. Files with limits appear under incomplete even when active or embedded features were found. Use the feature counts above.</p><table><thead><tr><th>Display group</th><th>Files</th></tr></thead><tbody>')
    for key, (label, _, _) in CATEGORIES.items():
        parts.append(f'<tr><td><a href="#group-{key}">{escaped(label)}</a></td><td>{counts[key]}</td></tr>')
    parts.append('</tbody></table><p>“Keyword warning not confirmed” can explain false alarms; it does not establish safety. An incomplete inspection can still contain important positive findings.</p>')
    for category, (label, explanation, next_step) in CATEGORIES.items():
        selected = [r for r in rows if r['category'] == category]
        parts.append(f'<section id="group-{category}"><h2>{escaped(label)} · {len(selected)}</h2><p>{escaped(explanation)}</p>')
        if not selected:
            parts.append('<p>No files in this display group.</p>')
        for row in selected:
            parts.extend([f'<article id="file-{row_ids[id(row)]}"><h3>{escaped(PurePosixPath(row["relative_path"]).name)}</h3>',
                          f'<p class="path">{escaped(row["relative_path"])}</p>',
                          f'<span class="badge">{escaped(row["priority"])}</span>',
                          f'<p><strong>Coverage:</strong> {COVERAGE[inspection_status(row)]}</p>',
                          f'<p><strong>Original screening warning:</strong> {escaped(row["audit_pdfid"])}</p>'])
            if row['findings']:
                parts.append('<ul>')
                for finding in row['findings']:
                    origin = ' (inside compressed object stream ' + finding['inside_object_stream'] + ')' if finding.get('inside_object_stream') else ''
                    parts.append(f'<li><strong>Object {escaped(finding["object"] + origin)}:</strong> {escaped(finding["detail"])}</li>')
                parts.append('</ul>')
            else:
                parts.append('<p>No selected active-feature structure was confirmed in this inspection.</p>' if category == 'keyword_unconfirmed' else '<p>No structural findings were returned.</p>')
            for line in detail_lines(dict(row, findings=[], limitations=[])):
                parts.append(f'<p>{escaped(line)}</p>')
            if row.get('script_reviews'):
                parts.append('<p>Script descriptions are bounded text-pattern observations. Comments or strings can match; no script was executed and its behaviour is not fully proven.</p>')
            if row.get('physical_only_findings'):
                parts.append('<details><summary>Physical findings outside current definitions</summary><ul>')
                for finding in row['physical_only_findings']:
                    parts.append(f'<li>Object {escaped(finding["object"])} · {escaped(FEATURES.get(finding["feature"], finding["feature"]))}: {escaped(finding["detail"])}</li>')
                parts.append('</ul></details>')
            verification = row.get('verification', {})
            if verification:
                parts.append(f'<p><strong>File verification:</strong> {escaped(verification.get("status", "not recorded"))}</p>')
                for key, label in [('expected_sha256', 'Saved fingerprint'), ('observed_sha256', 'Observed fingerprint')]:
                    if verification.get(key):
                        parts.append(f'<p>{label}: <code>{escaped(verification[key])}</code></p>')
            for note in row['limitations']:
                parts.append(f'<p class="warning">{escaped(note)}</p>')
            if row.get('revision_notice'):
                parts.append(f'<p>{escaped(row["revision_notice"])}</p>')
            parts.extend([f'<p><strong>Next:</strong> {escaped(next_step)}</p>',
                          '<details><summary>Inspection record</summary>',
                          f'<p>Worker: {escaped(row.get("worker_protection", "not started"))}<br>Objects inspected: {row.get("objects_inspected", 0)}; compressed-object origins: {row.get("compressed_objects_inspected") if row.get("compressed_objects_inspected") is not None else "not enumerated by revision reader"}</p>',
                          f'<p>SHA-256: <code>{escaped(row["sha256"])}</code></p></details></article>'])
        parts.append('</section>')
    if not rows:
        parts.append('<p>No PDF feature warnings were present in the saved audit. No documents were inspected.</p>')
    parts.append('</main><footer>Static inspection only · No uploads · No antivirus rescan · No automatic trust, deletion or conversion<br>Dangerzone, if separately chosen later, creates a reading copy and can lose links, bookmarks, forms and metadata.</footer></body></html>')
    return ''.join(parts)


def csv_report(rows):
    stream = StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['relative_path', 'finding_group', 'priority', 'findings', 'limitations', 'audit_pdfid', 'sha256',
                     'inspection_status', 'observed_features', 'script_and_launch_details', 'revision_status',
                     'physical_only_findings', 'verification_status', 'observed_sha256'])
    for row in rows:
        values = [row['relative_path'], CATEGORIES[row['category']][0], row['priority'],
                  ' | '.join(f'Object {f["object"]}: {f["detail"]}' for f in row['findings']),
                  ' | '.join(row['limitations']), row['audit_pdfid'], row['sha256'], inspection_status(row),
                  '; '.join(sorted({f['feature'] for f in row['findings']})),
                  ' | '.join(detail_lines(dict(row, findings=[], limitations=[]))),
                  row.get('revision_resolution', {}).get('status', 'not recorded'),
                  ' | '.join(f'Object {f["object"]}: {FEATURES.get(f["feature"], f["feature"])}' for f in row.get('physical_only_findings', [])),
                  row.get('verification', {}).get('status', 'not recorded'),
                  row.get('verification', {}).get('observed_sha256', '')]
        writer.writerow(["'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value for value in values])
    return stream.getvalue()


def run(layout, internal, selection, progress, timeout=90):
    started = time.monotonic()
    rows, total = [], len(selection['files'])
    progress.emit(1, 'running', 0, total)
    for index, row in enumerate(selection['files'], 1):
        progress.emit(1, 'running', index, total, row['relative_path'])
    progress.emit(1, 'complete', total, total)
    progress.emit(2, 'running', 0, total)
    for index, row in enumerate(selection['files'], 1):
        progress.emit(2, 'running', index - 1, total, row['relative_path'])
        result = inspect_one(layout, internal, row, timeout)
        rows.append(result)
        print(f'[{CATEGORIES[result["category"]][0]}] {row["relative_path"]}', flush=True)
        print('  Coverage: ' + COVERAGE[result['inspection_status']] + f'; {result["elapsed_seconds"]:.1f} seconds', flush=True)
        lines = detail_lines(result)
        # Always show limitations, even if many launch/script details exist.
        for line in lines[:8]:
            print('  ' + line, flush=True)
        if len(lines) > 8:
            print(f'  More details are in the saved report ({len(lines)} detail lines).', flush=True)
            for note in result['limitations']:
                print('  Limit: ' + note, flush=True)
        progress.emit(2, 'running', index, total, row['relative_path'])
    progress.emit(2, 'complete', total, total)
    progress.emit(3, 'running', 0, 3)
    layout.prepare_reports()
    folder = layout.reports / 'PDF Checks'
    if folder.is_symlink() or not folder.resolve().is_relative_to(layout.root):
        raise FollowupError('The deeper-report folder must stay inside the library')
    folder.mkdir(exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    destination = folder / run_id
    destination.mkdir()
    report = {'version': 3, 'created_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'audit_date': selection['audit_date'], 'audit_sha256': selection['audit_sha256'],
              'scanned_folder': selection['scanned_folder'], 'files': rows,
              'selection_scope': selection.get('selection_scope', 'Saved audit selection'),
              'available_warning_pdfs': selection.get('available_warning_pdfs', total),
              'summary': summary_counts(rows), 'elapsed_seconds': round(time.monotonic() - started, 3),
              'notice': 'Static evidence only. Findings are not a malware verdict or proof of safety. Originals are unchanged.'}
    write_report(destination / 'Report.html', html_report(report))
    write_report(destination / 'findings.csv', csv_report(rows), 'utf-8-sig')
    progress.emit(3, 'running', 1, 3)
    write_report(destination / 'findings.json', json.dumps(report, ensure_ascii=False, indent=2))
    progress.emit(3, 'running', 2, 3)
    latest = layout.reports / 'deeper_pdf_check.html'
    if latest.is_symlink():
        raise FollowupError('The latest-report link must not be symbolic')
    link = 'PDF%20Checks/' + run_id + '/Report.html'
    write_report(latest, '<!doctype html><html lang="en"><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=' + link + '"><title>Deeper PDF findings</title><p><a href="' + link + '">Open the latest completed Deeper PDF report</a></p></html>')
    progress.emit(3, 'complete', 3, 3)
    print('\nDeeper PDF summary', flush=True)
    print(f'{total} PDFs selected from the saved audit. No antivirus rescan was run. Time: {report["elapsed_seconds"]:.1f} seconds.', flush=True)
    print('Selection: ' + report['selection_scope'], flush=True)
    print('Features observed, including incomplete inspections (file counts overlap):', flush=True)
    for feature, label in FEATURES.items():
        print(f'  {label}: {report["summary"]["features_by_file"][feature]}', flush=True)
    if any(report['summary']['physical_only_features_by_file'].values()):
        print('Physical-only findings outside current definitions (not evidence of current execution):', flush=True)
        for feature, count in report['summary']['physical_only_features_by_file'].items():
            if count:
                print(f'  {FEATURES[feature]}: {count}', flush=True)
    print('Inspection coverage: ' + '; '.join(f'{COVERAGE[key]}: {value}' for key, value in report['summary']['coverage'].items()), flush=True)
    print('Exclusive display groups (incomplete files can still contain active features):', flush=True)
    counts = Counter(row['category'] for row in rows)
    for category, (label, _, _) in CATEGORIES.items():
        print(f'  {label}: {counts[category]}', flush=True)
    print('No malware verdict is made. Read each finding and limitation. Originals were left unchanged.', flush=True)
    print('Report: ' + str(destination.relative_to(layout.root) / 'Report.html'), flush=True)
    print('Use Open PDF Findings in Pinakeys. CSV and JSON are saved beside the HTML report.', flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--progress-file', help=argparse.SUPPRESS)
    parser.add_argument('--timeout', type=float, default=90, help='Per-PDF time limit, 1–300 seconds (default 90)')
    parser.add_argument('--only', action='append', metavar='RELATIVE_PATH', help='Inspect only this exact saved-audit PDF path; repeat to select a subset')
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 300:
        parser.error('The per-PDF timeout must be between 1 and 300 seconds')
    layout = library_paths(__file__)
    progress = AuditProgress(args.progress_file)
    try:
        selection = select_paths(read_selection(layout), args.only)
        print(f'Deeper PDF Check: {len(selection["files"])} feature-warning PDFs from the saved audit.', flush=True)
        print('Originals stay unchanged. Verified temporary copies are inspected without rendering or executing document scripts.', flush=True)
        run(layout, Path(__file__).resolve().parent, selection, progress, args.timeout)
        return 0
    except (OSError, ValueError) as exc:
        print('Could not complete Deeper PDF Check: ' + str(exc), flush=True)
        return 1
    finally:
        progress.close()


if __name__ == '__main__':
    from library_paths import configure_console
    configure_console()
    raise SystemExit(main())
