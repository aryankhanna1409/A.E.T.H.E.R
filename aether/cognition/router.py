"""Query routing engine: decides, per message, which knowledge sources to use.

Routes can combine (hybrid execution). Ordinary conversation gets no retrieval.
"""
from __future__ import annotations

import re

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
    def __init__(self, cfg: MemoryConfig, store: MemoryStore, kg: KnowledgeGraph, reranker=None):
        self.cfg, self.store, self.kg, self.reranker = cfg, store, kg, reranker

    @staticmethod
    def named_in(message: str, known: set[str]) -> list[str]:
        """Known entities named in the message itself, found by code rather than trusting the model's entity list
        (a 7B model often drops a surname or names nobody). A first name counts when only one entity has it."""
        text = norm(message)
        found = [e for e in known if len(e) >= 3 and e != "user"
                 and re.search(rf"(?<![a-z0-9_]){re.escape(e)}(?![a-z0-9_])", text)]
        firsts: dict[str, list[str]] = {}
        for e in known:
            if "_" in e:
                firsts.setdefault(e.split("_", 1)[0], []).append(e)
        for word in set(re.findall(r"[a-z0-9]+", text)):
            hits = firsts.get(word, [])
            if len(word) >= 3 and len(hits) == 1 and hits[0] not in found and word not in known:
                found.append(hits[0])
        # the longest names first, and drop a name that is only part of a longer one found ("drive_unit" in "rover")
        found.sort(key=len, reverse=True)
        return [e for i, e in enumerate(found) if not any(e in f and e != f for f in found[:i])][:8]

    def plan(self, ir: IntentIR, message: str) -> RoutePlan:
        plan = RoutePlan()
        needs = set(ir.needs)

        # Entity grounding: if the message names something the KG knows, symbolic context is cheap and relevant.
        known = self.kg.entities()
        mentioned = [e.name for e in ir.entities if norm(e.name) in known]
        mentioned += [e for e in self.named_in(message, known) if e not in {norm(m) for m in mentioned}]
        if mentioned:
            needs.add("symbolic")
            plan.reasons.append(f"known entities mentioned: {', '.join(mentioned)}")

        if "memory" in needs:
            plan.routes.append("memory")
            learned = getattr(self, "learned", None)
            wide = self.cfg.rag_top_k * (2 if learned is not None and learned.active else 1)
            plan.memories = self.store.search(message, k=wide, alpha=self.cfg.hybrid_alpha,
                                              layers=("semantic", "episodic", "procedural", "prospective"),
                                              min_score=self.cfg.rag_min_score * 0.6)
            if learned is not None:   # memories that tend to help come first (memory/nets.py)
                plan.memories = learned.rerank(plan.memories, self.cfg.rag_top_k)
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
            wide = self.cfg.rag_top_k * (3 if self.reranker else 1)
            plan.documents = self.store.search(message, k=wide, alpha=self.cfg.hybrid_alpha,
                                               layers=("document",), min_score=self.cfg.rag_min_score)
            if self.reranker:
                plan.documents = self.reranker.rerank(message, plan.documents, top_k=self.cfg.rag_top_k)
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
