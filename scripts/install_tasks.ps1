# install_tasks.ps1 — register WorkPulse scripts as Windows Task Scheduler tasks.
#
# Run once from an elevated (Administrator) PowerShell terminal:
#   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
#
# Tasks created:
#   WorkPulse-Watcher       At logon, restarts on failure
#   WorkPulse-ReportDaily   Daily at 23:00 EAT (UTC+3 = 20:00 UTC)
#   WorkPulse-ReportWeekly  Sunday at 20:00 EAT (17:00 UTC)

$Root    = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
# Use pythonw.exe (windowless) so Task Scheduler launches don't crash from
# missing console handles. python.exe needs a console; pythonw.exe doesn't.
$Python  = Join-Path $Root '.venv\Scripts\pythonw.exe'
$TaskUser = $env:USERNAME

# ── helpers ───────────────────────────────────────────────────────────────────

function Register-WPTask {
    param(
        [string]$Name,
        [string]$Arguments,
        [object]$Trigger,
        [string]$Description
    )

    $action = New-ScheduledTaskAction `
        -Execute $Python `
        -Argument $Arguments `
        -WorkingDirectory $Root

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -RestartCount 5 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)

    $principal = New-ScheduledTaskPrincipal `
        -UserId $TaskUser `
        -LogonType Interactive `
        -RunLevel Limited

    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "  Removed existing task: $Name"
    }

    Register-ScheduledTask `
        -TaskName    $Name `
        -Action      $action `
        -Trigger     $Trigger `
        -Settings    $settings `
        -Principal   $principal `
        -Description $Description | Out-Null

    Write-Host "  Registered: $Name"
}

# ── watcher (at logon) ────────────────────────────────────────────────────────

Write-Host "`n[1/3] WorkPulse tray app (at logon)..."
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $TaskUser
Register-WPTask `
    -Name        "WorkPulse-Watcher" `
    -Arguments   "scripts\tray.py" `
    -Trigger     $triggerLogon `
    -Description "WorkPulse — tray app, dashboard server, and file watcher"

# ── daily report (23:00 EAT = 20:00 UTC) ─────────────────────────────────────

Write-Host "`n[2/3] Daily report (23:00 EAT / 20:00 UTC)..."
$triggerDaily = New-ScheduledTaskTrigger -Daily -At "20:00"
Register-WPTask `
    -Name        "WorkPulse-ReportDaily" `
    -Arguments   "scripts\report.py --mode daily" `
    -Trigger     $triggerDaily `
    -Description "WorkPulse daily micro-report at 23:00 EAT"

# ── weekly report (Sunday 20:00 EAT = 17:00 UTC) ──────────────────────────────

Write-Host "`n[3/3] Weekly report (Sunday 20:00 EAT / 17:00 UTC)..."
$triggerWeekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "17:00"
Register-WPTask `
    -Name        "WorkPulse-ReportWeekly" `
    -Arguments   "scripts\report.py --mode weekly" `
    -Trigger     $triggerWeekly `
    -Description "WorkPulse weekly report on Sunday at 20:00 EAT"

# ── summary ───────────────────────────────────────────────────────────────────

Write-Host "`nDone. Registered tasks:"
Get-ScheduledTask | Where-Object { $_.TaskName -like "WorkPulse-*" } |
    Select-Object TaskName, State |
    Format-Table -AutoSize

Write-Host "To start the watcher immediately (without logging off):"
Write-Host "  Start-ScheduledTask -TaskName 'WorkPulse-Watcher'"
