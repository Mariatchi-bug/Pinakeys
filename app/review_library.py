"""Read catalogue snapshots and explicitly save portable metadata corrections.

Opening the editor only reads reports. Saving changes the independent CSV;
the caller refreshes the catalogue using its existing inventory action.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from io import StringIO
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from queue import Empty, Queue
import stat
import sys
import tempfile
from threading import Thread, Event
import time
import unicodedata
import uuid

from library_style import PALETTE
from library_paths import LibraryPaths, file_identity, file_state

FIELDS = ('relative_path', 'title', 'author', 'date', 'identifier')
EDITABLE = ('title', 'author')


class ReviewError(ValueError):
    """An actionable review/save error; preserve the user's edits on failure."""


def path_key(value):
    return unicodedata.normalize('NFC', value.strip().replace('\\', '/'))


def search_key(value):
    return unicodedata.normalize('NFC', str(value or '')).casefold()


def file_fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class FileProof:
    relative_path: str
    fingerprint: tuple
    digest: str


@dataclass(frozen=True)
class TrashPlan:
    target: FileProof
    duplicates: tuple[FileProof, ...]
    flagged_duplicate: bool


class ReviewStore:
    def __init__(self, layout):
        self.layout = LibraryPaths(layout.root.resolve(), layout.organized)
        self.catalogue = self.layout.reports / 'catalogue.json'
        self.corrections = self.layout.corrections
        self._safe_report(self.catalogue)
        self._safe_report(self.corrections)
        try:
            self.catalogue_bytes = self.catalogue.read_bytes()
            self.rows = json.loads(self.catalogue_bytes.decode('utf-8-sig'))
        except FileNotFoundError as exc:
            raise ReviewError('Refresh the catalogue first, then open Review Files.') from exc
        except (OSError, ValueError) as exc:
            raise ReviewError('The catalogue could not be read. Keep it and refresh before reviewing.') from exc
        if not isinstance(self.rows, list) or any(not isinstance(row, dict) for row in self.rows):
            raise ReviewError('The catalogue has an invalid format. No corrections were changed.')
        self.by_path = {}
        for row in self.rows:
            relative = row.get('relative_path')
            if not isinstance(relative, str):
                raise ReviewError('A catalogue entry has no usable file path.')
            self.material_path(relative)
            identity = path_key(relative)
            if identity in self.by_path:
                raise ReviewError('The catalogue repeats a file path. Refresh before reviewing.')
            self.by_path[identity] = row
        self.correction_bytes = self._current_corrections()
        self.header, self.correction_rows = self._parse_corrections(self.correction_bytes)
        from library_records import Records
        self.record_store = Records(self.layout)
        self._load_overrides()

    def _safe_report(self, path):
        root = self.layout.root.resolve()
        if not path.resolve().is_relative_to(root):
            raise ReviewError('The report or corrections path leaves the library.')
        cursor = path
        while cursor != self.layout.root:
            if cursor.is_symlink():
                raise ReviewError('Report and correction paths must not be symlinks.')
            cursor = cursor.parent

    def material_path(self, relative):
        p = PurePosixPath(relative)
        if (not relative or not p.parts or p.is_absolute() or PureWindowsPath(relative).drive
                or p.as_posix() != relative or '\\' in relative
                or any(part in {'.', '..'} or part.startswith('.') for part in p.parts)
                or p.parts[0] == 'Library Tools'):
            raise ReviewError('A catalogue entry contains an invalid file path.')
        target = self.layout.root.joinpath(*p.parts)
        if not target.resolve().is_relative_to(self.layout.root.resolve()):
            raise ReviewError('A catalogue file path leaves the library.')
        cursor = target
        while cursor != self.layout.root:
            if cursor.is_symlink():
                raise ReviewError('A catalogue file path now uses a symlink. Refresh before reviewing.')
            cursor = cursor.parent
        return target

    def _current_corrections(self):
        self._safe_report(self.corrections)
        try:
            return self.corrections.read_bytes()
        except FileNotFoundError:
            return None

    @staticmethod
    def _parse_corrections(data):
        if data is None:
            return list(FIELDS), []
        try:
            rows = list(csv.reader(StringIO(data.decode('utf-8-sig'), newline=''), strict=True))
        except (UnicodeError, csv.Error) as exc:
            raise ReviewError('The corrections CSV could not be read. It has been left unchanged.') from exc
        if not rows or 'relative_path' not in rows[0] or len(set(rows[0])) != len(rows[0]):
            raise ReviewError('The corrections CSV needs one relative_path column and unique column names.')
        header, values = rows[0], rows[1:]
        position = header.index('relative_path')
        seen = set()
        for row in values:
            if len(row) > len(header):
                raise ReviewError('A corrections row has extra cells. Keep the CSV and inspect it before editing.')
            identity = path_key(row[position]) if len(row) > position else ''
            if identity and identity in seen:
                raise ReviewError('The corrections CSV repeats a file path. Inspect it before editing.')
            if identity:
                seen.add(identity)
        return header, values

    def _load_overrides(self):
        self.overrides = {}
        if self.record_store.exists:
            for record in self.record_store.data['records']:
                if record['state'] != 'missing':
                    self.overrides[path_key(record['path'])] = self.record_store.corrections(record)
            return
        for values in self.correction_rows:
            row = dict(zip(self.header, values))
            relative = row.get('relative_path', '')
            if relative.strip():
                self.overrides[path_key(relative)] = row

    def edits(self, relative):
        row = self.overrides.get(path_key(relative), {})
        return {field: (row.get(field) or '').strip() for field in EDITABLE}

    def prefill(self, relative):
        """Offer editable starting values without saving filename suggestions."""
        row = self.by_path[path_key(relative)]
        stem = PurePosixPath(row.get('filename') or row['relative_path']).stem
        title, separator, author = stem.rpartition(' — ')
        if not separator or not title.strip() or not author.strip():
            title, author = stem, ''
        filename = {field: ' '.join(value.replace('_', ' ').split())
                    for field, value in (('title', title), ('author', author))}
        edits = self.edits(relative)
        values, sources = {}, {}
        for field in EDITABLE:
            embedded = str(row.get('embedded_' + field) or '').strip()
            current = str(row.get(field) or '').strip()
            if edits[field]:
                values[field], sources[field] = edits[field], 'Saved correction'
            elif current:
                values[field] = current
                sources[field] = 'Embedded metadata' if current == embedded else 'Current catalogue'
            elif embedded:
                values[field], sources[field] = embedded, 'Embedded metadata'
            elif filename[field]:
                values[field], sources[field] = filename[field], 'Suggested from filename — check before saving'
            else:
                values[field], sources[field] = '', f'No {field} available — enter one if known'
        return values, sources

    def filtered(self, query='', review_only=True):
        needle = search_key(query).strip()
        return [row for row in self.rows
                if (not review_only or row.get('review_flags')) and
                (not needle or needle in search_key(' '.join(str(row.get(field) or '')
                    for field in ('title', 'author', 'filename', 'folder', 'relative_path', 'review_flags'))))]

    def _unchanged_catalogue(self):
        self._safe_report(self.catalogue)
        if self.catalogue.read_bytes() != self.catalogue_bytes:
            raise ReviewError('The catalogue changed. Close and reopen Review Files before removing a file.')

    def _file_proof(self, relative):
        row = self.by_path.get(path_key(relative))
        if row is None:
            raise ReviewError('This file is no longer in the loaded catalogue.')
        target = self.material_path(row['relative_path'])
        info = target.lstat()
        modified = datetime.fromtimestamp(info.st_mtime).isoformat(timespec='seconds')
        if (not stat.S_ISREG(info.st_mode) or info.st_size != row.get('size_bytes')
                or modified != row.get('modified')):
            raise ReviewError('The file changed since the scan. Refresh the catalogue before removing it.')
        fingerprint = file_fingerprint(info)
        digest = hashlib.sha256()
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        with os.fdopen(os.open(target, flags), 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if file_identity(opened) != file_identity(info):
                raise ReviewError('The file changed during checking. Refresh before removing it.')
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                from library_records import check_read_cancelled,READ_CONTEXT
                check_read_cancelled()
                digest.update(chunk)
                callback=getattr(READ_CONTEXT,'progress',None)
                if callback:callback(target,len(chunk))
            if file_state(os.fstat(stream.fileno())) != file_state(opened):
                raise ReviewError('The file changed during checking. Refresh before removing it.')
        if file_fingerprint(self.material_path(row['relative_path']).lstat()) != fingerprint:
            raise ReviewError('The file changed during checking. Refresh before removing it.')
        return FileProof(row['relative_path'], fingerprint, digest.hexdigest())

    def prepare_trash(self, relative):
        """Read bytes for a specific confirmation; do not move any file here."""
        self._unchanged_catalogue()
        target = self._file_proof(relative)
        row = self.by_path[path_key(relative)]
        flagged = 'Exact duplicate file' in str(row.get('review_flags') or '')
        copies = []
        hint = 'Possible duplicate' in str(row.get('review_flags') or '')
        if flagged or hint:
            for candidate in self.rows:
                other = candidate['relative_path']
                if path_key(other) == path_key(relative) or candidate.get('size_bytes') != row.get('size_bytes'):
                    continue
                try:
                    proof = self._file_proof(other)
                except (OSError, ValueError):
                    continue
                # A second spelling of the same directory entry is not a copy.
                if proof.fingerprint[:2] != target.fingerprint[:2] and proof.digest == target.digest:
                    copies.append(proof)
            if flagged and not copies:
                raise ReviewError('Another identical copy could not be verified. Refresh the catalogue before removing this flagged duplicate.')
        self._unchanged_catalogue()
        return TrashPlan(target, tuple(copies), flagged or bool(copies))

    def trash(self, plan, move_to_trash):
        """Recheck the confirmed file and retained copy before the native move."""
        self._unchanged_catalogue()
        if self._file_proof(plan.target.relative_path) != plan.target:
            raise ReviewError('The selected file changed after confirmation. Nothing was moved.')
        if plan.flagged_duplicate:
            for copy in plan.duplicates:
                try:
                    if self._file_proof(copy.relative_path) == copy:
                        break
                except (OSError, ValueError):
                    continue
            else:
                raise ReviewError('The other identical copies changed or disappeared. Nothing was moved; refresh the catalogue.')
        self._unchanged_catalogue()
        target = self.material_path(plan.target.relative_path)
        if file_fingerprint(target.lstat()) != plan.target.fingerprint:
            raise ReviewError('The selected file changed after confirmation. Nothing was moved.')
        from library_records import check_read_cancelled
        check_read_cancelled()
        return move_to_trash(target)

    def save(self, relative, edits, *, progress=None, cancelled=None):
        from library_records import reading_context
        with reading_context(progress,cancelled):
            return self._save(relative,edits)

    def _save(self, relative, edits):
        identity = path_key(relative)
        if identity not in self.by_path:
            raise ReviewError('This entry is no longer in the loaded catalogue.')
        values = {field: str(edits.get(field, '')).strip() for field in EDITABLE}
        if any(len(value) > 4096 or any(char in value for char in '\r\n\x00') for value in values.values()):
            raise ReviewError('Use a single line of up to 4096 characters for each correction.')
        if values == self.edits(relative):
            return None
        row = self.by_path[identity]
        target = self.material_path(row['relative_path'])
        try:
            info = target.lstat()
        except OSError as exc:
            raise ReviewError('The file is missing or unavailable. Refresh the catalogue before saving.') from exc
        modified = datetime.fromtimestamp(info.st_mtime).isoformat(timespec='seconds')
        if (not stat.S_ISREG(info.st_mode) or info.st_size != row.get('size_bytes')
                or modified != row.get('modified')):
            raise ReviewError('The file changed since the scan. Refresh the catalogue before saving.')
        self._safe_report(self.catalogue)
        if self.catalogue.read_bytes() != self.catalogue_bytes:
            raise ReviewError('The catalogue changed while this editor was open. Close and reopen Review Files.')
        if self._current_corrections() != self.correction_bytes:
            raise ReviewError('Corrections changed in another window or program. Keep your edits, then reopen Review Files.')
        if self.record_store.exists:
            from intake_workflow import Intake
            if Intake(self.layout).pending():
                raise ReviewError('Recover interrupted filing in Intake before editing corrections.')
            record = self.record_store.by_path(row['relative_path'])
            if record and row.get('record_id') and record['id'] != row.get('record_id'):
                raise ReviewError('The copy identity changed. Refresh and reopen Review Files.')
            with self.record_store.lock():
                if record is None:
                    record = self.record_store.identify_one(row['relative_path'])
                self.record_store.set_metadata(record['id'], values)
                self.record_store.save()
                self.record_store.export()
            self._load_overrides()
            return self.record_store.history
        header = list(self.header)
        for field in EDITABLE:
            if field not in header:
                header.append(field)
        rows = [list(row) for row in self.correction_rows]
        path_position = header.index('relative_path')
        current = next((row for row in rows if len(row) > path_position
                        and path_key(row[path_position]) == identity), None)
        if current is None:
            current = [''] * len(header)
            rows.append(current)
        current.extend([''] * (len(header) - len(current)))
        current[path_position] = row['relative_path']
        for field, value in values.items():
            current[header.index(field)] = value
        output = StringIO(newline='')
        newline = '\r\n' if self.correction_bytes is None or b'\r\n' in self.correction_bytes else '\n'
        csv.writer(output, lineterminator=newline).writerows([header, *rows])
        bom = b'\xef\xbb\xbf' if self.correction_bytes is None or self.correction_bytes.startswith(b'\xef\xbb\xbf') else b''
        data = bom + output.getvalue().encode('utf-8')
        self.corrections.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if self.correction_bytes is not None:
            folder = self.corrections.parent / ('Backups' if self.layout.organized else '.metadata-corrections-backups')
            self._safe_report(folder)
            folder.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup = folder / f'metadata_corrections-{stamp}-{uuid.uuid4().hex[:8]}.csv'
            with backup.open('xb') as stream:
                stream.write(self.correction_bytes)
                stream.flush()
                os.fsync(stream.fileno())
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix='.corrections-', suffix='.tmp', dir=self.corrections.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                if self.corrections.exists() and hasattr(os, 'fchmod'):
                    os.fchmod(stream.fileno(), stat.S_IMODE(self.corrections.stat().st_mode))
            if self._current_corrections() != self.correction_bytes or self.catalogue.read_bytes() != self.catalogue_bytes:
                raise ReviewError('The catalogue or corrections changed during saving. Nothing was replaced; reopen Review Files.')
            os.replace(temporary, self.corrections)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        self.correction_bytes = data
        self.header, self.correction_rows = header, rows
        self._load_overrides()
        return backup


def explain_flags(row):
    flags = str(row.get('review_flags') or '')
    explanations = []
    if 'Empty file' in flags:
        explanations.append('This file contains no data. A title correction cannot repair it.')
    if 'Exact duplicate file' in flags:
        explanations.append('The last scan found another file with identical bytes. Corrections keep both files and this warning.')
    if 'Cannot inspect metadata' in flags:
        explanations.append('The document metadata could not be read. A catalogue correction does not repair the document.')
    if any(reason in flags for reason in ('Missing title', 'Missing author', 'Suspicious title')):
        explanations.append('You can provide a catalogue correction below without changing embedded document metadata.')
    return '\n\n'.join(explanations) or ('No review flag in the last scan.' if not flags else 'This reason comes from the last catalogue scan.')


class ReviewWindow:
    def __init__(self, parent, store, *, on_saved, on_close, is_busy, reveal_file, trash_file=None):
        import tkinter as tk
        from tkinter import ttk
        self.store = store
        self.on_saved, self.on_close = on_saved, on_close
        self.is_busy, self.reveal_file = is_busy, reveal_file
        self.trash_file = trash_file
        self.trash_job = False
        self.pending_trash = None
        self.selected = None
        self.baseline = dict.fromkeys(EDITABLE, '')
        self.loading = False
        self.busy = False
        self.saved_pending = False
        self.visible = {}
        self.top = tk.Toplevel(parent)
        self.top.title('Review Files · Pinakeys')
        self.top.transient(parent)
        self.top.geometry('1120x760')
        self.top.minsize(940, 680)
        self.top.protocol('WM_DELETE_WINDOW', self.close)
        self.top.configure(background=PALETTE['paper'])
        self.top.columnconfigure(0, weight=1)
        self.top.rowconfigure(0, weight=1)
        main = ttk.Frame(self.top, padding=24)
        main.grid(sticky='nsew')
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)
        ttk.Label(main, text='Review your files', style='Brand.TLabel').grid(sticky='w')
        ttk.Label(main, text='Correct catalogue details. Moving a file to Trash needs your confirmation.', style='Muted.TLabel').grid(sticky='w', pady=(4, 16))
        filters = ttk.Frame(main)
        filters.grid(sticky='ew', pady=(0, 14))
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text='Find a file').grid(row=0, column=0, padx=(0, 10))
        self.query = tk.StringVar()
        self.search = ttk.Entry(filters, textvariable=self.query)
        self.search.grid(row=0, column=1, sticky='ew')
        self.search.bind('<Return>', lambda event: self.filter_rows())
        self.search_button = ttk.Button(filters, text='Search', command=self.filter_rows, style='Quiet.TButton')
        self.search_button.grid(row=0, column=2, padx=10)
        self.review_only = tk.BooleanVar(value=True)
        self.filter_toggle = ttk.Checkbutton(filters, text='Needs review only', variable=self.review_only, command=self.filter_rows)
        self.filter_toggle.grid(row=0, column=3)
        self.applied_filters = ('', True)
        panes = ttk.Panedwindow(main, orient='horizontal')
        panes.grid(row=3, column=0, sticky='nsew')
        listing = ttk.Frame(panes, padding=(0, 0, 18, 0))
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(1, weight=1)
        panes.add(listing, weight=3)
        self.count = tk.StringVar()
        ttk.Label(listing, textvariable=self.count, style='Section.TLabel').grid(sticky='w', pady=(0, 8))
        self.tree = ttk.Treeview(listing, columns=('file', 'reason'), show='headings', selectmode='browse')
        self.tree.heading('file', text='File / displayed title')
        self.tree.heading('reason', text='Review reason')
        self.tree.column('file', width=285, minwidth=160)
        self.tree.column('reason', width=190, minwidth=130)
        self.tree.grid(row=1, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(listing, orient='vertical', command=self.tree.yview)
        scrollbar.grid(row=1, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        detail_outer = ttk.Frame(panes, padding=(18, 0, 0, 0))
        detail_outer.columnconfigure(0, weight=1)
        detail_outer.rowconfigure(0, weight=1)
        panes.add(detail_outer, weight=2)
        self.detail_canvas = tk.Canvas(detail_outer, background=PALETTE['paper'], highlightthickness=0, width=390)
        self.detail_canvas.grid(row=0, column=0, sticky='nsew')
        detail_scroll = ttk.Scrollbar(detail_outer, orient='vertical', command=self.detail_canvas.yview)
        detail_scroll.grid(row=0, column=1, sticky='ns')
        self.detail_canvas.configure(yscrollcommand=detail_scroll.set)
        for event in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            self.top.bind(event, self.scroll_detail, add='+')
        detail = ttk.Frame(self.detail_canvas, padding=(0, 0, 10, 0))
        canvas_item = self.detail_canvas.create_window((0, 0), window=detail, anchor='nw')
        detail.bind('<Configure>', lambda event: self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox('all')))
        def resize_detail(event):
            self.detail_canvas.itemconfigure(canvas_item, width=event.width)
            for child in detail.winfo_children():
                if isinstance(child, ttk.Label):
                    child.configure(wraplength=max(160, event.width - 12))
        self.detail_canvas.bind('<Configure>', resize_detail)
        detail.columnconfigure(0, weight=1)
        self.file_name = tk.StringVar(value='Select a file to review')
        self.file_path = tk.StringVar()
        self.file_status = tk.StringVar()
        self.reasons = tk.StringVar()
        self.explanation = tk.StringVar()
        ttk.Label(detail, textvariable=self.file_name, style='Section.TLabel', wraplength=370).grid(sticky='w')
        ttk.Label(detail, textvariable=self.file_path, style='Muted.TLabel', wraplength=370).grid(sticky='w', pady=(4, 8))
        ttk.Label(detail, textvariable=self.file_status, style='Muted.TLabel', wraplength=370).grid(sticky='w')
        ttk.Label(detail, textvariable=self.reasons, wraplength=370).grid(sticky='w', pady=(14, 6))
        ttk.Label(detail, textvariable=self.explanation, style='Muted.TLabel', wraplength=370).grid(sticky='w', pady=(0, 18))
        ttk.Label(detail, text='Catalogue corrections', style='Section.TLabel').grid(sticky='w', pady=(0, 4))
        self.inputs, self.entries, self.sources = {}, [], {}
        for field, label in (('title', 'Title correction'), ('author', 'Author correction')):
            ttk.Label(detail, text=label).grid(sticky='w', pady=(8, 4))
            variable = tk.StringVar()
            entry = ttk.Entry(detail, textvariable=variable, width=36)
            entry.grid(sticky='ew')
            variable.trace_add('write', self.changed)
            self.inputs[field] = variable
            self.entries.append(entry)
            self.sources[field] = tk.StringVar()
            ttk.Label(detail, textvariable=self.sources[field], style='Muted.TLabel', wraplength=370).grid(sticky='w', pady=(4, 0))
        ttk.Label(detail, text='Edit the details, then save both fields as catalogue corrections. Blank fields use embedded metadata when available.', style='Muted.TLabel', wraplength=370).grid(sticky='w', pady=(12, 16))
        buttons = ttk.Frame(detail_outer)
        buttons.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(12, 0))
        self.save_button = ttk.Button(buttons, text='Save corrections', command=self.save, style='Primary.TButton')
        self.save_button.pack(side='left')
        self.discard_button = ttk.Button(buttons, text='Discard edits', command=self.discard, style='Quiet.TButton')
        self.discard_button.pack(side='left', padx=(10, 0))
        self.save_cancel = None
        self.cancel_save_button = ttk.Button(buttons,text='Cancel file check',command=lambda:self.save_cancel.set() if self.save_cancel else None,style='Quiet.TButton',state='disabled')
        self.cancel_save_button.pack(side='left',padx=(10,0))
        file_actions = ttk.Frame(detail_outer)
        file_actions.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(10, 0))
        self.reveal_button = ttk.Button(file_actions,
            text='Show in Finder' if sys.platform == 'darwin' else 'Show containing folder',
            command=self.show_file, style='Quiet.TButton')
        self.reveal_button.pack(side='left')
        self.trash_button = ttk.Button(file_actions, text='Move to Trash…', command=self.move_to_trash, style='Remove.TButton')
        self.trash_button.pack(side='left', padx=(12, 0))
        if trash_file is None:
            ttk.Label(detail_outer, text='Trash is available on Mac in this version.', style='Muted.TLabel').grid(row=3, sticky='w', pady=(6, 0))
        self.status = tk.StringVar(value='Select a file. Saving keeps a backup and refreshes the catalogue.')
        ttk.Label(main, textvariable=self.status, wraplength=1020).grid(row=4, sticky='ew', pady=(16, 0))
        self.filter_rows()

    @property
    def dirty(self):
        return bool(self.selected and self.values() != self.baseline)

    def values(self):
        return {field: variable.get().strip() for field, variable in self.inputs.items()}

    def scroll_detail(self, event):
        canvas = self.detail_canvas
        if not (canvas.winfo_rootx() <= event.x_root < canvas.winfo_rootx() + canvas.winfo_width()
                and canvas.winfo_rooty() <= event.y_root < canvas.winfo_rooty() + canvas.winfo_height()):
            return
        number = getattr(event, 'num', None)
        delta = int(getattr(event, 'delta', 0))
        steps = (-1 if number == 4 else 1) if number in (4, 5) else (-int(delta / 120) if abs(delta) >= 120 else -delta)
        if steps:
            canvas.yview_scroll(steps, 'units')
            return 'break'

    def changed(self, *args):
        if not self.loading:
            self.update_controls()

    def update_controls(self):
        enabled = self.selected is not None and not self.busy
        for entry in self.entries:
            entry.configure(state='normal' if enabled else 'disabled')
        for button in (self.save_button, self.discard_button):
            button.configure(state='normal' if enabled and self.dirty else 'disabled')
        self.reveal_button.configure(state='normal' if enabled else 'disabled')
        self.trash_button.configure(state='normal' if enabled and self.trash_file is not None
            and not self.dirty and not self.pending_trash else 'disabled')
        for widget in (self.search, self.search_button, self.filter_toggle):
            widget.configure(state='disabled' if self.busy else 'normal')

    def allow_discard(self):
        from tkinter import messagebox
        return not self.dirty or messagebox.askyesno('Discard unsaved edits?',
            'This file has unsaved corrections. Discard these edits?', parent=self.top)

    def filter_rows(self):
        if self.busy:
            return
        if not self.allow_discard():
            self.query.set(self.applied_filters[0]); self.review_only.set(self.applied_filters[1])
            return
        chosen = self.selected
        self.loading = True
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.visible = {}
        for index, row in enumerate(self.store.filtered(self.query.get(), self.review_only.get())):
            item = str(index)
            self.visible[item] = row['relative_path']
            self.tree.insert('', 'end', iid=item, values=(row.get('title') or row.get('filename') or row['relative_path'],
                row.get('review_flags') or 'No review flag'))
        self.count.set(f'{len(self.visible)} shown · {sum(bool(row.get("review_flags")) for row in self.store.rows)} need review')
        self.applied_filters = (self.query.get(), self.review_only.get())
        item = next((item for item, path in self.visible.items() if path == chosen), next(iter(self.visible), None))
        self.selected = None
        self.loading = False
        if item is not None:
            self.tree.selection_set(item); self.tree.focus(item); self.tree.see(item)
            self.load(self.visible[item])
        else:
            self.load(None)
            self.status.set('No matching files. Change the search or turn off Needs review only.')

    def select(self, event=None):
        if self.loading:
            return
        items = self.tree.selection()
        if not items:
            return
        relative = self.visible.get(items[0])
        if relative == self.selected:
            return
        if self.busy or not self.allow_discard():
            self.loading = True
            old = next((item for item, path in self.visible.items() if path == self.selected), None)
            if old is not None:
                self.tree.selection_set(old)
            self.loading = False
            return
        self.load(relative)

    def load(self, relative):
        self.loading = True
        self.selected = relative
        row = self.store.by_path[path_key(relative)] if relative else {}
        self.file_name.set(row.get('filename') or ('Select a file to review' if not relative else relative))
        self.file_path.set(PurePosixPath(relative).parent.as_posix() if relative else '')
        self.file_status.set(' · '.join(str(row.get(field) or '') for field in ('format', 'read_status')) if relative else '')
        self.reasons.set(str(row.get('review_flags') or ('No review flag' if relative else '')))
        self.explanation.set(explain_flags(row) if relative else '')
        values, sources = (self.store.prefill(relative) if relative else
                           (dict.fromkeys(EDITABLE, ''), dict.fromkeys(EDITABLE, '')))
        for field, variable in self.inputs.items():
            variable.set(values[field])
            self.sources[field].set(sources[field])
        self.baseline = self.values()
        self.loading = False
        self.update_controls()

    def discard(self):
        self.load(self.selected)
        self.status.set('Unsaved edits discarded. Saved corrections are unchanged.')

    def save(self):
        from tkinter import messagebox
        if not self.selected or not self.dirty:
            return
        if self.busy or self.is_busy():
            messagebox.showinfo('Wait for the current action', 'Finish the current action before saving corrections.', parent=self.top)
            return
        if self.store.record_store.exists:
            self.save_portable()
            return
        try:
            values = self.values()
            unchanged = values == self.store.edits(self.selected)
            self.store.save(self.selected, values)
        except (OSError, ValueError) as exc:
            messagebox.showerror('Corrections were not saved', str(exc), parent=self.top)
            return
        self.baseline = values
        self.update_controls()
        if unchanged:
            self.status.set('Saved corrections are unchanged. Blank fields use embedded metadata when available.')
            return
        for field, value in values.items():
            self.sources[field].set('Saved correction' if value else 'Blank correction — use embedded metadata when available')
        self.saved_pending = True
        self.status.set('Corrections saved. Updating the catalogue…')
        self.update_controls()
        if not self.on_saved():
            self.status.set('Corrections saved. Catalogue refresh could not start; use Refresh Catalogue to apply them.')

    def save_portable(self):
        """Verify only this file off Tk, with cancellation before committing."""
        from tkinter import messagebox
        values,relative=self.values(),self.selected
        if values==self.store.edits(relative):
            self.baseline=values
            self.update_controls()
            self.status.set('Saved corrections are unchanged. Blank fields use embedded metadata when available.')
            return
        events=Queue()
        self.save_cancel=Event()
        cancelled=self.save_cancel
        self.trash_job=True
        self.set_busy(True)
        self.cancel_save_button.configure(state='normal')
        self.status.set('Verifying the selected file before saving…')
        def work():
            read=0;last=0.0
            def progress(path,count):
                nonlocal read,last
                read+=count
                if time.monotonic()-last>0.15:
                    events.put(('progress',f'Checking {path.name} · {read/1048576:.1f} MiB read'))
                    last=time.monotonic()
            try:
                self.store.save(relative,values,progress=progress,cancelled=cancelled.is_set)
                events.put(('done',None))
            except (OSError,ValueError) as exc:
                events.put(('error',exc))
            except Exception as exc:
                events.put(('error',exc))
        Thread(target=work,daemon=True).start()
        def poll():
            outcome=None
            while True:
                try:kind,payload=events.get_nowait()
                except Empty:break
                if kind=='progress':self.status.set(payload)
                else:outcome=(kind,payload)
            if outcome is None:
                self.top.after(60,poll)
                return
            self.trash_job=False
            self.save_cancel=None
            self.cancel_save_button.configure(state='disabled')
            self.set_busy(False)
            if outcome[0]=='error':
                self.status.set(str(outcome[1]))
                if not cancelled.is_set():messagebox.showerror('Corrections were not saved',str(outcome[1]),parent=self.top)
                return
            self.baseline=values
            self.saved_pending=True
            for field,value in values.items():
                self.sources[field].set('Saved correction' if value else 'Blank correction — use embedded metadata when available')
            self.status.set('Corrections saved. Updating the catalogue…')
            self.update_controls()
            if not self.on_saved():self.status.set('Corrections saved. Refresh Catalogue to update its view.')
        self.top.after(60,poll)

    def set_busy(self, busy):
        self.busy = busy
        self.update_controls()

    def refresh_finished(self, success, catalogue_changed=True):
        self.set_busy(False)
        if success and catalogue_changed:
            try:
                self.store = ReviewStore(self.store.layout)
            except (OSError, ValueError) as exc:
                self.status.set('Could not reload the catalogue: ' + str(exc))
                return
            self.filter_rows()
            self.status.set('File moved to Trash and catalogue updated. Saved corrections are preserved.' if self.pending_trash else
                ('Corrections saved and catalogue updated.' if self.saved_pending else 'Catalogue updated.'))
            self.saved_pending = False
            self.pending_trash = None
            self.update_controls()
        elif self.pending_trash:
            self.status.set('The file was moved to Trash. Catalogue update did not finish; refresh it before removing another file.')
        elif self.saved_pending:
            self.status.set('Corrections are saved. Catalogue update did not finish; refresh it again before relying on the reports.')

    def _trash_task(self, work, finished):
        """Keep hashing/native file operations off Tk's event thread."""
        from tkinter import messagebox
        outcomes = Queue(maxsize=1)
        cancelled=Event()
        self.save_cancel=cancelled
        self.cancel_save_button.configure(state='normal')
        read=[0]
        self.trash_job = True
        self.set_busy(True)
        def run():
            try:
                from library_records import reading_context
                def progress(path,count):read[0]+=count
                with reading_context(progress,cancelled.is_set):
                    outcomes.put((work(), None))
            except Exception as exc:
                outcomes.put((None, exc))
        Thread(target=run, daemon=True).start()
        def poll():
            try:
                result, error = outcomes.get_nowait()
            except Empty:
                if read[0]:self.status.set(f'Checking file contents · {read[0]/1048576:.1f} MiB read')
                self.top.after(60, poll)
                return
            self.save_cancel=None
            self.cancel_save_button.configure(state='disabled')
            self.trash_job = False
            self.set_busy(False)
            if error is not None and cancelled.is_set():
                self.status.set('File check cancelled. No file was moved.')
                return
            if error is not None:
                self.status.set('Trash action did not complete. Check the file and refresh before trying again.')
                messagebox.showerror('File was not confirmed moved to Trash', str(error), parent=self.top)
                return
            finished(result)
        self.top.after(60, poll)

    def move_to_trash(self):
        from tkinter import messagebox
        if not self.selected or self.busy or self.is_busy() or self.pending_trash:
            return
        if self.dirty:
            messagebox.showinfo('Unsaved corrections', 'Save or discard your edits before moving this file to Trash.', parent=self.top)
            return
        if self.trash_file is None:
            messagebox.showinfo('Trash unavailable', 'Use your file manager to remove files on this computer.', parent=self.top)
            return
        store, relative = self.store, self.selected
        self.status.set('Checking the selected file and any identical copies…')
        def confirm(plan):
            notice = f'Move this file to the Mac\'s Trash?\n\n{plan.target.relative_path}\n\n'
            if plan.duplicates:
                notice += 'Verified identical copies that will stay in the library:\n'
                notice += '\n'.join(copy.relative_path for copy in plan.duplicates[:3])
                if len(plan.duplicates) > 3:
                    notice += f'\n…and {len(plan.duplicates) - 3} more.'
            else:
                notice += 'No identical copy has been verified. This may be your only copy.'
            notice += '\n\nOnly the selected file will move. Restore it from Trash before emptying Trash, then refresh the catalogue. Saved corrections are kept.'
            if not messagebox.askyesno('Move selected file to Trash?', notice, default='no', icon='warning', parent=self.top):
                self.status.set('Cancelled. The file and saved corrections are unchanged.')
                return
            self.status.set('Rechecking the file and moving it to Trash…')
            self._trash_task(lambda: store.trash(plan, self.trash_file), lambda destination: self._trashed(relative))
        self._trash_task(lambda: store.prepare_trash(relative), confirm)

    def _trashed(self, relative):
        self.pending_trash = relative
        self.store.rows = [row for row in self.store.rows if path_key(row['relative_path']) != path_key(relative)]
        self.store.by_path.pop(path_key(relative), None)
        self.load(None)
        self.filter_rows()
        self.status.set('File moved to Trash. Updating the catalogue…')
        if not self.on_saved():
            self.status.set('File moved to Trash. Catalogue refresh could not start; use Refresh Catalogue before removing another file.')

    def show_file(self):
        from tkinter import messagebox
        if not self.selected:
            return
        try:
            self.reveal_file(self.store.material_path(self.selected))
        except (OSError, ValueError) as exc:
            messagebox.showinfo('Could not show the file', str(exc), parent=self.top)

    def focus(self):
        self.top.deiconify(); self.top.lift(); self.top.focus_set()

    def close(self):
        if self.trash_job:
            from tkinter import messagebox
            messagebox.showinfo('Please wait', 'Finish the file check or Trash action before closing.', parent=self.top)
            return False
        if not self.allow_discard():
            return False
        self.top.destroy()
        self.on_close()
        return True
