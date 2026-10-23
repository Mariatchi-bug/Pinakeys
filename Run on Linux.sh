#!/bin/sh
# Works on macOS and Linux. Dependencies are installed per computer, not on the USB.
set -u
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
VENV_PYTHON="$HOME/.usb-book-index/venv/bin/python"
if [ -x "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON=$(command -v python3)
else
    echo 'Python 3 is required. See README.md for setup instructions.' >&2
    exit 1
fi
"$PYTHON" "$ROOT/scan_library.py" "$@"
