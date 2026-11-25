"""The always-on listener must survive a fresh Windows install: openWakeWord without TFLite or model files,
and a speaking voice that get_model.py downloaded but aether.toml doesn't name."""
import numpy as np

from aether.core.config import VoiceConfig


def test_wake_word_uses_onnx_and_fetches_models(monkeypatch):
    import sys
    import types

    from aether.speech import voice_loop
    seen, fetched = {}, []

    class Model:
        def __init__(self, **kw):
            seen.update(kw)

    fake = types.ModuleType("openwakeword.model")
    fake.Model = Model
    monkeypatch.setitem(sys.modules, "openwakeword.model", fake)
    monkeypatch.setattr(voice_loop, "ensure_wake_models", fetched.append)
    voice_loop.WakeWord(VoiceConfig())
    assert seen["inference_framework"] == "onnx" and fetched == ["hey_jarvis"]


def test_a_broken_wake_word_falls_back_to_saying_the_name(monkeypatch):
    from aether.speech import voice_loop

    def boom(cfg):
        raise ValueError("Tried to import the tflite runtime")
    monkeypatch.setattr(voice_loop, "WakeWord", boom)
    name_stt = type("N", (), {"transcribe": lambda s, a: "Jarvis"})()
    va = voice_loop.VoiceAssistant(object(), VoiceConfig(), stt=object(), tts=object(), name_stt=name_stt,
                                   profile=voice_loop.SpeechProfile())
    assert isinstance(va.wake, voice_loop.NoWakeWord)
    assert not va.wake.heard(np.zeros(480, dtype=np.int16))


def test_voice_file_is_found_without_config(tmp_path, monkeypatch):
    from aether.speech import tts
    assert tts.find_voice(str(tmp_path / "missing.onnx")) in (None,) or tts.find_voice("").endswith(".onnx")
    real = tmp_path / "v.onnx"
    real.write_bytes(b"x")
    assert tts.find_voice(str(real)) == str(real)
