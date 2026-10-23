"""Run with: python3 -m unittest discover -s tests -v

Only synthetic files and fake external-tool executables are used; no malware.
"""
import csv
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

SOURCE = Path(__file__).resolve().parents[1]


class PortableAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ['scan_library.py', 'audit_materials.py', 'sanitize_with_dangerzone.py', 'library_paths.py', 'library_style.py', 'library_records.py', 'intake_workflow.py', 'catalogue_settings.py', 'operation_progress.py']:
            shutil.copy2(SOURCE / name, self.root / name)
        (self.root / 'Incoming').mkdir()
        (self.root / 'Books' / 'Notes').mkdir(parents=True)
        (self.root / 'Incoming' / 'mismatch.pdf').write_bytes(b'MZnot a pdf')
        (self.root / 'Incoming' / 'paper.epub').write_bytes(b'not a zip')
        (self.root / 'Incoming' / 'active.pdf').write_bytes(b'%PDF-1.4\n% no harm, test string only\n%%EOF')
        (self.root / 'Books' / 'Notes' / 'drawing.svg').write_text('<svg/>', encoding='utf-8')
        with zipfile.ZipFile(self.root / 'Books' / 'Notes' / 'example.epub', 'w') as z:
            z.writestr('META-INF/container.xml',
                       '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                       '<rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>')
            z.writestr('OEBPS/book.opf',
                       '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
                       'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                       '<dc:title>Synthetic EPUB</dc:title><dc:creator>Test Author</dc:creator>'
                       '</metadata></package>')

    def call(self, program, *args, env=None):
        return subprocess.run([sys.executable, str(self.root / program), *args],
                              capture_output=True, text=True, check=False, env=env)

    def load_csv(self, name):
        with (self.root / name).open(encoding='utf-8-sig', newline='') as stream:
            return list(csv.DictReader(stream))

    def fake_executable(self, name, source):
        path = self.root / name
        path.write_text('#!' + sys.executable + '\n' + source, encoding='utf-8')
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def test_default_audit_no_external_tools(self):
        result = self.call('audit_materials.py', '--no-clamav')
        self.assertEqual(result.returncode, 0, result.stderr)
        reports = {r['relative_path']: r for r in self.load_csv('intake_audit.csv')}
        self.assertEqual(reports['Incoming/mismatch.pdf']['status'], 'REVIEW')
        self.assertIn('PDF signature mismatch', reports['Incoming/mismatch.pdf']['review_reasons'])
        self.assertIn('EPUB container signature mismatch', reports['Incoming/paper.epub']['review_reasons'])
        self.assertEqual(reports['Incoming/active.pdf']['clamav'], 'NOT RUN')
        self.assertIn('ClamAV', reports['Incoming/active.pdf']['checks_not_run'])
        self.assertTrue(reports['Incoming/active.pdf']['sha256'])
        self.assertTrue((self.root / 'Incoming' / 'active.pdf').exists())

    def test_audit_details_explain_builtin_checks_and_missing_tools(self):
        environment = dict(os.environ, PATH=str(self.root / 'no-installed-tools'))
        before = {path: path.read_bytes() for path in (self.root / 'Incoming').iterdir()}
        result = self.call('audit_materials.py', env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Auditing 3 file(s) in Incoming', result.stdout)
        self.assertIn('short file signatures, empty files and extension mismatches', result.stdout)
        self.assertIn('SHA-256 fingerprints identify bytes, not malware', result.stdout)
        self.assertIn('ClamAV: malware-signature scan; NOT RUN (not installed)', result.stdout)
        self.assertIn('PDFiD: PDF active/embedded feature indicators; NOT RUN', result.stdout)
        self.assertIn('EPUBCheck: EPUB conformance validation, not a malware scan; NOT RUN', result.stdout)
        self.assertIn('3/3 signature/empty-file checks; 3/3 SHA-256 fingerprints completed', result.stdout)
        self.assertIn('ClamAV: 0 attempted (errors: 0); 3 unavailable; 0 skipped of 3 applicable files', result.stdout)
        self.assertIn('PDFiD: 0 attempted (errors: 0); 1 unavailable; 1 skipped of 2 applicable files', result.stdout)
        self.assertIn('Unavailable or skipped checks are incomplete', result.stdout)
        self.assertIn('not a guarantee of safety', result.stdout)
        report = json.loads((self.root / 'intake_audit.json').read_text(encoding='utf-8'))
        self.assertEqual(report['tools'], {'clamav': False, 'pdfid': False, 'epubcheck': False})
        active = next(row for row in report['files'] if row['relative_path'].endswith('active.pdf'))
        self.assertEqual(active['status'], 'LIMITED CHECKS')
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    @unittest.skipIf(os.name == 'nt', 'POSIX executable stubs; existing wrapper tests cover Windows')
    def test_audit_details_distinguish_tool_attempts_errors_and_safeguard_skips(self):
        self.fake_executable('clamscan', '''import pathlib, sys
manifest = next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--file-list='))
for name in pathlib.Path(manifest).read_text(encoding='utf-8').splitlines():
 print(name + (': scan failed ERROR' if name.endswith('active.pdf') else ': OK'))
sys.exit(2)
''')
        self.fake_executable('pdfid', 'import sys\nsys.exit(2)\n')
        self.fake_executable('epubcheck', 'import sys\nsys.exit(1)\n')
        (self.root / 'Incoming' / 'checked.pdf').write_bytes(b'%PDF-1.4\n%%EOF')
        (self.root / 'Incoming' / 'checked.epub').write_bytes(b'PK\x03\x04synthetic')
        environment = dict(os.environ, PATH=str(self.root))
        result = self.call('audit_materials.py', env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ClamAV: 5 attempted (errors: 1); 0 unavailable; 0 skipped of 5 applicable files', result.stdout)
        self.assertIn('PDFiD: 1 attempted (errors: 1); 0 unavailable; 2 skipped of 3 applicable files', result.stdout)
        self.assertIn('EPUBCheck: 1 attempted (errors: 0); 0 unavailable; 1 skipped of 2 applicable files', result.stdout)
        report = {row['relative_path']: row for row in self.load_csv('intake_audit.csv')}
        self.assertEqual(report['Incoming/active.pdf']['clamav'], 'ERROR')
        self.assertEqual(report['Incoming/active.pdf']['pdfid'], 'SKIPPED (prior warning)')
        self.assertEqual(report['Incoming/mismatch.pdf']['pdfid'], 'SKIPPED (prior warning)')
        self.assertEqual(report['Incoming/checked.pdf']['pdfid'], 'ERROR')
        self.assertEqual(report['Incoming/checked.epub']['epubcheck'], 'Validation errors')

    @unittest.skipIf(os.name == "nt", "POSIX executable stubs; portable wrapper tests cover Windows")
    def test_audit_with_fake_tools(self):
        # The mock antivirus is a deterministic program with documented 0/1/2 results.
        fake_clam = self.fake_executable('fake_clamscan', '''import pathlib, sys
manifest = next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--file-list='))
for name in pathlib.Path(manifest).read_text(encoding='utf-8').splitlines():
 print(name + (': Synthetic.Test FOUND' if name.endswith('active.pdf') else ': OK'))
sys.exit(1)
''')
        fake_pdfid = self.root / 'fake_pdfid.py'
        fake_pdfid.write_text('''import sys
print('PDFiD 0.2.10 ' + sys.argv[-1])
for marker in ('/JS', '/JavaScript', '/AA', '/OpenAction', '/Launch', '/EmbeddedFile', '/RichMedia'):
 print(marker + ('  1' if marker == '/JavaScript' and sys.argv[-1].endswith('active.pdf') else '  0'))
''', encoding='utf-8')
        fake_epub = self.fake_executable('fake_epubcheck', '''import sys
print('Validation errors')
sys.exit(1)
''')
        # Exercise the external-program wrappers directly; their invocation does not use a shell.
        sys.path.insert(0, str(self.root))
        try:
            import audit_materials as am
            # Module caching is acceptable: functions do not depend on __file__ here.
            self.assertEqual(am.check_clamav(self.root / 'Incoming' / 'active.pdf', fake_clam)[0], 'DETECTION')
            self.assertIn('Indicators found', am.check_pdfid(self.root / 'Incoming' / 'active.pdf', str(fake_pdfid))[0])
            self.assertEqual(am.check_epubcheck(self.root / 'Incoming' / 'paper.epub', fake_epub, None)[0], 'Validation errors')
        finally:
            sys.path.pop(0)

    def test_main_scan_excludes_incoming_and_preserves_metadata_corrections(self):
        before = self.call('scan_library.py', '--inventory-only')
        self.assertEqual(before.returncode, 0, before.stderr)
        rows = self.load_csv('catalogue.csv')
        self.assertEqual(len(rows), 2)
        self.assertNotIn('Incoming/', json.dumps(rows))
        self.assertEqual(set(row['format'] for row in rows), {'EPUB', 'SVG'})
        self.assertEqual([r for r in rows if r['format'] == 'EPUB'][0]['read_status'],
                         'Not inspected (inventory-only mode)')
        correction = self.root / 'metadata_corrections.csv'
        correction.write_text('relative_path,title,author,date,identifier\n'
                              'Books/Notes/example.epub,Corrected Title,,,\n', encoding='utf-8')
        self.assertEqual(self.call('scan_library.py').returncode, 0)
        rows = self.load_csv('catalogue.csv')
        ebook = next(r for r in rows if r['format'] == 'EPUB')
        self.assertEqual(ebook['title'], 'Corrected Title')
        self.assertEqual(ebook['embedded_title'], 'Synthetic EPUB')
        self.assertIn('Corrected Title', (self.root / 'library_mobile.html').read_text(encoding='utf-8'))
        self.assertIn('Corrected Title', correction.read_text(encoding='utf-8'))

    @unittest.skipIf(os.name == "nt", "POSIX executable stub; portable command tests cover Windows")
    def test_dangerzone_helper_requires_explicit_incoming_and_never_overwrites(self):
        fake_dz = self.fake_executable('fake_dangerzone', '''import pathlib, sys
args=sys.argv
pathlib.Path(args[args.index('--output-filename')+1]).write_bytes(b'%PDF-1.4\\n%%EOF')
''')
        outside = self.call('sanitize_with_dangerzone.py', 'Books/Notes/example.epub', '--cli', fake_dz)
        self.assertNotEqual(outside.returncode, 0)
        ok = self.call('sanitize_with_dangerzone.py', 'Incoming/active.pdf', '--cli', fake_dz)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertTrue((self.root / 'Clean Copies' / 'active-safe.pdf').exists())
        self.assertTrue((self.root / 'Incoming' / 'active.pdf').exists())
        repeated = self.call('sanitize_with_dangerzone.py', 'Incoming/active.pdf', '--cli', fake_dz)
        self.assertNotEqual(repeated.returncode, 0)


    def test_suspicious_signature_skips_external_pdf_parser(self):
        marker = self.root / 'invoked.txt'
        fake = self.root / 'pdfid.py'
        fake.write_text('''from pathlib import Path
Path(r"''' + str(marker) + '''").write_text("called")
print("PDFiD 0.2.10")
''', encoding='utf-8')
        result = self.call('audit_materials.py', '--no-clamav', '--pdfid', str(fake))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = {r['relative_path']: r for r in self.load_csv('intake_audit.csv')}
        self.assertEqual(report['Incoming/mismatch.pdf']['pdfid'], 'SKIPPED (prior warning)')
        self.assertTrue(marker.exists(), 'Valid-signature PDF should still receive requested PDFiD check')

    def test_root_inventory_does_not_index_project_tools(self):
        result = self.call('scan_library.py', '--folder', '.', '--inventory-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        names = {row['relative_path'] for row in self.load_csv('catalogue.csv')}
        self.assertEqual(names, {'Books/Notes/drawing.svg', 'Books/Notes/example.epub'})

if __name__ == '__main__':
    unittest.main()
