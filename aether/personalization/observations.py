"""Behavioral observations: patterns AETHER notices about how Aryan works and
feels, kept separate from facts.

Every observation has evidence and a confidence, starts as 'proposed', and is
only used to adapt tone once you confirm it. Nothing here is a diagnosis, and
sensitive traits are not inferred.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

SENSITIVE = ("diagnos", "disorder", "adhd", "autis", "bipolar", "depress", "religio", "sexual", "politic", "ethnic")


@dataclass
class Observation:
    id: int
    kind: str
    text: str
    evidence: list[str]
    confidence: float
    status: str  # proposed | confirmed | rejected


class Observations:
    def __init__(self, db: sqlite3.Connection, lock: threading.Lock):
        self.db, self.lock = db, lock
        with lock:
            db.execute("""CREATE TABLE IF NOT EXISTS observations (id INTEGER PRIMARY KEY, kind TEXT, text TEXT UNIQUE,
                evidence TEXT, confidence REAL, status TEXT DEFAULT 'proposed', created REAL, updated REAL)""")
            db.commit()

    def propose(self, kind: str, text: str, evidence: list[str], confidence: float) -> int | None:
        if any(w in text.lower() for w in SENSITIVE):
            return None  # never infer sensitive traits
        with self.lock:
            row = self.db.execute("SELECT id, status, evidence FROM observations WHERE text=?", (text,)).fetchone()
            if row:
                if row[1] == "rejected":
                    return None  # respect a rejection
                ev = (json.loads(row[2]) + evidence)[-8:]
                self.db.execute("UPDATE observations SET evidence=?, confidence=MAX(confidence, ?), updated=? WHERE id=?",
                                (json.dumps(ev), confidence, time.time(), row[0]))
                self.db.commit()
                return row[0]
            oid = self.db.execute("INSERT INTO observations(kind,text,evidence,confidence,created,updated) VALUES (?,?,?,?,?,?)",
                                  (kind, text, json.dumps(evidence[-8:]), confidence, time.time(), time.time())).lastrowid
            self.db.commit()
            return oid

    def set_status(self, oid: int, status: str) -> None:
        with self.lock:
            self.db.execute("UPDATE observations SET status=?, updated=? WHERE id=?", (status, time.time(), oid))
            self.db.commit()

    def delete(self, oid: int) -> None:
        with self.lock:
            self.db.execute("DELETE FROM observations WHERE id=?", (oid,))
            self.db.commit()

    def all(self, status: str | None = None) -> list[Observation]:
        q = "SELECT id,kind,text,evidence,confidence,status FROM observations"
        rows = self.db.execute(q + (" WHERE status=?" if status else "") + " ORDER BY updated DESC",
                               (status,) if status else ()).fetchall()
        return [Observation(r[0], r[1], r[2], json.loads(r[3]), r[4], r[5]) for r in rows]

    def prompt_block(self) -> str:
        conf = self.all("confirmed")
        if not conf:
            return ""
        return ("Patterns the user confirmed about themselves (use only to communicate more helpfully, never to "
                "pressure or manipulate):\n" + "\n".join(f"- {o.text}" for o in conf[:10]))


def analyse(db: sqlite3.Connection, obs: Observations, min_events: int = 3, decrypt=lambda s: s) -> list[int]:
    """Deterministic pattern mining over turns and mood observations."""
    proposed: list[int] = []
    moods = db.execute("SELECT text, meta, created FROM memories WHERE layer='observation' AND deleted=0 "
                       "AND json_extract(meta,'$.kind')='mood'").fetchall()
    if len(moods) >= min_events:
        hours = Counter(datetime.fromtimestamp(c).hour // 3 for _, _, c in moods)
        slot, n = hours.most_common(1)[0]
        if n >= min_events and n / len(moods) >= 0.5:
            span = f"{slot * 3:02d}:00-{slot * 3 + 3:02d}:00"
            oid = obs.propose("stress_timing", f"Often feels stressed around {span}",
                              [f"{n} of {len(moods)} stress signals fell in that window"], min(0.9, 0.4 + n * 0.1))
            if oid:
                proposed.append(oid)
        topics = Counter()
        for text, _, _ in moods:
            about = decrypt(text).split("about:", 1)[-1].strip().lower()
            for word in ("exam", "deadline", "work", "project", "money", "sleep", "family", "college", "job"):
                if word in about:
                    topics[word] += 1
        for word, k in topics.items():
            if k >= min_events:
                oid = obs.propose("stressor", f"'{word}' is a recurring source of stress",
                                  [f"mentioned in {k} stressful moments"], min(0.9, 0.4 + k * 0.1))
                if oid:
                    proposed.append(oid)
    rows = db.execute("SELECT text FROM turns WHERE role='user' ORDER BY id DESC LIMIT 200").fetchall()
    late = db.execute("SELECT COUNT(*) FROM turns WHERE role='user' AND CAST(strftime('%H', created, 'unixepoch', "
                      "'localtime') AS INTEGER) BETWEEN 0 AND 4").fetchone()[0]
    if rows and late >= max(min_events, len(rows) // 3):
        oid = obs.propose("routine", "Often works late at night (after midnight)",
                          [f"{late} of the last {len(rows)} messages were sent between 00:00 and 05:00"], 0.6)
        if oid:
            proposed.append(oid)
    return proposed
