"""Regression checks for portable settings and non-destructive lock recovery."""
import ctypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from build_usb import build_folder
from catalogue_settings import Selection
from library_paths import LibraryPaths
from library_records import Records, RecordError, encoded, process_running, proof
from pdf_followup import copy_verified, VerificationError
from review_library import ReviewStore, ReviewError


class ReleaseSafetyTests(unittest.TestCase):
    def check_proof_sources(self, offset=0, changed_field=None):
        """Exercise real reads while emulating Windows' stat/fstat clocks."""
        with tempfile.TemporaryDirectory(prefix='pinakeys-stat-') as directory:
            root = build_folder(Path(directory).resolve() / 'Kit')
            layout = LibraryPaths(root, True)
            relative = 'Incoming Files/sample.pdf'
            source = root / relative
            payload = b'%PDF-1.7\r\n\x1aSynthetic bytes\x00\r\n'
            source.write_bytes(payload)
            row = {'relative_path': relative, 'size_bytes': len(payload),
                   'modified': datetime.fromtimestamp(source.stat().st_mtime).isoformat(timespec='seconds'),
                   'sha256': hashlib.sha256(payload).hexdigest(),
                   'clamav': 'No detection (not proof of safety)', 'file_type_check': 'PDF header present'}
            (layout.reports / 'catalogue.json').write_text(json.dumps([row]), encoding='utf-8')
            store = ReviewStore(layout)
            original = os.fstat
            for operation, error in ((lambda: proof(source), RecordError),
                                     (lambda: store._file_proof(relative), ReviewError),
                                     (lambda: copy_verified(root, row, Path(directory) / 'copy.pdf'), VerificationError)):
                calls = 0
                def handle_info(fd):
                    nonlocal calls
                    calls += 1
                    info = original(fd)
                    values = {name: getattr(info, name) for name in
                              ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
                    values['st_ctime_ns'] += offset
                    if changed_field and calls >= 2:
                        values[changed_field] += 1
                    return SimpleNamespace(**values)
                with self.subTest(operation=operation), patch('os.fstat', side_effect=handle_info):
                    if changed_field:
                        with self.assertRaises(error):operation()
                    else:
                        result = operation()
                        if isinstance(result, dict):self.assertEqual(result['sha256'], row['sha256'])
                        elif hasattr(result, 'digest'):self.assertEqual(result.digest, row['sha256'])
                        else:self.assertEqual((Path(directory) / 'copy.pdf').read_bytes(), payload)
                self.assertEqual(source.read_bytes(), payload)

    def test_path_and_handle_ctime_may_have_different_stable_meanings(self):
        self.check_proof_sources(offset=1234567)

    def test_handle_change_time_still_detects_changes_during_read(self):
        self.check_proof_sources(offset=1234567, changed_field='st_ctime_ns')

    def test_handle_replacement_still_detected(self):
        self.check_proof_sources(offset=1234567, changed_field='st_ino')

    def test_live_process_is_not_signalled_and_dead_process_lock_is_recoverable(self):
        with tempfile.TemporaryDirectory(prefix='pinakeys-lock-test-') as directory:
            root = build_folder(Path(directory).resolve() / 'Kit')
            records = Records(LibraryPaths(root, True))
            records.folder.mkdir(exist_ok=True)
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            try:
                records.lock_path.write_bytes(encoded({'host': socket.gethostname(), 'pid': child.pid}))
                self.assertTrue(process_running(child.pid))
                with self.assertRaisesRegex(RecordError, 'still running'):
                    records.release_interrupted_lock()
                self.assertIsNone(child.poll())
                self.assertTrue(records.lock_path.exists())
            finally:
                child.terminate()
                child.wait(timeout=5)
            self.assertFalse(process_running(child.pid))
            records.release_interrupted_lock()
            self.assertFalse(records.lock_path.exists())

    def test_windows_query_never_uses_kill_and_closes_handles(self):
        for state, expected in [(0, False), (258, True), (0xFFFFFFFF, None)]:
            kernel = MagicMock()
            kernel.OpenProcess.return_value = 123
            kernel.WaitForSingleObject.return_value = state
            with self.subTest(state=state), patch('library_records.os.name', 'nt'), \
                    patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
                    patch('library_records.os.kill') as kill:
                if expected is None:
                    with self.assertRaises(RecordError):process_running(456)
                else:
                    self.assertEqual(process_running(456), expected)
                kernel.OpenProcess.assert_called_once_with(0x00100000, False, 456)
                kernel.CloseHandle.assert_called_once_with(123)
                kill.assert_not_called()

    def test_unknown_windows_process_state_and_invalid_pid_fail_closed(self):
        kernel = MagicMock(); kernel.OpenProcess.return_value = 0
        for error in (87, 5, 123):
            with self.subTest(error=error), patch('library_records.os.name', 'nt'), \
                    patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
                    patch.object(ctypes, 'get_last_error', return_value=error, create=True):
                if error == 87:self.assertFalse(process_running(456))
                else:
                    with self.assertRaises(RecordError):process_running(456)
        for pid in (0, -1, True, '123', None):
            with self.subTest(pid=pid), self.assertRaises(RecordError):process_running(pid)

    def test_unicode_selection_is_explicit_utf8_and_survives_reopen(self):
        with tempfile.TemporaryDirectory(prefix='pinakeys-utf8-') as directory:
            root = build_folder(Path(directory).resolve() / 'Kit')
            name = 'Café — 資料';(root / name).mkdir()
            layout = LibraryPaths(root, True)
            Selection(layout).save([name], ['*résumé*'])
            original = Path.read_text
            def checked(path, *args, **kwargs):
                self.assertEqual(kwargs.get('encoding'), 'utf-8')
                return original(path, *args, **kwargs)
            with patch.object(Path, 'read_text', checked):
                selection = Selection(layout)
            self.assertEqual(selection.data['folders'], [name])
            self.assertEqual(selection.data['exclude'], ['*résumé*'])

    def test_redirected_worker_console_is_utf8_even_with_legacy_encoding(self):
        env = os.environ.copy();env['PYTHONIOENCODING'] = 'ascii'
        result = subprocess.run([sys.executable, '-B', '-c',
            "from library_paths import configure_console; configure_console(); print('Café — 資料')"],
            cwd=Path(__file__).resolve().parents[1] / 'app', env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode('utf-8').strip(), 'Café — 資料')
