#define MyAppName "WorkPulse"
#define MyAppPublisher "Njiani"
#ifndef AppVersion
  #define AppVersion "dev"
#endif

[Setup]
AppId={{B6E2A1C4-7F3A-4E18-9A0B-7C2D5F8E4A11}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\WorkPulse
DefaultGroupName=WorkPulse
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
OutputDir=..\..\release-artifacts
OutputBaseFilename=WorkPulseSetup-{#AppVersion}-Windows
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
UninstallDisplayIcon={app}\WorkPulse.exe

[Files]
Source: "build\WorkPulse\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\WorkPulse"; Filename: "{app}\WorkPulse.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\WorkPulse"; Filename: "{app}\WorkPulse.exe"; WorkingDir: "{app}"
Name: "{group}\Uninstall WorkPulse"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\WorkPulse.exe"; Parameters: "--cli install"; WorkingDir: "{app}"; StatusMsg: "Preparing WorkPulse..."; Flags: runhidden waituntilterminated
Filename: "{app}\WorkPulse.exe"; WorkingDir: "{app}"; Description: "Open WorkPulse"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\WorkPulse.exe"; Parameters: "--cli uninstall"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated skipifdoesntexist
