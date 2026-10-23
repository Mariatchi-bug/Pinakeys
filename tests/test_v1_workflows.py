"""V1 journeys and performance contracts, using disposable materials only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote
import zipfile

from build_usb import build_folder
from catalogue_settings import Selection
from library_paths import LibraryPaths
from library_records import Records, proof
from intake_workflow import Intake
from review_library import ReviewStore
from record_backup import restore_records

class V1Journeys(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='pinakeys-v1-')
        self.addCleanup(self.tmp.cleanup)
        self.root=build_folder(Path(self.tmp.name).resolve()/'Mount A')
        self.layout=LibraryPaths(self.root,True)
        (self.root/'Research/Notes').mkdir(parents=True)
        self.book=self.root/'Research/Notes/Book — Writer.epub'
        with zipfile.ZipFile(self.book,'w') as z:
            z.writestr('META-INF/container.xml','<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
            z.writestr('book.opf','<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Embedded title</dc:title><dc:creator>Embedded author</dc:creator></metadata></package>')
        self.selection=Selection(self.layout)
        self.selection.save(['Research'],[])

    def scan(self,*extra):
        result=subprocess.run([sys.executable,'-B',str(self.root/'Library Tools/Internal/scan_library.py'),'--inventory-only',*extra],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        return json.loads((self.layout.reports/'catalogue.json').read_text(encoding='utf-8'))

    def test_core_journey_without_reorganizing_and_backup_restore(self):
        original=self.book.read_bytes()
        rows=self.scan();self.assertEqual(len(rows),1)
        self.assertEqual(self.book.read_bytes(),original)
        view=(self.root/'Open Library.html').read_text(encoding='utf-8')
        self.assertIn('id="search"',view)
        self.assertIn('Research/Notes/Book%20%E2%80%94%20Writer.epub',view)
        store=ReviewStore(self.layout)
        self.assertEqual(store.prefill(rows[0]['relative_path'])[0]['title'],'Book')
        store.save(rows[0]['relative_path'],{'title':'My correction','author':'My author'})
        rows=self.scan();identity=rows[0]['record_id']
        self.assertEqual(rows[0]['title'],'My correction')
        saved=(self.layout.records).read_bytes()
        snapshot=Path(self.tmp.name)/'saved records.json';snapshot.write_bytes(saved)
        # Same-sized new contents with unchanged mtime are not a cached proof.
        moved=self.book.with_name('Renamed book.epub');self.book.rename(moved)
        self.scan();self.assertTrue(any(r['state']=='missing' for r in Records(self.layout).data['records']))
        self.scan('--verify')
        self.assertEqual(Records(self.layout).by_path('Research/Notes/Renamed book.epub')['id'],identity)
        new_mount=self.root.with_name('Mount B with spaces');self.root.rename(new_mount)
        self.root=new_mount;self.layout=LibraryPaths(self.root,True)
        self.assertEqual(self.scan()[0]['title'],'My correction')
        # Restore the earlier ledger at its old path, then reconnect by fresh bytes.
        self.assertEqual(restore_records(self.layout,snapshot),1)
        self.scan('--verify')
        r=Records(self.layout).by_path('Research/Notes/Renamed book.epub')
        self.assertEqual(r['id'],identity);self.assertEqual(r['corrections']['title'],'My correction')
        self.assertTrue(list((self.layout.tools/'Corrections/Record History').glob('*.json')))

    def test_quick_update_and_single_correction_do_not_hash_archive(self):
        archive=self.root/'Research/huge.zip'
        with archive.open('wb') as f:f.truncate(64*1024**2 if os.name == 'nt' else 8*1024**3)
        rows=self.scan();self.assertEqual(len(rows),2)
        import library_records
        original=library_records.proof;seen=[]
        def checked(path):
            self.assertNotEqual(path.name,'huge.zip');seen.append(path.name);return original(path)
        with patch('library_records.proof',side_effect=checked):
            ReviewStore(self.layout).save('Research/Notes/Book — Writer.epub',{'title':'Fast correction','author':'Writer'})
        self.assertTrue(seen)
        self.assertEqual(self.scan()[0 if self.scan()[0]['format']=='EPUB' else 1]['title'],'Fast correction')
        self.assertTrue(all('not verified' in r['verification'].lower() for r in self.scan()))

    def test_selection_exclusions_and_scope_preserve_unselected_records(self):
        excluded=self.root/'Research/private.zip';excluded.write_bytes(b'archive')
        image=self.root/'Research/Notes/photo.png';image.write_bytes(b'image')
        self.selection.save(['Research'],['*.zip'])
        rows=self.scan('--verify')
        self.assertEqual({r['relative_path'] for r in rows},{'Research/Notes/Book — Writer.epub','Research/Notes/photo.png'})
        old=Records(self.layout).data['records']
        Selection(self.layout).save(['Content'],[]);self.scan('--verify')
        after=Records(self.layout).data['records']
        self.assertEqual({r['id'] for r in old},{r['id'] for r in after})
        self.assertTrue(all(r['state']=='present' for r in after))

    def test_identical_copies_and_unchanged_attributes_require_verification(self):
        a=self.root/'Research/one.txt';b=self.root/'Research/two.txt'
        a.write_bytes(b'abc');b.write_bytes(b'abc')
        self.scan('--verify');records=Records(self.layout)
        one=records.by_path('Research/one.txt');two=records.by_path('Research/two.txt')
        self.assertNotEqual(one['id'],two['id'])
        ReviewStore(self.layout).save('Research/one.txt',{'title':'Only one','author':'A'})
        stamp=a.stat();a.write_bytes(b'xyz');os.utime(a,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        self.scan();self.assertTrue(Records(self.layout).by_path('Research/one.txt')['content_unverified'])
        rows=self.scan('--verify');r=Records(self.layout).by_path('Research/one.txt')
        self.assertNotEqual(r['sha256'],one['sha256']);self.assertEqual(r['corrections']['title'],'Only one')
        self.assertEqual(Records(self.layout).corrections(r),{})
        self.assertTrue(r['versions'])

    def test_optional_intake_from_audit_to_preview_file_catalogue_and_undo(self):
        incoming=self.root/'Incoming Files/arrival.txt';incoming.write_text('Disposable arrival', encoding='utf-8')
        self.scan()  # New clean installation has an empty portable ledger.
        command=[sys.executable,'-B',str(self.root/'Library Tools/Internal/audit_materials.py'),'--no-clamav']
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        flow=Intake(self.layout);flow.reload(quick=True)
        r=flow.records.by_path('Incoming Files/arrival.txt')
        self.assertIsNotNone(r)
        self.assertTrue(r['content_unverified'])
        self.assertIn('Incomplete checks',flow.categories(r))
        flow.review(r['id'],'Reviewed arrival','Writer','Reviewed arrival.txt','Content/Filed',True)
        plan=flow.preview([r['id']]);self.assertTrue(incoming.exists())
        journal=flow.apply(plan,'FILE 1')
        target=self.root/'Content/Filed/Reviewed arrival.txt';self.assertTrue(target.exists())
        Selection(self.layout).save(['Research','Content'],[])
        rows=self.scan();self.assertTrue(any(x['title']=='Reviewed arrival' for x in rows))
        flow=Intake(self.layout);flow.rollback(journal,confirmation='UNDO 1')
        self.assertTrue(incoming.exists());self.assertFalse(target.exists())

    def test_full_verification_cancellation_leaves_records_intact(self):
        self.scan('--verify');before=self.layout.records.read_bytes()
        flow=Intake(self.layout)
        with patch('intake_workflow.proof',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):flow.reload()
        self.assertEqual(self.layout.records.read_bytes(),before)
        self.assertFalse((self.layout.tools/'Corrections/records.lock').exists())

    def test_cancel_selected_correction_leaves_records_and_archive_untouched(self):
        self.scan();store=ReviewStore(self.layout);before=self.layout.records.read_bytes()
        with self.assertRaisesRegex(ValueError,'cancelled'):
            store.save('Research/Notes/Book — Writer.epub',{'title':'Not committed','author':'Writer'},cancelled=lambda:True)
        self.assertEqual(self.layout.records.read_bytes(),before)
        self.assertFalse((self.layout.tools/'Corrections/records.lock').exists())

    def test_new_audit_reconnects_changed_arrival_without_a_second_full_pass(self):
        incoming=self.root/'Incoming Files/arrival.txt';incoming.write_text('Version one', encoding='utf-8')
        flow=Intake(self.layout);flow.reload(initialize=True)
        r=flow.records.by_path('Incoming Files/arrival.txt')
        with flow.records.lock():flow.records.set_metadata(r['id'],{'title':'Old correction'});flow.records.save()
        incoming.write_text('Version two', encoding='utf-8')
        command=[sys.executable,'-B',str(self.root/'Library Tools/Internal/audit_materials.py'),'--no-clamav']
        self.assertEqual(subprocess.run(command,capture_output=True).returncode,0)
        with patch('intake_workflow.proof',side_effect=AssertionError('Quick reload must not hash')):
            flow.reload(quick=True)
        current=flow.records.by_path('Incoming Files/arrival.txt')
        self.assertEqual(current['id'],r['id'])
        self.assertTrue(current['content_unverified'])
        self.assertEqual(current['corrections']['title'],'Old correction')
        self.assertEqual(flow.records.corrections(current),{})
        flow.review(current['id'],'New correction','Writer','Reviewed.txt','Content/Filed',True)
        self.assertEqual(flow.records.corrections(current)['title'],'New correction')

    def test_restore_refuses_other_library_and_preserves_unknown_fields(self):
        self.scan('--verify');d=json.loads(self.layout.records.read_text(encoding='utf-8'))
        d['unknown_future_field']={'keep':'all values'}
        snapshot=Path(self.tmp.name)/'snapshot.json';snapshot.write_text(json.dumps(d), encoding='utf-8')
        restore_records(self.layout,snapshot)
        self.assertEqual(Records(self.layout).data['unknown_future_field'],d['unknown_future_field'])
        d['library_id']='another library';snapshot.write_text(json.dumps(d), encoding='utf-8')
        with self.assertRaises(ValueError):restore_records(self.layout,snapshot)
