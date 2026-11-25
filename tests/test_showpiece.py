"""Glass-box traces, counterfactual reasoning, belief time travel, and their APIs."""
import sqlite3
import threading
import time

import pytest
from conftest import FakeLLM
from test_webapp import StreamingLLM, client, events

from aether.neurosymbolic.kg import KnowledgeGraph
from aether.neurosymbolic.rules import DEFAULT_RULES
from aether.neurosymbolic.whatif import parse, what_if


def kg():
    return KnowledgeGraph(sqlite3.connect(":memory:", check_same_thread=False), threading.Lock())


# ------------------------------------------------------------------ traces
def test_every_turn_leaves_an_encrypted_trace(make_aether, cfg):
    cfg.fast_mode = False   # the full path, so the trace shows every model pass
    a = make_aether(FakeLLM())
    raw = a.llm
    r = a.handle("what is the capital of france?")
    assert a.llm is raw  # the tracing wrapper is removed after the turn
    t = a.traces.get(r.turn_id)
    assert t["request"] == "what is the capital of france?" and t["answer"] == r.text
    stages = [e["name"] for e in t["events"] if e["kind"] == "stage"]
    assert stages[:4] == ["perceive", "interpret", "retrieve", "reason"] and "verify" in stages
    assert all(e["end"] >= e["start"] for e in t["events"])
    assert t["llm_calls"] and t["llm_calls"][0]["purpose"] == "IntentIR"
    assert t["total_ms"] >= max(c["end"] for c in t["llm_calls"])
    payload = a.db.execute("SELECT payload FROM traces WHERE turn_id=?", (r.turn_id,)).fetchone()[0]
    assert "capital of france" not in payload  # encrypted at rest like the rest of the history
    assert a.traces.recent(5)[0]["turn_id"] == r.turn_id


def test_trace_records_streaming_first_token_and_reasoning(make_aether):
    a = make_aether(StreamingLLM())
    a.kg.assert_fact("riya", "parent_of", "kabir")
    a.kg.assert_fact("kabir", "parent_of", "zoe")
    a._streaming = True
    try:
        r = a.handle("who is riya's grandchild?", on_event=lambda *_: None)
    finally:
        a._streaming = False
    t = a.traces.get(r.turn_id)
    streamed = [c for c in t["llm_calls"] if c["purpose"] == "answer (streamed)"]
    assert streamed and streamed[0]["first_token_ms"] is not None
    reason = next(e for e in t["events"] if e["name"] == "reason")["data"]
    d = next(x for x in reason["derived"] if x["fact"] == "riya grandparent_of zoe")
    assert d["because"] == ["riya parent_of kabir", "kabir parent_of zoe"]


# ------------------------------------------------------------------ what if
def test_what_if_derives_new_conclusions_without_touching_memory():
    g = kg()
    g.assert_fact("riya", "parent_of", "kabir")
    g.assert_fact("riya", "lives_in", "pune")
    before = [(t.s, t.p, t.o) for t in g.all()]
    r = what_if(g, DEFAULT_RULES, "kabir parent_of zoe; riya lives_in delhi")
    assert {"fact": "riya grandparent_of zoe", "rule": "parent_of(?g, ?p) & parent_of(?p, ?c) -> grandparent_of(?g, ?c)",
            "because": ["riya parent_of kabir", "kabir parent_of zoe"]} in r.gained
    assert r.overridden == ["riya lives_in pune"]  # lives_in is functional: one city at a time
    assert [(t.s, t.p, t.o) for t in g.all()] == before
    assert "riya grandparent_of zoe" in r.render() and "Nothing in your real memory was changed" in r.render()


def test_what_if_retraction_loses_dependent_conclusions_and_constraints():
    g = kg()
    g.assert_fact("meera", "parent_of", "riya")
    g.assert_fact("riya", "parent_of", "kabir")
    g.assert_fact("user", "likes", "coffee")
    g.assert_fact("user", "dislikes", "coffee")
    r = what_if(g, DEFAULT_RULES, "!riya parent_of kabir; !user dislikes coffee")
    assert r.retracted == ["riya parent_of kabir", "user dislikes coffee"]
    assert [x["fact"] for x in r.lost] == ["meera grandparent_of kabir"]
    assert r.resolved_violations and not r.new_violations


def test_what_if_reports_new_constraint_violations():
    g = kg()
    g.assert_fact("a", "depends_on", "b")
    r = what_if(g, DEFAULT_RULES, "b depends_on a")
    assert any("circular_dependency" in v for v in r.new_violations)


def test_what_if_parse_rejects_garbage():
    assert parse("Riya  Sharma lives_in New Delhi") == [(True, "riya_sharma", "lives_in", "new_delhi")]
    with pytest.raises(ValueError):
        parse("what if riya moved")


def test_what_if_is_a_tool(make_aether):
    a = make_aether(FakeLLM())
    a.kg.assert_fact("riya", "parent_of", "kabir")
    out = a.tools.call("what_if", {"assumption": "kabir parent_of zoe"}).output
    assert "riya grandparent_of zoe" in out


# ------------------------------------------------------------------ time travel
def test_as_of_history_and_snapshot_at():
    g = kg()
    g.assert_fact("user", "lives_in", "pune")
    time.sleep(0.01)
    mid = time.time()
    time.sleep(0.01)
    g.assert_fact("user", "lives_in", "delhi", replace=True)
    assert [(t.o) for t in g.as_of(mid)] == ["pune"]
    assert [(t.o) for t in g.query("user", "lives_in", None)] == ["delhi"]
    h = g.history("user")
    assert [e["o"] for e in h] == ["pune", "delhi"] and h[0]["retired"] and h[1]["retired"] is None
    assert [t.o for t in g.snapshot_at(mid).all()] == ["pune"]
    assert g.history(limit=1)[0]["o"] == "delhi"  # the limit keeps the most recent events


# ------------------------------------------------------------------ APIs
def test_trace_and_reasoning_apis(make_aether):
    a, c = client(make_aether)
    a.kg.assert_fact("riya", "parent_of", "kabir")
    a.kg.assert_fact("riya", "lives_in", "pune")
    mid = time.time()
    time.sleep(0.01)
    a.kg.assert_fact("riya", "lives_in", "delhi", replace=True)
    done = events(c.post("/api/chat/stream", json={"text": "where does riya live?"}))[-1][1]
    tr = c.get(f"/api/traces/{done['turn_id']}").json()
    assert tr["request"] == "where does riya live?" and tr["events"]
    assert c.get("/api/traces").json()[0]["turn_id"] == done["turn_id"]
    assert c.get("/api/traces/99999").status_code == 404

    w = c.post("/api/whatif", json={"text": "kabir parent_of zoe"}).json()
    assert w["gained"][0]["fact"] == "riya grandparent_of zoe" and "text" in w
    assert c.post("/api/whatif", json={"text": "nonsense"}).status_code == 400

    past = c.get(f"/api/graph?at={mid}").json()
    now = c.get("/api/graph").json()
    lives = lambda g: {l["target"] for l in g["links"] if l["label"] == "lives_in"}
    assert lives(past) == {"pune"} and lives(now) == {"delhi"}
    hist = c.get("/api/kg/history?entity=riya").json()
    assert [e["o"] for e in hist["events"] if e["p"] == "lives_in"] == ["pune", "delhi"] and hist["first"] <= hist["now"]


def test_upload_endpoints_accept_files(make_aether):
    """Regression: UploadFile must be resolvable, or every upload endpoint returns 500."""
    _, c = client(make_aether)
    r = c.post("/api/transcribe", files={"file": ("s.webm", b"x" * 3000, "audio/webm")})
    assert r.status_code in (200, 503)  # 503 when the optional speech extra isn't installed


def test_new_static_modules_are_served(make_aether):
    _, c = client(make_aether)
    for f in ("inspector.js", "dag.js", "hud.js"):
        r = c.get("/static/" + f)
        assert r.status_code == 200 and "export" in r.text
