"""Voice pipeline: wake word -> listen -> speech-to-text -> AETHER -> speech,
with barge-in (talk over AETHER to interrupt it).

States: idle (waiting for the wake word) -> listening -> thinking -> speaking.
After a reply it keeps listening for 8 seconds for a follow-up, so a
conversation does not need the wake word every turn.
Run:  python -m aether.speech.voice_loop
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


class WakeWord:
    def __init__(self, cfg: VoiceConfig):
        from pathlib import Path

        from openwakeword.model import Model
        name = cfg.wake_word
        kwargs: dict = {"wakeword_models": [name]}
        verifier = Path(cfg.wake_verifier_path).expanduser() if cfg.wake_verifier_path else None
        if verifier and verifier.exists():  # tuned to your voice by scripts/learn_my_voice.py
            key = Path(name).stem if name.endswith(".onnx") else name
            kwargs.update(custom_verifier_models={key: str(verifier)}, custom_verifier_threshold=0.3)
        try:
            self.model = Model(**kwargs)
        except TypeError:  # older openWakeWord without verifiers
            self.model = Model(wakeword_models=[name])
        self.threshold = cfg.wake_threshold

    def heard(self, frame_int16) -> bool:
        scores = self.model.predict(frame_int16)
        return max(scores.values(), default=0) >= self.threshold


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
        self.wake = wake if wake is not None else WakeWord(cfg)
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
        print("Voice ready. Say the wake word to start (Ctrl+C to quit).")
        with sd.InputStream(samplerate=self.cfg.sample_rate, channels=1, dtype="float32", blocksize=block,
                            callback=self._on_audio):
            while self.running:
                try:
                    self.step(self.frames.get(timeout=0.5))
                except queue.Empty:
                    continue


def main() -> None:
    from ..app import build
    from ..core.logging import setup_logging
    from ..interface.cli import ask
    setup_logging("INFO")
    aether, problem = build(confirm=ask)
    if problem:
        raise SystemExit(problem)
    va = VoiceAssistant(aether, aether.cfg.voice)
    va.on_state = report_presence
    va.run()


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
    main()
