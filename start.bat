@echo off
rem Double-click to start AETHER: the server, the tray icon, and Nova in the corner of your screen.
rem No window stays open. The app opens by itself once the model has loaded (a minute or two).
rem The first start also turns on "Start with Windows", so after a restart AETHER is simply there.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo AETHER isn't set up yet. Run this first, in PowerShell in this folder:
  echo   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Cuda
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import pystray, pynput" 2>nul
if errorlevel 1 (
  echo The tray packages are missing, installing them once...
  ".venv\Scripts\python.exe" -m pip install -q pystray pillow pynput psutil
)
".venv\Scripts\python.exe" -c "import pystray, pynput" 2>nul
if errorlevel 1 (
  rem Still no tray: show Nova, then run the server in this window. The app opens when the model is ready.
  start "" ".venv\Scripts\pythonw.exe" -m aether.interface.nova
  ".venv\Scripts\python.exe" -m aether.interface.server
  pause
  exit /b
)
start "" ".venv\Scripts\pythonw.exe" -m aether.interface.tray --open
echo AETHER is starting. Nova appears in the bottom-right corner, and the app opens when the model is ready.
echo If something goes wrong, the details are in %LOCALAPPDATA%\AETHER\logs\server.log
timeout /t 4 >nul
