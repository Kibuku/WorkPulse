# build.ps1 — produce installer\dist\WorkPulseSetup.exe.
#
# Bundles an embedded Python 3.12 distribution plus all WorkPulse runtime
# dependencies, then wraps the lot in a single Inno Setup installer.
#
# Re-runnable: each stage is idempotent. Downloaded artefacts are cached in
# installer\cache\ to keep subsequent builds fast.
#
# Usage:
#   cd D:\WorkPulse
#   powershell -ExecutionPolicy Bypass -File .\installer\build.ps1
#
# Output:
#   installer\dist\WorkPulseSetup-<version>.exe   (~40-60 MB)

$ErrorActionPreference = 'Stop'

# ── paths ─────────────────────────────────────────────────────────────────────
$Root        = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$InstallerDir= $PSScriptRoot
$BuildDir    = Join-Path $InstallerDir 'build'
$DistDir     = Join-Path $InstallerDir 'dist'
$CacheDir    = Join-Path $InstallerDir 'cache'
$PythonDir   = Join-Path $BuildDir 'python'
$IssScript   = Join-Path $InstallerDir 'WorkPulse.iss'

# ── config ────────────────────────────────────────────────────────────────────
$PYV         = '3.12.7'
$AppVersion  = '1.0.0'

# ── helpers ───────────────────────────────────────────────────────────────────
function Step($n, $msg) {
    Write-Host ""
    Write-Host "[$n] $msg" -ForegroundColor Cyan
}

function Find-ISCC {
    $candidates = @(
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    throw "Inno Setup not found. Install with: winget install JRSoftware.InnoSetup"
}

# ── 1. Clean / prepare staging ────────────────────────────────────────────────
Step 1 "Preparing build directories..."
if (Test-Path $BuildDir) {
    Remove-Item -Recurse -Force $BuildDir
}
New-Item -ItemType Directory -Force -Path $BuildDir, $DistDir, $CacheDir | Out-Null
Write-Host "  build:  $BuildDir"
Write-Host "  dist:   $DistDir"
Write-Host "  cache:  $CacheDir"

# ── 2. Embedded Python ────────────────────────────────────────────────────────
Step 2 "Fetching embedded Python $PYV..."
$pyZip = Join-Path $CacheDir "python-$PYV-embed-amd64.zip"
if (-not (Test-Path $pyZip)) {
    $url = "https://www.python.org/ftp/python/$PYV/python-$PYV-embed-amd64.zip"
    Write-Host "  Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $pyZip -UseBasicParsing
} else {
    Write-Host "  Cached: $pyZip"
}
Write-Host "  Extracting to: $PythonDir"
Expand-Archive -Path $pyZip -DestinationPath $PythonDir -Force

# ── 3. Enable `import site` so pip works ──────────────────────────────────────
Step 3 "Enabling site-packages in embedded Python..."
$pthFile = Get-ChildItem $PythonDir -Filter 'python*._pth' | Select-Object -First 1
if (-not $pthFile) { throw "python._pth file not found in $PythonDir" }
$pthContent = Get-Content $pthFile.FullName -Raw
$pthContent = $pthContent -replace '#\s*import site', 'import site'
Set-Content -Path $pthFile.FullName -Value $pthContent -NoNewline
Write-Host "  Patched: $($pthFile.Name)"

# ── 4. Bootstrap pip ──────────────────────────────────────────────────────────
Step 4 "Installing pip into embedded Python..."
$getPip = Join-Path $CacheDir 'get-pip.py'
if (-not (Test-Path $getPip)) {
    Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getPip -UseBasicParsing
}
& "$PythonDir\python.exe" $getPip --no-warn-script-location 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "get-pip.py failed (exit $LASTEXITCODE)" }
Write-Host "  pip installed"

# ── 5. Install WorkPulse runtime dependencies ─────────────────────────────────
Step 5 "Installing requirements.txt into embedded Python (this is the slow step)..."
& "$PythonDir\python.exe" -m pip install -r "$Root\requirements.txt" --no-warn-script-location --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements.txt failed (exit $LASTEXITCODE)" }
Write-Host "  Dependencies installed"

# ── 6. Stage WorkPulse source files ───────────────────────────────────────────
Step 6 "Staging WorkPulse source files..."
$copyMap = @{
    'scripts'                = 'scripts'
    'config\config.example.yaml' = 'config\config.example.yaml'
    'setup.ps1'              = 'setup.ps1'
    'requirements.txt'       = 'requirements.txt'
    'README.md'              = 'README.md'
}
foreach ($srcRel in $copyMap.Keys) {
    $src = Join-Path $Root $srcRel
    $dst = Join-Path $BuildDir $copyMap[$srcRel]
    if (-not (Test-Path $src)) {
        Write-Host "  SKIP (missing): $srcRel" -ForegroundColor Yellow
        continue
    }
    $dstParent = Split-Path -Parent $dst
    if (-not (Test-Path $dstParent)) {
        New-Item -ItemType Directory -Force -Path $dstParent | Out-Null
    }
    if (Test-Path $src -PathType Container) {
        Copy-Item -Path "$src\*" -Destination $dst -Recurse -Force
    } else {
        Copy-Item -Path $src -Destination $dst -Force
    }
}

# Strip throwaway dev scripts that start with underscore + clean __pycache__
Get-ChildItem -Path (Join-Path $BuildDir 'scripts') -Recurse -Include '_*.ps1','_*.py' `
    -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path (Join-Path $BuildDir 'scripts') -Recurse -Filter '__pycache__' `
    -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force

# Report bundle size
$totalKB = (Get-ChildItem $BuildDir -Recurse | Measure-Object Length -Sum).Sum / 1KB
Write-Host ("  Staged. Total bundle size: {0:N1} MB" -f ($totalKB / 1024))

# ── 7. Run Inno Setup compiler ────────────────────────────────────────────────
Step 7 "Running Inno Setup compiler..."
$iscc = Find-ISCC
Write-Host "  ISCC: $iscc"
$compileArgs = @(
    "/DAppVersion=$AppVersion",
    "/Qp",                        # quieter output, with progress
    $IssScript
)
& $iscc @compileArgs
if ($LASTEXITCODE -ne 0) { throw "ISCC failed (exit $LASTEXITCODE)" }

# ── 8. Report ─────────────────────────────────────────────────────────────────
Step 8 "Build complete."
$exe = Get-ChildItem $DistDir -Filter 'WorkPulseSetup*.exe' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($exe) {
    Write-Host ""
    Write-Host "  Installer: $($exe.FullName)" -ForegroundColor Green
    Write-Host ("  Size:      {0:N1} MB" -f ($exe.Length / 1MB))
    Write-Host ""
    Write-Host "  To test, double-click the installer. It installs to %LOCALAPPDATA%\Programs\WorkPulse"
    Write-Host "  and registers WorkPulse as a startup app. No admin required."
} else {
    Write-Host "  WARNING: no installer found in $DistDir" -ForegroundColor Yellow
}
