#ifndef Product
  #define Product "institution"
#endif
#ifndef Role
  #define Role "member"
#endif
#ifndef AppLabel
  #define AppLabel "WorkPulse Institution"
#endif
#ifndef AppSlug
  #define AppSlug "WorkPulseInstitution"
#endif
#ifndef AppGuid
  #define AppGuid "B6E2A1C4-7F3A-4E18-9A0B-7C2D5F8E4A11"
#endif
#define MyAppName AppLabel
#define MyAppPublisher "Njiani"
#ifndef AppVersion
  #define AppVersion "dev"
#endif

[Setup]
AppId={{{#AppGuid}}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#AppSlug}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
OutputDir=..\..\release-artifacts
OutputBaseFilename={#AppSlug}Setup-{#AppVersion}-Windows
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
UninstallDisplayIcon={app}\WorkPulse.exe

[Files]
Source: "build\WorkPulse\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\WorkPulse.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\WorkPulse.exe"; WorkingDir: "{app}"
; Auto-start the tray (which serves the dashboard) at logon. Windows has no
; dashboard scheduled task — the tray owns the server — so without this the
; dashboard only runs until the next sign-out.
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\WorkPulse.exe"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\WorkPulse.exe"; Parameters: "--cli install --product {#Product} --role {#Role}"; WorkingDir: "{app}"; StatusMsg: "Preparing {#MyAppName}..."; Flags: runhidden waituntilterminated
Filename: "{app}\WorkPulse.exe"; WorkingDir: "{app}"; Description: "Open {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\WorkPulse.exe"; Parameters: "--cli uninstall"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated skipifdoesntexist
