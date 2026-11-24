# AETHER blueprint: Windows 11, fully local, maximum ambition

## Principles

1. **The LLM is not the whole brain.** A local model interprets and writes; deterministic code validates, gates, reasons over facts and checks answers.
2. **Local first, offline capable.** Models run in-process (llama.cpp, sentence-transformers, later faster-whisper and Piper). No Ollama, no hosted APIs, no keys.
3. **Verifiable autonomy.** Every action passes a policy engine, needs approval when risky, is audited, and is verified after it runs.
4. **Inspectable learning.** Preferences, memories, facts and observations can all be viewed, corrected and deleted.

## The turn pipeline

```
PERCEIVE   wellness check, topic tracker, preference feedback           (code)
INTERPRET  message -> IntentIR (JSON-grammar constrained)               (LLM)
VALIDATE   resolve dates, drop unknown tools, ambiguity gate            (code)
LEARN      stated facts -> knowledge graph, contradictions flagged      (code)
RETRIEVE   router: direct | memory | documents | web | symbolic | ...  (code + hybrid search)
REASON     forward-chaining rules, constraint violations, proofs        (code)
PLAN/EXEC  tool loop, every call through the policy engine              (LLM proposes, code decides)
GENERATE   answer from separated trusted facts and untrusted data       (LLM)
VERIFY     claims vs knowledge graph -> revise; crisis resources; filters (LLM extracts, code judges)
LEARN      episodic summary, mood observation (inferred, never a fact)  (code)
```

## Repository structure

```
aether/
  core/            config (pydantic, aether.toml), logging, audit log
  llm/             provider interface, llama.cpp provider, transformers provider, local embeddings
  cognition/       IR schema + cognitive state, perception, router, context (topics, working memory), orchestrator
  neurosymbolic/   knowledge graph, rule engine, policy engine, temporal reasoning, verifier
  memory/          layered SQLite store with hybrid search
  retrieval/       BM25 and sparse vectors
  personalization/ preference learning and enforcement
  wellness/        companion modes, crisis handling
  tools/           registry, filesystem, web
  optimization/    GA, simulated annealing, PSO, schedule problem
  interface/       CLI, FastAPI server, web UI (later wrapped in Tauri)
tests/             27 tests, no model weights needed
scripts/           Windows setup, model download
```

Planned additions follow the same shape: `agents/` (executive, research, coding, operator, verification), `speech/` (stt, tts), `perception/vision`, `metacognition/`, `tools/apps`, `tools/shell`, `desktop/` (Tauri).

## Dependencies

| Purpose | Package | Phase |
| --- | --- | --- |
| LLM runtime | llama-cpp-python (CUDA or CPU wheel) | 1 |
| Config / schemas | pydantic 2 | 1 |
| UI server | fastapi, uvicorn | 1 |
| Web research | ddgs, httpx | 1 |
| Embeddings | sentence-transformers (bge-small-en-v1.5) | 1 (optional extra) |
| Documents | pypdf, python-docx | 1 (optional extra) |
| Vector index at scale | sqlite-vec or hnswlib | 2 |
| Reranking | bge-reranker-base via sentence-transformers CrossEncoder | 4 |
| Browser automation | playwright | 4 |
| Speech to text | faster-whisper (CTranslate2), silero-vad | 8 |
| Text to speech | piper-tts | 8 |
| Wake word | openWakeWord | 8 |
| Vision | llama.cpp multimodal (Qwen2.5-VL GGUF) or Florence-2 ONNX | 8 |
| Windows automation | pywinauto, uiautomation, mss | 6 |
| Optimization at scale | DEAP, pymoo (NSGA-II) | 9 |
| Desktop shell | Tauri 2 + React + TypeScript | 7 |

## Phases

| # | Phase | Status | Exit criteria |
| --- | --- | --- | --- |
| 1 | Foundation + cognitive core | **Done (this PR)** | Chat, IR, KG, rules, memory, routing, prefs, wellness, policy, UI, 27 tests |
| 2 | Memory depth | Next | sqlite-vec index, memory extraction after chats, summaries, prospective reminders, decay jobs |
| 3 | Neuro-symbolic depth | | LLM-extracted triples from documents, rule authoring in UI, proof display, constraint solver for plans |
| 4 | RAG and research | | Ingestion watcher, reranker, query decomposition, citation validation, research reports |
| 5 | Agents and autonomy | | Task graph with checkpoints, executive/research/verification agents, retries with limits, background goals |
| 6 | Computer control | | App launch, UI automation, sandboxed scripts, screen reading, undo journal |
| 7 | Personalization and desktop UI | | Tauri shell, observation review screen, preference timeline |
| 8 | Voice and multimodal | | Wake word, streaming STT/TTS with barge-in, screenshot and image understanding |
| 9 | Metaheuristics | | NSGA-II, optimization workspace, self-tuning of router thresholds on a scored eval set |
| 10 | Metacognition | | Post-task self-evaluation, metrics dashboard, approval-gated self-improvement |
| 11 | Hardening | | Encryption at rest (DPAPI), prompt-injection suite, installer, performance budget |

## Known limits of Phase 1

* Small local models follow instructions less reliably than large cloud models. The symbolic layer exists to catch that, but answer quality still depends on the GGUF you choose.
* Without the `embeddings` extra, search is keyword-only (BM25); `/status` and the UI header say so.
* Web search uses DuckDuckGo's public results, which can rate-limit heavy use.
* The orchestrator was tested end to end with a scripted model double; it has not yet been run against real GGUF weights in CI.
