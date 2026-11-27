import json
import time

from aether.memory.atlas import adamic_adar, betweenness, category, louvain, modularity, pagerank
from aether.memory import seedpack


def _two_cliques():
    adj = {}
    for grp in (["a", "b", "c", "d"], ["w", "x", "y", "z"]):
        for i in grp:
            adj.setdefault(i, {})
            for j in grp:
                if i != j:
                    adj[i][j] = 1.0
    adj["d"]["w"] = adj["w"]["d"] = 1.0                       # one bridge
    return adj


# ---------------------------------------------------------------- graph analytics
def test_graph_analytics_find_communities_bridges_and_missing_links():
    adj = _two_cliques()
    c = louvain(adj)
    assert c["a"] == c["b"] == c["c"] == c["d"] and c["w"] == c["x"] == c["y"] == c["z"] and c["a"] != c["w"]
    assert modularity(adj, c) > 0.35
    pr = pagerank(adj)
    assert abs(sum(pr.values()) - 1) < 1e-6 and pr["d"] > pr["a"]
    bt = betweenness(adj)
    assert max(bt, key=bt.get) in ("d", "w") and bt["a"] == 0
    score, common = adamic_adar(adj, "a", "w")
    assert common == ["d"] and score > 0


def test_memories_are_filed_into_areas_of_your_life():
    assert category("Alex started 75 Hard and tracks protein every day") == "health"
    assert category("Ravi is my classmate") == "people"
    assert category("Alex wants direct answers with numbers") == "assistant"
    assert category("Alex likes quiet cafes with Japanese interiors") == "prefs"
    assert category("Alex is an incoming FDE intern at Acme") == "career"
    assert category("Week 2 is TypeScript and FastAPI courses") == "study"
    assert category("anything", "semantic", {"category": "goals"}) == "goals"


# ---------------------------------------------------------------- the memory map
def _world(a):
    a.cfg.user_name = "Alex"
    a.kg.assert_fact("Alex", "lives_in", "Pune")
    a.kg.assert_fact("Alex", "teammate_of", "Sam Lee")
    a.kg.assert_fact("Alex", "teammate_of", "Priya")
    a.kg.assert_fact("Alex", "works_on", "Rescue Robot")
    a.kg.assert_fact("Bob Smith", "sister", "Jane Smith")       # a demo world with nothing to do with Alex
    a.kg.assert_fact("Bob Smith", "lives_in", "Leeds")
    for t in ["Sam Lee and Priya built the Rescue Robot cameras with Alex.",
              "Sam Lee and Priya tuned the YOLO model for the Rescue Robot.",
              "Alex started 75 Hard on 23 November and tracks protein daily.",
              "Alex started 75 Hard on 23 November and tracks protein daily!",
              "Alex wants direct answers with numbers."]:
        a.store.add(t, "semantic", "user_stated", 0.9)


def test_memory_map_centres_on_you_and_finds_structure(make_aether):
    a = make_aether()
    _world(a)
    m = a.atlas.build()
    ids = {n["id"] for n in m["nodes"]}
    assert "e:user" in ids and "e:alex" not in ids                       # your name and "user" are one node
    assert "e:bob_smith" not in ids and m["stats"]["hidden"] >= 3          # unrelated demo world hidden
    assert {"c:people", "c:health", "c:assistant"} <= ids                 # areas of your life hang off you
    assert any(n["id"] == "e:sam_lee" and n["kind"] == "person" for n in m["nodes"])
    ins = m["insights"]
    assert ins["duplicates"] and ins["duplicates"][0]["similarity"] >= 0.8
    pair = {(x["s_key"], x["t_key"]) for x in ins["suggestions"]}
    assert ("priya", "sam_lee") in pair or ("sam_lee", "priya") in pair
    assert m["communities"] and m["stats"]["modularity"] > 0
    everything = a.atlas.build(focus="all")
    assert any(n["id"] == "e:bob_smith" for n in everything["nodes"])
    t = time.time()
    assert a.atlas.build() is m and time.time() - t < 0.05              # cached until something changes
    node = a.atlas.node("e:sam_lee")
    assert node["facts"] and node["memories"] and node["timeline"]


def test_memory_map_fixes_what_aether_knows(make_aether):
    a = make_aether()
    _world(a)
    ins = a.atlas.build()["insights"]
    x = ins["suggestions"][0]
    assert a.atlas.act("link", {"s": x["s_key"], "o": x["t_key"]})["ok"]
    assert a.kg.query(x["s_key"], "related_to", x["t_key"])
    d = ins["duplicates"][0]
    a.atlas.act("merge", {"keep": int(d["keep"]["id"][2:]), "drop": int(d["drop"]["id"][2:])})
    assert len([m for m in a.store.list(None, 100) if "75 Hard" in m.text]) == 1
    out = a.atlas.act("prune", {})
    assert "Retired 2 facts" in out["message"] and not a.kg.query("bob_smith", None, None)
    assert a.kg.history("bob_smith")                                       # still in the belief history
    reply = a.handle("show my memory map").text
    assert "Memory map" in reply and "God nodes" in reply


# ---------------------------------------------------------------- RSI and RSI²
def _memories(a, n=24):
    topics = ["robot lidar slam", "protein breakfast dosa", "acme internship city", "aws exam certification",
              "neetcode arrays hashing", "kokoro voice synthesis", "grind habits water", "portfolio nextjs vercel"]
    for i in range(n):
        t = topics[i % len(topics)]
        a.store.add(f"Note {i}: Alex's {t} detail number{i} keyword{i} unique{i} zeta{i}.", "semantic", "user_stated")


def test_self_improvement_is_bounded_measured_and_needs_your_ok(make_aether):
    a = make_aether()
    assert "not enough memories" in a.rsi.cycle("test", seed=1)["outcome"]
    _memories(a)
    a.cfg.memory.rag_min_score = 0.79           # a bad setting: retrieves almost nothing
    a.cfg.memory.rag_top_k = 2
    before = (a.cfg.memory.hybrid_alpha, a.cfg.memory.rag_min_score, a.cfg.memory.rag_top_k)
    r = a.rsi.cycle("test", seed=3)
    assert r["cases"] >= 20 and r["evaluated"] >= 1 and r["hold_before"] is not None
    assert (a.cfg.memory.hybrid_alpha, a.cfg.memory.rag_min_score, a.cfg.memory.rag_top_k) == before   # nothing applied
    if r["proposal_id"]:
        assert r["hold_after"] >= r["hold_before"] + 0.02 and r["train_after"] >= r["train_before"] + 0.02
        for k, v in r["proposed"].items():
            lo, hi = a.rsi.info()["bounds"][k]
            assert lo <= v <= hi
        msg = a.meta.decide(r["proposal_id"], True, a.settings, a.prefs, a.store)
        assert "Applied" in msg and a.cfg.memory.rag_min_score == r["proposed"]["rag_min_score"]
    info = a.rsi.info()
    assert sum(v["tries"] for v in info["arms"].values()) > 0                 # level 2 learned from this cycle
    assert info["cycles"][0]["id"] == r["id"] and len(info["rails"]) >= 4
    assert 6 <= info["budget"] <= 24


def test_rsi_meta_level_adapts_strategy_step_and_budget(make_aether):
    a = make_aether()
    _memories(a)
    s0 = a.rsi.sigmas()
    for i in range(3):
        a.rsi.cycle("test", seed=10 + i)
    s1 = a.rsi.sigmas()
    assert s0 != s1                                                          # 1/5 rule moved the step sizes
    arms = a.rsi.arms()
    assert any(t > 0 for _, t in arms.values())
    assert "Self-improvement cycle" in a.handle("improve yourself").text


# ---------------------------------------------------------------- seed packs, cache busting, server
def test_seed_pack_carries_areas_and_plans(make_aether, tmp_path):
    a = make_aether()
    pack = {"memories": [{"text": "Alex does 75 Hard daily", "category": "health"}],
            "plans": "# Plans\n## Now · until 2027-01-18\n- [ ] Solve NeetCode\n"}
    out = seedpack.apply(a, pack)
    assert out["memories"] == 1 and out["plans"] == 1 and "NeetCode" in a.plans.text()
    assert a.store.list(None, 10)[0].meta["category"] == "health"
    assert seedpack.apply(a, {"plans": "# other"})["plans"] == 0              # never overwrites your plans


def test_server_serves_the_map_rsi_and_fresh_assets(make_aether):
    from fastapi.testclient import TestClient
    from aether.core.version import MARK, code_version
    from aether.interface import server
    a = make_aether()
    _world(a)
    server.build = lambda *x, **k: (a, None)
    c = TestClient(server.create_app())
    page = c.get("/")
    assert f"/static/app.js?v={code_version()[:12]}" in page.text and "no-store" in page.headers["cache-control"]
    assert c.get("/static/app.css").headers["cache-control"] == "no-cache"
    assert MARK == "Mark XXX" and "MK XXX" in page.text
    m = c.get("/api/atlas").json()
    assert m["stats"]["nodes"] > 5 and "insights" in m
    assert c.get("/api/atlas/node", params={"id": "e:user"}).status_code == 200
    assert c.get("/api/atlas/node", params={"id": "e:nope"}).status_code == 404
    assert c.post("/api/atlas/act", json={"op": "explode"}).status_code == 400
    assert "rails" in c.get("/api/rsi").json()
    assert c.post("/api/rsi/run").json()["started"] is True
