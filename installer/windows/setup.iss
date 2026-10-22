; Inno Setup script for the Flight Monitor installer (Windows).
; Build:  iscc installer\windows\setup.iss /DAppVersion=0.2.0
; Input:  dist\flight-monitor\  (pyinstaller flight-monitor.spec)
; Output: installer-output\flight-monitor-0.2.0-windows-setup.exe
;
; Installs per user (no admin). The "start with Windows" task (on by default)
; launches the app minimized to the tray at login, where it runs the monitor on
; its schedule. User data stays in %APPDATA%\flight-monitor, also on uninstall.

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
; Run as user -- no admin required for autopf on modern Windows
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\flight-monitor.exe
; The app lives in the tray: [Code] asks it to quit (flight-monitor.exe --quit)
; before files are replaced or removed; the Restart Manager is the fallback.
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
; An upgrade must not keep libraries the new version no longer ships.
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
    Sleep(2000);  { let it finish shutting down and release its files }
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
