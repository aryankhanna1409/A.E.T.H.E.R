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
