"""One portable folder/exclusion policy for catalogue and record operations."""
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from library_paths import LibraryPaths

VERSION = '1.0.2'
INTERNAL = {'Library Tools', 'Corrections', 'Reports', 'Record History', 'Filing History',
            'Settings', 'vendor', 'assets', 'tests', '__pycache__', '.git'}
GENERATED = {'library_index.html', 'Open Library.html', 'library_mobile.html', 'catalogue.csv',
             'catalogue.json', 'catalogue.txt', 'metadata_review.csv', 'metadata_corrections.csv',
             'library_records.json', 'library_records.csv', 'intake_audit.json', 'intake_audit.csv',
             'deeper_pdf_check.html', 'catalogue_run.json', 'README.md', 'LICENSE', 'CHANGELOG.md', 'CONTRIBUTING.md',
             'QUICKSTART.md', 'RELEASE_NOTES.md', 'FUTURE_WORK.md', 'DEPENDENCIES.md', 'SECURITY.md', 'SUPPORT.md', 'RELEASING.md'}

def relative(value):
    p = PurePosixPath(value)
    if (not value or p.is_absolute() or PureWindowsPath(value).drive or '\\' in value
            or p.as_posix() != value or '..' in p.parts or '\x00' in value
            or any(part.startswith('.') for part in p.parts)):
        raise ValueError('Choose a folder inside this kit; paths must be relative and cannot traverse or use hidden folders.')
    return value

class Selection:
    def __init__(self, layout):
        self.layout = LibraryPaths(layout.root.resolve(),layout.organized)
        layout=self.layout
        self.path = layout.tools / 'Settings/catalogue.json'
        if self.path.is_symlink() or self.path.parent.is_symlink() or not self.path.resolve().is_relative_to(layout.root.resolve()):
            raise ValueError('Settings must remain inside the library.')
        candidates = [s for s in ('Content', 'Books') if (layout.root/s).is_dir()]
        self.data = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {
            'schema': 1, 'folders': candidates if len(candidates) == 1 else ['.'], 'exclude': []}
        if (self.data.get('schema') != 1 or not isinstance(self.data.get('folders'), list)
                or not self.data['folders'] or not isinstance(self.data.get('exclude'), list)
                or not all(isinstance(x,str) for x in self.data['exclude'])):
            raise ValueError('Invalid folder settings; preserve Settings/catalogue.json before repairing it.')
        for name in self.data['folders']:
            relative(name)
            p = layout.root/name
            if not p.resolve().is_relative_to(layout.root.resolve()) or any(x.is_symlink() for x in [p,*p.parents] if x != layout.root):
                raise ValueError('Selected folders cannot use symbolic links or leave this kit.')

    def save(self, folders, exclusions):
        from library_records import atomic, encoded
        for folder in folders:
            relative(folder)
            target=self.layout.root/folder
            if not target.resolve().is_relative_to(self.layout.root.resolve()) or any(p.is_symlink() for p in [target,*target.parents] if p!=self.layout.root):
                raise ValueError('Selected folders cannot leave the kit or use symlinks.')
            if not target.is_dir():
                raise ValueError('Selected folder is unavailable: '+folder)
        previous=self.data
        self.data={**previous, 'folders':list(dict.fromkeys(folders)), 'exclude':list(exclusions)}
        if not self.data['folders']:
            raise ValueError('Select at least one folder.')
        atomic(self.path,encoded(self.data))

    def contains(self, name, *, intake=False):
        parts=PurePosixPath(name).parts
        if not parts or parts[0] in INTERNAL or any(p.startswith('.') or p=='__pycache__' for p in parts):
            return False
        if len(parts)==1 and (name in GENERATED or name in {'scan_library.py','audit_materials.py','review_library.py','pinakeys.py','library_paths.py','library_style.py','library_records.py','intake_ui.py','intake_workflow.py','catalogue_settings.py','record_backup.py','operation_progress.py','pdf_followup.py','pdf_inspection.py','sanitize_with_dangerzone.py','build_usb.py','release_check.py','run_tests.py','release_gate.py'} or name.startswith(('Run on ','Run safety audit ','Install PDF '))):
            return False
        roots=list(self.data['folders']) + (['Incoming Files','Incoming'] if intake else [])
        if not any(folder=='.' or name==folder or name.startswith(folder+'/') for folder in roots):
            return False
        # Incoming is opt-in for a root catalogue, but always available to Intake.
        if any(p in ('Incoming Files','Incoming') for p in parts) and not intake and not any(
                any(p in ('Incoming Files','Incoming') for p in PurePosixPath(folder).parts) and (name.startswith(folder+'/') or name==folder) for folder in self.data['folders']):
            return False
        return not any(fnmatchcase(name,pattern) or name==pattern.rstrip('/') or name.startswith(pattern.rstrip('/')+'/')
                       or any(fnmatchcase(p,pattern) for p in parts) for pattern in self.data['exclude'])

    def files(self, *, intake=False):
        found={}
        roots=list(self.data['folders']) + (['Incoming Files','Incoming'] if intake else [])
        for folder in roots:
            top=self.layout.root/folder
            if not top.is_dir():
                if folder in self.data['folders']:
                    raise ValueError('Selected folder is unavailable: '+folder)
                continue
            for base,dirs,files in os.walk(top,followlinks=False):
                dirs[:]=[d for d in dirs if not d.startswith('.') and d!='__pycache__' and (Path(base)/d).relative_to(self.layout.root).parts[0] not in INTERNAL and not (Path(base)/d).is_symlink()]
                for name in files:
                    path=Path(base)/name
                    rel=path.relative_to(self.layout.root).as_posix()
                    if self.contains(rel,intake=intake) and not path.is_symlink():
                        found[rel]=path
        from library_records import key
        keys=[key(p) for p in found]
        if len(keys)!=len(set(keys)):
            raise ValueError('Case/Unicode-equivalent file paths need an explicit identity choice before cataloguing.')
        return [found[k] for k in sorted(found,key=str.casefold)]
