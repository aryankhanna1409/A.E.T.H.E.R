"""The symbolic side grows by itself, with your OK: rules mined from your facts, and habits mined from your days.

Rule discovery (an AMIE-style miner, small enough for a personal knowledge graph)
    Candidate Horn rules with one or two body atoms are enumerated from the predicates you actually use:
        p(?a, ?b) -> q(?a, ?b)                 (whenever one holds, so does the other)
        p(?a, ?b) -> q(?b, ?a)                 (an inverse)
        p(?a, ?b) & q(?b, ?c) -> r(?a, ?c)     (a chain, like works_at + located_in -> works_in)
    Each is scored on the graph: support = how many head facts it predicts correctly, confidence = support divided
    by how many times its body matches (the standard and PCA confidence used by AMIE). Rules that clear both bars
    and aren't already known become suggestions. A rule you accept is added to the rule engine with its measured
    confidence, which then flows into every fact it derives (see rules.py).

Habits (episodic memory becoming semantic, never silently)
    Your messages and Grind 75 logs are scanned for activities you mention on several different days ("gym",
    "leetcode", "physio") and the weekdays and hours they cluster on. "You mention gym most Mon, Wed and Fri
    mornings: keep that as a habit?" Accepting saves it as a fact you approved; rejecting means it won't be
    suggested again.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime

from ..retrieval.lexical import STOP
from .kg import INVERSES, SYMMETRIC, KnowledgeGraph

SCHEMA = """CREATE TABLE IF NOT EXISTS suggestions (id INTEGER PRIMARY KEY, kind TEXT, key TEXT UNIQUE, text TEXT,
payload TEXT, support INTEGER, confidence REAL, status TEXT DEFAULT 'proposed', created REAL)"""
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def mine_rules(kg: KnowledgeGraph, existing: set[str] | None = None, min_support: int = 2,
               min_conf: float = 0.7, max_rules: int = 20) -> list[dict]:
    facts = {t.key() for t in kg.all()}
    by_p: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for s, p, o in facts:
        by_p[p].add((s, o))
    preds = [p for p, xs in by_p.items() if len(xs) >= min_support]
    existing = existing or set()
    out = []

    def add(text, support, body_n, head_subjects, predicted):
        conf = support / body_n if body_n else 0.0
        # PCA confidence: only count predictions for subjects whose head relation we know something about
        pca_n = sum(1 for s, _ in predicted if s in head_subjects)
        pca = support / pca_n if pca_n else 0.0
        if support >= min_support and max(conf, pca) >= min_conf and text not in existing:
            out.append({"rule": text, "support": support, "confidence": round(conf, 3), "pca": round(pca, 3)})

    for p in preds:
        for q in preds:
            if p == q:
                continue
            body, head = by_p[p], by_p[q]
            heads = {s for s, _ in head}
            if not (p in SYMMETRIC or INVERSES.get(p) == q):
                add(f"{p}(?a, ?b) -> {q}(?a, ?b)", len(body & head), len(body), heads, body)
                inv = {(o, s) for s, o in body}
                if INVERSES.get(p) != q:
                    add(f"{p}(?a, ?b) -> {q}(?b, ?a)", len(inv & head), len(inv), heads, inv)
    for p in preds:
        for q in preds:
            joins = [(a, c) for a, b in by_p[p] for b2, c in by_p[q] if b == b2 and a != c]
            if len(joins) < min_support:
                continue
            pairs = set(joins)
            for r in preds:
                if r in (p, q) and p == q:
                    continue
                head = by_p[r]
                add(f"{p}(?a, ?b) & {q}(?b, ?c) -> {r}(?a, ?c)", len(pairs & head), len(pairs),
                    {s for s, _ in head}, pairs)
    out.sort(key=lambda r: (r["confidence"], r["support"]), reverse=True)
    return out[:max_rules]


ACTIVITY_HINT = re.compile(r"\b(?:went|going|go|did|doing|finished|after|before|at|from|skipped|missed|back\s+from)\s+"
                           r"(?:to\s+|the\s+|my\s+)?([a-z][a-z-]{2,20}(?:\s+[a-z][a-z-]{2,20})?)", re.I)
BORING = STOP | {"home", "work", "it", "that", "this", "bed", "sleep", "time", "day", "night", "morning", "evening",
                 "today", "yesterday", "tomorrow", "now", "then", "one", "thing", "something", "lot", "some", "least",
                 "first", "last", "least", "all", "end", "same"}


def mine_habits(turns: list[tuple[str, float]], min_days: int = 3, window_days: int = 28) -> list[dict]:
    """turns: (text, unix time) of your own messages. Finds activities mentioned on several distinct days."""
    now = time.time()
    seen: dict[str, dict] = defaultdict(lambda: {"days": set(), "dow": Counter(), "hours": Counter()})
    for text, ts in turns:
        if now - ts > window_days * 86400:
            continue
        when = datetime.fromtimestamp(ts)
        for m in ACTIVITY_HINT.finditer(text.lower()):
            act = " ".join(w for w in m.group(1).split() if w not in BORING)
            if not act or len(act) < 3:
                continue
            d = seen[act]
            day = when.date().isoformat()
            if day not in d["days"]:
                d["days"].add(day)
                d["dow"][when.weekday()] += 1
                d["hours"][when.hour] += 1
    out = []
    for act, d in seen.items():
        n = len(d["days"])
        if n < min_days:
            continue
        common = [WEEKDAYS[i] for i, c in sorted(d["dow"].items()) if c >= max(2, n // 3)]
        hour = d["hours"].most_common(1)[0][0]
        part = "mornings" if hour < 12 else "afternoons" if hour < 17 else "evenings"
        when = (", ".join(common) + " " + part) if common and len(common) < 6 else f"most days, usually in the {part[:-1]}"
        out.append({"activity": act, "days": n, "when": when,
                    "text": f"usually does {act} on {when}" if common and len(common) < 6 else f"does {act} {when}"})
    out.sort(key=lambda h: h["days"], reverse=True)
    return out[:10]


class Suggestions:
    """Mined rules and habits waiting for your OK."""

    def __init__(self, aether):
        self.a = aether
        with aether.lock:
            aether.db.execute(SCHEMA)
            aether.db.commit()

    def _put(self, kind: str, key: str, text: str, payload: dict, support: int, conf: float) -> bool:
        with self.a.lock:
            cur = self.a.db.execute("INSERT OR IGNORE INTO suggestions(kind, key, text, payload, support, confidence, "
                                    "created) VALUES (?,?,?,?,?,?,?)",
                                    (kind, key, text, json.dumps(payload), support, conf, time.time()))
            self.a.db.commit()
            return cur.rowcount > 0

    def refresh(self) -> dict:
        n_rules = n_habits = 0
        existing = {r.text for r in self.a.rules.rules}
        for r in mine_rules(self.a.kg, existing):
            n_rules += self._put("rule", "rule:" + r["rule"], r["rule"], r, r["support"], r["confidence"])
        rows = self.a.db.execute("SELECT text, created FROM turns WHERE role='user' ORDER BY id DESC LIMIT 3000").fetchall()
        turns = [(self.a.store.cipher.decrypt(t), c) for t, c in rows if c]
        name = self.a.cfg.user_name or "The user"
        for h in mine_habits(turns):
            n_habits += self._put("habit", "habit:" + h["activity"], f"{name} {h['text']}", h, h["days"], 1.0)
        return {"rules_suggested": n_rules, "habits_suggested": n_habits}

    def list(self, status: str = "proposed") -> list[dict]:
        rows = self.a.db.execute("SELECT id, kind, text, support, confidence, status FROM suggestions WHERE status=? "
                                 "ORDER BY kind, confidence DESC, support DESC", (status,)).fetchall()
        return [{"id": r[0], "kind": r[1], "text": r[2], "support": r[3], "confidence": r[4], "status": r[5]} for r in rows]

    def decide(self, sid: int, accept: bool) -> str:
        r = self.a.db.execute("SELECT kind, text, confidence FROM suggestions WHERE id=?", (sid,)).fetchone()
        if not r:
            return "No such suggestion."
        kind, text, conf = r
        if accept:
            if kind == "rule":
                self.a.rules.add_rule(text, confidence=conf)
                self.a.rules.run()
            else:
                self.a.store.add(text + ".", "semantic", "user_approved", 0.9, {"kind": "habit"})
        with self.a.lock:
            self.a.db.execute("UPDATE suggestions SET status=? WHERE id=?", ("accepted" if accept else "rejected", sid))
            self.a.db.commit()
        return ("Added: " if accept else "Dropped: ") + text
