"""Performance budget check: load time and full-pipeline turn latency with
your real model, compared with the budget in docs/SECURITY.md#performance-budget.

Usage:  .\\.venv\\Scripts\\python scripts\\bench.py [--config aether.toml]
"""
import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aether.app import build  # noqa: E402
from aether.core.config import load_config  # noqa: E402

BUDGET = {"load_s": 45, "first_word_s": 6, "smalltalk_s": 8, "question_s": 25, "rag_s": 30}
TURNS = {
    "smalltalk_s": ["hey!", "thanks, that helps", "good morning"],
    "question_s": ["Explain the difference between TCP and UDP briefly.", "What is a Horn clause?"],
    "rag_s": ["What do my notes say about the Helios drone battery?"],
}


def run(cfg) -> dict | None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg.home = Path(tmp)
        docs = Path(tmp) / "docs"
        docs.mkdir()
        (docs / "helios.md").write_text("The Helios solar drone battery lasts 47 minutes per charge.")
        cfg.security.allowed_read_roots = [str(docs)]
        cfg.memory.watch_interval_s = 0
        cfg.proactive.enabled = False
        start = time.perf_counter()
        a, problem = build(cfg=cfg)
        load = time.perf_counter() - start
        if problem:
            print("No model loaded:", problem)
            return None
        a.index_folder(str(docs))
        results = {"load_s": [load], "first_word_s": []}
        for kind, msgs in TURNS.items():
            for m in msgs:
                t, first = time.perf_counter(), []
                a.handle(m, on_event=lambda k, d: first.append(time.perf_counter() - t) if k == "token" and not first else None)
                results.setdefault(kind, []).append(time.perf_counter() - t)
                if first and kind != "smalltalk_s":
                    results["first_word_s"].append(first[0])
        a.llm = None  # free the model before a second run loads another copy
    return results


def report(results: dict) -> bool:
    ok = True
    print(f"{'measure':<14}{'median':>9}{'worst':>9}{'budget':>9}")
    for k, xs in results.items():
        if not xs:
            continue
        med, worst = statistics.median(xs), max(xs)
        good = med <= BUDGET[k]
        ok &= good
        print(f"{k:<14}{med:9.1f}{worst:9.1f}{BUDGET[k]:9.0f}  {'ok' if good else 'OVER'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--compare-draft", action="store_true",
                    help="also run with prompt-lookup speculative decoding and compare")
    args = ap.parse_args()
    cfg = load_config(args.config)
    results = run(cfg)
    if results is None:
        return 1
    ok = report(results)
    if args.compare_draft:
        import gc
        gc.collect()
        cfg2 = load_config(args.config)
        cfg2.llm.draft_lookup_tokens = cfg2.llm.draft_lookup_tokens or 10
        print(f"\nWith speculative decoding (draft_lookup_tokens = {cfg2.llm.draft_lookup_tokens}):")
        with_draft = run(cfg2)
        if with_draft:
            report(with_draft)
            gain = statistics.median(results["question_s"] + results["rag_s"]) / max(
                statistics.median(with_draft["question_s"] + with_draft["rag_s"]), 1e-6)
            print(f"\nAnswers were {gain:.2f}x as fast with it. "
                  + ("Set [llm].draft_lookup_tokens = 10 in aether.toml." if gain > 1.08 else "Leave it off (0)."))
    if not ok:
        print("\nOver budget: run scripts\\tune_gpu.py, lower [llm].context_tokens, or try the 3B model.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
