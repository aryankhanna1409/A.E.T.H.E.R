"""Phone relay: call AETHER in from anywhere in the house.

Off by default. With ``[relay] enabled = true`` a second, small web server listens on your home Wi-Fi (port 8766)
and serves a phone page: hold the button and talk, or type. Your speech is transcribed on the PC, AETHER answers
and the reply is spoken on the phone in his own voice. The live state (listening, thinking, what he heard) comes
from the same event bus the HUD uses.

Safety:
* Every request needs the pairing token (a long random secret made on first run, shown in Settings with the
  link to open on the phone). Without it nothing answers but a 401.
* A turn from the phone never skips the Allow box: if you ask the phone to open an app or delete something, it's
  approved on the PC like anything AETHER didn't hear you say at the PC.
* Only the relay's few endpoints are on the network; the main app, settings and APIs stay on 127.0.0.1.
"""
from __future__ import annotations

import hmac
import io
import json
import secrets
import socket
import threading
import time
import wave
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

PAGE = Path(__file__).resolve().parent / "web" / "phone.html"
MAX_AUDIO_S = 30


def token_for(home: Path) -> str:
    p = Path(home) / "relay_token"
    try:
        t = p.read_text(encoding="utf-8").strip()
        if len(t) >= 24:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(24)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(t, encoding="utf-8")
    return t


def lan_ip() -> str:
    """This PC's address on the home network (no traffic is sent: a UDP 'connect' only picks the route)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class TextIn(BaseModel):
    text: str


def pcm16_from_wav(data: bytes):
    """The phone sends 16 kHz mono 16-bit WAV; returns float32 samples."""
    import numpy as np
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError("expected 16-bit mono WAV")
        rate = w.getframerate()
        frames = w.readframes(min(w.getnframes(), rate * MAX_AUDIO_S))
    audio = np.frombuffer(frames, dtype=np.int16).astype("float32") / 32768.0
    if rate != 16000 and len(audio):
        n = int(len(audio) * 16000 / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype("float32")
    return audio


def create_relay(aether, stt=None, tts=None) -> FastAPI:
    app = FastAPI(title="AETHER relay", docs_url=None, redoc_url=None, openapi_url=None)
    token = token_for(aether.cfg.home)
    lock = threading.Lock()   # one phone turn at a time
    state = {"stt": stt, "tts": tts, "last_wav": b""}

    def check(request: Request) -> None:
        got = request.headers.get("x-aether-token") or request.query_params.get("t") or request.cookies.get("aether_t", "")
        if not hmac.compare_digest(got.encode(), token.encode()):
            raise HTTPException(401, "Pair this phone first: open the link from AETHER's Settings.")

    def get_stt():
        if state["stt"] is None:
            from ..speech.profile import SpeechProfile
            from ..speech.stt import SpeechToText
            state["stt"] = SpeechToText(aether.cfg.voice, SpeechProfile.for_home(aether.cfg.home, aether.cfg.user_name))
        return state["stt"]

    def get_tts():
        if state["tts"] is None:
            try:
                from ..speech.tts import make_tts
                state["tts"] = make_tts(aether.cfg.voice)
            except Exception:
                state["tts"] = False
        return state["tts"] or None

    def answer(text: str) -> dict:
        with lock:
            aether.bus.publish("device", {"device": "phone", "state": "thinking"})
            r = aether.handle(text, spoken=True, origin="phone")
        tts = get_tts()
        voice = False
        if tts is not None and r.text:
            try:
                state["last_wav"] = tts.wav_bytes(r.text)
                voice = True
            except Exception:
                state["last_wav"] = b""
        return {"heard": text, "text": r.text, "voice": voice, "at": time.time()}

    @app.get("/", response_class=HTMLResponse)
    def page(request: Request):
        check(request)
        resp = HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})
        resp.set_cookie("aether_t", token, httponly=True, samesite="strict", max_age=3600 * 24 * 180)
        return resp

    @app.post("/api/text")
    def text_turn(body: TextIn, request: Request):
        check(request)
        t = body.text.strip()[:2000]
        if not t:
            raise HTTPException(400, "empty")
        return answer(t)

    @app.post("/api/talk")
    async def talk(request: Request):
        check(request)
        data = await request.body()
        if len(data) > 16000 * 2 * MAX_AUDIO_S + 1024:
            raise HTTPException(413, "too long")
        try:
            audio = pcm16_from_wav(data)
        except Exception:
            raise HTTPException(400, "send 16-bit mono WAV")
        if len(audio) < 1600:
            return {"heard": "", "text": "", "voice": False}
        aether.bus.publish("device", {"device": "phone", "state": "transcribing"})
        heard = get_stt().transcribe(audio).strip()
        if not heard:
            return {"heard": "", "text": "I didn't catch that.", "voice": False}
        return answer(heard)

    @app.get("/api/voice.wav")
    def voice(request: Request):
        check(request)
        if not state["last_wav"]:
            raise HTTPException(404, "nothing to say")
        return Response(state["last_wav"], media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @app.get("/api/events")
    def events(request: Request, after: int = -1):
        check(request)
        want = {"presence", "stage", "reply", "notify", "heard", "partial"}
        start = aether.bus.seq if after < 0 else after

        def gen():
            seq, end = start, time.time() + 600
            while time.time() < end:
                evs = aether.bus.wait(seq, 15.0, want)
                if not evs:
                    yield ": keep-alive\n\n"
                    continue
                for e in evs:
                    seq = e.seq
                    d = e.json()
                    if e.kind == "reply":
                        d["data"] = {"text": e.data.get("text", "")[:400], "origin": e.data.get("origin")}
                    yield f"event: {e.kind}\ndata: {json.dumps(d, default=str)}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/hello")
    def hello(request: Request):
        check(request)
        from ..core.version import MARK
        return {"name": aether.cfg.name, "mark": MARK, "user": aether.cfg.user_name}

    return app


def certificate(home: Path) -> tuple[str, str]:
    """A self-signed certificate for the relay. Phones only allow the microphone on https pages, so the relay
    serves https; the phone shows a warning once ("not private") because the certificate is AETHER's own."""
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    d = Path(home) / "relay"
    key_p, cert_p = d / "key.pem", d / "cert.pem"
    if key_p.exists() and cert_p.exists():
        return str(cert_p), str(key_p)
    d.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AETHER relay")])
    now = datetime.now(timezone.utc)
    import ipaddress
    alt = [x509.DNSName("localhost")]
    try:
        alt.append(x509.IPAddress(ipaddress.ip_address(lan_ip())))
    except ValueError:
        pass
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650)).add_extension(x509.SubjectAlternativeName(alt), False)
            .sign(key, hashes.SHA256()))
    key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(cert_p), str(key_p)


def info(aether) -> dict:
    rc = getattr(aether.cfg, "relay", None)
    port = rc.port if rc else 8766
    scheme = "https" if (rc is None or rc.https) else "http"
    t = token_for(aether.cfg.home)
    return {"enabled": bool(rc and rc.enabled), "port": port, "url": f"{scheme}://{lan_ip()}:{port}/?t={t}"}


def start(aether) -> None:  # pragma: no cover - needs the network
    """Run the relay on the home network in the background (called by aether-server when enabled)."""
    import uvicorn
    from ..core.logging import log
    rc = aether.cfg.relay
    app = create_relay(aether)
    log.info("phone relay on port %d: open the link from Settings on your phone", rc.port)
    kw = {}
    if rc.https:
        cert, key = certificate(aether.cfg.home)
        kw = {"ssl_certfile": cert, "ssl_keyfile": key}
    threading.Thread(target=lambda: uvicorn.run(app, host=rc.host, port=rc.port, log_level="warning", **kw),
                     daemon=True, name="aether-relay").start()
