"""Recall by date: "what did we talk about yesterday?", "what did I say about the internship last week?".

Dates are read by code ("yesterday", "last night", "this morning", "on Tuesday", "last week", "3 days ago",
"on 24 November"), and the answer is built from the chats and memories of that window, so it's instant, works
without the model and never makes anything up. With a topic, it searches only that window.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
_MON = "|".join(m[:3] + r"\w*" for m in MONTHS)

ASK = re.compile(
    r"^(?:what\s+did\s+(?:we|i)\s+(?:talk|chat|speak|discuss|say|tell\s+you)(?:\s+about)?|what\s+were\s+we\s+(?:talking|"
    r"chatting)\s+about|remind\s+me\s+what\s+we\s+(?:talked\s+about|discussed)|what\s+did\s+we\s+discuss|"
    r"recap\s+(?:our\s+chats?|what\s+we\s+(?:talked\s+about|discussed))|what\s+happened)\b\s*(?P<rest>.*)$", re.I)

STOP = set("""a an the and or but if to of in on at for with about from by is are was were be been it this that
these those i me my we our you your he she they them his her its what when where how why who which do did does
done just like so really very can could would should will shall not no yes ok okay hey please thanks thank also
then than there here have has had get got going go want need know think one some any all more much too im ive
dont didnt cant wont aether nova good great whats help make sure sir yeah fine well still""".split())


def window(text: str, now: datetime) -> tuple[datetime, datetime, str] | None:
    """The time window a phrase names, as (start, end, how to say it)."""
    t = text.lower()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if re.search(r"\blast\s+night\b", t):
        return day0 - timedelta(hours=6), day0 + timedelta(hours=4), "last night"
    if re.search(r"\bthis\s+morning\b", t):
        return day0 + timedelta(hours=4), day0 + timedelta(hours=12), "this morning"
    if re.search(r"\b(?:today|earlier)\b", t):
        return day0, now, "today"
    if re.search(r"\byesterday\b", t):
        return day0 - timedelta(days=1), day0, "yesterday"
    if m := re.search(r"\b(\d+|a|one|two|three|four|five|six)\s+days?\s+ago\b", t):
        n = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}.get(m.group(1)) or int(m.group(1))
        s = day0 - timedelta(days=n)
        return s, s + timedelta(days=1), f"{n} day{'s' if n > 1 else ''} ago"
    if re.search(r"\blast\s+week\b", t):
        s = day0 - timedelta(days=day0.weekday() + 7)
        return s, s + timedelta(days=7), "last week"
    if re.search(r"\bthis\s+week\b", t):
        return day0 - timedelta(days=day0.weekday()), now, "this week"
    if re.search(r"\b(?:past|last)\s+(?:few|couple(?:\s+of)?)\s+days\b", t):
        return day0 - timedelta(days=3), now, "the last few days"
    if m := re.search(rf"\b(?:on\s+)?(?:last\s+)?({'|'.join(DAYS)})\b", t):
        back = (day0.weekday() - DAYS.index(m.group(1))) % 7 or 7
        s = day0 - timedelta(days=back)
        return s, s + timedelta(days=1), f"on {m.group(1).title()}"
    if m := re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MON})\b|\b({_MON})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t):
        d, mon = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        mi = next(i for i, x in enumerate(MONTHS, 1) if x.startswith(mon[:3]))
        try:
            dd = date(now.year, mi, int(d))
        except ValueError:
            return None
        if dd > now.date():
            dd = dd.replace(year=now.year - 1)
        s = datetime.combine(dd, datetime.min.time())
        return s, s + timedelta(days=1), f"on {dd.day} {MONTHS[mi - 1].title()}"
    return None


def topic_of(rest: str) -> str:
    """What's left once the date words are taken out: 'the internship' in 'about the internship last week'."""
    t = re.sub(r"\b(?:last\s+night|this\s+morning|today|earlier|yesterday|last\s+week|this\s+week|"
               r"(?:past|last)\s+(?:few|couple(?:\s+of)?)\s+days|\w+\s+days?\s+ago|(?:on\s+)?(?:last\s+)?(?:"
               + "|".join(DAYS) + rf")|(?:on\s+)?\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MON})|(?:on\s+)?(?:{_MON})\s+\d{{1,2}}"
               r"(?:st|nd|rd|th)?)\b", " ", rest, flags=re.I)
    t = re.sub(r"^\s*(?:about|regarding|on)\s+", "", t.strip(" ?.!"), flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" ?.!,")


def topics(texts: list[str], n: int = 5) -> list[str]:
    c = Counter()
    for t in texts:
        seen = set()
        for w in re.findall(r"[a-z][a-z0-9+#.-]{2,}", t.lower()):
            w = w.strip(".-")
            if w not in STOP and w not in seen and len(w) > 2:
                c[w] += 1
                seen.add(w)
    return [w for w, k in c.most_common(n * 2) if k >= 1][:n]


def _short(s: str, n: int = 90) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


def recall(store, text: str, now: datetime, spoken: bool = False) -> str | None:
    m = ASK.match(text.strip())
    if not m:
        return None
    w = window(m.group("rest") or "", now)
    if w is None:
        return None
    start, end, label = w
    topic = topic_of(m.group("rest") or "")
    if topic:
        hits = store.search(topic, k=5, since=start.timestamp(), until=end.timestamp())
        turns = [(r, t, c) for r, t, c in store.turns_between(start.timestamp(), end.timestamp(), 400)
                 if r == "user" and not ASK.match(t.strip()) and any(w_ in t.lower() for w_ in topic.lower().split() if w_ not in STOP)]
        if not hits and not turns:
            return f"I don't have anything about {topic} from {label}."
        if spoken:
            first = (turns[0][1] if turns else hits[0].text)
            return f"About {topic}, {label}: you said \"{_short(first, 120)}\"" + \
                (f", and {len(turns) - 1} more things." if len(turns) > 1 else ".")
        lines = [f"**About {topic}, {label}:**"]
        lines += [f"- {datetime.fromtimestamp(c):%a %H:%M} · you: {_short(t, 140)}" for _, t, c in turns[:6]]
        lines += [f"- remembered: {_short(h.text, 140)}" for h in hits[:3] if not any(h.text[:40] in t for _, t, _ in turns)]
        return "\n".join(lines)
    turns = store.turns_between(start.timestamp(), end.timestamp(), 400)
    mine = [(t, c) for r, t, c in turns if r == "user" and not ASK.match(t.strip())]
    mems = [x for x in store.between(start.timestamp(), end.timestamp(), limit=40)
            if x.layer in ("semantic", "episodic", "preference", "prospective")]
    if not mine and not mems:
        return f"We didn't talk {label}." if label != "today" else "We haven't talked yet today."
    tops = topics([t for t, _ in mine])
    if spoken:
        s = f"{label[0].upper() + label[1:]} we had {len(mine)} exchange{'s' if len(mine) != 1 else ''}"
        s += f", mostly about {', '.join(tops[:3])}." if tops else "."
        if mems:
            s += f" I also noted {len(mems)} thing{'s' if len(mems) != 1 else ''} about you."
        return s
    lines = [f"**{label[0].upper() + label[1:]}: {len(mine)} message{'s' if len(mine) != 1 else ''} from you**"
             + (f", mostly about {', '.join(tops)}." if tops else ".")]
    step = max(1, len(mine) // 8)
    for t, c in mine[::step][:8]:
        lines.append(f"- {datetime.fromtimestamp(c):%a %H:%M} · {_short(t)}")
    if mems:
        lines.append("\n**What I noted then:**")
        lines += [f"- {_short(x.text, 120)}" for x in mems[:5]]
    lines.append("\n_Ask \"what did I say about <topic> " + label + "?\" for the details._")
    return "\n".join(lines)
