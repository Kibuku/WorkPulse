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

build_flavor() {
  local slug="$1" app_name="$2" product="$3" role="$4" entry="$5" identifier="$6"
  local flavor_build="$BUILD/$slug" payload="$BUILD/$slug/payload"
  local scripts="$BUILD/$slug/scripts" component="$BUILD/$slug/component.plist"
  mkdir -p "$payload/Applications" "$scripts"
  cp -R "$BUILD/dist/WorkPulse.app" "$payload/Applications/$app_name.app"
  sed -e "s|@APP_NAME@|$app_name|g" -e "s|@PRODUCT@|$product|g" \
      -e "s|@ROLE@|$role|g" -e "s|@ENTRY_PATH@|$entry|g" \
      "$ROOT/installer/desktop/macos-postinstall" > "$scripts/postinstall"
  chmod +x "$scripts/postinstall"
  pkgbuild --analyze --root "$payload" "$component"
  /usr/libexec/PlistBuddy -c "Set :0:BundleIsRelocatable false" "$component"
  pkgbuild --root "$payload" --scripts "$scripts" --component-plist "$component" \
    --identifier "$identifier" --version "$VERSION" --install-location / \
    "$ARTIFACTS/${slug}-${VERSION}-macOS.pkg"
}

build_flavor "WorkPulseInstitution" "WorkPulse Institution" "institution" "member" "/institution" "earth.njiani.workpulse.institution"
build_flavor "LearningPulseFacilitator" "LearningPulse Facilitator" "learning" "facilitator" "/learning" "earth.njiani.pulse.learning.facilitator"
build_flavor "LearningPulseDevice" "LearningPulse Device" "learning" "device" "/learning/device" "earth.njiani.pulse.learning.device"
