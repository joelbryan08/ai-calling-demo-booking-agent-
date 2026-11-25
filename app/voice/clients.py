"""Thin clients for Sarvam's streaming STT (WebSocket), chat completions (SSE) and TTS (REST)."""
import asyncio
import base64
import json
import logging
from typing import AsyncIterator
from urllib.parse import urlencode

import httpx

from ..config import settings
from .audio import pcm16_to_mulaw, strip_wav

log = logging.getLogger("voice")
BASE = "https://api.sarvam.ai"


class RealtimeSTT:
    """One WebSocket per call. Caller audio goes in as mu-law 8 kHz; events (vad.*, transcript.*) come out."""

    def __init__(self, on_event):
        self.on_event, self.ws, self._tasks = on_event, None, []

    async def connect(self):
        import websockets
        q = urlencode({"language_code": "unknown" if settings.agent_language in ("auto", "unknown") else settings.agent_language, "model": settings.stt_model, "stream_type": "fast",
                       "encoding": "mulaw", "sample_rate": 8000, "endpointing": "vad",
                       "silence_duration_ms": settings.stt_silence_ms})
        self.ws = await websockets.connect(f"wss://api.sarvam.ai/speech-to-text-realtime/ws?{q}",
                                           additional_headers={"api-subscription-key": settings.sarvam_api_key},
                                           max_size=None)
        self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._keepalive())]

    async def send_audio(self, b64_payload: str):
        if self.ws:
            await self.ws.send(json.dumps({"event": "audio_input", "audio": b64_payload}))

    async def _read(self):
        try:
            async for raw in self.ws:
                await self.on_event(json.loads(raw))
        except Exception:
            log.exception("STT socket closed")

    async def _keepalive(self):
        while True:
            await asyncio.sleep(20)
            await self.ws.send(json.dumps({"event": "ping"}))

    async def close(self):
        for t in self._tasks:
            t.cancel()
        if self.ws:
            try:
                await self.ws.send(json.dumps({"event": "end"}))
                await self.ws.close()
            except Exception:
                pass


class SarvamLLM:
    """Streams ("text", str) pieces, then at most one ("tool_calls", [{id, name, arguments}])."""

    def __init__(self):
        self._http = httpx.AsyncClient(base_url=BASE, timeout=30.0)

    async def stream(self, messages: list, tools: list) -> AsyncIterator[tuple]:
        payload = {"model": settings.llm_model, "messages": messages, "tools": tools, "tool_choice": "auto",
                   "parallel_tool_calls": False, "stream": True, "max_tokens": settings.llm_max_tokens,
                   "temperature": settings.llm_temperature,
                   "reasoning_effort": settings.llm_reasoning_effort or None}
        calls: dict = {}
        async with self._http.stream("POST", "/v1/chat/completions", json=payload,
                                     headers={"api-subscription-key": settings.sarvam_api_key}) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data in ("", "[DONE]"):
                    continue
                for choice in json.loads(data).get("choices", []):
                    delta = choice.get("delta") or {}
                    if delta.get("content"):          # never read delta.reasoning_content aloud
                        yield "text", delta["content"]
                    for tc in delta.get("tool_calls") or []:
                        c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        c["id"] = tc.get("id") or c["id"]
                        fn = tc.get("function") or {}
                        c["name"] = fn.get("name") or c["name"]
                        c["arguments"] += fn.get("arguments") or ""
        if calls:
            yield "tool_calls", [calls[i] for i in sorted(calls)]


    async def complete(self, messages: list, max_tokens: int = 500) -> str:
        r = await self._http.post("/v1/chat/completions", headers={"api-subscription-key": settings.sarvam_api_key}, json={
            "model": settings.llm_model, "messages": messages, "stream": False, "max_tokens": max_tokens,
            "temperature": 0.2, "reasoning_effort": settings.llm_reasoning_effort or None})
        r.raise_for_status()
        return r.json()["choices"][0]["message"].get("content") or ""


class SarvamTTS:
    """Text -> mu-law 8 kHz bytes ready for Plivo."""

    def __init__(self):
        self._http = httpx.AsyncClient(base_url=BASE, timeout=30.0)
        self.codec = settings.tts_codec

    async def synthesize(self, text: str, lang: str | None = None) -> bytes:
        speaker = settings.tts_speaker_hi if (lang or "").startswith("hi") and settings.tts_speaker_hi else settings.tts_speaker
        body = {"text": text, "target_language_code": lang or settings.tts_language, "speaker": speaker,
                "model": settings.tts_model, "speech_sample_rate": 8000, "output_audio_codec": self.codec}
        r = await self._http.post("/text-to-speech", json=body, headers={"api-subscription-key": settings.sarvam_api_key})
        if r.status_code == 400 and self.codec == "mulaw":   # codec not accepted here: ask for PCM and convert
            log.warning("TTS rejected mulaw (%s); falling back to linear16", r.text[:200])
            self.codec = "linear16"
            return await self.synthesize(text, lang)
        r.raise_for_status()
        audios = r.json().get("audios") or []
        if not audios:
            raise RuntimeError("Sarvam TTS returned no audio")
        audio = strip_wav(base64.b64decode("".join(audios)))
        return pcm16_to_mulaw(audio) if self.codec != "mulaw" else audio
