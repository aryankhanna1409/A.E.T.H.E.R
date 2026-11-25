import time

from conftest import FakeLLM

from aether.memory import lifecycle
from aether.memory.prospective import Reminders
from aether.memory.store import MemoryStore


class TinyEmbedder:
    name, dim = "tiny", 3
    VOCAB = ["drone", "food", "exam"]

    def embed(self, texts):
        return [[float(w in t.lower()) + 0.01 for w in self.VOCAB] for t in texts]


def test_vector_index_ranks_semantic_match(tmp_path):
    s = MemoryStore(tmp_path / "m.db", TinyEmbedder())
    s.add("the drone project", "semantic", "user_stated")
    s.add("favourite food is pasta", "semantic", "user_stated")
    assert s.search("drone launch", k=1)[0].text == "the drone project"
    s.add("exam on monday", "semantic", "user_stated")  # index must refresh after a write
    assert s.search("exam", k=1)[0].text == "exam on monday"


def test_reminders_resolve_dates_and_dedupe(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    r = Reminders(s.db, s.lock)
    a = r.add("submit essay", "tomorrow")
    b = r.add("submit essay", "tomorrow")
    assert a.id == b.id and len(r.open()) == 1
    assert r.due() == []  # due tomorrow, not today


def test_inferred_observations_decay_but_facts_do_not(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    obs = s.add("seems stressed about exams", "observation", "inferred", 0.5)
    fact = s.add("exam is on monday", "semantic", "user_stated", 0.9)
    old = time.time() - 60 * 86400
    s.db.execute("UPDATE memories SET updated=?, last_access=?", (old, old))
    s.db.commit()
    out = lifecycle.decay(s)
    ids = {m.id for m in s.list()}
    assert obs not in ids and fact in ids and out["removed"] == 1


def test_extraction_stores_only_important_items(make_aether):
    llm = FakeLLM(structured={"Extraction": {
        "facts": [{"subject": "user", "predicate": "studies_at", "object": "DTU"}],
        "reminders": [{"text": "pay hostel fees", "when": "friday"}],
        "routines": [], "importance": 0.8, "kind": "personal"}})
    a = make_aether(llm)
    r = a.handle("I study at DTU and I need to pay my hostel fees by friday")
    assert [t.o for t in a.kg.query("user", "studies_at", None)] == ["dtu"]
    assert a.reminders.open()[0].text == "pay hostel fees"
    assert any("Reminder noted" in n for n in r.notices)
    before = len(llm.calls)
    r2 = a.handle("lol ok")  # small talk: no extraction call at all
    assert not any(c[1] and c[1].get("title") == "Extraction" for c in llm.calls[before:])
