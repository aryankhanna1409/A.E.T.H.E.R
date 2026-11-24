"""Deterministic temporal reasoning: resolve relative dates the LLM extracted
("tomorrow", "next friday", "in 3 days") to real dates, and validate them."""
from __future__ import annotations

import re
from datetime import date, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
UNITS = {"day": 1, "days": 1, "week": 7, "weeks": 7}


def resolve(expr: str, today: date | None = None) -> date | None:
    today = today or date.today()
    e = expr.strip().lower()
    if e in {"today", "tonight", "now"}:
        return today
    if e == "tomorrow":
        return today + timedelta(days=1)
    if e == "yesterday":
        return today - timedelta(days=1)
    if e in {"this week", "this weekend"}:
        return today + timedelta(days=(5 - today.weekday()) % 7) if "weekend" in e else today
    if e == "next week":
        return today + timedelta(days=7 - today.weekday())
    m = re.fullmatch(r"in (\d+) (days?|weeks?)", e)
    if m:
        return today + timedelta(days=int(m.group(1)) * UNITS[m.group(2)])
    m = re.fullmatch(r"(next |this )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", e)
    if m:
        delta = (WEEKDAYS.index(m.group(2)) - today.weekday()) % 7
        if delta == 0 or m.group(1) == "next ":
            delta = delta or 7
        return today + timedelta(days=delta)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", e)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def find_expressions(text: str) -> list[str]:
    pat = (r"\b(today|tonight|tomorrow|yesterday|this weekend|this week|next week|in \d+ (?:days?|weeks?)|"
           r"(?:next |this )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|\d{4}-\d{2}-\d{2})\b")
    return [m.group(1) for m in re.finditer(pat, text.lower())]
