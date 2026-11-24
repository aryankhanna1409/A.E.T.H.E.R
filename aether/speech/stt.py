"""Local speech-to-text with faster-whisper (CTranslate2), on the CPU."""
from __future__ import annotations

from ..core.config import VoiceConfig


class SpeechToText:
    def __init__(self, cfg: VoiceConfig):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(cfg.stt_model, device=cfg.stt_device, compute_type=cfg.stt_compute_type)

    def transcribe(self, audio, language: str | None = "en") -> str:
        """audio: float32 numpy array at 16 kHz, or a path to an audio file."""
        segments, _ = self.model.transcribe(audio, language=language, vad_filter=True, beam_size=1)
        return " ".join(s.text.strip() for s in segments).strip()
