"""Import memories from another assistant (or a file you wrote) into AETHER, after you review them.

    .\\.venv\\Scripts\\python scripts\\import_memory.py docs\\memory\\seed_from_claude.md
    .\\.venv\\Scripts\\python scripts\\import_memory.py chatgpt_memories.txt
    .\\.venv\\Scripts\\python scripts\\import_memory.py conversations.json      (ChatGPT data export)

It lists what it found and asks before saving anything. Lines that look like instructions to an AI
("ignore previous instructions ...") are never imported. You can also drop the file on Memory > Import in the app.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def pick(answer: str, n: int) -> set[int] | None:
    """'y'/'' keeps the ticked ones, 'n' cancels, '3,5-7' drops those numbers, '+4' adds one back."""
    answer = answer.strip().lower()
    if answer in {"n", "no", "q"}:
        return None
    drop, add = set(), set()
    for part in answer.replace(" ", "").split(","):
        if not part or part in {"y", "yes"}:
            continue
        target = add if part.startswith("+") else drop
        part = part.lstrip("+-")
        a, _, b = part.partition("-")
        if a.isdigit():
            for i in range(int(a), int(b or a) + 1):
                if 1 <= i <= n:
                    target.add(i)
    return {-i for i in drop} | add


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="memory export: .txt, .md or .json (ChatGPT conversations.json works too)")
    ap.add_argument("--config", help="path to aether.toml")
    ap.add_argument("--yes", action="store_true", help="save every ticked item without asking")
    args = ap.parse_args(argv)

    from aether.cognition.orchestrator import Aether
    from aether.core.config import load_config
    from aether.llm.embeddings import load_embedder
    from aether.memory.importer import mark_existing, parse, save

    path = Path(args.file)
    text = path.read_text(encoding="utf-8", errors="replace")
    cfg_path = args.config or (ROOT / "aether.toml" if (ROOT / "aether.toml").exists() else None)
    cfg = load_config(cfg_path)
    aether = Aether(cfg, None, load_embedder(cfg.embeddings))
    items = mark_existing(parse(text, path.name), aether.store)
    if not items:
        print("Nothing to import: no facts or preferences found in that file.")
        return 1
    print(f"Found {len(items)} item(s) in {path.name}:\n")
    for i, c in enumerate(items, 1):
        mark = "x" if c.keep else " "
        note = " (already remembered)" if c.existing else f" (looks like an instruction: {', '.join(c.flags)}; never imported)" if c.flags else ""
        print(f"  [{mark}] {i:>3}. {c.kind:<10} {c.text}{note}")
    if args.yes:
        chosen = [c for c in items if c.keep]
    else:
        print("\nSave the ticked ones? Enter = yes, n = cancel, or numbers to change, e.g. 3,5-7 to drop, +4 to add.")
        sel = pick(input("> "), len(items))
        if sel is None:
            print("Cancelled. Nothing was saved.")
            return 0
        chosen = [c for i, c in enumerate(items, 1) if (c.keep and -i not in sel) or i in sel]
    result = save(aether, chosen, label=path.name)
    print(f"\nSaved {result['saved']} memory(ies)" + (f", skipped {result['skipped']}" if result["skipped"] else "")
          + ". See them in the app under Memory, or ask AETHER \"what do you know about me?\".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
