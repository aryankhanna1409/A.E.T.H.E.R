"""Plans by time horizon: what you're doing now, this week, before a milestone, and later.

Long-range plans ("an MBA after two years of work") used to sit beside this week's work with nothing to tell them
apart, so AETHER talked about the far future when you needed today. Plans live in one Markdown file you can edit
anywhere (``plans.md`` in AETHER's data folder, or the Plans card on Today):

    # Plans
    ## Now · until 2027-01-18
    Goal: start the internship strong
    ### Daily
    - [ ] Solve 3-5 NeetCode 150 problems
    - [ ] Commit to GitHub
    ### Week 1 · 2026-11-26 to 2026-12-02
    - [ ] Claude Code course (DeepLearning.AI)
    - [x] Set up the study guide
    ## 2027 · 2027-01-18 to 2027-06-30
    - [ ] Get the full-time conversion offer
    ## Later
    - [ ] MBA after about two years of work

A level-2 heading is a horizon, optionally with an end date ("until 2027-01-18") or a range; a level-3 heading
inside it is a block ("Daily", or a week with a date range). AETHER uses the horizon that is current today, leads
with it in the briefing and in answers, ticks items off when you say "done with the Claude Code course", and turns
the Daily list into today's checklist.
"""
from __future__ import annotations

import difflib
import re
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

DATE = r"(\d{4}-\d{2}-\d{2})"
ITEM = re.compile(r"^\s*[-*]\s*\[( |x|X)\]\s*(.+?)\s*$")
RANGE = re.compile(rf"{DATE}\s*(?:to|→|->|–|-|until)\s*{DATE}")
UNTIL = re.compile(rf"(?:until|till|by|before)\s+{DATE}", re.I)
FROM = re.compile(rf"(?:from|after|starting)\s+{DATE}", re.I)


@dataclass
class Item:
    text: str
    done: bool
    line: int


@dataclass
class Block:
    title: str
    start: date | None = None
    end: date | None = None
    items: list[Item] = field(default_factory=list)

    @property
    def daily(self) -> bool:
        return bool(re.match(r"daily|every\s*day|each\s+day", self.title, re.I))

    def covers(self, d: date) -> bool:
        return (self.start is None or self.start <= d) and (self.end is None or d <= self.end)


@dataclass
class Horizon:
    title: str
    start: date | None = None
    end: date | None = None
    goal: str = ""
    blocks: list[Block] = field(default_factory=list)

    def covers(self, d: date) -> bool:
        return (self.start is None or self.start <= d) and (self.end is None or d <= self.end)

    @property
    def items(self) -> list[Item]:
        return [i for b in self.blocks for i in b.items]

    @property
    def name(self) -> str:
        return re.split(r"\s*[·|(]\s*|\s+(?:until|from|by)\s+", self.title)[0].strip()


def _dates(title: str) -> tuple[date | None, date | None]:
    if m := RANGE.search(title):
        return date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
    start = date.fromisoformat(m.group(1)) if (m := FROM.search(title)) else None
    end = date.fromisoformat(m.group(1)) if (m := UNTIL.search(title)) else None
    return start, end


def parse(text: str) -> list[Horizon]:
    out: list[Horizon] = []
    for n, line in enumerate(text.splitlines()):
        if line.startswith("## "):
            s, e = _dates(line[3:])
            out.append(Horizon(line[3:].strip(), s, e, blocks=[Block("")]))
        elif line.startswith("### ") and out:
            s, e = _dates(line[4:])
            out[-1].blocks.append(Block(line[4:].strip(), s, e))
        elif out and (m := ITEM.match(line)):
            out[-1].blocks[-1].items.append(Item(m.group(2), m.group(1).lower() == "x", n))
        elif out and re.match(r"^\s*goal\s*:", line, re.I):
            out[-1].goal = line.split(":", 1)[1].strip()
    return out


class Plans:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def text(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def save(self, text: str) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)

    def horizons(self) -> list[Horizon]:
        return parse(self.text())

    def current(self, today: date | None = None) -> Horizon | None:
        """The first horizon whose dates include today; horizons without dates count as "now" only when no dated
        one does, and one called Later/Someday/Long term never does."""
        today = today or date.today()
        hs = self.horizons()
        dated = [h for h in hs if (h.start or h.end) and h.covers(today)]
        if dated:
            return dated[0]
        later = re.compile(r"later|someday|long[- ]term|future", re.I)
        return next((h for h in hs if not (h.start or h.end) and not later.search(h.title)), None)

    def today(self, today: date | None = None) -> dict:
        today = today or date.today()
        h = self.current(today)
        if h is None:
            return {}
        daily = [i.text for b in h.blocks if b.daily for i in b.items]
        week = next((b for b in h.blocks if not b.daily and (b.start or b.end) and b.covers(today)), None)
        undated = [i for b in h.blocks if not b.daily and not (b.start or b.end) for i in b.items if not i.done]
        focus = [i.text for i in (week.items if week else undated) if not i.done]
        done = sum(1 for i in h.items if i.done)
        total = sum(1 for b in h.blocks if not b.daily for i in b.items)
        return {"horizon": h.name, "title": h.title, "goal": h.goal, "end": h.end.isoformat() if h.end else None,
                "days_left": (h.end - today).days if h.end else None, "daily": daily,
                "block": week.title if week else "", "focus": focus[:8], "done": done, "total": total,
                "later": [f"{x.name} ({'; '.join(i.text for i in x.items if not i.done)[:160]})" for x in self.horizons()
                          if x.title != h.title and not (x.end and x.end < today)]}

    def progress_lines(self, today: date | None = None) -> list[str]:
        t = self.today(today)
        if not t:
            return []
        out = [f"{t['done']} of {t['total']} items done in '{t['horizon']}'"
               + (f", {t['days_left']} days to go" if t["days_left"] is not None else "")]
        if t["goal"]:
            out.append(f"working toward: {t['goal']}")
        return out

    def complete(self, said: str) -> str | None:
        """Tick off the open item that best matches what was said. Returns its text, or None."""
        with self.lock:
            text = self.text()
            lines = text.splitlines()
            best, score = None, 0.0
            s = said.lower()
            for n, line in enumerate(lines):
                m = ITEM.match(line)
                if not m or m.group(1).lower() == "x":
                    continue
                cand = m.group(2).lower()
                r = difflib.SequenceMatcher(a=s, b=cand).ratio()
                words = [w for w in re.findall(r"[a-z0-9]+", s) if len(w) > 2]
                if words:
                    r = max(r, sum(w in cand for w in words) / len(words) * 0.9)
                if r > score:
                    best, score = n, r
            if best is None or score < 0.55:
                return None
            lines[best] = re.sub(r"\[ \]", "[x]", lines[best], count=1)
            self.path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
            return ITEM.match(lines[best]).group(2)

    def describe(self, spoken: bool = False, today: date | None = None, scope: str = "today") -> str:
        t = self.today(today)
        if not t:
            return "You don't have a plan written down yet. Add one in Today, under Plans."
        head = t["horizon"] + (f": {t['days_left']} days to go" if t["days_left"] is not None else "") + \
            (f", toward {t['goal']}" if t["goal"] else "") + "."
        parts = [head]
        if t["daily"] and scope in {"today", "all"}:
            parts.append("Every day: " + "; ".join(t["daily"]) + ".")
        if t["focus"]:
            parts.append(("This week" if t["block"] else "Next up") + ": " + "; ".join(t["focus"][:3 if spoken else 8]) + ".")
        if scope == "all" and t["later"]:
            parts.append("Later: " + ", ".join(t["later"]) + ".")
        return " ".join(parts)


ASK = re.compile(r"^(?:what(?:'s| is)\s+(?:my|the)\s+(?:plan|roadmap|focus)|what\s+should\s+i\s+(?:work\s+on|study|do)\s+"
                 r"(?:today|this\s+week|now|next)|what'?s\s+next\s+on\s+my\s+(?:plan|roadmap|list)|my\s+(?:plan|roadmap))"
                 r"(?:\s+(?:for\s+)?(today|this\s+week|now))?\??$", re.I)
ASK_ALL = re.compile(r"^(?:what\s+are\s+my\s+plans|show\s+(?:me\s+)?my\s+plans|my\s+plans)\??$", re.I)
DONE = re.compile(r"^(?:i(?:'ve| have)?\s+)?(?:finished|completed|done\s+with|ticked\s+off|mark\s+(.+?)\s+(?:as\s+)?done)\s*(.*)$", re.I)


def handle(plans: Plans, text: str, spoken: bool = False) -> str | None:
    t = text.strip().rstrip(".!")
    if ASK_ALL.match(t):
        return plans.describe(spoken, scope="all")
    if m := ASK.match(t):
        return plans.describe(spoken, scope="today")
    if (m := DONE.match(t)) and (m.group(1) or m.group(2)):
        item = plans.complete(m.group(1) or m.group(2))
        if item:
            tt = plans.today()
            return f"Ticked off: {item}." + (f" {tt['done']} of {tt['total']} done." if tt else "")
    return None
