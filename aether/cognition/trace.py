"""Glass box: a complete, replayable record of how one answer was made.

Every turn records a timeline of pipeline stages, each model call (purpose,
prompt and output size, duration), tool calls, what was retrieved and why,
which rules fired with their supporting facts, and what the verifier checked.
Traces are stored encrypted with the conversation and shown in the web app's
inspector, so any answer can be audited after the fact.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Iterator

SCHEMA = """CREATE TABLE IF NOT EXISTS traces (
    turn_id INTEGER PRIMARY KEY, session TEXT, created REAL NOT NULL, payload TEXT NOT NULL)"""


class TraceRecorder:
    def __init__(self, text: str):
        self.t0 = time.perf_counter()
        self.started = time.time()
        self.events: list[dict] = []
        self.llm_calls: list[dict] = []
        self.request = text
        self._open: dict[str, float] = {}

    def ms(self) -> float:
        return round((time.perf_counter() - self.t0) * 1000, 1)

    def record(self, kind: str, data: dict) -> None:
        if kind == "token":
            return  # tokens are summarised by the generate stage, not stored one by one
        now = self.ms()
        if kind == "stage":
            name = data.get("name", "?")
            if data.get("done"):
                start = self._open.pop(name, now)
                self.events.append({"kind": "stage", "name": name, "start": start, "end": now, "data": data})
            else:
                self._open[name] = now
        elif kind == "tool" and not data.get("running"):
            self.events.append({"kind": "tool", "name": data.get("tool"), "start": now, "end": now, "data": data})
        elif kind == "replace":
            self.events.append({"kind": "replace", "name": "rewrite", "start": now, "end": now, "data": {}})

    def llm_call(self, purpose: str, prompt_chars: int, start: float, end: float, output_chars: int,
                 first_token_ms: float | None = None) -> None:
        self.llm_calls.append({"purpose": purpose, "prompt_chars": prompt_chars, "output_chars": output_chars,
                               "start": start, "end": end, "first_token_ms": first_token_ms})

    def finish(self, answer: str, state: dict, notices: list[str]) -> dict:
        return {"request": self.request, "started": self.started, "total_ms": self.ms(), "events": self.events,
                "llm_calls": self.llm_calls, "answer": answer, "state": state, "notices": notices,
                "tokens_est": round(sum(c["output_chars"] for c in self.llm_calls) / 4)}


class TracingLLM:
    """Wraps the provider for one turn and times every call (the provider itself is untouched)."""

    def __init__(self, inner, rec: TraceRecorder):
        self.inner, self.rec = inner, rec
        self.name = getattr(inner, "name", "llm")

    @staticmethod
    def _size(messages) -> int:
        return sum(len(getattr(m, "content", "") or "") for m in messages)

    def chat(self, messages, **kw) -> str:
        start = self.rec.ms()
        out = self.inner.chat(messages, **kw)
        schema = kw.get("json_schema")
        purpose = (schema or {}).get("title") or "answer"
        self.rec.llm_call(purpose, self._size(messages), start, self.rec.ms(), len(out or ""))
        return out

    def stream(self, messages, **kw) -> Iterator[str]:
        start, first, n = self.rec.ms(), None, 0
        for tok in self.inner.stream(messages, **kw):
            if first is None:
                first = self.rec.ms() - start
            n += len(tok)
            yield tok
        self.rec.llm_call("answer (streamed)", self._size(messages), start, self.rec.ms(), n, first)

    def __getattr__(self, item):
        return getattr(self.inner, item)


class TraceStore:
    def __init__(self, db: sqlite3.Connection, lock, cipher, keep: int = 500):
        self.db, self.lock, self.cipher, self.keep = db, lock, cipher, keep
        with lock:
            db.execute(SCHEMA)
            db.commit()

    def save(self, turn_id: int, session: str, payload: dict) -> None:
        blob = self.cipher.encrypt(json.dumps(payload, default=str))
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO traces VALUES (?,?,?,?)", (turn_id, session, time.time(), blob))
            self.db.execute("DELETE FROM traces WHERE turn_id NOT IN (SELECT turn_id FROM traces ORDER BY turn_id DESC LIMIT ?)",
                            (self.keep,))
            self.db.commit()

    def get(self, turn_id: int) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT payload FROM traces WHERE turn_id=?", (turn_id,)).fetchone()
        return json.loads(self.cipher.decrypt(row[0])) if row else None

    def recent(self, limit: int = 30) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT turn_id, session, created, payload FROM traces ORDER BY turn_id DESC LIMIT ?",
                                   (limit,)).fetchall()
        out = []
        for tid, s, c, p in rows:
            d = json.loads(self.cipher.decrypt(p))
            out.append({"turn_id": tid, "session": s, "created": c, "request": d["request"][:120],
                        "total_ms": d["total_ms"], "llm_calls": len(d["llm_calls"])})
        return out
