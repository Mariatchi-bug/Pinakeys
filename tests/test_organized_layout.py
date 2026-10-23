"""Exercise the actual packaged layout, using only temporary synthetic files."""
import csv
import hashlib
import importlib.util
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from build_usb import build_folder


class Links(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.hrefs = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.hrefs.extend(value for key, value in attrs if key == 'href')


class OrganizedLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='organized library ')
        self.addCleanup(self.temp.cleanup)
        self.root = build_folder(Path(self.temp.name).resolve() / 'USB with spaces')
        self.internal = self.root / 'Library Tools/Internal'
        self.reports = self.root / 'Library Tools/Reports'
        self.corrections = self.root / 'Library Tools/Corrections/metadata_corrections.csv'
        (self.root / 'Content' / 'notes #1.txt').write_bytes(b'unchanged notes')
        (self.root / 'Content' / 'paper.pdf').write_bytes(b'%PDF-synthetic')
        (self.root / 'Incoming Files' / 'incoming.pdf').write_bytes(b'MZtest fixture')

    def call(self, name, *args):
        return subprocess.run([sys.executable, str(self.internal / name), *args],
                              cwd=self.temp.name, capture_output=True, text=True)

    def test_builder_refuses_existing_folder(self):
        before = (self.root / 'Content/notes #1.txt').read_bytes()
        with self.assertRaises(FileExistsError):
            build_folder(self.root)
        self.assertEqual((self.root / 'Content/notes #1.txt').read_bytes(), before)

    def test_root_stays_simple_and_mobile_links_point_to_originals(self):
        result = self.call('scan_library.py', '--inventory-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        visible = {p.name for p in self.root.iterdir() if not p.name.startswith('.')}
        self.assertEqual(visible, {'Open Library.html', 'Content', 'Incoming Files', 'Library Tools'})
        rows = json.loads((self.reports / 'catalogue.json').read_text(encoding='utf-8'))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['relative_path'].startswith('Content/') for r in rows))
        # Audit generates the report linked by the help page, without a real antivirus.
        audit = self.call('audit_materials.py', '--no-clamav')
        self.assertEqual(audit.returncode, 0, audit.stderr)
        self.assertEqual(len(json.loads((self.reports / 'intake_audit.json').read_text(encoding='utf-8'))['files']), 1)
        for page in [self.root / 'Open Library.html', self.reports / 'library_mobile.html',
                     self.root / 'Library Tools/Start Here.html']:
            links = Links(page.read_text(encoding='utf-8')).hrefs
            self.assertTrue(links)
            for href in links:
                target = (page.parent / unquote(href)).resolve()
                self.assertTrue(target.is_relative_to(self.root), href)
                self.assertTrue(target.is_file(), f'Broken link in {page.name}: {href}')

    def test_root_scan_excludes_tools_reports_and_corrections(self):
        self.assertEqual(self.call('scan_library.py', '--inventory-only').returncode, 0)
        result = self.call('scan_library.py', '--folder', '.', '--inventory-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads((self.reports / 'catalogue.json').read_text(encoding='utf-8'))
        self.assertEqual({r['relative_path'] for r in rows}, {'Content/notes #1.txt', 'Content/paper.pdf'})

    def test_conversion_stays_under_content_and_preserves_original(self):
        spec = importlib.util.spec_from_file_location('organized_conversion', self.internal / 'sanitize_with_dangerzone.py')
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        original = self.root / 'Incoming Files/incoming.pdf'
        before = original.read_bytes()
        def convert(command, **kwargs):
            target = Path(command[command.index('--output-filename') + 1])
            self.assertEqual(target.parent, self.root / 'Content/Clean Copies')
            self.assertNotIn('--archive', command)
            target.write_bytes(b'%PDF-synthetic safe copy')
            return subprocess.CompletedProcess(command, 0)
        with patch.object(helper, 'locate_cli', return_value='fake'), patch.object(helper.subprocess, 'run', side_effect=convert):
            self.assertEqual(helper.main(['Incoming Files/incoming.pdf']), 0)
        self.assertEqual(original.read_bytes(), before)
        self.assertFalse((self.root / 'Clean Copies').exists())

    def test_corrections_remain_byte_identical_after_rescan_and_relocation(self):
        corrections = b'relative_path,title,author,date,identifier\r\nContent/notes #1.txt,My title,,,\r\n'
        self.corrections.write_bytes(corrections)
        material = self.root / 'Content/notes #1.txt'
        digest = hashlib.sha256(material.read_bytes()).hexdigest()
        for iteration in range(2):
            result = self.call('scan_library.py', '--inventory-only')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.corrections.read_bytes(), corrections)
            row = next(r for r in json.loads((self.reports / 'catalogue.json').read_text(encoding='utf-8'))
                       if r['relative_path'].endswith('.txt'))
            self.assertEqual(row['title'], 'My title')
            self.assertEqual(hashlib.sha256((self.root / 'Content/notes #1.txt').read_bytes()).hexdigest(), digest)
            if iteration == 0:
                moved = self.root.with_name('Different drive letter or mount')
                self.root.rename(moved)
                self.root = moved
                self.internal = moved / 'Library Tools/Internal'
                self.reports = moved / 'Library Tools/Reports'
                self.corrections = moved / 'Library Tools/Corrections/metadata_corrections.csv'

    @unittest.skipIf(os.name == 'nt', 'POSIX launchers require sh')
    def test_packaged_mac_and_linux_launchers_and_failure_status(self):
        for platform, suffix in [('Mac', '.command'), ('Linux', '.sh')]:
            for action in ['Update Catalogue', 'Safety Audit']:
                launcher = self.root / 'Library Tools' / platform / (action + suffix)
                args = ['--no-clamav'] if action == 'Safety Audit' else []
                result = subprocess.run(['sh', str(launcher), *args], cwd=self.temp.name,
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(['sh', str(self.root / 'Library Tools' / platform / ('Update Catalogue' + suffix)),
                                     '--folder', 'does-not-exist'], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
        rows = json.loads((self.reports / 'catalogue.json').read_text(encoding='utf-8'))
        self.assertIn('Not inspected (inventory-only mode)', [r['read_status'] for r in rows])

    def test_report_folder_symlink_outside_drive_is_rejected(self):
        if os.name == 'nt':
            self.skipTest('Creating symlinks may require Windows privileges')
        self.reports.rmdir()
        self.reports.symlink_to(Path(self.temp.name))
        result = self.call('scan_library.py', '--inventory-only')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((Path(self.temp.name) / 'catalogue.json').exists())


if __name__ == '__main__':
    unittest.main()
