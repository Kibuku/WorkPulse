param(
  [ValidateSet("all", "personal", "institution", "facilitator", "device")]
  [string]$Flavor = "all"
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = (Get-Command python -ErrorAction Stop).Source
& $Python -c "import sys; raise SystemExit(sys.version_info < (3, 11))"
if ($LASTEXITCODE -ne 0) { throw "WorkPulse requires Python 3.11 or newer to build." }
$Version = (& $Python -c "from workpulse import __version__; print(__version__)").Trim()
$Build = Join-Path $PSScriptRoot "build"
$Artifacts = Join-Path $Root "release-artifacts"
$Tesseract = @(
  (Join-Path $env:ProgramFiles "Tesseract-OCR"),
  (Join-Path ${env:ProgramFiles(x86)} "Tesseract-OCR")
) | Where-Object { $_ -and (Test-Path (Join-Path $_ "tesseract.exe")) } |
  Select-Object -First 1
if (-not $Tesseract) {
  throw "Tesseract OCR is required to build a semantic-capture Windows installer"
}

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
  --add-binary "$Tesseract;tesseract" `
  "$Root\workpulse\desktop.py"

$ISCC = @(
  "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $ISCC) { throw "Inno Setup 6 is not installed" }
$Flavors = @{
  personal = @{ Product="personal"; Role="individual"; Label="Personal WorkPulse"; Slug="PersonalWorkPulse"; Guid="0F82399D-03E7-40BC-BD4D-AF46293918AF" }
  institution = @{ Product="institution"; Role="member"; Label="WorkPulse Institution"; Slug="WorkPulseInstitution"; Guid="B6E2A1C4-7F3A-4E18-9A0B-7C2D5F8E4A11" }
  facilitator = @{ Product="learning"; Role="facilitator"; Label="LearningPulse Facilitator"; Slug="LearningPulseFacilitator"; Guid="8B2F11D9-8830-4DF8-99D1-047E3BE58718" }
  device = @{ Product="learning"; Role="device"; Label="LearningPulse Device"; Slug="LearningPulseDevice"; Guid="509DD493-6AD5-456B-93EA-B6200217C9E6" }
}
$Targets = if ($Flavor -eq "all") { @("personal", "institution", "facilitator", "device") } else { @($Flavor) }
foreach ($Name in $Targets) {
  $F = $Flavors[$Name]
  & $ISCC "/DAppVersion=$Version" "/DProduct=$($F.Product)" `
    "/DRole=$($F.Role)" "/DAppLabel=$($F.Label)" `
    "/DAppSlug=$($F.Slug)" "/DAppGuid=$($F.Guid)" `
    (Join-Path $PSScriptRoot "WorkPulse.iss")
  if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed for $Name" }
}
