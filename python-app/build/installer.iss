; Inno Setup 6 script — Flow2API Windows installer (Next → Next)
; Build: iscc installer.iss   (after build_release.bat stages files)
; Wizard asks for install dir + storage data dir.

#define MyAppName "Flow2API"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Flow2API"
#define MyAppExeName "Run Flow2API.bat"
#define MyAppAgent "Flow2API-Agent.exe"

; Stage dir produced by build_release.bat (relative to this .iss)
#define StageDir "..\dist\Flow2API-Release"

[Setup]
AppId={{A7C2E1B0-4F2D-4E91-9B8A-FLOW2API2026}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Flow2API
DefaultGroupName=Flow2API
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Flow2API-Setup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppAgent}
SetupIconFile=
InfoBeforeFile=AFTER_INSTALL.txt
DisableWelcomePage=no
AllowNoIcons=yes
UsedUserAreasWarning=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked
Name: "openreadme"; Description: "Show post-install notes after setup"; GroupDescription: "Additional options:"; Flags: checkedonce

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Flow2API"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Flow2API Admin"; Filename: "http://127.0.0.1:1994/admin"
Name: "{group}\Uninstall Flow2API"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Flow2API"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Flow2API now"; Flags: nowait postinstall skipifsilent unchecked
Filename: "{app}\AFTER_INSTALL.txt"; Description: "Open setup notes"; Flags: postinstall shellexec skipifsilent; Tasks: openreadme

[Code]
var
  StoragePage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  StoragePage := CreateInputDirPage(
    wpSelectDir,
    'Storage location',
    'Where should Flow2API store database, videos, and task outputs?',
    'Select a folder. It will be created if it does not exist.' + #13#10 +
    'Tip: use a drive with free space (SSD/HDD). App files and data can be on different disks.',
    False,
    'Flow2API-storage'
  );
  StoragePage.Add('Data / storage folder:');
  StoragePage.Values[0] := ExpandConstant('{localappdata}\Flow2API\storage');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  S: string;
begin
  Result := True;
  if CurPageID = StoragePage.ID then
  begin
    S := Trim(StoragePage.Values[0]);
    if S = '' then
    begin
      MsgBox('Please choose a storage folder.', mbError, MB_OK);
      Result := False;
      exit;
    end;
    // Disallow storing writeable data inside a possibly locked Program Files tree without notice
    // (allowed, but warn if under {app} which may be elevated/read-only).
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  PathFile, StoragePath: string;
begin
  if CurStep = ssPostInstall then
  begin
    StoragePath := Trim(StoragePage.Values[0]);
    if StoragePath = '' then
      StoragePath := ExpandConstant('{localappdata}\Flow2API\storage');
    if not DirExists(StoragePath) then
      ForceDirectories(StoragePath);
    PathFile := ExpandConstant('{app}\storage_path.txt');
    // one line: absolute path, UTF-8 without BOM for simple bat parser
    SaveStringToFile(PathFile, StoragePath + #13#10, False);
  end;
end;
