from conftest import FakeLLM
from fastapi.testclient import TestClient

from aether.agents.executive import Executive
from aether.agents.taskgraph import TaskGraph


def test_turns_are_measured_and_feedback_is_inferred(make_aether):
    a = make_aether(FakeLLM(reply="Paris is the capital of France."))
    r = a.handle("what is the capital of france?")
    assert r.state.latency_ms > 0
    s = a.meta.summary()
    assert s["turns"] == 1 and s["feedback"] == {"positive": 0, "negative": 0}
    r2 = a.handle("no, that's wrong")
    assert any("missed" in n for n in r2.notices)
    a.handle("perfect, thanks")
    s = a.meta.summary()
    assert s["turns"] == 3 and s["feedback"] == {"positive": 1, "negative": 1}


def test_tool_outcomes_recorded_but_policy_blocks_are_not_failures(make_aether, tmp_path):
    a = make_aether()
    a.tools.call("fs_read", {"path": str(tmp_path / "nowhere" / "x.txt")})  # outside roots: deny
    for _ in range(5):
        a.tools.call("fs_read", {"path": a.cfg.security.allowed_read_roots[0] + "/missing.txt"})
    failing = a.meta._failing_tools()
    assert failing and failing[0][0] == "fs_read" and failing[0][2] == 5


def test_proposals_need_approval_and_respect_whitelist(make_aether):
    a = make_aether(FakeLLM())
    a.cfg.llm.temperature = 0.7
    with a.lock:
        for i in range(20):  # simulate a fortnight where the verifier keeps correcting answers
            a.db.execute("INSERT INTO turn_metrics(t, latency_ms, intent, routes, tools, tool_failures, denied, revised,"
                         " fake_citations, confidence, wellness, answer_chars, feedback) VALUES"
                         " (strftime('%s','now'), 30000, 'q', 'direct', 0, 0, 0, ?, ?, 0.7, 'normal', 100, ?)",
                         (int(i % 3 == 0), int(i < 3), -1 if i < 6 else 0))
        a.db.commit()
    props = a.meta.propose(a.cfg)
    titles = [p.title for p in props]
    assert any("temperature" in t for t in titles)
    assert any("Retrieve" in t for t in titles)
    assert any("careful" in t for t in titles)
    assert any("cite" in t for t in titles)
    assert a.meta.propose(a.cfg) == []  # no duplicates while open
    assert a.cfg.llm.temperature == 0.7  # nothing applied yet
    temp = next(p for p in props if "temperature" in p.title)
    assert "Applied" in a.meta.decide(temp.id, True, a.settings, a.prefs, a.store)
    assert a.cfg.llm.temperature == 0.5
    cite = next(p for p in props if "cite" in p.title)
    a.meta.decide(cite.id, True, a.settings, a.prefs, a.store)
    assert any("citation" in p.instruction for p in a.prefs.all())
    careful = next(p for p in props if "careful" in p.title)
    a.meta.decide(careful.id, False, a.settings, a.prefs, a.store)
    assert "No open proposal" in a.meta.decide(careful.id, True, a.settings, a.prefs, a.store)


def test_finished_goal_gets_self_review_and_lesson(make_aether):
    llm = FakeLLM(reply="Done.", structured={
        "GoalPlan": {"tasks": [{"key": "t1", "agent": "reasoning", "description": "plan study days",
                                "success_criteria": "a plan exists"}]},
        "ToolPlan": {"calls": []},
        "Verdict": {"passed": True, "reason": "ok"},
        "SelfReview": {"went_well": "clear plan", "went_wrong": "nothing", "lesson": "split study into 45 min blocks",
                       "score": 0.9}})
    a = make_aether(llm)
    ex = Executive(llm, a.tools, a.store, a.rules, TaskGraph(a.db, a.lock),
                   on_finish=lambda g: a.meta.review_goal(llm, g, a.store))
    g = ex.run("plan my exam week")
    assert g["status"] == "done"
    rev = a.meta.reviews()[0]
    assert rev["score"] == 0.9 and "45 min" in rev["lesson"]
    assert any("45 min" in m.text for m in a.store.list("procedural"))


def test_failed_goal_self_score_is_capped(make_aether):
    llm = FakeLLM(structured={"SelfReview": {"went_well": "x", "went_wrong": "y", "lesson": "", "score": 1.0}})
    a = make_aether(llm)
    r = a.meta.review_goal(llm, {"id": 7, "text": "t", "status": "failed", "report": "r"})
    assert r.score == 0.5


def test_metrics_api(make_aether, monkeypatch):
    from aether.interface import server
    a = make_aether(FakeLLM())
    monkeypatch.setattr(server, "build", lambda *x, **k: (a, None))
    c = TestClient(server.create_app())
    c.post("/api/chat", json={"text": "hello"})
    assert c.post("/api/feedback", json={"value": 1}).status_code == 200
    m = c.get("/api/metrics").json()
    assert m["turns"] == 1 and m["feedback"]["positive"] == 1
    assert c.get("/api/proposals").json() == []
    assert c.get("/api/reviews").status_code == 200
