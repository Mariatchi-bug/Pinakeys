"""Explicit recovery of portable records; never touch document contents."""
import json
from pathlib import Path
import tempfile

from library_paths import LibraryPaths
from library_records import Records, RecordError, digest

def restore_records(layout, source):
    from intake_workflow import Intake
    current=Records(layout)
    if Intake(layout).pending():
        raise RecordError('Recover interrupted filing before restoring records.')
    raw=Path(source).read_bytes()
    with tempfile.TemporaryDirectory(prefix='pinakeys-record-validation-') as name:
        root=Path(name);(root/'Corrections').mkdir()
        (root/'Corrections/library_records.json').write_bytes(raw)
        candidate=Records(LibraryPaths(root,False)).data
    if current.exists and current.data['records'] and candidate['library_id']!=current.data['library_id']:
        raise RecordError('This backup belongs to a different library. No records were replaced.')
    with current.lock():
        revision=current.data['revision']
        current.data=candidate
        current.data['revision']=revision
        current.data.setdefault('restore_history',[]).append({'source_sha256':digest(raw)})
        for r in current.data['records']:
            r['content_unverified']=True
        current.save()
        current.export()
    return len(candidate['records'])
