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
        self.awareness = Awareness(aether, cfg)

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
            n += self._queue("reminder", "Reminder", f"{r.text} (due {r.due})", f"reminder:{r.id}:{day}")
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
        except Exception:
            log.exception("awareness triggers failed")
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
        with self.lock:
            rows = self.db.execute(f"SELECT id, kind, title, body, created FROM notifications WHERE delivered IS NULL "
                                   f"AND dismissed=0 AND kind!='timer' ORDER BY {order}, id LIMIT ?", (room,)).fetchall()
            for r in rows:
                self.db.execute("UPDATE notifications SET delivered=? WHERE id=?", (now.timestamp(), r[0]))
            self.db.commit()
        return urgent + [Notification(*r) for r in rows]

    def held(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM notifications WHERE delivered IS NULL AND dismissed=0").fetchone()[0]

    def dismiss(self, nid: int) -> None:
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
