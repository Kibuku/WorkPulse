#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
if [[ -n "${PYTHON:-}" ]]; then
  :
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
      PYTHON="$candidate"
      break
    fi
  done
fi
if [[ -z "${PYTHON:-}" ]]; then
  echo "WorkPulse requires Python 3.11 or newer to build." >&2
  exit 1
fi
VERSION="$($PYTHON -c 'from workpulse import __version__; print(__version__)')"
BUILD="$ROOT/installer/desktop/build-mac-$VERSION"
ARTIFACTS="$ROOT/release-artifacts"

$PYTHON -m pip install --upgrade pip
$PYTHON -m pip install -e '.[mac]' pyinstaller pystray pillow
rm -rf "$BUILD"
mkdir -p "$BUILD" "$ARTIFACTS"
# Do not upload every historical package as the artifact for this run.
find "$ARTIFACTS" -maxdepth 1 -type f -name '*macOS.pkg' -delete

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

COMPONENT_PLIST="$BUILD/component.plist"
pkgbuild --analyze --root "$PAYLOAD" "$COMPONENT_PLIST"
# PackageKit otherwise remembers an existing copy of the bundle and may
# "relocate" the payload back to a developer build directory. WorkPulse has one
# canonical install location: /Applications/WorkPulse.app.
/usr/libexec/PlistBuddy -c "Set :0:BundleIsRelocatable false" "$COMPONENT_PLIST"

pkgbuild --root "$PAYLOAD" \
  --scripts "$SCRIPTS" \
  --component-plist "$COMPONENT_PLIST" \
  --identifier earth.njiani.workpulse \
  --version "$VERSION" \
  --install-location / \
  "$ARTIFACTS/WorkPulse-${VERSION}-macOS.pkg"
