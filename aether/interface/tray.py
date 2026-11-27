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
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from ..core.logging import ensure_streams, log, log_dir, setup_logging

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "AETHER"
ROOT = Path(__file__).resolve().parents[2]   # the install folder, e.g. C:\AETHER
LOCK_PORT = 8768                             # one tray at a time
SERVER_WAIT_S = 300                          # the first load of a 7B model can take a few minutes


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


def console_python(python: str | None = None) -> str:
    """python.exe next to pythonw.exe: the server runs with it (hidden), so its output can go to a log file."""
    exe = Path(python or sys.executable)
    console = exe.with_name("python.exe")
    return str(console) if exe.name.lower() == "pythonw.exe" and console.exists() else str(exe)


def startup_command(python: str | None = None, config: str | None = None) -> str:
    """The command Windows runs at sign-in: pythonw (no console window) -m aether.interface.tray."""
    exe = Path(python or sys.executable)
    windowless = exe.with_name("pythonw.exe")
    if exe.name.lower() == "python.exe" and windowless.exists():
        exe = windowless
    cmd = f'"{exe}" -m aether.interface.tray --quiet-start'
    return cmd + (f' --config "{Path(config).resolve()}"' if config else "")


def single_instance(port: int | None = None) -> socket.socket | None:
    """Hold a local port as a lock so only one tray runs. None if another one already does."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT if port is None else port))
        return s
    except OSError:
        s.close()
        return None


def find_processes(module: str) -> list:
    """Running `python -m <module>` processes of this user (psutil), other than this one."""
    import os
    try:
        import psutil
    except ImportError:
        return []
    found = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = p.info.get("cmdline") or []
            if p.info["pid"] != os.getpid() and module in cmd and "-m" in cmd:
                found.append(p)
        except Exception:
            continue
    return found


def log_tail(path: Path, lines: int = 6) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-lines:])
    except OSError:
        return ""


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
    def __init__(self, port: int = 8765, hotkey: str = "ctrl+alt+j", poll_s: int = 5, config: str | None = None):
        self.base = f"http://127.0.0.1:{port}"
        self.port, self.hotkey, self.poll_s, self.config = port, hotkey, poll_s, config
        self.paused = False
        self.server: subprocess.Popen | None = None
        self.nova: subprocess.Popen | None = None
        self.voice: subprocess.Popen | None = None
        self.icon = None
        self.server_log = log_dir() / "server.log"
        self._stop = threading.Event()
        self._surfaced: set[str] = set()

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

    def server_version(self) -> str | None:
        try:
            return self.api("/api/status", timeout=2).get("version")
        except Exception:
            return None

    def replace_stale(self, wait_s: float = 15) -> list[str]:
        """Stop what an earlier run left behind. This tray holds the one-tray lock, so a voice listener or Nova that
        is already running belongs to an old tray (or crashed one) and may be running old code: replace them. A
        server is kept when it runs the same code as this tray, and replaced when it doesn't."""
        from ..core.version import code_version
        stopped = []
        for module in ("aether.speech.voice_loop", "aether.interface.nova"):
            for p in find_processes(module):
                try:
                    p.terminate()
                    stopped.append(module)
                except Exception:
                    pass
        if self.alive():
            running = self.server_version()
            if running != code_version():
                log.info("the running server has other code (%s, this is %s): restarting it", running, code_version())
                try:
                    self.api("/api/shutdown", method="POST", timeout=3)
                except Exception:
                    for p in find_processes("aether.interface.server"):
                        p.terminate()
                end = time.time() + wait_s
                while time.time() < end and self.alive():
                    time.sleep(0.5)
                stopped.append("aether.interface.server")
        return stopped

    def server_command(self) -> list[str]:
        cmd = [console_python(), "-m", "aether.interface.server", "--no-browser", "--port", str(self.port)]
        return cmd + (["--config", self.config] if self.config else [])

    def ensure_server(self, wait_s: float = SERVER_WAIT_S) -> bool:
        """Start the server hidden, with its output in logs\\server.log, and wait until it answers."""
        if self.alive():
            return True
        self.server_log.parent.mkdir(parents=True, exist_ok=True)
        if self.server_log.exists() and self.server_log.stat().st_size > 5_000_000:
            self.server_log.replace(self.server_log.with_suffix(".old.log"))
        out = open(self.server_log, "a", encoding="utf-8")
        out.write(f"\n---- starting AETHER server {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        out.flush()
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        self.server = subprocess.Popen(self.server_command(), cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, creationflags=flags)
        out.close()
        end = time.time() + wait_s  # loading the model takes a while on first start
        while time.time() < end and not self._stop.is_set():
            if self.alive():
                return True
            if self.server.poll() is not None:   # it crashed: no point waiting
                log.error("AETHER server exited with code %s. See %s", self.server.returncode, self.server_log)
                return False
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
        if self.paused:          # notifications are paused, but an approval you asked for still has to show
            self.surface_approvals()
            return 0
        items = self.api("/api/notifications").get("items", [])
        for n in items:
            self.notify(n["title"], n["body"])
        self.surface_approvals()
        return len(items)

    def surface_approvals(self) -> bool:
        """A request made by voice (e.g. "shut down my pc") needs your OK on screen. If no AETHER window is
        showing, open one so the Allow / Deny box appears. Returns True if it opened the app."""
        w = self.api("/api/pending/watch")
        new = [i for i in w.get("items", []) if i["id"] not in self._surfaced]
        self._surfaced.update(i["id"] for i in new)
        if not new:
            return False
        self.notify("AETHER needs your OK", new[0]["prompt"].split("\n")[0])
        if not w.get("window_visible"):
            self.open()
            return True
        return False

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
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, startup_command(config=self.config))
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

    # ---- Nova, the desktop pet
    def nova_command(self) -> list[str]:
        exe = sys.executable
        if sys.platform == "win32" and exe.lower().endswith("python.exe"):
            exe = exe[:-len("python.exe")] + "pythonw.exe"   # no console window
        return [exe, "-m", "aether.interface.nova", "--port", str(self.port)]

    def nova_running(self) -> bool:
        return self.nova is not None and self.nova.poll() is None

    def start_nova(self) -> None:
        if not self.nova_running():
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.nova = subprocess.Popen(self.nova_command(), creationflags=flags)

    # ---- always listening ("AETHER" or "Nova")
    def voice_command(self) -> list[str]:
        cmd = self.nova_command()[:1] + ["-m", "aether.speech.voice_loop", "--server", "--port", str(self.port)]
        return cmd + (["--config", self.config] if self.config else [])

    def listening(self) -> bool:
        return self.voice is not None and self.voice.poll() is None

    @staticmethod
    def listening_wanted() -> bool:
        from .nova import load_settings
        return load_settings().get("listening", True) is not False

    def start_voice(self) -> None:
        if not self.listening():
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.voice = subprocess.Popen(self.voice_command(), cwd=str(ROOT), creationflags=flags)
            threading.Thread(target=self._watch_voice, args=(self.voice,), daemon=True).start()

    def _watch_voice(self, proc: subprocess.Popen) -> None:
        """If the listener dies on its own (a missing voice file, no microphone...), say so instead of failing silently."""
        code = proc.wait()
        if self._stop.is_set() or proc is not self.voice or code == 0 or not self.listening_wanted():
            return
        voice_log = log_dir() / "voice.log"
        tail = log_tail(voice_log, 2)
        log.error("always-listening voice exited with code %s. See %s", code, voice_log)
        self.notify("AETHER stopped listening", f"Details in {voice_log}" + (f"\n{tail}" if tail else ""))

    def toggle_listening(self) -> None:
        """Mute or unmute the microphone. Muted, nothing listens at all; the hotkey still opens the HUD."""
        from .nova import save_settings
        if self.listening():
            save_settings({"listening": False})   # first, so the watcher knows this stop was on purpose
            self.voice.terminate()
            self.notify("Microphone off", "AETHER isn't listening. Turn it back on from the tray menu.")
        else:
            save_settings({"listening": True})
            self.start_voice()
            self.notify("Listening", 'Say "AETHER" or "Nova" any time.')

    def toggle_nova(self) -> None:
        from .nova import save_settings
        if self.nova_running():
            self.nova.terminate()
            save_settings({"enabled": False})
        else:
            save_settings({"enabled": True})
            self.start_nova()

    def quit(self) -> None:
        self._stop.set()
        if self.nova_running():
            self.nova.terminate()
        if self.listening():
            self.voice.terminate()
        if self.icon is not None:
            self.icon.stop()
        if self.server is not None:
            self.server.terminate()

    def turn_on_startup_once(self) -> None:
        """Turn on "Start with Windows" the first time AETHER is started, so a reboot needs no steps.
        Only once: if you untick it later in the tray menu, it stays off."""
        from .nova import load_settings, save_settings
        if sys.platform != "win32" or load_settings().get("startup_set"):
            return
        try:
            self.set_startup(True)
            save_settings({"startup_set": True})
        except Exception as e:  # pragma: no cover - Windows only
            log.warning("couldn't turn on Start with Windows: %s", e)

    def show_nova(self) -> None:
        from .nova import enabled as nova_enabled
        if nova_enabled():
            try:
                self.start_nova()
            except Exception as e:  # e.g. Python installed without Tk: the tray still works
                log.warning("Nova didn't start: %s", e)

    def run(self, quiet_start: bool = False, open_app: bool = False) -> int:  # pragma: no cover - needs a desktop

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
            item("Nova on screen", lambda: self.toggle_nova(), checked=lambda _: self.nova_running()),
            item("Always listening (mic on)", lambda: self.toggle_listening(), checked=lambda _: self.listening()),
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
            self.replace_stale()          # after a git pull: no old server, listener or Nova left running
            self.show_nova()             # straight away: Nova sleeps (grey) until the server is up
            self.turn_on_startup_once()
            if not self.alive() and not quiet_start:
                self.notify("AETHER is starting", "Loading the model. This takes a minute or two.")
            if not self.ensure_server():
                tail = log_tail(self.server_log, 2)
                self.notify("AETHER didn't start", f"Details in {self.server_log}" + (f"\n{tail}" if tail else ""))
                return
            if self.listening_wanted():
                try:
                    self.start_voice()
                except Exception as e:  # voice packages missing: everything else still works
                    log.warning("always-listening voice didn't start: %s", e)
            if open_app:
                self.open()
            elif not quiet_start:
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
    ap.add_argument("--open", action="store_true", help="open the app once the server is ready (start.bat)")
    args = ap.parse_args(argv)
    ensure_streams("tray")
    setup_logging("INFO")
    pynput_hotkey(args.hotkey)  # fail early on a bad hotkey
    try:
        import pystray  # noqa: F401
        import pynput  # noqa: F401
    except ImportError:
        print('The tray needs extra packages: pip install -e ".[tray]"')
        return 1
    config = args.config
    if config is None and Path("aether.toml").exists():
        config = str(Path("aether.toml").resolve())   # remembered for "Start with Windows"
    tray = Tray(args.port, args.hotkey, config=config)
    lock = single_instance()
    if lock is None and tray.alive():
        from ..core.version import code_version
        if tray.server_version() != code_version():
            # the tray that's running is from before an update (git pull): replace it, so the new code runs
            log.info("replacing an AETHER tray that runs older code")
            for p in find_processes("aether.interface.tray"):
                try:
                    p.terminate()
                except Exception:
                    pass
            end = time.time() + 10
            while lock is None and time.time() < end:
                time.sleep(0.5)
                lock = single_instance()
    if lock is None:   # already running (e.g. started at sign-in): just show the app
        if args.open:
            end = time.time() + SERVER_WAIT_S
            while time.time() < end and not tray.alive():
                time.sleep(1)
            tray.open()
        return 0
    try:
        return tray.run(args.quiet_start, open_app=args.open)
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
