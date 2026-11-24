"""The web app's backend: live streaming, conversations, search, graph, static files."""
import json

from conftest import FakeLLM
from fastapi.testclient import TestClient

from aether.cognition.perception import is_small_talk


class StreamingLLM(FakeLLM):
    def stream(self, messages, **kw):
        for w in ["Paris ", "is ", "the ", "capital."]:
            yield w


def client(make_aether, llm=None):
    from aether.interface import server
    a = make_aether(llm or StreamingLLM())
    server.build = lambda *x, **k: (a, None)
    return a, TestClient(server.create_app())


def events(resp):
    out = []
    for block in resp.text.strip().split("\n\n"):
        lines = dict(l.split(": ", 1) for l in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_stream_emits_stages_tokens_and_done(make_aether):
    a, c = client(make_aether)
    ev = events(c.post("/api/chat/stream", json={"text": "what is the capital of france?"}))
    kinds = [k for k, _ in ev]
    stages = [d["name"] for k, d in ev if k == "stage" and d.get("done")]
    assert stages == ["perceive", "interpret", "retrieve", "reason", "generate", "verify"]
    assert "".join(d["text"] for k, d in ev if k == "token") == "Paris is the capital."
    assert kinds[-1] == "done" and ev[-1][1]["text"] == "Paris is the capital." and ev[-1][1]["turn_id"]


def test_stream_replace_when_preferences_change_text(make_aether):
    class Emoji(StreamingLLM):
        def stream(self, messages, **kw):
            yield "Sure! 😀 Here you go."
    a, c = client(make_aether, Emoji())
    a.prefs.learn("stop using emojis, I hate them")
    ev = events(c.post("/api/chat/stream", json={"text": "tell me a fact about rome"}))
    replaced = [d for k, d in ev if k == "replace"]
    assert replaced and "😀" not in replaced[0]["text"] and "😀" not in ev[-1][1]["text"]


def test_stream_reports_errors(make_aether):
    a, c = client(make_aether)
    a.llm = None
    ev = events(c.post("/api/chat/stream", json={"text": "explain gravity please"}))
    assert ev[-1][0] == "error" and "model" in ev[-1][1]["message"].lower()


def test_conversations_list_open_search_delete(make_aether):
    a, c = client(make_aether)
    c.post("/api/chat/stream", json={"text": "my helios drone battery question"})
    first = a.session
    assert c.post("/api/conversations").json()["session"] != first
    c.post("/api/chat/stream", json={"text": "something about pasta"})
    items = c.get("/api/conversations").json()["items"]
    assert len(items) == 2 and {i["title"] for i in items} >= {"my helios drone battery question"}
    turns = c.get(f"/api/conversations/{first}").json()["turns"]
    assert turns[0]["text"] == "my helios drone battery question" and a.session == first
    assert a.wm.history(a.topics.current.id)  # replayed into working memory
    hits = c.get("/api/search?q=helios").json()["conversations"]
    assert hits and hits[0]["session"] == first
    c.patch(f"/api/conversations/{first}", json={"pinned": True, "title": "Drone"})
    assert c.get("/api/conversations").json()["items"][0]["title"] == "Drone"
    c.delete(f"/api/conversations/{first}")
    assert len(c.get("/api/conversations").json()["items"]) == 1


def test_titles_and_turns_encrypted(make_aether, cfg):
    a, c = client(make_aether)
    c.post("/api/chat/stream", json={"text": "my secret project codename is bluebird"})
    assert b"bluebird" not in cfg.db_path.read_bytes()


def test_graph_search_brief_reminders_config(make_aether):
    a, c = client(make_aether)
    a.kg.assert_fact("riya", "parent_of", "kabir", source="user_stated")
    a.kg.assert_fact("kabir", "parent_of", "mira", source="user_stated")
    g = c.get("/api/graph").json()
    assert {"riya", "kabir", "mira"} <= {n["id"] for n in g["nodes"]}
    assert any(l["kind"] == "derived" for l in g["links"])  # grandparent rule
    assert not any(t.p == "grandparent_of" for t in a.kg.all())  # viewing the graph does not persist derivations
    assert c.get("/api/search?q=kabir").json()["facts"]
    r = c.post("/api/reminders", json={"text": "call mom", "when": "friday"}).json()
    assert r["text"] == "call mom" and "call mom" in c.get("/api/brief").json()["text"] + str(c.get("/api/brief").json()["reminders"])
    cfg = c.get("/api/config").json()
    assert cfg["tools"] and "memory" in cfg["tunable"] and "security" not in cfg["tunable"]


def test_static_app_is_served(make_aether):
    a, c = client(make_aether)
    html = c.get("/").text
    assert "/static/app.js" in html and 'type="module"' in html
    for f in ["app.js", "app.css", "md.js", "graph.js"]:
        r = c.get("/static/" + f)
        assert r.status_code == 200 and len(r.text) > 1000, f
    assert "http" not in html.replace("http://www.w3.org", "")  # no CDN: works offline


def test_small_talk_fast_path_skips_interpretation_call(make_aether):
    assert is_small_talk("hey!") and is_small_talk("thanks a lot") and is_small_talk("Good morning Aether")
    assert not is_small_talk("hey can you delete my files") and not is_small_talk("thanks, now plan my week")
    llm = FakeLLM()
    a = make_aether(llm)
    a.handle("hey!")
    assert not any(s and s.get("title") == "IntentIR" for _, s in llm.calls)
