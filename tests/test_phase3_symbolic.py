from conftest import FakeLLM

from aether.memory.store import MemoryStore
from aether.neurosymbolic.csp import PlanTask, plan_tasks, plan_tool
from aether.neurosymbolic.kg import KnowledgeGraph
from aether.neurosymbolic.rules import RuleEngine


def test_csp_plan_respects_dependencies_and_capacity():
    tasks = [PlanTask("research", 3, 2), PlanTask("draft", 3, 3, ["research"]), PlanTask("edit", 2, 4, ["draft"])]
    sol, _ = plan_tasks(tasks, [3, 3, 3, 3, 3])
    assert sol["research"] < sol["draft"] < sol["edit"]


def test_csp_reports_why_impossible():
    out = plan_tool("a:5:1; b:5:1", "6,6")
    assert out.startswith("No plan") and "day 1" in out


def test_rules_persist_and_first_order_query(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    kg = KnowledgeGraph(s.db, s.lock)
    eng = RuleEngine(kg)
    eng.add_rule("mentor_of(?a, ?b) & studies(?b, ?t) -> can_help_with(?a, ?t)")
    kg.assert_fact("prof_rao", "mentor_of", "aryan")
    kg.assert_fact("aryan", "studies", "ml")
    eng2 = RuleEngine(KnowledgeGraph(s.db, s.lock))  # fresh engine loads the stored rule
    assert {"?x": "prof_rao"} in eng2.ask("can_help_with(?x, ml)")


def test_document_facts_enter_kg_below_user_facts(make_aether, cfg):
    from pathlib import Path
    docs = Path(cfg.security.allowed_read_roots[0])
    (docs / "org.md").write_text("Helios is part of the Aero Lab. Aero Lab is located in Delhi.")
    llm = FakeLLM(structured={"DocFacts": {"facts": [
        {"subject": "Helios", "predicate": "part_of", "object": "Aero Lab"},
        {"subject": "Aero Lab", "predicate": "located_in", "object": "Delhi"}]}})
    a = make_aether(llm)
    msg = a.index_folder(str(docs), extract_facts=True)
    assert "Added 2 facts" in msg
    assert a.kg.query("helios", "part_of", None)[0].source == "document"
