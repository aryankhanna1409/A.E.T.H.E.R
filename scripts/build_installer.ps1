# Builds dist\AETHER-Setup-<version>.exe with Inno Setup 6.
#   winget install JRSoftware.InnoSetup      (one time)
#   powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1
# The version comes from pyproject.toml, so the installer always matches the Mark being built.
# Jenkins runs this same script on the Windows agent (see Jenkinsfile and docs\CI_CD.md).
param([string]$Version = "")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not $Version) {
    $line = Select-String -Path "pyproject.toml" -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if (-not $line) { throw "Could not read the version from pyproject.toml" }
    $Version = $line.Matches[0].Groups[1].Value
}
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found. Install it with: winget install JRSoftware.InnoSetup" }
& $iscc "/DAppVersion=$Version" "installer\aether.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
Write-Host "Installer written to dist\AETHER-Setup-$Version.exe" -ForegroundColor Cyan
