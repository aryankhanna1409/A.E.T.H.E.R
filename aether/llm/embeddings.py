"""Local embedding models. The first run downloads the model to the Hugging
Face cache; after that it works offline (set HF_HUB_OFFLINE=1 to enforce it).

If no embedding backend is installed, AETHER runs with lexical (BM25) search
only and says so in /status; it does not pretend to have semantic search."""
from __future__ import annotations

import math
from typing import Protocol

from ..core.config import EmbeddingConfig
from ..core.logging import log


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    def __init__(self, cfg: EmbeddingConfig):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(cfg.model, device=cfg.device)
        self.dim = self.model.get_sentence_embedding_dimension()
        self.name = f"st:{cfg.model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(texts, normalize_embeddings=True).tolist()


def load_embedder(cfg: EmbeddingConfig) -> Embedder | None:
    if cfg.backend == "lexical":
        return None
    try:
        return SentenceTransformerEmbedder(cfg)
    except Exception as e:  # ImportError or model load failure
        log.warning("Embeddings unavailable (%s); falling back to lexical search only.", e)
        return None


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
