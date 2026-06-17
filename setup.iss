; setup.iss — Inno Setup script for MAI Trading Bot
; Requires Inno Setup 6.x: https://jrsoftware.org/isinfo.php
;
; Build steps:
;   1. pyinstaller build.spec          (creates dist/MAI_Trading_Bot.exe)
;   2. Open this file in Inno Setup Compiler and click Build

#define AppName "MAI Trading Bot"
#define AppVersion "1.0.0"
#define AppPublisher "Mitchell Attempted Investing"
#define AppURL "https://github.com/Arqua/Agentic-Trading"
#define AppExeName "MAI_Trading_Bot.exe"

[Setup]
AppId={{B2F4A6C1-9D3E-4B7F-A8E2-1C5D7F9B3E6A}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
; LicenseFile=LICENSE.txt
OutputDir=installer
OutputBaseFilename=MAI_Trading_Bot_Setup_v{#AppVersion}
; SetupIconFile=assets\mai_icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";  Description: "{cm:CreateDesktopIcon}";  GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupicon";  Description: "Start automatically with Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}";            Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}";  Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}";    Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}";      Filename: "{app}\{#AppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{userappdata}\MAI"
