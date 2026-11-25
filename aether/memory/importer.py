"""Import memories exported from other assistants (ChatGPT, Claude, Gemini, ...) or written by hand.

Nothing is saved blindly: `parse` turns an export into a list of candidate facts and preferences, each one
de-duplicated and checked for prompt injection, and the user picks which ones to keep before `save` writes
them into AETHER's encrypted memory.

Understood formats:
- plain text or Markdown, one memory per line or bullet (ChatGPT "Manage memories", Claude and Gemini
  memory/"saved info" text, notes written by hand); long paragraphs are split into sentences
- JSON: a list of strings, a list of objects with text/content/memory/value, or {"memories": [...]}
- ChatGPT's data export `conversations.json`: only the things you said about yourself ("I'm ...", "my ...",
  "I prefer ...") are proposed, never the assistant's replies
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from ..security.injection import scan

MAX_LEN = 400
PREF = re.compile(r"\b(prefers?|preferred|likes?|loves?|enjoys?|hates?|dislikes?|wants?|would rather|rather than|"
                  r"always|never|don'?t|do not|avoid|please|tone|style|format|concise|brief|detailed|"
                  r"favou?rite|call (?:me|him|her|them))\b", re.I)
SELF = re.compile(r"^(?:i am|i'm|im|i live|i work|i study|i use|i have|i own|i prefer|i like|i love|i hate|i don'?t like|"
                  r"i want|i'd rather|i usually|i always|i never|my name|my [a-z]+ (?:is|are)|call me)\b", re.I)
SKIP = re.compile(r"^(?:#|```|---|===|\*\*\*|>|\|)|^(?:memories?|saved info|notes?|about me|preferences?|facts?)\s*:?\s*$",
                  re.I)
BULLET = re.compile(r"^\s*(?:[-*+•·]|\d+[.)]|\[[ x]\])\s+")


@dataclass
class Candidate:
    text: str
    kind: str                 # "fact" or "preference"
    flags: list[str] = field(default_factory=list)   # injection patterns found: unticked by default
    existing: bool = False    # AETHER already remembers this
    keep: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", s.lower()).split())


def _clean(line: str) -> str:
    line = BULLET.sub("", line.strip())
    line = re.sub(r"\*\*|__|`", "", line).strip()
    return line.strip(" \t-•")


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    return [p.strip() for p in parts if p.strip()]


def _from_text(text: str) -> list[str]:
    out = []
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)       # notes to the reader, not memories
    text = re.sub(r"```.*?```", "", text, flags=re.S)        # code blocks
    for raw in text.splitlines():
        if raw.startswith(("    ", "\t")) and not BULLET.match(raw):
            continue                                         # indented code / commands
        if not raw.strip() or SKIP.search(raw.strip()):
            continue
        line = _clean(raw)
        if not line:
            continue
        out += _sentences(line) if len(line) > 160 else [line]
    return out


def _from_json(data) -> list[str]:
    if isinstance(data, dict):
        for key in ("memories", "memory", "items", "facts", "saved_info", "data"):
            if key in data:
                return _from_json(data[key])
        if "mapping" in data:          # a single ChatGPT conversation
            return _from_chatgpt([data])
        return [str(v) for v in data.values() if isinstance(v, str)]
    if isinstance(data, list):
        if data and isinstance(data[0], dict) and "mapping" in data[0]:
            return _from_chatgpt(data)
        out = []
        for item in data:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content", "memory", "value", "fact"):
                    if isinstance(item.get(key), str):
                        out.append(item[key])
                        break
        return [x for s in out for x in _from_text(s)]
    return []


def _from_chatgpt(conversations: list[dict]) -> list[str]:
    """Only what the user said about themselves, from ChatGPT's conversations.json."""
    out = []
    for conv in conversations:
        for node in (conv.get("mapping") or {}).values():
            msg = (node or {}).get("message") or {}
            if (msg.get("author") or {}).get("role") != "user":
                continue
            parts = (msg.get("content") or {}).get("parts") or []
            for part in parts:
                if not isinstance(part, str):
                    continue
                for s in _sentences(part.replace("\n", " ")):
                    if SELF.match(s) and 3 <= len(s.split()) <= 40:
                        out.append(s)
    return out


def parse(text: str, filename: str = "") -> list[Candidate]:
    text = text.lstrip("﻿")
    lines: list[str]
    if filename.lower().endswith(".json") or text.lstrip()[:1] in "[{":
        try:
            lines = _from_json(json.loads(text))
        except ValueError:
            lines = _from_text(text)
    else:
        lines = _from_text(text)
    seen: set[str] = set()
    out = []
    for line in lines:
        line = " ".join(line.split())
        if len(line.split()) < 3 or len(line) > MAX_LEN * 3:
            continue
        line = line[:MAX_LEN]
        key = _norm(line)
        if not key or key in seen:
            continue
        seen.add(key)
        flags = scan(line)
        out.append(Candidate(line, "preference" if PREF.search(line) else "fact", flags, keep=not flags))
    return out


def mark_existing(candidates: list[Candidate], store) -> list[Candidate]:
    known = {_norm(m.text) for m in store.list("semantic", 5000)}
    for c in candidates:
        if _norm(c.text) in known:
            c.existing, c.keep = True, False
    return candidates


def save(aether, items: list[dict | Candidate], label: str = "import") -> dict[str, int]:
    """Save the chosen items as user-approved long-term memories (the semantic layer, which every answer
    searches), encrypted at rest like every other memory. The kind is kept in the memory's metadata."""
    saved = skipped = 0
    for it in items:
        c = it if isinstance(it, Candidate) else Candidate(str(it.get("text", "")), str(it.get("kind", "fact")))
        text = " ".join(c.text.split())[:MAX_LEN]
        if len(text.split()) < 2 or scan(text):
            skipped += 1
            continue
        kind = "preference" if c.kind == "preference" else "fact"
        aether.store.add(text, "semantic", "user_approved", confidence=0.9,
                         meta={"imported_from": label[:80], "kind": kind})
        saved += 1
    return {"saved": saved, "skipped": skipped}
