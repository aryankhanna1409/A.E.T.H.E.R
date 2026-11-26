import sqlite3
import threading
import time

from aether.cognition.intent_model import IntentModel, hashed
from aether.cognition.perception import perceive
from aether.neurosymbolic.mining import mine_habits, mine_rules


class KeywordEmbedder:
    """Deterministic stand-in for bge-small: similar wording -> similar vectors."""
    def embed(self, texts):
        return [hashed(" ".join(w for w in t.lower().split() if len(w) > 3)) for t in texts]


def test_learned_router_distils_the_model_and_defers_when_unsure():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    m = IntentModel(db, threading.Lock(), KeywordEmbedder(), min_examples=20, min_accuracy=0.0)
    for i in range(15):
        m.record(f"latest news about topic{i} today", ["web"])
        m.record(f"what did I tell you about friend{i} before", ["memory"])
    m.record("open notepad", ["tools"])
    m.train()
    guess = m.predict("latest news about topic3 today")
    assert guess is not None and guess[0] == ["web"]
    assert m.predict("open notepad") is None                   # tools need the model's arguments
    assert m.predict("my sister is called riya") is None        # personal facts always get the full reading
    assert m.predict("completely unrelated words here xyz") is None
    assert m.info()["skipped"] >= 1


def test_perceive_skips_the_model_when_the_router_is_sure():
    calls = []

    class LLM:
        def chat(self, *a, **k):
            calls.append(1)
            raise RuntimeError("should not be called")

    class Sure:
        def predict(self, text): return (["web"], 0.95)
        def record(self, *a): pass
    ir = perceive(LLM(), "latest news on the mla-c01 exam changes please", [], ["fs_list"], learned=Sure())
    assert ir.needs == ["web"] and not calls and ir.intent.startswith("learned_")


def test_confidence_flows_through_rule_chains(make_aether):
    a = make_aether()
    a.kg.assert_fact("aryan", "works_at", "incedo", confidence=0.9)
    a.kg.assert_fact("incedo", "located_in", "pune", confidence=0.8)
    new, _ = a.rules.run(persist=False)
    d = next(x for x in new if x.triple == ("aryan", "works_in", "pune"))
    assert abs(d.confidence - 0.72) < 1e-6                     # 1.0 (rule) x 0.9 x 0.8
    a.rules.add_rule("lives_in(?x, ?c) -> works_in(?x, ?c)", persist=False, confidence=0.5)
    a.kg.assert_fact("aryan", "lives_in", "pune", confidence=1.0)
    a.rules.proofs.clear()
    new, _ = a.rules.run(persist=False)
    d = next(x for x in new if x.triple == ("aryan", "works_in", "pune"))
    assert d.proofs == 2 and abs(d.confidence - (1 - (1 - 0.72) * (1 - 0.5))) < 1e-6   # noisy-or of two proofs
    assert "confidence 0.86" in a.rules.explain(("aryan", "works_in", "pune"))[0]


def test_rule_mining_finds_a_chain_and_waits_for_ok(make_aether):
    a = make_aether()
    for person, org, city in [("a", "o1", "pune"), ("b", "o2", "mumbai"), ("c", "o3", "delhi")]:
        a.kg.assert_fact(person, "employed_by", org)
        a.kg.assert_fact(org, "based_in", city)
        a.kg.assert_fact(person, "commutes_to", city)
    rules = mine_rules(a.kg, {r.text for r in a.rules.rules})
    chain = "employed_by(?a, ?b) & based_in(?b, ?c) -> commutes_to(?a, ?c)"
    assert any(r["rule"] == chain and r["confidence"] == 1.0 and r["support"] == 3 for r in rules)
    out = a.suggestions.refresh()
    assert out["rules_suggested"] >= 1
    sid = next(s["id"] for s in a.suggestions.list() if s["text"] == chain)
    assert a.suggestions.decide(sid, True).startswith("Added")
    assert chain in [r.text for r in a.rules.rules]


def test_habit_mining_from_your_days():
    now = time.time()
    days = [now - d * 86400 for d in (1, 3, 8, 10, 15)]
    turns = [("just got back from the gym, legs are dead", t) for t in days] + [("I went to the cafe", now)]
    hs = mine_habits(turns)
    assert hs[0]["activity"] == "gym" and hs[0]["days"] == 5
    assert not any(h["activity"] == "cafe" for h in hs)


def test_document_answers_are_checked_sentence_by_sentence():
    from aether.neurosymbolic.grounding import check, footnote
    passages = ["The rover uses a Raspberry Pi 5 as the main server and a YOLOv11 model trained on 4,200 images.",
                "Batteries: a 20000 mAh power bank runs the robot for about three hours."]
    answer = ("The rover runs on a Raspberry Pi 5 as its main server. Its YOLOv11 model was trained on 9,000 images. "
              "The power bank lasts about three hours. It also has a robotic arm made of carbon fibre. "
              "Do you want the wiring diagram?")
    g = check(answer, passages)
    assert g.checked == 4
    assert any("9,000" in s for s in g.unsupported)            # wrong number: not in any passage
    assert any("carbon fibre" in s for s in g.unsupported)     # invented detail
    assert any("Raspberry Pi 5" in s for s in g.supported)
    assert "double-check" in footnote(g)
    assert check("Fine.", passages).checked == 0


def test_nudges_you_keep_dismissing_are_shown_less(make_aether):
    import random
    from datetime import datetime
    a = make_aether()
    pe = a.proactive
    pe.policy.rng = random.Random(1)
    pe.cfg.max_per_hour = 1000
    morning = datetime(2026, 11, 26, 9, 0)
    for i in range(12):
        pe._queue("break", "Time for a break?", "stand up", f"b{i}")
        got = [n for n in pe.deliver(morning) if n.kind == "break"]
        for n in got:
            pe.dismiss(n.id)
    a_, b_ = pe.policy.arm("break", "morning")
    assert b_ > a_                                    # learned that morning breaks get dismissed
    shown = 0
    for i in range(40):
        pe._queue("break", "Time for a break?", "stand up", f"later{i}")
        shown += sum(n.kind == "break" for n in pe.deliver(morning))
        a.db.execute("UPDATE notifications SET dismissed=1 WHERE kind='break'")
    assert shown < 20                                 # mostly held back now, still tried now and then
    pe._queue("reminder", "Reminder", "call mom", "r1")
    assert any(n.kind == "reminder" for n in pe.deliver(morning))   # reminders are never held back
    pe._queue("rest", "Rest day?", "rest", "rest1")
    n = next(n for n in pe.deliver(morning) if n.kind == "rest")
    pe.acted(n.id)
    assert pe.policy.arm("rest", "morning")[0] == 3.0
