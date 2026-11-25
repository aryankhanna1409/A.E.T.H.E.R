@echo off
rem Double-click to start AETHER: the tray icon (Ctrl+Alt+J to talk) and the app in your browser.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo AETHER isn't set up yet. Run this first, in PowerShell in this folder:
  echo   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Cuda
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import pystray, pynput" 2>nul
if errorlevel 1 (
  rem No tray packages: run the server in this window and open the app.
  ".venv\Scripts\aether-server.exe"
) else (
  start "" ".venv\Scripts\pythonw.exe" -m aether.interface.tray
  timeout /t 3 >nul
  echo AETHER is starting in the tray. The first start loads the model and takes about a minute.
  echo Press Ctrl+Alt+J anywhere to talk, or open http://127.0.0.1:8765
  timeout /t 5 >nul
  start "" http://127.0.0.1:8765
)
