"""Symbolic checks on neural output.

1. validate_ir: the parsed request is checked against hard facts (known tools,
   resolvable dates, policy) before anything runs.
2. check_answer: claims in the drafted answer are extracted (by the LLM, as
   typed triples) and compared with the knowledge graph; contradictions are
   either fixed by a revision pass or flagged to the user.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ..cognition.ir import FactTriple, IntentIR
from ..llm.base import LLMProvider, Message, structured
from .kg import FUNCTIONAL, KnowledgeGraph, norm
from .temporal import resolve


@dataclass
class IRReport:
    ok: bool
    issues: list[str] = field(default_factory=list)
    resolved_dates: dict[str, str] = field(default_factory=dict)
    dropped_tools: list[str] = field(default_factory=list)


def validate_ir(ir: IntentIR, known_tools: set[str]) -> IRReport:
    rep = IRReport(ok=True)
    for expr in ir.time_expressions + [v for v in ir.constraints.values()]:
        d = resolve(expr)
        if d:
            rep.resolved_dates[expr] = d.isoformat()
    for k, v in list(ir.constraints.items()):
        if v in rep.resolved_dates:
            ir.constraints[k] = rep.resolved_dates[v]
    kept = []
    for call in ir.tools_required:
        if call.tool in known_tools:
            kept.append(call)
        else:
            rep.dropped_tools.append(call.tool)
            rep.issues.append(f"model asked for unknown tool '{call.tool}'; ignored")
    ir.tools_required = kept
    if ir.ambiguity and ir.confidence < 0.4:
        rep.ok = False
        rep.issues.append(f"too ambiguous to act on: {ir.ambiguity}")
    return rep


class Claims(BaseModel):
    claims: list[FactTriple] = Field(default_factory=list,
                                     description="factual claims in the text about the user, people, places, dates; use 'user' for the user")


@dataclass
class AnswerCheck:
    contradictions: list[str] = field(default_factory=list)
    supported: list[str] = field(default_factory=list)


def check_answer(llm: LLMProvider, kg: KnowledgeGraph, answer: str) -> AnswerCheck:
    out = AnswerCheck()
    if not kg.all():
        return out
    try:
        claims = structured(llm, [
            Message("system", "Extract factual claims as subject/predicate/object triples. Only claims stated in the text."),
            Message("user", answer[:3000])], Claims, max_tokens=400)
    except ValueError:
        return out
    for c in claims.claims:
        s, p, o = norm(c.subject), norm(c.predicate), norm(c.object)
        stored = kg.query(s, p, None)
        if any(t.o == o for t in stored):
            out.supported.append(f"{s} {p} {o}")
        elif p in FUNCTIONAL and stored:
            out.contradictions.append(f"answer says {s} {p} {o}, but memory says {s} {p} {stored[0].o}")
    return out
