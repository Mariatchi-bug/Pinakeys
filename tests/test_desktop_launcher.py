"""Launcher/refresh behavior against synthetic folders. No real USB or GUI actions."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
import zipfile

from build_usb import build_folder
from pinakeys import ActionRunner


class DesktopLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pinakeys desktop test ')
        self.addCleanup(self.temp.cleanup)
        self.root = build_folder(Path(self.temp.name).resolve() / 'USB with spaces')
        self.internal = self.root / 'Library Tools/Internal'
        self.runner = ActionRunner(self.internal / 'pinakeys.py', python=sys.executable)
        self.addCleanup(self.cleanup_runner)
        self.epub = self.root / 'Content/example.epub'
        self.write_epub('Embedded title')
        (self.root / 'Incoming Files/test.pdf').write_bytes(b'MZsynthetic mismatch')

    def cleanup_runner(self):
        if self.runner.busy:
            self.runner.cancel()
            self.runner.process.wait(timeout=3)
        self.runner.close()

    def write_epub(self, title):
        with zipfile.ZipFile(self.epub, 'w') as z:
            z.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
            z.writestr('book.opf', '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                       f'<dc:title>{title}</dc:title><dc:creator>Author</dc:creator><dc:identifier>test-id</dc:identifier></metadata></package>')

    def run_action(self, action):
        self.runner.start(action)
        code = self.runner.process.wait(timeout=10)
        self.assertEqual(code, 0, self.runner.read_output())
        return self.runner.read_output()

    def row(self):
        return json.loads((self.runner.layout.reports / 'catalogue.json').read_text(encoding='utf-8'))[0]

    def test_starting_launcher_does_not_scan_or_create_reports(self):
        self.assertFalse(self.runner.busy)
        self.assertFalse((self.runner.layout.reports / 'catalogue.json').exists())
        with self.assertRaises(ValueError):
            self.runner.start('arbitrary command')

    def test_refresh_keeps_metadata_and_identifier_across_repeated_runs(self):
        self.run_action('metadata')
        for iteration in range(2):
            self.run_action('refresh')
            row = self.row()
            self.assertEqual(row['title'], 'Embedded title')
            self.assertEqual(row['author'], 'Author')
            self.assertEqual(row['identifier'], 'test-id')
            self.assertEqual(row['embedded_identifier'], 'test-id')
            self.assertIn('retained', row['read_status'])

    def test_removed_manual_override_does_not_become_embedded_metadata(self):
        correction = self.runner.layout.corrections
        correction.write_text('relative_path,title,author,date,identifier\nContent/example.epub,Manual title,,,manual-id\n', encoding='utf-8')
        original = self.epub.read_bytes()
        self.run_action('metadata')
        self.assertEqual(self.row()['title'], 'Manual title')
        correction.write_text('relative_path,title,author,date,identifier\n', encoding='utf-8')
        self.run_action('refresh')
        self.assertEqual(self.row()['title'], 'Embedded title')
        self.assertEqual(self.row()['identifier'], 'test-id')
        self.assertEqual(self.epub.read_bytes(), original)

    def test_changed_file_does_not_inherit_previous_extracted_metadata(self):
        self.run_action('metadata')
        self.write_epub('A substantially different embedded title')
        self.run_action('refresh')
        self.assertEqual(self.row()['title'], '')
        self.assertEqual(self.row()['read_status'], 'Not inspected (inventory-only mode)')
        self.run_action('metadata')
        self.assertEqual(self.row()['title'], 'A substantially different embedded title')

    def test_corrupt_previous_catalogue_survives_failed_refresh(self):
        target = self.runner.layout.reports / 'catalogue.json'
        target.write_bytes(b'unreadable previous snapshot')
        self.runner.start('refresh')
        self.assertNotEqual(self.runner.process.wait(timeout=5), 0)
        self.assertEqual(target.read_bytes(), b'unreadable previous snapshot')
        self.assertFalse(self.runner.layout.corrections.exists())

    def test_readonly_diagnostic_and_bundle_structure(self):
        contents = self.root / 'Library Tools/Mac/Pinakeys.app/Contents'
        data = plistlib.loads((contents / 'Info.plist').read_bytes())
        self.assertEqual(data['CFBundleExecutable'], 'Pinakeys')
        self.assertTrue(os.access(contents / 'MacOS/Pinakeys', os.X_OK))
        result = subprocess.run([sys.executable, str(self.internal / 'pinakeys.py'), '--check'],
                                cwd=self.temp.name, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['EPUB metadata'])
        self.assertFalse((self.runner.layout.reports / 'catalogue.json').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX entry points')
    def test_app_and_linux_entrypoints_pass_arguments_from_paths_with_spaces(self):
        for path in [self.root / 'Library Tools/Mac/Pinakeys.app/Contents/MacOS/Pinakeys',
                     self.root / 'Library Tools/Linux/Open Pinakeys.sh']:
            result = subprocess.run(['sh', str(path), '--check'], cwd=self.temp.name,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['EPUB metadata'])

    @unittest.skipIf(os.name == 'nt', 'POSIX process group cancellation')
    def test_only_one_action_runs_and_cancellation_stops_worker(self):
        (self.internal / 'audit_materials.py').write_text('import time\ntime.sleep(15)\n', encoding='utf-8')
        self.runner.start('audit')
        with self.assertRaises(RuntimeError):
            self.runner.start('refresh')
        self.runner.cancel()
        self.assertNotEqual(self.runner.process.wait(timeout=3), 0)
        self.assertTrue(self.runner.cancelled)


if __name__ == '__main__':
    unittest.main()
