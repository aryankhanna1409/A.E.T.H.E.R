"""Everyday skills: the things people actually ask a home assistant all day.

Collected from popular "build your own assistant" projects and tutorials, and done
properly here:

- timers and alarms that ring even when the chat is closed ("set a timer for
  10 minutes for the pasta", "wake me up at 6:30", "how long left?"),
- media and system: volume, mute, play/pause/next, brightness, lock the PC,
  sleep / restart / shut down (those always ask first),
- "play lofi on YouTube", "open GitHub", "search the web for ...",
- quick answers with no model: time, date, maths, unit conversions, battery,
  CPU / memory / disk,
- notes ("take a note: ...", "read my notes"),
- the clipboard ("summarise what I copied", "copy that").

Each one is an ordinary tool, so the policy engine, approvals and the audit log
apply. Common phrasings are recognised by code and run instantly without the
language model (QuickCommands); anything else still reaches these tools
through the normal model-driven planner.
"""
from __future__ import annotations

import ast
import math
import operator
import re
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

from ..core.logging import log
from .registry import Tool

SITES = {
    "youtube": "https://www.youtube.com", "github": "https://github.com", "gmail": "https://mail.google.com",
    "google": "https://www.google.com", "maps": "https://maps.google.com", "google maps": "https://maps.google.com",
    "chatgpt": "https://chat.openai.com", "whatsapp": "https://web.whatsapp.com", "whatsapp web": "https://web.whatsapp.com",
    "linkedin": "https://www.linkedin.com", "reddit": "https://www.reddit.com", "netflix": "https://www.netflix.com",
    "amazon": "https://www.amazon.in", "spotify": "https://open.spotify.com", "stack overflow": "https://stackoverflow.com",
    "stackoverflow": "https://stackoverflow.com", "wikipedia": "https://en.wikipedia.org", "twitter": "https://x.com",
    "x": "https://x.com", "instagram": "https://www.instagram.com", "outlook": "https://outlook.office.com",
    "drive": "https://drive.google.com", "google drive": "https://drive.google.com", "leetcode": "https://leetcode.com",
    "kaggle": "https://www.kaggle.com", "hugging face": "https://huggingface.co", "huggingface": "https://huggingface.co",
}


# ---------------------------------------------------------------- safe maths
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg,
        ast.UAdd: operator.pos}
_FUNCS = {"sqrt": math.sqrt, "sin": lambda x: math.sin(math.radians(x)), "cos": lambda x: math.cos(math.radians(x)),
          "tan": lambda x: math.tan(math.radians(x)), "log": math.log10, "ln": math.log, "abs": abs, "round": round,
          "pi": math.pi, "e": math.e}


def calc(expr: str) -> float:
    """Arithmetic only: no names, attributes or calls except a few maths functions. Powers are capped."""
    e = expr.lower().replace("^", "**").replace("×", "*").replace("÷", "/").replace(",", "")
    e = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"\1/100*", e)  # 15% of 2400
    e = re.sub(r"\b(?:what\s+is|what's|whats|calculate|compute|equals?|=|\?)\b", " ", e)
    e = re.sub(r"\btimes\b|\bx\b", "*", e)
    e = re.sub(r"\bplus\b", "+", e)
    e = re.sub(r"\bminus\b", "-", e)
    e = re.sub(r"\b(?:divided\s+by|over)\b", "/", e)
    e = re.sub(r"\bsquare\s+root\s+of\s+([\d.]+)", r"sqrt(\1)", e)
    e = re.sub(r"([\d.]+)\s+squared", r"(\1)**2", e)
    e = e.replace("?", "").strip()

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and (abs(b) > 100 or abs(a) > 1e6):
                raise ValueError("that power is too large")
            return _OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Name) and n.id in _FUNCS and not callable(_FUNCS[n.id]):
            return _FUNCS[n.id]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and len(n.args) == 1:
            return _FUNCS[n.func.id](ev(n.args[0]))
        raise ValueError("not a calculation")
    if not e or len(e) > 120 or not re.search(r"\d", e):
        raise ValueError("not a calculation")
    return ev(ast.parse(e, mode="eval"))


def fmt_num(x: float) -> str:
    if isinstance(x, float) and x.is_integer() and abs(x) < 1e15:
        return f"{int(x):,}"
    return f"{x:,.4f}".rstrip("0").rstrip(".") if abs(x) >= 0.0001 else f"{x:.3g}"


# ---------------------------------------------------------------- unit conversion
_UNITS = {  # unit -> (dimension, factor to base)
    "km": ("len", 1000), "kilometers": ("len", 1000), "kilometres": ("len", 1000), "m": ("len", 1), "meters": ("len", 1),
    "metres": ("len", 1), "cm": ("len", .01), "mm": ("len", .001), "miles": ("len", 1609.344), "mile": ("len", 1609.344),
    "mi": ("len", 1609.344), "feet": ("len", .3048), "foot": ("len", .3048), "ft": ("len", .3048), "inches": ("len", .0254),
    "inch": ("len", .0254), "in": ("len", .0254), "yards": ("len", .9144),
    "kg": ("mass", 1), "kilograms": ("mass", 1), "g": ("mass", .001), "grams": ("mass", .001), "lb": ("mass", .45359237),
    "lbs": ("mass", .45359237), "pounds": ("mass", .45359237), "oz": ("mass", .0283495), "ounces": ("mass", .0283495),
    "l": ("vol", 1), "litres": ("vol", 1), "liters": ("vol", 1), "ml": ("vol", .001), "gallons": ("vol", 3.78541),
    "cups": ("vol", .2365882),
    "kmh": ("speed", 1 / 3.6), "km/h": ("speed", 1 / 3.6), "mph": ("speed", .44704), "m/s": ("speed", 1),
    "gb": ("data", 1e9), "mb": ("data", 1e6), "tb": ("data", 1e12), "gib": ("data", 2 ** 30), "mib": ("data", 2 ** 20),
    "hours": ("time", 3600), "hour": ("time", 3600), "minutes": ("time", 60), "mins": ("time", 60), "seconds": ("time", 1),
    "days": ("time", 86400), "weeks": ("time", 604800),
}
_TEMP = {"c": "c", "celsius": "c", "°c": "c", "f": "f", "fahrenheit": "f", "°f": "f", "k": "k", "kelvin": "k"}
CONVERT = re.compile(r"^(?:convert\s+|how\s+many\s+([a-z/°]+)\s+(?:is|are|in)\s+)?(-?[\d.,]+)\s*([a-z/°]+)\s+(?:to|in|into)\s+([a-z/°]+)\??$", re.I)


def convert(value: float, src: str, dst: str) -> float:
    s, d = src.lower().rstrip("."), dst.lower().rstrip(".")
    if s in _TEMP and d in _TEMP:
        c = {"c": value, "f": (value - 32) * 5 / 9, "k": value - 273.15}[_TEMP[s]]
        return {"c": c, "f": c * 9 / 5 + 32, "k": c + 273.15}[_TEMP[d]]
    if s not in _UNITS or d not in _UNITS or _UNITS[s][0] != _UNITS[d][0]:
        raise ValueError(f"I can't convert {src} to {dst}.")
    return value * _UNITS[s][1] / _UNITS[d][1]


# ---------------------------------------------------------------- durations and clock times
def parse_duration(text: str) -> int | None:
    """'10 minutes', '1h 30m', 'an hour and a half', '90 sec' -> seconds."""
    t = text.lower().replace("an hour and a half", "90 minutes").replace("half an hour", "30 minutes")
    t = re.sub(r"\b(an?|one)\s+(hour|minute|second)", r"1 \2", t)
    total, found = 0, False
    for n, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(h|hrs?|hours?|m|mins?|minutes?|s|secs?|seconds?)\b", t):
        found = True
        total += float(n) * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
    return int(total) if found and 0 < total <= 7 * 86400 else None


def parse_clock(text: str, now: datetime) -> datetime | None:
    """'6:30', '6.30 am', '7pm', '18:45' -> the next such time."""
    m = re.search(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\b", text.lower())
    if not m or not (m.group(2) or m.group(3)):
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        h = h % 12 + (12 if m.group(3) == "pm" else 0)
    if h > 23 or mi > 59:
        return None
    at = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if "tomorrow" in text.lower() or at <= now:
        at += timedelta(days=1)
    return at


def say_duration(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{h} hour{'s' if h != 1 else ''}"] if h else []
    parts += [f"{m} minute{'s' if m != 1 else ''}"] if m else []
    parts += [f"{s} second{'s' if s != 1 else ''}"] if s and not h else []
    return " and ".join(parts) or "0 seconds"


# ---------------------------------------------------------------- timers
@dataclass
class Timer:
    id: int
    label: str
    due: float
    kind: str  # timer | alarm


class Timers:
    """Timers live in the database, so they survive a restart; ringing goes through proactive notifications."""

    def __init__(self, db, lock):
        self.db, self.lock = db, lock
        with lock:
            db.execute("CREATE TABLE IF NOT EXISTS timers (id INTEGER PRIMARY KEY, label TEXT, due REAL, kind TEXT, "
                       "created REAL, status TEXT DEFAULT 'running')")
            db.commit()

    def add(self, seconds: float, label: str = "", kind: str = "timer", now: float | None = None) -> Timer:
        now = now or time.time()
        with self.lock:
            tid = self.db.execute("INSERT INTO timers(label, due, kind, created) VALUES (?,?,?,?)",
                                  (label.strip()[:80], now + seconds, kind, now)).lastrowid
            self.db.commit()
        return Timer(tid, label.strip()[:80], now + seconds, kind)

    def running(self) -> list[Timer]:
        with self.lock:
            rows = self.db.execute("SELECT id, label, due, kind FROM timers WHERE status='running' ORDER BY due").fetchall()
        return [Timer(*r) for r in rows]

    def due(self, now: float | None = None) -> list[Timer]:
        now = now or time.time()
        return [t for t in self.running() if t.due <= now]

    def finish(self, tid: int, status: str = "rung") -> None:
        with self.lock:
            self.db.execute("UPDATE timers SET status=? WHERE id=?", (status, tid))
            self.db.commit()

    def cancel(self, label: str = "") -> list[Timer]:
        running = self.running()
        hit = [t for t in running if label and label.lower() in t.label.lower()] or running[:1] if label else running
        for t in hit:
            self.finish(t.id, "cancelled")
        return hit


# ---------------------------------------------------------------- the machine (Windows; harmless elsewhere)
class System:
    """Thin wrappers over Windows. `run` is swappable so tests never touch the real machine."""

    def __init__(self):
        self.run = self._run
        self.open_url = lambda url: webbrowser.open(url)
        self.find_video = _find_video
        self.keys: list[int] = []  # media keys sent (for tests)

    @staticmethod
    def _run(args: list[str], timeout: float = 10) -> str:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        return (r.stdout or r.stderr).strip()

    def key(self, vk: int, times: int = 1) -> None:
        self.keys += [vk] * times
        if sys.platform != "win32":
            return
        import ctypes  # pragma: no cover - Windows only
        for _ in range(times):
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, 2, 0)

    def ps(self, script: str) -> str:
        return self.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])

    def clipboard(self) -> str:
        if sys.platform == "win32":
            return self.ps("Get-Clipboard -Raw")
        try:
            import tkinter
            r = tkinter.Tk()
            r.withdraw()
            v = r.clipboard_get()
            r.destroy()
            return v
        except Exception:
            return ""

    def set_clipboard(self, text: str) -> None:
        if sys.platform == "win32":
            subprocess.run(["clip"], input=text.encode("utf-16-le"), check=False,
                           creationflags=0x08000000)
        else:
            self.run(["sh", "-c", "command -v xclip >/dev/null && printf %s \"$0\" | xclip -selection clipboard", text])


def _find_video(query: str) -> str | None:
    """The first YouTube video for a query (DuckDuckGo video search, no key), so 'play X' really plays."""
    try:
        from ddgs import DDGS
        hits = DDGS().videos(query, max_results=5)
        return next((h.get("content") for h in hits if "youtube.com/watch" in str(h.get("content"))), None)
    except Exception as e:
        log.info("video search unavailable (%s); opening YouTube search instead", e)
        return None


VK = {"mute": 0xAD, "down": 0xAE, "up": 0xAF, "next": 0xB0, "prev": 0xB1, "stop": 0xB2, "play": 0xB3}


def status_report(spoken: bool = False) -> str:
    try:
        import psutil
    except ImportError:
        return "System stats need psutil: pip install psutil"
    parts = []
    b = psutil.sensors_battery()
    if b:
        parts.append(f"Battery {b.percent:.0f}%" + (" and charging" if b.power_plugged else ""))
    parts.append(f"CPU {psutil.cpu_percent(interval=0.3):.0f}%")
    vm = psutil.virtual_memory()
    parts.append(f"memory {vm.percent:.0f}% used ({vm.available / 2 ** 30:.1f} GB free)")
    try:
        du = psutil.disk_usage(str(Path.home().anchor or "/"))
        parts.append(f"disk {du.free / 2 ** 30:.0f} GB free")
    except Exception:
        pass
    return ", ".join(parts) + "."


# ---------------------------------------------------------------- tools
def everyday_tools(a, system: System) -> list[Tool]:
    notes_dir = Path(a.cfg.home) / "notes"

    def timer(duration: str = "", label: str = "", at: str = "") -> str:
        now = datetime.now()
        if at:
            when = parse_clock(at, now)
            if not when:
                return f"I couldn't read the time '{at}'. Try '6:30 am'."
            t = a.timers.add((when - now).total_seconds(), label or "Alarm", "alarm")
            return f"Alarm set for {when:%H:%M}" + (" tomorrow" if when.date() > now.date() else "") + \
                   (f": {t.label}." if label else ".")
        secs = parse_duration(duration)
        if not secs:
            return f"I couldn't read '{duration}' as a length of time. Try '10 minutes'."
        a.timers.add(secs, label)
        return f"Timer set for {say_duration(secs)}" + (f" for {label}." if label else ".")

    def timers_list() -> str:
        r = a.timers.running()
        if not r:
            return "No timers running."
        now = time.time()
        return "\n".join(f"- {t.label or t.kind.capitalize()}: " + (
            f"{say_duration(t.due - now)} left" if t.kind == "timer" else f"at {datetime.fromtimestamp(t.due):%H:%M}")
            for t in r)

    def timer_cancel(label: str = "") -> str:
        gone = a.timers.cancel(label)
        return ("Cancelled " + ", ".join(t.label or t.kind for t in gone) + ".") if gone else "No timers to cancel."

    def media(action: str, amount: str = "") -> str:
        act = action.lower().strip()
        if act in {"volume_up", "louder", "up"}:
            system.key(VK["up"], max(1, min(25, int(amount or 5))))
            return "Volume up."
        if act in {"volume_down", "quieter", "down"}:
            system.key(VK["down"], max(1, min(25, int(amount or 5))))
            return "Volume down."
        if act in {"volume", "set"}:
            n = max(0, min(100, int(float(amount or 50))))
            system.key(VK["down"], 50)  # to zero, then up in 2% steps: no extra packages needed
            system.key(VK["up"], n // 2)
            return f"Volume at {n}%."
        if act in {"mute", "unmute"}:
            system.key(VK["mute"])
            return "Toggled mute."
        if act in {"pause", "play", "resume", "play_pause"}:
            system.key(VK["play"])
            return "Play/pause."
        if act in {"next", "skip"}:
            system.key(VK["next"])
            return "Next track."
        if act in {"previous", "prev", "back"}:
            system.key(VK["prev"])
            return "Previous track."
        return f"Unknown media action '{action}'."

    def brightness(level: str) -> str:
        n = max(0, min(100, int(float(level))))
        out = system.ps(f"(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods)"
                        f".WmiSetBrightness(1,{n})")
        return f"Brightness at {n}%." if "error" not in out.lower() else "This screen doesn't let me change brightness."

    def power(action: str) -> str:
        act = action.lower().strip()
        cmds = {"lock": ["rundll32.exe", "user32.dll,LockWorkStation"],
                "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                "restart": ["shutdown", "/r", "/t", "30"], "shutdown": ["shutdown", "/s", "/t", "30"],
                "cancel": ["shutdown", "/a"]}
        if act not in cmds:
            return f"Unknown power action '{action}'. I can lock, sleep, restart, shutdown or cancel."
        system.run(cmds[act])
        return {"lock": "Locked.", "sleep": "Going to sleep.", "restart": "Restarting in 30 seconds. Say 'cancel shutdown' to stop it.",
                "shutdown": "Shutting down in 30 seconds. Say 'cancel shutdown' to stop it.", "cancel": "Cancelled."}[act]

    def open_site(site: str) -> str:
        s = site.strip().lower().removeprefix("the ").strip()
        url = SITES.get(s) or (site if re.match(r"^https?://", site) else
                               f"https://{s}" if re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", s) else None)
        if not url:
            return f"I don't know a website called '{site}'. Say the address, like 'open python.org'."
        system.open_url(url)
        return f"Opening {s}."

    def youtube(query: str) -> str:
        url = system.find_video(query)
        system.open_url(url or f"https://www.youtube.com/results?search_query={quote_plus(query)}")
        return f"Playing {query} on YouTube." if url else f"Searching YouTube for {query}."

    def web_open_search(query: str) -> str:
        system.open_url(f"https://www.google.com/search?q={quote_plus(query)}")
        return f"Searching the web for {query}."

    def note(text: str) -> str:
        notes_dir.mkdir(parents=True, exist_ok=True)
        f = notes_dir / f"{datetime.now():%Y-%m}.md"
        with f.open("a", encoding="utf-8") as h:
            h.write(f"- {datetime.now():%a %d %b %H:%M}: {text.strip()}\n")
        a.store.add(f"[{datetime.now():%Y-%m-%d}] Note: {text.strip()}", "episodic", "user_stated")
        return "Noted."

    def notes_read(count: str = "5") -> str:
        files = sorted(notes_dir.glob("*.md"))
        lines = [l for f in files for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        n = max(1, min(30, int(count or 5)))
        return "\n".join(lines[-n:]) if lines else "No notes yet. Say 'take a note' followed by anything."

    def clipboard() -> str:
        text = system.clipboard()
        return text[:6000] if text.strip() else "The clipboard is empty."

    def copy_text(text: str) -> str:
        system.set_clipboard(text)
        return f"Copied {len(text)} characters."

    return [
        Tool("timer", "set a timer or an alarm", {"duration": "e.g. '10 minutes' (for a timer)",
                                                   "label": "what it's for, optional", "at": "clock time for an alarm, e.g. '6:30 am'"},
             "timer", timer),
        Tool("timers", "list running timers and alarms", {}, "timer", timers_list),
        Tool("cancel_timer", "cancel a timer or alarm", {"label": "which one, optional"}, "timer", timer_cancel),
        Tool("media", "control playback and volume", {"action": "volume_up, volume_down, volume, mute, pause, next, previous",
                                                      "amount": "steps, or a percent for 'volume'"}, "system.media", media),
        Tool("brightness", "set screen brightness", {"level": "0-100"}, "system.media", brightness),
        Tool("power", "lock, sleep, restart or shut down the PC (always asks first)", {"action": "lock, sleep, restart, shutdown, cancel"},
             "system.power", power),
        Tool("open_website", "open a website in the browser", {"site": "name (github, gmail) or address"}, "web.open", open_site),
        Tool("play_youtube", "find and play a video or music on YouTube", {"query": "what to play"}, "web.open", youtube),
        Tool("search_in_browser", "open a web search in the browser for the user to look at", {"query": "search terms"},
             "web.open", web_open_search),
        Tool("note", "take a quick note", {"text": "the note"}, "memory", note),
        Tool("read_notes", "read the latest notes", {"count": "how many, default 5"}, "memory", notes_read),
        Tool("read_clipboard", "read the text the user copied (to summarise, translate, fix...)", {}, "clipboard.read", clipboard),
        Tool("copy_to_clipboard", "put text on the clipboard", {"text": "text"}, "clipboard.write", copy_text),
        Tool("system_status", "battery, CPU, memory and disk", {}, "kg", lambda: status_report()),
    ]
