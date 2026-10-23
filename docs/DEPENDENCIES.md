# Dependencies and notices

Pinakeys' own source is MIT licensed; see [LICENSE](../LICENSE). Python, Tcl/Tk and optional
external tools retain their own licenses. The empty kit contains no Python
runtime, AV database, Java runtime, optional Python package or conversion VM.

The one bundled code dependency is `vendor/pdf_parser.py` (Didier Stevens'
pdf-parser.py 0.7.14), with its public-domain notice and included LZW decoder's
permissive copyright/license retained verbatim. See the [bundled parser notice](../app/vendor/README.md) for the
pinned source commit, checksum and upstream links. The runtime checks its hash.

| Optional tool | Used for | Upstream information |
| --- | --- | --- |
| PyMuPDF | Embedded PDF metadata; existing PDF revision support | https://pymupdf.readthedocs.io/ |
| ClamAV | Batch malware-signature checks | https://docs.clamav.net/ |
| PDFiD | Static PDF keyword indicators | https://blog.didierstevens.com/programs/pdf-tools/ |
| EPUBCheck + Java | EPUB conformance checks, not malware detection | https://www.w3.org/publishing/epubcheck/ |
| Dangerzone | Separate conversion to new copies | https://dangerzone.rocks/ |

Install optional tools separately from their official instructions and review
their license terms. Installing PDF support uses pip in a computer-local virtual
environment and needs internet; ordinary inventory never installs dependencies.
PyMuPDF uses its upstream open-source/commercial licensing, not Pinakeys' MIT
license. Core catalogue use does not require an online metadata service.

The app finds `clamscan`, `pdfid.py` (or `pdfid`) and `epubcheck` on the computer's
PATH. ClamAV also needs its separately maintained signature database. PDFiD
scripts and EPUBCheck JARs stored elsewhere can be used from the command line:

```sh
python3 audit_materials.py --pdfid '/path/to/pdfid.py' --epubcheck-jar '/path/to/epubcheck.jar'
```

Run this inside `Library Tools/Internal` in an organized kit. It audits the sole
Incoming or Incoming Files folder by default; `--folder` selects a different
relative scope. For the app, use the upstream executable wrappers on PATH.
Run details report which tools actually ran; installing one tool does not enable
the others. No installer automatically downloads or updates malware signatures.

Tkinter is the Python interface to Tcl/Tk; `python -m tkinter` verifies whether
it is installed: https://docs.python.org/3/library/tkinter.html . Browser and OS
file handlers are used only when the user explicitly opens a local link/file.

The optional PDF installers and native-PDF CI use the tested PyMuPDF 1.28.2.
Review upstream changes/licenses and rerun optional coverage before updating
that pin in both installers and CI. GitHub Action pins are maintained through
Dependabot PRs; the bundled parser keeps its recorded source checksum.
