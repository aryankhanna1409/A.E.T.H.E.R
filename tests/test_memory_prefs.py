import pytest

from aether.cognition.context import TopicTracker
from aether.memory.store import MemoryStore
from aether.personalization.preferences import PreferenceModel


def test_layers_provenance_and_dedupe(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    a = s.add("Aryan studies computer science", "semantic", "user_stated")
    b = s.add("aryan studies  computer science", "semantic", "user_stated")
    assert a == b and s.count("semantic") == 1
    with pytest.raises(ValueError):
        s.add("Aryan is anxious by nature", "semantic", "inferred")  # inferences never become facts


def test_hybrid_search_and_forget(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    s.add("Project Helios deadline is Friday", "semantic", "user_stated")
    s.add("Favourite food is rajma chawal", "semantic", "user_stated")
    s.add_document("Transformers use self attention over token sequences " * 5, "notes/ml.md")
    hits = s.search("when is the helios deadline", k=2)
    assert hits and "Helios" in hits[0].text
    assert s.search("self attention", layers=("document",))[0].meta["path"] == "notes/ml.md"
    s.forget(hits[0].id)
    assert all("Helios" not in m.text for m in s.list())


def test_explicit_feedback_becomes_active_preference(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    prefs = PreferenceModel(s.db, s.lock)
    updates = prefs.learn("Stop explaining things in that format. I hate those long introductions.")
    keys = {p.key for p in prefs.all() if p.active}
    assert "no_long_intros" in keys and "no_bullets" in keys and updates
    assert "Start with the answer" in prefs.prompt_block()


def test_inferred_signal_needs_repetition(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    prefs = PreferenceModel(s.db, s.lock)
    prefs.learn("that answer was too long")  # no explicit cue aimed at style
    p = prefs.get("concise")
    assert p is not None and not p.active and p.source == "inferred"
    for _ in range(3):
        prefs.learn("your reply is too long")
    assert prefs.get("concise").active


def test_filters_and_custom_ban(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    prefs = PreferenceModel(s.db, s.lock)
    prefs.learn('Never say "Great question" and stop using emojis in your replies')
    out = prefs.apply_filters("Great question! The answer is 42 🚀")
    assert out == "The answer is 42"


def test_topic_switch_and_resume():
    t = TopicTracker(0.12)
    a, e1 = t.observe("help me debug my python flask server crash")
    _, e2 = t.observe("the flask server crash happens on startup")
    b, e3 = t.observe("by the way, what should I cook for dinner tonight with paneer")
    c, e4 = t.observe("back to the flask server crash, any logs to check?")
    assert (e1, e2, e3) == ("new", "continue", "new")
    assert e4 == "resume" and c.id == a.id and b.id != a.id
