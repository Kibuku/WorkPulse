#!/usr/bin/env bash
# Build a shareable WorkPulse install package.
#
#   ./installer/build-package.sh
#
# Produces dist/WorkPulse-v<version>.zip containing:
#   install.command   (macOS installer — double-clickable)
#   install.ps1       (Windows installer)
#   payload/          (the app: exactly the git-tracked files, so no DB, no
#                      config.yaml, no vault — user data is gitignored and excluded)
#
# Share that zip with a pilot user. They unzip it and run the installer for their
# OS; it installs to ~/WorkPulse. Build a new zip from a newer commit/tag and send
# it again to ship an update — the installer applies it in place.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -d .git ]]; then
  echo "build-package must run inside the git checkout (need 'git archive')." >&2
  exit 1
fi

VER="$(sed -n 's/^__version__ = "\(.*\)".*/\1/p' workpulse/__init__.py | head -1)"
[[ -n "$VER" ]] || { echo "Could not read __version__ from workpulse/__init__.py" >&2; exit 1; }

PKG="WorkPulse-v${VER}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
OUT="$STAGE/$PKG"
mkdir -p "$OUT/payload"

echo "==> Packaging WorkPulse $VER"

# git archive = only tracked files → user data (gitignored) is excluded by design.
git archive --format=tar HEAD | tar -x -C "$OUT/payload"

# Top-level installers (copied out of payload for a one-obvious-file experience).
cp installer/install.command "$OUT/install.command"
cp installer/install.ps1     "$OUT/install.ps1"
chmod +x "$OUT/install.command"

cat > "$OUT/READ-ME-FIRST.txt" <<EOF
WorkPulse $VER — install package

macOS:    double-click "install.command"
          (or in Terminal:  bash install.command)
Windows:  right-click "install.ps1" → Run with PowerShell
          (or:  powershell -ExecutionPolicy Bypass -File .\\install.ps1)

WorkPulse installs to a "WorkPulse" folder in your home directory. To update
later, just run the installer from a newer package — your data and settings are
kept, and anything removed in the new version is cleaned up automatically.
EOF

mkdir -p "$REPO_ROOT/dist"
ZIP="$REPO_ROOT/dist/${PKG}.zip"
rm -f "$ZIP"
( cd "$STAGE" && zip -qr "$ZIP" "$PKG" )

echo "==> Built $ZIP"
echo "    Share it with a pilot user; they unzip and run the installer for their OS."
