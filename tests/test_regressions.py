"""Synthetic regression cases; never scan or modify a real library."""
import csv
import hashlib
from html.parser import HTMLParser
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from build_usb import RUNTIME, source_file

SOURCE = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, source_file(name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='portable index test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'USB with spaces'
        self.root.mkdir()
        for name in RUNTIME:
            shutil.copy2(source_file(name), self.root / name)
        for path in (SOURCE / 'tools').glob('*.py'):
            shutil.copy2(path, self.root / path.name)
        (self.root / 'Content').mkdir()
        (self.root / 'Incoming Files').mkdir()
        (self.root / 'Content' / 'notes.txt').write_bytes(b'local notes')
        (self.root / 'Incoming Files' / 'untrusted.pdf').write_bytes(b'MZsynthetic')

    def run_script(self, name, *args):
        return subprocess.run([sys.executable, str(self.root / name), *args],
                              cwd=self.temp.name, capture_output=True, text=True)

    def rows(self):
        return json.loads((self.root / 'catalogue.json').read_text(encoding='utf-8'))

    def test_content_default_excludes_both_intake_names(self):
        (self.root / 'Incoming').mkdir()
        (self.root / 'Incoming' / 'other.pdf').write_bytes(b'fake')
        (self.root / 'Content' / 'Incoming Files').mkdir()
        (self.root / 'Content' / 'Incoming Files' / 'nested.pdf').write_bytes(b'fake')
        (self.root / 'camera').mkdir()
        (self.root / 'camera' / 'photo.jpg').write_bytes(b'fake')
        result = self.run_script('scan_library.py', '--inventory-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r['relative_path'] for r in self.rows()], ['Content/notes.txt'])
        result = self.run_script('scan_library.py', '--folder', '.', '--inventory-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({r['relative_path'] for r in self.rows()},
                         {'Content/notes.txt', 'camera/photo.jpg'})

    def test_ambiguous_or_missing_default_preserves_existing_reports(self):
        report = self.root / 'catalogue.json'
        report.write_bytes(b'previous report')
        (self.root / 'Books').mkdir()
        self.assertNotEqual(self.run_script('scan_library.py').returncode, 0)
        (self.root / 'Books').rmdir()
        shutil.rmtree(self.root / 'Content')
        self.assertNotEqual(self.run_script('scan_library.py').returncode, 0)
        self.assertEqual(report.read_bytes(), b'previous report')
        self.assertFalse((self.root / 'metadata_corrections.csv').exists())

    def test_explicit_intake_scan_rejected_without_writes(self):
        result = self.run_script('scan_library.py', '--folder', 'Incoming Files')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'catalogue.json').exists())

    def test_audit_defaults_to_current_intake_and_rejects_ambiguity(self):
        source = self.root / 'Incoming Files' / 'untrusted.pdf'
        before = source.read_bytes()
        result = self.run_script('audit_materials.py', '--no-clamav')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.root / 'intake_audit.json'
        data = json.loads(report.read_text(encoding='utf-8'))
        self.assertEqual(data['scanned_folder'], 'Incoming Files')
        self.assertEqual(len(data['files']), 1)
        self.assertEqual(data['files'][0]['status'], 'REVIEW')
        summary = result.stdout.split('Audit summary', 1)[1]
        self.assertIn('1 file needs your attention', summary)
        self.assertIn('Checks are incomplete for 1 of 1 files (including flagged files)', summary)
        self.assertIn('intake_audit.csv', summary)
        self.assertEqual(source.read_bytes(), before)
        snapshot = report.read_bytes()
        (self.root / 'Incoming').mkdir()
        self.assertNotEqual(self.run_script('audit_materials.py', '--no-clamav').returncode, 0)
        self.assertEqual(report.read_bytes(), snapshot)

    def test_empty_files_and_inventory_status_are_visible(self):
        (self.root / 'Content' / 'empty.bin').write_bytes(b'')
        (self.root / 'Content' / 'paper.pdf').write_bytes(b'%PDF-synthetic')
        result = self.run_script('scan_library.py', '--inventory-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        empty = next(r for r in self.rows() if r['filename'] == 'empty.bin')
        self.assertIn('Empty file', empty['review_flags'])
        for name in ('library_index.html', 'library_mobile.html', 'catalogue.txt'):
            text = (self.root / name).read_text(encoding='utf-8')
            self.assertIn('Not inspected (inventory-only mode)', text)
            self.assertIn('Empty file', text)
        self.assertEqual(module('audit_materials').check_type(self.root / 'Content' / 'empty.bin')[0], 'Empty file')

    def test_corrections_and_original_bytes_survive_relocation(self):
        original = self.root / 'Content' / 'notes.txt'
        before = hashlib.sha256(original.read_bytes()).hexdigest()
        corrections = b'relative_path,title,author,date,identifier\r\nContent\\notes.txt,Manual title,Author,,\r\n'
        (self.root / 'metadata_corrections.csv').write_bytes(corrections)
        for iteration in range(2):
            result = self.run_script('scan_library.py', '--inventory-only')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.rows()[0]['title'], 'Manual title')
            self.assertEqual((self.root / 'metadata_corrections.csv').read_bytes(), corrections)
            self.assertEqual(hashlib.sha256((self.root / 'Content' / 'notes.txt').read_bytes()).hexdigest(), before)
            if iteration == 0:
                moved = self.root.with_name('Different mount point')
                self.root.rename(moved)
                self.root = moved

    def test_duplicate_flags_require_matching_bytes(self):
        for name, content in [('one.bin', b'1234'), ('two.bin', b'1234'), ('three.bin', b'abcd')]:
            (self.root / 'Content' / name).write_bytes(content)
        self.assertEqual(self.run_script('scan_library.py', '--inventory-only', '--verify').returncode, 0)
        duplicates = {r['filename'] for r in self.rows() if 'Exact duplicate' in r['review_flags']}
        self.assertEqual(duplicates, {'one.bin', 'two.bin'})

    def test_missing_pdf_dependency_is_not_reported_as_corruption(self):
        scanner = module('scan_library')
        with patch.object(scanner, 'pymupdf', None):
            with self.assertRaisesRegex(RuntimeError, 'not installed'):
                scanner.extract_pdf(Path('unused.pdf'))

    def test_pdfid_incomplete_or_failed_output_is_an_error(self):
        audit = module('audit_materials')
        for output in ('PDFiD 0.2.10\n', 'error in PDFiD', 'PDFiD 0.2.10\n/JS 0\n'):
            with patch.object(audit, 'run_tool', return_value=subprocess.CompletedProcess([], 0, output, '')):
                self.assertEqual(audit.check_pdfid(Path('file.pdf'), 'pdfid')[0], 'ERROR')
        output = 'PDFiD 0.2.10\n' + '\n'.join(x + ' 0' for x in audit.PDF_INDICATORS)
        with patch.object(audit, 'run_tool', return_value=subprocess.CompletedProcess([], 0, output, '')):
            self.assertIn('No selected indicators', audit.check_pdfid(Path('file.pdf'), 'pdfid')[0])

    def test_tool_exit_codes_are_not_confused_with_success(self):
        audit = module('audit_materials')
        for code, expected in [(0, 'No detection'), (1, 'DETECTION'), (2, 'ERROR')]:
            output = 'file.pdf: ' + {0: 'OK', 1: 'Synthetic.Test FOUND', 2: 'scan failed ERROR'}[code]
            with patch.object(audit, 'run_tool', return_value=subprocess.CompletedProcess([], code, output, '')):
                self.assertTrue(audit.check_clamav(Path('file.pdf'), 'clamscan')[0].startswith(expected))
        with patch.object(audit, 'run_tool', side_effect=subprocess.TimeoutExpired('tool', 1)):
            self.assertEqual(audit.check_epubcheck(Path('file.epub'), 'epubcheck', None)[0], 'ERROR')

    def test_dangerzone_current_intake_command_and_no_overwrite(self):
        helper = module('sanitize_with_dangerzone')
        original = self.root / 'Incoming Files' / 'untrusted.pdf'
        before = original.read_bytes()
        def convert(command, **kwargs):
            self.assertNotIn('--archive', command)
            Path(command[command.index('--output-filename') + 1]).write_bytes(b'%PDF-synthetic')
            return subprocess.CompletedProcess(command, 0)
        with patch.object(helper, 'ROOT', self.root), patch.object(helper, 'locate_cli', return_value='fake'), patch.object(helper.subprocess, 'run', side_effect=convert) as run:
            self.assertEqual(helper.main(['Incoming Files/untrusted.pdf']), 0)
            with self.assertRaises(SystemExit):
                helper.main(['Incoming Files/untrusted.pdf'])
            self.assertEqual(run.call_count, 1)
        self.assertEqual(original.read_bytes(), before)

    @unittest.skipIf(os.name == 'nt', 'POSIX launchers; Windows launcher requires cmd.exe')
    def test_posix_launchers_with_spaces_and_unrelated_working_directory(self):
        for launcher, args, expected in [
            ('Run on Mac.command', ['--inventory-only'], 'catalogue.json'),
            ('Run on Linux.sh', ['--inventory-only'], 'catalogue.json'),
            ('Run safety audit (Mac).command', ['--no-clamav'], 'intake_audit.json'),
            ('Run safety audit (Linux).sh', ['--no-clamav'], 'intake_audit.json'),
        ]:
            result = subprocess.run(['sh', str(self.root / launcher), *args],
                                    cwd=self.temp.name, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((self.root / expected).is_file())
        result = subprocess.run(['sh', str(self.root / 'Run on Mac.command'), '--folder', 'missing'],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def test_export_escapes_metadata_and_preserves_relative_links(self):
        scanner = module('scan_library')
        self.assertEqual(scanner.file_url('Content/a #1.pdf'), 'Content/a%20%231.pdf')
        target = self.root / 'export.csv'
        scanner.write_csv(target, [{'title': '=1+1'}], ['title'])
        with target.open(encoding='utf-8-sig') as stream:
            self.assertEqual(next(csv.DictReader(stream))['title'], "'=1+1")
        self.assertEqual(scanner.e('<script>'), '&lt;script&gt;')

    def test_searchable_titles_keep_characters_used_in_browser_queries(self):
        (self.root / 'metadata_corrections.csv').write_text(
            'relative_path,title,author,date,identifier\n'
            'Content/notes.txt,Straße,Synthetic author,,\n', encoding='utf-8')
        result = self.run_script('scan_library.py', '--inventory-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        searches = []

        class Rows(HTMLParser):
            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if tag == 'tr' and 'data-search' in attributes:
                    searches.append(attributes['data-search'])

        Rows().feed((self.root / 'library_index.html').read_text(encoding='utf-8'))
        self.assertEqual(len(searches), 1)
        self.assertIn('straße', searches[0])


class AuditSummaryTests(unittest.TestCase):
    def setUp(self):
        self.audit = module('audit_materials')

    def row(self, name='sample.pdf', status='LIMITED CHECKS', reason='', **changes):
        row = dict(relative_path='Incoming Files/' + name, sha256='synthetic fingerprint',
                   file_type_check='PDF header present', clamav='NOT RUN', pdfid='NOT RUN',
                   epubcheck='N/A', status=status, review_reasons=reason,
                   checks_not_run='ClamAV | PDFiD')
        row.update(changes)
        return row

    def summary(self, rows):
        return self.audit.format_audit_summary(rows, 'Incoming Files', 'Library Tools/Reports')

    def test_incomplete_total_includes_flagged_files_and_groups_their_reasons(self):
        rows = [self.row() for _ in range(42)]
        rows.extend(self.row(status='REVIEW', reason='Empty file (0 bytes)') for _ in range(3))
        rows.extend(self.row(name='arrival.duckload', status='REVIEW',
                             reason='File extension and signature merit review', pdfid='N/A') for _ in range(3))
        rows.extend(self.row(status='REVIEW', reason='PDF signature mismatch') for _ in range(2))
        text = self.summary(rows)
        self.assertIn('50 files checked', text)
        self.assertIn('8 files need your attention', text)
        self.assertIn('Empty files (0 bytes): 3', text)
        self.assertIn('Unusual file extensions for PDF/ZIP contents: 3', text)
        self.assertIn('Files labelled PDF whose contents do not look like a PDF: 2', text)
        self.assertIn('42 other files had no review warnings from the checks that ran', text)
        self.assertIn('Checks are incomplete for 50 of 50 files (including flagged files)', text)
        self.assertIn('No ClamAV malware scan completed', text)
        self.assertIn('Keep flagged files in Incoming Files', text)
        self.assertIn('Your incoming files were left unchanged', text)

    def test_no_flags_with_missing_tools_does_not_imply_safety(self):
        text = self.summary([self.row()])
        self.assertIn('No review warnings were raised by the checks that ran', text)
        self.assertIn('Checks are incomplete for 1 of 1 files', text)
        self.assertIn('ClamAV, PDFiD', text)
        self.assertIn('not proof of safety', text)
        self.assertNotIn('All applicable audit checks completed', text)

    def test_finished_checks_are_not_a_safety_certificate(self):
        text = self.summary([self.row(status='No issues detected by these checks',
            clamav='No detection (not proof of safety)',
            pdfid='No selected indicators detected (not proof of safety)', checks_not_run='')])
        self.assertIn('All applicable audit checks completed', text)
        self.assertIn('not proof of safety', text)
        self.assertNotIn('Checks are incomplete', text)
        self.assertNotIn('No ClamAV malware scan completed', text)

    def test_errors_and_skipped_checks_count_even_without_missing_tool_labels(self):
        text = self.summary([self.row(status='REVIEW',
            reason='ClamAV returned an error (check signatures or setup) | PDF signature mismatch',
            clamav='ERROR', pdfid='SKIPPED (prior warning)', checks_not_run='')])
        self.assertIn('1 file needs your attention', text)
        self.assertIn('A file can have more than one review reason', text)
        self.assertIn('Checks are incomplete for 1 of 1 files', text)
        self.assertIn('ClamAV, PDFiD', text)
        self.assertNotIn('All applicable audit checks completed', text)

    def test_unreadable_other_file_does_not_claim_pdf_or_epub_checks_apply(self):
        text = self.summary([self.row(name='notes.txt', status='REVIEW',
            reason='Could not read file: PermissionError', file_type_check='ERROR', sha256='',
            pdfid='NOT RUN', epubcheck='NOT RUN', checks_not_run='')])
        self.assertIn('Checks not run or not completed: ClamAV.', text)
        self.assertIn('PermissionError', text)
        self.assertIn('Checks are incomplete', text)

    def test_detection_is_reported_without_being_confused_with_a_failed_scan(self):
        text = self.summary([self.row(name='sample.txt', status='REVIEW',
            reason='ClamAV flagged file; do not open original', clamav='DETECTION',
            pdfid='N/A', checks_not_run='')])
        self.assertIn('ClamAV flagged file; do not open original', text)
        self.assertIn('1 file needs your attention', text)
        self.assertNotIn('No ClamAV malware scan completed', text)

    def test_empty_intake_is_not_reported_as_a_successful_security_check(self):
        text = self.summary([])
        self.assertIn('No files were found to check', text)
        self.assertIn('choose Check Incoming again', text)
        self.assertNotIn('All applicable audit checks completed', text)
        self.assertNotIn('No review warnings', text)


class ClamBatchTests(unittest.TestCase):
    def setUp(self):
        self.audit = module('audit_materials')
        self.temp = tempfile.TemporaryDirectory(prefix='clam batch test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.paths = [self.root/name for name in ('Café notes.txt', 'sample.pdf', 'empty.txt')]
        for path in self.paths:
            path.write_text('Synthetic input', encoding='utf-8')

    def run_batch(self, output, code=0):
        with patch.object(self.audit, 'run_tool', return_value=subprocess.CompletedProcess([], code, output, '')) as run:
            results = self.audit.check_clamav_batch(self.paths, 'clamscan')
        self.assertEqual(run.call_count, 1)
        return results

    def test_one_process_for_entire_manifest_preserves_names_and_originals(self):
        before = {p:p.read_bytes() for p in self.paths}
        manifests = []
        def tool(argv, timeout):
            self.assertNotIn('--infected', argv)
            self.assertFalse(any(a.startswith(('--remove', '--move', '--copy', '--recursive')) for a in argv))
            manifest = Path(next(a.split('=',1)[1] for a in argv if a.startswith('--file-list=')))
            manifests.append(manifest)
            self.assertEqual(manifest.read_text(encoding='utf-8').splitlines(), list(map(str,self.paths)))
            return subprocess.CompletedProcess(argv, 0, '\n'.join(str(p)+': OK' for p in self.paths), '')
        with patch.object(self.audit, 'run_tool', side_effect=tool) as run:
            results = self.audit.check_clamav_batch(self.paths, 'clamscan')
        self.assertEqual(run.call_count, 1)
        self.assertTrue(all(value[0].startswith('No detection') for value in results.values()))
        self.assertFalse(manifests[0].exists())
        self.assertEqual(before, {p:p.read_bytes() for p in self.paths})

    def test_detection_clean_empty_and_missing_results_stay_separate(self):
        output = f'{self.paths[0]}: Synthetic.Test FOUND\n{self.paths[2]}: Empty file\n'
        results = self.run_batch(output, 1)
        self.assertEqual(results[self.paths[0]][0], 'DETECTION')
        self.assertEqual(results[self.paths[1]][0], 'ERROR')
        self.assertTrue(results[self.paths[2]][0].startswith('No detection'))

    def test_zero_exit_without_per_file_output_is_not_clean(self):
        results = self.run_batch('')
        self.assertTrue(all(value[0] == 'ERROR' for value in results.values()))

    def test_error_and_detection_cannot_be_overwritten_by_later_ok(self):
        output = (f'{self.paths[0]}: scan failed ERROR\n{self.paths[0]}: OK\n'
                  f'{self.paths[1]}: Synthetic.Test FOUND\n{self.paths[1]}: OK\n')
        results = self.run_batch(output, 1)
        self.assertEqual(results[self.paths[0]][0], 'ERROR')
        self.assertEqual(results[self.paths[1]][0], 'DETECTION')

    def test_partial_error_does_not_discard_other_completed_scans(self):
        output = f'{self.paths[0]}: OK\n{self.paths[1]}: permission denied ERROR\n'
        results = self.run_batch(output, 2)
        self.assertTrue(results[self.paths[0]][0].startswith('No detection'))
        self.assertEqual(results[self.paths[1]][0], 'ERROR')
        self.assertEqual(results[self.paths[2]][0], 'ERROR')

    def test_timeout_preserves_detection_but_does_not_claim_clean_results(self):
        output = f'{self.paths[0]}: Synthetic.Test FOUND\n{self.paths[1]}: OK\n'.encode()
        with patch.object(self.audit, 'run_tool', side_effect=subprocess.TimeoutExpired('clamscan',1,output=output)):
            results = self.audit.check_clamav_batch(self.paths, 'clamscan')
        self.assertEqual(results[self.paths[0]][0], 'DETECTION')
        self.assertEqual(results[self.paths[1]][0], 'ERROR')

    def test_start_failure_unknown_exit_and_contradictory_exit_are_incomplete(self):
        with patch.object(self.audit, 'run_tool', side_effect=OSError('No executable')):
            results = self.audit.check_clamav_batch(self.paths, 'clamscan')
        self.assertTrue(all(value[0] == 'ERROR' for value in results.values()))
        for code in (-9,3,1):
            with self.subTest(code=code):
                results = self.run_batch('\n'.join(str(p)+': OK' for p in self.paths), code)
                self.assertTrue(all(value[0] == 'ERROR' for value in results.values()))

    def test_empty_and_disabled_audits_do_not_start_clamav(self):
        with patch.object(self.audit, 'run_tool') as run:
            self.assertEqual(self.audit.check_clamav_batch([], 'clamscan'), {})
            results = self.audit.check_clamav_batch(self.paths, None)
            self.assertTrue(all(value == ('NOT RUN','') for value in results.values()))
        run.assert_not_called()

    def test_newline_filename_cannot_inject_another_manifest_entry_or_result(self):
        bad = self.root/'bad\nforged: OK.txt'
        with patch.object(self.audit, 'run_tool') as run:
            results = self.audit.check_clamav_batch([bad], 'clamscan')
        run.assert_not_called()
        self.assertEqual(results[bad][0], 'ERROR')

    def test_longer_colon_filename_is_not_assigned_to_shorter_name(self):
        shorter = self.root/'Café'
        longer = self.root/'Café: notes.txt'
        output = f'{shorter}: OK\n{longer}: Synthetic.Test FOUND\n'
        with patch.object(self.audit, 'run_tool', return_value=subprocess.CompletedProcess([],1,output,'')):
            results = self.audit.check_clamav_batch([shorter,longer], 'clamscan')
        self.assertTrue(results[shorter][0].startswith('No detection'))
        self.assertEqual(results[longer][0], 'DETECTION')

    def test_changed_file_prevents_pdf_parser_and_retains_detection(self):
        incoming = self.root/'Incoming Files'
        incoming.mkdir()
        document = incoming/'sample.pdf'
        document.write_bytes(b'%PDF-1.4\nOriginal')
        layout = self.audit.library_paths(self.root/'audit_materials.py')
        for detected in (False,True):
            def changed(paths, executable):
                document.write_bytes(document.read_bytes()+b' changed')
                return {document: ('DETECTION','ClamAV flagged file; do not open original') if detected else ('No detection (not proof of safety)','')}
            with patch.object(self.audit,'ROOT',self.root), patch.object(self.audit,'LAYOUT',layout), \
                    patch.object(self.audit.shutil,'which',return_value='synthetic-tool'), \
                    patch.object(self.audit,'check_clamav_batch',side_effect=changed), \
                    patch.object(self.audit,'check_pdfid') as pdfid:
                self.assertEqual(self.audit.main([]),0)
            pdfid.assert_not_called()
            row = json.loads((self.root/'intake_audit.json').read_text(encoding='utf-8'))['files'][0]
            self.assertEqual(row['clamav'], 'DETECTION' if detected else 'ERROR')
            self.assertEqual(row['pdfid'],'SKIPPED (prior warning)')
            self.assertIn('File changed',row['review_reasons'])


class AuditProgressTests(unittest.TestCase):
    def setUp(self):
        self.audit = module('audit_materials')
        self.temp = tempfile.TemporaryDirectory(prefix='audit progress test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_stream_delivers_lines_before_tool_exits_and_preserves_output(self):
        marker = self.root / 'finished'
        script = ('import pathlib,time; print("first",flush=True); time.sleep(.3); '
                  'pathlib.Path(' + repr(str(marker)) + ').touch(); print("last",flush=True)')
        seen = []
        result = self.audit.run_tool([sys.executable, '-u', '-c', script], timeout=5,
                                    on_line=lambda line: seen.append((line, marker.exists())))
        self.assertEqual(seen, [('first', False), ('last', True)])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'first\nlast\n')

    def test_stream_timeout_handles_silent_tool_and_preserves_partial_line(self):
        for output in ('', 'partial output'):
            with self.subTest(output=output):
                started = time.monotonic()
                script = 'import time; print(' + repr(output) + ',end="",flush=True); time.sleep(10)'
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    self.audit.run_tool([sys.executable, '-u', '-c', script], timeout=.3, on_line=lambda line: None)
                self.assertEqual(caught.exception.output, output)
                self.assertLess(time.monotonic() - started, 3)

    def test_private_events_escape_filename_and_never_overwrite_an_existing_file(self):
        path = self.root / 'events'
        progress = self.audit.AuditProgress(path)
        name = 'Café\n{"stage":3,"state":"complete"}.txt'
        progress.emit(2, 'running', 0, 1, name)
        progress.close()
        lines = path.read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])['file'], name)
        with self.assertRaises(FileExistsError):
            self.audit.AuditProgress(path)
        self.assertEqual(path.read_text(encoding='utf-8').splitlines(), lines)

    def test_live_clamav_counts_known_files_once_and_missing_results_stay_errors(self):
        paths = [self.root / name for name in ('Café: notes.txt', 'empty.txt', 'missing.txt')]
        seen = []
        def tool(argv, timeout, on_line):
            self.assertIn('--verbose', argv)
            output = [f'Scanning {paths[0]}', f'{paths[0]}: OK', f'{paths[0]}: OK',
                      'unknown.txt: OK', f'{paths[1]}: Empty file']
            for line in output:
                on_line(line)
            self.assertEqual(seen, [('file', paths[0]), ('done', paths[0]), ('done', paths[1])])
            return subprocess.CompletedProcess(argv, 0, '\n'.join(output), '')
        with patch.object(self.audit, 'run_tool', side_effect=tool) as run:
            results = self.audit.check_clamav_batch(paths, 'clamscan', progress=lambda *args: seen.append(args))
        self.assertEqual(run.call_count, 1)
        self.assertEqual([path for kind, path in seen if kind == 'done'], paths)
        self.assertEqual(results[paths[2]][0], 'ERROR')

    def test_progress_reader_handles_partial_records_and_rejects_invalid_or_oversized_events(self):
        from pinakeys import ActionRunner
        runner = ActionRunner()
        self.addCleanup(runner.close)
        event = {'version': 1, 'stage': 1, 'state': 'running', 'done': 1, 'total': 2, 'file': 'Café.txt'}
        encoded = json.dumps(event)
        runner.progress_path.write_text(encoded[:20], encoding='utf-8')
        self.assertEqual(runner.read_progress(), [])
        with runner.progress_path.open('a') as stream:
            stream.write(encoded[20:] + '\n')
            for bad in ([], dict(event, done=True), dict(event, done=3), dict(event, stage=4),
                        dict(event, state='safe'), dict(event, file='x' * 4097)):
                stream.write(json.dumps(bad) + '\n')
            stream.write('not JSON\n' + 'x' * 80000 + '\n' + encoded + '\n')
        seen = []
        while runner.progress_pending:
            seen.extend(runner.read_progress())
        self.assertEqual(seen, [event, event])
        runner.log.write_text(encoded + '\n', encoding='utf-8')
        self.assertEqual(runner.read_progress(), [])


if __name__ == '__main__':
    unittest.main()
