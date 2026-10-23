"""Disposable end-to-end libraries; never read a real personal catalogue."""
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unicodedata
from unittest.mock import patch

from build_usb import build_folder
from intake_workflow import Intake, proposed_name, filename, reservation
from library_paths import LibraryPaths
from library_records import Records, RecordError, inventory, proof, encoded, atomic, relocation_bytes
from review_library import ReviewStore


class IntakeFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pinakeys-intake-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.root = build_folder(Path(self.temp.name) / 'USB')
        self.layout = LibraryPaths(self.root, True)
        self.path = self.root / 'Incoming Files/arrival.txt'
        self.path.write_bytes(b'Synthetic arrival only.')
        self.flow = Intake(self.layout)

    def initialize(self):
        self.flow.reload(initialize=True)
        return self.flow.records.by_path('Incoming Files/arrival.txt')

    def audit(self, *, indicators=False, incomplete=False):
        rows = []
        for item in inventory(self.root):
            rows.append({**item, 'file_type_check': 'unknown signature (no format validation for .txt)',
                'clamav': 'NOT RUN' if incomplete else 'No malware signatures detected (not proof of safety)',
                'pdfid': 'Indicators found: /JavaScript: 1' if indicators else 'NOT RUN',
                'epubcheck': 'Validation errors' if indicators else 'NOT RUN',
                'status': 'REVIEW' if indicators else 'LIMITED CHECKS',
                'review_reasons': ('PDF contains active/embedded features requiring review (not necessarily malware) | EPUBCheck reported conformance errors (not necessarily malicious)' if indicators else ''),
                'checks_not_run': 'ClamAV' if incomplete else ''})
        (self.layout.reports / 'intake_audit.json').write_text(json.dumps({'files': rows, 'scanned_at': 'fixture run', 'tools': {}}), encoding='utf-8')
        self.flow.reload()

    def approve(self, r, name='Reviewed — Writer.txt', folder='Content/Filed'):
        self.flow.review(r['id'], 'Reviewed', 'Writer', name, folder, True)

    def scan(self):
        result = subprocess.run([sys.executable, '-B', str(self.root / 'Library Tools/Internal/scan_library.py'), '--inventory-only', '--verify'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads((self.layout.reports / 'catalogue.json').read_bytes())


class IntakeTests(IntakeFixture):
    def test_open_is_read_only_and_migration_preserves_all_legacy_cells(self):
        raw = b'\xef\xbb\xbfrelative_path,title,author,personal_note\r\nIncoming Files/arrival.txt,Manual,Writer,keep me\r\nContent/missing.txt,Lost,,recover me\r\n\r\n'
        self.layout.corrections.write_bytes(raw)
        before = self.path.read_bytes()
        self.assertFalse(self.flow.records.exists)
        self.assertFalse(self.flow.reload())
        r = self.initialize()
        self.assertEqual(self.flow.records.corrections(r)['title'], 'Manual')
        self.assertEqual(r['legacy_rows'][0]['personal_note'], 'keep me')
        self.assertEqual(self.flow.records.data['unmatched_corrections'][0]['row']['title'], 'Lost')
        self.assertEqual(self.layout.corrections.read_bytes(), raw)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue((self.layout.reports / 'library_records.csv').exists())
        self.assertIn('recover me', (self.layout.reports / 'library_records.json').read_text(encoding='utf-8'))

    def test_repeated_legacy_rows_are_held_instead_of_last_wins(self):
        self.layout.corrections.write_text('relative_path,title\nIncoming Files/arrival.txt,First\nIncoming Files/arrival.txt,Second\n', encoding='utf-8')
        r = self.initialize()
        self.assertEqual(self.flow.records.corrections(r), {})
        rows = self.flow.records.data['unmatched_corrections']
        self.assertEqual(len(rows), 2)
        with self.flow.records.lock():
            self.flow.records.recover_correction(rows[0]['id'], r['id'])
            self.flow.records.save()
        self.assertEqual(self.flow.records.corrections(r)['title'], 'First')
        self.assertEqual(len(self.flow.records.data['unmatched_corrections']), 1)

    def test_complete_flow_real_audit_review_preview_file_catalogue_and_undo(self):
        r = self.initialize()
        result = subprocess.run([sys.executable, '-B', str(self.root / 'Library Tools/Internal/audit_materials.py'), '--no-clamav'], capture_output=True, text=True,
                                env={**os.environ, 'PATH': '/usr/bin:/bin'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.flow.reload()
        r = self.flow.records.get(r['id'])
        self.assertEqual(self.flow.next_step(r), 'Review findings and metadata')
        self.assertTrue(self.flow.categories(r)['Incomplete checks'])
        original = self.path.read_bytes()
        self.approve(r)
        self.assertTrue(self.path.exists())  # Saving review never renames.
        plan = self.flow.preview([r['id']])
        self.assertTrue(self.path.exists())  # Preview never renames.
        journal = self.flow.apply(plan, 'FILE 1')
        target = self.root / plan['moves'][0]['destination']
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(self.path.exists())
        rows = self.scan()
        self.assertEqual((rows[0]['title'], rows[0]['author'], rows[0]['record_id']), ('Reviewed', 'Writer', r['id']))
        self.assertIn('record_id', (self.layout.reports / 'catalogue.csv').read_text(encoding='utf-8'))
        self.assertIn('Reviewed', (self.layout.reports / 'catalogue.txt').read_text(encoding='utf-8'))
        # A later correction survives undo; undo restores paths only.
        self.flow = Intake(self.layout)
        with self.flow.records.lock():
            self.flow.records.set_metadata(r['id'], {'title': 'Later correction'})
            self.flow.records.save()
        self.flow.rollback(journal, confirmation='UNDO 1')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.flow.records.corrections(self.flow.records.get(r['id']))['title'], 'Later correction')

    def test_unique_manual_rename_folder_move_mount_change_keep_identity_and_audit(self):
        r = self.initialize()
        self.audit()
        r = self.flow.records.get(r['id'])
        self.approve(r)
        moved = self.root / 'Content/Elsewhere/Longer title.txt'
        moved.parent.mkdir()
        self.path.rename(moved)
        self.flow.reload()
        r = self.flow.records.get(r['id'])
        self.assertEqual(r['path'], 'Content/Elsewhere/Longer title.txt')
        self.assertEqual(self.flow.records.corrections(r)['title'], 'Reviewed')
        self.assertIsNotNone(self.flow.records.current_audit(r))
        changed_mount = self.root.with_name('Different mount path')
        self.root.rename(changed_mount)
        self.root = changed_mount
        self.layout = LibraryPaths(self.root, True)
        self.flow = Intake(self.layout)
        self.flow.reload()
        self.assertEqual(self.flow.records.get(r['id'])['path'], 'Content/Elsewhere/Longer title.txt')
        self.assertEqual(self.scan()[0]['title'], 'Reviewed')

    def test_identical_copies_have_distinct_corrections_and_ambiguous_moves_need_choice(self):
        copy_path = self.root / 'Incoming Files/copy.txt'
        copy_path.write_bytes(self.path.read_bytes())
        r = self.initialize()
        self.audit()
        r = self.flow.records.get(r['id'])
        other = self.flow.records.by_path('Incoming Files/copy.txt')
        self.approve(r)
        self.flow.review(other['id'], 'Other copy', 'Other writer', 'Other.txt', 'Content', True)
        self.assertNotEqual(r['id'], other['id'])
        self.assertTrue(self.flow.categories(r)['Duplicates'])
        self.path.rename(self.root / 'Incoming Files/new-a.txt')
        copy_path.rename(self.root / 'Incoming Files/new-b.txt')
        self.flow.reload()
        ambiguities = self.flow.records.data['ambiguities']
        self.assertEqual(len(ambiguities), 2)
        placeholder = self.flow.records.get(ambiguities[0]['id'])
        self.assertEqual(self.flow.records.corrections(placeholder), {})
        with self.assertRaisesRegex(RecordError, 'identity'):
            self.approve(placeholder)
        self.flow.resolve(placeholder['id'], other['id'])
        linked = self.flow.records.get(other['id'])
        self.assertEqual(self.flow.records.corrections(linked)['title'], 'Other copy')
        self.flow.reload()
        # The remaining duplicate still needs explicit mapping, not an automatic guess.
        self.assertEqual(len(self.flow.records.data['ambiguities']), 1)
        self.flow.resolve(self.flow.records.data['ambiguities'][0]['id'], r['id'])
        self.assertEqual(self.flow.records.corrections(self.flow.records.get(r['id']))['title'], 'Reviewed')

    def test_new_identical_copy_does_not_inherit_corrections(self):
        r = self.initialize()
        with self.flow.records.lock():
            self.flow.records.set_metadata(r['id'], {'title': 'Mine'})
            self.flow.records.save()
        (self.root / 'Incoming Files/new copy.txt').write_bytes(self.path.read_bytes())
        self.flow.reload()
        new = self.flow.records.by_path('Incoming Files/new copy.txt')
        self.assertNotEqual(new['id'], r['id'])
        self.assertEqual(self.flow.records.corrections(new), {})

    def test_resolving_ambiguous_copy_preserves_its_latest_audit_findings(self):
        other = self.root / 'Incoming Files/copy.txt'
        other.write_bytes(self.path.read_bytes())
        r = self.initialize()
        self.audit()
        self.path.rename(self.root / 'Incoming Files/new-a.txt')
        other.rename(self.root / 'Incoming Files/new-b.txt')
        self.flow.reload()
        self.audit()
        source = self.layout.reports / 'intake_audit.json'
        data = json.loads(source.read_bytes())
        data['scanned_at'] = 'Latest flagged audit'
        data['files'][0]['clamav'] = 'DETECTION'
        source.write_text(json.dumps(data), encoding='utf-8')
        self.flow.reload()
        placeholder = self.flow.records.by_path(data['files'][0]['relative_path'])
        self.flow.resolve(placeholder['id'], r['id'])
        linked = self.flow.records.get(r['id'])
        self.assertEqual(self.flow.records.current_audit(linked)['scanned_at'], 'Latest flagged audit')
        self.assertTrue(self.flow.categories(linked)['Security indicators'])

    def test_exchanged_filenames_follow_unique_bytes_not_the_occupied_path(self):
        other = self.root / 'Incoming Files/other.txt'
        other.write_bytes(b'A different book')
        r = self.initialize()
        with self.flow.records.lock():
            self.flow.records.set_metadata(r['id'], {'title': 'First identity'})
            self.flow.records.save()
        temporary = self.root / 'Incoming Files/swap.tmp'
        self.path.rename(temporary)
        other.rename(self.path)
        temporary.rename(other)
        self.flow.reload()
        same = self.flow.records.get(r['id'])
        self.assertEqual(same['path'], 'Incoming Files/other.txt')
        self.assertEqual(self.flow.records.corrections(same)['title'], 'First identity')

    def test_changed_bytes_same_size_same_timestamp_stale_audit_and_hold_correction(self):
        r = self.initialize()
        self.audit()
        r = self.flow.records.get(r['id'])
        self.approve(r)
        info = self.path.stat()
        self.path.write_bytes(b'X' * info.st_size)
        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.flow.reload()
        r = self.flow.records.get(r['id'])
        self.assertIsNone(self.flow.records.current_audit(r))
        self.assertIn('outdated', self.flow.next_step(r))
        self.assertEqual(self.flow.records.corrections(r), {})
        self.assertEqual(r['corrections']['title'], 'Reviewed')
        self.assertEqual(len(r['versions']), 1)
        with self.assertRaises(RecordError):
            self.flow.preview([r['id']])

    def test_missing_file_and_orphan_row_remain_exported(self):
        self.layout.corrections.write_text('relative_path,title\nIncoming Files/arrival.txt,Keep me\nContent/lost.txt,Unmatched\n', encoding='utf-8')
        r = self.initialize()
        self.path.unlink()
        self.flow.reload()
        r = self.flow.records.get(r['id'])
        self.assertEqual(r['state'], 'missing')
        self.assertEqual(r['corrections']['title'], 'Keep me')
        self.assertEqual(len(self.flow.records.data['unmatched_corrections']), 1)
        exported = (self.layout.reports / 'library_records.json').read_text(encoding='utf-8')
        self.assertIn('Keep me', exported)
        self.assertIn('Unmatched', exported)

    def test_findings_separate_format_security_incomplete_metadata_and_duplicates(self):
        r = self.initialize()
        self.audit(indicators=True, incomplete=True)
        r = self.flow.records.get(r['id'])
        groups = self.flow.categories(r)
        self.assertTrue(groups['File/format'])
        self.assertTrue(groups['Security indicators'])
        self.assertTrue(groups['Incomplete checks'])
        self.assertTrue(groups['Metadata'])
        self.assertFalse(groups['Duplicates'])
        self.assertNotIn('active/embedded', ' '.join(groups['File/format']))

    def test_failed_tools_are_incomplete_checks_rather_than_format_errors(self):
        r = self.initialize()
        self.audit()
        source = self.layout.reports / 'intake_audit.json'
        data = json.loads(source.read_bytes())
        data['files'][0]['clamav'] = 'ERROR'
        data['files'][0]['review_reasons'] = 'PDFiD returned incomplete indicator output | ClamAV returned an error for this file | EPUBCheck failed to run'
        source.write_text(json.dumps(data), encoding='utf-8')
        self.flow.reload()
        groups = self.flow.categories(self.flow.records.get(r['id']))
        self.assertFalse(groups['File/format'])
        self.assertEqual(len(groups['Incomplete checks']), 4)

    def test_preview_requires_acknowledgement_and_current_audit(self):
        r = self.initialize()
        with self.assertRaisesRegex(RecordError, 'Check Incoming'):
            self.approve(r)
        self.audit()
        with self.assertRaisesRegex(RecordError, 'acknowledge'):
            self.flow.review(r['id'], 'T', 'A', 'T.txt', 'Content', False)

    def test_collision_case_unicode_extension_traversal_and_long_name_protection(self):
        r = self.initialize()
        self.audit()
        r = self.flow.records.get(r['id'])
        for name, folder in [('Wrong.epub', 'Content'), ('okay.txt', '../escape'), ('okay.txt', 'Library Tools'), ('x' * 245 + '.txt', 'Content')]:
            with self.assertRaises(RecordError):
                self.approve(r, name, folder)
        for name in ['CON.txt', '.hidden.txt', 'trail .', 'bad/name.txt']:
            with self.assertRaises(RecordError):
                filename(name)
        (self.root / 'Content/TARGET.txt').write_bytes(b'Occupied')
        self.approve(r, 'target.txt', 'Content')
        with self.assertRaisesRegex(RecordError, 'collision'):
            self.flow.preview([r['id']])
        self.assertEqual(self.path.read_bytes(), b'Synthetic arrival only.')

    def test_stale_preview_and_wrong_confirmation_never_move_file(self):
        r = self.initialize()
        self.audit()
        r = self.flow.records.get(r['id'])
        self.approve(r)
        plan = self.flow.preview([r['id']])
        with self.assertRaises(RecordError):
            self.flow.apply(plan, 'YES')
        self.approve(r, 'Changed.txt')
        with self.assertRaisesRegex(RecordError, 'fresh preview'):
            self.flow.apply(plan, 'FILE 1')
        self.assertTrue(self.path.exists())

    def test_content_changed_after_preview_stops_apply_even_with_preserved_stats(self):
        r = self.initialize()
        self.audit()
        r = self.flow.records.get(r['id'])
        self.approve(r)
        plan = self.flow.preview([r['id']])
        self.path.write_bytes(b'Changed now')
        with self.assertRaisesRegex(RecordError, 'changed'):
            self.flow.apply(plan, 'FILE 1')
        self.assertEqual(self.path.read_bytes(), b'Changed now')

    def test_interruption_after_move_before_journal_recovered_on_fresh_process(self):
        r = self.initialize()
        self.audit()
        r = self.flow.records.get(r['id'])
        self.approve(r)
        plan = self.flow.preview([r['id']])
        def interrupt(index, phase):
            raise KeyboardInterrupt('Synthetic power interruption')
        with self.assertRaises(KeyboardInterrupt):
            self.flow.apply(plan, 'FILE 1', checkpoint=interrupt)
        self.flow = Intake(self.layout)
        self.assertEqual(len(self.flow.pending()), 1)
        with self.assertRaisesRegex(RecordError, 'recovery'):
            self.flow.reload()
        path, _ = self.flow.pending()[0]
        self.flow.rollback(path, confirmation='UNDO 1')
        self.assertEqual(self.path.read_bytes(), b'Synthetic arrival only.')
        self.assertEqual(self.flow.records.corrections(self.flow.records.get(r['id']))['title'], 'Reviewed')
        self.assertEqual(self.flow.pending(), [])

    def test_interruption_after_ledger_commit_restores_paths_preserving_values(self):
        r = self.initialize()
        self.audit()
        r = self.flow.records.get(r['id'])
        self.approve(r)
        plan = self.flow.preview([r['id']])
        def interrupt(index, phase):
            if phase == 'ledger_saved':
                raise RuntimeError('Synthetic report-write failure')
        with self.assertRaises(RuntimeError):
            self.flow.apply(plan, 'FILE 1', checkpoint=interrupt)
        self.flow = Intake(self.layout)
        path, _ = self.flow.pending()[0]
        self.flow.rollback(path, confirmation='UNDO 1')
        self.assertTrue(self.path.exists())
        self.assertEqual(self.flow.records.get(r['id'])['path'], 'Incoming Files/arrival.txt')

    def test_external_content_change_during_filing_does_not_commit_old_fingerprint(self):
        r = self.initialize()
        self.audit()
        r = self.flow.records.get(r['id'])
        self.approve(r)
        plan = self.flow.preview([r['id']])
        def modify(index, phase):
            (self.root / plan['moves'][0]['destination']).write_bytes(b'External edit during filing')
        with self.assertRaisesRegex(RecordError, 'changed during filing'):
            self.flow.apply(plan, 'FILE 1', checkpoint=modify)
        persisted = Records(self.layout).get(r['id'])
        self.assertEqual(persisted['path'], 'Incoming Files/arrival.txt')
        self.assertTrue(self.flow.pending())

    def test_recovery_refuses_changed_or_occupied_files_without_overwrite(self):
        r = self.initialize()
        self.audit()
        r = self.flow.records.get(r['id'])
        self.approve(r)
        plan = self.flow.preview([r['id']])
        journal = self.flow.apply(plan, 'FILE 1')
        self.path.write_bytes(b'Do not overwrite this')
        with self.assertRaisesRegex(RecordError, 'occupied'):
            self.flow.rollback(journal, confirmation='UNDO 1')
        self.assertEqual(self.path.read_bytes(), b'Do not overwrite this')

    def test_recovery_clears_only_own_exfat_reservation(self):
        r = self.initialize()
        self.audit()
        r = self.flow.records.get(r['id'])
        self.approve(r)
        plan = self.flow.preview([r['id']])
        move = plan['moves'][0]
        target = self.root / move['destination']
        target.parent.mkdir()
        target.write_bytes(reservation(move['token']))
        journal = self.flow.history / 'interrupted.json'
        atomic(journal, encoded({'schema': 1, 'status': 'applying', 'plan': plan}))
        self.flow.rollback(journal, confirmation='UNDO 1')
        self.assertFalse(target.exists())
        self.assertTrue(self.path.exists())

    def test_mac_hidden_metadata_companions_are_not_parsed_as_journals(self):
        self.initialize()
        self.flow.history.mkdir(parents=True)
        (self.flow.history / '._not-a-journal.json').write_bytes(b'AppleDouble binary fixture')
        self.assertEqual(self.flow.pending(), [])

    def test_review_editor_writes_same_record_and_scanner_follows_later_move(self):
        library = self.root / 'Content/library.txt'
        library.write_bytes(b'Catalogue fixture')
        self.initialize()
        rows = self.scan()
        identity = rows[0]['record_id']
        store = ReviewStore(self.layout)
        store.save('Content/library.txt', {'title': 'From editor', 'author': 'Editor writer'})
        library.rename(self.root / 'Content/renamed library.txt')
        rows = self.scan()
        self.assertEqual(rows[0]['record_id'], identity)
        self.assertEqual(rows[0]['title'], 'From editor')

    def test_unrelated_legacy_csv_edit_is_retained_for_explicit_import(self):
        r = self.initialize()
        self.layout.corrections.write_text('relative_path,title\nIncoming Files/arrival.txt,New CSV title\n', encoding='utf-8')
        self.flow.reload()
        self.assertTrue(self.flow.legacy_changed())
        self.assertEqual(self.flow.records.corrections(self.flow.records.get(r['id'])), {})
        self.flow.reload(import_legacy=True)
        self.assertEqual(self.flow.records.corrections(self.flow.records.get(r['id']))['title'], 'New CSV title')

    def test_concurrent_record_writer_rejected_and_symlink_destination_rejected(self):
        r = self.initialize()
        stale = Records(self.layout)
        self.flow.reload()
        with self.assertRaisesRegex(RecordError, 'changed'):
            with stale.lock():
                stale.save()
        if hasattr(os, 'symlink'):
            try:
                (self.root / 'Content/link').symlink_to(self.root / 'Incoming Files', target_is_directory=True)
            except OSError:
                self.skipTest('Symlinks unavailable')
            with self.assertRaisesRegex(RecordError, 'links'):
                self.flow.destination('Content/link', 'file.txt')

    def test_successful_checks_are_not_misreported_as_incomplete(self):
        r = self.initialize()
        self.audit()
        r = self.flow.records.get(r['id'])
        self.assertFalse(self.flow.categories(r)['Incomplete checks'])

    def test_new_audit_same_bytes_invalidates_previous_filing_approval(self):
        r = self.initialize()
        self.audit()
        r = self.flow.records.get(r['id'])
        self.approve(r)
        source = self.layout.reports / 'intake_audit.json'
        data = json.loads(source.read_bytes())
        data['scanned_at'] = 'Later audit'
        data['files'][0]['clamav'] = 'DETECTION'
        source.write_text(json.dumps(data), encoding='utf-8')
        self.flow.reload()
        r = self.flow.records.get(r['id'])
        self.assertEqual(self.flow.next_step(r), 'Review findings and metadata')
        self.assertTrue(self.flow.categories(r)['Security indicators'])

    def test_audit_replaced_after_preview_stops_filing_until_latest_findings_loaded(self):
        r = self.initialize()
        self.audit()
        r = self.flow.records.get(r['id'])
        self.approve(r)
        plan = self.flow.preview([r['id']])
        path = self.layout.reports / 'intake_audit.json'
        data = json.loads(path.read_bytes())
        data['files'][0]['clamav'] = 'DETECTION'
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(RecordError, 'audit changed'):
            self.flow.apply(plan, 'FILE 1')
        self.assertTrue(self.path.exists())

    def test_private_filename_choices_migrate_separately_from_catalogue_corrections(self):
        self.layout.corrections.write_text('relative_path,title,author\nIncoming Files/arrival.txt,Catalogue title,Catalogue author\n', encoding='utf-8')
        history = self.root / 'Library Tools/Personal Tools/Filename Cleanup/History'
        history.mkdir(parents=True)
        choice = {'path': 'Incoming Files/arrival.txt', 'sha256': proof(self.path)['sha256'],
                  'title': 'Filename title', 'author': 'Filename author', 'unknown_private_field': 'retain'}
        data = {'version': 1, 'choices': {'private-identity': choice,
                'missing-choice': {**choice, 'path': 'Content/missing', 'sha256': 'a' * 64}}}
        original = json.dumps(data).encode()
        source = history / 'filename_choices.json'
        source.write_bytes(original)
        r = self.initialize()
        self.assertEqual(self.flow.records.corrections(r)['title'], 'Catalogue title')
        self.assertEqual(r['filename_choice']['title'], 'Filename title')
        self.assertEqual(r['filename_choice']['unknown_private_field'], 'retain')
        self.assertEqual(self.flow.suggestions(r)[0]['title'], 'Catalogue title')
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(len(self.flow.records.data['unmatched_corrections']), 1)

    def test_confirmed_legacy_mapping_keeps_identical_copies_distinct(self):
        other = self.root / 'Incoming Files/copy.txt'
        other.write_bytes(self.path.read_bytes())
        r = self.initialize()
        self.audit()
        first = self.flow.records.get(r['id'])
        second = self.flow.records.by_path('Incoming Files/copy.txt')
        self.approve(first, 'First.txt', 'Content')
        self.flow.review(second['id'], 'Second correction', '', 'Second.txt', 'Content', True)
        mapping = {'Incoming Files/arrival.txt': 'Incoming Files/new-first.txt',
                   'Incoming Files/copy.txt': 'Incoming Files/new-second.txt'}
        for old, new in mapping.items():
            (self.root / old).rename(self.root / new)
        data = relocation_bytes(self.layout, mapping)
        atomic(self.layout.records, data)
        self.flow = Intake(self.layout)
        self.flow.reload()
        self.assertEqual(self.flow.records.get(first['id'])['path'], 'Incoming Files/new-first.txt')
        self.assertEqual(self.flow.records.corrections(self.flow.records.get(first['id']))['title'], 'Reviewed')
        self.assertEqual(self.flow.records.corrections(self.flow.records.get(second['id']))['title'], 'Second correction')
        self.assertFalse(self.flow.records.data['ambiguities'])

    def test_unicode_normalization_after_filing_does_not_mix_identical_copies(self):
        second_path = self.root / 'Incoming Files/copy.txt'
        second_path.write_bytes(self.path.read_bytes())
        r = self.initialize()
        self.audit()
        first = self.flow.records.get(r['id'])
        second = self.flow.records.by_path('Incoming Files/copy.txt')
        self.approve(first, 'Café first.txt', 'Content')
        self.flow.review(second['id'], 'Separate café copy', '', 'Café second.txt', 'Content', True)
        self.flow.apply(self.flow.preview([first['id'], second['id']]), 'FILE 2')
        for path in list((self.root / 'Content').iterdir()):
            path.rename(path.with_name(unicodedata.normalize('NFD', path.name)))
        self.flow.reload()
        self.assertFalse(self.flow.records.data['ambiguities'])
        self.assertEqual(self.flow.records.corrections(self.flow.records.get(first['id']))['title'], 'Reviewed')
        self.assertEqual(self.flow.records.corrections(self.flow.records.get(second['id']))['title'], 'Separate café copy')

    def test_process_exit_leaves_lock_and_journal_recoverable_after_mount_move(self):
        r = self.initialize()
        self.audit()
        r = self.flow.records.get(r['id'])
        self.approve(r)
        plan = self.flow.preview([r['id']])
        script = ('import json,os,sys; from pathlib import Path; '
                  'from library_paths import LibraryPaths; from intake_workflow import Intake; '
                  'f=Intake(LibraryPaths(Path(sys.argv[1]),True)); '
                  'f.apply(json.loads(sys.argv[2]),"FILE 1",checkpoint=lambda *args: os._exit(73))')
        child = subprocess.run([sys.executable, '-B', '-c', script, str(self.root), json.dumps(plan)], capture_output=True, text=True)
        self.assertEqual(child.returncode, 73, child.stderr)
        moved_root = self.root.with_name('Reconnected drive')
        self.root.rename(moved_root)
        self.root, self.layout = moved_root, LibraryPaths(moved_root, True)
        self.path = self.root / 'Incoming Files/arrival.txt'
        self.flow = Intake(self.layout)
        self.assertTrue(self.flow.records.lock_path.exists())
        self.flow.records.release_interrupted_lock()
        journal, _ = self.flow.pending()[0]
        self.flow.rollback(journal, confirmation='UNDO 1')
        self.assertTrue(self.path.exists())
        self.assertEqual(self.flow.records.corrections(self.flow.records.get(r['id']))['title'], 'Reviewed')

    def test_multiple_identical_copies_file_with_separate_values_and_partial_recovery(self):
        second_path = self.root / 'Incoming Files/second.txt'
        second_path.write_bytes(self.path.read_bytes())
        r = self.initialize()
        self.audit()
        first = self.flow.records.get(r['id'])
        second = self.flow.records.by_path('Incoming Files/second.txt')
        self.approve(first, 'One.txt', 'Content')
        self.flow.review(second['id'], 'Two', 'Second author', 'Two.txt', 'Content', True)
        plan = self.flow.preview([first['id'], second['id']])
        def fail(index, phase):
            if index == 0:
                raise RuntimeError('Interrupted after first of two')
        with self.assertRaises(RuntimeError):
            self.flow.apply(plan, 'FILE 2', checkpoint=fail)
        flow = Intake(self.layout)
        journal, _ = flow.pending()[0]
        flow.rollback(journal, confirmation='UNDO 2')
        self.assertTrue(self.path.exists())
        self.assertTrue(second_path.exists())
        flow.apply(flow.preview([first['id'], second['id']]), 'FILE 2')
        rows = self.scan()
        self.assertEqual({x['record_id']: x['title'] for x in rows}, {first['id']: 'Reviewed', second['id']: 'Two'})


class IntakeWidgetTests(IntakeFixture):
    """Run one real Tk path without inheriting the model test methods twice."""
    def test_tk_buttons_save_prefilled_review_preview_and_confirm_filing(self):
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(str(exc))
        self.addCleanup(root.destroy)
        root.withdraw()
        self.initialize()
        self.audit(incomplete=True)
        from intake_ui import IntakeWindow
        window = IntakeWindow(root, self.layout, run_audit=lambda: None,
                              on_close=lambda: None, is_busy=lambda: False, on_filed=lambda: None)
        self.addCleanup(lambda: window.top.destroy() if window.top.winfo_exists() else None)
        window.render()
        root.update()
        window.select()
        self.assertEqual(window.title.get(), 'arrival')
        self.assertEqual(window.name.get(), 'arrival.txt')
        window.title.set('Widget reviewed')
        window.author.set('Writer')
        window.name_button.invoke()
        window.ack.set(True)
        window.save_button.invoke()
        self.wait_job(root, window)
        self.assertTrue(self.path.exists())
        self.assertEqual(window.flow.records.corrections(window.flow.records.get(window.selected))['title'], 'Widget reviewed')
        self.assertIn('Incomplete checks', window.details.get('1.0', 'end'))
        with patch('tkinter.simpledialog.askstring', return_value='FILE 1'):
            window.preview_button.invoke()
            self.wait_job(root, window)
            from tkinter import ttk
            preview = next(w for w in window.top.winfo_children() if isinstance(w, tk.Toplevel))
            confirm = next(w for w in preview.winfo_children() if isinstance(w, ttk.Button) and 'Confirm filing' in w.cget('text'))
            confirm.invoke()
            self.wait_job(root, window)
        self.assertFalse(self.path.exists())
        self.assertTrue((self.root / 'Content/Widget reviewed — Writer.txt').exists())

    @staticmethod
    def wait_job(root, window):
        deadline = time.monotonic() + 20
        while window.job and time.monotonic() < deadline:
            root.update()
            time.sleep(.02)
        if window.job:
            raise AssertionError('Intake worker did not finish')
        root.update()


if __name__ == '__main__':
    unittest.main()
