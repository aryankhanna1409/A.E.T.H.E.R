"""One-time download of a GGUF model from Hugging Face into ./models.
This is the only time AETHER needs the internet for its brain; afterwards
the model runs fully offline in-process.

Usage:  python scripts/get_model.py            (7B, recommended for 8 GB+ VRAM / 16 GB RAM)
        python scripts/get_model.py small      (3B, for laptops)
        python scripts/get_model.py large      (14B, for 12 GB+ VRAM)
"""
import sys
from pathlib import Path

MODELS = {
    "small": ("Qwen/Qwen2.5-3B-Instruct-GGUF", "qwen2.5-3b-instruct-q4_k_m.gguf"),
    "default": ("Qwen/Qwen2.5-7B-Instruct-GGUF", "qwen2.5-7b-instruct-q4_k_m*.gguf"),
    "large": ("Qwen/Qwen2.5-14B-Instruct-GGUF", "qwen2.5-14b-instruct-q4_k_m*.gguf"),
}

if __name__ == "__main__":
    from huggingface_hub import snapshot_download  # pip install huggingface_hub
    repo, pattern = MODELS[sys.argv[1] if len(sys.argv) > 1 else "default"]
    out = Path("models")
    snapshot_download(repo, allow_patterns=[pattern], local_dir=out)
    files = sorted(out.glob(pattern.replace("*", "*")))
    print("Downloaded:", *files, sep="\n  ")
    print("\nSet [llm].model_path in aether.toml to the first file above "
          "(llama.cpp loads split files automatically from part 1).")
