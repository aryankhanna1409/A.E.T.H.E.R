"""Proactive awareness: notice long stretches of focus, and spot deadlines before they arrive.

Both are deterministic code on this PC, need no model, and go through the proactive engine, so quiet hours,
the hourly limit and dismissing work as for every other nudge. Each can be switched off in Settings.

Focus breaks
    Every minute AETHER asks Windows how long ago you last touched the keyboard or mouse (GetLastInputInfo)
    and which app is in front (the program's name only, never window titles or what is on screen). Activity with
    no gap of five minutes or more is one focus session. After ``focus_break_minutes`` (90 by default) it
    suggests a short break, once per stretch, and the count starts again when you step away.

Deadlines
    Calendar events and reminders in the next fortnight whose wording says they are due ("Essay submission",
    "Thesis deadline", "Exam"), and dates written next to "due", "deadline" or "submit by" in the documents
    AETHER has indexed ("Report due 4 December", "deadline: 2026-12-10"). Each one is nudged early, at 7, 3
    and 1 days before by default. Document text is untrusted, so anything that looks like instructions is
    skipped.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..core.logging import log

BREAK_GAP_S = 300          # five minutes away from the keyboard counts as a break
DEADLINE_WORDS = re.compile(r"\b(?:due|deadline|submission|submit|exam|examination|viva|test|interview|"
                            r"presentation|hand[- ]in|last date|closes|application)\b", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"], 1)}
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE = (rf"(?:(?P<iso>\d{{4}}-\d{{2}}-\d{{2}})"
         rf"|(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m1>{_MON})\.?(?:,?\s+(?P<y1>\d{{4}}))?"
         rf"|(?P<m2>{_MON})\.?\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<y2>\d{{4}}))?"
         rf"|(?P<d3>\d{{1,2}})/(?P<m3>\d{{1,2}})/(?P<y3>\d{{2,4}}))")
CUE = re.compile(rf"\b(?:due(?:\s+(?:on|by|date))?|deadline(?:\s+is)?|submit(?:\s+it)?\s+(?:by|before)|submission(?:\s+date)?"
                 rf"|hand\s+in\s+by|last\s+date(?:\s+to\s+\w+)?|closes(?:\s+on)?)\s*[:\-–]?\s*(?:on\s+)?{_DATE}", re.I)


# ------------------------------------------------------------------------------------------------ probes

def idle_seconds() -> float | None:
    """Seconds since the last keyboard or mouse input in this Windows session, or None where unsupported."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return None


FRIENDLY = {"code": "VS Code", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "winword": "Word",
            "excel": "Excel", "powerpnt": "PowerPoint", "pycharm64": "PyCharm", "windowsterminal": "Terminal",
            "matlab": "MATLAB", "solidworks": "SolidWorks", "sldworks": "SolidWorks", "obsidian": "Obsidian",
            "notion": "Notion", "discord": "Discord", "spotify": "Spotify", "explorer": "File Explorer"}


def foreground_app() -> str:
    """The program in front, as a friendly name ("VS Code"). Never the window title."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        import psutil
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = psutil.Process(pid.value).name().rsplit(".", 1)[0].lower()
        return FRIENDLY.get(name, name[:1].upper() + name[1:])
    except Exception:
        return ""


# ------------------------------------------------------------------------------------------------ focus

@dataclass
class FocusTracker:
    """Turns once-a-minute idle readings into focus sessions."""
    idle_probe: object = idle_seconds
    app_probe: object = foreground_app
    start: float | None = None
    last_active: float | None = None
    apps: Counter = field(default_factory=Counter)

    def sample(self, now: float) -> bool:
        """Record one reading. Returns False when this PC can't tell (then focus breaks stay quiet)."""
        idle = self.idle_probe()
        if idle is None:
            return False
        if idle >= BREAK_GAP_S:
            self.start, self.last_active = None, None   # away long enough: that was a break
            self.apps.clear()
            return True
        active_at = now - idle
        if self.start is None or (self.last_active is not None and active_at - self.last_active >= BREAK_GAP_S):
            self.start = active_at
            self.apps.clear()
        self.last_active = active_at
        app = self.app_probe()
        if app and app.lower() not in {"aether", "pythonw", "python", "lockapp"}:
            self.apps[app] += 1
        return True

    def minutes(self, now: float) -> float:
        return 0.0 if self.start is None else (now - self.start) / 60

    def main_app(self) -> str:
        return self.apps.most_common(1)[0][0] if self.apps else ""


def duration(minutes: float) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


# ------------------------------------------------------------------------------------------------ deadlines

@dataclass
class Deadline:
    title: str
    due: date
    source: str           # "calendar", "reminder" or a file path

    @property
    def key(self) -> str:
        return hashlib.sha1(f"{self.title.lower()}|{self.due}".encode()).hexdigest()[:16]


def parse_date(m: re.Match, today: date) -> date | None:
    g = m.groupdict()
    try:
        if g.get("iso"):
            return date.fromisoformat(g["iso"])
        if g.get("d3"):
            y = int(g["y3"])
            return date(y + 2000 if y < 100 else y, int(g["m3"]), int(g["d3"]))   # day first, as in India and the UK
        d, mon, y = (g["d1"], g["m1"], g["y1"]) if g.get("d1") else (g["d2"], g["m2"], g["y2"])
        month = MONTHS[mon[:3].lower()]
        if y:
            return date(int(y), month, int(d))
        guess = date(today.year, month, int(d))
        return guess if guess >= today - timedelta(days=30) else date(today.year + 1, month, int(d))
    except (ValueError, KeyError, TypeError):
        return None


def in_text(text: str, today: date, source: str = "") -> list[Deadline]:
    """Deadlines written in a document: a date right after "due", "deadline", "submit by" and the like."""
    from ..security import injection
    out = []
    for m in CUE.finditer(text):
        when = parse_date(m, today)
        if not when:
            continue
        start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
        end = m.end()
        title = " ".join(text[start:end].split()).strip(" -:–")
        if len(title) > 90:
            title = "…" + title[-89:]
        if title and not injection.scan(title):
            out.append(Deadline(title, when, source))
    return out


class Awareness:
    """Owned by the proactive engine. Keeps the focus tracker and the deadlines found in documents."""

    SCHEMA = """CREATE TABLE IF NOT EXISTS deadlines (
        key TEXT PRIMARY KEY, title TEXT NOT NULL, due TEXT NOT NULL, source TEXT NOT NULL, found REAL NOT NULL)"""

    def __init__(self, aether, cfg):
        self.a, self.cfg = aether, cfg
        self.db, self.lock = aether.db, aether.lock
        with self.lock:
            self.db.execute(self.SCHEMA)
            self.db.commit()
        self.focus = FocusTracker()
        self._last_doc_id = 0
        self._last_scan = 0.0

    # ---------------------------------------------------------------- focus breaks
    def focus_nudge(self, now: datetime) -> tuple[str, str, str] | None:
        """(title, body, key) when it is time to suggest a break, else None."""
        every = self.cfg.focus_break_minutes
        if every <= 0 or not self.focus.sample(now.timestamp()):
            return None
        mins = self.focus.minutes(now.timestamp())
        if mins < every:
            return None
        n = int(mins // every)
        app = self.focus.main_app()
        body = (f"You've been at it for {duration(mins)}" + (f", mostly in {app}" if app else "")
                + ". Five minutes away from the screen: stand up, water, look at something far away.")
        return "Time for a break?", body, f"focus:{int(self.focus.start)}:{n}"

    # ---------------------------------------------------------------- deadlines
    def scan_documents(self, today: date, force: bool = False) -> int:
        """Look through newly indexed document chunks for written deadlines. At most every 30 minutes."""
        if not force and time.time() - self._last_scan < 1800:
            return 0
        self._last_scan = time.time()
        store, found = self.a.store, 0
        with self.lock:
            rows = self.db.execute("SELECT id, text, meta FROM memories WHERE deleted=0 AND layer='document' AND id>? "
                                   "ORDER BY id LIMIT 5000", (self._last_doc_id,)).fetchall()
        for mid, blob, meta in rows:
            self._last_doc_id = max(self._last_doc_id, mid)
            try:
                text = store.cipher.decrypt(blob)
            except Exception:
                continue
            if not DEADLINE_WORDS.search(text):
                continue
            path = (json.loads(meta or "{}") or {}).get("path", "a document")
            for d in in_text(text, today, path):
                if d.due >= today:
                    found += self._save(d)
        return found

    def _save(self, d: Deadline) -> int:
        with self.lock:
            cur = self.db.execute("INSERT OR IGNORE INTO deadlines(key, title, due, source, found) VALUES (?,?,?,?,?)",
                                  (d.key, d.title, d.due.isoformat(), d.source, time.time()))
            self.db.commit()
        return cur.rowcount

    def upcoming(self, today: date, days: int = 14) -> list[Deadline]:
        """Every deadline AETHER knows of in the next `days` days, soonest first."""
        end = today + timedelta(days=days)
        out: dict[str, Deadline] = {}
        cal = getattr(self.a, "calendar", None)
        if cal is not None:
            try:
                start = datetime.combine(today, datetime.min.time())
                for e in cal.events(start, datetime.combine(end, datetime.max.time())):
                    if DEADLINE_WORDS.search(e.title):
                        d = Deadline(e.title, e.start.date(), "calendar")
                        out.setdefault(d.key, d)
            except Exception:
                log.exception("deadline scan of the calendar failed")
        for r in self.a.reminders.open():
            try:
                when = date.fromisoformat(str(r.due)[:10])
            except ValueError:
                continue
            if today <= when <= end and DEADLINE_WORDS.search(r.text):
                d = Deadline(r.text, when, "reminder")
                out.setdefault(d.key, d)
        with self.lock:
            rows = self.db.execute("SELECT title, due, source FROM deadlines WHERE due>=? AND due<=? ORDER BY due",
                                   (today.isoformat(), end.isoformat())).fetchall()
        for title, due, source in rows:
            d = Deadline(title, date.fromisoformat(due), source)
            out.setdefault(d.key, d)
        return sorted(out.values(), key=lambda d: (d.due, d.title))

    def deadline_nudges(self, now: datetime) -> list[tuple[str, str, str]]:
        """(title, body, key) for each deadline that has reached one of the lead times."""
        if not self.cfg.deadline_nudges:
            return []
        today = now.date()
        try:
            self.scan_documents(today)
        except Exception:
            log.exception("deadline scan of documents failed")
        leads = sorted({int(x) for x in self.cfg.deadline_lead_days if int(x) >= 0})
        out = []
        for d in self.upcoming(today, max(leads, default=7)):
            left = (d.due - today).days
            lead = next((x for x in leads if left <= x), None)
            if lead is None or (d.source == "reminder" and left == 0):   # on the day, the reminder itself rings
                continue
            when = "today" if left == 0 else "tomorrow" if left == 1 else f"in {left} days ({d.due:%a %d %b})"
            where = "" if d.source in {"calendar", "reminder"} else f" Found in {d.source}."
            out.append((f"Due {when}: {d.title}"[:120],
                        f"{d.title} is due {when}.{where} Want me to block time for it or set a reminder?",
                        f"deadline:{d.key}:{lead}"))
        return out
