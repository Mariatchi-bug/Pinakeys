# Support and compatibility

Pinakeys V1 catalogues existing materials without moving them. It is a small
local Python/Tk application, with an offline HTML catalogue and portable records.
There is no account, server, telemetry or automatic upload.

## Platform boundary

- macOS desktop: supported with computer-local Python 3.10+ and working Tkinter.
  The release verification report names the exact local and hosted configurations
  tested. The Mac app is unsigned and not notarized; Python/Tk are not bundled.
- Windows and Linux launchers: experimental. Passing hosted core/widget checks
  is automated coverage, not verification of every file handler or physical USB.
- uConsole: hardware unverified. Do not infer support from Linux hosted CI.

Use `python3 -m tkinter` (Windows: `py -3 -m tkinter`) before opening the app.
If that window fails, repair the Python/Tk installation first. Report the Python
and Tcl/Tk versions and OS with any GUI failure. Do not disable OS security settings.

## Stable V1 behavior

- Materials remain under the kit's common parent; selected folder paths are
  relative. External roots, symlinks and case/Unicode collisions are refused.
- Quick refresh uses paths/attributes. Full verification is an explicit complete
  byte read. Unique moved/renamed copies reconnect after verification; identical
  moved copies may require an explicit identity choice.
- Corrections are independent of filename and embedded metadata. Saving does not
  rewrite document contents. The editable fields identify their source.
- Full `library_records.json` is the authoritative lossless record format, schema
  1. Unknown fields and unmatched imports are retained. CSV/text/HTML are exports.
- Documented commands and existing export fields are compatibility surfaces.
  Additive fields may appear; readers should ignore fields they do not recognize.
  A record-schema change requires a documented migration and recovery path.
- The 1.0.x series preserves these contracts. Published version tags and downloads
  are fixed; different contents require a new version.

Keep Settings, Corrections and its history/journals, Internal, and materials with
the kit. Keep Reports to browse saved views without regenerating them. A record
backup is not a backup of your books; keep a separate copy of the materials.
Unmatched records remain available for recovery inside the library.

## Installation, upgrades and recovery

Follow QUICKSTART.md for a new kit. Never unpack an empty kit over an existing
library: its empty catalogue must not replace your saved view. The builder refuses
an existing destination. There is no automatic updater.

For an existing kit, back up materials, Settings, Corrections and Reports first.
Only replace the versioned Internal, platform launchers and Help from a reviewed
kit. Preserve Settings, Corrections, personal tools and reports. Regenerate the
catalogue explicitly. Legacy migration is an explicit action; keep its original
CSV and saved review choices until their accounting is reviewed. Ask for help when a
copy identity or source format is ambiguous.

Restore records from a full ledger/history JSON through the explicit Restore
records action. The current ledger is backed up before replacement; a different
nonempty library identity is refused. Interrupted filing must be recovered before
more filing. Uncertain/live process locks stay locked. Do not edit lock/journal
files as a shortcut or run two writers from different computers on one drive.

## Reporting problems

Use a GitHub issue with a minimal synthetic reproduction, app/OS/Python/Tk version,
expected behavior and redacted diagnostic output. Do not attach books, personal
catalogues, corrections or complete directory lists. See SECURITY.md for private
vulnerability reporting. There is no guaranteed response-time SLA.

Optional checks are described in README and DEPENDENCIES. Format errors, incomplete
checks, security indicators, metadata issues and duplicates are different findings.
No successful run or completed intake certifies a document as safe.
