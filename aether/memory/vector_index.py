"""In-memory vector index over the embeddings stored in SQLite.

Parsed vectors are cached (and kept as a numpy matrix when numpy is present)
so a search is one matrix product instead of re-parsing JSON for every row.
The cache is rebuilt lazily after writes.
"""
from __future__ import annotations

import json
import sqlite3

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy ships with sentence-transformers
    np = None


class VectorIndex:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.dirty = True
        self.ids: list[int] = []
        self.layers: list[str] = []
        self.matrix = None
        self.vectors: list[list[float]] = []

    def invalidate(self) -> None:
        self.dirty = True

    def _rebuild(self) -> None:
        rows = self.db.execute("SELECT id, layer, embedding FROM memories WHERE deleted=0 AND embedding IS NOT NULL").fetchall()
        self.ids = [r[0] for r in rows]
        self.layers = [r[1] for r in rows]
        self.vectors = [json.loads(r[2]) for r in rows]
        if np is not None and self.vectors:
            m = np.asarray(self.vectors, dtype=np.float32)
            norms = np.linalg.norm(m, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self.matrix = m / norms
        else:
            self.matrix = None
        self.dirty = False

    def scores(self, query_vec: list[float]) -> dict[int, float]:
        """Cosine similarity of the query to every stored memory, by memory id."""
        if self.dirty:
            self._rebuild()
        if not self.ids:
            return {}
        if self.matrix is not None:
            q = np.asarray(query_vec, dtype=np.float32)
            q = q / (np.linalg.norm(q) or 1.0)
            sims = self.matrix @ q
            return dict(zip(self.ids, sims.tolist()))
        from ..llm.embeddings import cosine
        return {i: cosine(query_vec, v) for i, v in zip(self.ids, self.vectors)}
