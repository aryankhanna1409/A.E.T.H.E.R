"""Provider-agnostic LLM interface. The rest of AETHER only sees this.

All providers run models in-process on the local machine. There are no
network calls and no API keys anywhere in this package.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterator, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str

    def as_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


class LLMProvider(Protocol):
    name: str

    def chat(self, messages: list[Message], *, max_tokens: int = 1024, temperature: float = 0.6,
             json_schema: dict | None = None, stop: list[str] | None = None) -> str: ...

    def stream(self, messages: list[Message], *, max_tokens: int = 1024,
               temperature: float = 0.6) -> Iterator[str]: ...


class LLMUnavailable(RuntimeError):
    pass


def structured(llm: LLMProvider, messages: list[Message], model: type[T], *,
               max_tokens: int = 768, retries: int = 1) -> T:
    """Ask the model for JSON matching a Pydantic schema. llama.cpp enforces the
    schema with a grammar, so the output parses; we still validate and retry once."""
    schema = model.model_json_schema()
    last_err: Exception | None = None
    for _ in range(retries + 1):
        raw = llm.chat(messages, max_tokens=max_tokens, temperature=0.1, json_schema=schema)
        try:
            return model.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as e:
            last_err = e
            messages = messages + [Message("assistant", raw),
                                   Message("user", f"That was not valid JSON for the schema: {e}. Reply with JSON only.")]
    raise ValueError(f"Model did not produce valid {model.__name__}: {last_err}")
