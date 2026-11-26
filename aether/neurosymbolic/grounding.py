"""Source check for answers built on your documents: every claim sentence must be backed by a retrieved passage.

A small model asked about your notes sometimes fills gaps from general knowledge, or gets a number slightly wrong.
After the draft is written, each sentence that makes a claim is compared with the passages it was given:

* the content words it shares with the best passage (how much of the sentence the passage covers),
* the sentence-embedding similarity to that passage, when the embedding model is loaded,
* and numbers, dates and amounts are strict: a number that appears in no passage makes the sentence unsupported.

Sentences that fail are listed under the answer ("Not found in your documents: ..."), so you know which part to
double-check. It needs no extra model call, so it costs milliseconds.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..retrieval.lexical import tokens

SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
NUM = re.compile(r"\b\d[\d,.:/-]*\b")
META = re.compile(r"^(?:i\s|i'm|i've|let me|here(?:'s| is| are)|sure|okay|according to|based on|in short|overall|"
                  r"you (?:can|could|might|may|should)|do you|would you|want me|if you)", re.I)


@dataclass
class Grounding:
    checked: int = 0
    supported: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return len(self.supported) / self.checked if self.checked else 1.0


def claims(answer: str) -> list[str]:
    text = re.sub(r"\[(?:doc|web|memory)\s*\d+\]", "", answer)
    text = re.sub(r"^[\s>*#-]+", "", text, flags=re.M)
    out = []
    for s in SENT.split(text.replace("\n", " ")):
        s = s.strip()
        if len(tokens(s)) < 4 or s.endswith("?") or META.match(s):
            continue
        out.append(s)
    return out


def _nums(s: str) -> set[str]:
    return {n.strip(".,").replace(",", "") for n in NUM.findall(s) if n.strip(".,")}


def check(answer: str, passages: list[str], embedder=None, cover: float = 0.6, sim: float = 0.78) -> Grounding:
    g = Grounding()
    sents = claims(answer)
    if not sents or not passages:
        return g
    ptoks = [set(tokens(p)) for p in passages]
    pnums = set().union(*(_nums(p) for p in passages))
    vecs = None
    if embedder is not None:
        try:
            import numpy as np
            m = np.asarray(embedder.embed(sents + passages), dtype="float32")
            m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
            vecs = (m[: len(sents)] @ m[len(sents):].T).max(axis=1)
        except Exception:
            vecs = None
    for i, s in enumerate(sents):
        g.checked += 1
        st = set(tokens(s))
        best = max((len(st & pt) / len(st) for pt in ptoks), default=0.0) if st else 0.0
        missing_num = bool(_nums(s) - pnums)
        ok = (best >= cover or (vecs is not None and float(vecs[i]) >= sim)) and not missing_num
        (g.supported if ok else g.unsupported).append(s)
    return g


def footnote(g: Grounding, limit: int = 3) -> str:
    if not g.unsupported:
        return ""
    items = "; ".join(f'"{s[:140]}"' for s in g.unsupported[:limit])
    more = f" and {len(g.unsupported) - limit} more" if len(g.unsupported) > limit else ""
    return f"\n\n_Not found in your documents, so double-check: {items}{more}._"
