# AETHER

**Adaptive Engine for Thought, Heuristics, Evolution & Reasoning**: a fully local, neuro-symbolic personal assistant for Windows 11.

No cloud APIs. No Ollama. The language model runs **in-process** through `llama-cpp-python` (GGUF weights on your disk), embeddings run locally, and your memories live in one SQLite file on your machine. The only network use is optional web research, which you can switch off.

## What works in Phase 1

| Capability | How |
| --- | --- |
| Conversation | Local GGUF model (Qwen2.5 7B recommended), terminal chat and a local desktop UI |
| Neuro-symbolic core | LLM parses each message into a typed intermediate representation; code validates it, resolves dates, checks it against a knowledge graph and a rule engine, and verifies the answer against known facts |
| Knowledge graph + rules | Typed triples with provenance, contradiction detection, forward-chaining Horn rules with proofs (`/why`), integrity constraints |
| Memory | Episodic, semantic, procedural, preference, prospective, observation and document layers with source, confidence, dedupe, decay, correction and deletion |
| RAG vs non-RAG routing | Router picks direct, memory, documents, web, symbolic, tools or optimize per message, and does no retrieval for small talk |
| Hybrid retrieval | BM25 + local embeddings (`bge-small`), recency and confidence weighting |
| Topic switching | Topic tracker keeps separate threads and resumes old ones ("back to the flask bug") |
| Preference learning | "Stop giving me long intros" becomes an active preference at once; inferred signals need repetition; preferences are applied in the prompt and enforced by code filters; inspect/edit/delete them in the UI |
| Wellness companion | Detects stress, chooses listening / planning / relaxation / ... modes, asks "listen or solve?" when unclear, crisis path with India, US and UK helplines that code guarantees |
| Computer access | Policy engine checks every tool call: read-only roots, separate write roots, secrets denylist, confirmations, emergency stop, audit log |
| Web research | DuckDuckGo search + page reader (no API key), results marked as untrusted data |
| Metaheuristics | Genetic algorithm, simulated annealing and particle swarm; weekly schedule optimizer with constraints and trade-off report |

Not yet built (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the phase plan): voice, screen/app automation, shell execution, multi-agent task graphs, vision, metacognition analytics, Tauri desktop shell.

## Setup on Windows 11

Requirements: Python 3.11 or newer. An NVIDIA GPU with 8 GB+ VRAM is recommended but not required.

```powershell
git clone https://github.com/aryankhanna1409/A.E.T.H.E.R
cd A.E.T.H.E.R
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Cuda   # drop -Cuda for CPU only
.\.venv\Scripts\python scripts\get_model.py                                # one-time ~4.7 GB download
notepad aether.toml                                                        # set model_path and your folders
.\.venv\Scripts\aether-server                                              # opens the UI at http://127.0.0.1:8765
```

Terminal mode: `.\.venv\Scripts\aether`. Type `/help` for commands (`/prefs`, `/memory`, `/facts`, `/why`, `/index`, `/optimize`, `/audit`, `emergency stop`).

### Choosing a model

The defaults are tuned for an **HP Victus 15** (RTX 3050 Laptop 4 GB, Ryzen 5 5600H, 16 GB RAM).

| Your PC | Model | Settings | Command |
| --- | --- | --- | --- |
| RTX 3050 Laptop 4 GB, 16 GB RAM (default) | Qwen2.5-7B-Instruct Q4_K_M (~4.7 GB) | `gpu_layers = 20`, `context_tokens = 6144`, `threads = 6` | `get_model.py` |
| Same laptop, speed over quality | Qwen2.5-3B-Instruct Q4_K_M (~2 GB) | `gpu_layers = -1`, `context_tokens = 8192` | `get_model.py small` |
| 8 GB+ VRAM | Qwen2.5-7B-Instruct Q4_K_M | `gpu_layers = -1` | `get_model.py` |
| 12 GB+ VRAM | Qwen2.5-14B-Instruct Q4_K_M (~9 GB) | `gpu_layers = -1` | `get_model.py large` |

The 7B model doesn't fit in 4 GB of VRAM, so about 20 of its 28 layers run on the GPU and the rest on the CPU. Expect roughly 6 to 12 tokens per second with the 7B model and 30+ with the 3B. Measure the real numbers on your laptop with `python scripts\tune_gpu.py`, which tries several `gpu_layers` values and prints the fastest one that fits. Plug the laptop in and use the "Performance" power mode, because on battery the GPU is throttled hard.

Any instruct GGUF works; llama.cpp reads the chat template from the file. Structured outputs (the intermediate representation) are enforced with a JSON grammar, so even small models return valid JSON.

## Tests

```powershell
.\.venv\Scripts\pytest
```

27 tests cover the symbolic engine, policy enforcement, memory, preference learning, topic switching, the wellness paths (including crisis), RAG routing, prompt-injection labelling, file-creation approval and verification, the agent loop stopping on denied actions, the optimizer, and the UI server. Orchestration tests use a scripted model double so they run in seconds without weights; the app itself never substitutes a fake model and reports clearly when none is loaded.

## Safety model

* AETHER starts **read-only** inside the folders you list. Writing needs a separate write root **and** your approval each time.
* Secrets (`.env`, keys, `.ssh`, browser profiles, credential stores) are blocked by code, whatever the model asks.
* Anything read from files or the web is wrapped as untrusted data, and the model is told never to follow instructions inside it.
* `emergency stop` (or the red button) blocks every action until you say `resume`.
* Every access and change is in `audit.jsonl` and the Activity tab.
* The wellness companion is supportive conversation, not therapy. Crisis messages always get helpline information (Tele MANAS 14416 in India, 988 in the US, Samaritans 116 123 in the UK).
