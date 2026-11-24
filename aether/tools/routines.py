"""Routines: named, one-line voice macros ("focus mode", "start my day") built from
existing tools. Saying the name runs the steps in order, deterministically and
without the language model, so it is instant. Every step still goes through the
tool registry, so the policy engine, approvals, taint mode and the audit log
apply exactly as if each tool were called on its own.

Steps are written one per line as `tool arg=value arg2="several words"`, e.g.

    app_open name=spotify
    remind text="stretch and drink water" when="in 50 minutes"
"""
from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import dataclass, field

NAME = re.compile(r"^[a-z0-9][a-z0-9 '-]{0,40}$")


@dataclass
class Routine:
    name: str
    steps: list[dict] = field(default_factory=list)  # [{"tool": str, "args": {str: str}}]
    description: str = ""

    def lines(self) -> str:
        return "\n".join(step_text(s) for s in self.steps)


def step_text(step: dict) -> str:
    def q(v: str) -> str:
        if not re.search(r"[\s'\"]", v):
            return v
        return f'"{v}"' if '"' not in v and "\\" not in v else shlex.quote(v)
    args = " ".join(f"{k}={q(str(v))}" for k, v in step["args"].items())
    return (step["tool"] + " " + args).strip()


def norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower().replace("_", " "))


def parse_steps(text: str, known_tools: set[str]) -> list[dict]:
    steps = []
    for n, line in enumerate(filter(None, (l.strip() for l in text.splitlines())), 1):
        if line.startswith("#"):
            continue
        try:
            words = shlex.split(line)
        except ValueError as e:
            raise ValueError(f"Step {n}: {e}") from e
        tool, args = words[0], {}
        if tool not in known_tools:
            raise ValueError(f"Step {n}: unknown tool '{tool}'. Known tools: {', '.join(sorted(known_tools))}")
        if tool in {"run_routine", "start_goal"}:
            raise ValueError(f"Step {n}: a routine can't call '{tool}'.")
        for w in words[1:]:
            if "=" not in w:
                raise ValueError(f"Step {n}: write arguments as name=value (got {w!r}).")
            k, v = w.split("=", 1)
            args[k.strip()] = v
        steps.append({"tool": tool, "args": args})
    if not steps:
        raise ValueError("A routine needs at least one step.")
    return steps


class Routines:
    def __init__(self, db, lock):
        self.db, self.lock = db, lock
        with lock:
            db.execute("CREATE TABLE IF NOT EXISTS routines (name TEXT PRIMARY KEY, description TEXT, steps TEXT, "
                       "created REAL, runs INTEGER DEFAULT 0, last_run REAL)")
            db.commit()

    def save(self, name: str, steps_text: str, known_tools: set[str], description: str = "") -> Routine:
        name = norm_name(name)
        if not NAME.match(name):
            raise ValueError("Give the routine a short name, like 'focus mode' or 'start my day'.")
        r = Routine(name, parse_steps(steps_text, known_tools), description.strip())
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO routines(name, description, steps, created, runs) VALUES "
                            "(?,?,?,?,COALESCE((SELECT runs FROM routines WHERE name=?),0))",
                            (r.name, r.description, json.dumps(r.steps), time.time(), r.name))
            self.db.commit()
        return r

    def delete(self, name: str) -> bool:
        with self.lock:
            n = self.db.execute("DELETE FROM routines WHERE name=?", (norm_name(name),)).rowcount
            self.db.commit()
        return bool(n)

    def get(self, name: str) -> Routine | None:
        with self.lock:
            row = self.db.execute("SELECT name, description, steps FROM routines WHERE name=?", (norm_name(name),)).fetchone()
        return Routine(row[0], json.loads(row[2]), row[1]) if row else None

    def list(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT name, description, steps, runs, last_run FROM routines ORDER BY name").fetchall()
        return [{"name": n, "description": d, "steps": step_lines(s), "runs": r, "last_run": lr} for n, d, s, r, lr in rows]

    def match(self, text: str) -> Routine | None:
        """'focus mode', 'run focus mode', 'Jarvis, start my day please' -> the routine, if one has that name.
        Only exact names (after dropping a wake word, a verb like 'run' and 'please') count, so ordinary
        sentences never trigger a routine by accident."""
        if len(text) > 80:
            return None
        t = norm_name(re.sub(r"[.!?,]+", " ", text))
        t = re.sub(r"^(?:hey\s+)?(?:aether|jarvis)\s+", "", t)
        t = re.sub(r"^please\s+|\s+please$", "", t).strip()
        candidates = [t]
        for _ in range(3):  # peel a verb, 'the'/'my', and 'routine'/'mode' in any order
            for c in list(candidates):
                verb = re.match(r"^(?:run|start|activate|begin|do|enable|launch|go into|switch to|turn on)\s+(.*)$", c)
                more = [verb.group(1)] if verb else []
                more += [c[len(p):] for p in ("the ", "my ") if c.startswith(p)]
                more += [c[: -len(x)] for x in (" routine", " mode") if c.endswith(x)]
                candidates += [m for m in more if m and m not in candidates]
        candidates += [c + " mode" for c in candidates if not c.endswith(" mode")]
        for c in candidates:
            r = self.get(c)
            if r:
                return r
        return None

    def run(self, routine: Routine, tools, on_step=None) -> list:
        results = []
        for step in routine.steps:
            r = tools.call(step["tool"], dict(step["args"]))
            results.append(r)
            if on_step:
                on_step(step, r)
            if r.decision in {"deny", "declined"}:
                break  # a refused step stops the routine: later steps may depend on it
        with self.lock:
            self.db.execute("UPDATE routines SET runs=runs+1, last_run=? WHERE name=?", (time.time(), routine.name))
            self.db.commit()
        return results


def step_lines(steps_json: str) -> str:
    return "\n".join(step_text(s) for s in json.loads(steps_json))


def summary(routine: Routine, results: list) -> str:
    marks = []
    for step, r in zip(routine.steps, results):
        mark = "✓" if r.ok else ("declined" if r.decision == "declined" else "blocked" if r.decision == "deny" else "✕")
        note = f" ({mark}: {r.output[:120]})" if not r.ok else (
            f" → {r.output.strip()}" if r.output and "\n" not in r.output.strip() and len(r.output) < 120 else "")
        marks.append(f"{'✓' if r.ok else '✕'} {step_text(step)}{note}")
    skipped = len(routine.steps) - len(results)
    done = sum(r.ok for r in results)
    head = f"Ran **{routine.name}**: {done} of {len(routine.steps)} step{'s' if len(routine.steps) != 1 else ''} done."
    tail = [f"Stopped early, so {skipped} step{'s' if skipped != 1 else ''} didn't run."] if skipped else []
    return "\n".join([head, *("- " + m for m in marks), *tail])
