[Setup]
AppId=CloudflaredQuickTunnelTray
AppName=Cloudflare Quick Tunnel Tray
AppVersion={#TrayVersion}
AppPublisher=robit-man
AppPublisherURL=https://github.com/robit-man/cloudflared-quick-tunnel-tray
DefaultDirName={localappdata}\Programs\Cloudflare Quick Tunnel Tray
DefaultGroupName=Cloudflare Quick Tunnel Tray
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=quick-tunnel-tray-windows-amd64
SolidCompression=yes
UninstallDisplayIcon={app}\QuickTunnelTray.exe

[Files]
Source: "..\dist\QuickTunnelTray\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Cloudflare Quick Tunnel Tray"; Filename: "{app}\QuickTunnelTray.exe"

[Run]
Filename: "{app}\QuickTunnelTray.exe"; Description: "Open Cloudflare Quick Tunnel Tray"; Flags: nowait postinstall skipifsilent
