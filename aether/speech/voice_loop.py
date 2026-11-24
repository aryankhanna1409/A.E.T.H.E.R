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
from .vad import Segmenter, rms

FRAME_MS = 30
FOLLOW_UP_S = 8


class WakeWord:
    def __init__(self, cfg: VoiceConfig):
        from openwakeword.model import Model
        name = cfg.wake_word
        self.model = Model(wakeword_models=[name]) if name.endswith(".onnx") else Model(wakeword_models=[name])
        self.threshold = cfg.wake_threshold

    def heard(self, frame_int16) -> bool:
        scores = self.model.predict(frame_int16)
        return max(scores.values(), default=0) >= self.threshold


class VoiceAssistant:
    def __init__(self, aether, cfg: VoiceConfig, stt=None, tts=None, wake=None):
        self.aether, self.cfg = aether, cfg
        if stt is None:
            from .stt import SpeechToText
            stt = SpeechToText(cfg)
        if tts is None:
            from .tts import TextToSpeech
            tts = TextToSpeech(cfg)
        self.stt, self.tts = stt, tts
        self.wake = wake if wake is not None else WakeWord(cfg)
        self.frames: queue.Queue = queue.Queue(maxsize=500)
        self.state = "idle"
        self.follow_up_until = 0.0
        self.running = True
        self.seg = Segmenter(cfg.vad_energy, cfg.silence_ms, FRAME_MS)
        self.barge_frames = 0
        self._gen = 0  # bumped to cancel queued speech

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

    def run(self) -> None:
        import sounddevice as sd
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
    VoiceAssistant(aether, aether.cfg.voice).run()


if __name__ == "__main__":
    main()
