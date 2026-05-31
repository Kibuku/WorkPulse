; WorkPulse.iss — Inno Setup script.
;
; Builds WorkPulseSetup-<version>.exe from the build\ staging directory.
; Driven by installer\build.ps1; not normally invoked directly.
;
; Install design:
;   - Per-user install (no UAC prompt) at %LOCALAPPDATA%\Programs\WorkPulse
;   - Bundles Python 3.12 embedded distribution + all WorkPulse deps
;   - Runs setup.ps1 -Yes -SkipSecrets -SkipLaunch post-install
;   - Creates Start Menu shortcuts + a Startup-folder shortcut so the tray
;     launches at every logon
;   - User configures API key etc. from the dashboard Settings page after

#define MyAppName "WorkPulse"
#define MyAppPublisher "Kibuku"
#define MyAppURL "https://github.com/kibuku/workpulse"
#define MyAppExeName "WorkPulse.exe"

; AppVersion is passed in by build.ps1 (/DAppVersion=1.0.0).
; Default to "dev" if not provided so the script can be hand-compiled too.
#ifndef AppVersion
  #define AppVersion "dev"
#endif

[Setup]
AppId={{B6E2A1C4-7F3A-4E18-9A0B-7C2D5F8E4A11}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={userpf}\WorkPulse
DefaultGroupName=WorkPulse
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
OutputDir=dist
OutputBaseFilename=WorkPulseSetup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
SetupLogging=yes
UninstallDisplayName=WorkPulse {#AppVersion}
UninstallDisplayIcon={app}\python\pythonw.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "build\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\WorkPulse"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\scripts\tray.py"""; WorkingDir: "{app}"; Comment: "Launch WorkPulse tray app"
Name: "{group}\Open Dashboard"; Filename: "http://127.0.0.1:5700"; Comment: "Open WorkPulse dashboard in default browser"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\WorkPulse"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\scripts\tray.py"""; WorkingDir: "{app}"; Tasks: desktopicon
; Startup folder shortcut so tray launches at every logon
Name: "{userstartup}\WorkPulse"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\scripts\tray.py"""; WorkingDir: "{app}"; Comment: "Auto-launch WorkPulse at logon"

[Run]
; Post-install: run setup.ps1 in silent/installer mode.
; This generates config\identity.yaml, copies config.example.yaml to config.yaml,
; and registers the Startup shortcut (idempotent — already present from [Icons]).
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\setup.ps1"" -Yes -SkipSecrets -SkipLaunch -BundledPython ""{app}\python\python.exe"""; \
    WorkingDir: "{app}"; \
    StatusMsg: "Configuring WorkPulse..."; \
    Flags: runhidden waituntilterminated

; Optional: launch WorkPulse tray right after install
Filename: "{app}\python\pythonw.exe"; \
    Parameters: """{app}\scripts\tray.py"""; \
    WorkingDir: "{app}"; \
    Description: "Launch WorkPulse now"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Best-effort: kill any running WorkPulse tray + child processes before uninstall.
; (-ErrorAction SilentlyContinue so a missing process doesn't abort uninstall.)
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -Command ""Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '{app}\*' } | Stop-Process -Force -ErrorAction SilentlyContinue"""; \
    Flags: runhidden waituntilterminated

[UninstallDelete]
; Clean up runtime-generated files that aren't tracked by the installer's
; uninstall ledger but should still be removed.
; NOTE: We deliberately keep config\config.yaml, config\identity.yaml,
; config\secrets.json, logs\, and vault\ — that's the user's personal data
; and they may want to reinstall later. They live in {app}\config and
; {app}\logs so will be left behind. Uninstaller prompts in [Code] handle
; "want me to delete those too?" for users who really want a clean slate.
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\__pycache__"
Type: filesandordirs; Name: "{app}\scripts\__pycache__"

[Code]
function InitializeUninstall(): Boolean;
var
  Confirm: Integer;
begin
  Confirm := MsgBox(
    'Uninstall WorkPulse?' + #13#10#13#10 +
    'Your personal data — config, logs, vault — will NOT be deleted.' + #13#10 +
    'They stay in:'  + #13#10 +
    ExpandConstant('{app}\config') + #13#10 +
    ExpandConstant('{app}\logs') + #13#10#13#10 +
    'Delete those folders by hand if you want a clean slate.',
    mbConfirmation, MB_OKCANCEL
  );
  Result := (Confirm = IDOK);
end;
