"""The sound wire format of `decomp tas serve`: the live stream's WAV header (`/audio/RUN` is in tas_serve)."""

from __future__ import annotations

import struct

AUDIO_LEAD_SECONDS = 0.25  # a listener joins this far behind the live end of the sound


def wav_header(rate: int) -> bytes:
    """A RIFF/WAVE header for s16 stereo of unknown length (sizes at their maximum), for a live stream."""
    size = 0xFFFFFFFF
    fmt = struct.pack("<HHIIHH", 1, 2, rate, rate * 4, 4, 16)
    return (
        b"RIFF"
        + struct.pack("<I", size)
        + b"WAVEfmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", size - 36)
    )
