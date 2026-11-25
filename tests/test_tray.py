"""The tray launcher's testable parts: hotkeys, the sign-in command, the icon, and notification relay."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from aether.interface.tray import Tray, pynput_hotkey, startup_command


def test_hotkeys_translate_and_reject_unsafe_ones():
    assert pynput_hotkey("ctrl+alt+j") == "<ctrl>+<alt>+j"
    assert pynput_hotkey("Win + Shift + Space") == "<cmd>+<shift>+<space>"
    for bad in ["j", "shift+j", "ctrl+alt+jj", ""]:
        with pytest.raises(ValueError):
            pynput_hotkey(bad)


def test_startup_command_uses_windowless_python(tmp_path):
    py = tmp_path / "python.exe"
    py.write_text("")
    assert startup_command(str(py)).endswith('-m aether.interface.tray --quiet-start')
    (tmp_path / "pythonw.exe").write_text("")
    assert startup_command(str(py)).startswith(f'"{tmp_path / "pythonw.exe"}"')


def test_icon_draws_without_assets():
    pytest.importorskip("PIL")
    from aether.interface.tray import make_icon
    assert make_icon(32).size == (32, 32) and make_icon(active=False).getpixel((32, 32))[3] > 0


class FakeServer(BaseHTTPRequestHandler):
    hits: list = []

    def do_GET(self):
        FakeServer.hits.append(self.path)
        body = {"items": [{"title": "In 15 min: Lecture", "body": "10:00-11:00 (Room 4)"}]} if "notifications" in self.path else {}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        FakeServer.hits.append("POST " + self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def test_tray_relays_notifications_and_respects_pause():
    srv = HTTPServer(("127.0.0.1", 0), FakeServer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        t = Tray(port=srv.server_address[1])
        shown = []
        t.icon = type("I", (), {"notify": lambda self, body, title: shown.append((title, body))})()
        assert t.alive() and t.ensure_server()  # already running: nothing is started
        assert t.server is None
        assert t.poll_once() == 1 and shown == [("In 15 min: Lecture", "10:00-11:00 (Room 4)")]
        t.paused = True
        assert t.poll_once() == 0 and len(shown) == 1
        t.emergency_stop()
        assert "POST /api/stop" in FakeServer.hits and shown[-1][0] == "Emergency stop"
    finally:
        srv.shutdown()


def test_startup_remembers_the_config_and_server_uses_console_python(tmp_path):
    from aether.interface.tray import console_python
    (tmp_path / "pythonw.exe").write_text("")
    (tmp_path / "python.exe").write_text("")
    cmd = startup_command(str(tmp_path / "python.exe"), config=str(tmp_path / "aether.toml"))
    assert cmd.endswith(f'--quiet-start --config "{tmp_path / "aether.toml"}"')
    assert console_python(str(tmp_path / "pythonw.exe")) == str(tmp_path / "python.exe")
    t = Tray(port=8799, config="C:/AETHER/aether.toml")
    assert t.server_command()[1:] == ["-m", "aether.interface.server", "--no-browser", "--port", "8799",
                                      "--config", "C:/AETHER/aether.toml"]


def test_only_one_tray_runs(monkeypatch):
    from aether.interface import tray
    first = tray.single_instance(0)
    port = first.getsockname()[1]
    assert tray.single_instance(port) is None
    first.close()


def test_a_crashing_server_is_logged_and_reported_quickly(tmp_path):
    import sys
    import time
    t = Tray(port=9)   # nothing answers there
    t.server_log = tmp_path / "logs" / "server.log"
    t.server_command = lambda: [sys.executable, "-c", "import sys; print('model file not found'); sys.exit(3)"]
    start = time.time()
    assert t.ensure_server(wait_s=60) is False and time.time() - start < 20
    from aether.interface.tray import log_tail
    assert "model file not found" in log_tail(t.server_log)


def test_pythonw_streams_go_to_a_log_file(tmp_path, monkeypatch):
    import sys
    from aether.core import logging as alog
    monkeypatch.setenv("AETHER_HOME", str(tmp_path))
    assert alog.ensure_streams("x") is None          # a normal console: nothing to do
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    path = alog.ensure_streams("server")
    print("hello from pythonw")
    sys.stdout.isatty()                               # uvicorn calls this; it crashed on None
    assert path == tmp_path / "logs" / "server.log" and "hello from pythonw" in path.read_text()


def test_voice_approvals_open_the_app_when_no_window_shows_them():
    t = Tray()
    opened, notes = [], []
    t.open, t.notify = lambda hud=False: opened.append(hud), lambda title, body: notes.append(title)
    state = {"items": [{"id": "a1", "prompt": "Shut down the PC?\nIt asks first."}], "window_visible": False}
    t.api = lambda path, *a, **k: state if path == "/api/pending/watch" else {"items": []}
    assert t.surface_approvals() and opened == [False] and notes == ["AETHER needs your OK"]
    assert not t.surface_approvals() and len(opened) == 1          # the same request isn't surfaced twice
    state["items"].append({"id": "b2", "prompt": "Open Spotify?"})
    state["window_visible"] = True
    assert not t.surface_approvals() and len(opened) == 1 and len(notes) == 2   # the app already shows it
    t.paused = True
    state["items"].append({"id": "c3", "prompt": "Restart?"})
    state["window_visible"] = False
    t.poll_once()
    assert len(opened) == 2


def test_server_knows_when_a_window_can_show_approvals(tmp_path):
    from fastapi.testclient import TestClient
    from aether.interface.server import create_app
    cfgfile = tmp_path / "aether.toml"
    cfgfile.write_text(f'home = "{(tmp_path / "h").as_posix()}"\n[embeddings]\nbackend = "lexical"\n'
                       '[llm]\nmodel_path = "missing.gguf"\n[memory]\nwatch_interval_s = 0\n')
    c = TestClient(create_app(str(cfgfile)))
    assert c.get("/api/pending/watch").json() == {"items": [], "window_visible": False}
    assert c.get("/api/pending").json() == []                       # a hidden tab doesn't count
    assert c.get("/api/pending/watch").json()["window_visible"] is False
    c.get("/api/pending", params={"visible": 1})
    assert c.get("/api/pending/watch").json()["window_visible"] is True
