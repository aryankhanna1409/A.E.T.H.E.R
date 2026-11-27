"""A world model of you: AETHER's running estimate of how you are right now, and what you'll need next.

Not a guess from the model: a small, explainable estimate from signals AETHER already has.
* Energy: last night's sleep (Grind 75), late nights this week, the time of day against your sharp hours and the
  after-lunch dip, and how long you've been at the keyboard today.
* Focus: how long the current stretch has run, and whether the app in front is deep work.
* Load: what's still on the calendar today, what's due, and deadlines in the next three days.
* Body: what's left of today's 75 Hard habits and protein.
* Mood: the wellbeing read from your last messages.

From that it predicts what would help next ("your next event is in 25 minutes", "60 g of protein to go", "you've
been heads-down for two hours"), which the HUD and Today show, the prompt includes, and nudges can use.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

DEEP_APPS = re.compile(r"code|pycharm|idea|studio|terminal|powershell|cmd|notion|obsidian|word|excel|matlab|jupyter|"
                       r"cursor|vim|ros|leetcode|neetcode", re.I)


@dataclass
class UserState:
    energy: float = 0.65
    focus: float = 0.0
    load: float = 0.0
    mood: str = "steady"
    body: str = ""
    why: dict = field(default_factory=dict)
    needs: list[str] = field(default_factory=list)

    @staticmethod
    def word(v: float, words=("low", "okay", "good", "high")) -> str:
        return words[min(len(words) - 1, int(v * len(words)))]

    def line(self) -> str:
        s = (f"Your state (estimated): energy {self.word(self.energy)}, focus {self.word(self.focus, ('scattered', 'warming up', 'in flow', 'deep'))}, "
             f"load {self.word(self.load, ('light', 'moderate', 'busy', 'packed'))}, mood {self.mood}.")
        return s + (f" Likely next need: {self.needs[0]}" if self.needs else "")

    def json(self) -> dict:
        return {"energy": round(self.energy, 2), "focus": round(self.focus, 2), "load": round(self.load, 2),
                "mood": self.mood, "body": self.body, "why": self.why, "needs": self.needs,
                "words": {"energy": self.word(self.energy),
                          "focus": self.word(self.focus, ('scattered', 'warming up', 'in flow', 'deep')),
                          "load": self.word(self.load, ('light', 'moderate', 'busy', 'packed'))}}


class WorldModel:
    def __init__(self, aether):
        self.a = aether

    def estimate(self, now: datetime | None = None) -> UserState:
        now = now or datetime.now()
        st = UserState()
        day = now.date().isoformat()
        e, why = 0.65, {}
        g = getattr(self.a, "grind", None)
        sleep = None
        if g is not None:
            try:
                for d in (day, (now.date() - timedelta(days=1)).isoformat()):
                    v = float((g.state.get("days", {}).get(d) or {}).get("sleepH") or 0)
                    if v > 0:
                        sleep = v
                        break
            except Exception:
                pass
        if sleep is not None:
            e = 0.85 if sleep >= 7.5 else 0.7 if sleep >= 6.5 else 0.5 if sleep >= 5.5 else 0.3
            why["sleep"] = f"{sleep:g} h sleep"
        pe = getattr(self.a, "proactive", None)
        f = getattr(getattr(pe, "awareness", None), "focus", None)
        if f is not None:
            try:
                late = f.late_nights(now.date())
                if late:
                    e -= min(0.2, 0.07 * late)
                    why["late_nights"] = f"{late} late night{'s' if late > 1 else ''} this week"
                hours = f.hours_on(day)
                if hours >= 8:
                    e -= 0.15
                    why["hours"] = f"{hours:.1f} h at the keyboard today"
                mins = f.minutes(now.timestamp())
                deep = bool(DEEP_APPS.search(f.main_app() or ""))
                st.focus = min(1.0, mins / 120) * (1.0 if deep else 0.6)
                if mins >= 20:
                    why["stretch"] = f"{mins:.0f} min stretch" + (f" in {f.main_app()}" if f.main_app() else "")
            except Exception:
                pass
        dp = getattr(self.a.cfg, "dayplan", None)
        peak = tuple(dp.peak_hours) if dp else (9, 13)
        h = now.hour + now.minute / 60
        if peak[0] <= h < peak[1]:
            e += 0.08
        elif 14 <= h < 16:
            e -= 0.08
            why["dip"] = "after-lunch dip"
        elif h >= 22 or h < 5:
            e -= 0.2
            why["late"] = "late"
        st.energy = max(0.0, min(1.0, e))
        st.why = why
        # load: calendar, due items, near deadlines
        events, nxt = [], None
        try:
            events = [x for x in self.a.calendar.day(now.date()) if not x.all_day and x.end > now]
            nxt = min((x for x in events if x.start > now), key=lambda x: x.start, default=None)
        except Exception:
            pass
        due = 0
        try:
            due = len(self.a.reminders.due(now.date()))
        except Exception:
            pass
        st.load = max(0.0, min(1.0, 0.18 * len(events) + 0.08 * due))
        if events or due:
            why["load"] = f"{len(events)} event{'s' if len(events) != 1 else ''} left, {due} due"
        # mood
        try:
            mode = self.a.wm.wellness_mode
            st.mood = {"stressed": "stressed", "low": "low", "self_doubt": "doubting yourself",
                       "crisis": "struggling"}.get(mode or "", "steady")
        except Exception:
            pass
        # body: 75 Hard and protein
        protein_left = None
        if g is not None:
            try:
                sc = g.day_score(day)
                left = sc["total"] - sc["done"]
                protein_left = max(0.0, float(g.state["targets"]["protein"]) - g.totals(day)["protein"])
                st.body = f"{left} of {sc['total']} habits left, {protein_left:.0f} g protein to go"
            except Exception:
                pass
        # what you'll likely need next, most urgent first
        if nxt is not None:
            mins = (nxt.start - now).total_seconds() / 60
            if mins <= 45:
                from ..security import injection

                def safe(s: str) -> str:   # invites are written by other people: never pass on instructions
                    return "an event" if injection.scan(s) else injection.neutralize(s)[:80]
                loc = getattr(nxt, "location", "")
                st.needs.append(f"{safe(nxt.title)} in {mins:.0f} min" + (f" ({safe(loc)})" if loc else ""))
        if st.focus >= 0.75 and st.energy < 0.6:
            st.needs.append("a five-minute break: long stretch on low energy")
        if st.energy < 0.4 and h < 21:
            st.needs.append("something light next, and an early night")
        if protein_left and protein_left >= 50 and h >= 17:
            st.needs.append(f"{protein_left:.0f} g of protein still to eat today")
        if st.load >= 0.7 and h < 12:
            st.needs.append('a plan for the day ("plan my day")')
        if h >= 22.5 and f is not None and f.minutes(now.timestamp()) > 30:
            st.needs.append("stopping at a clean point; it's late")
        return st


def handle(a, text: str, spoken: bool = False, now: datetime | None = None) -> str | None:
    t = re.sub(r"^(?:(?:hey|ok|okay)\s+)?(?:aether|nova)[\s,]+", "", text.strip(), flags=re.I).strip(" ?.!")
    if not re.match(r"^(?:how\s+am\s+i\s+(?:doing|looking)(?:\s+today)?|what\s+do\s+i\s+need(?:\s+(?:next|now))?|"
                    r"what\s+should\s+i\s+do\s+next|read\s+me|how'?s\s+my\s+(?:energy|state))$", t, re.I):
        return None
    st = a.world.estimate(now or a.clock())
    w = st.json()["words"]
    if spoken:
        s = f"Energy {w['energy']}, focus {w['focus']}, and your day looks {w['load']}."
        return s + (f" Next, I'd say: {st.needs[0]}." if st.needs else " Nothing pressing.")
    lines = [f"**Energy** {w['energy']} · **Focus** {w['focus']} · **Load** {w['load']} · **Mood** {st.mood}"]
    if st.why:
        lines.append("_Based on: " + "; ".join(st.why.values()) + "._")
    if st.body:
        lines.append(f"**75 Hard:** {st.body}.")
    if st.needs:
        lines += ["", "**What would help next:**"] + [f"- {n}" for n in st.needs]
    return "\n".join(lines)
