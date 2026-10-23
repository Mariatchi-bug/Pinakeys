#!/usr/bin/env python3
"""Portable, offline inventory for a removable drive.

Use the supplied organized kit, or keep flat-kit scripts at the common parent.
Source materials are never changed. Incoming folders require explicit selection
and inventory-only mode; root catalogues exclude them by default.

Usage:
    python3 scan_library.py --inventory-only
    python3 scan_library.py --inventory-only --folder 'Research/Books'
    python3 scan_library.py --inventory-only --verify
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import hashlib
import html
import json
import os
from pathlib import Path
import posixpath
import re
import sys
import signal
import time
import tempfile
from urllib.parse import quote
import xml.etree.ElementTree as ET
import zipfile
from library_paths import library_paths
from library_style import BASE_CSS

try:
    import pymupdf
except ImportError:
    pymupdf = None

DC = '{http://purl.org/dc/elements/1.1/}'
EPUB_CONTAINER = '{urn:oasis:names:tc:opendocument:xmlns:container}'
EPUB_OPF = '{http://www.idpf.org/2007/opf}'
CORRECTION_FIELDS = ['relative_path', 'title', 'author', 'date', 'identifier']
COLUMNS = [
    'relative_path', 'folder', 'filename', 'format', 'size_bytes',
    'modified', 'title', 'author', 'date', 'identifier', 'page_count',
    'embedded_title', 'embedded_author', 'embedded_date', 'embedded_identifier',
    'review_flags', 'read_status',
    'record_id', 'sha256', 'verification',
]
ROOT_IGNORED = {
    'scan_library.py', 'audit_materials.py', 'sanitize_with_dangerzone.py', 'pdf_followup.py', 'pdf_inspection.py', 'README.md',
    'Run on Mac.command', 'Run on Linux.sh', 'Run on Windows.bat',
    'Run safety audit (Mac).command', 'Run safety audit (Linux).sh',
    'Run safety audit (Windows).bat',
    'Install PDF metadata support (Mac-Linux).sh',
    'Install PDF metadata support (Windows).bat',
    'library_index.html', 'library_mobile.html', 'catalogue.txt',
    'catalogue.csv', 'catalogue.json', 'metadata_review.csv',
    'metadata_corrections.csv', 'intake_audit.csv', 'intake_audit.json', 'deeper_pdf_check.html',
    'LICENSE', 'CHANGELOG.md', 'CONTRIBUTING.md',
    'library_paths.py', 'library_style.py', 'build_usb.py', 'pinakeys.py', 'review_library.py', 'Open Library.html',
    'library_records.py', 'intake_workflow.py', 'intake_ui.py', 'library_records.csv', 'library_records.json',
}
INTAKE_NAMES = {'incoming', 'incoming files'}
RUN_STATUS = None

RETAINED_METADATA = 'Metadata retained from previous scan (not re-inspected)'
BAD_TITLE = re.compile(
    r'^(untitled(?: document)?|new document|document\s*\d*|scan\s*[-_ ]?\d*|unknown|none)$'
    r'|microsoft word|adobe acrobat|libreoffice|\.docx?$', re.I,
)


def extract_epub(path: Path) -> dict:
    """Read EPUB's package metadata; do not infer a title from the filename."""
    with zipfile.ZipFile(path) as package_zip:
        def read_limited(member_name: str) -> bytes:
            # Keep malformed/hostile EPUB members from exhausting memory.
            info = package_zip.getinfo(member_name)
            if info.file_size > 4 * 1024 * 1024:
                raise ValueError('EPUB metadata XML entry is unexpectedly large')
            with package_zip.open(info) as stream:
                content = stream.read(4 * 1024 * 1024 + 1)
            if len(content) > 4 * 1024 * 1024:
                raise ValueError('EPUB metadata XML entry exceeded size limit')
            return content

        container = ET.fromstring(read_limited('META-INF/container.xml'))
        rootfile = container.find('.//' + EPUB_CONTAINER + 'rootfile')
        if rootfile is None or not rootfile.get('full-path'):
            raise ValueError('EPUB package manifest not found')
        opf_path = posixpath.normpath(rootfile.attrib['full-path'])
        opf = ET.fromstring(read_limited(opf_path))
        metadata = opf.find(EPUB_OPF + 'metadata')
        if metadata is None:
            metadata = next((x for x in opf if x.tag.rsplit('}', 1)[-1] == 'metadata'), None)
        if metadata is None:
            raise ValueError('EPUB metadata section not found')

        def values(kind):
            return [x.text.strip() for x in metadata.findall('.//' + DC + kind)
                    if x.text and x.text.strip()]

        return {
            'title': ' ; '.join(values('title')),
            'author': ' ; '.join(values('creator')),
            'date': ' ; '.join(values('date')),
            'identifier': ' ; '.join(values('identifier')),
            'page_count': '',
        }


def extract_pdf(path: Path) -> dict:
    if pymupdf is None:
        raise RuntimeError('PDF metadata unavailable: PyMuPDF is not installed on this device')
    with pymupdf.open(str(path)) as pdf:
        if pdf.needs_pass:
            raise RuntimeError('Password-protected PDF')
        metadata = pdf.metadata or {}

        def field(name):
            val = (metadata.get(name) or '').strip()
            return '' if val.casefold() in {'none', 'null', 'unknown', 'n/a'} else val

        # A PDF creation date is not the publication date, so do not repurpose it.
        return {'title': field('title'), 'author': field('author'),
                'date': '', 'identifier': '', 'page_count': pdf.page_count}


def load_corrections(path: Path) -> dict[str, dict]:
    if not path.exists():
        # Only create this once. It is never rewritten on later scans.
        with path.open('x', encoding='utf-8-sig', newline='') as fh:
            csv.writer(fh).writerow(CORRECTION_FIELDS)
    with path.open('r', encoding='utf-8-sig', newline='') as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or 'relative_path' not in reader.fieldnames:
            raise ValueError('metadata_corrections.csv needs a relative_path column')
        return {row['relative_path'].strip().replace('\\', '/'): row
                for row in reader if (row.get('relative_path') or '').strip()}


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def previous_metadata(path: Path) -> dict:
    """Load successful extractions for inventory refreshes, never safety verdicts.

    Size and modification time are a convenience check, not proof of unchanged
    bytes. A deliberate metadata extraction always reads the document again.
    Refuse an unreadable snapshot before overwriting it.
    """
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError('Previous catalogue is unreadable; preserve it before rebuilding')
    return {row['relative_path']: row for row in data
            if isinstance(row.get('relative_path'), str)
            and row.get('read_status') in {'OK', RETAINED_METADATA}}


def restore_metadata(row: dict, previous: dict) -> bool:
    matched_bytes = bool(row.get('sha256') and row.get('sha256') == previous.get('sha256'))
    if not previous or (not matched_bytes and any(row[key] != previous.get(key)
                           for key in ('size_bytes', 'modified', 'format'))):
        return False
    if previous.get('sha256') and row.get('sha256') != previous['sha256']:
        return False
    for name in ('title', 'author', 'date', 'identifier'):
        # Never turn an old manual correction into extracted metadata. Older
        # catalogues did not preserve embedded identifiers separately.
        row[name] = previous.get('embedded_' + name, '')
        row['embedded_' + name] = row[name]
    row['page_count'] = previous.get('page_count', '')
    row['read_status'] = RETAINED_METADATA
    return True


def write_atomically(path: Path, data: str, encoding='utf-8'):
    """Stage generated reports on the same drive before replacing originals."""
    filename = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding=encoding, newline='',
                                         dir=path.parent, prefix='.usb-index-',
                                         suffix='.tmp', delete=False) as fh:
            filename = fh.name
            fh.write(data)
        os.replace(filename, path)
    finally:
        if filename and os.path.exists(filename):
            os.unlink(filename)


def write_csv(path: Path, rows: list[dict], columns: list[str]):
    # Avoid formula execution when opening exported CSV in Excel / Numbers.
    from io import StringIO
    output = StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        def safe(field):
            val = '' if row.get(field) is None else str(row.get(field))
            return "'" + val if val.lstrip().startswith(('=', '+', '-', '@')) else val
        writer.writerow({field: safe(field) for field in columns})
    write_atomically(path, output.getvalue(), 'utf-8-sig')


def e(value) -> str:
    return html.escape(str(value if value is not None else ''), quote=True)


def file_url(relative_path: str) -> str:
    # Relative links keep working when the USB's mount point or drive letter changes.
    return quote(relative_path, safe='/')


def html_prelude(title: str, summary: str, extra_css: str = '', header_link: str = '') -> str:
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} · Pinakeys</title>
<style>
{BASE_CSS}
{extra_css}
</style></head><body>
<header class="site-header"><span class="brand">Pinakeys</span>{header_link}</header>
<main>
<h1>{e(title)}</h1><p class="lede">{e(summary)}</p>
'''


def stats_block(rows: list[dict], interactive_review=False) -> str:
    n_pdf = sum(r['format'] == 'PDF' for r in rows)
    n_epub = sum(r['format'] == 'EPUB' for r in rows)
    flagged = sum(bool(r['review_flags']) for r in rows)
    stats = [(len(rows), 'Files'), (n_pdf, 'PDFs'), (n_epub, 'EPUBs')]
    review = (f'<strong>{flagged:,}</strong><small>Need review</small>')
    review = (f'<button class="stat stat-review" id="review-shortcut" type="button" '
              f'aria-pressed="false" aria-controls="files">{review}</button>' if interactive_review
              else f'<div class="stat stat-review">{review}</div>')
    return '<div class="stats" aria-label="Catalogue summary">' + ''.join(
        f'<div class="stat"><strong>{number:,}</strong><small>{e(label)}</small></div>' for number, label in stats
    ) + review + '</div>'


def create_desktop_html(rows: list[dict], scanned_folder: str, when: str, organized=False) -> str:
    report_link = 'Library%20Tools/Reports/' if organized else ''
    tools_link = ('<a class="header-link" href="Library%20Tools/Start%20Here.html">Tools &amp; help</a>'
                  if organized else '')
    corrections_label = ('Library Tools/Corrections/metadata_corrections.csv'
                         if organized else 'metadata_corrections.csv')
    css = '''
.toolbar {display:flex;align-items:center;flex-wrap:wrap;gap:16px;margin:0 0 22px}
input[type=search] {font:inherit;font-size:16px;flex:1;min-width:200px;padding:13px 16px;
  border-radius:5px;border:1px solid #b9c0b8;background:var(--surface);color:var(--text)}
input[type=search]::placeholder {color:var(--muted);opacity:1}
input[type=checkbox] {accent-color:var(--accent);width:17px;height:17px;margin:0}
.check {display:flex;align-items:center;gap:9px;font-size:15px;font-weight:550;white-space:nowrap}
#shown {font-size:15px;font-variant-numeric:tabular-nums;white-space:nowrap}
.tablewrap {border:1px solid var(--rule);border-radius:6px;overflow:hidden;background:var(--surface)}
table {border-collapse:collapse;table-layout:fixed;width:100%;font-size:16px}
.document-col {width:39%}.author-col{width:14%}.format-col{width:8%}.size-col{width:9%}.status-col{width:30%}
th,td {padding:16px 18px;text-align:left;vertical-align:middle;border-bottom:1px solid var(--rule);overflow-wrap:anywhere}
th {font-weight:600;font-size:15px;color:var(--muted);background:#f9f9f6;padding-top:14px;padding-bottom:14px}
th+th {border-left:1px solid var(--rule)}
tr:last-child td {border-bottom:0}
tr[data-review="1"] {background:var(--review-surface)}
tbody tr:hover {background:var(--wash)}
td small {display:block;color:var(--muted);font-size:14px;line-height:1.45;margin-top:4px;overflow-wrap:anywhere}
td a {font-size:18px;font-weight:600;color:var(--ink);text-decoration:none;line-height:1.45}
td a:hover {text-decoration:underline}
td:nth-child(5) {font-size:14px;line-height:1.5}
td:nth-child(3) {font-size:14px;color:var(--muted)}
td:nth-child(4) {font-size:14px;font-variant-numeric:tabular-nums}
[hidden],tr[hidden] {display:none!important}
.empty {padding:28px 18px;margin:0;text-align:center;color:var(--muted);border-top:1px solid var(--rule)}
.snapshot-details {margin-top:10px}
.snapshot-details summary {cursor:pointer;width:fit-content;color:var(--accent)}
.snapshot-details p {max-width:760px;margin:10px 0 0}
@media(max-width:700px) {
  .toolbar {gap:14px}
  input[type=search] {flex-basis:100%;min-width:0;width:100%}
  #shown {margin-left:auto}
  .tablewrap {border:0;border-radius:0;overflow:visible;background:transparent}
  table,tbody,tr,td {display:block;width:100%}
  thead,colgroup {display:none}
  tbody tr {border:1px solid var(--rule);border-radius:6px;background:var(--surface);padding:16px;margin-bottom:12px}
  td {padding:0;border:0}
  td:first-child {margin-bottom:10px}
  td:nth-child(2) {color:var(--muted);font-size:13px}
  td:nth-child(3),td:nth-child(4) {display:inline-block;width:auto;margin:10px 12px 10px 0}
  td:nth-child(5) {border-top:1px solid var(--rule);padding-top:10px}
}
'''
    parts = [html_prelude('Your library', f'{scanned_folder} · Updated {when}', css, tools_link),
             stats_block(rows, interactive_review=True),
             f'<nav class="view-links" aria-label="Other catalogue formats"><a href="{report_link}library_mobile.html">Simple view</a> '
             f'<a href="{report_link}catalogue.txt">Text list</a></nav>',
             '<div class="toolbar"><input type="search" id="search" aria-label="Search" '
             'placeholder="Search title, filename, author, folder…">'
             '<label class="check"><input type="checkbox" id="review" aria-controls="files">Needs review only</label>'
             '<span class="muted" id="shown" role="status" aria-live="polite"></span></div>',
             '<div class="tablewrap"><table aria-label="Materials"><colgroup><col class="document-col">'
             '<col class="author-col"><col class="format-col"><col class="size-col"><col class="status-col">'
             '</colgroup><thead><tr><th scope="col">Document</th><th scope="col">Author</th><th scope="col">Format</th>'
             '<th scope="col">Size</th><th scope="col">Review &amp; status</th></tr></thead><tbody id="files">']
    for r in rows:
        flags = r['review_flags']
        searchable = ' '.join(str(r.get(x, '')) for x in ('title', 'author', 'filename', 'folder', 'format', 'review_flags'))
        title = r['title'] or r['filename']
        status = 'No metadata flags' if r['read_status'] == 'OK' else r['read_status']
        status_html = (f'<span class="warning">{e(flags)}</span>' if flags
                       else f'<span class="ok">{e(status)}</span>')
        parts.append(
            f'<tr data-search="{e(searchable.lower())}" data-review="{1 if flags else 0}">'
            f'<td><a href="{e(file_url(r["relative_path"]))}">{e(title)}</a>'
            f'<small>{e(r["filename"])} · {e(r["folder"])}</small></td>'
            f'<td>{e(r["author"] or "—")}</td><td>{e(r["format"])}</td>'
            f'<td>{r["size_bytes"] / (1024*1024):.1f} MB</td>'
            f'<td>{status_html}</td></tr>'
        )
    parts.extend([
        '</tbody></table><p id="empty" class="empty" hidden>No matching files. Try another search or turn off the review filter.</p></div></main>',
        '<footer>An offline snapshot. Refresh Pinakeys after adding or removing files. Originals stay unchanged.'
        '<details class="snapshot-details"><summary>About this catalogue &amp; corrections</summary><p>'
        f'To change displayed metadata without changing originals, edit {corrections_label}. '
        'The search includes filenames, authors, folders and review reasons. File links can be limited by some iPhone preview apps.'
        '</p></details></footer>',
        '''<script>
(function () {
  const search = document.getElementById('search');
  const review = document.getElementById('review');
  const shown = document.getElementById('shown');
  const shortcut = document.getElementById('review-shortcut');
  const empty = document.getElementById('empty');
  const rows = Array.from(document.querySelectorAll('#files tr'));
  function refresh() {
    const term = search.value.trim().toLowerCase();
    let count = 0;
    for (const row of rows) {
      const visible = (!review.checked || row.dataset.review === '1') &&
                       row.dataset.search.includes(term);
      row.hidden = !visible;
      if (visible) count++;
    }
    shown.textContent = count + ' shown';
    shortcut.setAttribute('aria-pressed', String(review.checked));
    empty.hidden = count !== 0;
  }
  search.addEventListener('input', refresh);
  review.addEventListener('change', refresh);
  shortcut.addEventListener('click', function () {
    review.checked = !review.checked;
    refresh();
  });
  refresh();
})();
</script></body></html>'''
    ])
    return '\n'.join(parts)


def create_mobile_html(rows: list[dict], scanned_folder: str, when: str, organized=False) -> str:
    catalogue_link = '../../Open%20Library.html' if organized else 'library_index.html'
    file_prefix = '../../' if organized else ''
    """Pre-rendered catalogue. No JS, network, images or external assets."""
    css = '''
.folder {margin:28px 0 0;padding:15px 0 10px;border-top:1px solid var(--rule);font-size:16px;overflow-wrap:anywhere}
.entry {padding:15px 0;border-bottom:1px solid var(--rule)}
.entry a {display:block;font-size:16px;font-weight:600;color:var(--ink);overflow-wrap:anywhere}
.entry small {display:block;color:var(--muted);font-size:12px;margin-top:4px;overflow-wrap:anywhere}
.entry .flag {font-size:13px;color:var(--review);margin-top:6px}
'''
    folder_groups = defaultdict(list)
    for r in rows:
        folder_groups[r['folder']].append(r)
    parts = [html_prelude('Simple library view',
                          f'{scanned_folder} · Updated {when} · No JavaScript required', css,
                          f'<a class="header-link" href="{catalogue_link}">Your library</a>'),
             stats_block(rows),
             '<p>This is a static copy of the inventory for phone and restricted offline viewers. '
             f'<a href="{catalogue_link}">Interactive desktop view</a> · '
             '<a href="catalogue.txt">Plain-text fallback</a></p>']
    for folder, entries in sorted(folder_groups.items(), key=lambda pair: pair[0].casefold()):
        parts.append(f'<h2 class="folder">{e(folder)} <span class="pill">{len(entries)}</span></h2>')
        for r in entries:
            title = r['title'] or r['filename']
            parts.append('<div class="entry">'
                         f'<a href="{e(file_url(file_prefix + r["relative_path"]))}">{e(title)}</a>'
                         f'<small>{e(r["author"] or "Unknown author")} · {e(r["format"])} · '
                         f'{e(r["filename"])}</small>'
                         + (f'<div class="flag">{e(r["review_flags"])}</div>' if r['review_flags']
                            else f'<small>{e(r["read_status"])}</small>')
                         + '</div>')
    parts.append('</main><footer>The catalogue is an offline snapshot, not a live USB monitor. '
                 'Some mobile preview apps may block links to local files; the title and path '
                 'remain available in catalogue.txt and catalogue.csv.</footer></body></html>')
    return '\n'.join(parts)


def create_text_index(rows: list[dict], scanned_folder: str, when: str) -> str:
    text = [f'PORTABLE MATERIALS INDEX — {scanned_folder}',
            f'Updated: {when}', f'Files: {len(rows)}',
            'This is a static offline list. Re-run the script after changing files.', '']
    for row in rows:
        title = row['title'] or row['filename']
        text.append(title)
        if row['author']:
            text.append('  Author: ' + row['author'])
        text.append('  File: ' + row['relative_path'])
        if row['review_flags']:
            text.append('  Review: ' + row['review_flags'])
        if row['read_status'] != 'OK':
            text.append('  Inspection: ' + row['read_status'])
        text.append('')
    return '\n'.join(text)


def main() -> int:
    layout = library_paths(__file__)
    usb_root = layout.root
    parser = argparse.ArgumentParser(description='Create a portable, offline inventory of materials (with PDF/EPUB metadata).')
    parser.add_argument('--version', action='version', version='Pinakeys 1.0.2')
    parser.add_argument('--folder', action='append', help='Repeat a folder relative to the kit root; otherwise reuse saved selection, the sole Books/Content folder, or the kit root')
    parser.add_argument('--skip-duplicates', action='store_true', help='Skip duplicate grouping; does not disable explicit --verify fingerprints')
    parser.add_argument('--inventory-only', action='store_true', help='Quick path/attribute inventory without document parsers; add --verify for full fingerprints')
    parser.add_argument('--exclude', action='append', default=None, help='Relative folder or glob to exclude; persisted with selection')
    parser.add_argument('--verify', action='store_true', help='Read full fingerprints in the selection; reconnect moved copies and detect changed bytes')
    parser.add_argument('--progress-file', help=argparse.SUPPRESS)
    parser.add_argument('--include-intake', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--records-only', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    from catalogue_settings import Selection
    from library_records import Records, proof, atomic, encoded
    from intake_workflow import Intake
    from operation_progress import OperationProgress as AuditProgress
    selection = Selection(layout)
    if args.folder or args.exclude is not None:
        selection.save(args.folder or selection.data['folders'], args.exclude if args.exclude is not None else selection.data['exclude'])
    selected = ', '.join(selection.data['folders'])
    if not args.inventory_only and any(any(part.casefold() in INTAKE_NAMES for part in Path(f).parts) for f in selection.data['folders']):
        parser.error('Use --inventory-only to catalogue incoming folders; document inspection is a separate explicit workflow')
    paths = selection.files(intake=args.include_intake)
    if not selection.path.exists():
        selection.save(selection.data['folders'],selection.data['exclude'])
    layout.prepare_reports()
    global RUN_STATUS
    RUN_STATUS=layout.reports/'catalogue_run.json'
    atomic(RUN_STATUS,encoded({'state':'running','operation':'full verification' if args.verify else 'catalogue update'}))
    progress = AuditProgress(args.progress_file)
    progress.emit(1,'complete',len(paths),len(paths))
    def interrupted(signum, frame):
        raise KeyboardInterrupt('Cancelled')
    signal.signal(signal.SIGTERM, interrupted)
    prior_snapshot = previous_metadata(layout.reports / 'catalogue.json')
    previous = prior_snapshot if args.inventory_only else {}
    records = Records(layout)
    corrections = load_corrections(layout.corrections)
    # New empty kits enable IDs lazily. Existing legacy data still requires explicit migration.
    use_records = records.exists or args.verify or (not corrections and args.inventory_only)
    if use_records:
        if Intake(layout).pending():
            raise ValueError('Recover interrupted filing before updating the catalogue.')
        with records.lock():
            if args.verify:
                import library_records
                seen=[]; started=time.monotonic(); last=[0.0]; read=[0]
                def report(path, count):
                    read[0]+=count
                    if time.monotonic()-last[0]>=0.15:
                        progress.emit(2,'running',len(seen),len(paths),str(path.relative_to(usb_root))+' · '+str(read[0]//1048576)+' MiB read')
                        last[0]=time.monotonic()
                library_records.PROGRESS=report
                try:
                    for path in paths:
                        seen.append({'relative_path':path.relative_to(usb_root).as_posix(),**proof(path)})
                        progress.emit(2,'running',len(seen),len(paths),path.relative_to(usb_root).as_posix())
                    records.sync(seen,scope=lambda p:selection.contains(p,intake=args.include_intake))
                    records.import_legacy()
                    records.import_private_choices()
                    records.ingest_audit()
                finally:
                    library_records.PROGRESS=None
                print('Full verification read all selected bytes; saved audits remain historical results, not a safety certificate.',flush=True)
            else:
                records.quick_update(paths,lambda p:selection.contains(p,intake=args.include_intake))
                print('Quick update: filenames and attributes only; fingerprints and security results were not reverified.',flush=True)
            records.save()
            records.export()
    else:
        records=None
    if args.records_only:
        atomic(RUN_STATUS,encoded({'state':'complete','operation':'record migration','records':len(records.data['records'])}))
        progress.emit(2,'complete',len(paths),len(paths))
        progress.emit(3,'complete',1,1)
        progress.close()
        print('Portable records saved; original documents and source correction/choice files are unchanged.',flush=True)
        return 0
    previous_by_id = {r.get('record_id'):r for r in previous.values() if r.get('record_id')}
    print(f'Inventorying {len(paths)} files under {selected} ...', flush=True)
    if args.inventory_only:
        print('Inventory-only mode: documents will not be parsed for metadata.')
    rows = []
    for index, path in enumerate(paths):
        if not args.verify:
            progress.emit(2,'running',index,len(paths),path.relative_to(usb_root).as_posix())
        relative_path = path.relative_to(usb_root).as_posix()
        stat = path.stat()
        row = {
            'relative_path': relative_path,
            'folder': path.parent.relative_to(usb_root).as_posix(),
            'filename': path.name, 'format': path.suffix[1:].upper() or 'OTHER',
            'size_bytes': stat.st_size,
            'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds'),
            'title': '', 'author': '', 'date': '', 'identifier': '', 'page_count': '',
            'embedded_title': '', 'embedded_author': '', 'embedded_date': '', 'embedded_identifier': '',
            'review_flags': '', 'read_status': 'OK',
            'record_id': '', 'sha256': '',
        }
        record = records.by_path(relative_path) if records else None
        if record:
            row.update(record_id=record['id'], sha256=record['sha256'])
        flags = []
        if stat.st_size == 0:
            flags.append('Empty file (0 bytes)')
            row['read_status'] = 'Not inspected (empty file)'
        elif args.inventory_only and row['format'] in {'PDF', 'EPUB'}:
            if not restore_metadata(row, previous_by_id.get(row['record_id']) or previous.get(relative_path, {})):
                row['read_status'] = 'Not inspected (inventory-only mode)'
        elif row['format'] in {'PDF', 'EPUB'}:
            try:
                details = extract_pdf(path) if row['format'] == 'PDF' else extract_epub(path)
                for name in ('title', 'author', 'date', 'identifier', 'page_count'):
                    row[name] = details.get(name, '')
                for name in ('title', 'author', 'date', 'identifier'):
                    row['embedded_' + name] = row[name]
            except Exception as exc:
                row['read_status'] = str(exc)
                flags.append('Cannot inspect metadata: ' + str(exc))
        else:
            row['read_status'] = 'Metadata extraction not supported for this file type'

        manual = records.corrections(record) if record else corrections.get(relative_path, {})
        if record and record['state'] != 'present':
            flags.append('Copy identity needs review in Intake')
        if record and record.get('corrections') and not records.corrections(record):
            flags.append('Earlier corrections held after content change; review in Intake')
        for name in ('title', 'author', 'date', 'identifier'):
            replacement = (manual.get(name) or '').strip()
            if replacement:
                row[name] = replacement
        if row['format'] in {'PDF', 'EPUB'} and row['read_status'] in {'OK', RETAINED_METADATA}:
            if not row['title']:
                flags.append('Missing title')
            elif BAD_TITLE.search(row['title']):
                flags.append('Suspicious title')
            if not row['author']:
                flags.append('Missing author')
        row['verification'] = 'Fresh fingerprint' if args.verify else 'Not verified in this quick update'
        row['review_flags'] = ' | '.join(flags)
        rows.append(row)

    if not args.skip_duplicates:
        by_size=defaultdict(list)
        for row in rows: by_size[row['size_bytes']].append(row)
        for group in by_size.values():
            if len(group)<2:continue
            by_digest=defaultdict(list)
            for row in group:
                if args.verify and row.get('sha256'):
                    by_digest[row['sha256']].append(row)
                elif not args.inventory_only and not records:
                    by_digest[digest_file(usb_root/row['relative_path'])].append(row)
                else:
                    row['review_flags']=' | '.join(x for x in (row['review_flags'],'Possible duplicate (same size; bytes not verified)') if x)
            for duplicates in by_digest.values():
                if len(duplicates)>1:
                    for row in duplicates:
                        row['review_flags']=' | '.join(x for x in (row['review_flags'],'Exact duplicate file') if x)
    progress.emit(2,'complete',len(paths),len(paths))
    progress.emit(3,'running',0,1)
    review = [row for row in rows if row['review_flags']]
    when = datetime.now().strftime('%Y-%m-%d %H:%M')
    shown_folder = selected + (' · Full fingerprints checked (not a safety certificate)' if args.verify else ' · Quick update: contents not verified')
    outputs=[layout.reports/name for name in ('catalogue.csv','metadata_review.csv','catalogue.json','library_mobile.html','catalogue.txt')]+[layout.catalogue]
    prior_outputs={p:p.read_bytes() if p.exists() else None for p in outputs}
    try:
        write_csv(layout.reports / 'catalogue.csv', rows, COLUMNS)
        write_csv(layout.reports / 'metadata_review.csv', review, COLUMNS)
        write_atomically(layout.reports / 'catalogue.json', json.dumps(rows, ensure_ascii=False, indent=2))
        write_atomically(layout.catalogue, create_desktop_html(rows, shown_folder, when, layout.organized))
        write_atomically(layout.reports / 'library_mobile.html', create_mobile_html(rows, shown_folder, when, layout.organized))
        write_atomically(layout.reports / 'catalogue.txt', create_text_index(rows, shown_folder, when))
    except BaseException:
        for path,data in prior_outputs.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path,data)
        raise
    kinds = Counter(r['format'] for r in rows)
    print(f'Indexed {len(rows)} files: {kinds["PDF"]} PDF, {kinds["EPUB"]} EPUB, {len(rows)-kinds["PDF"]-kinds["EPUB"]} other; {len(review)} need review.')
    print('Open ' + layout.catalogue.name + ' to browse. Reports: ' + str(layout.reports.relative_to(usb_root)))
    if not args.inventory_only and pymupdf is None and kinds['PDF']:
        print('PDF metadata not inspected: install PyMuPDF once on this computer (see README).', file=sys.stderr)
    atomic(RUN_STATUS,encoded({'state':'complete','operation':'full verification' if args.verify else 'quick update','selected_folders':selection.data['folders'],'files':len(rows)}))
    progress.emit(3,'complete',1,1)
    progress.close()
    return 0


if __name__ == '__main__':
    from library_paths import configure_console
    configure_console()
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        if RUN_STATUS:
            from library_records import atomic,encoded
            atomic(RUN_STATUS,encoded({'state':'cancelled','complete':False}))
        print('Stopped. The verification did not complete; previous results must not be treated as fresh.', file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError) as error:
        if RUN_STATUS:
            from library_records import atomic,encoded
            atomic(RUN_STATUS,encoded({'state':'error','complete':False,'error':str(error)}))
        print(f'ERROR: {error}', file=sys.stderr)
        raise SystemExit(1)
