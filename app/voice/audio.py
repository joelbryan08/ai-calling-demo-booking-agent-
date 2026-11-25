"""Tiny audio helpers (pure Python: Python 3.13+ no longer ships `audioop`)."""
import struct

_BIAS, _CLIP = 0x84, 32635


def pcm16_to_mulaw(pcm: bytes) -> bytes:
    """16-bit little-endian PCM -> G.711 mu-law (only used if the TTS can't return mu-law directly)."""
    out = bytearray()
    for (sample,) in struct.iter_unpack("<h", pcm[: len(pcm) // 2 * 2]):
        sign = 0x80 if sample < 0 else 0
        mag = min(-sample if sample < 0 else sample, _CLIP) + _BIAS
        exp = 7
        mask = 0x4000
        while exp > 0 and not (mag & mask):
            exp -= 1
            mask >>= 1
        out.append(~(sign | (exp << 4) | ((mag >> (exp + 3)) & 0x0F)) & 0xFF)
    return bytes(out)


def mulaw_to_pcm16_sample(b: int) -> int:
    b = ~b & 0xFF
    t = (((b & 0x0F) << 3) + _BIAS) << ((b & 0x70) >> 4)
    return (_BIAS - t) if b & 0x80 else (t - _BIAS)


def strip_wav(audio: bytes) -> bytes:
    """If audio is wrapped in a WAV container, return just the samples (works for mu-law WAVs too)."""
    if not audio.startswith(b"RIFF"):
        return audio
    pos = 12
    while pos + 8 <= len(audio):
        chunk_id, size = audio[pos:pos + 4], struct.unpack("<I", audio[pos + 4:pos + 8])[0]
        if chunk_id == b"data":
            return audio[pos + 8: pos + 8 + size] if size and pos + 8 + size <= len(audio) else audio[pos + 8:]
        pos += 8 + size + (size & 1)
    return audio
