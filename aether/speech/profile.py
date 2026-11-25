"""Your speech profile: how AETHER learns the way you speak.

Speech-to-text models are trained mostly on American and British English, so they trip on Indian-accented
names and words ("Aundh" -> "on", "Shewate" -> "she wait"). Three things fix most of it, all local:

1. A prompt and hotwords: Whisper is told up front which names and terms to expect (your name, the assistant's
   name, places and terms from your memories, plus anything learnt below).
2. A personal correction dictionary: phrases the model keeps mishearing are rewritten to what you meant.
3. Learning: from the "learn my voice" calibration (scripts/learn_my_voice.py), and from every time you fix a
   voice transcript by typing what you actually said.

Everything lives in speech_profile.json in AETHER's data folder and can be edited by hand.
"""
from __future__ import annotations

import difflib
import json
import re
import threading
from collections import Counter
from pathlib import Path

ASSISTANT_NAME = "Nova"
# Indian-English context that is not personal: Whisper spells these right far more often when it expects them.
BASE_TERMS = ["Nova", "AETHER", "Jarvis", "Pune", "Mumbai", "Bengaluru", "Delhi", "Hyderabad", "Chennai",
              "Swiggy", "Zomato", "Ola", "Uber", "UPI", "rupees", "lakh", "crore", "paneer", "dosa", "chai",
              "YouTube", "WhatsApp", "GitHub", "Python", "VS Code", "Raspberry Pi", "ROS", "YOLO"]
WORD = re.compile(r"[\w'.-]+")
COMMON = {"the", "a", "an", "and", "or", "i", "my", "me", "to", "of", "in", "on", "at", "for", "is", "it", "be",
          "this", "that", "with", "you", "your", "what", "set", "open", "play", "please", "hey", "hi", "ok", "okay"}


def words(text: str) -> list[str]:
    return WORD.findall(text or "")


def _norm(w: str) -> str:
    return w.lower().strip(".,!?;:'\"")


class SpeechProfile:
    def __init__(self, path: Path | None = None, user_name: str = "", extra_terms: list[str] | None = None):
        self.path = Path(path) if path else None
        self.user_name = user_name
        self.extra_terms = list(extra_terms or [])
        self.hotwords: list[str] = []
        self.replacements: dict[str, str] = {}
        self.pending: Counter = Counter()          # corrections seen once from chat; learnt on the second time
        self.lock = threading.Lock()
        self._load()

    @classmethod
    def for_home(cls, home: Path, user_name: str = "", extra_terms: list[str] | None = None) -> "SpeechProfile":
        return cls(Path(home) / "speech_profile.json", user_name, extra_terms)

    # ------------------------------------------------------------------ storage
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.hotwords = [str(w) for w in data.get("hotwords", [])][:200]
        self.replacements = {str(k).lower(): str(v) for k, v in data.get("replacements", {}).items()}
        self.pending = Counter({str(k): int(v) for k, v in data.get("pending", {}).items()})

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"hotwords": self.hotwords, "replacements": self.replacements, "pending": dict(self.pending)}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------ using it
    def terms(self, limit: int = 60) -> list[str]:
        out = ([self.user_name] if self.user_name else []) + self.hotwords + self.extra_terms + BASE_TERMS
        return list(dict.fromkeys(t for t in out if t))[:limit]

    def prompt(self) -> str:
        """Whisper's initial prompt: a natural sentence that contains the words to expect."""
        who = f"{self.user_name} speaking" if self.user_name else "Speaking"
        return f"{who} in Indian English to {ASSISTANT_NAME}, the AETHER assistant. Names and terms: " + \
            ", ".join(self.terms(45)) + "."

    def hotword_string(self) -> str:
        return " ".join(self.terms(40))

    def apply(self, text: str) -> str:
        """Rewrite phrases this profile knows are mis-heard. Longest first, whole words only."""
        if not text or not self.replacements:
            return text
        for wrong in sorted(self.replacements, key=len, reverse=True):
            text = re.sub(r"(?<![\w'])" + re.escape(wrong) + r"(?![\w'])", self.replacements[wrong], text,
                          flags=re.I)
        return text

    # ------------------------------------------------------------------ learning
    def learn(self, heard: str, meant: str, confirmed: bool = True) -> list[tuple[str, str]]:
        """Compare what was transcribed with what was meant and remember the differences.

        confirmed=True (calibration, or an explicit correction): learnt at once.
        confirmed=False (a typed message that looks like a fix of the last transcript): learnt the second time."""
        hw, mw = words(heard), words(meant)
        if not hw or not mw:
            return []
        learnt = []
        sm = difflib.SequenceMatcher(a=[_norm(w) for w in hw], b=[_norm(w) for w in mw], autojunk=False)
        with self.lock:
            for op, i1, i2, j1, j2 in sm.get_opcodes():
                if op != "replace" or i2 - i1 > 3 or j2 - j1 > 3:
                    continue
                wrong = " ".join(_norm(w) for w in hw[i1:i2])
                right = " ".join(w.strip(".,!?;:\"") for w in mw[j1:j2])
                if not wrong or not right or wrong == right.lower() or len(wrong) < 2:
                    continue
                if all(w in COMMON for w in wrong.split()) and len(wrong.split()) == 1 and right.lower() in COMMON:
                    continue      # "a" vs "the": grammar noise, not your accent
                key = f"{wrong}=>{right}"
                if not confirmed:
                    self.pending[key] += 1
                    if self.pending[key] < 2:
                        continue
                    del self.pending[key]
                self.replacements[wrong] = right
                learnt.append((wrong, right))
                for w in right.split():
                    if w[:1].isupper() or any(c.isdigit() for c in w) or _norm(w) not in COMMON and len(w) > 5:
                        if w not in self.hotwords:
                            self.hotwords.insert(0, w)
            self.hotwords = self.hotwords[:200]
            if learnt or not confirmed:
                self.save()
        return learnt

    def add_terms(self, terms: list[str]) -> None:
        with self.lock:
            for t in terms:
                t = t.strip()
                if t and t not in self.hotwords:
                    self.hotwords.append(t)
            self.hotwords = self.hotwords[:200]
            self.save()

    def forget(self, wrong: str) -> bool:
        with self.lock:
            hit = self.replacements.pop(wrong.lower(), None) is not None
            self.save()
        return hit


def looks_like_correction(heard: str, typed: str) -> bool:
    """A typed message that is close to, but not the same as, the last voice transcript."""
    a, b = " ".join(_norm(w) for w in words(heard)), " ".join(_norm(w) for w in words(typed))
    if not a or not b or a == b:
        return False
    return difflib.SequenceMatcher(a=a, b=b).ratio() >= 0.6


WAKE = re.compile(r"\b(?:hey|hi|hay|he|okay|ok|a|yo)?[\s,]*(?:nova|nova|nowa|nava|nowah|novah|no\s?va)\b", re.I)


def is_wake_phrase(text: str, profile: SpeechProfile | None = None) -> bool:
    """True for a short utterance that calls the assistant by name ("hey Nova", "Nova")."""
    if profile is not None:
        text = profile.apply(text)
    t = text.strip().lower()
    return bool(t) and len(t.split()) <= 6 and bool(WAKE.search(t))


def terms_from_memories(store, limit: int = 40) -> list[str]:
    """Names and terms you use, taken from your long-term memories: capitalised words that aren't at the
    start of a sentence (Aundh, Incedo, Shewate ...), most frequent first."""
    counts: Counter = Counter()
    try:
        mems = store.list("semantic", 2000)
    except Exception:
        return []
    for m in mems:
        for sentence in re.split(r"(?<=[.!?])\s+", m.text):
            ws = words(sentence)
            for w in ws[1:]:
                w = w.strip(".,'")
                if len(w) > 2 and (w[:1].isupper() or w.isupper()) and _norm(w) not in COMMON:
                    counts[w] += 1
    return [w for w, _ in counts.most_common(limit)]
