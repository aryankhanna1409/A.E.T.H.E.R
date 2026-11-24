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
    return Aether(cfg, llm, embedder, confirm), problem
