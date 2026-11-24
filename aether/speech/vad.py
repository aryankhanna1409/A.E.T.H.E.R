"""Energy-based voice activity detection and utterance segmentation.
Pure numpy, no model needed; tune [voice].vad_energy for your microphone."""
from __future__ import annotations

from dataclasses import dataclass, field


def rms(frame) -> float:
    import numpy as np
    return float(np.sqrt(np.mean(np.square(frame.astype("float32")))))


@dataclass
class Segmenter:
    """Feed 30 ms frames; returns a finished utterance when speech is followed by silence."""
    energy: float = 0.012
    silence_ms: int = 900
    frame_ms: int = 30
    min_speech_ms: int = 250
    max_ms: int = 30000
    frames: list = field(default_factory=list)
    speech_ms: int = 0
    silence_run: int = 0
    started: bool = False

    def feed(self, frame):
        import numpy as np
        loud = rms(frame) >= self.energy
        if loud:
            self.started = True
            self.speech_ms += self.frame_ms
            self.silence_run = 0
        elif self.started:
            self.silence_run += self.frame_ms
        if self.started:
            self.frames.append(frame)
        total = len(self.frames) * self.frame_ms
        if self.started and (self.silence_run >= self.silence_ms or total >= self.max_ms):
            audio = np.concatenate(self.frames) if self.speech_ms >= self.min_speech_ms else None
            self.reset()
            return audio
        return None

    def reset(self) -> None:
        self.frames, self.speech_ms, self.silence_run, self.started = [], 0, 0, False
