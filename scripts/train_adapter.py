"""Can this PC fine-tune a LoRA adapter on what AETHER learned? Checks honestly, then says what to do.

    .\\.venv\\Scripts\\python scripts\\train_adapter.py

AETHER writes its training data every night (see aether/metacognition/learning.py):
    %LOCALAPPDATA%\\AETHER\\learning\\sft.jsonl   questions with the answers you liked or corrected
    %LOCALAPPDATA%\\AETHER\\learning\\dpo.jsonl   better vs worse answer pairs from your corrections

A LoRA for a 7B model needs about 6 GB of GPU memory even with 4-bit QLoRA (weights ~4.5 GB plus activations,
the adapter and optimiser state). A 4 GB RTX 3050 can't hold that, and on the CPU it would take days, so on that
laptop AETHER learns through lessons, facts and preferences instead, which works from the first correction.

With a bigger GPU (here or on another machine), train with any QLoRA tool on the JSONL files, for example
Hugging Face TRL (SFTTrainer, then DPOTrainer) on Qwen/Qwen2.5-7B-Instruct with r=16, then convert the adapter with
llama.cpp's convert_lora_to_gguf.py and set [llm].adapter_path in aether.toml to the .gguf file.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NEED_GB = {"7b": 6.0, "3b": 3.5, "1.5b": 2.5}


def plan(vram_gb: float | None, examples: int, pairs: int) -> list[str]:
    """What this machine can do, in plain sentences. Pure function, so it is tested."""
    lines = [f"Training data: {examples} examples and {pairs} preference pairs."]
    if examples < 50:
        lines.append("That's too little to train on yet (aim for 200 or more). Lessons already use every correction.")
    if vram_gb is None:
        lines.append("No CUDA GPU visible to PyTorch here, so no adapter training on this PC. On the CPU a 7B LoRA "
                     "would take days.")
    elif vram_gb < NEED_GB["7b"]:
        lines.append(f"This GPU has {vram_gb:.1f} GB. A 7B QLoRA needs about {NEED_GB['7b']:.0f} GB, so it does not fit. "
                     "AETHER keeps learning through lessons, facts and preferences, which needs no training.")
    else:
        lines.append(f"This GPU has {vram_gb:.1f} GB: enough for a 7B QLoRA with a short context (512 tokens, batch 1).")
    lines.append("To train elsewhere: copy the two .jsonl files, run a QLoRA (r=16) on Qwen/Qwen2.5-7B-Instruct, "
                 "convert it with llama.cpp's convert_lora_to_gguf.py and set [llm].adapter_path.")
    return lines


def main() -> int:
    from aether.core.config import load_config
    cfg = load_config(ROOT / "aether.toml" if (ROOT / "aether.toml").exists() else None)
    folder = Path(cfg.home) / "learning"
    count = lambda f: sum(1 for line in (folder / f).read_text(encoding="utf-8").splitlines() if line.strip()) \
        if (folder / f).exists() else 0  # noqa: E731
    vram = None
    try:
        import torch
        if torch.cuda.is_available():
            vram = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
    except Exception:
        pass
    print(f"Learning data folder: {folder}")
    for line in plan(vram, count("sft.jsonl"), count("dpo.jsonl")):
        print("- " + line)
    if (folder / "sft.jsonl").exists():
        first = (folder / "sft.jsonl").read_text(encoding="utf-8").splitlines()[:1]
        if first:
            print("\nFirst example:\n" + json.dumps(json.loads(first[0]), indent=2, ensure_ascii=False)[:800])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
