"""The plain-vs-RAG-vs-AETHER benchmark: the world, the scoring and the plumbing (the real numbers need the real model)."""
import json
import re

from conftest import FakeLLM

from aether.cognition.ir import Entity, IntentIR
from aether.evals import symbolic as sb


def test_world_and_questions_are_fixed_by_the_seed():
    f1, _ = sb.build_world(7)
    f2, _ = sb.build_world(7)
    assert [(f.s, f.p, f.o, f.old) for f in f1] == [(f.s, f.p, f.o, f.old) for f in f2]
    qs = sb.build_questions(f1)
    counts = {c: sum(q.category == c for q in qs) for c in sb.CATEGORIES}
    assert counts == {"recall": 12, "multi-hop": 12, "updated": 6, "unanswerable": 10, "false premise": 8,
                      "consistency": 18}
    assert len({q.id for q in qs}) == len(qs)
    for q in qs:
        if q.category == "updated":
            assert q.expect[0] != q.wrong[0]
        if q.category == "unanswerable":
            assert not q.expect
    assert all(len([q for q in qs if q.group == g]) == 3 for g in {q.group for q in qs if q.group})


def test_multi_hop_answers_follow_from_the_rules():
    from aether.neurosymbolic.kg import KnowledgeGraph
    from aether.neurosymbolic.rules import RuleEngine
    import sqlite3
    import threading
    facts, rules = sb.build_world()
    kg = KnowledgeGraph(sqlite3.connect(":memory:", check_same_thread=False), threading.Lock())
    for f in facts:
        kg.assert_fact(f.s, f.p, f.o)
    eng = RuleEngine(kg)
    for r in rules:
        eng.add_rule(r, persist=False)
    eng.run(persist=True)
    assert kg.query("Meera Iyer", "grandparent_of", "Kabir Iyer")
    assert kg.query("Meera Iyer", "parent_of", "Arjun Iyer")          # via the sibling rule
    assert kg.query("Riya Iyer", "works_in", "Ghent")
    assert kg.query("Isha Iyer", "experienced_with", "ROS 2")
    assert kg.query("motor driver", "part_of", "rover chassis")


def q(cat, expect=(), wrong=()):
    return sb.Question("x", cat, "?", list(expect), list(wrong))


def test_scoring_by_code():
    assert sb.score(q("recall", ["Oslo"]), "Kabir lives in Oslo.").correct
    assert not sb.score(q("recall", ["Oslo"]), "Kabir lives in Osloville.").correct
    v = sb.score(q("recall", ["Oslo"]), "He lives in Bergen.")
    assert v.hallucinated and not v.correct
    v = sb.score(q("recall", ["Oslo"]), "I don't have that information.")
    assert v.abstained and not v.correct and not v.hallucinated
    assert sb.score(q("multi-hop", ["yes"], ["no"]), "Yes, through the Kestrel project.").correct
    assert not sb.score(q("multi-hop", ["yes"], ["no"]), "No, not that I know of.").correct
    # updated facts: the new value wins, mentioning the old one as history is fine
    assert sb.score(q("updated", ["Austin"], ["Porto"]), "She moved from Porto to Austin.").correct
    assert not sb.score(q("updated", ["Austin"], ["Porto"]), "She lives in Porto.").correct
    # unanswerable: only "I don't know" is right
    assert sb.score(q("unanswerable"), "I'm not sure, you haven't told me her birthday.").correct
    assert sb.score(q("unanswerable"), "Her birthday is 4 March 1961.").hallucinated
    # false premises: correct it or reject it, never play along
    assert sb.score(q("false premise", ["Leeds"], ["Pune"]), "Liam didn't move to Pune; he lives in Leeds.").correct
    assert sb.score(q("false premise", ["Leeds"], ["Pune"]), "Actually, I have no record of that move.").correct
    v = sb.score(q("false premise", ["Leeds"], ["Pune"]), "He moved to Pune for a new job.")
    assert not v.correct and v.hallucinated
    assert sb.has("kabir_iyer lives_in oslo", "Kabir Iyer")


class Oracle(FakeLLM):
    """Answers with whatever notes or verified facts it was given, and says it doesn't know otherwise."""

    def chat(self, messages, **kw):
        if kw.get("json_schema") is not None:
            return super().chat(messages, **kw)
        text = "\n".join(m.content for m in messages)
        lines = re.findall(r"^- \((?:\d{4}-\d\d)\) (.+)$|^- (.+)$", text, re.M)
        facts = re.search(r"<known_facts>\n(.*?)\n</known_facts>", text, re.S)
        if facts:
            return "From what I know: " + facts.group(1).replace("\n", "; ")
        if lines:
            return "From my notes: " + "; ".join(a or b for a, b in lines)
        return "I don't know."


def test_the_three_systems_run_end_to_end_and_write_a_report(cfg, tmp_path):
    llm = Oracle()
    make = sb.make_aether_factory(cfg, llm, None)
    answers = sb.run(llm, ["plain", "rag", "aether"], make, limit=2, progress=lambda _: None)
    systems = {a.system for a in answers}
    assert systems == {"plain", "rag", "aether"}
    per = len(answers) // 3
    assert per == 2 * 5 + 3          # two per type, plus one consistency group of three
    summary = sb.save(tmp_path / "out", answers, {"model": "oracle", "seed": 7, "temperature": 0.2, "questions": per})
    # the plain model knows nothing, so it is right exactly when the answer is "I don't know"
    assert summary["plain"]["categories"]["recall"]["accuracy"] == 0
    assert summary["plain"]["abstain_when_unknown"] == 1
    # AETHER grounds the names itself and hands the model the derived facts
    assert summary["aether"]["categories"]["recall"]["accuracy"] == 1
    assert summary["aether"]["categories"]["multi-hop"]["accuracy"] == 1
    assert summary["aether"]["categories"]["updated"]["accuracy"] >= 0.5
    for name in ("report.html", "results.json", "answers.csv", "summary.csv", "accuracy.svg", "headline.svg"):
        assert (tmp_path / "out" / name).exists()
    data = json.loads((tmp_path / "out" / "results.json").read_text())
    assert len(data["answers"]) == len(answers)
    assert "<svg" in (tmp_path / "out" / "report.html").read_text()


def test_router_grounds_names_the_model_left_out(make_aether):
    a = make_aether()
    a.kg.assert_fact("Kabir Iyer", "lives_in", "Oslo")
    a.kg.assert_fact("motor driver", "part_of", "drive unit")
    plan = a.router.plan(IntentIR(intent="question", objective="", entities=[]), "Where does Kabir live these days?")
    assert "symbolic" in plan.routes and any(t.o == "oslo" for t in plan.kg_facts)
    plan = a.router.plan(IntentIR(intent="question", objective="", entities=[Entity(name="Kabir", type="person")]),
                         "Is the motor driver part of anything?")
    assert any(t.s == "motor_driver" for t in plan.kg_facts)
    # nobody known is named: nothing extra
    plan = a.router.plan(IntentIR(intent="chat", objective=""), "how's it going?")
    assert "symbolic" not in plan.routes
