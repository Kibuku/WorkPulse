# setup.ps1 - one-command WorkPulse install for a fresh Windows machine.
#
# Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# What it does:
#   1. Verify Python 3.12+ on PATH (or fail with a clear message)
#   2. Create a .venv and install requirements.txt
#   3. Bootstrap config/ files if missing (config.yaml, identity.yaml)
#   4. Offer to set up the API key and SMTP password (both fully skippable)
#   5. Register the Startup-folder shortcut so the tray auto-launches at logon
#   6. Optionally launch the tray right away
#
# Re-running setup.ps1 is safe — it only creates missing pieces.

param(
    [switch]$Yes,                       # answer "yes" to all optional prompts
    [switch]$SkipSecrets,               # don't prompt for API key / SMTP at all (installer use)
    [switch]$SkipLaunch,                # don't ask whether to launch the tray (installer use)
    [string]$ImportConfig,              # path to an existing config.yaml to copy in
    [string]$BundledPython              # full path to a Python interpreter to use instead of `py`/`python` on PATH
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path $PSScriptRoot).Path
Set-Location $Root

function Write-Step($n, $msg) {
    Write-Host ""
    Write-Host "[$n] $msg" -ForegroundColor Cyan
}

function Confirm-YesNo($msg, $default = $true) {
    if ($Yes) { return $true }
    $hint = if ($default) { '[Y/n]' } else { '[y/N]' }
    $ans = Read-Host "$msg $hint"
    if ([string]::IsNullOrWhiteSpace($ans)) { return $default }
    return $ans -match '^[Yy]'
}

# ── 1. Python ────────────────────────────────────────────────────────────────
Write-Step 1 "Checking for Python 3.12 or later..."
$pythonExe = $null
# Installer use: a Python interpreter was bundled with the .exe and passed in.
if ($BundledPython -and (Test-Path $BundledPython)) {
    $pythonExe = "`"$BundledPython`""
    Write-Host "  Using bundled Python: $BundledPython"
} else {
    foreach ($cmd in @('py -3.12','py -3.13','py -3.14','py -3','python')) {
        try {
            $parts = $cmd -split ' '
            $verRaw = & $parts[0] @($parts[1..($parts.Length-1)]) --version 2>&1
            if ($verRaw -match 'Python (\d+)\.(\d+)') {
                $major = [int]$matches[1]; $minor = [int]$matches[2]
                if ($major -ge 3 -and ($major -gt 3 -or $minor -ge 12)) {
                    $pythonExe = $cmd
                    Write-Host "  Found: $verRaw  (using '$cmd')"
                    break
                }
            }
        } catch { }
    }
}
if (-not $pythonExe) {
    Write-Host "  ERROR: Python 3.12+ not found on PATH." -ForegroundColor Red
    Write-Host "  Install from https://www.python.org/downloads/ and re-run setup.ps1."
    Write-Host "  Tip: check 'Add python.exe to PATH' during the installer."
    exit 1
}

# ── 2. Virtual environment ───────────────────────────────────────────────────
if ($BundledPython) {
    Write-Step 2 "Using bundled Python (skipping .venv creation and pip install)..."
    $venvPython = $BundledPython
    Write-Host "  Python: $BundledPython"
    Write-Host "  Dependencies were pre-installed in the bundle."
} else {
    Write-Step 2 "Setting up Python virtual environment in .venv\..."
    $venvPython = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPython)) {
        $parts = $pythonExe -split ' '
        & $parts[0] @($parts[1..($parts.Length-1)]) -m venv .venv
        Write-Host "  Created .venv"
    } else {
        Write-Host "  .venv already exists; reusing"
    }
    & $venvPython -m pip install --upgrade pip --quiet
    & $venvPython -m pip install -r requirements.txt --quiet
    Write-Host "  Installed dependencies from requirements.txt"
}

# ── 3. Config bootstrap ──────────────────────────────────────────────────────
Write-Step 3 "Bootstrapping config\..."
$cfgDir       = Join-Path $Root 'config'
$cfgPath      = Join-Path $cfgDir 'config.yaml'
$cfgExample   = Join-Path $cfgDir 'config.example.yaml'
$identityPath = Join-Path $cfgDir 'identity.yaml'

if (-not (Test-Path $cfgPath)) {
    if ($ImportConfig -and (Test-Path $ImportConfig)) {
        Copy-Item $ImportConfig $cfgPath
        Write-Host "  Imported config from $ImportConfig"
    } elseif (Test-Path $cfgExample) {
        Copy-Item $cfgExample $cfgPath
        Write-Host "  Created config/config.yaml from template"
        Write-Host "  Edit it later to define your own project streams."
    } else {
        Write-Host "  WARNING: no config.example.yaml found; you'll need to create config.yaml yourself" -ForegroundColor Yellow
    }
} else {
    Write-Host "  config/config.yaml already exists; leaving as-is"
}

if (-not (Test-Path $identityPath)) {
    Write-Host ""
    $actor = if ($Yes) { $env:USERNAME } else { Read-Host "  Your name (free text; used as the WorkPulse attribution label)" }
    if ([string]::IsNullOrWhiteSpace($actor)) { $actor = $env:USERNAME }
    $org = if ($Yes) { '' } else { Read-Host "  Organization or team (optional, press Enter to skip)" }
    $bytes = New-Object byte[] 8
    (New-Object System.Security.Cryptography.RNGCryptoServiceProvider).GetBytes($bytes)
    $actorId = "wp-" + (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
    $createdAt = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
    $idYaml = @"
# Identity file for this WorkPulse install. Local-only; never sent anywhere.
# The actor_id is reserved for the v3 Fingerprint Engine's attribution hash;
# it does NOT get stamped on raw activity logs.
actor_label:  "$actor"
organization: "$org"
hostname:     "$($env:COMPUTERNAME)"
created_at:   "$createdAt"
actor_id:     "$actorId"
"@
    Set-Content -Path $identityPath -Value $idYaml -Encoding UTF8
    Write-Host "  Created config/identity.yaml (actor_id: $actorId)"
} else {
    Write-Host "  config/identity.yaml already exists; leaving as-is"
}

# ── 4. Optional secrets (SKIPPABLE — these are NOT required to use WorkPulse) ─
Write-Step 4 "Optional API key / email setup..."
Write-Host "  WorkPulse runs fine without these. AI auto-tagging and email reports"
Write-Host "  stay off until you configure them. You can add them anytime from"
Write-Host "  the dashboard Settings page (top-right corner) - no terminal needed."
Write-Host ""

if (-not $SkipSecrets -and (Confirm-YesNo "  Add an Anthropic API key now? (enables smart auto-tagging)" $false)) {
    Write-Host ""
    Write-Host "  Get one from: https://console.anthropic.com/settings/keys"
    Write-Host "  (Sign up, add a payment method, click 'Create Key'.)"
    $secureKey = Read-Host "  Paste your API key (input hidden)" -AsSecureString
    $key = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey))
    if ($key) {
        & $venvPython -c "import sys; sys.path.insert(0, 'scripts'); import wp_secrets as s; s.set_('anthropic_key', sys.argv[1])" $key
        Write-Host "  Saved." -ForegroundColor Green
    }
} else {
    Write-Host "  Skipped. Add later from the dashboard."
}

if (-not $SkipSecrets -and (Confirm-YesNo "  Set up Gmail email reports now? (off by default)" $false)) {
    Write-Host ""
    Write-Host "  You need a Gmail App Password (not your normal password)."
    Write-Host "  Create one at: https://myaccount.google.com/apppasswords"
    $secureSmtp = Read-Host "  Paste your App Password (input hidden)" -AsSecureString
    $smtp = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureSmtp))
    if ($smtp) {
        & $venvPython -c "import sys; sys.path.insert(0, 'scripts'); import wp_secrets as s; s.set_('smtp_password', sys.argv[1])" $smtp
        Write-Host "  Saved. Toggle email on from the dashboard Settings page." -ForegroundColor Green
    }
} else {
    Write-Host "  Skipped. Add later from the dashboard."
}

# ── 5. Autostart shortcut ────────────────────────────────────────────────────
Write-Step 5 "Registering the Startup-folder shortcut..."
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'scripts\install_startup.ps1')

# ── 6. Optionally launch the tray ────────────────────────────────────────────
Write-Step 6 "Done."
Write-Host ""
Write-Host "WorkPulse will auto-launch at every logon from now on." -ForegroundColor Green
Write-Host "Dashboard will be at http://127.0.0.1:5700/"
Write-Host ""

if (-not $SkipLaunch -and (Confirm-YesNo "  Launch WorkPulse now?" $true)) {
    $startup  = [Environment]::GetFolderPath('Startup')
    $linkPath = Join-Path $startup 'WorkPulse.lnk'
    Invoke-Item -Path $linkPath
    Start-Sleep -Seconds 4
    Write-Host "  Launched. Look for the WorkPulse icon in your system tray."
}
