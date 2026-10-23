# Pinakeys 1.0.2

[Download V1.0.2](https://github.com/Mariatchi-bug/Pinakeys/releases/tag/v1.0.2) ·
[Quick start](docs/QUICKSTART.md) · [Support](docs/SUPPORT.md) · [Security](SECURITY.md) ·
[MIT license](LICENSE)

A portable, offline catalogue of materials you already have. Select folders,
leave their files where they are, and find/open them through a local HTML list.
Correct titles and authors without rewriting documents. Intake and security
inspection are optional.

No account, server, cloud upload, automatic deletion, OCR or full-text search.
Pinakeys writes its settings, independent records and generated reports inside
its kit. Document moves, filename changes and Mac Trash require explicit actions
and confirmation. No workflow or completed check certifies a file as safe.

## Install and start

Use the versioned **empty USB kit**, not a copy of someone else's catalogue.
Download `Pinakeys-1.0.2-USB.zip` from the release page. The source ZIP is for
developers; GitHub's automatic source downloads are not the ready-made USB kit.
Keep existing collections in their current folders. Put the kit at their common
parent on the same drive; do not move books into Content just to catalogue them.
See [QUICKSTART.md](docs/QUICKSTART.md) for the complete first-use instructions.

Requires Python **3.10+**. The desktop app also needs Tkinter (`python3 -m tkinter`
checks it). Basic inventory and EPUB metadata need only the standard library;
PDF metadata uses optional computer-local PyMuPDF. Nothing is downloaded during
ordinary catalogue use. Python and external security tools are not bundled.

| Platform | V1 status |
| --- | --- |
| macOS | Verified on the release host with Python 3.11 and Tk. Python-based app bundle; unsigned and not notarized. |
| Windows | **Experimental**. Automated Windows checks; physical USB and file-handler behavior unverified. |
| Linux / uConsole | **Experimental**. Hosted Linux checks do not establish desktop-device or uConsole hardware support. |

Open `Library Tools/Mac/Pinakeys.app`, `Library Tools/Windows/Open Pinakeys.bat`,
or `Library Tools/Linux/Open Pinakeys.sh`. Opening the app does not scan.
If macOS refuses a downloaded unsigned app, use the supplied command launcher
from Terminal with `sh`; no system security setting needs to be disabled.

For a first installation:

1. Download the USB ZIP, `SHA256SUMS` and `VERIFICATION.md` from the same release.
   Compare the ZIP's SHA-256 with its entry in `SHA256SUMS`: use
   `shasum -a 256 Pinakeys-1.0.2-USB.zip` on Mac,
   `sha256sum Pinakeys-1.0.2-USB.zip` on Linux, or
   `Get-FileHash .\Pinakeys-1.0.2-USB.zip -Algorithm SHA256` in PowerShell.
2. Extract into a **new** folder. Put its contents at your collections' common
   parent on the same drive. Never extract over an existing Pinakeys library.
3. Check Python/Tk, open the platform launcher, choose your folders, then refresh.
   Start with disposable copies if you want to try optional filing and undo.

Example layout; the existing collections do not need to move:

```text
Collection root/
├── Research/                 Your existing folder
├── Manuals/                  Your existing folder
├── Open Library.html         Generated offline catalogue
├── Library Tools/            App, settings, records, reports and help
├── Content/                  Optional intake filing destination
└── Incoming Files/           Optional arrival checks
```

The same layout can live on a USB or a local disk. Folders outside that common
parent are not supported. Opening saved HTML needs a browser, not Python;
creating/updating it and using the app need Python on each computer.

## Actions at a glance

| Control | What it does |
| --- | --- |
| Open Library | Opens the last saved HTML catalogue; does not scan. |
| Folders and exclusions… | Saves the relative catalogue scope; does not move files. |
| Refresh Catalogue | Quickly inventories paths/attributes; retains earlier metadata with its provenance. |
| Review Files | Shows findings and populated title/author correction fields. Saving checks the selected file's bytes. |
| Full verification… | Reads selected materials completely to reconnect identities and detect changed contents. |
| Restore records from backup… | Restores a full JSON ledger after confirmation, backing up the current records. |
| Check Incoming | Audits the incoming folder with built-in checks and available local tools. |
| Intake · Review and File… | Reviews findings/metadata, previews names and destinations, confirms filing, and offers recovery/undo. |
| Deeper PDF Check | Explicitly inspects verified temporary copies of PDFs flagged by the saved audit. |
| Open PDF Findings | Opens the last completed follow-up report; does not inspect anything. |
| Metadata, setup and other reports | Reveals Read Document Metadata, setup and report controls. |
| Stop Current Action | Requests cancellation; stopped or failed work is not marked complete. |

There is no background watcher or automatic schedule. You choose when to refresh,
verify, extract metadata or audit. A saved catalogue is a snapshot.

## Everyday catalogue

1. Choose **Folders and exclusions…**. Select one or more existing folders inside
   the kit's common parent. Save relative paths, one per line; `.` means that
   parent. Exclusions accept relative folders and globs such as `Camera` or
   `*.zip`. Hidden files, symlinks and Pinakeys' own tools/reports are excluded.
   Nested material folders named Reports or assets are ordinary material folders.
   An explicitly selected Incoming folder can be inventoried too; root scans
   exclude incoming folders unless explicitly selected.
2. **Refresh Catalogue** is a quick path/attribute update. It does **not** read
   the whole archive for fingerprints or parse PDF/EPUB contents. Open Library,
   search, follow a file link, or use the simple HTML/text view offline.
3. **Review Files** lets you edit any catalogue entry; turn off the review-only
   filter to show all rows. Fields are populated from manual corrections,
   available embedded metadata, or clearly labelled filename suggestions.
   Filename, embedded and manual values remain distinguishable. Blank corrections
   fall back to embedded data when available; they do not rewrite it.
4. Saving a correction freshly fingerprints **that selected file**, not the
   archive. It saves a recovery snapshot and refreshes the catalogue quickly.
   A very large selected file can still take time; the save runs off the UI thread and Cancel file check stops verification before committing.
5. **Full verification…** explicitly reads all bytes in the selected folders.
   Use it after external renames/moves or to detect content changes, including
   changes with unchanged sizes/timestamps. It reconnects unique fingerprints,
   identifies exact duplicates, and asks for a copy choice when ambiguous.
   Stop Current Action cancels; incomplete/error states are explicit.
6. **Extract Metadata** is a separate, confirmed action for documents you trust.
   It opens the selected PDF/EPUB documents in parsers. It is not a malware check.

Quick attributes are a convenience, **never proof of unchanged bytes**. Reports
label quick results unverified. Same-size files are possible duplicates until
fresh fingerprints establish equality. Old extracted metadata is marked retained,
not newly inspected. Fresh checks remain mandatory for security-result reuse,
filing, undo, copy-identity choices and Trash.

New inventory-only entries receive copy IDs when first corrected or fully
verified. Known copies retain their IDs. A quick update after an external rename
can show a new unverified entry and retain the old record as missing; run Full
verification to reconnect it. Two identical missing copies cannot be distinguished
by content: use **Intake → Missing / ambiguous → Choose copy identity…**.
Records outside a changed selection remain preserved, not silently deleted.

## Portable records and recovery

Keep these with the drive when moving to another computer:

- `Library Tools/Settings/` — relative folder selection and exclusions.
- `Library Tools/Corrections/` — authoritative `library_records.json`, original
  `metadata_corrections.csv`, Record History, Filing History and operation locks.
- Your existing material folders and `Library Tools/Internal/`.
- Reports if you want the saved HTML/CSV/JSON/text views without regenerating them.

Paths are relative; USB mount names and drive letters are not identity keys.
Each physical copy gets a separate random ID. SHA-256 identifies a version's
contents, not which of two identical copies was physically moved. Unknown legacy
fields, unmatched corrections/audits and original import text remain in JSON.
Changed contents retain earlier correction/review history; old audits are
outdated for different bytes. Missing records remain available for recovery.

Existing kits: **Enable portable records…** explicitly migrates legacy data and
can take time; its progress appears in the main window and Stop Current Action cancels it. An explicit Full verification also imports retained legacy sources.
Original import sources are retained. Later CSV changes require **Import legacy
CSV changes…**; conflicts go to **Recover unmatched corrections…**. Approving a
filename does not approve a file’s security findings.
A migration without historical fingerprints cannot prove earlier contents.

To recover saved records, close review windows and choose **Restore records from
backup…**. Select a Record History JSON snapshot or a full `library_records.json`
export. Confirm restoration. The current ledger is backed up before replacement;
documents are not moved. Refresh, then Full verification if paths/contents changed.
The app refuses a backup belonging to a different nonempty library. CSV/text/HTML
are useful portable exports; full JSON is the lossless record recovery format.
See the disposable recovery demonstration in the release verification report.

After a crash, preserve Corrections and its journals. Use **Recover interrupted
filing…** before further filing. A stale lock requires confirming no other
computer is using the drive; live local workers cannot be unlocked. Interrupted
verification never becomes a complete result. `Reports/catalogue_run.json` records
running, cancelled, failed or complete catalogue status. Ordinary report-write
failures restore previous catalogue outputs; a power loss can still leave a run
incomplete. Re-run a quick update after reconnecting. Journals cannot protect
against filesystem/device damage; keep a separate recovery backup.

## Optional intake

**Incoming Files → Check Incoming → Review findings and metadata → Preview →
Confirm filing.** No folder rearrangement is required for the core catalogue.
Check Incoming is fixed to the selected intake folder, independent of catalogue
selection; it reports the actual audit scope. Its checks include signatures,
empty files and full hashes, plus available local ClamAV, PDFiD and EPUBCheck.
ClamAV loads once per batch. Missing tools, errors and skipped checks remain
incomplete; EPUB formatting errors, PDF/security indicators, metadata issues
and duplicate information have separate explanations.

After an audit, Intake reconnects saved results without rehashing the archive.
Historical results are labelled; the selected file is freshly verified before
approval or filing. Review title/author and the proposed filename and folder.
Save review does not move anything. **Preview selected…** shows the exact paths;
**Confirm filing…** requires `FILE N`. Destinations remain under Content/Books;
this optional filing convention does not constrain catalogue folder selection.
Collision checks reject existing, case-equivalent and Unicode-equivalent names.
**Undo last filing…** previews and requires `UNDO N`; it preserves later metadata
corrections. Original extensions are retained, and long names are held rather
than silently shortened. Mac metadata companions require Mac for filing.

**Deeper PDF Check** follows saved PDFiD warnings on verified temporary copies.
Existing bounded static inspection, revision handling, timeouts and clear coverage
limits remain. It does not open a viewer, execute scripts, extract attachments,
convert documents or certify safety. Unsupported parsing remains incomplete.
Optional Dangerzone conversion is a separate CLI action producing new copies;
it does not rewrite originals.

Security tools are optional and separately installed; see
[DEPENDENCIES.md](docs/DEPENDENCIES.md). Title/author editing and optional embedded metadata extraction work
independently of the security tools.

Optional tools are installed on the computer, not supplied as working binaries
inside the kit. PDF support installers create a computer-local environment with
the tested PyMuPDF 1.28.2. ClamAV needs a current signature database; PDFiD and
EPUBCheck need their own setup, and EPUBCheck needs Java. Dangerzone has separate
runtime requirements. See the upstream links and configuration in DEPENDENCIES.md.

## Outputs and privacy

The main interactive view is `Open Library.html`. `Library Tools/Reports/` holds
`library_mobile.html` (simple HTML), `catalogue.csv`, `catalogue.json`,
`catalogue.txt`, run status and any optional audit/follow-up reports. Portable
record CSV/JSON exports appear after record operations. The authoritative ledger
lives under Corrections, not Reports.

Generated catalogues contain filenames, relative paths, metadata and corrections.
They are private data even though documents are not embedded in them. Keep them
out of public repositories and issue attachments. Pinakeys does not upload them;
opening an original delegates to your browser or the computer's file handler.

## Command line

Run these commands from `Library Tools/Internal` in a built kit, or from a legacy
flat kit. For development, build a disposable kit first as described below:

```sh
python3 scan_library.py --inventory-only --folder 'Research' --folder 'Manuals' --exclude 'Camera' --exclude '*.zip'
python3 scan_library.py --inventory-only          # reuse the saved selection
python3 scan_library.py --inventory-only --verify # explicit full-byte verification
python3 scan_library.py                          # explicit embedded metadata extraction
python3 pinakeys.py --check                      # capabilities, no scan
python3 pinakeys.py --version
python3 audit_materials.py --folder 'Incoming Files'
python3 pdf_followup.py --only 'Incoming Files/example.pdf'
```

On Windows use `py -3` instead of `python3`. The audit example explicitly starts
inspection; the follow-up requires a matching saved audit. No optional security
tool is silently installed. Use each program's `--help` for its actual options.

A legacy flat kit can continue path-based CSV edits until migration. For portable
IDs on a clean installation, start with the quick inventory action as above.
The kit must share a common parent with its folders; absolute external paths and
symbolic links are refused to preserve portability.

## Repository layout

The source repository and portable USB kit have different layouts:

```text
Pinakeys/
├── app/          Application code, icons and bundled parser notices
├── launchers/    Platform scripts copied into the USB kit
├── tools/        Build, test and release utilities
├── docs/         Installation, dependencies and maintenance guides
├── tests/        Disposable synthetic tests
└── .github/      Hosted checks, issue templates and repository rules
```

README, license, security and contribution guidance stay at the top level.
The root release-guide pointer also preserves existing documentation links.
Build a new kit for application testing; the source checkout is a development
workspace. The builder maps these source folders to the portable kit's existing
`Library Tools/Internal`, platform and Help folders. Repository organization does
not change a user's library layout.

## Build, verify and publish

For development, clone the source into its own directory. Do not develop inside
your only copy of a personal catalogue:

```sh
git clone https://github.com/Mariatchi-bug/Pinakeys.git
cd Pinakeys
```

```sh
python3 -B tools/run_tests.py
python3 tools/build_usb.py /path/to/NEW-empty-kit
python3 tools/release_check.py /path/to/NEW-empty-kit
python3 -B tools/release_gate.py --output /path/to/NEW-release-folder
```

The builder refuses existing destinations and never deploys into a personal USB.
The release gate requires a clean committed checkout and checks actual package
contents, source history, versions, rebuilds and disposable installation/recovery.
Published assets include the empty kit, source ZIP, file manifest, SHA-256 sums,
release notes and a verification report with exact coverage and skips. Hosted
checks cover Python 3.10/3.13 on Mac, Windows and Linux, plus optional PDF support;
headless Linux GUI and some Windows integration checks remain unverified.
See [CONTRIBUTING.md](CONTRIBUTING.md),
[RELEASE_NOTES.md](docs/RELEASE_NOTES.md) and [FUTURE_WORK.md](docs/FUTURE_WORK.md).
MIT covers Pinakeys' own code; bundled dependency notices remain intact.
See [SUPPORT.md](docs/SUPPORT.md) for the compatibility contract and upgrades,
[SECURITY.md](SECURITY.md) for private reporting, and [RELEASING.md](docs/RELEASING.md)
for the clean-commit release gate. Final downloads are built from one accepted
commit; a hosted pass is separate from hardware verification.

## Troubleshooting and known limits

| Symptom | Next step |
| --- | --- |
| App will not start | Check Python 3.10+ and `python3 -m tkinter`; read the platform command launcher's error. Python/Tk are not bundled. |
| Empty or stale library | Check Folders and exclusions, run Refresh Catalogue and reopen the saved HTML. Opening HTML does not refresh it. |
| File link fails after a move | Keep the kit/materials together; refresh paths, then use Full verification to reconnect unique identities. Restricted preview apps may block local links. |
| Author/title missing | Inventory does not parse documents. Edit the populated correction fields or explicitly extract metadata from trusted documents. |
| Duplicate identity is ambiguous | Choose the intended copy in Intake; identical bytes alone cannot distinguish copies. Do not delete records to force a match. |
| Audit says incomplete | Read the tool coverage and Run details. Install/configure missing tools or investigate errors; incomplete is not a pass. |
| Lock remains after a crash | Preserve journals and use Recover interrupted filing when no other computer is using the drive. Never remove an uncertain/live lock as a shortcut. |

Keep one writer per library. The exFAT reservation fallback is not an OS-atomic
no-replace operation; journals do not protect against hardware/filesystem damage.
Back up materials as well as records. A record backup cannot restore a lost book.
The app is not a document viewer, malware guarantee, automatic organiser or backup
system. OCR, full-text search, collections and cloud features are outside V1;
see [FUTURE_WORK.md](docs/FUTURE_WORK.md).

For help, [open an issue](https://github.com/Mariatchi-bug/Pinakeys/issues/new/choose)
with version, OS/Python/Tk details and a minimal synthetic reproduction. Redact
personal paths and logs. Report security vulnerabilities through the private route
in SECURITY.md, rather than attaching sensitive details to a public issue.
