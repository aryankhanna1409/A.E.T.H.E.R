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


class Text(BaseModel):
    text: str


class OptimizeIn(BaseModel):
    tasks: str
    hours_per_day: str = "4,4,4,4,4,6,6"
    algorithm: str = "auto"  # auto (GA vs SA) | pareto (NSGA-II) | tabu | exact (CSP)


class EvalIn(BaseModel):
    query: str
    expect: str = ""


class FeedbackIn(BaseModel):
    value: int = 1
    turn_id: int | None = None


class SettingsIn(BaseModel):
    section: str
    values: dict
    reason: str = "approved in UI"


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

    # ---------------------------------------------------------------- Phase 2-7 APIs

    @app.get("/api/reminders")
    def reminders():
        return [r.__dict__ for r in aether.reminders.open()]

    @app.post("/api/reminders/{rid}")
    def reminder_status(rid: int, status: str = "done"):
        aether.reminders.set_status(rid, status)
        return {"ok": True}

    @app.get("/api/observations")
    def observations():
        return [o.__dict__ for o in aether.observations.all()]

    @app.post("/api/observations/analyse")
    def analyse_obs():
        from ..personalization.observations import analyse
        return {"proposed": analyse(aether.db, aether.observations, decrypt=aether.store.cipher.decrypt)}

    @app.post("/api/observations/{oid}")
    def observation_status(oid: int, status: str):
        if status == "delete":
            aether.observations.delete(oid)
        else:
            aether.observations.set_status(oid, status)
        return {"ok": True}

    @app.get("/api/preferences/timeline")
    def pref_timeline():
        return aether.prefs.timeline()

    @app.get("/api/goals")
    def goals():
        return {"goals": aether.graph.goals(), "progress": aether.progress_log[-30:]}

    @app.get("/api/goals/{gid}")
    def goal(gid: int):
        return {**aether.graph.goal(gid), "tasks": [t.__dict__ for t in aether.graph.tasks(gid)]}

    @app.post("/api/goals")
    def submit_goal(body: Text):
        if not aether.goals:
            raise HTTPException(503, "No model loaded.")
        return {"message": aether.goals.submit(body.text)}

    @app.post("/api/goals/{gid}/cancel")
    def cancel_goal(gid: int):
        if aether.executive:
            aether.executive.cancel(gid)
        return {"ok": True}

    @app.get("/api/rules")
    def rules():
        return {"all": [r.text for r in aether.rules.rules], "yours": aether.rules.user_rules()}

    @app.post("/api/rules")
    def add_rule(body: Text):
        try:
            aether.rules.add_rule(body.text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        new, viol = aether.rules.run()
        return {"derived": len(new), "violations": [v.constraint for v in viol]}

    @app.delete("/api/rules")
    def remove_rule(body: Text):
        aether.rules.remove_rule(body.text)
        return {"ok": True}

    @app.post("/api/ask")
    def ask(body: Text):
        try:
            return {"bindings": aether.rules.ask(body.text)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/why")
    def why(s: str, p: str, o: str):
        aether.rules.run(persist=False)
        return {"proof": aether.rules.explain((s.lower(), p.lower(), o.lower()))}

    @app.get("/api/journal")
    def journal():
        return aether.journal.recent()

    @app.post("/api/undo")
    def undo():
        return {"message": aether.journal.undo_last()}

    @app.post("/api/index")
    def index(body: Text, facts: bool = False):
        return {"message": aether.index_folder(body.text, extract_facts=facts)}

    @app.post("/api/research")
    def research(body: Text):  # sync: worker thread
        r = aether.tools.call("research", {"question": body.text})
        return {"ok": r.ok, "text": r.output}

    # ---------------------------------------------------------------- Phase 9: optimization workspace

    @app.post("/api/optimize")
    def optimize(body: OptimizeIn):
        from ..optimization import engine
        from ..optimization.advanced import assignment_neighbours, tabu_search
        if body.algorithm == "pareto":
            r = aether.tools.call("pareto_schedule", {"tasks": body.tasks, "hours_per_day": body.hours_per_day})
        elif body.algorithm == "tabu":
            prob = engine.schedule_problem(engine.parse_tasks(body.tasks), [float(x) for x in body.hours_per_day.split(",")])
            return {"ok": True, "text": tabu_search(prob, assignment_neighbours(7)).report(prob)}
        elif body.algorithm == "exact":
            r = aether.tools.call("solve_plan", {"tasks": body.tasks, "hours_per_day": body.hours_per_day})
        else:
            r = aether.tools.call("optimize_schedule", {"tasks": body.tasks, "hours_per_day": body.hours_per_day})
        return {"ok": r.ok, "text": r.output}


    @app.get("/api/eval")
    def eval_cases():
        return aether.evals.rows()

    @app.post("/api/eval")
    def eval_add(body: EvalIn):
        return {"id": aether.evals.add(body.query, body.expect)}

    @app.delete("/api/eval/{cid}")
    def eval_del(cid: int):
        aether.evals.remove(cid)
        return {"ok": True}

    @app.post("/api/tune")
    def tune():
        try:
            return aether.tune_retrieval()
        except ValueError as e:
            raise HTTPException(400, str(e))


    @app.get("/api/settings")
    def settings_list():
        return aether.settings.list()

    @app.post("/api/settings")
    def settings_apply(body: SettingsIn):
        try:
            return {"changed": aether.settings.apply(body.section, body.values, body.reason)}
        except PermissionError as e:
            raise HTTPException(403, str(e))

    @app.post("/api/settings/revert")
    def settings_revert(section: str, key: str):
        return {"message": aether.settings.revert(section, key)}

    # ---------------------------------------------------------------- Phase 10: metacognition
    @app.get("/api/metrics")
    def metrics(days: float = 7):
        return aether.meta.summary(days)

    @app.post("/api/feedback")
    def feedback(body: FeedbackIn):
        aether.meta.feedback(body.turn_id, body.value)
        return {"ok": True}

    @app.get("/api/proposals")
    def proposals():
        aether.meta.propose(aether.cfg)
        return [p.__dict__ for p in aether.meta.proposals()]

    @app.post("/api/proposals/{pid}")
    def decide(pid: int, approve: bool):
        try:
            return {"message": aether.meta.decide(pid, approve, aether.settings, aether.prefs, aether.store)}
        except PermissionError as e:
            raise HTTPException(403, str(e))

    @app.get("/api/reviews")
    def reviews():
        return aether.meta.reviews()

    # ---------------------------------------------------------------- Phase 11: proactive notifications
    @app.get("/api/notifications")
    def notifications():
        return {"items": [n.__dict__ for n in aether.proactive.deliver()], "held": aether.proactive.held()}

    @app.post("/api/notifications/{nid}/dismiss")
    def dismiss(nid: int):
        aether.proactive.dismiss(nid)
        return {"ok": True}

    from fastapi import File, Form, UploadFile

    @app.post("/api/image")
    def image(file: UploadFile = File(...), question: str = Form("What is in this image?")):
        import tempfile
        suffix = Path(file.filename or "img.png").suffix or ".png"
        uploads = aether.cfg.home / "uploads"
        uploads.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=uploads) as tmp:
            tmp.write(file.file.read())
        try:
            answer = aether.vision.describe(tmp.name, question)
        except Exception as e:
            raise HTTPException(503, str(e))
        aether.store.add(f"Looked at an image ({file.filename}): {answer[:300]}", "episodic", "observed", 0.6)
        return {"text": answer}

    @app.post("/api/voice")
    def voice(file: UploadFile = File(...)):
        """Push-to-talk from the UI: audio in, transcript + reply out."""
        if not hasattr(app.state, "stt"):
            try:
                from ..speech.stt import SpeechToText
                app.state.stt = SpeechToText(aether.cfg.voice)
            except Exception as e:
                raise HTTPException(503, f"Speech-to-text unavailable: {e}")
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".webm", dir=aether.cfg.home) as tmp:
            tmp.write(file.file.read())
        text = app.state.stt.transcribe(tmp.name)
        Path(tmp.name).unlink(missing_ok=True)
        if not text:
            return {"transcript": "", "text": "", "notices": ["I didn't catch that."]}
        r = aether.handle(text)
        return {"transcript": text, "text": r.text, "notices": r.notices, "state": r.state.model_dump()}

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
