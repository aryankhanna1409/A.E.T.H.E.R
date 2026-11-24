"""Executive agent: turns a goal into a validated task graph, runs it with
specialist agents, verifies every result, retries with feedback (bounded),
re-plans once on failure and reports only what was verified."""
from __future__ import annotations

import threading
import time
from typing import Callable

from pydantic import BaseModel, Field

from ..core.logging import log
from ..llm.base import LLMProvider, Message, structured
from .specialists import SPECIALISTS, AgentResult, VerificationAgent
from .taskgraph import TaskGraph


class PlannedTask(BaseModel):
    key: str = Field(description="short id like t1")
    agent: str = Field(description="one of: " + ", ".join(SPECIALISTS))
    description: str
    depends_on: list[str] = Field(default_factory=list)
    success_criteria: str = ""


class GoalPlan(BaseModel):
    tasks: list[PlannedTask]
    needs_clarification: str | None = None


class Executive:
    def __init__(self, llm: LLMProvider, tools, store, rules, graph: TaskGraph, max_tasks: int = 10,
                 time_budget_s: int = 1800, progress: Callable[[str], None] = lambda m: None):
        self.llm, self.tools, self.store, self.rules, self.graph = llm, tools, store, rules, graph
        self.max_tasks, self.time_budget_s, self.progress = max_tasks, time_budget_s, progress
        self.cancelled: set[int] = set()
        self.verifier = VerificationAgent(llm, tools)

    # -------------------------------------------------------------- planning
    def plan(self, goal_id: int, goal: str, feedback: str = "") -> list[str]:
        p = structured(self.llm, [
            Message("system", "You are the executive agent. Split the goal into at most "
                              f"{self.max_tasks} tasks for these agents:\n"
                              + "\n".join(f"- {n}: {c.responsibility}" for n, c in SPECIALISTS.items())
                              + "\nUse depends_on for ordering. Give each task concrete success criteria."),
            Message("user", goal + (f"\n\nThe previous plan failed: {feedback}" if feedback else ""))],
            GoalPlan, max_tokens=900)
        problems = []
        if p.needs_clarification:
            problems.append("needs clarification: " + p.needs_clarification)
        keys = {t.key for t in p.tasks}
        for t in p.tasks:
            if t.agent not in SPECIALISTS:
                problems.append(f"unknown agent {t.agent}")
            problems += [f"{t.key} depends on missing {d}" for d in t.depends_on if d not in keys]
        if TaskGraph.has_cycle({t.key: t.depends_on for t in p.tasks}):
            problems.append("plan has a dependency cycle")
        if not p.tasks or len(p.tasks) > self.max_tasks:
            problems.append(f"plan has {len(p.tasks)} tasks (allowed 1-{self.max_tasks})")
        if problems:
            return problems
        ids: dict[str, int] = {}
        pending = list(p.tasks)  # dependencies were validated, so insert in an order that respects them
        while pending:
            for t in list(pending):
                if all(d in ids for d in t.depends_on):
                    ids[t.key] = self.graph.add_task(goal_id, t.agent, t.description,
                                                     [ids[d] for d in t.depends_on], t.success_criteria)
                    pending.remove(t)
        return []

    # -------------------------------------------------------------- execution
    def run(self, goal: str) -> dict:
        gid = self.graph.new_goal(goal)
        problems = self.plan(gid, goal)
        if problems:
            self.graph.set_goal(gid, "failed", "Could not make a valid plan: " + "; ".join(problems))
            return self.graph.goal(gid)
        return self.execute(gid, goal)

    def execute(self, gid: int, goal: str) -> dict:
        self.graph.set_goal(gid, "running")
        start, replanned = time.time(), False
        while True:
            if gid in self.cancelled:
                for t in self.graph.tasks(gid):
                    if t.status in {"pending", "running"}:
                        t.status = "cancelled"
                        self.graph.update(t)
                self.graph.set_goal(gid, "cancelled", self.report(gid, goal))
                return self.graph.goal(gid)
            if time.time() - start > self.time_budget_s:
                self.graph.set_goal(gid, "failed", "Time budget exceeded.\n" + self.report(gid, goal))
                return self.graph.goal(gid)
            ready = self.graph.ready(gid)
            tasks = self.graph.tasks(gid)
            if not ready:
                failed = [t for t in tasks if t.status == "failed"]
                if failed and not replanned:
                    replanned = True
                    why = "; ".join(f"{t.description}: {t.result[:200]}" for t in failed)
                    for t in tasks:
                        if t.status in {"pending", "failed"}:
                            t.status = "replaced"
                            self.graph.update(t)
                    self.progress(f"Re-planning after failure: {why[:200]}")
                    if not self.plan(gid, goal, why):
                        continue
                live = [t for t in self.graph.tasks(gid) if t.status != "replaced"]
                status = "done" if live and all(t.status == "done" for t in live) else "failed"
                self.graph.set_goal(gid, status, self.report(gid, goal))
                return self.graph.goal(gid)
            prior = {t.id: t.result for t in tasks if t.status == "done"}
            for task in ready:
                self._run_task(task, {d: prior[d] for d in task.depends_on if d in prior})

    def _run_task(self, task, prior: dict[int, str]) -> None:
        agent = SPECIALISTS[task.agent](self.llm, self.tools, self.store, self.rules)
        feedback = ""
        while task.attempts < task.max_attempts:
            task.attempts += 1
            task.status = "running"
            self.graph.update(task)
            self.progress(f"[{task.agent}] {task.description} (attempt {task.attempts})")
            try:
                result = agent.run(task, prior, feedback)
                verdict = self.verifier.verify(task, result)
            except Exception as e:
                log.exception("agent %s crashed", task.agent)
                result, verdict = AgentResult(False, f"error: {e}", []), None
            task.checkpoint = {"last_output": result.output[:2000], "evidence_items": len(result.evidence)}
            if verdict and verdict.passed:
                task.status, task.result = "done", result.output
                self.graph.update(task)
                return
            feedback = f"Previous attempt was rejected: {verdict.reason if verdict else result.output}. " + \
                       ("Missing: " + ", ".join(verdict.missing) if verdict and verdict.missing else "")
            task.result = feedback
            if not result.ok and any("declined" in e or "Blocked" in e for e in result.evidence):
                break  # never retry what the user or policy refused
        task.status = "failed"
        self.graph.update(task)

    def report(self, gid: int, goal: str) -> str:
        lines = [f"Goal: {goal}"]
        for t in self.graph.tasks(gid):
            lines.append(f"- [{t.status}] ({t.agent}) {t.description}")
            if t.status == "done":
                lines.append("    " + t.result[:400].replace("\n", "\n    "))
        lines.append("Only tasks marked [done] were verified against their success criteria.")
        return "\n".join(lines)

    def cancel(self, gid: int) -> None:
        self.cancelled.add(gid)


class GoalRunner:
    """Runs goals in the background, one at a time."""

    def __init__(self, executive: Executive):
        self.ex = executive
        self.queue: list[str] = []
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def submit(self, goal: str) -> str:
        with self.lock:
            self.queue.append(goal)
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, daemon=True, name="aether-goals")
                self.thread.start()
        return f"Queued. I'll work on it in the background: {goal}"

    def _loop(self) -> None:
        while True:
            with self.lock:
                if not self.queue:
                    return
                goal = self.queue.pop(0)
            try:
                self.ex.run(goal)
            except Exception:
                log.exception("goal failed: %s", goal)
