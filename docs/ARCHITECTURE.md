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
  core/            config (pydantic, aether.toml), logging + audit, encryption at rest (DPAPI), learned setting overrides
  llm/             provider interface, llama.cpp provider, transformers provider, local embeddings
  cognition/       IR schema + cognitive state, perception, router, context (topics, working memory), orchestrator, glass-box trace recorder
  neurosymbolic/   knowledge graph, rule engine, CSP solver, policy engine, temporal reasoning (as-of queries, belief history), what-if counterfactuals, verifier, document facts
  memory/          layered SQLite store with hybrid search, vector index, extraction, reminders, lifecycle
  retrieval/       BM25, folder watcher, reranker, citation validation
  research/        cited research pipeline (known vs new)
  agents/          task graph, executive, specialists (research, reasoning, coding, operator, memory, verification)
  tools/           registry (policy + taint), filesystem, web, computer (apps, shell, trash, undo, OCR, UI automation)
  personalization/ preference learning and enforcement, behavioural observations
  wellness/        companion modes, crisis handling
  optimization/    GA, SA, PSO (engine), NSGA-II, tabu, DE (advanced), retrieval self-tuning
  metacognition/   turn metrics, feedback, post-task self-review, improvement proposals
  proactive/       triggers, quiet hours, rate limits, notifications
  security/        prompt-injection scanner and neutralizer
  speech/          faster-whisper STT, Piper TTS, VAD, wake word, voice loop with barge-in
  perception/      local vision (Qwen2.5-VL via llama.cpp, loaded on demand)
  evals/           end-to-end scenarios for the real model (aether-eval)
  interface/       CLI, FastAPI server (SSE streaming), web app (web/static: app.js, md.js, graph.js, inspector.js, dag.js, hud.js, app.css; no build step, no CDN)
desktop/           Tauri 2 shell
installer/         Inno Setup script
scripts/           Windows setup, model download, GPU tuning, benchmark, installer build
tests/             122 tests, no model weights needed
```

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

| # | Phase | Status | What landed |
| --- | --- | --- | --- |
| 1 | Foundation + cognitive core | Done | Chat, IR, KG, rules, memory, routing, prefs, wellness, policy, UI |
| 2 | Memory depth | Done | Vector index, memory extraction after chats, topic summaries, prospective reminders, decay |
| 3 | Neuro-symbolic depth | Done | LLM-extracted triples from documents, persisted user rules, first-order queries with proofs, CSP planner |
| 4 | RAG and research | Done | Folder watcher, reranker, cited research briefs, citation validation |
| 5 | Agents and autonomy | Done | Persistent task graph, executive + specialists + verifier, bounded retries, re-plan, background goals |
| 6 | Computer control | Done | App launch, sandboxed shell, trash + undo journal, OCR screen reading, UI automation |
| 7 | Personalization and desktop UI | Done | Tauri shell, observation review, preference timeline, goals and patterns tabs |
| 8 | Voice and multimodal | Done | Wake word, STT/TTS with barge-in and follow-up window, image understanding |
| 9 | Metaheuristics | Done | NSGA-II, tabu search, differential evolution, optimization workspace (Lab tab), PSO self-tuning of retrieval |
| 10 | Metacognition | Done | Turn metrics, implicit and explicit feedback, post-task self-review with lessons, approval-gated proposals (Self tab) |
| 11 | Hardening | Done | Encryption at rest (DPAPI), injection tripwire + taint mode + test suite, proactive engine, real-model evals, installer, performance budget |
| 12 | Web app and polish | Done | Streaming replies with live pipeline events, saved conversations, knowledge map, command palette, browser voice and images, prompt KV cache, small-talk fast path |
| 13 | See inside it | Done | Glass-box traces of every turn (stages, LLM calls, retrieval scores, proofs, verifier, prompt budget), counterfactual what-if engine with graph diff, belief time travel over the temporal KG, live mission-control goal graph, HUD voice mode driven by real audio |

## Known limits

* Everything was built and unit-tested here with a scripted model double (122 tests). It has **not yet run against real GGUF weights, a microphone, a GPU or Windows APIs**; `aether-eval` and `scripts/bench.py` are the first real check on your PC.
* Small local models follow instructions less reliably than large cloud models. The symbolic layer and code-side filters catch a lot of that, but answer quality still depends on the GGUF you choose.
* The Tauri shell and the Inno Setup installer have not been compiled here.
* The injection scanner is a tripwire; the policy engine and taint mode are the real guarantee.
* Without the `embeddings` extra, search is keyword-only (BM25); `/status` and the UI header say so.
* Web search uses DuckDuckGo's public results, which can rate-limit heavy use.
