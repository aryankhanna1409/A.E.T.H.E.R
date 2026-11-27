from conftest import FakeLLM

from aether.agents.executive import Executive
from aether.agents.taskgraph import TaskGraph


def plan(tasks):
    return {"tasks": tasks}


def executive_for(a, llm):
    return Executive(llm, a.tools, a.store, a.rules, TaskGraph(a.db, a.lock))


def test_cycle_detection():
    assert TaskGraph.has_cycle({"a": ["b"], "b": ["a"]})
    assert not TaskGraph.has_cycle({"a": [], "b": ["a"]})


def test_goal_runs_in_dependency_order_and_verifies(make_aether):
    llm = FakeLLM(reply="Done, see results.", structured={
        "GoalPlan": plan([
            {"key": "t1", "agent": "memory", "description": "remember that the exam is on friday",
             "success_criteria": "memory saved"},
            {"key": "t2", "agent": "reasoning", "description": "plan study days", "depends_on": ["t1"],
             "success_criteria": "a plan exists"}]),
        "ToolPlan": lambda m: {"calls": [{"tool": "remember", "args": {"text": "exam on friday"}}]}
        if "memory agent" in m[0].content else {"calls": []},
        "Verdict": {"passed": True, "reason": "ok"}})
    a = make_aether(llm)
    goal = executive_for(a, llm).run("prepare for my exam")
    assert goal["status"] == "done", goal["report"]
    assert any("exam on friday" in m.text for m in a.store.list("semantic"))
    assert goal["report"].index("remember") < goal["report"].index("plan study")


def test_invalid_plan_is_rejected_symbolically(make_aether):
    llm = FakeLLM(structured={"GoalPlan": plan([
        {"key": "t1", "agent": "hacker", "description": "x"},
        {"key": "t2", "agent": "research", "description": "y", "depends_on": ["t9"]}])})
    goal = executive_for(make_aether(llm), llm).run("do something")
    assert goal["status"] == "failed" and "unknown agent hacker" in goal["report"] and "missing t9" in goal["report"]


def test_declined_action_is_not_retried_and_goal_fails_honestly(make_aether, cfg):
    calls = []
    llm = FakeLLM(structured={
        "GoalPlan": plan([{"key": "t1", "agent": "operator", "description": "create notes file",
                           "success_criteria": "file exists"}]),
        "ToolPlan": {"calls": [{"tool": "fs_create", "args": {"path": cfg.security.allowed_write_roots[0] + "/n.md",
                                                               "content": "x"}}]},
        "Verdict": {"passed": True, "reason": "ok"}})
    a = make_aether(llm, confirm=lambda p: calls.append(p) or False)
    goal = executive_for(a, llm).run("make a notes file")
    assert goal["status"] == "failed"
    assert len(calls) == 2  # asked once per plan (original + one re-plan), never re-asked within a task


def test_rejected_output_gets_retried_with_feedback(make_aether):
    verdicts = iter([{"passed": False, "reason": "no numbers", "missing": ["figures"]},
                     {"passed": True, "reason": "ok"}])
    llm = FakeLLM(structured={
        "GoalPlan": plan([{"key": "t1", "agent": "reasoning", "description": "estimate costs"}]),
        "ToolPlan": {"calls": []},
        "Verdict": lambda m: next(verdicts)})
    a = make_aether(llm)
    goal = executive_for(a, llm).run("estimate costs")
    t = a.graph.tasks(goal["id"])[0] if a.graph.tasks(goal["id"]) else TaskGraph(a.db, a.lock).tasks(goal["id"])[0]
    assert goal["status"] == "done" and t.attempts == 2
    assert any("Previous attempt was rejected: no numbers" in str(c[0][-1].content) for c in llm.calls if c[1] is None)
