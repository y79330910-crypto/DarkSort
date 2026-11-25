#ifndef AppVersion
  #error Pass the DarkSort release version with /DAppVersion.
#endif

[Setup]
AppId=DarkSort
AppName=DarkSort
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\DarkSort
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=DarkSort-{#AppVersion}-Setup
SetupIconFile=..\src\darksort\assets\darksort.ico
WizardStyle=modern dynamic
WizardSmallImageFile=darksort-icon.png
WizardSmallImageFileDynamicDark=darksort-icon.png
UninstallDisplayIcon={app}\DarkSort.exe
RestartApplications=no

[Languages]
Name: "zh_CN"; MessagesFile: "languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"

[Files]
Source: "..\dist\releases\{#AppVersion}\DarkSort\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\DarkSort"; Filename: "{app}\DarkSort.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\DarkSort"; Filename: "{app}\DarkSort.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\DarkSort.exe"; Description: "{cm:LaunchProgram,DarkSort}"; Flags: nowait postinstall skipifsilent
