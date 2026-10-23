# Development and local release

Preserve the existing Python/Tk/offline HTML stack. Use disposable synthetic
collections; never use personal books or a real USB as a test fixture. Ordinary
cataloguing leaves materials in place. Confirmed intake moves/undo are tested
only on disposable copies. Preserve independent corrections and unknown fields.

Run `python3 -B run_tests.py`. No third-party packages are
required for core tests. The runner isolates every native Tk case in a fresh process to match the app's single-interpreter lifetime. Native Tk tests skip only if a display/Tk is unavailable;
report skips honestly. Fake security tools verify contracts, not detection rates.
Run targeted tests while changing behavior; once stable, run the whole suite and
clean-build gate. Repeat broad checks only after subsequent relevant changes.

The GitHub test matrix uses Python 3.10/3.13 on macOS, Windows and Linux.
A local macOS pass does not establish Windows/Linux/uConsole device support or
remote CI success. Keep those platforms experimental until verified natively.
Do not widen those claims merely because portable path tests pass.

A local release gate is:

```sh
python3 -B run_tests.py
python3 build_usb.py /path/to/NEW-empty-kit
python3 release_check.py /path/to/NEW-empty-kit
```

Check installation from QUICKSTART using a fresh disposable copy of the kit.
Verify both journeys and recovery; use an 8 GiB sparse synthetic archive to
check quick actions without reading its payload. Explicit full verification can
be measured on a smaller known fixture; record bytes and conditions, not a USB
speed estimate. Browser search/open behavior needs actual offline-page validation,
not just HTML string checks. Document optional real-tool checks separately.

Before committing, inspect staged names and contents. Only the reviewed public
source allowlist belongs in Git. `.gitignore` is not a privacy audit: inspect
actual source archives and USB ZIP entries too. Refuse books, personal catalogue/
correction/audit files, private scripts, screenshots, caches and personal paths.
Preserve MIT, vendor notices and version consistency. Keep private verification
evidence outside the source commit. Never include personal library data in releases.

Submit focused changes through a pull request with relevant tests. Keep release
notes, version identifiers and installation instructions consistent. Maintainers
publish versioned releases after the required checks pass. Published tags and
assets are fixed; compatible corrections receive a new 1.0.x version.
Future features belong in FUTURE_WORK.md and should be scoped separately.

Use release_gate.py from a clean committed checkout for the full package and
installation gate. See RELEASING.md, SUPPORT.md and SECURITY.md. Issue/PR forms
require synthetic reproductions and redacted diagnostics. Public history must
preserve contributor attribution; never rewrite it without consent.
