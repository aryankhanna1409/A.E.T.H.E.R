"""Local desktop UI server. Binds to 127.0.0.1 only and opens in your browser.
Run: python -m aether.interface.server   (or the `aether-server` command)

Permission prompts are shown in the UI: a tool that needs approval blocks the
request until you press Allow or Deny (or 120 s pass, which counts as Deny).
"""
from __future__ import annotations

import argparse
import threading
import uuid
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..app import build
from ..core.logging import setup_logging

WEB = Path(__file__).parent / "web"


class Pending:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.lock = threading.Lock()

    def ask(self, prompt: str) -> bool:
        pid = uuid.uuid4().hex[:8]
        ev = threading.Event()
        with self.lock:
            self.items[pid] = {"id": pid, "prompt": prompt, "event": ev, "answer": False}
        ev.wait(timeout=120)
        with self.lock:
            return self.items.pop(pid)["answer"]

    def list(self) -> list[dict]:
        with self.lock:
            return [{"id": k, "prompt": v["prompt"]} for k, v in self.items.items()]

    def answer(self, pid: str, allow: bool) -> None:
        with self.lock:
            item = self.items.get(pid)
            if not item:
                raise KeyError(pid)
            item["answer"] = allow
            item["event"].set()


class ChatIn(BaseModel):
    text: str


class PrefEdit(BaseModel):
    key: str
    action: str  # on | off | delete | edit | add
    value: str = ""


def create_app(config_path: str | None = None) -> FastAPI:
    pending = Pending()
    aether, problem = build(config_path, confirm=pending.ask)
    app = FastAPI(title="AETHER")
    last = {"state": None}

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    @app.get("/api/status")
    def status():
        return {"model_loaded": aether.llm is not None, "problem": problem,
                "embeddings": getattr(aether.store.embedder, "name", "lexical only (BM25)"),
                "memories": aether.store.count(), "facts": len(aether.kg.all()),
                "emergency_stop": aether.policy.emergency_stop,
                "read_roots": aether.cfg.security.allowed_read_roots,
                "write_roots": aether.cfg.security.allowed_write_roots,
                "network": aether.cfg.security.network}

    @app.post("/api/chat")
    def chat(body: ChatIn):  # sync: runs in a worker thread so approvals can block
        try:
            r = aether.handle(body.text)
        except Exception as e:
            raise HTTPException(503, str(e))
        last["state"] = r.state.model_dump()
        return {"text": r.text, "notices": r.notices, "state": last["state"]}

    @app.get("/api/pending")
    def get_pending():
        return pending.list()

    @app.post("/api/pending/{pid}")
    def answer(pid: str, allow: bool):
        try:
            pending.answer(pid, allow)
        except KeyError:
            raise HTTPException(404, "no such request")
        return {"ok": True}

    @app.get("/api/state")
    def state():
        return last["state"] or {}

    @app.get("/api/preferences")
    def prefs():
        return [p.__dict__ for p in aether.prefs.all()]

    @app.post("/api/preferences")
    def edit_pref(body: PrefEdit):
        if body.action == "add":
            return {"key": aether.prefs.add_custom(body.value)}
        if body.action in {"on", "off"}:
            aether.prefs.set_active(body.key, body.action == "on")
        elif body.action == "delete":
            aether.prefs.delete(body.key)
        elif body.action == "edit":
            aether.prefs.edit(body.key, body.value)
        return {"ok": True}

    @app.get("/api/memories")
    def memories(layer: str | None = None):
        return [m.__dict__ for m in aether.store.list(layer, 200)]

    @app.delete("/api/memories/{mid}")
    def forget(mid: int):
        aether.store.forget(mid)
        return {"ok": True}

    @app.get("/api/facts")
    def facts():
        return [{"id": t.id, "s": t.s, "p": t.p, "o": t.o, "source": t.source, "confidence": t.confidence}
                for t in aether.kg.all()]

    @app.delete("/api/facts/{fid}")
    def retract(fid: int):
        aether.kg.retract(fid)
        return {"ok": True}

    @app.get("/api/audit")
    def audit():
        return list(reversed(aether.audit.tail(100)))

    @app.post("/api/stop")
    def stop(engaged: bool = True):
        aether.policy.emergency_stop = engaged
        aether.audit.record("emergency_stop", "all", True, "ui " + ("on" if engaged else "off"))
        return {"emergency_stop": engaged}

    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn
    ap = argparse.ArgumentParser(prog="aether-server")
    ap.add_argument("--config", default=None)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    setup_logging("INFO")
    app = create_app(args.config)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
