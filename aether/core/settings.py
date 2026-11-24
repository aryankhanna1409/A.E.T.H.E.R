"""Learned setting overrides. Tuning and approved self-improvements write here,
never to aether.toml, so the user's file stays theirs and every change can be
listed and reverted. Overrides are applied on top of the loaded config."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA = """CREATE TABLE IF NOT EXISTS setting_overrides (
    section TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, previous TEXT NOT NULL,
    reason TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(section, key))"""

# Only these may be changed automatically; security settings never are.
TUNABLE = {
    "memory": {"hybrid_alpha", "rag_min_score", "rag_top_k", "dedupe_threshold"},
    "context": {"topic_switch_threshold", "working_memory_turns"},
    "llm": {"temperature"},
}


class SettingsStore:
    def __init__(self, db: sqlite3.Connection, lock: threading.RLock, cfg):
        self.db, self.lock, self.cfg = db, lock, cfg
        with lock:
            db.execute(SCHEMA)
            db.commit()
        for row in self.list():  # re-apply on start
            self._set(row["section"], row["key"], row["value"])

    def _set(self, section: str, key: str, value) -> None:
        obj = getattr(self.cfg, section)
        current = getattr(obj, key)
        setattr(obj, key, type(current)(value))

    def apply(self, section: str, values: dict, reason: str) -> list[str]:
        allowed = TUNABLE.get(section, set())
        bad = [k for k in values if k not in allowed]
        if bad:
            raise PermissionError(f"not auto-tunable: {section}.{', '.join(bad)}")
        changed = []
        with self.lock:
            for k, v in values.items():
                prev = getattr(getattr(self.cfg, section), k)
                row = self.db.execute("SELECT previous FROM setting_overrides WHERE section=? AND key=?",
                                      (section, k)).fetchone()
                original = json.loads(row[0]) if row else prev
                self._set(section, k, v)
                self.db.execute("INSERT OR REPLACE INTO setting_overrides VALUES (?,?,?,?,?,?)",
                                (section, k, json.dumps(v), json.dumps(original), reason, time.time()))
                changed.append(f"{section}.{k}: {prev} -> {v}")
            self.db.commit()
        return changed

    def revert(self, section: str, key: str) -> str:
        with self.lock:
            row = self.db.execute("SELECT previous FROM setting_overrides WHERE section=? AND key=?",
                                  (section, key)).fetchone()
            if not row:
                return "No override for that setting."
            self._set(section, key, json.loads(row[0]))
            self.db.execute("DELETE FROM setting_overrides WHERE section=? AND key=?", (section, key))
            self.db.commit()
        return f"{section}.{key} restored to {json.loads(row[0])}."

    def list(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT section, key, value, previous, reason, updated FROM setting_overrides").fetchall()
        return [{"section": s, "key": k, "value": json.loads(v), "previous": json.loads(p), "reason": r, "updated": u}
                for s, k, v, p, r, u in rows]
