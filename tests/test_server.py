import threading
import time

from fastapi.testclient import TestClient

from aether.interface.server import create_app


def test_server_runs_without_model_and_manages_state(tmp_path, monkeypatch):
    cfgfile = tmp_path / "aether.toml"
    cfgfile.write_text(f'home = "{(tmp_path / "h").as_posix()}"\n[embeddings]\nbackend = "lexical"\n'
                       '[llm]\nmodel_path = "missing.gguf"\n')
    c = TestClient(create_app(str(cfgfile)))
    s = c.get("/api/status").json()
    assert s["model_loaded"] is False and "GGUF" in s["problem"]
    assert c.post("/api/chat", json={"text": "explain entropy"}).status_code == 503  # honest failure, no fake reply
    c.post("/api/preferences", json={"key": "", "action": "add", "value": "Answer in Hinglish"})
    assert c.get("/api/preferences").json()[0]["instruction"] == "Answer in Hinglish"
    assert c.post("/api/stop?engaged=true").json()["emergency_stop"] is True
    assert c.get("/").status_code == 200


def test_pending_permission_roundtrip():
    from aether.interface.server import Pending
    p = Pending()
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("v", p.ask("fs_create plan.md")))
    t.start()
    for _ in range(50):
        if p.list():
            break
        time.sleep(0.01)
    p.answer(p.list()[0]["id"], True)
    t.join(2)
    assert result["v"] is True
