"""Local speech-to-text with faster-whisper (CTranslate2), on the CPU.

Tuned for Indian-accented English: English is fixed (no language guessing), beam search is on, and the model
gets a prompt plus hotwords with your name, the assistant's name and the names and terms you use (see
speech/profile.py). Phrases it keeps mishearing are rewritten by your personal correction dictionary.
"""
from __future__ import annotations

from ..core.config import VoiceConfig


class SpeechToText:
    def __init__(self, cfg: VoiceConfig, profile=None, model_name: str | None = None):
        from faster_whisper import WhisperModel
        self.cfg, self.profile = cfg, profile
        self.model = WhisperModel(model_name or cfg.stt_model, device=cfg.stt_device, compute_type=cfg.stt_compute_type)
        self._hotwords_ok = True

    def options(self, beam_size: int | None = None) -> dict:
        opts = dict(language="en", vad_filter=True, beam_size=beam_size or self.cfg.stt_beam_size,
                    condition_on_previous_text=False)
        if self.profile is not None:
            prompt = self.profile.prompt()
            if self.cfg.stt_prompt:
                prompt = self.cfg.stt_prompt.strip() + " " + prompt
            opts["initial_prompt"] = prompt
            if self._hotwords_ok:
                opts["hotwords"] = self.profile.hotword_string()
        elif self.cfg.stt_prompt:
            opts["initial_prompt"] = self.cfg.stt_prompt
        return opts

    def raw(self, audio, beam_size: int | None = None) -> str:
        """Transcript before personal corrections (used by calibration)."""
        opts = self.options(beam_size)
        try:
            segments, _ = self.model.transcribe(audio, **opts)
        except TypeError:  # faster-whisper older than 1.0.2 has no hotwords
            self._hotwords_ok = False
            opts.pop("hotwords", None)
            segments, _ = self.model.transcribe(audio, **opts)
        return " ".join(s.text.strip() for s in segments).strip()

    def transcribe(self, audio, language: str | None = "en") -> str:
        """audio: float32 numpy array at 16 kHz, or a path to an audio file."""
        text = self.raw(audio)
        return self.profile.apply(text) if self.profile is not None else text
