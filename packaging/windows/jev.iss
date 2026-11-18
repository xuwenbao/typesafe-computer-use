; Per-user installer for jev. One run leaves jev.exe and its models ready to use.
; Build it with scripts\build-helper.ps1, which passes /DVersion. No admin rights.

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
    WizardForm.FinishedLabel.Height := WizardForm.ClientHeight - WizardForm.FinishedLabel.Top - ScaleY(16);
    WizardForm.FinishedLabel.Caption :=
      'Paste this into the agent''s MCP config. The same text is in mcp-config.json next to jev.exe:' + #13#10 + #13#10 +
      McpConfig();
  end;
end;
