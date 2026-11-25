# AETHER security and hardening

AETHER can read your files, browse the web, run allowlisted commands and drive apps, so it is built on the assumption that **the model can be wrong or manipulated**. Code, not the model, makes every safety decision.

## Threat model

| Threat | Defence (in code) |
| --- | --- |
| Model asks for something harmful by mistake | Policy engine: read roots, separate write roots, secrets denylist, ALLOW / CONFIRM / DENY per category, emergency stop |
| Instructions hidden in a web page, PDF, note or screenshot (prompt injection) | Data blocks are neutralized so they cannot pose as the user; a scanner flags instruction-like text; once flagged, the turn or goal is **tainted** and every action except plain reading needs your approval, with the reason shown |
| Data leaking out through a URL the model fetches | `web.fetch` and memory writes count as actions under taint, so they need your OK |
| Someone copies your AETHER folder | Memories and conversation logs are encrypted (Fernet); on Windows the key is wrapped with DPAPI, so it only opens for your Windows account on your PC |
| Self-improvement drifting into unsafe settings | Proposals change nothing until approved, and only whitelisted settings (retrieval, context, temperature) can be tuned; security settings are never touched |
| Anything going wrong unseen | Every tool call, block, taint and approval is in `audit.jsonl` and the Activity tab; file changes are journaled and undoable |

## Encryption at rest

* On by default (`[security].encrypt_at_rest = true`). Turning it on migrates existing rows in place.
* Encrypted: memory text (all layers, including indexed document chunks) and the conversation log.
* Not encrypted (needed for SQL queries or not personal): embeddings, knowledge-graph triples, reminders, metrics, the audit log, file names in the index. Use BitLocker on the drive if you want everything covered.
* The key is `%LOCALAPPDATA%\AETHER\aether.key`. Back it up with the database if you move PCs: a DPAPI-wrapped key only opens under the same Windows user, so to migrate, turn encryption off on the old PC, copy, then turn it back on.
* If the key is lost the memories are unreadable. AETHER says so loudly rather than showing garbage.

## Prompt-injection suite

`tests/test_phase11_hardening.py` contains 12 attack strings (instruction overrides, role hijacks, prompt probing, secrecy requests, URL and markdown-image exfiltration, tool commands, chat-template tokens and block escapes) and benign look-alikes that must **not** be flagged, plus end-to-end tests where a poisoned document tries to make a fooled model write a memory. `aether-eval` repeats the document attack against your real model.

The scanner is a tripwire, not a proof: new phrasings can slip past it. That is why the second layer (data is never instructions, every risky action needs a policy decision) does not depend on the scanner at all.

## Performance budget

Targets on an RTX 3050 Laptop (4 GB) with Qwen2.5-7B Q4_K_M at 20 GPU layers. Check yours with `python scripts\bench.py`.

| Measure | Budget (median) |
| --- | --- |
| Start-up (model load) | 45 s |
| First word of an answer (streamed; voice starts speaking here) | 6 s |
| Small talk reply | 8 s |
| General question | 25 s |
| Answer from your documents | 30 s |

If you are over budget: run `scripts\tune_gpu.py`, lower `[llm].context_tokens`, or switch to the 3B model. The Self tab tracks median and p95 reply times, and AETHER proposes a smaller retrieval size by itself when replies get slow.

## Proactive messages

AETHER may speak first for due reminders, a morning brief, events about to start, an evening review, low battery, finished goals, waiting proposals and a gentle check-in after repeated stress. Quiet hours (default 23:00 to 07:30) hold everything until morning, at most 3 messages are shown per hour, and nothing is dropped. All of it is in `[proactive]` in `aether.toml`.

The calendar is read from local files (and, if you turn it on, the Outlook app on this PC); nothing is fetched online. Calendar invites are written by other people, so event titles and locations are treated as untrusted data: tags are made inert, and a title that reads like instructions is hidden from the model entirely. **What goes online.** With `security.network = true` (the default), AETHER can search the web, read pages, and fetch the weather (Open-Meteo: the place name and then its coordinates) and news (the RSS feeds in `online.news_feeds`, or Google News for a topic you ask about). No accounts or API keys are involved, and your questions, memories, files and model never leave the PC; the language model always runs locally. Everything fetched is treated as untrusted: headlines that read like instructions are dropped from the briefing, and reading web output taints the turn like any other untrusted source. Set `network = false` and every online tool is refused by the policy engine.

Email is handled the same way and more strictly. Mail is only read, from files or the Outlook app on this PC. Anything read from it (and from the calendar) is scanned, and if it contains instructions aimed at the assistant the turn is tainted, so every later action asks with the reason shown. AETHER has no tool that sends mail: `draft_email` writes an unsent `.eml` (`X-Unsent: 1`) that you open, check and send yourself, and even that asks first (`mail.draft`). VIP notifications show only the sender and subject.

AETHER only ever writes to its own `aether.ics`, never into your exported files, and adding an event asks first unless you put `calendar.write` in `auto_approve`.
