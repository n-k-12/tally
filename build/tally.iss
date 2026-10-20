; Inno Setup script for Tally. build.py passes AppVersion, SourceDir and IconFile.
; AppId must never change: it is how an installer recognises an older Tally to upgrade.

[Setup]
AppId={{8B2F6C1E-5D4A-4C1B-9E3F-7A6D2B1C0E94}
AppName=Tally
AppVersion={#AppVersion}
AppPublisher=Tally
AppPublisherURL=https://github.com/n-k-12/tally
DefaultDirName={localappdata}\Programs\Tally
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=TallySetup-{#AppVersion}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\Tally.exe
UninstallDisplayName=Tally
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no

[Tasks]
Name: "autostart"; Description: "Start Tally when Windows starts"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; files from an older version that this one no longer has
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\Tally"; Filename: "{app}\Tally.exe"
Name: "{autodesktop}\Tally"; Filename: "{app}\Tally.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Tally"; \
  ValueData: """{app}\Tally.exe"" --background"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\Tally.exe"; Description: "Open Tally"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; takes Tally's hooks out of Claude Code's settings and closes the app, giving the lamp back
Filename: "{app}\Tally.exe"; Parameters: "--uninstall"; RunOnceId: "TallyUninstall"; Flags: runhidden waituntilterminated

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  // close a running Tally so its files can be replaced
  if FileExists(ExpandConstant('{app}\Tally.exe')) then
    Exec(ExpandConstant('{app}\Tally.exe'), '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
