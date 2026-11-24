"""One-time download of a GGUF model from Hugging Face into ./models.
This is the only time AETHER needs the internet for its brain; afterwards
the model runs fully offline in-process.

Usage:  python scripts/get_model.py            (7B, ~4.7 GB: the default; on a 4 GB RTX 3050
                                                it runs split between GPU and CPU)
        python scripts/get_model.py small      (3B, ~2 GB: fits fully in 4 GB VRAM, faster, weaker)
        python scripts/get_model.py large      (14B, ~9 GB: for 12 GB+ VRAM)
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
