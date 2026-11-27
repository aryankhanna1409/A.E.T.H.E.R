import io
import json
import time
import wave
from datetime import datetime

import numpy as np
import pytest

from aether.core.bus import EventBus
from aether.core.config import VoiceConfig
from aether.memory.nets import MemoryReranker, features, used
from aether.memory.store import Memory
from aether.speech import voice_loop
from aether.speech.turn import finished, silence_for
from aether.speech.tts import KokoroVoice, blend, split_sentences
from aether.speech.voice_loop import VoiceAssistant
from conftest import FakeLLM


# ---------------------------------------------------------------- voice: end of turn
def test_end_of_turn_reads_whether_you_have_finished():
    assert silence_for("What's the time?") == 350
    assert silence_for("open spotify") <= 500
    assert silence_for("set a timer for, um") == 1600
    assert silence_for("remind me to call my mom and") == 1600
    assert silence_for("I was thinking that maybe we could look at the") == 1600
    assert silence_for("yes") == 350
    assert 350 <= silence_for("the robot arm kinematics project is going well") <= 900
    assert finished("what's on my calendar tomorrow?") > finished("what's on my calendar tomorrow and")


# ---------------------------------------------------------------- voice: acknowledgement and partials
class SlowAether:
    def __init__(self, delay): self.delay = delay

    def handle(self, text, on_event=None, spoken=False):
        time.sleep(self.delay)
        return type("R", (), {"text": "The answer is 42."})()


class STT:
    def transcribe(self, audio): return "what is the meaning of life"
    def raw(self, audio, beam_size=None): return "what is the meaning of"


class TTS:
    def __init__(self): self.said = []
    def say(self, t): self.said.append(t); return True
    def stop(self): pass


class Wake:
    def heard(self, f): return False


def _va(aether, **cfg):
    va = VoiceAssistant(aether, VoiceConfig(vad_energy=0.05, silence_ms=300, **cfg), stt=STT(), tts=TTS(), wake=Wake())
    return va


def test_says_one_moment_when_the_answer_is_slow_and_nothing_when_fast():
    va = _va(SlowAether(0.5), ack_after_s=0.15)
    va.handle_text("deep question")
    time.sleep(0.3)
    assert va.tts.said[0] in voice_loop.ACKS and va.tts.said[-1] == "The answer is 42."
    va = _va(SlowAether(0.0), ack_after_s=0.3)
    va.handle_text("quick")
    time.sleep(0.4)
    assert va.tts.said == ["The answer is 42."]


def test_partial_transcript_sets_how_long_a_pause_ends_the_turn():
    va = _va(SlowAether(0))
    got = []
    va.on_partial = got.append
    va.set_partial("remind me to call my mom and")
    assert va.seg.silence_ms == 1600 and got == ["remind me to call my mom and"]
    va.set_partial("what's the time?")
    assert va.seg.silence_ms == 300                 # finished: as quick as configured (never slower)
    va._new_turn()
    assert va.seg.silence_ms == 300 and va.partial == ""


# ---------------------------------------------------------------- voice: Kokoro (with a stand-in engine)
class FakeKokoro:
    voices = {"bm_george": np.ones((4, 8), "float32"), "bm_lewis": np.zeros((4, 8), "float32")}

    def __init__(self): self.calls = []

    def create(self, text, voice, speed=1.0, lang="en-us"):
        self.calls.append((text, speed, lang, float(np.asarray(voice).mean())))
        return np.zeros(2400, "float32"), 24000


def test_kokoro_blends_voices_caches_phrases_and_speeds_up_alerts():
    eng = FakeKokoro()
    v = KokoroVoice(VoiceConfig(kokoro_voice="bm_george:0.75,bm_lewis:0.25"), engine=eng)
    assert abs(float(v.style.mean()) - 0.75) < 1e-6 and v.lang == "en-gb"
    v.warm(["One moment."])
    n = len(eng.calls)
    v._pcm_cached("One moment.")
    assert len(eng.calls) == n                                  # cached: no synthesis
    v.pcm("Warning: battery at 5 percent.")
    assert eng.calls[-1][1] > 1.0                               # alerts a touch quicker
    v.pcm("The lecture is in room four.")
    assert eng.calls[-1][1] == 1.0
    wav = v.wav_bytes("Hello there. How are you?")
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getframerate() == 24000 and w.getnframes() == 4800
    with pytest.raises(ValueError):
        blend(eng.voices, "bm_nobody")
    assert split_sentences("One. Two? **Three**!") == ["One.", "Two?", "Three!"]


# ---------------------------------------------------------------- event bus
def test_event_bus_streams_in_order_and_waits():
    bus = EventBus(size=5)
    for i in range(7):
        bus.publish("partial", {"text": str(i)})
    assert [e.data["text"] for e in bus.since(0)] == ["2", "3", "4", "5", "6"]   # ring buffer keeps the newest
    assert bus.wait(bus.seq, timeout=0.05) == []
    import threading
    threading.Timer(0.05, lambda: bus.publish("presence", {"state": "speaking"})).start()
    got = bus.wait(bus.seq, timeout=2, kinds={"presence"})
    assert got and got[0].data["state"] == "speaking"


def test_turns_publish_to_the_bus_and_the_api_accepts_voice_events(make_aether):
    from fastapi.testclient import TestClient
    from aether.interface import server
    a = make_aether()
    a.handle("set a timer for 5 minutes")
    kinds = [e.kind for e in a.bus.since(0)]
    assert "heard" in kinds and "reply" in kinds
    server.build = lambda *x, **k: (a, None)
    c = TestClient(server.create_app())
    assert c.post("/api/events", json={"kind": "partial", "data": {"text": "what's the"}}).status_code == 200
    assert a.bus.last("partial").data["text"] == "what's the"
    assert c.post("/api/events", json={"kind": "shutdown", "data": {}}).status_code == 400
    w = c.get("/api/world").json()
    assert {"energy", "focus", "load", "needs", "words"} <= set(w)


# ---------------------------------------------------------------- phone relay
def _wav(seconds=0.5, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.sin(np.arange(int(rate * seconds)) / 5) * 8000).astype(np.int16).tobytes())
    return buf.getvalue()


def test_phone_relay_needs_the_token_and_never_skips_the_allow_box(make_aether):
    from fastapi.testclient import TestClient
    from aether.interface.relay import create_relay, token_for
    a = make_aether()
    seen = []
    real = a.handle
    a.handle = lambda text, on_event=None, spoken=False, origin="local": seen.append(origin) or real(text, spoken=spoken, origin=origin)

    class PhoneSTT:
        def transcribe(self, audio): return "what's the time"
    c = TestClient(create_relay(a, stt=PhoneSTT(), tts=False))
    assert c.get("/").status_code == 401 and c.post("/api/text", json={"text": "hi"}).status_code == 401
    t = token_for(a.cfg.home)
    assert len(t) >= 24 and token_for(a.cfg.home) == t           # stable once made
    page = c.get("/?t=" + t)
    assert page.status_code == 200 and "Hold to talk" in page.text
    r = c.post("/api/text", json={"text": "what's the time"}, headers={"x-aether-token": t}).json()
    assert r["text"] and seen == ["phone"]
    r = c.post("/api/talk", content=_wav(), headers={"x-aether-token": t}).json()
    assert r["heard"] == "what's the time" and r["voice"] is False
    assert c.post("/api/talk", content=b"nope", headers={"x-aether-token": t}).status_code == 400


def test_a_phone_turn_does_not_count_as_said_at_the_pc(make_aether):
    a = make_aether()
    asked = []
    orig = a.tools.asking

    def spy(text):
        asked.append(text)
        return orig(text)
    a.tools.asking = spy
    a.handle("open notepad", origin="phone")
    assert asked == []
    a.handle("open notepad")
    assert asked == ["open notepad"]


def test_relay_certificate_is_made_once(tmp_path):
    from aether.interface.relay import certificate
    c1, k1 = certificate(tmp_path)
    assert open(c1).read().startswith("-----BEGIN CERTIFICATE") and certificate(tmp_path) == (c1, k1)


# ---------------------------------------------------------------- brains
def test_spoken_small_talk_goes_to_the_fast_talker(make_aether):
    main, fast = FakeLLM("Main model answer."), FakeLLM("Quick answer.")
    a = make_aether(main)
    a.fast_llm = fast
    r = a.handle("tell me a fun fact about octopuses", spoken=True)
    assert r.text == "Quick answer." and "fast talker" in r.state.tool_log
    r = a.handle("tell me a fun fact about octopuses")             # typed: the main model
    assert r.text == "Main model answer."


# ---------------------------------------------------------------- memory nets
def _mem(i, layer, score, conf=0.9, age_days=1):
    return Memory(i, layer, f"memory number {i} about topic{i}", "chat", conf, time.time() - age_days * 86400, {}, score)


def test_memory_reranker_learns_which_memories_help(make_aether):
    import random
    a = make_aether()
    r = MemoryReranker(a.db, a.lock, min_examples=60, min_auc=0.6)
    rng = random.Random(0)
    for _ in range(60):   # procedural memories keep getting used; episodic ones are noise, whatever their score
        mems = [_mem(1, "episodic", rng.uniform(0.6, 0.9)), _mem(2, "procedural", rng.uniform(0.3, 0.6))]
        r.observe(mems, "Use [memory 2] here.")
    out = r.train(epochs=200)
    assert out["trained"] and out["auc"] > 0.9 and r.active
    ranked = r.rerank([_mem(1, "episodic", 0.9), _mem(2, "procedural", 0.4)])
    assert ranked[0].layer == "procedural"
    assert MemoryReranker(a.db, a.lock).active        # the model is saved
    assert used("Your sister Riya lives in Delhi.", 1, "sister Riya lives Delhi") and not used("Nope.", 1, "sister riya delhi")
    assert len(features(_mem(1, "semantic", 0.5), 0, time.time())) == 12


def test_working_memory_and_world_model_reach_the_prompt(make_aether):
    llm = FakeLLM("Shall I set it up?")
    a = make_aether(llm)
    a.handle("help me plan a study session")
    assert "Shall I set it up?" in a.working.open_question
    llm.reply = "Done."
    a.handle("yes")
    prompt = "\n".join(m.content for msgs, schema in llm.calls if schema is None for m in msgs)
    assert "<working_memory>" in prompt and "You last asked them" in prompt and "Your state (estimated)" in prompt


def test_world_model_estimates_energy_and_needs(make_aether):
    a = make_aether()
    now = datetime(2026, 11, 27, 18, 30)
    a.grind.set_field("sleepH", 5, "2026-11-27")
    st = a.world.estimate(now)
    assert st.energy < 0.5 and "5 h sleep" in st.why["sleep"]
    assert any("protein" in n for n in st.needs)                  # evening with protein still to eat
    a.grind.set_field("sleepH", 8, "2026-11-27")
    assert a.world.estimate(datetime(2026, 11, 27, 10, 0)).energy > 0.8
    a.clock = lambda: now
    out = a.handle("how am I doing?").text
    assert "Energy" in out and "Focus" in out and "protein" in out
