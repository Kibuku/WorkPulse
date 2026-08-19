#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
if [[ -n "${PYTHON:-}" ]]; then
  :
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  # GitHub setup-python exposes its managed interpreter as `python`. Prefer it
  # over Homebrew's versioned executables, which are PEP-668 externally managed
  # and reject the build dependency installation.
  for candidate in python python3.12 python3.11 python3.13 python3; do
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
FLAVOR="${1:-all}"

$PYTHON -m pip install --upgrade pip
$PYTHON -m pip install -e '.[mac]' pyinstaller pystray pillow
rm -rf "$BUILD"
mkdir -p "$BUILD" "$ARTIFACTS"
# A local build must not erase historical release packages. The selected
# output filename is replaced atomically by pkgbuild when its version matches.

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
  local info="$payload/Applications/$app_name.app/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName $app_name" "$info"
  /usr/libexec/PlistBuddy -c "Set :CFBundleName $app_name" "$info"
  /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $identifier" "$info"
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$info"
  /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$info" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$info"
  # Updating Info.plist invalidates PyInstaller's ad-hoc signature. Re-sign the
  # complete local bundle so macOS can verify it before packaging.
  codesign --force --deep --sign - "$payload/Applications/$app_name.app"
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

case "$FLAVOR" in
  personal)
    build_flavor "PersonalWorkPulse" "Personal WorkPulse" "personal" "individual" "/personal" "earth.njiani.workpulse.personal"
    ;;
  all)
    build_flavor "PersonalWorkPulse" "Personal WorkPulse" "personal" "individual" "/personal" "earth.njiani.workpulse.personal"
    build_flavor "WorkPulseInstitution" "WorkPulse Institution" "institution" "member" "/institution" "earth.njiani.workpulse.institution"
    build_flavor "LearningPulseFacilitator" "LearningPulse Facilitator" "learning" "facilitator" "/learning" "earth.njiani.pulse.learning.facilitator"
    build_flavor "LearningPulseDevice" "LearningPulse Device" "learning" "device" "/learning/device" "earth.njiani.pulse.learning.device"
    ;;
  institution)
    build_flavor "WorkPulseInstitution" "WorkPulse Institution" "institution" "member" "/institution" "earth.njiani.workpulse.institution"
    ;;
  facilitator)
    build_flavor "LearningPulseFacilitator" "LearningPulse Facilitator" "learning" "facilitator" "/learning" "earth.njiani.pulse.learning.facilitator"
    ;;
  device)
    build_flavor "LearningPulseDevice" "LearningPulse Device" "learning" "device" "/learning/device" "earth.njiani.pulse.learning.device"
    ;;
  *)
    echo "usage: $0 [all|personal|institution|facilitator|device]" >&2
    exit 2
    ;;
esac
