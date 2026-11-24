"""Structured intermediate representation (IR) of a request.

The neural side (local LLM) fills this in from free text; the symbolic side
validates and enriches it before anything is executed.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Need = Literal["direct", "memory", "documents", "web", "symbolic", "tools", "optimize"]
Tone = Literal["neutral", "positive", "frustrated", "stressed", "sad", "angry", "curious", "urgent"]


class Entity(BaseModel):
    name: str
    type: str = Field(description="person, place, org, project, file, date, concept, ...")


class FactTriple(BaseModel):
    subject: str = Field(description="use 'user' for the speaker")
    predicate: str = Field(description="snake_case relation, e.g. lives_in, works_at, likes, deadline")
    object: str


class ToolCall(BaseModel):
    tool: str
    args: dict[str, str] = Field(default_factory=dict)
    why: str = ""


class IntentIR(BaseModel):
    intent: str = Field(description="short snake_case label, e.g. question, task, chit_chat, feedback, venting")
    objective: str = Field(description="what the user ultimately wants, one line")
    entities: list[Entity] = Field(default_factory=list)
    constraints: dict[str, str] = Field(default_factory=dict)
    time_expressions: list[str] = Field(default_factory=list)
    facts_stated: list[FactTriple] = Field(default_factory=list,
                                           description="personal facts the user just stated about themselves or their world")
    needs: list[Need] = Field(default_factory=list)
    tools_required: list[ToolCall] = Field(default_factory=list)
    requires_confirmation: bool = False
    emotional_tone: Tone = "neutral"
    ambiguity: str | None = Field(default=None, description="what is unclear, if anything blocks answering")
    confidence: float = Field(default=0.7, ge=0, le=1)


class CognitiveState(BaseModel):
    """Inspectable task state (shown in the UI's 'mind' panel)."""
    current_goal: str = ""
    active_topic: str = ""
    route: list[str] = Field(default_factory=list)
    known_facts: list[str] = Field(default_factory=list)
    uncertain_hypotheses: list[str] = Field(default_factory=list)
    active_constraints: dict[str, str] = Field(default_factory=dict)
    pending_tasks: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    tool_log: list[str] = Field(default_factory=list)
    wellness: str = "normal"
    confidence: float = 0.0
    reasoning_mode: str = "neuro_symbolic"
