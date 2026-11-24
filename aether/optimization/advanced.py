"""Phase 9 metaheuristics: NSGA-II (multi-objective, Pareto fronts), tabu search
and differential evolution, plus a multi-objective version of the weekly
schedule so AETHER can show real trade-offs instead of one weighted answer."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable

from .engine import DAYS, Problem, Result, Task, _penalized


# --------------------------------------------------------------------------- NSGA-II
@dataclass
class MOProblem:
    name: str
    random_solution: Callable[[random.Random], list]
    mutate: Callable[[list, random.Random], list]
    crossover: Callable[[list, list, random.Random], list]
    objectives: Callable[[list], tuple[float, ...]]  # every objective is MINIMIZED
    objective_names: list[str]
    violations: Callable[[list], list[str]] = lambda s: []
    describe: Callable[[list], str] = str


@dataclass
class ParetoResult:
    front: list[tuple[list, tuple[float, ...]]]  # (solution, objectives), feasible first
    generations: int
    evaluations: int
    objective_names: list[str] = field(default_factory=list)

    def report(self, problem: MOProblem, limit: int = 5) -> str:
        lines = [f"Pareto front for {problem.name}: {len(self.front)} non-dominated plans "
                 f"({self.generations} generations, {self.evaluations} evaluations).",
                 "Each plan is best at something; none is better on every objective."]
        picks = _spread(self.front, limit)
        for i, (s, obj) in enumerate(picks, 1):
            vals = ", ".join(f"{n} {v:.2f}" for n, v in zip(self.objective_names, obj))
            lines.append(f"\nOption {i}: {vals}\n{problem.describe(s)}")
        return "\n".join(lines)


def _dominates(a, b) -> bool:
    return all(x <= y for x, y in zip(a, b)) and any(x < y for x, y in zip(a, b))


def _constrained_key(problem, s, obj):
    """Deb's rule: feasible beats infeasible; infeasible compared by violation count."""
    return len(problem.violations(s)), obj


def _cdominates(a, b) -> bool:
    (va, oa), (vb, ob) = a, b
    if va != vb:
        return va < vb
    return _dominates(oa, ob)


def fast_nondominated_sort(keys: list) -> list[list[int]]:
    n = len(keys)
    dominated_by = [[] for _ in range(n)]
    count = [0] * n
    fronts: list[list[int]] = [[]]
    for p in range(n):
        for q in range(n):
            if p == q:
                continue
            if _cdominates(keys[p], keys[q]):
                dominated_by[p].append(q)
            elif _cdominates(keys[q], keys[p]):
                count[p] += 1
        if count[p] == 0:
            fronts[0].append(p)
    i = 0
    while fronts[i]:
        nxt = []
        for p in fronts[i]:
            for q in dominated_by[p]:
                count[q] -= 1
                if count[q] == 0:
                    nxt.append(q)
        i += 1
        fronts.append(nxt)
    return fronts[:-1]


def crowding_distance(objs: list[tuple[float, ...]]) -> list[float]:
    n = len(objs)
    if n <= 2:
        return [math.inf] * n
    dist = [0.0] * n
    for m in range(len(objs[0])):
        order = sorted(range(n), key=lambda i: objs[i][m])
        lo, hi = objs[order[0]][m], objs[order[-1]][m]
        dist[order[0]] = dist[order[-1]] = math.inf
        if hi - lo < 1e-12:
            continue
        for k in range(1, n - 1):
            dist[order[k]] += (objs[order[k + 1]][m] - objs[order[k - 1]][m]) / (hi - lo)
    return dist


def nsga2(problem: MOProblem, pop_size: int = 60, generations: int = 120, mutation_rate: float = 0.4,
          seed: int = 0) -> ParetoResult:
    rng = random.Random(seed)
    pop = [problem.random_solution(rng) for _ in range(pop_size)]
    evals = 0

    def evaluate(ss):
        nonlocal evals
        evals += len(ss)
        return [(len(problem.violations(s)), tuple(problem.objectives(s))) for s in ss]

    keys = evaluate(pop)
    for _ in range(generations):
        fronts = fast_nondominated_sort(keys)
        rank, crowd = {}, {}
        for r, f in enumerate(fronts):
            cd = crowding_distance([keys[i][1] for i in f])
            for i, d in zip(f, cd):
                rank[i], crowd[i] = r, d

        def tournament():
            a, b = rng.sample(range(len(pop)), 2)
            return pop[a] if (rank[a], -crowd[a]) <= (rank[b], -crowd[b]) else pop[b]

        children = []
        while len(children) < pop_size:
            c = problem.crossover(tournament(), tournament(), rng)
            if rng.random() < mutation_rate:
                c = problem.mutate(c, rng)
            children.append(c)
        union, ukeys = pop + children, keys + evaluate(children)
        nxt: list[int] = []
        for f in fast_nondominated_sort(ukeys):
            if len(nxt) + len(f) <= pop_size:
                nxt += f
                continue
            cd = crowding_distance([ukeys[i][1] for i in f])
            nxt += [i for _, i in sorted(zip(cd, f), key=lambda x: -x[0])][:pop_size - len(nxt)]
            break
        pop, keys = [union[i] for i in nxt], [ukeys[i] for i in nxt]
    first = fast_nondominated_sort(keys)[0]
    seen, front = set(), []
    for i in first:
        sig = tuple(pop[i])
        if sig not in seen:
            seen.add(sig)
            front.append((pop[i], keys[i][1]))
    front.sort(key=lambda x: x[1])
    return ParetoResult(front, generations, evals, problem.objective_names)


def _spread(front, limit):
    """Pick up to `limit` options spread across the front (extremes first)."""
    if len(front) <= limit:
        return front
    picks = [front[0], front[-1]]
    step = (len(front) - 1) / (limit - 1)
    for k in range(1, limit - 1):
        picks.insert(-1, front[round(k * step)])
    return picks


# --------------------------------------------------------------------------- tabu search
def tabu_search(problem: Problem, neighbours: Callable[[list], list[tuple[list, tuple]]],
                iters: int = 300, tenure: int = 12, seed: int = 0) -> Result:
    """Best-improvement local search that forbids recently changed moves.
    `neighbours(s)` returns (candidate, move_signature) pairs."""
    rng = random.Random(seed)
    cur = problem.random_solution(rng)
    best, best_f = cur, _penalized(problem, cur)
    tabu: dict[tuple, int] = {}
    history, evals = [], 0
    for it in range(iters):
        cands = neighbours(cur)
        evals += len(cands)
        scored = sorted(((_penalized(problem, c), c, m) for c, m in cands), key=lambda x: -x[0])
        moved = False
        for f, c, m in scored:
            if tabu.get(m, -1) >= it and f <= best_f:  # aspiration: a tabu move is fine if it beats the best
                continue
            cur, moved = c, True
            tabu[m] = it + tenure
            if f > best_f:
                best, best_f = c, f
            break
        history.append(best_f)
        if not moved:
            break
    return Result("tabu search", best, problem.fitness(best), history, evals, problem.violations(best), True)


def assignment_neighbours(values: int):
    """Neighbourhood for 'assign each item to one of `values` slots' encodings."""
    def gen(s: list[int]):
        out = []
        for i, v in enumerate(s):
            for nv in range(values):
                if nv != v:
                    c = s[:]
                    c[i] = nv
                    out.append((c, (i, nv)))
        return out
    return gen


# --------------------------------------------------------------------------- differential evolution
def differential_evolution(f: Callable[[list[float]], float], bounds: list[tuple[float, float]],
                           pop_size: int = 20, iters: int = 150, F: float = 0.7, CR: float = 0.9,
                           seed: int = 0) -> tuple[list[float], float, list[float]]:
    """DE/rand/1/bin for continuous problems. Maximizes f."""
    rng = random.Random(seed)
    dim = len(bounds)
    pop = [[rng.uniform(lo, hi) for lo, hi in bounds] for _ in range(pop_size)]
    fit = [f(p) for p in pop]
    history = []
    for _ in range(iters):
        for i in range(pop_size):
            a, b, c = rng.sample([j for j in range(pop_size) if j != i], 3)
            jr = rng.randrange(dim)
            trial = []
            for d in range(dim):
                lo, hi = bounds[d]
                v = pop[a][d] + F * (pop[b][d] - pop[c][d]) if (rng.random() < CR or d == jr) else pop[i][d]
                trial.append(min(hi, max(lo, v)))
            ft = f(trial)
            if ft >= fit[i]:
                pop[i], fit[i] = trial, ft
        history.append(max(fit))
    b = max(range(pop_size), key=lambda i: fit[i])
    return pop[b], fit[b], history


# --------------------------------------------------------------------------- multi-objective schedule
def schedule_mo_problem(tasks: list[Task], hours_per_day: list[float]) -> MOProblem:
    """Objectives (all minimized): priority-weighted lateness in the week,
    busiest day's load, and weekend hours used."""
    def loads(s):
        return [sum(t.hours for t, d in zip(tasks, s) if d == day) for day in range(7)]

    def violations(s):
        v = [f"{t.name} after deadline" for t, d in zip(tasks, s) if d > t.deadline_day]
        v += [f"{DAYS[day]} overloaded" for day, load in enumerate(loads(s)) if load > hours_per_day[day] + 1e-9]
        return v

    def objectives(s):
        lateness = sum(t.priority * d for t, d in zip(tasks, s))
        ls = loads(s)
        return (float(lateness), max(ls), ls[5] + ls[6])

    def describe(s):
        return "\n".join(f"  {DAYS[day]}: " + ", ".join(f"{t.name} ({t.hours:g}h)" for t, d in zip(tasks, s) if d == day)
                         for day in range(7) if any(d == day for d in s))

    n = max(len(tasks), 1)
    return MOProblem(
        name="weekly schedule",
        random_solution=lambda r: [r.randint(0, t.deadline_day) for t in tasks],
        mutate=lambda s, r: [r.randint(0, 6) if r.random() < 1.5 / n else d for d in s],
        crossover=lambda a, b, r: [x if r.random() < 0.5 else y for x, y in zip(a, b)],
        objectives=objectives, objective_names=["lateness", "busiest day h", "weekend h"],
        violations=violations, describe=describe)


def pareto_schedule(tasks: str, hours_per_day: str = "4,4,4,4,4,6,6", options: int = 4) -> str:
    from .engine import parse_tasks
    prob = schedule_mo_problem(parse_tasks(tasks), [float(x) for x in hours_per_day.split(",")])
    res = nsga2(prob)
    feasible = [(s, o) for s, o in res.front if not prob.violations(s)]
    if not feasible:
        return "No plan meets every deadline and daily limit. Free up hours or move a deadline.\n" + res.report(prob, 1)
    res.front = feasible
    return res.report(prob, options)
