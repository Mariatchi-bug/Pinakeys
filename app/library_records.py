"""Portable per-copy identities. SHA-256 identifies a version, never a copy.

The JSON ledger is authoritative; CSV/JSON reports are replaceable exports.
Legacy CSVs are retained byte for byte and imported only by explicit action.
No document parsers, network requests, or embedded metadata writes here.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import csv
from datetime import datetime, timezone
import hashlib
from io import StringIO
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import socket
import stat
import tempfile
import unicodedata
import uuid
from threading import local

from library_paths import LibraryPaths, file_identity, file_state

SCHEMA = 1
FIELDS = ('title', 'author', 'date', 'identifier')
SCOPES = ('Incoming Files', 'Incoming', 'Content', 'Books')


class RecordError(ValueError):
    pass


def process_running(pid):
    """Read-only liveness check. Windows os.kill(pid, 0) is not a probe."""
    if type(pid) is not int or pid <= 0:
        raise RecordError('The saved process identity is invalid; keep the lock for recovery.')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: no such process
                return False
            raise RecordError('Cannot confirm that the saved Windows process exited; keep the lock.')
        try:
            state = kernel.WaitForSingleObject(handle, 0)
            if state == 0:  # signalled: process exited
                return False
            if state == 258:  # WAIT_TIMEOUT: process still running
                return True
            raise RecordError('Cannot determine the saved process state; keep the lock.')
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise RecordError('Cannot confirm that the saved process exited; keep the lock.') from exc
    return True


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def key(path):
    return unicodedata.normalize('NFC', path).casefold()


def spelling(path):
    return unicodedata.normalize('NFC', path)


def safe_path(root, relative):
    """Reject nonportable traversal and symlinks, including symlink parents."""
    p = PurePosixPath(relative)
    if (not relative or p.as_posix() != relative or p.is_absolute()
            or PureWindowsPath(relative).drive or '\\' in relative
            or any(x in ('.', '..') or x.startswith('.') or '\x00' in x for x in p.parts)):
        raise RecordError('Use a relative path inside this USB, without hidden folders or traversal.')
    root = Path(root).resolve()
    target = root
    for part in p.parts:
        candidate = target / part
        # macOS may decompose accented names. Resolve a unique canonical
        # spelling when that same USB is subsequently used on Linux/Windows.
        if not candidate.exists() and not candidate.is_symlink() and target.is_dir():
            aliases = [x for x in target.iterdir() if spelling(x.name) == spelling(part)]
            if len(aliases) > 1:
                raise RecordError('Ambiguous Unicode spelling. Resolve the name collision first.')
            if aliases:
                candidate = aliases[0]
        target = candidate
    if not target.resolve().is_relative_to(root):
        raise RecordError('The path leaves this USB.')
    cursor = target
    while cursor != root:
        if cursor.is_symlink():
            raise RecordError('Symbolic links are not supported for records or filing.')
        cursor = cursor.parent
    return target


PROGRESS = None
READ_CONTEXT = local()

@contextmanager
def reading_context(progress=None, cancelled=None):
    old=getattr(READ_CONTEXT,'progress',None),getattr(READ_CONTEXT,'cancelled',None)
    READ_CONTEXT.progress,READ_CONTEXT.cancelled=progress,cancelled
    try:
        yield
    finally:
        READ_CONTEXT.progress,READ_CONTEXT.cancelled=old

def check_read_cancelled():
    cancelled=getattr(READ_CONTEXT,'cancelled',None)
    if cancelled and cancelled():
        raise RecordError('File check cancelled before saving; corrections are unchanged.')

def proof(path):
    check_read_cancelled()
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise RecordError('Choose a regular file, not a symbolic link.')
    h = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if file_identity(opened) != file_identity(before):
            raise RecordError('The file changed during identification. Refresh and try again.')
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            check_read_cancelled()
            h.update(block)
            callback=getattr(READ_CONTEXT,'progress',None) or PROGRESS
            if callback:
                callback(path, len(block))
        if file_state(os.fstat(stream.fileno())) != file_state(opened):
            raise RecordError('The file changed during identification. Refresh and try again.')
    if file_state(path.lstat()) != file_state(before):
        raise RecordError('The file changed during identification. Refresh and try again.')
    return {'sha256': h.hexdigest(), 'size_bytes': before.st_size}


def inventory(root):
    root = Path(root).resolve()
    result = []
    seen = set()
    for scope in SCOPES:
        folder = safe_path(root, scope)
        if not folder.exists():
            continue
        for base, dirs, files in os.walk(folder, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d != '__pycache__'
                             and not (Path(base) / d).is_symlink())
            for name in sorted(files):
                path = Path(base) / name
                if name.startswith('.') or path.is_symlink():
                    continue
                relative = path.relative_to(root).as_posix()
                if key(relative) in seen:
                    raise RecordError('Two paths differ only in case or Unicode spelling. Resolve this before portable filing.')
                seen.add(key(relative))
                result.append({'relative_path': relative, **proof(path)})
    return result


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.pinakeys-', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


class Records:
    def __init__(self, layout):
        self.layout = LibraryPaths(layout.root.resolve(), layout.organized)
        relative = ('Library Tools/Corrections' if layout.organized else 'Corrections')
        self.folder = safe_path(self.layout.root, relative)
        self.path = safe_path(self.layout.root, relative + '/library_records.json')
        self.history = safe_path(self.layout.root, relative + '/Record History')
        self.lock_path = safe_path(self.layout.root, relative + '/records.lock')
        self.raw = self.path.read_bytes() if self.path.exists() else None
        self.data = json.loads(self.raw) if self.raw else {
            'schema': SCHEMA, 'library_id': str(uuid.uuid4()), 'revision': 0,
            'records': [], 'legacy_imports': [], 'unmatched_corrections': [],
            'unmatched_audits': [], 'ambiguities': [], 'private_imports': [],
        }
        if (not isinstance(self.data, dict) or self.data.get('schema') != SCHEMA
                or not isinstance(self.data.get('records'), list)):
            raise RecordError('Unsupported or damaged record ledger. Preserve it for recovery.')
        if (not isinstance(self.data.get('library_id'),str) or type(self.data.get('revision')) is not int
                or self.data['revision']<0):
            raise RecordError('Invalid library identity or revision; preserve this ledger for recovery.')
        for name in ('legacy_imports','unmatched_corrections','unmatched_audits','ambiguities','private_imports'):
            self.data.setdefault(name,[])
            if not isinstance(self.data[name],list):
                raise RecordError('Invalid recovery history; preserve this ledger for recovery.')
        for r in self.data['records']:
            if (not isinstance(r, dict) or not all(isinstance(r.get(k), str) for k in ('id', 'path', 'sha256'))
                    or not re.fullmatch(r'[0-9a-f]{64}', r['sha256'])
                    or r.get('state') not in ('present', 'missing', 'ambiguous')
                    or not isinstance(r.get('corrections', {}), dict)
                    or not isinstance(r.get('audits', []), list)):
                raise RecordError('Damaged copy record. Preserve the ledger for recovery.')
            safe_path(self.layout.root, r['path'])
        ids = [r['id'] for r in self.data['records']]
        if len(set(ids)) != len(ids):
            raise RecordError('Repeated record IDs. Preserve the ledger for recovery.')

    @property
    def exists(self):
        return self.raw is not None

    @contextmanager
    def lock(self):
        if not self.layout.root.is_dir():
            raise RecordError('The library drive is unavailable. Reconnect it before continuing.')
        safe_path(self.layout.root, self.lock_path.relative_to(self.layout.root).as_posix())
        self.folder.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RecordError('Another records action is active, or an interruption left a lock. Use Recover Interrupted Filing when no other Pinakeys is running.') from exc
        token = uuid.uuid4().hex
        before = copy.deepcopy(self.data)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(encoded({'token': token, 'host': socket.gethostname(), 'pid': os.getpid()}))
                stream.flush()
                os.fsync(stream.fileno())
            self.check_current()
            yield
        except BaseException:
            # A failed import/review must not leak partial in-memory changes
            # into the next action in this window.
            current = self.path.read_bytes() if self.path.exists() else None
            if current:
                self.raw, self.data = current, json.loads(current)
            else:
                self.raw, self.data = None, before
            raise
        finally:
            if self.lock_path.exists() and json.loads(self.lock_path.read_bytes()).get('token') == token:
                self.lock_path.unlink()

    def release_interrupted_lock(self):
        """Caller must explicitly confirm no other computer/process is using USB."""
        if self.lock_path.exists():
            lock = json.loads(self.lock_path.read_bytes())
            if lock.get('host') == socket.gethostname():
                if process_running(lock.get('pid')):
                    raise RecordError('The recorded process is still running; close it before recovery.')
            self.lock_path.unlink()

    def check_current(self):
        current = self.path.read_bytes() if self.path.exists() else None
        if current != self.raw:
            raise RecordError('Records changed in another window. Reload before continuing.')

    def save(self):
        safe_path(self.layout.root, self.path.relative_to(self.layout.root).as_posix())
        self.check_current()
        if self.raw is not None:
            self.history.mkdir(parents=True, exist_ok=True)
            backup = safe_path(self.layout.root, (self.history / (uuid.uuid4().hex + '.json')).relative_to(self.layout.root).as_posix())
            atomic(backup, self.raw)
        self.data['revision'] += 1
        data = encoded(self.data)
        atomic(self.path, data)
        self.raw = data

    def get(self, identity):
        for record in self.data['records']:
            if record['id'] == identity:
                return record
        raise RecordError('Record not found. Reload the intake list.')

    def by_path(self, relative):
        matches = [r for r in self.data['records'] if spelling(r['path']) == spelling(relative) and r['state'] != 'missing']
        return matches[0] if len(matches) == 1 else None

    def corrections(self, record):
        return (record.get('corrections', {}) if record.get('corrections_sha256') == record['sha256']
                and record['state'] == 'present' and not record.get('needs_verification') else {})

    def current_audit(self, record):
        if record.get('content_unverified'):
            return None
        matching = [a for a in record.get('audits', []) if a['result'].get('sha256') == record['sha256']]
        return matching[-1] if matching else None

    def audit_key(self, record):
        return digest(encoded(self.current_audit(record)))

    def sync(self, observed=None, scope=None):
        """Exact paths anchor unchanged versions; unambiguous bytes follow moves.

        Changed-path matching runs last so exchanging two filenames cannot
        attach either copy's corrections to the other one's contents.
        """
        observed = inventory(self.layout.root) if observed is None else observed
        old = self.data['records']
        used = set()
        unbound = []
        for item in observed:
            r = next((r for r in old if spelling(r['path']) == spelling(item['relative_path'])
                      and r['sha256'] == item['sha256'] and not r.get('placeholder')), None)
            if r:
                if spelling(r.get('review', {}).get('destination', '')) == spelling(item['relative_path']):
                    r['review']['destination'] = item['relative_path']
                r['path'] = item['relative_path']
                r['state'] = 'present'
                used.add(r['id'])
            else:
                unbound.append(item)
        missing = [r for r in old if r['id'] not in used and not r.get('placeholder')
                   and (scope is None or scope(r['path']))]
        for r in old:
            if r['id'] not in used and (scope is None or scope(r['path'])):
                r['state'] = 'missing'
        ambiguities = []
        groups = {}
        for item in unbound:
            groups.setdefault(item['sha256'], []).append(item)
        for sha, items in groups.items():
            candidates = [r for r in missing if r['sha256'] == sha]
            known = [r for r in old if r['sha256'] == sha and not r.get('placeholder')]
            if len(items) == len(candidates) == len(known) == 1:
                r = candidates[0]
                r.setdefault('paths', []).append(r['path'])
                r.update(path=items[0]['relative_path'], state='present')
                used.add(r['id'])
                continue
            for item in items:
                real_candidates = [x['id'] for x in candidates if x['id'] not in used]
                # A version changed in place, after all possible move matches.
                prior = next((r for r in missing if spelling(r['path']) == spelling(item['relative_path'])
                              and r['id'] not in used), None)
                if prior and not real_candidates:
                    prior.setdefault('versions', []).append({
                        'sha256': prior['sha256'], 'corrections': copy.deepcopy(prior.get('corrections', {})),
                        'review': copy.deepcopy(prior.get('review', {})), 'changed_at': now()})
                    prior.update(sha256=sha, size_bytes=item['size_bytes'], state='present', review={})
                    used.add(prior['id'])
                    continue
                r = next((x for x in old if spelling(x['path']) == spelling(item['relative_path']) and x.get('placeholder')), None)
                if r is None:
                    r = {'id': str(uuid.uuid4()), 'path': item['relative_path'], 'paths': [],
                         'sha256': sha, 'size_bytes': item['size_bytes'], 'corrections': {},
                         'audits': [], 'review': {}, 'versions': []}
                    old.append(r)
                r['state'] = 'ambiguous' if real_candidates else 'present'
                r['placeholder'] = bool(real_candidates)
                r['sha256'], r['size_bytes'] = sha, item['size_bytes']
                used.add(r['id'])
                if real_candidates:
                    ambiguities.append({'id': r['id'], 'candidates': real_candidates})
        retained=[a for a in self.data.get('ambiguities',[]) if scope is not None
                  and not scope(self.get(a['id'])['path'])]
        self.data['ambiguities'] = retained + ambiguities
        for r in old:
            if r['id'] in used:
                r['content_unverified'] = False
                r['needs_verification'] = False
                path=safe_path(self.layout.root,r['path'])
                if path.is_file():
                    s=path.stat();r['attribute_cache']=[s.st_size,s.st_mtime_ns]

    def quick_update(self, paths, scope):
        """Observe paths/attributes only. Never represent cached bytes as verified."""
        observed={p.relative_to(self.layout.root).as_posix():p for p in paths}
        for r in self.data['records']:
            if not scope(r['path']):
                continue
            p=observed.get(r['path'])
            r['content_unverified']=True
            if p is None:
                r['state']='missing'
            else:
                s=p.stat();attrs=[s.st_size,s.st_mtime_ns]
                if r.get('attribute_cache') and r['attribute_cache']!=attrs:
                    r['needs_verification']=True
                if r['state']=='missing':
                    r['needs_verification']=True
                # Identity must be verified before reviving a missing copy.
                if r['state']!='missing':r['state']='present' if not r.get('placeholder') else 'ambiguous'

    def identify_one(self, relative):
        item={'relative_path':relative,**proof(safe_path(self.layout.root,relative))}
        missing={r['path'] for r in self.data['records'] if r['state']=='missing'}
        self.sync([item],scope=lambda p:p==relative or p in missing)
        r=self.by_path(relative)
        if not r or r['state']!='present':
            raise RecordError('Copy identification is ambiguous. Use Intake → Choose copy identity; your edits have not been saved.')
        return r

    def resolve(self, identity, prior_id=None):
        r = self.get(identity)
        choice = next((x for x in self.data['ambiguities'] if x['id'] == identity), None)
        if not choice:
            raise RecordError('This record has no unresolved identity choice.')
        if prior_id is None:
            r.update(state='present', placeholder=False)
        else:
            if prior_id not in choice['candidates']:
                raise RecordError('Choose one of the missing copies shown for this file.')
            prior = self.get(prior_id)
            if prior['state'] != 'missing' or prior['sha256'] != r['sha256']:
                raise RecordError('That copy is no longer missing or its fingerprint differs.')
            prior.setdefault('paths', []).append(prior['path'])
            prior.update(path=r['path'], state='present')
            prior.setdefault('identity_resolution_history', []).append(copy.deepcopy(r))
            for audit in r.get('audits', []):
                if audit not in prior.setdefault('audits', []):
                    prior['audits'].append(audit)
            if r.get('corrections'):
                self.data['unmatched_corrections'].append({'id': str(uuid.uuid4()),
                    'row': {'relative_path': r['path'], **r['corrections']},
                    'reason': 'Corrections imported at an ambiguous new path; choose whether to use them after linking its copy.'})
            if r.get('filename_choice'):
                self.data['unmatched_corrections'].append({'id': str(uuid.uuid4()), 'kind': 'private_filename',
                    'row': r['filename_choice'], 'reason': 'Filename choice imported at an ambiguous new path; retained after identity resolution.'})
            self.data['records'].remove(r)
        # Other ambiguous copies still require their own explicit choice.
        self.data['ambiguities'] = [x for x in self.data['ambiguities'] if x['id'] != identity]
        for x in self.data['ambiguities']:
            x['candidates'] = [i for i in x['candidates'] if i != prior_id]

    def import_legacy(self):
        source = self.layout.corrections
        safe_path(self.layout.root, source.relative_to(self.layout.root).as_posix())
        raw = source.read_bytes() if source.exists() else b''
        sha = digest(raw)
        if any(x['sha256'] == sha for x in self.data['legacy_imports']):
            return
        reader = csv.DictReader(StringIO(raw.decode('utf-8-sig'), newline=''))
        rows = list(reader) if raw else []
        if raw and (not reader.fieldnames or 'relative_path' not in reader.fieldnames
                    or len(set(reader.fieldnames)) != len(reader.fieldnames)
                    or any(None in r for r in rows)):
            raise RecordError('The legacy corrections CSV is malformed. It was left unchanged.')
        # Retain full original bytes, all unknown columns, empty and duplicate rows.
        self.data['legacy_imports'].append({'sha256': sha, 'at': now(), 'utf8_csv': raw.decode('utf-8-sig')})
        counts = {}
        for row in rows:
            relative = (row.get('relative_path') or '').replace('\\', '/')
            counts[relative] = counts.get(relative, 0) + 1
        for row in rows:
            relative = (row.get('relative_path') or '').replace('\\', '/')
            r = self.by_path(relative)
            if r and counts[relative] == 1 and not r.get('corrections'):
                r['corrections'] = {k: row.get(k) or '' for k in FIELDS}
                r['corrections_sha256'] = r['sha256']
                r['legacy_rows'] = [row]
            else:
                self.data['unmatched_corrections'].append({'id': str(uuid.uuid4()), 'row': row,
                    'reason': 'Missing/ambiguous path, repeated legacy row, or an existing correction needs your choice.'})

    def recover_correction(self, orphan_id, record_id):
        r = self.get(record_id)
        self.verify(r)
        orphan = next(x for x in self.data['unmatched_corrections'] if x['id'] == orphan_id)
        if orphan.get('kind') == 'private_filename':
            r.setdefault('filename_choice_history', []).append(copy.deepcopy(r.get('filename_choice')))
            r['filename_choice'] = orphan['row']
            self.data['unmatched_corrections'].remove(orphan)
            r['review'] = {}
            return
        r.setdefault('correction_history', []).append(copy.deepcopy(r.get('corrections', {})))
        r['corrections'] = {k: orphan['row'].get(k) or '' for k in FIELDS}
        r['corrections_sha256'] = r['sha256']
        r.setdefault('legacy_rows', []).append(orphan['row'])
        r['review'] = {}
        self.data['unmatched_corrections'].remove(orphan)

    def import_private_choices(self):
        source = safe_path(self.layout.root, 'Library Tools/Personal Tools/Filename Cleanup/History/filename_choices.json')
        if not source.exists():
            return
        raw = source.read_bytes()
        sha = digest(raw)
        imports = self.data.setdefault('private_imports', [])
        if any(x['sha256'] == sha for x in imports):
            return
        saved = json.loads(raw)
        if not isinstance(saved, dict) or saved.get('version') != 1 or not isinstance(saved.get('choices'), dict):
            raise RecordError('Private filename choices are unreadable; keep History for recovery.')
        imports.append({'sha256': sha, 'at': now(), 'utf8_json': raw.decode('utf-8')})
        for identity, choice in saved['choices'].items():
            matches = [r for r in self.data['records'] if r['path'] == choice.get('path') and r['sha256'] == choice.get('sha256')]
            if not matches:
                matches = [r for r in self.data['records'] if r['sha256'] == choice.get('sha256')]
            if len(matches) == 1 and not matches[0].get('filename_choice'):
                matches[0]['filename_choice'] = choice
                matches[0]['private_choice_id'] = identity
            else:
                self.data['unmatched_corrections'].append({'id': str(uuid.uuid4()), 'kind': 'private_filename',
                    'row': choice, 'reason': 'Private filename approval has no unique current copy, or conflicts with an existing choice.'})

    def ingest_audit(self, reconcile=False):
        source = self.layout.reports / 'intake_audit.json'
        safe_path(self.layout.root, source.relative_to(self.layout.root).as_posix())
        if not source.exists():
            self.data['audit_source_sha256'] = None
            return
        raw = source.read_bytes()
        changed_source=self.data.get('audit_source_sha256')!=digest(raw)
        self.data['audit_source_sha256'] = digest(raw)
        audit = json.loads(raw)
        if not isinstance(audit, dict) or not isinstance(audit.get('files'), list):
            raise RecordError('The saved intake audit is unreadable; preserve it before continuing.')
        # A completed audit can supply historical fingerprints for new arrivals.
        # They remain explicitly unverified until the selected file is checked.
        from catalogue_settings import Selection
        selection=Selection(self.layout)
        for row in audit['files']:
            if not isinstance(row,dict):
                raise RecordError('The saved audit contains an invalid row; preserve it for recovery.')
            relative=row.get('relative_path','');sha=row.get('sha256','')
            if (not isinstance(relative,str) or not isinstance(sha,str) or not re.fullmatch(r'[0-9a-f]{64}',sha)
                    or not selection.contains(relative,intake=True)):
                continue
            existing=self.by_path(relative)
            if existing:
                if reconcile and changed_source and existing['sha256']!=sha:
                    existing.setdefault('versions',[]).append({'sha256':existing['sha256'],'corrections':copy.deepcopy(existing.get('corrections',{})),'review':copy.deepcopy(existing.get('review',{})),'changed_at':now()})
                    existing.update(sha256=sha,size_bytes=row.get('size_bytes',existing['size_bytes']),review={},content_unverified=True,needs_verification=True)
                continue
            target=safe_path(self.layout.root,relative)
            if not target.is_file():continue
            candidates=[r['id'] for r in self.data['records'] if r['state']=='missing' and r['sha256']==sha]
            r={'id':str(uuid.uuid4()),'path':relative,'paths':[],'sha256':sha,
               'size_bytes':row.get('size_bytes',target.stat().st_size),'corrections':{},'audits':[],
               'review':{},'versions':[],'state':'ambiguous' if candidates else 'present',
               'placeholder':bool(candidates),'content_unverified':True}
            self.data['records'].append(r)
            if candidates:self.data['ambiguities'].append({'id':r['id'],'candidates':candidates})
        unmatched = []
        for row in audit['files']:
            sha = row.get('sha256')
            matches = [r for r in self.data['records'] if sha and r['sha256'] == sha and spelling(r['path']) == spelling(row.get('relative_path', ''))]
            if not matches:
                matches = [r for r in self.data['records'] if sha and r['sha256'] == sha
                           and row.get('relative_path') in r.get('paths', [])]
            if len(matches) != 1:
                matches = [r for r in self.data['records'] if sha and r['sha256'] == sha]
            saved = {'scanned_at': audit.get('scanned_at'), 'tools': audit.get('tools', {}), 'result': row}
            if len(matches) == 1:
                if saved not in matches[0].setdefault('audits', []):
                    matches[0]['audits'].append(saved)
                for retained in self.data['unmatched_audits']:
                    if all(retained.get(k) == saved.get(k) for k in saved):
                        retained['resolved_record_id'] = matches[0]['id']
            else:
                unmatched.append(saved)
        # A newer audit must not erase older rows that could not be identified.
        for saved in unmatched:
            if not any(all(x.get(k) == saved.get(k) for k in saved) for x in self.data['unmatched_audits']):
                self.data['unmatched_audits'].append(saved)

    def verify(self, record):
        if record['state'] != 'present':
            raise RecordError('Resolve the missing or ambiguous file identity first.')
        result = proof(safe_path(self.layout.root, record['path']))
        if result['sha256'] != record['sha256']:
            raise RecordError('File contents changed. Reload; old audit results and approvals are outdated.')
        record['content_unverified']=False
        record['needs_verification']=False

    def set_metadata(self, identity, values):
        r = self.get(identity)
        self.verify(r)
        check_read_cancelled()
        if any(not isinstance(v, str) or len(v) > 4096 or any(c in v for c in '\r\n\x00') for v in values.values()):
            raise RecordError('Use single-line metadata fields of at most 4096 characters.')
        r.setdefault('correction_history', []).append(copy.deepcopy(r.get('corrections', {})))
        r['corrections'] = {**r.get('corrections', {}), **values}
        r['corrections_sha256'] = r['sha256']
        r['review'] = {}

    def export(self):
        self.layout.prepare_reports()
        rows = []
        for r in self.data['records']:
            rows.append({'record_id': r['id'], 'relative_path': r['path'], 'sha256': r['sha256'],
                         'state': r['state'], 'corrections_current': bool(self.corrections(r)),
                         **{k: r.get('corrections', {}).get(k, '') for k in FIELDS}})
        text = StringIO(newline='')
        columns = ['record_id', 'relative_path', 'sha256', 'state', 'corrections_current', *FIELDS]
        writer = csv.DictWriter(text, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: "'" + str(v) if str(v).lstrip().startswith(('=', '+', '-', '@')) else v for k, v in row.items()})
        for name, data in [('library_records.csv', text.getvalue().encode('utf-8-sig')),
                           ('library_records.json', encoded(self.data))]:
            path = self.layout.reports / name
            safe_path(self.layout.root, path.relative_to(self.layout.root).as_posix())
            atomic(path, data)


def relocation_bytes(layout, mapping):
    """Adapter for existing confirmed private rename/undo journals.

    Return proposed ledger bytes, never write them. The existing caller must
    protect this ledger in its backup/CAS/rollback transaction with the files.
    Supports preflight (old path exists) and post-move (new path exists).
    """
    records = Records(layout)
    if not records.exists:
        return None
    if records.lock_path.exists():
        raise RecordError('A portable-record action is running; finish it before private cleanup.')
    normalized = {key(old): new for old, new in mapping.items()}
    for r in records.data['records']:
        if key(r['path']) not in normalized:
            continue
        if r['state'] != 'present':
            raise RecordError('Resolve copy identity in Intake before renaming this file.')
        new = normalized[key(r['path'])]
        old_path, new_path = safe_path(layout.root, r['path']), safe_path(layout.root, new)
        actual = old_path if old_path.exists() else new_path
        if proof(actual)['sha256'] != r['sha256']:
            raise RecordError('File bytes changed. Refresh Intake and review before renaming.')
        r.setdefault('paths', []).append(r['path'])
        r['path'] = new
    records.data['revision'] += 1
    return encoded(records.data)
