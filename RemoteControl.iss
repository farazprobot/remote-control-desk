#define MyAppName "Remote Control Desk"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Remote Control Desk"
#define MyAppExeName "RemoteControlDesk.exe"

[Setup]
AppId={{E07DC0BD-C5F7-4D43-9CF1-6D9216A6DCE1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Remote Control Desk
DefaultGroupName={#MyAppName}
OutputDir=..\dist\installer
OutputBaseFilename=RemoteControlDeskSetup
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64

[Files]
Source: "..\dist\RemoteControlDesk.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Remote Control Desk"; Filename: "{app}\RemoteControlDesk.exe"
Name: "{commondesktop}\Remote Control Desk"; Filename: "{app}\RemoteControlDesk.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"