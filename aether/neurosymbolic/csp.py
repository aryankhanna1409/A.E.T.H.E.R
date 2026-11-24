"""Finite-domain constraint solver (backtracking + MRV + forward checking)
and a task planner built on it: tasks with durations, dependencies, deadlines
and limited hours per day. Deterministic, so plans are reproducible and every
failure names the constraint that could not be met."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Hashable

Value = Hashable
Constraint = tuple[tuple[str, ...], Callable[..., bool], str]  # (variables, predicate, description)
# Partial constraints are also checked on incomplete assignments (unassigned
# variables are passed as None), which prunes large n-ary constraints early.


@dataclass
class CSP:
    domains: dict[str, list[Value]]
    constraints: list[Constraint] = field(default_factory=list)
    partial: set[int] = field(default_factory=set)

    def add(self, variables: tuple[str, ...], predicate: Callable[..., bool], description: str,
            partial: bool = False) -> None:
        if partial:
            self.partial.add(len(self.constraints))
        self.constraints.append((variables, predicate, description))

    def _consistent(self, var: str, assignment: dict[str, Value]) -> str | None:
        for i, (vars_, pred, desc) in enumerate(self.constraints):
            if var not in vars_:
                continue
            if all(v in assignment for v in vars_):
                if not pred(*(assignment[v] for v in vars_)):
                    return desc
            elif i in self.partial and not pred(*(assignment.get(v) for v in vars_)):
                return desc
        return None

    def solve(self, max_nodes: int = 200_000) -> tuple[dict[str, Value] | None, list[str]]:
        """Returns (solution, reasons). Reasons list the constraints that failed most often."""
        failures: dict[str, int] = {}
        nodes = 0

        def backtrack(assignment: dict[str, Value], domains: dict[str, list[Value]]):
            nonlocal nodes
            if len(assignment) == len(self.domains):
                return dict(assignment)
            var = min((v for v in self.domains if v not in assignment), key=lambda v: len(domains[v]))  # MRV
            for value in domains[var]:
                nodes += 1
                if nodes > max_nodes:
                    return None
                assignment[var] = value
                bad = self._consistent(var, assignment)
                if bad is None:
                    pruned = self._forward_check(var, assignment, domains, failures)
                    if pruned is not None:
                        result = backtrack(assignment, pruned)
                        if result is not None:
                            return result
                else:
                    failures[bad] = failures.get(bad, 0) + 1
                del assignment[var]
            return None

        sol = backtrack({}, {k: list(v) for k, v in self.domains.items()})
        reasons = [d for d, _ in sorted(failures.items(), key=lambda x: -x[1])[:3]]
        return sol, ([] if sol else reasons or ["search budget exhausted"])

    def _forward_check(self, var, assignment, domains, failures):
        new = {k: list(v) for k, v in domains.items()}
        for vars_, pred, desc in self.constraints:
            if var not in vars_:
                continue
            unassigned = [v for v in vars_ if v not in assignment]
            if len(unassigned) != 1:
                continue
            u = unassigned[0]
            keep = []
            for val in new[u]:
                trial = {**assignment, u: val}
                if pred(*(trial[v] for v in vars_)):
                    keep.append(val)
            if not keep:
                failures[desc] = failures.get(desc, 0) + 1
                return None
            new[u] = keep
        return new


@dataclass
class PlanTask:
    name: str
    hours: int
    deadline_day: int  # 0-based day index in the planning window
    after: list[str] = field(default_factory=list)


def plan_tasks(tasks: list[PlanTask], hours_per_day: list[int]) -> tuple[dict[str, int] | None, list[str]]:
    """Assign each task to a day: deadlines, dependencies (strictly earlier day) and daily capacity."""
    names = [t.name for t in tasks]
    by = {t.name: t for t in tasks}
    csp = CSP({t.name: list(range(min(t.deadline_day, len(hours_per_day) - 1) + 1)) for t in tasks})
    for t in tasks:
        for dep in t.after:
            if dep in by:
                csp.add((dep, t.name), lambda a, b: a < b, f"{t.name} must come after {dep}")
    for day, cap in enumerate(hours_per_day):
        def fits(*days, day=day, cap=cap):
            return sum(by[n].hours for n, d in zip(names, days) if d == day) <= cap
        csp.add(tuple(names), fits, f"day {day + 1} has only {cap}h", partial=True)
    return csp.solve()


def plan_tool(tasks: str, hours_per_day: str = "4,4,4,4,4,6,6") -> str:
    """'essay:3:2; slides:2:4:essay' -> name:hours:deadline_day(1-based)[:after1,after2]."""
    items = []
    for part in filter(None, (p.strip() for p in tasks.split(";"))):
        bits = [b.strip() for b in part.split(":")]
        items.append(PlanTask(bits[0], int(float(bits[1])), int(bits[2]) - 1,
                              [x.strip() for x in bits[3].split(",")] if len(bits) > 3 and bits[3] else []))
    cap = [int(float(x)) for x in hours_per_day.split(",")]
    sol, reasons = plan_tasks(items, cap)
    if not sol:
        return "No plan satisfies every constraint. Hardest constraints: " + "; ".join(reasons)
    lines = []
    for day in range(len(cap)):
        todo = [f"{n} ({next(t.hours for t in items if t.name == n)}h)" for n, d in sol.items() if d == day]
        if todo:
            lines.append(f"Day {day + 1}: " + ", ".join(todo))
    return "Valid plan (all deadlines, dependencies and daily limits hold):\n" + "\n".join(lines)
