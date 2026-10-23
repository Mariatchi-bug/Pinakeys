#!/usr/bin/env python3
"""Non-destructive preflight audit of files stored on the same removable drive.

No file contents are executed, opened in a viewer, unpacked, deleted, or moved.
Reads a short header, hashes bytes and optionally invokes independently installed
security/validation programs. It does not certify any document as safe.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from library_paths import library_paths

LAYOUT = library_paths(__file__)
ROOT = LAYOUT.root
REPORTS = {'intake_audit.csv', 'intake_audit.json'}
COLUMNS = [
    'relative_path', 'size_bytes', 'sha256', 'file_type_check', 'clamav',
    'pdfid', 'epubcheck', 'status', 'review_reasons', 'checks_not_run',
]
PDF_INDICATORS = ('/JS', '/JavaScript', '/AA', '/OpenAction', '/Launch', '/EmbeddedFile', '/RichMedia')


def run_tool(argv: list[str], timeout=120, on_line=None) -> subprocess.CompletedProcess:
    if on_line is not None:
        return stream_tool(argv, timeout, on_line)
    return subprocess.run(argv, capture_output=True, text=True, errors='replace',
                          timeout=timeout, check=False, stdin=subprocess.DEVNULL)


def stream_tool(argv, timeout, on_line):
    """Read a tool live without blocking Tk or losing the batch's final output.

    A reader thread works on Windows pipes too. The supervising thread enforces
    the deadline even when the tool is silent or has not finished a line.
    """
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, text=True, encoding='utf-8',
                               errors='replace', bufsize=1)
    messages = queue.Queue()
    output = []

    def read():
        try:
            for line in process.stdout:
                messages.put(line)
        finally:
            messages.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                line = messages.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if line is None:
                break
            output.append(line)
            on_line(line.rstrip('\r\n'))
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return subprocess.CompletedProcess(argv, code, ''.join(output), '')
    except BaseException as exc:
        if process.poll() is None:
            process.kill()
        process.wait()
        reader.join(timeout=2)
        while not messages.empty():
            line = messages.get_nowait()
            if line is not None:
                output.append(line)
        if isinstance(exc, subprocess.TimeoutExpired):
            exc.output = ''.join(output)
        raise
    finally:
        reader.join(timeout=2)
        process.stdout.close()


from operation_progress import OperationProgress as AuditProgress


def clam_line(line, known):
    position = line.rfind(': ')
    while position >= 0 and line[:position] not in known:
        position = line.rfind(': ', 0, position)
    return (known[line[:position]], line[position + 2:]) if position >= 0 else (None, '')


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(data)
    return digest.hexdigest()


def header_kind(header: bytes) -> str:
    if header[:1024].lstrip().startswith(b'%PDF-'):
        return 'PDF'
    if header.startswith((b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')):
        return 'ZIP'
    if header.startswith(b'MZ'):
        return 'Windows executable'
    if header.startswith(b'\x7fELF'):
        return 'ELF executable'
    if header[:4] in (b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe'):
        return 'Mach-O executable'
    if header.lstrip().lower().startswith((b'<!doctype html', b'<html')):
        return 'HTML'
    return 'unknown'


def check_type(path: Path) -> tuple[str, str]:
    with path.open('rb') as handle:
        header = handle.read(4096)
    if not header:
        return 'Empty file', 'Empty file (0 bytes)'
    kind = header_kind(header)
    ext = path.suffix.casefold()
    if ext == '.pdf':
        return ('PDF header present', '') if kind == 'PDF' else (f'PDF extension, {kind} signature', 'PDF signature mismatch')
    if ext == '.epub':
        return ('ZIP container signature present (EPUB not fully validated)', '') if kind == 'ZIP' else (f'EPUB extension, {kind} signature', 'EPUB container signature mismatch')
    if kind in ('Windows executable', 'ELF executable', 'Mach-O executable'):
        return (f'{kind} signature', 'Executable signature: confirm file is expected')
    if ext and kind in ('PDF', 'ZIP') and ext not in ('.zip', '.cbz', '.jar', '.odt', '.docx', '.xlsx', '.pptx'):
        return (f'{kind} signature, extension {ext}', 'File extension and signature merit review')
    return (f'{kind} signature (no format validation for {ext or "extensionless file"})', '')


def file_stamp(path: Path) -> tuple:
    """Reject replaced symlinks/non-files and notice ordinary concurrent edits."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Not a regular file')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def check_clamav_batch(paths: list[Path], executable: str | None, progress=None) -> dict[Path, tuple[str, str]]:
    """One engine load, with explicit per-file results; absence is never success.

    A private file list avoids command-line length limits and shell evaluation.
    ClamAV's line-based file list cannot represent CR/LF in a filename; report
    those files as incomplete rather than scanning an unintended path.
    """
    if not executable:
        return {path: ('NOT RUN', '') for path in paths}
    failure = ('ERROR', 'ClamAV did not return a completed result for this file')
    results = {path: failure for path in paths}
    eligible = []
    for path in paths:
        try:
            if '\n' in str(path) or '\r' in str(path):
                raise ValueError('Line break in filename')
            str(path).encode('utf-8')
        except (ValueError, UnicodeError):
            results[path] = ('ERROR', 'ClamAV batch cannot represent this filename; check separately')
        else:
            eligible.append(path)
    notified = set()
    known = {str(path): path for path in eligible}

    def notify(kind, path):
        if progress is None:
            return
        if kind == 'done':
            if path in notified:
                return
            notified.add(path)
        progress(kind, path)

    def live_line(line):
        if line.startswith('Scanning ') and line[9:] in known:
            notify('file', known[line[9:]])
        path, message = clam_line(line, known)
        if path is not None and (message in ('OK', 'Empty file') or message.endswith((' FOUND', ' ERROR'))):
            notify('done', path)

    if not eligible:
        for path in paths:
            notify('done', path)
        return results
    try:
        with tempfile.TemporaryDirectory(prefix='pinakeys-clamav-') as directory:
            manifest = Path(directory) / 'files.txt'
            manifest.write_text(''.join(str(path) + '\n' for path in eligible), encoding='utf-8')
            # No recursive folder scan, no remove/move, no new process group:
            # the launcher's Stop action also terminates this child process.
            arguments = [executable, '--stdout', '--no-summary', '--file-list=' + str(manifest)]
            if progress is not None:
                arguments.append('--verbose')
            result = run_tool(arguments, timeout=240 * len(eligible),
                              **({'on_line': live_line} if progress is not None else {}))
    except (OSError, subprocess.TimeoutExpired) as exc:
        # A timed-out batch is incomplete; retain any explicit detections.
        output = getattr(exc, 'stdout', '') or ''
        if isinstance(output, bytes):
            output = output.decode('utf-8', errors='replace')
        result = subprocess.CompletedProcess([], -1, output, '')
        for path in eligible:
            results[path] = ('ERROR', f'ClamAV could not finish: {type(exc).__name__}')
    explicit_errors = set()
    output = (result.stdout or '') + '\n' + (result.stderr or '')
    for line in output.split('\n'):
        line = line.rstrip('\r')
        # Work from the right so paths containing ': ' cannot steal a result
        # from a longer filename with the same prefix.
        path, message = clam_line(line, known)
        if path is None:
            continue
        if message.endswith(' FOUND'):
            results[path] = ('DETECTION', 'ClamAV flagged file; do not open original')
        elif results[path][0] != 'DETECTION':
            if message in ('OK', 'Empty file') and result.returncode in (0, 1, 2) and path not in explicit_errors:
                results[path] = ('No detection (not proof of safety)', '')
            elif message.endswith(' ERROR'):
                explicit_errors.add(path)
                results[path] = ('ERROR', 'ClamAV returned an error for this file')
    if result.returncode not in (0, 1, 2) or (
            result.returncode == 1 and not any(value[0] == 'DETECTION' for value in results.values())):
        for path in eligible:
            if results[path][0] != 'DETECTION':
                results[path] = ('ERROR', 'ClamAV batch did not finish reliably; run the audit again')
    for path in paths:
        notify('done', path)
    return results


def check_clamav(path: Path, executable: str | None) -> tuple[str, str]:
    """Single-file entry point; audits use check_clamav_batch once instead."""
    return check_clamav_batch([path], executable)[path]


def check_pdfid(path: Path, executable: str | None) -> tuple[str, str]:
    if not executable:
        return 'NOT RUN', ''
    argv = [sys.executable, executable, str(path)] if executable.lower().endswith('.py') else [executable, str(path)]
    try:
        result = run_tool(argv, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 'ERROR', f'PDFiD failed: {type(exc).__name__}'
    output = (result.stdout or '') + '\n' + (result.stderr or '')
    if result.returncode != 0 or not re.search(r'^PDFiD\s+\d', output, re.MULTILINE):
        return 'ERROR', 'PDFiD failed or returned unrecognized output'
    suspicious = []
    for marker in PDF_INDICATORS:
        match = re.search(r'^\s*' + re.escape(marker) + r'\s+(\d+)\b', output, re.MULTILINE)
        if match is None:
            return 'ERROR', 'PDFiD returned incomplete indicator output'
        if match and int(match.group(1)) > 0:
            suspicious.append(f'{marker}: {match.group(1)}')
    if suspicious:
        return 'Indicators found: ' + ', '.join(suspicious), 'PDF contains active/embedded features requiring review (not necessarily malware)'
    return 'No selected indicators detected (not proof of safety)', ''


def check_epubcheck(path: Path, executable: str | None, jar: str | None) -> tuple[str, str]:
    if not executable and not jar:
        return 'NOT RUN', ''
    argv = ['java', '-jar', jar, str(path)] if jar else [executable, str(path)]
    try:
        result = run_tool(argv, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 'ERROR', f'EPUBCheck failed: {type(exc).__name__}'
    if result.returncode == 0:
        return 'Validation passed (not a malware scan)', ''
    if result.returncode == 1:
        return 'Validation errors', 'EPUBCheck reported conformance errors (not necessarily malicious)'
    return 'ERROR', 'EPUBCheck failed to run'


def write_report(path: Path, content: str, encoding='utf-8') -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding=encoding, newline='', dir=path.parent,
                                         prefix='.materials-audit-', suffix='.tmp', delete=False) as stream:
            temporary = stream.name
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def generate_csv(rows: list[dict]) -> str:
    stream = StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=COLUMNS)
    writer.writeheader()
    for row in rows:
        # Protect spreadsheet users from formula injection in filename strings.
        writer.writerow({key: "'" + str(row[key]) if str(row[key]).lstrip().startswith(('=', '+', '-', '@'))
                         else row[key] for key in COLUMNS})
    return stream.getvalue()


def unfinished_tools(row: dict) -> list[str]:
    """Include failures and skipped checks, also on files already flagged."""
    extension = Path(row['relative_path']).suffix.casefold()
    applicable = [('clamav', 'ClamAV')]
    if extension == '.pdf':
        applicable.append(('pdfid', 'PDFiD'))
    elif extension == '.epub':
        applicable.append(('epubcheck', 'EPUBCheck'))
    return [label for key, label in applicable
            if row[key].startswith(('NOT RUN', 'SKIPPED', 'ERROR'))]


def checks_incomplete(row: dict) -> bool:
    return bool(row['checks_not_run'] or unfinished_tools(row)
                or row['file_type_check'] == 'ERROR' or not row['sha256'])


def format_audit_summary(rows: list[dict], folder: str, report_folder: str) -> str:
    """Explain existing results without interpreting a limited check as safety."""
    total = len(rows)
    lines = ['\nAudit summary', f'{total} {"file" if total == 1 else "files"} checked in {folder}.']
    if not rows:
        lines.extend(['No files were found to check.',
                      f'Next: put arrivals in {folder}, then choose Check Incoming again.',
                      'Your incoming files were left unchanged.'])
        return '\n'.join(lines)

    flagged = [row for row in rows if row['status'] == 'REVIEW']
    if flagged:
        lines.append(f'{len(flagged)} {"file needs" if len(flagged) == 1 else "files need"} your attention:')
        reasons = Counter(reason for row in flagged
                          for reason in set(row['review_reasons'].split(' | ')) if reason)
        labels = {
            'Empty file (0 bytes)': 'Empty files (0 bytes)',
            'PDF signature mismatch': 'Files labelled PDF whose contents do not look like a PDF',
            'EPUB container signature mismatch': 'Files labelled EPUB without a ZIP container signature',
            'File extension and signature merit review': 'Unusual file extensions for PDF/ZIP contents',
            'Executable signature: confirm file is expected': 'Files with executable-program signatures',
        }
        lines.extend(f'  - {labels.get(reason, reason)}: {count}'
                     for reason, count in sorted(reasons.items()))
        if sum(reasons.values()) > len(flagged):
            lines.append('A file can have more than one review reason.')
        other = total - len(flagged)
        if other:
            lines.append(f'{other} other {"file had" if other == 1 else "files had"} no review warnings from the checks that ran.')
    else:
        lines.append('No review warnings were raised by the checks that ran.')

    incomplete = sum(checks_incomplete(row) for row in rows)
    if incomplete:
        lines.append(f'Checks are incomplete for {incomplete} of {total} files'
                     + (' (including flagged files).' if any(checks_incomplete(row) for row in flagged) else '.'))
        tools = sorted({name for row in rows for name in unfinished_tools(row)})
        if tools:
            lines.append('Checks not run or not completed: ' + ', '.join(tools) + '.')
    else:
        lines.append('All applicable audit checks completed.')
    if not any(row['clamav'] == 'DETECTION' or row['clamav'].startswith('No detection') for row in rows):
        lines.append('No ClamAV malware scan completed for any file.')
    lines.append(f'Next: open {report_folder}/intake_audit.csv for filenames, review reasons and checks not run.')
    if flagged:
        lines.append(f'Keep flagged files in {folder} until you have reviewed them.')
    lines.append('Your incoming files were left unchanged. A result without warnings is not proof of safety.')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Audit incoming or existing archive files without altering them.')
    parser.add_argument('--folder', help='USB-relative folder; defaults to the sole Incoming/ or Incoming Files/ folder')
    parser.add_argument('--no-clamav', action='store_true', help='Do not invoke ClamAV even if installed')
    parser.add_argument('--pdfid', help='Path to an externally installed pdfid.py or executable')
    parser.add_argument('--epubcheck-jar', help='Path to an externally downloaded epubcheck.jar')
    parser.add_argument('--progress-file', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.folder is None:
        candidates = [name for name in ('Incoming', 'Incoming Files') if (ROOT / name).is_dir()]
        if len(candidates) != 1:
            parser.error('Choose an intake folder with --folder NAME; expected exactly one of Incoming/ or Incoming Files/')
        args.folder = candidates[0]
    target = (ROOT / args.folder).resolve()
    try:
        target.relative_to(ROOT)
    except ValueError:
        parser.error('--folder must be a folder within the same USB root')
    if not target.is_dir():
        parser.error('Folder not found: ' + str(target))
    clam = None if args.no_clamav else shutil.which('clamscan')
    pdfid = args.pdfid or shutil.which('pdfid.py') or shutil.which('pdfid')
    epubcheck = None if args.epubcheck_jar else shutil.which('epubcheck')
    if args.pdfid and not Path(args.pdfid).is_file() and not shutil.which(args.pdfid):
        parser.error('PDFiD executable or script not found')
    if args.epubcheck_jar and not Path(args.epubcheck_jar).is_file():
        parser.error('EPUBCheck JAR not found')

    paths = sorted((p for p in target.rglob('*') if p.is_file() and not p.is_symlink()
                    and not any(part.startswith('.') or part == '__pycache__' for part in p.relative_to(target).parts)
                    and p.name not in REPORTS and p.name != 'metadata_corrections.csv'
                    and p.relative_to(ROOT).parts[0] not in {'Library Tools', 'tests'}
                    and not (target == ROOT and p.parent == ROOT and p.suffix.casefold() in {'.py', '.bat', '.command', '.sh', '.md', '.html', '.json', '.txt', '.csv'})),
                   key=lambda p: str(p).casefold())
    progress = AuditProgress(args.progress_file)
    rows: list[dict] = []
    coverage = {name: {'attempted': 0, 'errors': 0, 'unavailable': 0}
                for name in ('ClamAV', 'PDFiD', 'EPUBCheck')}
    signature_checks = 0
    fingerprints = 0

    def record_check(name: str, result: str, available: bool) -> None:
        counts = coverage[name]
        counts['attempted' if available else 'unavailable'] += 1
        if result == 'ERROR':
            counts['errors'] += 1

    print(f'Auditing {len(paths)} file(s) in {args.folder} (no files will be altered).', flush=True)
    print('Built-in checks: short file signatures, empty files and extension mismatches; '
          'SHA-256 fingerprints identify bytes, not malware.', flush=True)
    print('ClamAV: malware-signature scan; ' + ('available.' if clam else
          'NOT RUN (' + ('disabled for this run' if args.no_clamav else 'not installed') + ').'), flush=True)
    print('PDFiD: PDF active/embedded feature indicators; ' +
          ('available.' if pdfid else 'NOT RUN (not configured).'), flush=True)
    print('EPUBCheck: EPUB conformance validation, not a malware scan; ' +
          ('available.' if epubcheck or args.epubcheck_jar else 'NOT RUN (not configured).'), flush=True)
    print('PDF/EPUB parser checks are skipped after a file-signature warning or ClamAV detection/error.', flush=True)
    progress.emit(1, 'loading' if clam and paths else 'skipped', 0, len(paths))
    stamps = {}
    if clam and paths:
        for path in paths:
            try:
                stamps[path] = file_stamp(path)
            except (OSError, ValueError):
                stamps[path] = None
        print(f'ClamAV: loading the database once and scanning {len(paths)} files. '
              'Stop Current Action remains available.', flush=True)
    started = time.monotonic()
    completed = set()

    def antivirus_progress(kind, path):
        if kind == 'done':
            completed.add(path)
        progress.emit(1, 'running', len(completed), len(paths), path.relative_to(ROOT).as_posix())

    clam_results = check_clamav_batch(paths, clam,
                                    **({'progress': antivirus_progress} if progress.stream is not None else {}))
    progress.emit(1, 'complete' if clam and paths else 'skipped', len(paths) if clam else 0, len(paths))
    if clam and paths:
        print(f'ClamAV batch finished in {time.monotonic() - started:.1f}s. '
              'Checking file details and applicable PDF/EPUB structure next.', flush=True)
    progress.emit(2, 'running', 0, len(paths))
    for index, path in enumerate(paths, 1):
        rel = path.relative_to(ROOT).as_posix()
        progress.emit(2, 'running', index - 1, len(paths), rel)
        problems: list[str] = []
        missing: list[str] = []
        clam_result, issue = clam_results[path]
        if issue:
            problems.append(issue)
        record_check('ClamAV', clam_result, bool(clam))
        pdfid_result = epub_result = 'NOT RUN'
        try:
            current_stamp = file_stamp(path)
            if clam and current_stamp != stamps[path]:
                problems.append('File changed during the antivirus scan; run the audit again')
                if clam_result != 'DETECTION':
                    if clam_result != 'ERROR':
                        coverage['ClamAV']['errors'] += 1
                    clam_result = 'ERROR'
            kind, issue_from_type = check_type(path)
            signature_checks += 1
            if issue_from_type:
                problems.append(issue_from_type)
            if not clam:
                missing.append('ClamAV')
            # Never feed files with a signature mismatch or AV detection/error
            # to additional PDF/EPUB parsers in this unsandboxed audit.
            prior_warning = bool(issue_from_type) or clam_result in {'DETECTION', 'ERROR'}
            pdfid_result = 'N/A'
            epub_result = 'N/A'
            if path.suffix.casefold() == '.pdf':
                if prior_warning:
                    pdfid_result = 'SKIPPED (prior warning)'
                else:
                    pdfid_result, issue = check_pdfid(path, pdfid)
                    record_check('PDFiD', pdfid_result, bool(pdfid))
                    if issue:
                        problems.append(issue)
                if not pdfid or prior_warning:
                    missing.append('PDFiD' + (' skipped' if prior_warning else ''))
            elif path.suffix.casefold() == '.epub':
                if prior_warning:
                    epub_result = 'SKIPPED (prior warning)'
                else:
                    epub_result, issue = check_epubcheck(path, epubcheck, args.epubcheck_jar)
                    record_check('EPUBCheck', epub_result, bool(epubcheck or args.epubcheck_jar))
                    if issue:
                        problems.append(issue)
                if (not epubcheck and not args.epubcheck_jar) or prior_warning:
                    missing.append('EPUBCheck' + (' skipped' if prior_warning else ''))
            digest = sha256_file(path)
            fingerprints += 1
            size = path.stat().st_size
            if file_stamp(path) != current_stamp:
                problems.append('File changed while checking details; run the audit again')
                if clam and clam_result != 'DETECTION':
                    if clam_result != 'ERROR':
                        coverage['ClamAV']['errors'] += 1
                    clam_result = 'ERROR'
        except (OSError, ValueError) as exc:
            kind, digest, size = 'ERROR', '', 0
            problems.append(f'Could not read file: {type(exc).__name__}')
        state = 'REVIEW' if problems else ('LIMITED CHECKS' if missing else 'No issues detected by these checks')
        rows.append({'relative_path': rel, 'size_bytes': size, 'sha256': digest,
                     'file_type_check': kind, 'clamav': clam_result, 'pdfid': pdfid_result,
                     'epubcheck': epub_result, 'status': state,
                     'review_reasons': ' | '.join(problems), 'checks_not_run': ' | '.join(missing)})
        print(f'  [{state}] {rel}', flush=True)
        progress.emit(2, 'running', index, len(paths), rel)
    progress.emit(2, 'complete', len(paths), len(paths))
    when = datetime.now().astimezone().isoformat(timespec='seconds')
    progress.emit(3, 'running', 0, 2)
    LAYOUT.prepare_reports()
    write_report(LAYOUT.reports / 'intake_audit.csv', generate_csv(rows), 'utf-8-sig')
    progress.emit(3, 'running', 1, 2)
    write_report(LAYOUT.reports / 'intake_audit.json', json.dumps({
        'scanned_folder': target.relative_to(ROOT).as_posix(), 'scanned_at': when,
        'notice': 'No detection or clean validation is a guarantee of safety. The results are a snapshot.',
        'tools': {'clamav': bool(clam), 'pdfid': bool(pdfid), 'epubcheck': bool(epubcheck or args.epubcheck_jar)},
        'files': rows,
    }, ensure_ascii=False, indent=2))
    print(f'Completed: {len(rows)} files; {sum(r["status"] == "REVIEW" for r in rows)} flagged for review; '
          f'{sum(checks_incomplete(r) for r in rows)} with incomplete checks (including flagged files).')
    print(f'Built-in coverage: {signature_checks}/{len(paths)} signature/empty-file checks; '
          f'{fingerprints}/{len(paths)} SHA-256 fingerprints completed.')
    eligible = {'ClamAV': len(paths),
                'PDFiD': sum(path.suffix.casefold() == '.pdf' for path in paths),
                'EPUBCheck': sum(path.suffix.casefold() == '.epub' for path in paths)}
    for name, counts in coverage.items():
        skipped = eligible[name] - counts['attempted'] - counts['unavailable']
        print(f'{name}: {counts["attempted"]} attempted (errors: {counts["errors"]}); '
              f'{counts["unavailable"]} unavailable; {skipped} skipped of {eligible[name]} applicable files.')
    print('Unavailable or skipped checks are incomplete. A scan without detections is not a guarantee of safety.')
    print('Reports: ' + str(LAYOUT.reports.relative_to(ROOT)) + '/intake_audit.csv or intake_audit.json. Nothing was automatically moved, deleted or trusted.')
    print(format_audit_summary(rows, args.folder, LAYOUT.reports.relative_to(ROOT).as_posix()), flush=True)
    progress.emit(3, 'complete', 2, 2)
    progress.close()
    return 0


if __name__ == '__main__':
    from library_paths import configure_console
    configure_console()
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(1)
