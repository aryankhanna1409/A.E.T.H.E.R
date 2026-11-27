"""Local text-to-speech, interruptible for barge-in.

Engines, best first:
* Kokoro (82M-parameter neural TTS, ONNX, runs on the CPU at several times real time): natural British voices
  (bm_george, bm_lewis, bm_fable, bm_daniel, bf_emma ...), blends of them ("bm_george:0.7,bm_lewis:0.3" makes a voice
  of your own), and tone: alerts are a touch quicker, everything else calm.
* Piper (smaller, robotic but quick).
* The built-in Windows voice, so it never goes silent.

Short replies he says all the time ("One moment.", "Right away.", "Done.") are synthesized once and cached, so they
start instantly.
"""
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


COMMON = ["One moment.", "Give me a second.", "Right away.", "On it.", "Done.", "Of course.", "Working on it.",
          "Yes?", "I'm here.", "Good morning.", "Okay."]
URGENT = re.compile(r"\b(?:warning|urgent|alarm|immediately|battery|stop|careful|leave now|in \d+ min(?:ute)?s?)\b|!$", re.I)


def split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", speakable(text)) if s]


class _Cached:
    """Shared by the neural engines: a phrase cache and interruptible playback of PCM."""
    rate = 22050

    def _init_cache(self) -> None:
        self.cache: dict[str, bytes] = {}
        self._stop = threading.Event()
        self.speaking = threading.Event()

    def pcm(self, sentence: str) -> bytes:
        raise NotImplementedError

    def warm(self, phrases: list[str] | None = None) -> int:
        """Synthesize the phrases he says all the time once, so they start with no delay."""
        n = 0
        for p in phrases or COMMON:
            if p not in self.cache:
                try:
                    self.cache[p] = self.pcm(p)
                    n += 1
                except Exception:
                    break
        return n

    def _pcm_cached(self, sentence: str) -> bytes:
        return self.cache.get(sentence) or self.pcm(sentence)

    def stop(self) -> None:
        self._stop.set()

    def wav_bytes(self, text: str) -> bytes:
        """Synthesize to an in-memory WAV (for the web app and the phone to play)."""
        import io
        import wave
        pcm = b"".join(self._pcm_cached(s) for s in split_sentences(text))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(pcm)
        return buf.getvalue()

    def say(self, text: str) -> bool:
        """Speak sentence by sentence, synthesizing the next one while this one plays; False if interrupted."""
        import numpy as np
        import sounddevice as sd
        from concurrent.futures import ThreadPoolExecutor
        self._stop.clear()
        self.speaking.set()
        sentences = split_sentences(text)
        try:
            with ThreadPoolExecutor(1) as ex:
                nxt = ex.submit(self._pcm_cached, sentences[0]) if sentences else None
                for i in range(len(sentences)):
                    if self._stop.is_set():
                        return False
                    pcm = nxt.result()
                    nxt = ex.submit(self._pcm_cached, sentences[i + 1]) if i + 1 < len(sentences) else None
                    sd.play(np.frombuffer(pcm, dtype=np.int16), self.rate)
                    while sd.get_stream().active:
                        if self._stop.wait(0.03):
                            sd.stop()
                            return False
            return True
        finally:
            self.speaking.clear()


def find_kokoro(model_path: str = "") -> tuple[str, str] | None:
    """(model, voices) for Kokoro: the configured model, or what get_model.py kokoro put in models/kokoro."""
    from pathlib import Path
    cands = [Path(model_path).expanduser()] if model_path else []
    root = Path(__file__).resolve().parents[2] / "models" / "kokoro"
    if root.is_dir():
        cands += sorted(root.glob("kokoro-v*.onnx"), key=lambda p: ("int8" not in p.name, p.name))
    for m in cands:
        if m.is_file():
            v = next((x for x in sorted(m.parent.glob("voices*.bin"))), None)
            if v:
                return str(m), str(v)
    return None


def blend(voices, spec: str):
    """'bm_george' or 'bm_george:0.7,bm_lewis:0.3' -> a style vector (a weighted mix of the voices)."""
    import numpy as np
    parts = []
    for item in spec.split(","):
        name, _, w = item.strip().partition(":")
        if name:
            parts.append((name.strip(), float(w) if w else 1.0))
    total = sum(w for _, w in parts) or 1.0
    missing = [n for n, _ in parts if n not in voices]
    if missing:
        raise ValueError(f"unknown Kokoro voice(s): {', '.join(missing)}")
    return sum(np.asarray(voices[n], dtype="float32") * (w / total) for n, w in parts)


class KokoroVoice(_Cached):
    rate = 24000

    def __init__(self, cfg: VoiceConfig, engine=None):
        if engine is None:
            from kokoro_onnx import Kokoro
            found = find_kokoro(getattr(cfg, "kokoro_model_path", ""))
            if found is None:
                raise RuntimeError("No Kokoro model: run scripts\\get_model.py kokoro")
            engine = Kokoro(*found)
        self.engine = engine
        self.style = blend(engine.voices, getattr(cfg, "kokoro_voice", "bm_george") or "bm_george")
        self.speed = float(getattr(cfg, "kokoro_speed", 1.0) or 1.0)
        self.lang = "en-gb" if (getattr(cfg, "kokoro_voice", "bm_george") or "b")[:1] == "b" else "en-us"
        self._init_cache()

    def tone_speed(self, sentence: str) -> float:
        """Prosody by sentence: alerts a little quicker, questions natural, everything else calm."""
        return min(2.0, self.speed * (1.1 if URGENT.search(sentence) else 1.0))

    def pcm(self, sentence: str) -> bytes:
        import numpy as np
        audio, _ = self.engine.create(sentence, voice=self.style, speed=self.tone_speed(sentence), lang=self.lang)
        return (np.clip(np.asarray(audio, dtype="float32"), -1, 1) * 32767).astype(np.int16).tobytes()


class TextToSpeech(_Cached):
    def __init__(self, cfg: VoiceConfig):
        from piper import PiperVoice
        path = find_voice(cfg.tts_voice_path)
        if path is None:
            raise RuntimeError("No speaking voice: run scripts\\get_model.py voice, or set [voice].tts_voice_path "
                               "to a Piper .onnx voice (see README).")
        self.voice = PiperVoice.load(path)
        self.rate = self.voice.config.sample_rate
        self._init_cache()

    def pcm(self, sentence: str) -> bytes:
        return b"".join(chunk.audio_int16_bytes for chunk in self.voice.synthesize(sentence))


class WindowsVoice:
    """Windows' built-in voice (SAPI), used when no Piper voice is installed, so the always-on listener can still
    answer out loud instead of crashing. One hidden PowerShell process speaks line by line; stop() ends it."""

    SCRIPT = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              "try { $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Male, "
              "[System.Speech.Synthesis.VoiceAge]::Adult, 0, [Globalization.CultureInfo]'en-GB') } catch {}; "
              "[Console]::InputEncoding = [Text.Encoding]::UTF8; "
              "while (($l = [Console]::In.ReadLine()) -ne $null) { if ($l) { $s.Speak($l) }; "
              "[Console]::Out.WriteLine('done'); [Console]::Out.Flush() }")

    def __init__(self, cfg: VoiceConfig | None = None):
        self._stop = threading.Event()
        self.speaking = threading.Event()
        self.proc = None
        self._start()

    def _start(self) -> None:
        import subprocess
        self.proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", self.SCRIPT],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8", creationflags=0x08000000)

    def stop(self) -> None:
        self._stop.set()
        if self.speaking.is_set() and self.proc and self.proc.poll() is None:
            self.proc.kill()   # SAPI can't be interrupted from outside mid-sentence; a fresh one starts next time

    def say(self, text: str) -> bool:
        self._stop.clear()
        self.speaking.set()
        try:
            for sentence in re.split(r"(?<=[.!?])\s+", speakable(text)):
                if self._stop.is_set():
                    return False
                if not sentence:
                    continue
                if self.proc is None or self.proc.poll() is not None:
                    self._start()
                try:
                    self.proc.stdin.write(sentence.replace("\n", " ") + "\n")
                    self.proc.stdin.flush()
                    self.proc.stdout.readline()   # 'done' once spoken; '' if stop() ended it
                except OSError:
                    return False
            return not self._stop.is_set()
        finally:
            self.speaking.clear()


def make_tts(cfg: VoiceConfig):
    """The best engine that's installed: Kokoro, then Piper, then (on Windows) the built-in voice.
    [voice] tts_engine = "kokoro" | "piper" | "windows" picks one; "auto" (the default) tries them in that order."""
    import sys
    from ..core.logging import log
    engine = getattr(cfg, "tts_engine", "auto") or "auto"
    errors = []
    for name, make in (("kokoro", KokoroVoice), ("piper", TextToSpeech), ("windows", WindowsVoice)):
        if engine not in ("auto", name):
            continue
        if name == "windows" and sys.platform != "win32":
            continue
        try:
            tts = make(cfg)
            if hasattr(tts, "warm") and name != "windows":
                threading.Thread(target=tts.warm, daemon=True, name="tts-warm").start()
            if errors:
                log.warning("speaking with %s (%s)", name, "; ".join(errors))
            return tts
        except Exception as e:
            errors.append(f"{name}: {e}")
    raise RuntimeError("No speaking voice. Run scripts\\get_model.py kokoro (best) or voice. " + "; ".join(errors))
