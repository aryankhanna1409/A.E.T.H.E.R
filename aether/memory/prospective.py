"""Prospective memory: reminders, follow-ups and deadlines with real dates."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date

from ..neurosymbolic.temporal import resolve


@dataclass
class ReminderItem:
    id: int
    text: str
    due: str  # ISO date
    status: str  # open | done | dismissed


class Reminders:
    def __init__(self, db: sqlite3.Connection, lock: threading.Lock):
        self.db, self.lock = db, lock
        with lock:
            db.execute("""CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY, text TEXT, due TEXT, status TEXT DEFAULT 'open', created REAL, notified REAL)""")
            db.commit()

    def add(self, text: str, when: str) -> ReminderItem:
        d = resolve(when) or resolve("tomorrow")
        with self.lock:
            dup = self.db.execute("SELECT id FROM reminders WHERE text=? AND due=? AND status='open'",
                                  (text, d.isoformat())).fetchone()
            if dup:
                rid = dup[0]
            else:
                rid = self.db.execute("INSERT INTO reminders(text,due,created) VALUES (?,?,?)",
                                      (text, d.isoformat(), time.time())).lastrowid
                self.db.commit()
        return ReminderItem(rid, text, d.isoformat(), "open")

    def due(self, on: date | None = None) -> list[ReminderItem]:
        day = (on or date.today()).isoformat()
        rows = self.db.execute("SELECT id,text,due,status FROM reminders WHERE status='open' AND due<=? ORDER BY due",
                               (day,)).fetchall()
        return [ReminderItem(*r) for r in rows]

    def open(self) -> list[ReminderItem]:
        rows = self.db.execute("SELECT id,text,due,status FROM reminders WHERE status='open' ORDER BY due").fetchall()
        return [ReminderItem(*r) for r in rows]

    def mark_notified(self, rid: int) -> None:
        with self.lock:
            self.db.execute("UPDATE reminders SET notified=? WHERE id=?", (time.time(), rid))
            self.db.commit()

    def set_status(self, rid: int, status: str) -> None:
        with self.lock:
            self.db.execute("UPDATE reminders SET status=? WHERE id=?", (status, rid))
            self.db.commit()
