"""AETHER in the Windows system tray.

`aether-tray` keeps AETHER one keystroke away without a window open:

- starts the local server in the background (if it is not already running),
- a global hotkey (Ctrl+Alt+J by default) opens the voice HUD from anywhere,
- proactive notifications (reminders, "lecture in 15 minutes", low battery)
  appear as Windows notifications,
- the tray menu opens the app or the HUD, pauses notifications, triggers the
  emergency stop, and turns "start with Windows" on or off.

Everything talks to the server on 127.0.0.1 only. Needs the `tray` extra:
pip install -e ".[tray]"  (pystray, pillow, pynput).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from ..core.logging import log, setup_logging

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "AETHER"


# ---------------------------------------------------------------- pure helpers (tested)
def pynput_hotkey(spec: str) -> str:
    """'ctrl+alt+j' / 'Ctrl + Shift + Space' -> pynput's '<ctrl>+<alt>+j' format."""
    named = {"ctrl", "alt", "shift", "cmd", "win", "space", "enter", "tab", "esc", "f1", "f2", "f3", "f4", "f5", "f6",
             "f7", "f8", "f9", "f10", "f11", "f12"}
    parts = [p.strip().lower() for p in spec.replace(" ", "").split("+") if p.strip()]
    if not parts or len(parts[-1]) != 1 and parts[-1] not in named:
        raise ValueError(f"Can't read the hotkey '{spec}'. Use something like ctrl+alt+j.")
    if not any(p in {"ctrl", "alt", "cmd", "win"} for p in parts[:-1]):
        raise ValueError("A global hotkey needs Ctrl, Alt or Win, so it doesn't clash with normal typing.")
    return "+".join(f"<{'cmd' if p == 'win' else p}>" if p in named else p for p in parts)


def startup_command(python: str | None = None) -> str:
    """The command Windows runs at sign-in: pythonw (no console window) -m aether.interface.tray."""
    exe = Path(python or sys.executable)
    windowless = exe.with_name("pythonw.exe")
    if exe.name.lower() == "python.exe" and windowless.exists():
        exe = windowless
    return f'"{exe}" -m aether.interface.tray --quiet-start'


def app_window_command(url: str) -> list[str] | None:
    """Open a URL as a clean app window (no tabs or address bar) with Edge or Chrome, if one is installed."""
    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        exe = shutil.which(name)
        if exe:
            return [exe, f"--app={url}", "--window-size=1100,760"]
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if Path(p).exists():
            return [p, f"--app={url}", "--window-size=1100,760"]
    return None


def make_icon(size: int = 64, active: bool = True):
    """A small glowing ring, drawn at runtime so there is no binary asset to ship."""
    from PIL import Image, ImageDraw, ImageFilter
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = (69, 214, 255, 255) if active else (130, 140, 150, 255)
    w = s // 9
    d.ellipse([w, w, s - w, s - w], outline=c, width=w)
    glow = img.filter(ImageFilter.GaussianBlur(s // 24))
    img = Image.alpha_composite(glow, img)
    r = s // 7
    ImageDraw.Draw(img).ellipse([s // 2 - r, s // 2 - r, s // 2 + r, s // 2 + r], fill=c)
    return img.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------- the tray app
class Tray:
    def __init__(self, port: int = 8765, hotkey: str = "ctrl+alt+j", poll_s: int = 30, config: str | None = None):
        self.base = f"http://127.0.0.1:{port}"
        self.port, self.hotkey, self.poll_s, self.config = port, hotkey, poll_s, config
        self.paused = False
        self.server: subprocess.Popen | None = None
        self.icon = None
        self._stop = threading.Event()

    # ---- server
    def api(self, path: str, method: str = "GET", body: dict | None = None, timeout: float = 5):
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")

    def alive(self) -> bool:
        try:
            self.api("/api/status", timeout=1.5)
            return True
        except Exception:
            return False

    def ensure_server(self, wait_s: float = 90) -> bool:
        if self.alive():
            return True
        cmd = [sys.executable, "-m", "aether.interface.server", "--no-browser", "--port", str(self.port)]
        if self.config:
            cmd += ["--config", self.config]
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        self.server = subprocess.Popen(cmd, creationflags=flags)
        end = time.time() + wait_s  # loading the model takes a while on first start
        while time.time() < end and not self._stop.is_set():
            if self.alive():
                return True
            time.sleep(1)
        return False

    # ---- actions
    def open(self, hud: bool | str = False) -> None:
        url = self.base + (f"/?hud={'brief' if hud == 'brief' else 1}" if hud else "/")
        cmd = app_window_command(url)
        if cmd:
            subprocess.Popen(cmd)
        else:
            webbrowser.open(url)

    def notify(self, title: str, body: str) -> None:
        if self.icon is not None:
            try:
                self.icon.notify(body[:240], title[:60])
            except Exception as e:  # pragma: no cover - platform dependent
                log.warning("tray notification failed: %s", e)

    def poll_once(self) -> int:
        if self.paused:
            return 0
        items = self.api("/api/notifications").get("items", [])
        for n in items:
            self.notify(n["title"], n["body"])
        return len(items)

    def emergency_stop(self) -> None:
        try:
            self.api("/api/stop", "POST")
            self.notify("Emergency stop", "All actions are blocked. Say 'resume' in AETHER to continue.")
        except Exception as e:
            self.notify("AETHER", f"Couldn't reach the server: {e}")

    # ---- start with Windows (current user only, no admin rights)
    def startup_enabled(self) -> bool:
        if sys.platform != "win32":
            return False
        import winreg  # pragma: no cover - Windows only
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, APP_NAME)
            return True
        except OSError:
            return False

    def set_startup(self, on: bool) -> None:  # pragma: no cover - Windows only
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, startup_command())
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except OSError:
                    pass

    # ---- loop
    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.poll_once()
            except Exception as e:
                log.debug("notification poll failed: %s", e)

    def quit(self) -> None:
        self._stop.set()
        if self.icon is not None:
            self.icon.stop()
        if self.server is not None:
            self.server.terminate()

    def run(self, quiet_start: bool = False) -> int:  # pragma: no cover - needs a desktop session
        import pystray
        from pynput import keyboard

        item, menu = pystray.MenuItem, pystray.Menu

        def toggle_pause(icon, _):
            self.paused = not self.paused
            icon.icon = make_icon(active=not self.paused)

        self.icon = pystray.Icon(APP_NAME, make_icon(), f"AETHER  ({self.hotkey.upper()} for voice)", menu(
            item("Open AETHER", lambda: self.open(), default=True),
            item(f"Talk ({self.hotkey})", lambda: self.open(hud=True)),
            item("Morning briefing", lambda: self.open(hud="brief")),
            menu.SEPARATOR,
            item("Pause notifications", toggle_pause, checked=lambda _: self.paused),
            item("Start with Windows", lambda: self.set_startup(not self.startup_enabled()),
                 checked=lambda _: self.startup_enabled(), visible=sys.platform == "win32"),
            item("Emergency stop", lambda: self.emergency_stop()),
            menu.SEPARATOR,
            item("Quit", lambda: self.quit()),
        ))

        def started(icon):
            icon.visible = True
            if not self.ensure_server():
                self.notify("AETHER", "The server didn't start. Run aether-server in a terminal to see why.")
                return
            if not quiet_start:
                self.notify("AETHER is ready", f"Press {self.hotkey.upper()} anywhere to talk.")
            threading.Thread(target=self._poll_loop, daemon=True).start()

        hk = keyboard.GlobalHotKeys({pynput_hotkey(self.hotkey): lambda: self.open(hud=True)})
        hk.start()
        try:
            self.icon.run(setup=started)
        finally:
            hk.stop()
            self.quit()
        return 0


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - needs a desktop session
    ap = argparse.ArgumentParser(prog="aether-tray", description="AETHER in the system tray, with a global voice hotkey.")
    ap.add_argument("--config", default=None)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--hotkey", default="ctrl+alt+j", help="global hotkey for the voice HUD (default ctrl+alt+j)")
    ap.add_argument("--quiet-start", action="store_true", help="no 'ready' notification (used at sign-in)")
    args = ap.parse_args(argv)
    setup_logging("INFO")
    pynput_hotkey(args.hotkey)  # fail early on a bad hotkey
    try:
        import pystray  # noqa: F401
        import pynput  # noqa: F401
    except ImportError:
        print('The tray needs extra packages: pip install -e ".[tray]"')
        return 1
    return Tray(args.port, args.hotkey, config=args.config).run(args.quiet_start)


if __name__ == "__main__":
    raise SystemExit(main())
