import time
from datetime import datetime

from aether.memory.recall import topic_of, window
from aether.proactive.review import week_bounds

NOW = datetime(2026, 11, 27, 15, 0)   # a Friday


def test_date_windows_are_read_by_code():
    s, e, label = window("yesterday", NOW)
    assert (s.day, e.day, label) == (26, 27, "yesterday")
    s, e, label = window("on tuesday", NOW)
    assert s == datetime(2026, 11, 24) and label == "on Tuesday"
    s, e, _ = window("last week", NOW)
    assert s == datetime(2026, 11, 16) and e == datetime(2026, 11, 23)
    s, _, label = window("3 days ago", NOW)
    assert s == datetime(2026, 11, 24) and label == "3 days ago"
    s, _, label = window("on 24 november", NOW)
    assert s == datetime(2026, 11, 24) and label == "on 24 November"
    s, _, _ = window("on december 3", NOW)
    assert s.year == 2025                                # a future date means last year's
    assert window("about my sister", NOW) is None
    assert topic_of("about the internship last week") == "the internship"
    assert topic_of("yesterday") == ""


def _at(a, when: datetime, role: str, text: str):
    a.store.log_turn("s1", 0, role, text)
    a.store.db.execute("UPDATE turns SET created=? WHERE id=(SELECT MAX(id) FROM turns)", (when.timestamp(),))
    a.store.db.commit()


def test_what_did_we_talk_about_yesterday(make_aether):
    a = make_aether()
    a.clock = lambda: NOW
    _at(a, datetime(2026, 11, 26, 10, 0), "user", "help me plan the aws certification study schedule")
    _at(a, datetime(2026, 11, 26, 10, 1), "assistant", "Sure, here's a plan.")
    _at(a, datetime(2026, 11, 26, 21, 0), "user", "the aws exam is in january, how many hours a week?")
    _at(a, datetime(2026, 11, 24, 9, 0), "user", "what's a good protein breakfast")
    out = a.handle("what did we talk about yesterday?").text
    assert "2 messages" in out and "aws" in out and "protein" not in out
    out = a.handle("what did I say about the aws exam yesterday").text
    assert "january" in out
    assert "didn't talk" in a.handle("what did we talk about last week?").text
    out = a.handle("what did we talk about on tuesday").text
    assert "protein" in out and "good" not in out.split("\n")[0]
    assert "what did we talk" not in a.handle("what did we talk about today").text.lower()   # recall asks aren't topics


def test_status_report_and_weekly_review(make_aether):
    a = make_aether()
    a.clock = lambda: NOW
    out = a.handle("status report").text
    assert "Mark XXVIII" in out and "Memory" in out and "Brain" in out
    assert "Mark XXVIII" in a.handle("Nova, run diagnostics").text
    a.grind.set_field("weight", 62.6, "2026-11-23")
    a.grind.set_field("weight", 63.0, "2026-11-27")
    a.grind.set_target_weight(67)
    out = a.handle("how did my week go?").text
    assert "This week in review" in out and "62.6 → 63.0" in out and "toward your target" in out
    assert week_bounds(datetime(2026, 11, 30, 9, 0))[0].isoformat() == "2026-11-23"   # Monday morning looks back


def test_sunday_evening_offers_the_week_in_review(make_aether):
    a = make_aether()
    pe = a.proactive
    pe.cfg.max_per_hour = 1000
    assert pe._review_trigger(datetime(2026, 11, 29, 19, 0)) == 1
    assert pe._review_trigger(datetime(2026, 11, 29, 19, 30)) == 0      # once per Sunday
    assert pe._review_trigger(datetime(2026, 11, 27, 19, 0)) == 0      # not on Friday
