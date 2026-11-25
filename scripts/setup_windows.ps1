# AETHER setup for Windows 11. Run in PowerShell from the repo folder:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 [-Cuda]
param([switch]$Cuda)
$ErrorActionPreference = "Stop"

# AETHER needs Python 3.11 (3.12 also works): the prebuilt GPU model runner and the voice packages are published
# for those versions. Newer Pythons (3.13, 3.14, 3.15...) can stay installed; the py launcher picks the right one.
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
  Write-Host "The 'py' launcher isn't installed. Install Python 3.11 from" -ForegroundColor Red
  Write-Host "  https://www.python.org/downloads/release/python-3119/  (Windows installer, 64-bit; keep 'py launcher' ticked)"
  exit 1
}
$PyVer = $null
foreach ($v in @("3.11", "3.12")) {
  & py "-$v" -c "import sys" 2>$null
  if ($LASTEXITCODE -eq 0) { $PyVer = $v; break }
}
if (-not $PyVer) {
  $have = (& py --version 2>&1) -join " "
  Write-Host "AETHER needs Python 3.11, but this PC only has: $have" -ForegroundColor Red
  Write-Host "Your current Python can stay. Install 3.11 next to it (it won't replace it):"
  Write-Host "  https://www.python.org/downloads/release/python-3119/  -> 'Windows installer (64-bit)'"
  Write-Host "  In the installer keep 'py launcher' ticked. You do NOT need 'Add to PATH'."
  Write-Host "Then close and reopen PowerShell, check with 'py -3.11 --version', and run this script again."
  Write-Host "(All installed versions: 'py -0')"
  exit 1
}
Write-Host "Using Python $PyVer" -ForegroundColor Cyan

# An old .venv made with another Python version is rebuilt.
if (Test-Path .venv\Scripts\python.exe) {
  $venvVer = & .\.venv\Scripts\python -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
  if ($venvVer -ne $PyVer) {
    Write-Host "Rebuilding .venv (it was made with Python $venvVer)" -ForegroundColor Yellow
    Remove-Item -Recurse -Force .venv
  }
}
if (-not (Test-Path .venv\Scripts\python.exe)) { & py "-$PyVer" -m venv .venv }
.\.venv\Scripts\python -m pip install --upgrade pip

if ($Cuda) {
  # Prebuilt CUDA 12.4 wheel for NVIDIA GPUs such as the RTX 3050 (no compiler needed).
  # Needs a recent NVIDIA driver (550 or newer): update it from the NVIDIA app if the GPU is not used.
  .\.venv\Scripts\pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124
} else {
  .\.venv\Scripts\pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
}
.\.venv\Scripts\pip install -e ".[all]" huggingface_hub

if (-not (Test-Path aether.toml)) { Copy-Item aether.example.toml aether.toml }
New-Item -ItemType Directory -Force "$env:USERPROFILE\Documents\AETHER\output" | Out-Null

Write-Host "`nNext:" -ForegroundColor Cyan
Write-Host "  1. .\.venv\Scripts\python scripts\get_model.py      (one-time model download)"
Write-Host "  2. Edit aether.toml: model_path and your folders"
Write-Host "  3. .\.venv\Scripts\aether-server                    (desktop UI)  or  .\.venv\Scripts\aether  (terminal)"
