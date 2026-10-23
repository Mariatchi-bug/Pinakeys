"""Connected intake review and journalled, explicitly confirmed filing."""
from __future__ import annotations

import copy
import ctypes
import errno
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import uuid

from library_records import Records, RecordError, atomic, digest, encoded, key, now, proof, safe_path


def filename(value):
    if (not value or value != value.strip() or value.endswith('.') or value.startswith('.')
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
            or re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value)
            or len(value.encode('utf-8')) > 240 or len(value.encode('utf-16-le')) > 480):
        raise RecordError('Choose a portable filename, at most 240 bytes, without reserved characters or names. Long titles are never shortened automatically.')
    return value


def portable_text(value):
    return ' '.join(re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' - ', value).split()).rstrip(' .')


def proposed_name(path, title, author):
    stem = portable_text(title)
    if author.strip():
        stem += ' — ' + portable_text(author)
    return filename(stem.rstrip('.') + Path(path).suffix)


def exclusive_move(source, target, token):
    """Existing private-tool strategy: no-replace rename, exFAT reservation fallback.

    The fallback replaces only its own verified marker. Other file operations
    must be idle; this is not an OS-atomic no-replace primitive on exFAT.
    """
    if os.name == 'nt':
        os.rename(source, target)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    result = None
    if sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
        fn = libc.renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        result = fn(os.fsencode(source), os.fsencode(target), 4)
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        result = fn(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    if result == 0:
        return
    if result is not None and ctypes.get_errno() not in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL}:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()), str(target))
    marker = reservation(token)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(marker)
        stream.flush()
        os.fsync(stream.fileno())
        info = os.fstat(stream.fileno())
    current = target.lstat()
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or target.read_bytes() != marker:
        raise RecordError('The destination reservation changed. Recover this operation before retrying.')
    os.replace(source, target)


def reservation(token):
    return ('PINAKEYS INTAKE RESERVATION\n' + token + '\n').encode('ascii')


class Intake:
    def __init__(self, layout):
        self.records = Records(layout)
        self.layout = self.records.layout
        rel = ('Library Tools/Corrections' if layout.organized else 'Corrections') + '/Filing History'
        self.history = safe_path(self.layout.root, rel)

    def journals(self):
        if not self.history.exists():
            return []
        result = []
        for path in sorted(self.history.glob('*.json')):
            if path.name.startswith('.'):
                continue  # macOS AppleDouble companions are not JSON journals.
            safe_path(self.layout.root, path.relative_to(self.layout.root).as_posix())
            data = json.loads(path.read_bytes())
            if not isinstance(data, dict) or data.get('schema') != 1:
                raise RecordError('A filing journal is unreadable; preserve Filing History for recovery.')
            result.append((path, data))
        return result

    def pending(self):
        return [(p, j) for p, j in self.journals() if j['status'] not in ('committed', 'undone')]

    def reload(self, *, initialize=False, import_legacy=False, quick=False):
        self.records = Records(self.layout)
        if not self.records.exists and not initialize:
            return False
        if self.pending():
            raise RecordError('An interrupted filing needs recovery before refreshing records.')
        with self.records.lock():
            from catalogue_settings import Selection
            selection=Selection(self.layout)
            if quick:
                self.records.quick_update(selection.files(intake=True),lambda p:selection.contains(p,intake=True))
            elif selection.path.exists():
                observed=[{'relative_path':p.relative_to(self.layout.root).as_posix(),**proof(p)} for p in selection.files(intake=True)]
                self.records.sync(observed,scope=lambda p:selection.contains(p,intake=True))
            else:
                self.records.sync()
            if initialize or import_legacy:
                self.records.import_legacy()
                self.records.import_private_choices()
            self.records.ingest_audit(reconcile=quick)
            self.records.save()
            self.records.export()
        return True

    def legacy_changed(self):
        data = self.layout.corrections.read_bytes() if self.layout.corrections.exists() else b''
        csv_changed = not any(x['sha256'] == digest(data) for x in self.records.data['legacy_imports'])
        source = safe_path(self.layout.root, 'Library Tools/Personal Tools/Filename Cleanup/History/filename_choices.json')
        private_changed = source.exists() and not any(x['sha256'] == digest(source.read_bytes()) for x in self.records.data.get('private_imports', []))
        return csv_changed or private_changed

    def categories(self, r):
        groups = {name: [] for name in ('File/format', 'Incomplete checks', 'Security indicators', 'Metadata', 'Duplicates', 'Identity')}
        if r['state'] != 'present':
            groups['Identity'].append('Missing file: records retained for recovery.' if r['state'] == 'missing' else
                                      'Identical missing copies could match this file. Choose its identity before editing or filing.')
        audit = self.records.current_audit(r)
        if not audit and r.get('content_unverified'):
            groups['Incomplete checks'].append('Saved checks are historical; current contents will be verified before approval.')
            matching=[a for a in r.get('audits',[]) if a['result'].get('sha256')==r['sha256']]
            audit=matching[-1] if matching else None
        if not audit:
            groups['Incomplete checks'].append('No current audit for these bytes. Run Check Incoming.' +
                (' Current contents are unverified; saved results are historical.' if r.get('content_unverified') else ' Earlier results are outdated.' if r.get('audits') else ''))
        else:
            row = audit['result']
            applicable = ['clamav'] + ({'.pdf': ['pdfid'], '.epub': ['epubcheck']}.get(Path(r['path']).suffix.casefold(), []))
            for name in applicable:
                value = str(row.get(name) or 'NOT RUN')
                upper = value.upper()
                if upper.startswith(('NOT RUN', 'UNAVAILABLE', 'SKIP', 'ERROR', 'TIMEOUT')):
                    groups['Incomplete checks'].append(name + ': ' + value)
            if row.get('checks_not_run'):
                groups['Incomplete checks'].append(row['checks_not_run'])
            if row.get('clamav') == 'DETECTION':
                groups['Security indicators'].append('ClamAV detection. Keep apart; investigate before opening or filing.')
            if row.get('pdfid') and row['pdfid'] not in ('OK', 'NOT A PDF'):
                if any(x in row['pdfid'].upper() for x in ('WARNING', 'INDICATORS FOUND')):
                    groups['Security indicators'].append('PDFiD feature warning: scripts/actions/embedded content may be legitimate. This is not a malware verdict.')
            if row.get('epubcheck') == 'Validation errors':
                groups['File/format'].append('EPUBCheck conformance errors are formatting problems, not malware findings.')
            # Preserve verbatim explanations even for future tool result formats.
            reasons = str(row.get('review_reasons') or '')
            for reason in reasons.split(' | '):
                if not reason:
                    continue
                low = reason.lower()
                category = ('Security indicators' if any(w in low for w in ('clamav flagged', 'clamav detection', 'active/embedded', 'executable signature', 'pdf feature', 'pdfid feature')) else
                            'Incomplete checks' if any(w in low for w in ('could not', 'changed', 'timeout', 'timed out', 'error running', 'not complete', 'failed', 'did not', 'returned incomplete', 'unrecognized output', 'returned an error')) else
                            'File/format')
                groups[category].append(reason)
        if r['size_bytes'] == 0:
            groups['File/format'].append('Empty file (0 bytes); metadata edits cannot repair it.')
        values = self.records.corrections(r)
        for field in ('title', 'author'):
            if not values.get(field):
                groups['Metadata'].append('No reviewed ' + field + '; filename or recovered wording remains a suggestion.')
        if r.get('corrections') and not self.records.corrections(r):
            groups['Metadata'].append('Earlier corrections retained but held after a content/identity change. Verify them before saving again.')
        copies = [x for x in self.records.data['records'] if x['id'] != r['id'] and x['state'] != 'missing' and x['sha256'] == r['sha256']]
        groups['Duplicates'] += [x['path'] + ' · separate copy ' + x['id'][:8] for x in copies]
        return groups

    def next_step(self, r):
        if r['state'] != 'present':
            return 'Recover missing file' if r['state'] == 'missing' else 'Choose copy identity'
        if r.get('content_unverified') and any(a['result'].get('sha256')==r['sha256'] for a in r.get('audits',[])):
            return 'Review findings (verify selected file)'
        if not self.records.current_audit(r):
            return 'Check Incoming (old audit outdated)' if r.get('audits') else 'Check Incoming'
        review = r.get('review', {})
        if review.get('sha256') != r['sha256'] or review.get('audit_key') != self.records.audit_key(r):
            return 'Review findings and metadata'
        return 'Filed in library' if r['path'].split('/')[0] in ('Content', 'Books') else 'Preview filing'

    def suggestions(self, r):
        stem = Path(r['path']).stem
        title, sep, author = stem.rpartition(' — ')
        values = {'title': title if sep else stem, 'author': author if sep else ''}
        notes = ['Filename suggestions; verify wording.']
        private = self.private_module('clean_download_filenames')
        passes = self.private_module('filename_passes')
        if private and passes:
            try:
                title, author, _, reasons = passes.parse_name(Path(r['path']).name, private)
                title, author, more = passes.normalize_fields(title, author)
                values.update(title=title, author=author)
                notes = reasons + more + passes.review_flags(title, author)
            except ValueError as exc:
                notes.append(str(exc))
            except private.CleanupError as exc:
                notes.append(str(exc))
        choice = r.get('filename_choice', {})
        if choice.get('sha256') == r['sha256']:
            values.update({k: choice[k] for k in values if choice.get(k)})
            notes.append('Earlier private filename approval; catalogue corrections stay separate.')
        # Prefer reviewed values, then already extracted catalogue evidence.
        source = self.layout.reports / 'catalogue.json'
        if source.exists():
            catalogue = json.loads(source.read_bytes())
            for row in catalogue:
                if row.get('record_id') == r['id'] or row.get('relative_path') == r['path']:
                    for field in ('title', 'author'):
                        if row.get(field):
                            values[field] = row[field]
        values.update({k: v for k, v in r.get('corrections', {}).items() if k in values and v})
        return values, notes

    def private_module(self, name):
        relative = 'Library Tools/Personal Tools/Filename Cleanup/' + name + '.py'
        path = safe_path(self.layout.root, relative)
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location('_pinakeys_private_' + name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def read_inside(self, identity):
        r = self.records.get(identity)
        self.records.verify(r)
        reader = self.private_module('file_content_review')
        if not reader:
            raise RecordError('The optional private Read Inside File tool is not installed in this kit. You can enter metadata manually.')
        path = safe_path(self.layout.root, r['path'])
        result = reader.inspect_file(path, reader.facts(path))
        self.records.verify(r)
        return result

    def review(self, identity, title, author, name, destination, acknowledged):
        self.check_audit_source()
        if self.pending():
            raise RecordError('Recover the interrupted filing before editing its review.')
        if not acknowledged:
            raise RecordError('Read the findings and acknowledge the limits before approving this file for a preview.')
        r = self.records.get(identity)
        self.records.verify(r)
        if not self.records.current_audit(r):
            raise RecordError('Run Check Incoming for the current file contents before approving filing.')
        target = self.destination(destination, name)
        if Path(name).suffix.casefold() != Path(r['path']).suffix.casefold():
            raise RecordError('Keep the original extension; this workflow does not convert documents.')
        with self.records.lock():
            self.records.set_metadata(identity, {'title': title.strip(), 'author': author.strip()})
            r['review'] = {'sha256': r['sha256'], 'audit_key': self.records.audit_key(r),
                           'destination': target, 'acknowledged': True, 'at': now()}
            self.records.save()
            self.records.export()

    def destination(self, folder, name):
        filename(name)
        target = folder.rstrip('/') + '/' + name
        if target.split('/')[0] not in ('Content', 'Books') or '/' not in target:
            raise RecordError('Choose a destination under Content or Books.')
        top = target.split('/')[0]
        alternate = 'Books' if top == 'Content' else 'Content'
        if not (self.layout.root / top).exists() and (self.layout.root / alternate).exists():
            raise RecordError('Use a folder under the existing ' + alternate + ' library, rather than creating a second library root.')
        for part in Path(folder).parts:
            filename(part)
        safe_path(self.layout.root, target)
        return target

    def check_audit_source(self):
        source = self.layout.reports / 'intake_audit.json'
        safe_path(self.layout.root, source.relative_to(self.layout.root).as_posix())
        actual = digest(source.read_bytes()) if source.exists() else None
        if actual != self.records.data.get('audit_source_sha256'):
            raise RecordError('The saved audit changed. Refresh intake to review the latest findings before proceeding.')

    def preview(self, identities):
        self.check_audit_source()
        self.records.check_current()
        if self.pending():
            raise RecordError('Recover the interrupted filing before preparing another preview.')
        if not identities or len(set(identities)) != len(identities):
            raise RecordError('Choose one or more distinct reviewed files.')
        moves = []
        for identity in identities:
            r = self.records.get(identity)
            self.records.verify(r)
            review = r.get('review', {})
            if self.next_step(r) not in ('Preview filing', 'Filed in library'):
                raise RecordError(r['path'] + ': review the current findings and metadata first.')
            target = review['destination']
            self.destination(str(Path(target).parent.as_posix()), Path(target).name)
            if r['path'] == target:
                continue
            moves.append({'record_id': r['id'], 'source': r['path'], 'destination': target,
                          'sha256': r['sha256'], 'audit_key': review['audit_key'], 'token': uuid.uuid4().hex})
        if not moves:
            raise RecordError('No filename or destination changes are proposed.')
        self.validate_moves(moves)
        return {'schema': 1, 'id': now().replace(':', '') + '-' + uuid.uuid4().hex[:8],
                'ledger_sha256': digest(self.records.raw), 'moves': moves, 'notice':
                'Filing records your choice. It does not certify safety; embedded metadata stays unchanged.'}

    def validate_moves(self, moves):
        targets = set()
        for move in moves:
            source = safe_path(self.layout.root, move['source'])
            target = safe_path(self.layout.root, move['destination'])
            if proof(source)['sha256'] != move['sha256']:
                raise RecordError('A source changed since the preview. Reload and review it again.')
            portable = key(move['destination'])
            if portable in targets or key(move['source']) == portable:
                raise RecordError('Two proposed names collide, or the change is case-only. Choose distinct portable names.')
            targets.add(portable)
            # Check each component for case/Unicode aliases on case-sensitive hosts.
            cursor = self.layout.root
            for part in Path(move['destination']).parts:
                if cursor.exists():
                    aliases = [p for p in cursor.iterdir() if key(p.name) == key(part)]
                    if aliases and (aliases[0].name != part or cursor / part == target):
                        raise RecordError('Destination already exists or has a case/Unicode collision: ' + move['destination'])
                cursor = cursor / part
            # macOS companion metadata must move with its file; other OSes hold it.
            companion = source.with_name('._' + source.name)
            if companion.exists() and sys.platform != 'darwin':
                raise RecordError('This file has Mac metadata companions. File it on Mac to preserve them.')

    def apply(self, plan, confirmation, *, checkpoint=None):
        """Only this method moves materials. The exact preview must be confirmed."""
        moves = plan['moves']
        self.check_audit_source()
        if confirmation != f'FILE {len(moves)}':
            raise RecordError('Confirmation did not match. No files were moved.')
        if self.pending():
            raise RecordError('Recover the interrupted filing first.')
        with self.records.lock():
            if digest(self.records.raw) != plan['ledger_sha256']:
                raise RecordError('Reviews changed after the preview. Make a fresh preview.')
            self.validate_moves(moves)
            for move in moves:
                r = self.records.get(move['record_id'])
                if (r['path'] != move['source'] or r['sha256'] != move['sha256']
                        or self.records.audit_key(r) != move['audit_key']
                        or r.get('review', {}).get('destination') != move['destination']
                        or self.next_step(r) not in ('Preview filing', 'Filed in library')):
                    raise RecordError('File identity or audit changed since the preview.')
            journal = {'schema': 1, 'status': 'applying', 'plan': copy.deepcopy(plan), 'created_at': now()}
            path = self.history / (plan['id'] + '.json')
            safe_path(self.layout.root, path.relative_to(self.layout.root).as_posix())
            atomic(path, encoded(journal))
            for index, move in enumerate(moves):
                source = safe_path(self.layout.root, move['source'])
                target = safe_path(self.layout.root, move['destination'])
                target.parent.mkdir(parents=True, exist_ok=True)
                self.validate_moves([move])
                exclusive_move(source, target, move['token'])
                if checkpoint:
                    checkpoint(index, 'moved_before_journal')
                journal['last_moved'] = index
                atomic(path, encoded(journal))
            for move in moves:
                if proof(safe_path(self.layout.root, move['destination']))['sha256'] != move['sha256']:
                    raise RecordError('A file changed during filing. Keep the recovery journal; its old audit was not approved for the changed bytes.')
                r = self.records.get(move['record_id'])
                r.setdefault('paths', []).append(r['path'])
                r['path'] = move['destination']
            self.records.save()
            if checkpoint:
                checkpoint(len(moves), 'ledger_saved')
            self.records.export()
            journal.update(status='committed', finished_at=now())
            atomic(path, encoded(journal))
        return path

    def rollback(self, path, *, confirmation):
        journal = json.loads(path.read_bytes())
        moves = journal['plan']['moves']
        if confirmation != f'UNDO {len(moves)}':
            raise RecordError('Undo confirmation did not match.')
        with self.records.lock():
            # Validate the complete reversal before making its first move.
            actions = []
            for move in reversed(moves):
                source = safe_path(self.layout.root, move['source'])
                target = safe_path(self.layout.root, move['destination'])
                r = self.records.get(move['record_id'])
                if r['path'] not in (move['source'], move['destination']) or r['sha256'] != move['sha256']:
                    raise RecordError('A filed record changed later. Recovery will not overwrite it.')
                marker = target.exists() and target.is_file() and target.stat().st_size == len(reservation(move['token'])) and target.read_bytes() == reservation(move['token'])
                if source.exists():
                    if proof(source)['sha256'] != move['sha256'] or (target.exists() and not marker):
                        raise RecordError('Original path is occupied or two copies are present. Resolve this before recovery.')
                    actions.append((move, 'marker' if marker else 'none'))
                elif target.exists() and not marker and proof(target)['sha256'] == move['sha256']:
                    # Reuse portable collision validation with reversed paths.
                    self.validate_moves([{**move, 'source': move['destination'], 'destination': move['source']}])
                    actions.append((move, 'reverse'))
                else:
                    raise RecordError('A file is missing or changed. Keep the journal for recovery; nothing was overwritten.')
            journal['status'] = 'recovering'
            atomic(path, encoded(journal))
            for move, action in actions:
                source = safe_path(self.layout.root, move['source'])
                target = safe_path(self.layout.root, move['destination'])
                if action == 'reverse':
                    source.parent.mkdir(parents=True, exist_ok=True)
                    exclusive_move(target, source, move['token'])
                elif action == 'marker':
                    target.unlink()  # Only this operation's verified reservation.
                r = self.records.get(move['record_id'])
                if r['path'] != move['source']:
                    r.setdefault('paths', []).append(r['path'])
                    r['path'] = move['source']
            self.records.save()
            self.records.export()
            journal.update(status='undone', undone_at=now())
            atomic(path, encoded(journal))

    def resolve(self, identity, prior_id):
        with self.records.lock():
            r=self.records.get(identity)
            if proof(safe_path(self.layout.root,r['path']))['sha256']!=r['sha256']:
                raise RecordError('The file changed; verify it again before choosing its identity.')
            self.records.resolve(identity, prior_id)
            self.records.get(prior_id or identity)['content_unverified']=False
            self.records.save()
            self.records.export()
