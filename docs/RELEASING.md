# Release process

Pinakeys V1 provides an offline catalogue, portable corrections and recovery,
and optional intake review. Test releases on disposable materials.

1. Review focused changes through a pull request. Run targeted tests during
   development and the complete `python3 -B tools/run_tests.py` suite once stable.
   Native Tk cases run in separate processes. Record skips and platform limits.
2. Run the optional PDF suite with the tested PyMuPDF version in DEPENDENCIES.md.
   Keep third-party notices and pinned dependencies intact.
3. Inspect tracked source, contributor identities and binary metadata. Exclude
   personal materials, catalogues, corrections, reports and private scripts.
   Preserve contributor attribution and dependency licenses.
4. Commit the release source. From a clean checkout run
   `python3 -B tools/release_gate.py --output /path/to/NEW-release-folder`.
   It inspects source and archive contents, checks versions, rebuilds the empty
   kit and exercises disposable installation, record recovery, intake and undo.
5. Require all hosted checks to pass for that exact commit. Record unavailable
   hardware and skipped checks explicitly; hosted tests do not establish physical
   USB or desktop-device compatibility.
6. Verify offline search, opening a synthetic material and clearing search in a
   desktop browser. Repeat this manual check when HTML rendering or link behavior
   changes. Document the browser and any local-file restrictions.
7. Review version, commit, release notes, support claims, dependency notices,
   manifests and checksums. Include a verification report with test counts,
   methods and limitations. A completed check does not certify document safety.
8. Verify the repository's main/tag rulesets, required checks, read-only workflow
   permissions, private vulnerability reporting and immutable-release settings.
   Ruleset configuration is versioned in .github/rulesets; hosting settings must
   also be checked on the repository.
9. Create a draft release, attach every asset, then publish the fixed version tag.
   Verify anonymous cloning, downloads, checksums and documentation links.
   Published tags/assets are not replaced; compatible fixes receive a new 1.0.x
   version. Keep a separate backup of source and release assets.

CI uses read-only tokens, reviewed action pins, bounded jobs, stale-run
cancellation and dependency-update pull requests. It does not need personal USBs,
material backups or long-lived publication credentials. The main-branch policy
requires the PR route and current checks without requiring an unavailable second
reviewer for a sole maintainer.
