from datetime import date, datetime

from aether.memory.plans import Plans, handle
from aether.wellness.companion import assess, companion_instructions

PLAN = """# Plans
## Now · until 2027-01-18
Goal: start the internship strong
### Daily
- [ ] Solve 3-5 NeetCode 150 problems
- [ ] Commit to GitHub
### Week 1 · 2026-11-26 to 2026-12-02
- [ ] Claude Code course (DeepLearning.AI)
- [ ] Gen AI for software development
### Week 2 · 2026-12-03 to 2026-12-09
- [ ] TypeScript on Udemy
## Internship · 2027-01-18 to 2027-06-30
- [ ] Get the full-time conversion offer
## Later
- [ ] MBA after about two years of work
"""


def test_plans_lead_with_now_not_the_far_future(tmp_path):
    p = Plans(tmp_path / "plans.md")
    p.save(PLAN)
    t = p.today(date(2026, 11, 27))
    assert t["horizon"] == "Now" and t["days_left"] == 52 and t["block"].startswith("Week 1")
    assert t["daily"][0].startswith("Solve 3-5") and t["focus"][0].startswith("Claude Code")
    assert p.today(date(2026, 12, 5))["focus"] == ["TypeScript on Udemy"]
    assert p.today(date(2027, 2, 1))["horizon"] == "Internship"
    said = p.describe(today=date(2026, 11, 27))
    assert "MBA" not in said and "NeetCode" in said
    assert "MBA" in p.describe(scope="all", today=date(2026, 11, 27))


def test_plans_by_voice(tmp_path):
    p = Plans(tmp_path / "plans.md")
    p.save(PLAN)
    assert "Now" in handle(p, "what's my plan for today?")
    assert handle(p, "I finished the claude code course").startswith("Ticked off: Claude Code course")
    assert "- [x] Claude Code course" in p.text()
    assert handle(p, "done with quantum basket weaving") is None
    assert "MBA" in handle(p, "what are my plans")


def test_self_doubt_gets_evidence_not_platitudes(make_aether):
    a = make_aether()
    a.store.add("Aryan passed the AWS AI Practitioner exam.", "semantic", "user_stated")
    a.plans.save(PLAN)
    text = "I'm a robotics student and I got a software job, I feel like an imposter"
    sig = assess(text)
    addendum, mode = companion_instructions(sig, text, None)
    assert sig.level == "stressed" and mode == "self_doubt" and "evidence" in addendum
    block = a._evidence_block()
    assert "AWS AI Practitioner" in block and "Plan progress" in block


def test_rest_and_overwork_nudges(make_aether):
    a = make_aether()
    pe = a.proactive
    f = pe.awareness.focus
    day = datetime(2026, 11, 26, 18, 0)
    f.day_minutes[day.date().isoformat()] = 11 * 60
    assert pe._rest_triggers(day) == 1
    rows = a.db.execute("SELECT title FROM notifications WHERE kind='rest'").fetchall()
    assert rows[0][0] == "Call it a day?"


def test_briefing_has_plan_and_75_hard(make_aether):
    a = make_aether()
    a.plans.save(PLAN.replace("until 2027-01-18", "until 2099-01-18"))
    a.grind.set_field("weight", 62.6)
    b = a.briefing(datetime.now(), online=False)
    assert "**Plan · Now" in b.written and "**75 Hard:**" in b.written and "NeetCode" in b.spoken
