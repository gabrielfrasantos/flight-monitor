#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{0D477BBB-DB3C-457C-A540-28FEF76F094D}
AppName=Flight Monitor
AppVersion={#AppVersion}
AppPublisher=Gabriel Santos
AppPublisherURL=https://github.com/gabrielfrasantos/flight-monitor
AppSupportURL=https://github.com/gabrielfrasantos/flight-monitor/issues
AppUpdatesURL=https://github.com/gabrielfrasantos/flight-monitor/releases
DefaultDirName={autopf}\Flight Monitor
DefaultGroupName=Flight Monitor
AllowNoIcons=yes
OutputDir=..\..\installer-output
OutputBaseFilename=flight-monitor-{#AppVersion}-windows-setup
SetupIconFile=..\..\assets\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\flight-monitor.exe
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startupicon"; Description: "Start Flight Monitor with Windows (runs in the tray and keeps the schedule)"; \
      GroupDescription: "Startup options:"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
      GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\..\dist\flight-monitor\*"; DestDir: "{app}"; \
        Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Flight Monitor";                       Filename: "{app}\flight-monitor.exe"
Name: "{group}\{cm:UninstallProgram,Flight Monitor}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Flight Monitor";                 Filename: "{app}\flight-monitor.exe"; \
      Tasks: desktopicon
Name: "{userstartup}\Flight Monitor";                 Filename: "{app}\flight-monitor.exe"; \
      Parameters: "--minimized"; Tasks: startupicon

[Run]
Filename: "{app}\flight-monitor.exe"; \
          Description: "{cm:LaunchProgram,Flight Monitor}"; \
          Flags: nowait postinstall skipifsilent

[Code]
procedure QuitRunningApp();
var
  ResultCode: Integer;
begin
  if FileExists(ExpandConstant('{app}\flight-monitor.exe')) then
  begin
    Exec(ExpandConstant('{app}\flight-monitor.exe'), '--quit', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
    Sleep(2000);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  QuitRunningApp();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  QuitRunningApp();
  Result := True;
end;
