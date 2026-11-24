"""Shared fixtures. `FakeLLM` is a test double used ONLY in tests to exercise
the orchestration logic deterministically; the app itself never falls back to it."""
import json

import pytest

from aether.cognition.orchestrator import Aether
from aether.core.config import Config


class FakeLLM:
    name = "fake"

    def __init__(self, reply="Here is my answer.", ir=None, claims=None, steps=None, structured=None):
        self.structured = structured or {}
        self.reply = reply
        self.ir = ir or {}
        self.claims = claims or []
        self.steps = list(steps or [])
        self.calls = []

    def chat(self, messages, *, max_tokens=1024, temperature=0.6, json_schema=None, stop=None):
        self.calls.append((messages, json_schema))
        if json_schema is not None:
            title = json_schema.get("title")
            if title == "IntentIR":
                base = {"intent": "question", "objective": messages[-1].content[-80:], "needs": ["direct"]}
                return json.dumps({**base, **self.ir})
            if title == "Claims":
                return json.dumps({"claims": self.claims})
            if title == "NextStep":
                return json.dumps(self.steps.pop(0) if self.steps else {"done": True})
            if title in self.structured:
                v = self.structured[title]
                return json.dumps(v(messages) if callable(v) else v)
            return "{}"  # other schemas: all defaults
        return self.reply(messages) if callable(self.reply) else self.reply

    def stream(self, messages, **kw):
        yield self.chat(messages)


@pytest.fixture
def cfg(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    c = Config(home=tmp_path / "home")
    c.home.mkdir()
    c.embeddings.backend = "lexical"
    c.security.allowed_read_roots = [str(docs.resolve())]
    c.security.allowed_write_roots = [str(out.resolve())]
    c.security.network = False
    return c


@pytest.fixture
def make_aether(cfg):
    def _make(llm=None, confirm=lambda _: True):
        return Aether(cfg, llm or FakeLLM(), None, confirm)
    return _make


@pytest.fixture(autouse=True)
def _restore_server_build():
    """Some tests point the web server at a test instance; put the real builder back afterwards."""
    from aether.interface import server
    real = server.build
    yield
    server.build = real
