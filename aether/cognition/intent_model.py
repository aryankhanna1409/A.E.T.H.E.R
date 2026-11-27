"""A small learned router that lets AETHER skip the model's "what do you want?" pass on most messages.

Before answering, AETHER asks the language model to read each non-trivial message into an IntentIR (what it needs:
memory, documents, web, tools ...). On a 4 GB GPU that is a whole extra model call, seconds per turn. This module
learns to make that call itself:

* Every time the model does the reading, the message and the model's answer (its ``needs``) are kept as a training
  example, so the classifier is distilled from the model on your own messages. A small built-in seed set means it
  has a sensible start on day one.
* Features are the sentence embedding AETHER already computes for memory search (bge-small, on the CPU), or hashed
  words and word pairs when no embedding model is loaded.
* k-nearest neighbours over those vectors (numpy, milliseconds), re-indexed when enough new examples arrive and
  during the night pass. Its leave-one-out agreement with the model is measured; below 85% it stays off.
* It is trusted only when it is sure (every need's probability far from 0.5), when its held-out accuracy is good,
  and only for readings that need no tool arguments, so actions and personal facts still get the model's full
  reading. Everything else falls through to the model exactly as before.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path

from ..core.logging import log
from ..retrieval.lexical import tokens

LABELS = ["direct", "memory", "documents", "web", "symbolic", "tools", "optimize"]
SAFE = {"direct", "memory", "documents", "web"}    # readings it may decide alone (no tool arguments needed)
SEED_FILE = Path(__file__).with_name("intent_seed.json")
HASH_DIM = 2048


def hashed(text: str) -> list[float]:
    v = [0.0] * HASH_DIM
    ts = tokens(text)
    feats = ts + [a + "_" + b for a, b in zip(ts, ts[1:])] + ["len:" + str(min(len(ts) // 4, 6)),
                                                              "q:" + str(text.strip().endswith("?"))]
    for f in feats:
        h = int(hashlib.md5(f.encode()).hexdigest()[:8], 16)
        v[h % HASH_DIM] += 1.0 if h & 1 << 31 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class IntentModel:
    def __init__(self, db: sqlite3.Connection, lock: threading.Lock, embedder=None,
                 min_examples: int = 60, min_accuracy: float = 0.85, margin: float = 0.3, cipher=None):
        from ..core.crypto import NullCipher
        self.db, self.lock, self.embedder = db, lock, embedder
        self.cipher = cipher or NullCipher()   # your messages are encrypted here like everywhere else
        self.min_examples, self.min_accuracy, self.margin = min_examples, min_accuracy, margin
        self.W = None
        self._index = None
        self.b = None
        self.coverage = 0.0
        self.last_neighbour = ""
        self.accuracy = 0.0
        self.trained_on = 0
        self.stats = {"skipped": 0, "deferred": 0}
        self._training = threading.Lock()   # one training pass at a time; the index is swapped in whole
        with lock:
            db.execute("CREATE TABLE IF NOT EXISTS intent_examples (id INTEGER PRIMARY KEY, text TEXT, needs TEXT, "
                       "source TEXT, created REAL)")
            db.commit()
        self._seed()

    # ---------------------------------------------------------------- data
    def _seed(self) -> None:
        with self.lock:
            if self.db.execute("SELECT COUNT(*) FROM intent_examples WHERE source='seed'").fetchone()[0]:
                return
            try:
                seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            self.db.executemany("INSERT INTO intent_examples(text, needs, source, created) VALUES (?,?,?,?)",
                                [(self.cipher.encrypt(t), json.dumps(n), "seed", time.time()) for t, n in seed])
            self.db.commit()

    def record(self, text: str, needs: list[str]) -> None:
        """Keep the model's reading of a message as a training example (text is stored only in AETHER's own DB)."""
        needs = sorted({n for n in needs if n in LABELS}) or ["direct"]
        with self.lock:
            self.db.execute("INSERT INTO intent_examples(text, needs, source, created) VALUES (?,?,?,?)",
                            (self.cipher.encrypt(text[:500]), json.dumps(needs), "model", time.time()))
            self.db.commit()
        n = self.count()
        if (self.W is None and n >= self.min_examples or n - self.trained_on >= 50) and not self._training.locked():
            threading.Thread(target=self.safe_train, daemon=True).start()

    def count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM intent_examples").fetchone()[0]

    def _features(self, texts: list[str]):
        import numpy as np
        if self.embedder is not None:
            try:
                return np.asarray(self.embedder.embed(texts), dtype="float32")
            except Exception as e:
                log.info("intent model: embeddings failed (%s), using hashed words", e)
        return np.asarray([hashed(t) for t in texts], dtype="float32")

    # ---------------------------------------------------------------- training
    def safe_train(self) -> dict:
        try:
            return self.train()
        except Exception as e:  # background re-indexing must never take anything down
            log.warning("intent model training failed: %s", e)
            return {"trained": False, "error": str(e)}

    def train(self, k: int = 5) -> dict:
        with self._training:
            return self._train(k)

    def _train(self, k: int = 5) -> dict:
        """Index the examples. The model is k-nearest-neighbours over sentence embeddings: with a few hundred
        examples it beats a trained classifier, it explains itself ("this reads like: what did I tell you about my
        sister"), and its leave-one-out accuracy is an honest estimate of how often it agrees with the model."""
        try:
            import numpy as np
        except ImportError:  # pragma: no cover
            return {"trained": False}
        with self.lock:
            rows = self.db.execute("SELECT text, needs FROM intent_examples ORDER BY id DESC LIMIT 5000").fetchall()
        if len(rows) < self.min_examples:
            return {"trained": False, "examples": len(rows)}
        texts = [self.cipher.decrypt(r[0]) for r in rows]
        X = self._features(texts)
        X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
        Y = np.asarray([[1.0 if lab in json.loads(r[1]) else 0.0 for lab in LABELS] for r in rows], dtype="float32")
        sims = X @ X.T
        np.fill_diagonal(sims, -1)
        ok = n = 0
        for i in range(len(X)):
            pred = self._vote(sims[i], Y, k)
            if pred is not None:
                n += 1
                ok += set(pred[0]) == {lab for lab, y in zip(LABELS, Y[i]) if y}
        # swap the whole index in at once, so a message read meanwhile never sees half of an old and new model
        self._index = (X, Y, texts, k)
        self.W, self.b, self.texts, self.k = X, Y, texts, k
        self.accuracy = ok / n if n else 0.0
        self.coverage = n / len(X)
        self.trained_on = len(rows)
        log.info("intent model: %d examples, decides %.0f%% alone, agrees with the model %.0f%% of those",
                 len(rows), 100 * self.coverage, 100 * self.accuracy)
        return {"trained": True, "examples": len(rows), "accuracy": round(self.accuracy, 3),
                "coverage": round(self.coverage, 3)}

    def _vote(self, sims, Y=None, k: int | None = None):
        import numpy as np
        Y = self.b if Y is None else Y
        top = np.argsort(-sims)[: k or self.k]
        best = float(sims[top[0]])
        if best < self.threshold:
            return None
        w = np.clip(sims[top], 0, None)
        p = (w[:, None] * Y[top]).sum(0) / (w.sum() + 1e-9)
        sure = float(np.min(np.abs(p - 0.5)))
        if sure < self.margin:
            return None
        return [lab for lab, v in zip(LABELS, p) if v > 0.5] or ["direct"], 0.5 + sure, int(top[0])

    # ---------------------------------------------------------------- use
    @property
    def threshold(self) -> float:
        return 0.72 if self.embedder is not None else 0.55

    def predict(self, text: str) -> tuple[list[str], float] | None:
        """(needs, confidence) when the neighbours agree and are close, and the reading needs no tool arguments."""
        if self._index is None or self.accuracy < self.min_accuracy:
            return None
        import numpy as np
        W, Y, texts, k = self._index
        q = self._features([text])[0]
        q /= np.linalg.norm(q) + 1e-9
        v = self._vote(W @ q, Y, k)
        if v is None or not set(v[0]) <= SAFE or ACTION_OR_FACT.search(text):
            self.stats["deferred"] += 1
            return None
        self.stats["skipped"] += 1
        self.last_neighbour = texts[v[2]]
        return v[0], v[1]

    def info(self) -> dict:
        return {"examples": self.count(), "trained_on": self.trained_on, "accuracy": round(self.accuracy, 3),
                "coverage": round(self.coverage, 3),
                "active": self.W is not None and self.accuracy >= self.min_accuracy, **self.stats}


ACTION_OR_FACT = re.compile(r"\b(?:my\s+\w+\s+is|i\s+(?:live|work|moved|am\s+(?:a|an|now))|remind|schedule|open|create|"
                            r"delete|send|save|move|rename|set\s+(?:a|an|the)|book|install|run)\b", re.I)
