"""Knowledge graph: typed triples with provenance, confidence and time.

Functional predicates (a subject can have only one value at a time, such as
`lives_in` or `birthday`) give the symbolic layer something concrete to check:
a new value that disagrees with a stored one is reported as a contradiction
instead of silently overwriting it.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass

FUNCTIONAL = {"lives_in", "birthday", "age", "name", "job_title", "works_at", "born_in", "timezone",
              "partner", "nationality", "studies_at", "email"}
SYMMETRIC = {"sibling_of", "friend_of", "married_to", "colleague_of"}
INVERSES = {"parent_of": "child_of", "child_of": "parent_of", "manages": "managed_by", "managed_by": "manages"}


def norm(term: str) -> str:
    return re.sub(r"\s+", "_", term.strip().lower())


@dataclass(frozen=True)
class Triple:
    s: str
    p: str
    o: str
    source: str = "user_stated"
    confidence: float = 0.9
    id: int | None = None

    def key(self) -> tuple[str, str, str]:
        return (self.s, self.p, self.o)

    def __str__(self) -> str:
        return f"{self.s} {self.p} {self.o}"


@dataclass
class Contradiction:
    new: Triple
    existing: Triple
    reason: str


class KnowledgeGraph:
    def __init__(self, db: sqlite3.Connection, lock: threading.Lock):
        self.db, self.lock = db, lock
        with lock:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS triples (
                id INTEGER PRIMARY KEY, s TEXT, p TEXT, o TEXT, source TEXT, confidence REAL,
                created REAL, valid_to REAL, derived INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS idx_spo ON triples(s, p, o);
            """)
            db.commit()

    def assert_fact(self, s: str, p: str, o: str, source: str = "user_stated", confidence: float = 0.9,
                    derived: bool = False, replace: bool = False) -> list[Contradiction]:
        """Add a fact. Returns contradictions; with replace=True, a functional
        predicate's old value is retired (kept for history) instead of conflicting."""
        t = Triple(norm(s), norm(p), norm(o), source, confidence)
        conflicts = self.check(t)
        if conflicts and not replace:
            if source in {"user_stated", "user_approved"} and all(
                    c.existing.source not in {"user_stated", "user_approved"} for c in conflicts):
                replace = True  # what the user says outranks what was inferred or read
            else:
                return conflicts
        now = time.time()
        with self.lock:
            if replace and t.p in FUNCTIONAL:
                self.db.execute("UPDATE triples SET valid_to=? WHERE s=? AND p=? AND valid_to IS NULL", (now, t.s, t.p))
            exists = self.db.execute("SELECT id FROM triples WHERE s=? AND p=? AND o=? AND valid_to IS NULL",
                                     t.key()).fetchone()
            if exists:
                self.db.execute("UPDATE triples SET confidence=MAX(confidence, ?) WHERE id=?", (confidence, exists[0]))
            else:
                self.db.execute("INSERT INTO triples(s,p,o,source,confidence,created,derived) VALUES (?,?,?,?,?,?,?)",
                                (*t.key(), source, confidence, now, int(derived)))
            self.db.commit()
        return []

    def check(self, t: Triple) -> list[Contradiction]:
        out = []
        if t.p in FUNCTIONAL:
            for e in self.query(t.s, t.p, None):
                if e.o != t.o:
                    out.append(Contradiction(t, e, f"{t.p} can only have one value for {t.s}"))
        neg = "not_" + t.p if not t.p.startswith("not_") else t.p[4:]
        for e in self.query(t.s, neg, t.o):
            out.append(Contradiction(t, e, "directly negated by a stored fact"))
        return out

    def query(self, s: str | None, p: str | None, o: str | None, include_derived: bool = True) -> list[Triple]:
        q = "SELECT id,s,p,o,source,confidence FROM triples WHERE valid_to IS NULL"
        args = []
        for col, val in (("s", s), ("p", p), ("o", o)):
            if val is not None:
                q += f" AND {col}=?"
                args.append(norm(val))
        if not include_derived:
            q += " AND derived=0"
        return [Triple(r[1], r[2], r[3], r[4], r[5], r[0]) for r in self.db.execute(q, args).fetchall()]

    def about(self, entity: str, limit: int = 30) -> list[Triple]:
        e = norm(entity)
        rows = self.db.execute("SELECT id,s,p,o,source,confidence FROM triples WHERE valid_to IS NULL AND "
                               "(s=? OR o=?) LIMIT ?", (e, e, limit)).fetchall()
        return [Triple(r[1], r[2], r[3], r[4], r[5], r[0]) for r in rows]

    def entities(self) -> set[str]:
        rows = self.db.execute("SELECT s, o FROM triples WHERE valid_to IS NULL").fetchall()
        return {x for r in rows for x in r}

    def retract(self, triple_id: int) -> None:
        with self.lock:
            self.db.execute("UPDATE triples SET valid_to=? WHERE id=?", (time.time(), triple_id))
            self.db.commit()

    def all(self) -> list[Triple]:
        return self.query(None, None, None)
