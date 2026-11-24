# AETHER

**Adaptive Engine for Thought, Heuristics, Evolution & Reasoning**: a fully local, neuro-symbolic personal assistant for Windows 11.

No cloud APIs. No Ollama. The language model runs **in-process** through `llama-cpp-python` (GGUF weights on your disk). Embeddings, speech, vision and memory are local too, and your memories live in one encrypted SQLite file on your machine. The only network use is optional web research, which you can switch off.

## What it does

| Area | Capability |
| --- | --- |
| Conversation | Local Qwen2.5 model, desktop UI, terminal chat, and voice ("hey jarvis") with barge-in |
| Neuro-symbolic core | The model turns each message into a typed intermediate representation; code validates it, resolves dates, checks it against a knowledge graph and rule engine, solves constraints exactly, and verifies the answer against known facts (`/why` shows proofs) |
| Memory | Episodic, semantic, procedural, preference, prospective, observation and document layers; extraction after chats, summaries, reminders, decay, correction and deletion; hybrid BM25 + vector search |
| RAG vs non-RAG | A router decides per message: direct, memory, documents, web, symbolic, tools or optimize. Small talk does no retrieval |
| Documents and research | Folder watcher keeps your notes indexed, optional reranker, cited research briefs that separate what you already knew from what is new, fake citations removed by code |
| Agents | Multi-step goals as a persistent task graph: executive, research, reasoning, coding, operator, memory and verification agents; bounded retries; one re-plan; runs in the background |
| Computer control | Open allowlisted apps, sandboxed shell, move and delete to a trash with undo, screen reading (OCR) and UI automation, all off or asking first by default |
| Personalization | Learns what you like and hate in replies ("stop the long intros" takes effect at once); notices patterns about you but only keeps them if you confirm; timeline of every change |
| Wellness companion | Detects stress, chooses listening, planning, relaxation and other modes, asks "listen or solve?", and a crisis path whose helplines are guaranteed by code |
| Metaheuristics | Genetic algorithm, simulated annealing, particle swarm, tabu search, differential evolution and NSGA-II (Pareto trade-offs); self-tunes its own retrieval on your eval cases |
| Metacognition | Measures every turn, learns from 👍/👎 and "that's wrong", reviews its own finished goals, and proposes improvements that only apply when you approve |
| Proactive | Due reminders, a morning brief, finished goals and gentle check-ins, with quiet hours and a rate limit |
| Security | Policy engine on every action, secrets denylist, prompt-injection tripwire with taint mode, encryption at rest (DPAPI-protected key), audit log, emergency stop |

## Setup on Windows 11 (HP Victus 15 defaults)

Requirements: Python 3.11 (from python.org, with the `py` launcher), and for the GPU an NVIDIA driver 550 or newer. The defaults in `aether.example.toml` are tuned for an RTX 3050 Laptop (4 GB), Ryzen 5 5600H and 16 GB RAM.

```powershell
git clone https://github.com/aryankhanna1409/A.E.T.H.E.R
cd A.E.T.H.E.R
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Cuda   # drop -Cuda for CPU only
.\.venv\Scripts\python scripts\get_model.py                                # 7B model, ~4.7 GB, one time
.\.venv\Scripts\python scripts\get_model.py voice                          # optional: speaking voice
notepad aether.toml                                                        # model_path, your folders, voice path
.\.venv\Scripts\python scripts\tune_gpu.py                                 # optional: best gpu_layers for your GPU
.\.venv\Scripts\aether-server                                              # opens the UI at http://127.0.0.1:8765
```

Other ways in:

* `.\.venv\Scripts\aether` for the terminal (type `/help`).
* `.\.venv\Scripts\aether-voice` for hands-free voice.
* `desktop\` holds a Tauri 2 shell that wraps the UI as a native window (see `desktop\README.md`).
* `scripts\build_installer.ps1` builds a one-click `AETHER-Setup.exe` with Inno Setup.

### Choosing a model

| Your PC | Model | Command |
| --- | --- | --- |
| 4 GB VRAM (RTX 3050 Laptop), 16 GB RAM | Qwen2.5-7B Q4_K_M, 20 of 28 layers on the GPU (default) | `get_model.py` |
| Want faster replies on 4 GB | Qwen2.5-3B Q4_K_M, fully on the GPU | `get_model.py small` |
| 12 GB+ VRAM | Qwen2.5-14B Q4_K_M | `get_model.py large` |

Structured outputs are enforced with a JSON grammar, so even small models return valid intermediate representations.

## Check it on your PC

```powershell
.\.venv\Scripts\pytest                       # 103 unit tests, no model needed, about 15 s
.\.venv\Scripts\aether-eval                  # 10 end-to-end scenarios with YOUR model, 5-15 min
.\.venv\Scripts\python scripts\bench.py      # speed against the performance budget
```

`aether-eval` checks routing, fact learning and contradictions, preference enforcement, crisis resources, stress detection, document answers, prompt-injection resistance, topic switching, approval for deletes and the optimizer, all judged by code. It writes `eval_report.json`.

## Safety model

* AETHER starts **read-only** inside the folders you list. Writing needs a separate write root **and** your approval each time.
* Secrets (`.env`, keys, `.ssh`, browser profiles, credential stores) are blocked by code, whatever the model asks.
* Anything read from files, the web or the screen is untrusted data. If it contains instructions aimed at AETHER, the turn is tainted and every action needs your OK, with the reason shown.
* Memories and chats are encrypted at rest; the key only opens under your Windows account.
* `emergency stop` (or the red button) blocks every action until you say `resume`.
* Every access and change is in `audit.jsonl` and the Activity tab; file changes can be undone.
* The wellness companion is supportive conversation, not therapy. Crisis messages always get helpline information (Tele MANAS 14416 in India, 988 in the US, Samaritans 116 123 in the UK).

More in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/SECURITY.md](docs/SECURITY.md).
