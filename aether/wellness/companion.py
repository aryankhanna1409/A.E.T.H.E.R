"""Wellness companion: supportive conversation and stress relief, not therapy.

Each message is classified as normal / stressed / crisis. Crisis handling is
deterministic code, so it never depends on what the model decides. In stressed
moments the companion picks a conversational mode, and when it cannot tell
whether Aryan wants to vent or to fix things, it asks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CRISIS_PATTERNS = [
    r"\bkill (my ?self|me)\b", r"\bsuicid", r"\bend (it all|my life)\b", r"\bwant to die\b",
    r"\bself[- ]?harm", r"\bcut(ting)? myself\b", r"\bno reason to live\b", r"\bbetter off without me\b",
    r"\bdon'?t want to (be here|exist|live)\b", r"\bhurt myself\b",
]
STRESS_PATTERNS = [
    r"\bstress(ed|ful)?\b", r"\banxi(ous|ety)\b", r"\boverwhelm", r"\bburn(ed|t)? ?out\b",
    r"\bpanic", r"\bcan'?t (sleep|cope|focus)\b", r"\bexhausted\b", r"\bsad\b", r"\bdepress",
    r"\blonely\b", r"\bfrustrat", r"\bworried\b", r"\bhopeless\b", r"\bcrying\b",
]

CRISIS_RESOURCES = (
    "If you are in danger or thinking about ending your life, please reach out right now:\n"
    "- India: Tele MANAS 14416 or 1-800-891-4416 (24/7), or KIRAN 1800-599-0019\n"
    "- US: call or text 988 (Suicide & Crisis Lifeline)\n"
    "- UK and Ireland: Samaritans 116 123\n"
    "- Anywhere: findahelpline.com lists free local lines\n"
    "- Emergency: call your local emergency number (112 in India and the EU, 911 in the US)"
)

WELLBEING_STYLE = (
    "The user seems stressed or low right now. Switch to supportive mode: slow down, "
    "be warm and brief, reflect back what they said, and ask one gentle question at a time. "
    "Offer a concrete grounding tool only if it fits (box breathing 4-4-4-4, 5-4-3-2-1 senses, "
    "a two-minute walk, writing the worry down). Do not diagnose, do not lecture, and do not "
    "claim to be a therapist. If things sound persistent or heavy, gently suggest talking to "
    "someone they trust or a professional."
)

CRISIS_STYLE = (
    "SAFETY OVERRIDE: the user may be at risk of self-harm. Respond with care and without "
    "judgement. Acknowledge their pain, say you are glad they told you, ask if they are safe "
    "right now, and encourage them to contact a crisis line or someone they trust now. Include "
    "these resources verbatim:\n" + CRISIS_RESOURCES + "\nDo not give methods or any information "
    "that could be used for self-harm. Do not end the conversation abruptly."
)


@dataclass
class WellbeingSignal:
    level: str  # "normal" | "stressed" | "crisis"
    matches: list[str]

    @property
    def system_addendum(self) -> str:
        return {"crisis": CRISIS_STYLE, "stressed": WELLBEING_STYLE}.get(self.level, "")


def assess(text: str) -> WellbeingSignal:
    lowered = text.lower()
    crisis = [p for p in CRISIS_PATTERNS if re.search(p, lowered)]
    if crisis:
        return WellbeingSignal("crisis", crisis)
    stress = [p for p in STRESS_PATTERNS if re.search(p, lowered)]
    return WellbeingSignal("stressed" if stress else "normal", stress)


BREATHING = (
    "Let's do one round of box breathing together:\n"
    "1. Breathe in through your nose for 4 seconds.\n"
    "2. Hold for 4.\n3. Breathe out slowly for 4.\n4. Hold for 4.\n"
    "Repeat 4 times. I'm here when you're done."
)


MODES = {
    "listening": "Listen. Reflect back what you heard in one or two sentences. Do not give advice or lists.",
    "reflection": "Help them reflect: ask one open question that helps them see the situation more clearly.",
    "problem_solving": "Help solve it: identify the one most important next step, then at most two more.",
    "motivation": "Encourage them genuinely and specifically, without toxic positivity.",
    "relaxation": "Guide a short calming exercise step by step (breathing or 5-4-3-2-1 grounding).",
    "planning": "Help turn the overwhelm into a small plan: three items max, smallest first step first.",
}
MODE_CUES = {
    "listening": r"\b(just (need|want) to (vent|talk)|just listen|let me vent|need to rant)\b",
    "problem_solving": r"\b(how do i|help me (fix|solve|figure)|what should i do|any advice)\b",
    "relaxation": r"\b(calm down|breath|relax|panic|can'?t breathe)\b",
    "planning": r"\b(too much to do|overwhelmed|so many (tasks|things)|prioriti[sz]e|deadline)\b",
    "motivation": r"\b(unmotivated|no motivation|can'?t get started|procrastinat|give up)\b",
    "reflection": r"\b(why do i|keep (doing|feeling)|pattern|always end up)\b",
}
ASK_MODE = ("Do you want me to just listen for a bit, or would it help to sort through it together "
            "and make a plan?")


def pick_mode(text: str) -> str | None:
    lowered = text.lower()
    for mode, pat in MODE_CUES.items():
        if re.search(pat, lowered):
            return mode
    return None


def companion_instructions(signal: WellbeingSignal, text: str, sticky_mode: str | None) -> tuple[str, str | None]:
    """Returns (system addendum, chosen mode)."""
    if signal.level == "crisis":
        return CRISIS_STYLE, "crisis"
    if signal.level != "stressed" and not sticky_mode:
        return "", None
    mode = pick_mode(text) or sticky_mode
    parts = [WELLBEING_STYLE]
    if mode:
        parts.append(f"Mode: {mode}. {MODES[mode]}")
    else:
        parts.append("Their preference is unclear: respond briefly and warmly, then ask exactly this: " + ASK_MODE)
    if mode == "planning":
        parts.append("Do NOT dump a long list on them. Offer to prioritise together.")
    return "\n".join(parts), mode


def ensure_resources(reply: str, signal: WellbeingSignal) -> str:
    """Guarantee crisis resources are shown even if the model left them out."""
    if signal.level == "crisis" and "14416" not in reply and "988" not in reply:
        reply = reply.rstrip() + "\n\n" + CRISIS_RESOURCES
    return reply
