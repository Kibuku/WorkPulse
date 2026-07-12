# WorkPulse - shareable installer for Windows.
#
# Hand a user the whole package folder (or a .zip of it). They run:
#     powershell -ExecutionPolicy Bypass -File .\install.ps1
# and WorkPulse installs to %USERPROFILE%\WorkPulse. Run it again from a NEWER
# package to update that same install in place:
#   - new/changed code files are copied in
#   - files removed in the new version (archived) are deleted from the install
#   - your data and settings are preserved untouched (see $preserve below)
#
# Override the install location by setting $env:WORKPULSE_HOME before running.
$ErrorActionPreference = "Stop"

$scriptDir = $PSScriptRoot
$src  = Join-Path $scriptDir "payload"
$dest = if ($env:WORKPULSE_HOME) { $env:WORKPULSE_HOME } else { Join-Path $env:USERPROFILE "WorkPulse" }

if (-not (Test-Path (Join-Path $src "workpulse"))) {
    Write-Error "Can't find the package payload at '$src'. Run this installer from inside the WorkPulse package folder (it must sit next to a 'payload' directory)."
    exit 1
}

$py = "py"
if (-not (Get-Command $py -ErrorAction SilentlyContinue)) { $py = "python" }
if (-not (Get-Command $py -ErrorAction SilentlyContinue)) {
    Write-Error "Python 3.11+ is required. Install it from https://www.python.org/downloads/ (tick 'Add to PATH') and re-run."
    exit 1
}

function Read-Version($initPath) {
    if (-not (Test-Path $initPath)) { return "?" }
    $m = Select-String -Path $initPath -Pattern '^__version__ = "(.*)"' | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value } else { return "?" }
}

$newVer = Read-Version (Join-Path $src "workpulse\__init__.py")

$destInit = Join-Path $dest "workpulse\__init__.py"
Write-Host "============================================================"
if (Test-Path $destInit) {
    $oldVer = Read-Version $destInit
    Write-Host "WorkPulse - updating existing install"
    Write-Host "  location : $dest"
    Write-Host "  version  : $oldVer  ->  $newVer"
    $mode = "update"
} else {
    Write-Host "WorkPulse - fresh install"
    Write-Host "  location : $dest"
    Write-Host "  version  : $newVer"
    $mode = "fresh"
}
Write-Host "============================================================"

New-Item -ItemType Directory -Force -Path $dest | Out-Null

# Directories/files that belong to the USER, never to a release: protected from the
# /MIR mirror-delete and never overwritten. Keep in sync with the user-data section
# of .gitignore. Full paths anchor them to the install root.
$xdirs = @(".venv", ".git", "logs", "captures", "consolidation", "reports", "brain", "vault") |
    ForEach-Object { Join-Path $dest $_ }
$xfiles = @(
    (Join-Path $dest "config\config.yaml"),
    (Join-Path $dest "config\projects.yaml"),
    (Join-Path $dest "config\identity.yaml"),
    (Join-Path $dest "config\calendar.url"),
    (Join-Path $dest "config\secrets.json")
)

Write-Host "==> Syncing files (removing anything archived in this version, keeping your data)"
# /MIR mirrors src -> dest (adds/updates/deletes); /XD and /XF shield your data+venv.
# Also exclude root-level SQLite files by name so the DB is never mirror-deleted.
robocopy $src $dest /MIR /XD $xdirs /XF $xfiles "*.db" "*.db-wal" "*.db-shm" ".DS_Store" | Out-Null
if ($LASTEXITCODE -ge 8) {
    Write-Error "File sync failed (robocopy exit code $LASTEXITCODE)."
    exit 1
}

Write-Host "==> Running setup (venv, dependencies, config merge, database, agents)"
# setup.ps1 sets its own location, so the venv + editable install land in $dest.
powershell -ExecutionPolicy Bypass -File (Join-Path $dest "setup.ps1")

Write-Host ""
Write-Host "============================================================"
if ($mode -eq "update") {
    Write-Host "Updated to WorkPulse $newVer at $dest"
    Write-Host "Your settings and data were preserved. If the dashboard was open, restart it."
} else {
    Write-Host "Installed WorkPulse $newVer at $dest"
}
Write-Host "Open it with:"
Write-Host "    cd `"$dest`"; .\.venv\Scripts\Activate.ps1; workpulse web"
Write-Host "Then visit http://127.0.0.1:5700"
Write-Host "============================================================"
