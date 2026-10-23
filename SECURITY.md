# Security policy

## Supported versions and reporting

Security fixes target the latest 1.0.x release.

Report vulnerabilities privately using **Security → Report a vulnerability**:
https://github.com/Mariatchi-bug/Pinakeys/security/advisories/new
If that route is unavailable, open an issue requesting a private reporting route
without including sensitive details. Do not upload books, private catalogues,
corrections, credentials or unredacted file lists. Prefer a minimal synthetic
reproduction, affected version/platform and expected versus observed behavior.
There is no guaranteed response-time SLA.

## System boundary

Pinakeys is a local Python/Tk application and offline HTML catalogue. It has no
server, account service or automatic cloud upload. The core inventories selected
existing folders and saves independent records. Optional intake invokes separately
installed local tools; explicit filing/undo and Mac Trash can change file paths.
The packaged dependency and subprocess inspection workers are in scope.

## Inputs and trust boundaries

Treat document bytes, filenames, embedded metadata, imported CSV/JSON, saved
reports and externally edited journals as untrusted input. The person at the
computer authorizes operations. Existing files, corrections, identities and
recovery history are assets that must not be silently lost or attached to the
wrong copy. Optional external tools/runtimes are separate trust dependencies.

## Required properties

- Ordinary cataloguing and editing corrections leave document contents unchanged.
- File operations require explicit preview/confirmation, fresh matching content
  proofs, collision checks, journals and recovery/undo protections.
- Paths remain inside the selected kit boundary; traversal and symlink substitution
  must not authorize an operation outside it.
- Identical copies remain distinguishable. Ambiguous identities require a choice.
  Missing, unknown and unmatched records remain recoverable.
- Cached attributes are never proof of unchanged bytes. Changed contents invalidate
  reuse of earlier audit results. Skipped/failed/unavailable checks stay incomplete.
- Untrusted values are escaped in generated HTML and passed as data to subprocesses.
  Documents, scripts, embedded attachments and filename text are not executed by
  indexing or static inspection.
- Live or uncertain record locks cannot be cleared as stale. Checking process
  liveness must not signal or terminate another process.
- Cancellation/failure must not be reported as complete or silently discard saved
  records. Damaged recovery input must be retained/refused rather than guessed.

## Reportable findings and limitations

Report reachable violations of these properties, including unintended data
changes, identity/correction mix-ups, path escapes, unintended execution, unsafe
recovery, sensitive diagnostic disclosure and misleading security-result reuse.
Assess severity from actual local reachability and impact. No finding class is
excluded merely because the application is offline or a platform experimental.

This is not an antivirus engine or a safety certification. External signatures
and format checks have limited coverage. A document being malicious, an EPUB
being invalid, or an optional tool being absent is not by itself a vulnerability
in Pinakeys; unsafe handling or misleading presentation can be.

The Mac app is unsigned and requires computer-local Python/Tk. Windows/Linux
launchers are experimental; uConsole hardware is unverified. Static inspection
uses a separate process and time limits; operating-system restrictions vary by
platform. These limits do not excuse bypassing the required properties above.
Journals do not protect against filesystem/device damage. Keep independent backups.

Dependencies retain their own notices; see DEPENDENCIES.md and vendor/README.md.
Keep personal materials and unrelated local scripts out of source, tests, release
assets, screenshots and public issue reports.
