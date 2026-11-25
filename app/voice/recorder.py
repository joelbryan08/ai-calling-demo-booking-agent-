"""Records both sides of a call into a stereo WAV (left = caller, right = agent), kept on your own disk."""
import base64
import struct
import sys
from array import array
from pathlib import Path

from .audio import mulaw_to_pcm16_sample

SILENCE = 0xFF                                   # mu-law zero
_DEC = array("h", [mulaw_to_pcm16_sample(b) for b in range(256)])


class Recorder:
    def __init__(self, clock):
        self.clock, self.t0 = clock, None
        self.caller, self.agent = bytearray(), bytearray()

    def start(self):
        self.t0 = self.clock()

    def caller_frame(self, b64: str):
        self.caller += base64.b64decode(b64)

    def agent_audio(self, audio: bytes, start_at: float):
        if self.t0 is None:
            return
        off = max(0, int((start_at - self.t0) * 8000))
        if off > len(self.agent):
            self.agent += bytes([SILENCE]) * (off - len(self.agent))
        self.agent[off:off + len(audio)] = audio

    def cut(self):
        """The caller interrupted: drop whatever agent audio hadn't played yet."""
        if self.t0 is not None:
            del self.agent[max(0, int((self.clock() - self.t0) * 8000)):]

    def wav_bytes(self) -> bytes:
        n = max(len(self.caller), len(self.agent))
        left = bytes(self.caller) + bytes([SILENCE]) * (n - len(self.caller))
        right = bytes(self.agent) + bytes([SILENCE]) * (n - len(self.agent))
        pcm = array("h", [0]) * (2 * n)
        pcm[0::2] = array("h", (_DEC[b] for b in left))
        pcm[1::2] = array("h", (_DEC[b] for b in right))
        if sys.byteorder == "big":
            pcm.byteswap()
        data = pcm.tobytes()
        return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 2, 8000, 32000, 4, 16)
                + b"data" + struct.pack("<I", len(data)) + data)

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.wav_bytes())
