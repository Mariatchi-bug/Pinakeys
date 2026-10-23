"""Build and verify a clean release from a committed source checkout; never publish."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

from build_usb import build_folder, RUNTIME, source_file
from release_check import inspect, SOURCE_FILES, HISTORY_FILES, kit_files

ROOT = Path(__file__).resolve().parents[1]


def command(args, **kwargs):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=120, **kwargs)
    if result.returncode:
        raise ValueError((result.stdout + result.stderr)[-10000:])
    return result.stdout.strip()


def source_history():
    tracked = set(command(['git', 'ls-files', '-z']).strip('\0').split('\0'))
    if tracked != SOURCE_FILES:
        raise ValueError('Tracked source differs from the reviewed allowlist: ' + str(tracked ^ SOURCE_FILES))
    if command(['git', 'status', '--porcelain=v1', '--untracked-files=all']):
        raise ValueError('Commit reviewed source before building a versioned release.')
    commits = command(['git', 'rev-list', 'HEAD']).splitlines()
    objects = set()
    identities = set()
    for commit in commits:
        rows = command(['git', 'ls-tree', '-r', '-z', commit]).split('\0')
        for row in filter(None, rows):
            metadata, name = row.split('\t', 1)
            mode, kind, sha = metadata.split()
            if name not in HISTORY_FILES or kind != 'blob' or mode == '120000':
                raise ValueError('Unreviewed historical path: ' + name)
            objects.add(sha)
        identities.update(command(['git', 'show', '-s', '--format=%ae%n%ce', commit]).splitlines())
    credential = re.compile(rb'(?:gh[pousr]_|github_pat_|sk-(?:proj-|svcacct-)?)[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
    for sha in objects:
        raw = subprocess.check_output(['git', 'cat-file', 'blob', sha], cwd=ROOT)
        if credential.search(raw) or any(p in raw for p in (b'/'+b'Users/', b'/'+b'Volumes/', b'C:'+b'\\Users\\')):
            raise ValueError('Sensitive marker in reachable source history; inspect privately before release.')
    return {'commits': len(commits), 'unique_blobs': len(objects), 'tracked_files': len(tracked),
            'non_noreply_identities': sum(not x.endswith('@users.noreply.github.com') for x in identities),
            'privacy_scan': 'allowlist and credential/personal-path patterns passed; human review still required'}


INSTALL_SCRIPT = r'''
import json,subprocess,sys
from pathlib import Path
internal=Path(sys.argv[1]);sys.path.insert(0,str(internal))
from library_paths import LibraryPaths
from library_records import Records
from review_library import ReviewStore
from record_backup import restore_records
from catalogue_settings import Selection
from intake_workflow import Intake
root=internal.parent.parent
folder='Existing Café — 資料'
(root/folder).mkdir()
book=root/folder/'Alpha — Writer.txt';book.write_bytes(b'Synthetic offline material.')
(root/folder/'Beta.txt').write_bytes(b'Another synthetic material.')
(root/folder/'omit.zip').write_bytes(b'excluded archive')
def scan(*extra):
    p=subprocess.run([sys.executable,'-B','-S',str(internal/'scan_library.py'),'--inventory-only',*extra],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
    assert p.returncode==0,p.stdout+p.stderr
    return json.loads((root/'Library Tools/Reports/catalogue.json').read_bytes())
rows=scan('--folder',folder,'--exclude','*.zip');assert len(rows)==2
assert book.read_bytes()==b'Synthetic offline material.'
layout=LibraryPaths(root,True)
ReviewStore(layout).save(folder+'/Alpha — Writer.txt',{'title':'Kept correction','author':'Writer'})
assert any(r['title']=='Kept correction' for r in scan())
identity=Records(layout).by_path(folder+'/Alpha — Writer.txt')['id']
sub=root/folder/'Moved';sub.mkdir();book.rename(sub/'Renamed.txt')
new_root=root.with_name(root.name+' reconnected');root.rename(new_root);root=new_root
internal=root/'Library Tools/Internal';layout=LibraryPaths(root,True)
scan('--verify');record=Records(layout).by_path(folder+'/Moved/Renamed.txt')
assert record['id']==identity and record['corrections']['title']=='Kept correction'
snapshot=root.parent/'saved-records.json';snapshot.write_bytes(layout.records.read_bytes())
ReviewStore(layout).save(folder+'/Moved/Renamed.txt',{'title':'Temporary edit','author':'Writer'})
restore_records(layout,snapshot)
assert Records(layout).get(identity)['corrections']['title']=='Kept correction'
arrival=root/'Incoming Files/Arrival.txt';arrival.write_bytes(b'Disposable arrival.')
p=subprocess.run([sys.executable,'-B','-S',str(internal/'audit_materials.py'),'--no-clamav'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
assert p.returncode==0,p.stdout+p.stderr
flow=Intake(layout);flow.reload(quick=True);r=flow.records.by_path('Incoming Files/Arrival.txt')
assert 'Incomplete checks' in flow.categories(r)
flow.review(r['id'],'Reviewed arrival','Writer','Reviewed arrival.txt','Content/Filed',True)
plan=flow.preview([r['id']]);assert arrival.exists()
journal=flow.apply(plan,'FILE 1')
Selection(layout).save([folder,'Content'],['*.zip'])
assert any(r['title']=='Reviewed arrival' for r in scan())
flow=Intake(layout);flow.rollback(journal,confirmation='UNDO 1')
assert arrival.read_bytes()==b'Disposable arrival.'
assert (root/folder/'Moved/Renamed.txt').read_bytes()==b'Synthetic offline material.'
for name in ['catalogue.csv','catalogue.json','catalogue.txt','library_mobile.html']:
    assert (layout.reports/name).is_file()
assert (root/'Open Library.html').is_file()
print(json.dumps({'core_in_place':'passed','utf8_folders':'passed','rename_move_mount_identity':'passed','restore_saved_records':'passed','optional_intake_and_undo':'passed','exports':'passed','bundled_runtime_only':True,'optional_site_packages_disabled':True}))
'''


def install_check(kit):
    with tempfile.TemporaryDirectory(prefix='pinakeys-install-gate-') as temp:
        copy = Path(temp).resolve() / 'USB kit with spaces'
        shutil.copytree(kit, copy)
        internal = copy / 'Library Tools/Internal'
        result = command([sys.executable, '-B', '-S', '-c', INSTALL_SCRIPT, str(internal)])
        report = json.loads(result)
        # Independently create/close the installed GUI when a display is available.
        copy = copy.with_name(copy.name + ' reconnected')
        internal = copy / 'Library Tools/Internal'
        smoke = subprocess.run([sys.executable, '-B', '-S', str(internal / 'pinakeys.py'), '--smoke-test'],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20)
        if smoke.returncode:
            if 'Could not open' not in smoke.stderr and 'needs Python with Tkinter' not in smoke.stderr:
                raise ValueError(smoke.stdout + smoke.stderr)
            report['desktop_smoke'] = 'skipped: Tk/display unavailable; native GUI assertions remain in the test runner'
        else:
            report['desktop_smoke'] = 'passed'
        return report


def zip_kit(kit, target):
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in [kit, *sorted(kit.rglob('*'))]:
            name = kit.name + ('/' + path.relative_to(kit).as_posix() if path != kit else '')
            if path.is_dir():name += '/'
            info = zipfile.ZipInfo.from_file(path, name)
            # Windows filesystems do not expose Unix execute bits. Package
            # declared launcher permissions independently of the build host.
            info.create_system = 3
            executable = path.suffix in ('.sh', '.command') or path.as_posix().endswith('/MacOS/Pinakeys')
            mode = (stat.S_IFDIR | 0o755) if path.is_dir() else (stat.S_IFREG | (0o755 if executable else 0o644))
            info.external_attr = (mode << 16) | (0x10 if path.is_dir() else 0)
            archive.writestr(info, b'' if path.is_dir() else path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)


def build_release(output):
    history = source_history()
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    from catalogue_settings import VERSION
    if version != VERSION or command([sys.executable, '-B', 'app/pinakeys.py', '--version']) != 'Pinakeys ' + version:
        raise ValueError('Version sources disagree.')
    output = Path(output).resolve();output.mkdir(parents=True, exist_ok=False)
    commit = command(['git', 'rev-parse', 'HEAD'])
    kit = build_folder(output / ('Pinakeys-' + version + '-USB'))
    manifest = inspect(kit)
    source = output / ('Pinakeys-' + version + '-source.zip')
    prefix = 'Pinakeys-' + version + '-source/'
    command(['git', 'archive', '--format=zip', '--prefix=' + prefix, '-o', str(source), 'HEAD'])
    with tempfile.TemporaryDirectory(prefix='pinakeys-rebuild-') as temp:
        with zipfile.ZipFile(source) as archive:
            names = [x[len(prefix):] for x in archive.namelist() if not x.endswith('/')]
            if set(names) != SOURCE_FILES or len(names) != len(set(names)) or archive.testzip():
                raise ValueError('Unexpected source archive entries or failed integrity.')
            archive.extractall(temp)
        extracted = Path(temp) / prefix.rstrip('/')
        rebuilt = Path(temp) / 'Rebuilt kit'
        command([sys.executable, '-B', str(extracted / 'tools/build_usb.py'), str(rebuilt)])
        rebuilt_manifest = inspect(rebuilt)
        if rebuilt_manifest['files'] != manifest['files']:
            raise ValueError('Kit differs from its clean source-archive rebuild.')
    usb = output / (kit.name + '.zip');zip_kit(kit, usb)
    with zipfile.ZipFile(usb) as archive:
        names = [x[len(kit.name)+1:] for x in archive.namelist() if not x.endswith('/')]
        if set(names) != kit_files() or len(names) != len(set(names)) or archive.testzip():
            raise ValueError('Unexpected USB archive entries or failed integrity.')
        mode = archive.getinfo(kit.name+'/Library Tools/Mac/Pinakeys.app/Contents/MacOS/Pinakeys').external_attr >> 16
        if not mode & 0o111:raise ValueError('Mac launcher execute permission missing from ZIP.')
        with tempfile.TemporaryDirectory(prefix='pinakeys-zip-install-') as temp:
            archive.extractall(temp)
            for entry in archive.infolist():
                if os.name != 'nt' and entry.external_attr >> 16:
                    (Path(temp)/entry.filename).chmod((entry.external_attr >> 16) & 0o777)
            installation = install_check(Path(temp)/kit.name)
    report = {'version': version, 'commit': commit, 'history': history, 'installation': installation,
              'contents': manifest, 'clean_source_rebuild': 'passed', 'zip_integrity': 'passed',
              'package_status': 'verified'}
    (output/'CONTENTS.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    shutil.copyfile(source_file('RELEASE_NOTES.md'),output/'RELEASE_NOTES.md')
    sums=[]
    for path in [source,usb,output/'CONTENTS.json',output/'RELEASE_NOTES.md']:
        sums.append(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.name)
    (output/'SHA256SUMS').write_text('\n'.join(sums)+'\n',encoding='utf-8')
    return {'version': version, 'commit': commit, 'source_files': len(SOURCE_FILES),
            'kit_files': len(manifest['files']), 'history': history,
            'installation': installation, 'clean_source_rebuild': 'passed', 'zip_integrity': 'passed'}


if __name__ == '__main__':
    from library_paths import configure_console
    configure_console()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,help='NEW directory; existing folders are refused')
    args=parser.parse_args()
    try:
        print(json.dumps(build_release(args.output),indent=2))
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        print('Release gate failed: '+str(exc),file=sys.stderr)
        raise SystemExit(1)
