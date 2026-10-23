"""Build a clean USB kit in a NEW folder; never migrate an existing library."""
import argparse
from pathlib import Path
import shutil
import plistlib
from library_style import BASE_CSS

SOURCE = Path(__file__).resolve().parent
DOCS = ['README.md', 'LICENSE', 'QUICKSTART.md', 'VERSION', 'DEPENDENCIES.md', 'RELEASE_NOTES.md', 'FUTURE_WORK.md', 'SECURITY.md', 'SUPPORT.md']
RUNTIME = [
    'scan_library.py', 'audit_materials.py', 'sanitize_with_dangerzone.py', 'library_paths.py', 'library_style.py', 'pinakeys.py', 'review_library.py',
    'pdf_followup.py', 'pdf_inspection.py', 'library_records.py', 'intake_workflow.py', 'intake_ui.py', 'catalogue_settings.py', 'record_backup.py', 'operation_progress.py',
    'Run on Mac.command', 'Run on Linux.sh', 'Run on Windows.bat',
    'Run safety audit (Mac).command', 'Run safety audit (Linux).sh',
    'Run safety audit (Windows).bat',
    'Install PDF metadata support (Mac-Linux).sh', 'Install PDF metadata support (Windows).bat',
]

START_HERE = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pinakeys 1.0.2 · Tools and help</title><style>__SHARED_STYLE__ body{max-width:1000px;line-height:1.65}.grid{display:grid;grid-template-columns:1fr 1fr;gap:28px}h2{font-size:22px}@media(max-width:700px){.grid{display:block}}</style><header class="site-header"><span class="brand">Pinakeys 1.0.2</span><a href="../Open%20Library.html">Your catalogue</a></header><main><h1>Your materials, where you keep them.</h1><p>Open Pinakeys from the Mac folder. Windows and Linux/uConsole launchers are experimental. Python 3.10+ and Tkinter are required for the app.</p><p><a href="Help/QUICKSTART.md">First-use instructions</a> · <a href="Help/README.md">Full guide</a></p><div class="grid"><section><h2>Catalogue existing folders</h2><p>Choose <strong>Folders and exclusions…</strong>, select collections inside this kit’s common parent, and save. No file reorganization is needed. <strong>Refresh Catalogue</strong> lists paths and attributes quickly. <strong>Open Library</strong> lets you search and open originals offline.</p><p><strong>Full verification…</strong> is a separate action that reads the selected files completely. Use it after renames/moves or to detect changed bytes. Cached attributes are not proof. Stop Current Action cancels a running verification.</p></section><section><h2>Correct and recover</h2><p><strong>Review Files</strong> opens populated title/author fields. Untick Needs review only to see every entry. Saving verifies that file and keeps corrections separate from the document.</p><p>Keep <strong>Settings</strong> and <strong>Corrections</strong> with the drive. <strong>Restore records from backup…</strong> restores a full saved JSON ledger with a backup of the current one. Missing and unmatched records are retained.</p></section><section><h2>Optional intake</h2><p><strong>Check Incoming → Intake · Review and File… → Preview → Confirm filing</strong>. The audit reports actual tool coverage and separate formatting, incomplete-check, security, metadata and duplicate findings. A completed run is not a safety certificate.</p><p>Filing requires typing FILE N; undo requires UNDO N. Journals support recovery after interruption. Deeper PDF Check is a separate explicit follow-up; no original is rewritten.</p></section><section><h2>Portable views</h2><p>These links become available after the first update:</p><ul><li><a href="Reports/library_mobile.html">Simple HTML view</a></li><li><a href="Reports/catalogue.csv">CSV</a> · <a href="Reports/catalogue.json">JSON</a> · <a href="Reports/catalogue.txt">Text</a></li></ul><p><a href="Help/DEPENDENCIES.md">Optional dependencies and notices</a> · <a href="Help/LICENSE">MIT license</a></p></section></div></main></html>'''.replace('__SHARED_STYLE__', BASE_CSS)


def build_folder(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for name in ['Content', 'Incoming Files', 'Library Tools/Internal', 'Library Tools/Reports',
                 'Library Tools/Corrections', 'Library Tools/Help', 'Library Tools/Mac',
                 'Library Tools/Windows', 'Library Tools/Linux']:
        (destination / name).mkdir(parents=True, exist_ok=True)
    internal = destination / 'Library Tools/Internal'
    for name in RUNTIME:
        if name.endswith('.bat'):
            data = (SOURCE / name).read_bytes().replace(b'\r\n', b'\n')
            (internal / name).write_bytes(data.replace(b'\n', b'\r\n'))
        else:
            shutil.copyfile(SOURCE / name, internal / name)
    (internal / 'vendor').mkdir()
    for name in ('pdf_parser.py', 'README.md'):
        shutil.copyfile(SOURCE / 'vendor' / name, internal / 'vendor' / name)
    (internal / 'assets').mkdir()
    shutil.copyfile(SOURCE / 'assets/pinakeys-icon.png', internal / 'assets/pinakeys-icon.png')
    for name in DOCS:
        shutil.copyfile(SOURCE / name, destination / 'Library Tools/Help' / name)
    shutil.copyfile(SOURCE / '.gitignore', destination / '.gitignore')
    (destination / 'Library Tools/Start Here.html').write_text(START_HERE, encoding='utf-8')
    (destination / 'Library Tools/Internal/README.txt').write_text(
        'These files support the actions in Mac, Windows and Linux. Leave this folder in place.\n'
        'For ordinary use, open Library Tools/Start Here.html or the platform folders.\n', encoding='utf-8')
    (destination / 'Open Library.html').write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Your library · Pinakeys</title><style>' + BASE_CSS + '</style>'
        '<header class="site-header"><span class="brand">Pinakeys</span>'
        '<a class="header-link" href="Library%20Tools/Start%20Here.html">Tools &amp; help</a></header>'
        '<main><h1>Your library</h1><p class="lede">Your first catalogue starts here.</p>'
        '<p>Open Pinakeys from your computer\'s folder in Library Tools and choose Refresh Catalogue, '
        'or run Update Catalogue. Then reopen this file.</p></main></html>',
        encoding='utf-8')
    actions = {
        'Update Catalogue': ('scan', '--inventory-only'),
        'Extract Metadata': ('scan', ''),
        'Safety Audit': ('audit', ''),
        'Install PDF Support': ('install', ''),
    }
    for platform, suffix in [('Mac', '.command'), ('Linux', '.sh'), ('Windows', '.bat')]:
        for label, (action, flags) in actions.items():
            if action == 'install':
                script = ('Install PDF metadata support (Windows).bat' if platform == 'Windows'
                          else 'Install PDF metadata support (Mac-Linux).sh')
            elif action == 'audit':
                script = f'Run safety audit ({platform})' + suffix
            else:
                script = f'Run on {platform}' + suffix
            if platform == 'Windows':
                content = f'@echo off\ncall "%~dp0..\\Internal\\{script}" {flags} %*\nexit /b %errorlevel%\n'
            else:
                content = ('#!/bin/sh\nROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1\n'
                           f'sh "$ROOT/../Internal/{script}" {flags} "$@"\n')
                if platform == 'Mac' and action == 'install':
                    content += ('STATUS=$?\nif [ -t 0 ]; then\n'
                                '    printf "Press Return to close this window... "\n'
                                '    read -r answer\nfi\nexit "$STATUS"\n')
            target = destination / 'Library Tools' / platform / (label + suffix)
            target.write_bytes(content.replace('\n', '\r\n').encode() if platform == 'Windows' else content.encode())
    build_desktop_launchers(destination)
    for path in destination.rglob('*'):
        if path.suffix in {'.command', '.sh'}:
            path.chmod(0o755)
    return destination


def build_desktop_launchers(destination):
    """Small entry points; Python and Tk remain computer-local prerequisites."""
    tools = Path(destination) / 'Library Tools'
    app = tools / 'Mac/Pinakeys.app/Contents'
    (app / 'MacOS').mkdir(parents=True)
    (app / 'Resources').mkdir()
    shutil.copyfile(SOURCE / 'assets/Pinakeys.icns', app / 'Resources/Pinakeys.icns')
    (app / 'Info.plist').write_bytes(plistlib.dumps({
        'CFBundleName': 'Pinakeys', 'CFBundleDisplayName': 'Pinakeys',
        'CFBundleIdentifier': 'org.pinakeys.launcher', 'CFBundleExecutable': 'Pinakeys',
        'CFBundlePackageType': 'APPL', 'CFBundleVersion': '10002',
        'CFBundleShortVersionString': '1.0.2', 'NSHighResolutionCapable': True,
        'CFBundleIconFile': 'Pinakeys.icns',
    }))
    posix = ('#!/bin/sh\nROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1\n'
             'PYTHON=""\n'
             'for CANDIDATE in "$HOME/.usb-book-index/venv/bin/python" /usr/local/bin/python3 '
             '/opt/homebrew/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 '
             '"$(command -v python3)"; do\n'
             '    if [ -x "$CANDIDATE" ]; then PYTHON="$CANDIDATE"; break; fi\n'
             'done\n'
             'if [ -z "$PYTHON" ]; then echo "Python 3 with Tkinter is required. See the setup guide." >&2; exit 1; fi\n')
    # Finder does not normally include Homebrew's tool directories on PATH.
    mac = posix.replace('PYTHON=""\n',
                        'export PATH="${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}:/opt/homebrew/bin:/usr/local/bin"\nPYTHON=""\n', 1)
    mac += 'exec "$PYTHON" "$ROOT/../../../../Internal/pinakeys.py" "$@"\n'
    (app / 'MacOS/Pinakeys').write_text(mac, encoding='utf-8')
    (app / 'MacOS/Pinakeys').chmod(0o755)
    linux = tools / 'Linux/Open Pinakeys.sh'
    linux.write_text(posix + 'exec "$PYTHON" "$ROOT/../Internal/pinakeys.py" "$@"\n', encoding='utf-8')
    linux.chmod(0o755)
    windows = '''@echo off
setlocal
set "SCRIPT=%~dp0..\\Internal\\pinakeys.py"
set "VENV=%LOCALAPPDATA%\\USBBookIndex\\venv\\Scripts"
if exist "%VENV%\\pythonw.exe" (
    start "" "%VENV%\\pythonw.exe" "%SCRIPT%"
    exit /b 0
)
where pyw >nul 2>nul
if not errorlevel 1 (
    start "" pyw -3 "%SCRIPT%"
    exit /b 0
)
python "%SCRIPT%"
if errorlevel 1 pause
'''
    (tools / 'Windows/Open Pinakeys.bat').write_bytes(windows.replace('\n', '\r\n').encode())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', help='New, empty destination; existing folders are refused')
    args = parser.parse_args()
    print('Created:', build_folder(args.destination))
