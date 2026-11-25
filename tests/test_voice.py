import asyncio
import json
import struct
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.tools as tools
import app.voice.session as session_mod
from app.db import SessionLocal
from app.google_clients import get_calendar, get_sheets
from app.main import app
from app.models import Booking, Lead
from app.voice.audio import mulaw_to_pcm16_sample, pcm16_to_mulaw, strip_wav
from app.voice.session import CallSession, _split_sentence
from app.voice.tools_bridge import CallContext

NOW = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)  # Monday 11:30 IST


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(tools, "_now", lambda: NOW)
    monkeypatch.setattr(session_mod, "END_MARGIN", 0)


class FakeTTS:
    async def synthesize(self, text, lang=None):
        return b"\xff" * (len(text) * 20)


class FakeSTT:
    def __init__(self, on_event):
        self.on_event, self.audio, self.closed = on_event, [], False

    async def connect(self): pass
    async def send_audio(self, payload): self.audio.append(payload)
    async def close(self): self.closed = True


class FakeLLM:
    def __init__(self, turns):
        self.turns, self.calls = list(turns), []
        self.summary = '{"summary": "Asha loses customers to slow follow-ups.", "sentiment": "positive", "pain_points": "churn", "next_step": "demo on Tuesday"}'

    async def complete(self, messages, max_tokens=500):
        return "Sure! " + self.summary

    async def stream(self, messages, tools):
        self.calls.append(json.loads(json.dumps(messages)))
        turn = self.turns.pop(0)
        for event in (turn(messages) if callable(turn) else turn):
            yield event


def tool(name, **args):
    return ("tool_calls", [{"id": f"c-{name}", "name": name, "arguments": json.dumps(args)}])


def last_tool_json(messages):
    return json.loads([m for m in messages if m["role"] == "tool"][-1]["content"])


class Call:
    def __init__(self, llm, iid="call-1", tts=None, call_id=None, record=False, rec_dir=None):
        self.sent, self.closed, self.t = [], False, 0.0
        self.stt = None

        async def send(m): self.sent.append(json.loads(m))

        async def close(): self.closed = True

        def factory(cb):
            self.stt = FakeSTT(cb)
            return self.stt

        ctx = CallContext(call_id=call_id, lead_name="Asha", company="Acme", phone="+919876543210", interaction_id=iid)
        self.session = CallSession(send, close, ctx, stt_factory=factory, llm=llm, tts=tts or FakeTTS(), clock=lambda: self.t, record=record, rec_dir=rec_dir)

    async def start(self):
        await self.session.on_plivo_message(json.dumps({"event": "start", "start": {"streamId": "s1"}}))
        await self.settle()

    async def say(self, text):
        await self.session.on_stt_event({"event": "transcript.final", "text": text})
        await self.settle()

    async def settle(self):
        for _ in range(20):
            t = self.session._turn
            if t is None or t.done():
                return
            await asyncio.wait_for(asyncio.shield(t), 5)

    def kinds(self):
        return [m["event"] for m in self.sent]


def bookings():
    with SessionLocal() as db:
        return list(db.scalars(select(Booking)))


def run(coro):
    return asyncio.run(coro)


def add_lead(phone="+919876543210", name="Asha"):
    with SessionLocal() as db:
        db.add(Lead(phone=phone, name=name, company="Acme"))
        db.commit()


def test_full_short_call_books_without_email_and_confirms_whatsapp():
    add_lead()
    llm = FakeLLM([
        [("text", "That sounds tough. Dexkor automates follow-ups. Can we book a quick 15 minute demo?")],
        [tool("book_demo", slot_start="2026-11-24T15:00:00+05:30")],
        [("text", "Done, you're booked for Tuesday 24 November at 3:00 PM. Is this number on WhatsApp? "
                  "Our team will send the meeting details and follow up there.")],
        [("text", "Great, we'll follow up on WhatsApp. Thanks, bye!"), tool("log_call", outcome="booked", whatsapp="yes")],
        [tool("end_call")],
    ])

    async def scenario():
        c = Call(llm)
        await c.start()
        await c.say("yes, we keep losing customers")
        await c.say("sure, tomorrow at 3 PM")
        await c.say("yes it is")
        return c

    c = run(scenario())
    [b] = bookings()
    assert b.prospect_email == "" and b.prospect_phone == "+919876543210" and b.interaction_id == "call-1" and b.event_id
    assert get_calendar().events[b.event_id]["attendee"] is None          # no email, no invite attendee
    assert c.closed and c.session._logged and c.session.booked
    [row] = get_sheets().rows
    assert row[4] == "booked" and row[5] == "Tuesday 24 November at 3:00 PM"
    assert "WhatsApp: confirmed on this number" in row[7] and row[9] == b.presenter_email and row[10].startswith("https://meet")
    with SessionLocal() as db:
        assert db.scalar(select(Lead).where(Lead.phone == "+919876543210")).whatsapp_consent is True
    for i, m in enumerate(llm.calls[-1]):
        if m.get("tool_calls"):
            assert llm.calls[-1][i + 1]["role"] == "tool"


def test_taken_time_leads_to_one_proposal_not_a_list():
    start = datetime(2026, 11, 24, 9, 30, tzinfo=timezone.utc)           # 15:00 IST
    for p in ("a@co.com", "b@co.com", "c@co.com"):
        get_calendar().add_busy(p, start, start + timedelta(hours=1))

    def after_taken(messages):
        assert last_tool_json(messages)["status"] == "slot_taken"
        return [tool("check_availability", preferred_date="2026-11-24", time_of_day="afternoon")]

    llm = FakeLLM([[tool("book_demo", slot_start="2026-11-24T15:00:00+05:30")], after_taken,
                   [("text", "How about Tuesday at 4:30 PM?")]])

    async def scenario():
        c = Call(llm)
        await c.start()
        await c.say("tomorrow at 3 works")
        return c

    c = run(scenario())
    assert bookings() == []
    offered = last_tool_json(llm.calls[-1])
    assert offered["status"] == "ok" and all(x["slot_start"] != "2026-11-24T15:00:00+05:30" for x in offered["slots"])
    spoken = [t for who, t in c.session.transcript if who == "Agent"][-1]
    assert spoken.count("?") == 1 and "How about" in spoken


def test_prompt_follows_the_short_script():
    from app.voice.prompt import greeting, system_prompt
    assert greeting() == "Hi, this is Diya from Dexkor. Are you having any problems handling customer churn and support?"
    text = system_prompt("Asha", "Acme", datetime(2026, 11, 23).date())
    assert "NEVER ask for an email" in text and "NEVER read out a list of times" in text
    assert "Tuesday 24 November = 2026-11-24" in text and "Is this number on WhatsApp?" in text


def test_barge_in_clears_audio_and_cancels_the_turn():
    async def scenario():
        c = Call(FakeLLM([]))
        await c.start()
        assert c.session.speaking_until > 0
        await c.session.on_stt_event({"event": "vad.speech_start"})
        return c

    c = run(scenario())
    assert {"event": "clearAudio", "streamId": "s1"} in c.sent and c.session.speaking_until == 0


def test_hangup_without_logging_is_still_recorded():
    async def scenario(speak):
        c = Call(FakeLLM([[("text", "Great, tell me about your follow-ups.")]]), iid=f"call-{speak}")
        await c.start()
        if speak:
            await c.say("yes I have a minute")
        await c.session.finish()
        return c

    run(scenario(True))
    assert get_sheets().rows[-1][4] == "callback" and "before an outcome" in get_sheets().rows[-1][7]
    run(scenario(False))
    assert get_sheets().rows[-1][4] == "no_answer"


def test_unparseable_time_is_reported_back_to_the_model():
    llm = FakeLLM([[tool("book_demo", slot_start="tomorrow 3pm")], [("text", "Sorry, what time again?")]])

    async def scenario():
        c = Call(llm)
        await c.start()
        await c.say("book it")

    run(scenario())
    assert "slot_start must look like" in llm.calls[1][-1]["content"] and bookings() == []


def test_speech_during_a_tool_call_is_queued_not_lost(monkeypatch):
    gate = {}

    async def slow_tool(name, args, ctx):
        await gate["release"].wait()
        return {"status": "ok", "speech": "I have Tuesday at 10."}

    monkeypatch.setattr(session_mod, "run_tool", slow_tool)
    llm = FakeLLM([[tool("check_availability")], [("text", "Here are some times, anything else?")],
                   [("text", "Sure, Wednesday it is, noted.")]])

    async def scenario():
        gate["release"] = asyncio.Event()
        c = Call(llm)
        await c.start()
        await c.session.on_stt_event({"event": "transcript.final", "text": "do you have anything on Wednesday"})
        await asyncio.sleep(0.05)                      # let the turn reach the (slow) tool
        assert c.session._in_tool
        await c.session.on_stt_event({"event": "transcript.final", "text": "actually Wednesday works better"})
        await asyncio.sleep(0.05)
        assert len(llm.calls) == 1                     # still waiting on the tool
        gate["release"].set()
        await c.settle()

    run(scenario())
    users = [m["content"] for m in llm.calls[-1] if m["role"] == "user"]
    assert users[-1] == "actually Wednesday works better" and len(llm.calls) == 3


def test_helpers():
    assert _split_sentence("Hi there. How are you doing today? Fine") == ("Hi there. How are you doing today?", "Fine")
    assert _split_sentence("Short. ") is None
    for b in range(256):
        if b != 0x7F:                                  # 0x7F and 0xFF are both "zero"
            assert pcm16_to_mulaw(struct.pack("<h", mulaw_to_pcm16_sample(b))) == bytes([b])
    payload = b"\x01\x02\x03\x04"
    wav = b"RIFF" + struct.pack("<I", 36 + 4) + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 7, 1, 8000, 8000, 1, 8) + \
          b"data" + struct.pack("<I", 4) + payload
    assert strip_wav(wav) == payload and strip_wav(payload) == payload


def test_answer_url_returns_stream_xml_in_stream_mode(monkeypatch):
    client = TestClient(app)
    monkeypatch.setenv("VOICE_MODE", "stream")
    r = client.post("/plivo/answer?call_id=7&token=dev-secret")
    assert 'bidirectional="true"' in r.text and "call_id=7&amp;token=" in r.text and "<Hangup/>" in r.text
    assert "audio/x-mulaw;rate=8000" in r.text
    monkeypatch.setenv("VOICE_MODE", "test_message")
    assert "<Speak>" in client.post("/plivo/answer?call_id=7&token=dev-secret").text


def test_voice_socket_rejects_bad_token():
    client = TestClient(app)
    with pytest.raises(Exception):
        with client.websocket_connect("/voice/ws?call_id=1&token=wrong") as ws:
            ws.receive_text()


def test_live_registry_shows_the_call_until_it_ends():
    from app.voice.session import LIVE

    async def scenario():
        c = Call(FakeLLM([[("text", "Great, tell me more about that.")]]), iid="call-live")
        await c.start()
        await c.say("yes we do have problems")
        snap = c.session.snapshot()
        assert len(LIVE) == 1 and snap["name"] == "Asha" and snap["lines"][-1]["who"] == "Agent"
        await c.session.finish()

    run(scenario())
    assert not LIVE


def test_voicemail_is_detected_and_hung_up_on():
    async def scenario():
        c = Call(FakeLLM([]), iid="call-vm")        # no LLM turns: the agent must not pitch a machine
        await c.start()
        await c.say("Hi, you have reached John's voicemail. Please leave a message after the tone.")
        await c.session.finish()
        return c

    c = run(scenario())
    assert c.closed and get_sheets().rows[-1][4] == "voicemail"


def test_silence_gets_a_nudge_then_a_polite_goodbye(monkeypatch):
    monkeypatch.setattr(session_mod, "WATCH_EVERY", 0.01)
    monkeypatch.setattr(session_mod, "SILENCE_SECS", 5.0)

    async def scenario():
        c = Call(FakeLLM([]), iid="call-quiet")
        await c.start()
        before = c.kinds().count("playAudio")
        c.t = 100
        await asyncio.sleep(0.1)
        await c.settle()
        assert c.kinds().count("playAudio") > before and c.session.nudges == 1 and not c.closed
        c.t = 300
        await asyncio.sleep(0.1)
        await c.settle()
        assert c.closed
        await c.session.finish()

    run(scenario())
    assert get_sheets().rows[-1][4] == "no_answer"


def test_caller_language_switches_the_voice_language():
    langs = []

    class Tracking(FakeTTS):
        async def synthesize(self, text, lang=None):
            langs.append(lang)
            return await super().synthesize(text)

    async def scenario():
        c = Call(FakeLLM([[("text", "Theek hai, bataiye.")]]), iid="call-hi", tts=Tracking())
        await c.start()
        await c.session.on_stt_event({"event": "transcript.final", "text": "haan boliye", "language_code": "hi-IN"})
        await c.settle()

    run(scenario())
    assert langs[0] != "hi-IN" and langs[-1] == "hi-IN"


def test_recorder_builds_a_stereo_wav_and_drops_interrupted_audio():
    import base64 as b64
    from app.voice.recorder import Recorder
    clock = {"t": 0.0}
    r = Recorder(lambda: clock["t"])
    r.start()
    r.caller_frame(b64.b64encode(b"\xff" * 800).decode())              # 0.1 s of caller audio
    r.agent_audio(b"\x80" * 4000, 0.05)                                # agent starts 0.05 s in, 0.5 s long
    clock["t"] = 0.15
    r.cut()                                                            # caller interrupts at 0.15 s
    assert len(r.agent) == 1200
    wav = r.wav_bytes()
    assert wav[:4] == b"RIFF" and wav[8:16] == b"WAVEfmt " and len(wav) == 44 + 1200 * 4


def test_call_is_recorded_and_summarised(tmp_path):
    import base64 as b64
    from app.models import CallInsight
    from app.voice.session import BG

    async def scenario():
        c = Call(FakeLLM([[("text", "Great, tell me more about that.")]]), iid="call-rec", call_id=7, record=True, rec_dir=tmp_path)
        await c.start()
        for _ in range(3):
            await c.session.on_plivo_message(json.dumps({"event": "media", "media": {"payload": b64.b64encode(b"\xff" * 160).decode()}}))
        await c.say("yes we keep losing customers")
        await c.session.finish()
        await asyncio.gather(*BG)

    run(scenario())
    wav = (tmp_path / "call-7.wav").read_bytes()
    assert wav[:4] == b"RIFF" and len(wav) > 44
    with SessionLocal() as db:
        ins = db.get(CallInsight, 7)
        assert ins.sentiment == "positive" and "slow follow-ups" in ins.summary and ins.next_step == "demo on Tuesday"


def test_recording_adds_a_notice_to_the_opening_line(monkeypatch):
    import dataclasses
    from app.config import settings
    from app.voice import prompt
    monkeypatch.setattr(prompt, "settings", dataclasses.replace(settings, record_calls=True))
    line = prompt.greeting()
    assert "This call may be recorded." in line and line.index("recorded") < line.index("Are you having")
