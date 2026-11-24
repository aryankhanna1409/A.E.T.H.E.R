"""Optional cross-encoder reranker (runs locally on CPU). Without it, the
hybrid score order is kept."""
from __future__ import annotations

from ..core.logging import log


class Reranker:
    def __init__(self, model: str = "BAAI/bge-reranker-base", device: str = "cpu"):
        self.model = None
        try:
            from sentence_transformers import CrossEncoder
            self.model = CrossEncoder(model, device=device)
        except Exception as e:
            log.info("Reranker not available (%s); using hybrid scores only.", e)

    @property
    def available(self) -> bool:
        return self.model is not None

    def rerank(self, query: str, items: list, text_of=lambda x: x.text, top_k: int = 6) -> list:
        if not self.model or len(items) <= 1:
            return items[:top_k]
        scores = self.model.predict([(query, text_of(i)) for i in items])
        ranked = sorted(zip(items, scores), key=lambda x: float(x[1]), reverse=True)
        return [i for i, _ in ranked[:top_k]]
