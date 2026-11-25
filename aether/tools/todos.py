"""Your to-do list as tools: add, list, tick off, and turn any item into a daily (or weekly...) thing.

To-dos and reminders are the same list (memory/prospective.py), so whatever you add by voice, in chat or on
the Today page shows up everywhere, and repeating ones come back on their next day after you tick them off.
"""
from __future__ import annotations

import re
from datetime import date

from ..memory.prospective import ReminderItem, describe_repeat, parse_at, parse_repeat
from .registry import Tool


def clock12(hm: str) -> str:
    """'18:00' -> '6 pm', '09:30' -> '9:30 am'."""
    h, m = (int(x) for x in hm.split(":"))
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12} {suffix}" if m == 0 else f"{h12}:{m:02d} {suffix}"


def when_words(item: ReminderItem, today: date | None = None) -> str:
    today = today or date.today()
    if item.repeat:
        when = describe_repeat(item.repeat)
    else:
        d = date.fromisoformat(item.due)
        when = "today" if d == today else "tomorrow" if (d - today).days == 1 else f"on {d:%A %d %B}" \
            if d > today else f"overdue since {d:%d %B}"
    return when + (f" at {clock12(item.at)}" if item.at else "")


def say_item(item: ReminderItem, today: date | None = None) -> str:
    return f"{item.text} ({when_words(item, today)})"


def todo_tools(a) -> list[Tool]:
    def add(text: str, when: str = "", repeat: str = "", at: str = "") -> str:
        rep = parse_repeat(f"{repeat} {when}") or (repeat if repeat in {"daily", "weekdays", "weekends"} else "")
        t = parse_at(at) if at else parse_at(when) if when else ""
        if not t and re.search(r"morning|evening|night|afternoon", repeat, re.I):
            t = parse_at(repeat)   # "every morning" -> 08:00
        day = "" if rep else re.sub(r"\b(?:at\s+)?\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)\b|\bat\s+\d{1,2}(?::\d{2})?\b|"
                                    r"\b(?:this\s+)?(?:morning|afternoon|evening|night|tonight|noon)\b", "", when).strip()
        item = a.reminders.add(text, day or ("" if rep else "today"), rep, t)
        if item.repeat or item.at:
            return f"Got it. I'll remind you to {item.text} {when_words(item)}."
        return f"Added {item.text} to your to-do list for {when_words(item).removeprefix('on ')}."

    def listing(when: str = "") -> str:
        today = date.today()
        items = a.reminders.due(today) if when.strip().lower() == "today" else a.reminders.open()
        if not items:
            return "Your to-do list is empty." if not when else "Nothing on your list for today."
        due = [i for i in items if i.due <= today.isoformat()]
        later = [i for i in items if i.due > today.isoformat()]
        lines = []
        if due:
            lines.append("Today:\n" + "\n".join(f"- {say_item(i, today)}" for i in due))
        if later:
            lines.append("Coming up:\n" + "\n".join(f"- {say_item(i, today)}" for i in later[:10]))
        return "\n".join(lines)

    def done(item: str) -> str:
        it = a.reminders.find(item) if item and item.lower() not in {"it", "that", "this"} else a.reminders.last()
        if it is None:
            return f"I couldn't find '{item}' on your list."
        after = a.reminders.set_status(it.id, "done")
        if it.repeat and after:
            nxt = date.fromisoformat(after.due)
            return f"Ticked off {it.text}. It comes back " + (
                "tomorrow." if (nxt - date.today()).days == 1 else f"on {nxt:%A}.")
        return f"Ticked off: {it.text}."

    def remove(item: str) -> str:
        it = a.reminders.find(item) if item and item.lower() not in {"it", "that", "this"} else a.reminders.last()
        if it is None:
            return f"I couldn't find '{item}' on your list."
        a.reminders.set_status(it.id, "dismissed")
        return f"Removed {it.text} from your list."

    def make_repeat(repeat: str = "daily", item: str = "", at: str = "") -> str:
        it = a.reminders.find(item) if item and item.lower() not in {"it", "that", "this"} else a.reminders.last()
        if it is None:
            return "There's nothing on your list to repeat yet. Say, for example, 'remind me to drink water every day'."
        rep = parse_repeat(repeat) or (repeat if repeat in {"daily", "weekdays", "weekends"} else "daily")
        after = a.reminders.make_repeating(it.id, rep, parse_at(at) if at else parse_at(repeat))
        return f"Done. {after.text[:1].upper() + after.text[1:]} now repeats {when_words(after)}." if after \
            else "That item is gone."

    return [
        Tool("remind", "add something to the user's to-do list / set a reminder, optionally repeating and at a time",
             {"text": "what", "when": "optional day: today, tomorrow, friday, 2026-12-01",
              "repeat": "optional: daily, weekdays, weekends, every monday, every 3 days",
              "at": "optional time of day, e.g. 9pm"}, "memory", add),
        Tool("todos", "the user's to-do list and reminders", {"when": "optional: 'today' for just today"}, "memory",
             listing),
        Tool("complete_todo", "tick off a to-do (a repeating one comes back on its next day)",
             {"item": "a few words of it, or 'it' for the last one"}, "memory", done),
        Tool("remove_todo", "delete a to-do or reminder for good", {"item": "a few words of it"}, "memory", remove),
        Tool("repeat_todo", "make a to-do repeat (e.g. turn it into a daily thing)",
             {"repeat": "daily, weekdays, every monday...", "item": "a few words of it, or empty for the last one",
              "at": "optional time, e.g. 8am"}, "memory", make_repeat),
    ]
