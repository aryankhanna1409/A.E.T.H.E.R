"""Local desktop UI server. Binds to 127.0.0.1 only and opens in your browser.
Run: python -m aether.interface.server   (or the `aether-server` command)

Permission prompts are shown in the UI: a tool that needs approval blocks the
request until you press Allow or Deny (or 120 s pass, which counts as Deny).
"""
from __future__ import annotations

import argparse
import os
import re
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..app import build
from ..core.logging import ensure_streams, setup_logging
from ..core.version import code_version
from ..health import grind_api
from ..neurosymbolic.rules import RuleEngine

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
    voice: bool = False  # the reply will be spoken: keep it short and plain


class EventIn(BaseModel):
    title: str
    when: str
    minutes: int = 60
    location: str = ""


class RoutineIn(BaseModel):
    name: str
    steps: str
    description: str = ""


class PrefEdit(BaseModel):
    key: str
    action: str  # on | off | delete | edit | add
    value: str = ""


class ImportIn(BaseModel):
    text: str
    filename: str = ""


class ImportSave(BaseModel):
    items: list[dict]
    label: str = ""


class CorrectionIn(BaseModel):
    heard: str
    meant: str


class PresenceIn(BaseModel):
    state: str
    seconds: float = 30


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


class ReminderIn(BaseModel):
    text: str
    when: str = "tomorrow"


class ConvEdit(BaseModel):
    title: str | None = None
    pinned: bool | None = None


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
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
    grind_api.register(app, aether)   # Grind 75 (75 Hard, food, body) at /grind/

    # What AETHER is doing right now, for Nova (the desktop pet) and other small displays.
    presence = {"state": "idle", "until": 0.0}

    @app.get("/api/presence")
    def get_presence():
        if getattr(aether, "_active_turns", 0) > 0 and presence["state"] not in {"speaking", "listening"}:
            return {"state": "thinking"}
        if presence["until"] > time.time():
            return {"state": presence["state"]}
        return {"state": "idle"}

    @app.post("/api/presence")
    def set_presence(body: PresenceIn):
        state = body.state if body.state in {"idle", "listening", "thinking", "speaking"} else "idle"
        presence.update(state=state, until=time.time() + max(1.0, min(body.seconds, 600.0)))
        return {"state": state}

    @app.get("/api/status")
    def status():
        return {"model_loaded": aether.llm is not None, "problem": problem,
                "embeddings": getattr(aether.store.embedder, "name", "lexical only (BM25)"),
                "memories": aether.store.count(), "facts": len(aether.kg.all()),
                "emergency_stop": aether.policy.emergency_stop,
                "read_roots": aether.cfg.security.allowed_read_roots,
                "write_roots": aether.cfg.security.allowed_write_roots,
                "network": aether.cfg.security.network, "version": code_version()}

    @app.post("/api/shutdown")
    def shutdown():
        """Used by the tray after an update to replace a server that is still running the old code.
        The server only listens on 127.0.0.1, so only programs on this PC can call it."""
        threading.Timer(0.5, lambda: os._exit(0)).start()
        return {"ok": True}

    # ---------------------------------------------------------------- speech profile (accent learning)
    heard = {"text": "", "t": 0.0}

    def _profile():
        if not hasattr(app.state, "speech_profile"):
            from ..speech.profile import SpeechProfile, terms_from_memories
            app.state.speech_profile = SpeechProfile.for_home(aether.cfg.home, aether.cfg.user_name,
                                                              terms_from_memories(aether.store))
        return app.state.speech_profile

    def _heard(text: str) -> str:
        heard.update(text=text, t=time.time())
        return text

    def _maybe_learn(typed: str) -> None:
        """A typed message right after a voice transcript that is a near-copy of it is a correction."""
        if heard["text"] and time.time() - heard["t"] < 120:
            from ..speech.profile import looks_like_correction
            if looks_like_correction(heard["text"], typed):
                _profile().learn(heard["text"], typed, confirmed=False)
            heard["text"] = ""

    @app.get("/api/speech/profile")
    def speech_profile():
        p = _profile()
        return {"prompt": p.prompt(), "hotwords": p.hotwords, "replacements": p.replacements}

    @app.post("/api/speech/correct")
    def speech_correct(body: CorrectionIn):
        return {"learnt": _profile().learn(body.heard[:500], body.meant[:500], confirmed=True)}

    @app.delete("/api/speech/replacements")
    def speech_forget(wrong: str):
        return {"removed": _profile().forget(wrong)}

    @app.post("/api/chat")
    def chat(body: ChatIn):  # sync: runs in a worker thread so approvals can block
        _maybe_learn(body.text)
        try:
            r = aether.handle(body.text)
        except Exception as e:
            raise HTTPException(503, str(e))
        last["state"] = r.state.model_dump()
        return {"text": r.text, "notices": r.notices, "state": last["state"]}

    @app.post("/api/chat/stream")
    def chat_stream(body: ChatIn):
        """Server-sent events: stage, tool, token, replace, then done (or error)."""
        _maybe_learn(body.text)
        import json
        import queue as _q
        q: _q.Queue = _q.Queue()

        def work():
            try:
                r = aether.handle(body.text, on_event=lambda kind, data: q.put((kind, data)), spoken=body.voice)
                last["state"] = r.state.model_dump()
                q.put(("done", {"text": r.text, "notices": r.notices, "state": last["state"],
                                "turn_id": r.turn_id, "session": aether.session}))
            except Exception as e:
                q.put(("error", {"message": str(e)}))
            q.put(None)

        threading.Thread(target=work, daemon=True, name="aether-turn").start()

        def events():
            while True:
                item = q.get()
                if item is None:
                    return
                kind, data = item
                yield f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---------------------------------------------------------------- conversations, search, graph
    @app.get("/api/conversations")
    def conversations():
        return {"current": aether.session, "items": aether.conversations.list()}

    @app.post("/api/conversations")
    def new_conversation():
        return {"session": aether.new_conversation()}

    @app.get("/api/conversations/{session}")
    def open_conversation(session: str):
        return {"session": session, "turns": aether.open_conversation(session)}

    @app.patch("/api/conversations/{session}")
    def edit_conversation(session: str, body: ConvEdit):
        if body.title is not None:
            aether.conversations.rename(session, body.title)
        if body.pinned is not None:
            aether.conversations.pin(session, body.pinned)
        return {"ok": True}

    @app.delete("/api/conversations/{session}")
    def delete_conversation(session: str):
        aether.conversations.delete(session)
        if session == aether.session:
            aether.new_conversation()
        return {"ok": True}

    @app.get("/api/search")
    def search(q: str):
        mems = aether.store.search(q, k=12, min_score=0.05)
        ql = q.lower()
        facts = [t for t in aether.kg.all() if ql in f"{t.s} {t.p} {t.o}".lower().replace("_", " ")][:15]
        return {
            "memories": [{"id": m.id, "layer": m.layer, "text": m.text[:300], "score": round(m.score, 3),
                          "path": m.meta.get("path", "")} for m in mems],
            "facts": [{"id": t.id, "s": t.s, "p": t.p, "o": t.o} for t in facts],
            "conversations": aether.conversations.search(q, 15),
        }

    @app.get("/api/graph")
    def graph(at: float | None = None):
        if at is None:
            triples = aether.kg.all()
            derived, _ = aether.rules.run(persist=False)
        else:  # time travel: what AETHER believed at `at`, with the rules re-run over that belief state
            past = aether.kg.snapshot_at(at)
            triples = past.all()
            derived, _ = RuleEngine(past, [r.text for r in aether.rules.rules]).run(persist=False)
        nodes, links = {}, []
        edges = [(t.s, t.p, t.o, "fact", t.id) for t in triples]
        edges += [(*d.triple, "derived", None) for d in derived[:200]]
        for s_, p_, o_, kind, tid in edges:
            for n in (s_, o_):
                nodes.setdefault(n, {"id": n, "degree": 0, "user": n == "user"})
                nodes[n]["degree"] += 1
            links.append({"source": s_, "target": o_, "label": p_, "kind": kind, "id": tid})
        return {"nodes": list(nodes.values()), "links": links}

    @app.get("/api/suggestions")
    def get_suggestions():
        return {"items": aether.suggestions.list(), "router": aether.intent_model.info()}

    @app.post("/api/suggestions/refresh")
    def refresh_suggestions():
        return aether.suggestions.refresh()

    @app.post("/api/suggestions/{sid}")
    def decide_suggestion(sid: int, body: dict):
        return {"message": aether.suggestions.decide(sid, bool(body.get("accept")))}

    @app.get("/api/plans")
    def get_plans():
        return {"text": aether.plans.text(), "today": aether.plans.today()}

    @app.post("/api/plans")
    def save_plans(body: dict):
        text = str(body.get("text", ""))
        if len(text) > 200_000:
            raise HTTPException(413, "plan too long")
        aether.plans.save(text)
        return {"today": aether.plans.today()}

    @app.get("/api/routines")
    def routines():
        return {"items": aether.routines.list(), "tools": aether.tools.names()}

    @app.post("/api/routines")
    def save_routine(body: RoutineIn):
        try:
            r = aether.routines.save(body.name, body.steps, set(aether.tools.names()), body.description)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"name": r.name, "steps": r.lines()}

    @app.delete("/api/routines/{name}")
    def delete_routine(name: str):
        return {"deleted": aether.routines.delete(name)}

    @app.post("/api/routines/{name}/run")
    def run_routine(name: str):
        r = aether.routines.get(name)
        if not r:
            raise HTTPException(404, "No such routine.")
        from ..tools.routines import summary
        results = aether.routines.run(r, aether.tools)
        return {"text": summary(r, results), "results": [{"tool": x.tool, "ok": x.ok, "decision": x.decision,
                                                           "output": x.output[:300]} for x in results]}

    @app.get("/api/calendar")
    def calendar(when: str = "today"):
        from datetime import datetime
        from ..memory import calendar as cal
        c, cc = aether.calendar, aether.cfg.calendar
        start, end, label = cal.resolve_range(when, datetime.now())
        one_day = (end - start).days <= 1
        return {"label": label, "start": start.isoformat(), "end": end.isoformat(),
                "events": [e.as_dict() for e in c.events(start, end)],
                "free": [[a.isoformat(timespec="minutes"), b.isoformat(timespec="minutes")]
                         for a, b in (c.free_slots(start.date(), cc.day_start, cc.day_end) if one_day else [])],
                "day_start": cc.day_start, "day_end": cc.day_end, "files": [f.name for f in c.files()],
                "folder": str(c.paths[0]) if c.paths else "", "outlook": cc.outlook}

    @app.post("/api/calendar")
    def add_event(body: EventIn):
        from datetime import datetime, timedelta
        from ..memory import calendar as cal
        start = cal.parse_when(body.when, datetime.now())
        if start is None:
            raise HTTPException(400, f"I couldn't tell when '{body.when}' is. Try 'friday 3pm'.")
        end = start + timedelta(minutes=max(5, min(24 * 60, body.minutes)))
        clash = aether.calendar.clash(start, end)
        ev = aether.calendar.add(body.title.strip()[:200] or "(no title)", start, end, body.location.strip()[:200])
        aether.audit.record("calendar.write", f"{ev.title} @ {ev.start.isoformat()}", True, "added by you in the app")
        return {**ev.as_dict(), "clash": [e.as_dict() for e in clash]}

    @app.get("/api/awareness")
    def awareness():
        """Focus right now and the deadlines AETHER has spotted, for the Today view."""
        from datetime import date as _date
        aw, cfgp = aether.proactive.awareness, aether.cfg.proactive
        now = time.time()
        f = aw.focus
        return {"focus": {"supported": f.last_active is not None or f.idle_probe() is not None,
                          "minutes": round(f.minutes(now)), "app": f.main_app(), "break_every": cfgp.focus_break_minutes},
                "deadlines": [{"title": d.title, "due": d.due.isoformat(), "source": d.source,
                               "days": (d.due - _date.today()).days} for d in aw.upcoming(_date.today(), 14)],
                "deadline_nudges": cfgp.deadline_nudges}

    @app.get("/api/timers")
    def timers():
        now = time.time()
        return {"items": [{"id": t.id, "label": t.label, "kind": t.kind, "due": t.due, "left": max(0, t.due - now)}
                          for t in aether.timers.running()]}

    @app.post("/api/timers")
    def add_timer(body: Text):
        r = aether.tools.call("timer", {"duration": body.text})
        if not r.ok or not r.output.startswith("Timer set"):
            raise HTTPException(400, r.output)
        return {"text": r.output}

    @app.delete("/api/timers/{tid}")
    def cancel_timer(tid: int):
        aether.timers.finish(tid, "cancelled")
        return {"ok": True}

    @app.get("/api/briefing")
    def briefing(online: bool = True):
        b = aether.briefing(online=online and aether.cfg.security.network)
        return {"text": b.written, "spoken": b.spoken, "sections": b.sections}

    @app.get("/api/weather")
    def weather(place: str = ""):
        try:
            w = aether.online.weather(place)
        except (ValueError, PermissionError) as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(502, f"Weather service unreachable: {e}")
        return {"place": w.place, "now": w.now_temp, "summary": w.now_summary, "line": w.line(0),
                "days": [{"day": d.day.isoformat(), "summary": d.summary, "high": d.high, "low": d.low,
                          "rain": d.rain_chance} for d in w.days]}

    @app.get("/api/mail")
    def mail(q: str = "", days: float = 3, limit: int = 12):
        from ..memory.mail import is_vip
        box, vip = aether.mail, set(aether.cfg.mail.vip)
        items = box.search(q, limit) if q.strip() else box.recent(days, limit)
        return {"items": [{**m.as_dict(), "vip": is_vip(m, vip)} for m in items], "files": len(box.files()),
                "folder": str(box.paths[0]) if box.paths else "", "outlook": aether.cfg.mail.outlook}

    @app.get("/api/kg/history")
    def kg_history(entity: str | None = None, limit: int = 400):
        events = aether.kg.history(entity, limit)
        return {"events": events, "first": events[0]["learned"] if events else None, "now": time.time()}

    @app.post("/api/whatif")
    def whatif(body: Text):
        try:
            r = aether.what_if(body.text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {**r.__dict__, "text": r.render()}

    @app.get("/api/traces")
    def traces(limit: int = 30):
        return aether.traces.recent(limit)

    @app.get("/api/traces/{turn_id}")
    def trace(turn_id: int):
        t = aether.traces.get(turn_id)
        if not t:
            raise HTTPException(404, "No trace for that turn.")
        return t

    @app.get("/api/brief")
    def brief():
        from datetime import datetime
        return {"text": aether.proactive.brief(datetime.now()),
                "reminders": [r.__dict__ for r in aether.reminders.open()[:8]],
                "goals": aether.graph.goals(20)}

    @app.get("/api/config")
    def config_view():
        c = aether.cfg
        from ..core.settings import TUNABLE
        return {"user_name": c.user_name, "model": Path(c.llm.model_path).name if c.llm.model_path else "",
                "gpu_layers": c.llm.gpu_layers, "context_tokens": c.llm.context_tokens, "temperature": c.llm.temperature,
                "memory": c.memory.model_dump(), "context": c.context.model_dump(),
                "security": {k: getattr(c.security, k) for k in ("network", "encrypt_at_rest", "screen_access",
                                                                  "ui_automation", "allowed_read_roots",
                                                                  "allowed_write_roots", "shell_allow")},
                "voice": {"tts": bool(c.voice.tts_voice_path), "stt_model": c.voice.stt_model},
                "vision": bool(c.vision.model_path), "proactive": c.proactive.model_dump(),
                "tunable": {k: sorted(v) for k, v in TUNABLE.items()},
                "tools": [{"name": t.name, "category": t.category, "description": t.description}
                          for t in aether.tools.tools.values()]}

    seen = {"visible": 0.0}

    @app.get("/api/pending")
    def get_pending(visible: bool = False):
        if visible:              # the app (or its HUD) is on screen and will show the approval
            seen["visible"] = time.time()
        return pending.list()

    @app.get("/api/pending/watch")
    def watch_pending():
        """For the tray: approvals waiting, and whether an AETHER window is on screen to show them."""
        return {"items": pending.list(), "window_visible": time.time() - seen["visible"] < 3}

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

    @app.post("/api/memories/import/preview")
    def import_preview(body: ImportIn):
        from ..memory.importer import mark_existing, parse
        items = mark_existing(parse(body.text[:2_000_000], body.filename), aether.store)
        return {"items": [c.as_dict() for c in items[:2000]]}

    @app.post("/api/memories/import")
    def import_save(body: ImportSave):
        from ..memory.importer import save
        return save(aether, [i for i in body.items[:2000] if isinstance(i, dict)], label=body.label or "import")

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

    @app.post("/api/reminders")
    def add_reminder(body: ReminderIn):
        from ..memory.prospective import parse_at, parse_repeat
        when = body.when or "today"
        repeat, at = parse_repeat(when), parse_at(when)   # "every day at 9pm" works here too
        day = "" if repeat else re.sub(r"\b(?:at\s+)?\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?\b|\b(?:morning|evening|night|tonight)\b",
                                       "", when).strip() or ("today" if at else when)
        return aether.reminders.add(body.text, day, repeat, at).__dict__

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
        if body.turn_id:
            aether.learner.rate(body.turn_id, body.value)
        return {"ok": True}

    @app.get("/api/learning")
    def learning():
        """The weekly summary, recent lessons and the last overnight pass."""
        from datetime import datetime as _dt
        r = aether.learner.report()
        last = aether.learner.last_run()
        return {"report": r, "last_run": last, "enabled": aether.cfg.learning.enabled,
                "lessons": [{"id": le.id, "kind": le.kind, "question": le.question[:200], "correction": le.correction[:300],
                             "lesson": le.lesson, "uses": le.uses, "created": le.created}
                            for le in aether.learner.lessons(30, ("correction", "disliked"))],
                "would_run": aether.learner.should_run(_dt.now(), aether.proactive.idle_probe(), None)[1]}

    @app.post("/api/learning/run")
    def learning_run():
        threading.Thread(target=aether.learner.run, args=("you asked",), daemon=True).start()
        return {"started": True}

    @app.delete("/api/learning/lessons/{lid}")
    def learning_forget(lid: int):
        aether.learner.forget(lid)
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

    @app.get("/api/alarms")
    def alarms(after: float = 0.0):
        """Timers and timed reminders that rang after `after` (the always-listening voice says them aloud)."""
        return {"items": aether.proactive.alarms_since(after), "now": time.time()}

    @app.post("/api/notifications/{nid}/dismiss")
    def dismiss(nid: int):
        aether.proactive.dismiss(nid)
        return {"ok": True}


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

    def _stt():
        if not hasattr(app.state, "stt"):
            try:
                from ..speech.stt import SpeechToText
                app.state.stt = SpeechToText(aether.cfg.voice, _profile())
            except Exception as e:
                raise HTTPException(503, f"Speech-to-text unavailable: {e}")
        return app.state.stt

    @app.post("/api/transcribe")
    def transcribe(file: UploadFile = File(...)):
        import tempfile
        stt = _stt()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".webm", dir=aether.cfg.home) as tmp:
            tmp.write(file.file.read())
        try:
            return {"text": _heard(stt.transcribe(tmp.name))}
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    @app.post("/api/speak")
    def speak(body: Text):
        if not hasattr(app.state, "tts"):
            try:
                from ..speech.tts import TextToSpeech
                app.state.tts = TextToSpeech(aether.cfg.voice)
            except Exception as e:
                raise HTTPException(503, f"Text-to-speech unavailable: {e}")
        return Response(app.state.tts.wav_bytes(body.text[:4000]), media_type="audio/wav")

    @app.post("/api/voice")
    def voice(file: UploadFile = File(...)):
        """Push-to-talk from the UI: audio in, transcript + reply out."""
        _stt()
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".webm", dir=aether.cfg.home) as tmp:
            tmp.write(file.file.read())
        text = _heard(app.state.stt.transcribe(tmp.name))
        Path(tmp.name).unlink(missing_ok=True)
        if not text:
            return {"transcript": "", "text": "", "notices": ["I didn't catch that."]}
        r = aether.handle(text, spoken=True)
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
    ensure_streams("server")   # started without a console (tray, sign-in): uvicorn needs a stdout
    setup_logging("INFO")
    app = create_app(args.config)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    os.environ["AETHER_URL"] = f"http://127.0.0.1:{args.port}"   # so "open the HUD" opens this server
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
