; Inno Setup script for AETHER (https://jrsoftware.org/isinfo.php)
; Build:  powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1
; The installer copies AETHER, creates its Python environment with the right
; llama.cpp wheel (CUDA or CPU), optionally downloads the model, and adds
; Start menu shortcuts. Your memories live in %LOCALAPPDATA%\AETHER and are
; kept on uninstall unless you tick the box.

#define AppName "AETHER"
#define AppVersion "0.11.0"

[Setup]
AppId={{6A0E2B7C-4F1D-4C59-9B8E-AE7E3A11E0F1}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Aryan Khanna
DefaultDirName={localappdata}\Programs\AETHER
DefaultGroupName=AETHER
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=AETHER-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
UninstallDisplayName=AETHER

[Tasks]
Name: "cuda"; Description: "Use my NVIDIA GPU (CUDA 12.4; needs driver 550 or newer)"; GroupDescription: "Language model:"
Name: "model"; Description: "Download the recommended model now (Qwen2.5 7B, about 4.7 GB)"; GroupDescription: "Language model:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\aether\*"; DestDir: "{app}\aether"; Flags: recursesubdirs; Excludes: "__pycache__,*.pyc"
Source: "..\scripts\*"; DestDir: "{app}\scripts"; Flags: recursesubdirs
Source: "..\docs\*"; DestDir: "{app}\docs"; Flags: recursesubdirs
Source: "..\pyproject.toml"; DestDir: "{app}"
Source: "..\README.md"; DestDir: "{app}"
Source: "..\aether.example.toml"; DestDir: "{app}"
Source: "..\aether.example.toml"; DestDir: "{app}"; DestName: "aether.toml"; Flags: onlyifdoesntexist

[Run]
Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\setup_windows.ps1"" -Cuda"; WorkingDir: "{app}"; StatusMsg: "Creating the Python environment (CUDA)... this takes a few minutes"; Tasks: cuda; Flags: waituntilterminated
Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\setup_windows.ps1"""; WorkingDir: "{app}"; StatusMsg: "Creating the Python environment (CPU)... this takes a few minutes"; Tasks: not cuda; Flags: waituntilterminated
Filename: "{app}\.venv\Scripts\python.exe"; Parameters: """{app}\scripts\get_model.py"""; WorkingDir: "{app}"; StatusMsg: "Downloading the language model (about 4.7 GB)..."; Tasks: model; Flags: waituntilterminated
Filename: "{app}\.venv\Scripts\aether-server.exe"; Description: "Start AETHER now"; Flags: postinstall nowait skipifsilent

[Icons]
Name: "{group}\AETHER"; Filename: "{app}\.venv\Scripts\aether-server.exe"; WorkingDir: "{app}"
Name: "{group}\AETHER (terminal)"; Filename: "{app}\.venv\Scripts\aether.exe"; WorkingDir: "{app}"
Name: "{group}\AETHER voice"; Filename: "{app}\.venv\Scripts\aether-voice.exe"; WorkingDir: "{app}"
Name: "{group}\Edit settings"; Filename: "notepad.exe"; Parameters: """{app}\aether.toml"""
Name: "{group}\Uninstall AETHER"; Filename: "{uninstallexe}"
Name: "{autodesktop}\AETHER"; Filename: "{app}\.venv\Scripts\aether-server.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"

[Code]
function InitializeSetup(): Boolean;
var Code: Integer;
begin
  Result := True;
  if not Exec('py', '-3.11 --version', '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
  begin
    MsgBox('AETHER needs Python 3.11. Install it from python.org (tick "Add to PATH" and the py launcher), then run this installer again.', mbError, MB_OK);
    Result := False;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and
     (MsgBox('Also delete your AETHER memories, preferences and logs (%LOCALAPPDATA%\AETHER)? This cannot be undone.',
             mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
    DelTree(ExpandConstant('{localappdata}\AETHER'), True, True, True);
end;
