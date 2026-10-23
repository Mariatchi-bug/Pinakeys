#!/bin/sh
# One-time setup on each macOS or Linux computer. Requires internet.
set -eu
if ! command -v python3 >/dev/null 2>&1; then
    echo 'Please install Python 3 first, then re-run this installer.' >&2
    exit 1
fi
TARGET="$HOME/.usb-book-index/venv"
mkdir -p "$(dirname "$TARGET")"
python3 -m venv "$TARGET" || {
    echo 'Could not create Python virtual environment.' >&2
    echo 'On Debian/Ubuntu, you may need: sudo apt install python3-venv' >&2
    exit 1
}
"$TARGET/bin/python" -m pip install pymupdf==1.28.2
printf '\nReady. Use the Run launcher on your USB. No internet required for scans.\n'
