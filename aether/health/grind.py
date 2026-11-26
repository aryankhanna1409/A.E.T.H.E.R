"""Grind 75 inside AETHER: a 75-day challenge tracker with food, protein, body, reading, sleep, water and money.

The data is the same JSON document the Grind 75 desktop app keeps (``grind75-data.json``), so the web page at
``/grind/`` is the Grind 75 interface itself and an existing Grind 75 history carries straight over: on first run
AETHER copies ``%APPDATA%\\Grind 75\\grind75-data.json`` if it exists.

This module is the Python half: AETHER's voice and chat use it to log things in plain words ("I had 3 roti and a
katori of dal", "weight 63.2", "slept 7 hours", "drank half a litre", "workout done, chest day"), to answer "how's my
75 Hard going?", and to feed the morning briefing, nudges and rest-day advice. The rules match the Grind 75 app's
own (habit math, streaks with the 70% good-day line, the weight trend fit, the offline food table).
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

FOODS_FILE = Path(__file__).with_name("foods.json")
GOOD_DAY = 0.7
SPLIT = ["Back + biceps", "Chest + shoulders + triceps", "Quads", "Hamstrings + calves + abs", "Rest / walk"]

DEFAULT_HABITS = [
    {"id": "workout", "label": "Workout done", "emoji": "💪", "auto": False},
    {"id": "protein", "label": "Hit protein", "emoji": "🥛", "auto": True},
    {"id": "kcal", "label": "Hit calories", "emoji": "🍛", "auto": True},
    {"id": "read", "label": "Read pages", "emoji": "📖", "auto": True},
    {"id": "sleep", "label": "Slept 7h+", "emoji": "😴", "auto": True},
    {"id": "water", "label": "Water 3L", "emoji": "💧", "auto": True},
    {"id": "homefood", "label": "No eating out", "emoji": "🏠", "auto": False},
    {"id": "spend", "label": "Logged spending", "emoji": "💸", "auto": False},
    {"id": "journal", "label": "Journaled", "emoji": "✍️", "auto": True},
]


def ymd(d: date | None = None) -> str:
    return (d or date.today()).isoformat()


def parse_ymd(s: str) -> date:
    return date.fromisoformat(s)


def add_days(s: str, n: int) -> str:
    return ymd(parse_ymd(s) + timedelta(days=n))


def diff_days(a: str, b: str) -> int:
    return (parse_ymd(a) - parse_ymd(b)).days


def default_state(name: str = "") -> dict:
    return {
        "version": 1,
        "profile": {"name": name, "heightCm": 170, "startDate": ymd(), "startWeight": 60, "targetWeight": 65},
        "targets": {"kcal": 3000, "protein": 140, "pages": 20, "waterL": 3, "sleepH": 7, "workoutsPerWeek": 5,
                    "weeklySpend": 2000},
        "habits": [dict(h) for h in DEFAULT_HABITS],
        "days": {}, "books": [], "goals": [], "chat": [], "customFoods": [],
        "settings": {"aiProvider": "auto", "wallpaperMode": "mix", "autoDesktop": False, "theme": "pastel"},
        "walls": {},
    }


def merge_defaults(base: dict, saved: dict | None) -> dict:
    if not isinstance(saved, dict):
        return base
    out = {**base, **saved}
    for k in ("profile", "targets", "settings"):
        out[k] = {**base[k], **(saved.get(k) or {})}
    return out


def grind75_app_file() -> Path | None:
    """Where the Grind 75 desktop app keeps its data on Windows (Electron's userData folder)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    for name in ("Grind 75", "grind75"):
        p = Path(appdata) / name / "grind75-data.json"
        if p.exists():
            return p
    return None


def _num(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------------------------- food parsing
class FoodTable:
    def __init__(self, custom: list[dict] | None = None):
        data = json.loads(FOODS_FILE.read_text(encoding="utf-8"))
        self.foods = list(custom or []) + data["foods"]
        self.units = data["units"]
        self.number_words = data["number_words"]

    def match(self, name: str) -> dict | None:
        q = re.sub(r"\s+", " ", re.sub(r"[^a-z\s]", " ", name.lower())).strip()
        if not q:
            return None
        best, best_score = None, 0
        for f in self.foods:
            for alias in [f["n"], *(f.get("a") or [])]:
                a = alias.lower()
                if q == a:
                    score = 100
                elif re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", q):
                    score = 50 + len(a)
                elif a in q and len(a) >= 4:
                    score = 40 + len(a)
                elif q in a and len(q) >= 3:
                    score = 30 + len(q)
                else:
                    common = sum(1 for w in q.split() if len(w) > 2 and w in a.split())
                    score = 10 * common
                if score > best_score:
                    best, best_score = f, score
        return best if best_score >= 10 else None

    def parse(self, text: str) -> tuple[list[dict], list[str]]:
        """'3 roti, 1 katori rajma and 200 ml curd' -> items with kcal/protein/carbs/fat, plus unrecognised parts."""
        parts = [p.strip() for p in re.split(r",|\+|\n|;|\band\b|\bwith\b|\bplus\b", text, flags=re.I) if p.strip()]
        items, unknown = [], []
        for raw in parts:
            s = raw.lower()
            s = re.sub(r"^(?:i\s+(?:had|ate|have\s+had|just\s+had|drank)|had|ate|log(?:ged)?|add)\s+", "", s)
            qty = None
            m = re.search(r"(\d+(?:\.\d+)?)(?:\s*/\s*(\d+))?", s)
            if m:
                qty = float(m.group(1)) / float(m.group(2)) if m.group(2) else float(m.group(1))
                s = s.replace(m.group(0), " ", 1)
            else:
                for w, v in self.number_words.items():
                    if re.search(rf"\b{w}\b", s):
                        qty = v
                        s = re.sub(rf"\b{w}\b", " ", s, count=1)
                        break
            unit = None
            for u in sorted(self.units, key=len, reverse=True):
                if re.search(rf"(^|\s|\d){re.escape(u)}\b", s):
                    unit = self.units[u]
                    s = re.sub(rf"(^|\s|\d){re.escape(u)}\b", " ", s, count=1)
                    break
            s = re.sub(r"\b(of|x|small|medium|large|big|cooked|homemade|some|for|breakfast|lunch|dinner|today)\b",
                       " ", s).strip()
            f = self.match(s)
            if not f:
                unknown.append(raw)
                continue
            if qty is None:
                qty = 100 if f.get("unit") == "g" else 1
            fg = f.get("grams")
            if not unit:
                factor = qty * 100 if f.get("unit") == "g" and qty < 10 else qty  # "paneer 2" ~ 200 g
            elif unit["canon"] == f.get("unit"):
                factor = qty
            elif unit.get("grams") and fg:
                factor = qty * unit["grams"] / fg
            else:
                factor = qty
            items.append({"name": f["n"], "quantity": raw.strip(), "kcal": round(f["kcal"] * factor),
                          "protein": round(f["p"] * factor, 1), "carbs": round(f["c"] * factor, 1),
                          "fat": round(f["f"] * factor, 1)})
        return items, unknown


def guess_meal(hour: int | None = None) -> str:
    h = datetime.now().hour if hour is None else hour
    return ("Pre-workout" if h < 6 else "Post-workout shake" if h < 8 else "Breakfast" if h < 11 else "Lunch" if h < 15
            else "Evening snack" if h < 19 else "Dinner" if h < 22 else "Before bed")


# ---------------------------------------------------------------------------------------------- the tracker
class Grind:
    def __init__(self, home: Path, user_name: str = "", import_from: Path | None = None):
        self.dir = home / "grind"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "grind75-data.json"
        self.lock = threading.RLock()
        self.user_name = user_name
        self.rev = 0
        self.state = self._load(import_from)

    # ------------------------------------------------------------------ storage
    def _load(self, import_from: Path | None) -> dict:
        saved = None
        if self.file.exists():
            try:
                saved = json.loads(self.file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                bad = self.file.with_suffix(f".corrupt-{int(time.time())}")
                try:
                    self.file.replace(bad)
                except OSError:
                    pass
        if saved is None:
            src = import_from if import_from is not None else grind75_app_file()
            if src and Path(src).exists():
                try:
                    saved = json.loads(Path(src).read_text(encoding="utf-8"))
                    saved.setdefault("_imported_from", str(src))
                except (OSError, ValueError):
                    saved = None
        st = merge_defaults(default_state(self.user_name), saved)
        if not st["profile"].get("name"):
            st["profile"]["name"] = self.user_name
        self.rev = int(st.get("_rev", 0))
        if saved is None or "_imported_from" in (saved or {}):
            self._write(st)
        return st

    def _write(self, st: dict) -> None:
        st["_rev"] = self.rev
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.file)
        bdir = self.dir / "backups"
        bdir.mkdir(exist_ok=True)
        b = bdir / f"{ymd()}.json"
        if not b.exists():
            b.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
            for old in sorted(bdir.glob("*.json"))[:-30]:
                old.unlink(missing_ok=True)

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps({**self.state, "_rev": self.rev}))

    def save_from_ui(self, incoming: dict, base_rev: int | None) -> dict:
        """The web page saves its whole document. If AETHER logged something by voice since the page loaded
        (base_rev older than ours), those entries are kept instead of being overwritten."""
        with self.lock:
            if base_rev is not None and base_rev < self.rev:
                for day_key, mine in self.state.get("days", {}).items():
                    theirs = incoming.setdefault("days", {}).setdefault(day_key, mine)
                    if theirs is mine:
                        continue
                    seen = {(f.get("name"), f.get("quantity"), f.get("ts")) for f in theirs.get("food", [])}
                    for f in mine.get("food", []):
                        if f.get("via") == "aether" and (f.get("name"), f.get("quantity"), f.get("ts")) not in seen \
                                and (f.get("rev", 0) > base_rev):
                            theirs.setdefault("food", []).append(f)
                    for field, r in (mine.get("_aether") or {}).items():
                        if r > base_rev:
                            if field.startswith("checks."):
                                theirs.setdefault("checks", {})[field[7:]] = mine.get("checks", {}).get(field[7:])
                            else:
                                theirs[field] = mine.get(field)
            self.rev += 1
            incoming.pop("_rev", None)
            self.state = merge_defaults(default_state(self.user_name), incoming)
            self._write(self.state)
            return {"rev": self.rev}

    def _commit(self) -> None:
        self.rev += 1
        self._write(self.state)

    def day(self, d: str | None = None) -> dict:
        d = d or ymd()
        days = self.state.setdefault("days", {})
        if d not in days:
            days[d] = {"checks": {}, "food": [], "weight": None, "workout": None, "reading": [], "sleepH": None,
                       "waterL": 0, "spend": [], "journal": "", "mood": None}
        return days[d]

    def _mark(self, dd: dict, field: str) -> None:
        dd.setdefault("_aether", {})[field] = self.rev + 1

    # ------------------------------------------------------------------ math (same rules as the app)
    def challenge(self, d: str | None = None) -> dict:
        d = d or ymd()
        n = diff_days(d, self.state["profile"]["startDate"]) + 1
        if n < 1:
            return {"started": False, "daysUntil": 1 - n}
        block = math.ceil(n / 75)
        in_block = (n - 1) % 75 + 1
        return {"started": True, "n": n, "block": block, "dayInBlock": in_block, "left": 75 - in_block,
                "blockEnd": add_days(self.state["profile"]["startDate"], block * 75 - 1)}

    def totals(self, d: str | None = None) -> dict:
        dd = self.state.get("days", {}).get(d or ymd())
        t = {"kcal": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0}
        for f in (dd or {}).get("food", []):
            for k in t:
                t[k] += _num(f.get(k))
        return t

    def pages_on(self, d: str) -> float:
        return sum(_num(r.get("pages")) for r in (self.state.get("days", {}).get(d) or {}).get("reading", []))

    def habit_done(self, h: dict, d: str) -> bool:
        dd = self.state.get("days", {}).get(d)
        if not dd:
            return False
        checks = dd.get("checks") or {}
        if h["id"] in checks and not h.get("auto"):
            return bool(checks[h["id"]])
        T = self.state["targets"]
        hid = h["id"]
        if hid == "protein":
            return self.totals(d)["protein"] >= T["protein"]
        if hid == "kcal":
            return self.totals(d)["kcal"] >= T["kcal"]
        if hid == "read":
            return self.pages_on(d) >= T["pages"]
        if hid == "sleep":
            return _num(dd.get("sleepH")) >= T["sleepH"]
        if hid == "water":
            return _num(dd.get("waterL")) >= T["waterL"]
        if hid == "journal":
            return len((dd.get("journal") or "").strip()) >= 30
        if hid == "workout":
            return bool(checks.get("workout")) or bool((dd.get("workout") or {}).get("done"))
        if hid == "spend":
            return bool(checks.get("spend")) or bool(dd.get("spend"))
        return bool(checks.get(hid))

    def day_score(self, d: str) -> dict:
        hs = self.state["habits"]
        done = sum(1 for h in hs if self.habit_done(h, d))
        return {"done": done, "total": len(hs), "pct": done / len(hs) if hs else 0.0}

    def streak(self, today: str | None = None) -> int:
        d = today or ymd()
        if self.day_score(d)["pct"] < GOOD_DAY:
            d = add_days(d, -1)
        s = 0
        while diff_days(d, self.state["profile"]["startDate"]) >= 0 and self.day_score(d)["pct"] >= GOOD_DAY:
            s += 1
            d = add_days(d, -1)
        return s

    def weights(self) -> list[tuple[str, float]]:
        return sorted((d, _num(x.get("weight"))) for d, x in self.state.get("days", {}).items() if _num(x.get("weight")) > 0)

    def latest_weight(self) -> float:
        w = self.weights()
        return w[-1][1] if w else _num(self.state["profile"].get("startWeight"))

    def weight_rate(self, days: int = 21, today: str | None = None) -> float | None:
        """kg per week from a least-squares fit over the last ``days`` days."""
        since = add_days(today or ymd(), -days)
        pts = [(diff_days(d, since), w) for d, w in self.weights() if d >= since]
        if len(pts) < 3:
            return None
        mx = sum(x for x, _ in pts) / len(pts)
        my = sum(y for _, y in pts) / len(pts)
        den = sum((x - mx) ** 2 for x, _ in pts)
        return (sum((x - mx) * (y - my) for x, y in pts) / den) * 7 if den else None

    def workouts_this_week(self, today: str | None = None) -> int:
        t = today or ymd()
        dow = parse_ymd(t).weekday()
        return sum(1 for i in range(dow + 1) if self.habit_done({"id": "workout"}, add_days(t, -i)))

    def spend_this_week(self, today: str | None = None) -> float:
        t = today or ymd()
        dow = parse_ymd(t).weekday()
        return sum(_num(x.get("amt")) for i in range(dow + 1)
                   for x in (self.state.get("days", {}).get(add_days(t, -i)) or {}).get("spend", []))

    def next_split(self) -> str:
        done = sorted(((d, x["workout"]["type"]) for d, x in self.state.get("days", {}).items()
                       if (x.get("workout") or {}).get("type") in SPLIT), reverse=True)
        return SPLIT[0] if not done else SPLIT[(SPLIT.index(done[0][1]) + 1) % len(SPLIT)]

    # ------------------------------------------------------------------ rest days
    def consecutive_training_days(self, today: str | None = None) -> int:
        d, n = today or ymd(), 0
        if not self.habit_done({"id": "workout"}, d):
            d = add_days(d, -1)
        while self.habit_done({"id": "workout"}, d) and (self.state["days"].get(d, {}).get("workout") or {}).get("type") != "Rest / walk":
            n += 1
            d = add_days(d, -1)
        return n

    def rest_advice(self, today: str | None = None, work_hours: float | None = None, late_nights: int = 0) -> str | None:
        """A rest day suggestion when the signs say so: many training days in a row, short sleep for several
        nights, long screen days, or the rotation's rest slot. Returns None when nothing points to rest."""
        t = today or ymd()
        reasons = []
        streak = self.consecutive_training_days(t)
        if streak >= 6:
            reasons.append(f"you've trained {streak} days in a row")
        short = [d for d in (add_days(t, -i) for i in range(1, 4))
                 if 0 < _num((self.state["days"].get(d) or {}).get("sleepH")) < self.state["targets"]["sleepH"] - 0.5]
        if len(short) >= 2:
            reasons.append(f"sleep was short on {len(short)} of the last 3 nights")
        if work_hours is not None and work_hours >= 10:
            reasons.append(f"you've been at the screen about {work_hours:.0f} hours today")
        if late_nights >= 3:
            reasons.append(f"you were up past 1 AM on {late_nights} nights this week")
        if self.next_split() == "Rest / walk" and streak >= 4:
            reasons.append("your split says rest is next")
        if not reasons:
            return None
        return ("Take a rest day: " + "; ".join(reasons) + ". Rest still counts in 75 Hard: walk, stretch, eat your "
                "targets and sleep early. Growth happens on the rest days, and the streak stays intact.")

    # ------------------------------------------------------------------ logging (voice and chat)
    def log_food(self, text: str, meal: str | None = None, d: str | None = None) -> tuple[list[dict], list[str]]:
        with self.lock:
            items, unknown = FoodTable(self.state.get("customFoods")).parse(text)
            if items:
                dd = self.day(d)
                now = time.time()
                for x in items:
                    dd["food"].append({**x, "meal": meal or guess_meal(), "src": "offline table", "via": "aether",
                                       "ts": now, "rev": self.rev + 1})
                self._commit()
            return items, unknown

    def set_field(self, field: str, value, d: str | None = None) -> None:
        with self.lock:
            dd = self.day(d)
            dd[field] = value
            self._mark(dd, field)
            self._commit()

    def add_water(self, litres: float, d: str | None = None) -> float:
        with self.lock:
            dd = self.day(d)
            dd["waterL"] = round(_num(dd.get("waterL")) + litres, 2)
            self._mark(dd, "waterL")
            self._commit()
            return dd["waterL"]

    def log_workout(self, kind: str | None, notes: str = "", d: str | None = None) -> str:
        with self.lock:
            dd = self.day(d)
            kind = kind or self.next_split()
            dd["workout"] = {"done": True, "type": kind, "notes": notes}
            dd.setdefault("checks", {})["workout"] = True
            self._mark(dd, "workout")
            self._mark(dd, "checks.workout")
            self._commit()
            return kind

    def add_reading(self, pages: float, book: str = "", d: str | None = None) -> float:
        with self.lock:
            dd = self.day(d)
            dd.setdefault("reading", []).append({"pages": pages, "book": book})
            for b in self.state.get("books", []):
                if b.get("status") == "reading" and (not book or book.lower() in b.get("title", "").lower()):
                    b["pagesRead"] = _num(b.get("pagesRead")) + pages
                    break
            self._mark(dd, "reading")
            self._commit()
            return self.pages_on(d or ymd())

    def add_spend(self, amt: float, what: str = "misc", d: str | None = None) -> None:
        with self.lock:
            dd = self.day(d)
            dd.setdefault("spend", []).append({"amt": amt, "what": what})
            dd.setdefault("checks", {})["spend"] = True
            self._mark(dd, "spend")
            self._mark(dd, "checks.spend")
            self._commit()

    def check(self, habit_id: str, on: bool = True, d: str | None = None) -> None:
        with self.lock:
            dd = self.day(d)
            dd.setdefault("checks", {})[habit_id] = on
            self._mark(dd, f"checks.{habit_id}")
            self._commit()

    def set_target_weight(self, kg: float) -> None:
        with self.lock:
            self.state["profile"]["targetWeight"] = kg
            self._commit()

    # ------------------------------------------------------------------ coaching text
    def insights(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now()
        today = ymd(now.date())
        t, T = self.totals(today), self.state["targets"]
        out = []
        p_left, k_left = T["protein"] - t["protein"], T["kcal"] - t["kcal"]
        if p_left > 0:
            tip = (" A shake with two scoops of whey, milk, banana and peanut butter covers about 55 g." if p_left >= 40
                   else " A scoop of whey in milk or 100 g paneer closes it." if p_left >= 20
                   else " A katori of curd or a handful of roasted chana will do.")
            out.append(f"{round(p_left)} g protein to go today.{tip}")
        else:
            out.append("Protein is done for today.")
        if k_left > 300:
            out.append(f"{round(k_left)} kcal left." + (" Add two extra roti and a glass of full-cream milk tonight."
                                                        if now.hour >= 18 else " Spread it over two more meals plus the shake."))
        r = self.weight_rate(today=today)
        if r is None:
            out.append("Log your weight each morning to unlock the trend.")
        elif r < 0.15:
            out.append(f"Weight trend is {r:+.2f} kg a week, slow for a bulk: add about 250 kcal a day.")
        elif r > 0.6:
            out.append(f"Weight trend is {r:+.2f} kg a week, a bit fast. Trim about 150 kcal of junk, keep protein high.")
        else:
            out.append(f"Weight trend {r:+.2f} kg a week is right in the lean-bulk zone.")
        yest = add_days(today, -1)
        if diff_days(yest, self.state["profile"]["startDate"]) >= 0 and self.day_score(yest)["pct"] < GOOD_DAY:
            out.append("Yesterday slipped. Never miss twice: today only needs the minimum version done.")
        rest = self.rest_advice(today)
        if rest:
            out.append(rest)
        return out

    def status(self, spoken: bool = False, today: str | None = None) -> str:
        t_day = today or ymd()
        c = self.challenge(t_day)
        sc = self.day_score(t_day)
        t, T = self.totals(t_day), self.state["targets"]
        missing = [h["label"].lower() for h in self.state["habits"] if not self.habit_done(h, t_day)]
        head = (f"Day {c['dayInBlock']} of 75" + (f", block {c['block']}" if c["block"] > 1 else "") if c["started"]
                else f"75 Hard starts in {c['daysUntil']} days")
        s = self.streak(t_day)
        bits = [f"{head}. {sc['done']} of {sc['total']} habits done today" + (f", {s}-day streak" if s else "") + ".",
                f"Food: {round(t['kcal'])} of {T['kcal']} kcal and {round(t['protein'])} of {T['protein']} g protein."]
        w = self.weights()
        if w:
            target = self.state["profile"]["targetWeight"]
            bits.append(f"Weight {w[-1][1]:g} kg, target {target:g} kg.")
        if missing:
            bits.append("Still to do: " + ", ".join(missing[:5]) + ".")
        if not spoken:
            bits += self.insights()[:2]
        return " ".join(bits)

    def coach_context(self) -> str:
        """Plain-text summary of the last week, for the local model when it coaches."""
        today = ymd()
        c = self.challenge(today)
        T, P = self.state["targets"], self.state["profile"]
        lines = [f"Today is {today}. " + (f"Day {c['dayInBlock']}/75 of block {c['block']}." if c["started"]
                                          else f"The challenge starts in {c['daysUntil']} days."),
                 f"Targets: {T['kcal']} kcal, {T['protein']} g protein, {T['pages']} pages, {T['workoutsPerWeek']} "
                 f"workouts/week, sleep {T['sleepH']}h, water {T['waterL']} L.",
                 f"Weight: latest {self.latest_weight():g} kg (start {P.get('startWeight')}, target {P.get('targetWeight')})"
                 + (f", trend {self.weight_rate():+.2f} kg/week." if self.weight_rate() is not None else ".")]
        for i in range(6, -1, -1):
            d = add_days(today, -i)
            dd = self.state["days"].get(d)
            if not dd:
                continue
            t, sc = self.totals(d), self.day_score(d)
            lines.append(f"{d}: {round(t['kcal'])} kcal, {round(t['protein'])} g protein, {self.pages_on(d):g} pages, "
                         f"workout {(dd.get('workout') or {}).get('type', 'yes') if self.habit_done({'id': 'workout'}, d) else 'no'}, "
                         f"sleep {dd.get('sleepH') or '?'}h, habits {sc['done']}/{sc['total']}")
        goals = [g["title"] for g in self.state.get("goals", []) if not g.get("done")]
        if goals:
            lines.append("Open goals: " + "; ".join(goals))
        return "\n".join(lines)


COACH_PROMPT = """You are AETHER, coaching the user through a 75-day challenge (75 Hard) inside their Grind 75 tracker.
Use only the numbers in the context; never invent logs. Be concise (under 150 words unless asked for a plan), specific
and kind, and push slightly harder than comfortable. Rules: "never miss twice"; a bad day becomes a minimum-version day,
never a restart. Suggest a rest day when training, sleep or work load says so. Food suggestions should fit the foods the
user actually logs. Use INR for money."""


# ---------------------------------------------------------------------------------------------- voice / chat
NUM = r"(\d+(?:\.\d+)?)"
WEIGHT = re.compile(rf"^(?:log\s+)?(?:my\s+)?(?:weight|weighed|i\s+weigh(?:ed)?)(?:\s+is|\s+was|\s+today)?[:\s]+{NUM}\s*(?:kg|kgs|kilos?)?\.?$", re.I)
SLEEP = re.compile(rf"^(?:i\s+)?slept\s+(?:for\s+)?{NUM}\s*(?:h|hrs?|hours?)\b", re.I)
WATER = re.compile(rf"^(?:i\s+)?(?:drank|had|log(?:ged)?|add)\s+(?:(half\s+a|a|one|{NUM})\s*)?(l|litres?|liters?|ml|glass(?:es)?|bottles?)\s+(?:of\s+)?water", re.I)
WORKOUT = re.compile(r"^(?:i\s+)?(?:workout|work\s*out|trained|gym|lifted)(?:\s+(?:is|was))?\s*(?:done|finished|complete[d]?)?(?:[,:\s-]+(.*))?$|^(?:done\s+with|finished)\s+(?:my\s+)?(?:workout|gym)(?:[,:\s-]+(.*))?$", re.I)
READ = re.compile(rf"^(?:i\s+)?read\s+{NUM}\s+pages?(?:\s+of\s+(.+))?", re.I)
SPEND = re.compile(rf"^(?:i\s+)?(?:spent|paid)\s+(?:rs\.?|₹|inr)?\s*{NUM}\s*(?:rs|rupees|inr)?(?:\s+(?:on|for)\s+(.+))?", re.I)
FOOD = re.compile(r"^(?:i\s+)?(?:had|ate|have\s+had|just\s+had|log(?:ged)?\s+food|add\s+food)\s+(.+)$", re.I)
STATUS = re.compile(r"\b(?:75\s*hard|grind\s*75|my\s+streak|how(?:'s| is| am i doing (?:on|with))\s+(?:my\s+)?(?:75|challenge|bulk|diet)|"
                    r"protein\s+(?:left|to\s+go)|how\s+much\s+protein|calories?\s+(?:left|to\s+go)|how\s+many\s+calories)", re.I)
REST = re.compile(r"\b(?:should\s+i\s+(?:rest|take\s+a\s+rest\s+day|skip\s+(?:the\s+)?gym)|rest\s+day\??$)", re.I)
TARGET = re.compile(rf"\b(?:my\s+)?target\s+weight\s+(?:is\s+|to\s+)?{NUM}\s*(?:kg|kgs)?", re.I)
WORKOUT_KINDS = {"back": SPLIT[0], "biceps": SPLIT[0], "chest": SPLIT[1], "shoulder": SPLIT[1], "push": SPLIT[1],
                 "quad": SPLIT[2], "leg": SPLIT[2], "hamstring": SPLIT[3], "calves": SPLIT[3], "abs": SPLIT[3],
                 "cardio": "Cardio", "run": "Cardio", "full body": "Full body", "arms": "Arms + shoulders",
                 "rest": "Rest / walk", "walk": "Rest / walk"}
FOOD_WORDS = re.compile(r"\b(roti|chapati|rice|dal|paneer|curd|milk|shake|whey|banana|egg|soya|rajma|chole|poha|upma|idli|"
                        r"dosa|paratha|sabzi|peanut|chana|oats|bread|apple|katori|glass|scoop|almonds?|ghee|maggi|pizza|"
                        r"burger|biryani|khichdi|sandwich|tea|coffee|chai)\b", re.I)


def handle(grind: Grind, text: str) -> str | None:
    """Plain-language logging and questions for the tracker. Returns the reply, or None if it isn't one of these."""
    t = text.strip().rstrip(".!")
    if m := TARGET.search(t):
        grind.set_target_weight(float(m.group(1)))
        return f"Target weight set to {float(m.group(1)):g} kg."
    if m := WEIGHT.match(t):
        w = float(m.group(1))
        grind.set_field("weight", w)
        r = grind.weight_rate()
        return f"Logged {w:g} kg." + (f" Trend {r:+.2f} kg a week." if r is not None else "")
    if m := SLEEP.match(t):
        h = float(m.group(1))
        grind.set_field("sleepH", h)
        return f"Logged {h:g} hours of sleep." + ("" if h >= grind.state["targets"]["sleepH"] else
                                                   " That's under your target, so go easier today and get to bed early.")
    if m := WATER.match(t):
        amount, num, unit = m.group(1), m.group(2), m.group(3).lower()
        n = 0.5 if amount and amount.lower().startswith("half") else float(num) if num else 1.0
        litres = n / 1000 if unit == "ml" else n * 0.25 if unit.startswith("glass") else n * 0.75 if unit.startswith("bottle") else n
        total = grind.add_water(litres)
        return f"Water: {total:g} of {grind.state['targets']['waterL']:g} L today."
    if m := READ.match(t):
        pages = grind.add_reading(float(m.group(1)), (m.group(2) or "").strip())
        return f"Logged. {pages:g} of {grind.state['targets']['pages']} pages today."
    if m := SPEND.match(t):
        grind.add_spend(float(m.group(1)), (m.group(2) or "misc").strip())
        wk = grind.spend_this_week()
        return f"Logged ₹{float(m.group(1)):g}. This week ₹{wk:g} of ₹{grind.state['targets']['weeklySpend']:g}."
    if m := WORKOUT.match(t):
        rest = (m.group(1) or m.group(2) or "").strip()
        kind = next((v for k, v in WORKOUT_KINDS.items() if k in rest.lower()), None)
        kind = grind.log_workout(kind, rest)
        return f"Workout logged: {kind}. Next in the rotation: {grind.next_split()}."
    if REST.search(t):
        return grind.rest_advice() or ("Nothing says you need rest today: sleep and training load look fine. "
                                       "If your body says otherwise, a walk-and-stretch day still counts.")
    if STATUS.search(t):
        return grind.status(spoken=False)
    if (m := FOOD.match(t)) and FOOD_WORDS.search(m.group(1)):
        items, unknown = grind.log_food(m.group(1))
        if not items:
            return None
        tot = grind.totals()
        T = grind.state["targets"]
        added = sum(x["kcal"] for x in items), sum(x["protein"] for x in items)
        msg = (f"Logged {', '.join(x['name'] for x in items)}: {added[0]:g} kcal, {added[1]:g} g protein. "
               f"Today {round(tot['kcal'])} of {T['kcal']} kcal, {round(tot['protein'])} of {T['protein']} g protein.")
        if unknown:
            msg += f" I didn't recognise {', '.join(unknown)}; add it in Grind 75's food page."
        return msg
    return None
