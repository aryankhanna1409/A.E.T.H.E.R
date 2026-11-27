"""Memory nets on top of the store: working memory and a reranker that learns which memories actually help.

AETHER's memory is several nets, like a brain:
* working memory (here): what's live right now (the time, the app in front of you, how long you've been at it,
  this week's focus, what he just did, a question he's waiting on you to answer), put in every prompt,
* episodic (chats and events, searchable by time: memory/recall.py) and semantic (facts and the knowledge graph),
* procedural (routines, "how Aryan does things") and prospective (reminders, deadlines, watches),
* consolidation while you sleep (metacognition/learning.py: lessons distilled, duplicates merged, weak inferred
  memories fade, new rules and habits mined for your OK).

The learned reranker: every time AETHER retrieves memories for an answer, it notes which ones the answer actually
used (cited, or clearly drawn on) and which were noise. Once it has enough examples, a small logistic model over
cheap features (search score, rank, layer, age, confidence, how often it's been useful before) reorders what the
search finds, so memories that tend to help come first. It never adds anything the search didn't find.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from datetime import datetime

from ..retrieval.lexical import tokens

LAYERS = ["semantic", "episodic", "procedural", "prospective", "preference", "document"]
SCHEMA = """CREATE TABLE IF NOT EXISTS rerank_examples (id INTEGER PRIMARY KEY, x TEXT, y REAL, at REAL);
CREATE TABLE IF NOT EXISTS rerank_model (id INTEGER PRIMARY KEY CHECK (id = 1), w TEXT, n INTEGER, auc REAL, at REAL);"""


def features(m, rank: int, now: float) -> list[float]:
    age_days = max(0.0, (now - getattr(m, "created", now)) / 86400)
    uses = float((getattr(m, "meta", {}) or {}).get("useful", 0))
    x = [1.0, float(m.score), 1.0 / (1 + rank), math.log1p(age_days) / 6, float(m.confidence), math.log1p(uses)]
    x += [1.0 if m.layer == lay else 0.0 for lay in LAYERS]
    return x


def used(answer: str, i: int, text: str) -> bool:
    """Did the answer draw on memory i? Cited as [memory i], or shares most of its content words."""
    if re.search(rf"\[memory\s*{i}\]", answer, re.I):
        return True
    mt = set(tokens(text))
    if len(mt) < 3:
        return False
    return len(mt & set(tokens(answer))) / len(mt) >= 0.6


def _sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def auc(scores: list[float], ys: list[float]) -> float:
    pos = [s for s, y in zip(scores, ys) if y > 0.5]
    neg = [s for s, y in zip(scores, ys) if y <= 0.5]
    if not pos or not neg:
        return 0.5
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


class MemoryReranker:
    def __init__(self, db: sqlite3.Connection, lock: threading.Lock, min_examples: int = 150, min_auc: float = 0.62):
        self.db, self.lock = db, lock
        self.min_examples, self.min_auc = min_examples, min_auc
        self.w: list[float] | None = None
        self.auc = 0.0
        self.n = 0
        with lock:
            db.executescript(SCHEMA)
            db.commit()
            r = db.execute("SELECT w, n, auc FROM rerank_model WHERE id=1").fetchone()
        if r:
            self.w, self.n, self.auc = json.loads(r[0]), r[1], r[2]

    @property
    def active(self) -> bool:
        return self.w is not None and self.auc >= self.min_auc

    def observe(self, memories: list, answer: str, now: float | None = None) -> int:
        """Record which retrieved memories the answer used. Returns how many examples were added."""
        if not memories or not answer:
            return 0
        now = now or time.time()
        rows = []
        for i, m in enumerate(memories, 1):
            rows.append((json.dumps([round(v, 4) for v in features(m, i - 1, now)]), 1.0 if used(answer, i, m.text) else 0.0, now))
        with self.lock:
            self.db.executemany("INSERT INTO rerank_examples(x, y, at) VALUES (?,?,?)", rows)
            self.db.commit()
            total = self.db.execute("SELECT COUNT(*) FROM rerank_examples").fetchone()[0]
        if total >= self.min_examples and total - self.n >= 100:
            threading.Thread(target=self.train, daemon=True).start()
        return len(rows)

    def train(self, epochs: int = 300, lr: float = 0.3, l2: float = 1e-3) -> dict:
        with self.lock:
            rows = self.db.execute("SELECT x, y FROM rerank_examples ORDER BY id DESC LIMIT 20000").fetchall()
        if len(rows) < self.min_examples:
            return {"trained": False, "examples": len(rows)}
        X = [json.loads(r[0]) for r in rows]
        Y = [r[1] for r in rows]
        if len({y > 0.5 for y in Y}) < 2:
            return {"trained": False, "examples": len(rows), "reason": "only one kind of outcome so far"}
        d = len(X[0])
        cut = max(1, len(X) // 5)               # newest fifth held out for an honest AUC
        trX, trY, teX, teY = X[cut:], Y[cut:], X[:cut], Y[:cut]
        w = [0.0] * d
        pos = sum(trY) or 1.0
        neg = (len(trY) - sum(trY)) or 1.0
        for _ in range(epochs):                 # class-balanced logistic regression, plain gradient descent
            g = [0.0] * d
            for x, y in zip(trX, trY):
                p = _sigmoid(sum(a * b for a, b in zip(w, x)))
                c = (p - y) * (0.5 / pos if y > 0.5 else 0.5 / neg)
                for j in range(d):
                    g[j] += c * x[j]
            w = [wj - lr * (gj + l2 * wj) for wj, gj in zip(w, g)]
        score = auc([sum(a * b for a, b in zip(w, x)) for x in teX], teY)
        self.w, self.auc, self.n = w, score, len(rows)
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO rerank_model(id, w, n, auc, at) VALUES (1, ?, ?, ?, ?)",
                            (json.dumps(w), len(rows), score, time.time()))
            self.db.commit()
        return {"trained": True, "examples": len(rows), "auc": round(score, 3), "active": self.active}

    def rerank(self, memories: list, k: int | None = None, now: float | None = None) -> list:
        """Reorder search hits by how likely each is to help (only once the model has proven itself)."""
        if not self.active or len(memories) < 2:
            return memories[:k] if k else memories
        now = now or time.time()
        scored = [(_sigmoid(sum(a * b for a, b in zip(self.w, features(m, i, now)))), i, m)
                  for i, m in enumerate(memories)]
        scored.sort(key=lambda t: (-t[0], t[1]))
        out = [m for _, _, m in scored]
        return out[:k] if k else out

    def info(self) -> dict:
        with self.lock:
            n = self.db.execute("SELECT COUNT(*) FROM rerank_examples").fetchone()[0]
            pos = self.db.execute("SELECT COALESCE(SUM(y), 0) FROM rerank_examples").fetchone()[0]
        return {"examples": n, "useful": int(pos), "auc": round(self.auc, 3), "active": self.active}


class WorkingMemory:
    """What's live right now, as a short block for the prompt."""

    def __init__(self, aether):
        self.a = aether
        self.recent_tools: list[str] = []
        self.open_question = ""

    def note_reply(self, reply: str, tool_log: list[str]) -> None:
        tools = [t.split("(")[0].split(" ")[0] for t in tool_log if t and "->" in t]
        self.recent_tools = (tools + self.recent_tools)[:4]
        r = (reply or "").strip()
        last = re.split(r"(?<=[.!?])\s+", r)[-1] if r else ""
        self.open_question = last[:160] if last.endswith("?") else ""

    def lines(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now()
        out = []
        pe = getattr(self.a, "proactive", None)
        try:
            f = pe.awareness.focus
            app = f.main_app()
            mins = f.minutes(now.timestamp())
            hours = f.hours_on(now.date().isoformat())
            bits = []
            if app:
                bits.append(f"mostly in {app}")
            if hours >= 0.25:
                bits.append(f"{hours:.1f} h at the keyboard today")
            if mins:
                bits.append(f"current stretch {mins:.0f} min")
            if bits:
                out.append("Right now: " + ", ".join(bits) + ".")
        except Exception:
            pass
        try:
            t = self.a.plans.today(now.date())
            if t and t.get("focus"):
                out.append("This week's focus: " + "; ".join(t["focus"][:3]) + ".")
        except Exception:
            pass
        world = getattr(self.a, "world", None)
        if world is not None:
            try:
                line = world.estimate(now).line()
                if line:
                    out.append(line)
            except Exception:
                pass
        if self.recent_tools:
            out.append("You just used: " + ", ".join(dict.fromkeys(self.recent_tools)) + ".")
        if self.open_question:
            out.append(f'You last asked them: "{self.open_question}" (their message may be the answer).')
        return out

    def block(self, now: datetime | None = None) -> str:
        ls = self.lines(now)
        return "<working_memory>\n" + "\n".join(ls) + "\n</working_memory>" if ls else ""
