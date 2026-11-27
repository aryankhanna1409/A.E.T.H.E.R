"""Fill AETHER from a seed pack: memories, knowledge graph, rules, preferences, patterns, goals and test cases.

    .\\.venv\\Scripts\\python scripts\\import_seed_pack.py my_seed_pack.json
    .\\.venv\\Scripts\\python scripts\\import_seed_pack.py my_seed_pack.json --yes --only-me

--only-me also retires facts that have nothing to do with you (demo worlds, test data), so the memory map and the
Knowledge view show only your world. Retired facts stay in the belief history.

Quit AETHER first (tray icon > Quit), then start it again afterwards. The format is described in
aether/memory/seedpack.py. Everything added is marked as seeded, and running it twice adds nothing new.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pack", help="seed pack .json")
    ap.add_argument("--config", help="path to aether.toml")
    ap.add_argument("--yes", action="store_true", help="don't ask before saving")
    ap.add_argument("--only-me", action="store_true", help="also retire facts not connected to you (demo/test data)")
    args = ap.parse_args(argv)

    from aether.cognition.orchestrator import Aether
    from aether.core.config import load_config
    from aether.llm.embeddings import load_embedder
    from aether.memory import seedpack

    pack = seedpack.load(args.pack)
    counts = seedpack.summary(pack)
    if not counts:
        print("That pack is empty.")
        return 1
    print(f"{pack.get('label') or Path(args.pack).name}:")
    for k, v in counts.items():
        print(f"  {v:>4}  {k.replace('_', ' ')}")
    if not args.yes and input("\nAdd these to AETHER? [Y/n] ").strip().lower() in {"n", "no", "q"}:
        print("Cancelled. Nothing was saved.")
        return 0
    cfg_path = args.config or (ROOT / "aether.toml" if (ROOT / "aether.toml").exists() else None)
    cfg = load_config(cfg_path)
    print("Loading AETHER's memory (no model needed)...")
    aether = Aether(cfg, None, load_embedder(cfg.embeddings))
    out = seedpack.apply(aether, pack, label=pack.get("label") or Path(args.pack).stem)
    added = [f"{v} {k.replace('_', ' ')}" for k, v in out.items() if v and k != "skipped"]
    print("\nAdded: " + ", ".join(added) + "." if added else "\nNothing new: AETHER already has everything in this pack.")
    if out["skipped"]:
        print(f"Skipped {out['skipped']} item(s) that were already there or looked unsafe.")
    if args.only_me:
        print(aether.atlas.act("prune", {})["message"])
    m = aether.atlas.build()
    s = m["stats"]
    print(f"Memory map: {s['nodes']} nodes in {s['communities']} communities across {len(s['areas'])} areas"
          + (f" ({s['hidden']} unrelated nodes hidden)" if s.get("hidden") else "") + ".")
    print("Start AETHER again (start.bat) and open Map, Memory, Today and Goals.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
