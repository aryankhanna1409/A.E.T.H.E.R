"""Phase 8: voice pipeline state machine and vision gating, with no audio
hardware or model weights (stt, tts and wake word are test doubles)."""
from pathlib import Path

import numpy as np

from aether.core.config import VoiceConfig, VisionConfig
from aether.llm.base import LLMUnavailable
from aether.perception.vision import Vision
from aether.speech.tts import speakable
from aether.speech.vad import Segmenter
from aether.speech import voice_loop
from aether.speech.voice_loop import VoiceAssistant

LOUD = np.full(480, 0.2, dtype="float32")
QUIET = np.zeros(480, dtype="float32")


def test_speakable_strips_markup():
    s = speakable("**Done** [web 1]\n- see https://x.org\n```py\nprint(1)\n```")
    assert "*" not in s and "[web" not in s and "https" not in s
    assert "link on screen" in s and "code shown on screen" in s


def test_segmenter_finds_utterance_and_ignores_clicks():
    seg = Segmenter(0.05, silence_ms=300, frame_ms=30)
    outs = [seg.feed(f) for f in [QUIET] * 3 + [LOUD] * 12 + [QUIET] * 10]
    audio = [o for o in outs if o is not None]
    assert len(audio) == 1 and len(audio[0]) >= 12 * 480
    click = [seg.feed(f) for f in [LOUD] + [QUIET] * 12]
    assert all(o is None for o in click)  # too short to count as speech


class FakeWake:
    def __init__(self): self.fire = False
    def heard(self, frame): return self.fire


class FakeSTT:
    def transcribe(self, audio): return "what time is it"


class FakeTTS:
    def __init__(self): self.said, self.stopped = [], 0
    def say(self, text): self.said.append(text); return True
    def stop(self): self.stopped += 1


class Reply:
    text = "It is noon."


class FakeAether:
    def __init__(self): self.heard = []
    def handle(self, text, on_event=None, spoken=False): self.heard.append(text); return Reply()


def _assistant(monkeypatch):
    class Sync:  # run the speaking thread inline so the test is deterministic
        def __init__(self, target, args, daemon): self.t, self.a = target, args
        def start(self): self.t(*self.a)
    monkeypatch.setattr(voice_loop.threading, "Thread", Sync)
    cfg = VoiceConfig(vad_energy=0.05, silence_ms=300)
    wake, tts, ae = FakeWake(), FakeTTS(), FakeAether()
    return VoiceAssistant(ae, cfg, stt=FakeSTT(), tts=tts, wake=wake), wake, tts, ae


def test_wake_listen_reply_cycle(monkeypatch):
    va, wake, tts, ae = _assistant(monkeypatch)
    va.step(LOUD)
    assert va.state == "idle"  # no wake word, nothing happens
    wake.fire = True
    va.step(QUIET)
    assert va.state == "listening"
    for f in [LOUD] * 12 + [QUIET] * 11:
        va.step(f)
    assert ae.heard == ["what time is it"] and tts.said == ["It is noon."]
    assert va.state == "listening" and va.follow_up_until > 0  # follow-up window open
    va.follow_up_until = 1.0  # window expired
    va.step(QUIET)
    assert va.state == "idle"


def test_barge_in_interrupts_speech(monkeypatch):
    va, _, tts, _ = _assistant(monkeypatch)
    va.state = "speaking"
    for _ in range(10):
        va.step(QUIET)  # the first 300 ms learn how loud AETHER's own voice is in the mic (quiet here)
    for _ in range(5):
        va.step(LOUD)
    va.step(QUIET)  # a pause resets the count
    assert tts.stopped == 0
    for _ in range(12):
        va.step(LOUD)
    assert tts.stopped == 1 and va.state == "listening" and va.seg.started


def test_its_own_voice_in_the_speakers_does_not_interrupt_it(monkeypatch):
    va, _, tts, _ = _assistant(monkeypatch)
    va.state = "speaking"
    for _ in range(200):          # AETHER's voice echoing back from laptop speakers, as loud as speech
        va.step(LOUD)
    assert tts.stopped == 0 and va.state == "speaking"


def test_vision_unconfigured_is_clear():
    v = Vision(VisionConfig())
    assert not v.configured
    try:
        v.describe("x.png")
        assert False
    except LLMUnavailable as e:
        assert "model_path" in str(e)


def test_look_at_image_is_policy_gated(make_aether, cfg, tmp_path):
    a = make_aether()
    secret = tmp_path / "elsewhere" / "shot.png"
    secret.parent.mkdir()
    secret.write_bytes(b"png")
    assert a.tools.call("look_at_image", {"path": str(secret)}).decision == "deny"
    ok = Path(cfg.security.allowed_read_roots[0]) / "shot.png"
    ok.write_bytes(b"png")
    r = a.tools.call("look_at_image", {"path": str(ok)})
    assert not r.ok and "Vision model not set" in r.output
