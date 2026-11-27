import json
from datetime import date, timedelta

from fastapi.testclient import TestClient

from aether.health.grind import FoodTable, Grind, add_days, handle, ymd
from aether.interface.server import create_app


def test_food_parser_matches_grind75_rules():
    items, unknown = FoodTable().parse("3 roti, 1 katori dal and 200 ml curd")
    assert [i["name"] for i in items] == ["roti", "dal (cooked)", "curd"] and not unknown
    assert items[0]["kcal"] == 330 and items[0]["protein"] == 10.5   # 3 x 110 kcal, 3 x 3.5 g
    paneer, _ = FoodTable().parse("100g paneer")
    assert paneer[0]["kcal"] == 265 and paneer[0]["protein"] == 18     # per-gram foods scale by grams
    _, unknown = FoodTable().parse("2 plates of quantum soup")
    assert unknown


def test_voice_logging_and_status(tmp_path):
    g = Grind(tmp_path, "Aryan", import_from=tmp_path / "none.json")
    assert "Logged roti" in handle(g, "I had 2 roti and 1 glass milk")
    assert handle(g, "weight 62.6").startswith("Logged 62.6 kg")
    assert "under your target" in handle(g, "slept 5.5 hours")
    assert handle(g, "drank 2 glasses of water").startswith("Water: 0.5")
    assert "Chest" in handle(g, "workout done, chest and shoulders")
    assert "20 of 20 pages" in handle(g, "read 20 pages")
    assert "₹250" in handle(g, "spent 250 on lunch")
    assert handle(g, "my target weight is 67") == "Target weight set to 67 kg."
    s = handle(g, "how's my 75 hard going?")
    assert "Day 1 of 75" in s and "62.6 kg, target 67 kg" in s
    assert handle(g, "tell me a joke") is None
    assert handle(g, "I had a long day") is None          # conversation, not a food log
    saved = json.loads(g.file.read_text(encoding="utf-8"))
    assert saved["days"][ymd()]["weight"] == 62.6


def test_streak_trend_and_rest_day(tmp_path):
    g = Grind(tmp_path, "A", import_from=tmp_path / "none.json")
    start = date.today() - timedelta(days=10)
    g.state["profile"]["startDate"] = start.isoformat()
    for i in range(8):
        d = (start + timedelta(days=i + 3)).isoformat()
        g.day(d)["weight"] = 62 + i * 0.05
        g.log_workout("Quads", d=d)
        g.set_field("sleepH", 5, d=d)
    assert g.weight_rate() is not None and 0.3 < g.weight_rate() < 0.4
    assert g.consecutive_training_days() >= 6
    advice = g.rest_advice()
    assert advice and "in a row" in advice and "sleep was short" in advice


def test_imports_existing_grind75_data_and_keeps_voice_logs_on_ui_save(tmp_path):
    app_file = tmp_path / "grind75-data.json"
    app_file.write_text(json.dumps({"version": 1, "profile": {"name": "A", "startDate": "2026-11-23", "startWeight": 62,
                                                               "targetWeight": 72, "heightCm": 170},
                                    "days": {"2026-11-24": {"checks": {}, "food": [], "weight": 62.4}}}))
    g = Grind(tmp_path / "home", "A", import_from=app_file)
    assert g.state["days"]["2026-11-24"]["weight"] == 62.4 and g.state["targets"]["kcal"] > 0
    page = g.snapshot()                            # the page loads...
    handle(g, "I had 2 banana")                    # ...AETHER logs by voice...
    page["days"]["2026-11-24"]["weight"] = 62.5    # ...and the page saves its older copy
    g.save_from_ui(page, page["_rev"])
    assert [f["name"] for f in g.state["days"][ymd()]["food"]] == ["banana"]
    assert g.state["days"]["2026-11-24"]["weight"] == 62.5


def test_grind_routes(tmp_path):
    cfgfile = tmp_path / "aether.toml"
    cfgfile.write_text(f'home = "{(tmp_path / "h").as_posix()}"\n[embeddings]\nbackend = "lexical"\n'
                       '[llm]\nmodel_path = "missing.gguf"\n[security]\nnetwork = false\n')
    c = TestClient(create_app(str(cfgfile)))
    assert "Grind 75" in c.get("/grind/").text
    assert c.get("/grind/app.js").status_code == 200 and c.get("/grind/bridge.js").status_code == 200
    assert "window.FOODS" in c.get("/grind/foods.js").text
    st = c.get("/api/grind/state").json()
    st["days"][ymd()] = {"checks": {"workout": True}, "food": [], "weight": 63}
    assert c.post("/api/grind/state", json={"state": st, "rev": st["_rev"]}).json()["rev"] == st["_rev"] + 1
    assert c.get("/api/grind/state").json()["days"][ymd()]["weight"] == 63
    assert c.get("/api/grind/ai").json() == {"aether": False}
    assert c.post("/api/grind/food", json={"text": "2 roti"}).json()["ok"] is False   # honest: no model, page falls back
    assert c.post("/api/grind/scenery", json={"date": ymd()}).json() == {"ok": False}  # network off
    assert "Day" in c.get("/api/grind/summary").json()["status"] or "starts" in c.get("/api/grind/summary").json()["status"]
    assert add_days("2026-01-31", 1) == "2026-02-01"
