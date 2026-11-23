; Per-user installer for jev. One run leaves jev.exe and its models ready to use.
; Build it with scripts\build-helper.ps1, which passes /DVersion. No admin rights.
; Keep this file UTF-8 with a BOM: without it ISCC reads the Chinese text in the ANSI code page.

#ifndef Version
  #define Version "0.0.0"
#endif
; Absolute paths, passed by the build script. A relative path that still contains ".." can exceed
; the 260-character limit on a long checkout path before Windows normalizes it.
#ifndef DistDir
  #define DistDir "..\..\build\helper\dist\jev"
#endif
#ifndef ModelsDir
  #define ModelsDir "..\..\build\helper\models"
#endif
#ifndef SkillsDir
  #define SkillsDir "..\..\skills\jev"
#endif
#ifndef PromptFile
  #define PromptFile "agent-prompt.md"
#endif
#ifndef GuideFile
  #define GuideFile "如何接入智能体.txt"
#endif

#define AppExe "jev.exe"

[Setup]
AppId={{7C4E9A21-5B18-4D6F-A3E2-91F0C8B64D27}}
AppName=jev
AppVersion={#Version}
AppPublisher=jev
VersionInfoVersion={#Version}
DefaultDirName={localappdata}\Programs\jev
DisableDirPage=yes
DisableProgramGroupPage=yes
UsePreviousAppDir=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\..\dist
OutputBaseFilename=jev-setup-{#Version}
Compression=lzma2/max
SolidCompression=no
DiskSpanning=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter={#AppExe}
RestartApplications=no
UninstallDisplayName=jev

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ModelsDir}\*"; DestDir: "{app}\models"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SkillsDir}\*"; DestDir: "{app}\skills\jev"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PromptFile}"; DestDir: "{app}"; DestName: "agent-prompt.md"; Flags: ignoreversion
Source: "{#GuideFile}"; DestDir: "{app}"; DestName: "如何接入智能体.txt"; Flags: ignoreversion

[Run]
Filename: "{win}\notepad.exe"; Parameters: """{app}\如何接入智能体.txt"""; Description: "打开接入说明（教你把初始化提示词复制给智能体）"; Flags: postinstall nowait skipifsilent

[Code]
function McpConfig(): String;
var
  Path: String;
begin
  Path := ExpandConstant('{app}\{#AppExe}');
  StringChangeEx(Path, '\', '\\', True);
  Result :=
    '{' + #13#10 +
    '  "mcpServers": {' + #13#10 +
    '    "jev": {' + #13#10 +
    '      "command": "' + Path + '",' + #13#10 +
    '      "args": ["mcp"]' + #13#10 +
    '    }' + #13#10 +
    '  }' + #13#10 +
    '}';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SaveStringToFile(ExpandConstant('{app}\mcp-config.json'), McpConfig(), False);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
  begin
    WizardForm.FinishedLabel.AutoSize := False;
    WizardForm.FinishedLabel.WordWrap := True;
    WizardForm.FinishedLabel.Caption :=
      '勾选下面的「打开接入说明」，点完成后会打开一份说明，教你把初始化提示词复制给智能体。' + #13#10 + #13#10 +
      McpConfig();
    { Keep the checkbox above the Finish button. ClientHeight includes that button and hid the list. }
    WizardForm.RunList.Height := ScaleY(44);
    WizardForm.RunList.Top := WizardForm.CancelButton.Top - WizardForm.RunList.Height - ScaleY(12);
    WizardForm.FinishedLabel.Height := WizardForm.RunList.Top - WizardForm.FinishedLabel.Top - ScaleY(8);
  end;
end;
