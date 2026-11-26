"""Forward-chaining rule engine over the knowledge graph (Datalog-style Horn
clauses with variables). Derived facts carry a proof so AETHER can explain
*why* it believes something.

Rule syntax (one per line, also loadable from rules.txt):
    sibling_of(?a, ?b) & parent_of(?p, ?a) -> parent_of(?p, ?b)
    deadline(?t, ?d) & before(?d, today) -> overdue(?t, yes)
`!` in the head marks an integrity constraint: if the body matches, it is a violation.
    allergic_to(?u, ?x) & recommended(?u, ?x) -> !unsafe(?u, ?x)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .kg import INVERSES, SYMMETRIC, KnowledgeGraph, Triple

ATOM = re.compile(r"(!?)(\w+)\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*\)")


@dataclass(frozen=True)
class Atom:
    p: str
    s: str
    o: str
    constraint: bool = False


@dataclass
class Rule:
    body: list[Atom]
    head: Atom
    text: str
    confidence: float = 1.0   # hand-written rules are certain; rules AETHER mined from your facts carry their measured rate


@dataclass
class Derivation:
    triple: tuple[str, str, str]
    rule: str
    support: list[tuple[str, str, str]] = field(default_factory=list)
    confidence: float = 1.0   # rule confidence x the confidence of every supporting fact; several proofs: noisy-or
    proofs: int = 1


@dataclass
class Violation:
    constraint: str
    binding: dict[str, str]
    support: list[tuple[str, str, str]]


def parse_rule(text: str) -> Rule:
    body_txt, head_txt = text.split("->")
    body = [Atom(m.group(2), m.group(3), m.group(4)) for m in ATOM.finditer(body_txt)]
    hm = ATOM.search(head_txt)
    if not body or not hm:
        raise ValueError(f"Cannot parse rule: {text}")
    return Rule(body, Atom(hm.group(2), hm.group(3), hm.group(4), bool(hm.group(1))), text.strip())


DEFAULT_RULES = [
    "parent_of(?p, ?a) & sibling_of(?a, ?b) -> parent_of(?p, ?b)",
    "parent_of(?g, ?p) & parent_of(?p, ?c) -> grandparent_of(?g, ?c)",
    "works_at(?x, ?org) & located_in(?org, ?city) -> works_in(?x, ?city)",
    "part_of(?a, ?b) & part_of(?b, ?c) -> part_of(?a, ?c)",
    "depends_on(?a, ?b) & depends_on(?b, ?c) -> depends_on(?a, ?c)",
    "is_a(?x, ?c) & is_a(?c, ?d) -> is_a(?x, ?d)",
    "dislikes(?u, ?x) & likes(?u, ?x) -> !inconsistent_preference(?u, ?x)",
    "allergic_to(?u, ?x) & recommended_to(?x, ?u) -> !unsafe_recommendation(?u, ?x)",
    "depends_on(?a, ?a) -> !circular_dependency(?a, ?a)",
]


def _is_var(t: str) -> bool:
    return t.startswith("?")


class RuleEngine:
    def __init__(self, kg: KnowledgeGraph, rules: list[str] | None = None):
        self.kg = kg
        self.rules = [parse_rule(r) for r in (rules or DEFAULT_RULES)]
        self.proofs: dict[tuple[str, str, str], Derivation] = {}
        with kg.lock:
            kg.db.execute("CREATE TABLE IF NOT EXISTS rules (text TEXT PRIMARY KEY)")
            if "confidence" not in {r[1] for r in kg.db.execute("PRAGMA table_info(rules)")}:
                kg.db.execute("ALTER TABLE rules ADD COLUMN confidence REAL DEFAULT 1.0")
            kg.db.commit()
        for text, conf in kg.db.execute("SELECT text, confidence FROM rules").fetchall():
            try:
                r = parse_rule(text)
                r.confidence = conf if conf is not None else 1.0
                self.rules.append(r)
            except ValueError:
                pass

    def add_rule(self, text: str, persist: bool = True, confidence: float = 1.0) -> None:
        rule = parse_rule(text)  # validates before storing
        rule.confidence = max(0.0, min(1.0, confidence))
        self.rules.append(rule)
        if persist:
            with self.kg.lock:
                self.kg.db.execute("INSERT OR REPLACE INTO rules(text, confidence) VALUES (?, ?)",
                                   (rule.text, rule.confidence))
                self.kg.db.commit()

    def remove_rule(self, text: str) -> None:
        self.rules = [r for r in self.rules if r.text != text.strip()]
        with self.kg.lock:
            self.kg.db.execute("DELETE FROM rules WHERE text=?", (text.strip(),))
            self.kg.db.commit()

    def user_rules(self) -> list[str]:
        return [r[0] for r in self.kg.db.execute("SELECT text FROM rules").fetchall()]

    def ask(self, pattern: str) -> list[dict[str, str]]:
        """First-order query over stated + derived facts, e.g. 'parent_of(?p, riya)'
        or 'works_in(?x, ?c) & likes(?x, jazz)'. Returns variable bindings."""
        atoms = [Atom(m.group(2), m.group(3).strip().lower(), m.group(4).strip().lower()) for m in ATOM.finditer(pattern)]
        if not atoms:
            raise ValueError("Query must look like predicate(subject, object)")
        self.run(persist=False)
        facts = self._facts() | set(self.proofs)
        seen, out = set(), []
        for binding, _ in self._match(atoms, facts, {}, []):
            key = tuple(sorted(binding.items()))
            if key not in seen:
                seen.add(key)
                out.append(binding)
        return out

    def _facts(self) -> set[tuple[str, str, str]]:
        facts = {t.key() for t in self.kg.all()}
        for s, p, o in list(facts):  # built-in symmetric and inverse closure
            if p in SYMMETRIC:
                facts.add((o, p, s))
            if p in INVERSES:
                facts.add((o, INVERSES[p], s))
        return facts

    def _match(self, body: list[Atom], facts: set[tuple[str, str, str]], binding: dict[str, str],
               support: list) -> list[tuple[dict[str, str], list]]:
        if not body:
            return [(binding, support)]
        atom, rest = body[0], body[1:]
        results = []
        for s, p, o in facts:
            if p != atom.p:
                continue
            b = dict(binding)
            ok = True
            for term, val in ((atom.s, s), (atom.o, o)):
                if _is_var(term):
                    if b.setdefault(term, val) != val:
                        ok = False
                elif term != val:
                    ok = False
            if ok:
                results += self._match(rest, facts, b, support + [(s, p, o)])
        return results

    def _fact_confidence(self) -> dict[tuple[str, str, str], float]:
        conf: dict[tuple[str, str, str], float] = {}
        for t in self.kg.all():
            for k in [t.key()] + ([(t.o, t.p, t.s)] if t.p in SYMMETRIC else []) + \
                     ([(t.o, INVERSES[t.p], t.s)] if t.p in INVERSES else []):
                conf[k] = max(conf.get(k, 0.0), t.confidence)
        return conf

    def run(self, max_rounds: int = 10, persist: bool = True) -> tuple[list[Derivation], list[Violation]]:
        """Forward-chain to a fixpoint. Returns new derivations and constraint violations.

        Each derived fact gets a confidence: the rule's confidence times the confidence of every fact it used
        (so a conclusion is never surer than its weakest premise). A fact proved in several independent ways
        combines them with noisy-or, 1 - (1-a)(1-b), so more evidence makes it surer but never certain."""
        facts = self._facts()
        conf = self._fact_confidence()
        stated = set(facts)
        new: list[Derivation] = []
        seen_proofs: set = set()
        violations: list[Violation] = []
        for _ in range(max_rounds):
            added = False
            for rule in self.rules:
                for binding, support in self._match(rule.body, facts, {}, []):
                    s = binding.get(rule.head.s, rule.head.s)
                    o = binding.get(rule.head.o, rule.head.o)
                    if rule.head.constraint:
                        v = Violation(rule.text, binding, support)
                        if all(v.binding != x.binding or v.constraint != x.constraint for x in violations):
                            violations.append(v)
                        continue
                    t = (s, rule.head.p, o)
                    c = rule.confidence
                    for sup in support:
                        c *= conf.get(sup, 1.0)
                    proof = (t, rule.text, tuple(support))
                    if t not in facts:
                        facts.add(t)
                        conf[t] = c
                        seen_proofs.add(proof)
                        d = Derivation(t, rule.text, support, round(c, 4))
                        self.proofs[t] = d
                        new.append(d)
                        added = True
                    elif t not in stated and proof not in seen_proofs and t in self.proofs:
                        seen_proofs.add(proof)        # another, independent proof of a derived fact
                        d = self.proofs[t]
                        d.confidence = round(1 - (1 - d.confidence) * (1 - c), 4)
                        d.proofs += 1
                        conf[t] = d.confidence
            if not added:
                break
        if persist:
            for d in new:
                self.kg.assert_fact(*d.triple, source="observed", confidence=round(0.9 * d.confidence, 3),
                                    derived=True)
        return new, violations

    def explain(self, triple: tuple[str, str, str], depth: int = 0) -> list[str]:
        d = self.proofs.get(triple)
        if not d:
            return ["  " * depth + f"{' '.join(triple)} (stated fact)"]
        lines = ["  " * depth + f"{' '.join(triple)} because [{d.rule}]"
                 + (f" (confidence {d.confidence:.2f}" + (f", {d.proofs} proofs" if d.proofs > 1 else "") + ")"
                    if d.confidence < 1 or d.proofs > 1 else "")]
        for sup in d.support:
            lines += self.explain(sup, depth + 1)
        return lines
