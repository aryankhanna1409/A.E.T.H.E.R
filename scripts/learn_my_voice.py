"""Teach AETHER your voice and accent (about 5 minutes).

    .\\.venv\\Scripts\\python scripts\\learn_my_voice.py

You read 20 short phrases aloud. For each one AETHER shows what it heard, and learns from the difference:
- words it misheard go into your personal correction dictionary ("on" -> "Aundh")
- names and terms go into the hotword list Whisper is given every time
Then you say "AETHER" and "Nova" a few times, so you can see he hears his name in your accent.
Run it again any time; it keeps what it learnt and adds to it. Your recordings stay in AETHER's data folder.
"""
from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PHRASES = [
    "Hey Nova, what's the weather in Pune today?",
    "Set a timer for twelve minutes for the pasta.",
    "Remind me to call my dad at six thirty in the evening.",
    "Play some lo-fi music on YouTube.",
    "What's on my calendar for tomorrow morning?",
    "Take a note: order masala dosa and filter coffee on Swiggy.",
    "Search my files for the search and rescue robot report.",
    "What is fifteen percent of two thousand four hundred rupees?",
    "Open Visual Studio Code and GitHub.",
    "Summarise what I copied in three bullet points.",
    "How long is left on the pasta timer?",
    "Turn the volume down a little and pause the music.",
    "Convert five kilometres to miles.",
    "Explain how a Raspberry Pi talks to an Arduino over serial.",
    "Draft a reply saying I'll send the ROS two launch file tonight.",
    "What did I say about the YOLO model last week?",
    "Lock my laptop.",
    "Read me the news headlines about India.",
    "Wake me up at seven fifteen tomorrow.",
    "Nova, good morning. Brief me.",
]
WAKE_PHRASES = ["AETHER", "Hey AETHER", "AETHER, what's the time?", "Nova", "Hey Nova", "Nova, good morning"]


def wer(a: str, b: str) -> float:
    """Word error rate of transcript a against the reference b."""
    import difflib

    from aether.speech.profile import _norm, words
    x, y = [_norm(w) for w in words(a)], [_norm(w) for w in words(b)]
    if not y:
        return 0.0
    sm = difflib.SequenceMatcher(a=x, b=y, autojunk=False)
    errors = sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")
    return errors / len(y)


def record(cfg, seconds_max: float = 10.0):
    """Record one utterance: starts when you speak, stops after a short silence."""
    import numpy as np
    import sounddevice as sd

    from aether.speech.vad import Segmenter
    frame = int(cfg.sample_rate * 0.03)
    seg = Segmenter(cfg.vad_energy, 900, 30)
    seg.max_ms = int(seconds_max * 1000)
    with sd.InputStream(samplerate=cfg.sample_rate, channels=1, dtype="float32", blocksize=frame) as stream:
        start = time.time()
        while time.time() - start < seconds_max + 5:
            data, _ = stream.read(frame)
            audio = seg.feed(data[:, 0].copy())
            if audio is not None:
                return audio
    return np.zeros(0, dtype="float32")


def save_wav(path: Path, audio, rate: int = 16000) -> Path:
    import numpy as np
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return path


def train_wake_verifier(cfg, positives: list[Path], negatives: list[Path], out: Path) -> str:
    """Tune "hey Jarvis" to your voice with openWakeWord's custom verifier. Returns a message."""
    if len(positives) < 3 or not negatives:
        return "Not enough clips to tune the wake word."
    try:
        import openwakeword
        model = cfg.wake_word if cfg.wake_word.endswith(".onnx") else f"{cfg.wake_word}.onnx"
        openwakeword.train_custom_verifier(positive_reference_clips=[str(p) for p in positives],
                                           negative_reference_clips=[str(p) for p in negatives],
                                           output_path=str(out), model_name=model)
        return f"Wake word tuned to your voice: {out}"
    except Exception as e:  # pragma: no cover - depends on the openWakeWord version
        return f"Couldn't tune the wake word ({e}). Saying the name still works."


def set_toml_value(toml: Path, section: str, key: str, value: str) -> None:
    """Set key = "value" in [section] of aether.toml, keeping everything else as it is."""
    import re
    text = toml.read_text(encoding="utf-8") if toml.exists() else ""
    line = f'{key} = "{value}"'
    sec = re.search(rf"(?m)^\[{re.escape(section)}\][^\n]*\n", text)
    if not sec:
        text = text.rstrip() + f"\n\n[{section}]\n{line}\n"
    else:
        end = re.search(r"(?m)^\[", text[sec.end():])
        body_end = sec.end() + (end.start() if end else len(text) - sec.end())
        body = text[sec.end():body_end]
        if re.search(rf"(?m)^{re.escape(key)}\s*=", body):
            body = re.sub(rf"(?m)^{re.escape(key)}\s*=.*$", line.replace("\\", "/"), body)
        else:
            body = line + "\n" + body
        text = text[:sec.end()] + body + text[body_end:]
    toml.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - needs a microphone
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to aether.toml")
    ap.add_argument("--phrases", type=int, default=len(PHRASES), help="how many phrases to read (default 20)")
    ap.add_argument("--no-wake", action="store_true", help="skip the wake word part")
    args = ap.parse_args(argv)

    from aether.cognition.orchestrator import Aether
    from aether.core.config import load_config
    from aether.speech.profile import SpeechProfile, terms_from_memories
    from aether.speech.stt import SpeechToText

    toml = Path(args.config) if args.config else ROOT / "aether.toml"
    cfg = load_config(toml if toml.exists() else None)
    aether = Aether(cfg, None, None)
    profile = SpeechProfile.for_home(cfg.home, cfg.user_name, terms_from_memories(aether.store))
    print("Loading speech-to-text...")
    stt = SpeechToText(cfg.voice, profile)
    clips = cfg.home / "voice" / "calibration"
    print(f"\nRead each phrase aloud in your normal voice, after you press Enter. ({args.phrases} phrases)\n")
    before = after = 0.0
    negatives: list[Path] = []
    n = 0
    for i, phrase in enumerate(PHRASES[:args.phrases], 1):
        input(f"[{i}/{args.phrases}]  {phrase}\n        Press Enter, then speak...")
        audio = record(cfg.voice)
        if len(audio) < 1600:
            print("        (didn't hear anything, skipping)\n")
            continue
        negatives.append(save_wav(clips / f"phrase_{i:02d}.wav", audio, cfg.voice.sample_rate))
        raw = stt.raw(audio)
        learnt = profile.learn(raw, phrase, confirmed=True)
        fixed = profile.apply(raw)
        b, a = wer(raw, phrase), wer(fixed, phrase)
        before, after, n = before + b, after + a, n + 1
        print(f"        heard: {raw}")
        if learnt:
            print("        learnt: " + ", ".join(f'"{w}" -> "{r}"' for w, r in learnt))
        print()
    if n:
        print(f"Accuracy on your voice: {100 * (1 - before / n):.0f}% before learning, "
              f"{100 * (1 - after / n):.0f}% with your corrections.")
        print(f"Personal corrections: {len(profile.replacements)}. Hotwords: {len(profile.hotwords)}. "
              f"Saved in {profile.path}")

    if not args.no_wake:
        print("\nNow the wake word. Say each one when asked, like you would to call AETHER from across the desk.\n")
        hits = 0
        from aether.speech.profile import is_wake_phrase
        for i, phrase in enumerate(WAKE_PHRASES, 1):
            input(f"[{i}/{len(WAKE_PHRASES)}]  {phrase}   (Enter, then speak)")
            audio = record(cfg.voice, 4)
            if len(audio) < 1600:
                continue
            save_wav(clips / f"wake_{i:02d}.wav", audio, cfg.voice.sample_rate)
            heard = stt.raw(audio)
            profile.learn(heard, phrase, confirmed=True)
            ok = is_wake_phrase(heard)
            hits += ok
            print(f"        heard: {heard}   {'-> wakes him' if ok else '-> would NOT wake him'}")
        print(f"\nHe heard his name {hits} of {len(WAKE_PHRASES)} times."
              + ("" if hits >= len(WAKE_PHRASES) - 1 else ' If "AETHER" misses, "Nova" is the easier one to hear.'))
    print("\nDone. Restart AETHER (or aether-voice) to use it. Fix any misheard words by clicking them in the HUD;"
          " AETHER keeps learning from those.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
