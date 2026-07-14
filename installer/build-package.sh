#!/usr/bin/env bash
# Build the shareable WorkPulse install packages.
#
#   ./installer/build-package.sh
#
# Produces three zips in dist/, all sharing the same app payload (exactly the
# git-tracked files, so no DB, no config.yaml, no vault: user data is gitignored
# and excluded):
#
#   WorkPulse-v<version>-mac.zip   install.command + payload   (the site's Mac download)
#   WorkPulse-v<version>-win.zip   install.ps1     + payload   (the site's Windows download)
#   WorkPulse-v<version>.zip       both installers + payload   (one package, pick your OS)
#
# The per-OS zips are what the Njiani site links as "Download for Mac" and
# "Download for Windows" so a pilot grabs exactly the one for their machine. The
# combined zip is kept for the Windows test harness (installer/remote-test.sh)
# and for anyone who wants both in one file. A pilot unzips their zip and runs
# the installer; it installs to ~/WorkPulse and applies later versions in place.
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

echo "==> Packaging WorkPulse $VER"

# Extract the app payload ONCE (tracked files only -> user data excluded by
# design), then each platform zip reuses this same payload.
PAYLOAD="$STAGE/_payload"
mkdir -p "$PAYLOAD"
git archive --format=tar HEAD | tar -x -C "$PAYLOAD"

mkdir -p "$REPO_ROOT/dist"

# READMEs, one per package flavour. No em dashes (house voice).
readme_mac() {
  cat <<EOF
WorkPulse $VER install package (macOS)

Double-click "install.command"
  (or in Terminal:  bash install.command)

WorkPulse installs to a "WorkPulse" folder in your home directory. To update
later, just run the installer from a newer package. Your data and settings are
kept, and anything removed in the new version is cleaned up automatically.
Requires Python 3.11 or newer.
EOF
}
readme_win() {
  cat <<EOF
WorkPulse $VER install package (Windows)

Right-click "install.ps1" and choose Run with PowerShell
  (or:  powershell -ExecutionPolicy Bypass -File .\\install.ps1)

WorkPulse installs to a "WorkPulse" folder in your home directory. To update
later, just run the installer from a newer package. Your data and settings are
kept, and anything removed in the new version is cleaned up automatically.
Requires Python 3.11 or newer.
EOF
}
readme_both() {
  cat <<EOF
WorkPulse $VER install package

macOS:    double-click "install.command"
          (or in Terminal:  bash install.command)
Windows:  right-click "install.ps1" and choose Run with PowerShell
          (or:  powershell -ExecutionPolicy Bypass -File .\\install.ps1)

WorkPulse installs to a "WorkPulse" folder in your home directory. To update
later, just run the installer from a newer package. Your data and settings are
kept, and anything removed in the new version is cleaned up automatically.
Requires Python 3.11 or newer.
EOF
}

# build_pkg <dist-zip-name> <readme-fn> <installer-basename...>
# Assembles a package dir (WorkPulse-v<ver>/ with payload + the given
# installers + a README) and zips it into dist/.
build_pkg() {
  local zipname="$1" readme="$2"; shift 2
  local build="$STAGE/b_${zipname}"
  local root="$build/$PKG"
  rm -rf "$build"
  mkdir -p "$root/payload"
  cp -R "$PAYLOAD/." "$root/payload/"
  local inst
  for inst in "$@"; do
    cp "installer/$inst" "$root/$inst"
    [[ "$inst" == *.command ]] && chmod +x "$root/$inst"
  done
  "$readme" > "$root/READ-ME-FIRST.txt"
  local out="$REPO_ROOT/dist/${zipname}"
  rm -f "$out"
  ( cd "$build" && zip -qr "$out" "$PKG" )
  echo "==> Built dist/${zipname}"
}

build_pkg "${PKG}-mac.zip" readme_mac install.command
build_pkg "${PKG}-win.zip" readme_win install.ps1
build_pkg "${PKG}.zip"     readme_both install.command install.ps1

echo "==> Done. Per-OS zips are the site's Mac / Windows downloads;"
echo "    the combined zip stays for installer/remote-test.sh."
