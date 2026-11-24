"""Metaheuristic optimization engine: genetic algorithm, simulated annealing
and particle swarm over a common Problem interface, plus a weekly schedule
problem AETHER can use for planning.

Every result reports the objective, constraints, best candidate, trade-offs
and convergence history, so nothing is a black box.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar

S = TypeVar("S")


@dataclass
class Problem(Generic[S]):
    name: str
    random_solution: Callable[[random.Random], S]
    mutate: Callable[[S, random.Random], S]
    fitness: Callable[[S], float]  # higher is better
    crossover: Callable[[S, S, random.Random], S] | None = None
    violations: Callable[[S], list[str]] = lambda s: []
    describe: Callable[[S], str] = str
    objective: str = ""
    constraints: list[str] = field(default_factory=list)


@dataclass
class Result(Generic[S]):
    algorithm: str
    best: S
    fitness: float
    history: list[float]
    evaluations: int
    violations: list[str]
    converged: bool

    def report(self, problem: Problem) -> str:
        lines = [f"Objective: {problem.objective}",
                 "Hard constraints: " + ("; ".join(problem.constraints) or "none"),
                 f"Algorithm: {self.algorithm} ({self.evaluations} evaluations, "
                 f"{'converged' if self.converged else 'stopped at budget'})",
                 f"Best fitness: {self.fitness:.3f}",
                 "Best candidate:\n" + problem.describe(self.best)]
        lines.append("Constraint violations: " + ("; ".join(self.violations) if self.violations else "none"))
        return "\n".join(lines)


def _penalized(problem: Problem, s) -> float:
    return problem.fitness(s) - 1000 * len(problem.violations(s))


def genetic(problem: Problem, pop_size: int = 60, generations: int = 150, elite: int = 4,
            mutation_rate: float = 0.3, seed: int = 0, patience: int = 30) -> Result:
    rng = random.Random(seed)
    pop = [problem.random_solution(rng) for _ in range(pop_size)]
    history, evals, stale, best_f = [], 0, 0, -math.inf
    for _ in range(generations):
        scored = sorted(((_penalized(problem, s), s) for s in pop), key=lambda x: x[0], reverse=True)
        evals += len(pop)
        history.append(scored[0][0])
        if scored[0][0] > best_f + 1e-9:
            best_f, stale = scored[0][0], 0
        else:
            stale += 1
        if stale >= patience:
            break
        nxt = [s for _, s in scored[:elite]]
        while len(nxt) < pop_size:
            a = max(rng.sample(scored, 3), key=lambda x: x[0])[1]  # tournament selection
            b = max(rng.sample(scored, 3), key=lambda x: x[0])[1]
            child = problem.crossover(a, b, rng) if problem.crossover else a
            if rng.random() < mutation_rate:
                child = problem.mutate(child, rng)
            nxt.append(child)
        pop = nxt
    best = max(pop, key=lambda s: _penalized(problem, s))
    return Result("genetic algorithm", best, problem.fitness(best), history, evals,
                  problem.violations(best), stale >= patience)


def annealing(problem: Problem, steps: int = 5000, t0: float = 5.0, t_end: float = 0.01, seed: int = 0) -> Result:
    rng = random.Random(seed)
    cur = problem.random_solution(rng)
    cur_f = _penalized(problem, cur)
    best, best_f, history = cur, cur_f, []
    for i in range(steps):
        t = t0 * (t_end / t0) ** (i / steps)  # geometric cooling
        cand = problem.mutate(cur, rng)
        f = _penalized(problem, cand)
        if f >= cur_f or rng.random() < math.exp((f - cur_f) / t):
            cur, cur_f = cand, f
            if f > best_f:
                best, best_f = cand, f
        if i % 50 == 0:
            history.append(best_f)
    return Result("simulated annealing", best, problem.fitness(best), history, steps,
                  problem.violations(best), True)


def particle_swarm(f: Callable[[list[float]], float], bounds: list[tuple[float, float]], particles: int = 30,
                   iters: int = 200, seed: int = 0, w: float = 0.7, c1: float = 1.5, c2: float = 1.5) -> tuple[list[float], float, list[float]]:
    """PSO for continuous problems (e.g. tuning router thresholds). Maximizes f."""
    rng = random.Random(seed)
    dim = len(bounds)
    pos = [[rng.uniform(lo, hi) for lo, hi in bounds] for _ in range(particles)]
    vel = [[0.0] * dim for _ in range(particles)]
    pbest = [p[:] for p in pos]
    pbest_f = [f(p) for p in pos]
    g = max(range(particles), key=lambda i: pbest_f[i])
    gbest, gbest_f, history = pbest[g][:], pbest_f[g], []
    for _ in range(iters):
        for i in range(particles):
            for d in range(dim):
                vel[i][d] = (w * vel[i][d] + c1 * rng.random() * (pbest[i][d] - pos[i][d])
                             + c2 * rng.random() * (gbest[d] - pos[i][d]))
                lo, hi = bounds[d]
                pos[i][d] = min(hi, max(lo, pos[i][d] + vel[i][d]))
            fi = f(pos[i])
            if fi > pbest_f[i]:
                pbest[i], pbest_f[i] = pos[i][:], fi
                if fi > gbest_f:
                    gbest, gbest_f = pos[i][:], fi
        history.append(gbest_f)
    return gbest, gbest_f, history


# --------------------------------------------------------------------------- schedule problem
@dataclass
class Task:
    name: str
    hours: float
    priority: int  # 1 (low) .. 5 (high)
    deadline_day: int  # 0 = Monday .. 6 = Sunday


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def parse_tasks(spec: str) -> list[Task]:
    """'essay:3:5:2; gym:1:3:6' -> name:hours:priority:deadline_day(0=Mon)."""
    tasks = []
    for part in filter(None, (p.strip() for p in spec.split(";"))):
        name, hours, prio, dl = (x.strip() for x in part.split(":"))
        dl_idx = int(dl) if dl.isdigit() else [d.lower() for d in DAYS].index(dl[:3].lower())
        tasks.append(Task(name, float(hours), int(prio), dl_idx))
    return tasks


def schedule_problem(tasks: list[Task], hours_per_day: list[float]) -> Problem[list[int]]:
    """Assign each task to a day. Maximize priority-weighted earliness and balance;
    hard constraints: deadlines and daily capacity."""
    n = len(tasks)

    def violations(s: list[int]) -> list[str]:
        v = [f"{t.name} after its deadline ({DAYS[t.deadline_day]})" for t, d in zip(tasks, s) if d > t.deadline_day]
        for day in range(7):
            load = sum(t.hours for t, d in zip(tasks, s) if d == day)
            if load > hours_per_day[day] + 1e-9:
                v.append(f"{DAYS[day]} overloaded ({load:.1f}h > {hours_per_day[day]:.1f}h)")
        return v

    def fitness(s: list[int]) -> float:
        early = sum(t.priority * (t.deadline_day - d + 1) for t, d in zip(tasks, s) if d <= t.deadline_day)
        loads = [sum(t.hours for t, d in zip(tasks, s) if d == day) / max(hours_per_day[day], 0.1)
                 for day in range(7) if hours_per_day[day] > 0]
        mean = sum(loads) / len(loads)
        balance = -sum((x - mean) ** 2 for x in loads)
        return early + 5 * balance

    def describe(s: list[int]) -> str:
        out = []
        for day in range(7):
            items = [f"{t.name} ({t.hours:g}h, p{t.priority})" for t, d in zip(tasks, s) if d == day]
            if items:
                out.append(f"{DAYS[day]}: " + ", ".join(items))
        return "\n".join(out)

    return Problem(
        name="weekly schedule",
        random_solution=lambda r: [r.randint(0, max(t.deadline_day, 0)) for t in tasks],
        mutate=lambda s, r: [r.randint(0, 6) if r.random() < 1.5 / n else d for d in s],
        crossover=lambda a, b, r: [x if r.random() < 0.5 else y for x, y in zip(a, b)],
        fitness=fitness, violations=violations, describe=describe,
        objective="finish high-priority work early while keeping days evenly loaded",
        constraints=["every task on or before its deadline", "daily hours within capacity"],
    )


def optimize_schedule(tasks: str, hours_per_day: str = "4,4,4,4,4,6,6") -> str:
    ts = parse_tasks(tasks)
    cap = [float(x) for x in hours_per_day.split(",")]
    prob = schedule_problem(ts, cap)
    ga = genetic(prob)
    sa = annealing(prob)
    best = ga if (len(ga.violations), -ga.fitness) <= (len(sa.violations), -sa.fitness) else sa
    other = sa if best is ga else ga
    trade = (f"Compared with {other.algorithm}: fitness {other.fitness:.2f} vs {best.fitness:.2f}. "
             f"Total work {sum(t.hours for t in ts):g}h vs capacity {sum(cap):g}h.")
    limits = "Limitations: assumes task hours are accurate and tasks are not split across days."
    return best.report(prob) + "\nTrade-offs: " + trade + "\n" + limits
