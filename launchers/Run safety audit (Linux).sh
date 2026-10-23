#!/bin/sh
# Preflight inspection of Incoming/ by default; no file movement or deletion.
set -u
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
VENV_PYTHON="$HOME/.usb-book-index/venv/bin/python"
if [ -x "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON=$(command -v python3)
else
    echo 'Python 3 is required. See README.md.' >&2
    exit 1
fi
"$PYTHON" "$ROOT/audit_materials.py" "$@"
