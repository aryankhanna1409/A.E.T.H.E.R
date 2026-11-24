import math

import pytest
from fastapi.testclient import TestClient

from aether.optimization.advanced import (MOProblem, assignment_neighbours, crowding_distance,
                                          differential_evolution, fast_nondominated_sort, nsga2,
                                          pareto_schedule, tabu_search)
from aether.optimization.engine import parse_tasks, schedule_problem
from aether.optimization.tuning import EvalCase, score


def test_nondominated_sort_and_crowding():
    keys = [(0, (1, 5)), (0, (2, 2)), (0, (5, 1)), (0, (3, 3)), (1, (0, 0))]  # last is infeasible
    fronts = fast_nondominated_sort(keys)
    assert sorted(fronts[0]) == [0, 1, 2] and fronts[1] == [3] and fronts[-1] == [4]
    cd = crowding_distance([(1, 5), (2, 2), (5, 1)])
    assert cd[0] == math.inf and cd[2] == math.inf and 0 < cd[1] < math.inf


def test_nsga2_finds_known_pareto_front():
    # Schaffer-like binary problem: x in 0..15, minimize (x, 15-x): every x is Pareto optimal
    prob = MOProblem("toy", lambda r: [r.randint(0, 15)], lambda s, r: [min(15, max(0, s[0] + r.choice([-1, 1])))],
                     lambda a, b, r: [a[0]], lambda s: (s[0], 15 - s[0]), ["a", "b"])
    res = nsga2(prob, pop_size=24, generations=40)
    xs = sorted(s[0] for s, _ in res.front)
    assert len(xs) >= 10 and xs[0] <= 2 and xs[-1] >= 13


def test_pareto_schedule_offers_tradeoffs():
    out = pareto_schedule("essay:3:5:Wed; gym:1:2:Sun; slides:2:4:Fri; reading:2:1:Sun; taxes:2:3:Thu")
    assert "Pareto front" in out and "Option 2" in out and "weekend h" in out
    assert "No plan" in pareto_schedule("huge:30:5:Mon")


def test_tabu_search_solves_schedule():
    prob = schedule_problem(parse_tasks("a:3:5:Mon; b:3:4:Tue; c:3:3:Wed; d:2:2:Thu"), [4] * 7)
    res = tabu_search(prob, assignment_neighbours(7), iters=60)
    assert res.violations == [] and res.algorithm == "tabu search"


def test_differential_evolution_optimum():
    best, f, hist = differential_evolution(lambda x: -((x[0] - 0.3) ** 2 + (x[1] + 1) ** 2), [(-2, 2), (-2, 2)])
    assert abs(best[0] - 0.3) < 0.02 and abs(best[1] + 1) < 0.02 and hist[-1] >= hist[0]


def test_retrieval_tuning_improves_and_needs_approval(make_aether):
    a = make_aether()
    a.store.add("Aryan's flask app crashes on startup because of a circular import", "semantic", "user_stated")
    a.store.add("Aryan prefers dark roast coffee in the morning", "semantic", "user_stated")
    a.store.add("The fusion reactor notes say tokamak confinement is the hard part", "semantic", "user_stated")
    a.evals.add("why does my flask app crash", "circular import")
    a.evals.add("what coffee do I like", "dark roast")
    a.evals.add("hello there", "")
    a.evals.add("the weather is hard today", "")
    a.cfg.memory.rag_min_score = 0.05  # deliberately bad: retrieves noise for small talk
    before = score(a.store, a.evals.all(), a.cfg.memory.hybrid_alpha, 0.05)
    r = a.tune_retrieval()
    assert r["f1_after"] > before and r["worth_applying"]
    assert a.cfg.memory.rag_min_score == 0.05  # only a proposal so far
    changed = a.settings.apply("memory", r["proposed"], "test")
    assert changed and a.cfg.memory.rag_min_score == r["proposed"]["rag_min_score"]
    b = make_aether()  # overrides persist across restarts
    assert b.cfg.memory.rag_min_score == r["proposed"]["rag_min_score"]
    assert "restored" in b.settings.revert("memory", "rag_min_score")
    with pytest.raises(PermissionError):
        a.settings.apply("security", {"screen_access": True}, "sneaky")


def test_tuning_needs_cases(make_aether):
    with pytest.raises(ValueError):
        make_aether().tune_retrieval()


def test_optimize_api(make_aether, monkeypatch):
    from aether.interface import server
    a = make_aether()
    monkeypatch.setattr(server, "build", lambda *x, **k: (a, None))
    c = TestClient(server.create_app())
    for algo in ["auto", "pareto", "tabu", "exact"]:
        tasks = "essay:3:2; slides:2:4" if algo == "exact" else "essay:3:5:Wed; slides:2:4:Fri"
        r = c.post("/api/optimize", json={"tasks": tasks, "algorithm": algo})
        assert r.status_code == 200 and r.json()["text"], algo
    assert c.post("/api/tune").status_code == 400
    assert c.post("/api/settings", json={"section": "security", "values": {"shell_allow": ["rm"]}}).status_code == 403


def test_body_models_resolve(make_aether, monkeypatch):
    """Request bodies must be read from JSON, not mistaken for query params."""
    from aether.interface import server
    a = make_aether()
    monkeypatch.setattr(server, "build", lambda *x, **k: (a, None))
    c = TestClient(server.create_app())
    assert c.post("/api/ask", json={"text": "parent_of(?p, riya)"}).status_code == 200
    assert c.post("/api/eval", json={"query": "hi", "expect": ""}).status_code == 200
