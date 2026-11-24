"""Perception: turn a raw message into an IntentIR.

Neural path: the local LLM fills the IR schema (grammar-constrained JSON).
Symbolic enrichment: dates, wellbeing signal and explicit cues are added by
code, so they never depend on a small model getting them right.
"""
from __future__ import annotations

import re

from ..core.logging import log
from ..llm.base import LLMProvider, Message, structured
from ..neurosymbolic.temporal import find_expressions
from .ir import FactTriple, IntentIR

PERCEPTION_PROMPT = """You convert the user's message into a structured representation for an assistant's planner.
Rules:
- needs: pick what is required. "direct" = answer from general knowledge or conversation.
  "memory" = the user refers to things they told the assistant before, or to themselves.
  "documents" = their local files or notes. "web" = current events, prices, latest info, things to look up.
  "symbolic" = logic, relationships, consistency, schedules, dependencies. "tools" = an action on the computer.
  "optimize" = choosing the best plan or schedule among many options.
- tools_required: only from this list: {tools}.
- facts_stated: only facts the user states about themselves or their world (subject 'user' for the speaker).
- requires_confirmation: true for anything that changes files, sends something, or is irreversible.
- Do not invent entities. Keep objective to one line."""

WEB_CUES = re.compile(r"\b(latest|news|today'?s|current|price|weather|search|look up|google|research|recent|2025|2026)\b", re.I)
MEMORY_CUES = re.compile(r"\b(remember|last (time|week)|i told you|we discussed|you know (that|my)|my \w+)\b", re.I)
DOC_CUES = re.compile(r"\b(my (files?|notes?|documents?|folder|research folder|pdfs?)|in (the|my) (folder|docs?))\b", re.I)
FILE_ACTION = re.compile(r"\b(create|write|save|make) (a |the )?(file|document|note)\b", re.I)
SELF_FACT = re.compile(r"\bmy (\w+(?: \w+)?) is ([\w' -]{2,40}?)(?:[.,!]|$)|\bi (live|work) (?:in|at) ([\w' -]{2,40}?)(?:[.,!]|$)", re.I)


def heuristic_ir(text: str) -> IntentIR:
    """Rule-based perception used when no LLM is loaded or its output is invalid."""
    needs: list = []
    if WEB_CUES.search(text):
        needs.append("web")
    if MEMORY_CUES.search(text):
        needs.append("memory")
    if DOC_CUES.search(text):
        needs.append("documents")
    if FILE_ACTION.search(text):
        needs.append("tools")
    facts = []
    for m in SELF_FACT.finditer(text):
        if m.group(1):
            facts.append(FactTriple(subject="user", predicate=m.group(1).replace(" ", "_"), object=m.group(2).strip()))
        else:
            pred = "lives_in" if m.group(3).lower() == "live" else "works_at"
            facts.append(FactTriple(subject="user", predicate=pred, object=m.group(4).strip()))
    return IntentIR(intent="question" if text.strip().endswith("?") else "statement",
                    objective=text.strip()[:120], needs=needs or ["direct"], facts_stated=facts,
                    time_expressions=find_expressions(text), confidence=0.5)


def perceive(llm: LLMProvider | None, text: str, recent: list[tuple[str, str]], tools: list[str]) -> IntentIR:
    base = heuristic_ir(text)
    if llm is None:
        return base
    history = "\n".join(f"{r}: {t[:300]}" for r, t in recent[-4:])
    try:
        ir = structured(llm, [
            Message("system", PERCEPTION_PROMPT.format(tools=", ".join(tools))),
            Message("user", f"Recent conversation:\n{history or '(none)'}\n\nMessage to analyse:\n{text}")],
            IntentIR, max_tokens=700)
    except Exception as e:  # invalid JSON after retry, model error
        log.warning("Neural perception failed (%s); using rule-based IR.", e)
        return base
    # Symbolic enrichment: union of model judgement and deterministic cues.
    ir.time_expressions = sorted(set(ir.time_expressions) | set(base.time_expressions))
    for n in base.needs:
        if n != "direct" and n not in ir.needs:
            ir.needs.append(n)
    known = {(f.predicate, f.object.lower()) for f in ir.facts_stated}
    ir.facts_stated += [f for f in base.facts_stated if (f.predicate, f.object.lower()) not in known]
    if not ir.needs:
        ir.needs = ["direct"]
    return ir
