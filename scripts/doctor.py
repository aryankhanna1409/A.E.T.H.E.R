"""Check that AETHER is set up correctly on this PC, and say exactly what to fix.

    .\\.venv\\Scripts\\python scripts\\doctor.py

Nothing is changed; it only looks.
"""
from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OK, WARN, BAD = "  OK  ", " WARN ", " FAIL "
problems = 0


def line(mark: str, what: str, fix: str = "") -> None:
    global problems
    problems += mark == BAD
    print(f"[{mark}] {what}" + (f"\n         -> {fix}" if fix else ""))


def has(mod: str) -> bool:
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


def main() -> int:
    print("AETHER doctor\n")
    v = sys.version_info
    if (v.major, v.minor) in {(3, 11), (3, 12)}:
        line(OK, f"Python {v.major}.{v.minor}")
    else:
        line(BAD, f"This Python is {v.major}.{v.minor}; AETHER needs 3.11 (or 3.12)",
             "Keep your current Python and install 3.11 next to it (python.org, keep 'py launcher' ticked), then re-run "
             "scripts\\setup_windows.ps1: it rebuilds .venv with 3.11. Run this check with .\\.venv\\Scripts\\python")
    if ".venv" not in sys.prefix:
        line(WARN, "Not running inside .venv", "Use .\\.venv\\Scripts\\python scripts\\doctor.py")

    for mod, why in [("fastapi", "web app"), ("pydantic", "core"), ("cryptography", "encryption")]:
        line(OK if has(mod) else BAD, f"{mod} ({why})", "" if has(mod) else "Re-run scripts\\setup_windows.ps1")

    if has("llama_cpp"):
        import llama_cpp
        gpu = False
        try:
            gpu = bool(llama_cpp.llama_supports_gpu_offload())
        except Exception:
            pass
        line(OK if gpu else WARN, f"llama-cpp-python {llama_cpp.__version__} ({'GPU build' if gpu else 'CPU only'})",
             "" if gpu else "For the RTX 3050: re-run setup_windows.ps1 with -Cuda, and update the NVIDIA driver (550+)")
    else:
        line(BAD, "llama-cpp-python is missing", "Re-run scripts\\setup_windows.ps1 -Cuda")
    if shutil.which("nvidia-smi"):
        line(OK, "NVIDIA driver found (nvidia-smi)")
    else:
        line(WARN, "nvidia-smi not found", "Install or update the NVIDIA driver from the NVIDIA app if you want GPU speed")

    try:
        from aether.core.config import load_config
        cfg = load_config(ROOT / "aether.toml" if (ROOT / "aether.toml").exists() else None)
        line(OK, "aether.toml loads")
    except Exception as e:
        line(BAD, f"aether.toml has a problem: {e}", "Copy aether.example.toml to aether.toml and edit it again")
        return 1

    mp = Path(cfg.llm.model_path)
    if cfg.llm.model_path and mp.exists():
        line(OK, f"model: {mp.name} ({mp.stat().st_size / 2 ** 30:.1f} GB)")
    else:
        found = sorted((ROOT / "models").glob("*.gguf"))
        hint = f"Set [llm] model_path = \"{found[0].as_posix()}\"" if found else \
            "Run .\\.venv\\Scripts\\python scripts\\get_model.py (one time, about 4.7 GB)"
        line(BAD, f"model file not found: {cfg.llm.model_path or '(empty)'}", hint)

    for label, paths in [("readable folders", cfg.security.allowed_read_roots), ("writable folders", cfg.security.allowed_write_roots)]:
        missing = [p for p in paths if not Path(p).exists()]
        line(WARN if missing else OK, f"{label}: {len(paths) - len(missing)} of {len(paths)} exist",
             f"Create or fix these in aether.toml: {', '.join(missing)}" if missing else "")

    voice = [m for m in ("faster_whisper", "piper", "openwakeword", "sounddevice") if not has(m)]
    line(OK if not voice else WARN, "voice packages" + (f" missing: {', '.join(voice)}" if voice else ""),
         'pip install -e ".[voice]"' if voice else "")
    if cfg.voice.tts_voice_path and not Path(cfg.voice.tts_voice_path).exists():
        found = sorted((ROOT / "models" / "voices").rglob("*.onnx"))
        line(WARN, "speaking voice file not found",
             f"Set [voice] tts_voice_path = \"{found[0].as_posix()}\"" if found else
             "Run .\\.venv\\Scripts\\python scripts\\get_model.py voice")
    if has("sounddevice"):
        try:
            import sounddevice as sd
            sd.query_devices(kind="input")
            line(OK, "microphone found")
        except Exception:
            line(WARN, "no microphone found", "Plug one in or check Windows privacy settings for microphone access")

    tray = [m for m in ("pystray", "PIL", "pynput") if not has(m)]
    line(OK if not tray else WARN, "tray and hotkey" + (f" missing: {', '.join(tray)}" if tray else ""),
         'pip install -e ".[tray]"' if tray else "")
    line(OK if has("psutil") else WARN, "battery and system status (psutil)", "" if has("psutil") else "pip install psutil")
    line(OK if cfg.online.location else WARN, f"weather location: {cfg.online.location or '(not set)'}",
         "" if cfg.online.location else 'Set [online] location = "Vellore" (or your city) in aether.toml')
    print(f"\n{'All good. Start it with start.bat' if not problems else f'{problems} thing(s) to fix above.'}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
