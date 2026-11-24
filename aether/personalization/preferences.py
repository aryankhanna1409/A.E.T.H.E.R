"""Preference learning: how Aryan wants AETHER to talk.

- Explicit feedback ("stop giving me long introductions") is strong evidence
  and becomes an active, user-stated preference immediately.
- Inferred signals (e.g. the user keeps asking "shorter?") only raise a
  candidate's confidence; a candidate becomes active above a threshold, and
  can be inspected, edited or deleted at any time.
- Active preferences are applied twice: as instructions in the prompt, and as
  deterministic post-filters wherever a rule can enforce them in code.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass

# Canonical preference keys with the phrases that signal them and how to apply them.
CATALOG: dict[str, dict] = {
    "no_long_intros": {
        "patterns": [r"(long|lengthy) intro", r"get to the point", r"skip the (intro|preamble)",
                     r"no (intro|preamble)", r"stop (with )?the (intro|preamble)"],
        "instruction": "Start with the answer. No introductions, no restating the question.",
    },
    "concise": {
        "patterns": [r"\b(too long|shorter|be brief|be concise|less verbose|tl;?dr|wall of text)\b"],
        "instruction": "Keep replies short: a few sentences unless detail is requested.",
    },
    "detailed": {
        "patterns": [r"\b(more detail|go deeper|elaborate|explain more|in depth)\b"],
        "instruction": "Give thorough, detailed explanations.",
    },
    "no_disclaimers": {
        "patterns": [r"disclaimer", r"stop (warning|hedging)", r"\bcaveats?\b"],
        "instruction": "Do not add disclaimers or hedging boilerplate.",
        "filter": "disclaimers",
    },
    "no_emojis": {
        "patterns": [r"emojis?"],
        "instruction": "Never use emojis.",
        "filter": "emojis",
    },
    "no_bullets": {
        "patterns": [r"bullet ?points?", r"(that|this) format", r"\blists?\b"],
        "instruction": "Write in plain prose paragraphs, not bullet lists or headings.",
        "filter": "bullets",
    },
    "no_followup_questions": {
        "patterns": [r"stop asking (me )?(follow[- ]?up )?questions", r"don'?t ask (me )?if"],
        "instruction": "Do not end replies with follow-up questions or offers.",
        "filter": "trailing_offer",
    },
    "no_filler": {
        "patterns": [r"great question", r"\bfiller\b", r"sycophan", r"stop (praising|flattering)"],
        "instruction": "No filler or flattery like 'Great question!'.",
        "filter": "filler",
    },
    "technical": {
        "patterns": [r"\b(more technical|technical depth|don'?t dumb it down)\b"],
        "instruction": "Use technical depth and precise terminology.",
    },
    "simple": {
        "patterns": [r"\b(simpler|eli5|plain english|too technical)\b"],
        "instruction": "Explain simply, in plain language.",
    },
    "casual": {
        "patterns": [r"\b(more casual|less formal|chill)\b"],
        "instruction": "Use a casual, conversational tone.",
    },
    "formal": {
        "patterns": [r"\b(more formal|professional tone)\b"],
        "instruction": "Use a formal, professional tone.",
    },
}
OPPOSITES = {"concise": "detailed", "detailed": "concise", "technical": "simple", "simple": "technical",
             "casual": "formal", "formal": "casual"}

NEGATIVE_CUE = re.compile(r"\b(stop|don'?t|do not|never|hate|dislike|annoy|can'?t stand|no more|quit|enough with)\b", re.I)
POSITIVE_CUE = re.compile(r"\b(prefer|i like|i love|please (always|be)|keep it|i want|be more|make it)\b", re.I)
AIMED_AT_ME = re.compile(r"\b(you|your|reply|replies|response|responses|answers?|explain|format|intro)", re.I)
CUSTOM_BAN = re.compile(r"""(?:stop|don'?t|never) (?:saying|say|use|using) ["'“](.+?)["'”]""", re.I)

ACTIVATE_AT = 0.7
EXPLICIT_WEIGHT = 0.9
INFERRED_WEIGHT = 0.2

EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐✅]")
FILTERS = {
    "emojis": lambda s: EMOJI.sub("", s),
    "disclaimers": lambda s: re.sub(r"(?im)^\s*(?:\*\*)?(?:note|disclaimer|please note|important)(?:\*\*)?:.*$\n?", "", s),
    "filler": lambda s: re.sub(r"(?i)^\s*(great|good|excellent) question[!.]?\s*|^\s*(certainly|absolutely|of course|sure thing)[!.,]\s*", "", s),
    "trailing_offer": lambda s: re.sub(r"(?is)\n+[^\n]*\b(let me know|would you like|do you want me|shall i|feel free)\b[^\n]*\?*\s*$", "", s),
    "bullets": lambda s: re.sub(r"(?m)^\s*(?:[-*•]|\d+\.)\s+", "", re.sub(r"(?m)^#+\s*", "", s)),
}


@dataclass
class Preference:
    key: str
    instruction: str
    confidence: float
    source: str  # "user_stated" | "inferred" | "user_edited"
    evidence: list[str]
    active: bool
    custom_phrase: str | None = None


class PreferenceModel:
    def __init__(self, db: sqlite3.Connection, lock: threading.Lock):
        self.db, self.lock = db, lock
        with lock:
            db.execute("""CREATE TABLE IF NOT EXISTS preferences (
                key TEXT PRIMARY KEY, instruction TEXT, confidence REAL, source TEXT,
                evidence TEXT, active INTEGER, custom_phrase TEXT, updated REAL)""")
            db.commit()

    # ---------------------------------------------------------------- learning
    def learn(self, text: str) -> list[str]:
        """Detect feedback about AETHER's style. Returns human-readable updates."""
        updates: list[str] = []
        if not AIMED_AT_ME.search(text):
            return updates
        explicit = bool(NEGATIVE_CUE.search(text) or POSITIVE_CUE.search(text))
        for m in CUSTOM_BAN.finditer(text):
            phrase = m.group(1).strip()
            key = "ban:" + phrase.lower()
            self._upsert(Preference(key, f'Never say "{phrase}".', 1.0, "user_stated", [text[:200]], True, phrase))
            updates.append(f'will never say "{phrase}"')
        for key, spec in CATALOG.items():
            if any(re.search(p, text, re.I) for p in spec["patterns"]):
                # "no_*" keys need a negative cue; style keys need either cue to count as explicit.
                if key.startswith("no_") and not NEGATIVE_CUE.search(text):
                    continue
                weight = EXPLICIT_WEIGHT if explicit else INFERRED_WEIGHT
                pref = self.reinforce(key, weight, "user_stated" if explicit else "inferred", text)
                if pref.active:
                    updates.append(pref.instruction)
        return updates

    def reinforce(self, key: str, weight: float, source: str, evidence: str) -> Preference:
        current = self.get(key)
        conf = min(1.0, (current.confidence if current else 0.0) + weight)
        ev = ((current.evidence if current else []) + [evidence[:200]])[-5:]
        src = "user_stated" if source == "user_stated" or (current and current.source == "user_stated") else source
        pref = Preference(key, CATALOG[key]["instruction"], conf, src, ev, conf >= ACTIVATE_AT)
        self._upsert(pref)
        if pref.active and key in OPPOSITES:  # an explicit new style overrides its opposite
            opp = self.get(OPPOSITES[key])
            if opp and opp.active:
                self.set_active(opp.key, False)
        return pref

    # ---------------------------------------------------------------- management (UI/CLI)
    def all(self) -> list[Preference]:
        rows = self.db.execute("SELECT * FROM preferences ORDER BY active DESC, confidence DESC").fetchall()
        return [self._row(r) for r in rows]

    def get(self, key: str) -> Preference | None:
        r = self.db.execute("SELECT * FROM preferences WHERE key=?", (key,)).fetchone()
        return self._row(r) if r else None

    def set_active(self, key: str, active: bool) -> None:
        with self.lock:
            self.db.execute("UPDATE preferences SET active=?, updated=? WHERE key=?", (int(active), time.time(), key))
            self.db.commit()

    def edit(self, key: str, instruction: str) -> None:
        with self.lock:
            self.db.execute("UPDATE preferences SET instruction=?, source='user_edited', confidence=1.0, active=1 "
                            "WHERE key=?", (instruction, key))
            self.db.commit()

    def add_custom(self, instruction: str) -> str:
        key = "custom:" + re.sub(r"\W+", "_", instruction.lower())[:40]
        self._upsert(Preference(key, instruction, 1.0, "user_stated", ["added manually"], True))
        return key

    def delete(self, key: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM preferences WHERE key=?", (key,))
            self.db.commit()

    # ---------------------------------------------------------------- applying
    def prompt_block(self) -> str:
        active = [p for p in self.all() if p.active]
        if not active:
            return ""
        return "User's communication preferences (follow strictly):\n" + "\n".join(f"- {p.instruction}" for p in active)

    def apply_filters(self, text: str) -> str:
        for p in self.all():
            if not p.active:
                continue
            if p.custom_phrase:
                text = re.sub(re.escape(p.custom_phrase) + r"[,!.]?\s*", "", text, flags=re.I)
            fname = CATALOG.get(p.key, {}).get("filter")
            if fname:
                text = FILTERS[fname](text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    # ---------------------------------------------------------------- internals
    def _upsert(self, p: Preference) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO preferences VALUES (?,?,?,?,?,?,?,?)",
                            (p.key, p.instruction, p.confidence, p.source, json.dumps(p.evidence),
                             int(p.active), p.custom_phrase, time.time()))
            self.db.commit()

    @staticmethod
    def _row(r) -> Preference:
        return Preference(r["key"], r["instruction"], r["confidence"], r["source"],
                          json.loads(r["evidence"] or "[]"), bool(r["active"]), r["custom_phrase"])
