#!/usr/bin/env python3
"""Explicit, opt-in wrapper around an independently installed Dangerzone CLI.

Runs no conversions unless given one Incoming/ file as an argument. Leaves
originals in place and saves the sanitized PDF in Clean Copies/.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
from library_paths import library_paths

LAYOUT = library_paths(__file__)
ROOT = LAYOUT.root
EXTENSIONS = {'.pdf', '.epub', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
              '.odt', '.ods', '.odp', '.odg', '.hwp', '.hwpx', '.jpg', '.jpeg',
              '.gif', '.png', '.svg', '.bmp', '.pnm', '.pbm', '.ppm', '.tif', '.tiff'}


def locate_cli(override: str | None) -> str | None:
    if override:
        return override if Path(override).is_file() or shutil.which(override) else None
    located = shutil.which('dangerzone-cli')
    if located:
        return located
    candidates = (
        '/Applications/Dangerzone.app/Contents/MacOS/dangerzone-cli',
        r'C:\Program Files\Dangerzone\dangerzone-cli.exe',
    )
    return next((value for value in candidates if Path(value).is_file()), None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Convert ONE Incoming document to a new PDF using installed Dangerzone.')
    parser.add_argument('file', help='Path relative to USB root, e.g. Incoming/paper.epub')
    parser.add_argument('--ocr-lang', help='Optional Tesseract OCR language code, e.g. eng')
    parser.add_argument('--cli', help='Optional path to independently installed dangerzone-cli executable')
    args = parser.parse_args(argv)
    cli = locate_cli(args.cli)
    if not cli:
        parser.error('Dangerzone CLI not found. Install from https://dangerzone.rocks/ first.')
    raw_source = ROOT / args.file
    source = raw_source.resolve()
    allowed = [ROOT / name for name in ('Incoming', 'Incoming Files')]
    if not any(not folder.is_symlink() and source.is_relative_to(folder)
               for folder in allowed):
        parser.error('Only sources inside Incoming/ or Incoming Files/ are accepted.')
    if not source.is_file() or raw_source.is_symlink() or source.suffix.casefold() not in EXTENSIONS:
        parser.error('Source is missing, is a symlink, or has an unsupported file extension.')
    target_dir = ROOT / 'Content' / 'Clean Copies' if LAYOUT.organized else ROOT / 'Clean Copies'
    if target_dir.is_symlink() or not target_dir.resolve().is_relative_to(ROOT):
        parser.error('Clean Copies directory is unsafe or points off this drive')
    target = target_dir / f'{source.stem}-safe.pdf'
    if target.exists() or target.is_symlink():
        parser.error('Refusing to overwrite existing sanitized PDF: ' + target.name)
    target_dir.mkdir(exist_ok=True)
    command = [cli, '--output-filename', str(target)]
    if args.ocr_lang:
        command += ['--ocr-lang', args.ocr_lang]
    command.append(str(source))
    print('Original (unchanged): ' + source.relative_to(ROOT).as_posix())
    print('New PDF destination: ' + target.relative_to(ROOT).as_posix())
    print('Running locally installed Dangerzone...')
    try:
        outcome = subprocess.run(command, check=False, stdin=subprocess.DEVNULL)
    except OSError as exc:
        print(f'Could not start Dangerzone: {exc}', file=sys.stderr)
        return 1
    if outcome.returncode or not target.is_file():
        print('Dangerzone did not confirm a completed conversion. Review its output above.', file=sys.stderr)
        return 1
    print('Sanitized PDF created. Original remains in its intake folder. Inspect the PDF before using it.')
    return 0


if __name__ == '__main__':
    from library_paths import configure_console
    configure_console()
    raise SystemExit(main())
