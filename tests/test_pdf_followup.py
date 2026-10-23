"""Synthetic PDF structure, preservation, failure and real button tests."""
import hashlib
import importlib.util
from html.parser import HTMLParser
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from build_usb import build_folder
from run_tests import settle_window
from library_paths import library_paths
from pdf_followup import FollowupError, csv_report, html_report, inspect_one, read_selection, select_paths, summary_counts
from pdf_inspection import inspect, Values, InspectionError
from pinakeys import ActionRunner, PinakeysWindow


def synthetic_pdf(extra=b'', objects=(), trailer=b''):
    bodies = [(1, b'<< /Type /Catalog /Pages 2 0 R ' + extra + b' >>'),
              (2, b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>'),
              (3, b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>'), *objects]
    result = b'%PDF-1.7\n'
    offsets = {}
    for number, body in bodies:
        offsets[number] = len(result)
        result += str(number).encode() + b' 0 obj\n' + body + b'\nendobj\n'
    start = len(result)
    size = max(offsets) + 1
    result += f'xref\n0 {size}\n0000000000 65535 f \n'.encode()
    for number in range(1, size):
        result += (f'{offsets[number]:010d} 00000 n \n' if number in offsets else '0000000000 00000 f \n').encode()
    return result + f'trailer\n<< /Size {size} /Root 1 0 R '.encode() + trailer + f' >>\nstartxref\n{start}\n%%EOF\n'.encode()


def stream(dictionary, data):
    return b'<< /Length ' + str(len(data)).encode() + b' ' + dictionary + b' >>\nstream\n' + data + b'\nendstream'


def append_revision(data, objects=(), freed=(), unreferenced=b''):
    """A real incremental xref/trailer chain for disposable synthetic PDFs."""
    prev = int(re.findall(rb'startxref\s+(\d+)', data)[-1])
    size = max([3, *(int(n) for n in re.findall(rb'(\d+) 0 obj', data)),
                *(number for number, _, _ in objects), *freed]) + 1
    result = data + b'\n'
    entries = {}
    for number, generation, body in objects:
        entries[number] = (len(result), generation, 'n')
        result += f'{number} {generation} obj\n'.encode() + body + b'\nendobj\n'
    result += unreferenced
    for number in freed:
        entries[number] = (0, 1, 'f')
    start = len(result)
    result += b'xref\n'
    for number, (offset, generation, state) in sorted(entries.items()):
        result += f'{number} 1\n{offset:010d} {generation:05d} {state} \n'.encode()
    return result + f'trailer\n<< /Size {size} /Root 1 0 R /Prev {prev} >>\nstartxref\n{start}\n%%EOF\n'.encode()


class StaticInspectionTests(unittest.TestCase):
    def test_page_view_is_explained_without_a_safety_verdict(self):
        result = inspect(synthetic_pdf(b'/OpenAction [3 0 R /Fit]'))
        self.assertEqual(result['category'], 'opening_view')
        self.assertEqual(result['limitations'], [])
        self.assertIn('rather than a script', result['findings'][0]['detail'])

    def test_goto_open_action_is_a_view_but_chained_script_is_not(self):
        plain = inspect(synthetic_pdf(b'/OpenAction << /S /GoTo /D [3 0 R /Fit] >>'))
        self.assertEqual(plain['category'], 'opening_view')
        chained = inspect(synthetic_pdf(b'/OpenAction << /S /GoTo /D [3 0 R /Fit] /Next << /S /JavaScript /JS (inert) >> >>'))
        self.assertEqual(chained['category'], 'active_features')
        self.assertIn('automatic_javascript', {f['feature'] for f in chained['findings']})

    def test_indirect_script_action_and_escaped_names_are_followed(self):
        result = inspect(synthetic_pdf(b'/OpenAction 4 0 R', [(4, b'<< /S /Java#53cript /J#53 (this is inert text) >>')]))
        self.assertEqual(result['category'], 'active_features')
        self.assertIn('automatic_javascript', {f['feature'] for f in result['findings']})
        self.assertTrue(all('inert text' not in f['detail'] for f in result['findings']))

    def test_keywords_in_literal_or_hex_strings_are_not_dictionary_keys(self):
        result = inspect(synthetic_pdf(b'/Title (Words /JS /AA /OpenAction and an escaped \\( bracket) /Other <2f4a53>'))
        self.assertEqual(result['category'], 'keyword_unconfirmed')
        self.assertEqual(result['findings'], [])

    def test_keywords_inside_unrelated_stream_are_not_confirmed(self):
        result = inspect(synthetic_pdf(objects=[(4, stream(b'', b'coincidental /JS /AA /OpenAction bytes'))]))
        self.assertEqual(result['category'], 'keyword_unconfirmed')

    def test_embedded_file_and_launch_action_are_reported_without_extraction(self):
        data = synthetic_pdf(b'/Names << /EmbeddedFiles << /Names [(item) 4 0 R] >> >>',
                             [(4, b'<< /Type /Filespec /EF << /F 5 0 R >> >>'), (5, stream(b'/Type /EmbeddedFile', b'INERT synthetic attachment'))])
        result = inspect(data)
        self.assertEqual(result['category'], 'embedded_content')
        launched = inspect(synthetic_pdf(b'/OpenAction << /S /Launch /F (inert-target) >>'))
        self.assertEqual(launched['category'], 'active_features')
        self.assertIn('launch', {f['feature'] for f in launched['findings']})

    def test_flated_object_stream_is_inspected(self):
        fragment = b'4 0 << /S /JavaScript /JS (not executed) >>'
        data = synthetic_pdf(b'/OpenAction 4 0 R', [(6, stream(b'/Type /ObjStm /N 1 /First 4 /Filter /FlateDecode', zlib.compress(fragment)))])
        result = inspect(data)
        self.assertEqual(result['category'], 'active_features', result)
        self.assertEqual(result['compressed_objects_inspected'], 1)
        self.assertIn('automatic_javascript', {f['feature'] for f in result['findings']})

    def test_uncompressed_object_stream_is_also_inspected(self):
        fragment = b'4 0 << /S /JavaScript /JS (not executed) >>'
        data = synthetic_pdf(b'/OpenAction 4 0 R', [(6, stream(b'/Type /ObjStm /N 1 /First 4', fragment))])
        self.assertEqual(inspect(data)['compressed_objects_inspected'], 1)

    def test_unsupported_or_bad_object_stream_is_incomplete(self):
        for dictionary, data in [(b'/Type /ObjStm /N 1 /First 4 /Filter /LZWDecode', b'inert'),
                                 (b'/Type /ObjStm /N 1 /First 4 /Filter /FlateDecode', b'not compressed')]:
            with self.subTest(dictionary=dictionary):
                result = inspect(synthetic_pdf(objects=[(6, stream(dictionary, data))]))
                self.assertEqual(result['category'], 'incomplete')
                self.assertTrue(result['limitations'])

    def test_encryption_unresolved_reference_and_broken_file_stay_incomplete(self):
        for data in (synthetic_pdf(trailer=b'/Encrypt 8 0 R'), synthetic_pdf(b'/OpenAction 99 0 R'), b'%PDF-1.7\n1 0 obj << /Type /Catalog'):
            self.assertEqual(inspect(data)['category'], 'incomplete')

    def test_duplicate_keys_and_depth_exhaustion_are_not_accepted(self):
        for data in ('<< /JS (one) /JS (two) >>', '[' * 50 + '0' + ']' * 50):
            with self.assertRaises(InspectionError):
                Values(data).read()

    def test_action_in_nested_array_and_event_dictionary_is_detected(self):
        result = inspect(synthetic_pdf(b'/AA << /O 4 0 R >> /Other [<< /S /JavaScript /JS (inert) >>]', [(4, b'<< /S /JavaScript /JS (also inert) >>')]))
        self.assertEqual(result['category'], 'active_features')
        self.assertIn('automatic_javascript', {f['feature'] for f in result['findings']})

    def test_trailer_whitespace_is_preserved_for_numeric_values_and_references(self):
        data = synthetic_pdf(trailer=b'/ID [<1234> <5678>] /Prev 0')
        result = inspect(data)
        self.assertEqual(result['limitations'], [])
        # The old reconstruction produced /Size4/Root10R and could not read it.
        encrypted = inspect(synthetic_pdf(trailer=b'/Encrypt 8 0 R'))
        self.assertTrue(encrypted['encrypted'])
        commented = inspect(synthetic_pdf(trailer=b'/Info 4% comment is whitespace\n0 R'))
        self.assertEqual(commented['limitations'], [])

    def test_nul_whitespace_is_supported_in_values_and_object_stream_index(self):
        self.assertEqual(Values('<< /Count 1\x00/Next 3\x000\x00R >>').read()['/Count'], 1)
        fragment = b'4\x000\x00<< /S /JavaScript /JS (this.print\(\);) >>'
        result = inspect(synthetic_pdf(b'/OpenAction 4 0 R', [(6, stream(b'/Type /ObjStm /N 1 /First 4', fragment))]))
        self.assertEqual(result['category'], 'active_features', result)
        self.assertIn('Print command', result['script_reviews'][0]['observations'])

    def test_inline_script_descriptions_do_not_export_or_execute_code(self):
        code = b'this.print\(\); app.launchURL\("https://example.invalid"\);'
        result = inspect(synthetic_pdf(b'/OpenAction << /S /JavaScript /JS (' + code + b') >>'))
        review = result['script_reviews'][0]
        self.assertIn('Print command', review['observations'])
        self.assertIn('URL, submission or network-related text', review['observations'])
        self.assertNotIn('example.invalid', json.dumps(result))

    def test_navigation_and_host_messages_are_described_without_hiding_other_patterns(self):
        code = b'this.pageNum=2; this.zoomType=zoomType.none; this.hostContainer.postMessage\(["newPage", "synthetic", 0]\); app.launchURL\("https://example.invalid"\);'
        result = inspect(synthetic_pdf(b'/OpenAction << /S /JavaScript /JS (' + code + b') >>'))
        observations = result['script_reviews'][0]['observations']
        self.assertIn('Page navigation or view setting', observations)
        self.assertIn('Message to the hosting application', observations)
        self.assertIn('URL, submission or network-related text', observations)
        self.assertEqual(result['category'], 'active_features')

    def test_utf16_hex_script_and_flated_script_stream_are_inspected_statically(self):
        code = b'app.execMenuItem("Print");'
        hex_code = (b'\xfe\xff' + code.decode().encode('utf-16-be')).hex().encode()
        literal = inspect(synthetic_pdf(b'/OpenAction << /S /JavaScript /JS <' + hex_code + b'> >>'))
        streamed = inspect(synthetic_pdf(b'/OpenAction 4 0 R', [
            (4, b'<< /S /JavaScript /JS 5 0 R >>'),
            (5, stream(b'/Filter /FlateDecode', zlib.compress(code)))]))
        for result in (literal, streamed):
            self.assertIn('Viewer menu or toolbar command', result['script_reviews'][0]['observations'])
            self.assertEqual(result['inspection_status'], 'complete_within_limits')

    def test_unsupported_script_stream_keeps_positive_finding_and_incomplete_status(self):
        result = inspect(synthetic_pdf(b'/OpenAction 4 0 R', [
            (4, b'<< /S /JavaScript /JS 5 0 R >>'),
            (5, stream(b'/Filter /LZWDecode', b'inert'))]))
        self.assertEqual(result['category'], 'incomplete')
        self.assertTrue(any(f['feature'] == 'javascript' for f in result['findings']))
        self.assertTrue(result['limitations'])

    def test_launch_targets_are_text_only_and_escaped_strings_decode(self):
        result = inspect(synthetic_pdf(b'/OpenAction << /S /Launch /Win << /F (book\\050part\\051.pdf) /P (inert) >> >>'))
        self.assertEqual(result['launch_reviews'][0]['target'], 'book(part).pdf')
        self.assertTrue(result['launch_reviews'][0]['parameters_present'])

    def test_repeated_definitions_remain_unresolved_instead_of_becoming_a_pass(self):
        with patch('pdf_inspection.load_revision_engine', return_value=None):
            result = inspect(synthetic_pdf(objects=[(4, b'<< /S /JavaScript /JS (this.print\(\);) >>'),
                                                    (4, b'<< /S /JavaScript /JS (this.print\(\);) >>')]))
        self.assertEqual(result['inspection_status'], 'incomplete')
        self.assertTrue(result['script_reviews'])
        self.assertIn('optional PyMuPDF', ' '.join(result['limitations']))


@unittest.skipUnless(importlib.util.find_spec('pymupdf'), 'Optional native revision resolver not installed')
class RevisionInspectionTests(unittest.TestCase):
    def test_new_xref_definition_replaces_old_script_without_hiding_history(self):
        base = synthetic_pdf(b'/OpenAction 4 0 R', [(4, b'<< /S /JavaScript /JS (this.print\(\);) >>')])
        result = inspect(append_revision(base, [(4, 0, b'<< /S /GoTo /D [3 0 R /Fit] >>')]))
        self.assertEqual(result['revision_resolution']['status'], 'resolved_current_xref', result)
        self.assertEqual(result['revision_resolution']['versions'], 2)
        self.assertEqual(result['inspection_status'], 'complete_within_limits', result)
        self.assertNotIn('javascript', {r['feature'] for r in result['findings']})
        self.assertIn('javascript', {r['feature'] for r in result['physical_only_findings']})

    def test_new_script_in_current_revision_is_still_flagged(self):
        base = synthetic_pdf(b'/OpenAction 4 0 R', [(4, b'<< /S /GoTo /D [3 0 R /Fit] >>')])
        result = inspect(append_revision(base, [(4, 0, b'<< /S /JavaScript /JS (this.print\(\);) >>')]))
        self.assertEqual(result['category'], 'active_features', result)
        self.assertIn('automatic_javascript', {r['feature'] for r in result['findings']})

    def test_freed_object_is_not_revived_from_earlier_physical_bytes(self):
        base = synthetic_pdf(objects=[(4, b'<< /S /JavaScript /JS (this.print\(\);) >>')])
        result = inspect(append_revision(base, freed=[4]))
        self.assertEqual(result['revision_resolution']['status'], 'resolved_current_xref', result)
        self.assertFalse(result['findings'])
        self.assertEqual(result['category'], 'historical_features')
        self.assertIn('javascript', {r['feature'] for r in result['physical_only_findings']})

    def test_unreferenced_later_stream_does_not_replace_xref_selected_script(self):
        base = synthetic_pdf(b'/OpenAction 4 0 R', [(4, b'<< /S /JavaScript /JS 5 0 R >>'),
                             (5, stream(b'', b'this.pageNum=0;'))])
        decoy = b'5 0 obj\n' + stream(b'', b'app.launchURL("https://example.invalid");') + b'\nendobj\n'
        data = append_revision(base, [(5, 0, stream(b'', b'this.print();'))], unreferenced=decoy)
        result = inspect(data)
        self.assertEqual(result['revision_resolution']['status'], 'resolved_current_xref', result)
        observations = result['script_reviews'][0]['observations']
        self.assertIn('Print command', observations)
        self.assertNotIn('URL, submission or network-related text', observations)

    def test_repaired_and_generation_sensitive_files_remain_unresolved(self):
        base = synthetic_pdf(objects=[(4, b'<< /S /JavaScript /JS (this.print\(\);) >>')])
        changed = append_revision(base, [(4, 1, b'<< /S /GoTo /D [3 0 R /Fit] >>')])
        result = inspect(changed)
        self.assertEqual(result['inspection_status'], 'incomplete')
        self.assertIn('Nonzero', ' '.join(result['limitations']))
        broken = re.sub(rb'startxref\s+\d+\s+%%EOF\s*$', b'startxref\n1\n%%EOF\n', append_revision(base, [(4, 0, b'null')]))
        result = inspect(broken)
        self.assertEqual(result['inspection_status'], 'incomplete')
        self.assertEqual(result['revision_resolution']['status'], 'unresolved')

    def test_compressed_xref_and_incremental_update_are_supported(self):
        import pymupdf
        base = synthetic_pdf(b'/OpenAction 4 0 R', [(4, b'<< /S /GoTo /D [3 0 R /Fit] >>')])
        with tempfile.TemporaryDirectory(prefix='pinakeys synthetic revision ') as temp:
            target = Path(temp) / 'synthetic-only.pdf'
            with pymupdf.open(stream=base, filetype='pdf') as doc:
                target.write_bytes(doc.tobytes(use_objstms=1))
            with pymupdf.open(target) as doc:
                doc.update_object(4, '<< /S /JavaScript /JS (this.print\\(\\);) >>')
                doc.saveIncr()
            result = inspect(target.read_bytes())
        self.assertEqual(result['revision_resolution']['status'], 'resolved_current_xref', result)
        self.assertIn('automatic_javascript', {r['feature'] for r in result['findings']})

    def test_absent_xref_numbers_are_not_loaded_but_dangling_references_are_flagged(self):
        base = synthetic_pdf(objects=[(10, b'<< /Type /Example >>')])
        start = int(re.findall(rb'startxref\s+(\d+)', base)[-1])
        # Sparse sections leave unallocated numbers instead of a dense run.
        offsets = {int(m[1]): m.start() for m in re.finditer(rb'(\d+) 0 obj\n', base)}
        sparse = base[:start] + b'xref\n0 4\n0000000000 65535 f \n'
        sparse += b''.join(f'{offsets[n]:010d} 00000 n \n'.encode() for n in (1, 2, 3))
        sparse += f'10 1\n{offsets[10]:010d} 00000 n \ntrailer\n<< /Size 11 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode()
        result = inspect(append_revision(sparse, [(10, 0, b'<< /Type /Example >>')]))
        self.assertEqual(result['revision_resolution']['status'], 'resolved_current_xref', result)
        self.assertEqual(result['inspection_status'], 'complete_within_limits', result)
        self.assertGreater(result['revision_resolution']['absent_object_numbers'], 0)
        dangling = append_revision(sparse, [(10, 0, b'<< /Other 8 0 R >>')])
        result = inspect(dangling)
        self.assertEqual(result['inspection_status'], 'incomplete')
        self.assertIn('missing object numbers: 8', ' '.join(result['limitations']))

    def test_current_reference_to_a_null_value_is_not_a_missing_object(self):
        base = synthetic_pdf(b'/Optional 9 0 R', [(4, b'<< /Type /Example >>'), (9, b'null')])
        result = inspect(append_revision(base, [(4, 0, b'<< /Type /Example /Updated true >>')]))
        self.assertEqual(result['revision_resolution']['status'], 'resolved_current_xref', result)
        self.assertEqual(result['inspection_status'], 'complete_within_limits', result)


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pinakeys pdf synthetic ')
        self.addCleanup(self.temp.cleanup)
        self.root = build_folder(Path(self.temp.name).resolve() / 'USB')
        self.internal = self.root / 'Library Tools/Internal'
        self.layout = library_paths(self.internal / 'pdf_followup.py')
        self.pdf = self.root / 'Incoming Files/example.pdf'
        self.pdf.write_bytes(synthetic_pdf(b'/OpenAction [3 0 R /Fit]'))
        self.row = {'relative_path': 'Incoming Files/example.pdf', 'size_bytes': self.pdf.stat().st_size,
                    'sha256': hashlib.sha256(self.pdf.read_bytes()).hexdigest(), 'pdfid': 'Indicators found: /OpenAction: 1',
                    'clamav': 'No detection (not proof of safety)', 'file_type_check': 'PDF header present'}
        self.report = self.layout.reports / 'intake_audit.json'
        self.save_audit([self.row])

    def save_audit(self, rows):
        self.report.write_text(json.dumps({'scanned_at': 'synthetic snapshot', 'scanned_folder': 'Incoming Files', 'files': rows}), encoding='utf-8')

    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-B', str(self.internal / 'pdf_followup.py'), *args], cwd=self.temp.name,
                              capture_output=True, text=True, timeout=15)

    def test_selects_only_feature_warning_pdfs_without_starting_any_worker(self):
        other = dict(self.row, relative_path='Incoming Files/ordinary.pdf', pdfid='No selected indicators detected')
        self.save_audit([self.row, other])
        before = self.report.read_bytes()
        with patch('pdf_followup.subprocess.run') as run:
            selection = read_selection(self.layout)
        self.assertEqual(len(selection['files']), 1)
        run.assert_not_called()
        self.assertEqual(self.report.read_bytes(), before)

    def test_subset_is_explicit_and_rejects_unknown_or_repeated_paths(self):
        selection = read_selection(self.layout)
        chosen = select_paths(selection, [self.row['relative_path']])
        self.assertEqual(len(chosen['files']), 1)
        self.assertIn('subset only', chosen['selection_scope'])
        for paths in (['Incoming Files/not-a-warning.pdf'], [self.row['relative_path']] * 2):
            with self.assertRaises(FollowupError):
                select_paths(selection, paths)

    def test_partial_findings_are_counted_in_html_json_summary_and_csv(self):
        row = dict(self.row, category='incomplete', audit_pdfid=self.row['pdfid'], priority='closer review',
                   limitations=['Synthetic parser limit'], findings=[
                       {'feature': 'javascript', 'object': '4 0', 'detail': 'Script structure'},
                       {'feature': 'javascript', 'object': '5 0', 'detail': 'Another script'},
                       {'feature': 'embedded', 'object': '6 0', 'detail': 'Attachment structure'}])
        summary = summary_counts([row])
        self.assertEqual(summary['features_by_file']['javascript'], 1)
        self.assertEqual(summary['features_by_file']['embedded'], 1)
        self.assertEqual(summary['coverage']['incomplete'], 1)
        page = html_report({'files': [row], 'created_at_utc': 'now', 'audit_date': 'before', 'scanned_folder': 'Incoming Files'})
        self.assertIn('JavaScript-related structures</td><td>1', page)
        self.assertIn('href="#file-0"', page)
        self.assertIn('id="file-0"', page)
        class Links(HTMLParser):
            def __init__(self):
                super().__init__()
                self.targets, self.links = set(), set()
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if 'id' in attrs:
                    self.targets.add(attrs['id'])
                if attrs.get('href', '').startswith('#'):
                    self.links.add(attrs['href'][1:])
        links = Links()
        links.feed(page)
        self.assertTrue(links.links <= links.targets)
        csv = csv_report([row])
        self.assertIn('inspection_status', csv)
        self.assertIn('embedded; javascript', csv)

    def test_historical_findings_and_verification_are_separate_and_escaped(self):
        row = dict(self.row, category='historical_features', audit_pdfid=self.row['pdfid'], priority='historical review',
                   limitations=[], findings=[], physical_only_findings=[
                       {'feature': 'javascript', 'object': '4 0', 'detail': '<script>not current</script>'}],
                   revision_resolution={'status': 'resolved_current_xref', 'engine': 'synthetic engine', 'versions': 2},
                   verification={'status': 'matched_saved_audit'})
        summary = summary_counts([row])
        self.assertEqual(summary['features_by_file']['javascript'], 0)
        self.assertEqual(summary['physical_only_features_by_file']['javascript'], 1)
        page = html_report({'files': [row], 'created_at_utc': 'now', 'audit_date': 'before', 'scanned_folder': 'Incoming Files'})
        self.assertIn('Physical-only findings outside current definitions', page)
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('matched_saved_audit', page)
        self.assertIn('resolved_current_xref', csv_report([row]))

    def test_subset_cli_inspects_only_requested_file_and_names_coverage(self):
        second = self.root / 'Incoming Files/do-not-inspect.pdf'
        second.write_bytes(b'%PDF-invalid deliberately outside subset')
        self.save_audit([self.row, dict(self.row, relative_path='Incoming Files/do-not-inspect.pdf')])
        result = self.run_cli('--only', self.row['relative_path'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(next((self.layout.reports / 'PDF Checks').glob('*/findings.json')).read_text(encoding='utf-8'))
        self.assertEqual([row['relative_path'] for row in data['files']], [self.row['relative_path']])
        self.assertEqual(data['available_warning_pdfs'], 2)
        self.assertIn('Coverage:', result.stdout)
        self.assertIn('Features observed, including incomplete', result.stdout)

    def test_real_worker_produces_report_and_preserves_originals_and_old_reports(self):
        original = self.pdf.read_bytes()
        old_report = self.report.read_bytes()
        (self.layout.reports / 'intake_audit.csv').write_bytes(b'prior audit csv')
        self.layout.corrections.write_bytes(b'prior corrections')
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = next((self.layout.reports / 'PDF Checks').glob('*/findings.json'))
        data = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(data['files'][0]['category'], 'opening_view', data)
        self.assertTrue((output.parent / 'Report.html').is_file())
        self.assertTrue((output.parent / 'findings.csv').is_file())
        self.assertEqual(self.pdf.read_bytes(), original)
        self.assertEqual(self.report.read_bytes(), old_report)
        self.assertEqual(self.layout.corrections.read_bytes(), b'prior corrections')
        self.assertEqual((self.layout.reports / 'intake_audit.csv').read_bytes(), b'prior audit csv')
        self.assertIn('Opening view setting found: 1', result.stdout)

    def test_changed_bytes_missing_and_symlink_sources_are_skipped(self):
        self.pdf.write_bytes(b'x' * self.row['size_bytes'])
        result = inspect_one(self.layout, self.internal, self.row, 5)
        self.assertEqual(result['category'], 'skipped')
        self.assertIn('fingerprint', ' '.join(result['limitations']))
        self.assertEqual(result['verification']['status'], 'fingerprint_mismatch')
        self.assertEqual(result['verification']['expected_sha256'], self.row['sha256'])
        self.assertTrue(result['verification']['source_stable_during_copy'])
        self.pdf.unlink()
        self.assertEqual(inspect_one(self.layout, self.internal, self.row, 5)['category'], 'skipped')
        outside = Path(self.temp.name) / 'outside.pdf'
        outside.write_bytes(synthetic_pdf())
        try:
            self.pdf.symlink_to(outside)
        except (OSError, NotImplementedError):
            return
        self.assertEqual(inspect_one(self.layout, self.internal, self.row, 5)['category'], 'skipped')

    def test_prior_av_error_or_detection_is_not_parsed(self):
        for state in ('ERROR', 'DETECTION', 'NOT RUN'):
            with patch('pdf_followup.subprocess.run') as run:
                result = inspect_one(self.layout, self.internal, dict(self.row, clamav=state), 5)
            self.assertEqual(result['category'], 'skipped')
            run.assert_not_called()

    def test_fingerprint_mismatch_never_launches_worker_or_updates_saved_audit(self):
        before = self.report.read_bytes()
        self.pdf.write_bytes(b'x' * self.row['size_bytes'])
        with patch('pdf_followup.subprocess.run') as run:
            result = inspect_one(self.layout, self.internal, self.row, 5)
        run.assert_not_called()
        self.assertEqual(result['verification']['status'], 'fingerprint_mismatch')
        self.assertNotEqual(result['verification']['observed_sha256'], self.row['sha256'])
        self.assertEqual(self.report.read_bytes(), before)

    def test_metadata_change_during_copy_is_distinguished_from_hash_mismatch(self):
        import pdf_followup
        before = pdf_followup.source_info(self.pdf)
        changed = SimpleNamespace(**{field: getattr(before, field) for field in
                                  ('st_dev', 'st_ino', 'st_size', 'st_mode', 'st_mtime_ns', 'st_ctime_ns')})
        changed.st_ctime_ns += 1
        with patch('pdf_followup.source_info', side_effect=[before, changed]), patch('pdf_followup.subprocess.run') as run:
            result = inspect_one(self.layout, self.internal, self.row, 5)
        run.assert_not_called()
        self.assertEqual(result['verification']['status'], 'source_changed_during_copy')
        self.assertEqual(result['verification']['expected_sha256'], result['verification']['observed_sha256'])

    @unittest.skipUnless(importlib.util.find_spec('pymupdf'), 'Optional native revision resolver not installed')
    def test_cyclic_revision_pointer_is_incomplete_with_a_bounded_worker(self):
        data = append_revision(synthetic_pdf(), [(3, 0, b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>')])
        current = re.findall(rb'startxref\s+(\d+)', data)[-1]
        data = re.sub(rb'/Prev \d+', b'/Prev ' + current, data)
        self.pdf.write_bytes(data)
        row = dict(self.row, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
        result = inspect_one(self.layout, self.internal, row, 2)
        self.assertEqual(result['inspection_status'], 'incomplete', result)

    def test_timeout_and_bad_worker_output_remain_incomplete(self):
        for result in (subprocess.TimeoutExpired(['synthetic'], 1), subprocess.CompletedProcess([], 1, b'bad json', b'')):
            with self.subTest(result=result), patch('pdf_followup.subprocess.run', side_effect=result if isinstance(result, Exception) else None, return_value=result):
                output = inspect_one(self.layout, self.internal, self.row, 1)
            self.assertEqual(output['category'], 'incomplete')
            self.assertTrue(output['limitations'])

    def test_file_changed_while_worker_runs_is_not_a_completed_inspection(self):
        valid = json.dumps({'category': 'opening_view', 'findings': [], 'limitations': []}).encode()
        def edited(*args, **kwargs):
            self.pdf.write_bytes(b'External synthetic edit')
            return subprocess.CompletedProcess([], 0, valid, b'')
        with patch('pdf_followup.subprocess.run', side_effect=edited):
            result = inspect_one(self.layout, self.internal, self.row, 2)
        self.assertEqual(result['category'], 'incomplete')
        self.assertIn('original file changed', ' '.join(result['limitations']))

    def test_changed_bundled_parser_is_not_imported(self):
        (self.internal / 'vendor/pdf_parser.py').write_text('raise Exception("must not be imported")\n', encoding='utf-8')
        result = inspect_one(self.layout, self.internal, self.row, 5)
        self.assertEqual(result['category'], 'incomplete')
        self.assertIn('checksum differs', ' '.join(result['limitations']))

    def test_traversal_duplicate_and_corrupt_audits_fail_without_reports(self):
        for rows in ([dict(self.row, relative_path='../private.pdf')], [self.row, self.row]):
            self.save_audit(rows)
            with self.assertRaises(FollowupError):
                read_selection(self.layout)
        self.report.write_text('{bad json', encoding='utf-8')
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertFalse((self.layout.reports / 'deeper_pdf_check.html').exists())

    def test_report_escapes_filenames_and_does_not_offer_original_open_links(self):
        row = dict(self.row, relative_path='Incoming Files/<script>alert(1)</script>.pdf', category='keyword_unconfirmed',
                   audit_pdfid=self.row['pdfid'], priority='context review', findings=[], limitations=[])
        page = html_report({'files': [row], 'created_at_utc': 'now', 'audit_date': 'before', 'scanned_folder': 'Incoming Files'})
        self.assertNotIn('<script>', page)
        self.assertNotIn('href="file:', page)
        self.assertIn('Content-Security-Policy', page)
        self.assertIn('&lt;script&gt;', page)
        row['relative_path'] = '=formula.pdf'
        self.assertIn("'=formula.pdf", csv_report([row]))

    def test_missing_audit_or_worker_cannot_replace_last_completed_report(self):
        latest = self.layout.reports / 'deeper_pdf_check.html'
        latest.write_bytes(b'previous completed report')
        self.report.unlink()
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertEqual(latest.read_bytes(), b'previous completed report')

    def test_deeper_action_is_fixed_and_does_not_run_on_app_creation(self):
        runner = ActionRunner(self.internal / 'pinakeys.py', python=sys.executable)
        try:
            self.assertFalse(runner.busy)
            self.assertFalse((self.layout.reports / 'deeper_pdf_check.html').exists())
            runner.start('pdf')
            code = runner.process.wait(timeout=15)
            output = runner.read_output()
            self.assertEqual(code, 0, output)
            self.assertIn('Deeper PDF summary', output)
        finally:
            if runner.busy:
                runner.cancel()
                runner.process.wait(timeout=3)
            runner.close()

    @unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file(), 'macOS process restrictions')
    def test_macos_profile_blocks_network_and_file_writes(self):
        from pdf_followup import worker_command
        command, _ = worker_command(self.internal, self.pdf)
        destination = str(Path(self.temp.name) / 'must-not-write')
        code = ('import socket\nfrom pathlib import Path\n'
                'blocked = 0\n'
                'try: Path(' + repr(destination) + ').write_text("forbidden")\nexcept OSError: blocked += 1\n'
                'try: socket.create_connection(("127.0.0.1", 9), timeout=1)\nexcept PermissionError: blocked += 1\n'
                'assert blocked == 2, blocked\n')
        result = subprocess.run([*command[:3], sys.executable, '-I', '-B', '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(Path(destination).exists())


class ButtonTests(unittest.TestCase):
    save_audit = FollowupTests.save_audit

    def setUp(self):
        FollowupTests.setUp(self)
        import tkinter as tk
        try:
            self.tkroot = tk.Tk()
            self.tkroot.withdraw()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.runner = ActionRunner(self.internal / 'pinakeys.py', python=sys.executable)
        self.support = patch('pinakeys.capabilities', return_value={})
        self.support.start()
        self.app = PinakeysWindow(self.tkroot, self.runner)
        self.addCleanup(self.finish_gui)

    def finish_gui(self):
        if self.runner.busy:
            self.runner.cancel()
            self.runner.process.wait(timeout=5)
        self.runner.close()
        for identifier in self.tkroot.tk.call('after', 'info'):
            self.tkroot.tk.call('after', 'cancel', identifier)
        self.tkroot.destroy()
        self.support.stop()

    def button(self, text):
        return next(b for b in self.app.buttons if b.cget('text') == text)

    def wait(self):
        end = time.monotonic() + 15
        def poll():
            if self.app.action is None or time.monotonic() >= end:
                self.tkroot.quit()
            else:
                self.tkroot.after(20, poll)
        self.tkroot.after(20, poll)
        self.tkroot.mainloop()
        self.assertIsNone(self.app.action, self.app.status.get())

    def test_button_confirmation_cancel_then_run_and_open_findings(self):
        original = self.pdf.read_bytes()
        self.assertFalse(self.runner.busy)
        with patch('tkinter.messagebox.askyesno', return_value=False):
            self.button('Deeper PDF Check').invoke()
        self.assertIsNone(self.app.action)
        self.assertFalse((self.layout.reports / 'deeper_pdf_check.html').exists())
        with patch('tkinter.messagebox.askyesno', return_value=True):
            self.button('Deeper PDF Check').invoke()
        self.assertEqual(self.app.action, 'pdf')
        self.wait()
        self.assertIn('Open PDF Findings', self.app.status.get())
        self.assertIn('Opening view setting found: 1', self.app.details.get('1.0', 'end'))
        with patch('pinakeys.open_local') as open_file:
            self.button('Open PDF Findings').invoke()
        self.assertEqual(open_file.call_args.args, (self.layout.reports / 'deeper_pdf_check.html',))
        self.assertEqual(self.pdf.read_bytes(), original)

    def test_missing_audit_explains_without_starting(self):
        self.report.unlink()
        with patch('tkinter.messagebox.showinfo') as message:
            self.button('Deeper PDF Check').invoke()
        self.assertFalse(self.runner.busy)
        self.assertIn('Check Incoming first', message.call_args.args[1])

    def test_stop_leaves_last_completed_report_and_original_unchanged(self):
        latest = self.layout.reports / 'deeper_pdf_check.html'
        latest.write_bytes(b'previous completed findings')
        original = self.pdf.read_bytes()
        (self.internal / 'pdf_inspection.py').write_text('import time\ntime.sleep(30)\n', encoding='utf-8')
        with patch('tkinter.messagebox.askyesno', return_value=True):
            self.button('Deeper PDF Check').invoke()
        self.assertEqual(self.app.action, 'pdf')
        self.app.stop.invoke()
        self.wait()
        self.assertIn('Stopped', self.app.status.get())
        self.assertIn('PDF check incomplete', self.app.progress_text.get())
        self.assertEqual(latest.read_bytes(), b'previous completed findings')
        self.assertEqual(self.pdf.read_bytes(), original)

    def test_progress_uses_pdf_stages(self):
        self.app.action = 'pdf'
        self.app.audit_started = time.monotonic()
        self.app.audit_event = {'stage': 2, 'state': 'running', 'done': 1, 'total': 3, 'file': 'Incoming Files/synthetic.pdf'}
        self.app.render_audit_progress()
        self.assertIn('inspecting PDF structure', self.app.status.get())
        self.assertIn('1 of 3', self.app.progress_text.get())
        self.assertNotIn('Antivirus', self.app.status.get())
        self.app.action = None

    def test_new_controls_fit_the_small_window(self):
        self.tkroot.deiconify()
        self.tkroot.geometry('650x680')
        settle_window(self.tkroot, lambda: self.button('Deeper PDF Check').winfo_width() > 80)
        for label in ('Deeper PDF Check', 'Open PDF Findings'):
            button = self.button(label)
            self.assertGreater(button.winfo_width(), 80)
            bottom = button.winfo_rooty() - self.tkroot.winfo_rooty() + button.winfo_height()
            self.assertLess(bottom, self.tkroot.winfo_height())
        self.assertLess(self.app.details.winfo_rooty() - self.tkroot.winfo_rooty(), self.tkroot.winfo_height())


if __name__ == '__main__':
    unittest.main()
