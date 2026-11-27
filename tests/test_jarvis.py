"""Jarvis batch 1: sentence-streamed speech, routines, spoken mode and persona, screen awareness."""
import time

import pytest
from conftest import FakeLLM
from test_webapp import StreamingLLM, client

from aether.core.config import VoiceConfig
from aether.speech.chunker import SentenceChunker
from aether.speech.voice_loop import VoiceAssistant
from aether.tools.routines import parse_steps

ANSWER = ("Here's a focused plan for today, Aryan. **Top priority:** finish the essay [memory 1]. It costs 3.5 hours.\n\n"
          "1. Essay first, deep work, phone away\n2. Gym at noon\n\n```python\nprint('hi. there. ')\n```\n\n"
          "| a | b |\n| 1 | 2 |\n\nTip: you work best before 1 PM!")


def chunks(text, size=3):
    c, out = SentenceChunker(), []
    for i in range(0, len(text), size):
        out += c.feed(text[i:i + size])
    return out + c.flush()


# ------------------------------------------------------------------ speech chunking
def test_chunker_yields_clean_sentences_and_skips_code_and_tables():
    out = chunks(ANSWER)
    assert out[0] == "Here's a focused plan for today, Aryan."
    assert "Top priority: finish the essay." in out
    assert out[-1] == "Tip: you work best before 1 PM!"
    spoken = " ".join(out)
    assert "print" not in spoken and "|" not in spoken and "*" not in spoken and "[memory" not in spoken
    assert "3.5 hours" in spoken  # decimals are not sentence ends


def test_chunker_emits_first_sentence_before_the_answer_is_finished():
    c = SentenceChunker()
    assert c.feed("The meeting moved to Thursday at ten") == []
    assert c.feed(". I updated") == ["The meeting moved to Thursday at ten."]
    assert c.flush() == ["I updated"]


def test_chunker_is_token_size_independent():
    assert chunks(ANSWER, 1) == chunks(ANSWER, 7) == chunks(ANSWER, 1000)


class StreamingAether:
    """Emits tokens with a delay so speech can start before handle() returns."""
    def __init__(self, text, replace=None):
        self.text, self.replace, self.spoken = text, replace, None

    def handle(self, text, on_event=None, spoken=False):
        self.spoken, self.finished = spoken, False
        for w in self.text.split(" "):
            on_event("token", {"text": w + " "})
            time.sleep(0.005)
        if self.replace:
            on_event("replace", {"text": self.replace})
        self.finished = True
        return type("R", (), {"text": self.replace or self.text})()


class RecordingTTS:
    def __init__(self, ae): self.ae, self.said, self.during, self.stopped = ae, [], [], 0
    def say(self, text):
        self.said.append(text); self.during.append(not self.ae.finished); return True
    def stop(self): self.stopped += 1


def run_voice(ae):
    tts = RecordingTTS(ae)
    va = VoiceAssistant(ae, VoiceConfig(), stt=type("S", (), {"transcribe": lambda s, a: "plan my day"})(), tts=tts,
                        wake=object())
    va.handle_utterance(None)
    for _ in range(200):
        if va.state == "listening":
            break
        time.sleep(0.01)
    return va, tts


def test_voice_loop_starts_speaking_while_the_model_is_still_writing():
    ae = StreamingAether("First, the essay is due at noon today. Then the gym at four. After that the slides need "
                         "two more hours. Finally call Riya about the weekend plans tonight.")
    va, tts = run_voice(ae)
    assert ae.spoken is True  # the voice loop asks for a short spoken answer
    assert tts.said[0] == "First, the essay is due at noon today." and tts.during[0]
    assert " ".join(tts.said).endswith("weekend plans tonight.")
    assert va.state == "idle"   # a statement, not a question: back to waiting for its name, not answering the room


def test_voice_loop_says_the_corrected_answer_when_the_verifier_rewrites():
    ae = StreamingAether("Riya lives in Pune and has done so for years, as far as I know. She moved there in 2019.",
                         replace="Riya lives in Delhi now; you told me she moved.")
    va, tts = run_voice(ae)
    assert tts.said[-1] == "Riya lives in Delhi now; you told me she moved."


# ------------------------------------------------------------------ routines
TOOLS = {"app_open", "remind", "shell", "run_routine", "kg_query"}


def test_routine_steps_parse_and_validate():
    steps = parse_steps('app_open name=spotify\n# comment\nremind text="stretch and drink water" when="in 50 minutes"', TOOLS)
    assert steps == [{"tool": "app_open", "args": {"name": "spotify"}},
                     {"tool": "remind", "args": {"text": "stretch and drink water", "when": "in 50 minutes"}}]
    for bad, msg in [("teleport to=mars", "unknown tool"), ("run_routine name=x", "can't call"), ("app_open spotify", "name=value"),
                     ("", "at least one")]:
        with pytest.raises(ValueError, match=msg):
            parse_steps(bad, TOOLS)


def test_saying_a_routine_name_runs_it_without_the_model(make_aether):
    llm = FakeLLM()
    a = make_aether(llm)
    a.routines.save("Focus Mode", 'remind text="check email" when=tomorrow\nkg_query pattern="likes(user, ?x)"',
                    set(a.tools.names()), "deep work")
    a.routines.save("start my day", "remind text=water when=today", set(a.tools.names()))
    assert a.handle("start my day").text.startswith("Ran **start my day**")
    assert a.handle("Jarvis, start my day.").text.startswith("Ran **start my day**")
    for said in ["focus mode", "Jarvis, start focus mode please", "run my focus routine", "activate focus"]:
        n = len(llm.calls)
        r = a.handle(said)
        assert r.text.startswith("Ran **focus mode**: 2 of 2 steps done."), said
        assert len(llm.calls) == n  # deterministic: no model call
    assert a.routines.list()[0]["runs"] == 4
    assert a.handle("what's my focus like?").text != ""  # not a routine trigger


def test_a_declined_step_stops_the_routine(make_aether):
    a = make_aether(FakeLLM(), confirm=lambda prompt: False)
    a.routines.save("music", "app_open name=spotify\nremind text=x when=tomorrow", set(a.tools.names()))
    r = a.handle("music")
    assert "0 of 2" in r.text and "declined" in r.text and "Stopped early" in r.text


def test_routine_api(make_aether):
    a, c = client(make_aether)
    assert c.post("/api/routines", json={"name": "x", "steps": "nope"}).status_code == 400
    r = c.post("/api/routines", json={"name": "start my day", "steps": "remind text=water when=today"}).json()
    assert r == {"name": "start my day", "steps": "remind text=water when=today"}
    lst = c.get("/api/routines").json()
    assert lst["items"][0]["name"] == "start my day" and "remind" in lst["tools"]
    run = c.post("/api/routines/start my day/run").json()
    assert run["results"][0]["ok"] and "1 of 1" in run["text"]
    assert c.delete("/api/routines/start my day").json() == {"deleted": True}
    assert c.post("/api/routines/start my day/run").status_code == 404


def test_run_routine_tool(make_aether):
    a = make_aether(FakeLLM())
    assert "No routine called" in a.tools.call("run_routine", {"name": "nope"}).output
    a.routines.save("wind down", "remind text=sleep when=today", set(a.tools.names()))
    assert "1 of 1" in a.tools.call("run_routine", {"name": "wind down"}).output
    assert "wind down" in a.tools.call("list_routines", {}).output


# ------------------------------------------------------------------ persona, spoken mode, screen
def system_prompt(llm):
    return next(m.content for msgs, schema in llm.calls if schema is None for m in msgs if m.role == "system")


def test_spoken_mode_and_persona_shape_the_prompt(make_aether):
    llm = FakeLLM()
    a = make_aether(llm)
    a.cfg.address_as = "sir"
    a.handle("what's the plan for tomorrow?", spoken=True)
    sp = system_prompt(llm)
    assert "SPOKEN aloud" in sp and "chief of staff" in sp and "'sir'" in sp
    llm.calls.clear()
    a.cfg.persona = "neutral"
    a.handle("and the day after?")
    sp = system_prompt(llm)
    assert "SPOKEN" not in sp and "chief of staff" not in sp


class RecordingStream(StreamingLLM):
    def stream(self, messages, **kw):
        self.calls.append((messages, None))
        yield from super().stream(messages, **kw)


def test_chat_stream_voice_flag_reaches_the_prompt(make_aether):
    llm = RecordingStream()
    a, c = client(make_aether, llm)
    c.post("/api/chat/stream", json={"text": "what is the capital of france?", "voice": True})
    assert "SPOKEN aloud" in system_prompt(llm)


def test_look_at_screen_is_off_by_default_and_asks_first(make_aether):
    asked = []
    a = make_aether(FakeLLM(), confirm=lambda p: asked.append(p) or True)
    r = a.tools.call("look_at_screen", {"question": "what's this error?"})
    assert asked and "screen.observe" in asked[0]  # always needs approval
    assert "Screen access is off" in r.output


def test_voice_loop_keeps_talking_when_only_unspoken_parts_change():
    ae = StreamingAether("The battery lasts forty seven minutes per charge [doc 1]. That is enough for the demo.",
                         replace="The battery lasts forty seven minutes per charge. That is enough for the demo.")
    va, tts = run_voice(ae)
    assert tts.said == ["The battery lasts forty seven minutes per charge.", "That is enough for the demo."]
