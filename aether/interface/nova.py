"""Nova: AETHER's little desktop companion.

A small glowing orb with eyes that floats in a corner of the screen, always on top:
- blue and calm while idle (it breathes, blinks and glances at your mouse pointer)
- amber while listening, with wide eyes
- amber with a spinning ring while thinking
- shifting between blue and amber, with ripples, while speaking
- grey and sleepy when AETHER's server isn't running

Left-click to talk (opens the voice/chat HUD), double-click to type a command to Nova right there ("open youtube",
"remind me to stretch every day at 6pm"), drag to move it (the spot is remembered), right-click for a menu.
Nova's answer appears in a small speech bubble next to it.
It only asks the local server (127.0.0.1) what AETHER is doing, twice a second. Drawing is plain Tk on the CPU,
at most 30 small frames a second, so it costs almost nothing. The tray starts it; on its own:

    .\\.venv\\Scripts\\pythonw -m aether.interface.nova
"""
from __future__ import annotations

import argparse
import json
import math
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

NAME = "Nova"
BLUE = (255, 47, 179)      # AETHER Vision magenta (Mark XXX)
AMBER = (255, 176, 32)     # orange-yellow
SLEEP = (92, 110, 132)     # server not reachable
SIZE = 72                  # window size in px, small like a desktop pet; the orb is about half of it
KEY = "#010203"            # colour made transparent on Windows
LOCK_PORT = 8767           # one Nova at a time
STATES = ("idle", "listening", "thinking", "speaking", "offline")


# ---------------------------------------------------------------- pure helpers (tested)
def mix(a: tuple, b: tuple, t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def hexc(rgb: tuple) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


def target_colour(state: str, t: float) -> tuple[int, int, int]:
    """The colour Nova moves towards in this state at time t (seconds)."""
    if state == "speaking":   # swing between blue and amber, spending little time on the grey in between
        w = math.sin(t * 2.2)
        return mix(BLUE, AMBER, 0.5 + 0.5 * math.copysign(abs(w) ** 0.35, w))
    if state in {"listening", "thinking"}:
        return AMBER
    if state == "offline":
        return SLEEP
    return BLUE


def pulse(state: str, t: float) -> float:
    """Orb scale around 1.0: slow breathing when idle, quicker and bigger when active."""
    speed, depth = {"idle": (1.3, 0.035), "listening": (3.0, 0.06), "thinking": (2.2, 0.03),
                    "speaking": (7.0, 0.07), "offline": (0.6, 0.015)}.get(state, (1.3, 0.035))
    return 1.0 + depth * math.sin(t * speed)


def is_click(dx: float, dy: float, threshold: float = 4) -> bool:
    return dx * dx + dy * dy <= threshold * threshold


def default_position(screen_w: int, screen_h: int, size: int = SIZE) -> tuple[int, int]:
    """Bottom-right corner, just above the taskbar."""
    return max(0, screen_w - size - 16), max(0, screen_h - size - 56)


def clamp_position(x: int, y: int, screen_w: int, screen_h: int, size: int = SIZE) -> tuple[int, int]:
    return max(0, min(x, screen_w - size)), max(0, min(y, screen_h - size))


def settings_path() -> Path:
    from ..core.config import default_home
    return default_home() / "nova.json"


def load_settings(path: Path | None = None) -> dict:
    path = path or settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(changes: dict, path: Path | None = None) -> dict:
    path = path or settings_path()
    data = {**load_settings(path), **changes}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    return data


def enabled(path: Path | None = None) -> bool:
    return load_settings(path).get("enabled", True) is not False


def ask(base: str, text: str, timeout: float = 180) -> str:
    """Send one command to AETHER and return its answer (what Nova's speech bubble shows)."""
    req = urllib.request.Request(base + "/api/chat", method="POST", data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("text", "") or "Done."
    except urllib.error.HTTPError as e:
        try:
            return "Sorry: " + json.loads(e.read().decode("utf-8")).get("detail", str(e))[:200]
        except Exception:
            return f"Sorry: {e}"
    except Exception:
        return "I can't reach AETHER right now. Is it still starting?"


def bubble_seconds(text: str) -> float:
    """How long the speech bubble stays: long enough to read."""
    return max(5.0, min(25.0, len(text) / 14))


def fetch_state(base: str, timeout: float = 0.8) -> str:
    try:
        with urllib.request.urlopen(base + "/api/presence", timeout=timeout) as r:
            state = json.loads(r.read().decode("utf-8")).get("state", "idle")
        return state if state in STATES else "idle"
    except Exception:
        return "offline"


# ---------------------------------------------------------------- the window
class Nova:  # pragma: no cover - needs a desktop session
    def __init__(self, port: int = 8765):
        import tkinter as tk
        self.tk = tk
        self.base = f"http://127.0.0.1:{port}"
        self.state = "offline"
        self.colour = SLEEP
        self.t0 = time.monotonic()
        self.blink_at = self.t0 + 3
        self.hidden_until = 0.0
        self.press = None
        self.dragged = False

        root = self.root = tk.Tk()
        root.title(NAME)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        win = sys.platform == "win32"
        if win:
            root.attributes("-transparentcolor", KEY)
        bg = KEY if win else "#101418"
        root.configure(bg=bg)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        s = load_settings()
        x, y = (s["x"], s["y"]) if isinstance(s.get("x"), int) and isinstance(s.get("y"), int) else default_position(sw, sh)
        x, y = clamp_position(x, y, sw, sh)
        root.geometry(f"{SIZE}x{SIZE}+{x}+{y}")

        c = self.canvas = tk.Canvas(root, width=SIZE, height=SIZE, bg=bg, highlightthickness=0, bd=0)
        c.pack()
        self.rings = [c.create_oval(0, 0, 0, 0, outline="", width=2) for _ in range(2)]
        self.layers = [c.create_oval(0, 0, 0, 0, outline="", fill="") for _ in range(8)]
        self.arc = c.create_arc(0, 0, 0, 0, style="arc", width=3, extent=90, outline="", start=0)
        self.eyes = [c.create_oval(0, 0, 0, 0, fill="white", outline="") for _ in range(2)]
        self.shine = c.create_oval(0, 0, 0, 0, fill="", outline="")

        self.box = self.bubble = None
        self._click_job = None
        menu = self.menu = tk.Menu(root, tearoff=0)
        menu.add_command(label="Type a command…", command=self.command_box)
        menu.add_command(label="My to-do list", command=lambda: self.send("what's on my todo list"))
        menu.add_command(label="Talk to AETHER", command=lambda: self.open(hud=True))
        menu.add_command(label="Open AETHER", command=lambda: self.open())
        menu.add_command(label="Settings", command=lambda: self.open(page="settings"))
        menu.add_separator()
        menu.add_command(label="Hide for an hour", command=self.hide_hour)
        menu.add_command(label="Back to the corner", command=self.to_corner)
        menu.add_command(label="Quit Nova", command=self.quit_for_good)

        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Double-Button-1>", self.on_double)
        c.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
        threading.Thread(target=self.poll, daemon=True).start()
        self.tick()

    # ---- server
    def poll(self) -> None:
        while True:
            self.state = fetch_state(self.base)
            time.sleep(0.5)

    def open(self, hud: bool = False, page: str = "") -> None:
        from .tray import app_window_command
        url = self.base + ("/?hud=1" if hud else f"/#{page}" if page else "/")
        if self.state == "offline":
            url = self.base + "/"
        cmd = app_window_command(url)
        if cmd:
            import subprocess
            subprocess.Popen(cmd)
        else:
            webbrowser.open(url)

    # ---- mouse
    def on_press(self, e) -> None:
        self.press = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())
        self.dragged = False

    def on_drag(self, e) -> None:
        if not self.press:
            return
        dx, dy = e.x_root - self.press[0], e.y_root - self.press[1]
        if not self.dragged and is_click(dx, dy):
            return
        self.dragged = True
        self.root.geometry(f"+{self.press[2] + dx}+{self.press[3] + dy}")

    def on_release(self, e) -> None:
        if self.dragged:
            save_settings({"x": self.root.winfo_x(), "y": self.root.winfo_y()})
        elif self._click_job is None:   # wait a moment: a double-click opens the command box instead
            self._click_job = self.root.after(260, self._single_click)
        self.press, self.dragged = None, False

    def _single_click(self) -> None:
        self._click_job = None
        self.open(hud=True)

    def on_double(self, e) -> None:
        if self._click_job is not None:
            self.root.after_cancel(self._click_job)
            self._click_job = None
        self.command_box()

    # ---- commands typed to Nova, answers in a speech bubble
    def _popup(self, width: int, height: int):
        tk = self.tk
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        x = max(0, self.root.winfo_x() - width + SIZE // 2)
        y = max(0, self.root.winfo_y() - height - 6)
        w.geometry(f"+{x}+{y}")
        return w

    def command_box(self) -> None:
        tk = self.tk
        if self.box is not None:
            self.box.destroy()
        box = self.box = self._popup(300, 40)
        frame = tk.Frame(box, bg="#0d1726", highlightbackground=hexc(BLUE), highlightthickness=1)
        frame.pack()
        entry = tk.Entry(frame, width=34, bg="#0d1726", fg="white", insertbackground="white", relief="flat",
                         font=("Segoe UI", 11))
        entry.pack(padx=8, pady=6)
        box.after(60, entry.focus_force)   # an undecorated window needs a moment before it can take the keyboard

        def go(_e=None):
            text = entry.get().strip()
            box.destroy()
            self.box = None
            if text:
                self.send(text)
            else:
                self.open(hud=True)
        entry.bind("<Return>", go)
        entry.bind("<Escape>", lambda _e: (box.destroy(), setattr(self, "box", None)))

        def close_if_left():
            try:
                focused = self.root.focus_get()
            except Exception:   # Tk raises for some focused widgets (menus): treat as "elsewhere"
                focused = None
            if self.box is box and box.winfo_exists() and focused is None:
                box.destroy()
                self.box = None
        entry.bind("<FocusOut>", lambda _e: self.root.after(300, close_if_left))

    def send(self, text: str) -> None:
        self.say("…")
        threading.Thread(target=lambda: self._answer(ask(self.base, text)), daemon=True).start()

    def _answer(self, reply: str) -> None:
        self.root.after(0, lambda: self.say(reply))

    def say(self, text: str) -> None:
        tk = self.tk
        if self.bubble is not None:
            self.bubble.destroy()
        short = text if len(text) <= 420 else text[:400].rsplit(" ", 1)[0] + "… (more in the app)"
        b = self.bubble = self._popup(300, 24 + 18 * (1 + len(short) // 38))
        lbl = tk.Label(b, text=short, wraplength=280, justify="left", bg="#0d1726", fg="white",
                       font=("Segoe UI", 10), padx=10, pady=8, highlightbackground=hexc(BLUE), highlightthickness=1)
        lbl.pack()
        lbl.bind("<Button-1>", lambda _e: b.destroy())
        if text != "…":
            b.after(int(bubble_seconds(text) * 1000), lambda: b.winfo_exists() and b.destroy())

    # ---- menu
    def hide_hour(self) -> None:
        self.hidden_until = time.monotonic() + 3600
        self.root.withdraw()

    def to_corner(self) -> None:
        x, y = default_position(self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        self.root.geometry(f"+{x}+{y}")
        save_settings({"x": x, "y": y})

    def quit_for_good(self) -> None:
        save_settings({"enabled": False})   # the tray's "Nova on screen" turns it back on
        self.root.destroy()

    # ---- drawing
    def tick(self) -> None:
        now = time.monotonic()
        if self.hidden_until and now >= self.hidden_until:
            self.hidden_until = 0.0
            self.root.deiconify()
            self.root.attributes("-topmost", True)
        if not self.hidden_until:
            self.draw(now - self.t0, now)
        self.root.after(33 if self.state in {"speaking", "thinking", "listening"} else 66, self.tick)

    def draw(self, t: float, now: float) -> None:
        c, state = self.canvas, self.state
        self.colour = mix(self.colour, target_colour(state, t), 0.12)
        mid, r = SIZE / 2, SIZE * 0.24 * pulse(state, t)
        dark = mix(self.colour, (8, 12, 20), 0.55)
        for i, item in enumerate(self.layers):          # soft radial gradient, light in the middle
            k = 1 - i / len(self.layers)
            rr = r * k
            c.coords(item, mid - rr, mid - rr, mid + rr, mid + rr)
            c.itemconfigure(item, fill=hexc(mix(dark, mix(self.colour, (255, 255, 255), 0.35), i / (len(self.layers) - 1))))
        c.coords(self.shine, mid - r * 0.45, mid - r * 0.62, mid - r * 0.12, mid - r * 0.38)
        c.itemconfigure(self.shine, fill=hexc(mix(self.colour, (255, 255, 255), 0.7)))
        for i, ring in enumerate(self.rings):           # halo: ripples outward while speaking or listening
            if state in {"speaking", "listening"}:
                ph = ((t * (1.1 if state == "speaking" else 0.6)) + i * 0.5) % 1.0
                rr, fade = r * (1.15 + 0.75 * ph), ph
            else:
                rr, fade = r * (1.2 + 0.12 * i + 0.03 * math.sin(t * 1.3 + i)), 0.55 + 0.2 * i
            c.coords(ring, mid - rr, mid - rr, mid + rr, mid + rr)
            c.itemconfigure(ring, outline=hexc(mix(self.colour, (8, 12, 20), fade)) if state != "offline" else "")
        if state == "thinking":
            ra = r * 1.32
            c.coords(self.arc, mid - ra, mid - ra, mid + ra, mid + ra)
            c.itemconfigure(self.arc, outline=hexc(self.colour), start=(-t * 280) % 360)
        else:
            c.itemconfigure(self.arc, outline="")
        self.draw_eyes(mid, r, t, now, state)

    def draw_eyes(self, mid: float, r: float, t: float, now: float, state: str) -> None:
        c = self.canvas
        if now >= self.blink_at + 0.14:
            self.blink_at = now + random.uniform(2.5, 6.0)
        blinking = self.blink_at <= now < self.blink_at + 0.14
        w = r * (0.16 if state == "listening" else 0.13)
        h = r * (0.32 if state == "listening" else 0.26)
        if state == "offline" or blinking:
            w, h = r * 0.16, r * 0.04                      # closed: asleep, or a blink
        lx = ly = 0.0
        if state not in {"offline"}:                       # glance towards the mouse pointer
            px, py = self.root.winfo_pointerxy()
            cx, cy = self.root.winfo_rootx() + mid, self.root.winfo_rooty() + mid
            d = math.hypot(px - cx, py - cy) or 1
            lx, ly = (px - cx) / d * r * 0.1, (py - cy) / d * r * 0.08
        if state == "thinking":
            lx, ly = r * 0.1 * math.cos(t * 3), -r * 0.12   # looking up, pondering
        for i, eye in enumerate(self.eyes):
            ex = mid + (-1 if i == 0 else 1) * r * 0.32 + lx
            ey = mid - r * 0.02 + ly
            c.coords(eye, ex - w, ey - h, ex + w, ey + h)

    def run(self) -> None:
        self.root.mainloop()


def single_instance() -> socket.socket | None:
    """Hold a local port as a lock so that only one Nova runs. None if another one already does."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        return s
    except OSError:
        s.close()
        return None


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - needs a desktop session
    ap = argparse.ArgumentParser(prog="aether-nova", description="Nova, AETHER's desktop companion.")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    from ..core.logging import ensure_streams
    ensure_streams("nova")
    lock = single_instance()
    if lock is None:
        return 0
    save_settings({"enabled": True})
    Nova(args.port).run()
    lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
