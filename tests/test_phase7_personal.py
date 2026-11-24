import time

from aether.memory.store import MemoryStore
from aether.personalization.observations import Observations, analyse
from aether.personalization.preferences import PreferenceModel


def test_observations_need_confirmation_and_respect_rejection(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    obs = Observations(s.db, s.lock)
    noon = time.mktime(time.strptime("2026-11-20 22:30", "%Y-%m-%d %H:%M"))
    for i in range(4):
        s.add(f"Expressed stressed feelings about: exam prep {i}", "observation", "inferred", 0.5, {"kind": "mood"})
    s.db.execute("UPDATE memories SET created=?", (noon,))
    s.db.commit()
    ids = analyse(s.db, obs)
    texts = {o.text: o for o in obs.all()}
    assert "Often feels stressed around 21:00-24:00" in texts and "'exam' is a recurring source of stress" in texts
    assert all(o.status == "proposed" for o in obs.all()) and obs.prompt_block() == ""
    obs.set_status(ids[0], "confirmed")
    assert "21:00-24:00" in obs.prompt_block()
    rej = texts["'exam' is a recurring source of stress"].id
    obs.set_status(rej, "rejected")
    analyse(s.db, obs)
    assert obs.all("rejected")[0].id == rej  # re-analysis does not revive a rejected observation


def test_sensitive_traits_are_never_proposed(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    assert Observations(s.db, s.lock).propose("trait", "Probably has ADHD", ["x"], 0.9) is None


def test_preference_timeline(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    p = PreferenceModel(s.db, s.lock)
    p.learn("stop using emojis in your replies")
    p.set_active("no_emojis", False)
    changes = [e["change"] for e in p.timeline()]
    assert changes[0] == "disabled" and changes[-1].startswith("active")
