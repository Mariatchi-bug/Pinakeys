"""Small desktop launcher for Pinakeys. No listener, server, or automatic scan."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import webbrowser

from library_paths import library_paths
from library_style import PALETTE


def worker_python() -> str:
    if os.name == 'nt':
        environment = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'USBBookIndex/venv/Scripts/python.exe'
    else:
        environment = Path.home() / '.usb-book-index/venv/bin/python'
    if environment.is_file() and os.access(environment, os.X_OK):
        return str(environment)
    current = Path(sys.executable)
    console = current.with_name('python.exe') if current.name.lower() == 'pythonw.exe' else current
    return str(console)


def capabilities(python: str) -> dict:
    runtime = False
    try:
        result = subprocess.run(
            [python, '-c', 'import importlib.util; print(bool(importlib.util.find_spec("pymupdf")))'],
            capture_output=True, text=True, timeout=8, check=False,
            **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
        runtime = result.returncode == 0 and result.stdout.strip() in {'True', 'False'}
        pdf = runtime and result.stdout.strip() == 'True'
    except (OSError, subprocess.TimeoutExpired):
        pdf = False
    return {'Python runtime': runtime, 'PDF metadata': pdf, 'EPUB metadata': runtime,
            'ClamAV': bool(shutil.which('clamscan')),
            'PDFiD': bool(shutil.which('pdfid.py') or shutil.which('pdfid')),
            'EPUBCheck': bool(shutil.which('epubcheck'))}


class ActionRunner:
    """Run only fixed actions, with a private temporary log and bounded polling."""
    ACTIONS = {'refresh': ('scan_library.py', '--inventory-only'),
               'verify': ('scan_library.py', '--inventory-only', '--verify'),
               'migrate': ('scan_library.py', '--inventory-only', '--verify', '--include-intake', '--records-only'),
               'metadata': ('scan_library.py',), 'audit': ('audit_materials.py',),
               'pdf': ('pdf_followup.py',)}

    def __init__(self, script=__file__, python=None):
        self.layout = library_paths(script)
        self.internal = Path(script).resolve().parent
        self.python = python or worker_python()
        self.process = None
        self.temporary = tempfile.TemporaryDirectory(prefix='pinakeys-session-')
        self.log = Path(self.temporary.name) / 'run.log'
        self.progress_path = Path(self.temporary.name) / 'progress.jsonl'
        self.progress_offset = 0
        self.progress_buffer = ''
        self.progress_discard = False
        self.stream = None
        self.offset = 0
        self.cancelled = False

    @property
    def busy(self):
        return self.process is not None and self.process.poll() is None

    def start(self, action):
        if self.busy:
            raise RuntimeError('An action is already running')
        if action not in self.ACTIONS:
            raise ValueError('Unknown action')
        if self.stream:
            self.stream.close()
        self.stream = self.log.open('w', encoding='utf-8')
        self.offset = 0
        self.cancelled = False
        self.progress_path.unlink(missing_ok=True)
        self.progress_offset = 0
        self.progress_buffer = ''
        self.progress_discard = False
        program, *arguments = self.ACTIONS[action]
        if action in ('audit', 'pdf', 'refresh', 'metadata', 'verify', 'migrate'):
            arguments.extend(['--progress-file', str(self.progress_path)])
        options = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
                   if os.name == 'nt' else {'start_new_session': True})
        try:
            self.process = subprocess.Popen(
                [self.python, '-u', str(self.internal / program), *arguments],
                cwd=self.layout.root, stdin=subprocess.DEVNULL, stdout=self.stream,
                stderr=subprocess.STDOUT, **options)
        except OSError:
            self.stream.close()
            self.stream = None
            raise

    def read_output(self):
        if not self.log.exists():
            return ''
        with self.log.open(encoding='utf-8', errors='replace') as stream:
            stream.seek(self.offset)
            text = stream.read(32768)
            self.offset = stream.tell()
        return text

    @property
    def progress_pending(self):
        return self.progress_path.exists() and self.progress_offset < self.progress_path.stat().st_size

    def read_progress(self):
        """Read only complete, bounded records from the private worker channel."""
        if not self.progress_path.exists():
            return []
        with self.progress_path.open(encoding='utf-8', errors='replace') as stream:
            stream.seek(self.progress_offset)
            text = stream.read(65536)
            self.progress_offset = stream.tell()
        if self.progress_discard:
            if '\n' not in text:
                return []
            text = text.split('\n', 1)[1]
            self.progress_discard = False
        lines = (self.progress_buffer + text).split('\n')
        self.progress_buffer = lines.pop()
        if len(self.progress_buffer) > 16384:
            self.progress_buffer = ''
            self.progress_discard = True
        events = []
        for line in lines:
            if len(line) > 16384:
                continue
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(event, dict):
                continue
            if any(type(event.get(key)) is not int for key in ('version', 'stage', 'done', 'total')):
                continue
            if (event['version'] != 1 or event['stage'] not in (1, 2, 3)
                    or event.get('state') not in ('loading', 'running', 'skipped', 'complete')
                    or not 0 <= event['done'] <= event['total'] <= 10**9
                    or not isinstance(event.get('file', ''), str) or len(event.get('file', '')) > 4096):
                continue
            events.append(event)
        return events

    def cancel(self):
        if not self.busy:
            return
        self.cancelled = True
        if os.name == 'nt':
            result = subprocess.run(['taskkill', '/PID', str(self.process.pid), '/T', '/F'],
                                    capture_output=True, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode and self.busy:
                self.process.terminate()
        else:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def close(self):
        if self.busy:
            raise RuntimeError('Wait for the running action before closing')
        if self.stream:
            self.stream.close()
        self.temporary.cleanup()


def open_local(path):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError('This report has not been generated yet')
    if path.suffix.lower() == '.html':
        try:
            opened = webbrowser.open(path.resolve().as_uri())
        except webbrowser.Error as exc:
            raise OSError('Could not open the browser: ' + str(exc)) from exc
        if not opened:
            raise OSError('Could not open the browser. Open the catalogue from your file manager.')
    elif os.name == 'nt':
        os.startfile(path)
    else:
        try:
            result = subprocess.run(['open' if sys.platform == 'darwin' else 'xdg-open', str(path)],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    errors='replace', timeout=10, check=False)
        except subprocess.TimeoutExpired as exc:
            raise OSError('Could not confirm that the file opened. Check your file manager.') from exc
        if result.returncode:
            raise OSError(result.stderr.strip() or 'The default application could not open this file.')


def reveal_local(path):
    """Reveal a material in Finder; keep the folder action on other systems."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError('This file is missing or unavailable. Refresh the catalogue before trying again.')
    if sys.platform != 'darwin':
        return open_local(path.parent)
    try:
        result = subprocess.run(['/usr/bin/open', '-R', str(path.resolve())],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                errors='replace', timeout=10, check=False)
    except subprocess.TimeoutExpired as exc:
        raise OSError('Finder did not respond. Try showing the file again.') from exc
    if result.returncode:
        raise OSError(result.stderr.strip() or 'Finder could not show this file.')


TRASH_SCRIPT = '''ObjC.import('Foundation');
function run(argv) {
    var url = $.NSURL.fileURLWithPath(argv[0]);
    if (!$.NSFileManager.defaultManager.trashItemAtURLResultingItemURLError(url, null, null)) {
        throw Error('The file could not be moved to Trash. No permanent deletion was attempted.');
    }
    return 'PINakeys_TRASH_OK';
}'''


def trash_local(path):
    """Use the Mac's native Trash operation; never fall back to deletion."""
    if sys.platform != 'darwin':
        raise OSError('Moving to Trash is available on Mac in this version.')
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise OSError('The selected file is missing or is no longer a regular file. Refresh the catalogue.')
    native_path = unicodedata.normalize('NFC', str(path.absolute()))
    try:
        result = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', '-e', TRASH_SCRIPT, native_path],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                errors='replace', timeout=15, check=False)
    except subprocess.TimeoutExpired as exc:
        raise OSError('The Trash request was not confirmed. Check Trash and refresh the catalogue before trying again.') from exc
    if result.returncode:
        raise OSError(result.stderr.strip() or 'The file could not be moved to Trash. No permanent deletion was attempted.')
    if result.stdout.strip() != 'PINakeys_TRASH_OK' or path.exists():
        raise OSError('The file move was not confirmed. Check Trash and refresh the catalogue.')


class PinakeysWindow:
    def __init__(self, root, runner):
        import tkinter as tk
        from tkinter import ttk
        from tkinter import font as tkfont
        from tkinter.scrolledtext import ScrolledText
        self.root, self.runner = root, runner
        self.ttk = ttk
        self.closing = False
        self.action = None
        self.review_window = None
        self.intake_window = None
        self.support = {}
        self.tools_checked = False
        self.has_run = False
        self.audit_started = None
        self.audit_event = None
        root.title('Pinakeys')
        self.app_icon = None
        try:
            self.app_icon = tk.PhotoImage(master=root, file=str(runner.internal / 'assets/pinakeys-icon.png'))
            root.iconphoto(True, self.app_icon)
        except (tk.TclError, OSError):
            # A missing optional image must not prevent access to the library.
            self.app_icon = None
        root.minsize(650, 680)
        root.geometry('760x800')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.configure(background=PALETTE['paper'])
        style = ttk.Style(root)
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        heading_font = 'Georgia' if 'Georgia' in tkfont.families(root) else 'Times'
        style.configure('TFrame', background=PALETTE['paper'])
        style.configure('TLabel', background=PALETTE['paper'], foreground=PALETTE['text'], font=('TkDefaultFont', 11))
        style.configure('Brand.TLabel', foreground=PALETTE['ink'], font=(heading_font, 30, 'bold'))
        style.configure('Muted.TLabel', foreground=PALETTE['muted'], font=('TkDefaultFont', 10))
        style.configure('Summary.TLabel', foreground=PALETTE['ink'], background=PALETTE['wash'],
                        font=('TkDefaultFont', 13, 'bold'), padding=(14, 12))
        style.configure('Section.TLabel', foreground=PALETTE['ink'], font=('TkDefaultFont', 11, 'bold'))
        style.configure('TButton', background=PALETTE['surface'], foreground=PALETTE['ink'],
                        bordercolor=PALETTE['rule'], lightcolor=PALETTE['surface'], darkcolor=PALETTE['rule'],
                        relief='flat', borderwidth=1, padding=(12, 10), font=('TkDefaultFont', 11))
        style.map('TButton', background=[('disabled', PALETTE['paper']), ('active', PALETTE['wash'])],
                  foreground=[('disabled', PALETTE['muted'])], bordercolor=[('focus', PALETTE['accent'])])
        style.configure('Primary.TButton', background=PALETTE['accent'], foreground=PALETTE['surface'],
                        bordercolor=PALETTE['accent'], lightcolor=PALETTE['accent'], darkcolor=PALETTE['accent'])
        style.map('Primary.TButton', background=[('disabled', PALETTE['wash']), ('active', PALETTE['accent_hover'])],
                  foreground=[('disabled', PALETTE['muted']), ('!disabled', PALETTE['surface'])])
        style.configure('Quiet.TButton', padding=(10, 5), font=('TkDefaultFont', 10))
        style.configure('Remove.TButton', padding=(10, 5), font=('TkDefaultFont', 10), foreground=PALETTE['review'])
        style.configure('TCheckbutton', background=PALETTE['paper'], foreground=PALETTE['muted'],
                        font=('TkDefaultFont', 10), padding=(0, 4))
        style.map('TCheckbutton', background=[('active', PALETTE['paper'])],
                  indicatorbackground=[('selected', PALETTE['accent'])])
        style.configure('Horizontal.TProgressbar', background=PALETTE['accent'], troughcolor=PALETTE['wash'],
                        bordercolor=PALETTE['wash'], lightcolor=PALETTE['accent'], darkcolor=PALETTE['accent'], thickness=4)
        shell = ttk.Frame(root, padding=(28, 22, 28, 24))
        shell.pack(fill='both', expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(0, weight=3, minsize=200)
        shell.rowconfigure(2, weight=1, minsize=60)
        # Reserve the status/cancellation area. Long paths, platform fonts or
        # expanded setup controls can scroll without pushing Stop off screen.
        controls = ttk.Frame(shell)
        controls.grid(row=0, column=0, sticky='nsew')
        controls.columnconfigure(0, weight=1)
        controls.rowconfigure(0, weight=1)
        self.action_canvas = tk.Canvas(controls, height=450, width=590,
                                       background=PALETTE['paper'], highlightthickness=0)
        self.action_canvas.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(controls, orient='vertical', command=self.action_canvas.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        main = ttk.Frame(self.action_canvas)
        content = self.action_canvas.create_window((0, 0), window=main, anchor='nw')
        main.bind('<Configure>', lambda event: self.action_canvas.configure(scrollregion=self.action_canvas.bbox('all')))
        self.action_canvas.bind('<Configure>', lambda event: self.action_canvas.itemconfigure(content, width=event.width))
        def scroll_position(first, last):
            scroll.set(first, last)
            if float(first) <= 0 and float(last) >= 1:
                scroll.grid_remove()
            else:
                scroll.grid()
        self.action_canvas.configure(yscrollcommand=scroll_position)
        run_area = ttk.Frame(shell)
        run_area.grid(row=1, column=0, sticky='ew')
        ttk.Label(main, text='Pinakeys', style='Brand.TLabel').pack(anchor='w')
        ttk.Label(main, text='Your materials, kept findable.', style='Muted.TLabel').pack(anchor='w', pady=(3, 12))
        ttk.Label(main, text=str(runner.layout.root), style='Muted.TLabel', wraplength=590).pack(anchor='w')
        self.summary = tk.StringVar()
        ttk.Label(main, textvariable=self.summary, style='Summary.TLabel', wraplength=550).pack(fill='x', pady=(16, 18))
        actions = ttk.Frame(main)
        actions.pack(fill='x')
        actions.columnconfigure((0, 1), weight=1)
        self.buttons = []
        intake_button = ttk.Button(main, text='Intake · Review and File…', command=self.open_intake, style='Quiet.TButton')
        intake_button.pack(after=actions, anchor='w', pady=(0, 6))
        self.buttons.append(intake_button)
        style.configure('Toolbar.TButton', padding=(5,3), font=('TkDefaultFont',9))
        toolbar=ttk.Frame(main)
        toolbar.pack(before=actions,fill='x',pady=(0,8))
        for label,callback in [('Folders and exclusions…',self.folder_settings),('Full verification…',lambda:self.start('verify')),('Restore records from backup…',self.restore_backup)]:
            control=ttk.Button(toolbar,text=label,command=callback,style='Toolbar.TButton')
            control.pack(side='left',padx=(0,6))
            self.buttons.append(control)
        choices = [('Open Library', lambda: self.open(runner.layout.catalogue), 'Browse the last saved catalogue.'),
                   ('Refresh Catalogue', lambda: self.start('refresh'), 'Quick update of the selected folders; no full fingerprint pass.'),
                   ('Check Incoming', lambda: self.start('audit'), 'Inspect arrivals using available local tools.'),
                   ('Review Files', self.open_review, 'See review reasons and correct titles or authors.'),
                   ('Deeper PDF Check', lambda: self.start('pdf'), 'Investigate PDF feature warnings in the saved audit.'),
                   ('Open PDF Findings', lambda: self.open(runner.layout.reports / 'deeper_pdf_check.html'), 'Read the last completed deeper PDF report.')]
        for index, (label, command, description) in enumerate(choices):
            cell = ttk.Frame(actions, padding=(0, 0, 14 if index % 2 == 0 else 0, 14))
            cell.grid(row=index // 2, column=index % 2, sticky='nsew')
            button = ttk.Button(cell, text=label, command=command,
                                style='Primary.TButton' if index == 0 else ('Quiet.TButton' if index >= 4 else 'TButton'))
            button.pack(fill='x')
            ttk.Label(cell, text=description, style='Muted.TLabel', wraplength=275).pack(anchor='w', pady=(6, 0))
            self.buttons.append(button)
        self.advanced = tk.BooleanVar(value=False)
        self.advanced_toggle = ttk.Checkbutton(main, text='Metadata, setup and other reports', variable=self.advanced,
                                              command=self.toggle_advanced)
        self.advanced_toggle.pack(anchor='w', pady=(0, 8))
        self.advanced_frame = ttk.Frame(main)
        advanced_actions = ttk.Frame(self.advanced_frame)
        advanced_actions.pack(anchor='w')
        metadata = ttk.Button(advanced_actions, text='Read Document Metadata', command=lambda: self.start('metadata'), style='Quiet.TButton')
        metadata.pack(side='left')
        self.buttons.append(metadata)
        ttk.Button(advanced_actions, text='Open Setup Folder', command=self.open_setup, style='Quiet.TButton').pack(side='left', padx=8)
        ttk.Button(advanced_actions, text='Open Reports', command=lambda: self.open(runner.layout.reports), style='Quiet.TButton').pack(side='left')
        self.tools = tk.StringVar(value='Checking optional tools…')
        ttk.Label(self.advanced_frame, textvariable=self.tools, style='Muted.TLabel', wraplength=570).pack(anchor='w', pady=(10, 6))
        self.status = tk.StringVar(value='Ready. Choose an action above.')
        ttk.Label(run_area, textvariable=self.status, wraplength=570).pack(anchor='w', pady=(10, 0))
        self.progress = ttk.Progressbar(run_area, mode='indeterminate')
        self.progress.pack(fill='x', pady=(8, 8))
        self.progress_text = tk.StringVar(value='')
        self.progress_note = ttk.Label(run_area, textvariable=self.progress_text, style='Muted.TLabel', wraplength=550)
        log_header = ttk.Frame(run_area)
        log_header.pack(fill='x', pady=(12, 6))
        ttk.Label(log_header, text='Run details', style='Section.TLabel').pack(side='left')
        self.stop = ttk.Button(log_header, text='Stop Current Action', command=self.cancel, state='disabled', style='Quiet.TButton')
        self.stop.pack(side='right')
        self.details = ScrolledText(shell, height=5, wrap='word', font=('TkFixedFont', 10), state='normal',
                                   background=PALETTE['surface'], foreground=PALETTE['text'], relief='flat',
                                   borderwidth=0, highlightthickness=1, highlightbackground=PALETTE['rule'],
                                   highlightcolor=PALETTE['accent'], padx=12, pady=10)
        self.details.insert('1.0', self.audit_overview())
        self.details.configure(state='disabled')
        self.details.frame.grid(row=2, column=0, sticky='nsew')
        def wheel(event):
            if self.action_canvas.yview() != (0.0, 1.0):
                direction = -1 if getattr(event, 'num', None) == 4 or event.delta > 0 else 1
                self.action_canvas.yview_scroll(direction, 'units')
                return 'break'
        def focus_visible(event):
            widget = event.widget
            top = widget.winfo_rooty() - main.winfo_rooty()
            height = max(1, main.winfo_height())
            visible = self.action_canvas.canvasy(0)
            if top < visible or top + widget.winfo_height() > visible + self.action_canvas.winfo_height():
                self.action_canvas.yview_moveto(max(0, top - 8) / height)
        def bind_scrolling(widget):
            widget.bind('<MouseWheel>', wheel, add='+')
            widget.bind('<Button-4>', wheel, add='+')
            widget.bind('<Button-5>', wheel, add='+')
            widget.bind('<FocusIn>', focus_visible, add='+')
            for child in widget.winfo_children():
                bind_scrolling(child)
        bind_scrolling(main)
        self.update_summary()
        root.after(50, self.check_tools)
        root.after(150, self.poll)

    def check_tools(self):
        self.support = capabilities(self.runner.python)
        self.tools_checked = True
        ready = [name for name, available in self.support.items() if available]
        missing = [name for name, available in self.support.items() if not available]
        self.tools.set('Available: ' + ', '.join(ready) + '. Optional tools not found: ' + (', '.join(missing) or 'none') + '.')
        if not self.has_run:
            self.set_details(self.audit_overview())

    def audit_overview(self):
        folders = [name for name in ('Incoming', 'Incoming Files') if (self.runner.layout.root / name).is_dir()]
        scope = folders[0] + '/' if len(folders) == 1 else 'Incoming/ or Incoming Files/ (exactly one is needed)'
        lines = ['Security checks: choose Check Incoming.',
                 'Audit folder: ' + scope,
                 'Built-in checks: file signatures, empty files and SHA-256 fingerprints.']
        for name, purpose in (('ClamAV', 'malware signatures'), ('PDFiD', 'PDF active features'),
                              ('EPUBCheck', 'EPUB structure, not a malware scan')):
            state = ('AVAILABLE for applicable files' if self.support.get(name) else 'NOT INSTALLED; check unavailable') if self.tools_checked else 'checking availability…'
            lines.append(f'{name} ({purpose}): {state}')
        lines.extend(['A fingerprint identifies file bytes; it does not detect malware.',
                      'Missing or skipped tools leave incomplete checks. Results do not guarantee safety.',
                      'The audit keeps files in place. Actual checks and skipped checks appear here when it runs.',
                      'Deeper PDF Check is a separate follow-up of PDF feature warnings in the saved audit.',
                      'It inspects verified copies and saves findings; it does not rescan antivirus or certify safety.'])
        return '\n'.join(lines) + '\n'

    def set_details(self, text):
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('1.0', text)
        self.details.configure(state='disabled')

    def toggle_advanced(self):
        if self.advanced.get():
            self.advanced_frame.pack(after=self.advanced_toggle, anchor='w', pady=(0, 6))
        else:
            self.advanced_frame.pack_forget()

    def open(self, path):
        from tkinter import messagebox
        try:
            open_local(path)
        except OSError as exc:
            messagebox.showinfo('Pinakeys', str(exc), parent=self.root)

    def open_setup(self):
        platform = 'Mac' if sys.platform == 'darwin' else ('Windows' if os.name == 'nt' else 'Linux')
        self.open(self.runner.layout.tools / platform)

    def folder_settings(self):
        from tkinter import Toplevel, Text, ttk, filedialog, messagebox
        from catalogue_settings import Selection
        if self.runner.busy or self.review_window is not None or self.intake_window is not None:
            messagebox.showinfo('Please wait', 'Finish actions and close review windows before changing selection.', parent=self.root)
            return
        selection=Selection(self.runner.layout)
        top=Toplevel(self.root);top.title('Catalogue folders and exclusions')
        ttk.Label(top,text='Select existing folders inside this kit. Files stay where they are.\nOne relative folder per line; . means the kit root.').pack(padx=20,pady=12)
        folders=Text(top,height=6,width=65);folders.pack(padx=20)
        folders.insert('1.0','\n'.join(selection.data['folders']))
        def browse():
            path=filedialog.askdirectory(initialdir=self.runner.layout.root,parent=top)
            if path:
                try:
                    value=Path(path).resolve().relative_to(self.runner.layout.root).as_posix()
                    folders.insert('end','\n'+value)
                except ValueError:
                    messagebox.showerror('Keep the catalogue portable','Place the kit at a common parent of your collection, then select folders inside it. No files were moved.',parent=top)
        ttk.Button(top,text='Add folder…',command=browse).pack(pady=6)
        ttk.Label(top,text='Exclude relative folders or globs, one per line (for example: Camera or *.zip)').pack(padx=20)
        excludes=Text(top,height=5,width=65);excludes.pack(padx=20)
        excludes.insert('1.0','\n'.join(selection.data['exclude']))
        def save():
            try:
                selection.save([v.strip() for v in folders.get('1.0','end').splitlines() if v.strip()],
                               [v.strip() for v in excludes.get('1.0','end').splitlines() if v.strip()])
            except (ValueError,OSError) as exc:
                messagebox.showerror('Selection not saved',str(exc),parent=top)
                return
            self.status.set('Folders saved. Choose Refresh Catalogue for a quick update; documents stay in place.')
            top.destroy()
        ttk.Button(top,text='Save selection (no scan)',command=save).pack(pady=15)

    def restore_backup(self):
        from tkinter import filedialog, messagebox
        if self.runner.busy or self.review_window is not None or self.intake_window is not None:
            messagebox.showinfo('Close review first','Finish actions and close review windows before restoring records.',parent=self.root)
            return
        path=filedialog.askopenfilename(title='Choose a saved library_records JSON or Record History snapshot',parent=self.root,filetypes=[('JSON records','*.json')])
        if not path or not messagebox.askyesno('Restore saved records?', 'Restore this saved ledger? The current ledger is backed up first. Documents stay unchanged; saved audits remain unverified until checked.',parent=self.root):
            return
        try:
            from record_backup import restore_records
            count=restore_records(self.runner.layout,path)
            self.status.set(f'Restored {count} records. Refresh Catalogue; use Full verification to reconnect moved copies.')
        except (ValueError,OSError) as exc:
            messagebox.showerror('Records not restored',str(exc),parent=self.root)

    def open_review(self):
        from tkinter import messagebox
        from review_library import ReviewStore, ReviewWindow
        if self.intake_window is not None:
            messagebox.showinfo('Intake is open', 'Close Intake before opening the separate catalogue editor.', parent=self.root)
            self.intake_window.focus()
            return
        if self.runner.busy:
            messagebox.showinfo('Wait for the current action', 'Finish the current action before opening Review Files.', parent=self.root)
            return
        if self.review_window is not None:
            self.review_window.focus()
            return
        try:
            store = ReviewStore(self.runner.layout)
        except (OSError, ValueError) as exc:
            messagebox.showinfo('Review Files', str(exc), parent=self.root)
            return
        self.review_window = ReviewWindow(self.root, store,
            on_saved=lambda: self.start('refresh'), on_close=lambda: setattr(self, 'review_window', None),
            is_busy=lambda: self.runner.busy, reveal_file=reveal_local,
            trash_file=trash_local if sys.platform == 'darwin' and Path('/usr/bin/osascript').is_file() else None)

    def open_intake(self):
        from tkinter import messagebox
        from intake_ui import IntakeWindow
        if self.runner.busy or self.review_window is not None:
            messagebox.showinfo('Finish the current action', 'Finish the current action and close Review Files before opening Intake.', parent=self.root)
            return
        if self.intake_window is not None:
            self.intake_window.focus()
            return
        try:
            self.intake_window = IntakeWindow(self.root, self.runner.layout,
                run_audit=lambda: self.start('audit'), is_busy=lambda: self.runner.busy,
                on_close=lambda: setattr(self, 'intake_window', None),
                on_filed=lambda: self.start('refresh'), run_verify=lambda:self.start('migrate'))
        except (OSError, ValueError) as exc:
            messagebox.showerror('Intake needs attention', str(exc), parent=self.root)

    def update_summary(self):
        try:
            data = json.loads((self.runner.layout.reports / 'catalogue.json').read_text())
            if not isinstance(data, list):
                raise ValueError('Invalid catalogue')
            self.summary.set(f'{len(data):,} files · {sum(bool(row.get("review_flags")) for row in data):,} files need review')
        except (OSError, ValueError, TypeError, AttributeError):
            self.summary.set('Refresh the catalogue to see your collection here.')

    def start(self, action):
        from tkinter import messagebox
        if self.runner.busy:
            return False
        if self.intake_window is not None and (self.intake_window.job or self.intake_window.dirty):
            messagebox.showinfo('Finish intake review', 'Wait for the intake operation, or save/discard its edits before starting another action.', parent=self.intake_window.top)
            return False
        if self.review_window is not None and self.review_window.trash_job:
            messagebox.showinfo('Please wait', 'Finish the file check or Trash action before starting another action.', parent=self.review_window.top)
            return False
        if self.review_window is not None and self.review_window.dirty:
            messagebox.showinfo('Unsaved review edits', 'Save or discard the review edits before starting another action.', parent=self.review_window.top)
            self.review_window.focus()
            return False
        if action == 'verify' and not messagebox.askyesno('Full verification', 'Read all bytes in the selected folders? This can take time for large archives. Reconnect renamed copies and detect content changes; originals stay unchanged. Stop Current Action cancels verification.', parent=self.root):
            return False
        if action == 'metadata':
            notice = 'Read embedded titles and authors from the selected folders?\n\nUse this action only on documents you trust. Originals stay unchanged.'
            if not self.support.get('PDF metadata'):
                notice += '\n\nPDF support is not installed. EPUB extraction can run; PDF entries will report that metadata was not inspected.'
            if not messagebox.askyesno('Read document metadata', notice, parent=self.root):
                return False
        if action == 'pdf':
            from pdf_followup import read_selection
            try:
                count = len(read_selection(self.runner.layout)['files'])
            except (OSError, ValueError) as exc:
                messagebox.showinfo('Deeper PDF Check', str(exc), parent=self.root)
                return False
            if not count:
                messagebox.showinfo('Deeper PDF Check', 'The saved audit has no PDF feature warnings to follow up. No documents were inspected.', parent=self.root)
                return False
            notice = (f'Inspect {count} PDFs flagged in the saved audit?\n\n'
                      'File fingerprints are checked first. Only temporary copies are parsed. '
                      'Originals stay unchanged; scripts and attachments are not executed.\n\n'
                      'This produces a separate findings report, not a malware verdict. '
                      'It does not repeat the antivirus scan.')
            if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
                notice += '\n\nThis computer has no supported operating-system sandbox for this check. The inspection uses a separate process with a per-file time limit.'
            if not messagebox.askyesno('Deeper PDF Check', notice, parent=self.root):
                return False
        try:
            self.runner.start(action)
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror('Could not start', str(exc), parent=self.root)
            return False
        if self.intake_window is not None:
            self.intake_window.set_external_busy(True)
        self.action = action
        self.has_run = True
        self.audit_event = None
        self.audit_started = time.monotonic() if action in ('audit', 'pdf', 'refresh', 'metadata', 'verify', 'migrate') else None
        self.progress_text.set('')
        self.progress_note.pack_forget()
        self.progress.stop()
        self.progress.configure(mode='indeterminate', value=0)
        if action in ('audit', 'pdf', 'refresh', 'metadata', 'verify', 'migrate'):
            self.progress_note.pack(after=self.progress, fill='x', pady=(0, 4))
        for button in self.buttons:
            button.configure(state='disabled')
        self.stop.configure(state='normal')
        self.status.set({'migrate': 'Migrating saved records…', 'verify': 'Verifying selected files…', 'refresh': 'Refreshing the catalogue…', 'audit': 'Checking incoming files…',
                         'pdf': 'Inspecting the saved audit’s flagged PDFs…',
                         'metadata': 'Reading document metadata…'}[action])
        self.progress.start(80)
        if action in ('audit', 'pdf', 'refresh', 'metadata', 'verify', 'migrate'):
            self.render_audit_progress()
        self.set_details({'migrate': 'Portable records: reading the selection and incoming files; preserving legacy sources.\n\n', 'verify': 'Full verification: reading selected files. Cached attributes are not proof of unchanged contents.\n\n', 'refresh': 'Refresh Catalogue: updating the inventory. Choose Check Incoming for security checks.\n\n',
                          'metadata': 'Read Document Metadata: extracting embedded details. Choose Check Incoming for security checks.\n\n',
                          'pdf': 'Deeper PDF Check: inspecting verified copies of the saved audit’s feature-warning PDFs. Originals stay unchanged.\n\n',
                          'audit': 'Check Incoming: auditing arrivals with the available local tools.\n\n'}[action])
        if self.review_window is not None:
            self.review_window.set_busy(True)
        return True

    def apply_audit_progress(self, events):
        for event in events:
            previous = self.audit_event
            if previous and (event['stage'] < previous['stage'] or (
                    event['stage'] == previous['stage'] and (
                        event['total'] != previous['total'] or event['done'] < previous['done']
                        or previous['state'] in ('complete', 'skipped')))):
                continue
            self.audit_event = event

    def audit_time(self):
        elapsed = max(0, int(time.monotonic() - self.audit_started)) if self.audit_started is not None else 0
        minutes, seconds = divmod(elapsed, 60)
        return f'{minutes:02d}:{seconds:02d}'

    def render_audit_progress(self):
        if self.runner.cancelled:
            return
        event = self.audit_event
        elapsed = 'Elapsed ' + self.audit_time()
        if event is None:
            self.progress_text.set('Preparing file list… · ' + elapsed)
            return
        stage, state = event['stage'], event['state']
        names = {1: 'Antivirus', 2: 'File details and PDF/EPUB checks', 3: 'Saving reports'}
        if self.action in ('refresh','metadata','verify','migrate'):
            names = {1: 'Listing selected folders', 2: 'Verifying contents' if self.action in ('verify','migrate') else 'Updating catalogue', 3: 'Saving catalogue'}
        if self.action == 'pdf':
            names = {1: 'Preparing the saved audit selection', 2: 'Verifying bytes and inspecting PDF structure', 3: 'Saving PDF findings'}
        self.status.set(f'Stage {stage} of 3 — {names[stage]}' + (': loading database…' if state == 'loading' else ''))
        animated = state == 'loading' or stage == 3
        mode = 'indeterminate' if animated else 'determinate'
        if str(self.progress.cget('mode')) != mode:
            self.progress.stop()
            self.progress.configure(mode=mode, maximum=100, value=0)
            if animated:
                self.progress.start(80)
        if stage == 3:
            note = f'{event["done"]} of {event["total"]} report steps completed · {elapsed}'
        elif state == 'loading':
            note = 'Loading antivirus database… · ' + elapsed
        elif state == 'skipped':
            note = 'Antivirus not run (unavailable, disabled or no files) · ' + elapsed
            self.progress.configure(value=0)
        elif event['total']:
            percent = event['done'] * 100 // event['total']
            self.progress.configure(value=percent)
            note = f'{event["done"]} of {event["total"]} files processed · {percent}% of files · {elapsed}'
        else:
            note = 'No files to check · ' + elapsed
            self.progress.configure(value=0)
        filename = event.get('file', '')
        if filename:
            filename = Path(filename).name
            filename = ''.join(char if char.isprintable() else repr(char)[1:-1] for char in filename)
            if len(filename) > 72:
                filename = filename[:69] + '…'
            note += '\nFile: ' + filename
        self.progress_text.set(note)

    def finish_audit_progress(self, code):
        elapsed = 'Elapsed ' + self.audit_time()
        self.progress.configure(mode='determinate', maximum=100)
        event = self.audit_event
        if self.runner.cancelled or code != 0:
            self.progress.configure(value=min(float(self.progress['value']), 99))
            incomplete = 'PDF check incomplete' if self.action == 'pdf' else 'Audit incomplete'
            stopped = 'Stopped — PDF check incomplete' if self.action == 'pdf' else 'Stopped — audit incomplete'
            self.progress_text.set((stopped if self.runner.cancelled else incomplete) + ' · ' + elapsed)
        elif event and event['stage'] == 3 and event['state'] == 'complete':
            self.progress.configure(value=100)
            self.progress_text.set('Complete — review findings and missing checks in Run details · ' + elapsed)
        else:
            self.progress.configure(value=0)
            self.progress_text.set('Finished — live progress unavailable · ' + elapsed)
        self.audit_started = None

    def poll(self):
        if self.action in ('audit', 'pdf', 'refresh', 'metadata', 'verify', 'migrate'):
            self.apply_audit_progress(self.runner.read_progress())
            self.render_audit_progress()
        text = self.runner.read_output()
        if text:
            self.details.configure(state='normal')
            self.details.insert('end', text)
            if int(self.details.index('end-1c').split('.')[0]) > 1500:
                self.details.delete('1.0', '500.0')
            self.details.see('end')
            self.details.configure(state='disabled')
        if self.action and self.runner.process.poll() is not None and not (
                self.action in ('audit', 'pdf', 'refresh', 'metadata', 'verify', 'migrate') and self.runner.progress_pending):
            completed_action = self.action
            self.progress.stop()
            code = self.runner.process.returncode
            if completed_action in ('audit', 'pdf', 'refresh', 'metadata', 'verify', 'migrate'):
                self.finish_audit_progress(code)
            if self.runner.cancelled:
                self.status.set('Stopped. Run Deeper PDF Check again before relying on this run.' if completed_action == 'pdf'
                                else 'Stopped. Reports may be incomplete; refresh again before relying on them.')
            elif code == 0:
                self.status.set({'refresh': 'Catalogue refreshed. Open Library to browse the updated list.',
                                 'migrate': 'Portable records migration finished; source corrections and documents are unchanged.',
                                 'verify': 'Full verification finished. Review missing, changed or ambiguous records in Intake.',
                                 'metadata': 'Metadata extraction finished. Review Files shows entries needing attention.',
                                 'pdf': 'Deeper PDF check finished. Choose Open PDF Findings to read the report.',
                                 'audit': 'Audit finished. Read the summary at the end of Run details.'}[self.action])
            else:
                self.status.set('This action did not complete. See the run details below.')
            self.action = None
            for button in self.buttons:
                button.configure(state='normal')
            self.stop.configure(state='disabled')
            self.update_summary()
            if self.intake_window is not None:
                self.intake_window.set_external_busy(False)
                if completed_action == 'audit':
                    self.intake_window.audit_finished(code == 0 and not self.runner.cancelled)
                elif completed_action in ('refresh', 'metadata', 'verify', 'migrate') and code == 0:
                    self.intake_window.catalogue_finished()
            if self.review_window is not None:
                self.review_window.refresh_finished(code == 0 and not self.runner.cancelled,
                    catalogue_changed=completed_action in {'refresh', 'metadata', 'verify', 'migrate'})
            if self.closing:
                self.close()
                return
        self.root.after(150, self.poll)

    def cancel(self):
        self.runner.cancel()
        self.stop.configure(state='disabled')
        self.status.set('Stopping the current action…')

    def close(self):
        if self.intake_window is not None and not self.intake_window.close():
            return
        if self.review_window is not None and not self.review_window.close():
            return
        if self.runner.busy:
            from tkinter import messagebox
            if not messagebox.askyesno('Close Pinakeys?', 'Stop the running action and close? Reports may be incomplete.', parent=self.root):
                return
            self.closing = True
            self.cancel()
        else:
            self.runner.close()
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='Pinakeys 1.0.2')
    parser.add_argument('--check', action='store_true', help='Print detected capabilities without opening a window or running a scan')
    parser.add_argument('--smoke-test', action='store_true', help='Create and close the window without running any action')
    parser.add_argument('--review', action='store_true', help='Open Review Files after the window starts; do not run a scan')
    parser.add_argument('--intake', action='store_true', help='Open Intake without migrating, auditing or moving files')
    args = parser.parse_args()
    if args.check:
        print(json.dumps(capabilities(worker_python()), indent=2))
        return 0
    try:
        import tkinter as tk
    except ImportError as exc:
        print('Pinakeys needs Python with Tkinter. See the setup instructions. ' + str(exc), file=sys.stderr)
        return 1
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print('Could not open the Pinakeys window: ' + str(exc), file=sys.stderr)
        return 1
    runner = ActionRunner()
    window = PinakeysWindow(root, runner)
    if args.review:
        root.after_idle(window.open_review)
    if args.intake:
        root.after_idle(window.open_intake)
    if args.smoke_test:
        window.advanced.set(True)
        window.toggle_advanced()
        root.update_idletasks()
        print('Pinakeys window and advanced controls created; no actions run.', flush=True)
        if args.review and window.review_window is not None:
            print(f'Review editor created: {len(window.review_window.visible)} entries shown; no corrections saved.', flush=True)
        root.after(250, root.destroy)
    root.mainloop()
    if not runner.busy:
        runner.close()
    if args.smoke_test:
        print('Pinakeys desktop smoke test completed.', flush=True)
    return 0


if __name__ == '__main__':
    from library_paths import configure_console
    configure_console()
    raise SystemExit(main())
