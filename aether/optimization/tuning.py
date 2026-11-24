"""Self-tuning of retrieval settings on a scored evaluation set.

An eval case is a message plus what retrieval SHOULD return: a substring of
the memory that must come back, or nothing at all (small talk). Particle
swarm searches hybrid_alpha and rag_min_score to maximise F1 over the cases.
The result is only a proposal; it is applied after the user approves it."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass

from .engine import particle_swarm

SCHEMA = """CREATE TABLE IF NOT EXISTS eval_cases (
    id INTEGER PRIMARY KEY, query TEXT NOT NULL, expect TEXT NOT NULL, created REAL NOT NULL)"""

BOUNDS = {"hybrid_alpha": (0.0, 1.0), "rag_min_score": (0.02, 0.8)}


@dataclass
class EvalCase:
    query: str
    expect: str  # substring of a memory that should be retrieved, or "" for "retrieve nothing"


class EvalSet:
    def __init__(self, db: sqlite3.Connection, lock: threading.RLock):
        self.db, self.lock = db, lock
        with lock:
            db.execute(SCHEMA)
            db.commit()

    def add(self, query: str, expect: str = "") -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO eval_cases(query, expect, created) VALUES (?,?,?)",
                                  (query.strip(), expect.strip(), time.time()))
            self.db.commit()
            return cur.lastrowid

    def remove(self, case_id: int) -> None:
        with self.lock:
            self.db.execute("DELETE FROM eval_cases WHERE id=?", (case_id,))
            self.db.commit()

    def all(self) -> list[EvalCase]:
        with self.lock:
            rows = self.db.execute("SELECT query, expect FROM eval_cases ORDER BY id").fetchall()
        return [EvalCase(q, e) for q, e in rows]

    def rows(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT id, query, expect FROM eval_cases ORDER BY id").fetchall()
        return [{"id": i, "query": q, "expect": e} for i, q, e in rows]


def score(store, cases: list[EvalCase], alpha: float, min_score: float, k: int = 6) -> float:
    """F1 of retrieval decisions: a hit is the expected memory in the results;
    for 'expect nothing' cases any retrieval is a false positive."""
    tp = fp = fn = 0
    for c in cases:
        got = store.search(c.query, k=k, alpha=alpha, min_score=min_score,
                           layers=("semantic", "episodic", "procedural", "document"))
        if c.expect:
            hit = any(c.expect.lower() in m.text.lower() for m in got)
            tp += hit
            fn += not hit
            fp += len(got) - hit > k // 2  # flooding the context with noise counts against
        else:
            fp += bool(got)
    return 2 * tp / max(2 * tp + fp + fn, 1)


def tune_retrieval(store, cfg_memory, cases: list[EvalCase], iters: int = 25, seed: int = 0) -> dict:
    if len(cases) < 3:
        raise ValueError("Add at least 3 eval cases first (/eval add <message> => <expected memory text>).")
    names = list(BOUNDS)
    cache: dict = {}

    def f(x):
        key = tuple(round(v, 3) for v in x)
        if key not in cache:
            cache[key] = score(store, cases, *key, k=cfg_memory.rag_top_k)
        return cache[key]

    before = score(store, cases, cfg_memory.hybrid_alpha, cfg_memory.rag_min_score, cfg_memory.rag_top_k)
    best, best_f, history = particle_swarm(f, [BOUNDS[n] for n in names], particles=12, iters=iters, seed=seed)
    proposal = {n: round(v, 3) for n, v in zip(names, best)}
    return {"setting": "memory", "current": {n: getattr(cfg_memory, n) for n in names}, "proposed": proposal,
            "f1_before": round(before, 3), "f1_after": round(best_f, 3), "cases": len(cases),
            "evaluations": len(cache), "history": [round(h, 3) for h in history],
            "worth_applying": best_f > before + 0.02}


def describe(result: dict) -> str:
    return (f"Retrieval tuning on {result['cases']} eval cases: F1 {result['f1_before']} -> {result['f1_after']}.\n"
            f"Current {json.dumps(result['current'])}, proposed {json.dumps(result['proposed'])}.\n"
            + ("Worth applying." if result["worth_applying"] else "Not a meaningful gain; keeping current settings is fine."))
