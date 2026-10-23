"""Verify source commands, portable output and source-history privacy boundaries."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

from build_usb import build_folder
from release_check import SOURCE_FILES

ROOT = Path(__file__).resolve().parents[1]


class SourceLayoutTests(unittest.TestCase):
    def test_build_and_inspect_commands_work_outside_the_checkout(self):
        with tempfile.TemporaryDirectory(prefix='pinakeys-source-command-') as directory:
            target = Path(directory).resolve() / 'New USB kit with spaces'
            built = subprocess.run([sys.executable, '-B', '-S', str(ROOT/'tools/build_usb.py'), str(target)],
                                   cwd=directory, capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout+built.stderr)
            checked = subprocess.run([sys.executable, '-B', '-S', str(ROOT/'tools/release_check.py'), str(target)],
                                     cwd=directory, capture_output=True, text=True, timeout=30)
            self.assertEqual(checked.returncode, 0, checked.stdout+checked.stderr)
            self.assertEqual(json.loads(checked.stdout)['kind'], 'empty USB kit')
            app = subprocess.run([sys.executable, '-B', '-S', str(target/'Library Tools/Internal/pinakeys.py'), '--check'],
                                 cwd=directory, capture_output=True, text=True, timeout=20)
            self.assertEqual(app.returncode, 0, app.stdout+app.stderr)
            self.assertFalse(any((target/'Content').iterdir()))
            self.assertFalse(any((target/'Incoming Files').iterdir()))

    def test_test_runner_discovers_cases_outside_the_checkout(self):
        with tempfile.TemporaryDirectory(prefix='pinakeys-runner-command-') as directory:
            report = Path(directory)/'tests.json'
            result = subprocess.run([sys.executable, '-B', '-S', str(ROOT/'tools/run_tests.py'),
                                     '--pattern', 'test_release_safety.py', '--report-json', str(report)],
                                    cwd=directory, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            data = json.loads(report.read_text(encoding='utf-8'))
            self.assertTrue(data['successful'])
            self.assertGreater(data['tests'], 0)

    def assert_links(self, page, boundary):
        for href in re.findall(r'\[[^\]\n]*\]\(([^\s)]+)\)', page.read_text(encoding='utf-8')):
            link = urlsplit(href)
            if link.scheme or link.netloc or not link.path:
                continue
            target = (page.parent/unquote(link.path)).resolve()
            self.assertTrue(target.is_relative_to(boundary), (page, href))
            self.assertTrue(target.is_file(), (page, href))

    def test_source_and_packaged_help_links_resolve_in_their_own_layouts(self):
        for name in SOURCE_FILES:
            if name.endswith('.md'):
                self.assert_links(ROOT/name, ROOT)
        with tempfile.TemporaryDirectory(prefix='pinakeys-help-links-') as directory:
            kit = build_folder(Path(directory).resolve()/'Kit')
            for page in (kit/'Library Tools/Help').glob('*.md'):
                self.assert_links(page, kit)
            guide = (kit/'Library Tools/Help/README.md').read_text(encoding='utf-8')
            self.assertIn('](QUICKSTART.md)', guide)
            notices = (kit/'Library Tools/Help/DEPENDENCIES.md').read_text(encoding='utf-8')
            self.assertIn('](../Internal/vendor/README.md)', notices)

    def test_ignore_rules_allow_source_and_reject_personal_runtime_outputs(self):
        private = {'Books/private.pdf', 'app/catalogue.json', 'app/metadata_corrections.csv',
                   'app/Corrections/library_records.json', 'docs/local-audit.json', 'tools/secret.env'}
        inputs = '\0'.join(sorted(SOURCE_FILES | private))+'\0'
        result = subprocess.run(['git', 'check-ignore', '--no-index', '-z', '--stdin'], input=inputs,
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(result.stdout.strip('\0').split('\0')), private)

    def test_history_allows_flat_paths_but_rejects_unreviewed_historical_files(self):
        with tempfile.TemporaryDirectory(prefix='pinakeys-source-history-') as directory:
            fixture = Path(directory).resolve()/'Source'
            fixture.mkdir()
            def git(*args):
                return subprocess.run(['git', *args], cwd=fixture, capture_output=True,
                                      text=True, check=True, timeout=20)
            git('init', '-b', 'main')
            git('config', 'user.name', 'Pinakeys tests')
            git('config', 'user.email', 'tests@users.noreply.github.com')
            git('config', 'commit.gpgsign', 'false')
            git('config', 'core.hooksPath', '.git/no-hooks')
            (fixture/'scan_library.py').write_text('# Synthetic flat source fixture\n', encoding='utf-8')
            git('add', '--all');git('commit', '-m', 'Flat source fixture')
            (fixture/'scan_library.py').unlink()
            for name in SOURCE_FILES:
                target = fixture/name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT/name, target)
            git('add', '--all');git('commit', '-m', 'Structured source fixture')
            command = [sys.executable, '-B', '-S', '-c',
                       'import sys,json; sys.path.insert(0,sys.argv[1]); from release_gate import source_history; print(json.dumps(source_history()))',
                       str(fixture/'tools')]
            checked = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=45)
            self.assertEqual(checked.returncode, 0, checked.stdout+checked.stderr)
            self.assertEqual(json.loads(checked.stdout)['commits'], 2)
            unexpected = fixture/'personal-catalogue.csv'
            unexpected.write_text('Synthetic private fixture only\n', encoding='utf-8')
            git('add', '--force', unexpected.name);git('commit', '-m', 'Unreviewed path fixture')
            unexpected.unlink()
            git('add', '--update');git('commit', '-m', 'Remove fixture path')
            rejected = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=45)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn('Unreviewed historical path: personal-catalogue.csv', rejected.stderr)
