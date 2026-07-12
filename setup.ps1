# WorkPulse - one-command install for Windows.
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
# Creates a virtualenv, installs WorkPulse, and sets up the background agents.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$py = "py"
if (-not (Get-Command $py -ErrorAction SilentlyContinue)) { $py = "python" }
if (-not (Get-Command $py -ErrorAction SilentlyContinue)) {
    Write-Error "Python 3.11+ is required. Install it from https://www.python.org/downloads/ (tick 'Add to PATH') and re-run."
    exit 1
}

Write-Host "==> Creating virtualenv (.venv)"
& $py -m venv .venv
$activate = Join-Path $PSScriptRoot ".venv\Scripts\Activate.ps1"
. $activate

Write-Host "==> Installing WorkPulse (this can take a minute)"
python -m pip install --upgrade pip | Out-Null
pip install -e ".[win]"

Write-Host "==> Setting up config, database, and background agents"
workpulse install

Write-Host @"

------------------------------------------------------------
WorkPulse is installed. To open the dashboard now:

    .\.venv\Scripts\Activate.ps1
    workpulse web        # then visit http://127.0.0.1:5700

The background sensors and hourly jobs run via Task Scheduler.
Check anytime with:  workpulse status
------------------------------------------------------------
"@
