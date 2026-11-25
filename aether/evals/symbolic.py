"""Does the symbolic layer actually help? A reproducible benchmark on your own model.

The same local model answers the same questions about the same small world in three set-ups:

- ``plain``: the model alone, with no notes. This is what a bare 7B chatbot gives you.
- ``rag``: the model plus the eight most relevant notes (BM25 over one sentence per fact, including outdated
  notes with their dates). This is the usual "retrieval-augmented" assistant.
- ``aether``: the full AETHER pipeline: knowledge graph, rules, router, context, verifier.

The world is synthetic and generated from a fixed seed (three families, their jobs, homes, studies, projects
and allergies, some of which changed over time), so nothing personal is involved and every run asks the same
questions. Every answer is scored by code, never by a model:

=============  ===========================================================  =================================
category       what is asked                                                correct when the answer...
=============  ===========================================================  =================================
recall         a fact that was stated                                       names the stored value
multi-hop      something that follows from two facts (grandparents,         names the value the rules derive
               the city someone works in, part-of chains, experience)
updated        a fact that changed ("where does X live now?")               names the new value, not the old
unanswerable   something never stated (birthdays, unknown people)           says it doesn't know
false premise  a question built on a wrong fact ("why did X move to Y?")    corrects it or rejects the premise
consistency    the same fact asked three different ways                     all three answers are right
=============  ===========================================================  =================================

A *hallucination* is an answer that commits to something unsupported: a wrong value in an answerable
question, an invented answer to an unanswerable one, or going along with a false premise.

Run on your PC (about 30-45 minutes for all three systems on a 7B model; ``--limit`` for a quick look):

    .\\.venv\\Scripts\\python -m aether.evals.symbolic
    .\\.venv\\Scripts\\python -m aether.evals.symbolic --limit 3 --systems plain,aether

Results go to ``bench_symbolic/``: ``report.html`` (open it in a browser: charts and every answer),
``results.json``, ``answers.csv`` and ``summary.csv``.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import random
import re
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

SYSTEMS = ("plain", "rag", "aether")
CATEGORIES = ("recall", "multi-hop", "updated", "unanswerable", "false premise", "consistency")
LABELS = {"plain": "Plain 7B", "rag": "7B + RAG", "aether": "AETHER"}
COLOURS = {"plain": "#9AA5B1", "rag": "#6B5CE0", "aether": "#0A9BB8"}
SYSTEM_PROMPT = "You are a helpful personal assistant. Answer the question briefly and directly."

# ---------------------------------------------------------------------------------------------------------------
# The world


@dataclass
class Fact:
    s: str
    p: str
    o: str
    date: str = ""          # when it was stated (for the RAG notes)
    old: str = ""           # the previous value, for facts that changed

    def sentence(self) -> str:
        return SENTENCES[self.p].format(s=self.s, o=self.o)


SENTENCES = {
    "parent_of": "{s} is the parent of {o}.",
    "sibling_of": "{s} and {o} are siblings.",
    "lives_in": "{s} lives in {o}.",
    "works_at": "{s} works at {o}.",
    "located_in": "{s} is located in {o}.",
    "studies_at": "{s} studies at {o}.",
    "allergic_to": "{s} is allergic to {o}.",
    "works_on": "{s} works on the {o} project.",
    "uses": "The {s} project uses {o}.",
    "part_of": "The {s} is part of the {o}.",
}

FAMILIES = [
    ("Meera Iyer", "Riya Iyer", "Arjun Iyer", "Kabir Iyer", "Isha Iyer"),
    ("Tom Walsh", "Nora Walsh", "Liam Walsh", "Ava Walsh", "Owen Walsh"),
    ("Sara Haddad", "Omar Haddad", "Layla Haddad", "Zain Haddad", "Mira Haddad"),
]
CITIES = ["Pune", "Leeds", "Lisbon", "Nagpur", "Austin", "Kyoto", "Oslo", "Porto", "Dubai", "Hobart", "Ghent", "Quito"]
ORGS = [("Brightwave Labs", "Ghent"), ("Northpeak Robotics", "Oslo"), ("Saffron Analytics", "Nagpur"),
        ("Bluegum Systems", "Hobart"), ("Tidewater Health", "Porto"), ("Lumen Foods", "Quito")]
SCHOOLS = ["Westbrook University", "Coral Bay College", "Hillcrest Institute"]
ALLERGENS = ["peanuts", "penicillin", "shellfish", "pollen", "latex", "sesame"]
PROJECTS = [("Kestrel", "ROS 2"), ("Ember", "PyTorch"), ("Atlas", "Kubernetes"), ("Wren", "Rust")]
PARTS = [("motor driver", "drive unit"), ("drive unit", "rover chassis"), ("lidar mount", "sensor mast"),
         ("sensor mast", "rover chassis")]
UNKNOWN_PEOPLE = ["Felix Brandt", "Hana Sato", "Diego Ruiz"]


@dataclass
class Question:
    id: str
    category: str
    text: str
    expect: list[str] = field(default_factory=list)   # any of these counts as right
    wrong: list[str] = field(default_factory=list)    # naming one of these (without a correction) is wrong
    group: str = ""                                  # consistency questions that ask the same thing


def build_world(seed: int = 7) -> tuple[list[Fact], list[str]]:
    """Facts and the extra rules AETHER gets. Deterministic for a given seed."""
    rnd = random.Random(seed)
    facts: list[Fact] = []
    cities = CITIES[:]
    rnd.shuffle(cities)
    for gp, a, b, c, d in FAMILIES:
        # gp -> a ; a sibling b ; a -> c ; b -> d   (the rules derive gp -> b, gp grandparent of c and d)
        facts += [Fact(gp, "parent_of", a), Fact(a, "sibling_of", b), Fact(a, "parent_of", c), Fact(b, "parent_of", d)]
    people = [p for fam in FAMILIES for p in fam]
    for i, person in enumerate(people):
        facts.append(Fact(person, "lives_in", cities[i % len(cities)], date="2026-03"))
    for i, person in enumerate(people[1::2]):
        org = ORGS[i % len(ORGS)][0]
        facts.append(Fact(person, "works_at", org, date="2025-09"))
    for org, city in ORGS:
        facts.append(Fact(org, "located_in", city))
    for i, person in enumerate(people[3::5]):
        facts.append(Fact(person, "studies_at", SCHOOLS[i % len(SCHOOLS)], date="2025-07"))
    for i, person in enumerate(people[2::4]):
        facts.append(Fact(person, "allergic_to", ALLERGENS[i % len(ALLERGENS)]))
    for i, (proj, tool) in enumerate(PROJECTS):
        facts += [Fact(proj, "uses", tool), Fact(people[4 + i * 3], "works_on", proj)]
    facts += [Fact(a, "part_of", b) for a, b in PARTS]
    # six people moved: the old home is stated first (with an older date), then the new one
    movers = people[0:15:3][:5] + [people[13]]
    for k, person in enumerate(movers):
        f = next(f for f in facts if f.s == person and f.p == "lives_in")
        new = cities[(cities.index(f.o) + 5 + k) % len(cities)]
        f.old, f.o, f.date = f.o, new, "2026-10"
    rules = ["works_on(?x, ?p) & uses(?p, ?t) -> experienced_with(?x, ?t)"]
    return facts, rules


def first(name: str) -> str:
    return name.split()[0]


def build_questions(facts: list[Fact]) -> list[Question]:
    qs: list[Question] = []
    by = defaultdict(list)
    for f in facts:
        by[f.p].append(f)

    def add(cat, text, expect=(), wrong=(), group=""):
        qs.append(Question(f"{cat[:4]}{len([q for q in qs if q.category == cat]) + 1:02d}", cat, text,
                           list(expect), list(wrong), group))

    # recall: stated facts that never changed
    stable = [f for f in by["lives_in"] if not f.old]
    for f in stable[:4]:
        add("recall", f"Which city does {f.s} live in?", [f.o])
    for f in by["works_at"][:3]:
        add("recall", f"Where does {f.s} work?", [f.o])
    for f in by["studies_at"][:2]:
        add("recall", f"Which university does {f.s} study at?", [f.o])
    for f in by["allergic_to"][:3]:
        add("recall", f"What is {f.s} allergic to?", [f.o])

    # multi-hop: needs two facts and a rule
    for gp, a, b, c, d in FAMILIES:
        add("multi-hop", f"Who is {c}'s grandparent?", [gp, first(gp)])
        add("multi-hop", f"Who is {b}'s parent?", [gp, first(gp)])
    located = {f.s: f.o for f in by["located_in"]}
    for f in by["works_at"][:3]:
        add("multi-hop", f"Which city does {f.s} work in?", [located[f.o]])
    uses = {f.s: f.o for f in by["uses"]}
    for f in by["works_on"][:2]:
        add("multi-hop", f"Does {f.s} have experience with {uses[f.o]}? Answer yes or no first.", ["yes"], ["no"])
    add("multi-hop", "Is the motor driver part of the rover chassis? Answer yes or no first.", ["yes"], ["no"])

    # updated: the value changed
    for f in [f for f in by["lives_in"] if f.old]:
        add("updated", f"Where does {f.s} live now?", [f.o], [f.old])

    # unanswerable: never stated
    known = [f.s for f in by["lives_in"]]
    for person in known[:4]:
        add("unanswerable", f"What is {person}'s date of birth?")
    for person in known[4:7]:
        add("unanswerable", f"What is {person}'s favourite film?")
    for person in UNKNOWN_PEOPLE:
        add("unanswerable", f"Which city does {person} live in?")

    # false premise: the question assumes something that contradicts a stored fact
    for f in stable[4:8]:
        fake = next(c for c in CITIES if c != f.o)
        add("false premise", f"Why did {f.s} move to {fake}?", [f.o], [fake])
    for f in by["works_at"][3:6]:
        fake = next(o for o, _ in ORGS if o != f.o)
        add("false premise", f"How long has {f.s} been working at {fake}?", [f.o], [fake])
    for f in by["allergic_to"][3:4]:
        fake = next(a for a in ALLERGENS if a != f.o)
        add("false premise", f"Since {f.s} is allergic to {fake}, what should I avoid cooking?", [f.o], [fake])

    # consistency: the same fact, three phrasings
    for f in stable[6:9]:
        g = f"lives:{f.s}"
        for t in (f"Where does {f.s} live?", f"Which city is {f.s} based in?", f"What city is {first(f.s)} {f.s.split()[1]}'s home?"):
            add("consistency", t, [f.o], [], g)
    for f in by["works_at"][4:7]:
        g = f"works:{f.s}"
        for t in (f"Who does {f.s} work for?", f"What company employs {f.s}?", f"Name {f.s}'s employer."):
            add("consistency", t, [f.o], [], g)
    return qs


# ---------------------------------------------------------------------------------------------------------------
# Scoring (code only)

ABSTAIN = re.compile(
    r"\b(?:(?:i|we)\s+(?:do\s*n[o']?t|don't|dont|cannot|can't|am not able to|have no|haven't|have not)\s+"
    r"(?:know|have|find|see|tell|recall|been told|any|access|information|info|record|details|able|sure)"
    r"|no (?:information|info|record|details|data|mention|knowledge)"
    r"|not (?:sure|aware|mentioned|specified|provided|stated|in (?:my|the) (?:notes|memory|records))"
    r"|(?:isn't|is not|wasn't|was not|aren't) (?:mentioned|specified|provided|stated|available|known|recorded)"
    r"|(?:unknown|unclear) (?:to me)?"
    r"|nothing (?:about|on|in my)|no way (?:to|of) know|haven't (?:told|mentioned|shared)"
    r"|you haven't|not something i know)", re.I)
REJECT = re.compile(
    r"\b(?:actually|however|but|in fact|not (?:correct|right|true|accurate)|incorrect|mistaken|there is no record"
    r"|doesn't|does not|didn't|did not|isn't|is not|hasn't|has not|never|no indication|no evidence|rather)\b", re.I)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("_", " ").lower())


def has(answer: str, value: str) -> bool:
    v = norm(value)
    if v in {"yes", "no"}:
        return re.match(rf"\W*{v}\b", norm(answer)) is not None
    return re.search(rf"(?<![a-z0-9]){re.escape(v)}(?![a-z0-9])", norm(answer)) is not None


@dataclass
class Verdict:
    correct: bool
    hallucinated: bool
    abstained: bool
    reason: str


def score(q: Question, answer: str) -> Verdict:
    a = answer or ""
    abstained = bool(ABSTAIN.search(a))
    right = any(has(a, e) for e in q.expect)
    wrong = any(has(a, w) for w in q.wrong)
    if q.category == "unanswerable":
        return Verdict(abstained, not abstained, abstained, "abstained" if abstained else "made something up")
    if q.category == "false premise":
        corrected = right or (bool(REJECT.search(a)) and not wrong) or (abstained and not wrong)
        if wrong and (right or REJECT.search(a)):
            corrected = True     # "Riya didn't move to Lisbon, she lives in Pune"
        return Verdict(corrected, not corrected and not abstained, abstained,
                       "corrected the premise" if corrected else "went along with it")
    if q.category == "updated" and right and wrong:
        # "used to live in Pune, now in Delhi" is right; "lives in Pune" (and Delhi only in passing) is checked
        # by which value is named last.
        last = max(((m.start(), True) for e in q.expect for m in re.finditer(re.escape(norm(e)), norm(a))),
                   default=(-1, True))
        lastw = max(((m.start(), False) for w in q.wrong for m in re.finditer(re.escape(norm(w)), norm(a))),
                    default=(-1, False))
        right = last[0] > lastw[0] or bool(re.search(r"\b(?:now|currently|moved|used to|previously|formerly)\b", norm(a)))
    if right and not (wrong and q.category not in ("updated",)):
        return Verdict(True, False, abstained, "right")
    if abstained:
        return Verdict(False, False, True, "said it didn't know")
    return Verdict(False, True, False, "wrong value")


# ---------------------------------------------------------------------------------------------------------------
# The three systems


def notes_for(facts: list[Fact]) -> list[str]:
    out = []
    for f in facts:
        if f.old:
            out.append(f"({f.date[:4]}-01) " + SENTENCES[f.p].format(s=f.s, o=f.old))
            out.append(f"({f.date}) {f.s} moved to {f.o}.")
        else:
            out.append((f"({f.date}) " if f.date else "") + f.sentence())
    return out


def bm25(query: str, docs: list[str], k: int = 8) -> list[str]:
    tok = lambda t: re.findall(r"[a-z0-9]+", t.lower())
    dt = [tok(d) for d in docs]
    avg = sum(map(len, dt)) / max(1, len(dt))
    df = Counter(w for d in dt for w in set(d))
    n = len(dt)

    def s(d):
        tf = Counter(d)
        return sum(math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5)) * tf[w] * 2.2 / (tf[w] + 1.2 * (0.25 + 0.75 * len(d) / avg))
                   for w in set(tok(query)) if w in tf)
    ranked = sorted(range(n), key=lambda i: -s(dt[i]))
    return [docs[i] for i in ranked[:k] if s(dt[i]) > 0]


def ask_plain(llm, q: str, temperature: float) -> str:
    from ..llm.base import Message
    return llm.chat([Message("system", SYSTEM_PROMPT), Message("user", q)], max_tokens=200, temperature=temperature)


def ask_rag(llm, q: str, notes: list[str], temperature: float) -> str:
    from ..llm.base import Message
    hits = bm25(q, notes)
    ctx = "Notes about the people in the user's life (dates show when each note was written):\n" + \
          "\n".join(f"- {h}" for h in hits)
    return llm.chat([Message("system", SYSTEM_PROMPT + "\n\n" + ctx), Message("user", q)], max_tokens=200,
                    temperature=temperature)


def make_aether_factory(base_cfg, llm, embedder) -> Callable[[list[Fact], list[str]], object]:
    """Each question gets a fresh AETHER with the world loaded, so nothing one answer teaches leaks into the next."""
    from ..cognition.orchestrator import Aether

    def make(facts: list[Fact], rules: list[str]):
        home = Path(tempfile.mkdtemp(prefix="aether-bench-"))
        cfg = base_cfg.model_copy(deep=True)
        cfg.home = home
        (home / "docs").mkdir(parents=True, exist_ok=True)
        cfg.security.allowed_read_roots = [str(home / "docs")]
        cfg.security.allowed_write_roots = []
        cfg.security.network = False
        cfg.memory.watch_interval_s = 0
        cfg.proactive.enabled = False
        a = Aether(cfg, llm, embedder)
        for f in facts:
            if f.old:
                a.kg.assert_fact(f.s, f.p, f.old, source="user_stated")
            a.kg.assert_fact(f.s, f.p, f.o, source="user_stated", replace=True)
        for r in rules:
            a.rules.add_rule(r)
        a.rules.run()
        return a
    return make


# ---------------------------------------------------------------------------------------------------------------
# Running


def _dispose(a) -> None:
    """Close a per-question AETHER and delete its throwaway folder."""
    import shutil
    try:
        a.db.close()
    except Exception:
        pass
    shutil.rmtree(a.cfg.home, ignore_errors=True)


@dataclass
class Answer:
    system: str
    qid: str
    category: str
    question: str
    answer: str
    correct: bool
    hallucinated: bool
    abstained: bool
    reason: str
    seconds: float
    group: str = ""


def run(llm, systems: Iterable[str], make_aether=None, limit: int = 0, seed: int = 7, temperature: float = 0.2,
        progress: Callable[[str], None] = print) -> list[Answer]:
    facts, rules = build_world(seed)
    questions = build_questions(facts)
    if limit:
        keep, seen = [], Counter()
        groups = set()
        for q in questions:
            if q.category == "consistency":
                if q.group in groups or len(groups) < max(1, limit // 3 or 1):
                    groups.add(q.group); keep.append(q)
            elif seen[q.category] < limit:
                seen[q.category] += 1; keep.append(q)
        questions = keep
    notes = notes_for(facts)
    out: list[Answer] = []
    total = len(questions) * len(list(systems := list(systems)))
    for system in systems:
        for q in questions:
            t = time.perf_counter()
            try:
                if system == "plain":
                    text = ask_plain(llm, q.text, temperature)
                elif system == "rag":
                    text = ask_rag(llm, q.text, notes, temperature)
                else:
                    a = make_aether(facts, rules)
                    try:
                        text = a.handle(q.text).text
                    finally:
                        _dispose(a)
            except Exception as e:  # a crash is a wrong answer, not the end of the run
                text = f"[error: {e}]"
            v = score(q, text)
            out.append(Answer(system, q.id, q.category, q.text, text.strip(), v.correct, v.hallucinated, v.abstained,
                              v.reason, round(time.perf_counter() - t, 2), q.group))
            progress(f"[{len(out):>3}/{total}] {LABELS.get(system, system):9} {'ok ' if v.correct else 'NO '} "
                     f"{q.category:13} {q.text[:60]}")
    return out


def summarise(answers: list[Answer]) -> dict:
    res: dict = {}
    for system in dict.fromkeys(a.system for a in answers):
        rows = [a for a in answers if a.system == system]
        cats = {}
        for cat in CATEGORIES:
            cr = [a for a in rows if a.category == cat]
            if not cr:
                continue
            if cat == "consistency":
                groups = defaultdict(list)
                for a in cr:
                    groups[a.group].append(a.correct)
                cats[cat] = {"n": len(groups), "accuracy": sum(all(g) for g in groups.values()) / len(groups),
                             "per_question": sum(a.correct for a in cr) / len(cr)}
            else:
                cats[cat] = {"n": len(cr), "accuracy": sum(a.correct for a in cr) / len(cr)}
        res[system] = {
            "categories": cats,
            "overall": sum(a.correct for a in rows) / len(rows),
            "hallucination_rate": sum(a.hallucinated for a in rows) / len(rows),
            "abstain_when_unknown": cats.get("unanswerable", {}).get("accuracy"),
            "premise_caught": cats.get("false premise", {}).get("accuracy"),
            "median_seconds": sorted(a.seconds for a in rows)[len(rows) // 2],
            "n": len(rows),
        }
    return res


# ---------------------------------------------------------------------------------------------------------------
# Report: plain SVG charts, no plotting library needed


def _bars(title: str, groups: list[str], series: dict[str, list[float | None]], pct: bool = True, w: int = 760,
          h: int = 300, maxv: float | None = None) -> str:
    pad_l, pad_b, pad_t = 46, 46, 46
    plot_w, plot_h = w - pad_l - 16, h - pad_b - pad_t
    vals = [v for s in series.values() for v in s if v is not None]
    top = maxv or (1.0 if pct else (max(vals) * 1.15 if vals else 1))
    gw = plot_w / max(1, len(groups))
    bw = min(34, (gw - 18) / max(1, len(series)))
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{html.escape(title)}" class="chart" style="max-width:{w}px">',
           f'<text x="{pad_l}" y="20" class="t">{html.escape(title)}</text>']
    for i in range(5):
        y = pad_t + plot_h - plot_h * i / 4
        lab = f"{int(100 * top * i / 4)}%" if pct else f"{top * i / 4:.{0 if top >= 8 else 1}f}"
        out.append(f'<line x1="{pad_l}" x2="{w - 16}" y1="{y:.1f}" y2="{y:.1f}" class="g"/>'
                   f'<text x="{pad_l - 6}" y="{y + 4:.1f}" class="a" text-anchor="end">{lab}</text>')
    for gi, g in enumerate(groups):
        x0 = pad_l + gi * gw + (gw - bw * len(series)) / 2
        for si, (name, s) in enumerate(series.items()):
            v = s[gi]
            if v is None:
                continue
            bh = plot_h * min(v, top) / top
            x, y = x0 + si * bw, pad_t + plot_h - bh
            lab = f"{round(100 * v)}%" if pct else f"{v:.1f}"
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw - 3:.1f}" height="{max(bh, 1):.1f}" rx="2" '
                       f'fill="{COLOURS.get(name, "#888")}"><title>{LABELS.get(name, name)}: {lab}</title></rect>'
                       f'<text x="{x + (bw - 3) / 2:.1f}" y="{y - 4:.1f}" class="v" text-anchor="middle">{lab}</text>')
        out.append(f'<text x="{pad_l + gi * gw + gw / 2:.1f}" y="{h - pad_b + 18}" class="a" text-anchor="middle">'
                   f'{html.escape(g)}</text>')
    lx = pad_l
    for name in series:
        out.append(f'<rect x="{lx}" y="{h - 16}" width="11" height="11" rx="2" fill="{COLOURS.get(name, "#888")}"/>'
                   f'<text x="{lx + 16}" y="{h - 6}" class="a">{LABELS.get(name, name)}</text>')
        lx += 130
    out.append("</svg>")
    return "".join(out)


def charts(summary: dict) -> dict[str, str]:
    systems = list(summary)
    cats = [c for c in CATEGORIES if any(c in summary[s]["categories"] for s in systems)]
    return {
        "accuracy": _bars("Correct answers by question type", cats,
                          {s: [summary[s]["categories"].get(c, {}).get("accuracy") for c in cats] for s in systems}),
        "headline": _bars("Headline numbers", ["overall correct", "hallucination rate", "says 'I don't know'",
                                               "catches false premises"],
                          {s: [summary[s]["overall"], summary[s]["hallucination_rate"],
                               summary[s]["abstain_when_unknown"], summary[s]["premise_caught"]] for s in systems}),
        "latency": _bars("Median seconds per answer", ["median"], {s: [summary[s]["median_seconds"]] for s in systems},
                         pct=False, w=420),
    }


def report_html(summary: dict, answers: list[Answer], meta: dict) -> str:
    ch = charts(summary)
    rows = "".join(
        f"<tr class='{'ok' if a.correct else 'no'}'><td>{LABELS.get(a.system, a.system)}</td><td>{a.category}</td>"
        f"<td>{html.escape(a.question)}</td><td>{html.escape(a.answer[:400])}</td><td>{html.escape(a.reason)}</td>"
        f"<td>{a.seconds:.1f}</td></tr>" for a in answers)
    head = "".join(f"<th>{LABELS.get(s, s)}</th>" for s in summary)
    def line(name, key, pct=True):
        return f"<tr><td>{name}</td>" + "".join(
            f"<td>{'' if summary[s].get(key) is None else (f'{100 * summary[s][key]:.0f}%' if pct else f'{summary[s][key]:.1f} s')}</td>"
            for s in summary) + "</tr>"
    table = (f"<table class='sum'><tr><th></th>{head}</tr>" + line("Correct overall", "overall") +
             line("Hallucination rate (lower is better)", "hallucination_rate") +
             line("Says it doesn't know when it can't", "abstain_when_unknown") +
             line("Catches false premises", "premise_caught") + line("Median time per answer", "median_seconds", False) +
             "</table>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AETHER symbolic benchmark</title><style>
:root{{--bg:#fff;--fg:#14202b;--mut:#5b6b7a;--line:#e3e8ee;--ok:#e8f6ee;--no:#fdeeee}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d141b;--fg:#e6eef5;--mut:#93a4b4;--line:#22303c;--ok:#11281c;--no:#2c1616}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif;margin:0 auto;max-width:1040px;padding:24px 16px}}
h1{{margin:0 0 4px}} .mut{{color:var(--mut)}} .chart{{width:100%;max-width:760px;display:block;margin:18px 0}}
.chart .t{{font-weight:600;fill:var(--fg);font-size:15px}} .chart .a{{fill:var(--mut);font-size:12px}} .chart .v{{fill:var(--fg);font-size:11px}}
.chart .g{{stroke:var(--line)}} table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}}
.sum td:not(:first-child),.sum th{{text-align:right}} tr.ok td:nth-child(5){{background:var(--ok)}} tr.no td:nth-child(5){{background:var(--no)}}
.all{{font-size:13px}} .wrap{{overflow-x:auto}}
</style></head><body>
<h1>Does the symbolic layer help?</h1>
<p class="mut">{html.escape(meta.get('model', ''))} · {meta.get('questions')} questions × {len(summary)} systems · seed {meta.get('seed')} ·
temperature {meta.get('temperature')} · {html.escape(meta.get('when', ''))}</p>
{table}{ch['headline']}{ch['accuracy']}{ch['latency']}
<h2>Every answer</h2><div class="wrap"><table class="all"><tr><th>System</th><th>Type</th><th>Question</th><th>Answer</th><th>Verdict</th><th>s</th></tr>{rows}</table></div>
</body></html>"""


def save(out_dir: Path, answers: list[Answer], meta: dict) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarise(answers)
    (out_dir / "results.json").write_text(json.dumps({"meta": meta, "summary": summary,
                                                      "answers": [asdict(a) for a in answers]}, indent=1), encoding="utf-8")
    with open(out_dir / "answers.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(asdict(answers[0]).keys()))
        w.writeheader()
        w.writerows(asdict(a) for a in answers)
    with open(out_dir / "summary.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["system", "category", "n", "accuracy"])
        for s, r in summary.items():
            for c, v in r["categories"].items():
                w.writerow([s, c, v["n"], f"{v['accuracy']:.3f}"])
            w.writerow([s, "overall", r["n"], f"{r['overall']:.3f}"])
            w.writerow([s, "hallucination_rate", r["n"], f"{r['hallucination_rate']:.3f}"])
    for name, svg in charts(summary).items():
        (out_dir / f"{name}.svg").write_text(svg, encoding="utf-8")
    (out_dir / "report.html").write_text(report_html(summary, answers, meta), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m aether.evals.symbolic", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=None, help="path to aether.toml (default: the usual one)")
    ap.add_argument("--systems", default=",".join(SYSTEMS), help="comma-separated: plain,rag,aether")
    ap.add_argument("--limit", type=int, default=0, help="at most this many questions per type (0 = all)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--temperature", type=float, default=0.2, help="the same for every system")
    ap.add_argument("--out", default="bench_symbolic")
    args = ap.parse_args(argv)
    systems = [s.strip() for s in args.systems.split(",") if s.strip() in SYSTEMS]
    if not systems:
        ap.error("choose systems from plain, rag, aether")

    from ..core.config import load_config
    from ..core.logging import setup_logging
    from ..llm import LLMUnavailable, load_llm
    from ..llm.embeddings import load_embedder
    setup_logging("ERROR")
    cfg = load_config(args.config)
    cfg.llm.temperature = args.temperature
    print("Loading the model once for all three systems...")
    try:
        llm = load_llm(cfg.llm)
    except LLMUnavailable as e:
        print(f"No model loaded: {e}")
        return 1
    make = make_aether_factory(cfg, llm, load_embedder(cfg.embeddings)) if "aether" in systems else None
    t0 = time.time()
    answers = run(llm, systems, make, args.limit, args.seed, args.temperature)
    meta = {"model": Path(cfg.llm.model_path).name or cfg.llm.backend, "seed": args.seed, "temperature": args.temperature,
            "questions": len({a.qid for a in answers}), "systems": systems,
            "when": time.strftime("%Y-%m-%d %H:%M"), "minutes": round((time.time() - t0) / 60, 1),
            "gpu_layers": cfg.llm.gpu_layers, "context_tokens": cfg.llm.context_tokens}
    summary = save(Path(args.out), answers, meta)
    print(f"\n{'':38}" + "".join(f"{LABELS[s]:>11}" for s in summary))
    for label, key in (("correct overall", "overall"), ("hallucination rate (lower is better)", "hallucination_rate"),
                       ("says it doesn't know when it can't", "abstain_when_unknown"),
                       ("catches false premises", "premise_caught")):
        print(f"{label:38}" + "".join(f"{'' if summary[s][key] is None else f'{100 * summary[s][key]:.0f}%':>11}" for s in summary))
    for c in CATEGORIES:
        print(f"  {c:36}" + "".join(f"{100 * summary[s]['categories'][c]['accuracy']:>10.0f}%" if c in summary[s]['categories'] else f"{'':>11}" for s in summary))
    print(f"\nReport: {Path(args.out) / 'report.html'}  (took {meta['minutes']} min)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
