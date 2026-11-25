"""Prospective memory: your to-do list and reminders, with real dates.

A reminder can repeat ("every day", "weekdays", "every monday", "every 3 days") and can have a time of day
("at 9pm"). Ticking off a repeating one moves it to its next day instead of closing it, so a daily habit stays
on the list. A reminder with a time rings like an alarm at that time; one without shows up on the day.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta

from ..neurosymbolic.temporal import resolve

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = {d: n for d, n in zip(DAYS, ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))}


@dataclass
class ReminderItem:
    id: int
    text: str
    due: str  # ISO date
    status: str  # open | done | dismissed
    repeat: str = ""  # "" | daily | weekdays | weekends | weekly:mon | every:3
    at: str = ""  # "" or HH:MM

    def when_text(self) -> str:
        return " ".join(p for p in (describe_repeat(self.repeat), f"at {self.at}" if self.at else "") if p)


def parse_repeat(text: str) -> str:
    """'every day' -> daily, 'every weekday' -> weekdays, 'every monday' -> weekly:mon, 'every 3 days' -> every:3.
    Returns '' when the words don't describe a repeat."""
    t = text.lower().strip()
    if re.search(r"\b(daily|every\s*day|everyday|each\s+day|every\s+(?:morning|night|evening|afternoon)|"
                 r"each\s+(?:morning|night|evening)|(?:a|per)\s+day|day\s+by\s+day)\b", t):
        return "daily"
    if re.search(r"\b(every\s+)?week\s*days\b", t) or "weekday" in t:
        return "weekdays"
    if re.search(r"\b(every\s+)?weekends?\b", t):
        return "weekends"
    m = re.search(r"\bevery\s+(\d+)\s+days?\b", t)
    if m:
        n = int(m.group(1))
        return "daily" if n == 1 else f"every:{n}"
    m = re.search(r"\b(?:every|each|on)\s+(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", t)
    if m:
        return f"weekly:{m.group(1)}"
    if re.search(r"\b(weekly|every\s+week|once\s+a\s+week)\b", t):
        return f"weekly:{DAYS[date.today().weekday()]}"
    return ""


def describe_repeat(repeat: str) -> str:
    if not repeat:
        return ""
    if repeat in {"daily", "weekdays", "weekends"}:
        return {"daily": "every day", "weekdays": "every weekday", "weekends": "every weekend"}[repeat]
    kind, _, arg = repeat.partition(":")
    if kind == "weekly":
        return f"every {DAY_NAMES.get(arg, arg)}"
    if kind == "every":
        return f"every {arg} days"
    return repeat


def occurs_on(repeat: str, day: date, start: date | None = None) -> bool:
    if repeat == "daily":
        return True
    if repeat == "weekdays":
        return day.weekday() < 5
    if repeat == "weekends":
        return day.weekday() >= 5
    kind, _, arg = repeat.partition(":")
    if kind == "weekly":
        return DAYS[day.weekday()] == arg
    if kind == "every" and arg.isdigit():
        return ((day - (start or day)).days % int(arg)) == 0
    return False


def next_due(repeat: str, after: date, start: date | None = None) -> date:
    """The first day after `after` that the repeat falls on."""
    day = after + timedelta(days=1)
    for _ in range(400):
        if occurs_on(repeat, day, start or after):
            return day
        day += timedelta(days=1)
    return after + timedelta(days=1)


def first_due(repeat: str, today: date) -> date:
    return today if occurs_on(repeat, today, today) else next_due(repeat, today, today)


def parse_at(text: str) -> str:
    """'at 9pm' / '21:30' / 'at 7' / 'every morning' -> 'HH:MM', or ''."""
    t = text.lower()
    m = re.search(r"\b(?:at\s+)?(\d{1,2})(?::|\.)?(\d{2})?\s*(am|pm|a\.m\.|p\.m\.)\b", t) or \
        re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\b(?!\s*(?:days?|weeks?|minutes?|hours?|%))", t) or \
        re.search(r"\b(\d{1,2}):(\d{2})\b()", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        ampm = (m.group(3) or "").replace(".", "") if m.lastindex and m.lastindex >= 3 else ""
        if ampm:
            h = h % 12 + (12 if ampm == "pm" else 0)
        if h <= 23 and mi <= 59:
            return f"{h:02d}:{mi:02d}"
    for word, hm in (("morning", "08:00"), ("afternoon", "14:00"), ("evening", "19:00"), ("night", "21:00"),
                     ("tonight", "21:00"), ("noon", "12:00"), ("midnight", "00:00")):
        if re.search(rf"\b{word}\b", t):
            return hm
    return ""


class Reminders:
    def __init__(self, db: sqlite3.Connection, lock: threading.Lock):
        self.db, self.lock = db, lock
        with lock:
            db.execute("""CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY, text TEXT, due TEXT, status TEXT DEFAULT 'open', created REAL, notified REAL)""")
            cols = {r[1] for r in db.execute("PRAGMA table_info(reminders)")}
            if "repeat" not in cols:
                db.execute("ALTER TABLE reminders ADD COLUMN repeat TEXT DEFAULT ''")
            if "at" not in cols:
                db.execute("ALTER TABLE reminders ADD COLUMN at TEXT DEFAULT ''")
            db.commit()

    _COLS = "id,text,due,status,COALESCE(repeat,''),COALESCE(at,'')"

    def add(self, text: str, when: str = "", repeat: str = "", at: str = "", today: date | None = None) -> ReminderItem:
        today = today or date.today()
        if repeat:
            d = resolve(when, today) if when and when not in {"today", "tomorrow"} else None
            d = d or (first_due(repeat, today) if when != "tomorrow" else next_due(repeat, today, today))
        else:
            d = resolve(when, today) if when else (today if at else None)
            d = d or resolve("tomorrow", today)
        text = text.strip()
        with self.lock:
            dup = self.db.execute("SELECT id FROM reminders WHERE lower(text)=lower(?) AND status='open' AND "
                                  "(due=? OR COALESCE(repeat,'')<>'')", (text, d.isoformat())).fetchone()
            if dup:
                rid = dup[0]
                if repeat or at:
                    self.db.execute("UPDATE reminders SET repeat=?, at=? WHERE id=?", (repeat, at, rid))
            else:
                rid = self.db.execute("INSERT INTO reminders(text,due,created,repeat,at) VALUES (?,?,?,?,?)",
                                      (text, d.isoformat(), time.time(), repeat, at)).lastrowid
            self.db.commit()
        return self.get(rid)

    def get(self, rid: int) -> ReminderItem | None:
        with self.lock:
            r = self.db.execute(f"SELECT {self._COLS} FROM reminders WHERE id=?", (rid,)).fetchone()
        return ReminderItem(*r) if r else None

    def _roll(self, today: date) -> None:
        """A repeating item you didn't tick off isn't "overdue since Monday": it moves to its latest day."""
        with self.lock:
            rows = self.db.execute("SELECT id, due, repeat FROM reminders WHERE status='open' AND COALESCE(repeat,'')<>'' "
                                   "AND due<?", (today.isoformat(),)).fetchall()
            for rid, due, repeat in rows:
                d = date.fromisoformat(due)
                new = first_due(repeat, today) if repeat in {"daily", "weekdays", "weekends"} or repeat.startswith("weekly") \
                    else next_due(repeat, today - timedelta(days=1), d)
                self.db.execute("UPDATE reminders SET due=?, notified=NULL WHERE id=?", (new.isoformat(), rid))
            if rows:
                self.db.commit()

    def due(self, on: date | None = None) -> list[ReminderItem]:
        self._roll(on or date.today())
        day = (on or date.today()).isoformat()
        with self.lock:
            rows = self.db.execute(f"SELECT {self._COLS} FROM reminders WHERE status='open' AND due<=? ORDER BY due, at",
                                   (day,)).fetchall()
        return [ReminderItem(*r) for r in rows]

    def open(self) -> list[ReminderItem]:
        self._roll(date.today())
        with self.lock:
            rows = self.db.execute(f"SELECT {self._COLS} FROM reminders WHERE status='open' ORDER BY due, at").fetchall()
        return [ReminderItem(*r) for r in rows]

    def last(self) -> ReminderItem | None:
        """The one you added most recently (for "make it a daily thing")."""
        with self.lock:
            r = self.db.execute(f"SELECT {self._COLS} FROM reminders WHERE status='open' ORDER BY created DESC, id DESC "
                                "LIMIT 1").fetchone()
        return ReminderItem(*r) if r else None

    def find(self, words: str) -> ReminderItem | None:
        """The open item that best matches a few words of its text ("the milk one", "drink water")."""
        def stems(text: str) -> set[str]:
            return {re.sub(r"(?:ing|ed|es|s)$", "", w) if len(w) > 4 else w for w in re.findall(r"[a-z0-9]+", text.lower())}
        want = stems(words) - {"the", "a", "an", "my", "to", "one", "task", "todo",
                                                               "reminder", "item", "it", "that", "this"}
        if not want:
            return None
        best, score = None, 0.0
        for item in self.open():
            have = stems(item.text)
            s = len(want & have) / len(want)
            if s > score:
                best, score = item, s
        return best if score >= 0.5 else None

    def make_repeating(self, rid: int, repeat: str, at: str = "", today: date | None = None) -> ReminderItem | None:
        today = today or date.today()
        item = self.get(rid)
        if item is None:
            return None
        due = date.fromisoformat(item.due)
        if not occurs_on(repeat, due, due):
            due = first_due(repeat, today)
        with self.lock:
            self.db.execute("UPDATE reminders SET repeat=?, at=?, due=?, status='open' WHERE id=?",
                            (repeat, at or item.at, due.isoformat(), rid))
            self.db.commit()
        return self.get(rid)

    def mark_notified(self, rid: int) -> None:
        with self.lock:
            self.db.execute("UPDATE reminders SET notified=? WHERE id=?", (time.time(), rid))
            self.db.commit()

    def set_status(self, rid: int, status: str, today: date | None = None) -> ReminderItem | None:
        """Done on a repeating item moves it to its next day; dismissed stops it for good."""
        item = self.get(rid)
        with self.lock:
            if item and item.repeat and status == "done":
                start = max(date.fromisoformat(item.due), today or date.today())
                nxt = next_due(item.repeat, start, date.fromisoformat(item.due))
                self.db.execute("UPDATE reminders SET due=?, notified=NULL WHERE id=?", (nxt.isoformat(), rid))
            else:
                self.db.execute("UPDATE reminders SET status=? WHERE id=?", (status, rid))
            self.db.commit()
        return self.get(rid)
