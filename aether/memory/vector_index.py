"""In-memory vector index over the embeddings stored in SQLite.

Built for a whole-drive index (hundreds of thousands of chunks) on a 16 GB laptop:

- vectors live in SQLite as compact float32 blobs (old JSON rows are migrated once by the store),
- the in-RAM copy is float16 (half the memory) and is split in two partitions, ``personal`` and ``document``, so
  a question about you never has to touch the document vectors and vice versa,
- adding a memory appends to the partition instead of rebuilding it, and deleting one masks it out, so the
  background indexer can keep writing while you chat without every search reloading the whole table,
- a query is scored in blocks, so the float32 working copy never exceeds a few MB.
"""
from __future__ import annotations

import sqlite3
from array import array

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy ships with sentence-transformers
    np = None

BLOCK = 65536


def pack(vec) -> bytes:
    return array("f", [float(x) for x in vec]).tobytes()


def unpack(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return a.tolist()


def partition(layer: str) -> str:
    return "document" if layer == "document" else "personal"


class _Part:
    def __init__(self):
        self.ids: list[int] = []
        self.layers: list[str] = []
        self.chunks: list = []          # numpy float16 blocks, or lists of python vectors without numpy
        self.pending: list[tuple[int, str, list[float]]] = []
        self.removed: set[int] = set()

    def flush(self) -> None:
        if not self.pending:
            return
        self.ids += [p[0] for p in self.pending]
        self.layers += [p[1] for p in self.pending]
        if np is not None:
            m = np.asarray([p[2] for p in self.pending], dtype=np.float32)
            norms = np.linalg.norm(m, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self.chunks.append((m / norms).astype(np.float16))
            if len(self.chunks) > 8:  # keep the block list short
                self.chunks = [np.concatenate(self.chunks)]
        else:
            self.chunks.append([p[2] for p in self.pending])
        self.pending = []

    def __len__(self) -> int:
        return len(self.ids) + len(self.pending)


class VectorIndex:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.dirty = True
        self.parts: dict[str, _Part] = {}

    # ------------------------------------------------------------ maintenance
    def invalidate(self) -> None:
        """Something changed rows behind the index's back: reload everything on the next search."""
        self.dirty = True

    def add(self, mid: int, layer: str, vec) -> None:
        if self.dirty or vec is None:
            return  # the next rebuild reads it from the table
        self.parts.setdefault(partition(layer), _Part()).pending.append((mid, layer, list(vec)))

    def remove(self, ids) -> None:
        if self.dirty:
            return
        for p in self.parts.values():
            p.removed.update(ids)

    def _rebuild(self) -> None:
        self.parts = {}
        cur = self.db.execute("SELECT id, layer, vec FROM memories WHERE deleted=0 AND vec IS NOT NULL")
        while True:
            rows = cur.fetchmany(20000)
            if not rows:
                break
            for r in rows:
                self.parts.setdefault(partition(r[1]), _Part()).pending.append((r[0], r[1], unpack(r[2])))
            for p in self.parts.values():
                p.flush()
        self.dirty = False

    def size(self) -> int:
        if self.dirty:
            self._rebuild()
        return sum(len(p) for p in self.parts.values())

    # ------------------------------------------------------------------ query
    def _part_scores(self, part: _Part, q, layers: set[str] | None) -> tuple[list[int], list[float]]:
        part.flush()
        ids, sims = [], []
        off = 0
        for chunk in part.chunks:
            n = len(chunk)
            if np is not None:
                s = (chunk.astype(np.float32) @ q).tolist() if n <= BLOCK else \
                    [x for i in range(0, n, BLOCK) for x in (chunk[i:i + BLOCK].astype(np.float32) @ q).tolist()]
            else:
                from ..llm.embeddings import cosine
                s = [cosine(q, v) for v in chunk]
            ids += part.ids[off:off + n]
            sims += s
            off += n
        if layers is not None or part.removed:
            keep = [i for i, mid in enumerate(ids) if mid not in part.removed
                    and (layers is None or part.layers[i] in layers)]
            ids, sims = [ids[i] for i in keep], [sims[i] for i in keep]
        return ids, sims

    def scores(self, query_vec, layers=None, top: int | None = None) -> dict[int, float]:
        """Cosine similarity of the query to stored memories (optionally only some layers), by memory id.
        With ``top``, only the best ``top`` matches are returned."""
        if self.dirty:
            self._rebuild()
        if not self.parts:
            return {}
        want = set(layers) if layers else None
        parts = [p for name, p in self.parts.items()
                 if want is None or any(partition(l) == name for l in want)]
        if np is not None:
            q = np.asarray(query_vec, dtype=np.float32)
            q = q / (np.linalg.norm(q) or 1.0)
        else:
            q = list(query_vec)
        ids: list[int] = []
        sims: list[float] = []
        for p in parts:
            i, s = self._part_scores(p, q, want)
            ids += i
            sims += s
        if top is not None and len(ids) > top:
            if np is not None:
                arr = np.asarray(sims)
                best = np.argpartition(-arr, top)[:top]
                return {ids[i]: float(arr[i]) for i in best}
            order = sorted(range(len(ids)), key=lambda i: -sims[i])[:top]
            return {ids[i]: sims[i] for i in order}
        return dict(zip(ids, sims))

    def nearest(self, query_vec, layer: str) -> tuple[int | None, float]:
        """The closest memory in one layer and its similarity (used to merge near-duplicates on write)."""
        s = self.scores(query_vec, layers=(layer,), top=1)
        if not s:
            return None, 0.0
        mid, sim = next(iter(s.items()))
        return mid, sim
