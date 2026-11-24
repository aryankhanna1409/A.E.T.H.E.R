"""Terminal chat for AETHER. Run: python -m aether"""
from __future__ import annotations

import argparse
import sys

from ..app import build
from ..core.logging import setup_logging

HELP = """Commands:
  /prefs                 show learned communication preferences
  /prefs off KEY         disable one        /prefs del KEY   delete one
  /prefs add TEXT        add your own rule
  /memory [layer]        list memories (semantic, episodic, observation, document, ...)
  /forget ID             delete a memory    /forget about TEXT   delete matching memories
  /facts [entity]        knowledge-graph facts          /why S P O   explain a derived fact
  /rule BODY -> HEAD     add a symbolic rule, e.g. /rule works_at(?x,?o) & located_in(?o,?c) -> works_in(?x,?c)
  /index FOLDER          index an authorized folder for RAG
  /state                 show the last cognitive state  /audit   recent activity log
  /optimize TASKS        e.g. /optimize essay:3:5:Wed; gym:1:3:Sun; slides:2:4:Fri
  /pareto TASKS          trade-off options for the week (NSGA-II), same format as /optimize
  /eval add MSG => TEXT  add a retrieval eval case (TEXT = memory that should come back; empty = nothing)
  /eval                  list eval cases       /tune   tune retrieval on them (asks before applying)
  /settings              learned setting overrides       /settings revert SECTION.KEY
  /metrics [days]        how AETHER has been performing    /good | /bad   rate the last answer
  /proposals             self-improvement proposals        /approve ID | /decline ID
  /reviews               post-task self-reviews of finished goals
  /ask PATTERN           logic query, e.g. /ask parent_of(?p, riya)
  /plan TASKS            exact planner, e.g. /plan essay:3:2; slides:2:4:essay
  /research QUESTION     cited research brief (web + your documents)
  /goal OBJECTIVE        run a multi-step goal with agents   /goals   list goals
  /reminders             open reminders       /observations   patterns AETHER noticed (confirm/reject)
  /confirm ID | /reject ID   confirm or reject an observation
  /undo                  undo the last file change           /journal   recent file changes
  emergency stop         block all actions immediately  resume   re-enable
  /quit"""


def ask(prompt: str) -> bool:
    print(f"\n\033[33m[permission] {prompt}\033[0m")
    return input("Allow? [y/N] ").strip().lower() in {"y", "yes"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aether")
    ap.add_argument("--config", default=None)
    ap.add_argument("--log", default="WARNING")
    args = ap.parse_args(argv)
    setup_logging(args.log)
    aether, problem = build(args.config, confirm=ask)
    print("\033[36mAETHER online.\033[0m Type /help for commands.")
    if problem:
        print(f"\033[31mNo language model loaded:\033[0m {problem}\nCommands still work; chat needs a model.")
    last_state = None
    while True:
        for n in aether.proactive.deliver():
            print(f"\n\033[35m[{n.title}]\033[0m {n.body}")
        try:
            text = input("\n\033[36myou >\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text.startswith("/"):
            if text in {"/quit", "/exit"}:
                return 0
            print(command(aether, text, last_state))
            continue
        try:
            reply = aether.handle(text)
        except Exception as e:
            print(f"\033[31merror:\033[0m {e}")
            continue
        last_state = reply.state
        for n in reply.notices:
            print(f"\033[90m· {n}\033[0m")
        print(f"\n\033[36maether >\033[0m {reply.text}")


def command(a, text: str, last_state) -> str:
    parts = text.split(maxsplit=2)
    cmd = parts[0]
    if cmd == "/help":
        return HELP
    if cmd == "/prefs":
        if len(parts) == 1:
            prefs = a.prefs.all()
            return "\n".join(f"[{'on ' if p.active else 'off'}] {p.key}: {p.instruction} "
                             f"(conf {p.confidence:.2f}, {p.source})" for p in prefs) or "No preferences learned yet."
        sub, arg = parts[1], parts[2] if len(parts) > 2 else ""
        if sub == "off":
            a.prefs.set_active(arg, False)
        elif sub == "on":
            a.prefs.set_active(arg, True)
        elif sub == "del":
            a.prefs.delete(arg)
        elif sub == "add":
            return f"Added {a.prefs.add_custom(arg)}"
        return "Done."
    if cmd == "/memory":
        layer = parts[1] if len(parts) > 1 else None
        mems = a.store.list(layer, 40)
        return "\n".join(f"#{m.id} [{m.layer}/{m.source} {m.confidence:.2f}] {m.text[:140]}" for m in mems) or "Empty."
    if cmd == "/forget":
        if len(parts) > 2 and parts[1] == "about":
            return f"Forgot {a.store.forget_matching(parts[2])} memories."
        a.store.forget(int(parts[1]))
        return "Forgotten."
    if cmd == "/facts":
        facts = a.kg.about(parts[1]) if len(parts) > 1 else a.kg.all()
        return "\n".join(f"#{t.id} {t} ({t.source}, {t.confidence:.2f})" for t in facts) or "No facts yet."
    if cmd == "/why":
        s, p, o = text.split()[1:4]
        a.rules.run(persist=False)
        return "\n".join(a.rules.explain((s, p, o)))
    if cmd == "/rule":
        a.rules.add_rule(text[len("/rule"):].strip())
        new, viol = a.rules.run()
        return f"Rule added. {len(new)} new facts derived, {len(viol)} constraint violations."
    if cmd == "/index":
        return a.index_folder(text[len("/index"):].strip())
    if cmd == "/state":
        return last_state.model_dump_json(indent=2) if last_state else "No state yet."
    if cmd == "/audit":
        return "\n".join(f"{e['action']} {e['target']} allowed={e['allowed']} ({e['reason']})"
                         for e in a.audit.tail(20)) or "Empty."
    if cmd == "/optimize":
        return a.tools.call("optimize_schedule", {"tasks": text[len("/optimize"):].strip()}).output
    if cmd == "/pareto":
        return a.tools.call("pareto_schedule", {"tasks": text[len("/pareto"):].strip()}).output
    if cmd == "/eval":
        rest = text[len("/eval"):].strip()
        if rest.startswith("add "):
            q, _, e = rest[4:].partition("=>")
            return f"Eval case #{a.evals.add(q, e)} added."
        if rest.startswith("del "):
            a.evals.remove(int(rest[4:]))
            return "Removed."
        return "\n".join(f"#{c['id']} {c['query']} => {c['expect'] or '(nothing)'}" for c in a.evals.rows()) or "No eval cases."
    if cmd == "/tune":
        from ..optimization.tuning import describe
        try:
            r = a.tune_retrieval()
        except ValueError as e:
            return str(e)
        print(describe(r))
        if r["worth_applying"] and ask("Apply the proposed retrieval settings?"):
            return "\n".join(a.settings.apply("memory", r["proposed"], f"PSO tuning F1 {r['f1_before']}->{r['f1_after']}"))
        return "Settings unchanged."
    if cmd == "/settings":
        if len(parts) > 2 and parts[1] == "revert":
            sec, _, key = parts[2].partition(".")
            return a.settings.revert(sec, key)
        return "\n".join(f"{o['section']}.{o['key']} = {o['value']} (was {o['previous']}; {o['reason']})"
                         for o in a.settings.list()) or "No learned overrides; aether.toml values are in use."
    if cmd == "/metrics":
        import json as _json
        return _json.dumps(a.meta.summary(float(parts[1]) if len(parts) > 1 else 7), indent=2)
    if cmd in {"/good", "/bad"}:
        a.meta.feedback(None, 1 if cmd == "/good" else -1)
        return "Thanks, noted."
    if cmd == "/proposals":
        a.meta.propose(a.cfg)
        return "\n".join(f"#{p.id} [{p.status}] {p.title}\n    {p.rationale}" for p in a.meta.proposals()) \
            or "No proposals. Nothing in the metrics needs changing."
    if cmd in {"/approve", "/decline"}:
        return a.meta.decide(int(parts[1]), cmd == "/approve", a.settings, a.prefs, a.store)
    if cmd == "/reviews":
        return "\n".join(f"goal #{r['goal_id']} [{r['status']}] score {r['score']:.2f}: {r['went_wrong']} Lesson: {r['lesson']}"
                         for r in a.meta.reviews()) or "No finished goals reviewed yet."
    if cmd == "/ask":
        return "\n".join(str(b) for b in a.rules.ask(text[4:].strip())) or "No matches."
    if cmd == "/plan":
        return a.tools.call("solve_plan", {"tasks": text[5:].strip()}).output
    if cmd == "/research":
        return a.tools.call("research", {"question": text[9:].strip()}).output
    if cmd == "/goal":
        if not a.executive:
            return "Goals need a loaded model."
        g = a.executive.run(text[5:].strip())
        return f"[{g['status']}]\n{g['report']}"
    if cmd == "/goals":
        return "\n".join(f"#{g['id']} [{g['status']}] {g['text']}" for g in a.graph.goals()) or "No goals yet."
    if cmd == "/reminders":
        return "\n".join(f"#{r.id} {r.due}: {r.text}" for r in a.reminders.open()) or "No open reminders."
    if cmd == "/observations":
        return "\n".join(f"#{o.id} [{o.status}] {o.text} (conf {o.confidence:.2f}; {'; '.join(o.evidence[-2:])})"
                         for o in a.observations.all()) or "No observations yet."
    if cmd in {"/confirm", "/reject"}:
        a.observations.set_status(int(parts[1]), "confirmed" if cmd == "/confirm" else "rejected")
        return "Done."
    if cmd == "/undo":
        return a.journal.undo_last()
    if cmd == "/journal":
        return "\n".join(f"#{j['id']} {j['op']} {j['src']} {'(undone)' if j['undone'] else ''}"
                         for j in a.journal.recent()) or "No file changes yet."
    return "Unknown command. /help lists them."


if __name__ == "__main__":
    sys.exit(main())
