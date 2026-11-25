"""One phone call: Plivo audio in -> Sarvam STT -> Sarvam LLM (+ tools) -> Sarvam TTS -> Plivo audio out."""
import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path
from datetime import timedelta
from zoneinfo import ZoneInfo

from .. import tools as T
from ..config import settings
from ..db import SessionLocal
from ..models import Call
from ..runtime_settings import defaults
from .insights import SYSTEM as INSIGHT_SYSTEM, parse_insight, save_insight
from .prompt import FILLERS, TOOLS, greeting, system_prompt
from .recorder import Recorder
from .tools_bridge import CallContext, run_tool

log = logging.getLogger("voice")
SENT_END = re.compile(r"[.!?।॥]+[\s\"')\]]*|\n+")
MIN_CHARS, MAX_ROUNDS, TOOL_TIMEOUT = 20, 4, 15
END_MARGIN = 0.6   # seconds to let the goodbye finish playing before hanging up
SILENCE_SECS, WATCH_EVERY = 9.0, 1.0
VOICEMAIL_RE = re.compile(r"voice ?mail|leave (?:a |your )?message|after the (?:tone|beep)|at the tone|mailbox|not reachable|switched off|"
                          r"out of (?:coverage|range)|number you (?:have )?(?:dialled|dialed|called)|can(?:no|')t take your call|"
                          r"बीप|उपलब्ध नहीं है|स्विच ऑफ|पहुंच से बाहर|संदेश छोड़", re.I)
LANGS = {"en-IN", "hi-IN", "bn-IN", "ta-IN", "te-IN", "kn-IN", "ml-IN", "mr-IN", "gu-IN", "pa-IN", "od-IN"}
BG: set = set()      # background tasks (summaries) so they aren't garbage collected
LIVE: dict = {}    # calls in progress (this process), for the dashboard's live view
FALLBACK_LINE = "Sorry, I didn't catch that. Could you say that again?"


def _split_sentence(pending: str):
    for m in SENT_END.finditer(pending):
        if m.end() >= MIN_CHARS:
            return pending[:m.end()].strip(), pending[m.end():]
    return None


class CallSession:
    def __init__(self, send, close, ctx: CallContext, *, stt_factory, llm, tts, clock=time.monotonic, cfg=None, record=None, rec_dir=None):
        self.send, self.close, self.ctx = send, close, ctx
        self.stt_factory, self.llm, self.tts, self.clock = stt_factory, llm, tts, clock
        self.stt = None
        self.stream_id = None
        self.cfg, self.t0, self._key = cfg or defaults(), time.time(), ctx.call_id or id(self)
        tz = ZoneInfo(settings.presenter_timezone)
        today = T._now().astimezone(tz).date()
        self.messages = [{"role": "system", "content": system_prompt(ctx.lead_name, ctx.company, today, self.cfg)}]
        self.transcript: list = []
        self.speaking_until = 0.0
        self._turn = self._timer = None
        self._in_tool = self._end_requested = self._finished = False
        self._logged = self.booked = self.heard_user = False
        self._round_spoken: list = []
        self.pending_user: list = []
        self.rec = Recorder(clock) if (settings.record_calls if record is None else record) else None
        self.rec_dir = rec_dir or settings.recordings_dir
        self.lang, self.nudges, self.last_user, self.user_texts, self._watch = settings.tts_language, 0, 0.0, 0, None

    # ------------------------------------------------------------------ Plivo side
    async def on_plivo_message(self, raw: str):
        data = json.loads(raw)
        event = data.get("event")
        if event == "start":
            await self._start(data)
        elif event == "media" and self.stt:
            payload = (data.get("media") or {}).get("payload")
            if payload:
                if self.rec:
                    self.rec.caller_frame(payload)
                await self.stt.send_audio(payload)   # already base64 mu-law 8 kHz: forward untouched

    async def _start(self, data: dict):
        self.stream_id = (data.get("start") or {}).get("streamId") or data.get("streamId")
        if self.rec:
            self.rec.start()
        self.stt = self.stt_factory(self.on_stt_event)
        await self.stt.connect()
        self._timer = asyncio.create_task(self._max_duration())
        self.last_user, self._watch = self.clock(), asyncio.create_task(self._watchdog())
        opening = greeting(self.cfg)
        LIVE[self._key] = self
        self.messages.append({"role": "assistant", "content": opening})
        self.transcript.append(("Agent", opening))
        self._turn = asyncio.create_task(self._say_safe(opening))

    # ------------------------------------------------------------------ STT side
    async def on_stt_event(self, msg: dict):
        event = msg.get("event")
        if event == "vad.speech_start":
            self.last_user = self.clock()
            if self.clock() < self.speaking_until and not self._in_tool and not self._end_requested:
                await self._barge_in()
        elif event == "transcript.final":
            text = (msg.get("text") or "").strip()
            code = msg.get("language_code") or (msg.get("data") or {}).get("language_code")
            if code in LANGS:
                self.lang = code             # answer in the language the caller is speaking
            if text:
                await self._on_user_text(text)
        elif event == "error":
            log.error("STT error: %s", msg)

    async def _barge_in(self):
        await self._cancel_turn()
        await self._clear_audio()

    async def _on_user_text(self, text: str):
        self.heard_user = True
        self.user_texts += 1
        self.nudges, self.last_user = 0, self.clock()
        self.transcript.append(("Caller", text))
        if self.user_texts <= 3 and VOICEMAIL_RE.search(text):
            return await self._voicemail()
        if self._in_tool:                     # finish the booking first, then answer
            self.pending_user.append(text)
            return
        await self._cancel_turn()
        if self.clock() < self.speaking_until:
            await self._clear_audio()
        self.messages.append({"role": "user", "content": text})
        self._turn = asyncio.create_task(self._respond())

    # ------------------------------------------------------------------ the turn
    async def _respond(self):
        try:
            for _ in range(MAX_ROUNDS):
                tool_calls = await self._llm_round()
                if not tool_calls:
                    break
                await self._run_tools(tool_calls)
                if self._end_requested:
                    break
        except asyncio.CancelledError:
            if self._round_spoken:            # keep only what the caller actually heard
                self.messages.append({"role": "assistant", "content": " ".join(self._round_spoken)})
                self._round_spoken = []
            raise
        except Exception:
            log.exception("turn failed")
            await self._say_safe(FALLBACK_LINE)
        if self._end_requested:
            await self._hangup_after_playback()
        elif self.pending_user:
            self.messages.append({"role": "user", "content": " ".join(self.pending_user)})
            self.pending_user = []
            self._turn = asyncio.create_task(self._respond())

    async def _llm_round(self):
        text_all, pending, tool_calls = "", "", None
        self._round_spoken = spoken = []
        async for kind, val in self.llm.stream(self.messages, TOOLS):
            if kind == "text":
                pending += val
                while (cut := _split_sentence(pending)):
                    sentence, pending = cut
                    if sentence:
                        await self._say(sentence)
                        spoken.append(sentence)
            elif kind == "tool_calls":
                tool_calls = val
        if pending.strip():
            await self._say(pending.strip())
            spoken.append(pending.strip())
        if tool_calls and not spoken and tool_calls[0]["name"] in FILLERS:
            await self._say(FILLERS[tool_calls[0]["name"]])
            spoken.append(FILLERS[tool_calls[0]["name"]])
        if not spoken and not tool_calls:
            await self._say(FALLBACK_LINE)
            spoken.append(FALLBACK_LINE)
        msg = {"role": "assistant", "content": " ".join(spoken) or None}
        if tool_calls:
            msg["tool_calls"] = [{"id": c["id"] or f"call_{i}", "type": "function",
                                  "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                                 for i, c in enumerate(tool_calls)]
            for i, c in enumerate(tool_calls):
                c["id"] = c["id"] or f"call_{i}"
        self.messages.append(msg)
        self._round_spoken = []
        if spoken:
            self.transcript.append(("Agent", " ".join(spoken)))
        return tool_calls

    async def _run_tools(self, tool_calls: list):
        self._in_tool = True
        try:
            for c in tool_calls:
                try:
                    args = json.loads(c["arguments"] or "{}")
                except ValueError:
                    args = {}
                name = c["name"]
                if name == "end_call":
                    result, self._end_requested = {"status": "ending"}, True
                else:
                    try:
                        result = await asyncio.wait_for(run_tool(name, args, self.ctx), TOOL_TIMEOUT)
                    except asyncio.TimeoutError:
                        result = {"status": "error", "message": "The calendar is slow right now. Offer to follow up by email."}
                    if name == "book_demo" and result.get("status") == "booked":
                        self.booked, self.ctx.demo_time = True, result.get("spoken")
                    if name == "log_call" and result.get("status") == "logged":
                        self._logged = True
                self.messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(result)})
        finally:
            self._in_tool = False

    # ------------------------------------------------------------------ speaking
    async def _say(self, text: str):
        text = re.sub(r"[*#`_~]+", "", text).strip()
        if text:
            await self._play(await self.tts.synthesize(text, self.lang))

    async def _say_safe(self, text: str):
        try:
            await self._say(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("TTS failed")

    async def _play(self, audio: bytes):
        if self.rec:
            self.rec.agent_audio(audio, max(self.speaking_until, self.clock()))
        for i in range(0, len(audio), 3200):    # mu-law 8 kHz = 8000 bytes per second
            await self.send(json.dumps({"event": "playAudio", "media": {
                "contentType": "audio/x-mulaw", "sampleRate": 8000,
                "payload": base64.b64encode(audio[i:i + 3200]).decode()}}))
        self.speaking_until = max(self.speaking_until, self.clock()) + len(audio) / 8000

    async def _clear_audio(self):
        await self.send(json.dumps({"event": "clearAudio", "streamId": self.stream_id}))
        if self.rec:
            self.rec.cut()
        self.speaking_until = 0.0

    async def _cancel_turn(self):
        t = self._turn
        if t and not t.done() and t is not asyncio.current_task():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    async def _voicemail(self):
        """A machine answered: don't pitch it. Log and hang up."""
        await self._cancel_turn()
        await self._clear_audio()
        self._end_requested = True
        await run_tool("log_call", {"outcome": "voicemail", "notes": "Reached voicemail; hung up without leaving a message."}, self.ctx)
        self._logged = True
        await self.close()

    async def _watchdog(self):
        """Caller went quiet: one nudge, then a polite goodbye."""
        while True:
            await asyncio.sleep(WATCH_EVERY)
            busy = self._in_tool or self._end_requested or self._finished or (self._turn and not self._turn.done())
            if busy or self.clock() < self.speaking_until or self.clock() - max(self.speaking_until, self.last_user) < SILENCE_SECS:
                continue
            if self.nudges == 0:
                self.nudges, self.last_user = 1, self.clock()
                self._turn = asyncio.create_task(self._say_safe("Hello? Are you still there?"))
            else:
                self._end_requested = True
                self._turn = asyncio.create_task(self._goodbye("I can't hear you, so I'll let you go. Thank you, goodbye."))

    async def _hangup_after_playback(self):
        await asyncio.sleep(max(0.0, self.speaking_until - self.clock()) + END_MARGIN)
        await self.close()     # closing the stream makes Plivo run <Hangup/>

    async def _max_duration(self):
        await asyncio.sleep(settings.max_call_seconds)
        await self._cancel_turn()
        self._end_requested = True
        line = "I'm sorry, I have to let you go now. Thank you for your time. Goodbye."
        self.messages.append({"role": "assistant", "content": line})
        self._turn = asyncio.create_task(self._goodbye(line))

    async def _goodbye(self, line: str):
        await self._say_safe(line)
        await self._hangup_after_playback()

    def snapshot(self) -> dict:
        state = ("working" if self._in_tool else "speaking" if self.clock() < self.speaking_until
                 else "thinking" if self._turn and not self._turn.done() else "listening")
        return {"call_id": self.ctx.call_id, "name": self.ctx.lead_name, "phone": self.ctx.phone, "state": state,
                "elapsed": int(time.time() - self.t0), "lines": [{"who": w, "text": t} for w, t in self.transcript]}

    # ------------------------------------------------------------------ end of call
    async def finish(self):
        if self._finished:
            return
        self._finished = True
        LIVE.pop(self._key, None)
        for t in (self._timer, self._watch):
            if t:
                t.cancel()
        await self._cancel_turn()
        if self.stt:
            await self.stt.close()
        if not self._logged:                    # the caller hung up before the agent logged anything
            outcome = "booked" if self.booked else ("callback" if self.heard_user else "no_answer")
            note = None if self.booked else ("Call ended before an outcome was recorded." if self.heard_user else None)
            await run_tool("log_call", {"outcome": outcome, "notes": note}, self.ctx)
        if self.rec and self.ctx.call_id:
            await asyncio.to_thread(self.rec.save, Path(self.rec_dir) / f"call-{self.ctx.call_id}.wav")
        if self.ctx.call_id and self.heard_user and len(self.transcript) >= 3 and hasattr(self.llm, "complete"):
            task = asyncio.create_task(self._summarize())
            BG.add(task)
            task.add_done_callback(BG.discard)
        if self.ctx.call_id:
            text = "\n".join(f"{who}: {line}" for who, line in self.transcript)
            await asyncio.to_thread(self._save_transcript, text)

    async def _summarize(self):
        try:
            text = "\n".join(f"{who}: {line}" for who, line in self.transcript)[:8000]
            raw = await self.llm.complete([{"role": "system", "content": INSIGHT_SYSTEM}, {"role": "user", "content": text}])
            data = parse_insight(raw)
            def store():
                with SessionLocal() as db:
                    save_insight(db, self.ctx.call_id, data)
            await asyncio.to_thread(store)
        except Exception:
            log.exception("call summary failed")

    def _save_transcript(self, text: str):
        with SessionLocal() as db:
            call = db.get(Call, self.ctx.call_id)
            if call:
                call.transcript = text
                db.commit()
