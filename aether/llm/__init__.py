from ..core.config import LLMConfig
from .base import LLMProvider, LLMUnavailable, Message, structured


def load_llm(cfg: LLMConfig) -> LLMProvider:
    if cfg.backend == "llama_cpp":
        from .llama_cpp_provider import LlamaCppProvider
        return LlamaCppProvider(cfg)
    if cfg.backend == "transformers":
        from .transformers_provider import TransformersProvider
        return TransformersProvider(cfg)
    raise LLMUnavailable(f"Unknown LLM backend '{cfg.backend}'")


__all__ = ["LLMProvider", "LLMUnavailable", "Message", "structured", "load_llm"]
