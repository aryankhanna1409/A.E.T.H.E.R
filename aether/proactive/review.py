"""Status report and weekly review: two things a real assistant says without being asked twice.

* "Status report" / "systems check" / "run diagnostics": every part of AETHER in one breath (brain, memory,
  knowledge, learning, voice, privacy, the PC itself), plus how *you're* doing today.
* "How did my week go?" / "weekly review": the week in numbers (time at the keyboard, 75 Hard days, workouts,
  weight, what changed in your plan, what AETHER learned), and what next week is about. Sunday evening it offers
  this on its own (a learned nudge, so it fades if you never open it).

Both are composed by code from what AETHER already tracks, so they're instant and never make anything up.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from ..core.version import MARK

STATUS = re.compile(r"^(?:(?:give\s+me\s+(?:a|the)\s+|run\s+(?:a\s+)?)?(?:status\s+report|systems?\s+check|diagnostics?|"
                    r"self[\s-]?test)|run\s+diagnostics|how\s+are\s+(?:your|all)\s+systems|are\s+you\s+(?:fully\s+)?"
                    r"(?:online|operational)|systems?\s+status)(?:\s+please)?$", re.I)
WEEK = re.compile(r"^(?:how\s+(?:did|was)\s+my\s+week(?:\s+go)?|weekly\s+review|(?:my\s+)?week\s+in\s+review|"
                  r"review\s+(?:my|the|this)\s+week|how'?s\s+my\s+week\s+(?:going|been))(?:\s+please)?$", re.I)


def _clean(text: str) -> str:
    t = re.sub(r"^(?:(?:hey|ok|okay)\s+)?(?:aether|nova)[\s,]+", "", text.strip(), flags=re.I)
    return t.strip(" ?.!")


# ------------------------------------------------------------------ status report
def status(a, spoken: bool = False, now: datetime | None = None) -> str:
    now = now or datetime.now()
    rows: list[tuple[str, str, bool]] = []   # (system, state, ok)
    llm = getattr(a, "llm", None)
    model = getattr(getattr(a.cfg, "llm", None), "model_path", "") or ""
    model = re.sub(r"(-0000\d-of-0000\d)?\.gguf$", "", model.replace("\\", "/").rsplit("/", 1)[-1]) or "no model set"
    rows.append(("Brain", f"{model} loaded" if llm is not None else f"{model}, not loaded", llm is not None))
    try:
        emb = getattr(a.store.embedder, "name", None)
        rows.append(("Memory", f"{a.store.count():,} memories, "
                     + ("hybrid search" if emb else "keyword search only (no embedding model)")
                     + (", encrypted" if a.store.cipher.enabled else ", NOT encrypted"), a.store.cipher.enabled))
    except Exception:
        rows.append(("Memory", "unavailable", False))
    try:
        rules = len(getattr(a.rules, "rules", []))
        rows.append(("Knowledge", f"{len(a.kg.all()):,} facts, {rules} rules", True))
    except Exception:
        pass
    im = getattr(a, "intent_model", None)
    if im is not None:
        try:
            i = im.info()
            rows.append(("Router", f"{'on' if i['active'] else 'learning'}, {i['examples']} examples, "
                                   f"{i.get('skipped', 0)} model calls saved", True))
        except Exception:
            pass
    sug = getattr(a, "suggestions", None)
    if sug is not None:
        try:
            k = len(sug.list())
            rows.append(("Discovery", f"{k} rule{'s' if k != 1 else ''} or habit{'s' if k != 1 else ''} waiting for "
                                      "your OK" if k else "nothing new to approve", True))
        except Exception:
            pass
    pe = getattr(a, "proactive", None)
    if pe is not None and getattr(pe, "policy", None) is not None:
        try:
            t = pe.policy.table()
            quiet = [f"{x['kind']} in the {x['part']}" for x in t if x["rate"] < 0.35 and x["seen"] >= 3]
            rows.append(("Nudges", "learning what you act on" + (f"; holding back {', '.join(quiet[:2])}" if quiet else ""),
                         True))
        except Exception:
            pass
    sec = getattr(a.cfg, "security", None)
    if sec is not None:
        rows.append(("Privacy", ("online lookups on" if sec.network else "fully offline")
                     + ("; EMERGENCY STOP is on" if getattr(a.policy, "emergency_stop", False) else ""),
                     not getattr(a.policy, "emergency_stop", False)))
    try:
        import psutil
        b = psutil.sensors_battery()
        vm = psutil.virtual_memory()
        pc = f"RAM {vm.percent:.0f}% used"
        if b:
            pc = f"battery {b.percent:.0f}%{' and charging' if b.power_plugged else ''}, " + pc
        rows.append(("PC", pc, not (b and b.percent < 20 and not b.power_plugged)))
    except Exception:
        pass
    you = []
    g = getattr(a, "grind", None)
    if g is not None:
        try:
            day = now.date().isoformat()
            sc = g.day_score(day)
            ch = g.challenge(day)
            if ch.get("started"):
                you.append(f"75 Hard day {ch['dayInBlock']}, {sc['done']} of {sc['total']} habits done")
        except Exception:
            pass
    if pe is not None:
        try:
            h = pe.awareness.focus.hours_on(now.date().isoformat())
            if h >= 0.25:
                you.append(f"{h:.1f} h at the keyboard")
        except Exception:
            pass
    bad = [r for r in rows if not r[2]]
    if spoken:
        head = f"AETHER {MARK}. " + ("All systems normal." if not bad else
                                     "Mostly fine, but " + "; ".join(f"{s.lower()}: {st}" for s, st, _ in bad) + ".")
        return head + (f" You: {', '.join(you)}." if you else "")
    lines = [f"**AETHER {MARK}: {'all systems normal' if not bad else f'{len(bad)} thing' + ('s need' if len(bad) > 1 else ' needs') + ' attention'}**", ""]
    lines += [f"- {'✅' if ok else '⚠️'} **{s}**: {st}" for s, st, ok in rows]
    if you:
        lines += ["", f"**You today:** {', '.join(you)}."]
    return "\n".join(lines)


# ------------------------------------------------------------------ weekly review
def week_bounds(now: datetime) -> tuple[date, date]:
    """Monday to today, or the whole of last week on a Monday morning (so Monday's review looks back)."""
    d = now.date()
    if d.weekday() == 0 and now.hour < 12:
        return d - timedelta(days=7), d - timedelta(days=1)
    return d - timedelta(days=d.weekday()), d


def weekly(a, spoken: bool = False, now: datetime | None = None) -> str:
    now = now or datetime.now()
    start, end = week_bounds(now)
    days = [(start + timedelta(days=i)) for i in range((end - start).days + 1)]
    keys = [d.isoformat() for d in days]
    facts: list[str] = []
    wins: list[str] = []
    pe = getattr(a, "proactive", None)
    try:
        f = pe.awareness.focus
        hours = sum(f.hours_on(k) for k in keys)
        if hours >= 0.5:
            best = max(keys, key=f.hours_on)
            facts.append(f"{hours:.0f} h at the keyboard, most on {date.fromisoformat(best):%A} ({f.hours_on(best):.1f} h)")
            long_days = sum(f.hours_on(k) >= 10 for k in keys)
            if long_days >= 2:
                facts.append(f"{long_days} days over 10 hours, which is a lot; protect one rest day next week")
    except Exception:
        pass
    g = getattr(a, "grind", None)
    if g is not None:
        try:
            full = [k for k in keys if (s := g.day_score(k)) and s.get("total") and s.get("done") == s.get("total")]
            facts.append(f"75 Hard: {len(full)} of {len(keys)} days complete, streak {g.streak(end.isoformat())}")
            if len(full) == len(keys):
                wins.append("every 75 Hard day done")
        except Exception:
            pass
        try:
            w = g.workouts_this_week(end.isoformat())
            facts.append(f"{w} workout{'s' if w != 1 else ''}")
        except Exception:
            pass
        try:
            ws = [(d, v) for d, v in g.weights() if start.isoformat() <= d <= end.isoformat()]
            if len(ws) >= 2:
                delta = ws[-1][1] - ws[0][1]
                facts.append(f"weight {ws[0][1]:.1f} → {ws[-1][1]:.1f} kg ({delta:+.1f})")
                target = (g.state.get("profile") or {}).get("targetWeight")
                if target and ((target > ws[0][1] and delta > 0) or (target < ws[0][1] and delta < 0)):
                    wins.append("weight moving toward your target")
        except Exception:
            pass
    try:
        since = datetime.combine(start, datetime.min.time()).timestamp()
        turns = a.store.turns_between(since, now.timestamp(), 2000)
        mine = sum(r == "user" for r, _, _ in turns)
        if mine:
            facts.append(f"{mine} messages to AETHER")
    except Exception:
        pass
    plans = getattr(a, "plans", None)
    done_lines, next_focus = [], []
    if plans is not None:
        try:
            done_lines = [x for x in plans.progress_lines(end) if x]
            nxt = plans.today(end + timedelta(days=7))
            next_focus = nxt.get("focus", [])[:3]
        except Exception:
            pass
    label = "This week" if end == now.date() else "Last week"
    if spoken:
        s = f"{label}: " + ("; ".join(facts[:4]) + "." if facts else "not much tracked yet.")
        if wins:
            s += f" Best part: {wins[0]}."
        if next_focus:
            s += f" Next week is about {', '.join(next_focus[:2])}."
        return s
    lines = [f"**{label} in review ({start:%a %d %b} – {end:%a %d %b})**", ""]
    lines += [f"- {x}" for x in facts] or ["- Not much tracked yet. Log your day in Grind 75 and this fills in."]
    if wins:
        lines += ["", "**Wins:** " + "; ".join(wins) + "."]
    if done_lines:
        lines += ["", "**Plan:**"] + [f"- {x}" for x in done_lines[:6]]
    if next_focus:
        lines += ["", "**Next week:** " + "; ".join(next_focus) + "."]
    return "\n".join(lines)


def handle(a, text: str, spoken: bool = False, now: datetime | None = None) -> str | None:
    t = _clean(text)
    now = now or a.clock()
    if STATUS.match(t):
        return status(a, spoken, now)
    if WEEK.match(t):
        return weekly(a, spoken, now)
    return None
