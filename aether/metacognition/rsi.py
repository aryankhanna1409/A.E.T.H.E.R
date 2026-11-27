"""Recursive self-improvement, bounded: AETHER improves itself, and improves how it improves.

RSI (level 1): improve AETHER.
  Each cycle builds a benchmark from your own data (questions it writes from your memories, the eval cases you
  added, and small talk that should retrieve nothing). It then proposes changes to its own retrieval knobs,
  scores each one on half the benchmark, and checks the best on the other half it has never seen. A change that
  wins on both becomes a proposal. Nothing changes until you approve it, and every applied change can be undone.

RSI² (level 2): improve the improver.
  * Strategy: how it proposes changes (a small step, a random jump, a mix of two past winners, or going back to
    the default) is a set of arms. Thompson sampling picks the next one, so the strategies that have won get
    used more.
  * Step size: each knob's step follows the evolution-strategy 1/5 success rule: wider while steps keep winning,
    finer once they stop.
  * Budget: more candidates per cycle while cycles find gains, fewer once it has converged, so it doesn't burn
    your laptop for nothing.
  * Curriculum: benchmark questions the current setup gets wrong weigh more next cycle, so it works on its
    weak spots instead of what it already does well.

Safety rails (fixed in code; the loop can't change them):
  * It only touches settings on the TUNABLE list (no security, tools, permissions or code), always within hard
    bounds.
  * It holds out half the benchmark and needs a clear margin on it, so it can't overfit its own test.
  * You approve every change. It goes through the same settings store as every other change, so "revert" works.
  * Each cycle has a fixed evaluation budget, and every cycle is logged with what it tried and why.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import time
from collections import Counter
from dataclasses import dataclass

BOUNDS = {"hybrid_alpha": (0.0, 1.0, float), "rag_min_score": (0.02, 0.8, float), "rag_top_k": (2, 12, int)}
OPERATORS = ("nudge", "jump", "crossover", "revert")
SMALL_TALK = ["hi", "thanks", "how are you", "good morning", "tell me a joke", "what's up", "ok cool", "nice"]
MARGIN = 0.02

SCHEMA = """
CREATE TABLE IF NOT EXISTS rsi_cycles (
    id INTEGER PRIMARY KEY, t REAL, trigger TEXT, cases INTEGER, budget INTEGER, evaluated INTEGER,
    train_before REAL, train_after REAL, hold_before REAL, hold_after REAL, current TEXT, proposed TEXT,
    winner_op TEXT, ops TEXT, outcome TEXT, proposal_id INTEGER, ms INTEGER);
CREATE TABLE IF NOT EXISTS rsi_arms (arm TEXT PRIMARY KEY, wins REAL, tries REAL);
CREATE TABLE IF NOT EXISTS rsi_meta (k TEXT PRIMARY KEY, v TEXT);
"""


@dataclass
class Case:
    query: str
    expect: str        # substring of the memory that should come back; "" means retrieve nothing
    key: str           # stable id, for the curriculum weights
    origin: str        # "yours", "self-made", "small talk"


def _tok(text: str) -> list[str]:
    from ..memory.atlas import tokens
    return tokens(text)


class SelfImprover:
    def __init__(self, aether):
        self.a = aether
        with aether.lock:
            aether.db.executescript(SCHEMA)
            aether.db.commit()

    # -------------------------------------------------------------- meta state
    def _get(self, k: str, default):
        r = self.a.db.execute("SELECT v FROM rsi_meta WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def _put(self, k: str, v) -> None:
        with self.a.lock:
            self.a.db.execute("INSERT OR REPLACE INTO rsi_meta VALUES (?, ?)", (k, json.dumps(v)))
            self.a.db.commit()

    def sigmas(self) -> dict:
        s = self._get("sigma", {})
        return {k: float(s.get(k, 0.15 * (hi - lo))) for k, (lo, hi, _) in BOUNDS.items()}

    def budget(self) -> int:
        return int(self._get("budget", 12))

    def arms(self) -> dict:
        rows = dict((r[0], (r[1], r[2])) for r in self.a.db.execute("SELECT arm, wins, tries FROM rsi_arms"))
        return {op: rows.get(op, (0.0, 0.0)) for op in OPERATORS}

    # -------------------------------------------------------------- the benchmark, made from your data
    def benchmark(self, seed: int = 0, size: int = 40) -> list[Case]:
        rng = random.Random(seed)
        cases: list[Case] = []
        try:
            for i, c in enumerate(self.a.evals.all()):
                cases.append(Case(c.query, c.expect, f"user:{i}:{c.query[:40]}", "yours"))
        except Exception:
            pass
        mems = [m for m in self.a.store.list(None, 3000) if m.layer in
                ("semantic", "episodic", "procedural", "preference", "document") and len(m.text) >= 30]
        if mems:
            docs = {m.id: set(_tok(m.text)) for m in mems}
            df = Counter(t for ts in docs.values() for t in ts)
            for m in rng.sample(mems, min(size, len(mems))):
                rare = sorted(docs[m.id], key=lambda t: (df[t], t))[:4]
                if len(rare) < 2:
                    continue
                if len(rare) >= 4 and rng.random() < 0.5:        # sometimes a vaguer question, like people ask
                    rare = rare[:3]
                rng.shuffle(rare)
                q = ("what about " if rng.random() < 0.5 else "") + " ".join(rare)
                cases.append(Case(q, m.text[:60], f"mem:{m.id}", "self-made"))
        for s in SMALL_TALK:
            cases.append(Case(s, "", f"talk:{s}", "small talk"))
        return cases

    @staticmethod
    def split(cases: list[Case]) -> tuple[list[Case], list[Case]]:
        tr, ho = [], []
        for c in cases:
            (ho if int(hashlib.sha1(c.key.encode()).hexdigest(), 16) % 2 else tr).append(c)
        return tr, ho

    def evaluate(self, params: dict, cases: list[Case], weights: dict | None = None) -> tuple[float, set]:
        """Weighted F1 of retrieval on the cases, and the keys of the cases it got wrong."""
        weights = weights or {}
        tp = fp = fn = 0.0
        wrong = set()
        k = int(params["rag_top_k"])
        for c in cases:
            w = float(weights.get(c.key, 1.0))
            got = self.a.store.search(c.query, k=k, alpha=float(params["hybrid_alpha"]),
                                      min_score=float(params["rag_min_score"]),
                                      layers=("semantic", "episodic", "procedural", "preference", "document"))
            if c.expect:
                hit = any(c.expect.lower() in m.text.lower() for m in got)
                tp += w * hit
                fn += w * (not hit)
                noise = len(got) - hit > max(2, k // 2)
                fp += w * noise
                if not hit or noise:
                    wrong.add(c.key)
            elif got:
                fp += w
                wrong.add(c.key)
        return 2 * tp / max(2 * tp + fp + fn, 1e-9), wrong

    # -------------------------------------------------------------- proposing changes
    def _clip(self, k: str, v):
        lo, hi, typ = BOUNDS[k]
        v = min(hi, max(lo, v))
        return int(round(v)) if typ is int else round(float(v), 3)

    def _pick(self, rng: random.Random) -> str:
        arms = self.arms()
        draws = {op: rng.betavariate(1 + w, 1 + max(0.0, t - w)) for op, (w, t) in arms.items()}
        return max(draws, key=draws.get)

    def _candidate(self, op: str, inc: dict, rng: random.Random, winners: list[dict], default: dict) -> tuple[dict, str]:
        c = dict(inc)
        sig = self.sigmas()
        if op == "nudge":
            k = rng.choice(list(BOUNDS))
            c[k] = self._clip(k, inc[k] + rng.gauss(0, sig[k]) or sig[k])
            return c, k
        if op == "jump":
            for k, (lo, hi, _) in BOUNDS.items():
                c[k] = self._clip(k, rng.uniform(lo, hi))
            return c, "all"
        if op == "crossover" and winners:
            other = rng.choice(winners)
            for k in BOUNDS:
                if rng.random() < 0.5:
                    c[k] = self._clip(k, other[k])
            return c, "mix"
        k = rng.choice(list(BOUNDS))
        c[k] = self._clip(k, default[k])
        return c, k

    # -------------------------------------------------------------- one cycle
    def cycle(self, trigger: str = "manual", seed: int | None = None) -> dict:
        t0 = time.time()
        seed = int(t0) if seed is None else seed
        rng = random.Random(seed)
        cfg = self.a.cfg.memory
        current = {k: getattr(cfg, k) for k in BOUNDS}
        from ..core.config import MemoryConfig
        default = {k: getattr(MemoryConfig(), k) for k in BOUNDS}
        cases = self.benchmark(seed)
        train, hold = self.split(cases)
        if len([c for c in train if c.expect]) < 3 or len([c for c in hold if c.expect]) < 3:
            return self._log(trigger, cases, 0, 0, None, None, None, None, current, None, None, {},
                             "not enough memories yet to test against (it needs about a dozen)", None, t0)
        weights = self._get("weights", {})
        base_tr, _ = self.evaluate(current, train, weights)
        inc, inc_tr = dict(current), base_tr
        winners: list[dict] = self._get("winners", [])
        budget = self.budget()
        ops_used: Counter = Counter()
        ops_won: Counter = Counter()
        sig = self.sigmas()
        knob_wins, knob_tries = Counter(), Counter()
        seen = {json.dumps(current, sort_keys=True)}
        best_op = None
        for _ in range(budget):
            op = self._pick(rng)
            cand, knob = self._candidate(op, inc, rng, winners, default)
            key = json.dumps(cand, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            f, _ = self.evaluate(cand, train, weights)
            ops_used[op] += 1
            won = f > inc_tr + 1e-6
            if knob in BOUNDS:
                knob_tries[knob] += 1
                knob_wins[knob] += won
            if won:
                ops_won[op] += 1
                inc, inc_tr, best_op = cand, f, op
        # ---- level 2: learn how to improve
        with self.a.lock:
            for op in OPERATORS:
                if ops_used[op]:
                    self.a.db.execute("INSERT INTO rsi_arms VALUES (?, ?, ?) ON CONFLICT(arm) DO UPDATE SET "
                                      "wins=wins*0.9+excluded.wins, tries=tries*0.9+excluded.tries",
                                      (op, float(ops_won[op]), float(ops_used[op])))
            self.a.db.commit()
        for k in BOUNDS:                                    # 1/5 success rule on each knob's step
            if knob_tries[k]:
                rate = knob_wins[k] / knob_tries[k]
                lo, hi, _ = BOUNDS[k]
                sig[k] = min(0.5 * (hi - lo), max(0.01 * (hi - lo), sig[k] * (1.22 if rate > 0.2 else 0.82)))
        self._put("sigma", sig)
        hold_before, _ = self.evaluate(current, hold)
        hold_after, _ = self.evaluate(inc, hold) if inc != current else (hold_before, None)
        gained = inc != current and inc_tr >= base_tr + MARGIN and hold_after >= hold_before + MARGIN
        past = self._get("gains", [])[-4:] + [1 if gained else 0]
        self._put("gains", past)
        if gained:
            budget = min(24, int(budget * 1.5) + 1)
            winners = (winners + [inc])[-8:]
            self._put("winners", winners)
        elif len(past) >= 3 and not any(past[-3:]):
            budget = max(6, budget // 2)
        self._put("budget", budget)
        _, wrong = self.evaluate(inc, cases)               # curriculum: what it still gets wrong weighs more
        nw = {c.key: (min(4.0, float(weights.get(c.key, 1.0)) * 1.5) if c.key in wrong
                      else max(1.0, float(weights.get(c.key, 1.0)) * 0.8)) for c in cases}
        self._put("weights", {k: round(v, 2) for k, v in nw.items() if v > 1.0})
        pid, outcome = None, "no change beat the current setup on unseen questions; keeping it"
        if gained:
            pid = self._propose(current, inc, base_tr, inc_tr, hold_before, hold_after, len(cases))
            outcome = "found an improvement; waiting for your approval" if pid else "found an improvement (already proposed)"
        elif inc != current:
            outcome = (f"a change scored better on practice questions ({base_tr:.0%}→{inc_tr:.0%}) but not clearly better "
                       f"on unseen ones ({hold_before:.0%}→{hold_after:.0%}), so it was discarded rather than risk "
                       "overfitting")
        return self._log(trigger, cases, budget, sum(ops_used.values()), base_tr, inc_tr, hold_before, hold_after,
                         current, inc if inc != current else None, best_op, dict(ops_used), outcome, pid, t0)

    def _propose(self, current, new, tr0, tr1, h0, h1, n) -> int | None:
        meta = self.a.meta
        changed = {k: v for k, v in new.items() if current[k] != v}
        title = "Retrieval tune-up: " + ", ".join(f"{k.replace('_', ' ')} {current[k]}→{v}" for k, v in changed.items())
        why = (f"Self-improvement cycle on {n} questions from your own data: {tr0:.0%}→{tr1:.0%} on practice, "
               f"{h0:.0%}→{h1:.0%} on questions it never trained on.")
        with meta.lock:
            meta.db.execute("DELETE FROM proposals WHERE signature='rsi:memory' AND status='proposed'")
            cur = meta.db.execute("INSERT INTO proposals(t, kind, title, rationale, change, signature) VALUES (?,?,?,?,?,?)",
                                  (time.time(), "setting", title, why,
                                   json.dumps({"section": "memory", "values": changed}), "rsi:memory"))
            meta.db.commit()
        return cur.lastrowid

    def _log(self, trigger, cases, budget, evaluated, tr0, tr1, h0, h1, current, proposed, op, ops, outcome, pid, t0):
        row = (time.time(), trigger, len(cases), budget, evaluated, tr0, tr1, h0, h1, json.dumps(current),
               json.dumps(proposed) if proposed else None, op, json.dumps(ops), outcome, pid, int((time.time() - t0) * 1000))
        with self.a.lock:
            cur = self.a.db.execute("INSERT INTO rsi_cycles(t, trigger, cases, budget, evaluated, train_before, "
                                    "train_after, hold_before, hold_after, current, proposed, winner_op, ops, outcome, "
                                    "proposal_id, ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            self.a.db.commit()
        try:
            self.a.bus.publish("rsi", {"outcome": outcome})
        except Exception:
            pass
        return {"id": cur.lastrowid, "trigger": trigger, "cases": len(cases), "budget": budget, "evaluated": evaluated,
                "train_before": _r(tr0), "train_after": _r(tr1), "hold_before": _r(h0), "hold_after": _r(h1),
                "current": current, "proposed": proposed, "winner_op": op, "ops": ops, "outcome": outcome,
                "proposal_id": pid}

    # -------------------------------------------------------------- what the UI shows
    def info(self, limit: int = 20) -> dict:
        rows = self.a.db.execute("SELECT id, t, trigger, cases, evaluated, train_before, train_after, hold_before, "
                                 "hold_after, proposed, winner_op, outcome, proposal_id FROM rsi_cycles "
                                 "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        cycles = [{"id": r[0], "t": r[1], "trigger": r[2], "cases": r[3], "evaluated": r[4], "train_before": _r(r[5]),
                   "train_after": _r(r[6]), "hold_before": _r(r[7]), "hold_after": _r(r[8]),
                   "proposed": json.loads(r[9]) if r[9] else None, "winner_op": r[10], "outcome": r[11],
                   "proposal_id": r[12]} for r in rows]
        arms = {op: {"wins": round(w, 2), "tries": round(t, 2), "rate": round((1 + w) / (2 + t), 2)}
                for op, (w, t) in self.arms().items()}
        return {"cycles": cycles, "arms": arms, "sigma": {k: round(v, 3) for k, v in self.sigmas().items()},
                "budget": self.budget(), "hard_cases": len(self._get("weights", {})),
                "knobs": {k: getattr(self.a.cfg.memory, k) for k in BOUNDS},
                "bounds": {k: [lo, hi] for k, (lo, hi, _) in BOUNDS.items()},
                "rails": ["only retrieval settings on the tunable list, within fixed bounds",
                          "never security, permissions, tools or code",
                          "must win on questions it never trained on",
                          "you approve every change; every change can be reverted",
                          f"at most {24} evaluations per cycle"]}


def _r(x):
    return None if x is None else round(float(x), 3)


def handle(a, text: str) -> str | None:
    import re
    t = re.sub(r"^(?:(?:hey|ok|okay)\s+)?(?:aether|nova)[\s,]+", "", text.strip(), flags=re.I).strip(" ?.!")
    if not re.match(r"^(?:improve\s+yourself|run\s+(?:a\s+)?self[\s-]?improvement(?:\s+cycle)?|self[\s-]?improve|"
                    r"get\s+better|tune\s+yourself)$", t, re.I):
        return None
    r = a.rsi.cycle("asked")
    s = f"Self-improvement cycle: {r['outcome']}."
    if r["hold_before"] is not None:
        s += (f" Tested {r['evaluated']} variants on {r['cases']} questions from your data; on unseen questions "
              f"{r['hold_before']:.0%} → {r['hold_after']:.0%}.")
    if r["proposal_id"]:
        s += " Approve it in Self → Self-improvement."
    return s
