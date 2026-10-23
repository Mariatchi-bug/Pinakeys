"""Paths for the original flat kit and the organized USB distribution."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LibraryPaths:
    root: Path
    organized: bool

    @property
    def tools(self):
        return self.root / 'Library Tools' if self.organized else self.root

    @property
    def reports(self):
        return self.tools / 'Reports' if self.organized else self.root

    @property
    def corrections(self):
        folder = self.tools / 'Corrections' if self.organized else self.root
        return folder / 'metadata_corrections.csv'

    @property
    def catalogue(self):
        return self.root / ('Open Library.html' if self.organized else 'library_index.html')

    @property
    def records(self):
        return self.root / ('Library Tools/Corrections/library_records.json' if self.organized
                            else 'Corrections/library_records.json')

    def prepare_reports(self):
        for path in (self.reports, self.corrections.parent):
            if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                raise ValueError('Report/corrections folder must stay inside the library')
            path.mkdir(parents=True, exist_ok=True)


def library_paths(script):
    internal = Path(script).resolve().parent
    organized = internal.name == 'Internal' and internal.parent.name == 'Library Tools'
    return LibraryPaths(internal.parent.parent if organized else internal, organized)


def configure_console():
    """Use the same UTF-8 contract for redirected worker logs and reports."""
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')


def file_identity(info):
    """Comparable path/handle attributes; not proof of unchanged contents.

    Windows Python can report creation time for stat().st_ctime but change
    time for fstat().st_ctime (CPython issue 157671). Compare each source's
    own ctime before/after instead of comparing those two meanings.
    """
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


def file_state(info):
    """A version guard for observations from the SAME stat source."""
    return (*file_identity(info), info.st_ctime_ns)
