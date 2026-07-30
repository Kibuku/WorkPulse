$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = (Get-Command python -ErrorAction Stop).Source
& $Python -c "import sys; raise SystemExit(sys.version_info < (3, 11))"
if ($LASTEXITCODE -ne 0) { throw "WorkPulse requires Python 3.11 or newer to build." }
$Version = (& $Python -c "from workpulse import __version__; print(__version__)").Trim()
$Build = Join-Path $PSScriptRoot "build"
$Artifacts = Join-Path $Root "release-artifacts"

& $Python -m pip install --upgrade pip
& $Python -m pip install -e "${Root}[win]" pyinstaller pystray pillow
if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
New-Item -ItemType Directory -Force -Path $Build | Out-Null
# Historical release files remain in Git, but each Actions artifact should
# contain only the installer created by this run.
Get-ChildItem $Artifacts -Filter "*Windows.exe" -ErrorAction SilentlyContinue |
  Remove-Item -Force

& $Python -m PyInstaller --noconfirm --clean --windowed `
  --name WorkPulse `
  --distpath $Build `
  --workpath (Join-Path $Build "pyi") `
  --specpath $Build `
  --collect-all workpulse `
  --collect-all sqlite_vec `
  --hidden-import pystray._win32 `
  --add-data "$Root\config\config.example.yaml;config" `
  "$Root\workpulse\desktop.py"

$ISCC = @(
  "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $ISCC) { throw "Inno Setup 6 is not installed" }
& $ISCC "/DAppVersion=$Version" (Join-Path $PSScriptRoot "WorkPulse.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
