"""Indian-accent support: prompt and hotwords, the personal correction dictionary, learning from fixes,
say-the-name wake, and the calibration helpers."""
import importlib.util
from pathlib import Path

import numpy as np

from aether.core.config import VoiceConfig
from aether.speech.profile import (SpeechProfile, is_wake_phrase, looks_like_correction, terms_from_memories)

ROOT = Path(__file__).resolve().parent.parent


def test_learns_misheard_names_and_rewrites_them(tmp_path):
    p = SpeechProfile(tmp_path / "sp.json", "Aryan")
    learnt = p.learn("book a show at Cinepolis in on tonight", "book a show at Cinepolis in Aundh tonight")
    assert learnt == [("on", "Aundh")]
    assert p.apply("the cafe in on is quiet") == "the cafe in Aundh is quiet"
    assert "Aundh" in p.hotwords
    again = SpeechProfile(tmp_path / "sp.json", "Aryan")     # saved and reloaded
    assert again.replacements == {"on": "Aundh"}


def test_multi_word_mishearing(tmp_path):
    p = SpeechProfile(tmp_path / "sp.json")
    p.learn("call arvind she wait at six", "call Arvind Shewate at six")
    assert p.apply("did she wait reply") == "did Shewate reply"


def test_grammar_noise_is_not_learnt(tmp_path):
    p = SpeechProfile(tmp_path / "sp.json")
    assert p.learn("open the file", "open a file") == []
    assert p.replacements == {}


def test_chat_corrections_need_to_repeat_before_they_stick(tmp_path):
    p = SpeechProfile(tmp_path / "sp.json")
    assert p.learn("remind me about in said oh", "remind me about Incedo", confirmed=False) == []
    assert p.replacements == {}
    assert p.learn("message from in said oh today", "message from Incedo today", confirmed=False) == \
        [("in said oh", "Incedo")]


def test_prompt_and_hotwords_carry_names(tmp_path):
    p = SpeechProfile(tmp_path / "sp.json", "Aryan", ["Aundh", "Incedo"])
    assert "Aryan speaking in Indian English to Nova" in p.prompt()
    assert "Aundh" in p.prompt() and "Pune" in p.prompt()
    assert p.hotword_string().split()[:3] == ["Aryan", "Aundh", "Incedo"]


def test_looks_like_correction():
    assert looks_like_correction("set a timer for twelve minutes for the pastor", "set a timer for twelve minutes for the pasta")
    assert not looks_like_correction("what's the weather", "what's the weather")
    assert not looks_like_correction("what's the weather", "tell me a joke about robots")


def test_wake_by_name():
    for said in ["Hey Nova", "hey, Nova.", "Nova", "Hi Nova!", "okay nova"]:
        assert is_wake_phrase(said), said
    assert not is_wake_phrase("I read about a supernova yesterday")
    assert not is_wake_phrase("we went to the casa nova cafe in koregaon park after the lecture today")
    p = SpeechProfile()
    p.replacements["hey nawa"] = "hey Nova"
    assert is_wake_phrase("Hey nawa", p)


def test_terms_from_memories(make_aether):
    a = make_aether()
    a.store.add("Aryan lives in Pune near Aundh.", "semantic", "user_stated")
    a.store.add("Aryan will intern at Incedo in Pune.", "semantic", "user_stated")
    terms = terms_from_memories(a.store)
    assert terms[0] == "Pune" and {"Aundh", "Incedo"} <= set(terms) and "Aryan" not in terms[:1]


class FakeModel:
    def __init__(self, text):
        self.text, self.calls = text, []

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        seg = type("S", (), {"text": " " + self.text})
        return [seg], None


def make_stt(text, profile, cfg=None):
    from aether.speech.stt import SpeechToText
    s = SpeechToText.__new__(SpeechToText)
    s.cfg, s.profile, s.model, s._hotwords_ok = cfg or VoiceConfig(), profile, FakeModel(text), True
    return s


def test_stt_uses_prompt_hotwords_beam_and_corrections(tmp_path):
    p = SpeechProfile(tmp_path / "sp.json", "Aryan")
    p.replacements["on"] = "Aundh"
    s = make_stt("cafes in on", p, VoiceConfig(stt_prompt="Robotics student."))
    assert s.transcribe(np.zeros(16000, dtype="float32")) == "cafes in Aundh"
    kw = s.model.calls[0]
    assert kw["language"] == "en" and kw["beam_size"] == 5 and kw["initial_prompt"].startswith("Robotics student. Aryan")
    assert "Aryan" in kw["hotwords"]


def test_stt_works_on_faster_whisper_without_hotwords(tmp_path):
    s = make_stt("hello", SpeechProfile())

    class Old(FakeModel):
        def transcribe(self, audio, **kw):
            if "hotwords" in kw:
                raise TypeError("unexpected keyword 'hotwords'")
            return super().transcribe(audio, **kw)
    s.model = Old("hello")
    assert s.transcribe("x.wav") == "hello" and s._hotwords_ok is False


def test_voice_loop_wakes_by_name_and_answers_same_breath_command(make_aether):
    from aether.speech.voice_loop import VoiceAssistant

    class Wake:
        def heard(self, f):
            return False
    va = VoiceAssistant(make_aether(), VoiceConfig(), stt=type("S", (), {"transcribe": lambda s, a: "Hey Nova, what's the time"})(),
                        tts=object(), wake=Wake(), name_stt=type("N", (), {"transcribe": lambda s, a: "Hey Nova what's the time"})())
    said = []
    va.handle_text = lambda text: said.append(text) or ""
    assert va.name_called(np.zeros(8000, dtype="float32")) and said == ["what's the time"] and va.state == "listening"
    va.state = "idle"
    va.name_stt = type("N", (), {"transcribe": lambda s, a: "I told my friend about Nova"})()
    assert not va.name_called(np.zeros(8000, dtype="float32")) and va.state == "idle"


def test_server_learns_from_a_typed_fix_and_explicit_corrections(tmp_path):
    from fastapi.testclient import TestClient
    from aether.interface.server import create_app
    cfgfile = tmp_path / "aether.toml"
    cfgfile.write_text(f'home = "{(tmp_path / "h").as_posix()}"\n[embeddings]\nbackend = "lexical"\n'
                       '[llm]\nmodel_path = "missing.gguf"\n[memory]\nwatch_interval_s = 0\n')
    app = create_app(str(cfgfile))
    c = TestClient(app)
    r = c.post("/api/speech/correct", json={"heard": "meet me in on", "meant": "meet me in Aundh"}).json()
    assert r["learnt"] == [["on", "Aundh"]]
    prof = c.get("/api/speech/profile").json()
    assert prof["replacements"] == {"on": "Aundh"} and "Aundh" in prof["hotwords"]
    assert c.delete("/api/speech/replacements", params={"wrong": "on"}).json() == {"removed": True}


def load_script():
    spec = importlib.util.spec_from_file_location("learn_my_voice", ROOT / "scripts" / "learn_my_voice.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_calibration_helpers(tmp_path):
    m = load_script()
    assert len(m.PHRASES) == 20 and m.wer("set a timer", "set a timer") == 0
    assert m.wer("set a time her", "set a timer") > 0
    toml = tmp_path / "aether.toml"
    toml.write_text('[llm]\nmodel_path = "x"\n\n[voice]\nstt_model = "small.en"\n\n[proactive]\nenabled = true\n')
    m.set_toml_value(toml, "voice", "wake_verifier_path", "C:/x/v.pkl")
    m.set_toml_value(toml, "voice", "wake_verifier_path", "C:/y/v.pkl")
    text = toml.read_text()
    assert text.count("wake_verifier_path") == 1 and 'wake_verifier_path = "C:/y/v.pkl"' in text
    assert text.index("wake_verifier_path") < text.index("[proactive]")
    wav = m.save_wav(tmp_path / "a.wav", np.zeros(1600, dtype="float32"))
    assert wav.stat().st_size > 3000
    assert "Not enough" in m.train_wake_verifier(VoiceConfig(), [], [], tmp_path / "v.pkl")
