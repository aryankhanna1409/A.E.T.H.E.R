"""Builds a fully wired AETHER instance from config."""
from __future__ import annotations

from typing import Callable

from .cognition.orchestrator import Aether
from .core.config import Config, load_config
from .core.logging import log
from .llm import LLMUnavailable, load_llm
from .llm.embeddings import load_embedder


def build(config_path: str | None = None, confirm: Callable[[str], bool] = lambda _: False,
          cfg: Config | None = None) -> tuple[Aether, str | None]:
    """Returns (aether, problem). `problem` explains why no LLM is loaded, if so."""
    cfg = cfg or load_config(config_path)
    problem = None
    try:
        llm = load_llm(cfg.llm)
    except LLMUnavailable as e:
        llm, problem = None, str(e)
        log.error("%s", e)
    embedder = load_embedder(cfg.embeddings)
    reranker = None
    if cfg.memory.reranker_model:
        from .retrieval.rerank import Reranker
        reranker = Reranker(cfg.memory.reranker_model) or None
        reranker = reranker if reranker.available else None
    aether = Aether(cfg, llm, embedder, confirm, reranker)
    if cfg.memory.watch_interval_s > 0 and cfg.security.allowed_read_roots:
        w = aether.watcher()
        for folder in cfg.security.allowed_read_roots:
            try:
                w.scan(folder)
            except FileNotFoundError:
                log.warning("Authorized folder %s does not exist yet.", folder)
        w.watch(cfg.security.allowed_read_roots, cfg.memory.watch_interval_s)
    try:
        from .personalization.observations import analyse
        analyse(aether.db, aether.observations, decrypt=aether.store.cipher.decrypt)
    except Exception as e:
        log.warning("observation analysis skipped: %s", e)
    if cfg.proactive.enabled:
        aether.proactive.tick()
        aether.proactive.start()
    return aether, problem
