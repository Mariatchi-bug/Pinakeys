"""Inspect actual V1 source/empty-kit files. Never deploy or publish."""
import argparse
import hashlib
import json
from pathlib import Path
import py_compile
import tempfile

from build_usb import RUNTIME, DOCS

SOURCE_FILES=set(RUNTIME+DOCS+['CHANGELOG.md','CONTRIBUTING.md','build_usb.py','release_check.py','run_tests.py','release_gate.py','RELEASING.md',
    '.github/rulesets/main.json','.github/rulesets/releases.json','.github/dependabot.yml','.github/PULL_REQUEST_TEMPLATE.md','.github/ISSUE_TEMPLATE/bug_report.yml',
    '.github/ISSUE_TEMPLATE/feature_request.yml','.github/ISSUE_TEMPLATE/config.yml',
    '.gitignore','.gitattributes','.github/workflows/tests.yml','assets/Pinakeys.icns','assets/pinakeys-icon.png',
    'vendor/pdf_parser.py','vendor/README.md'])
SOURCE_FILES.update('tests/'+name for name in ['test_gui_actions.py','test_pdf_followup.py','test_organized_layout.py',
    'test_portability.py','test_review_editor.py','test_regressions.py','test_intake_workflow.py',
    'test_desktop_launcher.py','test_v1_workflows.py','test_release_safety.py'])

def kit_files():
    names={'.gitignore','Open Library.html','Library Tools/Start Here.html','Library Tools/Internal/README.txt'}
    names.update('Library Tools/Internal/'+name for name in RUNTIME)
    names.update('Library Tools/Help/'+name for name in DOCS)
    names.update('Library Tools/Internal/'+name for name in ['vendor/pdf_parser.py','vendor/README.md','assets/pinakeys-icon.png'])
    names.update('Library Tools/Mac/Pinakeys.app/Contents/'+name for name in ['Info.plist','MacOS/Pinakeys','Resources/Pinakeys.icns'])
    for platform,suffix in [('Mac','.command'),('Linux','.sh'),('Windows','.bat')]:
        for action in ['Update Catalogue','Extract Metadata','Safety Audit','Install PDF Support']:
            names.add('Library Tools/'+platform+'/'+action+suffix)
    names.update(['Library Tools/Linux/Open Pinakeys.sh','Library Tools/Windows/Open Pinakeys.bat'])
    return names

def inspect(root, *, source=False):
    root=Path(root).resolve()
    allowed=SOURCE_FILES if source else kit_files()
    if source:
        actual={name for name in allowed if (root/name).is_file()}
    else:
        actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
        if any(p.is_symlink() for p in root.rglob('*')):
            raise ValueError('A release kit must not contain symbolic links.')
    if actual!=allowed:
        raise ValueError('Unexpected/missing release files: '+str(sorted(actual-allowed))+' / '+str(sorted(allowed-actual)))
    results=[]
    with tempfile.TemporaryDirectory(prefix='pinakeys-compile-') as tmp:
        for index,name in enumerate(sorted(actual)):
            p=root/name
            if p.is_symlink():raise ValueError('Source symlink: '+name)
            data=p.read_bytes()
            if any(marker in data for marker in [b'/'+b'Users/',b'/'+b'Volumes/',b'C:'+b'\\Users\\']):
                raise ValueError('Personal information marker found in '+name)
            if p.suffix=='.py':
                py_compile.compile(str(p),cfile=str(Path(tmp)/(str(index)+'.pyc')),doraise=True)
            results.append({'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    if not source:
        for folder in ['Content','Incoming Files','Library Tools/Reports','Library Tools/Corrections']:
            if any((root/folder).rglob('*')):
                raise ValueError('Public kit must be empty: '+folder)
        import plistlib
        info=plistlib.loads((root/'Library Tools/Mac/Pinakeys.app/Contents/Info.plist').read_bytes())
        if info['CFBundleShortVersionString']!='1.0.2':raise ValueError('Wrong bundle version')
    return {'version':'1.0.2','kind':'source allowlist' if source else 'empty USB kit',
            'files':results,'private_payloads_found':False,'syntax_compilation':'passed'}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder');parser.add_argument('--source',action='store_true')
    args=parser.parse_args()
    print(json.dumps(inspect(args.folder,source=args.source),indent=2))
