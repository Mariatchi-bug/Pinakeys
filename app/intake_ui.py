"""Tk intake review. Workers never access Tk; decisions stay in one local flow."""
from pathlib import Path
from queue import Queue, Empty
from threading import Thread

from intake_workflow import Intake, proposed_name
from library_records import RecordError
from library_style import PALETTE


class IntakeWindow:
    def __init__(self, parent, layout, *, run_audit, on_close, is_busy, on_filed, run_verify=None):
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText
        self.flow = Intake(layout)
        self.run_audit, self.on_close = run_audit, on_close
        self.run_verify=run_verify
        self.is_busy, self.on_filed = is_busy, on_filed
        self.job = False
        self.selected = None
        self.baseline = None
        self.top = tk.Toplevel(parent)
        self.top.title('Intake · Pinakeys')
        self.top.geometry('1160x850')
        self.top.minsize(980, 720)
        self.top.configure(background=PALETTE['paper'])
        self.top.protocol('WM_DELETE_WINDOW', self.close)
        main = ttk.Frame(self.top, padding=18)
        main.pack(fill='both', expand=True)
        ttk.Label(main, text='Incoming → Check → Review → Preview → File', style='Section.TLabel').pack(anchor='w')
        ttk.Label(main, text='Filing is your decision. Completed checks never guarantee safety. Originals are moved only after confirmation.',
                  wraplength=1080, style='Muted.TLabel').pack(anchor='w', pady=(5, 12))
        actions = ttk.Frame(main)
        actions.pack(fill='x')
        self.controls = []
        self.inputs = []
        def button(label, callback, parent=actions):
            b = ttk.Button(parent, text=label, command=callback, style='Quiet.TButton')
            b.pack(side='left', padx=(0, 7), pady=3)
            self.controls.append(b)
            return b
        self.enable_button = button('Enable portable records…', self.enable)
        self.refresh_button = button('Refresh intake', self.reload)
        self.audit_button = button('Check Incoming', self.audit)
        self.preview_button = button('Preview selected…', self.preview)
        self.undo_button = button('Undo last filing…', self.undo)
        self.recover_button = button('Recover interrupted filing…', self.recover)
        recovery = ttk.Frame(main)
        recovery.pack(fill='x')
        self.import_button = button('Import legacy CSV changes…', self.import_legacy, recovery)
        self.orphan_button = button('Recover unmatched corrections…', self.orphans, recovery)
        self.scope = tk.StringVar(value='Incoming')
        ttk.Label(recovery, text='Show:').pack(side='left', padx=(8, 4))
        self.scope_box = ttk.Combobox(recovery, textvariable=self.scope, values=['Incoming', 'Library', 'Missing / ambiguous', 'All'], state='readonly', width=20)
        self.scope_box.pack(side='left')
        self.scope_box.bind('<<ComboboxSelected>>', lambda e: self.render())
        self.search = tk.StringVar()
        search = ttk.Entry(recovery, textvariable=self.search, width=25)
        search.pack(side='left', padx=8)
        self.inputs += [search, self.scope_box]
        self.search.trace_add('write', lambda *args: self.render())
        panels = ttk.Panedwindow(main, orient='horizontal')
        panels.pack(fill='both', expand=True, pady=10)
        left, right = ttk.Frame(panels), ttk.Frame(panels, padding=(12, 0, 0, 0))
        panels.add(left, weight=2)
        panels.add(right, weight=3)
        self.tree = ttk.Treeview(left, columns=('next',), show='tree headings', selectmode='extended')
        self.tree.heading('#0', text='File / separate copy')
        self.tree.heading('next', text='Next step')
        self.tree.column('#0', width=245, minwidth=150)
        self.tree.column('next', width=170, minwidth=130)
        scrollbar = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        self.details = ScrolledText(right, height=12, wrap='word', font=('TkDefaultFont', 11),
                                    background=PALETTE['surface'], foreground=PALETTE['text'])
        self.details.pack(fill='both', expand=True)
        self.title, self.author, self.name, self.destination = (tk.StringVar() for _ in range(4))
        self.ack = tk.BooleanVar(value=False)
        fields = ttk.Frame(right)
        fields.pack(fill='x', pady=8)
        fields.columnconfigure(1, weight=1)
        for index, (label, value) in enumerate([('Title', self.title), ('Author', self.author),
                                               ('Filename', self.name), ('Destination folder', self.destination)]):
            ttk.Label(fields, text=label).grid(row=index, column=0, sticky='w', padx=(0, 10), pady=3)
            entry = ttk.Entry(fields, textvariable=value)
            entry.grid(row=index, column=1, sticky='ew', pady=3)
            self.inputs.append(entry)
        tools = ttk.Frame(right)
        tools.pack(fill='x')
        self.read_button = button('Read Inside File…', self.read_inside, tools)
        if not (layout.tools/'Personal Tools/Filename Cleanup/content_recovery.py').exists():
            self.read_button.pack_forget()
        self.name_button = button('Suggest filename', self.suggest_name, tools)
        self.identity_button = button('Choose copy identity…', self.identity, tools)
        acknowledgement = ttk.Checkbutton(right, variable=self.ack,
            text='I reviewed the findings, missing checks, metadata and copies.\nThis is my filing choice, not a safety certificate.')
        acknowledgement.pack(anchor='w', pady=6)
        self.inputs.append(acknowledgement)
        bottom = ttk.Frame(right)
        bottom.pack(fill='x')
        self.save_button = button('Save review (no rename)', self.save, bottom)
        self.discard_button = button('Discard edits', self.load_selected, bottom)
        self.status = tk.StringVar(value='Enable portable records to connect existing reports and corrections. This does not move materials.')
        ttk.Label(main, textvariable=self.status, wraplength=1080, style='Muted.TLabel').pack(fill='x')
        self.progress = ttk.Progressbar(main, mode='indeterminate')
        self.progress.pack(fill='x', pady=(6, 0))
        self.render()

    @property
    def dirty(self):
        return self.baseline is not None and self.values() != self.baseline

    def values(self):
        return (self.title.get(), self.author.get(), self.name.get(), self.destination.get(), self.ack.get())

    def guard(self):
        from tkinter import messagebox
        if self.job or self.is_busy():
            messagebox.showinfo('Please wait', 'Wait for the current action to finish.', parent=self.top)
            return False
        if self.dirty:
            messagebox.showinfo('Unsaved intake review', 'Save or discard your current review edits first.', parent=self.top)
            return False
        return True

    def run(self, description, operation, after=None, *, preserve_edits=False):
        from tkinter import messagebox
        if self.job or self.is_busy():
            return
        self.job = True
        self.status.set(description)
        self.progress.start(70)
        for b in self.controls + self.inputs:
            b.configure(state='disabled')
        queue = Queue()
        def work():
            try:
                queue.put((True, operation()))
            except Exception as exc:
                queue.put((False, exc))
        Thread(target=work, daemon=True).start()
        def poll():
            try:
                ok, result = queue.get_nowait()
            except Empty:
                self.top.after(60, poll)
                return
            self.job = False
            self.progress.stop()
            for b in self.controls + self.inputs:
                b.configure(state='normal')
            self.scope_box.configure(state='readonly')
            if not ok:
                self.status.set(str(result))
                messagebox.showerror('Intake needs attention', str(result), parent=self.top)
            else:
                if not preserve_edits:
                    self.baseline = None
                    self.selected = None
                    self.render()
                    self.select()
                self.status.set('Records saved. No file was renamed by saving a review.' if after is None else 'Action finished.')
                if after:
                    after(result)
        self.top.after(60, poll)

    def enable(self):
        from tkinter import messagebox
        if not self.guard():
            return
        if messagebox.askyesno('Enable portable records',
            'Read full fingerprints of the selected folders and Incoming files and migrate saved corrections into a portable record ledger?\n\n'
            'Original documents and the legacy CSV stay unchanged. Missing rows and unknown columns are retained. '
            'This can take time on a USB. No audit or metadata parser runs.', parent=self.top):
            if self.run_verify:
                self.run_verify()
                self.status.set('Migration is running in Pinakeys. Stop Current Action can cancel it.')
            else:
                self.run('Identifying files and preserving legacy corrections…', lambda: self.flow.reload(initialize=True))

    def reload(self):
        if self.guard():
            self.run('Updating paths; current contents are not reverified…', lambda:self.flow.reload(quick=True))

    def audit(self):
        if self.guard():
            self.run_audit()
            self.status.set('Check Incoming is running in Pinakeys. Results reconnect here when it finishes.')

    def set_external_busy(self, busy):
        for control in self.controls + self.inputs:
            control.configure(state='disabled' if busy else 'normal')
        if not busy:
            self.scope_box.configure(state='readonly')
            self.render()

    def catalogue_finished(self):
        self.flow = Intake(self.flow.layout)
        self.baseline = None
        self.selected = None
        self.render()
        self.select()

    def audit_finished(self, success):
        if success:
            self.reload()
        else:
            self.status.set('The audit did not finish. Its run is incomplete; review Run details.')

    def render(self):
        if self.job:
            return
        selection = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        needle = self.search.get().casefold()
        for r in self.flow.records.data['records']:
            incoming = r['path'].split('/')[0] in ('Incoming Files', 'Incoming')
            mode = self.scope.get()
            if ((mode == 'Incoming' and (not incoming or r['state'] == 'missing'))
                    or (mode == 'Library' and (incoming or r['state'] == 'missing'))
                    or (mode == 'Missing / ambiguous' and r['state'] == 'present')):
                continue
            if needle and needle not in (r['path'] + str(r.get('corrections', {}))).casefold():
                continue
            self.tree.insert('', 'end', iid=r['id'], text=Path(r['path']).name + ' · ' + r['id'][:8],
                             values=(self.flow.next_step(r),))
        keep = [i for i in selection if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)
        elif self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])
        self.enable_button.configure(state='disabled' if self.flow.records.exists else 'normal')
        self.import_button.configure(state='normal' if self.flow.records.exists and self.flow.legacy_changed() else 'disabled')
        self.orphan_button.configure(text=f'Recover unmatched corrections ({len(self.flow.records.data["unmatched_corrections"])})…')
        if self.flow.records.exists:
            self.status.set(f'{len(self.flow.records.data["records"])} portable copy records · '
                            f'{len(self.flow.records.data["unmatched_corrections"])} unmatched corrections retained · '
                            f'{sum(not x.get("resolved_record_id") for x in self.flow.records.data["unmatched_audits"])} audit rows not matched')

    def select(self, event=None):
        if self.job:
            return
        selected = self.tree.selection()
        identity = selected[0] if selected else None
        if identity == self.selected:
            return
        if self.dirty:
            if self.tree.exists(self.selected):
                self.tree.selection_set(self.selected)
            self.guard()
            return
        self.selected = identity
        self.load_selected()

    def load_selected(self):
        if not self.selected:
            return
        r = self.flow.records.get(self.selected)
        values, notes = self.flow.suggestions(r)
        review = r.get('review', {})
        library_folder = 'Books' if not (self.flow.layout.root / 'Content').exists() and (self.flow.layout.root / 'Books').exists() else 'Content'
        current_folder = Path(r['path']).parent.as_posix() if r['path'].split('/')[0] in ('Content', 'Books') else library_folder
        target = review.get('destination') or current_folder + '/' + Path(r['path']).name
        self.title.set(values['title'])
        self.author.set(values['author'])
        self.name.set(Path(target).name)
        self.destination.set(Path(target).parent.as_posix())
        self.ack.set(review.get('sha256') == r['sha256'] and review.get('audit_key') == self.flow.records.audit_key(r))
        self.baseline = self.values()
        lines = [r['path'], 'Copy ID: ' + r['id'], 'Next: ' + self.flow.next_step(r), '']
        for category, reasons in self.flow.categories(r).items():
            lines += [category + ':', *(['• ' + reason for reason in reasons] or ['No issue recorded in this category.']), '']
        audit = self.flow.records.current_audit(r)
        if audit:
            lines += ['Saved check outputs (' + str(audit.get('scanned_at')) + '):']
            lines += [name + ': ' + str(audit['result'].get(name, 'not recorded')) for name in ('file_type_check', 'clamav', 'pdfid', 'epubcheck')]
        lines += ['', 'Metadata suggestions:', *notes, '', 'Unknown wording stays editable. No embedded metadata is changed.']
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('1.0', '\n'.join(lines))
        self.details.configure(state='disabled')

    def suggest_name(self):
        from tkinter import messagebox
        if self.selected and not self.job:
            try:
                self.name.set(proposed_name(self.flow.records.get(self.selected)['path'], self.title.get(), self.author.get()))
            except ValueError as exc:
                messagebox.showinfo('Filename needs attention', str(exc), parent=self.top)

    def save(self):
        if not self.selected:
            return
        identity = self.selected
        values = self.values()
        self.run('Saving this review; filenames stay unchanged…',
                 lambda: self.flow.review(identity, *values))

    def preview(self):
        from tkinter import messagebox, simpledialog
        if not self.guard():
            return
        identities = list(self.tree.selection())
        def show(plan):
            import tkinter as tk
            from tkinter import ttk
            from tkinter.scrolledtext import ScrolledText
            top = tk.Toplevel(self.top)
            top.title('Preview filing · Pinakeys')
            top.geometry('900x620')
            top.transient(self.top)
            top.grab_set()
            text = ScrolledText(top, wrap='word')
            text.pack(fill='both', expand=True, padx=14, pady=14)
            lines = [plan['notice'], 'Keep other file tools idle until filing/undo finishes.', '']
            for move in plan['moves']:
                r = self.flow.records.get(move['record_id'])
                lines += [move['source'], '→ ' + move['destination'], 'Copy ' + r['id'], 'Findings acknowledged:']
                for group, issues in self.flow.categories(r).items():
                    if issues:
                        lines += [group + ': ' + ' | '.join(issues)]
                lines += ['']
            text.insert('1.0', '\n'.join(lines))
            text.configure(state='disabled')
            def apply():
                count = len(plan['moves'])
                confirm = simpledialog.askstring('Confirm filing', f'Move/rename these {count} files exactly as previewed?\nType FILE {count}.', parent=top)
                if confirm is None:
                    return
                if confirm != f'FILE {count}':
                    messagebox.showinfo('Nothing moved', 'The confirmation did not match.', parent=top)
                    return
                top.destroy()
                self.run('Filing confirmed files and saving recovery records…', lambda: self.flow.apply(plan, confirm),
                         lambda result: self.filed())
            ttk.Button(top, text=f'Confirm filing {len(plan["moves"])} files…', command=apply).pack(side='right', padx=14, pady=10)
            ttk.Button(top, text='Close preview', command=top.destroy).pack(side='left', padx=14, pady=10)
        self.run('Verifying sources and destinations for this preview…', lambda: self.flow.preview(identities), show)

    def filed(self):
        self.status.set('Filing finished. Corrections and review records followed their copy IDs. Refreshing the library catalogue…')
        self.on_filed()

    def undo(self):
        self._reverse(False)

    def recover(self):
        self._reverse(True)

    def _reverse(self, interrupted):
        from tkinter import messagebox, simpledialog
        if not self.guard():
            return
        if interrupted and self.flow.records.lock_path.exists():
            if not messagebox.askyesno('Recover records lock', 'Confirm that no other Pinakeys process or computer is using this USB before reclaiming an interrupted lock.', parent=self.top):
                return
            try:
                self.flow.records.release_interrupted_lock()
            except (ValueError, OSError) as exc:
                messagebox.showerror('Recovery stopped', str(exc), parent=self.top)
                return
        journals = self.flow.pending() if interrupted else [(p, j) for p, j in self.flow.journals() if j['status'] == 'committed'][-1:]
        if not journals:
            messagebox.showinfo('No filing to recover', 'No applicable filing operation was found. Refresh intake to continue.', parent=self.top)
            return
        path, journal = journals[-1]
        count = len(journal['plan']['moves'])
        paths = '\n'.join(m['destination'] + '\n→ ' + m['source'] for m in journal['plan']['moves'])
        confirm = simpledialog.askstring('Preview reversal', paths + f'\n\nRestore these paths? Type UNDO {count}. Changed or occupied files stop recovery.', parent=self.top)
        if confirm is not None:
            self.run('Verifying and restoring original paths…', lambda: self.flow.rollback(path, confirmation=confirm), lambda _: self.filed())

    def import_legacy(self):
        from tkinter import messagebox
        if self.guard() and messagebox.askyesno('Import legacy CSV', 'Read changed legacy corrections? Existing record corrections will be kept; conflicting and unmatched rows go to recovery. The CSV remains unchanged.', parent=self.top):
            self.run('Importing legacy corrections without replacing reviewed values…', lambda: self.flow.reload(import_legacy=True))

    def choose(self, title, description, options, callback):
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText
        top = tk.Toplevel(self.top)
        top.title(title)
        top.geometry('780x560')
        top.transient(self.top)
        top.grab_set()
        ttk.Label(top, text=description, wraplength=740).pack(anchor='w', padx=14, pady=10)
        choice = tk.StringVar(value=options[0][0])
        area = ScrolledText(top, wrap='word')
        area.pack(fill='both', expand=True, padx=14)
        for value, label in options:
            radio = ttk.Radiobutton(area, text=label, value=value, variable=choice)
            area.window_create('end', window=radio)
            area.insert('end', '\n\n')
        area.configure(state='disabled')
        def use():
            value = choice.get()
            top.destroy()
            callback(value)
        ttk.Button(top, text='Use selected choice', command=use).pack(side='right', padx=14, pady=12)
        ttk.Button(top, text='Cancel', command=top.destroy).pack(side='left', padx=14, pady=12)

    def identity(self):
        from tkinter import messagebox
        if not self.guard() or not self.selected:
            return
        choices = next((x for x in self.flow.records.data['ambiguities'] if x['id'] == self.selected), None)
        if not choices:
            messagebox.showinfo('Copy identity', 'This copy has no ambiguous identity. Its copy ID stays distinct from identical files.', parent=self.top)
            return
        options = [('NEW', 'Treat as a new copy; keep old corrections recoverable')]
        for identity in choices['candidates']:
            r = self.flow.records.get(identity)
            options.append((identity, r['path'] + '\n' + str(r.get('corrections', {})) + ' · ' + identity[:8]))
        identity = self.selected
        self.choose('Choose the intended missing copy',
                    'The contents are identical, so Pinakeys cannot identify the physical copy. Choose which record follows this file.',
                    options, lambda choice: self.run('Saving explicit copy identity…',
                        lambda: self.flow.resolve(identity, None if choice == 'NEW' else choice)))

    def orphans(self):
        from tkinter import messagebox
        if not self.guard() or not self.selected:
            return
        rows = self.flow.records.data['unmatched_corrections']
        if not rows:
            messagebox.showinfo('Correction recovery', 'No unmatched correction rows. Missing records remain visible under Missing / ambiguous.', parent=self.top)
            return
        selected = self.selected
        def save(identity):
            def operation():
                with self.flow.records.lock():
                    self.flow.records.recover_correction(identity, selected)
                    self.flow.records.save()
                    self.flow.records.export()
            self.run('Recovering the selected correction…', operation)
        self.choose('Recover a correction',
                    'Attach the selected retained row to ' + self.flow.records.get(selected)['path'] +
                    '. Earlier record values remain in history. Private filename choices stay separate from catalogue corrections.',
                    [(x['id'], str(x['row']) + '\n' + x['reason']) for x in rows], save)

    def read_inside(self):
        if not self.selected or self.job:
            return
        def show(result):
            import tkinter as tk
            from tkinter import ttk
            from tkinter.scrolledtext import ScrolledText
            top = tk.Toplevel(self.top)
            top.title('Local title and author evidence')
            top.geometry('850x650')
            top.transient(self.top)
            top.grab_set()
            for field, variable in [('title', self.title), ('author', self.author)]:
                frame = ttk.Frame(top, padding=8)
                frame.pack(fill='x')
                ttk.Label(frame, text=field.capitalize() + ' suggestions (choose explicitly)').pack(anchor='w')
                box = tk.Listbox(frame, height=4, exportselection=False)
                items = result.get(field, [])
                for item in items:
                    box.insert('end', item['value'] + ' · ' + item['source'])
                box.pack(fill='x')
                def use(box=box, items=items, variable=variable):
                    if box.curselection():
                        variable.set(items[box.curselection()[0]]['value'])
                ttk.Button(frame, text='Use selected ' + field, command=use).pack(anchor='w')
            excerpts = ScrolledText(top, wrap='word', height=12)
            excerpts.pack(fill='both', expand=True, padx=8)
            excerpts.insert('1.0', '\n\n'.join(x['source'] + '\n' + x['text'] for x in result.get('excerpts', [])))
            excerpts.configure(state='disabled')
            ttk.Button(top, text='Done — suggestions are not saved yet', command=top.destroy).pack(pady=8)
        self.run('Reading bounded local title/author evidence; no filename changes…', lambda: self.flow.read_inside(self.selected), show, preserve_edits=True)

    def focus(self):
        self.top.lift()
        self.top.focus_force()

    def close(self):
        from tkinter import messagebox
        if self.job:
            messagebox.showinfo('Please wait', 'Let the file operation or reader finish before closing.', parent=self.top)
            return False
        if self.dirty and not messagebox.askyesno('Discard intake edits?', 'Close without saving the current review edits?', parent=self.top):
            return False
        self.top.destroy()
        self.on_close()
        return True
