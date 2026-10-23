#!/bin/sh
# Open from Finder (if permitted), or run: sh 'path/to/Run on Mac.command'
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
sh "$ROOT/Run on Linux.sh" "$@"
STATUS=$?
echo
if [ -t 0 ]; then
    printf 'Press Return to close this window... '
    read -r answer
fi
exit "$STATUS"
