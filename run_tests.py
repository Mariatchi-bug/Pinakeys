"""Run the synthetic suite, isolating native Tk cases in fresh processes.

The application creates one Tk interpreter per process. Repeated test-only Tk
interpreters can crash old Cocoa Tk or be collected on a later worker thread.
Every GUI assertion still runs; each child has its own interpreter and timeout.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

GUI_CLASSES = {'WindowActionTests', 'ReviewWindowTests', 'ButtonTests', 'IntakeWidgetTests'}
ROOT = Path(__file__).resolve().parent


def settle_window(root, ready):
    """Let native map/layout events run before checking geometry."""
    deadline = time.monotonic() + 3
    def poll():
        if ready() or time.monotonic() >= deadline:
            root.quit()
        else:
            root.after(10, poll)
    root.after(10, poll)
    root.mainloop()


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


class IsolatedGuiCase(unittest.TestCase):
    def __init__(self, name):
        super().__init__()
        self.name = name

    def id(self):
        return self.name

    def __str__(self):
        return self.name + ' [isolated Tk]'

    def runTest(self):
        with tempfile.TemporaryDirectory(prefix='pinakeys-gui-result-') as directory:
            result_path = Path(directory) / 'result.json'
            command = [sys.executable, '-B', str(ROOT / 'run_tests.py'),
                       '--gui-case', self.name, '--report-json', str(result_path)]
            try:
                result = subprocess.run(command, cwd=ROOT, capture_output=True,
                                        text=True, encoding='utf-8', errors='replace', timeout=60)
            except subprocess.TimeoutExpired as exc:
                self.fail('Native Tk case exceeded 60 seconds: ' + self.name)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-12000:])
            report = json.loads(result_path.read_text(encoding='utf-8'))
            if report['skipped']:
                self.skipTest(report['skipped'][0]['reason'])
            self.assertEqual(report['tests'], 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gui-case', help=argparse.SUPPRESS)
    parser.add_argument('--pattern', default='test_*.py')
    parser.add_argument('--report-json')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / 'tests'))
    if args.gui_case:
        suite = unittest.defaultTestLoader.loadTestsFromName(args.gui_case)
    else:
        discovered = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=args.pattern)
        suite = unittest.TestSuite(IsolatedGuiCase(case.id()) if type(case).__name__ in GUI_CLASSES else case
                                   for case in cases(discovered))
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if args.report_json:
        report = {'tests': result.testsRun, 'failures': len(result.failures),
                  'errors': len(result.errors), 'seconds': time.monotonic() - started,
                  'skipped': [{'test': test.id(), 'reason': reason} for test, reason in result.skipped],
                  'python': sys.version.split()[0], 'platform': sys.platform,
                  'successful': result.wasSuccessful(), 'isolated_gui_cases': not bool(args.gui_case)}
        Path(args.report_json).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    from library_paths import configure_console
    configure_console()
    raise SystemExit(main())
