"""Find the fastest gpu_layers setting for your GPU, on your machine.

Loads the model with several gpu_layers values, generates a short reply with
each, and reports tokens per second. Settings that run out of VRAM are
reported as failed. Close games and browsers with many tabs first.

Usage:  python scripts/tune_gpu.py [path/to/model.gguf] [--layers 14,18,20,22,24,28]
"""
import argparse
import gc
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aether.core.config import load_config  # noqa: E402


def trial(model: str, layers: int, ctx: int, threads: int, flash: bool) -> float | None:
    from llama_cpp import Llama
    try:
        llm = Llama(model_path=model, n_ctx=ctx, n_gpu_layers=layers, n_threads=threads,
                    flash_attn=flash, n_batch=256, verbose=False)
        start = time.perf_counter()
        out = llm.create_chat_completion(
            messages=[{"role": "user", "content": "Explain in about 120 words why the sky is blue."}],
            max_tokens=160, temperature=0)
        elapsed = time.perf_counter() - start
        tokens = out["usage"]["completion_tokens"]
        del llm
        gc.collect()
        return tokens / elapsed
    except Exception as e:  # CUDA out of memory and friends
        print(f"  gpu_layers={layers}: failed ({str(e)[:80]})")
        gc.collect()
        return None


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("model", nargs="?", default=cfg.llm.model_path)
    ap.add_argument("--layers", default="0,14,18,20,22,24,28")
    args = ap.parse_args()
    if not args.model or not Path(args.model).exists():
        sys.exit("Pass a .gguf path or set [llm].model_path in aether.toml")
    results = {}
    for n in (int(x) for x in args.layers.split(",")):
        speed = trial(args.model, n, cfg.llm.context_tokens, cfg.llm.threads, cfg.llm.flash_attn)
        if speed:
            results[n] = speed
            print(f"  gpu_layers={n:>3}: {speed:5.1f} tokens/s")
    if not results:
        sys.exit("No setting worked. Check that the CUDA build of llama-cpp-python is installed.")
    best = max(results, key=results.get)
    print(f"\nFastest: gpu_layers = {best} ({results[best]:.1f} tokens/s). Put that in aether.toml.")
    print("Tip: use one or two layers fewer than the fastest if the UI or other apps also need VRAM.")


if __name__ == "__main__":
    main()
