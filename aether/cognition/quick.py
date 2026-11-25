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

WAKE = re.compile(r"^(?:(?:hey|ok|okay)\s+)?(?:jarvis|aether)[\s,.!]+", re.I)
POLITE = re.compile(r"^(?:(?:can|could|would)\s+you\s+|please\s+)|\s*(?:,?\s*please|,?\s*thanks?(?:\s+you)?|,?\s*(?:jarvis|aether|sir))\s*[.!?]*$", re.I)


@dataclass
class Quick:
    kind: str  # answer | tool | copy_last
    text: str = ""
    tool: str = ""
    args: dict = field(default_factory=dict)


def _clean(text: str) -> str:
    t = WAKE.sub("", text.strip())
    for _ in range(3):
        t = POLITE.sub("", t).strip()
    return t.rstrip(".!").strip()


def match(text: str, now: datetime, apps: set[str] | None = None) -> Quick | None:
    if not text or len(text) > 120:
        return None
    t = _clean(text)
    low = t.lower()
    apps = {a.lower() for a in (apps or set())}

    # ---- clock and calendar facts
    if re.fullmatch(r"(?:what(?:'s| is)\s+)?(?:the\s+)?time(?:\s+is\s+it)?(?:\s+now)?\??|what\s+time\s+is\s+it(?:\s+now)?\??", low):
        return Quick("answer", f"It's {now:%H:%M}.")
    if re.fullmatch(r"(?:what(?:'s| is)\s+)?(?:the\s+|today'?s\s+)?date(?:\s+today)?\??|what\s+day\s+is\s+(?:it|today)\??", low):
        return Quick("answer", f"It's {now:%A %d %B %Y}.")

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
    m = re.fullmatch(r"(?:open|launch|start|go\s+to)\s+(?:up\s+)?(.+)", low)
    if m:
        target = m.group(1).strip().removeprefix("the ").strip()
        if target in apps:
            return Quick("tool", tool="app_open", args={"name": target})
        if target in SITES or re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", target):
            return Quick("tool", tool="open_website", args={"site": target})

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
