"""Local calendar: read .ics files (exported from Outlook, Google Calendar, Apple
Calendar, a university timetable...) from folders on this PC, plus, optionally,
the classic Outlook desktop app on Windows through COM. Nothing goes online.

The parser covers what real exports contain: folded lines, UTC / floating /
TZID / all-day times, and recurring events (RRULE with FREQ, INTERVAL, COUNT,
UNTIL, BYDAY, BYMONTHDAY, plus EXDATE).
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from ..core.logging import log

DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


@dataclass
class Event:
    title: str
    start: datetime  # naive local time
    end: datetime
    all_day: bool = False
    location: str = ""
    uid: str = ""
    source: str = ""

    def as_dict(self) -> dict:
        return {"title": self.title, "start": self.start.isoformat(timespec="minutes"),
                "end": self.end.isoformat(timespec="minutes"), "all_day": self.all_day,
                "location": self.location, "uid": self.uid, "source": self.source}

    def when(self) -> str:
        if self.all_day:
            return "all day"
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


@dataclass
class _Raw:
    props: dict = field(default_factory=dict)  # name -> (params, value); last wins
    exdates: list = field(default_factory=list)


def _unfold(text: str) -> list[str]:
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line:
            out.append(line)
    return out


def _split(line: str) -> tuple[str, dict, str]:
    head, _, value = line.partition(":")
    name, *params = head.split(";")
    p = {}
    for x in params:
        k, _, v = x.partition("=")
        p[k.upper()] = v.strip('"')
    return name.upper(), p, value


def _unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _tz(name: str | None):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None  # Windows zone names or a missing tz database: treat as local time


def _dt(params: dict, value: str) -> tuple[datetime, bool]:
    """Returns (naive local datetime, all_day)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d"), True
    utc = value.endswith("Z")
    d = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        return d.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None), False
    tz = _tz(params.get("TZID"))
    if tz is not None:
        return d.replace(tzinfo=tz).astimezone().replace(tzinfo=None), False
    return d, False


def _duration(v: str) -> timedelta:
    m = re.fullmatch(r"(-)?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v.strip())
    if not m:
        return timedelta(hours=1)
    sign, w, d, h, mi, s = m.groups()
    td = timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0), seconds=int(s or 0))
    return -td if sign else td


def parse_ics(text: str, source: str = "") -> tuple[list[Event], list[tuple[Event, str, list[datetime]]]]:
    """Returns (single events, recurring masters as (event, RRULE, exdates))."""
    singles, recurring, moved = [], [], []
    raw: _Raw | None = None
    depth = 0
    for line in _unfold(text):
        name, params, value = _split(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            raw, depth = _Raw(), 0
            continue
        if raw is None:
            continue
        if name == "BEGIN":  # nested VALARM etc.
            depth += 1
            continue
        if name == "END" and depth:
            depth -= 1
            continue
        if depth:
            continue
        if name == "END" and value.upper() == "VEVENT":
            ev = _build(raw, source)
            cancelled = raw.props.get("STATUS", ({}, ""))[1].upper() == "CANCELLED"
            if ev and "RECURRENCE-ID" in raw.props:  # one changed (or cancelled) occurrence of a series
                try:
                    moved.append((ev.uid, _dt(*raw.props["RECURRENCE-ID"])[0]))
                except ValueError:
                    pass
            if ev:
                rrule = raw.props.get("RRULE", (None, ""))[1]
                if rrule and not cancelled:
                    recurring.append((ev, rrule, raw.exdates))
                elif not rrule and not cancelled:
                    singles.append(ev)
            raw = None
            continue
        if name == "EXDATE":
            for v in value.split(","):
                try:
                    raw.exdates.append(_dt(params, v)[0])
                except ValueError:
                    pass
        else:
            raw.props[name] = (params, value)
    for ev, _, exdates in recurring:
        exdates += [when for uid, when in moved if uid == ev.uid]
    return singles, recurring


def _build(raw: _Raw, source: str) -> Event | None:
    if "DTSTART" not in raw.props:
        return None
    try:
        start, all_day = _dt(*raw.props["DTSTART"])
        if "DTEND" in raw.props:
            end = _dt(*raw.props["DTEND"])[0]
        elif "DURATION" in raw.props:
            end = start + _duration(raw.props["DURATION"][1])
        else:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    except ValueError:
        return None
    return Event(_unescape(raw.props.get("SUMMARY", ({}, "(no title)"))[1]).strip() or "(no title)", start, end, all_day,
                 _unescape(raw.props.get("LOCATION", ({}, ""))[1]).strip(), raw.props.get("UID", ({}, ""))[1], source)


def expand(ev: Event, rrule: str, exdates: list[datetime], window_start: datetime, window_end: datetime,
           limit: int = 1000) -> list[Event]:
    """Occurrences of a recurring event that overlap the window."""
    r = dict(x.split("=", 1) for x in rrule.upper().split(";") if "=" in x)
    freq, interval = r.get("FREQ", "DAILY"), max(1, int(r.get("INTERVAL", "1")))
    count = int(r["COUNT"]) if "COUNT" in r else None
    until = None
    if "UNTIL" in r:
        try:
            until = _dt({}, r["UNTIL"])[0]
            if len(r["UNTIL"]) == 8:
                until += timedelta(days=1)  # date-only UNTIL is inclusive
        except ValueError:
            pass
    byday = [d[-2:] for d in r.get("BYDAY", "").split(",") if d[-2:] in DAYS]
    bymonthday = [int(x) for x in r.get("BYMONTHDAY", "").split(",") if x.strip().lstrip("-").isdigit()]
    length, ex = ev.end - ev.start, set(exdates)
    out, n, k = [], 0, 0

    def candidates(period: int):
        base = ev.start
        if freq == "DAILY":
            yield base + timedelta(days=period * interval)
        elif freq == "WEEKLY":
            week0 = base - timedelta(days=base.weekday()) + timedelta(weeks=period * interval)
            for d in sorted(DAYS.index(x) for x in (byday or [DAYS[base.weekday()]])):
                yield datetime.combine((week0 + timedelta(days=d)).date(), base.time())
        elif freq == "MONTHLY":
            y, m = divmod(base.month - 1 + period * interval, 12)
            for day in (bymonthday or [base.day]):
                try:
                    yield base.replace(year=base.year + y, month=m + 1, day=day)
                except ValueError:
                    pass  # e.g. the 31st in a 30-day month
        elif freq == "YEARLY":
            try:
                yield base.replace(year=base.year + period * interval)
            except ValueError:
                pass

    while k < 5000:
        produced = list(candidates(k))
        k += 1
        if produced and min(produced) > window_end:
            break
        for start in produced:
            if start < ev.start:
                continue
            if until and start >= until:
                return out
            n += 1
            if count is not None and n > count:
                return out
            if start in ex:
                continue
            if start < window_end and start + length > window_start:
                out.append(Event(ev.title, start, start + length, ev.all_day, ev.location, ev.uid, ev.source))
                if len(out) >= limit:
                    return out
    return out


TIME = re.compile(r"\b(?:at\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\b|\b(noon|midday|midnight)\b", re.I)


def parse_when(text: str, now: datetime) -> datetime | None:
    """'friday 3pm', 'tomorrow at 14:30', '2026-12-01 09:00', 'today noon' -> a local datetime (None if no time)."""
    from ..neurosymbolic.temporal import resolve
    t = text.strip().lower()
    hour = minute = None
    for m in TIME.finditer(t):
        if m.group(4):
            hour, minute = {"noon": 12, "midday": 12, "midnight": 0}[m.group(4)], 0
        elif m.group(2) or m.group(3) or "at " in m.group(0):
            h = int(m.group(1))
            if m.group(3):
                h = h % 12 + (12 if m.group(3) == "pm" else 0)
            if h > 23 or int(m.group(2) or 0) > 59:
                continue
            hour, minute = h, int(m.group(2) or 0)
        else:
            continue
        t = (t[:m.start()] + " " + t[m.end():]).strip()
        break
    if hour is None:
        return None
    t = re.sub(r"\s+", " ", re.sub(r"\b(?:on|at)\b", " ", t)).strip()
    d = resolve(t, now.date()) if t else now.date()
    if d is None:
        return None
    return datetime.combine(d, time(hour, minute))


def _ics_text(v: str) -> str:
    return v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


class Calendar:
    def __init__(self, paths: list[str], outlook: bool = False):
        self.paths = [Path(p).expanduser() for p in paths]
        self.outlook = outlook
        self._cache: dict[Path, tuple[float, tuple]] = {}
        self._lock = threading.Lock()

    def files(self) -> list[Path]:
        out = []
        for p in self.paths:
            if p.is_file() and p.suffix.lower() == ".ics":
                out.append(p)
            elif p.is_dir():
                out += sorted(p.glob("*.ics"))
        return out

    def _load(self, f: Path) -> tuple:
        mtime = f.stat().st_mtime
        with self._lock:
            hit = self._cache.get(f)
            if hit and hit[0] == mtime:
                return hit[1]
        try:
            parsed = parse_ics(f.read_text(encoding="utf-8", errors="replace"), f.name)
        except Exception as e:  # a broken file must not break the brief
            log.warning("calendar %s unreadable: %s", f, e)
            parsed = ([], [])
        with self._lock:
            self._cache[f] = (mtime, parsed)
        return parsed

    def events(self, start: datetime, end: datetime) -> list[Event]:
        out: list[Event] = []
        for f in self.files():
            singles, recurring = self._load(f)
            out += [e for e in singles if e.start < end and e.end > start]
            for ev, rule, ex in recurring:
                out += expand(ev, rule, ex, start, end)
        if self.outlook:
            out += self._outlook(start, end)
        seen, uniq = set(), []
        for e in sorted(out, key=lambda e: (e.start, e.title)):
            key = (e.title.lower(), e.start)
            if key not in seen:
                seen.add(key)
                uniq.append(e)
        return uniq

    def day(self, d: date) -> list[Event]:
        return self.events(datetime.combine(d, time()), datetime.combine(d + timedelta(days=1), time()))

    def upcoming(self, now: datetime, minutes: int) -> list[Event]:
        return [e for e in self.events(now, now + timedelta(minutes=minutes)) if not e.all_day and e.start >= now]

    def free_slots(self, d: date, day_start: str = "09:00", day_end: str = "21:00", min_minutes: int = 30) -> list[tuple[datetime, datetime]]:
        h1, m1 = map(int, day_start.split(":"))
        h2, m2 = map(int, day_end.split(":"))
        cur, stop = datetime.combine(d, time(h1, m1)), datetime.combine(d, time(h2, m2))
        slots = []
        for e in self.day(d):
            if e.all_day:
                continue
            if e.start > cur and (e.start - cur) >= timedelta(minutes=min_minutes):
                slots.append((cur, min(e.start, stop)))
            cur = max(cur, e.end)
            if cur >= stop:
                break
        if stop - cur >= timedelta(minutes=min_minutes):
            slots.append((cur, stop))
        return [s for s in slots if s[1] > s[0]]

    def own_file(self) -> Path:
        """AETHER writes the events you ask it to add to its own file, never into your exports."""
        folder = next((p for p in self.paths if not p.suffix), self.paths[0].parent if self.paths else Path.home())
        return folder / "aether.ics"

    def add(self, title: str, start: datetime, end: datetime, location: str = "") -> Event:
        import uuid
        f = self.own_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        uid = f"{uuid.uuid4()}@aether.local"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        body = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}", f"DTSTART:{start:%Y%m%dT%H%M%S}",
                f"DTEND:{end:%Y%m%dT%H%M%S}", f"SUMMARY:{_ics_text(title)}"]
        if location:
            body.append(f"LOCATION:{_ics_text(location)}")
        body.append("END:VEVENT")
        with self._lock:
            old = f.read_text(encoding="utf-8") if f.exists() else ""
            if "END:VCALENDAR" in old:
                text = old[:old.rindex("END:VCALENDAR")] + "\r\n".join(body) + "\r\nEND:VCALENDAR\r\n"
            else:
                text = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AETHER//local//EN", *body,
                                     "END:VCALENDAR"]) + "\r\n"
            tmp = f.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(f)
        return Event(title, start, end, False, location, uid, f.name)

    def clash(self, start: datetime, end: datetime) -> list[Event]:
        return [e for e in self.events(start, end) if not e.all_day]

    def _outlook(self, start: datetime, end: datetime) -> list[Event]:  # pragma: no cover - Windows + Outlook only
        """Classic Outlook desktop through COM (pywin32). Read-only, local MAPI store; nothing leaves the PC."""
        try:
            import win32com.client
            items = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI").GetDefaultFolder(9).Items
            items.IncludeRecurrences = True
            items.Sort("[Start]")
            fmt = "%m/%d/%Y %I:%M %p"
            found = items.Restrict(f"[Start] < '{end.strftime(fmt)}' AND [End] > '{start.strftime(fmt)}'")
            out = []
            for it in found:
                s = datetime.fromtimestamp(it.Start.timestamp())
                e = datetime.fromtimestamp(it.End.timestamp())
                out.append(Event(str(it.Subject), s, e, bool(it.AllDayEvent), str(it.Location or ""), "", "Outlook"))
                if len(out) > 500:
                    break
            return out
        except Exception as e:
            log.warning("Outlook calendar unavailable: %s", e)
            return []


def describe(events: list[Event], free: list[tuple[datetime, datetime]] | None = None, label: str = "") -> str:
    if not events:
        lines = [f"Nothing on the calendar{(' ' + label) if label else ''}."]
    else:
        lines = [f"{label.capitalize() or 'Calendar'}:" if label else "Calendar:"]
        lines += [f"- {e.start:%a %d %b} {e.when()}: {e.title}" + (f" ({e.location})" if e.location else "") for e in events]
    if free:
        lines.append("Free: " + ", ".join(f"{a:%H:%M}-{b:%H:%M}" for a, b in free))
    return "\n".join(lines)


def resolve_range(when: str, now: datetime) -> tuple[datetime, datetime, str]:
    w = (when or "today").strip().lower()
    today = datetime.combine(now.date(), time())
    if w in {"today", ""}:
        return today, today + timedelta(days=1), "today"
    if w == "tomorrow":
        return today + timedelta(days=1), today + timedelta(days=2), "tomorrow"
    if w in {"this week", "week"}:
        return today, today + timedelta(days=7 - today.weekday()), "this week"
    if w in {"next week"}:
        s = today + timedelta(days=7 - today.weekday())
        return s, s + timedelta(days=7), "next week"
    names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    day = re.sub(r"^(?:on |this |next )", "", w)
    for i, n in enumerate(names):
        if len(day) >= 3 and n.startswith(day):
            delta = (i - today.weekday()) % 7 or (7 if w.startswith("next ") else 0)
            s = today + timedelta(days=delta)
            return s, s + timedelta(days=1), s.strftime("%A")
    try:
        d = datetime.strptime(w[:10], "%Y-%m-%d")
        return d, d + timedelta(days=1), d.strftime("%A %d %B")
    except ValueError:
        return today, today + timedelta(days=7), "the next 7 days"
