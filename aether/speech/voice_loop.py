"""Voice pipeline: wake word -> listen -> speech-to-text -> AETHER -> speech,
with barge-in (talk over AETHER to interrupt it).

States: idle (waiting for the wake word) -> listening -> thinking -> speaking.
After a reply it keeps listening for 8 seconds for a follow-up, so a
conversation does not need the wake word every turn.
Run:  python -m aether.speech.voice_loop            (loads its own copy of AETHER)
      python -m aether.speech.voice_loop --server   (uses the running server's model: what the tray starts)
Wake it with "hey Jarvis", or just say "Nova", "Jarvis" or "AETHER" (a command can follow in the same breath).
"""
from __future__ import annotations

import queue
import threading
import time

from ..core.config import VoiceConfig
from ..core.logging import log
from .chunker import SentenceChunker, clean
from .profile import WAKE, SpeechProfile, terms_from_memories
from .vad import Segmenter, rms

FRAME_MS = 30
FOLLOW_UP_S = 8
LOCK_PORT = 8769   # one microphone listener at a time


class RemoteAether:
    """AETHER's brain over the local server, so the voice process doesn't load a second copy of the model
    (two 7B models don't fit in 4 GB of VRAM). Same handle() shape as Aether, for VoiceAssistant."""

    def __init__(self, base: str = "http://127.0.0.1:8765", timeout: float = 600):
        self.base, self.timeout = base.rstrip("/"), timeout

    def handle(self, text: str, on_event=None, spoken: bool = True):
        import json
        import types
        import urllib.request
        req = urllib.request.Request(self.base + "/api/chat/stream", method="POST",
                                     data=json.dumps({"text": text, "voice": spoken}).encode(),
                                     headers={"Content-Type": "application/json"})
        reply, kind = "", None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("event:"):
                        kind = line[6:].strip()
                    elif line.startswith("data:") and kind:
                        data = json.loads(line[5:].strip() or "{}")
                        if kind == "done":
                            reply = data.get("text", "")
                        elif kind == "error":
                            reply = "Sorry, something went wrong: " + data.get("message", "")[:200]
                        elif on_event:
                            on_event(kind, data)
        except Exception as e:
            log.warning("AETHER server didn't answer: %s", e)
            reply = "I can't reach the AETHER server right now."
        return types.SimpleNamespace(text=reply)

    def alive(self) -> bool:
        import urllib.request
        try:
            urllib.request.urlopen(self.base + "/api/status", timeout=2).close()
            return True
        except Exception:
            return False

    def hotwords(self) -> list[str]:
        """Names and terms from your memories, as the server knows them."""
        import json
        import urllib.request
        try:
            with urllib.request.urlopen(self.base + "/api/speech/profile", timeout=5) as r:
                return list(json.loads(r.read()).get("hotwords", []))
        except Exception:
            return []


class WakeWord:
    """openWakeWord's "hey Jarvis" detector.

    Two things break it on a fresh Windows install, and both used to kill the whole listener:
    openWakeWord 0.6 defaults to TensorFlow Lite, which has no Windows build, and its pip package ships
    without the model files. So: always use ONNX, and fetch the model files (~6 MB) the first time."""

    def __init__(self, cfg: VoiceConfig):
        from pathlib import Path

        from openwakeword.model import Model
        name = cfg.wake_word
        custom = name.endswith(".onnx")
        if not custom:
            ensure_wake_models(name)
        kwargs: dict = {"wakeword_models": [name], "inference_framework": "onnx"}
        verifier = Path(cfg.wake_verifier_path).expanduser() if cfg.wake_verifier_path else None
        if verifier and verifier.exists():  # tuned to your voice by scripts/learn_my_voice.py
            key = Path(name).stem if custom else name
            kwargs.update(custom_verifier_models={key: str(verifier)}, custom_verifier_threshold=0.3)
        try:
            self.model = Model(**kwargs)
        except TypeError:  # older openWakeWord without verifiers or inference_framework
            self.model = Model(wakeword_models=[name])
        self.threshold = cfg.wake_threshold

    def heard(self, frame_int16) -> bool:
        scores = self.model.predict(frame_int16)
        return max(scores.values(), default=0) >= self.threshold


def ensure_wake_models(name: str) -> None:
    """Download openWakeWord's shared feature models and the named wake word model if they're missing."""
    from pathlib import Path

    import openwakeword
    models = Path(openwakeword.__file__).parent / "resources" / "models"
    have = {p.name for p in models.glob("*.onnx")} if models.is_dir() else set()
    if {"melspectrogram.onnx", "embedding_model.onnx"} <= have and any(n.startswith(name) for n in have):
        return
    from openwakeword.utils import download_models
    log.info("downloading the openWakeWord model for %s (one time, ~6 MB)", name)
    try:
        download_models(model_names=[name])
    except TypeError:  # pragma: no cover - older signature
        download_models()


class NoWakeWord:
    """Used when openWakeWord can't load: saying the name (checked by Whisper) still wakes AETHER."""

    def heard(self, frame_int16) -> bool:
        return False


class VoiceAssistant:
    def __init__(self, aether, cfg: VoiceConfig, stt=None, tts=None, wake=None, name_stt=None, profile=None):
        self.aether, self.cfg = aether, cfg
        if profile is None:
            acfg = getattr(aether, "cfg", None)
            profile = SpeechProfile.for_home(acfg.home, acfg.user_name, terms_from_memories(aether.store)) \
                if acfg is not None else SpeechProfile()
        self.profile = profile
        real = stt is None   # tests pass their own speech-to-text; only a real run loads Whisper models
        if stt is None:
            from .stt import SpeechToText
            stt = SpeechToText(cfg, profile)
        if name_stt is None and cfg.wake_by_name and real:
            try:
                from .stt import SpeechToText
                name_stt = SpeechToText(cfg, profile, model_name=cfg.wake_stt_model)
            except Exception as e:  # pragma: no cover - the wake word still works
                log.warning("say-the-name wake unavailable: %s", e)
        self.name_stt = name_stt
        self.idle_seg = Segmenter(cfg.vad_energy, 500, FRAME_MS)
        self.idle_seg.max_ms = 8000
        if tts is None:
            from .tts import TextToSpeech
            tts = TextToSpeech(cfg)
        self.stt, self.tts = stt, tts
        if wake is None:
            try:
                wake = WakeWord(cfg)
            except Exception as e:
                log.warning("the \"hey Jarvis\" detector didn't load (%s); listening for the name only", e)
                if self.name_stt is None:
                    raise
                wake = NoWakeWord()
        self.wake = wake
        self.frames: queue.Queue = queue.Queue(maxsize=500)
        self.on_state = None  # called with the new state (idle/listening/thinking/speaking), e.g. to tell Nova
        self.state = "idle"
        self.follow_up_until = 0.0
        self.running = True
        self.seg = Segmenter(cfg.vad_energy, cfg.silence_ms, FRAME_MS)
        self.barge_frames = 0
        self._gen = 0  # bumped to cancel queued speech

    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, value: str) -> None:
        changed = value != getattr(self, "_state", None)
        self._state = value
        if changed and self.on_state:
            try:
                self.on_state(value)
            except Exception:  # pragma: no cover - a display must never break the voice loop
                pass

    def _on_audio(self, indata, frames, t, status):
        try:
            self.frames.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def handle_utterance(self, audio) -> str:
        """Transcribe, answer, and start speaking at the first complete sentence while the
        model is still writing the rest (see speech/chunker.py)."""
        self.state = "thinking"
        text = self.stt.transcribe(audio)
        if not text:
            self.state = "listening"
            return ""
        return self.handle_text(text)

    def name_called(self, audio) -> bool:
        """While idle: was the assistant called by name ("hey Nova", or "Nova, what's the time")?
        A command said in the same breath is answered at once."""
        if self.name_stt is None:
            return False
        heard = self.name_stt.transcribe(audio).strip()
        m = WAKE.search(heard)
        if not m or m.start() > 12:      # the name has to come first, not in passing ("I told Nova ...")
            return False
        self.state = "listening"
        self.seg.reset()
        rest = heard[m.end():].strip(" ,.!?")
        if len(rest.split()) >= 2:       # a command in the same breath: transcribe it properly and answer
            full = self.stt.transcribe(audio)
            m2 = WAKE.search(full)
            rest = (full[m2.end():] if m2 else rest).strip(" ,.!?") or rest
            self.handle_text(rest)
        return True

    def handle_text(self, text: str) -> str:
        self.state = "thinking"
        log.info("heard: %s", text)
        self._gen += 1
        gen, q, started, replaced = self._gen, queue.Queue(), [False], [False]
        chunker, streamed = SentenceChunker(), []

        def start():
            if not started[0]:
                started[0] = True
                self.state = "speaking"
                threading.Thread(target=self._speak_queue, args=(q, gen), daemon=True).start()

        def on_event(kind, data):
            if kind == "token" and not replaced[0]:
                streamed.append(data.get("text", ""))
                for sentence in chunker.feed(data.get("text", "")):
                    q.put(sentence)
                    start()
            elif kind == "replace" and clean(data.get("text", "")) != clean("".join(streamed)):
                replaced[0] = True  # the verifier changed what was said: stop and say the corrected answer

        reply = self.aether.handle(text, on_event=on_event, spoken=True)
        if replaced[0] or not started[0]:
            if started[0]:
                self.tts.stop()
                self._gen += 1
                gen, q, started[0] = self._gen, queue.Queue(), False
            q.put(reply.text)
        else:
            for sentence in chunker.flush():
                q.put(sentence)
        q.put(None)
        start()
        return reply.text

    def _speak_queue(self, q: queue.Queue, gen: int) -> None:
        while True:
            sentence = q.get()
            if sentence is None or gen != self._gen:
                break
            if not self.tts.say(sentence):
                break  # interrupted (barge-in)
        if gen == self._gen and self.state == "speaking":
            self.state = "listening"
            self.follow_up_until = time.time() + FOLLOW_UP_S

    def step(self, frame) -> None:
        """Process one 30 ms float32 frame (exposed for testing)."""
        import numpy as np
        if self.state == "idle":
            if self.wake.heard((frame * 32767).astype(np.int16)):
                self.state = "listening"
                self.seg.reset()
                self.idle_seg.reset()
                return
            if self.name_stt is not None:
                audio = self.idle_seg.feed(frame)
                if audio is not None:
                    self.name_called(audio)
            return
        if self.state == "speaking":
            if self.cfg.barge_in and rms(frame) >= self.cfg.vad_energy * 2.5:
                self.barge_frames += 1
                if self.barge_frames >= 8:  # ~240 ms of clear speech interrupts
                    self._gen += 1  # drop the sentences still queued
                    self.tts.stop()
                    self.state = "listening"
                    self.seg.reset()
                    self.seg.feed(frame)
            else:
                self.barge_frames = 0
            return
        if self.state == "listening":
            audio = self.seg.feed(frame)
            if audio is not None:
                self.handle_utterance(audio)
            elif not self.seg.started and self.follow_up_until and time.time() > self.follow_up_until:
                self.state, self.follow_up_until = "idle", 0.0

    def ring(self, timer, title: str) -> None:
        """A timer or alarm went off: say it, unless AETHER is in the middle of a sentence."""
        what = timer.label or ("your alarm" if timer.kind == "alarm" else "your timer")
        line = f"{what[:1].upper() + what[1:]}: time's up." if timer.kind == "timer" else f"It's time. {what[:1].upper() + what[1:]}."
        threading.Thread(target=self.tts.say, args=(line,), daemon=True).start()

    def run(self) -> None:
        import sounddevice as sd
        proactive = getattr(self.aether, "proactive", None)
        if proactive is not None and self.ring not in proactive.listeners:
            proactive.listeners.append(self.ring)
        block = int(self.cfg.sample_rate * FRAME_MS / 1000)
        print('Voice ready. Say "hey Jarvis", or just "Nova", "Jarvis" or "AETHER" (Ctrl+C to quit).')
        with sd.InputStream(samplerate=self.cfg.sample_rate, channels=1, dtype="float32", blocksize=block,
                            callback=self._on_audio):
            while self.running:
                try:
                    self.step(self.frames.get(timeout=0.5))
                except queue.Empty:
                    continue


def single_instance():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        return s
    except OSError:
        s.close()
        return None


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - needs a microphone
    import argparse
    ap = argparse.ArgumentParser(prog="aether-voice", description="Always-listening voice for AETHER.")
    ap.add_argument("--server", action="store_true", help="use the running AETHER server instead of loading the model")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)
    from ..core.logging import ensure_streams, setup_logging
    ensure_streams("voice")
    setup_logging("INFO")
    lock = single_instance()
    if lock is None:
        print("AETHER is already listening (another aether-voice is running).")
        return 0
    if args.server:
        from ..core.config import load_config
        cfg = load_config(args.config)
        brain = RemoteAether(f"http://127.0.0.1:{args.port}")
        while not brain.alive():          # started with the tray: wait for the model to load
            time.sleep(2)
        profile = SpeechProfile.for_home(cfg.home, cfg.user_name, brain.hotwords())
        va = VoiceAssistant(brain, cfg.voice, profile=profile)
    else:
        from ..app import build
        from ..interface.cli import ask
        aether, problem = build(args.config, confirm=ask)
        if problem:
            raise SystemExit(problem)
        va = VoiceAssistant(aether, aether.cfg.voice)
    va.on_state = lambda state: report_presence(state, args.port)
    va.state = "idle"
    try:
        va.run()
    finally:
        lock.close()
    return 0


def report_presence(state: str, port: int = 8765) -> None:
    """Best effort: tell the local server (and so Nova) the voice state. Never blocks or fails the loop."""
    import json
    import urllib.request

    def send():
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/presence", method="POST",
                                         data=json.dumps({"state": state, "seconds": 120}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=0.5).close()
        except Exception:
            pass
    threading.Thread(target=send, daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
