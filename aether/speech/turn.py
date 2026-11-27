"""End of turn: reply when you've actually finished, not at the first pause.

A fixed "900 ms of silence" either cuts you off mid-thought ("set a timer for... um... ten minutes") or makes every
reply feel slow. AETHER reads the live partial transcript instead and picks how long a pause has to be:

* a finished request ("what's the time", "open spotify", anything ending in a question mark) ends after ~350 ms,
* a sentence that trails off on a filler or a joining word ("um", "and", "the", "to", "because") waits ~1.6 s,
* anything else uses the configured silence.

It's a tiny scoring model over words people actually trail off on, with no extra download and no GPU.
"""
from __future__ import annotations

import re

HOLD = {"and", "or", "but", "so", "because", "cause", "the", "a", "an", "to", "of", "for", "with", "in", "on", "at",
        "my", "your", "about", "from", "into", "if", "when", "that", "which", "who", "is", "are", "was", "what's",
        "um", "uh", "er", "erm", "hmm", "like", "maybe", "then", "also", "set", "add", "remind", "open", "play",
        "search", "between", "than", "as", "by", "it's", "its", "i", "we", "you", "me", "can", "could", "would",
        "should", "will", "please,", "let's", "for,", "uh,", "um,"}
COMPLETE_START = re.compile(
    r"^(?:what(?:'s| is| are)?|who|when|where|why|how|is|are|do|does|did|can|could|will|would|should|open|close|"
    r"play|pause|stop|start|set|turn|mute|unmute|remind|add|show|tell|read|call|search|google|take|plan|thanks?|"
    r"thank you|good (?:morning|night|evening|afternoon)|status report|run diagnostics|yes|no|yeah|nope|okay|ok|"
    r"sure|cancel|never ?mind)\b", re.I)
SHORT_COMMAND = re.compile(r"^(?:yes|no|yeah|yep|nope|okay|ok|sure|thanks?|thank you|cancel|stop|pause|next|"
                           r"never ?mind|go ahead|do it|mute|unmute)[.!]?$", re.I)


def finished(text: str) -> float:
    """How finished the partial transcript sounds, from 0 (clearly mid-sentence) to 1 (clearly done)."""
    t = text.strip()
    if not t:
        return 0.5
    if SHORT_COMMAND.match(t):
        return 1.0
    words = re.findall(r"[\w']+[,]?", t.lower())
    last = words[-1] if words else ""
    score = 0.5
    if t.endswith("?"):
        score += 0.4
    elif t.endswith((".", "!")):
        score += 0.15        # Whisper adds full stops generously, so this is weak evidence
    if t.endswith((",", "-", "…", "...")):
        score -= 0.35
    if last.rstrip(",") in HOLD:
        score -= 0.45
    if COMPLETE_START.match(t) and len(words) >= 2 and last.rstrip(",") not in HOLD:
        score += 0.35 if len(words) <= 5 else 0.15     # short commands ("open spotify") are usually complete
    if len(words) <= 1:
        score -= 0.1
    return max(0.0, min(1.0, score))


def silence_for(text: str, base_ms: int = 900, quick_ms: int = 350, hold_ms: int = 1600) -> int:
    """Milliseconds of silence that should end this turn, given what's been said so far."""
    f = finished(text)
    quick_ms = min(quick_ms, base_ms)          # never slower than what you configured for a finished request
    if f >= 0.85:
        return quick_ms
    if f <= 0.2:
        return hold_ms
    if f >= 0.65:
        return int(quick_ms + (base_ms - quick_ms) * (0.85 - f) / 0.2)
    return base_ms
