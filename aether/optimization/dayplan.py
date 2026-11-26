"""The best order for today: your plan's daily list, this week's focus, what's due and the gym, placed around the
calendar by simulated annealing.

What it optimises (higher is better):
* deep work (problem solving, courses, building) lands in your sharpest hours (``[dayplan].peak_hours``, and the
  hours AETHER has seen you focus longest, once it has a week of data),
* things due today come first, and anything due is never left out if it fits,
* a workout sits at your usual training time when the day allows,
* short breaks between deep blocks, and nothing overlaps a calendar event,
* the day ends by your wind-down time, so overwork isn't planned in.

The plan is a suggestion with its reasoning, not a calendar write: "plan my day" shows it, the morning brief can
include it, and nothing is booked unless you ask.
"""
from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .engine import Problem, annealing

DEEP = re.compile(r"\b(neetcode|leetcode|solve|course|study|learn|build|code|write|project|aether|portfolio|thesis|"
                  r"read(?:ing)?|exam|practice|assignment|paper|research|dsa|sql|aws)\b", re.I)
GYM = re.compile(r"\b(gym|workout|train(?:ing)?|run|lift|exercise|walk)\b", re.I)


@dataclass
class Block:
    name: str
    minutes: int
    deep: bool = False
    due: bool = False
    gym: bool = False


@dataclass
class Busy:
    start: datetime
    end: datetime
    title: str


@dataclass
class DayPlan:
    slots: list[tuple[datetime, datetime, str]] = field(default_factory=list)
    left_out: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def text(self, spoken: bool = False) -> str:
        if not self.slots:
            return "There's nothing to plan for today yet."
        if spoken:
            first = self.slots[:3]
            return "Here's the order I'd go in: " + "; ".join(f"{s:%H:%M} {n}" for s, _, n in first) + \
                (f", then {len(self.slots) - 3} more." if len(self.slots) > 3 else ".")
        lines = [f"- **{s:%H:%M}–{e:%H:%M}** {n}" for s, e, n in self.slots]
        if self.left_out:
            lines.append(f"\n_Didn't fit today: {'; '.join(self.left_out)}._")
        if self.notes:
            lines.append("\n" + " ".join(self.notes))
        return "\n".join(lines)


def estimate_minutes(text: str) -> int:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(h|hr|hours?|m|min|minutes?)\b", text, re.I)
    if m:
        v = float(m.group(1))
        return int(v * 60 if m.group(2).lower().startswith("h") else v)
    if GYM.search(text):
        return 75
    if re.search(r"\b(neetcode|leetcode|problems?)\b", text, re.I):
        return 90
    if re.search(r"\b(commit|email|call|reply|book|pay)\b", text, re.I):
        return 15
    return 60 if DEEP.search(text) else 30


def plan_day(blocks: list[Block], busy: list[Busy], now: datetime, end_hour: int = 22,
             peak: tuple[int, int] = (9, 13), gym_hour: int | None = 6, seed: int = 0) -> DayPlan:
    start = max(now, now.replace(hour=6, minute=0, second=0, microsecond=0))
    start = start.replace(minute=(start.minute // 15) * 15, second=0, microsecond=0) + timedelta(minutes=15 if now.minute % 15 else 0)
    stop = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    busy = sorted(busy, key=lambda b: b.start)
    if not blocks or start >= stop:
        return DayPlan()

    def lay(order: list[int]) -> tuple[list[tuple[datetime, datetime, int]], list[int]]:
        t, out, skipped, prev_deep = start, [], [], False
        for i in order:
            b = blocks[i]
            if prev_deep and b.deep:
                t += timedelta(minutes=10)            # a short break between two deep blocks
            for _ in range(len(busy) + 1):
                end = t + timedelta(minutes=b.minutes)
                clash = next((x for x in busy if x.start < end and t < x.end), None)
                if clash is None:
                    break
                t = clash.end
            end = t + timedelta(minutes=b.minutes)
            if end > stop:
                skipped.append(i)
                continue
            out.append((t, end, i))
            t, prev_deep = end, b.deep
        return out, skipped

    def fitness(order: list[int]) -> float:
        placed, skipped = lay(order)
        f = 0.0
        for s, e, i in placed:
            b = blocks[i]
            mid = s + (e - s) / 2
            if b.deep:
                f += 3.0 if peak[0] <= mid.hour < peak[1] else 1.0 if mid.hour < 17 else 0.3
            if b.due:
                f += 4.0 - (s - start).total_seconds() / 3600 * 0.3
            if b.gym and gym_hour is not None:
                f += max(0.0, 3.0 - abs(s.hour + s.minute / 60 - gym_hour) * 0.5)
            f += 1.0
        f -= sum(6.0 if blocks[i].due else 1.5 for i in skipped)
        return f

    n = len(blocks)
    problem = Problem("day plan", lambda rng: rng.sample(range(n), n),
                      lambda o, rng: _swap(o, rng), fitness,
                      objective="deep work in peak hours, due items early, gym at the usual time, nothing past wind-down")
    best = annealing(problem, steps=min(4000, 400 * n), seed=seed).best if n > 1 else [0]
    placed, skipped = lay(best)
    plan = DayPlan([(s, e, blocks[i].name) for s, e, i in placed], [blocks[i].name for i in skipped])
    deep = [s for s, _, i in placed if blocks[i].deep and peak[0] <= s.hour < peak[1]]
    if deep:
        plan.notes.append(f"Deep work sits in your sharpest hours ({peak[0]}:00–{peak[1]}:00).")
    if any(b.due for b in blocks):
        plan.notes.append("Things due today go first.")
    plan.notes.append(f"Done by {stop:%H:%M} on purpose.")
    return plan


def _swap(order: list[int], rng: random.Random) -> list[int]:
    o = list(order)
    if len(o) > 1:
        i, j = rng.sample(range(len(o)), 2)
        if rng.random() < 0.5:
            o[i], o[j] = o[j], o[i]
        else:
            o.insert(j, o.pop(i))
    return o


def peak_from_focus(day_minutes: dict, hour_minutes: dict | None = None, default: tuple[int, int] = (9, 13)) -> tuple[int, int]:
    """Your sharpest four hours from the focus tracker's per-hour totals, once there's enough data."""
    if not hour_minutes or sum(hour_minutes.values()) < 600:
        return default
    best = max(range(6, 20), key=lambda h: sum(hour_minutes.get(x, 0) for x in range(h, h + 4)))
    return best, best + 4


def gather(a, now: datetime) -> tuple[list[Block], list[Busy]]:
    """Today's blocks from AETHER's plan, reminders and 75 Hard tracker, and the busy times from the calendar."""
    blocks: list[Block] = []
    seen = set()

    def add(name: str, due: bool = False):
        k = name.lower()[:60]
        if k in seen or not name.strip():
            return
        seen.add(k)
        blocks.append(Block(name.strip()[:80], estimate_minutes(name), bool(DEEP.search(name)), due, bool(GYM.search(name))))
    try:
        for r in a.reminders.due(now.date()):
            add(r.text, due=True)
    except Exception:
        pass
    try:
        t = a.plans.today(now.date())
        for x in t.get("daily", []):
            if not re.search(r"\bcommit\b", x, re.I) or len(t.get("daily", [])) < 6:
                add(x)
        for x in t.get("focus", [])[:2]:
            add(x)
    except Exception:
        pass
    try:
        g = a.grind
        if not g.habit_done({"id": "workout"}, now.date().isoformat()) and g.state.get("days"):
            add(f"Workout: {g.next_split()}" if g.next_split() != "Rest / walk" else "Walk and stretch (rest day)")
    except Exception:
        pass
    busy = []
    try:
        for e in a.calendar.day(now.date()):
            if not e.all_day and e.end > now:
                busy.append(Busy(e.start, e.end, e.title))
    except Exception:
        pass
    return blocks, busy


ASK = re.compile(r"^(?:plan\s+my\s+day|what'?s\s+the\s+best\s+order\s+(?:for\s+)?today|how\s+should\s+i\s+(?:plan|order|"
                 r"structure)\s+(?:my\s+)?(?:day|today)|order\s+my\s+day|best\s+order\s+for\s+today)\??$", re.I)


def handle(a, text: str, spoken: bool = False, now: datetime | None = None) -> str | None:
    if not ASK.match(text.strip().rstrip(".!")):
        return None
    now = now or a.clock()
    cfg = getattr(a.cfg, "dayplan", None)
    blocks, busy = gather(a, now)
    if not blocks:
        return "There's nothing on your plan, reminders or tracker for today, so the day is yours."
    focus = getattr(getattr(a, "proactive", None), "awareness", None)
    hours = getattr(getattr(focus, "focus", None), "hour_minutes", None) if focus else None
    peak = peak_from_focus({}, hours, tuple(cfg.peak_hours) if cfg else (9, 13))
    plan = plan_day(blocks, busy, now, end_hour=cfg.wind_down_hour if cfg else 22, peak=peak,
                    gym_hour=cfg.gym_hour if cfg else 6)
    return plan.text(spoken)
