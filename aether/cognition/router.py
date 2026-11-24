"""Query routing engine: decides, per message, which knowledge sources to use.

Routes can combine (hybrid execution). Ordinary conversation gets no retrieval.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.config import MemoryConfig
from ..memory.store import Memory, MemoryStore
from ..neurosymbolic.kg import KnowledgeGraph, Triple, norm
from .ir import IntentIR


@dataclass
class RoutePlan:
    routes: list[str] = field(default_factory=list)
    memories: list[Memory] = field(default_factory=list)
    documents: list[Memory] = field(default_factory=list)
    kg_facts: list[Triple] = field(default_factory=list)
    web_query: str | None = None
    reasons: list[str] = field(default_factory=list)


class QueryRouter:
    def __init__(self, cfg: MemoryConfig, store: MemoryStore, kg: KnowledgeGraph):
        self.cfg, self.store, self.kg = cfg, store, kg

    def plan(self, ir: IntentIR, message: str) -> RoutePlan:
        plan = RoutePlan()
        needs = set(ir.needs)

        # Entity grounding: if the message names something the KG knows, symbolic context is cheap and relevant.
        known = self.kg.entities()
        mentioned = [e.name for e in ir.entities if norm(e.name) in known]
        if mentioned:
            needs.add("symbolic")
            plan.reasons.append(f"known entities mentioned: {', '.join(mentioned)}")

        if "memory" in needs:
            plan.routes.append("memory")
            plan.memories = self.store.search(message, k=self.cfg.rag_top_k, alpha=self.cfg.hybrid_alpha,
                                              layers=("semantic", "episodic", "procedural", "prospective"),
                                              min_score=self.cfg.rag_min_score * 0.6)
            plan.kg_facts += self.kg.about("user")
            plan.reasons.append(f"memory: {len(plan.memories)} items, {len(plan.kg_facts)} facts about you")
        else:
            # Opportunistic recall, only when a stored memory is a strong match.
            strong = self.store.search(message, k=3, alpha=self.cfg.hybrid_alpha,
                                       layers=("semantic", "episodic"), min_score=self.cfg.rag_min_score * 2)
            if strong:
                plan.routes.append("memory")
                plan.memories = strong
                plan.reasons.append("strong memory match without being asked")

        if "documents" in needs and self.store.count("document"):
            plan.routes.append("documents")
            plan.documents = self.store.search(message, k=self.cfg.rag_top_k, alpha=self.cfg.hybrid_alpha,
                                               layers=("document",), min_score=self.cfg.rag_min_score)
            plan.reasons.append(f"documents: {len(plan.documents)} chunks")

        if "symbolic" in needs:
            plan.routes.append("symbolic")
            for name in mentioned or [e.name for e in ir.entities]:
                plan.kg_facts += self.kg.about(name)
        if "web" in needs:
            plan.routes.append("web")
            plan.web_query = ir.objective or message
        if "tools" in needs:
            plan.routes.append("tools")
        if "optimize" in needs:
            plan.routes.append("optimize")
        if not plan.routes:
            plan.routes.append("direct")
            plan.reasons.append("no retrieval needed")
        seen = set()
        plan.kg_facts = [t for t in plan.kg_facts if not (t.key() in seen or seen.add(t.key()))]
        return plan
