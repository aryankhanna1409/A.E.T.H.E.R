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
$Pip = ".\.venv\Scripts\python.exe"
# From here on, exit codes are checked by hand. ("Stop" would turn harmless pip warnings on stderr into errors.)
$ErrorActionPreference = "Continue"
& $Pip -m pip install --upgrade pip

# 1. Everything except the model runner. This never needs a compiler, and a model-runner problem can't block it.
Write-Host "`nInstalling AETHER and its packages..." -ForegroundColor Cyan
& $Pip -m pip install -e ".[all]" huggingface_hub
if ($LASTEXITCODE -ne 0) {
  Write-Host "Installing the packages failed (see the error above). Check your internet connection and run this script again." -ForegroundColor Red
  exit 1
}

# 2. The model runner (llama-cpp-python) from PREBUILT wheels only. Without --only-binary, pip falls back to
#    compiling it from source, which needs Visual Studio's C++ tools ("nmake ... not found"). We never do that.
$Idx = "https://abetlen.github.io/llama-cpp-python/whl"
$Ver = "0.3.4"  # the newest release with prebuilt Windows wheels for CUDA 12.4 and CPU
& $Pip -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('llama_cpp') else 1)"
$have = ($LASTEXITCODE -eq 0)
function Install-Llama($flavour, $pin) {
  $spec = if ($pin) { "llama-cpp-python==$Ver" } else { "llama-cpp-python" }
  Write-Host "Trying the prebuilt $flavour model runner ($spec)..." -ForegroundColor Cyan
  $extra = if ($have) { @("--force-reinstall", "--no-deps") } else { @() }  # swap a CPU build for a GPU one in place
  & $Pip -m pip install $spec --only-binary llama-cpp-python --prefer-binary --extra-index-url "$Idx/$flavour" @extra | Out-Host  # Out-Host: keep pip's text out of the return value
  return ($LASTEXITCODE -eq 0)
}
$gpu = $false
if ($have) {
  & $Pip -c "import llama_cpp, sys; sys.exit(0 if llama_cpp.llama_supports_gpu_offload() else 1)"
  $gpu = ($LASTEXITCODE -eq 0)
}
$ok = $have -and ($gpu -or -not $Cuda)
if ($ok) { Write-Host "The model runner is already installed$(if ($gpu) { ' (GPU build)' })." -ForegroundColor Green }
if (-not $ok -and $Cuda) {
  # Needs an NVIDIA driver 550 or newer: update it from the NVIDIA app if the GPU isn't used.
  $ok = (Install-Llama "cu124" $true) -or (Install-Llama "cu124" $false) -or (Install-Llama "cu122" $false)
  if (-not $ok) {
    Write-Host "No prebuilt GPU runner fits this PC; using the CPU one (slower, still works)." -ForegroundColor Yellow
    $ok = $have  # keep the CPU build that is already there
  }
}
if (-not $ok) { $ok = (Install-Llama "cpu" $true) -or (Install-Llama "cpu" $false) }
if (-not $ok) {
  Write-Host "`nCouldn't install a prebuilt model runner. Everything else is installed." -ForegroundColor Red
  Write-Host "Run '.\.venv\Scripts\python scripts\doctor.py' and send its output; the rest of AETHER (and the tests) work without it."
}

if (-not (Test-Path aether.toml)) { Copy-Item aether.example.toml aether.toml }
New-Item -ItemType Directory -Force "$env:USERPROFILE\Documents\AETHER\output" | Out-Null

Write-Host "`nNext:" -ForegroundColor Cyan
Write-Host "  1. .\.venv\Scripts\python scripts\get_model.py      (one-time model download)"
Write-Host "  2. Edit aether.toml: model_path and your folders"
Write-Host "  3. .\.venv\Scripts\aether-server                    (desktop UI)  or  .\.venv\Scripts\aether  (terminal)"
