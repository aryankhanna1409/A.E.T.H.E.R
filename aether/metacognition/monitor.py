"""Metacognition: AETHER watching its own performance.

* Every turn is measured: latency, route, tools used and failed, whether the
  verifier had to revise, removed fake citations, confidence and the user's
  feedback (explicit thumbs, or implicit: "that's wrong" on the next message).
* Finished goals get a post-task self-review (LLM writes it, code stores it);
  lessons become procedural memories the agents can retrieve next time.
* Code scans the metrics for patterns and writes improvement PROPOSALS.
  A proposal changes nothing until the user approves it, and only whitelisted
  settings can be touched (see core.settings.TUNABLE); security never is.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass

from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_metrics (
    id INTEGER PRIMARY KEY, t REAL NOT NULL, latency_ms REAL NOT NULL, intent TEXT, routes TEXT,
    tools INTEGER, tool_failures INTEGER, denied INTEGER, revised INTEGER, fake_citations INTEGER,
    confidence REAL, wellness TEXT, answer_chars INTEGER, feedback INTEGER DEFAULT 0, error TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY, t REAL NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, rationale TEXT NOT NULL,
    change TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', signature TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS self_reviews (
    id INTEGER PRIMARY KEY, t REAL NOT NULL, goal_id INTEGER, status TEXT, went_well TEXT, went_wrong TEXT,
    lesson TEXT, score REAL);
CREATE TABLE IF NOT EXISTS tool_outcomes (t REAL NOT NULL, tool TEXT NOT NULL, ok INTEGER NOT NULL);
"""

NEGATIVE = re.compile(r"^\s*(no(?![,.! ]+(?:problem|worries|thanks|need|rush|hurry|stress)\b)[,.! ]|nope|wrong|that'?s (wrong|not (it|right|what i (asked|meant)))|"
                      r"incorrect|useless|you misunderstood|not helpful|that'?s bad|try again)", re.I)
POSITIVE = re.compile(r"^\s*(perfect|great|thanks?|thank you|nice|exactly|that'?s (right|it|helpful)|"
                      r"awesome|love it|good job)\b", re.I)


class SelfReview(BaseModel):
    went_well: str = Field(description="what worked, one sentence")
    went_wrong: str = Field(description="what failed or was slow, one sentence, or 'nothing'")
    lesson: str = Field(description="one reusable lesson for similar goals next time, or ''")
    score: float = Field(ge=0, le=1, description="how well the goal was achieved, 0..1")


@dataclass
class Proposal:
    id: int
    kind: str  # setting | preference | procedure
    title: str
    rationale: str
    change: dict
    status: str


class Metacognition:
    def __init__(self, db: sqlite3.Connection, lock: threading.RLock):
        self.db, self.lock = db, lock
        with lock:
            db.executescript(SCHEMA)
            db.commit()
        self.last_turn: int | None = None

    # ---------------------------------------------------------- measurement
    def record_turn(self, state, latency_ms: float, intent: str, answer: str, notices: list[str],
                    error: str = "") -> int:
        tools = [x for x in state.tool_log]
        failures = sum("ok=False" in x for x in tools)
        denied = sum("-> deny" in x for x in tools)
        fake = sum(n.startswith("Removed citations") for n in notices)
        revised = int(getattr(state, "revised", False))
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO turn_metrics(t, latency_ms, intent, routes, tools, tool_failures, denied, revised,"
                " fake_citations, confidence, wellness, answer_chars, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), latency_ms, intent, ",".join(state.route), len(tools), failures, denied, revised,
                 fake, state.confidence, state.wellness, len(answer), error[:300]))
            self.db.commit()
        self.last_turn = cur.lastrowid
        return cur.lastrowid

    def implicit_feedback(self, text: str) -> int:
        """Score the PREVIOUS answer from how the user reacts to it. Returns -1, 0 or +1."""
        if self.last_turn is None:
            return 0
        fb = -1 if NEGATIVE.search(text) else 1 if POSITIVE.search(text) else 0
        if fb:
            self.feedback(self.last_turn, fb)
        return fb

    def feedback(self, turn_id: int | None = None, value: int = 1) -> None:
        turn_id = turn_id or self.last_turn
        if turn_id is None:
            return
        with self.lock:
            self.db.execute("UPDATE turn_metrics SET feedback=? WHERE id=?", (max(-1, min(1, value)), turn_id))
            self.db.commit()

    # ---------------------------------------------------------- post-task review
    def review_goal(self, llm, goal: dict, store=None) -> SelfReview | None:
        from ..llm.base import Message, structured
        if llm is None or not goal:
            return None
        try:
            r = structured(llm, [Message("system", "You review your own finished work honestly. No excuses, "
                                         "no flattery. Base it only on the report."),
                                 Message("user", f"Goal status: {goal['status']}\n\n{goal.get('report', '')[:6000]}")],
                           SelfReview, max_tokens=400)
        except Exception:
            ok = goal["status"] == "done"
            r = SelfReview(went_well="completed" if ok else "", went_wrong="" if ok else goal["status"],
                           lesson="", score=1.0 if ok else 0.0)
        if goal["status"] != "done":
            r.score = min(r.score, 0.5)  # code caps the self-score of a goal that did not finish
        with self.lock:
            self.db.execute("INSERT INTO self_reviews(t, goal_id, status, went_well, went_wrong, lesson, score)"
                            " VALUES (?,?,?,?,?,?,?)", (time.time(), goal["id"], goal["status"], r.went_well,
                                                        r.went_wrong, r.lesson, r.score))
            self.db.commit()
        if store is not None and r.lesson.strip():
            store.add(f"Lesson from goal '{goal['text'][:80]}': {r.lesson.strip()}", "procedural", "inferred", 0.6,
                      {"goal_id": goal["id"]})
        return r

    def reviews(self, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT goal_id, status, went_well, went_wrong, lesson, score, t FROM self_reviews"
                                   " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        keys = ["goal_id", "status", "went_well", "went_wrong", "lesson", "score", "t"]
        return [dict(zip(keys, r)) for r in rows]

    # ---------------------------------------------------------- dashboard
    def summary(self, days: float = 7) -> dict:
        since = time.time() - days * 86400
        with self.lock:
            rows = self.db.execute("SELECT latency_ms, routes, tools, tool_failures, denied, revised, fake_citations,"
                                   " confidence, feedback, error, intent FROM turn_metrics WHERE t>=?", (since,)).fetchall()
        n = len(rows)
        if not n:
            return {"turns": 0, "days": days}
        lat = sorted(r[0] for r in rows)
        routes = Counter(x for r in rows for x in (r[1] or "direct").split(","))
        tools = sum(r[2] for r in rows)
        fb = [r[8] for r in rows if r[8]]
        return {
            "turns": n, "days": days,
            "latency_ms": {"median": lat[n // 2], "p95": lat[min(n - 1, int(n * 0.95))]},
            "routes": dict(routes.most_common()),
            "tool_calls": tools,
            "tool_success_rate": round(1 - sum(r[3] for r in rows) / tools, 3) if tools else None,
            "denied_actions": sum(r[4] for r in rows),
            "verifier_revision_rate": round(sum(r[5] for r in rows) / n, 3),
            "fake_citation_turns": sum(1 for r in rows if r[6]),
            "avg_confidence": round(sum(r[7] or 0 for r in rows) / n, 3),
            "feedback": {"positive": sum(1 for x in fb if x > 0), "negative": sum(1 for x in fb if x < 0)},
            "errors": sum(1 for r in rows if r[9]),
            "intents": dict(Counter(r[10] for r in rows).most_common(6)),
        }

    # ---------------------------------------------------------- proposals
    def propose(self, cfg, prefs=None, min_turns: int = 15) -> list[Proposal]:
        """Deterministic rules over the metrics. Each finding becomes at most one open proposal."""
        s = self.summary(days=14)
        found: list[tuple[str, str, str, dict]] = []
        if s["turns"] >= min_turns:
            if s["verifier_revision_rate"] > 0.15 and cfg.llm.temperature > 0.3:
                t = round(max(0.3, cfg.llm.temperature - 0.2), 2)
                found.append(("setting", f"Lower temperature to {t}",
                              f"The verifier had to correct {s['verifier_revision_rate']:.0%} of answers for "
                              "contradicting known facts. A cooler model invents less.",
                              {"section": "llm", "values": {"temperature": t}}))
            if s["latency_ms"]["median"] > 25000 and cfg.memory.rag_top_k > 3:
                k = cfg.memory.rag_top_k - 2
                found.append(("setting", f"Retrieve {k} items instead of {cfg.memory.rag_top_k}",
                              f"Median reply time is {s['latency_ms']['median'] / 1000:.0f}s. Fewer retrieved chunks "
                              "means a shorter prompt and faster answers on your GPU.",
                              {"section": "memory", "values": {"rag_top_k": k}}))
            neg = s["feedback"]["negative"]
            if neg >= 4 and neg > s["feedback"]["positive"]:
                with self.lock:
                    rows = self.db.execute("SELECT routes FROM turn_metrics WHERE feedback<0 ORDER BY id DESC LIMIT 20").fetchall()
                worst = Counter(x for (r,) in rows for x in (r or "direct").split(",")).most_common(1)
                if worst:
                    route = worst[0][0] or "direct"
                    found.append(("procedure", f"Be more careful on '{route}' answers",
                                  f"{neg} recent answers got negative reactions, mostly on the {route} route.",
                                  {"memory": f"When answering via {route}, state assumptions and check facts "
                                             "before answering; ask one clarifying question if the request is vague."}))
            if s["fake_citation_turns"] >= 3:
                found.append(("preference", "Only cite sources that were actually retrieved",
                              f"{s['fake_citation_turns']} answers contained citations with no matching source "
                              "(code removed them).",
                              {"instruction": "Never write a citation like [web 3] unless that numbered source is in the context."}))
        failing = self._failing_tools()
        for tool, rate, n in failing:
            found.append(("procedure", f"Check inputs before calling {tool}",
                          f"{tool} failed {rate:.0%} of {n} recent calls.",
                          {"memory": f"Tool {tool} often fails; validate its arguments and paths before calling it, "
                                     "and say so if it cannot run."}))
        out = []
        for kind, title, why, change in found:
            sig = kind + ":" + title.split(" to ")[0].split(" instead")[0]
            with self.lock:
                exists = self.db.execute("SELECT 1 FROM proposals WHERE signature=? AND status='proposed'", (sig,)).fetchone()
                if exists:
                    continue
                self.db.execute("DELETE FROM proposals WHERE signature=? AND status!='proposed'", (sig,))
                cur = self.db.execute("INSERT INTO proposals(t, kind, title, rationale, change, signature) VALUES (?,?,?,?,?,?)",
                                      (time.time(), kind, title, why, json.dumps(change), sig))
                self.db.commit()
            out.append(Proposal(cur.lastrowid, kind, title, why, change, "proposed"))
        return out

    def _failing_tools(self) -> list[tuple[str, float, int]]:
        with self.lock:
            rows = self.db.execute("SELECT tool, ok FROM tool_outcomes WHERE t>=?", (time.time() - 14 * 86400,)).fetchall()
        stats: dict[str, list[int]] = {}
        for tool, ok in rows:
            stats.setdefault(tool, []).append(ok)
        return [(t, 1 - sum(v) / len(v), len(v)) for t, v in stats.items() if len(v) >= 5 and sum(v) / len(v) < 0.5]

    def record_tool(self, tool: str, ok: bool, decision: str) -> None:
        if decision == "deny":
            return  # a policy block is not the tool's fault
        with self.lock:
            self.db.execute("INSERT INTO tool_outcomes VALUES (?,?,?)", (time.time(), tool, int(ok)))
            self.db.commit()

    def proposals(self, status: str | None = None) -> list[Proposal]:
        q = "SELECT id, kind, title, rationale, change, status FROM proposals"
        args: tuple = ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY id DESC", args).fetchall()
        return [Proposal(i, k, t, r, json.loads(c), s) for i, k, t, r, c, s in rows]

    def decide(self, pid: int, approve: bool, settings=None, prefs=None, store=None) -> str:
        p = next((x for x in self.proposals() if x.id == pid), None)
        if not p or p.status != "proposed":
            return "No open proposal with that id."
        msg = "Declined. I won't suggest that again unless the pattern comes back."
        if approve:
            if p.kind == "setting":
                msg = "Applied: " + "; ".join(settings.apply(p.change["section"], p.change["values"], p.title))
            elif p.kind == "preference":
                msg = "Added preference " + prefs.add_custom(p.change["instruction"])
            elif p.kind == "procedure":
                store.add(p.change["memory"], "procedural", "user_stated", 0.85, {"proposal": p.id})
                msg = "Saved as a working procedure."
        with self.lock:
            self.db.execute("UPDATE proposals SET status=? WHERE id=?", ("applied" if approve else "declined", pid))
            self.db.commit()
        return msg
