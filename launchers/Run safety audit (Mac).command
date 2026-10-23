#!/bin/sh
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
export PATH="${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}:/opt/homebrew/bin:/usr/local/bin"
sh "$ROOT/Run safety audit (Linux).sh" "$@"
STATUS=$?
echo
if [ -t 0 ]; then
    printf 'Press Return to close this window... '
    read -r answer
fi
exit "$STATUS"
