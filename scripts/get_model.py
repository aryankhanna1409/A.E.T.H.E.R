"""One-time downloads from Hugging Face into ./models. This is the only time
AETHER needs the internet for its models; afterwards everything runs offline.

Usage:  python scripts/get_model.py            (7B, ~4.7 GB: the default; on a 4 GB RTX 3050
                                                it runs split between GPU and CPU)
        python scripts/get_model.py small      (3B, ~2 GB: fits fully in 4 GB VRAM, faster, weaker)
        python scripts/get_model.py large      (14B, ~9 GB: for 12 GB+ VRAM)
        python scripts/get_model.py voice      (Piper English voice for text-to-speech, ~60 MB)
        python scripts/get_model.py vision     (Qwen2.5-VL 3B + projector for images, ~3 GB, runs on CPU)
Speech-to-text (faster-whisper) and the wake word download themselves on first use.
"""
import sys
from pathlib import Path

MODELS = {
    "small": ("Qwen/Qwen2.5-3B-Instruct-GGUF", ["qwen2.5-3b-instruct-q4_k_m.gguf"], "[llm].model_path"),
    "default": ("Qwen/Qwen2.5-7B-Instruct-GGUF", ["qwen2.5-7b-instruct-q4_k_m*.gguf"], "[llm].model_path (part 1)"),
    "large": ("Qwen/Qwen2.5-14B-Instruct-GGUF", ["qwen2.5-14b-instruct-q4_k_m*.gguf"], "[llm].model_path (part 1)"),
    "voice": ("rhasspy/piper-voices", ["en/en_GB/alan/medium/*"], "[voice].tts_voice_path (the .onnx file)"),
    "vision": ("ggml-org/Qwen2.5-VL-3B-Instruct-GGUF", ["*Q4_K_M.gguf", "mmproj*f16.gguf"],
               "[vision].model_path and [vision].mmproj_path"),
}

if __name__ == "__main__":
    from huggingface_hub import snapshot_download  # pip install huggingface_hub
    which = sys.argv[1] if len(sys.argv) > 1 else "default"
    repo, patterns, setting = MODELS[which]
    out = Path("models") / ("voices" if which == "voice" else "vision" if which == "vision" else "")
    snapshot_download(repo, allow_patterns=patterns, local_dir=out)
    files = sorted(p for pat in patterns for p in out.glob(pat) if p.is_file() and p.suffix in {".gguf", ".onnx"})
    print("Downloaded:", *[f.resolve() for f in files], sep="\n  ")
    print(f"\nPut the path in aether.toml: {setting}.")
