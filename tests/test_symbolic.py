from datetime import date

from aether.neurosymbolic.kg import KnowledgeGraph
from aether.neurosymbolic.policy import Decision, PolicyEngine
from aether.neurosymbolic.rules import RuleEngine
from aether.neurosymbolic.temporal import resolve
from aether.memory.store import MemoryStore


def kg_for(tmp_path):
    store = MemoryStore(tmp_path / "t.db")
    return KnowledgeGraph(store.db, store.lock)


def test_functional_contradiction_detected(tmp_path):
    kg = kg_for(tmp_path)
    assert kg.assert_fact("user", "lives_in", "Delhi") == []
    conflicts = kg.assert_fact("user", "lives_in", "Mumbai")
    assert conflicts and "one value" in conflicts[0].reason
    assert [t.o for t in kg.query("user", "lives_in", None)] == ["delhi"]


def test_user_statement_overrides_inferred(tmp_path):
    kg = kg_for(tmp_path)
    kg.assert_fact("user", "works_at", "acme", source="inferred")
    assert kg.assert_fact("user", "works_at", "globex", source="user_stated") == []
    assert [t.o for t in kg.query("user", "works_at", None)] == ["globex"]


def test_forward_chaining_and_proof(tmp_path):
    kg = kg_for(tmp_path)
    kg.assert_fact("asha", "parent_of", "aryan")
    kg.assert_fact("aryan", "sibling_of", "riya")
    eng = RuleEngine(kg)
    new, viol = eng.run()
    assert ("asha", "parent_of", "riya") in [d.triple for d in new]
    assert any("because" in line for line in eng.explain(("asha", "parent_of", "riya")))
    assert not viol


def test_constraint_violation(tmp_path):
    kg = kg_for(tmp_path)
    kg.assert_fact("user", "likes", "jazz")
    kg.assert_fact("user", "dislikes", "jazz")
    _, viol = RuleEngine(kg).run()
    assert any("inconsistent_preference" in v.constraint for v in viol)


def test_temporal_resolution():
    monday = date(2026, 11, 23)
    assert resolve("tomorrow", monday) == date(2026, 11, 24)
    assert resolve("friday", monday) == date(2026, 11, 27)
    assert resolve("next monday", monday) == date(2026, 11, 30)
    assert resolve("in 2 weeks", monday) == date(2026, 12, 7)


def test_policy_blocks_secrets_and_outside_paths(cfg, tmp_path):
    pe = PolicyEngine(cfg.security)
    docs = cfg.security.allowed_read_roots[0]
    assert pe.evaluate("fs.read", f"{docs}/notes.md").decision == Decision.ALLOW
    assert pe.evaluate("fs.read", f"{docs}/.env").decision == Decision.DENY
    assert pe.evaluate("fs.read", f"{docs}/.ssh/id_rsa").decision == Decision.DENY
    assert pe.evaluate("fs.read", str(tmp_path / "elsewhere.txt")).decision == Decision.DENY
    assert pe.evaluate("fs.create", f"{docs}/new.md").decision == Decision.DENY  # read root is not a write root
    out = cfg.security.allowed_write_roots[0]
    assert pe.evaluate("fs.create", f"{out}/plan.md").decision == Decision.CONFIRM
    assert pe.evaluate("shell").decision == Decision.CONFIRM
    assert pe.evaluate("web.search").decision == Decision.DENY  # network off in tests
    pe.emergency_stop = True
    assert pe.evaluate("fs.read", f"{docs}/notes.md").decision == Decision.DENY
