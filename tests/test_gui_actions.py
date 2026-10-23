"""Exercise actual Tk widgets on synthetic libraries; external opens are mocked.

These are component integration tests, not mouse-driven visual QA. Headless
systems may skip this optional suite; core tests do not require Tkinter.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest.mock import patch
import zipfile

try:
    import tkinter as tk
except ImportError:
    tk = None

from build_usb import build_folder
from run_tests import settle_window
from pinakeys import ActionRunner, PinakeysWindow, capabilities, open_local, reveal_local, trash_local


class OpenDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pinakeys open test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.page = self.root / 'Local catalogue.html'
        self.page.write_text('<html>synthetic</html>', encoding='utf-8')

    def test_missing_file_is_reported_before_starting_external_program(self):
        with patch('pinakeys.webbrowser.open') as browser:
            with self.assertRaises(FileNotFoundError):
                open_local(self.root / 'missing.html')
            browser.assert_not_called()

    def test_browser_dispatch_uses_file_uri_with_escaped_spaces(self):
        with patch('pinakeys.webbrowser.open', return_value=True) as browser:
            open_local(self.page)
            browser.assert_called_once_with(self.page.resolve().as_uri())

    def test_failed_browser_dispatch_is_reported(self):
        with patch('pinakeys.webbrowser.open', return_value=False):
            with self.assertRaisesRegex(OSError, 'browser'):
                open_local(self.page)

    @unittest.skipIf(os.name == 'nt', 'Windows uses os.startfile')
    def test_failed_file_handler_is_reported(self):
        report = self.root / 'review.csv'
        report.write_text('filename,reason\n', encoding='utf-8')
        with patch('pinakeys.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'No application available')):
            with self.assertRaisesRegex(OSError, 'No application'):
                open_local(report)

    def test_missing_worker_is_not_reported_as_available_epub_support(self):
        result = capabilities(str(self.root / 'nonexistent-python'))
        self.assertFalse(result['EPUB metadata'])
        self.assertFalse(result['PDF metadata'])

    def test_mac_reveal_selects_the_file_without_opening_its_default_application(self):
        material = self.root / 'Café $Book; [1].html'
        material.write_text('Synthetic reveal test', encoding='utf-8')
        with patch('pinakeys.sys.platform', 'darwin'), \
                patch('pinakeys.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run, \
                patch('pinakeys.open_local') as opened, patch('pinakeys.webbrowser.open') as browser:
            reveal_local(material)
        args, options = run.call_args
        self.assertEqual(args[0], ['/usr/bin/open', '-R', str(material.resolve())])
        self.assertFalse(options.get('shell', False))
        self.assertEqual(options['timeout'], 10)
        opened.assert_not_called()
        browser.assert_not_called()

    def test_missing_reveal_target_is_reported_without_external_dispatch(self):
        with patch('pinakeys.subprocess.run') as run, patch('pinakeys.open_local') as opened:
            with self.assertRaisesRegex(FileNotFoundError, 'file is missing'):
                reveal_local(self.root / 'missing.epub')
        run.assert_not_called()
        opened.assert_not_called()

    def test_mac_reveal_failure_and_timeout_are_reported(self):
        with patch('pinakeys.sys.platform', 'darwin'):
            with patch('pinakeys.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'Finder could not show this file')):
                with self.assertRaisesRegex(OSError, 'Finder could not'):
                    reveal_local(self.page)
            with patch('pinakeys.subprocess.run', side_effect=subprocess.TimeoutExpired('open', 10)):
                with self.assertRaisesRegex(OSError, 'Finder did not respond'):
                    reveal_local(self.page)

    def test_other_platforms_keep_the_containing_folder_action(self):
        for platform in ('win32', 'linux'):
            with self.subTest(platform=platform), patch('pinakeys.sys.platform', platform), \
                    patch('pinakeys.open_local') as opened, patch('pinakeys.subprocess.run') as run:
                reveal_local(self.page)
                opened.assert_called_once_with(self.page.parent)
                run.assert_not_called()

    def test_native_trash_passes_filename_as_data_and_requires_confirmed_removal(self):
        material = self.root/'Café $book; sample.txt'
        payload = b'Synthetic native Trash test'
        material.write_bytes(payload)
        destination = self.root/'Synthetic Trash.txt'
        def native_move(*args, **options):
            material.rename(destination)
            return subprocess.CompletedProcess(args[0], 0, 'PINakeys_TRASH_OK\n', '')
        with patch('pinakeys.sys.platform', 'darwin'), patch('pinakeys.subprocess.run', side_effect=native_move) as run:
            trash_local(material)
        args, options = run.call_args
        self.assertEqual(args[0][:4], ['/usr/bin/osascript', '-l', 'JavaScript', '-e'])
        self.assertEqual(len(args[0]), 6)
        self.assertNotIn(material.name, args[0][4])
        self.assertEqual(args[0][-1], unicodedata.normalize('NFC', str(material.absolute())))
        self.assertFalse(options.get('shell', False))
        self.assertEqual(destination.read_bytes(), payload)
        self.assertFalse(material.exists())

    def test_trash_unavailable_and_timeout_leave_the_file_without_deletion_fallback(self):
        original = self.page.read_bytes()
        failures = [subprocess.CompletedProcess([], 1, '', 'Trash unavailable'), subprocess.TimeoutExpired('osascript', 15)]
        for failure in failures:
            options = {'side_effect':failure} if isinstance(failure, Exception) else {'return_value':failure}
            with self.subTest(failure=type(failure).__name__), patch('pinakeys.sys.platform', 'darwin'), \
                    patch('pinakeys.subprocess.run', **options) as run:
                with self.assertRaises(OSError):
                    trash_local(self.page)
                run.assert_called_once()
                self.assertEqual(self.page.read_bytes(), original)

    def test_trash_success_response_with_file_still_present_is_not_accepted(self):
        original = self.page.read_bytes()
        with patch('pinakeys.sys.platform', 'darwin'), \
                patch('pinakeys.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'PINakeys_TRASH_OK\n', '')):
            with self.assertRaisesRegex(OSError, 'move was not confirmed'):
                trash_local(self.page)
        self.assertEqual(self.page.read_bytes(), original)

    def test_trash_on_unsupported_platform_does_not_start_a_file_operation(self):
        with patch('pinakeys.sys.platform', 'linux'), patch('pinakeys.subprocess.run') as run:
            with self.assertRaisesRegex(OSError, 'available on Mac'):
                trash_local(self.page)
            run.assert_not_called()


@unittest.skipIf(tk is None, 'Tkinter is not installed')
class WindowActionTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest('No display available for Tk component tests: ' + str(exc))
        self.temp = tempfile.TemporaryDirectory(prefix='pinakeys widget test ')
        self.library = build_folder(Path(self.temp.name).resolve() / 'USB with spaces')
        self.internal = self.library / 'Library Tools/Internal'
        with zipfile.ZipFile(self.library / 'Content/example.epub', 'w') as z:
            z.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
            z.writestr('book.opf', '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Widget test</dc:title><dc:creator>Synthetic author</dc:creator></metadata></package>')
        self.runner = ActionRunner(self.internal / 'pinakeys.py', python=sys.executable)
        self.support_patch = patch('pinakeys.capabilities', return_value={'EPUB metadata': True, 'PDF metadata': False})
        self.support_patch.start()
        self.window = PinakeysWindow(self.root, self.runner)
        self.root.update_idletasks()
        self.ask_patch = patch('tkinter.messagebox.askyesno', return_value=True)
        self.ask = self.ask_patch.start()
        self.info_patch = patch('tkinter.messagebox.showinfo')
        self.info = self.info_patch.start()
        self.error_patch = patch('tkinter.messagebox.showerror')
        self.error = self.error_patch.start()
        self.closed = False

    def tearDown(self):
        if not hasattr(self, 'runner'):
            return
        if self.runner.busy:
            self.runner.cancel()
            self.runner.process.wait(timeout=3)
        self.runner.close()
        try:
            if self.root.winfo_exists():
                for identifier in self.root.tk.call('after', 'info'):
                    self.root.tk.call('after', 'cancel', identifier)
                self.root.destroy()
        except tk.TclError:
            pass
        self.temp.cleanup()
        self.support_patch.stop()
        self.ask_patch.stop()
        self.info_patch.stop()
        self.error_patch.stop()

    def widgets(self, parent=None):
        for child in (parent or self.root).winfo_children():
            yield child
            yield from self.widgets(child)

    def button(self, label):
        return next(w for w in self.widgets() if w.winfo_class() == 'TButton' and w.cget('text') == label)

    def finish_action(self):
        deadline = time.monotonic() + 10

        def completed():
            if not self.window.action or time.monotonic() >= deadline:
                self.root.quit()
            else:
                self.root.after(10, completed)

        # Use the application's real event loop. Repeated update() calls can
        # stay inside macOS's event queue while themed progress bars animate.
        self.root.after(10, completed)
        self.root.mainloop()
        self.assertIsNone(self.window.action, self.window.status.get())

    def test_folder_settings_save_portable_selection_without_running(self):
        (self.library/'Existing collection').mkdir()
        self.button('Folders and exclusions…').invoke()
        dialog=next(w for w in self.root.winfo_children() if w.winfo_class()=='Toplevel')
        texts=[w for w in dialog.winfo_children() if w.winfo_class()=='Text']
        texts[0].delete('1.0','end');texts[0].insert('1.0','Existing collection')
        texts[1].insert('1.0','*.zip')
        next(w for w in dialog.winfo_children() if w.winfo_class()=='TButton' and w.cget('text')=='Save selection (no scan)').invoke()
        from catalogue_settings import Selection
        self.assertEqual(Selection(self.runner.layout).data['folders'],['Existing collection'])
        self.assertEqual(Selection(self.runner.layout).data['exclude'],['*.zip'])
        self.assertFalse(self.runner.busy)

    def test_full_verification_button_progress_and_completion(self):
        self.button('Full verification…').invoke()
        self.assertTrue(self.runner.busy)
        self.finish_action()
        self.assertIn('Full verification finished',self.window.status.get())
        rows=json.loads((self.runner.layout.reports/'catalogue.json').read_text(encoding='utf-8'))
        self.assertTrue(rows[0]['record_id'])
        self.assertTrue(rows[0]['sha256'])
        self.assertEqual(rows[0]['verification'],'Fresh fingerprint')

    def test_legacy_migration_uses_cancellable_worker_and_preserves_source_csv(self):
        csv=self.runner.layout.corrections
        csv.write_text('relative_path,title,author,unknown\nContent/example.epub,Legacy title,Writer,retain me\n', encoding='utf-8')
        before=csv.read_bytes()
        self.button('Intake · Review and File…').invoke()
        self.button('Enable portable records…').invoke()
        self.assertEqual(self.window.action,'migrate')
        self.assertTrue(self.runner.busy)
        self.finish_action()
        from library_records import Records
        records=Records(self.runner.layout)
        r=records.by_path('Content/example.epub')
        self.assertEqual(r['corrections']['title'],'Legacy title')
        self.assertEqual(r['legacy_rows'][0]['unknown'],'retain me')
        self.assertEqual(csv.read_bytes(),before)
        self.assertIn('migration finished',self.window.status.get())

    def test_all_open_buttons_dispatch_expected_destinations(self):
        platform = 'Mac' if sys.platform == 'darwin' else ('Windows' if os.name == 'nt' else 'Linux')
        with patch('pinakeys.open_local') as open_file:
            expected = [('Open Library', self.runner.layout.catalogue),
                        ('Open Setup Folder', self.runner.layout.tools / platform),
                        ('Open Reports', self.runner.layout.reports)]
            for label, target in expected:
                self.button(label).invoke()
                self.assertEqual(open_file.call_args.args, (target,))
        self.assertFalse(self.runner.busy)

    def test_advanced_checkbox_expands_and_collapses_actions(self):
        self.assertEqual(self.window.advanced_frame.winfo_manager(), '')
        self.window.advanced_toggle.invoke()
        self.assertEqual(self.window.advanced_frame.winfo_manager(), 'pack')
        self.window.advanced_toggle.invoke()
        self.assertEqual(self.window.advanced_frame.winfo_manager(), '')

    def test_run_details_explains_the_existing_audit_and_available_tools_without_running(self):
        with patch('pinakeys.capabilities', return_value={'EPUB metadata': True, 'ClamAV': False, 'PDFiD': True, 'EPUBCheck': False}):
            self.window.check_tools()
        text = self.window.details.get('1.0', 'end')
        self.assertIn('choose Check Incoming', text)
        self.assertIn('Audit folder: Incoming Files/', text)
        self.assertIn('file signatures, empty files and SHA-256', text)
        self.assertIn('ClamAV (malware signatures): NOT INSTALLED', text)
        self.assertIn('PDFiD (PDF active features): AVAILABLE', text)
        self.assertIn('EPUB structure, not a malware scan', text)
        self.assertIn('incomplete checks', text)
        self.assertFalse(self.runner.busy)
        self.assertFalse((self.runner.layout.reports/'intake_audit.json').exists())
        self.assertFalse((self.runner.layout.reports/'catalogue.json').exists())

    def test_tool_availability_refresh_does_not_overwrite_current_or_finished_run_details(self):
        self.button('Refresh Catalogue').invoke()
        self.assertIn('Choose Check Incoming for security checks', self.window.details.get('1.0', 'end'))
        current = self.window.details.get('1.0', 'end')
        self.window.check_tools()
        self.assertEqual(self.window.details.get('1.0', 'end'), current)
        self.finish_action()
        finished = self.window.details.get('1.0', 'end')
        self.window.check_tools()
        self.assertEqual(self.window.details.get('1.0', 'end'), finished)
        self.assertIn('Indexed 1 files', finished)

    def test_refresh_button_locks_actions_then_updates_summary_and_details(self):
        self.button('Refresh Catalogue').invoke()
        self.assertTrue(self.runner.busy)
        self.assertTrue(all(str(button.cget('state')) == 'disabled' for button in self.window.buttons))
        self.assertEqual(str(self.window.stop.cget('state')), 'normal')
        self.finish_action()
        self.assertTrue(all(str(button.cget('state')) == 'normal' for button in self.window.buttons))
        self.assertEqual(str(self.window.stop.cget('state')), 'disabled')
        self.assertIn('1 files', self.window.summary.get())
        self.assertIn('Catalogue refreshed', self.window.status.get())
        self.assertIn('Indexed 1 files', self.window.details.get('1.0', 'end'))

    def test_metadata_confirmation_cancel_and_confirm(self):
        self.ask.return_value = False
        self.button('Read Document Metadata').invoke()
        self.assertFalse(self.runner.busy)
        self.assertFalse((self.runner.layout.reports / 'catalogue.json').exists())
        self.ask.return_value = True
        self.button('Read Document Metadata').invoke()
        self.finish_action()
        rows = json.loads((self.runner.layout.reports / 'catalogue.json').read_text(encoding='utf-8'))
        self.assertEqual(rows[0]['title'], 'Widget test')
        self.assertIn('Metadata extraction finished', self.window.status.get())

    def test_missing_report_opens_explanation_without_crashing(self):
        self.button('Review Files').invoke()
        self.info.assert_called_once()
        self.error.assert_not_called()
        self.assertFalse(self.runner.busy)

    def test_failed_external_open_is_explained_by_the_window(self):
        with patch('pinakeys.open_local', side_effect=OSError('Synthetic file-handler failure')):
            self.button('Open Library').invoke()
        self.assertIn('Synthetic file-handler failure', self.info.call_args.args[1])
        self.assertFalse(self.runner.busy)

    def test_check_incoming_completes_and_creates_reports(self):
        self.button('Check Incoming').invoke()
        self.finish_action()
        report = json.loads((self.runner.layout.reports / 'intake_audit.json').read_text(encoding='utf-8'))
        self.assertEqual(report['files'], [])
        self.assertEqual(report['scanned_folder'], 'Incoming Files')
        self.assertIn('Audit finished', self.window.status.get())
        self.assertIn('Completed: 0 files', self.window.details.get('1.0', 'end'))
        self.assertIn('No files were found to check', self.window.details.get('1.0', 'end'))
        self.assertIn('summary at the end of Run details', self.window.status.get())
        self.assertEqual(float(self.window.progress['value']), 100)
        self.assertIn('Complete', self.window.progress_text.get())

    def test_check_incoming_shows_final_summary_and_preserves_arrival(self):
        arrival = self.library / 'Incoming Files' / 'synthetic.pdf'
        original = b'MZ synthetic test data, not an executable'
        arrival.write_bytes(original)
        with patch.dict(os.environ, {'PATH': ''}):
            self.button('Check Incoming').invoke()
            self.finish_action()
        summary = self.window.details.get('1.0', 'end').split('Audit summary', 1)[1]
        self.assertIn('1 file needs your attention', summary)
        self.assertIn('Checks are incomplete for 1 of 1 files', summary)
        self.assertIn('No ClamAV malware scan completed', summary)
        self.assertIn('Library Tools/Reports/intake_audit.csv', summary)
        self.assertEqual(arrival.read_bytes(), original)
        self.assertEqual(float(self.window.progress['value']), 100)
        self.assertIn('missing checks', self.window.progress_text.get())

    def test_audit_stages_show_live_counts_slow_file_elapsed_and_only_finish_after_exit(self):
        script = '''import argparse,json,time
p=argparse.ArgumentParser();p.add_argument('--progress-file');args=p.parse_args()
with open(args.progress_file,'x') as stream:
    def emit(stage,state,done,total,file=''):
        stream.write(json.dumps(dict(version=1,stage=stage,state=state,done=done,total=total,file=file))+'\\n');stream.flush()
    emit(1,'loading',0,3);time.sleep(.5)
    emit(1,'running',1,3,'Incoming Files/first.txt');time.sleep(.5)
    emit(1,'complete',3,3)
    emit(2,'running',0,3,'Incoming Files/slow.epub');time.sleep(1.3)
    emit(2,'running',2,3,'Incoming Files/final.pdf');time.sleep(.4)
    emit(3,'running',0,2);time.sleep(.4)
    emit(3,'complete',2,2);time.sleep(.4)
print('Synthetic audit finished',flush=True)
'''
        (self.internal / 'audit_materials.py').write_text(script, encoding='utf-8')
        self.button('Check Incoming').invoke()
        samples = []
        def sample():
            samples.append((self.window.status.get(), str(self.window.progress['mode']),
                            float(self.window.progress['value']), self.window.progress_text.get(), self.runner.busy))
            if self.window.action:
                self.root.after(40, sample)
        self.root.after(40, sample)
        self.finish_action()
        self.assertTrue(any('loading database' in status and mode == 'indeterminate' for status, mode, *_ in samples))
        self.assertTrue(any(value == 33 and '1 of 3' in note and busy for _, _, value, note, busy in samples))
        slow = [note for _, mode, value, note, busy in samples if 'slow.epub' in note and busy]
        self.assertGreater(len(set(slow)), 1, 'Elapsed time did not advance during the slow file')
        self.assertTrue(any('Stage 2' in status and value == 0 and '0 of 3' in note for status, _, value, note, _ in samples))
        self.assertTrue(any('Stage 3' in status and mode == 'indeterminate' and busy for status, mode, _, _, busy in samples))
        self.assertFalse(any(note.startswith('Complete') and busy for _, _, _, note, busy in samples))
        self.assertEqual(float(self.window.progress['value']), 100)
        self.assertIn('Complete', self.window.progress_text.get())

    def test_progress_ignores_regression_and_cannot_mark_failed_worker_complete(self):
        events = [dict(version=1, stage=2, state='running', done=2, total=3),
                  dict(version=1, stage=1, state='complete', done=3, total=3),
                  dict(version=1, stage=2, state='running', done=1, total=3),
                  dict(version=1, stage=2, state='running', done=4, total=5)]
        self.window.apply_audit_progress(events)
        self.assertEqual(self.window.audit_event, events[0])
        script = ('import json,sys\nfrom pathlib import Path\n'
                  'Path(sys.argv[2]).write_text(json.dumps(dict(version=1,stage=3,state="complete",done=2,total=2))+"\\n")\n'
                  'raise SystemExit(3)\n')
        (self.internal / 'audit_materials.py').write_text(script, encoding='utf-8')
        self.button('Check Incoming').invoke()
        self.finish_action()
        self.assertLess(float(self.window.progress['value']), 100)
        self.assertIn('incomplete', self.window.progress_text.get())

    def test_progress_note_fits_minimum_window_and_shows_for_quick_update(self):
        self.root.deiconify()
        self.root.geometry('650x620')
        self.window.advanced_toggle.invoke()
        (self.internal / 'audit_materials.py').write_text('import time;time.sleep(15)', encoding='utf-8')
        self.button('Check Incoming').invoke()
        self.window.apply_audit_progress([dict(version=1, stage=2, state='running', done=0, total=1, file='long name.pdf')])
        self.window.render_audit_progress()
        # Settle the visible window through the real event loop. Re-entering
        # idle layout while a native Mac progress bar animates can crash Tk.
        settle_window(self.root, lambda: self.window.progress_note.winfo_ismapped() and self.window.stop.winfo_ismapped())
        for widget in (self.window.progress_note, self.window.stop):
            bottom = widget.winfo_rooty() - self.root.winfo_rooty() + widget.winfo_height()
            self.assertLessEqual(bottom, self.root.winfo_height())
            self.assertTrue(widget.winfo_ismapped())
        # Setup controls remain reachable even when the operational footer
        # reserves space at the minimum size or fonts/path wrapping are tall.
        self.window.action_canvas.yview_moveto(1.0)
        settle_window(self.root, lambda: self.window.action_canvas.yview()[1] >= 0.99)
        self.assertGreaterEqual(self.window.action_canvas.yview()[1], 0.99)
        self.window.stop.invoke()
        self.finish_action()
        self.assertLess(float(self.window.progress['value']), 100)
        self.assertIn('incomplete', self.window.progress_text.get())
        self.button('Refresh Catalogue').invoke()
        self.assertEqual(self.window.progress_note.winfo_manager(), 'pack')
        self.assertIsNone(self.window.audit_event)
        self.assertEqual(self.runner.read_progress(), [])
        self.finish_action()

    def test_worker_cannot_start_leaves_controls_ready(self):
        self.runner.python = str(self.library / 'missing-python')
        self.button('Refresh Catalogue').invoke()
        self.error.assert_called_once()
        self.assertFalse(self.runner.busy)
        self.assertIsNone(self.window.action)
        self.assertTrue(all(str(button.cget('state')) == 'normal' for button in self.window.buttons))
        self.assertEqual(str(self.window.stop.cget('state')), 'disabled')

    def test_worker_failure_is_visible_and_reenables_buttons(self):
        (self.internal / 'audit_materials.py').write_text('import sys\nprint("Synthetic worker failure", file=sys.stderr)\nsys.exit(3)\n', encoding='utf-8')
        self.button('Check Incoming').invoke()
        self.finish_action()
        self.assertIn('did not complete', self.window.status.get())
        self.assertIn('Synthetic worker failure', self.window.details.get('1.0', 'end'))
        self.assertTrue(all(str(button.cget('state')) == 'normal' for button in self.window.buttons))
        self.assertLess(float(self.window.progress['value']), 100)
        self.assertIn('incomplete', self.window.progress_text.get())

    @unittest.skipIf(os.name == 'nt', 'POSIX cancellation; Windows process-tree integration pending')
    def test_stop_button_terminates_running_worker(self):
        (self.internal / 'audit_materials.py').write_text('import time\nprint("Synthetic slow worker", flush=True)\ntime.sleep(15)\n', encoding='utf-8')
        self.button('Check Incoming').invoke()
        self.window.stop.invoke()
        self.finish_action()
        self.assertTrue(self.runner.cancelled)
        self.assertIn('Stopped', self.window.status.get())

    @unittest.skipIf(os.name == 'nt', 'POSIX cancellation; Windows process-tree integration pending')
    def test_close_during_run_can_be_declined_or_stop_the_worker(self):
        (self.internal / 'audit_materials.py').write_text('import time\ntime.sleep(15)\n', encoding='utf-8')
        self.button('Check Incoming').invoke()
        self.ask.return_value = False
        self.window.close()
        self.assertTrue(self.runner.busy)
        self.assertFalse(self.window.closing)
        self.ask.return_value = True
        self.window.close()
        self.assertTrue(self.window.closing)
        self.root.after(5000, self.root.quit)
        self.root.mainloop()
        self.assertFalse(self.runner.busy)
        try:
            exists = self.root.winfo_exists()
        except tk.TclError:
            exists = False
        self.assertFalse(exists)


if __name__ == '__main__':
    unittest.main()
