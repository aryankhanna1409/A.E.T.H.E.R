"""Lexical retrieval: tokenizer, sparse TF-IDF vectors (for topic tracking)
and BM25 scoring (for keyword search in hybrid retrieval)."""
from __future__ import annotations

import math
import re
from collections import Counter

STOP = set("""a an the and or but if of to in on at for with is are was were be been it this that
these those i you he she we they me my your our their as by from not no do does did so than then
just about what which who how why when where can could would should will shall may might have has
had am please tell its it's im i'm""".split())

TOKEN = re.compile(r"[a-z0-9']+")


def tokens(text: str) -> list[str]:
    return [t for t in TOKEN.findall(text.lower()) if t not in STOP and len(t) > 1]


def vectorize(text: str, idf: dict[str, float] | None = None) -> dict[str, float]:
    counts = Counter(tokens(text))
    if not counts:
        return {}
    vec = {t: (1 + math.log(c)) * (idf.get(t, 1.0) if idf else 1.0) for t, c in counts.items()}
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {t: v / norm for t, v in vec.items()}


def sparse_cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.docs = [tokens(d) for d in docs]
        self.k1, self.b = k1, b
        self.avgdl = sum(map(len, self.docs)) / max(len(self.docs), 1)
        df = Counter(t for d in self.docs for t in set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self.tfs = [Counter(d) for d in self.docs]

    def scores(self, query: str) -> list[float]:
        q = tokens(query)
        out = []
        for tf, d in zip(self.tfs, self.docs):
            s = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(d) / (self.avgdl or 1)))
            out.append(s)
        return out
