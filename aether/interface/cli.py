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
    return "Unknown command. /help lists them."


if __name__ == "__main__":
    sys.exit(main())
