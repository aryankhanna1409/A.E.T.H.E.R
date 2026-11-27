"""HTTP routes for Grind 75 inside AETHER (the page at /grind/ talks to these instead of Electron)."""
from __future__ import annotations

import base64
import json
import os
import re
import threading
from pathlib import Path

from pydantic import BaseModel, Field

from ..core.logging import log
from ..llm.base import Message, structured
from .grind import COACH_PROMPT, FOODS_FILE, FoodTable, Grind, grind75_app_file, merge_defaults, default_state

WEB = Path(__file__).resolve().parents[1] / "interface" / "web" / "grind"
COMMONS = ["Category:Featured pictures of landscapes", "Category:Featured pictures of mountains",
           "Category:Featured pictures of the night sky", "Category:Featured pictures of waterfalls",
           "Category:Featured pictures of lakes", "Category:Featured pictures of forests"]


class FoodItem(BaseModel):
    name: str
    quantity: str = ""
    kcal: float = Field(ge=0)
    protein: float = Field(ge=0)
    carbs: float = Field(0, ge=0)
    fat: float = Field(0, ge=0)


class FoodEstimate(BaseModel):
    items: list[FoodItem]


def _seeded(arr: list, seed: str):
    h = 2166136261
    for c in seed:
        h = ((h ^ ord(c)) * 16777619) & 0xFFFFFFFF
    return arr[h % len(arr)]


def set_wallpaper(file: str) -> dict:
    if os.name != "nt":
        return {"ok": False, "error": "setting the desktop wallpaper works on Windows only"}
    try:
        import ctypes
        ok = ctypes.windll.user32.SystemParametersInfoW(20, 0, str(file), 3)  # SPI_SETDESKWALLPAPER, update + notify
        return {"ok": bool(ok)} if ok else {"ok": False, "error": "Windows refused the wallpaper"}
    except Exception as e:  # pragma: no cover - Windows only
        return {"ok": False, "error": str(e)}


def register(app, aether) -> None:
    from fastapi import HTTPException
    from fastapi.responses import FileResponse, Response
    from fastapi.staticfiles import StaticFiles

    grind: Grind = aether.grind
    walls = grind.dir / "wallpapers"
    llm_lock = threading.Lock()

    @app.get("/grind")
    @app.get("/grind/")
    def grind_index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/grind/foods.js")
    def grind_foods():
        d = json.loads(FOODS_FILE.read_text(encoding="utf-8"))
        js = (f"window.FOODS = {json.dumps(d['foods'], ensure_ascii=False)};\n"
              f"window.UNITS = {json.dumps(d['units'])};\nwindow.NUMBER_WORDS = {json.dumps(d['number_words'])};\n")
        return Response(js, media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/api/grind/state")
    def grind_state():
        return grind.snapshot()

    @app.get("/api/grind/rev")
    def grind_rev():
        return {"rev": grind.rev}

    @app.post("/api/grind/state")
    def grind_save(body: dict):
        st = body.get("state")
        if not isinstance(st, dict) or "days" not in st:
            raise HTTPException(400, "not a Grind 75 document")
        return grind.save_from_ui(st, body.get("rev"))

    @app.get("/api/grind/summary")
    def grind_summary():
        return {"status": grind.status(), "insights": grind.insights(), "streak": grind.streak(),
                "challenge": grind.challenge(), "rest": grind.rest_advice()}

    @app.post("/api/grind/open-folder")
    def grind_folder():
        if os.name == "nt":
            os.startfile(grind.dir)  # noqa: S606 - opens AETHER's own data folder in Explorer
        return {"path": str(grind.dir)}

    @app.post("/api/grind/export")
    def grind_export(body: dict):
        out = Path(os.path.expanduser("~")) / "Downloads"
        out = out if out.exists() else grind.dir
        p = out / f"grind75-backup-{__import__('datetime').date.today().isoformat()}.json"
        p.write_text(json.dumps(body.get("state") or grind.snapshot(), ensure_ascii=False, indent=1), encoding="utf-8")
        return {"path": str(p)}

    @app.post("/api/grind/import-app")
    def grind_import():
        src = grind75_app_file()
        if not src:
            raise HTTPException(404, "The Grind 75 app's data file wasn't found in %APPDATA%.")
        return {"state": merge_defaults(default_state(grind.user_name), json.loads(src.read_text(encoding="utf-8")))}

    @app.get("/api/grind/ai")
    def grind_ai():
        return {"aether": aether.llm is not None}

    @app.post("/api/grind/food")
    def grind_food(body: dict):
        text = str(body.get("text", ""))[:600]
        if aether.llm is None:
            return {"ok": False, "reason": "no-model", "error": "AETHER's model is not loaded"}
        local, _ = FoodTable(grind.state.get("customFoods")).parse(text)
        hint = "; ".join(f"{x['name']} ({x['quantity']}): {x['kcal']} kcal, {x['protein']} g protein" for x in local)
        try:
            with llm_lock:
                est = structured(aether.llm, [
                    Message("system", "You estimate nutrition for home-cooked Indian food. Typical portions: 1 katori = "
                            "~150 ml cooked, 1 roti = 30-35 g atta, 1 scoop whey = ~30 g powder. Split the input into "
                            "separate items and give kcal, protein, carbs and fat for the stated quantity."
                            + (f" A food table says: {hint}. Stay close to it for those items." if hint else "")),
                    Message("user", text)], FoodEstimate, max_tokens=500)
        except Exception as e:
            return {"ok": False, "reason": "model-error", "error": str(e)[:200]}
        items = [{"name": i.name, "quantity": i.quantity, "kcal": round(i.kcal), "protein": round(i.protein, 1),
                  "carbs": round(i.carbs, 1), "fat": round(i.fat, 1)} for i in est.items]
        return {"ok": True, "provider": "aether", "items": items}

    @app.post("/api/grind/chat")
    def grind_chat(body: dict):
        if aether.llm is None:
            return {"ok": False, "reason": "no-model", "error": "AETHER's model is not loaded"}
        ctx = str(body.get("context") or grind.coach_context())[:6000]
        extra = ""
        rest = grind.rest_advice()
        if rest:
            extra = f"\nRest signal: {rest}"
        msgs = [Message("system", COACH_PROMPT + "\n\nThe user's data:\n" + grind.coach_context() + extra +
                        ("\n\nWhat the page sent:\n" + ctx if ctx else ""))]
        for m in (body.get("messages") or [])[-12:]:
            if m.get("role") in {"user", "assistant"}:
                msgs.append(Message(m["role"], str(m.get("content", ""))[:2000]))
        try:
            with llm_lock:
                text = aether.llm.chat(msgs, max_tokens=500, temperature=0.5).strip()
        except Exception as e:
            return {"ok": False, "reason": "model-error", "error": str(e)[:200]}
        return {"ok": True, "text": text}

    @app.post("/api/grind/wallpaper")
    def grind_wall(body: dict):
        walls.mkdir(parents=True, exist_ok=True)
        date = re.sub(r"[^0-9-]", "", str(body.get("date", "")))[:10]
        kind = re.sub(r"[^a-z]", "", str(body.get("kind", "")))[:12]
        data = str(body.get("dataUrl", ""))
        if not date or "," not in data:
            raise HTTPException(400, "bad wallpaper")
        f = walls / f"{date}-{kind}.{'png' if 'image/png' in data[:30] else 'jpg'}"
        f.write_bytes(base64.b64decode(data.split(",", 1)[1]))
        return {"file": str(f)}

    @app.post("/api/grind/scenery")
    def grind_scenery(body: dict):
        """A Wikimedia Commons featured picture for the day (free licence, no key), cached per day."""
        date = re.sub(r"[^0-9-]", "", str(body.get("date", "")))[:10]
        if not date or not aether.cfg.security.network:
            return {"ok": False}
        walls.mkdir(parents=True, exist_ok=True)
        f, meta = walls / f"{date}-scenery.jpg", walls / f"{date}-scenery.json"
        if not (f.exists() and meta.exists()):
            import httpx
            ua = {"User-Agent": "AETHER-Grind75/1.0 (personal desktop app)"}
            first = _seeded(COMMONS, date)
            info = None
            for cat in [first] + [c for c in COMMONS if c != first]:
                try:
                    j = httpx.get("https://commons.wikimedia.org/w/api.php", headers=ua, timeout=15, params={
                        "action": "query", "format": "json", "generator": "categorymembers", "gcmtitle": cat,
                        "gcmtype": "file", "gcmlimit": 200, "prop": "imageinfo",
                        "iiprop": "url|extmetadata|size", "iiextmetadatafilter": "Artist|LicenseShortName",
                        "iiurlwidth": 2560}).json()
                    pages = [p for p in (j.get("query", {}).get("pages") or {}).values()
                             if p.get("imageinfo") and p["imageinfo"][0]["width"] > p["imageinfo"][0]["height"]
                             and re.search(r"\.(jpe?g|png)$", p["title"], re.I)]
                    if not pages:
                        continue
                    p = _seeded(sorted(pages, key=lambda x: x["title"]), date + cat)
                    ii = p["imageinfo"][0]
                    f.write_bytes(httpx.get(ii.get("thumburl") or ii["url"], headers=ua, timeout=60).content)
                    md = ii.get("extmetadata") or {}
                    artist = re.sub(r"<[^>]+>", "", (md.get("Artist") or {}).get("value", "Unknown")).strip()
                    lic = (md.get("LicenseShortName") or {}).get("value", "")
                    info = {"credit": f"{artist} · {lic} · Wikimedia Commons", "link": ii.get("descriptionurl")}
                    meta.write_text(json.dumps(info), encoding="utf-8")
                    break
                except Exception as e:
                    log.info("wallpaper source %s failed: %s", cat, e)
            if info is None:
                return {"ok": False}
        info = json.loads(meta.read_text(encoding="utf-8"))
        data = "data:image/jpeg;base64," + base64.b64encode(f.read_bytes()).decode("ascii")
        return {"ok": True, "dataUrl": data, "file": str(f), **info}

    @app.post("/api/grind/desktop")
    def grind_desktop(body: dict):
        f = Path(str(body.get("file", "")))
        if not f.exists() or walls.resolve() not in f.resolve().parents:
            return {"ok": False, "error": "unknown wallpaper file"}
        return set_wallpaper(str(f))

    app.mount("/grind", StaticFiles(directory=WEB), name="grind")
