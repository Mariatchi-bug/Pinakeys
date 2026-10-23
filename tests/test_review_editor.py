"""Review and correction tests use disposable synthetic libraries only."""
import csv
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Event
import time
import unicodedata
import unittest
from unittest.mock import patch
import zipfile

from build_usb import build_folder, source_file
from run_tests import settle_window
from library_paths import LibraryPaths
from pinakeys import ActionRunner, PinakeysWindow
from review_library import ReviewError, ReviewStore, path_key

try:
    import tkinter as tk
except ImportError:
    tk = None


def epub(path, title='', author=''):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        archive.writestr('book.opf', '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                         f'<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator></metadata></package>')


def scan(root):
    result = subprocess.run([sys.executable, '-B', str(root / 'Library Tools/Internal/scan_library.py'), '--folder', 'Content'],
                            capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pinakeys-review-model-')
        self.addCleanup(self.temp.cleanup)
        self.root = build_folder(Path(self.temp.name).resolve() / 'USB with spaces')
        self.layout = LibraryPaths(self.root, True)
        self.document = self.root / 'Content/Café sample.epub'
        epub(self.document, 'Original title', 'Original author')
        self.other = self.root / 'Content/other.epub'
        epub(self.other, 'Other title', 'Other author')
        scan(self.root)
        self.rows = json.loads((self.layout.reports / 'catalogue.json').read_text(encoding='utf-8'))
        self.relative = next(row['relative_path'] for row in self.rows if 'sample' in row['filename'])
        self.original = self.document.read_bytes()

    def write_corrections(self):
        stream = StringIO(newline='')
        writer = csv.writer(stream, lineterminator='\n')
        writer.writerow(['relative_path', 'title', 'author', 'date', 'identifier', 'personal_note'])
        writer.writerow([self.relative, 'Earlier title', '', '1999', 'isbn:kept', 'keep this column'])
        writer.writerow(['Content/other.epub', 'Other override', 'Other author', '', '', 'untouched'])
        self.layout.corrections.write_bytes(b'\xef\xbb\xbf' + stream.getvalue().encode())

    def test_opening_and_filtering_are_read_only(self):
        self.layout.corrections.unlink()
        reports = {p.name: p.read_bytes() for p in self.layout.reports.iterdir()}
        store = ReviewStore(self.layout)
        self.assertEqual(len(store.filtered('CAFÉ', False)), 1)
        self.assertEqual(len(store.filtered('cafe\u0301', False)), 1)
        self.assertEqual(store.filtered('', True), [])
        self.assertFalse(self.layout.corrections.exists())
        self.assertFalse((self.layout.corrections.parent / 'Backups').exists())
        self.assertEqual({p.name: p.read_bytes() for p in self.layout.reports.iterdir()}, reports)
        self.assertEqual(self.document.read_bytes(), self.original)

    def test_preparing_trash_reads_the_file_without_changing_materials_or_corrections(self):
        store = ReviewStore(self.layout)
        before = self.layout.corrections.read_bytes()
        plan = store.prepare_trash(self.relative)
        self.assertEqual(plan.target.digest, hashlib.sha256(self.original).hexdigest())
        self.assertFalse(plan.flagged_duplicate)
        self.assertEqual(plan.duplicates, ())
        self.assertEqual(self.document.read_bytes(), self.original)
        self.assertEqual(self.layout.corrections.read_bytes(), before)

    def test_confirmed_duplicate_moves_only_selected_file_and_restores_with_its_corrections(self):
        self.other.write_bytes(self.original)
        self.write_corrections()
        scan(self.root)
        store = ReviewStore(self.layout)
        corrections = self.layout.corrections.read_bytes()
        catalogue = store.catalogue_bytes
        plan = store.prepare_trash(self.relative)
        self.assertEqual([copy.relative_path for copy in plan.duplicates], ['Content/other.epub'])
        destination = Path(self.temp.name)/'Synthetic Trash.epub'
        store.trash(plan, lambda path: path.rename(destination))
        self.assertFalse(self.document.exists())
        self.assertEqual(self.other.read_bytes(), self.original)
        self.assertEqual(destination.read_bytes(), self.original)
        self.assertEqual(self.layout.corrections.read_bytes(), corrections)
        self.assertEqual(store.catalogue.read_bytes(), catalogue)
        scan(self.root)
        rows = json.loads(store.catalogue.read_text(encoding='utf-8'))
        self.assertEqual(len(rows), 1)
        self.assertNotIn('Exact duplicate', rows[0]['review_flags'])
        destination.rename(self.document)
        scan(self.root)
        restored = ReviewStore(self.layout)
        self.assertEqual(restored.prefill(self.relative)[0]['title'], 'Earlier title')
        self.assertEqual(self.layout.corrections.read_bytes(), corrections)

    def test_duplicate_warning_with_no_current_identical_copy_refuses_trash(self):
        store = ReviewStore(self.layout)
        store.by_path[path_key(self.relative)]['review_flags'] = 'Exact duplicate file'
        with self.assertRaisesRegex(ReviewError, 'Another identical copy'):
            store.prepare_trash(self.relative)
        self.assertEqual(self.document.read_bytes(), self.original)

    def test_changed_or_missing_retained_duplicate_cannot_remove_the_last_verified_copy(self):
        for change in ('changed', 'missing'):
            with self.subTest(change=change):
                self.other.write_bytes(self.original)
                scan(self.root)
                store = ReviewStore(self.layout)
                plan = store.prepare_trash(self.relative)
                if change == 'missing':
                    self.other.unlink()
                else:
                    self.other.write_bytes(b'No longer the same file')
                with patch('pinakeys.trash_local') as move:
                    with self.assertRaisesRegex(ReviewError, 'other identical copies'):
                        store.trash(plan, move)
                    move.assert_not_called()
                self.assertEqual(self.document.read_bytes(), self.original)

    def test_changed_selected_bytes_are_refused_even_with_same_size_and_restored_mtime(self):
        store = ReviewStore(self.layout)
        plan = store.prepare_trash(self.relative)
        info = self.document.stat()
        self.document.write_bytes(b'X' + self.original[1:])
        os.utime(self.document, ns=(info.st_atime_ns, info.st_mtime_ns))
        with patch('pinakeys.trash_local') as move:
            with self.assertRaisesRegex(ReviewError, 'changed after confirmation'):
                store.trash(plan, move)
            move.assert_not_called()

    def test_changed_catalogue_and_replaced_symlink_are_refused_before_trash(self):
        store = ReviewStore(self.layout)
        plan = store.prepare_trash(self.relative)
        store.catalogue.write_bytes(store.catalogue_bytes + b'\n')
        with patch('pinakeys.trash_local') as move:
            with self.assertRaisesRegex(ReviewError, 'catalogue changed'):
                store.trash(plan, move)
            move.assert_not_called()
        if os.name != 'nt':
            store.catalogue.write_bytes(store.catalogue_bytes)
            self.document.unlink()
            self.document.symlink_to(self.other)
            with patch('pinakeys.trash_local') as move:
                with self.assertRaisesRegex(ReviewError, 'symlink'):
                    store.trash(plan, move)
                move.assert_not_called()

    def test_prefill_prefers_saved_corrections_then_current_metadata_without_writing(self):
        self.write_corrections()
        before = self.layout.corrections.read_bytes()
        store = ReviewStore(self.layout)
        values, sources = store.prefill(self.relative)
        self.assertEqual(values, {'title': 'Earlier title', 'author': 'Original author'})
        self.assertEqual(sources, {'title': 'Saved correction', 'author': 'Embedded metadata'})
        self.assertEqual(self.layout.corrections.read_bytes(), before)
        self.assertFalse((self.layout.corrections.parent / 'Backups').exists())

    def test_prefill_uses_current_catalogue_and_embedded_fallback_before_filename(self):
        store = ReviewStore(self.layout)
        row = store.by_path[path_key(self.relative)]
        row.update(title='Current title', author='', embedded_author='Embedded author')
        values, sources = store.prefill(self.relative)
        self.assertEqual(values, {'title': 'Current title', 'author': 'Embedded author'})
        self.assertEqual(sources, {'title': 'Current catalogue', 'author': 'Embedded metadata'})

    def test_filename_suggestions_split_only_the_spaced_em_dash_convention(self):
        store = ReviewStore(self.layout)
        row = store.by_path[path_key(self.relative)]
        row.update(title='', author='', embedded_title='', embedded_author='')
        cases = [
            ('FEMA_REP_Program_Manual_Dec_2019 1.pdf', 'FEMA REP Program Manual Dec 2019 1', ''),
            ('Designing_Gardens — Jane_Doe.epub', 'Designing Gardens', 'Jane Doe'),
            ('Title — A subtitle — É. Author.pdf', 'Title — A subtitle', 'É. Author'),
            ('Title - A subtitle.epub', 'Title - A subtitle', ''),
            ('Title — .pdf', 'Title —', ''),
        ]
        for filename, title, author in cases:
            with self.subTest(filename=filename):
                row['filename'] = filename
                values, sources = store.prefill(self.relative)
                self.assertEqual(values, {'title': title, 'author': author})
                self.assertIn('Suggested from filename', sources['title'])
                self.assertIn('Suggested from filename' if author else 'No author available', sources['author'])

    def test_save_preserves_other_fields_rows_bom_and_exact_backup(self):
        self.write_corrections()
        original_csv = self.layout.corrections.read_bytes()
        catalogue = (self.layout.reports / 'catalogue.json').read_bytes()
        store = ReviewStore(self.layout)
        backup = store.save(self.relative, {'title': 'A title, with commas', 'author': 'M. García'})
        self.assertEqual(backup.read_bytes(), original_csv)
        saved = self.layout.corrections.read_bytes()
        self.assertTrue(saved.startswith(b'\xef\xbb\xbf'))
        self.assertNotIn(b'\r\n', saved)
        values = list(csv.DictReader(StringIO(saved.decode('utf-8-sig'))))
        self.assertEqual(values[0]['title'], 'A title, with commas')
        self.assertEqual(values[0]['author'], 'M. García')
        self.assertEqual((values[0]['date'], values[0]['identifier'], values[0]['personal_note']), ('1999', 'isbn:kept', 'keep this column'))
        self.assertEqual(values[1]['title'], 'Other override')
        self.assertEqual(values[1]['personal_note'], 'untouched')
        self.assertEqual((self.layout.reports / 'catalogue.json').read_bytes(), catalogue)
        self.assertEqual(self.document.read_bytes(), self.original)

    def test_clearing_overrides_preserves_date_identifier_and_restores_embedded_values(self):
        self.write_corrections()
        store = ReviewStore(self.layout)
        store.save(self.relative, {'title': '', 'author': ''})
        scan(self.root)
        rows = json.loads((self.layout.reports / 'catalogue.json').read_text(encoding='utf-8'))
        row = next(row for row in rows if path_key(row['relative_path']) == path_key(self.relative))
        self.assertEqual((row['title'], row['author']), ('Original title', 'Original author'))
        self.assertEqual((row['date'], row['identifier']), ('1999', 'isbn:kept'))

    def test_noop_preserves_csv_bytes_and_does_not_make_backup(self):
        self.write_corrections()
        original = self.layout.corrections.read_bytes()
        store = ReviewStore(self.layout)
        self.assertIsNone(store.save(self.relative, {'title': 'Earlier title', 'author': ''}))
        self.assertEqual(self.layout.corrections.read_bytes(), original)
        self.assertFalse((self.layout.corrections.parent / 'Backups').exists())

    def test_external_edit_and_changed_catalogue_are_not_overwritten(self):
        store = ReviewStore(self.layout)
        self.layout.corrections.write_text('relative_path,title\nContent/other.epub,External edit\n', encoding='utf-8')
        external = self.layout.corrections.read_bytes()
        with self.assertRaisesRegex(ReviewError, 'another window'):
            store.save(self.relative, {'title': 'Local edit', 'author': ''})
        self.assertEqual(self.layout.corrections.read_bytes(), external)
        store = ReviewStore(self.layout)
        target = self.layout.reports / 'catalogue.json'
        target.write_bytes(target.read_bytes() + b'\n')
        with self.assertRaisesRegex(ReviewError, 'catalogue changed'):
            store.save(self.relative, {'title': 'Local edit', 'author': ''})
        self.assertEqual(self.layout.corrections.read_bytes(), external)

    def test_changed_and_missing_materials_refuse_stale_corrections(self):
        store = ReviewStore(self.layout)
        before = self.layout.corrections.read_bytes()
        self.document.write_bytes(b'Changed since last scan')
        with self.assertRaisesRegex(ReviewError, 'file changed'):
            store.save(self.relative, {'title': 'Edit', 'author': ''})
        self.document.unlink()
        with self.assertRaisesRegex(ReviewError, 'missing or unavailable'):
            store.save(self.relative, {'title': 'Edit', 'author': ''})
        self.assertEqual(self.layout.corrections.read_bytes(), before)

    def test_malformed_csv_is_left_unchanged(self):
        malformed = [b'', b'wrong,header\n', b'relative_path,title,title\n',
                     b'relative_path,title\na,one\na,two\n', b'relative_path,title\na,b,c\n', b'\xff\xfe']
        for data in malformed:
            with self.subTest(data=data):
                self.layout.corrections.write_bytes(data)
                with self.assertRaises(ReviewError):
                    ReviewStore(self.layout)
                self.assertEqual(self.layout.corrections.read_bytes(), data)

    def test_unsafe_catalogue_paths_are_refused_before_any_save(self):
        catalogue = self.layout.reports / 'catalogue.json'
        for relative in ('../outside.epub', 'Content/../outside.epub', '.', 'C:/outside.epub', 'Content/.hidden.epub', 'Library Tools/Internal/pinakeys.py'):
            catalogue.write_text(json.dumps([{'relative_path': relative}]), encoding='utf-8')
            with self.subTest(relative=relative), self.assertRaises(ReviewError):
                ReviewStore(self.layout)

    @unittest.skipIf(os.name == 'nt', 'Symlink creation may require Windows developer mode')
    def test_symlink_corrections_and_material_paths_are_not_followed(self):
        original = self.layout.corrections.read_bytes()
        outside = Path(self.temp.name) / 'outside.csv'
        outside.write_bytes(original)
        self.layout.corrections.unlink()
        self.layout.corrections.symlink_to(outside)
        with self.assertRaises(ReviewError):
            ReviewStore(self.layout)
        self.assertEqual(outside.read_bytes(), original)
        self.layout.corrections.unlink()
        self.layout.corrections.write_bytes(original)
        self.document.unlink()
        self.document.symlink_to(self.other)
        with self.assertRaises(ReviewError):
            ReviewStore(self.layout)

    def test_failed_atomic_replace_preserves_previous_corrections(self):
        self.write_corrections()
        before = self.layout.corrections.read_bytes()
        store = ReviewStore(self.layout)
        with patch('review_library.os.replace', side_effect=OSError('Synthetic disk error')):
            with self.assertRaises(OSError):
                store.save(self.relative, {'title': 'Edit', 'author': ''})
        self.assertEqual(self.layout.corrections.read_bytes(), before)
        self.assertEqual(self.document.read_bytes(), self.original)
        self.assertFalse(list(self.layout.corrections.parent.glob('.corrections-*.tmp')))

    def test_accented_and_windows_separator_keys_save_the_current_catalogue_spelling(self):
        portable = unicodedata.normalize('NFC', self.relative).replace('/', '\\')
        stream = StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(['relative_path', 'title', 'author', 'date', 'identifier'])
        writer.writerow([portable, 'Earlier title', '', '', ''])
        self.layout.corrections.write_text(stream.getvalue(), encoding='utf-8')
        store = ReviewStore(self.layout)
        self.assertEqual(store.edits(self.relative)['title'], 'Earlier title')
        store.save(self.relative, {'title': 'Café correction', 'author': 'É. Author'})
        with self.layout.corrections.open(encoding='utf-8-sig', newline='') as stream:
            self.assertEqual(list(csv.DictReader(stream))[0]['relative_path'], self.relative)
        scan(self.root)
        row = next(row for row in json.loads((self.layout.reports / 'catalogue.json').read_text(encoding='utf-8')) if 'sample' in row['filename'])
        self.assertEqual(row['title'], 'Café correction')

    def test_invalid_single_line_values_do_not_write(self):
        store = ReviewStore(self.layout)
        before = self.layout.corrections.read_bytes()
        for title in ('two\nlines', 'null\x00inside', 'x' * 4097):
            with self.subTest(title=title[:20]), self.assertRaises(ReviewError):
                store.save(self.relative, {'title': title, 'author': ''})
        self.assertEqual(self.layout.corrections.read_bytes(), before)

    def test_minimal_csv_keeps_existing_columns_and_adds_editable_fields(self):
        stream = StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(['relative_path', 'date', 'note'])
        writer.writerow([self.relative, '2001', 'keep note'])
        self.layout.corrections.write_text(stream.getvalue(), encoding='utf-8')
        store = ReviewStore(self.layout)
        store.save(self.relative, {'title': 'New title', 'author': 'New author'})
        with self.layout.corrections.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream); values = list(reader)
        self.assertEqual(reader.fieldnames, ['relative_path', 'date', 'note', 'title', 'author'])
        self.assertEqual(values[0]['date'], '2001')
        self.assertEqual(values[0]['note'], 'keep note')

    def test_flat_kit_backups_are_not_added_to_root_inventory(self):
        flat = Path(self.temp.name).resolve() / 'Flat kit'
        flat.mkdir()
        for name in ('scan_library.py', 'library_paths.py', 'library_style.py', 'review_library.py', 'library_records.py', 'catalogue_settings.py', 'intake_workflow.py', 'audit_materials.py', 'operation_progress.py'):
            shutil.copyfile(source_file(name), flat / name)
        (flat / 'Content').mkdir()
        epub(flat / 'Content/example.epub', 'Original', 'Author')
        def refresh():
            result = subprocess.run([sys.executable, '-B', str(flat / 'scan_library.py'), '--folder', '.'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        refresh()
        store = ReviewStore(LibraryPaths(flat, False))
        backup = store.save('Content/example.epub', {'title': 'Corrected', 'author': ''})
        self.assertEqual(backup.parent.name, '.metadata-corrections-backups')
        refresh()
        rows = json.loads((flat / 'catalogue.json').read_text(encoding='utf-8'))
        self.assertEqual([row['relative_path'] for row in rows], ['Content/example.epub'])
        self.assertEqual(rows[0]['title'], 'Corrected')


@unittest.skipIf(tk is None, 'Tkinter is not installed')
class ReviewWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk(); self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest('No display for Tk integration tests: ' + str(exc))
        self.temp = tempfile.TemporaryDirectory(prefix='pinakeys-review-window-')
        self.library = build_folder(Path(self.temp.name).resolve() / 'USB with spaces')
        self.internal = self.library / 'Library Tools/Internal'
        self.document = self.library / 'Content/needs-metadata.epub'
        epub(self.document)
        epub(self.library / 'Content/healthy.epub', 'Healthy title', 'An author')
        scan(self.library)
        self.runner = ActionRunner(self.internal / 'pinakeys.py', python=sys.executable)
        self.mocks = [patch('pinakeys.capabilities', return_value={'EPUB metadata': True}),
                      patch('tkinter.messagebox.askyesno', return_value=False),
                      patch('tkinter.messagebox.showinfo'), patch('tkinter.messagebox.showerror')]
        self.support, self.ask, self.info, self.error = [item.start() for item in self.mocks]
        self.window = PinakeysWindow(self.root, self.runner)
        self.root.update_idletasks()

    def tearDown(self):
        if not hasattr(self, 'runner'):
            return
        if self.runner.busy:
            self.runner.cancel(); self.runner.process.wait(timeout=3)
        self.runner.close()
        try:
            for identifier in self.root.tk.call('after', 'info'):
                self.root.tk.call('after', 'cancel', identifier)
            self.root.destroy()
        except tk.TclError:
            pass
        self.temp.cleanup()
        for item in reversed(self.mocks): item.stop()

    def editor(self):
        self.window.open_review()
        self.root.update_idletasks()
        self.assertIsNotNone(self.window.review_window)
        return self.window.review_window

    def finish(self):
        deadline = time.monotonic() + 10
        def done():
            if self.window.action is None or time.monotonic() >= deadline:
                self.root.quit()
            else:
                self.root.after(10, done)
        self.root.after(10, done); self.root.mainloop()
        self.assertIsNone(self.window.action)

    def finish_trash(self, editor):
        deadline = time.monotonic() + 10
        def done():
            if (not editor.trash_job and self.window.action is None) or time.monotonic() >= deadline:
                self.root.quit()
            else:
                self.root.after(10, done)
        self.root.after(10, done); self.root.mainloop()
        self.assertFalse(editor.trash_job)
        self.assertIsNone(self.window.action)

    def test_trash_cancel_identifies_exact_file_and_defaults_to_no(self):
        editor = self.editor()
        before = self.document.read_bytes()
        corrections = self.runner.layout.corrections.read_bytes()
        with patch('pinakeys.trash_local') as move:
            editor.trash_file = move; editor.update_controls()
            editor.trash_button.invoke(); self.finish_trash(editor)
        move.assert_not_called()
        self.assertEqual(self.document.read_bytes(), before)
        self.assertEqual(self.runner.layout.corrections.read_bytes(), corrections)
        args, options = self.ask.call_args
        self.assertIn('Content/needs-metadata.epub', args[1])
        self.assertIn('This may be your only copy', args[1])
        self.assertEqual(options['default'], 'no')
        self.assertFalse(self.runner.busy)

    def test_trash_duplicate_confirms_the_copy_kept_and_refreshes_catalogue(self):
        kept = self.library/'Content/healthy.epub'
        payload = kept.read_bytes()
        self.document.write_bytes(payload)
        scan(self.library)
        corrections = self.runner.layout.corrections.read_bytes()
        destination = Path(self.temp.name)/'Synthetic Trash.epub'
        editor = self.editor()
        item = next(item for item, relative in editor.visible.items() if relative.endswith(self.document.name))
        editor.tree.selection_set(item); editor.select()
        editor.trash_file = lambda path: path.rename(destination)
        self.ask.return_value = True
        editor.update_controls(); editor.trash_button.invoke(); self.finish_trash(editor)
        self.assertIn('Content/healthy.epub', self.ask.call_args.args[1])
        self.assertFalse(self.document.exists())
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(kept.read_bytes(), payload)
        rows = json.loads((self.runner.layout.reports/'catalogue.json').read_text(encoding='utf-8'))
        self.assertEqual([row['relative_path'] for row in rows], ['Content/healthy.epub'])
        self.assertNotIn('Exact duplicate', rows[0]['review_flags'])
        self.assertEqual(self.runner.layout.corrections.read_bytes(), corrections)
        self.assertIn('File moved to Trash and catalogue updated', editor.status.get())
        self.assertIsNone(editor.pending_trash)

    def test_unsaved_corrections_block_trash_without_discarding_or_saving(self):
        editor = self.editor()
        with patch('pinakeys.trash_local') as move:
            editor.trash_file = move
            editor.inputs['title'].set('Keep my unsaved edit')
            self.assertEqual(str(editor.trash_button.cget('state')), 'disabled')
            editor.move_to_trash()
            move.assert_not_called()
        self.ask.assert_not_called()
        self.assertTrue(editor.dirty)
        self.assertTrue(self.document.exists())
        self.assertIn('Save or discard', self.info.call_args.args[1])

    def test_trash_failure_leaves_file_and_current_details_available(self):
        editor = self.editor()
        initial = editor.values()
        original = self.document.read_bytes()
        self.ask.return_value = True
        with patch('pinakeys.trash_local', side_effect=OSError('Synthetic Trash unavailable')) as move:
            editor.trash_file = move; editor.update_controls()
            editor.trash_button.invoke(); self.finish_trash(editor)
        self.assertEqual(self.document.read_bytes(), original)
        self.assertEqual(editor.values(), initial)
        self.error.assert_called_once()
        self.assertFalse(self.runner.busy)
        self.assertIsNone(editor.pending_trash)

    def test_failed_refresh_after_trash_keeps_the_file_recoverable_and_blocks_further_trash(self):
        editor = self.editor()
        destination = Path(self.temp.name)/'Synthetic Trash.epub'
        payload = self.document.read_bytes()
        editor.trash_file = lambda path: path.rename(destination)
        (self.internal/'scan_library.py').write_text('import sys\nsys.exit(2)\n', encoding='utf-8')
        self.ask.return_value = True
        editor.update_controls(); editor.trash_button.invoke(); self.finish_trash(editor)
        self.assertEqual(destination.read_bytes(), payload)
        self.assertFalse(self.document.exists())
        self.assertEqual(editor.pending_trash, 'Content/needs-metadata.epub')
        self.assertIn('Catalogue update did not finish', editor.status.get())
        self.assertEqual(str(editor.trash_button.cget('state')), 'disabled')

    def test_file_check_keeps_tk_responsive_and_blocks_overlapping_actions_and_close(self):
        editor = self.editor()
        editor.trash_file = lambda path: None
        started, release = Event(), Event()
        prepare = editor.store.prepare_trash
        def slow_check(relative):
            started.set()
            release.wait(3)
            return prepare(relative)
        with patch.object(editor.store, 'prepare_trash', side_effect=slow_check):
            editor.move_to_trash()
            try:
                self.assertTrue(started.wait(1))
                heartbeat = []
                self.root.after(10, lambda: heartbeat.append(True))
                self.root.after(35, self.root.quit); self.root.mainloop()
                self.assertTrue(heartbeat)
                self.assertFalse(self.window.start('refresh'))
                self.assertFalse(editor.close())
            finally:
                release.set()
            self.finish_trash(editor)
        self.assertTrue(self.document.exists())

    def test_open_shows_flags_and_reuses_one_editor_without_writing(self):
        before = self.runner.layout.corrections.read_bytes()
        editor = self.editor()
        self.assertEqual(len(editor.visible), 1)
        self.assertIn('Missing title', editor.reasons.get())
        self.assertIn('Missing author', editor.reasons.get())
        self.window.open_review()
        self.assertIs(self.window.review_window, editor)
        self.assertEqual(self.runner.layout.corrections.read_bytes(), before)
        self.assertFalse(self.runner.busy)

    def test_prefilled_fields_are_clean_and_selection_and_close_need_no_discard(self):
        before = self.runner.layout.corrections.read_bytes()
        editor = self.editor()
        self.assertEqual(editor.inputs['title'].get(), 'needs-metadata')
        self.assertEqual(editor.inputs['author'].get(), '')
        self.assertIn('Suggested from filename', editor.sources['title'].get())
        self.assertIn('No author available', editor.sources['author'].get())
        self.assertFalse(editor.dirty)
        self.assertEqual(str(editor.save_button.cget('state')), 'disabled')
        self.assertEqual(str(editor.discard_button.cget('state')), 'disabled')
        editor.review_only.set(False); editor.filter_rows()
        item = next(item for item, relative in editor.visible.items() if relative.endswith('healthy.epub'))
        editor.tree.selection_set(item); editor.select()
        self.assertEqual(editor.values(), {'title': 'Healthy title', 'author': 'An author'})
        self.assertEqual(editor.sources['author'].get(), 'Embedded metadata')
        self.assertFalse(editor.dirty)
        self.assertTrue(editor.close())
        self.ask.assert_not_called()
        self.assertEqual(self.runner.layout.corrections.read_bytes(), before)

    def test_editing_a_prefilled_title_saves_both_displayed_details_only_on_save(self):
        named = self.library / 'Content/Designing_Gardens — Jane_Doe.epub'
        epub(named)
        scan(self.library)
        original = named.read_bytes()
        before = self.runner.layout.corrections.read_bytes()
        editor = self.editor()
        editor.query.set('Designing_Gardens'); editor.review_only.set(False); editor.filter_rows()
        self.assertEqual(editor.values(), {'title': 'Designing Gardens', 'author': 'Jane Doe'})
        self.assertFalse(editor.dirty)
        editor.inputs['title'].set('Designing small gardens')
        self.assertTrue(editor.dirty)
        self.assertEqual(self.runner.layout.corrections.read_bytes(), before)
        editor.save_button.invoke()
        self.assertTrue(self.runner.busy)
        self.assertFalse(editor.dirty)
        self.finish()
        rows = json.loads((self.runner.layout.reports / 'catalogue.json').read_text(encoding='utf-8'))
        row = next(row for row in rows if row['filename'] == named.name)
        self.assertEqual((row['title'], row['author']), ('Designing small gardens', 'Jane Doe'))
        self.assertEqual(editor.values(), {'title': 'Designing small gardens', 'author': 'Jane Doe'})
        self.assertEqual(editor.sources['author'].get(), 'Saved correction')
        self.assertEqual(named.read_bytes(), original)
        self.ask.assert_not_called()

    def test_discard_restores_prefill_and_reverting_typing_is_clean(self):
        before = self.runner.layout.corrections.read_bytes()
        editor = self.editor()
        initial = editor.values()
        editor.inputs['title'].set('An unsaved edit')
        self.assertTrue(editor.dirty)
        editor.discard_button.invoke()
        self.assertEqual(editor.values(), initial)
        self.assertFalse(editor.dirty)
        editor.inputs['author'].set('An unsaved author')
        editor.inputs['author'].set(initial['author'])
        self.assertFalse(editor.dirty)
        self.assertEqual(str(editor.save_button.cget('state')), 'disabled')
        self.assertEqual(self.runner.layout.corrections.read_bytes(), before)

    def test_portable_save_verifies_one_file_off_tk_and_refreshes(self):
        from library_records import Records
        records=Records(self.runner.layout)
        with records.lock():records.save()
        editor=self.editor();original=self.document.read_bytes()
        editor.inputs['title'].set('Portable correction');editor.inputs['author'].set('Writer')
        editor.save_button.invoke()
        self.assertTrue(editor.trash_job)
        self.finish_trash(editor)
        record=Records(self.runner.layout).by_path('Content/needs-metadata.epub')
        self.assertEqual(record['corrections']['title'],'Portable correction')
        self.assertEqual(self.document.read_bytes(),original)
        self.assertIn('Portable correction',self.runner.layout.catalogue.read_text(encoding='utf-8'))

    def test_portable_save_check_can_be_cancelled_without_losing_edits(self):
        from library_records import Records,proof
        from threading import Event
        records=Records(self.runner.layout)
        with records.lock():records.save()
        before=records.path.read_bytes();editor=self.editor()
        editor.inputs['title'].set('Keep unsaved');release=Event();started=Event();ticks=[]
        def slow(path):
            started.set();release.wait(3);return proof(path)
        with patch('library_records.proof',side_effect=slow):
            editor.save_button.invoke()
            self.root.after(15,lambda:ticks.append(True))
            def cancel():
                editor.cancel_save_button.invoke();release.set()
            self.root.after(60,cancel)
            self.finish_trash(editor)
        self.assertTrue(started.is_set());self.assertTrue(ticks)
        self.assertTrue(editor.dirty)
        self.assertEqual(records.path.read_bytes(),before)
        self.assertIn('cancelled',editor.status.get())
        self.error.assert_not_called()

    def test_clearing_an_unsaved_filename_suggestion_does_not_write_blank_overrides(self):
        before = self.runner.layout.corrections.read_bytes()
        catalogue = (self.runner.layout.reports / 'catalogue.json').read_bytes()
        editor = self.editor()
        editor.inputs['title'].set('')
        editor.save_button.invoke()
        self.assertFalse(editor.dirty)
        self.assertFalse(self.runner.busy)
        self.assertIn('Saved corrections are unchanged', editor.status.get())
        self.assertEqual(self.runner.layout.corrections.read_bytes(), before)
        self.assertEqual((self.runner.layout.reports / 'catalogue.json').read_bytes(), catalogue)
        self.assertFalse((self.runner.layout.corrections.parent / 'Backups').exists())

    def test_review_launch_action_opens_and_closes_without_writing(self):
        corrections = self.runner.layout.corrections.read_bytes()
        catalogue = (self.runner.layout.reports / 'catalogue.json').read_bytes()
        result = subprocess.run([sys.executable, '-B', str(self.internal / 'pinakeys.py'), '--review', '--smoke-test'],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Review editor created: 1 entries shown', result.stdout)
        self.assertEqual(self.runner.layout.corrections.read_bytes(), corrections)
        self.assertEqual((self.runner.layout.reports / 'catalogue.json').read_bytes(), catalogue)

    def test_save_updates_reports_and_clears_only_resolved_metadata_flags(self):
        editor = self.editor()
        original = self.document.read_bytes()
        prior_csv = self.runner.layout.corrections.read_bytes()
        editor.inputs['title'].set('A corrected title')
        editor.inputs['author'].set('A corrected author')
        self.assertEqual(str(editor.save_button.cget('state')), 'normal')
        editor.save_button.invoke()
        self.assertTrue(self.runner.busy)
        self.assertEqual(str(editor.save_button.cget('state')), 'disabled')
        self.finish()
        rows = json.loads((self.runner.layout.reports / 'catalogue.json').read_text(encoding='utf-8'))
        row = next(row for row in rows if row['filename'] == self.document.name)
        self.assertEqual((row['title'], row['author'], row['review_flags']), ('A corrected title', 'A corrected author', ''))
        self.assertIn('A corrected title', self.runner.layout.catalogue.read_text(encoding='utf-8'))
        self.assertIn('saved and catalogue updated', editor.status.get())
        self.assertFalse(editor.visible)
        self.assertEqual(self.document.read_bytes(), original)
        backups = list((self.runner.layout.corrections.parent / 'Backups').glob('*.csv'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), prior_csv)

    def test_external_csv_change_keeps_unsaved_form_and_shows_error(self):
        editor = self.editor()
        editor.inputs['title'].set('Unsaved local correction')
        self.runner.layout.corrections.write_text('relative_path,title\nContent/healthy.epub,External correction\n', encoding='utf-8')
        changed = self.runner.layout.corrections.read_bytes()
        editor.save_button.invoke()
        self.error.assert_called_once()
        self.assertTrue(editor.dirty)
        self.assertFalse(self.runner.busy)
        self.assertEqual(self.runner.layout.corrections.read_bytes(), changed)

    def test_selection_and_parent_close_preserve_unsaved_edits_when_discard_declined(self):
        editor = self.editor()
        editor.review_only.set(False); editor.filter_rows()
        selected = editor.selected
        editor.inputs['title'].set('Keep these unsaved edits')
        other = next(item for item, relative in editor.visible.items() if relative != selected)
        editor.tree.selection_set(other); editor.select()
        self.assertEqual(editor.selected, selected)
        self.assertEqual(editor.inputs['title'].get(), 'Keep these unsaved edits')
        self.window.close()
        self.assertTrue(self.root.winfo_exists())
        self.assertIs(self.window.review_window, editor)
        self.assertTrue(editor.dirty)
        self.assertFalse(self.window.start('refresh'))
        self.assertFalse(self.runner.busy)

    def test_search_and_review_filter_work_together(self):
        editor = self.editor()
        editor.query.set('Healthy'); editor.filter_rows()
        self.assertFalse(editor.visible)
        self.assertIn('No matching files', editor.status.get())
        editor.review_only.set(False); editor.filter_rows()
        self.assertEqual(len(editor.visible), 1)
        self.assertEqual(editor.file_name.get(), 'healthy.epub')
        editor.query.set(''); editor.review_only.set(True); editor.filter_rows()
        self.assertEqual(len(editor.visible), 1)
        self.assertEqual(editor.file_name.get(), 'needs-metadata.epub')

    def test_failed_refresh_keeps_the_saved_correction_and_explains_status(self):
        editor = self.editor()
        (self.internal / 'scan_library.py').write_text('import sys\nsys.exit(2)\n', encoding='utf-8')
        editor.inputs['title'].set('Saved even if refresh fails')
        editor.save_button.invoke(); self.finish()
        with self.runner.layout.corrections.open(encoding='utf-8-sig', newline='') as stream:
            values = list(csv.DictReader(stream))
        self.assertEqual(values[0]['title'], 'Saved even if refresh fails')
        self.assertIn('Corrections are saved', editor.status.get())
        self.assertIn('did not finish', editor.status.get())
        self.assertFalse(editor.dirty)
        self.assertFalse(editor.busy)

    def test_reveal_action_targets_the_selected_file_without_saving_edits(self):
        before = self.runner.layout.corrections.read_bytes()
        with patch('pinakeys.reveal_local') as revealed:
            editor = self.editor()
            editor.inputs['title'].set('An unsaved correction')
            editor.reveal_button.invoke()
        revealed.assert_called_once_with(self.document)
        expected = 'Show in Finder' if sys.platform == 'darwin' else 'Show containing folder'
        self.assertEqual(editor.reveal_button.cget('text'), expected)
        self.assertTrue(editor.dirty)
        self.assertEqual(self.runner.layout.corrections.read_bytes(), before)

    def test_reveal_failure_keeps_unsaved_edits_and_explains_the_error(self):
        before = self.runner.layout.corrections.read_bytes()
        with patch('pinakeys.reveal_local', side_effect=OSError('Synthetic reveal failure')):
            editor = self.editor()
            editor.inputs['author'].set('An unsaved author')
            editor.reveal_button.invoke()
        self.info.assert_called_once_with('Could not show the file', 'Synthetic reveal failure', parent=editor.top)
        self.assertEqual(editor.inputs['author'].get(), 'An unsaved author')
        self.assertTrue(editor.dirty)
        self.assertEqual(self.runner.layout.corrections.read_bytes(), before)

    def test_save_controls_remain_inside_the_window_at_minimum_size(self):
        editor = self.editor()
        self.root.deiconify(); editor.focus()
        editor.top.geometry('940x680')
        settle_window(self.root, lambda: editor.detail_canvas.winfo_height() > 100)
        bottom = editor.save_button.winfo_rooty() + editor.save_button.winfo_height()
        self.assertLessEqual(bottom, editor.top.winfo_rooty() + editor.top.winfo_height())
        right = editor.save_button.winfo_rootx() + editor.save_button.winfo_width()
        self.assertLessEqual(right, editor.top.winfo_rootx() + editor.top.winfo_width())
        self.assertLessEqual(editor.reveal_button.winfo_rooty() + editor.reveal_button.winfo_height(),
                             editor.top.winfo_rooty() + editor.top.winfo_height())
        self.assertLessEqual(editor.trash_button.winfo_rootx() + editor.trash_button.winfo_width(),
                             editor.top.winfo_rootx() + editor.top.winfo_width())
        self.assertLessEqual(editor.trash_button.winfo_rooty() + editor.trash_button.winfo_height(),
                             editor.top.winfo_rooty() + editor.top.winfo_height())
        self.assertGreater(editor.detail_canvas.winfo_height(), 100)

    def test_title_edit_does_not_clear_empty_or_duplicate_warnings(self):
        for name in ('empty-one.pdf', 'empty-two.pdf'):
            (self.library / 'Content' / name).write_bytes(b'')
        scan(self.library)
        editor = self.editor()
        item = next(item for item, path in editor.visible.items() if path.endswith('empty-one.pdf'))
        editor.tree.selection_set(item); editor.select()
        self.assertIn('cannot repair', editor.explanation.get())
        editor.inputs['title'].set('Known empty copy')
        editor.inputs['author'].set('A recorded author')
        editor.save_button.invoke(); self.finish()
        rows = json.loads((self.runner.layout.reports / 'catalogue.json').read_text(encoding='utf-8'))
        row = next(row for row in rows if row['filename'] == 'empty-one.pdf')
        self.assertIn('Empty file', row['review_flags'])
        self.assertIn('Possible duplicate', row['review_flags'])
        self.assertIn('Empty file', editor.reasons.get())
        self.assertEqual((self.library / 'Content/empty-one.pdf').read_bytes(), b'')

    def test_clearing_saved_form_corrections_restores_missing_metadata_flags(self):
        editor = self.editor()
        editor.inputs['title'].set('Temporary title')
        editor.inputs['author'].set('Temporary author')
        editor.save_button.invoke(); self.finish()
        editor.query.set('needs-metadata'); editor.review_only.set(False); editor.filter_rows()
        self.assertEqual(editor.inputs['title'].get(), 'Temporary title')
        editor.inputs['title'].set(''); editor.inputs['author'].set('')
        editor.save_button.invoke(); self.finish()
        rows = json.loads((self.runner.layout.reports / 'catalogue.json').read_text(encoding='utf-8'))
        row = next(row for row in rows if row['filename'] == self.document.name)
        self.assertEqual(row['title'], '')
        self.assertIn('Missing title', row['review_flags'])
        self.assertIn('Missing author', row['review_flags'])


if __name__ == '__main__':
    unittest.main()
