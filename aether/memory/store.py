"""Multi-layer persistent memory in one SQLite file.

Layers: working (in RAM, see cognition.context), episodic, semantic, procedural,
preference, prospective, observation, document.
Every memory carries its provenance (`source`), a confidence, timestamps and
an optional embedding. Psychological inferences are stored only as
`observation` with source="inferred" and are never promoted to facts silently.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..llm.embeddings import Embedder, cosine  # noqa: F401
from ..retrieval.lexical import BM25
from .vector_index import VectorIndex

LAYERS = {"episodic", "semantic", "procedural", "preference", "prospective", "observation", "document"}
SOURCES = {"user_stated", "user_approved", "observed", "inferred", "document", "web", "system"}


@dataclass
class Memory:
    id: int
    layer: str
    text: str
    source: str
    confidence: float
    created: float
    meta: dict = field(default_factory=dict)
    score: float = 0.0


class MemoryStore:
    def __init__(self, db_path: Path, embedder: Embedder | None = None, cipher=None):
        from ..core.crypto import NullCipher
        self.cipher = cipher or NullCipher()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.embedder = embedder
        self.index = VectorIndex(self.db)
        with self.lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY, layer TEXT NOT NULL, text TEXT NOT NULL,
                source TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.8,
                created REAL, updated REAL, last_access REAL, access_count INTEGER DEFAULT 0,
                meta TEXT DEFAULT '{}', embedding TEXT, deleted INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS idx_mem_layer ON memories(layer, deleted);
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY, session TEXT, topic INTEGER, role TEXT, text TEXT, created REAL);
            """)
            self.db.commit()

    # ------------------------------------------------------------------ writes
    def add(self, text: str, layer: str, source: str, confidence: float = 0.8,
            meta: dict | None = None, dedupe_threshold: float = 0.92) -> int:
        if layer not in LAYERS:
            raise ValueError(f"unknown memory layer {layer}")
        if source not in SOURCES:
            raise ValueError(f"unknown memory source {source}")
        if layer == "semantic" and source == "inferred":
            raise ValueError("Inferences go to the 'observation' layer, never straight to facts.")
        emb = self.embedder.embed([text])[0] if self.embedder else None
        dup = self._find_duplicate(text, layer, emb, dedupe_threshold)
        now = time.time()
        with self.lock:
            if dup is not None:  # reinforce instead of duplicating
                self.db.execute("UPDATE memories SET updated=?, confidence=MIN(1.0, confidence+0.05) WHERE id=?",
                                (now, dup))
                self.db.commit()
                return dup
            cur = self.db.execute(
                "INSERT INTO memories(layer,text,source,confidence,created,updated,meta,embedding) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (layer, self.cipher.encrypt(text), source, confidence, now, now, json.dumps(meta or {}),
                 json.dumps(emb) if emb else None))
            self.db.commit()
            self.index.invalidate()
            return cur.lastrowid

    def _find_duplicate(self, text: str, layer: str, emb, threshold: float) -> int | None:
        rows = self.db.execute("SELECT id, text, embedding FROM memories WHERE layer=? AND deleted=0",
                               (layer,)).fetchall()
        norm = " ".join(text.lower().split())
        for r in rows:
            if " ".join(self.cipher.decrypt(r["text"]).lower().split()) == norm:
                return r["id"]
            if emb and r["embedding"] and cosine(emb, json.loads(r["embedding"])) >= threshold:
                return r["id"]
        return None

    def add_document(self, text: str, source_path: str, chunk_words: int = 220, overlap: int = 40) -> int:
        words = text.split()
        n = 0
        step = max(chunk_words - overlap, 1)
        for i in range(0, max(len(words), 1), step):
            chunk = " ".join(words[i:i + chunk_words])
            if chunk.strip():
                self.add(chunk, "document", "document", 0.9, {"path": source_path, "offset": i},
                         dedupe_threshold=1.01)
                n += 1
        return n

    def delete_document(self, source_path: str) -> int:
        with self.lock:
            cur = self.db.execute("DELETE FROM memories WHERE layer='document' AND json_extract(meta, '$.path')=?",
                                  (source_path,))
            self.db.commit()
        self.index.invalidate()
        return cur.rowcount

    def correct(self, memory_id: int, new_text: str) -> None:
        emb = self.embedder.embed([new_text])[0] if self.embedder else None
        with self.lock:
            self.db.execute("UPDATE memories SET text=?, embedding=?, updated=?, source='user_approved', "
                            "confidence=1.0 WHERE id=?",
                            (self.cipher.encrypt(new_text), json.dumps(emb) if emb else None, time.time(), memory_id))
            self.db.commit()
            self.index.invalidate()

    def forget(self, memory_id: int, hard: bool = True) -> None:
        with self.lock:
            if hard:
                self.db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            else:
                self.db.execute("UPDATE memories SET deleted=1 WHERE id=?", (memory_id,))
            self.db.commit()
            self.index.invalidate()

    def forget_matching(self, query: str) -> int:
        hits = self.search(query, k=20, min_score=0.5)
        for h in hits:
            self.forget(h.id)
        return len(hits)

    def log_turn(self, session: str, topic: int, role: str, text: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO turns(session,topic,role,text,created) VALUES (?,?,?,?,?)",
                            (session, topic, role, self.cipher.encrypt(text), time.time()))
            self.db.commit()

    # ------------------------------------------------------------------ reads
    def list(self, layer: str | None = None, limit: int = 100) -> list[Memory]:
        q = "SELECT * FROM memories WHERE deleted=0" + (" AND layer=?" if layer else "") + \
            " ORDER BY updated DESC LIMIT ?"
        args = (layer, limit) if layer else (limit,)
        return [self._row(r) for r in self.db.execute(q, args).fetchall()]

    def count(self, layer: str | None = None) -> int:
        if layer:
            return self.db.execute("SELECT COUNT(*) FROM memories WHERE deleted=0 AND layer=?", (layer,)).fetchone()[0]
        return self.db.execute("SELECT COUNT(*) FROM memories WHERE deleted=0").fetchone()[0]

    def search(self, query: str, k: int = 6, layers: tuple[str, ...] | None = None,
               alpha: float = 0.6, min_score: float = 0.0, half_life_days: float = 180) -> list[Memory]:
        """Hybrid search: BM25 keyword score + embedding cosine, with a mild recency decay."""
        q = "SELECT * FROM memories WHERE deleted=0"
        args: list = []
        if layers:
            q += f" AND layer IN ({','.join('?' * len(layers))})"
            args += list(layers)
        rows = self.db.execute(q, args).fetchall()
        if not rows:
            return []
        texts = [self.cipher.decrypt(r["text"]) for r in rows]
        bm = BM25(texts).scores(query)
        top_bm = max(bm) or 1.0
        qv = self.embedder.embed([query])[0] if self.embedder else None
        vec_scores = self.index.scores(qv) if qv is not None else {}
        now = time.time()
        out = []
        for r, b, text in zip(rows, bm, texts):
            lex = b / top_bm
            if qv is not None and r["id"] in vec_scores:
                vec = max(vec_scores[r["id"]], 0.0)
                score = alpha * vec + (1 - alpha) * lex
            else:
                score = lex * (0.6 if b < 1.0 else 1.0)  # weak keyword-only hits get discounted
            age_days = (now - r["updated"]) / 86400
            score *= 0.85 + 0.15 * 0.5 ** (age_days / half_life_days)
            score *= 0.7 + 0.3 * r["confidence"]
            if score > min_score:
                m = self._row(r, text)
                m.score = score
                out.append(m)
        out.sort(key=lambda m: m.score, reverse=True)
        top = out[:k]
        with self.lock:
            for m in top:
                self.db.execute("UPDATE memories SET last_access=?, access_count=access_count+1 WHERE id=?",
                                (now, m.id))
            self.db.commit()
        return top

    def recent_turns(self, session: str, limit: int = 12) -> list[tuple[str, str]]:
        rows = self.db.execute("SELECT role, text FROM turns WHERE session=? ORDER BY id DESC LIMIT ?",
                               (session, limit)).fetchall()
        return [(r["role"], self.cipher.decrypt(r["text"])) for r in reversed(rows)]

    def user_turn_texts(self, limit: int = 200) -> list[str]:
        rows = self.db.execute("SELECT text FROM turns WHERE role='user' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self.cipher.decrypt(r["text"]) for r in rows]

    def encrypt_existing(self) -> int:
        """Migrate plaintext rows written before encryption was switched on."""
        if not self.cipher.enabled:
            return 0
        n = 0
        with self.lock:
            for table in ("memories", "turns"):
                rows = self.db.execute(f"SELECT id, text FROM {table} WHERE text NOT LIKE 'enc1:%'").fetchall()
                for r in rows:
                    self.db.execute(f"UPDATE {table} SET text=? WHERE id=?", (self.cipher.encrypt(r["text"]), r["id"]))
                n += len(rows)
            self.db.commit()
        return n

    def _row(self, r: sqlite3.Row, text: str | None = None) -> Memory:
        return Memory(r["id"], r["layer"], text if text is not None else self.cipher.decrypt(r["text"]), r["source"],
                      r["confidence"], r["created"], json.loads(r["meta"] or "{}"))
