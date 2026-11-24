# Builds dist\AETHER-Setup-<version>.exe with Inno Setup 6.
#   winget install JRSoftware.InnoSetup      (one time)
#   powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1
$ErrorActionPreference = "Stop"
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found. Install it with: winget install JRSoftware.InnoSetup" }
& $iscc "installer\aether.iss"
Write-Host "Installer written to dist\" -ForegroundColor Cyan
