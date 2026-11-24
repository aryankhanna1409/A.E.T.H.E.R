"""Selective memory extraction after a conversation turn.

The local LLM proposes what is worth keeping (facts, reminders, routines);
code decides: only items above an importance threshold are stored, facts go
through the knowledge graph's contradiction check, and nothing is stored for
small talk. Psychological readings are never extracted as facts.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from ..cognition.ir import FactTriple
from ..llm.base import LLMProvider, Message, structured

WORTH_CHECKING = re.compile(
    r"\b(i|i'm|i've|my|me|remind|remember|always|every (day|week|morning)|deadline|due|tomorrow|next week|"
    r"birthday|appointment|meeting|exam|project|routine|usually)\b", re.I)


class Reminder(BaseModel):
    text: str
    when: str = Field(description="time expression as said, e.g. 'tomorrow', 'friday', '2026-12-01'")


class Routine(BaseModel):
    name: str
    steps: list[str]


class Extraction(BaseModel):
    facts: list[FactTriple] = Field(default_factory=list,
                                    description="durable facts the USER stated about themselves or their world")
    reminders: list[Reminder] = Field(default_factory=list, description="things the user wants to do or be reminded of")
    routines: list[Routine] = Field(default_factory=list, description="repeatable workflows the user described")
    importance: float = Field(0.0, ge=0, le=1, description="0 = small talk, 1 = clearly important to remember")
    kind: Literal["small_talk", "personal", "task", "knowledge", "mixed"] = "small_talk"


PROMPT = """Decide what from this exchange is worth remembering long-term about the user.
Only extract what the USER said (not the assistant). Do not infer personality traits or diagnoses.
Return empty lists and importance 0 for small talk."""


def should_extract(user_text: str) -> bool:
    return len(user_text.split()) >= 4 and bool(WORTH_CHECKING.search(user_text))


def extract(llm: LLMProvider, user_text: str, assistant_text: str) -> Extraction:
    return structured(llm, [Message("system", PROMPT),
                            Message("user", f"USER: {user_text}\nASSISTANT: {assistant_text[:800]}")],
                      Extraction, max_tokens=500)
