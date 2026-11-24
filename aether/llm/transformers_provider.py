"""Alternative in-process backend using Hugging Face transformers (safetensors
weights in a local folder). Slower than llama.cpp on CPU, but handy for models
without a GGUF build. JSON mode is prompt-based here, then validated."""
from __future__ import annotations

import json
import threading
from typing import Iterator

from ..core.config import LLMConfig
from .base import LLMUnavailable, Message


class TransformersProvider:
    name = "transformers"

    def __init__(self, cfg: LLMConfig):
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as e:  # pragma: no cover
            raise LLMUnavailable("Install the extra: pip install -e .[transformers]") from e
        if not cfg.model_path:
            raise LLMUnavailable("Set [llm].model_path to a local Hugging Face model folder.")
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(cfg.model_path, local_files_only=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            cfg.model_path, local_files_only=True, torch_dtype="auto",
            device_map="auto" if torch.cuda.is_available() else None)
        self._lock = threading.Lock()

    def _ids(self, messages: list[Message]):
        return self.tok.apply_chat_template([m.as_dict() for m in messages], add_generation_prompt=True,
                                            return_tensors="pt").to(self.model.device)

    def chat(self, messages: list[Message], *, max_tokens: int = 1024, temperature: float = 0.6,
             json_schema: dict | None = None, stop: list[str] | None = None) -> str:
        if json_schema is not None:
            messages = messages + [Message("system", "Reply ONLY with JSON matching this schema:\n"
                                           + json.dumps(json_schema))]
        ids = self._ids(messages)
        with self._lock, self.torch.no_grad():
            out = self.model.generate(ids, max_new_tokens=max_tokens, do_sample=temperature > 0,
                                      temperature=max(temperature, 1e-3))
        text = self.tok.decode(out[0][ids.shape[-1]:], skip_special_tokens=True)
        if json_schema is not None:  # trim chatter around the JSON object
            start, end = text.find("{"), text.rfind("}")
            text = text[start:end + 1] if start >= 0 else text
        return text

    def stream(self, messages: list[Message], *, max_tokens: int = 1024,
               temperature: float = 0.6) -> Iterator[str]:
        yield self.chat(messages, max_tokens=max_tokens, temperature=temperature)
