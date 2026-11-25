"""Open and close any app on the PC by name, plus folders, Windows settings pages, screenshots and AETHER's own windows.

"open spotify" has to work whether Spotify came from its installer or the Microsoft Store, so an app name is
looked up in this order:
  1. [security].apps in aether.toml (your own names and commands)
  2. built-in Windows names (file explorer, task manager, settings, paint, word, excel...)
  3. your folders (downloads, documents, desktop, pictures...)
  4. Start menu shortcuts (every installed desktop app has one)
  5. Microsoft Store apps (PowerShell Get-StartApps, cached for an hour)
  6. programs on PATH
Every launch still goes through the policy engine as app.open: it runs without asking only when you said
"open <that app>" yourself this turn (security.run_when_asked).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..core.logging import log
from .registry import Tool

ALIASES = {
    "file explorer": "explorer.exe", "explorer": "explorer.exe", "files": "explorer.exe", "this pc": "explorer.exe",
    "my computer": "explorer.exe", "task manager": "taskmgr.exe", "control panel": "control.exe",
    "settings": "ms-settings:", "windows settings": "ms-settings:", "pc settings": "ms-settings:",
    "bluetooth settings": "ms-settings:bluetooth", "bluetooth": "ms-settings:bluetooth",
    "wifi settings": "ms-settings:network-wifi", "wi-fi settings": "ms-settings:network-wifi", "wifi": "ms-settings:network-wifi",
    "display settings": "ms-settings:display", "sound settings": "ms-settings:sound", "battery settings": "ms-settings:batterysaver",
    "update settings": "ms-settings:windowsupdate", "windows update": "ms-settings:windowsupdate",
    "apps settings": "ms-settings:appsfeatures", "notification settings": "ms-settings:notifications",
    "command prompt": "cmd.exe", "cmd": "cmd.exe", "terminal": "wt.exe", "windows terminal": "wt.exe",
    "powershell": "powershell.exe", "paint": "mspaint.exe", "notepad": "notepad.exe", "calculator": "calc.exe",
    "calc": "calc.exe", "snipping tool": "ms-screenclip:", "clock": "ms-clock:", "alarms": "ms-clock:",
    "camera": "microsoft.windows.camera:", "store": "ms-windows-store:", "microsoft store": "ms-windows-store:",
    "photos": "ms-photos:", "mail app": "outlookmail:", "calendar app": "outlookcal:", "maps app": "bingmaps:",
    "word": "winword.exe", "microsoft word": "winword.exe", "excel": "excel.exe", "microsoft excel": "excel.exe",
    "powerpoint": "powerpnt.exe", "microsoft powerpoint": "powerpnt.exe", "onenote": "onenote.exe",
    "vs code": "code", "vscode": "code", "visual studio code": "code", "code": "code",
    "edge": "msedge.exe", "microsoft edge": "msedge.exe", "chrome": "chrome.exe", "google chrome": "chrome.exe",
    "firefox": "firefox.exe", "brave": "brave.exe", "registry editor": "regedit.exe", "device manager": "devmgmt.msc",
    "disk management": "diskmgmt.msc", "recycle bin": "shell:RecycleBinFolder", "startup apps": "ms-settings:startupapps",
}
FOLDERS = {
    "downloads": "shell:Downloads", "download": "shell:Downloads", "documents": "shell:Personal",
    "docs": "shell:Personal", "desktop": "shell:Desktop", "pictures": "shell:My Pictures", "photos folder": "shell:My Pictures",
    "music": "shell:My Music", "videos": "shell:My Video", "home": str(Path.home()), "user folder": str(Path.home()),
    "screenshots": "shell:Screenshots", "onedrive": "shell:OneDrive", "recent": "shell:Recent",
    "startup": "shell:Startup", "appdata": "shell:AppData", "temp": "%TEMP%",
}
# Never close these: Windows itself, or AETHER (which would end the conversation).
PROTECTED = {"explorer", "csrss", "winlogon", "wininit", "services", "lsass", "svchost", "system", "dwm", "smss",
             "python", "pythonw", "aether", "aether-tray", "aether-server"}
# What a few apps are called as processes.
PROCESS = {"chrome": "chrome", "google chrome": "chrome", "edge": "msedge", "microsoft edge": "msedge",
           "vs code": "code", "vscode": "code", "visual studio code": "code", "word": "winword", "excel": "excel",
           "powerpoint": "powerpnt", "file explorer": "explorer", "task manager": "taskmgr", "calculator": "calculatorapp",
           "notepad": "notepad", "spotify": "spotify", "discord": "discord", "whatsapp": "whatsapp", "teams": "ms-teams",
           "steam": "steam", "obs": "obs64", "paint": "mspaint", "settings": "systemsettings", "terminal": "windowsterminal"}


def norm(name: str) -> str:
    n = re.sub(r"[^a-z0-9+.# -]", "", name.lower()).strip()
    n = re.sub(r"^(?:the|my)\s+", "", n)
    n = re.sub(r"\s+(?:app|application|program|folder|window)$", "", n)
    return re.sub(r"\s+", " ", n).strip()


@dataclass
class Launch:
    name: str        # what to call it in the reply
    target: str      # what to start
    kind: str        # exe | uri | shortcut | store | folder | path


def start_menu_dirs() -> list[Path]:
    dirs = []
    for env in ("ProgramData", "APPDATA"):
        base = os.environ.get(env)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [d for d in dirs if d.is_dir()]


def best_match(want: str, names: dict[str, str], exact: bool = False) -> str | None:
    """The best entry of {display name: target} for a spoken app name: exact, then starts-with, then all words."""
    w = norm(want)
    if not w:
        return None
    low = {norm(k): v for k, v in names.items()}
    if w in low:
        return low[w]
    if exact:
        return None
    words = w.split()
    starts = [k for k in low if k.startswith(w + " ") or k.startswith(w)]
    if starts:
        return low[min(starts, key=len)]
    every = [k for k in low if all(re.search(rf"\b{re.escape(x)}", k) for x in words)]
    if every:
        return low[min(every, key=len)]
    squashed = w.replace(" ", "")
    joined = [k for k in low if k.replace(" ", "") == squashed]
    return low[joined[0]] if joined else None


class Apps:
    def __init__(self, configured: dict[str, str] | None = None, any_app: bool = True):
        self.configured = {norm(k): v for k, v in (configured or {}).items()}
        self.any_app = any_app
        self._store: tuple[float, dict[str, str]] = (0.0, {})
        self._shortcuts: tuple[float, dict[str, str]] = (0.0, {})
        self.launcher = self._launch   # swappable in tests
        self.windows = sys.platform == "win32"

    # ---- lookup
    def shortcuts(self) -> dict[str, str]:
        t, cache = self._shortcuts
        if time.time() - t < 600 and cache:
            return cache
        found: dict[str, str] = {}
        for d in start_menu_dirs():
            for p in d.rglob("*.lnk"):
                stem = p.stem
                if re.search(r"uninstall|readme|help|documentation|release notes|website", stem, re.I):
                    continue
                found.setdefault(stem, str(p))
        self._shortcuts = (time.time(), found)
        return found

    def store_apps(self) -> dict[str, str]:
        t, cache = self._store
        if time.time() - t < 3600 and cache:
            return cache
        if not self.windows:
            return {}
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                  "Get-StartApps | ForEach-Object { $_.Name + \"`t\" + $_.AppID }"],
                                 capture_output=True, text=True, timeout=20, creationflags=0x08000000).stdout
            apps = dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)
        except Exception as e:
            log.info("Get-StartApps unavailable: %s", e)
            apps = {}
        self._store = (time.time(), apps)
        return apps

    def candidates(self, name: str, exact: bool = False):
        """Every way to open `name`, best first. exact=True only accepts an app whose name is exactly that (so
        "open spotify" opens the Spotify app if it's installed, and the website if not)."""
        n = norm(name)
        if not n:
            return
        if n in self.configured:
            yield Launch(name, self.configured[n], "exe")
        if not self.any_app:
            return
        if n in ALIASES:
            target = ALIASES[n]
            yield Launch(name, target, "folder" if target.startswith("shell:") else "uri" if ":" in target else "exe")
        folder = FOLDERS.get(n) or FOLDERS.get(n.removesuffix(" folder"))
        if folder:
            yield Launch(f"your {n.removesuffix(' folder')} folder", folder, "folder")
        p = Path(os.path.expandvars(name.strip().strip('"'))).expanduser()
        if p.is_absolute() and p.exists():
            yield Launch(p.name, str(p), "path")
        hit = best_match(n, self.shortcuts(), exact)
        if hit:
            yield Launch(name, hit, "shortcut")
        hit = best_match(n, self.store_apps(), exact)
        if hit:
            yield Launch(name, hit, "store")
        if not exact:
            exe = shutil.which(n) or shutil.which(n.replace(" ", "")) or shutil.which(n + ".exe")
            if exe:
                yield Launch(name, exe, "exe")

    def warm(self) -> None:
        """Look up the installed apps in the background at startup, so the first "open ..." is instant."""
        import threading
        threading.Thread(target=lambda: (self.shortcuts(), self.store_apps()), daemon=True, name="aether-apps").start()

    def resolve(self, name: str, exact: bool = False) -> Launch | None:
        return next(self.candidates(name, exact), None)

    # ---- actions
    def _launch(self, ln: Launch) -> None:
        if not self.windows:
            subprocess.Popen([ln.target] if ln.kind != "uri" else ["xdg-open", ln.target])
            return
        if ln.kind == "store":
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{ln.target}"])
        elif ln.kind == "folder" and ln.target.startswith("shell:"):
            subprocess.Popen(["explorer.exe", ln.target])
        elif ln.kind == "exe" and not Path(ln.target).exists() and not ln.target.lower().endswith((".exe", ".msc")):
            subprocess.Popen(f'start "" {ln.target}', shell=True, creationflags=0x08000000)   # e.g. "code" (a .cmd)
        else:
            os.startfile(os.path.expandvars(ln.target))   # exe names use the App Paths registry; URIs open their app

    def open(self, name: str) -> str:
        ln = self.resolve(name)
        if ln is None and not self.any_app:
            return f"'{name}' is not in the allowed apps list: {', '.join(self.configured)}"
        if ln is None:
            return (f"I couldn't find an app called '{name}' on this PC. If it's installed, add it under "
                    f"[security].apps in aether.toml, e.g. {norm(name).replace(' ', '')} = \"C:/path/to/app.exe\".")
        error = None
        for ln in self.candidates(name):   # e.g. spotify.exe isn't there for the Store version: try its Start menu entry
            try:
                self.launcher(ln)
                return f"Opening {ln.name}."
            except OSError as e:
                error = e
                log.info("couldn't start %s via %s (%s); trying the next way", name, ln.target, e)
        return f"I found {name} but couldn't start it: {error}"

    def close(self, name: str) -> str:
        try:
            import psutil
        except ImportError:
            return "Closing apps needs psutil: pip install psutil"
        n = norm(name)
        want = PROCESS.get(n) or Path(ALIASES.get(n, n)).stem.lower().replace(" ", "")
        if want in PROTECTED or n in PROTECTED:
            return f"I won't close {name}: Windows or AETHER needs it."
        hits = []
        for p in psutil.process_iter(["name", "exe"]):
            pname = (p.info.get("name") or "").lower().removesuffix(".exe")
            if pname in PROTECTED:
                continue
            if pname == want or pname == n.replace(" ", "") or (len(want) >= 4 and pname.startswith(want)):
                hits.append(p)
        if not hits:
            return f"{name[:1].upper() + name[1:]} isn't running."
        for p in hits:
            try:
                p.terminate()
            except Exception:
                pass
        return f"Closed {name}."

    def screenshot(self, folder: str = "") -> str:
        out = Path(folder).expanduser() if folder else Path.home() / "Pictures" / "Screenshots"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"AETHER {datetime.now():%Y-%m-%d %H.%M.%S}.png"
        try:
            from PIL import ImageGrab
            ImageGrab.grab(all_screens=True).save(path)
        except Exception:
            import mss
            with mss.mss() as s:
                s.shot(mon=-1, output=str(path))
        return f"Screenshot saved to {path}."


def show_desktop() -> str:
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import ctypes
        k = ctypes.windll.user32.keybd_event
        k(0x5B, 0, 0, 0)
        k(0x44, 0, 0, 0)
        k(0x44, 0, 2, 0)
        k(0x5B, 0, 2, 0)
    return "Showing the desktop."


def aether_url(view: str = "") -> str:
    base = os.environ.get("AETHER_URL", "http://127.0.0.1:8765").rstrip("/")
    v = norm(view)
    if v in {"hud", "voice", "talk", "mic", ""}:
        return base + "/?hud=1"
    if v in {"app", "aether", "jarvis", "chat", "home", "window", "main"}:
        return base + "/"
    return base + "/#" + v.replace(" ", "-")


def open_aether(view: str = "hud", opener=None) -> str:
    url = aether_url(view)
    if opener is not None:
        opener(url)
    else:
        from ..interface.tray import app_window_command
        cmd = app_window_command(url)
        if cmd:
            subprocess.Popen(cmd)
        else:
            import webbrowser
            webbrowser.open(url)
    return "Opening the HUD." if "hud=1" in url else f"Opening AETHER{' ' + norm(view) if norm(view) not in {'app', 'aether', 'jarvis', 'chat', 'home', ''} else ''}."


def app_tools(apps: Apps, opener=None) -> list[Tool]:
    return [
        Tool("app_open", "open any installed app, a folder (downloads, documents...), or a Windows settings page",
             {"name": "the app or folder, e.g. spotify, whatsapp, downloads, bluetooth settings"}, "app.open", apps.open),
        Tool("app_close", "close (quit) a running app", {"name": "the app, e.g. chrome or spotify"}, "app.close", apps.close),
        Tool("screenshot", "take a screenshot and save it to Pictures\\Screenshots", {}, "screen.save",
             lambda: apps.screenshot()),
        Tool("show_desktop", "minimise every window and show the desktop", {}, "system.media", show_desktop),
        Tool("show_aether", "open AETHER's own windows: the voice HUD, the app, or a page (today, settings, memory...)",
             {"view": "hud, app, today, settings, memory, goals"}, "ui.self",
             lambda view="hud": open_aether(view, opener)),
    ]
