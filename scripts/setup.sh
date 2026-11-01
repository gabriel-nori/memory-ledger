#!/usr/bin/env bash
# memory-ledger setup on macOS and Linux: find a Python 3 and run setup.py.
# Every argument passes through: ./scripts/setup.sh --voice
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    exec "$py" "$here/setup.py" "$@"
  fi
done
echo "memory-ledger setup: no Python 3.11+ on PATH. Install it (brew install python, apt install python3 python3-venv) and run this again." >&2
exit 1
