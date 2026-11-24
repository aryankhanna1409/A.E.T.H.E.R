import os
import time
from pathlib import Path

from conftest import FakeLLM

from aether.research import pipeline
from aether.retrieval.citations import validate
from aether.tools.registry import Tool


def test_citation_validation_removes_fabricated():
    text, bad = validate("Fusion works [web 1]. Also [web 7] and [doc 1].", {"web": 2, "doc": 1})
    assert bad == ["[web 7]"] and "[web 1]" in text and "[web 7]" not in text


def test_watcher_indexes_updates_and_removes(make_aether, cfg):
    docs = Path(cfg.security.allowed_read_roots[0])
    f = docs / "notes.md"
    f.write_text("alpha beta gamma")
    (docs / "id_rsa").write_text("PRIVATE")
    a = make_aether()
    w = a.watcher()
    assert w.scan(str(docs))["added"] == 1
    assert w.scan(str(docs))["added"] == 0  # unchanged
    f.write_text("alpha beta delta")
    os.utime(f, (time.time() + 5, time.time() + 5))
    assert w.scan(str(docs))["updated"] == 1
    assert "delta" in a.store.search("delta", layers=("document",))[0].text
    f.unlink()
    assert w.scan(str(docs))["removed"] == 1 and a.store.count("document") == 0


def test_research_pipeline_separates_known_and_new_and_validates(make_aether, cfg):
    docs = Path(cfg.security.allowed_read_roots[0])
    (docs / "fusion.md").write_text("My fusion energy notes: tokamaks confine plasma with magnetic fields.")
    llm = FakeLLM(reply="Known: tokamaks use magnets [doc 1]. New: record run in 2026 [web 1]. Fake [web 9].",
                  structured={"SubQueries": {"queries": ["fusion energy record 2026"]}})
    a = make_aether(llm)
    a.watcher().scan(str(docs))
    a.tools.register(Tool("web_search", "stub", {"query": "", "max_results": ""}, "memory",
                          lambda query, max_results="4": "[1] Fusion record\nURL: https://example.org/f\nA record run."))
    a.tools.register(Tool("web_fetch", "stub", {"url": ""}, "memory",
                          lambda url: "Title: Fusion record\n\nThe 2026 tokamak run set a record."))
    rep = pipeline.run(llm, a.tools, a.store, "latest fusion energy developments")
    out = rep.render()
    assert "[web 9]" not in rep.report and rep.removed_citations == ["[web 9]"]
    assert "[web 1] Fusion record (https://example.org/f)" in out and "[doc 1] fusion.md" in out
