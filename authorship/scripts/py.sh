#!/bin/sh
# Runs a plugin script with AUTHORSHIP_PYTHON when set, else the first Python 3 found: python3, python,
# or the Windows launcher (py -3).
# Usage: sh py.sh <script.py> [args...]   (the script is looked up next to this file)
d=$(dirname "$0")
s=$1
shift
if [ -n "$AUTHORSHIP_PYTHON" ]; then
  exec "$AUTHORSHIP_PYTHON" "$d/$s" "$@"
fi
for p in python3 python; do
  c=$(command -v "$p" 2>/dev/null) || continue
  case "$c" in *WindowsApps*) continue ;; esac  # the Microsoft Store stub, which opens the Store instead
  exec "$c" "$d/$s" "$@"
done
if command -v py >/dev/null 2>&1; then
  exec py -3 "$d/$s" "$@"
fi
echo "authorship: no Python 3 found (tried python3, python, py)" >&2
exit 1
