"""In-process GGUF models through llama-cpp-python (no Ollama, no server).

Recommended models (download the .gguf once with scripts/get_model.py):
- 4 GB VRAM + 16 GB RAM (RTX 3050 Laptop): Qwen2.5-7B Q4_K_M, ~20 layers on GPU,
  or Qwen2.5-3B Q4_K_M fully on GPU when speed matters more than quality
- 8 GB+ VRAM:  Qwen2.5-7B Q4_K_M fully on GPU
- 12 GB+ VRAM: Qwen2.5-14B Q4_K_M
For NVIDIA GPUs install the CUDA wheel (see README).
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterator

from ..core.config import LLMConfig
from .base import LLMUnavailable, Message


class LlamaCppProvider:
    name = "llama_cpp"

    def __init__(self, cfg: LLMConfig):
        try:
            from llama_cpp import Llama
        except ImportError as e:  # pragma: no cover - depends on install
            raise LLMUnavailable("llama-cpp-python is not installed. Run: pip install llama-cpp-python") from e
        path = Path(cfg.model_path).expanduser()
        if not cfg.model_path or not path.exists():
            raise LLMUnavailable(
                f"No GGUF model found at '{cfg.model_path}'. Download one (see README, "
                "'Get a model') and set [llm].model_path in aether.toml.")
        kwargs = dict(model_path=str(path), n_ctx=cfg.context_tokens, n_gpu_layers=cfg.gpu_layers,
                      n_batch=cfg.batch_size, flash_attn=cfg.flash_attn, verbose=False)
        if cfg.threads:
            kwargs["n_threads"] = cfg.threads
        if cfg.chat_format:
            kwargs["chat_format"] = cfg.chat_format
        if getattr(cfg, "draft_lookup_tokens", 0) > 0:
            try:
                from llama_cpp.llama_speculative import LlamaPromptLookupDecoding
                kwargs["draft_model"] = LlamaPromptLookupDecoding(num_pred_tokens=cfg.draft_lookup_tokens)
            except ImportError:  # pragma: no cover - older llama-cpp-python
                pass
        self.model = Llama(**kwargs)
        if getattr(cfg, "prompt_cache_mb", 0) > 0:
            # The persona, preferences and tool catalog are the same every turn; caching their KV state
            # in RAM skips re-reading them, which is most of the prompt time on a partly offloaded 7B.
            try:
                from llama_cpp import LlamaRAMCache
                self.model.set_cache(LlamaRAMCache(capacity_bytes=cfg.prompt_cache_mb * 1024 * 1024))
            except Exception:  # pragma: no cover - older llama-cpp-python
                pass
        self.cfg = cfg
        self._lock = threading.Lock()  # llama.cpp contexts are not thread-safe

    def chat(self, messages: list[Message], *, max_tokens: int = 1024, temperature: float = 0.6,
             json_schema: dict | None = None, stop: list[str] | None = None) -> str:
        kwargs: dict = dict(messages=[m.as_dict() for m in messages], max_tokens=max_tokens,
                            temperature=temperature, stop=stop or [])
        if json_schema is not None:
            kwargs["response_format"] = {"type": "json_object", "schema": json_schema}
        with self._lock:
            out = self.model.create_chat_completion(**kwargs)
        return out["choices"][0]["message"]["content"] or ""

    def stream(self, messages: list[Message], *, max_tokens: int = 1024,
               temperature: float = 0.6) -> Iterator[str]:
        with self._lock:
            for chunk in self.model.create_chat_completion(
                    messages=[m.as_dict() for m in messages], max_tokens=max_tokens,
                    temperature=temperature, stream=True):
                delta = chunk["choices"][0]["delta"].get("content")
                if delta:
                    yield delta
