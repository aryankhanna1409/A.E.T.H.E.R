"""The morning briefing: weather, the day's calendar, what's due, important
email, background work and a few headlines, in one go.

It is assembled by code from facts AETHER already has (plus two small online
lookups), so it is instant, never invents anything, and works without the
language model. There is a written version for the screen and a spoken one
for the voice loop and the HUD.

Say "good morning", "brief me" or "what's my day look like" (after the wake
word, in chat or in the HUD), or just greet AETHER the first time you talk to
it in the morning.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..core.logging import log
from ..memory.mail import is_vip
from ..security import injection

TRIGGER = re.compile(
    r"^(?:(?:hey|ok|okay)\s+)?(?:jarvis|aether)?[\s,.!]*"
    r"(?:good\s+morning(?:\s+(?:jarvis|aether))?|(?:give\s+me\s+(?:my|the)\s+|my\s+|the\s+)?(?:morning|daily)\s+(?:briefing|brief|update)"
    r"|brief\s+me|what'?s\s+(?:on\s+)?(?:my|the)\s+day(?:\s+look(?:ing)?\s+like)?|how'?s\s+my\s+day\s+looking)"
    r"(?:\s+(?:jarvis|aether|please|sir))*[\s.!?]*$", re.I)
GREETING = re.compile(r"^(?:(?:hey|hi|hello|ok|okay)\s+)?(?:jarvis|aether)?[\s,.!]*(?:hi|hello|hey|morning|good\s+morning|"
                      r"i'?m\s+up|wake\s+up)?(?:\s+(?:jarvis|aether))?[\s.!?]*$", re.I)


def is_request(text: str) -> bool:
    return len(text) <= 80 and bool(TRIGGER.match(text.strip()))


def is_greeting(text: str) -> bool:
    t = text.strip()
    return 0 < len(t) <= 40 and bool(GREETING.match(t)) and bool(re.search(r"\w", t))


@dataclass
class Briefing:
    written: str
    spoken: str
    sections: dict = field(default_factory=dict)


def _safe(s: str) -> str:
    """Headlines, invite titles and subjects come from strangers: anything that tries to instruct is dropped."""
    return "" if injection.scan(s) else injection.neutralize(s)


def _when(e) -> str:
    return "all day" if e.all_day else f"{e.start:%H:%M}"


def _clock(dt: datetime) -> str:
    """'9', '9:30', '14:15': short, and read naturally by a 24-hour voice."""
    return str(dt.hour) if dt.minute == 0 else f"{dt.hour}:{dt.minute:02d}"


def compose(a, now: datetime | None = None, online: bool | None = None) -> Briefing:
    """Build the briefing for `a` (an Aether). Every part is optional: a part that fails is left out."""
    now = now or datetime.now()
    online = a.cfg.security.network if online is None else online
    name = a.cfg.address_as or a.cfg.user_name
    part = "morning" if now.hour < 12 else "afternoon" if now.hour < 17 else "evening"
    w_lines, s_lines, sections = [f"**Good {part}, {name}.** It's {now:%A %d %B}, {now:%H:%M}."], \
        [f"Good {part}, {name}. It's {now:%A the} {now.day}{_th(now.day)}."], {}

    weather = headlines = None
    if online and getattr(a, "online", None) is not None:
        with ThreadPoolExecutor(2) as pool:  # both lookups at once, each with its own timeout
            fw = pool.submit(a.online.weather) if a.cfg.online.location else None
            digest = getattr(a, "digest", None)
            fn = (pool.submit(digest.for_briefing, a.cfg.online.briefing_headlines + 2) if digest is not None else
                  pool.submit(a.online.headlines, "", a.cfg.online.briefing_headlines + 4) if a.cfg.online.news_feeds else None)
            for fut, label in ((fw, "weather"), (fn, "news")):
                if fut is None:
                    continue
                try:
                    if label == "weather":
                        weather = fut.result(timeout=10)
                    else:
                        headlines = fut.result(timeout=10)
                except Exception as e:
                    log.warning("briefing %s unavailable: %s", label, e)
    if weather:
        sections["weather"] = weather.line(0)
        w_lines.append("🌤 " + weather.line(0) + ".")
        s_lines.append(weather.line(0, spoken=True))

    try:
        events = [e for e in a.calendar.day(now.date()) if e.all_day or e.end > now]
    except Exception:
        events = []
    timed = [e for e in events if not e.all_day]
    if events:
        sections["calendar"] = [e.as_dict() for e in events]
        w_lines.append("**Today:** " + "; ".join(f"{_when(e)} {_safe(e.title) or '(event)'}" +
                                                 (f" ({_safe(e.location)})" if e.location and _safe(e.location) else "")
                                                 for e in events[:6]))
        if timed:
            first = timed[0]
            title = _safe(first.title) or "an event"
            lead = "First up" if first.start > now else "Right now"
            more = f", then {len(timed) - 1} more" if len(timed) > 1 else ""
            when = f"at {_clock(first.start)}" if first.start > now else f"until {_clock(first.end)}"
            s_lines.append(f"{lead}: {title} {when}{more}.")
            if len(timed) > 1:
                last = timed[-1]
                s_lines.append(f"You're done by {_clock(last.end)}.")
        for e in events:
            if e.all_day and _safe(e.title):
                s_lines.append(f"Also, {_safe(e.title)} today.")
                break
    elif a.calendar.files() or a.cfg.calendar.outlook:
        w_lines.append("**Today:** nothing on the calendar.")
        s_lines.append("Your calendar is clear today.")

    due = a.reminders.due(now.date())
    if due:
        sections["due"] = [r.text for r in due]
        w_lines.append("**Due:** " + "; ".join(f"{r.text}" + (" (overdue)" if r.due < now.date().isoformat() else "")
                                                for r in due[:6]))
        overdue = sum(r.due < now.date().isoformat() for r in due)
        s_lines.append(f"{len(due)} thing{'s' if len(due) != 1 else ''} due" + (f", {overdue} overdue" if overdue else "")
                       + f": {_list([r.text for r in due[:3]])}.")

    plans = getattr(a, "plans", None)
    try:
        t = plans.today(now.date()) if plans else {}
    except Exception:
        t = {}
    if t:
        sections["plan"] = t
        left = f", {t['days_left']} days to go" if t["days_left"] is not None else ""
        w_lines.append(f"**Plan · {t['horizon']}{left}:**" + (f" {t['goal']}." if t["goal"] else "")
                       + ("\n- Every day: " + "; ".join(t["daily"]) if t["daily"] else "")
                       + ("\n- " + (t["block"] or "Next up") + ": " + "; ".join(t["focus"][:5]) if t["focus"] else ""))
        s_lines.append(f"{t['horizon']}{left}." + (f" Today: {_list(t['daily'][:3])}." if t["daily"] else "")
                       + (f" This week: {_list(t['focus'][:2])}." if t["focus"] else ""))

    grind = getattr(a, "grind", None)
    if grind is not None and grind.state.get("days"):
        try:
            c = grind.challenge(now.date().isoformat())
            yest = (now.date() - timedelta(days=1)).isoformat()
            ys = grind.day_score(yest)
            streak = grind.streak(now.date().isoformat())
            rest = grind.rest_advice(now.date().isoformat())
            line = (f"Day {c['dayInBlock']} of 75" if c["started"] else f"Starts in {c['daysUntil']} days") + \
                (f", {streak}-day streak" if streak else "") + f". Yesterday {ys['done']}/{ys['total']} habits."
            T = grind.state["targets"]
            line += f" Targets: {T['kcal']} kcal, {T['protein']} g protein."
            sections["grind"] = {"line": line, "rest": rest}
            w_lines.append("**75 Hard:** " + line + (f"\n- {rest}" if rest else ""))
            s_lines.append(line + (" " + rest.split(":")[0] + " today, I'd say." if rest else ""))
        except Exception as e:
            log.warning("briefing grind section failed: %s", e)

    try:
        mail = a.mail.all(now - timedelta(hours=18))
    except Exception:
        mail = []
    if mail:
        vip = [m for m in mail if is_vip(m, set(a.cfg.mail.vip))]
        sections["mail"] = {"count": len(mail), "vip": [m.as_dict() for m in vip]}
        w_lines.append(f"**Email:** {len(mail)} since yesterday evening" + (
            ": " + "; ".join(f"★ {m.sender}: {_safe(m.subject) or '(subject hidden)'}" for m in vip[:4]) if vip else "."))
        if vip:
            people = list(dict.fromkeys(m.sender for m in vip))
            s_lines.append(f"Email from {_list(people[:3])}" + (f", about {_safe(vip[0].subject)}" if len(people) == 1 and _safe(vip[0].subject) else "") + ".")
        else:
            s_lines.append(f"{len(mail)} new email{'s' if len(mail) != 1 else ''}, nothing flagged.")

    running = [g for g in a.graph.goals(20) if g["status"] in {"running", "pending"}]
    if running:
        sections["goals"] = [g["text"] for g in running]
        w_lines.append("**Still working on:** " + "; ".join(_short(g["text"], 80) for g in running[:3]))
        s_lines.append(f"I'm still working on {_list([_short(g['text'].split(':')[0], 60, '') for g in running[:2]])}.")

    yours = [g for g in a.graph.goals(50) if g["status"] == "active"]   # your own goals, tracked in Mission control
    if yours:
        sections["your_goals"] = [g["text"] for g in yours]
        w_lines.append("**Your goals:** " + "; ".join(_short(g["text"], 70) for g in yours[:4]))

    if headlines:
        safe = [h for h in headlines if _safe(h.title)][:a.cfg.online.briefing_headlines]
        src = lambda h: ", ".join(h.sources[:2]) if hasattr(h, "sources") else h.source  # noqa: E731 - digest story or plain headline
        why = lambda h: f" · _for: {h.why}_" if getattr(h, "why", "") else ""  # noqa: E731
        sections["news"] = [{"title": h.title, "source": src(h), "link": h.link} for h in safe]
        if safe:
            w_lines.append("**While you were away:**\n" + "\n".join(f"- [{_safe(h.title)}]({h.link}) · {src(h)}{why(h)}" if h.link.startswith("http")
                                                                   else f"- {_safe(h.title)} · {src(h)}{why(h)}" for h in safe))
            s_lines.append("In the news: " + " ".join(f"{_safe(h.title).rstrip('.')}." for h in safe[:3]))

    if len(s_lines) == 1:
        s_lines.append("Nothing scheduled and nothing due. A clear day.")
        w_lines.append("Nothing scheduled and nothing due. A clear day.")
    if timed and timed[0].start > now and (timed[0].start - now) <= timedelta(minutes=45):
        s_lines.append(f"You'll want to leave soon for {_safe(timed[0].title) or 'your first event'}.")
    return Briefing("\n\n".join(w_lines), " ".join(s_lines), sections)


def _short(t: str, n: int, more: str = "…") -> str:
    t = t.strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0].rstrip(",;:") + more


def _list(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _th(d: int) -> str:
    return "th" if 11 <= d % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
