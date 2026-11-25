"""Jarvis has to do what you ask, quickly: to-dos that repeat, apps by name, the HUD, closing apps, screenshots,
Nova's typed commands, and no slow fact-check on a simple question."""
import json
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from conftest import FakeLLM

from aether.cognition import quick
from aether.memory.prospective import next_due, parse_at, parse_repeat
from aether.tools.apps import Apps, Launch

NOW = datetime(2026, 11, 25, 9, 0)


def q(text, can_open=lambda n, exact=False: n in {"spotify", "downloads folder", "task manager", "whatsapp", "chrome"}):
    return quick.match(text, NOW, can_open)


# ---------------------------------------------------------------- understanding everyday wording
def test_todo_phrasings():
    assert q("add buy milk to my todo list").args == {"text": "buy milk"}
    assert q("make a todo to drink water").args == {"text": "drink water"}
    assert q("todo: call the bank tomorrow").args == {"text": "call the bank", "when": "tomorrow"}
    assert q("remind me to stretch every day at 6pm").args == {"text": "stretch", "repeat": "every day", "at": "at 6pm"}
    assert q("every morning remind me to check email").args == {"text": "check email", "repeat": "every morning"}
    assert q("hey jarvis can you remind me to call mom at 6pm please").args == {"text": "call mom", "at": "at 6pm"}
    assert q("remind me in 10 minutes to stretch").tool == "timer"           # still a timer
    assert q("add a daily task to journal").args == {"text": "journal", "repeat": "every day"}


def test_make_it_a_daily_thing():
    for said in ["make it a daily thing", "make that daily", "make it a daily habit", "do that every day",
                 "make it recurring", "Nova, make this a daily task"]:
        r = q(said)
        assert r is not None and r.tool == "repeat_todo", said
    assert q("make that a daily task at 9am").args == {"repeat": "daily", "at": "at 9am"}
    assert q("make it weekly").args["repeat"] == "weekly"


def test_list_and_tick_off():
    for said in ["what's on my todo list", "what do i have to do today", "show my tasks", "my reminders", "todo list"]:
        assert q(said).tool == "todos", said
    assert q("mark buy milk as done").args == {"item": "buy milk"} and not q("mark buy milk as done").loose
    assert q("i finished stretching").loose      # conversation unless it's on the list
    assert q("remove call the bank from my list").tool == "remove_todo"


def test_apps_hud_and_pc_control():
    assert q("open the hud").tool == "show_aether" and q("nova, open the hud").args == {"view": "hud"}
    assert q("open aether settings").args == {"view": "settings"}
    assert q("can you open youtube for me").tool == "open_website"
    assert q("open spotify").tool == "app_open"                       # the app, when installed
    assert quick.match("open spotify", NOW, lambda n, exact=False: False).tool == "open_website"   # else the site
    assert q("open my downloads folder").args == {"name": "downloads folder"}
    assert q("open task manager").tool == "app_open"
    assert q("open the pod bay doors") is None                        # unknown: the model handles it
    assert q("close chrome").args == {"name": "chrome"} and q("quit spotify").tool == "app_close"
    assert q("close it") is None and q("close the door") is None
    assert q("remind me what we talked about yesterday") is None
    assert q("take a screenshot").tool == "screenshot" and q("screenshot").tool == "screenshot"
    assert q("show desktop").tool == "show_desktop"
    assert q("what's the weather").args == {"when": "today"}
    assert q("weather in mumbai tomorrow").args == {"when": "tomorrow", "place": "mumbai"}
    assert q("is it going to rain tomorrow").tool == "weather"
    assert q("what's the latest news").tool == "news" and q("news about robotics").args == {"topic": "robotics"}


# ---------------------------------------------------------------- repeating to-dos
def test_repeat_and_time_parsing():
    assert parse_repeat("every day") == parse_repeat("daily") == parse_repeat("every morning") == "daily"
    assert parse_repeat("every monday") == "weekly:mon" and parse_repeat("weekdays") == "weekdays"
    assert parse_repeat("every 3 days") == "every:3" and parse_repeat("tomorrow") == ""
    assert parse_at("at 6pm") == "18:00" and parse_at("9:30") == "09:30" and parse_at("every morning") == "08:00"
    assert parse_at("at 7") == "07:00" and parse_at("in 3 days") == ""
    friday = date(2026, 11, 27)
    assert next_due("weekdays", friday) == date(2026, 11, 30)


def test_todo_end_to_end(make_aether):
    a = make_aether(FakeLLM())
    assert "drink water" in a.handle("add drink water to my todo list").text
    r = a.handle("make it a daily thing at 9pm")
    assert "every day at 9 pm" in r.text and r.state.intent == "quick"
    item = a.reminders.last()
    assert item.repeat == "daily" and item.at == "21:00"
    r = a.handle("mark drink water as done")
    assert "comes back tomorrow" in r.text
    again = a.reminders.get(item.id)
    assert again.status == "open" and again.due > date.today().isoformat()    # comes back tomorrow
    assert "drink water" in a.handle("what's on my todo list").text
    assert a.handle("dinner is done").state.intent != "quick"               # not on the list: just conversation


def test_missed_daily_item_rolls_forward_not_overdue(make_aether):
    a = make_aether(FakeLLM())
    item = a.reminders.add("meditate", "", "daily", "", today=date.today() - timedelta(days=4))
    assert item.due < date.today().isoformat()
    assert [r.due for r in a.reminders.due()] == [date.today().isoformat()]


def test_timed_reminder_rings_once_at_its_time(make_aether):
    from aether.proactive.engine import ProactiveEngine
    a = make_aether(FakeLLM())
    a.reminders.add("stretch", "", "daily", "18:00")
    e = ProactiveEngine(a, a.cfg.proactive.model_copy(update={"quiet_start": "23:59", "quiet_end": "00:00",
                                                              "evening_review": "", "morning_brief": ""}))
    e.battery_probe = lambda: None
    rung = []
    e.listeners.append(lambda t, title: rung.append(t.label))
    today = datetime.combine(date.today(), datetime.min.time())
    e.tick(today + timedelta(hours=17, minutes=59))
    assert rung == []
    e.tick(today + timedelta(hours=18, minutes=1))
    e.tick(today + timedelta(hours=18, minutes=2))
    assert rung == ["stretch"]
    assert any("Stretch" in n.title for n in e.deliver(today + timedelta(hours=18, minutes=2)))


# ---------------------------------------------------------------- apps
def test_apps_resolve_store_and_start_menu_apps():
    apps = Apps({"notepad": "notepad.exe"})
    apps._shortcuts = (1e18, {"Spotify": "C:/sm/Spotify.lnk", "Google Chrome": "C:/sm/Chrome.lnk"})
    apps._store = (1e18, {"WhatsApp": "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"})
    assert apps.resolve("Spotify").kind == "shortcut"
    assert apps.resolve("whatsapp").kind == "store"
    assert [c.kind for c in apps.candidates("chrome")] == ["exe", "shortcut"]   # chrome.exe, then its Start menu entry
    assert apps.resolve("google", exact=True) is None          # "open google" stays the website
    assert apps.resolve("downloads").kind == "folder" and apps.resolve("bluetooth settings").kind == "uri"
    assert apps.resolve("notepad").target == "notepad.exe"


def test_open_falls_back_when_the_configured_exe_is_missing():
    apps = Apps({"spotify": "spotify.exe"})
    apps._shortcuts = (1e18, {"Spotify": "C:/sm/Spotify.lnk"})
    apps._store = (1e18, {})
    tried = []

    def launcher(ln: Launch):
        tried.append(ln.kind)
        if ln.kind == "exe":
            raise FileNotFoundError("spotify.exe")
    apps.launcher = launcher
    assert apps.open("spotify") == "Opening spotify." and tried == ["exe", "shortcut"]


def test_saying_it_runs_it_without_an_allow_box(make_aether):
    asked = []
    a = make_aether(FakeLLM(), confirm=lambda msg: asked.append(msg) or False)
    a.apps.launcher = lambda ln: None
    a.apps._shortcuts = (1e18, {"Spotify": "C:/sm/Spotify.lnk"})
    a.apps._store = (1e18, {})
    opened = []
    a.tools.tools["show_aether"].fn = lambda view="hud": opened.append(view) or "Opening the HUD."
    assert "Opening" in a.handle("open spotify").text
    assert a.handle("open the hud").text == "Opening the HUD." and opened == ["hud"]
    assert asked == []
    # the model can't open an app on its own: that still asks
    r = a.tools.call("app_open", {"name": "spotify"})
    assert r.decision == "declined" and asked


# ---------------------------------------------------------------- speed
def test_simple_question_is_one_model_call_even_with_facts(make_aether):
    llm = FakeLLM(reply="Paris.")
    a = make_aether(llm)
    a.kg.assert_fact("user", "lives_in", "pune")
    a.handle("what is the capital of france?")
    titles = [c[1].get("title") if c[1] else "answer" for c in llm.calls]
    assert titles == ["answer"], titles


def test_personal_question_still_checks_facts(make_aether):
    llm = FakeLLM(reply="You live in Pune.", claims=[{"subject": "user", "predicate": "lives_in", "object": "pune"}])
    a = make_aether(llm)
    a.kg.assert_fact("user", "lives_in", "pune")
    a.handle("where do I live?")
    assert any(c[1] and c[1].get("title") == "Claims" for c in llm.calls)


# ---------------------------------------------------------------- Nova
def test_nova_sends_a_typed_command_and_shows_the_answer():
    from aether.interface.nova import ask, bubble_seconds
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"text": "Opening YouTube."}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert ask(f"http://127.0.0.1:{srv.server_port}", "open youtube") == "Opening YouTube."
        assert got == [{"text": "open youtube"}]
    finally:
        srv.shutdown()
    assert "can't reach" in ask("http://127.0.0.1:9", "hi", timeout=1)
    assert 5 <= bubble_seconds("ok") <= bubble_seconds("x" * 300) <= 25


def test_quick_commands_stay_in_the_conversation(make_aether):
    a = make_aether(FakeLLM())
    a.handle("add buy milk to my todo list")
    a.handle("set a timer for 5 minutes")
    turns = a.conversations.history(a.session)
    assert [t["role"] for t in turns] == ["user", "assistant"] * 2
    assert "buy milk" in turns[1]["text"]
