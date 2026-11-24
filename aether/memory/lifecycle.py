"""Memory lifecycle: summaries of long topics, decay of unconfirmed
inferences and retention limits. Run on startup and once a day."""
from __future__ import annotations

import time

from ..llm.base import LLMProvider, Message
from .store import MemoryStore

DAY = 86400


def decay(store: MemoryStore, keep_days: int = 365, inferred_daily_factor: float = 0.98,
          floor: float = 0.15) -> dict[str, int]:
    """Inferred observations fade unless they are accessed or confirmed; very old
    low-confidence episodic memories are dropped. User-stated facts never decay."""
    now = time.time()
    faded = removed = 0
    rows = store.db.execute("SELECT id, layer, source, confidence, updated, last_access FROM memories WHERE deleted=0").fetchall()
    with store.lock:
        for r in rows:
            last = max(r["updated"] or 0, r["last_access"] or 0)
            days = (now - last) / DAY
            if r["source"] == "inferred" and days >= 1:
                conf = r["confidence"] * inferred_daily_factor ** days
                if conf < floor:
                    store.db.execute("DELETE FROM memories WHERE id=?", (r["id"],))
                    removed += 1
                else:
                    store.db.execute("UPDATE memories SET confidence=?, updated=? WHERE id=?", (conf, now, r["id"]))
                    faded += 1
            elif r["layer"] == "episodic" and days > keep_days and r["confidence"] < 0.8:
                store.db.execute("DELETE FROM memories WHERE id=?", (r["id"],))
                removed += 1
        store.db.commit()
    store.index.invalidate()
    return {"faded": faded, "removed": removed}


def summarize_turns(llm: LLMProvider, topic_label: str, turns: list[tuple[str, str]]) -> str:
    transcript = "\n".join(f"{r}: {t[:400]}" for r, t in turns)
    return llm.chat([
        Message("system", "Summarize this conversation in 2-4 sentences for long-term memory: decisions, "
                          "facts the user gave, open questions. No pleasantries."),
        Message("user", f"Topic: {topic_label}\n\n{transcript}")], max_tokens=220, temperature=0.2).strip()
