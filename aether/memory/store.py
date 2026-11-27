"""Multi-layer persistent memory in one SQLite file.

Layers: working (in RAM, see cognition.context), episodic, semantic, procedural,
preference, prospective, observation, document.
Every memory carries its provenance (`source`), a confidence, timestamps and
an optional embedding. Psychological inferences are stored only as
`observation` with source="inferred" and are never promoted to facts silently.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..llm.embeddings import Embedder, cosine  # noqa: F401
from ..retrieval.lexical import tokens
from .vector_index import VectorIndex, pack

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
    """SQLite-backed memory with a keyword index (FTS5 over keyed term hashes) and a partitioned vector index.

    A search no longer reads, decrypts and re-tokenises every row: candidates come from the keyword index and the
    vector index (a few hundred at most), and only those are decrypted and scored. Scores are the same BM25 + cosine
    blend as before, computed with corpus statistics from the index, so small stores rank exactly as they used to.
    """
    CANDIDATES = 400

    def __init__(self, db_path: Path, embedder: Embedder | None = None, cipher=None):
        from ..core.crypto import NullCipher
        self.cipher = cipher or NullCipher()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.embedder = embedder
        self.index = VectorIndex(self.db)
        self._writes = 0
        self._stats: dict = {}
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
            CREATE TABLE IF NOT EXISTS store_meta (k TEXT PRIMARY KEY, v TEXT);
            """)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(memories)")}
            for col, typ in (("vec", "BLOB"), ("nhash", "TEXT"), ("ntok", "INTEGER")):
                if col not in cols:
                    self.db.execute(f"ALTER TABLE memories ADD COLUMN {col} {typ}")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_mem_nhash ON memories(layer, nhash)")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_mem_created ON memories(created)")
            try:
                self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(terms, tokenize='ascii')")
                self.db.execute("CREATE TRIGGER IF NOT EXISTS mem_fts_del AFTER DELETE ON memories "
                                "BEGIN DELETE FROM mem_fts WHERE rowid=old.id; END")
                self.fts = True
            except sqlite3.OperationalError:  # pragma: no cover - SQLite built without FTS5
                self.fts = False
            self.db.commit()
        self._migrate()

    # ------------------------------------------------------------ term hashing
    def _term(self, t: str) -> str:
        return hmac.new(self.cipher.token_key, t.encode("utf-8"), hashlib.sha1).hexdigest()[:16]

    def _norm_hash(self, text: str) -> str:
        return hmac.new(self.cipher.token_key, " ".join(text.lower().split()).encode("utf-8"),
                        hashlib.sha256).hexdigest()[:32]

    def _index_terms(self, mid: int, text: str) -> int:
        toks = tokens(text)
        if self.fts:
            self.db.execute("INSERT OR REPLACE INTO mem_fts(rowid, terms) VALUES (?,?)",
                            (mid, " ".join(self._term(t) for t in toks)))
        return len(toks)

    def _migrate(self) -> None:
        """One-time upgrades of an older database: JSON embeddings become float32 blobs, every row gets a duplicate
        hash and keyword-index terms. Re-run when the term key changes (encryption switched on)."""
        fp = hashlib.sha1(self.cipher.token_key).hexdigest()[:12]
        with self.lock:
            row = self.db.execute("SELECT v FROM store_meta WHERE k='term_key'").fetchone()
            rebuild = row is None or row[0] != fp
            if rebuild and self.fts:
                self.db.execute("DELETE FROM mem_fts")
            cur = self.db.execute("SELECT id, text, embedding FROM memories WHERE " +
                                  ("1" if rebuild else "nhash IS NULL OR (embedding IS NOT NULL AND vec IS NULL)"))
            n = 0
            while True:
                rows = cur.fetchmany(5000)
                if not rows:
                    break
                for r in rows:
                    text = self.cipher.decrypt(r["text"])
                    vec = pack(json.loads(r["embedding"])) if r["embedding"] else None
                    ntok = self._index_terms(r["id"], text)
                    if vec is not None:
                        self.db.execute("UPDATE memories SET vec=?, embedding=NULL, nhash=?, ntok=? WHERE id=?",
                                        (vec, self._norm_hash(text), ntok, r["id"]))
                    else:
                        self.db.execute("UPDATE memories SET nhash=?, ntok=? WHERE id=?",
                                        (self._norm_hash(text), ntok, r["id"]))
                    n += 1
            self.db.execute("INSERT OR REPLACE INTO store_meta VALUES ('term_key', ?)", (fp,))
            self.db.commit()
        if n > 1000:
            from ..core.logging import log
            log.info("memory store: upgraded %d rows to the indexed format", n)

    def _touched(self) -> None:
        self._writes += 1
        self._stats.clear()

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
                "INSERT INTO memories(layer,text,source,confidence,created,updated,meta,vec,nhash) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (layer, self.cipher.encrypt(text), source, confidence, now, now, json.dumps(meta or {}),
                 pack(emb) if emb else None, self._norm_hash(text)))
            mid = cur.lastrowid
            ntok = self._index_terms(mid, text)
            self.db.execute("UPDATE memories SET ntok=? WHERE id=?", (ntok, mid))
            self.db.commit()
            self._touched()
        self.index.add(mid, layer, emb)
        return mid

    def _find_duplicate(self, text: str, layer: str, emb, threshold: float) -> int | None:
        r = self.db.execute("SELECT id FROM memories WHERE layer=? AND nhash=? AND deleted=0 LIMIT 1",
                            (layer, self._norm_hash(text))).fetchone()
        if r:
            return r[0]
        if emb and threshold <= 1.0:
            mid, sim = self.index.nearest(emb, layer)
            if mid is not None and sim >= threshold - 1e-3:
                return mid
        return None

    def add_document(self, text: str, source_path: str, chunk_words: int = 220, overlap: int = 40) -> int:
        """Chunk a document and store every chunk in one transaction, with one batched embedding call."""
        words = text.split()
        step = max(chunk_words - overlap, 1)
        chunks = [(i, " ".join(words[i:i + chunk_words])) for i in range(0, max(len(words), 1), step)]
        chunks = [(i, c) for i, c in chunks if c.strip()]
        if not chunks:
            return 0
        embs = self.embedder.embed([c for _, c in chunks]) if self.embedder else [None] * len(chunks)
        now = time.time()
        added = []
        with self.lock:
            for (off, chunk), emb in zip(chunks, embs):
                h = self._norm_hash(chunk)
                if self.db.execute("SELECT 1 FROM memories WHERE layer='document' AND nhash=? AND deleted=0 AND "
                                   "json_extract(meta,'$.path')=?", (h, source_path)).fetchone():
                    continue
                cur = self.db.execute(
                    "INSERT INTO memories(layer,text,source,confidence,created,updated,meta,vec,nhash) "
                    "VALUES ('document',?,'document',0.9,?,?,?,?,?)",
                    (self.cipher.encrypt(chunk), now, now, json.dumps({"path": source_path, "offset": off}),
                     pack(emb) if emb else None, h))
                mid = cur.lastrowid
                self.db.execute("UPDATE memories SET ntok=? WHERE id=?", (self._index_terms(mid, chunk), mid))
                added.append((mid, emb))
            self.db.commit()
            self._touched()
        for mid, emb in added:
            self.index.add(mid, "document", emb)
        return len(added)

    def delete_document(self, source_path: str) -> int:
        with self.lock:
            ids = [r[0] for r in self.db.execute(
                "SELECT id FROM memories WHERE layer='document' AND json_extract(meta, '$.path')=?", (source_path,))]
            self.db.executemany("DELETE FROM memories WHERE id=?", [(i,) for i in ids])
            self.db.commit()
            self._touched()
        self.index.remove(ids)
        return len(ids)

    def correct(self, memory_id: int, new_text: str) -> None:
        emb = self.embedder.embed([new_text])[0] if self.embedder else None
        with self.lock:
            self.db.execute("UPDATE memories SET text=?, vec=?, nhash=?, updated=?, source='user_approved', "
                            "confidence=1.0 WHERE id=?",
                            (self.cipher.encrypt(new_text), pack(emb) if emb else None, self._norm_hash(new_text),
                             time.time(), memory_id))
            self.db.execute("UPDATE memories SET ntok=? WHERE id=?", (self._index_terms(memory_id, new_text), memory_id))
            self.db.commit()
            self._touched()
        self.index.invalidate()

    def forget(self, memory_id: int, hard: bool = True) -> None:
        with self.lock:
            if hard:
                self.db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            else:
                self.db.execute("UPDATE memories SET deleted=1 WHERE id=?", (memory_id,))
            self.db.commit()
            self._touched()
        self.index.remove([memory_id])

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

    # ------------------------------------------------------------ keyword index
    def _layer_sql(self, layers, since=None, until=None) -> tuple[str, list]:
        q, args = "m.deleted=0", []
        if layers:
            q += f" AND m.layer IN ({','.join('?' * len(layers))})"
            args += list(layers)
        if since is not None:
            q += " AND m.created>=?"
            args.append(since)
        if until is not None:
            q += " AND m.created<?"
            args.append(until)
        return q, args

    def _corpus(self, layers, since, until) -> tuple[int, float]:
        key = ("corpus", layers, since, until)
        if key not in self._stats:
            where, args = self._layer_sql(layers, since, until)
            n, tot = self.db.execute(f"SELECT COUNT(*), COALESCE(SUM(ntok),0) FROM memories m WHERE {where}",
                                     args).fetchone()
            self._stats[key] = (n, tot / n if n else 0.0)
        return self._stats[key]

    def _df(self, term: str, layers, since, until) -> int:
        key = ("df", term, layers, since, until)
        if key not in self._stats:
            where, args = self._layer_sql(layers, since, until)
            self._stats[key] = self.db.execute(
                f"SELECT COUNT(*) FROM mem_fts f JOIN memories m ON m.id=f.rowid WHERE mem_fts MATCH ? AND {where}",
                [f'"{self._term(term)}"'] + args).fetchone()[0]
        return self._stats[key]

    def _keyword_candidates(self, qtoks: list[str], layers, since, until) -> list[int]:
        if not self.fts or not qtoks:
            return []
        where, args = self._layer_sql(layers, since, until)
        match = " OR ".join(f'"{self._term(t)}"' for t in dict.fromkeys(qtoks))
        return [r[0] for r in self.db.execute(
            f"SELECT f.rowid FROM mem_fts f JOIN memories m ON m.id=f.rowid WHERE mem_fts MATCH ? AND {where} "
            f"ORDER BY bm25(mem_fts) LIMIT {self.CANDIDATES}", [match] + args)]

    def _bm25(self, qtoks: list[str], dtoks: list[str], layers, since, until, k1=1.5, b=0.75) -> float:
        n, avgdl = self._corpus(layers, since, until)
        tf: dict[str, int] = {}
        for t in dtoks:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for t in qtoks:
            f = tf.get(t)
            if f:
                df = self._df(t, layers, since, until)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(dtoks) / (avgdl or 1)))
        return s

    # ------------------------------------------------------------------ reads
    def search(self, query: str, k: int = 6, layers: tuple[str, ...] | None = None,
               alpha: float = 0.6, min_score: float = 0.0, half_life_days: float = 180,
               since: float | None = None, until: float | None = None) -> list[Memory]:
        """Hybrid search: BM25 keyword score + embedding cosine, with a mild recency decay.
        ``since``/``until`` (unix times) limit it to memories made in that window ("what did we talk about on
        Tuesday")."""
        layers = tuple(layers) if layers else None
        qtoks = tokens(query)
        cand = set(self._keyword_candidates(qtoks, layers, since, until))
        qv = self.embedder.embed([query])[0] if self.embedder else None
        vec_scores = self.index.scores(qv, layers=layers, top=self.CANDIDATES) if qv is not None else {}
        cand |= set(vec_scores)
        if not cand:
            return []
        where, args = self._layer_sql(layers, since, until)
        rows = []
        ids = list(cand)
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            rows += self.db.execute(f"SELECT * FROM memories m WHERE {where} AND m.id IN ({','.join('?' * len(part))})",
                                    args + part).fetchall()
        if not rows:
            return []
        texts = [self.cipher.decrypt(r["text"]) for r in rows]
        bm = [self._bm25(qtoks, tokens(t), layers, since, until) for t in texts]
        top_bm = max(bm) or 1.0
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

    def between(self, since: float, until: float, layers: tuple[str, ...] | None = None,
                limit: int = 50) -> list[Memory]:
        """Memories made in a time window, oldest first (date-scoped recall without a query)."""
        where, args = self._layer_sql(layers, since, until)
        rows = self.db.execute(f"SELECT * FROM memories m WHERE {where} ORDER BY created LIMIT ?",
                               args + [limit]).fetchall()
        return [self._row(r) for r in rows]

    def turns_between(self, since: float, until: float, limit: int = 200) -> list[tuple[str, str, float]]:
        rows = self.db.execute("SELECT role, text, created FROM turns WHERE created>=? AND created<? ORDER BY id "
                               "LIMIT ?", (since, until, limit)).fetchall()
        return [(r["role"], self.cipher.decrypt(r["text"]), r["created"]) for r in rows]

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
        self._migrate()
        return n

    def _row(self, r: sqlite3.Row, text: str | None = None) -> Memory:
        return Memory(r["id"], r["layer"], text if text is not None else self.cipher.decrypt(r["text"]), r["source"],
                      r["confidence"], r["created"], json.loads(r["meta"] or "{}"))
