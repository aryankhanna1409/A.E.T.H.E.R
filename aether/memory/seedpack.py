"""Seed packs: fill every part of AETHER (memories, knowledge graph, rules, preferences, patterns, goals,
retrieval test cases) from one JSON file you control, so a fresh install already knows you.

A pack is private data you write or export yourself; nothing personal ships with AETHER. Every item it adds is
marked as seeded (memory meta `seeded`, fact source `user_approved`), dated with the date the pack gives when there
is one, and run through the same prompt-injection check as any import. Applying the same pack twice adds nothing.

    {
      "label": "my history",
      "memories":     [{"text": "...", "layer": "semantic|episodic|procedural", "date": "2026-10-25", "kind": "fact",
                        "category": "health"}],   # category: a memory-map area (see aether/memory/atlas.py)
      "facts":        [["Sam", "studies_at", "Leeds University"], {"s": "...", "p": "...", "o": "...", "date": "2024-09-01"}],
      "rules":        ["works_on(?u, ?p) & uses(?p, ?t) -> experienced_with(?u, ?t)"],
      "preferences":  ["Answer directly, with numbers"],
      "observations": [{"kind": "habit", "text": "...", "evidence": ["..."], "confidence": 0.8}],
      "goals":        [{"text": "...", "status": "active|done", "date": "...",
                        "tasks": [{"description": "...", "status": "done|pending", "agent": "planner",
                                   "after": [0], "done_when": "..."}]}],
      "eval_cases":   [{"query": "where do I study", "expect": "Sam studies in Leeds."}],
      "reminders":    [{"text": "...", "when": "2027-01-04"}],
      "plans":        "# Plans\n## Now · until 2027-01-18\n..."     # your plans.md, used only if you have none yet
    }
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from ..security.injection import scan

MEMORY_LAYERS = {"semantic", "episodic", "procedural"}
TASK_STATUSES = {"pending", "done", "failed", "cancelled", "blocked"}
GOAL_STATUSES = {"active", "done", "failed", "cancelled"}


def when(date: str | None) -> float | None:
    """'2026-10-25', '2026-10' or '2026' -> a timestamp (local time), else None."""
    if not date:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(str(date), fmt).timestamp()
        except ValueError:
            continue
    return None


def load(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("A seed pack is a JSON object with lists such as memories, facts and goals.")
    return data


def _clean(text) -> str:
    return " ".join(str(text or "").split())[:600]


def _safe(text: str) -> bool:
    return len(text.split()) >= 2 and not scan(text)


def summary(pack: dict) -> dict[str, int]:
    keys = ("memories", "facts", "rules", "preferences", "observations", "goals", "eval_cases", "reminders", "plans")
    return {k: (1 if k == "plans" else len(pack.get(k) or [])) for k in keys if pack.get(k)}


def apply(aether, pack: dict, label: str | None = None) -> dict[str, int]:
    """Write the pack into AETHER. Returns how many items of each kind were added (skipped ones aren't counted)."""
    label = (label or pack.get("label") or "seed pack")[:80]
    out = {k: 0 for k in ("memories", "facts", "rules", "preferences", "observations", "goals", "eval_cases",
                          "reminders", "skipped")}
    store = aether.store
    known = {_clean(m.text).lower() for layer in MEMORY_LAYERS for m in store.list(layer, 100000)}

    for m in pack.get("memories") or []:
        m = {"text": m} if isinstance(m, str) else m
        text, layer = _clean(m.get("text")), m.get("layer", "semantic")
        if layer not in MEMORY_LAYERS or not _safe(text) or text.lower() in known:
            out["skipped"] += 1
            continue
        meta = {"seeded": True, "imported_from": label, "kind": m.get("kind", "fact")}
        if m.get("date"):
            meta["date"] = str(m["date"])
        if m.get("category"):
            meta["category"] = str(m["category"])[:40]
        mid = store.add(text, layer, "user_approved", confidence=float(m.get("confidence", 0.9)), meta=meta)
        t = when(m.get("date"))
        if t:
            with store.lock:
                store.db.execute("UPDATE memories SET created=?, updated=? WHERE id=?", (t, t, mid))
                store.db.commit()
        known.add(text.lower())
        out["memories"] += 1

    kg = aether.kg
    for f in pack.get("facts") or []:
        s, p, o = (f[0], f[1], f[2]) if isinstance(f, (list, tuple)) else (f.get("s"), f.get("p"), f.get("o"))
        if not (s and p and o) or not _safe(f"{s} {p} {o}"):
            out["skipped"] += 1
            continue
        if kg.query(s, p, o):
            continue
        kg.assert_fact(s, p, o, source="user_approved", confidence=0.95, replace=True)
        t = when(f.get("date")) if isinstance(f, dict) else None
        if t:
            from ..neurosymbolic.kg import norm
            with kg.lock:
                kg.db.execute("UPDATE triples SET created=? WHERE s=? AND p=? AND o=? AND valid_to IS NULL",
                              (t, norm(s), norm(p), norm(o)))
                kg.db.commit()
        out["facts"] += 1

    existing_rules = set(aether.rules.user_rules())
    for r in pack.get("rules") or []:
        if r.strip() in existing_rules:
            continue
        try:
            aether.rules.add_rule(r)
            out["rules"] += 1
        except ValueError:
            out["skipped"] += 1
    if out["rules"] or out["facts"]:
        aether.rules.run()

    have = {p.instruction.lower() for p in aether.prefs.all()}
    for instruction in pack.get("preferences") or []:
        instruction = _clean(instruction)
        if not _safe(instruction) or instruction.lower() in have:
            continue
        aether.prefs.add_custom(instruction)
        out["preferences"] += 1

    seen_obs = {o.text for o in aether.observations.all()}
    for o in pack.get("observations") or []:
        text = _clean(o.get("text"))
        if not _safe(text) or text in seen_obs:
            continue
        oid = aether.observations.propose(o.get("kind", "pattern"), text, [_clean(e) for e in o.get("evidence", [])],
                                          float(o.get("confidence", 0.8)))
        if oid:
            aether.observations.set_status(oid, "confirmed")   # you wrote it, so it is confirmed, not a guess
            out["observations"] += 1

    graph = aether.graph
    goals_known = {g["text"] for g in graph.goals(100000)}
    for g in pack.get("goals") or []:
        text = _clean(g.get("text"))
        if not _safe(text) or text in goals_known:
            continue
        gid = graph.new_goal(text)
        ids: list[int] = []
        for task in g.get("tasks") or []:
            after = [ids[i] for i in task.get("after", []) if isinstance(i, int) and 0 <= i < len(ids)]
            tid = graph.add_task(gid, task.get("agent", "memory"), _clean(task.get("description")), after,
                                 _clean(task.get("done_when", "")), max_attempts=1)
            status = task.get("status", "pending")
            for t in graph.tasks(gid):
                if t.id == tid and status in TASK_STATUSES:
                    t.status, t.result = status, _clean(task.get("note", ""))
                    graph.update(t)
            ids.append(tid)
        status = g.get("status", "active")
        graph.set_goal(gid, status if status in GOAL_STATUSES else "active", _clean(g.get("report")) or None)
        t = when(g.get("date"))
        with graph.lock:
            graph.db.execute("UPDATE goals SET created=? WHERE id=?", (t or time.time(), gid))
            graph.db.commit()
        goals_known.add(text)
        out["goals"] += 1

    cases = {(c["query"], c["expect"]) for c in aether.evals.rows()}
    for c in pack.get("eval_cases") or []:
        q, e = _clean(c.get("query")), _clean(c.get("expect"))
        if q and (q, e) not in cases:
            aether.evals.add(q, e)
            out["eval_cases"] += 1

    open_reminders = {r.text for r in aether.reminders.open()}
    for r in pack.get("reminders") or []:
        text = _clean(r.get("text"))
        if _safe(text) and text not in open_reminders and r.get("when"):
            aether.reminders.add(text, str(r["when"]))
            out["reminders"] += 1

    plan = pack.get("plans")
    if plan:
        out["plans"] = 0
    plans = getattr(aether, "plans", None)
    if isinstance(plan, str) and plan.strip() and plans is not None and not plans.text().strip() and _safe(plan[:2000]):
        plans.save(plan)          # never overwrites plans you already keep in AETHER
        out["plans"] = 1

    added = sum(v for k, v in out.items() if k != "skipped")
    aether.audit.record("memory.seed_pack", label, True, f"you imported a seed pack: {added} items added")
    return out
