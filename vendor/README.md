# Bundled PDF inspection dependency

`pdf_parser.py` is an unmodified copy of Didier Stevens' **pdf-parser.py 0.7.14**.
Its original header says: “Source code put in public domain by Didier Stevens,
no Copyright”. That header and the author's history are retained in the file.
Pinakeys' MIT license does not replace this original public-domain notice.
The source also retains Yusuke Shinyama's copyright (2004–2009) and permissive
license notice for the included LZW decoder. Both original notices are kept
verbatim in the bundled file.

- Upstream: https://github.com/DidierStevens/DidierStevensSuite
- Source commit: `b6167ecdd1318709a24406d7dcf32a48fde411bb`
- Source file: https://raw.githubusercontent.com/DidierStevens/DidierStevensSuite/b6167ecdd1318709a24406d7dcf32a48fde411bb/pdf-parser.py
- SHA-256: `a9fa2faa25e88ab0293543d14aa168ff21fc9fd25a0a8ad657b0dad3171214ac`
- Tool documentation: https://blog.didierstevens.com/programs/pdf-tools/

The follow-up worker verifies the dependency's checksum before importing it.
It calls the object parser on a local in-memory copy; it does not call the
upstream CLI, URL/ZIP input, YARA, decoder plugins or document rendering.
No internet connection or optional Python package is needed for this check.
Updating this dependency requires reviewing the new source, updating the
recorded checksum and rerunning the synthetic regression tests.
