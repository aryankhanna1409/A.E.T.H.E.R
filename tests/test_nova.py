"""Nova, the desktop pet: colours, motion, settings, presence. The window itself needs a desktop."""
import math

from aether.interface import nova


def test_colours_follow_state():
    assert nova.target_colour("idle", 0) == nova.BLUE
    assert nova.target_colour("listening", 3) == nova.AMBER == nova.target_colour("thinking", 9)
    assert nova.target_colour("offline", 0) == nova.SLEEP
    seen = {nova.target_colour("speaking", t / 10) for t in range(60)}
    assert nova.BLUE in seen or min(sum(abs(a - b) for a, b in zip(c, nova.BLUE)) for c in seen) < 30
    assert min(sum(abs(a - b) for a, b in zip(c, nova.AMBER)) for c in seen) < 30


def test_mix_hex_and_pulse():
    assert nova.mix((0, 0, 0), (200, 100, 50), 0.5) == (100, 50, 25)
    assert nova.mix((0, 0, 0), (10, 10, 10), 7) == (10, 10, 10)
    assert nova.hexc((58, 160, 255)) == "#3aa0ff" and nova.hexc((300, -4, 0)) == "#ff0000"
    idle = [nova.pulse("idle", t / 10) for t in range(100)]
    talk = [nova.pulse("speaking", t / 10) for t in range(100)]
    assert max(idle) - min(idle) < max(talk) - min(talk) and all(0.9 < x < 1.1 for x in idle + talk)


def test_click_versus_drag_and_positions():
    assert nova.is_click(2, 3) and not nova.is_click(5, 0)
    x, y = nova.default_position(1920, 1080)
    assert (x, y) == (1920 - nova.SIZE - 16, 1080 - nova.SIZE - 56)
    assert nova.clamp_position(5000, -40, 1920, 1080) == (1920 - nova.SIZE, 0)


def test_settings_roundtrip(tmp_path):
    f = tmp_path / "nova.json"
    assert nova.load_settings(f) == {} and nova.enabled(f)
    nova.save_settings({"x": 10, "y": 20}, f)
    nova.save_settings({"enabled": False}, f)
    assert nova.load_settings(f) == {"x": 10, "y": 20, "enabled": False} and not nova.enabled(f)
    f.write_text("not json")
    assert nova.load_settings(f) == {} and nova.enabled(f)


def test_offline_when_no_server():
    assert nova.fetch_state("http://127.0.0.1:9", timeout=0.3) == "offline"


def test_single_instance_lock(monkeypatch):
    monkeypatch.setattr(nova, "LOCK_PORT", 0)
    first = nova.single_instance()
    port = first.getsockname()[1]
    monkeypatch.setattr(nova, "LOCK_PORT", port)
    assert nova.single_instance() is None
    first.close()


def test_presence_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from aether.interface.server import create_app
    cfgfile = tmp_path / "aether.toml"
    cfgfile.write_text(f'home = "{(tmp_path / "h").as_posix()}"\n[embeddings]\nbackend = "lexical"\n'
                       '[llm]\nmodel_path = "missing.gguf"\n[memory]\nwatch_interval_s = 0\n')
    c = TestClient(create_app(str(cfgfile)))
    assert c.get("/api/presence").json() == {"state": "idle"}
    assert c.post("/api/presence", json={"state": "speaking", "seconds": 30}).json() == {"state": "speaking"}
    assert c.get("/api/presence").json() == {"state": "speaking"}
    assert c.post("/api/presence", json={"state": "dancing"}).json() == {"state": "idle"}
    c.post("/api/presence", json={"state": "listening", "seconds": 0})
    import time
    time.sleep(1.05)
    assert c.get("/api/presence").json() == {"state": "idle"}


def test_voice_loop_reports_state_changes_once(make_aether):
    from aether.speech.voice_loop import VoiceAssistant
    from aether.core.config import VoiceConfig

    class Dummy:
        def heard(self, frame):
            return False
    va = VoiceAssistant(make_aether(), VoiceConfig(), stt=object(), tts=object(), wake=Dummy())
    seen = []
    va.on_state = seen.append
    va.state = "listening"
    va.state = "listening"
    va.state = "thinking"
    assert seen == ["listening", "thinking"] and va.state == "thinking"


def test_tray_ember_command_and_toggle(tmp_path, monkeypatch):
    from aether.interface.tray import Tray
    t = Tray(port=8765)
    assert t.nova_command()[-4:] == ["-m", "aether.interface.nova", "--port", "8765"]
    started = []

    class P:
        def __init__(self, cmd, creationflags=0):
            started.append(cmd)
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.alive = False
    monkeypatch.setattr("subprocess.Popen", P)
    monkeypatch.setattr(nova, "settings_path", lambda: tmp_path / "nova.json")
    t.toggle_nova()
    assert t.nova_running() and len(started) == 1 and nova.enabled(tmp_path / "nova.json")
    t.toggle_nova()
    assert not t.nova_running() and not nova.enabled(tmp_path / "nova.json")
    assert math.isfinite(nova.pulse("thinking", 1.0))
