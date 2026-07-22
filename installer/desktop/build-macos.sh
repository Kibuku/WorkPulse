#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"
VERSION="$($PYTHON -c 'from workpulse import __version__; print(__version__)')"
BUILD="$ROOT/installer/desktop/build-mac"
ARTIFACTS="$ROOT/release-artifacts"

$PYTHON -m pip install --upgrade pip
$PYTHON -m pip install -e '.[mac]' pyinstaller pystray pillow
rm -rf "$BUILD"
mkdir -p "$BUILD" "$ARTIFACTS"

$PYTHON -m PyInstaller --noconfirm --clean --windowed \
  --name WorkPulse \
  --osx-bundle-identifier earth.njiani.workpulse \
  --distpath "$BUILD/dist" \
  --workpath "$BUILD/pyi" \
  --specpath "$BUILD" \
  --collect-all workpulse \
  --collect-all sqlite_vec \
  --hidden-import rumps \
  --add-data "$ROOT/config/config.example.yaml:config" \
  "$ROOT/workpulse/desktop.py"

PAYLOAD="$BUILD/payload"
mkdir -p "$PAYLOAD/Applications"
cp -R "$BUILD/dist/WorkPulse.app" "$PAYLOAD/Applications/"

SCRIPTS="$BUILD/scripts"
mkdir -p "$SCRIPTS"
cp "$ROOT/installer/desktop/macos-postinstall" "$SCRIPTS/postinstall"
chmod +x "$SCRIPTS/postinstall"

pkgbuild --root "$PAYLOAD" \
  --scripts "$SCRIPTS" \
  --identifier earth.njiani.workpulse \
  --version "$VERSION" \
  --install-location / \
  "$ARTIFACTS/WorkPulse-${VERSION}-macOS.pkg"
