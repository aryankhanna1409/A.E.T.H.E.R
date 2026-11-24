"""Specialist agents. Each has one responsibility and works only through the
shared tool registry (so the policy engine and audit log see everything)."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from ..cognition.ir import ToolCall
from ..llm.base import LLMProvider, Message, structured
from .taskgraph import Task


@dataclass
class AgentResult:
    ok: bool
    output: str
    evidence: list[str]  # tool results or checks that back the output


class ToolPlan(BaseModel):
    calls: list[ToolCall] = Field(default_factory=list)
    note: str = ""


class Verdict(BaseModel):
    passed: bool
    reason: str
    missing: list[str] = Field(default_factory=list)


class BaseAgent:
    name = "base"
    responsibility = ""
    allowed_tools: tuple[str, ...] = ()

    def __init__(self, llm: LLMProvider, tools, store=None, kg_rules=None):
        self.llm, self.tools, self.store, self.rules = llm, tools, store, kg_rules

    def _context(self, prior: dict[int, str]) -> str:
        return "\n\n".join(f"<result of task {k}>\n{v[:2000]}\n</result>" for k, v in prior.items()) or "(none)"

    def _use_tools(self, task: Task, prior: dict[int, str], feedback: str) -> AgentResult:
        catalog = "\n".join(t.signature() for n, t in self.tools.tools.items() if n in self.allowed_tools)
        plan = structured(self.llm, [
            Message("system", f"You are the {self.name} agent: {self.responsibility}\nTools you may call:\n{catalog}\n"
                              "Return the tool calls needed for this task, in order (max 4)."),
            Message("user", f"Task: {task.description}\nSuccess criteria: {task.success_criteria}\n"
                            f"Earlier results:\n{self._context(prior)}\n{feedback}")], ToolPlan, max_tokens=400)
        evidence, ok = [], True
        for call in plan.calls[:4]:
            if call.tool not in self.allowed_tools:
                evidence.append(f"refused: {call.tool} is not available to the {self.name} agent")
                continue
            r = self.tools.call(call.tool, call.args)
            evidence.append(r.as_context()[:3000])
            if r.decision in {"deny", "declined"}:
                ok = False
                break
        summary = self.llm.chat([
            Message("system", f"You are the {self.name} agent. Report what was done, based ONLY on the tool results. "
                              "Never claim an action that has no tool result."),
            Message("user", f"Task: {task.description}\n\nTool results:\n" + "\n".join(evidence))],
            max_tokens=700, temperature=0.2)
        return AgentResult(ok and bool(evidence), summary, evidence)

    def run(self, task: Task, prior: dict[int, str], feedback: str = "") -> AgentResult:
        return self._use_tools(task, prior, feedback)


class ResearchAgent(BaseAgent):
    name = "research"
    responsibility = "search the internet, read sources, cross-check claims, write cited findings"
    allowed_tools = ("research", "web_search", "web_fetch")


class ReasoningAgent(BaseAgent):
    name = "reasoning"
    responsibility = "structured reasoning, comparing alternatives, logic queries and constraint solving"
    allowed_tools = ("kg_query", "solve_plan", "optimize_schedule")

    def run(self, task, prior, feedback=""):
        res = self._use_tools(task, prior, feedback)
        if res.evidence:
            return res
        text = self.llm.chat([Message("system", "Reason step by step, then give a clear conclusion. Mark assumptions."),
                              Message("user", f"{task.description}\n\nInputs:\n{self._context(prior)}\n{feedback}")],
                             max_tokens=900, temperature=0.3)
        return AgentResult(True, text, ["reasoned from inputs (no tools)"])


class CodingAgent(BaseAgent):
    name = "coding"
    responsibility = "read authorized code, write changes, run tests, propose fixes"
    allowed_tools = ("fs_read", "fs_list", "fs_search", "fs_create", "fs_modify", "shell")


class OperatorAgent(BaseAgent):
    name = "operator"
    responsibility = "manage files and applications on the user's computer within granted permissions"
    allowed_tools = ("fs_list", "fs_search", "fs_read", "fs_create", "fs_modify", "fs_move", "fs_delete",
                     "app_open", "shell", "screen_read")


class MemoryAgent(BaseAgent):
    name = "memory"
    responsibility = "store and retrieve the user's memories and reminders"
    allowed_tools = ("remember", "remind", "kg_query")

    def run(self, task, prior, feedback=""):
        hits = self.store.search(task.description, k=6) if self.store else []
        if hits and "remember" not in task.description.lower() and "remind" not in task.description.lower():
            return AgentResult(True, "\n".join(f"- {h.text}" for h in hits), [f"memory #{h.id}" for h in hits])
        return self._use_tools(task, prior, feedback)


class VerificationAgent(BaseAgent):
    name = "verification"
    responsibility = "check outputs against success criteria and evidence; detect unsupported claims"

    def verify(self, task: Task, result: AgentResult) -> Verdict:
        if not result.ok:
            return Verdict(passed=False, reason="the agent reported failure or a blocked action")
        if not result.evidence:
            return Verdict(passed=False, reason="no evidence backs the output")
        return structured(self.llm, [
            Message("system", "You verify work. Pass only if the output meets the success criteria AND every claim "
                              "is backed by the evidence. Be strict."),
            Message("user", f"Task: {task.description}\nSuccess criteria: {task.success_criteria or 'task completed'}\n"
                            f"Output:\n{result.output[:3000]}\n\nEvidence:\n" + "\n".join(e[:1500] for e in result.evidence))],
            Verdict, max_tokens=300)


SPECIALISTS = {c.name: c for c in (ResearchAgent, ReasoningAgent, CodingAgent, OperatorAgent, MemoryAgent)}
