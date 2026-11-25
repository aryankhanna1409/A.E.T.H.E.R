"""Quick commands: the everyday phrasings that should be instant.

"set a timer for 10 minutes", "volume up", "play lofi on youtube", "what's 15%
of 2400", "5 km in miles", "take a note: ...", "lock my pc"... are recognised
here by code and run straight away, without waiting for the language model.
Matching is deliberately strict (whole utterance, short), so a normal sentence
that merely contains these words still goes to the model. Every action still
runs as a tool through the policy engine.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from ..tools.everyday import CONVERT, SITES, calc, convert, fmt_num

WAKE = re.compile(r"^(?:(?:hey|ok|okay|hi|yo)\s+)?(?:jarvis|aether|ether|nova)[\s,.!]+", re.I)
POLITE = re.compile(r"^(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+|(?:i\s+(?:want|need)\s+you\s+to|go\s+ahead\s+and|"
                    r"just|quickly|kindly)\s+)|\s*(?:,?\s*please|,?\s*thanks?(?:\s+you)?|,?\s*(?:jarvis|aether|nova|sir|buddy)|"
                    r"\s+for\s+me|\s+(?:right\s+)?now)\s*[.!?]*$", re.I)

# ---- to-dos and reminders: the schedule words are lifted out of the sentence by code
REPEAT = (r"(?:every\s*day|everyday|daily|each\s+day|every\s+(?:single\s+)?(?:morning|night|evening|afternoon)|"
          r"(?:every|on)\s+week\s*days|every\s+weekend|on\s+weekends|weekly|every\s+week|"
          r"(?:every|each)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
          r"on\s+(?:mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays)|every\s+\d+\s+days?)")
CLOCK = r"(?:at\s+)?\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|at\s+\d{1,2}(?::\d{2})?(?!\s*(?:days?|weeks?|minutes?|%))|at\s+(?:noon|midnight)"
PART = r"(?:tonight|this\s+(?:morning|afternoon|evening)|in\s+the\s+(?:morning|afternoon|evening)|at\s+night)"
DAY = (r"(?:today|tomorrow|day\s+after\s+tomorrow|(?:on\s+|next\s+|this\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|"
       r"sunday)|on\s+\d{4}-\d{2}-\d{2}|in\s+\d+\s+(?:days?|weeks?)|next\s+week|this\s+weekend)")
TODO_WORD = r"(?:to-?\s?do|todo|task|reminder)"
LIST_WORD = rf"(?:{TODO_WORD}s?(?:\s+list)?|list|reminders)"


def split_schedule(text: str) -> tuple[str, str, str, str]:
    """'drink water every day at 9pm' -> ('drink water', '', 'every day', 'at 9pm')."""
    found = {}
    rest = " " + text + " "
    for key, rx in (("repeat", REPEAT), ("at", CLOCK), ("part", PART), ("day", DAY)):
        m = re.search(rf"[\s,]({rx})(?=[\s,.!?])", rest, re.I)
        if m:
            found[key] = m.group(1)
            rest = rest[:m.start()] + " " + rest[m.end():]
    at = found.get("at", "") or found.get("part", "")
    rest = re.sub(r"\s+", " ", rest).strip(" ,.-:")
    rest = re.sub(r"^(?:to|that|about|i\s+(?:need|have)\s+to|i\s+should)\s+", "", rest, flags=re.I)
    rest = re.sub(r"\s+(?:and|,)$", "", rest).strip()
    return rest, found.get("day", "").removeprefix("on ").strip(), found.get("repeat", ""), at


def todo(text: str) -> "Quick | None":
    what, day, repeat, at = split_schedule(text)
    if not what or len(what) > 90:
        return None
    args = {"text": what}
    if day:
        args["when"] = day
    if repeat:
        args["repeat"] = repeat
    if at:
        args["at"] = at
    return Quick("tool", tool="remind", args=args)


@dataclass
class Quick:
    kind: str  # answer | tool | copy_last
    text: str = ""
    tool: str = ""
    args: dict = field(default_factory=dict)
    loose: bool = False  # a guess from everyday wording: the caller drops it if nothing on your list matches


def _clean(text: str) -> str:
    t = WAKE.sub("", text.strip())
    for _ in range(3):
        t = POLITE.sub("", t).strip()
    return t.rstrip(".!").strip()


def match(text: str, now: datetime, apps=None) -> Quick | None:
    """`apps`: a set of app names, or a function that says whether an app name can be opened on this PC."""
    if not text or len(text) > 160:
        return None
    t = _clean(text)
    low = t.lower()
    if callable(apps):
        can_open = apps
    else:
        names = {a.lower() for a in (apps or set())}
        can_open = lambda n, exact=False: n.lower() in names  # noqa: E731

    # ---- clock and calendar facts
    if re.fullmatch(r"(?:what(?:'s| is)\s+)?(?:the\s+)?time(?:\s+is\s+it)?(?:\s+now)?\??|what\s+time\s+is\s+it(?:\s+now)?\??", low):
        return Quick("answer", f"It's {now:%H:%M}.")
    if re.fullmatch(r"(?:what(?:'s| is)\s+)?(?:the\s+|today'?s\s+)?date(?:\s+today)?\??|what\s+day\s+is\s+(?:it|today)\??", low):
        return Quick("answer", f"It's {now:%A %d %B %Y}.")


    # ---- to-dos and reminders ("remind me to drink water every day at 9", "make it a daily thing")
    m = re.fullmatch(rf"(?:make|turn|set)\s+(?:it|that|this|the\s+last\s+one)(?:\s+into)?\s+(?:a\s+|an\s+)?"
                     rf"(daily|everyday|every\s*day|weekly|recurring|repeating|weekday|regular)(?:\s+(?:thing|task|habit|"
                     rf"{TODO_WORD}|one|reminder|routine))?(.*)|(?:make\s+(?:it|that|this)\s+)?repeat(?:ing)?\s+(?:it\s+|that\s+)?(.+)|"
                     rf"(?:(?:do|remind\s+me\s+(?:of|about)?)\s+)?(?:it|that|this)\s+({REPEAT})(.*)", low)
    if m:
        rep = m.group(1) or m.group(3) or m.group(4) or "daily"
        rep = {"recurring": "daily", "repeating": "daily", "regular": "daily", "weekday": "weekdays"}.get(rep.strip(), rep)
        extra = (m.group(2) or m.group(5) or "").strip()
        args = {"repeat": rep}
        _, _, rep2, at = split_schedule(extra) if extra else ("", "", "", "")
        if rep2:
            args["repeat"] = rep2
        if at:
            args["at"] = at
        return Quick("tool", tool="repeat_todo", args=args)
    if re.fullmatch(rf"(?:what(?:'s|\s+is|\s+are)\s+(?:on\s+)?|show\s+(?:me\s+)?|read\s+(?:me\s+)?|list\s+|check\s+|open\s+)?"
                    rf"(?:my|the)\s+{LIST_WORD}(?:\s+(?:for\s+)?today)?\??|what\s+do\s+i\s+(?:have|need)\s+to\s+do(?:\s+today)?\??|"
                    rf"(?:any|what)\s+{TODO_WORD}s(?:\s+(?:for\s+)?today)?\??|{TODO_WORD}s|{TODO_WORD}\s+list", low):
        return Quick("tool", tool="todos", args={"when": "today"} if "today" in low else {})
    m = re.fullmatch(rf"(?:add|put|write)\s+(.+?)\s+(?:to|on|in|onto)\s+(?:my\s+|the\s+)?{LIST_WORD}(.*)", t, re.I) or \
        re.fullmatch(rf"(?:make|create|add|set|new|start)\s+(?:me\s+)?(?:a\s+|an\s+)?(?:new\s+)?(?:daily\s+)?{TODO_WORD}"
                     rf"(?:\s+(?:list\s+)?item)?(?:\s*[:,\-]\s*|\s+(?:to|for|that|saying|about)\s+)(.+)()", t, re.I) or \
        re.fullmatch(rf"{TODO_WORD}\s*[:,\-]\s*(.+)()", t, re.I)
    if m:
        body = (m.group(1) + " " + (m.group(2) or "")).strip()
        if re.search(r"\bdaily\s+(?:to-?\s?do|todo|task|reminder)", low) and not re.search(REPEAT, body, re.I):
            body += " every day"
        q = todo(body)
        if q:
            return q
    m = re.fullmatch(rf"({REPEAT}(?:\s+(?:{CLOCK}|{PART}))?),?\s+remind\s+me\s+(?:to\s+|about\s+)?(.+)", t, re.I)
    if m:
        q = todo(m.group(2) + " " + m.group(1))
        if q:
            return q
    m = re.fullmatch(r"remind\s+me\s+(?!in\s+\d)(?:(?:to|about|that)\s+)?(.+)", t, re.I)
    if m and not re.fullmatch(r"in\s+.+?\s+to\s+.+", m.group(1), re.I):
        q = todo(m.group(1))
        if q:
            return q
    m = re.fullmatch(rf"(?:mark|tick|check|cross)\s+(?:off\s+)?(.+?)(?:\s+(?:as\s+)?(?:done|complete|completed|off))?"
                     rf"(?:\s+(?:on|from)\s+(?:my\s+)?{LIST_WORD})?|i(?:'ve|\s+have)?\s+(?:finished|completed|done|did)\s+(.+)|"
                     rf"(?:done|finished)\s+with\s+(.+)|(.+?)\s+is\s+(?:done|finished|complete)", low)
    if m and not re.search(r"\b(?:timer|alarm|email|mail)\b", low):
        item = next(g for g in m.groups() if g)
        item = re.sub(rf"^(?:the\s+|my\s+)|\s+(?:{TODO_WORD}|one|item)$", "", item.strip())
        if item and len(item.split()) <= 8:
            return Quick("tool", tool="complete_todo", args={"item": item},
                         loose=not re.match(r"(?:mark|tick|check|cross)\b", low))
    m = re.fullmatch(rf"(?:remove|delete|cancel|drop|take)\s+(?:the\s+)?(.+?)(?:\s+{TODO_WORD})?\s+(?:off|from)\s+(?:my\s+|the\s+)?{LIST_WORD}|"
                     rf"(?:cancel|delete|remove)\s+(?:the\s+|my\s+)?(.+?)\s+reminder", low)
    if m:
        return Quick("tool", tool="remove_todo", args={"item": (m.group(1) or m.group(2)).strip()})

    # ---- timers and alarms
    m = re.fullmatch(r"(?:set\s+(?:a|an|the)?\s*)?(?:(.+?)\s+)?timer\s+(?:for\s+)?(.+?)(?:\s+for\s+(?:the\s+|my\s+)?(.+))?\??", low)
    if m and re.search(r"\d|an? (?:hour|minute)|half an hour", m.group(2)):
        label = (m.group(3) or m.group(1) or "").strip()
        label = "" if label in {"a", "an", "the", "set", "set a", "start a"} else re.sub(r"^(?:start\s+)?(?:a|an)\s+", "", label)
        return Quick("tool", tool="timer", args={"duration": m.group(2), "label": label})
    m = re.fullmatch(r"(?:set\s+)?(?:a\s+|an\s+)?timer\s+(.+)", low)
    if m and re.search(r"\d", m.group(1)):
        return Quick("tool", tool="timer", args={"duration": m.group(1), "label": ""})
    m = re.fullmatch(r"remind\s+me\s+in\s+(.+?)\s+to\s+(.+)", low)
    if m and re.search(r"\d|an? (?:hour|minute)", m.group(1)):
        return Quick("tool", tool="timer", args={"duration": m.group(1), "label": m.group(2)})
    m = re.fullmatch(r"(?:set\s+(?:an?\s+)?alarm\s+(?:for|at)|wake\s+me\s+(?:up\s+)?at|alarm\s+(?:for|at))\s+(.+?)(?:\s+(?:to|for)\s+(.+))?", low)
    if m:
        return Quick("tool", tool="timer", args={"at": m.group(1), "label": m.group(2) or ""})
    if re.fullmatch(r"(?:how\s+(?:long|much\s+time)\s+(?:is\s+)?left(?:\s+on\s+(?:the|my)\s+timer)?|(?:what|which|any)\s+timers?(?:\s+are\s+running)?|(?:show|list)\s+(?:my\s+)?timers)\??", low):
        return Quick("tool", tool="timers")
    m = re.fullmatch(r"(?:cancel|stop|delete)\s+(?:the\s+|my\s+)?(?:(.+?)\s+)?(?:timer|alarm)s?", low)
    if m:
        return Quick("tool", tool="cancel_timer", args={"label": (m.group(1) or "").replace("all", "").strip()})

    # ---- media and volume
    if re.fullmatch(r"(?:turn\s+(?:it|the\s+volume|the\s+music)\s+up|volume\s+up|louder|increase\s+(?:the\s+)?volume)", low):
        return Quick("tool", tool="media", args={"action": "volume_up"})
    if re.fullmatch(r"(?:turn\s+(?:it|the\s+volume|the\s+music)\s+down|volume\s+down|quieter|softer|decrease\s+(?:the\s+)?volume|lower\s+(?:the\s+)?volume)", low):
        return Quick("tool", tool="media", args={"action": "volume_down"})
    m = re.fullmatch(r"(?:set\s+(?:the\s+)?)?volume\s+(?:to\s+)?(\d{1,3})\s*%?(?:\s+percent)?", low)
    if m:
        return Quick("tool", tool="media", args={"action": "volume", "amount": m.group(1)})
    if re.fullmatch(r"(?:un)?mute(?:\s+(?:the\s+)?(?:sound|audio|volume|pc|computer))?", low):
        return Quick("tool", tool="media", args={"action": "mute"})
    if re.fullmatch(r"(?:pause|stop|resume|unpause)(?:\s+(?:the\s+)?(?:music|song|video|playback|it))?|play\s+(?:the\s+)?(?:music|song)", low):
        return Quick("tool", tool="media", args={"action": "pause"})
    if re.fullmatch(r"(?:next|skip)(?:\s+(?:this\s+)?(?:song|track|video))?|(?:play\s+)?(?:the\s+)?next\s+(?:song|track)", low):
        return Quick("tool", tool="media", args={"action": "next"})
    if re.fullmatch(r"(?:previous|last|go\s+back)(?:\s+(?:song|track))?|(?:play\s+)?(?:the\s+)?previous\s+(?:song|track)", low):
        return Quick("tool", tool="media", args={"action": "previous"})
    m = re.fullmatch(r"(?:set\s+(?:the\s+)?)?(?:screen\s+)?brightness\s+(?:to\s+)?(\d{1,3})\s*%?", low)
    if m:
        return Quick("tool", tool="brightness", args={"level": m.group(1)})

    # ---- power
    if re.fullmatch(r"lock\s+(?:my\s+|the\s+)?(?:pc|computer|laptop|screen|windows)", low):
        return Quick("tool", tool="power", args={"action": "lock"})
    m = re.fullmatch(r"(shut\s*down|restart|reboot|sleep)\s+(?:my\s+|the\s+)?(?:pc|computer|laptop)|put\s+(?:my\s+|the\s+)?(?:pc|computer|laptop)\s+to\s+(sleep)", low)
    if m:
        act = (m.group(1) or m.group(2)).replace(" ", "")
        return Quick("tool", tool="power", args={"action": {"reboot": "restart"}.get(act, act)})
    if re.fullmatch(r"cancel\s+(?:the\s+)?(?:shutdown|restart)", low):
        return Quick("tool", tool="power", args={"action": "cancel"})

    # ---- web, YouTube, apps
    m = re.fullmatch(r"play\s+(.+?)\s+on\s+youtube|youtube\s+(.+)|(?:put\s+on|play)\s+(?:some\s+)?(.+?)\s+(?:music|songs|playlist)\s+on\s+youtube", low)
    if m:
        return Quick("tool", tool="play_youtube", args={"query": next(g for g in m.groups() if g)})
    m = re.fullmatch(r"(?:google|search\s+(?:google|the\s+web|online)\s+for|look\s+up)\s+(.+)", low)
    if m and not low.startswith("look up my"):
        return Quick("tool", tool="search_in_browser", args={"query": m.group(1)})
    # ---- weather and news (live lookups, no model needed to ask)
    m = re.fullmatch(r"(?:what(?:'s|\s+is)\s+the\s+|how(?:'s|\s+is)\s+the\s+|check\s+the\s+)?(?:weather|forecast|temperature)"
                     r"(?:\s+like)?(?:\s+(today|tomorrow))?(?:\s+(?:in|at|for)\s+([a-z .'-]{2,30}?))?(?:\s+(today|tomorrow))?\??|"
                     r"(?:is\s+it|will\s+it|is\s+it\s+going\s+to)\s+(?:rain|be\s+(?:hot|cold|sunny))(?:\s+(today|tomorrow))?\??", low)
    if m:
        when = m.group(1) or m.group(3) or m.group(4) or "today"
        args = {"when": when}
        if m.group(2):
            args["place"] = m.group(2).strip()
        return Quick("tool", tool="weather", args=args)
    m = re.fullmatch(r"(?:what(?:'s|\s+is)\s+(?:the\s+)?|give\s+me\s+(?:the\s+)?|read\s+(?:me\s+)?(?:the\s+)?|any\s+)?(?:latest\s+|today'?s\s+)?"
                     r"(?:news|headlines)(?:\s+(?:about|on|in)\s+(.+?))?(?:\s+today)?\??", low)
    if m:
        return Quick("tool", tool="news", args={"topic": m.group(1)} if m.group(1) else {})

    # ---- AETHER's own windows
    m = re.fullmatch(r"(?:open|show|bring\s+up|pull\s+up|launch|start)\s+(?:up\s+)?(?:the\s+|your\s+|my\s+)?(?:(hud|voice\s+hud|mic)|"
                     r"(?:aether|jarvis|nova)(?:\s+(app|window|settings|today|memory|goals|inbox))?|(?:aether|jarvis)'?s?\s+(settings|today\s+page|memory))",
                     low)
    if m:
        view = "hud" if m.group(1) else (m.group(2) or m.group(3) or "app").replace(" page", "")
        return Quick("tool", tool="show_aether", args={"view": view})

    # ---- apps, folders, the desktop
    m = re.fullmatch(r"(?:open|launch|start|run|fire\s+up|pull\s+up|go\s+to|bring\s+up)\s+(?:up\s+)?(.+)", low)
    if m:
        target = re.sub(r"^(?:the|my)\s+", "", m.group(1).strip()).strip()
        if target in SITES and can_open(target, exact=True):   # the Spotify / WhatsApp app when it's installed
            return Quick("tool", tool="app_open", args={"name": target})
        if target in SITES or re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", target):
            return Quick("tool", tool="open_website", args={"site": target})
        if len(target.split()) <= 5 and can_open(target):
            return Quick("tool", tool="app_open", args={"name": target})
        site = re.sub(r"\s+(?:website|site|web\s*site|in\s+(?:the\s+)?browser)$", "", target)
        if site != target and (site in SITES or re.fullmatch(r"[\w-]+(\.[\w-]+)+", site)):
            return Quick("tool", tool="open_website", args={"site": site})
    m = re.fullmatch(r"(?:close|quit|exit|kill|shut)\s+(?:down\s+)?(?:the\s+|my\s+)?([\w .+-]{2,40}?)(?:\s+(?:app|window|for\s+me))?", low)
    if m and m.group(1) not in {"it", "this", "that", "everything", "all", "pc", "computer", "laptop", "the pc"}:
        return Quick("tool", tool="app_close", args={"name": m.group(1).strip()})
    if re.fullmatch(r"(?:take\s+(?:a\s+)?|grab\s+(?:a\s+)?)?screen\s*shot|capture\s+(?:my\s+|the\s+)?screen|"
                    r"take\s+a\s+(?:screen\s*)?(?:capture|grab)(?:\s+of\s+(?:my|the)\s+screen)?", low):
        return Quick("tool", tool="screenshot")
    if re.fullmatch(r"show\s+(?:me\s+)?(?:the\s+)?desktop|minimi[sz]e\s+(?:everything|all(?:\s+windows)?)|go\s+to\s+(?:the\s+)?desktop", low):
        return Quick("tool", tool="show_desktop")

    # ---- notes and clipboard
    m = re.fullmatch(r"(?:take\s+(?:a\s+)?note|note\s+(?:that|down)|make\s+a\s+note|write\s+(?:this\s+)?down)[\s:,-]+(?:that\s+)?(.+)", t, re.I)
    if m:
        return Quick("tool", tool="note", args={"text": m.group(1)})
    if re.fullmatch(r"(?:read|show)\s+(?:me\s+)?my\s+(?:latest\s+|last\s+)?notes|what\s+(?:are|were)\s+my\s+notes\??", low):
        return Quick("tool", tool="read_notes")
    if re.fullmatch(r"copy\s+(?:that|this|it)(?:\s+to\s+(?:the\s+)?clipboard)?", low):
        return Quick("copy_last")

    # ---- the machine
    if re.fullmatch(r"(?:how(?:'s| is)\s+(?:my\s+)?(?:battery|pc|computer|laptop|system)(?:\s+doing)?|(?:battery|system)\s+(?:status|level|health)|battery|how\s+much\s+battery(?:\s+(?:is\s+)?left)?)\??", low):
        return Quick("tool", tool="system_status")

    # ---- maths and conversions (answered here, no tool needed)
    m = CONVERT.fullmatch(low)
    if m:
        try:
            value = float(m.group(2).replace(",", ""))
            out = convert(value, m.group(3), m.group(4))
            return Quick("answer", f"{fmt_num(value)} {m.group(3)} is {fmt_num(round(out, 4))} {m.group(4)}.")
        except ValueError:
            pass
    if re.search(r"\d", low) and re.search(r"[-+*/^%×÷]|\b(?:plus|minus|times|divided|squared|square\s+root|sqrt|percent)\b", low) \
            and re.fullmatch(r"(?:what(?:'s| is)|calculate|compute|how much is)?\s*[\d\s.,+\-*/^%()×÷a-z]+\??", low) \
            and len(re.findall(r"[a-z]{3,}", low)) <= 4:
        try:
            v = calc(low)
            expr = re.sub(r"^(?:what(?:'s| is)|calculate|compute|how much is)\s*", "", t, flags=re.I).rstrip("?")
            return Quick("answer", f"{expr} = {fmt_num(v)}")
        except (ValueError, ZeroDivisionError, SyntaxError, OverflowError, TypeError):
            pass
    return None
