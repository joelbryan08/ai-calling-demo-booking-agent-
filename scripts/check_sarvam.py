"""Quick check that your Sarvam key, models and voice work with this project.
Run from the project folder:   python scripts/check_sarvam.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings                      # noqa: E402
from app.voice.clients import RealtimeSTT, SarvamLLM, SarvamTTS   # noqa: E402
from app.voice.prompt import TOOLS                    # noqa: E402


def show(ok, title, detail=""):
    print(f"[{'OK ' if ok else 'FAIL'}] {title}" + (f"  {detail}" if detail else ""))
    return ok


async def main():
    if not settings.sarvam_api_key:
        print("SARVAM_API_KEY is empty. Put it in your .env file and run this again.")
        return 1
    print(f"STT {settings.stt_model} | LLM {settings.llm_model} | TTS {settings.tts_model} / {settings.tts_speaker}\n")
    results = []

    try:                                              # 1. text to speech
        tts = SarvamTTS()
        audio = await tts.synthesize("Hello, this is a test.")
        results.append(show(len(audio) > 800, "Text-to-speech", f"{len(audio)} bytes of audio (codec used: {tts.codec}, "
                            f"about {len(audio) / 8000:.1f}s at 8 kHz)"))
    except Exception as e:
        results.append(show(False, "Text-to-speech", f"{type(e).__name__}: {str(e)[:200]}"))
        print("      Check TTS_SPEAKER: speakers differ per model (see Sarvam's Voices page).")

    try:                                              # 2. LLM with a tool
        texts, calls = [], None
        msgs = [{"role": "system", "content": "You are a phone agent. Use tools when asked about availability."},
                {"role": "user", "content": "Do you have any demo slots tomorrow afternoon?"}]
        async for kind, val in SarvamLLM().stream(msgs, TOOLS):
            if kind == "text":
                texts.append(val)
            else:
                calls = val
        if calls:
            results.append(show(True, "LLM tool calling", f"model called {calls[0]['name']}({calls[0]['arguments']})"))
        else:
            results.append(show(bool("".join(texts).strip()), "LLM reply (no tool call)", "".join(texts)[:120]))
            print("      If you expected a tool call, tool calling may be unreliable on this model: tell me.")
    except Exception as e:
        results.append(show(False, "LLM", f"{type(e).__name__}: {str(e)[:200]}"))

    try:                                              # 3. streaming speech to text handshake
        events = []

        async def collect(msg):
            events.append(msg.get("event"))

        stt = RealtimeSTT(collect)
        await stt.connect()
        await stt.send_audio("/w" * 80)               # a little noise, mu-law-ish bytes
        await asyncio.sleep(1.5)
        await stt.close()
        results.append(show("error" not in events, "Streaming speech-to-text connection", f"events: {events}"))
    except Exception as e:
        results.append(show(False, "Streaming speech-to-text", f"{type(e).__name__}: {str(e)[:200]}"))

    print("\nAll good - you can try a call." if all(results) else "\nFix the FAIL lines above first.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
