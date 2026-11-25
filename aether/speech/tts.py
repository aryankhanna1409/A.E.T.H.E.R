"""Local text-to-speech with Piper, interruptible for barge-in."""
from __future__ import annotations

import re
import threading

from ..core.config import VoiceConfig


def speakable(text: str) -> str:
    """Strip markdown, citations and URLs so the voice reads naturally."""
    text = re.sub(r"```.*?```", " (code shown on screen) ", text, flags=re.S)
    text = re.sub(r"\[(web|doc|memory|source)\s*\d+\]", "", text, flags=re.I)
    text = re.sub(r"https?://\S+", "a link on screen", text)
    text = re.sub(r"[*_#>`|]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


def find_voice(configured: str) -> str | None:
    """The configured Piper voice, or else the first one get_model.py downloaded into models/voices."""
    from pathlib import Path
    if configured and Path(configured).expanduser().exists():
        return str(Path(configured).expanduser())
    root = Path(__file__).resolve().parents[2] / "models" / "voices"
    found = sorted(root.rglob("*.onnx")) if root.is_dir() else []
    return str(found[0]) if found else None


class TextToSpeech:
    def __init__(self, cfg: VoiceConfig):
        from piper import PiperVoice
        path = find_voice(cfg.tts_voice_path)
        if path is None:
            raise RuntimeError("No speaking voice: run scripts\\get_model.py voice, or set [voice].tts_voice_path "
                               "to a Piper .onnx voice (see README).")
        self.voice = PiperVoice.load(path)
        self.rate = self.voice.config.sample_rate
        self._stop = threading.Event()
        self.speaking = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def wav_bytes(self, text: str) -> bytes:
        """Synthesize to an in-memory WAV (for the web app to play)."""
        import io
        import wave
        pcm = b"".join(chunk.audio_int16_bytes for s in re.split(r"(?<=[.!?])\s+", speakable(text)) if s
                       for chunk in self.voice.synthesize(s))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(pcm)
        return buf.getvalue()

    def say(self, text: str) -> bool:
        """Speak sentence by sentence; returns False if interrupted."""
        import numpy as np
        import sounddevice as sd
        self._stop.clear()
        self.speaking.set()
        try:
            for sentence in re.split(r"(?<=[.!?])\s+", speakable(text)):
                if self._stop.is_set():
                    return False
                if not sentence:
                    continue
                pcm = b"".join(chunk.audio_int16_bytes for chunk in self.voice.synthesize(sentence))
                audio = np.frombuffer(pcm, dtype=np.int16)
                sd.play(audio, self.rate)
                while sd.get_stream().active:
                    if self._stop.wait(0.05):
                        sd.stop()
                        return False
            return True
        finally:
            self.speaking.clear()
