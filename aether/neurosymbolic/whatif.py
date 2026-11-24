"""Counterfactual reasoning: "what if X were true?"

The assumption is applied to an in-memory snapshot of the knowledge graph and
the same rules are forward-chained there. The answer is a precise diff: which
conclusions would newly follow, which would no longer hold, which stated facts
the assumption overrides, and which integrity constraints it would break. The
real memory is never touched, so you can explore freely.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .kg import FUNCTIONAL, KnowledgeGraph, norm
from .rules import RuleEngine



@dataclass
class WhatIf:
    assumptions: list[str]
    retracted: list[str] = field(default_factory=list)
    overridden: list[str] = field(default_factory=list)
    gained: list[dict] = field(default_factory=list)
    lost: list[dict] = field(default_factory=list)
    new_violations: list[str] = field(default_factory=list)
    resolved_violations: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = ["Hypothetically assuming: " + "; ".join(self.assumptions + [f"NOT {r}" for r in self.retracted]),
                 "(Nothing in your real memory was changed.)"]
        if self.overridden:
            lines.append("This would replace what I currently believe: " + "; ".join(self.overridden))
        if self.gained:
            lines.append("It would follow that:")
            lines += [f"  + {g['fact']}   [because {' & '.join(g['because'])}]" for g in self.gained[:20]]
        if self.lost:
            lines.append("These conclusions would no longer hold:")
            lines += [f"  - {l['fact']}" for l in self.lost[:20]]
        if self.new_violations:
            lines.append("It would break these constraints: " + "; ".join(self.new_violations))
        if self.resolved_violations:
            lines.append("It would resolve: " + "; ".join(self.resolved_violations))
        if not (self.gained or self.lost or self.new_violations or self.overridden):
            lines.append("No other conclusion changes: nothing I know depends on this.")
        return "\n".join(lines)


def parse(spec: str) -> list[tuple[bool, str, str, str]]:
    """'riya lives_in delhi; !user likes coffee' -> [(add?, s, p, o), ...]

    The predicate is the first snake_case word (so subjects and objects may be several words:
    'Riya Sharma lives_in New Delhi'), or the middle word of a three-word assumption."""
    out = []
    for part in filter(None, (x.strip() for x in spec.split(";"))):
        neg = part.startswith("!")
        words = part.lstrip("!").split()
        idx = next((i for i, w in enumerate(words) if "_" in w.strip("_") and 0 < i < len(words) - 1), None)
        if idx is None and len(words) == 3:
            idx = 1
        if idx is None or not re.fullmatch(r"[a-z_]+", words[idx].lower()):
            raise ValueError(f"Write each assumption as 'subject predicate object', e.g. 'riya lives_in delhi' (got: {part!r})")
        out.append((not neg, norm(" ".join(words[:idx])), norm(words[idx]), norm(" ".join(words[idx + 1:]))))
    return out


def _closure(kg: KnowledgeGraph, rules: list[str]) -> tuple[dict, set[str]]:
    eng = RuleEngine(kg, rules)
    derived, violations = eng.run(persist=False)
    return ({d.triple: d for d in derived},
            {f"{v.constraint.split('->')[-1].strip()} {sorted(v.binding.items())}" for v in violations})


def what_if(kg: KnowledgeGraph, rules: list[str], spec: str) -> WhatIf:
    changes = parse(spec)
    before_kg = kg.snapshot()
    before, viol_before = _closure(before_kg, rules)
    after_kg = kg.snapshot()
    res = WhatIf([f"{s} {p} {o}" for add, s, p, o in changes if add])
    for add, s, p, o in changes:
        if add:
            if p in FUNCTIONAL:
                res.overridden += [f"{t.s} {t.p} {t.o}" for t in after_kg.query(s, p, None) if t.o != o]
            after_kg.assert_fact(s, p, o, source="hypothesis", replace=True)
        else:
            res.retracted.append(f"{s} {p} {o}")
            for t in after_kg.query(s, p, o):
                after_kg.retract(t.id)
    after, viol_after = _closure(after_kg, rules)
    stated_after = {(t.s, t.p, t.o) for t in after_kg.all()}
    stated_before = {(t.s, t.p, t.o) for t in before_kg.all()}
    for t, d in after.items():
        if t not in before and t not in stated_before:
            res.gained.append({"fact": " ".join(t), "rule": d.rule, "because": [" ".join(x) for x in d.support]})
    for t, d in before.items():
        if t not in after and t not in stated_after:
            res.lost.append({"fact": " ".join(t), "rule": d.rule})
    res.new_violations = sorted(viol_after - viol_before)
    res.resolved_violations = sorted(viol_before - viol_after)
    return res
