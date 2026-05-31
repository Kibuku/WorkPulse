# install_startup.ps1 - install WorkPulse tray app into Windows Startup folder.
#
# This is the most reliable way to auto-launch a system-tray app at logon.
# Task Scheduler has issues with pystray (stays "Queued", no desktop session).
# Startup folder shortcuts run in the normal user explorer.exe context.
#
# Path-independent: derives WorkPulse root from this script's own location,
# so the same installer works wherever the repo is copied.

$Root      = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Pythonw   = Join-Path $Root '.venv\Scripts\pythonw.exe'
$Script    = Join-Path $Root 'scripts\tray.py'
$Startup   = [Environment]::GetFolderPath('Startup')
$LinkPath  = Join-Path $Startup 'WorkPulse.lnk'

if (-not (Test-Path $Pythonw)) {
    Write-Host "ERROR: $Pythonw not found." -ForegroundColor Red
    Write-Host "Run setup.ps1 first to create the .venv and install dependencies."
    exit 1
}

Write-Host "Installing WorkPulse autostart shortcut..."
Write-Host "  Root:      $Root"
Write-Host "  Target:    $Pythonw $Script"
Write-Host "  Shortcut:  $LinkPath"

$WScript        = New-Object -ComObject WScript.Shell
$Shortcut       = $WScript.CreateShortcut($LinkPath)
$Shortcut.TargetPath        = $Pythonw
$Shortcut.Arguments         = "`"$Script`""
$Shortcut.WorkingDirectory  = $Root
$Shortcut.WindowStyle       = 7    # 7 = minimised, but pythonw is windowless anyway
$Shortcut.Description       = "WorkPulse - local productivity tracker"
$Shortcut.Save()

# If a legacy Task Scheduler entry exists, disable it so we don't have two
# competing autostarts.
if (Get-ScheduledTask -TaskName 'WorkPulse-Watcher' -ErrorAction SilentlyContinue) {
    Disable-ScheduledTask -TaskName 'WorkPulse-Watcher' | Out-Null
    Write-Host "  Disabled legacy WorkPulse-Watcher Task Scheduler entry"
}

Write-Host ""
Write-Host "Done. WorkPulse will now launch automatically at every logon."
Write-Host "To launch immediately without logging out, run:"
Write-Host "  & '$Pythonw' '$Script'"
