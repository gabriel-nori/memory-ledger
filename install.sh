#!/usr/bin/env bash
# memory-ledger installer for macOS and Linux: make sure a Python this project
# supports is there (installing one under ~/.second_brain when not), then run
# scripts/setup.py, which does the rest and puts a launcher on the Desktop.
#   ./install.sh [setup.py options]
# The wanted version lives in .python-version; MIN is the oldest we accept.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
want="$(tr -d '[:space:]' < "$here/.python-version")"
min_major=3; min_minor=11
home="${SECOND_BRAIN_HOME:-$HOME/.second_brain}"

ok_python() { "$1" -c "import sys; sys.exit(sys.version_info < ($min_major, $min_minor))" 2>/dev/null; }

for py in "python$want" python3 python; do
  if command -v "$py" >/dev/null 2>&1 && ok_python "$py"; then
    exec "$py" "$here/scripts/setup.py" --launcher "$@"
  fi
done

echo "No Python $min_major.$min_minor+ found; installing Python $want under $home/python (uv, nothing system-wide)."
mkdir -p "$home"
export UV_PYTHON_INSTALL_DIR="$home/python"
export UV_UNMANAGED_INSTALL="$home/bin"   # uv's installer: put uv here, leave shell profiles alone
if ! command -v uv >/dev/null 2>&1; then
  if [ ! -x "$home/bin/uv" ]; then
    command -v curl >/dev/null 2>&1 || { echo "curl is needed to fetch uv (https://astral.sh/uv)." >&2; exit 1; }
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  export PATH="$home/bin:$PATH"
fi
uv python install "$want"
py="$(uv python find "$want")"
exec "$py" "$here/scripts/setup.py" --launcher "$@"
