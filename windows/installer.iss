; Inno Setup script: builds PeakSetup.exe from the PyInstaller folder.
[Setup]
AppName=Peak Download Manager
AppVersion=1.0.0
AppPublisher=Peak
DefaultDirName={localappdata}\Programs\Peak Download Manager
DefaultGroupName=Peak Download Manager
PrivilegesRequired=lowest
OutputBaseFilename=PeakSetup
SetupIconFile=peak.ico
UninstallDisplayIcon={app}\Peak Download Manager.exe
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Files]
Source: "dist\Peak Download Manager\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Peak Download Manager"; Filename: "{app}\Peak Download Manager.exe"
Name: "{autodesktop}\Peak Download Manager"; Filename: "{app}\Peak Download Manager.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"
Name: "startup"; Description: "Start Peak when Windows starts"
Name: "torrents"; Description: "Open magnet links and .torrent files with Peak"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Peak"; \
  ValueData: """{app}\Peak Download Manager.exe"" --hidden"; Tasks: startup; Flags: uninsdeletevalue

; magnet: links and .torrent files -> Peak (opens the New download box first)
Root: HKCU; Subkey: "Software\Classes\magnet"; ValueType: string; ValueData: "URL:Magnet link"; Tasks: torrents; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\magnet"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""; Tasks: torrents
Root: HKCU; Subkey: "Software\Classes\magnet\DefaultIcon"; ValueType: string; ValueData: """{app}\Peak Download Manager.exe"",0"; Tasks: torrents
Root: HKCU; Subkey: "Software\Classes\magnet\shell\open\command"; ValueType: string; ValueData: """{app}\Peak Download Manager.exe"" ""%1"""; Tasks: torrents
Root: HKCU; Subkey: "Software\Classes\.torrent"; ValueType: string; ValueData: "Peak.Torrent"; Tasks: torrents; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\Peak.Torrent"; ValueType: string; ValueData: "Torrent file"; Tasks: torrents; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Peak.Torrent\shell\open\command"; ValueType: string; ValueData: """{app}\Peak Download Manager.exe"" ""%1"""; Tasks: torrents

[Run]
Filename: "{app}\Peak Download Manager.exe"; Description: "Open Peak"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "taskkill"; Parameters: "/IM ""Peak Download Manager.exe"" /F /T"; Flags: runhidden; RunOnceId: "KillPeak"
