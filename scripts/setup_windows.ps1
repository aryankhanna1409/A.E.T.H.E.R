# AETHER setup for Windows 11. Run in PowerShell from the repo folder:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 [-Cuda]
param([switch]$Cuda)
$ErrorActionPreference = "Stop"

py -3.11 -m venv .venv
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
