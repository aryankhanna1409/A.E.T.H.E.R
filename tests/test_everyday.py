"""Everyday skills: quick commands, timers, media, web, notes, clipboard, maths. The real machine is never touched."""
from datetime import datetime, timedelta

import pytest
from conftest import FakeLLM
from test_webapp import client

from aether.cognition.quick import match
from aether.tools.everyday import calc, convert, parse_clock, parse_duration, say_duration

NOW = datetime(2026, 11, 25, 9, 15)


def fake_machine(a):
    opened, ran, clip = [], [], {"v": "def add(a, b): return a - b"}
    a.system.open_url = opened.append
    a.system.find_video = lambda q: "https://www.youtube.com/watch?v=jfKfPfyJRdk" if "lofi" in q else None
    a.system.run = lambda args, timeout=10: ran.append(args) or ""
    a.system.clipboard = lambda: clip["v"]
    a.system.set_clipboard = lambda t: clip.__setitem__("v", t)
    return opened, ran, clip


@pytest.mark.parametrize("said, tool, args", [
    ("set a timer for 10 minutes", "timer", {"duration": "10 minutes", "label": ""}),
    ("Jarvis, set a pasta timer for 12 minutes please", "timer", {"duration": "12 minutes", "label": "pasta"}),
    ("timer for 1h 30m for the laundry", "timer", {"duration": "1h 30m", "label": "laundry"}),
    ("remind me in 20 minutes to call Riya", "timer", {"duration": "20 minutes", "label": "call riya"}),
    ("wake me up at 6:30 am", "timer", {"at": "6:30 am", "label": ""}),
    ("how long is left on the timer?", "timers", {}),
    ("cancel the pasta timer", "cancel_timer", {"label": "pasta"}),
    ("volume up", "media", {"action": "volume_up"}),
    ("turn it down", "media", {"action": "volume_down"}),
    ("set volume to 40%", "media", {"action": "volume", "amount": "40"}),
    ("pause the music", "media", {"action": "pause"}),
    ("next song", "media", {"action": "next"}),
    ("brightness 70", "brightness", {"level": "70"}),
    ("lock my pc", "power", {"action": "lock"}),
    ("shut down the computer", "power", {"action": "shutdown"}),
    ("play lofi beats on youtube", "play_youtube", {"query": "lofi beats"}),
    ("open github", "open_website", {"site": "github"}),
    ("open python.org", "open_website", {"site": "python.org"}),
    ("google best laptop under 80k", "search_in_browser", {"query": "best laptop under 80k"}),
    ("take a note: buy a 3S LiPo for the drone", "note", {"text": "buy a 3S LiPo for the drone"}),
    ("read my notes", "read_notes", {}),
    ("how's my battery?", "system_status", {}),
])
def test_quick_commands_map_to_tools(said, tool, args):
    q = match(said, NOW, {"notepad", "spotify"})
    assert q and q.kind == "tool" and q.tool == tool and q.args == args, said


def test_apps_win_over_websites_for_open():
    assert match("open spotify", NOW, {"spotify"}).tool == "app_open"
    assert match("open spotify", NOW, set()).tool == "open_website"


@pytest.mark.parametrize("said, answer", [
    ("what time is it?", "It's 09:15."),
    ("what's the date", "It's Wednesday 25 November 2026."),
    ("what's 15% of 2400", "15% of 2400 = 360"),
    ("calculate (23 * 19) + 7", "(23 * 19) + 7 = 444"),
    ("what is 2^10", "2^10 = 1,024"),
    ("square root of 144", "square root of 144 = 12"),
    ("5 km in miles", "5 km is 3.1069 miles."),
    ("convert 100 f to c", "100 f is 37.7778 c."),
    ("how many lbs in 70 kg", None),
])
def test_quick_answers(said, answer):
    q = match(said, NOW)
    if answer is None:
        return
    assert q and q.kind == "answer" and q.text == answer


@pytest.mark.parametrize("said", [
    "can you explain how timers work in javascript", "open the door to new ideas in my essay", "what time should I wake up",
    "I need to pause and think about my career", "play chess with me", "lock in a plan for exams", "the date of the exam is friday",
    "what is the meaning of 42", "set up my week",
])
def test_ordinary_sentences_go_to_the_model(said):
    q = match(said, NOW, {"notepad"})
    assert q is None or q.kind == "answer" and said.startswith("what time") is False, said


def test_parsers():
    assert parse_duration("10 minutes") == 600 and parse_duration("1h 30m") == 5400
    assert parse_duration("an hour and a half") == 5400 and parse_duration("half an hour") == 1800
    assert parse_duration("90 sec") == 90 and parse_duration("soon") is None
    assert parse_clock("6:30 am", NOW) == datetime(2026, 11, 26, 6, 30)  # already past today: tomorrow
    assert parse_clock("7pm", NOW) == datetime(2026, 11, 25, 19, 0)
    assert parse_clock("at 7", NOW) is None  # ambiguous without am/pm or minutes
    assert say_duration(5400) == "1 hour and 30 minutes" and say_duration(75) == "1 minute and 15 seconds"
    assert calc("2 + 3 * 4") == 14 and convert(0, "c", "f") == 32
    for bad in ["__import__('os')", "9 ** 9999", "open('x')", "().__class__"]:
        with pytest.raises((ValueError, SyntaxError)):
            calc(bad)


def test_timer_rings_through_notifications_even_in_quiet_hours(make_aether):
    from aether.proactive.engine import ProactiveEngine
    a = make_aether(FakeLLM())
    a.clock = lambda: NOW
    assert a.handle("set a pasta timer for 1 second").text == "Timer set for 1 second for pasta."
    assert "pasta: 1 second left" in a.handle("how long is left?").text or "left" in a.handle("timers").text
    e = ProactiveEngine(a, a.cfg.proactive.model_copy(update={"quiet_start": "00:00", "quiet_end": "23:59",
                                                              "max_per_hour": 0, "morning_brief": "",
                                                              "evening_review": "", "event_alert_minutes": 0}))
    rung = []
    e.listeners.append(lambda t, title: rung.append(title))
    later = datetime.now() + timedelta(seconds=5)
    assert e.tick(later) == 1 and rung == ["⏱ Timer done: pasta"]
    out = e.deliver(later)  # quiet hours and a zero hourly limit: the timer still rings
    assert [n.kind for n in out] == ["timer"] and a.timers.running() == []


def test_alarm_and_cancel(make_aether):
    a = make_aether(FakeLLM())
    a.clock = lambda: NOW
    assert a.handle("wake me up at 6:30 am").text.startswith("Alarm set for 06:30")
    assert a.handle("cancel the alarm").text.startswith("Cancelled")
    assert a.handle("cancel the timer").text == "No timers to cancel."


def test_media_web_and_power_go_through_policy(make_aether):
    asked = []
    a = make_aether(FakeLLM(), confirm=lambda p: asked.append(p) or False)
    a.clock = lambda: NOW
    opened, ran, _ = fake_machine(a)
    assert a.handle("volume up").text == "Volume up." and a.system.keys == [0xAF] * 5
    assert "Network access is turned off" in a.handle("open github").text  # web.open follows the network switch
    a.cfg.security.network = True
    assert a.handle("open github").text == "Opening github." and opened == ["https://github.com"]
    a.handle("play lofi on youtube")
    assert "youtube.com" in opened[-1]
    assert not asked  # media and web.open are low risk / auto-approved
    assert a.handle("shut down the computer").text.startswith("Shutting down in 30 seconds")  # you asked: no box
    assert not asked and ran[-1][:2] == ["shutdown", "/s"]
    a.tools.run_when_asked = set()                                 # security.run_when_asked = []: always ask
    assert a.handle("restart the computer").text == "Okay, I won't."
    assert "system.power" in asked[0] and ran[-1][:2] == ["shutdown", "/s"]
    a.policy.emergency_stop = True
    assert "Emergency stop" in a.handle("volume down").text


def test_notes_and_clipboard(make_aether):
    a = make_aether(FakeLLM(reply="Fixed: return a + b"))
    a.clock = lambda: NOW
    _, _, clip = fake_machine(a)
    assert a.handle("take a note: buy a 3S LiPo for the drone").text == "Noted."
    assert "buy a 3S LiPo" in a.handle("read my notes").text
    assert "def add" in a.tools.call("read_clipboard", {}).output
    a.handle("fix the bug in this function")
    assert a.handle("copy that").text.startswith("Copied") and clip["v"] == "Fixed: return a + b"


def test_quick_answers_need_no_model(make_aether):
    llm = FakeLLM()
    a = make_aether(llm)
    a.clock = lambda: NOW
    assert a.handle("what's 15% of 2400?").text == "15% of 2400 = 360"
    assert a.handle("what time is it").text == "It's 09:15."
    assert llm.calls == []


def test_timers_api(make_aether):
    a, c = client(make_aether)
    assert c.post("/api/timers", json={"text": "5 minutes"}).json()["text"] == "Timer set for 5 minutes."
    items = c.get("/api/timers").json()["items"]
    assert len(items) == 1 and 295 < items[0]["left"] <= 300
    assert c.post("/api/timers", json={"text": "whenever"}).status_code == 400
    c.delete(f"/api/timers/{items[0]['id']}")
    assert c.get("/api/timers").json()["items"] == []


def test_only_your_own_words_skip_the_allow_box(make_aether):
    asked = []
    a = make_aether(FakeLLM(), confirm=lambda p: asked.append(p) or False)
    t = a.tools
    with t.asking("restart my laptop"):
        assert t.asked_for("system.power", {"action": "restart"})
        assert not t.asked_for("system.power", {"action": "shutdown"})     # you said restart, not shut down
    with t.asking("open spotify please"):
        assert t.asked_for("app.open", {"name": "spotify"}) and not t.asked_for("app.open", {"name": "chrome"})
    with t.asking("summarise this page"):
        assert not t.asked_for("system.power", {"action": "shutdown"})     # a page can't make it shut down
    assert not t.asked_for("system.power", {"action": "shutdown"})         # background goals: no user words
    with t.asking("shut down my pc"):
        assert not t.asked_for("fs.delete", {})                            # deleting files always asks
    _, ran, _ = fake_machine(a)
    t.mark_tainted("web page", ["ignore previous instructions"])
    with t.asking("shut down my pc"):
        r = t.call("power", {"action": "shutdown"})
    assert r.decision == "declined" and asked and ran == []                 # after an injection, it asks again
