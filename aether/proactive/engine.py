"""Proactive engine: AETHER speaks first, but politely.

Triggers are deterministic code (no model needed): due reminders, a morning
brief, calendar events about to start, an evening review, low battery,
finished background goals, waiting improvement proposals, a gentle
check-in after repeated stress signals, a break after a long stretch of
focus and early warnings of deadlines (see awareness.py). Every notification is deduplicated,
held during quiet hours, rate limited per hour, and never dropped: anything
held is delivered later. The UI polls /api/notifications; the CLI prints them
before each prompt.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta

from ..core.config import ProactiveConfig
from ..core.logging import log

SCHEMA = """CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY, created REAL NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
    key TEXT UNIQUE NOT NULL, delivered REAL, dismissed INTEGER DEFAULT 0)"""


@dataclass
class Notification:
    id: int
    kind: str
    title: str
    body: str
    created: float


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


class ProactiveEngine:
    def __init__(self, aether, cfg: ProactiveConfig):
        self.a, self.cfg = aether, cfg
        self.db, self.lock = aether.db, aether.lock
        with self.lock:
            self.db.execute(SCHEMA)
            self.db.commit()
        # goals that finished before this engine existed are not news
        for g in aether.graph.goals(200):
            if g["status"] in {"done", "failed", "cancelled"}:
                self._queue("goal", "", "", f"goal:{g['id']}:{g['status']}", delivered=True)
        self._stop = threading.Event()
        self.battery_probe = _battery  # swappable in tests
        from ..metacognition.learning import idle_seconds
        self.idle_probe = idle_seconds   # seconds since the last keyboard or mouse input (None where unknown)
        self.listeners: list = []  # called with each ringing timer, e.g. the voice loop says it out loud
        from .awareness import Awareness
        from .bandit import NudgePolicy
        self.awareness = Awareness(aether, cfg)
        self.policy = NudgePolicy(self.db, self.lock)

    # ---------------------------------------------------------- policy
    def quiet(self, now: datetime) -> bool:
        start, end, t = _hm(self.cfg.quiet_start), _hm(self.cfg.quiet_end), now.time()
        return (start <= t or t < end) if start > end else (start <= t < end)

    def _delivered_last_hour(self, now: datetime) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM notifications WHERE delivered>=? AND kind NOT IN ('silent', 'timer')",
                                   (now.timestamp() - 3600,)).fetchone()[0]

    def _queue(self, kind: str, title: str, body: str, key: str, delivered: bool = False) -> bool:
        with self.lock:
            cur = self.db.execute("INSERT OR IGNORE INTO notifications(created, kind, title, body, key, delivered)"
                                  " VALUES (?,?,?,?,?,?)", (time.time(), "silent" if delivered else kind, title, body,
                                                            key, time.time() if delivered else None))
            self.db.commit()
            bus = getattr(self.a, "bus", None)
            if bus is not None and cur.rowcount and not delivered:
                bus.publish("notify", {"kind": kind, "title": title[:120]})
        return cur.rowcount > 0

    # ---------------------------------------------------------- triggers
    def tick(self, now: datetime | None = None) -> int:
        """Run every trigger once. Returns how many new notifications were queued."""
        now = now or datetime.now()
        if not self.cfg.enabled:
            return 0
        n = self.ring(now)
        day = now.date().isoformat()
        for r in self.a.reminders.due(now.date()):
            if r.at:   # a reminder with a time rings like an alarm, at that time, on the day it is due
                if r.due < day or now.strftime("%H:%M") >= r.at:
                    n += self._ring_reminder(r, day)
                continue
            n += self._queue("reminder", "Reminder", f"{r.text}" + (f" ({r.when_text()})" if r.repeat else f" (due {r.due})"),
                             f"reminder:{r.id}:{day}")
        if self.cfg.morning_brief and now.time() >= _hm(self.cfg.morning_brief):
            key = f"brief:{day}"
            if not self._exists(key):  # build it (with weather and news) only once a day
                n += self._queue("brief", "Morning brief", self.full_brief(now), key)
        n += self._calendar_triggers(now, day)
        n += self._battery_trigger(now, day)
        n += self._mail_trigger(now)
        n += self._learning_triggers(now)
        for g in self.a.graph.goals(50):
            if g["status"] in {"done", "failed", "cancelled"}:
                n += self._queue("goal", f"Goal {g['status']}", g["text"][:200], f"goal:{g['id']}:{g['status']}")
        waiting = [p for p in self.a.meta.proposals("proposed")]
        if waiting:
            n += self._queue("proposal", "Ideas to improve myself",
                             f"{len(waiting)} proposal(s) need your OK: " + "; ".join(p.title for p in waiting[:3]),
                             f"proposals:{day}")
        n += self._awareness_triggers(now)
        if self.cfg.wellness_checkins and self._stressed_recently(now):
            n += self._queue("checkin", "Checking in",
                             "You've sounded stressed a few times today. Want to take five minutes, or talk it through?",
                             f"checkin:{day}")
        return n

    def _awareness_triggers(self, now: datetime) -> int:
        n = 0
        try:
            nudge = self.awareness.focus_nudge(now)
            if nudge:
                n += self._queue("break", *nudge)
            for title, body, key in self.awareness.deadline_nudges(now):
                n += self._queue("deadline", title, body, key)
            n += self._rest_triggers(now)
            n += self._watch_triggers(now)
            n += self._review_trigger(now)
        except Exception:
            log.exception("awareness triggers failed")
        return n

    def _watch_triggers(self, now: datetime) -> int:
        digest = getattr(self.a, "digest", None)
        if digest is None:
            return 0
        n = 0
        for query, new in digest.check_watches(getattr(self.a.cfg.online, "watch_every_hours", 4)):
            body = "; ".join(h.title for h in new[:3]) + (f" (+{len(new) - 3} more)" if len(new) > 3 else "")
            n += self._queue("news", f"New on {query[:60]}", body, f"watch:{query}:{new[0].title[:40]}")
        return n

    def _review_trigger(self, now: datetime) -> int:
        """Sunday evening: offer the week in review (a learned nudge, so it fades if you never open it)."""
        if not getattr(self.cfg, "weekly_review", True) or now.weekday() != 6 or not 18 <= now.hour < 22:
            return 0
        try:
            from .review import weekly
            body = weekly(self.a, spoken=True, now=now)
        except Exception:
            return 0
        return self._queue("review", "Your week in review", body[:400], f"review:{now.date().isoformat()}")

    def _rest_triggers(self, now: datetime) -> int:
        """Rest days for someone who tends to overwork: a morning suggestion when training, sleep or late nights say
        so, and an evening "call it a day" after a very long day at the screen."""
        n, day = 0, now.date().isoformat()
        focus = self.awareness.focus
        grind = getattr(self.a, "grind", None)
        if grind is not None and getattr(self.cfg, "rest_days", True) and 7 <= now.hour < 12:
            advice = grind.rest_advice(day, late_nights=focus.late_nights(now.date()))
            if advice:
                n += self._queue("rest", "Rest day?", advice, f"rest:{day}")
        hours = focus.hours_on(day)
        limit = getattr(self.cfg, "overwork_hours", 10)
        if limit and hours >= limit and now.hour >= 17:
            n += self._queue("rest", "Call it a day?",
                             f"You've been at the keyboard about {hours:.0f} hours today. Stop at a clean point, write "
                             "down the next step so tomorrow starts fast, and get some sleep. Overworking tonight costs "
                             "tomorrow's focus.", f"overwork:{day}")
        return n

    def _calendar_triggers(self, now: datetime, day: str) -> int:
        n = 0
        cal = getattr(self.a, "calendar", None)
        if cal is None:
            return 0
        try:
            if self.cfg.event_alert_minutes > 0:
                for e in cal.upcoming(now, self.cfg.event_alert_minutes):
                    mins = max(1, round((e.start - now).total_seconds() / 60))
                    where = f" ({e.location})" if e.location else ""
                    n += self._queue("event", f"In {mins} min: {e.title}"[:120], f"{e.when()}{where}",
                                     f"event:{e.uid or e.title}:{e.start.isoformat()}")
            if self.cfg.evening_review and now.time() >= _hm(self.cfg.evening_review):
                n += self._queue("review", "Evening review", self.evening(now), f"review:{day}")
        except Exception:
            log.exception("calendar triggers failed")
        return n

    def evening(self, now: datetime) -> str:
        lines = []
        due = self.a.reminders.due(now.date())
        if due:
            lines.append("Still open from today: " + "; ".join(r.text for r in due[:6]) + ".")
        tomorrow = now.date() + timedelta(days=1)
        cal = getattr(self.a, "calendar", None)
        events = [e for e in (cal.day(tomorrow) if cal else []) if not e.all_day]
        if events:
            first = events[0]
            lines.append(f"Tomorrow starts with {first.title} at {first.start:%H:%M}"
                         + (f", then {len(events) - 1} more." if len(events) > 1 else "."))
        else:
            lines.append("Nothing on the calendar tomorrow morning.")
        if not due:
            lines.insert(0, "Everything due today is done. Nicely handled.")
        return "\n".join(lines)

    def _mail_trigger(self, now: datetime) -> int:
        """New mail from someone on the VIP list. Only the sender and subject are shown, never instructions from it."""
        box, vip = getattr(self.a, "mail", None), set(self.a.cfg.mail.vip)
        if box is None or not vip:
            return 0
        from ..memory.mail import is_vip
        from ..security import injection
        n = 0
        try:
            for m in box.all(now - timedelta(hours=12)):
                if is_vip(m, vip) and m.date <= now:
                    subject = "(subject hidden)" if injection.scan(m.subject) else m.subject
                    n += self._queue("mail", f"Email from {m.sender}"[:120], subject[:200] or "(no subject)", f"mail:{m.id}")
        except Exception:
            log.exception("mail trigger failed")
        return n

    def _battery_trigger(self, now: datetime, day: str) -> int:
        if self.cfg.low_battery <= 0:
            return 0
        b = self.battery_probe()
        if not b or b[1] or b[0] > self.cfg.low_battery:
            return 0
        pct = int(b[0])
        step = "critical" if pct <= max(5, self.cfg.low_battery // 2) else "low"  # one warning per level per day
        return self._queue("battery", f"Battery {pct}%", "You're on battery. Plug in soon, or I'll keep the "
                           "heavy work light." if step == "low" else "Battery is nearly empty. Save your work and plug in.",
                           f"battery:{day}:{step}")

    def _learning_triggers(self, now: datetime) -> int:
        """Start the overnight learning pass when the PC is idle and plugged in; offer the weekly summary on Sunday."""
        learner = getattr(self.a, "learner", None)
        if learner is None:
            return 0
        from ..metacognition.learning import next_report_due
        b = self.battery_probe()
        ok, why = learner.should_run(now, self.idle_probe(), None if not b else bool(b[1]))
        if ok:
            log.info("starting overnight learning (%s)", why)
            threading.Thread(target=learner.run, args=("idle: " + why,), daemon=True, name="aether-learning").start()
        n = 0
        if next_report_due(now):
            key = f"learned:{now.isocalendar()[0]}-{now.isocalendar()[1]}"
            if not self._exists(key):
                r = learner.report(now=now.timestamp())
                if r["facts_learned"] or r["corrections"] or r["preferences"]:   # a quiet week is not news
                    n += self._queue("learning", "What I learned this week", r["text"], key)
        return n

    def ring(self, now: datetime | None = None) -> int:
        """Timers and alarms that are due. They ignore quiet hours and the hourly limit: you set them."""
        timers = getattr(self.a, "timers", None)
        if timers is None:
            return 0
        n = 0
        for t in timers.due((now or datetime.now()).timestamp()):
            timers.finish(t.id)
            title = ("⏰ " + (t.label[:1].upper() + t.label[1:] if t.label else "Alarm")) if t.kind == "alarm" else \
                f"⏱ Timer done{': ' + t.label if t.label else ''}"
            body = f"It's {datetime.fromtimestamp(t.due):%H:%M}." if t.kind == "alarm" else "Time's up."
            n += self._queue("timer", title, body, f"timer:{t.id}")
            for fn in list(self.listeners):
                try:
                    fn(t, title)
                except Exception:
                    log.exception("timer listener failed")
        return n

    def _ring_reminder(self, r, day: str) -> int:
        key = f"reminder-at:{r.id}:{day}"
        if not self._queue("timer", "⏰ " + r.text[:1].upper() + r.text[1:], f"It's {r.at}." + (
                f" ({r.when_text()})" if r.repeat else ""), key):
            return 0
        import types
        alarm = types.SimpleNamespace(id=r.id, label=r.text, due=time.time(), kind="alarm")
        for fn in list(self.listeners):
            try:
                fn(alarm, r.text)
            except Exception:
                log.exception("reminder listener failed")
        return 1

    def _exists(self, key: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM notifications WHERE key=?", (key,)).fetchone() is not None

    def full_brief(self, now: datetime) -> str:
        """The full briefing (weather, news) when AETHER has it, else the offline brief."""
        try:
            return self.a.briefing(now).written if hasattr(self.a, "briefing") else self.brief(now)
        except Exception:
            log.exception("briefing failed; using the offline brief")
            return self.brief(now)

    def _stressed_recently(self, now: datetime) -> bool:
        with self.lock:
            k = self.db.execute("SELECT COUNT(*) FROM memories WHERE layer='observation' AND deleted=0 AND created>=? "
                                "AND json_extract(meta,'$.kind')='mood'", (now.timestamp() - 86400,)).fetchone()[0]
        return k >= 3

    def brief(self, now: datetime) -> str:
        part = "morning" if now.hour < 12 else "afternoon" if now.hour < 17 else "evening"
        lines = [f"Good {part}. It's {now:%A %d %B}."]
        due = self.a.reminders.due(now.date())
        if due:
            lines.append("Due today or overdue: " + "; ".join(r.text for r in due[:6]))
        upcoming = [r for r in self.a.reminders.open() if r not in due][:3]
        if upcoming:
            lines.append("Coming up: " + "; ".join(f"{r.text} ({r.due})" for r in upcoming))
        cal = getattr(self.a, "calendar", None)
        try:
            events = cal.day(now.date()) if cal else []
        except Exception:
            events = []
        if events:
            lines.append("On the calendar: " + "; ".join(f"{e.when()} {e.title}" for e in events[:6]))
        box = getattr(self.a, "mail", None)
        try:
            recent = box.all(now - timedelta(days=1)) if box else []
        except Exception:
            recent = []
        if recent:
            from ..memory.mail import is_vip
            vip = [m.sender for m in recent if is_vip(m, set(self.a.cfg.mail.vip))]
            lines.append(f"Email: {len(recent)} in the last day" + (f", including {', '.join(dict.fromkeys(vip))}." if vip else "."))
        running = [g for g in self.a.graph.goals(20) if g["status"] in {"running", "pending"}]
        if running:
            lines.append("Still working on: " + "; ".join(g["text"][:60] for g in running))
        if len(lines) == 1:
            lines.append("Nothing scheduled. A clear day.")
        return "\n".join(lines)

    # ---------------------------------------------------------- delivery
    def deliver(self, now: datetime | None = None) -> list[Notification]:
        """Hand out what may be shown now (quiet hours and rate limit applied). Marks them delivered."""
        now = now or datetime.now()
        with self.lock:  # "in 15 minutes" is useless an hour later: stale event alerts expire unseen
            self.db.execute("UPDATE notifications SET dismissed=1 WHERE kind='event' AND delivered IS NULL AND created<?",
                            (now.timestamp() - max(1, self.cfg.event_alert_minutes) * 60,))
            # a break suggestion held through quiet hours or the hourly limit is stale after 20 minutes
            self.db.execute("UPDATE notifications SET dismissed=1 WHERE kind='break' AND delivered IS NULL AND created<?",
                            (now.timestamp() - 1200,))
            self.db.commit()
        with self.lock:  # timers always ring, quiet hours or not
            urgent = self.db.execute("SELECT id, kind, title, body, created FROM notifications WHERE delivered IS NULL "
                                     "AND dismissed=0 AND kind='timer' ORDER BY id").fetchall()
            for r in urgent:
                self.db.execute("UPDATE notifications SET delivered=? WHERE id=?", (now.timestamp(), r[0]))
            self.db.commit()
        urgent = [Notification(*r) for r in urgent]
        if self.quiet(now):
            return urgent
        room = self.cfg.max_per_hour - self._delivered_last_hour(now)
        if room <= 0:
            return urgent
        order = ("CASE kind WHEN 'event' THEN 0 WHEN 'battery' THEN 0 WHEN 'reminder' THEN 1 WHEN 'deadline' THEN 1 "
                 "WHEN 'checkin' THEN 2 WHEN 'break' THEN 2 "
                 "WHEN 'goal' THEN 3 ELSE 4 END")
        self._expire_ignored(now)
        with self.lock:
            cand = self.db.execute(f"SELECT id, kind, title, body, created FROM notifications WHERE delivered IS NULL "
                                   f"AND dismissed=0 AND kind!='timer' ORDER BY {order}, id LIMIT ?", (room * 3,)).fetchall()
            rows = []
            for r in cand:
                if len(rows) >= room:
                    break
                if not self.policy.allow(r[1], now):   # a kind you keep dismissing at this time of day: held back
                    self.db.execute("UPDATE notifications SET dismissed=2 WHERE id=?", (r[0],))
                    continue
                rows.append(r)
                self.db.execute("UPDATE notifications SET delivered=? WHERE id=?", (now.timestamp(), r[0]))
            self.db.commit()
        return urgent + [Notification(*r) for r in rows]

    def _expire_ignored(self, now: datetime) -> None:
        rows = self.db.execute("SELECT id, kind, delivered FROM notifications WHERE delivered IS NOT NULL AND dismissed=0 "
                               "AND delivered<? AND delivered>?", (now.timestamp() - 12 * 3600, now.timestamp() - 3 * 86400)).fetchall()
        for nid, kind, delivered in rows:
            self.policy.ignored(nid, kind, delivered)

    def acted(self, nid: int) -> None:
        """You clicked a nudge or did what it suggested: more like this, at this time of day."""
        r = self.db.execute("SELECT kind, delivered FROM notifications WHERE id=?", (nid,)).fetchone()
        if r and r[1]:
            self.policy.acted(nid, r[0], r[1])
        with self.lock:
            self.db.execute("UPDATE notifications SET dismissed=3 WHERE id=?", (nid,))
            self.db.commit()

    def alarms_since(self, after: float) -> list[dict]:
        """Timers, alarms and timed reminders that went off after `after` (for the voice listener to say aloud).
        Read-only: the tray and the app still get them as notifications."""
        with self.lock:
            rows = self.db.execute("SELECT id, created, title, body FROM notifications WHERE kind='timer' AND created>? "
                                   "ORDER BY id", (after,)).fetchall()
        return [{"id": r[0], "created": r[1], "title": r[2], "body": r[3]} for r in rows]

    def held(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM notifications WHERE delivered IS NULL AND dismissed=0").fetchone()[0]

    def dismiss(self, nid: int) -> None:
        r = self.db.execute("SELECT kind, delivered FROM notifications WHERE id=?", (nid,)).fetchone()
        if r and r[1]:
            self.policy.dismissed(nid, r[0], r[1])
        with self.lock:
            self.db.execute("UPDATE notifications SET dismissed=1 WHERE id=?", (nid,))
            self.db.commit()

    # ---------------------------------------------------------- background loop
    def start(self) -> threading.Thread:
        def loop():
            while not self._stop.wait(self.cfg.check_interval_s):
                try:
                    self.tick()
                except Exception:
                    log.exception("proactive tick failed")
        def timer_loop():  # timers need to ring on the second, not on the next minute's tick
            while not self._stop.wait(1.0):
                try:
                    self.ring()
                except Exception:
                    log.exception("timer check failed")
        t = threading.Thread(target=loop, daemon=True, name="aether-proactive")
        t.start()
        threading.Thread(target=timer_loop, daemon=True, name="aether-timers").start()
        return t

    def stop(self) -> None:
        self._stop.set()


def _battery() -> tuple[float, bool] | None:
    """(percent, plugged in) from psutil, or None on a desktop / without psutil."""
    try:
        import psutil
        b = psutil.sensors_battery()
        return (b.percent, bool(b.power_plugged)) if b else None
    except Exception:
        return None
