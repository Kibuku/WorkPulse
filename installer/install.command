#!/usr/bin/env bash
# WorkPulse — shareable installer for macOS.
#
# Hand a user the whole package folder (or a .zip of it). They double-click this
# file (or run `bash install.command`) and WorkPulse installs to ~/WorkPulse.
# Run it again from a NEWER package to update that same install in place:
#   - new/changed code files are copied in
#   - files removed in the new version (archived) are deleted from the install
#   - your data and settings are preserved untouched (see PRESERVE below)
#
# Override the install location with:  WORKPULSE_HOME=/some/path bash install.command
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/payload"
DEST="${WORKPULSE_HOME:-$HOME/WorkPulse}"

if [[ ! -d "$SRC" ]]; then
  echo "ERROR: can't find the package payload at:" >&2
  echo "  $SRC" >&2
  echo "Run this installer from inside the WorkPulse package folder (it must sit next to a 'payload/' directory)." >&2
  exit 1
fi

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.11+ is required but '$PY' was not found." >&2
  echo "Install it from https://www.python.org/downloads/ and re-run." >&2
  exit 1
fi

read_version() {  # read_version <path-to-__init__.py>  → prints version or "?"
  local f="$1"
  [[ -f "$f" ]] || { echo "?"; return; }
  sed -n 's/^__version__ = "\(.*\)".*/\1/p' "$f" | head -1
}

NEW_VER="$(read_version "$SRC/workpulse/__init__.py")"

# Files that belong to the USER, never to a release. These are protected from the
# --delete sweep and never overwritten. Keep in sync with the user-data section of
# .gitignore. Paths are anchored (leading /) to the install root.
PRESERVE=(
  --exclude='/.venv/'
  --exclude='/.git/'
  --exclude='/config/config.yaml'
  --exclude='/config/projects.yaml'
  --exclude='/config/identity.yaml'
  --exclude='/config/calendar.url'
  --exclude='/config/secrets.json'
  --exclude='/*.db'
  --exclude='/*.db-wal'
  --exclude='/*.db-shm'
  --exclude='/logs/'
  --exclude='/captures/'
  --exclude='/consolidation/'
  --exclude='/reports/'
  --exclude='/brain/'
  --exclude='/vault/'
  --exclude='.DS_Store'
)

echo "============================================================"
if [[ -f "$DEST/workpulse/__init__.py" ]]; then
  OLD_VER="$(read_version "$DEST/workpulse/__init__.py")"
  echo "WorkPulse — updating existing install"
  echo "  location : $DEST"
  echo "  version  : $OLD_VER  →  $NEW_VER"
  MODE="update"
else
  echo "WorkPulse — fresh install"
  echo "  location : $DEST"
  echo "  version  : $NEW_VER"
  MODE="fresh"
fi
echo "============================================================"

mkdir -p "$DEST"

echo "==> Syncing files (removing anything archived in this version, keeping your data)"
# --delete removes files the new version dropped; PRESERVE shields your data+venv.
rsync -a --delete "${PRESERVE[@]}" "$SRC/" "$DEST/"

echo "==> Running setup (venv, dependencies, config merge, database, agents)"
chmod +x "$DEST/setup.sh"
# setup.sh cd's into its own dir, so the venv + editable install land in $DEST.
"$DEST/setup.sh"

echo
echo "============================================================"
if [[ "$MODE" == "update" ]]; then
  echo "Updated to WorkPulse $NEW_VER at $DEST"
  echo "Your settings and data were preserved. If the dashboard was open, restart it."
else
  echo "Installed WorkPulse $NEW_VER at $DEST"
fi
echo "Open it with:"
echo "    cd \"$DEST\" && source .venv/bin/activate && workpulse web"
echo "Then visit http://127.0.0.1:5700"
echo "============================================================"
