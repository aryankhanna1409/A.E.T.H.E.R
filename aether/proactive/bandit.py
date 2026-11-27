"""Nudges that learn: AETHER keeps the kinds of nudges, at the times of day, that you actually act on.

Each optional nudge kind (break, rest, deadline, check-in, news, proposal ...) at each part of the day (morning,
afternoon, evening, night) is a Beta-Bernoulli arm. Acting on a nudge (clicking it, or doing what it said) is a
success; dismissing it is a failure; leaving it untouched for half a day counts as half a failure. Before a nudge
is shown, a sample is drawn from its arm (Thompson sampling): kinds and times you keep dismissing get shown less
and less, but are still tried now and then in case things changed, and anything new starts optimistic.

Never held back: timers, alarms, reminders, calendar alerts, the morning brief, battery and crisis resources.
"""
from __future__ import annotations

import random
import sqlite3
import threading
import time
from datetime import datetime

LEARNED = {"break", "rest", "deadline", "checkin", "news", "proposal", "goal", "lesson", "learning", "review"}
SCHEMA = """CREATE TABLE IF NOT EXISTS nudge_arms (kind TEXT, part TEXT, a REAL DEFAULT 2.0, b REAL DEFAULT 1.0,
            PRIMARY KEY (kind, part));
CREATE TABLE IF NOT EXISTS nudge_outcomes (nid INTEGER PRIMARY KEY, kind TEXT, part TEXT, outcome TEXT, at REAL);"""


def part_of_day(when: datetime) -> str:
    h = when.hour
    return "morning" if 5 <= h < 12 else "afternoon" if h < 17 else "evening" if h < 22 else "night"


class NudgePolicy:
    def __init__(self, db: sqlite3.Connection, lock: threading.Lock, floor: float = 0.3, rng: random.Random | None = None):
        self.db, self.lock, self.floor = db, lock, floor
        self.rng = rng or random.Random()
        with lock:
            db.executescript(SCHEMA)
            db.commit()

    def arm(self, kind: str, part: str) -> tuple[float, float]:
        r = self.db.execute("SELECT a, b FROM nudge_arms WHERE kind=? AND part=?", (kind, part)).fetchone()
        return (r[0], r[1]) if r else (2.0, 1.0)

    def allow(self, kind: str, when: datetime) -> bool:
        """Show this nudge now? Always yes for kinds that aren't learned."""
        if kind not in LEARNED:
            return True
        a, b = self.arm(kind, part_of_day(when))
        if a + b <= 3.0:
            return True   # no outcomes yet for this kind at this time of day: always show it until there's evidence
        return self.rng.betavariate(a, b) >= self.floor

    def _update(self, nid: int, kind: str, part: str, outcome: str, da: float, db_: float) -> None:
        if kind not in LEARNED:
            return
        with self.lock:
            if self.db.execute("SELECT 1 FROM nudge_outcomes WHERE nid=?", (nid,)).fetchone():
                return   # one outcome per nudge
            self.db.execute("INSERT INTO nudge_outcomes VALUES (?,?,?,?,?)", (nid, kind, part, outcome, time.time()))
            self.db.execute("INSERT INTO nudge_arms(kind, part, a, b) VALUES (?, ?, ?, ?) ON CONFLICT(kind, part) "
                            "DO UPDATE SET a = a + ?, b = b + ?", (kind, part, 2.0 + da, 1.0 + db_, da, db_))
            self.db.commit()

    def acted(self, nid: int, kind: str, delivered: float) -> None:
        self._update(nid, kind, part_of_day(datetime.fromtimestamp(delivered)), "acted", 1.0, 0.0)

    def dismissed(self, nid: int, kind: str, delivered: float) -> None:
        self._update(nid, kind, part_of_day(datetime.fromtimestamp(delivered)), "dismissed", 0.0, 1.0)

    def ignored(self, nid: int, kind: str, delivered: float) -> None:
        self._update(nid, kind, part_of_day(datetime.fromtimestamp(delivered)), "ignored", 0.0, 0.5)

    def table(self) -> list[dict]:
        rows = self.db.execute("SELECT kind, part, a, b FROM nudge_arms ORDER BY kind, part").fetchall()
        return [{"kind": k, "part": p, "rate": round(a / (a + b), 2), "seen": round(a + b - 3, 1)} for k, p, a, b in rows]
