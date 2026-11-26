"""Always listening: wake by "Nova" or "AETHER", the voice process using the server's model,
and the tray's microphone toggle."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np

from aether.core.config import VoiceConfig
from aether.speech.profile import is_wake_phrase


def test_every_name_wakes_it_but_ordinary_speech_does_not():
    for said in ["AETHER", "Aether, what's the time", "Hey Ether", "hey eather", "Nova", "ok nova", "Hi Nava!"]:
        assert is_wake_phrase(said), said
    for said in ["either way is fine", "I read about a supernova", "we watched a movie about Jarvis and Tony yesterday night",
                 "I told Nova about it", "so the ether thing", "Jarvis"]:
        assert not is_wake_phrase(said), said


def fake_va(heard_small, heard_big):
    from aether.speech.voice_loop import VoiceAssistant

    class Wake:
        def heard(self, f):
            return False
    va = VoiceAssistant(object(), VoiceConfig(), stt=type("S", (), {"transcribe": lambda s, a: heard_big})(),
                        tts=object(), wake=Wake(), name_stt=type("N", (), {"transcribe": lambda s, a: heard_small})(),
                        profile=__import__("aether.speech.profile", fromlist=["SpeechProfile"]).SpeechProfile())
    said, states = [], []
    va.handle_text = lambda text: said.append(text) or ""
    va.on_state = states.append
    return va, said, states


def test_bare_aether_and_nova_wake_and_nova_turns_amber():
    va, said, states = fake_va("Aether", "AETHER")
    assert va.name_called(np.zeros(8000, dtype="float32")) and said == [] and states == ["listening"]
    va, said, _ = fake_va("Aether set a timer for five minutes", "AETHER, set a timer for five minutes")
    assert va.name_called(np.zeros(8000, dtype="float32")) and said == ["set a timer for five minutes"]


class SSE(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SSE.got = body
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for kind, data in [("stage", {"name": "thinking"}), ("token", {"text": "It's "}), ("token", {"text": "five."}),
                           ("done", {"text": "It's five."})]:
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"hotwords": ["Aryan", "Aundh"]} if "speech" in self.path else {}).encode())

    def log_message(self, *a):
        pass


def test_voice_uses_the_running_servers_model():
    from aether.speech.voice_loop import RemoteAether
    srv = HTTPServer(("127.0.0.1", 0), SSE)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        brain = RemoteAether(f"http://127.0.0.1:{srv.server_port}")
        events = []
        r = brain.handle("what's the time", on_event=lambda k, d: events.append(k))
        assert r.text == "It's five." and SSE.got == {"text": "what's the time", "voice": True}
        assert events == ["stage", "token", "token"]
        assert brain.alive() and brain.hotwords() == ["Aryan", "Aundh"]
    finally:
        srv.shutdown()
    assert "can't reach" in RemoteAether("http://127.0.0.1:9", timeout=1).handle("hi").text


def test_tray_starts_the_listener_and_can_mute_it(tmp_path, monkeypatch):
    from aether.interface import nova
    from aether.interface.tray import Tray
    monkeypatch.setattr(nova, "settings_path", lambda: tmp_path / "nova.json")
    t = Tray(port=8799, config="C:/AETHER/aether.toml")
    assert t.voice_command()[1:] == ["-m", "aether.speech.voice_loop", "--server", "--port", "8799",
                                     "--config", "C:/AETHER/aether.toml"]
    assert Tray.listening_wanted() and not t.listening()
    nova.save_settings({"listening": False})
    assert not Tray.listening_wanted()


def test_name_in_passing_or_background_talk_does_not_wake():
    va, said, states = fake_va("and then I told Nova to stop", "and then I told Nova to stop")
    assert not va.name_called(np.zeros(8000, dtype="float32")) and states == []
    va, said, states = fake_va("", "")      # Whisper wasn't sure it heard speech at all
    assert not va.name_called(np.zeros(8000, dtype="float32"))


def test_follow_up_only_after_a_question_and_wake_window_expires():
    import time as _t
    va, said, states = fake_va("Nova", "Nova")
    va._after_reply("It's 6 PM.")
    assert va.state == "idle" and va.follow_up_until == 0
    va.state = "speaking"
    va._after_reply("Do you want me to set a reminder?")
    assert va.state == "listening" and va.follow_up_until > _t.time()
    for _ in range(5):
        va._after_reply("Anything else?")
    assert va.state == "idle"          # never an endless conversation with the room
    va.name_called(np.zeros(8000, dtype="float32"))
    assert va.state == "listening" and 0 < va.follow_up_until - _t.time() <= 8.5   # listening after the name times out
