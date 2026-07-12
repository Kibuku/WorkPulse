#!/usr/bin/env bash
# Remote Windows test harness for the WorkPulse package installer.
#
# From this Mac, in one command: build the package, copy it to the Windows box
# over SSH, run install.ps1 there, then run workpulse version/status/doctor and
# stream the output back. It exercises the exact path a real Windows pilot user
# follows, so it doubles as Windows validation.
#
# One-time setup on the Windows box (Administrator PowerShell):
#   Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
#   Start-Service sshd
#   Set-Service -Name sshd -StartupType Automatic
#   ipconfig            # note the IPv4 address
#
# Usage:
#   installer/remote-test.sh <win-user>@<win-host>
#   WIN_TARGET=georgek@192.168.1.42 installer/remote-test.sh
set -euo pipefail

TARGET="${1:-${WIN_TARGET:-}}"
if [[ -z "$TARGET" ]]; then
  echo "Usage: installer/remote-test.sh <win-user>@<win-host>" >&2
  echo "   or: WIN_TARGET=user@host installer/remote-test.sh" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VER="$(sed -n 's/^__version__ = "\(.*\)".*/\1/p' workpulse/__init__.py | head -1)"
[[ -n "$VER" ]] || { echo "Could not read __version__ from workpulse/__init__.py" >&2; exit 1; }
ZIP="dist/WorkPulse-v${VER}.zip"

echo "==> [1/4] Building package (v${VER})"
./installer/build-package.sh
[[ -f "$ZIP" ]] || { echo "Expected $ZIP not found after build." >&2; exit 1; }

echo "==> [2/4] Writing remote runner"
RUNNER="dist/remote-run.ps1"
# Unquoted heredoc: bash expands ${VER}; PowerShell '$' is escaped as '\$'.
cat > "$RUNNER" <<PS1
\$ErrorActionPreference = "Stop"
\$ver = "${VER}"
Write-Host "==> Expanding package"
Remove-Item -Recurse -Force "\$HOME\WorkPulse-pkg" -ErrorAction SilentlyContinue
Expand-Archive -Force "\$HOME\WorkPulse-pkg.zip" -DestinationPath "\$HOME\WorkPulse-pkg"
\$installer = Join-Path "\$HOME" "WorkPulse-pkg\WorkPulse-v\$ver\install.ps1"
Write-Host "==> Running installer: \$installer"
powershell -ExecutionPolicy Bypass -File "\$installer"
Write-Host ""
Write-Host "==> Post-install checks"
. (Join-Path "\$HOME" "WorkPulse\.venv\Scripts\Activate.ps1")
workpulse version
workpulse status
workpulse doctor
PS1

echo "==> [3/4] Copying package + runner to ${TARGET}"
scp "$ZIP"    "${TARGET}:WorkPulse-pkg.zip"
scp "$RUNNER" "${TARGET}:remote-run.ps1"

echo "==> [4/4] Installing + checking on ${TARGET}"
ssh "$TARGET" "powershell -ExecutionPolicy Bypass -File remote-run.ps1"

echo
echo "==> Remote test finished on ${TARGET}"
echo "    (WorkPulse installed to the Windows user's home folder: %USERPROFILE%\\WorkPulse)"
